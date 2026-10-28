from dataclasses import dataclass, field
from typing import Any, Literal

RelationshipStatus = Literal[
    "candidate", "evidence_collected", "needs_review", "validated", "rejected", "stale"
]


@dataclass
class RelationshipCandidate:
    """候选关系及证据；候选不等于真实关系。"""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    cardinality: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence_level: str = "unknown"
    status: RelationshipStatus = "candidate"
