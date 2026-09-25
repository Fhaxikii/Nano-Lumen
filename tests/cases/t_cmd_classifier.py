# -*- coding: utf-8 -*-
"""auto 模式下的命令危险判定（2026-08-27）。

═══ 它解决什么 ═══

`run_command` 的 floor=3，而 auto 模式**跳过所有确认弹窗** ——
于是 `sc query Audiosrv` 和 `rmdir /s /q ...` 待遇完全相同：都直接执行。
⇒ 这道判定把后者捞出来。**不是让 auto 变严，是让 auto 敢用。**

═══ ⭐⭐ 四态═══

    A  不是 run_command      auto 不弹   零 token   ~85%
    B  判定：安全            auto 不弹   有 token   ~12%
    C  判定：危险            🔴 弹窗     有 token   ~2%
    D  判定：判不了          🔴 弹窗     看来源     ~0.3%

**C 必须同时满足三条**（「这条是绝对的设计铁律」）：
    ① 是 run_command  ∧  ② 属于高危指令类  ∧  ③ **跟用户要做的事对不上**

🔴 没有 ③ 的话它退化成「删除 = 危险」—— 那是一张正则表就能做的事，
   而且会拦掉用户**明确要求**的删除。> 「如果按照你刚才说的 删除=危险，那我们绕这么一大圈意义在哪？
   >   直接改成 auto 模式管不着某些命令、一律弹窗不得了吗？」

用法：
  py -3.10 tests\\cases\\t_cmd_classifier.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
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


# ══════════════════════════════════════════════════════════════════════════
def t_vendor_table() -> None:
    """厂商表：三个角色各有一张**有序**白名单（价格升序），默认取第一个。

    ⚠️ 2026-08-30 之前 classifier 的 fallback 与 distiller **相反**；统一之后
       差别只剩「未适配的厂商 → 空串 → 不启用」，那条仍然成立。
    """
    print("\n▶ 厂商适配表")
    from core.models import classifier_for, distiller_for

    check(classifier_for("anthropic/claude-opus-5")
          == "anthropic/claude-haiku-4.5",
          "⭐ 主模型 → **同厂**角色池的第一个（价格升序，用户只有那一家的 key）")
    # ⚠️ 例子从 deepseek/… 换成了一个占位厂商 —— 深度求索 2026-08-31 已适配，
    #    再拿它当"未适配"的反例会变成假失败。
    #    📌 用具体厂商名当反例，会在那家被支持的那天失效。
    check(classifier_for("someco/some-model") == "",
          "⭐⭐ 未适配的厂商 → 空串。📌 这保住了框架中立性："
          "用户接别家模型时**不是让 Sonnet 去判**，而是判定不启用、回到现状")
    check(classifier_for("") == "", "⚠️ 空 model id 不炸")
    check(distiller_for("anthropic/claude-opus-5") != "",
          "⚠️ distiller 同样走池子")

    src = module_text("core.models")
    # ⚠️ 2026-08-30：三个角色统一成「取池子第一个」，原来那条"两者相反"的纪律
    #    已移除（禁令针对的是退回**主模型**=自己判自己；退回池子里的另一个
    #    模型仍然是第三方）。这里改钉**新的**不变量：三个角色同一个解析器。
    check("model_for_role" in src and "不回退主模型" in src,
          "🔴🔴 钉住三角色同源：都走 `model_for_role`，**不回退主模型**（旧文案：distiller 空串=退回主模型"
          "（宁可贵，不许失能）；classifier 空串=**不启用**（宁可不省，也不冒险）。"
          "📌 两个函数长得一模一样而语义相反，不写清楚下一个人必然照着改错")


def t_script_prescan() -> None:
    """跑脚本那一类：先静态看内容，**这一层不花 token**。"""
    print("\n▶ 脚本静态预扫（免费）")
    from core.os_layer import cmd_classifier as C

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "safe.py").write_text(
        "import json,pathlib\nprint(json.loads(pathlib.Path('x').read_text()))\n",
        encoding="utf-8")
    (d / "danger.py").write_text("import shutil\nshutil.rmtree('C:/x')\n",
                                 encoding="utf-8")
    (d / "local.py").write_text("import my_helper\nprint(1)\n", encoding="utf-8")
    (d / "bad.py").write_text("def (\n", encoding="utf-8")

    v, _ = C.prescan_script(f'python "{d / "safe.py"}"')
    check(v == C.ALLOW,
          "⭐⭐ 纯查询脚本 → 放行，**连模型都不用调** —— "
          "📌 当初的问法：「能不能让分类器像判断别的命令一样，判断这段脚本危不危险」")
    v, why = C.prescan_script(f'python "{d / "danger.py"}"')
    check(v == "" and "rmtree" in why,
          "🔴🔴 **有副作用时【不下判决】，只交证据**（verdict 是空串）—— "
          "第一版我这里直接 BLOCK，那是在最靠近铁律的地方把铁律绕过去了："
          "用户说「跑一下你刚写的那个补丁脚本」+ 脚本会改文件 → **本该 ALLOW**。"
          "📌 **有没有副作用是【证据】，该不该拦是【判决】** —— 取证的不能兼任审判", why)

    # 证据必须真的送到模型面前 —— 它自己看不到脚本内容
    class _Spy:
        seen = ""

        async def chat_without_tools(self, ctx, sys_, **k):
            _Spy.seen = ctx[0]["content"]
            return ("ALLOW", "m")
    asyncio.run(C.classify(_Spy(), command=f'python "{d / "danger.py"}"',
                           user_messages=["跑一下你刚写的那个脚本"],
                           main_model="anthropic/claude-opus-4.8"))
    check("STATIC ANALYSIS" in _Spy.seen and "rmtree" in _Spy.seen,
          "⭐⭐ 静态挖到的副作用**进了给模型的 payload** —— "
          "📌 这是取证与判决的交接点：证据由代码给，判决由「对不对得上意图」下")
    v, why = C.prescan_script(f'python "{d / "local.py"}"')
    check(v == C.UNKNOWN and "本地模块" in why,
          "⚠️ import 了本地模块 → **D 态**。静态扫只看这一个文件，"
          "📌 **看不全就不放行** —— 但说的是「我看不全」，不是「它危险」")
    v, why = C.prescan_script(f'python "{d / "bad.py"}"')
    check(v == C.UNKNOWN, "⚠️ 语法错 → D（不是 BLOCK：它可能完全无害）")
    v, why = C.prescan_script(r'powershell -File "C:\x\a.ps1"')
    check(v == C.UNKNOWN and "无法静态分析" in why,
          "⭐ .ps1 → D，理由是「无法静态分析」。"
          "🔴 **刻意不做文本关键词匹配** —— 编码/拼接/混淆全躲得过，"
          "📌 一个「看起来分析过了」的结果，比明说「没分析」更坏")
    v, _ = C.prescan_script('python patch.py')
    check(v == C.UNKNOWN, "⚠️ 相对路径 → D（cwd 不可信，实测过：explorer 起的是 system32）")
    v, _ = C.prescan_script(r'python "C:\nope\gone.py"')
    check(v == C.UNKNOWN, "⚠️ 文件读不到 → D")
    check(C.prescan_script("sc query Audiosrv") is None,
          "⭐ 不是跑脚本 → None，交给模型判（这一层只管脚本）")


def t_lookaway_safety() -> None:
    """两条「闸自己失效时朝哪边倒」。"""
    print("\n▶ 闸失效时的方向")
    import ast as _ast
    import sys as _sys
    from core.os_layer import cmd_classifier as C

    _bak = getattr(_sys, "stdlib_module_names", None)
    try:
        if hasattr(_sys, "stdlib_module_names"):
            del _sys.stdlib_module_names
        check(C._stdlib_ok(_ast.parse("import json")) is False,
              "🔴 拿不到标准库名单 → **判不了**，不是「没人说不行就放行」。"
              "📌 这条闸的意义就是「看不全就不放行」——"
              "它自己失效时若朝放行倒，等于闸门坏在开着的位置")
    finally:
        if _bak is not None:
            _sys.stdlib_module_names = _bak
    check(C._stdlib_ok(_ast.parse("import json")) is True, "⚠️ 恢复后照常")
    check(C._stdlib_ok(_ast.parse("import my_helper")) is False, "⚠️ 本地模块 → 看不全")
    check(C._stdlib_ok(_ast.parse("from . import x")) is False, "⚠️ 相对导入 → 看不全")

    check("VERDICT: UNCLEAR" in C._STAGE2_SYS and C._STAGE1_TAIL in C._STAGE1_SYS,
          "🔴 Stage2 提示词由 Stage1 **派生**，模块级 assert 守着 —— "
          "`str.replace` 找不到目标时**不报错不替换**，"
          "那样 Stage2 会静默变成 Stage1 的复制品，**UNCLEAR 从模型侧消失**。"
          "📌 同 code_scan 那条 write_text 规则从来没生效过一个形状")


def t_fail_safe() -> None:
    """失败方向：**一律朝 D（弹窗）倒**。"""
    print("\n▶ 失败方向")
    from core.os_layer import cmd_classifier as C

    async def go():
        v, why = await C.classify(None, command="del x",
                                  user_messages=["删掉它"],
                                  main_model="someco/some-model")
        check(v == C.UNKNOWN and "没有配置" in why,
              "🔴 没配判定模型 → D。**绝不退回主模型** —— "
              "那既贵，又变成「自己判自己」")

        v, why = await C.classify(object(), command="del x", user_messages=[],
                                  main_model="anthropic/claude-opus-4.8")
        check(v == C.UNKNOWN and "用户消息" in why,
              "🔴🔴 **没有用户消息 → D，而不是按 ①② 硬判** —— "
              "📌 条件 ③ 无从判起时若还出结论，就退化成「删除=危险」那张正则表")

        class Boom:
            async def chat_without_tools(self, *a, **k):
                raise RuntimeError("network down")
        v, why = await C.classify(Boom(), command="del x",
                                  user_messages=["看看这个文件"],
                                  main_model="anthropic/claude-opus-4.8")
        check(v == C.UNKNOWN and "调用失败" in why, "⚠️ API 炸了 → D")

        v, _ = await C.classify(object(), command="   ",
                                user_messages=["x"],
                                main_model="anthropic/claude-opus-4.8")
        check(v == C.UNKNOWN, "⚠️ 空命令 → D")
    asyncio.run(go())

    for t, want in (("ALLOW", C.ALLOW), ("block", C.BLOCK),
                    ("VERDICT: UNCLEAR", C.UNKNOWN),
                    ("I think it's fine", C.UNKNOWN), ("", C.UNKNOWN)):
        check(C._verdict_of(t) == want,
              f"⚠️ 判定解析 {t!r:20} → {want} —— **认不出就 UNKNOWN，绝不猜**")


def t_cache_key_carries_intent() -> None:
    """缓存键必须带意图指纹 —— 这是条件 ③ 的直接推论。"""
    print("\n▶ 缓存")
    from core.os_layer import cmd_classifier as C
    k1 = C._cache_key("del /q /f %temp%\\*", "- 帮我清理临时文件")
    k2 = C._cache_key("del /q /f %temp%\\*", "- 看看这个 txt 写了什么")
    check(k1 != k2,
          "🔴🔴 **同一条命令 + 不同意图 = 不同的键**。"
          "📌 因为判据是「动作跟意图对不对得上」——"
          "同一条 del 在「帮我清理」下该放行，在「看看文件」下必须拦。"
          "键里不带意图的话，第一次的判定会被错误地复用到第二次")
    check(C._cache_key("a", "x") == C._cache_key("a", "x"), "⚠️ 同输入同键")
    C._cache["zzz"] = C.ALLOW
    C.reset_cache()
    check(not C._cache, "⚠️ 换会话清空 —— 意图变了，旧判定不再适用")


def t_intent_source() -> None:
    """意图只来自**用户自己说的话**。"""
    print("\n▶ reasoning-blind")
    from core.os_layer import cmd_classifier as C
    s = C.build_intent(["帮我清理临时文件", "  ", "顺便看看日志"])
    check("清理临时文件" in s and "看看日志" in s, "⭐ 用户消息进得来")
    check(C.build_intent([]) == "", "⚠️ 空 → 空（上游据此落 D）")
    check(len(C.build_intent(["x" * 5000])) <= C._MAX_USER_CHARS,
          "⚠️ 长会话有上限，不会越判越贵")

    src = module_text("core.os_layer.cmd_classifier")
    check("reasoning-blind" in src and "注入的载体" in src,
          "⭐⭐ 钉住输入边界：看【用户消息+这一条工具调用】，"
          "剥掉【模型自己的话】和【工具输出】。"
          "📌 前者是被核的对象不能自辩，后者是注入的载体 —— "
          "让它进判定器等于把闸交给攻击者")
    check("危险不是命令的属性" in src,
          "🔴 钉住那条铁律：危险是「动作偏离了意图」这个**关系**的属性，"
          "不是命令本身的属性")


def t_wiring() -> None:
    """接线：判据、fail-safe、以及「始终允许」为什么不用特意去掉。"""
    print("\n▶ 接线")
    _app = module_text("app")
    _orc = module_text("core.orchestrator")

    check('if self._auto_on() and step.get("auto_ok") is True:' in _app,
          "🔴🔴 **判据是「明确说可以」而不是「没人说不行」** —— "
          "📌 上游漏传这个键时：前者退化成照常弹窗（安全），"
          "后者退化成静默执行（危险）。**默认值那一侧永远是危险的那侧**")
    check('"auto_ok": _auto_ok' in _orc and '"gate_by": _gate_by' in _orc,
          "⭐ 事件里带出判定结果")
    check("if _dsl_g.auto_authorization_on():" in _orc,
          "⭐⭐ **只在 auto 开着时才调判定** —— "
          "ask permission 模式本来就每条都问，判定一分钱不值")
    check("_auto_ok, _gate_by, _gate_why = False" in _orc,
          "🔴 连「要不要判」都判不了时 → 需要确认（fail-safe）")
    check('if action != "run_command":' in _orc,
          "⭐ A 态：不是 run_command 就压根不算，零 token")
    check("[自动放行被拦下]" in _orc,
          "⭐ 理由复用弹窗现成的「风险原因」栏 —— "
          "📌 一个只多一行文字的需求，不该换来一条新的展示管线")

    # ⭐ 「始终允许」那条约束是**自动满足**的，不是我们特意去掉的
    check("risk=2：标准卡片，可\"始终允许\"" in _app,
          "⭐⭐ 「始终允许」只在 risk=2 画；而 run_command 的 floor=3 "
          "⇒ C/D 弹窗天然没有它。"
          "📌 当初的质疑：「用户开的本来就是 auto，要『始终』什么东西呢？"
          "下一个弹窗依旧是被怀疑意图对不上，而不是跟之前长得一样的高危命令」")

    from core.os_layer import dsl
    check(dsl.action_floor("run_command") == 3, "⚠️ floor 表一个字没改")
    src = module_text("core.os_layer.safety")
    check("risk=3：永远返回 False" in src,
          "⭐ safety 的铁律**一个字没动** —— 这道闸加在 auto 那条路上，"
          "不是改风险等级。📌 risk 等级按 action【类型】定，"
          "危险与否按命令【内容】判，两者正交")


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 74)
    print("auto 模式命令危险判定（四态）")
    print("=" * 74)
    t_vendor_table()
    t_script_prescan()
    t_lookaway_safety()
    t_fail_safe()
    t_cache_key_carries_intent()
    t_intent_source()
    t_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
