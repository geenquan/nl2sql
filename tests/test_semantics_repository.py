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
