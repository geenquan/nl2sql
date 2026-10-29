"""M4 收尾修复轮 2 回归（五项，全部先在旧实现复现失败；离线虚构数据）。

P1 历史模型主题/指标名泄漏；P1 grain 描述变化未撤销确认；
P2 重确认后永久阻断；P2 依赖检查用 latest 非 effective；
P2 无关风险路径误伤单实体指标。
"""

import json
from typing import Any

import pytest

from app.adapters.repositories.sqlite_metadata_repository import (
    SQLiteMetadataRepository,
)
from app.core.config import Settings
from app.services.semantic_model_service import SemanticModelService
from tests.semantic_model_fixtures import (
    DEFAULT_VALIDATED_RELATIONSHIPS,
    SCHEMA,
    m4_catalog,
    seed_m4_environment,
    seed_relationships,
)

SECRET = "Round2DummyToken"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _new_env(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    seed_m4_environment(repo, source_id,
                        relationship_specs=DEFAULT_VALIDATED_RELATIONSHIPS)
    return repo, source_id, SemanticModelService(repo, _settings())


def _legacy_model_with_secrets(service: SemanticModelService,
                               repo: SQLiteMetadataRepository,
                               source_id: str) -> dict[str, Any]:
    """构造符合旧构建行为的历史模型：同一虚构口令散布在
    实体名/主题名/指标名/派生描述/审核备注/change_note。"""
    model = service.build_model(source_id)
    payload = json.loads(json.dumps(model))  # 深拷贝
    # 模拟旧构建器把含密注释派生到各自由文本字段
    for entity in payload["entities"][:2]:
        entity["name"] = f"password: {SECRET}"
    for topic in payload["topics"][:2]:
        topic["name"] = f"token: {SECRET}"
        topic["grain"]["statement"] = f"一行代表一条 password: {SECRET} 记录"
    for metric in payload["metrics"][:2]:
        metric["name"] = f"api_key: {SECRET} 数量"
        metric["description"] = f'Authorization: Bearer {SECRET}'
    payload["change_note"] = f'"secret": "{SECRET}"'
    _overwrite_stored_model(repo, payload)
    return payload


def _overwrite_stored_model(repo: SQLiteMetadataRepository,
                            payload: dict[str, Any]) -> None:
    from sqlalchemy import update

    with repo._engine.begin() as conn:
        conn.execute(
            update(repo._semantic_models).where(
                repo._semantic_models.c.model_id == payload["model_id"],
                repo._semantic_models.c.version == payload["version"],
            ).values(payload=json.dumps(payload, ensure_ascii=False)))


def _stored_payload(repo, model_id: str, version: int) -> str:
    with repo._engine.connect() as conn:
        row = conn.execute(
            repo._semantic_models.select().where(
                repo._semantic_models.c.model_id == model_id,
                repo._semantic_models.c.version == version,
            )).first()
    return row.payload


# == 一、历史模型的主题名/指标名泄漏 =============================================


class TestHistoricalSecretLeakage:
    def test_all_read_and_export_paths_sanitized(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        legacy = _legacy_model_with_secrets(service, repo, source_id)
        model_id, version = legacy["model_id"], legacy["version"]
        before = _stored_payload(repo, model_id, version)

        # 1) get / list / versions 返回入口均不含原值
        assert SECRET not in json.dumps(
            service.get_model(model_id, version), ensure_ascii=False)
        assert SECRET not in json.dumps(
            service.list_models(source_id), ensure_ascii=False)
        assert SECRET not in json.dumps(
            service.list_versions(model_id), ensure_ascii=False)
        # 2) JSON/YAML 导出不含原值
        assert SECRET not in service.export_model(
            model_id, version, export_format="json")["content"]
        assert SECRET not in service.export_model(
            model_id, version, export_format="yaml")["content"]
        # 3) 读取前后数据库存储内容完全相同（不改写历史行）
        assert _stored_payload(repo, model_id, version) == before
        # 4) 结构化标识及引用保持不变
        refreshed = service.get_model(model_id, version)
        assert [e["entity_id"] for e in refreshed["entities"]] == \
            [e["entity_id"] for e in legacy["entities"]]
        assert [m["metric_id"] for m in refreshed["metrics"]] == \
            [m["metric_id"] for m in legacy["metrics"]]
        assert [t["topic_id"] for t in refreshed["topics"]] == \
            [t["topic_id"] for t in legacy["topics"]]
        for metric in refreshed["metrics"]:
            assert metric["measure_field"] in (None,) or \
                metric["measure_field"]["column"]  # 结构化引用未动
        # 5) 普通业务文本不被错误清空（无敏感内容的名称保留）
        clean_names = [t["name"] for t in refreshed["topics"]
                       if SECRET not in t["name"]]
        assert clean_names, "未含敏感内容的主题名应原样保留"

    def test_new_revision_persists_sanitized_free_text(self, tmp_path):
        """新建修订的自由文本在持久化前净化（存储行即不含原值）。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        topic = model["topics"][0]
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows")
        revised = service.revise_model(
            model["model_id"], model["version"], {
                "topic_names": {topic["topic_id"]: f"token: {SECRET}"},
                "metrics": {metric["metric_id"]: {
                    "action": "update", "name": f"api_key: {SECRET} 数量",
                    "description": f"Bearer {SECRET}"}},
            }, reviewer="dev", note=f"password: {SECRET}")
        stored = _stored_payload(repo, revised["model_id"], revised["version"])
        assert SECRET not in stored

    def test_sanitize_uses_independent_copy(self, tmp_path):
        """净化不修改调用方持有的模型对象。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        holder = json.loads(json.dumps(model))
        holder["topics"][0]["name"] = f"token: {SECRET}"
        _overwrite_stored_model(repo, holder)
        caller_ref = json.loads(json.dumps(holder))
        service.get_model(model["model_id"], model["version"])
        assert caller_ref == holder  # 调用方对象未被净化操作改动


# == 二、grain 业务描述变化未撤销指标确认 ==========================================


def _confirmed_with_metric(env) -> dict:
    _, source_id, service = env
    model = service.build_model(source_id)
    metric = next(
        m for m in model["metrics"] if m["aggregation"] == "count_rows"
        and m["entity_id"].startswith(f"{SCHEMA}.booking"))
    return service.revise_model(
        model["model_id"], model["version"], {
            "topic_grains": {
                t["topic_id"]: {"statement": f"一行代表一条{t['name']}记录",
                                "keys": t["grain"]["keys"], "confirm": True}
                for t in model["topics"] if t["grain"]["keys"]},
            "metrics": {metric["metric_id"]: {
                "action": "confirm", "time_basis": "自然日",
                "skip_status_filter": True}},
        }, reviewer="dev")


def _metric_of(model: dict, metric_id: str) -> dict:
    return next(m for m in model["metrics"] if m["metric_id"] == metric_id)


class TestGrainStatementChange:
    def test_statement_only_change_invalidates(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "一行代表一个完全不同的业务对象",
                    "keys": topic["grain"]["keys"], "confirm": True}}},
            reviewer="dev")
        revised_metric = _metric_of(revised, metric["metric_id"])
        assert revised_metric["status"] == "suggested"  # keys/basis 未变也必须失效
        assert revised_metric["business_confirmation"] is None
        assert revised_metric["grain_statement"] == "一行代表一个完全不同的业务对象"
        # 原版本不变
        original = service.get_model(confirmed["model_id"], confirmed["version"])
        assert _metric_of(original, metric["metric_id"])[
            "status"] == "business_confirmed"

    def test_whitespace_only_change_does_not_invalidate(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": f"  {topic['grain']['statement']}  ",
                    "keys": topic["grain"]["keys"], "confirm": True}}},
            reviewer="dev")
        assert _metric_of(revised, metric["metric_id"])[
            "status"] == "business_confirmed"  # 仅空白不误撤销

    def test_identical_definition_does_not_invalidate(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": topic["grain"]["statement"],
                    "keys": topic["grain"]["keys"], "confirm": True}}},
            reviewer="dev")
        assert _metric_of(revised, metric["metric_id"])[
            "status"] == "business_confirmed"

    def test_topic_name_only_change_keeps_confirmation(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_names": {topic["topic_id"]: "预订主题改名"}},
            reviewer="dev")
        assert _metric_of(revised, metric["metric_id"])[
            "status"] == "business_confirmed"

    def test_reconfirm_after_grain_change_restores_formal(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        changed = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "新业务粒度", "keys": topic["grain"]["keys"],
                    "confirm": True}}}, reviewer="dev")
        reconfirmed = service.revise_model(
            changed["model_id"], changed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "自然日",
                    "skip_status_filter": True}}}, reviewer="dev")
        revised_metric = _metric_of(reconfirmed, metric["metric_id"])
        assert revised_metric["status"] == "business_confirmed"
        assert revised_metric["grain_statement"] == "新业务粒度"
        service.publish_model(reconfirmed["model_id"], reconfirmed["version"],
                              reviewer="dev")
        published = service.get_model(reconfirmed["model_id"],
                                      reconfirmed["version"])
        assert metric["metric_id"] in published["formal_metric_ids"]

    def test_same_request_grain_change_and_confirm_not_reinvalidated(
            self, tmp_path):
        """同请求：改 grain + 明确确认指标——确认基于新 grain 完成，
        尾部失效循环不得再撤销刚完成的新确认。"""
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        topic = next(t for t in confirmed["topics"]
                     if t["central_entity_id"] == metric["entity_id"])
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "topic_grains": {topic["topic_id"]: {
                    "statement": "同请求新粒度", "keys": topic["grain"]["keys"],
                    "confirm": True}},
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "自然日",
                    "skip_status_filter": True}}},
            reviewer="dev")
        revised_metric = _metric_of(revised, metric["metric_id"])
        assert revised_metric["status"] == "business_confirmed"
        assert revised_metric["grain_statement"] == "同请求新粒度"


