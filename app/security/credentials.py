"""本地凭证文件存储（M1 修复轮加固）。

限制（必须知晓后才可在本地开发使用）：
- 凭证以明文保存在项目目录下的独立 JSON 文件中，只有文件权限(0600)与
  同目录锁文件保护，没有加密、审计、轮换或访问控制；仅限本地开发，
  不得用于生产或多用户环境；生产方案在 M8 接入专用密钥管理服务。
- 跨进程互斥依赖 POSIX flock（Linux/macOS）。在无 fcntl 的平台（如 Windows）
  退化为仅进程内锁，多进程并发写不受保护——该平台不在本项目的支持范围内。
- 元数据表只保存 credential_ref，绝不保存明文密码。

文件状态语义：
- 文件不存在＝空存储，允许首次初始化写入；
- 合法空 JSON 对象 {}＝空存储；
- 已存在的零字节、纯空白、非法 JSON、非对象结构、键或值非字符串的文件
  一律视为损坏：读/写/删都抛 CredentialStoreError，绝不修改或自动修复。

所有预期的文件系统/锁/编码故障统一转换为 CredentialStoreError（固定安全消息，
仅含异常类型名与路径，不含文件内容和底层异常文本），由 API 层返回 503。
"""

import fcntl
import json
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.exceptions import CredentialStoreError


class CredentialStore(ABC):
    """凭证存储抽象。生产环境接入密钥管理服务，不保存明文到元数据表。"""

    @abstractmethod
    def put(self, credential_id: str, secret: str) -> None:
        """保存凭证；不得把 secret 写入日志。"""

    @abstractmethod
    def get(self, credential_id: str) -> str | None:
        """读取凭证；校验调用方权限后读取，不得在日志中输出 secret。"""

    @abstractmethod
    def delete(self, credential_id: str) -> bool:
        """删除凭证；返回是否确实删除了记录。"""


class FileCredentialStore(CredentialStore):
    """本地开发用凭证文件存储。

    写路径：独占 flock → 锁内重读最新文件 → 合并修改 → 同目录临时文件
    写入并 fsync → os.replace 原子替换。读路径：共享 flock + 每次重读文件，
    因此能看到其他实例/进程已提交的更新。文件损坏时显式报错，绝不覆盖。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._thread_lock = threading.RLock()

    # -- 锁与读写 -----------------------------------------------------------

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        with self._thread_lock:
            try:
                self._ensure_parent_dir()
                fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                raise CredentialStoreError(
                    f"凭证锁文件无法打开（{type(exc).__name__}）：{self._lock_path}"
                ) from None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            except OSError as exc:
                os.close(fd)
                raise CredentialStoreError(
                    f"凭证文件锁获取失败（{type(exc).__name__}）：{self._lock_path}"
                ) from None
            try:
                yield
            finally:
                os.close(fd)

    def _ensure_parent_dir(self) -> None:
        # 逐级创建缺失目录，保证每一级都是 0700；已存在的目录不改权限。
        missing: list[Path] = []
        current = self._path.parent
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except OSError as exc:
                raise CredentialStoreError(
                    f"凭证目录无法创建（{type(exc).__name__}）：{directory}"
                ) from None

    def _read_latest(self) -> dict[str, str]:
        if not self._path.exists():
            return {}  # 仅“文件不存在”视为空存储，允许首次初始化
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"凭证文件读取失败（{type(exc).__name__}）：{self._path}"
            ) from None
        if not raw.strip():
            raise CredentialStoreError(
                f"凭证文件为空或仅含空白，拒绝读取或覆盖：{self._path}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CredentialStoreError(
                f"凭证文件损坏（非法 JSON），拒绝读取或覆盖：{self._path}"
            ) from None
        if not isinstance(data, dict):
            raise CredentialStoreError("凭证文件格式不正确（顶层应为 JSON 对象）")
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                # 不把非法结构 str() 后当密码使用。
                raise CredentialStoreError(
                    "凭证文件结构不合法（键和值必须是字符串），拒绝读取或覆盖"
                )
        return dict(data)

    def _write_atomic(self, data: dict[str, str]) -> None:
        tmp = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            # 固定安全消息：仅类型名与路径；不落底层文本（可能含环境信息）。
            raise CredentialStoreError(
                f"凭证文件写入失败（{type(exc).__name__}）：{self._path}"
            ) from None
        finally:
            try:
                tmp.unlink(missing_ok=True)  # 清理失败不得覆盖主要错误
            except OSError:
                pass

    # -- CredentialStore 接口 ------------------------------------------------

    def put(self, credential_id: str, secret: str) -> None:
        with self._locked(exclusive=True):
            data = self._read_latest()  # 锁内重读，避免过期缓存覆盖他方写入
            data[credential_id] = secret
            self._write_atomic(data)

    def get(self, credential_id: str) -> str | None:
        with self._locked(exclusive=False):
            return self._read_latest().get(credential_id)

    def delete(self, credential_id: str) -> bool:
        with self._locked(exclusive=True):
            data = self._read_latest()
            if credential_id not in data:
                return False
            del data[credential_id]
            self._write_atomic(data)
            return True

    def __repr__(self) -> str:  # pragma: no cover - 防御性实现
        return f"<FileCredentialStore path={self._path!r}>"
