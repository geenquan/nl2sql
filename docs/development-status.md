# 开发交接报告

更新时间：2026-10-28（M1 完成 + 三轮修复；M2 字段语义理解完成主体，Embedding 检索项未完成）。本报告记录实际环境和验证结果，后续开发仍需按改动复验。

## 当前环境

- 使用现有 Conda 环境 `nl2sql`，实际 Python **3.13.18**，保留当前环境，不降级到 3.12。
- 本机解释器：`/Users/apple/miniconda3/envs/nl2sql/bin/python`；其他机器激活自己的同名环境。
- 依赖：**PyMySQL 1.2.3**（MySQL 驱动）；**httpx 0.28.1 转为运行时依赖**（DeepSeek 客户端
  使用，未引入供应商 SDK）。其余与 M0 基线一致（fastapi 0.162.0 / sqlalchemy 2.1.6 等）。

## M2 修复轮 2（2026-10-28，五项遗漏，全部先复现后修复）

1. **敏感文本缺口【P1】**：键值遮蔽重写为“保留键名+完整遮蔽值到句界”（覆盖
   Bearer/引号/空格/后半段，`token: Bearer X` 不再残留 X）；样本外发逐值执行
   mask_value + sanitize_text_for_model 完整净化，净化后仍残留键值形态的值拒绝外发；
   模型提供的 name_pattern/glossary_term 上下文证据（任意文本不可核验）一律丢弃，
   不存储、不作为依据；business_name/entities/rationale/alternatives 全部净化。
2. **证据校验漏洞【P1】**：修复 `return column if ref == column else None, True`
   元组缺陷（此前产生 ref=None 的“有效证据”）；index 证据仅接受包含当前字段的
   真实索引名（字段名自身不能绕过）；画像指标数值等值（类型明确、容差 1e-9，
   不做字符串包含）；表用途画像证据必须以 columns.<列>.profile.<指标>[:=值]
   定位实际字段的已计算指标（值+口径核对），裸指标名/值形式不再通过；
   上层增加规范引用非空不变量。
3. **运行总期限【P2】**：单调时钟绝对截止时间贯穿连接/读取/退避/流式分块；
   剩余期限传入客户端（重试与修复共享）；返回后再次检查，超期结果不发布
   （deadline_exceeded 回退规则）；注入时钟测试，无真实长 sleep。
4. **失败路径计量【P2】**：HTTP 尝试计数统一在发起点，所有失败路径携带累计次数；
   usage/model 在内容解析前提取，内容不合格/截断不丢用量（随异常携带）；
   分项完整性 `llm_prompt_usage_complete/llm_completion_usage_complete`
   （已知一侧保留、缺失一侧如实标记）；同一逻辑调用内含传输重试时早期尝试
   用量不可确认 → 完整性 False 但保留已知小计；model_reported 仅服务端值。
5. **全状态有界读取【P2】**：错误响应体也走有界流式嗅探（4KB），
   超限直接丢弃正文只用状态码；声明长度仅对 200 生效且不信任（非法值忽略，
   累计字节为最终上限）；读取中途异常按 network 处理。

验证（修复轮 2 后）：`python -m pytest -q` **357 passed, 6 skipped**；ruff/pip check 通过；
真实复验 hotel 只读 + deepseek-flash 1 次调用（1201/668 tokens）通过全部新校验。

## M2 缺陷修复轮（2026-10-28，七组问题全部先复现后修复）

1. **模型输入净化与样本外发【P1】**：键值敏感信息改为“保留键名+遮蔽值”（冒号/等号/
   全角/空白/大小写变体，原匹配值不再拼回）；所有文本入口（注释/字段名/表名/约束名/
   引用字段/修复反馈）统一净化；修复反馈只含固定提示与服务端已知对象名（模型编造名
   不回传）；样本外发边界只信任带当前 MASKING_VERSION（mask-v2，画像持久化时随
   payload 记录）的未脱敏非敏感样本并逐值再脱敏，历史样本一律不外发；模型输出的
   business_name/entities/rationale 均经类型/长度/安全校验（类型错误显式失败，
   不 str() 冒充、不静默清空）。
