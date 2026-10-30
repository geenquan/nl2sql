# 开发交接报告

更新时间：2026-10-30（M5 最后定点修复轮：恢复前置指针核对+别名防护、创建结果不明保留 pending、ES 请求保留有限默认超时+多请求按剩余期限递减；新增 14 项回归（10 项先在旧实现失败），全量 739 passed；全部离线验证）。本报告记录实际环境和验证结果，后续开发仍需按改动复验。

## M5 最后定点修复轮（2026-10-30，三项，回归先行）

### 一（P1）：恢复先核对指针，再切换 ES alias

- 根因（复现）：`_reconcile_locked` 先 switch_alias 再经
  `complete_dataset_reconciliation` 检查前指针——指针已被移到 v999 时
  返回 pointer_conflict，但 alias 已被改回 v1（实际外部副作用）。
- 修改：`dataset_service.reconcile/_reconcile_locked`——认领 recovering+
  验证恢复依据后**立即读取当前指针**，只允许 ==previous_pointer 或
  ==目标版本（None＝当时无指针的明确比较条件，不是"不检查"）；其他
  情况退回 pending 返回 pointer_conflict，**不调用 switch_alias、不改
  状态/指针/审计**。事务内条件比较保留（前置核对避免已知冲突下的外部
  副作用，事务比较保护本地提交）；恢复全程持有 recovering 占用。
- 别名防护：alias 指向既非本次目标、也非原前指针对应索引的未知索引时
  返回 alias_conflict 保留 pending（不把未知索引作为 previous_index
  移除）；原前指针为空且 alias 不存在＝首次发布，正常恢复。
- 回归（旧实现失败）：指针 v999+alias 无关索引 → pointer_conflict 且
  alias_history 不增/alias 不变/指针状态审计不变；alias 无关索引 →
  alias_conflict 不覆盖；正常恢复+幂等恢复仍通过。

### 二（P1）：创建调用结果不明保留 pending

- 根因（复现）：external_created 只在创建调用正常返回后为 True——
  服务端创建成功、响应丢失/连接断开时调用抛异常，被误判为确定失败：
  failed_publish+释放占用，下一次发布绕过结果未明的操作。
- 修改：`_publish_es/_publish_view` 在创建调用前落库 phase
  creating_index/creating_view（结果不明时恢复依据已持久化）；新增
  `ExternalOutcomeUnknownError`（app/core/exceptions）与
  `_creation_definitely_absent` 分类——传输错误/连接中断/服务端 5xx/
  MySQL 视图 DDL 后异常 → 结果不明 → needs_reconciliation+pending；
  明确已存在（index_exists/视图已存在）、4xx 明确拒绝
  （index_create_rejected）、调用前参数校验（ValueError/invalid_timeout）
  → 确定未创建 → failed_publish+释放。finally 不把 pending 改成
  failed/completed。ESHttpPublisher.create_version_index 按异常类别
  （HTTPStatusError 5xx/4xx、TransportError、其他 HTTPError）分类；
  MySQLViewPublisher DDL 后异常改抛结果不明。
- 恢复边界（保守，不自动处理）：对象明确不存在可确定失败；查询对象
  状态失败回 pending；对象存在但导出未完整继续阻断（不自动重导出、
  不删孤立索引、无后台恢复——创建结果不明且索引已建的极端情形需人工
  处置，本轮如实保留该边界）。
- 回归（旧实现失败）：ES 替身"先真实创建索引再抛响应丢失"→ 索引存在
  +needs_reconciliation+pending，新发布/回滚 409 且无额外外部副作用；
  视图同场景 → pending，故障移除后既有 reconcile 正确恢复。

### 三（P2）：ES 请求保留默认超时，重试使用剩余期限

- 根因一：`_request_timeout(None)` 返回 None 且显式传给 httpx——HTTPX
  中 timeout=None 是**关闭超时**（不是用客户端默认），reconcile 未传
  timeout_ms 的调用可能无限等待。根因二：HEAD/PUT 与 bulk 每次重试复用
  同一 timeout_ms，不扣除已消耗时间。
- 修改：`ESHttpPublisher`（`_deadline_from/_request_timeout_before/
  _check_deadline` 替代 `_request_timeout`）：未指定 timeout_ms 用
  构造配置的有限默认（绝不传 None）；timeout_ms≤0 在发出请求前拒绝
  （invalid_timeout）；HEAD+PUT 与 bulk 重试在入口计算一次绝对截止，
  每次请求前重算剩余（≤配置上限），耗尽即停止不启动下一次请求，响应
  返回后同样检查（超期结果不得使发布继续成功）；时钟可注入
  （构造参数 clock）。`reconcile` 建立恢复共享 run_context：
  count/别名查询/切换/本地提交用同一剩余期限；期限耗尽→不切别名、
  不提交指针、占用退回 pending（保留安全领域错误）。重试次数不变。
- 回归（旧实现失败，**全部经 httpx.MockTransport 验证真实
  ESHttpPublisher 的实际请求超时**）：A 不传 timeout_ms → 四项超时
  均为配置的 5.0（旧实现全 None）；B HEAD 消耗 9.5s/10s → PUT 用
  ~0.5s 剩余（旧实现复用 10s）；期限耗尽 PUT 不发出；C bulk 重试第
  二次超时降到 ~4s、期限耗尽后第三次不发出（旧实现复用同一值）；D
  reconcile count 耗尽 0.3s 期限 → 不切别名/不提交指针/回 pending/
  无恢复审计。

