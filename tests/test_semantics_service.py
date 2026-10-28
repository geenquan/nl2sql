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
                            *, max_output_chars: int | None = None,
                            max_attempts: int | None = None,
                            deadline_monotonic: float | None = None) -> LLMStructuredOutput:
        self.calls += 1
        self.prompts.append((system, user))
        self.max_attempts_limits = getattr(self, "max_attempts_limits", [])
        self.max_attempts_limits.append(max_attempts)
        step = self.script.pop(0) if self.script else {"columns": []}
        if isinstance(step, Exception):
            raise step
        return LLMStructuredOutput(
            result=step,
            usage=LLMUsage(model_reported="deepseek-fake", model_configured="deepseek-test",
                           prompt_tokens=100, completion_tokens=50, calls=1,
                           http_attempts=1, elapsed_ms=12),
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
        "table_purpose": {
            "purpose": "transaction_event",
            "confidence": "medium",
            "rationale": "订单命名且含金额字段",
            "evidence": [{"type": "comment", "ref": "金额"}],
            "alternatives": [],
        },
    }


def make_service(tmp_path, llm: Any = None, settings: Settings | None = None,
                 table: dict | None = None,
                 profiles: list[dict] | None = None,
                 clock=None) -> tuple[SemanticsService, SQLiteMetadataRepository]:
    settings = settings or Settings(_env_file=None)
    repo = StubRepo(f"sqlite:///{tmp_path/'meta.db'}", table or FAKE_TABLE_META,
                    profiles if profiles is not None else PROFILES)
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
        clock=clock,
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

    def test_send_samples_optin_only_and_boundary_rules(self, tmp_path):
        from app.security.masking import MASKING_VERSION
        trusted_profiles = [dict(p, masking_version=MASKING_VERSION) for p in PROFILES]

        def build(path, profiles, send_enabled=True):
            settings = Settings(_env_file=None, deepseek_enabled=True,
                                semantics_send_samples=send_enabled)
            llm = FakeLLM([valid_model_output()])
            service, _ = make_service(path, llm=llm, settings=settings,
                                      profiles=profiles)
            return service, llm

        # 双侧开启 + 当前脱敏版本标记：非敏感样本外发
        service, llm = build(tmp_path, trusted_profiles)
        service.analyze_table("src-1", "tbl-1", send_samples=True)
        assert "10.00" in llm.prompts[0][1]

        # 历史样本（无版本标记）即使双侧开启也不外发
        (tmp_path / "h").mkdir(parents=True, exist_ok=True)
        service_h, llm_h = build(tmp_path / "h", PROFILES)
        service_h.analyze_table("src-1", "tbl-1", send_samples=True)
        assert "10.00" not in llm_h.prompts[0][1]

        # 请求未开启 → 不外发
        (tmp_path / "r").mkdir(parents=True, exist_ok=True)
        service_r, llm_r = build(tmp_path / "r", trusted_profiles)
        service_r.analyze_table("src-1", "tbl-1")
        assert "10.00" not in llm_r.prompts[0][1]

        # 配置关闭时请求开启也无效
        (tmp_path / "c").mkdir(parents=True, exist_ok=True)
        service_c, llm_c = build(tmp_path / "c", trusted_profiles, send_enabled=False)
        service_c.analyze_table("src-1", "tbl-1", send_samples=True)
        assert "10.00" not in llm_c.prompts[0][1]

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


