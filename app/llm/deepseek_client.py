"""DeepSeek 客户端（OpenAI 兼容 chat/completions，httpx 实现；M2 修复轮加固）。

- 一次 generate_structured 是一次“逻辑调用”；其内部的传输重试与上层结构修复
  共享同一预算：max_attempts 限定该次逻辑调用的总 HTTP 尝试数；
- 只重试可恢复错误（429/5xx/网络/超时）；认证与非法请求立即失败；
  最后一次尝试失败后不做无意义退避；
- 响应大小限制在流式读取中执行（累计字节超限立即停止），而非读完后检查；
- usage 如实记录：model_reported 只填服务端返回值（缺失为 None，不把配置
  模型冒充 reported）；token 取该次逻辑调用内最后一个有 usage 的响应；
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
        max_attempts: int | None = None,
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
        started = time.perf_counter()
        attempts = 0
        last_error: LLMRequestError | None = None

        while attempts < attempts_allowed:
            attempts += 1
            try:
                status_code, _content_length, body_text = self._request(url, headers, payload)
            except _Recoverable as exc:
                last_error = exc.error
                if attempts >= attempts_allowed:
                    break  # 最后一次失败不再退避
                self._sleep_backoff(attempts - 1)
                continue
            except LLMRequestError:
                raise

            if status_code in (401, 403):
                raise LLMRequestError(
                    "auth_failure", "模型认证失败（auth_failure），已停止重试",
                    attempts=attempts)
            if status_code in _RECOVERABLE_STATUS:
                last_error = LLMRequestError(
                    "rate_limited" if status_code == 429 else "server_error",
                    "模型服务暂时不可用，已按重试策略处理")
                if attempts >= attempts_allowed:
                    break
                self._sleep_backoff(attempts - 1)
                continue
            if status_code >= 400:
                raise LLMRequestError(
                    "invalid_request",
                    f"模型请求被拒绝（HTTP {status_code}），已停止", attempts=attempts)

            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                raise LLMRequestError(
                    "invalid_response", "模型响应不是合法 JSON", attempts=attempts) from None
            choices = body.get("choices") or []
            if not choices:
                raise LLMRequestError(
                    "invalid_response", "模型响应缺少 choices", attempts=attempts)
            message = choices[0].get("message") or {}
            content = message.get("content")
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                raise LLMRequestError(
                    "output_truncated", "模型输出因长度被截断，弃用该结果",
                    attempts=attempts)
            if not isinstance(content, str) or not content.strip():
                raise LLMRequestError(
                    "invalid_response", "模型响应内容为空", attempts=attempts)
            if max_output_chars is not None and len(content) > max_output_chars:
                raise LLMRequestError(
                    "output_truncated", "模型输出超过字符预算，弃用该结果",
                    attempts=attempts)

            parsed = self._parse_content(content)
            usage = LLMUsage(
                model_reported=body.get("model"),
                model_configured=self._model,
                prompt_tokens=(body.get("usage") or {}).get("prompt_tokens"),
                completion_tokens=(body.get("usage") or {}).get("completion_tokens"),
                calls=1,
                http_attempts=attempts,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return LLMStructuredOutput(result=parsed, usage=usage)

        if last_error is not None:
            last_error.attempts = attempts
            raise last_error
        raise LLMRequestError("server_error", "模型调用失败", attempts=attempts)

    # -- 内部 -----------------------------------------------------------------

    def _request(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, str | None, str]:
        """流式发起请求并在连接存续期内完成读取（带累计字节上限）。"""
        timeout = httpx.Timeout(self._timeout_seconds, connect=5.0)
        try:
            with (
                httpx.Client(timeout=timeout, transport=self._transport) as client,
                client.stream("POST", url, headers=headers, json=payload) as response,
            ):
                    status_code = response.status_code
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
                        raise LLMRequestError(
                            "invalid_response", "模型响应超过大小上限", attempts=1)
                    chunks: list[bytes] = []
                    total = 0
                    body_text = ""
                    if status_code == 200:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > _MAX_RESPONSE_BYTES:
                                # 超限立即停止读取，不先完整加载
                                raise LLMRequestError(
                                    "invalid_response", "模型响应超过大小上限", attempts=1)
                            chunks.append(chunk)
                        body_text = b"".join(chunks).decode("utf-8", errors="replace")
                    else:
                        body_text = response.read().decode("utf-8", errors="replace")
                    return status_code, content_length, body_text
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

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(0.5 * (2**attempt), 4.0))


class _Recoverable(Exception):
    """可恢复错误的内部中继（超时/网络），转换为 LLMRequestError 参与重试。"""

    def __init__(self, error: LLMRequestError) -> None:
        super().__init__()
        self.error = error
