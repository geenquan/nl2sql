"""M4 语义模型用例编排：构建草稿 → 人工修订/确认 → 检查 → 发布 → 差异/回滚。

约束：
- 一次构建限定一个 source+snapshot（默认 M1 发布目录快照；部分快照需显式
  allow_partial 且保留 input_complete=false 与缺失说明；显式快照校验归属）；
- 消费 M2 effective（rejected 不作正向依据）与 M3 同快照 effective validated
  关系（含修订号），不跨快照借用；
- 模型版本不可变：修订/确认产生新版本（基于基线版本的并发修订由
  (model_id, version) 唯一约束仲裁 → 409）；发布/回滚只原子切换指针并审计；
- 发布前重新核对（grain 已确认、正式指标口径已确认、引用有效、无未处理
  阻断、非未获允许的部分快照、依赖修订未降级、输入快照仍为当前发布快照）；
- 依赖降级：M3 关系被拒绝/标 stale 或目录快照更替 → needs_revalidation /
  snapshot_changed，模型可查看导出但不冒充当前可用；源库实际变化未扫描的
  情况无法检测（如实说明）；
- 指标修改均产生新版本；受限聚合/过滤操作符/类型化值，服务端核验字段引用，
  不接受任意 SQL/Python 表达式，不以完整 JSON 绕过检查；
- 错误消息沿用固定安全文案（未预期异常只含类型名+阶段+标识）。
"""

import copy
import json
import time as _time
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import (
    ResourceNotFoundError,
    RevisionConflictError,
)
from app.domain.relationships import now_iso
from app.domain.semantic_model import (
    METRIC_AGGREGATIONS,
    METRIC_FILTER_OPERATORS,
)
from app.semantics.model_builder import SemanticModelBuilder


