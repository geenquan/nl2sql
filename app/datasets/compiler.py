"""M5 确定性 SQL 编译器（dataset-compile-v1）。

输入为结构化数据集定义 + 白名单（M4 实体字段/关系映射），输出参数化 SQL：
- 标识符全部来自白名单并加反引号；输出别名唯一稳定；
- 过滤值全部参数化（%(pN)s），日志不展开参数；
- detail_view：中心表字段 + 沿 validated many_to_one/one_to_one 关系的
  维度 LEFT/INNER JOIN（默认 LEFT 保留中心行，缺失记 NULL）；
- aggregate：中心 grain 分组聚合 + **一跳明细预聚合 CTE**（明细按完整
  关联键分组独立计算→关联键上至多一行→连中心表保持 grain）；
- NULL 语义：过滤默认作用于中心表（不改变 LEFT JOIN 行保留）；过滤在
  维度表列时编译为 ON 子句条件（避免 WHERE 把 LEFT JOIN 变 INNER）；
- 不产生 DISTINCT/SUM(DISTINCT)；AVG 由编译器以 SUM/COUNT 表达口径。
"""

import hashlib
import json
from typing import Any

from app.datasets.safety import validate_controlled_select
from app.domain.datasets import (
    DATASET_COMPILER_VERSION,
    CompiledPlan,
    DatasetSpec,
    FieldRef,
)

_AGGREGATION_SQL = {
    "count_rows": "COUNT(*)",
    # AVG 明确为和/计数口径（不做子组 AVG 合并）
    "avg": "SUM({col}) / NULLIF(COUNT({col}), 0)",
}


def quote(identifier: str) -> str:
    """MySQL 反引号标识符（白名单内，防御性拒绝反引号/控制字符）。"""
    if not identifier or "`" in identifier or "\n" in identifier or len(
            identifier) > 64:
        raise ValueError(f"非法标识符：{identifier!r}")
    return f"`{identifier}`"


def qualified(schema: str, table: str, alias: str | None = None) -> str:
    base = f"{quote(schema)}.{quote(table)}"
    return f"{base} AS {quote(alias)}" if alias else base


class Whitelist:
    """从 M4 模型构建的标识符白名单与关系映射。"""

    def __init__(self, model: dict[str, Any]):
        self.entities = {e["entity_id"]: e for e in model.get("entities", [])}
        self.tables: dict[str, dict[str, Any]] = {}  # "schema.table" -> entity
        self.columns: dict[str, set[str]] = {}
        for entity in model.get("entities", []):
            source = entity.get("source") or {}
            key = f"{source.get('schema')}.{source.get('table')}"
            self.tables[key] = entity
            self.columns[key] = {
                a["field"]["column"].lower()
                for a in entity.get("attributes", [])}
        self.relationships = {
            r["relationship_key"]: r for r in model.get("relationships", [])}
        self.metrics = {m["metric_id"]: m for m in model.get("metrics", [])}

    def require_table(self, schema: str, table: str) -> str:
        key = f"{schema}.{table}".lower()
        if key not in self.columns:
            raise ValueError(f"表不在模型白名单：{schema}.{table}")
        return key

    def require_column(self, schema: str, table: str, column: str) -> None:
        key = self.require_table(schema, table)
        if column.lower() not in self.columns[key]:
            raise ValueError(f"字段不在模型白名单：{schema}.{table}.{column}")


