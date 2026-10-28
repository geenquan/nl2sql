"""M4 业务语义层领域契约（semantic-model-v1）。

设计约束（doc/05_M4 + 任务要求）：
- 字段身份＝source+snapshot+schema+table+column 的完整结构化引用；
  组合键/映射保存**有序字段列表**，不用字符串拼接代替结构化身份；
- 不按同名列合并不同表/Schema 的字段；一表一实体候选，不因名称相同自动合并；
- grain 必须同时含业务描述、中心表与粒度键的结构化引用、依据与确认状态；
  主键/真实唯一约束可作结构依据，样本唯一率不能升级为全表唯一事实；
- 指标使用受限聚合（count_rows/count_distinct/sum/avg/min/max）+ 受限过滤
  操作符 + 类型化值；不接受任意 SQL/Python 表达式/eval；结构化定义是事实
  来源，展示表达式只是派生；
- 指标状态：suggested → data_validated（确有数据验证证据）→
  business_confirmed（人工确认口径）→ published（随模型发布）；
  两个维度分别保留证据；本轮无指标对账，不得仅凭结构检查标 data_validated；
- 关系只引用 M3 已验证关系（relationship_key+revision+完整身份+有序映射），
  不存自由文本 JOIN 条件；默认不跨快照借用旧 validated 记录；
- 查询路径方向沿用 M3 source→target；多 validated 边不等于路径可安全聚合
  （扇出/粒度风险须显式评估，必要时阻断指标进入正式集合）；
- 模型版本不可变（修改/确认/回滚产生新版本或原子指针切换，全部留审计）；
- “发布语义模型”仅切换下游使用的模型版本指针，不代表发布数据集或执行 SQL。
"""

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "semantic-model-v1"
MODEL_BUILDER_VERSION = "smodel-build-v1"

METRIC_STATUSES: tuple[str, ...] = (
    "suggested", "data_validated", "business_confirmed", "published",
)
# 允许的聚合函数（受限表达式；任意 SQL/Python 一律拒绝）
METRIC_AGGREGATIONS: tuple[str, ...] = (
    "count_rows", "count_distinct", "sum", "avg", "min", "max",
)
# 允许的过滤操作符（值按字段类型核验，不做字符串拼接进表达式）
METRIC_FILTER_OPERATORS: tuple[str, ...] = (
    "eq", "ne", "in", "not_in", "gt", "ge", "lt", "le", "is_null", "not_null",
)
# 维度允许用途
DIMENSION_USES: tuple[str, ...] = ("filter", "group", "order")
# grain 结构依据
GRAIN_BASES: tuple[str, ...] = ("primary_key", "unique_constraint", "unknown")
# 模型版本状态（版本内容不可变；published 由发布指针表达）
MODEL_VERSION_STATUSES: tuple[str, ...] = ("draft", "published", "retired")
# 路径状态
PATH_STATUSES: tuple[str, ...] = ("available", "needs_review", "blocked")
# 依赖可用性
DEPENDENCY_STATUSES: tuple[str, ...] = ("ok", "needs_revalidation", "snapshot_changed")


@dataclass(frozen=True)
class FieldRef:
    """字段完整身份（结构化；不拼接字符串）。"""

    schema_name: str
    table_name: str
    column_name: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema_name, "table": self.table_name,
                "column": self.column_name}


@dataclass(frozen=True)
class TableRef:
    schema_name: str
    table_name: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema_name, "table": self.table_name}


@dataclass
class Grain:
    """主题/实体粒度：业务描述 + 结构化键引用 + 依据 + 确认状态。"""

    statement: str | None = None  # “一行代表什么”的业务描述（人工确认前为空）
    table: TableRef | None = None
    keys: list[str] = field(default_factory=list)  # 有序粒度键列
    basis: str = "unknown"  # primary_key / unique_constraint / unknown
    confirmed: bool = False  # 业务确认（“每行一条记录”不算确认）
    confirmed_by: str | None = None
    missing: list[str] = field(default_factory=list)  # 无法确定时的缺失项

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "table": self.table.to_dict() if self.table else None,
            "keys": list(self.keys),
            "basis": self.basis,
            "confirmed": self.confirmed,
            "confirmed_by": self.confirmed_by,
            "missing": list(self.missing),
        }


@dataclass
class Attribute:
    attribute_id: str
    field: FieldRef
    business_name: str  # 回退原始名时注明来源
    name_source: str  # m2_business_name / m2_comment / column_name
    data_type: str
    semantic_category: str  # M2 类别；rejected/缺失记 unknown
    m2_review_status: str | None  # M2 审核状态（如实保留）
    sensitive: bool = False  # 最小敏感标签（列名关键词层）
    status: str = "suggested"  # suggested / business_confirmed
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute_id": self.attribute_id,
            "field": self.field.to_dict(),
            "business_name": self.business_name,
            "name_source": self.name_source,
            "data_type": self.data_type,
            "semantic_category": self.semantic_category,
            "m2_review_status": self.m2_review_status,
            "sensitive": self.sensitive,
            "status": self.status,
            "evidence": list(self.evidence),
        }


