# 跨阶段数据契约

本文件定义模块之间的稳定输入输出。需要修改契约时，先说明兼容性和迁移影响。

## 数据源
- `source_id`、名称、方言、主机/端口/数据库、允许 Schema（`allowed_schemas`，空则默认数据源自身库）
- 凭证使用 `credential_ref`，不在普通元数据表中保存明文密码
- `read_only=true` 是默认约束，写入型数据源直接拒绝注册
- M1 起数据源卡片额外返回 `table_count / column_count / latest_scan`

## 扫描任务（M1 新增；修复轮调整）
- `job_id`、`source_id`、`snapshot_id`、`started_at / finished_at`
- 状态：`running → succeeded | partial | failed`；
  `failed`=结构性失败（连接/凭证/Schema 不可见，或预算内表全部失败），
  `partial`=存在表级失败或被预算截断（缺失表不当作确认删除），
  `succeeded`=完整成功（此时确认空 Schema 可发布空目录）
- 任何任务级异常都会收尾为 `failed` + `finished_at` + 已清洗的错误原因
  （`internal_error` / `credential_store_error` 等）；表级可恢复异常按表隔离
  （`table_scan_error`），持久化等基础设施故障中止整单扫描
- 计数：`tables_planned / tables_scanned / tables_failed / tables_skipped`，预算参数
  `sample_rows / max_tables` 一并落库以便复现
- 错误明细 `errors[]`：`schema / table / reason / message`，`message` 已清洗（剔除凭证、
  截断长度），不含原始样本

## 目录发布（修复轮新增契约）
- 任务状态与目录发布分离：**默认目录＝最近一次 succeeded 快照**；无完整成功时回退最近
  一次 `partial`（`tables_scanned>0`）快照并标记 `complete=false`；
  `failed/running` 快照永不发布
- 因权限、采样失败或预算跳过而缺失的表不得当作确认删除；只有下一次完整成功快照
  才决定目录内容（完整成功确认空 Schema 时发布空目录）
- 历史快照全部保留（不删除历史数据规避问题）；`GET /sources/{id}/tables?snapshot_id=`
  可查看任意历史快照用于诊断
- 数据源卡片同时暴露 `latest_scan`（最近任务）与 `catalog`（目录实际来源
  `{snapshot_id, status, complete}`），避免两者来源混淆

## 表与字段元数据
- 数据源、Schema、表、字段标识、表注释、归属快照（`snapshot_id` 随目录项返回）
- 字段类型、注释、可空、主键/外键、索引（名称、字段列表、唯一性）、字段默认值
- 行数来自 `information_schema.TABLES`，`row_count_is_estimate` 恒为 true（InnoDB 估计值）
- 同一快照内按（snapshot, schema, table）去重；重复扫描生成新快照
- 外键统一为 `{name, columns, ref_schema, ref_table, ref_columns}`，`ref_schema` 为空时回退本表 Schema

## 字段画像
- `null_ratio`、`distinct_count_estimate`、`uniqueness_ratio`（均基于采样计算，按原始值统计）
- `sample_values`（脱敏，最多 5 条）：两层检测——列名关键词（内置 + `SENSITIVE_COLUMN_KEYWORDS`
  追加）整列遮蔽；与列名无关的值格式检测（邮箱/大陆手机号/18 位身份证）命中即脱敏
  （整值命中部分遮蔽，嵌入自由文本则整值遮蔽）
- 允许原样展示的类型仅限：数字、布尔、日期时间形式短字符串、不含敏感格式且 ≤64 字符的
  短字符串；长文本/JSON 保守遮蔽；二进制仅返回长度。脱敏发生在持久化与 API 响应之前
- `samples_redacted` 如实反映任何实际脱敏（含值格式检测与保守遮蔽）
- 口径与范围：`metric_basis`（`exact`=采样覆盖整表 / `sample`=样本估计 /
  `insufficient_sample`=空表等无数据 / `skipped`=超预算未计算）、
  `sample_row_count`、`row_count_basis`、`computed_at`、`profile_version`
- `empty_string_count` 区分空字符串与 NULL；`unsupported_metrics` 列出未计算的指标，
  不支持或样本不足时保持 `None`，不虚构结果
- **历史数据**：规则升级不自动修复已持久化画像；本地清理脚本
  `scripts/clear_profile_samples.py` 只清空历史 `sample_values` 并标记
  `sample_values_cleared=true`（需重新画像），保留统计与元数据

## 字段语义
- 来源表/字段、建议业务名称、语义类型、候选实体
- 支持证据、替代解释、置信等级
- 模型/提示词版本、审核状态
- 必须允许 `unknown`

