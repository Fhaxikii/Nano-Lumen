# core/orchestrator/_types.py
"""ReAct 主循环与工具执行用的数据结构。"""

from dataclasses import dataclass
from typing import Any, NamedTuple, Optional

from core.orchestrator._runtime import _rt_has_live_work, _rt_live_interactions
from core.schema import ToolCall


@dataclass
class StreamDecisionState:
    """_stream_decision_core 的局部结果容器，避免实例属性在并发场景下被覆盖。"""
    decision: Any = None
    model: str = "UNKNOWN"
    notice: str = ""
    block_started: bool = False
    block_id: Optional[str] = None
    answer_bid: Optional[str] = None


@dataclass
class ToolExecution:
    """单次工具执行结果。"""
    call: ToolCall
    result_text: str
    ok: bool = True
    raw_result: Any = None
    tool_data: Any = None
    error: str = ""


class _ToolRuntimeView:
    """`availability` 判据能看到的运行时事实 —— **只读的窄接口**。

    ⭐ 三个判据都不是新写的，是把**原来散在 `_build_skills_info` 里的三个 if**
       搬到一处：
         · `has_live_work()`        ← `if _rt_has_live_work(self)`（给 `task_boundary`）
         · `is_recheck_round()`     ← `if getattr(self, "_recheck_sid", "")`（给 `set_next_checkin`）
         · `has_open_interaction()` ← 未决交互存在时才注入（给 `answer_open_interaction`）

    📌 判据出处（改造前就写在 `_rt_ongoing_work` 的注释里）：
       **一个工具和它的事实来源，必须由同一个条件控制** ——
       「有工具没事实」或「有事实没工具」两种半截状态都会让**模型开始猜**。
    ⚠️ 三个方法都吞异常返回 False：**算不出来就不给**（多一个工具的代价是模型乱调，
       少一个只是这一轮用不上它）。
    """

    __slots__ = ("_o",)

    def __init__(self, orch):
        self._o = orch

    def has_live_work(self) -> bool:
        try:
            return bool(_rt_has_live_work(self._o))
        except Exception:
            return False

    def is_recheck_round(self) -> bool:
        try:
            return bool(getattr(self._o, "_recheck_sid", ""))
        except Exception:
            return False

    def has_detachable_carrier(self) -> bool:
        """此刻有没有一个**还活着的、可以被交出去/停掉的**慢调用。

        ⭐⭐ [2026-08-22] 它补的是 `is_recheck_round` 够不着的那一半：
           交还发生在一轮的中途，而用户说「把这个放后台」/「把它停掉」
           **必然是下一轮**，那一轮既不是回看轮、也没有中途注入的机会。
        🔴 实测后果（两条）：
           · `dont_wait` 收到「没有交还给你的慢调用」，而那件事明明在跑
           · `stop_background` 压根不在工具表里 → 模型改用 `taskkill` 按**进程名**杀，
             把不相干的同名进程一起杀了
        📌 那条：**一个工具和它的事实来源，必须由同一个条件控制。**
           载体现在能活过一轮了（见 `_handle_query_impl` 里那段），
           工具的出现条件就必须跟着它走 —— 否则又是「有事实没工具」。
        ⚠️ 只认**活着**的：记录点存在不等于对象还在。
        """
        try:
            _c = getattr(self._o, "_detachable_carrier", None) or {}
            _wid = _c.get("wait_id")
            if not _wid:
                return False
            from core.runtime.kernel import get_kernel as _gk_av
            from core.runtime import waitcond as _wc_av
            _r = _wc_av.find_by_id(_gk_av(), _wid)
            return bool(_r is not None and _r.is_live)
        except Exception:
            return False

    def has_open_interaction(self) -> bool:
        try:
            return bool(_rt_live_interactions(self._o))
        except Exception:
            return False

    def has_evicted_history(self) -> bool:
        """这个会话里有没有"已经移出上下文"的交换。

        ⭐ 与 `note_image` 同一条纪律：**一个工具和它的事实来源由同一个条件控制。**
           没有 L3 记录时 system 里一条索引都没有，那时给模型一个
           `recall_conversation`，它只会拿它去猜。
        """
        try:
            from core.context.decay_store import DecayStore, L3
            from core.runtime.kernel import get_kernel
            _sid = self._o.memory.conversation_session_id
            if not _sid:
                return False
            rows = DecayStore(get_kernel().store).active_entries(_sid)
            return any(str(e.get("level")) == L3 and e.get("index_entry")
                       for e in rows.values())
        except Exception:
            return False

    def has_unsummarized_image(self) -> bool:
        """**这一轮**有图、且还没记下来。

        ⚠️⚠️ 读的是**本轮标志** `_turn_image_pending`，**不是 memory 的最后一条**。
           理由（实测栽过两次）写在 `_handle_query_impl` 顶部那段注释里：
           核心工具清单在用户消息进 memory **之前**就算完了，
           而 ReAct 循环里 `storage[-1]` 又会变成 `tool_results`。
           📌 **判据不能挂在「storage 的最后一条是什么」上** ——
              那个位置一轮之内会变好几次，而"这一轮有没有图"只有一个答案。

        ⭐ **同一个判据同时控制三样东西**：`note_image` 在不在工具清单里、
           那段一次性提示注不注入、记完之后两者一起消失。
        📌 那条：**一个工具和它的事实来源，必须由同一个条件控制。**
        ⚠️ 天然一次性：`note_image` 一落，标志翻假。
           用户提醒的「摘要千万不能像 base64 那个 bug 一样每轮注入」——
           防线不是"记得别注入"，是**它没有第二轮可注入**。
        """
        try:
            return bool(getattr(self._o, "_turn_image_pending", False))
        except Exception:
            return False


class ToolOutcome(NamedTuple):
    """工具 handler 的统一返回契约。

    ⭐ AST 实测（提取前）确认：`_execute_one_tool_call` 的 name 分派链**之后**
       只读 `result_text` 一个变量，所以 handler 的完整契约就是
       「产出文本 + 这次算不算失败」。其余（`tool_end` 事件、日志、
       `ToolExecution` 组装、错误分类）一律留在统一 plumbing 里。

    📌 **允许 handler 直接返回 `str`**（绝大多数工具不会失败），
       由 `ToolOutcome.of()` 归一 —— 与项目既有的 `SkillResult.from_raw` 同一范式：
       **一个便利构造器，不是两种并存的契约。**
    """
    text: str
    failed: bool = False

    @staticmethod
    def of(value: Any) -> "ToolOutcome":
        if isinstance(value, ToolOutcome):
            return value
        return ToolOutcome(str(value) if value is not None else "")
