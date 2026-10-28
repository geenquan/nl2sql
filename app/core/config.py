from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # 本地密钥从 .env 读取；部署时使用环境变量覆盖。禁止记录配置中的密钥。
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    app_name: str = "Enterprise Data Understanding NL2SQL"
    app_env: str = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    metadata_database_url: str = "sqlite:///./metadata.db"
    allow_source_db_writes: bool = False

    # M1 预算与本地凭证存储：扫描表数、采样行数、画像列数和错误记录条数都有硬上限。
    credential_store_path: str = "./.credential_store.json"
    profile_sample_rows: int = Field(default=500, gt=0, le=10000)
    scan_max_tables: int = Field(default=100, gt=0)
    profile_max_columns: int = Field(default=64, gt=0)
    scan_max_recorded_errors: int = Field(default=50, ge=1)
    # 追加固敏感列名关键词（小写子串匹配，JSON 数组）；内置规则见 app/security/masking.py。
    sensitive_column_keywords: list[str] = Field(default_factory=list)

    # M2 语义分析预算：默认不外发样本，只发送结构元数据与画像统计。
    semantics_max_columns_per_run: int = Field(default=40, gt=0)
    semantics_llm_max_calls: int = Field(default=2, ge=1)
    semantics_input_char_limit: int = Field(default=24000, gt=0)
    semantics_send_samples: bool = False
    semantics_search_default_limit: int = Field(default=20, gt=0, le=100)
    # 单次语义分析的模型调用总期限（秒）；请求超时不替代整次运行期限。
    semantics_llm_deadline_seconds: int = Field(default=120, gt=0)

    # M3 关系发现与验证预算：候选召回纯本地；数据验证显式触发、全程有界。
    relationship_max_candidates_per_run: int = Field(default=200, gt=0)
    relationship_topk_per_field: int = Field(default=5, gt=0)
    relationship_discovery_deadline_seconds: int = Field(default=60, gt=0)
    relationship_validation_max_candidates: int = Field(default=10, gt=0)
    relationship_validation_max_queries: int = Field(default=120, gt=0)
    relationship_query_timeout_ms: int = Field(default=15000, ge=100)
    relationship_validation_deadline_seconds: int = Field(default=300, gt=0)
    # 证据采样窗口行数；窗口未截断（行数<窗口）才记 exact 口径。
    relationship_sample_window_rows: int = Field(default=500, gt=0, le=10000)
    # 成本门槛：右端行数估计超过该值且连接列无索引时拒绝 JOIN 探测（降级 unknown）。
    relationship_exact_max_rows: int = Field(default=50000, gt=0)
    # 并发验证上限：当前实现为串行执行（=1 由执行模型直接保证；配置保留用于未来并行化）。
    relationship_max_concurrent_validations: int = Field(default=1, ge=1)
    # 自动 validated 的前向覆盖率阈值（exact 口径）；仅高覆盖率不能单独升级。
    relationship_coverage_threshold: float = Field(default=0.995, gt=0.5, le=1.0)
    relationship_path_max_hops: int = Field(default=3, ge=1, le=6)
    relationship_path_max_paths: int = Field(default=5, ge=1, le=50)
    relationship_path_max_nodes: int = Field(default=200, ge=1)
    relationship_path_deadline_seconds: int = Field(default=10, gt=0)

    # M4 语义模型：确定性规则构建 + 人工确认；不调用模型、不新增基础设施。
    semantic_model_max_hops: int = Field(default=2, ge=1, le=4)
    semantic_model_max_paths_per_pair: int = Field(default=3, ge=1, le=10)
    semantic_model_max_topics: int = Field(default=50, gt=0)
    semantic_model_build_deadline_seconds: int = Field(default=60, gt=0)

    # 系统业务库：与只读查询数据源独立，持久化接入仍待实现。
    biz_mysql_host: str = "127.0.0.1"
    biz_mysql_port: int = Field(default=3306, gt=0, le=65535)
    biz_mysql_db: str = "nl2sql_system"
    biz_mysql_user: str = ""
    biz_mysql_password: SecretStr = SecretStr("")

    # 源 MySQL（M1 消费这些配置；元数据库与源数据库分别配置）。
    mysql_host: str = Field(
        default="127.0.0.1", validation_alias=AliasChoices("DATA_MYSQL_HOST", "MYSQL_HOST")
    )
    mysql_port: int = Field(
        default=3306, gt=0, le=65535,
        validation_alias=AliasChoices("DATA_MYSQL_PORT", "MYSQL_PORT"),
    )
    mysql_database: str = Field(
        default="nl2sql_source", validation_alias=AliasChoices("DATA_MYSQL_DB", "MYSQL_DATABASE")
    )
    mysql_username: str = Field(
        default="readonly", validation_alias=AliasChoices("DATA_MYSQL_USER", "MYSQL_USERNAME")
    )
    mysql_password: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("DATA_MYSQL_PASSWORD", "MYSQL_PASSWORD")
    )
    mysql_charset: str = "utf8mb4"
    mysql_connect_timeout_seconds: int = Field(default=10, gt=0)
    mysql_read_timeout_seconds: int = Field(default=30, gt=0)

    # 宽表 Elasticsearch（M5 实现导出、索引映射和发布）。
    es_hosts: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:9200"])
    es_host: str | None = None
    es_username: str = Field(
        default="", validation_alias=AliasChoices("ES_USER", "ES_USERNAME")
    )
    es_password: SecretStr = SecretStr("")
    es_api_key: SecretStr = SecretStr("")
    es_index_prefix: str = "nl2sql_wide"
    es_request_timeout_seconds: int = Field(default=30, gt=0)
    es_verify_certs: bool = True
    es_ca_certs: str | None = None
    es_bulk_batch_size: int = Field(default=500, gt=0)

    # 仅加载配置；当前 API 尚未实现登录或 JWT 鉴权。
    login_enabled: bool = False
    jwt_secret: SecretStr = SecretStr("")

    # DeepSeek 的 OpenAI 兼容 API；M2 实现客户端及预算控制。
    deepseek_enabled: bool = False
    llm_provider: str = "deepseek"
    llm_model: str = Field(
        default="deepseek-flash", validation_alias=AliasChoices("DEEPSEEK_MODEL", "LLM_MODEL")
    )
    llm_api_base: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "LLM_API_BASE"),
    )
    llm_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("DEEPSEEK_API_KEY", "LLM_API_KEY")
    )
    llm_timeout_seconds: int = Field(default=60, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)

    @model_validator(mode="after")
    def resolve_es_hosts(self) -> "Settings":
        # 显式 ES_HOSTS 优先；只有单节点配置时转换成客户端通用的节点列表。
        if self.es_host and "es_hosts" not in self.model_fields_set:
            self.es_hosts = [self.es_host]
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