class SemanticModelService:
    def __init__(
        self,
        metadata_repository: Any,  # 同时实现 MetadataRepository 与 SemanticModelRepository
        settings: Settings,
        builder: SemanticModelBuilder | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._repo = metadata_repository
        self._settings = settings
        self._builder = builder or SemanticModelBuilder()
        self._clock = clock or _time.monotonic

    # -- 构建 -----------------------------------------------------------------

    def build_model(
        self, source_id: str, *, snapshot_id: str | None = None,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        """从当前 M1/M2/M3 结果构建模型草稿（新版本；不覆盖人工版本，
        不切换发布指针）。"""
        if self._repo.get_source(source_id) is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        snapshot_id, input_complete = self._resolve_snapshot(
            source_id, snapshot_id, allow_partial)
        tables = self._repo.list_table_metadata(source_id, snapshot_id=snapshot_id)
        semantics = self._repo.list_field_semantics_by_source(
            source_id, snapshot_id, effective=True)
        purposes = {
            table["table_id"]: (self._repo.get_table_purpose(
                table["table_id"], effective=True) or {})
            for table in tables
        }
        relationships = [
            record for record in self._repo.list_relationships(
                source_id, snapshot_id=snapshot_id, effective_only=True)
            if record.get("status") == "validated"
        ]
        model_id = f"sm-{source_id[:32]}"
        latest = self._repo.get_model(model_id)
        version = (latest["version"] + 1) if latest else 1
        model = self._builder.build(
            source_id=source_id, snapshot_id=snapshot_id,
            model_id=model_id, version=version,
            tables=tables, semantics=semantics, purposes=purposes,
            relationships=relationships,
            input_complete=input_complete,
            partial_missing=(
                [] if input_complete else ["部分快照：缺失表未计入（input_complete=false）"]),
            sensitive_keywords=tuple(self._settings.sensitive_column_keywords),
            max_hops=self._settings.semantic_model_max_hops,
            max_paths_per_pair=self._settings.semantic_model_max_paths_per_pair,
            max_topics=self._settings.semantic_model_max_topics,
            deadline_monotonic=self._clock()
            + self._settings.semantic_model_build_deadline_seconds,
            clock=self._clock,
        )
        payload = model.to_dict()
        try:
            self._repo.save_model(payload)
        except RevisionConflictError:
            raise RevisionConflictError(
                "模型版本冲突：存在并发的重新构建，请重试（重新构建不覆盖人工版本）"
            ) from None
        return self._enrich(payload)

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
                    "当前发布目录来自部分成功快照（输入不完整）；"
                    "确需构建请传 allow_partial=true（模型将保留 input_complete=false）")
            return published["snapshot_id"], bool(published.get("complete", True))
        jobs = [j for j in self._repo.list_scan_jobs(source_id)
                if j.get("snapshot_id") == snapshot_id]
        if not jobs:
            raise ResourceNotFoundError(
                "snapshot",
                f"快照不属于该数据源或没有对应扫描任务：{snapshot_id}")
        complete = jobs[0].get("status") == "succeeded"
        if not complete and not allow_partial:
            raise ValueError(
                f"目标快照为部分成功（输入不完整）：{snapshot_id}；"
                "确需构建请传 allow_partial=true（模型将保留 input_complete=false）")
        return snapshot_id, complete

    # -- 读取（含可用性评估） ---------------------------------------------------

    def list_models(self, source_id: str) -> list[dict[str, Any]]:
        if self._repo.get_source(source_id) is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        return [self._enrich(m) for m in self._repo.list_models(source_id)]

    def get_model(self, model_id: str,
                  version: int | None = None) -> dict[str, Any]:
        model = self._repo.get_model(model_id, version=version)
        if model is None:
            raise ResourceNotFoundError(
                "semantic_model", f"语义模型不存在：{model_id}"
                + (f" v{version}" if version is not None else ""))
        return self._enrich(model)

    def list_versions(self, model_id: str) -> list[dict[str, Any]]:
        versions = self._repo.list_versions(model_id)
        if not versions:
            raise ResourceNotFoundError(
                "semantic_model", f"语义模型不存在：{model_id}")
        return [self._enrich(v) for v in versions]

    def get_published(self, source_id: str) -> dict[str, Any] | None:
        published = self._repo.get_published(source_id)
        if published is None or published.get("model") is None:
            return None
        published["model"] = self._enrich(published["model"])
        return published

    def publication_log(self, source_id: str) -> list[dict[str, Any]]:
        return self._repo.list_publication_log(source_id)

    def _enrich(self, model: dict[str, Any]) -> dict[str, Any]:
        """附加依赖可用性（不修改已存版本内容）。"""
        deps = model.get("dependencies") or {}
        relations_status: dict[str, str] = {}
        for key in (deps.get("relationship_revisions") or {}):
            effective = self._repo.get_effective_relationship(
                key, snapshot_id=model.get("snapshot_id"))
            relations_status[key] = (
                "ok" if effective and effective.get("status") == "validated"
                else "needs_revalidation")
        published_snapshot = self._repo.get_published_snapshot(model["source_id"])
        snapshot_ok = bool(
            published_snapshot
            and published_snapshot["snapshot_id"] == model.get("snapshot_id"))
        if not snapshot_ok:
            dependency_status = "snapshot_changed"
        elif any(v != "ok" for v in relations_status.values()):
            dependency_status = "needs_revalidation"
        else:
            dependency_status = "ok"
        pointer = self._repo.get_published(model["source_id"])
        is_published = bool(pointer and pointer["model_id"] == model["model_id"]
                            and pointer["version"] == model["version"])
        enriched = dict(model)
        enriched["availability"] = {
            "dependency_status": dependency_status,
            "relationship_status": relations_status,
            "snapshot_current": snapshot_ok,
            "is_published_version": is_published,
            "usable_as_current": dependency_status == "ok",
            "note": (
                "可用性基于本地元数据依赖检查（M3 有效状态与目录快照指针）；"
                "源库实际变化但尚未重新扫描的情况无法检测"
                if dependency_status == "ok" else
                "依赖已降级：模型可查看/导出，不能冒充当前快照可用模型"),
        }
        enriched["formal_metric_ids"] = [
            m["metric_id"] for m in model.get("metrics", [])
            if m.get("status") in ("business_confirmed", "published")]
        return enriched

    # -- 人工修订（新版本，服务端核验引用） ---------------------------------------

    def revise_model(
        self, model_id: str, base_version: int, changes: dict[str, Any],
        *, reviewer: str = "local-dev", note: str | None = None,
    ) -> dict[str, Any]:
        base = self._repo.get_model(model_id, version=base_version)
        if base is None:
            raise ResourceNotFoundError(
                "semantic_model", f"基线版本不存在：{model_id} v{base_version}")
        revised = copy.deepcopy(base)
        revised.update({
            "version": base_version + 1,
            "based_on_version": base_version,
            "created_at": now_iso(),
            "created_by": reviewer,  # 本地开发输入，不宣称身份认证
            "change_note": note,
        })
        self._apply_changes(revised, changes, reviewer)
        try:
            self._repo.save_model(revised)
        except RevisionConflictError:
            raise RevisionConflictError(
                f"修订冲突：{model_id} v{base_version + 1} 已存在"
                f"（并发基于 v{base_version} 的修订只有一个成功）") from None
        return self._enrich(revised)

    def _apply_changes(
        self, model: dict[str, Any], changes: dict[str, Any], reviewer: str
    ) -> None:
        topics = {t["topic_id"]: t for t in model.get("topics", [])}
        metrics = {m["metric_id"]: m for m in model.get("metrics", [])}
        attributes = {
            a["attribute_id"]: a for e in model.get("entities", [])
            for a in e.get("attributes", [])}
        fields_by_ref = {
            (a["field"]["schema"], a["field"]["table"], a["field"]["column"]): a
            for a in attributes.values()}
        paths = {p["path_id"]: p for p in model.get("paths", [])}

        for topic_id, topic_change in (changes.get("topic_grains") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            statement = (topic_change.get("statement") or "").strip()
            if not statement:
                raise ValueError(f"grain 业务描述不能为空：{topic_id}")
            keys = topic_change.get("keys") or topic["grain"].get("keys") or []
            central = next(
                (e for e in model.get("entities", [])
                 if e["entity_id"] == topic["central_entity_id"]), None)
            if central is None or not keys:
                raise ValueError(f"主题缺少结构粒度键：{topic_id}")
            entity_columns = [
                a["field"]["column"] for a in central.get("attributes", [])]
            unknown_keys = [k for k in keys if k not in entity_columns]
            if unknown_keys:
                raise ValueError(
                    f"粒度键不存在于中心表：{','.join(unknown_keys)}")
            if not topic_change.get("confirm"):
                raise ValueError(f"grain 修订必须显式 confirm=true：{topic_id}")
            topic["grain"].update({
                "statement": statement, "keys": list(keys),
                "confirmed": True, "confirmed_by": reviewer,
                "missing": [],
            })

        for topic_id, name in (changes.get("topic_names") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            if not (name or "").strip():
                raise ValueError(f"主题名称不能为空：{topic_id}")
            topic["name"] = name.strip()

        for attribute_id in changes.get("confirm_attributes") or []:
            attribute = attributes.get(attribute_id)
            if attribute is None:
                raise ValueError(f"未知属性：{attribute_id}")
            attribute["status"] = "business_confirmed"

        for metric_id, metric_change in (changes.get("metrics") or {}).items():
            metric = metrics.get(metric_id)
            if metric is None:
                raise ValueError(f"未知指标：{metric_id}")
            self._apply_metric_change(
                metric, metric_change, fields_by_ref, reviewer)

        for topic_id, path_id in (changes.get("selected_paths") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            path = paths.get(path_id)
            if path is None or path.get("topic_id") != topic_id:
                raise ValueError(f"路径不属于该主题：{path_id}")
            if path.get("status") == "blocked":
                raise ValueError(f"阻断路径不可选为正式路径：{path_id}")
            # 人工消歧：选定路径标记 available，其余同对路径标记已否决
            path["status"] = "available"
            path["status_reasons"] = [
                r for r in path.get("status_reasons", []) if "多条路径" not in r]
            for other in model.get("paths", []):
                if (other["path_id"] != path_id
                        and other["topic_id"] == topic_id
                        and other["to"]["table"] == path["to"]["table"]
                        and other["to"]["schema"] == path["to"]["schema"]):
                    other["status"] = "needs_review"
                    other.setdefault("status_reasons", []).append(
                        "人工已选择其他路径（本路径未被选用）")

    def _apply_metric_change(
        self, metric: dict[str, Any], change: dict[str, Any],
        fields_by_ref: dict[tuple[str, str, str], dict[str, Any]],
        reviewer: str,
    ) -> None:
        action = change.get("action")
        if action not in ("confirm", "reject", "update"):
            raise ValueError(f"未知指标动作：{action}")
        if change.get("aggregation") is not None \
                and change["aggregation"] != metric["aggregation"]:
            # 聚合口径变更：校验受限聚合合法性后更新（口径修改=新版本）
            if change["aggregation"] not in METRIC_AGGREGATIONS:
                raise ValueError(f"非法聚合函数：{change['aggregation']}")
            metric["aggregation"] = change["aggregation"]
        if change.get("unit") is not None:
            metric["unit"] = str(change["unit"])
        if change.get("time_basis") is not None:
            metric["time_basis"] = str(change["time_basis"])
        if change.get("time_field") is not None:
            ref = change["time_field"]
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            if key not in fields_by_ref:
                raise ValueError(f"time_field 引用不存在的字段：{ref}")
            metric["time_field"] = ref
        if change.get("filters") is not None:
            metric["filters"] = self._validated_filters(
                change["filters"], fields_by_ref)
        if action == "reject":
            metric["status"] = "suggested"
            metric["business_confirmation"] = None
            metric.setdefault("blocking", []).append("人工否决（不进入正式集合）")
            return
        if action == "confirm":
            if metric["aggregation"] in ("sum", "avg") and not metric.get("unit"):
                raise ValueError(f"确认 {metric['metric_id']} 前必须明确单位")
            if not metric.get("time_basis"):
                raise ValueError(f"确认 {metric['metric_id']} 前必须明确时间口径")
            if not change.get("skip_status_filter") and any(
                    "状态过滤" in b for b in metric.get("blocking", [])):
                raise ValueError(
                    f"确认 {metric['metric_id']} 前必须处理状态过滤口径"
                    "（提供 filters 或显式 skip_status_filter=true）")
            metric["status"] = "business_confirmed"
            metric["business_confirmation"] = {
                "confirmed_by": reviewer, "confirmed_at": now_iso(),
                "note": change.get("note"),
            }
            metric["blocking"] = [
                b for b in metric.get("blocking", [])
                if ("时间口径" in b or "单位" in b or "计量口径" in b
                    or "状态过滤" in b or "过滤口径" in b)
                and not (
                    ("时间口径" in b and metric.get("time_basis"))
                    or ("单位" in b and metric.get("unit"))
                    or ("计量口径" in b and metric.get("unit"))
                    or ("状态过滤" in b and (metric.get("filters")
                                             or change.get("skip_status_filter")))
                    or ("过滤口径" in b and metric.get("filters")))]

    @staticmethod
    def _validated_filters(
        raw_filters: list[dict[str, Any]],
        fields_by_ref: dict[tuple[str, str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validated: list[dict[str, Any]] = []
        for raw in raw_filters:
            ref = raw.get("field") or {}
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            attribute = fields_by_ref.get(key)
            if attribute is None:
                raise ValueError(f"过滤条件引用不存在的字段：{ref}")
            operator = raw.get("operator")
            if operator not in METRIC_FILTER_OPERATORS:
                raise ValueError(f"非法过滤操作符：{operator}")
            value = raw.get("value")
            if operator in ("is_null", "not_null"):
                value = None
            elif operator in ("in", "not_in"):
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{operator} 过滤需要非空列表值")
                value = [v if isinstance(v, (int, float, str, bool)) else str(v)
                         for v in value]
            data_type = (attribute.get("data_type") or "").lower()
            numeric_field = any(t in data_type for t in
                                ("int", "decimal", "float", "double", "numeric"))
            if numeric_field and operator not in ("is_null", "not_null"):
                if isinstance(value, list):
                    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               for v in value):
                        raise ValueError(f"数值字段的过滤值必须是数字：{ref}")
                elif (value is not None
                        and (not isinstance(value, (int, float))
                             or isinstance(value, bool))):
                    raise ValueError(f"数值字段的过滤值必须是数字：{ref}")
            validated.append({"field": ref, "operator": operator, "value": value})
        return validated

    # -- 检查 / 发布 / 回滚 -------------------------------------------------------

    def validate_model(self, model_id: str, version: int) -> dict[str, Any]:
        model = self._repo.get_model(model_id, version=version)
        if model is None:
            raise ResourceNotFoundError(
                "semantic_model", f"模型版本不存在：{model_id} v{version}")
        return self._validation_report(model)

    def _validation_report(self, model: dict[str, Any]) -> dict[str, Any]:
        issues: list[str] = []
        checks: dict[str, Any] = {}
        enriched = self._enrich(model)
        availability = enriched["availability"]
        checks["dependency_status"] = availability["dependency_status"]
        if availability["dependency_status"] != "ok":
            issues.append(
                f"依赖不可用：{availability['dependency_status']}"
                "（需重验关系或基于当前发布快照重建）")
        checks["input_complete"] = (model.get("dependencies") or {}).get(
            "input_complete", True)
        if not checks["input_complete"]:
            issues.append("输入为部分快照（未获显式许可不得发布）")

        unconfirmed_grains = [
            t["topic_id"] for t in model.get("topics", [])
            if not (t.get("grain") or {}).get("confirmed")]
        checks["grain_confirmed"] = not unconfirmed_grains
        if unconfirmed_grains:
            issues.append(f"主题 grain 未确认：{','.join(unconfirmed_grains[:5])}")

        formal = [m for m in model.get("metrics", [])
                  if m.get("status") in ("business_confirmed", "published")]
        checks["formal_metric_count"] = len(formal)
        for metric in formal:
            if metric.get("blocking"):
                issues.append(
                    f"正式指标仍有未确认项：{metric['metric_id']}"
                    f"（{';'.join(metric['blocking'][:3])}）")
        unconfirmed_metrics = [
            m["metric_id"] for m in model.get("metrics", [])
            if m.get("status") == "suggested"]
        checks["suggested_metric_count"] = len(unconfirmed_metrics)

        ambiguous_paths = [
            p["path_id"] for p in model.get("paths", [])
            if p.get("status") != "available"]
        checks["path_issues"] = ambiguous_paths
        blocked_affects_formal = any(
            path.get("status") == "blocked"
            and any(m.get("status") in ("business_confirmed", "published")
                    for m in model.get("metrics", [])
                    if m.get("entity_id") and path.get("to", {}).get("table")
                    and m["metric_id"].startswith(
                        f"metric.app.{path['to']['table']}."))
            for path in model.get("paths", []))
        if blocked_affects_formal:
            issues.append("存在阻断路径影响正式指标（重复计数风险未解决）")
        return {"valid": not issues, "issues": issues, "checks": checks}

    def publish_model(
        self, model_id: str, version: int, *, reviewer: str = "local-dev",
        note: str | None = None, expected_pointer: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """发布语义模型＝原子切换下游使用指针（不发布数据集、不执行 SQL）。"""
        model = self._repo.get_model(model_id, version=version)
        if model is None:
            raise ResourceNotFoundError(
                "semantic_model", f"模型版本不存在：{model_id} v{version}")
        report = self._validation_report(model)
        if not report["valid"]:
            raise ValueError(
                "发布检查未通过：" + "；".join(report["issues"]))
        ok = self._repo.set_published(
            model["source_id"], model_id, version,
            operator=reviewer, reason=note, action="publish",
            expected=expected_pointer)
        if not ok:
            raise RevisionConflictError(
                "发布指针并发冲突：当前指针已被其他发布/回滚更新，请刷新后重试")
        return {
            "model_id": model_id, "version": version,
            "published": True, "operator": reviewer,
            "report": report,
        }

    def rollback_model(
        self, model_id: str, target_version: int, *,
        reviewer: str = "local-dev", reason: str | None = None,
        expected_pointer: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """回滚＝指针原子切回历史版本；目标版本必须当前仍可用（依赖未降级），
        不恢复失效关系的正式使用资格。全部历史保留。"""
        model = self._repo.get_model(model_id, version=target_version)
        if model is None:
            raise ResourceNotFoundError(
                "semantic_model", f"回滚目标版本不存在：{model_id} v{target_version}")
        report = self._validation_report(model)
        if not report["valid"]:
            raise ValueError(
                "回滚目标版本当前不可用（依赖降级或检查未通过）："
                + "；".join(report["issues"]))
        ok = self._repo.set_published(
            model["source_id"], model_id, target_version,
            operator=reviewer, reason=reason, action="rollback",
            expected=expected_pointer)
        if not ok:
            raise RevisionConflictError(
                "发布指针并发冲突：当前指针已被其他发布/回滚更新，请刷新后重试")
        return {
            "model_id": model_id, "version": target_version,
            "rolled_back": True, "operator": reviewer, "report": report,
        }

    # -- 差异与导出 ---------------------------------------------------------------

    def diff_models(
        self, model_id: str, version_a: int, version_b: int,
    ) -> dict[str, Any]:
        model_a = self._repo.get_model(model_id, version=version_a)
        model_b = self._repo.get_model(model_id, version=version_b)
        for version, model in ((version_a, model_a), (version_b, model_b)):
            if model is None:
                raise ResourceNotFoundError(
                    "semantic_model", f"模型版本不存在：{model_id} v{version}")
        return {
            "model_id": model_id,
            "version_a": version_a, "version_b": version_b,
            "topics": self._diff_section(
                model_a.get("topics", []), model_b.get("topics", []), "topic_id"),
            "entities": self._diff_section(
                model_a.get("entities", []), model_b.get("entities", []),
                "entity_id"),
            "metrics": self._diff_section(
                model_a.get("metrics", []), model_b.get("metrics", []), "metric_id"),
            "relationships": self._diff_section(
                model_a.get("relationships", []),
                model_b.get("relationships", []), "relationship_key"),
            "paths": self._diff_section(
                model_a.get("paths", []), model_b.get("paths", []), "path_id"),
        }

    @staticmethod
    def _diff_section(
        section_a: list[dict[str, Any]], section_b: list[dict[str, Any]],
        key_field: str,
    ) -> dict[str, Any]:
        a_by_key = {item[key_field]: item for item in section_a}
        b_by_key = {item[key_field]: item for item in section_b}
        return {
            "added": sorted(b_by_key.keys() - a_by_key.keys()),
            "removed": sorted(a_by_key.keys() - b_by_key.keys()),
            "changed": sorted(
                key for key in a_by_key.keys() & b_by_key.keys()
                if _jsonable(a_by_key[key]) != _jsonable(b_by_key[key])),
        }

    def export_model(
        self, model_id: str, version: int, *, export_format: str = "json",
    ) -> dict[str, Any]:
        if export_format not in ("json", "yaml"):
            raise ValueError("导出格式仅支持 json/yaml")
        enriched = self.get_model(model_id, version=version)
        # 顺序稳定（便于版本比较）；不含凭证/连接信息/sample_values
        export_payload = {
            "schema_version": enriched["schema_version"],
            "model_id": enriched["model_id"],
            "source_id": enriched["source_id"],
            "snapshot_id": enriched["snapshot_id"],
            "version": enriched["version"],
            "created_at": enriched["created_at"],
            "created_by": enriched["created_by"],
            "based_on_version": enriched.get("based_on_version"),
            "change_note": enriched.get("change_note"),
            "availability": enriched["availability"],
            "formal_metric_ids": enriched["formal_metric_ids"],
            "dependencies": enriched["dependencies"],
            "topics": enriched["topics"],
            "entities": enriched["entities"],
            "relationships": enriched["relationships"],
            "dimensions": enriched["dimensions"],
            "metrics": enriched["metrics"],
            "paths": enriched["paths"],
        }
        if export_format == "json":
            content = json.dumps(
                export_payload, ensure_ascii=False, indent=2, sort_keys=False)
        else:
            import yaml

            content = yaml.safe_dump(
                export_payload, allow_unicode=True, sort_keys=False, width=100)
        return {
            "format": export_format,
            "model_id": model_id,
            "version": version,
            "content": content,
            "note": (
                "导出为模型语义的表达（发布指针见 availability.is_published_version）；"
                "正式可用集合=formal_metric_ids；历史/失效版本导出以 availability 标记"),
        }


def _jsonable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
