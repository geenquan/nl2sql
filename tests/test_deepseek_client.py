"""DeepSeek 客户端测试：httpx MockTransport 模拟，不产生真实网络请求。"""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.exceptions import LLMRequestError
from app.llm.deepseek_client import DeepSeekClient


def make_client(transport: httpx.BaseTransport, **overrides) -> DeepSeekClient:
    params = {
        "api_base": "https://api.deepseek.example/v1",
        "api_key": SecretStr("test-key-do-not-log"),
        "model": "deepseek-test",
        "timeout_seconds": 5,
        "max_retries": 2,
        "max_output_tokens": 512,
    }
    params.update(overrides)
    return DeepSeekClient(transport=transport, **params)


def ok_response(content: dict | str, model: str = "deepseek-test", finish: str = "stop",
                usage: dict | None = None) -> httpx.Response:
    # 真实 API 的 message.content 是字符串
    content_text = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
    body = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content_text}, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


SCHEMA = {"type": "object"}


class TestSuccess:
    def test_returns_parsed_result_and_usage(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return ok_response({"a": 1}, usage={"prompt_tokens": 10, "completion_tokens": 5})

        client = make_client(httpx.MockTransport(handler))
        out = client.generate_structured("system", "user", SCHEMA)
        assert out.result == {"a": 1}
        assert out.usage.model_reported == "deepseek-test"
        assert out.usage.prompt_tokens == 10
        assert out.usage.completion_tokens == 5
        assert out.usage.calls == 1
        # 请求头与消息体不落日志且包含认证头
        assert calls[0].headers["authorization"] == "Bearer test-key-do-not-log"

    def test_usage_unavailable_when_server_omits(self) -> None:
        client = make_client(httpx.MockTransport(lambda r: ok_response({"a": 1})))
        out = client.generate_structured("system", "user", SCHEMA)
        assert out.usage.prompt_tokens is None  # unavailable，不编造
        assert out.usage.completion_tokens is None


class TestRetryPolicy:
    def test_auth_failure_no_retry(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, json={"error": "bad key"})

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason == "auth_failure"
        assert len(calls) == 1  # 不重试

    def test_rate_limited_then_success(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, json={"error": "rate limit"})
            return ok_response({"a": 1})

        client = make_client(httpx.MockTransport(handler))
        out = client.generate_structured("system", "user", SCHEMA)
        assert out.result == {"a": 1}
        # 新口径：calls=逻辑调用数，http_attempts=含传输重试的 HTTP 尝试数
        assert out.usage.calls == 1
        assert out.usage.http_attempts == 2

    def test_retry_limit_exceeded(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"error": "down"})

        client = make_client(httpx.MockTransport(handler), max_retries=2)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason in ("server_error", "rate_limited")
        assert len(attempts) == 3  # 1 + 2 次重试

    def test_timeout_retries_then_fails(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadTimeout("timed out")

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason == "timeout"
        assert len(attempts) == 3

    def test_invalid_request_no_retry(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(400, json={"error": "bad request"})

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason == "invalid_request"
        assert len(calls) == 1  # 非法请求不重试


class TestOutputLimits:
    def test_truncated_by_finish_reason(self) -> None:
        client = make_client(
            httpx.MockTransport(lambda r: ok_response('{"a": 1', finish="length"))
        )
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason == "output_truncated"

    def test_oversized_content_rejected(self) -> None:
        big = json.dumps({"k": "v" * 5000})
        client = make_client(httpx.MockTransport(lambda r: ok_response(big)))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA, max_output_chars=1000)
        assert exc.value.reason in ("output_truncated", "invalid_response")

    def test_invalid_json_content(self) -> None:
        client = make_client(httpx.MockTransport(lambda r: ok_response("not json at all")))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert exc.value.reason == "invalid_response"

    def test_code_fence_content_parsed(self) -> None:
        client = make_client(
            httpx.MockTransport(lambda r: ok_response('```json\n{"a": 2}\n```'))
        )
        out = client.generate_structured("system", "user", SCHEMA)
        assert out.result == {"a": 2}

    def test_error_messages_never_contain_key_or_body(self) -> None:
        client = make_client(
            httpx.MockTransport(lambda r: ok_response("<<<bad response with secret sk-live-xyz123456>>>"))
        )
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("system", "user", SCHEMA)
        assert "test-key-do-not-log" not in str(exc.value)
        assert "sk-live" not in str(exc.value)


class TestAttemptBudgetAndUsage:
    def test_max_attempts_caps_transport_retries(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"error": "down"})

        client = make_client(httpx.MockTransport(handler), max_retries=5)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA, max_attempts=2)
        assert len(attempts) == 2  # 预算 2 次封顶，嵌套重试不放大
        assert exc.value.attempts == 2

    def test_error_carries_attempts_for_budget_accounting(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadTimeout("timed out")

        client = make_client(httpx.MockTransport(handler), max_retries=3)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA, max_attempts=3)
        assert exc.value.reason == "timeout"
        assert exc.value.attempts == 3

    def test_model_reported_none_when_server_omits(self) -> None:
        client = make_client(httpx.MockTransport(
            lambda r: ok_response({"a": 1}, model="")))
        out = client.generate_structured("s", "u", SCHEMA)
        assert out.usage.model_reported in (None, "")
        assert out.usage.model_configured == "deepseek-test"  # 配置模型单列，不冒充

    def test_oversize_response_rejected_with_declared_length(self) -> None:
        huge = httpx.Response(
            200, headers={"content-length": str(10 * 1024 * 1024)},
            json={"k": "v"})
        client = make_client(httpx.MockTransport(lambda r: huge))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"

    def test_oversize_response_rejected_streaming_without_length(self) -> None:
        big_body = json.dumps({"k": "v" * (3 * 1024 * 1024)}).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=big_body)  # 无 content-length 声明

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"

    def test_no_backoff_after_final_attempt(self) -> None:
        import time as _time

        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(_time.monotonic())
            return httpx.Response(503, json={"error": "down"})

        client = make_client(httpx.MockTransport(handler), max_retries=2)
        with pytest.raises(LLMRequestError):
            client.generate_structured("s", "u", SCHEMA)
        raised_at = _time.monotonic()  # 紧接异常返回之后
        # 最后一次尝试之后立即抛错，无额外退避
        assert raised_at - attempts[-1] < 0.3


