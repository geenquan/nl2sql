from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticAttribute:
    name: str
    source_table: str
    source_column: str
    data_type: str
    description: str | None = None
    status: str = "suggested"


@dataclass
class SemanticEntity:
    """业务实体。grain 必须描述数据的一行代表什么。"""

    name: str
    display_name: str
    grain: str
    source_tables: list[str] = field(default_factory=list)
    attributes: list[SemanticAttribute] = field(default_factory=list)


@dataclass
class SemanticRelationship:
    source_entity: str
    target_entity: str
    join_condition: str
    cardinality: str
    status: str = "candidate"


@dataclass
class SemanticMetric:
    """指标定义应包含粒度、表达式、过滤条件和口径状态。"""

    name: str
    display_name: str
    expression: str
    grain: str
    filters: list[str] = field(default_factory=list)
    status: str = "suggested"


@dataclass
class SemanticModel:
    version: str
    entities: list[SemanticEntity] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)
    metrics: list[SemanticMetric] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