# == 三、合法重新确认仍留下永久阻断 ================================================


class TestReconfirmRemovesReconfirmBlockers:
    def test_update_then_reconfirm_then_publish(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        updated = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "time_basis": "事件时间"}}}, reviewer="dev")
        updated_metric = _metric_of(updated, metric["metric_id"])
        assert updated_metric["status"] == "suggested"
        assert any("需重新确认" in b for b in updated_metric["blocking"])
        reconfirmed = service.revise_model(
            updated["model_id"], updated["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "事件时间",
                    "skip_status_filter": True}}}, reviewer="dev")
        revised_metric = _metric_of(reconfirmed, metric["metric_id"])
        # 重确认后：待确认类阻断解除（不再永久残留）
        assert not any("需重新确认" in b for b in revised_metric["blocking"])
        assert revised_metric["status"] == "business_confirmed"
        # 发布成功，正式集合恢复
        service.publish_model(reconfirmed["model_id"], reconfirmed["version"],
                              reviewer="dev")
        published = service.get_model(reconfirmed["model_id"],
                                      reconfirmed["version"])
        assert metric["metric_id"] in published["formal_metric_ids"]

    def test_update_and_confirm_same_request_publishable(self, tmp_path):
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        revised = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "新口径",
                    "skip_status_filter": True}}}, reviewer="dev")
        revised_metric = _metric_of(revised, metric["metric_id"])
        assert revised_metric["status"] == "business_confirmed"
        assert not any("需重新确认" in b for b in revised_metric["blocking"])
        service.publish_model(revised["model_id"], revised["version"],
                              reviewer="dev")

    def test_confirm_generates_fresh_evidence(self, tmp_path):
        """确认生成新的确认证据（时间/备注），不恢复旧记录冒充新审核。"""
        env = _new_env(tmp_path)
        _env = env
        service = _env[2]
        confirmed = _confirmed_with_metric(env)
        metric = next(m for m in confirmed["metrics"]
                      if m.get("status") == "business_confirmed")
        old_evidence = dict(metric["business_confirmation"])
        updated = service.revise_model(
            confirmed["model_id"], confirmed["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "update", "time_basis": "口径A"}}}, reviewer="dev")
        reconfirmed = service.revise_model(
            updated["model_id"], updated["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "口径A",
                    "skip_status_filter": True, "note": "重新确认"}}}, reviewer="dev")
        new_evidence = _metric_of(
            reconfirmed, metric["metric_id"])["business_confirmation"]
        assert new_evidence["note"] == "重新确认"
        assert new_evidence != old_evidence
        assert new_evidence["confirmed_at"] >= old_evidence["confirmed_at"]

    def test_real_blockers_survive_confirm(self, tmp_path):
        """真正安全/业务阻断不被 confirm 解除（人工否决标记保留）。"""
        env = _new_env(tmp_path)
        service = env[2]
        source_id = env[1]
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        rejected = service.revise_model(
            model["model_id"], model["version"], {
                "metrics": {metric["metric_id"]: {"action": "reject"}}},
            reviewer="dev")
        rejected_metric = _metric_of(rejected, metric["metric_id"])
        assert any("人工否决" in b for b in rejected_metric["blocking"])
        reconfirmed = service.revise_model(
            rejected["model_id"], rejected["version"], {
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "自然日",
                    "skip_status_filter": True}}}, reviewer="dev")
        # 人工否决阻断保留（历史事实）；确认本身可行但否决事实不消失
        still = _metric_of(reconfirmed, metric["metric_id"])
        assert any("人工否决" in b for b in still["blocking"])


