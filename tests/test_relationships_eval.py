"""M3 关系评测集（合成数据 + 开发标注，非真实业务人工金标准）。

标签来源说明：正例/负例由开发者按合成库设计标注（tests/relationship_fixtures.py），
用于回归监控与基线比较；不冒充真实业务人工金标准。

评测候选空间声明：Recall 分母 = 本合成库中开发者标注的全部真实引用关系
（9 条），不是全库任意字段对的召回率；该口径在报告中如实表达。

未评测项：LLM 基线与 Embedding 基线未实现（M2 未接入 Embedding、本轮不扩大
模型调用），不做比较、不虚构结果。
"""

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.core.config import Settings
from app.services.relationship_service import RelationshipService
from tests.relationship_fixtures import (
    SCHEMA,
    FakeM3Connector,
    FakeRelationshipProbe,
    seed_catalog,
    seed_profiles,
    seed_semantics,
    synthetic_catalog,
)
from tests.test_relationships_service import FakeCredentialStore

# 开发标注标签（无序端点对 + 期望基数）
POSITIVES = {
    frozenset({("booking", "guest_id"), ("guest", "id")}): "many_to_one",
    frozenset({("booking", "room_id"), ("room", "id")}): "many_to_one",
    frozenset({("guest", "tenant_id"), ("tenant", "id")}): "many_to_one",
    frozenset({("room", "tenant_id"), ("tenant", "id")}): "many_to_one",
    frozenset({("room", "type_id"), ("room_type", "id")}): "many_to_one",
    frozenset({("booking_item", "booking_id"), ("booking", "id")}): "many_to_one",
    frozenset({("booking_item", "item_id"), ("service_item", "id")}): "many_to_one",
    frozenset({("payment", "booking_no"), ("booking", "booking_no")}): "many_to_one",
    frozenset({("employee", "manager_id"), ("employee", "id")}): "many_to_one",
}
NEGATIVES = {
    frozenset({("guest", "status_code"), ("booking", "status_code")}),  # 共享状态码
    frozenset({("room_type", "code"), ("service_item", "code")}),  # 字典↔字典
    frozenset({("payment", "bank_txn_no"), ("booking", "id")}),  # 字符串 vs 整数
    frozenset({("booking", "tenant_seq"), ("tenant", "id")}),  # 租户内编号重复
    frozenset({("booking", "room_id"), ("room_type", "id")}),  # 表名 token 命中但语义不符
    frozenset({("room", "room_no"), ("room_type", "code")}),  # 同 room 词但编码不同
}


def _pair(record: dict) -> frozenset:
    return frozenset({
        (record["source_table"], record["source_columns"][0]),
        (record["target_table"], record["target_columns"][0]),
    })


def _precision_recall_f1(predicted: set, labels: set) -> tuple[float, float, float]:
    tp = len(predicted & labels)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(labels) if labels else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return precision, recall, f1


