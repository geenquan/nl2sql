"""DeepSeek 客户端（OpenAI 兼容 chat/completions，httpx 实现）。

- 只重试可恢复错误（429/5xx/网络/超时）；401/403 等认证错误与 400 非法请求立即失败；
- 输出预算：max_tokens 下传；finish_reason=length 或超长内容判 output_truncated；
- 响应大小限制：Content-Length 与读取内容双重上限；
- usage/model 如实记录，服务端未提供则保持 None（unavailable）；
- 绝不把提示词、响应内容、密钥或底层异常文本写入日志或错误消息。
"""

import json
import time
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.exceptions import LLMRequestError
from app.ports.llm import LLMClient, LLMStructuredOutput, LLMUsage

_RECOVERABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class DeepSeekClient(LLMClient):
    def __init__(
        self,
        *,
        api_base: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_base or not api_key.get_secret_value():
            raise LLMRequestError("config", "模型客户端配置不完整（地址或密钥缺失）")
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    def generate_structured(
        self,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        *,
        max_output_chars: int | None = None,
    ) -> LLMStructuredOutput:
        url = f"{self._api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        attempts = self._max_retries + 1
        calls = 0
        last_error: LLMRequestError | None = None

        for attempt in range(attempts):
            calls += 1
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": self._max_output_tokens,
                "temperature": 0,
            }
            try:
                status_code, content_length, body_text = self._request(url, headers, payload)
            except _Recoverable as exc:
                last_error = exc.error
                self._sleep_backoff(attempt)
                continue
            except LLMRequestError:
                raise

            if status_code in (401, 403):
                raise LLMRequestError("auth_failure", "模型认证失败（auth_failure），已停止重试")
            if status_code in _RECOVERABLE_STATUS:
                last_error = LLMRequestError(
                    "rate_limited" if status_code == 429 else "server_error",
                    "模型服务暂时不可用，已按重试策略处理",
                )
                self._sleep_backoff(attempt)
                continue
            if status_code >= 400:
                raise LLMRequestError(
                    "invalid_request",
                    f"模型请求被拒绝（HTTP {status_code}），已停止",
                )

            if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
                raise LLMRequestError("invalid_response", "模型响应超过大小上限")
            if len(body_text.encode("utf-8", errors="ignore")) > _MAX_RESPONSE_BYTES:
                raise LLMRequestError("invalid_response", "模型响应超过大小上限")

            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                raise LLMRequestError("invalid_response", "模型响应不是合法 JSON") from None
            choices = body.get("choices") or []
            if not choices:
                raise LLMRequestError("invalid_response", "模型响应缺少 choices")
            message = choices[0].get("message") or {}
            content = message.get("content")
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                raise LLMRequestError("output_truncated", "模型输出因长度被截断，弃用该结果")
            if not isinstance(content, str) or not content.strip():
                raise LLMRequestError("invalid_response", "模型响应内容为空")
            if max_output_chars is not None and len(content) > max_output_chars:
                raise LLMRequestError("output_truncated", "模型输出超过字符预算，弃用该结果")

            parsed = self._parse_content(content)
            usage_raw = body.get("usage") or {}
            usage = LLMUsage(
                model_reported=body.get("model") or self._model,
                prompt_tokens=usage_raw.get("prompt_tokens"),
                completion_tokens=usage_raw.get("completion_tokens"),
                calls=calls,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return LLMStructuredOutput(result=parsed, usage=usage)

        raise last_error or LLMRequestError("server_error", "模型调用失败")

    # -- 内部 -----------------------------------------------------------------

    def _request(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, str | None, str]:
        """发起请求并在连接存续期内完成读取，返回 (状态码, Content-Length, 响应体)。"""
        timeout = httpx.Timeout(self._timeout_seconds, connect=5.0)
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                response = client.post(url, headers=headers, json=payload)
                return response.status_code, response.headers.get("content-length"), response.text
        except httpx.TimeoutException:
            raise _Recoverable(LLMRequestError("timeout", "模型请求超时，已按重试策略处理")) from None
        except httpx.TransportError:
            raise _Recoverable(
                LLMRequestError("network", "模型网络错误，已按重试策略处理")
            ) from None

    def _parse_content(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1 and text.rstrip().endswith("```"):
                text = text[first_newline + 1:text.rstrip().rfind("```")]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise LLMRequestError("invalid_response", "模型输出不是合法 JSON") from None
        if not isinstance(parsed, dict):
            raise LLMRequestError("invalid_response", "模型输出不是 JSON 对象")
        return parsed

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(0.5 * (2**attempt), 4.0))


class _Recoverable(Exception):
    """可恢复错误的内部中继（超时/网络），转换为 LLMRequestError 参与重试。"""

    def __init__(self, error: LLMRequestError) -> None:
        super().__init__()
        self.error = error
