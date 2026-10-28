from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import LLMRequestError  # noqa: F401  （调用方捕获用）


@dataclass
class LLMUsage:
    """如实记录的调用用量；服务端未提供的数据为 None（unavailable），不编造。"""

    model_reported: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    calls: int = 0
    elapsed_ms: int = 0


@dataclass
class LLMStructuredOutput:
    result: dict[str, Any]
    usage: LLMUsage


class LLMClient(ABC):
    """大模型端口；业务模块不直接导入特定供应商 SDK。

    M2 接口调整：generate_structured 拆分 system/user 输入并返回
    LLMStructuredOutput（结构化结果 + 如实用量）；适配器负责超时、
    有限重试、响应大小限制与 JSON 解析。领域级校验由业务模块执行。
    """

    @abstractmethod
    def generate_structured(
        self,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        *,
        max_output_chars: int | None = None,
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
