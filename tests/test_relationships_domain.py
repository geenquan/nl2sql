"""M3 领域契约测试：逻辑键稳定性、状态机、有效结果解析。"""

from app.domain.relationships import (
    RELATIONSHIP_TRANSITIONS,
    relationship_key,
    select_effective_relationship,
)


def _row(revision, status, source="system", **extra):
    return {"revision": revision, "status": status, "inference_source": source,
            "relationship_key": "k", **extra}


class TestRelationshipKey:
    def test_key_is_direction_sensitive(self):
        a = relationship_key("s", "db", "orders", ["customer_id"], "db", "customers", ["id"])
        b = relationship_key("s", "db", "customers", ["id"], "db", "orders", ["customer_id"])
        assert a != b

    def test_key_uses_full_identity_not_column_name(self):
        # 同名列不同表 → 不同键（不能仅凭列名合并）
        a = relationship_key("s", "db", "orders", ["code"], "db", "dict_a", ["code"])
        b = relationship_key("s", "db", "payments", ["code"], "db", "dict_b", ["code"])
        assert a != b

    def test_key_stable_across_snapshots(self):
        # 逻辑身份与快照无关：同一关系跨快照共享键
        a = relationship_key("s", "db", "orders", ["customer_id"], "db", "customers", ["id"])
        assert a == relationship_key(
            "s", "db", "orders", ["customer_id"], "db", "customers", ["id"])

    def test_key_source_scoped(self):
        a = relationship_key("s1", "db", "t", ["a"], "db", "u", ["b"])
        b = relationship_key("s2", "db", "t", ["a"], "db", "u", ["b"])
        assert a != b

    def test_composite_mapping_order_sensitive(self):
        a = relationship_key("s", "db", "t", ["x", "y"], "db", "u", ["m", "n"])
        b = relationship_key("s", "db", "t", ["y", "x"], "db", "u", ["m", "n"])
        assert a != b  # 有序字段映射


class TestTransitions:
    def test_machine_and_human_paths_exist(self):
        # 机器路径：candidate → evidence_collected → validated/needs_review；→ stale
        assert "validated" in RELATIONSHIP_TRANSITIONS["candidate"]
        assert "needs_review" in RELATIONSHIP_TRANSITIONS["evidence_collected"]
        assert "stale" in RELATIONSHIP_TRANSITIONS["validated"]
        # 人工路径：revert 回 candidate；rejected → stale
        assert "candidate" in RELATIONSHIP_TRANSITIONS["rejected"]
        assert "stale" in RELATIONSHIP_TRANSITIONS["rejected"]

    def test_stale_can_be_revalidated(self):
        assert "evidence_collected" in RELATIONSHIP_TRANSITIONS["stale"]
        assert "validated" in RELATIONSHIP_TRANSITIONS["stale"]


class TestSelectEffective:
    def test_latest_machine_wins_without_human(self):
        rows = [_row(2, "needs_review"), _row(1, "candidate")]
        assert select_effective_relationship(rows)["revision"] == 2

    def test_human_latest_wins(self):
        rows = [_row(3, "rejected", "human"), _row(2, "validated"),
                _row(1, "candidate")]
        assert select_effective_relationship(rows)["status"] == "rejected"

    def test_machine_does_not_override_human(self):
        # 新发现/新证据不能覆盖人工结论（新发现不覆盖已有人工审核）
        rows = [_row(3, "candidate"), _row(2, "validated", "human"),
                _row(1, "candidate")]
        effective = select_effective_relationship(rows)
        assert effective["revision"] == 2 and effective["status"] == "validated"

    def test_stale_latest_takes_effect(self):
        # 过期必须改变有效结果，不能继续优先旧 validated
        rows = [_row(3, "stale"), _row(2, "validated"), _row(1, "candidate")]
        assert select_effective_relationship(rows)["status"] == "stale"

    def test_stale_after_human_then_revalidated(self):
        # 人工确认 → 结构变化 stale → 重验通过：生效最新机器 validated
        rows = [_row(4, "validated"), _row(3, "stale"),
                _row(2, "validated", "human"), _row(1, "candidate")]
        effective = select_effective_relationship(rows)
        assert effective["revision"] == 4 and effective["status"] == "validated"

    def test_stale_after_human_still_current(self):
        rows = [_row(3, "stale"), _row(2, "validated", "human"),
                _row(1, "candidate")]
        assert select_effective_relationship(rows)["status"] == "stale"

    def test_human_reject_not_revived_by_machine(self):
        rows = [_row(3, "needs_review"), _row(2, "rejected", "human"),
                _row(1, "candidate")]
        effective = select_effective_relationship(rows)
        assert effective["status"] == "rejected"

    def test_human_revert_to_candidate_effective(self):
        rows = [_row(3, "candidate", "human"), _row(2, "validated", "human"),
                _row(1, "candidate")]
        assert select_effective_relationship(rows)["status"] == "candidate"