2. **跨表同名字段合并【P1】**：`_effective_revision` 等全部按 (table_id, column_name)
   分组，跨表/跨源/跨快照不合并；审核优先只作用于对应字段。
3. **审核并发原子性【P1】**：`expected_revision` 传入仓储，版本比较与写入同事务；
   并发同版本提交恰一个成功（唯一约束为多进程仲裁者，IntegrityError→409）；
   过期视图优先报冲突而非转换错误；真实并发（线程 Barrier）回归覆盖字段/表用途/
   模型写入交错。
4. **证据归属与结构校验【P1】**：证据按“当前字段直接证据/表级上下文证据”区分核验，
   跨字段引用（他列主键/注释/复合索引）、不可用画像指标（None/skipped/
   insufficient/值不符）一律无效；支持标准/路径/值三形态并规范化；仅上下文证据
   降级 unknown；表用途要求结构化对象+可核验证据+备选；输出类型错误显式失败。
5. **撤销后有效选择【P2】**：确认后的人工修订（revert/改写）终止此前确认生效；
   模型/规则修订不终止（模型不覆盖人工确认）；字段/表用途/检索一致；rejected
   不参与检索加权。
6. **失败运行中间结果【P1】**：整次分析（字段+表用途+运行记录）经
   `save_semantics_batch` 单事务提交，failed 运行零残留；模型失败回退规则的
   partial 可发布（来源标记 rules）；显式 snapshot_id 按对应扫描任务核定
   input_complete，不再无条件视为完整。
7. **模型预算与用量【P2】**：`SEMANTICS_LLM_MAX_CALLS` 重定义为**总 HTTP 尝试预算**
   （传输重试与结构修复共享，客户端按剩余预算限定尝试，不嵌套放大）；token 累计
   所有返回过 usage 的响应（含结构不合格），缺失标记 llm_usage_complete=false；
   model_reported 只填服务端返回值（配置模型分列 model_configured）；总期限
   `SEMANTICS_LLM_DEADLINE_SECONDS` 独立于请求超时；响应字节上限在流式读取中执行；
   修复请求重查输入预算；末次失败不退避。

真实验证（修复后）：hotel 库只读 + deepseek-flash，1 次逻辑调用（1201/680 tokens，
2415ms）通过全部新校验（含跨字段证据拒绝、值形式规范化、表注释表级对象）；期间曾
出现“模型输出字符串用途/路径证据被拒→由修复与值形式规范化解决”的真实往返。

## M2 字段语义理解（2026-10-28 完成主体）

### 已实现能力

| 能力 | 实现 |
| --- | --- |
| 结构化语义契约 | `app/domain/semantics.py`：类别/表用途/置信/证据类型注册表（集中可扩展，无行业硬编码）、审核状态机（suggested → business_confirmed/rejected，revert 回 suggested）、FieldSemantics/TablePurpose/SemanticRun |
| 拆词与术语 | `app/semantics/tokenizer.py`（中英文/下划线/驼峰/缩写/中文贪心组词/虚词分隔）、`glossary.json`（小型通用术语表，中英双语）+ `glossary.py`（最长子词匹配） |
| 规则初筛 | `app/semantics/rules.py`：约束>术语>类型>命名优先级；信号冲突保留多候选并降置信、类型信号物化为备选；画像引用带口径 caveat（样本唯一≠全表唯一）；无信号必须 unknown；不推断状态码含义 |
| DeepSeek 客户端 | `app/llm/deepseek_client.py`（httpx）：仅 429/5xx/网络/超时重试；401/403 立即失败；finish_reason=length 或超字符预算判截断弃用；响应字节上限；usage/model 如实记录（缺省 None=unavailable）；固定安全错误消息 |
| 输入/输出安全 | `app/semantics/prompt.py`：输入白名单（无 sample_values/连接信息/凭证）；注释净化（去控制字符、PII 遮蔽、sk-密钥遮蔽、提示注入模式中和、限长）；输出严格校验（字段覆盖、枚举、长度、证据引用核验）；证据支持标准/路径两种形态并规范化；仅不可核验证据不足以支撑非 unknown |
| 服务与 API | `services/semantics_service.py` + `routes/semantics.py`：分析（默认发布快照，部分目录需 allow_partial 并标记 input_complete=false，显式 snapshot_id 仅诊断）、运行记录、语义/用途查询、人工修订（expected_revision 并发控制→409）、历史、词法检索 |
| 持久化 | SQLite 追加三表（field_semantics/table_purposes/semantic_runs，`create_all` 兼容追加）；修订式历史（不覆盖审计）；effective 解析使人工确认不被模型覆盖 |
| 语义检索 | 本地词法基线（确认业务名 3.0 > 建议 2.0 > 原名 2.0 > 注释/实体 1.5 > 类别 1.0 > 表名 0.5），结果标记 `method=lexical`，默认限定 source+发布快照 |

