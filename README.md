# 企业数据库自动理解与 NL2SQL

这是一个**分阶段、可解耦、供 Claude Code 逐期补齐的模块化单体架构骨架**。当前只搭好工程结构、核心契约、文档和 TODO，不声称数据库扫描、关系发现、宽表生成或 NL2SQL 已经实现。

## 总体目标

数据库配置 → 元数据采集 → 字段画像 → 字段语义理解 → 跨表关系发现与验证 → 业务语义模型 → 可信视图/宽表 → NL2SQL → 查询型智能体。

## 设计原则

1. **模块化单体**：一个服务、清晰模块边界；先不引入微服务、消息队列、图数据库和多智能体框架。
2. **按里程碑实现**：Claude Code 每次只做一个阶段；当前阶段通过验收后才进入下一阶段。
3. **依赖倒置**：业务服务依赖接口（ports），不直接依赖具体数据库驱动或模型供应商。
4. **领域模型稳定**：跨阶段共享对象集中在 `app/domain/`，接口契约集中在 `app/ports/`。
5. **失败显式**：未实现的功能返回明确错误，不伪装成成功。
6. **安全优先**：源数据库默认只读；密码不进日志；模型输出不可信；SQL 执行前必须通过安全检查。
7. **证据优先**：候选关系不等于真实关系；未经验证的关系不得默认进入生产查询。
8. **先视图后物化**：先生成可审查的 SQL 视图；确认性能需求后再考虑物化宽表。

## 技术选型

- Python 3.13.18，以现有 Conda 环境 `nl2sql` 为准
- FastAPI + Pydantic Settings
- SQLAlchemy 2.x（M1 实现数据库适配器时使用）
- pytest
- 元数据存储：本地开发可用 SQLite，部署环境可切 PostgreSQL
- LLM：DeepSeek API，通过接口接入；Embedding 供应商后续单独选择
- 宽表存储：Elasticsearch（M5 实现写入与索引管理）
- 第一版只实现 MySQL 数据源；其他方言通过新增 Connector 扩展

## 运行

```bash
# 已有环境直接激活；首次创建可执行 conda env create -f environment.yml
conda activate nl2sql
python --version  # 当前 3.13.18
python -m pip install -e ".[dev]"
# 仅首次复制，已有 .env 时不要覆盖
test -f .env || cp .env.example .env
# 编辑 .env 中的 MySQL、ES 和 DeepSeek 配置后启动
python -m uvicorn app.main:app --reload
# 在另一终端激活同一环境后运行
python -m pytest
```

访问 `http://127.0.0.1:8000/health` 和 `http://127.0.0.1:8000/api/v1/milestones`。

根目录 `.env` 和 `.env.example` 已由用户更新，请保留现有配置，完整配置说明见
[`docs/configuration.md`](docs/configuration.md)。启动时只加载配置，不自动连接
MySQL/ES、不调用 DeepSeek；M1 的扫描与画像在 API 调用时按需访问源库（只读）。
已安装当前工程和开发依赖；真实验证结果及 Claude 后续任务见 [开发交接报告](docs/development-status.md)。

## M1：数据源接入与元数据画像（可用）

```bash
# 1) 注册 env 中 DATA_MYSQL_* 为默认数据源（幂等）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/default | python -m json.tool
# 返回 source_id，下文记为 <sid>

# 2) 连接测试（请求体一次性测试，不落库）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/test -H 'Content-Type: application/json' \
  -d '{"dialect":"mysql","host":"<host>","port":3306,"database":"<db>","username":"<user>","password":"<pwd>"}'

# 3) 扫描（同步，受 SCAN_MAX_TABLES/PROFILE_* 预算约束）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/scans

# 4) 查看数据源卡片 / Schema / 表目录 / 表详情 / 字段画像 / 扫描任务
curl -s http://127.0.0.1:8000/api/v1/sources/<sid>
curl -s http://127.0.0.1:8000/api/v1/sources/<sid>/tables
curl -s http://127.0.0.1:8000/api/v1/tables/<table_id>
curl -s http://127.0.0.1:8000/api/v1/tables/<table_id>/profile
curl -s http://127.0.0.1:8000/api/v1/scans/<job_id>
```

