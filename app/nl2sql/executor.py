from typing import Any


class ReadOnlyQueryExecutor:
    """执行器只负责安全检查后的只读查询，不负责生成 SQL。"""

    def execute(self, sql: str, parameters: dict[str, Any] | None = None) -> dict:
        # TODO(M6): 先调用 SQLSafetyChecker，再经 Connector 执行并记录审计摘要。
        raise NotImplementedError("TODO(M6): execute read-only query")
