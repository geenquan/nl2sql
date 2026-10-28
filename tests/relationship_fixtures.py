"""M3 共享测试基建：合成库目录、内存数据与探查/连接器替身。

数据全部为虚构的合成数据（hotel 风格 + 负例），标签为开发标注，
不冒充真实业务人工金标准。所有计算离线完成，不访问网络。
"""

from typing import Any

from app.core.exceptions import DataSourceConnectionError
from app.domain.metadata import ColumnMetadata, IndexMetadata, TableMetadata
from app.ports.connectors import (
    JoinMetricsRequest,
    JoinMetricsResult,
    RelationshipProbe,
    WindowMetricsRequest,
    WindowMetricsResult,
)

SCHEMA = "app"


def _col(name: str, data_type: str, nullable: bool = True, comment: str | None = None):
    return {"name": name, "data_type": data_type, "nullable": nullable,
            "comment": comment}


def synthetic_catalog(source_id: str = "src-m3", snapshot_id: str = "snap-m3"):
    """合成库目录：正例外键 + 无外键正例 + 四类负例。

    负例设计：
    - guest.status_code ↔ booking.status_code：共享状态码；
    - room_type.code ↔ service_item.code：字典↔字典同名词典码；
    - payment.bank_txn_no ↔ booking.id：字符串 vs 整数（编码映射形态）；
    - booking.tenant_seq ↔ tenant.id：租户内编号（跨租户重复，唯一率低）。
    """
    tables = {
        "tenant": {
            "columns": [_col("id", "int", False, "租户ID"), _col("name", "varchar(50)")],
            "primary_key": ["id"], "foreign_keys": [], "indexes": [],
        },
        "guest": {
            "columns": [
                _col("id", "int", False), _col("tenant_id", "int"),
                _col("name", "varchar(50)"), _col("phone", "varchar(20)"),
                _col("email", "varchar(100)"), _col("status_code", "varchar(10)"),
            ],
            "primary_key": ["id"],
            "foreign_keys": [{"name": "fk_guest_tenant", "columns": ["tenant_id"],
                              "ref_table": "tenant", "ref_columns": ["id"]}],
            "indexes": [{"name": "uq_guest_email", "columns": ["email"], "unique": True}],
        },
        "room_type": {
            "columns": [_col("id", "int", False), _col("code", "varchar(20)"),
                        _col("name", "varchar(50)")],
            "primary_key": ["id"], "foreign_keys": [],
            "indexes": [{"name": "uq_rt_code", "columns": ["code"], "unique": True}],
        },
        "room": {
            "columns": [
                _col("id", "int", False), _col("room_no", "varchar(20)"),
                _col("type_id", "int"), _col("tenant_id", "int"),
            ],
            "primary_key": ["id"],
            "foreign_keys": [
                {"name": "fk_room_type", "columns": ["type_id"],
                 "ref_table": "room_type", "ref_columns": ["id"]},
                {"name": "fk_room_tenant", "columns": ["tenant_id"],
                 "ref_table": "tenant", "ref_columns": ["id"]},
            ],
            "indexes": [{"name": "uq_room_no", "columns": ["room_no"], "unique": True}],
        },
        "booking": {
            "columns": [
                _col("id", "int", False), _col("booking_no", "varchar(30)"),
                _col("guest_id", "int"), _col("room_id", "int"),
                _col("status_code", "varchar(10)"),
                _col("created_at", "datetime"), _col("tenant_seq", "int"),
            ],
            "primary_key": ["id"],
            "foreign_keys": [
                {"name": "fk_booking_guest", "columns": ["guest_id"],
                 "ref_table": "guest", "ref_columns": ["id"]},
                {"name": "fk_booking_room", "columns": ["room_id"],
                 "ref_table": "room", "ref_columns": ["id"]},
            ],
            "indexes": [{"name": "uq_booking_no", "columns": ["booking_no"],
                         "unique": True}],
        },
        "service_item": {
            "columns": [
                _col("id", "int", False), _col("code", "varchar(20)"),
                _col("name", "varchar(50)"), _col("price", "decimal(10,2)"),
            ],
            "primary_key": ["id"], "foreign_keys": [],
            "indexes": [{"name": "uq_si_code", "columns": ["code"], "unique": True}],
        },
        "booking_item": {
            "columns": [_col("booking_id", "int", False), _col("item_id", "int", False)],
            "primary_key": ["booking_id", "item_id"],
            "foreign_keys": [
                {"name": "fk_bi_booking", "columns": ["booking_id"],
                 "ref_table": "booking", "ref_columns": ["id"]},
                {"name": "fk_bi_item", "columns": ["item_id"],
                 "ref_table": "service_item", "ref_columns": ["id"]},
            ],
            "indexes": [],
        },
        "payment": {
            "columns": [
                _col("id", "int", False), _col("booking_no", "varchar(30)"),
                _col("amount", "decimal(10,2)"), _col("paid_at", "datetime"),
                _col("bank_txn_no", "varchar(40)"),
            ],
            "primary_key": ["id"], "foreign_keys": [],
            "indexes": [],
        },
        "employee": {
            "columns": [_col("id", "int", False), _col("name", "varchar(50)"),
                        _col("manager_id", "int")],
            "primary_key": ["id"],
            "foreign_keys": [{"name": "fk_emp_manager", "columns": ["manager_id"],
                              "ref_table": "employee", "ref_columns": ["id"]}],
            "indexes": [],
        },
    }
    catalog = []
    row_estimates = {"tenant": 10, "guest": 100, "room_type": 5, "room": 50,
                     "booking": 200, "service_item": 20, "booking_item": 400,
                     "payment": 160, "employee": 10}
    for table_name, spec in tables.items():
        for fk in spec["foreign_keys"]:
            fk.setdefault("ref_schema", SCHEMA)
        catalog.append({
            "table_id": f"{snapshot_id}-{table_name}",
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "schema_name": SCHEMA,
            "table_name": table_name,
            "table_comment": None,
            "columns": spec["columns"],
            "primary_key": spec["primary_key"],
            "foreign_keys": spec["foreign_keys"],
            "indexes": spec["indexes"],
            "row_count_estimate": row_estimates[table_name],
            "row_count_is_estimate": True,
        })
    return catalog


