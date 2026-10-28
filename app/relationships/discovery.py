"""M3 关系候选召回（纯本地计算，不连接源库）。

召回策略（禁止先构造全字段笛卡尔积再截断）：
1. 显式外键：直接产出候选（含组合键有序映射与自关联），结构分满格；
2. 信号召回：只考虑"引用方特征"列（M2 foreign_identifier_candidate /
   标识符命名模式 / 非通用 token）与"被引用方特征"列（主键 / 单列唯一索引 /
   M2 entity_identifier），按非通用 token 分桶后在桶内配对，
   每个引用方列只保留 Top-K；同名词典 code↔code、status/status 等
   通用词与双端均为"被引用方"的配对一律不召回；
3. 类型兼容性先行过滤（incompatible 不召回——编码映射走人工登记），
   risky 保留并附风险；
4. M2 rejected 结论不作为正向语义证据；人工确认加权；
5. 样本唯一率只作支持信号并强制带口径 caveat（样本唯一≠全表唯一）；
6. 词法相似标记 method=lexical；无 Embedding，不伪造向量分数；
   评分为有序等级（rel-scoring-v1），不是概率。

输出携带召回理由与反例风险（同名不同义/共享状态码/租户重复编号/
组合键需求/编码映射/时间条件缺失的定性提示）。
"""

import hashlib
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.domain.relationships import SCORING_VERSION, relationship_key
from app.relationships.type_compat import type_compatibility
from app.semantics.tokenizer import tokenize

DISCOVERY_VERSION = "rel-recall-v1"

# 通用词：单独出现不构成关系信号（避免 id↔id、status↔status 大量配对）
_GENERIC_TOKENS = frozenset({
    "id", "no", "number", "code", "编码", "编号", "号码", "status", "状态",
    "type", "类型", "类别", "name", "名称", "flag", "seq", "sequence", "序号",
    "level", "version",
})

_IDENTIFIER_SUFFIX_TOKENS = {
    "id", "no", "number", "code", "key", "sequence", "编号", "号码", "编码",
}

# 引用方语义类别（M2）；被引用方语义类别
_FK_LIKE_CATEGORIES = {"foreign_identifier_candidate", "organization_identifier"}
_ENTITY_CATEGORIES = {"entity_identifier"}
_EXCLUDED_CATEGORIES = {"status", "category"}  # 共享状态码/类别枚举不作关系信号

_SCORE_WEIGHTS = {"structural": 0.4, "naming": 0.25, "semantic": 0.25, "profile": 0.1}


@dataclass
class ColumnSignals:
    table: dict[str, Any]
    column: dict[str, Any]
    tokens: list[str]
    signal_tokens: list[str]  # 非通用 token
    is_pk: bool
    in_unique_index: bool  # 单列唯一索引
    indexed: bool
    is_fk: bool
    semantics: dict[str, Any] | None
    profile: dict[str, Any] | None

    @property
    def category(self) -> str | None:
        return (self.semantics or {}).get("category")

    @property
    def review_status(self) -> str | None:
        return (self.semantics or {}).get("review_status")

    @property
    def entities(self) -> list[str]:
        return list((self.semantics or {}).get("candidate_entities") or [])


