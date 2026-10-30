"""M5 适配器：受控只读执行器（MySQL/SQLite）与视图发布器。

- 执行器：执行前**强制 AST 白名单校验**（不信任调用方）；连接级 READ ONLY
  （复用 MySQLConnector 策略）；错误经固定脱敏翻译；行数截断如实标记。
- 视图发布器（MySQL）：独立目标 DSN/Schema（配置默认关闭）；仅创建应用
  前缀 + 版本化名称的视图；已存在即冲突（不覆盖用户对象）；DDL 与
  SELECT 执行器是不同端口，SELECT 执行器不接受 DDL。
- 视图发布器（SQLite）：离线测试替身（同端口语义）。
"""

import time as _time
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.exceptions import (
    DataSourceConnectionError,
    ValidationDeadlineExceededError,
    ValidationQueryTimeoutError,
    ValidationResultBudgetExceededError,
)
from app.datasets.safety import (
    SQLSafetyError,
    validate_controlled_select,
    validate_statement_type,
)
from app.ports.datasets import ControlledQueryExecutor, ViewPublisher


def _mysql_timeout_hint(sql: str, effective_ms: int) -> str:
    """MySQL 服务端单查询期限（SELECT 优化器提示；WITH 语句不注入——
    提示仅对顶层 SELECT 生效，CTE 场景由客户端返回后检查兜底）。"""
    stripped = sql.lstrip()
    if stripped[:6].upper() == "SELECT":
        return f"SELECT /*+ MAX_EXECUTION_TIME({effective_ms}) */" \
            + stripped[6:]
    return sql


