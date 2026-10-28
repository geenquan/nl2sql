"""M4 语义模型真实只读演示（hotel 库，小预算）。

运行前提：`.env` 中 DATA_MYSQL_* 指向可达只读账号。
用法：conda activate nl2sql && python scripts/m4_real_demo.py

安全与预算约定：
- 元数据库与凭证文件写临时目录（结束即清理），不触碰项目根目录已有产物；
- 全程只读：扫描 ≤5 表 × 采样 50 行；M3 关系验证 ≤3 候选 × ≤30 条 SQL
  语句（逐语句硬预算）；不调用 DeepSeek、不外发样本、不访问 ES；
- 人工输入标记为“开发演示假设”，不宣称获得业务确认；
- 不为演示强行确认证据不足的关系；输出不含业务原始值与凭证。
"""

import tempfile
from pathlib import Path

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.mysql_probe import MySQLRelationshipProbe
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.security.credentials import FileCredentialStore
from app.services.relationship_service import RelationshipService
from app.services.scan_service import ScanService
from app.services.semantic_model_service import SemanticModelService
from app.services.semantics_service import SemanticsService
from app.services.source_service import SourceService

SCAN_MAX_TABLES = 5
SCAN_SAMPLE_ROWS = 50
RELATION_CANDIDATES = 3
RELATION_SQL_BUDGET = 45
RELATION_WINDOW_ROWS = 200  # 验证窗口覆盖小表全量 → exact 口径（扫描采样独立）


def _connector_factory(settings: Settings):
    def factory(params: dict) -> MySQLConnector:
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
    def factory(params: dict) -> MySQLRelationshipProbe:
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
    with tempfile.TemporaryDirectory(prefix="m4-demo-") as raw_dir:
        work = Path(raw_dir)
        store = FileCredentialStore(work / ".credential_store.json")
        repo = SQLiteMetadataRepository(f"sqlite:///{work / 'meta.db'}")
        connector_factory = _connector_factory(settings)
        probe_factory = _probe_factory(settings)
        try:
            print("== M4 语义模型真实只读演示（临时元数据/凭证目录）==")
            print(f"预算：扫描 ≤{SCAN_MAX_TABLES} 表 × 采样 {SCAN_SAMPLE_ROWS} 行；"
                  f"M3 验证 ≤{RELATION_CANDIDATES} 候选 × ≤{RELATION_SQL_BUDGET} 条 SQL；"
                  "M4 构建为确定性规则（纯本地，不调用模型）\n")

            source_service = SourceService(
                source_repository=repo, credential_store=store,
                connector_factory=connector_factory, settings=settings)
            source_id = source_service.register_default_source()["source_id"]
            scan_service = ScanService(
                source_repository=repo, metadata_repository=repo,
                credential_store=store, connector_factory=connector_factory,
                sample_rows=SCAN_SAMPLE_ROWS, max_tables=SCAN_MAX_TABLES,
                max_columns=32)
            scan = scan_service.run_scan(source_id)
            print(f"[1] 扫描 status={scan['status']} "
                  f"scanned={scan['tables_scanned']}/{scan['tables_planned']}")
            allow_partial = scan["status"] == "partial"

            relationship_service = RelationshipService(
                metadata_repository=repo, source_repository=repo,
                credential_store=store, connector_factory=connector_factory,
                probe_factory=probe_factory,
                settings=Settings(
                    _env_file=None,
                    relationship_validation_max_candidates=RELATION_CANDIDATES,
                    relationship_validation_max_queries=RELATION_SQL_BUDGET,
                    relationship_sample_window_rows=RELATION_WINDOW_ROWS,
                ))
            # 规则模式 M2 语义（不调用模型；演示流程的“规则语义结果”步骤）
            semantics_service = SemanticsService(
                metadata_repository=repo, llm_factory=None,
                settings=Settings(_env_file=None,
                                  semantics_max_columns_per_run=32))
            analyzed = 0
            for table in repo.list_table_metadata(
                    source_id, snapshot_id=scan["snapshot_id"]):
                semantics_service.analyze_table(
                    source_id, table["table_id"], allow_partial=allow_partial)
                analyzed += 1
            print(f"[2a] M2 规则语义：{analyzed} 表（rules_only，未调用模型）")

            discovery = relationship_service.discover_relationships(
                source_id, allow_partial=allow_partial)
            print(f"[2b] M3 召回 found={discovery['candidates_new']}")
            validation = relationship_service.validate_batch(
                source_id, limit=RELATION_CANDIDATES, allow_partial=allow_partial)
            validated_count = sum(
                1 for r in validation.get("results", [])
                if r.get("status") == "validated")
            del validated_count
            print(f"[3] M3 验证 status={validation['status']} "
                  f"validated={validation['candidates_validated']} "
                  f"needs_review={validation['candidates_needs_review']} "
                  f"sql={validation['queries_used']}（窗口 {RELATION_WINDOW_ROWS}）")
            if validation["candidates_validated"] == 0:
                print("    真实小预算下无自动 validated 关系：M4 模型将不含"
                      "正式关系/主题——如实阻断，不强行确认")

            model_service = SemanticModelService(repo, Settings(_env_file=None))
            model = model_service.build_model(
                source_id, allow_partial=allow_partial)
            print(f"[4] 构建草稿 v{model['version']}："
                  f"实体 {len(model['entities'])} / 主题 {len(model['topics'])} / "
                  f"关系 {len(model['relationships'])} / "
                  f"候选指标 {len(model['metrics'])}（全部 suggested）")
            sample_topic = next(
                (t for t in model["topics"] if t["grain"]["keys"]), None)
            if sample_topic is not None:
                print(f"    示例主题 {sample_topic['name']}: grain 键="
                      f"{sample_topic['grain']['keys']}（依据 "
                      f"{sample_topic['grain']['basis']}，业务描述待人工确认）")

            # 人工明确 grain 与一个指标口径（开发演示假设，非业务确认）
            if sample_topic is not None:
                confirmable = {
                    t["topic_id"]: {
                        "statement": f"【开发演示假设】一行代表一条{t['name']}记录",
                        "keys": t["grain"]["keys"], "confirm": True}
                    for t in model["topics"] if t["grain"]["keys"]}
                metric = next(
                    (m for m in model["metrics"]
                     if m["aggregation"] == "count_rows"), None)
                changes: dict = {"topic_grains": confirmable}
                if metric is not None:
                    changes["metrics"] = {metric["metric_id"]: {
                        "action": "confirm", "time_basis": "【演示假设】自然日",
                        "skip_status_filter": True}}
                revised = model_service.revise_model(
                    model["model_id"], model["version"], changes,
                    reviewer="local-dev", note="开发演示假设（非业务确认）")
                print(f"[5] 人工修订 v{revised['version']}：grain/指标口径已按"
                      "【开发演示假设】确认")

                report = model_service.validate_model(
                    revised["model_id"], revised["version"])
                print(f"[6] 检查 valid={report['valid']} "
                      f"issues={report['issues'] or '无'}")
                if report["valid"]:
                    model_service.publish_model(
                        revised["model_id"], revised["version"],
                        reviewer="local-dev", note="演示发布")
                    published = model_service.get_published(source_id)
                    print(f"[7] 发布指针 → v{published['version']}；"
                          f"正式指标集合={len(revised['formal_metric_ids'])} 个")
                    exported = model_service.export_model(
                        revised["model_id"], revised["version"],
                        export_format="yaml")
                    print(f"[8] YAML 导出 {len(exported['content'])} 字符；"
                          "JSON 导出一致性见离线测试")
                    next_rev = model_service.revise_model(
                        revised["model_id"], revised["version"],
                        {"topic_names": {
                            sample_topic["topic_id"]: "演示主题改名"}},
                        reviewer="local-dev")
                    diff = model_service.diff_models(
                        revised["model_id"], revised["version"],
                        next_rev["version"])
                    print(f"[9] 新版本 v{next_rev['version']}；差异 topics.changed="
                          f"{diff['topics']['changed']}")
                    model_service.rollback_model(
                        revised["model_id"], revised["version"],
                        reviewer="local-dev", reason="演示回滚")
                    rolled = model_service.get_published(source_id)["version"]
                    print(f"[10] 回滚 → v{rolled}")
                else:
                    print("[7] 检查未通过：如实保留草稿（不强行发布）")
            print("\n---- 以下为合成夹具演示（虚构数据；真实库小预算无法覆盖的"
                  "完整流程在此清楚区分展示）----")
            synthetic_demo()

            print("\n== 演示结束（未打印业务原始值/凭证；人工输入均为开发演示假设）==")
        finally:
            repo.close()


