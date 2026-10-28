from app.schemas.milestone import MilestoneResponse


class MilestoneService:
    """开发阶段清单。当前状态是代码基线，不代表自动检测的完成度。"""

    _items = (
        ("M0", "架构骨架与开发约定", "done", "目录、核心契约、文档、健康检查"),
        ("M1", "数据源接入与元数据画像", "done", "MySQL 连接、元数据扫描、字段画像与快照持久化"),
        ("M2", "字段语义理解", "todo", "字段/表用途候选、证据和人工修订"),
        ("M3", "跨表关系发现与验证", "todo", "候选召回、关系证据、基数和验证"),
        ("M4", "业务语义层", "todo", "实体、关系、指标、粒度和查询路径"),
        ("M5", "视图/宽表生成", "todo", "SQL 预览、质量校验、发布和回滚"),
        ("M6", "NL2SQL", "todo", "语义检索、SQL 安全、只读执行和结果评测"),
        ("M7", "查询型智能体", "todo", "受控工具编排、反馈和审计"),
        ("M8", "产品化治理", "todo", "权限、漂移、运维、备份和部署"),
    )

    def list_milestones(self) -> list[MilestoneResponse]:
        return [
            MilestoneResponse(id=i, name=n, status=s, description=d)
            for i, n, s, d in self._items
        ]