@dataclass
class Entity:
    entity_id: str  # schema.table（完整身份，不跨 Schema 合并）
    name: str
    aliases: list[str] = field(default_factory=list)
    purpose: str = "unknown"  # M2 表用途（rejected 不作正向，记 unknown）
    purpose_note: str | None = None  # 用途消费说明（如被否决/缺失）
    source: TableRef | None = None
    primary_key: list[str] = field(default_factory=list)  # 结构依据（有序）
    unique_indexes: list[list[str]] = field(default_factory=list)  # 唯一约束（有序组合）
    attributes: list[Attribute] = field(default_factory=list)
    grain: Grain = field(default_factory=Grain)
    status: str = "suggested"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "purpose": self.purpose,
            "purpose_note": self.purpose_note,
            "source": self.source.to_dict() if self.source else None,
            "primary_key": list(self.primary_key),
            "unique_indexes": [list(cols) for cols in self.unique_indexes],
            "attributes": [a.to_dict() for a in self.attributes],
            "grain": self.grain.to_dict(),
            "status": self.status,
        }


@dataclass
class Dimension:
    dimension_id: str
    attribute_id: str
    allowed_uses: list[str] = field(default_factory=list)  # filter/group/order

    def to_dict(self) -> dict[str, Any]:
        return {"dimension_id": self.dimension_id,
                "attribute_id": self.attribute_id,
                "allowed_uses": list(self.allowed_uses)}


@dataclass
class ModelRelationship:
    """对 M3 已验证关系的结构化引用（含修订号；不存自由文本 JOIN）。"""

    relationship_key: str
    revision: int
    source: TableRef
    source_columns: list[str]  # 有序映射
    target: TableRef
    target_columns: list[str]
    cardinality: str
    evidence_scope: dict[str, Any] = field(default_factory=dict)  # M3 验证范围摘要

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_key": self.relationship_key,
            "revision": self.revision,
            "source": {**self.source.to_dict(), "columns": list(self.source_columns)},
            "target": {**self.target.to_dict(), "columns": list(self.target_columns)},
            "cardinality": self.cardinality,
            "evidence_scope": dict(self.evidence_scope),
        }


@dataclass
class MetricFilter:
    field: FieldRef
    operator: str  # METRIC_FILTER_OPERATORS 之一
    value: Any = None  # 类型化值（服务端按字段类型核验）

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field.to_dict(), "operator": self.operator,
                "value": self.value}


@dataclass
class Metric:
    metric_id: str
    name: str
    entity_id: str
    aggregation: str  # METRIC_AGGREGATIONS 之一
    description: str | None = None
    measure_field: FieldRef | None = None  # count_rows 无字段
    distinct_keys: list[FieldRef] = field(default_factory=list)  # 组合去重键（元组语义）
    filters: list[MetricFilter] = field(default_factory=list)
    grain_statement: str | None = None  # 统计 grain（引用主题粒度）
    time_field: FieldRef | None = None
    time_basis: str | None = None  # 时间口径（如 事件发生时间/自然日）
    unit: str | None = None
    status: str = "suggested"
    data_validation: dict[str, Any] | None = None  # 数据验证证据（独立维度）
    business_confirmation: dict[str, Any] | None = None  # 人工确认证据（独立维度）
    blocking: list[str] = field(default_factory=list)  # 待确认项（单位/口径/过滤）
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def display_expression(self) -> str:
        """展示表达式（派生；结构化定义才是事实来源）。"""
        if self.aggregation == "count_rows":
            expr = "COUNT(*)"
        elif self.aggregation == "count_distinct":
            keys = ", ".join(f.column_name for f in self.distinct_keys) or "?"
            expr = f"COUNT(DISTINCT ({keys}))"
        else:
            column = self.measure_field.column_name if self.measure_field else "?"
            expr = f"{self.aggregation.upper()}({column})"
        if self.filters:
            expr += " WHERE " + " AND ".join(
                f"{flt.field.column_name} {flt.operator} ..." for flt in self.filters)
        return expr

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "description": self.description,
            "entity_id": self.entity_id,
            "aggregation": self.aggregation,
            "measure_field": self.measure_field.to_dict() if self.measure_field else None,
            "distinct_keys": [k.to_dict() for k in self.distinct_keys],
            "filters": [f.to_dict() for f in self.filters],
            "grain_statement": self.grain_statement,
            "time_field": self.time_field.to_dict() if self.time_field else None,
            "time_basis": self.time_basis,
            "unit": self.unit,
            "status": self.status,
            "data_validation": self.data_validation,
            "business_confirmation": self.business_confirmation,
            "blocking": list(self.blocking),
            "evidence": list(self.evidence),
            "display_expression": derive_display_expression({
                "aggregation": self.aggregation,
                "measure_field": self.measure_field.to_dict()
                if self.measure_field else None,
                "distinct_keys": [k.to_dict() for k in self.distinct_keys],
                "filters": [f.to_dict() for f in self.filters],
            }),
        }


