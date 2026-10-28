"""M2 语义持久化：修订历史、有效语义、运行记录。"""

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.ports.repositories import MetadataRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteMetadataRepository:
    return SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")


def semantics(table_id="t1", column="amount", revision=None, **overrides) -> dict:
    record = {
        "source_id": "s1", "snapshot_id": "snap1", "table_id": table_id,
        "column_name": column, "original_name": column, "original_comment": "金额",
        "suggested_business_name": "金额", "category": "amount",
        "candidate_entities": [], "alternatives": [], "evidence": [],
        "confidence": "medium", "inference_source": "rules", "model_name": None,
        "prompt_version": None, "rules_version": "rules-v1", "run_id": "run1",
        "review_status": "suggested", "reviewed_by": None, "review_note": None,
    }
    if revision is not None:
        record["revision"] = revision
    record.update(overrides)
    return record


class TestFieldSemantics:
    def test_save_appends_new_revision_and_lists_latest(self, repo) -> None:
        first = repo.save_field_semantics(semantics())
        assert first == 1
        second = repo.save_field_semantics(
            semantics(category="unknown", run_id="run2"))
        assert second == 2  # 追加修订，不覆盖审计历史
        rows = repo.get_field_semantics_history("t1", "amount")
        assert [r["revision"] for r in rows] == [2, 1]  # 倒序
        latest = repo.list_field_semantics("t1")
        assert len(latest) == 1
        assert latest[0]["revision"] == 2

    def test_confirmed_revision_is_effective_over_newer_suggestion(self, repo) -> None:
        repo.save_field_semantics(semantics())
        repo.save_field_semantics(
            semantics(revision=None, review_status="business_confirmed",
                      inference_source="human", category="amount", reviewed_by="dev"))
        # 新一轮模型建议（revision 3）不得覆盖人工确认
        repo.save_field_semantics(semantics(run_id="run3", category="unknown"))
        effective = {c["column_name"]: c for c in repo.list_field_semantics("t1", effective=True)}
        assert effective["amount"]["review_status"] == "business_confirmed"
        assert effective["amount"]["inference_source"] == "human"
        # 默认视图展示最新修订（模型建议），历史完整保留
        latest = {c["column_name"]: c for c in repo.list_field_semantics("t1")}
        assert latest["amount"]["revision"] == 3
        assert latest["amount"]["inference_source"] == "rules"

    def test_multiple_columns(self, repo) -> None:
        repo.save_field_semantics(semantics(column="a"))
        repo.save_field_semantics(semantics(column="b"))
        latest = {c["column_name"] for c in repo.list_field_semantics("t1")}
        assert latest == {"a", "b"}


class TestTablePurpose:
    def test_save_and_get_with_history(self, repo) -> None:
        record = {
            "source_id": "s1", "snapshot_id": "snap1", "table_id": "t1",
            "table_name": "orders", "purpose": "transaction_event", "rationale": "命名",
            "alternatives": [], "evidence": [], "confidence": "medium",
            "inference_source": "rules", "rules_version": "rules-v1", "run_id": "run1",
            "review_status": "suggested",
        }
        assert repo.save_table_purpose(record) == 1
        record2 = {**record, "purpose": "master_data",
                   "review_status": "business_confirmed", "inference_source": "human"}
        assert repo.save_table_purpose(record2) == 2
        current = repo.get_table_purpose("t1")
        assert current["purpose"] == "master_data"
        confirmed = repo.get_table_purpose("t1", effective=True)
        assert confirmed["review_status"] == "business_confirmed"
        history = repo.get_table_purpose_history("t1")
        assert [r["revision"] for r in history] == [2, 1]

    def test_get_missing_returns_none(self, repo) -> None:
        assert repo.get_table_purpose("nope") is None