class DatasetCompiler:
    """结构化定义 → 参数化 SQL + 血缘。"""

    def compile(self, spec: DatasetSpec, model: dict[str, Any]) -> CompiledPlan:
        whitelist = Whitelist(model)
        central = whitelist.entities.get(spec.central_entity_id)
        if central is None:
            raise ValueError(f"中心实体不在模型：{spec.central_entity_id}")
        central_source = central.get("source") or {}
        c_schema, c_table = central_source["schema"], central_source["table"]
        central_key = whitelist.require_table(c_schema, c_table)

        params: dict[str, Any] = {}
        lineage: dict[str, dict[str, Any]] = {}
        select_parts: list[str] = []
        used_aliases: set[str] = set()

        def _alias(name: str) -> str:
            candidate = name
            index = 1
            while candidate in used_aliases:
                index += 1
                candidate = f"{name}_{index}"
            used_aliases.add(candidate)
            return candidate

        def _param(value: Any) -> str:
            name = f"p{len(params)}"
            params[name] = value
            return f"%({name})s"

        # 中心 grain 键 + 字段
        for key in spec.grain_keys:
            whitelist.require_column(c_schema, c_table, key)
            alias = _alias(key)
            select_parts.append(f"c0.{quote(key)} AS {quote(alias)}")
            lineage[alias] = {"schema": c_schema, "table": c_table,
                              "column": key, "role": "grain_key"}
        for field_spec in spec.fields:
            ref = field_spec["field"]
            whitelist.require_column(ref["schema"], ref["table"], ref["column"])
            alias = _alias(field_spec.get("output_alias") or ref["column"])
            select_parts.append(f"c0.{quote(ref['column'])} AS {quote(alias)}")
            lineage[alias] = {
                **ref, "role": "field",
                "sensitive": self._is_sensitive(whitelist, ref)}

        from_clause = qualified(c_schema, c_table, "c0")
        joins: list[str] = []
        # 中心级过滤（不改变 LEFT JOIN 语义）
        central_conditions: list[str] = []
        dim_on_conditions: dict[str, list[str]] = {}
        dim_table_keys: dict[str, str] = {}

        # 维度 JOIN（many_to_one/one_to_one validated 关系）
        for index, dim in enumerate(spec.dimensions):
            ref = dim["field"]
            relation = whitelist.relationships.get(dim["relationship_key"])
            if relation is None:
                raise ValueError(
                    f"维度关系不在模型：{dim['relationship_key']}")
            cardinality = relation.get("cardinality")
            if cardinality not in ("many_to_one", "one_to_one"):
                raise ValueError(
                    f"维度关系必须为 many_to_one/one_to_one，"
                    f"实际 {cardinality}：{dim['relationship_key']}")
            # 方向：关系 source→target；中心必须为 source 侧（FK 侧），
            # 维度表为 target 侧（被引用侧）——沿安全方向取维度。
            if (relation["source"]["schema"] != c_schema
                    or relation["source"]["table"] != c_table):
                raise ValueError(
                    "维度关系方向必须为中心(源)→维度(目标)："
                    f"{dim['relationship_key']}")
            d_schema = relation["target"]["schema"]
            d_table = relation["target"]["table"]
            whitelist.require_column(ref["schema"], ref["table"], ref["column"])
            if (ref["schema"], ref["table"]) != (d_schema, d_table):
                raise ValueError(
                    f"维度字段必须属于关系目标表：{ref['table']}≠{d_table}")
            alias = f"d{index}"
            dim_table_keys[f"{d_schema}.{d_table}".lower()] = alias
            join_keyword = "LEFT JOIN" if dim.get("join_type", "left") == \
                "left" else "INNER JOIN"
            on_parts = [
                f"{alias}.{quote(tc)} = c0.{quote(sc)}"
                for sc, tc in zip(relation["source"]["columns"],
                                  relation["target"]["columns"], strict=True)]
            joins.append(
                f"{join_keyword} {qualified(d_schema, d_table, alias)} "
                f"ON {' AND '.join(on_parts)}")
            out_alias = _alias(dim.get("output_alias") or ref["column"])
            select_parts.append(f"{alias}.{quote(ref['column'])} "
                                f"AS {quote(out_alias)}")
            lineage[out_alias] = {
                **ref, "role": "dimension",
                "sensitive": self._is_sensitive(whitelist, ref)}

        # 预聚合 CTE（一跳明细：明细(源)→中心(目标) validated 关系）
        ctes: list[str] = []
        for index, pre in enumerate(spec.preaggregations):
            metric = whitelist.metrics.get(pre["metric_id"])
            if metric is None:
                raise ValueError(f"预聚合指标不在模型：{pre['metric_id']}")
            relation = whitelist.relationships.get(pre["relationship_key"])
            if relation is None:
                raise ValueError(
                    f"预聚合关系不在模型：{pre['relationship_key']}")
            if (relation["target"]["schema"] != c_schema
                    or relation["target"]["table"] != c_table):
                raise ValueError(
                    "预聚合关系目标必须是中心表："
                    f"{pre['relationship_key']}")
            detail_schema = relation["source"]["schema"]
            detail_table = relation["source"]["table"]
            detail_key = whitelist.require_table(detail_schema, detail_table)
            metric_entity = whitelist.entities.get(metric.get("entity_id"))
            if metric_entity is None or (
                    metric_entity.get("source") or {}).get("table") != detail_table:
                raise ValueError(
                    f"预聚合指标必须属于明细表实体：{pre['metric_id']}")
            group_cols = list(relation["source"]["columns"])
            metric_sql = self._metric_expression(metric, whitelist, "pa")
            cte_name = f"pa_{index}"
            select_cte = [f"pa.{quote(col)} AS {quote(col)}"
                          for col in group_cols]
            select_cte.append(f"{metric_sql} AS {quote(pre['output_alias'])}")
            ctes.append(
                f"{cte_name} AS (SELECT {', '.join(select_cte)} "
                f"FROM {qualified(detail_schema, detail_table, 'pa')} "
                f"GROUP BY {', '.join(f'pa.{quote(c)}' for c in group_cols)})")
            join_on = " AND ".join(
                f"{cte_name}.{quote(sc)} = c0.{quote(tc)}"
                for sc, tc in zip(group_cols, relation["target"]["columns"],
                                  strict=True))
            joins.append(f"LEFT JOIN {quote(cte_name)} ON {join_on}")
            out_alias = _alias(pre["output_alias"])
            select_parts.append(
                f"{cte_name}.{quote(pre['output_alias'])} AS {quote(out_alias)}")
            lineage[out_alias] = {
                "schema": detail_schema, "table": detail_table,
                "column": metric.get("measure_field", {}).get("column")
                if metric.get("measure_field") else None,
                "role": "preaggregated_metric",
                "metric_id": pre["metric_id"],
                "relationship_key": pre["relationship_key"],
                "scope_note": pre.get("scope_note"),
            }
            del detail_key

        # 中心表直接聚合指标（aggregate 类型）
        for metric_sel in spec.metrics:
            metric = whitelist.metrics.get(metric_sel["metric_id"])
            if metric is None:
                raise ValueError(f"指标不在模型：{metric_sel['metric_id']}")
            expr = self._metric_expression(metric, whitelist, "c0")
            alias = _alias(metric_sel.get("output_alias")
                           or metric_sel["metric_id"].rsplit(".", 1)[-1])
            select_parts.append(f"{expr} AS {quote(alias)}")
            lineage[alias] = {
                "schema": c_schema, "table": c_table,
                "column": (metric.get("measure_field") or {}).get("column"),
                "role": "metric", "metric_id": metric_sel["metric_id"],
            }

        # 类型化过滤：位置由字段所属对象决定——
        # 中心表列 → WHERE；维度表列 → 并入该维度的 ON 子句
        # （过滤写在 WHERE 会把 LEFT JOIN 隐式变 INNER，为受控负例）；
        # 明细/预聚合表列首版不支持过滤（拒绝，不隐式改计划）。
        for flt in spec.filters:
            ref = flt["field"]
            whitelist.require_column(ref["schema"], ref["table"],
                                     ref["column"])
            table_key = f"{ref['schema']}.{ref['table']}".lower()
            qualifier_alias = dim_table_keys.get(table_key)
            if qualifier_alias is None and table_key != central_key:
                raise ValueError(
                    f"过滤字段必须属于中心表或维度表：{ref['table']}"
                    f".{ref['column']}（明细表过滤首版不支持）")
            qualifier = f"{qualifier_alias or 'c0'}.{quote(ref['column'])}"
            predicate = self._predicate(
                qualifier, flt["operator"], flt["value"], _param)
            if qualifier_alias is not None:
                dim_on_conditions.setdefault(
                    qualifier_alias, []).append(predicate)
            else:
                central_conditions.append(predicate)
        # 维度列过滤并入 ON（保持 LEFT 行保留语义）
        if dim_on_conditions:
            for index, join in enumerate(list(joins)):
                alias = f"d{index}"
                if alias in dim_on_conditions and join.startswith("LEFT"):
                    extra = " AND ".join(dim_on_conditions[alias])
                    joins[index] = f"{join} AND ({extra})"

        where = f" WHERE {' AND '.join(central_conditions)}" \
            if central_conditions else ""
        with_clause = f"WITH {', '.join(ctes)}\n" if ctes else ""
        group_by = ""
        if spec.grain_keys:
            group_by = "\nGROUP BY " + ", ".join(
                f"c0.{quote(k)}" for k in spec.grain_keys)
        sql = (f"{with_clause}SELECT {', '.join(select_parts)}\n"
               f"FROM {from_clause}"
               + ("\n" + "\n".join(joins) if joins else "")
               + where + group_by)

        # 编译产物立即过 AST 白名单校验（编译器即校验器入口）
        validate_controlled_select(
            sql, set(whitelist.columns.keys()), whitelist.columns)

        plan = CompiledPlan(
            sql=sql, params=params, lineage=lineage,
            compiler_version=DATASET_COMPILER_VERSION)
        plan.plan_fingerprint = self.fingerprint(spec, plan)
        return plan

    @staticmethod
    def _is_sensitive(whitelist: "Whitelist", ref: dict) -> bool:
        """沿模型属性回溯 sensitive 标签（输出别名改写不能绕过）。"""
        key = f"{ref['schema']}.{ref['table']}".lower()
        entity = whitelist.tables.get(key) or {}
        for attribute in entity.get("attributes", []):
            if attribute["field"]["column"].lower() == (
                    ref["column"].lower()):
                return bool(attribute.get("sensitive"))
        return False

    def _metric_expression(
        self, metric: dict[str, Any], whitelist: Whitelist, alias: str
    ) -> str:
        aggregation = metric.get("aggregation")
        measure = metric.get("measure_field") or {}
        entity = whitelist.entities.get(metric.get("entity_id")) or {}
        source = entity.get("source") or {}
        if aggregation == "count_rows":
            return "COUNT(*)"
        if aggregation == "count_distinct":
            keys = metric.get("distinct_keys") or []
            if not keys:
                raise ValueError("count_distinct 缺少去重键")
            columns = ", ".join(f"{alias}.{quote(k['column'])}" for k in keys)
            # 组合键元组语义（多列 COUNT DISTINCT）
            return f"COUNT(DISTINCT {columns})"
        if not measure:
            raise ValueError(f"{aggregation} 缺少度量字段")
        whitelist.require_column(
            source.get("schema", ""), source.get("table", ""),
            measure["column"])
        column = f"{alias}.{quote(measure['column'])}"
        if aggregation in ("sum", "min", "max"):
            return f"{aggregation.upper()}({column})"
        if aggregation == "avg":
            return _AGGREGATION_SQL["avg"].format(col=column)
        raise ValueError(f"不支持的聚合：{aggregation}")

    @staticmethod
    def _predicate(
        qualifier: str, operator: str, value: Any, _param: Any,
    ) -> str:
        """构造参数化谓词（调用方已解析限定名与放置位置）。"""
        if operator == "is_null":
            return f"{qualifier} IS NULL"
        if operator == "not_null":
            return f"{qualifier} IS NOT NULL"
        if operator in ("in", "not_in"):
            if not isinstance(value, list) or not value:
                raise ValueError(f"{operator} 过滤需要非空列表值")
            placeholders = ", ".join(_param(v) for v in value)
            return (f"{qualifier} {'IN' if operator == 'in' else 'NOT IN'} "
                    f"({placeholders})")
        sql_operator = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=",
                        "lt": "<", "le": "<="}.get(operator)
        if sql_operator is None:
            raise ValueError(f"不支持的过滤操作符：{operator}")
        return f"{qualifier} {sql_operator} {_param(value)}"

    @staticmethod
    def fingerprint(spec: DatasetSpec, plan: CompiledPlan) -> str:
        payload = {
            "definition": {
                "kind": spec.kind,
                "central_entity_id": spec.central_entity_id,
                "grain_keys": list(spec.grain_keys),
                "fields": spec.fields,
                "dimensions": spec.dimensions,
                "metrics": spec.metrics,
                "preaggregations": spec.preaggregations,
                "filters": spec.filters,
                "join_type": spec.join_type,
                "model_id": spec.model_id,
                "model_version": spec.model_version,
                "topic_id": spec.topic_id,
            },
            "sql": plan.sql,
            "params_keys": sorted(plan.params.keys()),
            "compiler": plan.compiler_version,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       default=str).encode()).hexdigest()[:32]


def definition_fingerprint(spec: DatasetSpec) -> str:
    """定义指纹（不含编译产物；定义变化即失效旧 SQL/质量报告）。"""
    payload = {
        "kind": spec.kind, "central_entity_id": spec.central_entity_id,
        "grain_keys": list(spec.grain_keys), "fields": spec.fields,
        "dimensions": spec.dimensions, "metrics": spec.metrics,
        "preaggregations": spec.preaggregations, "filters": spec.filters,
        "join_type": spec.join_type, "model_id": spec.model_id,
        "model_version": spec.model_version, "topic_id": spec.topic_id,
        "target": spec.target,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode()).hexdigest()[:32]


def field_ref(ref: dict[str, Any]) -> FieldRef:
    return FieldRef(ref["schema"], ref["table"], ref["column"])