@dataclass
class PathStep:
    relationship_key: str
    revision: int
    from_table: TableRef
    from_columns: list[str]
    to_table: TableRef
    to_columns: list[str]
    cardinality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_key": self.relationship_key,
            "revision": self.revision,
            "from": {**self.from_table.to_dict(), "columns": list(self.from_columns)},
            "to": {**self.to_table.to_dict(), "columns": list(self.to_columns)},
            "cardinality": self.cardinality,
        }


@dataclass
class QueryPath:
    path_id: str
    topic_id: str
    from_table: TableRef
    to_table: TableRef
    steps: list[PathStep] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    search_complete: bool = True  # 路径搜索是否完整（预算截断则 False）
    budget_truncated: bool = False
    status: str = "needs_review"  # available / needs_review / blocked
    status_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "topic_id": self.topic_id,
            "from": self.from_table.to_dict(),
            "to": self.to_table.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "risks": list(self.risks),
            "search_complete": self.search_complete,
            "budget_truncated": self.budget_truncated,
            "status": self.status,
            "status_reasons": list(self.status_reasons),
        }


@dataclass
class Topic:
    topic_id: str
    name: str
    central_entity_id: str
    description: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    grain: Grain = field(default_factory=Grain)  # 引用中心实体粒度
    dimension_ids: list[str] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)
    status: str = "suggested"

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "name": self.name,
            "description": self.description,
            "central_entity_id": self.central_entity_id,
            "entity_ids": list(self.entity_ids),
            "grain": self.grain.to_dict(),
            "dimension_ids": list(self.dimension_ids),
            "metric_ids": list(self.metric_ids),
            "path_ids": list(self.path_ids),
            "status": self.status,
        }


@dataclass
class ModelDependencies:
    """依赖清单：历史模型内容可重现，不因后来审核静默变化。

    修复轮：除 M3 关系修订外，记录实际采用的 M2 字段语义修订
    （table_id.column_name → revision）与 M2 表用途修订（table_id →
    revision）；可用性检查逐项与当前 effective 修订比对——状态仍为
    validated 不代表旧修订/旧基数仍可继续使用。
    """

    metadata_snapshot_id: str | None = None
    m2_semantics_snapshot_id: str | None = None
    relationship_revisions: dict[str, int] = field(default_factory=dict)
    m2_field_semantics: dict[str, int] = field(default_factory=dict)
    m2_table_purposes: dict[str, int] = field(default_factory=dict)
    rules_version: str = MODEL_BUILDER_VERSION
    input_complete: bool = True
    partial_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata_snapshot_id": self.metadata_snapshot_id,
            "m2_semantics_snapshot_id": self.m2_semantics_snapshot_id,
            "relationship_revisions": dict(self.relationship_revisions),
            "m2_field_semantics": dict(self.m2_field_semantics),
            "m2_table_purposes": dict(self.m2_table_purposes),
            "rules_version": self.rules_version,
            "input_complete": self.input_complete,
            "partial_missing": list(self.partial_missing),
        }


def derive_display_expression(metric: dict[str, Any]) -> str:
    """从当前结构化定义派生展示表达式（修复轮：dict 修订后必须重派生，
    不得沿用构建时保存的旧表达式；结构化定义才是事实来源）。"""
    aggregation = metric.get("aggregation")
    if aggregation == "count_rows":
        expr = "COUNT(*)"
    elif aggregation == "count_distinct":
        keys = ", ".join(
            k.get("column", "?") for k in metric.get("distinct_keys") or [])
        expr = f"COUNT(DISTINCT ({keys or '?'}))"
    else:
        column = (metric.get("measure_field") or {}).get("column", "?")
        expr = f"{str(aggregation).upper()}({column})"
    filters = metric.get("filters") or []
    if filters:
        expr += " WHERE " + " AND ".join(
            f"{flt['field']['column']} {flt['operator']} ..."
            for flt in filters)
    return expr


@dataclass
class SemanticModel:
    """版本化业务语义模型（版本内容不可变；发布用指针表达）。"""

    model_id: str
    source_id: str
    snapshot_id: str
    version: int = 1
    status: str = "draft"  # draft / published / retired
    created_at: str = ""
    created_by: str = "system"
    based_on_version: int | None = None
    change_note: str | None = None
    topics: list[Topic] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    relationships: list[ModelRelationship] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    paths: list[QueryPath] = field(default_factory=list)
    dependencies: ModelDependencies = field(default_factory=ModelDependencies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "model_id": self.model_id,
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "based_on_version": self.based_on_version,
            "change_note": self.change_note,
            "topics": [t.to_dict() for t in self.topics],
            "entities": [e.to_dict() for e in self.entities],
            "relationships": [r.to_dict() for r in self.relationships],
            "dimensions": [d.to_dict() for d in self.dimensions],
            "metrics": [m.to_dict() for m in self.metrics],
            "paths": [p.to_dict() for p in self.paths],
            "dependencies": self.dependencies.to_dict(),
        }
