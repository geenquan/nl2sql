"""M3 真实 MySQL 只读验证演示（hotel 库，授权只读）。

运行前提：`.env` 中 DATA_MYSQL_* 指向可达只读账号。
用法：conda activate nl2sql && python scripts/m3_real_validation.py

安全与预算约定：
- 元数据库与凭证文件写入临时目录（结束即清理），不改写项目根目录已有
  metadata.db / .credential_store.json，不覆盖任何用户已有审核结论；
- 全程只读：仅系统构造的元数据/采样/聚合查询；不建表、不写入、
  不修改结构与权限；验证前先报告预算；
- 输出只含聚合指标与标识（覆盖率/基数/状态/运行计数），不打印任何
  真实匹配值、地址或凭证；
- 失败、跳过与未完成项如实输出，不为演示降低验证门槛。
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
from app.services.source_service import SourceService

SCAN_MAX_TABLES = 20      # 小范围扫描预算
SCAN_SAMPLE_ROWS = 200    # 画像采样预算
VALIDATE_CANDIDATES = 5   # 验证候选数预算
VALIDATE_QUERIES = 120    # 验证总 SQL 语句预算（结构核对 information_schema 查询
                          # 与窗口/JOIN 聚合逐语句计数，含失败尝试）
WINDOW_ROWS = 200         # 证据窗口


def build_connector_factory(settings: Settings):
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


def build_probe_factory(settings: Settings):
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
    with tempfile.TemporaryDirectory(prefix="m3-demo-") as raw_dir:
        work = Path(raw_dir)
        store = FileCredentialStore(work / ".credential_store.json")
        repo = SQLiteMetadataRepository(f"sqlite:///{work / 'meta.db'}")
        connector_factory = build_connector_factory(settings)
        try:
            print("== M3 真实只读验证演示（临时元数据/凭证目录）==")
            print(f"预算：扫描 ≤{SCAN_MAX_TABLES} 表 × 采样 {SCAN_SAMPLE_ROWS} 行；"
                  f"验证 ≤{VALIDATE_CANDIDATES} 候选 × ≤{VALIDATE_QUERIES} 条 SQL 语句"
                  f"（结构核对+聚合逐语句计数；窗口 {WINDOW_ROWS} 行；并发额度=进程内共享）\n")

            # 1) 注册默认源（DATA_MYSQL_*）并做小范围只读扫描
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
                  f"planned={scan['tables_planned']} scanned={scan['tables_scanned']} "
                  f"failed={scan['tables_failed']} skipped={scan['tables_skipped']}")

            # 2) 候选召回（纯本地）
            demo_settings = Settings(
                _env_file=None,
                relationship_validation_max_candidates=VALIDATE_CANDIDATES,
                relationship_validation_max_queries=VALIDATE_QUERIES,
                relationship_sample_window_rows=WINDOW_ROWS,
            )
            relationship_service = RelationshipService(
                metadata_repository=repo, source_repository=repo,
                credential_store=store, connector_factory=connector_factory,
                probe_factory=build_probe_factory(settings),
                settings=demo_settings)
            allow_partial = scan["status"] == "partial"
            discovery = relationship_service.discover_relationships(
                source_id, allow_partial=allow_partial)
            print(f"[2] 召回 status={discovery['status']} "
                  f"found={discovery['candidates_found']} "
                  f"new={discovery['candidates_new']} "
                  f"input_complete={discovery['input_complete']} "
                  f"notes={discovery['notes'] or '无'}")

            # 3) 有界只读验证（前 5 个高分候选）
            validation = relationship_service.validate_batch(
                source_id, limit=VALIDATE_CANDIDATES, allow_partial=allow_partial)
            print(f"[3] 验证 status={validation['status']} "
                  f"validated={validation['candidates_validated']} "
                  f"needs_review={validation['candidates_needs_review']} "
                  f"stale={validation['candidates_stale_marked']} "
                  f"skipped={validation['candidates_skipped']} "
                  f"sql_statements={validation['queries_used']}")
            for result in validation["results"]:
                print(f"    - {result['relationship_key'][:12]} → {result['status']}"
                      f"｜阻断：{result.get('blocking') or '无'}")

            # 4) 证据详情（聚合口径示例）
            for record in relationship_service.list_relationships(source_id):
                if record.get("evidence"):
                    forward = record["evidence"]["coverage"]["forward"]
                    cardinality = record["evidence"].get(
                        "cardinality_observed", {}).get("cardinality")
                    print(f"[4] 证据示例 {record['source_table']}."
                          f"{','.join(record['source_columns'])} → "
                          f"{record['target_table']}.{','.join(record['target_columns'])}："
                          f"前向覆盖={forward.get('ratio')}（口径 {forward.get('basis')}，"
                          f"分母 {forward.get('denominator')}），"
                          f"fanout={forward.get('fanout_ratio')}，基数={cardinality}，"
                          f"状态={record['status']}")
                    break

            # 5) 人工审核演示：确认一条（如有 validated/needs_review）
            pending = next(
                (r for r in relationship_service.list_relationships(source_id)
                 if r["status"] in ("validated", "needs_review")), None)
            if pending is not None:
                key = pending["relationship_key"]
                revision = relationship_service.submit_review(
                    key, action="confirm",
                    expected_revision=pending["revision"],
                    reviewer="local-dev", note="演示确认（本地开发输入）")
                print(f"[5] 人工确认 {key[:12]} → revision={revision} "
                      f"生效状态={relationship_service.get_relationship(key)['status']}")

                # 6) 标记 stale → 默认路径排除 → 重验恢复
                relationship_service.mark_stale(key, reason="演示过期",
                                                expected_revision=revision)
                effective = relationship_service.get_relationship(key)
                print(f"[6] 标记 stale 后生效状态={effective['status']}")
                rerun = relationship_service.revalidate(source_id, key)
                print(f"    重验 status={rerun['status']} → "
                      f"生效状态={relationship_service.get_relationship(key)['status']}")

            # 7) 图与路径（Mermaid 摘要 + 路径预算）
            graph = relationship_service.graph(source_id, mermaid=True)
            print(f"[7] 图：节点 {len(graph['nodes'])} / 边 {len(graph['edges'])}"
                  f"（状态分布 "
                  + str({s: sum(1 for e in graph['edges'] if e['status'] == s)
                         for s in {e['status'] for e in graph['edges']}}) + "）")
            validated_edges = [e for e in graph["edges"] if e["status"] == "validated"]
            if len(validated_edges) >= 2:
                from_table = validated_edges[0]["source"]["table_id"]
                to_candidates = [
                    e["target"]["table_id"] for e in validated_edges
                    if e["source"]["table_id"] != from_table]
                if to_candidates:
                    paths = relationship_service.paths(
                        source_id, from_table, to_candidates[0], max_hops=3)
                    print(f"    路径搜索 {from_table[:8]}→{to_candidates[0][:8]}："
                          f"找到 {paths['path_count']} 条（预算 max_hops=3）")
            print("\n== 演示结束（未打印任何业务原始值/凭证）==")
        finally:
            repo.close()


if __name__ == "__main__":
    main()
