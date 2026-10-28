from app.domain.semantic_model import SemanticModel


class SemanticModelBuilder:
    """将字段语义和已验证关系组合成可版本化的业务语义模型。"""

    def build(self, field_semantics: list[dict], relationships: list[dict]) -> SemanticModel:
        # TODO(M4): 识别实体、属性、主题、粒度和查询路径。
        # TODO(M4): 未确认的指标口径只能标记 suggested，不得默认发布。
        raise NotImplementedError("TODO(M4): build semantic model")
