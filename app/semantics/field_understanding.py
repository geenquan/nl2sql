"""字段/表语义理解门面：规则初筛（M2 基线），模型融合由服务层编排。"""

from typing import Any

from app.semantics.glossary import Glossary
from app.semantics.rules import RULES_VERSION, RuleEngine


class FieldUnderstandingService:
    """把元数据和画像转换为结构化语义候选；不确定时必须允许 unknown。

    注意：语义相似不等于 JOIN 关系成立；候选类别和实体是候选判断，
    不是校准概率或已验证事实。
    """

    def __init__(self, glossary: Glossary | None = None) -> None:
        self._engine = RuleEngine(glossary)

    @property
    def rules_version(self) -> str:
        return RULES_VERSION

    def infer_column_semantics(self, column_context: dict[str, Any]) -> dict[str, Any]:
        return self._engine.infer_column(column_context)

    def infer_table_purpose(self, table_context: dict[str, Any]) -> dict[str, Any]:
        return self._engine.infer_table_purpose(table_context)
