# -*- coding: utf-8 -*-
# nano-test: live — opens real Notepad windows and needs an idle desktop
"""窗口绑定 —— 「Nano 知道自己在操作哪个窗口」这个缺失原语。

⚠️ **本套件会真的开两个记事本**（Windows only）。它必须这么做：
   要复现的那个事故的核心是「**两个标题完全相同的窗口**」，
   而 mock 出来的假 hwnd 证明不了真实 Win32 语义。

═══ 这个原语要解决什么 ═══
2026-08-07 实测数据损坏：Nano 要操作它自己打开的 `新建文本文档.txt`，
用户中途把焦点放到**自己的**另一个记事本上，Nano 对着那个 `Ctrl+A` + 输入，
**清掉了用户的内容**。根因是 `get_target_window()` 返回「Z-order 最前、非 Nano」的窗口 ——
一个**「任务开始时」的启发式**被当成**「每步重新求值」的权威**用了。

═══ 四组，第 [2] 组是事故本身 ═══
1. 绑定建立：**动作前后的窗口差集** = 这次动作开出来的窗口
2. ⭐⭐ **事故复现**：两个同名记事本，绑一个、把另一个提到前台 → 必须分得出来
3. ⭐⭐ **截图答不了的四分**：还在 / 最小化 / 已关闭 / 不在前台
4. ⭐ 有效期从**活动租约**推导，不靠任何人记得清理

用法：
  py -3.10 tests\t_window_binding.py
"""
from __future__ import annotations

import ast
import ctypes
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.os_layer import window_binding as WB

_results: list[tuple[bool, str, str]] = []
_U = ctypes.windll.user32
_SW_MINIMIZE, _SW_RESTORE = 6, 9
#: 两个自建窗口用同一标题 —— 复现「靠标题判身份会失效」的场景
TITLE = "无标题 - 文档"


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _SpawnedWindow:
    """一个自建窗口：进程 + 它拥有的那个顶层窗口句柄。

    自建窗口的 pid / hwnd / 标题都由 `_win_window.py` 子进程可控，
    归属在任何 Windows 版本上都确定 —— 不依赖系统记事本，
    也就绕开了「Win11 打包版把窗口挂到另一个进程名下」的问题。
    """
    __slots__ = ("proc", "hwnd")

    def __init__(self, proc, hwnd):
        self.proc = proc
        self.hwnd = hwnd


def _open_window(title: str = "无标题 - Notepad"):
    """spawn 一个自建窗口，**等它的窗口真的出现**再返回。

    窗口由独立子进程开出，术语上是进程自己拥有的顶层窗口，
    所以 `_hwnds_of_pid(proc.pid)` 一定能找到它（这是自建的目的）。

    ⚠️ 仍留一个总上限：等不到就返回，让断言去报错 ——
       📌 一个没有上限的等待，会把「启动失败」变成「测试挂住」。
    """
    p = subprocess.Popen([
        sys.executable, "-X", "utf8", str(pathlib.Path(__file__).parent / "_win_window.py"),
        "--title", title,
    ])
    _deadline = time.time() + 8.0
    while time.time() < _deadline:
        got = _hwnds_of_pid(p.pid)
        if got:
            time.sleep(0.25)      # 让窗口稳定下来
            return _SpawnedWindow(p, sorted(got)[0])
        time.sleep(0.1)
    return _SpawnedWindow(p, 0)


def _hwnds_of_pid(pid: int) -> set:
    """那个进程拥有的可见顶层窗口。

    ⚠️⚠️ **这个助手是为了修一处结构性抖动加的（2026-08-08）。**
       原来判「新开的是哪个窗口」用的是 `len(差集) == 1` ——
       那假设**这段时间里只出现这次动作开的那个窗口**。
       整套测试连跑时（前面的用例刚开关过窗口、系统弹了个什么）差集会有两个，
       于是它**随机变红**。
    📌 **别用一个近似物（差集里只有一个）去回答一个能精确回答的问题
       （那个窗口属于刚起的那个进程）** —— pid 就在手上，`Popen` 返回的。
    📌 而且抖动本身值得单独修：**一个会随机变红的测试，会训练出
       「红了先重跑一次」的习惯，而那正好废掉整套测试。**
    """
    out = set()
    try:
        u = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                         ctypes.c_void_p)

        def _cb(h, _l):
            try:
                got = ctypes.c_ulong()
                u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(got))
                if got.value == pid and u.IsWindowVisible(ctypes.c_void_p(h)):
                    out.add(int(h))
            except Exception:
                pass
            return True

        u.EnumWindows(WNDENUMPROC(_cb), None)
    except Exception:
        pass
    return out