### 输入安全与模型预算（实际策略）

- 模型输入默认仅含：表名/注释（净化后）、列名/类型/注释（净化后）、约束（表/列名）、画像统计（含 metric_basis/sample_row_count 口径）。**不含 sample_values、源库地址/账号/密码/credential_ref**。
- 样本外发开关双层：`SEMANTICS_SEND_SAMPLES`（配置，默认 false）且请求 `send_samples=true`；开启后仍跳过敏感列、已脱敏与全遮蔽样本，仅 3 条 × 32 字符。
- 预算：单次分析列数（`SEMANTICS_MAX_COLUMNS_PER_RUN=40`，演示/验证用 8）、LLM 调用上限（`SEMANTICS_LLM_MAX_CALLS=2`：首次+一次修复）、输入字符上限（24000）、输出 max_tokens 沿用 `LLM_MAX_TOKENS`、响应字节上限 2MB。
- 错误处理沿用固定安全文案：模型故障（model_request_failed:reason）与证据不足（unknown）分开；不落提示词/响应/异常文本。

### 评测结果（离线，开发测试标签）

标签来源：开发者编写的回归用例（18 字段类别 + 6 表用途 + 2 unknown + 2 冲突 + 1 unknown 表），
**不是人工金标准**；数值仅作回归监控。

| 指标 | 结果 | 分母 |
| --- | --- | --- |
| 字段类别准确率（规则模式） | **0.889**（16/18） | 18 |
| 表用途准确率（规则模式） | **1.000**（6/6） | 6 |
| unknown 召回（应 unknown 的用例） | **1.000**（2/2） | 2 |
| 冲突用例降置信+保留备选 | **2/2 通过** | 2 |
| 证据引用有效率（规则模式） | **1.000**（核验通过） | 全部证据 |
| 结构化输出合格率（规则模式） | **1.000**（确定性路径） | 18 |
| 模型路径结构合格率（离线假模型矩阵） | 合法/虚构/遗漏/重复/无效证据用例全部按预期通过或拒绝 | 12+ 用例 |
| 候选实体准确率 / 人工修订率 / 成本 | **unavailable**（无可靠标签与价格配置，不虚构） | — |

### 实际验证结果（Conda `nl2sql`，2026-10-28）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **357 passed, 6 skipped**（修复轮 2 后；skipped 为真实集成，默认关闭；无残留文件） |
| `python -m ruff check app tests scripts` | **All checks passed** |
| `python -m pip check` | **No broken requirements found** |
| `NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q`（M1 只读复验） | **5 passed** |
| `NL2SQL_MYSQL_INTEGRATION=1 NL2SQL_SEMANTICS_INTEGRATION=1 pytest tests/integration -q`（含真实模型） | **6 passed**：真实模型链路通过；曾出现一次“首次校验失败→修复重试→通过”（验证有限修复机制），另一次直接通过 |
| 真实模型演示（临时库） | hotel 库只读扫描（5 表预算）→ `code_consumption_history`（9 列）8 列预算分析：mode=model_assisted、**deepseek-flash**、1 次调用、1139/633 tokens、2549ms；表用途 log_history；字段类别含 entity_identifier/foreign_identifier_candidate/timestamp/status（证据已核验）；人工确认→修订 2；历史 [(2,human,confirmed),(1,model,suggested)]；词法检索命中并展示 match_basis |

