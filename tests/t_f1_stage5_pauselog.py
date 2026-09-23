# -*- coding: utf-8 -*-
"""挂起期一次性日志。

═══════════════════════════════════════════════════════════════════════════
这个文件重点测什么
═══════════════════════════════════════════════════════════════════════════
不是"字段存没存对"，而是**那几条判据有没有在代码里真的成立**：

* 摘要体积**与窗口数成正比、与事件数无关** —— 直接拿 3000 条事件去撞。
* 环形缓冲**只丢头、绝不丢尾** —— 尾部才是决定现状的那一段。
* 日志**自陈完整性**，但同时明说"尾部完整"（不给模型畏手畏脚的理由）。
* 🔴 **空日志 ≠ 什么都没发生** —— 传感器死了要说出来并要求截图。
* 记录范围（`CONSEQUENTIAL`，含焦点）比接管判定（`TAKEOVER_TRIGGERS`，不含焦点）**宽**。
* ⚠️ 恢复文案**不许**把"核实/截图"写成无条件义务（那是被早先的设计纠正过的老路）。

用法：
  py -3.10 tests	_f1_stage5_pauselog.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.proactive import takeover, takeover_log as tl

PASS = FAIL = 0
_FAILED = []


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        _FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


def sec(t):
    print(f"\n{'=' * 66}\n{t}\n{'=' * 66}")


# ══════════════════════════════════════════════════════════════════════════
sec("① 录制生命周期")

tl._reset_for_test()
ck("初始不在录", not tl.is_recording())
tl.note("input", hwnd=1)
ck("没在录时 note 直接丢", tl.finish() is None)

tl.start(1234, "记事本")
ck("start 后在录", tl.is_recording())
s = tl.finish()
ck("finish 拿到 session", s is not None and s.target_hwnd == 1234)
ck("finish 取完即清（一次性）", not tl.is_recording())

# ⭐ 幂等：连点会触发多次接管/续期，但那是**同一段**挂起。
tl._reset_for_test()
tl.start(1, "a")
tl.note("input", hwnd=1)
t0 = tl._cur.started_at
tl.start(2, "b")          # 第二次不许重开
ck("重复 start 不重开（起录时刻不动）", tl._cur.started_at == t0)
ck("重复 start 不丢已有证据", len(tl._cur.events) == 1)
ck("重复 start 不改已设的目标窗口", tl._cur.target_hwnd == 1)

tl._reset_for_test()
tl.start(0, "")           # 首次接管时可能还没绑定
tl.start(77, "later")
ck("目标窗口原为空时可被补上", tl._cur.target_hwnd == 77)
tl._reset_for_test()


# ══════════════════════════════════════════════════════════════════════════
sec("② 环形缓冲：只丢头，绝不丢尾")

tl._reset_for_test()
tl.start(9, "target")
for i in range(tl.MAX_EVENTS + 250):
    tl.note("input", hwnd=9, detail=f"e{i}")
s = tl.finish()
ck("上限生效", len(s.events) == tl.MAX_EVENTS, f"got {len(s.events)}")
ck("丢弃计数正确", s.dropped == 250, f"got {s.dropped}")
ck("⭐ 最后一条是最新的（尾部没丢）",
   s.events[-1].detail == f"e{tl.MAX_EVENTS + 249}", s.events[-1].detail)
ck("⭐ 第一条是被截断之后的（丢的是头）",
   s.events[0].detail == "e250", s.events[0].detail)
ck("first_kept_at 被更新（用于自陈覆盖区间）", s.first_kept_at >= s.started_at)


# ══════════════════════════════════════════════════════════════════════════
sec("③ ⭐⭐ 摘要体积与「窗口数」成正比，与「事件数」无关")

tl._reset_for_test()
tl.start(100, "我的记事本")
for i in range(3000):                    # 打字 3000 下
    tl.note("input", hwnd=100, title="我的记事本")
for i in range(400):                     # 拖窗上千条 → 只该留一个标记
    tl.note("moved", hwnd=100, title="我的记事本")
s = tl.finish()
out = tl.summarize(s)
ck("3400 条事件的摘要仍然很短（< 1500 字符）", len(out) < 1500, f"len={len(out)}")
ck("摘要里没有逐条事件", out.count("hwnd=100") <= 3, out.count("hwnd=100"))

# 而窗口一多，摘要才该变长
tl._reset_for_test()
tl.start(100, "我的记事本")
for h in range(200, 240):
    tl.note("created", hwnd=h, title=f"w{h}")
    tl.note("focus", hwnd=h, title=f"w{h}")
s2 = tl.finish()
out2 = tl.summarize(s2)
ck("40 个窗口时摘要提到了数量", "40 other window" in out2, out2[:200])
ck("⭐ 40 个窗口也没有把 40 行铺出来", out2.count("w2") <= 6, out2.count("w2"))


# ══════════════════════════════════════════════════════════════════════════
sec("④ 以「我的目标窗口」为中心")

tl._reset_for_test()
tl.start(500, "某用户的记事本")
tl.note("input", hwnd=500, title="某用户的记事本")
tl.note("input", hwnd=500, title="某用户的记事本")
out = tl.summarize(tl.finish())
ck("点出目标窗口", "YOUR TARGET WINDOW" in out)
ck("hwnd 在摘要里（标题不足以区分两个无名记事本）", "hwnd=500" in out)
ck("⭐ 说清「内容可能变了」而不只是「用户打了字」",
   "CONTENT may have changed" in out, out)
ck("报出次数（模型才能对上'那是不是我要清空的那个'）", "2 time" in out, out)

tl._reset_for_test()
tl.start(500, "我的窗口")
tl.note("focus", hwnd=600, title="别人的窗口")
out = tl.summarize(tl.finish())
ck("目标窗口没被碰 → 明确说 nothing happened",
   "nothing happened to it" in out, out)

# ⭐⭐ 终态 vs 期间变更 —— **这一组是实测探针抓出来的**，单元测试原来测不到。
# 原实现平铺成一个列表，输出 `CLOSED; MINIMIZED; restored; MOVED`：
# 时序乱了，而且 minimized 与 restored 同时出现时分不出终态。
tl._reset_for_test()
tl.start(500, "我的")
tl.note("moved", hwnd=500)
tl.note("minimized", hwnd=500)
tl.note("restored", hwnd=500)
tl.note("moved", hwnd=500)
tl.note("destroyed", hwnd=500)
out = tl.summarize(tl.finish())
ck("⭐ 终态单独说，且按**最后一条**事件定（关掉了）",
   "now GONE" in out, out)
ck("⭐ 期间变更分到另一行（不与终态混在一个列表里）",
   "During the pause:" in out, out)
ck("坐标失效仍然报出", "coordinates are stale" in out, out)
ck("最小化后又还原 → 说成「minimized and then restored」，不是两个并列状态",
   "minimized and then restored" in out, out)

tl._reset_for_test()
tl.start(500, "我的")
tl.note("moved", hwnd=500)
tl.note("minimized", hwnd=500)
out = tl.summarize(tl.finish())
ck("⭐ 终态是最小化时如实说 MINIMIZED", "now MINIMIZED" in out, out)
ck("终态已是最小化 → 不再重复说「又还原了」",
   "minimized and then restored" not in out, out)
ck("⚠️ 而且不许还说 GONE（那是另一个终态）", "GONE" not in out, out)

# ⭐ 目标窗口被关掉必须无条件留 —— 它可能整段挂起里什么都没发生、最后被关。
tl._reset_for_test()
tl.start(500, "我的")
tl.note("destroyed", hwnd=500)
out = tl.summarize(tl.finish())
ck("⭐⭐ 目标窗口的 destroyed 无条件保留（最要紧的一条）",
   "now GONE" in out, out)

# ⚠️ 而没见过的窗口的 destroyed 要丢：那些是隐藏辅助窗口的销毁噪音
#    （上游按「可见 + 有标题」拦了它们的 created，这里对齐）。
tl._reset_for_test()
tl.start(500, "我的")
tl.note("destroyed", hwnd=88888)
s = tl.finish()
ck("没见过的 hwnd 的 destroyed 被丢掉（对齐上游的 created 过滤）",
   len(s.events) == 0, s.events)

tl._reset_for_test()
tl.start(500, "我的")
tl.note("focus", hwnd=88888, title="见过它")
tl.note("destroyed", hwnd=88888)
s = tl.finish()
ck("见过的 hwnd 的 destroyed 保留", len(s.events) == 2, s.events)

tl._reset_for_test()
tl.start(0, "")
tl.note("input", hwnd=1)
out = tl.summarize(tl.finish())
ck("没有绑定目标时如实说没有", "no bound target window" in out, out)


# ══════════════════════════════════════════════════════════════════════════
sec("⑤ 焦点序列：压重复、报终态")

tl._reset_for_test()
tl.start(1, "target")
for _ in range(5):                # A↔B 反复 5 次 = 10 跳
    tl.note("focus", hwnd=10, title="A")
    tl.note("focus", hwnd=11, title="B")
out = tl.summarize(tl.finish())
ck("⭐ 报出终态（Nano 面对的是终态）", "Foreground ENDED on" in out, out)
ck("终态是最后那个", "ENDED on: hwnd=11" in out, out)
ck("路线没有铺 10 跳", out.count("→") <= 4, out.count("→"))

tl._reset_for_test()
tl.start(1, "target")
tl.note("focus", hwnd=10, title="A")
tl.note("focus", hwnd=10, title="A")     # 连续重复
tl.note("focus", hwnd=11, title="B")
s = tl.finish()
out = tl.summarize(s)
ck("连续同一个窗口只算一跳", out.count("hwnd=10") == 1, out.count("hwnd=10"))


# ══════════════════════════════════════════════════════════════════════════
sec("⑥ 自陈完整性 —— 说真话，但不制造恐慌")

tl._reset_for_test()
tl.start(1, "t")
tl.note("input", hwnd=1)
out = tl.summarize(tl.finish())
ck("没截断时说 complete", "Coverage: complete" in out, out)

tl._reset_for_test()
tl.start(1, "t")
for i in range(tl.MAX_EVENTS + 60):
    tl.note("input", hwnd=1)
out = tl.summarize(tl.finish())
ck("截断时报出丢了多少", "60 earlier event" in out, out)
ck("报出真实起点（不是缓冲起点）", "pause actually began" in out, out)
ck("⭐⭐ 同时明说尾部完整（不给它畏手畏脚的理由）",
   "TAIL is intact" in out and "only ever drops the head" in out, out)


# ══════════════════════════════════════════════════════════════════════════
sec("⑦ 🔴 传感器静默失效：空日志 ≠ 什么都没发生")

tl._reset_for_test()
tl.start(1, "t")
s = tl.finish()
ck("轮询也没看到变化 → 无证据说钩子死了", tl.sensors_healthy(s))
out = tl.summarize(s, tl.sensors_healthy(s))
ck("健康的空日志 → 明说「真的什么都没发生」",
   "nothing observable actually happened" in out, out)

tl._reset_for_test()
tl.start(1, "t")
tl.note_poll_focus()              # 独立第二传感器看到焦点变了
s = tl.finish()                   # 而事件钩子一条 focus 都没报
ck("🔴 轮询看到了、钩子没报 → 判定钩子死了", not tl.sensors_healthy(s))
out = tl.summarize(s, tl.sensors_healthy(s))
ck("说出传感器失效", "SENSOR FAILURE" in out, out)
ck("⭐ 明确否掉「空日志=没事」这个错结论",
   "does NOT" in out and "nothing happened" in out, out)
ck("这一种情况才要求截图", "Take a screenshot" in out, out)

tl._reset_for_test()
tl.start(1, "t")
tl.note_poll_focus()
tl.note("focus", hwnd=5, title="x")     # 两边都看到了
s = tl.finish()
ck("两边都看到 → 健康", tl.sensors_healthy(s))

# ⚠️ 反方向不许判死：焦点 2 秒内变过去又变回来，轮询天然看不见。
tl._reset_for_test()
tl.start(1, "t")
tl.note("focus", hwnd=5, title="x")     # 钩子报了，轮询没报
s = tl.finish()
ck("⭐ 钩子报了、轮询没报 → **不**判死（该有却没有才算）",
   tl.sensors_healthy(s))

ck("没有 session 时不谎报健康问题", tl.sensors_healthy(None))
ck("没有 session 时摘要是空串（不编）", tl.summarize(None) == "")


# ══════════════════════════════════════════════════════════════════════════
sec("⑧ 第 2/3 层的关系：日志必读、截图有条件")

tl._reset_for_test()
tl.start(1, "t")
tl.note("input", hwnd=1)
out = tl.summarize(tl.finish())
ck("明确说「能还原就别截图」", "do NOT take a screenshot" in out, out)
ck("明确说截图是 only if 还原不了", "Only if you cannot reconstruct" in out, out)
ck("⭐ 有截图时要求与日志合看，而非独立判断",
   "TOGETHER with this log" in out and "the two agree" in out, out)


# ══════════════════════════════════════════════════════════════════════════
sec("⑨ 记录范围比接管判定宽（两张表不许混用）")

tl._reset_for_test()
tl.start(1, "t")
takeover._feed_log(takeover.FOCUS_CHANGE, False, 42,
                   takeover.TakeoverResult.IGNORED_NOT_A_TAKEOVER)
s = tl.finish()
ck("⭐ 焦点切换不算接管，但**进日志**（它是环境变化）",
   len(s.events) == 1 and s.events[0].kind == "focus", s.events)

tl._reset_for_test()
tl.start(1, "t")
takeover._feed_log(takeover.KEY, True, 42, takeover.TakeoverResult.IGNORED_SELF_INPUT)
s = tl.finish()
ck("Nano 自己发的输入不进日志（这份日志答的是「用户」做了什么）",
   len(s.events) == 0, s.events)

tl._reset_for_test()
tl.start(1, "t")
takeover._feed_log(takeover.MOVE, False, 42,
                   takeover.TakeoverResult.IGNORED_NOT_CONSEQUENTIAL)
s = tl.finish()
ck("纯移动不进日志（不产生后果）", len(s.events) == 0, s.events)

tl._reset_for_test()
takeover._feed_log(takeover.CLICK, False, 42, takeover.TakeoverResult.TAKEN)
ck("⭐ TAKEN 会**自己开始录**（起录挂在瞬发那一刻）", tl.is_recording())
s = tl.finish()
ck("那次触发接管的操作本身也被记下（它可能就是破坏动作的那一下）",
   len(s.events) == 1, s.events)

tl._reset_for_test()
takeover._feed_log(takeover.CLICK, False, 42, takeover.TakeoverResult.IGNORED_NANO_IDLE)
ck("Nano 不在 GUI 模式时不开始录", not tl.is_recording())
tl._reset_for_test()


# ══════════════════════════════════════════════════════════════════════════
sec("⑩ 源码不变量")

src_log = module_text("core.proactive.takeover_log")
src_tk = module_text("core.proactive.takeover")
src_hk = module_text("core.proactive.takeover_hooks")
src_hp = module_text("core.proactive.hooks")
src_or = module_text("core.orchestrator")
src_wb = module_text("core.os_layer.window_binding")

ck("独立第二传感器真的接上了（hooks.py 的 2s 轮询）",
   "takeover_log.note_poll_focus()" in src_hp)
ck("窗口生死/几何事件不走接管判定（直接写日志）",
   "takeover_log.note(kind" in src_hk and
   "on_user_signal" not in src_hk.split("_on_winevent")[1].split("_kb_cb =")[0])
ck("LOCATIONCHANGE 有节流（拖一次窗上千条）",
   "_loc_last" in src_hk and "< 1.0" in src_hk)
ck("只取顶层窗口（不然每个子控件都报一次）",
   "id_obj != _OBJID_WINDOW" in src_hk)
# ⭐ 实测探针抓到的：开**一个**记事本报 11 条 created，摘要说成"10 newly opened"。
ck("⭐ created/moved 要求「可见 + 有标题」（实测探针抓出来的噪音）",
   src_hk.count("IsWindowVisible") >= 2 and "_win_title(h)" in src_hk)
ck("没在录就立刻返回（平时开销近乎为零）",
   "if not takeover_log.is_recording():" in src_hk)

ck("消费点在**全量**等待处，不只在 os_execute",
   "_tl_gw.finish()" in src_or and "_a3_pause_note" in src_or)
ck("挂起日志挂在工具结果**前面**", '_pn}\\n\\n---\\n' in src_or or
   "f\"{_pn}" in src_or)
ck("取完即清（一次性证据）", 'self._a3_pause_note = ""' in src_or)

# ⚠️⚠️ 这条是最要紧的一条源码不变量：
#    第 2 层的全部意义就是"能还原就不用截图"。恢复文案里留着一句
#    无条件的"Verify the current state"，就等于把第 3 层又变回义务。
ck("🔴 恢复文案不含无条件核实义务",
   "Verify the current state before your next action" not in src_or)
ck("恢复文案改成了「读日志再判断影不影响下一步」",
   "Read the pause log above and decide whether" in src_or)

ck("为日志开的绑定读取**不校验 lease_id** 且写清了理由",
   "snapshot_bound_for_log" in src_wb and
   "两个不同的权限" in src_wb)
ck("它仍然检查 IsWindow（已销毁的窗口不算「你的目标还在」）",
   "IsWindow" in src_wb.split("snapshot_bound_for_log")[1].split("def clear")[0])

ck("健康计数器在 start 归零，不在 finish（顺序依赖已留痕）",
   "_poll_focus_seen = 0" in src_log.split("def start")[1].split("def note")[0])
ck("模块头写清了为什么不扩 ActivityBuffer",
   "两个不同的问题不要挤进同一个缓冲" in src_log)
ck("记录范围宽于判定范围这件事在代码里留了痕",
   "两张表本来就是为两个不同问题定义的" in src_tk)


# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 66}")
print(f"结果: {PASS} passed, {FAIL} failed")
if _FAILED:
    print("失败项:")
    for f in _FAILED:
        print(f"  · {f}")
print("=" * 66)
