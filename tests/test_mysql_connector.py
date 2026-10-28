import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.adapters.mysql_connector import (
    MySQLConnectionConfig,
    MySQLConnector,
    build_sample_query,
    map_inspector_to_table_metadata,
    quote_mysql_identifier,
)
from app.core.exceptions import DataSourceConnectionError, NotImplementedFeatureError


def make_config(password: str = "Sup3rSecret!") -> MySQLConnectionConfig:
    return MySQLConnectionConfig(
        host="db.example.internal",
        port=3306,
        database="sales",
        username="readonly",
        password=SecretStr(password),
        charset="utf8mb4",
        connect_timeout_seconds=10,
        read_timeout_seconds=30,
    )


class TestIdentifierQuoting:
    def test_plain_identifier_is_backtick_quoted(self) -> None:
        assert quote_mysql_identifier("orders") == "`orders`"

    def test_unicode_identifier_is_quoted(self) -> None:
        assert quote_mysql_identifier("订单表") == "`订单表`"

    def test_identifier_with_space_is_quoted(self) -> None:
        assert quote_mysql_identifier("my table") == "`my table`"

    @pytest.mark.parametrize("bad", ["a`b", "a\x00b", "a\nb", "x" * 65, ""])
    def test_dangerous_identifiers_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            quote_mysql_identifier(bad)


class TestSampleQuery:
    def test_builds_quoted_select_with_pk_order(self) -> None:
        sql = build_sample_query("sales", "orders", ("id", "amount"), ("id",), limit=100)
        assert sql == "SELECT `id`, `amount` FROM `sales`.`orders` ORDER BY `id` LIMIT 100"

    def test_no_order_clause_without_pk(self) -> None:
        sql = build_sample_query("s", "t", ("a",), (), limit=10)
        assert sql == "SELECT `a` FROM `s`.`t` LIMIT 10"

    def test_limit_must_be_positive_int(self) -> None:
        with pytest.raises(ValueError):
            build_sample_query("s", "t", ("a",), (), limit=0)
        with pytest.raises(ValueError):
            build_sample_query("s", "t", ("a",), (), limit=-5)  # type: ignore[arg-type]

    def test_invalid_column_identifier_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_sample_query("s", "t", ("a`b",), (), limit=10)


class TestEngineSafety:
    def test_engine_url_hides_password(self) -> None:
        connector = MySQLConnector(make_config())
        url_text = str(connector.engine.url)
        assert url_text.startswith("mysql+pymysql://")
        assert "Sup3rSecret!" not in url_text
        assert connector.engine.url.password == "Sup3rSecret!"  # 引擎内部需要，但不可外泄


class TestErrorTranslation:
    def test_auth_failure_is_classified_and_sanitized(self) -> None:
        raw = OperationalError(
            "SELECT 1",
            None,
            RuntimeError("(1045, \"Access denied for user 'readonly'@'10.0.0.2' (using password: YES)\")"),
        )
        err = MySQLConnector(make_config())._translate_error(raw)
        assert isinstance(err, DataSourceConnectionError)
        assert err.reason == "auth_failure"
        assert "Sup3rSecret!" not in str(err)

    def test_password_in_message_is_scrubbed(self) -> None:
        raw = OperationalError(
            "SELECT 1",
            None,
            RuntimeError("could not connect to mysql+pymysql://readonly:Sup3rSecret!@db.example.internal:3306/sales"),
        )
        err = MySQLConnector(make_config())._translate_error(raw)
        assert "Sup3rSecret!" not in str(err)
        assert "***" in str(err)

    @pytest.mark.parametrize(
        ("message", "expected_reason"),
        [
            ("(1049, \"Unknown database 'sales'\")", "unknown_database"),
            ("(1142, \"SELECT command denied to user 'readonly' for table 't'\")", "insufficient_privilege"),
            ("(1144, \"INSERT command denied\")", "insufficient_privilege"),
            ("Can't connect to MySQL server on 'db.example.internal:3306' (111)", "unreachable"),
            ("(2003, \"Unknown MySQL server host\")", "unreachable"),
            ("(2003, 'Lost connection to MySQL server at query')", "timeout"),
            ("(3024, 'Query execution was interrupted, maximum statement execution time exceeded')", "timeout"),
        ],
    )
    def test_reason_classification(self, message: str, expected_reason: str) -> None:
        raw = OperationalError("SELECT 1", None, RuntimeError(message))
        err = MySQLConnector(make_config())._translate_error(raw)
        assert err.reason == expected_reason

    def test_unclassified_error_maps_to_unknown_reason_without_password(self) -> None:
        raw = ProgrammingError("SELECT 1", None, RuntimeError("(1064, 'You have an error in SQL syntax; Sup3rSecret!')"))
        err = MySQLConnector(make_config())._translate_error(raw)
        assert err.reason == "unknown"
        assert "Sup3rSecret!" not in str(err)


class TestMetadataMapping:
    def test_inspector_payload_maps_to_domain_model(self) -> None:
        table = map_inspector_to_table_metadata(
            source_id="src-1",
            schema_name="sales",
            table_name="orders",
            columns=[
                {
                    "name": "id",
                    "type": "BIGINT",
                    "nullable": False,
                    "comment": "主键",
                    "ordinal_position": 1,
                    "default": None,
                },
                {
                    "name": "customer_id",
                    "type": "BIGINT",
                    "nullable": True,
                    "comment": None,
                    "ordinal_position": 2,
                    "default": "0",
                },
            ],
            pk_constraint={"constrained_columns": ["id"]},
            foreign_keys=[
                {
                    "name": "fk_orders_customer",
                    "constrained_columns": ["customer_id"],
                    "referred_schema": None,
                    "referred_table": "customers",
                    "referred_columns": ["id"],
                }
            ],
            indexes=[
                {"name": "idx_customer", "column_names": ["customer_id"], "unique": False},
                {"name": "uk_order_no", "column_names": ["order_no"], "unique": True},
                {"name": "idx_expr", "column_names": [None], "unique": False},
            ],
            table_rows=98765,
            table_comment="订单主表",
        )
        assert table.source_id == "src-1"
        assert table.primary_key == ("id",)
        assert table.columns[0].comment == "主键"
        assert table.columns[1].default_value == "0"
        fk = table.foreign_keys[0]
        assert fk["ref_schema"] == "sales"  # referred_schema 为空时回退到本表 schema
        assert fk["ref_columns"] == ("id",)
        assert {i.name: i.unique for i in table.indexes} == {
            "idx_customer": False,
            "uk_order_no": True,
            "idx_expr": False,
        }
        assert all(None not in i.columns for i in table.indexes)
        assert table.row_count_estimate == 98765
        assert table.row_count_is_estimate is True  # information_schema 行数始终标记为估计
        assert table.table_comment == "订单主表"


class TestUnimplementedSurface:
    def test_execute_readonly_stays_explicitly_unimplemented(self) -> None:
        connector = MySQLConnector(make_config())
        with pytest.raises(NotImplementedFeatureError):
            connector.execute_readonly("SELECT 1")
