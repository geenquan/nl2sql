"""M5 合成数据与共享夹具（SQLite 业务库 + M1–M4 全链种子）。

核心场景：一中心表 + 两个独立明细表（5×3 不放大）；many_to_one 维度；
组合键/键值含分隔字符/跨 Schema 同名表；NULL/空表/未匹配/重复键。
全部虚构数据。
"""

import json
from typing import Any

from sqlalchemy import create_engine, text

from app.adapters.repositories.sqlite_metadata_repository import (
    SQLiteMetadataRepository,
)
from app.core.config import Settings
from app.domain.relationships import relationship_key
from app.services.dataset_service import DatasetService
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import SCHEMA, seed_m4_environment

ARCHIVE = "archive"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def build_sqlite_data(database_url: str) -> dict[str, int]:
    """建合成业务表并写入 5×5/3 场景数据（含边界值），返回行数基准。

    schema 限定名通过 ATTACH 文件库实现（跨连接持久；跨 Schema 同名表
    app.guest/archive.guest 真实分离）。
    """
    engine = create_engine(database_url)
    base = database_url.replace("sqlite:///", "")
    with engine.begin() as conn:
        conn.execute(text(f"ATTACH DATABASE '{base}.app' AS app"))
        conn.execute(text(f"ATTACH DATABASE '{base}.archive' AS archive"))
        conn.execute(text("""
            CREATE TABLE app.tenant (id INTEGER PRIMARY KEY, name TEXT)"""))
        conn.execute(text("""
            CREATE TABLE app.guest (id INTEGER PRIMARY KEY,
                tenant_id INTEGER, guest_name TEXT, email TEXT,
                phone TEXT, status_code TEXT)"""))
        conn.execute(text("""
            CREATE TABLE app.booking (id INTEGER PRIMARY KEY,
                booking_no TEXT UNIQUE, guest_id INTEGER, room_id INTEGER,
                status_code TEXT, created_at TEXT, amount TEXT,
                tenant_seq INTEGER)"""))
        conn.execute(text("""
            CREATE TABLE app.detail_a (id INTEGER PRIMARY KEY,
                booking_id INTEGER, line_amount TEXT, qty INTEGER)"""))
        conn.execute(text("""
            CREATE TABLE app.detail_b (id INTEGER PRIMARY KEY,
                booking_id INTEGER, line_amount TEXT, note TEXT)"""))
        conn.execute(text("""
            CREATE TABLE app.no_pk_notes (note_text TEXT, created_at TEXT)"""))
        conn.execute(text(
            "CREATE TABLE archive.guest (id INTEGER PRIMARY KEY, "
            "old_name TEXT)"))
        conn.execute(text(
            "INSERT INTO app.tenant VALUES (1,'租户一'),(2,'租户二')"))
        # guest 5 行：tenant_id 含 NULL；键值含分隔字符
        conn.execute(text(
            "INSERT INTO app.guest VALUES "
            "(1,1,'张三','a@x.com','13800000001','active'),"
            "(2,1,'李四|分隔','b@x.com','13800000002','active'),"
            "(3,2,'王五','c@x.com','13800000003','inactive'),"
            "(4,NULL,'赵六','d@x.com','13800000004','active'),"
            "(5,2,'钱七','e@x.com','13800000005','active')"))
        # booking 5 行：guest_id 含未匹配（999）
        conn.execute(text(
            "INSERT INTO app.booking VALUES "
            "(1,'B0001',1,1,'paid','2026-01-01 10:00:00','100.50',1),"
            "(2,'B0002',2,1,'paid','2026-01-02 10:00:00','200.25',2),"
            "(3,'B0003',3,2,'cancelled','2026-01-03 10:00:00','50.00',1),"
            "(4,'B0004',999,2,'paid','2026-01-04 10:00:00','75.75',3),"
            "(5,'B0005',5,NULL,'paid','2026-01-05 10:00:00','10.00',2)"))
        # 明细 A：booking 1..5 各 5/4/3/2/1 行（共 15 行）
        rows_a = []
        for b in range(1, 6):
            for i in range(6 - b):
                rows_a.append(
                    f"({b * 10 + i},{b},'{10 + b}.10','{i + 1}')")
        conn.execute(text(
            "INSERT INTO app.detail_a VALUES " + ",".join(rows_a)))
        # 明细 B：booking 1..3 各 1 行（共 3 行）
        conn.execute(text(
            "INSERT INTO app.detail_b VALUES "
            "(1,1,'5.00','x'),(2,2,'6.00','y'),(3,3,'7.00','z')"))
        conn.execute(text(
            "INSERT INTO app.no_pk_notes VALUES "
            "('note1','2026-01-01'),('note2','2026-01-02')"))
        conn.execute(text("INSERT INTO archive.guest VALUES (1,'旧档案')"))
    engine.dispose()
    return {"central_rows": 5, "detail_a_rows": 15, "detail_b_rows": 3}


