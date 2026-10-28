# 开发交接报告

更新时间：2026-10-28（M3 修复轮：跨快照链语义、共享并发额度、SQL 语句级预算与期限、JOIN 成本门槛、组合键去重碰撞；真实只读复验通过）。本报告记录实际环境和验证结果，后续开发仍需按改动复验。

## 当前环境

- 使用现有 Conda 环境 `nl2sql`，实际 Python **3.13.18**，保留当前环境，不降级到 3.12。
- 本机解释器：`/Users/apple/miniconda3/envs/nl2sql/bin/python`；其他机器激活自己的同名环境。
- 依赖：**PyMySQL 1.2.3**（MySQL 驱动）；**httpx 0.28.1 转为运行时依赖**（DeepSeek 客户端
  使用，未引入供应商 SDK）。其余与 M0 基线一致（fastapi 0.162.0 / sqlalchemy 2.1.6 等）。
  **M3 未引入任何新第三方依赖**（纯标准库 + 既有 fastapi/sqlalchemy/pymysql）。

## M3 修复轮（2026-10-28，五组问题全部先复现后修复）

### 1. 跨快照有效关系选择与重验【P1】

- **根因**：仓储先跨快照按逻辑键解析有效修订、再过滤 snapshot_id——旧快照的 stale/
  人工修订遮挡新快照候选；重验沿用旧快照 table_id 比较新结构，永远 mismatch。
- **修复**（`sqlite_metadata_repository.py` / `relationship_service.py` /
  `ports/repositories.py` / 路由）：同一逻辑键的不同快照是**独立证据链**——
  查询一律**先限定快照，再链内解析有效修订**（`list_relationships` /
  `get_effective_relationship(key, snapshot_id)`）；默认列表/详情/图/路径统一
  当前发布快照规则，不回退借用旧快照 validated 证据；旧快照 stale/人工确认/拒绝
  不遮挡新快照候选、也不未经审核继承为新快照结论（同结构新快照链从 candidate 起步）；
  历史修订全保留（`?snapshot_id=` 显式查询）。**重验明确选择目标快照**（默认发布快照，
  部分快照需 allow_partial 且记录 input_complete=false）：目标链存在则在该链验证；
  不存在则按两端 (schema, table, columns) 在目标快照重绑后验证（证据绑目标快照）；
  表/字段消失明确报 400 不可重验。**expected_revision 按目标快照链校验**（修订号
  全局单调分配；跨链并发由 (key, revision) 唯一约束安全仲裁，可能表现为安全侧 409）。
- **回归**：`TestCrossSnapshotChains`（9 项）——旧 validated→加列→新候选可见→重验；
  同结构不继承；旧人工不遮挡 + 新链独立审核；链内冲突域；表/字段消失不可重验；
  默认 vs 显式历史查询；图/路径不混快照；跨快照幂等。

### 2. 共享验证并发限制【P1】

- **根因**："串行循环"只约束单请求内部；两个并发请求探查峰值达 2。
- **修复**（`relationships/execution.py` 新增 + 服务）：`ValidationSlotLimiter` 进程内
  共享信号量（按上限值复用，同一进程所有服务实例共用）；额度覆盖**打开源库连接、
  结构核对与数据探查全程**（等待期间不占用连接组）；单条/批量/重验同一入口；
  等待受运行期限约束（超时 `concurrency_slot_timeout` 收尾 failed，未执行任何查询）；
  异常/超时/连接创建失败 finally 释放。**作用范围=单进程**：多进程部署（多 worker）
  各自独立，不构成跨进程全局保证（文档明示；跨进程仲裁需外部协调服务，本轮不引入）。
- **回归**：`TestSharedConcurrencyLimit`（6 项，真实线程）——同实例/不同实例两请求
  峰值=1；单条+批量+重验交错峰值=1；首请求异常释放额度后第二请求成功；
  等待期间期限耗尽；limiter 超时语义。

