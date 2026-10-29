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

## M3：跨表关系发现与验证（可用）

```bash
# 1) 候选召回（纯本地 M1/M2 输入，不连源库；默认发布快照）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/relationships/discover \
  -H 'Content-Type: application/json' -d '{}'
# 返回 run（candidates_new/notes/budget）；部分快照需 {"allow_partial": true}

# 2) 按优先级有界只读验证 Top-N 候选（显式触发才连源库，聚合查询无业务值）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/relationships/validate \
  -H 'Content-Type: application/json' -d '{"limit": 5}'

# 3) 列出/查看关系详情（证据束、口径、阻断原因）与修订历史
curl -s "http://127.0.0.1:8000/api/v1/sources/<sid>/relationships?status=validated"
curl -s http://127.0.0.1:8000/api/v1/relationships/<relationship_key>
curl -s http://127.0.0.1:8000/api/v1/relationships/<relationship_key>/history

# 4) 人工审核（confirm/reject/revert；expected_revision 冲突返回 409）
curl -s -X POST http://127.0.0.1:8000/api/v1/relationships/<relationship_key>/review \
  -H 'Content-Type: application/json' \
  -d '{"action":"confirm","reviewer":"dev","expected_revision":2}'

# 5) 标记过期与显式重验；单关系验证 / 运行记录
curl -s -X POST http://127.0.0.1:8000/api/v1/relationships/<relationship_key>/stale \
  -H 'Content-Type: application/json' -d '{"reason":"结构变化","expected_revision":3}'
curl -s -X POST "http://127.0.0.1:8000/api/v1/relationships/<relationship_key>/revalidate?source_id=<sid>"
curl -s http://127.0.0.1:8000/api/v1/relationships/runs/<run_id>

# 6) 关系图（可 Mermaid）与受限多跳路径（仅 validated 非 stale 边）
curl -s "http://127.0.0.1:8000/api/v1/sources/<sid>/relationships/graph?mermaid=true"
curl -s "http://127.0.0.1:8000/api/v1/sources/<sid>/relationships/paths?from_table_id=<t1>&to_table_id=<t2>&max_hops=3"
```

约束：候选≠已验证（ADR-002），默认路径搜索只用 validated 非 stale 边；验证前核对源库
结构与输入快照一致；指标只记聚合口径（exact/sample/unknown），采样口径不能自动升级为
validated；真实只读演示脚本 `python scripts/m3_real_validation.py`（临时目录、预算先行、
不打印业务值）。评测与真实验证结果见 [开发交接报告](docs/development-status.md)。

## M4：业务实体与语义层（可用）

```bash
# 1) 构建模型草稿（纯本地确定性规则；默认发布快照，部分快照需 allow_partial）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/semantic-models/build \
  -H 'Content-Type: application/json' -d '{}'
# 返回 model_id 与 version；实体/主题/关系（M3 validated）/候选指标（全部 suggested）

# 2) 人工修订：确认 grain 与指标口径（引用经服务端核验；并发基线冲突 409）
curl -s -X POST http://127.0.0.1:8000/api/v1/semantic-models/<mid>/revise \
  -H 'Content-Type: application/json' -d '{
    "base_version": 1, "reviewer": "dev",
    "topic_grains": {"topic.app.booking": {"statement": "一行代表一次预订", "confirm": true}}}'

# 3) 检查 / 发布 / 当前发布版本 / 审计
curl -s -X POST "http://127.0.0.1:8000/api/v1/semantic-models/<mid>/validate?version=2"
curl -s -X POST "http://127.0.0.1:8000/api/v1/semantic-models/<mid>/publish?version=2" \
  -H 'Content-Type: application/json' -d '{"reviewer":"dev","note":"首次发布"}'
curl -s http://127.0.0.1:8000/api/v1/sources/<sid>/semantic-models/published
curl -s http://127.0.0.1:8000/api/v1/sources/<sid>/semantic-models/publication-log

# 4) 差异 / 回滚 / 导出（JSON/YAML 同语义）
curl -s "http://127.0.0.1:8000/api/v1/semantic-models/<mid>/diff?version_a=2&version_b=3"
curl -s -X POST http://127.0.0.1:8000/api/v1/semantic-models/<mid>/rollback \
  -H 'Content-Type: application/json' -d '{"target_version":2,"reviewer":"dev","reason":"回滚"}'
curl -s "http://127.0.0.1:8000/api/v1/semantic-models/<mid>/export?version=2&format=yaml"
```

