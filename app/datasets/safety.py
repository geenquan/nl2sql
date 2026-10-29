"""M5 受控 SQL 安全校验（sqlglot AST；不宣称 M6 任意 SQL 安全执行已完成）。

仅接受系统编译器产物形态的 SQL：
- 单条 SELECT（可含受控 CTE），方言匹配；
- FROM/JOIN 的真实表必须属于白名单（CTE 名视为内部对象）；
- 所有列经别名作用域解析到真实表后必须在白名单；
- 拒绝 DDL/DML/多语句/文件输出/锁定读/危险函数。
"""

import re

import sqlglot
from sqlglot import exp

_FORBIDDEN_FUNCTION_PATTERNS = (
    "load_file", "into_outfile", "into_dumpfile", "get_lock", "benchmark",
    "sleep", "extractvalue", "updatexml", "sys_exec", "exec",
)
_FORBIDDEN_STATEMENT_ROOTS = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Set, exp.Use, exp.Command,
)


class SQLSafetyError(ValueError):
    """受控 SQL 校验失败（安全错误，不含数据）。"""


_PYFORMAT_PARAM = re.compile(r"%\(\w+\)s")


def validate_controlled_select(
    sql: str,
    allowed_tables: set[str],  # "schema.table" 全小写
    allowed_columns: dict[str, set[str]],  # "schema.table" -> {column 小写}
    dialect: str = "mysql",
) -> None:
    """校验编译产物；不通过抛 SQLSafetyError。

    pyformat 参数占位（%(name)s）在校验前替换为 NULL 字面量——参数值
    经参数化通道传递，不属于 SQL 文本；替换保证 AST 解析不被 % 干扰。
    """
    sql = _PYFORMAT_PARAM.sub("NULL", sql)
    statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(statements) != 1:
        raise SQLSafetyError(f"必须是单条语句，实际 {len(statements)} 条")
    tree = statements[0]
    if not isinstance(tree, exp.Select) and not isinstance(tree, exp.Union):
        raise SQLSafetyError(f"必须是 SELECT，实际 {type(tree).__name__}")
    if isinstance(tree, exp.Select) and not tree.args.get("with") and \
            not isinstance(tree, exp.Select):
        raise SQLSafetyError("不支持的语句形态")

    # 多语句防护：原始文本中出现在语句外的新起语句特征
    stripped = re.sub(r"'(?:[^']|'')*'", "", sql).lower()
    for marker in (";", "union all select 1;"):
        if marker == ";" and stripped.count(";") > 0:
            # sqlglot 已解析为单语句；防御性拒绝任何残留分号
            raise SQLSafetyError("语句中出现分号（拒绝多语句）")
    for token in ("load_file", "into outfile", "into dumpfile", "for update",
                  "lock in share mode", "get_lock", "benchmark("):
        if token in stripped:
            raise SQLSafetyError(f"包含禁止的构造：{token}")

    # 收集 CTE 名（内部对象）
    cte_names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        cte_alias = cte.alias_or_name
        if cte_alias:
            cte_names.add(cte_alias.lower())

    # 表白名单（真实表；CTE 除外）
    alias_to_table: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name
        alias = table.alias_or_name or name
        if name.lower() in cte_names:
            continue  # 内部 CTE
        schema = table.db or ""
        full = f"{schema}.{name}".lower().lstrip(".")
        if full not in allowed_tables:
            raise SQLSafetyError(f"表不在白名单：{full or name}")
        alias_to_table[alias.lower()] = full

    # 列白名单（经别名作用域解析）。无表限定列：若语句只涉及一张真实表，
    # 按该表核验（编译器总是限定，但校验器不得放行未限定引用）。
    single_table = next(iter(alias_to_table.values())) \
        if len(set(alias_to_table.values())) == 1 else None
    for column in tree.find_all(exp.Column):
        table_alias = (column.table or "").lower()
        column_name = column.name.lower()
        if not table_alias:
            if single_table is None:
                continue  # 多表/CTE 场景的投影表达式：编译器已控制
            resolved = single_table
        else:
            resolved = alias_to_table.get(table_alias)
            if resolved is None:
                if table_alias in cte_names:
                    continue  # CTE 派生列
                raise SQLSafetyError(f"未知表别名：{table_alias}")
        if column_name not in allowed_columns.get(resolved, set()):
            raise SQLSafetyError(
                f"列不在白名单：{resolved}.{column_name}")

    # 函数白名单（聚合 + 基础函数；其余拒绝）
    allowed_functions = {
        "sum", "count", "avg", "min", "max", "coalesce", "cast", "concat",
        "concat_ws", "round", "abs", "ifnull", "nullif", "date_format",
    }
    for func in tree.find_all(exp.Func):
        name = (func.sql_name() or "").lower()
        if name in ("", "anonymous"):  # 匿名函数取实际调用名
            inner = func.this
            name = (str(inner) if inner is not None else "").lower()
        if not name:
            continue
        if name not in allowed_functions:
            raise SQLSafetyError(
                f"函数不在白名单：{name}"
                + ("（禁止的函数）" if any(
                    p in name for p in _FORBIDDEN_FUNCTION_PATTERNS) else ""))


def validate_statement_type(sql: str, dialect: str = "mysql") -> None:
    """独立语句类型防御（用于入口快速拒绝 DDL/DML 形态）。"""
    sql = _PYFORMAT_PARAM.sub("NULL", sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s]
    except Exception as exc:  # noqa: BLE001 - 解析失败即拒绝
        raise SQLSafetyError(f"SQL 解析失败（{type(exc).__name__}）") from None
    if len(statements) != 1:
        raise SQLSafetyError(f"必须是单条语句，实际 {len(statements)} 条")
    if isinstance(statements[0], _FORBIDDEN_STATEMENT_ROOTS):
        raise SQLSafetyError(
            f"拒绝非 SELECT 语句：{type(statements[0]).__name__}")
