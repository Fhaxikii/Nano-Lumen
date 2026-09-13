# core/schema.py
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field


# ══════════════════════════════════════════════════════════════════════════
# ReAct 多工具基础数据结构
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    """单次工具调用描述（ReAct 多工具支持）。"""
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    tool_use_id: Optional[str] = None
    index: int = 0


@dataclass
class ToolResultBlock:
    """单个工具执行结果（ReAct 多工具支持）。"""
    name: str
    tool_use_id: str
    content: str
    is_error: bool = False
    raw_result: Any = None
    tool_data: Any = None

    def __post_init__(self):
        # Anthropic tool_result.content 必须是字符串，强制转换防止 API 400
        import json as _json
        if not isinstance(self.content, str):
            self.content = _json.dumps(self.content, ensure_ascii=False, default=str)


class ChatMessage:
    """统一的消息格式模型（Anthropic 格式）。

    role 取值:
      - "user"         : 用户输入
      - "assistant"    : 模型纯文本回复
      - "tool_call"    : 单工具调用（旧格式，向后兼容）
      - "tool"         : 单工具结果（旧格式，向后兼容）
      - "tool_calls"   : ReAct 多工具调用（新格式）
      - "tool_results" : ReAct 多工具结果（新格式）
    """
    def __init__(
        self,
        role: str,
        content: str = "",
        name: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        tool_use_id: Optional[str] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List["ToolCall | Dict[str, Any]"]] = None,
        tool_results: Optional[List["ToolResultBlock | Dict[str, Any]"]] = None,
        visible_to_user: bool = True,
        ui_images: Optional[List[str]] = None,
        image_summary: str = "",
        reply_quote: str = "",
        render_kind: str = "",
    ):
        self.role = role
        self.content = content
        # ⭐⭐⭐ [2026-08-22] **这条记录是「谁」说的 —— 对话里其实有三种参与者。**
        #
        # `role` 只有 user / assistant，因为**provider 只认这两种**。
        # 📌 但**provider 只认两种角色，不代表对话只有两种参与者** ——
        #    把一个下游约束当成账本的模型，就是让它决定它管不着的形状。
        #
        # `render_kind="sys_error"` 记的是**系统事件**：那一刻 API 调用本身失败了
        # （402 欠费 / 熔断 / 网络全挂）。它既不是用户说的，也不是 Nano 说的 ——
        # 那一轮**根本没有到达模型**。
        # ⚠️ 所以它只进**账本**、不进 `storage`（模型上下文）——
        #    见 `MemoryManager.add_ui_only_record()`，那是 `add_system_note()`
        #    的精确镜像（后者只给模型、不给用户）。
        # 📌 一条从没到达模型的消息，不该在它的历史里显示成它说过的话。
        self.render_kind = render_kind
        # ⭐⭐⭐ [2026-08-13] **「模型看到的」和「用户看到的」不是同一份东西。**
        #
        # 🔴 实测：重启 Nano 之后，聊天区里冒出一个 `Koala ❯` 气泡，内容是
        #    `[System check-in] You put this in the background a while ago…`
        #    —— 一整段英文系统提示词，署着用户的名字。
        #
        # 成因：这类系统注记为了让模型读到，必须以 `user` / `assistant` 角色进
        # 上下文（provider 只认这两种）。而上下文是原样落盘的（**对的**，
        # 不然重启后 thinking 签名和工具往返就不合法了），而 UI 重放时
        # **把整本账本当成聊天记录逐条画出来** —— 于是它们全部现形。
        #
        # 📌 **一份账本如果同时被当作「模型上下文」和「用户看过的东西」，
        #    它就必须记下这两者的差别 —— 否则重放必然泄露。**
        # ⚠️ 注意这不是"UI 忘了过滤"：过滤所需的事实**当时根本不存在**，
        #    账本里没有任何一列区分得开这两者。所以修的是账本，不是渲染。
        #
        # 默认 True：老数据（payload 里没这个键）与真正的用户输入都照常显示，
        # 📌 **fail-safe 方向定为「显示」** —— 少显示一句真话（用户以为自己
        # 没说过某句）比多显示一句系统注记更糟，后者难看，前者是记忆错乱。
        self.visible_to_user = bool(visible_to_user)

        # ⭐⭐⭐ 用户这一轮发过的图，**Nano 自己那份的引用**（内容寻址）。
        #
        # 🔴 用户界面上不该消失：即使模型早就不记得一百轮对话之前那张图了，
        #    用户这边仍然要看得见、点得开。
        #
        # ⭐ 它与 `content` 里的 image block **是两件事，故意分开**：
        #      content   → **模型上下文**。随时可以被 `compress_image_blocks`
        #                   压成占位符，那是省 token 的正当手段。
        #      ui_images → **用户的历史**。谁都不许动它。
        # 📌 **上下文可以忘，历史不可以。**
        #    UI 端的图没必要跟模型端强绑定 —— 那是两本不同的账。
        #
        # ⚠️ 与 `visible_to_user` 同一条纪律：**新增一个可选事实，不该改写既有
        #    事实的形状** —— 为空时 payload 里一个字节都不写（见 `_message_payload`）。
        self.ui_images: List[str] = [str(r) for r in (ui_images or []) if r]

        # ⭐⭐ 模型**在看得见像素的那一轮**自己写下的图片描述。
        #
        # ⚠️⚠️ **它不是答案，是参考。** 两个例子说得最清楚：
        #   · 用户只发了一张风景图、什么都没问 → 摘要可以是八百字的构图清单，
        #     但回复**不能**是那八百字，最多"挺漂亮的，你想让我做什么？"
        #   · 图上写着"1+1 等于几"、用户没打字 → 回复应当是"答案是 2"，
        #     **绝不能**是"这是一张白底黑字的图片，上面写着…"
        #   📌 **摘要是给未来的自己看的，不是给现在的用户看的。**
        #
        # ⚠️ 为什么必须在**当轮**写：那一轮的回复不一定描述了图
        #    （用户可能问的是别的），而像素**只有那一轮在**。错过就永远没有了。
        #
        # ⚠️ 它随消息一起落盘，也随消息一起消失 —— **不做任何压缩豁免**。
        #    "让图活过上下文压缩"是被明确否掉的：连文本都随 UI 上下文走了，
        #    专门给一张连界面上都不在的图做持久记忆，**一点必要都没有**。
        self.image_summary: str = str(image_summary or "")

        # ⭐⭐ [2026-08-22 实测] **这条用户消息是在「回复」什么。**
        #
        # 🔴 问题：重启之后，本来是引用回复的消息**变回了普通消息** ——
        #    `↳` 没了、上面那条引用横幅也没了。
        #    成因：引用指向只活在内存里（`_reply_target` → `_reply_target_turn`），
        #    发出去就移交给本轮的 prompt，**从来没跟着消息落过盘**。
        #    于是重放时那个事实压根不存在，渲染只能画成普通消息。
        #
        # 📌 与 `visible_to_user` / `ui_images` **同一条判据**（上面那段）：
        #    **一份账本如果同时被当作「模型上下文」和「用户看过的东西」，
        #    它就必须记下这两者的差别。**
        #    「这句话在回答哪一条」对模型是**当轮**的事（用完即弃），
        #    对用户却是**永久**的事（往上翻还得看得懂）——
        #    ⭐ 两个受众的时效不同，所以必须各存各的，不能共用那个内存指针。
        # ⚠️ 存的是**原文**不是 id：id 指向的东西可能已经被清掉/过期，
        #    而用户要看的就是「我当时引的那句话」。
        #    📌 一个给人看的引用，指向的必须是内容本身，不是一个可能失效的句柄。
        self.reply_quote: str = str(reply_quote or "")[:200]

        self.name = name
        self.args = args or {}
        self.tool_use_id = tool_use_id
        # Anthropic extended thinking + tool_use 必须原样回传 thinking/redacted_thinking blocks。
        # 只保存已转成普通 dict 的 block，不保存 SDK 对象。
        self.thinking_blocks = [
            dict(b) for b in (thinking_blocks or [])
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        ]

        # 多工具调用列表（tool_calls role 用）
        self.tool_calls: List[ToolCall] = []
        for i, tc in enumerate(tool_calls or []):
            if isinstance(tc, ToolCall):
                self.tool_calls.append(tc)
            elif isinstance(tc, dict):
                self.tool_calls.append(ToolCall(
                    name=tc.get("name", ""),
                    args=tc.get("args") or {},
                    tool_use_id=tc.get("tool_use_id"),
                    index=tc.get("index", i),
                ))

        # 多工具结果列表（tool_results role 用）
        self.tool_results: List[ToolResultBlock] = []
        for tr in (tool_results or []):
            if isinstance(tr, ToolResultBlock):
                self.tool_results.append(tr)
            elif isinstance(tr, dict):
                self.tool_results.append(ToolResultBlock(
                    name=tr.get("name", ""),
                    tool_use_id=tr.get("tool_use_id", ""),
                    content=tr.get("content", ""),
                    is_error=bool(tr.get("is_error", False)),
                    raw_result=tr.get("raw_result"),
                    tool_data=tr.get("tool_data"),
                ))

    def to_dict(self) -> Dict[str, Any]:
        """转换为 Anthropic Messages API 格式。"""

        # ── 新格式：多工具调用（ReAct） ──────────────────────────────────
        if self.role == "tool_calls":
            content_blocks = []
            # thinking blocks 必须在所有 tool_use 前面，整批一起
            for b in self.thinking_blocks:
                content_blocks.append(dict(b))
            for tc in self.tool_calls:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.tool_use_id or f"tool_{tc.name}_{tc.index}",
                    "name": tc.name or "unknown_tool",
                    "input": tc.args or {},
                })
            return {"role": "assistant", "content": content_blocks}

        # ── 新格式：多工具结果（ReAct） ──────────────────────────────────
        if self.role == "tool_results":
            blocks = []
            for tr in self.tool_results:
                block: Dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tr.tool_use_id,
                    "content": tr.content,
                }
                if tr.is_error:
                    block["is_error"] = True
                blocks.append(block)
            return {"role": "user", "content": blocks}

        # ── 旧格式：单工具调用（向后兼容） ───────────────────────────────
        if self.role == "tool_call":
            content_blocks = []
            # thinking/redacted_thinking 必须在 tool_use 前面，原样回传，不改写 signature。
            for b in self.thinking_blocks:
                content_blocks.append(dict(b))
            content_blocks.append({
                "type": "tool_use",
                "id": self.tool_use_id or f"tool_{self.name}",
                "name": self.name or "unknown_tool",
                "input": self.args or {},
            })
            return {"role": "assistant", "content": content_blocks}

        # ── 旧格式：单工具结果（向后兼容） ───────────────────────────────
        if self.role == "tool":
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": self.tool_use_id or f"tool_{self.name}",
                    "content": self.content,
                }],
            }

        api_role = "assistant" if self.role in ("assistant", "model") else "user"
        # 多模态：content 为 list 时（图片/文件），直接作为 content blocks 传给 API
        if isinstance(self.content, list):
            return {"role": api_role, "content": self.content}
        return {"role": api_role, "content": self.content}


