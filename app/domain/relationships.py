"""M3 跨表关系领域契约。

设计约束（docs/data-contracts.md 关系候选节 + ADR-002）：
- 候选（candidate）不等于已验证（validated）；未经人工或策略充分验证的关系
  不得默认用于生产查询路径；
- 关系身份使用稳定逻辑键（relationship_key，由数据源与两端有序字段映射构成），
  与快照版本（snapshot_id）区分：同一逻辑关系在不同快照下各有一条证据链，
  新快照不静默覆盖旧证据；
- 方向必须保留：source 端（引用方/外键方）→ target 端（被引用方），
  A→B 与 B→A 的覆盖率与基数分别记录，不混淆；
- 组合键使用有序字段映射（source_columns[i] 对应 target_columns[i]），
  显式组合外键不得拆成独立单列关系；
- 字段身份是完整标识（schema+table+columns），不能仅凭列名合并；
- 评分为有序等级（相对强度），不是校准概率；
- 审核语义沿用 M2：人工结论不被机器修订覆盖；人工可撤销自己的结论；
  人工拒绝/过期必须改变有效结果；reviewer 仅为本地开发输入。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# -- 状态机 ---------------------------------------------------------------------
# candidate：召回候选（无数据证据）
# evidence_collected：已采集只读证据（尚未判定）
# validated：通过验证策略（机器策略充分满足或人工确认）；带范围与时效限制
# needs_review：证据不足/冲突/风险未解除，等待人工或补充证据
# rejected：人工否决（保留历史）
# stale：结构/快照变化或人工标记过期，需重验；默认路径搜索排除
# 机器可执行：→ evidence_collected / validated(策略) / needs_review / stale(结构变化)
# 仅人工可执行：→ rejected、revert（回 candidate）、显式标记 stale；
# 人工 mark_stale 是“需重验”指令而非终局判定（有效结果解析见
# select_effective_relationship）。

RELATIONSHIP_STATUSES: tuple[str, ...] = (
    "candidate",
    "evidence_collected",
    "validated",
    "needs_review",
    "rejected",
    "stale",
)

RELATIONSHIP_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate": ("evidence_collected", "validated", "needs_review", "rejected", "stale"),
    "evidence_collected": ("validated", "needs_review", "rejected", "stale"),
    "validated": ("needs_review", "rejected", "stale", "candidate"),
    "needs_review": ("evidence_collected", "validated", "rejected", "stale", "candidate"),
    "rejected": ("candidate", "stale"),
    # 机器验证运行可一次产出最终判定（子状态 evidence_collected 被包含）；
    # candidate 仅人工 revert 可达
    "stale": ("evidence_collected", "validated", "needs_review", "rejected", "candidate"),
}
# 人工专属动作（机器修订不得进入这些转换）
HUMAN_ONLY_TARGETS = frozenset({"rejected", "candidate"})

CARDINALITIES: tuple[str, ...] = (
    "one_to_one", "many_to_one", "one_to_many", "many_to_many", "unknown",
)

# 候选来源：显式外键 / 命名规则 / 词法语义（M2）/ 画像特征 / 人工登记
RELATIONSHIP_ORIGINS: tuple[str, ...] = (
    "explicit_fk", "name_rule", "lexical_semantic", "profile_feature", "human",
)

INFERENCE_SOURCES: tuple[str, ...] = ("rules", "system", "human")

SCORING_VERSION = "rel-scoring-v1"
VALIDATION_POLICY_VERSION = "rel-validate-v1"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def relationship_key(
    source_id: str,
    source_schema: str,
    source_table: str,
    source_columns: tuple[str, ...] | list[str],
    target_schema: str,
    target_table: str,
    target_columns: tuple[str, ...] | list[str],
) -> str:
    """稳定逻辑关系键：数据源 + 两端完整标识 + 有序字段映射（方向敏感）。

    同一逻辑关系跨快照共享该键；A→B 与 B→A 是不同的键（方向不丢）。
    """
    payload = "|".join((
        source_id,
        f"{source_schema}.{source_table}({','.join(source_columns)})",
        f"{target_schema}.{target_table}({','.join(target_columns)})",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


@dataclass
class RelationshipRecord:
    """持久化关系记录（修订式追加，每次写入产生新 revision）。"""

    relationship_key: str
    source_id: str
    source_schema: str
    source_table: str
    source_columns: list[str]
    target_schema: str
    target_table: str
    target_columns: list[str]
    source_table_id: str | None = None
    target_table_id: str | None = None
    # 方向固定为 source→target（source=引用方）；反向覆盖/基数单独记录在 evidence 中
    origin: list[str] = field(default_factory=list)
    cardinality: str = "unknown"
    recall_reasons: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    support_evidence: list[dict[str, Any]] = field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    scoring_version: str = SCORING_VERSION
    status: str = "candidate"
    evidence: dict[str, Any] | None = None  # 证据束（采集后填充）
    validation: dict[str, Any] | None = None  # 判定说明（范围/限制/时间/输入版本）
    snapshot_id: str | None = None  # 本修订证据所基于的输入快照
    semantics_snapshot_id: str | None = None  # 使用的 M2 语义快照
    rules_version: str | None = None
    run_id: str | None = None
    revision: int = 1
    inference_source: str = "rules"
    reviewed_by: str | None = None
    review_note: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def select_effective_relationship(rows_desc: list[dict[str, Any]]) -> dict[str, Any]:
    """有效关系选择（rows 按 revision 倒序，按 relationship_key 分组后调用）。

    规则（与 M2 语义一致的原则，纳入 stale 语义）：
    - 人工终局判定 = 人工修订且状态 ∈ validated/rejected/candidate(revert)；
      人工 mark_stale 不是终局判定，而是“需重验”指令（见下条）；
    - 最新修订为 stale → 生效 stale（过期必须改变有效结果，等待显式重验）；
    - 最新修订为人工终局判定 → 生效最新（人工结论当前，机器不覆盖）；
    - 存在最新人工终局判定 H：若其后存在 stale 修订（人工或系统），
      H 已被过期终止 → 生效最新修订（stale 之后的重验/重发现结果）；
      否则生效 H（机器修订不覆盖人工）；
    - 无人工终局判定 → 生效最新修订（rejected 仅人工产生，机器不产出）。
    """
    latest = rows_desc[0]
    if latest.get("status") == "stale":
        return latest
    if latest.get("inference_source") == "human":
        return latest
    terminal_human_rows = [
        r for r in rows_desc
        if r.get("inference_source") == "human" and r.get("status") != "stale"
    ]
    if not terminal_human_rows:
        return latest
    newest_human = terminal_human_rows[0]
    stale_after_human = next(
        (r for r in rows_desc
         if r.get("status") == "stale" and r.get("revision", 0) > newest_human["revision"]),
        None,
    )
    if stale_after_human is not None:
        return latest
    return newest_human
