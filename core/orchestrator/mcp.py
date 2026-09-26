# core/orchestrator/mcp.py
"""Orchestrator 的 MCP 部分：搜索官方注册表、连接与管理外部服务。"""

import asyncio
import time

from loguru import logger

from core.orchestrator._runtime import _rt_open_mcp_manage


class McpMixin:
    """MCP：搜索官方注册表、连接与管理外部服务（含用户确认）。"""

    # ── MCP 接入辅助 ──────────────────────────────────────────────────────
    @staticmethod
    def _is_mcp_tool(name: str) -> bool:
        """MCP 工具名形如 mcp__<server>__<tool>（廉价前缀判断，不需加载 manager）。"""
        return isinstance(name, str) and name.startswith("mcp__")

    @property
    def _mcp_manager(self):
        """懒加载 MCP 管理器单例。MCP 工具属于'Nano 自身能力'，内联分发不进 registry。"""
        mgr = getattr(self, "_mcp_mgr_cache", None)
        if mgr is None:
            from core.mcp_client import get_mcp_manager
            mgr = get_mcp_manager()
            self._mcp_mgr_cache = mgr
        return mgr

    async def _exit_connect_mcp(self, exit_call, decision, *, used_model,
                                base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_connect_mcp_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_manage_mcp(self, exit_call, decision, *, used_model,
                               base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_manage_mcp_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _handle_search_mcp_registry(self, args: dict, aid: str, *,
                                           event_queue=None, used_model: str = "",
                                           **_ctx) -> str:
        """查 Official MCP Registry。**只读，不改任何东西。**

        ⚠️ 返回的是给模型的**候选清单**，不是结论 —— 判断留给模型和用户。
           早先定过：`source = OFFICIAL_MCP_REGISTRY` 不等于 `trust = SAFE`。
        ⚠️ 查不到 / 查不通 一律**如实说是哪一种**，不合并成「没有」——
           📌 「读不懂」「连不上」「确实没有」是三件事，压成一件会让模型
              向用户报告一个假的空结果。
        """
        from core.mcp_discovery import search_registry
        _ok, _txt = await search_registry(args.get("query") or "",
                                          args.get("limit") or 8)
        return _txt

    async def _handle_manage_mcp_decision(
        self,
        decision,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """`manage_mcp` 元工具 —— **形状照抄 `_handle_manage_existing_skill_decision`**。

        📌 用户对「关掉那个东西」的心智，在 Skill 和 MCP 上是同一个 ——
           两套交互只会把多出来的复杂度落在用户身上。

        ⚠️ 与 Skill 的三处**必要**差异（不是随意偏离）：
          ① 多一个 `retry` —— MCP 多一个 Skill 没有的状态：**连不上**。
             📌 「连不上」和「被禁用」对用户是两件事：前者说「再试试」，
                后者说「要不要打开」。合成一个操作等于让用户猜现在是哪种。
          ② `enable` / `disable` / `retry` **立即生效、不走二次确认** ——
             它们可逆，而 Skill 那边同样只对 delete 要确认。
             🔴 `delete` 会改配置文件、**不可逆** ⇒ 与 Skill 一致走
                自然语言二次确认（`_request_mcp_management_confirmation`），
                **不弹窗**。弹窗只在用户走 UI 按钮删除时出现。
          ③ 存在性校验查的是**配置**而不是"已连接" ——
             📌 一个被禁用、或正连不上的 server **依然存在**；
                拿"连上了没有"当"存不存在"，就会对着一个真实存在的东西说它不存在。
        """
        _args = decision.args or {}
        _op = (_args.get("operation") or "").strip().lower()
        _srv = (_args.get("server_name") or "").strip()

        # ⚠️ 同义词兜底 —— **不是给某个模型打的补丁**。
        #    「重连 / retry / restart」对人和模型都指同一件事；一个只认其中一个
        #    拼法的接口，是接口自己的问题。
        #    📌 与「不留专门给某个模型的修正动作」不冲突：那条禁的是迁就某个模型的
        #       **怪癖**，而这里任何人、任何模型都会在这几个词之间摇摆。
        #    ⚠️ 只收**真同义**的；`connect` / `start` / `stop` 故意不收 ——
        #       它们指向别的东西（connect_mcp 是新增，不是重连），猜错比不猜更坏。
        _SYNONYM = {
            "retry": "reconnect", "restart": "reconnect", "re-connect": "reconnect",
            "on": "enable", "turn_on": "enable", "activate": "enable",
            "off": "disable", "turn_off": "disable", "deactivate": "disable",
            "remove": "delete", "uninstall": "delete",
        }
        _op = _SYNONYM.get(_op, _op)
        _OPS = {"enable", "disable", "delete", "reconnect"}

        # ⭐⭐ **这个 handler 一句中文都不自己说。**
        #
        # 全部出口走 `exit_flow_defer_to_model`：只把**英文事实**交回主 ReAct，
        # 由模型用它自己的话（用户当前的语言 + 当前的人格）说出来。
        #
        # 🔴 第一版另造了个 `_bail`，每个出口写死一句中文，2026-08-28
        #    当场抓到其中一句。而这个机制 **2026-08-06 就存在了** ——
        #    照抄了 Skill 管理那套的**形状**，却没查那个形状本身对不对。
        #    📌 **照抄一个形状之前，先确认那个形状本身是对的。**
        #
        # 🔴🔴 走 defer 的路径**绝对不能自己 `add_tool_call`** ——
        #    拦截处（`if _defer_to_model:`）会无条件写一次。写两次 →
        #    tool_use / tool_result 配对损坏 → 下一轮直接 400。
        #    ⇒ 所以原来那句「始终先写 tool_call」**整条删掉**，不是挪位置。
        #    （既有两处正确用法所在的函数里，`add_tool_call` 出现 **0 次** —— 已核。）
        def _defer(facts: str, log: str, ok: bool = False):
            # ok=True：这件事已经做成，只是措辞交回模型（工具行显示 ✓）
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log,
                    "ok": ok}

        if _op not in _OPS:
            yield _defer(
                f"manage_mcp did NOT run: operation {_op!r} is not one of the four valid "
                f"operations (enable, disable, delete, reconnect). Nothing was changed. "
                f"If you can tell which one the user meant, call manage_mcp again with that "
                f"exact value; otherwise ask the user which one they want.",
                "manage_mcp operation 非法，措辞交回模型。")
            return

        try:
            from core.mcp_client import MCPManager as _MM
            _mgr = _MM.instance()
        except Exception as e:
            yield _defer(
                f"manage_mcp could not run: the MCP subsystem is unavailable ({e}). "
                f"Nothing was changed. Tell the user this did not go through; do not retry.",
                "manage_mcp: MCPManager 不可用，措辞交回模型。")
            return

        # ── 存在性校验：查【配置】，不查"连上了没有" ──────────────────
        # ⚠️ `owned_by` 非空的**不算在内** —— 它不是用户的外接能力，是某个 Skill
        #    的内部零件。列进来的话，模型会拿它当一个可管理的候选去猜。
        _all = getattr(_mgr, "servers", {}) or {}
        _known = [n for n, _s in _all.items()
                  if not str(getattr(_s, "owned_by", "") or "").strip()]

        # 🔴 撞到零件：四个动作全拒。
        #    ⚠️ 但**不能说"没有这个 server"** —— 那是撒谎，它确实存在，
        #       而且模型下一步会去"创建"或"重新接入"一个已经在的东西。
        #    📌 拒绝要给真实理由，否则模型只会换个姿势再试一次。
        _target = _all.get(_srv)
        _owner = str(getattr(_target, "owned_by", "") or "").strip() if _target else ""
        if _owner:
            yield _defer(
                f"manage_mcp will not act on {_srv!r}: it is not a user-managed MCP server, "
                f"it is an internal component of the Skill {_owner!r}. It starts on demand and "
                f"stops on its own; showing as 'not connected' is normal, not a failure. "
                f"Nothing was changed and nothing needs to be. If the user is having trouble "
                f"with what {_owner} does, talk about that Skill - not about this component.",
                f"manage_mcp 拒绝对内部零件「{_srv}」动手（属于 {_owner}）。")
            return

        if not _srv or _srv not in _known:
            # ⭐ 把**配置里真实存在的名字**一起交回去 —— 模型据此能自己改名重试，
            #    不必回头问用户「你说的是哪个」。
            yield _defer(
                f"manage_mcp did NOT run: there is no MCP server named {_srv!r}. "
                f"The servers that actually exist are: {_known}. Nothing was changed. "
                f"If one of those is clearly what the user meant, call manage_mcp again with "
                f"that exact name; otherwise ask the user which one.",
                "manage_mcp 目标不存在，措辞交回模型。")
            return

        # ── delete：不可逆 ⇒ 自然语言二次确认（与 Skill 一致，不弹窗）──
        if _op == "delete":
            # ⚠️ 返回值（那句固定中文）**故意不再使用** —— 只留它的副作用：
            #    把待确认登记成 Interaction（PERSISTED，跨重启存活 + 模型看得见）。
            #    ⚠️ 待办条目自身的展示文本目前仍是固定中文，属多语言化（i18n）的范围，本次不动。
            self._request_mcp_management_confirmation(_op, _srv)
            yield _defer(
                f"Deleting MCP server {_srv!r} is NOT done yet - it needs the user's explicit "
                f"confirmation first, because it edits the local configuration and cannot be "
                f"undone. A pending confirmation has been registered. Ask the user to confirm "
                f"or cancel, in your own words. Do not call manage_mcp again for this.",
                f"等待用户确认 delete MCP「{_srv}」，措辞交回模型。")
            return

        # ── enable / disable / reconnect：可逆，立即执行 ────────────────
        try:
            if _op == "enable":
                await _mgr.set_enabled(_srv, True)
                _facts = (f"MCP server {_srv!r} is now enabled and is connecting. "
                          f"Its tools become usable once the connection is up.")
            elif _op == "disable":
                await _mgr.set_enabled(_srv, False)
                _facts = (f"MCP server {_srv!r} is now disabled. Its tools are unavailable "
                          f"until it is enabled again.")
            else:
                # ⚠️ 内部 API 仍叫 `retry_server` —— 对模型暴露的动词是 `reconnect`。
                #    📌 内部命名和对外契约不必一致；**对外那个要贴用户的说法**。
                await _mgr.retry_server(_srv)
                _facts = f"A reconnect was triggered for MCP server {_srv!r}."
        except Exception as e:
            logger.warning(f"[B3] manage_mcp {_op} {_srv} 失败: {e}")
            yield _defer(
                f"manage_mcp {_op} on {_srv!r} failed: {type(e).__name__}: {e}. "
                f"Nothing was changed. Tell the user what failed, in your own words.",
                f"manage_mcp {_op} 失败，措辞交回模型。")
            return

        # ⭐ 感知：Nano 自己动的手，也要落进本次会话的事实账
        #    📌 与用户手点 UI 那条**同一个出口** —— 见 `_note_mcp_change`。
        self._note_mcp_change(_op, _srv, by="nano")
        yield _defer(
            _facts + " This already succeeded - do not call manage_mcp again for it. "
                     "Just tell the user, in your own words.",
            f"manage_mcp {_op}「{_srv}」完成，措辞交回模型。", ok=True)

    async def _handle_connect_mcp_decision(
        self, decision, used_model: str, base_guide: str, realtime_callback,
    ):
        """`connect_mcp` —— 接入一个新 MCP，**必须先经用户批准**。

        ⚠️⚠️ **这个弹窗无视 auto 模式**。理由不是
           「MCP 特殊」或「频率低」——那种理由是**例外**，而例外会被下一个人
           问「那为什么别的不例外」。真正的理由是**它不在 auto 管辖的维度上**：

               能力开关 答「这项能力开不开放」
               auto      答「开放了的，要不要逐个授权」
               这个弹窗 答「**这个第三方是什么东西**」   ← 第三个维度

           📌 **auto 从来没有承诺「不给你看东西」，它承诺的是「不用你逐个点同意」。**
           而接入 MCP 时缺的不是授权，是**信息**：用户说「帮我接入 X」时，
           动作与意图完全对齐（命令分类器会放行），但 **X 是什么，用户不知道**。
        🔴 而且早先的设计写死了一条不能豁免的：对 stdio，「连上」本身就是在本机
           执行第三方代码 ⇒ 这个弹窗是**执行第三方代码前的最后一道**，
           auto 豁免它 = 那道就不存在了。

        ⭐ 流程严格是：**只解析 → 弹窗 → 用户批准 → 才写配置、才启动**。
           `preview_server_json` 一行第三方代码都不执行，一个字都不写盘。
        """
        _args = decision.args or {}
        _cfg_txt = (_args.get("config_json") or "").strip()
        _purpose = (_args.get("purpose_line") or "").strip()
        _what = (_args.get("what_it_does") or "").strip()

        # ⚠️ **不写 `add_tool_call`** —— 本 handler 所有出口都走
        #    `exit_flow_defer_to_model`，拦截处会无条件写一次。写两次 →
        #    tool_use / tool_result 配对损坏 → 下一轮 400。（同 manage_mcp 那条）
        def _defer(facts: str, log: str, ok: bool = False):
            # ok=True：这件事已经做成，只是措辞交回模型（工具行显示 ✓）
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log,
                    "ok": ok}

        try:
            from core.mcp_client import MCPManager as _MM
            _mgr = _MM.instance()
        except Exception as e:
            yield _defer(
                f"connect_mcp could not run: the MCP subsystem is unavailable ({e}). "
                f"Nothing was added. Tell the user this did not go through.",
                "connect_mcp: 管理器不可用，措辞交回模型。")
            return

        _ok, _info = _mgr.preview_server_json(_cfg_txt)
        if not _ok:
            yield _defer(
                f"connect_mcp did NOT run: that MCP configuration could not be parsed - {_info}. "
                f"Nothing was added and the user was not asked anything. "
                f"Explain the problem to the user, or ask them for a corrected config.",
                "connect_mcp: 配置无效，措辞交回模型。")
            return

        # ⭐ 弹窗事件。**刻意用一个新事件名**而不是复用 os_action_confirm /
        #    execution_confirm —— 那两个在 Auto 下由后端自动放行
        #    （`os_skill` 的危险判定 / `dsl.auto_skips_confirmation`），
        #    而这一类**不许被 auto 豁免**。
        #    📌 复用一个「会被豁免」的通道去表达「不许豁免」，是自相矛盾的接线。
        # ⚠️ 等待走**既有合同** `inbox.wait_confirm_or_user_message(event, timeout)` ——
        #    📌 它同时处理三种结局：用户点了、用户改口说别的、干等超时。
        from core.runtime import inbox as _ib
        _confirm_ev = asyncio.Event()
        _approved = {"v": False}

        def _on_ok():
            _approved["v"] = True
            _confirm_ev.set()

        def _on_no():
            _approved["v"] = False
            _confirm_ev.set()

        from core.runtime import replies as _replies
        _rid = _replies.register({"confirm": _on_ok, "cancel": _on_no})
        try:
            yield {"event": "mcp_connect_confirm",
                   "info": _info, "purpose_line": _purpose, "what_it_does": _what,
                   "reply_id": _rid, "actions": ["confirm", "cancel"]}

            _oc = await _ib.wait_confirm_or_user_message(_confirm_ev, 300)
        finally:
            _replies.discard(_rid)
        if _oc != _ib.ConfirmOutcome.CONFIRMED or not _approved["v"]:
            # 🔴 三种"没接入"**分开交给模型** —— 📌 对模型下一步的含义完全不同：
            #    用户明确拒绝 → 别再提；改口说别的 → 先答那件事；超时 → 可以再问一次。
            # ⚠️ 上一版这三种区别只进了 `log`（用户和模型都看不到），
            #    模型收到的 tool_result 是 `MCP not installed: ConfirmOutcome.TIMEOUT`
            #    这样的 enum repr。📌 **注释里写着的设计，不等于落地了的设计** ——
            #    这次是把那条注释真正接上。
            _nm = _info.get("name", "")
            _facts = {
                _ib.ConfirmOutcome.USER_MESSAGE: (
                    f"MCP server {_nm!r} was NOT added: the user said something else while the "
                    f"approval dialog was open, so it was dismissed. Nothing was installed. "
                    f"Answer what they just said first; only come back to this if they ask."),
                _ib.ConfirmOutcome.TIMEOUT: (
                    f"MCP server {_nm!r} was NOT added: the approval dialog timed out with no "
                    f"answer. Nothing was installed. You may offer once more, briefly."),
            }.get(_oc, (
                f"MCP server {_nm!r} was NOT added: the user declined it. Nothing was installed. "
                f"Accept that and move on - do not offer it again unless they bring it up."))
            yield _defer(_facts, f"connect_mcp 未接入（{_oc}），措辞交回模型。")
            return

        # ── 批准之后才动真格：写配置 → 连接 → 取工具清单 → 生成说明 ──
        _ok2, _name = _mgr.add_server_from_json(_cfg_txt)
        if not _ok2:
            yield _defer(
                f"connect_mcp failed while writing the configuration: {_name}. "
                f"The user had already approved, but nothing was installed. "
                f"Tell them what failed.",
                "connect_mcp: 写配置失败，措辞交回模型。")
            return
        _srv = _info["name"]
        try:
            await _mgr.retry_server(_srv)
        except Exception as e:
            logger.warning(f"[B3] 接入后连接 {_srv} 失败: {e}")

        self._note_mcp_change("add", _srv, by="nano")
        _tools = []
        try:
            _s = _mgr.servers.get(_srv)
            _tools = [t.get("name", "") for t in (getattr(_s, "tools", []) or [])]
        except Exception:
            pass

        # ⭐ tooltip：**优先问 server 自己**，模型写的那句只是兜底。
        #    📌 早先的设计：「这个机制其实能同时覆盖 ①②③」——
        #       让「模型凭任务上下文写简述」退化成拿不到工具清单时的兜底。
        _desc = await self._mcp_tooltip(_srv, _tools, fallback=_what)
        if _desc:
            _mgr.set_description(_srv, _desc)

        # ⭐⭐ **回核** —— 拿真实能力对照授权时说的话。
        #
        # 早先定的链条是三段，前半段只做了前两段：
        #   授权前只读元数据 ✅（preview_server_json）→ 授权后启动取 tools/list ✅
        #   → **拿真实能力回核授权前的描述** ← 🔴 这一段一直缺
        # 缺的后果很具体：用户批准的是 A，装进来的可能是 B，**没有任何东西会发现**。
        #
        # ⭐ 而它几乎不要额外成本：对照用的两样**都已经在手上** ——
        #   `_what`   授权时模型写的那句（弹窗上给用户看过）
        #   `_tools`  tools/list 回来的真实工具名
        #   📌 **回核不需要新机制，只需要把「说过的」和「拿到的」放进同一句话** ——
        #      它们原本分别躺在两个变量里，谁也不认识谁。
        # ⚠️ 判断交给模型而不是写规则：「宣称的能力」和「工具名列表」之间是**语义**
        #    关系。说「读取网页」的 server 提供 fetch_url / get_page 都算相符，
        #    提供 send_email 就不相符 —— 这条线没有算法边界。
        #    而这一轮本来就要 defer 回模型说话 ⇒ **判断塞在同一轮里，零额外调用**。
        #
        # ⚠️ 「装上了」和「连上了」仍然分开说 ——
        #    📌 说成一件事的话，用户会以为能用了，然后发现工具还是不在。
        _claimed = (_what or '').strip()
        _recheck = ''
        if _tools and _claimed:
            _recheck = (
                " Before approving, you told the user this server would: "
                + repr(_claimed)
                + ". What it actually exposes is: " + ", ".join(_tools[:30])
                + ". Check those against each other. If they broadly match, just say "
                "what it can do. If they do NOT match - it does something else, or "
                "far more than described - say so plainly to the user and tell them "
                "they can remove it with manage_mcp. Do not quietly move on."
            )
        elif _tools and not _claimed:
            # ⚠️ 没有宣称过 ⇒ **没得对照**，说清是「没得核」而不是「核过了」。
            #    📌 「没核」和「核过没问题」压成一件事，就等于把一次没做的检查
            #       报告成通过了。
            _recheck = (" There was no description given when this was approved, "
                        "so there is nothing to check it against. Tell the user what "
                        "it actually exposes.")
        yield _defer(
            f"MCP server {_srv!r} was added and approved by the user. "
            + (f"It connected and exposes {len(_tools)} tools: "
               + ", ".join(_tools[:30]) + ". "
               if _tools else
               "It is installed but has not connected yet, so its tools are not "
               "available yet and there is nothing to verify against what was "
               "promised; a reconnect can be tried later. ")
            + _recheck
            + " This already succeeded - do not call connect_mcp again for it. "
              "Tell the user, in your own words.",
            f"connect_mcp 已接入「{_srv}」，措辞交回模型。", ok=True)

    async def _mcp_tooltip(self, server: str, tool_names: list, *, fallback: str = "") -> str:
        """一句话说明这个 MCP 是干什么的。**先问 server 自己，模型写的只是兜底。**

        ⚠️ 生态标准的 `mcpServers` snippet 里**没有 description 字段** ——
           command/args/env/url 全都不含人话描述。而 `tools/list` 回来的
           每个工具都带 name + description，**数据本来就在手上**。
        ⭐ 所以顺序是：拿真实工具清单 → 让便宜模型压成一句 → 存下来。
           拿不到清单（连不上 / 描述为空）才退回模型凭任务上下文写的那句。
           📌 早先的设计：这个机制**同时覆盖**官方内置、Nano 自主接入、用户手动粘贴三种来源。

        ⚠️ 语言跟随用户界面语言（`language_clause`）—— 与 digest 同一条路。
           📌 而它一旦生成就**存下来不再变**：切换语言后旧内容保持原样，
              这是 已经定过的规矩，不为它单开机制。
        """
        if not tool_names:
            return (fallback or "").strip()[:200]
        try:
            from core.models import distiller_for
            from core.i18n import language_clause
            _mdl = distiller_for(getattr(self, "target_model", "") or "") or None
            _sys = ("You write a one-line description of what an MCP server does, "
                    "based on the names of the tools it exposes. "
                    "One sentence, no more than 25 words, no marketing words. "
                    + language_clause("the description"))
            _usr = f"MCP server: {server}\nIts tools: " + ", ".join(tool_names[:30])
            _txt, _ = await self.provider.chat_without_tools(
                [{"role": "user", "content": _usr}], _sys,
                model_override=_mdl, max_tokens=120)
            _txt = " ".join(str(_txt or "").split())
            return _txt[:200] or (fallback or "").strip()[:200]
        except Exception as e:
            logger.warning(f"[B3] tooltip 生成失败，退回模型写的那句: {e}")
            return (fallback or "").strip()[:200]

    def _request_mcp_management_confirmation(self, op: str, server: str) -> str:
        """MCP 删除的自然语言二次确认 —— 形状照抄 Skill 那条。

        ⚠️ 同样登记成 Interaction：**跨重启存活 + 模型看得见**。
           📌 只挂 `_pending_action` 的话，重启后那个待确认会蒸发，
              而用户以为自己还欠一句「确认」。
        ⚠️ 文案里**不预先解释这个 MCP 是干什么的** —— 2026-08-28：
           用户主动要删，说明他知道那是什么；真不确定时会问，而那时 Nano 答得出来。
           📌 **数据要有，不代表要预先展示**；替用户预设一个他不会有的困惑，
              只会让每一次确认都变长。
        """
        self._pending_action = {"op": op, "mcp": server}
        self._pending_action_at = time.time()
        _name = {"delete": "删除"}.get(op, op)
        _msg = (f"将要{_name} MCP 服务「{server}」。"
                f"这会改动本地配置且不可撤销，请回复确认继续，或回复取消。")
        try:
            _rt_open_mcp_manage(self, op, server, _msg)
        except Exception as e:
            logger.warning(f"[B3] MCP 管理待办登记失败（不影响本次确认）: {e}")
        return _msg

    def _note_mcp_change(self, op: str, server: str, *, by: str) -> None:
        """MCP 被增删改这件事，**两条来路共用的唯一出口**。

        🔴 `by` 有两个取值，而它们**不能合并**：
             "nano"  Nano 自己调 `manage_mcp` 动的
             "user"  用户在设置里手点的
           📌 对模型来说这是两件事：前者是它自己做的（它知道），
              后者是**环境变了**（它必须被告知，否则下一轮还当那个工具在）。
              ——同 OS 授权那条判据：「用户亲自点了同意」和「auto 替用户点了」
                对模型是两件事，复用同一个回调这个区别就消失了。

        ⚠️ 写进 session log 而不是只发个事件：📌 事件是**当轮**的，
           而「这个 MCP 已经没了」是**后续每一轮**都要知道的事实。
        """
        _verb = {"enable": "启用", "disable": "停用", "delete": "删除",
                 "retry": "重连", "add": "接入"}.get(op, op)
        _who = "Nano" if by == "nano" else "用户"
        try:
            self._session_log_append(f"{_who}{_verb}了 MCP 服务「{server}」")
        except Exception as e:
            logger.warning(f"[B3] MCP 变更未能记入 session log: {e}")