### 3. 查询数与期限预算覆盖完整过程【P1】

- **根因**：结构核对（连接器 Inspector，每表多条 SQL）完全未计数（预算 1 时实际执行
  2 次元数据调用）；期限只在查询前检查，证据查询返回超期后策略仍可发布 validated。
- **修复**：`StatementCounter` 经工厂参数注入连接器与探查（SQLAlchemy
  `before_cursor_execute` **逐语句计数**，含 Inspector 内部语句与失败尝试；语句发出
  即计数）；运行预算口径改为**实际 SQL 语句数**（run 字段 `queries_used`=
  语句数、budget `max_sql_statements`；证据 queries 逐条记 `sql_statements`）；
  操作组（结构核对/每条聚合）开始前按**估计下界准入**（不足即不启动该组，不执行
  "最后一组"），完成后按实际核销；同表结构核对**运行内缓存**（仅本运行有效，不跨
  运行信任过期结构）；单调时钟截止贯穿**等待额度→打开连接→每组查询前→请求超时取
  剩余期限→每条查询返回后→判定前**；超期证据标记 `deadline_exceeded`，
  `decide_status` 阻断升级 validated（已有有效关系不受失败/超期运行污染）。
- **回归**：`TestBudgetAndDeadlineCoverage`（7 项，可控时钟/替身）——总预算 1 结构
  核对不绕过（0 语句执行）；结构核对按实际语句计数；多候选共享语句预算；结构核对
  阶段超期；指标查询返回后超期不升级；发布前超期；预算耗尽收尾且旧结果不变。

### 4. JOIN 成本门槛与重复匹配规模控制【P1】

- **根因**：索引可用性用**无序集合包含**判断——组合索引 (tenant_id, code) 仅以 code
  连接也放行（缺前导列索引不可用）；左窗 JOIN 完整右表，非唯一右键可产生巨大匹配量，
  一行聚合结果不代表工作量有界。
- **修复**（`evidence.py::_join_cost_mode` + 探查）：索引判断改为**按序前导列前缀**
  匹配；完整右表 JOIN **仅当右端连接键结构唯一**（主键/唯一索引恰为连接列，自带
  可用前导索引，每左行 ≤1 次索引定位）；其余一切情形（非唯一右键/缺前导列/无索引/
  行数未知或低估或超大）一律**双侧有界窗口 JOIN**（各 ≤W 行、中间结果 ≤W×W 对，
  受控计数，口径降级 sample 并注明）；**不再以行数估计作为完整表 JOIN 的安全依据**
  （估计是优化器近似值且可能低估，不是硬上限；估计只作证据字段记录）；数据库
  执行期限与运行期限保留。修复中另发现并修复窗口化右端派生表缺 presence 列导致
  的真实 MySQL `Unknown column` 错误（真实库复验抓出）。
- **回归**：`TestJoinCostMode`（6 项）——组合唯一键完整映射 full；缺前导列 windowed；
  唯一键前导可用 full / 非唯一右端 windowed；大量重复键右端请求确实受限（≤W）；
  行数低估不作为安全依据；小表正常关系 full+exact。

### 5. 组合键去重碰撞【P2】

- **根因**（真实 MySQL 只读常量查询复现）：`CONCAT_WS(CHAR(31), ...)` 拼接去重在
  字段值包含 CHAR(31) 时碰撞——两个不同元组拼接后同为 1 个值（应为 2）。
- **修复**（`mysql_probe.py`）：全部改用**原生多列 `COUNT(DISTINCT a, b)`**
  （窗口 distinct_values、组合主键 left_rows_seen、distinct_right_matched 三处；
  右端去重逐列 CASE 包裹，presence NULL→全列 NULL→被多列 DISTINCT 排除）。
  原生语义：按元组比较、任一列 NULL 的行自动排除（与窗口"全列非空"口径一致）、
  字段顺序与原始类型保留。不用哈希替代。
