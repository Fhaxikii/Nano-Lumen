# -*- coding: utf-8 -*-
"""重启后：那几件停在半路的活，由 Nano 主动问一句要不要重做。

═══ 这一套守什么 ═══

  ① **上个进程留下的活一律终止**（关闭 = 那次执行结束，技术上也确实续不了）
  ② **但不等于闭嘴** —— 由 Nano **主动开口**问用户要不要重做（新气泡）
     ⚠️ 措辞说「关闭」，**不说「崩溃」**：我们分不清是哪种，
        而「崩溃」是更强的断言，说错了会让用户以为软件坏了。
        📌 两个描述都可能对时，用**断言更弱**的那个。
  ③ **说不出「是什么」的不提**（`goal_summary` 为空 → 过滤掉）
  ④ 生成失败就一个字都不说 —— 固定文案兜底已整个拆掉

═══ ⚠️ 这里曾经还有一套，2026-08-22 拆了 ═══

「被搁置的动作 → 用户放开电脑 → 提醒模型」那一整套（`DEFERRED_ACTION` /
blocker provider / tick / 每轮注入）。拆的理由见 `runtime/scheduler.py` 的墓碑：
它服务的场景只有「Nano 要点鼠标而用户占着电脑」这一条窄缝，
而 主线那个场景（「那个包装好了」）**v1.52 早就通了**，跟它无关。
📌 **一个已经存在的形状，第二次出现时该复用它，而不是造第二条。**

本文件末尾留了几条**负向断言**：确认那套真的拆干净了，别哪天又长回来。

用法：
  py -3.10 tests\\cases\\t_b1_scheduler.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP = module_text("app")
SCHED = module_text("core.runtime.scheduler")
ORCH = module_text("core.orchestrator")
RECON = module_text("core.runtime.reconciler")
SCHED = module_text("core.runtime.scheduler")

BASE_T = 1_000_000.0


def _fn(src: str, name: str):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise LookupError(f"def {name} not found")


# ══════════════════════════════════════════════════════════════════════════

def t_restart_path_is_a_different_thing() -> None:
    print("\n[1] ⭐⭐ 重启那条是另一件事：新气泡 + 主动开口 + 说「关闭」")
    from core.runtime.scheduler import startup_resume_notice as f

    check(f([]) == "", "没有未完成的活 → 一个字都不说")
    check(f([{"task_id": "T", "kind": "BACKGROUND_JOB", "goal": ""}]) == "",
          "⭐ 说不出「是什么」的也不提（提了是噪音）")
    msg = f([{"task_id": "T", "kind": "BACKGROUND_JOB", "goal": "批量改 timeout"}])
    check("批量改 timeout" in msg, "带上它是什么")
    check("was closed" in msg, "⭐ 说「关闭」")
    # ⚠️ 已定：分不清崩溃与否时，用**断言更弱**的那个描述
    check("do NOT say it crashed" in msg,
          "⭐⭐⭐ 明确禁止说「崩溃」（两个描述都可能对时，用断言更弱的）")
    check("wait for their answer" in msg,
          "⭐⭐ 明说先别动手 —— 提醒不是自动续上")

    fn = _fn(APP, "_startup_resume_offer")
    check(fn is not None, "存在 `_startup_resume_offer`")
    src = ast.get_source_segment(APP, fn) or ""
    check("_proactive_push" in src,
          "⭐⭐ 走主动开口 → **新气泡**（合并进老气泡会非常奇怪）")
    check("language_clause" in src, "⭐ 语言注入走 i18n 那个唯一出处")
    # 🔴 拆掉的那条债不许在这里复活
    # 🔴 这条断言的第一版写的是「源码里不许出现『兜底』二字」——**假红**，
    #    命中的是 docstring 里解释「为什么没有兜底」的那句话。
    #    📌 本套件家族第五次栽在「按字符串出现过核，不算核」上。
    # ⭐ 要守的其实是一个**结构**性质：**失败路径上没有 push**。
    _handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    _pushes_in_except = [
        c for h in _handlers for c in ast.walk(h)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "_proactive_push"]
    check(_handlers and not _pushes_in_except,
          "⭐⭐⭐ **异常路径上一句话都不说** —— API 调不通意味着它此刻不能思考，"
          "这时蹦一句写死的话是在谎报它的状态")
    # 空产出也不说
    _early = [n2 for n2 in ast.walk(fn) if isinstance(n2, ast.Return)]
    check(len(_early) >= 3, "多处提前返回（没活/说不出是什么/生成为空）", str(len(_early)))


def t_startup_terminates_and_still_speaks() -> None:
    print("\n[2] ⭐⭐ 「不自动续」和「不提」是两件事")
    # 终止：执行体随进程消失，技术上确实续不了
    check("_RESUMABLE_KINDS: frozenset = frozenset()" in RECON,
          "⭐ 豁免名单已清空 —— ACTIVE 的一律终止")
    check("interrupted_details" in RECON, "⭐ 报告带上「被终止的是什么活」")
    # 但仍然会问
    # 🔴 这里踩过一个真 bug：第一版写成 `self._startup_interrupted`，
    #    而那段启动恢复代码**在模块级，没有 self** → 运行时 `NameError`，
    #    被外层 `except` 吞成一句「启动恢复失败（不影响启动）」。
    #    📌 **一条被吞掉的 NameError，表现成的是「功能失败」，
    #       不是「有人写错了变量」。**
    # ⭐ 而 `t_l23_missing_imports` 那个作用域检查器**本该抓到却没抓到** ——
    #    因为 `self`/`cls` 当时在它的 `_BUILTINS` 豁免表里（已摘掉，见那边留痕）。
    check("_STARTUP_INTERRUPTED: list = []" in APP,
          "⭐⭐ 用模块级变量接住（那段代码本来就在模块级）")
    # ⚠️ 按 **AST 判有没有那次属性访问**，不在源码里搜字符串 ——
    #    上面那段留痕注释里就逐字写着它。📌 本项目第六次栽在
    #    「按字符串出现过核，不算核」上。
    _bad = [nd for nd in ast.walk(ast.parse(APP))
            if isinstance(nd, ast.Attribute) and nd.attr == "_startup_interrupted"
            and isinstance(nd.value, ast.Name) and nd.value.id == "self"]
    check(not _bad, "⭐ 那个不存在的 self 引用已清掉（按 AST 判）", str(len(_bad)))
    import t_l23_missing_imports as _M
    check("self" not in _M._BUILTINS and "cls" not in _M._BUILTINS,
          "⭐⭐⭐ 作用域检查器不再豁免 self/cls —— 下次这种错它能抓到")
    check("ui.timer(4.0, self._startup_resume_offer, once=True)" in APP,
          "⭐ 启动后会开口问")
    # 顺序：系统陈述事实在前，Nano 开口在后
    i_crash = APP.find("ui.timer(2.5, self._crash_journal_tick")
    i_offer = APP.find("ui.timer(4.0, self._startup_resume_offer")
    check(0 < i_crash < i_offer,
          "⭐ 崩溃留痕在前、Nano 开口在后（顺序反了会像 Nano 在替系统解释）")
    # 🔴 第一版那套「关闭=放弃」的论证已经被推翻，不该还留在代码里
    check("关闭软件即视为放弃本次协同" not in RECON,
          "⭐⭐ 被推翻的那套论证已经从代码里清掉了")
    check("误触了关闭按钮" in RECON,
          "📌 而推翻它的那个反例留了痕（下一个人才不会再走一遍）")


def t_no_fixed_text_fallback_anywhere() -> None:
    print("\n[4] 旧的主动开口机制（ProactiveSpeaker）已删除，开口只由主动智能引擎决定")
    import importlib.util as _ilu
    check(_ilu.find_spec("core.proactive.speaker") is None, "core.proactive.speaker 模块不存在")
    check("ProactiveSpeaker" not in APP and "_speaker" not in APP, "app 不再创建或调用旧 speaker")


def t_the_torn_down_set_stays_torn_down() -> None:
    """⚠️ 负向断言：那套拆掉的东西别哪天又长回来。

    📌 拆一个东西时，**最容易复发的不是代码，是那个想法** ——
       所以这里守的是「它没有回来」，而墓碑守的是「别再想它」。
    """
    print("\n[3] ⚠️ 被拆掉的那套没有长回来")
    from core.runtime.task import TaskKind
    check("deferred" not in TaskKind._ALL,
          "⭐⭐ `DEFERRED_ACTION` 这类 Task 没了", str(sorted(TaskKind._ALL)))
    import core.runtime.task as _tk
    check(not hasattr(_tk, "defer_action") and not hasattr(_tk, "settle_deferred_action"),
          "⭐ 建/收那条代办的两个入口没了")
    import core.runtime.scheduler as _S
    check(not hasattr(_S, "due_resumptions") and not hasattr(_S, "describe_for_model"),
          "⭐⭐ scheduler 里只剩重启那条",
          str([n for n in dir(_S) if not n.startswith("_")]))
    import core.runtime.oslease as _ol
    check(not hasattr(_ol, "_deferred_action_blockers"),
          "⭐ 那个 blocker provider 没了")
    check("_b1_resume_notice" not in ORCH, "⭐ 每轮注入那段没了")
    check("_b1_refresh_resume_hint" not in APP, "⭐ app 侧那个 tick 没了")

    # ⭐ 但墓碑要在 —— 那两个「调度器落地后」的挂钩仍是真缺口，
    #    下一个人看到它们时得知道「有人走过这条路，别再造一套」
    check("🪦" in SCHED or "这里曾经有一套" in SCHED, "⭐⭐ 墓碑留着")
    check("set_next_checkin" in SCHED,
          "⭐⭐ 墓碑指出了真要做时的正确做法（复用既有机制，不造第二套）")
    check("⏸ 调度器落地后" in ORCH,
          "⭐ 原来那个挂钩注释还在（缺口是真的，只是不值得一套机制）")
    check("2026-08-22 试过一版又拆了" in ORCH,
          "⭐⭐ 挂钩旁边留了「试过又拆了」—— 📌 别让下一个人重走一遍")


def main() -> int:
    print("=" * 74)
    print("[B1] 重启后问一句 + 拆固定文案兜底（那套 Scheduler 已拆，见负向断言）")
    print("=" * 74)
    t_restart_path_is_a_different_thing()
    t_startup_terminates_and_still_speaks()
    t_the_torn_down_set_stays_torn_down()
    t_no_fixed_text_fallback_anywhere()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
