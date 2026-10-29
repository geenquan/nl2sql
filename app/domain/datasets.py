"""M5 数据集领域契约（dataset-v1）。

边界与原则：
- 数据集定义由**结构化引用**构成（M4 实体/属性/指标/关系键+修订+方向），
  不接受客户端 SQL 片段；SQL 只由已验证定义经确定性编译产生；
- 版本不可变：修改字段/过滤/grain/指标/路径产生新版本；旧 SQL 与质量
  报告不为新定义背书；历史可查看不自动成为当前可发布版本；
- 正式构建/校验/发布必须从仓储重载 M4 模型并校验（当前发布版本、
  usable_as_current、formal_metric_ids、同源同快照、关系为该快照有效
  validated 修订），不信任客户端提交的 status/JSON；
- 一跳明细预聚合是**单独的、有明确粒度转换的计划**（明细按完整关联键
  分组→各组独立聚合→按关联键至多一行→再连中心表保持 grain），不把
  M4 被阻断的一对多路径改判 available，不冒充原 M4 指标定义未变；
- 首版拒绝：未验证关系、原始多对多展开、缺组合关联键、隐式类型转换/
  未定义编码映射、复杂/循环/嵌套跨表公式、SUM(DISTINCT)/SELECT DISTINCT
  掩盖错误 JOIN、AVG(子组 AVG) 合并；
- “发布数据集”＝创建应用拥有的版本化视图/ES 版本索引并切换本地指针；
  与外部目标不跨库原子提交，未知结果显式标记可恢复。
"""

from dataclasses import dataclass, field
from typing import Any

DATASET_SCHEMA_VERSION = "dataset-v1"
DATASET_COMPILER_VERSION = "dataset-compile-v1"
DATASET_QUALITY_VERSION = "dataset-quality-v1"

# 数据集类型（首版两类）
DATASET_KINDS: tuple[str, ...] = ("detail_view", "aggregate")
# 状态机：草稿→校验中→就绪/校验失败→发布中→已发布/发布失败/需核对
DATASET_STATUSES: tuple[str, ...] = (
    "draft", "validating", "ready", "failed_validation",
    "publishing", "published", "failed_publish", "needs_reconciliation",
)
DATASET_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("validating",),
    "validating": ("ready", "failed_validation"),
    "ready": ("publishing",),
    "failed_validation": ("validating", "draft"),
    "publishing": ("published", "failed_publish", "needs_reconciliation"),
    "failed_publish": ("publishing", "draft"),
    # 显式核对是 needs_reconciliation 的解决路径：外部对象在→published，
    # 不在→failed_publish
    "needs_reconciliation": ("publishing", "draft", "published",
                             "failed_publish"),
    "published": ("publishing",),  # 重新刷新/发布新目标
}
# 发布目标
DATASET_TARGETS: tuple[str, ...] = ("view", "elasticsearch")
# 质量检查项结果
QUALITY_OUTCOMES: tuple[str, ...] = ("passed", "failed", "incomplete")
QUALITY_BASES: tuple[str, ...] = ("exact", "sample", "unknown")


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@dataclass
class FieldRef:
    """完整字段身份（白名单成员）。"""

    schema_name: str
    table_name: str
    column_name: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema_name, "table": self.table_name,
                "column": self.column_name}


@dataclass
class TypedFilter:
    """类型化过滤（编译为参数化谓词；不接受 SQL 片段）。"""

    field: FieldRef
    operator: str  # eq/ne/in/not_in/gt/ge/lt/le/is_null/not_null
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field.to_dict(), "operator": self.operator,
                "value": self.value}


@dataclass
class MetricSelection:
    """选用的 M4 正式指标（构建/发布时校验 ∈ formal_metric_ids）。"""

    metric_id: str
    output_alias: str

    def to_dict(self) -> dict[str, Any]:
        return {"metric_id": self.metric_id, "output_alias": self.output_alias}


@dataclass
class DimensionSelection:
    """沿安全 many_to_one/one_to_one 关系取得的维度字段。"""

    field: FieldRef
    output_alias: str
    relationship_key: str  # 经由的 M3 关系（中心→维度方向，validated）
    join_type: str = "left"  # left|inner（首版默认 left 保留中心行）

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field.to_dict(),
                "output_alias": self.output_alias,
                "relationship_key": self.relationship_key,
                "join_type": self.join_type}


@dataclass
class PreaggregationSpec:
    """一跳明细预聚合：明细按指向中心表的完整关联键分组，
    独立计算指标后在关联键上至多一行，再连中心表保持 grain。"""

    metric_id: str  # 明细实体上的 M4 指标（重定口径显式记录）
    output_alias: str
    relationship_key: str  # 明细(source)→中心(target) 的 validated 关系
    scope_note: str = ""  # 显式记录的新计算范围/目标粒度说明

    def to_dict(self) -> dict[str, Any]:
        return {"metric_id": self.metric_id, "output_alias": self.output_alias,
                "relationship_key": self.relationship_key,
                "scope_note": self.scope_note}


@dataclass
class DatasetSpec:
    """数据集定义（版本不可变；grain 必须明确“一行代表什么”）。"""

    dataset_id: str
    name: str
    source_id: str
    snapshot_id: str | None
    model_id: str
    model_version: int
    topic_id: str
    kind: str  # detail_view | aggregate
    central_entity_id: str
    grain_keys: list[str] = field(default_factory=list)  # 中心表列（有序）
    grain_statement: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)  # 中心表字段
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    preaggregations: list[dict[str, Any]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    join_type: str = "left"  # 维度 JOIN 默认（保留中心行，缺失记 NULL）
    target: str = "view"  # view | elasticsearch
    status: str = "draft"
    version: int = 1
    based_on_version: int | None = None
    created_by: str = "local-dev"
    created_at: str = field(default_factory=_now)
    change_note: str | None = None
    definition_fingerprint: str | None = None
    plan_fingerprint: str | None = None
    quality_policy: dict[str, Any] = field(default_factory=lambda: {
        "require_grain_unique": True,
        "require_reconciliation": True,
    })

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class CompiledPlan:
    """确定性编译产物（SQL 只含白名单标识符 + 参数占位）。"""

    sql: str
    params: dict[str, Any]
    lineage: dict[str, dict[str, Any]]  # 输出别名 → 来源字段
    dialect: str = "mysql"
    compiler_version: str = DATASET_COMPILER_VERSION
    plan_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"sql": self.sql, "params": self.params,
                "lineage": self.lineage, "dialect": self.dialect,
                "compiler_version": self.compiler_version,
                "plan_fingerprint": self.plan_fingerprint}


@dataclass
class QualityCheck:
    check: str
    expected: str
    actual: Any = None
    denominator: Any = None
    scope: str = ""  # 验证范围说明（窗口/全表/业务范围）
    basis: str = "unknown"  # exact | sample | unknown
    outcome: str = "incomplete"  # passed | failed | incomplete
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class QualityReport:
    dataset_id: str
    dataset_version: int
    plan_fingerprint: str | None
    definition_fingerprint: str | None
    model_id: str
    model_version: int
    snapshot_id: str | None
    checks: list[dict[str, Any]] = field(default_factory=list)
    all_passed: bool = False
    basis_note: str = (
        "质量证据代表验证时点；元数据 snapshot_id 不等于源数据事务快照，"
        "源数据变化后需重新验证")
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))