### 验证

- 新增 `tests/test_m5_final_fix_round.py` 14 项（10 项在修复前失败，
  4 项守护既有行为：正常/幂等恢复、明确已存在释放、核对网络失败保
  pending、并发认领恰一）。
- `python -m pytest -q` → **739 passed, 9 skipped**；ruff（app tests
  scripts）/ pip check 通过（全部离线；真实 MySQL/ES 发布仍未验证）。

## M5 恢复修复轮（2026-10-30，三组问题，回归先行）

### 修复一：恢复依据在本地指针提交前可靠保存

- 根因（复现）：`_publish_es` 只在全部成功后把 documents 写入
  run.result——本地提交失败时恢复逻辑读到 expected=None，完整成功的
  外部发布无法恢复（reconcile 返回 expected_unknown）。
- 修改：`dataset_service._new_recovery_record/_persist_run/
  _set_recovery_phase`——发布运行 result.recovery 结构化对象
  （operation_id/dataset/version/target/plan_fingerprint/
  previous_pointer(+recorded 标记)/index+alias 或 view schema+name/
  export_complete/expected_documents/count_verified/phase），取得占用后
  **任何外部副作用前**读取并落库 previous_pointer；导出完整结束、count
  核对、alias 切换前后逐阶段真实落库（`save_dataset_run`，非内存对象；
  关键阶段保存失败即停止后续副作用）。`_export_to_es` 进度增量更新不
  覆盖 recovery；export_complete 只由发布方在分页正常结束后设置。
- `_reconcile_locked` 只接受与当前 dataset/version/target/指纹/原操作
  全匹配的恢复记录；恢复要求 export_complete+expected_documents 明确
  （**0 是有效值**）+ES 计数相等+alias 指向目标（或在指针核验后显式
  切换）；ES count 网络错误≠索引不存在（index_not_found 为独立 reason），
  查询失败保留待核对。

### 修复二：待恢复占用 + 原始前指针恢复提交

- 根因（复现）：finally 无条件释放占用——v1 待恢复可被 v2 发布绕过，
  reconcile(v1) 可把指针从 v2 改回 v1；且 set_published_dataset 与
  update_dataset_status 两事务不原子。
- 仓储状态机（复用 dataset_publish_locks，无新框架）：active→completed/
  failed（确定结束）；active→pending（外部已创建/结果不明）；
  pending→recovering（`claim_publish_reconciliation` 原子认领，保留原
  operation_id）；recovering→completed（恢复成功）/pending（核对未定）/
  failed（确定失败）。`acquire_publish_lock` 只认终态（completed/failed/
  遗留 released），active/pending/recovering 均拒绝（409），首次 INSERT
  并发唯一冲突→False；`get_active_publish_lock` 覆盖三种占用态并返回
  status。`_publish_view/_publish_es` 以局部 outcome 收尾：无外部副作用
  确定失败→failed；外部已创建/不明→pending；成功→completed；finally
  不把 pending 覆盖为 failed/released。
- `reconcile` 认领原发布操作的 pending（不再新建操作 ID/普通 acquire）；
  并发 reconcile 恰一认领，失败方不动占用/指针/别名；未完成退回 pending
  （可再核对）。恢复前核对 previous_pointer（None＝当时无指针，是明确
  比较条件）：当前==前指针→提交；当前==目标版本→幂等完成（不重复
  审计）；其他→pointer_conflict（不改写）。旧运行无恢复依据保守拒绝。
- `complete_dataset_reconciliation`（仓储单事务，同一 conn）：核对
  recovering+operation_id→前指针比较（含"预期不存在"）→条件切指针→
  版本状态 published→恢复审计→占用 completed；任一步失败全部回滚；
  重复调用幂等（already_committed 不重复插审计）。

### 修复三：期限与结果字节阻断发布

- 根因（已复现）：execute 未实际使用 timeout_ms；超期仅置 truncated；
  字节检查按字符数且先入批后检查；导出丢弃 truncated——查询返回 5 行
  且 truncated=True、期限已过仍 published=True 且别名已切换。
- `ControlledExecutorBase.execute`：单查询有效期限=min(timeout_ms,
  运行剩余)——执行前（不消耗额度）、fetch 循环内、返回后三处检查；
  MySQL 经 SELECT 优化器提示 MAX_EXECUTION_TIME 服务端限执行（WITH/
  CTE 不注入，客户端返回后拒绝兜底；MariaDB 服务端取消限制保留）；
  SQLite 经 sqlite3 进度处理器**执行中中止**（不用线程超时遗留后台查询）。
  结果字节＝每行按导出序列化规则（Decimal/date→文本）的 UTF-8 字节数
  逐行累计，加入前检查，超限抛 `ValidationResultBudgetExceededError`
  （不返回部分超限批次）；单行超限立即拒绝；中文按字节计。超时/超期抛
  `ValidationQueryTimeoutError`/`ValidationDeadlineExceededError`，与
  预算类领域异常一并原样透传（不翻译为 query_failed）。时钟可注入
  （_monotonic）供确定性测试。
