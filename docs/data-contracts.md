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
  table_id 比较新结构）；**重绑候选依据全部按目标快照重算**（类型兼容/
  外键存在性/结构唯一性/组合键风险/证据/来源/基数/评分及版本；无外键不标
  explicit_fk；缺画像/语义按缺失处理不借用旧快照；无法常规召回时保留为显式
  重验请求并标记来源；类型跨族不执行 JOIN、不得自动 validated；不继承旧链
  审核人/确认/验证结论——旧依据只保留在旧链历史中）
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
- 预算（修复轮 2 口径）：**逐语句硬限制**——每条 SQL 发出前原子检查并消耗
  额度（StatementBudget.allow_one，不足即拒绝执行，语句不到达数据库）；
  Connector/Probe 共享同一运行预算，结构核对 Inspector 多条 SQL 同样受控；
  已发出的失败语句消耗额度（不退回、不重复计数）；被拒语句不计入已执行
  （queries_used=已发出；sql_statements_rejected=被拒）；操作组估计下界准入
  仅为优化（不能替代硬限制）；同表结构核对运行内缓存（不跨运行）；
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

## 业务语义模型（M4 已实现，semantic-model-v1）

- 身份：字段=FieldRef(schema,table,column) 完整结构化引用；组合键/映射为
  **有序字段列表**；实体 entity_id=schema.table（跨 Schema 同名表不合并；
  不按同名列合并字段）；一次构建限定一个 source+snapshot（默认发布目录
  快照；部分快照需显式 allow_partial 且 input_complete=false；显式快照
  校验归属且部分快照同样受门槛）
- Entity/Attribute/Dimension：一表一实体候选（保守，不自动合并）；属性
  消费 M2 effective（rejected 不作正向命名/分类；缺失按 unknown；回退
  原始名注明来源）；维度带允许用途（filter/group/order）；敏感列打标签
  不进维度
- grain：业务描述 + 中心表与粒度键结构化引用 + 依据（primary_key/
  unique_constraint/unknown）+ 确认状态；样本唯一率不作全表唯一依据；
  “每行一条记录”不算已确认业务粒度
- Relationship：引用 M3 已验证关系（relationship_key+revision+两端完整
  身份+有序映射+基数+证据范围），不存自由文本 JOIN；仅同快照 effective
  validated 非 stale；候选/拒绝/stale 不进入模型
- Metric：稳定标识/名称/描述/来源实体 + 受限聚合（count_rows/
  count_distinct/sum/avg/min/max）+ 结构化字段引用 + 去重键（组合键元组
  语义）+ 受限过滤操作符与类型化值（不接受任意 SQL/Python/eval；展示
  表达式为派生）+ 时间字段/时间口径/单位 + 状态四态
  （suggested→data_validated→business_confirmed→published）；数据验证与
  业务确认两维证据分立；无对账不产生 data_validated；未确认指标不进入
  正式集合（formal_metric_ids）；口径修改产生新版本
- QueryPath：复用 M3 find_paths（source→target 正向，不逆向）；保存有序
  实体/关系修订/字段映射/基数/风险/输入快照/搜索完整性；many_to_many
  阻断；同对多路径=歧义待审（不默认取最高分）；基数未知待审；预算截断
  如实标记（不排除存在其他路径）
- 版本与发布：模型版本**不可变**（修订/确认产生新版本；并发同基线由
  (model_id,version) 唯一约束仲裁→409）；发布=指针原子切换+审计（不发布
  数据集、不执行 SQL）；发布前复查（grain 确认/正式指标口径/引用有效/
  无未处理阻断/非未许可部分快照/依赖未降级/输入快照仍为当前发布快照）；
  回滚核对目标当前可用性；失败不产生半个模型、不破坏原指针