## 字段语义（M2 新增）

- 标识：`source_id / snapshot_id / table_id / column_name`（不跨快照混用）、`run_id`、`revision`
- 内容：`original_name / original_comment`、`suggested_business_name`、`category`
  （注册表枚举，见 `app/domain/semantics.py`）、`candidate_entities`、`alternatives`
  （多候选解释）、`confidence`（low/medium/high，**候选判断强度，不是校准概率**）
- 证据：`evidence[] = {type, ref, detail}`，type ∈ column_type/comment/primary_key/
  foreign_key/index/profile_metric/name_pattern/glossary_term；服务端核验引用真实存在
  （支持标准与 `columns.<列>.<字段>` 路径形态并规范化）；仅不可核验证据不足以支撑
  非 unknown 结论；模型解释不构成新增事实
- 来源与版本：`inference_source`（rules/model/human）、`model_name`（服务端返回，缺省
  unavailable 不编造）、`prompt_version`、`rules_version`、`created_at`
- 审核：`review_status` ∈ suggested/business_confirmed/rejected；允许转换
  suggested→confirmed/rejected、confirmed/rejected→suggested(revert)；
  人工修订携带 `expected_revision`，**版本比较与新修订写入在仓储同一事务内原子完成**
  （并发同版本提交恰一个成功，另一个 409；唯一约束为多进程最终仲裁者）；
  **模型建议不覆盖有效的人工确认；人工 revert 可终止自己此前的确认**（撤销后
  生效最新修订），两者独立处理；全部历史保留；
  rejected 不参与检索加权与确认类输出，但保留在历史中；
  人工确认 ≠ M3 关系验证 ≠ M4 生产发布；reviewer 仅为本地开发输入，无身份认证
- 证据边界：字段证据必须归属**当前字段自身对象**（类型/注释/主键/外键/含本列的
  索引/本字段已计算画像指标），支持标准、路径（columns.<列>.<字段>[:值]）、
  值（类型串/metric=value）三种形态并统一核验规范化；引用其他字段的对象无效；
  画像指标需值非 None 且口径可用（skipped/insufficient 不可作证据）；
  引用存在 ≠ 结论正确（rationale 如实表述）；**模型提供的 name_pattern/
  glossary_term 上下文证据（任意文本）一律丢弃**（规则引擎自产证据不经此路径），
  仅可核验直接证据支撑非 unknown 结论；指标值等值为类型明确的数值比较
  （容差 1e-9，不做字符串包含）；表用途输出为结构化对象
  （purpose/confidence/rationale/evidence/alternatives），画像证据必须以
  columns.<列>.profile.<指标>[:=值] 定位实际字段的已计算指标（裸指标名不通过），
  规范引用为 <列>.<指标>；上层保证规范引用非空（不保存 ref=null 的证据）
- 运行记录：`mode`（rules_only/model_assisted）、`input_complete`（显式快照按对应
  扫描任务真实性核定，不无条件视为完整）、`status`、用量计量（`llm_calls`=逻辑调用、
  `llm_http_attempts`=总 HTTP 尝试（计数在请求发起点，所有失败路径携带累计值）、
  token 为**所有返回过 usage 响应的累计**含结构不合格者（usage 在内容解析前提取、
  失败随异常携带不丢失）、`llm_prompt_usage_complete/llm_completion_usage_complete`
  分项表达完整性（已知一侧保留、缺失一侧如实标记；同逻辑调用内含传输重试时
  早期尝试用量不可确认→完整性 False 但保留已知小计）、`model_reported` 只填服务端
  返回值且与 `model_configured` 分列、运行期限为单调时钟绝对截止（贯穿读取与退避，
  超期 deadline_exceeded 且已发生请求如实计数）、`send_samples`、安全错误明细；
  **发布规则**：整次分析结果（字段+表用途+运行记录）单事务提交，failed 运行
  不产生部分有效结果；模型失败回退规则的 partial 运行可发布（字段来源标记 rules）

## 表用途分类（M2 新增）

- 枚举：master_data / transaction_event / detail / dictionary / log_history / relation / unknown
- 字段结构与修订/审核语义同字段语义；`alternatives` 保留竞争候选

## 语义检索（M2，本地词法基线）

- 入参：`source_id`、概念词 `q`、可选 `snapshot_id`/`limit`；默认限定 source + 发布快照
- 返回：`method=lexical`（明确标记检索方式，不称向量相似度）、`snapshot_id`、候选字段
  （含 `business_name/category/review_status/score/match_basis`）
- 计分：人工确认业务名(3.0) > 建议名(2.0) > 原名(2.0) > 注释/候选实体(1.5) >
  类别(1.0) > 表名(0.5)；Embedding 向量检索为未完成项，`EmbeddingClient` 端口保留