class BaseSkill:
    """技能基类。

    新生成的 Skill 还应实现 get_spec() 返回 SkillSpec，用于 Plan 编排器
    做上下文级别检查、副作用控制和原子性校验。get_spec() 不实现不会立刻崩,
    但该 Skill 将无法被 Plan 调用(只能走快路径单工具直调)。
    """
    def __init__(self):
        self.name = self.__class__.__name__

    @staticmethod
    def sanitize_format_arg(params: Dict[str, Any]) -> Dict[str, Any]:
        """净化 format 类参数，防止模型传字面量格式字符串。

        问题根源:manifest description 里写了"如 HH:mm"这类示例,
        模型会把示例值当成默认值传进来。strftime("HH:mm") 返回字面量,
        不是真实时间——工具结果就错了,模型还会掩盖这个错误。

        规则:
        - 参数名含 "format"(format/format_string/time_format/date_format 等)
        - 且值不含 '%' → 自动清除(不是 Python strftime 格式)
        - 其他参数不动

        不只处理 'format'，覆盖所有名字里含 'format' 的参数。
        """
        if not params:
            return params
        cleaned = dict(params)
        for key in list(cleaned.keys()):
            if "format" in key.lower() and isinstance(cleaned.get(key), str):
                if "%" not in cleaned[key]:
                    del cleaned[key]
        return cleaned

    def get_manifest(self) -> Dict[str, Any]:
        raise NotImplementedError

    def get_spec(self) -> "SkillSpec":
        """返回 SkillSpec。新 Skill 必须实现。

        基类抛 NotImplementedError 而不是返回默认值——因为默认值会让没声明
        副作用的 Skill 在 Plan 里被当成 readonly 用,这正是要防的事。
        宁可 Plan 执行器拿到异常拒绝调用,也不能给假数据。
        """
        raise NotImplementedError(
            f"Skill '{self.name}' does not implement get_spec(). "
            f"The Plan orchestrator cannot verify its context requirements or side effects, "
            f"so it will refuse to call this Skill inside a Plan. "
            f"Implement get_spec() if this Skill needs Plan support."
        )

    async def run(self, **kwargs) -> Any:
        raise NotImplementedError

    # ── 进度上报（长任务 Skill 用）────────────────────────────────────────
    def report_progress(self, message: str = "", *,
                        progress: float | None = None,
                        total: float | None = None) -> None:
        """报一句「我现在在干什么」。**长任务 Skill 应该调它，短的不用管。**

        ╔══════════════════════════════════════════════════════════════════╗
        ║ 为什么这条要写进 Skill 协议，而不是做成一个运行时机制             ║
        ╚══════════════════════════════════════════════════════════════════╝

        一个 Skill 跑超过 90 秒时，系统会把控制权交回 Nano，并在一段时间后
        **让它回看一眼「那件事现在怎么了」**。而回看的价值完全取决于那一眼
        能看到什么：

          · OS 长命令 → 有 stdout（pip 的百分比、报错都在里面）
          · MCP 调用  → 有协议原生的进度通知
          · 本地 Skill → **只有这个方法**。不调就什么都看不到。

        ⭐⭐ 而这个方法真正的杠杆在于：**Nano 自己写 Skill。**
           所以 Skill 协议**就是给模型的提示词** —— 只要 `report_progress`
           出现在这份契约里，Nano 写长任务 Skill 时自己就会用上。
           📌 **一个由模型自己写的东西，它的协议就是给它的提示词** ——
              这种场景下「把正确做法写进规范」比「加一道运行时强制」有效，
              而且不用为了少数长任务给多数短 Skill 加样板。

        ⚠️ 刻意**不强制**：绝大多数 Skill 秒级返回，强制上报只会制造噪音。
           不报的后果是如实的「系统看不到它的进度」，不比今天差。
           📌 一个「可选」的通道，前提是**缺了它时系统会说实话**。

        用法（三种都可以）::

            self.report_progress("正在解析第 3 个文件")
            self.report_progress("下载中", progress=42, total=100)   # → [42%] 下载中
            self.report_progress(progress=7, total=None)             # → [7]（不编百分比）

        ⚠️ 边界：`await asyncio.to_thread(...)` 里调是通的（上下文会拷贝），
           裸 `threading.Thread` 里调**报不出来**（见 `core/runtime/progress`）。
        ⚠️ 本方法**永不抛异常**，也永不阻塞 —— 报不出去就静默丢弃。
        """
        try:
            from core.runtime import progress as _pb
            _pb.report_here(message, progress=progress, total=total)
        except Exception:
            pass


