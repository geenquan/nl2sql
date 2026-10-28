"""元数据扫描用例编排（M1，修复轮加固）。

- 扫描为同步执行（无后台任务框架），作业状态落在元数据库中；
  后台队列与增量扫描在 M8 之前不引入。
- 整个生命周期有异常收尾：任务级异常尽力把任务落为 failed + finished_at +
  已清洗的错误原因；可恢复的表级异常按表隔离；持久化等基础设施故障中止扫描。
- 目录发布规则见 ports/repositories.py 与仓储实现：只有完整成功快照发布为
  完整目录；失败/部分成功/预算截断不替换已有有效目录，历史快照保留可查。
- 已知限制：若元数据库本身在收尾时不可写，最终状态无法落库（异常向上抛出，
  API 返回 5xx 而非伪造成功）。
"""

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from app.core.exceptions import (
    CredentialStoreError,
    DataSourceConnectionError,
    ResourceNotFoundError,
)
from app.domain.metadata import TableMetadata
from app.ports.connectors import ConnectorFactory
from app.ports.repositories import MetadataRepository, SourceRepository
from app.profiling.profiler import DataProfiler
from app.security.credentials import CredentialStore

_MAX_ERROR_MESSAGE_LENGTH = 300


class _ScanAborted(Exception):
    """内部哨兵：基础设施持久化失败后中止扫描，错误条目已记录，不再重复包装。"""


