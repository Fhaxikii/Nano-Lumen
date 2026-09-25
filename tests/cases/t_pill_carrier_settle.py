# -*- coding: utf-8 -*-
"""回看那一刻工具 pill 被提前定型成 `[✓] 2 tools · 55.2s`（命令还在跑）。

实测（2026-08-10）：一条 90 秒的 PowerShell 命令被交还，**回看那一刻**
pill 就变成了 `[✓] 2 tools · 55.2s`。两处都是假的：
  ① `[✓]` 宣称这批工具做完了 —— 它没有
  ② `55.2s` 不是真实用时 —— 只是「回看碰巧发生在第 55 秒」

📌 根因判据：**「模型开始说话」曾经等价于「工具都做完了」；长任务被交还之后
   不再等价** —— 交还的全部意义就是「让它一边说话一边继续跑」。
📌 这是「强制后台化」被删之后冒出来的**次生**缺陷：
   **一个前提被拿掉之后，依赖它的推断不会自己失效，只会变成错的。**
"""
import ast
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import module_text  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond, label, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}" + (f"   [{detail}]" if detail else ""))
    else:
        _failed.append(f"{label}   [{detail}]")
        print(f"  FAIL  {label}" + (f"   [{detail}]" if detail else ""))


class _El:
    """够用的假 UI 元素。"""

    def __init__(self, txt=""):
        self.txt = txt
        self.visible = True
        self.cls = "nano-tool-active"

    def set_text(self, t):
        self.txt = t

    def set_visibility(self, v):
        self.visible = v

    def classes(self, *a, remove=None, **kw):
        if remove:
            self.cls = self.cls.replace(remove, "").strip()
        return self

    def style(self, *a, **kw):
        return self


class _Gui:
    """只借 WebUI 的那几个方法，不起 NiceGUI。"""

    def __init__(self):
        from app import WebUI
        for _n in ("_settle_tool_pill", "_snapshot_tool_pill",
                   "_pill_has_running_carrier", "_settle_pill_snapshot",
                   "_settle_waiting_action", "_register_hidden_waiting"):
            setattr(self, _n, getattr(WebUI, _n).__get__(self, _Gui))
        self._waiting_pills = {}
        self._resp_state = {}

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _ui_scope(self):
        return self._Scope()


def _fresh_rs(count=2, age=55.2):
    return {"batch_tool_count": count, "batch_fail_count": 0,
            "tool_pill_lbl": _El("运行命令…"), "tool_pill_dollar": _El("$"),
            "tool_pill_fail_lbl": _El(""),
            "batch_start_time": time.time() - age, "pill_settled": False}


def _arm(g, _rs, sid="w1"):
    g._resp_state = _rs
    g._register_hidden_waiting(suspension_id=sid, action_ref=None,
                               waiting_intent="carrier_handback",
                               bg_ref="cmd_x",
                               pill=g._snapshot_tool_pill(_rs))


# ══════════════════════════════════════════════════════════════════════════
def t_not_settled_while_running() -> None:
    print("\n[1] 🔴🔴🔴 载体还在跑 → pill 不许定型（实测那一条）")
    g = _Gui()
    _rs = _fresh_rs()
    _arm(g, _rs)
    _lbl = _rs["tool_pill_lbl"]

    g._settle_tool_pill(_rs)
    check("tools" not in _lbl.txt and "[✓]" not in _lbl.txt,
          "⭐⭐⭐ **回看/说话都不再让它定型** —— 载体还在跑，"
          "`[✓] 2 tools · 55.2s` 里两处都是假的：勾宣称做完了，"
          "秒数只是「回看碰巧发生在第 55 秒」。"
          "📌 **「模型开始说话」曾经等价于「工具都做完了」；"
          "长任务被交还之后不再等价**", _lbl.txt)
    check(_rs.get("pill_settled") is not True,
          "⭐ 而且**没有**把 `pill_settled` 标上 —— "
          "📌 一个「暂时不能做」的动作，不许留下「已经做过」的痕迹，"
          "否则真的到点时它会被幂等挡住", f"pill_settled={_rs.get('pill_settled')}")
    check("nano-tool-active" in _lbl.cls,
          "流光还在（它确实还在跑，这是真事实）")


def t_settled_on_real_completion() -> None:
    print("\n[2] ⭐⭐ 载体真完成 → 才定型，而且用**真实用时**")
    g = _Gui()
    _rs = _fresh_rs(count=2, age=90.4)
    _arm(g, _rs)
    _lbl = _rs["tool_pill_lbl"]
    g._settle_tool_pill(_rs)                     # 回看：让路
    g._settle_waiting_action("w1", ok=True)      # 完成信号：真收

    check("[✓] used 2 tools" in _lbl.txt, "定型成 `[✓] used 2 tools`", _lbl.txt)
    _num = float(_lbl.txt.split("·")[1].strip().rstrip("s"))
    check(89.0 < _num < 92.0,
          "⭐⭐ 时长是**载体真实跑完的用时**（~90s），不是回看那一刻的 55.2s —— "
          "📌 **两个症状同一个根因时，别分别修**："
          "让路之后秒数自动就对了，不需要第二套时间来源", f"{_num}s")
    check("nano-tool-active" not in _lbl.cls, "流光撤了")
    check(_rs["tool_pill_dollar"].visible is False, "$ 提示符收了")


