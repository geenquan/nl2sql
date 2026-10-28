from app.domain.relationships import RelationshipCandidate


class RelationshipDiscoveryService:
    """关系发现核心。候选召回与关系验证分离，便于分别评测和替换算法。"""

    def recall_candidates(
        self, metadata: list[dict], profiles: list[dict], semantics: list[dict]
    ) -> list[RelationshipCandidate]:
        # TODO(M3): 综合类型、字段命名、语义、基数与画像召回候选。
        # TODO(M3): 使用 Top-K 和预算限制，禁止所有字段两两无约束比较。
        raise NotImplementedError("TODO(M3): recall candidates")

    def collect_evidence(self, candidate: RelationshipCandidate) -> dict:
        # TODO(M3): 计算值域覆盖、唯一性、空值、JOIN 匹配率和行数变化。
        raise NotImplementedError("TODO(M3): collect evidence")

    def validate_candidate(self, candidate: RelationshipCandidate) -> RelationshipCandidate:
        # TODO(M3): 综合证据并标记 validated / needs_review / rejected。
        raise NotImplementedError("TODO(M3): validate candidate")
