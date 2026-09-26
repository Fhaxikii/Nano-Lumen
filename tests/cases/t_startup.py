# -*- coding: utf-8 -*-
"""启动呈现、健康状态消费、Skill 热重载、初始化进度都由后端发事件（S6-6b 业务状态下沉第 7 步）。

- 启动呈现（`core.startup.present_startup`）按顺序：崩溃留痕（故障卡）→ 关软件时未发的用户消息
  （`unsent_message`，呈现后出队；唤醒意图只丢弃）→ 「我重启前还挂着在等」→ 续做询问（模型生成；
  生成失败不说）。系统陈述事实在前、Nano 开口在后。
- 健康消费（`health_tick`）：给模型记所有转移；只有「不可用」出卡，多项合并成一张；
  恢复 → `faults_recovered`（界面撤卡）。
- Skill 热重载（`core.skill_watch`）：监听线程只做标记，心跳里合并重载一次并发 `skills_reloaded`；
  自己写文件时可抑制。
- 初始化进度（`watch_init_progress`）：`init_facts` → 每个新阶段 `init_stage` → `init_ready`。
- 界面只渲染：上述事件的处理分支都在 `_handle_out_of_turn`。

测试不碰真实 data/：内核用临时库，崩溃留痕 / 健康登记表 / RAG / 注册表都用替身。

用法：
  py -3.10 tests\\cases\\t_startup.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core import startup as ST  # noqa: E402
from core import skill_watch as SW  # noqa: E402
from core.runtime import events as E  # noqa: E402
from core.runtime import inbox as IB  # noqa: E402
from core.runtime import task as T  # noqa: E402
from core.runtime import waitcond as W  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(1_700_000_000.0))


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class _Patch:
    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.orig = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *a):
        setattr(self.obj, self.name, self.orig)


class _Provider:
    def __init__(self, reply="", fail=False):
        self.reply, self.fail, self.calls = reply, fail, 0

    async def chat_without_tools(self, context, system_guide=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("API down")
        return self.reply, None


class _Agent:
    def __init__(self, provider):
        self.provider = provider


def t_startup_order(tmp):
    print("\n▶ 启动呈现：顺序与内容")
    make_kernel(tmp / "a")
    from core import crash_journal as CJ
    marked = []
    IB.submit_user_message("上次没发出去的那句", {"had_image": True})
    IB.submit_wake_intent("wait_x", "timer")
    W.open_wait(reason="pip install torch", wake_on=["background"], bg_ref="cmd_1")
    ST.set_interrupted([{"task_id": "T1", "kind": "BACKGROUND_JOB", "goal": "批量改 timeout"}])

    async def run(provider, crash=True):
        E._reset_for_tests()
        q = E.subscribe()
        _recs = [{"id": "c1", "summary": "segfault in embed"}] if crash else []
        with _Patch(CJ, "startup_scan", lambda: _recs), \
                _Patch(CJ, "mark_presented", lambda ids: marked.extend(ids)):
            await ST.present_startup(_Agent(provider))
        out = _drain(q)
        E._reset_for_tests()
        return out

    prov = _Provider(reply="上次关掉的时候批量改 timeout 还没做完，要接着做吗？")
    out = asyncio.run(run(prov))
    evs = [e for _, e in out]
    kinds = [(e["event"], e.get("category", "")) for e in evs]
    check(all(t is None for t, _ in out), "全是轮外事件")
    check(kinds == [("chat_message", "fault"), ("unsent_message", ""),
                    ("chat_message", "speech"), ("chat_message", "speech")],
          "顺序：崩溃留痕 → 未发消息 → 重启前还在等 → 续做询问", str(kinds))
    check(evs[0].get("title") == "上次运行没有正常退出" and marked == ["c1"], "崩溃留痕出卡并标记已呈现")
    check(evs[1].get("text") == "上次没发出去的那句" and evs[1].get("had_image") is True,
          "未发消息原样呈现（带附件标记）")
    check(IB.list_unfinished() == [], "呈现之后出队（唤醒意图也丢弃）—— 不会被当成要执行的消息")
    check("pip install torch" in evs[2].get("body", ""), "活着的等待：告诉用户还在等什么")
    check(evs[3].get("body") == prov.reply and prov.calls == 1, "续做询问由模型生成")

    make_kernel(tmp / "b")
    ST.set_interrupted([{"task_id": "T1", "kind": "BACKGROUND_JOB", "goal": "批量改 timeout"}])
    prov2 = _Provider(fail=True)
    out2 = asyncio.run(run(prov2, crash=False))
    check(out2 == [] and prov2.calls == 1, "生成失败 → 一个字都不说（不拿写死的话顶上）", str(out2))
    ST.set_interrupted([])


class _State:
    def __init__(self, cap, status, fp="fp", presented=None):
        self.capability, self.status, self.fingerprint = cap, status, fp + cap
        self.user_message, self.recovery_hint = f"{cap} 坏了", f"修 {cap}"
        self.severity, self.generation, self.presented_at = "high", 1, presented

    def snapshot(self):
        return {"label": self.capability.upper()}


class _Trans:
    def __init__(self, kind, state):
        self.kind, self.state = kind, state


class _Health:
    def __init__(self, trans):
        self.trans = trans

    def drain_transitions(self):
        t, self.trans = self.trans, []
        return t

    def mark_presented(self, cap, gen):
        return True


def t_health():
    print("\n▶ 健康消费")
    from core import health as Hm
    sysev = []

    class _Sys:
        def add(self, m):
            sysev.append(m)

    trans = [_Trans(Hm.Transition.OPENED, _State("rag", Hm.Status.UNAVAILABLE)),
             _Trans(Hm.Transition.OPENED, _State("mcp", Hm.Status.UNAVAILABLE)),
             _Trans(Hm.Transition.OPENED, _State("vision", Hm.Status.DEGRADED)),
             _Trans(Hm.Transition.RECOVERED, _State("tts", "OK"))]
    h = _Health(trans)
    E._reset_for_tests()
    q = E.subscribe()
    with _Patch(Hm, "get_health", lambda: h), _Patch(Hm, "get_system_events", lambda: _Sys()):
        ST.health_tick()
        ST.health_tick()
    out = [e for _, e in _drain(q)]
    E._reset_for_tests()
    kinds = [e["event"] for e in out]
    check(kinds == ["faults_recovered", "chat_message"], "恢复先发撤卡，再发故障卡", str(kinds))
    check(out[0].get("capabilities") == ["tts"], "撤卡带上恢复的能力")
    card = out[1]
    check(card.get("category") == "fault" and card.get("title") == "检测到 2 项能力不可用"
          and sorted(card.get("capabilities")) == ["mcp", "rag"],
          "只有「不可用」出卡，两项合并成一张（降级不出卡）", str(card.get("title")))
    check(len(sysev) == 4, "给模型的系统事件记下所有转移（含降级与恢复）", str(len(sysev)))


def t_skill_watch():
    print("\n▶ Skill 热重载")
    SW._reset_for_tests()
    from core import registry as Rm
    reloads = []
    E._reset_for_tests()
    q = E.subscribe()
    with _Patch(Rm.registry, "reload_all", lambda: reloads.append(1)):
        check(SW.reload_tick() is False and not reloads, "没有标记不重载")
        ths = [threading.Thread(target=SW.request_reload) for _ in range(5)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        check(SW.reload_tick() is True and reloads == [1], "多次标记（可来自监听线程）合并成一次重载")
        check(SW.reload_tick() is False, "重载后标记清掉")
    out = [e for _, e in _drain(q)]
    E._reset_for_tests()
    check(out == [{"event": "skills_reloaded"}], "重载后通知界面刷新列表", str(out))
    SW.suppress(5)
    check(SW.suppressed(), "自己写文件时可抑制监听")
    SW._reset_for_tests()
    orch = S.module_text("core.orchestrator")
    check("request_skill_refresh" not in orch and orch.count("_skw.request_reload(") == 2,
          "管理操作完成后直接请求重载（原来的 `self.request_skill_refresh` 在 orchestrator 上不存在，静默不生效）")


def t_init_progress():
    print("\n▶ 初始化进度")
    from core import rag as R
    from core.registry import registry as REG
    stages = ["temp_cleaned:3"]
    ready = threading.Event()

    class _A:
        _rag_ready = ready

    async def run():
        E._reset_for_tests()
        q = E.subscribe()
        with _Patch(R, "get_init_stage_log", lambda: list(stages)), \
                _Patch(R, "get_stats", lambda: {"total_chunks": 42}), \
                _Patch(REG, "get_all_manifests", lambda: [1, 2, 3]):
            task = asyncio.ensure_future(ST.watch_init_progress(_A(), poll=0.01))
            await asyncio.sleep(0.03)
            stages.append("embedder_ready")
            await asyncio.sleep(0.03)
            stages.append("done:1:2:0")
            ready.set()
            await task
        out = [e for _, e in _drain(q)]
        E._reset_for_tests()
        return out
    out = asyncio.run(run())
    kinds = [e["event"] if e["event"] != "init_stage" else e["stage"] for e in out]
    check(kinds == ["init_facts", "temp_cleaned:3", "embedder_ready", "done:1:2:0", "init_ready"],
          "facts → 各阶段（每个只发一次，就绪前最后写入的也补上）→ ready", str(kinds))
    check(out[0].get("skill_count") == 3 and out[0].get("chunk_count") == 42, "facts 带真实数据")


def t_ui_wiring():
    print("\n▶ 界面只渲染")
    app = S.module_text("app")
    code = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    for k in ("unsent_message", "faults_recovered", "skills_reloaded", "init_facts", "chat_message"):
        check(f'"{k}"' in app.split("def _handle_out_of_turn")[1].split("\n    def ")[0],
              f"`{k}` 由界面的轮外事件分支处理")
    for gone in ("def _health_consumer_tick", "def _crash_journal_tick", "def _startup_present_unsent",
                 "def _startup_resume_offer", "_restore_suspensions", "class SkillWatcher",
                 "def _consume_skill_refresh_request", "rag_engine.get_init_stage_log()",
                 "_STARTUP_INTERRUPTED"):
        check(gone not in code, f"界面不再有 `{gone}`")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_startup_order(tmp)
        t_health()
        t_skill_watch()
        t_init_progress()
        t_ui_wiring()
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
