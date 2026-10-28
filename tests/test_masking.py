"""脱敏规则测试。所有数据均为虚构，不得出现真实个人信息。"""

from app.security.masking import (
    MaskedSamples,
    is_sensitive_column,
    mask_sample_values,
    mask_value,
)


class TestStrongSensitiveColumns:
    def test_strongly_sensitive_columns_are_fully_redacted(self) -> None:
        for name in (
            "password", "user_pwd", "passwd", "api_key", "apikey", "secret_token",
            "client_secret", "credential", "ssn", "id_card_no", "身份证号",
            "id_number", "idNumber".lower(), "bank_card", "card_no", "银行卡号",
            "salary", "工资",
        ):
            assert is_sensitive_column(name) is True, name

    def test_extra_keywords_extend_detection(self) -> None:
        assert is_sensitive_column("tenant_token_v2") is True  # 默认已含 token
        assert is_sensitive_column("campus_code") is False
        assert is_sensitive_column("campus_code", extra_keywords=("campus",)) is True

    def test_sensitive_named_column_masks_every_value(self) -> None:
        result = mask_sample_values(["hunter2", "abcdef"], "user_password")
        assert result.values == ["***", "***"]
        assert result.redacted is True


class TestValueFormatDetection:
    def test_email_in_generic_column_is_masked(self) -> None:
        # contact 列名不含敏感关键词，靠值格式检测
        assert is_sensitive_column("contact") is False
        result = mask_sample_values(["zhang_san@example.com"], "contact")
        assert result.values == ["z***@example.com"]
        assert result.redacted is True

    def test_id_card_format_value_is_masked(self) -> None:
        # 18 位身份证格式（虚构）
        fake_id = "330106199001011234"
        assert is_sensitive_column(" remark ") is False
        result = mask_sample_values([fake_id], "remark")
        assert result.values == ["****1234"]
        assert result.redacted is True

    def test_mobile_phone_in_generic_column_is_masked(self) -> None:
        result = mask_sample_values(["13812345678"], "remark")
        assert result.values == ["****5678"]
        assert result.redacted is True

    def test_numeric_phone_value_is_masked(self) -> None:
        # 数字类型的手机号同样按值格式脱敏
        result = mask_sample_values([13812345678], "user_contact")
        assert result.values == ["****5678"]
        assert result.redacted is True

    def test_sensitive_value_embedded_in_text_masks_whole_value(self) -> None:
        embedded = "请联系 zhang_san@example.com 或电话 13812345678 获取帮助"
        result = mask_sample_values([embedded], "remark")
        assert result.values == ["***"]
        assert "zhang_san@example.com" not in result.values[0]

    def test_json_like_value_is_conservatively_redacted(self) -> None:
        result = mask_sample_values(['{"email": "a@b.com"}'], "ext_json")
        assert result.values == ["***"]
        result2 = mask_sample_values([{"email": "a@b.com"}], "ext_json")
        assert result2.values == ["***"]

    def test_long_free_text_is_conservatively_redacted(self) -> None:
        long_text = "这是一段超过六十四个字符的自由文本，" + "补齐长度" * 12
        assert len(long_text) > 64
        result = mask_sample_values([long_text], "remark")
        assert result.values == ["***"]


class TestAllowedDisplay:
    def test_short_plain_strings_displayed(self) -> None:
        result = mask_sample_values(["已完成", "待支付"], "status")
        assert result.values == ["已完成", "待支付"]
        assert result.redacted is False

    def test_numbers_and_booleans_displayed(self) -> None:
        result = mask_sample_values([42, 3.14, True], "amount")
        assert result.values == ["42", "3.14", "True"]
        assert result.redacted is False

    def test_date_like_strings_displayed(self) -> None:
        result = mask_sample_values(["2026-10-27 10:00:00", "2026-10-27"], "created_at")
        assert result.values == ["2026-10-27 10:00:00", "2026-10-27"]
        assert result.redacted is False

    def test_empty_string_is_displayed(self) -> None:
        result = mask_sample_values([""], "remark")
        assert result.values == [""]
        assert result.redacted is False

    def test_binary_only_exposes_length(self) -> None:
        result = mask_sample_values([b"\xde\xad\xbe\xef"], "avatar")
        assert result.values == ["<binary:4B>"]
        assert result.redacted is True


class TestPartialMaskColumns:
    def test_contact_like_columns_are_partial_masked(self) -> None:
        assert mask_value("someone@example.com", "email") == "s***@example.com"
        assert mask_value("13812345678", "mobile_phone") == "****5678"
        assert mask_value("0571-1234567", "contact_phone") == "****4567"


class TestApiShape:
    def test_masked_samples_shape_and_limit(self) -> None:
        values = [f"v{i}" for i in range(50)]
        result = mask_sample_values(values, "status", limit=5)
        assert isinstance(result, MaskedSamples)
        assert result.values == [f"v{i}" for i in range(5)]
        assert result.redacted is False

    def test_null_values_are_skipped_not_masked(self) -> None:
        result = mask_sample_values([None, "ok", None], "status")
        assert result.values == ["ok"]

    def test_extra_keywords_flow_through_samples(self) -> None:
        result = mask_sample_values(["abc"], "campus_code", extra_keywords=("campus",))
        assert result.values == ["***"]
        assert result.redacted is True
