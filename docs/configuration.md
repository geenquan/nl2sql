# 运行环境与配置

## Conda 环境

项目以现有 Conda 环境 `nl2sql` 为准，实际 Python 版本为 **3.13.18**，无需降级到 3.12。
解释器路径为 `/Users/apple/miniconda3/envs/nl2sql/bin/python`。
仅当环境不存在时，从项目根目录创建：

```bash
conda env create -f environment.yml
conda activate nl2sql
python --version
```

已有环境时不要重复创建，激活后确认版本为 `3.13.18`，再执行
`python -m pip install -e ".[dev]"`。PyCharm 项目解释器也应选择该 Conda 环境。
本轮已在现有环境安装项目及 `[dev]` 依赖，无需因历史缺包描述再次创建环境。
`pyproject.toml` 最低版本为 3.13，Ruff 目标版本为 `py313`，
`environment.yml` 指定 Python 3.13.18 并通过 pip 安装本地项目；它是可重建的环境定义，不是全部传递依赖的锁文件。

## 配置入口与加载规则

- 根目录 `.env`：用户已更新的本机实际配置，以现有内容为准，不覆盖、不回显。
- 根目录 `.env.example`：用户已更新的参考模板，本轮保留原内容；分享前确认凭证已脱敏。
- `app/core/config.py`：配置字段、类型、默认值和校验；通过 `get_settings()` 获取。

首次缺少 `.env` 时执行 `cp .env.example .env`，已有配置不要覆盖。
读取优先级为显式传给 `Settings` 的参数、进程环境变量、项目根目录 `.env`、字段默认值。
`.env` 使用绝对项目路径定位，切换工作目录不会改变配置文件位置；
`METADATA_DATABASE_URL` 中 SQLite 的相对路径仍以进程工作目录为准，因此从项目根目录启动。
配置在进程内缓存，修改后需要重启服务；测试切换配置可调用 `get_settings.cache_clear()`。
凭证字段使用 `SecretStr`，未来适配器仅在调用外部服务时用 `.get_secret_value()` 取值，禁止记录明文。
`.env` 已加入 `.gitignore`，不应提交；部署时可通过环境变量注入凭证。
下表为代码默认值或字段用途，不是对当前 `.env` 实际连接信息的记录。

## 系统业务库 MySQL

`BIZ_MYSQL_HOST`、`BIZ_MYSQL_PORT`、`BIZ_MYSQL_DB`、`BIZ_MYSQL_USER`、
`BIZ_MYSQL_PASSWORD` 对应 `Settings.biz_mysql_*`，用于用户、会话和图表等系统业务数据。
当前只加载这些字段，尚未实现业务表、迁移、连接和持久化。
`METADATA_DATABASE_URL` 仍独立控制元数据仓储，默认 SQLite；不会因为配置了业务 MySQL 自动切库。

## M1 元数据存储、凭证与扫描预算（已实现）

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `METADATA_DATABASE_URL` | `sqlite:///./metadata.db`，元数据仓储（数据源、扫描任务、表结构、画像快照） |
| `CREDENTIAL_STORE_PATH` | `./.credential_store.json`，本地凭证文件（0600 权限明文 JSON，仅限本地开发） |
| `PROFILE_SAMPLE_ROWS` | `500`，字段画像单表采样行数上限（1–10000） |
| `SCAN_MAX_TABLES` | `100`，单次扫描最多处理的表数，超出记 `skipped` |
| `PROFILE_MAX_COLUMNS` | `64`，单表最多计算画像的列数，超出列仅保留结构元数据 |
| `SCAN_MAX_RECORDED_ERRORS` | `50`，扫描错误明细最多记录条数 |
| `SENSITIVE_COLUMN_KEYWORDS` | `[]`，追加的固敏感列名关键词（JSON 数组，小写子串匹配） |

说明：

- 元数据默认落在项目根目录 `metadata.db`（已 gitignore）；从其他目录启动时 SQLite
  相对路径以进程工作目录为准。
- **凭证文件可靠性**：写入走「独占文件锁（POSIX flock）→ 锁内重读最新 → 同目录临时文件
  fsync → 原子替换」，读操作每次重读文件，多实例/多进程不丢更新。**文件状态语义**：
  文件不存在＝空存储（允许首次初始化）；合法空 JSON 对象 `{}`＝空存储；已存在的零字节、
  纯空白、非法 JSON、非对象结构或键/值非字符串的文件一律视为损坏——get/put/delete 都
  返回固定安全错误（HTTP 503），原文件字节保持不变，不自动修复或删除。
  读取、目录创建、锁文件打开、加锁、写入/fsync/替换等预期文件系统故障统一转换为
  `CredentialStoreError`（消息仅含异常类型名与路径），API 统一 503；扫描流程记为
  failed + credential_store_error，不会返回扫描成功。仍是明文存储、无加密审计轮换，
  仅限本地开发；跨进程锁依赖 flock，Windows 等平台不在支持范围（M8 接入密钥管理服务）。
