# -*- coding: utf-8 -*-
"""后台任务成为 Task —— 让「三种结局」在历史里分得开。

═══ 这个套件盯的核心 ═══

🔴 **Nano 自己正犯着 用户在 Claude Code 那里观察到、并写成早先的设计三条要求的那个问题。**
   旧的 `_run_bg_task` 只有两行：

       try:    result = await coro
       except: result = f"执行失败：{e}"

   于是异常被压成一个字符串，然后走**和成功完全一样**的路径 ——
   「跑完了但没输出」/「崩了」/「被用户停了」在下游**表象完全一致**。
   用户当时的原话：「**要不是我在对话里说了一句，模型永远不会知道。**」

📌 **一个能分辨三种结局的系统，和一个能描述其中一种的系统，
   差的不是细节，是「历史能不能读出真相」。**

═══ 第二条：句柄是终止功能的【前提】，不是它的一部分 ═══
旧实现 `asyncio.create_task(...)` 的**返回值直接丢掉**。两个后果：
① 早先的设计 4253 行要的「每个后台任务各自的终止按钮」**物理上不可能实现** ——
   没有任何东西可以被 cancel；
② asyncio 的已知陷阱：没有强引用的 task **可能在完成前被 GC 回收**，
   表现为「跑了一半没了」且不留痕迹。
📌 **一个「以后要能停下它」的东西，创建时就得把句柄留住。**

═══ 第三条：不加列、不加表、不删行 ═══
见 `task.py` 那一层的头部注释。要点：
📌 **「要不要加列」先问「这条信息在崩溃之后还有意义吗」**；
📌 **「谁来把它降下去」的合法答案里包括「刻意没有人，因为代价已经算过」。**

用法：
  py -3.10 tests\cases\t_f1_stage7_bgjob.py
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

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import reset_kernel_for_tests, Command
from core.runtime.store import RuntimeStore
from core.runtime import task as T
from core.runtime import waitcond as W
from core.runtime.reconciler import reconcile_on_startup

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"),
                                 clock=FakeClock(BASE_T))


# ══════════════════════════════════════════════════════════════════════════

def t_three_outcomes(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐⭐ 三种结局必须分得开（这一项的全部意义）")
    k = make_kernel(tmp / "a")

    a = T.create_background_job("pip 装个大库")
    b = T.create_background_job("下载模型")
    c = T.create_background_job("跑个脚本")
    check(all((a, b, c)), "三个后台任务都建出了权威记录")
    check(len(T.live_background_jobs(k)) == 3,
          "⭐ 它们都在 live 列表里 —— **`x running task(s)` 那个数字的唯一数据源**")
    check(all(r.placement == T.Placement.BACKGROUND
              for r in T.live_background_jobs(k)),
          "⭐ 生下来就是**后台** —— 那是定义，不是选项")

    T.finish_background_job(a, "completed", "装完了")
    T.finish_background_job(b, "failed", "网络断了")
    T.finish_background_job(c, "cancelled", "被 user 手动终止")

    ra, rb, rc = (T.get_task(k, x) for x in (a, b, c))
    check(ra.terminal_reason == T.TerminalReason.COMPLETED, "跑完了 → COMPLETED")
    check(rb.terminal_reason == T.TerminalReason.FAILED, "崩了 → FAILED")
    check(rc.terminal_reason == T.TerminalReason.CANCELLED,
          "⭐⭐⭐ **被用户停了 → CANCELLED，不是 FAILED** —— "
          "早就定过：用户主动停掉不是失败，"
          "归错会让模型和用户去排查一个不存在的问题",
          str(rc.terminal_reason))
    check(len({ra.terminal_reason, rb.terminal_reason, rc.terminal_reason}) == 3,
          "🔴🔴 **三个结局互不相同** —— 这一格红了就等于回到了那个"
          "「三种情况表象完全一样」的旧实现")

    # ── 未知结局的兜底方向 ────────────────────────────────────────────────
    d = T.create_background_job("结局不明的那个")
    T.finish_background_job(d, "wat")
    check(T.get_task(k, d).terminal_reason == T.TerminalReason.FAILED,
          "⭐⭐ 不认识的结局 → **FAILED，不是 COMPLETED** —— "
          "📌 兜底方向要朝「看得见」错，不朝「看起来一切正常」错："
          "错成 COMPLETED 是造一个假事实，错成 FAILED 只是多一次排查",
          str(T.get_task(k, d).terminal_reason))

    check(T.finish_background_job(None, "completed") is False,
          "⚠️ 没有权威记录（建的时候失败了）时收尾返回 False，不炸")


def t_live_vs_finished(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐ Running / Finished 两段的数据源")
    k = make_kernel(tmp / "b")
    ids = [T.create_background_job(f"任务{i}") for i in range(3)]
    check(len(T.live_background_jobs(k)) == 3 and T.finished_background_jobs(k) == [],
          "前置：三个在跑、Finished 是空的")

    for i, tid in enumerate(ids):
        k.clock.advance(1.0)
        T.finish_background_job(tid, "completed")
    fin = T.finished_background_jobs(k)
    check(len(fin) == 3 and T.live_background_jobs(k) == [],
          "收完之后：live 空、Finished 三条")
    check([r.goal_summary for r in fin] == ["任务2", "任务1", "任务0"],
          "⭐ **新的在前** —— Finished 段要先看到最近那个",
          str([r.goal_summary for r in fin]))
    check(len(T.finished_background_jobs(k, limit=2)) == 2,
          "⭐⭐ 读侧封顶生效 —— 这就是「谁来把它降下去」的第一层答案："
          "**单调堆积在界面上不会发生**")


def t_no_task_birth(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐ 后台任务**不许**让一件事诞生")
    k = make_kernel(tmp / "c")
    check(T.current_conversation_task(k) is None, "前置：没有在进行的事")

    tid = T.create_background_job("装库", T.owner_label())
    convs = [r for r in T.list_active_tasks(k) if r.kind == T.TaskKind.CONVERSATION]
    check(len(convs) == 0,
          "🔴🔴 **起一个后台任务 → 仍然 0 件对话类 Task** —— "
          "后台任务只在某件事进行中才会被起来，而那件事早就因为别的归属物"
          "（等待）存在了；确实没有时**留空比无端造一件事诚实**")
    check(T.get_task(k, tid).parent_task_id is None, "父留空")

    # 有那件事时就挂上去
    opened = W.open_wait(reason="等装库", wake_on=[W.WakeSource.TIMER],
                         timer_seconds=60)
    assert opened is not None
    conv = T.current_conversation_task(k)
    tid2 = T.create_background_job("再装一个", T.owner_label())
    check(T.get_task(k, tid2).parent_task_id == conv,
          "⭐ 有那件事在进行时，后台任务**挂到它下面**（分组信息落上了）",
          str(T.get_task(k, tid2).parent_task_id))


def t_crash_forensics(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐⭐⭐ 崩溃取证：上个进程死的时候在跑什么")
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "d" / "rt.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    T.clear_blocker_providers_for_tests()
    k = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T))
    tid = T.create_background_job("下载一个 8G 的模型")

    # 模拟崩溃重启
    T.clear_blocker_providers_for_tests()
    k2 = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T + 500))
    reconcile_on_startup(k2)

    r = T.get_task(k2, tid)
    check(r.lifecycle == T.Lifecycle.TERMINAL
          and r.terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐⭐ 重启后被收成 `INTERRUPTED_BY_RESTART` —— "
          "它的载体是内存里的 coroutine，进程死了就没了",
          f"{r.lifecycle}/{r.terminal_reason}")
    fin = T.finished_background_jobs(k2)
    check(len(fin) == 1 and fin[0].goal_summary == "下载一个 8G 的模型",
          "⭐⭐⭐ **而它出现在 Finished 段里，带着「在跑什么」** —— "
          "这正是「崩溃恢复后能翻看上个进程在跑什么」要的效果。"
          "🔴 旧实现是内存 dict，重启后**连它曾经存在都不知道**",
          str([x.goal_summary for x in fin]))
    check(fin[0].terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐ 而且它和「跑完了」「崩了」「被停了」都分得开 —— **第四种结局**")


def t_model_sees_only_live(tmp: pathlib.Path) -> None:
    print("\n[5] ⭐ 注入段只报还在跑的")
    k = make_kernel(tmp / "e")
    check(T.background_jobs_for_model(k) == "",
          "⭐ 一个都没有时是**空串，一个字符都不加**")
    a = T.create_background_job("装库")
    txt = T.background_jobs_for_model(k)
    check("Background jobs" in txt and "装库" in txt, "有在跑的 → 报出来")
    T.finish_background_job(a, "completed")
    check(T.background_jobs_for_model(k) == "",
          "⭐⭐ **收掉之后立刻不报** —— 已结束的结果本来就通过 background 唤醒 / "
          "诈尸气泡送到它面前了，动态段再报一遍等于同一件事说两次，"
          "而模型会当成两件事。"
          "📌 一件已经通过别的通道告诉过它的事，不该再进每轮注入")
    T.create_background_job("还在跑的")
    check("does NOT stop them" in T.background_jobs_for_model(k),
          "⭐⭐ 措辞里明说**结束一件事不会停掉它们** —— "
          "明确要求过：终止/收尾都不许连带杀掉后台任务，"
          "要停得由 Nano 自己下一次行动去取消")


def t_slots(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ execution slots —— 前台 1 + 后台 N。

    🔴 在此之前**后台并发完全没有上限**（`asyncio.create_task` 想起多少起多少），
       而后台任务**每完成一个就唤醒一次模型** —— 所以那不只是句柄上限，
       **它是一个成本乘数**：同时二十个下载 = 完成时二十次模型调用。
    📌 **一个只存在于内存里的限制，不是这个系统的限制，
       它只是这一次进程运行的限制**（`pipeline_lock` 就是那样，
       而且它对后台一无所知）。
    """
    print("\n[7] ⭐⭐⭐ execution slots：前台 1 + 后台 N")
    from core.runtime.kernel import InvariantViolation  # noqa: F401
    k = make_kernel(tmp / "s")

    ids = [T.create_background_job(f"job{i}") for i in range(4)]
    check(len(T.queued_background_jobs(k)) == 4 and T.running_background_count(k) == 0,
          "⭐⭐ 建出来时**四个全在排队**（ACTIVE + IDLE）—— "
          "`CREATE` 恒定写 IDLE，那本来就是「还没跑」的诚实状态")

    for i in ids[:T.MAX_BACKGROUND_RUNNING]:
        check(T.mark_background_running(i) is True, f"拿到 slot → RUNNING")
    check(T.running_background_count(k) == T.MAX_BACKGROUND_RUNNING,
          f"跑满 {T.MAX_BACKGROUND_RUNNING} 个", str(T.running_background_count(k)))

    # ── 超上限：不变量必须拦住，而且是**回滚**不是半写 ──────────────────
    ok = T.mark_background_running(ids[T.MAX_BACKGROUND_RUNNING])
    check(ok is False, "⭐⭐ 第 N+1 个转 RUNNING **被拦住**")
    check(T.get_task(k, ids[T.MAX_BACKGROUND_RUNNING]).execution == T.Execution.IDLE,
          "⭐⭐⭐ 而且是**整体回滚** —— 它仍然停在 IDLE，"
          "不是「一半写进去了」。📌 不变量在事务内，破了就回滚",
          T.get_task(k, ids[T.MAX_BACKGROUND_RUNNING]).execution)
    check(T.running_background_count(k) == T.MAX_BACKGROUND_RUNNING,
          "计数没被污染")

    # ── 前台那个 1 ────────────────────────────────────────────────────────
    check(T.MAX_FOREGROUND_RUNNING == 1,
          "⭐ 前台上限是 **1**，而且它是常量不是配置项 —— "
          "📌 一个用户一次只在跟一件事对话，做成可配的等于允许一种没有意义的状态")
    conv = T.ensure_conversation_task("聊着")
    k.submit(Command(kind=T.SET_EXECUTION, subject_id=conv,
                     payload={"task_id": conv, "execution": T.Execution.RUNNING}))
    conv2 = T.start_new_conversation_task("另一件")
    try:
        k.submit(Command(kind=T.SET_EXECUTION, subject_id=conv2,
                         payload={"task_id": conv2, "execution": T.Execution.RUNNING}))
        _blocked = False
    except Exception:
        _blocked = True
    check(_blocked,
          "⭐⭐⭐ 第二个**前台** Task 转 RUNNING 被拦 —— "
          "这条约束此前只靠内存里的 `pipeline_lock`，而它一重启就没了、"
          "且对后台一无所知")

    # ── 注入段要把「排队中」标出来 ────────────────────────────────────────
    txt = T.background_jobs_for_model(k)
    check("[queued - not started yet]" in txt,
          "⭐⭐ 注入段把**排队中**的标出来了 —— "
          "一个还没轮到的任务和一个真在跑的任务，对模型是两个不同的事实；"
          "混在一起它会去汇报一个还没发生的进展。"
          "📌 一段注入里，凡是两种状态会导致模型说不同的话，就必须分开标")


