# -*- coding: utf-8 -*-
"""进度可见性：进度总线 + 三个载体 + 本地 Skill 交还合同。

背景：
    MCP 调用与本地 Skill 在回看时都只能给出「还没返回」，看不到任何进度。
    提出的方向是：**能不能用 Skill 的协议规范去解决**，而不是加一道运行时强制。

核实结论：
  🔴 **MCP 协议原生就有进度通知（`notifications/progress`），是我们没接** ——
     已装 SDK 的 `ClientSession.call_tool` 有 `progress_callback` 形参，
     `BaseSession.send_request` 见到它非空就自动注入 `_meta.progressToken`。
     📌 **一个能力没被调用，不等于它不存在。**
  ⭐ 本地 Skill 没有这个概念 → 按 用户的想法**写进 Skill 协议**
     （`BaseSkill.report_progress`）。
     📌 **一个由模型自己写的东西，它的协议就是给它的提示词。**
"""
import ast
import asyncio
import pathlib
import sys
import time as _time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import module_text  # noqa: E402

from core.runtime import progress as _prog_mod

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


def _src(rel: str) -> str:
    """`core/registry.py` 形式的相对路径 → 该模块（或拆分后的包）的源码。"""
    return module_text(rel[:-3].replace("/", "."))


def _code_only(rel: str) -> str:
    """剥掉所有 docstring 的源码。

    📌 一条「代码里不许出现 X」的断言，必须先剥掉非代码部分 ——
       越是把规则写清楚的代码，越容易被自己的解释判成违规（本项目栽过三次）。
    """
    tree = ast.parse(_src(rel))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            if (n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant)
                    and isinstance(n.body[0].value.value, str)):
                n.body = n.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


# ══════════════════════════════════════════════════════════════════════════
def t_bus_basics() -> None:
    print("\n[1] 总线：报什么就看到什么，没报就是空")
    from core.runtime import progress as P

    P.forget("r1")
    check(P.tail("r1") == "", "没人报过 → **空字符串**，不是「(无进度)」。"
          "📌 一句「我不知道」必须由知道这件事的那一层说 —— "
          "总线只报事实，措辞不属于它")
    check(P.has("r1") is False, "has() 同步反映「看不到」")

    P.report("r1", "step one")
    check("step one" in P.tail("r1"), "报了就看得到")

    P.report("r1", "downloading", progress=42, total=100)
    check("[42%] downloading" in P.tail("r1"), "有 progress + total → 折成百分比")

    P.report("r1", "chunk", progress=7)
    _t = P.tail("r1")
    check("[7] chunk" in _t and "700%" not in _t and "7%" not in _t,
          "⭐⭐ 有 progress **没有** total → **不许算百分比** —— "
          "📌 不许为一件还没结束的事记一个结论；分母未知就说不出比例", _t[-40:])

    P.report("r1", "same")
    P.report("r1", "same")
    P.report("r1", "same")
    check(P.tail("r1").count("same") == 1,
          "连续同一行只留一条（进度通知常常只改数字不改文字）")

    P.forget("r1")
    check(P.tail("r1") == "", "forget 之后看不到")


def t_bus_never_raises() -> None:
    print("\n[2] ⭐ 观测通道的故障不许变成被观测那件事的故障")
    from core.runtime import progress as P

    class _Bad:
        def __str__(self):
            raise RuntimeError("boom")

    for bad in (None, "", 12345):
        try:
            P.report(bad, "x")          # type: ignore[arg-type]
            P.report("ok", bad)         # type: ignore[arg-type]
            _ok = True
        except Exception as e:
            _ok = False
        check(_ok, f"report({bad!r}) 不抛异常")
    try:
        P.report("ok2", "x", progress=float("nan"), total=0)
        _ok = True
    except Exception:
        _ok = False
    check(_ok, "total=0 / NaN 也不抛（0 会被当成假值，不做除法）")
    P.forget("ok"); P.forget("ok2")


