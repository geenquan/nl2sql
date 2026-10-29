from typing import Any


class AppError(Exception):
    """可预期的应用异常基类。"""


class NotImplementedFeatureError(AppError):
    """功能尚未实现时抛出，避免返回伪造的成功结果。"""


class UnsafeOperationError(AppError):
    """请求违反只读或 SQL 安全策略时抛出。"""


class DataSourceConnectionError(AppError):
    """数据源连接/查询失败。message 必须已脱敏，不得包含凭证。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ResourceNotFoundError(AppError):
    """请求的资源不存在。"""

    def __init__(self, resource: str, message: str) -> None:
        super().__init__(message)
        self.resource = resource


class CredentialStoreError(AppError):
    """本地凭证文件损坏或不可读写；消息不含任何凭证内容。"""


class LLMRequestError(AppError):
    """模型调用失败。reason：auth_failure / rate_limited / server_error / timeout /
    network / invalid_request / invalid_response / output_truncated / config /
    budget_exceeded / deadline_exceeded。消息为固定安全文案，不含提示词、响应或密钥。
    attempts＝该次逻辑调用已消耗的 HTTP 尝试数（供上层预算扣减）；
    usage＝失败前最后一次已接收响应的用量（可能为 None＝未知，不虚构）。"""

    def __init__(self, reason: str, message: str, attempts: int = 0,
                 usage: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts
        self.usage = usage


class RevisionConflictError(AppError):
    """人工修订基于过期修订号提交（并发冲突）；不静默覆盖。"""


class ValidationStatementBudgetExceeded(AppError):
    """M3 验证 SQL 语句预算耗尽：该语句在发出前被拒绝（未到达数据库）。

    固定安全文案：不含 SQL 文本、参数、标识符或底层异常；不是数据库故障，
    适配器不得把它翻译为 DataSourceConnectionError（unknown）。"""

    def __init__(self, limit: int, issued: int) -> None:
        super().__init__(
            f"验证 SQL 语句预算耗尽：已发出 {issued}/{limit} 条，"
            "后续语句在发出前被拒绝（未执行）")


class ValidationDeadlineExceededError(AppError):
    """验证运行总期限耗尽：后续语句在发出前被拒绝（未到达数据库）。

    与 ValidationStatementBudgetExceeded 同级的安全闸：固定文案、无细节；
    区别在于这是整次运行的时间期限，不是语句条数额度。"""