## 关系候选（M3 已实现）

- 身份：`relationship_key` 为**跨快照稳定的逻辑键**（数据源 + 两端完整标识 +
  有序字段映射，方向敏感；A→B 与 B→A 是不同键）；`snapshot_id` 标记本修订
  证据所基于的输入快照——**同一逻辑关系在不同快照是独立证据链**（修复轮：
  查询先限定快照再链内解析有效修订；默认列表/详情/图/路径=当前发布快照，
  不回退借用旧快照 validated 证据；旧链 stale/人工确认/拒绝不遮挡新链候选、
  也不未经审核继承为新链结论；历史修订全保留，`?snapshot_id=` 显式查询）；
  字段身份是完整标识（schema+table+columns），不能仅凭列名合并；
  修订号全局单调分配，expected_revision 按目标快照链原子校验（跨链并发由
  (key, revision) 唯一约束安全仲裁）；**重验明确目标快照**（默认发布快照；
  部分快照需 allow_partial 且 input_complete=false）：目标链存在则链内验证，
  不存在则按两端完整标识重绑后验证（表/字段消失报不可重验，不拿旧
  table_id 比较新结构）
- 端点与方向：source 端=引用方（外键方），target 端=被引用方；组合键使用
  有序字段映射（source_columns[i] ↔ target_columns[i]），显式组合外键不拆成
  单列关系；自关联带风险标记（路径搜索防环）
- 来源 origin：explicit_fk / name_rule / lexical_semantic / profile_feature /
  human；召回理由逐条记录；词法相似标记 method=lexical（无 Embedding，
  不伪造向量分）；M2 rejected 语义不作正向证据，人工确认加权
- 基数：one_to_one / many_to_one / one_to_many / many_to_many / unknown；
  基数=源端唯一性×目标端唯一性，方向不混淆（前向/反向分别记录）
- 评分：分项（structural/naming/semantic/profile）+ 加权总分 + 有序等级
  （rel-scoring-v1）；**等级是相对强度，不是校准概率**
- 状态机：`candidate → evidence_collected → validated / needs_review / rejected`；
  `stale`（结构/快照变化或人工标记）与重验；机器不产出 rejected；
  人工专属：rejected、revert 回 candidate、显式标记 stale
- 审核语义（沿用 M2）：expected_revision 仓储事务内原子乐观锁（并发同版本
  恰一成功→409，唯一约束为多进程仲裁者）；机器修订不覆盖人工终局判定
  （validated/rejected/revert）；人工可撤销自己的确认；**人工拒绝/撤销/过期
  必须改变有效结果**；人工 mark_stale 是“需重验”指令而非终局判定，
  其后的重验/重发现结果重新接管有效结果
- 幂等与残留：同一快照内重复召回同一关系（同签名）幂等跳过；整次运行的
  关系修订+运行记录单事务提交，失败运行零残留（不发布部分“已验证”结果）

## 关系证据与验证（M3 已实现）

- 采集方式：专用 `RelationshipProbe` 端口（类型化 Spec → 系统构造聚合查询），
  **不是任意 SQL**；标识符来自快照元数据白名单并转义，无业务参数值，
  只返回聚合计数；执行前核对源库结构与输入快照一致，不一致不绑定证据
  （转 stale，需重扫后重验）
- 指标口径：每个指标记录方法/分母/采样范围/时间/快照/口径
  （exact|sample|unknown）/是否截断/预算与过滤条件；SQL 只保存模板名与
  预算参数摘要
- 分母约定：行覆盖率分母=左窗非空键行数（NULL 键行排除并单独报告）；
  唯一值覆盖率分母=非空行数；JOIN 输出行数（fanout）单独记录——三种
  分母不混用；NULL 不参与匹配；空串/占位值不计入有意义匹配（也不当 NULL）
- 窗口语义：窗口行数 < limit（未截断）→ exact；达到 limit → sample；
  成本门槛（右端大表且连接列无索引）/预算耗尽 → unknown（gated 记原因）；
  两边均 sample 时未匹配可能来自样本缺失，不据此否定关系；
  JOIN 膨胀在聚合上计算（COUNT/DISTINCT），不物化多对多结果
- 验证策略（rel-validate-v1）：名称相似、语义相似或高样本覆盖率均不能
  单独自动升级 validated。自动 validated 需同时满足：类型兼容（typed 等值
  比较，不做隐式转换）、前向覆盖 exact 且 ≥ 阈值（默认 0.995）、目标端唯一
  （结构或 exact 口径）、JOIN 无膨胀（fanout≤1）、支持分 ≥ medium、无阻断
  风险（组合键缺失/语义被否决/源端低唯一率/结构漂移）；否则 needs_review
  并逐条给出阻断原因。validated 附范围声明（快照/时间/口径/基数/排除项/
  限制），不代表无条件或无限期正确