def _baseline_excluding_strangers(before: set, *pids: int) -> set:
    """把**不属于我们刚开的那些进程**的新窗口并回 `before`。

    🔴🔴 为什么需要它（2026-08-16，第三次修同一个抖动）：
       `bind_if_new` 的语义是「动作跑完后新冒出来的窗口就是我开的」——
       在**生产**里这是对的（动作很短，而开窗的就是 Nano）。
       但在**全量测试**里，同一时间还有别的窗口在开：Nano 自己被反复重启、
       别的用例也在开窗。于是差集里混进外来窗口，两条断言当场红：
           `rec["proc"] == "notepad.exe"`   ← 前台可能是那个外来窗口
           `rec["sure"] is True`            ← 差集里不止一个
       而**红的原因与它要证的事毫无关系**。

    📌 **一个共享的操作系统会往你的差集里塞别人的东西** ——
       测试要测的是「**这次动作**开了什么」，那就必须把不属于这次动作的
       算回基线，而不是放宽断言。
    ⚠️ 放宽断言（比如允许 `sure=False`）是错的修法：那恰好把这套测试
       唯一要守的东西（**能不能认准这次开的那一个**）一起放掉了。

    ⚠️ 前两次修的是**别的成因**，都没修到这一层：
       ① 用 `_hwnds_of_pid` 取代 `len(差集)==1`（2026-08-08）
       ② 把 `time.sleep(1.4)` 换成轮询等窗口出现（2026-08-16）
       📌 一个抖动被修过两次还在抖，说明前两次修的是它的**症状**。

    ⚠️⚠️ **第四次观测（2026-08-21，留数据不下结论）**：全量里仍偶发红，
       两次分别是 33/42 和 41/42；**紧接着单独连跑 12 次全是 42/42**。
       → 这说明剩下的成因**仍然与「同时有别的东西在开窗」有关**，
         而不是本次那批改动（那批一个字都没碰窗口绑定）。
       📌 记在这里而不是「重跑一次就过了」：**一个被重跑掩盖过去的红，
          下一次就没人会认真看它了** —— 而这套测试守的正是「能不能认准我开的那一个」。
       ⏸ 真要修，方向应该是**给整套测试串行化开窗**（一个进程级的锁），
          而不是继续在差集这一层打补丁 —— 前三次都在这一层，都没修掉。
    """
    _mine = set()
    for _pid in pids:
        _mine |= _hwnds_of_pid(_pid)
    _new = WB.snapshot() - before
    return before | (_new - _mine)


def t_bind_by_diff() -> None:
    print("\n[1] 绑定建立：动作前后的窗口差集")
    WB.clear()
    before = WB.snapshot()
    w = _open_window()
    try:
        before = _baseline_excluding_strangers(before, w.proc.pid)
        rec = WB.bind_if_new(before, "L1")
        check(rec is not None, "开了个窗口 → 绑上了")
        check(rec and rec["hwnd"] == w.hwnd,
              "绑的是刚开的那个窗口", f"{rec['hwnd'] if rec else None} vs {w.hwnd}")
        check(rec and rec["sure"] is True,
              "⭐ 只出现一个新窗口 → 标记为确定")
        check(rec and rec["hwnd"] not in before,
              "⚠️ 绑的 hwnd**确实是新的** —— 差集不是摆设")

        # 没有新窗口时不许乱改绑定
        hw = rec["hwnd"]
        again = WB.bind_if_new(WB.snapshot(), "L1")
        check(again and again["hwnd"] == hw,
              "⭐ 后续动作没开新窗口 → 绑定保持不动（幂等）")
    finally:
        w.proc.terminate(); time.sleep(0.6)


