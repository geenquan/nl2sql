"""M3 关系图数据与受限多跳路径搜索。

- 节点=表（完整标识 table_id/schema/table）；边=关系（方向、字段映射、
  状态、基数、风险与证据摘要）；候选/拒绝/过期边可见但状态清晰区分；
- 默认路径搜索仅使用当前有效且非 stale 的 validated 边（ADR-002：
  未验证关系不得默认用于生产查询路径）；
- 限制最大跳数、路径数、搜索节点数与期限；同路径防环；
  禁止跨数据源/快照混用（构建时过滤）；
- 多对多、粒度不兼容、组合条件缺失的路径带风险标记；
  图连通不等于业务 JOIN 正确，不生成生产 SQL（M4 范畴）。
"""

import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class PathSearchBudget:
    max_hops: int = 3
    max_paths: int = 5
    max_visited_nodes: int = 200
    deadline: float | None = None
    clock: Callable[[], float] = _time.monotonic


def build_graph(
    records: list[dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
    *,
    statuses: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """构建图 JSON。records 为关系修订（通常 effective 或最新）。"""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for record in records:
        if statuses is not None and record.get("status") not in statuses:
            continue
        for table_id, schema, table_name, columns in (
            (record.get("source_table_id"), record["source_schema"],
             record["source_table"], record["source_columns"]),
            (record.get("target_table_id"), record["target_schema"],
             record["target_table"], record["target_columns"]),
        ):
            if table_id is None:
                continue
            node = nodes.setdefault(table_id, {
                "table_id": table_id,
                "schema_name": schema,
                "table_name": table_name,
                "table_full": f"{schema}.{table_name}",
            })
            node.setdefault("column_count", None)
            table = tables_by_id.get(table_id) or {}
            node["column_count"] = len(table.get("columns") or [])
        evidence_summary = None
        coverage = ((record.get("evidence") or {}).get("coverage") or {})
        forward = coverage.get("forward") or {}
        if forward:
            evidence_summary = {
                "forward_ratio": forward.get("ratio"),
                "forward_basis": forward.get("basis"),
            }
        edges.append({
            "relationship_key": record["relationship_key"],
            "source": {
                "table_id": record.get("source_table_id"),
                "table_full": f"{record['source_schema']}.{record['source_table']}",
                "columns": list(record["source_columns"]),
            },
            "target": {
                "table_id": record.get("target_table_id"),
                "table_full": f"{record['target_schema']}.{record['target_table']}",
                "columns": list(record["target_columns"]),
            },
            "status": record.get("status"),
            "cardinality": (record.get("evidence") or {})
            .get("cardinality_observed", {}).get("cardinality")
            or record.get("cardinality_hint"),
            "origin": list(record.get("origin") or []),
            "risks": list(record.get("risks") or []),
            "score": (record.get("scores") or {}).get("total_weighted"),
            "evidence_summary": evidence_summary,
        })
    return {"nodes": sorted(nodes.values(), key=lambda n: n["table_full"]),
            "edges": edges}


def find_paths(
    records: list[dict[str, Any]],
    from_table_id: str,
    to_table_id: str,
    budget: PathSearchBudget | None = None,
) -> dict[str, Any]:
    """在 validated 且非 stale 的有效关系上做受限路径搜索（DFS 枚举 + 防环）。

    返回 paths（每条含逐跳边与风险汇总）与搜索预算消耗；连通不代表业务
    JOIN 正确，多跳边带基数/组合条件风险提示。
    """
    budget = budget or PathSearchBudget()
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for record in records:
        if record.get("status") != "validated":
            continue  # 默认仅 validated；stale/rejected/needs_review 不进入路径
        src = record.get("source_table_id")
        dst = record.get("target_table_id")
        if not src or not dst:
            continue
        adjacency.setdefault(src, []).append((dst, record))
    # 防环 + 双向遍历会重复；关系允许双向走（B→A 用同一条边表示反向语义时
    # 基数会反转），路径仅沿 source→target 方向行进，避免语义混淆。
    paths: list[list[dict[str, Any]]] = []
    visited_nodes = 0
    stopped_reason: str | None = None

    def _walk(current: str, target: str, trail: list[dict[str, Any]],
              seen: set[str]) -> None:
        nonlocal visited_nodes, stopped_reason
        if stopped_reason is not None:
            return
        if len(paths) >= budget.max_paths:
            stopped_reason = "max_paths_reached"
            return
        if budget.deadline is not None and budget.clock() >= budget.deadline:
            stopped_reason = "deadline_exceeded"
            return
        if visited_nodes >= budget.max_visited_nodes:
            stopped_reason = "max_visited_nodes_reached"
            return
        if current == target and trail:
            paths.append(list(trail))
            return
        if len(trail) >= budget.max_hops:
            return
        for next_node, record in adjacency.get(current, ()):
            if next_node in seen:  # 同路径防环
                continue
            visited_nodes += 1
            hop = {
                "from_table_id": current,
                "to_table_id": next_node,
                "relationship_key": record["relationship_key"],
                "source_columns": list(record["source_columns"]),
                "target_columns": list(record["target_columns"]),
                "cardinality": (record.get("evidence") or {})
                .get("cardinality_observed", {}).get("cardinality")
                or record.get("cardinality_hint"),
                "risks": list(record.get("risks") or []),
            }
            _walk(next_node, target, trail + [hop], seen | {next_node})

    _walk(from_table_id, to_table_id, [], {from_table_id})
    for path in paths:
        risk_notes: list[str] = []
        for hop in path:
            if hop["cardinality"] == "many_to_many":
                risk_notes.append(
                    f"{hop['relationship_key'][:8]}：多对多，JOIN 需聚合或条件约束")
            if any(r.get("type") == "composite_key_risk" for r in hop["risks"]):
                risk_notes.append(
                    f"{hop['relationship_key'][:8]}：组合条件缺失风险")
        path.append({
            "path_risk_notes": sorted(set(risk_notes)),
            "path_note": "图连通不等于业务 JOIN 正确；执行前需核对粒度与口径",
        })
    return {
        "from_table_id": from_table_id,
        "to_table_id": to_table_id,
        "paths": paths,
        "path_count": len(paths),
        "stopped_reason": stopped_reason,
        "searched_nodes": visited_nodes,
        "budget": {
            "max_hops": budget.max_hops,
            "max_paths": budget.max_paths,
            "max_visited_nodes": budget.max_visited_nodes,
        },
        "note": "仅沿 source→target 方向的 validated 有效边搜索",
    }


def to_mermaid(graph: dict[str, Any]) -> str:
    """把图数据渲染为 Mermaid flowchart 文本（演示/文档用）。"""
    aliases: dict[str, str] = {}
    lines = ["graph LR"]
    for index, node in enumerate(graph.get("nodes", [])):
        alias = f"T{index}"
        aliases[node["table_id"]] = alias
        safe_name = node["table_full"].replace('"', "'")
        lines.append(f"    {alias}[\"{safe_name}\"]")
    style_map = {
        "validated": "-->", "evidence_collected": "-.->",
        "needs_review": "-.->", "candidate": "-.->",
        "rejected": "-. x", "stale": "-.->",
    }
    for edge in graph.get("edges", []):
        src = aliases.get((edge["source"] or {}).get("table_id") or "")
        dst = aliases.get((edge["target"] or {}).get("table_id") or "")
        if not src or not dst:
            continue
        arrow = style_map.get(edge.get("status") or "", "-.->")
        label = f"{edge.get('status')}|{edge.get('cardinality') or 'unknown'}"
        lines.append(f"    {src} {arrow} {dst}[\"{label}\"]")
    return "\n".join(lines)