### 未完成项（明确保留）

- **Embedding 语义检索未接入**：当前无 Embedding 配置（DeepSeek 对话接口 ≠ 向量接口），
  已交付本地词法基线（结果明确标记 `method=lexical`，不称向量相似度），`EmbeddingClient`
  端口保留；阶段手册“使用 Embedding 检索相似字段”一项未完成 → **M2 整体标记 partial**。
- 候选实体准确率、人工修订率、成本指标 unavailable（无标签/无价格配置）。
- 术语表为内置通用版，未做管理界面；人工操作者标识为本地开发输入，无身份认证。

### M3 可使用的输入

- 表/字段语义（含类别、候选实体、证据、置信、审核状态）可按 source+snapshot 稳定读取：
  `GET /tables/{id}/semantics`、`/sources/{id}/semantics/search`；
- `foreign_identifier_candidate` 字段清单可直接作为 M3 关系候选召回输入（注意：语义相似
  ≠ 可 JOIN，M3 必须自行验证）；画像统计（含口径）提供值域覆盖/唯一性证据基础。

## M1 缺陷修复轮（2026-10-27，第三轮：3 项遗漏）

## M1 缺陷修复轮（2026-10-27）

五个问题全部先复现、后修复、均有能在旧实现上失败的回归测试：

### 1. 请求校验错误回显密码【P1】
- 根因：FastAPI 默认把 `RequestValidationError` 的原始 `input` 放回 422 响应；`SecretStr`
  只保护解析成功的模型，校验失败阶段的密码明文被回显（已复现：缺 port 的 `/sources/test`
  请求响应含完整密码）。
- 修复：`app/main.py` 统一拦截 `RequestValidationError`，只返回 `loc/type/msg`
  （msg 截断 300 字符），不含 `input`、`ctx` 或异常字符串；不记录请求体。
- 回归：`tests/test_validation_error_sanitization.py`（缺字段+密码、非法端口、密码类型错误、
  嵌套输入含密钥、响应字段白名单）。

### 2. 失败扫描替换有效目录【P1】
- 根因：仓储把“最近 succeeded/partial 任务”直接当默认快照，全部表失败的新任务（partial、
  scanned=0）成为默认目录，表目录被清空（已复现）。
- 修复：任务状态与目录发布分离（`get_published_snapshot`）：默认目录＝最近完整成功快照；
  无完整成功回退最近 partial（`tables_scanned>0`）并标记 `complete=false`；failed/running
  永不发布。预算内表全部失败判 `failed`。权限/采样失败/预算跳过缺失的表不当作确认删除；
  完整成功确认空 Schema 发布空目录；历史快照全保留，`?snapshot_id=` 可查看任意历史快照；
  数据源卡片同时返回 `latest_scan` 与 `catalog`（目录实际来源）。
- 回归：仓储 5 个发布规则测试；服务层 `TestScanPublication`（成功后全失败/部分失败/预算截断、
  首次仅部分、确认空 Schema、删除表消失）；API 层目录保留与历史查看测试。

### 3. 画像样本脱敏不足【P1】
- 根因：脱敏只依赖列名关键词，普通文本仅截断；`contact` 列的邮箱、`id_number` 列的身份证
  格式值原样进入 `sample_values` 并持久化（已复现）。