- 预算（修复轮口径）：按**实际执行的 SQL 语句数**核销（结构核对
  information_schema 查询与窗口/JOIN 聚合逐语句计数，语句发出即计数、失败
  尝试计入；一次含多条 SQL 的方法调用不记为 1）；操作组开始前按估计下界准入
  （不执行"最后一组"），完成后按实际核销；同表结构核对运行内缓存（不跨运行）；
  单查询期限（MySQL ≥5.7.8 服务端 MAX_EXECUTION_TIME 提示；MariaDB 回退
  客户端 read_timeout——**是放弃读取，不等于服务端取消语句**）；运行总期限
  （单调时钟）贯穿等待并发额度→打开连接→每组查询前→请求超时取剩余→每条查询
  返回后→判定前；**超期证据（deadline_exceeded）不得升级 validated**；
  预算/期限/异常均正常收尾，不停留 running，已有有效关系不受污染
- 并发：**进程内共享额度**（同一进程所有服务实例共用；覆盖打开连接/结构核对/
  探查全程；等待受运行期限约束）；**单进程语义**——多进程部署各自独立，
  不构成跨进程全局保证
- JOIN 匹配规模（修复轮）：完整右表 JOIN 仅当右端连接键**结构唯一**（主键/
  唯一索引恰为连接列，按序前导可用，每左行 ≤1 次索引定位）；其余（非唯一右键/
  组合索引缺前导列/无索引/行数未知或低估或超大）一律**双侧有界窗口 JOIN**
  （各 ≤W 行、中间结果 ≤W×W 对，受控计数，口径降级 sample）；**不以行数估计
  作为完整表 JOIN 的安全依据**（估计为优化器近似值，只作证据字段记录）；
  组合键去重用**原生多列 COUNT(DISTINCT a, b)**（CONCAT_WS 分隔符拼接在值
  含 CHAR(31) 时碰撞——真实 MySQL 常量查询已复现并修复）
- 过期：新快照结构指纹比较（列/类型/主键/外键/索引）变化或完整快照中表
  消失 → 追加 stale 修订（部分快照缺表不当作消失）；人工可显式标记 stale
  与重验；无法验证的数据分布漂移明确列为限制（不声称自动识别所有漂移，
  定时漂移检测在 M8）

## 关系图与路径（M3 已实现）

- 图数据：节点=表（完整标识）；边含方向、有序字段映射、状态、基数、风险
  与证据摘要；候选/拒绝/过期边可见且状态清晰区分；默认限定单数据源单快照
- 路径搜索：默认仅沿**有效且非 stale 的 validated 边**（ADR-002：未验证
  关系不得默认用于生产查询路径）；仅沿 source→target 方向行进；限制最大
  跳数/路径数/搜索节点数/期限；同路径防环；多对多与组合条件缺失的边
  附风险提示；图连通不等于业务 JOIN 正确，不生成生产 SQL（M4 范畴）
- 提供 JSON 图数据与 Mermaid 文本（`?mermaid=true`）

## 关系候选（历史约定，保留参考）
- 源表/字段、目标表/字段、候选基数
- 分项证据、评分方法/版本、状态
- 验证 SQL 摘要、验证结果、过期条件
- `candidate` 不等于 `validated`

## 业务语义模型
- 实体、属性、关系、指标、维度和查询路径
- 每个主题有明确粒度
- 指标含表达式、去重规则、过滤条件和时间口径
- 来源、状态、版本和权限策略

## 数据集定义
- 名称、主题、grain、一行代表什么
- 来源表、关系路径、字段和指标
- 生成 SQL、质量检查、刷新策略
- 草稿/校验中/已发布/失败/过期状态和版本
- ES 宽表实现时需补充目标索引/别名、文档主键与 grain 的映射、字段 mapping、刷新与版本切换信息；当前 `DatasetSpec` 尚未包含这些目标存储字段

## NL2SQL 结果
- 问题和解析出的意图
- 使用的实体、指标、关系和口径假设
- SQL、安全检查状态、执行状态、结果摘要
- 解释、耗时、trace_id 和反馈

## 状态约定
关系：`candidate → evidence_collected → validated / needs_review / rejected`；源数据变化后可转 `stale`。
语义：`suggested → data_validated → business_confirmed → published`。
数据集：`draft → validating → published`；校验失败为 `failed_validation`。
