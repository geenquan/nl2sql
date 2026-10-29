"""M5 适配器：受控只读执行器（MySQL/SQLite）与视图发布器。

- 执行器：执行前**强制 AST 白名单校验**（不信任调用方）；连接级 READ ONLY
  （复用 MySQLConnector 策略）；错误经固定脱敏翻译；行数截断如实标记。
- 视图发布器（MySQL）：独立目标 DSN/Schema（配置默认关闭）；仅创建应用
  前缀 + 版本化名称的视图；已存在即冲突（不覆盖用户对象）；DDL 与
  SELECT 执行器是不同端口，SELECT 执行器不接受 DDL。
- 视图发布器（SQLite）：离线测试替身（同端口语义）。
"""

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.exceptions import DataSourceConnectionError
from app.datasets.safety import (
    SQLSafetyError,
    validate_controlled_select,
    validate_statement_type,
)
from app.ports.datasets import ControlledQueryExecutor, ViewPublisher


class ControlledExecutorBase(ControlledQueryExecutor):
    """共用执行逻辑：先校验后执行；参数化；截断标记。"""

    def __init__(self, engine: Engine, dialect: str,
                 allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None):
        self._engine = engine
        self._dialect = dialect
        self._allowed_tables = allowed_tables
        self._allowed_columns = allowed_columns
        self._statement_budget = statement_budget  # 可选：逐语句硬预算

    def _before_statement(self) -> None:
        if self._statement_budget is not None:
            self._statement_budget.allow_one()

    def execute(self, sql, params, *, max_rows: int, timeout_ms: int) \
            -> tuple[list[dict[str, Any]], bool]:
        del timeout_ms  # 期限由调用方预算/连接超时控制；此处不额外实现
        validate_statement_type(sql, dialect=self._dialect)
        validate_controlled_select(
            sql, self._allowed_tables, self._allowed_columns,
            dialect=self._dialect)
        fetch_cap = max_rows + 1  # 多取一行判断截断
        self._before_statement()
        executable_sql = self._adapt_params(sql)
        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(executable_sql), params or {})
                rows = [dict(row) for row in
                        result.mappings().fetchmany(fetch_cap)]
        except Exception as exc:
            if isinstance(exc, (SQLSafetyError, DataSourceConnectionError)):
                raise
            raise DataSourceConnectionError(
                "query_failed",
                "数据集查询执行失败（query_failed）；错误详情已抑制"
            ) from None
        truncated = len(rows) > max_rows
        return (rows[:max_rows], truncated)

    def _adapt_params(self, sql: str) -> str:
        """编译产物统一使用 pyformat %(name)s 占位；SQLite 方言转 :name。"""
        if self._dialect == "sqlite":
            import re as _re

            return _re.sub(r"%\((\w+)\)s", r":\1", sql)
        return sql

    def close(self) -> None:
        self._engine.dispose()


class SQLiteControlledExecutor(ControlledExecutorBase):
    """离线测试/演示执行器（SQLite 引擎 + 同一受控校验）。

    attach：schema 名 → 文件路径（SQLite ATTACH 每连接执行，经连接事件
    挂载；用于模拟 MySQL 的 schema 限定名与跨 Schema 同名表）。
    """

    def __init__(self, database_url: str, allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None,
                 attach: dict[str, str] | None = None):
        engine = create_engine(database_url)
        if attach:
            from sqlalchemy import event

            @event.listens_for(engine, "connect")
            def _attach_schemas(dbapi_conn, _record):
                for schema, path in attach.items():
                    dbapi_conn.execute(
                        f"ATTACH DATABASE '{path}' AS {schema}")
        super().__init__(engine, "sqlite", allowed_tables, allowed_columns,
                         statement_budget)


class MySQLControlledExecutor(ControlledExecutorBase):
    """源库 MySQL 只读执行器（连接级 READ ONLY 复用 MySQLConnector）。"""

    def __init__(self, host: str, port: int, database: str, username: str,
                 password: str, allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None,
                 connect_timeout: int = 10, read_timeout: int = 30):
        engine = create_engine(
            f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}",
            connect_args={
                "charset": "utf8mb4",
                "connect_timeout": connect_timeout,
                "read_timeout": read_timeout,
                "init_command": "SET SESSION TRANSACTION READ ONLY",
            },
            pool_pre_ping=True, pool_size=2, max_overflow=2)
        super().__init__(engine, "mysql", allowed_tables, allowed_columns,
                         statement_budget)