class SkillResult:
    """Skill 执行结果的内部统一包装。兼容旧 Skill 的 str 返回。

    修复的是：
    - 新增 error 字段:SkillWriter 生成的代码可能用 error= 传错误信息
    - text 字段为空时,from_raw 和 __str__ 会返回 data 的摘要,防止模型拿到空字符串后凭空编造
    """
    def __init__(self, success: bool = True, text: str = "",
                 data: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None):
        self.success = success
        self.data = data or {}
        self.error = error or ""
        # text 优先用传入值;传入为空时:成功则从 data 生成摘要,失败则用 error
        if text:
            self.text = text
        elif not success:
            self.text = error or "Execution failed"
        else:
            # data 非空时生成摘要
            # 优先级:路径字段 > 其他字段(路径对用户最有价值)
            if self.data:
                _PATH_KEYS = {"file_path", "output_path", "artifact_path",
                              "save_path", "dest_path", "target_path", "path"}
                _path_val = next(
                    (str(v) for k, v in self.data.items() if k in _PATH_KEYS and v),
                    None
                )
                if _path_val:
                    self.text = f"Execution successful. File path: {_path_val}"
                else:
                    parts = []
                    for k, v in list(self.data.items())[:3]:
                        parts.append(f"{k}={v}")
                    self.text = "Execution successful. " + ", ".join(parts)
            else:
                self.text = "Execution successful"

    @classmethod
    def from_raw(cls, raw: Any) -> "SkillResult":
        if isinstance(raw, SkillResult):
            return raw
        return cls(success=True, text=str(raw) if raw is not None else "", data={"raw": raw})

    def to_dict(self) -> Dict[str, Any]:
        return {"success": self.success, "text": self.text, "data": self.data, "error": self.error}

    def __str__(self) -> str:
        return self.text


