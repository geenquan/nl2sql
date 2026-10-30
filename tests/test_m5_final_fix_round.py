"""M5 最后定点修复轮回归（三项；全部先在当前实现失败）。

一（P1）：恢复先核对指针再切 alias——指针冲突时不得产生外部副作用；
    alias 指向无关索引时不得覆盖（alias_conflict）。
二（P1）：创建调用结果不明（服务端已建、响应丢失）保留 pending 占用，
    不得 failed_publish 释放后被绕过；调用前确定失败仍可释放。
三（P2）：ES 请求保留默认有限超时（HTTPX timeout=None 是关闭超时）；
    多请求操作按剩余期限递减；reconcile 共享 run_context；
    用 httpx.MockTransport 验证真实 ESHttpPublisher 的实际请求超时。
"""

import threading
import time
from typing import Any

import httpx
import pytest

from app.adapters.dataset_executors import SQLiteViewPublisher
from app.adapters.es_publisher import ESHttpPublisher, FakeESPublisher
from app.core.config import Settings
from app.core.exceptions import (
    ExternalOutcomeUnknownError,
    RevisionConflictError,
    ValidationDeadlineExceededError,
)
from tests.dataset_fixtures import (
    SCHEMA,
    aggregate_definition,
    make_service,
    seed_environment,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture()
def env(tmp_path):
    return seed_environment(tmp_path)


def _attach_map(env: dict[str, Any]) -> dict[str, str]:
    base = env["data_url"].replace("sqlite:///", "")
    return {SCHEMA: f"{base}.app", "archive": f"{base}.archive"}


def _es_service(env, fake, **overrides):
    return make_service(
        env, es_publisher_factory=lambda: fake,
        settings=_settings(dataset_es_publish_enabled=True,
                           dataset_validation_rows=1000,
                           **overrides))


def _es_definition(env):
    definition = aggregate_definition(env, env["model"])
    definition["target"] = "elasticsearch"
    return definition


def _inject_pointer_failure(env):
    real_set = env["repo"].set_published_dataset

    def failing_set(*args, **kwargs):
        raise RuntimeError("本地指针故障")

    env["repo"].set_published_dataset = failing_set  # type: ignore
    return real_set


def _pending_es_publish(env, fake, service=None):
    """完整外部成功+本地提交失败 → needs_reconciliation + pending。
    返回 (service, dataset_id, version, alias, index)。"""
    service = service or _es_service(env, fake)
    dataset = service.create_dataset(env["source_id"], _es_definition(env))
    dataset_id, version = dataset["dataset_id"], dataset["version"]
    service.validate(dataset_id, version)
    real_set = _inject_pointer_failure(env)
    try:
        with pytest.raises(RuntimeError, match="本地指针故障"):
            service.publish(dataset_id, version)
    finally:
        env["repo"].set_published_dataset = real_set  # type: ignore
    alias = f"nl2sql_wide_{dataset_id}"
    index = f"nl2sql_wide_{dataset_id}_v{version}".lower()
    return service, dataset_id, version, alias, index


def _reconcile_audits(env, dataset_id) -> list:
    return [row for row in env["repo"]._aux_rows(dataset_id, "publish_log")
            if row.get("action") == "reconcile"]


# == 一：恢复先核对指针，再切换 alias ============================================


class TestPointerBeforeAlias:
    def test_pointer_and_alias_moved__no_external_side_effect(self, env):
        """指针已变（v999）且 alias 已变（无关索引）：reconcile 返回
        pointer_conflict——不调用 switch_alias（history 不增、alias 不变）、
        指针/状态/审计均不变化。"""
        fake = FakeESPublisher()
        service, dataset_id, version, alias, index = _pending_es_publish(
            env, fake)
        assert fake.aliases[alias] == index  # 失败前 alias 已切到目标

        # 指针被外部移到 v999；alias 被移到无关索引
        assert env["repo"].set_published_dataset(
            env["source_id"], dataset_id, 999, operator="external")
        unrelated = "nl2sql_wide_someone_else_v7"
        fake.aliases[alias] = unrelated
        history_before = len(fake.alias_history)
        audits_before = _reconcile_audits(env, dataset_id)

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "pointer_conflict"
        # 外部副作用证据：alias 未被切换/未被动过
        assert len(fake.alias_history) == history_before
        assert fake.aliases[alias] == unrelated
        # 本地状态证据：指针/状态/审计不变；占用退回 pending
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == 999
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        assert _reconcile_audits(env, dataset_id) == audits_before
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

    def test_alias_unknown_index_not_removed(self, env):
        """指针未冲突（仍无指针）但 alias 指向无关索引：alias_conflict，
        不把未知索引作为 previous_index 移除。"""
        fake = FakeESPublisher()
        service, dataset_id, version, alias, _index = _pending_es_publish(
            env, fake)
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None  # previous=None
        unrelated = "nl2sql_wide_other_dataset_v3"
        fake.aliases[alias] = unrelated
        history_before = len(fake.alias_history)

        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "alias_conflict"
        assert fake.aliases[alias] == unrelated, "不得覆盖无关索引"
        assert len(fake.alias_history) == history_before
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

    def test_normal_and_idempotent_recovery_still_pass(self, env):
        """正常恢复（alias 已指向目标）与首次发布（无前指针、alias 不存在）
        仍可通过。"""
        fake = FakeESPublisher()
        service, dataset_id, version, alias, index = _pending_es_publish(
            env, fake)
        # alias 已指向目标（发布失败发生在切换之后）→ 幂等完成
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_index_complete"
        assert fake.aliases[alias] == index
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version
        assert service.get_dataset(dataset_id, version)["status"] == "published"

        # 首次发布恢复：alias 不存在（模拟切换请求结果丢失）→ 显式切换
        fake2 = FakeESPublisher()
        service2, ds2, v2, alias2, index2 = _pending_es_publish(env, fake2)
        fake2.aliases.pop(alias2)  # alias 未建立
        reconciled2 = service2.reconcile(ds2, v2)
        assert reconciled2["reconciled"] == "external_index_complete"
        assert fake2.aliases[alias2] == index2


# == 二：创建结果不明保留 pending ================================================


class UnknownCreateES(FakeESPublisher):
    """先真实创建索引，再抛"响应丢失"（服务端已建、调用方不知道）。"""

    def __init__(self):
        super().__init__()
        self.fail_creation = True

    def create_version_index(self, index_name, mapping, *, timeout_ms=None):
        if self.fail_creation:
            super().create_version_index(index_name, mapping)
            raise ExternalOutcomeUnknownError(
                "index_create_unknown",
                "索引创建请求已发出但响应丢失（替身注入）")
        return super().create_version_index(index_name, mapping,
                                            timeout_ms=timeout_ms)


class UnknownCreateView(SQLiteViewPublisher):
    """先真实创建视图，再抛"响应丢失"。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_creation = True

    def create_versioned_view(self, target_schema, view_name, select_sql,
                              params=None):
        if self.fail_creation:
            super().create_versioned_view(target_schema, view_name,
                                          select_sql, params)
            raise ExternalOutcomeUnknownError(
                "view_publish_unknown",
                "视图创建请求已发出但响应丢失（替身注入）")
        return super().create_versioned_view(target_schema, view_name,
                                             select_sql, params)


class TestUnknownCreationOutcome:
    def test_es_creation_response_lost_keeps_pending(self, env):
        fake = UnknownCreateES()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        with pytest.raises(ExternalOutcomeUnknownError):
            service.publish(dataset_id, version)

        # 索引实际存在（服务端已创建）
        index = f"nl2sql_wide_{dataset_id}_v{version}".lower()
        assert index in fake.indices, "替身应先真实创建索引"
        # 不得误判为确定失败：needs_reconciliation + pending 保留
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"
        # 新发布与回滚均被 409 拒绝，且无额外外部副作用
        revised = service.revise_dataset(dataset_id, version,
                                         {"join_type": "left"})
        service.validate(revised["dataset_id"], revised["version"])
        indices_before = set(fake.indices)
        with pytest.raises(RevisionConflictError):
            service.publish(revised["dataset_id"], revised["version"])
        assert set(fake.indices) == indices_before
        assert fake.aliases == {}
        assert service.get_dataset(dataset_id, version)["status"] \
            != "published"

    def test_view_creation_response_lost_recovers(self, env):
        publisher = UnknownCreateView(env["data_url"], attach=_attach_map(env))
        service = make_service(
            env, view_publisher_factory=lambda: publisher,
            settings=_settings(dataset_view_publish_enabled=True,
                               dataset_view_target_schema="app",
                               dataset_validation_rows=1000))
        dataset = service.create_dataset(
            env["source_id"], aggregate_definition(env, env["model"]))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        with pytest.raises(ExternalOutcomeUnknownError):
            service.publish(dataset_id, version)
        # 视图实际存在 + pending 保留
        view_name = f"nl2sql_ds_{dataset_id}_v{version}"
        assert publisher.view_exists("app", view_name)
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"

        # 故障移除 → 既有 reconcile 正确恢复
        publisher.fail_creation = False
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "external_object_exists"
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == version
        assert service.get_dataset(dataset_id, version)["status"] == "published"

    def test_definite_pre_creation_failure_releases(self, env):
        """调用前确定失败（目标已存在=明确服务端状态）：failed_publish +
        释放占用（不是 pending）。"""
        fake = FakeESPublisher()
        service = _es_service(env, fake)
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, version = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, version)
        index = f"nl2sql_wide_{dataset_id}_v{version}".lower()
        fake.indices[index] = {}  # 预先存在（明确状态）
        with pytest.raises(Exception):  # noqa: B017 - index_exists 冲突
            service.publish(dataset_id, version)
        assert service.get_dataset(dataset_id, version)["status"] \
            == "failed_publish"
        # 确定结束：占用不保留 pending
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is None or lock["status"] not in ("pending", "recovering")

    def test_reconcile_query_failure_keeps_pending(self, env):
        """恢复时查询对象状态失败（网络）≠ 对象不存在：保留 pending。"""
        fake = FakeESPublisher()
        service, dataset_id, version, alias, index = _pending_es_publish(
            env, fake)
        fake._count_error = True  # type: ignore[attr-defined]
        reconciled = service.reconcile(dataset_id, version)
        assert reconciled["reconciled"] == "count_failed"
        assert service.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"
        assert fake.aliases[alias] == index  # 未切别名
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None


# == 三：ES 请求超时（真实 ESHttpPublisher + MockTransport） =====================


class _RecordingTransport(httpx.MockTransport):
    """记录每次请求的实际超时（httpx 把 timeout 写入 request.extensions）。"""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        super().__init__(handler)

    def handle_request(self, request):
        self.requests.append(request)
        return super().handle_request(request)


def _publisher_with_transport(handler, *, timeout_seconds=5.0, clock=None):
    publisher = ESHttpPublisher(
        hosts=["http://es.test"], timeout_seconds=int(timeout_seconds),
        clock=clock)
    transport = _RecordingTransport(handler)
    publisher._client = httpx.Client(
        base_url="http://es.test",
        headers={"Content-Type": "application/json"},
        timeout=timeout_seconds, transport=transport)
    publisher._transport_recorder = transport
    return publisher


def _timeout_of(request: httpx.Request):
    return request.extensions.get("timeout") or {}


class TestESHttpRequestTimeouts:
    def test_no_timeout_ms_uses_finite_default(self):
        """A：不传 timeout_ms → 实际请求使用配置的有限超时
        （HTTPX timeout=None 是关闭超时，不允许）。"""
        def handler(request):
            if request.method == "HEAD":
                return httpx.Response(404)
            return httpx.Response(200)

        publisher = _publisher_with_transport(handler, timeout_seconds=5)
        publisher.create_version_index("idx", {"properties": {}})
        timeouts = [_timeout_of(r) for r in
                    publisher._transport_recorder.requests]
        assert len(timeouts) == 2, "HEAD+PUT 两次请求"
        for ext in timeouts:
            values = [ext.get(k) for k in
                      ("connect", "read", "write", "pool")]
            assert all(isinstance(v, (int, float)) for v in values), (
                f"超时被关闭或缺失：{ext}")
            assert all(v == 5.0 for v in values), ext

    def test_head_consumes_deadline_put_shrinks(self):
        """B：HEAD 消耗大部分期限 → PUT 用减少后的剩余时间。"""
        clock = {"t": 1000.0}

        def handler(request):
            if request.method == "HEAD":
                clock["t"] += 9.5  # 消耗 9.5s（总期限 10s）
                return httpx.Response(404)
            return httpx.Response(200)

        publisher = _publisher_with_transport(
            handler, timeout_seconds=5, clock=lambda: clock["t"])
        publisher.create_version_index("idx", {"properties": {}},
                                       timeout_ms=10_000)
        requests = publisher._transport_recorder.requests
        assert [r.method for r in requests] == ["HEAD", "PUT"]
        put_timeout = _timeout_of(requests[1]).get("read")
        assert put_timeout is not None and 0.3 <= put_timeout <= 0.7, (
            f"PUT 应使用剩余 ~0.5s，实际 {put_timeout}")

    def test_deadline_exhausted_put_not_issued(self):
        clock = {"t": 1000.0}

        def handler(request):
            if request.method == "HEAD":
                clock["t"] += 11.0  # 超过总期限
                return httpx.Response(404)
            return httpx.Response(200)

        publisher = _publisher_with_transport(
            handler, timeout_seconds=5, clock=lambda: clock["t"])
        with pytest.raises(Exception) as excinfo:
            publisher.create_version_index("idx", {"properties": {}},
                                           timeout_ms=10_000)
        assert getattr(excinfo.value, "reason", "") == "deadline_exhausted"
        requests = publisher._transport_recorder.requests
        assert [r.method for r in requests] == ["HEAD"], "PUT 不得发出"

    def test_bulk_retry_timeout_shrinks_and_stops(self):
        """C：bulk 重试每次重新计算剩余期限；耗尽后不再发出请求。"""
        clock = {"t": 2000.0}

        def handler(request):
            clock["t"] += 6.0  # 每次尝试消耗 6s（总期限 10s）
            return httpx.Response(
                429, json={"errors": True, "items": []})

        publisher = _publisher_with_transport(
            handler, timeout_seconds=5, clock=lambda: clock["t"])
        with pytest.raises(Exception) as excinfo:
            publisher.bulk_index("idx", [{"_id": "a", "_source": {}}],
                                 timeout_ms=10_000)
        assert getattr(excinfo.value, "reason", "") == "deadline_exhausted"
        requests = publisher._transport_recorder.requests
        # 两次请求发出（第 3 次因期限耗尽不启动）；第 2 次超时=剩余 4s
        assert len(requests) == 2, f"期限耗尽后不得重试：{len(requests)}"
        second = _timeout_of(requests[1]).get("read")
        assert second is not None and 3.5 <= second <= 4.5, second

    def test_invalid_timeout_ms_rejected_before_request(self):
        """timeout_ms<=0 不得解释为无限等待：发出请求前拒绝。"""
        def handler(request):
            return httpx.Response(200)

        publisher = _publisher_with_transport(handler)
        with pytest.raises(Exception) as excinfo:
            publisher.count("idx", timeout_ms=0)
        assert getattr(excinfo.value, "reason", "") == "invalid_timeout"
        assert publisher._transport_recorder.requests == []


class TestReconcileSharedDeadline:
    def test_reconcile_deadline_exhausted_keeps_pending(self, env):
        """D：恢复操作共享期限——count 耗尽期限后不切别名、不提交指针，
        占用退回 pending。"""
        fake = FakeESPublisher()
        _service, dataset_id, version, alias, index = _pending_es_publish(
            env, fake)

        class SlowCount(FakeESPublisher):
            def count(self, index_name, *, timeout_ms=None):
                time.sleep(0.4)  # 越过 0.3s 恢复期限
                return super().count(index_name)

        slow = SlowCount()
        slow.indices = fake.indices
        slow.documents = fake.documents
        slow.aliases = fake.aliases
        slow.alias_history = fake.alias_history
        service2 = _es_service(env, slow)

        original_context = service2._new_run_context

        def short_context():
            context = original_context()
            context["deadline"] = time.monotonic() + 0.3
            return context

        service2._new_run_context = short_context  # type: ignore
        with pytest.raises(ValidationDeadlineExceededError):
            service2.reconcile(dataset_id, version)
        # 未切别名、未提交指针；占用退回 pending
        assert slow.aliases[alias] == index
        assert len(slow.alias_history) == 1
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) is None
        assert service2.get_dataset(dataset_id, version)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"
        assert _reconcile_audits(env, dataset_id) == []


# == 并发不变量（保持） ==========================================================


class TestConcurrencyInvariant:
    def test_concurrent_claims_still_exactly_one(self, env):
        repo = env["repo"]
        dataset_id = "ds-final-race"
        assert repo.acquire_publish_lock(dataset_id, "op-x", "publish")
        repo.transition_publish_lock(dataset_id, "op-x", "active", "pending")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            results.append(repo.claim_publish_reconciliation(
                dataset_id, "op-x"))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(results) == [False, True]
