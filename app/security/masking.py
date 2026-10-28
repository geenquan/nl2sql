"""样本脱敏规则（M1 修复轮加固）。

画像样本必须先经过本模块才能离开进程（返回给 API 或写入存储）；脱敏发生在
持久化与 API 响应之前，不能只靠前端处理。

两层检测：
1. 列名检测：强敏感列（密码/密钥/证件/银行卡等，含可配置追加关键词）整列遮蔽；
   联系方式类列名做部分遮蔽。
2. 值格式检测（与列名无关）：邮箱、大陆手机号、18 位身份证号——即使出现在
   通用字段或嵌入在自由文本/JSON 中，也先脱敏再返回；无法确认安全的值
   （长文本、含换行、JSON 形态）默认保守遮蔽。

允许原样展示（仅截断）的类型：数字、布尔、日期时间形式的短字符串、
不含敏感格式且长度不超过 64 的普通短字符串；二进制只返回长度。
统计指标（空值率/去重/唯一率）基于原始值计算，不受脱敏影响。
"""

import re
from collections.abc import Iterable, Sequence
from typing import NamedTuple

# 强敏感关键词（小写子串匹配）；可用 settings.sensitive_column_keywords 追加。
_FULL_REDACT_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "ssn",
    "id_card",
    "idcard",
    "id_number",
    "idnumber",
    "id_no",
    "身份证",
    "证件",
    "bank_card",
    "card_no",
    "银行卡",
    "credit_card",
    "salary",
    "工资",
)

# 部分脱敏关键词：保留极小尾部信息便于人工辨认格式。
_PARTIAL_MASK_KEYWORDS = ("email", "mail", "phone", "mobile", "tel", "电话", "手机", "邮箱")

_MAX_SAMPLE_LENGTH = 64
_SAMPLE_VALUE_LIMIT = 5

# 当前脱敏规则版本。画像持久化时随 payload 记录；外发边界只信任带当前版本
# 标记且未脱敏的样本——无法证明经过当前规则处理的历史样本默认不外发。
MASKING_VERSION = "mask-v2"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MOBILE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_18_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


class MaskedSamples(NamedTuple):
    """一批脱敏后的样本及“是否发生过脱敏”的如实标记。"""

    values: list[str]
    redacted: bool


def is_sensitive_column(column_name: str, extra_keywords: Sequence[str] = ()) -> bool:
    """强敏感列返回 True（样本必须整列遮蔽）。"""
    lowered = column_name.strip().lower()
    keywords = _FULL_REDACT_KEYWORDS + tuple(k.lower() for k in extra_keywords)
    return any(keyword in lowered for keyword in keywords)


def _is_partial_mask_column(column_name: str) -> bool:
    lowered = column_name.strip().lower()
    return any(keyword in lowered for keyword in _PARTIAL_MASK_KEYWORDS)


def _is_conservative_text(text: str) -> bool:
    """自由文本/JSON 等无法确认安全的字符串：长文本、含换行、JSON 形态。"""
    stripped = text.lstrip()
    return (
        len(text) > _MAX_SAMPLE_LENGTH
        or "\n" in text
        or stripped[:1] in ("{", "[")
    )


def _mask_email(text: str) -> str:
    local, _, domain = text.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}" if local else f"***@{domain}"


def _mask_tail4(text: str) -> str:
    digits = "".join(ch for ch in text if ch.isdigit())
    tail = digits[-4:] if digits else text[-4:]
    return f"****{tail}"


def mask_value_with_flag(
    value: object, column_name: str, extra_keywords: Sequence[str] = ()
) -> tuple[str, bool]:
    """把单个样本值转为脱敏字符串；返回 (脱敏值, 是否发生了脱敏)。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(bytes(value))}B>", True
    if isinstance(value, (dict, list, set)):
        return "***", True
    if is_sensitive_column(column_name, extra_keywords):
        return "***", True
    text = value if isinstance(value, str) else str(value)

    # 值格式检测（与列名无关）。整值命中→格式化部分遮蔽；嵌入更长文本→整值保守遮蔽。
    if _ID_CARD_18_RE.search(text):
        if _ID_CARD_18_RE.fullmatch(text.strip()):
            return f"****{text.strip()[-4:]}", True
        return "***", True
    email_match = _EMAIL_RE.search(text)
    if email_match:
        if email_match.group(0) == text.strip() and text.strip() == text:
            return _mask_email(text), True
        return "***", True
    if _MOBILE_PHONE_RE.search(text):
        if _MOBILE_PHONE_RE.fullmatch(text.strip()) and text.strip() == text:
            return _mask_tail4(text), True
        return "***", True

    if _is_conservative_text(text):
        return "***", True
    if _is_partial_mask_column(column_name):
        return _partial_mask(text), True
    return text[:_MAX_SAMPLE_LENGTH], False


def _partial_mask(text: str) -> str:
    text = text[:_MAX_SAMPLE_LENGTH]
    if "@" in text:
        return _mask_email(text)
    return _mask_tail4(text) if any(ch.isdigit() for ch in text) else "***"


def mask_value(value: object, column_name: str, extra_keywords: Sequence[str] = ()) -> str:
    """单值脱敏的便捷入口；需要 redacted 标记时使用 mask_sample_values。"""
    return mask_value_with_flag(value, column_name, extra_keywords)[0]


def mask_sample_values(
    values: Iterable[object],
    column_name: str,
    limit: int = _SAMPLE_VALUE_LIMIT,
    extra_keywords: Sequence[str] = (),
) -> MaskedSamples:
    """批量脱敏并限制样本条数；redacted 如实反映任何实际发生的脱敏。"""
    masked: list[str] = []
    redacted = False
    for value in values:
        if value is None:
            continue  # NULL 不进入样本展示，由画像层单独统计
        text, was_redacted = mask_value_with_flag(value, column_name, extra_keywords)
        masked.append(text)
        redacted = redacted or was_redacted
        if len(masked) >= limit:
            break
    return MaskedSamples(masked, redacted)
