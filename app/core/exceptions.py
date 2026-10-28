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
    budget_exceeded。消息为固定安全文案，不含提示词、响应或密钥。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RevisionConflictError(AppError):
    """人工修订基于过期修订号提交（并发冲突）；不静默覆盖。"""
