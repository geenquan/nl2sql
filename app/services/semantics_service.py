"""M2 语义分析用例编排。

- 默认读取 M1 发布快照；部分目录需显式 allow_partial 并标记 input_complete=False；
  显式 snapshot_id 仅用于诊断，不改变发布目录；
- 规则结果始终生成；模型（DeepSeek）启用时做受控推断：预算内调用、严格校验、
  失败回退规则并如实记录，不伪造模型语义；
- 人工修订采用修订号并发控制（RevisionConflictError），确认结论不被模型覆盖；
- 检索为本地词法基线（method=lexical），不冒充向量相似度；
- 错误消息沿用固定安全文案模式（类别 + 标识），不落提示词/响应/异常文本。
"""

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import (
    LLMRequestError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from app.domain.semantics import (
    FIELD_CATEGORIES,
    REVIEW_TRANSITIONS,
    TABLE_PURPOSES,
    FieldSemantics,
    SemanticRun,
    TablePurpose,
    now_iso,
)
from app.ports.llm import LLMClient, LLMStructuredOutput
from app.ports.repositories import MetadataRepository
from app.security.masking import is_sensitive_column
from app.semantics import prompt as prompt_module
from app.semantics.field_understanding import FieldUnderstandingService
from app.semantics.glossary import Glossary

_REVIEW_ACTIONS = {
    "confirm": "business_confirmed",
    "reject": "rejected",
    "revert": "suggested",
}


class SemanticsService:
    def __init__(
        self,
        metadata_repository: MetadataRepository,
        llm_factory: Callable[[], LLMClient] | None,
        settings: Settings,
        understanding: FieldUnderstandingService | None = None,
    ) -> None:
        self._repo = metadata_repository
        self._llm_factory = llm_factory
        self._settings = settings
        self._understanding = understanding or FieldUnderstandingService(
            glossary=Glossary.load_default()
        )

    # -- 分析 -----------------------------------------------------------------

    def analyze_table(
        self,
        source_id: str,
        table_id: str,
        *,
        snapshot_id: str | None = None,
        allow_partial: bool = False,
        send_samples: bool = False,
        column_limit: int | None = None,
    ) -> dict[str, Any]:
        table = self._repo.get_table_metadata(table_id)
        if table is None or table["source_id"] != source_id:
            raise ResourceNotFoundError("table", f"表元数据不存在：{table_id}")

        if snapshot_id is None:
            published = self._repo.get_published_snapshot(source_id)
            if published is None:
                raise ResourceNotFoundError(
                    "snapshot", f"数据源没有可用的发布快照，请先完成扫描：{source_id}")
            snapshot_id = published["snapshot_id"]
            if published.get("complete") is False and not allow_partial:
                raise ValueError(
                    "当前发布目录来自部分成功快照（输入不完整）；确需分析请传 allow_partial=true"
                )
            input_complete = published.get("complete", True)
        else:
            # 显式历史快照仅用于诊断，不改变默认发布目录
            input_complete = True
        if table["snapshot_id"] != snapshot_id:
            raise ValueError(
                f"表不属于目标快照：table_snapshot={table['snapshot_id']}，"
                f"requested={snapshot_id}"
            )

        run = SemanticRun(
            run_id=uuid4().hex,
            source_id=source_id,
            snapshot_id=snapshot_id,
            mode="rules_only",
            table_ids=[table_id],
            input_complete=input_complete,
            send_samples=bool(send_samples and self._settings.semantics_send_samples),
        )
        self._repo.save_semantic_run(run.to_dict())

        try:
            result = self._analyze(table, run, send_samples=run.send_samples,
                                   column_limit=column_limit)
        except Exception as exc:  # 收尾兜底：任务级异常落库为 failed（固定安全文案）
            run.status = "failed"
            run.finished_at = now_iso()
            run.errors.append({
                "reason": "internal_error",
                "message": self._safe_internal(exc, run.run_id, "语义分析"),
            })
            self._repo.save_semantic_run(run.to_dict())
            return run.to_dict()
        return result

    def _analyze(self, table: dict[str, Any], run: SemanticRun, *,
                 send_samples: bool, column_limit: int | None) -> dict[str, Any]:
        profiles = self._repo.list_column_profiles(table["table_id"])
        profiles_by_name = {p.get("column_name"): p for p in profiles}
        columns = table.get("columns", [])
        limit = min(
            self._settings.semantics_max_columns_per_run,
            column_limit or self._settings.semantics_max_columns_per_run,
        )
        analyzed_columns = columns[:limit]
        if len(columns) > limit:
            run.errors.append({
                "reason": "budget_truncated",
                "message": f"列数预算 {limit}，超出 {len(columns) - limit} 列本次未分析",
            })

        fk_columns = {col for fk in (table.get("foreign_keys") or [])
                      for col in (fk.get("columns") or [])}
        rules_results: dict[str, dict[str, Any]] = {}
        for column in analyzed_columns:
            context = {
                "column": column,
                "is_primary_key": column["name"] in (table.get("primary_key") or []),
                "is_foreign_key": column["name"] in fk_columns,
                "in_unique_index": any(
                    column["name"] in (idx.get("columns") or [])
                    for idx in (table.get("indexes") or []) if idx.get("unique")
                ),
                "profile": profiles_by_name.get(column["name"]),
            }
            rules_results[column["name"]] = self._understanding.infer_column_semantics(context)
        purpose_rules = self._understanding.infer_table_purpose({
            "table_name": table.get("table_name", ""),
            "columns": analyzed_columns,
            "primary_key": table.get("primary_key") or [],
            "foreign_keys": table.get("foreign_keys") or [],
            "indexes": table.get("indexes") or [],
            "profiles": [p for p in profiles if p.get("column_name") in rules_results],
        })

        model_outputs: dict[str, dict[str, Any]] = {}
        model_purpose: str | None = None
        if self._settings.deepseek_enabled and self._llm_factory is not None:
            run.mode = "model_assisted"
            model_outputs, model_purpose, model_error = self._model_infer(
                table, analyzed_columns, profiles, run, send_samples)
            if model_error:
                run.errors.append(model_error)
                run.status = "partial"
        run.columns_analyzed = len(analyzed_columns)
        run.status = run.status if run.status == "partial" else "succeeded"

        for column in analyzed_columns:
            model_entry = model_outputs.get(column["name"])
            record = self._build_field_record(
                table, run, column, rules_results[column["name"]], model_entry)
            self._repo.save_field_semantics(record)
        purpose_record = self._build_purpose_record(
            table, run, purpose_rules, model_purpose,
            model_used=bool(model_outputs))
        self._repo.save_table_purpose(purpose_record)

        run.finished_at = now_iso()
        self._repo.save_semantic_run(run.to_dict())
        return run.to_dict()

    def _model_infer(
        self,
        table: dict[str, Any],
        columns: list[dict[str, Any]],
        profiles: list[dict[str, Any]],
        run: SemanticRun,
        send_samples: bool,
    ) -> tuple[dict[str, dict[str, Any]], str | None, dict[str, Any] | None]:
        """返回 (字段输出, 表用途, 错误条目|None)。预算内调用 + 一次修复。"""
        sensitive = {
            c["name"] for c in columns
            if is_sensitive_column(c["name"], self._settings.sensitive_column_keywords)
        }
        payload = prompt_module.build_table_analysis_payload(
            {**table, "columns": columns}, profiles,
            send_samples=send_samples, sensitive_columns=sensitive)
        system, user = prompt_module.build_prompts(payload)
        if len(system) + len(user) > self._settings.semantics_input_char_limit:
            return {}, None, {
                "reason": "input_budget_exceeded",
                "message": "模型输入超过字符预算，本次仅使用规则结果",
            }
        refs, columns_meta = prompt_module.evidence_refs_for(
            {**table, "columns": columns}, profiles)
        expected = [c["name"] for c in columns]

        client = self._llm_factory()  # type: ignore[misc]
        started = time.perf_counter()
        llm_calls = 0
        output: LLMStructuredOutput | None = None
        feedback = ""
        max_calls = min(self._settings.semantics_llm_max_calls, 2)
        try:
            for attempt in range(max_calls):
                llm_calls += 1
                user_prompt = user if not feedback else (
                    user + "\n上一次输出未通过校验：" + feedback + "，请修正后重新输出完整 JSON。"
                )
                output = client.generate_structured(
                    system, user_prompt, {"type": "object"},
                    max_output_chars=100_000,
                )
                validation = prompt_module.validate_model_response(
                    output.result, expected, refs, columns_meta=columns_meta)
                if validation["valid"]:
                    run.model_reported = output.usage.model_reported
                    run.llm_prompt_tokens = output.usage.prompt_tokens
                    run.llm_completion_tokens = output.usage.completion_tokens
                    run.llm_calls = llm_calls
                    run.elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return validation["columns"], validation["table_purpose"], None
                feedback = ";".join(validation["errors"][:5])
            # 有限修复仍不合格：记录失败，不返回伪造模型语义
            run.llm_calls = llm_calls
            run.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return {}, None, {
                "reason": "model_validation_failed",
                "message": "模型输出未通过结构校验（虚构/遗漏/无效证据），已回退规则结果",
            }
        except LLMRequestError as exc:
            run.llm_calls = llm_calls
            run.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return {}, None, {
                "reason": f"model_request_failed:{exc.reason}",
                "message": "模型调用失败（运行故障），已回退规则结果；"
                           "该失败与证据不足的 unknown 不同",
            }

    # -- 记录构建 ---------------------------------------------------------------

    def _build_field_record(
        self,
        table: dict[str, Any],
        run: SemanticRun,
        column: dict[str, Any],
        rules_result: dict[str, Any],
        model_entry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base = FieldSemantics(
            source_id=table["source_id"],
            snapshot_id=table["snapshot_id"],
            table_id=table["table_id"],
            column_name=column["name"],
            original_name=column["name"],
            original_comment=column.get("comment"),
            rules_version=rules_result.get("rules_version"),
            run_id=run.run_id,
        )
        if model_entry is not None:
            record = base.to_dict()
            record.update({
                "category": model_entry["category"],
                "confidence": model_entry["confidence"],
                "suggested_business_name": model_entry.get("business_name")
                or rules_result.get("suggested_business_name"),
                "candidate_entities": model_entry.get("candidate_entities")
                or rules_result.get("candidate_entities", []),
                "alternatives": [{
                    "category": rules_result["category"],
                    "rationale": f"规则候选（{rules_result.get('rules_version')}）",
                    "confidence": rules_result.get("confidence", "low"),
                }] if rules_result["category"] != model_entry["category"] else [],
                "evidence": model_entry.get("evidence", []),
                "inference_source": "model",
                "model_name": run.model_reported,
                "prompt_version": prompt_module.PROMPT_VERSION,
                "rationale": "模型推断（证据已服务端核验）",
            })
            return record
        record = base.to_dict()
        record.update({
            "category": rules_result["category"],
            "confidence": rules_result["confidence"],
            "suggested_business_name": rules_result.get("suggested_business_name"),
            "candidate_entities": rules_result.get("candidate_entities", []),
            "alternatives": rules_result.get("alternatives", []),
            "evidence": rules_result.get("evidence", []),
            "rationale": rules_result.get("rationale"),
            "prompt_version": None,
            "model_name": None,
        })
        return record

    def _build_purpose_record(
        self,
        table: dict[str, Any],
        run: SemanticRun,
        rules_result: dict[str, Any],
        model_purpose: str | None,
        model_used: bool,
    ) -> dict[str, Any]:
        record = TablePurpose(
            source_id=table["source_id"],
            snapshot_id=table["snapshot_id"],
            table_id=table["table_id"],
            table_name=table.get("table_name", ""),
            rules_version=rules_result.get("rules_version"),
            run_id=run.run_id,
        ).to_dict()
        if model_used and model_purpose:
            record.update({
                "purpose": model_purpose,
                "rationale": "模型推断（枚举校验通过）",
                "inference_source": "model",
                "model_name": run.model_reported,
                "prompt_version": prompt_module.PROMPT_VERSION,
                "alternatives": [{
                    "purpose": rules_result["purpose"],
                    "rationale": f"规则候选（{rules_result.get('rules_version')}）",
                    "confidence": rules_result.get("confidence", "low"),
                }] if rules_result["purpose"] != model_purpose else [],
            })
        else:
            record.update({
                "purpose": rules_result["purpose"],
                "rationale": rules_result.get("rationale"),
                "alternatives": rules_result.get("alternatives", []),
                "evidence": rules_result.get("evidence", []),
                "confidence": rules_result.get("confidence", "low"),
            })
        return record

    # -- 人工修订 ---------------------------------------------------------------

    def submit_review(
        self,
        table_id: str,
        column_name: str,
        *,
        action: str,
        patch: dict[str, Any],
        reviewer: str = "local-dev",
        expected_revision: int,
        note: str | None = None,
    ) -> int:
        history = self._repo.get_field_semantics_history(table_id, column_name)
        if not history:
            raise ResourceNotFoundError("field_semantics", f"字段语义不存在：{column_name}")
        latest = history[0]
        if action not in _REVIEW_ACTIONS:
            raise ValueError(f"未知审核动作：{action}")
        target_status = _REVIEW_ACTIONS[action]
        allowed = REVIEW_TRANSITIONS.get(latest["review_status"], ())
        if target_status not in allowed:
            raise ValueError(
                f"不允许的审核转换：{latest['review_status']} → {target_status}"
            )
        if patch.get("category") is not None and patch["category"] not in FIELD_CATEGORIES:
            raise ValueError(f"非法语义类别：{patch['category']}")
        if latest["revision"] != expected_revision:
            raise RevisionConflictError(
                f"修订冲突：最新修订为 {latest['revision']}，提交基于 {expected_revision}"
            )
        normalized = dict(patch)
        if "business_name" in normalized:
            normalized["suggested_business_name"] = normalized.pop("business_name")
        updated = {
            **latest,
            **{k: v for k, v in normalized.items() if v is not None},
            "inference_source": "human",
            "review_status": target_status,
            "reviewed_by": reviewer,  # 本地开发输入，不宣称身份认证
            "review_note": note,
            "created_at": now_iso(),
            "run_id": None,
        }
        updated.pop("revision", None)
        return self._repo.save_field_semantics(updated)

    def submit_purpose_review(
        self,
        table_id: str,
        *,
        action: str,
        patch: dict[str, Any],
        reviewer: str = "local-dev",
        expected_revision: int,
        note: str | None = None,
    ) -> int:
        history = self._repo.get_table_purpose_history(table_id)
        if not history:
            raise ResourceNotFoundError("table_purpose", f"表用途不存在：{table_id}")
        latest = history[0]
        if action not in _REVIEW_ACTIONS:
            raise ValueError(f"未知审核动作：{action}")
        target_status = _REVIEW_ACTIONS[action]
        if target_status not in REVIEW_TRANSITIONS.get(latest["review_status"], ()):
            raise ValueError(
                f"不允许的审核转换：{latest['review_status']} → {target_status}")
        if patch.get("purpose") is not None and patch["purpose"] not in TABLE_PURPOSES:
            raise ValueError(f"非法表用途：{patch['purpose']}")
        if latest["revision"] != expected_revision:
            raise RevisionConflictError(
                f"修订冲突：最新修订为 {latest['revision']}，提交基于 {expected_revision}")
        updated = {
            **latest,
            **{k: v for k, v in patch.items() if v is not None},
            "inference_source": "human",
            "review_status": target_status,
            "reviewed_by": reviewer,
            "review_note": note,
            "created_at": now_iso(),
            "run_id": None,
        }
        updated.pop("revision", None)
        return self._repo.save_table_purpose(updated)

    # -- 查询 -----------------------------------------------------------------

    def get_table_semantics(self, table_id: str, effective: bool = False) -> dict[str, Any]:
        if self._repo.get_table_metadata(table_id) is None:
            raise ResourceNotFoundError("table", f"表元数据不存在：{table_id}")
        return {
            "table_id": table_id,
            "effective": effective,
            "purpose": self._repo.get_table_purpose(table_id, effective=effective),
            "fields": self._repo.list_field_semantics(table_id, effective=effective),
        }

    def get_column_history(self, table_id: str, column_name: str) -> list[dict[str, Any]]:
        history = self._repo.get_field_semantics_history(table_id, column_name)
        if not history:
            raise ResourceNotFoundError(
                "field_semantics", f"字段语义不存在：{table_id}.{column_name}")
        return history

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._repo.get_semantic_run(run_id)
        if run is None:
            raise ResourceNotFoundError("semantic_run", f"语义分析运行不存在：{run_id}")
        return run

    def list_runs(self, source_id: str) -> list[dict[str, Any]]:
        return self._repo.list_semantic_runs(source_id)

    # -- 语义检索（本地词法基线，非向量相似度） -----------------------------------

    def search_fields(
        self,
        source_id: str,
        query: str,
        *,
        snapshot_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if snapshot_id is None:
            published = self._repo.get_published_snapshot(source_id)
            snapshot_id = published["snapshot_id"] if published else None
        if snapshot_id is None:
            return {"method": "lexical", "snapshot_id": None, "items": [],
                    "query": query, "note": "数据源没有发布快照"}
        limit = limit or self._settings.semantics_search_default_limit
        from app.semantics.tokenizer import tokenize

        query_tokens = {t.lower() for t in tokenize(query)} | {query.strip().lower()}
        semantics = self._repo.list_field_semantics_by_source(
            source_id, snapshot_id, effective=True)
        table_cache: dict[str, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []
        for record in semantics:
            table = table_cache.get(record["table_id"])
            if table is None:
                table = self._repo.get_table_metadata(record["table_id"]) or {}
                table_cache[record["table_id"]] = table
            haystacks: list[tuple[str, float]] = []
            business = record.get("suggested_business_name") or ""
            if record.get("review_status") == "business_confirmed" and business:
                haystacks.append((business, 3.0))
            elif business:
                haystacks.append((business, 2.0))
            haystacks.append((record.get("original_name") or "", 2.0))
            haystacks.append((record.get("original_comment") or "", 1.5))
            haystacks.append((" ".join(record.get("candidate_entities") or []), 1.5))
            haystacks.append((record.get("category") or "", 1.0))
            haystacks.append((table.get("table_name") or "", 0.5))
            score = 0.0
            basis: list[str] = []
            for text, weight in haystacks:
                lowered = (text or "").lower()
                if not lowered:
                    continue
                hits = [t for t in query_tokens if t and (t in lowered or lowered in t
                                                          and len(lowered) <= 24)]
                if hits:
                    score += weight * min(len(hits), 3)
                    source_label = {
                        3.0: "confirmed_business_name", 2.0: "business_name",
                    }.get(weight) or {
                        2.0: "original_name", 1.5: "comment_or_entity",
                        1.0: "category", 0.5: "table_name",
                    }.get(weight)
                    if source_label:
                        basis.append(source_label)
            if score > 0:
                items.append({
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "table_id": record["table_id"],
                    "table_name": table.get("table_name"),
                    "column_name": record["column_name"],
                    "business_name": record.get("suggested_business_name"),
                    "category": record.get("category"),
                    "review_status": record.get("review_status"),
                    "inference_source": record.get("inference_source"),
                    "score": round(score, 2),
                    "match_basis": sorted(set(basis)),
                })
        items.sort(key=lambda item: (-item["score"], item["table_id"], item["column_name"]))
        return {"method": "lexical", "snapshot_id": snapshot_id, "query": query,
                "items": items[:limit]}

    @staticmethod
    def _safe_internal(exc: Exception, run_id: str, stage: str) -> str:
        return (
            f"未预期错误[{type(exc).__name__}]，阶段={stage}，run_id={run_id}；错误详情已抑制"
        )
