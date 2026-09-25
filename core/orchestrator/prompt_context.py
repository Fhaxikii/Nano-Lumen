# core/orchestrator/prompt_context.py
"""系统提示与每轮注入块：环境、工具感知、记忆、交互、ambient、会话日志等。（`Orchestrator` 的 mixin）"""

import os
import time

from loguru import logger

from core.orchestrator._runtime import _rt_live_interactions
from core.proactive import referent as _referent
from core.provider import CACHE_BREAK_MARKER
from core.tools import ToolOrigin, ToolScope


def _ambient_cat(app: str) -> str:
    """→ `referent.cat`。**搬家留的转发**，判据只有那一处。"""
    return _referent.cat(app)


def _ambient_parse_title(app: str, title: str):
    """→ `referent.parse_title`。**搬家留的转发**。

    ⚠️ 它被搬进 `core/proactive/referent.py` 是因为那里的句柄解析要用它，
       而 `proactive → orchestrator` 会成环。完整推导（含 `office` 为什么
       必须和 `editor` 走同一条）在那边的函数 docstring 里。
    📌 **删/搬一段代码时，长在它身上的「为什么」要跟着搬到新家** ——
       注释掉队比代码掉队更难发现。
    """
    return _referent.parse_title(app, title)


class PromptContextMixin:
    """系统提示与每轮注入块：环境、工具感知、记忆、交互、ambient、会话日志等。"""

    def _load_system_instruction(self) -> str:
        parts = []
        persona_path = os.path.join(os.path.dirname(self.instruction_path), "persona.txt")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if os.path.exists(self.instruction_path):
            with open(self.instruction_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if parts:
            return "\n\n---\n\n".join(parts)
        return (
            "You are Nano, the user's personal assistant.\n"
            "Available tool chain: {skills}.\n"
            "Stay in Nano's persona: talkative but not verbose, sharp, technically tough, a little snarky but not hurtful, and never like customer support.\n"
            "You dislike vague requests and rework, so if something is unclear, point it out directly and give an executable way forward.\n"
            "Do not invent facts, results, or abilities. If uncertain, say what is unclear and how to verify it.\n"
            "Do not use customer-service filler such as 'Received' or 'Need anything else?'."
        )

    def _build_tool_awareness_block(self) -> str:
        """构建当前可用能力边界。Token 压缩版：不重复列出所有内置工具长描述。

        ⑩ 两处改动，其余逐字不变：
          · 常驻集不再抄 `_CORE_TOOL_NAMES`，改问目录（`preload=CORE` 派生）。
            ⭐ 顺带**消灭了那个 `- {"answer_open_interaction"}` 的手工减法** ——
               它要减掉的是「名义常驻但其实是条件注入」的那一个，而现在
               `availability` 直接表达了这件事：没有未决交互它压根不在 eligible 里，
               不需要有人记得在这里减一次。
               📌 **一个需要在下游手工修正的名单，说明它上游描述得不对。**
          · Skill 那两行的描述从 `registry.get_skill_awareness_list()` 的
            **`[:50]` 字符级截断**换成目录里的 `awareness`（按词/句边界压缩）。
            🔴 那个 `[:50]` 是 `[:28]` 截断问题的**另一半** —— 只修 `[:28]` 不修它，
               不算死透。`get_skill_awareness_list` 随本次改动一起删除。
        ⚠️ 「官方 / 用户」这个维度**仍然问 registry**，没有塞进 ToolDefinition：
           `ToolOrigin` 回答的是「身份权威在哪」（BUILTIN/SKILL/MCP），
           再让它兼答「是不是官方预置」就是**一个字段表达两个现实** ——
           正是这一整轮在反对的东西。
        """
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        _skills = [d for d in _cat.advertised(ToolScope.MAIN, _rtv)
                   if d.origin is ToolOrigin.SKILL]
        cats = {"official": [], "user": []}
        for d in _skills:
            entry = {"name": d.name, "desc": d.awareness}
            try:
                _is_official = self.registry.is_official_skill(d.name)
            except Exception:
                _is_official = False
            cats["official" if _is_official else "user"].append(entry)
        lines = [
            "\n\n[Current Capability Boundary]",
            "Nano has always-on core tools and more deferred capabilities. "
            "If a needed capability is not in the active tool schema, call load_tools first instead of saying Nano cannot do it.",
            "Always-on core: " + " / ".join(sorted(
                m["name"] for m in _cat.core_manifests(ToolScope.MAIN, _rtv))),
        ]
        lines.append(
            "When explaining user-created Skills to the user, localize or paraphrase their descriptions into the user's current language. "
            "Do not translate Skill names, filenames, parameters, field names, or code identifiers. "
            "Official built-in Skills may keep their original fixed descriptions."
        )
        if cats["official"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["official"]]
            lines.append("Official Skills (schema loaded on demand; cannot be deleted/disabled): " + " / ".join(parts))
        if cats["user"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["user"]]
            lines.append("User Skills (schema loaded on demand): " + " / ".join(parts))
        else:
            lines.append("User Skills: none yet; new reusable Skills can be created when requested.")

        # ⭐⭐ **已配置的 MCP 服务清单** —— 实测 2026-08-28 抓到的缺口。
        #
        # 🔴 问题：用户说「有个 mcp 叫 context7」，Nano 答「这边找不到 / 目前只接入了
        #    time 这一个」—— 而 context7 明明在配置里、还连着。
        #    它说的那句「只接入了 time」不是查出来的，是它**记得自己刚接入过 time**。
        # 📌 根因：模型能看到的只有【已连接 server 的工具】（进 deferred awareness），
        #    **从来没有一份「有哪些 MCP 服务、各自什么状态」的清单**。
        #    ⇒ 于是 `manage_mcp` 直接作废：它要 `server_name`，而模型不知道有哪些名字。
        # ⚠️ 原本清单里的「MCP 感知」做的是**变更感知**（增删改记进 session log），
        #    而这里缺的是**状态感知**（现在有哪些）—— 两件事被混成了一件。
        #    📌 「发生了什么」和「现在是什么」是两份不同的账，缺哪一份都会让模型瞎猜。
        #
        # ⭐ 状态**必须写出来**，不能只列名字：场景「用户手动禁用了某个 MCP」全靠它 ——
        #    模型要能说「它被禁用了，要我打开吗」，而不是「我没有这个能力」。
        # ⚠️ 形状照抄上面 Skill 那两行（名字 + 一句话），每轮成本约几十 token：
        #    MCP 数量天然很少（个位数），而没有它模型就会**编**。
        try:
            from core.mcp_client import MCPManager as _MM_r
            _snap = _MM_r.instance().status_snapshot()
        except Exception:
            _snap = []
        if _snap:
            _ST_WORD = {"connected": "connected", "disabled": "disabled by the user",
                        "failed": "failed to connect", "needs_auth": "needs authentication",
                        "connecting": "connecting", "disconnected": "not connected"}
            # ⚠️ **只给名字 + 状态，不给描述。**
            #    📌 模型选工具靠的是 deferred awareness 里那些【工具】的描述，
            #       server 这一层的描述对「选哪个工具」毫无帮助 ——
            #       它的用途是回答用户「这个 mcp 是干嘛的」，而那是**低频问题**。
            #    ⇒ 每轮都发它，等于为一个偶尔才被问到的问题付固定成本。
            #      （实测带描述 127 token/轮，去掉后 ~40。）
            #    ⭐ 真被问到时，管理页和 `status_snapshot()` 里都有完整描述。
            # ⚠️ `owned_by` 非空的**不列** —— 它是某个 Skill 的内部零件，
            #    模型既管不了它、也从来调不到它的工具
            #    （`expose_tools=False` ⇒ 隐藏 server 的工具连 `_tool_index` 都不进，
            #      只能被那个包装 Skill 通过 `call_hidden()` 走 —— 已回代码核实）。
            #    📌 列一个它既不能用、也不能管的东西，只会让它去"修"一个没坏的东西：
            #       实测 2026-08-28 它就这么干了，还宣布"连上了"。
            _mp = [f"{_m['name']} [{_ST_WORD.get(_m.get('status', ''), _m.get('status', ''))}]"
                   for _m in _snap if not _m.get("owned_by")]
        if _snap and not _mp:
            _snap = []          # 全是零件 ⇒ 对模型而言等于一个都没配
            # 🔴🔴 **「authoritative and complete」这半句不是客套，是这条的命门。**
            #    实测第二轮：清单已经在上下文里、内容也对，模型**照样**花了 5 次工具
            #    调用 68.7 秒去翻 `mcp*.json`，最后加载了用户主目录下的
            #    `.claude.json` —— 那是 **Claude Code 的配置**，不是 Nano 的
            #    （Nano 的在 `config/mcp_servers.json`）。
            #
            # ⭐⭐ 同一个问题的第三次发作，三次的形状一模一样：
            #        ① `.cursor/mcp.json`  → 以为自己是 Cursor
            #        ② 会话记忆            → 「我只接入了 time 这一个」
            #        ③ `.claude.json`      → 以为自己是 Claude Code
            #    📌 **没有一个被声明为权威的来源，模型就会自己找一个像样的。**
            #       而「像样的来源」在一台装着别的 AI 工具的机器上遍地都是。
            #
            # ⚠️ 这不算「专门给某个模型的修正动作」：这份清单**客观上**就是权威且
            #    完整的（直接来自 MCPManager 的活状态），原来只是没把这个事实说出来。
            #    📌 补一句真话 ≠ 打一个补丁。
            # ⚠️ 多花的约 50 token/轮，换掉的是 68 秒 + 5 次工具 + 一个读错文件的答案。
            #    📌 体验 > 成本。
            lines.append(
                "MCP servers on this machine (this list is authoritative and complete "
                "- answer questions about which MCP servers exist or their status "
                "directly from it; never search the disk or read config files for this, "
                "config files belonging to other AI tools are not Nano's). "
                "manage_mcp enables / disables / deletes / retries them; "
                "connect_mcp adds a new one: " + " / ".join(_mp))
        else:
            # ⚠️ 空清单**同样要带权威性** —— 否则模型看到 "none" 的第一反应
            #    就是「不对吧，我去找找配置文件」，于是绕回上面那三次里的任意一次。
            lines.append(
                "MCP servers: none configured on this machine yet (authoritative "
                "- do not look for MCP config files on disk). connect_mcp can add one.")
        return "\n".join(lines)

    def _build_pipeline_context(self) -> list:
        return self.memory.get_full_context()

    @staticmethod
    def _insert_before_cache_break(text: str, extra: str) -> str:
        """把稳定协议插入 CACHE_BREAK_MARKER 前，确保可被 prompt cache 命中。"""
        if not extra:
            return text
        if CACHE_BREAK_MARKER in text:
            return text.replace(CACHE_BREAK_MARKER, extra + CACHE_BREAK_MARKER, 1)
        return text + extra

    # ⭐⭐ Nano 的「我在哪、我在什么机器上」—— 2026-08-28 用户提出。
    #
    # 🔴 起因是 MCP 那次绕路：模型不知道自己的程序目录，于是去翻用户主目录，
    #    读到了 `.claude.json`（Claude Code 的配置）当成自己的。
    #    📌 前后三次发作（.cursor / 会话记忆 / .claude.json）都是同一句话：
    #       **一个不知道自己在哪的程序，会把任何长得像自己配置的文件认成自己的。**
    #
    # ⚠️ 这里是**程序目录**，不是工作目录（用户特意点名）：
    #       cwd    取决于用户从哪儿启动（双击 / 快捷方式 / 命令行各不相同）
    #       项目根（`core.paths.ROOT`）由代码文件位置推出 —— **装在哪就是哪**
    #    ⇒ 「每个人存放路径不一样」恰恰不构成问题：它不依赖 cwd、注册表或启动方式。
    # 🔴 若将来打包成 exe（PyInstaller），`__file__` 会指向临时解压目录 `_MEIPASS`，
    #    **不会报错，只会静默指错**。到那天必须在 `core/paths.py` 加 `sys.frozen` 分支。
    _ENV_BLOCK: str = ""          # 一次会话内不变 ⇒ 只算一次（注册表读取不该每轮跑）

    @classmethod
    def _environment_block(cls) -> str:
        if cls._ENV_BLOCK:
            return cls._ENV_BLOCK
        import sys as _sys, platform as _pf
        try:
            from core.paths import ROOT as _ROOT
            _root = str(_ROOT)
        except Exception:
            _root = "(unknown)"
        cls._ENV_BLOCK = (
            "\n\n[Environment]\n"
            "Nano itself is running in the following environment:\n"
            f"- Nano's own program directory: {_root} "
            "(this is where Nano's own files live - its code, config and data. "
            "It is NOT the user's working folder; never assume the user's files are here.)\n"
            f"- Platform: {_sys.platform}\n"
            f"- OS Version: {cls._os_version_string()}\n"
        )
        return cls._ENV_BLOCK

    @staticmethod
    def _os_version_string() -> str:
        """真实的 OS 版本串。

        🔴 **不能用 `platform.version()` / `platform.platform()`** —— 它们走
           `GetVersionEx`，被 Windows 的兼容性垫层锁住。实测这台机器：
               platform.version()      = 10.0.19041   ❌
               sys.getwindowsversion() = build 19045   ✅
           📌 差 4 个版本，而且**不报错** —— 最顺手的那两个 API 悄悄给错值。

        🔴 **Windows 11 的注册表仍然写着 "Windows 10 Pro"**（微软没改过这个键）。
           ⚠️ 这条在 Win10 上**永远测不出来** —— 正是「在我机器上好使」
              的标准形态，所以按 build 号硬修正。
        """
        import sys as _sys, platform as _pf
        if _sys.platform != "win32":
            return _pf.platform()          # mac/linux 没有上面那两个坑
        name, build, major, minor = "", 0, 10, 0
        try:
            _wv = _sys.getwindowsversion()
            major, minor, build = _wv.major, _wv.minor, _wv.build
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as _k:
                name = str(winreg.QueryValueEx(_k, "ProductName")[0])
                if not build:
                    build = int(winreg.QueryValueEx(_k, "CurrentBuild")[0])
        except Exception:
            pass
        if not name:
            name = f"Windows {_pf.release()}"
        if build >= 22000 and "Windows 10" in name:
            name = name.replace("Windows 10", "Windows 11")
        return f"{name} {major}.{minor}.{build}" if build else name

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    def _build_audit_failure_injection(self) -> str:
        """把"上一个 Skill 没能部署，以及具体哪里不合协议"摆到模型面前。

        ═══ 为什么需要它（2026-08-05 实测）═══

        审计窗口拦下一个 Skill、用户点丢弃之后，模型手上什么都没有：
          · 代码从未进 memory（`skill_preview` 的 code 只给弹窗渲染）
          · 校验报错只进 `validation_lbl`，纯 UI
          · app 侧那条 `[System record: ...discarded...]` 被 max_turns=10 切掉了
        于是用户说"上次写的校验报错，重新写"时，它只能反问
        「哪个 Skill 出问题了？」「我需要先检查它的代码看看哪里报错」——
        而那个 Skill 压根没部署，`inspect_existing_skill` 查不到。
        **它不是笨，是真的什么都不知道。**

        ⚠️ 刻意**不写 memory**：memory 会被截断，那正是这条缺陷的一半。
        动态段每轮从实例状态重建，模型隔多少轮回来都还在。
        同 `[Open Interactions]` 的做法。

        没有失败记录时返回空串，一个字符都不加。
        """
        af = getattr(self, "_last_audit_failure", None)
        if not isinstance(af, dict) or not af.get("filename"):
            return ""
        _outcome = af.get("outcome") or "awaiting"
        _errs = af.get("errors") or []
        _fn = af.get("filename")
        _state = {
            "discarded": "The user reviewed it and discarded it. It was NOT deployed and its "
                         "code no longer exists anywhere — you cannot inspect it.",
            "awaiting": "It is still sitting in the audit window awaiting the user's decision.",
        }.get(_outcome, "")

        out = ["\n\n[Last Skill Audit — Not Deployed]",
               f"Skill: {_fn}  (mode={af.get('mode', 'create')}, "
               f"{af.get('code_lines', 0)} lines, code_hash={af.get('code_hash') or 'n/a'})",
               _state]
        if _errs:
            out.append("It failed the v3.x protocol check on these points:")
            out.extend(f"  - {e}" for e in _errs)
            out.append(
                "If the user asks you to rewrite or fix it, you already know which Skill and "
                "which problems — do not ask them, and do not try to inspect it (it was never "
                "deployed). Write a fresh version that fixes exactly the points above."
            )
        else:
            out.append(
                "It passed validation but the user discarded it anyway. Do not assume it exists. "
                "If they ask again, write it fresh; consider asking what they disliked."
            )
        return "\n".join(x for x in out if x)

    def _l3_index_block(self) -> str:
        """已经移出上下文的那些交换，留下的一行行索引。

        ⚠️ **低于一条就一个字都不注入** —— 📌 一段每轮都在、又暂时没有内容的
           标题会被模型学成背景噪音（同压力段那条纪律）。
        ⚠️⚠️ **刻意不判总开关。** `ladder_enabled` 是一个**单向迁移开关**：
           它控制"还产不产生新的衰减"，**不控制"已经产生的算不算数"**。
           关掉它之后，已经降到 L3 的那些交换仍然：hydrate 时被移出、
           UI 仍隐藏、索引仍注入 —— 三处**必须一致**，所以读侧一律不判开关。
           📌 已经发生的降级是**既成事实**，一个 bool 撤不回来 ——
              真要恢复旧行为，清 `exchange_decay` 表（见 `model_config.json`
              的 `_ladder` 说明）。
        ⚠️ 与 `recall_conversation` 的 availability 判据（`has_evicted_history`）
           **共用同一个事实**：有索引 ⇔ 有工具。
        """
        try:
            from core.context.decay import l3_index_lines
            from core.context.decay_store import DecayStore
            from core.runtime.kernel import get_kernel
            _sid = self.memory.conversation_session_id
            if not _sid:
                return ""
            lines_ = l3_index_lines(DecayStore(get_kernel().store), _sid)
        except Exception:
            return ""
        if not lines_:
            return ""
        return (
            "\n\n[Earlier in this conversation - moved out of context]\n"
            "These exchanges are no longer in your context, but they did happen and "
            "they are still recallable with recall_conversation. Use them to know that "
            "something exists, then recall it if the current question needs it. "
            "Do NOT claim you never discussed these." + "\n"
            + "\n".join(f"  {x}" for x in lines_) + "\n"
        )

    def _image_note_request_block(self) -> str:
        """只在「这轮有图且还没记」时出现的一次性提示。

        ⚠️⚠️ **它由 `has_unsummarized_image()` 控制 —— 和 `note_image` 工具是同一个条件。**
           📌 那条：**一个工具和它的事实来源，必须由同一个条件控制**；
              这里连提示词也挂在同一个条件上，于是三者永远同生共死。

        ⚠️ 「摘要千万不能跟 changelog 修复那个 base64 的 bug 一样，一直每轮注入」——
           ⭐ 这段**没有第二轮可注入**：模型一调 `note_image`，条件就为假。
           而万一它不调，下一轮还有图未记 —— 那时提示还在，这是**正确的**重试，
           不是"每轮都塞"。
        """
        try:
            if not self._tool_runtime_view().has_unsummarized_image():
                return ""
        except Exception:
            return ""
        # ⭐ 留痕：这段是一次性的，出现过就该看得见 —— 否则"摘要没生成"时
        #    分不清是【提示没注入】还是【模型没听】。两者的修法完全不同。
        logger.info("[D10] 本轮有图待记摘要 → 注入一次性提示 + 提供 note_image")
        # ⚠️⚠️ 措辞被实测推翻过一次（2026-08-13）：第一版只说"Before you reply, call
        #    note_image once"，**注入了、工具也给了，模型三次全部无视，直接作答**。
        #    根因不是它没读到，是**主循环的纪律在压制它** —— 系统提示里反复讲
        #    「闲聊不调工具 / 第一轮直接出话」，而这条要求它为一件**用户看不见的事**
        #    先调一次工具，正好撞在那条纪律上。
        # 📌 **一条新指令如果和系统反复强化过的纪律冲突，它必须自己说明"我覆盖那条"** ——
        #    否则模型会按更常被强调的那条走，而且不会告诉你它做了取舍。
        return (
            "\n\n[Image just received — one required tool call]\n"
            "The user's latest message carries an image, and its pixels will be dropped from your "
            "context shortly. Call note_image ONCE, first, before you reply.\n"
            "⚠️ This OVERRIDES the usual guidance about answering directly without tools. It is "
            "not optional and it is not a judgement call: an image is present, so this one call "
            "happens. It is the only chance to record what is in it.\n"
            "⚠️ The summary is NOT your answer. Write it for your future self, independently of "
            "what the user asked; then reply to their actual message in your own voice, and never "
            "recite the summary back to them.\n"
        )

    def _execution_scope_block(self, scope: str = "main_react") -> str:
        """每轮的执行作用域声明。极短（约 40 token）。

        ⚠️⚠️ **措辞必须是 "this model request"，不能写 "this turn"。**
        一个用户 turn 内部有**多次** ReAct model request，而 `load_tools` 之后
        `tools_manifest` 会在 turn 中途变化 —— 说 turn 就把粒度说错了一档，
        而这次要明确的粒度恰恰就是 request。

        ⚠️ **刻意不列工具名。** 当前 API manifest 已经是工具的唯一权威来源，
           在这里再列一遍就是第二份名单（正是 花一整轮消灭的东西）。
           所以这段话只讲**规则**，不讲**内容** —— 也因此它在一个 turn 内恒定。

        📌 而这段话之所以敢说 "Only tools attached to this model request are callable"，
           是因为 让它**成为了真的**（`TOOL_NOT_ACTIVE` 那道闸）。
           在 ③ 之前它是一句假话 —— 实测实证：`create_new_skill` 不在本轮 6 个工具里，
           模型凭历史 schema 调它，**照样执行了**。
           📌 **要注入一句话，先让它成为真的。**
        """
        try:
            from core.runtime.identity import current_runtime_id
            _rid = current_runtime_id()
        except Exception:
            _rid = "unknown"
        return (
            "\n\n[Execution Scope]\n"
            f"runtime_id={_rid}\n"
            f"scope={scope}\n"
            "Only tools attached to this model request are callable. "
            "Historical tool activation does not carry forward — an earlier "
            "\"loaded capabilities\" result does not make a tool callable now. "
            "If a capability you need is not attached, call load_tools first, "
            "then use it on the next step.\n"
        )

    def _build_quoted_selection_injection(self) -> str:
        """用户在聊天区**选中了一段文字**，然后右键 replay。

        ⭐ 与 `_build_open_interactions_injection` 是两件事，**刻意分开写**：
           那一段说的是「去答这条待办」（带 interaction_id，有工具要调）；
           这一段说的是「我在指刚才这句话」（没有 id，也没有工具要调）。
           📌 「一个字段不许表达两个现实」—— 合成一段就得靠 `iid` 空不空
              去猜是哪种，而那正是让人猜错的写法。

        ⚠️ 引用的是**聊天区里已有的文字**，所以它一定在上下文里（或者已被
           移出 —— 那更需要这一段，模型自己已经看不到原话了）。
           所以这里把原文**逐字**给它，不做摘要。

        ⚠️ 长度截到 200（UI 侧存的时候就截过）：引用是个指路标，不是重新贴一遍。
        """
        rt = (getattr(self, "_reply_target_turn", None)
              or getattr(self, "_reply_target", None) or {})
        if rt.get("kind") != "selection":
            return ""
        q = (rt.get("q") or "").strip()
        if not q:
            return ""
        return (
            "\n\n[Quoted by the user]\n"
            f'The user selected this text from earlier in this conversation and is '
            f'replying to it:\n"""\n{q}\n"""\n'
            "Their message is about THAT specific passage. Resolve any vague reference "
            "in it (this, that, it, here) against the quote above before anything else in "
            "the conversation. If the quote is something you said, they are pointing at "
            "your own words - do not treat it as a new topic."
        )

    def _build_open_interactions_injection(self) -> str:
        """把未决交互摆到模型面前。

        ⭐ 这是整条改造里**用户体感变化最大**的一块。
        旧实现是路由劫持：代码把下一条用户消息截走，模型自始至终不知道
        有个问题挂在那里。于是"用户回答了但被判成新话题"和"用户问了别的
        但被当成回答"这两种错误，模型都没有机会纠正——它看不见。

        现在改成把状态摆给模型，由它判断。三种走向都是它决定的：
            回答      → 调 answer_open_interaction
            改需求    → 同上，relation=ANSWER_AND_AMENDMENT
            说别的    → 不调工具，正常处理；交互保持 OPEN 留在屏幕上

        落在缓存哨兵之后的动态段：没有待办时返回空串、一个字符都不加。
        """
        recs = _rt_live_interactions(self)
        if not recs:
            return ""
        from core.runtime import interaction as _it

        # ⭐⭐ [2026-08-06 实测] 标注新旧，并把"最新那条"显式指出来。
        #
        # 实测："把 skill 部署吧"（不点名）→ Nano 部署了**队列里第一条**。
        # 那条恰好是最早创建的（前台槽给最先来的那个），而模型是照着这份清单挑的。
        #
        # 理由很硬：真实场景下 Nano 跟用户聊了很多轮，最后一轮刚做完一个 Skill，
        # 用户说"ok 部署吧" —— **指的必然是刚做完那个**。反手部署最早那个非常反直觉。
        #
        # ⚠️ 但**不在代码里硬选**（设计原则 3）：已明确说"问用户"和"部署最新的"
        # 两种都对，取决于人设模板与模型的谨慎程度。代码该做的是**把新旧这个事实
        # 摆清楚**，让模型能自己判断；唯一的硬约束是"别默认挑最旧的"。
        #
        # 顺序仍然保持与 UI 一致（前台优先），因为用户说"第一个"时两边得指同一条。
        _newest_id = max(recs, key=lambda x: x.created_at).interaction_id if recs else ""
        _now = 0.0
        try:
            from core.runtime.kernel import get_kernel as _gk
            _now = _gk().now()
        except Exception:
            pass
        lines = []
        _live_ids: set[str] = set()
        _has_audit = False
        for r in recs:
            if r.kind == _it.Kind.SKILL_AUDIT:
                _has_audit = True
            q = (r.prompt_text or "").strip().replace("\n", " ")
            if len(q) > 200:
                q = q[:200] + "…"
            _age = ""
            if _now and r.created_at:
                _m = max(0, int((_now - r.created_at) / 60))
                _age = f" · created {_m}m ago" if _m else " · created just now"
            _live_ids.add(r.interaction_id)
            _tag = "  ← NEWEST" if r.interaction_id == _newest_id and len(recs) > 1 else ""
            lines.append(f"  - {r.interaction_id} [{r.kind}]{_age}{_tag} {q}")
            if r.status == _it.Status.ANSWERED:
                # 重试入口。没有这行，ANSWERED 就成了没有消费者的永久状态
                # 早先就要求过必须定义谁来触发重试）。
                a = (r.answer_verbatim or "").strip().replace("\n", " ")
                if len(a) > 120:
                    a = a[:120] + "…"
                lines.append(
                    f"      ALREADY ANSWERED but the follow-up failed last time. "
                    f'The user already said: "{a}" — call answer_open_interaction again '
                    f"with the same text to retry. Do NOT ask them to repeat themselves."
                )

        # 「回复这条」的目标：只有它**还在上面这份清单里**才算有效指向。
        # 指向已关闭/已取代的那条 = 把模型逼进死角，理由见下方注释。
        #
        # ⭐⭐⭐ [2026-08-13 CMD63] **先读本轮快照，再退回实时值。**
        #    UI 一按发送就把指向移交到 `_reply_target_turn`（见 `hand_off_reply_target`），
        #    此刻 `_reply_target` 已经是空的 —— 只读它就等于永远读不到用户点的那条，
        #    那正是 CMD63「引用了审计卡却去新建 Skill」的根因。
        # ⚠️ 保留 `or _reply_target` 这一路：不经过 UI 发送路径的调用方
        #    （测试替身、将来别的入口）仍然只设了 `_reply_target`。
        _reply_iid = (
            (getattr(self, "_reply_target_turn", None)
             or getattr(self, "_reply_target", None) or {}).get("iid") or ""
        )
        if _reply_iid and _reply_iid not in _live_ids:
            logger.info(
                f"[Interaction] 「回复这条」目标 {_reply_iid} 已不在未决清单里，"
                f"本轮不注入指向（UI 侧应同步复位）"
            )
            _reply_iid = ""

        return (
            # ⚠️ 这段措辞第一版有两个实测暴露出来的缺口（2026-08-05，）：
            #
            # ① **只提了"answers"，完全没提"取消"。** 用户说"算了不做了"时，
            #    模型在这段里找不到任何适用的指令，于是只回了一句"好的，取消了"、
            #    **没调工具**，待办留在原地。`relation=CANCEL` 明明存在，
            #    但它只写在工具描述里，而模型是照着这段动态段决定要不要调工具的。
            # ② 原文强调 "not blocking" / "do not nag" —— 那是为了防它过度热情，
            #    结果把它压成了**过度冷淡**："just handle their request normally"
            #    成了阻力最小的路。
            #
            # 现在把三种走向**并列写全**，并且明确"关掉它需要调工具，光说一句不算"。
            "\n\n[Open Interactions]\n"
            "Nano is waiting on the user for these. They do not block anything: "
            "the user may reply, may drop the whole thing, or may talk about something else.\n"
            "Three cases, and only the first two involve the tool:\n"
            "1. The message answers one of them (even while also changing the requirement) "
            "→ call answer_open_interaction with relation=ANSWER or ANSWER_AND_AMENDMENT.\n"
            "2. The message says they no longer want it ('forget it', 'never mind', "
            "'drop that skill') → call answer_open_interaction with relation=CANCEL. "
            "**Replying 'okay, cancelled' in text does NOT close it** — the item stays open "
            "and will keep showing up here until the tool records the cancellation.\n"
            "3. The message is about something else entirely → handle it normally, call no tool, "
            "leave the item open. Do not nag them about it.\n"
            # ⭐⭐⭐ [2026-08-06 实测] `[skill_audit]` 的语义必须写出来。
            #
            # 上面那三条是照着**澄清**写的（"Nano 在等你回答问题"）。对审计条目
            # 这个框架是错的：审计不是一个问题，**代码已经写完了在等批准**。
            #
            # 实测复现（19:37）：用户在有真代码的待审卡 int_9eeac9e476 上点了
            # 「回复这条」，然后说「你能不能目前修改这个代码」——
            # 指向是活的、是对的，`answer_open_interaction` 也在工具表里，
            # 模型照样走了 `create_new_skill`，并且原话说
            # **「到现在为止，我们只是澄清了需求，还没有生成任何代码」**。
            #
            # 它不是不听话，是**它看到的那一行只有一个不透明的标签 `[skill_audit]`**，
            # 头部还告诉它"这些是 Nano 在等用户回答的问题"。于是"改这个代码"、
            # "部署这个吧"、"继续"这些话，它认得的唯一出口就是 create_new_skill
            # → 每轮再造一个同名 Skill → 卡片越堆越多 → 死循环
            #   （最后堆了三张同名 GetComputerIP 的待审卡）。
            #
            # 📌 判据：**给模型一个状态标签，不等于给了它这个状态的语义。**
            #    枚举名对写代码的人是自解释的，对模型只是一个陌生字符串。
            + ("\n[About the skill_audit items above]\n"
               "Those are NOT questions — **the code is already written**. Nano finished a "
               "draft and it is sitting in the review window, waiting for the user's call. "
               "So for a skill_audit item:\n"
               "- ANSWER ('deploy it', 'go ahead', 'looks good', 'yes') → the draft is "
               "deployed as-is.\n"
               "- ANSWER_AND_AMENDMENT ('change X', 'use Y instead', 'can you modify this "
               "code', 'make it also return Z') → the draft is rewritten with that change. "
               "**This is how you modify pending code — you do not need to write it again.**\n"
               "- CANCEL ('forget it', 'drop that one') → the draft is discarded.\n"
               "⚠️ Do NOT call create_new_skill for a requirement that already has a "
               "skill_audit item open. That does not act on the existing draft — it produces "
               "a second draft under the same name, and the user ends up with a pile of "
               "near-identical review cards.\n"
               if _has_audit else "")
            # ⭐ 用户点了卡片上的「回复这条」→ 指向是**显式**的，不用猜。
            # 这与下面那段歧义消解互补：那条让模型猜得更准，这条让它根本不必猜。
            #
            # ⚠️⚠️ 必须先确认 `_reply_target` 指的那条**还活着**（`_reply_iid` 已过滤）。
            #
            # 2026-08-06 实测：用户在 17:26 点了澄清
            # int_192a0393ab 的「回复这条」，17:46:13 那条被草稿消化 → SUPERSEDED。
            # 但 `_reply_target` 只有用户手点 ✕ 才清，于是**之后每一轮**都注入
            # 「answering int_192a0393ab … do not pick a different one」，
            # 而下面的清单里根本没有它。
            #
            # 后果比"多一句废话"严重得多：17:58 用户说「部署这个吧」，模型被
            # **点名指向一条不存在的交互 + 明令禁止改挑别的** ——
            # `answer_open_interaction` 无路可走，于是退回 `create_new_skill`，
            # 新建了一个 Skill 而不是部署待审那个（用户报的最恶性那条）。
            #
            # 📌 判据：**指向性的注入必须校验指向的东西还在。**
            #    一个悬空的"必须回答 X"比没有这句话更糟 —— 它把模型逼进死角，
            #    而模型只能从别的工具里找出路。
            #
            # ⚠️ 这只是安全网。真正的修法是 UI 侧发完消息就复位（用户的 BUG5），
            #    两边都要有：目标也可能在选中期间被别人关掉。
            + (f"\n⭐ The user explicitly marked this message as answering "
               f"{_reply_iid} "
               f"— they clicked \"reply to this\" on that card. "
               f"Treat it as the answer to that item; do not pick a different one.\n"
               if _reply_iid else "")
            # ⭐⭐ 歧义消解（2026-08-06 实测）：不点名时该指哪一条。
            # 见上面 `_newest_id` 那段注释里用户的理由。
            # ⚠️ 上面插了一个 `+ (条件表达式)` 之后，这里**必须继续用 `+`** ——
            # 裸字符串没法跟一个 `+` 表达式的结果做隐式拼接（那是语法错误）。
            + "\nIf several items are open and the user does not say which one "
            "('deploy it', 'go ahead', 'yes'):\n"
            "- The list is ordered newest first, so item 1 is the most recent. "
            "That one (also marked ← NEWEST) is almost always what they mean — "
            "they were just talking about it. Prefer it.\n"
            "- Asking which one is also fine, and is the better choice when the items are "
            "similar or when acting on the wrong one would be hard to undo.\n"
            "- **Never quietly pick the oldest one.** After a long conversation, 'deploy it' "
            "referring to something from twenty turns ago is not a reading a person would make.\n"
            + "\n".join(lines)
        )

    def _build_session_log_injection(self) -> str:
        """Format current-session operation records for system_guide injection.

        Return empty text when there is no session log.
        Limit each entry length to avoid bloating system_guide.
        """
        if not self._session_log:
            return ""
        lines = []
        for entry in self._session_log[-10:]:  
            if len(entry) > 120:
                entry = entry[:117] + "..."
            lines.append(f"  • {entry}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Session Log]\n" + "\n".join(lines)

    def _build_episodic_injection(self) -> str:
        """Inject recent cross-session summaries from working memory into system_guide."""
        if self._wm is None:
            return ""
        try:
            rows = self._wm.get_recent_session_ends(limit=5)
        except Exception:
            return ""
        if not rows:
            return ""
        lines = []
        for r in rows:
            ts_short = r["ts"][5:16] if r.get("ts") else ""  # MM-DD HH:MM
            subject = r.get("subject", "")[:40]
            outcome = r.get("action", "")  # success/failed/chitchat/partial
            outcome_badge = {"success": "✓", "failed": "✗", "partial": "~", "chitchat": "💬"}.get(outcome, "")
            import json as _json
            try:
                skills = _json.loads(r.get("tags", "[]") or "[]")
            except Exception:
                skills = []
            skills_str = f" [Skills: {', '.join(skills)}]" if skills else ""
            lines.append(f"  • [{ts_short}]{(' ' + outcome_badge) if outcome_badge else ''} {subject}{skills_str}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Recent Cross-Session Summaries]\n" + "\n".join(lines)

    # ⭐⭐⭐ **记忆摘要每轮无条件注入 —— 整个改造的核心。**
    #
    # 🔴 2026-08-05 的原话，根因当时就说对了：
    #    > cc 会在合适的时候召回记忆是因为 **cc 每次都注入了摘要**；
    #    > nano 只是很软地声明了「在 xx 时候必须先查看记忆再回答」。
    #    > **太模糊，nano 没办法先入为主地「预览到」记忆里大概有些什么。**
    # 📌 **一条召回不到的记忆，和一条没写过的记忆，对用户是同一个东西。**
    # ⭐ 沿用这句：**索引不是被回忆的，是被塞进来的。**
    # ⭐ 这是「注入事实」模式的第六个实例：
    #    健康态 → 预算态 → 上下文压力 → 记忆索引 →
    #    [窗口形态] → 本条。
    #
    # ═══ ⚠️ 保底（floor），不是配额（quota）—— 2026-08-24 定 ═══
    # 一度建议「N 写成配额，和 L0/L1/L2 共用水位」。用户否掉：
    #    「我没把握，因为如果特别小对这部分是致命的，**因为纠错本身是一等公民**」
    # 这个判断是对的：
    #    L0→L1→L2→L3 是**分辨率阶梯**，每一档都还携带着东西，降档 = 变模糊
    #    记忆     **没有半条**：要么完整生效，要么等于不存在
    # 📌 **配额是给「可以降分辨率的东西」用的。** 记忆降不了分辨率，
    #    进那套机制的唯一结果是「压力一大就被挤掉」——
    #    而压力大 ＝ 对话长 ＝ 用户投入最多的时刻。
    #
    # ⭐⭐ [2026-08-25] **连「保底 40 条」这个数字也拿掉了。**
    #    原话：「我们不数记忆的条数，我们数记忆导致的常驻 token 注入的消耗」。
    #    ⇒ **全部注入，不截断**；约束改成下面那套按 token 水位的提醒。
    # 📌 为什么：**一个「会积累的东西」的清除条件，应该来自它消耗的那个资源** ——
    #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
    #    「40 条」是那个资源的**代理变量**，而代理变量在密度变化时就失效
    #    （这条判据是 L3→L4 那格定下来的，这里是它的第二次兑现）。
    # ⚠️ 于是这里**不再有任何常量** —— 阈值全在 `_MEM_WARN_ON/OFF` 那一段，
    #    而且那三个数是**算出来的**（见那段注释），不是拍的。


    # ══════════════════════════════════════════════════════════════════════
    # ⭐⭐⭐ **记忆的自治理：数 token，不数条数。**
    #
    # 已定形状：
    #   · 不设条数上限 —— 全部注入
    #   · 盯的是**这些记忆每轮真实花掉多少 token**
    #   · 越过阈值才注入一句话，让 Nano 在**这一轮自然收尾时**跟用户提一句，
    #     并且**自己先判断**哪些低价值/过时，但**不许擅自删**
    #
    # 📌 为什么数 token 不数条数：**一个「会积累的东西」的清除条件，
    #    应该来自它消耗的那个资源** —— 一条 30 字的和一条 300 字的，
    #    条数上一样，成本差十倍。（这条判据是 L3→L4 那格定下来的。）
    #
    # ═══ 两条线 + 只记「上次提醒时的水位」═══
    # 🔴 已明确两个问题，根子是同一个：**只有一条线的开关会在线附近抖动。**
    #    ① 用户不理会 → 每轮都提 → Nano 当成迫切问题
    #    ② 1200 提示 → 删到 900 → 又写回 1200 → 又提示（鬼畜）
    # ⭐ 修法：触发线与解除线分开，并且**只记上次提醒时的水位**，
    #    要再涨 50% 才提第二次。
    #      提醒条件： 水位 > WARN_ON  且  水位 > last_warned × 1.5
    #      提醒之后： last_warned ← 当前水位
    #      跌破 WARN_OFF： last_warned ← 0（重新武装）
    #
    # ⭐⭐ **我们从不判断用户答了什么。** 提醒的语义是「告诉你一声」，
    #    而告诉一次就够了；再涨一大截是**新的事实**，值得再说一次。
    #    📌 一旦要判断「用户同意还是拒绝」，就得做自然语言理解 ——
    #       而那正是本项目明确否过的方向（「不许让代码去理解『算了别点了』」）。
    #    ⇒ 用户越不在意，提醒频率**指数级变稀**（1200 → 1800 → 2700 → …）。
    #
    # ═══ 阈值是**算出来的**，不是拍的═══
    #   注入格式 `  - {英文 summary_model}  [when: {英文 when}]`
    #   四条真实样例实测（`meter.estimate_text`）：平均 **20.2 token/条**，表头 27。
    #     20 条 ≈  432    40 条 ≈  837    60 条 ≈ 1242    80 条 ≈ 1647
    #   ⇒ WARN_ON 1200 ≈ **58 条**：58 条之前一句不提
    #     忽略一次后 1800 ≈ **88 条**：再攒 30 条才提第二次
    #     WARN_OFF  800 ≈ **38 条**：要真清理到 40 条以内才重新武装
    # ⚠️ 英文摘要 ≈20 token/条，同内容中文 ≈38 —— 强制 `summary_model` 写英文
    #    这一条本身就把水位压掉了一半。
    # ══════════════════════════════════════════════════════════════════════
    _MEM_WARN_ON = 1200          # 触发线（token）≈ 58 条

    _MEM_WARN_OFF = 800          # 解除线（token）≈ 38 条

    _MEM_WARN_GROWTH = 1.5       # 再涨这么多倍才提第二次

    def _mem_water_path(self):
        import pathlib as _pl
        from core.paths import data_path
        return data_path("memory_water.json")

    def _mem_water_read(self) -> float:
        """上次提醒时的水位。读不到当 0（= 未提过）。

        ⚠️ fail-safe 方向是 **0**：读不到就当没提过，最多多提一次；
           反过来（当成提过）会让一个真的涨上去的水位**永远不提**。
        """
        try:
            import json as _j
            return float(_j.loads(self._mem_water_path().read_text("utf-8"))
                         .get("last_warned_at", 0) or 0)
        except Exception:
            return 0.0

    def _mem_water_write(self, v: float) -> None:
        try:
            import json as _j
            _p = self._mem_water_path()
            _p.parent.mkdir(parents=True, exist_ok=True)
            _p.write_text(_j.dumps({"last_warned_at": round(float(v), 1)}), "utf-8")
        except Exception as e:
            logger.debug(f"[O2] 水位留痕失败（不影响本轮）: {e}")

    def _mem_water_notice(self, injected: str) -> str:
        """按注入内容算水位，越线就返回那句提醒；否则空串。

        ⚠️ 算的是**真实注入出去的那段文本**，不是估的条数 ——
           📌 要管一个资源，就得量它本身，不能量它的代理变量。
        ⚠️ 整段吞异常：📌 治理是家务，不该有能力让这一轮失败。
        """
        try:
            if not injected:
                return ""
            from core.context.meter import estimate_text as _est
            _now = float(_est(injected))
            _last = self._mem_water_read()
            if _now < self._MEM_WARN_OFF and _last:
                self._mem_water_write(0)          # 真清理过 → 重新武装
                return ""
            if _now <= self._MEM_WARN_ON or _now <= _last * self._MEM_WARN_GROWTH:
                return ""
            self._mem_water_write(_now)
            logger.info(f"[O2] 记忆注入水位 {_now:.0f} token 越线（上次 {_last:.0f}）→ 提醒一次")
            # ⚠️ **不列候选**：那句提醒本身就是为了省 token，
            #    它自己很长就矛盾了。而 Nano 手上已经有全部摘要，
            #    足够做一轮浅判断；真要确认某条是否过时，再去 recall 全文。
            # ⚠️ 明说要先 load `forget_user_note` —— 📌 否则会重演 computer_use
            #    那个坑：告诉它去用一个它手上没有的工具。
            #
            # 🔴🔴 [2026-08-25 实测] 第一版写的是
            #    「Finish what you are doing first. Then, when this turn **wraps up
            #      naturally**, mention it to them」—— **它一个字都没提。**
            #    日志显示提醒确实注进去了（dyn 7713 → 下一轮 6373），是模型没照做。
            # 📌 根因是改动了用户的措辞：原话是「**你在你的下一条消息当中**，
            #    应该向用户给出建议」，被软化成了「自然收尾时顺带提一句」。
            #    **一条明确的指令被软化成了一条建议，然后它就被当成建议对待了。**
            # ⚠️ 软化的动机是「别劫持当前任务」—— 但那个担心该靠**位置**解决
            #    （答完用户再说），不该靠**削弱语气**解决。两者被混成了一件事。
            # ⚠️ 而对着一句「你好」，「自然收尾时」几乎等于没说：这一轮没什么要收尾的。
            # ⭐ 现在：**这一轮必须说**（明确），但**放在回答之后**（不劫持）。
            #    并且如实告诉它「跳过就没有下一次了」—— 📌 一个不说明代价的指令，
            #    模型没有理由把它排在别的事情前面。
            return (
                f"\n\n[Memory cost - act on this in THIS reply]\n"
                f"The memories above now cost about {_now:.0f} tokens on every single "
                f"turn. Nothing is broken; the user just deserves to know.\n"
                f"Answer the user normally first. Then, at the END OF THAT SAME REPLY, "
                f"add a short paragraph that: says roughly what the memories cost per "
                f"turn; names the ones you think have gone stale or low-value (you can "
                f"see them all above - judge for yourself, do not make the user audit "
                f"the list); and offers to delete those for them.\n"
                f"To actually delete, load forget_user_note first - it is not attached "
                f"by default. Delete only what they agree to; never on your own.\n"
                f"Do NOT skip this and do NOT save it for a later turn: this notice "
                f"appears once and will not come back until the cost grows a lot more. "
                f"If they say the cost is fine, that is a perfectly good answer."
            )
        except Exception as e:
            logger.debug(f"[O2] 水位判断跳过: {e}")
            return ""

    def _build_memory_injection(self) -> str:
        """把已确认记忆的 `summary_model` 一行行塞进动态段。

        ⚠️ 用 `summary_model` **不是** `summary_user` —— 两句话是分开写的：
           📌 一句话同时服务两个受众，最后两边都不合身（这个坑踩过）。
        ⚠️ 带上 `applies_when`：📌 一条不说「什么时候用得上」的记忆，
           模型只能每条都掂量一遍；把作用域摆在旁边，它才能一眼跳过不相关的。
        ⚠️ 整段吞异常并返回空串：📌 记忆是**增益**，读不到不该让这一轮失败。
        """
        try:
            if self._wm is None:
                return ""
            rows = self._wm.get_all_confirmed_notes()
        except Exception as e:
            logger.debug(f"[O2] 读记忆失败（本轮不注入）: {e}")
            return ""
        if not rows:
            return ""
        lines = []
        # ⚠️ **不设条数上限**：📌 要管一个资源就得量它本身 ——
        #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
        #    真正的闸是下面那个**按 token 水位**的提醒。
        for r in rows:
            _s = (r.get("summary_model") or r.get("detail") or "").strip()
            if not _s:
                continue
            _w = (r.get("applies_when") or "").strip()
            # 🔴 [2026-08-25 实测] 这一行原来**不带 id** —— 而 `forget_user_note`
            #    要的正是 id。实测里 Nano 判断出「这条已经过时该删」（完全正确），
            #    然后**只能猜**，猜了 1 和 2，两次都 "No memory with id N was found"。
            # 📌 **我们让它做一件事，却没给它做这件事需要的东西** ——
            #    跟 `look_at_screen` 只给散文不给坐标是同一个形状。
            #    ⚠️ 而工具描述里还写着「by the id shown next to it」，
            #       **根本没有任何地方 shown** —— 一句指向空气的描述。
            # ⚠️ 代价：编号约 4 字符 ≈ 1~2 token/条，可以忽略。
            lines.append(f"  - #{r.get('id')} {_s}" + (f"  [when: {_w}]" if _w else ""))
        if not lines:
            return ""
        _block = ("\n\n[What you remember about this user]\n"
                  "These are already in front of you - you do NOT need to look them up.\n"
                  + "\n".join(lines))
        # ⭐ 水位按**真实注入出去的那段文本**算，不按条数估。
        return _block + self._mem_water_notice(_block)

    def _build_window_mode_injection(self) -> str:
        """每轮注入窗口形态与 GUI 任务状态，让模型不必猜「现在是不是 mini」。

        三种状态：mini（任务进行中，不要再缩）/ 任务进行中但用户放大了（尊重，只在真的
        挡住下一步时再缩并说明）/ full 且没有任务（操作屏幕前先缩窗开始任务；看屏幕不需要）。
        """
        if self._window_mode_now() == "mini":
            return ("\n\n[Window] Nano's own window is CURRENTLY MINI (small, "
                    "top-right corner) and a screen-operation task is in progress. Do NOT call "
                    "set_window_mode('mini') again; it is already done.")
        if self._gui_task_active():
            return ("\n\n[Window] A screen-operation task is in progress, but the user enlarged "
                    "Nano's window - respect that. Shrink again only if the window truly blocks "
                    "the next step (a drag across it, or a target hidden under it); looking at the "
                    "screen is never a reason. If you shrink, say in one short sentence why, then "
                    "continue. No new authorization is needed.")
        return ("\n\n[Window] Nano's own window is CURRENTLY FULL SIZE. Before operating the "
                "screen with mouse or keyboard (computer_use), call set_window_mode('mini'): it "
                "starts a screen-operation task that the user approves once. Looking at the "
                "screen does not require it (look_at_screen hides Nano by itself).")

    def _build_ambient_injection(self) -> str:
        """Ambient Memory Phase 1: inject a lightweight rule-based activity snapshot.

        Nano can use this to resolve references such as "this", "that thing",
        or "what I was just doing" without extra model calls.
        This uses only context-level signals such as app name, window title,
        activity rhythm, idle time, saves, and background audio. It does not read private content.
        Nano's own window is excluded because the real ambient context is usually the window
        the user was using before switching to Nano.
        """
        try:
            from core.proactive.activity import get_buffer
            from core.self_identity import is_self_pid, is_self_window
            snap = get_buffer().snapshot()
        except Exception:
            return ""
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        saves = snap.get("saves", [])

        def _self(w) -> bool:
            try:
                return (is_self_pid(getattr(w, "pid", 0) or 0)
                        or is_self_window(getattr(w, "hwnd", 0) or 0))
            except Exception:
                return False

        # Use all focus events as time boundaries, including Nano windows.
        # Nano windows are excluded from reporting, but they still provide accurate duration boundaries.
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        if not all_focus and not keys:
            return ""

        def _dur(sec: float) -> str:
            m = int(sec // 60)
            return f"about {m} min" if m >= 1 else "less than 1 min"

        dwell: dict = {}
        last_real = None
        app_title: dict = {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            dur = max(0.0, end - w.ts)
            if _self(w):
                continue
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + dur
            app_title[app] = w.window_title or ""
            last_real = w
        cur = last_real
        span_min = int((now - all_focus[0].ts) / 60) if all_focus else 0
        top = sorted(dwell.items(), key=lambda kv: -kv[1])

        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        last_act = max([k.ts for k in keys] + [w.ts for w in all_focus] + [0.0])
        idle_sec = now - last_act if last_act else 9999

        # Interpretation layer: app category + title parsing + restrained activity guess.
        _cat = _ambient_cat
        _parse_title = _ambient_parse_title
        cur_app = (cur.process_name or "").replace(".exe", "").strip() if cur else ""
        dom_app = top[0][0] if top else cur_app
        dom_cat = _cat(dom_app)
        f_name, proj, page = _parse_title(cur_app, cur.window_title if cur else "")
        n_switch = sum(1 for w in all_focus if not _self(w))

        dom_url = (snap.get("url_domain") or "").strip()
        guess = ""
        if dom_cat == "editor":
            guess = ("looks like editing/debugging code" if (char_n >= 60 and bs_n * 3 >= char_n)
                     else "looks like writing code" if char_n >= 60 else "looks like reading code")
        elif dom_cat == "browser":
            if dom_url:
                guess = f"looks like browsing {dom_url}"
            else:
                guess = "looks like researching or looking for something" if n_switch >= 4 else "looks like reading a webpage"
        elif dom_cat == "im":
            guess = "looks like chatting or communicating"
        elif dom_cat == "terminal":
            guess = "looks like running commands or debugging"
        if not guess and char_n == 0 and idle_sec > 180:
            guess = "was idle for a while and may have just returned"

        import datetime as _dt2
        parts = []
        if cur:
            since = now - cur.ts
            ago = "was just in" if since < 90 else f"was in about {int(since // 60)} min ago"
            tgt = cur_app
            if f_name:
                tgt += f" ({f_name[:40]})"
            elif page:
                tgt += f" ({page[:40]})"
            lead = f"{ago} \"{tgt}\""
            # ⭐⭐ **「刚才」那一条往往根本不在 trail 里。**
            #    `recent()` 刻意 `exclude_last_sec=600`，而 trail 每 4 分钟才写一次 ——
            #    用户在记事本改完切过来就问，命中的是**实时 buffer**，不是 trail。
            #    📌 只给 trail 加句柄的话，那个最常见的场景一条都拉不到。
            _cur_ref = getattr(cur, "ref", None) or {}
            if _cur_ref.get("path"):
                lead += " ▸" + _dt2.datetime.fromtimestamp(cur.ts).strftime("%H:%M:%S")
            if guess:
                lead += f", {guess}"
            parts.append(lead)
        elif guess:
            parts.append(guess)

        if top:
            def _seg_one(a, s):
                # Browser includes page title; editor includes file name when available.
                fn, pj, pg = _parse_title(a, app_title.get(a, ""))
                detail = fn or pg
                d = f" {detail[:36]}" if detail else ""
                return f"{a}{d} ({_dur(s)})"

            seg = "; ".join(_seg_one(a, s) for a, s in top[:4])
            head = f"over the last about {span_min} min, mainly in" if span_min >= 1 else "recently, mainly in"
            parts.append(f"{head}: {seg}")

        if saves:
            # ⭐ 这里原来直接甩**原始完整窗口标题**，是全局
            #    唯一一处没过 `_parse_title` 的地方 —— 它反而会漏出
            #    `xxx.txt - 记事本` 这种带文件名的串。
            # 📌 **歪打正着 ≠ 可控**：改完 ① 之后，同一个文件会在同一段话里
            #    出现两种写法（`notepad (a.txt)` 和 `saved a file (a.txt - 记事本)`），
            #    而模型没有理由知道它俩是一个东西。⇒ 两条对齐到同一个解析器。
            # ⚠️ 解析不出来时**退回原始标题**，不是退回空 —— 一个不认识的应用，
            #    完整标题仍然比什么都没有强。
            _sv = saves[-1]
            _sf, _, _sp = _parse_title(
                (_sv.process_name or "").replace(".exe", "").strip(),
                _sv.window_title or "")
            st = (_sf or _sp or (_sv.window_title or "")).strip()
            parts.append("recently saved a file" + (f" ({st[:40]})" if st else ""))

        audio = list(snap.get("audio_apps") or [])
        if audio:
            parts.append("background audio: " + ", ".join(audio[:2]))

        # Persistent trail from earlier today, outside the short live buffer.
        try:
            from core.proactive import ambient_trail
            import datetime as _dt
            tr = ambient_trail.recent(hours=12, limit=6)
            segs, prev = [], None
            for r in tr:
                ln = r.get("line", "")
                if ln and ln != prev:
                    # ⭐ 精度提到**秒**：这个时刻同时是拉取时的 id。
                    # 🔴 **绝不能用序号** —— trail 是滚动的（18h/300 条）且每 4 分钟
                    #    新增一条，注入取的是最后 6 条：
                    #      第 N 轮模型看到「#3」→ 第 N+1 轮它调工具要 #3 → 已经不是那条了。
                    #    📌 **一个会在两次调用之间漂的标识，漂了没人知道。** 时刻不漂。
                    hhmm = _dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
                    _mark = " ▸" if (r.get("ref") or {}).get("path") else ""
                    segs.append(f"{hhmm} {ln}{_mark}")
                    prev = ln
            if segs:
                parts.insert(0, "earlier today: " + "; ".join(segs))
        except Exception:
            pass

        if not parts:
            return ""

        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Ambient]\n" + "; ".join(parts) + "."

    def _ambient_scene_now(self):
        """当前现场的一行摘要 —— **起返回 `(line, ref)`**。

        ⚠️ `ref` 取的是「停留最久那个应用**最后一条**焦点记录」上的句柄：
           dwell 决定的是「这段时间主要在哪」，而句柄必须落在**具体某一次**
           焦点上。📌 一个按时长聚合出来的对象，不能带一个按次记录的地址 ——
           除非明确说清取的是哪一次。
        """
        try:
            from core.proactive.activity import get_buffer
            from core.self_identity import is_self_pid, is_self_window
            snap = get_buffer().snapshot()
        except Exception:
            return ("", None)
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        dwell, titles, refs = {}, {}, {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            try:
                if (is_self_pid(getattr(w, "pid", 0) or 0)
                        or is_self_window(getattr(w, "hwnd", 0) or 0)):
                    continue
            except Exception:
                pass
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + max(0.0, end - w.ts)
            titles[app] = w.window_title or ""
            if getattr(w, "ref", None):
                refs[app] = w.ref            # 后来的覆盖先前的 = 最后一次
        if not dwell:
            audio = snap.get("audio_apps") or []
            return (("background audio: " + audio[0]), None) if audio else ("", None)
        dom = max(dwell.items(), key=lambda kv: kv[1])[0]
        cat = _ambient_cat(dom)
        fn, pj, pg = _ambient_parse_title(dom, titles.get(dom, ""))
        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        act = ""
        if cat == "editor":
            act = "editing code" if (char_n >= 60 and bs_n * 3 >= char_n) else "writing code" if char_n >= 60 else "reading code"
        elif cat == "browser":
            act = "reading a webpage"
        elif cat == "im":
            act = "communicating"
        elif cat == "terminal":
            act = "running commands"
        detail = fn or pg
        line = f"in {dom}"
        if act:
            line += f" {act}"
        if detail:
            line += f" ({detail[:30]})"
        return (line, refs.get(dom))

    def record_ambient_trail(self) -> None:
        """由 app.py 定时器周期调用：把当前现场追加进持久轨迹（Phase 4）。"""
        try:
            from core.proactive import ambient_trail
            line, ref = self._ambient_scene_now()
            if line:
                ambient_trail.append(line, ref)
        except Exception:
            pass

    def _build_suspension_resume_injection(self, records: list) -> str:
        """把待恢复的挂起记录拼成一段注入文本，让模型唤醒时知道自己在等什么。

        参考 Claude Code：状态本身在对话上下文里，这段只是显式提醒"你之前挂起了，
        现在被唤醒，先核对等的事成了没"，避免模型忽略历史里的 wait_for。
        """
        if not records:
            return ""
        lines = ["\n\n[Suspension Resume — Previous Wait State]"]
        for r in records:
            _src = "/".join(r.wake_on)
            lines.append(f"- Waiting for: {r.reason} (wake sources: {_src})")
        lines.append(
            "You have now been resumed. First verify whether the awaited condition is satisfied:\n"
            "- If satisfied, continue the task.\n"
            "- If not satisfied, you may call wait_for again, or briefly tell the user the current state and what is needed.\n"
            "Do not pretend Nano was working in the background while suspended; Nano was paused until this wake event."
        )
        return "\n".join(lines)
