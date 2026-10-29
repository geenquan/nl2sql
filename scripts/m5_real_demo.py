"""M5 真实只读验证演示（hotel 库，小预算）。

运行前提：`.env` 中 DATA_MYSQL_* 指向可达只读账号。
用法：conda activate nl2sql && python scripts/m5_real_demo.py

安全与预算约定：
- 临时元数据库/凭证（结束清理）；不改源库结构、不写入、不创建远端视图；
- 扫描 ≤5 表 × 采样 50 行；M2 规则语义；M3 验证 ≤3 候选；
- M4 构建纯本地；M5 预览 ≤20 行（只 SELECT）；
- 质量验证 ≤50 条 SQL（逐语句硬预算+AST 白名单）；
- 不调用 DeepSeek、不访问 ES、不发布视图/ES（目标默认关闭，如实报告
  "实现已测试，真实发布未验证"）；
- 输出不含业务原始值（预览经脱敏）与凭证。
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import (
    SQLiteMetadataRepository,
)
from app.core.config import Settings
from app.security.credentials import FileCredentialStore
from app.services.dataset_service import DatasetService
from app.services.relationship_service import RelationshipService
from app.services.scan_service import ScanService
from app.services.semantic_model_service import SemanticModelService
from app.services.semantics_service import SemanticsService
from app.services.source_service import SourceService

SCAN_MAX_TABLES = 5
SCAN_SAMPLE_ROWS = 50
M3_CANDIDATES = 3
M3_SQL_BUDGET = 45
M3_WINDOW = 200
M5_PREVIEW_ROWS = 20
M5_VALIDATION_ROWS = 50
M5_VALIDATION_SQL = 50


def _connector_factory(settings: Settings):
    def factory(params: dict):
        return MySQLConnector(
            MySQLConnectionConfig(
                host=params["host"], port=int(params["port"]),
                database=params["database"], username=params["username"],
                password=params.get("password") or settings.mysql_password,
                charset=settings.mysql_charset,
                connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
                read_timeout_seconds=settings.mysql_read_timeout_seconds,
            ),
            statement_counter=params.get("statement_counter"))
    return factory


def _probe_factory(settings: Settings):
    def factory(params: dict):
        return MySQLRelationshipProbe(
            MySQLConnectionConfig(
                host=params["host"], port=int(params["port"]),
                database=params["database"], username=params["username"],
                password=params.get("password") or settings.mysql_password,
                charset=settings.mysql_charset,
                connect_timeout_seconds=settings.mysql_connect_timeout_seconds,
                read_timeout_seconds=settings.mysql_read_timeout_seconds,
            ),
            statement_counter=params.get("statement_counter"))
    return factory


def main() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory(prefix="m5-demo-") as raw:
        work = Path(raw)
        store = FileCredentialStore(work / ".credential_store.json")
        repo = SQLiteMetadataRepository(f"sqlite:///{work / 'meta.db'}")
        connector_factory = _connector_factory(settings)
        probe_factory = _probe_factory(settings)
        try:
            print("== M5 真实只读验证演示（临时元数据/凭证目录）==")
            print(f"预算：扫描 ≤{SCAN_MAX_TABLES} 表 × {SCAN_SAMPLE_ROWS} 行；"
                  f"M3 验证 ≤{M3_CANDIDATES} 候选 × ≤{M3_SQL_BUDGET} SQL；"
                  f"M5 预览 ≤{M5_PREVIEW_ROWS} 行、质量 ≤{M5_VALIDATION_SQL} SQL"
                  "（逐语句硬预算+AST 白名单）\n")

            # 1) 注册源 → 扫描 → M2 规则语义 → M3 验证
            source_service = SourceService(
                source_repository=repo, credential_store=store,
                connector_factory=connector_factory, settings=settings)
            source_id = source_service.register_default_source()["source_id"]
            scan = ScanService(
                source_repository=repo, metadata_repository=repo,
                credential_store=store, connector_factory=connector_factory,
                sample_rows=SCAN_SAMPLE_ROWS, max_tables=SCAN_MAX_TABLES,
                max_columns=32).run_scan(source_id)
            allow_partial = scan["status"] == "partial"
            print(f"[1] 扫描 status={scan['status']} "
                  f"scanned={scan['tables_scanned']}/{scan['tables_planned']}")

            semantics_service = SemanticsService(
                metadata_repository=repo, llm_factory=None,
                settings=Settings(_env_file=None,
                                  semantics_max_columns_per_run=32))
            for table in repo.list_table_metadata(
                    source_id, snapshot_id=scan["snapshot_id"]):
                semantics_service.analyze_table(
                    source_id, table["table_id"], allow_partial=allow_partial)
            print("[2] M2 规则语义完成（rules_only，未调用模型）")

            relationship_service = RelationshipService(
                metadata_repository=repo, source_repository=repo,
                credential_store=store, connector_factory=connector_factory,
                probe_factory=probe_factory,
                settings=Settings(
                    _env_file=None,
                    relationship_validation_max_candidates=M3_CANDIDATES,
                    relationship_validation_max_queries=M3_SQL_BUDGET,
                    relationship_sample_window_rows=M3_WINDOW))
            relationship_service.discover_relationships(
                source_id, allow_partial=allow_partial)
            validation = relationship_service.validate_batch(
                source_id, limit=M3_CANDIDATES, allow_partial=allow_partial)
            print(f"[3] M3 验证 validated={validation['candidates_validated']} "
                  f"needs_review={validation['candidates_needs_review']} "
                  f"sql={validation['queries_used']}")
            if validation["candidates_validated"] == 0:
                print("    真实小预算下无 validated 关系 → M4/M5 如实阻断"
                      "（不强行确认）")
                _synthetic_demo()
                return

            # 2) M4 构建 → 发布
            model_service = SemanticModelService(
                repo, Settings(_env_file=None))
            model = model_service.build_model(
                source_id, allow_partial=allow_partial)
            grains = {
                t["topic_id"]: {
                    "statement": f"【开发演示假设】一行代表一条{t['name']}记录",
                    "keys": t["grain"]["keys"], "confirm": True}
                for t in model["topics"] if t["grain"]["keys"]}
            if not grains:
                print("[4] 无可确认 grain 主题 → 如实阻断")
                _synthetic_demo()
                return
            confirmed = model_service.revise_model(
                model["model_id"], model["version"],
                {"topic_grains": grains}, reviewer="local-dev")
            model_service.publish_model(
                confirmed["model_id"], confirmed["version"],
                reviewer="local-dev")
            published_model = model_service.get_model(
                confirmed["model_id"], confirmed["version"])
            print(f"[4] M4 模型 v{published_model['version']} 发布；"
                  f"usable={published_model['availability']['usable_as_current']}")

            # 3) M5 数据集（明细主题视图）
            def executor_factory(payload, mdl, cfg):
                from app.adapters.dataset_executors import (
                    MySQLControlledExecutor,
                )
                tables = {}
                for entity in mdl.get("entities", []):
                    src = entity.get("source") or {}
                    key = f"{src.get('schema')}.{src.get('table')}".lower()
                    tables[key] = {
                        a["field"]["column"].lower()
                        for a in entity.get("attributes", [])}
                source = repo.get_source(payload["source_id"])
                password = store.get(source["credential_ref"]) or ""
                return MySQLControlledExecutor(
                    host=source["host"], port=int(source["port"]),
                    database=source["database"], username=source["username"],
                    password=password, allowed_tables=set(tables.keys()),
                    allowed_columns=tables)

            dataset_service = DatasetService(
                metadata_repository=repo, semantic_models=model_service,
                settings=Settings(
                    _env_file=None, dataset_preview_rows=M5_PREVIEW_ROWS,
                    dataset_validation_rows=M5_VALIDATION_ROWS),
                executor_factory=executor_factory)
            topic = next(
                (t for t in published_model["topics"]
                 if t["grain"]["keys"] and t["central_entity_id"]), None)
            if topic is None:
                print("[5] 无可用主题 → 如实阻断")
                _synthetic_demo()
                return
            entity = next(
                e for e in published_model["entities"]
                if e["entity_id"] == topic["central_entity_id"])
            source_ref = entity["source"]
            definition = {
                "name": f"{topic['name']}明细视图",
                "model_id": published_model["model_id"],
                "model_version": published_model["version"],
                "topic_id": topic["topic_id"], "kind": "detail_view",
                "central_entity_id": topic["central_entity_id"],
                "grain_keys": list(topic["grain"]["keys"]),
                "grain_statement": "【演示假设】" + (
                    topic["grain"]["statement"] or "一行代表一条记录"),
                "fields": [{
                    "field": {"schema": source_ref["schema"],
                              "table": source_ref["table"],
                              "column": c},
                    "output_alias": c}
                    for c in (entity.get("primary_key") or [])[:2]],
                "dimensions": [], "metrics": [], "preaggregations": [],
                "filters": [], "target": "view",
            }
            dataset = dataset_service.create_dataset(
                source_id, definition)
            print(f"[5] 数据集 {dataset['dataset_id']} v{dataset['version']} "
                  f"创建（status={dataset['status']}）")
            result = dataset_service.validate(
                dataset["dataset_id"], dataset["version"])
            print(f"[6] 质量验证 status={result['status']}")
            for check in (result["quality"] or {}).get("checks", []):
                detail = (
                    f" actual={check.get('actual')}"
                    f"/denominator={check.get('denominator')}"
                    if check.get("denominator") is not None else "")
                print(f"    - {check['check']}: {check['outcome']}{detail}")
            if result["status"] == "ready":
                preview = dataset_service.preview(
                    dataset["dataset_id"], dataset["version"])
                print(f"[7] 预览 {preview['row_count']} 行"
                      f"（truncated={preview['truncated']}，脱敏展示）")
                try:
                    dataset_service.publish(
                        dataset["dataset_id"], dataset["version"],
                        operator="local-dev")
                    print("[8] 发布成功")
                except ValueError as exc:
                    print(f"[8] 发布阻断（如实）：{str(exc)[:60]}")
            else:
                print("[7] 质量未通过 → 不发布（如实）")
            _synthetic_demo()
        finally:
            repo.close()


def _synthetic_demo() -> None:
    """合成夹具完整闭环（清楚区分：真实不可覆盖的流程在此演示）。"""
    import importlib.util

    print("\n---- 以下为合成夹具演示（虚构数据；完整发布闭环在此清楚区分）----")
    spec = importlib.util.spec_from_file_location(
        "dataset_fixtures",
        PROJECT_ROOT / "tests" / "dataset_fixtures.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    with tempfile.TemporaryDirectory(prefix="m5-syn-") as raw:
        env = fixture.seed_environment(Path(raw))
        from app.adapters.dataset_executors import SQLiteViewPublisher
        from app.core.config import Settings

        base = env["data_url"].replace("sqlite:///", "")
        attach = {"app": f"{base}.app", "archive": f"{base}.archive"}
        publisher = SQLiteViewPublisher(env["data_url"], attach=attach)
        service = fixture.make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=Settings(_env_file=None,
                              dataset_view_publish_enabled=True,
                              dataset_view_target_schema="app",
                              dataset_validation_rows=1000))
        definition = fixture.aggregate_definition(env, env["model"])
        dataset = service.create_dataset(env["source_id"], definition)
        result = service.validate(dataset["dataset_id"], dataset["version"])
        print(f"[S1] 5×(5..1)/3 聚合数据集：{result['status']}")
        published = service.publish(
            dataset["dataset_id"], dataset["version"], operator="dev")
        print(f"[S2] 视图发布：{published['view']}")
        preview = service.preview(
            dataset["dataset_id"], dataset["version"])
        counts = {str(r["id"]): str(r["detail_a_count"])
                  for r in preview["rows"]}
        print(f"[S3] 预览行数={preview['row_count']}"
              f"（明细 A 计数 {counts}，无膨胀）")


if __name__ == "__main__":
    main()