@dataclass
class RecallCandidate:
    """召回阶段的候选（服务层转换为 RelationshipRecord 持久化）。"""

    relationship_key: str
    source_table: dict[str, Any]
    source_columns: list[str]
    target_table: dict[str, Any]
    target_columns: list[str]
    origin: list[str]
    reasons: list[dict[str, Any]] = field(default_factory=list)
    support: list[dict[str, Any]] = field(default_factory=list)
    counter: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    type_verdict: str = "unknown"
    type_reason: str = ""
    cardinality_hint: str = "unknown"
    risks: list[dict[str, Any]] = field(default_factory=list)

    def signature(self) -> str:
        """幂等签名：同一快照内重复召回同一关系（同来源同分数）可跳过。"""
        payload = "|".join((
            self.relationship_key,
            ",".join(sorted(self.origin)),
            self.type_verdict,
            self.cardinality_hint,
            f"{self.scores.get('total_weighted', 0):.3f}",
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class RelationshipDiscoveryService:
    """关系候选召回核心（本地 M1/M2 输入 → 候选清单）。"""

    def recall_candidates(
        self,
        metadata: list[dict],
        profiles: dict[str, list[dict]],
        semantics: list[dict],
        *,
        topk_per_field: int = 5,
        max_candidates: int = 200,
        clock: Callable[[], float] | None = None,
        deadline: float | None = None,
    ) -> tuple[list[RecallCandidate], list[dict[str, Any]]]:
        """返回 (候选列表, 附注/跳过说明)。semantics 为 effective 字段语义。"""
        get_time = clock or _time.monotonic
        notes: list[dict[str, Any]] = []
        candidates: list[RecallCandidate] = []

        tables_by_name: dict[tuple[str, str], dict[str, Any]] = {
            (t["schema_name"], t["table_name"]): t for t in metadata
        }
        semantics_by_key = {
            (s["table_id"], s["column_name"]): s for s in semantics
        }
        profiles_by_key = {
            (table_id, p["column_name"]): p
            for table_id, plist in profiles.items() for p in plist
        }

        signals = self._build_column_signals(
            metadata, semantics_by_key, profiles_by_key)
        ref_like, entity_like = self._split_signals(signals)

        # 1) 显式外键（含组合键有序映射与自关联）；期限同样约束该阶段
        for table in metadata:
            if deadline is not None and get_time() >= deadline:
                notes.append({
                    "reason": "deadline_exceeded",
                    "message": "召回处理期限已到，剩余表的外键本轮未处理",
                })
                break
            for fk in table.get("foreign_keys") or []:
                candidate = self._fk_candidate(
                    table, fk, tables_by_name, semantics_by_key, profiles_by_key, signals)
                if candidate is None:
                    ref_table = fk.get("ref_table") or ""
                    notes.append({
                        "reason": "dangling_foreign_key",
                        "message": (
                            f"外键目标表不在当前快照目录：{table['schema_name']}."
                            f"{table['table_name']} → {fk.get('ref_schema') or table['schema_name']}."
                            f"{ref_table}（跳过，不做跨库/跨快照推断）"
                        ),
                    })
                    continue
                candidates.append(candidate)

        # 2) 信号召回（分桶 Top-K；显式外键已覆盖的不重复计分召回）
        fk_keys = {c.relationship_key for c in candidates}
        recalled = self._signal_recall(
            ref_like, entity_like,
            topk_per_field=topk_per_field,
            max_candidates=max_candidates,
            get_time=get_time, deadline=deadline,
            existing_keys=fk_keys, notes=notes,
        )
        candidates.extend(recalled)

        candidates.sort(key=lambda c: -c.scores.get("total_weighted", 0.0))
        if len(candidates) > max_candidates:
            notes.append({
                "reason": "budget_truncated",
                "message": f"候选总数预算 {max_candidates}，超出 {len(candidates) - max_candidates} 条被截断",
            })
            candidates = candidates[:max_candidates]
        return candidates, notes

    # -- 按映射重建候选（重验重绑用，复用同一套依据计算） -------------------------

    def build_candidate_for_mapping(
        self,
        metadata: list[dict],
        profiles: dict[str, list[dict]],
        semantics: list[dict],
        *,
        source_schema: str,
        source_table: str,
        source_columns: list[str],
        target_schema: str,
        target_table: str,
        target_columns: list[str],
    ) -> RecallCandidate | None:
        """按给定映射在**给定（目标）快照输入**上重建候选依据。

        重验重绑专用：稳定逻辑身份（两端标识+有序映射）来自旧链，候选依据
        （类型兼容/外键存在性/结构唯一性/风险/证据/评分/版本）全部按目标
        快照数据重算，绝不继承旧快照内容：

        - 目标快照仍存在**映射一致的外键** → 走 _fk_candidate（结构依据真实）；
        - 无外键 → 尝试按常规信号机制重建（_build_signal_candidate；类型
          跨族或无信号时返回 None）；
        - 信号机制无法召回 → 保留为**显式重验请求**：origin 仅标记
          revalidate_rebind（reasons 注明），不伪造结构/语义支持；类型结论
          与组合键风险仍如实计算并记录。

        两端表或字段在目标快照不存在 → 返回 None（调用方报告不可重验）。
        """
        tables_by_name = {
            (t["schema_name"], t["table_name"]): t for t in metadata
        }
        src = tables_by_name.get((source_schema, source_table))
        tgt = tables_by_name.get((target_schema, target_table))
        if src is None or tgt is None:
            return None
        src_names = {c["name"] for c in src.get("columns") or []}
        tgt_names = {c["name"] for c in tgt.get("columns") or []}
        if not set(source_columns) <= src_names or not set(target_columns) <= tgt_names:
            return None

        semantics_by_key = {
            (s["table_id"], s["column_name"]): s for s in semantics
        }
        profiles_by_key = {
            (table_id, p["column_name"]): p
            for table_id, plist in profiles.items() for p in plist
        }
        signals = self._build_column_signals(
            metadata, semantics_by_key, profiles_by_key)

        # 1) 外键仍存在且映射一致 → 完整结构依据（来自目标快照）。
        #    有效目标 Schema：fk.ref_schema 有值时用之，否则回退来源表 schema；
        #    必须与请求的 target_schema 一致——指向其他 Schema 的同名表
        #    （如 archive.guest）不能作为本映射的 explicit_fk 依据。
        for fk in src.get("foreign_keys") or []:
            effective_ref_schema = fk.get("ref_schema") or src["schema_name"]
            if (list(fk.get("columns") or []) == list(source_columns)
                    and effective_ref_schema == target_schema
                    and fk.get("ref_table") == target_table
                    and list(fk.get("ref_columns") or []) == list(target_columns)):
                return self._fk_candidate(
                    src, fk, tables_by_name, semantics_by_key, profiles_by_key,
                    signals)

        verdict, reason = self._pair_type_verdict(
            src, source_columns, tgt, target_columns)
        key = relationship_key(
            src["source_id"], source_schema, source_table, source_columns,
            target_schema, target_table, target_columns)

        # 2) 无外键：按常规信号机制重建（单列；类型跨族/无信号返回 None）
        if len(source_columns) == 1 and len(target_columns) == 1:
            by_identity = {
                (sig.table["table_id"], sig.column["name"]): sig for sig in signals
            }
            ref = by_identity.get((src["table_id"], source_columns[0]))
            target = by_identity.get((tgt["table_id"], target_columns[0]))
            if ref is not None and target is not None:
                candidate = self._build_signal_candidate(ref, target)
                if candidate is not None:
                    return candidate

        # 3) 显式重验请求：不伪造结构/语义支持；类型与组合键风险如实记录
        target_pk = list(tgt.get("primary_key") or [])
        risks: list[dict[str, Any]] = []
        if verdict != "compatible":
            risks.append({"type": "type_risk", "note": reason})
        if len(target_pk) > 1 and not any(
            idx.get("unique") and list(idx.get("columns") or []) == target_columns
            for idx in (tgt.get("indexes") or [])
        ):
            risks.append({
                "type": "composite_key_risk",
                "note": f"目标表主键为组合键（{','.join(target_pk)}），"
                        "当前映射可能不足，需组合字段或人工确认",
            })
        source_unique = self._columns_unique(src, source_columns)
        target_unique = self._columns_unique(tgt, target_columns)
        candidate = RecallCandidate(
            relationship_key=key,
            source_table=src,
            source_columns=list(source_columns),
            target_table=tgt,
            target_columns=list(target_columns),
            origin=[],
            reasons=[{
                "origin": "revalidate_rebind", "method": "explicit",
                "detail": "目标快照内无对应外键且不满足常规召回信号；"
                          "保留为显式重验请求，候选依据按目标快照重算，"
                          "不继承旧链内容",
            }],
            risks=risks,
            type_verdict=verdict,
            type_reason=reason,
            cardinality_hint=self._cardinality(source_unique, target_unique),
        )
        candidate.scores = self._score(
            structural=(0.7 if target_unique == "unique" else 0.3),
            naming=self._naming_overlap(source_columns, target_columns),
            semantic=self._semantic_match(
                src, source_columns, tgt, target_columns, semantics_by_key),
            profile=self._profile_support(src, source_columns, profiles_by_key),
        )
        return candidate

    # -- 信号构建 ---------------------------------------------------------------

    @staticmethod
    def _build_column_signals(
        metadata: list[dict[str, Any]],
        semantics_by_key: dict[tuple[str, str], dict[str, Any]],
        profiles_by_key: dict[tuple[str, str], dict[str, Any]],
    ) -> list[ColumnSignals]:
        out: list[ColumnSignals] = []
        for table in metadata:
            pk = set(table.get("primary_key") or [])
            fk_columns = {
                col for fk in (table.get("foreign_keys") or [])
                for col in (fk.get("columns") or [])
            }
            unique_single = {
                (idx.get("columns") or [None])[0]
                for idx in (table.get("indexes") or [])
                if idx.get("unique") and len(idx.get("columns") or []) == 1
            }
            indexed_columns = {
                col for idx in (table.get("indexes") or [])
                for col in (idx.get("columns") or [])
            }
            for column in table.get("columns") or []:
                name = column["name"]
                comment = column.get("comment") or ""
                tokens = tokenize(name)
                comment_tokens = [t for t in tokenize(comment) if t not in tokens]
                signal_tokens = [t for t in tokens + comment_tokens
                                 if t not in _GENERIC_TOKENS]
                out.append(ColumnSignals(
                    table=table,
                    column=column,
                    tokens=tokens + comment_tokens,
                    signal_tokens=signal_tokens,
                    is_pk=name in pk,
                    in_unique_index=name in unique_single,
                    indexed=name in indexed_columns,
                    is_fk=name in fk_columns,
                    semantics=semantics_by_key.get((table["table_id"], name)),
                    profile=profiles_by_key.get((table["table_id"], name)),
                ))
        return out

    @staticmethod
    def _split_signals(
        signals: list[ColumnSignals],
    ) -> tuple[list[ColumnSignals], list[ColumnSignals]]:
        """(引用方特征列, 被引用方特征列)。双端均被引用方特征的配对不召回。"""
        ref_like: list[ColumnSignals] = []
        entity_like: list[ColumnSignals] = []
        for sig in signals:
            category = sig.category
            if category in _EXCLUDED_CATEGORIES:
                continue  # 共享状态码/类别枚举：不作为关系信号（负例防线）
            identifier_named = (
                bool(sig.tokens)
                and (sig.tokens[-1] in _IDENTIFIER_SUFFIX_TOKENS
                     or sig.column["name"].lower().endswith("_id"))
            )
            if (category in _FK_LIKE_CATEGORIES
                    or (identifier_named and not sig.is_pk)):
                ref_like.append(sig)
            if sig.is_pk or sig.in_unique_index or category in _ENTITY_CATEGORIES:
                entity_like.append(sig)
        return ref_like, entity_like

    # -- 显式外键 ---------------------------------------------------------------

    def _fk_candidate(
        self,
        table: dict[str, Any],
        fk: dict[str, Any],
        tables_by_name: dict[tuple[str, str], dict[str, Any]],
        semantics_by_key: dict[tuple[str, str], dict[str, Any]],
        profiles_by_key: dict[tuple[str, str], dict[str, Any]],
        signals: list[ColumnSignals],
    ) -> RecallCandidate | None:
        columns = list(fk.get("columns") or [])
        ref_columns = list(fk.get("ref_columns") or [])
        if not columns or len(columns) != len(ref_columns):
            return None
        ref_schema = fk.get("ref_schema") or table["schema_name"]
        target = tables_by_name.get((ref_schema, fk.get("ref_table") or ""))
        if target is None:
            return None
        verdict, reason = self._pair_type_verdict(
            table, columns, target, ref_columns)
        self_reference = target["table_id"] == table["table_id"]
        # 结构性候选即使类型 risky 也保留（外键是权威结构证据），风险如实标注
        candidate = RecallCandidate(
            relationship_key=relationship_key(
                table["source_id"], table["schema_name"], table["table_name"], columns,
                ref_schema, target["table_name"], ref_columns),
            source_table=table,
            source_columns=columns,
            target_table=target,
            target_columns=ref_columns,
            origin=["explicit_fk"],
            reasons=[{
                "origin": "explicit_fk",
                "detail": "数据库声明的外键约束（有序字段映射）",
            }],
            support=[{
                "type": "foreign_key",
                "ref": f"{fk.get('name') or 'fk'}",
                "detail": f"{table['table_name']}({','.join(columns)}) → "
                          f"{target['table_name']}({','.join(ref_columns)})",
            }],
            type_verdict=verdict,
            type_reason=reason,
        )
        if self_reference:
            candidate.risks.append({
                "type": "self_referential",
                "note": "自关联关系；路径搜索需防环",
            })
        target_pk = list(target.get("primary_key") or [])
        if len(target_pk) > len(ref_columns) and not any(
            idx.get("unique") and list(idx.get("columns") or []) == ref_columns
            for idx in (target.get("indexes") or [])
        ):
            candidate.risks.append({
                "type": "composite_key_risk",
                "note": f"目标表主键为组合键（{','.join(target_pk)}），"
                        "当前映射字段不足，需组合字段或人工确认",
            })
        if verdict != "compatible":
            candidate.risks.append({"type": "type_risk", "note": reason})
        # 基数提示：源端组合列是否唯一
        source_unique = self._columns_unique(table, columns)
        target_unique = self._columns_unique(target, ref_columns)
        candidate.cardinality_hint = self._cardinality(source_unique, target_unique)
        candidate.scores = self._score(
            structural=1.0 if verdict != "incompatible" else 0.6,
            naming=self._naming_overlap(columns, ref_columns),
            semantic=self._semantic_match(
                table, columns, target, ref_columns, semantics_by_key),
            profile=self._profile_support(
                table, columns, profiles_by_key),
        )
        del signals  # 预留扩展：逐列信号细节（当前以结构证据为主）
        return candidate

    @staticmethod
    def _columns_unique(table: dict[str, Any], columns: list[str]) -> str:
        """结构唯一性：pk / 唯一索引 / 非唯一。样本唯一不算（口径 caveat）。"""
        pk = list(table.get("primary_key") or [])
        if pk == columns:
            return "unique"
        for idx in table.get("indexes") or []:
            if idx.get("unique") and list(idx.get("columns") or []) == columns:
                return "unique"
        return "not_unique"

    @staticmethod
    def _cardinality(source_unique: str, target_unique: str) -> str:
        if source_unique == "unique" and target_unique == "unique":
            return "one_to_one"
        if source_unique == "not_unique" and target_unique == "unique":
            return "many_to_one"
        if source_unique == "unique" and target_unique == "not_unique":
            return "one_to_many"
        return "many_to_many"

    def _pair_type_verdict(
        self,
        source_table: dict[str, Any], source_columns: list[str],
        target_table: dict[str, Any], target_columns: list[str],
    ) -> tuple[str, str]:
        """逐对判断类型兼容；取最差结论（组合键任一对不兼容即整体不兼容）。"""
        source_types = {
            c["name"]: str(c.get("data_type", "")) for c in source_table.get("columns") or []
        }
        target_types = {
            c["name"]: str(c.get("data_type", "")) for c in target_table.get("columns") or []
        }
        verdicts = [
            type_compatibility(
                source_types.get(sc, ""), target_types.get(tc, ""))
            for sc, tc in zip(source_columns, target_columns, strict=True)
        ]
        ranks = {"incompatible": 0, "unknown": 1, "risky": 2, "compatible": 3}
        worst = min(verdicts, key=lambda v: ranks[v[0]])
        if all(v[0] == "compatible" for v in verdicts):
            return "compatible", "全部字段对类型兼容"
        reasons = "; ".join(f"{v[0]}: {v[1]}" for v in verdicts if v[0] != "compatible")
        return worst[0], reasons or worst[1]

    # -- 信号召回 ---------------------------------------------------------------

    @staticmethod
    def _token_variants(tokens: list[str]) -> set[str]:
        """轻量词形变体（英文复数去 s）；只用于召回桶匹配，不改变 M2 拆词。"""
        variants: set[str] = set()
        for token in tokens:
            variants.add(token)
            if len(token) > 3 and token.endswith("s"):
                variants.add(token[:-1])
        return variants

    def _target_identity_tokens(self, target: ColumnSignals) -> set[str]:
        """目标侧身份 token：列信号 token ∪ 表名 token（均去通用词，含变体）。

        guests.id 这类"裸主键"靠表名词法信号参与匹配（method=lexical）。
        """
        table_tokens = [
            t for t in tokenize(target.table.get("table_name") or "")
            if t not in _GENERIC_TOKENS
        ]
        return self._token_variants(
            [t for t in target.signal_tokens if t not in _GENERIC_TOKENS]
        ) | self._token_variants(table_tokens)

    def _signal_recall(
        self,
        ref_like: list[ColumnSignals],
        entity_like: list[ColumnSignals],
        *,
        topk_per_field: int,
        max_candidates: int,
        get_time: Callable[[], float],
        deadline: float | None,
        existing_keys: set[str],
        notes: list[dict[str, Any]],
    ) -> list[RecallCandidate]:
        # 分桶：目标身份 token → 被引用方特征列（同源同快照，桶内配对；
        # 无任何身份 token 的目标列不可被词法召回，进入哨兵桶隔离）
        buckets: dict[str, list[ColumnSignals]] = {}
        for sig in entity_like:
            keys = self._target_identity_tokens(sig)
            if not keys:
                keys = {f"__isolated_{sig.table['table_id']}_{sig.column['name']}"}
            for token in keys:
                buckets.setdefault(token, []).append(sig)

        candidates: list[RecallCandidate] = []
        for ref in ref_like:
            if deadline is not None and get_time() >= deadline:
                notes.append({
                    "reason": "deadline_exceeded",
                    "message": "召回处理期限已到，剩余引用方列本轮未处理",
                })
                break
            scored: list[tuple[float, ColumnSignals]] = []
            seen_targets: set[tuple[str, str]] = set()
            for token in self._token_variants(ref.signal_tokens):
                for target in buckets.get(token, ()):
                    identity = (target.table["table_id"], target.column["name"])
                    if identity in seen_targets:
                        continue
                    if identity == (ref.table["table_id"], ref.column["name"]):
                        continue
                    seen_targets.add(identity)
                    scored.append((self._pair_score(ref, target), target))
            scored.sort(key=lambda item: (-item[0], item[1].table["table_id"],
                                          item[1].column["name"]))
            emitted = 0
            for score, target in scored:
                if emitted >= topk_per_field or len(candidates) >= max_candidates:
                    break
                candidate = self._build_signal_candidate(ref, target)
                if candidate is None or candidate.relationship_key in existing_keys:
                    continue
                existing_keys.add(candidate.relationship_key)
                candidates.append(candidate)
                emitted += 1
        return candidates

    def _pair_score(self, ref: ColumnSignals, target: ColumnSignals) -> float:
        naming = self._token_jaccard_sets(
            self._token_variants(ref.signal_tokens), self._target_identity_tokens(target))
        semantic = 0.0
        if ref.category in _FK_LIKE_CATEGORIES and target.category in _ENTITY_CATEGORIES:
            semantic += 0.6
        shared = set(ref.entities) & set(target.entities)
        if shared:
            semantic += 0.2
        if ref.review_status == "business_confirmed":
            semantic += 0.1
        if target.review_status == "business_confirmed":
            semantic += 0.1
        return naming + semantic

    def _build_signal_candidate(
        self, ref: ColumnSignals, target: ColumnSignals
    ) -> RecallCandidate | None:
        # 同表同列 / 引用方自身是主键且目标也是主键（id↔id）不召回
        if ref.table["table_id"] == target.table["table_id"] and \
                ref.column["name"] == target.column["name"]:
            return None
        if ref.is_pk and target.is_pk:
            return None
        verdict, reason = self._pair_type_verdict(
            ref.table, [ref.column["name"]], target.table, [target.column["name"]])
        if verdict == "incompatible":
            return None  # 编码映射等跨族候选走人工登记，不自动召回
        # 目标为组合主键但匹配列不是独立唯一列 → 组合键风险
        target_pk = list(target.table.get("primary_key") or [])
        risks: list[dict[str, Any]] = []
        if len(target_pk) > 1 and not target.in_unique_index:
            risks.append({
                "type": "composite_key_risk",
                "note": f"目标表主键为组合键（{','.join(target_pk)}），"
                        "单列匹配可能不足，需组合字段或人工确认",
            })
        if verdict == "risky" or verdict == "unknown":
            risks.append({"type": "type_risk", "note": reason})
        if ref.column["name"] == target.column["name"] and \
                ref.category and target.category and ref.category != target.category:
            risks.append({
                "type": "same_name_diff_meaning",
                "note": f"同名列但 M2 类别不一致（{ref.category} vs {target.category}）",
            })
        reasons = []
        origin = []
        target_tokens = self._target_identity_tokens(target)
        ref_tokens = self._token_variants(ref.signal_tokens)
        shared_tokens = sorted(ref_tokens & target_tokens)
        column_shared = sorted(
            set(ref.signal_tokens) & set(target.signal_tokens))
        table_tokens = [
            t for t in tokenize(target.table.get("table_name") or "")
            if t not in _GENERIC_TOKENS
        ]
        matched_via_table = bool(shared_tokens) and not column_shared and bool(
            set(shared_tokens) & self._token_variants(table_tokens))
        if shared_tokens:
            origin.append("name_rule")
            basis = ("表名词法匹配（目标列自身无信号词，依据表名，method=lexical）"
                     if matched_via_table else "字段名/注释 token 重叠（method=lexical）")
            reasons.append({
                "origin": "name_rule", "method": "lexical",
                "detail": f"{basis}：{','.join(shared_tokens[:6])}",
            })
        if ref.category in _FK_LIKE_CATEGORIES and target.category in _ENTITY_CATEGORIES:
            origin.append("lexical_semantic")
            reasons.append({
                "origin": "lexical_semantic", "method": "lexical",
                "detail": f"M2 语义：{ref.category} → {target.category}",
            })
        shared_entities = sorted(set(ref.entities) & set(target.entities))
        if shared_entities:
            reasons.append({
                "origin": "lexical_semantic", "method": "lexical",
                "detail": f"共享候选实体：{','.join(shared_entities[:3])}",
            })
        if not origin:
            return None
        source_unique = "unique" if (
            ref.is_pk or ref.in_unique_index) else "not_unique"
        target_unique = "unique" if (
            target.is_pk or target.in_unique_index) else "not_unique"
        profile_score = 0.0
        profile = ref.profile
        if profile and profile.get("uniqueness_ratio") is not None:
            basis = profile.get("metric_basis")
            ratio = profile["uniqueness_ratio"]
            # 样本唯一≠全表唯一：只作支持信号并强制 caveat
            profile_score = 0.5 if ratio >= 0.98 else (0.2 if ratio >= 0.5 else 0.0)
            reasons.append({
                "origin": "profile_feature",
                "detail": f"源端样本唯一率 {ratio:.3f}（口径={basis}，样本行数="
                          f"{profile.get('sample_row_count')}；样本唯一不能断定全表唯一）",
            })
            if basis in ("skipped", "insufficient_sample", None):
                profile_score = 0.0
            if ratio < 0.5:
                risks.append({
                    "type": "low_source_uniqueness",
                    "note": f"源端样本唯一率低（{ratio:.3f}），可能是共享编码而非外键",
                })
        semantic_score = self._semantic_signal_score(ref, target)
        if ref.review_status == "rejected" or target.review_status == "rejected":
            # M2 rejected 不作为正向语义证据
            semantic_score = 0.0
            risks.append({
                "type": "semantics_rejected",
                "note": "M2 语义结论已被人工否决，不作为正向证据",
            })
        candidate = RecallCandidate(
            relationship_key=relationship_key(
                ref.table["source_id"], ref.table["schema_name"],
                ref.table["table_name"], [ref.column["name"]],
                target.table["schema_name"], target.table["table_name"],
                [target.column["name"]]),
            source_table=ref.table,
            source_columns=[ref.column["name"]],
            target_table=target.table,
            target_columns=[target.column["name"]],
            origin=origin,
            reasons=reasons,
            counter=[],
            risks=risks,
            type_verdict=verdict,
            type_reason=reason,
            cardinality_hint=self._cardinality(source_unique, target_unique),
        )
        candidate.scores = self._score(
            structural=(0.7 if target.is_pk or target.in_unique_index else 0.3)
            + (0.1 if ref.indexed else 0.0),
            naming=self._token_jaccard_sets(ref_tokens, target_tokens),
            semantic=semantic_score,
            profile=profile_score,
        )
        return candidate

    def _semantic_signal_score(self, ref: ColumnSignals, target: ColumnSignals) -> float:
        score = 0.0
        if ref.category in _FK_LIKE_CATEGORIES and target.category in _ENTITY_CATEGORIES:
            score += 0.6
        if set(ref.entities) & set(target.entities):
            score += 0.2
        if ref.review_status == "business_confirmed":
            score += 0.1
        if target.review_status == "business_confirmed":
            score += 0.1
        return min(score, 1.0)

    # -- 评分 -------------------------------------------------------------------

    def _score(
        self, *, structural: float, naming: float, semantic: float, profile: float
    ) -> dict[str, Any]:
        total = (
            _SCORE_WEIGHTS["structural"] * min(structural, 1.0)
            + _SCORE_WEIGHTS["naming"] * min(naming, 1.0)
            + _SCORE_WEIGHTS["semantic"] * min(semantic, 1.0)
            + _SCORE_WEIGHTS["profile"] * min(profile, 1.0)
        )
        grade = "high" if total >= 0.7 else ("medium" if total >= 0.4 else "low")
        return {
            "structural": round(min(structural, 1.0), 3),
            "naming": round(min(naming, 1.0), 3),
            "semantic": round(min(semantic, 1.0), 3),
            "profile": round(min(profile, 1.0), 3),
            "naming_method": "lexical",  # 词法基线；无 Embedding，不伪造向量分
            "total_weighted": round(total, 3),
            "grade": grade,
            "weights": dict(_SCORE_WEIGHTS),
            "version": SCORING_VERSION,
            # 有序等级（相对强度），不是校准概率
        }

    def _naming_overlap(self, source_columns: list[str], target_columns: list[str]) -> float:
        return self._token_jaccard(
            [t for c in source_columns for t in tokenize(c)],
            [t for c in target_columns for t in tokenize(c)],
        )

    def _semantic_match(
        self,
        source_table: dict[str, Any], source_columns: list[str],
        target_table: dict[str, Any], target_columns: list[str],
        semantics_by_key: dict[tuple[str, str], dict[str, Any]],
    ) -> float:
        def _side(table: dict[str, Any], columns: list[str]) -> float:
            score = 0.0
            for column in columns:
                sem = semantics_by_key.get((table["table_id"], column))
                if not sem:
                    continue
                if sem.get("review_status") == "rejected":
                    continue
                if sem.get("category") in _FK_LIKE_CATEGORIES | _ENTITY_CATEGORIES:
                    score += 0.4
                if sem.get("review_status") == "business_confirmed":
                    score += 0.1
            return score

        return min(_side(source_table, source_columns) + _side(target_table, target_columns), 1.0)

    def _profile_support(
        self,
        table: dict[str, Any],
        columns: list[str],
        profiles_by_key: dict[tuple[str, str], dict[str, Any]],
    ) -> float:
        score = 0.0
        for column in columns:
            profile = profiles_by_key.get((table["table_id"], column))
            if not profile:
                continue
            ratio = profile.get("uniqueness_ratio")
            basis = profile.get("metric_basis")
            if ratio is None or basis in ("skipped", "insufficient_sample", None):
                continue
            score = max(score, 0.5 if ratio >= 0.98 else (0.2 if ratio >= 0.5 else 0.0))
        return score

    @staticmethod
    def _token_jaccard(a: list[str], b: list[str]) -> float:
        set_a, set_b = set(a), set(b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    @staticmethod
    def _token_jaccard_sets(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)
