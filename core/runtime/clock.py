# core/runtime/clock.py
"""
Runtime Kernel 的统一时钟。

═══ 为什么时钟必须可注入 ═══

Runtime 的验收标准里有一条是"崩溃恢复测试：在每个状态转移点强杀进程，重启后 Reconcile
结果符合预期表"。而 Reconcile 的判据全是时间比较（lease 有没有过期、deadline 有没有到、
orphan 该不该回收）。如果时钟是 `time.time()` 写死的，测这些就只能靠 `sleep`——
一个 lease 超时测试要真等 30 秒，十几个用例就是几分钟，而且不稳定（CI 慢一点就假失败）。

注入 FakeClock 之后这些测试是**瞬时且确定**的：`clock.advance(31)` 就到点了。

═══ 四类时间必须分开 ═══

Kernel 只提供一个 `now()`，但**语义上有四类时间，不许共用一个 expires_at**：

  1. 业务 deadline    过时后任务语义已无效（"10 分钟内确认"）
  2. Lease timeout    执行者可能已经死了（OS activity 长时间没心跳）
  3. Orphan timeout   后台 worker 已不存在，但记录仍 active
  4. Retention TTL    终态记录保留多久后才物理删除

"30 分钟没确认"与"后台进程已经死掉"共用一个字段，是那条"不死挂起"的成因之一。
所以各实体自己声明这四个策略，Reconciler 分别判，时钟只负责"现在几点"。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockProtocol(Protocol):
    """Kernel 只依赖这一个方法。故意不提供 sleep —— 等待是调用方的事，
    时钟只回答"现在几点"。这样 FakeClock 不需要实现任何异步语义。"""

    def now(self) -> float:
        ...


class SystemClock:
    """生产用。Unix 时间戳（秒，float）。

    ⚠️ 刻意用 `time.time()` 而不是 `time.monotonic()`：这些时间戳要**落盘**并跨进程比较，
    单调时钟的原点每次开机都不同，落盘后毫无意义。
    代价是用户改系统时间会让判据错乱——但那属于"用户主动搞乱环境"，
    而"跨重启还能比较"是硬需求，取后者。
    """

    __slots__ = ()

    def now(self) -> float:
        return time.time()


class FakeClock:
    """测试用。手动推进，绝不自己走。

    线程安全：崩溃恢复测试会在子进程/多线程里读它，用锁保证读到的是完整值。
    """

    __slots__ = ("_t", "_lock")

    def __init__(self, start: float = 1_800_000_000.0):
        # 默认起点用一个"看起来像真时间戳但明显是假的"值，
        # 日志里一眼能认出这是测试环境，不会被误当成真实记录。
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> float:
        """向前推进。返回推进后的时刻。"""
        with self._lock:
            self._t += float(seconds)
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = float(t)


# 进程默认时钟。Kernel 构造时可覆盖；测试里直接传 FakeClock，不要改这个全局。
SYSTEM_CLOCK = SystemClock()
