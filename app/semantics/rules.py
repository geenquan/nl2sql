"""规则初筛：字段语义类别与表用途候选。

约束：
- 每个结论带证据（结构化引用）、置信等级与规则版本；
- 画像统计必须携带口径 caveat（样本唯一≠全表唯一；外键候选≠已验证关系）；
- 信号冲突时保留多候选并降置信；证据不足必须给 unknown，不虚构。
"""

from typing import Any

from app.semantics.glossary import Glossary
from app.semantics.tokenizer import tokenize

RULES_VERSION = "rules-v1"

_NAME_HINTS: dict[str, str] = {
    "status": "status", "状态": "status",
    "type": "category", "类别": "category", "kind": "category",
    "name": "name", "名称": "name", "title": "name", "标题": "name",
    "description": "description", "说明": "description", "备注": "description",
    "note": "description", "comment": "description",
    "amount": "amount", "金额": "amount", "价格": "amount", "费用": "amount",
    "quantity": "quantity", "数量": "quantity", "count": "quantity",
    "时间": "timestamp", "日期": "date",
}

_IDENTIFIER_WORDS = {"id", "number", "code", "编号", "号码", "编码", "代码", "单号", "no"}

_NUMERIC_TYPES = ("int", "decimal", "float", "double", "numeric", "bit")
_TEMPORAL_TYPES = ("datetime", "timestamp")
_TEXT_TYPES = ("char", "text", "varchar", "enum")

# 候选优先级：约束 > 术语 > 类型 > 命名模式（同级按置信度）
_PRIORITY_CONSTRAINT = 0
_PRIORITY_GLOSSARY = 1
_PRIORITY_TYPE = 2
_PRIORITY_PATTERN = 3


def _ev(evidence_type: str, ref: str, detail: str = "") -> dict[str, Any]:
    return {"type": evidence_type, "ref": ref, "detail": detail}


