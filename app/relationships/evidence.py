"""M3 只读证据采集、指标口径与验证策略。

口径约定（docs/data-contracts.md 关系候选节）：
- 每个指标记录 方法/分母/采样范围/时间/快照/口径（exact|sample|unknown）/
  是否截断/查询预算与适用过滤条件；
- 行覆盖率（JOIN 匹配）与唯一值覆盖率（去重）分母不同，不混用：
  覆盖率分母=左窗参与行数（COUNT DISTINCT 连接键）；唯一率分母=非空行数；
- NULL 不参与匹配（typed 等值比较）；空串与占位值不计入"有意义匹配"，
  也不自动当作 NULL；
- 窗口口径：window_rows < limit（未截断）→ exact（窗口覆盖整表）；
  达到 limit → sample；成本门槛跳过 → unknown；
- 两边均采样时未匹配可能来自样本缺失，不得据此判定关系不存在；
- SQL 只保存模板与参数摘要（预算数字），不保存业务参数值；
- 采样/截断指标不得标为全表精确值；
- JOIN 膨胀在聚合上计算（COUNT/DISTINCT），不物化多对多结果。

验证策略（rel-validate-v1）：名称相似、语义相似或高样本覆盖率均不能
单独自动升级 validated。自动 validated 需同时满足：
类型兼容、前向覆盖 exact 且 ≥ 阈值、目标端唯一（结构或 exact 口径）、
JOIN 无膨胀、支持分 ≥ medium、无阻断风险；否则 needs_review。
validated 附范围/时间/输入版本/基数限制声明，不代表无限期无条件正确。
"""

import hashlib
import json
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain.relationships import VALIDATION_POLICY_VERSION
from app.ports.connectors import (
    JoinMetricsRequest,
    RelationshipProbe,
    WindowMetricsRequest,
)