class TestSemanticRuns:
    def test_run_upsert(self, repo) -> None:
        run = {"run_id": "r1", "source_id": "s1", "snapshot_id": "snap1",
               "mode": "rules_only", "status": "running", "started_at": "t0",
               "table_ids": ["t1"], "errors": []}
        repo.save_semantic_run(run)
        run["status"] = "succeeded"
        run["finished_at"] = "t1"
        run["columns_analyzed"] = 3
        repo.save_semantic_run(run)
        stored = repo.get_semantic_run("r1")
        assert stored["status"] == "succeeded"
        assert stored["columns_analyzed"] == 3
        assert repo.get_semantic_run("missing") is None
        listed = repo.list_semantic_runs("s1")
        assert len(listed) == 1


def test_repo_still_implements_port(repo) -> None:
    assert isinstance(repo, MetadataRepository)


class TestCrossTableSameNameColumns:
    """跨表同名字段不得合并（修复轮：按 (table_id, column_name) 分组）。"""

    def _seed(self, repo, source="s", snapshot="snap") -> None:
        for table_id in ("t1", "t2", "t3"):
            for column in ("id", "status", "name"):
                repo.save_field_semantics(semantics(
                    source_id=source, snapshot_id=snapshot, table_id=table_id,
                    column=column, revision=None))

    def test_three_tables_same_columns_all_kept(self, repo) -> None:
        self._seed(repo)
        rows = repo.list_field_semantics_by_source("s", "snap", effective=True)
        assert len(rows) == 9  # 3 表 × 3 列，全部保留
        pairs = {(r["table_id"], r["column_name"]) for r in rows}
        assert ("t2", "id") in pairs and ("t3", "status") in pairs

    def test_confirmation_only_affects_its_own_field(self, repo) -> None:
        self._seed(repo)
        # t2.id 人工确认（修订 2）
        repo.save_field_semantics(semantics(
            source_id="s", snapshot_id="snap", table_id="t2", column="id",
            review_status="business_confirmed",
            inference_source="human"), expected_revision=1)
        rows = repo.list_field_semantics_by_source("s", "snap", effective=True)
        by_key = {(r["table_id"], r["column_name"]): r for r in rows}
        assert by_key[("t2", "id")]["review_status"] == "business_confirmed"
        assert by_key[("t1", "id")]["review_status"] == "suggested"  # 不受影响
        assert by_key[("t3", "id")]["review_status"] == "suggested"

    def test_no_cross_source_or_snapshot_mixing(self, repo) -> None:
        self._seed(repo, source="s1", snapshot="snap1")
        self._seed(repo, source="s2", snapshot="snap1")
        self._seed(repo, source="s1", snapshot="snap2")
        assert len(repo.list_field_semantics_by_source("s1", "snap1")) == 9
        assert len(repo.list_field_semantics_by_source("s2", "snap1")) == 9
        assert len(repo.list_field_semantics_by_source("s1", "snap2")) == 9


class TestAtomicRevisionControl:
    def test_concurrent_same_expected_only_one_wins(self, repo) -> None:
        import threading

        from app.core.exceptions import RevisionConflictError

        repo.save_field_semantics(semantics(table_id="t1", column="id"))
        results: list[str] = []
        barrier = threading.Barrier(2)

        def submit() -> None:
            barrier.wait()
            try:
                repo.save_field_semantics(
                    semantics(table_id="t1", column="id", review_status="business_confirmed",
                              inference_source="human"),
                    expected_revision=1)
                results.append("ok")
            except RevisionConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["conflict", "ok"]  # 恰一个成功
        history = repo.get_field_semantics_history("t1", "id")
        assert len(history) == 2  # 只产生一个新修订

    def test_purpose_concurrent_conflict(self, repo) -> None:
        from app.core.exceptions import RevisionConflictError

        record = {"source_id": "s", "snapshot_id": "snap", "table_id": "t1",
                  "table_name": "t", "purpose": "unknown", "inference_source": "rules",
                  "review_status": "suggested"}
        repo.save_table_purpose(record)
        record2 = {**record, "purpose": "master_data", "review_status": "business_confirmed",
                   "inference_source": "human"}
        repo.save_table_purpose(record2, expected_revision=1)
        with __import__("pytest").raises(RevisionConflictError):
            repo.save_table_purpose(record2, expected_revision=1)  # 过期版本


