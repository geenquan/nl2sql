# 附录：Claude Code 任务模板、输入输出样例与通用验收清单

## 1. 单个里程碑任务模板

```text
你正在实现“企业数据库自动理解与 NL2SQL”项目。

请先阅读：
- 00_总路线与里程碑指导手册.md
- 对应的阶段手册

本次目标：<填写里程碑和子任务>
本次不做：<明确后续功能边界>

执行要求：
1. 先检查仓库结构和现有代码，再给出实施计划；
2. 列出准备新增/修改的文件和数据模型变更；
3. 按现有架构实现，不要无理由重写已有模块；
4. 为核心逻辑写单元测试和必要的集成测试；
5. 数据库默认只读，凭证不得进入日志或模型上下文；
6. 所有模型输出都要做结构校验；
7. 对 SQL 执行、权限和异常路径写负向测试；
8. 运行测试并如实报告，不得伪造通过结果；
9. 提供可复制的启动命令、API 示例、输入输出样例；
10. 列出未完成项、风险和下一阶段依赖。
```

## 2. 示例：关系发现的输入输出展示

### 输入

```json
{
  "source_table": "T_DEVICE",
  "source_column": "DEV_ID",
  "target_table": "T_DEFECT",
  "target_column": "OBJ_CODE",
  "source_type": "varchar(32)",
  "target_type": "varchar(32)",
  "source_unique_rate": 0.998,
  "target_null_rate": 0.02
}
```

### 输出

```json
{
  "candidate_id": "rel_0001",
  "source": "T_DEVICE.DEV_ID",
  "target": "T_DEFECT.OBJ_CODE",
  "candidate_cardinality": "one_to_many",
  "evidence": {
    "type_compatibility": "compatible",
    "value_coverage": null,
    "source_uniqueness": 0.998,
    "join_validation": "pending"
  },
  "confidence_level": "medium",
  "status": "candidate",
  "next_action": "run_bounded_join_validation"
}
```

注意：示例中的 `value_coverage: null` 表示尚未计算，不能把缺失证据当作通过。

## 3. 示例：数据集构建预览

```json
{
  "dataset_name": "device_overview",
  "grain": "one_row_per_device",
  "sources": ["T_DEVICE", "T_DEFECT"],
  "relationship_ids": ["rel_0001"],
  "fields": [
    "device_id",
    "device_name",
    "organization",
    "defect_count",
    "unresolved_defect_count"
  ],
  "build_mode": "view",
  "validation_status": "pending",
  "publish_status": "draft"
}
```

前端应显示“草稿/待校验”，而不是在 SQL 尚未执行和对账前显示“已成功生成”。

## 4. 每次提交前的通用检查

- [ ] 是否只实现当前任务范围？
- [ ] 是否有单元测试？
- [ ] 是否有异常路径测试？
- [ ] 是否有输入输出 Schema？
- [ ] 是否有权限与数据安全检查？
- [ ] 是否记录版本和证据？
- [ ] 是否有实际运行输出，而非静态 mock？
- [ ] 是否更新 README 和阶段文档？
- [ ] 是否报告真实测试结果？
- [ ] 是否保留失败原因和已知限制？

## 5. 推荐开发节奏

- 一个里程碑拆成多个可独立验收的小任务；
- 每次只修改有限模块；
- 先让数据模型和 API 稳定，再做复杂前端；
- 先在合成数据上测试，再用脱敏真实数据验证；
- 每个阶段保留一个可运行版本；
- 核心评测结果纳入版本控制；
- 关系发现或数据对账不通过时，不要靠跳过测试进入下一阶段。

## 6. 风险登记表模板

| 风险 | 影响 | 发现方式 | 缓解措施 | 状态 |
|---|---|---|---|---|
| 错误关联 | 错误查询结果 | 关系真值集、对账 | 关系状态门槛、人工审核 | 待评估 |
| JOIN 行数膨胀 | 指标重复计算 | 行数与粒度测试 | 先聚合再关联 | 待评估 |
| 敏感数据外发 | 合规风险 | 审计、数据流检查 | 脱敏、本地模型、最小样本 | 待评估 |
| Schema 漂移 | 视图失效 | 快照差异与定期扫描 | 标记 stale、重验、回滚 | 待评估 |
| 模型幻觉 | 错误语义 | 人工抽检、证据核验 | unknown 状态、规则约束 | 待评估 |
