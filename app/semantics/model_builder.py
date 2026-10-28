"""M4 语义模型构建器（保守规则，确定性，不调用模型）。

规则要点：
- 一表一实体候选（entity_id=schema.table），不因名称/实体词相同自动合并；
  跨 Schema 同名表是不同实体；
- M2 effective 消费：rejected 语义不作为正向业务命名/分类依据（记 unknown
  并保留原审核状态）；缺失语义按缺失处理（unknown）；不按同名列合并字段；
- grain：主键→primary_key 依据；单列唯一索引→unique_constraint 依据；
  否则 unknown + 缺失项。业务描述与确认状态初始为空/False（人工确认）；
- 指标候选全部 suggested：金额/数量类字段凭 M2 类别证据生成 sum/avg 候选，
  事件/明细主题生成 count_rows，主数据生成 count_distinct（组合键保留
  元组语义）；单位/时间口径/状态过滤未确认项进入 blocking；
  本轮不做指标对账，不产生 data_validated；
- 关系只引用同快照 effective、validated、非 stale 的 M3 记录（含修订号）；
- 查询路径复用 M3 find_paths（source→target 方向，不逆向）：桥接/需逆向
  的关联不可达（阻断或待审核）；同对多路径=歧义待审核；基数未知待审核；
  多对多风险待审核；预算截断如实标记（不得得出“无其他路径”结论）；
- 敏感列（列名关键词层）打最小敏感标签且不进入维度候选。
"""

import time as _time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.domain.relationships import now_iso
from app.domain.semantic_model import (
    MODEL_BUILDER_VERSION,
    Attribute,
    Dimension,
    Entity,
    FieldRef,
    Grain,
    Metric,
    ModelDependencies,
    ModelRelationship,
    PathStep,
    QueryPath,
    SemanticModel,
    TableRef,
    Topic,
)
from app.relationships.graph import PathSearchBudget, find_paths
from app.security.masking import is_sensitive_column
from app.semantics.prompt import sanitize_text_for_model

_TOPIC_PURPOSES = ("master_data", "transaction_event", "detail")
_DIMENSION_CATEGORIES = {
    "status": ("filter", "group"),
    "category": ("filter", "group"),
    "name": ("filter", "group"),
    "date": ("filter", "group", "order"),
    "timestamp": ("filter", "group", "order"),
}
_METRIC_FIELD_CATEGORIES = {"amount": ("sum", "avg"), "quantity": ("sum",)}


def _table_ref(table: dict[str, Any]) -> TableRef:
    return TableRef(schema_name=table["schema_name"], table_name=table["table_name"])