def t_two_identical_windows() -> None:
    """⭐⭐ 事故本身。"""
    print("\n[2] ⭐⭐ 事故复现：两个**标题完全相同**的窗口")
    WB.clear()
    before = WB.snapshot()
    mine = _open_window(TITLE)          # 「Nano 打开的那个」
    try:
        before = _baseline_excluding_strangers(before, mine.proc.pid)
        rec = WB.bind_if_new(before, "L2")
        check(rec is not None, "绑定「我的」窗口", str(rec and rec["hwnd"]))
        my_hwnd = rec["hwnd"]
        my_title = rec["title"]

        mid = WB.snapshot()
        theirs = _open_window(TITLE)    # 「用户自己的那个」
        try:
            # ⚠️ **不能假设新开的进程会拿到前台** —— `Popen` 起的进程 Windows 不一定给焦点
            #    （第一版就这么错的：`GetForegroundWindow()` 拿到的是 Chrome）。
            #    改成按差集找它的 hwnd，不依赖系统给不给焦点。
            # ⚠️ 按**那个进程拥有的窗口**筛，不靠「差集里只有一个」（见 `_hwnds_of_pid`）
            _new = (WB.snapshot() - mid) & _hwnds_of_pid(theirs.proc.pid)
            if not _new:
                # 兜底：窗口还没建好时差集是空的 —— 再等一下按 pid 直接取
                time.sleep(0.8)
                _new = _hwnds_of_pid(theirs.proc.pid) - {my_hwnd}
            check(len(_new) == 1,
                  "拿到「用户那个」窗口的 hwnd（按 pid 筛，不靠差集只有一个）",
                  str(sorted(_new)))
            other = sorted(_new)[0]
            check(other != my_hwnd, "前提：是另一个 hwnd", f"{other} vs {my_hwnd}")
            check(WB._title(other) == my_title,
                  "⭐⭐ 两个窗口的标题**完全相同** —— 这正是靠标题判身份会失效的地方",
                  repr(my_title))

            st = WB.target_state("L2")
            check(st is not None and st["alive"] is True,
                  "⭐ 我的窗口仍然存在（没被用户开新窗口影响）")
            check(st["foreground"] is False,
                  "⭐⭐ 而且**它不是前台** —— 这一条就是事故的分界线")
            check(st["fg_hwnd"] != my_hwnd,
                  "如实报出前台是别人", f"fg={st['fg_hwnd']} mine={my_hwnd}")

            note = WB.describe("L2")
            check("NOT in the foreground" in note, "警告说清了「不是前台」")
            check("DIFFERENT window" in note and "identical titles" in note,
                  "⭐⭐ 明说了「是另一个窗口，即使看起来一样」—— "
                  "不说这句，模型看到同名就会以为是同一个")
            for w in ("select-all", "delete", "overwrite", "save"):
                check(w in note, f"点名破坏性动作：{w}")
            check("stop and ask" in note, "给了出路（提到前台，或停下来问）")

            # ⭐ 真修：选目标窗口时必须选【我的】，不是最前面那个
            from core.os_layer.executor_low import get_target_window
            import core.runtime.oslease as _ol

            class _FakeLease:
                holder = _ol.Holder.NANO
                lease_id = "L2"
            _saved = _ol.current_activity
            try:
                _ol.current_activity = lambda *a, **kw: _FakeLease()
                w = get_target_window()
                check(w is not None and w._hWnd == my_hwnd,
                      "⭐⭐ `get_target_window()` 返回**我绑定的**那个，"
                      "不是 Z-order 最前的那个 —— 这就是不再写错对象的那一行",
                      f"got={getattr(w, '_hWnd', None)} want={my_hwnd}")
            finally:
                _ol.current_activity = _saved

            # 反证：没有绑定时必须退回启发式，否则一次任务的第一步就没法做
            WB.clear()
            try:
                _ol.current_activity = lambda *a, **kw: _FakeLease()
                w2 = get_target_window()
                check(w2 is not None,
                      "反证：**没有绑定**时退回 Z-order 启发式（仍拿得到窗口）—— "
                      "一次任务的第一步本来就没绑定可用，那时「操作用户在看的窗口」是对的")
            finally:
                _ol.current_activity = _saved
        finally:
            theirs.proc.terminate(); time.sleep(0.5)
    finally:
        mine.proc.terminate(); time.sleep(0.6)