- 依赖与可用性（修复轮）：依赖清单记录 **M3 关系修订 + M2 字段语义/表用途
  修订**；可用性逐项比对当前 effective 结果与采用修订（缺失/否决/撤销/
  修订不一致/关系非 validated → needs_revalidation；状态仍 validated 不代表
  旧修订旧基数可用）；缺完整依赖信息的旧模型保守降级；目录快照更替 →
  snapshot_changed（可查看/导出不冒充当前可用；不自动映射新表 ID；未扫描的
  源库实际变化无法检测）。**usable_as_current = 发布指针命中 且 依赖 ok**；
  `formal_metric_ids` 仅当前可用时返回（下游正式集合）；已确认草稿另列
  `confirmed_metric_ids`；validate/publish/导出共用同一判定
- 指标口径（修复轮+收尾轮 2）：口径字段=聚合/度量字段/去重键/过滤/时间字段/
  时间口径/单位；实质变化撤销业务确认与数据验证退 suggested（待确认类
  阻断带 `[待重新确认]` 稳定标记，重确认校验新定义后仅解除该类条目——
  真正安全/业务阻断保留；确认生成全新证据）；grain 业务定义（statement
  去空白+keys+basis）实质变化同样使受影响指标重新确认（同请求改 grain+
  确认按新定义完成不重复撤销）；共用结构化定义校验
  （build/revise/publish 三处一致）：各聚合所需字段与类型、去重键有序不
  重复、sum/avg 数值度量、time_field 日期时间类型、过滤值类型严格相容、
  引用字段必须属于指标自身实体（跨实体直接拒绝不隐式补 JOIN）；
  display_expression 一律从结构化定义重派生；发布前重新校验正式指标
- grain（修复轮）：键变化重核依据（主键/唯一索引精确匹配才可确认）；
  普通非唯一列人工声明保留待验证草稿（confirmed=false+basis=unknown）；
  主题/中心实体 grain 同步；指标统计粒度解析自主题 grain；发布重核
- 路径风险（修复轮）：按方向识别 one_to_many/many_to_many 扇出（阻断）与
  未知基数（待审）；人工选路只解除竞争路径歧义（其他风险不因选择消除；
  未选阻断路径不降级）；首版指标仅用本实体字段（跨实体定义显式拒绝），
  **不使用关联路径**——风险路径不阻断本表正式指标，仅自身保持
  blocked/needs_review 且不可选
- 自由文本净化（修复轮+收尾轮 2）：全部自由文本（实体/主题/指标名称与
  描述、grain 描述、确认备注、change_note、证据说明）进入模型/导出前经
  统一净化（含无键名前缀的 Bearer 形态）；发生遮蔽的不作业务名（回退原始
  标识）；净化在独立深拷贝上执行（不改调用方对象）；历史版本只在读取/
  API/导出边界净化（不改写存储行）；结构化标识与过滤值不净化；不宣称
  识别所有敏感内容
- 导出：JSON/YAML 表达同一份模型语义；schema_version+版本+来源+状态；
  顺序稳定；不含凭证/连接信息/sample_values；正式集合与草稿区分；
  不实现 YAML 导入或任意文件写入

## 业务语义模型（历史约定，保留参考）
- 实体、属性、关系、指标、维度和查询路径
- 每个主题有明确粒度
- 指标含表达式、去重规则、过滤条件和时间口径
- 来源、状态、版本和权限策略

## 数据集定义（M5 已实现，dataset-v1）

- 标识：dataset_id、version（不可变）；source_id、snapshot_id、
  model_id+model_version、topic_id、central_entity_id
- grain：中心表有序键列表+业务描述；**一行=中心 grain 的一个键值**
- 两类首版：detail_view（中心字段+沿 validated many_to_one/one_to_one
  的维度 LEFT/INNER JOIN，默认 LEFT 保留中心行缺失记 NULL）；
  aggregate（中心 grain 分组 + **一跳明细预聚合**：明细按指向中心的
  完整关联键分组独立聚合→关联键上至多一行→再连中心表；5×5/3 不膨胀；
  不用 DISTINCT/SUM(DISTINCT) 掩盖错误 JOIN；AVG=SUM/COUNT 口径）
- 引用：M4 formal_metric_ids 指标（服务端从仓储重载校验，不信任客户端
  status）；关系键+修订+方向+有序映射（该快照 validated 修订）
