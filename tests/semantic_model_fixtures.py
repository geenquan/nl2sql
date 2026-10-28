"""M4 共享测试夹具：在 M3 合成目录上扩展语义层场景（全部虚构数据）。

场景覆盖：
- 主数据→事件→明细（tenant/guest/room_type/service_item → booking/payment
  → booking_item）；
- 桥接表与多对多（booking_item 组合主键）；
- 历史版本表（audit_log）；
- 两条竞争关联路径（booking→guest→tenant 与 booking→room→tenant）；
- 跨 Schema 同名表（archive.guest）；
- 无法确定业务粒度的表（no_pk_notes，无主键/唯一约束）。
"""

from typing import Any

from app.domain.relationships import relationship_key
from tests.relationship_fixtures import (
    SCHEMA,
    seed_catalog,
    seed_profiles,
    seed_semantics,
    synthetic_catalog,
)


def m4_catalog(source_id: str, snapshot_id: str = "snap-m4") -> list[dict[str, Any]]:
    catalog = synthetic_catalog(source_id=source_id, snapshot_id=snapshot_id)
    for table in catalog:
        table["table_id"] = f"{snapshot_id}-{table['table_name']}"
    catalog.append({
        "table_id": f"{snapshot_id}-audit_log",
        "source_id": source_id, "snapshot_id": snapshot_id,
        "schema_name": SCHEMA, "table_name": "audit_log", "table_comment": None,
        "columns": [
            {"name": "id", "data_type": "bigint", "nullable": False},
            {"name": "entity_type", "data_type": "varchar(30)"},
            {"name": "action", "data_type": "varchar(30)"},
            {"name": "operated_at", "data_type": "datetime"},
        ],
        "primary_key": ["id"], "foreign_keys": [], "indexes": [],
        "row_count_estimate": 1000, "row_count_is_estimate": True,
    })
    catalog.append({  # 跨 Schema 同名表：archive.guest ≠ app.guest
        "table_id": f"{snapshot_id}-archive-guest",
        "source_id": source_id, "snapshot_id": snapshot_id,
        "schema_name": "archive", "table_name": "guest", "table_comment": None,
        "columns": [
            {"name": "id", "data_type": "int", "nullable": False},
            {"name": "old_name", "data_type": "varchar(50)"},
        ],
        "primary_key": ["id"], "foreign_keys": [], "indexes": [],
        "row_count_estimate": 80, "row_count_is_estimate": True,
    })
    catalog.append({  # 无法确定业务粒度：无主键/唯一约束
        "table_id": f"{snapshot_id}-no_pk_notes",
        "source_id": source_id, "snapshot_id": snapshot_id,
        "schema_name": SCHEMA, "table_name": "no_pk_notes", "table_comment": None,
        "columns": [
            {"name": "note_text", "data_type": "varchar(200)"},
            {"name": "created_at", "data_type": "datetime"},
        ],
        "primary_key": [], "foreign_keys": [], "indexes": [],
        "row_count_estimate": 40, "row_count_is_estimate": True,
    })
    return catalog


# M2 表用途（table_id → purpose）
M4_PURPOSES = {
    "tenant": "master_data", "guest": "master_data", "room_type": "master_data",
    "service_item": "master_data", "room": "master_data",
    "booking": "transaction_event", "payment": "transaction_event",
    "booking_item": "relation", "employee": "master_data",
    "audit_log": "log_history", "archive.guest": "master_data",
    "no_pk_notes": "unknown",
}


def seed_m4_environment(
    repo, source_id: str, *, snapshot_id: str = "snap-m4",
    with_semantics: bool = True,
    relationship_specs: list[dict[str, Any]] | None = None,
    started_at: str = "2026-10-29T00:00:00+00:00",
) -> list[dict[str, Any]]:
    """写入快照目录/画像/语义/表用途/已验证关系，返回目录。"""
    catalog = m4_catalog(source_id, snapshot_id)
    seed_catalog(repo, catalog, source_id=source_id, snapshot_id=snapshot_id,
                 started_at=started_at)
    seed_profiles(repo, catalog)
    if with_semantics:
        seed_semantics(repo, catalog)
        _add_amount_semantics(repo, source_id, snapshot_id, catalog)
    for table in catalog:
        purpose = M4_PURPOSES.get(
            f"{table['schema_name']}.{table['table_name']}"
            if table["schema_name"] != SCHEMA else table["table_name"], "unknown")
        repo.save_table_purpose({
            "source_id": source_id, "snapshot_id": snapshot_id,
            "table_id": table["table_id"], "table_name": table["table_name"],
            "purpose": purpose, "rationale": "夹具用途",
            "alternatives": [], "evidence": [], "confidence": "medium",
            "inference_source": "rules", "review_status": "suggested",
            "created_at": "2026-10-29T00:00:00+00:00",
        })
    if relationship_specs is not None:
        seed_relationships(repo, source_id, snapshot_id, catalog,
                           relationship_specs)
    return catalog