class TestFixRound2UsageOnFailure:
    """失败路径的次数与用量计量。"""

    def test_retry_then_invalid_json_preserves_usage_and_attempts(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) <= 2:
                return httpx.Response(429, json={"error": "rate limit"})
            # 第三次：HTTP 200 但内容非法，响应带 usage
            return httpx.Response(200, json={
                "model": "deepseek-test",
                "usage": {"prompt_tokens": 111, "completion_tokens": 22},
                "choices": [{"message": {"content": "<<<not json>>>"}}],
            })

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"
        assert exc.value.attempts == 3  # 实际 HTTP 尝试数
        assert exc.value.usage is not None  # 已提供用量不丢失
        assert exc.value.usage.prompt_tokens == 111
        assert exc.value.usage.completion_tokens == 22

    def test_truncated_with_usage_carries_usage(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "model": "deepseek-test",
                "usage": {"prompt_tokens": 50, "completion_tokens": 60},
                "choices": [{"message": {"content": '{"a": 1'},
                                            "finish_reason": "length"}],
            })

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "output_truncated"
        assert exc.value.usage.prompt_tokens == 50

    def test_missing_choices_but_usage_present(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "model": "deepseek-test",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [],
            })

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"
        assert exc.value.usage.prompt_tokens == 10

    def test_partial_usage_kept_per_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "model": "deepseek-test",
                "usage": {"prompt_tokens": 77},  # completion 缺失
                "choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
            })

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.usage.prompt_tokens == 77  # 已知一侧保留
        assert exc.value.usage.completion_tokens is None  # 另一侧如实缺失

    def test_network_failure_usage_unknown_not_fabricated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "network"
        assert exc.value.usage is None  # 无响应 → unknown，不虚构
        assert exc.value.attempts == 3  # 1 + 2 次重试都计数

    def test_oversize_on_second_attempt_counted(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

        client = make_client(httpx.MockTransport(handler))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"
        assert exc.value.attempts == 2


class TestFixRound2Deadline:
    """运行总期限（注入时钟，不做真实长 sleep）。"""

    @staticmethod
    def _client(handler, clock) -> DeepSeekClient:
        return make_client(httpx.MockTransport(handler), clock=clock)

    def test_single_response_crossing_deadline(self) -> None:
        clock = {"t": 100.0}
        advances = iter([105.0])  # 响应读取后的时钟

        def fake_clock() -> float:
            return clock["t"]

        def handler(request: httpx.Request) -> httpx.Response:
            clock["t"] = next(advances)  # 响应返回即已越界
            return ok_response({"a": 1})

        client = self._client(handler, fake_clock)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA, deadline_monotonic=101.0)
        assert exc.value.reason == "deadline_exceeded"
        assert exc.value.attempts == 1

    def test_deadline_before_first_call(self) -> None:
        client = make_client(
            httpx.MockTransport(lambda r: ok_response({"a": 1})), clock=lambda: 100.0)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA, deadline_monotonic=99.0)
        assert exc.value.reason == "deadline_exceeded"
        assert exc.value.attempts == 0  # 未发起任何请求

    def test_backoff_capped_and_deadline_wins(self) -> None:
        clock = {"t": 100.0}

        def fake_clock() -> float:
            return clock["t"]

        def handler(request: httpx.Request) -> httpx.Response:
            clock["t"] += 10.0  # 每次请求跨过剩余期限
            return httpx.Response(429, json={"error": "rate limit"})

        client = make_client(httpx.MockTransport(handler), clock=fake_clock, max_retries=3)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA, deadline_monotonic=103.0)
        assert exc.value.reason == "deadline_exceeded"
        assert exc.value.attempts == 1  # 退避被期限截断，不继续重试

    def test_within_deadline_succeeds(self) -> None:
        clock = {"t": 100.0}

        def fake_clock() -> float:
            return clock["t"]

        def handler(request: httpx.Request) -> httpx.Response:
            clock["t"] += 0.1
            return ok_response({"a": 1})

        client = make_client(httpx.MockTransport(handler), clock=fake_clock)
        out = client.generate_structured("s", "u", SCHEMA, deadline_monotonic=200.0)
        assert out.result == {"a": 1}


