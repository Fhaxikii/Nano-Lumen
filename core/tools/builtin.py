# core/tools/builtin.py
"""内置工具的**唯一一处声明**。

═══ 这份文件替掉了什么 ═══

改造前，一个内置工具的事实散在 **11 处**，漏一处各有各的坏法且**都不报错**。
这份文件把它们收成一条 `ToolDefinition`：

    manifest      ← `core/tools/manifests.py` 的 `_XXX_MANIFEST` 字面量（唯一 schema 权威）
    awareness     ← 原来的 `_BUILTIN_TOOLS_AWARENESS` + `_build_deferred_awareness` 的 `[:28]`
    presentation  ← 原来的 `_tool_action_display` **与** `_describe_decision_for_user` 两张表
    scheduling    ← 原来的 `_REACT_SERIAL_TOOLS` / `_REACT_PARALLEL_SAFE_TOOLS`
    flow          ← 原来的 `_REACT_EXIT_TOOLS`
    preload       ← 原来的 `_CORE_TOOL_NAMES`
    bindings      ← 原来 `_execute_one_tool_call` 的 if/elif 分派 + `_EXPLORATION_DISPATCH_TOOLS`
    availability  ← 原来散在 `_build_skills_info` 里的三个条件注入判断

═══ 录入时逐条核对出来的既有缺口（照抄事实，不照抄缺陷）═══

⚠️ 以下都是**当时就已经不一致**的地方，本文件按「正确的那一侧」录入：

1. **5 个工具的 `serial` 声明是死的**：`create_new_skill` / `update_existing_skill` /
   `manage_existing_skill` / `WriteSkill` / `answer_open_interaction` 同时在
   `_REACT_SERIAL_TOOLS` 与 `_REACT_EXIT_TOOLS` 里，而 `_classify_tool_safety`
   先判 EXIT 就 `return` → 那半个 serial 永远不生效。
   → 本文件录成 **`EXCLUSIVE`**（它们的真实语义是「与任何其他工具同轮出现就整批拒绝」）。
2. **5 个工具没有 awareness**：`cancel_wait` / `load_tools` / `answer_open_interaction` /
   `WriteSkill` / `conclude_exploration` 不在 `_BUILTIN_TOOLS_AWARENESS` 里。
   → 本文件**人工补写**（awareness 是必填项，构造期会拦）。
3. **`cancel_wait` 不在任何调度表里**，靠 `_classify_tool_safety` 的兜底落成 serial ——
   📌 那是**「碰巧对」不是「被声明为对」**，兜底哪天改掉它就静默错了。
   → 本文件显式录成 `SERIAL`。
4. **`os_execute` 的 awareness 被 `[:28]` 截成 `Operate this computer: inspe`**（切在词中间）。
   → 本文件**人工写一句完整的**，且**不列 39 个 action**
     （它们通过 manifest 的 enum 自动进检索文档，`load_tools(query="delete_file")` 照样命中）。

5. 🔴🔴 **最要紧的一条：`_BUILTIN_TOOLS_AWARENESS` 这张表【零使用点】——它是死表。**
   全项目搜下来，生产代码里只有它的**定义**，没有任何读取。
   ⭐ 于是那 20 条**人工写的、质量不错的**一句话描述，**从来没有进过模型的上下文**；
   模型实际看到的一直是 `_build_deferred_awareness` 拿 **manifest description 做 `[:28]`**
   截出来的残句。
   📌 **所以那个根因要重新定性**：不是「截断了一个好描述」，
      而是「**那张好描述的表压根没被接上**」—— 系统里同时存在「写好的正确答案」和
      「实际在用的错误答案」，而**它们互不知道对方存在**。
   ⚠️ 这是「写好但零调用方」的**第三个实例**（前两个：`mcp_client.awareness_lines()`、
      `app.cancel_bg_task()`）—— 📌 **一个写好但没人调的东西，比没写更坏：
      没写时缺口是可见的，写了不接时缺口【看起来已经补上了】。**
   ⭐ 本文件把这张死表的内容**真正接进了唯一权威**：切换之后，
      它们第一次会真的到达模型。

═══ 为什么用工厂函数而不是模块级常量 ═══

manifest（schema）由调用方传进来（`core/tools/manifests.py` 的 `BUILTIN_MANIFESTS`）：
本文件只声明「除 schema 之外的一切」，两边按工具名合起来；缺哪个工具的 manifest 会直接抛错。
"""
from __future__ import annotations

from typing import Any, Mapping

from core.tools.catalog import (
    ALWAYS, Flow, Preload, Presentation, Scheduling, ToolDefinition,
    ToolOrigin, ToolRuntimeView, ToolScope,
    DetailBlock,
    clip,
)


# ── 运行时可用条件（原来散在 `_build_skills_info` 里的三个 if）─────────────
#
# 📌 判据来自 `_rt_ongoing_work` 的注释（改造前就写着）：
#    **一个工具和它的事实来源，必须由同一个条件控制** ——
#    「有工具没事实」或「有事实没工具」两种半截状态都会让**模型开始猜**。
#    例：没有在进行的事却给出 `task_boundary`，它会「找一件不存在的事来结束」。

def _when_live_work(rt: ToolRuntimeView) -> bool:
    return rt.has_live_work()


def _when_open_interaction(rt: ToolRuntimeView) -> bool:
    return rt.has_open_interaction()


def _when_recheck_round(rt: ToolRuntimeView) -> bool:
    return rt.is_recheck_round()


def _when_has_carrier(rt: ToolRuntimeView) -> bool:
    """有一个慢调用**正在跑**：回看轮，或上一轮交还给我们、至今还活着的那条。

    ⭐⭐ 两个来源缺一不可，理由是它们**覆盖不同的时刻**：
       · `is_recheck_round()` —— 「我刚看了它一眼」
       · `has_detachable_carrier()` —— 「它是上一轮交还的，现在还在跑」
    🔴 只有前者时的实际症状：用户下一轮说「把这个放后台」/「把它停掉」，
       工具**不在表里**（`stop_background`），或者在表里但记录点已被清空
       （`dont_wait` → 「没有交还给你的慢调用」）。
    📌 **一个只在「我刚看过」时才给的出口，答不了「用户现在开口了」。**
       而用户开口恰恰是这两个工具最该出现的时刻。
    """
    return rt.is_recheck_round() or rt.has_detachable_carrier()


def _when_has_evicted_history(rt: ToolRuntimeView) -> bool:
    """有交换被衰减出上下文（降到 L3）时，才给"召回"这个出口。

    ⭐ 条件与 `_l3_index_block` 注入索引的条件**是同一件事**：有索引 ⇔ 有工具。
       📌 「有工具没事实」和「有事实没工具」两种半截状态都会让模型开始猜 ——
          而这次正是后者：索引写着"可 recall_conversation"，工具却不在清单里。
    """
    return rt.has_evicted_history()


def _when_image_needs_summary(rt: ToolRuntimeView) -> bool:
    """这轮有图、且还没记下来。

    ⭐ **天然一次性**：摘要一写上，条件立刻为假，工具和那段提示一起消失。
    📌 特别提醒过：「摘要千万不能像 base64 那个 bug 一样每轮注入」——
       这里的防线不是"记得别注入"，是**它根本没有第二轮可注入**。
    """
    return rt.has_unsummarized_image()


# ── 用户可见文案（原 `_tool_action_display` 的 `_display_map`，逐条照抄）────

def _d_mcp_manage(a: dict) -> str:
    """MCP 管理的卡片标题。⚠️ 抽成函数而不是内联 lambda ——
    f-string 里嵌 dict 字面量会撞花括号（第一版就栽在这）。"""
    _op = {"enable": "启用", "disable": "停用",
           "delete": "删除", "retry": "重连"}.get(a.get("operation", ""),
                                                 a.get("operation", ""))
    return f"{_op} MCP：{a.get('server_name', '')}"


