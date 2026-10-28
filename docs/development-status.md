# 开发交接报告

更新时间：2026-10-27（M1 完成轮 + 缺陷修复轮 + 收尾修复轮）。本报告记录实际环境和验证结果，后续开发仍需按改动复验。

## 当前环境

- 使用现有 Conda 环境 `nl2sql`，实际 Python **3.13.18**，保留当前环境，不降级到 3.12。
- 本机解释器：`/Users/apple/miniconda3/envs/nl2sql/bin/python`；其他机器激活自己的同名环境。
- 依赖：**PyMySQL 1.2.3**（MySQL 驱动，`pyproject.toml` 已声明）；其余与 M0 基线一致
  （fastapi 0.162.0 / sqlalchemy 2.1.6 / pytest 8.4.2 / ruff 0.17.3 等）。
- 本轮无新增第三方依赖（凭证锁用标准库 `fcntl`，POSIX 范围见下）。

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