- **扫描错误消息**：未预期异常使用固定安全文案（异常类型名 + 阶段 + schema/table +
  job_id，“错误详情已抑制”），不携带异常文本或业务样本，也不转移至日志/异常链；
  `GET /scans/{id}` 返回同样内容。
- **目录发布规则**：默认目录＝最近一次完整成功（succeeded）快照；无完整成功时回退最近
  一次部分成功（partial）快照并标记 `complete=false`；failed/running 永不发布。部分成功、
  权限失败或预算截断缺失的表不当作确认删除；完整成功扫描确认空 Schema 时发布空目录。
  历史快照全部保留，`GET /sources/{id}/tables?snapshot_id=...` 可查看任意历史快照。
- **采样画像口径**：确定性有界采样（有主键按主键序 LIMIT，否则无序 LIMIT）；
  返回行数小于 LIMIT 记 `exact`，等于 LIMIT 记 `sample`。
- **样本脱敏**（持久化与 API 响应之前执行）：两层检测——列名关键词（内置 + 可配置追加）
  整列遮蔽；与列名无关的值格式检测（邮箱/大陆手机号/18 位身份证）命中即脱敏。
  允许展示的只有：数字、布尔、日期时间形式短字符串、不含敏感格式且 ≤64 字符的短字符串；
  长文本、JSON、二进制（仅长度）保守遮蔽。`samples_redacted` 如实反映任何脱敏。
  统计指标按原始值计算，不受脱敏影响。**历史画像不会因规则升级自动变安全**，用
  `python scripts/clear_profile_samples.py --db sqlite:///./metadata.db [--apply]` 本地清理
  （只清空 sample_values 并标记需重新画像，保留统计与元数据，不回显样本、不连数据源）。
- **请求校验错误**：422 响应只含 `loc/type/msg`，不回显原始请求体（防止密码经校验错误泄漏）。
- 并发预算：扫描串行执行（不引入任务框架），连接池上限 2+3；
  连接/读取超时沿用 `MYSQL_CONNECT_TIMEOUT_SECONDS / MYSQL_READ_TIMEOUT_SECONDS`。
- 真实 MySQL 集成测试默认跳过，设置 `NL2SQL_MYSQL_INTEGRATION=1` 后运行：
  `NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -m integration -q`。

## MySQL 数据源

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `DATA_MYSQL_HOST` / `DATA_MYSQL_PORT` | `127.0.0.1` / `3306` |
| `DATA_MYSQL_DB` | `nl2sql_source`，用户实际查询的目标库 |
| `DATA_MYSQL_USER` / `DATA_MYSQL_PASSWORD` | `readonly` / 空，应使用只读账号 |
| `MYSQL_CHARSET` | `utf8mb4` |
| `MYSQL_CONNECT_TIMEOUT_SECONDS` | `10`，连接超时 |
| `MYSQL_READ_TIMEOUT_SECONDS` | `30`，读取超时 |
| `ALLOW_SOURCE_DB_WRITES` | `false`，源库写入策略，后续执行层负责强制执行 |

这些字段映射到现有 `Settings.mysql_host / mysql_port / mysql_database / mysql_username / mysql_password`。
兼容旧变量 `MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USERNAME / MYSQL_PASSWORD`，
同一来源同时存在时优先 `DATA_MYSQL_*`；请统一使用新变量名。
查询数据源与系统业务库、元数据仓储相互独立。

### M1 数据源选择规则（已实现）

- 以 `source_id` 操作的接口（测试连接、扫描、目录与画像查询）一律使用已持久化的数据源配置；
- `POST /api/v1/sources/test` 仅使用请求体连接参数做一次性测试，不落库、不注册；
- `POST /api/v1/sources/default` 显式把 `DATA_MYSQL_*`（`Settings.mysql_*`）注册为名为 `default`
  的数据源（幂等，重复调用返回已有源并标记 `already_registered=true`）；服务启动时不自动注册；
- `POST /api/v1/sources` 用请求体注册新数据源，必须 `dialect=mysql` 且 `read_only=true`，
  密码只进入本地凭证文件，元数据表仅保存 `credential_ref`。

只读策略由三层共同落实：数据库账号仅授予 SELECT（部署要求）；
连接建立时执行 `SET SESSION TRANSACTION READ ONLY`（PyMySQL `init_command`）；
代码只执行系统构造的元数据/采样查询（information_schema 参数化查询 + 反引号转义标识符），
不提供任意 SQL 执行接口（`execute_readonly` 在 M6 前显式拒绝）。
仅配置 `ALLOW_SOURCE_DB_WRITES=false` 不能替代上述任何一层。

