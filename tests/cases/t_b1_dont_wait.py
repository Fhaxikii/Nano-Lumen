# -*- coding: utf-8 -*-
"""`dont_wait` —— 「抛后台」的主语从一件 Task 换成**一次调用**。

═══ 这一套守的东西 ═══

  ① **「手头的活」是唯一判据，而它由 `next_step` 强制写下来**
     用户的三种情况，分界线全在这一个参数上：
       · pip 完才能改代码（有依赖）→ 下一步就是等它 → 填不出别的 → 不该调
       · pip 与改代码无关          → 下一步是「改代码」→ 填得出   → 该调
       · 只有 pip 这一件事          → 下一步就是等它    → 填不出   → 不该调
     📌 **「不等它」只有在「我有别的事要做」时才成立** ——
        把那个前提做成必填字段，模型就没法含糊过去。
     ⚠️ 这正是实测抓到的滥用（逢长任务就 `dont_wait`）的修法：
        不是在描述里写「别乱用」，是**让它乱用时无话可填**。

  ② **交还合同有两档，而它们的差别是【真的】写进那条等待记录的**
     在手头 → `["background","timer"]` + `intent=system_recheck`（会回看）
     不在手头 → `["background"]`       + `intent=detached`（只等完成信号）
     ⚠️ 本套件**真的开一条等待再读回来**，不是查源码里有没有那个字面量 ——
        📌 断言读的是代码的【形状】时，形状可以完全正确而它一跑就炸
        （`bridge.get_store` 那 28 项绿灯的教训）。

  ③ **Subagent永远走「不回看」那一档**
     早先的设计：agent 是「**无法被回看的后台任务**」——
     📌 一个为了隔离而存在的东西，不能有一条把它的过程灌回来的通路。

  ④ **`dont_wait` 之后那条载体才进抽屉**
     🔴 在此之前抽屉 Running 段只有一个生产者（Subagent），
        那正是 用户报的「后台任务只有Subagent能进去」。

  ⑤ **元信息行在「还没轮到它跑」的那段时间必须有人念**
     🔴 实测：插话后秒数一直是 0，前一轮跑完直接跳到 84s。
        `thinking · 0s` 是写死的字面量，而计时协程要等前一轮结束才启动。

用法：
  py -3.10 tests\\cases\\t_b1_dont_wait.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
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
from core.runtime.kernel import reset_kernel_for_tests, get_kernel
from core.runtime.store import RuntimeStore
from core.runtime import task as T
from core.runtime import waitcond as WC

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
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


class _Q:
    """收事件的假队列。⚠️ 只收不丢 —— 断言要看它到底发了什么。"""

    def __init__(self):
        self.items: list = []

    async def put(self, ev):
        self.items.append(ev)


def _orch():
    import core.orchestrator as O
    return O, O.Orchestrator.__new__(O.Orchestrator)


# ══════════════════════════════════════════════════════════════════════════
def t_schema_forces_next_step() -> None:
    print("")
    print("[1] ⭐⭐⭐ `next_step` 是**必填**，而它就是「手头的活」那个判据")
    import core.orchestrator as O
    from core.tools.manifests import _DONT_WAIT_MANIFEST as m
    check(m["parameters"].get("required") == ["next_step"],
          "⭐⭐⭐ **`next_step` 必填** —— 📌「不等它」只有在「我有别的事要做」时"
          "才成立；把那个前提做成必填字段，模型就没法含糊过去",
          str(m["parameters"].get("required")))
    _desc = m["description"]
    # 🔴 描述必须说**它改变了什么**（不再被打断），而不是"停止等待" ——
    #    📌 用模型侧的语言去描述一件系统侧的事，模型会判断它无事发生。
    check("Stop waiting for the slow call" not in _desc,
          "⭐⭐⭐ 第一句**不再是**「Stop waiting…」 —— "
          "🔴 控制权已经交还给它了，它主观上没在等，那句话对它是空的")
    check("stops interrupting you" in _desc and "woken up once" in _desc,
          "⭐⭐⭐ 换成**它能据以行动的后果**：不再被拉回来看、完成时叫你一次")
    check("BEFORE you start the other work" in _desc,
          "⭐⭐ 并写清**顺序** —— 📌 顺序错了这个工具就白调了")
    # 🔴 第二条实测教训：模型把「回头看看它好没好」当成 next_step ——
    #    那等于什么都没说（它还是在等它）。
    for _bad in ("check on it", "wait for it", "see if it finished"):
        check(_bad in _desc,
              f"⭐⭐ 描述里**点名**「{_bad}」不算「别的事」 —— "
              "📌 一个「必须填别的事」的字段，必须同时说清什么不算别的事，"
              "否则模型会用一个语义上等价于「我还是在等」的答案绕过去")
    check("woken up" in _desc or "wake" in _desc,
          "⭐ 并明说**系统会叫你** —— 不写这句，模型就会自己去安排一次回看")


def t_two_gears_are_really_written_down(tmp: pathlib.Path) -> None:
    print("")
    print("[2] ⭐⭐⭐ 交还合同两档：**真的开一条等待再读回来**")
    boot(tmp / "a")
    O, orch = _orch()

    async def _go():
        q1, q2 = _Q(), _Q()
        t1 = await orch._hand_back_long_task(
            display="pip install torch", bg_ref="ref_fg",
            action_id="a1", event_queue=q1)
        t2 = await orch._hand_back_long_task(
            display="Agent · 查点东西", bg_ref="ref_agent",
            action_id="a2", event_queue=q2, recheck=False, detachable=False)
        return q1, q2, t1, t2

    q1, q2, t1, t2 = asyncio.get_event_loop().run_until_complete(_go())
    k = get_kernel()
    live = {r.bg_ref: r for r in WC.list_live(k)}

    _fg = live.get("ref_fg")
    check(_fg is not None
          and set(str(w) for w in _fg.wake_on) >= {"background", "timer"},
          "⭐⭐⭐ **在手头 → 双源**（完成信号 + 到点回看）",
          str(getattr(_fg, "wake_on", None)))
    check(_fg is not None and _fg.intent == "system_recheck",
          "⭐ 而且意图是**被写下的**，不是从「有没有 timer」反推的 —— "
          "📌 与「不许由 `timer_at` 猜」那条逐字同形",
          str(getattr(_fg, "intent", None)))

    _ag = live.get("ref_agent")
    check(_ag is not None and "timer" not in set(str(w) for w in _ag.wake_on),
          "⭐⭐⭐ **不在手头 → 单源**：Subagent永远不回看 —— "
          "📌 回看 = 把它走过的步骤灌回主上下文，那正好抵消掉隔离的全部价值",
          str(getattr(_ag, "wake_on", None)))
    check(_ag is not None and _ag.intent == "detached",
          "⭐⭐ 它的意图是 `detached`，与 `system_recheck` 分得开 —— "
          "📌 两者的差别不是「有没有 timer」（那是后果），"
          "是**Nano 此刻还等不等它**",
          str(getattr(_ag, "intent", None)))
    check(_ag is not None and _ag.fire_at in (None, 0) or True, "（fire_at 不作断言）")

    # ⭐ 事件里给 UI 的那份也必须跟着分档，否则 UI 会画一个不存在的倒计时
    _e1 = [e for e in q1.items if e.get("event") == "suspend_waiting"]
    _e2 = [e for e in q2.items if e.get("event") == "suspend_waiting"]
    # ⭐⭐ 交还那段话必须**把出口连同条件一起说出来** ——
    #    ⚠️ 只说出口不说条件，等于邀请它逢长任务就调（那正是那次滥用）。
    #    📌 一个只在特定条件下才正确的动作，提示它的时候必须连条件一起提。
    # 🔴 实测 2026-08-20：模型**看得见** `dont_wait`（日志里 `dont_wait:1070`）
    #    却直接跳过它去调 `edit_file`。两处措辞各有一半责任：
    #      ① 工具描述第一句是 "Stop waiting…"，而控制权**已经交还**给它了 ——
    #         它主观上没在等，于是这个动作在它看来什么也不改变
    #      ② 交还那段话**先**给了出口（「你可以继续做不依赖它的步骤」），
    #         **后**才提条件 —— 📌 先给出口再给条件，它只会读到出口
    check("Decide now, before you do anything else" in t1,
          "⭐⭐⭐ 交还那段话**先要它做决定**，不再先递出口 —— "
          "🔴 上一版两句自相矛盾，实测它就照着前一句直接干活去了",
          t1[-200:])
    check("call `dont_wait` FIRST" in t1 and "one wrong answer" in t1,
          "⭐⭐⭐ 明说**顺序**（先调再做）+ 点名唯一的错误答案 —— "
          "📌 一个「顺序错了就白调」的动作，必须把顺序写进指引里")
    check("dont_wait" in t1 and "does not need" in t1,
          "⭐⭐⭐ 在手头那一档：交还时**点名 `dont_wait` 并写清条件** —— "
          "📌 模型不会因为「清单里有个工具」就想到该用它",
          t1[-160:])
    check("dont_wait" not in t2,
          "⭐⭐ Subagent那一档**一个字都不提** —— 它已经不在手头了，"
          "提一个「别等它」只会让模型对一件已经做完的决定再决定一次",
          t2[-80:])

    check(_e1 and _e1[0]["waiting_intent"] == "system_recheck"
          and _e2 and _e2[0]["waiting_intent"] == "detached",
          "⭐⭐ 发给 UI 的事件也带着**同一个**意图 —— "
          "📌 同一件事有两份说法时，它们只在「我两次想法相同」的前提下一致")


def t_dont_wait_only_appears_when_there_is_a_carrier(tmp: pathlib.Path) -> None:
    print("")
    print("[3] ⭐⭐ 它只在**有载体可交出去**的那一刻出现")
    boot(tmp / "b")
    O, orch = _orch()
    orch._pending_loaded_manifests = []
    orch._detachable_carrier = None

    async def _go():
        q = _Q()
        await orch._hand_back_long_task(
            display="pip install torch", bg_ref="ref_x",
            action_id="a1", event_queue=q)

    asyncio.get_event_loop().run_until_complete(_go())
    _names = [(m or {}).get("name") for m in orch._pending_loaded_manifests]
    check("dont_wait" in _names,
          "⭐⭐⭐ 交还那一刻**本轮中途并入 schema** —— "
          "📌 `availability` 是每轮开头算一次的，接不住「一轮的中途才出现的对象」",
          str(_names))

    # Subagent那一档**刻意不给**：它已经不在手头了
    O2, orch2 = _orch()
    orch2._pending_loaded_manifests = []
    orch2._detachable_carrier = None

    async def _go2():
        q = _Q()
        await orch2._hand_back_long_task(
            display="Agent · x", bg_ref="ref_y", action_id="a2",
            event_queue=q, recheck=False, detachable=False)

    asyncio.get_event_loop().run_until_complete(_go2())
    check([(m or {}).get("name") for m in orch2._pending_loaded_manifests] == [],
          "⭐⭐ Subagent那一档**不给** `dont_wait` —— 它已经不在手头了，"
          "再给一个「别等它」是让模型对一件已经做完的决定再决定一次。"
          "📌 一个工具只该出现在「它还能改变什么」的时刻",
          str([(m or {}).get("name") for m in orch2._pending_loaded_manifests]))

    # ⭐ 非回看轮不常驻（否则模型会拿它去「不等」一件不存在的事）
    from core.tools.builtin import build_builtin_definitions
    # ⚠️ 键是**工具名**，不是变量名 —— `build_builtin_definitions` 按工具名查。
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    _defs = {d.name: d for d in build_builtin_definitions(_mans)}
    _dw = _defs.get("dont_wait")
    check(_dw is not None, "⚠️ 前置：`dont_wait` 在工具目录里")

    class _RV:
        def __init__(self, rk):
            self._rk = rk

        def is_recheck_round(self):
            return self._rk

        def __getattr__(self, _n):
            return lambda *a, **k: False

    from core.tools.catalog import ToolScope
    check(_dw is not None and not _dw.is_eligible(ToolScope.MAIN, _RV(False)),
          "⭐⭐⭐ **非回看轮不出现** —— 📌 没有载体时给出这个工具，"
          "模型只会拿它去「不等」一件不存在的事")
    check(_dw is not None and _dw.is_eligible(ToolScope.MAIN, _RV(True)),
          "⭐⭐ 而**回看轮出现**：「看了一眼，还早得很，我不等了」是它的第二个时刻")


def t_handler_really_runs(tmp: pathlib.Path) -> None:
    print("")
    print("[4] ⭐⭐⭐ 真的 await 那个 handler（不是查 manifest 里有没有那串字）")
    boot(tmp / "c")
    O, orch = _orch()
    orch._pending_loaded_manifests = []
    orch._detachable_carrier = None
    orch._recheck_sid = ""

    async def _go():
        q = _Q()
        # ① 没有载体 → 如实说没有
        r0 = await orch._handle_dont_wait({"next_step": "改代码"}, "a0", event_queue=q)
        # ② 有载体、但没填 next_step → 拒绝（这就是滥用闸）
        await orch._hand_back_long_task(
            display="pip install torch", bg_ref="ref_z",
            action_id="a1", event_queue=q)
        r1 = await orch._handle_dont_wait({}, "a1", event_queue=q)
        # ③ 正常
        r2 = await orch._handle_dont_wait(
            {"next_step": "改 config.py 里的超时"}, "a2", event_queue=q)
        # ④ 一次性：再调一次就没有对象了
        r3 = await orch._handle_dont_wait({"next_step": "别的"}, "a3", event_queue=q)
        # ⑤ **指向还在、但那条等待已经终态** —— 记录点存在 ≠ 对象还在。
        #    🔴 不验的话 `reschedule_wait` 静默返回 False，而模型收到「办好了」，
        #       然后去做 next_step 并以为有人会叫它回来。
        WC.cancel_wait(_stale, resolved_by="test")
        orch._detachable_carrier = {"wait_id": _stale, "bg_ref": "ref_stale",
                                    "display": "早就完事的那个", "action_id": "a4"}
        r4 = await orch._handle_dont_wait({"next_step": "改代码"}, "a4", event_queue=q)
        return q, r0, r1, r2, r3, r4

    # 先造一条、再让它终态 —— ⚠️ 用真的记录，不是编一个 id：
    #    📌 编一个不存在的 id 只能验「查不到」，验不了「查到了但它已经结束」。
    _stale_rec = None
    try:
        from core.orchestrator import _rt_wait_open as _open
        _stale_rec = _open(reason="still running: 早就完事的那个",
                           wake_on=["background"], bg_ref="ref_stale",
                           intent="detached")
    except Exception:
        pass
    _stale = getattr(_stale_rec, "wait_id", "") or ""

    q, r0, r1, r2, r3, r4 = asyncio.get_event_loop().run_until_complete(_go())
    check(r0.failed and "nothing to stop waiting for" in (r0.text or ""),
          "⭐⭐ 没有载体 → **如实说没有 + 禁止宣布** —— "
          "📌 fail-safe 朝「少说一句」错，不朝「多断言一件事」错",
          (r0.text or "")[:50])
    check(r1.failed and "next_step" in (r1.text or ""),
          "⭐⭐⭐ **填不出 `next_step` 就调不动它** —— 这就是那次滥用的修法："
          "不是在描述里写「别乱用」，是让它乱用时无话可填",
          (r1.text or "")[:60])
    check(not r2.failed,
          "⭐⭐ 填了就放行", (r2.text or "")[:40])
    check("Go do this now: 改 config.py 里的超时" in (r2.text or ""),
          "⭐⭐ 回话里**把它自己写的下一步顶回它脸上** —— "
          "📌 一个只在参数里出现过的承诺，模型下一步就会忘",
          (r2.text or "")[:80])
    check("Do NOT schedule or perform a check" in (r2.text or ""),
          "⭐⭐⭐ 并**堵死「把回看当下一步」那条退路** —— "
          "🔴 那是实测抓到的第二个 bug：模型把「回头看看它好没好」当成下一步，"
          "于是 `dont_wait` 白调一次")
    check(r3.failed,
          "⭐⭐ **一次性**：用掉就清 —— 📌 不清的话，下一次 `dont_wait` 会作用在"
          "一条早就结束的等待上，而且不报错")

    check(bool(_stale) and r4.failed,
          "⭐⭐⭐ 指向**已终态**时如实说没有，不宣布 —— "
          "📌 一个「稍后使用」的指向，使用前必须问一次它指的东西还在不在；"
          "否则失败会以【成功】的形状返回",
          (r4.text or "")[:50])

    # ⑥ 回看真的被撤了（读回那条记录，不是看返回值）
    _live = {r.bg_ref: r for r in WC.list_live(get_kernel())}
    _z = _live.get("ref_z")
    check(_z is not None and _z.fire_at in (None, 0),
          "⭐⭐⭐ **那条等待的回看真的被撤掉了** —— 读记录，不是读返回值",
          f"fire_at={getattr(_z, 'fire_at', 'gone')}")

    # ⑦ 它告诉了 UI（否则抽屉里永远看不到它）
    _det = [e for e in q.items if e.get("event") == "carrier_detached"]
    check(len(_det) == 1 and _det[0]["bg_ref"] == "ref_z",
          "⭐⭐ 发了 `carrier_detached` —— 那是抽屉 Running 段的**第二个生产者**",
          str([e.get("event") for e in q.items]))


def t_agent_goes_through_the_same_contract() -> None:
    print("")
    print("[5] ⭐⭐⭐ Subagent走**同一条**交还合同，且永远停在「不回看」那一档")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    fn = next((f for f in ast.walk(tree)
               if isinstance(f, ast.AsyncFunctionDef)
               and f.name == "_handle_spawn_agent"), None)
    check(fn is not None, "⚠️ 前置：找得到 `_handle_spawn_agent`")
    body = ast.unparse(fn) if fn else ""
    check("_hand_back_long_task" in body,
          "⭐⭐⭐ 它**不另写一套**交还 —— 📌 交还合同的 docstring 早就写着"
          "「MCP / OS 命令 / **以后的Subagent**，全走这里」")
    check("recheck=False" in body and "detachable=False" in body,
          "⭐⭐⭐ 而且是「不回看 + 不可再 detach」那一档")
    check("_AGENT_HANDBACK_SEC" in body,
          "⭐ 干等的上限取自常量，不是写死在这里的数字")
    check("_wait_or_user_speaks" in body,
          "⭐⭐ **先等一小会儿**：5 秒内跑完的Subagent照旧同步返回 —— "
          "📌 一个阈值防的是「干等」，不该让不干等的那些也改变行为。"
          "⚠️ 而且等的是「它跑完 **或** 用户又说话了」：📌 一个「我还要不要干等」"
          "的阈值，在用户已经开口之后就失去了前提")
    # ⭐ 快路径与慢路径**都要能回到 main agent**
    check("_task.result()" in body,
          "⭐ 快路径直接把结果当 tool_result 回去（与改造前逐字一致）")
    # 🔴 归属：`begin_agent` 必须在Subagent自己那条协程里 set
    _runner = next((f for f in ast.walk(fn) if isinstance(f, ast.AsyncFunctionDef)
                    and f.name == "_agent_runner"), None) if fn else None
    check(_runner is not None and "begin_agent" in ast.unparse(_runner),
          "⭐⭐⭐ **`begin_agent()` 在Subagent自己那条协程里** —— "
          "🔴 留在 handler 里的话，交还之后 main agent 继续跑的模型调用会接着落在"
          "同一个 context 上，被记进这个Subagent的账。"
          "📌 一个用来「归属」的 ContextVar，必须在它所归属的那条执行链里 set")
    # 🔴 一条记录只能有一个收尾人
    check("owns_record=False" in body,
          "⭐⭐⭐ Subagent**自己**收权威记录，载体那层不许再收一次 —— "
          "📌 两个都收的表现是「后收的把先收的盖掉」，而且不会报错")
    check("cancelled" in body,
          "⭐⭐ `cancelled` 与 `failed` 分得开 —— "
          "📌 用户主动停掉不是失败；归进 failed 会让人去排查一个不存在的问题")


def t_pending_epoch_row_is_written_by_someone() -> None:
    print("")
    print("[6] 🔴 插话之后那一行**有人念**（秒数卡 0 再跳 84 的那个）")
    import app as _app
    src = module_text("app")
    tree = ast.parse(src)
    _defs = {f.name for f in ast.walk(tree)
             if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_pending_epoch_timer" in _defs, "⚠️ 前置：有这个协程")

    # ⭐ 它真的被**在忙的那条路上**启动 —— 只查函数存在的话，
    #   零调用方的写法看起来跟接好了一模一样（那笔债的形状）。
    # ⚠️ 那段建气泡的代码住在 `start_pipeline_task` 里（不是 `send_message`）——
    #    📌 回代码核实过；照印象写函数名的话，这条断言会**永远为假而不报错**。
    fn = next((f for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
               and f.name == "start_pipeline_task"), None)
    check(fn is not None, "⚠️ 前置：找得到建那个气泡的函数")
    _started = False
    for n in ast.walk(fn) if fn else []:
        if isinstance(n, ast.If) and "_rt_inbox_busy" in ast.unparse(n.test):
            if "_pending_epoch_timer" in ast.unparse(n):
                _started = True
    check(_started,
          "⭐⭐⭐ **忙的时候真的把它起起来** —— "
          "🔴 改造前那段等待期【零个协程】在写这一行，于是 `thinking · 0s` "
          "那个写死的字面量一直挂着，直到前一轮跑完才一次跳到 84s")

    # ── ⭐⭐ 真的跑一遍那个协程（不是只看结构）────────────────────────────
    class _Lbl:
        def __init__(self):
            self.text = "thinking · 0s"

        def set_text(self, t):
            self.text = t

    class _Host:
        _QUEUED_NOTICE_SEC = _app.WebUI._QUEUED_NOTICE_SEC
        _pending_epoch_timer = _app.WebUI._pending_epoch_timer

        def _ui_scope(self):
            return contextlib.nullcontext()

    import time as _time
    _lbl = _Lbl()
    _state = {"running": True, "pending_epoch": True, "status_lbl": _lbl,
              "spin_lbl": None, "start_time": _time.time()}

    async def _early():
        h = _Host()
        _t = asyncio.ensure_future(h._pending_epoch_timer(_state))
        await asyncio.sleep(0.4)
        _early_text = _lbl.text
        # 把表往前拨 —— ⚠️ 不真的睡 5 秒：📌 一条测试如果靠等真实时间，
        #    它会在跑全量时因为机器忙而变红，而那和它要证的事毫无关系。
        _state["start_time"] = _time.time() - 30
        await asyncio.sleep(0.4)
        _late_text = _lbl.text
        _state["pending_epoch"] = False
        await asyncio.sleep(0.3)
        _t.cancel()
        return _early_text, _late_text

    _early_text, _late_text = asyncio.get_event_loop().run_until_complete(_early())
    check(_early_text == "thinking · 0s",
          "⭐⭐ 头 5 秒**一个字都不改** —— 📌 一个只在异常时才需要出现的状态，"
          "不该在正常路径上闪一下（插话后立刻被接上是常态）",
          _early_text)
    check(_late_text.startswith("queued · ") and _late_text != "queued · 0s",
          "⭐⭐⭐ 超过之后**秒数真的在走，措辞也是真话** —— "
          "📌 它没在 thinking，它在排队；假事实的修法是换成真话，不是把话删掉",
          _late_text)

    # ⭐ 同一回应期只允许一个计时协程写该元信息行 —— 两者共用同一个键，
    #   于是 `navigate_pipeline` 开头那段「换班前先取消旧的」天然管住它。
    check(src.count('["_status_timer_task"]') >= 2,
          "⭐⭐ 它存在**同一个** `_status_timer_task` 键里 —— "
          "📌 定死的一条：同一回应期只允许一个计时协程写该元信息行",
          str(src.count('["_status_timer_task"]')))


def t_user_speaking_cuts_the_wait_short() -> None:
    """🔴🔴 实测 2026-08-20：插话之后 `queued` 挂了**五十多秒**。

    那不是排队态画错了，是**它真的排了那么久** —— 长任务的前台等待是 90 秒定长
    （`SlowProgressTest` 跑 170s），插话只能在队列里干等它到点。
    📌 **一个「我还要不要干等」的阈值，在用户已经开口之后就失去了前提** ——
       它防的是「没有更值得做的事时白等」，而用户说话恰恰说明有了。
    ⭐ 这不是新机制：`run_command` 早就这么做了（`_new_user_input_arrived`）——
       📌 又一次「正确做法已经在代码里，却没被推广到同类场景」。
    """
    print("")
    print("[7] 🔴 用户一开口，长任务的干等**立刻结束**（那 50 秒的来源）")
    import core.orchestrator as O
    from core.runtime import inbox as _ib

    _base = _ib.submit_seq()

    async def _never():
        await asyncio.sleep(30)

    async def _go():
        import time as _t
        _task = asyncio.ensure_future(_never())
        # ① 没人说话 → 老老实实等到 timeout（这里给 0.6s，证明它确实在等）
        _t0 = _t.time()
        _fin = await O.Orchestrator._wait_or_user_speaks(_task, 0.6)
        _elapsed_quiet = _t.time() - _t0

        # ② 有人说话 → 立刻返回（不等满 30 秒）
        _t1 = _t.time()
        _w = asyncio.ensure_future(
            O.Orchestrator._wait_or_user_speaks(_task, 30.0))
        await asyncio.sleep(0.05)
        _ib.submit_user_message("用户插了一句")   # ← 真的往 inbox 里塞一条
        _fin2 = await _w
        _elapsed_loud = _t.time() - _t1
        _task.cancel()
        return _fin, _elapsed_quiet, _fin2, _elapsed_loud

    _fin, _quiet, _fin2, _loud = asyncio.get_event_loop().run_until_complete(_go())
    check(_fin is False and _quiet >= 0.5,
          "⭐ 没人说话时**照旧等满阈值**（fail-safe 方向 = 继续等）",
          f"{_quiet:.2f}s")
    check(_fin2 is False and _loud < 3.0,
          "⭐⭐⭐ **用户一开口就立刻交还** —— 不等满 30 秒。"
          "🔴 这正是实测看到的那 50 多秒 `queued` 的来源",
          f"{_loud:.2f}s")
    check(_ib.submit_seq() > _base, "⚠️ 前置：那条插话真的进了 inbox")


# ══════════════════════════════════════════════════════════════════════════
# [] 实测 2026-08-22：长调用那次建模 落地后的五处修复
# ══════════════════════════════════════════════════════════════════════════

def t_cmd71_carrier_outlives_the_turn() -> None:
    """🔴 「把这个放后台」永远失败，因为载体的记录点只活一轮。

    实测：SlowProgressTest 交还之后，用户 **下一轮**说「把这个放后台」，
    `dont_wait` 回「There is no slow call handed back to you right now」——
    而那件事明明还在跑。Nano 随后还照着说了「已经在后台了」。

    ⚠️ **与载体类型无关**：命令/MCP/Skill 走同一个 `_hand_back_long_task`，
       `_detachable_carrier` 是**轮级状态**。实测里命令那两次成功，
       只是因为模型**当轮就调了** `dont_wait`。
    📌 一个防御措施的理由被另一个更精确的措施接管之后，它自己就该移除。
    """
    print("\n[cmd71-1] 🔴 可交载体必须活过一轮")
    orc = module_text("core.orchestrator")
    tree = ast.parse(orc)
    # 轮开始的状态接续在 `_turn_begin` 里（由 `_handle_query_impl` 第一个调用）
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_turn_begin"), None)
    check(fn is not None, "前置：找到 `_turn_begin`")
    if fn is None:
        return
    body = ast.unparse(fn)
    # 🔴 负向：不许再有无条件的 `self._detachable_carrier = None`
    #    ⚠️ 判的是**语句**不是文本 —— 注释里现在正写着这个赋值的历史。
    #    📌 按「字符串出现过」核，不算核（本项目栽过 6 次）。
    _bare = [n for n in ast.walk(fn)
             if isinstance(n, ast.Assign)
             and any(ast.unparse(tg) == "self._detachable_carrier" for tg in n.targets)
             and ast.unparse(n.value) == "None"]
    # 允许 else 分支里那一处（没有 wait_id 时清掉），但不许它是无条件的
    _uncond = []
    for _b in _bare:
        _parents = [n for n in ast.walk(fn)
                    if isinstance(n, (ast.If, ast.Try))
                    and any(_b is s for s in ast.walk(n))]
        if not _parents:
            _uncond.append(_b)
    check(not _uncond,
          "⭐⭐⭐ 不再**无条件**清空 `_detachable_carrier` —— "
          "📌 「是不是本轮」是近似物，「那条等待还活着吗」才是那个问题本身",
          f"仍有 {len(_uncond)} 处无条件清空")
    check("is_live" in body,
          "⭐ 清除改由 **liveness** 决定（记录点存在 ≠ 对象还在）")

    # ⭐ 第二半：工具的出现条件必须跟着载体走（那条）
    bt = module_text("core.tools.builtin")
    check("def _when_has_carrier(" in bt,
          "⭐⭐ 有一个「有活载体」的判据（回看轮 **或** 上一轮交还且还活着）")
    for _tool in ("dont_wait", "stop_background"):
        _i = bt.index(f'D("{_tool}"')
        _seg = bt[_i:bt.index("handler=", _i)]
        check("availability=_when_has_carrier," in _seg,
              f"⭐⭐ `{_tool}` 挂在「有活载体」上，不是「回看轮」—— "
              f"📌 一个只在「我刚看过」时才给的出口，答不了「用户现在开口了」",
              _tool)
    # 🔴 实测代价：`stop_background` 当时不在表里，Nano 改用 taskkill 按**进程名**杀
    check("has_detachable_carrier" in
          module_text("core.tools.catalog"),
          "⭐ 这个事实进了 `ToolRuntimeView` 的窄接口（不是直接摸 orchestrator）")
    # ⚠️ `set_next_checkin` **仍然**只在回看轮 —— 它问的是「我刚看过它」，
    #    是另一个问题。📌 答的不是同一个问题，就不合并。
    _i2 = bt.index('D("set_next_checkin"')
    check("availability=_when_recheck_round," in bt[_i2:bt.index("handler=", _i2)],
          "⚠️ `set_next_checkin` 刻意**没跟着改** —— 它问的是「我刚看过」，"
          "不是「有东西在跑」")


def t_cmd71_recheck_continues_into_the_same_bubble() -> None:
    """气泡合并判据。

        合并 <=> 最新气泡是 nano 的 AND **触发那一刻前台上有东西**

    ⚠️⚠️ **这条断言曾经验的是完全相反的东西**，来历要写清，否则下一个人会
    以为删掉那个指针是退化：
      旧判据比对 `_waiting_pills[sid]["resp_state"]` 与当前 `_resp_state` 的
      **对象身份**，而那个指针记的是【交还那一刻】是哪个回应期。
      回看轮和用户新消息**都会换掉** `_resp_state` —— 于是它一路过期：
      里 6 次唤醒只有 2 次续接成功；补了一条路径之后 又漏一次。
    📌 **一个需要多处同步才能保持正确的记录点，换成一个随时可直接读的事实。**
       补丁式地「再补一条路径」永远补不完，而且漏的时候不报错。

    ⭐ 为什么判据是「前台」而不是「有没有转圈的 pill」：
       后台的东西没完成时 pill **本来就该转**（UI 如实报事实）——
       那条 pill 说的是「它还没好」，不是「Nano 还在忙」。
       📌 **用一个回答 A 的信号去回答 B，它再准也是错的。**
    """
    print("\n[气泡] 合并 = 最新气泡是 nano 的 AND 触发那刻前台上有东西")
    app = module_text("app")
    tree = ast.parse(app)
    wake = "\n".join(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in ("_drive_wake", "_drive_wake_inner"))
    check("busy_at_trigger" in wake,
          "⭐⭐⭐ 判据是**触发那一刻**前台忙不忙（`busy_at_trigger`）—— "
          "⚠️ 不能在渲染那刻再问：排队的唤醒被排到时上一轮早已结束、锁也释放了，"
          "那时读到的是**另一个时刻**的答案，会把该并入的判成新气泡")
    check("_same_epoch = bool(busy_at_trigger)" in wake,
          "⭐⭐ 合并条件就是它 —— 不再有任何对象身份比对")
    check("_pill_entry['resp_state'] = self._resp_state" not in wake
          and '_pill_entry["resp_state"] = self._resp_state' not in wake,
          "🔴 **那个会过期的指针不许回来**（负向断言）—— 判据换掉后它再无读取点；"
          "📌 留着一个零读取点的写入，是「写好但没人调」的反面镜像："
          "它同样会让下一个人以为这里还有机制在生效")
    check("走了哪条路" in app,
          "⭐ 留痕：**为什么不用新加时间戳** —— 触发时忙走 `_park_wake` 进队列、"
          "闲则直接进来，**走了哪条路本身就是那个记录**")

    # ── 空气泡：插话时折叠「什么都没产出」的占位 ──────────────────────
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_handoff_response_epoch"), None)
    check(fn is not None, "前置：找到 `_handoff_response_epoch`")
    if fn is None:
        return
    body = ast.unparse(fn)
    check("_nothing_shown" in body,
          "⭐⭐ 折叠判据从 `pending_epoch` 放宽到「**它到底产出过没有**」—— "
          "📌 别用近似物回答一个能精确回答的问题")
    # ⚠️ 三样都要算进「产出」，少算一样就会删掉一个其实有内容的气泡
    for _fact, _why in (("current_text", "正文"),
                        ("tool_count", "工具卡"),
                        ("_waiting_pills", "已经画上去的等待 pill")):
        check(_fact in body,
              f"⚠️ 「产出」把 **{_why}** 也算进去 —— "
              f"📌 默认必须是**留着**，只有全空才折叠（删错比留个空头严重）",
              _fact)


def t_cmd71_wake_closes_its_own_inbox_row() -> None:
    """🔴 一条卡住的 CLAIMED，把整条队列的投递记账全废掉。

    实测：`inbox_consumed_was_delivered` 被破坏 **5 次**。
    `_drain_inbox` 的 wake 分支 `claim` 了一条记录，而消费只发生在
    `_safe_execute_pipeline` 的收尾里 —— **唤醒不走那个函数**。
    于是记录永远停在 `CLAIMED`，而 `_claim` 的第一道闸是
    「已经有一条 CLAIMED → 直接返回 None」→ 此后每一次认领静默失效。

    📌 `delivery_count` 存在的唯一理由，是回答「崩溃时这条给模型看过没有」。
    """
    print("\n[cmd71-3] 🔴 唤醒轮要收自己那条 inbox 记录")
    app = module_text("app")
    tree = ast.parse(app)
    outer = next((n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_drive_wake"), None)
    inner = next((n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_drive_wake_inner"), None)
    check(outer is not None and inner is not None,
          "⭐⭐ 唤醒是**壳 + 内层**两个函数")
    if outer is None or inner is None:
        return
    check(any(a.arg == "inbox_item_id" for a in outer.args.args),
          "⭐ 壳收 `inbox_item_id`（谁起的 turn，谁负责收）")

    # ⭐⭐⭐ 收尾必须在 `finally` 里，且**壳里只有一条路**
    _fins = [n for n in ast.walk(outer) if isinstance(n, ast.Try) and n.finalbody]
    check(bool(_fins) and any("_rt_inbox_consume" in ast.unparse(f2)
                              for n2 in _fins for f2 in n2.finalbody),
          "⭐⭐⭐ 消费挂在 `finally` 上 —— **这一轮怎么结束的都要收**")
    # 🔴 负向：内层里**不许**再各自补一次
    #    📌 逐出口补丁的正确性依赖「我数全了」；第一版就是逐个补，
    #       当场漏了「预算满」那条。包一层不依赖任何人记得。
    check("_rt_inbox_consume" not in ast.unparse(inner),
          "🔴 内层里**没有**逐出口的收尾 —— "
          "📌 里面有三条早退，逐个补的写法会随着将来新增 return 再次失效，"
          "而且失效时不报错",
          "内层里出现了 _rt_inbox_consume")
    # 前置：内层确实有多条早退（这条断言的价值就建立在这上面）
    _rets = [n for n in ast.walk(inner)
             if isinstance(n, ast.Return) and n.value is None]
    # ⚠️ 内层里是 **2** 条早退（内核忙 / 预算满）——「无处可排」那条 return
    #    在 `_park_wake` 里，不在这个函数。
    #    📌 写这条断言时把它数成了 3，红了才回代码看 ——
    #       **一个凭印象写下的数字，和一个查过的数字，长得一模一样。**
    # ⭐ 两条就已经够证明包一层的必要：第一版逐个补，漏的正是这两条里的
    #    「预算满」那条。**出口只要多于一个，"我数全了"就不再是可靠前提。**
    check(len(_rets) >= 2,
          "⚠️ 前置：内层确实有多条早退路径（内核忙 / 预算满）",
          f"实际 {len(_rets)} 条")


def t_cmd71_reply_survives_a_restart() -> None:
    """🔴 重启之后，引用回复的消息变回了普通消息。

    引用指向只活在内存里（`_reply_target` → `_reply_target_turn`），
    发出去就被本轮的 prompt 消费掉，**从来没跟着消息落过盘**。
    📌 与 `visible_to_user` / `ui_images` 同一条判据：
       一份账本同时被当作「模型上下文」和「用户看过的东西」时，
       它必须记下这两者的差别。
    """
    print("\n[cmd71-4] 🔴 引用要能活过重启")
    from core.schema import ChatMessage
    from core.runtime.conversation import _message_payload, _message_from_payload

    m = ChatMessage(role="user", content="把它停掉", reply_quote="跑 SlowProgressTest 120 秒")
    pl = _message_payload(m)
    check(pl.get("reply_quote") == "跑 SlowProgressTest 120 秒", "⭐⭐ 引用跟着消息落盘")
    check(_message_from_payload("user", pl).reply_quote == "跑 SlowProgressTest 120 秒",
          "⭐⭐ 读得回来（往返可逆）")
    # ⚠️ 新增一个可选事实，不该改写既有事实的形状
    check("reply_quote" not in _message_payload(ChatMessage(role="user", content="x")),
          "⚠️ 普通消息的 payload **一个字节都不变** —— 为空时不写")
    check(_message_from_payload("user", {"content": "x"}).reply_quote == "",
          "⚠️ 老行读回是空串，不是崩溃")
    # ⚠️ 存的是原文不是 id
    check("[:200]" in module_text("core.schema")
          .split("self.reply_quote")[1][:60],
          "⚠️ 截到 200 —— 引用是指路标，不是重新贴一遍")

    app = module_text("app")
    # ⭐⭐⭐ live 与重放**共用同一份**横幅实现
    check("def _render_quote_banner(" in app, "⭐⭐ 引用横幅抽成了一份实现")
    check(app.count("self._render_quote_banner(") == 2,
          "⭐⭐⭐ live 和重放**都调它** —— "
          "📌 同一个东西有两份实现，只在「我两次想法相同」的前提下一致"
          "（本项目栽过：上下文计量 vs 投影、工具详情的 live vs 重放）",
          f"实际 {app.count('self._render_quote_banner(')} 处")
    # 🔴 负向：重放那处不许再写死 ❯
    tree = ast.parse(app)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and "reply_quote" in ast.unparse(n)
               and "_user_label" in ast.unparse(n)), None)
    check(fn is not None, "前置：找到重放里画用户消息那处")
    if fn is not None:
        _b = ast.unparse(fn)
        check("_REPLY_PROMPT" in _b,
              "🔴 重放会画 `↳` —— 在此之前它**写死 `_NORMAL_PROMPT`**")
    # 生产端
    check("def attach_reply_quote(" in
          module_text("memory.manager"),
          "⭐ 落盘收口在 memory 那一侧（形状照抄 `attach_user_images`）")


def t_cmd71_model_prose_is_whitelisted_for_reply() -> None:
    """「回复」是白名单：只有 Nano 自己说的话能回复。

    🔴 此前条件是「在聊天区里」→ 系统报错 / 工具卡 / 用户自己的话都能回复。
    📌 白名单**不是**排除法：排除法的欠账随时间增长，
       而漏掉的那种不会报错 —— 它会安静地允许一个不该允许的动作。
    """
    print("\n[cmd71-5] 「回复」的白名单")
    app = module_text("app")
    check("def nano_md(" in app,
          "⭐ 模型正文收成一个函数 —— 📌 贴 class 那种写法，**第 10 处会忘**，"
          "而忘了的表现是「那条 Nano 说的话回复不了」，一个没人会报的静默缺失")
    check(app.count("nano_md(") >= 9,
          "⭐⭐ 模型正文全部走它", f"实际 {app.count('nano_md(')} 处")
    # ⚠️ 第一版是 `app[_err:_err + 900]` —— 靠**字符距离**框范围。
    #    2026-08-22 把卡片抽成 `render_sys_error_card()` 之后，那个窗口越过
    #    函数边界撞上紧随其后的 `def nano_md(` → **假阳性**（性质并没有破）。
    # 📌 **一条靠「字符距离」定位的断言，会在代码挪位置时变成假阳性。**
    # ⚠️⚠️ 而这条断言**当时在两个文件里各有一份**（这里 + `t_u3_chat_surface`），
    #    改的时候只改到了那一处 —— 📌 **一句出现在两处的东西，
    #    必然有一天只改到一处**，而这次的证据就是它自己。
    #    ⭐ 两处现在都换成**按 AST 取函数体**：问的是那个性质本身，
    #       而不是它在文件里的位置。
    _card_fn = next(
        (n for n in ast.walk(ast.parse(app))
         if isinstance(n, ast.FunctionDef) and n.name == "render_sys_error_card"),
        None)
    check(_card_fn is not None, "前置：错误卡片收成了一个函数（live 与重放共用）")
    _card_src = ("\n".join(app.splitlines()[_card_fn.lineno - 1:_card_fn.end_lineno])
                 if _card_fn else "")
    check(bool(_card_src) and "nano_md(" not in _card_src
          and "ui.markdown(" in _card_src,
          "🔴 System Error 卡片**不**带可回复标记（它不是 Nano 说的）—— "
          "用裸 `ui.markdown`，不走 `nano_md()`")
    check("saidByNano" in app and "commonAncestorContainer" in app,
          "⭐⭐ 判的是**整段选中**都在里面 —— "
          "📌 跨界的选中「回复的是哪一条」答不上来，答不上来就不给")



def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nanodw_"))
    try:
        t_schema_forces_next_step()
        for fn in (t_two_gears_are_really_written_down,
                   t_dont_wait_only_appears_when_there_is_a_carrier,
                   t_handler_really_runs):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
                traceback.print_exc()
        t_agent_goes_through_the_same_contract()
        t_pending_epoch_row_is_written_by_someone()
        t_user_speaking_cuts_the_wait_short()
        t_cmd71_carrier_outlives_the_turn()
        t_cmd71_recheck_continues_into_the_same_bubble()
        t_cmd71_wake_closes_its_own_inbox_row()
        t_cmd71_reply_survives_a_restart()
        t_cmd71_model_prose_is_whitelisted_for_reply()
    finally:
        close_all_stores()

    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
