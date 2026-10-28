"""M3 关系用例编排：发现、验证、审核、过期与图查询（修复轮加固）。

执行与安全约束：
- 候选发现只读本地持久化的 M1/M2 结果，不连接源库；数据验证由显式操作
  触发，经专用 RelationshipProbe 执行系统构造的有界聚合查询（无任意 SQL）；
- 快照语义（修复轮）：同一逻辑键在不同快照是**独立证据链**——查询一律
  先限定快照再解析链内有效修订（默认当前发布快照；不回退借用旧快照的
  validated 证据；旧快照 stale/人工结论不遮挡新快照候选，也不未经审核
  继承为新快照结论）；历史修订全保留，可显式查询历史快照；重验显式选择
  目标快照并重新解析两端完整标识（表/字段消失则明确报告不可重验）；
- 验证并发：进程内共享额度（ValidationSlotLimiter，单进程语义；多进程
  部署各自独立，不构成跨进程保证）；额度覆盖打开连接、结构核对与探查
  全程；等待额度受运行期限约束，超时收尾不停留 running；
- 预算与期限：预算按**实际执行的 SQL 语句数**核销（结构核对的
  information_schema 查询一并计数；语句发出即计数，失败尝试计入）；
  单调时钟截止贯穿：等待额度/打开连接/每组查询前检查、请求超时取剩余
  期限、每条查询返回后检查、判定前再检查；超期证据不得升级 validated；
  操作组开始前按估计下界准入，不在预算耗尽后执行"最后一组"；
- 结果发布沿用 M2「候选写入—成功发布」：整次运行的关系修订 + 运行记录
  单事务提交，失败运行零残留；已有有效关系不受失败/超期运行污染；
- 人工审核 expected_revision 仓储内原子乐观锁（按目标快照链校验，409）；
  机器修订不覆盖人工结论；stale/拒绝必须改变有效结果；
- 错误消息沿用固定安全文案：未预期异常只含类型名+阶段+对象标识；
  连接类错误透出连接器已脱敏的 reason；不记录业务值/SQL/参数明文。
"""

import time as _time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import (
    CredentialStoreError,
    DataSourceConnectionError,
    ResourceNotFoundError,
    RevisionConflictError,
    ValidationStatementBudgetExceeded,
)
from app.domain.relationships import (
    RELATIONSHIP_TRANSITIONS,
    RelationshipRecord,
    now_iso,
)
from app.ports.connectors import ConnectorFactory, ProbeFactory
from app.ports.repositories import SourceRepository
from app.relationships.discovery import (
    DISCOVERY_VERSION,
    RecallCandidate,
    RelationshipDiscoveryService,
)
from app.relationships.evidence import (
    EvidenceCollector,
    ValidationBudget,
    decide_status,
    structure_changed,
)
from app.relationships.execution import (
    StatementBudget,
    ValidationSlotLimiter,
)
from app.relationships.graph import (
    PathSearchBudget,
    build_graph,
    find_paths,
    to_mermaid,
)
from app.security.credentials import CredentialStore

_REVIEW_ACTIONS = {"confirm": "validated", "reject": "rejected", "revert": "candidate"}
_VALIDATABLE_STATUSES = ("candidate", "evidence_collected", "needs_review")
# 重绑身份不变量失败的固定安全错误（不携带请求/重建内容，不探查、不保存）
_REBIND_IDENTITY_MISMATCH = (
    "重验重绑内部一致性校验失败：重建候选与请求关系身份不一致"
    "（Schema/表/字段映射或关系键）；已终止，未执行探查、未保存任何关系；"
    "错误详情已抑制"
)
# 结构核对操作组估计下界：每表至少 1 条 information_schema 查询（真实
# Inspector 为多条，按实际计数核销；估计只用于组准入）
_STRUCTURE_GROUP_ESTIMATE = 2


class _BudgetExceeded(Exception):
    """内部哨兵：操作组预算准入不足（该候选本轮不执行，收尾为跳过）。"""


class _DeadlineExceededRun(Exception):
    """内部哨兵：运行期限已到（收尾为 failed/partial，不停留 running）。"""