- 修复：`app/security/masking.py` 两层检测——列名（内置关键词扩充 + `SENSITIVE_COLUMN_KEYWORDS`
  可配置追加）+ 与列名无关的值格式检测（邮箱/大陆手机号/18 位身份证：整值命中部分遮蔽，
  嵌入文本整值遮蔽）。允许展示类型白名单：数字/布尔/日期时间短字符串/≤64 字符无敏感格式
  短字符串；长文本、JSON、dict/list 保守遮蔽；二进制仅长度。脱敏在持久化与 API 响应之前；
  `samples_redacted` 如实反映任何脱敏；统计指标仍按原始值计算。
- **历史数据**：已持久化画像不会因规则升级自动变安全。提供本地脚本
  `scripts/clear_profile_samples.py`（默认预览，`--apply` 执行）：只清空历史 `sample_values`
  并标记 `sample_values_cleared=true`，保留统计与元数据；不回显样本、不连数据源、不自动重扫。
- 回归：`tests/test_masking.py`（19 项，虚构数据）、`tests/test_profiler.py` 值格式用例、
  服务/API 层持久化与响应不含原始敏感样本断言、`tests/test_clear_profile_samples.py`。

### 4. 扫描任务异常后状态停留 running【P2】
- 根因：`run_scan` 只捕获连接类错误，`get_table_metadata` 抛 `RuntimeError` 时异常直接上抛，
  连接关闭但任务永远 `running`（已复现）。
- 修复：全生命周期收尾——任务级异常尽力落库 `failed + finished_at + 已清洗原因`
  （`internal_error` / `credential_store_error`；消息剔除凭证、截断 300 字符，不保存原始异常
  全文）。表级可恢复异常（连接错误、非法标识符、采样/画像崩溃）按表隔离
  （`table_scan_error` / `invalid_identifier` / 连接 reason），不再统一伪装成
  invalid_identifier；持久化等基础设施故障中止扫描；`connector.close()` 异常被吞掉，
  不覆盖主要结果、不阻断收尾。**已知限制**：若元数据库在收尾时本身不可写，最终状态无法
  落库（异常上抛，API 5xx，不伪装成功）。
- 回归：`TestScanFinalization`（连接器创建失败、未预期元数据异常、画像失败按表隔离、
  持久化失败中止、close 失败不阻断、凭证文件损坏）。

### 5. 凭证文件写入可靠性【P2】
- 根因：`FileCredentialStore` 用内存缓存 + `O_TRUNC` 直接覆盖：两实例先后写入丢更新
  （已复现 cred-a 丢失）；中断可能留下损坏文件；损坏 JSON 被静默当空存储。
- 修复：写入＝独占文件锁（POSIX `flock`，锁文件 0600）→ 锁内重读最新 → 同目录临时文件
  写入 + `fsync` → `os.replace` 原子替换；读操作每次重读文件（能看到其他实例/进程提交的
  更新）；损坏/非法 JSON 显式抛 `CredentialStoreError`（API 503），绝不覆盖；写入失败清理
  临时文件、旧文件完好；新建目录逐级 0700，不 chmod 用户已有目录。**限制**：跨进程锁依赖
  flock（Linux/macOS）；无 fcntl 平台（Windows）不在支持范围。仍是明文本地存储，仅限开发。
- 回归：`tests/test_credentials.py`（11 项：双实例交替、多进程并发写、写失败旧文件可读、
  损坏不覆盖、删除/读取一致性、权限与父目录不动、嵌套目录权限）。

## M1 收尾修复轮（2026-10-27，第三轮：3 项遗漏）

### 6. 扫描错误信息泄漏业务样本【P1】
- 根因：`_internal_message()` 用 `str(exc)` 构造未预期异常的对外/持久化消息，仅替换数据库
  密码并截断；画像等异常文本中的邮箱、手机号、token、样本密码会随扫描任务 errors 持久化
  并经 `GET /scans/{id}` 返回（已复现）。
