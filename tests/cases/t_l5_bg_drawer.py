# -*- coding: utf-8 -*-
"""后台任务抽屉 —— `■` 终止、Finished 的范围、载体心跳。

  ① `■` 的 handler 调到后端载体表（`carriers.cancel`），并真的把在跑的载体取消
  ② Finished = **本次运行产生的**（不是"最近 N 条"），且**含**本次启动
     认定的 `INTERRUPTED_BY_RESTART`
  ③ 「本次运行」的起点取自**内核时钟**，不是模块级 `time.time()`
  ④ CANCELLED **不并进** FAILED
  ⑤ 载体心跳（推后等待记录的 `orphan_at`）读的是载体表

用法：
  py -3.10 tests\\cases\\t_l5_bg_drawer.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


BASE_T = 1_000_000.0


def _kernel(t=BASE_T):
    from core.runtime.store import RuntimeStore
    from core.runtime.clock import FakeClock
    from core.runtime.kernel import reset_kernel_for_tests
    db = pathlib.Path(tempfile.mkdtemp(prefix="nanol5_")) / "t.db"
    return reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(t)), db


def _func_src(src: str, name: str) -> str:
    """按名字取一个函数的源码（AST 定界，不靠缩进猜）。"""
    import ast as _ast
    for n in _ast.walk(_ast.parse(src)):
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == name:
            return chr(10).join(src.splitlines()[n.lineno - 1:n.end_lineno])
    raise LookupError(f"def {name} not found")


def t_cancel_has_a_ui_caller() -> None:
    print("[1] `■` 的 handler 调到后端载体表")
    src = module_text("app")
    seg = _func_src(src, "_cancel_bg_from_ui")
    check("_carriers.cancel(" in seg, "⭐⭐ `_cancel_bg_from_ui` 调用 `carriers.cancel`")
    tree = ast.parse(src)
    _fn = next((f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "_render_bg_row"), None)
    _seg = (ast.get_source_segment(src, _fn) or "") if _fn else ""
    check("_cancel_bg_from_ui" in _seg and "■" in _seg,
          "⭐⭐⭐ handler 被 `■` 的点击绑上了")


class _UiHost:
    def __init__(self):
        self.refreshed = 0
        self.notified = []

    def _refresh_tasks_panel(self):
        self.refreshed += 1


def _click_stop(h, task_id: str) -> None:
    """真的调 WebUI 上那个方法（绑到替身上），`ui.notify` 换成记录。"""
    from app import WebUI
    import app as _app

    class _Rec:
        pass
    _r = _Rec()
    _r.task_id = task_id

    class _Notify:
        @staticmethod
        def notify(*a, **k):
            h.notified.append(a[0] if a else "")
    _orig = _app.ui
    try:
        _app.ui = _Notify
        WebUI._cancel_bg_from_ui.__get__(h, _UiHost)(_r)
    finally:
        _app.ui = _orig


def t_cancel_path_really_runs() -> None:
    print("[2] 没有对应载体时如实说「不在跑了」")
    from core.runtime import carriers as C
    C._reset_for_tests()
    h = _UiHost()
    _click_stop(h, "rt_gone")
    check(bool(h.notified) and "不在跑" in str(h.notified[0]), "⭐ 提示「这个任务已经不在跑了」",
          str(h.notified))
    check(h.refreshed >= 1, "⚠️ 之后重刷面板（否则那一行会一直显示在 Running）")


def t_cancel_reaches_the_only_real_producer() -> None:
    """`■` 打到载体表里真的在跑的那一条（Subagent 的载体），并真的取消它。"""
    print("")
    print("[2b] 🔴 那颗 `■` 打到的是**真的有货的那张表**")
    import asyncio as _aio
    from core.runtime import carriers as C

    async def _drive():
        C._reset_for_tests()
        got = []

        async def _done(ref, hint):
            got.append((ref, hint))
        C.set_completion_handler(_done)

        async def _forever():
            await _aio.sleep(60)

        _t = _aio.ensure_future(_forever())
        C.start("Agent · 查点东西", _t, "agent_x", rt_task_id="rt_agent", owns_record=False)
        await _aio.sleep(0)                     # 让它真的跑起来
        h2 = _UiHost()
        _click_stop(h2, "rt_agent")
        for _ in range(20):
            await _aio.sleep(0.01)
        C._reset_for_tests()
        return h2, _t.cancelled(), got

    h2, _cancelled, got = _aio.run(_drive())
    check(_cancelled, "⭐⭐⭐ **载体真的被 cancel 了**", f"cancelled={_cancelled}")
    check(bool(h2.notified) and "已终止" in str(h2.notified[0]),
          "⭐ 告诉用户的是「已终止」", str(h2.notified))
    check(len(got) == 1 and got[0][0] == "agent_x" and "the user manually stopped this" in got[0][1],
          "⭐⭐ 等待方收到的是「用户手动停的」", str(got)[:120])


def t_finished_is_this_run() -> None:
    print("\n[3] ⭐⭐⭐ Finished = **本次运行产生的**，不是「最近 N 条」")
    import core.runtime.task as T
    k, db = _kernel(BASE_T)

    # 上一次运行留下的一条（时间戳在本次内核诞生之前）
    _old = T.create_background_job("上一次运行的任务")
    T.finish_background_job(_old, "completed")
    _n_old = len(T.finished_background_jobs(k))

    # 换一个"新进程"：新内核、时钟往后走
    from core.runtime.store import RuntimeStore
    from core.runtime.clock import FakeClock
    from core.runtime.kernel import reset_kernel_for_tests
    k2 = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T + 500))
    _fin = T.finished_background_jobs(k2)
    check(_n_old == 1 and not _fin,
          "⭐⭐⭐ 上一次运行的 Finished **不再出现** —— "
          "📌 单纯硬编码 limit 数字没有任何语义；"
          "而「这次打开 Nano 产生的」起码表达了一件事",
          f"本次={len(_fin)} / 上次={_n_old}")

    # 显式要全部历史仍然拿得到（导出/排查用）
    check(len(T.finished_background_jobs(k2, since=0)) == 1,
          "⚠️ 但 `since=0` 仍能要到全部历史 —— "
          "📌 隐藏不等于删除；这条路留给导出和排查")

    # 🔴 崩溃取证那一格：本次启动认定的 INTERRUPTED_BY_RESTART 必须还在
    _live = T.create_background_job("本次运行在跑的")
    k2.clock.advance(1.0)
    T.finish_background_job(_live, "cancelled")
    _fin2 = T.finished_background_jobs(k2)
    check(len(_fin2) == 1 and _fin2[0].terminal_reason == T.TerminalReason.CANCELLED,
          "⭐⭐ 本次运行结束的那条在列表里")
    # 🔴 CANCELLED 不许并进 FAILED
    check(_fin2[0].terminal_reason != T.TerminalReason.FAILED,
          "⭐⭐⭐ **用户主动停掉不是失败** —— "
          "🔴 归错会让模型和用户去排查一个不存在的问题")


def t_run_marker_uses_kernel_clock() -> None:
    print("\n[4] ⭐⭐ 「本次运行」的起点取自**内核时钟**，不是模块级 time.time()")
    import core.runtime.task as T
    check(not hasattr(T, "_PROCESS_START"),
          "⭐⭐⭐ `task.py` 里**没有**模块级 `_PROCESS_START` —— "
          "🔴 第一版我写的正是它，于是拿墙钟去和 FakeClock 写的 updated_at "
          "比大小，崩溃取证那条当场红了。"
          "📌 一个模块级的 `time.time()` 是一个看不见的全局依赖："
          "在已经有时钟抽象的系统里，它等于给同一件事造了第二个时间源")

    k, _ = _kernel(BASE_T)
    check(abs(k.started_at - BASE_T) < 1e-6,
          "⭐⭐ `kernel.started_at` 跟着**注入的时钟**走", str(k.started_at))

    src = module_text("core.runtime.task")
    fn = next((f for f in ast.walk(ast.parse(src))
               if isinstance(f, ast.FunctionDef)
               and f.name == "finished_background_jobs"), None)
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""
    check("kernel.started_at" in seg and "time.time()" not in seg,
          "⭐⭐ 那个函数里用的是 `kernel.started_at`，没有第二个时间源")


def t_pill_and_button_are_two_clocks() -> None:
    print("\n[5] ⭐ pill 管「向未来」/ 常驻按钮管「向过去」")
    src = module_text("app")
    tree = ast.parse(src)
    _sync = next((f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                  and f.name == "_sync_task_pill"), None)
    _seg = (ast.get_source_segment(src, _sync) or "") if _sync else ""
    check(_sync is not None and "set_visibility(False)" in _seg,
          "⭐⭐ pill 在 0 时**整个消失** —— 📌 它管的是「还有事在跑」，"
          "没有事时它不该占位")
    check(".move(" in _seg,
          "⭐ pill **跟着最新那条气泡**（重新挂到容器末尾）—— "
          "📌 一个始终跟随的东西，本身就该只有一个；"
          "每条气泡各挂一个会在历史里留下一串隐藏空壳")
    # 常驻按钮：pill 归零后仍能翻 Finished
    check("'tasks': self.tasks_panel" in src and "[任务]" in src,
          "⭐⭐ 常驻抽屉按钮存在 —— "
          "📌 它解掉的是「pill 归零后无法主动翻 Finished 历史」；"
          "两个不同的钟，不合并")


def t_running_is_not_shown_as_queued() -> None:
    """在跑的后台任务不显示成「排队中」：`mark_background_running` 必须有活着的调用方。"""
    print("")
    print("[2c] 🔴 在跑的**不许**显示成「排队中」")
    import ast as _ast
    from core.runtime import task as T

    k, _ = _kernel()
    _tid = T.create_background_job("Agent · 查点东西")
    _id = str(getattr(_tid, "task_id", "") or _tid or "")
    _queued = {getattr(r, "task_id", "") for r in T.queued_background_jobs(k)}
    check(_id in _queued,
          "⚠️ 前置：刚建出来时它确实在排队（`CREATE` 恒定写 IDLE）")

    T.mark_background_running(_id)
    _queued2 = {getattr(r, "task_id", "") for r in T.queued_background_jobs(k)}
    _live = {getattr(r, "task_id", "") for r in T.live_background_jobs(k)}
    check(_id not in _queued2 and _id in _live,
          "⭐⭐⭐ **转 RUNNING 之后它不再算排队** —— "
          "📌 「在排队」和「在跑」必须是两个状态；压成一个之后，"
          "用户看着一个毫无进展的「排队中」，而它其实一直在跑",
          f"queued={_queued2} live={_live}")

    # ⭐ 而它必须**真的有生产调用方** —— 这一项就是那笔债本身
    _hits = []
    for _f in ("app", "core.orchestrator"):
        _src = module_text(_f)
        _tree = _ast.parse(_src)
        for n in _ast.walk(_tree):
            if not (isinstance(n, _ast.Call)
                    and getattr(n.func, "attr", getattr(n.func, "id", ""))
                    in ("mark_background_running", "_mbr")):
                continue
            for _fn in _ast.walk(_tree):
                if (isinstance(_fn, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                        and _fn.lineno <= n.lineno <= (_fn.end_lineno or 0)):
                    _hits.add if False else _hits.append((_f, _fn.name))
    check(bool(_hits),
          "⭐⭐⭐ **`mark_background_running` 有调用方**", str(_hits))


def t_cancel_reason_reaches_the_model() -> None:
    """「是用户停的」这件事必须到得了模型（后端载体表 `core.runtime.carriers`）。"""
    print("")
    print("[2d] 🔴 「是用户停的」这件事必须到得了模型")
    import ast as _ast
    _src = module_text("core.runtime.carriers")
    check("cancelled_by_user" in _src,
          "⭐⭐⭐ 载体**分得清**「用户按了停」和「它自己没了」")
    check("the user manually stopped this" in _src and "Do NOT restart it on" in _src,
          "⭐⭐⭐ 用户停的那一档，给模型的话指名道姓，并明说别自己重启")
    check("it was not the user" in _src,
          "⭐ 不是用户停的那一档也如实说（不替用户编一个没做过的动作）")
    _tree = _ast.parse(_src)
    _cf = next((f for f in _ast.walk(_tree)
                if isinstance(f, _ast.FunctionDef) and f.name == "cancel"), None)
    _cb = _ast.unparse(_cf) if _cf else ""
    check(bool(_cb) and _cb.index("cancelled_by_user") < _cb.index(".cancel()"),
          "⭐⭐ **先落标记，再 cancel**（否则取消分支可能先跑到、读到「没人按过停」）")


def t_pill_handle_cannot_outlive_its_element() -> None:
    """🔴 [2026-08-22] **pill 只在启动后出现一次，之后永远不再出现。**

    「重启又能再出现一次，抽屉全程正常」—— 这个规律把范围直接锁死在
    **一个活得比元素更长的句柄**上。

    ⭐ 根因不在显示逻辑，在**重建条件**：原来只问 `if _pill is None:`——
       那问的是「**我建过没有**」，而真正要问的是「**它还在不在**」。
       📌 又一次「别用近似物回答一个能精确回答的问题」。

    两条路会让句柄变死，而且**都不报错**：
      ① `move()` 不是原子的（NiceGUI 源码：先 `children.remove(self)`、
         再 `parent.update()`、最后才 `insert`）——中间抛出时元素已经离开容器，
         而调用处原来是 `except Exception: pass`。
         📌 **一个「先摘下来再挂上去」的操作被 except 吞掉时，
            会停在「摘下来了」那一半。**
      ② `chat_container.clear()`（对话重置/重放）会把元素从 client 注册表删掉。

    🔴 后果一样：死句柄上 `set_visibility(True)` **不报错也不显示**。
    """
    print("\n[L5] pill 句柄不许活得比元素长")
    app = module_text("app")
    fn_src = _func_src(app, "_sync_task_pill")
    check("default_slot.children" in fn_src,
          "⭐⭐⭐ 重建判据是「**它还在不在聊天容器的孩子里**」（level-triggered），"
          "不是「句柄空不空」")
    check("self._task_pill = None" in fn_src,
          "⭐ 判定为不在时**把句柄清掉** —— 否则下一次还是不会重建")
    # move 失败不许静默吞
    _i = fn_src.index(".move(")
    _seg = fn_src[_i:]   # 到函数结尾 —— move 与它的失败处理之间隔着整段注释
    check("except Exception: pass" not in _seg.replace(chr(10), " ").replace("  ", " "),
          "🔴 **`move()` 失败不许 `pass`**（负向断言）—— 它先摘后挂，"
          "抛在中间时元素已经离开容器；静默吞掉 = pill 永久消失，"
          "而下一次调用还会因为句柄非空而不重建")
    check("下次重建" in _seg or "self._task_pill = None" in _seg,
          "⭐ 失败的正确表现是**回到可恢复的状态**（清句柄，2 秒后重建），"
          "不是「什么都不做」")


def t_carrier_heartbeat_reads_carriers() -> None:
    print("[L5-hb] 载体心跳：后端心跳周期调用，只推后还在跑的载体（D46）")
    import asyncio as _aio
    from core.runtime import carriers as C
    from core.runtime import waitcond as W
    touched = []
    _orig = W.touch_by_bg_ref
    W.touch_by_bg_ref = lambda ref: touched.append(ref)

    async def _drive():
        C._reset_for_tests()

        async def _slow():
            await _aio.sleep(60)
        _t = _aio.ensure_future(_slow())
        C.start("pip install", _t, "cmd_live")
        _done = _aio.ensure_future(_aio.sleep(0))
        await _done
        C._carriers["dead"] = {"aio": _done, "suspension_ref": "cmd_dead"}
        C.heartbeat()
        _t.cancel()
        await _aio.sleep(0.01)
        C._reset_for_tests()

    try:
        _aio.run(_drive())
    finally:
        W.touch_by_bg_ref = _orig
    check(touched == ["cmd_live"], "⭐⭐ 只推后还在跑的载体", str(touched))
    check('heartbeat.register("carrier_heartbeat"' in module_text("core.backend"),
          "⭐ 由后端心跳调度器周期调用（不再挂在抽屉的界面刷新上）")
    check("touch_by_bg_ref" not in _func_src(module_text("app"), "_refresh_tasks_panel"),
          "界面刷新不再做心跳")


def main() -> int:
    t_cancel_has_a_ui_caller()
    t_cancel_path_really_runs()
    t_cancel_reaches_the_only_real_producer()
    t_running_is_not_shown_as_queued()
    t_cancel_reason_reaches_the_model()
    t_finished_is_this_run()
    t_run_marker_uses_kernel_clock()
    t_pill_and_button_are_two_clocks()
    t_pill_handle_cannot_outlive_its_element()
    t_carrier_heartbeat_reads_carriers()
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
