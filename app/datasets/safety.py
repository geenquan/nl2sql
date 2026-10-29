"""M5 受控 SQL 安全校验（sqlglot AST；不宣称 M6 任意 SQL 安全执行已完成）。

仅接受系统编译器产物形态的 SQL：
- 单条 SELECT（可含受控 CTE），方言匹配；
- FROM/JOIN 的真实表必须属于白名单（**带 Schema 的真实表不能因名字
  等于 CTE 名就跳过核验**——CTE 豁免仅适用于无 Schema 限定的引用）；
- 列校验使用 sqlglot **build_scope 逐作用域解析**（修复轮：不再用
  全查询共享 alias 表）：每个 SELECT/CTE 作用域内的列必须解析到该
  作用域可见的白名单真实表列或该作用域声明的 CTE/派生表输出列；
  未限定列在多表作用域中无法证明归属时拒绝；SELECT * 展开要求全部
  列在白名单；
- 拒绝 DDL/DML/多语句/文件输出/锁定读/危险函数。
"""

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

_FORBIDDEN_FUNCTION_PATTERNS = (
    "load_file", "into_outfile", "into_dumpfile", "get_lock", "benchmark",
    "sleep", "extractvalue", "updatexml", "sys_exec", "exec",
)
_FORBIDDEN_STATEMENT_ROOTS = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Set, exp.Use, exp.Command,
)