- 类型化过滤：值参数化（pyformat）；过滤位置区分（维度列入 ON 不改
  LEFT 语义；明细表列首版拒绝）；进视图定义时类型化字面量编译
- 指纹：definition_fingerprint（定义变化即失效旧 SQL/质量报告）+
  plan_fingerprint（SQL+参数形状）
- 状态：draft→validating→ready/failed_validation→publishing→published/
  failed_publish/**needs_reconciliation**（外部成功本地失败→显式核对
  恢复；不伪装成功不盲目删除）；并发由状态条件更新+指针原子切换仲裁

## M5 SQL 编译与执行安全（已实现）

- 编译：标识符全部来自模型白名单+反引号；输出别名唯一稳定；血缘
  （输出别名→来源字段+角色）；dialect=mysql
- AST 校验（sqlglot）：单 SELECT/受控 CTE；表/列白名单（别名作用域
  解析；CTE 内部对象豁免）；函数白名单；DDL/DML/多语句/分号/文件
  输出/锁定读/危险函数拒绝；pyformat 占位替换后解析
- 执行端口：ControlledQueryExecutor 只接受已校验 SQL（执行前强制）；
  MySQL 连接级 READ ONLY；SELECT 执行器不接受 DDL——视图 DDL 在
  独立 ViewPublisher 端口
- 预算：预览行数（20）/质量窗口（1000）/导出批次与硬上限；**运行
  上下文三要素**（逐语句硬额度 120 + monotonic 总期限 300s + 单查询
  结果字节上限 10MB）贯穿验证/预览/导出——每条语句发出前经单一硬闸
  （先期限后额度；期限拒绝不消耗额度），不存在无界查询入口；截断
  如实标记（LIMIT 不宣称限制扫描量）

## M5 质量报告（dataset-quality-v1）

- 每项：检查项/期望/实际/分母/范围/口径(exact|sample|unknown)/
  结果(passed|failed|incomplete)/原因；报告绑定 dataset_version+
  双指纹+model_version+snapshot（定义变化后必须重验）
- 检查：grain 非空/唯一（窗口 sample 不冒充全量）；中心行数↔中心表
  独立计数；JOIN 覆盖率；必填缺失；**明细预聚合↔各明细表独立分组
  聚合对账**（Decimal 容差 0.01）；中心指标↔中心表独立聚合
- 独立基准不重复执行生成 SQL；金额 Decimal 策略；空数据区分
  不适用/通过；时点证据（元数据快照≠源数据事务快照）
- 发布门槛：质量通过+报告指纹匹配定义+M4 依赖重载校验

## M5 视图与 ES 发布（已实现；真实发布未验证）

- 发布所有权（修复轮 2+3+最小轮）：`dataset_publish_locks` 表**原子占用**
  （dataset_id 唯一键，INSERT/条件 UPDATE）；**只有获得了锁的线程才
  更新版本状态**（未获锁的 409 失败方不覆盖他方状态）；全部路径
  finally 释放锁（**含 needs_reconciliation**——保留 active 锁会卡死
  reconcile 恢复路径，reconcile 自身经正常 acquire 取得占用）；
  状态更新原子条件（期望当前状态入 WHERE，恰一成功）
- 视图：独立目标 DSN/Schema（默认关闭）；应用前缀版本化视图名
  （nl2sql_ds_前缀）；不覆盖用户对象（已存在→幂等刷新或 409）；
  回滚=指针切回仍 published 的旧版本（视图不删除）
- ES：显式 mapping；grain→SHA256 文档 ID（组合键无歧义编码）；
  NULL/重复 grain 阻断（不静默覆盖）；Decimal→字符串（金额精度）；
  仅导出显式选定字段；分批 bulk+逐项结果检查（永久失败阻断/可重试
  有限重试）；行数核对后原子切换应用别名；版本索引保留不删除；
  导出硬上限触发失败（不切换别名）
- 外部目标与本地指针不跨库原子：先记录发布尝试→创建版本目标→
  验证→切换指针；外部成功本地失败→needs_reconciliation（显式
  reconcile 核对外部对象存在性后恢复）

## 数据集定义（历史约定，保留参考）
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