class ControlledExecutorBase(ControlledQueryExecutor):
    """共用执行逻辑：先校验后执行；参数化；截断标记。

    时间闸（恢复修复轮）：
    - 单查询有效期限＝min(timeout_ms, 运行剩余时间)；执行前、读取中、
      返回后三处检查——超时结果被拒绝（抛 ValidationQueryTimeoutError），
      不返回给调用方消费；
    - 运行总期限耗尽抛 ValidationDeadlineExceededError（发出前拒绝且
      不消耗语句额度；返回后发现的同样拒绝）；
    - 结果字节上限按"每行序列化(Decimal/date→文本)后 UTF-8 字节数"
      逐行累计，超限抛 ValidationResultBudgetExceededError（不返回
      部分超限批次）。
    预算/期限/超时/字节领域异常原样透传，不翻译为 query_failed。
    """

    def __init__(self, engine: Engine, dialect: str,
                 allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None,
                 deadline_monotonic: float | None = None,
                 max_result_bytes: int | None = None):
        self._engine = engine
        self._dialect = dialect
        self._allowed_tables = allowed_tables
        self._allowed_columns = allowed_columns
        self._statement_budget = statement_budget
        self._deadline = deadline_monotonic
        self._max_result_bytes = max_result_bytes
        # 可注入时钟（测试用确定性时间推进；默认单调时钟）
        self._monotonic = _time.monotonic
        # 当前语句的绝对期限（SQLite 进度处理器读取；见 _arm_progress）
        self._current_query_deadline: float | None = None

    # -- 时间闸 -----------------------------------------------------------------

    def _effective_timeout_ms(self, timeout_ms: int) -> int | None:
        """单查询有效期限：与运行剩余时间取小；运行已超期即拒绝。"""
        if timeout_ms and timeout_ms > 0:
            effective = timeout_ms
        else:
            effective = None  # 调用方未给单查询限制
        if self._deadline is not None:
            remaining_ms = int((self._deadline - self._monotonic()) * 1000)
            if remaining_ms <= 0:
                raise ValidationDeadlineExceededError(
                    "验证运行总期限耗尽：后续语句在发出前被拒绝（未执行）")
            effective = (min(effective, remaining_ms)
                         if effective is not None else remaining_ms)
        return effective

    def _check_time(self, per_query_deadline: float | None) -> None:
        """执行中/返回后的双重检查：先总期限后单查询期限。"""
        now = self._monotonic()
        if self._deadline is not None and now >= self._deadline:
            raise ValidationDeadlineExceededError(
                "验证运行总期限耗尽：查询结果被拒绝（不用于质量判定）")
        if per_query_deadline is not None and now >= per_query_deadline:
            raise ValidationQueryTimeoutError(
                "单查询执行超时（timeout_ms）：语句被中止或结果被拒绝")

    def _before_statement(self) -> None:
        """语句发出前的统一硬闸：先查运行总期限（超期抛
        ValidationDeadlineExceededError），再原子消耗预算（耗尽抛
        ValidationStatementBudgetExceeded，保留真实 issued/limit）。
        期限已耗尽时不再消耗预算——被拒绝的语句从未发出。"""
        if self._deadline is not None and \
                self._monotonic() >= self._deadline:
            raise ValidationDeadlineExceededError(
                "验证运行总期限耗尽：后续语句在发出前被拒绝（未执行）")
        if self._statement_budget is not None:
            self._statement_budget.allow_one()

    # -- 执行 -------------------------------------------------------------------

    def execute(self, sql, params, *, max_rows: int, timeout_ms: int) \
            -> tuple[list[dict[str, Any]], bool]:
        validate_statement_type(sql, dialect=self._dialect)
        validate_controlled_select(
            sql, self._allowed_tables, self._allowed_columns,
            dialect=self._dialect)
        effective_ms = self._effective_timeout_ms(timeout_ms)
        self._before_statement()
        per_query_deadline = (
            self._monotonic() + effective_ms / 1000.0
            if effective_ms is not None else None)
        fetch_cap = max_rows + 1  # 多取一行判断截断
        executable_sql = self._adapt_params(sql)
        if effective_ms is not None and self._dialect == "mysql":
            executable_sql = _mysql_timeout_hint(executable_sql,
                                                 effective_ms)
        rows: list[dict[str, Any]] = []
        byte_total = 0
        try:
            with self._engine.connect() as conn:
                self._arm_progress(conn, per_query_deadline)
                result = conn.execute(text(executable_sql), params or {})
                mappings = result.mappings()
                while len(rows) < fetch_cap:
                    self._check_time(per_query_deadline)
                    batch = mappings.fetchmany(
                        min(500, fetch_cap - len(rows)))
                    if not batch:
                        break
                    for raw in batch:
                        row = dict(raw)
                        if self._max_result_bytes is not None:
                            row_bytes = self._row_bytes(row)
                            if byte_total + row_bytes \
                                    > self._max_result_bytes:
                                raise ValidationResultBudgetExceededError(
                                    "查询结果超出运行字节上限："
                                    "已读取部分被整体丢弃（不返回超限结果）")
                            byte_total += row_bytes
                        rows.append(row)
                self._check_time(per_query_deadline)
        except Exception as exc:
            if isinstance(exc, (SQLSafetyError, DataSourceConnectionError,
                                ValidationDeadlineExceededError,
                                ValidationQueryTimeoutError,
                                ValidationResultBudgetExceededError)):
                raise
            # SQLite 进度中止等基础设施错误：若因期限触发则按期限归因
            if per_query_deadline is not None:
                now = self._monotonic()
                if self._deadline is not None and now >= self._deadline:
                    raise ValidationDeadlineExceededError(
                        "验证运行总期限耗尽：查询被中止") from None
                if now >= per_query_deadline:
                    raise ValidationQueryTimeoutError(
                        "单查询执行超时（timeout_ms）：语句被中止") from None
            raise DataSourceConnectionError(
                "query_failed",
                "数据集查询执行失败（query_failed）；错误详情已抑制"
            ) from None
        finally:
            self._current_query_deadline = None
        truncated = len(rows) > max_rows
        return (rows[:max_rows], truncated)

    def _arm_progress(self, conn: Any,
                      per_query_deadline: float | None) -> None:
        """方言相关的执行中中止钩子（默认无操作；SQLite 覆盖为进度处理）。"""
        del conn, per_query_deadline

    @staticmethod
    def _row_bytes(row: dict[str, Any]) -> int:
        """单行结果的 UTF-8 字节数（Decimal/date 按导出序列化规则转文本；
        不记录业务内容本身）。"""
        total = 0
        for value in row.values():
            if value is None:
                total += 4
            elif isinstance(value, Decimal):
                total += len(str(value).encode("utf-8"))
            elif isinstance(value, (bytes, bytearray, memoryview)):
                total += len(bytes(value))
            elif hasattr(value, "isoformat"):
                total += len(value.isoformat().encode("utf-8"))
            else:
                total += len(str(value).encode("utf-8"))
        return total

    @staticmethod
    def _rows_bytes(rows: list[dict[str, Any]]) -> int:
        """整批字节（兼容旧调用；逐行口径见 _row_bytes）。"""
        return sum(ControlledExecutorBase._row_bytes(row) for row in rows)

    def _adapt_params(self, sql: str) -> str:
        """编译产物统一使用 pyformat %(name)s 占位（修复轮：两种方言
        均转换为 SQLAlchemy :name 命名绑定——MySQL 方言下 text() 不会
        将 %(name)s 识别为绑定参数，会产生 %% 转义导致 compiled.params
        为空）。"""
        import re as _re

        return _re.sub(r"%\((\w+)\)s", r":\1", sql)

    def close(self) -> None:
        self._engine.dispose()


