# core/orchestrator/tool_dispatch.py
"""工具目录与工具执行：单个 / 批量执行、load_tools、失败描述、出口决策。（`Orchestrator` 的 mixin）"""

import asyncio
import time

from loguru import logger

from core.memory_store import EntryType
from core.orchestrator._runtime import current_agent_label
from core.orchestrator._types import ToolExecution, ToolOutcome, _ToolRuntimeView
from core.schema import AgentDecision, ToolCall
from core.tools import Preload, Scheduling, ToolOrigin, ToolScope
from core.tools.manifests import BUILTIN_MANIFESTS


class ToolDispatchMixin:
    """工具目录与工具执行：单个 / 批量执行、load_tools、失败描述、出口决策。"""

    # ══════════════════════════════════════════════════════════════════════
    # 工具失败诊断 —— 让模型拿到【正确且充分】的失败信息
    # ══════════════════════════════════════════════════════════════════════
    #
    # ═══ 这不是"安全网"，是在修一条说假话的错误信息 ═══
    #
    # 改造前，模型调一个**不存在**的工具名，收到的是：
    #
    #     "Error: the tool did not return any valid content."
    #
    # 这句话的意思是「工具跑了，但没返回内容」——**和事实正好相反**，
    # 工具压根没被调用（`registry.execute` 找不到就 `return None`，见 registry.py:280，
    # 然后 None 在这里被翻译成了上面那句）。
    #
    # 更糟的是：一个**真实存在**、只是 `run()` 返回了 None 的 Skill，
    # 拿到的是**一模一样的字符串**。两种完全不同的故障连区分都做不到，
    # 所以模型就算想诊断也诊断不出来，只能换参数重试或者再猜一个名字。
    #
    # 也就是说：模型不是"判断失误需要被兜住"，是**在一个假前提上做了在那个前提下
    # 完全合理的判断**。错的是我们。
    #
    # ═══ 判据═══
    #
    #     拿着这条失败消息，一个没有别的上下文的人能不能选出正确的下一步？
    #     不能就不合格 —— **哪怕它技术上没说错**。
    #
    # 所以这里要满足两条独立要求：
    #     正确 = 描述的是真实发生的事（区分"不存在" / "参数不对" / "跑了但失败"）
    #     充分 = 拿着它就能选出下一步（给相近真名字、说清 load_tools 帮不帮得上忙）
    #
    # ⚠️ **第一行必须是人话**：`result_text[:80]` 会作为 `result_summary`
    #    显示在用户可见的工具卡片上（见本文件 `tool_end` 事件），
    #    诊断块只能放在第一行之后。

    class _ToolFailCause:
        UNKNOWN_TOOL = "UNKNOWN_TOOL"       # 名字不存在
        DISABLED_TOOL = "DISABLED_TOOL"     # Skill 存在但被禁用了
        BAD_PARAMS = "BAD_PARAMS"           # 工具存在，参数不合 schema
        TOOL_ERROR = "TOOL_ERROR"           # 工具跑了并自己报了错
        # ⭐ 工具存在、当前健康、当前 eligible —— 只是产生这次调用的那次
        #    API request **没把它的 schema 给模型**（模型凭更早一次请求的记忆调的）。
        # ⚠️ **刻意不复用上面四类**：塞进 UNKNOWN_TOOL 是撒谎（它明明存在）、
        #    塞进 DISABLED_TOOL 也是（它没被禁用）、塞进 TOOL_ERROR 更错（它压根没跑）。
        #    📌 给模型的失败信息必须**正确**且**足够让它选出下一步** ——
        #       而这里正确的下一步是 `load_tools`，与那四类的下一步都不同。
        TOOL_NOT_ACTIVE = "TOOL_NOT_ACTIVE" # 存在且可用，但本次 request 没附它的 schema

    def _known_tool_names(self) -> list[str]:
        """当前进程里**全部合法**的工具名。给"最接近的真名字"用。

        🔴 改造前这里手工拼**四个来源**（registry / `_tool_pool` / `_CORE_TOOL_NAMES`
           / MCP），注释还自陈「四个来源缺一不可」—— 那句话本身就是证据：
           **一份靠"记得把四个都写上"维持的名单，漏一个不会报错，只会让诊断变笨。**
        ⭐ 现在只有一个来源。

        ⚠️ 与改造前有**一处刻意的差异**，留痕：
           改造前拼的是 `registry.list_enabled_skills()`（Skill **实例**字典），
           而目录投影的是 `registry.get_all_manifests()`（**manifest** 列表）。
           两者在正常情况下一致，但那种「manifest 顶层没有 name、整条被丢弃」
           的 Skill 只会出现在前者里 —— 它**装载成功、UI 显示 READY，而模型
           从来看不见它**。把这种名字放进"你是不是想调这个"的提示里，
           等于劝模型去调一个它拿不到 schema 的东西。
           📌 **半可见的能力比不可见更坏。**
        """
        return sorted(n for n in self._get_tool_catalog().names() if n)

    def _describe_tool_failure(self, name: str, cause: str, tool_error: str = "") -> str:
        """产出给模型看的诊断块。**只在失败时调用**——成功时一个字都不加。

        为什么不把 1/2/3 三条排查步骤丢给模型自己走：
        **分发的那一刻代码就已经知道是哪一类了。** 让模型再推一遍，
        等于在"别猜"的地方又开了一次猜的口子。直接把结论递给它。
        """
        import difflib
        # 同一轮内重复撞同一个名字 → 升级措辞，防止它在一轮里连试三个变体
        seen = getattr(self, "_tool_failures_this_turn", None)
        if seen is None:
            seen = self._tool_failures_this_turn = {}
        key = f"{name}::{cause}"
        seen[key] = seen.get(key, 0) + 1
        repeated = seen[key] > 1

        lines = ["", "[Tool Call Diagnostics — internal, not shown to the user]"]

        if cause == self._ToolFailCause.UNKNOWN_TOOL:
            # ⚠️ 必须**忽略大小写**地匹配。`difflib` 直接比 "getdns" 和 "GetDNSList"
            # 相似度只有 0.5 出头，够不到任何合理阈值——而大小写写错恰恰是最常见的
            # 一种错法（一次就修过 21 处 manifest 大小写不一致）。
            # 只按原样匹配的话，最该被提示的那一类反而永远提示不出来。
            known = self._known_tool_names()
            lower_map = {n.lower(): n for n in known}
            exact_ci = lower_map.get(name.lower())
            near = [lower_map[m] for m in difflib.get_close_matches(
                name.lower(), list(lower_map), n=3, cutoff=0.6)]
            lines += [
                "- tool_error: none (the tool was never invoked, so it produced no error of its own)",
                f"- cause: UNKNOWN_TOOL — no tool, Skill, or MCP tool is named \"{name}\".",
            ]
            if exact_ci:
                lines.append(
                    f"- ⭐ The correct name is \"{exact_ci}\" — you only got the capitalization wrong. "
                    f"Tool names are case-sensitive. Retry with the exact spelling."
                )
            elif near:
                lines.append(f"- closest existing names: {', '.join(near)}")
            else:
                lines.append("- no existing name is close to it.")
            lines += [
                # ⚠️ 这条必须说清楚，否则模型会去调 load_tools 然后原地打转：
                # 延迟的 Skill/MCP 即使没 load 也能按名字直接执行（分发与 schema 注入解耦），
                # load_tools 只负责给参数 schema，**不决定工具能不能调**。
                "- load_tools will NOT help here: it only supplies parameter schemas for tools "
                "that already exist. It cannot make a nonexistent name callable.",
                "- Next: call one of the names above if it matches your intent, or tell the user "
                "plainly that this capability does not exist. Do not try another guessed name.",
            ]

        elif cause == self._ToolFailCause.DISABLED_TOOL:
            # ⚠️ Skill 与 MCP 的**下一步不同工具**，所以这句不能合并成一句泛的。
            #    📌 失败信息要同时【正确】且**足够让它选出下一步** ——
            #       而"下一步"在两边是两个不同的工具名。
            _mcp_srv = ""
            try:
                from core.mcp_client import MCPManager as _MM_n
                _mcp_srv = _MM_n.instance().disabled_tool_server(name)
            except Exception:
                _mcp_srv = ""
            if _mcp_srv:
                lines += [
                    "- tool_error: none (the MCP server is disabled, so nothing was invoked)",
                    f"- cause: DISABLED_TOOL — the MCP server \"{_mcp_srv}\" is disabled.",
                    "- The tool name is fine. Do not look for another name and do not "
                    "try to reach the same capability some other way first.",
                    "- Next: tell the user that this MCP server is turned off, and ask "
                    f"whether to enable it (manage_mcp can enable \"{_mcp_srv}\").",
                ]
            else:
                lines += [
                    "- tool_error: none (the Skill exists but is currently disabled, so it was not invoked)",
                    f"- cause: DISABLED_TOOL — the Skill \"{name}\" exists but is disabled.",
                    "- The name is correct. Do not look for another name and do not recreate it.",
                    "- Next: tell the user it is disabled and ask whether to enable it "
                    "(manage_existing_skill can enable it).",
                ]

        elif cause == self._ToolFailCause.BAD_PARAMS:
            lines += [
                f"- tool_error: {tool_error or '(none)'}",
                f"- cause: BAD_PARAMS — \"{name}\" exists; your arguments did not match its schema.",
                "- The name is correct. Do not switch to a different tool.",
                f"- Next: call load_tools(names=[\"{name}\"]) to get the exact parameter schema, "
                "then retry with corrected arguments. Do not guess parameter names.",
            ]

        else:  # TOOL_ERROR
            lines += [
                "- cause: TOOL_ERROR — the tool exists and ran. The error above is its own.",
                "- That message is authoritative. Do not reinterpret it or second-guess it.",
                "- Next: fix the inputs based on that message, or report it to the user. "
                "Do not switch to a different tool name because of this error.",
            ]

        if repeated:
            lines.append(
                f"- ⚠️ You already called \"{name}\" this turn and it failed the same way. "
                "Stop retrying. Explain to the user what is missing instead."
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 统一工具目录的接入点
    #
    # ⭐ 这三个方法是 11 个消费点的**共同前置**：目录本身、这一刻的运行时事实、
    #    以及"把两者合起来问"的入口。
    # 📌 **Catalog 只回答"谁处理 / 该不该出现"；"怎么运行"仍是 Runner 的事**
    #    （GUI 租约等待 / health 门控 / 工具卡事件 / 长任务交还 / 错误分类 /
    #     EXIT 路径的同轮拒绝与终端事件收尾，一律留在原处）。
    # ══════════════════════════════════════════════════════════════════════

    def _tool_runtime_view(self) -> "_ToolRuntimeView":
        """这一刻的运行时事实 —— `availability` 判据的唯一输入。

        ⚠️ 刻意做成**窄接口**而不是直接把 orchestrator 传进去：
           一个 predicate 能拿到整个 orchestrator，就迟早有人在里面写副作用。
        """
        return _ToolRuntimeView(self)

    def _get_tool_catalog(self) -> "ToolCatalog":
        """装配工具目录：内置声明（静态）+ Skill / MCP（每次重建）。

        ⚠️ 内置声明只构造一次并缓存 —— 它是静态的，而且构造期不变量
        （manifest 名一致 / awareness 必填 / bindings 非空 / binding 方法真实存在）
        每次重跑纯属浪费。**动态部分每次重建**：Skill 会热加载、MCP 会重连。
        """
        _cat = getattr(self, "_tool_catalog", None)
        if _cat is None:
            from core.tools import ToolCatalog as _TC
            from core.tools.builtin import build_builtin_definitions as _bbd
            _cat = _TC()
            for _d in _bbd(BUILTIN_MANIFESTS):
                _cat.add_builtin(_d)
            # ⭐ 启动期响亮失败：binding 用方法名字符串写，写错的话**运行时才炸**，
            #    而且表现为「模型调了工具、然后什么都没发生」。
            _cat.assert_bindings_exist(self)
            self._tool_catalog = _cat
        _cat.clear_dynamic()
        try:
            from core.tools import mcp_definitions, skill_definitions
            for _d in skill_definitions(self.registry):
                _cat.add_dynamic(_d)
            _mgr = getattr(self, "_mcp_manager", None)
            if _mgr is not None:
                for _d in mcp_definitions(_mgr):
                    _cat.add_dynamic(_d)
        except Exception as _e:
            logger.warning(f"[F4] 动态工具投影失败（内置仍可用）：{_e}")
        # ⚠️ 被隔离的动态工具要**响亮**（但不致命）—— 一条撞名的用户 Skill /
        #    外部服务不该把整个 Nano 打死，但也绝不能静默消失。
        for _r in _cat.rejected():
            logger.error(f"[F4] 工具被隔离：{_r.name}（{_r.origin.value}）—— {_r.reason}")
        return _cat

    # ══════════════════════════════════════════════════════════════════════
    # 工具 handler —— 从 `_execute_one_tool_call` 的 name 分派链
    #             **机械提取**出来的分支体。
    #
    # ⚠️ **这是纯重构，不是 shadow**：旧的 if/elif 仍然调用它们，行为零变化。
    #    目的是让最终切换那一次只需要把
    #        `if name == A: ... elif name == B: ...`
    #    换成
    #        `handler = catalog.resolve(name, scope); await handler(...)`
    #    而**不必同时重写执行逻辑**。
    #
    # ⭐ 分层（外部评审的核心建议，已核实）：
    #    **Catalog 决定"谁处理"，Flow Runner 决定"怎么运行这一类 handler"。**
    #    所以 handler **只负责产出 result_text**；下面这些统一 plumbing
    #    一律留在 `_execute_one_tool_call` 里，不许被 handler 吞掉：
    #      GUI 租约等待 / health fail-fast / tool_start·tool_end 事件 /
    #      长任务交还 / 错误分类 / 挂起期日志 / ToolExecution 组装。
    #
    # 📌 统一签名 `(args, aid, *, event_queue) -> str`：
    #    AST 实测（提取前）确认 —— 分派链**之后**的代码只读 `result_text` 一个变量，
    #    所以"返回 result_text"就是这批 handler 的完整契约。
    # ══════════════════════════════════════════════════════════════════════

    def _ui_sink(self, event_queue):
        """这次 UI 往返该发到哪条通道。

        🔴🔴 **main agent 和 Subagent 的通道寿命不一样**（实测 2026-08-20 卡死）：
           轮内的 `event_queue` 由 `navigate_pipeline` 的 `async for` 消费，
           **那个循环随本轮结束而退出**。Subagent detach 之后活得比它长 ——
           于是Subagent发的 `os_action_confirm` 落进一个没人读的队列，
           弹窗永远不出现，Subagent在确认闸上干等 300 秒，屏幕上什么都没有。
        📌 **一个跨过了自己那一轮的执行者，不能再用那一轮的通道去要 UI。**
        ⚠️ 这个洞只在**需要 UI 往返**的事件上存在：Subagent此前全是只读工具，
           一次往返都不需要 —— 给它写权限的那一刻才暴露。
           📌 一条只在某种能力出现后才会被走到的路，它的缺陷会**和那个能力
              同一天诞生**，而不是在它被写下的那天。
        ⚠️ 取不到轮外通道时**退回轮内**（fail-safe：最坏退化成今天的行为，
           而不是把授权请求整个丢掉）。
        """
        try:
            if current_agent_label():
                _oob = getattr(self, "_ui_oob_events", None)
                if _oob is not None:
                    return _oob
                logger.error("[OOB] Subagent要弹窗，但轮外通道不存在 —— "
                             "退回轮内通道（本轮结束后它就没人读了）")
        except Exception:
            pass
        return event_queue

    async def _handle_load_tools(self, args: dict, aid: str, *,
                                 event_queue, **_ctx) -> str:
        # 按需加载工具：匹配全量池 → 暂存，由 ReAct 循环在本批结束后 append 进 tools_manifest
        # ⑤ 匹配问目录 —— 改造前是一张**人工维护的关键词分组表**（`_GROUPS`：
        # "网页/浏览/browser…" → `nm.startswith("browser_")` 之类），加一个新工具
        # 就要记得往表里塞一条，漏了的表现是「load_tools 搜不到它」。
        # ⭐ 现在检索文档由声明**自动**构建（name + awareness + description +
        #    所有 property 名 + **所有 enum 值**），于是
        #    `load_tools(query="file_delete")` 天然精准命中 `os_execute` ——
        #    因为 39 个 action 的 enum 全在它的检索文档里，
        #    **而 awareness 行一个 action 都不用列**。
        # ⚠️ 只在本作用域此刻 eligible 的集合里搜（HIDDEN 的搜不到）——
        #    搜出一个当前不该出现的工具，等于把 eligible 那条不变量从后门破掉。
        _matched = [
            d.manifest for d in self._get_tool_catalog().search(
                args.get("query", ""), args.get("names") or [],
                scope=ToolScope.MAIN, runtime=self._tool_runtime_view())
        ]
        # ⭐⭐⭐ [2026-08-23 实测回归] **必绑定的工具，加载也要绑定。**
        #
        # 🔴 实测症状：模型 `load_tools(computer_use)` 之后，要缩小窗口时填了
        #    `computer_use(action='set_window_mode')` → 「unknown action …
        #    actions must come from the closed enum and cannot be invented」。
        #
        # ⭐ 它不是在乱编：`computer_use` 的描述里明写着「先 call
        #    set_window_mode('mini')」，而 `set_window_mode` 是 DEFERRED、
        #    **这一轮根本没被加载**。手上没有那个工具，它只好拿手上有的那个凑。
        #
        # 📌 **我们声明了「必绑定」，却把绑定的两个放在了不同的加载状态** ——
        #    这正是这次拆分要消除的那个问题（「一组必绑定的东西被放进两个不同的
        #    可见性层级」），而在修它的同一轮里又造了一次。
        # 📌 **一个绑定关系，该由系统保证，不该靠模型记得一起加载。**
        #    靠模型记得 = 又一条「要求每个调用方自觉」的机制，而它漏的时候不报错，
        #    只会让模型编一个不存在的 action。
        #
        # ⚠️ 只补**真正的强绑定**的：`computer_use` 一旦上场，Nano 自己的窗口
        #    必然挡在它要操作的那块屏幕前面 —— 这是**系统事实**，不是偏好。
        #    ⚠️ `look_at_screen` **不在这里**：它和 computer_use 是「常一起出现」，
        #       不是「缺了就干不成」。📌 把「经常一起」当成「必须一起」，
        #       会让按需加载退化成打包加载。
        # ⚠️ 2026-08-23 机械扫了一遍**全部工具描述**（谁提到了谁 + 双方 preload），
        #    发现同形的一共三处 —— 修掉的那处只是其中之一，而
        #    `look_at_screen → set_window_mode` **比它更老**：
        #    📌 **这不是拆分引入的新问题，是拆分让一个既有的坑更容易被踩到。**
        #    ⭐ 筛选判据：那句话是「你**现在就得**去调它」还是「以后可能用到它」——
        #       前者才是真风险（模型此刻要用、手上没有 → 它会编一个）。
        #       像 `os_execute → computer_use`（"load it when you need it"）
        #       就不算，那句话本身就是在叫它去加载。
        _BOUND_WITH = {
            # 「FIRST … This one is not optional」——GUI 操作前必须缩小自己
            "computer_use": ("set_window_mode",),
            # 「FIRST shrink yourself … BEFORE your first look_at_screen」
            "look_at_screen": ("set_window_mode",),
            # 成对：没有表就没有步骤可更新
            "update_task_step": ("create_task_list",),
        }
        _have = {m.get("name") for m in _matched}
        _extra_names = [n for src in _have for n in _BOUND_WITH.get(src, ())
                        if n not in _have]
        if _extra_names:
            _bound = [d.manifest for d in self._get_tool_catalog().search(
                "", _extra_names, scope=ToolScope.MAIN,
                runtime=self._tool_runtime_view())]
            if _bound:
                logger.info(f"[F4] 连带加载 {[m.get('name') for m in _bound]}"
                            f"（被 {sorted(_have & set(_BOUND_WITH))} 强绑定）")
                _matched = _matched + _bound
        if not hasattr(self, "_pending_loaded_manifests"):
            self._pending_loaded_manifests = []
        self._pending_loaded_manifests.extend(_matched)
        if _matched:
            return (
                # ⚠️ 旧文案是 "They can now be called directly."，**没有作用域限定**。
                # 两个方向都假：
                #   ① 不跨轮 —— 每轮开头都会重建 manifest（实测日志：某轮 10 个工具，
                #      下一轮就回到 6 个），但这句话会永久留在对话历史里；
                #   ② 不跨子作用域 —— 主循环加载的工具不进 Skill Explorer 的 manifest。
                # 而它作为普通 tool_result 会写进 MemoryManager，于是一句瞬时状态
                # 变成了模型可以永久引用的"事实"。Explorer 死路事故就是
                # 模型据此再次调用了当前作用域里根本不存在的 create_new_skill。
                #
                # 修在源头比事后清洗历史便宜得多：改这一句，所有作用域一起受益。
                # 见本文件里「运行作用域与事实生命周期」那段说明。
                "Loaded capabilities: " + ", ".join(m.get("name", "") for m in _matched)
                + ". This applies to the current step only. What you can actually call is "
                  "always defined by the tools attached to the current request — not by this "
                  "message. Do not treat this as evidence that these tools are available in a "
                  "later turn or inside a specialized sub-flow. Call the needed tool in the next step."
            )
        return (
            "No matching capability was found. Use names to specify exact tool names, "
            "or describe the task differently and try load_tools again."
        )

    @staticmethod
    def _drain_event_queue(event_queue) -> list:
        """把队列里攒着的事件取干净，交给调用处 yield 出去。

        ⭐ 为什么 exit 路径需要它：`event_queue` 的常规排空循环长在**工具批次
        那一段**里，exit 工具在到达那段之前就路由走了。于是这条路径上**任何**
        `event_queue.put` 都是只进不出 —— 不只是工具卡片，`realtime_callback`
        发的 `thinking` 状态事件也一样卡在里面（表现为探索阶段状态行不更新）。

        非阻塞：只取已经在队列里的，不等新的，绝不拖慢路由。
        """
        out = []
        while not event_queue.empty():
            try:
                out.append(event_queue.get_nowait())
            except Exception:
                break
        return out

    # ══════════════════════════════════════════════════════════════════════
    # exit-flow 薄适配器
    #
    # ⭐ 它们存在的唯一理由：**让 EXIT runner 里不再有一张按工具名分派的表。**
    #    改造前那里是 `if exit_call.name == "update_existing_skill": … elif …`，
    #    而那正是这次要删的第二份权威（那条 AST 断言要管的就是它）。
    #
    # 📌 分层没变，仍然是那条：**Catalog 决定"谁处理"，Runner 决定
    #    "怎么运行这一类 handler"。** 所以这五个适配器里**只有各自那一支
    #    原本就在做的事**（构造 `AgentDecision` / 补 requirement / supersede 澄清 /
    #    排空队列），而公共 plumbing —— 同轮拒绝、`tool_start`·`tool_end`、
    #    终端事件前收尾、`exit_flow_defer_to_model` 拦截 —— **一行都没搬**，
    #    仍然长在 runner 里。
    #
    # ⚠️ 统一签名 `(exit_call, decision, *, used_model, base_guide,
    #    realtime_callback, event_queue) -> AsyncIterator[dict]`。
    #    参数**取全集**：每个适配器只用它自己需要的那几个，用不上的不用管 ——
    #    这比让 runner 去猜"这个 handler 要哪几个参数"可靠得多
    #    （猜的那条路只有两种实现：按名字特判，或者反射签名，
    #     前者是我们正在删的东西，后者会把"参数写错"从启动期错误变成运行期错误）。
    # ══════════════════════════════════════════════════════════════════════

    def _exit_decision_from(self, exit_call, decision) -> "AgentDecision":
        """把 exit 的 `ToolCall` 还原成 `AgentDecision`（三个适配器都要）。"""
        return AgentDecision(
            "call", name=exit_call.name, args=exit_call.args,
            tool_use_id=exit_call.tool_use_id,
            thinking_blocks=decision.thinking_blocks,
        )

    async def _execute_one_tool_call(
        self,
        call: ToolCall,
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> ToolExecution:
        """执行单个工具调用，把 tool_start/tool_end 事件推进 event_queue。

        不负责写 memory（由 ReAct 主循环统一批量写入）。
        """
        self._ensure_react_sems()
        name = call.name
        args = call.args or {}
        aid = f"react_{call.index}_{time.time_ns()}"
        raw_result = None
        tool_data = None
        # 目录 + 这一刻的运行时事实。工具卡文案、handler 解析都问它。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()

        # ⭐⭐⭐ **在 GUI 模式里，任何工具调用之前都先看机器归谁。**
        #
        # 「只要 mini 窗口存在，不管是 nano 任何行为 ——
        # 命令行、mcp、键鼠模拟、skill 等等，全部受被动挂起的监控，
        # **不管 nano 目前在做什么，只要用户进行操作，一律挂起**。」
        #
        # ⚠️ 第一版只把**感知**换成了全量，**等待**还留在 `os_execute` 里、
        #    而且还额外要求是键鼠动作 —— 用户当场看出来只做了一半。
        #    📌 **感知的范围和反应的范围必须一致**，否则用户看到接管状态条却发现
        #    Nano 照样在动，那正是一开始诊断出的那个"UI 语义不一致"。
        #
        # ⭐ 顺带更正之前一个判断：曾经认为"截图不该被拦，因为恢复后要判断环境
        #    变没变恰恰需要能看"。但 `_look_at_screen` **会最小化/还原自己的窗口**
        #    来避开遮挡 —— 那是**抢焦点**，它确实在跟用户争。所以它也该等。
        #
        # ⚠️ 等待期间零 LLM、零 token（纯 `asyncio.sleep`），只占一个挂着的 turn。
        # ⚠️ 不在 GUI 模式时**完全不查**（一次数据库读都不多花）——
        #    纯命令行 / 纯对话的一轮完全不受影响。
        _gui_waited = 0.0
        if name in ("computer_use", "look_at_screen", "set_window_mode"):
            self._gui_task_touch()
        try:
            from core.runtime import oslease as _ol_gw
            from core.runtime.kernel import get_kernel as _gk_gw
            _k_gw = _gk_gw()
            if _ol_gw.gui_session_active(_k_gw):
                _may_gw, _why_gw = _ol_gw.nano_may_touch_os(_k_gw)
                if not _may_gw:
                    logger.info(f"[A3] GUI 模式中，机器不归 Nano（{_why_gw}）—— "
                                f"工具 {name!r} 前进入等待（**不结束 turn**）")
                    _h_gw, _gui_waited = await _ol_gw.await_activity_lease(
                        f"before tool {name}")
                    # ⚠️ 只有键鼠动作才真的需要**持有**租约；其余工具等到"没人占着"
                    #    就够了，拿了反而会让 canary 误以为 Nano 在开车。
                    #    📌 「等到可以动」和「持有驾驶权」是两件事。
                    if _h_gw is not None:
                        try:
                            from core.os_layer import dsl as _dsl_gw
                            if name == "os_execute" and _dsl_gw.contends_for_machine(
                                    (args or {}).get("action") or ""):
                                self._rt_os_lease = _h_gw
                            else:
                                _ol_gw.release_activity(_h_gw)
                        except Exception:
                            _ol_gw.release_activity(_h_gw)
                    logger.info(f"[A3] 等了 {_gui_waited:.0f}s，继续执行 {name!r}")
                    # ⭐⭐ 取出这段挂起期的证据日志。
                    #
                    # ⚠️⚠️ **必须在这里取，不能只在 `os_execute` 那一支取** ——
                    #    等待本身是**全量**的（命令行 / MCP / skill / 截图 / 键鼠都会等），
                    #    只在键鼠那一支消费的话，"用户接管期间发生了什么"这条信息
                    #    就取决于 Nano 恢复后**碰巧先调哪个工具**。
                    #    📌 **一份一次性的证据，取它的地方必须和产生它的地方同一个作用域。**
                    #
                    # ⚠️ 日志是**一次性**的：`finish()` 取完即清。所以这里取到就必须
                    #    真的送到模型面前（见函数尾部那段），不能取了不用 —— 那等于丢证据。
                    try:
                        from core.proactive import takeover_log as _tl_gw
                        _sess_gw = _tl_gw.finish()
                        _sum_gw = _tl_gw.summarize(
                            _sess_gw, _tl_gw.sensors_healthy(_sess_gw))
                        if _sum_gw:
                            self._a3_pause_note = _sum_gw
                            logger.info("[A3] 挂起期证据日志已生成，"
                                        f"{len(_sum_gw)} 字符 → 随本次工具结果交给模型")
                    except Exception as _e_tl:
                        logger.debug(f"[A3] 挂起期日志生成失败（不阻断）: {_e_tl}")
        except Exception as _e_gw:
            logger.debug(f"[A3] GUI 模式等待检查失败（放行）: {_e_gw}")

        # ── 能力门控·第二道：执行层 fail-fast ────────────────────────────
        # manifest 门控（第一道）能挡住绝大多数，但挡不住这三种：模型沿用上一轮已加载
        # 的旧 schema、load_tools 动态拉进来的、以及故障恰好发生在本轮 manifest 构建
        # 之后。所以真正动手前再查一次——宁可返回一个结构化的"不可用"，也不让它进到
        # RAG 函数里摔出原生异常。
        try:
            from core.health import get_health
            _blk = get_health().tool_block_reason(name)
        except Exception:
            _blk = None
        if _blk is not None:
            _rt = (
                f"Tool \"{name}\" is currently unavailable: {_blk.user_message} "
                + (f"Recovery hint: {_blk.recovery_hint} " if _blk.recovery_hint else "")
                + "Do not retry this tool. Tell the user plainly what is broken, and continue with "
                  "whatever you can still do."
            )
            logger.warning(f"[Health] 执行层拦截工具【{name}】：{_blk.code}")
            await event_queue.put({
                "event": "tool_start", "action_id": aid,
                "tool_use_id": getattr(call, "tool_use_id", "") or "",
                "action_display": _cat.presentation(name, args),
                "action_input": str(args)[:200],
                "log": f"能力不可用，已拦截【{name}】",
                "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
            })
            await event_queue.put({
                "event": "tool_end", "action_id": aid,
                "result_summary": f"能力不可用：{_blk.user_message}"[:80],
                "status": "SYS_IDLE", "model": used_model,
                "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
            })
            return ToolExecution(call=call, result_text=_rt, ok=False,
                                 error=f"capability unavailable: {_blk.code}")

        # ══════════════════════════════════════════════════════════════════
        # ⭐⭐⭐ 本次 request 的 tool contract 闸门
        # ══════════════════════════════════════════════════════════════════
        #
        # ═══ 它堵的洞（实测实证 2026-08-13）═══
        #
        #     07:55:39  [TOKEN-PLAN] core_tools=6（不含 create_new_skill，本轮也没 load_tools）
        #     07:55:49  [Router] create_new_skill 触发（requirement='部署这个吧'）  ← 照样执行了
        #
        # 用户对着一张待审卡说「部署这个吧」，模型**凭上一轮的 schema 记忆**调了
        # `create_new_skill`，而 `answer_open_interaction` 就在它手边。
        # ⭐ 这正是 要修的问题本身：**模型把"我曾经能调"当成"我现在能调"。**
        #
        # ═══ ⚠️ 这一条【推翻】了工具目录里的一条既有规定，不是 bug 修复 ═══
        #
        # 原规定（`catalog.py` 模块头 + `t_f4_catalog.py` 模块头两处）：
        #     「deferred 工具『schema 当前没附带但仍可执行』是**正常状态**」
        # **现在改成：主模型的工具调用必须受【产生该调用的那次 API request 的
        # tool contract】约束。** 读到 `catalog.py` 那条的人请看这里，
        # 不要以为 Catalog 被改坏了。
        #
        # ⚠️ 而这不是给正常路径加一轮：`DEFERRED_HEADER` 本来就白纸黑字写着
        #    「call load_tools first … then call the loaded tool **on the next step**」。
        #    所以这是**关掉一条与自己声明矛盾的旁路**。
        #
        # ═══ 五格顺序（少一格就会抢掉别处已经正确的诊断）═══
        #
        #   1. health blocked          → 上面那段（已 return）。已经正确，本闸不许抢
        #   2. Preload.HIDDEN          → 放行到既有内部语义，**绝不能建议 load_tools**
        #                                （HIDDEN 的定义就是"处理得了但刻意不给正式通路"，
        #                                  而 `load_tools` 本来就搜不到它）
        #   3. availability=False      → 放行到 handler，由它说「你要处理的那个对象没了」
        #                                （`eligible ⊆ resolvable` 单向包含正为它设计，
        #                                  失败信息必须**正确**，"不存在"是假话）
        #   4. eligible+DEFERRED+未附  → **本闸拦在这里** → TOOL_NOT_ACTIVE
        #   5. 名字不存在              → 下游既有的 UNKNOWN_TOOL 诊断（含最接近的真名字）
        #
        # ⚠️ **fail-open 是刻意的**：拿不到 `active_tool_names`（老路径、测试替身、
        #    子流程）时**不校验**。📌 一道新加的闸，在信息不全时应当放行 ——
        #    否则它会在自己还没接全的地方制造假故障。
        _active = active_tool_names
        if _active:
            _d6 = _cat.get(name)
            if (_d6 is not None                                  # 存在（否则交给 ⑤）
                    and _d6.preload is Preload.DEFERRED           # 排除 CORE / HIDDEN（②）
                    and _d6.is_eligible(ToolScope.MAIN, _rtv)     # 排除 availability=False（③）
                    and name not in _active):                     # 本次 request 没附它
                logger.warning(
                    f"[F6] 工具【{name}】不在本次 request 的工具表里 → 拒绝执行"
                    f"（本次 attach 了 {len(_active)} 个）"
                )
                _rt6 = (
                    f"Tool \"{name}\" was not executed because it was not active in the "
                    f"tool set attached to this model request.\n"
                    f"[Tool Call Diagnostics]\n"
                    f"- cause: {self._ToolFailCause.TOOL_NOT_ACTIVE} — the tool exists and is "
                    f"currently available, but its schema was not attached to this request.\n"
                    f"- This call came from stale knowledge of an earlier request's tool set, "
                    f"not from the tool contract of the request you are answering now.\n"
                    f"- Next: call load_tools(names=[\"{name}\"]) and then call it on the "
                    f"following step. Loading takes effect from the next step, not this one."
                )
                await event_queue.put({
                    "event": "tool_start", "action_id": aid,
                    "tool_use_id": getattr(call, "tool_use_id", "") or "",
                    "action_display": _cat.presentation(name, args),
                    "action_input": str(args)[:200],
                    "log": f"工具未在本轮工具表中，已拦截【{name}】",
                    "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
                })
                await event_queue.put({
                    "event": "tool_end", "action_id": aid,
                    "result_summary": f"未加载：{name}"[:80],
                    "status": "SYS_IDLE", "model": used_model,
                    "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
                })
                return ToolExecution(call=call, result_text=_rt6, ok=False,
                                     error=f"tool not active in this request: {name}")

        await event_queue.put({
            "event": "tool_start",
            "action_id": aid,
            # ⭐ 详情按 `tool_use_id` 回账本取（参数与结果都在那边）。
            #    ⚠️ **刻意不带 args** —— 那会让参数有两个来源（live 走事件 /
            #    重放走账本），而两个来源只在"我两次想法相同"时一致。
            "tool_use_id": getattr(call, "tool_use_id", "") or "",
            "action_display": _cat.presentation(name, args),
            "action_input": str(args)[:200],
            "log": f"ReAct: 执行工具【{name}】",
            "status": "TOOL_EXECUTING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
        })

        # 统一失败标记。⚠️ 原注释写「内置工具不会设它」——**那句已过期**：
        # 实际有 5 个内置工具会设（set_next_checkin / task_boundary / cancel_wait /
        # wait_for / os_execute），它们的 handler 返回 `ToolOutcome(text, failed)`。
        # 📌 这正是 要修的问题的一个小样本：**注释描述的能力与实现不一致，而且不报错。**
        _is_failed = False
        try:
            # ── 内置伪工具 ───────────────────────────────────────────────
            # 分支体已机械提取成 `_handle_*`（纯重构，行为零变化）。
            # cutover 时这一串 if/elif 会被 `catalog.resolve(name, scope)` 取代。
            # ── 内置工具：**问目录谁来处理** ─────────────────────────────
            #
            # 🔴 改造前这里是 **19 段 `elif name == "..."`** —— 一份按工具名建立的
            #    第二权威。分支体已机械提取成 `_handle_*`（纯重构、行为
            #    零变化），这一步只把「谁处理」这个问题交还给唯一权威。
            #
            # ⚠️ 统一契约是**实测**出来的、不是设计出来的：提取前用 AST 量过，
            #    分派链**之后**只读 `result_text` 一个变量 —— 所以 handler 的完整
            #    契约就是「产出文本 + 这次算不算失败」，`str | ToolOutcome` 两种写法
            #    由 `ToolOutcome.of()` 归一（同 `SkillResult.from_raw` 的既有范式：
            #    **一个便利构造器，不是两种并存的契约**）。
            #
            # ⚠️ 参数**取全集**：`call` / `used_model` / `gui_waited` 只有 `os_execute`
            #    用得上，其余 handler 由 `**_ctx` 收着。为什么不"按需传"——
            #    按需传只有两种实现：按名字特判（正是这次要删的东西），
            #    或反射签名（把"参数写错"从启动期错误变成运行期错误）。
            #
            # ⚠️ **只有内置走这条路**。本地 Skill 与 MCP 的执行体仍是下面两段内联
            #    分支：它们除了 `result_text` 还要回填 `raw_result` / `tool_data`
            #    （Skill 的 `dsl_plan` 转路由就靠它），契约比这批 handler 宽 ——
            #    那次机械提取当时也正是按这条线划的（19/19 = 全部内置）。
            #    📌 目录对这件事**如实描述**：它们的 binding 指向
            #       `_execute_one_tool_call` 本身，因为"谁处理"现在确实是这个函数
            #       （同探索作用域的 binding 指向 `_run_skill_exploration` 那个大循环
            #        —— **不为了好看虚构一层**）。
            _def = _cat.get(name)
            _handler_ref = _cat.resolve(name, ToolScope.MAIN, _rtv)
            if (_def is not None and _def.origin is ToolOrigin.BUILTIN
                    and _handler_ref is not None):
                _out = await getattr(self, _handler_ref)(
                    args, aid, event_queue=event_queue,
                    call=call, used_model=used_model, gui_waited=_gui_waited)
                if isinstance(_out, ToolExecution):
                    # `os_execute` 独有的两处提前返回（机器被占 / 长命令交还）：
                    # 语义是「这次调用已经自己完成了全部收尾」，所以直接 return。
                    return _out
                _outcome = ToolOutcome.of(_out)
                result_text, _is_failed = _outcome.text, _outcome.failed

            elif self._is_mcp_tool(name):
                # ── MCP 工具执行（Nano 自身能力，内联分发，不进 registry）──
                # 照 os_execute/render_visual/wait_for 范式：懒加载 manager、调用、回填
                # result_text。tool_start/tool_end 卡片由本函数头尾自动发，无需手动加。
                _mcp_mgr = self._mcp_manager
                # 在场确认：仅当 server 明确标 destructive（不可逆）时弹确认卡，复用 execution_confirm。
                # 普通读/写不拦——透明度由工具卡片保证（对齐"可逆直接做、不可逆先确认"）。
                _mcp_confirm = _mcp_mgr.confirm_summary(name)
                from core.os_layer import dsl as _dsl_auto
                if _mcp_confirm and not _dsl_auto.auto_skips_confirmation(f"MCP 工具 {name}"):
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    from core.runtime import replies as _replies
                    _rid = _replies.register({"confirm": _on_confirm, "cancel": _on_cancel})
                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": [_mcp_confirm],
                        "reply_id": _rid, "actions": ["confirm", "cancel"],
                    })
                    from core.runtime import inbox as _ib6
                    try:
                        _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    finally:
                        _replies.discard(_rid)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True
                    if _cancelled[0]:
                        # ⚠️ 两种「没执行」要分开说：**用户改口**和**干等超时**
                        #    在结果上一样，但要告诉模型的话完全不同。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (
                                f"External capability \"{name}\" was NOT executed.\n"
                                + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"External capability \"{name}\" was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end", "action_id": aid,
                            "result_summary": result_text[:80], "status": "SYS_IDLE",
                            "model": used_model, "current_skill": name, "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ── 自动后台化（MCP 就是 Nano 的一部分，长调用不特殊对待）──────────
                # 先发起调用，race 一个阈值。阈值内回 = 同步快路径（绝大多数 MCP 是
                # 请求/响应）；超阈值 = 系统【自动】转后台（不需要模型判断哪个工具是
                # "长任务"）：登记 background 挂起 + 把还在跑的调用交给 _start_bg_task
                # 生产端 + 出等待 pill，让模型说句"我去跑着"结束本轮，跑完
                # notify_background_done 自动唤醒续做。复用整套背景唤醒桥。
                # ⭐⭐⭐ **ref 必须在发起调用之前就定下来。**
                #    🔴 上一版是在「超阈值」那条分支里才算 `_bg_ref` ——
                #       那已经是 90 秒之后，而进度订阅必须在**请求发出时**
                #       就带上 `progressToken`，事后补不上。
                #    📌 **一个「出问题时才需要的标识」，如果它同时是
                #       「观测的订阅凭据」，就必须在事情开始时就存在** ——
                #       等到出问题才生成，前 90 秒的进度就永远丢了。
                _bg_ref = f"long_{call.tool_use_id or aid}"
                _bg_display = _cat.presentation(name, args)
                _mcp_task = asyncio.create_task(_mcp_mgr.call(
                    name, args, timeout=self._HANDED_BACK_DEADLINE_SEC,
                    progress_ref=_bg_ref))
                # ⭐ 等它跑完**或者等到用户又说话** —— 见 `_wait_or_user_speaks`。
                _mcp_finished = await self._wait_or_user_speaks(
                    _mcp_task, self._LONG_TASK_HANDBACK_SEC)
                _mcp_done = {_mcp_task} if _mcp_finished else set()
                self._session_log_append(f"[MCP call] {name}")
                self._last_called_skill = name

                if _mcp_task in _mcp_done:
                    # 快路径：阈值内返回 → 这条 ref 再没人看，丢掉轨迹。
                    # ⚠️ 不丢的后果不是崩，是**总线上慢慢堆满已完成的调用** ——
                    #    `_MAX_REFS` 那个兜底会开始丢掉真正活着的那些。
                    #    📌 一个兜底上限的存在，不免除正常路径显式收尾的义务。
                    try:
                        from core.runtime import progress as _pb1
                        _pb1.forget(_bg_ref)
                    except Exception:
                        pass
                    _mcp_text, _mcp_err, _mcp_needs_auth, _mcp_server = _mcp_task.result()
                    result_text = _mcp_text
                    _is_failed = bool(_mcp_err)
                    if _mcp_needs_auth:
                        # 在场授权（OAuth 延后授权）：server 需登录。发事件让 UI 渲染授权卡。
                        await event_queue.put({
                            "event": "mcp_auth_required",
                            "server": _mcp_server,
                            "tool": name,
                        })
                        result_text = (
                            f"External service \"{_mcp_server}\" requires login authorization before use. "
                            "Tell the user in one sentence that authorization is required before this capability can be used. "
                            "They can log in from MCP connections in settings."
                        )
                    if _is_failed:
                        self._last_skill_error = {
                            "skill": name,
                            "error": f"Tool \"{name}\" failed: {result_text}",
                            "consumed": False,
                        }
                else:
                    # ⭐ 慢路径：超阈值 → **交回控制权**（不是「转后台」）。
                    #    公共合同见 `_hand_back_long_task` —— MCP 在这里
                    #    **没有任何特殊之处**，它只是恰好是第一个用上它的载体。
                    result_text = await self._hand_back_long_task(
                        display=_bg_display, bg_ref=_bg_ref,
                        action_id=aid, event_queue=event_queue)

                    # ⭐⭐⭐ **形状转换放在知道形状的这一端。**
                    #
                    # 🔴 上一版直接把 `_mcp_task` 塞进事件，而 app 侧的消费端写的是
                    #    `_txt, _err, _na, _srv = await _t` —— **写死了 MCP 的四元组**。
                    #    于是长命令接进来那一步（它的载体返回一个字符串）
                    #    在完成的那一刻会被解成
                    #    `后台执行失败：too many values to unpack (expected 4)`。
                    #    ⚠️ 而当时的注释写的是「事件名保留 `mcp_background_request`
                    #       —— 改它要同时动 app 侧」——
                    #       📌 **一个「保留旧名字」的决定，必须同时检查那个事件的
                    #          消费端还假设着什么。名字兼容 ≠ 形状兼容。**
                    #
                    # ⭐ 修法：每个载体自己把结果包成**给模型看的文本**，
                    #    消费端只 `await` 出一个字符串。
                    #    📌 **形状转换要发生在知道形状的那一端**，
                    #       不是让公共消费端认识每一种载体。
                    async def _await_mcp(_t=_mcp_task, _n=name, _r=_bg_ref):
                        try:
                            _txt, _err, _na, _srv = await _t
                        except Exception as _e:
                            return (f"External capability \"{_n}\" failed after it was "
                                    f"handed back: {type(_e).__name__}: {_e}")
                        finally:
                            try:
                                from core.runtime import progress as _pb2
                                _pb2.forget(_r)
                            except Exception:
                                pass
                        if _err:
                            return (f"External capability \"{_n}\" finished with an "
                                    f"error:\n{_txt}")
                        return f"External capability \"{_n}\" finished:\n{_txt}"

                    await event_queue.put({
                        "event": "long_task_handback",
                        "task": asyncio.ensure_future(_await_mcp()),
                        "bg_task_ref": _bg_ref,
                        "display": _bg_display,
                    })
            else:
                # 兜底：模型有时把 os_execute 的 action 名（file_write/click/run_command…）
                # 当成独立工具直接调 → registry 里没有 → 会死 fail。这里识别并引导回正道。
                #
                # 🔴🔴 改造前这里是**手抄的 29 个 action**，而 `dsl.py` 实际有 **39 个**
                #    —— 漏了 10 个，且专挑高频项漏：`file_delete` / `file_move` /
                #    `write_registry` / `network_config` / `manage_service` /
                #    `modify_startup` / `schedule_task` / `set_env_var` /
                #    `read_screen_region` / `request_user_choice`。
                #    后果具体：模型把 `file_delete` 当成独立工具调时，兜底**不会**告诉它
                #    "这是 os_execute 的一个 action"，只给一句泛泛的"工具不存在"。
                # 📌 **一份手抄的清单，它的过期是静默的，而且专挑高频项漏。**
                #
                # ⚠️⚠️ **权威取 `dsl.ALL_ACTION_NAMES`，不取 `os_execute` manifest 的
                #    `action.enum`** —— 早先写的是后者，但回代码一核，那个 enum
                #    **本身也是手抄的、也过期了**：它只有 36 个，缺 `move` /
                #    `read_screen_region` / `request_user_choice`，而且 `move` 恰好
                #    是旧 `_OS_ACTIONS` 有、enum 没有的那一个 —— 照早先的设计做会
                #    **让这条兜底反而少认一个 action**。
                #    📌 判据就是 自己那条：**派生要派生到原生权威为止**。
                #       `_ACTIONS` 表在 `dsl.py`，它是执行器真正查的那张表；
                #       manifest 的 enum 只是它的又一份手抄件 ——
                #       **从一份手抄件派生，得到的还是手抄件。**
                #    （enum 少的那 3 个是另一个独立问题：模型 schema 层根本填不了
                #      它们。已单独留痕，不在本次"只切权威"的范围内。）
                try:
                    from core.os_layer import dsl as _dsl_fb
                    _os_actions = _dsl_fb.ALL_ACTION_NAMES
                except Exception:
                    _os_actions = frozenset()
                if name in _os_actions:
                    # ⭐⭐⭐ [2026-08-23 拆分] **必须按归属指路，不能写死 `os_execute`。**
                    #
                    # 🔴 这是整次拆分里**最危险的一处**：它是一条**纠错指引** ——
                    #    模型把 action 当独立工具调时，靠它回到正道。
                    #    拆分后 `click` 属于 `computer_use`，而这句话若仍写死
                    #    `os_execute`，就会把模型**精确地导向错误的工具**，
                    #    然后它拿到「没有这个 action」，再猜一次。
                    # 📌 **一条指错方向的纠错提示，比没有这条提示更坏** ——
                    #    没有时模型会重新想，有错的时它会照着走。
                    # ⚠️ 归属取 `dsl` 的 `tool` 字段（唯一权威），不另维护映射。
                    try:
                        _owner = _dsl_fb._ACTIONS[name].tool
                    except Exception:
                        _owner = "os_execute"
                    _rt = (
                        f"\"{name}\" is not a standalone tool. It is a {_owner} action. "
                        f"Call {_owner}(action='{name}', params={{...}}, declared_risk=...)."
                        + ("" if _owner == "os_execute" else
                           f" {_owner} is not loaded by default - load it first.")
                    )
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _rt[:80], "status": "SYS_IDLE",
                        "model": used_model, "current_skill": name, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=_rt, ok=False,
                                         error="os_execute action was incorrectly called as a standalone tool")
                # ── 先判"名字存不存在"，再往下走 ───────────────────
                # 必须在 execute() 之前判：`registry.execute` 对"找不到"和
                # "跑了但返回 None"都返回 None，事后无法区分（见 registry.py:280）。
                #
                # ⚠️ 也必须在**副作用确认之前**判。这个位置一开始放错了，
                # 结果是给一个根本不存在的 Skill 弹副作用确认框，然后
                # `asyncio.wait_for(..., timeout=300)` 干等五分钟——
                # 用户会看到一个"要不要允许 XXX 写文件"的框，而 XXX 压根不存在。
                _enabled_names = []
                try:
                    _enabled_names = self.registry.list_enabled_skills()
                except Exception:
                    pass
                if _enabled_names and name not in _enabled_names:
                    _disabled = []
                    try:
                        _disabled = self.registry.list_disabled_skills()
                    except Exception:
                        pass
                    # ⭐⭐ **MCP 也要能说出「它被禁用了」。**
                    #
                    # 🔴 问题：`list_tool_manifests` 只收 `ST_CONNECTED` 的 server，
                    #    于是一个被禁用的 MCP，它的工具**从目录里彻底消失** ——
                    #    模型调它会被判成 UNKNOWN_TOOL（"这个名字不存在"）。
                    #    而这正是本文件在 `_ToolFailCause` 那里明令避免的那件事：
                    #    **塞进 UNKNOWN_TOOL 是撒谎（它明明存在）。**
                    # ⚠️ MCP 比 Skill 更棘手：Skill 的 manifest 是本地文件，禁用了
                    #    照样读得到；**MCP 的工具清单要连上才知道** ——
                    #    从没连过的 disabled server，我们不可能知道它有哪些工具。
                    # ⭐ 但 MCP 的工具名**自带 server 名** ⇒ 判据下移一层：
                    #    从「这个**工具**被禁用了」下移到「它所属的**服务**被禁用了」。
                    #    📌 答不出精确的那一问时，答一个**同样有用且答得准**的问题，
                    #       比猜一个精确答案强。
                    _mcp_off = ""
                    try:
                        from core.mcp_client import MCPManager as _MM_d
                        _mcp_off = _MM_d.instance().disabled_tool_server(name)
                    except Exception:
                        _mcp_off = ""
                    if _mcp_off:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = (f"The MCP server \"{_mcp_off}\" is currently disabled, "
                                 f"so \"{name}\" was not run.")
                    elif name in _disabled:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = f"Skill \"{name}\" exists but is currently disabled, so it was not run."
                    else:
                        _cause = self._ToolFailCause.UNKNOWN_TOOL
                        _head = f"Tool \"{name}\" does not exist. The name is wrong; this is not a transient failure."
                    result_text = _head + "\n" + self._describe_tool_failure(name, _cause)
                    logger.warning(f"[ReAct] 工具调用失败 {_cause}: {name}")
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _head[:80], "status": "CORE_THINKING",
                        "model": used_model, "current_skill": name,
                        "tool_use_id": call.tool_use_id, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=result_text, ok=False,
                                         error=f"{_cause}: {name}")

                # 注：延迟的技能/MCP 即使没 load 也能按名字直接执行（registry/MCP 分发与
                # schema 注入解耦），所以这里不拦——load_tools 只为给模型正确的参数 schema。
                # ── 本地 Skill 执行 ──────────────────────────────────────
                # 副作用确认检查
                _side_check = self._check_skill_side_effects(name)
                from core.os_layer import dsl as _dsl_auto
                if _side_check and not _dsl_auto.auto_skips_confirmation(f"Skill {name}"):
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    from core.runtime import replies as _replies
                    _rid = _replies.register({"confirm": _on_confirm, "cancel": _on_cancel})
                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": _side_check,
                        "reply_id": _rid, "actions": ["confirm", "cancel"],
                    })
                    from core.runtime import inbox as _ib6
                    try:
                        _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    finally:
                        _replies.discard(_rid)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True

                    if _cancelled[0]:
                        # ⚠️ 同上：用户改口 vs 干等超时，要分开告诉模型。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (f"Skill \"{name}\" was NOT executed.\n"
                                           + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"Skill \"{name}\" execution was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end",
                            "action_id": aid,
                            "result_summary": result_text[:80],
                            "status": "SYS_IDLE",
                            "model": used_model,
                            "current_skill": name,
                            "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ⭐⭐⭐ [统一长任务 · 第三类载体] **本地 Skill 走同一条交还合同。**
                #
                # 🔴 上一版这里是 `async with sem: raw_result = await execute(...)`
                #    —— **没有超时，也没有交还**。一个跑很久的 Skill 会把整轮
                #    无限期堵住，比改造前的 `run_command`（至少 30 秒会报错）
                #    还糟：那是「错了」，这是「永远不回来」。
                # 📌 已定原则：**我们要识别的只有「长任务」，
                #    跟任务类型从来没有关系过。** 命令 / MCP / Skill
                #    是同一件事的三个载体，不是三种机制。
                #
                # ⚠️ 阈值用同一个 `_LONG_TASK_HANDBACK_SEC`（**5s**，不是 90s ——
                #    2026-08-22 那次建模 把命令 45 / 外部服务 90 / Subagent 5 统一成 5，
                #    这两处注释忘了跟着改，2026-08-25 排查时才发现）。
                # 📌 一条说 90 的注释配一个写 5 的常量，读注释的人会按 90 去推理，
                #    而按 90 推出来的每一步都是错的 —— **过时的注释比没有注释更贵**。
                #    ⭐ 而**不对称的依据不是任务类型，是「回看那一眼能不能看到
                #       东西」**：命令有 stdout（45s 就交还，因为回看有价值），
                #       Skill 只有它自己调 `report_progress` 时才有 ——
                #       所以给它更长的前台耐心。
                _sk_ref = f"long_{call.tool_use_id or aid}"
                _sk_disp = _cat.presentation(name, args)
                # ⚠️⚠️ **信号量必须手动收放**，不能用 `async with`：
                #    交还之后 Skill **还在跑**，而这个函数要 return ——
                #    `async with` 会在 return 时释放，那没问题；
                #    问题是**不释放**才是错的。前台并发位是「前台等待」的资源，
                #    交还之后这件事已经归后台合同管了。
                #    📌 **一个操作的占位，在控制权被交回模型之后，应该由
                #       「后台合同」决定，而不再由「前台等待的耐心」决定。**
                #       （与 `_HANDED_BACK_DEADLINE_SEC` 同一条判据。）
                await self._tool_parallel_sem.acquire()
                _sem_held = True
                try:
                    _sk_task = asyncio.create_task(
                        self.registry.execute(name, args, progress_ref=_sk_ref))
                    # ⭐ 同 MCP 那条：用户一开口就立刻交还，不等满阈值。
                    _sk_finished = await self._wait_or_user_speaks(
                        _sk_task, self._LONG_TASK_HANDBACK_SEC)
                    if not _sk_finished:
                        # 超阈值 → 交还控制权，Skill 继续跑
                        self._tool_parallel_sem.release()
                        _sem_held = False
                        result_text = await self._hand_back_long_task(
                            display=_sk_disp, bg_ref=_sk_ref,
                            action_id=aid, event_queue=event_queue)

                        async def _await_skill(_t=_sk_task, _n=name, _r=_sk_ref):
                            try:
                                _raw = await _t
                            except Exception as _e:
                                return (f"Skill \"{_n}\" failed after it was handed "
                                        f"back: {type(_e).__name__}: {_e}")
                            finally:
                                try:
                                    from core.runtime import progress as _pb4
                                    _pb4.forget(_r)
                                except Exception:
                                    pass
                            _t2 = str(_raw) if _raw is not None else \
                                f"Skill \"{_n}\" ran but returned no content."
                            return f"Skill \"{_n}\" finished:\n{_t2}"

                        await event_queue.put({
                            "event": "long_task_handback",
                            "task": asyncio.ensure_future(_await_skill()),
                            "bg_task_ref": _sk_ref,
                            "display": _sk_disp,
                            "skill_name": name,
                        })
                        # ⚠️ 刻意**不**收这条 ActionAttempt —— 那次尝试还没结束。
                        #    📌 不许为一件还没结束的事记一个结论。
                        return ToolExecution(call=call, result_text=result_text, ok=True)
                    raw_result = _sk_task.result()
                finally:
                    if _sem_held:
                        self._tool_parallel_sem.release()
                try:
                    from core.runtime import progress as _pb5
                    _pb5.forget(_sk_ref)
                except Exception:
                    pass
                # 走到这里名字一定存在，所以 None 的唯一含义是"跑了但没返回内容"——
                # 这句话现在是真的了。
                result_text = str(raw_result) if raw_result is not None else \
                    f"Tool \"{name}\" ran but returned no content."
                tool_data = getattr(raw_result, "data", None)

                # 记录执行失败状态（供后续修复路由使用）
                _is_failed = False
                if raw_result is None:
                    _is_failed = True  # None 返回明确标为失败
                elif getattr(raw_result, "success", True) is False:
                    _is_failed = True
                if not _is_failed and isinstance(result_text, str) and any(
                    result_text.startswith(m) or m in result_text
                    for m in ("错误：", "【错误】", "【执行失败】", "执行故障", "执行失败")
                ):
                    _is_failed = True

                if _is_failed:
                    self._last_skill_error = {
                        "skill": name,
                        "error": f"Tool \"{name}\" failed: {result_text}",
                        "consumed": False,
                    }
                    # 名字对、跑了、失败了 —— 区分"参数不合 schema"和"执行本身失败"。
                    # `registry.execute` 的参数校验失败会返回以 "Execution failed:" 开头的串
                    # （`registry.py` 里那三处），那三条是同一类：工具在，参数不对。
                    _param_fail = isinstance(result_text, str) and \
                        result_text.startswith("Execution failed:")
                    result_text = result_text + self._describe_tool_failure(
                        name,
                        self._ToolFailCause.BAD_PARAMS if _param_fail
                        else self._ToolFailCause.TOOL_ERROR,
                        tool_error=result_text if _param_fail else "",
                    )

                # OS dsl_plan 检测：Skill 返回了 OS 执行计划，需要转入 OS 执行循环
                _os_plan = (tool_data or {}).get("dsl_plan") if isinstance(tool_data, dict) else None
                if isinstance(_os_plan, list):
                    # 注入特殊标记，ReAct 主循环检测到后转路由
                    result_text = f"__OS_DSL_PLAN__{name}"
                    tool_data = {"dsl_plan": _os_plan, "_skill_name": name, "_skill_args": args}

                self._session_log_append(f"[Skill call] {name}")
                self._wm_add(
                    EntryType.SKILL_CALL, name, "call",
                    detail="", tags=["skill", "call", name],
                )
                self._last_called_skill = name

            ok = not _is_failed
            error = (f"Tool \"{name}\" failed: {result_text[:100]}" if _is_failed else "")

        except Exception as e:
            ok = False
            error = str(e).replace(chr(10), " ")
            result_text = f"Tool \"{name}\" execution error: {error}"
            raw_result = None
            tool_data = None
            self._last_skill_error = {
                "skill": name,
                "error": result_text,
                "consumed": False,
            }
            logger.error(f"[ReAct] 工具【{name}】执行异常: {error}")

        await event_queue.put({
            "event": "tool_end",
            "action_id": aid,
            "result_summary": result_text[:80],
            "status": "CORE_THINKING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
            "ok": ok,
        })

        # ⭐⭐ 挂起期证据日志 —— 挂在**工具结果前面**交给模型。
        #
        # ⚠️ 位置刻意在 `tool_end` 事件**之后**：那个事件的 `result_summary` 取
        #    `result_text[:80]`，放前面的话卡片摘要会变成日志开头而不是工具结果。
        #    📌 **给模型看的内容和给用户看的摘要，不该是同一个字符串。**
        #
        # ⚠️ 取完即清：它是一次性的，下一个工具不该再看到同一段。
        _pn = getattr(self, "_a3_pause_note", "")
        if _pn:
            self._a3_pause_note = ""
            result_text = f"{_pn}\n\n---\n{result_text}"
        # 用户在 GUI 任务中途放大了窗口：一次性说明附在工具结果后面。
        _wn = self._take_window_note()
        if _wn:
            result_text = f"{result_text}\n\n{_wn}"

        return ToolExecution(
            call=call,
            result_text=result_text,
            ok=ok,
            raw_result=raw_result,
            tool_data=tool_data,
            error=error,
        )

    async def _execute_tool_batch(
        self,
        calls: list[ToolCall],
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> list[ToolExecution]:
        """执行一批工具调用（Claude 单轮决策返回的所有 tool_use）。

        策略：
        - 按 Claude 原始顺序遍历，遇到连续的 parallel 工具段则并发执行，
          遇到 serial 工具时先 flush 前面的 parallel 段再串行执行。
          这样保证执行语义顺序与 Claude 意图一致。
        - 超出 MAX_TOOLS_PER_ROUND 的工具不执行，但必须生成 error tool_result，
          确保 tool_use / tool_result 数量完全对应（Anthropic API 要求）。
        - 结果按原始顺序排列。
        """
        original_calls = list(calls)
        # 每轮上限只数非 computer_use 的调用：一批屏幕动作连发多少步由模型按「哪里需要
        # 中途核对真实状态」判断，不设数字上限（失败即停见下面的串行段）。
        executable_calls, skipped_calls, _n_counted = [], [], 0
        for _c in original_calls:
            if _c.name == "computer_use":
                executable_calls.append(_c)
            elif _n_counted < self.MAX_TOOLS_PER_ROUND:
                executable_calls.append(_c)
                _n_counted += 1
            else:
                skipped_calls.append(_c)

        results_by_key: dict[str, ToolExecution] = {}

        def _key(c: ToolCall) -> str:
            return c.tool_use_id or f"tool_{c.name}_{c.index}"

        # 按原始顺序、分 parallel 段并发执行
        parallel_segment: list[ToolCall] = []

        async def flush_parallel():
            nonlocal parallel_segment
            if not parallel_segment:
                return
            tasks = [
                asyncio.create_task(self._execute_one_tool_call(
                    c, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                ))
                for c in parallel_segment
            ]
            for coro in asyncio.as_completed(tasks):
                ex = await coro
                results_by_key[_key(ex.call)] = ex
            parallel_segment = []

        # 并发资格问目录，不再问 `_classify_tool_safety` 那个三值返回。
        #
        # 🔴 那个函数把**两个正交维度压成了一个返回值**：
        #      if EXIT:  return "exit"     ← 先命中就 return
        #      if SERIAL: return "serial"  ← 于是这一行永远走不到
        #    结果是 5 个工具（create_new_skill / update_existing_skill /
        #    manage_existing_skill / WriteSkill / answer_open_interaction）
        #    **同时**登记在 SERIAL 和 EXIT 两张表里，而它们声明的 serial
        #    **永远不生效、且不报错**。
        # 📌 那不是"将来会漏"，是**当时就有一半声明是死的** —— 的直接动机之一。
        #
        # ⭐ 现在 `flow` 答「调用之后去哪」、`scheduling` 答「能不能和别人同批」，
        #    两个维度各自独立。exit 工具的真实调度语义是 **EXCLUSIVE**
        #    （与任何其他工具同轮 → 整批一个都不执行、全部写 error、下轮重选），
        #    **不是** SERIAL —— 写 SERIAL 就是又造一个新的死声明。
        # ⚠️ 走到这里的一定不是 exit 工具（`_run_react_loop` 在更早就摘走了），
        #    所以这里只需要分「能不能并发」：PARALLEL 进并发段，其余串行。
        #    找不到声明的（幻觉名字）落串行 —— 与改造前的兜底同向。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        # 同一批里的屏幕动作按顺序执行；任一步失败，本批剩下的 computer_use 不再执行
        # （后面的步骤是按失败那一步成功的前提排的，例如点输入框失败后不能接着打字）。
        _screen_step_failed = False
        for call in executable_calls:
            _d = _cat.get(call.name)
            if call.name == "computer_use" and _screen_step_failed:
                results_by_key[_key(call)] = ToolExecution(
                    call=call,
                    result_text=("NOT executed: an earlier computer_use step in this batch failed, "
                                 "so the remaining screen steps were skipped. Re-plan from the "
                                 "current screen."),
                    ok=False, error="skipped after an earlier screen step failed")
                continue
            if _d is not None and _d.scheduling is Scheduling.PARALLEL:
                parallel_segment.append(call)
            else:
                await flush_parallel()
                ex = await self._execute_one_tool_call(
                    call, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                )
                results_by_key[_key(ex.call)] = ex
                if call.name == "computer_use" and not ex.ok:
                    _screen_step_failed = True

        await flush_parallel()

        # 超限工具必须生成 error tool_result，否则 tool_use/tool_result 不对称
        for c in skipped_calls:
            results_by_key[_key(c)] = ToolExecution(
                call=c,
                result_text=(
                    f"Tool \"{c.name}\" was not executed: the model requested {len(original_calls)} tools this round, "
                    f"which exceeds the per-round system limit of {self.MAX_TOOLS_PER_ROUND}. "
                    "In the next round, decide whether to continue calling tools based on the returned results."
                ),
                ok=False,
                error="tool limit exceeded",
            )

        # 按 Claude 原始顺序排列
        ordered = []
        for call in original_calls:
            k = _key(call)
            ex = results_by_key.get(k) or ToolExecution(
                call=call, result_text="Tool execution result was missing.", ok=False, error="result missing"
            )
            ordered.append(ex)

        return ordered