def seed_catalog(repo, catalog: list[dict], *, job_status: str = "succeeded",
                 source_id: str = "src-m3", snapshot_id: str = "snap-m3",
                 started_at: str = "2026-10-28T00:00:00+00:00"):
    """把合成目录与扫描任务写入仓储（发布快照 = 该 snapshot）。"""
    repo.save_scan_job({
        "job_id": f"job-{snapshot_id}", "source_id": source_id,
        "snapshot_id": snapshot_id, "status": job_status,
        "started_at": started_at,
        "finished_at": "2026-10-28T00:00:01+00:00",
        "tables_planned": len(catalog), "tables_scanned": len(catalog),
        "tables_failed": 0, "tables_skipped": 0, "errors": [],
    })
    table_ids = {}
    for table in catalog:
        table_ids[table["table_name"]] = repo.save_table_metadata(dict(table))
    return table_ids


def seed_profiles(repo, catalog: list[dict]) -> None:
    """写入简化画像（唯一率口径 exact；tenant_seq 刻意低唯一率作负例）。"""
    for table in catalog:
        profiles = []
        for column in table["columns"]:
            name = column["name"]
            if name == "tenant_seq":
                uniqueness, distinct = 0.1, 20
            elif name in ("manager_id",):
                uniqueness, distinct = 0.9, 5
            elif name.endswith("_id") or name == "id":
                uniqueness, distinct = 1.0, 100
            else:
                uniqueness, distinct = 0.5, 10
            profiles.append({
                "column_name": name,
                "null_ratio": 0.05 if name == "manager_id" else 0.0,
                "distinct_count_estimate": distinct,
                "uniqueness_ratio": uniqueness,
                "metric_basis": "exact",
                "sample_row_count": 100,
                "row_count_basis": 100,
                "sample_values": [],
                "samples_redacted": True,
                "empty_string_count": 0,
                "masking_version": "mask-v2",
            })
        repo.save_column_profiles(table["table_id"], profiles)


def seed_semantics(repo, catalog: list[dict]) -> None:
    """写入 M2 有效语义（简化：一条 suggested 修订即可）。"""

    semantics_map = {
        ("guest", "tenant_id"): ("foreign_identifier_candidate", ["租户"]),
        ("booking", "guest_id"): ("foreign_identifier_candidate", ["客人"]),
        ("booking", "room_id"): ("foreign_identifier_candidate", ["客房"]),
        ("room", "type_id"): ("foreign_identifier_candidate", ["房型"]),
        ("room", "tenant_id"): ("foreign_identifier_candidate", ["租户"]),
        ("booking_item", "booking_id"): ("foreign_identifier_candidate", ["订单"]),
        ("booking_item", "item_id"): ("foreign_identifier_candidate", ["服务项目"]),
        ("payment", "booking_no"): ("foreign_identifier_candidate", ["订单"]),
        ("employee", "manager_id"): ("foreign_identifier_candidate", ["员工"]),
        ("guest", "status_code"): ("status", []),
        ("booking", "status_code"): ("status", []),
        ("room_type", "code"): ("entity_identifier", []),
        ("service_item", "code"): ("entity_identifier", []),
    }
    for table in catalog:
        for column in table["columns"]:
            entry = semantics_map.get((table["table_name"], column["name"]))
            category = entry[0] if entry else (
                "entity_identifier" if column["name"] == "id" else "unknown")
            entities = entry[1] if entry else []
            record = {
                "source_id": table["source_id"],
                "snapshot_id": table["snapshot_id"],
                "table_id": table["table_id"],
                "column_name": column["name"],
                "original_name": column["name"],
                "original_comment": column.get("comment"),
                "category": category,
                "suggested_business_name": None,
                "candidate_entities": entities,
                "alternatives": [], "evidence": [], "confidence": "medium",
                "inference_source": "rules", "review_status": "suggested",
                "created_at": "2026-10-28T00:00:00+00:00",
            }
            repo.save_field_semantics(record)


