# -*- coding: utf-8 -*-
"""· 拆澄清路由劫持 + answer_open_interaction（清单第 ②a / ③ 项）。

验的不是"内核对不对"（那是 `t_f1_stage3_interaction.py` 的事），
而是**接线对不对**：驱动真实的 `_run_react_loop` 与真实的 `_run_skill_exploration`，
让事件从模型决策一路跑到 SQLite。

═══ 注入层级 ═══

和 2A 注入套件同一套：patch `_stream_decision_core`（模型↔循环的边界）给循环喂剧本，
循环本身一行未改。理由见 ——如果直接调 kernel 命令，验的只是内核，
接线一行都没过。

用法：
  py -3.10 tests\t_f1_stage3_routing.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

import core.orchestrator as orch_mod
from core.orchestrator import Orchestrator
from core.schema import AgentDecision, ToolCall
from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import interaction as I
from memory.manager import MemoryManager

BASE_T = 1_800_000_000.0
_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ══════════════════════════════════════════════════════════════════════════
# 假件
# ══════════════════════════════════════════════════════════════════════════

class _FakeProvider:
    target_model = "fake-model"

    async def chat_with_tools_stream(self, *a, **k):
        yield {"type": "done", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}

    async def chat_without_tools_stream(self, *a, **k):
        yield {"type": "done", "text": "", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}


class _FakeRegistry:
    skills: dict = {}
    tools_manifest: list = []

    def get_permanent_manifests(self): return []
    def get_all_manifests(self): return []
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None


def make_orch(db_dir: pathlib.Path, t: float = BASE_T):
    orch_mod._rag_init_started = True
    clock = FakeClock(t)
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    k = reset_kernel_for_tests(store=st, clock=clock)
    o = Orchestrator(_FakeProvider(), _FakeRegistry(), MemoryManager(max_turns=20))
    o._rt_turn_id = "rtturn_test"
    o._tool_pool = {}
    o._core_manifest = []
    return o, k, clock


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


def script_decisions(o, decisions: list):
    seq = list(decisions)

    async def _fake(self, context, tools_manifest, system_guide, *, task_type,
                    stage_label, state, emit_done=True, allow_multi_tools=True):
        state.model = "fake-model"
        state.decision = seq.pop(0) if seq else AgentDecision("text", content="done")
        if False:
            yield {}

    o._stream_decision_core = _fake.__get__(o, type(o))




async def drive(o) -> list:
    evs = []
    agen = o._run_react_loop(
        tools_manifest=[], system_guide="sys", base_guide="base",
        realtime_callback=None, event_queue=asyncio.Queue(),
    )
    try:
        async for ev in agen:
            evs.append(ev)
    except Exception as e:
        evs.append({"event": "__raised__", "err": f"{type(e).__name__}: {e}"})
    return evs


def answer_call(iid: str, text: str, relation: str = "ANSWER"):
    return AgentDecision("call", name="answer_open_interaction",
                         args={"interaction_id": iid, "answer_verbatim": text,
                               "relation": relation},
                         tool_use_id="tu_ans")


# ══════════════════════════════════════════════════════════════════════════
# 1｜旧字段是否真的死透了
# ══════════════════════════════════════════════════════════════════════════

def t_field_removed(tmp: pathlib.Path) -> None:
    print("\n[1] `_pending_skill_clarification` 已从代码里消失")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)

    # ⭐ 用 AST 而不是文本匹配。文本匹配会被注释和 docstring 里的历史说明打中——
    # 本文件里就有 6 处提到这个名字的留档注释，全是有意保留的。
    # （这条纪律是 2A 那轮踩出来的：一个 docstring 里的示例把断言弄成了假失败。）
    assigned, read = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                if isinstance(t, ast.Attribute) and t.attr == "_pending_skill_clarification":
                    assigned.append(getattr(node, "lineno", -1))
        if isinstance(node, ast.Attribute) and node.attr == "_pending_skill_clarification" \
                and isinstance(node.ctx, ast.Load):
            read.append(getattr(node, "lineno", -1))
    check(not assigned, "没有任何地方给它赋值", str(assigned))
    check(not read, "没有任何地方读它", str(read))

    # 前置条件：确认 AST 分析确实在工作（纪律——别让"什么都没找到"假通过）
    other = [n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "_pending_skill"]
    check(len(other) > 5, "前置条件：AST 能在同一文件里找到 _pending_skill（分析有效）",
          f"命中 {len(other)} 次")

    # ⚠️ 同样只能用 AST。这三个名字在留档注释里被**故意**提到了
    # （"这里曾经是…被删掉的三样东西各自是一个 bug"），
    # 文本匹配会全部打中——第一版就是这么写的，三条里错了两条。
    dead_names = {"_CLARIFY_TIMEOUT", "_NEW_REQUEST_SIGNALS", "_SKILL_CLARIFY_WORDS",
                  "_abort_words"}
    live = sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and n.id in dead_names})
    check(not live, "关键词启发式的四个变量在代码里已不存在（只剩注释留档）", str(live))


# ══════════════════════════════════════════════════════════════════════════
# 2｜澄清问题 → Interaction
# ══════════════════════════════════════════════════════════════════════════

def t_clarify_opens_interaction(tmp: pathlib.Path) -> None:
    print("\n[2] Explorer 提问 → 登记 Interaction（不再是内存字段）")
    o, k, _ = make_orch(tmp / "a")

    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费",
        last_creation_note="文件已找到，阈值待定：>4000 还是 >=6000？", prompt_text="阈值用 >4000 还是 >=6000？")
    check(bool(iid), "返回了 interaction_id", iid)

    rec = I.get(k, iid)
    check(rec is not None and rec.kind == I.Kind.SKILL_CLARIFICATION, "kind 正确")
    check(rec.mode == I.Mode.DEFERRED, "DEFERRED —— 用户可以先不理，Nano 继续做别的")
    check(rec.durability == I.Durability.PERSISTED, "PERSISTED —— 跨重启存活")
    # ⚠️ 这条断言原来写的是 `rec.deadline_at is None`，理由"澄清永不静默超时"。
    # **实测 证伪了它**：没有任何过期机制 → 僵尸单调累积 →
    # deferred 槽 8 小时后躺满 5 条 → 之后每次登记都被不变量拒绝、静默退化成普通对话，
    # 整套澄清机制死掉而用户看不出来。
    #
    # 但原来的理由**不是全错**：旧实现的 10 分钟太短、而且是**静默**作废
    # （用户去泡杯茶回来，答案就没人接了）。真正的修法不是"永不过期"，
    # 而是"**足够长 + 过期留痕**"：2 小时 TTL，到期记成 EXPIRED 而不是悄悄消失。
    # 📌 又一次：把一个当时的判断写成了断言，它就把那个判断锁住了。
    check(rec.deadline_at is not None, "⭐ 澄清有 deadline（没有的话僵尸会占满槽位）")
    _ttl = rec.deadline_at - rec.created_at
    check(abs(_ttl - orch_mod._CLARIFICATION_TTL_SECONDS) < 5,
          f"TTL = {orch_mod._CLARIFICATION_TTL_SECONDS / 3600:.0f} 小时，远长于旧实现的 10 分钟",
          f"实际 {_ttl / 60:.0f} 分钟")

    # 审计刻意【不】设 deadline —— 两种 kind 的过期语义不同，
    # 用户可能真想把代码放几天再审，但不会回来答一个 8 小时前的提问。
    _audit = k.submit(Command(kind=I.OPEN, payload={
        "kind": I.Kind.SKILL_AUDIT, "prompt_text": "审这段代码"})).data["interaction_id"]
    check(I.get(k, _audit).deadline_at is None,
          "⭐ Skill 审计仍然不设 deadline（积压代码审查不是错误）")
    # ⚠️ 用完就关掉：这几个用例共用同一个 kernel，留着它会让后面
    # "没有待办时动态段是空串"那两条假失败。
    k.submit(Command(kind=I.CANCEL, subject_id=_audit,
                     payload={"interaction_id": _audit}))

    p = rec.payload
    # ⚠️ 2026-08-13：探索子循环拆除后 payload 从四个字段缩到两个。
    #    删掉的两个都是 Explorer 专属：`include_os` 是死链（整条路径没人能传 True），
    #    `explorer_prompt_version` 指向一个已经不存在的提示词。
    # 📌 **新写入不再生产已经没有对象的字段** —— 留着就是给后人假事实。
    check(set(p) == {"original_requirement", "last_creation_note"},
          "payload 就是两个字段，不多不少（Explorer 专属的两个已删）", str(sorted(p)))
    check(p["original_requirement"] == "导出大额消费", "原始需求存下来了")
    check("阈值待定" in p["last_creation_note"], "上一轮已确立的内容存下来了")

    # 工具与动态段只在有待办时出现
    # 换锚点：`_build_skills_info` 那 11 个 include_* 开关已随 cutover 删除，
    # 「这一轮给哪些工具」改问目录的 `advertised(scope, runtime)`。
    # ⭐ 断言的意图正是 `availability` 这个维度要表达的东西，一个字没改。
    from core.tools import ToolScope as _TS
    names = [d.name for d in o._get_tool_catalog().advertised(
        _TS.MAIN, o._tool_runtime_view())]
    check("answer_open_interaction" in names, "有待办时工具进 manifest")
    blk = o._build_open_interactions_injection()
    check(iid in blk and "[Open Interactions]" in blk, "动态段列出了这条待办")
    check("Do not nag" in blk or "do not nag" in blk, "动态段明确说了不要催用户")
    # ⭐ 三种走向必须都写全（2026-08-05 实测）：第一版只写了"answers"，
    # 完全没提"取消"。用户说"算了不做了"时模型在这段里找不到适用指令，
    # 于是只回一句"好的，取消了"、**没调工具**，待办留在原地。
    check("relation=CANCEL" in blk,
          "⭐ 取消这条走向也写进动态段了（原来只在工具描述里，模型看不到）")
    check("does NOT close it" in blk,
          "⭐ 并明说'光回一句话不算关掉'（这正是实测翻车的那一步）")
    check("relation=ANSWER" in blk, "回答那条走向也在")

    # ⭐ 开新的创建流程 = 同需求的澄清被接管（2026-08-05 实测）
    #
    # 实测：Nano 问"你要哪些网络配置？"，用户答"选 7，全部上述内容"，模型**没调工具**，
    # 而是开了一条全新的 create_new_skill（需求里已含那个回答）。需求办成了，澄清变僵尸。
    #
    # ⚠️ 这是启发式，可用的理由是**失败代价对称且轻微**（误判=重问一次，漏判=TTL 收掉）。
    # 阈值 0.5 是量出来的：真正例 67~86%、假正例 0~25%。见函数 docstring。
    _net = orch_mod._rt_open_clarification(
        o, original_requirement="写个skill，作用是查看当前网络配置",
        last_creation_note="n", prompt_text="要哪些？")
    _other = orch_mod._rt_open_clarification(
        o, original_requirement="写个skill，作用是批量重命名图片",
        last_creation_note="n", prompt_text="要哪些？")
    check(bool(_net) and bool(_other), "前置条件：两条澄清都登记成功")

    _n = orch_mod._rt_supersede_covered_clarifications(
        o, "创建一个Skill「WindowsNetworkConfig」，用于查看当前系统的网络配置，"
           "包括网卡、DNS、路由表、代理和连接状态")
    check(_n == 1, "⭐ 只关掉被涵盖的那一条", f"关掉 {_n} 条")
    check(I.get(k, _net).status == I.Status.SUPERSEDED,
          "⭐ 同需求那条 → SUPERSEDED（不是 RESOLVED：它不是被回答的，是被取代的）")
    check(I.get(k, _net).resolution == I.Resolution.FOLLOW_UP_QUESTION,
          "resolution 说明是被后续流程接管")
    check(I.get(k, _other).status == I.Status.OPEN,
          "⭐⭐ 无关需求那条【不动】—— 误关一条真在等答案的澄清是这里唯一要防的事")

    # 剥样板是关键：不剥的话两个无关需求会因共享"写个skill，作用是"拿到虚高重叠度
    check(orch_mod._strip_skill_boilerplate("写个skill，作用是查看当前网络配置")
          == "查看当前网络配置",
          "样板词被剥干净（第一版没剥，真正例只拿到 24% 判不出来）")
    check(orch_mod._COVERAGE_THRESHOLD == 0.5, "阈值 0.5（量出来的，不是拍的）")
    for _iid in (_net, _other):
        if I.get(k, _iid).is_live:
            orch_mod._rt_close_interaction(_iid, I.CANCEL)

    # 清掉之后两者都应消失
    orch_mod._rt_close_interaction(iid, I.RESOLVE)
    from core.tools import ToolScope as _TS2
    names2 = [d.name for d in o._get_tool_catalog().advertised(
        _TS2.MAIN, o._tool_runtime_view())]
    check("answer_open_interaction" not in names2,
          "没待办时工具不注入（工具存在本身就是一种暗示）")
    check(o._build_open_interactions_injection() == "", "没待办时动态段是空串，一个字符不加")


# ══════════════════════════════════════════════════════════════════════════
# 3｜完整回答链路
# ══════════════════════════════════════════════════════════════════════════

def t_answer_flow(tmp: pathlib.Path) -> None:
    """[3] ⭐⭐⭐ 用户回答 → 原子落盘 ANSWERED → **把事实交回主 ReAct**。

    ⚠️⚠️ **2026-08-13：这一组的断言方向被【刻意】改了，不是回归。**

    旧实现：回答之后**同轮重跑 Explorer**，跑完就 RESOLVED。
    新实现：探索子循环已拆，回答之后把 checkpoint + 用户原话作为**事实**
            交回主 ReAct（`exit_flow_defer_to_model`），由主模型重新决策。

    🔴 **于是这里【不】RESOLVE，状态停在 ANSWERED** —— 这是有意的：
       「把消息递出去」**不等于**领域工作已经续接成功。如果紧接着 provider 报错、
       或者模型没接住，答案必须还在，下一轮靠 `[Open Interactions]` 幂等重试，
       **不要求用户重说一遍**。那正是这条状态流当初存在的全部理由。
    📌 **判据：一个「已回答」的记录，要等到它引发的领域动作真的产生了新状态，
       才算续接完成 —— 递出去不算。**
    """
    print("")
    print("[3] ⭐ 用户回答 → 落盘 ANSWERED → 交回主 ReAct（刻意不 RESOLVE）")
    o, k, _ = make_orch(tmp / "b")
    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费",
        last_creation_note="文件已找到，阈值待定", prompt_text="阈值用哪个？")

    script_decisions(o, [answer_call(iid, "用 >=6000")])
    evs = asyncio.run(drive(o))

    rec = I.get(k, iid)
    check(rec.answer_verbatim == "用 >=6000", "用户原话已原子落盘")
    check(rec.status == I.Status.ANSWERED,
          "⭐⭐ 停在 ANSWERED —— 交回主模型不等于续接成功（provider 若在此刻失败，"
          "答案不能跟着消失）", rec.status)

    # ⭐⭐ 断言看的是**模型真正收到的东西**，不是那条中间事件。
    #
    # ⚠️ 第一版断言 `evs` 里有 `exit_flow_defer_to_model` —— **收不到，而且是对的**：
    #    主循环的 `_take_defer` 按设计把它**拦截**（`return True` = 不转发给 UI），
    #    然后写成一条成对的 `tool_result` 进 memory。那才是模型下一轮读到的。
    # 📌 **断言要钉在「谁最终读到它」上，不是「路上经过了什么」** ——
    #    钉在中间事件上，既会被合法的拦截打红，也验不到 plumbing 有没有接对。
    _tr = [b for m in o.memory.storage if getattr(m, "role", "") == "tool_results"
           for b in getattr(m, "tool_results", [])]
    check(len(_tr) == 1, "⭐ 交回的事实被写成了一条成对的 tool_result（模型下一轮读得到）",
          str(len(_tr)))
    if _tr:
        _t = _tr[0].content or ""
        check("导出大额消费" in _t, "⭐ 带着**原始需求**（不是用户这句回答）")
        check("文件已找到，阈值待定" in _t, "⭐ 带着上一轮已确立的内容（checkpoint 的价值）")
        check("用 >=6000" in _t, "带着用户原话")
        check("do not ask the user to repeat" in _t.lower(),
              "明确禁止让用户重说一遍")


def t_answer_then_followup(tmp: pathlib.Path) -> None:
    """[4] 主模型带着答案重新决策后又发现新问题 → 旧的被取代，新的是另一条交互。"""
    print("")
    print("[4] 回答后又冒出新问题 → 新建一条，旧的被涵盖取代")
    o, k, _ = make_orch(tmp / "c")
    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费", last_creation_note="阈值待定",
        prompt_text="阈值用哪个？")

    # 主模型重新决策后再开一条澄清（新链路里这是它自己调 create_new_skill 带
    # 非空 open_questions 的结果；这里直接驱动那条既有路径）。
    new_iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费", last_creation_note="阈值已定 >=6000",
        prompt_text="那导出成 CSV 还是 XLSX？")
    check(new_iid and new_iid != iid,
          "⭐ 新问题是**另一条**交互，有自己的 id（不做 ANSWERED→OPEN 回退覆盖 ——"
          "旧问题确实已经被回答了）", f"{iid} vs {new_iid}")
    _new = I.get(k, new_iid)
    check(_new.revision == 1, "新交互的 revision 从 1 开始（不是覆盖旧的）")
    check("CSV" in _new.prompt_text, "新交互带的是新问题")

    # ⭐ 旧的由既有的"涵盖判定"收掉：新的创建流程涵盖同一需求 → SUPERSEDED。
    #    📌 这条机制是实测 挣来的，与探索在不在无关，所以拆除后照旧成立。
    n = orch_mod._rt_supersede_covered_clarifications(o, "导出大额消费")
    check(n >= 1, "涵盖判定收掉了至少一条旧澄清", str(n))
    check(I.get(k, iid).status == I.Status.SUPERSEDED,
          "⭐ 旧的那条是 SUPERSEDED（被取代），不是假装它自己完成了",
          I.get(k, iid).status)


def t_continuation_failure(tmp: pathlib.Path) -> None:
    """[5] ⭐⭐ 答案不丢：ANSWERED 是可重试状态，且重试不重复写答案。

    ⚠️ 新链路下"续接失败"的形态变了（不再是 Explorer 抛异常），但**这一组要守的
    东西一个字没变**：用户原话必须还在、动态段必须让模型幂等重试、
    **绝不能要求用户重说一遍**。这正是整个早先存在的理由 ——
    旧实现在调 Explorer 之前就把 pending 清了。
    """
    print("")
    print("[5] ⭐ ANSWERED 可重试：答案不丢、动态段带原话、不让用户重说")
    o, k, _ = make_orch(tmp / "d")
    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费", last_creation_note="阈值待定",
        prompt_text="阈值用哪个？")

    script_decisions(o, [answer_call(iid, "用 >=6000，另外导出成 CSV",
                                     relation="ANSWER_AND_AMENDMENT")])
    asyncio.run(drive(o))

    rec = I.get(k, iid)
    check(rec.status == I.Status.ANSWERED, "停在 ANSWERED", rec.status)
    check(rec.answer_verbatim == "用 >=6000，另外导出成 CSV", "用户原话还在")
    check(rec.relation == I.Relation.ANSWER_AND_AMENDMENT, "relation 也还在")
    _rev = rec.revision
    check(_rev == 2, "revision 停在 2（open=1 → answer=2）", f"rev={_rev}")

    blk = o._build_open_interactions_injection()
    check("ALREADY ANSWERED" in blk, "动态段出现重试提示")
    check("Do NOT ask them to repeat themselves" in blk, "明确禁止让用户重说")
    check("用 >=6000，另外导出成 CSV" in blk, "重试提示里带着原话")

    # 下一轮重试：同一个工具、同一个答案 → **幂等**，不许重复写答案再 bump 一次
    script_decisions(o, [answer_call(iid, "用 >=6000，另外导出成 CSV",
                                     relation="ANSWER_AND_AMENDMENT")])
    asyncio.run(drive(o))
    rec2 = I.get(k, iid)
    check(rec2.revision == _rev,
          "⭐⭐ 重试**没有**重复写答案（同答案幂等，revision 不再 bump）",
          f"{_rev} -> {rec2.revision}")


def t_unrelated_keeps_open(tmp: pathlib.Path) -> None:
    print("\n[6] 用户说别的 → 不调工具 → 交互保持 OPEN")
    o, k, _ = make_orch(tmp / "e")
    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费", last_creation_note="阈值待定", prompt_text="阈值用哪个？")

    # 模型判断这是新话题 → 直接出文本，不调 answer_open_interaction
    script_decisions(o, [AgentDecision("text", content="纽约现在 22 度。")])
    asyncio.run(drive(o))

    rec = I.get(k, iid)
    check(rec.status == I.Status.OPEN, "交互仍然 OPEN —— 没被误关", rec.status)
    check(rec.answer_verbatim is None, "没有把无关的话当成答案存进去")
    check(rec.revision == 1, "revision 没动（UNRELATED 不产生无意义 revision）")
    check(len(I.list_live(k)) == 1, "它还在待办列表里，用户之后仍可回答")


def t_cancel(tmp: pathlib.Path) -> None:
    print("\n[7] relation=CANCEL → 用户放弃")
    o, k, _ = make_orch(tmp / "f")
    iid = orch_mod._rt_open_clarification(
        o, original_requirement="导出大额消费", last_creation_note="阈值待定", prompt_text="阈值用哪个？")

    script_decisions(o, [answer_call(iid, "算了不做了", relation="CANCEL")])
    evs = asyncio.run(drive(o))

    rec = I.get(k, iid)
    check(rec.status == I.Status.CANCELLED, "CANCELLED", rec.status)
    check(rec.answer_verbatim == "算了不做了", "取消时用户原话同样落盘（可追溯）")
    # ⭐ 取消**不跑 continuation** —— 用户放弃了这件事，没有什么要接着做。
    # ⚠️ 这条断言的**理由变了，结论没变**：取消分支现在也交回模型措辞，
    #    但 defer 事件会被 `_run_react_loop` 的拦截处吃掉（写成 tool_result 后
    #    `continue`），所以它照样不出现在 evs 里。
    #    📌 断言值没变、含义变了的时候，注释必须跟着改 —— 否则下一个人会拿它
    #       当"取消不走 defer"的证据。
    check(not [e for e in evs if e.get("event") == "exit_flow_defer_to_model"],
          "⭐ defer 事件不外泄到 UI —— 被拦截处消费掉了")
    # ⚠️ 措辞交回模型后，这里不再有成品中文；改断言【事实进了模型的上下文】。
    _ctx = str(o.memory.get_full_context())
    check("cancelled the pending to-do" in _ctx, "取消这件事交到了模型手上")
    # ⚠️ 不能断言"完全没有 content" —— 拦截后 ReAct 会再跑一轮，那一轮
    #    （由模型产出）本来就该有 content。要测的是**那句写死的中文没了**。
    check(not any("先不做这个" in (e.get("content") or "") for e in evs),
          "⭐ 写死的那句中文不再出现 —— 措辞归模型")


def t_stale_id(tmp: pathlib.Path) -> None:
    print("\n[8] 模型引用了已关闭/不存在的交互 → 明说，不静默吞掉")
    o, k, _ = make_orch(tmp / "g")
    script_decisions(o, [answer_call("int_nonexistent", "用 >=6000")])
    evs = asyncio.run(drive(o))
    # ⚠️ 同上：断言从"成品中文"改成"事实交到位"。
    _ctx = str(o.memory.get_full_context())
    check("does not exist" in _ctx, "告诉模型这条待办不存在", _ctx[-90:])
    check("用 >=6000" in _ctx, "用户说的话没被丢掉，原样交给模型继续处理")


def t_reset(tmp: pathlib.Path) -> None:
    print("\n[9] 重置对话 → 未决交互一并作废")
    o, k, _ = make_orch(tmp / "h")
    a = orch_mod._rt_open_clarification(o, original_requirement="需求A",
                                        last_creation_note="问题A", prompt_text="问题A？")
    b = orch_mod._rt_open_clarification(o, original_requirement="需求B",
                                        last_creation_note="问题B", prompt_text="问题B？")
    check(len(I.list_live(k)) == 2, "前置条件：两条都活着")

    o.reset_conversation()
    check(len(I.list_live(k)) == 0, "重置后一条不剩")
    check(I.get(k, a).status == I.Status.CANCELLED, "A 被 CANCELLED")
    check(I.get(k, b).resolution == I.Resolution.USER_CANCELLED, "作废原因是用户操作")
    check(k.check_invariants_now() == [], "重置后不变量全通过")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("拆澄清路由劫持 + answer_open_interaction")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_f1s3r_"))
    try:
        for fn in (t_field_removed, t_clarify_opens_interaction, t_answer_flow,
                   t_answer_then_followup, t_continuation_failure,
                   t_unrelated_keeps_open, t_cancel, t_stale_id, t_reset):
            d = tmp / fn.__name__
            d.mkdir(parents=True, exist_ok=True)
            try:
                fn(d)
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
    finally:
        close_all_stores()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
