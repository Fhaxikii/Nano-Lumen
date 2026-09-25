# -*- coding: utf-8 -*-
"""待审草稿：模型要知道它是什么，而且一个文件名只能有一份。

═══ 决定性的那一轮（19:37）═══

用户在**有真代码的**待审卡 `int_9eeac9e476`（GetComputerIP）上点了「回复这条」，
等了 10 秒确认生效，然后说「你能不能目前修改这个代码」。日志：

    19:37:54 [Router] create_new_skill 元工具触发 → 进探索阶段（requirement='你能不能目前修改这个代码'）

Nano 在 UI 上的原话：**「因为到现在为止，我们只是澄清了需求，还没有生成任何代码。」**

指向是活的、是对的，`answer_open_interaction` 就在工具表里，它还是去建新的了。
所以**不是**"指向失效把它逼进死角"（那是上一版的错误结论，已证伪）。

真因：它看到的那一行只有一个不透明标签 `[skill_audit]`，而 `[Open Interactions]`
的头部写着"Nano is waiting on the user for these"、"The message answers one of them"
—— 整个框架是照**澄清**写的。对审计来说这是错的：那不是一个问题，
**代码已经写完了在等批准**，`ANSWER`=部署、`ANSWER_AND_AMENDMENT`=改代码。
这些语义**一个字都没写**。

于是"改这个代码 / 部署这个吧 / 继续"这些话，模型认得的唯一出口就是
`create_new_skill` → 每轮再造一个同名 Skill → 最后堆了**三张同名
GetComputerIP 待审卡**（int_9eeac9e476 / int_e42c7c7f04 / int_f2633f59d5）→ 死循环。

📌 判据：**给模型一个状态标签，不等于给了它这个状态的语义。**
   枚举名对写代码的人自解释，对模型只是一个陌生字符串。

═══ 同名并存那半 ═══

是同一个原因的另一副面孔：两张同名 `GetDNSConfig` 待审卡，
部署其中一张**两张一起消失**（18:08:15 同毫秒关掉两条）。
因为 `_pending_skills` 按文件名存、`apply_pending_skill` 也只认文件名 ——
**底下从来只有一份 payload，卡片却有好几张**。
所以"多张同名卡"从一开始就是假象，正解是新草稿 SUPERSEDE 旧草稿。

用法：
  py -3.10 tests\cases\t_audit_semantics.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402
from tests._patch import patch_global  # noqa: E402

from loguru import logger
logger.remove()

import core.orchestrator as om
from core.runtime import interaction as _it

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class Rec:
    def __init__(self, iid, kind, txt, ts, artifact=""):
        self.interaction_id, self.kind, self.prompt_text = iid, kind, txt
        self.created_at, self.artifact_id = ts, artifact
        self.status, self.answer_verbatim = _it.Status.OPEN, ""


def _render(live):
    patch_global("core.orchestrator", "_rt_live_interactions", lambda o: live)
    return om.Orchestrator._build_open_interactions_injection(
        types.SimpleNamespace(_reply_target=None))


AUDIT = Rec("int_9eeac9e476", _it.Kind.SKILL_AUDIT,
            "待审计的 Skill「GetComputerIP」（校验通过）：获取本机 IP", 200.0, "GetComputerIP")
CLAR = Rec("int_6ac6ec0512", _it.Kind.SKILL_CLARIFICATION,
           "这个 Skill 的核心作用是什么？", 100.0)


def t_audit_semantics_present() -> None:
    print("\n[1] ⭐⭐ 有待审草稿时，必须把 skill_audit 的语义写给模型")
    out = _render([AUDIT])

    check("the code is already written" in out,
          "⭐ 明说【代码已经写完了】—— 这是 19:37 那轮模型不知道的事")
    check("ANSWER_AND_AMENDMENT" in out and "modify pending code" in out,
          "⭐ 明说【改待审代码走 ANSWER_AND_AMENDMENT，不用重写】")
    check("deployed as-is" in out,
          "明说 ANSWER = 直接部署")
    check("discarded" in out,
          "明说 CANCEL = 丢弃草稿")
    check("Do NOT call create_new_skill" in out,
          "⭐⭐ 明令禁止对已有待审的需求再调 create_new_skill（死循环的直接出口）")

    # ⚠️ 反向：没有审计时不许出现这一段，否则是恒真的废话 + 白烧 token
    print("\n[2] ⚠️ 只有澄清、没有审计时，这一段【不能】出现")
    out2 = _render([CLAR])
    check("[About the skill_audit items above]" not in out2,
          "⭐ 纯澄清场景不注入审计语义（前置条件：证明它是按需出现的）")
    check("[Open Interactions]" in out2 and CLAR.interaction_id in out2,
          "前置条件：澄清本身仍然正常注入（不是整块都没了）")


def t_same_name_superseded() -> None:
    """同名旧草稿在登记新草稿时被 SUPERSEDE（AST 验结构）。

    不跑真 kernel：那需要落盘 + 一整套 Command 环境，成本远大于收益。
    这条要钉的是"**登记时确实去收同名的旧条目了**"这个结构事实。
    """
    print("\n[3] 同名旧草稿被新草稿取代（一个文件名只能有一份待审）")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_rt_open_skill_audit"), None)
    check(fn is not None, "前置条件：找得到 _rt_open_skill_audit")
    if fn is None:
        return

    seg = ast.get_source_segment(src, fn) or ""
    calls = [c for c in ast.walk(ast.parse(seg.strip()))
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "_rt_close_interaction"]
    check(bool(calls), "登记审计时会去关别的交互")

    # 必须是 SUPERSEDE，不是 CANCEL —— 用户没取消任何东西，是 Nano 自己重写了一版
    sup = [c for c in calls
           if any(isinstance(a, ast.Attribute) and a.attr == "SUPERSEDE" for a in c.args)]
    check(bool(sup),
          "⭐ 用 SUPERSEDE 而不是 CANCEL（语义要对得上：不是用户取消，是被重写取代）")

    # 必须按 artifact_id（文件名）匹配 —— 文件名就是这个产物的身份
    check("artifact_id == filename" in seg,
          "⭐ 按 artifact_id（文件名）匹配旧草稿 —— 文件名就是产物身份")
    check("_old.interaction_id != iid" in seg,
          "⚠️ 排除刚登记的这条自己（否则新草稿开出来就被自己收掉）")

    # 收不掉不能让登记失败
    check("except Exception" in seg,
          "收拢失败不致命（最坏回到旧行为，不是数据损坏）")


def t_guard_reject_keeps_draft_reachable() -> None:
    """指纹守卫拒绝放行时，**不许把这条待审关掉**（实测）。

    ═══ 现场 ═══

    用户在编辑器里改了代码，然后说「把那个待审的 Skill 部署了吧」。
    守卫正确拒绝了（批准指的是旧版本）：

        [Interaction] 审计 int_8d73eb0b00 的 artifact 已变更，拒绝放行：
        artifact GetLocalIPv4Address 内容已变更（8c48e21acfe5f393 → c68a263f7633c8a0）

    但同一行代码还把交互 `SUPERSEDE` 掉了。于是：
      · **卡片消失**，而回复却说"再点一次「验证并应用」即可" ——
        那个弹窗的唯一入口就是刚被关掉的那张卡；
      · 用户改到一半的代码**再也拿不回来**；
      · `SUPERSEDED` 却没有任何后继者，状态本身是假的；
      · 下一轮 `[TOKEN-PLAN] core_tools=5` —— **没有 answer_open_interaction**
        （没有活跃交互就不给这个工具）。所以紧接着那句
        「改一下这个待审的代码」模型手里根本没有能改的东西，空转 5 轮。
        用户报的"卡片消失"和"回复不对"是**同一条因果链**。

    📌 判据：**拒绝一次操作 ≠ 结束这件事。**
       守卫要挡的是"这次批准指的是旧版本"，不是"这份草稿不要了"。

    正解：给当前这一版重新开一张待审卡（新卡登记会自动 SUPERSEDE 同名旧卡，
    于是"被取代"第一次真的成立，用户也立刻有了新入口）。
    """
    print("\n[4] ⭐⭐ 指纹拒绝时不许关掉待审（否则用户的编辑连同入口一起没了）")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_handle_answer_interaction"), None)
    check(fn is not None, "前置条件：找得到 _handle_answer_interaction")
    if fn is None:
        return
    seg = ast.get_source_segment(src, fn) or ""

    i = seg.find("if not _ok_art:")
    check(i != -1, "前置条件：守卫的拒绝分支还在（`if not _ok_art:`）")
    if i == -1:
        return
    # 取这个分支到下一个同级 if 之前
    j = seg.find("if relation ==", i)
    branch = seg[i:j if j != -1 else len(seg)]

    sub = ast.parse(branch.strip().replace("if not _ok_art:", "if True:"))
    closes = [c for c in ast.walk(sub)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "_rt_close_interaction"]
    check(not closes,
          "⭐⭐ 拒绝分支里【不再】调 _rt_close_interaction —— 草稿留着，入口留着",
          f"仍有 {len(closes)} 处关闭调用" if closes else "")

    reopens = [c for c in ast.walk(sub)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and c.func.id == "_rt_open_skill_audit"]
    check(bool(reopens),
          "⭐ 改为给当前这一版重开一张待审卡（新卡会 SUPERSEDE 同名旧卡）")
    check("except Exception" in branch,
          "⚠️ 重开失败时也不能把旧卡关掉 —— 宁可留一张会被拒的卡，"
          "也好过留一份够不着的代码")


def main() -> int:
    t_audit_semantics_present()
    t_same_name_superseded()
    t_guard_reject_keeps_draft_reachable()
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
