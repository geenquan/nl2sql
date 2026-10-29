"""Group 2 回归：MySQL 参数绑定（真实编译器产物→执行器路径）。"""
import pytest

from app.adapters.dataset_executors import MySQLControlledExecutor
from app.datasets.compiler import DatasetCompiler
from app.domain.datasets import DatasetSpec
from tests.dataset_fixtures import seed_environment


@pytest.fixture()
def env(tmp_path):
    return seed_environment(tmp_path)



class _FakeDBAPIConnection:
    """捕获最终 SQL 与参数的 DBAPI 替身。"""
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
    def cursor(self):
        return _FakeCursor(self)
    def close(self):
        pass


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = [("id",)]
    def execute(self, sql, params=None):
        self._conn.executed.append((sql, tuple(params or ())))
    def fetchmany(self, n):
        return [(1,)] * min(n, 1)
    def close(self):
        pass


class TestMySQLParamBinding:
    def _compiled_plan_with_filters(self, env):
        """经真实编译器取含过滤参数的产物。"""
        from tests.dataset_fixtures import detail_view_definition
        definition = detail_view_definition(env["model"])
        definition["filters"] = [{
            "field": {"schema": "app", "table": "booking",
                      "column": "id"},
            "operator": "le", "value": 3}]
        model = env["model_service"].get_model(
            definition["model_id"], version=definition["model_version"])
        spec = DatasetSpec(
            dataset_id="t", name="t", source_id=env["source_id"],
            snapshot_id=definition.get("snapshot_id"),
            model_id=definition["model_id"],
            model_version=definition["model_version"],
            topic_id=definition["topic_id"],
            kind=definition["kind"],
            central_entity_id=definition["central_entity_id"])
        for key in ("grain_keys", "fields", "dimensions", "metrics",
                    "preaggregations", "filters", "join_type", "target"):
            setattr(spec, key, definition.get(key, []))
        return DatasetCompiler().compile(spec, model)

    def test_mysql_executor_adapts_params(self, env):
        """MySQL 执行器将 pyformat %(pN)s 转为 :pN 命名绑定。"""
        plan = self._compiled_plan_with_filters(env)
        # 编译产物含 pyformat 占位符
        assert "%(p0)s" in plan.sql
        assert plan.params["p0"] == 3

        executor = MySQLControlledExecutor(
            host="unused", port=3306, database="unused",
            username="u", password="x",
            allowed_tables={"app.booking": {"id", "booking_no"}},
            allowed_columns={"app.booking": {"id", "booking_no"}})
        # 验证 _adapt_params 转换（MySQL 不再跳过）
        adapted = executor._adapt_params(plan.sql)
        assert ":p0" in adapted, (
            f"MySQL 未转换 pyformat：{adapted}")
        assert "%(p0)s" not in adapted
        executor.close()

    def test_sqlalchemy_compiles_named_params(self, env):
        """转换后的 SQL 经 SQLAlchemy MySQL 方言编译产生绑定参数。"""
        from sqlalchemy import text
        from sqlalchemy.dialects import mysql

        plan = self._compiled_plan_with_filters(env)
        executor = MySQLControlledExecutor(
            host="u", port=3306, database="u", username="u",
            password="x", allowed_tables={"app.booking": {"id"}},
            allowed_columns={"app.booking": {"id"}})
        adapted = executor._adapt_params(plan.sql)
        executor.close()
        stmt = text(adapted)
        compiled = stmt.compile(dialect=mysql.dialect())
        # 命名绑定参数存在于编译结果
        assert "p0" in compiled.params, (
            f"参数绑定未生效：{compiled.params}")
        # 值不内联到 SQL
        assert "3" not in str(compiled) or ":p0" in str(compiled)

    def test_param_types(self, env):
        """字符串/数字/NULL/IN 列表参数适配。"""
        executor = MySQLControlledExecutor(
            host="u", port=3306, database="u", username="u",
            password="x", allowed_tables={"app.booking": {"id"}},
            allowed_columns={"app.booking": {"id"}})
        adapted = executor._adapt_params(
            "SELECT `id` FROM `app`.`booking` WHERE `id` = %(p_str)s "
            "AND `id` >= %(p_num)s AND `id` IS %(p_null)s "
            "AND `id` IN %(p_in)s")
        # 全部转换为命名绑定
        for name in ("p_str", "p_num", "p_null", "p_in"):
            assert f":{name}" in adapted, f"参数 {name} 未转换"
        executor.close()