class RelationshipService:
    def __init__(
        self,
        metadata_repository: Any,  # 具体仓储同时实现 MetadataRepository 与 RelationshipRepository
        source_repository: SourceRepository,
        credential_store: CredentialStore,
        connector_factory: ConnectorFactory,
        probe_factory: ProbeFactory,
        settings: Settings,
        discovery: RelationshipDiscoveryService | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._repo = metadata_repository
        self._sources = source_repository
        self._credentials = credential_store
        self._connector_factory = connector_factory
        self._probe_factory = probe_factory
        self._settings = settings
        self._clock = clock or _time.monotonic
        self._discovery = discovery or RelationshipDiscoveryService()

    # -- 快照解析（与 M2 语义一致的目录语义） -----------------------------------

    def _resolve_snapshot(
        self, source_id: str, snapshot_id: str | None, allow_partial: bool
    ) -> tuple[str, bool]:
        if snapshot_id is None:
            published = self._repo.get_published_snapshot(source_id)
            if published is None:
                raise ResourceNotFoundError(
                    "snapshot", f"数据源没有可用的发布快照，请先完成扫描：{source_id}")
            if published.get("complete") is False and not allow_partial:
                raise ValueError(
                    "当前发布目录来自部分成功快照（输入不完整）；确需执行请传 allow_partial=true"
                )
            return published["snapshot_id"], bool(published.get("complete", True))
        job = next(
            (j for j in self._repo.list_scan_jobs(source_id)
             if j.get("snapshot_id") == snapshot_id), None)
        if job is None:
            raise ResourceNotFoundError(
                "snapshot", f"快照没有对应的扫描任务记录：{snapshot_id}")
        return snapshot_id, job.get("status") == "succeeded"

    def _require_source(self, source_id: str) -> dict[str, Any]:
        source = self._sources.get_source(source_id)
        if source is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        return source

    def _published_snapshot_id(self, source_id: str) -> str | None:
        published = self._repo.get_published_snapshot(source_id)
        return published["snapshot_id"] if published else None

    # -- 候选发现（纯本地） -------------------------------------------------------

    def discover_relationships(
        self,
        source_id: str,
        *,
        snapshot_id: str | None = None,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        self._require_source(source_id)
        snapshot_id, input_complete = self._resolve_snapshot(
            source_id, snapshot_id, allow_partial)
        run = {
            "run_id": uuid4().hex,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "run_type": "discovery",
            "status": "running",
            "started_at": now_iso(),
            "finished_at": None,
            "input_complete": input_complete,
            "tables_in_input": 0,
            "columns_analyzed_input": 0,
            "semantics_used": 0,
            "candidates_found": 0,
            "candidates_new": 0,
            "candidates_idempotent_skipped": 0,
            "stale_marked": 0,
            "rules_version": DISCOVERY_VERSION,
            "budget": {
                "max_candidates": self._settings.relationship_max_candidates_per_run,
                "topk_per_field": self._settings.relationship_topk_per_field,
                "deadline_seconds": self._settings.relationship_discovery_deadline_seconds,
            },
            "errors": [],
            "notes": [],
            "elapsed_ms": None,
        }
        self._repo.save_relationship_run(run)
        started = self._clock()
        try:
            tables = self._repo.list_table_metadata(source_id, snapshot_id=snapshot_id)
            profiles = {t["table_id"]: self._repo.list_column_profiles(t["table_id"])
                        for t in tables}
            semantics = self._repo.list_field_semantics_by_source(
                source_id, snapshot_id, effective=True)
            run["tables_in_input"] = len(tables)
            run["columns_analyzed_input"] = sum(
                len(t.get("columns") or []) for t in tables)
            run["semantics_used"] = len(semantics)
            records, notes, counts = self._build_discovery_records(
                source_id, snapshot_id, input_complete, tables, profiles, semantics, run)
        except Exception as exc:  # 收尾兜底：固定安全文案，失败运行零残留
            run["status"] = "failed"
            run["finished_at"] = now_iso()
            run["elapsed_ms"] = int((self._clock() - started) * 1000)
            run["errors"].append({
                "reason": "internal_error",
                "message": self._safe_internal(exc, run["run_id"], "关系发现"),
            })
            self._repo.save_relationship_run(run)
            return run

        run.update({
            "candidates_found": counts["found"],
            "candidates_new": counts["new"],
            "candidates_idempotent_skipped": counts["skipped"],
            "stale_marked": counts["stale_marked"],
            "notes": notes,
        })
        run["status"] = "succeeded" if not run["errors"] else "partial"
        run["finished_at"] = now_iso()
        run["elapsed_ms"] = int((self._clock() - started) * 1000)
        # 单事务提交：候选修订 + 运行记录；批写入失败 → failed 运行零残留
        try:
            self._repo.save_relationship_batch(records, run)
        except Exception as exc:
            run["status"] = "failed"
            run["errors"].append({
                "reason": "internal_error",
                "message": self._safe_internal(exc, run["run_id"], "关系发现持久化"),
            })
            self._repo.save_relationship_run(run)
        return run

    def _build_discovery_records(
        self,
        source_id: str,
        snapshot_id: str,
        input_complete: bool,
        tables: list[dict[str, Any]],
        profiles: dict[str, list[dict[str, Any]]],
        semantics: list[dict[str, Any]],
        run: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        deadline = self._clock() + self._settings.relationship_discovery_deadline_seconds
        candidates, notes = self._discovery.recall_candidates(
            tables, profiles, semantics,
            topk_per_field=self._settings.relationship_topk_per_field,
            max_candidates=self._settings.relationship_max_candidates_per_run,
            clock=self._clock, deadline=deadline,
        )
        tables_by_id = {t["table_id"]: t for t in tables}
        existing_latest = {
            r["relationship_key"]: r
            for r in self._repo.list_relationships(
                source_id, snapshot_id=snapshot_id, effective_only=False)
        }

        records: list[dict[str, Any]] = []
        skipped = 0
        for candidate in candidates:
            existing = existing_latest.get(candidate.relationship_key)
            if existing is not None and existing.get("recall_signature") == candidate.signature():
                skipped += 1  # 同快照重复召回：幂等跳过
                continue
            records.append(self._candidate_to_record(
                candidate, snapshot_id, run["run_id"]))
        # 旧证据链的过期判定：结构变化（或完整新快照中表消失）→ stale
        stale_records = self._mark_stale_on_structure_change(
            source_id, snapshot_id, input_complete, tables_by_id, run)
        records.extend(stale_records)
        counts = {
            "found": len(candidates),
            "new": len(records) - len(stale_records),
            "skipped": skipped,
            "stale_marked": len(stale_records),
        }
        return records, notes, counts

    def _candidate_to_record(
        self, candidate: RecallCandidate, snapshot_id: str, run_id: str
    ) -> dict[str, Any]:
        record = RelationshipRecord(
            relationship_key=candidate.relationship_key,
            source_id=candidate.source_table["source_id"],
            source_schema=candidate.source_table["schema_name"],
            source_table=candidate.source_table["table_name"],
            source_table_id=candidate.source_table["table_id"],
            source_columns=list(candidate.source_columns),
            target_schema=candidate.target_table["schema_name"],
            target_table=candidate.target_table["table_name"],
            target_table_id=candidate.target_table["table_id"],
            target_columns=list(candidate.target_columns),
            origin=list(candidate.origin),
            cardinality=candidate.cardinality_hint,
            recall_reasons=list(candidate.reasons),
            risks=list(candidate.risks),
            support_evidence=list(candidate.support),
            counter_evidence=list(candidate.counter),
            scores=dict(candidate.scores),
            status="candidate",
            snapshot_id=snapshot_id,
            run_id=run_id,
            inference_source="rules",
            rules_version=DISCOVERY_VERSION,
        ).to_dict()
        record["type_verdict"] = candidate.type_verdict
        record["type_reason"] = candidate.type_reason
        record["recall_signature"] = candidate.signature()
        return record

    def _mark_stale_on_structure_change(
        self,
        source_id: str,
        new_snapshot_id: str,
        new_input_complete: bool,
        new_tables_by_id: dict[str, dict[str, Any]],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """快照比较过期：对带证据的旧链，结构变化（或完整快照中表消失）
        在**其所属链内**追加 stale 修订；缺失于部分快照的表不当作消失。
        旧链的 stale 不遮挡新快照候选（列表按快照先行过滤）。"""
        new_by_name = {
            (t["schema_name"], t["table_name"]): t for t in new_tables_by_id.values()
        }
        stale_records: list[dict[str, Any]] = []
        # 全部链（每键每快照一条有效结果）：旧链独立判定
        for record in self._repo.list_relationships(source_id, effective_only=True):
            if record.get("snapshot_id") == new_snapshot_id:
                continue
            if record.get("status") not in ("evidence_collected", "validated", "needs_review"):
                continue  # candidate 无数据证据，无需过期
            changed_tables: list[str] = []
            missing_tables: list[str] = []
            for schema, table_name, table_id in (
                (record["source_schema"], record["source_table"],
                 record.get("source_table_id")),
                (record["target_schema"], record["target_table"],
                 record.get("target_table_id")),
            ):
                old_table = (
                    self._repo.get_table_metadata(table_id)
                    if table_id else None
                )
                new_table = new_by_name.get((schema, table_name))
                if new_table is None:
                    if new_input_complete:
                        missing_tables.append(f"{schema}.{table_name}")
                    continue  # 部分快照缺表不当作消失
                if old_table is not None and structure_changed(old_table, new_table):
                    changed_tables.append(f"{schema}.{table_name}")
            if changed_tables or missing_tables:
                reasons = []
                if changed_tables:
                    reasons.append(f"结构变化：{','.join(changed_tables)}")
                if missing_tables:
                    reasons.append(f"表在新快照中消失：{','.join(missing_tables)}")
                stale_records.append({
                    **record,
                    "status": "stale",
                    "inference_source": "system",
                    "run_id": run["run_id"],
                    "created_at": now_iso(),
                    "review_note": "快照比较判定过期（" + "；".join(reasons) + "）；需显式重验",
                    "validation": {
                        "policy_version": None,
                        "decision": "stale",
                        "decided_by": "system_snapshot_compare",
                        "reasons": reasons,
                    },
                })
        return stale_records

    # -- 数据验证（显式触发、有界、只读） -----------------------------------------

    def validate_relationship(
        self, source_id: str, relationship_key: str, *,
        snapshot_id: str | None = None, allow_partial: bool = False,
    ) -> dict[str, Any]:
        """对单条关系发起验证：默认**当前发布快照**内的链（与其他查询规则
        一致）；显式 snapshot_id 可验证历史链（诊断用）。"""
        self._require_source(source_id)
        snapshot_id, input_complete = self._resolve_snapshot(
            source_id, snapshot_id, allow_partial)
        record = self._repo.get_effective_relationship(
            relationship_key, snapshot_id=snapshot_id)
        if record is None or record.get("source_id") != source_id:
            raise ResourceNotFoundError(
                "relationship",
                f"关系在目标快照内不存在：{relationship_key}"
                "（可改用 revalidate 对目标快照重绑重验，或传 snapshot_id 验证历史链）")
        if record.get("status") not in _VALIDATABLE_STATUSES + ("stale",):
            raise ValueError(
                f"当前状态 {record.get('status')} 不可发起验证"
                f"（validated/rejected 请先 revert 或标记 stale 后重验）")
        return self._run_validation(
            source_id, [record], input_complete=input_complete)

    def validate_batch(
        self, source_id: str, *, limit: int | None = None,
        snapshot_id: str | None = None, allow_partial: bool = False,
    ) -> dict[str, Any]:
        self._require_source(source_id)
        snapshot_id, input_complete = self._resolve_snapshot(
            source_id, snapshot_id, allow_partial)
        limit = min(
            limit or self._settings.relationship_validation_max_candidates,
            self._settings.relationship_validation_max_candidates,
        )
        candidates = [
            r for r in self._repo.list_relationships(
                source_id, snapshot_id=snapshot_id, effective_only=True)
            if r.get("status") in _VALIDATABLE_STATUSES
        ][:limit]
        if not candidates:
            raise ResourceNotFoundError(
                "relationship", "目标快照内没有可验证的候选关系（先执行发现）")
        return self._run_validation(
            source_id, candidates, input_complete=input_complete)

    def _run_validation(
        self, source_id: str, records: list[dict[str, Any]],
        *, input_complete: bool | None = None,
    ) -> dict[str, Any]:
        """验证执行（修复轮）：进程内共享并发额度 + SQL 语句预算 + 期限贯穿。

        - 并发：等待额度受运行期限约束（超时收尾 failed，未执行任何查询）；
          额度覆盖打开连接、结构核对与探查全程；异常路径 finally 释放；
        - 预算：按实际 SQL 语句数核销（含结构核对与失败尝试）；每组查询
          开始前按估计下界准入；耗尽即停，不执行"最后一组"；
        - 期限：单调时钟截止贯穿等待/连接/每组查询/返回后/判定前；
          超期证据标记 deadline_exceeded，策略不得升级 validated。
        """
        run = {
            "run_id": uuid4().hex,
            "source_id": source_id,
            "snapshot_id": records[0].get("snapshot_id") if len(records) == 1 else None,
            "run_type": "validation",
            "status": "running",
            "started_at": now_iso(),
            "finished_at": None,
            "input_complete": input_complete,
            "candidates_planned": len(records),
            "candidates_validated": 0,
            "candidates_needs_review": 0,
            "candidates_stale_marked": 0,
            "candidates_skipped": 0,
            "queries_used": 0,  # 已发出并实际执行的 SQL 语句数（含结构核对与失败尝试；被拒绝语句不计入）
            "sql_statements_rejected": 0,  # 在预算检查处被拒绝、未发出的语句数
            "budget": {
                "max_candidates": len(records),
                "max_sql_statements": self._settings.relationship_validation_max_queries,
                "per_query_timeout_ms": self._settings.relationship_query_timeout_ms,
                "deadline_seconds": self._settings.relationship_validation_deadline_seconds,
                "sample_window_rows": self._settings.relationship_sample_window_rows,
                "exact_max_rows": self._settings.relationship_exact_max_rows,
                "concurrency": {
                    "limit": self._settings.relationship_max_concurrent_validations,
                    "scope": "process",  # 单进程共享；多进程部署各自独立
                },
            },
            "results": [],
            "errors": [],
            "elapsed_ms": None,
        }
        self._repo.save_relationship_run(run)
        started = self._clock()
        deadline = started + self._settings.relationship_validation_deadline_seconds
        counter = StatementBudget(self._settings.relationship_validation_max_queries)
        budget = ValidationBudget(
            max_queries=self._settings.relationship_validation_max_queries,
            per_query_timeout_ms=self._settings.relationship_query_timeout_ms,
            deadline_monotonic=deadline,
            sample_window_rows=self._settings.relationship_sample_window_rows,
            exact_max_rows=self._settings.relationship_exact_max_rows,
            clock=self._clock,
            counter=counter,
        )
        concurrency_limit = self._settings.relationship_max_concurrent_validations
        connector = None
        probe = None
        new_records: list[dict[str, Any]] = []
        slot_acquired = False
        try:
            try:
                # 1) 进程内共享并发额度（等待受期限约束；False=超时未获额度）
                wait_seconds = deadline - self._clock()
                if not ValidationSlotLimiter.acquire(concurrency_limit,
                                                     timeout=wait_seconds):
                    run["errors"].append({
                        "reason": "concurrency_slot_timeout",
                        "message": "等待验证并发额度超时（运行期限耗尽），本次未执行任何查询",
                    })
                    raise _DeadlineExceededRun
                slot_acquired = True
                # 2) 打开连接前检查期限
                if budget.expired():
                    run["errors"].append({
                        "reason": "deadline_exceeded",
                        "message": "获得并发额度后运行期限已耗尽，未打开源库连接",
                    })
                    raise _DeadlineExceededRun
                connector, probe = self._open_source_connection(source_id, counter)
                structure_cache: dict[tuple[str, str], dict[str, Any]] = {}
                collector = EvidenceCollector(probe, counter)
                for record in sorted(
                    records,
                    key=lambda r: -((r.get("scores") or {}).get("total_weighted", 0.0)),
                ):
                    if budget.expired() or budget.exhausted():
                        run["candidates_skipped"] += (
                            len(records) - len(new_records) - run["candidates_skipped"])
                        run["errors"].append({
                            "reason": "budget_exceeded",
                            "message": (
                                f"验证预算耗尽（SQL 语句 {budget.statements_used()}"
                                f"/{budget.max_queries} 或期限到期），剩余候选本轮未验证"),
                        })
                        break
                    try:
                        new_record = self._validate_one(
                            record, connector, collector, budget, run, structure_cache)
                    except _DeadlineExceededRun:
                        raise  # 期限到：停止处理剩余候选，交由外层收尾
                    except _BudgetExceeded:
                        run["candidates_skipped"] += 1
                        run["errors"].append({
                            "reason": "budget_exceeded",
                            "message": "结构核对操作组预算不足（估计下界不满足），该候选本轮跳过",
                        })
                        continue
                    except ValidationStatementBudgetExceeded as exc:
                        # 逐语句硬限制：语句被拒绝未发出（结构核对多语句中途触发）
                        run["candidates_skipped"] += 1
                        run["errors"].append({
                            "reason": "budget_exceeded",
                            "message": f"{exc}；该候选本轮跳过，剩余候选不再执行",
                        })
                        break
                    except DataSourceConnectionError as exc:
                        run["candidates_skipped"] += 1
                        run["errors"].append({
                            "reason": f"source_query_failed:{exc.reason}",
                            "message": str(exc),
                        })
                        continue
                    except Exception as exc:  # 单候选未预期异常：隔离，固定安全文案
                        run["candidates_skipped"] += 1
                        run["errors"].append({
                            "reason": "internal_error",
                            "message": self._safe_internal(
                                exc, run["run_id"], "关系验证",
                                record.get("relationship_key")),
                        })
                        continue
                    if new_record is not None:
                        new_records.append(new_record)
                        if new_record["status"] == "validated":
                            run["candidates_validated"] += 1
                        elif new_record["status"] == "needs_review":
                            run["candidates_needs_review"] += 1
                        elif new_record["status"] == "stale":
                            run["candidates_stale_marked"] += 1
            except _DeadlineExceededRun:
                if not any(e["reason"] == "deadline_exceeded" for e in run["errors"]):
                    run["errors"].append({
                        "reason": "deadline_exceeded",
                        "message": "运行期限耗尽，已停止后续验证（结构核对/证据采集未完成）",
                    })
            except CredentialStoreError as exc:
                run["errors"].append({"reason": "credential_store_error", "message": str(exc)})
            except DataSourceConnectionError as exc:
                run["errors"].append({"reason": f"source_connection_failed:{exc.reason}",
                                      "message": str(exc)})
            except Exception as exc:  # 兜底收尾：failed，零残留
                run["errors"].append({
                    "reason": "internal_error",
                    "message": self._safe_internal(exc, run["run_id"], "关系验证"),
                })
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
            if connector is not None:
                try:
                    connector.close()
                except Exception:
                    pass
            if slot_acquired:
                try:
                    ValidationSlotLimiter.release(concurrency_limit)
                except Exception:
                    pass  # 越界释放为编程错误；不覆盖主要结果
            run["queries_used"] = counter.count
            run["sql_statements_rejected"] = counter.rejected
        run["finished_at"] = now_iso()
        run["elapsed_ms"] = int((self._clock() - started) * 1000)
        if any(e["reason"] in ("deadline_exceeded", "concurrency_slot_timeout")
               for e in run["errors"]) and not new_records:
            run["status"] = "failed"
        elif run["errors"] and not new_records and not run["results"]:
            # 连接/凭证等启动失败且零候选处理 → failed（不伪装 partial 成功）
            run["status"] = "failed"
        elif run["candidates_skipped"] or run["errors"]:
            run["status"] = "partial"
        else:
            run["status"] = "succeeded"
        if run["status"] == "failed":
            # 失败运行零残留：仅保存运行记录，不发布任何"已验证"修订
            # （已有有效关系不受本次失败/超期运行污染）
            self._repo.save_relationship_run(run)
        else:
            try:
                self._repo.save_relationship_batch(new_records, run)
            except Exception as exc:
                run["status"] = "failed"
                run["errors"].append({
                    "reason": "internal_error",
                    "message": self._safe_internal(exc, run["run_id"], "关系验证持久化"),
                })
                self._repo.save_relationship_run(run)
        return run

    def _validate_one(
        self,
        record: dict[str, Any],
        connector: Any,
        collector: EvidenceCollector,
        budget: ValidationBudget,
        run: dict[str, Any],
        structure_cache: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any] | None:
        # 1) 结构一致性：源库 vs 输入快照（不一致不绑定证据 → stale）。
        #    同表结构核对在**本运行内**复用（缓存仅运行内有效，不跨运行信任）；
        #    每次核对按实际 SQL 语句计数，组前按估计下界准入。
        snapshot_tables: dict[str, dict[str, Any]] = {}
        for side, schema, table_name, table_id in (
            ("source", record["source_schema"], record["source_table"],
             record.get("source_table_id")),
            ("target", record["target_schema"], record["target_table"],
             record.get("target_table_id")),
        ):
            snapshot = (
                self._repo.get_table_metadata(table_id) if table_id else None)
            if snapshot is None:
                raise ValueError("输入快照中找不到表元数据，无法验证")
            snapshot_tables[side] = snapshot
            cache_key = (schema, table_name)
            live = structure_cache.get(cache_key)
            if live is None:
                if budget.expired():
                    raise _DeadlineExceededRun
                if not budget.check_group(_STRUCTURE_GROUP_ESTIMATE):
                    raise _BudgetExceeded
                live = self._live_table_dict(
                    connector.get_table_metadata(schema, table_name))
                structure_cache[cache_key] = live
            if not EvidenceCollector.structure_matches(snapshot, live):
                stale_record = {
                    **record,
                    "status": "stale",
                    "inference_source": "system",
                    "run_id": run["run_id"],
                    "created_at": now_iso(),
                    "review_note": (
                        f"验证时发现源库结构与输入快照不一致（{side} 侧"
                        f"{schema}.{table_name}）；证据不绑定当前快照，请重扫后重验"
                    ),
                    "risks": list(record.get("risks") or []) + [{
                        "type": "structure_changed",
                        "note": "源库结构与输入快照不一致，证据未绑定",
                    }],
                }
                run["results"].append({
                    "relationship_key": record["relationship_key"],
                    "status": "stale",
                    "reason": "structure_mismatch",
                })
                return stale_record

        # 2) 证据采集（有界只读聚合；标识符来自快照元数据白名单）。
        #    类型不兼容（跨族，如字符串 vs 整数）时不执行 JOIN——即使 typed
        #    等值比较不会隐式转换，也不做无意义探查；明确记阻断原因。
        if record.get("type_verdict") == "incompatible":
            evidence = {
                "collected_at": now_iso(),
                "snapshot_id": record.get("snapshot_id"),
                "policy_version": None,
                "queries": [],
                "gated": [{
                    "metric": "all",
                    "reason": "type_incompatible:连接两端类型跨族，"
                              "不执行可能产生隐式类型转换语义混淆的 JOIN",
                }],
                "deadline_exceeded": False,
                "notes": ["类型不兼容：候选可能是编码映射，需人工判断"],
            }
        else:
            evidence = collector.collect(
                record, snapshot_tables["source"], snapshot_tables["target"], budget)
        # 3) 判定前再次检查期限：超期证据不得升级 validated（策略内阻断）
        if budget.expired():
            evidence["deadline_exceeded"] = True

        updated = {
            **record,
            "evidence": evidence,
            "cardinality": (evidence.get("cardinality_observed") or {})
            .get("cardinality") or record.get("cardinality_hint"),
            "run_id": run["run_id"],
            "created_at": now_iso(),
            "inference_source": "system",
        }
        validation = decide_status(
            updated, evidence,
            coverage_threshold=self._settings.relationship_coverage_threshold)
        if validation["decision"] == "validated":
            updated["risks"] = list(updated.get("risks") or []) + [{
                "type": "tenant_scope",
                "note": "若业务存在租户/维度划分，编号可能跨租户重复；"
                        "当前验证未分层，必要时按租户/时间/状态条件分层重验",
            }]
        updated["validation"] = validation
        updated["status"] = validation["decision"]
        run["results"].append({
            "relationship_key": record["relationship_key"],
            "status": updated["status"],
            "decision": validation["decision"],
            "blocking": validation["blocking"][:5],
            "queries": [q["template"] for q in evidence.get("queries", [])],
        })
        return updated

    @staticmethod
    def _live_table_dict(table_metadata: Any) -> dict[str, Any]:
        """把连接器返回的 TableMetadata 领域对象转为可比较 dict。"""
        columns = []
        for column in table_metadata.columns:
            columns.append({
                "name": column.name,
                "data_type": column.data_type,
                "nullable": column.nullable,
                "comment": column.comment,
            })
        return {
            "schema_name": table_metadata.schema_name,
            "table_name": table_metadata.table_name,
            "columns": columns,
            "primary_key": list(table_metadata.primary_key),
            "foreign_keys": [dict(fk) for fk in table_metadata.foreign_keys],
            "indexes": [
                {"name": idx.name, "columns": list(idx.columns),
                 "unique": idx.unique}
                for idx in table_metadata.indexes
            ],
        }

    def _open_source_connection(
        self, source_id: str, counter: StatementBudget | None = None
    ) -> tuple[Any, Any]:
        source = self._require_source(source_id)
        password = self._credentials.get(source["credential_ref"])
        if password is None:
            raise CredentialStoreError("凭证不存在或已被删除，无法建立只读验证连接")
        params: dict[str, Any] = {
            "dialect": source["dialect"],
            "host": source["host"],
            "port": source["port"],
            "database": source["database"],
            "username": source["username"],
            "password": SecretStr(password),
        }
        if counter is not None:
            params["statement_counter"] = counter  # 适配器逐 SQL 语句计数
        connector = self._connector_factory(params)
        try:
            probe = self._probe_factory(params)
        except Exception:
            connector.close()
            raise
        return connector, probe

    # -- 查询（默认当前发布快照；显式 snapshot_id 查历史） -----------------------

    def list_relationships(
        self, source_id: str, *, status: str | None = None,
        snapshot_id: str | None = None, effective_only: bool = True,
    ) -> list[dict[str, Any]]:
        self._require_source(source_id)
        if snapshot_id is None:
            snapshot_id = self._published_snapshot_id(source_id)
        if snapshot_id is None:
            return []
        return self._repo.list_relationships(
            source_id, snapshot_id=snapshot_id, status=status,
            effective_only=effective_only)

    def get_relationship(
        self, relationship_key: str, *, effective: bool = True,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """详情默认解析记录所属数据源的**当前发布快照**链（与列表/图一致，
        不回退旧快照证据）；显式 snapshot_id 查询历史链。"""
        history = self._repo.get_relationship_history(relationship_key)
        if not history:
            raise ResourceNotFoundError(
                "relationship", f"关系不存在：{relationship_key}")
        if snapshot_id is None:
            snapshot_id = self._published_snapshot_id(history[0]["source_id"])
            if snapshot_id is None:
                raise ResourceNotFoundError(
                    "relationship",
                    f"数据源没有发布快照，无法定位关系默认视图：{relationship_key}"
                    "（可传 snapshot_id 查询历史快照）")
        if effective:
            record = self._repo.get_effective_relationship(
                relationship_key, snapshot_id=snapshot_id)
        else:
            record = next(
                (r for r in history if r.get("snapshot_id") == snapshot_id), None)
        if record is None:
            raise ResourceNotFoundError(
                "relationship",
                f"关系在快照 {snapshot_id} 内不存在：{relationship_key}"
                "（默认查询当前发布快照；可传 snapshot_id 查询历史快照）")
        return record

    def get_relationship_history(self, relationship_key: str) -> list[dict[str, Any]]:
        history = self._repo.get_relationship_history(relationship_key)
        if not history:
            raise ResourceNotFoundError(
                "relationship", f"关系不存在：{relationship_key}")
        return history

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._repo.get_relationship_run(run_id)
        if run is None:
            raise ResourceNotFoundError("relationship_run", f"运行不存在：{run_id}")
        return run

    def list_runs(self, source_id: str, run_type: str | None = None) -> list[dict[str, Any]]:
        self._require_source(source_id)
        return self._repo.list_relationship_runs(source_id, run_type=run_type)

    # -- 人工审核 / 过期 / 重验 -------------------------------------------------

    def _chain_latest(
        self, relationship_key: str, expected_revision: int
    ) -> dict[str, Any]:
        """按 expected_revision 定位目标修订及其快照链内最新修订。

        审核语义：expected_revision 指向的修订决定**目标快照链**；链内最新
        修订必须等于 expected_revision（否则 409）。其他快照链的写入不构成
        本链冲突（修订号全局单调，跨链并发由唯一约束安全仲裁）。
        """
        history = self._repo.get_relationship_history(relationship_key)
        if not history:
            raise ResourceNotFoundError(
                "relationship", f"关系不存在：{relationship_key}")
        target = next(
            (r for r in history if r.get("revision") == expected_revision), None)
        if target is None:
            raise RevisionConflictError(
                f"修订冲突：修订 {expected_revision} 不存在（可能已被并发覆盖）")
        chain = [r for r in history if r.get("snapshot_id") == target.get("snapshot_id")]
        latest_in_chain = chain[0]
        if latest_in_chain["revision"] != expected_revision:
            raise RevisionConflictError(
                f"修订冲突：该快照链最新修订为 {latest_in_chain['revision']}，"
                f"提交基于 {expected_revision}")
        return latest_in_chain

    def submit_review(
        self,
        relationship_key: str,
        *,
        action: str,
        reviewer: str = "local-dev",
        expected_revision: int,
        note: str | None = None,
    ) -> int:
        if action not in _REVIEW_ACTIONS:
            raise ValueError(f"未知审核动作：{action}")
        latest = self._chain_latest(relationship_key, expected_revision)
        target = _REVIEW_ACTIONS[action]
        allowed = RELATIONSHIP_TRANSITIONS.get(latest.get("status"), ())
        # 同状态重复提交：仅 confirm 允许（人工再确认强化结论）；
        # revert 要求真实状态变化（candidate 再 revert 无意义）
        same_status_ok = target == latest.get("status") and action == "confirm"
        if not same_status_ok and target not in allowed:
            raise ValueError(
                f"不允许的转换：{latest.get('status')} → {target}"
                f"（action={action}）")
        updated = {
            **latest,
            "status": target,
            "inference_source": "human",
            "reviewed_by": reviewer,  # 本地开发输入，不宣称身份认证
            "review_note": note,
            "run_id": None,
            "created_at": now_iso(),
        }
        if action == "confirm":
            updated["validation"] = {
                "policy_version": None,
                "decision": "validated",
                "decided_by": "human",
                "reasons": ["人工确认（审核范围与限制以人工说明为准）"],
                "scope": (latest.get("validation") or {}).get("scope"),
            }
        updated.pop("revision", None)
        # 仓储按目标快照链原子校验并写入（全局修订号唯一约束仲裁并发）
        return self._repo.save_relationship(updated, expected_revision=expected_revision)

    def mark_stale(
        self, relationship_key: str, *, reason: str | None = None,
        reviewer: str = "local-dev", expected_revision: int | None = None,
    ) -> int:
        history = self._repo.get_relationship_history(relationship_key)
        if not history:
            raise ResourceNotFoundError(
                "relationship", f"关系不存在：{relationship_key}")
        if expected_revision is not None:
            latest = self._chain_latest(relationship_key, expected_revision)
        else:
            latest = history[0]  # 未指定版本：作用于全局最新修订所属链
        updated = {
            **latest,
            "status": "stale",
            "inference_source": "human",
            "reviewed_by": reviewer,
            "review_note": reason or "人工标记过期，需重验",
            "run_id": None,
            "created_at": now_iso(),
        }
        updated.pop("revision", None)
        return self._repo.save_relationship(updated, expected_revision=expected_revision)

    def revalidate(
        self, source_id: str, relationship_key: str, *,
        snapshot_id: str | None = None, allow_partial: bool = False,
    ) -> dict[str, Any]:
        """显式重验：**明确选择目标快照**（默认当前发布快照；部分快照需
        allow_partial 并记录 input_complete=false），重新解析两端表与字段
        的完整标识后验证：

        - 目标快照链存在且可验证（stale/candidate/evidence_collected/
          needs_review）→ 在该链上验证（使用目标快照 table_id，不借用旧链）；
        - 目标快照无该链 → 按两端 (schema, table, columns) 在目标快照内重绑：
          字段仍存在且映射合法 → 绑定目标快照的新链验证（证据绑目标快照）；
          表或字段已消失 → 明确报告不可重验（400），不拿旧 table_id 比较；
        - 旧快照的人工确认不继承：重绑链从 candidate 起步，验证结论独立。
        """
        self._require_source(source_id)
        target_snapshot, input_complete = self._resolve_snapshot(
            source_id, snapshot_id, allow_partial)
        target_record = self._repo.get_effective_relationship(
            relationship_key, snapshot_id=target_snapshot)
        if target_record is not None and target_record.get("source_id") == source_id:
            if target_record.get("status") not in _VALIDATABLE_STATUSES + ("stale",):
                raise ValueError(
                    f"目标快照链当前状态 {target_record.get('status')} 不可重验"
                    f"（validated/rejected 请先 revert 或标记 stale）")
            return self._run_validation(
                source_id, [target_record], input_complete=input_complete)

        history = self._repo.get_relationship_history(relationship_key)
        if not history or history[0].get("source_id") != source_id:
            raise ResourceNotFoundError(
                "relationship", f"关系不存在：{relationship_key}")
        old_record = history[0]
        rebound, missing = self._rebind_to_snapshot(old_record, target_snapshot)
        if missing:
            raise ValueError(
                "不可重验：目标快照中缺少 " + "；".join(missing)
                + "（表或字段已消失；请确认目标快照或重新发现）")
        return self._run_validation(
            source_id, [rebound], input_complete=input_complete)

    def _rebind_to_snapshot(
        self, record: dict[str, Any], target_snapshot: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """把稳定逻辑身份重绑到目标快照；候选依据**全部按目标快照重算**。

        稳定身份（relationship_key=两端完整标识+有序映射）与依赖快照的
        候选依据（类型兼容/外键存在性/结构唯一性/风险/证据/评分/画像/语义
        及其版本）明确区分：旧链依据只保留在旧快照历史中；新链经
        discovery.build_candidate_for_mapping 用目标快照的表/画像/有效语义
        重建（复用常规候选构建，不在重验服务里复制算法），不继承旧链的
        审核人、确认或验证结论。目标快照缺画像/语义按缺失处理（对应分项
        记 0，不借用旧快照结果）。表/字段消失 → 返回缺失清单（不可重验）。
        """
        tables = self._repo.list_table_metadata(
            record["source_id"], snapshot_id=target_snapshot)
        tables_by_name = {
            (t["schema_name"], t["table_name"]): t for t in tables
        }
        missing: list[str] = []
        for side in ("source", "target"):
            schema = record[f"{side}_schema"]
            table_name = record[f"{side}_table"]
            columns = list(record[f"{side}_columns"])
            table = tables_by_name.get((schema, table_name))
            if table is None:
                missing.append(f"{schema}.{table_name}")
                continue
            column_names = {c["name"] for c in table.get("columns") or []}
            absent = [c for c in columns if c not in column_names]
            if absent:
                missing.append(f"{schema}.{table_name}({','.join(absent)})")
        if missing:
            return None, missing
        profiles = {t["table_id"]: self._repo.list_column_profiles(t["table_id"])
                    for t in tables}
        semantics = self._repo.list_field_semantics_by_source(
            record["source_id"], target_snapshot, effective=True)
        candidate = self._discovery.build_candidate_for_mapping(
            tables, profiles, semantics,
            source_schema=record["source_schema"],
            source_table=record["source_table"],
            source_columns=list(record["source_columns"]),
            target_schema=record["target_schema"],
            target_table=record["target_table"],
            target_columns=list(record["target_columns"]),
        )
        if candidate is None:
            return None, ["两端标识（内部一致性）"]  # 防御：缺失已在上面排查
        rebound = self._candidate_to_record(candidate, target_snapshot, run_id=None)
        # 身份不变量（防御）：重建候选的完整身份必须与请求关系一致——
        # relationship_key、source_id、两端 Schema/表与有序字段映射。
        # 不一致（如外键改指其他 Schema 的同名表导致重建出另一条关系）时
        # 用固定安全错误终止：不探查、不保存另一条关系，也不通过改键掩盖。
        expected_identity = self._identity_tuple(record)
        actual_identity = self._identity_tuple(rebound)
        if actual_identity != expected_identity:
            raise ValueError(_REBIND_IDENTITY_MISMATCH)
        rebound["recall_reasons"] = list(rebound["recall_reasons"]) + [{
            "origin": "revalidate_rebind", "method": "explicit",
            "detail": f"显式重验：稳定逻辑身份从快照 {record.get('snapshot_id')}"
                      f" 重绑到 {target_snapshot}；候选依据（类型/来源/证据/"
                      "评分/风险）按目标快照重算，不继承旧链内容",
        }]
        return rebound, []

    @staticmethod
    def _identity_tuple(record: dict[str, Any]) -> tuple:
        return (
            record.get("relationship_key"), record.get("source_id"),
            record.get("source_schema"), record.get("source_table"),
            tuple(record.get("source_columns") or []),
            record.get("target_schema"), record.get("target_table"),
            tuple(record.get("target_columns") or []),
        )

    # -- 图与路径 -------------------------------------------------------------

    def graph(
        self, source_id: str, *, snapshot_id: str | None = None,
        statuses: list[str] | None = None, mermaid: bool = False,
    ) -> dict[str, Any]:
        self._require_source(source_id)
        if snapshot_id is None:
            snapshot_id = self._published_snapshot_id(source_id)
        if snapshot_id is None:
            return {"source_id": source_id, "snapshot_id": None,
                    "nodes": [], "edges": [], "mermaid": "graph LR\n    empty[(无数据)]",
                    "note": "数据源没有发布快照"}
        records = self._repo.list_relationships(
            source_id, snapshot_id=snapshot_id, effective_only=True)
        tables = {
            t["table_id"]: t
            for t in self._repo.list_table_metadata(source_id, snapshot_id=snapshot_id)
        }
        graph = build_graph(records, tables, statuses=tuple(statuses) if statuses else None)
        graph["source_id"] = source_id
        graph["snapshot_id"] = snapshot_id
        graph["status_note"] = (
            "候选/拒绝/过期边均可见（状态字段区分）；默认路径搜索仅使用 validated 边"
            "；边仅来自所示快照（不与其他快照混用）")
        if mermaid:
            graph["mermaid"] = to_mermaid(graph)
        return graph

    def paths(
        self, source_id: str, from_table_id: str, to_table_id: str, *,
        max_hops: int | None = None, limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_source(source_id)
        if snapshot_id is None:
            snapshot_id = self._published_snapshot_id(source_id)
        records = self._repo.list_relationships(
            source_id, snapshot_id=snapshot_id, effective_only=True)
        budget = PathSearchBudget(
            max_hops=max_hops or self._settings.relationship_path_max_hops,
            max_paths=limit or self._settings.relationship_path_max_paths,
            max_visited_nodes=self._settings.relationship_path_max_nodes,
            deadline=self._clock() + self._settings.relationship_path_deadline_seconds,
            clock=self._clock,
        )
        result = find_paths(records, from_table_id, to_table_id, budget)
        result["source_id"] = source_id
        result["snapshot_id"] = snapshot_id
        return result

    @staticmethod
    def _safe_internal(
        exc: Exception, run_id: str, stage: str,
        relationship_key: str | None = None,
    ) -> str:
        parts = [f"未预期错误[{type(exc).__name__}]", f"阶段={stage}"]
        if relationship_key:
            parts.append(f"relationship={relationship_key[:12]}")
        parts.append(f"run_id={run_id}")
        return "，".join(parts) + "；错误详情已抑制"