class SQLiteControlledExecutor(ControlledExecutorBase):
    """离线测试/演示执行器（SQLite 引擎 + 同一受控校验）。

    attach：schema 名 → 文件路径（SQLite ATTACH 每连接执行，经连接事件
    挂载；用于模拟 MySQL 的 schema 限定名与跨 Schema 同名表）。
    单查询期限经 sqlite3 进度处理器实现**执行中中止**（返回 1 中止语句；
    不用线程超时后遗留后台查询的方式）。
    """

    def __init__(self, database_url: str, allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None,
                 attach: dict[str, str] | None = None,
                 deadline_monotonic: float | None = None,
                 max_result_bytes: int | None = None):
        engine = create_engine(database_url)
        from sqlalchemy import event

        if attach:
            @event.listens_for(engine, "connect")
            def _attach_schemas(dbapi_conn, _record):
                for schema, path in attach.items():
                    dbapi_conn.execute(
                        f"ATTACH DATABASE '{path}' AS {schema}")

        @event.listens_for(engine, "connect")
        def _install_progress_guard(dbapi_conn, _record):
            dbapi_conn.set_progress_handler(self._progress_guard, 1000)

        super().__init__(engine, "sqlite", allowed_tables, allowed_columns,
                         statement_budget, deadline_monotonic,
                         max_result_bytes)

    def _progress_guard(self) -> int:
        """SQLite 进度回调：超过当前语句期限即中止（1=中断语句）。"""
        deadline = self._current_query_deadline
        if deadline is not None and self._monotonic() >= deadline:
            return 1
        return 0

    def _arm_progress(self, conn: Any,
                      per_query_deadline: float | None) -> None:
        # 进度处理器经连接事件常驻；此处发布当前语句期限供其读取
        del conn
        self._current_query_deadline = per_query_deadline