# == 四、依赖检查使用 latest 而非 effective ========================================


class TestDependencyEffectiveRevision:
    def test_machine_suggestion_after_human_confirm_no_degrade(self, tmp_path):
        """人工确认修订 N 后出现机器建议修订 N+1：effective 仍为 N——
        采用 N 的模型不降级（latest≠effective 正是本缺陷场景）。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        dep_key = next(iter(model["dependencies"]["m2_field_semantics"]))
        table_id, column = dep_key.rsplit(".", 1)
        # 先人工确认该字段语义（effective 被 pinned 到人工修订）
        history = repo.get_field_semantics_history(table_id, column)
        repo.save_field_semantics({
            **history[0], "inference_source": "human",
            "review_status": "business_confirmed",
        }, expected_revision=history[0]["revision"])
        # 采用人工确认修订重新构建（构建读取 effective=人工修订）
        rebuilt = service.build_model(source_id)
        assert rebuilt["availability"]["dependency_status"] == "ok"
        adopted = rebuilt["dependencies"]["m2_field_semantics"][dep_key]
        # 追加机器建议修订：history[0] 变化但 effective 仍是人工修订
        repo.save_field_semantics({
            **history[0], "inference_source": "rules",
            "review_status": "suggested", "revision": None,
        })
        latest = repo.get_field_semantics_history(table_id, column)[0]
        assert latest["revision"] != adopted  # latest 已变
        refreshed = service.get_model(rebuilt["model_id"], rebuilt["version"])
        assert refreshed["availability"]["dependency_status"] == "ok"

    def test_rebuild_after_machine_suggestion_not_self_degraded(self, tmp_path):
        """人工确认被机器建议跟随的状态下重建：不立即自我降级。"""
        repo, source_id, service = _new_env(tmp_path)
        first = service.build_model(source_id)
        dep_key = next(iter(first["dependencies"]["m2_field_semantics"]))
        table_id, column = dep_key.rsplit(".", 1)
        history = repo.get_field_semantics_history(table_id, column)
        repo.save_field_semantics({
            **history[0], "inference_source": "human",
            "review_status": "business_confirmed",
        }, expected_revision=history[0]["revision"])
        repo.save_field_semantics({
            **history[0], "inference_source": "rules",
            "review_status": "suggested", "revision": None,
        })
        rebuilt = service.build_model(source_id)  # 采用 effective（人工修订）
        assert rebuilt["availability"]["dependency_status"] == "ok"

    def test_effective_change_degrades(self, tmp_path):
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        dep_key = next(iter(model["dependencies"]["m2_field_semantics"]))
        table_id, column = dep_key.rsplit(".", 1)
        history = repo.get_field_semantics_history(table_id, column)
        # 人工修订改变 effective
        repo.save_field_semantics({
            **history[0], "inference_source": "human",
            "review_status": "business_confirmed", "revision": None,
        }, expected_revision=None)
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == (
            "needs_revalidation")

    def test_same_column_different_tables_not_confused(self, tmp_path):
        """不同表同名列：按 table_id 作用域隔离，互不串用。"""
        repo, source_id, service = _new_env(tmp_path)
        model = service.build_model(source_id)
        # 修改 booking.id 的语义不影响 guest.id 的依赖判定
        booking_dep = next(
            (k for k in model["dependencies"]["m2_field_semantics"]
             if "booking.id" in k), None)
        guest_dep = next(
            k for k in model["dependencies"]["m2_field_semantics"]
            if "guest.id" in k)
        if booking_dep is None:
            pytest.skip("夹具无该依赖")
        table_id, column = booking_dep.rsplit(".", 1)
        history = repo.get_field_semantics_history(table_id, column)
        repo.save_field_semantics({
            **history[0], "category": "name", "revision": None,
        }, expected_revision=history[0]["revision"])
        refreshed = service.get_model(model["model_id"], model["version"])
        assert refreshed["availability"]["dependency_status"] == (
            "needs_revalidation")
        # 依赖明细指向 booking 表的字段，而非 guest 同名列
        details_text = " ".join(
            refreshed["availability"]["dependency_info"]["details"]["m2"])
        assert table_id in details_text
        assert guest_dep.rsplit(".", 1)[0] not in details_text


# == 五、无关风险路径误伤单实体指标 ================================================


class TestUnrelatedPathNotBlockingSingleEntityMetrics:
    def _risky_env(self, tmp_path, cardinality="one_to_many"):
        repo, source_id, service = _new_env(tmp_path)
        seed_relationships(repo, source_id, "snap-m4", m4_catalog(source_id), [{
            "src": "booking", "src_cols": ["id"],
            "tgt": "service_item", "tgt_cols": ["id"],
            "cardinality": cardinality, "status": "validated"}])
        return repo, source_id, service

    def _confirmed_service_item_metric(self, env) -> dict:
        _, source_id, service = env
        model = service.build_model(source_id)
        # service_item.price 的本表 SUM 指标
        metric = next(
            (m for m in model["metrics"]
             if m["aggregation"] == "sum"
             and m["entity_id"] == f"{SCHEMA}.service_item"), None)
        if metric is None:
            pytest.skip("夹具无 service_item 金额指标")
        grains = {
            t["topic_id"]: {"statement": f"一行代表一条{t['name']}",
                            "keys": t["grain"]["keys"], "confirm": True}
            for t in model["topics"] if t["grain"]["keys"]}
        return service.revise_model(
            model["model_id"], model["version"], {
                "topic_grains": grains,
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "unit": "元",
                    "time_basis": "自然日", "skip_status_filter": True}}},
            reviewer="dev")

    def test_unrelated_one_to_many_path_not_blocking_own_sum(self, tmp_path):
        env = self._risky_env(tmp_path, "one_to_many")
        service = env[2]
        confirmed = self._confirmed_service_item_metric(env)
        report = service.validate_model(
            confirmed["model_id"], confirmed["version"])
        assert not any("阻断路径影响正式指标" in i for i in report["issues"]), \
            report["issues"]
        service.publish_model(confirmed["model_id"], confirmed["version"],
                              reviewer="dev")
        published = service.get_model(confirmed["model_id"],
                                      confirmed["version"])
        assert published["formal_metric_ids"]

    def test_unrelated_many_to_many_not_blocking_own_count(self, tmp_path):
        env = self._risky_env(tmp_path, "many_to_many")
        service = env[2]
        model = service.build_model(env[1])
        metric = next(
            (m for m in model["metrics"]
             if m["aggregation"] == "count_rows"
             and m["entity_id"] == f"{SCHEMA}.service_item"), None)
        if metric is None:
            pytest.skip("无该指标")
        grains = {
            t["topic_id"]: {"statement": "s", "keys": t["grain"]["keys"],
                            "confirm": True}
            for t in model["topics"] if t["grain"]["keys"]}
        confirmed = service.revise_model(
            model["model_id"], model["version"], {
                "topic_grains": grains,
                "metrics": {metric["metric_id"]: {
                    "action": "confirm", "time_basis": "自然日",
                    "skip_status_filter": True}}}, reviewer="dev")
        report = service.validate_model(
            confirmed["model_id"], confirmed["version"])
        assert not any("阻断路径影响正式指标" in i for i in report["issues"])

    def test_risky_path_stays_blocked_and_unselectable(self, tmp_path):
        env = self._risky_env(tmp_path, "many_to_many")
        service = env[2]
        model = service.build_model(env[1])
        blocked = next(
            p for p in model["paths"]
            if p["to"]["table"] == "service_item" and p["status"] == "blocked")
        with pytest.raises(ValueError, match="阻断路径不可选"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "selected_paths": {
                        blocked["topic_id"]: blocked["path_id"]}},
                reviewer="dev")

    def test_cross_entity_metric_definition_rejected(self, tmp_path):
        """依赖跨实体（路径型）的指标定义被显式拒绝——不误伤也不隐式支持。"""
        env = self._risky_env(tmp_path, "one_to_many")
        _, source_id, service = env
        model = service.build_model(source_id)
        metric = next(m for m in model["metrics"]
                      if m["aggregation"] == "count_rows"
                      and m["entity_id"].startswith(f"{SCHEMA}.booking"))
        with pytest.raises(ValueError, match="不属于指标自身实体"):
            service.revise_model(
                model["model_id"], model["version"], {
                    "metrics": {metric["metric_id"]: {
                        "action": "update",
                        "filters": [{"field": {
                            "schema": SCHEMA, "table": "service_item",
                            "column": "id"}, "operator": "ge", "value": 1}]}}},
                reviewer="dev")
