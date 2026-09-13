# -*- coding: utf-8 -*-
"""token 归属与并发 —— **Subagent能并发之前必须成立的两条**。

早先的设计 开头「🔴 天然会错的两条」：

  ③ `turn_tokens` 的归属会算错 —— 只要后台任务/Subagent**活过发起它的那一轮**。
     旧实现是「打点 + 取差值」，而差值隐含假设「这段时间里只有它自己在花」。
     📌 并发一引入这个假设就没了，**而它不会报错**。

  ④ `usage_tracker` 没有并发保护。`self._x += n` 是读-改-写，并发会丢更新。
     ⭐ 对照：`RecentSystemEvents` / `HealthRegistry` 都带 `RLock`，唯独它没有。

⚠️⚠️ 本套件**必须真的跨 `asyncio.create_task` 边界**跑一遍：
   归属靠的是 `ContextVar` 在建任务那一刻被复制 ——
   📌 这件事**只有真的建一个任务才验得到**，看源码里有没有 `ContextVar`
      证明不了它被正确地 set 在了那个协程里。
   （本轮已经被"只验结构"坑过两次：`bridge.get_store` / `bridge.recall`。）

用法：
  py -3.10 tests\\t_a4_usage_attribution.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _tracker(tmp: pathlib.Path):
    """⚠️ 换掉落盘路径 ——：测试绝不许碰生产的 usage.json。"""
    import core.usage as U
    U._USAGE_FILE = tmp / "usage.json"
    U._CONFIG_FILE = tmp / "usage_config.json"
    U._DATA_DIR = tmp
    t = U.UsageTracker()
    return U, t


def t_attribution_survives_a_later_turn() -> None:
    print("\n[1] ⭐⭐⭐ 后台任务活过发起它的那一轮，账**仍然算在它那一轮**")
    import tempfile
    U, t = _tracker(pathlib.Path(tempfile.mkdtemp(prefix="nanoa4_")))

    async def _run():
        # ── turn A：起一个"后台任务"，它慢慢跑 ──
        tid_a = t.begin_turn()
        _late = asyncio.Event()

        async def _bg():
            # ⚠️ 关键：这个协程是在 turn A 的上下文里 `create_task` 的，
            #    所以它**继承**了 A 的 turn id。
            await _late.wait()
            t.record(100, 50, "anthropic/claude-haiku-4.5")

        job = asyncio.create_task(_bg())
        await asyncio.sleep(0)

        # ── turn B：用户又说了一句，重新打点 ──
        tid_b = t.begin_turn()
        t.record(10, 5, "anthropic/claude-haiku-4.5")     # B 自己花的

        # ── 后台任务此刻才回来 ──
        _late.set()
        await job
        return tid_a, tid_b

    tid_a, tid_b = asyncio.run(_run())

    check(t.turn_tokens(tid_a) == 150,
          "⭐⭐⭐ 后台那 150 记在 **turn A**（发起它的那一轮）—— "
          "🔴 旧的差值实现会把它算进 turn B，**记到不相干的一轮头上且不报错**",
          str(t.turn_tokens(tid_a)))
    check(t.turn_tokens(tid_b) == 15,
          "⭐⭐⭐ turn B 只有它自己的 15 —— "
          "📌 归属跟着「谁发起的」走，不跟着「什么时候回来的」走",
          str(t.turn_tokens(tid_b)))
    _si, _so = t.session_tokens()
    check((_si, _so) == (110, 55),
          "⚠️ session 总量两笔都算（Subagent 的 token 天然并入 main agent）—— "
          "⭐ 这一条「天然成立」，要做的不是实现它，是**别破坏它**",
          f"{_si}/{_so}")


def t_no_turn_context_is_not_forced_into_one() -> None:
    print("\n[2] ⭐⭐ 不属于任何一轮的调用，**不许硬塞给最近一轮**")
    import tempfile
    U, t = _tracker(pathlib.Path(tempfile.mkdtemp(prefix="nanoa4b_")))
    tid = t.begin_turn()
    check(t.turn_tokens(tid) == 0, "前置：新一轮从 0 开始")

    done = {}

    def _outside():
        # 新线程 = 全新上下文，没有 turn id
        t.record(999, 999, "anthropic/claude-haiku-4.5")
        done["ok"] = True

    th = threading.Thread(target=_outside)
    th.start(); th.join()
    check(done.get("ok"), "前置：那次记账真的发生了")
    check(t.turn_tokens(tid) == 0,
          "⭐⭐⭐ 它**没有**被算进那一轮 —— "
          "🔴 硬塞的话就是把「不属于任何一轮」伪装成「属于这一轮」，"
          "而 UI 上看起来完全正常")
    check(t.session_tokens() == (999, 999),
          "⚠️ 但 session 总量照记（钱确实花了）—— "
          "📌 「算不清算谁的」和「没发生」是两件事")


def t_concurrent_records_do_not_lose_updates() -> None:
    print("\n[3] ⭐⭐⭐ 并发记账**不丢更新**（`+=` 是读-改-写）")
    import tempfile
    U, t = _tracker(pathlib.Path(tempfile.mkdtemp(prefix="nanoa4c_")))
    check(hasattr(t, "_lock") and isinstance(t._lock, type(threading.RLock())),
          "⭐⭐ 有 `RLock` —— ⭐ 对照 `RecentSystemEvents` / `HealthRegistry` "
          "早就有；📌 又一次「正确做法已在代码里，却没推广到同类场景」")

    N, PER = 16, 40

    def _hammer():
        for _ in range(PER):
            t.record(1, 1, "anthropic/claude-haiku-4.5")

    ths = [threading.Thread(target=_hammer) for _ in range(N)]
    for x in ths:
        x.start()
    for x in ths:
        x.join()
    _si, _so = t.session_tokens()
    check((_si, _so) == (N * PER, N * PER),
          f"⭐⭐⭐ 内存计数器一笔不少（{N}×{PER}）", f"{_si}/{_so}")

    # 🔴 落盘那份更重要：丢的是**钱**
    _d = t._load_usage()
    check(_d.get("input_tokens") == N * PER and _d.get("output_tokens") == N * PER,
          "⭐⭐⭐ **usage.json 也一笔不少** —— "
          "🔴 `_load → += → _save` 那一段比内存计数器更危险："
          "内存丢的是显示，落盘丢的是账",
          f"{_d.get('input_tokens')}/{_d.get('output_tokens')}")


def t_turn_table_is_bounded() -> None:
    print("\n[4] ⭐ 轮次表有上限（否则长会话会无界增长）")
    import tempfile
    U, t = _tracker(pathlib.Path(tempfile.mkdtemp(prefix="nanoa4d_")))
    ids = [t.begin_turn() for _ in range(U._TURN_KEEP + 30)]
    check(len(t._turn_acc) <= U._TURN_KEEP,
          f"⭐⭐ 只保留最近 {U._TURN_KEEP} 轮 —— "
          "📌 任何列表/配额落地时必须回答「谁来把它降下去」",
          str(len(t._turn_acc)))
    check(t.turn_tokens(ids[-1]) == 0 and ids[-1] in t._turn_acc,
          "⚠️ 而**当前这一轮一定还在**（掉的是最老的）")


def main() -> int:
    t_attribution_survives_a_later_turn()
    t_no_turn_context_is_not_forced_into_one()
    t_concurrent_records_do_not_lose_updates()
    t_turn_table_is_bounded()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