def _d_manage(a: dict) -> str:
    _op = a.get("operation", "")
    return f"{ {'delete': '删除', 'disable': '禁用', 'enable': '启用'}.get(_op, _op) } Skill：{a.get('skill_name', '')}"


def _d_task_boundary(a: dict) -> str:
    if a.get("action") == "finish":
        return f"标记这件事{'完成' if a.get('outcome') != 'abandoned' else '放弃'}"
    return f"另开一件事：{a.get('goal', '')}"


def _d_ask_choice(a: dict) -> str:
    _qs = a.get("questions")
    if isinstance(_qs, list) and _qs:
        return f"请用户选择（{len(_qs)} 个）"
    return f"选择：{a.get('question', '')}"


def _d_dont_wait(a: dict) -> str:
    # ⚠️ 卡片上要出现 `next_step` —— 用户该看见的是「它转去做什么了」，
    #    而不是「它调了一个叫 dont_wait 的东西」。
    #    📌 一张工具卡答的是「Nano 在做什么」，不是「哪个函数被调用了」。
    return f"不等它了，先去：{a.get('next_step', '') or '做别的'}"


def _d_set_next_checkin(a: dict) -> str:
    return (f"下次 {a.get('seconds')} 秒后再看一眼" if a.get("seconds")
            else "不再回看，等它自己完成")


# ── 「模型打算做什么」——与卡片文案**不是同一句话** ─────────────────────────
#
# ⚠️ 下面这几个的 intent 在改造前写在 `_describe_decision_for_user` 里，
#    与 `_tool_action_display` 的 card **措辞不同**（card 答「正在做什么」，
#    intent 答「打算做什么」）。早先那张逐字对拍表**只对了 card**，
#    于是这几条 intent 一直在对拍范围之外 —— 切换时逐条跑出来才发现
#    它们会退回 card、悄悄改掉用户看到的话。
# 📌 **一张对拍表漏了什么，切换就会漂什么。** 补齐之后，intent 也一并进对拍表。

def _i_dont_wait(a: dict) -> str:
    return f"不再等那个调用，先去做：{a.get('next_step', '')}"


def _i_set_next_checkin(a: dict) -> str:
    return (f"设定 {a.get('seconds')} 秒后再看一眼那个后台任务" if a.get("seconds")
            else "决定不再回看，等后台任务自己完成")


def _i_task_boundary(a: dict) -> str:
    if a.get("action") == "finish":
        return ("把这件事标记为放弃" if a.get("outcome") == "abandoned"
                else "把这件事标记为完成")
    return f"另开一件事：{a.get('goal', '')}"


def _i_write_skill(a: dict) -> str:
    return (f"生成新 Skill「{a.get('filename', '')}」的代码" if a.get("filename")
            else "生成新 Skill 的代码")


# 主决策作用域里，每个工具由谁执行（= 已从分派链机械提取出的 handler）。
_MAIN = ToolScope.MAIN
# ⭐⭐ Subagent 的作用域。**白名单**：只有下面显式写了 `agent=True`
#    的工具才进得去（见 `D()`）。默认 `False` = 新工具作者什么都不做，
#    Subagent就拿不到它 —— 📌 排除法要求你记得每一个新东西，白名单只要求你
#    记得你想要的那几个；前者的欠账随时间增长，后者不会。
#
# ⚠️⚠️ **这里曾经有一张「明确不授予」的清单，它过期过两次**（2026-08-20 删）：
#      · 它写着「不给 `os_execute` —— 会改真实电脑」，而 v1.50（08-16）
#        已经给了**只读版**（`agent_handler=_handle_os_execute_readonly`）
#      · 它写着Subagent是「只读探索型」，而Subagent的写能力已经给了 `edit_file`
#    📌 **一张写在注释里的名单，会在权威表变了之后原地过期，而且不报错** ——
#       它只是从此开始说谎，然后被下一个人当成判据抄走。
#    ⭐ 所以现在这里**不再复述名单**：谁在Subagent作用域里，只有一个出处 ——
#       各个 `D(...)` 上的 `agent=True`。要看全集就去读那个表。
#
# ⭐ 而**判据**留下来（判据不会过期，名单会）：
#    Subagent的唯一优势是**上下文隔离**，所以给它的工具只该服务于一件事 ——
#    📌 **这件事会产生大量 main agent 不需要看的中间材料，而结论很短。**
#    今天符合的两类：① 批量查找/勘察 ② 确定性、机械性的批量修改。
#    不符合的一律不给，理由逐条写在 `edit_file` 那条声明上方。
_AGENT = ToolScope.AGENT


# ══════════════════════════════════════════════════════════════════════════
# 专属详情渲染器 —— 只给盘点里标 🔴 的那几个
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️ 写专属渲染器的**唯一理由**是「通用兜底把重要的东西埋起来了」：
#    兜底把参数整个塞进一个 JSON 块，于是 `os_execute` 真正要看的那条命令
#    和一堆 `reason` / `risk` / `timeout` 混在一起。
# 📌 **不是所有工具都值得写一个** —— 兜底已经够用的就别写，
#    多一个渲染器就是多一处会跟 handler 漂开的地方。


def _dt_os_execute(a: dict, r) -> list[DetailBlock]:
    """`os_execute`：命令原文单独一块，其余参数照旧。

    🔴 这是整份盘点里**最缺**的一个：改造前用户完全看不到跑了什么命令、
       输出是什么 —— 而 OS 授权弹窗答的是"要不要授权"，不是"它做了什么"。
    """
    out = []
    _cmd = a.get("command") or a.get("script") or a.get("text") or ""
    if _cmd:
        out.append(DetailBlock("命令", clip(str(_cmd)), kind="code", lang="powershell"))
    _act = a.get("action") or ""
    _rest = {k: v for k, v in a.items()
             if k not in ("command", "script", "text") and v not in (None, "", [])}
    if _rest:
        import json as _j
        out.append(DetailBlock(f"动作 {_act}".strip(),
                               clip(_j.dumps(_rest, ensure_ascii=False, indent=2, default=str)),
                               kind="code", lang="json"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("输出（stdout / stderr）" if not _err else "错误",
                               clip(_c), kind="error" if _err else "text"))
    return out


def _intish(v) -> int | None:
    """把模型给的行号取成 int —— **取不到就当没给**。

    ⚠️ 模型时不时会把 `offset` 发成 `"2151"` 甚至 `"2,151"`。
    📌 这里是渲染层：**看不懂一个数字，代价最多是少显示一行坐标**，
       绝不能让它把整张工具卡炸掉。
    """
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _slice_tag(a: dict) -> str:
    """卡片标题上的切片坐标 —— **只有真在切片时才出现**。

    📌 整读（不带 offset）时一个字都不多加：那时「加载文件：X」已经说全了。
    """
    _o = _intish(a.get("offset"))
    return f" · 第 {_o:,} 行起" if _o else ""


def _dt_file(a: dict, r) -> list[DetailBlock]:
    """`load_full_file` / `peek_file` / `get_file_path`：**路径必须显眼**。"""
    out = []
    _f = a.get("filename") or a.get("path") or a.get("file") or ""
    if _f:
        out.append(DetailBlock("文件", str(_f), kind="text"))

    # ⭐⭐ 坐标 + 承接笔记。
    #
    # 📌 迭代阅读把这个工具从「读文件」改成了「读**某一段**、并带着**上一段的
    #    结论**」—— 而卡片语义还停在 V1.10：用户只看得见「加载文件 ✓」，
    #    看不出它读到第几段，更看不出它有没有把理解接上。
    # ⚠️ `notes` 是整套设计里**唯一一处靠模型自觉产出**的东西。把它渲染出来，
    #    「填没填」就成了肉眼可见的事实，而不是只能靠翻日志反推。
    #    （同「点到了 和没点到」那条判据：不可见的机制等于没有机制。）
    _off, _lim = _intish(a.get("offset")), _intish(a.get("limit"))
    if _off or _lim:
        _co = []
        if _off:
            _co.append(f"第 {_off:,} 行起")
        _co.append(f"读 {_lim:,} 行" if _lim else "读到结尾")
        out.append(DetailBlock("本段坐标", " · ".join(_co), kind="text"))

    _notes = str(a.get("notes") or "").strip()
    if _notes:
        out.append(DetailBlock("承接笔记", clip(_notes), kind="text"))

    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("内容" if not _err else "错误", clip(_c),
                               kind="error" if _err else "text"))
    return out


