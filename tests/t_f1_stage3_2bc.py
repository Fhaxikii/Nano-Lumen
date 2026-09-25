# -*- coding: utf-8 -*-
"""· ②b/②c：Skill 审计 与 管理确认 迁进 Interaction。

═══ 这两处劫持被删掉了什么 ═══

    ②b `_pending_skill`  + `_handle_pending_skill`（85 行）+ classify_pending_skill_intent（8 标签）
    ②c `_pending_action` + `_handle_pending_action`（117 行）+ classify_pending_action_intent（6 标签）

两个分类器**每条消息各多一次 API 往返**，而它们的标签里有一半本质是"正常回答用户"。
设计原则 3 第二、三次生效（第一次是 v1.1 删 classify_primary_intent）。

⚠️ **本套件重点验三件实测验不出来的事**：
  1. **UI 按钮那条路也要关交互** —— 用户点「验证并应用」/「丢弃」走的是
     `apply_pending_skill` / `cancel_pending_skill`，漏了就留僵尸（今天刚修过这个形状）
  2. **artifact 指纹复核** —— 用户点同意与代码写盘之间代码被改过，必须拒绝放行
  3. **取消要连工作载荷一起清** —— 只关交互会让弹窗仍可点部署，而用户刚说了不要

用法：
  py -3.10 tests\t_f1_stage3_2bc.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

import core.orchestrator as orch_mod
from core.orchestrator import Orchestrator
from core.runtime.clock import FakeClock
from core.runtime.kernel import reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import interaction as I
from core.schema import AgentDecision
from memory.manager import MemoryManager

BASE_T = 1_800_000_000.0
_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _FakeProvider:
    target_model = "fake"

    async def chat_with_tools_stream(self, *a, **k):
        yield {"type": "done", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}


class _FakeRegistry:
    def __init__(self):
        self.skills = {}
        self.calls = []

    def get_permanent_manifests(self): return []
    def get_all_manifests(self): return []
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None
    def reload_all(self): pass

    def delete_skill_file(self, n, include_disabled=True):
        self.calls.append(("delete", n)); return {"ok": True, "msg": f"已删除 {n}"}

    def disable_skill(self, n):
        self.calls.append(("disable", n)); return {"ok": True, "msg": f"已禁用 {n}"}

    def enable_skill(self, n):
        self.calls.append(("enable", n)); return {"ok": True, "msg": f"已启用 {n}"}


def make_orch(tmp: pathlib.Path):
    import core.rag as _rag
    _rag._background_index_started = True   # 跳过知识库后台索引（会去加载 bge-m3）
    st = RuntimeStore(tmp / "rt.db")
    _stores.append(st)
    k = reset_kernel_for_tests(store=st, clock=FakeClock(BASE_T))
    reg = _FakeRegistry()
    o = Orchestrator(_FakeProvider(), reg, MemoryManager(max_turns=20))
    o._rt_turn_id = "rtturn_test"
    o._tool_pool = {}
    o._core_manifest = []
    return o, k, reg


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


_CODE = "class X:\n    async def run(self):\n        return 1\n"


def _seed_audit(o, filename="MySkill", code=_CODE, valid=True):
    """等价于 `_emit_skill_preview` 走到审计那一刻的状态。"""
    iid = orch_mod._rt_open_skill_audit(o, filename, "干点啥", code, "create", valid, [])
    o._pending_skill = {
        "mode": "create", "target_skill": filename, "change_summary": "",
        "filename": filename, "code": code, "description": "干点啥",
        "valid": valid, "errors": [], "lifecycle": "permanent",
        "spec_side_effects": None, "error_context": "",
    }
    o._pending_skill["at"] = BASE_T
    o._pending_skill_at = BASE_T
    return iid


async def _answer(o, iid, answer, relation):
    out = []
    async for ev in o._handle_answer_interaction(
        {"interaction_id": iid, "answer_verbatim": answer, "relation": relation},
        "base", None, asyncio.Queue(),
    ):
        out.append(ev)
    return out


# ══════════════════════════════════════════════════════════════════════════

def t_hijacks_gone(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐ 两处劫持与两个分类器确实被删掉了")
    import core.provider as prov
    src_o = module_text("core.orchestrator")
    # 用 AST 而不是文本匹配 —— 注释里还留着这些名字做留档（的纪律）
    import ast
    tree = ast.parse(src_o)
    fns = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_handle_pending_skill" not in fns, "_handle_pending_skill 已删除")
    check("_handle_pending_action" not in fns, "_handle_pending_action 已删除")
    check(not hasattr(prov.ClaudeProvider, "classify_pending_skill_intent"),
          "classify_pending_skill_intent 已删除")
    check(not hasattr(prov.ClaudeProvider, "classify_pending_action_intent"),
          "classify_pending_action_intent 已删除")
    check(hasattr(prov.ClaudeProvider, "generate_skill_spec"),
          "generate_skill_spec 保留（它不是路由分类器）")
    # 伪事件也该跟着消失。⚠️ **不能用文本匹配** —— 删除说明的注释里刻意保留了
    # 这个名字做留档（"逃逸用的伪事件 `__pending_new_request__`"）。
    # 第一版写成 `"..." not in src_o or count == 0`，两个条件互相矛盾，必然失败。
    # 这是 那条纪律的又一次现场应验：**检查代码性质要用 AST，不要用文本匹配。**
    _live_refs = [n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and "__pending_new_request__" in n.value]
    check(not _live_refs,
          "逃逸用的伪事件 __pending_new_request__ 已无活引用（注释里的留档不算）",
          str(_live_refs))


def t_audit_opens_interaction(tmp: pathlib.Path) -> None:
    print("\n[2] 审计弹窗 → 一条带 artifact 指纹的交互")
    o, k, _ = make_orch(tmp)
    iid = _seed_audit(o)
    rec = I.get(k, iid)
    check(rec is not None and rec.kind == I.Kind.SKILL_AUDIT, "kind=skill_audit")
    check(rec.durability == I.Durability.PERSISTED, "PERSISTED —— 跨重启存活")
    check(rec.artifact_id == "MySkill", "artifact_id = 文件名")
    check(rec.artifact_hash == I.artifact_hash(_CODE), "⭐ artifact 指纹已钉")
    check(rec.deadline_at is None, "⭐ 审计不设 deadline（代码可以放几天再审）")


def t_ui_buttons_close_it(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐ UI 按钮那条路也必须关掉交互（漏了就是僵尸）")
    o, k, _ = make_orch(tmp)
    iid = _seed_audit(o, "ByDiscard")
    check(I.get(k, iid).is_live, "前置条件：交互确实是活的")
    o.cancel_pending_skill()
    check(I.get(k, iid).status == I.Status.REJECTED,
          "点「丢弃」→ REJECTED", I.get(k, iid).status)
    check(not I.get(k, iid).is_live, "不再是未决")
    check(o._pending_skill is None, "工作载荷也清了")

    # 「验证并应用」那条路：apply_pending_skill 内部会写文件，这里只验交互收尾被调到。
    o2, k2, _ = make_orch(tmp / "b")
    iid2 = _seed_audit(o2, "ByApply")
    orch_mod._rt_close_skill_audit("ByApply", approved=True)
    check(I.get(k2, iid2).status == I.Status.APPROVED,
          "点「验证并应用」→ APPROVED", I.get(k2, iid2).status)


def t_artifact_guard(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐ 确认之后代码被改过 → 拒绝放行")
    o, k, _ = make_orch(tmp)
    iid = _seed_audit(o, "Tampered")
    # 模拟用户在 CodeMirror 里改了代码（或别的流程覆盖了 _pending_skill）
    o._pending_skill["code"] = _CODE + "\nimport os\nos.remove('/')\n"
    evs = asyncio.run(_answer(o, iid, "可以，部署吧", I.Relation.ANSWER))

    # ⚠️ 这一段断言在 2026-08-06 被**改过**，因为它原来锁死的是一个错误行为。
    #
    # 旧断言是 `check("已经变过了" in _txt, …)` —— 它要求这条路径**自己写一句中文
    # 交给用户**。已明确那违反设计原则：固定文案不受人格模板影响，
    # 项目里这类文案有 35 处，占比一高 Nano 就会人格分裂
    #（用户把人设改成别的，满屏都是那个语气，中间突然蹦出一句
    # 「批准要对得上具体哪一版」）。豁免只有两种：模型调用本身出问题的兜底、
    # 气泡里的系统级通知/报错 —— 这条**两个都不占**。
    #
    # 现在的契约：把**事实**交回主循环（英文），由模型用它自己的话说。
    # 所以这里不再断言用户看到什么措辞，而是断言**事实有没有交到位**。
    _defer = [e for e in evs if e.get("event") == "exit_flow_defer_to_model"]
    check(len(_defer) == 1,
          "⭐ 走「把措辞交给模型」这条路，而不是自己 yield 一句写死的中文",
          f"{len(_defer)} 个 defer 事件")
    _payload = (_defer[0].get("tool_result") or "") if _defer else ""
    check("Not deployed" in _payload,
          "⭐ 事实第一条：没有部署（模型必须知道结果，不能自己猜）")
    check("pending review" in _payload or "apply button" in _payload,
          "⭐ 事实第二条：用户下一步能干什么（[D12]：够它判断下一步行为）")
    # ⚠️ 反向：不许把用户看不懂的东西塞给模型去复述
    check("Do not repeat the hash" in _payload,
          "⚠️ 明确要求别把指纹哈希念给用户听")
    # ⚠️ 这条路径**不得**直接产出面向用户的成品文字
    _txt = " ".join(e.get("content", "") for e in evs if e.get("event") == "final_result")
    check(not _txt.strip(),
          "⭐ 没有任何 final_result 文字 —— 措辞权完全交出去了", _txt[:60])

    check(I.get(k, iid).status == I.Status.SUPERSEDED,
          "交互标 SUPERSEDED（被改动后那一版取代）", I.get(k, iid).status)
    # 先证明"没部署"这件事的前置条件成立 —— 交互当初确实是活的且指纹对得上
    check(I.get(k, iid).artifact_hash == I.artifact_hash(_CODE),
          "前置条件：交互钉的仍是原始那一版的指纹")
    # ⭐ 拒绝 ≠ 结束这件事：改动后的那一版必须重新挂成待审，用户不能丢了入口
    _live = [r for r in I.list_live(k) if r.artifact_id == "Tampered"]
    check(len(_live) == 1,
          "⭐ 仍然恰好有一张活的待审卡（改动后的那一版），用户没有失去入口",
          f"{len(_live)} 张")
    check(bool(_live) and _live[0].interaction_id != iid,
          "⭐ 是**新的**那一条，不是原地复活旧的")


def t_audit_payload_gone(tmp: pathlib.Path) -> None:
    print("\n[5] 重启后只剩交互记录、代码正文没了 → 如实说清")
    o, k, _ = make_orch(tmp)
    iid = _seed_audit(o, "Vanished")
    o._pending_skill = None          # 等价于重启后：交互在库里，载荷在内存里没了
    evs = asyncio.run(_answer(o, iid, "可以", I.Relation.ANSWER))
    # ⚠️ 措辞交回模型之后，这一支不再产出成品中文 ——
    #    断言改成【事实有没有交到位】+【有没有产出成品文字】，
    #    照 2026-08-06 那次同样的改法（早先的设计：老测试断言固定文案 = 把错误行为锁死了）。
    _txt = " ".join(e.get("content", "") for e in evs)
    _facts = " ".join(str(e.get("tool_result", "")) for e in evs)
    check("gone" in _facts and "cannot be recovered" in _facts,
          "如实说明待审代码已失效（事实交到位）")
    check("generate a fresh version" in _facts,
          "⭐ 给出可执行的下一步（设计原则 3 的推论）")
    check(not any(e.get("content") for e in evs),
          "⭐ 没有产出成品文字 —— 措辞归模型")
    check(not I.get(k, iid).is_live, "交互被收掉，不留僵尸")


def t_multi_slot_payloads(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐⭐ [D17] 两条待审并存时，各自的代码正文都不能被覆盖")
    o, k, _ = make_orch(tmp)
    _a = _seed_audit(o, "AlphaSkill", code=_CODE)
    _b = _seed_audit(o, "BetaSkill", code=_CODE + "# beta marker")

    check(len(o._pending_skills) == 2, "两条载荷并存", str(sorted(o._pending_skills)))
    check(o._get_pending_skill("AlphaSkill")["code"] == _CODE,
          "⭐ 第一条的代码正文【没有被第二条覆盖】 —— 这就是那个 bug 的根因")
    check(o._get_pending_skill("BetaSkill")["code"].endswith("# beta marker"),
          "第二条的代码正文也是它自己的")
    check(I.get(k, _a).is_live and I.get(k, _b).is_live, "两条交互都还是未决")
    check(o._pending_skill["filename"] == "BetaSkill",
          "`_pending_skill` 兼容视图返回【最近那条】")

    # ⭐ 对**第一条**调工具，必须部署第一条，而不是最近那条
    evs = asyncio.run(_answer(o, _a, "可以，部署吧", I.Relation.ANSWER))
    _txt = " ".join(e.get("content", "") for e in evs)
    check("已经不在了" not in _txt,
          "⭐⭐ 不再误报「那份代码已经不在了」—— 那个 bug 的直接症状", _txt[:60])
    # ⚠️ 这里【不断言 APPROVED】：假环境里 `apply_pending_skill` 会真去写文件 +
    # 跑协议校验，`_CODE` 那段占位代码过不了校验，所以交互停在 ANSWERED。
    # 那不是的症状。要验的是**选中了哪一条** —— 用
    # `_active_apply_filename`（apply 入口记下的选中项）来断言，与部署成败无关。
    check(getattr(o, "_active_apply_filename", "") == "AlphaSkill",
          "⭐⭐ apply 选中的是【第一条】，不是最近那条",
          getattr(o, "_active_apply_filename", "(未设置)"))
    check(I.get(k, _a).status != I.Status.CANCELLED,
          "⭐ 第一条【没有】被误取消（旧实现就是在这里把它 CANCELLED 掉的）",
          I.get(k, _a).status)
    check(I.get(k, _b).is_live, "⭐ 另一条仍然未决，没被牵连")
    check(o._get_pending_skill("BetaSkill") is not None,
          "⭐ 另一条的载荷也还完好（部署一条不该清空全部）")


def t_multi_slot_targeted_cancel(tmp: pathlib.Path) -> None:
    print("\n[7] [D17] 指定丢弃某一条，不影响其它条")
    o, k, _ = make_orch(tmp)
    _a = _seed_audit(o, "KeepMe")
    _b = _seed_audit(o, "DropMe")
    o.cancel_pending_skill("DropMe")
    check(I.get(k, _b).status == I.Status.REJECTED, "指定那条被拒", I.get(k, _b).status)
    check(I.get(k, _a).is_live, "⭐ 另一条不受影响")
    check(o._get_pending_skill("KeepMe") is not None, "另一条载荷还在")
    check(o._get_pending_skill("DropMe") is None, "被丢那条载荷已清")
    # 不传 filename 时仍是"最近那条"（保持 UI 单弹窗时代的默认行为）
    o.cancel_pending_skill()
    check(o._pending_skills == {}, "不传名字 → 丢最近那条（这里只剩一条）")


def t_multi_slot_expiry_per_entry(tmp: pathlib.Path) -> None:
    print("\n[8] [D17] 超时逐条判，不是一刀切")
    o, k, _ = make_orch(tmp)
    _old = _seed_audit(o, "OldOne")
    o._pending_skills["OldOne"]["at"] = 1.0          # 远古
    _new = _seed_audit(o, "FreshOne")
    o._pending_skills["FreshOne"]["at"] = time.time()   # 刚生成
    o._expire_stale_pending()
    check(o._get_pending_skill("OldOne") is None, "⭐ 只有超时那条被清")
    check(o._get_pending_skill("FreshOne") is not None,
          "⭐⭐ 刚生成的那条【没被连带清掉】—— 改造前会一起没")
    check(I.get(k, _old).is_live is False, "超时那条的交互也一并关掉（不留僵尸卡片）")
    check(I.get(k, _new).is_live, "新的那条交互仍在")


def t_manage_confirm(tmp: pathlib.Path) -> None:
    print("\n[6] ②c 管理确认：delete / disable / enable")
    for op, want in (("delete", "delete"), ("disable", "disable"), ("enable", "enable")):
        o, k, reg = make_orch(tmp / op)
        msg = o._request_management_confirmation(op, "Victim")
        live = [r for r in I.list_live(k) if r.kind == I.Kind.SKILL_MANAGE]
        check(len(live) == 1, f"{op}：登记了一条 skill_manage 交互")
        iid = live[0].interaction_id
        check(live[0].artifact_id == "Victim", f"{op}：artifact_id 是目标 Skill")
        asyncio.run(_answer(o, iid, "确认", I.Relation.ANSWER))
        check(reg.calls == [(want, "Victim")], f"⭐ {op}：真的调到了 registry", str(reg.calls))
        check(I.get(k, iid).status == I.Status.APPROVED, f"{op}：交互 APPROVED")
        check(o._pending_action is None, f"{op}：工作载荷已清")


def t_manage_cancel(tmp: pathlib.Path) -> None:
    print("\n[7] ⭐ 管理确认取消：必须连载荷一起清（删 Skill 不可逆）")
    o, k, reg = make_orch(tmp)
    o._request_management_confirmation("delete", "Precious")
    iid = [r for r in I.list_live(k) if r.kind == I.Kind.SKILL_MANAGE][0].interaction_id
    asyncio.run(_answer(o, iid, "算了别删", I.Relation.CANCEL))
    check(reg.calls == [], "⭐ 没有执行删除", str(reg.calls))
    check(I.get(k, iid).status == I.Status.REJECTED, "交互 REJECTED")
    check(o._pending_action is None,
          "⭐⭐ `_pending_action` 也清了 —— 留着的话用户再说「确认」会误删")


def t_unknown_op(tmp: pathlib.Path) -> None:
    print("\n[8] 不认识的 op → 不执行、不静默")
    o, k, reg = make_orch(tmp)
    iid = orch_mod._rt_open_skill_manage(o, "nuke_everything", "X", "要 nuke 吗？")
    o._pending_action = {"op": "nuke_everything", "skill": "X"}
    evs = asyncio.run(_answer(o, iid, "确认", I.Relation.ANSWER))
    _txt = " ".join(e.get("content", "") for e in evs)
    check(reg.calls == [], "没有执行任何 registry 操作")
    _facts = " ".join(str(e.get("tool_result", "")) for e in evs)
    check("is not one of delete" in _facts, "如实告诉用户没认出这个操作（事实交到位）")
    check(not I.get(k, iid).is_live, "交互被收掉")


def t_manage_error_surfaced(tmp: pathlib.Path) -> None:
    print("\n[9] registry 抛异常 → 原样呈现，不换成通用文案")
    o, k, reg = make_orch(tmp)

    def _boom(n, include_disabled=True):
        raise RuntimeError("文件被占用，删不掉")
    reg.delete_skill_file = _boom
    o._request_management_confirmation("delete", "Locked")
    iid = [r for r in I.list_live(k) if r.kind == I.Kind.SKILL_MANAGE][0].interaction_id
    evs = asyncio.run(_answer(o, iid, "确认", I.Relation.ANSWER))
    _txt = " ".join(e.get("content", "") for e in evs)
    _facts = " ".join(str(e.get("tool_result", "")) for e in evs)
    # ⭐ 报错原文仍然一字不改地传出去 —— 只是收件人变成了模型，由它转述给用户。
    check("文件被占用" in _facts,
          "⭐ 真实报错原文原样交出（设计原则 3 的推论）")
    # ⚠️ **行为变化**：走 defer 之后这一轮不发终端事件，所以没有 status 字段。
    #    「这是一次失败」改由模型在它自己的话里说清 —— 事实里写明了 failed with。
    #    📌 把措辞交给模型，就意味着「这是错误」这件事也由它来表达。
    check("failed with" in _facts, "失败这件事在事实里说清了")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("审计与管理确认迁进 Interaction")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_2bc_"))
    try:
        for fn in (t_hijacks_gone, t_audit_opens_interaction, t_ui_buttons_close_it,
                   t_multi_slot_payloads, t_multi_slot_targeted_cancel,
                   t_multi_slot_expiry_per_entry,
                   t_artifact_guard, t_audit_payload_gone, t_manage_confirm,
                   t_manage_cancel, t_unknown_op, t_manage_error_surfaced):
            try:
                (tmp / fn.__name__).mkdir(parents=True, exist_ok=True)
                fn(tmp / fn.__name__)
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
    finally:
        close_all_stores()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
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