def synthetic_rows() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """合成数据（与目录一致；探查替身据此计算指标）。"""
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    rows[(SCHEMA, "tenant")] = [
        {"id": i, "name": f"租户{i}"} for i in range(1, 11)]
    rows[(SCHEMA, "guest")] = [
        {"id": i, "tenant_id": (i - 1) // 10 + 1, "name": f"客人{i}",
         "phone": f"1380000{i:04d}", "email": f"g{i}@example.com",
         "status_code": "active" if i % 2 else "inactive"}
        for i in range(1, 101)]
    rows[(SCHEMA, "room_type")] = [
        {"id": i, "code": f"RT{i:02d}", "name": f"房型{i}"} for i in range(1, 6)]
    rows[(SCHEMA, "room")] = [
        {"id": i, "room_no": f"R{i:03d}", "type_id": (i - 1) // 10 + 1,
         "tenant_id": (i - 1) // 5 + 1} for i in range(1, 51)]
    rows[(SCHEMA, "booking")] = []
    statuses = ("confirmed", "cancelled", "paid")
    for i in range(1, 201):
        rows[(SCHEMA, "booking")].append({
            "id": i, "booking_no": f"B{i:04d}",
            "guest_id": (i - 1) % 100 + 1, "room_id": (i - 1) % 50 + 1,
            "status_code": statuses[(i - 1) % 3],
            "created_at": f"2026-01-{(i - 1) % 28 + 1:02d} 10:00:00",
            "tenant_seq": (i - 1) % 20 + 1,
        })
    rows[(SCHEMA, "service_item")] = [
        {"id": i, "code": f"SI{i:02d}", "name": f"服务{i}", "price": 10.5}
        for i in range(1, 21)]
    rows[(SCHEMA, "booking_item")] = []
    for i in range(1, 201):
        rows[(SCHEMA, "booking_item")].append({"booking_id": i, "item_id": (i - 1) % 20 + 1})
        rows[(SCHEMA, "booking_item")].append(
            {"booking_id": i, "item_id": (i + 4) % 20 + 1})
    payment_rows = []
    pid = 1
    for i in range(1, 101):  # 订单 1..100 各一笔
        payment_rows.append({"id": pid, "booking_no": f"B{i:04d}",
                             "amount": 100.0 + i, "paid_at": "2026-01-05 12:00:00",
                             "bank_txn_no": f"TXN{pid:06d}"})
        pid += 1
    for i in range(1, 61):  # 订单 1..60 再各一笔（源端重复 → many_to_one）
        payment_rows.append({"id": pid, "booking_no": f"B{i:04d}",
                             "amount": 50.0 + i, "paid_at": "2026-01-06 12:00:00",
                             "bank_txn_no": f"TXN{pid:06d}"})
        pid += 1
    rows[(SCHEMA, "payment")] = payment_rows
    rows[(SCHEMA, "employee")] = [
        {"id": 1, "name": "E1", "manager_id": None},
        {"id": 2, "name": "E2", "manager_id": 1},
        {"id": 3, "name": "E3", "manager_id": 1},
        {"id": 4, "name": "E4", "manager_id": 2},
        {"id": 5, "name": "E5", "manager_id": 2},
        {"id": 6, "name": "E6", "manager_id": 3},
        {"id": 7, "name": "E7", "manager_id": 3},
        {"id": 8, "name": "E8", "manager_id": 4},
        {"id": 9, "name": "E9", "manager_id": 4},
        {"id": 10, "name": "E10", "manager_id": 5},
    ]
    return rows


class FakeRelationshipProbe(RelationshipProbe):
    """内存探查替身：按合成数据计算窗口/JOIN 指标（typed 语义）。"""

    def __init__(self, rows: dict[tuple[str, str], list[dict[str, Any]]] | None = None):
        self.rows = rows if rows is not None else synthetic_rows()
        self.window_requests: list[WindowMetricsRequest] = []
        self.join_requests: list[JoinMetricsRequest] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def _table_rows(self, schema: str, table: str) -> list[dict[str, Any]]:
        key = (schema, table)
        if key not in self.rows:
            raise DataSourceConnectionError(
                "unknown_table", f"MySQL 查询失败（unknown_table）：{schema}.{table}")
        return self.rows[key]

    @staticmethod
    def _window_slice(rows, order_by, limit):
        if order_by:
            rows = sorted(rows, key=lambda r: tuple(
                (r.get(c) is None, r.get(c)) for c in order_by))
        return rows[:limit]

    def window_metrics(self, request: WindowMetricsRequest) -> WindowMetricsResult:
        self.window_requests.append(request)
        rows = self._window_slice(
            self._table_rows(request.schema_name, request.table_name),
            request.order_by, request.limit)

        def _all_non_null(row) -> bool:
            return all(row.get(c) is not None for c in request.columns)

        def _has_empty(row) -> bool:
            return any(row.get(c) == "" for c in request.string_columns)

        non_null = [r for r in rows if _all_non_null(r)]
        empty = [r for r in non_null if _has_empty(r)]
        distinct = {
            tuple(r[c] for c in request.columns) for r in non_null
        }
        return WindowMetricsResult(
            window_rows=len(rows),
            non_null_rows=len(non_null),
            empty_string_rows=len(empty),
            distinct_values=len(distinct),
            truncated=len(rows) >= request.limit,
        )

    def join_metrics(self, request: JoinMetricsRequest) -> JoinMetricsResult:
        self.join_requests.append(request)
        left = self._window_slice(
            self._table_rows(request.left_schema, request.left_table),
            request.left_order_by, request.left_limit)
        right = self._table_rows(request.right_schema, request.right_table)
        right_index: dict[tuple, int] = {}
        for row in right:
            key = tuple(row.get(c) for c in request.right_columns)
            if any(v is None for v in key):
                continue  # NULL 键不参与匹配
            right_index[key] = right_index.get(key, 0) + 1

        def _empty_key(row) -> bool:
            return any(row.get(c) == "" for c in request.string_join_columns)

        unmatched = 0
        rows_after = 0
        inner = 0
        empty_matched = 0
        matched_right_keys: set[tuple] = set()
        for row in left:
            key = tuple(row.get(c) for c in request.left_columns)
            if any(v is None for v in key):
                unmatched += 1
                rows_after += 1
                continue
            matches = right_index.get(key, 0)
            if matches == 0:
                unmatched += 1
                rows_after += 1
                continue
            rows_after += matches
            inner += matches
            if _empty_key(row):
                empty_matched += 1
            matched_right_keys.add(key)
        left_rows = len(left) if request.left_order_by else None
        return JoinMetricsResult(
            left_rows_seen=left_rows,
            unmatched_left_rows=unmatched,
            rows_after_left_join=rows_after,
            distinct_right_matched=len(matched_right_keys),
            empty_key_matched_rows=empty_matched,
        )


class FakeM3Connector:
    """结构核对替身：返回与快照一致的 TableMetadata（可注入漂移）。"""

    def __init__(self, catalog: list[dict], *, alter_table: str | None = None):
        self.catalog = {(t["schema_name"], t["table_name"]): t for t in catalog}
        self.alter_table = alter_table
        self.closed = False

    def get_table_metadata(self, schema_name: str, table_name: str):
        table = self.catalog.get((schema_name, table_name))
        if table is None:
            raise DataSourceConnectionError(
                "unknown_table", f"MySQL 查询失败（unknown_table）：{table_name}")
        columns = [dict(c) for c in table["columns"]]
        if self.alter_table == table_name:
            columns.append({"name": "extra_col", "data_type": "varchar(10)"})
        return TableMetadata(
            source_id=table["source_id"],
            schema_name=table["schema_name"],
            table_name=table["table_name"],
            columns=tuple(
                ColumnMetadata(
                    name=c["name"], data_type=c["data_type"],
                    nullable=c.get("nullable", True), comment=c.get("comment"))
                for c in columns
            ),
            primary_key=tuple(table["primary_key"]),
            foreign_keys=tuple(dict(fk) for fk in table["foreign_keys"]),
            indexes=tuple(
                IndexMetadata(name=idx["name"], columns=tuple(idx["columns"]),
                              unique=idx.get("unique", False))
                for idx in table["indexes"]
            ),
        )

    def close(self) -> None:
        self.closed = True