- `_export_to_es`/`_publish_es` 共享同一 run_context（不重计时）：导出
  分页不丢弃 truncated（分页自带 LIMIT batch，意外截断即停止）；每次
  查询/bulk 前后与 count/alias/本地提交前检查同一期限；超期不启动下一项
  外部副作用、不设 export_complete、不返回 published=True；已发生外部
  副作用的按修复二进 pending。ES 写入/核对调用接受 timeout_ms（不超过
  剩余期限的单请求超时；端口向后兼容可选参数，真实适配器 httpx 按请求
  覆盖 + 替身同步记录）；执行器/发布器关闭均在 finally。

### 语义更正（既有测试预期更新，未删除恢复可达性检查）

1. `test_m5_min_fix_round.py::TestESLockRelease`：原断言"ES 失败发布后
   释放占用"改为"**pending 保留 + reconcile 可认领 + 新发布被 409 拒绝**"
   ——上一轮"始终释放"会让待恢复状态被新发布绕过（本轮修复二的根因），
   恢复可达性改由认领机制保证。
2. `test_byte_cap`：字节超限从"标记截断"改为"抛领域异常拒绝结果"——
   截断语义保留给预览行数上限，超限数据不得交回调用方。
3. `test_partial_index_not_recovered_as_published`：直接构造
   needs_reconciliation（无发布运行/占用）的旧数据场景，从"reconcile
   静默返回"改为"保守拒绝（409）且不恢复 published"。

### 验证

- 新增 `tests/test_m5_recovery_round.py` 18 项（A 完整 ES 恢复含恢复
  依据持久化断言/B expected=0/C 导出未完整/D pending 阻断发布+回滚且
  无外部副作用/E 旧数据保守拒绝+指针冲突不覆盖/F 双线程恰一认领/
  G 事务注入故障零部分提交+可再恢复+审计不重复/H 最后一页超期不
  bulk/不切别名/不提交指针且 ES 请求超时≤剩余期限/I ASCII+中文 UTF-8
  口径+单行超限/J timeout_ms 可注入时钟确定性触发+MySQL 提示注入/
  K deps 正式工厂上下文真实影响执行行为/L 冒烟）。
- `python -m pytest -q` → **725 passed, 9 skipped**；ruff（app tests
  scripts）/ pip check 通过（全部离线；真实 MySQL/ES 发布仍未验证）。

## M5 最小修复轮（2026-10-30，五项指定 + 五处执行中发现，先复现后修复）

指定五项（基于 a690e02）：

1. **条件 AVG 分母排除 NULL 度量值**：`compiler._conditional_aggregate`
   的 AVG 分母原为 `SUM(CASE WHEN cond THEN 1 ...)`（计入度量值为 NULL
   的行），与分子 `SUM(CASE WHEN cond THEN col)`（自动跳过 NULL）口径
   不一致。修复：分母改为 `SUM(CASE WHEN cond AND col IS NOT NULL THEN
   1 ELSE 0 END)`；分子分母同为"满足条件且度量非空"口径。
2. **中心指标独立对账**：`quality._metric_reconciliation` 此前未按各
   指标自身过滤独立构建基准。修复：逐指标应用其 M4 结构化过滤
   （各指标独立、不合并条件），AVG 基准=`SUM(col)/NULLIF(COUNT(col),0)`
   （分母只计非 NULL；不把 COUNT 当 AVG 结果）。
3. **回滚尊重发布占用**：`rollback` 在外部副作用（ES 别名切换）前经
   `dataset_publish_locks` 取得数据集级占用；占用被他方持有时 409 拒绝
   且无外部副作用；期望指针不匹配在外部切换前拒绝。
4. **核对完成指针提交**：`_reconcile_locked` 恢复 published 时经
   `_commit_pointer()` 同时提交发布指针+审计+版本状态（旧实现只改
   status，指针仍指向旧版本——视图存在但指针未恢复）。核对自身也须
   取得占用（不绕过互斥）。
5. **正式预算接入（不留限制）**：运行上下文（`StatementBudget` 逐语句
   原子额度 + monotonic 总期限 + 结果字节上限）贯穿验证/预览/ES 导出
   三类运行：service 创建 → 装配工厂转发 → 执行器强制；`del timeout_ms`
   类临时占位已移除。

执行中复现并一并修复的缺陷：

6. **预算双重扣减**：`execute` 同时经 `_before_query` 与
   `_before_statement` 各消耗一次额度（限额实际减半，既有回归测试
   失败暴露）。修复：合并为单一 `_before_statement` 硬闸——先查总期限
   （超期抛 `ValidationDeadlineExceededError`，不消耗预算），再原子
   消耗预算（耗尽抛 `ValidationStatementBudgetExceeded`，保留真实
   issued/limit）；SIM103 一并消除。
7. **AVG 预聚合基准列错位**：`_preaggregation_reconciliation` 的 AVG
   基准为 `SUM(x), COUNT(x)` 双列（别名只作用于 COUNT，比较对象错位，
   5 组中 4 组误报不一致）。修复：单表达式 `SUM(x)/NULLIF(COUNT(x),0)`，
   与编译产物同口径（分母只计非 NULL 度量行）。
8. **ES 失败发布保留 active 锁**：`_publish_es` 在外部索引已建、本地
   失败（needs_reconciliation）时保留 active 锁，而 reconcile 也须
   acquire——恢复路径永久死锁。修复：与 view 路径一致，获得锁即始终
   释放（reconcile 是恢复路径，经正常 acquire 取得占用）。
