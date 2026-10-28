# Claude Code 分阶段开发指南

## 开发规则

- 以现有 Conda `nl2sql` / Python 3.13.18 为准，不按旧版说明降级或重建环境；配置与启动步骤见 [configuration.md](configuration.md)。
- 先阅读 [development-status.md](development-status.md) 的真实验证与未实现项；依赖已安装，仍需按任务复验。
- 保留用户已更新的 `.env` 和 `.env.example`，不要用默认模板覆盖或输出其中凭证；数据源用 `DATA_MYSQL_*`，系统库用 `BIZ_MYSQL_*`，模型用 `DEEPSEEK_*`。
- 新增配置时同步更新 `app/core/config.py`、`.env.example` 和配置文档，不提交本机 `.env`。
- 一次只完成一个里程碑，禁止顺手实现后续阶段。
- 先检查代码现状，再提出计划；不要重建已有架构。
- 优先复用 `app/domain/` 和 `app/ports/` 的契约。
- 算法写在对应模块，`services/` 只做编排。
- 先写测试，再实现核心逻辑；至少覆盖正常、异常和安全路径。
- 只有真实实现并通过测试后才能把 TODO 标记为完成。
- 不要把真实密码、API Key 或未脱敏样本放入日志、代码或提示词。
- 输出示例必须标记为真实运行结果或说明是示例，不能混淆。

## 推荐每次给 Claude Code 的提示词

```text
请先阅读 README.md、docs/architecture.md、docs/milestones.md、
docs/data-contracts.md、docs/configuration.md、docs/development-status.md、
docs/claude-code-guide.md，以及本阶段涉及模块。

本次只实现：M<N> / <具体子任务>。
本次明确不做：所有后续阶段功能。

请先检查当前代码并给出实施计划、文件变更清单和接口影响。
要求：
1. 保持模块化单体架构，不引入无必要的新基础设施；
2. 优先实现现有 TODO，不要无理由重命名核心对象或接口；
3. 如必须修改跨阶段契约，先解释原因和兼容影响；
4. 为新增逻辑写单元/集成测试，并实际运行；
5. 源数据库默认只读，密码不进日志，SQL 执行必须经过安全校验；
6. 不得伪造运行结果或测试结果；
7. 最后提供变更文件、运行命令、真实输入输出、测试结果、已知限制和下一阶段依赖。
```

## 推荐开发顺序

M0 骨架 → M1 数据源/画像 → M2 字段语义 → M3 关系发现 → M4 语义层 → M5 数据集生成 → M6 NL2SQL → M7 智能体 → M8 产品化。

M3 评测不过，不要跳到自动宽表发布；M5 对账不过，不要进入正式 NL2SQL；M6 安全检查不过，不要开放查询执行。
