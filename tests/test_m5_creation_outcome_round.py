"""M5 创建超期误释放占用定点修复回归（先在当前实现失败）。

缺陷：create_version_index 的 PUT 已在服务端成功创建索引、但响应返回时
超过操作期限 → ESPublishError(reason=deadline_exhausted)；
_creation_definitely_absent 因 ESPublishError 继承 ValueError 把它误判为
"确定未创建" → failed_publish+释放占用，结果未明的发布被绕过。

全部使用真实 ESHttpPublisher + httpx.MockTransport + DatasetService 跨层
验证（不依赖 FakeESPublisher 直接抛 ExternalOutcomeUnknownError）。
"""

import time
from typing import Any

import httpx
import pytest

from app.adapters.es_publisher import ESHttpPublisher, ESPublishError
from app.core.config import Settings
from app.core.exceptions import (
    ExternalOutcomeUnknownError,
    RevisionConflictError,
)
from tests.dataset_fixtures import (
    aggregate_definition,
    make_service,
    seed_environment,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture()
def env(tmp_path):
    return seed_environment(tmp_path)


class _RecordingTransport(httpx.MockTransport):
    """记录全部请求（用于断言 bulk/alias 未被调用）。"""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        super().__init__(handler)

    def handle_request(self, request):
        self.requests.append(request)
        return super().handle_request(request)


def _es_definition(env):
    definition = aggregate_definition(env, env["model"])
    definition["target"] = "elasticsearch"
    return definition


def _make_cross_layer_publisher_factory(
        *, slow_put_index_suffix: str | None = None,
        head_advances_clock: float = 0.0):
    """真实 ESHttpPublisher + MockTransport 模拟服务端。

    - HEAD → 404（索引不存在）；
    - PUT → 服务端记录"索引已创建"；当索引名含 slow_put_index_suffix
      时推进注入时钟越过操作期限（创建成功但响应超期），返回 200；
    - bulk/_count/_aliases 支撑正常发布路径（v1）。
    服务层每次发布会 close 发布器：工厂每次返回**新**发布器实例，
    但共享同一 transport（同一模拟服务端/请求记录）与时钟。
    返回 (factory, server_state, clock, transport)。
    """
    clock = {"t": time.monotonic()}
    server_state: dict[str, Any] = {
        "created_indexes": set(), "documents": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "HEAD":
            clock["t"] += head_advances_clock
            return httpx.Response(404)
        if request.method == "PUT":
            server_state["created_indexes"].add(path.strip("/"))
            if slow_put_index_suffix and slow_put_index_suffix in path:
                clock["t"] += 400.0  # 创建成功，但响应返回时超期
            return httpx.Response(200)
        if path == "/_bulk":
            lines = request.read().decode().strip().splitlines()
            server_state["documents"] += len(lines) // 2  # NDJSON 每文档 2 行
            return httpx.Response(200, json={"errors": False})
        if path.endswith("/_count"):
            return httpx.Response(
                200, json={"count": server_state["documents"]})
        if path == "/_aliases":
            return httpx.Response(200, json={"acknowledged": True})
        return httpx.Response(200)

    transport = _RecordingTransport(handler)

    def factory() -> ESHttpPublisher:
        publisher = ESHttpPublisher(
            hosts=["http://es.test"], timeout_seconds=5,
            clock=lambda: clock["t"])
        publisher._client = httpx.Client(
            base_url="http://es.test",
            headers={"Content-Type": "application/json"},
            timeout=5, transport=transport)
        return publisher

    return factory, server_state, clock, transport


class TestPutCreatedButDeadlineExceeded:
    def test_put_created_response_late_keeps_pending(self, env):
        """跨层：PUT 在服务端创建成功、响应返回时超期 → ExternalOutcome
        UnknownError → needs_reconciliation+pending；无 bulk/alias/指针提交；
        新发布与回滚被 409 且无额外外部副作用。"""
        # v2 索引的 PUT 推进注入时钟 400s（越过适配器 ~300s 操作期限）
        publisher_factory, server_state, _clock, transport = \
            _make_cross_layer_publisher_factory(
                slow_put_index_suffix="_v2")
        service = make_service(
            env, es_publisher_factory=publisher_factory,
            settings=_settings(dataset_es_publish_enabled=True,
                               dataset_validation_rows=1000))
        # v1 正常发布（真实 HTTP 路径：创建/导出/计数/别名/指针）
        dataset = service.create_dataset(env["source_id"], _es_definition(env))
        dataset_id, v1 = dataset["dataset_id"], dataset["version"]
        service.validate(dataset_id, v1)
        service.publish(dataset_id, v1)
        assert service.get_dataset(dataset_id, v1)["status"] == "published"

        # v2：创建成功但响应超期
        revised = service.revise_dataset(dataset_id, v1, {"join_type": "left"})
        v2 = revised["version"]
        service.validate(dataset_id, v2)
        with pytest.raises(ExternalOutcomeUnknownError):
            service.publish(dataset_id, v2)

        # 1. v2 的 HEAD、PUT 确实发出；模拟服务端 v2 索引确实存在
        v2_index = f"nl2sql_wide_{dataset_id}_v{v2}".lower()
        v2_requests = [r for r in transport.requests
                       if v2_index in r.url.path]
        assert [r.method for r in v2_requests] == ["HEAD", "PUT"]
        assert v2_index in server_state["created_indexes"]
        # 2.（异常类型已由 pytest.raises 断言为 ExternalOutcomeUnknownError）
        # 3/4. needs_reconciliation + pending（不 failed_publish/不释放）
        assert service.get_dataset(dataset_id, v2)["status"] \
            == "needs_reconciliation"
        lock = env["repo"].get_active_publish_lock(dataset_id)
        assert lock is not None and lock["status"] == "pending"
        # 5. 没有 v2 的 bulk、alias 切换或发布指针提交
        v2_side_effects = [r for r in transport.requests
                           if r.url.path in ("/_bulk", "/_aliases")
                           or r.url.path.endswith("/_count")]
        assert len(v2_side_effects) == 3, (
            "只有 v1 的一次 bulk/count/alias；v2 不得有导出或切换")
        assert env["repo"].get_published_dataset(
            env["source_id"], dataset_id) == v1
        # 6. 新版本发布与回滚被 409 拒绝，且无额外外部副作用
        revised3 = service.revise_dataset(dataset_id, v2, {"join_type": "left"})
        service.validate(revised3["dataset_id"], revised3["version"])
        requests_before = len(transport.requests)
        with pytest.raises(RevisionConflictError):
            service.publish(revised3["dataset_id"], revised3["version"])
        assert len(transport.requests) == requests_before
        with pytest.raises(RevisionConflictError):
            service.rollback(dataset_id, v1)
        assert len(transport.requests) == requests_before
        assert len(server_state["created_indexes"]) == 2  # 无第二次创建尝试


class TestClassificationGuards:
    def test_deadline_exhausted_not_definitely_absent(self):
        """ESPublishError(reason=deadline_exhausted) 不得被判为确定未创建
        （ESPublishError 继承 ValueError，普通 ValueError 判断过宽）。"""
        from app.services.dataset_service import DatasetService

        exc = ESPublishError("deadline_exhausted",
                             "ES 操作期限耗尽：响应结果不用于发布")
        assert isinstance(exc, ValueError)  # 缺陷前提：它是 ValueError
        assert DatasetService._creation_definitely_absent(exc) is False

        # 明确分类仍为确定未创建
        for reason in ("index_exists", "index_create_rejected",
                       "invalid_timeout"):
            definite = ESPublishError(reason, "固定安全文案")
            assert DatasetService._creation_definitely_absent(
                definite) is True, reason

    def test_pre_put_deadline_exhausted_does_not_send_put(self):
        """PUT 发出前期限耗尽：不发送 PUT，且不得声称已创建
        （异常为 deadline_exhausted，非 ExternalOutcomeUnknownError）。"""
        # 创建期限 10s；HEAD 响应推进时钟 11s → PUT 前耗尽
        publisher_factory, server_state, _clock, transport = \
            _make_cross_layer_publisher_factory(head_advances_clock=11.0)
        with pytest.raises(ESPublishError) as excinfo:
            publisher_factory().create_version_index(
                "idx_pre_put", {"properties": {}}, timeout_ms=10_000)
        assert excinfo.value.reason == "deadline_exhausted"
        assert [r.method for r in transport.requests] == ["HEAD"]
        assert server_state["created_indexes"] == set(), "不得发出 PUT"
