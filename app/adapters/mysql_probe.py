"""MySQL 关系证据探查适配器（M3）。

只读与安全策略（与 mysql_connector.py 同一族，缺一不可）：
1. 复用 MySQLConnector 的连接级 READ ONLY（init_command）与账号 SELECT 约束；
2. 只执行由 WindowMetricsRequest / JoinMetricsRequest 构造的系统聚合查询：
   - 标识符全部来自快照元数据白名单，经 quote_mysql_identifier 校验转义；
   - LIMIT / 超时为受校验整数（不做字符串拼接插值）；
   - 查询不携带任何业务参数值，结果只返回聚合计数，不返回原始行；
3. 数据库级执行期限：SELECT 优化器提示 MAX_EXECUTION_TIME(ms)（MySQL ≥5.7.8
   顶层 SELECT 生效；MariaDB 不支持该提示，未知提示被忽略并回退为客户端
   read_timeout——支持范围以此为准，不宣称所有版本都有服务端期限）；
4. 错误经 MySQLConnector._translate_error 脱敏（同适配器家族内部复用），
   不携带 SQL 文本、参数或业务值。
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.adapters.mysql_connector import (
    MySQLConnectionConfig,
    MySQLConnector,
    quote_mysql_identifier,
)
from app.core.exceptions import DataSourceConnectionError
from app.ports.connectors import (
    JoinMetricsRequest,
    JoinMetricsResult,
    RelationshipProbe,
    WindowMetricsRequest,
    WindowMetricsResult,
)

_MAX_TIMEOUT_MS = 3_600_000  # 1 小时上限，防御异常配置
_MIN_TIMEOUT_MS = 50


def _timeout_ms(clamped: int) -> int:
    return max(_MIN_TIMEOUT_MS, min(int(clamped), _MAX_TIMEOUT_MS))


class MySQLRelationshipProbe(RelationshipProbe):
    """基于 MySQLConnector 引擎的只读聚合探查。"""

    def __init__(
        self,
        connection_config: MySQLConnectionConfig,
        engine: Engine | None = None,
    ) -> None:
        self._connector = MySQLConnector(connection_config)
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = self._connector.engine
        return self._engine

    def close(self) -> None:
        self._connector.close()
        self._engine = None

    # -- SQL 构造（系统构造，标识符白名单） ------------------------------------

    @staticmethod
    def _window_subquery(request: WindowMetricsRequest) -> str:
        columns = ", ".join(quote_mysql_identifier(c) for c in request.columns)
        source = (
            f"{quote_mysql_identifier(request.schema_name)}"
            f".{quote_mysql_identifier(request.table_name)}"
        )
        order = (
            " ORDER BY " + ", ".join(quote_mysql_identifier(c) for c in request.order_by)
            if request.order_by else ""
        )
        return f"(SELECT {columns} FROM {source}{order} LIMIT {int(request.limit)})"

    def window_metrics(self, request: WindowMetricsRequest) -> WindowMetricsResult:
        non_null_expr = " AND ".join(
            f"{quote_mysql_identifier(c)} IS NOT NULL" for c in request.columns
        )
        select_parts = ["COUNT(*) AS window_rows", f"SUM({non_null_expr}) AS non_null_rows"]
        if request.string_columns:
            empty_expr = " OR ".join(
                f"{quote_mysql_identifier(c)} = ''" for c in request.string_columns
            )
            select_parts.append(f"SUM(({non_null_expr}) AND ({empty_expr})) AS empty_string_rows")
        else:
            select_parts.append("0 AS empty_string_rows")
        if len(request.columns) == 1:
            col = quote_mysql_identifier(request.columns[0])
            select_parts.append(f"COUNT(DISTINCT {col}) AS distinct_values")
        else:
            concat = ", ".join(quote_mysql_identifier(c) for c in request.columns)
            select_parts.append(
                "COUNT(DISTINCT CASE WHEN " + non_null_expr
                + f" THEN CONCAT_WS(CHAR(31), {concat}) END) AS distinct_values"
            )
        sql = (
            f"SELECT /*+ MAX_EXECUTION_TIME({_timeout_ms(request.timeout_ms)}) */ "
            + ", ".join(select_parts) + " FROM " + self._window_subquery(request) + " AS w"
        )
        row = self._fetch_one(sql)
        window_rows = int(row["window_rows"] or 0)
        return WindowMetricsResult(
            window_rows=window_rows,
            non_null_rows=int(row["non_null_rows"] or 0),
            empty_string_rows=int(row["empty_string_rows"] or 0),
            distinct_values=int(row["distinct_values"] or 0),
            truncated=window_rows >= int(request.limit),
        )

    def join_metrics(self, request: JoinMetricsRequest) -> JoinMetricsResult:
        # 左窗选择连接键 + 主键列（主键用于精确行数；与连接键去重）
        select_columns = list(dict.fromkeys(
            list(request.left_columns) + list(request.left_order_by)))
        left_select = ", ".join(
            quote_mysql_identifier(c) for c in select_columns
        )
        left_source = (
            f"{quote_mysql_identifier(request.left_schema)}"
            f".{quote_mysql_identifier(request.left_table)}"
        )
        left_order = (
            " ORDER BY " + ", ".join(quote_mysql_identifier(c) for c in request.left_order_by)
            if request.left_order_by else ""
        )
        left_window = (
            f"(SELECT {left_select} FROM {left_source}{left_order}"
            f" LIMIT {int(request.left_limit)})"
        )
        right_alias = "r"
        on_conditions = " AND ".join(
            f"w.{quote_mysql_identifier(lc)} = {right_alias}.{quote_mysql_identifier(rc)}"
            for lc, rc in zip(request.left_columns, request.right_columns, strict=True)
        )
        presence = f"{right_alias}.{quote_mysql_identifier(request.right_presence_column)}"

        # 左侧行数：有主键时 COUNT(DISTINCT 主键)=行数（主键唯一）；
        # 无主键时无法在单查询内精确区分重复行 → NULL，由服务层用窗口行数兜底。
        if request.left_order_by:
            if len(request.left_order_by) == 1:
                pk_expr = f"w.{quote_mysql_identifier(request.left_order_by[0])}"
            else:
                pk_expr = "CONCAT_WS(CHAR(31), " + ", ".join(
                    f"w.{quote_mysql_identifier(c)}" for c in request.left_order_by) + ")"
            left_rows_expr = f"COUNT(DISTINCT {pk_expr}) AS left_rows_seen"
        else:
            left_rows_expr = "NULL AS left_rows_seen"

        def _right_concat() -> str:
            if len(request.right_columns) == 1:
                return f"{right_alias}.{quote_mysql_identifier(request.right_columns[0])}"
            return "CONCAT_WS(CHAR(31), " + ", ".join(
                f"{right_alias}.{quote_mysql_identifier(c)}"
                for c in request.right_columns) + ")"

        select_parts = [
            left_rows_expr,
            f"SUM(CASE WHEN {presence} IS NULL THEN 1 ELSE 0 END) AS unmatched_left_rows",
            "COUNT(*) AS rows_after_left_join",
            (f"COUNT(DISTINCT CASE WHEN {presence} IS NOT NULL "
              f"THEN {_right_concat()} END) AS distinct_right_matched"),
        ]
        if request.string_join_columns:
            empty_expr = " OR ".join(
                f"w.{quote_mysql_identifier(c)} = ''" for c in request.string_join_columns
            )
            select_parts.append(
                f"SUM(CASE WHEN {presence} IS NOT NULL AND ({empty_expr}) THEN 1 ELSE 0 END)"
                " AS empty_key_matched_rows"
            )
        else:
            select_parts.append("0 AS empty_key_matched_rows")
        right_source = (
            f"{quote_mysql_identifier(request.right_schema)}"
            f".{quote_mysql_identifier(request.right_table)}"
        )
        sql = (
            f"SELECT /*+ MAX_EXECUTION_TIME({_timeout_ms(request.timeout_ms)}) */ "
            + ", ".join(select_parts)
            + f" FROM {left_window} AS w LEFT JOIN {right_source} AS {right_alias}"
            + f" ON {on_conditions}"
        )
        row = self._fetch_one(sql)
        left_rows = row.get("left_rows_seen")
        return JoinMetricsResult(
            left_rows_seen=int(left_rows) if left_rows is not None else None,
            unmatched_left_rows=int(row["unmatched_left_rows"] or 0),
            rows_after_left_join=int(row["rows_after_left_join"] or 0),
            distinct_right_matched=int(row["distinct_right_matched"] or 0),
            empty_key_matched_rows=int(row["empty_key_matched_rows"] or 0),
        )

    # -- 执行 -------------------------------------------------------------------

    def _fetch_one(self, sql: str) -> dict[str, Any]:
        """执行聚合查询并返回单行结果；错误统一脱敏。"""
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(sql))
                row = result.mappings().first()
                return dict(row) if row is not None else {}
        except Exception as exc:  # SQLAlchemyError/OSError 统一经连接器翻译脱敏
            if isinstance(exc, DataSourceConnectionError):
                raise
            raise self._connector._translate_error(exc) from None