class AgentDecision:
    """AI 决策模型。支持单工具（旧格式兼容）和多工具（ReAct 新格式）。

    decision_type 取值:
      - "text"      : 模型直接输出文字答案，无工具调用
      - "call"      : 单工具调用（tool_calls 长度为 1）
      - "call_many" : 多工具调用（tool_calls 长度 > 1）
      - "web_text"  : 联网搜索后的文字答案
      - "unsupported" / "plan" : 保留旧值
    """
    def __init__(
        self,
        decision_type: str,
        content: str = "",
        name: str = "",
        args: dict = None,
        steps: list = None,
        tool_use_id: Optional[str] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List["ToolCall | Dict[str, Any]"]] = None,
        answer_bid: Optional[str] = None,
        discarded_text: str = "",
        truncated: bool = False,
    ):
        # 响应撞 max_tokens 被截断 → tool_use 的参数是残缺的。
        # 调用方必须能区分"模型写了个空的"和"模型话没说完" ——
        # 实测里两者的表象完全一样（`code` 为空），但成因和修法完全不同。
        self.truncated = bool(truncated)
        self.content = content
        # ⭐ 模型在调工具的**同一次回复里**还输出了正文时，那段正文会被
        # `answer_discard` 从 UI 上撤销，而且不会进 `content`（对主 ReAct 循环
        # 是对的：调工具前的碎话不该留在屏幕上）。
        #
        # 但有一个场景那段正文**就是交付物**：Skill 探索阶段调 `conclude_exploration`
        # 收尾时，正文是它的完整探索结论，参数里的 summary 只是浓缩版。
        # 丢掉正文会让用户看到"长篇结论流式到一半，突然被一句短摘要整段覆盖"
        # （实测），而且历史里也只剩那句短摘要。
        #
        # 所以原样带出来，**放在独立字段而不是 content** —— content 对 "call" 型
        # 决策的既有语义是"没有正文"，塞进去会影响所有读 content 的旧分支。
        self.discarded_text = discarded_text or ""
        self.steps = steps or []
        self.answer_bid = answer_bid
        # Anthropic extended thinking / redacted_thinking blocks
        self.thinking_blocks = [
            dict(b) for b in (thinking_blocks or [])
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        ]

        # 规范化 tool_calls 列表
        normalized: List[ToolCall] = []
        for i, tc in enumerate(tool_calls or []):
            if isinstance(tc, ToolCall):
                normalized.append(tc)
            elif isinstance(tc, dict):
                normalized.append(ToolCall(
                    name=tc.get("name", ""),
                    args=tc.get("args") or {},
                    tool_use_id=tc.get("tool_use_id"),
                    index=tc.get("index", i),
                ))

        # 兼容旧调用: AgentDecision("call", name="X", args={...}, tool_use_id="...")
        if not normalized and name:
            normalized.append(ToolCall(
                name=name,
                args=args or {},
                tool_use_id=tool_use_id,
                index=0,
            ))

        self.tool_calls: List[ToolCall] = normalized

        # 旧字段保留（避免一次性改爆旧代码）
        first = normalized[0] if normalized else None
        self.name = first.name if first else name
        self.args = first.args if first else (args or {})
        self.tool_use_id = first.tool_use_id if first else tool_use_id

        # 规范化 decision_type
        if normalized:
            self.decision_type = "call_many" if len(normalized) > 1 else "call"
        else:
            self.decision_type = decision_type

    @property
    def is_tool_action(self) -> bool:
        """是否是工具调用决策（单工具或多工具）。"""
        return self.decision_type in ("call", "call_many") and bool(self.tool_calls)


