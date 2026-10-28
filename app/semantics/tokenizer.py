"""字段名/注释拆词：中英文、下划线、驼峰与常见缩写。"""

import re

# 常见业务缩写 → 规范词（小写）。可按需扩充，不包含行业专属概念。
ABBREVIATIONS: dict[str, str] = {
    "amt": "amount",
    "qty": "quantity",
    "num": "number",
    "no": "number",
    "cnt": "count",
    "dt": "date",
    "tm": "time",
    "ts": "timestamp",
    "desc": "description",
    "addr": "address",
    "tel": "phone",
    "pwd": "password",
    "org": "organization",
    "dept": "department",
    "cust": "customer",
    "usr": "user",
    "mgr": "manager",
    "seq": "sequence",
    "msg": "message",
    "cfg": "config",
    "ts2": "timestamp",
}

_CAMEL_SPLIT = re.compile(
    r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z][a-z0-9]*|[a-z0-9]+|[一-鿿]+"
)
# 中文常见业务词（通用，非行业专属）；用于对无分隔中文串做贪心切分。
_CN_TERMS = (
    "手机号码", "手机号", "订单金额", "创建时间", "更新时间", "身份证", "组织机构",
    "编号", "号码", "金额", "数量", "单价", "日期", "时间", "状态", "类型", "类别",
    "名称", "描述", "说明", "备注", "订单", "客户", "用户", "部门", "组织", "机构",
    "企业", "商品", "地址", "邮箱", "密码", "编码", "代码", "标识",
)


def _split_chinese(text: str) -> list[str]:
    """对中文片段按常见业务词贪心切分；虚词作分隔符，无法切分的字符逐字保留。"""
    particles = {"的", "之", "与", "和", "及", "或"}
    tokens: list[str] = []
    rest = text
    while rest:
        if rest[0] in particles:
            rest = rest[1:]
            continue
        matched = next((term for term in _CN_TERMS if rest.startswith(term)), None)
        if matched:
            tokens.append(matched)
            rest = rest[len(matched):]
            continue
        tokens.append(rest[0])
        rest = rest[1:]
    return tokens


def tokenize(raw: str) -> list[str]:
    """把标识符拆成规范词列表（小写、去重保序）。"""
    if not raw:
        return []
    normalized = raw.strip().replace("-", "_").replace(".", "_")
    # 尾部数字与字母拆分（addr1 → addr, 1）；单字母前缀版本号保留（v2）
    normalized = re.sub(r"(?<=[A-Za-z][A-Za-z])(\d+)$", r"_\1", normalized)
    parts: list[str] = []
    for chunk in normalized.split("_"):
        if not chunk:
            continue
        if chunk.isupper() and len(chunk) <= 6:
            pieces = [chunk.lower()]  # ID/URL/HTTP 等整词缩写
        else:
            pieces = _CAMEL_SPLIT.findall(chunk)
        for piece in pieces:
            lowered = piece.lower()
            if re.fullmatch(r"[一-鿿]+", lowered):
                parts.extend(_split_chinese(lowered))
            else:
                parts.append(lowered)
    # 缩写规范化 + 去重保序
    canonical: list[str] = []
    for token in parts:
        expanded = ABBREVIATIONS.get(token, token)
        if expanded not in canonical:
            canonical.append(expanded)
    return canonical