9. **超期判定被覆盖**：`validate` 在超期置 failed_validation 后又被
   `payload.update({"status": "ready" if all_passed ...})` 无条件覆盖。
   修复：`passed = all_passed and not expired`，超期一律不进 ready。
10. **MySQL 执行器参数缺失**：`MySQLControlledExecutor` 不接受装配工厂
    传入的 `deadline_monotonic/max_result_bytes`（真实 MySQL 路径
    TypeError；离线 SQLite 夹具不暴露）。修复：接受并转发至基类。
    另补齐 `max_result_bytes` 的真实执行（逐批字节检查，超限即停并
    如实标记截断）与预览/导出的运行上下文接线（原为无界查询入口）。

- 回归（`tests/test_m5_min_fix_round.py`，12 项，其中 4 项在修复前
  失败：AVG 分母值断言、中心指标独立对账、期限不消耗预算、字节上限
  标记截断）：另含回滚占用拒绝+释放后成功、核对指针恢复断言、单次
  额度消耗计数、超期验证不进 ready、预览运行上下文接线、MySQL 执行器
  参数、装配工厂转发、ES needs_reconciliation 后锁已释放且 reconcile
  可运行。
- 验证：`python -m pytest -q` → **707 passed, 9 skipped**；ruff /
  pip check 通过（全部离线；skipped=默认关闭的真实集成）。
- 新增配置：`DATASET_VALIDATION_MAX_QUERIES=120`、
  `DATASET_VALIDATION_DEADLINE_SECONDS=300`、
  `DATASET_MAX_RESULT_BYTES=10000000`（见 docs/configuration.md）。

## M4 净化链路收尾修补（2026-10-29，两处遗漏，先复现后修复）

1. **实体 grain 读取净化遗漏**：`_sanitize_model_text` 只净化了
   topics[].grain，遗漏 entities[].grain.statement/missing——历史模型中
   同一口令在主题中已遮蔽、在实体中原样返回（get/list/versions/API/
   JSON/YAML 均泄漏）。修复：实体与主题 grain 自由文本（statement+missing）
   使用一致净化规则；结构化字段（table/keys/basis/对象 ID）不变；独立深拷贝；
   历史存储行不改写（回归逐字节验证）。
2. **新版本保存前未统一净化**：从含敏感文本的历史版本创建新修订时 API
   响应已净化但数据库载荷仍含原值；非唯一 grain 键的待验证草稿分支
   statement 原样持久化。修复：`build_model`/`revise_model` 在 `save_model`
   前统一执行 `_sanitize_model_text`（复用现有净化器，返回独立深拷贝，
   不引入 availability 等动态字段）——保存与响应一致，覆盖继承文本与全部
   分支；不改写旧版本；结构化标识/过滤值不受影响；确认失效、发布门槛、
   版本并发行为不变（回归验证）。
- 回归（`tests/test_semantic_model_fix_round3.py`，4 项，3 项先在旧实现
  失败）：实体/主题 grain 泄漏五出口+存储逐字节不变+结构化字段不变；
  继承敏感文本的新修订存储净化+原版本不变；草稿分支存储/响应净化+草稿
  语义与发布阻断保持；普通文本/结构化引用/过滤值保持原语义。
- 验证：`python -m pytest -q` → **648 passed, 9 skipped**；ruff / pip check
  通过（全部离线）。

## M4 收尾修复轮 2（2026-10-29，五项，全部先复现后修复）

1. **历史模型主题名/指标名泄漏敏感内容【P1】**
   - 根因（复现）：`_sanitize_model_text` 只覆盖实体/属性/指标描述——
     旧构建器把含密注释派生的主题名、指标名、grain 描述、确认备注、
     change_note 原样出现在 get/list/versions/JSON/YAML。
   - 修复：净化覆盖全部自由文本（实体名/别名/用途说明/属性业务名与证据、
     主题名/描述/grain 描述与缺失项、指标名/描述/统计粒度/确认备注/证据、
     change_note）；**深拷贝独立副本**（不修改调用方对象）；结构化标识
     （Schema/表/列/关系键/对象 ID/过滤值）不净化；历史版本只在读取/
     API/导出边界净化（回归断言读取前后存储行逐字节相同）；新建修订的
     grain 描述/change_note/确认备注在持久化前同边界净化；共享净化器补
     无键名前缀的 `Bearer <token>` 形态遮蔽。
2. **grain 业务描述变化未撤销指标确认【P1】**
   - 根因（复现）：只比较 keys/basis；statement 实质变化后旧确认保留且
     可直接发布；另有 `and False` 死条件与重复判断。
   - 修复：修改前取完整旧 grain，业务定义一致判定＝statement（去首尾
     空白）+keys+basis；实质变化使受影响指标确认失效；仅空白或完全相同
     不误撤销；仅改主题名不撤销；同请求"改 grain+明确确认"先更新 grain
     再按新定义校验确认，尾部失效循环跳过本请求已重新确认的指标。
