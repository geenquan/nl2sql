"""M2 字段语义领域契约。

设计约束：
- 语义类别与表用途集中在注册表，可扩展；不得把行业专属概念写进核心代码；
- 置信等级是"候选判断的相对强度"，不是校准概率；
- 证据必须是结构化引用（类型 + 引用键），服务端会核验引用真实存在；
  模型给出的解释不能被当成新增事实；
- 审核状态机与转换见 REVIEW_TRANSITIONS；人工确认 ≠ M3 关系验证 ≠ M4 发布。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# -- 语义类别注册表（可扩展；新增类别只需在此登记并同步提示词） ----------------

FIELD_CATEGORIES: tuple[str, ...] = (
    "entity_identifier",
    "foreign_identifier_candidate",
    "name",
    "description",
    "status",
    "category",
    "timestamp",
    "date",
    "quantity",
    "amount",
    "measurement",
    "organization_identifier",
    "free_text",
    "unknown",
)

# -- 表用途注册表 -------------------------------------------------------------

TABLE_PURPOSES: tuple[str, ...] = (
    "master_data",
    "transaction_event",
    "detail",
    "dictionary",
    "log_history",
    "relation",
    "unknown",
)

# -- 置信等级：候选判断强度，非概率 -------------------------------------------

CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

# -- 推断来源 -------------------------------------------------------------------

INFERENCE_SOURCES: tuple[str, ...] = ("rules", "model", "human")

# -- 审核状态与允许的转换 --------------------------------------------------------
# suggested：规则/模型候选；business_confirmed：人工业务确认（本地开发输入，
# 无身份认证）；rejected：人工否决。确认与否决都可回退为 suggested（产生新修订）。

REVIEW_STATUSES: tuple[str, ...] = ("suggested", "business_confirmed", "rejected")

REVIEW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "suggested": ("business_confirmed", "rejected"),
    "business_confirmed": ("suggested",),
    "rejected": ("suggested",),
}

# -- 证据引用类型 ----------------------------------------------------------------
# ref 必须能被服务端核验：column_type/comment/primary_key/foreign_key/index 引用
# 字段名或约束名；profile_metric 引用画像指标键（如 distinct_count_estimate）；
# name_pattern/glossary_term 引用规则模式或术语词条。

EVIDENCE_TYPES: tuple[str, ...] = (
    "column_type",
    "comment",
    "primary_key",
    "foreign_key",
    "index",
    "profile_metric",
    "name_pattern",
    "glossary_term",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Evidence:
    """结构化证据引用；detail 为人读说明，不构成新事实。"""

    type: str
    ref: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ref": self.ref, "detail": self.detail}


@dataclass
class FieldSemantics:
    source_id: str
    snapshot_id: str
    table_id: str
    column_name: str
    original_name: str
    original_comment: str | None
    suggested_business_name: str | None = None
    category: str = "unknown"
    candidate_entities: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "low"
    inference_source: str = "rules"
    model_name: str | None = None
    prompt_version: str | None = None
    rules_version: str | None = None
    run_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    revision: int = 1
    review_status: str = "suggested"
    reviewed_by: str | None = None
    review_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class TablePurpose:
    source_id: str
    snapshot_id: str
    table_id: str
    table_name: str
    purpose: str = "unknown"
    rationale: str | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "low"
    inference_source: str = "rules"
    model_name: str | None = None
    prompt_version: str | None = None
    rules_version: str | None = None
    run_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    revision: int = 1
    review_status: str = "suggested"
    reviewed_by: str | None = None
    review_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class SemanticRun:
    """一次语义分析的运行记录（输入范围、配置版本、状态与用量）。"""

    run_id: str
    source_id: str
    snapshot_id: str
    mode: str  # rules_only | model_assisted
    table_ids: list[str] = field(default_factory=list)
    input_complete: bool = True
    status: str = "running"  # running | succeeded | partial | failed
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    rules_version: str = "rules-v1"
    prompt_version: str = "prompt-v1"
    model_configured: str | None = None
    model_reported: str | None = None
    llm_calls: int = 0
    llm_prompt_tokens: int | None = None  # None = unavailable，不编造
    llm_completion_tokens: int | None = None
    elapsed_ms: int | None = None
    columns_analyzed: int = 0
    send_samples: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))
