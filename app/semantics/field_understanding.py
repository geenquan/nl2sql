from typing import Any


class FieldUnderstandingService:
    """把元数据和画像转换为结构化语义候选；不确定时必须允许 unknown。"""

    def infer_table_purpose(self, table_context: dict[str, Any]) -> dict[str, Any]:
        # TODO(M2): 规则初筛 + LLM 结构化推理；记录证据和模型版本。
        raise NotImplementedError("TODO(M2): infer table purpose")

    def infer_column_semantics(self, column_context: dict[str, Any]) -> dict[str, Any]:
        # TODO(M2): 结合字段名、注释、类型、画像和有限脱敏样本。
        # 注意：语义相似不等于 JOIN 关系成立。
        raise NotImplementedError("TODO(M2): infer column semantics")
