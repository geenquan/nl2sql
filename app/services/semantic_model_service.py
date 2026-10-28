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
    derive_display_expression,
)
from app.semantics.model_builder import SemanticModelBuilder
from app.semantics.prompt import sanitize_text_for_model


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
        # 共用指标定义校验（修复轮：所有入口一致；构建产物不合规不保存）
        entities_by_id = {e["entity_id"]: e for e in payload.get("entities", [])}
        invalid: list[str] = []
        for metric in payload.get("metrics", []):
            if self._validate_metric_definition(metric, entities_by_id):
                invalid.append(metric["metric_id"])
        if invalid:
            payload["metrics"] = [
                m for m in payload.get("metrics", [])
                if m["metric_id"] not in invalid]
            for topic in payload.get("topics", []):
                topic["metric_ids"] = [
                    mid for mid in topic.get("metric_ids", [])
                    if mid not in invalid]
            payload.setdefault("dependencies", {})["partial_missing"] = list(
                payload["dependencies"].get("partial_missing") or []
            ) + [f"构建时剔除不合规候选指标：{','.join(invalid[:5])}"]
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

    def _dependency_status(
        self, model: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """依赖判定（修复轮）：逐项比对**采用的修订**与当前 effective 结果。

        - M3 关系：当前快照内 effective 缺失 / 修订号不一致 / 状态非
          validated → needs_revalidation（状态仍 validated 不代表旧修订/
          旧基数仍可使用）；
        - M2 字段语义/表用途：当前 effective 修订号与采用值不一致（含被
          否决、撤销）→ needs_revalidation；
        - 目录快照更替 → snapshot_changed；
        - 依赖信息不完整的旧模型（缺 M2 依赖记录）→ needs_revalidation
          （不能无条件视为已验证）。
        """
        deps = model.get("dependencies") or {}
        details: dict[str, Any] = {"relationships": {}, "m2": []}
        relation_revisions = deps.get("relationship_revisions") or {}
        for key, adopted_revision in relation_revisions.items():
            effective = self._repo.get_effective_relationship(
                key, snapshot_id=model.get("snapshot_id"))
            if (effective is None
                    or effective.get("revision") != adopted_revision
                    or effective.get("status") != "validated"):
                details["relationships"][key] = {
                    "adopted_revision": adopted_revision,
                    "current_revision": effective.get("revision")
                    if effective else None,
                    "current_status": effective.get("status")
                    if effective else "missing",
                }
        field_deps = deps.get("m2_field_semantics") or {}
        if relation_revisions and not field_deps and not deps.get(
                "m2_table_purposes"):
            details["m2"].append(
                "缺少完整 M2 依赖信息（历史模型）：保守降级，重建后恢复")
        else:
            for ref, adopted_revision in field_deps.items():
                table_id, _, column = ref.partition(".")
                history = self._repo.get_field_semantics_history(
                    table_id, column)
                effective_revision = history[0].get("revision") if history \
                    else None
                if effective_revision != adopted_revision:
                    details["m2"].append(
                        f"字段语义修订变化：{ref} 采用 {adopted_revision}"
                        f" 当前 {effective_revision}")
            for table_id, adopted_revision in (
                    deps.get("m2_table_purposes") or {}).items():
                purpose = self._repo.get_table_purpose(
                    table_id, effective=True)
                effective_revision = purpose.get("revision") if purpose \
                    else None
                if effective_revision != adopted_revision:
                    details["m2"].append(
                        f"表用途修订变化：{table_id} 采用 {adopted_revision}"
                        f" 当前 {effective_revision}")
        published_snapshot = self._repo.get_published_snapshot(
            model["source_id"])
        snapshot_ok = bool(
            published_snapshot
            and published_snapshot["snapshot_id"] == model.get("snapshot_id"))
        if not snapshot_ok:
            status = "snapshot_changed"
        elif details["relationships"] or details["m2"]:
            status = "needs_revalidation"
        else:
            status = "ok"
        return status, {"snapshot_current": snapshot_ok, "details": details}

    def _enrich(self, model: dict[str, Any]) -> dict[str, Any]:
        """附加可用性（修复轮：usable_as_current=发布指针命中+依赖 ok；
        formal_metric_ids 仅在当前可用时返回，已确认草稿另列
        confirmed_metric_ids；不修改已存版本内容）。"""
        dependency_status, dependency_info = self._dependency_status(model)
        pointer = self._repo.get_published(model["source_id"])
        is_published = bool(pointer and pointer["model_id"] == model["model_id"]
                            and pointer["version"] == model["version"])
        usable_as_current = is_published and dependency_status == "ok"
        confirmed = [
            m["metric_id"] for m in model.get("metrics", [])
            if m.get("status") in ("business_confirmed", "published")]
        enriched = self._sanitize_model_text(dict(model))
        enriched["availability"] = {
            "dependency_status": dependency_status,
            "dependency_info": dependency_info,
            "is_published_version": is_published,
            "usable_as_current": usable_as_current,
            "note": (
                "可用=发布指针命中且依赖（M2/M3 采用修订 vs 当前 effective）"
                "一致；基于本地元数据，未扫描的源库实际变化无法检测"
                if usable_as_current else
                "当前不可用作正式模型：" + (
                    "未发布（此版本不是发布指针所指）" if not is_published
                    else f"依赖降级（{dependency_status}）")
                + "；已确认草稿指标见 confirmed_metric_ids"),
        }
        enriched["confirmed_metric_ids"] = confirmed
        # 正式集合：仅当前可用（已发布且依赖有效）时返回；否则为空
        enriched["formal_metric_ids"] = confirmed if usable_as_current else []
        return enriched

    def _sanitize_model_text(self, model: dict[str, Any]) -> dict[str, Any]:
        """读取/导出边界的自由文本净化（修复轮）：历史版本不改写存储行，
        但 API/导出不得继续原样暴露敏感文本；结构性标识（Schema/表/列/
        关系键）不净化。净化后业务名不可用时回退原始字段名并注明。"""
        sanitize = sanitize_text_for_model
        for entity in model.get("entities", []):
            entity["name"] = sanitize(entity.get("name") or "", 60) \
                or entity.get("source", {}).get("table", entity["name"])
            if entity.get("purpose_note"):
                entity["purpose_note"] = sanitize(entity["purpose_note"], 120)
            for attribute in entity.get("attributes", []):
                if attribute.get("name_source") != "column_name":
                    cleaned = sanitize(attribute.get("business_name") or "", 60)
                    if not cleaned.strip():
                        attribute["business_name"] = attribute["field"]["column"]
                        attribute["name_source"] = "column_name"
                    else:
                        attribute["business_name"] = cleaned
                for evidence in attribute.get("evidence", []):
                    if isinstance(evidence.get("detail"), str):
                        evidence["detail"] = sanitize(evidence["detail"], 160)
        for metric in model.get("metrics", []):
            metric["display_expression"] = derive_display_expression(metric)
            if metric.get("description"):
                metric["description"] = sanitize(metric["description"], 160)
        return model

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
        entities_by_id = {e["entity_id"]: e for e in model.get("entities", [])}

        grain_changed_topics: set[str] = set()

        for topic_id, topic_change in (changes.get("topic_grains") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            statement = (topic_change.get("statement") or "").strip()
            if not statement:
                raise ValueError(f"grain 业务描述不能为空：{topic_id}")
            keys = topic_change.get("keys") or topic["grain"].get("keys") or []
            central = entities_by_id.get(topic["central_entity_id"])
            if central is None or not keys:
                raise ValueError(f"主题缺少结构粒度键：{topic_id}")
            entity_columns = [
                a["field"]["column"] for a in central.get("attributes", [])]
            unknown_keys = [k for k in keys if k not in entity_columns]
            if unknown_keys:
                raise ValueError(
                    f"粒度键不存在于中心表：{','.join(unknown_keys)}")
            if len(set(keys)) != len(keys):
                raise ValueError(f"粒度键存在重复列：{topic_id}")
            # 依据重核（修复轮）：键集必须与主键/唯一索引**精确匹配**才可确认；
            # 普通非唯一列不得经人工 confirm 伪装成已证实行粒度键——保留为
            # 待验证草稿（confirmed=False + missing 说明）。
            basis, _missing = self._grain_structural_basis(central, keys)
            wants_confirm = bool(topic_change.get("confirm"))
            if wants_confirm and basis == "unknown":
                topic["grain"].update({
                    "statement": statement, "keys": list(keys),
                    "basis": "unknown",
                    "confirmed": False,  # 无结构证明：保留待验证草稿
                    "confirmed_by": None,
                    "missing": [
                        ("人工声明的粒度键无结构证明（非主键/唯一约束）："
                         "保留为待验证草稿，不作已证实行粒度"),
                        "需补充唯一性证据或改用结构键后重新确认",
                    ],
                })
                grain_changed_topics.add(topic_id)
                continue
            if not wants_confirm:
                raise ValueError(
                    f"grain 修订必须显式 confirm=true：{topic_id}"
                    "（无结构证明的键会被保留为待验证草稿）")
            old_keys = list(topic["grain"].get("keys") or [])
            old_basis = topic["grain"].get("basis")
            topic["grain"].update({
                "statement": statement, "keys": list(keys),
                "basis": basis,
                "confirmed": True, "confirmed_by": reviewer,
                "missing": [],
            })
            # 主题与中心实体 grain 保持一致（修复轮：同一事实两处引用）
            central["grain"].update(topic["grain"])
            if old_keys != list(keys) or old_basis != basis \
                    or not (topic["grain"].get("confirmed") or False) \
                    and False:
                grain_changed_topics.add(topic_id)
            if old_keys != list(keys) or old_basis != basis:
                grain_changed_topics.add(topic_id)

        for topic_id, name in (changes.get("topic_names") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            if not (name or "").strip():
                raise ValueError(f"主题名称不能为空：{topic_id}")
            topic["name"] = sanitize_text_for_model(name.strip(), 60)

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
                metric, metric_change, fields_by_ref, entities_by_id, reviewer)

        for topic_id, path_id in (changes.get("selected_paths") or {}).items():
            topic = topics.get(topic_id)
            if topic is None:
                raise ValueError(f"未知主题：{topic_id}")
            path = paths.get(path_id)
            if path is None or path.get("topic_id") != topic_id:
                raise ValueError(f"路径不属于该主题：{path_id}")
            if path.get("status") == "blocked":
                raise ValueError(f"阻断路径不可选为正式路径：{path_id}")
            # 人工消歧（修复轮）：只解除“多条路径”歧义；其他风险
            # （基数未知/扇出/预算截断/组合条件）不被解除，状态按剩余
            # 风险重新判定——带未决风险的路径保持 needs_review/blocked。
            remaining_reasons = [
                r for r in path.get("status_reasons", []) if "多条路径" not in r]
            has_fanout = any(
                r.get("type") in ("many_to_many", "fanout_one_to_many")
                for r in path.get("risks", []))
            if has_fanout:
                path["status"] = "blocked"
            elif remaining_reasons:
                path["status"] = "needs_review"
            else:
                path["status"] = "available"
            path["status_reasons"] = remaining_reasons
            path["manually_selected"] = True
            for other in model.get("paths", []):
                if (other["path_id"] != path_id
                        and other["topic_id"] == topic_id
                        and other["to"]["table"] == path["to"]["table"]
                        and other["to"]["schema"] == path["to"]["schema"]):
                    if other.get("status") != "blocked":
                        other["status"] = "needs_review"
                    other.setdefault("status_reasons", []).append(
                        "人工已选择其他路径（本路径未被选用）")

        # 指标统计粒度始终解析自主题 grain（修复轮：可完整解析）；
        # grain 键/依据实质变化时受影响指标重新确认（P1-3 联动）
        for metric in model.get("metrics", []):
            topic = next(
                (t for t in model.get("topics", [])
                 if t["central_entity_id"] == metric.get("entity_id")), None)
            if topic is None:
                continue
            if topic["topic_id"] in grain_changed_topics:
                self._invalidate_metric_confirmation(
                    metric, "主题 grain 变化：统计口径需重新确认")
            metric["grain_statement"] = (
                topic["grain"].get("statement")
                or metric.get("grain_statement"))

    @staticmethod
    def _grain_structural_basis(
        entity: dict[str, Any], keys: list[str]
    ) -> tuple[str, list[str]]:
        """重核 grain 结构依据：与主键/唯一索引**精确匹配**（有序组合比较）。"""
        if list(entity.get("primary_key") or []) == list(keys):
            return "primary_key", []
        for columns in entity.get("unique_indexes") or []:
            if list(columns) == list(keys):
                return "unique_constraint", []
        return "unknown", ["粒度键无结构证明"]

    @staticmethod
    def _invalidate_metric_confirmation(
        metric: dict[str, Any], reason: str
    ) -> None:
        """口径实质变化：撤销业务确认与不再适用的数据验证证据（修复轮）。"""
        if metric.get("status") in ("business_confirmed", "published"):
            metric["status"] = "suggested"
            metric["business_confirmation"] = None
            metric["data_validation"] = None
            metric.setdefault("blocking", []).append(reason)
            metric["blocking"] = list(dict.fromkeys(metric["blocking"]))

    # 口径字段（修复轮定义）：实质变化必须撤销确认重新审核；
    # 非口径字段（名称/描述/备注）可保留确认。
    _METRIC_CALIBER_FIELDS = (
        "aggregation", "measure_field", "distinct_keys", "filters",
        "time_field", "time_basis", "unit",
    )

    def _apply_metric_change(
        self, metric: dict[str, Any], change: dict[str, Any],
        fields_by_ref: dict[tuple[str, str, str], dict[str, Any]],
        entities_by_id: dict[str, dict[str, Any]],
        reviewer: str,
    ) -> None:
        action = change.get("action")
        if action not in ("confirm", "reject", "update"):
            raise ValueError(f"未知指标动作：{action}")
        # 非口径字段（先应用，不影响确认）
        if change.get("name") is not None:
            metric["name"] = sanitize_text_for_model(str(change["name"]), 60)
        if change.get("description") is not None:
            metric["description"] = sanitize_text_for_model(
                str(change["description"]), 160)

        caliber_changed = False
        if change.get("aggregation") is not None \
                and change["aggregation"] != metric["aggregation"]:
            if change["aggregation"] not in METRIC_AGGREGATIONS:
                raise ValueError(f"非法聚合函数：{change['aggregation']}")
            metric["aggregation"] = change["aggregation"]
            caliber_changed = True
        if change.get("measure_field") is not None:
            ref = change["measure_field"]
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            if key not in fields_by_ref:
                raise ValueError(f"measure_field 引用不存在的字段：{ref}")
            if (metric.get("measure_field") or {}).get("column") != ref.get("column") \
                    or (metric.get("measure_field") or {}).get("table") != ref.get("table"):
                caliber_changed = True
            metric["measure_field"] = ref
        if change.get("unit") is not None and change["unit"] != metric.get("unit"):
            metric["unit"] = str(change["unit"])
            caliber_changed = True
        if change.get("time_basis") is not None \
                and change["time_basis"] != metric.get("time_basis"):
            metric["time_basis"] = str(change["time_basis"])
            caliber_changed = True
        if change.get("time_field") is not None:
            ref = change["time_field"]
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            if key not in fields_by_ref:
                raise ValueError(f"time_field 引用不存在的字段：{ref}")
            attribute = fields_by_ref[key]
            if not self._is_temporal_field(attribute):
                raise ValueError(
                    f"time_field 必须是日期/时间类型字段："
                    f"{ref.get('column')}（{attribute.get('data_type')}）")
            if metric.get("time_field") != ref:
                caliber_changed = True
            metric["time_field"] = ref
        if change.get("filters") is not None:
            new_filters = self._validated_filters(
                change["filters"], fields_by_ref, metric.get("entity_id"),
                entities_by_id)
            if new_filters != metric.get("filters"):
                caliber_changed = True
            metric["filters"] = new_filters

        # 结构化定义校验（修复轮：修订后立即校验，缺字段定义不保存）
        errors = self._validate_metric_definition(metric, entities_by_id)
        if errors:
            raise ValueError(
                f"指标定义不完整（{metric['metric_id']}）："
                + "；".join(errors))

        # 口径实质变化：撤销确认与不再适用的数据验证（修复轮 P1-3）
        if caliber_changed and action != "confirm":
            self._invalidate_metric_confirmation(
                metric, "指标口径已修改：需重新确认（旧确认已失效）")
        elif caliber_changed and action == "confirm":
            # 同请求 confirm：先撤销旧确认，完整校验新定义后记录新确认
            self._invalidate_metric_confirmation(
                metric, "指标口径已修改：按新定义重新确认")

        # 重派生展示表达式（修复轮：从当前结构化定义派生）
        metric["display_expression"] = derive_display_expression(metric)

        if action == "reject":
            metric["status"] = "suggested"
            metric["business_confirmation"] = None
            metric.setdefault("blocking", []).append("人工否决（不进入正式集合）")
            metric["blocking"] = list(dict.fromkeys(metric["blocking"]))
            return
        if action == "confirm":
            errors = self._validate_metric_definition(metric, entities_by_id)
            if errors:
                raise ValueError(
                    f"确认前定义校验失败（{metric['metric_id']}）："
                    + "；".join(errors))
            if metric["aggregation"] in ("sum", "avg") and not metric.get("unit"):
                raise ValueError(f"确认 {metric['metric_id']} 前必须明确单位")
            if not metric.get("time_basis"):
                raise ValueError(f"确认 {metric['metric_id']} 前必须明确时间口径")
            if not change.get("skip_status_filter") and any(
                    "状态过滤" in b for b in metric.get("blocking", [])):
                raise ValueError(
                    f"确认 {metric['metric_id']} 前必须处理状态过滤口径"
                    "（提供 filters 或显式 skip_status_filter=true）")
            # confirm 只解除有明确依据的待确认项（口径类）；未知/安全阻断保留
            resolved = ("时间口径", "单位", "计量口径", "状态过滤", "过滤口径")
            metric["blocking"] = [
                b for b in metric.get("blocking", [])
                if not any(tag in b for tag in resolved)
                or not (
                    ("时间口径" in b and metric.get("time_basis"))
                    or ("单位" in b and metric.get("unit"))
                    or ("计量口径" in b and metric.get("unit"))
                    or ("状态过滤" in b and (metric.get("filters")
                                             or change.get("skip_status_filter")))
                    or ("过滤口径" in b and metric.get("filters")))]
            metric["status"] = "business_confirmed"
            metric["business_confirmation"] = {
                "confirmed_by": reviewer, "confirmed_at": now_iso(),
                "note": change.get("note"),
            }

    @staticmethod
    def _is_temporal_field(attribute: dict[str, Any]) -> bool:
        data_type = (attribute.get("data_type") or "").lower()
        return any(t in data_type for t in ("date", "datetime", "timestamp", "time"))

    def _validate_metric_definition(
        self, metric: dict[str, Any],
        entities_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        """共用结构化指标定义校验（修复轮 P1-4：build/revise/publish 共用）。

        - 各聚合所需的字段与类型；
        - 引用字段属于指标自身实体（首版不支持跨实体，直接拒绝不隐式补
          JOIN——模型中存在不等于该指标可引用）；
        - 不因字段存在就放行：类型与聚合语义必须匹配。
        """
        errors: list[str] = []
        aggregation = metric.get("aggregation")
        entity = entities_by_id.get(metric.get("entity_id") or "")
        if entity is None:
            return [f"指标实体不存在：{metric.get('entity_id')}"]
        own_fields: dict[tuple[str, str, str], dict[str, Any]] = {
            (a["field"]["schema"], a["field"]["table"], a["field"]["column"]): a
            for a in entity.get("attributes", [])}

        def _field_type(attribute: dict[str, Any]) -> str:
            return (attribute.get("data_type") or "").lower()

        def _is_numeric(attribute: dict[str, Any]) -> bool:
            return any(t in _field_type(attribute) for t in
                       ("int", "decimal", "float", "double", "numeric"))

        measure = metric.get("measure_field")
        if aggregation == "count_rows":
            if measure is not None:
                errors.append("count_rows 不使用度量字段")
            if metric.get("distinct_keys"):
                errors.append("count_rows 不使用去重键（应使用 count_distinct）")
        elif aggregation == "count_distinct":
            if measure is not None:
                errors.append("count_distinct 使用 distinct_keys 而非度量字段")
            keys = metric.get("distinct_keys") or []
            if not keys:
                errors.append("count_distinct 缺少去重键")
            columns = [k.get("column") for k in keys]
            if len(set(columns)) != len(columns):
                errors.append("count_distinct 去重键存在重复列（组合键为有序元组语义）")
            for key in keys:
                ref = (key.get("schema"), key.get("table"), key.get("column"))
                if ref not in own_fields:
                    errors.append(
                        f"去重键不属于指标自身实体：{key.get('column')}"
                        "（首版不支持跨实体引用，不隐式补 JOIN）")
        elif aggregation in ("sum", "avg"):
            if measure is None:
                errors.append(f"{aggregation} 缺少数值型度量字段")
            else:
                ref = (measure.get("schema"), measure.get("table"),
                       measure.get("column"))
                attribute = own_fields.get(ref)
                if attribute is None:
                    errors.append(
                        f"度量字段不属于指标自身实体：{measure.get('column')}"
                        "（首版不支持跨实体引用）")
                elif not _is_numeric(attribute):
                    errors.append(
                        f"{aggregation} 度量字段必须是数值型："
                        f"{measure.get('column')}（{_field_type(attribute)}）")
        elif aggregation in ("min", "max"):
            if measure is None:
                errors.append(f"{aggregation} 缺少度量字段")
            else:
                ref = (measure.get("schema"), measure.get("table"),
                       measure.get("column"))
                attribute = own_fields.get(ref)
                if attribute is None:
                    errors.append(
                        f"度量字段不属于指标自身实体：{measure.get('column')}")
                elif not (_is_numeric(attribute)
                          or self._is_temporal_field(attribute)
                          or any(t in _field_type(attribute)
                                 for t in ("char", "text"))):
                    errors.append(
                        f"{aggregation} 不支持该字段类型：{_field_type(attribute)}")
        else:
            errors.append(f"非法聚合函数：{aggregation}")

        time_field = metric.get("time_field")
        if time_field is not None:
            ref = (time_field.get("schema"), time_field.get("table"),
                   time_field.get("column"))
            attribute = own_fields.get(ref)
            if attribute is None:
                errors.append(
                    f"time_field 不属于指标自身实体：{time_field.get('column')}"
                    "（首版不支持跨实体引用）")
            elif not self._is_temporal_field(attribute):
                errors.append(
                    f"time_field 必须是日期/时间类型：{time_field.get('column')}"
                    f"（{_field_type(attribute)}）")
        for flt in metric.get("filters") or []:
            ref = flt.get("field") or {}
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            if key not in own_fields:
                errors.append(
                    f"过滤字段不属于指标自身实体：{ref.get('column')}"
                    "（首版不支持跨实体过滤，不隐式补 JOIN）")
        return errors

    @staticmethod
    def _validated_filters(
        raw_filters: list[dict[str, Any]],
        fields_by_ref: dict[tuple[str, str, str], dict[str, Any]],
        entity_id: str | None = None,
        entities_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """过滤校验（修复轮：字段归属指标自身实体 + 值类型严格相容，
        不把任意对象 str() 后当合法值）。"""
        validated: list[dict[str, Any]] = []
        own_fields: set[tuple[str, str, str]] | None = None
        if entity_id and entities_by_id:
            entity = entities_by_id.get(entity_id) or {}
            own_fields = {
                (a["field"]["schema"], a["field"]["table"], a["field"]["column"])
                for a in entity.get("attributes", [])}
        for raw in raw_filters:
            ref = raw.get("field") or {}
            key = (ref.get("schema"), ref.get("table"), ref.get("column"))
            attribute = fields_by_ref.get(key)
            if attribute is None:
                raise ValueError(f"过滤条件引用不存在的字段：{ref}")
            if own_fields is not None and key not in own_fields:
                raise ValueError(
                    f"过滤字段不属于指标自身实体：{ref.get('column')}"
                    "（首版不支持跨实体过滤）")
            operator = raw.get("operator")
            if operator not in METRIC_FILTER_OPERATORS:
                raise ValueError(f"非法过滤操作符：{operator}")
            value = raw.get("value")
            if operator in ("is_null", "not_null"):
                value = None
            elif operator in ("in", "not_in"):
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{operator} 过滤需要非空列表值")
                if not all(isinstance(v, (int, float, str, bool))
                           for v in value):
                    raise ValueError(
                        f"{operator} 过滤值只接受数字/字符串/布尔（不接受任意对象）")
            else:
                if value is not None and not isinstance(value, (int, float, str, bool)):
                    raise ValueError(
                        "过滤值只接受数字/字符串/布尔/空值（不接受任意对象）")
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
        """发布前检查（修复轮：不依赖发布状态——validate/_enrich/publish
        不互相递归；正式指标重新做结构化定义校验与路径影响判定）。"""
        issues: list[str] = []
        checks: dict[str, Any] = {}
        dependency_status, dependency_info = self._dependency_status(model)
        checks["dependency_status"] = dependency_status
        if dependency_status != "ok":
            issues.append(
                f"依赖不可用：{dependency_status}"
                "（需重验关系/语义或基于当前发布快照重建）")
        checks["input_complete"] = (model.get("dependencies") or {}).get(
            "input_complete", True)
        if not checks["input_complete"]:
            issues.append("输入为部分快照（未获显式许可不得发布）")

        entities_by_id = {e["entity_id"]: e for e in model.get("entities", [])}
        # grain 重核（修复轮：不能只信 confirmed 布尔值）
        unconfirmed_grains = []
        for topic in model.get("topics", []):
            grain = topic.get("grain") or {}
            if not grain.get("confirmed"):
                unconfirmed_grains.append(topic["topic_id"])
                continue
            central = entities_by_id.get(topic.get("central_entity_id")) or {}
            basis, _ = self._grain_structural_basis(
                central, list(grain.get("keys") or []))
            if basis == "unknown":
                issues.append(
                    f"已确认 grain 无结构依据复核通过：{topic['topic_id']}"
                    "（键集与主键/唯一约束不匹配）")
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
            # 发布前重新做结构化定义校验（修复轮：不只查 status/blocking）
            errors = self._validate_metric_definition(metric, entities_by_id)
            if errors:
                issues.append(
                    f"正式指标定义校验失败：{metric['metric_id']}"
                    f"（{';'.join(errors[:3])}）")
            if not metric.get("grain_statement"):
                issues.append(
                    f"正式指标缺少可解析的统计粒度：{metric['metric_id']}")
        checks["suggested_metric_count"] = sum(
            1 for m in model.get("metrics", []) if m.get("status") == "suggested")

        # 路径影响判定（修复轮：结构化引用替代前缀硬编码）
        checks["path_issues"] = [
            p["path_id"] for p in model.get("paths", [])
            if p.get("status") != "available"]
        entity_id_by_table: dict[tuple[str, str], str] = {
            (e.get("source", {}).get("schema"),
             e.get("source", {}).get("table")): e["entity_id"]
            for e in model.get("entities", []) if e.get("source")}
        for path in model.get("paths", []):
            if path.get("status") != "blocked":
                continue
            affected_entity = entity_id_by_table.get(
                (path.get("to", {}).get("schema"),
                 path.get("to", {}).get("table")))
            if affected_entity and any(
                    m.get("entity_id") == affected_entity
                    and m.get("status") in ("business_confirmed", "published")
                    for m in model.get("metrics", [])):
                issues.append(
                    f"阻断路径影响正式指标（重复计数风险未解决）："
                    f"{path['path_id']} → {affected_entity}")
        return {"valid": not issues, "issues": issues, "checks": checks,
                "dependency_info": dependency_info}

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
        enriched = self.get_model(model_id, version=version)  # 已净化（读取边界）
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
