class SQLSafetyChecker:
    """SQL 安全边界。必须使用 AST/SQL parser，不能只做字符串包含判断。"""

    def validate(
        self, sql: str, allowed_tables: set[str], allowed_columns: set[str]
    ) -> None:
        # TODO(M6): 限制为 SELECT/受控 CTE，拒绝 DDL/DML、多语句和越权对象。
        # TODO(M6): 配合只读数据库账号、超时、并发和结果行数限制。
        raise NotImplementedError("TODO(M6): validate SQL")
