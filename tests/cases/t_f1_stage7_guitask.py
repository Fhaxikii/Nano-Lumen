# -*- coding: utf-8 -*-
"""GUI 自动化成为**第一个真实的 Task**。

═══ 这个套件盯的核心 ═══

`tasks` 表在真库里长期是 **0 行** —— 状态机 / 命令 / 不变量 / 启动恢复全都有，
但**生产侧从来没有人建过一个**。这一项让它有了第一个真实用例。

📌 **给一个新实体接线，要从「边界最清楚」的那一类开始，
   不是从「最常见」的那一类开始。**
   最常见的那类（对话）边界最模糊（一句话算一件事？一个话题算一件事？），
   而**模糊的边界会让这个实体从第一天就不可信** ——
   之后所有 `owner_task_id` 都继承那份不可信。
⭐ GUI 自动化的边界是**零歧义**的：缩窗开始、还原结束，
   而这两个事件**已经是权威租约**（早先），**不需要模型判断**。

═══ 第二条：`owner_task_id` 是「归属标签」，不是「生命周期主宰」═══
每一类被拥有的东西**保留自己独立的失效条件**。
📌 因为对话类 Task 的边界最终要靠模型判断，而**模型会判错** ——
   让它决定谁该死，就是把「分组判错」升级成「租约泄漏」。

用法：
  py -3.10 tests\cases\t_f1_stage7_guitask.py
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
from core.runtime.kernel import (
    Command, KernelError, reset_kernel_for_tests)
from core.runtime.store import RuntimeStore
from core.runtime import oslease as L
from core.runtime import task as T

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

def t_lifecycle(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐ 缩窗建 Task / 还原收 Task（边界零歧义）")
    k = make_kernel(tmp / "a")

    check(len(T.list_active_tasks(k)) == 0,
          "前置：一开始一个 Task 都没有（这就是接线前真库的样子）")
    check(L.current_gui_task_id() is None, "没在 GUI 模式 → 没有 GUI Task")

    L.open_gui_session("往记事本里写点东西")
    tid = L.current_gui_task_id()
    check(bool(tid), "⭐⭐⭐ 缩窗 → **真的建出了一个 Task**", str(tid))

    rec = T.get_task(k, tid)
    check(rec.kind == T.TaskKind.GUI_AUTOMATION, "kind = GUI 自动化", rec.kind)
    check(rec.lifecycle == T.Lifecycle.ACTIVE, "活着")
    check(rec.placement == T.Placement.FOREGROUND,
          "⭐ 生下来就是**前台** —— GUI Task 永远前台（见硬约束那组）")
    check(rec.goal_summary == "往记事本里写点东西",
          "⭐ 目标摘要带上了 —— 它回答「这一次是要干什么」", rec.goal_summary)

    L.close_gui_session()
    rec2 = T.get_task(k, tid)
    check(rec2.lifecycle == T.Lifecycle.TERMINAL, "⭐ 还原窗口 → Task 收成终态")
    check(rec2.terminal_reason == T.TerminalReason.COMPLETED,
          "终态原因 = 完成", str(rec2.terminal_reason))
    check(tid not in [x.task_id for x in T.list_active_tasks(k)],
          "不在活跃列表里了")

    # 幂等
    L.close_gui_session()
    check(T.get_task(k, tid).lifecycle == T.Lifecycle.TERMINAL,
          "⚠️ 重复关是幂等的")


def t_ownership(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐⭐ 两条授权都绑到这个 Task 上")
    k = make_kernel(tmp / "b")

    L.open_gui_session("操作屏幕")
    tid = L.current_gui_task_id()
    L.grant_temp_auto("用户批准")

    with k.store.read() as conn:
        rows = {r["scope"]: r["owner_task_id"] for r in conn.execute(
            "SELECT scope, owner_task_id FROM os_leases WHERE status='HELD'")}
    check(rows.get(L.GUI_SESSION_SCOPE) == tid,
          "⭐ GUI 会话租约的 owner_task_id = 那个 Task", str(rows))
    check(rows.get(L.TEMP_AUTO_SCOPE) == tid,
          "⭐⭐⭐ **免确认授权也绑到同一个 Task** —— 当初的原话："
          "「`_temp_auto` 由『绑 mini 窗』改为**显式绑 Task**」。"
          "于是「mini 窗关 = 授权失效」这个**用 UI 形态当授权作用域锚点**的"
          "方向错误，换成了「**Task 结束 = 授权失效**」", str(rows))

    # ⚠️⚠️ 归属标签 ≠ 生命周期主宰
    check(L.temp_auto_authorized(k) is True, "前置：授权此刻有效")
    L.revoke_temp_auto()
    check(L.temp_auto_authorized(k) is False,
          "⭐⭐ 授权仍能**独立**失效（`revoke_temp_auto` 照旧管用，"
          "不需要动那个 Task）。"
          "📌 `owner_task_id` 是**归属标签**，不是**生命周期主宰** —— "
          "对话类 Task 的边界最终要靠模型判断，而模型会判错；"
          "让它决定谁该死，就是把「分组判错」升级成「租约泄漏」")

    # ⭐ 启动收尾照旧独立生效
    k2 = make_kernel(tmp / "b2")
    L.open_gui_session("又一次")
    L.grant_temp_auto("批准")
    L.startup_release_all(k2)
    check(L.gui_session_active(k2) is False and L.temp_auto_authorized(k2) is False,
          "⭐ 启动收尾照旧把两条都收掉 —— **不依赖 Task 有没有被收尾**。"
          "📌 过期/回收是推导出来的，不依赖任何人记得收")


def t_no_backgrounding(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐⭐ 硬约束：GUI Task 不可后台化")
    k = make_kernel(tmp / "c")
    L.open_gui_session("操作屏幕")
    tid = L.current_gui_task_id()

    try:
        k.submit(Command(kind=T.SET_PLACEMENT, subject_id=tid,
                         payload={"task_id": tid,
                                  "placement": T.Placement.BACKGROUND}))
        check(False, "应该被挡住")
    except KernelError as e:
        check(True,
              "⭐⭐⭐ 挡住了。**为什么必须是硬约束而不是「碰巧成立」**："
              "当初的原话 —— 改绑 Task 后必须保证生命周期与 mini 窗**严格同步**，"
              "否则**用户再也无法从屏幕判断授权是否仍在**。"
              "GUI Task 被丢后台 → mini 窗还开着但那件事「在后台跑」→ "
              "**屏幕信号与真实状态脱钩**，而 mini 窗恰恰是"
              "「Nano 正在自主操作」唯一的用户可见信号。"
              "📌 同 [F5]：**UI 必须是权威状态的忠实投影**", str(e)[:50])

    # 反向前置：别的 Task **可以**后台化（否则上面只证明了「谁都不许」）
    t2 = k.submit(Command(kind=T.CREATE,
                          payload={"kind": T.TaskKind.CONVERSATION})).data["task_id"]
    k.submit(Command(kind=T.SET_PLACEMENT, subject_id=t2,
                     payload={"task_id": t2, "placement": T.Placement.BACKGROUND}))
    check(T.get_task(k, t2).placement == T.Placement.BACKGROUND,
          "反向前置：**普通 Task 可以**后台化 —— "
          "说明上面那条不是「谁都不许」")

    # ⚠️ 前台方向不许被误挡
    k.submit(Command(kind=T.SET_PLACEMENT, subject_id=tid,
                     payload={"task_id": tid, "placement": T.Placement.FOREGROUND}))
    check(T.get_task(k, tid).placement == T.Placement.FOREGROUND,
          "⚠️ 把 GUI Task 设成前台**不该被挡**（它本来就是前台，幂等）")


def t_failure_isolation(tmp: pathlib.Path) -> None:
    print("\n[4] ⚠️ 新实体的失败不许让已经能用的东西停摆")
    k = make_kernel(tmp / "d")

    # 让建 Task 这一步炸掉
    _saved = T.CREATE
    try:
        T.CREATE = "task.does_not_exist"     # 提交时会抛
        lid = L.open_gui_session("即使建 Task 失败")
        check(lid is not None,
              "⭐⭐ 建 Task 失败时**GUI 模式照旧开起来** —— "
              "📌 **接线一个新实体时，它的失败不该让已经能用的东西停摆。**"
              "（GUI 模式的权威是那条租约，不是 Task）")
        check(L.gui_session_active(k) is True, "GUI 模式真的生效了")
        check(L.current_gui_task_id() is None,
              "⚠️ 而且如实反映「没有 Task」，不编一个假 id")
    finally:
        T.CREATE = _saved

    L.close_gui_session()
    check(L.gui_session_active(k) is False, "关也照旧能关（没有 Task 也不报错）")


def t_read_single_source(tmp: pathlib.Path) -> None:
    print("\n[5] ⚠️ 「谁拥有它」只有一个来源")
    k = make_kernel(tmp / "e")
    L.open_gui_session("x")
    tid = L.current_gui_task_id()

    # 直接改租约上的归属 → 读出来必须跟着变（证明没有内存副本）
    with k.store.read() as conn:
        pass
    import sqlite3
    c = sqlite3.connect(str(tmp / "e" / "rt.db"))
    c.execute("UPDATE os_leases SET owner_task_id='task_someone_else' "
              "WHERE scope=?", (L.GUI_SESSION_SCOPE,))
    c.commit()
    c.close()
    check(L.current_gui_task_id() == "task_someone_else",
          "⭐ 从租约上现读，**没有另存一份内存副本**。"
          "📌 一个「谁拥有它」的答案只该有一个来源 —— 再存一份，"
          "就又造出「两个东西都以为自己知道」的局面", str(L.current_gui_task_id()))


def t_owner_needs_own_expiry(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ **任何「绑 Task」的东西都必须自带一条独立兜底失效条件。**

    这条判据来自 2026-08-08 那次十项核查（动手前的二次核实）。推导过程：
    （迭代阅读的 scratchpad）/（仅本次 MCP）/ per-task 成本 三条**各自独立**地
    要求 Task **跨多次交换**，而它们对「什么时候结束」的要求是同一句：
    **那个目标达成或放弃** —— 而那**只有模型知道**。

    ⚠️ 于是「模型忘了说完了」就成了必须回答的问题。逐个算后果：
    scratchpad 不销毁＝上下文留一坨；**「仅本次」授权不失效＝安全问题**；
    成本一直累加＝数字失去意义；running tasks 一直显示＝噪音。

    📌📌 **所以：不许把 Task 的结束当成任何东西【唯一】的失效条件。**
       它必须自带 TTL / 数量上限 / 启动收尾之类的独立兜底 ——
       因为 **Task 的结束依赖模型判断，而模型会忘**。
    ⭐ 这是「`owner_task_id` 是归属标签，不是生命周期主宰」的**第二次应用**，
       而这一次它救的是**安全属性**，不只是整洁。
    """
    print("\n[7] ⭐⭐⭐ 绑 Task 的东西必须自带独立兜底（新判据）")
    k = make_kernel(tmp / "own")
    L.open_gui_session("操作屏幕")
    tid = L.current_gui_task_id()
    L.grant_temp_auto("用户批准")
    check(bool(tid) and L.temp_auto_authorized(k), "前置：两条授权都绑在这个 Task 上")

    # ── 路径一：Task **根本没被收尾**，授权照旧能独立撤掉 ──────────────
    L.revoke_temp_auto()
    check(L.temp_auto_authorized(k) is False,
          "⭐⭐ Task 还是 ACTIVE，但授权**已经独立失效** —— "
          "📌 不依赖「那件事结束了」")
    check(T.get_task(k, tid).lifecycle == T.Lifecycle.ACTIVE,
          "⚠️ 而且确实**没动那个 Task**（证明上面不是靠收尾顺带做到的）",
          T.get_task(k, tid).lifecycle)

    # ── 路径二：启动收尾也不看 Task ────────────────────────────────────
    k2 = make_kernel(tmp / "own2")
    L.open_gui_session("又一次")
    tid2 = L.current_gui_task_id()
    L.grant_temp_auto("批准")
    L.startup_release_all(k2)
    check(L.temp_auto_authorized(k2) is False and L.gui_session_active(k2) is False,
          "⭐⭐ 启动收尾把两条都收了 —— **同样不看那个 Task 有没有被收尾**")
    check(T.get_task(k2, tid2).lifecycle == T.Lifecycle.ACTIVE,
          "⚠️ 光跑 `startup_release_all` 时那个 Task 仍是 ACTIVE —— "
          "⭐ 这里验的是**租约的独立性**（它不看 Task），不是「Task 该不该活」。"
          "收 Task 是 `reconcile_on_startup` 第 1b 步的职责，见 [8]。"
          "⚠️ 本行的说明 2026-08-09 改过：原文写的是「进程死了它没机会被收」，"
          "而那句话在 1b 步落地的同一刻就不成立了。"
          "📌 一条断言的**措辞**也会过期，而它过期时【不会红】—— "
          "它会安静地教下一个人一件错事",
          T.get_task(k2, tid2).lifecycle)

    # ── 判据（出处写在这里，测试不读 docs 等外部文件）────────────────────
    # 凡是把生命周期挂在某个 Task 上的东西（暂存器、「仅本次」的授权、per-task 计数、
    # 运行中任务的显示），都不许把 Task 的结束当成它唯一的失效条件；必须另外自带一条
    # 独立兜底：TTL、数量上限，或启动时收尾。原因是 Task 的结束依赖模型判断，而模型会忘。