- **回归**：替身元组语义（CHAR(31) 边界/NULL/空串/真重复）；**SQL 构造断言**
  （三处均无 CONCAT_WS、含原生多列 DISTINCT；窗口化右端为有界派生表）；
  **真实 MySQL 常量 SELECT 测试**（native=2 vs concat=1 碰撞复现；NULL/空串/
  数字-字符串混合语义）——不建表、不读业务数据。

### 验证（修复轮后）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **531 passed, 9 skipped**（修复轮新增 34 项离线回归；skipped=默认关闭的真实集成） |
| `python -m ruff check app tests scripts` / `pip check` | 全部通过 |
| `NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q` | **8 passed, 1 skipped**（新增 CHAR(31) 常量 SELECT 语义测试；1 skipped 为 M2 真实模型项） |
| 真实只读演示（hotel，临时目录） | 18 表扫描 → 67 候选 → 5 候选验证 **sql_statements=34**（语句级如实计数）→ 1 validated + 4 needs_review（阻断原因如实）→ 审核/stale/重验/图闭环 |

### 口径纠正（覆盖此前不准确描述）

- ~~"并发=1 由串行循环保证"~~ → **进程内共享并发额度**（单进程 scope；多进程部署
  各自独立，不构成跨进程保证）。
- ~~"每候选最多 4 条聚合查询"~~ → 每候选 ≤4 次**探查调用**，另加结构核对
  information_schema 查询（真实库约每表 5-8 条语句，运行内同表缓存）；预算与
  `queries_used` 一律按**实际 SQL 语句数**计。
- MariaDB 无 `MAX_EXECUTION_TIME`：回退**客户端 read_timeout——是放弃读取，
  不等于服务端取消语句**（支持范围已在配置文档明示）。

## M3 跨表关系发现与验证（2026-10-28 完成）

### 已实现能力

