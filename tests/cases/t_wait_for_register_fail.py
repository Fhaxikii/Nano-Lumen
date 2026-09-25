# -*- coding: utf-8 -*-
"""`wait_for` 登记失败时，不许对模型和用户各撒一个谎。

🔴 **旧行为有两个后果，都是假的**（2026-08-12 在 机械提取 handler 时发现）：

  ① 代码里写了一句诚实的「Could not register that wait…」，
     但**随后的 `result_text` 无条件覆盖了它** —— 模型实际收到的是
     「Scheduled a re-check in N seconds…」。**那是一句假陈述**（什么都没登记）。
     📌 与 要修的问题同族：**一个写了但永远不生效的声明**
        （同 `_REACT_SERIAL_TOOLS` 里那 5 个永不生效的 serial）。

  ② 更糟的一半（修的时候才查出来）：它**仍然会发 `suspend_waiting` 事件**，
     而 `suspension_id` 是空串。UI 侧两条路**保护不对称**：
       · `_register_hidden_waiting` 有 `if not suspension_id: return`  ✅
       · `_make_pill_waiting` **没有**                                  ❌
     于是 `scheduled_timer`（用户委托的定时计划）会画出「⏸ 等待中」pill +
     倒计时 + [立即执行][取消计划]，而它注册在**空 key** 上 →
     收尾时用真 id 永远找不到 → **永远转圈的 pill，还带两颗点了没用的按钮**。

⚠️ 它同时违反两条硬判据：
   · **宁可承认"不知道"，也不许替用户编一个用户没做过的动作**（模型侧）
   · **UI 必须是权威状态的忠实投影**（用户侧）—— 没有权威记录就不该有 UI 投影

📌 修法是「立刻返回」而不是「补一个空 id 判断」：
   **不产生那个事件，比让下游各自记得防它更可靠** —— 下游有两条路，
   而它们的保护本来就不一致，那正是这个 bug 能活下来的原因。
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _console  # noqa: F401,E402

import core.orchestrator as orch  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond, label, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}" + (f"   [{detail}]" if detail else ""))
    else:
        _failed.append(label)
        print(f"  !! {label}" + (f"   [{detail}]" if detail else ""))


class _Q:
    """记录 handler 往 UI 发了什么事件。"""
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)

    def put_nowait(self, ev):
        self.events.append(ev)


class _Orch:
    """只借用真实的 `_handle_wait_for`，其余全是替身。"""
    _handle_wait_for = orch.Orchestrator._handle_wait_for


def _run(args, *, open_returns):
    """驱动真实 handler；`_rt_wait_open` 被替换成可控替身。"""
    q = _Q()
    real = orch._rt_wait_open
    orch._rt_wait_open = lambda **kw: open_returns
    try:
        out = asyncio.run(_Orch()._handle_wait_for(args, "aid_1", event_queue=q))
    finally:
        orch._rt_wait_open = real
    return out, q.events


class _Rec:
    wait_id = "wait_ok_1"
    wake_on = ["timer"]
    fire_at = 12345.0


def t_register_failed():
    print("\n[1] ⭐⭐ 登记失败 → 如实说 + 不发 UI 事件")
    out, events = _run({"reason": "看看 CI", "timer_seconds": 60}, open_returns=None)

    check(out.failed is True, "这次调用被标记为失败")
    check("Could not register" in out.text,
          "⭐ 模型收到的是**那句诚实的话**", out.text[:60])
    check("Scheduled" not in out.text,
          "⭐⭐ **不再出现 `Scheduled a re-check…`** —— "
          "🔴 旧行为里那句诚实的话被无条件覆盖，模型拿到的是一句假陈述。"
          "📌 宁可承认『不知道』，也不许替用户编一个用户没做过的动作", out.text[:60])
    check(all(e.get("event") != "suspend_waiting" for e in events),
          "⭐⭐ **一个 `suspend_waiting` 事件都没发** —— "
          "🔴 旧行为会发一个 `suspension_id=''` 的事件，而 `_make_pill_waiting` "
          "没有空 id 保护 → 画出一个注册在空 key 上、**永远收不掉**的等待 pill。"
          "📌 UI 必须是权威状态的忠实投影：没有权威记录就不该有 UI 投影",
          f"共发了 {len(events)} 个事件")


def t_scheduled_plan_failed():
    print("\n[2] 用户委托的定时计划（scheduled_plan）登记失败 —— 同样不许画 pill")
    # ⚠️ 这条单独测：`scheduled_timer` 正是 UI 侧**没有**空 id 保护的那条路，
    #    也就是旧 bug 唯一能真正造出「永远转圈 pill」的入口。
    out, events = _run({"reason": "10 分钟后提醒我", "timer_seconds": 600,
                        "intent": "scheduled_plan"}, open_returns=None)
    check(out.failed is True and "Could not register" in out.text,
          "失败 + 诚实文案")
    check(all(e.get("event") != "suspend_waiting" for e in events),
          "⭐ 这条路也不发事件（它才是能造出僵尸 pill 的那条）")


def t_success_path_unchanged():
    print("\n[3] ⚠️ 回归：登记成功那条路【一个字都没变】")
    out, events = _run({"reason": "看看 CI", "timer_seconds": 60}, open_returns=_Rec())
    check(out.failed is False, "成功 → 不标失败")
    check("Scheduled a re-check in 60 seconds" in out.text,
          "文案照旧", out.text[:56])
    sw = [e for e in events if e.get("event") == "suspend_waiting"]
    check(len(sw) == 1, "照旧发一个 suspend_waiting 事件")
    check(sw and sw[0].get("suspension_id") == "wait_ok_1",
          "⭐ 事件带的是**真实 wait_id**，不是空串", str(sw[0].get("suspension_id")))
    check(sw and sw[0].get("waiting_intent") == "condition_recheck",
          "intent 照旧")

    out2, events2 = _run({"reason": "提醒我", "timer_seconds": 600,
                          "intent": "scheduled_plan"}, open_returns=_Rec())
    sw2 = [e for e in events2 if e.get("event") == "suspend_waiting"]
    check(sw2 and sw2[0].get("waiting_intent") == "scheduled_timer",
          "用户委托的计划仍然走 scheduled_timer（可被用户操纵）")
    check("Scheduled the user's plan" in out2.text, "计划类文案照旧")


def t_missing_timer_unchanged():
    print("\n[4] ⚠️ 回归：缺 timer_seconds 那条路不变")
    out, events = _run({"reason": "等着"}, open_returns=_Rec())
    check(out.failed is True and "needs timer_seconds" in out.text,
          "缺时间 → 失败 + 如实说要什么")
    check(all(e.get("event") != "suspend_waiting" for e in events),
          "也不发事件（它本来就在写库之前退回）")


for _t in (t_register_failed, t_scheduled_plan_failed,
           t_success_path_unchanged, t_missing_timer_unchanged):
    _t()

print("\n" + "=" * 74)
print(f"结果: {_passed} passed, {len(_failed)} failed")
print("=" * 74)
if _failed:
    for f in _failed:
        print("  FAILED:", f)
    sys.exit(1)