def t_failure_shows_as_failure() -> None:
    print("\n[3] 载体失败 → `[✗]`，不许因为「有个完成信号」就报成功")
    g = _Gui()
    _rs = _fresh_rs(count=1, age=12.0)
    _arm(g, _rs)
    g._settle_waiting_action("w1", ok=False)
    _t = _rs["tool_pill_lbl"].txt
    check("[✗]" in _t, "打叉", _t)
    check("1 failed" in _t,
          "失败数补上（`ok=False` 时至少算 1，不许显示 `0 failed`）—— 现并入主标签",
          _t)


def t_later_batch_not_blocked() -> None:
    print("\n[4] ⭐⭐⭐ 押着的是**那一个 pill**，不是整段回应期")
    g = _Gui()
    _rs = _fresh_rs(count=2, age=55.0)
    _arm(g, _rs)
    _old = _rs["tool_pill_lbl"]
    # 回看轮开新批次（实测就是这样：`不再回看，等它自己完成`）
    _rs.update({"tool_pill_lbl": _El("不再回看…"), "batch_tool_count": 1,
                "batch_start_time": time.time() - 0.4, "pill_settled": False,
                "tool_pill_dollar": _El("$"), "tool_pill_fail_lbl": _El("")})
    _new = _rs["tool_pill_lbl"]
    g._settle_tool_pill(_rs)
    check("[✓] used 1 tool" in _new.txt,
          "⭐⭐⭐ 回看轮**自己那个**批次照常定型 —— "
          "🔴 如果判据写成「这段回应期里有没有载体在跑」，"
          "这个批次会被一起锁住，而它里面根本没有长任务。"
          "📌 **能用「是不是同一个对象」判断的事，不要绕道去比别的东西**",
          _new.txt)
    check("tools" not in _old.txt and "[✓]" not in _old.txt,
          "而老批次仍然没被定型（它才是押着载体的那个）", _old.txt)


def t_snapshot_not_live_read() -> None:
    print("\n[5] ⭐⭐⭐ 收尾读**快照**，不是「到时候再看当前是哪个」")
    g = _Gui()
    _rs = _fresh_rs(count=2, age=88.0)
    _arm(g, _rs)
    _old = _rs["tool_pill_lbl"]
    _rs["tool_pill_lbl"] = _El("另一个批次")      # 指向已经换了
    _rs["batch_tool_count"] = 1
    g._settle_waiting_action("w1", ok=True)
    check("[✓] used 2 tools" in _old.txt,
          "⭐⭐⭐ 收的是**交还那一刻**那个 pill（2 tools），"
          "不是现在指向的那个（1 tool）—— "
          "📌 **一个「稍后收尾」的动作，必须记住它要收的那个对象，"
          "而不是到时候再去读「当前是哪个」**，"
          "因为在「稍后」这段时间里「当前」的含义会变", _old.txt)
    check("[✓]" not in _rs["tool_pill_lbl"].txt, "新指向的那个没被误收")


def t_action_done_always_set() -> None:
    print("\n[6] ⭐ 没有明细行可收时，也必须标 `action_done`")
    g = _Gui()
    _rs = _fresh_rs()
    _arm(g, _rs)
    g._settle_waiting_action("w1", ok=True)
    check(g._waiting_pills["w1"].get("action_done") is True,
          "⭐ `action_ref` 为空也标上 —— "
          "🔴 原来是 `if not ref: return`（不标就走），而它现在还是"
          "「这个 pill 还押着载体吗」的判据 → 不标就等于那个 pill **永远**"
          "定不了型，一直挂着流光。"
          "📌 **一个状态位一旦有了第二个读者，"
          "它的每一条设定路径都要重新过一遍**")
    check(g._pill_has_running_carrier(_rs["tool_pill_lbl"]) is False,
          "标了之后判据立刻放行（level-triggered，不用维护计数器）")

    _rs2 = _fresh_rs()
    g._settle_tool_pill(_rs2)
    check("[✓] used 2 tools" in _rs2["tool_pill_lbl"].txt,
          "⚠️ 回归：**没有**载体的普通批次照旧立刻定型（绝大多数走这条）",
          _rs2["tool_pill_lbl"].txt)


