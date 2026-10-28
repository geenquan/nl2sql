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
        assert out.usage.calls == 2

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