PLACEHOLDER_NOTE = (
    "空串与占位值（如 '0'、空白、默认码）不计入有意义匹配；"
    "未识别的占位形式需人工核对，本策略不假设其语义"
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ValidationBudget:
    """整次验证共享的预算（SQL 语句数/期限/窗口/成本门槛；不嵌套放大）。

    修复轮口径：max_queries 计**实际执行的 SQL 语句数**（结构核对的
    information_schema 查询、窗口与 JOIN 聚合都逐语句计数；一次包含多条
    SQL 的方法调用不得记为 1）。check_group(estimated) 在每个操作组开始前
    用下界估计做准入（剩余不足即不启动该组，避免"最后一组"超发），
    完成后按实际语句数核销（可能高于估计，如实记录）。
    """

    max_queries: int
    per_query_timeout_ms: int
    deadline_monotonic: float | None
    sample_window_rows: int
    exact_max_rows: int
    clock: Callable[[], float] = _time.monotonic  # 直接以函数为默认值（可注入）
    counter: Any = None  # StatementCounter；None 时回退内部计数（按探查调用 1 条/次）

    def __post_init__(self) -> None:
        self._fallback_statements = 0

    def statements_used(self) -> int:
        return self.counter.count if self.counter is not None else self._fallback_statements

    def note_statements(self, count: int) -> None:
        """无 counter 时的回退核销（每次探查调用按 1 条语句计）。"""
        if self.counter is None:
            self._fallback_statements += count

    def exhausted(self) -> bool:
        return self.statements_used() >= self.max_queries

    def check_group(self, estimated_statements: int) -> bool:
        """操作组准入：未超期且剩余语句预算 ≥ 估计下界。"""
        return (not self.expired()
                and self.statements_used() + estimated_statements <= self.max_queries)

    def remaining_ms(self) -> int:
        if self.deadline_monotonic is None:
            return self.per_query_timeout_ms
        return max(
            50, min(self.per_query_timeout_ms,
                    int((self.deadline_monotonic - self.clock()) * 1000)),
        )

    def expired(self) -> bool:
        return (self.deadline_monotonic is not None
                and self.clock() >= self.deadline_monotonic)


class EvidenceCollector:
    """对选定候选执行有界只读证据采集（探查端口，无任意 SQL）。

    修复轮：每个操作组前做预算准入（check_group），执行后按实际 SQL 语句
    数核销（counter 计数，失败尝试同样计入）；每条查询返回后检查期限，
    超期即停止后续采集并标记 evidence["deadline_exceeded"]（超期证据不得
    升级 validated，由 decide_status 阻断）。
    """

    def __init__(self, probe: RelationshipProbe, counter: Any = None) -> None:
        self.probe = probe
        self.counter = counter
        self.queries_used = 0  # 探查调用数（兼容记录；预算按 SQL 语句数）
        self._budget: ValidationBudget | None = None

    def _gate_budget(self, evidence: dict[str, Any], metric: str,
                     estimated: int = 1) -> bool:
        """操作组准入；不足或超期时记 gated 并返回 False。"""
        if self._budget is None:
            return True
        if self._budget.expired():
            evidence["gated"].append({"metric": metric, "reason": "budget:deadline_exceeded"})
            evidence["deadline_exceeded"] = True
            return False
        if not self._budget.check_group(estimated):
            evidence["gated"].append({
                "metric": metric,
                "reason": "budget:sql_statements_exhausted"
                f"（已用 {self._budget.statements_used()}"
                f"/{self._budget.max_queries}，组估计 ≥{estimated}）",
            })
            return False
        return True

    # -- 结构一致性（快照 vs 源库） ---------------------------------------------

    @staticmethod
    def structure_shape(table: dict[str, Any]) -> dict[str, Any]:
        return {
            "columns": [
                [c.get("name"), str(c.get("data_type", "")).upper(), c.get("nullable")]
                for c in table.get("columns") or []
            ],
            "primary_key": list(table.get("primary_key") or []),
            "foreign_keys": sorted(
                json.dumps(fk, ensure_ascii=False, default=str, sort_keys=True)
                for fk in (table.get("foreign_keys") or [])
            ),
            "indexes": sorted(
                json.dumps(
                    {"name": idx.get("name"), "columns": idx.get("columns"),
                     "unique": idx.get("unique")},
                    ensure_ascii=False, default=str, sort_keys=True,
                )
                for idx in (table.get("indexes") or [])
            ),
        }

    @classmethod
    def structure_matches(cls, snapshot_table: dict[str, Any],
                          live_table: dict[str, Any]) -> bool:
        """源库当前结构是否仍与输入快照一致（列/类型/主键/索引/外键）。"""
        return cls.structure_shape(snapshot_table) == cls.structure_shape(live_table)

    # -- 采集 -------------------------------------------------------------------

    def collect(
        self,
        record: dict[str, Any],
        source_table: dict[str, Any],
        target_table: dict[str, Any],
        budget: ValidationBudget,
    ) -> dict[str, Any]:
        """采集一个候选的全部指标；预算/期限耗尽的指标记 gated（unknown）。"""
        self._budget = budget
        source_columns = list(record["source_columns"])
        target_columns = list(record["target_columns"])
        evidence: dict[str, Any] = {
            "collected_at": _now_iso(),
            "snapshot_id": record.get("snapshot_id"),
            "policy_version": VALIDATION_POLICY_VERSION,
            "queries": [],
            "gated": [],
            "deadline_exceeded": False,
            "notes": [PLACEHOLDER_NOTE],
        }

        source_types = self._column_types(source_table)
        target_types = self._column_types(target_table)
        source_strings = tuple(
            c for c in source_columns if self._is_string(source_types.get(c)))
        target_strings = tuple(
            c for c in target_columns if self._is_string(target_types.get(c)))

        # 1) 双侧窗口统计（唯一性/空值/空串/常量列）
        source_window = self._window(
            source_table, source_columns, source_strings, budget, evidence)
        target_window = self._window(
            target_table, target_columns, target_strings, budget, evidence)
        evidence["source_side"] = source_window
        evidence["target_side"] = target_window

        # 2) 双向 JOIN 覆盖（前向 A→B；反向 B→A，方向不混淆）
        forward = self._join_coverage(
            source_table, source_columns, source_strings, source_window,
            target_table, target_columns, target_window, budget, evidence)
        backward = self._join_coverage(
            target_table, target_columns, target_strings, target_window,
            source_table, source_columns, source_window, budget, evidence,
            reverse=True)
        evidence["coverage"] = {"forward": forward, "backward": backward}

        # 3) 唯一性与基数观察（结构唯一性由服务层按快照元数据计算后传入）
        evidence["uniqueness"] = {
            "source": self._uniqueness(source_window),
            "target": self._uniqueness(target_window),
        }
        evidence["cardinality_observed"] = self._observed_cardinality(
            source_window, target_window, evidence["uniqueness"], forward,
            structural_source_unique=self._structurally_unique(
                source_table, source_columns),
            structural_target_unique=self._structurally_unique(
                target_table, target_columns),
        )
        evidence["null_impact"] = {
            "source": self._null_summary(source_window),
            "target": self._null_summary(target_window),
            "note": (
                "NULL 行不参与匹配；空串行计入覆盖排除项；"
                "两边均 sample 口径时未匹配可能来自样本缺失，不据此否定关系"
            ),
        }
        for side_name, window in (("source", source_window), ("target", target_window)):
            if window.get("constant_column"):
                evidence["notes"].append(
                    f"{side_name}_side：窗口内非空值全部相同（可能是占位/默认值列），"
                    "唯一性口径需人工复核")
        return evidence

    # -- 内部：预算化执行 ---------------------------------------------------------

    def _run_query(
        self,
        evidence: dict[str, Any],
        template: str,
        table: dict[str, Any],
        budget: ValidationBudget,
        call: Callable[[], Any],
    ) -> Any | None:
        """执行一个探查调用：组准入 → 计数测量 → 返回后期限检查。

        counter 存在时按实际 SQL 语句数核销（语句发出即计数，失败尝试同样
        计入）；返回后若已超期，标记 deadline_exceeded 并返回 None（调用方
        不再使用该结果且后续指标全部 gated）。
        """
        before = self.counter.count if self.counter is not None else None
        started = budget.clock()
        try:
            result = call()
        finally:
            self.queries_used += 1
        elapsed_ms = int((budget.clock() - started) * 1000)
        if self.counter is not None and before is not None:
            statements = self.counter.count - before
        else:
            statements = 1
            budget.note_statements(1)  # 无计数器回退：探查调用按 1 条语句核销
        evidence["queries"].append(self._query_log(
            template, table, elapsed_ms, statements))
        if budget.expired():
            # 查询返回后检查：超期结果不得用于自动升级 validated
            evidence["deadline_exceeded"] = True
        return result

    # -- 内部：单侧窗口 -----------------------------------------------------------

    def _window(
        self,
        table: dict[str, Any],
        columns: tuple[str, ...] | list[str],
        string_columns: tuple[str, ...],
        budget: ValidationBudget,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._gate_budget(evidence, f"window:{table['table_name']}"):
            return {"basis": "unknown", "gated": "budget"}
        pk = tuple(table.get("primary_key") or [])
        request = WindowMetricsRequest(
            schema_name=table["schema_name"],
            table_name=table["table_name"],
            columns=tuple(columns),
            order_by=pk,
            limit=budget.sample_window_rows,
            string_columns=string_columns,
            timeout_ms=budget.remaining_ms(),
        )
        result = self._run_query(
            evidence, "window_metrics", table, budget,
            lambda: self.probe.window_metrics(request))
        if result is None:
            return {"basis": "unknown", "gated": "deadline_exceeded_after_query"}
        basis = "sample" if result.truncated else "exact"
        meaningful = max(result.non_null_rows - result.empty_string_rows, 0)
        return {
            "window_rows": result.window_rows,
            "non_null_rows": result.non_null_rows,
            "empty_string_rows": result.empty_string_rows,
            "meaningful_rows": meaningful,
            "distinct_values": result.distinct_values,
            "basis": basis,
            "truncated": result.truncated,
            "window_unordered": not bool(pk),
            "constant_column": (
                result.distinct_values == 1 and result.non_null_rows > 1
            ),
            "sample_row_count": result.window_rows,
            "row_count_estimate": table.get("row_count_estimate"),
            "row_count_is_estimate": table.get("row_count_is_estimate", True),
            "method": "bounded_window_aggregate",
        }

    # -- 内部：单方向覆盖 ---------------------------------------------------------

    def _join_coverage(
        self,
        left_table: dict[str, Any],
        left_columns: list[str],
        left_strings: tuple[str, ...],
        left_window: dict[str, Any],
        right_table: dict[str, Any],
        right_columns: list[str],
        right_window: dict[str, Any],
        budget: ValidationBudget,
        evidence: dict[str, Any],
        *,
        reverse: bool = False,
    ) -> dict[str, Any]:
        direction = "backward" if reverse else "forward"
        if not self._gate_budget(evidence, f"join.{direction}"):
            return {"basis": "unknown", "gated": "budget"}
        # 成本门槛：决定右端模式（full：完整表受控 / windowed：双侧有界窗口 /
        # gated：拒绝执行降级 unknown）
        mode, gate_reason = self._join_cost_mode(right_table, right_columns, budget)
        if mode is None:
            evidence["gated"].append(
                {"metric": f"join.{direction}", "reason": gate_reason or ""})
            return {"basis": "unknown", "gated": gate_reason}
        left_pk = tuple(left_table.get("primary_key") or [])
        right_pk = list(right_table.get("primary_key") or [])
        right_presence = right_pk[0] if right_pk else right_columns[0]
        request_kwargs: dict[str, Any] = {}
        method = "bounded_left_window_left_join_aggregate"
        basis = left_window.get("basis", "unknown")
        notes = ["NULL 键不匹配且不入分母；空串匹配已从有意义匹配中扣除"]
        if mode == "windowed":
            request_kwargs = {
                "right_order_by": tuple(right_pk),
                "right_limit": budget.sample_window_rows,
            }
            method = "bounded_window_join_aggregate"
            notes.append(
                "右端键非唯一（展开量不可控）：改为双侧有界窗口 JOIN"
                f"（各 ≤{budget.sample_window_rows} 行，中间结果 ≤W×W 对）；"
                "右端窗口截断时未匹配可能来自窗口缺失，口径降级 sample")
            if right_window.get("truncated"):
                basis = "sample"  # 右窗未覆盖整表：覆盖结论只能是样本口径
        request = JoinMetricsRequest(
            left_schema=left_table["schema_name"],
            left_table=left_table["table_name"],
            left_columns=tuple(left_columns),
            left_order_by=left_pk,
            left_limit=budget.sample_window_rows,
            right_schema=right_table["schema_name"],
            right_table=right_table["table_name"],
            right_columns=tuple(right_columns),
            right_presence_column=right_presence,
            string_join_columns=left_strings,
            timeout_ms=budget.remaining_ms(),
            **request_kwargs,
        )
        result = self._run_query(
            evidence, f"join_metrics:{direction}:{mode}", left_table, budget,
            lambda: self.probe.join_metrics(request))
        if result is None:
            return {"basis": "unknown", "gated": "deadline_exceeded_after_query"}

        # 分母=左窗非空键行数（NULL 键行排除在覆盖率之外并单独报告）；
        # 探查的 unmatched 含 NULL 键行，需扣除窗口口径的 NULL 行数
        window_rows = left_window.get("window_rows")
        non_null_rows = left_window.get("non_null_rows")
        if window_rows is None or non_null_rows is None:
            denominator = result.left_rows_seen  # 窗口被预算跳过时的兜底
            null_key_rows = None
        else:
            denominator = non_null_rows
            null_key_rows = max(window_rows - non_null_rows, 0)
        unmatched_total = result.unmatched_left_rows
        unmatched_non_null = (
            max(unmatched_total - (null_key_rows or 0), 0)
            if null_key_rows is not None else None)
        # 无主键时探查无法给出行数：以窗口行数与 unmatched 对齐
        if denominator is None:
            denominator = window_rows
        matched_rows = (max(denominator - (unmatched_non_null
                                           if unmatched_non_null is not None
                                           else unmatched_total), 0)
                        if denominator is not None else None)
        inner_join_rows = max(result.rows_after_left_join - unmatched_total, 0)
        meaningful_matched = (
            max((matched_rows or 0) - result.empty_key_matched_rows, 0)
            if matched_rows is not None else None)
        ratio = (meaningful_matched / denominator) if denominator else None
        fanout = (inner_join_rows / matched_rows) if matched_rows else None
        return {
            "direction": f"{left_table['table_name']}→{right_table['table_name']}",
            "right_mode": mode,
            "denominator": denominator,
            "denominator_note": "左窗非空键行数（NULL 键行不计入覆盖率分母）",
            "matched": meaningful_matched,
            "matched_raw": matched_rows,
            "empty_key_matched": result.empty_key_matched_rows,
            "null_key_rows": null_key_rows,
            "unmatched": unmatched_non_null,
            "unmatched_total": unmatched_total,
            "ratio": round(ratio, 6) if ratio is not None else None,
            "basis": basis,
            "truncated": left_window.get("truncated"),
            "right_truncated": (right_window.get("truncated")
                                if mode == "windowed" else False),
            "inner_join_rows": inner_join_rows,
            "fanout_ratio": round(fanout, 6) if fanout is not None else None,
            "distinct_right_matched": result.distinct_right_matched,
            "method": method,
            "note": "；".join(notes),
        }

    # -- 内部：工具 -------------------------------------------------------------

    @staticmethod
    def _leading_prefix(columns: list[str], index_columns: list[str]) -> bool:
        """连接列必须构成索引的**前导列前缀**（按序），集合包含不算可用。"""
        return list(index_columns[:len(columns)]) == list(columns)

    def _join_cost_mode(
        self,
        right_table: dict[str, Any],
        right_columns: list[str],
        budget: ValidationBudget,
    ) -> tuple[str | None, str | None]:
        """右端 JOIN 模式判定（修复轮：前导索引 + 右端键唯一性 + 受控计数）。

        返回 ("full", None)：**仅当**右端连接键结构唯一（主键/唯一索引恰为
        连接列，按序）——该约束本身自带可用前导索引，每个左窗行经索引定位
        匹配 ≤1 行，完整右表探测成本受控；
        ("windowed", None)：其余一切情形（右端键非唯一、组合索引缺前导列、
        无索引、行数未知/低估/超大）→ 双侧有界窗口 JOIN（各 ≤W 行，中间
        结果 ≤W×W 对，受控计数，口径降级 sample）。**不使用行数估计作为
        完整表 JOIN 的安全依据**（估计是优化器近似值且可能低估，不是硬
        上限）；估计只作为证据字段如实记录；
        (None, reason)：保留降级出口（当前唯一触发：预算/期限，由调用方
        在进入本方法前处理；此返回值供未来策略扩展）。
        """
        pk = list(right_table.get("primary_key") or [])
        right = list(right_columns)
        structurally_unique = (
            pk == right
            or any(idx.get("unique") and list(idx.get("columns") or []) == right
                   for idx in (right_table.get("indexes") or []))
        )
        if structurally_unique and (
            self._leading_prefix(right, pk)
            or any(self._leading_prefix(right, list(idx.get("columns") or []))
                   for idx in (right_table.get("indexes") or []))
        ):
            return "full", None
        del budget  # 行数估计不参与 JOIN 安全判定（只记录在证据中）
        return "windowed", None

    @staticmethod
    def _column_types(table: dict[str, Any]) -> dict[str, str]:
        return {
            c["name"]: str(c.get("data_type", ""))
            for c in table.get("columns") or []
        }

    @staticmethod
    def _is_string(data_type: str | None) -> bool:
        lowered = (data_type or "").lower()
        return any(family in lowered for family in ("char", "text", "enum", "set"))

    @staticmethod
    def _uniqueness(window: dict[str, Any]) -> dict[str, Any]:
        non_null = window.get("non_null_rows")
        distinct = window.get("distinct_values")
        ratio = (distinct / non_null) if non_null and distinct is not None else None
        return {
            "distinct": distinct,
            "non_null": non_null,
            "ratio": round(ratio, 6) if ratio is not None else None,
            "basis": window.get("basis"),
            "note": "唯一值覆盖率分母=非空行数；sample 口径为窗口估计，非全表证明",
        }

    @staticmethod
    def _null_summary(window: dict[str, Any]) -> dict[str, Any]:
        return {
            "window_rows": window.get("window_rows"),
            "non_null_rows": window.get("non_null_rows"),
            "empty_string_rows": window.get("empty_string_rows"),
            "constant_column": window.get("constant_column"),
        }

    @staticmethod
    def _observed_cardinality(
        source_window: dict[str, Any],
        target_window: dict[str, Any],
        uniqueness: dict[str, Any],
        forward: dict[str, Any],
        *,
        structural_source_unique: bool,
        structural_target_unique: bool,
    ) -> dict[str, Any]:
        def _unique(side: str, structural: bool) -> tuple[str, str]:
            if structural:
                return "unique", "结构唯一（主键/唯一索引）"
            stats = uniqueness[side]
            if stats["basis"] == "exact" and stats["non_null"]:
                if stats["distinct"] == stats["non_null"]:
                    return "unique", f"exact 口径 distinct=non_null={stats['non_null']}"
                return "not_unique", (
                    f"exact 口径存在重复（distinct={stats['distinct']}，"
                    f"non_null={stats['non_null']}）")
            return "unknown", "样本口径无法证明全表唯一（样本唯一≠全表唯一）"

        source_unique, source_note = _unique("source", structural_source_unique)
        target_unique, target_note = _unique("target", structural_target_unique)
        mapping = {
            ("unique", "unique"): "one_to_one",
            ("not_unique", "unique"): "many_to_one",
            ("unique", "not_unique"): "one_to_many",
            ("not_unique", "not_unique"): "many_to_many",
        }
        cardinality = mapping.get((source_unique, target_unique), "unknown")
        return {
            "cardinality": cardinality,
            "source_uniqueness": {"value": source_unique, "note": source_note},
            "target_uniqueness": {"value": target_unique, "note": target_note},
            "forward_fanout": forward.get("fanout_ratio"),
            "note": (
                "基数=源端唯一性×目标端唯一性；fanout>1 表示目标端存在重复匹配"
                "（JOIN 膨胀风险）；unknown 表示口径不足，不代表否定"
            ),
        }

    @staticmethod
    def _structurally_unique(table: dict[str, Any], columns: list[str]) -> bool:
        if list(table.get("primary_key") or []) == columns:
            return True
        return any(
            idx.get("unique") and list(idx.get("columns") or []) == columns
            for idx in (table.get("indexes") or [])
        )

    @staticmethod
    def _query_log(
        template: str, table: dict[str, Any], elapsed_ms: int, statements: int
    ) -> dict[str, Any]:
        return {
            "template": template,  # SQL 模板名；不保存 SQL 文本与业务参数值
            "table": f"{table['schema_name']}.{table['table_name']}",
            "elapsed_ms": elapsed_ms,
            "sql_statements": statements,  # 该操作实际执行的语句数（含失败尝试）
        }


# -- 验证策略 -------------------------------------------------------------------


def decide_status(
    record: dict[str, Any],
    evidence: dict[str, Any],
    *,
    coverage_threshold: float,
) -> dict[str, Any]:
    """按 rel-validate-v1 判定 validated / needs_review。

    返回 validation 说明（decision/reasons/blocking/scope/limits），
    不修改 record。任何单项不满足都阻断自动 validated（宁可待审核）。
    """
    blocking: list[str] = []

    if evidence.get("deadline_exceeded"):
        blocking.append(
            "deadline_exceeded:证据采集期间或之后超过运行期限，"
            "超期证据不得自动升级 validated（重跑验证获取新鲜证据）")

    type_verdict = record.get("type_verdict", "unknown")
    if type_verdict != "compatible":
        blocking.append(f"type:{type_verdict}:{record.get('type_reason', '')}")

    forward = (evidence.get("coverage") or {}).get("forward") or {}
    ratio = forward.get("ratio")
    basis = forward.get("basis")
    if basis != "exact" or ratio is None:
        blocking.append(f"coverage_basis:{basis or 'unknown'}（自动验证需 exact 口径）")
    elif ratio < coverage_threshold:
        blocking.append(
            f"coverage:{ratio:.4f}<{coverage_threshold}（覆盖不足；sample 口径不能自动升级）")

    target_uniq = ((evidence.get("cardinality_observed") or {})
                   .get("target_uniqueness") or {})
    if target_uniq.get("value") != "unique":
        blocking.append(
            f"target_not_unique:{target_uniq.get('note', '口径不足')}")

    fanout = forward.get("fanout_ratio")
    if fanout is not None and fanout > 1.0 + 1e-9:
        blocking.append(f"join_fanout:{fanout:.4f}>1（目标端重复造成膨胀）")

    scores = record.get("scores") or {}
    if scores.get("total_weighted", 0.0) < 0.4:
        blocking.append(
            f"support_score:{scores.get('total_weighted', 0.0)}<0.4"
            "（名称/语义/结构支持不足；高覆盖或名称相似不能单独升级 validated）")

    for risk in record.get("risks") or []:
        if risk.get("type") in (
            "composite_key_risk", "semantics_rejected", "low_source_uniqueness",
            "structure_changed",
        ):
            blocking.append(f"risk:{risk.get('type')}:{str(risk.get('note', ''))[:80]}")

    decision = "validated" if not blocking else "needs_review"
    if decision == "validated":
        reasons = [("类型兼容、exact 前向覆盖达标、目标端唯一、无 JOIN 膨胀、"
                    "支持分达标且无阻断风险")]
    else:
        reasons = blocking[:10]
    observed = evidence.get("cardinality_observed") or {}
    source_side = evidence.get("source_side") or {}
    return {
        "policy_version": VALIDATION_POLICY_VERSION,
        "decision": decision,
        "decided_by": "policy",
        "reasons": reasons,
        "blocking": blocking[:10],
        "scope": {
            "snapshot_id": evidence.get("snapshot_id"),
            "verified_at": evidence.get("collected_at"),
            "coverage_basis": basis,
            "coverage_ratio": ratio,
            "window_rows": source_side.get("window_rows") if isinstance(source_side, dict) else None,
            "cardinality": observed.get("cardinality"),
            "exclusions": "NULL 不参与匹配；空串/占位值不计入有意义匹配",
            "limits": (
                "验证结论仅覆盖所述快照、采样窗口与排除项口径，"
                "不代表无条件或无限期正确；数据分布变化后需重验（stale）"
            ),
        },
    }


# -- 结构指纹与过期判定 -----------------------------------------------------------


def table_fingerprint(table: dict[str, Any]) -> str:
    """表结构指纹：列（名/类型/可空/顺序）+ 主键 + 外键 + 索引。"""
    shape = EvidenceCollector.structure_shape(table)
    return hashlib.sha256(
        json.dumps(shape, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def structure_changed(old_table: dict[str, Any], new_table: dict[str, Any]) -> bool:
    return table_fingerprint(old_table) != table_fingerprint(new_table)
