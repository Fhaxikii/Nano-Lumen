# core/reading.py
"""迭代阅读 —— 参数、切片、大文件先试读。**全项目唯一一份判据。**

═══ 它解决什么 ═══

`load_full_file` 原来是「一次性工具」：要么整份读进来，要么读不到。
把它重构成「可迭代的阅读能力」——模型自己决定从哪读、读多少，
配合一个随任务失效的 scratchpad（读完一片先记结论、再丢原文），
让**上下文厚度恒定**。

═══ 🔴 单位：寻址按【行】，配额按【字符】 ═══

最初设想的是「字符级位置控制（start_char + length）」，
而实现是**按行**的 `offset`/`limit`。两者都对了一半：

    寻址（模型说从哪开始）  按【行】 ✅ —— 跟 `search_files` 的行号**直接对得上**
                                        字符偏移的话，grep 给的行号模型换算不出来
    配额（系统决定给多少）  按【字符】✅ —— 那才是真实成本

🔴 **为什么行不能当配额**（2026-08-26 实测，跨 11 份真实文档）：
```
docx 英文长文     297 字符/行   ← 一「行」= 一整段
docx 中文报告     170 字符/行
md   技术文档      47 字符/行
pdf  中文通报      49 字符/行
pdf  中文公文      20 字符/行   ← 一「行」= 视觉一行
docx 短规则        18 字符/行
```
**跨度 16 倍。** 同样读 200 行：中文公文 4,000 字符（几乎没读到东西），
英文 docx 59,400 字符（一次撑爆上下文）。
📌 **「行」在不同格式里根本不是同一个单位** —— 它能当坐标，不能当配额。

═══ 参数（5 个）═══

前三个是直接设计出来的：`read_step` / `try_read_length` / `large_file_threshold`。
另外两个是**必须存在、但一开始没被命名**的闸：
  · `MAX_READ_CHARS`  —— 安全兜底：即使模型策略欠佳，系统也不会因单次
    超量加载而崩溃或产生无法接受的成本。
  · `MAX_NOTES_CHARS` —— scratchpad 必须真的小，否则「记结论、丢原文」会退化成
    「原文照抄」，再加上缓存失效，两头不讨好。
    ⚠️ 光靠提示词不行：**提示词能约束形状，约束不了内容**。所以要硬闸。

═══ ⭐ 两条原则看着矛盾，实际按「都对」调和 ═══

    **阅读主权**：模型应能决定「从哪开始读、**读多少**、下一步读哪」
                    ⇕  看起来直接矛盾
    **固定步长**：步长是系统参数，**模型不能修改步长**

📌 两者答的其实是**不同的问题**：
    阅读主权 答「谁有权决定读多少」            → 模型
    固定步长 答「模型不说时用多少」+「有没有上限」 → 系统（默认值 + 兜底）
⇒ 调和后：`limit` 由模型给／不给就用 `READ_STEP_CHARS`／
   但一律不许超过 `MAX_READ_CHARS`（安全兜底）。
⚠️ 按「固定步长」字面执行（模型完全不能指定）会**直接废掉主权和可控性**。
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

# ══════════════════════════════════════════════════════════════════════════
# 五个参数
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️⚠️ **这五个只有 ① 和 ③ 是真的「拍」出来的**，②④⑤ 从它们派生。
#    调 ① 或 ③ 时，另外三个要跟着重新算 —— 📌 别只改一个
#    （同 `USER_HOLD_SEC` 那次：改了常量而写死的派生值没跟着走）。

#: ① 模型没给 `limit` 时，一次读多少**字符**。
#:
#: ⭐ 决定性理由不是成本曲线，是这个（2026-08-26 实测真实样本）：
#:      中文报告   17K 字符 → **1 轮读完**
#:      英文长文   26K      → 2 轮
#:      普通 docx  32K      → 2 轮
#:      大部头文档 526K     → 26 轮
#:    ⇒ 20,000 让**绝大多数真实文档 1~2 轮读完**，迭代阅读只在真正的大部头上启动。
#: ⚠️ 成本上它也站得住：每轮固定开销实测 ≈1.4K token
#:    （system 21,681 字符 + tools 12,438 字符，走 cache_read 0.1x）。
#:    读完那份 526K 的：步长 8K → 66 轮，固定开销占 30%；步长 20K → 26 轮，占 14%。
#: ⭐ **它错了不疼** —— 模型下一轮可以自己给 `limit` 覆盖它（阅读主权）。
READ_STEP_CHARS = 20_000

#: ② 一次调用**最多**给多少字符。安全兜底要的那个闸。
#:
#: ⚠️ 它防的是「模型说 limit=999999」。
#:    太小 → 「阅读主权」是假的（模型说了不算）；
#:    太大 → 兜底形同虚设。
#: 60,000 ≈ 25K token（中文按 2.4 字符/token）—— 一轮吃得下，不至于一次撑爆。
MAX_READ_CHARS = 60_000

#: ③ 试读一次给多少字符。**系统定，不给模型选。**
#:
#: 🔴🔴 **这不是为了省事，是逻辑上没有别的选择** —— 这里有个悖论：
#:    「你不先看一部分、没拿到这个文件的任何上下文信息，
#:      根本无法决策接下来要看多少、要看哪里。所以试读就算给模型决策，
#:      它也是在没有任何线索的情况下纯猜，那还不如定一个固定的。」
#:    📌 **试读是决策的前提，所以它自己不能是决策的产物。**
#: ⚠️ 只有**第一次**（系统强制 offset=0）如此；之后的 peek 模型可以自选位置和长度
#:    —— 那时它已经有线索了，悖论不再成立。
#: ⭐ 定 1,000 而不是 2,000 的理由：有了「可以再 peek 一次补齐」这条路之后，
#:    看不全的代价从「一次 20,000 的精读」降到「一次 1,000 的 peek」，差 20 倍
#:    ⇒ **第一次试读看不全不再致命**。而 2,000 在小一点的文件上会读掉
#:    全文的 84%（实测一份图文混排的 docx），那时试读本身就失去意义了。
#: 💰 成本实测：1,000 字符 ≈ 250(英文) ~ 500(中文) token，
#:    **比每轮的固定开销还小** —— 试读那一轮的钱主要花在「起一轮」上。
PEEK_CHARS = 1_000

#: ④ 文件字符数超过 `READ_STEP_CHARS × 这个倍数` 时，**必须先试读**。
#:
#: ⭐ 下界是**算出来的，不是拍的**：
#:      试读的代价 = 多花 1 轮
#:      试读的收益 = 省下「从头顺序扫到目标」的轮数
#:      文件 L 字符、目标随机落在中间 → 顺序扫期望 L/(2×步长) 轮
#:      划算 ⟺ L/(2×步长) > 1 ⟺ **L > 2 × 步长**
#: 实际取 3 而非 2：顺序扫**还会读进大量无关内容**，那是真金白银。
#: ✅ 对着真实样本验过：526K 的大部头 → 触发；32K 的普通文档 → **不触发**
#:    （它本来 2 轮就读完，试读纯属多花一轮 —— 这正是这个参数存在的意义）。
LARGE_FILE_MULTIPLIER = 3

#: ⑤ scratchpad（`notes`）最多多少字符。
#:
#: 🔴 当初算成本账时**假设它 ≈500 token ≈1,200 字符**。
#:    它要是不小，「记结论、丢原文」会**退化成「原文堆叠」再加缓存失效，两头不讨好**。
#: ⚠️ 只靠提示词说「不许大段拷贝原文」不够 ——
#:    **提示词能约束形状，约束不了内容**。所以这里要有硬闸。
#: 2,000 留了余量，压缩比仍 ≥10×（20,000 原文 → ≤2,000 笔记）。
MAX_NOTES_CHARS = 2_000


def peek_required_chars() -> int:
    """超过这个字符数就必须先试读。**派生值，不单独配置。**"""
    return READ_STEP_CHARS * LARGE_FILE_MULTIPLIER


def needs_peek(total_chars: int) -> bool:
    return total_chars > peek_required_chars()


def clamp_notes(notes: str) -> tuple[str, bool]:
    """截断笔记，返回 `(内容, 是否截断了)`。

    ⚠️ **截断了必须让调用方能说出来** —— 📌 静默截断笔记 = 模型以为自己记住了，
       下一轮发现线索没了，而它不知道为什么。
       （同 `run_command` 那个坑：截断可以接受，不说截断了不行。）
    """
    n = (notes or "").strip()
    if len(n) <= MAX_NOTES_CHARS:
        return n, False
    return n[:MAX_NOTES_CHARS], True


def slice_lines(text: str, *, offset: Optional[int] = None,
                limit: Optional[int] = None,
                budget_chars: int = READ_STEP_CHARS,
                hard_cap_chars: int = MAX_READ_CHARS) -> dict:
    """从第 `offset` 行开始取一段，**行定位、字符封顶**。

    返回 `{body, start, end, total_lines, total_chars, has_more,
           capped_by, next_offset}`。

    ⭐ 三条规则，各答一个不同的问题：
        没给 limit        → 读到**字符预算**用完为止（默认步长）
        给了 limit        → 按行数读（模型决定读多少）
        任何情况          → 不许超过 `hard_cap_chars`（安全兜底）

    ⚠️ 越界**不报错**，钳到边界 + 说清楚：
       📌 模型给的 offset 超了，是它**不知道文件多长**，而那正是本项要修的问题；
          为此报一次错，等于用惩罚回答一个它没法预先知道的问题。
    """
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    start = max(1, int(offset or 1))
    if start > total_lines:
        return {"body": "", "start": start, "end": start,
                "total_lines": total_lines, "total_chars": total_chars,
                "has_more": False, "capped_by": "out_of_range",
                "next_offset": None}

    want_lines = None
    if limit not in (None, ""):
        try:
            want_lines = max(1, int(limit))
        except (TypeError, ValueError):
            want_lines = None

    out: list[str] = []
    used = 0
    capped_by = ""
    i = start - 1
    while i < total_lines:
        ln = lines[i]
        cost = len(ln) + 1
        # ⚠️ 硬闸先判：它比预算和 limit 都高一级（安全兜底）。
        if used + cost > hard_cap_chars and out:
            capped_by = "hard_cap"
            break
        if want_lines is not None:
            if len(out) >= want_lines:
                capped_by = "limit"
                break
        elif used + cost > budget_chars and out:
            capped_by = "budget"
            break
        out.append(ln)
        used += cost
        i += 1
    # ⚠️ 单行就超上限时也要给出去（`and out` 保证至少给一行）——
    #    📌 一行都不给的话，模型拿到空内容却不知道为什么，只会原地重试。
    end = start + len(out) - 1
    has_more = end < total_lines
    return {"body": "\n".join(out), "start": start, "end": end,
            "total_lines": total_lines, "total_chars": total_chars,
            "has_more": has_more, "capped_by": capped_by,
            "next_offset": (end + 1) if has_more else None}


def render(sl: dict, *, peek: bool = False, filename: str = "") -> str:
    """把切片结果渲染成给模型的那段文本。

    ⭐ **全局信息必须每次都给**：总行数 / 总字数 / 当前位置 /
       还剩多少 —— 📌 模型只有知道总长，才谈得上「还剩多少没读」；
       没有它，分片阅读就退化成「读一段、猜一下、再读一段」。
    """
    if sl["capped_by"] == "out_of_range":
        return (f"[Out of range] This file has only {sl['total_lines']:,} lines, "
                f"but offset={sl['start']}. The file is unchanged; read again with a "
                f"different offset.")
    # ⭐ **文件名必须在头里**，两个理由缺一不可：
    #   ① 给模型：多份文件交替读时，「第 5000 行」得知道是哪一份的第 5000 行
    #   ② 给压缩器：`compress_file_reads` 靠它认出「同一个文件的旧切片」——
    #      📌 没有它，改写只能靠「配对的 tool_use 参数」去反查，
    #         而那要求两边永远对得上，多一个假设就多一处会坏的地方。
    head = (f"[{'PEEK' if peek else 'Lines'} {sl['start']:,}-{sl['end']:,} "
            f"of {sl['total_lines']:,} lines / {sl['total_chars']:,} chars"
            + (f" · {filename}" if filename else "") + "]")
    tail = ""
    if sl["has_more"]:
        tail = (f"\n\n[{sl['total_lines'] - sl['end']:,} more lines not read yet. "
                f"To continue: offset={sl['next_offset']}]")
        if sl["capped_by"] == "hard_cap":
            # ⚠️ 说清是**系统**截的，不是文件到头了 —— 两者对下一步的含义不同。
            tail += (f"\n[This read hit the per-read limit of {MAX_READ_CHARS:,} "
                     f"characters and was cut here]")
    return f"{head}\n{'─' * 40}\n{sl['body']}{tail}"
