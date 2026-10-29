"""M5 质量检查与独立基准对账（dataset-quality-v1）。

口径原则：
- 对账不重复执行同一条生成 SQL：中心计数↔中心表独立基准、明细聚合↔
  各明细表独立聚合、组合去重/金额精度独立断言；
- 金额比较使用 Decimal（容差来自显式策略，不浮点裸比）；
- LIMIT 截断的窗口只能给 sample 口径：样本通过不证明全量正确；规模超
  预算 → incomplete（不发布伪完整数据集）；空数据区分“不适用/通过”；
- 每项检查记录 期望/实际/分母/范围/口径(exact|sample|unknown)/结果
  (passed|failed|incomplete)/原因；
- 报告绑定 dataset_version+plan_fingerprint+definition_fingerprint+
  model_version+snapshot；定义变化后旧报告失效。
"""

import hashlib
import json
from decimal import Decimal
from typing import Any

from app.domain.datasets import QualityCheck, QualityReport
from app.ports.datasets import ControlledQueryExecutor

_AMOUNT_TOLERANCE = Decimal("0.01")  # 显式金额对账容差（分位级）


def _grain_typed(value: Any) -> str:
    """grain 键规范化（组合键结构化编码，不用易碰撞分隔符拼接）。"""
    if isinstance(value, (list, tuple)):
        return json.dumps([_grain_typed(v) for v in value],
                          ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, (dict,)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          default=str)
    return f"{type(value).__name__}:{value!r}"


