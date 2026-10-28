from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from app.domain.metadata import TableMetadata


class DataSourceConnector(ABC):
    """数据源端口。所有数据库适配器都实现此接口，业务服务不依赖驱动细节。

    M1 接口调整：新增 `sample_table`，供画像模块以有界采样读取数据；
    只读约束由实现通过连接级设置与系统构造查询共同保证。
    """

    @abstractmethod
    def test_connection(self) -> bool:
        """验证连接；失败时抛出可处理的异常。"""

    @abstractmethod
    def list_schemas(self) -> list[str]:
        """列出当前账号有权访问的 Schema。"""

    @abstractmethod
    def list_tables(self, schema_name: str) -> list[str]:
        """列出指定 Schema 下的表。"""

    @abstractmethod
    def get_table_metadata(self, schema_name: str, table_name: str) -> TableMetadata:
        """返回数据库方言无关的表元数据。"""

    @abstractmethod
    def sample_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: Sequence[str],
        limit: int,
        order_by: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """按预算采样表数据；实现必须限制返回行数并安全处理标识符。"""

    @abstractmethod
    def execute_readonly(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """只读执行；实现必须接入 SQL 安全检查、超时和行数限制。"""

    def close(self) -> None:
        """释放连接池等资源；默认无操作，具体适配器按需覆盖。"""


# 组装根用该工厂按连接参数创建 Connector；服务层不感知具体适配器类。
# 参数键：dialect / host / port / database / username / password(SecretStr)，
# 可选 charset / connect_timeout_seconds / read_timeout_seconds。
ConnectorFactory = Callable[[dict[str, Any]], DataSourceConnector]
