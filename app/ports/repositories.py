from abc import ABC, abstractmethod
from typing import Any


class MetadataRepository(ABC):
    """元数据持久化端口；具体实现可以是 SQLite 或 PostgreSQL。

    M1 接口调整：在 M0 两个方法基础上补充扫描任务、快照查询、字段画像和
    表详情读取接口。快照语义（修复轮明确）：
    - 每次扫描生成 snapshot_id，表与画像归属快照，历史快照全部保留；
    - “默认发布快照”＝最近一次 succeeded（完整成功）扫描的快照；
      没有任何完整成功时，回退到最近一次 partial 扫描（tables_scanned>0），
      并标记 complete=False；failed/running 快照永不发布；
    - 完整成功扫描确认 Schema 为空时，发布空目录（已删除的表随之从目录消失）。
    """

    @abstractmethod
    def save_table_metadata(self, metadata: dict[str, Any]) -> str:
        """保存/更新一张表的元数据（按 snapshot+schema+table 去重），返回 table_id。"""

    @abstractmethod
    def list_table_metadata(
        self, source_id: str, snapshot_id: str | None = None
    ) -> list[dict[str, Any]]:
        """列出指定快照的表元数据；snapshot_id 为空时读取默认发布快照。"""

    @abstractmethod
    def get_table_metadata(self, table_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def save_column_profiles(self, table_id: str, profiles: list[dict[str, Any]]) -> None:
        """整表替换式保存字段画像（与该表元数据同一快照）。"""

    @abstractmethod
    def list_column_profiles(self, table_id: str) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def save_scan_job(self, job: dict[str, Any]) -> None:
        """按 job_id upsert 扫描任务记录。"""

    @abstractmethod
    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def list_scan_jobs(self, source_id: str) -> list[dict[str, Any]]:
        """按开始时间倒序列出该数据源的扫描任务。"""

    @abstractmethod
    def get_published_snapshot(self, source_id: str) -> dict[str, Any] | None:
        """返回默认发布快照信息 {snapshot_id, job_id, status, complete}；无则 None。"""

    # -- M2 语义持久化 ----------------------------------------------------------
    # 修订语义：每次写入追加新修订（不覆盖审计历史）；effective=True 时
    # business_confirmed 修订优先于更新的 suggested 修订（人工结论不被模型覆盖）。

    @abstractmethod
    def save_field_semantics(
        self, record: dict[str, Any], expected_revision: int | None = None
    ) -> int:
        """追加字段语义新修订，返回修订号；expected_revision 原子乐观锁
        （过期抛 RevisionConflictError，多进程安全）。"""

    @abstractmethod
    def list_field_semantics(
        self, table_id: str, effective: bool = False
    ) -> list[dict[str, Any]]:
        """列出一表的字段语义；effective=False 取每列最新修订，True 取人工确认优先。"""

    @abstractmethod
    def get_field_semantics_history(self, table_id: str, column_name: str) -> list[dict[str, Any]]:
        """按修订号倒序返回某字段的全部历史（含模型与人工修订）。"""

    @abstractmethod
    def save_table_purpose(
        self, record: dict[str, Any], expected_revision: int | None = None
    ) -> int:
        ...

    @abstractmethod
    def get_table_purpose(self, table_id: str, effective: bool = False) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def get_table_purpose_history(self, table_id: str) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def save_semantics_batch(
        self,
        field_records: list[dict[str, Any]],
        purpose_record: dict[str, Any] | None,
        run_record: dict[str, Any],
    ) -> None:
        """整次分析结果单事务写入：全成或全败，失败运行不产生部分有效结果。"""

    @abstractmethod
    def save_semantic_run(self, run: dict[str, Any]) -> None:
        """按 run_id upsert 语义分析运行记录。"""

    @abstractmethod
    def get_semantic_run(self, run_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def list_semantic_runs(self, source_id: str) -> list[dict[str, Any]]:
        """按开始时间倒序。"""

    @abstractmethod
    def list_field_semantics_by_source(
        self, source_id: str, snapshot_id: str, effective: bool = True
    ) -> list[dict[str, Any]]:
        """语义检索用：某数据源在某快照下的全部有效字段语义。"""


class SourceRepository(ABC):
    """数据源配置仓储。不得把明文密码作为普通字段保存。

    M1 接口调整：补充 list_sources / find_source_by_name 以支持默认数据源的
    幂等注册和列表查询。
    """

    @abstractmethod
    def create_source(self, source: dict[str, Any]) -> str:
        ...

    @abstractmethod
    def get_source(self, source_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def list_sources(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def find_source_by_name(self, name: str) -> dict[str, Any] | None:
        ...
