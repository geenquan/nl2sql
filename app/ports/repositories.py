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


class RelationshipRepository(ABC):
    """M3 关系持久化端口。

    修订语义（沿用 M2 原则，关系侧补充 stale）：
    - 每次写入追加新修订；relationship_key 是跨快照稳定的逻辑身份，
      snapshot_id 标记本修订证据所基于的输入快照（新快照不覆盖旧证据链）；
    - effective 解析：人工结论不被机器修订覆盖；人工可撤销自己的结论；
      stale（人工或结构变化）必须改变有效结果；stale 之后的新机器修订
      （重验/重发现）重新接管有效结果；
    - expected_revision 原子乐观锁：版本比较与写入同事务（多进程安全），
      唯一约束 (relationship_key, revision) 为最终仲裁者。
    """

    @abstractmethod
    def save_relationship(
        self, record: dict[str, Any], expected_revision: int | None = None
    ) -> int:
        """追加关系新修订，返回修订号；expected_revision 过期抛冲突。"""

    @abstractmethod
    def save_relationship_batch(
        self, records: list[dict[str, Any]], run_record: dict[str, Any]
    ) -> None:
        """整次运行的关系修订 + 运行记录单事务写入：全成或全败，
        失败运行不产生部分有效结果（沿用 M2 候选写入—成功发布语义）。"""

    @abstractmethod
    def get_relationship_history(self, relationship_key: str) -> list[dict[str, Any]]:
        """按修订号倒序返回该逻辑关系的全部修订（跨快照）。"""

    @abstractmethod
    def get_effective_relationship(
        self, relationship_key: str, snapshot_id: str | None = None
    ) -> dict[str, Any] | None:
        """快照内有效修订：snapshot_id 给定时先限定快照再解析该链
        （不跨快照合并；None=跨快照按修订号整体解析，仅诊断用）。"""

    @abstractmethod
    def list_relationships(
        self,
        source_id: str,
        snapshot_id: str | None = None,
        status: str | None = None,
        effective_only: bool = True,
    ) -> list[dict[str, Any]]:
        """列出关系：先限定快照（如给定），再按 (relationship_key, snapshot_id)
        链内解析有效修订；snapshot_id=None 返回全部链（每键每快照一条）。"""

    @abstractmethod
    def save_relationship_run(self, run: dict[str, Any]) -> None:
        """按 run_id upsert 关系运行记录。"""

    @abstractmethod
    def get_relationship_run(self, run_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def list_relationship_runs(
        self, source_id: str, run_type: str | None = None
    ) -> list[dict[str, Any]]:
        """按创建时间倒序。"""