def t_four_way_distinction() -> None:
    """⭐⭐ 截图答不了的那四分。"""
    print("\n[3] ⭐⭐ 还在 / 最小化 / 已关闭 —— 截图看起来完全一样")
    WB.clear()
    before = WB.snapshot()
    w = _open_window()
    hwnd = None
    try:
        before = _baseline_excluding_strangers(before, w.proc.pid)
        rec = WB.bind_if_new(before, "L3")
        hwnd = rec["hwnd"]

        n1 = WB.describe("L3")
        check("MINIMIZED" not in n1 and "GONE" not in n1, "正常状态：不报警")

        _U.ShowWindow(hwnd, _SW_MINIMIZE); time.sleep(0.6)
        st = WB.target_state("L3")
        check(st["minimized"] is True and st["alive"] is True,
              "⭐⭐ 最小化 → **仍然存在**（alive=True）。"
              "截图里看不到它，但它没消失")
        n2 = WB.describe("L3")
        check("MINIMIZED" in n2 and "did not disappear" in n2, "明说了它没消失")
        check("do not reopen" in n2,
              "⭐ 明说了**别重开** —— 重开会多出第二个窗口，"
              "而这正是「看截图没看到就重开」的典型错法")
        check("MOVED" not in n2,
              "⚠️ 最小化时**不报** MOVED（最小化会把 rect 改到屏幕外，技术上真变了）—— "
              "📌 报警的价值取决于读的人能不能一眼看出该干什么，不取决于它多全")

        _U.ShowWindow(hwnd, _SW_RESTORE); time.sleep(0.6)
        check(WB.target_state("L3")["minimized"] is False, "还原后恢复正常")
    finally:
        w.proc.terminate(); time.sleep(0.9)

    check(WB.bound("L3") is None,
          "⭐⭐ 窗口关掉 → 绑定自动失效（`IsWindow` 为假），"
          "**不需要任何人记得清理**")
    g = WB.gone_note("L3")
    check("GONE" in g and "CLOSED" in g, "已关闭有专门的话")
    check("not minimized, not covered" in g,
          "⭐⭐ 明确把「关掉了」和「最小化/被挡」区分开 —— "
          "前者要重开、后者要还原，而截图对这两种看起来一模一样")


def t_validity_derived_from_lease() -> None:
    print("\n[4] ⭐ 有效期从活动租约推导，不靠谁记得清理")
    WB.clear()
    before = WB.snapshot()
    w = _open_window()
    try:
        before = _baseline_excluding_strangers(before, w.proc.pid)
        WB.bind_if_new(before, "LEASE_A")
        check(WB.bound("LEASE_A") is not None, "本段（LEASE_A）内绑定有效")
        check(WB.bound("LEASE_B") is None,
              "⭐⭐ 换了一段 GUI 操作（新 lease）→ 旧绑定**自动**失效")
        check(WB.bound("") is None, "空 lease_id 一律无效（读不到租约时不许沿用旧绑定）")
        check(WB.describe("LEASE_B") == "",
              "⚠️ 失效时返回空串，**不编**一个看起来合理的答案")
    finally:
        w.proc.terminate(); time.sleep(0.6)


def t_wiring() -> None:
    print("\n[5] 接线（AST / 源码）")
    orc = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    low = (ROOT / "core" / "os_layer" / "executor_low.py").read_text(encoding="utf-8")
    lc = "\n".join(l for l in low.splitlines() if not l.strip().startswith("#"))

    check("_wb.snapshot()" in oc and "_wb.bind_if_new(" in oc,
          "⭐ `os_execute` 动作前后各拍一次快照")
    i_snap, i_bind = oc.find("_wb.snapshot()"), oc.find("_wb.bind_if_new(")
    check(0 < i_snap < i_bind,
          "⚠️ 快照在**前**、绑定在**后** —— 顺序反了差集永远是空的", f"{i_snap} < {i_bind}")
    check("finally:" in oc[i_snap:i_bind],
          "⭐ 绑定挂在 `finally` 里 —— 动作抛异常时窗口可能已经开出来了，"
          "那种情况下更需要知道它是谁")

    check("window_binding" in lc and "get_target_window" in low,
          "⭐⭐ `get_target_window()` 会先查绑定")
    i_b = lc.find("_wb.bound(")
    i_enum = lc.find("gw.getAllWindows()")
    check(0 < i_b < i_enum,
          "⭐⭐ **绑定优先于 Z-order 启发式**（绑定的查询排在枚举之前）",
          f"{i_b} < {i_enum}")
    check("Holder.NANO" in lc,
          "⚠️ 只在**Nano 自己持有**租约时才用绑定 —— "
          "机器在用户手里时不该拿旧绑定去动手")

    tree = ast.parse(orc)
    note = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_window_identity_note":
            note = ast.get_source_segment(orc, n) or ""
    check("window_binding" in note and "gone_note" in note,
          "⭐ 身份汇报里带上「我的目标窗口现在怎么样」，不只报前台是谁")

    wbsrc = (ROOT / "core" / "os_layer" / "window_binding.py").read_text(encoding="utf-8")
    check("EVENT_OBJECT_CREATE" in wbsrc,
          "⭐ 留痕：为什么用差集而**不用** `EVENT_OBJECT_CREATE` 钩子")


def main() -> int:
    import platform
    if platform.system() != "Windows":
        print("非 Windows，跳过（本套件需要真实窗口）")
        return 0
    t_bind_by_diff()
    t_two_identical_windows()
    t_four_way_distinction()
    t_validity_derived_from_lease()
    t_wiring()
    passed = sum(1 for r in _results if r[0])
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
