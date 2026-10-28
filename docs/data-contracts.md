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

## 关系候选
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
