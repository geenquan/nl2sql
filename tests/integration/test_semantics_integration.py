"""M2 真实链路集成测试：真实 MySQL 元数据（只读）+ 真实 DeepSeek 调用。

默认跳过；同时设置 NL2SQL_MYSQL_INTEGRATION=1 与 NL2SQL_SEMANTICS_INTEGRATION=1
且 `.env` 中 DEEPSEEK_ENABLED=true 时运行。小预算：仅扫描/分析一张表、
少量列；默认不外发样本；不对源库写入；不打印地址/密钥/样本。
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.adapters.mysql_connector import MySQLConnectionConfig, MySQLConnector
from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.llm.deepseek_client import DeepSeekClient
from app.profiling.profiler import DataProfiler
from app.security.credentials import FileCredentialStore
from app.services.scan_service import ScanService
from app.services.semantics_service import SemanticsService
from app.services.source_service import SourceService
from tests.fakes import FakeConnector  # noqa: F401  （确保测试替身不影响本文件）

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NL2SQL_MYSQL_INTEGRATION") != "1"
        or os.environ.get("NL2SQL_SEMANTICS_INTEGRATION") != "1",
        reason="需要真实 MySQL + DeepSeek；设置两个环境变量为 1 后运行",
    ),
]


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _connector_factory(settings: Settings):
    def factory(params: dict):
        return MySQLConnector(
            MySQLConnectionConfig(
                host=params["host"], port=params["port"], database=params["database"],
                username=params["username"],
                password=params.get("password") or SecretStr(""),
                charset=params.get("charset", settings.mysql_charset),
                connect_timeout_seconds=params.get(
                    "connect_timeout_seconds", settings.mysql_connect_timeout_seconds),
                read_timeout_seconds=params.get(
                    "read_timeout_seconds", settings.mysql_read_timeout_seconds),
            )
        )
    return factory


def test_real_scan_then_semantics_with_model(tmp_path: Path, settings: Settings) -> None:
    assert settings.deepseek_enabled, "DEEPSEEK_ENABLED 必须为 true 才能做真实模型验证"
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path/'meta.db'}")
    store = FileCredentialStore(tmp_path / "creds.json")
    factory = _connector_factory(settings)
    scan_service = ScanService(
        repo, repo, store, factory, DataProfiler(
            sensitive_keywords=settings.sensitive_column_keywords),
        sample_rows=50, max_tables=3, max_columns=32,
    )
    source_service = SourceService(repo, store, factory, settings)
    registered = source_service.register_default_source()
    assert registered["already_registered"] is False
    job = scan_service.run_scan(registered["source_id"])
    assert job["status"] in {"succeeded", "partial"}, job["errors"][:3]
    tables = scan_service.list_tables(registered["source_id"])
    assert tables, "真实库扫描后应有表目录"
    # 选列数最少的一张表做小预算真实模型分析
    target = min(tables, key=lambda t: len(t["columns"]))

    def llm_factory():
        return DeepSeekClient(
            api_base=settings.llm_api_base, api_key=settings.llm_api_key,
            model=settings.llm_model, timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries, max_output_tokens=settings.llm_max_tokens,
        )

    service = SemanticsService(
        metadata_repository=repo, llm_factory=llm_factory,
        settings=Settings(_env_file=None, deepseek_enabled=True,
                          semantics_max_columns_per_run=min(10, settings.semantics_max_columns_per_run),
                          llm_api_base=settings.llm_api_base, llm_api_key=settings.llm_api_key,
                          llm_model=settings.llm_model, llm_timeout_seconds=settings.llm_timeout_seconds,
                          llm_max_retries=settings.llm_max_retries,
                          llm_max_tokens=settings.llm_max_tokens),
    )
    run = service.analyze_table(registered["source_id"], target["table_id"],
                                column_limit=8, send_samples=False,
                                allow_partial=job["status"] == "partial")
    if job["status"] == "partial":
        assert run["input_complete"] is False  # 部分快照输入必须显式标记不完整
    assert run["mode"] == "model_assisted"
    assert run["status"] in {"succeeded", "partial"}
    assert run["llm_calls"] >= 1
    if run["status"] == "partial":
        # 模型故障/校验失败必须显式记录，不允许伪装成功
        assert run["errors"], "partial 必须携带错误明细"
        pytest.skip(f"真实模型调用未成功（原因已记录）: {[e['reason'] for e in run['errors']]}")
    assert run["model_reported"], "成功路径必须记录服务端返回的模型标识"
    semantics = repo.list_field_semantics(target["table_id"])
    assert semantics
    model_fields = [s for s in semantics if s["inference_source"] == "model"]
    assert model_fields, "成功路径应至少有模型来源的字段语义"
    # 证据可核验（模型路径的证据已由服务端校验通过才会保存）
    for record in model_fields:
        assert record["evidence"], record["column_name"]
        assert record["category"] != "unknown" or record["confidence"] == "low"
    # 用量如实记录（token 由服务端返回；无则 None）
    print(f"\n[真实模型] model={run['model_reported']}, calls={run['llm_calls']}, "
          f"tokens={run['llm_prompt_tokens']}/{run['llm_completion_tokens']}, "
          f"elapsed_ms={run['elapsed_ms']}")
    # 检索可用
    results = service.search_fields(registered["source_id"], target["table_name"] or "id", limit=5)
    assert results["method"] == "lexical"
