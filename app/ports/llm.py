from abc import ABC, abstractmethod
from typing import Any


class LLMClient(ABC):
    """大模型端口；业务模块不直接导入特定供应商 SDK。"""

    @abstractmethod
    def generate_structured(
        self, prompt: str, output_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """返回符合结构的结果；适配器应负责超时、重试和响应校验。"""


class EmbeddingClient(ABC):
    """向量化端口，可按需接入本地或远程模型。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...
