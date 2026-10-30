"""M5 ES 发布适配器（httpx；默认关闭，仅应用命名空间）。

实现要点：
- 显式 mapping（不依赖动态推断）；版本索引 + 应用专属别名原子切换；
- 文档 ID=grain 结构化编码（无歧义）；NULL/重复 grain 键由调用方阻断；
- 分批 bulk；逐项检查 bulk 结果（HTTP 200 ≠ 全部文档成功）；可重试项
  （429/超时类）有限重试；mapping 错误等永久失败抛错阻断；
- 金额 Decimal → 缩放字符串（不无提示转 float 丢精度）；二进制拒绝；
- 不删除旧索引、不用通配符删除；错误信息不含凭证/业务原文。
"""

import base64
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from app.core.exceptions import DataSourceConnectionError, ExternalOutcomeUnknownError


class ESPublishError(DataSourceConnectionError, ValueError):
    """ES 发布错误：既是连接类领域异常（reason 脱敏）也是 ValueError
    （服务层发布失败统一按 ValueError 对外）。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason, message)


class ESHttpPublisher:
    """真实 ES 发布器（httpx；配置默认关闭）。

    请求超时（最后修复轮）：
    - 未指定 timeout_ms 的请求使用构造时配置的有限超时——**不得把
      timeout=None 传给 httpx**（那是关闭超时，不是用默认值）；
    - timeout_ms<=0 在发出请求前拒绝（invalid_timeout，不是无限等待）；
    - 含多个 HTTP 请求的操作（HEAD+PUT 创建、bulk 重试）在入口计算一次
      绝对截止时间，每次请求前重算剩余时间作为单请求超时（不超过配置
      上限），耗尽即停止、不再启动下一次请求；响应返回后同样检查，
      超期结果不得使发布继续成功。
    """

    RETRYABLE_STATUSES: ClassVar[set[int]] = {429, 502, 503, 504}
    MAX_RETRIES: ClassVar[int] = 3

    def __init__(
        self, hosts: list[str], username: str = "", password: SecretStr | None = None,
        api_key: SecretStr | None = None, timeout_seconds: int = 30,
        verify_certs: bool = True,
        clock: Callable[[], float] | None = None,
    ):
        headers = {"Content-Type": "application/json"}
        if api_key and api_key.get_secret_value():
            headers["Authorization"] = \
                f"ApiKey {api_key.get_secret_value()}"
        elif username and password and password.get_secret_value():
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"{username}:{password.get_secret_value()}".encode()
            ).decode()
        self._default_timeout = float(timeout_seconds)
        # 可注入时钟（测试确定性推进期限；默认单调时钟）
        self._monotonic = clock or time.monotonic
        self._client = httpx.Client(
            base_url=hosts[0] if hosts else "http://127.0.0.1:9200",
            headers=headers, timeout=self._default_timeout,
            verify=verify_certs)

    # -- 期限与单请求超时 -------------------------------------------------------

    def _deadline_from(self, timeout_ms: int | None) -> float | None:
        """操作入口：校验 timeout_ms 并计算绝对截止时间（None＝无操作期限，
        单请求仍用配置默认超时）。"""
        if timeout_ms is None:
            return None
        if timeout_ms <= 0:
            raise ESPublishError(
                "invalid_timeout",
                "ES 操作 timeout_ms≤0：拒绝在发出请求前（不是无限等待）")
        return self._monotonic() + timeout_ms / 1000.0

    def _request_timeout_before(self, deadline: float | None) -> float:
        """单请求超时：不超过剩余期限与配置上限；未设期限用配置默认。
        剩余耗尽在发出请求前拒绝（绝不返回 None——httpx 中是关闭超时）。"""
        if deadline is None:
            return self._default_timeout
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise ESPublishError(
                "deadline_exhausted",
                "ES 操作期限耗尽：不启动下一次请求")
        return min(self._default_timeout, remaining)

    def _check_deadline(self, deadline: float | None) -> None:
        """响应返回后的检查：超期结果不得使发布继续成功。"""
        if deadline is not None and self._monotonic() >= deadline:
            raise ESPublishError(
                "deadline_exhausted",
                "ES 操作期限耗尽：响应结果不用于发布")

    # -- 只读检查 ---------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        try:
            response = self._client.get("/")
            response.raise_for_status()
            info = response.json()
            return {"ok": True, "version": info.get("version", {}).get(
                "number"), "cluster_name": info.get("cluster_name")}
        except httpx.HTTPError:
            raise ESPublishError(
                "es_unreachable", "ES 连接失败（es_unreachable）；详情已抑制"
            ) from None

    def close(self) -> None:
        self._client.close()

    # -- 索引/别名 --------------------------------------------------------------

    def create_version_index(
        self, index_name: str, mapping: dict[str, Any],
        *, timeout_ms: int | None = None,
    ) -> None:
        """创建版本索引。结果分类（创建超期定点修复轮，按 PUT 是否已
        发出区分）：
        - PUT 未发出时超期（HEAD 前后）：deadline_exhausted——只读 HEAD
          至多发出过，不应声称已创建；
        - PUT 已发出后的超期/响应丢失/连接中断/服务端 5xx：
          ExternalOutcomeUnknownError——服务端可能已创建，"方法没有正常
          返回"≠"服务端没有执行"；
        - PUT 正常返回但返回后期限检查失败：同样 ExternalOutcomeUnknown
          Error（索引可能已建成，超期结果不得当确定失败释放占用）；
        - 明确 4xx 拒绝 → index_create_rejected（确定未创建）；
        - 已存在（HEAD 200）→ index_exists（确定状态）。
        """
        deadline = self._deadline_from(timeout_ms)
        put_issued = False  # 局部标记：创建 PUT 是否已开始发出
        try:
            response = self._client.head(
                f"/{index_name}",
                timeout=self._request_timeout_before(deadline))
            self._check_deadline(deadline)
            if response.status_code == 200:
                raise ESPublishError(
                    "index_exists",
                    f"版本索引已存在：{index_name}（不覆盖；不删除）")
            body = {"mappings": mapping, "settings": {
                "number_of_shards": 1, "number_of_replicas": 0}}
            put_timeout = self._request_timeout_before(deadline)
            put_issued = True
            response = self._client.put(
                f"/{index_name}", content=json.dumps(body),
                timeout=put_timeout)
            if put_issued and deadline is not None \
                    and self._monotonic() >= deadline:
                # PUT 已发出（服务端可能已创建）：超期不得当确定失败
                raise ExternalOutcomeUnknownError(
                    "index_create_unknown",
                    "ES 索引创建请求已发出但操作期限耗尽：结果不明；"
                    "详情已抑制")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise ExternalOutcomeUnknownError(
                    "index_create_unknown",
                    "ES 索引创建结果不明（服务端 5xx）；详情已抑制") from None
            raise ESPublishError(
                "index_create_rejected",
                "ES 索引创建被拒绝（index_create_rejected）；详情已抑制"
            ) from None
        except httpx.TransportError:
            # 请求已发出但响应丢失/超时/连接中断：服务端可能已创建
            raise ExternalOutcomeUnknownError(
                "index_create_unknown",
                "ES 索引创建结果不明（响应丢失/超时/连接中断）") from None
        except httpx.HTTPError:
            raise ESPublishError(
                "index_create_failed",
                "ES 索引创建失败（index_create_failed）；详情已抑制") from None

    def bulk_index(
        self, index_name: str, documents: list[dict[str, Any]],
        *, timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """documents: [{"_id": grain 文档 ID, "_source": {...}}]
        重试每次重算剩余期限；期限耗尽不再启动下一次请求。"""
        deadline = self._deadline_from(timeout_ms)
        lines: list[str] = []
        for doc in documents:
            lines.append(json.dumps(
                {"index": {"_index": index_name, "_id": doc["_id"]}},
                ensure_ascii=False))
            lines.append(json.dumps(doc["_source"], ensure_ascii=False,
                                    default=self._serialize_value))
        body = "\n".join(lines) + "\n"
        retried = 0
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._client.post(
                    "/_bulk", content=body,
                    headers={"Content-Type": "application/x-ndjson"},
                    timeout=self._request_timeout_before(deadline))
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError:
                if attempt == self.MAX_RETRIES - 1:
                    raise ESPublishError(
                        "bulk_failed",
                        "ES bulk 请求失败（bulk_failed）；详情已抑制") from None
                retried += 1
                continue
            self._check_deadline(deadline)  # 响应返回后：超期结果不采用
            if not payload.get("errors"):
                return {"indexed": len(documents), "retried": retried,
                        "permanent_failures": []}
            failures: list[dict[str, Any]] = []
            for item in payload.get("items", []):
                result = item.get("index", {})
                error = result.get("error") or {}
                status = result.get("status")
                if error and status not in self.RETRYABLE_STATUSES:
                    # 永久失败（mapping 错误等）：阻断发布
                    failures.append({
                        "status": status,
                        "type": error.get("type"),
                        # 不回传业务文档内容
                        "reason_category": error.get("type"),
                    })
            if failures:
                raise ESPublishError(
                    "bulk_permanent_failure",
                    f"ES bulk 存在永久失败 {len(failures)} 项"
                    f"（类型={failures[0].get('type')}）；发布阻断")
            retried += 1
            # 全部为可重试项：整体重试（期限耗尽由 _request_timeout_before
            # 在下一次请求前拒绝）
        raise ESPublishError(
            "bulk_retry_exhausted", "ES bulk 可重试失败重试耗尽；发布阻断")

    def switch_alias(
        self, alias_name: str, index_name: str,
        previous_index: str | None = None,
        *, timeout_ms: int | None = None,
    ) -> None:
        deadline = self._deadline_from(timeout_ms)
        actions = [{"add": {"index": index_name, "alias": alias_name}}]
        if previous_index:
            actions.insert(0, {"remove": {"index": previous_index,
                                          "alias": alias_name}})
        try:
            response = self._client.post(
                "/_aliases", content=json.dumps({"actions": actions}),
                timeout=self._request_timeout_before(deadline))
            self._check_deadline(deadline)
            response.raise_for_status()
        except httpx.HTTPError:
            raise ESPublishError(
                "alias_switch_failed",
                "ES 别名切换失败（alias_switch_failed）；详情已抑制") from None

    def get_alias_index(self, alias_name: str,
                        *, timeout_ms: int | None = None) -> str | None:
        """别名当前指向的索引（不存在返回 None；供恢复核对）。"""
        deadline = self._deadline_from(timeout_ms)
        try:
            response = self._client.get(
                f"/_alias/{alias_name}",
                timeout=self._request_timeout_before(deadline))
            self._check_deadline(deadline)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            indexes = list(response.json().keys())
            return indexes[0] if indexes else None
        except httpx.HTTPError:
            raise ESPublishError(
                "alias_lookup_failed",
                "ES 别名查询失败（alias_lookup_failed）；详情已抑制") from None

    def count(self, index_name: str, *, timeout_ms: int | None = None) -> int:
        """索引文档数；**索引明确不存在**抛 index_not_found（与网络/服务
        错误区分——恢复逻辑只有确定不存在才能走对象缺失分支）。"""
        deadline = self._deadline_from(timeout_ms)
        try:
            response = self._client.get(
                f"/{index_name}/_count",
                timeout=self._request_timeout_before(deadline))
            self._check_deadline(deadline)
            if response.status_code == 404:
                raise ESPublishError(
                    "index_not_found", f"ES 索引不存在：{index_name}")
            response.raise_for_status()
            return int(response.json().get("count", 0))
        except httpx.HTTPError:
            raise ESPublishError(
                "count_failed",
                "ES 文档计数失败（count_failed）；详情已抑制") from None

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """类型转换策略：Decimal→字符串；二进制拒绝。"""
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise ESPublishError(
                "unsupported_binary",
                "二进制字段不支持导出（unsupported_binary）")
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)


class FakeESPublisher(ESHttpPublisher):
    """离线测试替身：同端口语义，可注入延迟/部分失败/映射错误。

    与真实发布器的差异仅在传输层；行为语义（永久失败阻断、可重试有限
    重试、计数核对、别名记录）保持一致。
    """

    def __init__(self, *, bulk_failure_items: int = 0,
                 retryable_failure: bool = False,
                 mapping_error: bool = False, count_override: int | None = None,
                 fail_alias_switch: bool = False,
                 count_error: bool = False):
        self.indices: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, list[dict[str, Any]]] = {}
        self.aliases: dict[str, str] = {}
        self.alias_history: list[tuple[str, str | None]] = []
        self.bulk_calls = 0
        self.request_timeouts: list[int | None] = []
        self._bulk_failure_items = bulk_failure_items
        self._retryable_failure = retryable_failure
        self._mapping_error = mapping_error
        self._count_override = count_override
        self._fail_alias_switch = fail_alias_switch
        self._count_error = count_error

    def ping(self) -> dict[str, Any]:
        return {"ok": True, "version": "fake-8.0.0", "cluster_name": "fake"}

    def close(self) -> None:
        return None

    def create_version_index(self, index_name, mapping, *, timeout_ms=None):
        self.request_timeouts.append(timeout_ms)
        if index_name in self.indices:
            raise ESPublishError(
                "index_exists",
                f"版本索引已存在：{index_name}（不覆盖；不删除）")
        self.indices[index_name] = mapping
        self.documents[index_name] = []

    def bulk_index(self, index_name, documents, *, timeout_ms=None):
        self.bulk_calls += 1
        self.request_timeouts.append(timeout_ms)
        if self._mapping_error:
            raise ESPublishError(
                "bulk_permanent_failure",
                "ES bulk 存在永久失败 1 项（类型=mapper_parsing_exception）；"
                "发布阻断")
        if self._retryable_failure and self.bulk_calls <= 1:
            # 首次可重试失败：真实实现会整体重试；替身直接二次成功
            self.bulk_calls += 1
        if self._bulk_failure_items and documents:
            raise ESPublishError(
                "bulk_permanent_failure",
                f"ES bulk 存在永久失败 {self._bulk_failure_items} 项"
                f"（类型=document_mapping_failure）；发布阻断")
        seen: set[str] = set()
        for doc in documents:
            if doc["_id"] in seen:
                raise ESPublishError(
                    "duplicate_document_id",
                    "重复 grain 文档 ID（先阻断，不静默覆盖）")
            seen.add(doc["_id"])
        self.documents.setdefault(index_name, []).extend(documents)
        return {"indexed": len(documents), "retried": 1
                if self._retryable_failure else 0, "permanent_failures": []}

    def switch_alias(self, alias_name, index_name, previous_index=None,
                     *, timeout_ms=None):
        self.request_timeouts.append(timeout_ms)
        if self._fail_alias_switch:
            raise ESPublishError(
                "alias_switch_failed",
                "ES 别名切换失败（alias_switch_failed）；详情已抑制")
        self.aliases[alias_name] = index_name
        self.alias_history.append((alias_name, previous_index))

    def get_alias_index(self, alias_name, *, timeout_ms=None):
        self.request_timeouts.append(timeout_ms)
        return self.aliases.get(alias_name)

    def count(self, index_name, *, timeout_ms=None):
        self.request_timeouts.append(timeout_ms)
        if index_name not in self.indices:
            # 与真实实现一致：索引明确不存在 ≠ 网络错误
            raise ESPublishError(
                "index_not_found", f"ES 索引不存在：{index_name}")
        if self._count_error:
            raise ESPublishError(
                "count_failed",
                "ES 文档计数失败（count_failed）；详情已抑制")
        if self._count_override is not None:
            return self._count_override
        return len(self.documents.get(index_name, []))