3. **合法重新确认留下永久阻断【P2】**
   - 根因（复现）：`_invalidate_metric_confirmation` 写入的"需重新确认"
     无稳定标记，confirm 无法区分"待重新业务确认"与真正安全阻断——
     重确认后残留导致发布永久失败。
   - 修复：待确认类阻断统一加 `[待重新确认]` 标记；confirm 在新定义完整
     校验通过后仅解除该标记条目与有依据的口径标签（`skip_status_filter`
     同时覆盖状态/过滤口径标签）；人工否决等真正阻断永不因确认消失；
     确认生成全新证据（时间/备注），不恢复旧记录。
4. **依赖检查用 latest 而非 effective【P2】**
   - 根因（复现）：字段语义依赖取 `history[0]`（latest）——人工确认后
     出现机器建议即误判降级（effective 未变）。
   - 修复：改用 `list_field_semantics(table_id, effective=True)`（与构建
     同语义的人工优先解析，复用仓储接口），按 table_id 缓存整表结果；
     作用域 table_id.column 不串用；effective 真变化（人工修改/撤销/
     否决）仍正确降级（既有回归继续通过）。
5. **无关风险路径误伤单实体指标【P2】**
   - 根因（复现）：blocked 路径终点实体的**全部**正式指标被阻断——
     booking→service_item 一对多路径阻止了本表 SUM(price) 指标发布。
   - 修复：移除该误伤规则——首版指标只支持本实体字段（跨实体定义在
     `_validate_metric_definition` 显式拒绝，不隐式补 JOIN），不使用
     关联路径；风险路径自身保持 blocked/needs_review、不可选为安全路径
     （保留既有约束）；`path_issues` 如实上报。轮 1 的路径影响断言同步
     更正为新语义。

### 验证（收尾轮 2 后）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **644 passed, 9 skipped**（收尾轮新增 21 项：9 项先在旧实现复现失败；未删测试/未放宽/无新增跳过；轮 1 路径测试按新语义更正） |
| `python -m ruff check app tests scripts` / `pip check` | 全部通过 |
| 端到端流程 | 构建→确认 grain/指标→发布→修改口径/grain→确认失效→按新口径重确认→再发布→正式集合正确恢复（回归覆盖） |



## M4 缺陷修复轮（2026-10-29，八组，全部先复现后修复）

### 1. 注释敏感内容进入模型与导出【P1】
- 根因：表/列注释等自由文本直接成为实体名/业务名并进入导出（复现：表注释
  `password: ReviewDummy123` 原样出现在 JSON 导出）。
- 修复：`model_builder` 构建前经 `sanitize_text_for_model` 净化（键值/Bearer/
  引号键名遮蔽）；发生遮蔽的注释不作业务名——回退原始表名/列名并注明来源
  （结构性标识不净化）；`_enrich` 在读取/导出边界对**历史版本**净化
  （不改写存储行，回归断言存储行原样、读取已净化）。

### 2. 发布指针并发保护非原子【P1】
- 根因：`set_published` 先 SELECT 比较再无条件 upsert（复现：两线程同期望
  指针分别发布 v3/v4 均成功）。
- 修复：期望比较进入 `UPDATE ... WHERE`（rowcount=0 即冲突）；expected=None
  语义=读当前值作隐式期望执行条件写（首次发布 INSERT 由 source_id 唯一约束
  仲裁）；审计与指针同事务（审计失败整体回滚）；发布/回滚共用保护。
- 回归：真实线程+双连接——同期望并发发布恰一成功；发布 vs 回滚竞争（目标
  版本故意设为移动型避免无操作假阳性）；首発竞争用引擎事件在 INSERT 处
  对齐两连接强制同时插入（唯一约束恰一成功）；失败不改指针不写成功审计。

### 3. 口径修改保留旧确认【P1】
- 修复：口径字段集=聚合/度量字段/去重键/过滤/时间字段/时间口径/单位；实质
  变化→撤销 business_confirmation 与 data_validation、退 suggested、加阻断
  （同请求 confirm=先撤销再按新定义完整校验后记录新确认）；非口径字段
  （名称/描述）保留确认；grain 键/依据变化联动受影响指标重新确认。

### 4. 指标定义与字段引用校验不足【P1】
- 修复：共用 `_validate_metric_definition`（build 剔除不合规候选/revise 保存
  前拒绝/publish 重新校验，不只查 status 与 blocking）：各聚合所需字段与
  类型（count_rows 无字段、count_distinct 去重键有序不重复、sum/avg 数值
  度量、min/max 明确类型）；time_field 必须日期时间类型；过滤值类型严格
  相容（不把任意对象 str() 当合法值）；引用字段必须属于指标**自身实体**
  （跨实体直接拒绝，不隐式补 JOIN）；display_expression 一律从当前结构化
  定义重派生（dict 修订后不再沿用旧表达式）。

### 5. 依赖修订未参与可用性检查【P1】
- 根因：只查关系当前状态是否 validated，不比较采用的修订（复现：M3 修订
  变更且基数变化后旧模型仍 ok）。
- 修复：依赖清单记录 M2 字段语义/表用途修订（`m2_field_semantics`/
  `m2_table_purposes`）；`_dependency_status` 逐项比对当前 effective 修订
  （缺失/否决/撤销/不一致 → needs_revalidation）；缺完整依赖信息的旧模型
  保守降级；发布/回滚/读取/导出共用同一判定。

