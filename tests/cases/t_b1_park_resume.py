# -*- coding: utf-8 -*-
"""搁置 / 回接 —— 「当前是哪件事」从**推导**变成**被写下的事实**。

═══ 这一套守的东西 ═══

  ① **「当前是哪件事」必须是显式的**
     原来 `current_conversation_task` 只有 `ORDER BY created_at DESC` ——
     「当前」是从创建时间**推**出来的。只有一个候选时它永远对，所以它一直
     没出问题；而 要的「搁置 X → 先做 Y → 回头接 X」一旦成立，它必然错：
     **X 永远比 Y 老，接不回去。**
     📌 **一个「现在是哪个」的问题，答案必须是一条被显式写下的事实，
        不能从一个碰巧单调的字段里推** —— 推导在只有一个候选时永远正确，
        于是它的错误要等到第二个候选出现才暴露，而那时它已经被到处依赖了。
     ⭐ 与早先的设计 `waiting_intent` 那条逐字同形（「不许由 `timer_at` 猜 pill 是否可控」）。

  ② **「两件事并存」≠「两件事都在前台」**
     🔴 这是一个**今天就存在**的缺陷：`task_boundary(start)` 直接建第二个
        FOREGROUND，于是旧那件事当场失联 —— 回不去（不是「当前」）、
        也 finish 不掉（finish 收的是新的那个）。
     ⚠️ 本套件**真的复现它**（不是查源码），再验修法。

  ③ **搁置不停任何东西**：后台任务 / 等待 / Subagent照旧跑
     📌 搁置的是「Nano 的注意力」，不是「那件事的执行体」。

  ④ **回接的路不许被自己的门禁挡掉**
     🔴 旧门禁 `current_conversation_task() is not None` 在**全部被搁置**时判 False
        → `task_boundary` 不注入 → 模型恰好在唯一需要 `resume` 的时刻拿不到它。
     📌 **一个「回去的动作」，不能由「已经不在那里」来决定给不给。**

  ⑤ **真的走过工具那一层**：本套件有一项**真的 await `_handle_task_boundary`**，
     不是查 manifest 里有没有那个字符串。
     📌 AST 断言只证明代码被摆成了当时理解的样子（本项目第五次教训）。

用法：
  py -3.10 tests\\cases\\t_b1_park_resume.py
"""
from __future__ import annotations

import asyncio
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
from core.runtime.kernel import reset_kernel_for_tests, Command, InvariantViolation
from core.runtime.store import RuntimeStore
from core.runtime import task as T

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def boot(tmp: pathlib.Path):
    st = RuntimeStore(tmp / "rt.db")
    _stores.append(st)
    return reset_kernel_for_tests(store=st, clock=FakeClock(BASE_T))


def close_all_stores() -> None:
    """Windows 上不关连接就删不掉 tempdir（WinError 32）—— 照抄早先那套。"""
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


def _parked(k):
    """被退居到后台的那些「一件事」。

    ⚠️ `parked_conversation_tasks()` 已随抽屉那段「搁置 N」一起删掉
       （2026-08-20，Task 收窄）—— 它唯一的消费者是那段 UI。
       ⭐ **但它守的那条判据没变**，所以这里就地读 placement：
          「至多一个在前台，其余退居后台」这个不变量仍然是本套件的主角。
    """
    return [r for r in T.list_active_tasks(k)
            if r.kind == T.TaskKind.CONVERSATION
            and r.placement == T.Placement.BACKGROUND]


def _mk(kind=None, placement=None, goal="", tid=None) -> str:
    """直接下 CREATE —— 绕开门面，为的是能造出**门面不会造的**状态。"""
    k = reset_kernel_for_tests.__self__ if False else None  # noqa: F841
    from core.runtime.kernel import get_kernel
    p = {"kind": kind or T.TaskKind.CONVERSATION,
         "placement": placement or T.Placement.FOREGROUND,
         "goal_summary": goal}
    if tid:
        p["task_id"] = tid
    return get_kernel().submit(Command(kind=T.CREATE, payload=p)).data["task_id"]


