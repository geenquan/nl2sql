"""M5 数据集服务：定义→编译→预览→质量→发布→刷新/回滚。

安全与一致性：
- 正式构建/校验/发布前从仓储**重载** M4 模型并校验（当前发布版本、
  usable_as_current、指标 ∈ formal_metric_ids、同源同快照、关系为该
  快照有效 validated 修订、路径已消歧），不信任客户端提交的状态；
- 修改定义产生新版本（旧 SQL/质量报告不背书新定义）；
- 未通过质量门槛不能发布；外部目标（视图/ES）与本地指针不跨库原子：
  先记录发布尝试、创建版本目标、验证后切换本地指针；外部成功本地收尾
  失败 → needs_reconciliation（可显式核对/恢复，不伪装成功不盲目删除）；
- 同一数据集并发发布由持久化状态（本地指针/版本状态原子条件更新）仲裁；
- 预览应用现有脱敏策略；原始业务行不入日志/任务载荷/异常消息。
"""

import copy
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError, RevisionConflictError
from app.datasets.compiler import (
    DatasetCompiler,
    definition_fingerprint,
)
from app.datasets.quality import grain_document_id, run_quality_checks
from app.domain.datasets import (
    DatasetSpec,
)
from app.domain.relationships import now_iso
from app.ports.datasets import ViewPublisher
from app.security.masking import mask_value
from app.semantics.prompt import sanitize_text_for_model


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DatasetService:
    def __init__(
        self,
        metadata_repository: Any,
        semantic_models: Any,  # SemanticModelService（重载校验用）
        settings: Settings,
        executor_factory: Any = None,  # (source, whitelist) -> ControlledQueryExecutor
        view_publisher_factory: Any = None,  # () -> ViewPublisher | None
        es_publisher_factory: Any = None,  # () -> ES publisher | None
    ) -> None:
        self._repo = metadata_repository
        self._models = semantic_models
        self._settings = settings
        self._compiler = DatasetCompiler()
        self._executor_factory = executor_factory
        self._view_publisher_factory = view_publisher_factory
        self._es_publisher_factory = es_publisher_factory
        self._publish_mutex = threading.Lock()  # 进程内附加保护（持久化为仲裁）

    # -- M4 门槛 -----------------------------------------------------------------

    def _load_and_check_model(self, source_id: str, model_id: str,
                              model_version: int) -> dict[str, Any]:
        model = self._models.get_model(model_id, version=model_version)
        availability = model.get("availability") or {}
        published = self._models.get_published(source_id)
        pointer_ok = bool(
            published and published["model_id"] == model_id
            and published["version"] == model_version)
        if not pointer_ok or not availability.get("usable_as_current"):
            raise ValueError(
                f"模型版本不是当前发布且可用的版本：{model_id} v{model_version}"
                "（需 M4 发布且依赖有效）")
        if model.get("source_id") != source_id:
            raise ValueError("模型与数据集不属于同一数据源")
        return model

    @staticmethod
    def _check_relationships(model: dict[str, Any], spec: DatasetSpec,
                             repo: Any) -> None:
        for dim in spec.dimensions:
            _check_relationship_valid(
                model, repo, dim["relationship_key"], spec.snapshot_id)
        for pre in spec.preaggregations:
            _check_relationship_valid(
                model, repo, pre["relationship_key"], spec.snapshot_id)

    # -- 定义/版本 -----------------------------------------------------------------

    def create_dataset(
        self, source_id: str, definition: dict[str, Any],
        *, created_by: str = "local-dev",
    ) -> dict[str, Any]:
        """创建数据集 v1（草稿）；服务端核验 M4 门槛与结构化引用。"""
        model = self._load_and_check_model(
            source_id, definition["model_id"], definition["model_version"])
        spec = self._build_spec(source_id, definition, version=1,
                                created_by=created_by)
        self._validate_definition(spec, model)
        payload = spec.to_dict()
        self._repo.save_dataset(payload)
        return self._enrich(payload)

    def revise_dataset(
        self, dataset_id: str, base_version: int, changes: dict[str, Any],
        *, created_by: str = "local-dev", note: str | None = None,
    ) -> dict[str, Any]:
        base = self._repo.get_dataset(dataset_id, version=base_version)
        if base is None:
            raise ResourceNotFoundError(
                "dataset", f"数据集版本不存在：{dataset_id} v{base_version}")
        revised = copy.deepcopy(base)
        revised.update({
            "version": base_version + 1,
            "based_on_version": base_version,
            "created_by": created_by,
            "created_at": _now(),
            "change_note": sanitize_text_for_model(note, 200) if note else None,
            "status": "draft",
            "plan_fingerprint": None,
            "definition_fingerprint": None,
        })
        for key in ("grain_keys", "grain_statement", "fields", "dimensions",
                    "metrics", "preaggregations", "filters", "join_type",
                    "kind", "target"):
            if key in changes and changes[key] is not None:
                revised[key] = changes[key]
        model = self._load_and_check_model(
            revised["source_id"], revised["model_id"],
            revised["model_version"])
        spec = self._spec_from_dict(revised)
        self._validate_definition(spec, model)
        payload = spec.to_dict()
        try:
            self._repo.save_dataset(payload)
        except RevisionConflictError:
            raise RevisionConflictError(
                f"修订冲突：{dataset_id} v{base_version + 1} 已存在") from None
        return self._enrich(payload)

    def _build_spec(self, source_id: str, definition: dict[str, Any], *,
                    version: int, created_by: str) -> DatasetSpec:
        return DatasetSpec(
            dataset_id=definition.get("dataset_id")
            or f"ds-{uuid4().hex[:16]}",
            name=sanitize_text_for_model(definition["name"], 60),
            source_id=source_id,
            snapshot_id=definition.get("snapshot_id"),
            model_id=definition["model_id"],
            model_version=definition["model_version"],
            topic_id=definition["topic_id"],
            kind=definition["kind"],
            central_entity_id=definition["central_entity_id"],
            grain_keys=list(definition.get("grain_keys") or []),
            grain_statement=sanitize_text_for_model(
                definition.get("grain_statement") or "", 200),
            fields=list(definition.get("fields") or []),
            dimensions=list(definition.get("dimensions") or []),
            metrics=list(definition.get("metrics") or []),
            preaggregations=list(definition.get("preaggregations") or []),
            filters=list(definition.get("filters") or []),
            join_type=definition.get("join_type", "left"),
            target=definition.get("target", "view"),
            version=version, created_by=created_by,
            status="draft",
        )

    @staticmethod
    def _spec_from_dict(payload: dict[str, Any]) -> DatasetSpec:
        spec = DatasetSpec(dataset_id=payload["dataset_id"],
                           name=payload["name"],
                           source_id=payload["source_id"],
                           snapshot_id=payload.get("snapshot_id"),
                           model_id=payload["model_id"],
                           model_version=payload["model_version"],
                           topic_id=payload["topic_id"],
                           kind=payload["kind"],
                           central_entity_id=payload["central_entity_id"])
        for key, value in payload.items():
            setattr(spec, key, value)
        return spec

    def _validate_definition(self, spec: DatasetSpec,
                             model: dict[str, Any]) -> None:
        """服务端核验（修复轮：topic/snapshot/实体归属/字段来源一致性）。"""
        entities = {e["entity_id"]: e for e in model.get("entities", [])}
        central = entities.get(spec.central_entity_id)
        if central is None:
            raise ValueError(
                f"中心实体不在模型：{spec.central_entity_id}")
        # snapshot 一致性（修复轮：未传绑定模型快照；传入必须一致）
        if spec.snapshot_id is not None and                 spec.snapshot_id != model.get("snapshot_id"):
            raise ValueError(
                f"快照不一致：数据集 {spec.snapshot_id} vs 模型 "
                f"{model.get('snapshot_id')}（必须与采用模型一致）")
        if spec.snapshot_id is None:
            spec.snapshot_id = model.get("snapshot_id")
        # topic 存在性与中心实体归属（修复轮）
        topics = {t["topic_id"]: t for t in model.get("topics", [])}
        topic = topics.get(spec.topic_id)
        if topic is None:
            raise ValueError(f"主题不在模型：{spec.topic_id}")
        if topic.get("central_entity_id") != spec.central_entity_id:
            raise ValueError(
                f"中心实体不属于该主题：{spec.central_entity_id} "
                f"≠ {topic.get('central_entity_id')}")
        if not spec.grain_keys:
            raise ValueError("数据集必须声明 grain 键")
        columns = {a["field"]["column"] for a in central.get("attributes", [])}
        for key in spec.grain_keys:
            if key not in columns:
                raise ValueError(f"grain 键不在中心表：{key}")
        # 普通 fields 必须属于中心实体（修复轮：完整 schema+table 身份比较）
        central_source = central.get("source") or {}
        for field_spec in spec.fields:
            ref = field_spec["field"]
            if (ref["schema"], ref["table"]) != (
                    central_source.get("schema"),
                    central_source.get("table")):
                raise ValueError(
                    f"字段不属于中心实体：{ref['schema']}.{ref['table']}"
                    f".{ref['column']}（来源 {central_source.get('table')}）")
        # 维度字段必须属于对应关系的目标表
        relationships = {
            r["relationship_key"]: r
            for r in model.get("relationships", [])}
        for dim in spec.dimensions:
            relation = relationships.get(dim["relationship_key"])
            if relation is None:
                raise ValueError(
                    f"维度关系不在模型：{dim['relationship_key']}")
            ref = dim["field"]
            target = relation.get("target") or {}
            if (ref["schema"], ref["table"]) != (
                    target.get("schema"), target.get("table")):
                raise ValueError(
                    f"维度字段不属于关系目标表：{ref['schema']}.{ref['table']}"
                    f"（关系目标 {target.get('table')}）")
        formal = set(model.get("formal_metric_ids") or [])
        metrics_by_id = {m["metric_id"]: m for m in model.get("metrics", [])}
        # 直接指标必须属于中心实体（修复轮）
        for selection in spec.metrics:
            if selection["metric_id"] not in formal:
                raise ValueError(
                    f"指标不属于模型正式集合：{selection['metric_id']}"
                    "（需 M4 business_confirmed 且模型当前可用）")
            metric = metrics_by_id.get(selection["metric_id"])
            if metric and metric.get("entity_id") != spec.central_entity_id:
                raise ValueError(
                    f"直接指标不属于中心实体：{selection['metric_id']}"
                    f"（属于 {metric.get('entity_id')}）")
        # 预聚合指标必须属于对应明细源实体（完整 schema+table 比较）
        for pre in spec.preaggregations:
            if pre["metric_id"] not in formal:
                raise ValueError(
                    f"预聚合指标不属于模型正式集合：{pre['metric_id']}")
            metric = metrics_by_id.get(pre["metric_id"])
            relation = relationships.get(pre["relationship_key"])
            if relation is None:
                raise ValueError(
                    f"预聚合关系不在模型：{pre['relationship_key']}")
            detail_source = relation.get("source") or {}
            metric_entity = (entities.get(metric.get("entity_id"))
                             if metric else None)
            metric_source = (metric_entity.get("source") or {}
                             if metric_entity else {})
            if (metric_source.get("schema"), metric_source.get("table")) != (
                    detail_source.get("schema"), detail_source.get("table")):
                raise ValueError(
                    f"预聚合指标不属于明细表实体：{pre['metric_id']}"
                    f"（属于 {metric_source.get('table')}，"
                    f"关系源 {detail_source.get('table')}）")
        if spec.kind not in ("detail_view", "aggregate"):
            raise ValueError(f"未知数据集类型：{spec.kind}")
        if spec.target not in ("view", "elasticsearch"):
            raise ValueError(f"未知发布目标：{spec.target}")
        self._check_relationships(model, spec, self._repo)
        # 编译即校验（白名单/方向/类型），并落指纹
        plan = self._compiler.compile(spec, model)
        spec.definition_fingerprint = definition_fingerprint(spec)
        spec.plan_fingerprint = plan.plan_fingerprint

    # -- 读取 -----------------------------------------------------------------

    def get_dataset(self, dataset_id: str, version: int | None = None) \
            -> dict[str, Any]:
        payload = self._repo.get_dataset(dataset_id, version=version)
        if payload is None:
            raise ResourceNotFoundError(
                "dataset", f"数据集不存在：{dataset_id}")
        return self._enrich(payload)

    def list_datasets(self, source_id: str) -> list[dict[str, Any]]:
        return [self._enrich(d)
                for d in self._repo.list_datasets(source_id)]

    def list_versions(self, dataset_id: str) -> list[dict[str, Any]]:
        versions = self._repo.list_dataset_versions(dataset_id)
        if not versions:
            raise ResourceNotFoundError(
                "dataset", f"数据集不存在：{dataset_id}")
        return [self._enrich(v) for v in versions]

    def _enrich(self, payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        enriched["sql_plan"] = self._repo.get_latest_plan(
            payload["dataset_id"], payload["version"])
        enriched["quality_report"] = self._repo.get_latest_quality(
            payload["dataset_id"], payload["version"])
        enriched["published"] = self._repo.get_published_dataset(
            payload["source_id"], payload["dataset_id"]) == payload["version"]
        return enriched

    # -- 预览 -----------------------------------------------------------------

    def preview(self, dataset_id: str, version: int, *,
                max_rows: int | None = None) -> dict[str, Any]:
        payload = self._require_ready(dataset_id, version)
        plan = self._repo.get_latest_plan(dataset_id, version)
        if plan is None:
            raise ValueError("该版本尚未编译（先执行质量验证）")
        model = self._models.get_model(
            payload["model_id"], version=payload["model_version"])
        # 预览同样受预算+期限+字节上限约束（修复轮 5：不留无界查询入口）
        executor = self._executor_factory(
            payload, model, self._settings,
            run_context=self._new_run_context())
        rows, truncated = executor.execute(
            plan["sql"], plan["params"],
            max_rows=max_rows or self._settings.dataset_preview_rows,
            timeout_ms=30_000)
        masked = [self._mask_row(row, payload) for row in rows]
        executor.close()
        return {
            "dataset_id": dataset_id, "version": version,
            "rows": masked, "row_count": len(masked),
            "truncated": truncated,
            "note": "预览行经脱敏展示；质量计算使用原始值",
        }

    @staticmethod
    def _mask_row(row: dict[str, Any], payload: dict[str, Any]) \
            -> dict[str, Any]:
        masked = {}
        for key, value in row.items():
            masked[key] = mask_value(value, key.lower())
        return masked

    # -- 质量验证 ---------------------------------------------------------------

    def _new_run_context(self) -> dict[str, Any]:
        """创建共享运行上下文（预算+期限+字节上限），贯穿整次运行。"""
        import time as _time

        from app.relationships.execution import StatementBudget

        return {
            "budget": StatementBudget(
                self._settings.dataset_validation_max_queries),
            "deadline": _time.monotonic()
            + self._settings.dataset_validation_deadline_seconds,
            "max_result_bytes": self._settings.dataset_max_result_bytes,
        }

    def _expired(self, context: dict[str, Any]) -> bool:
        import time as _time

        return _time.monotonic() >= context["deadline"]

    def validate(self, dataset_id: str, version: int) -> dict[str, Any]:
        payload = self._repo.get_dataset(dataset_id, version=version)
        if payload is None:
            raise ResourceNotFoundError(
                "dataset", f"数据集版本不存在：{dataset_id} v{version}")
        if payload.get("status") not in ("draft", "failed_validation"):
            raise ValueError(
                f"状态 {payload.get('status')} 不可发起验证"
                f"（已就绪/已发布版本需修订产生新版本）")
        model = self._load_and_check_model(
            payload["source_id"], payload["model_id"],
            payload["model_version"])
        # 模型依赖变化 → 定义校验会拒绝（关系修订/指标集合已变）
        self._check_relationships(
            model, self._spec_from_dict(payload), self._repo)
        run_id = uuid4().hex
        run = {"run_id": run_id, "dataset_id": dataset_id,
               "dataset_version": version, "run_type": "validation",
               "status": "running", "started_at": _now(), "finished_at": None,
               "budget": {"max_rows":
                          self._settings.dataset_validation_rows},
               "errors": [], "result": None}
        self._repo.save_dataset_run(run)
        if not self._repo.update_dataset_status(
                dataset_id, version, "validating"):
            run["status"] = "failed"
            run["errors"].append({
                "reason": "status_conflict",
                "message": "状态转换失败（并发状态变更）"})
            run["finished_at"] = _now()
            self._repo.save_dataset_run(run)
            raise ValueError("版本状态被并发变更，验证未执行")
        payload["status"] = "validating"
        try:
            spec = self._spec_from_dict(payload)
            plan = self._compiler.compile(spec, model)
            spec.definition_fingerprint = definition_fingerprint(spec)
            spec.plan_fingerprint = plan.plan_fingerprint
            run_context = self._new_run_context()
            executor = self._executor_factory(
                payload, model, self._settings, run_context=run_context)
            try:
                lineage = plan.lineage
                grain_aliases = list(lineage.keys())[:len(spec.grain_keys)]
                report = run_quality_checks(
                    spec={**spec.to_dict(), "_lineage": lineage,
                          "_grain_aliases": grain_aliases},
                    plan_sql=plan.sql, plan_params=plan.params,
                    executor=executor, model=model,
                    max_rows=self._settings.dataset_validation_rows)
            finally:
                executor.close()
            expired = self._expired(run_context)
            if expired:
                run["errors"].append({
                    "reason": "deadline_exceeded",
                    "message": "质量验证运行超期，不进入 ready"})
            self._repo.save_plan(dataset_id, version, plan.to_dict())
            self._repo.save_quality(dataset_id, version, report.to_dict())
            # 超期一律 failed_validation（超期判定不可被 all_passed 覆盖）
            passed = report.all_passed and not expired
            payload.update({
                "status": "ready" if passed else "failed_validation",
                "plan_fingerprint": plan.plan_fingerprint,
                "definition_fingerprint": spec.definition_fingerprint,
            })
            run["status"] = "succeeded"
            run["result"] = {"all_passed": passed,
                             "checks": len(report.checks)}
        except Exception as exc:  # noqa: BLE001 - 收尾兜底（固定安全错误）
            run["status"] = "failed"
            run["errors"].append({
                "reason": "internal_error",
                "message": f"未预期错误[{type(exc).__name__}]；错误详情已抑制"})
            payload["status"] = "failed_validation"
        finally:
            run["finished_at"] = _now()
            self._repo.save_dataset_run(run)
        self._repo.update_dataset_status(
            dataset_id, version, payload["status"],
            fields={"plan_fingerprint": payload.get("plan_fingerprint"),
                    "definition_fingerprint":
                        payload.get("definition_fingerprint")})
        return {"dataset_id": dataset_id, "version": version,
                "status": payload["status"],
                "quality": self._repo.get_latest_quality(dataset_id, version)}

    def _require_ready(self, dataset_id: str, version: int) -> dict[str, Any]:
        payload = self._repo.get_dataset(dataset_id, version=version)
        if payload is None:
            raise ResourceNotFoundError(
                "dataset", f"数据集版本不存在：{dataset_id} v{version}")
        if payload.get("status") not in ("ready", "published"):
            raise ValueError(
                f"版本状态 {payload.get('status')} 不可用"
                "（需先通过质量验证）")
        return payload

    # -- 发布 -----------------------------------------------------------------

    def publish(self, dataset_id: str, version: int, *,
                operator: str = "local-dev",
                expected_pointer: int | None = None) -> dict[str, Any]:
        payload = self._require_ready(dataset_id, version)
        quality = self._repo.get_latest_quality(dataset_id, version)
        if not quality or not quality.get("all_passed"):
            raise ValueError("质量验证未通过，不能发布")
        if quality.get("definition_fingerprint") != \
                payload.get("definition_fingerprint"):
            raise ValueError("质量报告与当前定义不匹配（定义变化需重验）")
        # 发布前重载 M4 门槛与依赖
        self._load_and_check_model(
            payload["source_id"], payload["model_id"],
            payload["model_version"])
        if payload.get("target") == "view":
            return self._publish_view(payload, operator, expected_pointer)
        return self._publish_es(payload, operator, expected_pointer)

    def _publish_view(self, payload: dict[str, Any], operator: str,
                      expected_pointer: int | None) -> dict[str, Any]:
        if not self._settings.dataset_view_publish_enabled:
            raise ValueError(
                "视图发布目标未启用（dataset_view_publish_enabled=false；"
                "实现已测试，真实发布未验证）")
        publisher: ViewPublisher = self._view_publisher_factory()
        plan = self._repo.get_latest_plan(
            payload["dataset_id"], payload["version"])
        run = self._new_publish_run(payload, "view")
        self._repo.save_dataset_run(run)
        target_schema = self._settings.dataset_view_target_schema
        view_name = self._view_name(payload)
        external_created = False
        lock_acquired = False
        try:
            # 数据集级原子占用（修复轮 3：数据库级互斥）
            if not self._repo.acquire_publish_lock(
                    payload["dataset_id"], run["run_id"], "publish"):
                raise RevisionConflictError(
                    "数据集级发布冲突：另一发布操作进行中"
                    "——同数据集同时只允许一个发布操作")
            lock_acquired = True
            if not self._repo.update_dataset_status(
                    payload["dataset_id"], payload["version"], "publishing"):
                raise RevisionConflictError(
                    "并发发布冲突：版本状态已被其他发布变更（publishing）")
            try:
                publisher.create_versioned_view(
                    target_schema, view_name, plan["sql"], plan["params"])
                external_created = True
            except ValueError as exc:
                if "已存在" not in str(exc):
                    raise
                pointer_version = self._repo.get_published_dataset(
                    payload["source_id"], payload["dataset_id"])
                if (publisher.view_exists(target_schema, view_name)
                        and pointer_version == payload["version"]):
                    # 幂等刷新：目标与指针均已是本版本 → 复用
                    self._repo.update_dataset_status(
                        payload["dataset_id"], payload["version"],
                        "published")
                    run["status"] = "succeeded"
                    run["result"] = {"target": "view", "idempotent": True,
                                     "view": f"{target_schema}.{view_name}"}
                    self._safe_finish_run(run)
                    return {"dataset_id": payload["dataset_id"],
                            "version": payload["version"], "published": True,
                            "target": "view",
                            "view": f"{target_schema}.{view_name}",
                            "idempotent": True}
                raise RevisionConflictError(
                    "并发发布冲突：目标视图已被其他发布创建"
                    "（同版本不重复创建冲突对象）") from None
            if not publisher.view_exists(target_schema, view_name):
                raise ValueError("目标视图创建后核对失败")
            switched = self._repo.set_published_dataset(
                payload["source_id"], payload["dataset_id"],
                payload["version"], operator=operator,
                expected=expected_pointer)
            if not switched:
                raise RevisionConflictError(
                    "发布指针并发冲突：数据集发布指针已被其他发布更新")
            self._repo.update_dataset_status(
                payload["dataset_id"], payload["version"], "published")
            run["status"] = "succeeded"
            run["result"] = {"target": "view",
                             "view": f"{target_schema}.{view_name}"}
        except Exception as exc:
            run["status"] = "failed"
            run["errors"].append({
                "reason": "publish_failed",
                "message": f"发布失败[{type(exc).__name__}]；错误详情已抑制"})
            # 修复轮 3：只有获得了锁的线程才更新状态
            # （未获锁的 409 失败方不覆盖他方状态）
            if lock_acquired:
                target_status = ("needs_reconciliation" if external_created
                                 else "failed_publish")
                self._repo.update_dataset_status(
                    payload["dataset_id"], payload["version"], target_status)
            self._safe_finish_run(run)
            raise
        finally:
            # 修复轮 3：成功与一般失败路径释放锁；needs_reconciliation
            # （外部成功本地失败）保留锁给恢复操作
            if lock_acquired:
                outcome = ("released" if run["status"] != "failed"
                           else "failed")
                self._repo.release_publish_lock(
                    payload["dataset_id"], run["run_id"], outcome=outcome)
            publisher.close()
        self._safe_finish_run(run)
        return {"dataset_id": payload["dataset_id"],
                "version": payload["version"], "published": True,
                "target": "view", "view": f"{target_schema}.{view_name}"}

    def _publish_es(self, payload: dict[str, Any], operator: str,
                    expected_pointer: int | None) -> dict[str, Any]:
        if not self._settings.dataset_es_publish_enabled:
            raise ValueError(
                "ES 发布目标未启用（dataset_es_publish_enabled=false；"
                "实现已测试，真实发布未验证）")
        es = self._es_publisher_factory()
        plan = self._repo.get_latest_plan(
            payload["dataset_id"], payload["version"])
        run = self._new_publish_run(payload, "elasticsearch")
        self._repo.save_dataset_run(run)
        index_name = self._es_index_name(payload)
        alias_name = self._es_alias_name(payload)
        external_created = False
        lock_acquired = False
        try:
            # 数据集级原子占用（修复轮 3：数据库级互斥）
            if not self._repo.acquire_publish_lock(
                    payload["dataset_id"], run["run_id"], "publish"):
                raise RevisionConflictError(
                    "数据集级发布冲突：另一发布操作进行中"
                    "——同数据集同时只允许一个发布操作")
            lock_acquired = True
            if not self._repo.update_dataset_status(
                    payload["dataset_id"], payload["version"], "publishing"):
                raise RevisionConflictError(
                    "并发发布冲突：版本状态已被其他发布变更（publishing）")
            mapping = self._es_mapping(payload, plan)
            es.create_version_index(index_name, mapping)
            external_created = True
            exported = self._export_to_es(
                payload, plan, es, index_name, run)
            expected_rows = exported["expected_rows"]
            actual = es.count(index_name)
            if actual != expected_rows:
                raise ValueError(
                    f"ES 文档数与预期不符（{actual}!={expected_rows}）；"
                    "不切换正式别名")
            previous = self._repo.get_published_dataset(
                payload["source_id"], payload["dataset_id"])
            previous_index = (
                self._es_index_name({**payload, "version": previous})
                if previous else None)
            es.switch_alias(alias_name, index_name, previous_index)
            switched = self._repo.set_published_dataset(
                payload["source_id"], payload["dataset_id"],
                payload["version"], operator=operator,
                expected=expected_pointer)
            if not switched:
                raise RevisionConflictError(
                    "发布指针并发冲突：数据集发布指针已被其他发布更新")
            self._repo.update_dataset_status(
                payload["dataset_id"], payload["version"], "published")
            run["status"] = "succeeded"
            run["result"] = {"target": "elasticsearch",
                             "index": index_name, "alias": alias_name,
                             "documents": actual}
        except Exception as exc:
            run["status"] = "failed"
            run["errors"].append({
                "reason": "publish_failed",
                "message": f"发布失败[{type(exc).__name__}]；错误详情已抑制"})
            # 修复轮 3：只有获得了锁的线程才更新状态
            if lock_acquired:
                self._repo.update_dataset_status(
                    payload["dataset_id"], payload["version"],
                    "needs_reconciliation" if external_created
                    else "failed_publish")
            self._safe_finish_run(run)
            raise
        finally:
            # 修复轮 5：与 view 路径一致——获得锁即始终释放（含
            # needs_reconciliation：保留 active 锁会卡死 reconcile，
            # 而 reconcile 正是恢复路径，也须经 acquire 取得占用）
            if lock_acquired:
                outcome = ("released" if run["status"] != "failed"
                           else "failed")
                self._repo.release_publish_lock(
                    payload["dataset_id"], run["run_id"], outcome=outcome)
        self._safe_finish_run(run)
        return {"dataset_id": payload["dataset_id"],
                "version": payload["version"], "published": True,
                "target": "elasticsearch", "index": index_name,
                "alias": alias_name, "documents": actual}

    def _export_to_es(self, payload: dict[str, Any], plan: dict[str, Any],
                      es: Any, index_name: str, run: dict[str, Any]) \
            -> dict[str, Any]:
        model = self._models.get_model(
            payload["model_id"], version=payload["model_version"])
        # 导出分页循环同样受预算+期限+字节上限约束（修复轮 5）
        executor = self._executor_factory(
            payload, model, self._settings,
            run_context=self._new_run_context())
        batch = self._settings.dataset_export_batch_size
        max_rows = self._settings.dataset_export_max_rows
        grain_aliases = list(plan["lineage"].keys())[
            :len(payload["grain_keys"])]
        exported = 0
        try:
            offset_alias = "__row_number__"
            # 分批读取（键集分页首版用行号窗口简化：按 grain 键排序 + OFFSET）
            order_by = ", ".join(
                f"{plan['sql'].split('FROM')[0].split(',', 1)[0]}"
                for _ in [])  # 占位：下方直接改写 SQL
            del order_by, offset_alias
            base_sql = plan["sql"].rstrip(";")
            ordered = base_sql + "\nORDER BY " + ", ".join(
                f"`{a}`" for a in grain_aliases)
            while exported < max_rows:
                page_sql = (f"{ordered}\nLIMIT {batch} OFFSET {exported}")
                rows, _ = executor.execute(
                    page_sql, plan["params"], max_rows=batch,
                    timeout_ms=60_000)
                if not rows:
                    break
                # 敏感字段排除（修复轮）：沿血缘回溯属性 sensitive 标签，
                # 输出别名改写不能绕过
                sensitive_aliases = self._sensitive_aliases(
                    plan, grain_aliases)
                documents = []
                for row in rows:
                    grain_values = [row.get(a) for a in grain_aliases]
                    if any(v is None for v in grain_values):
                        raise ValueError(
                            "grain 键存在 NULL：阻断导出（不静默丢行/覆盖）")
                    doc_id = grain_document_id(grain_values)
                    source = {
                        k: _es_value(v) for k, v in row.items()
                        if k not in sensitive_aliases}
                    documents.append({"_id": doc_id, "_source": source})
                result = es.bulk_index(index_name, documents)
                exported += result["indexed"]
                run["result"] = {"exported": exported}
                if len(rows) < batch:
                    break
        finally:
            executor.close()
        if exported >= max_rows:
            raise ValueError(
                f"达到导出硬上限 {max_rows}：发布失败（不切换正式别名）")
        return {"expected_rows": exported}

    @staticmethod
    def _sensitive_aliases(
        plan: dict[str, Any], grain_aliases: list[str],
    ) -> set[str]:
        """沿血缘回溯输出别名的敏感标签（改别名不能绕过）。"""
        sensitive: set[str] = set()
        for alias, info in (plan.get("lineage") or {}).items():
            if info.get("role") in ("grain_key",):
                continue  # grain 键为结构化标识，不按敏感排除
            if info.get("sensitive"):
                sensitive.add(alias)
        return sensitive

    def _es_mapping(self, payload: dict[str, Any],
                    plan: dict[str, Any]) -> dict[str, Any]:
        mapping: dict[str, Any] = {"properties": {}}
        for alias, info in plan["lineage"].items():
            field_type = "keyword"  # 默认结构化键
            if info.get("role") in ("metric", "preaggregated_metric"):
                field_type = "scaled_float" if not _is_integral(
                    info) else "long"
            elif info.get("role") == "field":
                field_type = "keyword"
            mapping["properties"][alias] = {"type": field_type}
        return mapping

    # -- 刷新/回滚/恢复 ------------------------------------------------------------

    def refresh(self, dataset_id: str, *, operator: str = "local-dev") \
            -> dict[str, Any]:
        """刷新：复制当前定义创建新版本→验证→发布（修复轮 3）。

        原发布版本、质量报告与物理目标**保持不变**（不重置 draft）。
        新版本使用新的视图/索引名称；成功后指针自动切换。
        质量失败：旧发布版本及别名保持有效。
        """
        current = self._repo.get_published_dataset_version(dataset_id)
        if current is None:
            raise ResourceNotFoundError(
                "dataset", f"数据集没有已发布版本：{dataset_id}")
        source_payload = self._repo.get_dataset(dataset_id, version=current)
        if source_payload is None:
            raise ResourceNotFoundError(
                "dataset", f"已发布版本不存在：{dataset_id} v{current}")
        # 复制定义创建新版本（原版本不动）
        latest_payload = self._repo.get_dataset(dataset_id)
        new_version = (latest_payload["version"] + 1
                       if latest_payload else current + 1)
        revised = copy.deepcopy(source_payload)
        revised.update({
            "version": new_version,
            "based_on_version": current,
            "created_by": operator,
            "created_at": now_iso(),
            "change_note": f"刷新（基于 v{current}）",
            "status": "draft",
            "plan_fingerprint": None,
            "definition_fingerprint": None,
        })
        revised.pop("sql_plan", None)
        revised.pop("quality_report", None)
        revised.pop("published", None)
        try:
            self._repo.save_dataset(revised)
        except RevisionConflictError:
            # 版本号冲突 → 重取最新
            latest = self._repo.get_dataset(dataset_id)
            revised["version"] = latest["version"] + 1
            self._repo.save_dataset(revised)
        new_v = revised["version"]
        # 验证新版本（失败时旧版本不受影响）
        report = self.validate(dataset_id, new_v)
        if report.get("status") != "ready":
            return {"dataset_id": dataset_id, "version": new_v,
                    "refreshed": False,
                    "reason": f"质量验证未通过（v{current} 保持发布）",
                    "report": report}
        # 发布新版本（成功后指针自动切换到新版本）
        published = self.publish(dataset_id, new_v, operator=operator)
        return {"dataset_id": dataset_id, "version": new_v,
                "refreshed": True, "previous_version": current,
                "published": published.get("published", False)}

    def rollback(self, dataset_id: str, target_version: int, *,
                 operator: str = "local-dev",
                 expected_pointer: int | None = None) -> dict[str, Any]:
        """回滚：复用 dataset_publish_locks 在外部副作用前取得占用。"""
        target = self._repo.get_dataset(dataset_id, version=target_version)
        if target is None or target.get("status") != "published":
            raise ValueError(
                f"回滚目标必须是仍处 published 状态的版本：v{target_version}")
        rollback_op = uuid4().hex
        if not self._repo.acquire_publish_lock(
                dataset_id, rollback_op, "rollback"):
            raise RevisionConflictError(
                "数据集级发布冲突：另一操作进行中，回滚被拒绝")
        try:
            # 核对期望指针（在外部切换前拒绝）
            current_pointer = self._repo.get_published_dataset(
                target["source_id"], dataset_id)
            if (expected_pointer is not None
                        and current_pointer != expected_pointer):
                raise RevisionConflictError(
                    f"回滚期望指针不匹配：当前 v{current_pointer}，"
                    f"期望 v{expected_pointer}")
            # ES 目标：先切换别名再切本地指针
            if (target.get("target") == "elasticsearch"
                        and self._settings.dataset_es_publish_enabled):
                es = self._es_publisher_factory()
                if current_pointer and current_pointer != target_version:
                    current_payload = self._repo.get_dataset(
                        dataset_id, version=current_pointer)
                    previous_index = (
                        self._es_index_name(current_payload)
                        if current_payload else None)
                    es.switch_alias(
                        self._es_alias_name(target),
                        self._es_index_name(target), previous_index)
            switched = self._repo.set_published_dataset(
                target["source_id"], dataset_id, target_version,
                operator=operator, expected=expected_pointer,
                action="rollback")
            if not switched:
                raise RevisionConflictError("发布指针并发冲突（回滚）")
        finally:
            self._repo.release_publish_lock(dataset_id, rollback_op)
        return {"dataset_id": dataset_id, "version": target_version,
                "rolled_back": True}

    def reconcile(self, dataset_id: str, version: int) -> dict[str, Any]:
        """显式核对结果不明的发布尝试（修复轮：完成指针+释放锁）。"""
        payload = self._repo.get_dataset(dataset_id, version=version)
        if payload is None:
            raise ResourceNotFoundError("dataset", "数据集不存在")
        if payload.get("status") != "needs_reconciliation":
            raise ValueError(
                f"版本状态 {payload.get('status')} 无需核对")
        # 恢复也须取得占用（不绕过其他操作）
        reconcile_op = uuid4().hex
        if not self._repo.acquire_publish_lock(
                dataset_id, reconcile_op, "reconcile"):
            raise RevisionConflictError(
                "数据集级发布冲突：另一操作进行中，恢复被拒绝")
        try:
            return self._reconcile_locked(dataset_id, version, payload)
        finally:
            self._repo.release_publish_lock(dataset_id, reconcile_op)

    def _reconcile_locked(
        self, dataset_id: str, version: int, payload: dict[str, Any],
    ) -> dict[str, Any]:
        def _commit_pointer() -> bool:
            """条件更新指针+审计；失败不返回成功。"""
            switched = self._repo.set_published_dataset(
                payload["source_id"], dataset_id, version,
                operator="reconcile")
            if not switched:
                return False
            return self._repo.update_dataset_status(
                dataset_id, version, "published")

        if payload.get("target") == "view":
            publisher = self._view_publisher_factory()
            view_name = self._view_name(payload)
            exists = publisher.view_exists(
                self._settings.dataset_view_target_schema, view_name)
            publisher.close()
            if exists:
                # 修复轮：提交指针+审计+状态（不能只改 status）
                if not _commit_pointer():
                    return {"dataset_id": dataset_id, "version": version,
                            "reconciled": "pointer_commit_conflict"}
                return {"dataset_id": dataset_id, "version": version,
                        "reconciled": "external_object_exists",
                        "pointer_restored": True}
            self._repo.update_dataset_status(dataset_id, version,
                                            "failed_publish")
            return {"dataset_id": dataset_id, "version": version,
                    "reconciled": "external_object_missing"}

        es = self._es_publisher_factory()
        index_name = self._es_index_name(payload)
        try:
            count = es.count(index_name)
        except Exception:  # noqa: BLE001 - 索引不存在
            self._repo.update_dataset_status(dataset_id, version,
                                            "failed_publish")
            return {"dataset_id": dataset_id, "version": version,
                    "reconciled": "external_index_missing",
                    "documents": 0, "expected": None}
        # 预期文档数：从发布运行记录获取（可能为 None→不恢复）
        expected = None
        for run in self._repo.list_dataset_runs(dataset_id):
            if (run.get("run_type") == "publish"
                    and run.get("dataset_version") == version
                    and isinstance(run.get("result"), dict)):
                documents = run["result"].get("documents")
                if documents is not None:
                    expected = documents
                    break
        if expected is None:
            # 无预期记录：保留待恢复（不凭存在判成功）
            return {"dataset_id": dataset_id, "version": version,
                    "reconciled": "expected_unknown",
                    "documents": count, "expected": None}
        # 完整性核对：文档数与预期一致才恢复
        if count == expected:
            if not _commit_pointer():
                return {"dataset_id": dataset_id, "version": version,
                        "reconciled": "pointer_commit_conflict",
                        "documents": count, "expected": expected}
            return {"dataset_id": dataset_id, "version": version,
                    "reconciled": "external_index_complete",
                    "documents": count, "expected": expected}
        if expected == 0 and count == 0:
            if not _commit_pointer():
                return {"dataset_id": dataset_id, "version": version,
                        "reconciled": "pointer_commit_conflict"}
            return {"dataset_id": dataset_id, "version": version,
                    "reconciled": "empty_dataset_confirmed",
                    "documents": 0, "expected": 0}
        self._repo.update_dataset_status(dataset_id, version,
                                        "failed_publish")
        return {"dataset_id": dataset_id, "version": version,
                "reconciled": "external_index_incomplete",
                "documents": count, "expected": expected}

    # -- 命名/工具 -----------------------------------------------------------------

    def _view_name(self, payload: dict[str, Any]) -> str:
        return (f"{self._settings.dataset_view_prefix}"
                f"{payload['dataset_id']}_v{payload['version']}")

    def _es_index_name(self, payload: dict[str, Any]) -> str:
        return (f"{self._settings.es_index_prefix}_{payload['dataset_id']}"
                f"_v{payload['version']}").lower()

    def _es_alias_name(self, payload: dict[str, Any]) -> str:
        return f"{self._settings.es_index_prefix}_{payload['dataset_id']}".lower()

    def _new_publish_run(self, payload: dict[str, Any], target: str) \
            -> dict[str, Any]:
        return {"run_id": uuid4().hex, "dataset_id": payload["dataset_id"],
                "dataset_version": payload["version"],
                "run_type": "publish", "target": target,
                "status": "running", "started_at": _now(),
                "finished_at": None,
                "budget": {"batch": self._settings.dataset_export_batch_size,
                           "max_rows":
                           self._settings.dataset_export_max_rows},
                "errors": [], "result": None}

    def _safe_finish_run(self, run: dict[str, Any]) -> None:
        run["finished_at"] = _now()
        try:
            self._repo.save_dataset_run(run)
        except Exception:  # noqa: BLE001 - 收尾失败不覆盖主要错误
            pass


def _check_relationship_valid(model: dict[str, Any], repo: Any,
                              relationship_key: str,
                              snapshot_id: str | None) -> None:
    relations = {r["relationship_key"]: r
                 for r in model.get("relationships", [])}
    if relationship_key not in relations:
        raise ValueError(f"关系不在模型中：{relationship_key}")
    effective = repo.get_effective_relationship(
        relationship_key, snapshot_id=snapshot_id)
    if (effective is None
            or effective.get("status") != "validated"
            or effective.get("revision")
            != relations[relationship_key].get("revision")):
        raise ValueError(
            f"关系不再是该快照的有效 validated 修订：{relationship_key}")


def _es_value(value: Any) -> Any:
    from decimal import Decimal as _Decimal

    if isinstance(value, _Decimal):
        return str(value)  # 金额精度：字符串承载，不无提示转 float
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("二进制字段不支持导出")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _is_integral(info: dict[str, Any]) -> bool:
    return info.get("metric_id", "").endswith("count_rows") or \
        info.get("metric_id", "").endswith("count_distinct")