### 6. 路径风险遗漏与人工选路绕过【P1】
- 修复：按方向识别 one_to_many/many_to_many 扇出（均阻断）；未知基数待审；
  人工选路只解除"多条路径"歧义——预算截断/扇出/基数未知不因此消除，状态
  按剩余风险重判；未选路径 blocked 不降级；验证报告用结构化实体引用替代
  `metric.app.<table>` 前缀硬编码（非 app Schema 指标同判定；单实体指标
  不被无关路径误伤）。

### 7. grain 变更保留错误结构依据【P1】
- 修复：键变化重核依据——与主键/唯一索引**精确匹配**才可 confirm；普通
  非唯一列人工 confirm 保留为待验证草稿（confirmed=False+basis=unknown+
  missing 说明）；重复列/缺列拒绝；主题与中心实体 grain 同步；指标
  grain_statement 始终解析自主题 grain；发布时重新核验（不只信 confirmed
  布尔值）；grain 实质变化使受影响指标重新确认。

### 8. 未发布草稿被标记当前正式可用【P1】
- 修复：`usable_as_current` = 发布指针命中 **且** 依赖 ok；`formal_metric_ids`
  仅在当前可用时返回（未发布/依赖失效→空），已确认草稿另列
  `confirmed_metric_ids`；validate/_enrich/publish 去递归（检查独立于
  "已发布"——发布前检查可在未发布版本上通过）。

### 9. 契约补齐【P2】
- Entity.purpose 消费有效 M2 表用途（rejected 记 unknown+purpose_note，
  不作正向主题分类）；Topic.metric_ids/dimension_ids 填充真实归属引用。

### 验证（修复轮后）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **623 passed, 9 skipped**（修复轮新增 38 项：全部先在旧实现复现失败后修复；未删测试/未放宽/无新增跳过） |
| `python -m ruff check app tests scripts` / `pip check` | 全部通过 |
| 真实集成（`NL2SQL_MYSQL_INTEGRATION=1`） | **8 passed, 1 skipped**（M1–M3 无回归） |
| M4 真实演示复跑 | 真实库段如实（partial 5/18、0 validated 阻断）；合成段全流程通过（发布→导出→改版→差异→回滚）；项目根无残留 metadata.db/凭证文件 |

### M5 正式可用性判定（修复后口径）

模型可用 = `availability.is_published_version && availability.dependency_status
== "ok"`（即 usable_as_current）；指标正式集合 = 该版本 `formal_metric_ids`
（仅当前可用时非空；草稿已确认见 confirmed_metric_ids）。依赖判定覆盖
M2/M3 采用修订 vs 当前 effective、目录快照指针。



## M4 业务实体与语义层（2026-10-29 完成）

### 已实现能力

| 能力 | 实现 |
| --- | --- |
| 领域契约 | `app/domain/semantic_model.py`（semantic-model-v1）：FieldRef/TableRef 完整身份（组合键有序列表，不拼接字符串）、Entity/Attribute/Dimension/Grain（业务描述+结构化键+依据+确认状态）、ModelRelationship（M3 key+revision+完整身份+有序映射，不存自由文本 JOIN）、Metric（受限聚合 count_rows/count_distinct/sum/avg/min/max + 受限过滤操作符 + 类型化值；数据验证与业务确认两个维度分别留证）、QueryPath（M3 find_paths 正向复用，含基数风险/歧义/截断标记）、Topic（中心实体+grain）、依赖清单（快照+M3 修订+规则版本） |
| 构建 | `app/semantics/model_builder.py`（smodel-build-v1，确定性规则不调用模型）：一表一实体候选（entity_id=schema.table，跨 Schema 同名表不合并）；M2 effective 消费（rejected 不作正向命名/分类，缺失按 unknown）；grain 结构依据（主键/唯一约束；样本唯一率不作依据）；候选指标全部 suggested（金额/数量凭 M2 类别证据生成 sum/avg，事件 count_rows，主数据 count_distinct 组合键元组语义；单位/时间口径/状态过滤进 blocking）；路径覆盖全部实体对（max_hops 限制）暴露竞争路径歧义；敏感列打标签不进维度 |
| 服务 | `app/services/semantic_model_service.py`：build（默认发布快照，部分快照需 allow_partial 且 input_complete=false；显式快照校验归属且部分快照同样受门槛）/revise（人工修订产生新版本，引用服务端核验——不存在字段/非法聚合/非法操作符/类型不符/任意 SQL 拒绝；expected_revision 由 (model_id,version) 唯一约束仲裁 → 409）/validate（grain 确认、正式指标口径、依赖、阻断风险、部分快照）/publish（发布前全面复查+指针原子切换+审计）/rollback（目标当前可用性核对+指针切换+审计）/diff（新增/移除/变更）/export（JSON/YAML 同语义、顺序稳定、不含凭证/样本值；正式集合与草稿区分） |
| 持久化 | SQLite 追加三表 `semantic_models`（(model_id,version) 唯一=不可变+并发仲裁）/`semantic_model_pointer`（发布指针）/`semantic_model_log`（发布/回滚审计）；create_all 兼容，不动既有表 |
| 依赖降级 | M3 关系被拒/stale → needs_revalidation；目录快照更替 → snapshot_changed（旧模型可查看导出但不冒充当前可用；不自动映射新表 ID；源库实际变化未扫描无法检测——如实说明） |
| API | `app/api/routes/semantic_models.py`：POST build/revise/validate/publish/rollback；GET models/{id}/versions/{v}/diff/export/publication-log/published |
| 演示 | `scripts/m4_real_demo.py`：真实库（扫描≤5 表×50 行 + M2 规则语义 + M3 验证≤3 候选×45 SQL；0 validated 时如实阻断）+ 合成夹具全流程（构建→grain/指标确认【开发演示假设】→检查→发布→YAML 导出→改版→差异→回滚），清楚区分 |

