# -*- coding: utf-8 -*-
"""临时执行通道 —— 「跑一段用完就扔的代码」这个出口（2026-08-26）。

═══ 它补的是哪个缺口 ═══

模型遇到「需要跑段代码，但不值得建 Skill」时，此前只剩三条路，**全是死路**：
  · 硬建永久 Skill —— `create_new_skill` **明令禁止**一次性用途，且污染 Skill 池
  · 走 `run_command` —— 语义是「操作电脑」不是「帮我算个东西」，
                        风险地板写死 3（每次弹窗）、`shell=True`、无隔离
  · 自己心算然后编 —— persona 明令禁止，**但这是阻力最小的那条路**
📌 记的那个形状：**模型需要的是一个出口，不是一个名字。**
   `create_new_skill` 禁止了一次性用途，**却没有给任何替代出口**。

═══ 设计里最该被守住的几条 ═══

⭐ **全程没有一样东西是为它新造的**（「直接跟正常 skill 的体感一致」）：
     扫描   `core.code_scan`          与 Skill 审计**同一份**
     词表   `code_scan.CONFIRM_LABELS` 与 Skill 弹窗**同一套中文**
     确认   `execution_confirm` 事件   与运行有副作用的 Skill **同一个弹窗**
     执行   `longcmd`                  长任务交还 / 落盘 / 回收全白拿
  🔴 原本设计的是「risk 1/2/3 + 白名单」——**那是在发明一套新东西**，
     而且用户要为同一件事学两种交互。

⚠️ **AST 扫描不是沙箱，是分类器。** `__import__("os").system(...)` 抓不到。
   📌 真正的边界是**用户看得见代码**，不是扫描结果。
   ⭐ 而这个风险水平**与正式 Skill 相同**（Skill 也是扫不出来就不弹），
      临时代码在可见性上**只多不少**：Skill 的代码用户只在创建时看过一次。

用法：
  py -3.10 tests\\t_c2_scratch_exec.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _code_only(module: str) -> str:
    src = module_text(module)
    try:
        return ast.unparse(ast.parse(src))
    except SyntaxError:
        return src


class _Q:
    def __init__(self):
        self.items = []

    async def put(self, x):
        self.items.append(x)


async def _call(code: str, purpose: str = "test", approve: bool = True,
                click: bool = True):
    """跑一次 handler，替用户点确认（如果弹了）。

    `click=False`：**谁都不点** —— 用来测「用户插话」那条路（没点按钮，
    而是说了句别的）。
    """
    from core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._LONG_TASK_HANDBACK_SEC = 30.0
    q = _Q()
    task = asyncio.create_task(
        Orchestrator._handle_run_scratch_code(
            o, {"code": code, "purpose": purpose}, "aid", event_queue=q))
    for _ in range(400):
        await asyncio.sleep(0.05)
        if click:
            for ev in q.items:
                if ev.get("event") == "execution_confirm" and not ev.get("_done"):
                    ev["_done"] = True
                    (ev["on_confirm"] if approve else ev["on_cancel"])()
        if task.done():
            break
    return await task, q.items


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
def t_name_has_no_ecosystem_owner() -> None:
    print("\n[1] 🔴🔴 名字不许撞生态（`WebSearch` 那个坑刚栽过）")
    import core.orchestrator as O
    m = O._RUN_SCRATCH_CODE_MANIFEST
    check(m["name"] == "run_scratch_code", "叫 run_scratch_code", m["name"])
    # 🔴 2026-08-25：那个 Skill 叫 `WebSearch` 时模型**每一次**都发空参数，
    #    只改名字就好了 —— 因为 Anthropic 有一个**服务端**同名工具（客户端不传参）。
    # 📌 模型对一个它「认识」的名字，会用记忆里的调用方式，而不是你给的 schema。
    for taken in ("code_execution", "python", "repl", "bash", "code_interpreter",
                  "run_python", "exec"):
        check(m["name"] != taken, f"不叫 `{taken}`（生态里已经属于别人）")
    check(set(m["parameters"]["required"]) == {"code", "purpose"},
          "code / purpose 都必填", str(m["parameters"]["required"]))


def t_no_side_effect_runs_straight() -> None:
    print("\n[2] ⭐ 无副作用 → 不弹窗，直接跑（与只读 Skill 一致）")
    r, evs = _run(_call("print(sum(range(101)))", "算 1..100"))
    check(not any(e.get("event") == "execution_confirm" for e in evs),
          "⭐⭐ 一个确认弹窗都没有")
    check("5050" in r, "结果对", r.replace("\n", " | ")[:60])
    # ⚠️ 读文件也不弹 —— 与 Skill 的 `file_read` 不需确认一致
    r2, evs2 = _run(_call("import io\nprint(len('abc'))", "读点东西"))
    check(not any(e.get("event") == "execution_confirm" for e in evs2),
          "⭐ `readonly` / `file_read` 那一类同样不打断")


def t_side_effect_uses_the_same_dialog() -> None:
    print("\n[3] ⭐⭐⭐ 有副作用 → 走【同一个】弹窗、【同一套】中文")
    code = "open('out.txt','w').write('hi')\nprint('written')"
    r, evs = _run(_call(code, "把结果写到文件"))
    ev = next((e for e in evs if e.get("event") == "execution_confirm"), None)
    check(ev is not None, "弹了 execution_confirm（与 Skill 同一个事件）")
    if ev is None:
        return
    from core.code_scan import CONFIRM_LABELS
    check(ev["side_effects"] == ["写入文件到磁盘"],
          "⭐⭐ 副作用文案与 Skill **一字不差**（不是 `file_write` 这种键）",
          str(ev["side_effects"]))
    check(all(x in CONFIRM_LABELS.values() for x in ev["side_effects"]),
          "⭐ 而且它就是从那张共用词表里取的")
    check(ev.get("preview_code") == code,
          "⭐⭐⭐ **代码带进了弹窗** —— 让用户知道自己在授权什么")
    check(ev.get("skill_name") == "把结果写到文件",
          "标题用 purpose，不是一个内部名字", str(ev.get("skill_name")))
    check("written" in r, "确认之后真的跑了")


def t_preview_is_readonly() -> None:
    print("\n[4] 🔴🔴 代码**另开一个只读窗**，授权窗保持与 Skill 一模一样")
    src = module_text("app")
    i = src.index("def _show_execution_confirm_dialog")
    j = src.index("# 操作栏", i)
    seg = src[i:j]

    # ⭐⭐ 2026-08-26 定的形状：**两个授权窗长得一模一样**，
    #    这个只多一个按钮；代码另开一个窗。
    #    🔴 第一版把 CodeMirror 内联进授权窗，一次实测暴露四个 UI 问题，
    #       而更根本的是**它把两种授权窗变成了两个长相**。
    check("width:420px" in seg,
          "⭐⭐⭐ 授权窗**恒定 420px** —— 与普通 Skill 授权窗**同一个长相**")
    check("_w = 720" not in seg and "{_w}px" not in seg,
          "⭐ 宽度不再随「有没有代码」变（那正是两个长相的来源）")
    check("_show_code_viewer_dialog" in seg, "⭐ 有「查看代码」按钮，指向独立的查看窗")
    check("nano-cm-host" not in seg,
          "⚠️ 授权窗里**不再挂 CodeMirror** —— 决策窗不该被一段长代码撑开")

    # ── 独立的只读查看窗 ──
    vi = src.index("def _show_code_viewer_dialog")
    vseg = src[vi:vi + 4200]
    check("readonly=True" in vseg, "⭐⭐⭐ 查看窗里 CodeMirror 以 **readonly=True** 挂载")
    # 🔴🔴 一个 class 决定四件事：顶部灰条 / 选中行高亮 / **左侧白条**（修过）/
    #    挂载时先压扁再撑开。漏掉它 = 一次性退回四个已经修好的问题。
    check("nano-cm-host" in vseg,
          "🔴🔴 挂载点带 `nano-cm-host` —— CM 的**全部**样式覆盖 scope 在它下面")
    check("nano-cm-readonly" in vseg,
          "⭐ 还带 `nano-cm-readonly`：只读态额外关掉选中行高亮与光标")
    check("只读" in vseg, "⚠️ 界面上**明说只读**（长得像编辑器的东西不说清会有人去改）")

    # ⭐ 不靠自觉：`_cm_init` 里 syncToPython 跟着 readonly 走
    #    （「可编辑就必须同步」那条不变量的另一半）—— 改了也传不回来。
    k = src.index("async def _cm_init")
    cm = src[k:k + 3000]
    check("syncToPython: {str(not readonly).lower()}" in cm,
          "⭐⭐ syncToPython 跟着 readonly 走 —— **改了也传不回来**，不靠自觉")
    # 🔴 理由留痕必须在（否则下一个人会「顺手」让它可编辑）
    check("授权就和" in vseg or "可编辑会让授权失去意义" in vseg,
          "🔴 留痕写清了为什么不许可编辑（风险类别是在【那段代码】上扫的）")

    # ⚠️ 只读态的 CSS 必须只关那两样，其余继承 —— 复制一份迟早分叉
    ci = src.index(".nano-cm-readonly .cm-activeLine")
    cseg = src[ci:ci + 1200]
    check("cm-cursor" in cseg and "background: transparent" in cseg,
          "⭐ 只读 CSS 只关掉「选中行高亮 + 光标」")
    check("--nano-cm-h" not in src,
          "⭐⭐ 那条「按行数算高度」的补丁**已经删掉** —— "
          "📌 代码另开窗之后它的前提就没了，而**一个前提没了的补丁本身就是债**")


def t_tool_card_always_shows_the_code() -> None:
    """⭐⭐ 事后可见性 —— 与弹窗那一半答的是**两个不同的问题**。"""
    print("\n[4b] ⭐⭐ 工具卡里**每一次**都能展开看到那段代码")
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    mans = {v["name"]: v for k, v in vars(O).items()
            if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    d = {x.name: x for x in build_builtin_definitions(mans)}["run_scratch_code"]
    code = "xs = [1, 2, 3]\nprint(sum(xs) / len(xs))"
    args_for_detail = {"purpose": "算平均值", "code": code}
    blocks = d.presentation.render_detail(args_for_detail, {"data": {"output": "2.0"}})
    labels = [b.label for b in blocks]
    check("运行的代码" in labels, "有「运行的代码」这一块", str(labels))
    _code_blk = next((b for b in blocks if b.label == "运行的代码"), None)
    check(_code_blk is not None and code.splitlines()[0] in _code_blk.body,
          "⭐⭐⭐ 代码**原样**可读 —— 不是 `default_detail` 那种 JSON 转义"
          "（满屏 \\n 的东西「有显示」但「看不懂」）")
    check("输出" in labels, "输出也在同一张卡里")

    # 🔴🔴 **实测抓到的**：账本给的 `result` 是 `ToolResultBlock` **对象**，
    #    而第一版只认 `str` / `dict` —— 两个分支都没命中，「输出」那块被
    #    **静默跳过**：卡上只有「目的」和「运行的代码」，而结果明明算出来了。
    # 📌 正确取法就在同文件的 `default_detail` 里（`getattr(result,"content")`）——
    #    **照着现成的来，别自己发明。**
    from core.schema import ToolResultBlock as _TRB
    _shapes = {
        "ToolResultBlock（账本，实际运行走这条）":
            _TRB(name="run_scratch_code", tool_use_id="t", content="Output:\n2.0"),
        "str（handler 直接返回）": "Output:\n2.0",
        "dict（内部结果）": {"data": {"output": "2.0"}},
    }
    for _tag, _r in _shapes.items():
        _lb = [b.label for b in d.presentation.render_detail(args_for_detail, _r)]
        check("输出" in _lb, f"⭐⭐ {_tag} 也能出「输出」", str(_lb))
    _lb_none = [b.label for b in d.presentation.render_detail(args_for_detail, None)]
    check("输出" not in _lb_none,
          "⚠️ 而还没落盘时**不硬造一个空「输出」**（说清是没有还是没取到）",
          str(_lb_none))
    _err = _TRB(name="x", tool_use_id="e", content="boom", is_error=True)
    _eb = {b.label: b.kind for b in d.presentation.render_detail(args_for_detail, _err)}
    check(_eb.get("错误") == "error", "⭐ 失败时标题是「错误」且标红", str(_eb))
    # 🔴 这一块存在的理由：**无副作用的代码不弹窗**，那这里就是它唯一的可见处。
    #    📌 少了它，一段没打断用户的代码就彻底不可见了。
    check(_code_blk is not None and _code_blk.kind == "code",
          "⚠️ 用的是标准 DetailBlock（跟别的工具卡同一套字体/颜色），"
          "**不是**弹窗那个高亮编辑器")


def t_failures_are_correctly_typed() -> None:
    print("\n[5] ⭐⭐ 三种失败必须**分得开**（[D12]：失败信息要正确）")
    r, evs = _run(_call("def f(:\n  pass", "坏代码"))
    check(not any(e.get("event") == "execution_confirm" for e in evs),
          "⭐ 语法错误在**执行前**就拦住，一个进程都没起")
    check("SyntaxError" in r and "does not parse" in r,
          "① 语法错 → 明说 parse 不了 + 行号", r.replace("\n", " | ")[:80])

    r2, _ = _run(_call("print(1/0)", "除零"))
    check("error in the code itself, not a problem with the tool" in r2,
          "⭐⭐ ② 代码报错 → **明说是代码的错，不是工具坏了** "
          "（两者对模型含义完全不同：一个该改代码，一个该换工具）")
    check("ZeroDivisionError" in r2, "而且把 traceback 给了它")

    # ⚠️ **必须把 `submit_seq` 钉住**，否则这条测的是环境不是代码：
    #    `wait_confirm_or_user_message` 在开始时取 `inbox.submit_seq()` 基线，
    #    真实 inbox 里只要有新消息进来，它就判成「用户插话」而走另一支。
    # 🔴 第一版没钉，于是拿到的是 `[Cancelled by the user speaking up]` ——
    #    📌 **代码分对了，是测试没让环境确定**。而它一开始看起来像个 bug。
    from core.runtime import inbox as _ib
    _orig_seq = _ib.submit_seq
    _ib.submit_seq = lambda: 0
    try:
        r3, _ = _run(_call("open('o.txt','w').write('x')", "写文件", approve=False))
    finally:
        _ib.submit_seq = _orig_seq
    check("did not approve" in r3 and "Do not retry the same code" in r3,
          "⭐ ③ 用户拒绝 → 明确挡住「原样重试」",
          " | ".join(r3.splitlines())[:120])

    # ⭐⭐ 而「用户插话」是**另一支**，两者必须分得开 —— 对模型的含义不同：
    #    点取消 = 不同意这件事；插话 = 改主意了，先看用户说了什么。
    # ⚠️ 真正的信号是 `_note_arrival()`（它动的是模块级 `_submit_seq` 并唤醒
    #    那个 Event），不是 `submit_seq()` 这个读取函数 ——
    #    📌 第一版打的是**读取口**，而判据读的是**变量**，于是补丁毫无作用。
    async def _speak_then_wait():
        t = asyncio.create_task(_call("open('o2.txt','w').write('x')", "写文件",
                                      approve=False, click=False))
        await asyncio.sleep(0.4)
        _ib._note_arrival()          # 用户开口了
        return await t
    r4, _ = _run(_speak_then_wait())
    check("NOT run" in r4 and "did not approve" not in r4,
          "⭐⭐ ④ 用户插话 → 走**另一条**文案（不是「不同意」）",
          " | ".join(r4.splitlines())[:100])


def t_empty_output_says_so() -> None:
    print("\n[6] ⭐ 忘了 print 要明说")
    r, _ = _run(_call("x = 1 + 1", "算一下"))
    # 📌 空串会被读成「结果就是空的」，而真相是它压根没打印。
    check("printed nothing" in r and "add a print()" in r,
          "⭐⭐ 明说「什么都没打印」+ 怎么办，而不是给一个空串",
          r.replace("\n", " | ")[:70])


def t_isolation_and_deps() -> None:
    print("\n[7] ⭐⭐⭐ 依赖可用 + 后门堵死（这两条必须同时成立）")
    from core import temp_exec as T
    # ① 同解释器 ⇒ 依赖白拿
    r, _ = _run(_call("import pandas as pd\nprint('pandas', pd.__version__)", "查版本"))
    check("pandas" in r and "successfully" in r,
          "⭐ pandas 在 —— 同一个 `sys.executable`，依赖是白拿的")
    # ② 而项目代码进不来
    r2, _ = _run(_call("import core.orchestrator\nprint('BREACH')", "试探"))
    check("ModuleNotFoundError" in r2 and "BREACH" not in r2,
          "⭐⭐⭐ `import core.*` **进不来** —— cwd + 干净 env 挡住")
    # 📌 早先的设计把「依赖可用」和「能 import core」绑成了一件事，**其实是两件**：
    #    前者靠同解释器拿到，后者靠 cwd/env 挡住。
    src = _code_only("core.temp_exec")
    check("PYTHONPATH" in src and "PYTHONHOME" in src and "PYTHONSTARTUP" in src,
          "⚠️ 三个变量都摘（只挡 PYTHONPATH 不够：HOME 换标准库、STARTUP 会执行文件）")
    check('"-I"' not in src,
          "⚠️ 没用 `-I`（isolated）—— 它会把 site-packages 一起挡掉，"
          "那正好废掉「依赖可用」")
    check("PYTHONUTF8" in src,
          "⭐ 强制 UTF-8 —— Windows 默认 GBK，模型代码里一个中文 print 就炸，"
          "而那个报错会把它引向一个不存在的问题")


def t_one_scanner_one_vocabulary() -> None:
    print("\n[8] 🔴 扫描器和词表**全项目各只有一份**")
    orch = module_text("core.orchestrator")
    cs = module_text("core.code_scan")
    # 🔴 两份扫描器分叉时不会报错，表现是「Skill 审计拦得住的，临时通道放过去了」
    check(orch.count("full_name == \"open\"") == 0,
          "⭐⭐ 扫描实现已从 orchestrator 移走（那里只剩一行转发）")
    check("from core.code_scan import detect_side_effects" in orch,
          "⭐ Skill 审计那条路也调**同一份**")
    check(cs.count("def detect_side_effects") == 1, "实现只有一份")
    # 词表同理
    check(orch.count('"file_write":    "写入文件到磁盘"') == 0,
          "⭐⭐ 中文词表也移走了（否则同一件事两种说法）")
    check("from core.code_scan import CONFIRM_LABELS" in orch,
          "⭐ `_check_skill_side_effects` 引用共用词表")
    from core.code_scan import labels_for
    check(labels_for(["未知类别"]) == ["未知类别"],
          "⚠️ 未知类别**原样保留不吞掉** —— 加了新类别忘了写文案时，"
          "不许静默少弹一条副作用")


def t_mechanical_guard_against_shadowing_skills() -> None:
    print("\n[9] ⭐⭐ 「别挤掉现成 Skill」靠机械保证，不靠提示词纪律")
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    from core.tools import Preload
    mans = {v["name"]: v for k, v in vars(O).items()
            if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    d = {x.name: x for x in build_builtin_definitions(mans)}["run_scratch_code"]
    # ⭐ DEFERRED ⇒ 模型必须先 load_tools，而 load_tools 的搜索**本来就会
    #    一起返回匹配的现成 Skill** ⇒ 两者同时摆在眼前让它选。
    # 📌 已定：不要用「给模型加一条纪律」当唯一答案。
    check(d.preload is Preload.DEFERRED,
          "⭐⭐⭐ 它是 DEFERRED —— 必须经过 load_tools，"
          "而那次搜索会把现成 Skill 一起摆出来")
    desc = mans["run_scratch_code"]["description"]
    check("Do NOT use it when a Skill already does this job" in desc,
          "⭐ 描述里也补了一句（**辅助**，不是主要防线）")
    check("create_new_skill answers" in desc and "run this once" in desc,
          "⭐ 与 create_new_skill 的边界讲清了：一个答「存不存」，一个答「跑一次」")
    check("by PATH, not by value" in desc,
          "⭐⭐ 明说数据靠**路径**传 —— 抄内容进代码会抄错，而且没人会发现")


def t_reuses_longcmd_not_a_second_pipeline() -> None:
    print("\n[10] ⭐ 输出走 longcmd，不是第二套")
    src = _code_only("core.temp_exec")
    check("longcmd" in src, "⭐⭐ 执行走 `longcmd` —— 长任务交还/落盘/回收全白拿")
    # ⚠️ 不能用 `"Popen" not in src` —— `_code_only` 走的是 `ast.unparse`，
    #    而它**保留 docstring**（那里正好写着「走 longcmd 而不是自己 Popen」）。
    #    📌 今天已经栽过一次（`search_markup_changed` 的计数）。
    # ⭐ 走 AST 数**真实调用**，跟注释和文档字符串都无关。
    _tree = ast.parse(module_text("core.temp_exec"))
    _popen_calls = [
        n for n in ast.walk(_tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == "Popen")
            or (isinstance(n.func, ast.Name) and n.func.id == "Popen"))
    ]
    check(not _popen_calls,
          "⭐⭐⭐ 自己**一个 Popen 都没调** —— 调了就是第二套输出处理",
          str(len(_popen_calls)))
    orch = _code_only("core.orchestrator")
    i = orch.index("_handle_run_scratch_code")
    seg = orch[i:i + 4000]
    check("_hand_back_long_task" in seg,
          "⭐ 超时走**同一条**长任务交还合同（与 MCP / run_command 同一条）")
    check("new_user_input_arrived" in seg,
          "⭐ 用户插话也能打断前台等待（判据与 run_command 共用一份）")


# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
def t_side_effect_aliases() -> None:
    """🔴 副作用扫描曾经**漏检 5/14**，而 一直拿它给用户看「这段代码干什么」。

    根因：匹配的是**完整点号链**（`p.write_text` → `"p.write_text"`），
    而规则表里写的是**裸名字**（`"write_text"`）⇒ 那条规则从来没生效过。
    ⚠️ 于是同一个操作，写法不同结果就不同：
        `pathlib.Path('a').write_bytes()`  检得出（链式，没落到变量上）
        `p = pathlib.Path('a'); p.write_text()`  **检不出** ← 而这是更常见的写法
    📌 同 `allow_dangerous` 那 6 个开关零读取点那次：
       **一个失效的检测比没有检测更危险** —— 弹窗上写着「无副作用」，
       用户据此点了同意，而那段代码正在改用户的 .env。
    """
    print("\n▶ 副作用扫描：别名 / 变量方法 / 误报边界")
    import ast as _ast
    from core.code_scan import detect_side_effects as D

    def scan(code):
        return D(code, _ast.parse(code))

    # ── 必须检出 ────────────────────────────────────────────────────────
    MUST = [
        ("open(f,'w')", "open('a.txt','w').write('x')"),
        ("🔴 p.write_text（曾漏）", "import pathlib\np=pathlib.Path('a')\np.write_text('x')"),
        ("pathlib 链式 write_bytes", "import pathlib\npathlib.Path('a').write_bytes(b'x')"),
        ("🔴 p.unlink（曾漏）", "import pathlib\np=pathlib.Path('a')\np.unlink()"),
        ("os.remove", "import os\nos.remove('a')"),
        ("shutil.rmtree", "import shutil\nshutil.rmtree('a')"),
        ("from shutil import rmtree", "from shutil import rmtree\nrmtree('a')"),
        ("subprocess.run", "import subprocess\nsubprocess.run(['x'])"),
        ("🔴 import as 别名（曾漏）", "import subprocess as sp\nsp.run(['x'])"),
        ("requests.get", "import requests\nrequests.get('http://x')"),
        ("🔴 requests.Session（曾漏）", "import requests\ns=requests.Session()\ns.get('http://x')"),
        ("🔴 os.rename（表里没有）", "import os\nos.rename('a','b')"),
        ("🔴 os.replace（表里没有）", "import os\nos.replace('a','b')"),
        ("from os import replace", "from os import replace\nreplace('a','b')"),
        ("shutil.move", "import shutil\nshutil.move('a','b')"),
        ("eval", "eval('1')"),
    ]
    for name, code in MUST:
        check(bool(scan(code)), f"检出 · {name}", str(scan(code))[:60])

    # ── 必须【不】误报 ──────────────────────────────────────────────────
    #    📌 判据：这些方法名在别的语义下**满地都是**。收了它们，
    #       检测会淹在误报里 —— 而误报多到一定程度，等于没有检测。
    MUST_NOT = [
        ("dict.get", "d={'a':1}\nprint(d.get('a'))"),
        ("list.remove", "L=[1,2]\nL.remove(1)"),
        ("str.replace", "print('abc'.replace('a','b'))"),
        ("自定义 .rename()", "class A:\n    def rename(self): pass\nA().rename()"),
        ("纯读纯打印", "import json,pathlib\nprint(json.loads(pathlib.Path('x').read_text()))"),
    ]
    for name, code in MUST_NOT:
        check(not scan(code), f"不误报 · {name}", str(scan(code))[:60])

    # ── 同一件事不许列两遍 ──────────────────────────────────────────────
    for name, code in (("链式 write_bytes", "import pathlib\npathlib.Path('a').write_bytes(b'x')"),
                       ("shutil.rmtree", "import shutil\nshutil.rmtree('a')"),
                       ("os.unlink", "import os\nos.unlink('a')")):
        f = scan(code)
        check(len(f) == len(set(f)),
              f"⚠️ 不重复 · {name} —— 📌 一条被更通用规则完全覆盖的规则，"
              f"留着不是「双保险」，是重复", str(f))

    src = module_text("core.code_scan")
    check("_alias" in src and "def _canon" in src,
          "⭐ 别名映射先摊平再匹配 —— 规则表一个字没改，却对所有 import 别名生效")
    check('"rename", "replace"' not in src,
          "🔴 裸 `rename`/`replace` **不许收** —— 第一版收了它，"
          "当场把 `'abc'.replace(...)` 报成「文件改名」")


if __name__ == "__main__":
    print("=" * 74)
    print("[C2] 临时执行通道")
    print("=" * 74)
    t_name_has_no_ecosystem_owner()
    t_no_side_effect_runs_straight()
    t_side_effect_uses_the_same_dialog()
    t_preview_is_readonly()
    t_tool_card_always_shows_the_code()
    t_failures_are_correctly_typed()
    t_empty_output_says_so()
    t_isolation_and_deps()
    t_one_scanner_one_vocabulary()
    t_mechanical_guard_against_shadowing_skills()
    t_reuses_longcmd_not_a_second_pipeline()
    t_side_effect_aliases()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
