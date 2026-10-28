"""M2 语义服务测试：规则/模型模式、修订审核、检索与快照隔离（全部离线）。"""

from typing import Any

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.core.exceptions import LLMRequestError, ResourceNotFoundError, RevisionConflictError
from app.ports.llm import LLMStructuredOutput, LLMUsage
from app.services.semantics_service import SemanticsService

FAKE_TABLE_META = {
    "table_id": "tbl-1",
    "source_id": "src-1",
    "snapshot_id": "snap-1",
    "schema_name": "sales",
    "table_name": "orders",
    "table_comment": "订单主表",
    "columns": [
        {"name": "id", "data_type": "bigint", "nullable": False, "comment": "主键"},
        {"name": "customer_id", "data_type": "bigint", "nullable": True, "comment": "客户ID"},
        {"name": "total_amount", "data_type": "decimal(10,2)", "nullable": True, "comment": "金额"},
    ],
    "primary_key": ["id"],
    "foreign_keys": [{"name": "fk", "columns": ["customer_id"], "ref_table": "customers",
                      "ref_columns": ["id"]}],
    "indexes": [],
    "row_count_estimate": 3,
    "row_count_is_estimate": True,
}


class StubRepo(SQLiteMetadataRepository):
    """在真实 SQLite 仓储上注入固定表元数据，隔离 M1 扫描依赖。"""

    def __init__(self, database_url: str, table: dict[str, Any], profiles: list[dict]) -> None:
        super().__init__(database_url)
        self._stub_table = table
        self._stub_profiles = profiles

    def get_table_metadata(self, table_id: str) -> dict[str, Any] | None:
        if table_id == self._stub_table["table_id"]:
            return dict(self._stub_table)
        return super().get_table_metadata(table_id)

    def list_column_profiles(self, table_id: str) -> list[dict[str, Any]]:
        if table_id == self._stub_table["table_id"]:
            return [dict(p) for p in self._stub_profiles]
        return super().list_column_profiles(table_id)


PROFILES = [
    {"column_name": "id", "null_ratio": 0.0, "distinct_count_estimate": 3,
     "uniqueness_ratio": 1.0, "metric_basis": "exact", "sample_row_count": 3,
     "row_count_basis": 3, "sample_values": ["1", "2", "3"], "samples_redacted": False},
    {"column_name": "customer_id", "null_ratio": 0.0, "distinct_count_estimate": 2,
     "uniqueness_ratio": 0.66, "metric_basis": "exact", "sample_row_count": 3,
     "row_count_basis": 3, "sample_values": ["***"], "samples_redacted": True},
    {"column_name": "total_amount", "null_ratio": 0.0, "distinct_count_estimate": 3,
     "uniqueness_ratio": 1.0, "metric_basis": "exact", "sample_row_count": 3,
     "row_count_basis": 3, "sample_values": ["10.00", "20.00"], "samples_redacted": False},
]