## Elasticsearch 宽表

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `ES_HOST` | 单节点 URL，转换为 `Settings.es_hosts` 列表；包含 `http://` 或 `https://` |
| `ES_HOSTS` | 可选 JSON 多节点数组；显式配置时优先于 `ES_HOST` |
| `ES_USER` / `ES_PASSWORD` | 空，无认证时留空；`ES_USER` 兼容旧名 `ES_USERNAME` |
| `ES_API_KEY` | 空，与用户名/密码认证二选一 |
| `ES_INDEX_PREFIX` | `nl2sql_wide`，宽表索引命名前缀 |
| `ES_REQUEST_TIMEOUT_SECONDS` | `30` |
| `ES_VERIFY_CERTS` | `true`，HTTPS 校验证书 |
| `ES_CA_CERTS` | 可选，本机 CA 证书文件路径；不使用时保持注释 |
| `ES_BULK_BATCH_SIZE` | `500`，批量写入条数 |

查询 MySQL 是数据来源，ES 用于检索、元数据索引及后续质量校验后的宽表存储。
当前元数据仓储仍由 `METADATA_DATABASE_URL` 控制，ES 索引适配尚未实现。
ES 目标写入独立于源库只读策略；M5 需要实现客户端认证选择、mapping、文档主键、
批量导出、刷新、版本索引和别名切换。当前没有自动创建索引或同步数据。
M6 也尚未实现 ES 查询适配器，不能将生成的 MySQL SQL 直接当作 ES 查询执行。

## DeepSeek 大模型

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `LLM_PROVIDER` | `deepseek` |
| `DEEPSEEK_ENABLED` | 默认 `false`；false 时语义分析明确执行规则模式（rules_only），不伪装模型推断 |
| `DEEPSEEK_MODEL` | 映射到 `llm_model`，以现有配置及账号可用模型为准 |
| `DEEPSEEK_BASE_URL` | 映射到 `llm_api_base`，默认 `https://api.deepseek.com` |
| `DEEPSEEK_API_KEY` | 映射到 `llm_api_key`，凭证不得回显 |
| `LLM_TIMEOUT_SECONDS` | `60`，单次请求超时（连接 5s） |
| `LLM_MAX_RETRIES` | `2`，仅 429/5xx/网络/超时重试；认证错误立即失败 |
| `LLM_MAX_TOKENS` | `4096`，输出 token 上限；超限或被截断的结果弃用 |

M2 已通过 `LLMClient` 实现 DeepSeek 客户端（httpx，OpenAI 兼容 chat/completions，
无供应商 SDK 依赖）。结构化输出校验与领域契约见 [data-contracts.md](data-contracts.md)。
Embedding 模型需要单独选型，不假定 DeepSeek 对话 API 支持向量化；当前语义检索为
本地词法基线（结果标记 `method=lexical`）。

## M2 语义分析预算（已实现）

| 配置项 | 默认值 / 用途 |
| --- | --- |
| `SEMANTICS_MAX_COLUMNS_PER_RUN` | `40`，单次分析最多处理的列数（超出记 budget_truncated） |
| `SEMANTICS_LLM_MAX_CALLS` | `2`，单次分析的最大模型调用数（首次 + 一次修复重试） |
| `SEMANTICS_INPUT_CHAR_LIMIT` | `24000`，模型输入字符上限（超出则本次仅用规则结果） |
| `SEMANTICS_SEND_SAMPLES` | `false`，样本外发总开关；需与请求 `send_samples=true` 同时开启 |
| `SEMANTICS_SEARCH_DEFAULT_LIMIT` | `20`，语义检索默认返回条数上限（≤100） |

说明：

- 模型输入默认仅含净化后的结构元数据与画像统计（含 metric_basis/sample_row_count 口径），
  不含 sample_values、源库地址/账号/密码/credential_ref；注释先经 PII 遮蔽、密钥遮蔽与
  提示注入模式中和。
- 样本外发双层开关开启后，仍跳过敏感列、已脱敏与全遮蔽样本（≤3 条 × 32 字符）。
- 模型调用失败（超时/认证/限流等）与语义 unknown（证据不足）分开表达；失败回退规则结果
  并记录 `model_request_failed:<reason>`，不返回伪造的模型语义。
- 真实模型集成测试默认跳过：`NL2SQL_MYSQL_INTEGRATION=1 NL2SQL_SEMANTICS_INTEGRATION=1
  python -m pytest tests/integration -q`（小预算：≤3 表扫描、≤8 列分析）。

## 登录与 JWT

`LOGIN_ENABLED` 和 `JWT_SECRET` 已映射到同名小写配置属性，JWT 密钥使用 `SecretStr`。
当前 API 尚未实现登录、JWT 校验或权限控制；设置开关不代表接口已经受保护。
后续实现需补齐鉴权、用户持久化和测试，不能将配置加载标记为功能完成。

## 启动与验证

```bash
conda activate nl2sql
python -m uvicorn app.main:app --reload
# 在另一终端激活相同环境后执行
python -m pytest
python -m ruff check app tests
python -m pip check
```

健康检查仅表明 Web 服务可用，不代表 MySQL、ES 或 DeepSeek 连通。
目前只校验配置类型、端口范围及正数超时等；空凭证允许 M0 离线启动。
真实连接、认证方式冲突检查和缺失凭证检查应在对应适配器启用时完成。
本轮实际验证结果及后续任务见 [开发交接报告](development-status.md)。