class TestFixRound2BoundedErrorBodies:
    """所有 HTTP 状态的响应体都受有界读取约束。"""

    def test_429_oversize_body_without_length_not_consumed(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, content=b"e" * (10 * 1024 * 1024))

        client = make_client(httpx.MockTransport(handler), max_retries=0)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "rate_limited"
        assert len(calls) == 1

    def test_500_oversize_chunked_body(self) -> None:
        client = make_client(
            httpx.MockTransport(lambda r: httpx.Response(500, content=b"x" * (5 * 1024 * 1024))),
            max_retries=0)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "server_error"

    def test_declared_small_length_but_actual_oversize(self) -> None:
        client = make_client(httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-length": "100"}, content=b"y" * (3 * 1024 * 1024))))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_response"

    def test_invalid_content_length_ignored(self) -> None:
        # 非法 Content-Length 被忽略，仍按字节上限处理；正常小响应不受影响
        client = make_client(httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-length": "not-a-number"},
            json={"model": "m", "choices": [{"message": {"content": '{"a":1}'},
                                             "finish_reason": "stop"}]})))
        out = client.generate_structured("s", "u", SCHEMA)
        assert out.result == {"a": 1}

    def test_small_error_body_readable(self) -> None:
        client = make_client(httpx.MockTransport(lambda r: httpx.Response(400, json={"e": 1})))
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "invalid_request"
        assert exc.value.attempts == 1

    def test_mid_read_transport_error(self) -> None:
        class BrokenBody(httpx.Response):
            def read(self) -> bytes:
                raise httpx.ReadError("broken mid-read")

        client = make_client(httpx.MockTransport(lambda r: BrokenBody(200, content=b"x")),
                             max_retries=0)
        with pytest.raises(LLMRequestError) as exc:
            client.generate_structured("s", "u", SCHEMA)
        assert exc.value.reason == "network"