class TestEffectiveSelectionWithRevoke:
    def _history(self, repo, statuses_sources) -> list[dict]:
        for status, source in statuses_sources:
            repo.save_field_semantics(semantics(
                table_id="t1", column="amount", review_status=status,
                inference_source=source))
        return repo.get_field_semantics_history("t1", "amount")

    def test_confirm_then_model_keeps_confirmation(self, repo) -> None:
        self._history(repo, [("suggested", "rules"), ("business_confirmed", "human"),
                             ("suggested", "model")])
        effective = repo.list_field_semantics("t1", effective=True)[0]
        assert effective["review_status"] == "business_confirmed"

    def test_confirm_then_revoke_terminates_confirmation(self, repo) -> None:
        self._history(repo, [("suggested", "rules"), ("business_confirmed", "human"),
                             ("suggested", "human")])  # revert → suggested + human
        effective = repo.list_field_semantics("t1", effective=True)[0]
        assert effective["review_status"] == "suggested"
        assert effective["inference_source"] == "human"

    def test_revoke_then_reconfirm(self, repo) -> None:
        self._history(repo, [("suggested", "rules"), ("business_confirmed", "human"),
                             ("suggested", "human"), ("business_confirmed", "human")])
        effective = repo.list_field_semantics("t1", effective=True)[0]
        assert effective["review_status"] == "business_confirmed"
        assert effective["revision"] == 4

    def test_purpose_revoke_semantics(self, repo) -> None:
        base = {"source_id": "s", "snapshot_id": "snap", "table_id": "t1",
                "table_name": "t", "purpose": "unknown", "review_status": "suggested",
                "inference_source": "rules"}
        repo.save_table_purpose(base)
        repo.save_table_purpose({**base, "purpose": "master_data",
                                 "review_status": "business_confirmed",
                                 "inference_source": "human"})
        repo.save_table_purpose({**base, "purpose": "master_data",
                                 "review_status": "suggested",
                                 "inference_source": "human"})  # revert
        effective = repo.get_table_purpose("t1", effective=True)
        assert effective["review_status"] == "suggested"
        assert effective["revision"] == 3


class TestBatchTransaction:
    def test_batch_all_or_nothing_on_failure(self, repo, monkeypatch) -> None:
        import app.adapters.repositories.sqlite_metadata_repository as repo_mod

        repo.save_field_semantics(semantics(table_id="t1", column="id"))
        original = repo_mod._new_id
        calls = {"n": 0}

        def flaky_new_id() -> str:
            calls["n"] += 1
            if calls["n"] == 2:  # 第二条字段写入中途失败
                raise RuntimeError("simulated mid-batch failure")
            return original()

        monkeypatch.setattr(repo_mod, "_new_id", flaky_new_id)
        run = {"run_id": "r1", "source_id": "s", "snapshot_id": "snap",
               "status": "succeeded", "started_at": "t0"}
        with __import__("pytest").raises(RuntimeError):
            repo.save_semantics_batch(
                [semantics(table_id="t1", column="id"),
                 semantics(table_id="t1", column="status")],
                None, run)
        monkeypatch.undo()
        # 整体回滚：没有新修订、没有运行记录
        assert len(repo.get_field_semantics_history("t1", "id")) == 1
        assert repo.get_field_semantics_history("t1", "status") == []
        assert repo.get_semantic_run("r1") is None

    def test_batch_success_writes_fields_purpose_and_run(self, repo) -> None:
        purpose = {"source_id": "s", "snapshot_id": "snap", "table_id": "t1",
                   "table_name": "t", "purpose": "dictionary",
                   "inference_source": "rules", "review_status": "suggested"}
        run = {"run_id": "r2", "source_id": "s", "snapshot_id": "snap",
               "status": "succeeded", "started_at": "t0", "finished_at": "t1"}
        repo.save_semantics_batch(
            [semantics(table_id="t1", column="id"), semantics(table_id="t1", column="status")],
            purpose, run)
        assert len(repo.list_field_semantics("t1")) == 2
        assert repo.get_table_purpose("t1")["purpose"] == "dictionary"
        assert repo.get_semantic_run("r2")["status"] == "succeeded"