### 验证结果（Conda `nl2sql`，2026-10-29）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **585 passed, 9 skipped**（M4 新增 32 项离线：服务 28 + API 4；skipped 为默认关闭的真实集成） |
| `python -m ruff check app tests scripts` / `pip check` | 全部通过（新增依赖 **PyYAML** 已声明 pyproject/environment） |
| 真实集成（`NL2SQL_MYSQL_INTEGRATION=1`） | **8 passed, 1 skipped**（M1–M3 无回归；M4 构建纯本地无需真实集成） |
| 真实演示（hotel，临时目录） | 扫描 5/18 表（partial 如实）→ M2 规则语义 5 表 → M3 验证 0 validated + 3 needs_review（20 SQL；如实阻断不强行确认）→ 模型草稿 5 实体 0 主题；合成夹具段：12 实体/9 主题/6 关系/21 候选指标 → 确认 → 检查通过 → 发布 → YAML 48473 字符 → 改版差异 → 回滚闭环 |

### 关键回归覆盖（tests/test_semantic_model_service.py + test_semantic_model_api.py）

正常构建/属性 M2 消费；组合粒度键有序；未知粒度表标记；跨 Schema 同名表不合并；部分快照门槛（默认/显式均拦截）；跨源快照拒绝；空目录；rejected 语义不作正向；仅 validated 关系入模型（候选/待审排除）；组合映射顺序保持；单路径 available/竞争路径歧义待审；多对多路径阻断；预算截断如实标记；grain/指标确认前置条件（单位/时间口径/状态过滤）；非法字段/聚合/操作符/SQL 注入值拒绝；人工选路消歧；并发同基线修订恰一成功；重建不覆盖人工版本；发布需确认 grain/有效依赖；指针并发 409；差异检出变更；JSON/YAML 同语义且无敏感内容；持久化失败指针不变；关系 stale/快照更替可用性降级且阻断发布。

### M5 可使用的稳定契约

- 已发布模型：`GET /api/v1/sources/{sid}/semantic-models/published`（含 availability.usable_as_current 与 formal_metric_ids）；
- 完整模型 JSON/YAML：`GET /semantic-models/{id}/export`（schema_version、依赖修订、有序映射、粒度、指标口径、路径步骤）；
- 正式可用性判定：发布指针存在 且 availability.dependency_status=="ok"（M3 有效 + 快照未更替）；指标正式集合＝formal_metric_ids（business_confirmed/published）。

### M4 已知限制（如实保留）

- 主题/实体/指标候选为保守规则生成（一表一实体，不自动合并）；业务口径需人工确认（演示中标注为开发假设）。
- 路径只沿 M3 source→target 正向；桥接/逆向关联不可达（阻断或待审，需人工选路）。
- 多 validated 边不等于路径可安全聚合：many_to_many 阻断、歧义/基数未知待审、正式指标需可用路径。
- 无指标对账（M5 数据集质量检查范畴），不产生 data_validated。
- 可用性检查基于本地元数据（M3 有效状态+目录指针）；源库实际变化未扫描无法检测。
- reviewer 为本地开发输入，无身份认证；M2 Embedding 仍未完成（不宣称 M2 全部完成）。



## M3 最小修补（2026-10-28，修复轮 3：跨 Schema 重验切换目标关系）

- **缺陷（已复现）**：`discovery.build_candidate_for_mapping` 匹配外键未比较
  ref_schema——新快照把 `app.booking.guest_id` 的外键改指 `archive.guest.id`
  后，重验请求 `app.booking.guest_id → app.guest.id` 会重建出指向 archive 的
  **另一条关系**并保存为 validated，运行仍报告原请求成功。
- **修复（最小改动）**：
  1. `app/relationships/discovery.py`：外键有效目标 Schema＝fk.ref_schema
     有值用之、否则回退来源表 schema，必须与请求 target_schema 一致（源字段/
     目标表/目标字段及顺序的现有比较不变）；指向其他 Schema 的同名表不能
     作为本映射的 explicit_fk 依据；无匹配外键走既有非外键重建流程
     （不放宽验证门槛）。
  2. `app/services/relationship_service.py`：重绑后新增**身份不变量检查**
     （relationship_key、source_id、两端 Schema/表、有序字段映射必须与请求
     一致）；不一致抛固定安全错误终止——不探查、不保存另一条关系、不改键
     掩盖（`_REBIND_IDENTITY_MISMATCH` 固定文案）。
- **回归（全部离线：临时 SQLite + 模拟连接器 + 虚构数据，不连真实库/模型）**：
  `tests/test_relationships_fix_round3.py` 7 项——app/archive 同名表并存、
  外键改指 archive 不匹配；外键仍指 app 正常匹配；ref_schema 缺失回退来源
  Schema；请求本身指向 archive 时用 archive 外键；服务层 revalidate 断言
  请求关系完整身份不变、不继承 explicit_fk、不验证另一条关系、旧快照历史
  不变；注入错误目标/错误关系键候选 → 固定错误终止且零探查零写入。
