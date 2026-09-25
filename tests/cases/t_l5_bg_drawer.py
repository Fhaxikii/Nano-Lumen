# -*- coding: utf-8 -*-
"""后台任务抽屉 —— **给 `cancel_bg_task` 认领**。

═══ 这一套真正要守的东西 ═══

早先那一行的原话：

  > ⚠️⚠️ **`cancel_bg_task()` 现在【零 UI 调用方】—— 这一行就是它的认领人。**
  > 📌 **一个写好但没人调的函数，比没写更坏** —— 没写时缺口是可见的，
  >    写了不接时缺口**看起来已经补上了**。

⭐ 所以本套件的第一项不是"抽屉画得对不对"，是 **`cancel_bg_task` 真的有人调**。
   本轮 又撞了两次同形的（`bridge.recall` 零调用方 / `bridge.get_store`
   名字根本不存在却有 28 项绿灯）—— 那两次都是**只验结构、没验行为**。
   ⚠️ 所以这里除了 AST 找调用点，还**真的把取消路径走一遍**。

其余三条：
  ② Finished = **本次运行产生的**（不是"最近 N 条"），且**含**本次启动
     认定的 `INTERRUPTED_BY_RESTART`
  ③ 「本次运行」的起点取自**内核时钟**，不是模块级 `time.time()`
  ④ CANCELLED **不并进** FAILED

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
    return ""


def t_cancel_has_a_ui_caller() -> None:
    print("\n[1] ⭐⭐⭐ `cancel_bg_task()` **真的有 UI 调用方了**（这一项就是那笔债）")
    src = module_text("app")
    tree = ast.parse(src)

    # ⚠️ 走 AST 找**调用**，不是在源码里搜字符串 ——
    #    📌 那个名字在注释和 docstring 里出现过好几次（都在解释"它没人调"），
    #       搜文本会被解释性注释本身喂绿。这是本项目第五次栽在同一个形状上。
    callers = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "cancel_bg_task"
                    and fn.name != "cancel_bg_task"):
                callers.append(fn.name)
    check(bool(callers),
          "⭐⭐⭐ 至少有一个函数**调用**了 `cancel_bg_task` —— "
          "🔴 早先的原话：一个写好但没人调的函数，比没写更坏",
          str(sorted(set(callers))))
    check("_cancel_bg_from_ui" in callers,
          "⭐⭐ 调用方是那颗 `■` 的 handler")

    # 那颗 ■ 必须真的挂在渲染里
    _fn = next((f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "_render_bg_row"), None)
    _seg = (ast.get_source_segment(src, _fn) or "") if _fn else ""
    check("_cancel_bg_from_ui" in _seg and "■" in _seg,
          "⭐⭐⭐ 而那个 handler **被 `■` 的点击真的绑上了** —— "
          "📌 「有 handler」和「handler 接上了」是两件事"
          "（同 bridge 那次：函数写得完全正确，零调用方）")


def t_cancel_path_really_runs() -> None:
    print("\n[2] ⭐⭐⭐ 取消路径**真的走一遍**（不是只看结构）")
    from app import WebUI

    class _Host:
        def __init__(self):
            self._bg_tasks = {"ui_1": {"rt_task_id": "rt_1"}}
            self._handed_back_carriers = {}
            self.called = []
            self.refreshed = 0

        # ⚠️ `_cancel_carrier` 用**真的那一个**（绑到 stub 上）——
        #    📌 换成假的就退回「我两次想法相同」，而这一项要验的恰恰是
        #       「点 `■` 之后到底有没有人接得住」。
        _cancel_carrier = WebUI._cancel_carrier

        def cancel_bg_task(self, task_id, by="user"):
            self.called.append((task_id, by))
            return True

        def _refresh_tasks_panel(self):
            self.refreshed += 1

    class _Rec:
        task_id = "rt_1"

    h = _Host()
    # ⚠️ 真的调 WebUI 上那个方法（绑到 stub 上），不重写一份逻辑 ——
    #    📌 自制假对象 + 自己重写的逻辑，只能证明两次想法相同。
    import app as _app

    class _Notify:
        @staticmethod
        def notify(*a, **k):
            pass
    _orig = _app.ui
    try:
        _app.ui = _Notify
        WebUI._cancel_bg_from_ui.__get__(h, _Host)(_Rec())
    finally:
        _app.ui = _orig
    check(h.called == [("ui_1", "user")],
          "⭐⭐⭐ **`rt_task_id` → UI 侧 id 的映射真的走通了**，"
          "并且带上了 `by='user'` —— "
          "🔴 映射写错的表现是「点了没反应」，不报错", str(h.called))
    check(h.refreshed >= 1, "⚠️ 取消后重刷面板（否则那一行会一直显示在 Running）")


def t_cancel_reaches_the_only_real_producer() -> None:
    """🔴🔴 这一项补的是 [1][2] 都没能守住的那个洞（2026-08-20 发现）。

    [1] 验「`cancel_bg_task` 有 UI 调用方」、[2] 验「给一张有货的 `_bg_tasks`，
    映射查得对」—— 两项都绿，而生产环境里**那颗 `■` 对每一条 Running 都失效**：
    `_bg_tasks` 的唯一写入方 `_start_bg_task()` 在 2026-08-10「系统交还不再写
    后台 Task 权威记录」之后就零生产调用方了，这张表**恒空**；
    而抽屉里唯一的 Running（Subagent）走的是 `_handed_back_carriers` 那条路。

    📌 **一条断言如果它声明的前提是自己建立的，它证明不了生产路径** ——
       [2] 自己 seed 了 `_bg_tasks`，于是它证明的是「查找逻辑对」，
       不是「有人往表里放货」。
    📌 与早先那条互为镜像：那次是前提**没人**建立，这次是前提
       **只有测试**建立。两种都让断言测的不是它声称在测的东西。
    """
    print("")
    print("[2b] 🔴 那颗 `■` 打到的是**真的有货的那张表**")
    from app import WebUI
    import asyncio as _aio

    class _Host2:
        def __init__(self):
            self._bg_tasks = {}                 # ← 生产环境里它就是空的
            self._handed_back_carriers = {}
            self.refreshed = 0
            self.notified = []
        _cancel_carrier = WebUI._cancel_carrier

        def cancel_bg_task(self, task_id, by="user"):
            raise AssertionError("不该走到 _bg_tasks 那条路")

        def _refresh_tasks_panel(self):
            self.refreshed += 1

    class _Rec2:
        task_id = "rt_agent"

    async def _drive():
        h2 = _Host2()

        async def _forever():
            await _aio.sleep(60)

        _t = _aio.ensure_future(_forever())
        await _aio.sleep(0)                     # 让它真的跑起来
        h2._handed_back_carriers["c1"] = {"display": "Agent · 查点东西",
                                          "aio": _t, "rt_task_id": "rt_agent",
                                          "owns_record": False}
        import app as _app

        class _N:
            @staticmethod
            def notify(*a, **k):
                h2.notified.append(a[0] if a else "")
        _orig = _app.ui
        try:
            _app.ui = _N
            WebUI._cancel_bg_from_ui.__get__(h2, _Host2)(_Rec2())
        finally:
            _app.ui = _orig
        # 让取消真的落地（cancel() 只是请求，要下一次调度才抛进去）
        try:
            await _t
        except _aio.CancelledError:
            pass
        return h2, _t.cancelled()

    h2, _cancelled = _aio.run(_drive())
    check(_cancelled,
          "⭐⭐⭐ **载体真的被 cancel 了** —— `_bg_tasks` 是空的，"
          "而它照样找得到那条在跑的东西",
          f"cancelled={_cancelled}")
    check(bool(h2.notified) and "已终止" in str(h2.notified[0]),
          "⭐ 而且告诉用户的是「已终止」，不是「这个任务已经不在跑了」—— "
          "🔴 后者正是改造前每一次点击都会得到的那句假话",
          str(h2.notified))


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
    """🔴 实测 2026-08-20：抽屉里一个**正在跑**的Subagent一直显示「排队中」。

    根因是同一条死路的第三个受害者：`mark_background_running()` 的唯一调用方是
    `_run_bg_task`，而它的唯一调用方是 `_start_bg_task` —— 后者在 2026-08-10
    「系统交还不再写后台 Task 权威记录」之后就**零生产调用方**。
    于是这行状态**再也没有人写过**，每一个真的后台任务都永久停在 ACTIVE+IDLE，
    而抽屉按 `queued_background_jobs()` 把它画成「排队中」。

    📌 **一条状态如果只有一个写入者，那个写入者一死，它就变成一个永远不会改变
       的谎** —— 而读它的人不会报错，只会一直读到那个谎。
    ⚠️ 前两个受害者：抽屉那颗 `■`（`_bg_tasks` 恒空）、后台任务权威记录本身。
    """
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
    _live_callers = [h for h in _hits if h[1] not in ("_run_bg_task",)]
    check(bool(_live_callers),
          "⭐⭐⭐ **`mark_background_running` 有活着的调用方** —— "
          "🔴 改造前唯一的调用方长在一条零生产调用方的死路上"
          "（`_start_bg_task` → `_run_bg_task`），于是这行状态再没被写过",
          str(_live_callers))


def t_cancel_reason_reaches_the_model() -> None:
    """🔴 实测 2026-08-20：main agent **不知道是用户手动停的**。

    用户点了抽屉里那颗 `■`，而 main agent 的说法是：
      「Subagent任务被中止了，没有返回结果。可能是那个目录太大、文件太多，
        搜索耗时太长被系统停了，或者其他原因。」
    —— 它在**猜**。

    根因：Subagent自己在取消分支里写的是「这个Subagent被用户手动终止了」，
    但它 re-raise 之后，载体那一层用一段**通用文案**覆盖了 `result`。
    📌 **两个人都写这条结论时，后写的那个会盖掉先写的** ——
       而先写的那个才是知道真相的。
    ⚠️ 刚为 `owns_record` 写过这句判据（一条记录只能有一个收尾人），
       转头在【结论文本】上又犯了一次 ——
       📌 一条判据只在你想到它适用的地方才生效；它不会自己去覆盖同形的第二处。
    """
    print("")
    print("[2d] 🔴 「是用户停的」这件事必须到得了模型")
    import ast as _ast
    _src = module_text("app")
    _tree = _ast.parse(_src)
    _fn = next((f for f in _ast.walk(_tree)
                if isinstance(f, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and f.name == "_start_handed_back_carrier"), None)
    check(_fn is not None, "⚠️ 前置：找得到载体")
    _body = _ast.unparse(_fn) if _fn else ""

    check("cancelled_by_user" in _body,
          "⭐⭐⭐ 载体**分得清**「用户按了停」和「它自己没了」 —— "
          "📌 一个字段不许表达两个现实；而这两者对模型的下一步完全不同")
    check("the user manually stopped this" in _body,
          "⭐⭐⭐ 用户停的那一档，给模型的话**指名道姓** —— "
          "🔴 改造前它只能猜「可能是目录太大被系统停了」")
    check("Do NOT restart it on your own" in _body,
          "⭐⭐ 并明说**别自己重启一遍** —— "
          "📌 用户停掉它是一次决定，不是一次故障")
    check("it was not the user" in _body,
          "⭐ 而**不是**用户停的那一档也要如实说 —— "
          "📌 宁可承认「不知道为什么」，也不许替用户编一个用户没做过的动作"
          "（同 `cancelled_by_user_message` 那条）")

    # ⭐ 标记必须**先落再扣扳机**
    _cf = next((f for f in _ast.walk(_tree)
                if isinstance(f, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and f.name == "_cancel_carrier"), None)
    _cb = _ast.unparse(_cf) if _cf else ""
    check(_cb.index("cancelled_by_user") < _cb.index(".cancel()"),
          "⭐⭐ **先落标记，再 cancel** —— 📌 顺序反了的话，取消分支可能在同一轮"
          "事件循环里先跑到，读到的还是「没人按过停」")


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
