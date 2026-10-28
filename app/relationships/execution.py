"""M3 验证执行控制：进程内共享并发额度与 SQL 语句计数（修复轮）。

- ValidationSlotLimiter：验证运行的并发额度。**进程内共享**（同一 Python
  进程的所有 RelationshipService 实例共用同一把按上限值复用的信号量），
  覆盖打开源库连接、结构核对与数据探查全过程；等待额度受运行期限约束。
  作用范围声明：**单进程**。多进程部署（多个 uvicorn worker/多个服务
  实例进程）各自持有独立额度，不构成跨进程全局保证；跨进程仲裁需外部
  协调服务（本轮明确不引入，见 docs/configuration.md 限制说明）。
- StatementCounter：线程安全的 SQL 语句计数器。适配器通过 SQLAlchemy
  `before_cursor_execute` 事件逐语句计数（一次包含多条 SQL 的方法调用
  按实际语句数计）；语句发出即计数，失败尝试同样计入。
"""

import threading
from typing import ClassVar

from app.core.exceptions import ValidationStatementBudgetExceeded


class StatementCounter:
    """线程安全 SQL 语句计数（含失败尝试；按实际执行语句数，非方法调用数）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def increment(self, times: int = 1) -> None:
        with self._lock:
            self._count += times

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


class StatementBudget(StatementCounter):
    """逐语句硬预算：`allow_one()` 在每条 SQL 发出前**原子检查并消耗**额度。

    - 额度不足时抛 ValidationStatementBudgetExceeded（固定安全文案，不含
      SQL/参数/底层异常）——语句在执行前被阻止，不到达数据库；
    - `count`＝已获准并实际发出的语句数（**含发出后失败的语句**：不退回、
      不重复计数）；`rejected`＝在预算检查处被拒绝、未发出的语句数；
    - 操作组"估计下界准入"只是减少浪费的优化，不能替代本硬限制；
    - 未注入本对象的历史调用（M1/M2 扫描与画像）行为不变。
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        if limit < 1:
            raise ValueError("语句预算必须 ≥1")
        self._limit = limit
        self._rejected = 0

    def allow_one(self) -> None:
        with self._lock:
            if self._count >= self._limit:
                self._rejected += 1
                raise ValidationStatementBudgetExceeded(self._limit, self._count)
            self._count += 1

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def rejected(self) -> int:
        with self._lock:
            return self._rejected


class ValidationSlotLimiter:
    """进程内共享的验证并发额度（按上限值复用同一信号量）。

    acquire(limit, timeout) 在期限内向共享信号量申请一个额度；
    超时返回 False（调用方按 deadline/slot_timeout 收尾，不停留 running）。
    release 必须且只能在成功 acquire 之后调用（BoundedSemaphore 防重复释放）。
    """

    _semaphores: ClassVar[dict[int, threading.BoundedSemaphore]] = {}
    _registry_lock = threading.Lock()

    @classmethod
    def acquire(cls, limit: int, timeout: float) -> bool:
        if timeout <= 0:
            return False
        with cls._registry_lock:
            semaphore = cls._semaphores.get(limit)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(limit)
                cls._semaphores[limit] = semaphore
        return semaphore.acquire(timeout=timeout)

    @classmethod
    def release(cls, limit: int) -> None:
        with cls._registry_lock:
            semaphore = cls._semaphores[limit]
        semaphore.release()
