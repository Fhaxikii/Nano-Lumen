# -*- coding: utf-8 -*-
"""「回复这条」指向已关闭的交互 → 必须不注入（BUG10(b) 的根因）。

═══ 实测怎么发生的（+ 聊天记录.txt）═══

    17:26   用户点了澄清 int_192a0393ab 的「回复这条」
    17:46   那条澄清被草稿消化 → SUPERSEDED（已关闭）
    17:58   用户说「部署这个吧」
            → [Router] create_new_skill 元工具触发（requirement='部署这个吧'）
            → **新建了一个 Skill，而不是部署待审那个**

`_reply_target` 只有用户手点 ✕ 才清，于是 17:46 之后**每一轮**都还在注入：

    ⭐ The user explicitly marked this message as answering int_192a0393ab
      — Treat it as the answer to that item; do not pick a different one.

而 int_192a0393ab **不在下面那份清单里**。模型被
「点名指向一条不存在的交互」+「明令禁止改挑别的」夹住，
`answer_open_interaction` 无路可走，只能从别的工具里找出路 → `create_new_skill`。

📌 判据：**指向性的注入必须校验指向的东西还在。**
   一个悬空的"必须回答 X"比没有这句话更糟 —— 它把模型逼进死角。

日志侧的旁证：17:45 dynamic=2617 → 17:49 dynamic=2622。澄清就在这中间死掉，
如果那 182 字符的标记跟着目标一起消失，应该掉 182；它只动了 +5。

═══ 两边都要修 ═══

  · 模型侧（本文件 [1][2]）：`_build_open_interactions_injection` 过滤掉死目标。
  · UI 侧（本文件 [3]）：`取消引用` 的 ✕ **只长在目标那张卡片上**，
    目标一关闭卡片就没了 → 用户再也点不到它。所以列表重画时必须自动复位。
    **只有一个入口能撤销的状态，那个入口不许比状态本身先消失。**

用法：
  py -3.10 tests\cases\t_reply_target_stale.py
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
    """够 `_build_open_interactions_injection` 用的最小记录。"""

    def __init__(self, iid, kind, txt, ts):
        self.interaction_id, self.kind, self.prompt_text, self.created_at = iid, kind, txt, ts
        self.status, self.answer_verbatim = _it.Status.OPEN, ""


CLAR = "int_192a0393ab"    # 实测那条澄清（17:46 被 SUPERSEDED）
AUD1 = "int_684c01d08d"    # ExtractComputerIPAddress 审计
AUD2 = "int_4269ba24a8"    # GetDNSConfig 审计（17:57 登记）

MARK = "explicitly marked this message as answering"


def _render(live: list[Rec], reply_iid: str | None) -> str:
    patch_global("core.orchestrator", "_rt_live_interactions", lambda o: live)
    stub = types.SimpleNamespace(
        _reply_target={"iid": reply_iid, "q": "x"} if reply_iid else None)
    return om.Orchestrator._build_open_interactions_injection(stub)


def t_live_target_still_injected() -> None:
    """⚠️ 前置条件：先证明"目标活着时确实会注入"。
    少了这条，把整段标记删掉也能让下面那条恒真地绿。"""
    print("\n[1] ⭐ 前置条件：目标【还活着】时，指向必须照常注入")
    live = [Rec(CLAR, _it.Kind.SKILL_CLARIFICATION, "dsakdkasd 是什么意思？", 1.0)]
    out = _render(live, CLAR)
    check(MARK in out, "目标在清单里 → 注入了「明确指向」那段")
    check(CLAR in out.split("If several items")[0],
          "指向段里点了名（不是只在清单行里出现）")


def t_dead_target_not_injected() -> None:
    print("\n[2] ⭐⭐ 目标【已关闭】→ 绝不注入指向（BUG10(b) 的正解）")
    # 实测 17:58 的真实状态：澄清已 SUPERSEDED，清单里只剩两条审计
    live = [Rec(AUD1, _it.Kind.SKILL_AUDIT, "Review pending draft ExtractComputerIPAddress.", 100.0),
            Rec(AUD2, _it.Kind.SKILL_AUDIT, "Review pending draft GetDNSConfig.", 200.0)]
    out = _render(live, CLAR)

    check(MARK not in out,
          "⭐ 没有「必须回答 X」那段 —— 死目标不再把模型逼进死角")
    check(CLAR not in out,
          "已关闭的 id 完全不出现在注入里", f"含 {CLAR}" if CLAR in out else "")

    # 清单本身必须完好：不能因为过滤指向把整块注入弄丢
    check(AUD1 in out and AUD2 in out,
          "两条还活着的审计仍然在清单里（没有连带丢掉）")
    check("← NEWEST" in out,
          "最新标记仍在 → 模型仍能自己判断「部署这个吧」指哪条")

    # 反向：目标是活着的那条时，照常注入（证明过滤是按"活没活"而不是按 kind）
    out2 = _render(live, AUD2)
    check(MARK in out2 and AUD2 in out2.split("If several items")[0],
          "⭐ 换成活着的审计做目标 → 照常注入（过滤依据是存活，不是 kind）")


def t_ui_resets_when_target_closed() -> None:
    """UI 侧：待办列表重画时，目标不在了就自动复位。

    用 AST 验，不跑 NiceGUI —— 这条是结构约束（复位调用必须在那个函数里）。
    """
    print("\n[3] UI 侧：目标关闭后自动复位（否则用户点不到撤销入口）")
    src = module_text("app")
    tree = ast.parse(src)

    fn = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = ast.get_source_segment(src, n) or ""
            if "_pinned_snapshot" in body and "list_live" in body:
                fn = n
                break
    check(fn is not None, "前置条件：找得到重画待办卡片的那个函数")
    if fn is None:
        return

    seg = ast.get_source_segment(src, fn) or ""
    sub = ast.parse(seg.strip())
    calls = [c for c in ast.walk(sub)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "_set_reply_target"]
    check(bool(calls), "⭐ 函数里调了 _set_reply_target 做复位")
    check(any(len(c.args) == 1 and isinstance(c.args[0], ast.Constant)
              and c.args[0].value is None for c in calls),
          "复位传的是 None（清除，而不是改指向别的）")

    # 复位必须发生在【指纹提前 return 之前】，否则指纹没变时永远走不到
    i_reset = seg.index("_set_reply_target")
    i_snap = seg.index("_pinned_snapshot")
    check(i_reset < i_snap,
          "⭐ 复位在指纹比较【之前】—— 指纹里不含 _reply_target，"
          "放后面会被 early-return 跳过", f"reset@{i_reset} snapshot@{i_snap}")


def t_handoff_survives_send() -> None:
    """⭐⭐⭐ [2026-08-13 CMD63] 指向必须**活过发送动作**，模型才读得到。

    ═══ 实测═══

        22:33:39.127  [UI] 引用已发出（int_2f32640844）→ 引用态复位
        22:33:39.131  [TOKEN-PLAN] core_tools=6 (…answer_open_interaction)

    用户点 replay 引用一张 skill_audit 卡、说「部署这个吧」。UI 在渲染发送行时
    就把 `_reply_target` 清了，而注入函数 **4 毫秒之后**才来读 → 指向段一个字都没进
    prompt → 模型改去 `load_tools` 捞 `create_new_skill` → 进探索 → 探索里没有
    "部署"这个出口 → 内部故障路径 → 澄清待办也跟着不登记。
    ⭐ 用户报的两个"独立问题"是一条链，头在这里。

    📌 **一个「发出去就该消失」的状态，不该被清掉，该被【移交】。**
    """
    print("\n[4] ⭐⭐⭐ CMD63：指向活过发送动作（移交，不是清除）")
    AUD = "int_2f32640844"
    live = [Rec(AUD, _it.Kind.SKILL_AUDIT, "Review pending draft LocalIPExtractor.", 300.0)]

    # ── 真实时序：UI 按发送 → 移交 → orchestrator 建 prompt ───────────────
    patch_global("core.orchestrator", "_rt_live_interactions", lambda o: live)
    stub = types.SimpleNamespace(_reply_target={"iid": AUD, "q": "x"},
                                 _reply_target_turn=None)
    handed = om.Orchestrator.hand_off_reply_target(stub)

    check(handed == AUD, "hand_off 返回被移交的 iid（供 UI 打日志）", handed)
    check(stub._reply_target is None,
          "⭐ UI 侧那份确实空了 —— composer 提示符能立刻刷回普通态")
    check((stub._reply_target_turn or {}).get("iid") == AUD,
          "⭐ 但指向没丢，它被移交到了本轮快照上")

    out = om.Orchestrator._build_open_interactions_injection(stub)
    check(MARK in out and AUD in out.split("If several items")[0],
          "⭐⭐ **发送之后**建 prompt，指向段仍然注入 —— 这就是 CMD63 修掉的那一格")

    # ── 反向：证明这条断言不是恒真 ──────────────────────────────────
    # 旧行为（发送时直接清空、不移交）必须让它红。
    old = types.SimpleNamespace(_reply_target=None, _reply_target_turn=None)
    check(MARK not in om.Orchestrator._build_open_interactions_injection(old),
          "⚠️ 反向：两个字段都空时确实不注入（上一条不是恒真）")

    # ── 不经过 UI 的调用方仍然只设 `_reply_target`，那一路不能断 ──────────
    only_live = types.SimpleNamespace(_reply_target={"iid": AUD, "q": "x"},
                                      _reply_target_turn=None)
    check(MARK in om.Orchestrator._build_open_interactions_injection(only_live),
          "⚠️ 只设了 _reply_target（测试替身/将来别的入口）→ 仍然注入")


def t_send_path_hands_off_not_clears() -> None:
    """结构约束：发送路径**不许**再自己清状态。

    ⚠️ 这条必须限定在发送那个函数里 —— `refresh_pinned_interactions` 里那处
    `_set_reply_target(None)` 是**对的**（目标已关闭时撤销悬空指向），
    全文件粗暴禁会把它一起判红。
    📌 同：**要禁的是某个位置上的行为，不是某个名字。**
    """
    print("\n[5] ⭐⭐ 发送路径：移交，不清除")
    src = module_text("app")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "start_pipeline_task"), None)
    check(fn is not None, "前置条件：找得到发送路径 start_pipeline_task")
    if fn is None:
        return
    seg = ast.get_source_segment(src, fn) or ""
    sub = ast.parse(seg.strip())

    clears = [c for c in ast.walk(sub)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
              and c.func.attr == "_set_reply_target"
              and len(c.args) == 1 and isinstance(c.args[0], ast.Constant)
              and c.args[0].value is None]
    check(not clears,
          "⭐⭐ 发送路径里【没有】_set_reply_target(None) —— 清早了模型就读空",
          f"仍有 {len(clears)} 处" if clears else "")

    handoffs = [c for c in ast.walk(sub)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "hand_off_reply_target"]
    check(bool(handoffs), "⭐ 改成调 agent.hand_off_reply_target()（权威只有一份）")
    check("_refresh_reply_prompt" in seg,
          "⚠️ 仍然刷一次 composer 提示符 —— 移交之后显示要立刻跟上")

    # finally 侧：两个字段都要清，漏一个就粘到下一轮
    orch = module_text("core.orchestrator")
    otree = ast.parse(orch)
    hq = next((n for n in ast.walk(otree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "handle_query"), None)
    fin = [n for t in ast.walk(hq) if isinstance(t, ast.Try) and t.finalbody
           for n in t.finalbody] if hq else []
    cleared = {t.attr for n in fin for c in ast.walk(n)
               if isinstance(c, ast.Assign)
               for t in c.targets
               if isinstance(t, ast.Attribute)
               and isinstance(c.value, ast.Constant) and c.value.value is None}
    check("_reply_target" in cleared and "_reply_target_turn" in cleared,
          "⭐⭐ finally 里【两个字段都】置 None（漏 _reply_target_turn = 粘到下一轮）",
          f"实际清了 {sorted(cleared & {'_reply_target', '_reply_target_turn'})}")


def main() -> int:
    t_live_target_still_injected()
    t_dead_target_not_injected()
    t_ui_resets_when_target_closed()
    t_handoff_survives_send()
    t_send_path_hands_off_not_clears()
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
