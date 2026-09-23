# -*- coding: utf-8 -*-
"""Subagent —— **隔离是不是真的隔离**。

═══ 这一套守的东西 ═══

  ① **白名单，不是排除法**
     早先的设计 原来写的是「把那五个 UI 工具从Subagent工具集里**摘掉**即可」，
     那句写在 落地之前。按排除法做，/每加一个内置工具，
     Subagent的名单就欠一笔，**而且欠的时候不会报错**。
     📌 排除法要求你记得每一个新东西；白名单只要求你记得你想要的那几个。
        **前者的欠账随时间增长，后者不会。**
     ⭐ 所以本套件有一项是「造一个全新工具，确认它天然不在Subagent里」——
        钉的是**机制**，不是当前这份名单。

  ② **Subagent不能再生Subagent**（防无限递归）

  ③ **执行侧也拦得住**：模型幻觉出 `os_execute`，`resolve` 也必须给不出 handler
     📌 的教训是「给出去的一定执行得了」；这里要的是第三种状态：
        **既没给出去、也执行不了**。而那一条**只有真的 resolve 一次**才验得到。

  ④ 上下文隔离：回 main agent 的**只有报告**

用法：
  py -3.10 tests\\t_a4_agent_scope.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _defs():
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    mans = {v["name"]: v for k, v in vars(O).items()
            if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    return {d.name: d for d in build_builtin_definitions(mans)}


def t_whitelist_not_denylist() -> None:
    print("\n[1] ⭐⭐⭐ Subagent工具集是**白名单** —— 新工具天然不在里面")
    from core.tools.catalog import ToolScope
    defs = _defs()
    agent = {n for n, d in defs.items() if ToolScope.AGENT in d.bindings}
    check(agent, "Subagent确实有工具", str(sorted(agent)))

    # 🔴 五个 UI 工具
    _ui = {"create_task_list", "update_task_step", "ask_user_choice",
           "set_window_mode", "render_visual"}
    check(not (_ui & agent),
          "⭐⭐⭐ 五个占用 main agent UI 的工具**都不在** —— "
          "🔴 `self._task_state` 是 WebUI 上的单个 dict，`create_task_list` "
          "直接整体覆盖；`_show_right_panel('plan')` 会抢抽屉。"
          "⭐ 正解不是在 UI 层做互斥，是根本不给Subagent这些工具",
          str(sorted(_ui & agent)))
    # 🔴 写工具
    # ⚠️⚠️ **2026-08-16 设计变更**：`os_execute` 从这份名单里移出来了。
    #    原来它不给Subagent，理由是它**全有或全无** —— 给了就是 39 个 action 全给
    #    （含 `file_delete` / 点鼠标）。
    #    ⭐ 有了 `readonly_only` 闸之后，「**只读的** os_execute」第一次成为
    #      一个可以单独给出去的东西，而Subagent此前连一个文件夹都 `list_dir` 不了。
    # 📌 **一条「不给」的决定，前提可能是「当时给不了一半」** ——
    #    能给一半之后，那条决定要重新问一遍，而不是当成结论继承下去。
    # 🔴 它的安全性由**下面第 [2] 组**钉（解析到只读 handler + 闸真的拦），
    #    不再由「它不在名单里」钉 —— 后者现在会给出错误的安心。
    _write = {"create_new_skill", "update_existing_skill",
              "WriteSkill", "manage_existing_skill", "write_user_note"}
    check(not (_write & agent),
          "⭐⭐⭐ 所有**写**工具都不在（Subagent仍然不能写 Skill / 写笔记）",
          str(sorted(_write & agent)))
    check("os_execute" in agent,
          "⭐ 而 `os_execute` **在**（只读版）—— 见 [3] 组：它解析到的是"
          "`_handle_os_execute_readonly`")

    # ⭐⭐⭐ 钉的是机制不是名单：造一个全新工具，它必须天然不在Subagent里
    from core.tools.catalog import (ToolDefinition, ToolOrigin, Presentation,
                                    Scheduling, Flow, Preload, ALWAYS)
    _new = ToolDefinition(
        name="brand_new_tool_from_C_series", origin=ToolOrigin.BUILTIN,
        manifest={"name": "brand_new_tool_from_C_series"},
        awareness="a brand new tool added by some later feature",
        presentation=Presentation(card=lambda a: "x"),
        scheduling=Scheduling.PARALLEL, flow=Flow.CONTINUE,
        preload=Preload.DEFERRED, bindings={ToolScope.MAIN: "_x"},
        availability=ALWAYS)
    check(ToolScope.AGENT not in _new.bindings,
          "⭐⭐⭐ **一个只声明 MAIN 的新工具，天然不在Subagent作用域里** —— "
          "📌 这正是要防的那件事：后续加工具时忘了给 Subagent 作用域表态，会留下欠账"
          "已经被 [F4] 解掉的证据：不需要有人记得去Subagent那边排除它")


def t_agent_cannot_spawn_agent() -> None:
    print("\n[2] ⭐⭐⭐ Subagent**不能再生Subagent**（防无限递归）")
    from core.tools.catalog import ToolScope
    defs = _defs()
    _sa = defs.get("spawn_agent")
    check(_sa is not None, "⚠️ [L5] 前置：`spawn_agent` 存在")
    check(_sa is not None and ToolScope.MAIN in _sa.bindings,
          "⭐ main agent 有它")
    check(_sa is not None and ToolScope.AGENT not in _sa.bindings,
          "⭐⭐⭐ **Subagent没有它** —— 🔴 无限递归的那道闸，"
          "而且是白名单天然给的，不是一个额外的 if",
          str(sorted(k.value for k in (_sa.bindings if _sa else {}))))


def t_resolve_really_blocks() -> None:
    print("\n[3] ⭐⭐⭐ 执行侧**真的 resolve 一次**：幻觉出的工具拿不到 handler")
    from core.tools import ToolCatalog
    from core.tools.catalog import ToolScope
    cat = ToolCatalog()
    for d in _defs().values():
        cat.add_builtin(d)

    class _RT:
        def has_live_work(self): return True
        def has_open_interaction(self): return True
        def is_recheck_round(self): return True
        def has_unsummarized_image(self): return True
        def has_evicted_history(self): return True
    rt = _RT()

    # Subagent调 os_execute → **解析到只读那个 handler**（2026-08-16 设计变更）
    _h = cat.resolve("os_execute", ToolScope.AGENT, rt)
    _hm = cat.resolve("os_execute", ToolScope.MAIN, rt)
    check(_h == "_handle_os_execute_readonly",
          "⭐⭐⭐ Subagent作用域解析到的是**只读 handler** —— "
          "📌 的 `bindings` 本意就是「不同作用域**真的有不同的 handler**」。"
          "⚠️ 这比「同一个 handler + 一个 readonly 标志」强的地方在于："
          "**一个靠「记得传参」维持的安全边界，等于把它交给了下一个人的记性**，"
          "而默认值那一侧永远是危险的那侧（忘了传 = 全权限）",
          str(_h))
    check(_hm == "_handle_os_execute" and _hm != _h,
          "⭐⭐ 而 main agent 那边是**另一个** handler（不是两边同一个蒙的）",
          f"main={_hm} / agent={_h}")

    # 🔴🔴 **闸必须真的拦** —— 只查 handler 名字证明不了它拦得住。
    from core.os_layer import dsl as _dsl
    _wr = _dsl.validate_instruction(
        {"action": "file_write", "params": {"path": "x", "content": "y"}},
        m3_mode=True, readonly_only=True)
    check(not _wr.ok and "read-only" in (_wr.error or ""),
          "⭐⭐⭐ **真的 validate 一次**：`readonly_only` 下 `file_write` 被拒 —— "
          "📌 只查「解析到只读 handler」是查名字；名字对而闸没接上，"
          "结果是Subagent照样能写，而且一路绿灯",
          (_wr.error or "")[:40])
    _rd = _dsl.validate_instruction({"action": "list_dir", "params": {"path": "."}},
                                    m3_mode=True, readonly_only=True)
    check(_rd.ok, "⚠️ [L5] 反向：只读的照样放行（不是「全拒」蒙的）")
    _rs = _dsl.validate_instruction({"action": "read_screen_region", "params": {}},
                                    m3_mode=True, readonly_only=True)
    check(_rs.ok,
          "⭐⭐ `read_screen_region` **放行** —— 它只读但 `stage=3`。"
          "📌 这一格正是 `readonly` 与 `m1_mode` 的分界："
          "前者问「改不改这台电脑」，后者问「这一步推进到哪了」；"
          "**合并它们就是让安全边界跟着开发进度走**")
    check(cat.resolve("spawn_agent", ToolScope.AGENT, rt) is None,
          "⭐⭐ Subagent调 `spawn_agent` 同样解析不出来")
    _ok = cat.resolve("query_local_knowledge", ToolScope.AGENT, rt)
    check(_ok is not None,
          "⭐ 而白名单里的那几个是**真的能解析**的（否则Subagent什么都干不了）", str(_ok))


def t_isolation_and_report() -> None:
    print("\n[4] ⭐⭐ 上下文隔离：回 main agent 的**只有报告**")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    _loop = next((f for f in ast.walk(tree)
                  if isinstance(f, ast.AsyncFunctionDef) and f.name == "_run_agent_loop"), None)
    check(_loop is not None, "⚠️ [L5] 前置：找得到隔离循环")
    seg = (ast.get_source_segment(src, _loop) or "") if _loop else ""

    # 🔴 隔离循环不许碰 main agent 的记忆
    bad = []
    for n in ast.walk(_loop) if _loop else []:
        if isinstance(n, ast.Attribute) and n.attr in ("memory", "storage"):
            bad.append(n.attr)
    check(not bad,
          "⭐⭐⭐ 隔离循环**不碰 `self.memory` / `storage`** —— "
          "📌 上下文隔离天然正确，要做的不是实现它，是**别破坏它**："
          "任何「顺手把 Subagent 的中间步骤也塞给 main agent」的好意，都会毁掉它的全部价值",
          str(bad))
    check("ToolScope.AGENT" in seg or "_TS.AGENT" in seg,
          "⭐⭐ 循环里用的是 **AGENT 作用域**（工具集与 resolve 两处）")

    _h = next((f for f in ast.walk(tree)
               if isinstance(f, ast.AsyncFunctionDef) and f.name == "_handle_spawn_agent"), None)
    hseg = (ast.get_source_segment(src, _h) or "") if _h else ""
    check("create_background_job" in hseg and "finish_background_job" in hseg,
          "⭐⭐ Subagent进 [L5] 那张后台任务抽屉（Running → Finished）—— "
          "📌 agent 是「无法被回看的后台任务」，用户看得到，主体模型看不到")
    check("_AGENT_MAX_STEPS" in src,
          "⭐ 有步数上限 —— 📌 一个没有上限的子循环，"
          "坏起来是「一直在跑、一直在花钱、没人看得见」")


def t_original_instruction_survives() -> None:
    print("\n[5] ⭐⭐ main agent 发给 Subagent 的**原始指令不许消失**（唯一为 Nano 微调的一处）")
    from core.tools.catalog import DetailBlock
    defs = _defs()
    _sa = defs["spawn_agent"]

    class _R:
        content = "Subagent的结论"
        is_error = False
    blocks = _sa.presentation.render_detail(
        {"label": "查 RAG 配置", "instruction": "去把 core/rag.py 里所有阈值列出来"}, _R())
    _lab = [b.label for b in blocks]
    check(any("原始指令" in x for x in _lab),
          "⭐⭐⭐ 展开后**看得到 main agent 当初发的完整指令** —— "
          "🔴 Claude Code 那边 agent 一响应，原始指令就从 UI 里没了；"
          "向 Claude Code 提的正是这条：**那个消失是缺陷，不是要照抄的形态**",
          str(_lab))
    check(any(b.body == "去把 core/rag.py 里所有阈值列出来" for b in blocks),
          "⭐ 而且是**逐字**的，不是摘要")
    check(any("报告" in x for x in _lab), "⭐ Subagent的报告也在同一处")


def t_context_shape_is_api_legal() -> None:
    print("\n[6] ⭐⭐⭐ Subagent发给 provider 的上下文**形状合法**（真过一遍 _merge_context）")
    import asyncio
    from core.provider import ClaudeProvider
    from core.schema import AgentDecision, ToolCall
    from core.orchestrator import Orchestrator

    seen = {"roles": [], "n": 0}

    class _Prov:
        target_model = "anthropic/claude-haiku-4.5"

        async def chat_with_tools(self, context, tools_manifest, system_guide, **kw):
            # 🔴🔴 过一遍**真的** `_merge_context`。
            #    实测第一次派Subagent就 400：
            #      Invalid value for 'messages[2].role': 'tool_results'
            #    而那时本套件 19 项**全绿** —— 因为它们只验了结构。
            # 📌 **一个自制的假 provider 如果照单全收，它只能证明两次想法相同。**
            #    （同 `t_f5_decay_l2` 里那段：假 provider 和调用方出自同一处。）
            merged = ClaudeProvider._merge_context(context)
            for m in merged:
                seen["roles"].append(m.get("role"))
            seen["n"] += 1
            if seen["n"] == 1:
                d = AgentDecision("call", name="list_knowledge_files",
                                  args={}, tool_use_id="tu_a")
                d.tool_calls = [ToolCall(name="list_knowledge_files", args={},
                                         tool_use_id="tu_a", index=0)]
                return d, self.target_model
            return AgentDecision("text", content="报告：看完了"), self.target_model

    class _RT:
        def has_live_work(self): return True
        def has_open_interaction(self): return True
        def is_recheck_round(self): return True
        def has_unsummarized_image(self): return True
        def has_evicted_history(self): return True

    class _Host:
        provider = _Prov()
        _AGENT_MAX_STEPS = 4
        _AGENT_SYS = "sys"

        def _get_tool_catalog(self):
            from core.tools import ToolCatalog
            c = ToolCatalog()
            for d in _defs().values():
                c.add_builtin(d)
            return c

        def _tool_runtime_view(self):
            return _RT()

        async def _handle_list_knowledge_files(self, args, aid, **kw):
            return "[Available Files]\nfoo.docx"

    class _Q:
        def put_nowait(self, *a, **k):
            pass

    h = _Host()
    report, steps = asyncio.run(
        Orchestrator._run_agent_loop.__get__(h, _Host)("去查一下", "aid", _Q()))

    check(steps == 2 and "看完了" in report,
          "⭐⭐ 隔离循环**真的跑完了一轮工具再收尾**", f"{steps} 步 / {report[:16]}")
    _bad = [r for r in seen["roles"] if r not in ("user", "assistant", "system")]
    check(not _bad,
          "⭐⭐⭐ 送出去的 role **全部是 API 认的那几个** —— "
          "🔴 第一版手搓了 `role='tool_results'`，实测第一次派 Subagent 就 400，"
          "而当时本套件 19 项全绿。"
          "📌 只验结构的断言，拦不住「形状错了」这类错",
          str(sorted(set(_bad))))
    check(seen["roles"].count("assistant") >= 1 and seen["roles"].count("user") >= 2,
          "⚠️ 而且确实有一轮完整往返（user 问 / assistant 调工具 / user 交结果）",
          str(seen["roles"]))


def t_parallel_agents() -> None:
    print("\n[7] ⭐⭐⭐ 多Subagent**真的并行**，且有并发上限")
    import asyncio
    from core.orchestrator import Orchestrator
    from core.schema import AgentDecision

    state = {"peak": 0, "cur": 0, "n": 0}

    class _Prov:
        target_model = "anthropic/claude-haiku-4.5"

        async def chat_with_tools(self, context, tools_manifest, system_guide, **kw):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            state["n"] += 1
            await asyncio.sleep(0.05)          # 让别的Subagent有机会挤进来
            state["cur"] -= 1
            return AgentDecision("text", content="报告"), self.target_model

    class _RT:
        def has_live_work(self): return True
        def has_open_interaction(self): return True
        def is_recheck_round(self): return True
        def has_unsummarized_image(self): return True
        def has_evicted_history(self): return True

    class _Host:
        provider = _Prov()
        _AGENT_MAX_STEPS = 4
        _AGENT_MAX_PARALLEL = 3
        _AGENT_SYS = "sys"

        def _get_tool_catalog(self):
            from core.tools import ToolCatalog
            c = ToolCatalog()
            for d in _defs().values():
                c.add_builtin(d)
            return c

        def _tool_runtime_view(self):
            return _RT()

    for _n in ("_agent_sem", "_run_agent_loop", "agent_transcript"):
        setattr(_Host, _n, getattr(Orchestrator, _n))

    class _Q:
        def put_nowait(self, *a, **k): pass

    h = _Host()

    async def _go():
        return await asyncio.gather(*[
            h._run_agent_loop(f"任务{i}", "aid", _Q()) for i in range(6)])

    out = asyncio.run(_go())
    check(len(out) == 6 and all(r[0] == "报告" for r in out),
          "⭐⭐ 6 个Subagent**全部跑完**", str(len(out)))
    check(state["peak"] >= 2,
          "⭐⭐⭐ **确实同时在跑**（峰值并发 ≥ 2）—— "
          "🔴 只断言「Semaphore 存在」证明不了并发发生过；"
          "📌 一个从未被两个协程同时进入的临界区，跟串行代码长得一模一样",
          f"峰值 {state['peak']}")

    # 并发闸本身（`_run_agent_loop` 不带闸，闸在 handler 里）
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    _h = next((f for f in ast.walk(tree)
               if isinstance(f, ast.AsyncFunctionDef) and f.name == "_handle_spawn_agent"), None)
    hseg = (ast.get_source_segment(src, _h) or "") if _h else ""
    check("_agent_sem()" in hseg,
          "⭐⭐ handler 里走并发闸（`_AGENT_MAX_PARALLEL`）—— "
          "📌 一个「想开几个开几个」的并发，第一次失控时的表现是账单，"
          "而账单要到第二天才看得见")
    _i_job = hseg.find("create_background_job")
    _i_sem = hseg.find("_agent_sem()")
    check(0 < _i_job < _i_sem,
          "⭐⭐⭐ **先进抽屉、再排队** —— "
          "📌 用户该看到「它在排队」，而不是「它不存在」",
          f"job@{_i_job} < sem@{_i_sem}")

    from core.tools.catalog import Scheduling
    check(_defs()["spawn_agent"].scheduling is Scheduling.PARALLEL,
          "⭐ `spawn_agent` 可与同批其他调用并行")


def t_agent_monitor_drawer() -> None:
    print("\n[8] ⭐⭐⭐ Subagent监控**抽屉**：内容照主聊天区那套画")
    # ⚠️⚠️ 这一项原来钉的是「行内 transcript 列表 + 入口是那一行的 `›`」——
    #    那是**一处走偏的设计**，用户说了三遍才纠正过来。
    #    📌 **一条断言可以完全正确地守着一个放错了位置的设计**
    #       （同 `chat` 混进 STATUS 那次）—— 它绿着，反而让人以为这件事被想过了。
    src = module_text("app")
    tree = ast.parse(src)

    _f = next((f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and f.name == "_render_agent_monitor"), None)
    check(_f is not None, "⚠️ [L5] 前置：有独立的**抽屉**渲染（不是行内列表）")
    seg = (ast.get_source_segment(src, _f) or "") if _f else ""

    check("nano ❯" in seg and "nano agent ❯" in seg,
          "⭐⭐⭐ 两个头部：`nano ❯`（**main agent** 发的指令）/ `nano agent ❯`（Subagent 的回报）—— "
          "📌 那条指令是 main agent 写的不是用户写的；没有头部它读起来像是用户发的，"
          "而最早的要求正是「用户要能看到 main agent 给 Subagent 写了什么指令」")
    check("used " in seg and "tool" in seg,
          "⭐⭐ 有与聊天区**同一形状**的工具 pill（`[✓] used N tools · …`）")

    # 🔴🔴 **顺序**：`nano agent ❯` 必须在工具 pill **之前**。
    #
    # ⚠️ 这一条是 2026-08-15 实测指出的：pill 原来画在
    #    `nano ❯`（main agent 指令）那一段下面、`nano agent ❯` 之前 ——
    #    于是**Subagent 调的工具看起来像是 main agent 调的**。
    # ⚠️⚠️ 而上面那两条断言（"两个头部都在"）**改之前也是绿的** ——
    #    它们对这个 bug 完全不敏感。
    #    📌 **一条只查「东西在不在」的断言，管不住「东西在哪」** ——
    #       而"在哪"恰恰是这一屏要表达的全部意思。
    _i_agent = seg.find("nano agent ❯")
    _i_pill = seg.find("used ")
    _i_nano = seg.find("nano ❯")
    check(0 <= _i_nano < _i_agent < _i_pill,
          "⭐⭐⭐ 顺序是 **main agent 指令 → `nano agent ❯` → 工具 pill → 报告** —— "
          "📌 一段行为归谁，是由它排在谁的名字下面决定的；"
          "**位置本身就是归属声明，不是排版细节**",
          f"nano@{_i_nano} < agent@{_i_agent} < pill@{_i_pill}")
    # ⚠️⚠️ 数**真实的 `ui.label` 实参**，不是数源码里出现几次 ——
    #    🔴 第一版写的是 `seg.count("nano agent ❯") == 1`，当场红：
    #       上面那段注释里解释了「pill 原来画在 `nano agent ❯` 之前」，
    #       **那句解释也被数进去了**。
    #    📌 **只要断言读的是文本，对代码的【解释】就会参与判定**
    #       —— 本项目第五次（源码换行 / 关键词缺席 / docstring / 注释 / 这次）。
    _hdr = [n for n in ast.walk(_f)
            if isinstance(n, ast.Call)
            and getattr(getattr(n.func, "attr", None), "__str__", str)() == "label"
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "nano agent ❯"]
    check(len(_hdr) == 1,
          "⚠️ 头部**只画一次** —— 📌 与聊天区一致："
          "一个头下面既有工具也有回答，不是每段内容各挂一个头",
          f"{len(_hdr)} 处")
    check("_attach_tool_detail" in seg and "direct=" in seg,
          "⭐⭐⭐ 每一步走 **[U8] 那个展开出口**（`direct=` 只是换了取数方式）—— "
          "📌 取数可以有两种，**渲染只能有一种**")
    check("tok" in seg,
          "⭐⭐ 显示 Subagent **自己花了多少 token** —— 要的是**监控**不是结果")

    # 入口：View transcript + pill，**没有并列按钮**
    _row = next((f for f in ast.walk(tree)
                 if isinstance(f, ast.FunctionDef) and f.name == "_render_bg_row"), None)
    rseg = (ast.get_source_segment(src, _row) or "") if _row else ""
    check("View transcript" in rseg and "_show_agent_monitor" in rseg,
          "⭐⭐⭐ 后台任务抽屉里**Subagent那一行是 `View transcript`** —— "
          "🔴 不是 `›`：📌 一个过程该有多大的展示空间，取决于它有多长，"
          "不取决于它此刻挂在哪一行下面")
    _p = next((f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and f.name == "_show_right_panel"), None)
    pseg = (ast.get_source_segment(src, _p) or "") if _p else ""
    check("'agent'" not in pseg and '"agent"' not in pseg,
          "⭐⭐ Subagent监控**不在 nav 面板表里** = 没有与 [知识库]/[监控]/[任务] "
          "并列的按钮 —— 📌 已经有两条通路的东西，第三个入口只是让导航更挤")
    check("agent_panel.set_visibility(False)" in pseg,
          "⚠️ 但切到别的面板时**必须把它藏掉** —— "
          "📌 一个不参与互斥的面板，会在某次切换后和别人叠在一起")

    # 实时
    _r = next((f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and f.name == "_refresh_tasks_panel"), None)
    fseg = (ast.get_source_segment(src, _r) or "") if _r else ""
    check("_render_agent_monitor" in fseg,
          "⭐⭐⭐ **实时**：抽屉开着时跟着每次刷新重画 —— "
          "📌 要的是监控；**跑完才有的东西叫记录**")


def t_agent_run_record() -> None:
    print("\n[9] ⭐⭐ Subagent记录里有监控需要的全部字段")
    from core.orchestrator import Orchestrator

    class _H:
        pass
    h = _H()
    for _n in ("agent_run", "_a4_rec", "agent_transcript"):
        setattr(_H, _n, getattr(Orchestrator, _n))

    rec = h._a4_rec("job1")
    check(set(rec) >= {"label", "instruction", "started", "ended",
                       "steps", "report", "ok"},
          "⭐⭐ 指令 / 起止时刻 / 步骤 / 报告 / 成败 都在", str(sorted(rec)))
    rec["steps"].append(("list_knowledge_files", {}, "结果", False))
    rec.update({"instruction": "去查一下", "report": "查完了", "ok": True})
    got = h.agent_run("job1")
    check(got.get("instruction") == "去查一下" and len(got.get("steps")) == 1,
          "⭐ `agent_run()` 读得到")
    check(h.agent_run("不存在的") == {},
          "⚠️ 没有记录时给空 dict，不抛 —— 上个进程跑的Subagent就是这种")

    # 🔴 在跑的过程中就要读得到（监控的前提）
    rec2 = h._a4_rec("job2")
    rec2.update({"instruction": "还在跑", "started": 1.0})
    rec2["steps"].append(("x", {}, "第一步", False))
    _live = h.agent_run("job2")
    check(_live.get("ok") is None and len(_live.get("steps")) == 1,
          "⭐⭐⭐ **还没跑完时就读得到已经走过的步** —— "
          "📌 这是「监控」与「记录」的分界：后者跑完才有")


def t_refusal_gives_an_exit() -> None:
    """🔴 实测 2026-08-20：Subagent说「我读不了文件」，然后**放弃了整件事**。

    它调的是 `os_execute(file_read)` —— 落在只读名单外，因为 `_ACTIONS` 表里
    `file_read.readonly=False`（那张表把「只读」和「要不要授权」写在同一格：
    `file_read.floor=2`，读任意文件确实该确认）。
    而它手上**明明有** `load_full_file`（v1.47 已确认绝对路径直接放行）。

    改造前那句拒绝有两处问题，逐字都点过：
      🔴 **不正确**：说「`file_read` 会改变这台电脑」—— 它一个字节都不改
      🔴 **不充分**：没告诉它可以改用什么 —— 📌：模型需要的是一个**出口**，
         不是一个名字
    ⚠️ 本项只管**说明**那一层；闸仍然只有 `dsl.is_readonly()` 一个出处（见 [3]）。
    """
    print("")
    print("[9] 🔴 拒绝的那句话必须【正确】且【给得出出口】")
    import asyncio as _a
    import core.orchestrator as _O
    _o = _O.Orchestrator.__new__(_O.Orchestrator)

    def _say(action):
        return _a.get_event_loop().run_until_complete(
            _O.Orchestrator._handle_os_execute_readonly(_o, {"action": action}, "x"))

    _fr = _say("file_read")
    check("会改变这台电脑" not in _fr,
          "⭐⭐⭐ **不再说假因** —— `file_read` 不改变任何东西，"
          "它落在名单外是另一个原因。📌 [D12]：给模型的失败信息必须【正确】",
          _fr[:60])
    check("load_full_file" in _fr,
          "⭐⭐⭐ **给出了它真正有的那个出口** —— Subagent白名单里就有 `load_full_file`。"
          "📌 模型需要的是一个出口，不是一个名字",
          _fr[:120])
    check("不要因此宣布任务无法完成" in _fr,
          "⭐⭐ 并明确堵住「就此放弃」那条路 —— "
          "🔴 实机那次它正是收到拒绝后直接写了一份「我无法完成此任务」的报告")

    _fw = _say("file_write")
    check("load_full_file" not in _fw and "只读动作" in _fw,
          "⭐ 而真的会写的动作**不给假出口** —— "
          "📌 一个「总能给出替代品」的兜底，会在没有替代品时编一个",
          _fw[:80])


def t_write_scope_is_exactly_one_tool() -> None:
    """Subagent的写口**只有 `edit_file` 一个**，而且它走的是同一条安全层。

    ⭐ 判据是从「什么时候该派Subagent」倒推的（2026-08-20 定的切入点）：
       Subagent的唯一优势是上下文隔离 →
       📌 **这件事会产生大量 main agent 不需要看的中间材料，而结论很短。**
       符合的两类：① 批量查找/勘察 ② 确定性、机械性的批量修改。
       ② 缺的就是一个「改」的能力，而**只缺一个**。
    """
    print("")
    print("[10] ⭐⭐⭐ 写口只有 `edit_file`，且安全层零改动")
    from core.tools.catalog import ToolCatalog, ToolScope
    import core.orchestrator as _O

    defs = _defs()

    _ef = defs.get("edit_file")
    check(_ef is not None and ToolScope.AGENT in _ef.bindings,
          "⭐⭐⭐ **`edit_file` 在 Subagent 作用域里** —— 让 Subagent 能写的全部内容就是这一条")
    check(_ef is not None
          and _ef.bindings.get(ToolScope.AGENT) == _ef.bindings.get(ToolScope.MAIN),
          "⭐⭐ 而且**与 main agent 是同一个 handler** —— 不是一个「Subagent 专用的写」。"
          "📌 `_handle_edit_file` 自己建 dispatcher 走 `_execute_dsl_step`，"
          "地板/确认/审计/路径策略全是 `os_execute` 那一套；"
          "换一个暴露层，不许换掉它底下的安全层",
          f"agent={_ef.bindings.get(ToolScope.AGENT)} main={_ef.bindings.get(ToolScope.MAIN)}")

    # 🔴 反向：那些**刻意不给**的，一个都不许在里面。
    #    ⚠️ 这一条钉的是**当下这份名单**，而下一条钉的才是**机制** ——
    #       两条都要：名单守「今天没漏」，机制守「明天加了新工具也不会漏」。
    _forbidden = ["create_task_list", "update_task_step", "ask_user_choice",
                  "set_window_mode", "look_at_screen", "render_visual",
                  "create_new_skill", "update_existing_skill",
                  "manage_existing_skill", "WriteSkill",
                  "spawn_agent", "task_boundary", "wait_for", "dont_wait",
                  "set_next_checkin", "cancel_wait"]
    _leaked = [n for n in _forbidden
               if n in defs and ToolScope.AGENT in defs[n].bindings]
    check(not _leaked,
          "⭐⭐⭐ 刻意不给的一个都没漏进去（UI / Skill 自写 / 编排 / 递归）",
          str(_leaked))

    # 🔴 `os_execute` 仍然是**只读那一个 handler** —— 给了 edit_file 不等于放开它。
    _os = defs.get("os_execute")
    check(_os is not None
          and _os.bindings.get(ToolScope.AGENT) == "_handle_os_execute_readonly"
          and _os.bindings.get(ToolScope.MAIN) != _os.bindings.get(ToolScope.AGENT),
          "⭐⭐⭐ `os_execute` **仍然只读** —— 📌 批量修改 ≠ 批量删除；"
          "`file_delete`/`file_move`/`run_command` 的错误不可逆，"
          "而 `edit_file` 匹配不上就失败、不动文件",
          str(_os.bindings.get(ToolScope.AGENT) if _os else None))


def t_agent_label_rides_the_execution_chain() -> None:
    """「这条执行链是谁的」必须跟着**执行链**走，不能是实例属性。

    ⭐ Subagent跑在自己的 `asyncio.Task` 里，`create_task` 在建任务那一刻复制上下文 ——
       于是 `ContextVar` 天然只作用在它那一支上。
       📌 与 `usage._turn_ctx` 同一个机制、同一个理由：**归属跟着「谁发起的」走。**
    ⚠️ 实例属性做不到（并发时互相串味）；参数透传要求每一层都记得传 ——
       那等于把它交给下一个人的记性。
    """
    print("")
    print("[11] ⭐⭐ 授权弹窗靠什么知道「这是Subagent干的」")
    import asyncio as _a
    import core.orchestrator as _O

    check(_O.current_agent_label() == "",
          "⭐⭐ **main agent 是空串** —— 📌 一个「看起来像 Subagent 其实是 main agent」的标识，"
          "比不画更坏")

    async def _go():
        _seen = {}

        async def _one(label):
            _O._agent_scope_ctx.set(label)
            await _a.sleep(0.01)
            _seen[label] = _O.current_agent_label()

        # ⚠️ **真的两个并发**：一个从没被两个协程同时进入的临界区，
        #    跟串行代码长得一模一样（并行那次的教训）。
        await _a.gather(_one("查 RAG 配置"), _one("批量改超时"))
        return _seen, _O.current_agent_label()

    _seen, _after = _a.get_event_loop().run_until_complete(_go())
    check(_seen == {"查 RAG 配置": "查 RAG 配置", "批量改超时": "批量改超时"},
          "⭐⭐⭐ **两个Subagent并发时各自看到自己的标识**，不串味", str(_seen))
    check(_after == "",
          "⭐⭐ 而且**没有渗回 main agent** —— 🔴 渗回去的表现是：Subagent 跑完之后，"
          "main agent 自己的授权弹窗上挂着一个 Subagent 的名字",
          repr(_after))

    # ⭐ 它真的被Subagent循环 set 了（零调用方的 ContextVar 和没有它一样）
    _src = module_text("core.orchestrator")
    _tree = ast.parse(_src)
    _sa = next((f for f in ast.walk(_tree)
                if isinstance(f, ast.AsyncFunctionDef)
                and f.name == "_handle_spawn_agent"), None)
    _runner = next((f for f in ast.walk(_sa) if isinstance(f, ast.AsyncFunctionDef)
                    and f.name == "_agent_runner"), None) if _sa else None
    check(_runner is not None and "_agent_scope_ctx.set" in ast.unparse(_runner),
          "⭐⭐⭐ **Subagent循环里真的 set 了它** —— 📌 一个写好但没人调的东西，"
          "比没写更坏：没写时缺口是可见的，写了不接时缺口看起来已经补上了")

    # ⭐ 弹窗事件真的带着它 + 确认闸真的按它换判据
    _dsl = next((f for f in ast.walk(_tree)
                 if isinstance(f, ast.AsyncFunctionDef)
                 and f.name == "_execute_dsl_step"), None)
    _body = ast.unparse(_dsl) if _dsl else ""
    check("'agent_label'" in _body,
          "⭐⭐ 授权弹窗事件**带着来源**（UI 据它画那一行）")
    check("cancel_on_user_message" in _body,
          "⭐⭐⭐ 而且确认闸按它换判据 —— "
          "📌 一个「取消」的信号，必须来自它要取消的那件事的同一条注意力")


def t_agent_prompt_matches_its_toolset() -> None:
    """🔴🔴 实测 2026-08-20：Subagent回了一份「我的角色被限制为只读权限」。

    给了它 `edit_file`，`spawn_agent` 的描述也改了 —— 但**Subagent自己读的那份
    system prompt 没改**，上面白纸黑字写着 "You can only read. You cannot change
    anything on this computer."。于是它照着说明放弃了第 3 步，
    而它手里明明有那个工具。

    📌 那一族的最坏形态：能力在、模型看不见 —— 而这次更糟，
       **我们明确告诉了它相反的事**。
    ⚠️ 而它不报错：一个被告知「我不能」的执行者，会**安静地不去做**。

    ⭐ 所以这一项钉的不是措辞，是**双向一致**：
       工具集里有 `edit_file` ⟺ 那份说明里点名 `edit_file`。
       改任何一边而忘了另一边，这里就红。
    """
    print("")
    print("[12] 🔴 Subagent读的那份说明，必须和它真有的工具对得上")
    from core.tools.catalog import ToolScope
    import core.orchestrator as _O

    _sys = _O.Orchestrator._AGENT_SYS
    _has_edit = ToolScope.AGENT in _defs()["edit_file"].bindings

    check(_has_edit == ("edit_file" in _sys),
          "⭐⭐⭐ **工具集里有 `edit_file` ⟺ 说明里点名它** —— "
          "🔴 实测那次正是两边脱节：工具给了，说明还写着「只读」",
          f"in_scope={_has_edit} in_prompt={'edit_file' in _sys}")

    for _lie in ("can only read", "cannot change anything"):
        check(_lie not in _sys,
              f"⭐⭐⭐ 说明里**不再有**「{_lie}」这句 —— "
              "📌 一句关于自己能做什么的假话，会让执行者安静地不去做",
              _sys[:60])

    # ⭐ 一次拒绝不许毁掉整件事（实测那次它直接写了「我无法完成此任务」）
    check("do NOT abandon the whole job" in _sys,
          "⭐⭐ 明说**一次被拒不要放弃整件事** —— "
          "🔴 Subagent不能提问，所以它对失败的默认反应就是收摊")
    check("approval" in _sys and "do not ask permission in your report" in _sys,
          "⭐⭐ 并告诉它「弹窗是常态」—— "
          "📌 不说的话它会把一次正常的授权流程理解成一道禁令，"
          "然后在报告里请示而不是动手")

    # 🔴 反向：说明里声称不能做的，必须真的不在工具集里
    _forbidden_in_prompt = ("delete or move files", "run commands",
                            "dispatch further sub-agents")
    for _f in _forbidden_in_prompt:
        check(_f in _sys, f"⚠️ 说明里仍然写明不能「{_f}」")
    _leak = [n for n in ("run_command", "spawn_agent")
             if n in _defs() and ToolScope.AGENT in _defs()[n].bindings]
    check(not _leak,
          "⭐⭐ 而那些「不能」是真的 —— 它们确实不在Subagent作用域里",
          str(_leak))


def t_agent_ui_requests_survive_its_turn() -> None:
    """🔴🔴 实测 2026-08-20：Subagent**永远卡住**。

    Subagent 5s 后交还 → 主轮继续 → 主轮结束 → `navigate_pipeline` 的 `async for`
    退出 → **从此没有人 drain `event_queue`**。Subagent随后调 `edit_file`，
    `os_action_confirm` 落进那个没人读的队列 → 弹窗永远不出现 →
    Subagent在确认闸上干等 300 秒，而屏幕上什么都不会发生。

    📌 **一个跨过了自己那一轮的执行者，不能再用那一轮的通道去要 UI** ——
       那条通道的寿命和那一轮绑在一起，而它已经不在那一轮里了。
    ⚠️ 这个洞只在**需要 UI 往返**的事件上存在：Subagent此前全是只读工具，
       一次往返都不需要 —— 给它写权限的那一刻才暴露。
       📌 一条只在某种能力出现后才会被走到的路，它的缺陷会**和那个能力
          同一天诞生**，而不是在它被写下的那天。
    """
    print("")
    print("[13] 🔴 Subagent要弹窗时，走的是**轮外**通道")
    import asyncio as _a
    import core.orchestrator as _O

    _o = _O.Orchestrator.__new__(_O.Orchestrator)
    _turn_q, _oob_q = object(), object()
    _o._ui_oob_events = _oob_q

    # main agent：走轮内
    check(_O.Orchestrator._ui_sink(_o, _turn_q) is _turn_q,
          "⭐ main agent 照旧走**轮内**通道（这一轮的事件流）")

    # Subagent：走轮外
    _tok = _O._agent_scope_ctx.set("批量改超时")
    try:
        check(_O.Orchestrator._ui_sink(_o, _turn_q) is _oob_q,
              "⭐⭐⭐ **Subagent走轮外通道** —— 🔴 走轮内的话，主轮一结束"
              "那个队列就没人读了，Subagent会在确认闸上干等 300 秒")
        # ⚠️ fail-safe：轮外通道不存在时**退回轮内**，而不是把请求丢掉
        _o._ui_oob_events = None
        check(_O.Orchestrator._ui_sink(_o, _turn_q) is _turn_q,
              "⚠️ 轮外通道缺席时退回轮内 —— "
              "📌 fail-safe 朝「退化成今天的行为」错，不朝「把授权请求整个丢掉」错")
    finally:
        _O._agent_scope_ctx.reset(_tok)
        _o._ui_oob_events = _oob_q

    # ⭐ 而轮外通道必须**真的有人读**（零消费者的通道 = 没有通道）
    _src = module_text("app")
    _tree = ast.parse(_src)
    _defs_app = {f.name for f in ast.walk(_tree)
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_drain_oob_events" in _defs_app, "⚠️ 前置：轮外消费者存在")
    _timered = any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "timer"
        and "_drain_oob_events" in ast.unparse(n)
        for n in ast.walk(_tree))
    check(_timered,
          "⭐⭐⭐ **它真的被挂上了定时器** —— "
          "📌 一个写好但没人调的消费者，比没写更坏：缺口看起来已经补上了"
          "（本项目第 N 次撞它）")

    # ⭐⭐ 弹窗的呈现**只有一个出口**（轮内轮外共用），不是各画一遍
    check("_present_os_confirm" in _defs_app,
          "⭐⭐ 授权弹窗的呈现收成一个出口 —— "
          "📌 同一件事在两处各画一遍，只在「我两次想法相同」的前提下一致")
    _callers = set()
    for n in ast.walk(_tree):
        if (isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "_present_os_confirm"):
            for f in ast.walk(_tree):
                if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and f.lineno <= n.lineno <= (f.end_lineno or 0)):
                    _callers.add(f.name)
    check(len(_callers) >= 2,
          "⭐⭐ 而且**两个入口都在用它**（轮内事件流 + 轮外消费者）",
          str(sorted(_callers)))


def main() -> int:
    t_whitelist_not_denylist()
    t_agent_cannot_spawn_agent()
    t_refusal_gives_an_exit()
    t_write_scope_is_exactly_one_tool()
    t_agent_label_rides_the_execution_chain()
    t_agent_prompt_matches_its_toolset()
    t_agent_ui_requests_survive_its_turn()
    t_resolve_really_blocks()
    t_isolation_and_report()
    t_original_instruction_survives()
    t_context_shape_is_api_legal()
    t_parallel_agents()
    t_agent_monitor_drawer()
    t_agent_run_record()
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