def synthetic_demo() -> None:
    """合成夹具上的完整生命周期：构建→确认→检查→发布→改版→差异→回滚。"""
    import sys

    from app.core.config import Settings as _Settings

    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    # 需先调整 sys.path 再导入夹具（此处非顶层导入）
    from tests.semantic_model_fixtures import DEFAULT_VALIDATED_RELATIONSHIPS as _RELS
    from tests.semantic_model_fixtures import seed_m4_environment as _seed

    with tempfile.TemporaryDirectory(prefix="m4-syn-") as raw:
        repo = SQLiteMetadataRepository(f"sqlite:///{Path(raw) / 's.db'}")
        try:
            source_id = repo.create_source({
                "name": "synthetic", "dialect": "mysql", "host": "h", "port": 1,
                "database": "d", "username": "u", "credential_ref": "c"})
            _seed(repo, source_id, relationship_specs=_RELS)
            service = SemanticModelService(repo, _Settings(_env_file=None))
            model = service.build_model(source_id)
            print(f"[S1] 构建：实体 {len(model['entities'])} / 主题 "
                  f"{len(model['topics'])} / 关系 {len(model['relationships'])} / "
                  f"候选指标 {len(model['metrics'])}")
            grains = {
                t["topic_id"]: {
                    "statement": f"【开发演示假设】一行代表一条{t['name']}记录",
                    "keys": t["grain"]["keys"], "confirm": True}
                for t in model["topics"] if t["grain"]["keys"]}
            revised = service.revise_model(
                model["model_id"], model["version"], {"topic_grains": grains},
                reviewer="local-dev")
            print(f"[S2] 确认 grain → v{revised['version']}")
            report = service.validate_model(
                revised["model_id"], revised["version"])
            print(f"[S3] 检查 valid={report['valid']}")
            if not report["valid"]:
                print(f"    issues={report['issues'][:3]}")
                return
            service.publish_model(revised["model_id"], revised["version"],
                                  reviewer="local-dev", note="演示发布")
            exported = service.export_model(
                revised["model_id"], revised["version"], export_format="yaml")
            next_version = service.revise_model(
                revised["model_id"], revised["version"],
                {"topic_names": {
                    model["topics"][0]["topic_id"]: "演示改名"}},
                reviewer="local-dev")
            diff = service.diff_models(revised["model_id"], revised["version"],
                                       next_version["version"])
            service.rollback_model(revised["model_id"], revised["version"],
                                   reviewer="local-dev", reason="演示回滚")
            print(f"[S4] 发布→YAML 导出 {len(exported['content'])} 字符→"
                  f"改版 v{next_version['version']}→差异 topics.changed="
                  f"{len(diff['topics']['changed'])}→回滚至 "
                  f"v{service.get_published(source_id)['version']}")
        finally:
            repo.close()


if __name__ == "__main__":
    main()
