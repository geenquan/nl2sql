from dataclasses import dataclass, field


@dataclass
class DatasetSpec:
    """数据集定义；在生成 SQL 前必须明确 grain（一行代表什么）。"""

    name: str
    subject: str
    grain: str
    fields: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    relationship_ids: list[str] = field(default_factory=list)
    build_mode: str = "view"
    version: str = "draft"