class TestFixRoundRegressions:
    """修复轮回归：并发审核、失败运行不发布、预算与用量核算。"""

    def test_concurrent_field_review_only_one_wins(self, tmp_path):
        import threading

        from app.core.exceptions import RevisionConflictError

        service, repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        results = []
        barrier = threading.Barrier(2)

        def submit() -> None:
            barrier.wait()
            try:
                service.submit_review("tbl-1", "id", action="confirm", patch={},
                                      reviewer="dev", expected_revision=1)
                results.append("ok")
            except RevisionConflictError:
                results.append("conflict")
            except Exception as exc:
                results.append(f"unexpected:{type(exc).__name__}")

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["conflict", "ok"]
        history = repo.get_field_semantics_history("tbl-1", "id")
        assert len(history) == 2  # 一次成功修订，无重复覆盖

    def test_concurrent_purpose_review_conflict(self, tmp_path):
        import threading

        from app.core.exceptions import RevisionConflictError

        service, _repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        results = []
        barrier = threading.Barrier(2)

        def submit() -> None:
            barrier.wait()
            try:
                service.submit_purpose_review(
                    "tbl-1", action="confirm", patch={"purpose": "transaction_event"},
                    reviewer="dev", expected_revision=1)
                results.append("ok")
            except RevisionConflictError:
                results.append("conflict")
            except Exception as exc:  # 任何其他异常都视为失败（不得静默）
                results.append(f"unexpected:{type(exc).__name__}")

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["conflict", "ok"], results

    def test_model_write_interleaved_with_review_keeps_consistency(self, tmp_path):
        import threading

        service, repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")
        outcomes = []
        barrier = threading.Barrier(2)

        def review() -> None:
            barrier.wait()
            try:
                service.submit_review("tbl-1", "id", action="confirm", patch={},
                                      reviewer="dev", expected_revision=1)
                outcomes.append("review-ok")
            except Exception:
                outcomes.append("review-conflict")

        def reanalyze() -> None:
            barrier.wait()
            try:
                service.analyze_table("src-1", "tbl-1")
                outcomes.append("analyze-ok")
            except Exception:
                outcomes.append("analyze-failed")

        threads = [threading.Thread(target=review), threading.Thread(target=reanalyze)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "analyze-failed" not in outcomes
        history = repo.get_field_semantics_history("tbl-1", "id")
        revisions = [h["revision"] for h in history]
        assert revisions == sorted(revisions, reverse=True)  # 无重复修订号
        effective = {s["column_name"]: s for s in repo.list_field_semantics("tbl-1", effective=True)}
        if "review-ok" in outcomes and any(
            h["review_status"] == "business_confirmed" for h in history
        ):
            # 人工确认存在且其后无人工撤销 → 有效结果保持确认
            assert effective["id"]["review_status"] == "business_confirmed"

    def test_failed_run_publishes_nothing(self, tmp_path):
        service, repo = make_service(tmp_path)
        service.analyze_table("src-1", "tbl-1")  # 先有成功结果

        class FailingBatchRepo(type(repo)):
            def save_semantics_batch(self, field_records, purpose_record, run_record):
                raise RuntimeError("simulated batch failure")

        failing = FailingBatchRepo(f"sqlite:///{tmp_path/'meta.db'}",
                                   FAKE_TABLE_META, PROFILES)
        failing_service = SemanticsService(
            metadata_repository=failing, llm_factory=None,
            settings=Settings(_env_file=None))
        run = failing_service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "failed"
        assert run["finished_at"] is not None
        # 失败运行不产生任何新修订，原结果不受影响
        for column in ("id", "customer_id", "total_amount"):
            history = repo.get_field_semantics_history("tbl-1", column)
            assert all(h["run_id"] != run["run_id"] for h in history)
        effective = {s["column_name"]: s for s in repo.list_field_semantics("tbl-1", effective=True)}
        assert effective["id"]["category"] == "entity_identifier"
        search = service.search_fields("src-1", "id")
        assert search["items"]  # 检索结果不受失败运行影响

    def test_explicit_partial_snapshot_marks_input_incomplete(self, tmp_path):
        service, repo = make_service(tmp_path)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["input_complete"] is True
        # 显式 snapshot_id 指向部分成功任务 → input_complete=False
        repo.save_scan_job({"job_id": "job-p", "source_id": "src-1",
                            "snapshot_id": "snap-partial", "status": "partial",
                            "tables_scanned": 1,
                            "started_at": "2026-10-28T02:00:00+00:00"})
        real_id = repo.save_table_metadata({
            "source_id": "src-1", "snapshot_id": "snap-partial", "schema_name": "sales",
            "table_name": "orders_p", "columns": FAKE_TABLE_META["columns"],
            "primary_key": ["id"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 3, "row_count_is_estimate": True, "scanned_at": "t"})
        run2 = service.analyze_table("src-1", real_id, snapshot_id="snap-partial")
        assert run2["input_complete"] is False

    def test_llm_budget_shared_between_retries_and_repair(self, tmp_path):
        # 第一次逻辑调用：429 后重试成功但结构错误（消耗 2 次 HTTP）
        # 预算 2 → 修复无剩余预算 → 回退规则
        class AttemptedLLM(FakeLLM):
            def __init__(self):
                super().__init__([valid_model_output()])
                self.http_attempts = 2  # 模拟传输重试消耗

            def generate_structured(self, *args, **kwargs):
                out = super().generate_structured(*args, **kwargs)
                out.usage.http_attempts = 2
                # 返回结构不合格的结果（虚构字段）触发修复路径
                out.result = {"columns": [{"column_name": "ghost", "category": "name",
                                           "confidence": "low", "evidence": []}]}
                return out

        llm = AttemptedLLM()
        settings = Settings(_env_file=None, deepseek_enabled=True,
                            semantics_llm_max_calls=2)
        (tmp_path / "b").mkdir()
        service, _repo = make_service(tmp_path / "b", llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert llm.calls == 1  # 修复未被允许（预算耗尽）
        assert run["status"] == "partial"
        assert any(e["reason"] == "model_request_failed:budget_exceeded" for e in run["errors"])
        assert run["llm_http_attempts"] == 2

    def test_tokens_accumulated_across_invalid_then_valid(self, tmp_path):
        bad = {"columns": [{"column_name": "ghost", "category": "name",
                            "confidence": "low", "evidence": []}]}
        llm = FakeLLM([bad, valid_model_output()])
        settings = Settings(_env_file=None, deepseek_enabled=True,
                            semantics_llm_max_calls=3)
        service, _ = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "succeeded"
        assert run["llm_calls"] == 2  # 首次 + 修复
        assert run["llm_prompt_tokens"] == 200  # 两次响应累计（100×2）
        assert run["llm_completion_tokens"] == 100
        assert run["llm_usage_complete"] is True

    def test_partial_usage_flagged(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        llm.missing_usage = True
        settings = Settings(_env_file=None, deepseek_enabled=True)

        original = llm.generate_structured

        def patched(*args, **kwargs):
            out = original(*args, **kwargs)
            out.usage.prompt_tokens = None  # 服务端未提供
            return out

        llm.generate_structured = patched
        service, _ = make_service(tmp_path, llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["llm_usage_complete"] is False
        assert run["llm_prompt_tokens"] is None  # unavailable，不编造

    def test_repair_feedback_inflating_input_blocked(self, tmp_path):
        bad = {"columns": [{"column_name": "ghost", "category": "name",
                            "confidence": "low", "evidence": []}]}
        llm = FakeLLM([bad, bad])
        settings = Settings(_env_file=None, deepseek_enabled=True,
                            semantics_input_char_limit=10)  # 极小预算
        (tmp_path / "s").mkdir()
        service, _ = make_service(tmp_path / "s", llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "partial"
        reasons = {e["reason"] for e in run["errors"]}
        assert "input_budget_exceeded" in reasons
        assert llm.calls <= 1  # 输入超限后不再发起请求


class TestFixRound2Service:
    """服务层期限与分项用量完整性。"""

    def test_deadline_exceeded_after_response_not_published(self, tmp_path):
        # 注入时钟：模型返回后越界 → 结果不发布，回退规则并记录 deadline_exceeded
        clock = {"t": 100.0}
        llm = FakeLLM([valid_model_output()])

        def fake_clock() -> float:
            return clock["t"]

        original = llm.generate_structured

        def advancing(*args, **kwargs):
            out = original(*args, **kwargs)
            clock["t"] += 50.0  # 单次响应跨越期限
            return out

        llm.generate_structured = advancing
        settings = Settings(_env_file=None, deepseek_enabled=True,
                            semantics_llm_deadline_seconds=10)
        (tmp_path / "d").mkdir()
        service, repo = make_service(tmp_path / "d", llm=llm, settings=settings,
                                     clock=fake_clock)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["status"] == "partial"
        assert any(e["reason"] == "model_request_failed:deadline_exceeded"
                   for e in run["errors"])
        stored = repo.list_field_semantics("tbl-1")
        assert all(s["inference_source"] == "rules" for s in stored)  # 模型结果未发布
        assert run["llm_calls"] == 1  # 已发生请求如实计数

    def test_per_side_usage_completeness(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        original = llm.generate_structured

        def partial_usage(*args, **kwargs):
            out = original(*args, **kwargs)
            out.usage.completion_tokens = None  # 仅一侧缺失
            return out

        llm.generate_structured = partial_usage
        settings = Settings(_env_file=None, deepseek_enabled=True)
        (tmp_path / "p").mkdir()
        service, _ = make_service(tmp_path / "p", llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        assert run["llm_prompt_tokens"] == 100  # 已知一侧保留
        assert run["llm_prompt_usage_complete"] is True
        assert run["llm_completion_usage_complete"] is False  # 缺失侧如实标记
        assert run["llm_usage_complete"] is False

    def test_retried_call_marks_usage_incomplete_keeps_known(self, tmp_path):
        llm = FakeLLM([valid_model_output()])
        original = llm.generate_structured

        def with_retry(*args, **kwargs):
            out = original(*args, **kwargs)
            out.usage.http_attempts = 2  # 同一逻辑调用内有传输重试
            return out

        llm.generate_structured = with_retry
        settings = Settings(_env_file=None, deepseek_enabled=True)
        (tmp_path / "r2").mkdir()
        service, _ = make_service(tmp_path / "r2", llm=llm, settings=settings)
        run = service.analyze_table("src-1", "tbl-1")
        # 已知小计保留；早期尝试用量不可确认 → 完整性 False
        assert run["llm_prompt_tokens"] == 100
        assert run["llm_usage_complete"] is False


class TestFixRound3FailureAccounting:
    """修复轮 3：失败路径用量完整性与零尝试计数。"""

    @staticmethod
    def _failing(tmp_path, error: LLMRequestError, settings=None):
        class Failing(FakeLLM):
            def generate_structured(self, *a, **k):
                raise error

        (tmp_path).mkdir(parents=True, exist_ok=True)
        service, _ = make_service(tmp_path, llm=Failing([]),
                                  settings=settings or Settings(
                                      _env_file=None, deepseek_enabled=True))
        return service.analyze_table("src-1", "tbl-1")

    def test_retries_then_invalid_json_usage_kept_incomplete(self, tmp_path):
        # 429→429→200 非法模型 JSON（带 usage）：请求数=3、token 保留、完整性=False
        error = LLMRequestError(
            "invalid_response", "模型响应不是合法 JSON", attempts=3,
            usage=LLMUsage(model_reported="m", prompt_tokens=111, completion_tokens=22,
                           calls=1, http_attempts=3))
        run = self._failing(tmp_path / "a", error)
        assert run["llm_http_attempts"] == 3
        assert run["llm_prompt_tokens"] == 111  # 已知 token 保留
        assert run["llm_completion_tokens"] == 22
        assert run["llm_usage_complete"] is False  # 早期 429 尝试用量不可确认
        assert run["llm_prompt_usage_complete"] is False
        assert run["llm_completion_usage_complete"] is False

    def test_zero_attempts_preserved(self, tmp_path):
        # 调用前已超期：attempts=0 必须保留为 0，不强制改写为 1
        error = LLMRequestError("deadline_exceeded", "超期", attempts=0, usage=None)
        run = self._failing(tmp_path / "b", error)
        assert run["llm_http_attempts"] == 0
        assert run["llm_prompt_tokens"] is None  # 未发生请求，无用量可记
        assert run["llm_usage_complete"] is True  # 没有已发生请求 → 无未知用量

    def test_single_failure_full_usage_complete(self, tmp_path):
        error = LLMRequestError(
            "invalid_response", "模型输出不是合法 JSON", attempts=1,
            usage=LLMUsage(prompt_tokens=50, completion_tokens=8, calls=1, http_attempts=1))
        run = self._failing(tmp_path / "c", error)
        assert run["llm_http_attempts"] == 1
        assert run["llm_prompt_tokens"] == 50
        assert run["llm_usage_complete"] is True  # 单次尝试且用量完整

    def test_one_side_known_flags(self, tmp_path):
        error = LLMRequestError(
            "invalid_response", "x", attempts=1,
            usage=LLMUsage(prompt_tokens=60, completion_tokens=None,
                           calls=1, http_attempts=1))
        run = self._failing(tmp_path / "d", error)
        assert run["llm_prompt_tokens"] == 60  # 已知一侧保留
        assert run["llm_prompt_usage_complete"] is True
        assert run["llm_completion_usage_complete"] is False
        assert run["llm_usage_complete"] is False

    def test_legacy_adapter_without_attempts_counts_one(self, tmp_path):
        # 旧适配器异常缺少 attempts 属性：按至少一次请求保守计数
        error = LLMRequestError("network", "网络错误")  # attempts 默认 0
        error.attempts = None  # 模拟旧适配器未携带
        run = self._failing(tmp_path / "e", error)
        assert run["llm_http_attempts"] == 1
        assert run["llm_usage_complete"] is False  # 无 usage 可确认