- 修复：`app/services/scan_service.py` 未预期异常一律使用**固定安全文案**
  （异常类型名 + 阶段 + schema + table + job_id，如“未预期错误[RuntimeError]，阶段=读取表结构，
  schema=sales，table=orders，job_id=…；错误详情已抑制”），不使用 `str(exc)/repr/args`，
  也不写日志或异常链规避（`raise ... from None`）；全流程跟踪阶段（读取凭证/创建连接器/
  测试连接/列举Schema/扫描表/读取表结构/计算画像/持久化快照）。`invalid_identifier` 同样改
  固定文案；仍保留透出的仅有两类明确安全的领域消息：连接器已脱敏的
  `DataSourceConnectionError` 文案与 `CredentialStoreError` 固定文案。按表隔离、任务收尾、
  有效快照保护逻辑不变（持久化失败经内部哨兵中止，不重复包装错误）。
- 回归：`tests/test_services.py::TestErrorSanitization`（元数据/画像/持久化/连接器创建四类
  异常各嵌入邮箱+手机号+token+样本密码载荷；断言返回值、SQLite 任务载荷、`GET /scans/{id}`
  响应、caplog 捕获日志均不含原始敏感值，且可通过 reason+阶段+类型名定位；失败任务不停留
  running、不替换有效目录）。

### 7. 已有空凭证文件被当作空存储【P2】
- 根因：`_read_latest()` 对已存在的零字节/纯空白文件返回 `{}`，随后 `put()` 覆盖（已复现）。
- 修复：仅“文件不存在”视为空存储（允许首次初始化）；合法空 JSON 对象 `{}` 也是空存储；
  已存在的零字节、纯空白、非法 JSON、非对象结构、键或值非字符串的文件一律抛
  `CredentialStoreError`，get/put/delete 均失败且**原文件字节保持不变**（不修改、不覆盖、
  不自动修复、不删除）；不再把非法结构 `str()` 后当密码使用。
- 回归：`tests/test_credentials.py::TestFileStateSemantics`（缺失可首写、`{}` 可读写、
  三类损坏内容 × get/put/delete 后字节不变、非字符串值拒绝、报错不含凭证内容）。

### 8. 凭证文件系统错误未统一转换【P2】
- 根因：`os.replace` 等失败直接抛 `PermissionError` 绕过 503 处理器（已复现）。
- 修复：`app/security/credentials.py` 在存储边界把预期的文件系统/锁/编码故障（读取、
  目录创建、锁文件打开、flock、写入/fsync/替换）统一转换为 `CredentialStoreError`，
  固定安全消息仅含异常类型名与路径（不含文件内容、凭证、底层异常文本，链上 `from None`）；
  API 按既有约定返回 503；扫描流程记录 failed + credential_store_error，绝不返回扫描成功。
  文件锁、锁内重读、原子替换、0600 权限、不改用户目录权限全部保留；写入失败旧文件完整可读、
  临时文件清理失败不覆盖主要错误；按异常类型精确捕获，不用宽泛 `except Exception` 隐藏编程错误。
- 回归：`tests/test_credentials.py::TestFilesystemErrorUnification`（读取/锁打开/flock/
  fsync/replace/目录创建六类故障，断言异常类型、恢复后可继续、旧文件完整）；
  `tests/test_sources_api.py::TestCredentialStoreUnavailable`（API 503 + 安全消息 +
  不回显请求凭证 + 未持久化不产生数据源）。

## 实际验证结果（Conda `nl2sql` 环境、项目根目录执行，收尾修复轮后）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **174 passed, 5 skipped**（skipped 为真实 MySQL 集成测试，默认关闭；不访问远端，运行后项目根无残留文件） |
| `python -m ruff check app tests scripts` | **All checks passed** |
| `python -m pip check` | **No broken requirements found** |
| 真实库集成（`NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q`，授权只读复验） | **5 passed**：连接、Schema 可见性、表结构读取与有界采样、任意 SQL 拒绝、完整服务链路（临时元数据库/凭证文件；断言元数据库无密码明文、画像口径与脱敏标记）。未打印连接信息或样本 |