def _profile_caveat(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    return (
        f"画像口径={profile.get('metric_basis')}，样本行数={profile.get('sample_row_count')}；"
        "样本唯一不能断定全表唯一"
    )


def _rank(confidence: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}[confidence]


class _Candidate:
    __slots__ = ("category", "confidence", "evidence", "priority", "rationale")

    def __init__(self, category: str, confidence: str, priority: int,
                 evidence: list[dict[str, Any]], rationale: str) -> None:
        self.category = category
        self.confidence = confidence
        self.priority = priority
        self.evidence = evidence
        self.rationale = rationale

    def sort_key(self) -> tuple[int, int]:
        return (self.priority, _rank(self.confidence))


class RuleEngine:
    def __init__(self, glossary: Glossary | None = None) -> None:
        self._glossary = glossary or Glossary.load_default()

    # -- 字段 ----------------------------------------------------------------

    def infer_column(self, ctx: dict[str, Any]) -> dict[str, Any]:
        column: dict[str, Any] = ctx["column"]
        name = column["name"]
        data_type = str(column.get("data_type", "")).lower()
        comment = column.get("comment")
        tokens = tokenize(name)
        comment_tokens = tokenize(comment) if comment else []
        all_tokens = tokens + [t for t in comment_tokens if t not in tokens]
        glossary_hit = self._glossary.hit(all_tokens)

        candidates: list[_Candidate] = []
        entities: list[str] = []
        is_pk = bool(ctx.get("is_primary_key"))
        is_fk = bool(ctx.get("is_foreign_key"))
        profile: dict[str, Any] | None = ctx.get("profile")

        if is_pk:
            evidence = [_ev("primary_key", name, "主键约束")]
            if profile and profile.get("uniqueness_ratio") is not None:
                evidence.append(_ev("profile_metric", "uniqueness_ratio", _profile_caveat(profile)))
            candidates.append(_Candidate(
                "entity_identifier", "high", _PRIORITY_CONSTRAINT, evidence, "主键约束"))

        identifier_like = bool(tokens) and (
            tokens[-1] in _IDENTIFIER_WORDS or name.lower().endswith("_id")
        )
        if is_fk:
            candidates.append(_Candidate(
                "foreign_identifier_candidate", "high", _PRIORITY_CONSTRAINT,
                [_ev("foreign_key", name, "外键约束；候选不等于已验证关系")],
                "外键约束（语义相似不代表可 JOIN，关系验证在 M3）"))
        elif identifier_like and not is_pk:
            candidates.append(_Candidate(
                "foreign_identifier_candidate", "medium", _PRIORITY_PATTERN,
                [_ev("name_pattern", "/(_id|_no|_code|编号|号码)$/", "标识符命名模式；未验证关系，不能据此 JOIN")],
                "命名模式提示标识符（是否真实外键需 M3 验证）"))

        if "timestamp" in data_type or "datetime" in data_type or "timestamp" in tokens:
            candidates.append(_Candidate(
                "timestamp", "medium", _PRIORITY_TYPE,
                [_ev("column_type", name, data_type or "timestamp")], "时间类型"))
        if data_type.startswith("date") or "日期" in tokens or "date" in tokens:
            candidates.append(_Candidate(
                "date", "medium", _PRIORITY_TYPE,
                [_ev("column_type", name, data_type or "date")], "日期类型/命名"))

        if glossary_hit:
            if glossary_hit.get("category"):
                glossary_evidence = [
                    _ev("glossary_term", glossary_hit["term"], "术语表命中")]
                if comment and glossary_hit["term"] in comment:
                    glossary_evidence.append(_ev("comment", comment, "注释提示"))
                candidates.append(_Candidate(
                    glossary_hit["category"], "medium", _PRIORITY_GLOSSARY,
                    glossary_evidence,
                    f"术语表命中：{glossary_hit['term']}"))
            entities.extend(glossary_hit.get("entities", []))

        name_hint = next((_NAME_HINTS[t] for t in all_tokens if t in _NAME_HINTS), None)
        if name_hint:
            matched_tokens = [t for t in all_tokens if t in _NAME_HINTS]
            hint_evidence = [
                _ev("name_pattern", "/(" + "|".join(matched_tokens) + ")/", "命名/注释提示")
            ]
            if name_hint in comment_tokens and comment:
                hint_evidence.append(_ev("comment", comment, "注释提示"))
            candidates.append(_Candidate(
                name_hint, "medium", _PRIORITY_PATTERN, hint_evidence, "命名/注释提示"))

        if not candidates:
            return {
                "category": "unknown", "confidence": "low",
                "suggested_business_name": _suggest_name(name, tokens, comment),
                "candidate_entities": [], "alternatives": [],
                "evidence": [_ev("column_type", name, f"仅字段类型 {data_type or 'unknown'}，无语义信号")],
                "rationale": "证据不足，保持 unknown",
                "inference_source": "rules", "rules_version": RULES_VERSION,
                "value_meanings": None,
            }

        # 类型族与候选冲突检测：冲突 → 降置信，并把类型信号物化为备选
        type_family = (
            "numeric" if any(t in data_type for t in _NUMERIC_TYPES)
            else "temporal" if ("date" in data_type or any(t in data_type for t in _TEMPORAL_TYPES))
            else "text" if any(t in data_type for t in _TEXT_TYPES)
            else "other"
        )
        identifier_categories = {
            "entity_identifier", "foreign_identifier_candidate", "organization_identifier",
        }
        text_expected = ({"name", "description", "free_text", "status", "category"}
                         | identifier_categories)
        numeric_expected = {"quantity", "amount", "measurement"}
        time_expected = {"timestamp", "date"}

        def _conflicts(category: str) -> bool:
            if type_family == "numeric":
                return category in text_expected - {"status", "category"}
            if type_family == "text":
                return category in numeric_expected | time_expected
            if type_family == "temporal":
                return category not in time_expected
            return False

        candidates.sort(key=_Candidate.sort_key)
        primary = candidates[0]
        # 类型与语义信号冲突：约束级（主键/外键）结论不受类型冲突降级
        type_conflict = any(_conflicts(c.category) for c in candidates)
        has_conflict = type_conflict and primary.priority != _PRIORITY_CONSTRAINT
        confidence = "low" if has_conflict else primary.confidence

        if has_conflict and type_family == "numeric" and not any(
            c.category in numeric_expected for c in candidates
        ):
            candidates.append(_Candidate(
                "measurement", "low", _PRIORITY_TYPE,
                [_ev("column_type", name, f"数值类型 {data_type} 与文本类信号冲突的备选")], "数值类型"))
        if has_conflict and type_family == "text" and not any(
            c.category in text_expected for c in candidates
        ):
            candidates.append(_Candidate(
                "free_text", "low", _PRIORITY_TYPE,
                [_ev("column_type", name, f"文本类型 {data_type} 与数值/时间信号冲突的备选")], "文本类型"))

        evidence = list(primary.evidence)
        rationale = primary.rationale
        if has_conflict:
            rationale += "；名称/类型/注释信号冲突，保留多候选并降置信"
        evidence.append(_ev("column_type", name, data_type or "unknown"))

        seen = {primary.category}
        alternatives = []
        for candidate in candidates[1:]:
            if candidate.category in seen:
                continue
            seen.add(candidate.category)
            alternatives.append({
                "category": candidate.category,
                "rationale": candidate.rationale,
                "evidence": candidate.evidence,
                "confidence": candidate.confidence,
            })

        return {
            "category": primary.category,
            "confidence": confidence,
            "suggested_business_name": _suggest_name(name, tokens, comment),
            "candidate_entities": entities,
            "alternatives": alternatives,
            "evidence": evidence,
            "rationale": rationale,
            "inference_source": "rules",
            "rules_version": RULES_VERSION,
            "value_meanings": None,  # 状态取值含义不凭行业常识推断
        }

    # -- 表用途 ----------------------------------------------------------------

    def infer_table_purpose(self, ctx: dict[str, Any]) -> dict[str, Any]:
        table_name = ctx.get("table_name", "")
        columns: list[dict[str, Any]] = ctx.get("columns", [])
        primary_key: list[str] = ctx.get("primary_key") or []
        foreign_keys: list[dict[str, Any]] = ctx.get("foreign_keys") or []
        tokens = tokenize(table_name)
        col_tokens = {token for col in columns for token in tokenize(col.get("name", ""))}
        types = {str(col.get("data_type", "")).lower() for col in columns}

        evidence: list[dict[str, Any]] = []
        candidates: list[tuple[str, str, str, int]] = []  # purpose, conf, rationale, priority

        fk_columns = {col for fk in foreign_keys for col in (fk.get("columns") or [])}
        if len(fk_columns) >= 2 and primary_key and all(col in fk_columns for col in primary_key):
            candidates.append(("relation", "high", "主键由两个以上外键列组成", 0))
            evidence.append(_ev("foreign_key", ",".join(sorted(fk_columns)), "复合主键包含外键列"))

        if "明细" in tokens or "detail" in tokens or "item" in tokens:
            candidates.append(("detail", "medium", "表名含明细", 1))
            evidence.append(_ev("name_pattern", "明细|detail|item", "表名命名"))

        has_audit_time = bool({"created_at", "updated_at"} & col_tokens) and any(
            t in col_tokens for t in ("action", "操作", "operator"))
        if any(t in tokens for t in ("日志", "历史", "log", "history", "audit")) or has_audit_time:
            candidates.append(("log_history", "medium", "日志/历史命名或操作+时间字段", 1))
            evidence.append(_ev("name_pattern", "日志|log|history", "表名或字段命名"))

        column_count = len(columns)
        has_code = any(t in col_tokens for t in ("code", "编码", "编号", "number"))
        has_name = any(t in col_tokens for t in ("name", "名称"))
        has_amount = any(t in col_tokens for t in ("amount", "金额", "价格", "总价"))
        has_time = any("datetime" in t or "timestamp" in t for t in types)
        entity_words = ("enterprise", "企业", "客户", "customer", "商品", "goods",
                        "organization", "组织", "机构", "user", "用户", "hotel")
        entity_named = any(t in tokens for t in entity_words) or any(
            t in col_tokens for t in entity_words)

        if (any(t in tokens for t in ("订单", "交易", "order", "transaction", "单据", "事件", "event"))
                and (has_amount or has_time)):
            candidates.append(("transaction_event", "medium", "订单/交易命名且含金额或时间字段", 2))
            evidence.append(_ev("name_pattern", "订单|交易|event", "表名命名"))
            if has_amount:
                evidence.append(_ev("column_type", "amount-like", "存在金额类字段"))

        non_entity_named = not any(
            t in tokens for t in ("字典", "dict", "配置", "明细", "detail", "item", "日志", "log", "history", "历史")
        )
        if (entity_named and has_code and has_name and not has_amount and not has_time
                and non_entity_named):
            candidates.append(("master_data", "medium", "实体命名且编码+名称结构", 2))
            evidence.append(_ev("name_pattern", "master-data-shape", "表名/结构特征"))

        dict_in_name = any(t in tokens for t in ("字典", "dict", "配置", "类型"))
        if dict_in_name or (column_count <= 8 and has_code and has_name and not has_amount and not entity_named):
            candidates.append((
                "dictionary", "medium", "小型表且含编码+名称字段",
                1 if dict_in_name else 2,
            ))
            evidence.append(_ev("name_pattern", "字典|dict", "表名或结构特征"))

        if not candidates:
            return {
                "purpose": "unknown", "confidence": "low",
                "rationale": "证据不足，保持 unknown", "alternatives": [],
                "evidence": [_ev("name_pattern", table_name, "表名无有效信号")],
                "inference_source": "rules", "rules_version": RULES_VERSION,
            }

        candidates.sort(key=lambda c: (c[3], _rank(c[1])))
        primary, rest = candidates[0], candidates[1:]
        alternatives = [
            {"purpose": purpose, "rationale": rationale, "confidence": conf}
            for purpose, conf, rationale, _ in rest
        ]
        return {
            "purpose": primary[0], "confidence": primary[1],
            "rationale": primary[2], "alternatives": alternatives,
            "evidence": evidence[:6],
            "inference_source": "rules", "rules_version": RULES_VERSION,
        }


def _suggest_name(raw_name: str, tokens: list[str], comment: str | None) -> str:
    if comment and 0 < len(comment) <= 30:
        return comment
    if tokens:
        return " ".join(tokens)
    return raw_name
