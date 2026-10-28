"""DeepSeek 客户端（OpenAI 兼容 chat/completions，httpx 实现；修复轮 2 加固）。

计量与预算（与上层共享，不嵌套放大）：
- 一次 generate_structured 是一次“逻辑调用”；max_attempts 限定该次逻辑调用的
  总 HTTP 尝试数；每次尝试在真正发起请求处计数，所有失败路径都携带累计次数；
- deadline_monotonic（绝对截止时间，单调时钟）：覆盖连接、读取、退避与流式
  读取中的每个分块；退避不超过剩余期限；超期立即停止并抛 deadline_exceeded；
- usage/model 在解析模型内容之前先从响应提取；内容不合格/截断不丢失已提供的
  用量（随异常携带）；认证失败、无响应体的失败 usage=None（不虚构）；
- 所有 HTTP 状态（含错误响应）都走有界流式读取；不信任 Content-Length
  （非法值忽略，累计字节为最终上限）；错误响应体超限直接丢弃（只需状态码）；
- 成功路径 model_reported 只填服务端返回值（缺失为 None），与配置模型分列；
- 绝不把提示词、响应内容、密钥或底层异常文本写入日志或错误消息。
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.exceptions import LLMRequestError
from app.ports.llm import LLMClient, LLMStructuredOutput, LLMUsage

_RECOVERABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ERROR_BODY_SNIFF_BYTES = 4 * 1024


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
        clock: Callable[[], float] = time.monotonic,
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
        self._clock = clock

    def generate_structured(
        self,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        *,
        max_output_chars: int | None = None,
        max_attempts: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> LLMStructuredOutput:
        url = f"{self._api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0,
        }
        default_attempts = self._max_retries + 1
        attempts_allowed = max(1, min(default_attempts, max_attempts or default_attempts))
        started = self._clock()
        attempts = 0
        last_error: LLMRequestError | None = None
        known_usage: LLMUsage | None = None  # 最后一个带 usage 的响应（可能不完整）

        def _remaining_deadline() -> float | None:
            if deadline_monotonic is None:
                return None
            return deadline_monotonic - self._clock()

        def _fail(reason: str, message: str) -> LLMRequestError:
            return LLMRequestError(reason, message, attempts=attempts, usage=known_usage)

        while attempts < attempts_allowed:
            remaining_deadline = _remaining_deadline()
            if remaining_deadline is not None and remaining_deadline <= 0:
                raise _fail("deadline_exceeded", "模型调用超过运行期限，已停止")
            attempts += 1  # 计数统一在真正发起请求处
            read_timeout = self._timeout_seconds
            if remaining_deadline is not None:
                read_timeout = max(0.05, min(self._timeout_seconds, remaining_deadline))
            try:
                status_code, body_text, usage_raw, model_reported = self._request(
                    url, headers, payload, read_timeout, deadline_monotonic)
            except _Recoverable as exc:
                last_error = exc.error
                if attempts >= attempts_allowed:
                    break  # 最后一次失败不再退避
                self._sleep_backoff(attempts - 1, _remaining_deadline())
                continue
            except LLMRequestError as exc:
                exc.attempts = attempts
                exc.usage = known_usage
                raise

            # usage/model 在内容校验之前提取（内容失败不丢用量）
            if usage_raw:
                known_usage = LLMUsage(
                    model_reported=model_reported,
                    model_configured=self._model,
                    prompt_tokens=usage_raw.get("prompt_tokens"),
                    completion_tokens=usage_raw.get("completion_tokens"),
                    calls=1, http_attempts=attempts, elapsed_ms=0,
                )

            if status_code in (401, 403):
                raise _fail("auth_failure", "模型认证失败（auth_failure），已停止重试")
            if status_code in _RECOVERABLE_STATUS:
                last_error = _fail(
                    "rate_limited" if status_code == 429 else "server_error",
                    "模型服务暂时不可用，已按重试策略处理")
                if attempts >= attempts_allowed:
                    break
                self._sleep_backoff(attempts - 1, _remaining_deadline())
                continue
            if status_code >= 400:
                raise _fail("invalid_request",
                            f"模型请求被拒绝（HTTP {status_code}），已停止")

            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                raise _fail("invalid_response", "模型响应不是合法 JSON") from None
            choices = body.get("choices") or []
            if not choices:
                raise _fail("invalid_response", "模型响应缺少 choices")
            message = choices[0].get("message") or {}
            content = message.get("content")
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                raise _fail("output_truncated", "模型输出因长度被截断，弃用该结果")
            if not isinstance(content, str) or not content.strip():
                raise _fail("invalid_response", "模型响应内容为空")
            if max_output_chars is not None and len(content) > max_output_chars:
                raise _fail("output_truncated", "模型输出超过字符预算，弃用该结果")
            try:
                parsed = self._parse_content(content)
            except LLMRequestError as exc:
                exc.attempts = attempts
                exc.usage = known_usage
                raise
            usage = LLMUsage(
                model_reported=body.get("model"),
                model_configured=self._model,
                prompt_tokens=(body.get("usage") or {}).get("prompt_tokens"),
                completion_tokens=(body.get("usage") or {}).get("completion_tokens"),
                calls=1, http_attempts=attempts,
                elapsed_ms=int((self._clock() - started) * 1000),
            )
            return LLMStructuredOutput(result=parsed, usage=usage)

        if last_error is not None:
            last_error.attempts = attempts
            last_error.usage = known_usage
            raise last_error
        raise LLMRequestError("server_error", "模型调用失败", attempts=attempts,
                              usage=known_usage)

    # -- 内部 -----------------------------------------------------------------

    def _request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        read_timeout: float,
        deadline_monotonic: float | None,
    ) -> tuple[int, str, dict[str, Any] | None, str | None]:
        """有界流式请求：所有状态都限读取；期限覆盖每个分块。

        返回 (状态码, 响应体文本, usage 原始对象, 服务端模型标识)。
        200 响应超限/超期抛 LLMRequestError；错误响应体超限只丢弃正文。
        """
        timeout = httpx.Timeout(read_timeout, connect=min(5.0, read_timeout))
        try:
            with (
                httpx.Client(timeout=timeout, transport=self._transport) as client,
                client.stream("POST", url, headers=headers, json=payload) as response,
            ):
                status_code = response.status_code
                content_length = response.headers.get("content-length")
                if status_code == 200 and content_length:
                    try:
                        if int(content_length) > _MAX_RESPONSE_BYTES:
                            raise LLMRequestError(
                                "invalid_response", "模型响应超过大小上限")
                    except ValueError:
                        pass  # 非法 Content-Length：忽略声明，靠累计字节上限
                limit = (_MAX_RESPONSE_BYTES if status_code == 200
                         else _ERROR_BODY_SNIFF_BYTES)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        if status_code == 200:
                            raise LLMRequestError(
                                "invalid_response", "模型响应超过大小上限")
                        break  # 错误响应体超限：丢弃正文，只需状态码
                    chunks.append(chunk)
                    if (deadline_monotonic is not None
                            and self._clock() >= deadline_monotonic):
                        raise LLMRequestError(
                            "deadline_exceeded", "模型响应读取超过运行期限，已停止")
                body_text = b"".join(chunks).decode("utf-8", errors="replace")
                usage_raw: dict[str, Any] | None = None
                model_reported: str | None = None
                if status_code == 200:
                    try:
                        parsed = json.loads(body_text)
                        usage_raw = parsed.get("usage")
                        model_reported = parsed.get("model")
                    except json.JSONDecodeError:
                        usage_raw = None
                return status_code, body_text, usage_raw, model_reported
        except httpx.TimeoutException:
            raise _Recoverable(
                LLMRequestError("timeout", "模型请求超时，已按重试策略处理")) from None
        except httpx.TransportError:
            raise _Recoverable(
                LLMRequestError("network", "模型网络错误，已按重试策略处理")) from None

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
            raise LLMRequestError("invalid_response", "模型输出不是 JSON 对象") from None
        return parsed

    def _sleep_backoff(self, attempt: int, remaining_deadline: float | None) -> None:
        backoff = min(0.5 * (2**attempt), 4.0)
        if remaining_deadline is not None:
            if remaining_deadline <= 0:
                return
            backoff = min(backoff, remaining_deadline)
        time.sleep(backoff)


class _Recoverable(Exception):
    """可恢复错误的内部中继（超时/网络），转换为 LLMRequestError 参与重试。"""

    def __init__(self, error: LLMRequestError) -> None:
        super().__init__()
        self.error = error