class FakeLLM:
    """可编程假模型：按脚本依次返回结果；记录收到的提示词。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[tuple[str, str]] = []
        self.calls = 0

    def generate_structured(self, system: str, user: str, output_schema: dict,
                            *, max_output_chars: int | None = None) -> LLMStructuredOutput:
        self.calls += 1
        self.prompts.append((system, user))
        step = self.script.pop(0) if self.script else {"columns": []}
        if isinstance(step, Exception):
            raise step
        return LLMStructuredOutput(
            result=step,
            usage=LLMUsage(model_reported="deepseek-fake", prompt_tokens=100,
                           completion_tokens=50, calls=1, elapsed_ms=12),
        )


def valid_model_output() -> dict:
    return {
        "columns": [
            {"column_name": "id", "category": "entity_identifier", "confidence": "high",
             "business_name": "订单ID", "candidate_entities": ["order"],
             "evidence": [{"type": "primary_key", "ref": "id"}]},
            {"column_name": "customer_id", "category": "foreign_identifier_candidate",
             "confidence": "medium", "business_name": "客户标识",
             "candidate_entities": ["customer"],
             "evidence": [{"type": "foreign_key", "ref": "customer_id"}]},
            {"column_name": "total_amount", "category": "amount", "confidence": "high",
             "business_name": "订单金额", "candidate_entities": [],
             "evidence": [{"type": "comment", "ref": "金额"}]},
        ],
        "table_purpose": "transaction_event",
    }


def make_service(tmp_path, llm: Any = None, settings: Settings | None = None,
                 table: dict | None = None) -> tuple[SemanticsService, SQLiteMetadataRepository]:
    settings = settings or Settings(_env_file=None)
    repo = StubRepo(f"sqlite:///{tmp_path/'meta.db'}", table or FAKE_TABLE_META, PROFILES)
    # 建立发布快照：succeeded 任务 + 表元数据行
    repo.save_scan_job({"job_id": "job-1", "source_id": "src-1", "snapshot_id": "snap-1",
                        "status": "succeeded", "started_at": "2026-10-28T01:00:00+00:00"})
    repo.save_table_metadata({
        "source_id": "src-1", "snapshot_id": "snap-1", "schema_name": "sales",
        "table_name": "orders", "columns": FAKE_TABLE_META["columns"],
        "primary_key": ["id"],
        "foreign_keys": FAKE_TABLE_META["foreign_keys"], "indexes": [],
        "row_count_estimate": 3, "row_count_is_estimate": True, "scanned_at": "t",
    })
    llm_factory = (lambda: llm) if llm is not None else None
    service = SemanticsService(
        metadata_repository=repo,
        llm_factory=llm_factory,
        settings=settings,
    )
    return service, repo


class TestRulesOnlyMode:
    def test_analyze_persists_semantics_and_purpose(self, tmp_path):
        service, repo = make_service(tmp_path)  # 未启用模型 → rules_only
        run = service.analyze_table("src-1", "tbl-1")
        assert run["mode"] == "rules_only"
        assert run["status"] == "succeeded"
        assert run["columns_analyzed"] == 3
        assert run["snapshot_id"] == "snap-1"
        stored = repo.list_field_semantics("tbl-1")
        by_col = {s["column_name"]: s for s in stored}
        assert by_col["id"]["category"] == "entity_identifier"
        assert by_col["total_amount"]["category"] == "amount"
        assert all(s["inference_source"] == "rules" for s in stored)
        purpose = repo.get_table_purpose("tbl-1")
        assert purpose["purpose"] in ("transaction_event", "unknown")
        run_loaded = repo.get_semantic_run(run["run_id"])
        assert run_loaded["status"] == "succeeded"

    def test_semantics_bound_to_snapshot(self, tmp_path):
        service, repo = make_service(tmp_path)
        run = service.analyze_table("src-1", "tbl-1")
        stored = repo.list_field_semantics("tbl-1")
        assert all(s["snapshot_id"] == "snap-1" for s in stored)
        assert all(s["run_id"] == run["run_id"] for s in stored)

    def test_missing_table_or_snapshot_fails(self, tmp_path):
        service, _repo = make_service(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            service.analyze_table("src-1", "nope")
        # 无发布快照
        repo2 = StubRepo(f"sqlite:///{tmp_path/'m2.db'}", FAKE_TABLE_META, PROFILES)
        service2 = SemanticsService(metadata_repository=repo2,
                                    llm_factory=None, settings=Settings(_env_file=None))
        with pytest.raises(ResourceNotFoundError):
            service2.analyze_table("src-1", "tbl-1")

    def test_partial_published_snapshot_requires_flag(self, tmp_path):
        settings = Settings(_env_file=None)
        repo = StubRepo(f"sqlite:///{tmp_path/'meta.db'}", FAKE_TABLE_META, PROFILES)
        repo.save_scan_job({"job_id": "j", "source_id": "src-1", "snapshot_id": "snap-1",
                            "status": "partial", "tables_scanned": 1,
                            "started_at": "2026-10-28T01:00:00+00:00"})
        service = SemanticsService(metadata_repository=repo, llm_factory=None, settings=settings)
        with pytest.raises(ValueError, match="不完整"):
            service.analyze_table("src-1", "tbl-1")
        run = service.analyze_table("src-1", "tbl-1", allow_partial=True)
        assert run["input_complete"] is False

    def test_column_budget_truncation_recorded(self, tmp_path):
        settings = Settings(_env_file=None, semantics_max_columns_per_run=2)
        service, _repo = make_service(tmp_path, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["columns_analyzed"] == 2
        assert any(e["reason"] == "budget_truncated" for e in run["errors"])


class TestModelAssistedMode:
    def test_valid_model_output_persisted_with_usage(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True)
        service, repo = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["mode"] == "model_assisted"
        assert run["status"] == "succeeded"
        assert run["model_reported"] == "deepseek-fake"
        assert run["llm_prompt_tokens"] == 100
        assert run["llm_calls"] == 1
        by_col = {s["column_name"]: s for s in repo.list_field_semantics("tbl-1")}
        assert by_col["id"]["category"] == "entity_identifier"
        assert by_col["id"]["inference_source"] == "model"
        assert by_col["id"]["model_name"] == "deepseek-fake"
        assert by_col["total_amount"]["suggested_business_name"] == "订单金额"

    def test_model_input_has_no_samples_or_secrets(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True)
        service, _ = make_service(tmp_path, llm=llm, settings=settings)
        service.analyze_table("src-1", "tbl-1")
        system, user = llm.prompts[0]
        combined = system + user
        for forbidden in ("10.00", "20.00", "sample_values", "password", "credential"):
            assert forbidden not in combined, forbidden

    def test_send_samples_optin_only_and_sensitive_excluded(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True, semantics_send_samples=True)
        service, _ = make_service(tmp_path, llm=llm, settings=settings)
        service.analyze_table("src-1", "tbl-1", send_samples=True)
        user = llm.prompts[0][1]
        assert "10.00" in user  # 非敏感样本按开关外发
        llm2 = FakeLLM([valid_model_output()])
        (tmp_path / "x").mkdir()
        service2, _ = make_service(tmp_path / "x", llm=llm2, settings=settings)
        service2.analyze_table("src-1", "tbl-1")  # 请求未开启
        assert "10.00" not in llm2.prompts[0][1]
        # 配置关闭时请求开启也无效
        settings_off = Settings(_env_file=None, deepseek_enabled=True, semantics_send_samples=False)
        llm3 = FakeLLM([valid_model_output()])
        (tmp_path / "y").mkdir()
        service3, _ = make_service(tmp_path / "y", llm=llm3, settings=settings_off)
        service3.analyze_table("src-1", "tbl-1", send_samples=True)
        assert "10.00" not in llm3.prompts[0][1]

    def test_invalid_then_repaired_within_call_budget(self, tmp_path):
        bad = {"columns": [{"column_name": "ghost", "category": "name", "confidence": "low",
                            "evidence": []}]}
        llm = FakeLLM([bad, valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True)
        service, _repo = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "succeeded"
        assert llm.calls == 2  # 一次修复

    def test_persistently_invalid_model_falls_back_to_rules(self, tmp_path):
        bad = {"columns": [{"column_name": "ghost", "category": "name", "confidence": "low",
                            "evidence": []}]}
        llm = FakeLLM([bad, bad])
        settings = Settings(_env_file=None, deepseek_enabled=True, semantics_llm_max_calls=2)
        service, repo = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "partial"
        assert any(e["reason"] == "model_validation_failed" for e in run["errors"])
        stored = repo.list_field_semantics("tbl-1")
        assert all(s["inference_source"] == "rules" for s in stored)  # 不伪造模型语义
        assert run["model_reported"] is None or run["mode"] == "model_assisted"

    def test_llm_runtime_failure_falls_back_to_rules(self, tmp_path):
        llm = FakeLLM([LLMRequestError("timeout", "模型请求超时")])
        settings = Settings(_env_file=None, deepseek_enabled=True)
        service, repo = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "partial"
        assert any(e["reason"] == "model_request_failed:timeout" for e in run["errors"])
        assert repo.list_field_semantics("tbl-1")  # 规则结果保留
        assert repo.get_semantic_run(run["run_id"])["status"] == "partial"  # 不停留 running

    def test_model_does_not_overwrite_confirmed_conclusion(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True)
        service, repo = make_service(tmp_path, llm=llm, settings=settings)
        service.analyze_table("src-1", "tbl-1")
        service.submit_review("tbl-1", "total_amount", action="confirm",
                              patch={"business_name": "订单总金额（人工）"}, reviewer="dev",
                              expected_revision=1)
        # 新一轮模型建议（改判 unknown）
        new_output = valid_model_output()
        new_output["columns"][2] = {"column_name": "total_amount", "category": "unknown",
                                    "confidence": "low", "evidence": []}
        llm2 = FakeLLM([new_output])
        (tmp_path / "z").mkdir()
        service2, _ = make_service(tmp_path / "z", llm=llm2, settings=settings)
        service2.analyze_table("src-1", "tbl-1")
        effective = {s["column_name"]: s for s in repo.list_field_semantics("tbl-1", effective=True)}
        assert effective["total_amount"]["review_status"] == "business_confirmed"
        assert effective["total_amount"]["suggested_business_name"] == "订单总金额（人工）"


class TestHumanReview:
    def _prepared(self, tmp_path):
        service, repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        return service, repo

    def test_confirm_creates_human_revision_with_history(self, tmp_path):
        service, repo = self._prepared(tmp_path)
        revision = service.submit_review(
            "tbl-1", "id", action="confirm",
            patch={"business_name": "订单主键", "candidate_entities": ["order"]},
            reviewer="dev", expected_revision=1, note="业务确认")
        assert revision == 2
        history = repo.get_field_semantics_history("tbl-1", "id")
        assert [r["review_status"] for r in history] == ["business_confirmed", "suggested"]
        assert history[0]["inference_source"] == "human"
        assert history[0]["reviewed_by"] == "dev"

    def test_stale_revision_conflict(self, tmp_path):
        service, _ = self._prepared(tmp_path)
        with pytest.raises(RevisionConflictError):
            service.submit_review("tbl-1", "id", action="confirm", patch={},
                                  reviewer="dev", expected_revision=99)

    def test_invalid_transition_and_category(self, tmp_path):
        service, _ = self._prepared(tmp_path)
        service.submit_review("tbl-1", "id", action="confirm", patch={},
                              reviewer="dev", expected_revision=1)
        with pytest.raises(ValueError):  # confirmed 只能回退 suggested
            service.submit_review("tbl-1", "id", action="confirm", patch={},
                                  reviewer="dev", expected_revision=2)
        with pytest.raises(ValueError):  # 非法类别
            service.submit_review("tbl-1", "customer_id", action="reject",
                                  patch={"category": "hotel_room"}, reviewer="dev",
                                  expected_revision=1)

    def test_purpose_review(self, tmp_path):
        service, repo = self._prepared(tmp_path)
        revision = service.submit_purpose_review(
            "tbl-1", action="confirm", patch={"purpose": "transaction_event"},
            reviewer="dev", expected_revision=1)
        assert revision == 2
        assert repo.get_table_purpose("tbl-1")["review_status"] == "business_confirmed"

    def test_reviewer_is_local_dev_input_only(self, tmp_path):
        service, repo = self._prepared(tmp_path)
        service.submit_review("tbl-1", "id", action="confirm", patch={},
                              reviewer="anyone", expected_revision=1)
        record = repo.get_field_semantics_history("tbl-1", "id")[0]
        assert record["review_note"] != "身份已认证"  # 不宣称身份认证


class TestSearch:
    def test_search_by_confirmed_business_name(self, tmp_path):
        service, _repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        service.submit_review("tbl-1", "total_amount", action="confirm",
                              patch={"business_name": "订单总金额"}, reviewer="dev",
                              expected_revision=1)
        results = service.search_fields("src-1", "订单总金额")
        assert results["method"] == "lexical"
        assert results["snapshot_id"] == "snap-1"
        assert results["items"]
        top = results["items"][0]
        assert top["column_name"] == "total_amount"
        assert top["review_status"] == "business_confirmed"
        assert top["match_basis"]

    def test_search_scoped_to_source_and_snapshot(self, tmp_path):
        service, _ = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        results = service.search_fields("src-1", "客户")
        assert all(item["source_id"] == "src-1" for item in results["items"])
        empty = service.search_fields("src-1", "不存在的概念xyzq")
        assert empty["items"] == []

    def test_search_limited(self, tmp_path):
        service, _ = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        results = service.search_fields("src-1", "id", limit=1)
        assert len(results["items"]) <= 1