# ══════════════════════════════════════════════════════════════════════════
# SkillSpec 协议
# ══════════════════════════════════════════════════════════════════════════
#
# SkillSpec 是原子 Skill 的"合同"。SkillWriter 生成代码前先生成 SkillSpec,
# 通过 hard_validate 硬校验后才允许进入代码生成阶段。
#
# Plan 编排器执行 skill_step 时,依据 SkillSpec 做三件事:
#   1. required_context_level 检查:当前上下文级别不够则自动注入 context_step 升级
#   2. side_effects / permission_level 控制:危险动作进审计/确认
#   3. data_output_keys 约定:Plan 后续步骤通过 $stepN.data.<key> 引用结构化输出
#
# 核心设计原则:
#   - 字段尽量精简,只保留 Plan 执行器真正用得到的
#   - 枚举值集中定义,代码里不允许出现裸字符串
#   - hard_validate 必须包含 side_effects vs permission_level 一致性检查
#     (防止模型声明 readonly 但实际有 file_write,这是最容易被破防的地方)
# ══════════════════════════════════════════════════════════════════════════


# ── 枚举:上下文可靠性等级 ─────────────────────────────────────────────────
class ContextLevel:
    """Skill 对输入上下文可靠性的要求。

    数值越大要求越严。Plan 编排器执行前比较"当前上下文级别 vs required_context_level",
    不够则自动升级(fragment_context → load_full_file,filename → get_file_path)。

    NONE:不需要任何外部上下文(纯计算 Skill,如 AddTwoNumbers)
    FRAGMENT_OK:RAG 片段够用(查具体事实、查关键词)
    GENERATED_OK:上游 Skill 产出的结构化 data 够用(如 GenerateMessage 接 MatchRules 的输出)
    FULL_REQUIRED:必须全文加载(规则提取、完整表格、跨段逻辑)
    FILE_PATH_REQUIRED:必须真实文件磁盘路径(Excel 计算、PDF 转换等程序化处理)
    WEB_REQUIRED:必须实时联网信息
    """
    NONE = "none"
    FRAGMENT_OK = "fragment_ok"
    GENERATED_OK = "generated_ok"
    FULL_REQUIRED = "full_required"
    FILE_PATH_REQUIRED = "file_path_required"
    WEB_REQUIRED = "web_required"

    ALL = {NONE, FRAGMENT_OK, GENERATED_OK, FULL_REQUIRED, FILE_PATH_REQUIRED, WEB_REQUIRED}

    # 数值排序:越大越严。NONE 和 GENERATED_OK 不参与升级链(它们是终态)
    # 升级链:FRAGMENT_OK < FULL_REQUIRED;filename < FILE_PATH_REQUIRED
    _RANK = {
        NONE: 0,
        FRAGMENT_OK: 1,
        GENERATED_OK: 2,
        FULL_REQUIRED: 3,
        FILE_PATH_REQUIRED: 4,
        WEB_REQUIRED: 5,
    }

    @classmethod
    def rank(cls, level: str) -> int:
        return cls._RANK.get(level, -1)

    # ── 小写别名(兼容 SkillWriter 生成的代码习惯) ──────────────────────
    none = NONE
    fragment_ok = FRAGMENT_OK
    generated_ok = GENERATED_OK
    full_required = FULL_REQUIRED
    file_path_required = FILE_PATH_REQUIRED
    web_required = WEB_REQUIRED