| 能力 | 实现 |
| --- | --- |
| 领域契约 | `app/domain/relationships.py`：稳定逻辑键（relationship_key=数据源+两端完整标识+有序映射，方向敏感，跨快照稳定）、状态机 candidate→evidence_collected→validated/needs_review/rejected + stale/重验、有效结果解析（人工终局不被机器覆盖；人工/系统 stale 必须改变有效结果；人工 mark_stale 是重验指令非终局判定） |
| 候选召回 | `app/relationships/discovery.py`（rel-recall-v1）：显式外键（含组合键有序映射/自关联/悬空外键报告）+ 分桶信号召回（禁止笛卡尔积）：引用方特征（M2 foreign_identifier_candidate/标识符命名/非通用 token）× 被引用方特征（主键/单列唯一索引/entity_identifier），桶=非通用 token（含表名词法与轻量复数变体，method=lexical）；每列 Top-K、总数预算、单调时钟期限；类型兼容矩阵先行过滤（incompatible 不召回——编码映射走人工登记；risky 保留带风险） |
| 类型兼容 | `app/relationships/type_compat.py`：族/符号/精度/标度/长度解析；int 宽度与符号、decimal 精度、varchar 长度、date vs datetime 粒度、text 不可比 → compatible/risky/incompatible/unknown + 原因 |
| 只读证据 | `app/relationships/evidence.py` + `app/adapters/mysql_probe.py`：专用 `RelationshipProbe` 端口（ports/connectors.py 类型化 Spec），系统构造聚合查询（窗口统计 + LEFT JOIN 聚合），标识符白名单转义、无业务参数、只返回计数；验证前源库结构 vs 输入快照核对（不一致→stale 不绑定证据） |
| 指标口径 | 双侧行数/非空/空串/组合去重/常量列（占位风险）+ 双向覆盖率（分母=左窗非空键行，NULL 键排除单列）+ JOIN 膨胀 fanout/INNER 行数 + 基数观察（源×目标唯一性）；每指标记方法/分母/口径（exact=窗口未截断，sample=截断，unknown=成本门槛/预算耗尽）/时间/快照/预算；SQL 只存模板名+预算摘要 |
| 验证策略 | rel-validate-v1：名称相似/语义相似/高样本覆盖率**不能单独**自动升级；自动 validated 需类型兼容 + 前向覆盖 exact≥0.995 + 目标端唯一（结构或 exact）+ fanout≤1 + 支持分≥0.4 + 无阻断风险（组合键缺失/语义被否决/源端低唯一率/结构漂移）；否则 needs_review 逐条阻断原因；validated 附范围/时间/快照/基数/排除项/限制声明 |
| 预算与成本 | 候选数/总查询数/单查询期限（MySQL≥5.7.8 `MAX_EXECUTION_TIME` 服务端提示，MariaDB 回退客户端 read_timeout——支持范围已明示）/运行总期限（单调时钟）/窗口行数/成本门槛（右端行数估计>5 万且连接列无索引→拒 JOIN 降级 unknown）；验证串行执行（并发=1 由执行模型保证）；JOIN 膨胀在聚合上计算不物化 |
| 审核与过期 | confirm/reject/revert + mark_stale + revalidate；expected_revision 仓储事务内原子乐观锁（409，唯一约束多进程仲裁）；机器不覆盖人工；新发现不覆盖人工审核；快照结构指纹（列/类型/PK/FK/索引）比较自动 stale（完整快照表消失也 stale；部分快照缺表不当作消失） |
| 图与路径 | `app/relationships/graph.py`：JSON 图（节点完整标识/边方向+有序映射+状态+基数+风险+证据摘要；候选/拒绝/过期可见且区分）+ Mermaid；受限 DFS 路径搜索默认仅 validated 非 stale 边、仅 source→target 方向、防环、跳数/路径数/节点数/期限预算、多对多与组合缺失风险提示 |
| 持久化 | SQLite 追加两表 `relationships`（唯一约束 relationship_key+revision）/`relationship_runs`（create_all 兼容，不动既有表）；修订式历史；整次运行修订+运行记录单事务提交，**失败运行零残留**（发现/验证同 M2 语义） |
| API | `app/api/routes/relationships.py`（前缀 /api/v1）：POST discover / validate（批量）/ relationships/{key}/validate / review / stale / revalidate；GET 列表（状态/快照过滤）/详情/历史/运行/图（?mermaid）/路径。默认发布快照，部分目录需 allow_partial 且 input_complete=false |

### 召回与验证的负例防线（要求第四节逐项落地）

同名不同义（M2 类别冲突风险标记）；共享状态码（status/category 类别列不作关系信号）；
不同租户重复编号（源端低唯一率风险阻断 + validated 附租户分层提示）；组合键需求
（目标组合主键且匹配列非独立唯一→组合键风险阻断自动验证）；编码映射（跨族类型
incompatible 不召回，人工登记保留）；时间条件缺失（多对多基数附 JOIN 需聚合/条件
约束提示）；同名词典 code↔code（双端均被引用方特征不配对）；高样本覆盖但支持分不足
（coverage 达标仍被 support_score 阻断）。

### 评测结果（合成集，开发标注——非真实业务人工金标准）

标签来源：开发者按合成库设计标注（tests/test_relationships_eval.py；
9 正例 + 6 负例）。**Recall 分母=本合成库标注的全部真实引用关系（9 条），
不是全库任意字段对的召回率**；LLM 基线与 Embedding 基线未实现，未评测
（不为此扩大本轮模型调用）。

