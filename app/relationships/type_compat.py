"""关系两端的类型兼容性判断（M3）。

不只比较类型字符串：解析族/符号/精度/长度后判断。结论三值：
- compatible：同族且无值域/精度风险，可执行 typed 等值 JOIN 证据查询；
- risky：可比较但存在风险（值域溢出/精度损失/粒度差异/文本不可比），
  候选保留并带风险标记，证据查询仍用 typed 比较（不做隐式转换）；
- incompatible：不同族（如字符串 vs 整数）。可能是"编码映射"关系，
  但不得自动 JOIN（不做隐式类型转换制造匹配率）；召回阶段排除，
  人工登记（origin=human）路径保留。
- unknown：无法解析的类型，按 risky 处理并注明。
"""

import re
from dataclasses import dataclass

_INTEGER_RANK = {
    "tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "integer": 4, "bigint": 5,
}
_STRING_FAMILIES = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"}
_TEMPORAL_FAMILIES = {"date", "datetime", "timestamp", "time", "year"}

_VERDICT_COMPATIBLE = "compatible"
_VERDICT_RISKY = "risky"
_VERDICT_INCOMPATIBLE = "incompatible"
_VERDICT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedType:
    family: str  # integer / decimal / float / string / temporal / binary / boolean / other
    detail: str  # 原始族名（int、varchar、datetime…）
    unsigned: bool = False
    precision: int | None = None
    scale: int | None = None
    length: int | None = None
    raw: str = ""

    @property
    def temporal_granularity(self) -> str | None:
        if self.detail == "date":
            return "date"
        if self.detail in ("datetime", "timestamp"):
            return "datetime"
        return None


def normalize_type(data_type: str | None) -> NormalizedType:
    raw = (data_type or "").strip()
    lowered = raw.lower()
    unsigned = "unsigned" in lowered
    # 抽出基础类型与参数：如 VARCHAR(20)、DECIMAL(10,2)、TINYINT(1)
    match = re.match(r"([a-z]+)\s*(?:\((\d+)(?:\s*,\s*(\d+))?\))?", lowered)
    if not match:
        return NormalizedType(family="other", detail="", raw=raw)
    base = match.group(1)
    p1 = int(match.group(2)) if match.group(2) else None
    p2 = int(match.group(3)) if match.group(3) else None

    if base in _INTEGER_RANK:
        # TINYINT(1) 习惯用作布尔；族仍归 integer，由调用方决定语义
        return NormalizedType("integer", base, unsigned, None, None, p1, raw)
    if base in ("decimal", "numeric"):
        return NormalizedType("decimal", base, unsigned, p1, p2, None, raw)
    if base in ("float", "double", "real"):
        return NormalizedType("float", base, unsigned, p1, p2, None, raw)
    if base in ("bit", "binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"):
        return NormalizedType("binary", base, unsigned, None, None, p1, raw)
    if base in _STRING_FAMILIES:
        return NormalizedType("string", base, unsigned, None, None, p1, raw)
    if base in _TEMPORAL_FAMILIES:
        return NormalizedType("temporal", base, unsigned, None, None, None, raw)
    return NormalizedType("other", base, unsigned, p1, p2, None, raw)


def type_compatibility(
    source_type: str | None, target_type: str | None
) -> tuple[str, str]:
    """返回 (verdict, reason)。source=引用方列类型，target=被引用方列类型。"""
    left = normalize_type(source_type)
    right = normalize_type(target_type)
    if left.family == "other" or right.family == "other":
        return _VERDICT_UNKNOWN, "存在无法解析的类型，按待审核处理"
    if left.family != right.family:
        return (
            _VERDICT_INCOMPATIBLE,
            (f"类型族不同（{left.detail} vs {right.detail}）；可能是编码映射，"
             "不做隐式转换 JOIN，需人工判断"),
        )
    if left.family == "integer":
        if left.unsigned != right.unsigned:
            return _VERDICT_RISKY, "符号性不一致（unsigned 混用），值域可能不覆盖"
        left_rank = _INTEGER_RANK.get(left.detail, 4)
        right_rank = _INTEGER_RANK.get(right.detail, 4)
        if left_rank > right_rank:
            return (
                _VERDICT_RISKY,
                f"源端整数更宽（{left.detail}→{right.detail}），存在值域溢出风险",
            )
        return _VERDICT_COMPATIBLE, "整数族一致且值域覆盖"
    if left.family == "decimal":
        if (left.scale or 0) != (right.scale or 0) or (
            (left.precision or 0) > (right.precision or 0)
        ):
            return _VERDICT_RISKY, "精度或标度不一致，存在精度损失风险"
        return _VERDICT_COMPATIBLE, "DECIMAL 精度标度一致"
    if left.family == "float":
        return _VERDICT_RISKY, "浮点族比较存在精度风险（建议 DECIMAL 口径复核）"
    if left.family == "string":
        if left.detail != right.detail and ("text" in (left.detail, right.detail)):
            return _VERDICT_RISKY, f"TEXT 与 {left.detail if left.detail != 'text' else right.detail} 长度不可比"
        if left.length is not None and right.length is not None and left.length > right.length:
            return _VERDICT_RISKY, f"源端更长（{left.length}>{right.length}），可能截断不匹配"
        return _VERDICT_COMPATIBLE, "字符串族一致"
    if left.family == "temporal":
        if left.temporal_granularity != right.temporal_granularity:
            return (
                _VERDICT_RISKY,
                f"时间粒度不同（{left.detail} vs {right.detail}），匹配率可能失真",
            )
        return _VERDICT_COMPATIBLE, "时间类型一致"
    if left.family == "binary":
        return _VERDICT_COMPATIBLE, "二进制族一致"
    return _VERDICT_UNKNOWN, "未覆盖的类型组合，按待审核处理"
