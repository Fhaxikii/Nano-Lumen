# -*- coding: utf-8 -*-
"""get_manifest 的外层形状必须在审计窗口就被拦住（2026-08-05 实测）。

═══ 这个套件在验什么 ═══

`ExtractIP` 部署成功、热载成功、UI 显示 READY，但加载日志里一行
`⚠️ manifest 缺少 name，已跳过` —— **manifest 被整条丢弃，模型的工具清单里
根本没有这个 Skill。** 用户看到它在列表里、状态 READY，Nano 却调不到它，
而没有任何人被告知。

根因：模型返回了 OpenAI 风格的嵌套外壳 `{"type":"function","function":{...}}`，
而 registry 取的是**顶层** `name`。协议 把内层字段规定得极细
（参数逐项对应、类型必须小写），**却从来没规定过骨架** —— 规定了细节、没规定形状。

⚠️ **为什么必须有单测**：这条是**概率性**的（取决于模型这次怎么写外层），
同批生成的 `GetSystemMemoryUsage` 就蒙对了。概率性失效比必然失效更难发现。

用法：
  py -3.10 tests\t_d14_manifest_shape.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()

from core.orchestrator import Orchestrator, _SKILL_PROTOCOL
from core.skill_check import validate_skill_code
from core.registry import SkillRegistry

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_TPL = '''
from core.schema import BaseSkill, SkillResult, SkillSpec, SideEffect, PermissionLevel
class X(BaseSkill):
    def get_manifest(self):
        return %s
    def get_spec(self):
        return SkillSpec(name="X", side_effects=[SideEffect.NONE],
                         permission_level=PermissionLevel.READONLY)
    async def run(self):
        """doc"""
        return SkillResult(True)
'''

_GOOD = '{"name": "X", "description": "d", "parameters": {"type": "object", "properties": {}}}'
_OPENAI = '{"type": "function", "function": {"name": "X", "parameters": {}}}'
_GEMINI = '{"function_declarations": [{"name": "X"}]}'
_NONAME = '{"desc": "d", "params": {}}'


def _errs(manifest_src: str):
    ok, errs = validate_skill_code(_TPL % manifest_src)
    return ok, [e for e in errs if "manifest" in e]


def t_shapes() -> None:
    print("\n[1] 四种外层形状，判据必须与 registry 完全一致")
    ok, e = _errs(_GOOD)
    check(ok and not e, "顶层 name → 放行", str(e))

    ok, e = _errs(_OPENAI)
    check(not ok and e, "⭐ OpenAI 嵌套外壳 → 拦住（实测撞到的就是这个）")
    check(e and "最外层" in e[0], "报错说清了怎么改（放到最外层）", e[0][:44] if e else "")
    check(e and "模型看不见" in e[0],
          "⭐ 并说清后果 —— 否则读的人会以为只是个小格式问题", e[0][:44] if e else "")

    ok, e = _errs(_GEMINI)
    # 🔴 2026-08-29 反转：Gemini 的 function_declarations 形式 **从放行改成拦住**。
    #
    # ⚠️ 这条断言变了，但**这个测试真正守的那条没变** —— 见下面那个「反向」循环：
    #    `check(ok == bool(_reg_name(d)), "校验器与 registry 对 X 结论一致")`。
    #    它不关心认不认 Gemini，只关心**两边说的是不是同一件事**。
    #    ⇒ 这次不是破坏一致性，是把一致点**从「都放行」移到了「都拒绝」**。
    #
    # 为什么移：get_manifest 的提示词里早就把 Gemini 风格列为 WRONG，
    # 而项目现在只接 Anthropic。留着「提示词说别用、但用了也能跑」这种状态，
    # 等于给一条**写错了却能跑**的路。
    # 📌 **一条规则如果提示词里说不行、实现里却放行，那它不是宽容，是没生效。**
    check(not ok and e, "⭐ Gemini 列表外壳 → 拦住（2026-08-29 反转，见上）")
    check(e and "最外层" in e[0], "报错说清了怎么改（放到最外层）", e[0][:44] if e else "")

    ok, e = _errs(_NONAME)
    check(not ok and e, "顶层既没 name 也没 function_declarations → 拦住")
    check(e and "desc" in e[0], "报错里列出实际的顶层键，方便定位", e[0][:44] if e else "")


def t_registry_agreement() -> None:
    print("\n[2] ⭐ 校验器与 registry 必须对同一份 manifest 给出一致结论")
    # 这是本条的核心不变量：校验器放行的，registry 必须能取到名字；
    # 校验器拦住的，registry 必须取不到。两边不一致就等于校验白做。
    import json

    def _reg_name(d):
        return SkillRegistry._manifest_name(d)

    cases = [
        ("顶层 name", {"name": "X"}, True),
        ("OpenAI 外壳", {"type": "function", "function": {"name": "X"}}, False),
        # ⚠️ 2026-08-29 起 registry 也不认它了（与校验器对齐，见上方留痕）。
        ("Gemini 列表", {"function_declarations": [{"name": "X"}]}, False),
        ("啥都没有", {"desc": "d"}, False),
    ]
    for nm, d, should_resolve in cases:
        got = bool(_reg_name(d))
        check(got == should_resolve,
              f"registry 对「{nm}」的判断符合预期", f"取到名字={got}")

    # 反向：校验器的结论必须和 registry 一致
    for nm, src, d in (("顶层 name", _GOOD, {"name": "X"}),
                       ("OpenAI 外壳", _OPENAI, {"type": "function", "function": {"name": "X"}}),
                       ("Gemini 列表", _GEMINI, {"function_declarations": [{"name": "X"}]}),
                       ("啥都没有", _NONAME, {"desc": "d"})):
        ok, _ = _errs(src)
        check(ok == bool(_reg_name(d)),
              f"⭐ 校验器与 registry 对「{nm}」结论一致",
              f"校验器放行={ok} / registry 取到={bool(_reg_name(d))}")


def t_indirect_return_passes() -> None:
    print("\n[3] 看不清的写法要放行（宁可漏判，不要误拦正确的 Skill）")
    ok, e = _errs("self._build_manifest()")
    check(ok and not e, "间接返回 → 放行（静态看不到字典字面量）")
    # 纪律：确认"放行"不是因为整段代码压根没过校验
    ok2, all_errs = validate_skill_code(_TPL % "self._build_manifest()")
    check(ok2, "前置条件：这段代码其余部分是合规的，放行不是因为别的错误",
          str(all_errs[:1]))


def t_protocol_documents_it() -> None:
    print("\n[4] 协议必须给出顶层完整示例，并显式禁止那个外壳")
    check('"name": "SkillName"' in _SKILL_PROTOCOL,
          "⭐ 给了可照抄的顶层完整示例（原来只规定内层字段，没规定骨架）")
    check('"type": "function", "function"' in _SKILL_PROTOCOL,
          "⭐ 显式点名了 OpenAI 外壳是错的（不点名模型就会照训练习惯填）")
    check("function_declarations" in _SKILL_PROTOCOL, "Gemini 形式也一并说明")
    check("cannot call it" in _SKILL_PROTOCOL or "never sees" in _SKILL_PROTOCOL,
          "并说清后果：Skill 装上了但模型看不见它")


def main() -> int:
    print("=" * 74)
    print("[D14] get_manifest() 外层形状")
    print("=" * 74)
    for fn in (t_shapes, t_registry_agreement, t_indirect_return_passes,
               t_protocol_documents_it):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")

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