# ══════════════════════════════════════════════════════════════════════════
def t_current_is_explicit(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐⭐ 「当前是哪件事」认的是 `placement`，不是创建时间")
    from core.runtime.kernel import get_kernel
    boot(tmp / "a")
    k = get_kernel()

    x = _mk(goal="装依赖")
    check(T.current_conversation_task(k) == x, "只有一件时它就是当前", x)

    # 搁置它 → 当前变成「没有」
    check(T._park_current_for_new("先做别的") == x, "⭐⭐ 搁置返回被搁置的 id")
    check(T.current_conversation_task(k) is None,
          "⭐⭐ 搁置之后**没有当前这件事**了 —— "
          "📌 「没有在前台的事」是一个诚实的状态，不该被最近那条 ACTIVE 顶上")
    check([r.task_id for r in _parked(k)] == [x],
          "⭐ 而它在**搁置**名单里（没消失，也没被当成结束）")

    # 开新的一件 → 它成为当前；⚠️ 老的**比新的老**，这正是旧实现的死穴
    y = _mk(goal="写报告")
    check(T.current_conversation_task(k) == y, "新开的成为当前", y)

    # ⭐⭐⭐ 回接一件**更早创建**的事 —— 旧实现在这里必错
    ok, msg = T.resume_conversation_task(x)
    check(ok, "⭐⭐⭐ 回接成功", msg[:50])
    check(T.current_conversation_task(k) == x,
          "⭐⭐⭐ **当前变回了那件更老的事** —— "
          "🔴 旧实现按 `created_at DESC` 取，这里会取到 y，"
          "也就是**回接根本不可能实现**。"
          "📌 一个「现在是哪个」的问题，答案必须被写下来，不能从时间推",
          f"current={T.current_conversation_task(k)} / x={x} / y={y}")
    check([r.task_id for r in _parked(k)] == [y],
          "⭐⭐ 而 y **自动让位**成了搁置 —— "
          "📌 「至多一个前台」这条不变量，决定了交接只能是"
          "**先腾位再入座**，所以让位必须由回接自己做，不能指望调用方记得")


def t_invariant_scope(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐⭐ 不变量：至多一个前台对话 —— **真的触发一次**")
    from core.runtime.kernel import get_kernel
    boot(tmp / "b")
    k = get_kernel()
    _mk(goal="第一件")

    _fired = ""
    try:
        _mk(goal="第二件也在前台")
        _fired = ""
    except InvariantViolation as e:
        _fired = str(e)
    check("one_foreground_conversation" in _fired,
          "⭐⭐⭐ **两个都在前台 → 事务当场回滚** —— "
          "📌 一个读侧假定唯一的字段，必须有人保证它唯一；"
          "否则 `LIMIT 1` 只是在两个都对的答案里随便挑一个，而且永远不报错",
          _fired[:60])
    check(len([r for r in T.list_active_tasks(k)
               if r.kind == T.TaskKind.CONVERSATION]) == 1,
          "⭐⭐ 回滚是**真的回滚**：库里仍然只有一条（不是拦了但已经写进去）")

    # ⚠️ 作用域：GUI Task 也是 FOREGROUND，不许被这条误伤
    _mk(kind=T.TaskKind.GUI_AUTOMATION, goal="点鼠标")
    check(True, "⭐⭐⭐ **GUI Task 同时在前台不触发** —— "
                "📌 一个限制的作用域必须和它要守的那个读侧查询逐字相同，"
                "否则不是拦得太松就是拦到了不相干的人")
    # 后台任务同理
    _mk(kind=T.TaskKind.BACKGROUND_JOB, placement=T.Placement.BACKGROUND, goal="下载")
    check(len(T.list_active_tasks(k)) == 3, "⭐ 三类共存互不干扰",
          str(sorted(r.kind for r in T.list_active_tasks(k))))


def t_start_parks_the_old_one(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐⭐ `start` 把旧的**显式搁置**（修掉一个今天就存在的失联缺陷）")
    from core.runtime.kernel import get_kernel
    boot(tmp / "c")
    k = get_kernel()
    x = T.ensure_conversation_task("装依赖")
    y = T.start_new_conversation_task("先看看别的")

    check(y and y != x, "另开了一件", f"{x} → {y}")
    check(T.current_conversation_task(k) == y, "当前是新那件")
    check([r.task_id for r in _parked(k)] == [x],
          "⭐⭐⭐ **旧那件被搁置，不是留在前台** —— "
          "🔴 旧实现建第二个 FOREGROUND，于是旧的既回不去、"
          "`finish` 也收不到它（收的是新的那个），只能等 2 小时 TTL；"
          "而名下有 blocker 的连 TTL 都不收，会在 [Ongoing work] 里一直报下去。"
          "📌 **「两件事并存」和「两件事都在前台」是不同的两句话**")

    # ⭐ 反向：finish 收的是**前台那个**，不碰被搁置的
    _done = T.finish_conversation_task("completed", "看完了")
    check(_done == y, "⭐⭐ finish 收的是前台那件", str(_done))
    check([r.task_id for r in _parked(k)] == [x],
          "⭐⭐ **被搁置的一个字没动** —— "
          "📌 结束一件事不该顺手结束另一件；它们并存是用户的意思")
    ok, _ = T.resume_conversation_task(x)
    check(ok and T.current_conversation_task(k) == x,
          "⭐⭐⭐ 收完新的之后**回得去老的** —— 这就是那条验收的骨架"
          "（X 卡住 → 先做 Y → 回头接上 X）")


def t_resume_validates_before_it_moves(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐⭐ 回接：**校验在前、副作用在后**")
    from core.runtime.kernel import get_kernel
    boot(tmp / "d")
    k = get_kernel()
    x = T.ensure_conversation_task("正经事")

    ok, msg = T.resume_conversation_task("task_不存在的")
    check(not ok and "No piece of work" in msg, "⭐ 不存在 → 失败", msg[:40])
    check(T.current_conversation_task(k) == x,
          "⭐⭐⭐ **失败时前台那件一个字没动** —— "
          "📌 一个先把现任搁置、然后才发现目标不存在的实现，"
          "会把用户从一件好好的事上挪走，去接一件不存在的事")

    ok2, msg2 = T.resume_conversation_task("")
    check(not ok2 and "needs the task id" in msg2, "⚠️ 空 id 如实拒绝")

    # 已结束的
    p = T._park_current_for_new()
    _gui = _mk(kind=T.TaskKind.GUI_AUTOMATION, goal="点鼠标")
    ok3, msg3 = T.resume_conversation_task(_gui)
    check(not ok3 and "not a conversation-level" in msg3,
          "⭐⭐ GUI Task **接不了** —— 它有自己的边界（mini 窗），不归这条路管",
          msg3[:40])

    get_kernel().submit(Command(kind=T.TERMINATE, subject_id=p,
                                payload={"task_id": p, "reason": T.TerminalReason.COMPLETED}))
    ok4, msg4 = T.resume_conversation_task(p)
    check(not ok4 and "already ended" in msg4 and "Do NOT tell the user" in msg4,
          "⭐⭐⭐ 已结束的 → **如实说它结束了，并明确禁止向用户宣布回接了** —— "
          "📌 静默当成功的代价是模型去汇报一件根本没发生的事", msg4[:50])


def t_injection_never_hides_parked(tmp: pathlib.Path) -> None:
    print("\n[5] ⭐⭐⭐ 注入：搁置的**必须报**，且带得走 id")
    from core.runtime.kernel import get_kernel
    boot(tmp / "e")
    k = get_kernel()
    x = T.ensure_conversation_task("装依赖")
    T._park_current_for_new()
    y = T.ensure_conversation_task("写报告")

    txt = T.conversation_tasks_for_model(k)
    check(x in txt,
          "⭐⭐⭐ **被搁置的那条在注入里** —— "
          "🔴 它名下空空、又不在推进，正好落在原来那条「没影响就藏起来」的"
          "过滤里；而**藏了它就再也回不去**：回接需要 id，id 只能从这段注入里拿。"
          "📌 判断「有没有影响」时，必须把「有人正等着用它」算进去")
    check("PARKED" in txt and "nothing is working on it" in txt,
          "⭐⭐⭐ 明说**没有人在推进它** —— "
          "📌 `placement=BACKGROUND` 只表示「不在用户眼前」，"
          "而模型看到 background 会默认「它自己在跑」，"
          "然后向用户汇报一个根本没有发生的进展")
    check("action='resume'" in txt,
          "⭐⭐ 告诉它**怎么回去**（且只在真有搁置时才讲）—— "
          "📌 只注入模型无法从上下文推导出来的那一点，其余全是噪音")
    check("[in front]" in txt and y in txt, "⭐ 前台那件标成 in front")

    # ⭐⭐⭐ 门禁：全部搁置时**仍然要有工具**
    T._park_current_for_new()
    check(T.current_conversation_task(k) is None, "前置：现在一件在前台的都没有")
    check(T.has_live_conversation_work(k),
          "⭐⭐⭐ **全部被搁置时门禁仍为真** —— "
          "🔴 旧门禁问的是 `current_conversation_task is not None`，"
          "这一刻判 False → `task_boundary` 不注入 → "
          "模型恰好在唯一需要 `resume` 的时刻拿不到它。"
          "📌 一个「回去的动作」，不能由「已经不在那里」来决定给不给")

    # 反向：真的什么都没有时，两边**同时**为空
    for r in _parked(k):
        get_kernel().submit(Command(kind=T.TERMINATE, subject_id=r.task_id,
                                    payload={"task_id": r.task_id,
                                             "reason": T.TerminalReason.COMPLETED}))
    check(T.conversation_tasks_for_model(k) == "" and not T.has_live_conversation_work(k),
          "⭐⭐⭐ 什么都没有时**两边同时为空** —— "
          "📌 一个工具和它的事实来源必须由同一个条件控制；"
          "半截状态下模型会开始猜（有工具没事实 → 去结束一件不存在的事）")


def t_the_tool_really_runs(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐⭐⭐ **真的走一遍工具那一层**（不是查 manifest 有没有那串字）")
    from core.runtime.kernel import get_kernel
    import core.orchestrator as O
    boot(tmp / "f")
    k = get_kernel()

    # ── manifest 真的把新动作给出去了（模型只看得到 schema）──────────────
    from core.tools import manifests as _MF
    _enum = (_MF._TASK_BOUNDARY_MANIFEST["parameters"]["properties"]
             ["action"]["enum"])
    check(set(_enum) == {"finish", "start", "resume"},
          "⭐⭐ schema 的 enum 就是这三个 —— `park` 已随「Task 抛后台」那个"
          "概念一起退役（2026-08-20）。"
          "📌 那条的镜像：**给不出去的执行得了也没用**；"
          "而它的反面同样要守：**给出去的必须还在**", str(_enum))
    check("task_id" in _MF._TASK_BOUNDARY_MANIFEST["parameters"]["properties"],
          "⭐ 而 resume 需要的 `task_id` 也在 schema 里 —— "
          "🔴 少了它模型说得出 resume 却指不出接哪个")

    # ── ⭐⭐⭐ 真的 await 那个 handler ────────────────────────────────────
    orch = O.Orchestrator.__new__(O.Orchestrator)   # 不跑 __init__：本用例只碰这一个方法

    async def _go():
        x = T.ensure_conversation_task("装依赖")
        # ⭐ 退居现在是 `start` 的**默认副作用**，不再是一个可申报的动作。
        #    📌 判据取自 manifest 自己写过的那句：**默认不需要动作，
        #       只有偏离默认才需要动作。**「放下」是默认，「回来」才是偏离。
        r1 = await O.Orchestrator._handle_task_boundary(
            orch, {"action": "start", "goal": "改代码"}, "a1", event_queue=None)
        _parked_after_start = [r.task_id for r in _parked(k)]
        r2 = await O.Orchestrator._handle_task_boundary(
            orch, {"action": "resume", "task_id": x}, "a2", event_queue=None)
        # ⚠️ **当场取**，不许留到后面几次调用跑完再看。
        #    📌 一个断言如果读的是「所有步骤都跑完之后的状态」，
        #       它证的就不是那一步做对了，而是最后一步做了什么。
        _cur_after_resume = T.current_conversation_task(k)
        r3 = await O.Orchestrator._handle_task_boundary(
            orch, {"action": "resume", "task_id": "task_没有这个"}, "a3", event_queue=None)
        r4 = await O.Orchestrator._handle_task_boundary(
            orch, {"action": "park"}, "a4", event_queue=None)
        return x, _parked_after_start, _cur_after_resume, r1, r2, r3, r4

    x, _parked_ids, _cur, r1, r2, r3, r4 = asyncio.get_event_loop().run_until_complete(_go())

    check(not r1.failed and _parked_ids == [x],
          "⭐⭐⭐ **`start` 把旧那件自动退居了** —— 🔴 它修的是一个今天就存在的"
          "缺陷：直接建第二个 FOREGROUND，旧那件当场变成回不去也 finish 不掉的孤儿",
          f"parked={_parked_ids} x={x}")
    check(x in (r1.text or ""),
          "⭐⭐ 而且**回话里带着旧那件的 id** —— 模型要靠它回来；"
          "📌 一个工具的回话是模型对世界的唯一观测，它描述的行为变了，"
          "回话不改就是一句假话",
          (r1.text or "")[:60])
    check(not r2.failed and _cur == x,
          "⭐⭐⭐ **resume 真的把它接回了前台** —— "
          "📌 这一项是本套件唯一跨了 orchestrator → task → kernel → SQLite "
          "四层的；其余都只证明我把代码摆成了我理解的样子")
    check(r3.failed and "No piece of work" in (r3.text or ""),
          "⭐⭐ 接一个不存在的 → **`is_error=True`** —— "
          "📌 工具的失败必须走 is_error，不能塞进正文让模型自己读出来",
          (r3.text or "")[:40])
    # ⭐⭐⭐ `park` 退役之后**必须撞墙，不许被静默忽略**。
    #    📌 一个被删掉的枚举值，如果调用它什么也不发生，模型会以为自己搁置成功了，
    #       然后向用户宣布一件没发生的事。
    check(r4.failed and "no 'park' action" in (r4.text or ""),
          "⭐⭐⭐ 模型照旧写 `park` → **如实说这个动作没有了**（is_error）—— "
          "📌 被删掉的枚举值必须让调用它的人撞到墙",
          (r4.text or "")[:50])
    check("dont_wait" in (r4.text or ""),
          "⭐⭐ 而且**指向正确的替代品**：想停下等一个慢【调用】用 `dont_wait` —— "
          "🔴 这正是那次返工的结论：抛后台的主语从 Task 换成了一次调用",
          (r4.text or "")[:80])


def t_parking_stops_nothing(tmp: pathlib.Path) -> None:
    print("\n[7] ⭐⭐ 搁置**不停任何东西**")
    from core.runtime.kernel import get_kernel
    boot(tmp / "g")
    k = get_kernel()
    T.ensure_conversation_task("装依赖")
    _bg = T.create_background_job("下载 3GB 的东西")
    T.mark_background_running(_bg)
    _before = T.running_background_count(k)

    T._park_current_for_new("先做别的")
    check(T.running_background_count(k) == _before == 1,
          "⭐⭐⭐ 搁置之后后台任务**照旧在跑** —— "
          "📌 搁置的是「Nano 的注意力」，不是「那件事的执行体」；"
          "两者混同就会出现「我把它放一放」顺手杀掉一个正在下载的东西。"
          "⭐ 与那条同源：中断的作用域是【这一轮的决策与输出】，"
          "不是【这一轮启动过的一切】")
    check("still running" in T.background_jobs_for_model(k)
          or "Background jobs" in T.background_jobs_for_model(k),
          "⭐ 而且模型照旧看得见它在跑")


def t_drawer_keeps_them_apart(tmp: pathlib.Path) -> None:
    print("\n[8] ⭐⭐ 抽屉：搁置的**不进 pill**、也不混进 Running")
    import ast as _ast
    src = module_text("app")
    tree = _ast.parse(src)
    fn = next((f for f in _ast.walk(tree)
               if isinstance(f, _ast.FunctionDef) and f.name == "_refresh_tasks_panel"), None)
    check(fn is not None, "⚠️ [L5] 前置：找得到刷新函数")

    # pill 的实参必须是 len(running)，不是 len(running)+len(parked)
    _pill_args = []
    for n in _ast.walk(fn) if fn else []:
        if (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
                and n.func.attr == "_sync_task_pill"):
            _pill_args = [_ast.unparse(a) for a in n.args]
    check(_pill_args == ["len(running)"],
          "⭐⭐⭐ **pill 只数在跑的** —— "
          "📌 pill 数的是「有东西在动」，而搁置的没有任何执行体；"
          "算进去用户会以为有人在推进它，然后等一个永远不会自己到来的结果。"
          "⭐ 同 2026-08-10 那张表：系统托管长调用也不进 pill",
          str(_pill_args))
    # ⭐⭐⭐ **2026-08-20 换了口径**：抽屉里那段「搁置 N」整段删掉了。
    #    上一版验的是「它单独一段、且没有 ■」；现在验的是**它根本不在抽屉里**。
    #    📌 判据没变，反而更纯粹：**抽屉回答的是「什么正在动」** ——
    #       一个被退居的「一件事」没有任何执行体，画在这里就是告诉用户
    #       「有人在推进它」，然后就会去等一个永远不会自己到来的结果。
    #    ⭐ 2026-08-20 补的那条是同一面：被 `dont_wait` 移出手头的调用，
    #       之后就算「变回手头的活」也**不搬 UI** —— 抽屉记的是执行体，
    #       而执行体从头到尾没变过。
    _defs_app = {f.name for f in _ast.walk(tree)
                 if isinstance(f, _ast.FunctionDef)}
    check("_render_parked_row" not in _defs_app and "_parked_snapshot" not in _defs_app,
          "⭐⭐⭐ 抽屉里**没有**「搁置」那一段（两个函数都已删除）—— "
          "📌 一个没有执行体的东西，不该出现在一个回答「什么在跑」的面板里",
          str(sorted(_defs_app & {"_render_parked_row", "_parked_snapshot"})))
    # ⚠️ 按**函数定义**数，不按文本数 —— 📌 只要断言读的是文本，
    #    那段留痕注释（里面必然提到这两个名字）就会参与判定。
    from tests._src import module_text as _mt_c
    check("def promote" in _mt_c("core.runtime.carriers")
          and "_carriers.promote(" in _mt_c("core.orchestrator"),
          "⭐⭐ 而抽屉 Running 段有了**第二个生产者**（`dont_wait` → 后端 `carriers.promote`）—— "
          "🔴 在此之前它只有一个（Subagent），那正是用户报的"
          "「后台任务只有Subagent能进去」：不是抽屉坏了，是没有第二条路")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nanob1_"))
    try:
        for fn in (t_current_is_explicit, t_invariant_scope,
                   t_start_parks_the_old_one, t_resume_validates_before_it_moves,
                   t_injection_never_hides_parked, t_the_tool_really_runs,
                   t_parking_stops_nothing, t_drawer_keeps_them_apart):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        close_all_stores()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in _results if not r[0]]
    print("\n" + "=" * 74)
    if bad:
        print("失败项：")
        for _, n, note in bad:
            print(f"  - {n}   [{note}]")
    print(f"结果：{len(_results) - len(bad)}/{len(_results)} 通过")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
