"""MySQL 数据源适配器（M1）。

只读策略由三层共同保证，缺一不可：
1. 数据库账号本身应只授予 SELECT 权限（部署要求，见 docs/configuration.md）；
2. 连接级约束：PyMySQL `init_command` 把会话内后续事务设为 READ ONLY；
3. 查询层约束：本类只执行系统构造的语句——元数据读取使用参数化查询，
   采样查询使用通过 `quote_mysql_identifier` 校验并加反引号的标识符。
   面向用户的任意 SQL 执行（execute_readonly）在 M6 实现前保持显式未实现。
"""

import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import DataSourceConnectionError, NotImplementedFeatureError
from app.domain.metadata import ColumnMetadata, IndexMetadata, TableMetadata
from app.ports.connectors import DataSourceConnector

_MAX_IDENTIFIER_LENGTH = 64
# 反引号是 MySQL 标识符的唯一转义符；NUL 与换行保守拒绝。
_IDENTIFIER_FORBIDDEN = re.compile(r"[\x00`\n\r]")
_DSN_PASSWORD_PATTERN = re.compile(r"(mysql\+pymysql://[^:/@\s]+:)[^@\s]+(@)")
_ERROR_CODE_PATTERN = re.compile(r"\((\d{4}),")

_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "lost connection",
    "maximum statement execution",
)
_UNREACHABLE_MARKERS = (
    "can't connect",
    "unknown mysql server host",
    "name or service not known",
    "connection refused",
    "no route to host",
)
_PRIVILEGE_ERROR_CODES = {1044, 1142, 1143, 1144, 1370}
_UNREACHABLE_ERROR_CODES = {2002, 2003, 2005, 2006}

# 系统库不参与扫描，即使账号可见也不返回。
_SYSTEM_SCHEMAS = frozenset({"information_schema", "performance_schema", "mysql", "sys"})


class MySQLConnectionConfig(BaseModel):
    """MySQL 连接配置；password 仅在创建引擎时短暂使用，禁止写入日志。"""

    host: str
    port: int = Field(gt=0, le=65535)
    database: str
    username: str
    password: SecretStr
    charset: str = "utf8mb4"
    connect_timeout_seconds: int = Field(default=10, gt=0)
    read_timeout_seconds: int = Field(default=30, gt=0)


def quote_mysql_identifier(identifier: str) -> str:
    """校验并给 MySQL 标识符加反引号。

    拒绝反引号/NUL/换行/超长标识符后加反引号，保证拼接进 SQL 时无法逃逸，
    这不是“前缀判断”，而是标识符白名单转义。
    """
    if (
        not identifier
        or len(identifier) > _MAX_IDENTIFIER_LENGTH
        or _IDENTIFIER_FORBIDDEN.search(identifier)
    ):
        raise ValueError("非法的 MySQL 标识符（为空、过长或包含反引号/控制字符）")
    return f"`{identifier}`"