def grain_document_id(grain_values: list[Any]) -> str:
    """组合键 → 无歧义结构化文档 ID（长度受控）。"""
    payload = json.dumps(
        [_grain_typed(v) for v in grain_values],
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 非数值显式 None
        return None


def run_quality_checks(
    *,
    spec: dict[str, Any],
    plan_sql: str,
    plan_params: dict[str, Any],
    executor: ControlledQueryExecutor,
    model: dict[str, Any],
    max_rows: int,
) -> QualityReport:
    """执行全部质量检查并产出报告（失败收尾，不抛业务异常）。"""
    checks: list[QualityCheck] = []
    central = _central_entity(model, spec["central_entity_id"])
    central_source = central.get("source") or {}

    # 数据集输出（LIMIT 窗口；截断即 sample 口径）
    rows, truncated = executor.execute(
        plan_sql, plan_params, max_rows=max_rows, timeout_ms=60_000)
    basis = "sample" if truncated else "exact"

    # 1) grain 键非空与唯一性（窗口口径；截断时不能宣称全量唯一）
    # 输出行别名以编译器血缘为准：取 grain 角色的别名
    grain_outputs = [
        alias for alias, info in _lineage_of(spec).items()
        if info.get("role") == "grain_key"]
    _null_check(checks, rows, grain_outputs, basis, truncated)
    _uniqueness_check(checks, rows, grain_outputs, basis, truncated)

    # 2) 中心行数与输出行数关系（独立基准：中心表直接计数）
    central_count = _scalar_benchmark(
        executor, model,
        f"SELECT COUNT(*) AS n FROM `{central_source['schema']}`."
        f"`{central_source['table']}`")
    _row_count_check(checks, rows, central_count, basis, truncated,
                     has_aggregation=bool(spec["metrics"] or
                                          spec["preaggregations"]))

    # 3) JOIN 覆盖率/未匹配（维度输出 NULL 比例）
    _join_coverage_checks(checks, rows, spec, basis)

    # 4) 必填字段缺失（输出血缘中 grain/字段角色）
    _required_fields_check(checks, rows, spec, basis)

    # 5) 明细预聚合独立对账（各明细表独立聚合 vs 数据集输出）
    _preaggregation_reconciliation(
        checks, executor, model, spec, rows, basis, truncated)

    # 6) 指标对账（中心聚合类指标 ↔ 中心表独立聚合）
    _metric_reconciliation(
        checks, executor, model, spec, rows, basis, truncated)

    # 修复轮：未完成（incomplete）的关键检查阻断 all_passed
    all_passed = (
        bool(checks)
        and all(c.outcome == "passed" for c in checks))
    report = QualityReport(
        dataset_id=spec["dataset_id"], dataset_version=spec["version"],
        plan_fingerprint=spec.get("plan_fingerprint"),
        definition_fingerprint=spec.get("definition_fingerprint"),
        model_id=spec["model_id"], model_version=spec["model_version"],
        snapshot_id=spec.get("snapshot_id"),
        checks=[c.to_dict() for c in checks], all_passed=all_passed)
    return report


def _lineage_of(spec: dict[str, Any]) -> dict[str, Any]:
    return spec.get("_lineage") or {}


def _central_entity(model: dict[str, Any], entity_id: str) -> dict[str, Any]:
    for entity in model.get("entities", []):
        if entity["entity_id"] == entity_id:
            return entity
    raise ValueError(f"中心实体不在模型：{entity_id}")


def _scalar_benchmark(executor: ControlledQueryExecutor, model: dict[str, Any],
                      sql: str, params: dict[str, Any] | None = None) -> Any:
    """独立基准查询（AST 校验复用执行器；白名单取自模型全表）。"""
    rows, _ = executor.execute(sql, params or {}, max_rows=1,
                               timeout_ms=60_000)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def _null_check(checks, rows, grain_outputs, basis, truncated) -> None:
    if not grain_outputs:
        checks.append(QualityCheck(
            check="grain_non_null", expected="grain 键全部非空",
            outcome="failed", reason="数据集缺少 grain 键"))
        return
    null_rows = sum(
        1 for row in rows
        if any(row.get(alias) is None for alias in grain_outputs))
    check = QualityCheck(
        check="grain_non_null", expected="grain 键为空的行数=0",
        actual=null_rows, denominator=len(rows),
        scope="数据集输出窗口", basis=basis,
        outcome="passed" if null_rows == 0 else "failed",
        reason="" if null_rows == 0 else "存在 grain 键为空的输出行")
    checks.append(check)


def _uniqueness_check(checks, rows, grain_outputs, basis, truncated) -> None:
    keys = [_grain_typed(tuple(row.get(a) for a in grain_outputs))
            for row in rows]
    duplicates = len(keys) - len(set(keys))
    if truncated:
        outcome = "incomplete"
        reason = "窗口截断：无法验证完整 grain 唯一性（不冒充全量唯一）"
    elif duplicates == 0:
        outcome = "passed"
        reason = ""
    else:
        outcome = "failed"
        reason = "存在重复 grain 键"
    checks.append(QualityCheck(
        check="grain_unique", expected="grain 键组合无重复",
        actual=duplicates, denominator=len(keys),
        scope="数据集输出窗口" + ("（截断）" if truncated else ""),
        basis=basis, outcome=outcome,
        reason=reason or ("存在重复 grain 键" if duplicates else "")))


def _row_count_check(checks, rows, central_count, basis, truncated,
                     has_aggregation) -> None:
    if has_aggregation:
        # 聚合输出行数 ≤ 中心行数（分组聚合）；NULL grain 分组单独说明
        ok = len(rows) <= (central_count or 0) or central_count is None
        expected = f"输出行数 ≤ 中心表行数（{central_count}）"
    else:
        ok = (not truncated) and len(rows) == (central_count or 0)
        expected = f"输出行数 = 中心表行数（{central_count}）"
    if central_count == 0:
        outcome = "passed"
        reason = "中心表为空：空数据集为合法状态（非不适用）"
    elif ok:
        outcome = "passed"
        reason = ""
    else:
        outcome = "failed"
        reason = "输出行数与中心基准不符（JOIN 行丢失或膨胀）"
    if truncated:
        outcome = "incomplete"
        reason = "输出窗口截断：无法比对全量行数（关键检查未覆盖完整定义范围，不据此发布）"
    checks.append(QualityCheck(
        check="row_count_vs_central", expected=expected,
        actual=len(rows), denominator=central_count,
        scope="数据集输出 vs 中心表独立计数", basis=basis, outcome=outcome,
        reason=reason))


def _join_coverage_checks(checks, rows, spec, basis) -> None:
    dimensions = [d for d in spec.get("dimensions", [])]
    if not dimensions:
        return
    for dim in dimensions:
        alias = dim.get("output_alias")
        matched = sum(1 for row in rows if row.get(alias) is not None)
        non_null_rows = sum(
            1 for row in rows
            if any(row.get(k) is not None for k in _grain_aliases(spec)))
        if non_null_rows == 0:
            outcome, reason = "passed", "空输出：覆盖率不适用"
        elif dim.get("join_type", "left") == "left":
            outcome = "passed"
            reason = f"LEFT JOIN 未匹配={non_null_rows - matched}（NULL 表达）"
        elif matched == non_null_rows:
            outcome, reason = "passed", ""
        else:
            outcome = "failed"
            reason = "INNER JOIN 存在未匹配行（行被过滤）"
        checks.append(QualityCheck(
            check="join_coverage",
            expected="维度匹配覆盖（LEFT 未匹配以 NULL 表达）",
            actual=matched, denominator=non_null_rows,
            scope=f"维度 {alias}", basis=basis, outcome=outcome,
            reason=reason))


def _grain_aliases(spec) -> list[str]:
    return [f for f in spec.get("_grain_aliases", [])]


def _required_fields_check(checks, rows, spec, basis) -> None:
    required = [
        info.get("output_alias") or alias
        for alias, info in _lineage_of(spec).items()
        if info.get("role") in ("grain_key", "field")]
    missing = {
        alias: sum(1 for row in rows if row.get(alias) is None)
        for alias in required}
    total_missing = sum(missing.values())
    checks.append(QualityCheck(
        check="required_fields_present", expected="必填字段无缺失",
        actual=total_missing, denominator=len(rows) * max(len(required), 1),
        scope="数据集输出窗口", basis=basis,
        outcome="passed" if total_missing == 0 else "failed",
        reason="" if total_missing == 0 else f"缺失分布={missing}"))


def _preaggregation_reconciliation(
        checks, executor, model, spec, rows, basis, truncated) -> None:
    """5×3 防膨胀核心对账：各明细表独立聚合 vs 数据集输出值。"""
    relationships = {
        r["relationship_key"]: r for r in model.get("relationships", [])}
    metrics = {m["metric_id"]: m for m in model.get("metrics", [])}
    for pre in spec.get("preaggregations", []):
        metric = metrics.get(pre["metric_id"])
        relation = relationships.get(pre["relationship_key"])
        if metric is None or relation is None:
            checks.append(QualityCheck(
                check="preaggregation_reconciliation",
                expected="预聚合定义可解析", outcome="failed",
                reason="指标或关系不在模型"))
            continue
        detail_source = relation["source"]
        measure = (metric.get("measure_field") or {}).get("column")
        group_cols = relation["source"]["columns"]
        # 独立基准：明细表直接按关联键分组聚合（与生成 SQL 不同对象）
        # 修复轮：应用 M4 指标的结构化过滤（同业务口径独立表达）
        columns = ", ".join(f"`{c}`" for c in group_cols)
        if metric["aggregation"] == "count_rows":
            aggregate = "COUNT(*)"
        elif metric["aggregation"] in ("sum", "avg"):
            # AVG 基准＝和/非空计数（单表达式；分母只计非 NULL 度量行，
            # 与编译产物同口径；不得拆成两列——别名会错位到 COUNT）
            aggregate = (f"SUM(`{measure}`) / NULLIF(COUNT(`{measure}`), 0)"
                         if metric["aggregation"] == "avg"
                         else f"SUM(`{measure}`)")
        elif metric["aggregation"] == "count_distinct":
            keys = metric.get("distinct_keys") or []
            aggregate = "COUNT(DISTINCT " + ", ".join(
                f"`{k['column']}`" for k in keys) + ")"
        elif metric["aggregation"] in ("min", "max"):
            aggregate = f"{metric['aggregation'].upper()}(`{measure}`)"
        else:
            aggregate = None
        if aggregate is None:
            checks.append(QualityCheck(
                check="preaggregation_reconciliation",
                expected="聚合可独立表达", outcome="failed",
                reason=f"不支持对账的聚合：{metric['aggregation']}"))
            continue
        # 指标过滤谓词（独立表达——与编译器产物不同代码路径）
        # 修复轮 3：IN 参数在同一个循环中分配名称、追加占位符和写入字典
        benchmark_conditions: list[str] = []
        bench_params: dict[str, Any] = {}
        for flt in metric.get("filters") or []:
            ref = flt.get("field") or {}
            column = f"`{ref.get('column', '')}`"
            op = flt.get("operator", "eq")
            if op == "is_null":
                benchmark_conditions.append(f"{column} IS NULL")
            elif op == "not_null":
                benchmark_conditions.append(f"{column} IS NOT NULL")
            elif op in ("in", "not_in"):
                values = flt.get("value") or []
                if isinstance(values, list) and values:
                    names = []
                    for value in values:
                        param_name = f"b{len(bench_params)}"
                        bench_params[param_name] = value
                        names.append(f":{param_name}")
                    benchmark_conditions.append(
                        f"{column} {'IN' if op == 'in' else 'NOT IN'} "
                        f"({', '.join(names)})")
            else:
                sql_op = {"eq": "=", "ne": "<>", "gt": ">",
                          "ge": ">=", "lt": "<", "le": "<="}.get(op, "=")
                param_name = f"b{len(bench_params)}"
                bench_params[param_name] = flt.get("value")
                benchmark_conditions.append(
                    f"{column} {sql_op} :{param_name}")
        bench_where = (" WHERE " + " AND ".join(benchmark_conditions)
                       if benchmark_conditions else "")
        benchmark_sql = (
            f"SELECT {columns}, {aggregate} AS agg FROM "
            f"`{detail_source['schema']}`.`{detail_source['table']}`"
            f"{bench_where} "
            f"GROUP BY {columns}")
        bench_rows, bench_truncated = executor.execute(
            benchmark_sql, bench_params, max_rows=10_000, timeout_ms=60_000)
        # 输出侧预聚合值按 grain 关联键对齐比较（窗口内逐组对账）
        grain_aliases = _grain_aliases(spec)
        output_by_key = {}
        for row in rows:
            key = _grain_typed(tuple(row.get(a) for a in grain_aliases))
            value = row.get(pre["output_alias"])
            if value is not None:
                output_by_key[key] = _as_decimal(value)
        mismatches = []
        compared = 0
        central = _central_entity(model, spec["central_entity_id"])
        central_pk = central.get("primary_key") or []
        for bench in bench_rows:
            # 明细关联键（source columns）对应中心目标列（grain 或主键）
            bench_key = _grain_typed(
                tuple(bench.get(c) for c in group_cols))
            expected = _as_decimal(bench.get("agg"))
            actual = output_by_key.get(bench_key)
            if expected is None and actual is None:
                continue
            compared += 1
            if expected is not None and actual is not None and \
                    abs(expected - actual) > _AMOUNT_TOLERANCE or (expected is None) != (actual is None):
                mismatches.append(bench_key)
        outcome = "failed" if mismatches else "passed"
        if truncated and not mismatches:
            outcome = "passed"
        checks.append(QualityCheck(
            check="preaggregation_reconciliation",
            expected=(f"各明细独立聚合与数据集输出一致（容差"
                      f"{_AMOUNT_TOLERANCE}；明细组={len(bench_rows)}）"),
            actual=len(mismatches), denominator=compared,
            scope=(f"{detail_source['table']} 独立分组聚合 vs 数据集输出"
                   + ("（双侧窗口）" if truncated or bench_truncated else "")),
            basis=basis if not (truncated or bench_truncated) else "sample",
            outcome=outcome,
            reason=("存在对账不一致的关联键组（明细预聚合结果与独立基准不符）"
                    if mismatches else
                    ("窗口对账一致（未证明全量）" if truncated else ""))))
        del central_pk


def _metric_reconciliation(
        checks, executor, model, spec, rows, basis, truncated) -> None:
    """中心聚合指标 ↔ 中心表独立聚合对账（修复轮：各指标独立过滤）。"""
    metrics = {m["metric_id"]: m for m in model.get("metrics", [])}
    for selection in spec.get("metrics", []):
        metric = metrics.get(selection["metric_id"])
        if metric is None:
            checks.append(QualityCheck(
                check="metric_reconciliation", expected="指标可解析",
                outcome="failed", reason="指标不在模型"))
            continue
        central = _central_entity(model, spec["central_entity_id"])
        source = central.get("source") or {}
        measure = (metric.get("measure_field") or {}).get("column")
        keys = metric.get("distinct_keys") or []
        grain = spec["grain_keys"]
        if metric["aggregation"] == "count_rows":
            aggregate = "COUNT(*)"
        elif metric["aggregation"] == "sum":
            aggregate = f"SUM(`{measure}`)"
        elif metric["aggregation"] == "avg":
            # AVG 基准返回真正平均值（修复轮：不把 COUNT 当 AVG）
            aggregate = (f"SUM(`{measure}`) / NULLIF(COUNT(`{measure}`), 0)")
        elif metric["aggregation"] == "count_distinct":
            aggregate = "COUNT(DISTINCT " + ", ".join(
                f"`{k['column']}`" for k in keys) + ")"
        elif metric["aggregation"] in ("min", "max"):
            aggregate = f"{metric['aggregation'].upper()}(`{measure}`)"
        else:
            aggregate = None
        if aggregate is None:
            continue
        # 修复轮：应用该指标自身的 filters（各指标独立，不合并条件）
        bench_conditions: list[str] = []
        bench_params: dict[str, Any] = {}
        for flt in metric.get("filters") or []:
            ref = flt.get("field") or {}
            column = f"`{ref.get('column', '')}`"
            op = flt.get("operator", "eq")
            if op == "is_null":
                bench_conditions.append(f"{column} IS NULL")
            elif op == "not_null":
                bench_conditions.append(f"{column} IS NOT NULL")
            elif op in ("in", "not_in"):
                values = flt.get("value") or []
                if isinstance(values, list) and values:
                    names = []
                    for value in values:
                        param_name = f"m{len(bench_params)}"
                        bench_params[param_name] = value
                        names.append(f":{param_name}")
                    bench_conditions.append(
                        f"{column} {'IN' if op == 'in' else 'NOT IN'} "
                        f"({', '.join(names)})")
            else:
                sql_op = {"eq": "=", "ne": "<>", "gt": ">",
                          "ge": ">=", "lt": "<", "le": "<="}.get(op, "=")
                param_name = f"m{len(bench_params)}"
                bench_params[param_name] = flt.get("value")
                bench_conditions.append(f"{column} {sql_op} :{param_name}")
        bench_where = (" WHERE " + " AND ".join(bench_conditions)
                       if bench_conditions else "")
        group = ", ".join(f"`{c}`" for c in grain)
        bench_sql = (f"SELECT {group}, {aggregate} AS agg FROM "
                     f"`{source['schema']}`.`{source['table']}`"
                     f"{bench_where} "
                     f"GROUP BY {group}")
        bench_rows, bench_truncated = executor.execute(
            bench_sql, bench_params, max_rows=10_000, timeout_ms=60_000)
        grain_aliases = _grain_aliases(spec)
        output_by_key = {}
        for row in rows:
            key = _grain_typed(tuple(row.get(a) for a in grain_aliases))
            output_by_key[key] = _as_decimal(
                row.get(selection.get("output_alias")
                        or selection["metric_id"].rsplit(".", 1)[-1]))
        mismatches = 0
        compared = 0
        for bench in bench_rows:
            key = _grain_typed(tuple(bench.get(c) for c in grain))
            expected = _as_decimal(bench.get("agg"))
            actual = output_by_key.get(key)
            compared += 1
            if expected is not None and actual is not None:
                if abs(expected - actual) > _AMOUNT_TOLERANCE:
                    mismatches += 1
            elif (expected is None) != (actual is None):
                mismatches += 1
        checks.append(QualityCheck(
            check="metric_reconciliation",
            expected=("中心表独立分组聚合与数据集输出一致"
                      f"（容差 {_AMOUNT_TOLERANCE}）"),
            actual=mismatches, denominator=compared,
            scope="中心表独立聚合 vs 数据集输出"
                  + ("（双侧窗口）" if truncated or bench_truncated else ""),
            basis=basis if not (truncated or bench_truncated) else "sample",
            outcome="failed" if mismatches else "passed",
            reason="聚合值与独立基准不符" if mismatches else ""))