| 指标 | 结果 | 说明 |
| --- | --- | --- |
| 召回 Precision | **0.750**（9/12） | 3 条误报均为标注负例（tenant_seq 租户编号、room_id→room_type、room_no→room_type.code 同名词法噪声），验证层全部 needs_review |
| 召回 Recall / F1 | **1.000** / **0.857** | 全部 9 条正例召回（8 显式外键 + 1 无外键 booking_no 词法） |
| 字段名相同基线 Precision | **≈0.04**（2/50） | 同名即候选：id↔id/name↔name/status↔status 大量配对；Recall 0.22（FK 名不同漏召） |
| 高置信错误（自动 validated 但标签为负例） | **0** | 验证层全部 9 条 validated 均为正例 |
| 基数判断准确率 | **1.000**（9/9 many_to_one） | evidence.cardinality_observed 与标注一致 |
| 路径用例 | **通过** | 3 跳 booking_item→booking→guest/room→tenant 可达；自关联防环（employee→employee 0 条）；有向不逆向（booking→payment 0 条） |
| 单候选查询成本 | **≤4 次探查调用/候选**（2 窗口 + 2 JOIN）+ 结构核对语句（真实库约每表 5-8 条，运行内同表缓存）；预算与 queries_used 按实际 SQL 语句数计 | 合成集与真实演示均符合 |

### 真实只读验证（hotel 库，2026-10-28）

`python scripts/m3_real_validation.py`（元数据/凭证写临时目录，预算先行打印，
不打印业务值/凭证；未覆盖任何用户已有审核结论）：

| 步骤 | 结果 |
| --- | --- |
| 扫描（≤20 表 × 200 行采样） | **succeeded**：18 表 planned/scanned，0 失败 0 跳过 |
| 候选召回（纯本地） | **succeeded**：found=new=67，input_complete=true |
| 有界验证（Top-5 × ≤30 查询，窗口 200） | **succeeded**：queries=20；**1 validated + 4 needs_review**（阻断原因如实：4 条 sample 口径 + 源端样本唯一率低 0.10–0.25——共享编码特征，策略拒绝自动升级） |
| validated 示例 | `role_permission.permission_id → permission.id`：前向覆盖 1.0（exact，分母 39）、fanout=1.0、基数 many_to_one |
| 审核/过期/重验闭环 | 人工确认 rev=3 → mark_stale 生效 stale → revalidate succeeded → 恢复 validated |
| 图 | 17 节点 / 67 边（状态分布 candidate 62 / needs_review 4 / validated 1，清晰区分） |

未在真实库上人工标注金标准，以上真实结果仅证明链路与预算执行，不作为准确率结论。

### 测试与验证（Conda `nl2sql`，2026-10-28）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **498 passed, 8 skipped**（M3 新增 123 项离线测试；skipped=6 项 M1/M2 真实集成 + 2 项 M3 真实集成，默认关闭） |
| `python -m ruff check app tests scripts` | **All checks passed** |
| `python -m pip check` | **No broken requirements found** |
| M3 新增测试文件 | domain（键/状态机/有效解析 19）、repository（修订/乐观锁/并发/批原子 15）、discovery（召回/负例/TopK/期限/幂等 19）、evidence（口径/分母/膨胀/占位/门槛/预算/策略 23）、service（编排/审核并发/stale/重验/零残留/安全文案 26）、graph（图/路径/防环/预算 12）、api（端到端 11）、eval（指标/基线/路径/成本 9） |
| 真实集成 | `NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q` → 修复轮后 **8 passed, 1 skipped**（5 项 M1 + 3 项 M3：探查端口有界聚合与脱敏、小预算真实发现验证链路、CHAR(31) 常量 SELECT 语义；1 skipped 为 M2 真实模型项，需另加 NL2SQL_SEMANTICS_INTEGRATION=1，本轮未调用模型） |

### M3 已知限制（如实保留）

- **数据分布漂移不自动识别**：结构漂移（列/类型/约束/索引变化、完整快照表消失）
  会自动 stale；纯数据分布变化（如覆盖率下降）需人工标记 stale/重验或下次验证时
  由策略降级——不声称自动发现所有漂移（定时漂移检测在 M8）。
