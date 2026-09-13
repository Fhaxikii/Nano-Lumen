# core/runtime/scheduler.py
"""
重启后：那几件停在半路的活，要不要重做。

⚠️ **本模块只剩这一件事。** 曾经还有一套「被搁置的动作 → 用户放开电脑 → 提醒」，
   2026-08-22 拆掉了，理由见下面那块墓碑。
"""
from __future__ import annotations

from loguru import logger  # noqa: F401  （本文件目前不打日志）


# ══════════════════════════════════════════════════════════════════════════
# 🪦 这里曾经有一套「被搁置的动作 → 用户放开电脑 → 提醒模型」，2026-08-22 拆掉
# ══════════════════════════════════════════════════════════════════════════
#
# 它做过的事：`os_execute` 需要鼠标键盘、但用户占着电脑，等满 180 秒收场时
# 记一条 `TaskKind.DEFERRED_ACTION`；用户放开后由 blocker provider 自动解除，
# scheduler 把它挂进下一轮的 system guide 提醒模型。
#
# 🔴 **拆掉的理由（两条都成立）：**
#
# ① **它服务的场景，比设计时预估的窄一个数量级。**
#    `_needs_machine` 只对 `click/type_text/hotkey/scroll/drag/move/win_*` 为真
#    （见 `dsl.CONTENDS_FOR_MACHINE`）。`edit_file` / `run_command` / 文件读写
#    **一个都不经过**。所以它只覆盖「Nano 要点鼠标而用户占着电脑」这一条窄缝。
#
# ② **而主线那个场景早就通了，跟它无关。**
#    「那个包装好了，要我接着弄吗」走的是 `dont_wait` + 完成唤醒，而那条路子
#    当初就写清楚了：「变回手头的活」**不需要任何新机制** —— 等待记录一直在，
#    模型回头看时用既有的 `set_next_checkin` 排一次回看就够了。
#    🔴 而这里为一个更窄的同类问题**造了第二套机制**
#       （新 TaskKind + blocker provider + tick + 注入）。
#
# 📌 **判据（当天第二次栽在同一形状上）：一个已经存在的形状，
#    第二次出现时该复用它，而不是造第二条。**
#    第一次是同一天把「提醒」做成了「排进 inbox + 起一轮空 query 的 turn」，
#    而正确做法只是挂进下一轮的 system guide。
#
# ⚠️⚠️ **那两个「调度器落地后登记 continuation」的挂钩仍然是真缺口** ——
#    收场时对用户许的那句「等你用完我再接着做」至今没人兑现。
#    但它**不值得一套新机制**：真要做，就用既有的 `set_next_checkin`
#    在收场时排一次回看（几行），而不是再引入一类 Task。
#    📌 留着这段墓碑，是为了让下一个看到那两个挂钩的人**别再走一遍同样的弯路**。


# ══════════════════════════════════════════════════════════════════════════
# 重启后：那几件停在半路的活，要不要重做
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️ 它**不依赖任何被拆掉的东西**：读的是 `reconcile_on_startup` 终止掉的那批活
#    （`ReconcileReport.interrupted_details`），与「用户占不占着电脑」无关。
#
# ⭐ 实际覆盖的是什么（按生产者查过，不是推的）：
#      · `dont_wait` 出去的长调用 —— **命令行 / MCP / Skill 都走这条**（主力）
#      · Subagent（`Agent · <label>`）
#      · 定时/后台等待（`ensure_conversation_task(reason)`）
#      · GUI 任务（顺带覆盖，不是它服务的对象）
#    ⚠️ 纯对话的 Task `goal_summary` 是空的 → **被过滤掉，不会提**。
#
# ⚠️ 用**新气泡**（走 `_proactive_push`）：这是关闭之后说的新一句话，
#    合并进老气泡会非常奇怪。
#
# ⭐⭐ 为什么是「问」而不是「自动接上」，也不是「什么都不做」：
#   我们真正想分清的是 ①运行中崩溃（该接）②非运行中崩溃 ③正常关闭（都不用管），
#   而它们在本机上**分不开**（见 `reconciler` 里那段留痕）。
#   🔴 第一版的处置是「按关闭=放弃，一律不提」—— 那只是把一个不可靠的推断
#      换成了另一个：**误触了关闭按钮呢？**
#   ⭐ 正解是**不推断**：把事实说出来，让用户答。
#      📌 **温和提醒本身是无害的**（它只是一段话，不是强制继续），
#         所以「问错了」的代价接近零，而「猜错了」会丢掉用户真正想接着做的事。
#         **两边代价不对称时，往代价小的那边倒。**


def startup_resume_notice(details: list[dict]) -> str:
    """重启后要不要问一句 —— 返回给模型的**事实**（英文），没有可问的就空串。

    ⚠️ **返回的是事实，不是文案。** Nano 说出口的那句话由模型自己组织 ——
       气泡里的文字一律不许写死 —— **文字出现在哪里，决定它是不是「Nano 在说话」**。

    ⚠️ **措辞一律说「关闭」，不说「崩溃」**：
       我们分不清是崩溃还是正常关闭，而「崩溃」是一个**更强的断言** ——
       说错了会让用户以为软件坏了。
       📌 两个描述都可能对时，用**断言更弱**的那个。

    ⚠️ 没有带 `goal` 的活**不进这份清单**：只说得出 `task_id` 的提醒，
       对用户等于噪音。📌 与「低于一条就一个字都不注入」同一条纪律 ——
       **一个说不出「是什么」的提醒，不如不提。**
    """
    items = []
    for d in details or ():
        goal = (d.get("goal") or "").strip()
        if not goal:
            continue
        items.append(f"  - {goal}（{d.get('kind') or 'task'}）")
    if not items:
        return ""
    return (
        "[Unfinished work from before the app was closed]\n"
        "The app was closed while you were part-way through the following. "
        "Those runs are over and cannot be continued in-place, but the work itself "
        "may still be wanted:\n"
        + "\n".join(items)
        + "\n\n"
        "Open the conversation by mentioning this and asking whether they want you to "
        "pick it up again. Keep it to one or two short sentences in your own voice. "
        "Say the app was closed - do NOT say it crashed; we cannot tell which it was, "
        "and claiming a crash would worry them over something that may never have happened. "
        "Do not start redoing anything yet: wait for their answer."
    )