def _dt_query(a: dict, r) -> list[DetailBlock]:
    """检索类：**查询串 + 命中了什么**。"""
    out = []
    _q = a.get("query") or a.get("question") or a.get("keyword") or ""
    if _q:
        out.append(DetailBlock("查询", str(_q), kind="text"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("命中" if not _err else "错误", clip(_c),
                               kind="error" if _err else "text"))
    return out


def _dt_code(a: dict, r) -> list[DetailBlock]:
    """Skill 类：源码用代码块，别塞进 JSON 里。"""
    out = []
    _n = a.get("skill_name") or a.get("name") or ""
    if _n:
        out.append(DetailBlock("Skill", str(_n), kind="text"))
    _src = a.get("code") or a.get("source") or a.get("content") or ""
    if _src:
        out.append(DetailBlock("代码", clip(str(_src)), kind="code", lang="python"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("结果" if not _err else "错误", clip(_c),
                               kind="error" if _err else "text"))
    return out


def _dt_agent(a: dict, r) -> list[DetailBlock]:
    """Subagent的工具卡详情。

    ⭐⭐ **main agent 发给 Subagent 的原始指令必须留着**（这是唯一一处为 Nano 做的微调）：
       Claude Code 那边 agent 一响应，main agent 发的原始指令就从 UI 里没了。
       📌 向 Claude Code 提过的正是这条 —— 「要能看到 main agent 给 agent 的指令」，
          **那个消失是缺陷，不是要照抄的形态**。
    """
    out = []
    if a.get("label"):
        out.append(DetailBlock("任务", str(a["label"]), kind="text"))
    if a.get("instruction"):
        out.append(DetailBlock("本体发给分身的原始指令",
                               clip(str(a["instruction"])), kind="text"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("分身的报告" if not _err else "失败",
                               clip(_c), kind="error" if _err else "text"))
    return out


def _dt_search(a: dict, r) -> list[DetailBlock]:
    """搜索的详情：**搜了什么**放最前面。"""
    out = []
    _q = " / ".join(x for x in (a.get("name_pattern"), a.get("content")) if x)
    if _q:
        out.append(DetailBlock("搜的是", _q, kind="text"))
    if a.get("path"):
        out.append(DetailBlock("范围", str(a["path"]), kind="text"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("命中" if not _err else "失败", clip(_c),
                               kind="error" if _err else "text"))
    return out


def _dt_scratch(a: dict, r) -> list[DetailBlock]:
    """临时代码的详情：**那段代码本身**最重要。

    ⭐⭐ 这是「事后可见性」那一半 —— 与弹窗那一半答的是**两个不同的问题**：
         弹窗（事前）  「我正在授权什么」    · 只在扫出副作用时出现
         工具卡（事后）「刚才到底发生了什么」· **每一次都在，不管有没有副作用**
       📌 无副作用的代码不弹窗，那它唯一的可见处就是这里。
          少了这块，一段没打断用户的代码就**彻底不可见**了。
    ⚠️ 不能靠 `default_detail` 兜底：它把参数当 JSON 显示，
       而 JSON 转义之后的代码（满屏 `
`）**读不了** ——
       📌 「有显示」和「看得懂」是两回事。
    """
    out = []
    if a.get("purpose"):
        out.append(DetailBlock("目的", str(a["purpose"]), kind="text"))
    if a.get("code"):
        out.append(DetailBlock("运行的代码", clip(str(a["code"])),
                               kind="code", lang="python"))
    # 输出：**照 `default_detail` 的取法**，别自己发明。
    # 🔴 第一版只认 `str` 和 `dict`，而账本给的是 `ToolResultBlock` **对象**
    #    （`_ledger_tool_record` 返回的是那条记录本身）——两个分支都没命中，
    #    于是「输出」那块被**静默跳过**：实际运行时那张卡只有「目的」和「运行的代码」，
    #    而结果明明算出来了。
    # 📌 取值当初是照着想象写的，而正确写法就在同文件的 `default_detail` 里
    #    （`getattr(result, "content", None)`）—— **照着现成的来，别自己发明。**
    # ⚠️ 三种形状都收：对象（账本）/ 字符串（handler 直接返回）/ dict（内部结果）。
    _txt = ""
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str):
        _txt = _c
    elif isinstance(r, str):
        _txt = r
    elif isinstance(r, dict):
        _txt = str((r.get("data") or {}).get("output") or r.get("summary") or "")
    if _txt.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("错误" if _err else "输出", clip(_txt),
                               kind="error" if _err else "code"))
    return out


def _dt_edit(a: dict, r) -> list[DetailBlock]:
    """修改的详情：**改了什么**（diff）最重要。

    ⭐ 这正是那条判据：「用户看到的是真正要变什么，而不是一个几千行文件」——
       而当初点出的更大缺陷（工具卡不可展开）已经修掉了，
       所以这里几乎是免费的。
    """
    out = []
    if a.get("path"):
        out.append(DetailBlock("文件", str(a["path"]), kind="text"))
    for _i, _e in enumerate(a.get("edits") or [], 1):
        if not isinstance(_e, dict):
            continue
        out.append(DetailBlock(f"第 {_i} 处 · 原文",
                               clip(str(_e.get("old_text", ""))), kind="code"))
        out.append(DetailBlock(f"第 {_i} 处 · 改成",
                               clip(str(_e.get("new_text", ""))) or "（删除）",
                               kind="code"))
    _c = getattr(r, "content", None) if r is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(r, "is_error", False))
        out.append(DetailBlock("结果" if not _err else "失败", clip(_c),
                               kind="error" if _err else "text"))
    return out


def build_builtin_definitions(manifests: Mapping[str, Any]) -> tuple[ToolDefinition, ...]:
    """产出全部内置工具声明。`manifests` 由调用方传入（键 = 工具名）。

    ⚠️⚠️ **2026-08-13：只剩一个作用域了。**
       这里原本声明两套 binding（`MAIN` + `EXPLORATION`），因为 Skill 创建有一个
       独立的探索子循环。那个子循环已整体拆除（1500 行），于是
       **`EXPLORATION` 作用域不再有任何工具**，`conclude_exploration` 也一并删除。
       它的两项职责搬到了 `create_new_skill` 的必填参数上：
         · 向 SkillWriter 的证据交接 → `handoff_summary`
         · 「还有未决问题就不许进代码生成」 → `open_questions`
       ⭐ 而 `decision` / `target_skill` **刻意没有搬** —— 那两个参数存在的前提
       （探索是封闭作用域、里面没有 `update_existing_skill` 出口）随作用域一起消失了；
       主循环里**选哪个工具本身就是那个 decision**。
       📌 **一个补丁的参数，不要跟着它保护的东西一起搬家 —— 先问那个补丁的前提还在不在。**
    """
    def M(name: str) -> dict:
        m = manifests.get(name)
        if not isinstance(m, dict):
            raise KeyError(f"缺少内置工具 {name!r} 的 manifest")
        return m

    def D(name: str, *, awareness: str, card, intent=None, detail=None,
          scheduling: Scheduling, flow: Flow = Flow.CONTINUE,
          preload: Preload = Preload.DEFERRED,
          handler: str, availability=ALWAYS,
          agent: bool = False, agent_handler: str = "") -> ToolDefinition:
        # ⚠️ 2026-08-13：原来这里还有一个 `expl_handler`（探索作用域的 binding）。
        #    探索子循环已整体拆除（见 `_exit_create_new_skill` 的 docstring），
        #    于是 **EXPLORATION 作用域不再有任何工具**，这个参数连同
        #    `conclude_exploration` 一起删掉。
        #    📌 那条不变量（「manifest ⊆ dispatcher」）也随之失去对象 ——
        #       它守的是"第二个作用域里看得见却执行不了"，而现在没有第二个作用域了。
        # ⭐ `agent=True` 才进Subagent作用域。**默认 False = 默认没有** ——
        #    📌 这就是白名单：新工具作者什么都不做，Subagent就拿不到它。
        _b = {_MAIN: handler}
        if agent_handler:
            # ⭐ Subagent用**另一个** handler（目前只有 `os_execute`：只读版）。
            #    📌 这比「同一个 handler + 一个标志」强的地方在于：
            #       作用域与 handler 的对应关系由**目录**保证，
            #       而不是由每个调用点记得传对参数。
            _b[_AGENT] = agent_handler
        elif agent:
            _b[_AGENT] = handler
        return ToolDefinition(
            name=name, origin=ToolOrigin.BUILTIN, manifest=M(name),
            awareness=awareness,
            # ⭐ `detail` 省略时走 `catalog.default_detail`（参数 + 结果）——
            #    📌 与 `intent` 的缺省方向**相反**：`intent` 缺省是"少写一遍"，
            #       `detail` 缺省是**"不许什么都没有"**。
            #       所以下面只给盘点里标 🔴 的那几个写专属渲染器，
            #       其余 20 个照样有基本透明度。
            presentation=Presentation(card=card, intent=intent, detail=detail),
            scheduling=scheduling, flow=flow, preload=preload,
            bindings=_b,
            availability=availability,
        )

    return (
        # ── 知识库/记忆：只读，可并发 ────────────────────────────────────
        D("list_knowledge_files",
          awareness="list accessible KB files",
          card=lambda a: "查看文件清单",
          intent=lambda a: "查看当前可访问的文件清单",
          scheduling=Scheduling.PARALLEL, preload=Preload.CORE,
          handler="_handle_list_knowledge_files", agent=True),
        # ⭐ 定位类工具。⚠️ **CORE**（常驻，不需要 load_tools）——
        #    📌 一个"我不知道东西在哪"时才用的工具，如果自己也要先被找出来，
        #       那它在最需要它的那一刻不可用。
        # ⭐ `agent=True`：Subagent是**探索型**，找文件正是它的主业。
        # ⭐ 精确修改。⚠️ **CORE 常驻**，理由与 search_files 不同：
        #    📌 让它难被看见，模型就会退回「读整份 → 重新生成 → 覆盖」——
        #       而那条路一旦生成错，用户那份文件就没有中间状态可退回。
        #    ⭐ 所以这里 CORE 不是为了方便，是为了**让危险的那条路不再是默认路**。
        # ⭐⭐ `agent=True`（2026-08-20 Subagent写能力落地）—— **Subagent唯一的写口，
        #    而且刻意只有这一个。**
        #    判据是从「什么时候该派Subagent」倒推的：
        #      · 批量查找 / 勘察 → 一句结论      —— 已有的只读工具够了
        #      · **确定性、机械性的批量修改**    —— 缺的就是这一个
        #    ⭐ 为什么是 `edit_file` 而不是放开 `file_write`：
        #      `edit_file` 锚点匹配「把这几行换掉」，匹配不上就失败、不动文件；
        #      `file_write` 是整份覆盖，一次判断错原文就没了。
        #      📌 **给一个「批量」执行者整份覆写的能力，等于把不可逆放大 N 倍** ——
        #         而覆盖错了退不回去（这正是上面那条 CORE 的理由）。
        #    ⭐ 安全层零改动：`_handle_edit_file` 自己建 dispatcher 走
        #      `_execute_dsl_step`，地板/确认弹窗/审计/路径策略全是 `os_execute`
        #      那一套 —— 兑现它 docstring 里那句「换一个暴露层，
        #      不许换掉它底下的安全层」。
        #    ⚠️ Subagent的确认弹窗多一行 `nano agent · <label>`，**规则一个字不改**
        #      （原则是「本身怎么授权它就怎么授权」）；而那个确认
        #      **不被新用户消息取消**（见 `wait_confirm_or_user_message`）。
        #    🔴 刻意**不给**的，各有理由（不是名单，是判据）：
        #      · `file_delete` / `file_move` —— 批量修改 ≠ 批量删除；不可逆且无预览
        #      · `run_command`  —— floor 3，每条都弹窗；一个批量执行者配一个
        #                          每次都弹的动作 = 纯噪音
        #      · GUI 模拟 / 键鼠 —— **在场语义**：它们的前提是「机器此刻归 Nano」，
        #                          而Subagent是后台的，**它不持有那个租约**
        #      · mini 窗         —— 同上，且它改变用户眼前的东西
        #      · 任务列表        —— 那是 main agent 对**用户**的表达，而 Subagent 没有用户
        #      · Skill 自写      —— 有专门的多步流程 + 审计卡，Subagent会绕过它
        #      · MCP            —— 第三方副作用不可预期，且台账还没落地
        D("edit_file",
          awareness="change specific lines inside an existing file without rewriting the whole file",
          card=lambda a: f"修改文件：{(a.get('path') or '').split(chr(92))[-1]}",
          intent=lambda a: (f"精确修改「{(a.get('path') or '').split(chr(92))[-1]}」的"
                            f"{len(a.get('edits') or [])} 处内容"),
          detail=_dt_edit,
          scheduling=Scheduling.SERIAL,
          preload=Preload.CORE,
          handler="_handle_edit_file", agent=True),

        D("search_files",
          awareness="find files by name or search inside them, recursively, anywhere on disk",
          card=lambda a: ("搜索：" + (a.get("content") or a.get("name_pattern") or
                                     a.get("path") or "")),
          intent=lambda a: (f"在「{a.get('path', '')}」里搜索"
                            + (f"内容「{a.get('content')}」" if a.get("content") else "")
                            + (f"文件名「{a.get('name_pattern')}」" if a.get("name_pattern") else "")),
          detail=_dt_search,
          scheduling=Scheduling.PARALLEL,
          preload=Preload.CORE,
          handler="_handle_search_files", agent=True),

        # ⭐⭐ Ambient 句柄解析 —— **CORE 常驻**（理由同下面的 `peek_file`）。
        D("resolve_ambient_referent",
          awareness="turn what the user was just doing into a real path or URL",
          card=lambda a: f"取回刚才那个：{a.get('at', '')}",
          intent=lambda a: (f"把 {a.get('at', '')} 那一刻在做的东西取成真实位置"
                            if a.get("at") else "把刚才在做的东西取成真实位置"),
          detail=_dt_file,
          scheduling=Scheduling.PARALLEL, preload=Preload.CORE,
          handler="_handle_resolve_ambient_referent"),

        # ⭐ 试读 —— **必须与 `load_full_file` 一样常驻**。
        #    ⚠️ 做成 DEFERRED 的话，模型要先 `load_tools` 才能试读，
        #       而试读的全部意义就是**省轮数** —— 那等于自己把收益抵消掉。
        D("peek_file",
          awareness="cheaply look at a small piece of a file",
          card=lambda a: f"试读文件：{a.get('filename', '')}{_slice_tag(a)}",
          intent=lambda a: (f"先看一眼「{a.get('filename', '')}」里有什么"
                            if a.get("filename") else "先试读一下文件"),
          detail=_dt_file,
          scheduling=Scheduling.SERIAL, preload=Preload.CORE,
          handler="_handle_peek_file"),
        D("load_full_file",
          # ⭐ 2026-08-26 复核：原文 "load a complete KB file" 两处都窄：
          #    它读的**不只是知识库**（绝对路径 v1.47 起就直接放行），
          #    也**不只读完整份**（现在 offset/limit 是常态）。
          #    ⚠️ awareness 进的是 `search_terms`（catalog.py:379），
          #       所以说窄了的代价是 `load_tools` 搜不到它。
          awareness="read a whole file, or one slice of a big one",
          card=lambda a: f"加载文件：{a.get('filename', '')}{_slice_tag(a)}",
          intent=lambda a: (f"完整加载文件「{a.get('filename', '')}」的内容"
                            if a.get("filename") else "完整加载某个文件的内容"),
          detail=_dt_file,
          # ⭐ 常驻（理由见 `os_execute` 那段）。它与 `query_local_knowledge`
          #    是常配对的一对：查到了就要读全文，而后者本来就常驻 ——
          #    📌 一对常配对的工具只常驻一半，另一半照样触发 `load_tools`，
          #       前缀照样断，等于白做。
          preload=Preload.CORE,
          scheduling=Scheduling.PARALLEL,
          handler="_handle_load_full_file", agent=True),
        D("query_local_knowledge",
          awareness="search KB fragments",
          card=lambda a: f"检索知识库：{a.get('query', '')[:30]}",
          intent=lambda a: (f"在本地知识库里查找「{a.get('query', '')}」相关内容"
                            if a.get("query") else "在本地知识库里检索相关内容"),
          scheduling=Scheduling.PARALLEL, preload=Preload.CORE,
          detail=_dt_query,
          handler="_handle_query_local_knowledge", agent=True),
        D("get_file_path",
          awareness="resolve a KB file to a disk path",
          card=lambda a: f"查询路径：{a.get('filename', '')}",
          intent=lambda a: (f"查找文件「{a.get('filename', '')}」的真实路径"
                            if a.get("filename") else "查找某个文件的真实路径"),
          # ⭐ 常驻（同上）。它是「把 KB 文件交给 Skill」的**必经一步**，
          #    漏了它模型只能拿 KB 的假路径去喂 Skill，然后失败。
          preload=Preload.CORE,
          scheduling=Scheduling.PARALLEL,
          detail=_dt_file,
          handler="_handle_get_file_path", agent=True),
        D("recall_working_memory",
          awareness="query cross-session work history",
          card=lambda a: f"查询记忆：{a.get('keyword', '')}",
          intent=lambda a: (f"查询历史操作记录（关键词：{a.get('keyword', '')}）"
                            if a.get("keyword") else "查询历史操作记录"),
          scheduling=Scheduling.PARALLEL, preload=Preload.CORE,
          detail=_dt_query,
          handler="_handle_recall_working_memory", agent=True),
        D("recall_conversation",
          awareness="recall an earlier part of this conversation that was moved out of context",
          card=lambda a: f"回想更早的对话：{a.get('query', '')}",
          intent=lambda a: (f"回想这次聊天更早时关于「{a.get('query', '')}」说过什么"
                            if a.get("query") else "回想这次聊天更早时说过什么"),
          scheduling=Scheduling.PARALLEL, preload=Preload.CORE,
          availability=_when_has_evicted_history,
          detail=_dt_query,
          handler="_handle_recall_conversation", agent=True),
        D("inspect_existing_skill",
          awareness="inspect existing Skill source code",
          card=lambda a: f"查看 Skill：{a.get('skill_name', '')}",
          intent=lambda a: (f"查看已有 Skill「{a.get('skill_name', '')}」的源代码，对比能否复用"
                            if a.get("skill_name") else "查看已有 Skill 的源代码做对比"),
          scheduling=Scheduling.PARALLEL,
          detail=_dt_code,
          handler="_handle_inspect_existing_skill", agent=True),

        # ── 有副作用/需交互：串行 ────────────────────────────────────────
        # ⚠️ 2026-08-25：卡片/意图读的是 **summary_user** —— 那两处是
        #    **给用户看的**，而 `summary_model` 是给模型看的英文摘要。
        #    🔴 改五字段时这里差点漏掉：它还在读旧参数名 `display_text`，
        #       表现是**工具卡上一片空白**（不报错，只是没字）。
        #       📌 改参数名时，「谁在读这个名字」比「谁在写它」更容易漏。
        #    ⚠️ 兜底到 `content`：宁可显示原话，也不要一张空卡。
        D("write_user_note",
          awareness="write durable user memory",
          card=lambda a: f"记住：{a.get('summary_user') or a.get('content', '')}",
          intent=lambda a: (f"记下一条信息：{a.get('summary_user') or a.get('content')}"
                            if (a.get("summary_user") or a.get("content"))
                            else "记下一条信息"),
          scheduling=Scheduling.SERIAL, preload=Preload.CORE,
          handler="_handle_write_user_note"),
        # ⭐ 删记忆 —— **按需加载**：它只在水位提醒之后才用得上，
        #    📌 一个偶发才用的工具进常驻，等于每轮为它付钱。
        D("forget_user_note",
          awareness="delete one saved memory",
          card=lambda a: f"删除记忆 #{a.get('note_id', '?')}",
          intent=lambda a: f"删掉第 {a.get('note_id', '?')} 条记忆",
          scheduling=Scheduling.SERIAL, preload=Preload.DEFERRED,
          handler="_handle_forget_user_note"),
        # ⭐ 临时执行通道 —— **按需加载**，理由与 forget_user_note 同：
        #    偶发才用的工具进常驻等于每轮为它付钱。
        # ⭐⭐ 而 DEFERRED 在这里**还兼着一层防线**：模型必须先 `load_tools`
        #    才拿得到它，而 `load_tools` 的搜索本来就会一起返回匹配的**现成 Skill**
        #    ⇒ 「有现成的就别自己写代码」是**机械保证**的，不是靠提示词纪律。
        #    📌 已定的原则：不要用「给模型加一条纪律」当唯一答案。
        D("run_scratch_code",
          awareness="run a one-off python snippet and get its output",
          card=lambda a: f"跑一段代码：{a.get('purpose', '') or '临时计算'}",
          intent=lambda a: (f"写一段临时代码来{a.get('purpose')}"
                            if a.get("purpose") else "跑一段临时代码"),
          # ⭐⭐ 事后可见性：**每一次都能展开看到那段代码**，不管有没有弹过窗。
          #    📌 无副作用的代码不打断用户，那这里就是它唯一的可见处。
          detail=_dt_scratch,
          scheduling=Scheduling.SERIAL, preload=Preload.DEFERRED,
          handler="_handle_run_scratch_code"),
        D("ask_user_choice",
          awareness="show choice cards to the user",
          card=_d_ask_choice,
          scheduling=Scheduling.SERIAL,
          handler="_handle_ask_user_choice"),
        D("create_task_list",
          awareness="create a visible multi-step task list",
          card=lambda a: f"创建任务：{a.get('title', '')}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_create_task_list"),
        D("update_task_step",
          awareness="update task-list step status",
          card=lambda a: f"更新步骤：{a.get('step_id', '')} → {a.get('status', '')}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_update_task_step"),
        D("render_visual",
          awareness="render inline charts, diagrams, or visuals",
          card=lambda a: f"渲染可视化：{a.get('title', '') or '图形'}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_render_visual"),
        D("set_window_mode",
          awareness="shrink Nano to mini or restore full window",
          card=lambda a: ("缩成小窗，让出屏幕" if a.get("mode") == "mini" else "恢复全屏"),
          scheduling=Scheduling.SERIAL,
          handler="_handle_set_window_mode"),
        D("end_screen_task",
          awareness="end the current screen-operation task (revoke Temp Auto, restore window)",
          card=lambda a: "结束屏幕操作任务",
          scheduling=Scheduling.SERIAL, preload=Preload.CORE,
          handler="_handle_end_screen_task"),
        D("look_at_screen",
          awareness="take a screenshot and understand the screen visually",
          card=lambda a: f"看一眼屏幕：{a.get('purpose', '') or '当前画面'}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_look_at_screen"),

        # ── 图片：当轮记一份 / 之后按需回看 ──────────────────────────
        # ⭐ 两个工具的 preload **刻意不同**，因为它们回答的问题不同：
        #      note_image      条件注入（只在"这轮有图且还没记"时存在）→ 一次性
        #      view_past_image DEFERRED（模型要用时 load_tools 拿）→ 按需
        #    📌 隔离原则：「**回看按需触发** —— 摘要够用就别回看，
        #       **不是提到图片就回看**」。DEFERRED 本身就是那句话的机械实现：
        #       它连出现在清单里都要模型先决定"我需要看图"。
        # ⚠️⚠️ 卡片文案**刻意不说"记"**（2026-08-13 当场否掉「记下这张图」）：
        #    > 「『记下』和『记忆』的记 —— 这句 pill 会导致用户认为『我就发了个图片，
        #    >   它怎么就加入记忆库了？我也没让它记这个啊』。
        #    >   这是完全没必要的给用户的歧义」
        # ⭐ 它对用户的真实含义就是「Nano 在看这张图」；"把描述写下来"是实现细节，
        #    而**卡片是说给用户听的，不是说给实现听的**。
        # ⚠️ 但也别写成"记忆/收藏/保存"那一类 —— 那会承诺一件它没做的事
        #    （图并没有进记忆库，摘要随这条消息一起活一起死）。
        # 📌 与 `view_past_image` 的「回看」形成一对：**查看=当轮，回看=历史。**
        D("note_image",
          awareness="write down what an image the user just sent contains",
          card=lambda a: "查看图片",
          scheduling=Scheduling.SERIAL, preload=Preload.CORE,
          availability=_when_image_needs_summary,
          handler="_handle_note_image"),
        D("view_past_image",
          awareness="look again at an image the user sent earlier",
          card=lambda a: f"回看那张图：{a.get('question', '') or '原图'}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_view_past_image"),

        # ⚠️ 改造前**没有** awareness（不在 `_BUILTIN_TOOLS_AWARENESS` 里）—— 人工补写
        D("load_tools",
          awareness="load the full parameter schema of a capability you can see but cannot call yet",
          card=lambda a: f"加载能力：{a.get('query', '') or '、'.join(a.get('names') or []) or '工具'}",
          scheduling=Scheduling.SERIAL, preload=Preload.CORE,
          handler="_handle_load_tools"),

        D("set_next_checkin",
          awareness="choose when to look at a background job again",
          card=_d_set_next_checkin, intent=_i_set_next_checkin,
          scheduling=Scheduling.SERIAL,
          # ⭐ 只在回看轮出现 —— 不在回看轮里给它，它会去安排一个不存在的对象
          availability=_when_recheck_round,
          handler="_handle_set_next_checkin"),
        D("stop_background",
          awareness="actually stop something that is still running (not just stop "
                    "waiting for it)",
          card=lambda a: f"停止后台：{str(a.get('ref') or '')[:24]}",
          intent=lambda a: "停掉一件还在跑的东西",
          scheduling=Scheduling.SERIAL,
          # ⚠️ 与 `dont_wait` 同一条件：**有在跑的东西时才给**。
          #    📌 没有载体时给出这个工具，模型只会拿它去停一件不存在的事。
          preload=Preload.CORE,
          availability=_when_has_carrier,
          handler="_handle_stop_background"),
        D("dont_wait",
          awareness="stop waiting for a slow call you already started, and go do "
                    "something else while it runs",
          card=_d_dont_wait, intent=_i_dont_wait,
          scheduling=Scheduling.SERIAL,
          # ⭐⭐ **两条互补的出现方式，缺一不可：**
          #   ① 回看轮 —— 这里的 `availability`（「看了一眼，还早，我不等了」）
          #   ② 交还那一刻 —— `_hand_back_long_task` 把 manifest 直接推进
          #      `_pending_loaded_manifests`（本轮中途并入 schema 的既有通道）
          # 📌 `availability` 是**每轮开头**算一次的，接不住「一轮的中途才
          #    出现的对象」—— 所以 ② 不是绕过 ①，是补上它够不着的那一半。
          # ⚠️ `preload=CORE` + 条件 → 非回看轮它**既不常驻、也不进 deferred
          #    感知块**，模型看不见它、也 `load_tools` 不出来。那是刻意的：
          #    没有载体时给出这个工具，模型只会拿它去「不等」一件不存在的事。
          preload=Preload.CORE,
          availability=_when_has_carrier,
          handler="_handle_dont_wait"),
        D("task_boundary",
          awareness="mark a piece of work finished, or start a different one",
          card=_d_task_boundary, intent=_i_task_boundary,
          scheduling=Scheduling.SERIAL,
          # ⭐ 没有在进行的事就不给 —— 否则模型会「找一件不存在的事来结束」
          availability=_when_live_work,
          handler="_handle_task_boundary"),
        # ⚠️ 改造前既**没有** awareness、也**不在任何调度表里**（靠兜底落成 serial）
        #    📌 那是「碰巧对」不是「被声明为对」—— 这里显式写死。
        D("cancel_wait",
          awareness="cancel a pending timed wait when the user says to stop waiting",
          card=lambda a: f"取消等待：{a.get('match', '') or '全部'}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_cancel_wait"),
        # 🔴🔴 **录声明时当场抓到的一处错误**：旧 `_BUILTIN_TOOLS_AWARENESS['wait_for']` 写的是
        #    `suspend until user/timer/background wake-up` —— 而 **`user` 与 `background`
        #    这两个唤醒源早已被删掉**（manifest 早已改成
        #    "Schedule a timer-owned future turn"，只接受 `timer_seconds`）。
        #    ⚠️ 后果具体：**模型读的感知块用的正是那个过期描述**，于是它以为自己能
        #    「等用户」「等后台任务」，传了参数却被守卫拒绝。
        #    📌 **同一个工具、两处描述、一处过期了没人知道** —— 这正是 11 处分散登记的
        #       典型危害，也正是统一注册表要让它结构上不可能的东西。
        D("wait_for",
          awareness="schedule a timed re-check, then end this turn",
          card=lambda a: f"挂起等待：{a.get('reason', '') or '外部状态'}",
          scheduling=Scheduling.SERIAL,
          handler="_handle_wait_for"),

        # ── 操作电脑 ─────────────────────────────────────────────────────
        # ⭐ awareness **人工写全**，不再用 `[:28]` 截断（旧值是 `Operate this computer: inspe`，
        #    切在词中间）。⚠️ **刻意不列 39 个 action** —— 它们通过 manifest 的 enum
        #    自动进入检索文档，`load_tools(query="delete_file")` 照样精准命中。
        #    📌 awareness 答「我有没有这类能力、什么时候该 load」；
        #       schema 答「具体 action 和参数怎么写」。两个层次，不该混。
        # ⭐ Subagent。⚠️ `agent=False`（默认）—— 🔴 **Subagent不能再生Subagent**，
        #    这是防无限递归的那一道；白名单机制让它天然成立，不需要额外的 if。
        D("spawn_agent",
          awareness=("dispatch a context-isolated sub-agent to sweep a lot of files "
                     "or directories and report back one conclusion. Only worth it "
                     "when you need the finding, not the process."),
          card=lambda a: f"派Subagent：{a.get('label') or (a.get('instruction') or '')[:20]}",
          intent=lambda a: (f"派一个Subagent去查「{a.get('label') or (a.get('instruction') or '')[:24]}」"
                            if (a.get('label') or a.get('instruction'))
                            else "派一个Subagent去做一件调查"),
          detail=_dt_agent,
          # ⭐⭐ **PARALLEL**：main agent 一轮里可以同时派几个 Subagent。
          #    ⚠️ 真正的上限在 `Orchestrator._AGENT_MAX_PARALLEL`（信号量），
          #       不在这里 —— 📌 `Scheduling` 答的是「能不能和别人同批」，
          #       **它不是一个数量闸**；把上限写进调度枚举会让两件事混成一件。
          scheduling=Scheduling.PARALLEL,
          handler="_handle_spawn_agent"),

        # ⭐⭐ [2026-08-23] `os_execute` 拆分后，awareness 也要跟着改口径 ——
        #    📌 一行还宣称自己能「use mouse and keyboard」的感知文本，
        #       会让模型 load 错工具，而它拿到的是「没有这个 action」。
        #       **感知块和 manifest 是同一句话的两个长度，不许只改一处。**
        D("os_execute",
          # ⚠️ 感知行的职责是**让模型知道它存在**，细节在 manifest 里 ——
          #    📌 感知块每轮都注入，一行写长 N 个工具就是 N 倍的常驻成本。
          #       （那条「感知块不膨胀」当场就抓住了。）
          awareness=("operate this computer without the screen: files, commands, "
                     "clipboard, apps, system state"),
          card=lambda a: a.get("reason") or a.get("action", "屏幕操作"),
          detail=_dt_os_execute,
          # ⭐⭐⭐ [2026-08-23] **提到 CORE（常驻）** —— 一举两得，两个收益独立成立：
          #
          #   ① **缓存**：缓存前缀是 `tools → system → messages`，tools 在最前、最脆弱。
          #      断点打在「最后一个**无条件**核心工具」上（`_core_stable_n`），
          #      而稳定块必须 **≥ 2048 token** 才会被 Haiku 真正缓存 ——
          #      🔴 实测：拆分前无条件核心只有 6.3K 字符（约 1.6–2.0K token），
          #         **卡在门槛上下**，于是那个断点写了等于没写
          #         （cmd 实测 `cache_read=0`，GLM 复核也指向同一处）。
          #      ⭐ 把这几个常用的搬进来，稳定块撑过线，那个断点才真的生效。
          #   ② **省掉一次纯浪费的往返**（长期看比①更重）：
          #      模型忘了 `load_tools` 就直接调它 → 一次 API 往返只换来一句
          #      「这个工具没加载」。📌 **那是每次都发生的经常性成本，
          #      而 schema 体积是一次性的** —— 频率一高，账就反过来了。
          #
          # ⚠️ 代价已经付过首付：`resolve()` 刻意**不看 `availability`**，
          #    没有对象时给的是「那条待办已经没了」而不是「查无此工具」。
          #    📌 所以常驻化的真实代价只是**一次多余的工具往返**，
          #       不是「模型开始猜」。
          # ⚠️ 但**不是谁都能进** —— 门槛是「高频 **且** 无条件」：
          #    带 `availability` 的工具进出的正是稳定块本身，它们必须留在条件区。
          # ⭐ `os_execute` 是这三个里最该常驻的：使用频率最高，
          #    而且拆掉图形界面那一半之后它只剩 2534 字符（原 3017）。
          preload=Preload.CORE,
          scheduling=Scheduling.SERIAL,
          handler="_handle_os_execute",
          # ⭐⭐ [2026-08-16] Subagent也能用 `os_execute`，但**解析到另一个 handler**
          #    （只读）。这是 `bindings` 的本意：
          #    「不同作用域**真的有不同的 handler 集合**」。
          # 🔴 当初不给Subagent `os_execute`，是因为它当时**全有或全无** ——
          #    给了就是 39 个 action 全给（含 `file_delete` / 点鼠标）。
          #    有了只读闸之后，「只读的 os_execute」第一次成为一个
          #    **可以单独给出去**的东西。
          # ⚠️ 边界是 `is_pre_authorized` 那条铁律划的，不是这里划的：
          #    只读 action 的 floor 全是 1 → **永远不需要授权弹窗**。
          #    跨过这条线（floor≥2）就要先回答「后台执行者的弹窗弹给谁」，
          #    那是Subagent写能力的事。
          agent_handler="_handle_os_execute_readonly"),

        # ⭐⭐⭐ [2026-08-23] 从 `os_execute` 拆出来的图形界面那一半。
        #
        # ⚠️ 它与 `look_at_screen`（眼睛）、`set_window_mode`（mini 窗）**同一簇**：
        #    三个都 DEFERRED，同进同出。拆分之前它们分属两个可见性层级，
        #    而我们又声明了它们必绑定 —— 那正是这次消除的自相矛盾。
        # ⚠️ **同一个 handler**（`_handle_os_execute`）：dispatcher 本来就按 `action`
        #    分派，风险确认层也按 `action` 键（`pre_authorize(action, risk, scope)`）——
        #    📌 拆的是「模型看到几个工具」，**不是**执行链。执行链一行都不用动。
        # ⚠️ **不给Subagent**（无 `agent_handler`）：Subagent那条是「只读 os」，
        #    而 `computer_use` 里 floor≥2 的动作要弹窗，
        #    「后台执行者的弹窗弹给谁」是Subagent写能力的事，不在这一轮。
        D("computer_use",
          awareness=("drive the screen itself: mouse, keyboard, scroll, drag, "
                     "screenshots, window control"),
          card=lambda a: a.get("reason") or a.get("action", "屏幕操作"),
          detail=_dt_os_execute,
          scheduling=Scheduling.SERIAL,
          handler="_handle_os_execute"),

        # ── exit-flow：调用后离开主 ReAct，进入专属状态机 ─────────────────
        #
        # ⭐⭐ `scheduling=EXCLUSIVE` 而**不是** `SERIAL`：真实语义是
        #     「与任何其他工具同轮出现 → 整批一个都不执行、全部写 error、下轮重选」。
        #     🔴 改造前它们同时在 SERIAL 和 EXIT 两张表里，而分类函数先判 EXIT 就 return
        #        → **那半个 serial 声明永远不生效，且不报错**。
        #
        # ⚠️⚠️ **这五个的 binding 指向 `_exit_*` 薄适配器，不是主链那种 `_handle_*`。**
        #    它们与主分派链的 handler 形态不同：主链的 handler 返回文本，
        #    而这五个是 **`async for … yield` 的事件流转发**（会进探索子循环 / 审计流 /
        #    交互续接），外面还包着 EXIT runner 自己的 plumbing
        #    （同轮拒绝 / tool_start·end / 队列排空 / 终端事件前收尾 / defer-to-model）。
        #    📌 **Catalog 只回答"谁处理"；"怎么运行这一类 handler" 是 Runner 的事。**
        #
        # ✅ **那个「签名各异」的边界已经定了：走薄适配器。**
        #    原方案二（让 EXIT runner 保留各自的调用差异）被否，理由只有一条 ——
        #    那些差异是靠 `if exit_call.name == "..."` 分出来的，而**那正是这次要删的
        #    第二份权威**。留着它，runner 里就还有一张按工具名分派的表，
        #    那条 AST 断言也就名存实亡。
        #    📌 **一个「保留现状」的选项，如果它保留的正是要修的问题，它就不是选项。**
        #    每个 `_exit_*` 只做**它那一支原本就在做的事**（构造 `AgentDecision`、
        #    补 requirement、supersede 澄清…），逐字搬过去，行为零变化；
        #    公共 plumbing 一行都不动，仍在 runner 里。
        # ⚠️⚠️ 下面三个的 awareness **刻意是长句**，不要"顺手统一成一行短句"。
        #
        # 🔴 它们在改造前**不走** `_BUILTIN_TOOLS_AWARENESS` 那条短句，而是走
        #    `_AWARENESS_FULL`（`orchestrator.py` 旧 `:4500`）——那条分支取 manifest
        #    description 的**第一段**（到第一个空行为止，上限 240 字符）。
        #    于是模型实际看到的是下面这三段完整的话，不是短句。
        #
        # ⭐ 判据写在旧 `_AWARENESS_FULL` 上方的注释里，逐条仍然成立：
        #    「⑥ 删掉关键词快路径之后，『写个 skill 做 XX』**唯一的入口就是
        #      `create_new_skill`**。而元工具要求模型判断『可复用能力 vs 一次性执行』；
        #      如果它连这个工具是干什么的都看不清，就更容易把一个明确的请求劝退，
        #      而劝退之后用户没有替代通道。」
        #
        # 🔴🔴 **这一条是切换时核出来的一个真实回归**：新声明原本给了短句，
        #    而等价性断言拿新值去对 `_BUILTIN_TOOLS_AWARENESS`（那张**死表**），
        #    对上了 —— 但**模型实际读的从来不是那张表**，是 `_AWARENESS_FULL` 这一段。
        #    📌 **拿一张没人用的表去证明等价，证出来的等价是假的。**
        #       正确的对拍对象是「旧路径**渲染出来**的那一行」，不是旧表里的字面量。
        D("create_new_skill",
          awareness=("Request creation of a new reusable Nano Skill, meaning a durable "
                     "tool/capability for future repeated use. This is a meta-tool: it "
                     "starts the explore-confirm-generate flow and does not write files "
                     "immediately."),
          card=lambda a: f"创建 Skill：{(a.get('requirement', '') or '')[:30]}",
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          handler="_exit_create_new_skill"),
        D("update_existing_skill",
          awareness=("Request modification of an already deployed Nano Skill. This is a "
                     "meta-tool: it starts a confirmation flow and does not modify files "
                     "immediately."),
          card=lambda a: f"修改 Skill：{a.get('skill_name', '')}",
          intent=lambda a: (f"请求修改 Skill「{a.get('skill_name') or '最近相关 Skill'}」："
                            f"{(a.get('change_summary') or '')[:40]}"),
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          handler="_exit_update_existing_skill"),
        D("manage_existing_skill",
          awareness=("Request delete, disable, or enable for an already deployed Nano "
                     "Skill. This is a meta-tool: it starts a second confirmation flow "
                     "and does not change files immediately."),
          card=_d_manage,
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          handler="_exit_manage_existing_skill"),
        # ⭐⭐ 接入一个新 MCP。
        # 🔴 **必须被广告**（DEFERRED 但进感知块）—— 2026-08-28 实际运行中的教训：
        #    第一版忘了登记它，模型看不到这个工具，于是**拿训练数据里的通用做法
        #    去凑** —— 跑去找 `.cursor/mcp.json`，以为自己是 Cursor。
        #    📌 **工具缺席时模型不会说「我不会」，它会用它知道的东西凑一个。**
        #       （同「拿对话历史填空」那次——给不出句柄它就自己编一个。）
        # ⭐ 第 3 步：查 Official MCP Registry。
        #    ⚠️ **不是 EXIT_REACT** —— 它只是一次只读查询，查完模型还要接着
        #    评估候选、可能再查 GitHub、最后才决定要不要 connect_mcp。
        #    📌 管理类动作独占一轮是因为它**改变了世界**；查询没改变任何东西，
        #       让它退出 ReAct 等于把一次搜索变成一次对话往返。
        D("search_mcp_registry",
          awareness="search the official MCP registry for servers by keyword",
          card=lambda a: f"查 MCP 注册表：{(a.get('query') or '')[:20]}",
          intent=lambda a: f"在官方 MCP 注册表里搜「{a.get('query') or ''}」",
          scheduling=Scheduling.SERIAL, flow=Flow.CONTINUE,
          handler="_handle_search_mcp_registry"),
        D("connect_mcp",
          # ⚠️ 感知行**每轮都发** ⇒ 长一句就乘以轮数（测试卡 90 字符）
          awareness="install a new MCP server from its config JSON (user approves first)",
          card=lambda a: "请求接入 MCP",
          intent=lambda a: (a.get("purpose_line") or "接入一个新的 MCP 服务"),
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          handler="_exit_connect_mcp"),
        # ⭐ MCP 管理 —— 与 `manage_existing_skill` 同一个形状：
        #    EXCLUSIVE + EXIT_REACT（管理类操作要独占一轮并退出 ReAct）。
        # ⚠️ **DEFERRED（默认），不进 CORE** —— 📌 常驻要付每一轮的钱，
        #    而管理 MCP 是极低频动作（可能一年也用不到几次）。
        #    模型需要时 `load_tools` 就能拿到它，而 awareness 一行足够它想起来。
        D("manage_mcp",
          awareness="enable, disable, delete or reconnect an already configured MCP server",
          card=_d_mcp_manage,
          intent=lambda a: (f"对 MCP 服务「{a.get('server_name','')}」执行"
                            f"{a.get('operation','')}"),
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          handler="_exit_manage_mcp"),
        # ⚠️ 改造前**没有** awareness —— 人工补写
        D("answer_open_interaction",
          awareness="record the user's answer to a pending question, or cancel that question",
          card=lambda a: f"回应待办：{(a.get('answer_verbatim') or '')[:24]}",
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          preload=Preload.CORE,
          # ⭐ 只有存在未决交互时才出现（改造前就是条件注入，见 orchestrator 注释）
          availability=_when_open_interaction,
          handler="_exit_answer_open_interaction"),
        D("WriteSkill",
          awareness="write the actual Skill source code (used inside the Skill-writing flow)",
          card=lambda a: f"写 Skill 代码：{a.get('filename', '')}",
          intent=_i_write_skill,
          scheduling=Scheduling.EXCLUSIVE, flow=Flow.EXIT_REACT,
          # ⭐⭐ HIDDEN：主循环**处理得了它，但从不告诉模型它存在**。
          #    改造前这个状态是靠「三个 `_build_skills_info` 调用点全都传
          #    `include_write=False`」**隐式**成立的 —— 一个靠"所有调用方碰巧
          #    都关掉了"维持的性质，加一个新调用点就会破。现在它是一个字段。
          #    ⚠️ 落进 DEFERRED 会让模型能 `load_tools` 出它、进而绕开
          #       「探索→确认→SkillSpec→生成」直接出草稿 —— 那是放开一条新路，
          #       不是切权威。理由详见 `Preload.HIDDEN` 的文档。
          preload=Preload.HIDDEN,
          handler="_exit_write_skill"),

    )
