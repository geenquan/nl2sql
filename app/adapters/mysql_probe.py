"""MySQL 关系证据探查适配器（M3，修复轮加固）。

只读与安全策略（与 mysql_connector.py 同一族，缺一不可）：
1. 复用 MySQLConnector 的连接级 READ ONLY（init_command）与账号 SELECT 约束；
2. 只执行由 WindowMetricsRequest / JoinMetricsRequest 构造的系统聚合查询：
   - 标识符全部来自快照元数据白名单，经 quote_mysql_identifier 校验转义；
   - LIMIT / 超时为受校验整数（不做字符串拼接插值）；
   - 查询不携带任何业务参数值，结果只返回聚合计数，不返回原始行；
3. 数据库级执行期限：SELECT 优化器提示 MAX_EXECUTION_TIME(ms)（MySQL ≥5.7.8
   顶层 SELECT 生效；MariaDB 不支持该提示，未知提示被忽略并回退为客户端
   read_timeout——**客户端超时是放弃读取，不等于服务端取消语句**，
   支持范围以此为准，不宣称所有版本都有服务端期限）；
4. 组合键去重使用**原生多列 COUNT(DISTINCT a, b)**，不用 CONCAT_WS 分隔符
   拼接（值包含分隔符 CHAR(31) 时拼接会碰撞，真实 MySQL 只读常量查询已
   复现：两不同元组拼接后同为 1 个值）；原生多列 DISTINCT 按元组比较并
   自动排除任一列为 NULL 的行，与窗口口径（全列非空）一致；
5. JOIN 匹配规模控制：默认右端为完整表（仅当右端键唯一且有可用前导索引，
   或无索引但行数估计在小量级余量内）；right_limit 给定时右端改为有界
   窗口派生表（双侧窗口 ≤ W×W 对，受控计数，不展开巨大多对多结果）；
6. 错误经 MySQLConnector._translate_error 脱敏（同适配器家族内部复用），
   不携带 SQL 文本、参数或业务值；
7. statement_counter（可选）：引擎挂 before_cursor_execute 逐语句计数，
   语句发出即计数（失败尝试同样计入）；一次方法调用 = 恰好一条 SQL。
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.adapters.mysql_connector import (
    MySQLConnectionConfig,
    MySQLConnector,
    attach_statement_guard,
    quote_mysql_identifier,
)
from app.core.exceptions import (
    DataSourceConnectionError,
    ValidationStatementBudgetExceeded,
)
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
    """基于 MySQLConnector 引擎的只读聚合探查（每方法恰好一条 SQL）。"""

    def __init__(
        self,
        connection_config: MySQLConnectionConfig,
        engine: Engine | None = None,
        statement_counter: Any = None,
    ) -> None:
        self._connector = MySQLConnector(
            connection_config, statement_counter=statement_counter)
        self._engine = engine
        # 外部传入的引擎同样挂语句守卫（与 Connector 共享同一预算对象）
        if engine is not None and statement_counter is not None:
            attach_statement_guard(engine, statement_counter)

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
    def _window_subquery(
        schema_name: str, table_name: str,
        columns: tuple[str, ...], order_by: tuple[str, ...], limit: int,
    ) -> str:
        column_list = ", ".join(quote_mysql_identifier(c) for c in columns)
        source = (
            f"{quote_mysql_identifier(schema_name)}"
            f".{quote_mysql_identifier(table_name)}"
        )
        order = (
            " ORDER BY " + ", ".join(quote_mysql_identifier(c) for c in order_by)
            if order_by else ""
        )
        return f"(SELECT {column_list} FROM {source}{order} LIMIT {int(limit)})"

    @staticmethod
    def _distinct_columns(args: list[str]) -> str:
        """原生多列 DISTINCT（非 CONCAT_WS 拼接，避免分隔符碰撞）。"""
        return "COUNT(DISTINCT " + ", ".join(args) + ")"

    def window_metrics(self, request: WindowMetricsRequest) -> WindowMetricsResult:
        quoted = [quote_mysql_identifier(c) for c in request.columns]
        non_null_expr = " AND ".join(f"{c} IS NOT NULL" for c in quoted)
        select_parts = ["COUNT(*) AS window_rows", f"SUM({non_null_expr}) AS non_null_rows"]
        if request.string_columns:
            empty_expr = " OR ".join(
                f"{quote_mysql_identifier(c)} = ''" for c in request.string_columns)
            select_parts.append(
                f"SUM(({non_null_expr}) AND ({empty_expr})) AS empty_string_rows")
        else:
            select_parts.append("0 AS empty_string_rows")
        # 原生多列 COUNT(DISTINCT)：任一列为 NULL 的行自动排除（=全列非空口径）
        select_parts.append(
            self._distinct_columns(quoted) + " AS distinct_values")
        sql = (
            f"SELECT /*+ MAX_EXECUTION_TIME({_timeout_ms(request.timeout_ms)}) */ "
            + ", ".join(select_parts)
            + " FROM " + self._window_subquery(
                request.schema_name, request.table_name, request.columns,
                request.order_by, request.limit) + " AS w"
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
        left_window = self._window_subquery(
            request.left_schema, request.left_table, tuple(select_columns),
            request.left_order_by, request.left_limit)
        # 右端：默认完整表（调用方成本门槛已判定安全）；right_limit 给定时
        # 改为有界窗口派生表（双侧 ≤W×W 对，匹配规模受控）。
        # 派生表必须同时携带 presence 列（外层匹配标记引用它），与连接列去重。
        if request.right_limit is not None:
            right_columns_with_presence = tuple(dict.fromkeys(
                list(request.right_columns) + [request.right_presence_column]))
            right_source = self._window_subquery(
                request.right_schema, request.right_table, right_columns_with_presence,
                request.right_order_by, request.right_limit)
        else:
            right_source = (
                f"{quote_mysql_identifier(request.right_schema)}"
                f".{quote_mysql_identifier(request.right_table)}"
            )
        right_alias = "r"
        on_conditions = " AND ".join(
            f"w.{quote_mysql_identifier(lc)} = {right_alias}.{quote_mysql_identifier(rc)}"
            for lc, rc in zip(request.left_columns, request.right_columns, strict=True)
        )
        presence = f"{right_alias}.{quote_mysql_identifier(request.right_presence_column)}"

        # 左侧行数：有主键时原生多列 COUNT(DISTINCT 主键)=行数（主键唯一非空）
        if request.left_order_by:
            left_pk_args = [
                f"w.{quote_mysql_identifier(c)}" for c in request.left_order_by]
            left_rows_expr = (
                self._distinct_columns(left_pk_args) + " AS left_rows_seen")
        else:
            left_rows_expr = "NULL AS left_rows_seen"

        # 右端去重匹配：逐列 CASE 包裹（presence 为 NULL → 全列 NULL → 被
        # 多列 DISTINCT 排除），不做分隔符拼接
        right_args = [
            f"CASE WHEN {presence} IS NOT NULL THEN "
            f"{right_alias}.{quote_mysql_identifier(c)} END"
            for c in request.right_columns
        ]
        select_parts = [
            left_rows_expr,
            f"SUM(CASE WHEN {presence} IS NULL THEN 1 ELSE 0 END) AS unmatched_left_rows",
            "COUNT(*) AS rows_after_left_join",
            self._distinct_columns(right_args) + " AS distinct_right_matched",
        ]
        if request.string_join_columns:
            empty_expr = " OR ".join(
                f"w.{quote_mysql_identifier(c)} = ''" for c in request.string_join_columns)
            select_parts.append(
                f"SUM(CASE WHEN {presence} IS NOT NULL AND ({empty_expr}) THEN 1 ELSE 0 END)"
                " AS empty_key_matched_rows")
        else:
            select_parts.append("0 AS empty_key_matched_rows")
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
        """执行聚合查询并返回单行结果；错误统一脱敏。

        预算拒绝（ValidationStatementBudgetExceeded）不是数据库故障：
        原样上抛给服务层按预算收尾，不翻译为 unknown 连接错误。"""
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(sql))
                row = result.mappings().first()
                return dict(row) if row is not None else {}
        except Exception as exc:
            if isinstance(exc, (DataSourceConnectionError,
                                ValidationStatementBudgetExceeded)):
                raise
            # SQLAlchemyError/OSError 统一经连接器翻译脱敏
            raise self._connector._translate_error(exc) from None
