"""本地元数据仓储（SQLite，M1）。

存储位置由 `METADATA_DATABASE_URL` 控制，默认 `sqlite:///./metadata.db`
（相对进程工作目录）。部署环境可切 PostgreSQL：实现同一 port，表结构语义不变。

快照与发布规则（修复轮明确）：
- 每次扫描生成新的 snapshot_id，表元数据与字段画像都归属各自快照，历史全部保留；
- 同一快照内按 (snapshot_id, schema_name, table_name) upsert 去重；
- 默认发布快照＝最近一次 succeeded（完整成功）扫描；无完整成功时回退最近一次
  partial（tables_scanned>0）并标记 complete=False；failed/running 永不发布；
- 完整成功扫描确认 Schema 为空时发布空目录（已删除的表随之从目录消失）；
- 因失败、权限或预算截断而缺失的表不会被当作确认删除，只有下一次完整成功
  快照才决定目录内容；
- 画像按 table_id 整表替换（同一快照内重复计算不会产生重复行）。
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    delete,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.ports.repositories import MetadataRepository, SourceRepository

_JSON_COLUMNS = ("columns", "primary_key", "foreign_keys", "indexes", "allowed_schemas", "errors")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    return json.loads(raw)


class SQLiteMetadataRepository(MetadataRepository, SourceRepository):
    """实现 MetadataRepository 与 SourceRepository 两个端口。"""

    def __init__(self, database_url: str, engine: Engine | None = None) -> None:
        self._database_url = database_url
        self._engine = engine or create_engine(
            database_url, connect_args={"check_same_thread": False}
        )
        self._metadata = MetaData()
        self._sources = Table(
            "sources",
            self._metadata,
            Column("id", String(36), primary_key=True),
            Column("name", String(100), nullable=False, unique=True),
            Column("dialect", String(32), nullable=False),
            Column("host", String(255)),
            Column("port", String(10)),
            Column("database", String(128)),
            Column("username", String(128)),
            Column("credential_ref", String(128)),
            Column("allowed_schemas", String(2000)),
            Column("read_only", String(1)),
            Column("is_default", String(1)),
            Column("created_at", String(40)),
            Column("updated_at", String(40)),
        )
        self._scan_jobs = Table(
            "scan_jobs",
            self._metadata,
            Column("id", String(36), primary_key=True),
            Column("source_id", String(36), nullable=False),
            Column("snapshot_id", String(36)),
            Column("status", String(16), nullable=False),
            Column("started_at", String(40)),
            Column("finished_at", String(40)),
            Column("tables_planned", String(10)),
            Column("tables_scanned", String(10)),
            Column("tables_failed", String(10)),
            Column("tables_skipped", String(10)),
            Column("sample_rows", String(10)),
            Column("max_tables", String(10)),
            Column("errors", String(10000)),
        )
        self._table_metadata = Table(
            "table_metadata",
            self._metadata,
            Column("id", String(36), primary_key=True),
            Column("source_id", String(36), nullable=False),
            Column("snapshot_id", String(36), nullable=False),
            Column("schema_name", String(128), nullable=False),
            Column("table_name", String(128), nullable=False),
            Column("table_comment", String(2000)),
            Column("columns", String(200000)),
            Column("primary_key", String(2000)),
            Column("foreign_keys", String(20000)),
            Column("indexes", String(20000)),
            Column("row_count_estimate", String(20)),
            Column("row_count_is_estimate", String(1)),
            Column("scanned_at", String(40)),
            UniqueConstraint(
                "snapshot_id", "schema_name", "table_name", name="uq_snapshot_table"
            ),
        )
        self._column_profiles = Table(
            "column_profiles",
            self._metadata,
            Column("seq", Integer, primary_key=True, autoincrement=True),
            Column("id", String(36), nullable=False, unique=True),
            Column("table_metadata_id", String(36), nullable=False),
            Column("source_id", String(36), nullable=False),
            Column("snapshot_id", String(36), nullable=False),
            Column("schema_name", String(128), nullable=False),
            Column("table_name", String(128), nullable=False),
            Column("column_name", String(128), nullable=False),
            Column("payload", String(50000)),
            Column("computed_at", String(40)),
        )
        # 开发级建表：不存在则创建；正式迁移方案在 M8 引入。
        self._metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    # -- SourceRepository -------------------------------------------------

    def create_source(self, source: dict[str, Any]) -> str:
        from uuid import uuid4

        now = _now()
        record = {
            "id": source.get("id") or uuid4().hex[:36],
            "name": source["name"],
            "dialect": source["dialect"],
            "host": source.get("host"),
            "port": str(source["port"]) if source.get("port") is not None else None,
            "database": source.get("database"),
            "username": source.get("username"),
            "credential_ref": source.get("credential_ref"),
            "allowed_schemas": _dump(source.get("allowed_schemas") or []),
            "read_only": "1" if source.get("read_only", True) else "0",
            "is_default": "1" if source.get("is_default") else "0",
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self._engine.begin() as conn:
                conn.execute(self._sources.insert().values(record))
        except IntegrityError as exc:
            raise ValueError(f"数据源名称已存在：{source['name']}") from exc
        return record["id"]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._sources).where(self._sources.c.id == source_id)
            ).first()
        return self._source_row_to_dict(row) if row else None

    def list_sources(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(self._sources).order_by(self._sources.c.created_at)
            ).fetchall()
        return [self._source_row_to_dict(row) for row in rows]

    def find_source_by_name(self, name: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._sources).where(self._sources.c.name == name)
            ).first()
        return self._source_row_to_dict(row) if row else None

    @staticmethod
    def _source_row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "source_id": row.id,
            "name": row.name,
            "dialect": row.dialect,
            "host": row.host,
            "port": int(row.port) if row.port else None,
            "database": row.database,
            "username": row.username,
            "credential_ref": row.credential_ref,
            "allowed_schemas": _load(row.allowed_schemas, []),
            "read_only": row.read_only == "1",
            "is_default": row.is_default == "1",
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # -- 扫描任务 -----------------------------------------------------------

    def save_scan_job(self, job: dict[str, Any]) -> None:
        record = {
            "id": job["job_id"],
            "source_id": job["source_id"],
            "snapshot_id": job.get("snapshot_id"),
            "status": job["status"],
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "tables_planned": str(job["tables_planned"]) if job.get("tables_planned") is not None else None,
            "tables_scanned": str(job["tables_scanned"]) if job.get("tables_scanned") is not None else None,
            "tables_failed": str(job["tables_failed"]) if job.get("tables_failed") is not None else None,
            "tables_skipped": str(job["tables_skipped"]) if job.get("tables_skipped") is not None else None,
            "sample_rows": str(job["sample_rows"]) if job.get("sample_rows") is not None else None,
            "max_tables": str(job["max_tables"]) if job.get("max_tables") is not None else None,
            "errors": _dump(job.get("errors") or []),
        }
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with self._engine.begin() as conn:
            conn.execute(
                sqlite_insert(self._scan_jobs)
                .values(record)
                .on_conflict_do_update(
                    index_elements=[self._scan_jobs.c.id],
                    set_={k: v for k, v in record.items() if k != "id"},
                )
            )

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._scan_jobs).where(self._scan_jobs.c.id == job_id)
            ).first()
        return self._job_row_to_dict(row) if row else None

    def list_scan_jobs(self, source_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(self._scan_jobs)
                .where(self._scan_jobs.c.source_id == source_id)
                .order_by(self._scan_jobs.c.started_at.desc())
            ).fetchall()
        return [self._job_row_to_dict(row) for row in rows]

    def get_published_snapshot(self, source_id: str) -> dict[str, Any] | None:
        """默认发布快照：最近完整成功优先；否则回退最近部分成功（complete=False）。

        failed/running 永不发布；完整成功确认空 Schema 时发布空目录。
        """
        from sqlalchemy import and_

        with self._engine.connect() as conn:
            succeeded = conn.execute(
                select(self._scan_jobs)
                .where(
                    and_(
                        self._scan_jobs.c.source_id == source_id,
                        self._scan_jobs.c.status == "succeeded",
                    )
                )
                .order_by(self._scan_jobs.c.started_at.desc())
                .limit(1)
            ).first()
            if succeeded is not None:
                return {
                    "job_id": succeeded.id,
                    "snapshot_id": succeeded.snapshot_id,
                    "status": "succeeded",
                    "complete": True,
                }
            partial = conn.execute(
                select(self._scan_jobs)
                .where(
                    and_(
                        self._scan_jobs.c.source_id == source_id,
                        self._scan_jobs.c.status == "partial",
                        self._scan_jobs.c.tables_scanned.cast(Integer) > 0,
                    )
                )
                .order_by(self._scan_jobs.c.started_at.desc())
                .limit(1)
            ).first()
            if partial is not None:
                return {
                    "job_id": partial.id,
                    "snapshot_id": partial.snapshot_id,
                    "status": "partial",
                    "complete": False,
                }
        return None

    @staticmethod
    def _job_row_to_dict(row: Any) -> dict[str, Any]:
        def _int(value: str | None) -> int | None:
            return int(value) if value is not None else None

        return {
            "job_id": row.id,
            "source_id": row.source_id,
            "snapshot_id": row.snapshot_id,
            "status": row.status,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "tables_planned": _int(row.tables_planned),
            "tables_scanned": _int(row.tables_scanned),
            "tables_failed": _int(row.tables_failed),
            "tables_skipped": _int(row.tables_skipped),
            "sample_rows": _int(row.sample_rows),
            "max_tables": _int(row.max_tables),
            "errors": _load(row.errors, []),
        }

    # -- 表元数据与画像 ------------------------------------------------------

    def save_table_metadata(self, metadata: dict[str, Any]) -> str:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        record = {
            "id": metadata.get("table_id") or _new_id(),
            "source_id": metadata["source_id"],
            "snapshot_id": metadata["snapshot_id"],
            "schema_name": metadata["schema_name"],
            "table_name": metadata["table_name"],
            "table_comment": metadata.get("table_comment"),
            "columns": _dump(metadata.get("columns") or []),
            "primary_key": _dump(metadata.get("primary_key") or []),
            "foreign_keys": _dump(metadata.get("foreign_keys") or []),
            "indexes": _dump(metadata.get("indexes") or []),
            "row_count_estimate": str(metadata["row_count_estimate"])
            if metadata.get("row_count_estimate") is not None
            else None,
            "row_count_is_estimate": "1" if metadata.get("row_count_is_estimate", True) else "0",
            "scanned_at": metadata.get("scanned_at"),
        }
        with self._engine.begin() as conn:
            conn.execute(
                sqlite_insert(self._table_metadata)
                .values(record)
                .on_conflict_do_update(
                    index_elements=[
                        self._table_metadata.c.snapshot_id,
                        self._table_metadata.c.schema_name,
                        self._table_metadata.c.table_name,
                    ],
                    set_={k: v for k, v in record.items() if k != "id"},
                )
            )
            # 冲突时保留既有行的 id，保证 table_id 在同一快照内稳定。
            row = conn.execute(
                select(self._table_metadata.c.id).where(
                    self._table_metadata.c.snapshot_id == record["snapshot_id"],
                    self._table_metadata.c.schema_name == record["schema_name"],
                    self._table_metadata.c.table_name == record["table_name"],
                )
            ).first()
        return row.id

    def list_table_metadata(
        self, source_id: str, snapshot_id: str | None = None
    ) -> list[dict[str, Any]]:
        if snapshot_id is None:
            published = self.get_published_snapshot(source_id)
            if published is None:
                return []
            snapshot_id = published["snapshot_id"]
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(self._table_metadata)
                .where(
                    self._table_metadata.c.source_id == source_id,
                    self._table_metadata.c.snapshot_id == snapshot_id,
                )
                .order_by(self._table_metadata.c.schema_name, self._table_metadata.c.table_name)
            ).fetchall()
        return [self._table_row_to_dict(row) for row in rows]

    def get_table_metadata(self, table_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._table_metadata).where(self._table_metadata.c.id == table_id)
            ).first()
        return self._table_row_to_dict(row) if row else None

    @staticmethod
    def _table_row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "table_id": row.id,
            "source_id": row.source_id,
            "snapshot_id": row.snapshot_id,
            "schema_name": row.schema_name,
            "table_name": row.table_name,
            "table_comment": row.table_comment,
            "columns": _load(row.columns, []),
            "primary_key": _load(row.primary_key, []),
            "foreign_keys": _load(row.foreign_keys, []),
            "indexes": _load(row.indexes, []),
            "row_count_estimate": int(row.row_count_estimate)
            if row.row_count_estimate is not None
            else None,
            "row_count_is_estimate": row.row_count_is_estimate == "1",
            "scanned_at": row.scanned_at,
        }

    def save_column_profiles(self, table_id: str, profiles: list[dict[str, Any]]) -> None:
        table = self.get_table_metadata(table_id)
        if table is None:
            raise ValueError(f"表元数据不存在：{table_id}")
        with self._engine.begin() as conn:
            conn.execute(
                delete(self._column_profiles).where(
                    self._column_profiles.c.table_metadata_id == table_id
                )
            )
            for profile in profiles:
                payload = dict(profile)
                payload.pop("table_id", None)
                conn.execute(
                    self._column_profiles.insert().values(
                        id=_new_id(),
                        table_metadata_id=table_id,
                        source_id=table["source_id"],
                        snapshot_id=table["snapshot_id"],
                        schema_name=table["schema_name"],
                        table_name=table["table_name"],
                        column_name=payload["column_name"],
                        payload=_dump(payload),
                        computed_at=payload.get("computed_at"),
                    )
                )

    def list_column_profiles(self, table_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(self._column_profiles)
                .where(self._column_profiles.c.table_metadata_id == table_id)
                .order_by(self._column_profiles.c.seq)
            ).fetchall()
        return [dict(_load(row.payload, {}), table_id=row.table_metadata_id) for row in rows]


def _new_id() -> str:
    from uuid import uuid4

    return uuid4().hex