def t_source_invariants() -> None:
    print("\n[7] 源码不变量")
    _src = module_text("app")
    _tree = ast.parse(_src)

    def _fn(name):
        return next(n for n in ast.walk(_tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == name)

    _sw = ast.unparse(_fn("_settle_waiting_action"))
    check("_settle_pill_snapshot" in _sw, "完成信号那条路上真的去收 pill 了")
    check(_sw.index("action_done'] = True") < _sw.index("if ref:"),
          "⭐ `action_done` 的赋值在 `if ref:` **之前** —— 顺序就是这个修复本身")

    _st = ast.unparse(_fn("_settle_tool_pill"))
    check("_pill_has_running_carrier" in _st, "定型前问了那个判据")
    check(_st.index("_pill_has_running_carrier")
          < _st.index("pill_settled'] = True"),
          "⭐ 判据在**标记之前** —— 反过来就等于「先说做完了再检查」")

    _pc = ast.unparse(_fn("_pill_has_running_carrier"))
    check(" is lbl" in _pc, "⭐ 用 `is` 比对象身份，不是比 id/名字/回应期")
    check("resp_state" not in _pc,
          "⚠️ 刻意**不**按回应期判断（那会锁住回看轮自己的批次）")

    _snap = ast.unparse(_fn("_snapshot_tool_pill"))
    check("'lbl'" in _snap and "resp_state" not in _snap,
          "⚠️ 快照里**不塞** `_rs` —— "
          "📌 快照的意义就是「不再依赖那个会变的东西」，"
          "顺手把它塞进来等于没快照")

    _dw = ast.unparse(_fn("_drive_wake"))
    check("_settle_pill_snapshot" not in _dw,
          "⭐⭐ 唤醒路径上**没有**收 pill 的动作 —— "
          "📌 **定时回看只证明「该看一眼」，不证明载体完成**；"
          "两件事共用一个出口，就会得到实测看到的那个假勾")


def t_meta_row_never_naked() -> None:
    print("\n[8] 🔴🔴 载体在跑时，元信息行不许变成一个裸计时器（实测 2026-08-10）")
    _src = module_text("app")
    _tree = ast.parse(_src)
    _timer = next(n for n in ast.walk(_tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_resp_status_timer")
    _code = ast.unparse(_timer)

    check("set_text(f'{elapsed}s')" not in _code
          and 'set_text(f"{elapsed}s")' not in _code,
          "⭐⭐⭐ **不再把元信息行写成一个裸的 `62s`** —— 实测：转圈没了、"
          "✦ 也没出来。那一行的词汇表只有两个状态（⠋ 进行中 / ✦ 已结束），"
          "藏了前者又不给后者就落进了一个**不存在的第三态**，读起来像它坏了。"
          "📌 **一个状态指示器有 N 个合法状态，任何路径都必须落在这 N 个里** —— "
          "「把它藏起来」通常不是第 N+1 个状态，而是「没有状态」")

    # ⚠️ 载体在跑时**不许**藏转圈：那一行属于「这一段回应期」，不属于 Nano
    _wfc = next((n for n in ast.walk(_timer)
                 if isinstance(n, ast.If) and "waiting_for_carrier" in ast.unparse(n.test)),
                None)
    check(_wfc is not None, "`waiting_for_carrier` 那个分支还在（措辞仍要区分）")
    if _wfc is not None:
        _branch = "\n".join(ast.unparse(s) for s in _wfc.body)
        check("set_visibility(False)" not in _branch,
              "⭐⭐⭐ 那个分支里**不再藏转圈** —— "
              "🔴 原注释的理由是「Nano is not thinking while the carrier runs」，"
              "对 Nano 是真的，**但这一行不属于 Nano**，它属于**这一段回应期**，"
              "而那一段确实还开着、确实还有事在跑。"
              "📌 **一个「进行中」指示器属于它所在的那一段，"
              "不属于其中某一个参与者**")
        check("continue" not in _branch,
              "⭐ 也不再 `continue` 跳过统一的写入 —— "
              "📌 一个「特殊情况」如果跳过了公共出口，它就得自己把公共出口"
              "做的每件事都做一遍，而那必然会漏")
        check("stage" in _branch,
              "⭐ 它现在只做一件事：**换措辞**。"
              "📌 **假事实的修法是换成真话，不是把话删掉** —— "
              "说 `thinking` 是假的（它没在想），"
              "但把整行删成一个数字并不因此变成真的")

    check('f"{stage} · {elapsed}s"' in _code or "f'{stage} · {elapsed}s'" in _code,
          "⚠️ 两种情况都走**同一条**写入语句（措辞是唯一的差别）")
    check("still running" in _code,
          "⭐ 载体在跑时的措辞说的是「还在跑」，不是「在想」", "still running")


if __name__ == "__main__":
    print("=" * 74)
    print("[实测] 工具 pill 不许在载体还在跑时定型")
    print("=" * 74)
    for _t in (t_not_settled_while_running, t_settled_on_real_completion,
               t_failure_shows_as_failure, t_later_batch_not_blocked,
               t_snapshot_not_live_read, t_action_done_always_set,
               t_source_invariants, t_meta_row_never_naked):
        _t()
    print("\n" + "=" * 74)
    print(f"结果: {_passed} passed, {len(_failed)} failed")
    print("=" * 74)
    if _failed:
        for _f in _failed:
            print("  !!", _f)
        sys.exit(1)
