# 项目开发入口

开始任务前阅读：

- `docs/development-status.md`：最新环境、真实验证结果、未实现项与下一步。
- `docs/configuration.md`：配置字段、别名和加载规则。
- `docs/architecture.md`、`docs/milestones.md`、`docs/data-contracts.md`、`docs/claude-code-guide.md`。

使用现有 Conda `nl2sql` 环境，当前 Python 3.13.18，以实际解释器为准；不要按照历史说明降级到 3.12 或重建已有环境。
依赖已安装，新增依赖应同步维护 `pyproject.toml`。`environment.yml` 是重建定义，不是完整锁文件。

保留用户已更新的 `.env` 和 `.env.example`，不要覆盖或回显凭证。
系统业务库用 `BIZ_MYSQL_*`，只读查询源用 `DATA_MYSQL_*`，模型用 `DEEPSEEK_*`。
通过 `app/core/config.py` 获取配置，复用现有领域模型与 ports，具体代码属性映射见配置文档。

当前进度：M0 与 M1（MySQL 只读接入、元数据扫描、字段画像、SQLite 元数据快照持久化）已完成并有真实验证，
且已通过缺陷修复轮（422 不回显密码、目录发布与任务状态分离、值格式脱敏、扫描异常收尾、凭证文件并发安全）。
ES 写入、DeepSeek 调用、登录/JWT、任意 SQL 执行仍未实现，不能根据配置存在就声称功能已完成。
按用户当前任务和里程碑实施；实际运行相关测试，更新交接报告，明确未验证的远端服务。

常用验证：激活环境后执行 `python -m pytest -q`、`python -m ruff check app tests`、`python -m pip check`；
真实 MySQL 集成测试默认跳过，用 `NL2SQL_MYSQL_INTEGRATION=1 python -m pytest tests/integration -q` 显式运行。
