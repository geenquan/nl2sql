import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.core.exceptions import CredentialStoreError
from app.security.credentials import FileCredentialStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestConcurrentWrites:
    def test_two_instances_alternate_puts_keep_all_credentials(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        store_a = FileCredentialStore(path)
        store_b = FileCredentialStore(path)
        store_a.put("cred-a", "secret-a")
        store_b.put("cred-b", "secret-b")
        store_a.put("cred-a2", "secret-a2")
        reader = FileCredentialStore(path)
        assert reader.get("cred-a") == "secret-a"
        assert reader.get("cred-b") == "secret-b"
        assert reader.get("cred-a2") == "secret-a2"

    def test_get_sees_updates_from_other_instance(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        reader = FileCredentialStore(path)
        assert reader.get("k") is None
        writer = FileCredentialStore(path)
        writer.put("k", "v1")
        assert reader.get("k") == "v1"  # 读操作重读文件，不用过期缓存
        writer.put("k", "v2")
        assert reader.get("k") == "v2"

    def test_multiprocess_concurrent_writes_keep_all_credentials(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        script = textwrap.dedent(
            """
            import sys
            from app.security.credentials import FileCredentialStore
            path, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
            store = FileCredentialStore(path)
            for i in range(count):
                store.put(f"cred-{worker}-{i}", f"s-{worker}-{i}")
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(path), str(w), "15"],
                cwd=str(PROJECT_ROOT),
            )
            for w in range(4)
        ]
        for proc in procs:
            assert proc.wait(timeout=60) == 0
        store = FileCredentialStore(path)
        for worker in range(4):
            for i in range(15):
                assert store.get(f"cred-{worker}-{i}") == f"s-{worker}-{i}"


class TestFileStateSemantics:
    def test_missing_file_allows_first_write(self, tmp_path: Path) -> None:
        store = FileCredentialStore(tmp_path / "missing.json")
        assert store.get("k") is None  # 文件不存在＝空存储，允许首次初始化
        store.put("k", "v")
        assert store.get("k") == "v"

    def test_empty_json_object_is_valid_empty_store(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        path.write_text("{}", encoding="utf-8")
        store = FileCredentialStore(path)
        assert store.get("k") is None
        store.put("k", "v")
        assert store.get("k") == "v"

    @pytest.mark.parametrize("content", [b"", b"   \n\t ", b"{ not valid json"])
    def test_existing_empty_whitespace_or_corrupt_file_is_rejected(
        self, tmp_path: Path, content: bytes
    ) -> None:
        path = tmp_path / "creds.json"
        path.write_bytes(content)
        before = path.read_bytes()
        store = FileCredentialStore(path)
        with pytest.raises(CredentialStoreError):
            store.get("any")
        with pytest.raises(CredentialStoreError):
            store.put("any", "value")
        with pytest.raises(CredentialStoreError):
            store.delete("any")
        assert path.read_bytes() == before  # 不修改、不覆盖、不自动修复

    def test_non_object_json_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        path.write_text('["not", "an", "object"]', encoding="utf-8")
        store = FileCredentialStore(path)
        with pytest.raises(CredentialStoreError):
            store.get("any")

    @pytest.mark.parametrize("value", ["123", "null", "true", '{"k": "v"}'])
    def test_non_string_credential_value_is_rejected(self, tmp_path: Path, value: str) -> None:
        path = tmp_path / "creds.json"
        path.write_text(f'{{"k": {value}}}', encoding="utf-8")
        store = FileCredentialStore(path)
        with pytest.raises(CredentialStoreError):
            store.get("k")
        assert path.read_text(encoding="utf-8") == f'{{"k": {value}}}'

    def test_error_messages_never_contain_credential_content(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        path.write_text('{"k": "do-not-leak-me"}\nGARBAGE', encoding="utf-8")
        store = FileCredentialStore(path)
        with pytest.raises(CredentialStoreError) as exc_info:
            store.get("k")
        assert "do-not-leak-me" not in str(exc_info.value)

    def test_delete_consistency_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "creds.json"
        writer = FileCredentialStore(path)
        writer.put("k", "v")
        other = FileCredentialStore(path)
        assert other.delete("k") is True
        assert writer.get("k") is None
        assert other.delete("k") is False


class TestFilesystemErrorUnification:
    """文件系统/锁/编码故障统一转换为 CredentialStoreError（API 503），固定安全消息。"""

    def test_unreadable_path_raises_credential_store_error(self, tmp_path: Path) -> None:
        store = FileCredentialStore(tmp_path)  # 路径是目录：read_text 必然 OSError
        with pytest.raises(CredentialStoreError) as exc_info:
            store.get("k")
        assert "读取失败" in str(exc_info.value)
        assert str(tmp_path) in str(exc_info.value)  # 路径可用于定位，不是凭证

    def test_lock_file_open_failure_raises_credential_store_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def broken_open(*args, **kwargs):
            raise PermissionError("no fd for you")

        monkeypatch.setattr("app.security.credentials.os.open", broken_open)
        store = FileCredentialStore(tmp_path / "creds.json")
        with pytest.raises(CredentialStoreError) as exc_info:
            store.put("k", "v")
        assert "PermissionError" in str(exc_info.value)  # 仅类型名，无底层文本

    def test_flock_failure_raises_credential_store_error(self, tmp_path: Path, monkeypatch) -> None:
        import types

        fake_fcntl = types.SimpleNamespace(
            LOCK_SH=1, LOCK_EX=2,
            flock=lambda fd, op: (_ for _ in ()).throw(OSError("lock busy")),
        )
        monkeypatch.setattr("app.security.credentials.fcntl", fake_fcntl)
        store = FileCredentialStore(tmp_path / "creds.json")
        with pytest.raises(CredentialStoreError) as exc_info:
            store.get("k")
        assert "锁" in str(exc_info.value)

    def test_fsync_failure_converted_and_old_file_intact(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "creds.json"
        store = FileCredentialStore(path)
        store.put("keep", "old-secret")
        before = path.read_bytes()

        def broken_fsync(fd):
            raise OSError("disk sync failed")

        monkeypatch.setattr("app.security.credentials.os.fsync", broken_fsync)
        with pytest.raises(CredentialStoreError) as exc_info:
            store.put("new", "value")
        assert "写入失败" in str(exc_info.value)
        monkeypatch.undo()
        assert path.read_bytes() == before  # 旧文件完整
        assert FileCredentialStore(path).get("keep") == "old-secret"  # 后续可恢复
        store.put("new", "value")  # 恢复后写入正常
        assert store.get("new") == "value"

    def test_replace_failure_converted_to_credential_store_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / "creds.json"
        store = FileCredentialStore(path)
        store.put("keep", "old-secret")
        before = path.read_bytes()

        def broken_replace(src, dst):
            raise PermissionError("replace denied")

        monkeypatch.setattr("app.security.credentials.os.replace", broken_replace)
        with pytest.raises(CredentialStoreError) as exc_info:
            store.put("new", "value")
        assert "写入失败" in str(exc_info.value)
        assert "PermissionError" in str(exc_info.value)
        monkeypatch.undo()
        assert path.read_bytes() == before
        assert not [p for p in tmp_path.glob("*.tmp") if p.is_file()], "失败后不留临时文件"
        store.put("new", "value")
        assert store.get("new") == "value"

    def test_directory_creation_failure_raises_credential_store_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def broken_mkdir(self, mode=0o777, parents=False, exist_ok=False):
            raise PermissionError("mkdir denied")

        monkeypatch.setattr("pathlib.Path.mkdir", broken_mkdir)
        store = FileCredentialStore(tmp_path / "a" / "b" / "creds.json")
        with pytest.raises(CredentialStoreError) as exc_info:
            store.put("k", "v")
        assert "目录" in str(exc_info.value)


class TestPermissions:
    def test_file_permissions_restricted_and_parent_dir_untouched(self, tmp_path: Path) -> None:
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir(mode=0o755)
        os.chmod(existing_dir, 0o755)
        store = FileCredentialStore(existing_dir / "creds.json")
        store.put("k", "v")
        if os.name == "posix":
            assert stat.S_IMODE((existing_dir / "creds.json").stat().st_mode) == 0o600
            assert stat.S_IMODE((existing_dir / "creds.json.lock").stat().st_mode) == 0o600
            assert stat.S_IMODE(existing_dir.stat().st_mode) == 0o755  # 不改用户目录权限

    def test_new_nested_directory_created_restricted(self, tmp_path: Path) -> None:
        if os.name != "posix":
            pytest.skip("权限断言仅 POSIX")
        store = FileCredentialStore(tmp_path / "a" / "b" / "creds.json")
        store.put("k", "v")
        assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "a" / "b").stat().st_mode) == 0o700

    def test_secret_never_in_repr(self, tmp_path: Path) -> None:
        store = FileCredentialStore(tmp_path / "creds.json")
        store.put("k", "do-not-log-me")
        assert "do-not-log-me" not in repr(store)
