from app.domain.datasets import DatasetSpec
from app.domain.semantic_model import SemanticModel


class DatasetBuilder:
    """根据语义模型生成可审查的视图/宽表，并在发布前执行质量校验。"""

    def preview_sql(self, model: SemanticModel, spec: DatasetSpec) -> str:
        # TODO(M5): 先生成 SQL 草稿和来源说明；预览阶段不执行、不发布。
        raise NotImplementedError("TODO(M5): preview SQL")

    def validate_dataset(self, spec: DatasetSpec) -> dict:
        # TODO(M5): 检查粒度唯一性、JOIN 覆盖率、行数膨胀和指标对账。
        raise NotImplementedError("TODO(M5): validate dataset")

    def publish_dataset(self, spec: DatasetSpec) -> dict:
        # TODO(M5): 仅发布通过校验的版本；保留版本号和回滚点。
        raise NotImplementedError("TODO(M5): publish dataset")