# ── 枚举:副作用类型 ─────────────────────────────────────────────────────
class SideEffect:
    """Skill 对外部世界的影响。一个 Skill 可同时有多种副作用。

    NONE:纯只读,无任何副作用(默认期望)
    FILE_READ:读取磁盘文件(注:仅指主动读 Skill 入参之外的文件;读 file_path 入参不算)
    FILE_WRITE:写文件到工作区
    FILE_DELETE:删除文件
    NETWORK:发起网络请求
    EXTERNAL_API:调用第三方 API
    SHELL:执行系统命令
    SEND_MESSAGE:发邮件/IM
    """
    NONE = "none"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    NETWORK = "network"
    EXTERNAL_API = "external_api"
    SHELL = "shell"
    SEND_MESSAGE = "send_message"
    OS_CONTROL = "os_control"   # OS层：鼠标/键盘/窗口/系统设置控制（第四层OS Skill专用）

    ALL = {NONE, FILE_READ, FILE_WRITE, FILE_DELETE, NETWORK,
           EXTERNAL_API, SHELL, SEND_MESSAGE, OS_CONTROL}

    # ── 小写别名 ──────────────────────────────────────────────────────────
    none = NONE
    file_read = FILE_READ
    file_write = FILE_WRITE
    file_delete = FILE_DELETE
    network = NETWORK
    external_api = EXTERNAL_API
    shell = SHELL
    send_message = SEND_MESSAGE
    os_control = OS_CONTROL


# ── 枚举:权限等级 ───────────────────────────────────────────────────────
class PermissionLevel:
    """Skill 所需权限。Plan 执行器据此决定是否进确认/审计流程。

    READONLY:只读,无任何写/网络/外部访问
    WORKSPACE_WRITE:允许写工作区
    NETWORK_ALLOWED:允许联网读取
    EXTERNAL_ACTION:允许调用外部 API 或发消息(有持久影响)
    DANGEROUS:删除/shell/批量修改等高危
    """
    READONLY = "readonly"
    WORKSPACE_WRITE = "workspace_write"
    NETWORK_ALLOWED = "network_allowed"
    EXTERNAL_ACTION = "external_action"
    DANGEROUS = "dangerous"

    ALL = {READONLY, WORKSPACE_WRITE, NETWORK_ALLOWED, EXTERNAL_ACTION, DANGEROUS}

    # ── 小写别名 ──────────────────────────────────────────────────────────
    readonly = READONLY
    workspace_write = WORKSPACE_WRITE
    network_allowed = NETWORK_ALLOWED
    external_action = EXTERNAL_ACTION
    dangerous = DANGEROUS