def seed_environment(tmp_path) -> dict[str, Any]:
    """完整环境：SQLite 元数据 + 业务库 + M1–M4 种子 + 已发布模型。"""
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1",
        "port": 3306, "database": SCHEMA, "username": "ro",
        "credential_ref": "cred-1", "allowed_schemas": [SCHEMA],
        "read_only": True, "is_default": True,
    })
    base_catalog = seed_m4_environment(repo, source_id)
    # 扩展两个明细表（组合进快照）
    extra = [
        _table(source_id, "detail_a", [
            ("id", "INTEGER", False), ("booking_id", "INTEGER", False),
            ("line_amount", "TEXT", True), ("qty", "INTEGER", True)]),
        _table(source_id, "detail_b", [
            ("id", "INTEGER", False), ("booking_id", "INTEGER", False),
            ("line_amount", "TEXT", True), ("note", "TEXT", True)]),
    ]
    for table in extra:
        repo.save_table_metadata(table)
        # 明细表用途（M4 主题分类依据）→ 构建时生成 count_rows 候选指标
        repo.save_table_purpose({
            "source_id": source_id, "snapshot_id": "snap-m4",
            "table_id": table["table_id"], "table_name": table["table_name"],
            "purpose": "detail", "rationale": "明细表", "alternatives": [],
            "evidence": [], "confidence": "medium",
            "inference_source": "rules", "review_status": "suggested",
            "created_at": "2026-10-29T00:00:00+00:00",
        })
    from tests.semantic_model_fixtures import (
        DEFAULT_VALIDATED_RELATIONSHIPS as _R,
    )
    from tests.semantic_model_fixtures import (
        seed_relationships,
    )
    rel_specs = list(_R) + [
        {"src": "detail_a", "src_cols": ["booking_id"], "tgt": "booking",
         "tgt_cols": ["id"], "cardinality": "many_to_one"},
        {"src": "detail_b", "src_cols": ["booking_id"], "tgt": "booking",
         "tgt_cols": ["id"], "cardinality": "many_to_one"},
        # 维度示例：booking(源)→guest(目标) many_to_one
    ]
    seed_relationships(repo, source_id, "snap-m4", base_catalog + extra,
                       rel_specs)
    # 业务库
    data_url = f"sqlite:///{tmp_path / 'biz.db'}"
    build_sqlite_data(data_url)
    # M4 模型：构建（纳入新关系）→ 确认 grain → 发布
    model_service = SemanticModelService(repo, _settings())
    model = model_service.build_model(source_id)
    grains = {
        t["topic_id"]: {
            "statement": f"一行代表一条{t['name']}记录",
            "keys": t["grain"]["keys"] or ["id"], "confirm": True}
        for t in model["topics"] if (t["grain"]["keys"]
                                     or t["grain"].get("basis"))}
    grains = {k: {**v, "keys": v["keys"] or ["id"]} for k, v in grains.items()}
    # 确认明细表指标（预聚合输入必须是 formal_metric_ids 成员）
    detail_metrics = {
        m["metric_id"]: {
            "action": "confirm", "time_basis": "自然日",
            "skip_status_filter": True}
        for m in model["metrics"]
        if m["aggregation"] == "count_rows"
        and m["entity_id"] in (f"{SCHEMA}.detail_a", f"{SCHEMA}.detail_b")}
    confirmed = model_service.revise_model(
        model["model_id"], model["version"],
        {"topic_grains": grains, "metrics": detail_metrics}, reviewer="dev")
    model_service.publish_model(
        confirmed["model_id"], confirmed["version"], reviewer="dev")
    published_model = model_service.get_model(
        confirmed["model_id"], confirmed["version"])
    assert published_model["formal_metric_ids"], "夹具必须包含正式指标"
    return {
        "repo": repo, "source_id": source_id, "model": published_model,
        "model_service": model_service, "data_url": data_url,
        "source_id_for_keys": source_id,
    }