def _add_amount_semantics(repo, source_id, snapshot_id, catalog):
    """为金额/数量字段补充分类语义（M4 指标候选依据；虚构数据）。"""
    targets = {
        ("service_item", "price"): ("amount", ["金额"]),
        ("booking", "tenant_seq"): ("quantity", ["序号"]),
    }
    for table in catalog:
        for column in table.get("columns") or []:
            entry = targets.get((table["table_name"], column["name"]))
            if entry is None:
                continue
            repo.save_field_semantics({
                "source_id": source_id, "snapshot_id": snapshot_id,
                "table_id": table["table_id"], "column_name": column["name"],
                "original_name": column["name"], "original_comment":
                    column.get("comment"),
                "category": entry[0], "suggested_business_name": None,
                "candidate_entities": entry[1], "alternatives": [],
                "evidence": [], "confidence": "medium",
                "inference_source": "rules", "review_status": "suggested",
                "created_at": "2026-10-29T00:00:01+00:00",
            })


DEFAULT_VALIDATED_RELATIONSHIPS = [
    # (source_table, source_columns, target_table, target_columns, cardinality)
    {"src": "booking", "src_cols": ["guest_id"], "tgt": "guest",
     "tgt_cols": ["id"], "cardinality": "many_to_one"},
    {"src": "booking", "src_cols": ["room_id"], "tgt": "room",
     "tgt_cols": ["id"], "cardinality": "many_to_one"},
    {"src": "guest", "src_cols": ["tenant_id"], "tgt": "tenant",
     "tgt_cols": ["id"], "cardinality": "many_to_one"},
    {"src": "room", "src_cols": ["tenant_id"], "tgt": "tenant",
     "tgt_cols": ["id"], "cardinality": "many_to_one"},
    {"src": "room", "src_cols": ["type_id"], "tgt": "room_type",
     "tgt_cols": ["id"], "cardinality": "many_to_one"},
    {"src": "payment", "src_cols": ["booking_no"], "tgt": "booking",
     "tgt_cols": ["booking_no"], "cardinality": "many_to_one"},
]


def seed_relationships(
    repo, source_id: str, snapshot_id: str, catalog: list[dict[str, Any]],
    specs: list[dict[str, Any]],
) -> list[str]:
    """按规格直接写入 M3 validated 关系修订（确定性；返回 relationship_key 列表）。"""
    by_name = {
        (t["schema_name"], t["table_name"]): t for t in catalog}
    keys: list[str] = []
    for spec in specs:
        source_table = by_name[(SCHEMA, spec["src"])]
        target_table = by_name[(SCHEMA, spec["tgt"])]
        key = relationship_key(
            source_id, SCHEMA, spec["src"], spec["src_cols"],
            SCHEMA, spec["tgt"], spec["tgt_cols"])
        repo.save_relationship({
            "relationship_key": key,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "source_schema": SCHEMA, "source_table": spec["src"],
            "source_table_id": source_table["table_id"],
            "source_columns": list(spec["src_cols"]),
            "target_schema": SCHEMA, "target_table": spec["tgt"],
            "target_table_id": target_table["table_id"],
            "target_columns": list(spec["tgt_cols"]),
            "origin": ["explicit_fk"], "cardinality": spec["cardinality"],
            "recall_reasons": [], "risks": [], "support_evidence": [],
            "counter_evidence": [], "scores": {"total_weighted": 0.9},
            "status": spec.get("status", "validated"),
            "evidence": {
                "cardinality_observed": {"cardinality": spec["cardinality"]},
            },
            "validation": {
                "decision": spec.get("status", "validated"),
                "scope": {"snapshot_id": snapshot_id, "coverage_basis": "exact",
                          "coverage_ratio": 1.0},
            },
            "inference_source": "system",
            "revision": 1, "created_at": "2026-10-29T00:00:00+00:00",
        })
        keys.append(key)
    return keys