def t_bus_cap() -> None:
    print("\n[3] ref 数量有兜底上限，但兜底不免除显式收尾")
    from core.runtime import progress as P

    for i in range(P._MAX_REFS + 30):
        P.report(f"cap{i}", "x")
    check(len(P.live_refs()) <= P._MAX_REFS,
          f"不超过 _MAX_REFS（{P._MAX_REFS}）—— 无界增长会吃内存")
    check(P.tail("cap0") == "" and P.tail(f"cap{P._MAX_REFS + 29}") != "",
          "⭐ 丢的是**最老的**，留的是最近的 —— "
          "📌 丢头不丢尾：出问题的证据总在末尾")
    for i in range(P._MAX_REFS + 30):
        P.forget(f"cap{i}")

    _code = _code_only("core/orchestrator.py")
    check(_code.count("forget") >= 4,
          "⭐ orchestrator 在**每条**载体结束的路径上显式 forget —— "
          "📌 一个兜底上限的存在，不免除正常路径显式收尾的义务",
          f"{_code.count('forget')} 处")


def t_provider_reads_authority() -> None:
    print("\n[4] ⭐⭐ 已经拥有输出的载体走 provider（不抄第二份）")
    from core.runtime import progress as P

    _seen = []

    def _prov(ref):
        _seen.append(ref)
        return "AUTHORITY OUTPUT" if ref == "p1" else None

    P.register_provider("_t_test", _prov)
    P.report("p1", "bus copy")
    _t = P.tail("p1")
    check(_t == "AUTHORITY OUTPUT",
          "⭐⭐ provider 有值时**优先于**总线自己的缓冲 —— "
          "📌 **读权威，不读副本**（副本会和权威分叉，而分叉之后没人知道信哪个）",
          _t)
    P.report("p2", "only in bus")
    check("only in bus" in P.tail("p2"),
          "provider 返回 None 时退回总线缓冲（两种接入方式共存）")

    def _boom(ref):
        raise RuntimeError("provider exploded")

    P.register_provider("_t_boom", _boom)
    check("only in bus" in P.tail("p2"),
          "⭐ 一个 provider 炸了不影响别的 —— "
          "📌 观测通道的故障不许扩散")
    P._providers.pop("_t_test", None)
    P._providers.pop("_t_boom", None)
    P.forget("p1"); P.forget("p2")

    check("register_provider" in _code_only("core/os_layer/longcmd.py"),
          "⭐ longcmd 走的是 provider（它的 stdout 已经在 `_buf` 里，就是权威）—— "
          "📌 不许为一个已经存在的权威再存一份副本")


