"""「一次交换」—— 阶梯的**单位**。

═══ 为什么它必须先有 ═══

L2 的定义就是「**一次交换**压成一条结论行」。而在这之前，
**「一次交换」在代码里没有任何载体** —— 它被临时算过三遍，每遍形状还不一样：

    memory/manager._truncate_safely    → `user_positions`（切点必须落在 role=="user"）
    app._replay_durable_conversation   → 按 role 分组画气泡
    app._SEARCH_OVERLAY_JS             → 爬到带 `❯` 的祖先（2026-08-14 刚写的第三遍）

📌 **同一个概念被临时算三次，每次略有不同 —— 那就是它该成为一个真实体的信号。**
📌 **没有单位就没有阶梯。**

═══ 🔴 单位是「一次交换」，不是「一段 Task」（2026-08-08 更正）═══

早先的设计写的是「L2 的归属单位是整段 Task」。**那会做错**，追问之后才发现：
Task **跨多个 turn 且是懒创建的** —— 简单问答根本没有 Task。
→ 照原写法，**那些没有 Task 的交换会整个绕过 L1→L2**，而且不会报任何错。

⭐ 而「一次交换」**每次都存在**，它是天然的单位。

═══ ⚠️ 刻意做成【纯函数 + 只读视图】，不是一个有状态的对象 ═══

本项目的设计决定：一次交换**不进 Kernel**（否则 Kernel 变成 UI 状态垃圾桶）。
所以这里没有类、没有注册表、没有生命周期 —— 只有
「给我一串消息，我告诉你它们怎么分组」。

📌 **一个只是「怎么看这堆数据」的概念，不该拥有自己的存储** ——
   存了就要回答「它和消息表哪个是权威」，而那个问题不该存在。
⚠️ 于是它天然免疫了一整类 bug：不会过期、不会和消息表不一致、不需要迁移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# 一次交换里，跟在 user 后面的那些角色。
# ⚠️ 用「不是 user 就归上一轮」而不是列白名单：📌 **列白名单的表会漏**
#    —— 新增一种 role（这个项目已经有 6 种）就得记得回来改，而漏了不报错。
_USER = "user"


def _opens_exchange(m: Any) -> bool:
    """这条消息算不算「用户开口」。

    🔴🔴 **`role == "user"` 一个条件不够**（2026-08-14 回代码核实成立）。

    系统注记（`[System check-in]` / `[System wake-up]` / `[Scheduled plan is now due]`）
    为了让模型读到，**必须**以 `user` 角色进上下文（provider 只认 user/assistant）——
    但**用户从没开过口**。第一版把它们当成了新交换，于是：

        交换边界错位  →  L2 摘错一整段  →  L3 删错一整段
        ⚠️ 而且全程不 crash，不报错。

    ⭐ 判别所需的事实**早就存在**：`visible_to_user`（修一次泄露时加出来的那一列）。
    📌 **一个为 A 问题引入的字段，往往正好是 B 问题缺的那个判据** ——
       但前提是你得想起来它在那儿。

    ⚠️ 系统注记**仍然属于**它所在的那次交换（它占上下文、要算进体积），
       只是**不创建**新交换。📌 「算不算数」和「开不开新段」是两个问题。

    🔴 附带教训：第一版的测试**把这个错误行为钉住了**（断言"系统注记确实开了一次
       新交换"）。📌 **一条断言只能证明"实现和写它时的理解一致"，
       证明不了那个理解是对的。**
    """
    if getattr(m, "role", "") != _USER:
        return False
    # 缺这个属性 = 老数据 / 普通消息 → 当作用户可见（与 `_message_from_payload` 同默认）
    return bool(getattr(m, "visible_to_user", True))


@dataclass(frozen=True)
class Exchange:
    """一次交换 = 用户那句 + 直到下一句用户话之前的全部内容。

    ⚠️ **包含系统注记**（`visible_to_user=False` 的那些）：它们是**模型上下文**
       的一部分，衰减要算上它们的体积。
       📌 「用户看不看得见」和「它占不占上下文」是两个问题 ——
          `visible_to_user` 只回答前一个。
    """
    index: int                       # 在这串消息里的第几次交换（从 0 起）
    start: int                       # 起始下标（闭）
    end: int                         # 结束下标（开）
    messages: list[Any] = field(default_factory=list)

    @property
    def user_message(self):
        """这次交换的用户那句。**可能为 None** —— 见 `split()` 的「前导残段」。

        ⚠️ 判据必须与 `_opens_exchange` **同一个** —— 这里出现过一次不一致：
           改了切分规则却忘了改这里，于是「以系统注记开头的残段」会被认成
           有用户开口。📌 **一个概念只能有一个定义，包括它的每一处消费点。**
        """
        m = self.messages[0] if self.messages else None
        return m if m is not None and _opens_exchange(m) else None

    @property
    def is_orphan(self) -> bool:
        """没有用户开头的那一段（历史被截断后的残留）。"""
        return self.user_message is None

    @property
    def start_ordinal(self) -> int | None:
        """这次交换在落盘账本里的起始 `ordinal` —— **它的稳定身份**。

        ⭐ 身份是**推导**出来的，不是存的：`_conversation_ordinal` 由
           `append_message()`（live）和 `_message_from_payload()`（hydrate）
           两条路都会挂上，所以内存里的消息本来就带着它。
        📌 **视图不需要 id，但挂在视图上的东西需要** ——
           用被引用者已有的稳定标识，而不是给视图发明一个。

        ⚠️ 为什么不用 `message_id`：回代码核实过，`load_messages` 只 SELECT
           `ordinal/role/payload_json/created_at`，**根本没把 message_id 带回内存**。
           用它反而要新增一条身份 plumbing；而 ordinal 在 session 内 append-only、
           `update_message` 不改它、reset 后 session 天然换代。

        ⚠️ 返回 None = 这批消息还没落盘（或是构造出来的测试对象）——
           **那种交换不许进衰减账本**：📌 没有稳定身份的东西不该被记账。
        """
        m = self.messages[0] if self.messages else None
        v = getattr(m, "_conversation_ordinal", None) if m is not None else None
        return int(v) if isinstance(v, int) else None

    @property
    def end_ordinal(self) -> int | None:
        """这次交换覆盖到的最后一个 `ordinal`（闭区间末端）。

        ⚠️ 倒着找第一个有 ordinal 的 —— 末尾可能挂着还没落盘的消息
           （当前这一轮正在进行时）。而那种交换本来就不该衰减
           （**只能衰减已经关闭的 Exchange**）。
        """
        for m in reversed(self.messages):
            v = getattr(m, "_conversation_ordinal", None)
            if isinstance(v, int):
                return int(v)
        return None

    @property
    def has_identity(self) -> bool:
        """能不能进衰减账本。**orphan 段也不行** —— 它没有真正的用户开口，
        而 L2 结论行 / L3 索引条目正是靠那句话写摘要的。"""
        return (not self.is_orphan
                and self.start_ordinal is not None
                and self.end_ordinal is not None)

    def text_len(self) -> int:
        """粗略体积（字符）。⚠️ 只用来比较大小，**不是 token** ——
        真 token 归 `meter.py`，📌 别在这里长出第二套计量。"""
        n = 0
        for m in self.messages:
            c = getattr(m, "content", "")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        n += len(b["text"])
        return n


def split(messages: Sequence[Any]) -> list[Exchange]:
    """把一串消息切成若干次交换。**纯函数，不改入参。**

    ⭐ 规则只有一条：**每遇到一次「用户真的开口」就开一次新交换**
       （见 `_opens_exchange` —— `role=="user"` **且** `visible_to_user`），
       其余一切（assistant / tool_calls / tool_results / tool / model /
       以 user 角色注入的系统注记）都归属于上一次交换。
    📌 「用户开口」是对话的自然边界 —— 而**系统替它说的话不是它开口**。

    ⚠️ **前导残段**：第一条不是 user 时（历史被截断过、或 hydrate 后开头被切），
       会产出一个 `is_orphan=True` 的交换。
       📌 **不许把它并进后面那次交换** —— 它属于一次**已经不完整**的对话，
          并进去会让那次交换的「用户那句」名不副实，
          而下游（L2 结论行、索引条目）正是靠那句话写摘要的。
    """
    out: list[Exchange] = []
    if not messages:
        return out
    start = 0
    for i, m in enumerate(messages):
        if not _opens_exchange(m) or i == 0:
            continue
        out.append(Exchange(len(out), start, i, list(messages[start:i])))
        start = i
    out.append(Exchange(len(out), start, len(messages), list(messages[start:])))
    return out


def user_cut_points(messages: Sequence[Any]) -> list[int]:
    """每次交换的起始下标里，**真正由用户开头**的那些。

    ⭐ 这就是 `_truncate_safely` 原来自己算的 `user_positions` ——
       把它收进来，是为了让「一次交换」这个单位**只有一个定义**。
    📌 **一个概念只要还有第二处实现，它迟早会有第二种含义。**

    ⚠️⚠️ **它现在与旧算法【不再逐位相同】，这是刻意的**：旧算法把系统注记
       也当成切点（见 `_opens_exchange`）。于是截断会切在一条用户从没说过的
       消息上 —— **那本来就是个 bug，只是没人看得见**。
       📌 收编一个旧实现时，如果发现旧的那个是错的，
          **要的是"修正 + 说清差异"，不是"为了对拍绿而把错抄过来"。**
    """
    return [e.start for e in split(messages) if not e.is_orphan]