class MySQLControlledExecutor(ControlledExecutorBase):
    """源库 MySQL 只读执行器（连接级 READ ONLY 复用 MySQLConnector）。

    URL 经 SQLAlchemy URL.create 结构化构造（密码含 @:/ 等字符安全）；
    不在异常中暴露 DSN/密码/业务参数。
    """

    def __init__(self, host: str, port: int, database: str, username: str,
                 password: str, allowed_tables: set[str],
                 allowed_columns: dict[str, set[str]],
                 statement_budget: Any = None,
                 connect_timeout: int = 10, read_timeout: int = 30,
                 deadline_monotonic: float | None = None,
                 max_result_bytes: int | None = None):
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql", username=username, password=password,
            host=host, port=port, database=database)
        engine = create_engine(
            url,
            connect_args={
                "charset": "utf8mb4",
                "connect_timeout": connect_timeout,
                "read_timeout": read_timeout,
                "init_command": "SET SESSION TRANSACTION READ ONLY",
            },
            pool_pre_ping=True, pool_size=2, max_overflow=2)
        super().__init__(engine, "mysql", allowed_tables, allowed_columns,
                         statement_budget, deadline_monotonic,
                         max_result_bytes)


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
    def _inline_params(select_sql: str, params: dict[str, Any] | None,
                       dialect: str = "sqlite") -> str:
        """AST 单趟参数字面量化（修复轮：彻底移除逐参数字符串替换）。

        解析原始 SQL 为 AST，将参数占位符替换为类型化字面量节点后
        序列化——值中含 "%(pN)s" 形态也只是一段字符串数据，不会被
        后续替换扫描产生注入。拒绝 NaN/Infinity；完成后重新过
        语句类型校验。
        """
        if not params:
            return select_sql
        import math

        import sqlglot
        from sqlglot import exp as _exp

        def _literal_node(value: Any) -> _exp.Expression:
            if value is None:
                return _exp.null()
            if isinstance(value, bool):
                return _exp.true() if value else _exp.false()
            if isinstance(value, (int, float)):
                if isinstance(value, float) and (
                        math.isnan(value) or math.isinf(value)):
                    raise ValueError(
                        f"视图定义不支持 NaN/Infinity 数值：{value}")
                return _exp.Literal.number(str(value))
            if isinstance(value, str):
                return _exp.Literal.string(value)
            raise ValueError(
                f"视图定义不支持该参数类型：{type(value).__name__}")

        # 将 pyformat %(name)s 替换为 :name（sqlglot 解析为 Placeholder）
        import re as _re

        placeholder_sql = _re.sub(r"%\((\w+)\)s", r":\1", select_sql)
        try:
            tree = sqlglot.parse_one(placeholder_sql, read=dialect)
        except Exception as exc:  # noqa: BLE001 - 解析失败即拒绝
            raise ValueError(
                f"视图 SQL 解析失败（{type(exc).__name__}）") from None

        # 单趟：将所有命名占位符节点替换为字面量
        replaced: set[str] = set()
        for node in list(tree.find_all(_exp.Placeholder)):
            # Placeholder 的 this 为参数名标识符
            name = (node.name or node.this
                    if isinstance(node.this, str) else str(
                        node.this or ""))
            if not name:
                continue
            name = str(name)
            if name in params:
                node.replace(_literal_node(params[name]))
                replaced.add(name)

        missing = set(params) - replaced
        if missing:
            raise ValueError(
                f"视图定义存在未消费的参数占位符：{', '.join(sorted(missing))}")

        result = tree.sql(dialect=dialect)
        # 完成后重新过语句类型校验（防御性）
        from app.datasets.safety import validate_statement_type

        validate_statement_type(result, dialect=dialect)
        return result


class MySQLViewPublisher(ViewPublisher):
    """MySQL 目标视图发布器（独立目标连接；配置默认关闭）。

    仅接受编译产物与系统生成目标名；SELECT 执行端口不接受 DDL——
    DDL 只存在于本发布器。目标连接必须能读取来源表（首版同实例）。
    URL 结构化构造（密码特殊字符安全）。
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 connect_timeout: int = 10):
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql", username=username, password=password,
            host=host, port=int(port))
        self._engine = create_engine(
            url,
            connect_args={"charset": "utf8mb4",
                          "connect_timeout": connect_timeout},
            pool_pre_ping=True, pool_size=1, max_overflow=1)

    def create_versioned_view(
        self, target_schema: str, view_name: str, select_sql: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        literal_sql = SQLiteViewPublisher._inline_params(
            select_sql, params, dialect="mysql")
        # 最终 SELECT 定义重新过 AST 白名单（目标 Schema/视图名标识符校验）
        from app.adapters.mysql_connector import quote_mysql_identifier

        quote_mysql_identifier(target_schema)
        quote_mysql_identifier(view_name)
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