单元测试覆盖累计：校验错误不回显凭证（422 白名单字段）、目录发布规则（成功后全失败/部分
失败/预算截断/首次部分/空 Schema/删除表消失/历史快照可查）、值格式脱敏（邮箱/手机号/
身份证/嵌入文本/JSON/自由文本/二进制/日期/数字/空串）、API 与 SQLite 持久化均不含原始
敏感样本、扫描异常收尾六场景、**未预期异常固定安全文案（四类阶段注入业务样本载荷，含
任务载荷/API 响应/日志三通道断言）**、凭证文件状态语义（缺失/`{}`/损坏/非法结构）、
凭证文件系统故障统一转换（六类故障 + API 503）、多进程并发写、迁移脚本幂等且不回显样本。

## 元数据存储与目录规则（当前生效）

- 位置：`METADATA_DATABASE_URL`（默认 `sqlite:///./metadata.db`）；
  凭证：`CREDENTIAL_STORE_PATH`（默认 `./.credential_store.json`，均已 gitignore）。
- 快照：每次扫描生成 `snapshot_id`，表/画像归属快照，历史全部保留；
  **默认目录＝最近完整成功快照，无则回退最近部分成功（complete=false）**；
  同快照内按（snapshot, schema, table）upsert 去重；画像按 table_id 整表替换。
- 任务状态：`failed`=结构性失败或预算内表全部失败；`partial`=表级失败或预算截断。

## 安全限制与准确表述

- 源库只读三层防线（账号权限 / 连接级 READ ONLY / 仅系统构造查询）不变；
  无任意 SQL 执行 API；422 校验错误不回显请求体；连接错误消息双通道清洗密码。
- **扫描错误消息采用固定安全文案**：未预期异常只透出异常类型名 + 阶段 + schema/table +
  job_id（“错误详情已抑制”），不使用异常文本，也不转移至日志或异常链；对外透出的领域
  消息仅限连接器已脱敏的连接错误与凭证存储固定文案。
- 样本脱敏是**两层检测 + 保守白名单展示**，不是“全面脱敏”：列名关键词与三类值格式的
  命中保证；无法确认安全的长文本/JSON 一律遮蔽；未覆盖的格式（如其他证件、地址、姓名）
  依赖“仅展示短字符串”的白名单缓解。历史画像需用脚本清理后重扫（本轮未自动执行）。
- **凭证文件状态语义**：文件不存在＝空存储（允许首写）；`{}`＝空存储；已存在的空/空白/
  损坏/非法结构文件一律报错且字节不动。凭证读写的一切预期文件系统/锁/编码故障统一返回
  固定安全错误的 503（消息仅含异常类型名与路径）。本地凭证文件仍为明文 + 文件锁 +
  原子替换，仅限开发；生产密钥管理在 M8。

## 实现边界与下一步

未实现项不变：字段语义理解（M2）、关系发现（M3）、语义层（M4）、视图/宽表与 ES（M5）、
NL2SQL 与任意 SQL 执行（M6）、智能体（M7）、登录/JWT、后台任务与正式迁移（M8）。
扫描仍为同步执行；`BIZ_MYSQL_*` 业务表未接入。

**M2 输入就绪**：表/字段元数据与画像（含口径标记）可从元数据库稳定读取；
`GET /tables/{id}` 与 `/tables/{id}/profile` 可直接作为语义理解输入。

## 交接时的常用命令

```bash
conda activate nl2sql
python --version                       # 3.13.18
python -m uvicorn app.main:app --reload
python -m pytest -q                    # 单元测试（不访问远端）
python -m ruff check app tests scripts
python -m pip check
python scripts/clear_profile_samples.py --db sqlite:///./metadata.db          # 历史样本预览
python scripts/clear_profile_samples.py --db sqlite:///./metadata.db --apply  # 清空历史样本
NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q              # 真实库集成（只读）
```

已有环境及 env 文件时直接使用，不重新创建、不用模板覆盖。不得把配置内容和凭证粘贴到报告或日志。