_ALLOWED_FUNCTIONS = frozenset({
    "sum", "count", "avg", "min", "max", "coalesce", "cast", "concat",
    "concat_ws", "round", "abs", "ifnull", "nullif", "date_format",
})


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
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect)
                      if s is not None]
    except Exception as exc:  # noqa: BLE001 - 解析失败即拒绝
        raise SQLSafetyError(f"SQL 解析失败（{type(exc).__name__}）") from None
    if len(statements) != 1:
        raise SQLSafetyError(f"必须是单条语句，实际 {len(statements)} 条")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLSafetyError(f"必须是 SELECT，实际 {type(tree).__name__}")

    stripped = re.sub(r"'(?:[^']|'')*'", "", sql).lower()
    if stripped.count(";") > 0:
        raise SQLSafetyError("语句中出现分号（拒绝多语句）")
    for token in ("load_file", "into outfile", "into dumpfile", "for update",
                  "lock in share mode", "get_lock", "benchmark("):
        if token in stripped:
            raise SQLSafetyError(f"包含禁止的构造：{token}")

    # 真实表白名单（带 Schema 的表不豁免于 CTE 同名）
    def _table_full_name(table: exp.Table) -> str:
        return f"{table.db or ''}.{table.name}".lower().lstrip(".")

    # CTE 名收集（无 Schema 限定的内部对象名）
    cte_names = {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE) if cte.alias_or_name}

    for table in tree.find_all(exp.Table):
        if table.db or table.name.lower() not in cte_names:  # 带 Schema 的引用：必须是白名单真实表（不豁免 CTE）
            full = _table_full_name(table)
            if full not in allowed_tables:
                raise SQLSafetyError(f"表不在白名单：{full}")

    # 作用域感知列校验（修复轮核心：逐 SELECT 作用域解析）
    root_scope = build_scope(tree)
    if root_scope is None:
        raise SQLSafetyError("无法构建查询作用域（拒绝）")
    for scope in root_scope.traverse():
        # 该作用域可见的真实表列与 CTE 输出列
        real_table_columns: dict[str, set[str]] = {}  # alias → 允许的列
        derived_columns: set[str] = set()  # CTE/派生表输出列（按别名追踪）
        derived_alias_to_output: dict[str, set[str]] = {}
        for name, source in scope.sources.items():
            if isinstance(source, exp.Table):
                full = _table_full_name(source)
                if full not in allowed_tables:
                    raise SQLSafetyError(f"表不在白名单：{full}")
                real_table_columns[name.lower()] = allowed_columns[full]
            else:
                # CTE/派生表引用（build_scope 中为 Scope 对象）或原生
                # Select/Union/Subquery：输出列由递归遍历覆盖核验，
                # 此处收集输出列名（投影别名或列名）用于本层解析
                output_names = set()
                inner_select = source
                if hasattr(source, "expression"):
                    inner_select = source.expression
                elif isinstance(source, exp.Subquery):
                    inner_select = source.this
                if isinstance(inner_select, (exp.Select, exp.Union)):
                    for select_expr in (inner_select.expressions or []):
                        alias = select_expr.alias_or_name
                        if alias:
                            output_names.add(alias.lower())
                        elif isinstance(select_expr, exp.Column):
                            output_names.add(select_expr.name.lower())
                        elif isinstance(select_expr, exp.Star):
                            raise SQLSafetyError(
                                "CTE/派生表投影含 *（拒绝未限定星号展开）")
                derived_alias_to_output[name.lower()] = output_names
                derived_columns.update(output_names)

        for column in scope.columns:
            column_name = column.name.lower() if column.name else ""
            if not column_name:
                continue
            table_ref = (column.table or "").lower()
            if table_ref:
                if table_ref in real_table_columns:
                    if column_name not in real_table_columns[table_ref]:
                        raise SQLSafetyError(
                            f"列不在白名单：{table_ref}.{column_name}")
                elif table_ref in derived_alias_to_output:
                    outputs = derived_alias_to_output[table_ref]
                    if outputs and column_name not in outputs:
                        raise SQLSafetyError(
                            f"CTE/派生表输出列不存在：{table_ref}.{column_name}")
                else:
                    raise SQLSafetyError(f"未知表别名：{table_ref}")
            else:
                # 未限定列：必须能唯一证明归属
                candidates = [
                    cols for alias, cols in real_table_columns.items()
                    if column_name in cols]
                in_derived = column_name in derived_columns
                if len(candidates) == 1:
                    continue  # 唯一归属
                if in_derived and not candidates:
                    continue  # 仅派生列
                if len(candidates) == 0 and not in_derived:
                    raise SQLSafetyError(
                        f"未限定列不在白名单且无法证明归属（拒绝）：{column_name}")
                if len(candidates) > 1:
                    raise SQLSafetyError(
                        f"未限定列在多表间歧义（拒绝）：{column_name}")

        # 星号展开：编译器不产生；防御性拒绝（scope.expression 为该层
        # SELECT 节点，其 projections 在 args["expressions"]）
        inner_select = scope.expression
        if isinstance(inner_select, exp.Select):
            for projection in inner_select.expressions or []:
                if isinstance(projection, exp.Star) or (
                        isinstance(projection, exp.Column)
                        and projection.name == "*"):
                    raise SQLSafetyError("拒绝 SELECT *（要求显式列）")

    # 函数白名单（聚合 + 基础函数；其余拒绝）
    # 修复轮 3：AND/OR/NOT/CASE/WHEN 等表达式节点不是函数调用——
    # sqlglot 中 ConnectivePrcondition/Condition 系列继承自 Expression
    # 而非 Func；但某些版本 CASE 也继承 Func——显式排除表达式节点
    _EXPRESSION_NODES = tuple(
        node for node in (
            getattr(exp, name, None) for name in (
                "And", "Or", "Not", "Case", "If", "Cast",
                "Between", "In", "Not", "Is", "EQ", "NEQ",
                "GT", "GTE", "LT", "LTE", "Paren", "Boolean",
                "LogicalAnd", "LogicalOr", "Case",
                "Exists", "Group", "Lambda",
            )
        ) if node is not None
    )
    for func in tree.find_all(exp.Func):
        if isinstance(func, _EXPRESSION_NODES):
            continue  # 表达式节点（AND/OR/CASE 等）不是函数调用
        name = (func.sql_name() or "").lower()
        if name in ("", "anonymous", "case"):
            inner = func.this
            name = (str(inner) if inner is not None else "").lower()
        if not name or name in ("and", "or", "not", "case", "when", "else",
                                 "then", "end", "if"):
            continue
        if name not in _ALLOWED_FUNCTIONS:
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
