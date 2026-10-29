"""M5 端口：受控查询执行器、视图发布器、ES 发布器。

边界：
- ControlledQueryExecutor 只接受**已通过 AST 校验的计划**（或由实现方
  在执行前强制校验），不开放 DataSourceConnector.execute_readonly 给
  任意字符串；
- ViewPublisher 在**独立显式配置的目标连接/Schema** 创建应用拥有的
  版本化视图（默认关闭；不覆盖用户对象；SELECT 执行器不接受 DDL）；
- ESPublisher 只管理应用命名空间索引/别名（默认关闭）。
"""

from abc import ABC, abstractmethod
from typing import Any


class ControlledQueryExecutor(ABC):
    """有预算的只读执行器（预览/质量/对账共用）。"""

    @abstractmethod
    def execute(
        self, sql: str, params: dict[str, Any], *,
        max_rows: int, timeout_ms: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """执行已校验的参数化 SELECT；返回 (行, 是否截断)。

        行数受 max_rows 限制（截断如实标记）；行内容为原始业务值，
        调用方负责脱敏展示与不落日志；错误必须为脱敏领域异常。
        """

    def close(self) -> None:
        """释放资源；默认无操作。"""


class ViewPublisher(ABC):
    """版本化视图发布器（目标连接与源库只读执行器分离）。"""

    @abstractmethod
    def create_versioned_view(
        self, target_schema: str, view_name: str, select_sql: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """创建应用拥有的版本化视图；已存在同名对象时抛冲突
        （不覆盖用户对象；类型化参数由方言编译进视图定义）。"""

    @abstractmethod
    def view_exists(self, target_schema: str, view_name: str) -> bool:
        """目标视图是否存在（回滚前核对版本对象仍在）。"""

    def close(self) -> None:
        """释放资源；默认无操作。"""


class ESPublisher(ABC):
    """ES 版本索引 + 别名切换发布器（应用命名空间）。"""

    @abstractmethod
    def ping(self) -> dict[str, Any]:
        """只读连通性/版本检查（真实远端验证仅用此操作）。"""

    @abstractmethod
    def create_version_index(
        self, index_name: str, mapping: dict[str, Any],
    ) -> None:
        """创建新版本索引（显式 mapping；已存在且 mapping 不一致抛错）。"""

    @abstractmethod
    def bulk_index(
        self, index_name: str, documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """批量写入；逐项检查 bulk 结果。

        返回 {indexed: n, retried: n, permanent_failures: [item 错误摘要]}；
        映射错误等永久失败直接抛错阻断发布；可重试项有限重试。
        """

    @abstractmethod
    def switch_alias(
        self, alias_name: str, index_name: str,
        previous_index: str | None = None,
    ) -> None:
        """全部验证通过后原子切换应用专属别名（保留旧版本索引）。"""

    @abstractmethod
    def count(self, index_name: str) -> int:
        """索引文档数（导出行数核对）。"""