class SemanticModelBuilder:
    """将 M1/M2/M3 结果组合成可版本化的业务语义模型（草稿）。"""

    def build(
        self,
        *,
        source_id: str,
        snapshot_id: str,
        model_id: str,
        version: int,
        tables: list[dict[str, Any]],
        semantics: list[dict[str, Any]],
        purposes: dict[str, dict[str, Any]],  # table_id -> effective purpose
        relationships: list[dict[str, Any]],  # M3 effective validated（本快照）
        input_complete: bool = True,
        partial_missing: list[str] | None = None,
        sensitive_keywords: tuple[str, ...] = (),
        max_hops: int = 2,
        max_paths_per_pair: int = 3,
        max_topics: int = 50,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> SemanticModel:
        get_time = clock or _time.monotonic
        semantics_by_key = {
            (s["table_id"], s["column_name"]): s for s in semantics
        }
        validated = [r for r in relationships if r.get("status") == "validated"]

        entities, dimensions = self._build_entities(
            tables, semantics_by_key, purposes, sensitive_keywords)
        model_relationships = self._build_relationships(validated)
        topics, paths = self._build_topics_and_paths(
            tables, entities, validated, purposes,
            max_hops=max_hops, max_paths_per_pair=max_paths_per_pair,
            max_topics=max_topics, get_time=get_time,
            deadline=deadline_monotonic)
        metrics = self._build_metrics(topics, entities)
        self._fill_topic_refs(topics, entities, dimensions, metrics)

        return SemanticModel(
            model_id=model_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            version=version,
            status="draft",
            created_at=now_iso(),
            created_by="system",
            topics=topics,
            entities=entities,
            relationships=model_relationships,
            dimensions=dimensions,
            metrics=metrics,
            paths=paths,
            dependencies=ModelDependencies(
                metadata_snapshot_id=snapshot_id,
                m2_semantics_snapshot_id=snapshot_id,
                relationship_revisions={
                    r["relationship_key"]: r["revision"] for r in validated},
                m2_field_semantics={
                    f"{table_id}.{column}": record["revision"]
                    for (table_id, column), record in semantics_by_key.items()
                    if record.get("revision") is not None},
                m2_table_purposes={
                    table_id: record["revision"]
                    for table_id, record in purposes.items()
                    if record.get("revision") is not None},
                rules_version=MODEL_BUILDER_VERSION,
                input_complete=input_complete,
                partial_missing=list(partial_missing or []),
            ),
        )

    @staticmethod
    def _fill_topic_refs(
        topics: list[Topic], entities: list[Entity],
        dimensions: list[Dimension], metrics: list[Metric],
    ) -> None:
        """主题的指标/维度引用真实存在且归属正确（P2：归属中心实体）。"""
        entity_dimension_ids: dict[str, list[str]] = {}
        for dimension in dimensions:
            entity_id = dimension.attribute_id.rsplit(".", 1)[0]
            entity_dimension_ids.setdefault(entity_id, []).append(
                dimension.dimension_id)
        entity_metric_ids: dict[str, list[str]] = {}
        for metric in metrics:
            entity_metric_ids.setdefault(metric.entity_id, []).append(
                metric.metric_id)
        for topic in topics:
            topic.metric_ids = sorted(
                entity_metric_ids.get(topic.central_entity_id, []))
            topic.dimension_ids = sorted(
                entity_dimension_ids.get(topic.central_entity_id, []))
            # 实体引用剔除不存在的 id（保守：多跳中间实体见路径步骤）
            known = {e.entity_id for e in entities}
            topic.entity_ids = [e for e in topic.entity_ids if e in known]

    # -- 实体与属性 -----------------------------------------------------------

    def _build_entities(
        self,
        tables: list[dict[str, Any]],
        semantics_by_key: dict[tuple[str, str], dict[str, Any]],
        purposes: dict[str, dict[str, Any]],
        sensitive_keywords: tuple[str, ...],
    ) -> tuple[list[Entity], list[Dimension]]:
        entities: list[Entity] = []
        dimensions: list[Dimension] = []
        for table in tables:
            ref = _table_ref(table)
            entity_id = f"{ref.schema_name}.{ref.table_name}"
            # 自由文本净化（修复轮）：表注释进入实体名前净化；发生遮蔽
            # （净化结果 ≠ 原文，说明含敏感内容）则不作业务名——安全回退
            # 原始表名并注明来源（结构性标识不净化）
            raw_comment = (table.get("table_comment") or "").strip()
            sanitized_comment = (
                sanitize_text_for_model(raw_comment, 30) if raw_comment else "")
            if (sanitized_comment and sanitized_comment == raw_comment
                    and 0 < len(raw_comment) <= 30):
                name = raw_comment
            else:
                name = ref.table_name
            # 实体用途消费有效 M2 结果（修复轮 P2）；rejected 不作正向依据
            purpose_record = purposes.get(table["table_id"]) or {}
            if purpose_record.get("review_status") == "rejected":
                purpose = "unknown"
                purpose_note = "M2 表用途已被人工否决，不作正向分类依据"
            else:
                purpose = purpose_record.get("purpose") or "unknown"
                purpose_note = None
            pk = list(table.get("primary_key") or [])
            attributes: list[Attribute] = []
            for column in table.get("columns") or []:
                column_name = column["name"]
                semantics = semantics_by_key.get((table["table_id"], column_name))
                category = "unknown"
                m2_status = None
                business_name = column_name
                name_source_attr = "column_name"
                evidence: list[dict[str, Any]] = []
                if semantics is not None:
                    m2_status = semantics.get("review_status")
                    # rejected 不作为正向命名/分类依据（如实保留审核状态）
                    if m2_status != "rejected":
                        category = semantics.get("category") or "unknown"
                        suggested = (semantics.get("suggested_business_name")
                                     or "").strip()
                        if suggested:
                            business_name = suggested[:60]
                            name_source_attr = "m2_business_name"
                        elif (column.get("comment") or "").strip():
                            sanitized_comment = sanitize_text_for_model(
                                column["comment"].strip(), 60)
                            if sanitized_comment.strip():
                                business_name = sanitized_comment
                                name_source_attr = "m2_comment"
                    evidence.append({
                        "type": "m2_semantics",
                        "ref": f"{semantics.get('revision')}",
                        "detail": f"M2 修订 {semantics.get('revision')}"
                                  f"（{m2_status}，类别 {semantics.get('category')}）",
                    })
                else:
                    evidence.append({
                        "type": "m2_missing",
                        "ref": column_name,
                        "detail": "该字段无 M2 语义结果，按 unknown 处理",
                    })
                sensitive = is_sensitive_column(column_name, sensitive_keywords)
                attribute = Attribute(
                    attribute_id=f"{entity_id}.{column_name}",
                    field=FieldRef(ref.schema_name, ref.table_name, column_name),
                    business_name=business_name,
                    name_source=name_source_attr,
                    data_type=str(column.get("data_type", "")),
                    semantic_category=category,
                    m2_review_status=m2_status,
                    sensitive=sensitive,
                    evidence=evidence,
                )
                attributes.append(attribute)
                uses = _DIMENSION_CATEGORIES.get(category)
                if uses and not sensitive:
                    dimensions.append(Dimension(
                        dimension_id=f"dim.{attribute.attribute_id}",
                        attribute_id=attribute.attribute_id,
                        allowed_uses=list(uses)))
            entities.append(Entity(
                entity_id=entity_id,
                name=name,
                purpose=purpose,
                purpose_note=purpose_note,
                source=ref,
                primary_key=pk,
                unique_indexes=[
                    list(index.get("columns") or [])
                    for index in (table.get("indexes") or [])
                    if index.get("unique")],
                attributes=attributes,
                grain=self._structural_grain(table),
            ))
        return entities, dimensions

    @staticmethod
    def _structural_grain(table: dict[str, Any]) -> Grain:
        ref = _table_ref(table)
        pk = list(table.get("primary_key") or [])
        if pk:
            return Grain(table=ref, keys=pk, basis="primary_key",
                         missing=["业务粒度描述未确认（人工确认前不算已确认粒度）"])
        for index in table.get("indexes") or []:
            columns = list(index.get("columns") or [])
            if index.get("unique") and columns:
                return Grain(table=ref, keys=columns, basis="unique_constraint",
                             missing=["业务粒度描述未确认（人工确认前不算已确认粒度）"])
        return Grain(table=ref, basis="unknown", missing=[
            "无主键或唯一约束，结构粒度键未知（样本唯一率不能作为全表唯一事实）",
            "业务粒度描述未确认",
        ])

    # -- 关系引用 ---------------------------------------------------------------

    @staticmethod
    def _build_relationships(
        validated: list[dict[str, Any]],
    ) -> list[ModelRelationship]:
        out: list[ModelRelationship] = []
        for record in validated:
            evidence = record.get("evidence") or {}
            scope = (record.get("validation") or {}).get("scope") or {}
            out.append(ModelRelationship(
                relationship_key=record["relationship_key"],
                revision=record["revision"],
                source=TableRef(record["source_schema"], record["source_table"]),
                source_columns=list(record["source_columns"]),
                target=TableRef(record["target_schema"], record["target_table"]),
                target_columns=list(record["target_columns"]),
                cardinality=(evidence.get("cardinality_observed", {})
                             .get("cardinality") or record.get("cardinality_hint")
                             or "unknown"),
                evidence_scope={
                    "snapshot_id": scope.get("snapshot_id"),
                    "coverage_basis": scope.get("coverage_basis"),
                    "coverage_ratio": scope.get("coverage_ratio"),
                },
            ))
        return out

    # -- 主题与路径 ---------------------------------------------------------------

    def _build_topics_and_paths(
        self,
        tables: list[dict[str, Any]],
        entities: list[Entity],
        validated: list[dict[str, Any]],
        purposes: dict[str, dict[str, Any]],
        *,
        max_hops: int,
        max_paths_per_pair: int,
        max_topics: int,
        get_time: Callable[[], float],
        deadline: float | None,
    ) -> tuple[list[Topic], list[QueryPath]]:
        by_entity = {e.entity_id: e for e in entities}
        purpose_by_entity = {
            e.entity_id: (purposes.get(table_id, {}) or {}).get("purpose")
            for table_id, e in ((t["table_id"], next(
                entity for entity in entities
                if entity.source and entity.source.schema_name == t["schema_name"]
                and entity.source.table_name == t["table_name"]),) for t in [])
        }
        del purpose_by_entity  # 见下：直接按 table->table_id 关联
        table_key_by_entity = {
            f"{t['schema_name']}.{t['table_name']}": t["table_id"] for t in tables}
        central_entities = [
            e for e in entities
            if (record := purposes.get(
                table_key_by_entity.get(e.entity_id, ""), {}) or {})
            and record.get("review_status") != "rejected"  # 否决不作正向分类
            and record.get("purpose") in _TOPIC_PURPOSES
        ][:max_topics]
        relationship_by_key = {r["relationship_key"]: r for r in validated}
        topics: list[Topic] = []
        paths: list[QueryPath] = []
        truncated = False
        for entity in central_entities:
            central_id = entity.entity_id
            topic_id = f"topic.{central_id}"
            # 1 跳邻居 = 主题包含实体（沿 validated 边双向可见，但路径只沿正向）
            neighbors = set()
            for record in validated:
                if record["source_table"] == entity.source.table_name \
                        and record["source_schema"] == entity.source.schema_name:
                    neighbors.add((record["target_schema"], record["target_table"]))
                if record["target_table"] == entity.source.table_name \
                        and record["target_schema"] == entity.source.schema_name:
                    neighbors.add((record["source_schema"], record["source_table"]))
            included = [central_id] + [
                f"{schema}.{table}" for schema, table in sorted(neighbors)
                if f"{schema}.{table}" in by_entity and f"{schema}.{table}" != central_id]
            # 路径：中心实体 → 模型内其他实体（M3 find_paths，仅 validated
            # 正向边，max_hops 限制；覆盖 2 跳及以后目标以暴露竞争路径歧义）。
            # deadline 为**绝对单调时钟**期限（剩余<=0 时跳过该对并标记截断）。
            topic_paths: list[str] = []
            path_targets = sorted(
                {(ref.schema_name, ref.table_name)
                 for entity_ref in by_entity.values()
                 if (ref := entity_ref.source) is not None
                 and f"{ref.schema_name}.{ref.table_name}" != central_id})
            for schema, table in path_targets:
                target_id = f"{schema}.{table}"
                if target_id == central_id or target_id not in by_entity:
                    continue
                remaining = (deadline - get_time()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    truncated = True
                    continue
                result = find_paths(
                    validated,
                    _table_id_of(tables, entity.source.schema_name,
                                 entity.source.table_name),
                    _table_id_of(tables, schema, table),
                    PathSearchBudget(
                        max_hops=max_hops,
                        max_paths=max_paths_per_pair,
                        deadline=deadline,
                        clock=get_time,
                    ),
                )
                if result.get("budget_truncated") or result.get("stopped_reason"):
                    truncated = truncated or bool(result.get("stopped_reason"))
                for raw_path in result.get("paths", []):
                    hops = raw_path[:-1] if raw_path and isinstance(
                        raw_path[-1], dict) and "path_risk_notes" in raw_path[-1] \
                        else raw_path
                    query_path = self._to_query_path(
                        topic_id, entity, hops, relationship_by_key,
                        ambiguous=result.get("path_count", 0) > 1,
                        budget_truncated=bool(result.get("stopped_reason")))
                    paths.append(query_path)
                    topic_paths.append(query_path.path_id)
            topics.append(Topic(
                topic_id=topic_id,
                name=entity.name,
                description=(
                    "部分邻居路径因构建期限到期未搜索（不排除存在其他路径）"
                    if truncated else None),
                central_entity_id=central_id,
                entity_ids=included,
                grain=entity.grain,
                path_ids=topic_paths,
            ))
        return topics, paths

    def _to_query_path(
        self,
        topic_id: str,
        central: Entity,
        hops: list[dict[str, Any]],
        relationship_by_key: dict[str, dict[str, Any]],
        *,
        ambiguous: bool,
        budget_truncated: bool,
    ) -> QueryPath:
        steps: list[PathStep] = []
        risks: list[dict[str, Any]] = []
        reasons: list[str] = []
        for hop in hops:
            record = relationship_by_key.get(hop["relationship_key"])
            if record is None:
                reasons.append("路径引用的关系未在模型依赖中（内部不一致）")
                continue
            steps.append(PathStep(
                relationship_key=hop["relationship_key"],
                revision=record["revision"],
                from_table=TableRef(record["source_schema"], record["source_table"]),
                from_columns=list(record["source_columns"]),
                to_table=TableRef(record["target_schema"], record["target_table"]),
                to_columns=list(record["target_columns"]),
                cardinality=hop.get("cardinality") or "unknown",
            ))
            cardinality = hop.get("cardinality")
            # 按路径方向识别扇出风险（修复轮）：路径沿 source→target 行进，
            # one_to_many（源唯一、目标非唯一）与 many_to_many（两端非唯一）
            # 都会造成行 fan-out/重复计数 → 阻断；many_to_one/one_to_one 安全。
            if cardinality == "many_to_many":
                risks.append({"type": "many_to_many",
                              "note": "多对多连接：聚合需显式去重/条件方案"})
                reasons.append("多对多步骤：扇出/重复计数风险，需明确聚合方案")
            elif cardinality == "one_to_many":
                risks.append({"type": "fanout_one_to_many",
                              "note": "一对多方向连接：目标端非唯一，行数膨胀"})
                reasons.append("一对多方向步骤：扇出/重复计数风险，需明确聚合方案")
            elif cardinality in ("unknown", None):
                reasons.append("步骤基数未知（口径不足）")
            if len(record["source_columns"]) > 1:
                risks.append({"type": "composite_join",
                              "note": "组合连接条件（多列映射），使用时须完整携带"})
        if ambiguous:
            reasons.append("同对存在多条路径：业务歧义待人工选择（不默认取最高分）")
        if budget_truncated:
            reasons.append("路径搜索受预算截断：不排除存在其他路径")
        status = "available" if not reasons else (
            "blocked" if any(r["type"] in ("many_to_many", "fanout_one_to_many")
                             for r in risks)
            else "needs_review")
        return QueryPath(
            path_id=f"path.{uuid4().hex[:12]}",
            topic_id=topic_id,
            from_table=central.source or TableRef("", ""),
            to_table=steps[-1].to_table if steps else central.source or TableRef("", ""),
            steps=steps,
            risks=risks,
            search_complete=not budget_truncated,
            budget_truncated=budget_truncated,
            status=status,
            status_reasons=reasons,
        )

    # -- 候选指标 ---------------------------------------------------------------

    def _build_metrics(
        self, topics: list[Topic], entities: list[Entity]
    ) -> list[Metric]:
        entity_by_id = {e.entity_id: e for e in entities}
        metrics: list[Metric] = []
        for topic in topics:
            central = entity_by_id.get(topic.central_entity_id)
            if central is None:
                continue
            # 1) count_rows：主题中心表行数候选（口径仍待确认）
            metrics.append(Metric(
                metric_id=f"metric.{topic.topic_id}.count_rows",
                name=f"{topic.name}记录数",
                description=f"{topic.name}的行数（粒度：{topic.grain.statement or '待确认'}）",
                entity_id=central.entity_id,
                aggregation="count_rows",
                grain_statement=topic.grain.statement,
                blocking=["时间口径未确认", "过滤口径未确认"],
                evidence=[{"type": "grain_basis", "ref": topic.grain.basis,
                           "detail": f"结构依据={topic.grain.basis}"}],
            ))
            # 2) 主数据主标识 count_distinct（组合键元组语义）
            if central.primary_key:
                metrics.append(Metric(
                    metric_id=f"metric.{topic.topic_id}.count_distinct",
                    name=f"{topic.name}去重数量",
                    entity_id=central.entity_id,
                    aggregation="count_distinct",
                    distinct_keys=[
                        FieldRef(central.source.schema_name, central.source.table_name, c)
                        for c in central.primary_key],
                    grain_statement=topic.grain.statement,
                    blocking=["去重业务口径未确认"],
                    evidence=[{"type": "primary_key", "ref": ",".join(central.primary_key),
                               "detail": "去重键=主键（结构依据）"}],
                ))
            # 3) 金额/数量属性 sum/avg 候选（凭 M2 类别证据；数字类型不自动成指标）
            for attribute in central.attributes:
                aggregations = _METRIC_FIELD_CATEGORIES.get(
                    attribute.semantic_category, ())
                if not aggregations or attribute.m2_review_status == "rejected":
                    continue
                for aggregation in aggregations:
                    metrics.append(Metric(
                        metric_id=f"metric.{attribute.attribute_id}.{aggregation}",
                        name=f"{attribute.business_name}{aggregation.upper()}",
                        entity_id=central.entity_id,
                        aggregation=aggregation,
                        measure_field=attribute.field,
                        grain_statement=topic.grain.statement,
                        blocking=[
                            "单位未确认" if attribute.semantic_category == "amount"
                            else "计量口径未确认",
                            "时间口径未确认",
                            "状态过滤未确认",
                        ],
                        evidence=[{
                            "type": "m2_semantics",
                            "ref": attribute.attribute_id,
                            "detail": f"M2 类别={attribute.semantic_category}"
                                      f"（审核状态 {attribute.m2_review_status}）",
                        }],
                    ))
        return metrics


def _table_id_of(tables: list[dict[str, Any]], schema: str, table_name: str) -> str:
    for table in tables:
        if table["schema_name"] == schema and table["table_name"] == table_name:
            return table["table_id"]
    return ""