def _table(source_id: str, name: str,
           columns: list[tuple[str, str, bool]]) -> dict[str, Any]:
    return {
        "table_id": f"snap-m4-{name}", "source_id": source_id,
        "snapshot_id": "snap-m4", "schema_name": SCHEMA, "table_name": name,
        "table_comment": None,
        "columns": [{"name": n, "data_type": t, "nullable": nullable}
                    for n, t, nullable in columns],
        "primary_key": [columns[0][0]], "foreign_keys": [], "indexes": [],
        "row_count_estimate": 100, "row_count_is_estimate": True,
    }


def detail_relation_key(env: dict[str, Any], table: str) -> str:
    return relationship_key(
        env["source_id"], SCHEMA, table, ["booking_id"],
        SCHEMA, "booking", ["id"])


def make_service(
    env: dict[str, Any],
    *,
    view_publisher_factory=None,
    es_publisher_factory=None,
    settings: Settings | None = None,
) -> DatasetService:
    """构造 DatasetService（SQLite 受控执行器 + 可注入发布器）。"""
    from app.adapters.dataset_executors import SQLiteControlledExecutor

    effective_settings = settings or _settings()

    attach = {SCHEMA: env["data_url"].replace("sqlite:///", "") + ".app",
              ARCHIVE: env["data_url"].replace("sqlite:///", "") + ".archive"}

    def executor_factory(payload, model, cfg, run_context=None):
        tables: dict[str, set[str]] = {}
        for entity in model.get("entities", []):
            source = entity.get("source") or {}
            key = f"{source.get('schema')}.{source.get('table')}".lower()
            tables[key] = {
                a["field"]["column"].lower()
                for a in entity.get("attributes", [])}
        return SQLiteControlledExecutor(
            env["data_url"], set(tables.keys()), tables, attach=attach,
            statement_budget=(run_context or {}).get("budget"),
            deadline_monotonic=(run_context or {}).get("deadline"),
            max_result_bytes=(run_context or {}).get("max_result_bytes"))

    return DatasetService(
        metadata_repository=env["repo"],
        semantic_models=env["model_service"],
        settings=effective_settings,
        executor_factory=executor_factory,
        view_publisher_factory=view_publisher_factory,
        es_publisher_factory=es_publisher_factory,
    )


def detail_view_definition(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "预订明细主题视图",
        "model_id": model["model_id"], "model_version": model["version"],
        "kind": "detail_view",
        "central_entity_id": f"{SCHEMA}.booking",
        "topic_id": f"topic.{SCHEMA}.booking",
        "grain_keys": ["id"], "grain_statement": "一行代表一次预订",
        "fields": [{"field": {"schema": SCHEMA, "table": "booking",
                              "column": "booking_no"},
                    "output_alias": "booking_no"}],
        "dimensions": [], "metrics": [], "preaggregations": [],
        "filters": [], "join_type": "left", "target": "view",
    }


def aggregate_definition(env: dict[str, Any],
                         model: dict[str, Any]) -> dict[str, Any]:
    """聚合数据集：中心 grain + 明细 A/B 预聚合（5×5/3 场景）。"""
    return {
        "name": "预订聚合数据集",
        "model_id": model["model_id"], "model_version": model["version"],
        "kind": "aggregate",
        "central_entity_id": f"{SCHEMA}.booking",
        "topic_id": f"topic.{SCHEMA}.booking",
        "grain_keys": ["id"], "grain_statement": "一行代表一次预订",
        "fields": [], "dimensions": [], "metrics": [],
        "preaggregations": [
            {"metric_id": _metric_id(model, "detail_a", "count_rows"),
             "output_alias": "detail_a_count",
             "relationship_key": detail_relation_key(env, "detail_a"),
             "scope_note": "按 booking_id 分组的明细 A 行数预聚合"},
            {"metric_id": _metric_id(model, "detail_b", "count_rows"),
             "output_alias": "detail_b_count",
             "relationship_key": detail_relation_key(env, "detail_b"),
             "scope_note": "按 booking_id 分组的明细 B 行数预聚合"},
        ],
        "filters": [], "join_type": "left", "target": "view",
    }


def _metric_id(model: dict[str, Any], table: str, aggregation: str) -> str:
    for metric in model.get("metrics", []):
        if metric.get("aggregation") == aggregation and \
                metric.get("entity_id") == f"{SCHEMA}.{table}":
            return metric["metric_id"]
    raise AssertionError(f"夹具缺少指标：{table}.{aggregation}")


def json_of(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
