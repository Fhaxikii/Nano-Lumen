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
    """这一轮的引用是 `_reply_target_turn`（调度器按消息设）；`_reply_target` 属于下一条消息。"""
    patch_global("core.orchestrator", "_rt_live_interactions", lambda o: live)
    stub = types.SimpleNamespace(
        _reply_target=None,
        _reply_target_turn={"iid": reply_iid, "q": "x"} if reply_iid else None)
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
    """目标关闭后自动复位（否则用户点不到撤销入口）。

    复位在后端算待审卡快照时做（`core.snapshots.pinned_state`，1.5 秒一次），不靠界面记得调。
    判据用全部未决交互：一条仍然 OPEN、只是不上卡的澄清不能被当成已关闭。
    """
    print("\n[3] 目标关闭后自动复位（后端算待审卡快照时）")
    from tests._patch import patch_global
    import core.runtime.kernel  # noqa: F401
    from core import snapshots as SN

    class _Agent:
        def __init__(self, iid):
            self._reply_target = {"iid": iid, "q": "?", "kind": "interaction"}

        def _get_pending_skill(self, _f):
            return None

    def _run(live, iid):
        ag = _Agent(iid)
        r1 = patch_global("core.runtime.interaction", "list_live", lambda _k, kind=None: live)
        r2 = patch_global("core.runtime.kernel", "get_kernel", lambda: None)
        try:
            snap = SN.pinned_state(ag)
        finally:
            r1()
            r2()
        return ag, snap

    def _rec(iid, kind):
        r = Rec(iid, kind, "x", 1.0)
        r.revision, r.artifact_id, r.needs_retry = 1, "", False
        return r

    live = [_rec(AUD1, _it.Kind.SKILL_AUDIT), _rec(AUD2, _it.Kind.SKILL_AUDIT)]
    ag, snap = _run(live, CLAR)
    check(ag._reply_target is None and snap["reply_target"] is None,
          "⭐ 目标已不在未决交互里 → 复位（后端清掉，快照里也没有）")
    check([d["interaction_id"] for d in snap["items"]] == [AUD1, AUD2],
          "复位不影响还活着的审计上卡")

    live2 = live + [_rec(CLAR, _it.Kind.SKILL_CLARIFICATION)]
    ag2, snap2 = _run(live2, CLAR)
    check(ag2._reply_target is not None and (snap2["reply_target"] or {}).get("iid") == CLAR,
          "⭐⭐ 目标仍然 OPEN、只是不上卡（澄清）→ **不复位**（判据用未过滤的全部交互）")
    check(CLAR not in [d["interaction_id"] for d in snap2["items"]],
          "澄清本身仍不上卡（显示范围只有审计）")


def t_quote_belongs_to_the_message() -> None:
    """引用属于发出的那条消息：发送时取走，由这条消息自己那一轮使用。

    发送时不能清空引用（注入读不到，模型会去新建 Skill），也不能直接写成「当前这一轮」的引用：
    消息可能排队，正在跑的上一轮会读到它并在收尾时清掉。所以发送时**取走**，随消息进调度器，
    轮到它时由调度器设为 `_reply_target_turn`（端到端的行为测试在 t_session_user）。

    原来这里断言「只设了 `_reply_target` 也要注入」（兜底读法）；那个兜底会把用户为下一条消息
    设的引用串进正在跑的这一轮，所以反过来：这一轮只认 `_reply_target_turn`。
    """
    print("\n[4] ⭐⭐⭐ 引用属于发出的那条消息（取走，不清除；这一轮只认自己的快照）")
    AUD = "int_2f32640844"
    live = [Rec(AUD, _it.Kind.SKILL_AUDIT, "Review pending draft LocalIPExtractor.", 300.0)]
    patch_global("core.orchestrator", "_rt_live_interactions", lambda o: live)

    # ── 发送：取走 ────────────────────────────────────────────────────
    stub = types.SimpleNamespace(_reply_target={"iid": AUD, "q": "x"},
                                 _reply_target_turn=None)
    taken = om.Orchestrator.take_reply_target(stub)
    check((taken or {}).get("iid") == AUD, "取走的就是用户点的那条（交给 submit 随消息走）", str(taken))
    check(stub._reply_target is None, "⭐ UI 侧那份空了 —— composer 提示符能立刻刷回普通态")
    check(stub._reply_target_turn is None, "⭐ 取走时不碰当前这一轮的引用（消息可能在排队）")

    # ── 这条消息自己的那一轮：引用生效 ──────────────────────────────────
    own = types.SimpleNamespace(_reply_target=None, _reply_target_turn=taken)
    out = om.Orchestrator._build_open_interactions_injection(own)
    check(MARK in out and AUD in out.split("If several items")[0],
          "⭐⭐ 这条消息那一轮建 prompt 时指向段注入")

    # ── 反向：没有引用时不注入（上一条不是恒真） ──────────────────────────
    none = types.SimpleNamespace(_reply_target=None, _reply_target_turn=None)
    check(MARK not in om.Orchestrator._build_open_interactions_injection(none),
          "⚠️ 反向：没有引用时确实不注入")

    # ── 只有「下一条消息的引用」时，这一轮不许用它 ─────────────────────────
    next_only = types.SimpleNamespace(_reply_target={"iid": AUD, "q": "x"},
                                      _reply_target_turn=None)
    check(MARK not in om.Orchestrator._build_open_interactions_injection(next_only),
          "⭐⭐ 用户在这一轮进行中为下一条消息设的引用，不会串进这一轮")


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

    takes = [c for c in ast.walk(sub)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "take_reply_target"]
    check(bool(takes), "⭐ 发送时取走引用（take_reply_target，权威只有一份）")
    submits = [c for c in ast.walk(sub)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "submit"
               and any(k.arg == "reply_target" for k in c.keywords)]
    check(bool(submits), "⭐⭐ 取走的引用随 api_turn.submit(reply_target=…) 交给调度器")
    check("_refresh_reply_prompt" in seg,
          "⚠️ 仍然刷一次 composer 提示符 —— 取走之后显示要立刻跟上")

    # finally 侧：只清这一轮的引用。原来断言「两个字段都清」——那会把用户在这一轮进行中
    # 为下一条消息设的 `_reply_target` 一起清掉（6b 完整实机 D3：引用了审计卡说「部署吧」，
    # 却因为上一轮收尾把引用清了而去新建 Skill）。
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
    check("_reply_target_turn" in cleared,
          "⭐⭐ finally 里清掉本轮的引用（漏了就粘到下一轮）", str(sorted(cleared)))
    check("_reply_target" not in cleared,
          "⭐⭐ finally 里不碰 `_reply_target`（那是下一条消息的引用）", str(sorted(cleared)))


def main() -> int:
    t_live_target_still_injected()
    t_dead_target_not_injected()
    t_ui_resets_when_target_closed()
    t_quote_belongs_to_the_message()
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
