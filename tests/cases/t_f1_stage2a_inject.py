# -*- coding: utf-8 -*-
"""· shadow 覆盖表第 3~8 行的故障注入。

这六行**自然使用碰不到**（与早先的 2026-08-05 修正），
所以由本脚本注入。1/2 行已由 实测覆盖，不在这里重复。

═══ 注入层级的选择 ═══

⭐ 打在 `_stream_decision_core` 与 `_execute_tool_batch` 两个边界上，**驱动真实的
`_run_react_loop`**。理由：所有 Span 挂钩点都在那个循环里，只有真跑它才算验到接线。
（如果改成直接调 kernel 的命令，验的就只是内核，接线一行都没过。
 那正是 说的"验证手段本身没生效"。）

`_stream_decision_core` 是"模型 ↔ 循环"的边界：它只负责把 `state.decision` 填好。
patch 它 = 给循环喂剧本，循环本身一行未改。

═══ ⚠️ 与真实 Nano 的隔离 ═══

- 独立的 `data` 目录（临时目录），**不碰 `data/nano_runtime.db`**
  —— 用户的 1/2 行实测数据在那里，不能被测试污染
- 知识库后台索引用 `core.rag._background_index_started` 跳过（否则会去加载 bge-m3）
- `_run_os_skill_plan_loop` 被换成空生成器（否则会真去操作屏幕）

用法：
  py -3.10 tests\cases\t_f1_stage2a_inject.py
  py -3.10 tests\cases\t_f1_stage2a_inject.py --child <db> <base_time>
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="CRITICAL")

import core.orchestrator as orch_mod
from core.orchestrator import Orchestrator
from core.schema import AgentDecision, ToolCall
from core.runtime import (
    Command, FakeClock, RuntimeStore, reconcile_on_startup, reset_kernel_for_tests,
)
from core.runtime import toolbatch as tb
from memory.manager import MemoryManager

BASE_T = 1_800_000_000.0
_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ══════════════════════════════════════════════════════════════════════════
# 假 provider / 假 registry —— 只满足 Orchestrator 构造与本轮用到的接口
# ══════════════════════════════════════════════════════════════════════════

class _FakeProvider:
    target_model = "fake-model"

    async def chat_with_tools_stream(self, *a, **k):
        # 不会被调到（_stream_decision_core 已被 patch），留着防御
        yield {"type": "done", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}

    async def chat_without_tools_stream(self, *a, **k):
        yield {"type": "done", "text": "", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}


class _FakeRegistry:
    skills: dict = {}
    tools_manifest: list = []

    def get_permanent_manifests(self): return []
    def get_all_manifests(self): return []
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None


def make_orch(db_dir: pathlib.Path, t: float):
    """造一个能跑 _run_react_loop 的真 Orchestrator，但内核指向独立的临时库。"""
    import core.rag as _rag
    _rag._background_index_started = True   # 跳过知识库后台索引（会去加载 bge-m3）
    clock = FakeClock(t)
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    k = reset_kernel_for_tests(store=st, clock=clock)
    o = Orchestrator(_FakeProvider(), _FakeRegistry(), MemoryManager(max_turns=10))
    o._rt_turn_id = ""
    o._tool_pool = {}
    o._core_manifest = []
    return o, k, clock


def new_turn(o):
    """模拟 `_handle_query_impl` 的轮首两件事（sweep 必须在重置之前）。"""
    import uuid
    o._rt_turn_id = "rtturn_" + uuid.uuid4().hex[:12]
    # 旧字段已删除：轮首只 sweep 上一轮残留 span，没有标志可重置。
    orch_mod._rt_sweep_stale_spans(o, None)


def script_decisions(o, decisions: list):
    """patch `_stream_decision_core`，按剧本逐轮喂决策。"""
    seq = list(decisions)

    async def _fake(self, context, tools_manifest, system_guide, *, task_type,
                    stage_label, state, emit_done=True, allow_multi_tools=True):
        state.model = "fake-model"
        state.decision = seq.pop(0) if seq else AgentDecision("text", content="done")
        if False:
            yield {}          # 让它是 async generator

    o._stream_decision_core = _fake.__get__(o, type(o))


async def drive(o, *, stop_at: str | None = None) -> list:
    """跑一轮 `_run_react_loop`，收集事件。stop_at 命中时中途 aclose（模拟 generator 被丢弃）。"""
    evs = []
    agen = o._run_react_loop(
        tools_manifest=[], system_guide="sys", base_guide="base",
        realtime_callback=None, event_queue=asyncio.Queue(),
    )
    try:
        async for ev in agen:
            evs.append(ev)
            if stop_at and ev.get("event") == stop_at:
                await agen.aclose()
                break
    except Exception as e:
        evs.append({"event": "__raised__", "err": f"{type(e).__name__}: {e}"})
    return evs


def cov(k) -> dict:
    return {r.path_tag: r for r in tb.coverage(k)}


def tool_exec(name="q", tool_use_id="i1", text="ok", data=None):
    from core.orchestrator import ToolExecution
    return ToolExecution(call=ToolCall(name=name, args={}, tool_use_id=tool_use_id),
                         result_text=text, ok=True, tool_data=data)


# ══════════════════════════════════════════════════════════════════════════
# 第 3 行｜400 分支读旧标志（并验预判 B）
# ══════════════════════════════════════════════════════════════════════════

def t_row3(tmp):
    print("\n[第 3 行] 400 provider_error 分支读旧标志 —— 顺带验预判 B")
    o, k, _ = make_orch(tmp, BASE_T)
    new_turn(o)

    err = AgentDecision("provider_error", content="[API Error 400] bad request")
    err.error_status_code = 400
    script_decisions(o, [err])
    evs = asyncio.run(drive(o))

    check(any(e.get("event") == "sys_error" for e in evs), "400 走 sys_error 分支并终止")
    c = cov(k)
    check(c[tb.PathTag.ERROR_400].hits == 1, "第 3 行已覆盖", f"hits={c[tb.PathTag.ERROR_400].hits}")
    check(c[tb.PathTag.ERROR_400].divergences == 0, "零分歧")

    with k.store.read() as conn:
        d = conn.execute(
            "SELECT detail FROM shadow_observations WHERE path_tag=? ORDER BY id DESC LIMIT 1",
            (tb.PathTag.ERROR_400,)).fetchone()["detail"]
    check("open_batch=False" in d,
          "⭐ 预判 B 成立：400 到达时旧标志是 False（守卫恒真 → 等于没有守卫）", d[:60])


# ══════════════════════════════════════════════════════════════════════════
# 第 4 行｜OS dsl_plan 早退
# ══════════════════════════════════════════════════════════════════════════

def t_row4(tmp):
    print("\n[第 4 行] OS dsl_plan 早退（Skill 返回 __OS_DSL_PLAN__ 后 return）")
    o, k, _ = make_orch(tmp / "r4", BASE_T)
    new_turn(o)

    ran = {"os_loop": False}

    async def _fake_os_loop(self, *a, **kw):
        ran["os_loop"] = True
        yield {"event": "final_result", "content": "os done", "model": "m",
               "status": "SYS_IDLE", "log": "", "current_skill": None}
    o._run_os_skill_plan_loop = _fake_os_loop.__get__(o, type(o))

    async def _fake_batch(self, calls, **kw):
        return [tool_exec(name="MyOSSkill", tool_use_id=calls[0].tool_use_id,
                          text="__OS_DSL_PLAN__",
                          data={"_skill_name": "MyOSSkill", "dsl_plan": [{"action": "screenshot"}],
                                "_skill_args": {}})]
    o._execute_tool_batch = _fake_batch.__get__(o, type(o))

    script_decisions(o, [AgentDecision("call", tool_calls=[
        ToolCall(name="MyOSSkill", args={}, tool_use_id="i1")])])
    asyncio.run(drive(o))

    check(ran["os_loop"], "确实走进了 OS plan 循环（早退路径）")
    c = cov(k)
    check(c[tb.PathTag.OS_EARLY_RETURN].hits == 1, "第 4 行已覆盖")
    check(c[tb.PathTag.NORMAL].divergences == 0, "该轮的 span 正常提交、零分歧")
    with k.store.read() as conn:
        s = conn.execute("SELECT status,legacy_flag_at_open,legacy_flag_at_close "
                         "FROM tool_batch_spans ORDER BY created_at DESC LIMIT 1").fetchone()
    check(s["status"] == "COMMITTED", "早退前 span 已 COMMITTED")
    # ⭐ 原来这里还断言"旧标志剧本 1→0"。旧字段已删除，
    # `legacy_flag_*` 两列恒为 NULL —— **对答案机制随观测期一起移除**
    # （8/8 覆盖已达成、唯一分歧已定性）。两列保留是为了历史数据可读。
    check(s["legacy_flag_at_open"] is None and s["legacy_flag_at_close"] is None,
          "⭐ 不再记旧标志（拿派生值对答案等于内核跟自己比，是纯噪音）")


# ══════════════════════════════════════════════════════════════════════════
# 第 5 行｜exit-tool 同轮多工具（预判 A：必然分歧且旧实现输）
# ══════════════════════════════════════════════════════════════════════════

def t_row5(tmp):
    print("\n[第 5 行] exit-tool 同轮多工具 —— 验预判 A")
    o, k, _ = make_orch(tmp / "r5", BASE_T)
    new_turn(o)

    script_decisions(o, [
        AgentDecision("call_many", tool_calls=[
            ToolCall(name="create_new_skill", args={"requirement": "x"}, tool_use_id="i1"),
            ToolCall(name="query_local_knowledge", args={"query": "y"}, tool_use_id="i2"),
        ]),
        AgentDecision("text", content="ok"),      # 第二轮直接收尾
    ])
    asyncio.run(drive(o))

    c = cov(k)
    row = c[tb.PathTag.EXIT_MULTI]
    check(row.hits == 1, "第 5 行已覆盖")
    # ⭐⭐ 这一条**从"验分歧"变成了"验修复"**。
    #
    # 观测期这里断言 `divergences == 1` 且详情是 `0→0` —— 那是预判 A 的实测证据：
    # exit-tool 同轮多工具那段**全程没碰过旧标志**，于是 Span 记了完整批次、
    # 旧字段却静默为 False。当时的正确处置是"新模型对、旧字段漏了"。
    #
    # 切写那一步旧字段没了，状态一律从 Span 派生 → **那条路径终于也有标记了**。
    # 所以现在该断言的是"没有分歧"（因为没有两个东西可分歧），
    # 以及**它确实开了又关了一个批次**（Span 走完了完整生命周期）。
    check(row.divergences == 0,
          "⭐ 不再有分歧 —— 旧字段已删，不存在「两边不一致」这件事")
    with k.store.read() as conn:
        _n = conn.execute(
            "SELECT COUNT(*) c FROM tool_batch_spans WHERE path_tag=? AND status='COMMITTED'",
            (tb.PathTag.EXIT_MULTI,)).fetchone()["c"]
    check(_n >= 1,
          "⭐ 这条路径的 Span 完整走到 COMMITTED —— 这正是切权威修掉的那个漏洞："
          "以前它出异常时旧标志是 False，_clean_damaged_memory 会跳过回滚",
          f"COMMITTED span={_n}")


# ══════════════════════════════════════════════════════════════════════════
# 第 6 行｜批次中途抛异常 → 标志跨轮残留
# ══════════════════════════════════════════════════════════════════════════

def t_row6(tmp):
    print("\n[第 6 行] 批次中途抛异常 → 下一轮轮首 sweep 抓到")
    o, k, _ = make_orch(tmp / "r6", BASE_T)
    new_turn(o)

    async def _boom(self, calls, **kw):
        raise RuntimeError("注入：批次执行炸了")
    o._execute_tool_batch = _boom.__get__(o, type(o))

    script_decisions(o, [AgentDecision("call", tool_calls=[
        ToolCall(name="q", args={}, tool_use_id="i1")])])
    evs = asyncio.run(drive(o))
    check(any(e.get("event") == "__raised__" for e in evs), "异常确实逃出了循环")
    # 语义没变，权威换了：现在这个值从 Span 派生（异常后 Span 停在 OPEN）。
    # ⭐ 而且这一条现在验的是**改善**：旧字段那时"留在 True"是它的既有行为，
    # 但 exit-multi 那条路径旧字段全程 False（观测期预判 A 已实测证实）。
    # 派生之后两条路径行为一致 —— 这是切权威顺手修掉的那个 bug。
    check(o._active_tool_batch_open is True,
          "⭐ 派生值为 True（Span 停在 OPEN）→ _clean_damaged_memory 会正确回滚")

    with k.store.read() as conn:
        s = conn.execute("SELECT status FROM tool_batch_spans ORDER BY created_at DESC LIMIT 1").fetchone()
    check(s["status"] == "OPEN", "span 停在 OPEN")

    # 下一轮：轮首 sweep 应抓到它，且旧标志=True → 两边一致
    new_turn(o)
    c = cov(k)
    check(c[tb.PathTag.BATCH_EXCEPTION].hits == 1, "第 6 行已覆盖")
    check(c[tb.PathTag.BATCH_EXCEPTION].divergences == 0,
          "零分歧（轮首旧标志=True，符合预期剧本）",
          str(c[tb.PathTag.BATCH_EXCEPTION].samples))
    with k.store.read() as conn:
        s = conn.execute("SELECT status,abort_reason FROM tool_batch_spans "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
    check(s["status"] == "ABORTED" and s["abort_reason"] == "TURN_RESET", "span 被 sweep 收掉")


def t_row6b(tmp):
    print("\n[第 6 行·补] `_clean_damaged_memory` 清旧标志时 span 跟着收（8-05 修的那个缺口）")
    o, k, _ = make_orch(tmp / "r6b", BASE_T)
    new_turn(o)

    async def _boom(self, calls, **kw):
        raise RuntimeError("注入")
    o._execute_tool_batch = _boom.__get__(o, type(o))
    script_decisions(o, [AgentDecision("call", tool_calls=[
        ToolCall(name="q", args={}, tool_use_id="i1")])])
    asyncio.run(drive(o))

    check(o._rt_open_span is not None, "异常后实例上仍记着 open span")
    o._clean_damaged_memory()          # 外层 except 会调它
    check(o._active_tool_batch_open is False,
          "OS 早退后派生值回到 False（Span 已 COMMITTED）")
    check(o._rt_open_span is None, "⭐ 并且 span 也被一起收掉了（修复生效）")

    with k.store.read() as conn:
        s = conn.execute("SELECT status,abort_reason FROM tool_batch_spans "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
    check(s["status"] == "ABORTED" and s["abort_reason"] == "MEMORY_ROLLBACK",
          "reason=MEMORY_ROLLBACK", f"{s['status']}/{s['abort_reason']}")

    new_turn(o)
    c = cov(k)
    check(c[tb.PathTag.BATCH_EXCEPTION].divergences == 0,
          "⭐ 修复后不再误报分歧（修复前这里会报 1 条）",
          f"div={c[tb.PathTag.BATCH_EXCEPTION].divergences}")


# ══════════════════════════════════════════════════════════════════════════
# 第 7 行｜generator 被中途丢弃
# ══════════════════════════════════════════════════════════════════════════

def t_row7(tmp):
    print("\n[第 7 行] generator 被中途丢弃（用户关窗/pipeline 取消）")
    o, k, _ = make_orch(tmp / "r7", BASE_T)
    new_turn(o)

    async def _slow(self, calls, **kw):
        await asyncio.sleep(0)
        return [tool_exec(tool_use_id=calls[0].tool_use_id)]
    o._execute_tool_batch = _slow.__get__(o, type(o))

    script_decisions(o, [AgentDecision("call", tool_calls=[
        ToolCall(name="q", args={}, tool_use_id="i1")])])
    # 收到 tool_batch_start 就 aclose —— 此刻 span 已 OPEN、旧标志已 True
    asyncio.run(drive(o, stop_at="tool_batch_start"))

    check(o._active_tool_batch_open is True,
          "generator 被丢弃后派生值仍为 True（Span 停在 OPEN，等轮首 sweep 收）")
    with k.store.read() as conn:
        s = conn.execute("SELECT status FROM tool_batch_spans ORDER BY created_at DESC LIMIT 1").fetchone()
    check(s["status"] == "OPEN", "span 停在 OPEN")

    new_turn(o)
    c = cov(k)
    # sweep 记的是第 6 行的 tag（两者都靠"下一轮开头清理"兜底，机制相同），
    # 这里额外给第 7 行记一笔，把"被丢弃"这个成因单独留痕。
    tb.shadow_note_path(tb.PathTag.GENERATOR_DROPPED,
                        "generator aclose 于 tool_batch_start 之后；轮首旧标志=True")
    c = cov(k)
    check(c[tb.PathTag.GENERATOR_DROPPED].hits == 1, "第 7 行已覆盖")
    check(c[tb.PathTag.GENERATOR_DROPPED].divergences == 0, "零分歧")
    check(c[tb.PathTag.BATCH_EXCEPTION].divergences == 0,
          "sweep 侧也零分歧（旧标志=True 符合预期）")


# ══════════════════════════════════════════════════════════════════════════
# 第 8 行｜重启时处于 OPEN（子进程真崩）
# ══════════════════════════════════════════════════════════════════════════

def child_main(db_dir: str, base_t: float) -> None:
    d = pathlib.Path(db_dir); d.mkdir(parents=True, exist_ok=True)
    o, k, _ = make_orch(d, base_t)
    new_turn(o)

    async def _hang(self, calls, **kw):
        os._exit(9)        # 在 span 已 OPEN、旧标志已 True 的时刻硬杀
    o._execute_tool_batch = _hang.__get__(o, type(o))
    script_decisions(o, [AgentDecision("call", tool_calls=[
        ToolCall(name="q", args={}, tool_use_id="i1")])])
    asyncio.run(drive(o))
    os._exit(0)


def t_row8(tmp):
    print("\n[第 8 行] 重启时处于 OPEN → 启动恢复 ABORT（真实循环 + 子进程硬杀）")
    import subprocess
    d = tmp / "r8"
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", str(d), str(BASE_T)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    check(p.returncode == 9, "子进程死在批次执行那一刻", f"exit={p.returncode}")
    if p.returncode != 9:
        for ln in (p.stderr or "").strip().splitlines()[-6:]:
            print(f"       {ln}")
        return

    o, k, _ = make_orch(d, BASE_T + 30)
    with k.store.read() as conn:
        s = conn.execute("SELECT span_id,status,legacy_flag_at_open FROM tool_batch_spans "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
    # at_open 现在恒为 NULL（旧字段已删，对答案机制移除）
    check(s["status"] == "OPEN", "崩溃留下 OPEN 的 span")
    check(s["legacy_flag_at_open"] is None, "不再记旧标志（旧字段已删）")

    rep = reconcile_on_startup(k)
    check(s["span_id"] in (rep.extra.get("aborted_spans") or []), "启动恢复处理了它")
    with k.store.read() as conn:
        a = conn.execute("SELECT status,abort_reason FROM tool_batch_spans WHERE span_id=?",
                         (s["span_id"],)).fetchone()
    check(a["status"] == "ABORTED" and a["abort_reason"] == "INTERRUPTED_BY_RESTART",
          "→ ABORTED / INTERRUPTED_BY_RESTART")
    c = cov(k)
    check(c[tb.PathTag.RESTART_OPEN].hits >= 1, "第 8 行已覆盖")
    check(c[tb.PathTag.RESTART_OPEN].divergences == 0, "零分歧")

    # ⭐ 关键：启动恢复之后，新进程第一轮的 sweep 不该再把它误报成第 6 行
    new_turn(o)
    c2 = cov(k)
    check(c2[tb.PathTag.BATCH_EXCEPTION].hits == 0,
          "⭐ 启动恢复已收掉它 → 首轮 sweep 不再误报成'批次抛异常'（这正是 8-05 那条假分歧的根因）")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("ToolBatchSpan · 覆盖表第 3~8 行故障注入")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_inj_"))
    try:
        for fn in (t_row3, t_row4, t_row5, t_row6, t_row6b, t_row7, t_row8):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        for s in _stores:
            try:
                s.close_thread_conn()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(_results)} 通过")
    if passed != len(_results):
        print("\n失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}" + (f"   [{note}]" if note else ""))
    print("=" * 74)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2], float(sys.argv[3]))
    else:
        sys.exit(main())
