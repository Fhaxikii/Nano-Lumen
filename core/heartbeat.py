# -*- coding: utf-8 -*-
"""后端周期任务：在 asyncio 事件循环上按固定间隔调用登记的函数，不依赖界面框架的定时器。

- `register(name, interval, fn)`：周期任务。启动时立即跑一次，之后每隔 `interval` 秒
  （扣除本次耗时）再跑。
- `register_once(name, delay, fn)`：启动后等 `delay` 秒跑一次。
- `start()` 必须在运行中的事件循环里调用；登记要在 `start()` 之前完成，之后登记的
  会在登记时立即启动。
- 函数可以是同步或 async；抛出的异常只记日志，不中断该任务的后续周期
  （每个任务第一次出错记 WARNING，之后记 DEBUG，避免刷屏）。
- `status()` 给出每个任务的运行次数与最近一次运行时间，用来核对心跳确实在跑。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger


@dataclass
class _Job:
    name: str
    fn: Callable[[], Any]
    interval: float            # 周期任务的间隔；一次性任务为 0
    delay: float = 0.0         # 一次性任务的延迟
    once: bool = False
    runs: int = 0
    errors: int = 0
    last_run: Optional[float] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)


_jobs: dict[str, _Job] = {}
_started = False


def register(name: str, interval: float, fn: Callable[[], Any]) -> None:
    """登记周期任务（同名覆盖）。"""
    _add(_Job(name=name, fn=fn, interval=float(interval)))


def register_once(name: str, delay: float, fn: Callable[[], Any]) -> None:
    """登记启动后只跑一次的任务（同名覆盖）。"""
    _add(_Job(name=name, fn=fn, interval=0.0, delay=float(delay), once=True))


def _add(job: _Job) -> None:
    old = _jobs.get(job.name)
    if old is not None and old.task is not None:
        old.task.cancel()
    _jobs[job.name] = job
    if _started:
        job.task = asyncio.get_running_loop().create_task(_run(job))


async def _call(job: _Job) -> None:
    try:
        res = job.fn()
        if inspect.isawaitable(res):
            await res
    except asyncio.CancelledError:
        raise
    except Exception as e:
        job.errors += 1
        (logger.warning if job.errors == 1 else logger.debug)(
            f"[Heartbeat] {job.name} 出错（第 {job.errors} 次）: {e}")
    finally:
        job.runs += 1
        job.last_run = time.time()


async def _run(job: _Job) -> None:
    try:
        if job.once:
            await asyncio.sleep(job.delay)
            await _call(job)
            return
        while True:
            t0 = time.monotonic()
            await _call(job)
            await asyncio.sleep(max(0.0, job.interval - (time.monotonic() - t0)))
    except asyncio.CancelledError:
        pass


def start() -> None:
    """在当前运行中的事件循环上启动全部已登记任务（重复调用无副作用）。"""
    global _started
    if _started:
        return
    loop = asyncio.get_running_loop()
    _started = True
    for job in _jobs.values():
        job.task = loop.create_task(_run(job))
    logger.debug(f"[Heartbeat] 已启动 {len(_jobs)} 个后端周期任务: {sorted(_jobs)}")


def stop() -> None:
    """取消全部任务（进程退出、测试用）。"""
    global _started
    for job in _jobs.values():
        if job.task is not None:
            job.task.cancel()
            job.task = None
    _started = False


def reset_for_tests() -> None:
    stop()
    _jobs.clear()


def status() -> dict[str, dict[str, Any]]:
    return {n: {"interval": j.interval, "once": j.once, "runs": j.runs,
                "errors": j.errors, "last_run": j.last_run}
            for n, j in _jobs.items()}