def t_contextvar_not_instance() -> None:
    print("\n[5] ⭐⭐⭐ 「本次调用的 ref」挂在调用上，不挂在对象上")
    from core.runtime import progress as P
    from core.schema import BaseSkill

    class _Slow(BaseSkill):
        def get_manifest(self):
            return {"name": "Slow", "description": "", "parameters": {}}

        async def run(self, tag=""):
            self.report_progress(f"working on {tag}")
            await asyncio.sleep(0.01)
            self.report_progress(f"done {tag}")
            return tag

    inst = _Slow()          # ⚠️ **一个实例**服务两次并发调用（registry 就是单例）

    async def _one(ref, tag):
        _tok = P.bind(ref)
        try:
            return await inst.run(tag=tag)
        finally:
            P.unbind(_tok)

    async def _both():
        return await asyncio.gather(_one("sA", "A"), _one("sB", "B"))

    asyncio.run(_both())
    _a, _b = P.tail("sA"), P.tail("sB")
    check("A" in _a and "B" not in _a and "B" in _b and "A" not in _b,
          "⭐⭐⭐ **两次并发调用的进度没有互相串味** —— "
          "🔴 写成 `self._progress_ref` 就会串（`registry.skills` 存的是单例，"
          "`core/registry.py` 的 `execute` 是 `self.skills.get(name)`）。"
          "📌 **一个单例上的「本次调用」状态，在并发下必然串味** —— "
          "这类状态只能挂在「调用」上，不能挂在「对象」上",
          f"A轨={_a!r} B轨={_b!r}")
    P.forget("sA"); P.forget("sB")

    check(P.current_ref() == "", "解绑后当前 ref 归零")
    inst.report_progress("nobody listening")
    check(True, "⭐ 没绑 ref 时静默丢弃（Skill 被单测直接调用时没有接收方）—— "
          "📌 一个观测通道在没有观测者时应该沉默，而不是报错")

    _reg = _code_only("core/registry.py")
    check("bind(" in _reg and "unbind(" in _reg,
          "registry.execute 里绑 + 解绑都在")
    _tree = ast.parse(_src("core/registry.py"))
    _exec = next(n for n in ast.walk(_tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "execute")
    _has_finally_unbind = any(
        isinstance(n, ast.Try) and n.finalbody
        and "unbind" in ast.unparse(ast.Module(body=n.finalbody, type_ignores=[]))
        for n in ast.walk(_exec))
    check(_has_finally_unbind,
          "⭐⭐ 解绑在 **finally** 里 —— "
          "🔴 只在正常返回路径上解绑的话，Skill 抛异常时这个 ref 会漏给"
          "同一上下文里后续的调用 → **下一个 Skill 的进度写进上一个的轨迹**。"
          "📌 一个「本次有效」的绑定，必须在每一条离开的路径上被还原")


def t_mcp_subscribes() -> None:
    print("\n[6] ⭐⭐⭐ MCP：协议原生进度通知**接上了**")
    import inspect
    from mcp import ClientSession

    check("progress_callback" in inspect.signature(ClientSession.call_tool).parameters,
          "⭐ 前提核实：已装 SDK 的 `call_tool` **确实**有 `progress_callback` —— "
          "🔴 我此前把「我们没接」记成了「MCP 没有进度通道」。"
          "📌 **一个能力没被调用，不等于它不存在** —— "
          "判断「有没有」要去看协议/SDK，不是去看我们的调用点")
    _send = inspect.getsource(
        __import__("mcp.shared.session", fromlist=["BaseSession"]).BaseSession.send_request)
    check("progressToken" in _send,
          "⭐ 传了回调，SDK 就自动注入 `_meta.progressToken`（服务器据此才推送）—— "
          "⚠️ 所以**不传回调 = 服务器根本不会发**，那不是「服务器不支持」")

    _mc = _code_only("core/mcp_client.py")
    check("progress_callback=" in _mc,
          "🔴→✅ `session.call_tool` 现在带上了 `progress_callback`")
    check("progress_ref" in _mc,
          "ref 一路串到调用点（manager.call → server.call_tool → 队列 → _serve）")
    _mc_tree = ast.parse(_src("core/mcp_client.py"))
    for _fn in ("call", "call_tool"):
        _nodes = [n for n in ast.walk(_mc_tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == _fn]
        _ok = any("progress_ref" in {a.arg for a in n.args.args + n.args.kwonlyargs}
                  for n in _nodes)
        check(_ok, f"`{_fn}()` 形参里有 progress_ref", f"{len(_nodes)} 个同名定义")
    check("_prog_bus" in _mc and "import" in _mc,
          "⚠️ `_prog_bus` 真的被导入了 —— "
          "📌 py_compile 通过不代表名字存在（这个漏过一次）")


def t_recheck_asks_the_bus() -> None:
    print("\n[7] ⭐⭐⭐ 回看问总线，不问某一个载体")
    _code = _code_only("core/orchestrator.py")
    check("progress as _pb3" in _code or "progress as _pb" in _code,
          "回看注入读 `core.runtime.progress`")
    check("longcmd as _lc3" not in _code,
          "🔴→✅ 不再直接问 longcmd —— "
          "那样 MCP 和 Skill 两条路**必然拿到空**，而代码上只看得出"
          "「这里读了命令的进度」，看不出「这里少了两个载体」。"
          "📌 **三个载体要被同一只眼睛看到，那个「看」的接口就该属于第三方**")

    check("knows nothing about its progress" not in _code,
          "⭐⭐ 交还的话里**不再无条件**说「运行时对进度一无所知」 —— "
          "📌 **一句「我不知道」在它其实知道的时候说出来，比不说更糟**："
          "模型会照着它做一次多余的探查，或者干脆放弃判断")
    check("cannot see its progress" in _code and "Do NOT claim you can see" in _code,
          "⭐ 但**真的看不到时必须明说看不到** + 禁止它假装看得见 —— "
          "📌 一个「进度」字段在没有进度时必须说「没有」")


def t_skill_handback() -> None:
    print("\n[8] ⭐⭐⭐ 本地 Skill 接进同一条交还合同（第三类载体）")
    _code = _code_only("core/orchestrator.py")
    _tree = ast.parse(_src("core/orchestrator.py"))

    check("async with self._tool_parallel_sem:\n" not in _src("core/orchestrator.py")
          or "_tool_parallel_sem.acquire()" in _code,
          "🔴→✅ Skill 分发不再是「无超时地 await 到底」 —— "
          "那比改造前的 `run_command`（30 秒会报错）还糟："
          "那是「错了」，这是**永远不回来**")
    check("_sk_ref" in _code and "_hand_back_long_task" in _code,
          "Skill 超阈值走**同一条**公共合同（不是新写一条）")
    check(_code.count("_hand_back_long_task(") >= 3,
          "⭐⭐⭐ 公共合同有**三个**接入点（MCP / 长命令 / 本地 Skill）—— "
          "📌 **一个机制如果只有一个接入点，那它可能不是机制，"
          "只是那一处的实现细节**",
          f"{_code.count('_hand_back_long_task(')} 处")

    check("_sem_held" in _code,
          "⭐⭐ 交还时**手动释放**前台并发位 —— "
          "📌 **一个操作的占位，在控制权被交回模型之后，应该由「后台合同」决定，"
          "而不再由「前台等待的耐心」决定**")
    _fin = [n for n in ast.walk(_tree)
            if isinstance(n, ast.Try) and n.finalbody
            and "_sem_held" in ast.unparse(ast.Module(body=n.finalbody, type_ignores=[]))]
    check(bool(_fin), "释放在 finally 里（Skill 抛异常也不会漏一个并发位）")

    # 交还分支刻意不收 attempt
    _handback_src = _code[_code.index("_sk_ref"):]
    _seg = _handback_src[:_handback_src.index("raw_result = _sk_task.result()")]
    check(".finish(" not in _seg,
          "⭐ 交还分支**不收** ActionAttempt —— "
          "📌 不许为一件还没结束的事记一个结论")


def t_consumer_is_shape_neutral() -> None:
    print("\n[9] 🔴🔴 消费端不许认识任何一种载体的形状")
    _app = _code_only("app.py")
    check("_txt, _err, _na, _srv = await" not in _app,
          "🔴→✅ 消费端**不再解 MCP 的四元组** —— "
          "那会让长命令和 Skill 完成的那一刻被解成 "
          "`too many values to unpack (expected 4)`，"
          "而那正好落在最典型的场景上（`pip install` 跑完）")
    check("long_task_handback" in _app,
          "事件名改成类型中立（`mcp_background_request` 会让人以为只有 MCP 走这里）—— "
          "📌 **一个「保留旧名字」的决定，必须同时检查那个事件的消费端还假设着"
          "什么。名字兼容 ≠ 形状兼容**")
    check("mcp_background_request" not in _app,
          "旧事件名在代码里已经不存在（注释里留着说明原因）")
    # ⚠️ `ast.unparse` 把字符串统一成单引号 —— 按双引号数会得 0。
    #    📌 一条在「规范化之后的源码」上做的断言，必须按规范化后的形状写。
    _orc = _code_only("core/orchestrator.py")
    check(_orc.count("'long_task_handback'") == 4,
          "⭐ 四个载体各自发同一个事件，各自把结果包成**文本**"
          "（MCP / 长命令 / 本地 Skill / [A4] Subagent，2026-08-20 加的第四个）—— "
          "📌 **形状转换要发生在知道形状的那一端**，"
          "不是让公共消费端认识每一种载体",
          f"{_orc.count(chr(34)+'long_task_handback'+chr(34))} 处")

    # CancelledError 与 Exception 分开接（否则「被终止」会被记成「失败」）
    _tree = ast.parse(_src("core/runtime/carriers.py"))
    _fn = next((n for n in ast.walk(_tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run"), None)
    check(_fn is not None, "载体的 `_run` 存在（后端 `core.runtime.carriers`）")
    if _fn is not None:
        _try = next(n for n in ast.walk(_fn) if isinstance(n, ast.Try))
        _h = [ast.unparse(h.type) for h in _try.handlers if h.type is not None]
        check("asyncio.CancelledError" in _h and "Exception" in _h
              and _h.index("asyncio.CancelledError") < _h.index("Exception"),
              "⭐⭐ `CancelledError` 单独先接 —— "
              "📌 **「这件事没成」和「这件事被停了」是两回事**，"
              "归错会让人去排查一个不存在的故障", str(_h))


def t_skill_protocol_teaches_it() -> None:
    print("\n[10] ⭐⭐⭐ 杠杆：Skill 协议就是给模型的提示词")
    import core.orchestrator as O

    _p = O._SKILL_PROTOCOL
    check("report_progress" in _p,
          "⭐⭐⭐ `report_progress` **写进了 Skill 开发协议** —— "
          "当初的方向：「能不能用 Skill 的协议规范来解决这个问题」。"
          "📌 **一个由模型自己写的东西，它的协议就是给它的提示词** —— "
          "这种场景下「把正确做法写进规范」比「加一道运行时强制」有效")
    check("look at how that Skill is doing" in _p or "LOOK AT" in _p,
          "⭐ 协议里说清了**为什么**要报（回看那一眼只能看到这个）—— "
          "不给理由的规范，模型会当成样板跳过")
    check("Never report a percentage you did not actually compute" in _p,
          "⭐ 禁止编百分比 —— 与 `report()` 里「分母未知不算比例」同一条纪律")
    check("Short Skills" in _p and "NOT call it" in _p,
          "⭐⭐ 明说**短 Skill 不要调** —— "
          "📌 刻意不强制：绝大多数 Skill 秒级返回，强制上报只制造噪音；"
          "而「可选」的前提是**缺了它时系统会说实话**")
    check("threading.Thread" in _p,
          "⭐ 把已知边界写给模型（裸线程里报不出来），而不是做一个"
          "「万能捕获」的全局变量 —— 那就退回串味了")

    _sch = _src("core/schema.py")
    check("def report_progress" in _sch, "`BaseSkill.report_progress` 存在")
    _tree = ast.parse(_sch)
    _rp = next(n for n in ast.walk(_tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "report_progress")
    check(not isinstance(_rp, ast.AsyncFunctionDef),
          "⭐ 它是**同步**方法 —— `await self.report_progress(...)` 会是个坑，"
          "而报进度本来就不该有等待语义")
    _body = ast.unparse(_rp)
    check("except Exception" in _body and "pass" in _body,
          "永不抛异常（协议里也这么写了）")




# ══════════════════════════════════════════════════════════════════════════
# [] 实测抓出来的：模型的 timeout 把前台等待关掉了
# ══════════════════════════════════════════════════════════════════════════
def t_cmd56_foreground_wait_is_system_owned() -> None:
    print("\n[11] 🔴🔴🔴 [cmd56] 前台等待是系统的，模型不许覆盖")
    import core.os_layer.executor_write as EW
    from core.os_layer import longcmd as L
    from core.runtime import inbox as IB

    _W = next(c for c in vars(EW).values()
              if isinstance(c, type) and hasattr(c, "_run_command_async"))
    w = object.__new__(_W)
    _SLOW, _SLOW_LONG = "ping -n 11 127.0.0.1", "ping -n 26 127.0.0.1"
    _refs = []

    _code = _code_only("core/os_layer/executor_write.py")
    check('params.get("timeout", self._FOREGROUND_WAIT_SEC)' not in _code
          and "params.get('timeout', self._FOREGROUND_WAIT_SEC)" not in _code,
          "🔴→✅ `_fg` **不再**从 `params['timeout']` 取 —— "
          "实测撞到过：一条 `timeout /t 80` 的命令，模型按旧 schema 描述"
          "（timeout = 「超时就杀掉」）填了 ≥80 的值，于是"
          "**「前台只等 45 秒」被模型自己关掉了** → 没交还 → 没开等待 → "
          "回看无从谈起 → 用户「算了别做了」等了 80 秒。"
          "**一个参数同时打掉了这次改造的三个成果。** "
          "📌 **一个参数的语义被改了，它的旧调用方会继续按旧语义传值** —— "
          "而当调用方是**模型**、读的是没更新的 schema 描述时，"
          "它填的「合理值」恰好是最坏的值")

    async def _run():
        _W._FOREGROUND_WAIT_SEC = 3
        _t0 = _time.time()
        r1 = await w._run_command_async({"command": _SLOW, "timeout": 120})
        _e1 = _time.time() - _t0
        _refs.append((r1.get("data") or {}).get("ref"))

        r2 = await w._run_command_async({"command": "echo hello"})

        _W._FOREGROUND_WAIT_SEC = 30

        async def _speak():
            await asyncio.sleep(0.8)
            IB._note_arrival()

        _t0 = _time.time()
        _, r3 = await asyncio.gather(_speak(),
                                     w._run_command_async({"command": _SLOW_LONG}))
        _e3 = _time.time() - _t0
        _refs.append((r3.get("data") or {}).get("ref"))

        _W._FOREGROUND_WAIT_SEC = 5
        r5 = await w._run_command_async({"command": _SLOW_LONG, "timeout": 1})
        _refs.append((r5.get("data") or {}).get("ref"))
        _W._FOREGROUND_WAIT_SEC = 45
        return r1, _e1, r2, r3, _e3, r5

    r1, e1, r2, r3, e3, r5 = asyncio.run(_run())

    check((r1.get("data") or {}).get("long_running") is True and 2.5 < e1 < 6.0,
          "⭐⭐⭐ 模型传 `timeout=120` 的 10 秒命令，前台仍然只等 3 秒就交还",
          f"{e1:.1f}s, long_running={(r1.get('data') or {}).get('long_running')}")
    check(r2.get("ok") and "hello" in (r2.get("data") or {}).get("output", "")
          and "long_running" not in (r2.get("data") or {}),
          "快命令仍走快路径，形状与旧实现一致（调用方不用改）")
    check((r3.get("data") or {}).get("long_running") is True and e3 < 4.0,
          "⭐⭐⭐ **等待期间用户说话 → 立刻交还**（前台耐心明明是 30s）—— "
          "📌 **前台等待的耐心，本质上是「现在没有比等它更值得做的事」**；"
          "用户一说话这个前提就不成立了。这不是「加一个中断」，"
          "是把那个耐心的真实条件写出来",
          f"{e3:.1f}s")
    check((r5.get("data") or {}).get("long_running") is True,
          "⭐ 模型传一个**比前台等待还短**的 timeout，交还这条路也不会被堵死 —— "
          "📌 一个「最多允许跑多久」的值如果小于「前台等多久」，"
          "交还就永远走不到，这个机制等于不存在")

    _p = _prog_mod.tail(_refs[1] or "", lines=4)
    check("still running" in _p and "elapsed" in _p,
          "⭐ 交还之后回看那一眼**看得见真实进度**（命令的 stdout）", _p[:60])

    for _r in _refs:
        _lc = L.get(_r or "")
        if _lc is not None:
            _lc.kill("test cleanup")
        _prog_mod.forget(_r or "")


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 74)
    print("进度可见性：总线 / 三载体 / Skill 协议")
    print("=" * 74)
    for _t in (t_bus_basics, t_bus_never_raises, t_bus_cap,
               t_provider_reads_authority, t_contextvar_not_instance,
               t_mcp_subscribes, t_recheck_asks_the_bus, t_skill_handback,
               t_consumer_is_shape_neutral, t_skill_protocol_teaches_it,
               t_cmd56_foreground_wait_is_system_owned):
        _t()
    print()
    print("=" * 74)
    print(f"结果: {_passed} passed, {len(_failed)} failed")
    if _failed:
        for f in _failed:
            print("  -", f)
    print("=" * 74)
    sys.exit(1 if _failed else 0)