- 验证并发为**进程内共享额度**（`RELATIONSHIP_MAX_CONCURRENT_VALIDATIONS`，
  默认 1；同一 Python 进程所有服务实例共用信号量，覆盖连接/核对/探查全程）；
  **单进程语义**——多进程部署各自独立，不构成跨进程全局保证，跨进程仲裁需外部
  协调服务（未引入）。
- MariaDB 不支持 `MAX_EXECUTION_TIME` 提示：回退客户端 read_timeout（单查询服务端
  期限仅 MySQL ≥5.7.8 SELECT 支持，范围已在配置文档明示）。
- 编码映射类跨族类型关系（字符串↔整数）不自动召回也不做隐式转换 JOIN；
  人工登记路径（origin=human）预留。
- 图路径为有向（source→target）搜索，不逆向使用边；连通≠业务可 JOIN（不生成
  生产 SQL，M4 范畴）。
- 词法召回无 Embedding（沿用 M2 词法基线，method=lexical，不伪造向量分）。

### M4 可使用的输入

- 有效关系清单（含 validated/needs_review 状态、有序字段映射、基数、证据口径）：
  `GET /api/v1/sources/{id}/relationships?status=validated`（默认发布快照 + effective）；
- 图与路径数据：`GET .../relationships/graph?mermaid=true`、`.../paths`；
- 关系修订历史与审核记录（溯源）；运行记录（预算与查询计数，成本核算用）。

## M2 修复轮 3（2026-10-28，三项小范围修复，全部先复现后修复，完全离线验证）

1. **引号密钥名绕过净化【P1】**：`{"password": "X"}` 形态此前不被键值正则命中
   （键名两侧引号阻断了分隔符匹配）。修复为键名两侧允许可选引号（单/双引号、
   JSON 嵌套、空白、大小写、Authorization/Bearer 均覆盖），值仍完整遮蔽到句界；
   普通非敏感 JSON 语义不变；样本开关/版本检查/默认不外发规则不变。
   回归：`TestFixRound3QuotedSecretKeys`（6 组引号形态 + 正常 JSON + 表/列注释
   进入提示词的端到端断言：密钥值与其尾段均不出现）。
2. **合法路径证据被提前拒绝【P2】**：`_resolve_column_evidence` 的标准形态分支
   先于路径解析执行且各自提前 return None，导致 `constraints.primary_key:id`
   等合法路径永远走不到路径解析。修复为“先规范化路径/约束形态 → 再统一执行
   归属与值校验”，标准/值/路径形态共用同一套真实性检查（主键/外键/索引/画像
   指标不放宽；跨字段、虚构对象、错误值、None/skipped 指标继续拒绝；规范引用
   非空不变量保留；字段与表用途画像路径使用一致的数值等值规则）。
   回归：`TestFixRound3PathEvidence`（主键/外键/类型/注释/画像指标的合法与
   非法成对用例 + 标准引用兼容）。
3. **失败路径用量完整性与零尝试计数【P2】**：异常分支此前传入 `_account_usage`
   的尝试数为 0 且把 attempts=0 强制记为 1。修复为使用异常携带的实际 attempts
   （0 保留为 0；旧适配器缺属性时按 1 保守计数），多次尝试中存在无法确认用量
   的请求时整体与分项完整性标记 False，已知 token 小计保留不重复累计；未修改
   客户端重试策略与预算定义。
   回归：`TestFixRound3FailureAccounting`（429×2→带 usage 非法 JSON、零尝试、
   单次失败用量完整、单侧已知、旧适配器兼容五场景）。

验证：`python -m pytest -q` **375 passed, 6 skipped**（本轮全部离线，未访问
真实数据库或模型）；ruff / pip check 通过。无接口兼容变化；M2 保持 partial。

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
| `python -m pytest -q` | **375 passed, 6 skipped**（修复轮 3 后；skipped 为真实集成，默认关闭；无残留文件） |
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