元数据落在 `metadata.db`（SQLite，快照模型），凭证在 `.credential_store.json`（本地开发方案，
文件锁 + 原子替换，明文存储仅限开发）。重复扫描生成新快照：**默认目录始终是最近一次完整成功
快照**，失败/部分成功/预算截断不会替换有效目录（历史快照保留，可用 `?snapshot_id=` 查看）。
行数为 information_schema 估计值（显式标记），画像逐列记录 `metric_basis`
（exact/sample/insufficient_sample/skipped）。样本在持久化与返回前经过两层脱敏：敏感列名
关键词整列遮蔽 + 值格式检测（邮箱/手机号/身份证），长文本与 JSON 保守遮蔽；历史画像需用
`scripts/clear_profile_samples.py` 本地清理（保留统计，只清样本并标记需重扫）。

## M2：字段语义理解（主体可用，Embedding 检索未接入）

```bash
# 1) 对某张已扫描的表触发语义分析（默认规则模式；DEEPSEEK_ENABLED=true 时为模型辅助）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/semantics/analyze \
  -H 'Content-Type: application/json' -d '{"table_id":"<table_id>","column_limit":8}'
# 返回 run_id；部分快照需显式 {"allow_partial": true}

# 2) 查看运行状态 / 表语义 / 字段修订历史
curl -s http://127.0.0.1:8000/api/v1/semantics/runs/<run_id>
curl -s http://127.0.0.1:8000/api/v1/tables/<table_id>/semantics
curl -s http://127.0.0.1:8000/api/v1/tables/<table_id>/semantics/<column>/history

# 3) 人工审核（expected_revision 并发控制，冲突返回 409）
curl -s -X POST http://127.0.0.1:8000/api/v1/tables/<table_id>/semantics/<column>/review \
  -H 'Content-Type: application/json' \
  -d '{"action":"confirm","business_name":"订单总金额","reviewer":"dev","expected_revision":1}'

# 4) 按业务概念搜索字段（本地词法基线，method=lexical）
curl -s "http://127.0.0.1:8000/api/v1/sources/<sid>/semantics/search?q=组织机构编码"
```

约束：模型输入默认不含样本与连接信息（双层样本开关默认关）；证据引用经服务端核验；
人工确认不被新一轮模型建议覆盖；模型故障与语义 unknown 分开表达。评测指标与真实模型
验证结果见 [开发交接报告](docs/development-status.md)。

## 目录

```text
app/
  api/          HTTP 层，只做参数校验和响应转换
  core/         配置、日志、异常
  domain/       跨阶段共享的领域对象
  ports/        连接器、仓储、LLM 等接口契约
  adapters/     接口的具体实现，如 MySQL 连接器
  services/     用例编排，不放大段算法
  profiling/    字段画像（M1）
  semantics/    字段语义和业务语义模型（M2/M4）
  relationships/ 关系发现与验证（M3）
  datasets/     视图/宽表生成和质量检查（M5）
  nl2sql/       SQL 生成、安全检查和只读执行（M6）
  agents/       查询型智能体（M7）
docs/
  architecture.md
  milestones.md
  data-contracts.md
  claude-code-guide.md
  decisions/
tests/
```

## Claude Code 开发约定

开始每个阶段前，先阅读：
- `docs/architecture.md`
- `docs/milestones.md`
- `docs/data-contracts.md`
- `docs/claude-code-guide.md`
- `docs/configuration.md`
- `docs/development-status.md`
- 当前阶段涉及模块中的 TODO

每次只做一个里程碑。要求 Claude Code：
- 先检查现有代码，再给出计划；
- 只修改本阶段涉及的模块和必要接口；
- 写测试并真实运行；
- 给出真实输入输出、运行命令、测试结果；
- 明确尚未实现的内容；
- 不删除 TODO 来伪装完成；
- 不得把数据库密码、API Key 或未脱敏业务样本写入日志。

## 当前状态

- [x] M0：工程结构、文档、配置、健康检查、里程碑 API、核心契约
- [x] M1：数据源连接、元数据扫描、画像与持久化（真实库全链路验证，见交接报告）
- [~] M2：字段语义理解（规则+DeepSeek 已真实验证；Embedding 检索未接入，见交接报告）
- [ ] M3：跨表关系发现
- [ ] M3：关系发现与验证
- [ ] M4：业务语义层
- [ ] M5：视图/宽表生成和质量校验
- [ ] M6：NL2SQL、安全执行和结果验证
- [ ] M7：查询型智能体
- [ ] M8：产品化、版本、漂移和运维治理