def build_sample_query(
    schema_name: str,
    table_name: str,
    column_names: Sequence[str],
    order_by: Sequence[str],
    limit: int,
) -> str:
    """构造有界采样查询；limit 必须是正整数，标识符全部走转义。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("采样行数必须是正整数")
    if not column_names:
        raise ValueError("采样至少需要一个字段")
    columns = ", ".join(quote_mysql_identifier(column) for column in column_names)
    from_clause = f"{quote_mysql_identifier(schema_name)}.{quote_mysql_identifier(table_name)}"
    order_clause = (
        " ORDER BY " + ", ".join(quote_mysql_identifier(column) for column in order_by)
        if order_by
        else ""
    )
    return f"SELECT {columns} FROM {from_clause}{order_clause} LIMIT {limit}"


def map_inspector_to_table_metadata(
    source_id: str,
    schema_name: str,
    table_name: str,
    columns: list[dict[str, Any]],
    pk_constraint: dict[str, Any],
    foreign_keys: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
    table_rows: int | None,
    table_comment: str | None,
) -> TableMetadata:
    """把 SQLAlchemy Inspector 的原生结构映射为统一领域模型。"""
    mapped_columns = tuple(
        ColumnMetadata(
            name=column["name"],
            data_type=str(column.get("type", "")),
            nullable=bool(column.get("nullable", True)),
            comment=column.get("comment") or None,
            ordinal_position=column.get("ordinal_position"),
            default_value=str(column["default"]) if column.get("default") is not None else None,
        )
        for column in columns
    )
    primary_key = tuple(pk_constraint.get("constrained_columns") or ())
    mapped_foreign_keys = tuple(
        {
            "name": fk.get("name"),
            "columns": tuple(fk.get("constrained_columns") or ()),
            "ref_schema": fk.get("referred_schema") or schema_name,
            "ref_table": fk.get("referred_table") or "",
            "ref_columns": tuple(fk.get("referred_columns") or ()),
        }
        for fk in foreign_keys
    )
    mapped_indexes = tuple(
        IndexMetadata(
            name=index.get("name") or "",
            columns=tuple(col for col in (index.get("column_names") or []) if col is not None),
            unique=bool(index.get("unique", False)),
        )
        for index in indexes
    )
    # information_schema.TABLE_ROWS 对 InnoDB 是优化器估计值，必须显式标记。
    return TableMetadata(
        source_id=source_id,
        schema_name=schema_name,
        table_name=table_name,
        columns=mapped_columns,
        primary_key=primary_key,
        foreign_keys=mapped_foreign_keys,
        indexes=mapped_indexes,
        row_count_estimate=table_rows,
        row_count_is_estimate=True,
        table_comment=table_comment or None,
    )


class MySQLConnector(DataSourceConnector):
    """MySQL 适配器。业务层不得依赖该类的内部实现。

    statement_counter（可选）：经工厂参数注入后，引擎上挂 SQLAlchemy
    `before_cursor_execute` 事件逐语句计数（含 Inspector 内部语句与失败
    尝试）；不注入时行为与历史完全一致（扫描链路不受影响）。
    """

    def __init__(
        self,
        connection_config: MySQLConnectionConfig,
        engine_factory: Callable[[MySQLConnectionConfig], Engine] | None = None,
        statement_counter: Any = None,
    ) -> None:
        self._config = connection_config
        self._engine: Engine | None = None
        self._engine_factory = engine_factory or self._create_engine
        self._statement_counter = statement_counter
        self._counter_attached = False

    @staticmethod
    def _create_engine(config: MySQLConnectionConfig) -> Engine:
        url = URL.create(
            "mysql+pymysql",
            username=config.username,
            password=config.password.get_secret_value(),
            host=config.host,
            port=config.port,
            database=config.database,
        )
        return create_engine(
            url,
            connect_args={
                "charset": config.charset,
                "connect_timeout": config.connect_timeout_seconds,
                "read_timeout": config.read_timeout_seconds,
                # 连接级只读：会话内后续事务全部以 READ ONLY 打开（MySQL 5.6+）。
                "init_command": "SET SESSION TRANSACTION READ ONLY",
            },
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=3,
        )

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = self._engine_factory(self._config)
            if self._statement_counter is not None and not self._counter_attached:
                from sqlalchemy import event

                event.listen(
                    self._engine, "before_cursor_execute",
                    lambda *args, **kwargs: self._statement_counter.increment(),
                )
                self._counter_attached = True
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- 错误翻译 ---------------------------------------------------------

    def _translate_error(self, exc: Exception) -> DataSourceConnectionError:
        password = self._config.password.get_secret_value()
        message = str(exc)
        if password:
            message = message.replace(password, "***")
        message = _DSN_PASSWORD_PATTERN.sub(r"\1***\2", message)
        code_match = _ERROR_CODE_PATTERN.search(message)
        code = int(code_match.group(1)) if code_match else None
        orig = getattr(exc, "orig", None)
        orig_args = getattr(orig, "args", None)
        if orig_args and isinstance(orig_args, tuple) and isinstance(orig_args[0], int):
            code = orig_args[0]
        lowered = message.lower()

        if code == 1045 or ("access denied" in lowered and "using password" in lowered):
            reason = "auth_failure"
        elif code in _PRIVILEGE_ERROR_CODES or "command denied" in lowered:
            reason = "insufficient_privilege"
        elif code == 1049 or "unknown database" in lowered:
            reason = "unknown_database"
        elif any(marker in lowered for marker in _TIMEOUT_MARKERS):
            reason = "timeout"
        elif code in _UNREACHABLE_ERROR_CODES or any(
            marker in lowered for marker in _UNREACHABLE_MARKERS
        ):
            reason = "unreachable"
        else:
            reason = "unknown"
        return DataSourceConnectionError(reason, f"MySQL 连接或查询失败（{reason}）：{message}")

    def _run(self, operation: Callable[[Any], Any]) -> Any:
        try:
            with self.engine.connect() as connection:
                return operation(connection)
        except SQLAlchemyError as exc:
            raise self._translate_error(exc) from exc
        except (OSError, TimeoutError) as exc:
            raise self._translate_error(exc) from exc

    # -- DataSourceConnector 接口 ----------------------------------------

    def test_connection(self) -> bool:
        def _ping(connection: Any) -> None:
            connection.execute(text("SELECT 1"))

        self._run(_ping)
        return True

    def list_schemas(self) -> list[str]:
        def _list(connection: Any) -> list[str]:
            inspector = inspect(connection)
            return sorted(
                name
                for name in inspector.get_schema_names()
                if name.lower() not in _SYSTEM_SCHEMAS
            )

        return self._run(_list)

    def list_tables(self, schema_name: str) -> list[str]:
        def _list(connection: Any) -> list[str]:
            inspector = inspect(connection)
            return sorted(inspector.get_table_names(schema=schema_name))

        return self._run(_list)

    def get_table_metadata(self, schema_name: str, table_name: str) -> TableMetadata:
        def _load(connection: Any) -> TableMetadata:
            inspector = inspect(connection)
            columns = inspector.get_columns(table_name=table_name, schema=schema_name)
            pk = inspector.get_pk_constraint(table_name=table_name, schema=schema_name)
            foreign_keys = inspector.get_foreign_keys(table_name=table_name, schema=schema_name)
            indexes = inspector.get_indexes(table_name=table_name, schema=schema_name)
            stats = connection.execute(
                text(
                    "SELECT TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :schema_name AND TABLE_NAME = :table_name"
                ),
                {"schema_name": schema_name, "table_name": table_name},
            ).first()
            table_rows = int(stats[0]) if stats is not None and stats[0] is not None else None
            table_comment = stats[1] if stats is not None else None
            return map_inspector_to_table_metadata(
                source_id="",
                schema_name=schema_name,
                table_name=table_name,
                columns=columns,
                pk_constraint=pk,
                foreign_keys=foreign_keys,
                indexes=indexes,
                table_rows=table_rows,
                table_comment=table_comment,
            )

        return self._run(_load)

    def sample_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: Sequence[str],
        limit: int,
        order_by: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        sql = build_sample_query(schema_name, table_name, column_names, order_by, limit)

        def _sample(connection: Any) -> list[dict[str, Any]]:
            result = connection.execute(text(sql))
            return [dict(row) for row in result.mappings().all()]

        return self._run(_sample)

    def execute_readonly(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        # M1 不提供任意 SQL 执行入口；AST 安全检查与只读执行在 M6 实现。
        raise NotImplementedFeatureError(
            "TODO(M6): 任意 SQL 只读执行尚未实现；M1 仅执行系统构造的元数据与采样查询。"
        )