def t_restart_kills_unrebuildable(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ 重启后：**执行载体不可重建的 Task 必须终止**（2026-08-09 补的既存缺陷）。

    🔴 **这一组是实测抓出来的，不是推理出来的。** 现象：
    `open_gui_session()` → 换新内核（模拟重启）→ 租约收掉了 ✅，
    **GUI Task 仍然 ACTIVE、`terminal_reason=None`** ❌。

    ⚠️ 两个原因叠在一起，各自都不显眼：
    ① `reconcile_on_startup` 第 1 步的 SQL 只查 `execution=RUNNING`，
       而 `open_gui_session` **不置 RUNNING** —— 📌 **一个「只查某个状态」的
       恢复步骤，捞不到从来不进那个状态的东西。**
    ② `_finish_gui_task` 的唯一入口是 `close_gui_session`，那需要「当前 GUI 会话」，
       而**会话租约已经先被收掉了** ——
       📌📌 **一条记录的收尾路径，不许依赖另一条会先它一步消失的记录。**
       （这正是 2026-08-04 两条不死挂起的形状：收尾能力和被收尾对象一起消失。）

    ═══ 判据 ═══
    📌 **「进程死了这件事该不该放弃」取决于它的执行载体能不能被重建** ——
       对话能（载体是用户，用户会回来）；GUI 不能（载体是那段点鼠标的代码 + 当时的屏幕）；
       后台任务/Subagent不能（载体是内存里的 coroutine / 子进程）。
    ⭐ 与 `oslease.startup_release_all` / `waitcond.startup_sweep` 是**同一条判据**
       的第三次应用：**收什么取决于「它的执行体还在不在」。**
    """
    print("\n[8] ⭐⭐⭐ 重启：载体不可重建的 Task 必须终止（补既存缺陷）")
    from core.runtime.reconciler import reconcile_on_startup
    from core.runtime.kernel import Command as _C

    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "restart" / "rt.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    T.clear_blocker_providers_for_tests()
    k = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T))
    L.open_gui_session("往记事本写点东西")
    gui = L.current_gui_task_id()
    bg = k.submit(_C(kind=T.CREATE, payload={
        "kind": T.TaskKind.BACKGROUND_JOB, "placement": T.Placement.BACKGROUND,
        "goal_summary": "pip 装个大库"})).data["task_id"]
    conv = T.ensure_conversation_task("聊着呢")
    check(all(T.get_task(k, t).lifecycle == T.Lifecycle.ACTIVE for t in (gui, bg, conv)),
          "前置：三类 Task 都 ACTIVE")

    # ── 模拟重启：同一个库，全新内核 ────────────────────────────────────
    T.clear_blocker_providers_for_tests()
    k2 = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T + 100))
    reconcile_on_startup(k2)
    L.startup_release_all(k2)

    rg = T.get_task(k2, gui)
    check(rg.lifecycle == T.Lifecycle.TERMINAL,
          "⭐⭐⭐ **GUI Task 被收成终态** —— 这就是那个既存缺陷修掉的样子",
          rg.lifecycle)
    check(rg.terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐⭐ 而且原因是 `INTERRUPTED_BY_RESTART`，**不是 COMPLETED** —— "
          "payload 的 key 必须是 `reason`（写成 `terminal_reason` 会被静默忽略、"
          "默认成 COMPLETED，于是「被重启打断」在历史里长成「做完了」）",
          str(rg.terminal_reason))

    rb = T.get_task(k2, bg)
    check(rb.lifecycle == T.Lifecycle.TERMINAL
          and rb.terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐⭐ 后台任务同样被收 —— 它的载体是内存里的 coroutine，进程死了就没了",
          f"{rb.lifecycle}/{rb.terminal_reason}")

    rc = T.get_task(k2, conv)
    # 🔴🔴 **2026-08-22 的改动推翻了这一格原来守的东西。**
    #    旧断言：「对话类【豁免】，仍然 ACTIVE —— 它的载体是用户，用户会回来接着做」。
    #    ⭐ 那条判据答的是「技术上能不能续」，而真正决定这件事的是
    #       「用户还想不想要」—— 代码没有资格代答。
    #    ⚠️ 中间还错过一版：先改成了「关闭 = 用户放弃本次协同，所以一律终止」，
    #       用户当场指出那只是把一个不可靠的推断换成了另一个（**误触关闭按钮呢？**）。
    #    ⭐ 最终形态：**终止执行**（执行体随进程消失是事实），
    #       但**由 Nano 主动开口问用户要不要重做**（`scheduler.startup_resume_notice`）。
    #    📌 **「不自动续」和「不提」是两件事** —— 第一版把它们绑在了一起。
    check(rc.lifecycle == T.Lifecycle.TERMINAL
          and rc.terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐⭐⭐ 对话类**也被终止**（上个进程结束 = 那次执行结束）",
          f"{rc.lifecycle}/{rc.terminal_reason}")
    check(not T.list_active_tasks(k2),
          "⭐⭐ 重启后活跃列表**空了** —— 没有任何一件活会自己接着跑")

    # ⚠️ 但它们没有被忘掉：报告要带上「是什么」，好让 Nano 说得出人话
    rep2 = reconcile_on_startup(k2)
    _details = getattr(rep2, "interrupted_details", None)
    check(_details is not None, "⭐ 报告有 `interrupted_details` 这个字段")

    # ── 幂等：连跑两次结果相同 ──────────────────────────────────────────
    reconcile_on_startup(k2)
    check(T.get_task(k2, conv).terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART
          and T.get_task(k2, gui).terminal_reason == T.TerminalReason.INTERRUPTED_BY_RESTART,
          "⭐ 再跑一次启动恢复：结果不变（`command_id` 带 revision 做的幂等）")

    # ── 豁免名单清空之后，SQL 那条空集分支必须存在 ──────────────────────
    src = module_text("core.runtime.reconciler")
    check("_RESUMABLE_KINDS: frozenset = frozenset()" in src,
          "⭐⭐ 豁免名单已清空 —— ACTIVE 的一律终止")
    # ⚠️ `NOT IN ()` 在 SQLite 里是语法错误（空括号），所以必须有另一条分支。
    #    📌 一个「名单可能为空」的查询，空集是它的**正常输入**，不是边界情况。
    _fn = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "reconcile_on_startup")
    _seg = ast.get_source_segment(src, _fn) or ""
    check("if _RESUMABLE_KINDS:" in _seg,
          "⭐⭐ 有空集分支 —— 否则 `NOT IN ()` 会直接抛 SQLite 语法错误")

    # ── 而「终止」不等于「闭嘴」──────────────────────────────────────────
    _sched = module_text("core.runtime.scheduler")
    check("def startup_resume_notice" in _sched,
          "⭐⭐⭐ 有一条「要不要重做」的出口 —— 终止的是执行，不是这件事")
    check("do NOT say it crashed" in _sched,
          "⭐ 而且措辞说「关闭」不说「崩溃」（分不清时用断言更弱的那个）")


def t_source_invariants() -> None:
    print("\n[6] 源码不变量")
    ol = module_text("core.runtime.oslease")
    tk = module_text("core.runtime.task")

    check("_create_gui_task(reason)" in ol, "缩窗时建 Task")
    check("_finish_gui_task(_tid)" in ol, "还原时收 Task")

    # ⚠️ 顺序：必须先读 task_id 再撤租约
    seg = ol.split("def close_gui_session")[1].split("def ")[0]
    i_read = seg.index("current_gui_task_id()")
    i_revoke = seg.index("kind=REVOKE")
    check(i_read < i_revoke,
          "⭐⭐ **先读 task_id、再撤租约** —— 撤完就读不到了（它挂在租约上）。"
          "📌 一个「顺带收尾」的动作，取信息的顺序不能晚于毁掉信息的动作")

    check("模糊的边界会让这个实体从第一天就不可信" in ol,
          "⚠️ 「从边界最清楚的那类开始」这条判据留了痕")
    check("归属标签" in ol and "生命周期主宰" in ol,
          "⚠️ 「归属标签 ≠ 生命周期主宰」留了痕")

    # ⭐ 那条曾经写着「现在不加」的注释，现在必须真的加上了
    check("会在这里加" not in tk,
          "⭐ task.py 里那句「以后会在这里加、现在不加」已经被兑现掉")
    check("生命周期必须与 mini 窗严格同步" in tk,
          "硬约束的错误信息说清了依据 —— "
          "📌 说的是**为什么**不可后台化，而不是一个读者查不到的编号")
    check("提前加会变成一条没人能违反" in tk or "没人能违反" in tk or
          "别在一条规则的执行层存在之前就写下它" in tk,
          "⚠️ 并留痕了那条注释本身的价值：📌 **别在一条规则的执行层"
          "存在之前就写下它** —— 否则它是一条没人能违反、也没人能兑现的空话")

    # AST：硬约束必须在 SET_PLACEMENT 里，且在真正 UPDATE 之前
    tree = ast.parse(tk)
    seg2 = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_set_placement":
            seg2 = ast.get_source_segment(tk, n) or ""
    check("GUI_AUTOMATION" in seg2 and "BACKGROUND" in seg2,
          "AST：约束真的在 `_set_placement` 里", str(bool(seg2)))
    check(seg2.index("raise KernelError") < seg2.index("UPDATE tasks SET placement"),
          "⭐ 而且在真正写库**之前**拦 —— 不是写完再回滚")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_lifecycle(tmp)
        t_ownership(tmp)
        t_no_backgrounding(tmp)
        t_failure_isolation(tmp)
        t_read_single_source(tmp)
        t_owner_needs_own_expiry(tmp)
        t_restart_kills_unrebuildable(tmp)
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
