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


class ValidationQueryTimeoutError(AppError):
    """单查询执行超时（timeout_ms）：语句被中止或结果被拒绝。

    超时返回的结果不得用于质量判定或发布；固定安全文案，无 SQL/参数
    或底层异常细节。"""


class ValidationResultBudgetExceededError(AppError):
    """查询结果超出运行字节上限：已读取部分被整体丢弃。

    不返回部分超限结果（避免调用方消费超预算数据）；固定安全文案，
    不含任何业务值内容。"""


class ExternalOutcomeUnknownError(AppError):
    """外部目标操作结果不明：请求已发出，但响应丢失/超时/连接中断，
    或服务端 5xx——无法确定对象是否已创建。

    固定安全文案（不含响应正文/凭证/业务数据）。调用方不得把它当作
    "确定未发生副作用"释放占用——必须保留待核对（pending）。"""