- **验证**：`python -m pytest -q` → **553 passed, 9 skipped**（新增 7 项）；
  ruff / pip check 通过。

## M3 修复轮 2（2026-10-28，两项 P1，先复现后修复）

### 1. 跨快照重绑继承过期候选依据【P1】

- **根因（已复现）**：`_rebind_to_snapshot` 用 `{**record}` 继承旧链全部候选依据——
  新快照把目标字段 int→varchar(20) 并删除外键后，重验结果仍
  status=validated / type_verdict=compatible / origin=['explicit_fk']。
- **修复**：稳定逻辑身份（relationship_key=两端标识+有序映射）与**依赖快照的
  候选依据**明确区分——重绑记录经 `discovery.build_candidate_for_mapping`
  （新增，复用常规候选构建算法）按**目标快照**的表/画像/有效语义重算：
  类型兼容及原因、外键存在性（目标快照无外键不标 explicit_fk）、结构唯一性/
  组合键风险（含外键路径的目标组合主键风险补齐）、支持证据、来源、基数提示、
  评分及版本；目标快照缺画像/语义按缺失处理（分项记 0，不借用旧快照）；
  常规信号无法召回时保留为**显式重验请求**（origin 空 + reasons 标记
  revalidate_rebind，不伪造结构/语义支持）；类型跨族时不执行可能产生隐式
  转换语义混淆的 JOIN（证据 queries 为空 + gated type_incompatible +
  策略阻断，不得自动 validated）；新链不继承旧审核人/确认/验证结论
  （`_candidate_to_record` 全新构建），旧依据只保留旧链历史。
- **回归**：`tests/test_relationships_fix_round2.py::TestRebindRecomputesBasis`
  （7 项）——int→varchar+删外键（断言 origin/type_verdict/evidence/阻断全字段）；
  删外键类型兼容（无 explicit_fk 仍可凭证据验证）；唯一约束删除（结构分重算、
  唯一性口径来自 exact 观察而非结构）；变组合唯一（组合键风险）；缺画像/语义
  （分项 0 不借用）；仅加列（外键保留仍可重验）；新旧链互不污染。

### 2. SQL 数量预算改为逐语句硬限制【P1】

- **根因（已复现）**：操作组估计下界 2、预算 2，但组内第 3 条 SQL 仍发往
  数据库——事件钩子只计数不阻止。
- **修复**：`StatementBudget`（execution.py）在每条 SQL **发出前**原子检查并
  消耗额度，不足抛 `ValidationStatementBudgetExceeded`（新领域异常，固定安全
  文案，无 SQL/参数/底层异常文本；适配器不得翻译为 unknown 数据库故障）——
  `before_cursor_execute` 钩子内抛出即阻止执行，语句不到达数据库；Connector
  与 Probe 共享同一运行预算（结构核对 Inspector 多条 SQL 同样受控）；操作组
  估计准入保留为优化。口径区分：`queries_used`=已获准并实际发出的语句数
  （**含发出后失败的**：不退回不重复计数），`sql_statements_rejected`=在预算
  检查处被拒绝、未发出的语句数（**不计入已执行**）；证据采集中途被拒优雅降级
  （该指标 gated statement_rejected，候选 needs_review，不冒充完整验证、不
  覆盖已有有效关系）；结构核对中途被拒则该候选干净跳过（零残留）；运行正常
  收尾、关闭连接并释放共享并发额度；普通 `StatementCounter`（仅计数不阻止）
  保留——未注入预算的 M1/M2 历史调用行为不变。
- **回归**：`TestStatementBudgetEventLevel`（6 项，**真实 SQLAlchemy 执行事件 +
  临时 SQLite 引擎**）——预算 2 尝试 3 条 SELECT：接收监听器（挂载在守卫之后）
  证明数据库只收到 2 条；剩余 1 条额度进入多语句 Inspector 读取即被拒且异常
  无 SQL 文本；连接器与探查（独立引擎）共享额度；失败 SQL 消耗额度不退回；
  被拒不计为已执行；并发争抢最后额度无超发（恰一成功一拒绝）；普通计数器
  行为不变。`TestBudgetExhaustedRunFinalization`（2 项）——多语句操作组中途
  逐语句拒绝（组估计 1<实际 2）优雅降级、旧有效结果不变、并发额度可再取得；
  组准入拦截零语句发出。

### 验证（修复轮 2 后）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **546 passed, 9 skipped**（新增 15 项；未删断言/未放宽/无新增跳过） |
| `python -m ruff check app tests scripts` / `pip check` | 全部通过 |
| 真实集成（`NL2SQL_MYSQL_INTEGRATION=1`） | **8 passed, 1 skipped**（未在真实库制造类型变化/删外键/超时） |
| 真实只读演示（hotel，临时目录） | 18 表 → 67 候选 → 5 候选 **sql_statements=34**（逐语句硬预算下如实计数）→ 1 validated + 4 needs_review → 闭环正常 |


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

未实现项：NL2SQL 与任意 SQL 执行（M6）、智能体（M7）、登录/JWT、后台任务与正式迁移（M8）。
M0–M5 离线闭环完成（M5 视图/ES 真实发布未验证，目标默认关闭）；Embedding 检索未接入。
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