class ScanService:
    def __init__(
        self,
        source_repository: SourceRepository,
        metadata_repository: MetadataRepository,
        credential_store: CredentialStore,
        connector_factory: ConnectorFactory,
        profiler: DataProfiler | None = None,
        *,
        sample_rows: int,
        max_tables: int,
        max_columns: int,
        max_recorded_errors: int = 50,
    ) -> None:
        self._sources = source_repository
        self._metadata = metadata_repository
        self._credentials = credential_store
        self._connector_factory = connector_factory
        self._profiler = profiler or DataProfiler()
        self._sample_rows = sample_rows
        self._max_tables = max_tables
        self._max_columns = max_columns
        self._max_recorded_errors = max_recorded_errors

    # -- 扫描 -----------------------------------------------------------------

    def run_scan(self, source_id: str) -> dict[str, Any]:
        source = self._sources.get_source(source_id)
        if source is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        job_id = uuid4().hex
        snapshot_id = uuid4().hex
        base_job: dict[str, Any] = {
            "job_id": job_id,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "status": "running",
            "started_at": _now(),
            "sample_rows": self._sample_rows,
            "max_tables": self._max_tables,
        }
        self._metadata.save_scan_job(base_job)
        errors: list[dict[str, Any]] = []
        planned = scanned = failed = skipped = 0
        connector = None
        stage = "读取凭证"

        def finish(status: str) -> dict[str, Any]:
            job = {
                **base_job,
                "status": status,
                "finished_at": _now(),
                "tables_planned": planned,
                "tables_scanned": scanned,
                "tables_failed": failed,
                "tables_skipped": skipped,
                "errors": errors[: self._max_recorded_errors],
            }
            # 元数据库不可写时收尾会抛出，API 返回 5xx；不伪装成功。
            self._metadata.save_scan_job(job)
            return job

        def abort(stage_name: str, exc: Exception, schema: str | None, table: str | None) -> None:
            errors.append(
                {
                    "schema": schema,
                    "table": table,
                    "reason": "internal_error",
                    "message": _safe_unexpected_message(stage_name, exc, schema, table, job_id),
                }
            )

        try:
            try:
                password = self._credentials.get(source["credential_ref"])
            except CredentialStoreError as exc:
                errors.append(
                    {
                        "schema": None,
                        "table": None,
                        "reason": "credential_store_error",
                        # CredentialStoreError 本身使用固定安全文案，可安全透出。
                        "message": str(exc),
                    }
                )
                return finish("failed")
            if password is None:
                errors.append(
                    {
                        "schema": None,
                        "table": None,
                        "reason": "credential_missing",
                        "message": "凭证不存在或已被删除，无法建立连接",
                    }
                )
                return finish("failed")

            stage = "创建连接器"
            try:
                connector = self._connector_factory(
                    {
                        "dialect": source["dialect"],
                        "host": source["host"],
                        "port": source["port"],
                        "database": source["database"],
                        "username": source["username"],
                        "password": SecretStr(password),
                    }
                )
            except Exception as exc:
                abort(stage, exc, None, None)
                return finish("failed")

            stage = "测试连接"
            try:
                connector.test_connection()
            except DataSourceConnectionError as exc:
                errors.append(_error(None, None, exc))
                return finish("failed")

            stage = "列举Schema"
            try:
                visible = set(connector.list_schemas())
            except DataSourceConnectionError as exc:
                errors.append(_error(None, None, exc))
                return finish("failed")

            allowed = source["allowed_schemas"] or [source["database"]]
            target_schemas: list[str] = []
            for schema in allowed:
                if schema in visible:
                    target_schemas.append(schema)
                else:
                    errors.append(
                        {
                            "schema": schema,
                            "table": None,
                            "reason": "schema_not_visible",
                            "message": "Schema 不在账号可见范围或不存在",
                        }
                    )

            table_plan: list[tuple[str, str]] = []
            for schema in target_schemas:
                try:
                    table_names = connector.list_tables(schema)
                except DataSourceConnectionError as exc:
                    errors.append(_error(schema, None, exc))
                    continue
                table_plan.extend((schema, name) for name in table_names)
            planned = len(table_plan)
            budgeted = sorted(table_plan)[: self._max_tables]
            skipped = planned - len(budgeted)
            structural_errors = len(errors)

            stage = "扫描表"
            for schema, table_name in budgeted:
                # 表级可恢复异常（连接错误、非法标识符、采样/画像崩溃）只影响本表。
                try:
                    table = connector.get_table_metadata(schema, table_name)
                    table = dataclasses.replace(table, source_id=source_id)
                    sample_columns = tuple(
                        column.name for column in table.columns[: self._max_columns]
                    )
                    rows = connector.sample_table(
                        schema,
                        table_name,
                        sample_columns,
                        self._sample_rows,
                        order_by=table.primary_key,
                    )
                except DataSourceConnectionError as exc:
                    failed += 1
                    errors.append(_error(schema, table_name, exc))
                    continue
                except ValueError:
                    # 连接器标识符校验（本地固定文案）；不用异常文本，防止底层内容外泄。
                    failed += 1
                    errors.append(
                        {
                            "schema": schema,
                            "table": table_name,
                            "reason": "invalid_identifier",
                            "message": _invalid_identifier_message(schema, table_name),
                        }
                    )
                    continue
                except Exception as exc:  # 采样等未预期异常：按表隔离
                    failed += 1
                    errors.append(
                        {
                            "schema": schema,
                            "table": table_name,
                            "reason": "table_scan_error",
                            "message": _safe_unexpected_message(
                                "读取表结构", exc, schema, table_name, job_id
                            ),
                        }
                    )
                    continue

                try:
                    profiles = self._profiler.profile_table(
                        table,
                        rows,
                        sample_limit=self._sample_rows,
                        max_columns=self._max_columns,
                    )
                except Exception as exc:  # 画像崩溃：表级可恢复
                    failed += 1
                    errors.append(
                        {
                            "schema": schema,
                            "table": table_name,
                            "reason": "table_scan_error",
                            "message": _safe_unexpected_message(
                                "计算画像", exc, schema, table_name, job_id
                            ),
                        }
                    )
                    continue

                # 持久化属于基础设施：失败即中止，避免产生不一致的快照。
                try:
                    table_id = self._metadata.save_table_metadata(
                        _table_payload(source_id, snapshot_id, table)
                    )
                    self._metadata.save_column_profiles(
                        table_id, [dataclasses.asdict(profile) for profile in profiles]
                    )
                except Exception as exc:
                    abort("持久化快照", exc, schema, table_name)
                    raise _ScanAborted from None
                scanned += 1
        except _ScanAborted:
            status = "failed"
        except Exception as exc:  # 任务级未预期异常：收尾为 failed，不伪装成功
            abort(stage, exc, None, None)
            status = "failed"
        else:
            # 状态判定：
            # failed＝结构性失败（连接/凭证/Schema 不可见）或预算内表全部失败；
            # partial＝存在表级失败或被预算截断（缺失表不当作确认删除）；
            # succeeded＝完整成功（此时确认空 Schema 可发布空目录）。
            all_budgeted_failed = failed > 0 and scanned == 0 and failed == len(budgeted)
            if (structural_errors > 0 and scanned == 0) or all_budgeted_failed:
                status = "failed"
            elif failed > 0 or skipped > 0 or structural_errors > 0:
                status = "partial"
            else:
                status = "succeeded"
        finally:
            if connector is not None:
                try:
                    connector.close()
                except Exception:  # close 失败不得覆盖主要结果或阻断收尾
                    pass
        return finish(status)

    # -- 查询入口 ---------------------------------------------------------------

    def get_scan(self, job_id: str) -> dict[str, Any]:
        job = self._metadata.get_scan_job(job_id)
        if job is None:
            raise ResourceNotFoundError("scan", f"扫描任务不存在：{job_id}")
        return job

    def list_scans(self, source_id: str) -> list[dict[str, Any]]:
        if self._sources.get_source(source_id) is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        return self._metadata.list_scan_jobs(source_id)

    def list_tables(
        self,
        source_id: str,
        schema_name: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """默认读取发布目录；显式传 snapshot_id 可查看历史快照（诊断用）。"""
        if self._sources.get_source(source_id) is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        tables = self._metadata.list_table_metadata(source_id, snapshot_id=snapshot_id)
        if schema_name:
            tables = [t for t in tables if t["schema_name"] == schema_name]
        return tables

    def get_table(self, table_id: str) -> dict[str, Any]:
        table = self._metadata.get_table_metadata(table_id)
        if table is None:
            raise ResourceNotFoundError("table", f"表元数据不存在：{table_id}")
        return table

    def get_table_profile(self, table_id: str) -> list[dict[str, Any]]:
        if self._metadata.get_table_metadata(table_id) is None:
            raise ResourceNotFoundError("table", f"表元数据不存在：{table_id}")
        return self._metadata.list_column_profiles(table_id)

    def source_overview(self, source_id: str) -> dict[str, Any]:
        if self._sources.get_source(source_id) is None:
            raise ResourceNotFoundError("source", f"数据源不存在：{source_id}")
        tables = self._metadata.list_table_metadata(source_id)
        jobs = self._metadata.list_scan_jobs(source_id)
        latest = jobs[0] if jobs else None
        published = self._metadata.get_published_snapshot(source_id)
        catalog = (
            {
                "snapshot_id": published["snapshot_id"],
                "status": published["status"],
                "complete": published["complete"],
            }
            if published
            else None
        )
        return {
            "table_count": len(tables),
            "column_count": sum(len(t["columns"]) for t in tables),
            "latest_scan": (
                {
                    "job_id": latest["job_id"],
                    "status": latest["status"],
                    "started_at": latest["started_at"],
                    "finished_at": latest["finished_at"],
                }
                if latest
                else None
            ),
            "catalog": catalog,
            "latest_table_ids": [t["table_id"] for t in tables],
        }


def _error(schema: str | None, table: str | None, exc: DataSourceConnectionError) -> dict[str, Any]:
    return {
        "schema": schema,
        "table": table,
        "reason": exc.reason,
        "message": str(exc),
    }


def _safe_unexpected_message(
    stage: str, exc: Exception, schema: str | None, table: str | None, job_id: str
) -> str:
    """未预期异常的固定安全文案。

    绝不使用 str(exc)/repr(exc)/异常参数——底层异常文本可能携带业务样本
    （如采样值、邮箱、token），一旦持久化或经 API 返回即泄漏。
    只保留异常类型名与阶段/Schema/表/任务标识用于定位；也不写日志规避。
    """
    parts = [f"未预期错误[{type(exc).__name__}]", f"阶段={stage}"]
    if schema:
        parts.append(f"schema={schema}")
    if table:
        parts.append(f"table={table}")
    parts.append(f"job_id={job_id}")
    return "，".join(parts)[:_MAX_ERROR_MESSAGE_LENGTH] + "；错误详情已抑制"


def _invalid_identifier_message(schema: str, table_name: str) -> str:
    return (
        f"连接器拒绝为该表构造查询（标识符非法）：schema={schema}，table={table_name}"
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _table_payload(source_id: str, snapshot_id: str, table: TableMetadata) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "schema_name": table.schema_name,
        "table_name": table.table_name,
        "table_comment": table.table_comment,
        "columns": [dataclasses.asdict(column) for column in table.columns],
        "primary_key": list(table.primary_key),
        "foreign_keys": [dict(fk) for fk in table.foreign_keys],
        "indexes": [dataclasses.asdict(index) for index in table.indexes],
        "row_count_estimate": table.row_count_estimate,
        "row_count_is_estimate": table.row_count_is_estimate,
        "scanned_at": _now(),
    }
