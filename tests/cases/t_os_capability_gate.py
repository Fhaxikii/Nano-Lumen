# -*- coding: utf-8 -*-
"""OS 能力开关 —— 从「6 个里 5 个是死的」到真的成为闸（2026-08-20）。

═══ 这一套守的东西 ═══

  ① 🔴🔴 **每一个开关都要有真执行点**
     发现的时候：设置 → OS 权限那 6 个开关里，**只有 `allow_mouse_keyboard`
     有读取点**。AST 层面数下来，另外 5 个（工作区写入 / 窗口控制 / 系统设置 /
     注册表写入 / **高危操作总闸**）在全项目里各出现 **1 次**，
     而那一次就是 `_DEFAULT_PERMISSIONS` 这个字典本身 —— 是**定义**，不是读取。
     ⇒ 用户把「高危操作总闸」关掉，Nano 照样能 `run_command`、`file_delete`；
       开关照样存进 os_config.json、UI 上照样变灰，**看起来完全生效了**。
     📌 **一个失效的开关比没有这个开关更危险** —— 用户会据此放松警惕。
        而它坏的方向是**放行**。

  ② ⭐⭐ **能力闸在授权闸的【上游】**（2026-08-20 定的语义）
         能力开关答「这项能力**开不开放**」
         Auto     答「开放了的，**要不要逐个授权**」
     ⇒ 能力没开放时，连"要不要授权"这个问题都不该被问到 —— **auto 救不了**。
     📌 一个上游的闸如果被放到下游，下游的豁免就会顺手把它一起豁免掉。
     ⚠️ 本套件**真的跑一遍 dispatcher**，看它到底 yield 了什么 ——
        只查"代码里有那个 if"是查形状，而形状对了位置错照样不起作用。

  ③ ⭐ **总闸跟着【有效风险】走，不跟着静态表走**
     `dynamic_upgrade_rules` 会把静态 floor=2 的动作（`launch_app powershell`）
     升到 3。总闸若只看静态表，升级上来的那些正好全部绕过它 ——
     📌 而那批恰恰是最该被总闸挡住的。

  ④ ⭐ **拒绝的话要说清是哪一道、谁能打开**（：正确 **且** 充分）

  ⑤ ⭐ **`authorized_by`：同意与拒绝都要能被感知**，且「用户点的」与
     「auto 替用户点的」分得开。

用法：
  py -3.10 tests\\cases\\t_os_capability_gate.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.os_layer import dsl

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


ALL_ON = {k: True for k in dsl.PERMISSION_LABELS}
ALL_OFF = {k: False for k in dsl.PERMISSION_LABELS}


# ══════════════════════════════════════════════════════════════════════════
def t_every_switch_gates_something() -> None:
    print("")
    print("[1] 🔴🔴 六个开关**每一个**都真的挡得住东西")
    # ⚠️ 按**行为**验，不按「代码里有没有出现这个字符串」——
    #    📌 后者正是这个洞能活这么久的原因：5 个键都"出现过"（在默认值字典里）。
    for _k in dsl.PERMISSION_LABELS:
        _perms = dict(ALL_ON)
        _perms[_k] = False
        _blocked = [a for a in dsl.ALL_ACTION_NAMES
                    if dsl.missing_permissions(a, _perms, dsl.action_floor(a))]
        check(bool(_blocked),
              f"⭐⭐⭐ 关掉「{dsl.PERMISSION_LABELS[_k]}」→ 真的有 action 被挡住",
              f"{len(_blocked)} 个，例：{_blocked[:3]}")

    # ⭐ 反向：全开时**一个都不该挡**（否则等于把开关默认拧死）
    _still = [a for a in dsl.ALL_ACTION_NAMES
              if dsl.missing_permissions(a, ALL_ON, dsl.action_floor(a))]
    check(not _still, "⭐ 全开时一个都不挡（这一轮只收紧不误伤）", str(_still))


def t_danger_follows_effective_risk() -> None:
    print("")
    print("[2] ⭐⭐ 总闸跟着**有效风险**走，不跟着静态表走")
    _no_danger = dict(ALL_ON)
    _no_danger[dsl.PERM_DANGEROUS] = False

    # `launch_app` 静态 floor=2；`dynamic_upgrade_rules` 会把 launch_app powershell 升到 3
    check(not dsl.missing_permissions("launch_app", _no_danger, 2),
          "⭐ 普通 launch_app（有效风险 2）不需要总闸")
    check(dsl.missing_permissions("launch_app", _no_danger, 3) == [dsl.PERM_DANGEROUS],
          "⭐⭐⭐ **同一个 action 被升到风险 3 → 立刻需要总闸** —— "
          "📌 一个「总闸」如果只对静态标了高危的那些生效，它就不是总闸；"
          "而升级上来的那批恰恰是最该被它挡住的",
          str(dsl.missing_permissions("launch_app", _no_danger, 3)))
    check(dsl.missing_permissions("run_command", _no_danger, 3) == [dsl.PERM_DANGEROUS],
          "⭐ run_command 被总闸挡住")
    # ⚠️ 未知 action → fail-safe 朝「多要一道」错
    check(dsl.required_permissions("这个动作不存在", 1) == frozenset({dsl.PERM_DANGEROUS}),
          "⚠️ 未知 action 要总闸 —— 📌 fail-safe 不朝「谁都不管」错")
    # 🔴 静态表里不许出现总闸（它不是能力类别）
    check(all(dsl.PERM_DANGEROUS not in s.perms for s in dsl._ACTIONS.values()),
          "⭐⭐ 静态 `perms` 里一个总闸都没有 —— 它是风险档，不是能力类别")


def t_table_cannot_drift() -> None:
    print("")
    print("[3] ⭐⭐ 表本身不许漂")
    # ① `perms` 无默认值 —— 加新 action 时**必须**回答"它属于哪一类能力"
    try:
        dsl.ActionDef("新动作", 2, False, 2, "测试用")
        _ok = False
    except TypeError:
        _ok = True
    check(_ok,
          "⭐⭐⭐ **`perms` 没有默认值** —— 少了它连构造都通不过。"
          "📌 这是这一层唯一真正的防漂措施：有默认值的话，"
          "下一个 action 会静默落进「谁都不管」")

    # ② 启动期不变量真的会抓（不是摆着好看）
    check(hasattr(dsl, "_assert_perm_table_sane"), "⚠️ 前置：不变量函数在")
    _spec = dsl._ACTIONS["file_write"]
    _saved = _spec.perms
    try:
        object.__setattr__(_spec, "perms", frozenset())   # floor=2 且无归属
        try:
            dsl._assert_perm_table_sane()
            _caught = False
        except RuntimeError as e:
            _caught = "file_write" in str(e)
    finally:
        object.__setattr__(_spec, "perms", _saved)
    check(_caught,
          "⭐⭐⭐ **floor=2 且没有能力归属 → 启动期就炸** —— "
          "🔴 那正是「auto 一开就是零闸」的形状")

    # ③ 只读的那些不该被任何开关挡（否则关一个开关会顺手把观察能力也关掉）
    _ro = [n for n, s in dsl._ACTIONS.items() if s.readonly and s.perms
           and n != "read_screen_region"]
    check(not _ro,
          "⭐ 只读 action 不受能力开关管（`read_screen_region` 是唯一例外，"
          "⏸ 保守留在鼠标键盘档：它今天就被那个开关挡着，改成不挡是**放宽**）",
          str(_ro))


def t_capability_gate_is_upstream_of_confirm() -> None:
    print("")
    print("[4] ⭐⭐⭐ 能力闸在授权闸**上游** —— 真的跑一遍 dispatcher")
    from core.os_layer.dispatch import OSDispatcher

    def _run(perms_off: str, action: str, params: dict):
        d = OSDispatcher(session_id="t", m1_mode=False, m2_mode=True, m3_mode=True)
        d._permissions = dict(ALL_ON)
        d._permissions[perms_off] = False

        async def _go():
            out = []
            async for ev in d.execute({"action": action, "params": params,
                                       "reason": "测试"}):
                out.append(ev)
            return out
        return asyncio.get_event_loop().run_until_complete(_go())

    _evs = _run(dsl.PERM_WORKSPACE_WRITE, "file_write",
                # ⚠️ 路径只需要「落在工作区里」；能力闸在碰文件系统之前就拒了，
                #    这个串从头到尾不会被解析。
                {"path": str(ROOT / "data" / "gate_probe.md"),
                 "content": "x"})
    _kinds = [e.get("type") for e in _evs]
    check("confirm_request" not in _kinds,
          "⭐⭐⭐ **能力关着时压根不 yield `confirm_request`** —— "
          "🔴 这一条就是「auto 救不了」的实现：auto 豁免的是确认弹窗，"
          "而这里根本走不到那一步。"
          "📌 一个上游的闸如果被放到下游，下游的豁免会顺手把它一起豁免掉",
          str(_kinds))
    _res = [e for e in _evs if e.get("type") == "result"]
    check(_res and not _res[0].get("ok"),
          "⭐ 直接给一个失败结果", str(_kinds))
    check(_res and _res[0].get("missing_permissions") == [dsl.PERM_WORKSPACE_WRITE],
          "⭐⭐ 结构化那一份也给出去了（消费方不该去解析中文句子）",
          str(_res[0].get("missing_permissions") if _res else None))
    check(_res and _res[0].get("authorized_by") == "capability_disabled",
          "⭐ `authorized_by` 说清这是**能力没开放**，不是授权被拒",
          str(_res[0].get("authorized_by") if _res else None))

    _err = (_res[0].get("error") or "") if _res else ""
    # 正确 **且** 充分
    check("不是**授权被拒**" in _err or "不是" in _err and "授权被拒" in _err,
          "⭐⭐ 明说**这不是授权被拒** —— 📌 两者对模型的下一步完全不同")
    check("工作区写入" in _err,
          "⭐⭐⭐ **点名是哪一个开关**（用用户在 UI 上看到的那个名字）", _err[:60])
    check("设置" in _err and "OS 权限" in _err,
          "⭐⭐ 并说清**去哪里打开** —— 🔴 改造前这里是 "
          "`permission_denied: allow_mouse_keyboard` 一个机器串，模型只能猜")
    check("用户自己" in _err and "不要重试" in _err,
          "⭐⭐ 且说清**你打不开它、也别重试** —— "
          "📌 给模型的失败信息必须同时【正确】且【充分】")


def t_labels_do_not_drift() -> None:
    print("")
    print("[5] ⚠️ UI 上那 6 个名字与模型看到的**必须是同一份**")
    import app as _app
    _ui = {k: label for k, _icon, label, _desc, _color in _app.WebUI._OS_PERMISSION_META}
    check(_ui == dsl.PERMISSION_LABELS,
          "⭐⭐ `app._OS_PERMISSION_META` 的标签与 `dsl.PERMISSION_LABELS` 逐字一致 —— "
          "🔴 拒绝话术里那句「去打开【X】」用的是后者，用户在界面上看到的是前者；"
          "📌 同一个开关有两份名字时，改了一处另一处就开始说谎",
          f"ui={_ui}")


def t_authorized_by_is_visible() -> None:
    print("")
    print("[6] ⭐⭐ 「谁批的」对模型可见，且**用户点的**与**auto 替用户点的**分得开")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    fn = next((f for f in ast.walk(tree)
               if isinstance(f, ast.AsyncFunctionDef) and f.name == "_execute_dsl_step"), None)
    check(fn is not None, "⚠️ 前置：找得到 `_execute_dsl_step`")
    body = ast.unparse(fn) if fn else ""

    check("'auto': _on_auto" in body,
          "⭐⭐⭐ **auto 走一个单独的回调** —— 🔴 复用 `on_confirm` 的话，"
          "「用户亲自点了同意」和「auto 替用户点了」在这一层就完全无法区分，"
          "而它们对模型是两件事")
    for _v in ("user_once", "user_always", "auto", "user_denied"):
        check(f"'{_v}'" in body, f"⭐ `authorized_by` 有 `{_v}` 这一档")
    check("confirm_timeout" in body and "cancelled_by_user_message" in body,
          "⭐ 三种「没执行」分得开（拒绝 / 改口 / 超时）—— "
          "📌 一个字段不许表达两个现实")

    # 🔴 auto 不许落成永久预授权
    _i_auto = body.find("'auto'")
    _i_always = body.find("choice == 'always'")
    check(_i_always > 0 and "choice == 'always'" in body,
          "⭐⭐⭐ 预授权只由 **`always`** 触发 —— "
          "🔴 若 auto 也写预授权，那么关掉 auto 之后它还在，"
          "而用户以为自己关掉了。📌 一个临时的豁免不许顺手落成永久的")

    # 拒绝那句话必须说清"这是决定不是故障"
    check("This is a decision, not a failure" in src,
          "⭐⭐⭐ 用户点拒绝 → **明说这是一次判断、不是一次故障，不要重试** —— "
          "🔴 改造前这里只有「用户已取消」五个字，而旁边两条都是完整句："
          "📌 三条出口里，真正的「用户拒绝」反而说得最不清楚，"
          "而它是唯一一条模型必须停手的")

    # 预授权/无需授权那两档在 dispatch 侧
    _dsp = module_text("core.os_layer.dispatch")
    # ⚠️ 按 AST 数字符串常量，不按引号形状 —— 源码里可能是单引号也可能是双引号，
    #    📌 一条断言不该在「我换了个引号」时变红。
    _consts = {n.value for n in ast.walk(ast.parse(_dsp))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check({"not_required", "pre_authorized"} <= _consts,
          "⭐ 「本来就不需要授权」与「以前批过、这次沿用」分得开")


def t_state_is_injected_as_fact() -> None:
    print("")
    print("[7] ⭐⭐ 现状投影给模型 —— 给事实，不给剧本")
    import core.orchestrator as O

    # ① 出厂态（全开 + 非 auto）→ **一个字都不说**
    from core.runtime import oslease as _ol
    _saved_lp, _saved_auto = dsl.load_permissions, dsl.user_auto_mode_on
    _saved_temp = _ol.temp_auto_authorized
    try:
        dsl.load_permissions = lambda *a, **k: dict(ALL_ON)
        dsl.user_auto_mode_on = lambda *a, **k: False
        _ol.temp_auto_authorized = lambda *a, **k: False
        check(O._rt_authorization_state(None) == "",
              "⭐⭐ 出厂态**返回空串** —— 📌 一段每轮都在的注入会被模型学会忽略，"
              "恰好毁掉「响亮」这件事本身（同 [F5] 压力段那条纪律）")

        # ② 关了开关 → 说清哪一个 + auto 救不了
        _p = dict(ALL_ON); _p[dsl.PERM_DANGEROUS] = False
        dsl.load_permissions = lambda *a, **k: dict(_p)
        _t = O._rt_authorization_state(None)
        check("高危操作总闸" in _t and "Auto does not" in _t,
              "⭐⭐⭐ 点名那个开关，并明说 **Auto 覆盖不了它** —— "
              "📌 不写这句，模型会用「反正开了 auto」去解释一次能力被关的失败",
              _t[:80])
        check("设置" in _t and "OS 权限" in _t, "⭐ 并说清用户去哪里开")

        # ③ auto 开着 → 说清"没人会被问"
        dsl.load_permissions = lambda *a, **k: dict(ALL_ON)
        dsl.user_auto_mode_on = lambda *a, **k: True
        _t2 = O._rt_authorization_state(None)
        check("Auto is ON" in _t2 and "waiting for their approval" in _t2,
              "⭐⭐ auto 开着时告诉它**不会被逐个询问**，且别说「我在等你批准」 —— "
              "🔴 改造前模型对 auto **完全无感**：开着和不开看到的一模一样",
              _t2[:80])
        check("turned OFF" not in _t2,
              "⭐ 而此时**不提能力**（全开）—— 📌 两件事分开说，"
              "混成一句模型就会互相解释")
        check("Temp Auto" not in _t2, "用户自己选的 Auto 不说成 Temp Auto")

        # ④ GUI 任务的临时授权 → 单独一行 Temp Auto，不冒充用户的 Auto
        dsl.user_auto_mode_on = lambda *a, **k: False
        _ol.temp_auto_authorized = lambda *a, **k: True
        _t3 = O._rt_authorization_state(None)
        check("Temp Auto is ON" in _t3 and "Auto is ON: for" not in _t3,
              "临时授权单独说成 Temp Auto，与用户选的 Ask / Auto 分开", _t3[:80])
        check("end_screen_task" in _t3 and "ends with the task" in _t3,
              "说清它随任务结束、怎么结束")
    finally:
        dsl.load_permissions = _saved_lp
        dsl.user_auto_mode_on = _saved_auto
        _ol.temp_auto_authorized = _saved_temp


def t_injection_names_the_affected_tools() -> None:
    """🔴 实测 2026-08-20：「工作区写入」关着时**main agent 不信 Subagent 的报告**。

    Subagent 如实回报「该能力已被禁用」，main agent 的原话：
      「Subagent 的报告有问题——它说「工作区写入」禁用了，但我用 `edit_file`
        可以直接编辑文件」
    然后自己又读了一遍文件、又要了一次授权。

    📌 **注入告诉了它「哪个开关关了」，没告诉它「这会让你的哪些工具用不了」** ——
       它手里那个工具叫 `edit_file`，关掉的东西叫「工作区写入」，
       两个名字之间没有任何东西把它们连起来。
    ⚠️ 代价不只是多花一轮：它**推翻了一份正确的报告**，
       而那会教它以后也别信Subagent。
    """
    print("")
    print("[8] 🔴 注入必须点名**受影响的工具**，不能只报开关名")
    import core.orchestrator as O

    _p = dict(ALL_ON); _p[dsl.PERM_WORKSPACE_WRITE] = False
    _lp = dsl.load_permissions
    dsl.load_permissions = lambda *a, **k: dict(_p)
    try:
        _t = O._rt_authorization_state(None)
    finally:
        dsl.load_permissions = _lp

    check("edit_file" in _t,
          "⭐⭐⭐ **点名 `edit_file`** —— 它自己不写盘，但穿过 `file_write` 那条路；"
          "🔴 不点名的话模型连不上「关掉的是工作区写入」和「我这个工具叫 edit_file」",
          _t[-160:])
    check("os_execute" in _t and "file_write" in _t,
          "⭐⭐ 并点名 `os_execute` 与具体被挡的 action")
    check("telling you the truth" in _t and "do NOT retry it yourself" in _t,
          "⭐⭐⭐ 明说**Subagent这时说的是真话、别自己换个工具再试一遍** —— "
          "🔴 实测那次它正是这么干的")

    # ⭐ 那张表必须与「真的会走 OS 层的 handler」一一对应 —— **数出来，不靠记性**
    _src = module_text("core.orchestrator")
    _tree = ast.parse(_src)
    _hosts = set()
    for n in ast.walk(_tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_execute_dsl_step"):
            for f in ast.walk(_tree):
                if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and f.lineno <= n.lineno <= (f.end_lineno or 0)):
                    _hosts.add(f.name)
    check(len(_hosts) == len(O._OS_BACKED_TOOLS),
          "⭐⭐⭐ **会走 OS 层的 handler 有几个，表里就有几条** —— "
          "📌 一张手写的表，只有在有东西替你数它的时候才不会过期",
          f"handlers={sorted(_hosts)} 表={sorted(O._OS_BACKED_TOOLS)}")


def main() -> int:
    t_every_switch_gates_something()
    t_danger_follows_effective_risk()
    t_table_cannot_drift()
    t_capability_gate_is_upstream_of_confirm()
    t_labels_do_not_drift()
    t_authorized_by_is_visible()
    t_state_is_injected_as_fact()
    t_injection_names_the_affected_tools()

    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
