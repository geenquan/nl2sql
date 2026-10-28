import pytest

from app.semantics.tokenizer import tokenize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user_name", ["user", "name"]),
        ("userName", ["user", "name"]),
        ("UserName", ["user", "name"]),
        ("HTTPStatus", ["http", "status"]),  # 连续大写缩写
        ("order_amt", ["order", "amount"]),  # 常见缩写
        ("qty", ["quantity"]),
        ("create_dt", ["create", "date"]),
        ("cust_no", ["customer", "number"]),
        ("手机号码", ["手机号码"]),  # 中文最长词优先，保留完整概念
        ("订单金额", ["订单金额"]),
        ("客户的名称", ["客户", "名称"]),  # 词典外字符逐字并继续组词
        ("ID", ["id"]),
        ("id", ["id"]),
    ],
)
def test_tokenize(raw: str, expected: list[str]) -> None:
    assert tokenize(raw) == expected


def test_tokenize_strips_and_dedupes() -> None:
    assert tokenize("  user__name  ") == ["user", "name"]
    assert tokenize("name_name") == ["name"]


def test_tokenize_keeps_alphanumeric_tokens() -> None:
    assert tokenize("addr1") == ["address", "1"]
    assert tokenize("v2_code") == ["v2", "code"]