class SQLiteViewPublisher(ViewPublisher):
    """离线测试视图发布器（SQLite CREATE VIEW，同端口语义）。

    attach：schema → 文件路径（与受控执行器同策略，经连接事件挂载）。
    """

    def __init__(self, database_url: str,
                 attach: dict[str, str] | None = None):
        self._engine = create_engine(database_url)
        if attach:
            from sqlalchemy import event

            @event.listens_for(self._engine, "connect")
            def _attach_schemas(dbapi_conn, _record):
                for schema, path in attach.items():
                    dbapi_conn.execute(
                        f"ATTACH DATABASE '{path}' AS {schema}")

    def create_versioned_view(
        self, target_schema: str, view_name: str, select_sql: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        # SQLite 限制：视图必须与被引用对象同库——attach 模式下视图建在
        # 业务库文件中（target_schema 由视图名前缀表达，应用所有权不变）
        literal_sql = self._inline_params(select_sql, params)
        schema_prefix = f"{target_schema}." if target_schema else ""
        try:
            with self._engine.begin() as conn:
                conn.execute(text(
                    f'CREATE VIEW {schema_prefix}"{view_name}" AS {literal_sql}'))
        except Exception as exc:  # noqa: BLE001
            if "already exists" in str(exc).lower():
                raise ValueError(
                    f"目标视图已存在（不覆盖用户对象）：{view_name}") from None
            raise DataSourceConnectionError(
                "view_publish_failed",
                "视图创建失败（view_publish_failed）；错误详情已抑制"
            ) from None

    def view_exists(self, target_schema: str, view_name: str) -> bool:
        # attach 模式下视图在业务库文件中：查目标 schema 的 sqlite_master
        schema = target_schema or "main"
        with self._engine.connect() as conn:
            row = conn.execute(text(
                f'SELECT name FROM "{schema}".sqlite_master '
                "WHERE type='view' AND name=:n"), {"n": view_name}).first()
        return row is not None

    def close(self) -> None:
        self._engine.dispose()

    @staticmethod
    def _inline_params(select_sql: str, params: dict[str, Any] | None) -> str:
        """类型化字面量编译（数值/字符串/None；字符串经 SQLite 转义）。"""
        if not params:
            return select_sql
        sql = select_sql

        def escape(value: Any) -> str:
            if value is None:
                return "NULL"
            if isinstance(value, bool):
                return "1" if value else "0"
            if isinstance(value, (int, float)):
                return repr(value)
            if isinstance(value, str):
                return "'" + value.replace("'", "''") + "'"
            raise ValueError(f"视图定义不支持该参数类型：{type(value).__name__}")

        for name, value in params.items():
            sql = sql.replace(f"%({name})s", escape(value))
        if "%(" in sql:
            raise ValueError("视图定义存在未消费的参数占位符")
        return sql


class MySQLViewPublisher(ViewPublisher):
    """MySQL 目标视图发布器（独立目标连接；配置默认关闭）。

    仅接受编译产物与系统生成目标名；SELECT 执行端口不接受 DDL——
    DDL 只存在于本发布器。目标连接必须能读取来源表（首版同实例）。
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 connect_timeout: int = 10):
        self._engine = create_engine(
            f"mysql+pymysql://{username}:{password}@{host}:{port}",
            connect_args={"charset": "utf8mb4",
                          "connect_timeout": connect_timeout},
            pool_pre_ping=True, pool_size=1, max_overflow=1)

    def create_versioned_view(
        self, target_schema: str, view_name: str, select_sql: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        literal_sql = SQLiteViewPublisher._inline_params(select_sql, params)
        try:
            with self._engine.begin() as conn:
                conn.execute(text(
                    f"CREATE VIEW `{target_schema}`.`{view_name}` AS "
                    f"{literal_sql}"))
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "already exists" in message or "1050" in message:
                raise ValueError(
                    f"目标视图已存在（不覆盖用户对象）："
                    f"{target_schema}.{view_name}") from None
            raise DataSourceConnectionError(
                "view_publish_failed",
                "目标视图创建失败（view_publish_failed）；错误详情已抑制"
            ) from None

    def view_exists(self, target_schema: str, view_name: str) -> bool:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT TABLE_NAME FROM information_schema.VIEWS "
                    "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :v"),
                    {"s": target_schema, "v": view_name}).first()
            return row is not None
        except Exception:  # noqa: BLE001
            raise DataSourceConnectionError(
                "view_publish_failed",
                "目标视图核对失败（view_publish_failed）；错误详情已抑制"
            ) from None

    def close(self) -> None:
        self._engine.dispose()