def t_source_invariants() -> None:
    print("\n[6] ⭐⭐ 源码不变量")
    app = module_text("app")
    tsk = module_text("core.runtime.task")

    # ① 载体的 asyncio.Task 句柄被保住（终止功能的前提；无强引用的 task 可能被 GC）
    car = module_text("core.runtime.carriers")
    tree = ast.parse(car)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "start":
            fn = n
    check(fn is not None, "前置：找到后端载体表的 `start`")
    body = ast.unparse(fn) if fn else ""
    check("aio = asyncio.ensure_future" in body and "'aio': aio" in body.replace('"', "'"),
          "⭐⭐⭐ AST：`ensure_future` 的返回值存进载体表")
    check("def cancel" in car and "_carriers.cancel(" in app, "⭐ 终止入口存在且界面 `■` 调它")

    # ② CancelledError 接在 Exception 之前（3.8+ 它继承 BaseException，不被 except Exception 捕获）
    run_fn = None
    for n in ast.walk(fn) if fn else []:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run":
            run_fn = n
    check(run_fn is not None, "前置：找到载体的 `_run`")
    handlers = []
    for n in ast.walk(run_fn) if run_fn else []:
        if isinstance(n, ast.Try):
            handlers = [ast.unparse(h.type) if h.type else "bare" for h in n.handlers]
            break
    check("asyncio.CancelledError" in handlers and "Exception" in handlers
          and handlers.index("asyncio.CancelledError") < handlers.index("Exception"),
          "⭐⭐⭐ AST：`CancelledError` 接在 `Exception` **之前**", str(handlers))

    # ③ 旧后台任务链（零生产调用方，已删除）没有长回来
    for _gone in ("_start_bg_task", "_run_bg_task", "cancel_bg_task", "_bg_slot_sem",
                  "_bg_tasks", "_append_zombie_bubble"):
        check(_gone not in app, f"旧链 `{_gone}` 不在 app 里")
    check("MAX_BACKGROUND_RUNNING" in tsk,
          "⭐ 上限只有一个数字来源（`task.MAX_BACKGROUND_RUNNING`）")

    # ④ 三种结局的映射表里三个值互不相同
    i = tsk.index("_BG_OUTCOME_TO_REASON = {")
    seg = tsk[i:tsk.index("}", i)]
    check(all(x in seg for x in ("COMPLETED", "FAILED", "CANCELLED")),
          "⭐ 映射表里三个终态原因都在，且互不相同")

    # ⑤ 不删行：这一层不许出现 DELETE
    # ⚠️ 锚点用的是**那一层自己的说明行**，不是内部代号 ——
    #    代号会随清理消失，而这句注释和它描述的那层代码是同生共死的。
    j = tsk.index("# 后台任务成为 Task")
    layer = tsk[j:tsk.index("def owner_label", j)]
    # ⚠️ **必须先滤掉注释行再查。** 第一版直接在整段里搜 `DELETE`，
    #    结果被**这一层自己那段解释「为什么不删行」的注释**打中了 ——
    #    那段注释里就写着 `DELETE` 这个词。
    # 📌 这是 那条判据的又一次现身：**文本匹配会被注释和 docstring 打中**，
    #    所以一条「代码里不许出现 X」的断言，必须先把不是代码的部分去掉 ——
    #    否则**越是把理由写清楚的代码，越容易被自己的解释判成违规**。
    layer_code = "\n".join(l for l in layer.splitlines()
                           if not l.strip().startswith("#"))
    check("DELETE" not in layer_code.upper(),
          "⭐⭐ 这一层**没有任何 DELETE** —— 本项目从不删状态行（全都关到终态），"
          "而且那还会绕过内核的唯一写路径。"
          "📌 「谁来把它降下去」的合法答案里包括「刻意没有人，因为代价已经算过」")
    check("不到 1 MB/年" in layer,
          "⚠️ 而那个「代价算过」真的算了并写下来了（不是一句「量很小」）")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_three_outcomes(tmp)
        t_live_vs_finished(tmp)
        t_no_task_birth(tmp)
        t_crash_forensics(tmp)
        t_model_sees_only_live(tmp)
        t_slots(tmp)
        t_source_invariants()

    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过" +
          ("" if ok == len(_results) else " —— 失败项："))
    for good, name, note in _results:
        if not good:
            print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
