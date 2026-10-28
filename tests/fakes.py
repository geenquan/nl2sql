"""共享测试替身：内存连接器。

统一从 `tests.fakes` 导入，保证 conftest 的类级开关复位作用于同一个类对象。
所有样本值均为虚构数据。
"""

from typing import Any

from app.core.exceptions import DataSourceConnectionError
from app.domain.metadata import ColumnMetadata, TableMetadata


class FakeConnector:
    """内存版连接器：模拟可见 Schema、表、采样与失败路径，不访问网络。"""

    fail_metadata_all = False  # 全部表结构读取失败（连接类错误）
    runtime_metadata_error = False  # get_table_metadata 抛未预期异常
    legacy_dropped = False  # other.legacy 被删除
    close_error = False
    broken_fails = True  # broken 表是否读取失败（构造“成功后部分失败”场景）

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        self.closed = False

    def test_connection(self) -> bool:
        if self.params["host"] == "down.internal":
            raise DataSourceConnectionError("unreachable", "MySQL 连接失败（unreachable）：模拟")
        return True

    def list_schemas(self) -> list[str]:
        return ["sales", "other", "empty"]

    def list_tables(self, schema_name: str) -> list[str]:
        if schema_name == "other" and self.legacy_dropped:
            return []
        return {
            "sales": ["orders", "customers", "broken"],
            "other": ["legacy"],
            "empty": [],
        }.get(schema_name, [])

    def get_table_metadata(self, schema_name: str, table_name: str) -> TableMetadata:
        if self.runtime_metadata_error:
            raise RuntimeError("模拟未预期异常: inspector 崩溃")
        if self.fail_metadata_all or (table_name == "broken" and self.broken_fails):
            raise DataSourceConnectionError(
                "insufficient_privilege", "MySQL 查询失败（insufficient_privilege）：拒绝访问"
            )
        return TableMetadata(
            source_id="",
            schema_name=schema_name,
            table_name=table_name,
            columns=(
                ColumnMetadata(name="id", data_type="bigint", nullable=False),
                ColumnMetadata(name="user_password", data_type="varchar(64)"),
                ColumnMetadata(name="contact", data_type="varchar(100)"),
                ColumnMetadata(name="remark", data_type="varchar(200)"),
            ),
            primary_key=("id",),
            row_count_estimate=2,
        )

    def sample_table(self, schema_name, table_name, column_names, limit, order_by=()):
        if self.fail_metadata_all or (table_name == "broken" and self.broken_fails):
            raise DataSourceConnectionError("insufficient_privilege", "拒绝访问")
        return [
            {
                "id": 1,
                "user_password": "raw-secret-1",
                "contact": "zhang_san@example.com",
                "remark": "13812345678",
            },
            {
                "id": 2,
                "user_password": "raw-secret-2",
                "contact": "li_si@example.com",
                "remark": "备用电话 13912345678",
            },
        ]

    def execute_readonly(self, sql, parameters=None):
        raise NotImplementedError

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("close 崩溃")