@pytest.fixture
def validated_env(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    catalog = synthetic_catalog(source_id=source_id)
    seed_catalog(repo, catalog, source_id=source_id)
    seed_profiles(repo, catalog)
    seed_semantics(repo, catalog)
    service = RelationshipService(
        metadata_repository=repo, source_repository=repo,
        credential_store=FakeCredentialStore({"cred-1": "secret"}),
        connector_factory=lambda params: FakeM3Connector(catalog),
        probe_factory=lambda params: FakeRelationshipProbe(),
        settings=Settings(_env_file=None,
                          relationship_validation_max_candidates=20),
    )
    discovery = service.discover_relationships(source_id)
    validation = service.validate_batch(source_id, limit=20)
    return {"service": service, "repo": repo, "source_id": source_id,
            "discovery": discovery, "validation": validation}


class TestRecallLevelMetrics:
    def test_recall_precision_f1_vs_labels(self, validated_env):
        records = validated_env["service"].list_relationships(
            validated_env["source_id"])
        predicted = {_pair(r) for r in records}
        precision, recall, f1 = _precision_recall_f1(predicted, set(POSITIVES))
        # 合成集回归阈值：全部正例召回；3 条误报均为标注负例
        # （tenant_seq 租户编号、room_id→room_type、room_no→room_type.code 同名词法噪声）
        assert recall == 1.0, predicted - set(POSITIVES) - NEGATIVES
        assert precision == 0.75  # 9/12，回归锚点
        assert f1 >= 0.857

    def test_shared_code_and_type_negatives_not_recalled(self, validated_env):
        records = validated_env["service"].list_relationships(
            validated_env["source_id"])
        predicted = {_pair(r) for r in records}
        # 这四类负例在召回层即被防线挡住（不进入候选）
        for negative in NEGATIVES - {
            frozenset({("booking", "tenant_seq"), ("tenant", "id")}),
            frozenset({("booking", "room_id"), ("room_type", "id")}),
            frozenset({("room", "room_no"), ("room_type", "code")}),
        }:
            assert negative not in predicted, negative

    def test_name_only_baseline_is_worse(self, validated_env):
        """字段名相同即候选的基线：同空间下 Precision/Recall 均更差。"""
        catalog = synthetic_catalog()
        # 同名（表,列）对 → 候选（无序）
        by_name: dict[str, list[str]] = {}
        for table in catalog:
            for column in table["columns"]:
                by_name.setdefault(column["name"], []).append(table["table_name"])
        baseline: set[frozenset] = set()
        for col_name, owners in by_name.items():
            for i in range(len(owners)):
                for j in range(i + 1, len(owners)):
                    baseline.add(frozenset({
                        (owners[i], col_name), (owners[j], col_name)}))
        records = validated_env["service"].list_relationships(
            validated_env["source_id"])
        ours = {_pair(r) for r in records}
        base_p, base_r, _ = _precision_recall_f1(baseline, set(POSITIVES))
        our_p, our_r, _ = _precision_recall_f1(ours, set(POSITIVES))
        # 基线因 id↔id/name↔name/status↔status 大量配对而精度极低；
        # 又因 FK 名不同（guest_id vs id）召回也低
        assert base_p < our_p
        assert base_r < our_r
        assert len(baseline) > 40  # 基线候选规模大（对照单候选成本）


class TestValidationLevelMetrics:
    def test_validated_and_needs_review_split(self, validated_env):
        service = validated_env["service"]
        validated = service.list_relationships(
            validated_env["source_id"], status="validated")
        needs_review = service.list_relationships(
            validated_env["source_id"], status="needs_review")
        assert { _pair(r) for r in validated } == set(POSITIVES)
        assert all(_pair(r) in NEGATIVES for r in needs_review)

    def test_high_confidence_errors_zero(self, validated_env):
        """高置信错误（自动 validated 但标签为负例）必须为 0。"""
        validated = validated_env["service"].list_relationships(
            validated_env["source_id"], status="validated")
        high_confidence_errors = [
            r for r in validated if _pair(r) in NEGATIVES
        ]
        assert high_confidence_errors == []

    def test_cardinality_accuracy(self, validated_env):
        validated = validated_env["service"].list_relationships(
            validated_env["source_id"], status="validated")
        correct = [
            r for r in validated
            if POSITIVES[_pair(r)] == (
                (r.get("evidence") or {}).get("cardinality_observed", {})
                .get("cardinality"))
        ]
        assert len(correct) == len(validated)  # 全部基数判断正确（合成集）

    def test_queries_per_candidate_within_budget(self, validated_env):
        run = validated_env["validation"]
        per_candidate = [
            len(result.get("queries", []))
            for result in run["results"]
        ]
        assert per_candidate and max(per_candidate) <= 4  # 每候选 ≤4 条聚合查询
        assert run["queries_used"] <= run["budget"]["max_queries"]


class TestPathMetrics:
    def test_three_hop_path_and_no_cycle(self, validated_env):
        service, source_id = (validated_env["service"],
                              validated_env["source_id"])
        tables = {
            t["table_name"]: t["table_id"]
            for t in validated_env["repo"].list_table_metadata(source_id)
        }
        result = service.paths(source_id, tables["booking_item"],
                               tables["tenant"], max_hops=3)
        assert result["path_count"] >= 1
        first = result["paths"][0][:-1]
        # 路径不重复经过节点（无环）
        visited = {first[0]["from_table_id"]}
        for hop in first:
            assert hop["to_table_id"] not in visited
            visited.add(hop["to_table_id"])
        # 自关联不产生 A→A→A 环路径
        self_result = service.paths(source_id, tables["employee"],
                                    tables["employee"], max_hops=3)
        assert self_result["path_count"] == 0

    def test_unreachable_direction_reported(self, validated_env):
        service, source_id = (validated_env["service"],
                              validated_env["source_id"])
        tables = {
            t["table_name"]: t["table_id"]
            for t in validated_env["repo"].list_table_metadata(source_id)
        }
        # 有向边 payment→booking，而 booking 无法回到 payment：不应有路径
        result = service.paths(source_id, tables["booking"], tables["payment"])
        assert result["path_count"] == 0
