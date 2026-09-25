# -*- coding: utf-8 -*-
"""对话类 Task 接线 —— 「一件事」的边界由模型划，代码给默认。

═══ 这个套件盯的核心，只有一条 ═══

📌 **懒创建的「懒」，全部靠「谁有权触发创建」那张名单守住。**
   名单一放宽，懒创建就静默退化成 **per-exchange** —— 而 per-exchange 已被
   **三条各自独立的判断**否掉（迭代阅读的 scratchpad / 「一个 turn 不能
   代表任务完成」/ per-task 成本要「总执行代价」）。
   🔴 而且退化**不会有任何报错**：每条消息都有主人，看起来一切正常，
      只是 Task 变成了 turn 的同义词。所以这条边界**必须由断言守**。

名单（本套件逐项验）：
  · 有权创建   —— 等待（定义就是「结束这一轮、等触发再回来」，天生跨 turn）
                   DEFERRED 交互（审计连 deadline 都不设，可能挂几天）
  · 只贴标签   —— 排队消息、工具批次、单次动作尝试（**全是一轮内的东西**）

═══ 第二条：工具和它的事实来源必须同条件 ═══
`task_boundary`（工具）与 `[Ongoing work]`（事实）由同一个判断控制。
📌 半截状态下模型会开始猜：有工具没事实 → 它去结束一件不存在的事；
   有事实没工具 → 它想说「这件事完了」却无处可说。

═══ 第三条：做完了 ≠ 放弃了 ═══
早先已定「`CANCELLED` 必须独立，不许塞进 `FAILED`」，理由是**用户主动停掉
不是失败**。这里同源：**历史要读得出真相**，否则有人会去排查一个不存在的问题。

用法：
  py -3.10 tests\t_f1_stage7_convtask.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
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
from core.runtime import inbox as I
from core.runtime import attempt as A
from core.runtime import toolbatch as B

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def _code_of(node) -> str:
    """把一个 AST 节点 unparse 成**只剩代码**的样子 —— docstring 剥掉。

    ⚠️⚠️ **`ast.unparse` 会保留 docstring。** 于是「一段解释某条规则的 docstring」
       会把那条规则自己的断言判红。
    🔴 这是同一天**第三次**撞到这个形状：
         ① 查「这一层不许有 DELETE」→ 被那一层解释「为什么不删行」的注释打中
         ② 查「内核不许出现 UI 标识符」→ 被 `kernel.py` 里引用原文的模块头打中
         ③ 就是这里：把历史接口名当行为断言 → 被解释迁移历史的 docstring 打中
    📌 **越是把规则写清楚的代码，越容易被自己的解释判成违规。**
    📌 而更要紧的那条：**第二次撞上同一个形状时就该修形状** ——
       ② 里写下了这句话，然后 ③ 里又犯了一次，
       因为那次只在**那个文件**里修了个案。
       **一条判据只被用在它诞生的那个文件里，等于没立。**
    """
    import copy
    n = copy.deepcopy(node)
    for sub in ast.walk(n):
        body = getattr(sub, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
    return ast.unparse(n)


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    T.clear_activity_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"),
                                  clock=FakeClock(BASE_T))


def _susp(sid: str, **kw) -> dict:
    r = {"suspension_id": sid, "reason": "等装库跑完", "wake_on": ["timer"],
         "timer_at": BASE_T + 60.0, "bg_task_ref": None}
    r.update(kw)
    return r


def _open_wait(rec: dict) -> str:
    """把历史夹具形状送进正式入口；测试不再依赖观测期的 shadow。"""
    timer_at = rec.get("timer_at")
    timer_seconds = (max(1, int(timer_at - BASE_T))
                     if timer_at is not None else None)
    opened = W.open_wait(
        reason=rec.get("reason") or "",
        wake_on=list(rec.get("wake_on") or []),
        timer_seconds=timer_seconds,
        bg_ref=rec.get("bg_task_ref"),
    )
    assert opened is not None
    return opened.wait_id


def _conv(k) -> list:
    return [r for r in T.list_active_tasks(k) if r.kind == T.TaskKind.CONVERSATION]


# ══════════════════════════════════════════════════════════════════════════

def t_who_may_create(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐⭐ 名单：谁有权让一件事诞生")
    k = make_kernel(tmp / "a")

    check(len(_conv(k)) == 0, "前置：一件事都没有")
    check(T.owner_label() is None,
          "⭐ 没在进行任何事时 `owner_label()` 是 None —— 这是**正常值**，不是错误")
    check(T.current_conversation_task(k) is None, "查询也是 None")

    # ── 只贴标签的三类：一个都不许创建 ──────────────────────────────────
    I.submit_user_message("你好")
    check(len(_conv(k)) == 0,
          "🔴🔴 **排队消息进库 → 仍然 0 件事** —— 这条一旦红，懒创建已退化成 per-exchange")

    A.begin(action="click", tool_name="os_execute", summary="点一下")
    check(len(_conv(k)) == 0, "🔴 单次动作尝试 → 仍然 0 件事（它是一轮内的东西）")

    B.shadow_prepare(turn_id="t1", round_idx=0, intended_names=["os_execute"],
                     path_tag="probe")
    check(len(_conv(k)) == 0, "🔴 工具批次 → 仍然 0 件事")

    check(I.pending_count(k) == 1,
          "⭐ 而且这三样**自己都办成了** —— 不许因为「没主人」就失败")

    # ── 有权创建的：等待 ────────────────────────────────────────────────
    wid = _open_wait(_susp("susp_1"))
    check(bool(wid), "等待登记成功", str(wid))
    got = _conv(k)
    check(len(got) == 1,
          "⭐⭐⭐ **一条等待 → 诞生了一件事**（等待天生跨 turn，所以它需要主人）")
    tid = got[0].task_id if got else ""
    rec = W.get(k, wid)
    check(rec is not None and rec.owner_task_id == tid,
          "⭐ 而且这条等待的 `owner_task_id` 真的落到了它身上",
          str(rec.owner_task_id if rec else None))
    check(got and got[0].kind == T.TaskKind.CONVERSATION, "kind = 对话类")
    check(got and got[0].placement == T.Placement.FOREGROUND, "前台")

    # ── 跨 turn 复用：第二条等待不许再生一件 ────────────────────────────
    wid2 = _open_wait(_susp("susp_2", reason="等下载"))
    check(len(_conv(k)) == 1,
          "⭐⭐ **第二条等待复用同一件事** —— 这就是「跨多个 turn」的实现方式")
    rec2 = W.get(k, wid2)
    check(rec2 is not None and rec2.owner_task_id == tid, "两条等待同一个主人")

    # ── 有了之后，贴标签的那三类才贴得上 ────────────────────────────────
    iid = I.submit_user_message("再顺手看下时间")
    it = I.get(k, iid)
    check(it is not None and it.owner_task_id == tid,
          "⭐ 现在有一件事在进行了 → 排队消息**贴上了标签**（但它从来不负责创建）",
          str(it.owner_task_id if it else None))


def t_finish_and_start(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐ 做完了 / 放弃了 / 另开一件（三个都不许混）")
    k = make_kernel(tmp / "b")

    check(T.finish_conversation_task("completed") is None,
          "⭐⭐ 没有在进行的事时收尾 → 返回 None（**不许假装收了一件**，"
          "否则模型会向用户宣布一个不存在的完成）")

    _open_wait(_susp("s1"))
    t1 = T.current_conversation_task(k)
    done = T.finish_conversation_task("completed", "装完了")
    check(done == t1, "收掉的正是当前那件", str(done))
    r1 = T.get_task(k, t1)
    check(r1.lifecycle == T.Lifecycle.TERMINAL, "进终态")
    check(r1.terminal_reason == T.TerminalReason.COMPLETED,
          "⭐ 「做完了」→ COMPLETED", str(r1.terminal_reason))

    _open_wait(_susp("s2"))
    t2 = T.current_conversation_task(k)
    T.finish_conversation_task("abandoned", "用户说算了")
    r2 = T.get_task(k, t2)
    check(r2.terminal_reason == T.TerminalReason.CANCELLED,
          "⭐⭐⭐ 「放弃了」→ **CANCELLED，不是 FAILED** —— "
          "用户主动停掉不是失败，归错会让人去排查一个不存在的问题",
          str(r2.terminal_reason))

    # ── start 默认不结束旧的 ────────────────────────────────────────────
    _open_wait(_susp("s3"))
    t3 = T.current_conversation_task(k)
    t4 = T.start_new_conversation_task("顺手查个天气")
    live = {x.task_id for x in _conv(k)}
    check(t3 in live and t4 in live,
          "⭐⭐⭐ **另开一件 → 两件并存，旧的没被收掉** —— "
          "那条硬约束是「新来的这句话与手头这件事不要求相关」，用户在长任务里插一句别的，"
          "那是两件事同时在跑")
    check(T.current_conversation_task(k) == t4,
          "⭐ 而「当前那件」变成了新的（最近创建的那个）")

    t5 = T.start_new_conversation_task("再开一件顺便收尾", finish_current=True)
    check(T.get_task(k, t4).lifecycle == T.Lifecycle.TERMINAL,
          "⭐ 显式 `finish_current=True` 才收旧的 —— 两个动作分得开")
    check(t5 in {x.task_id for x in _conv(k)}, "新的活着")
    check(t3 in {x.task_id for x in _conv(k)},
          "⭐ 而 t3 **仍然活着** —— 收尾只收「当前那件」，不是把所有的都清了")


def t_owner_is_a_label(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐ 归属是标签，不是生命周期主宰")
    k = make_kernel(tmp / "c")

    wid = _open_wait(_susp("s1"))
    tid = T.current_conversation_task(k)
    T.finish_conversation_task("abandoned", "用户改主意了")

    rec = W.get(k, wid)
    check(rec is not None and rec.status == W.WaitStatus.WAITING,
          "⭐⭐⭐ **一件事收成终态之后，它名下的等待仍然活着** —— "
          "这是那条判据的直接兑现：不许把「这件事结束了」当成任何东西"
          "【唯一】的失效条件（因为它的结束依赖模型判断，而模型会忘）",
          str(rec.status if rec else None))
    check(rec is not None and rec.owner_task_id == tid,
          "⭐ 标签还在（历史读得出它当时归谁）—— 只是那个主人已经终态了")
    check(len(W.list_live(k, owner_task_id=tid)) == 1,
          "按主人查还查得到（分组信息没丢）")


def t_tool_and_facts_same_gate(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐⭐ 工具与它的事实来源，必须同条件")
    k = make_kernel(tmp / "d")
    import core.orchestrator as om

    has0 = om._rt_has_live_work(None)
    txt0 = om._rt_ongoing_work(None)
    check(has0 is False, "没事时：工具**不注入**")
    check(txt0 == "",
          "⭐ 没事时：事实段是**空串，一个字符都不加**（同 Interaction 那条）",
          repr(txt0[:20]))

    _open_wait(_susp("s1", reason="等 pip 装完"))
    has1 = om._rt_has_live_work(None)
    txt1 = om._rt_ongoing_work(None)
    check(has1 is True, "有事时：工具注入")
    check(txt1 != "" and "Ongoing work" in txt1,
          "⭐ 有事时：事实段有内容", txt1.split("\n")[0][:60] if txt1 else "")
    check(has0 == bool(txt0) and has1 == bool(txt1),
          "⭐⭐⭐ **两者永远同真同假** —— 半截状态下模型会开始猜："
          "有工具没事实 → 去结束一件不存在的事；有事实没工具 → 想说却无处可说")

    # 事实段里得真的有那件事的目标，否则模型没法判断该不该收
    tid = T.current_conversation_task(k)
    check(tid in txt1, "⭐ 事实段里带着 task_id（模型要能指名道姓地收它）")
    check("pip" in txt1,
          "⭐ 也带着目标摘要 —— 它回答「这一件到底是什么事」")
    # ⭐⭐⭐ [2026-08-22] **那句话是【读】出来的，不是【复制】进去的。**
    #
    # 🔴 老写法把等待的 `reason` 复制进 `goal_summary`
    #    （`ensure_conversation_task(reason)`）。实测：ping 早跑完了，
    #    那条 Task 仍 ACTIVE，`goal_summary` 逐字写着「still running: ping …」
    #    并每轮注入 —— 用户**重置对话之后** Nano 还在说那条命令在跑。
    # 📌 **把一个会过期的事实复制成永久的，就是造一个会说谎的字段。**
    #    而 `Blocker.summary` 只从**还活着的**等待里来，等待一结束它自动消失 ——
    #    **结构上不可能变陈旧**。
    # 📌 **一个复制来的事实需要有人负责让它过期；一个读出来的事实不需要。**
    _rec_now = T.get_task(k, tid) if hasattr(T, "get_task") else None
    check(_rec_now is not None,
          "读得到这个 Task（`get_task` 改名或查不到时，下面的检查不能静默跳过）")
    if _rec_now is not None:
        check(not (_rec_now.goal_summary or ""),
              "⭐⭐⭐ `goal_summary` 是**空的** —— 那句 pip 来自 blocker，"
              "不是被复制进 Task 里的",
              repr((_rec_now.goal_summary or "")[:40]))
    # 🔴 负向：等待一结束，那句话必须**自己消失**（这正是复制做不到的那一半）
    _wids = [b.blocker_id for b in (T.get_task_view(k, tid).blockers or ())]
    check(len(_wids) == 1, "前置：那件事被 1 条等待挡着", f"实际 {len(_wids)}")
    for _w in _wids:
        W.resolve_wait(_w, "test")
    _txt_after = om._rt_ongoing_work(None)
    check("pip" not in _txt_after,
          "⭐⭐⭐ 等待结束后那句话**自动没了** —— "
          "🔴 这正是实测撞到的那个 bug 的反面：复制进去的那份不会自己走",
          _txt_after.replace(chr(10), " | ")[:80])


def t_failsafe_direction(tmp: pathlib.Path) -> None:
    print("\n[5] ⭐ fail-safe 方向：朝「少说一句」错，不朝「多断言一件事」错")
    make_kernel(tmp / "e")
    import core.orchestrator as om
    from core.runtime import kernel as _kmod

    _saved = _kmod.get_kernel
    try:
        def _boom():
            raise RuntimeError("内核读不出来")
        _kmod.get_kernel = _boom
        check(om._rt_has_live_work(None) is False,
              "⭐⭐ 读不出来 → 判「没有」→ **工具不注入**。"
              "反过来错的代价是模型宣布一个假的完成，那是个假事实")
        check(om._rt_ongoing_work(None) == "", "事实段也退成空串（同方向）")
        check(T.owner_label() is None, "`owner_label()` 也退成 None")
    finally:
        _kmod.get_kernel = _saved


def t_creation_failure_isolated(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐ 建不出主人时，归属物照旧要办成")
    k = make_kernel(tmp / "f")
    _saved = T.ensure_conversation_task
    try:
        def _boom(goal_summary: str = ""):
            raise RuntimeError("建 Task 炸了")
        T.ensure_conversation_task = _boom
        wid = _open_wait(_susp("s1"))
        check(bool(wid),
              "⭐⭐ 主人建不出来 → **等待照旧登记成功**（owner 留空）。"
              "接一个新实体时，它的失败不该让已经能用的东西停摆", str(wid))
        rec = W.get(k, wid)
        check(rec is not None and rec.owner_task_id is None,
              "owner 是 None —— 老实留空，不编一个值")
    finally:
        T.ensure_conversation_task = _saved


def t_source_invariants() -> None:
    print("\n[7] ⭐⭐ 源码不变量：把规则绑在能被检查的地方")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)

    import asyncio as _aio
    import core.orchestrator as _om
    from core.tools import Preload as _PL, Scheduling as _SC, ToolScope as _TS
    from core.tools.builtin import build_builtin_definitions as _bbd
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    _defs = {d.name: d for d in _bbd(_mans)}
    _tb = _defs["task_boundary"]

    # ① 条件注入：**没有在进行的事时，它不许出现在工具清单里**。
    # ⚠️ 换锚点：改造前这是「`_TASK_BOUNDARY_MANIFEST` 的 append 必须包在
    #    `if _rt_has_live_work(self):` 里」——一条**关于代码长什么样**的断言，
    #    而那个 if 已经随 `_build_skills_info` 一起删除（条件搬进了声明的
    #    `availability` 字段）。
    # ⭐ 现在直接量**结果**：切换运行时事实，看它出不出现。
    #    这比查 if 强 —— 加第二个注入点时，查 if 的写法拦不住，量结果的拦得住。
    class _RTNoWork:
        def has_live_work(self): return False
        def has_open_interaction(self): return False
        def is_recheck_round(self): return False

    class _RTWork(_RTNoWork):
        def has_live_work(self): return True

    from core.tools import ToolCatalog as _TC
    _cat = _TC()
    for _d in _defs.values():
        _cat.add_builtin(_d)
    _no = {d.name for d in _cat.advertised(_TS.MAIN, _RTNoWork())}
    _yes = {d.name for d in _cat.advertised(_TS.MAIN, _RTWork())}
    check("task_boundary" not in _no,
          "⭐⭐⭐ 没有在进行的事时 `task_boundary` **不进工具清单** —— "
          "常驻会诱导模型去「找一件事来结束」，而它结束的会是一件不存在的事")
    check("task_boundary" in _yes,
          "⭐ 有在进行的事时它才出现 —— "
          "📌 **一个工具和它的事实来源，必须由同一个条件控制**"
          "（半截状态会让模型开始猜）")

    # ⚠️⚠️ 下面整段换了锚点。旧写法是**对源码做字符串切片**：
    #    切 `elif name == "task_boundary":` 到 `elif name == "cancel_wait":` 之间那段，
    #    再在里面找 `_is_failed = True` / `"abandoned"` 这些字面量。
    #    ⭐ 把分支体机械提取进 `_handle_task_boundary` 之后，那一段只剩
    #       三行调用桩 —— **这两条断言从那时起就一直是红的**（cutover 前跑基线
    #       才发现；早先的设计写的"全量 0 失败"是被一个有 bug 的汇总脚本放过的：
    #       它对 `X passed, Y failed` 验了 Y==0，对 `77/79 通过` 却只验了
    #       "有没有汇总行"，从没比过 77 是否等于 79）。
    # 📌 教训不是"该早点跑测试"，而是：**一条断言如果绑在「源码长什么样」上，
    #    它迟早会被一次重构打红，而那时红的不是代码、是锚点。**
    #    所以这里改成绑在「**行为是什么**」上 —— 直接调 handler 看它返回什么。
    #    这比数字面量强：措辞换个说法就失效的断言，本来就守不住"不许宣布完成"。
    # ② 不常驻（改造前查的是 `_CORE_TOOL_NAMES` 这张手写表，现在是声明里的字段）
    check(_tb.preload is not _PL.CORE,
          "⭐⭐ `task_boundary` **不常驻** —— "
          "一个只在某种状态下才有意义的工具，不该每轮都背着它的 schema"
          "（既烧 token，又给模型一个在无意义时调用它的机会）", _tb.preload.value)

    # ③ 「四张注册表都登记了」→ 现在只有一处声明，齐不齐由构造期不变量保证
    check(bool(_tb.manifest and _tb.awareness and _tb.presentation
               and _tb.bindings and _tb.scheduling and _tb.flow),
          "⭐ 一处声明八个维度全填 —— 改造前这是**四张表**（manifest / 工具名单 / "
          "两处显示名 / 分发），漏一张的表现各不相同且都不报错；"
          "现在漏填在构造期就抛异常")

    # ④ 行为：收不到任何事时必须标失败 + 明确禁止向用户宣布完成
    _o2 = _om.Orchestrator.__new__(_om.Orchestrator)
    _out = _aio.run(_o2._handle_task_boundary(
        {"action": "finish", "outcome": "completed"}, "aid", event_queue=None))
    check(_out.failed is True,
          "⭐⭐ 行为：收不到任何在进行的事时**标失败** —— fail-safe 不能只写在注释里")
    check("Do NOT tell the user you finished" in _out.text,
          "⭐⭐ 行为：明确禁止向用户宣布完成 —— "
          "📌 这是 fail-safe 在**措辞层**的兑现：不许替用户编一个用户没做过的动作",
          _out.text[:60])

    # ④a 「放弃」这一档必须认得出（不许只有一种结局）
    check("放弃" in _tb.presentation.render_card(
              {"action": "finish", "outcome": "abandoned"})
          and "完成" in _tb.presentation.render_card(
              {"action": "finish", "outcome": "completed"}),
          "⭐ 认得出「放弃」这一档（不许只有一种结局）")

    # ④b 并发分类必须是**声明的** serial，不是兜底碰巧对的 serial
    check(_tb.scheduling is _SC.SERIAL,
          "⭐⭐ `task_boundary` 的调度值是**显式声明**的 SERIAL —— "
          "🔴 加它那天真的漏了这一处登记（靠 `_classify_tool_safety` 的兜底碰巧对）。"
          "📌 **一个靠兜底默认恰好正确的分类，和一个被声明为正确的分类，"
          "在兜底默认哪天改掉的那一刻就不再等价。** 现在它没有兜底可依赖了")
    check(_TS.MAIN in _tb.bindings,
          "⭐ 而且它在主决策里真有 handler（声明与执行同源）", _tb.bindings[_TS.MAIN])

    # ⑤ 交互的归属由 `mode` 决定，**不由调用点各写一次**
    # ⭐ 2026-08-28：3 → 4。第四处是 `_rt_open_mcp_manage`（MCP 删除确认）。
    #    ⭐⭐ **这条断言当天就兑现了它自己的承诺** —— 它写着「第四处开交互的代码
    #       就不会抄错」，而第四处真的出现时它响了一声，逼人回来确认新代码
    #       有没有照走同一个函数（走了）。
    #    📌 一条会随新增而变红的计数断言，价值不在数字，在**它逼你回来看一眼**。
    check(src.count("_rt_ia_owner(_it.Mode.DEFERRED)") == 4,
          "⭐⭐ 四处开交互全部走同一个 `_rt_ia_owner(mode)` —— "
          "把「要不要创建」写成 mode 的函数，第四处开交互的代码就不会抄错",
          f"{src.count('_rt_ia_owner(_it.Mode.DEFERRED)')} 处")
    k0 = src.index("def _rt_ia_owner(")
    k1 = src.index("\ndef ", k0 + 10)
    fseg = src[k0:k1]
    check("ensure_conversation_task" in fseg and "owner_label" in fseg,
          "⭐ 而这个函数里**两条路都在**：DEFERRED 走 ensure、INLINE 只贴标签")
    check(fseg.index("DEFERRED") < fseg.index("ensure_conversation_task"),
          "⭐ 创建那条路真的挂在 DEFERRED 上（不是反的）")

    # ⑥ 归属物那两类必须**分开调**：等待用 ensure，一轮内的东西用 owner_label
    wsrc = module_text("core.runtime.waitcond")
    check("ensure_conversation_task" in wsrc,
          "⭐ waitcond 用 `ensure_...`（有权创建）")
    for mod in ("inbox", "attempt", "toolbatch"):
        msrc = (ROOT / "core" / "runtime" / f"{mod}.py").read_text(encoding="utf-8")
        check("owner_label" in msrc and "ensure_conversation_task" not in msrc,
              f"🔴🔴 {mod}：只用 `owner_label()`、**绝不用 `ensure_...`** —— "
              f"这一格红了就意味着每条消息都会诞生一件事")


def t_pill_settle_from_authority() -> None:
    """⭐⭐⭐ [2026-08-09 实测] 等待 pill 的收尾 —— 措辞与时机都回权威。

    🔴 **实测现象**：Nano 自己调 `cancel_wait` 取消了那条定时（**取消是真的，
       不会再触发**），但 pill 照旧数完、然后翻成「等待中 · 即将继续」，
       用户强制点「继续」才看到「这个等待已经结束了」。
    ⭐ 用户的判断（成立）：「**结束 = UI 跟上 = 那个按钮不该存在**」。

    ⚠️⚠️ **根因不是「少发了一个事件」，是「收 pill 只挂在部分路径上」**：
       UI 取消按钮 ✅ / 用户发新消息 ✅ / **模型 `cancel_wait` ❌**
       （它在 orchestrator 里，改了权威却没有 UI 通道）。
    📌 而 `app.py` 那段镜像注释早就立了这条判据 ——
       「**镜像点要按「权威被改动的地方」去找，不是按模块去找**」。
       当时把**镜像**点数全了，却没对 **pill** 问同一个问题。
       📌 **一条判据只被用在它诞生的那个问题上，等于没立。**

    ⭐ 所以修法是把那个**本来就是 level-triggered** 的收尾接到时钟上，
       而不是给 `cancel_wait` 补发事件：
       📌 **level-triggered 的收尾对「以后又多一条取消路径」免疫，
          edge-triggered 的补发只修当前这一条。**
    """
    print("\n[8] ⭐⭐⭐ 等待 pill 收尾：时机与措辞都回权威读")
    app = module_text("app")
    alive = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    tree = ast.parse(app)

    # ① 收尾必须挂在 tick 上（level-triggered）
    tick = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_suspension_tick":
            tick = n
    check(tick is not None, "前置：找到 `_suspension_tick`（5 秒一轮）")
    tsrc = ast.unparse(tick) if tick else ""
    check("_settle_all_waiting_pills" in tsrc,
          "⭐⭐⭐ AST：pill 收尾**挂在 5 秒 tick 里** —— "
          "它逐条回查权威、只收真的不在 active 里的，"
          "所以**任何**杀掉等待的路径（含以后新增的）都会被收到")

    # ② 措辞必须从权威读，不由调用方给
    words = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_pill_settle_words":
            words = n
    check(words is not None, "前置：找到 `_pill_settle_words`")
    # ⚠️ 用 `_code_of` 而不是 `ast.unparse` —— 见那个函数的说明（第三次撞同一个形状）。
    wsrc = _code_of(words) if words else ""
    # ⚠️⚠️ **这三条在 2026-08-09 实测 之后重写过。**
    #    第一版断言的是「读了 `rec.get('status')` + `resolved_by`」——
    #    **那两个字段名对，但数据源错了**：它读的是切读期那个有损兼容投影，
    #    而那层投影明写着「`status`：新的六态**折成旧的两态**」，
    #    于是一条 `CANCELLED` 出来是 `"resolved"` → pill 落到兜底文案「▶ 继续」（绿色）。
    # 🔴 **而第一版断言【通过了】** —— 它验的是「有没有读那两个字段」，
    #    不是「读到的东西分不分得出取消」。
    # 📌 **一条只验「读了哪个字段」的断言，不能替你验「那个字段答不答得出你的问题」。**
    _w = wsrc.replace('"', "'")
    check("find_by_id" in _w,
          "⭐⭐⭐ 它读的是 **`waitcond.find_by_id`（不折叠的权威六态）**，"
          "**不是**切读期的兼容投影（六态折两态）—— "
          "📌 **一个兼容层刻意丢掉的信息，不会因为下游需要它而回来**；"
          "📌 **「读权威」不只是「别读缓存」，还包括「别读一个降了分辨率的投影」**")
    check("WaitStatus" in _w and "CANCELLED" in _w and "ORPHANED" in _w,
          "⭐⭐ 六态里该分开的都分开了（取消 / 过期 / 等的东西没回来）—— "
          "而 `ORPHANED` 那一档刻意用**警示色**：它意味着等的东西再也没回来，"
          "是被兜底回收的，用户有权知道这不是正常收尾")
    check("satisfied_by" in _w and "background" in _w and "timer" in _w,
          "⭐ 满足那一档也回权威读 `satisfied_by`（不退回兜底）—— "
          "否则 tick 抢在唤醒路径前面时会用通用措辞盖掉更具体的那句，"
          "而那种降级**只在偶发时出现**，是最难查的一类")
    check("model" not in _w.replace("model_", ""),
          "⭐⭐ **刻意不区分「是谁取消的」** —— 权威里压根没记这件事"
          "（`resolution` 是固定枚举，取消入口传的 `model-cancel` "
          "进的是 `payload['reason']`，不落库）。"
          "📌 **一行系统注记多说一句，就多一个可能为假的断言** —— "
          "而是谁取消的就写在上面那句 Nano 的话里，pill 不必替它说")

    # ②b 🔴 **那两个 async 方法必须还在** —— 2026-08-09 真的把它们删掉过一次：
    #    用 `"    def "` 当替换范围的右边界，而它们是 `    async def`，
    #    前缀不匹配 → 被跳过并**静默删掉**，而且 `py_compile` 照样通过
    #    （删掉两个方法不影响语法），是这条测试把它抓出来的。
    # 📌 **替换范围必须由结构（AST）划定，不许用字符串前缀猜** ——
    #    这是同一天第四次栽在「范围用文本定」上（前三次：断言窗口太宽 /
    #    太窄 / 拿注释当右边界）。
    check("async def _wake_now" in app and "async def _cancel_suspension" in app,
          "🔴🔴 `_wake_now` / `_cancel_suspension` **两个方法都在** —— "
          "它们是等待 pill 上「立即继续」「取消」两颗按钮的 handler，"
          "删掉后语法照旧正确、点下去才 AttributeError")
    check(app.count("self._wake_now(") >= 1 and app.count("self._cancel_suspension(") >= 1,
          "⭐ 而且按钮的调用点也还在（定义和调用必须成对存在）")

    # ③ 那句「这个等待已经结束了」的兜底**必须留着**
    check("这个等待已经结束了" in alive,
          "⭐⭐ 而 `_wake_now` 里那句兜底**刻意保留** —— "
          "当时的要求是：「一旦出现（万一），让点击出现这个已经结束了，"
          "而不是直接卡死或者僵尸按钮」。"
          "📌 机制负责让它不发生，兜底负责让它发生时不至于卡死 —— "
          "删掉兜底等于假设投影永远不会滞后，而那正是本项目反复栽的地方")


def t_stale_conversation_closed(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ [2026-08-09 实测] 空壳对话 Task 的回收 —— 懒创建的【对称面】。

    🔴 **实测证据**：整份日志里**没有一行 `[Task] 新开一件事`**，
       却有 `[Task] 一件事收尾 task_d093a2b6ccd6 → COMPLETED` ——
       那个 Task 是**更早一次运行留下来的**，跨了至少一次重启还活着。
       后果：一件三天前的事、名下什么都没有，`[Ongoing work]` 照旧报
       「You currently have these things in progress: … 等提醒」→
       **Nano 会跟用户说它还在办一件早就没了的事。**

    📌 **懒创建的对称面**：一个「只为给别人当主人而存在」的实体，
       在最后一个孩子消失之后就没有存在理由了。当时建了**诞生条件**，
       没建它的**反面**。

    ═══ ⚠️ 这条判据走过两版错的，两版都是既存断言判红的 ═══
    **第一版**：放在启动恢复里、「名下空了就收」。
      → 被 `t_f1_stage7_guitask` + `t_f1_stage1_runtime`[7] 判红，而**它们是对的**：
        🔴 **「名下空了」≠「没有在进行的事」** —— 死在 `RUNNING` 里的 Task
           之所以没有 blocker，**正是因为它当时在干活而不是在等**。
    **第二版**：加「刚被打断的宽限一次」。
      → 被「第二次启动恢复不再处理它（幂等）」判红：
        🔴 它让结果取决于**启动了几次**，而不是取决于**状态** ——
           **edge-triggered 思维溜了进来**。
    **第三版（本组测的）**：TTL + tick。
      📌 **区分「刚发生」和「早就没了」要用时间，不用「第几次启动」** ——
         后者不是状态，它是历史。时间对启动次数、进程边界、调用顺序全都免疫。
    """
    print("\n[9] ⭐⭐⭐ 空壳对话 Task：TTL + tick 回收（懒创建的对称面）")
    k = make_kernel(tmp / "z")

    # ── ① 有 blocker → 多久都不许收 ──────────────────────────────────────
    wid = _open_wait(_susp("s1", reason="等 pip 装完",
                                              timer_at=BASE_T + 99999))
    tid = T.current_conversation_task(k)
    check(bool(tid), "前置：等待建出了一件事")
    k.clock.advance(T._STALE_CONV_TTL_SECONDS * 10)
    check(T.sweep_stale_conversations(k) == 0,
          "⭐⭐⭐ **有 blocker → 放十倍 TTL 也不收** —— "
          "有东西需要它当主人，它就有存在理由")
    check(T.get_task(k, tid).lifecycle == T.Lifecycle.ACTIVE, "它还活着")

    # ── ② 没 blocker 但还新鲜 → 不许收（第一版那个错） ────────────────────
    W.resolve_wait(wid, "timer")
    check(not T.get_task_view(k, tid).blockers, "前置：blocker 已经空了")
    # 收尾那一下 touch 了这行，所以它此刻是「刚动过」
    check(T.sweep_stale_conversations(k) == 0,
          "⭐⭐⭐ **名下空了、但刚动过 → 不收** —— "
          "🔴 这正是第一版犯的错：「名下空了」≠「没有在进行的事」，"
          "一个刚才还在干活的 Task 之所以没 blocker，是因为它在干活而不是在等")

    # ── ③ 没 blocker 且放久了 → 收，且原因必须是 EXPIRED ──────────────────
    k.clock.advance(T._STALE_CONV_TTL_SECONDS + 1)
    check(T.sweep_stale_conversations(k) == 1,
          "⭐⭐⭐ **名下空了 + 放过 TTL → 收掉**（这就是那个僵尸的下场）")
    r = T.get_task(k, tid)
    check(r.lifecycle == T.Lifecycle.TERMINAL, "进终态", r.lifecycle)
    check(r.terminal_reason == T.TerminalReason.EXPIRED,
          "⭐⭐ 原因是 **EXPIRED** —— 不是 COMPLETED（我们并不知道那件事成了没）、"
          "不是 CANCELLED（没人取消过它）、不是 INTERRUPTED_BY_RESTART（它没被打断）。"
          "📌 **终态原因是一句事实陈述，不许拿一个「差不多」的值凑**",
          str(r.terminal_reason))
    check(T.current_conversation_task(k) is None,
          "⭐ 「当前那件事」回到 None —— 用户下次说话由懒创建新建一个")
    check(T.conversation_tasks_for_model(k) == "",
          "⭐⭐ 注入段变回**空串** —— 这就是这个 bug 的真实危害面（假陈述消失了）",
          repr(T.conversation_tasks_for_model(k)[:40]))

    # ── ④ 两者仍然同真同假（别把 [4] 那条配对性质弄坏） ────────────────────
    import core.orchestrator as om
    check(om._rt_has_live_work(None) is False and om._rt_ongoing_work(None) == "",
          "⭐⭐⭐ 而且**工具与事实仍然同真同假** —— "
          "修一个「该收没收」的 bug 时最容易顺手弄坏的就是这条配对性质")

    # ── ⑤ 幂等：再扫多少次都不再动它 ──────────────────────────────────────
    check(T.sweep_stale_conversations(k) == 0 and T.sweep_stale_conversations(k) == 0,
          "⭐⭐ 再扫两次都返回 0 —— **level-triggered 的幂等是天然的**："
          "它只看当前状态，而终态不可逆")

    # ── ⑥ 只碰 IDLE 的；RUNNING / PAUSED 一律不动 ─────────────────────────
    k2 = make_kernel(tmp / "z2")
    _open_wait(_susp("s2", timer_at=BASE_T + 99999))
    tid2 = T.current_conversation_task(k2)
    W.resolve_wait(W.list_live(k2, owner_task_id=tid2)[0].wait_id, "timer")
    k2.submit(Command(kind=T.SET_EXECUTION, subject_id=tid2,
                      payload={"task_id": tid2, "execution": T.Execution.RUNNING}))
    k2.clock.advance(T._STALE_CONV_TTL_SECONDS * 10)
    check(T.sweep_stale_conversations(k2) == 0,
          "⭐⭐ **`RUNNING` 的一律不碰** —— 它正在被推进，"
          "「名下暂时没东西」对一个在跑的 Task 是正常状态")

    # ── ⑦ 接线：tick 步骤真的登记了（否则又是一个「写好没人调」）─────────────
    from core.runtime import reconciler as _rec
    check("task_stale_conversations" in _rec._tick_steps,
          "⭐⭐⭐ tick 步骤**真的登记在 Reconciler 里** —— "
          "📌 一个写好但没人调的回收，比没写更坏："
          "没写时缺口可见，写了不接时缺口**看起来已经补上了**"
          "（`mcp_client.awareness_lines()` 的教训）")
    ksrc = module_text("core.runtime.kernel")
    check(ksrc.count("_task.install_reconcile(k)") == 2,
          "⭐ 而且**两条内核初始化路径都接了**（生产 + 测试重置）—— "
          "只接一条会让测试里永远看不到它",
          str(ksrc.count("_task.install_reconcile(k)")))

    # ── ⑧ TTL 取值必须有先例，不是新发明一个数 ────────────────────────────
    tsrc = module_text("core.runtime.task")
    check("_CLARIFICATION_TTL_SECONDS" in tsrc,
          "⭐ TTL 的取值在注释里指明了**项目内的同语义先例**（澄清交互的 2 小时）—— "
          "📌 一个新 TTL 应该先找已有的同语义先例，而不是另发明一个数")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_who_may_create(tmp)
        t_finish_and_start(tmp)
        t_owner_is_a_label(tmp)
        t_tool_and_facts_same_gate(tmp)
        t_failsafe_direction(tmp)
        t_creation_failure_isolated(tmp)
        t_source_invariants()
        t_pill_settle_from_authority()
        t_stale_conversation_closed(tmp)

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