约束：模型版本不可变（修订产生新版本）；发布＝切换下游使用指针（不发布
数据集、不执行 SQL）；仅 M3 validated 非 stale 关系进入模型；路径仅正向且
多对多阻断、歧义待审；未确认 grain/指标不得发布；依赖降级
（needs_revalidation/snapshot_changed）时旧模型可查看导出但不冒充当前可用。
真实演示脚本 `python scripts/m4_real_demo.py`（临时目录、小预算、人工输入
标注开发演示假设）。

## M5：数据集、主题视图与 ES 宽表（离线闭环可用；发布目标默认关闭）

```bash
# 1) 创建数据集（服务端核验 M4 门槛；结构化定义不接受 SQL 片段）
curl -s -X POST http://127.0.0.1:8000/api/v1/sources/<sid>/datasets \
  -H 'Content-Type: application/json' -d '{
    "name": "预订聚合", "model_id": "<mid>", "model_version": 2,
    "topic_id": "topic.app.booking", "kind": "aggregate",
    "central_entity_id": "app.booking", "grain_keys": ["id"],
    "preaggregations": [{"metric_id": "...", "output_alias": "a_count",
      "relationship_key": "...", "scope_note": "按关联键预聚合"}]}'

# 2) 质量验证（独立基准对账；报告绑定指纹）/ 预览（脱敏有界）
curl -s -X POST "http://127.0.0.1:8000/api/v1/datasets/<did>/validate?version=1"
curl -s "http://127.0.0.1:8000/api/v1/datasets/<did>/preview?version=1&max_rows=20"

# 3) 发布（view/ES；需先启用目标配置；并发冲突 409）/ 刷新 / 回滚 / 核对
curl -s -X POST "http://127.0.0.1:8000/api/v1/datasets/<did>/publish?version=1" \
  -H 'Content-Type: application/json' -d '{"operator":"dev"}'
curl -s -X POST http://127.0.0.1:8000/api/v1/datasets/<did>/refresh \
  -H 'Content-Type: application/json' -d '{"operator":"dev"}'
curl -s -X POST http://127.0.0.1:8000/api/v1/datasets/<did>/rollback \
  -H 'Content-Type: application/json' -d '{"target_version":1,"operator":"dev"}'
curl -s -X POST "http://127.0.0.1:8000/api/v1/datasets/<did>/reconcile?version=2"

# 4) 修订产生新版本（旧 SQL/质量报告不背书新定义）/ 版本与运行记录
curl -s -X POST http://127.0.0.1:8000/api/v1/datasets/<did>/revise \
  -H 'Content-Type: application/json' -d '{"base_version":1, "filters": [...]}'
curl -s http://127.0.0.1:8000/api/v1/datasets/<did>/versions
curl -s http://127.0.0.1:8000/api/v1/datasets/<did>/runs
```

约束：SQL 只由结构化定义经确定性编译产生（AST 白名单：表/列/函数；
DDL/DML/多语句拒绝）；预览只读脱敏；一跳明细预聚合保持中心 grain
（5×5/3 不膨胀，独立基准对账）；质量未通过/报告指纹不匹配/依赖失效
不能发布；发布/回滚指针原子切换；外部成功本地失败 →
needs_reconciliation 显式核对恢复。**视图/ES 发布目标默认关闭**
（实现经离线适配器闭环验证，真实发布未验证）。演示脚本
`python scripts/m5_real_demo.py`。

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
- [x] M3：跨表关系发现与验证（候选召回/有界只读验证/审核/过期重验/关系图与路径；真实只读演示通过，见交接报告）
- [x] M4：业务实体与语义层（主题/实体/指标/路径、版本与发布/回滚、JSON/YAML 导出；真实+合成演示通过，见交接报告）
- [ ] M5：视图/宽表生成和质量校验
- [ ] M6：NL2SQL、安全执行和结果验证
- [ ] M7：查询型智能体
- [ ] M8：产品化、版本、漂移和运维治理
