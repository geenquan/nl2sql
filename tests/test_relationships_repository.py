"""M3 关系仓储测试：修订追加、乐观锁、原子批写与有效解析。"""

import threading

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.exceptions import RevisionConflictError
from tests.relationship_fixtures import synthetic_catalog


def _record(key="rel-1", revision_free=True, **overrides):
    base = {
        "relationship_key": key,
        "source_id": "src-m3",
        "snapshot_id": "snap-m3",
        "status": "candidate",
        "inference_source": "rules",
        "source_schema": "app", "source_table": "booking",
        "source_columns": ["guest_id"],
        "target_schema": "app", "target_table": "guest",
        "target_columns": ["id"],
        "scores": {"total_weighted": 0.8},
        "created_at": "2026-10-28T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def repo(tmp_path):
    return SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")


class TestRevisionAppend:
    def test_save_appends_revision(self, repo):
        assert repo.save_relationship(_record()) == 1
        assert repo.save_relationship(_record(status="validated",
                                              inference_source="system")) == 2
        history = repo.get_relationship_history("rel-1")
        assert [r["revision"] for r in history] == [2, 1]

    def test_effective_follows_resolution_rules(self, repo):
        repo.save_relationship(_record())
        repo.save_relationship(_record(status="validated", inference_source="human"))
        repo.save_relationship(_record(status="candidate"))  # 机器不覆盖人工
        effective = repo.get_effective_relationship("rel-1")
        assert effective["status"] == "validated"
        assert effective["inference_source"] == "human"

    def test_optimistic_lock_conflict(self, repo):
        repo.save_relationship(_record())
        # 基于最新修订 1 提交 → 成功推进到 2
        assert repo.save_relationship(
            _record(status="validated"), expected_revision=1) == 2
        # 再基于过期修订 1 提交 → 409，历史不被污染
        with pytest.raises(RevisionConflictError):
            repo.save_relationship(_record(status="stale"), expected_revision=1)
        assert repo.get_relationship_history("rel-1")[0]["revision"] == 2

    def test_concurrent_same_revision_exactly_one_wins(self, repo):
        repo.save_relationship(_record())
        barrier = threading.Barrier(2)
        results: list[int | Exception] = []

        def submit():
            barrier.wait()
            try:
                results.append(repo.save_relationship(
                    _record(status="validated", inference_source="human"),
                    expected_revision=1))
            except Exception as exc:  # noqa: BLE001 - 线程内收集结果
                results.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [r for r in results if isinstance(r, int)]
        conflicts = [r for r in results if isinstance(r, Exception)]
        assert len(ok) == 1 and ok[0] == 2
        assert len(conflicts) == 1 and isinstance(conflicts[0], RevisionConflictError)

    def test_list_relationships_across_snapshots_keeps_human(self, repo):
        repo.save_relationship(_record(key="rel-a", snapshot_id="snap-1",
                                       status="validated", inference_source="human"))
        repo.save_relationship(_record(key="rel-a", snapshot_id="snap-2",
                                       status="candidate"))
        listed = repo.list_relationships("src-m3")
        assert len(listed) == 1  # 同一逻辑键只出一条有效结果
        assert listed[0]["status"] == "validated"  # 人工不被新快照机器修订覆盖
        assert listed[0]["snapshot_id"] == "snap-1"
        snap2 = repo.list_relationships("src-m3", snapshot_id="snap-2")
        assert snap2 == []  # snap-2 的机器修订不是有效结果


class TestBatchAtomicity:
    def test_batch_all_or_nothing(self, repo):
        records = [_record(key="rel-a"), _record(key="rel-b")]
        run = {"run_id": "run-1", "source_id": "src-m3", "run_type": "discovery",
               "status": "succeeded", "started_at": "2026-10-28T00:00:00+00:00"}
        repo.save_relationship_batch(records, run)
        assert len(repo.list_relationships("src-m3", effective_only=False)) == 2
        assert repo.get_relationship_run("run-1")["status"] == "succeeded"

    def test_failed_batch_leaves_no_residue(self, repo):
        # 第二条记录缺字段 → 整体回滚：零残留（沿用 M2 发布语义）
        run = {"run_id": "run-2", "source_id": "src-m3", "run_type": "validation",
               "status": "succeeded", "started_at": "2026-10-28T00:00:00+00:00"}
        with pytest.raises(KeyError):
            repo.save_relationship_batch(
                [_record(key="rel-c"), {"relationship_key": "rel-d"}], run)
        assert repo.list_relationships("src-m3", effective_only=False) == []
        assert repo.get_relationship_run("run-2") is None


class TestIdempotentCatalog:
    def test_catalog_fixture_is_consistent(self):
        catalog = synthetic_catalog()
        names = {t["table_name"] for t in catalog}
        assert {"booking", "guest", "tenant", "booking_item", "payment",
                "employee", "room", "room_type", "service_item"} <= names
        for table in catalog:
            assert table["snapshot_id"] == "snap-m3"