# ── 枚举:生命周期 ───────────────────────────────────────────────────────
class Lifecycle:
    """Skill 生命周期。

    PERMANENT:常驻 Skill(放 skills/)
    EXPERIMENTAL:实验性 Skill(标记但仍放 skills/)

    注:临时 Skill(temporary)概念已彻底废弃，所有 Skill 统一部署到 skills/。
    """
    PERMANENT = "permanent"
    EXPERIMENTAL = "experimental"

    ALL = {PERMANENT, EXPERIMENTAL}

    # ── 小写别名 ──────────────────────────────────────────────────────────
    permanent = PERMANENT
    experimental = EXPERIMENTAL


# ── 副作用与权限一致性矩阵 ───────────────────────────────────────────────
# 防止模型声明 readonly 但 side_effects 里有 file_write 这种欺骗性声明。
# 规则:某个 side_effect 至少需要的最低权限等级。
#
# Skill 实际权限必须 >= 它所有副作用所需的最低权限。
# 例如:side_effects=[file_write] → 最低需要 WORKSPACE_WRITE
#       声明 permission_level=READONLY → 拒绝
_SIDE_EFFECT_MIN_PERMISSION = {
    SideEffect.NONE:           PermissionLevel.READONLY,
    SideEffect.FILE_READ:      PermissionLevel.READONLY,
    SideEffect.FILE_WRITE:     PermissionLevel.WORKSPACE_WRITE,
    SideEffect.FILE_DELETE:    PermissionLevel.DANGEROUS,
    SideEffect.NETWORK:        PermissionLevel.NETWORK_ALLOWED,
    SideEffect.EXTERNAL_API:   PermissionLevel.EXTERNAL_ACTION,
    SideEffect.SHELL:          PermissionLevel.DANGEROUS,
    SideEffect.SEND_MESSAGE:   PermissionLevel.EXTERNAL_ACTION,
    SideEffect.OS_CONTROL:     PermissionLevel.DANGEROUS,   # OS Skill 走独立执行循环，最严
}

_PERMISSION_RANK = {
    PermissionLevel.READONLY:         0,
    PermissionLevel.WORKSPACE_WRITE:  1,
    PermissionLevel.NETWORK_ALLOWED:  2,
    PermissionLevel.EXTERNAL_ACTION:  3,
    PermissionLevel.DANGEROUS:        4,
}


@dataclass
class InputDef:
    """SkillSpec 中一个输入参数的定义。"""
    name: str
    type: str          # "string" / "integer" / "number" / "boolean" / "object" / "array" / "file_path" / "table_data" / ...
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }


@dataclass
class SkillSpec:
    """原子 Skill 的合同。SkillWriter 生成代码前必须先生成并通过校验。

    Plan 编排器读取此 spec 做:
      - 上下文级别检查(防止 RAG 片段冒充全文驱动高准确性任务)
      - 副作用/权限审计(危险动作进确认流)
      - 数据流串联($stepN.data.<key> 引用)
    """
    # 基本信息
    name: str
    purpose: str                            # 一句话职责

    # 输入输出协议
    required_inputs: List[InputDef] = field(default_factory=list)
    optional_inputs: List[InputDef] = field(default_factory=list)
    data_output_keys: List[str] = field(default_factory=list)  # data dict 必须包含哪些 key

    # 上下文协议

    # 副作用与权限
    side_effects: List[str] = field(default_factory=lambda: [SideEffect.NONE])
    permission_level: str = PermissionLevel.READONLY

    # 边界声明
    not_responsible_for: List[str] = field(default_factory=list)

    # 原子性硬卡死

    # 生命周期
    lifecycle: str = Lifecycle.PERMANENT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "required_inputs": [i.to_dict() for i in self.required_inputs],
            "optional_inputs": [i.to_dict() for i in self.optional_inputs],
            "data_output_keys": list(self.data_output_keys),
            "side_effects": list(self.side_effects),
            "permission_level": self.permission_level,
            "not_responsible_for": list(self.not_responsible_for),
            "lifecycle": self.lifecycle,
        }

    def hard_validate(self) -> tuple[bool, List[str]]:
        """硬校验。任一条不过都拒绝注册该 Skill。

        Returns:
            (ok, errors):errors 是失败原因列表,ok=True 时 errors 为空
        """
        errors: List[str] = []

        # 1. name 检查
        if not self.name or not isinstance(self.name, str):
            errors.append("name must be a non-empty string")
        elif not self.name.isidentifier():
            errors.append(f"name '{self.name}' is not a valid Python identifier")

        # 2. purpose 非空
        if not self.purpose or not self.purpose.strip():
            errors.append("purpose must be non-empty and clearly describe the Skill in one sentence")

        # 🪦 原来这里还校验 `atomic_action` 非空、`required_context_level` 属于枚举。
        #    两个字段都是 **Plan 编排器时代**的产物，而那个编排器早已删除
        #    （orchestrator 里明写着「不会重蹈已删除的多步 Plan 覆辙」）。
        #    ⇒ 消费者没了，生产者留着 —— 每建一次 Skill 都要模型认真填两个
        #      **不产生任何行为差异**的字段，还要为它们付一段提示词的 token。
        #    📌 **一条校验如果校验的是一个没人读的字段，它保证的只是「格式对」，
        #       而格式对不对已经无关紧要了。**

        # 4. side_effects 校验
        if not isinstance(self.side_effects, list) or not self.side_effects:
            errors.append("side_effects must be a non-empty list, even for read-only Skills use ['none']")
        else:
            for se in self.side_effects:
                if se not in SideEffect.ALL:
                    errors.append(f"side_effects contains invalid value '{se}'. Allowed values: {sorted(SideEffect.ALL)}")
            # 'none' 不能跟其他副作用混用
            if SideEffect.NONE in self.side_effects and len(self.side_effects) > 1:
                errors.append("side_effects cannot include 'none' together with other side effects")

        # 5. permission_level 枚举
        if self.permission_level not in PermissionLevel.ALL:
            errors.append(
                f"permission_level '{self.permission_level}' is invalid. "
                f"Allowed values: {sorted(PermissionLevel.ALL)}"
            )

        # 6. 关键安全检查:side_effects 与 permission_level 一致性
        # 防止模型声明 readonly 但代码里偷偷 file_write
        if self.permission_level in PermissionLevel.ALL and \
           all(se in SideEffect.ALL for se in self.side_effects):
            declared_rank = _PERMISSION_RANK[self.permission_level]
            for se in self.side_effects:
                required = _SIDE_EFFECT_MIN_PERMISSION[se]
                required_rank = _PERMISSION_RANK[required]
                if declared_rank < required_rank:
                    errors.append(
                        f"Permission mismatch: side_effects contains '{se}', which requires at least "
                        f"permission_level='{required}', but current permission_level is '{self.permission_level}'"
                    )

        # 7. data_output_keys 非空
        # 即使是纯副作用 Skill(如发邮件),也应至少输出 {"sent": True} 之类供下游引用
        if not self.data_output_keys or not isinstance(self.data_output_keys, list):
            errors.append(
                "data_output_keys must be non-empty. Even a mostly side-effect Skill must declare "
                "at least one data key such as 'success', 'artifact_path', or 'sent_count' for Plan references"
            )
        else:
            for k in self.data_output_keys:
                if not isinstance(k, str) or not k.strip():
                    errors.append(f"data_output_keys contains invalid key: {k!r}")
                elif not k.replace("_", "").isalnum():
                    errors.append(f"data_output_key '{k}' may contain only letters, numbers, and underscores")


        # 9. lifecycle 枚举
        if self.lifecycle not in Lifecycle.ALL:
            errors.append(
                f"lifecycle '{self.lifecycle}' is invalid. Allowed values: {sorted(Lifecycle.ALL)}"
            )

        # 10. required_inputs 元素类型
        for i, inp in enumerate(self.required_inputs):
            if not isinstance(inp, InputDef):
                errors.append(f"required_inputs[{i}] must be an InputDef instance")
            elif not inp.name or not inp.name.isidentifier():
                errors.append(f"required_inputs[{i}].name '{inp.name}' is not a valid identifier")

        for i, inp in enumerate(self.optional_inputs):
            if not isinstance(inp, InputDef):
                errors.append(f"optional_inputs[{i}] must be an InputDef instance")

        return (len(errors) == 0), errors
