from app.domain.semantic_model import SemanticModel


class NL2SQLService:
    """基于已发布语义层生成 SQL，不直接把整个数据库 Schema 发给模型。"""

    def generate_sql(self, question: str, model: SemanticModel, user_context: dict) -> dict:
        # TODO(M6): 问题解析、语义检索、路径选择和 SQL 生成。
        # 输出至少包括 sql、assumptions、selected_metrics、selected_relationships。
        raise NotImplementedError("TODO(M6): generate SQL")
