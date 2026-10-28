"""M3 关系图与受限路径搜索测试。"""

import time

from app.relationships.graph import PathSearchBudget, build_graph, find_paths, to_mermaid


def _edge(key, src, dst, status="validated", cardinality=None, risks=None,
          source_columns=("a_id",), target_columns=("id",)):
    return {
        "relationship_key": key,
        "source_table_id": src, "source_schema": "db", "source_table": f"t{src}",
        "source_columns": list(source_columns),
        "target_table_id": dst, "target_schema": "db", "target_table": f"t{dst}",
        "target_columns": list(target_columns),
        "status": status,
        "origin": ["explicit_fk"],
        "risks": risks or [],
        "scores": {"total_weighted": 0.8},
        "cardinality_hint": cardinality or "many_to_one",
        "evidence": ({"cardinality_observed": {"cardinality": cardinality}}
                     if cardinality else None),
    }


class TestBuildGraph:
    def test_nodes_have_full_identity_and_status_distinction(self):
        records = [
            _edge("k1", "A", "B", status="validated"),
            _edge("k2", "A", "C", status="rejected"),
            _edge("k3", "B", "C", status="stale"),
        ]
        graph = build_graph(records, {})
        assert {n["table_id"] for n in graph["nodes"]} == {"A", "B", "C"}
        statuses = {e["relationship_key"]: e["status"] for e in graph["edges"]}
        assert statuses == {"k1": "validated", "k2": "rejected", "k3": "stale"}

    def test_status_filter(self):
        records = [
            _edge("k1", "A", "B", status="validated"),
            _edge("k2", "A", "C", status="candidate"),
        ]
        graph = build_graph(records, {}, statuses=("validated",))
        assert {e["relationship_key"] for e in graph["edges"]} == {"k1"}

    def test_edge_carries_mapping_and_risk_summary(self):
        records = [_edge("k1", "A", "B",
                         risks=[{"type": "tenant_scope", "note": "x"}],
                         source_columns=("x", "y"), target_columns=("m", "n"))]
        edge = build_graph(records, {})["edges"][0]
        assert edge["source"]["columns"] == ["x", "y"]
        assert edge["target"]["columns"] == ["m", "n"]
        assert edge["risks"] == [{"type": "tenant_scope", "note": "x"}]

    def test_mermaid_render(self):
        records = [_edge("k1", "A", "B"), _edge("k2", "A", "C", status="rejected")]
        text = to_mermaid(build_graph(records, {}))
        assert text.startswith("graph LR")
        assert "-->" in text and "validated" in text


class TestFindPaths:
    def test_only_validated_edges_used(self):
        records = [
            _edge("k1", "A", "B"),                       # validated
            _edge("k2", "B", "C", status="stale"),        # 排除
            _edge("k3", "C", "D", status="needs_review"),  # 排除
        ]
        result = find_paths(records, "A", "D")
        assert result["path_count"] == 0

    def test_multi_hop_path_found_with_risk_notes(self):
        records = [
            _edge("k1", "A", "B"),
            _edge("k2", "B", "C", cardinality="many_to_many",
                  risks=[{"type": "composite_key_risk", "note": "组合条件"}]),
            _edge("k3", "C", "D"),
        ]
        result = find_paths(records, "A", "D")
        assert result["path_count"] == 1
        path = result["paths"][0]
        assert [hop["to_table_id"] for hop in path[:3]] == ["B", "C", "D"]
        notes = path[-1]["path_risk_notes"]
        assert any("多对多" in n for n in notes)
        assert any("组合条件缺失" in n for n in notes)

    def test_cycle_avoided_on_self_reference(self):
        records = [_edge("k1", "A", "A")]  # 自关联
        result = find_paths(records, "A", "A")
        assert result["path_count"] == 0  # 防环：不产生 A→A→A

    def test_directed_only_no_reverse_traversal(self):
        # 边方向 A→B；从 B 到 A 无路径（不逆向使用边）
        records = [_edge("k1", "A", "B")]
        assert find_paths(records, "B", "A")["path_count"] == 0
        assert find_paths(records, "A", "B")["path_count"] == 1

    def test_hop_limit(self):
        records = [_edge(f"k{i}", chr(65 + i), chr(66 + i)) for i in range(5)]
        assert find_paths(records, "A", "F",
                          PathSearchBudget(max_hops=2))["path_count"] == 0
        assert find_paths(records, "A", "F",
                          PathSearchBudget(max_hops=5))["path_count"] == 1

    def test_path_limit_and_budget(self):
        # 菱形图 A→B, A→C, B→D, C→D：两条路径，限制取 1
        records = [
            _edge("k1", "A", "B"), _edge("k2", "A", "C"),
            _edge("k3", "B", "D"), _edge("k4", "C", "D"),
        ]
        result = find_paths(records, "A", "D", PathSearchBudget(max_paths=1))
        assert result["path_count"] == 1
        assert result["stopped_reason"] == "max_paths_reached"

    def test_deadline_budget(self):
        records = [_edge("k1", "A", "B")]
        result = find_paths(
            records, "A", "B",
            PathSearchBudget(deadline=time.monotonic() - 1))
        assert result["path_count"] == 0
        assert result["stopped_reason"] == "deadline_exceeded"

    def test_node_budget(self):
        records = [_edge(f"k{i}", chr(65 + i), chr(66 + i)) for i in range(5)]
        result = find_paths(records, "A", "F",
                            PathSearchBudget(max_visited_nodes=1))
        assert result["stopped_reason"] == "max_visited_nodes_reached"
