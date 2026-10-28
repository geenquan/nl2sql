from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import LLMRequestError  # noqa: F401  （调用方捕获用）


@dataclass
class LLMUsage:
    """如实记录的调用用量；服务端未提供的数据为 None（unavailable），不编造。

    区分三个计数：逻辑调用数（generate_structured 次数）、HTTP 尝试数
    （含传输层重试）、结构修复次数（由服务层统计）。model_reported 只填
    服务端返回的标识；配置的模型名放在 model_configured，二者不得混用。
    """

    model_reported: str | None = None
    model_configured: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    calls: int = 1
    http_attempts: int = 1
    elapsed_ms: int = 0


@dataclass
class LLMStructuredOutput:
    result: dict[str, Any]
    usage: LLMUsage


class LLMClient(ABC):
    """大模型端口；业务模块不直接导入特定供应商 SDK。

    M2 修复轮接口调整：generate_structured 增加 max_attempts（该次逻辑调用
    允许的总 HTTP 尝试上限，含传输重试；传输重试与上层修复共享同一预算，
    不允许嵌套放大）。失败抛出的 LLMRequestError 携带 attempts（已消耗的
    HTTP 尝试数），供上层继续扣减预算。
    """

    @abstractmethod
    def generate_structured(
        self,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        *,
        max_output_chars: int | None = None,
        max_attempts: int | None = None,
    ) -> LLMStructuredOutput:
        """返回解析后的 JSON 对象；失败抛 LLMRequestError（固定安全消息）。"""


class EmbeddingClient(ABC):
    """向量化端口，可按需接入本地或远程模型。

    M2 未接入任何实现（当前无 Embedding 配置）；语义检索使用本地词法基线，
    检索结果明确标记检索方式，不冒充向量相似度。
    """

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...
