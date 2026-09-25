# -*- coding: utf-8 -*-
"""SkillWriter 的散文不许直接漏进 UI。

═══ 为什么这条最阻塞后续推进 ═══

它本身不算严重，但它**让屏幕不可信** —— 而后面每一项都要靠实测验证。

实测：模型那一次**没调 WriteSkill**
（`[SkillWriter] 模型未调 WriteSkill（首次），输出：我看到了。你的Skill描述是"dsakdkasd"…`，
后面有自动重试），它吐的那一大段废话被无条件 `yield` 进了聊天气泡。
而 memory 里**没有这段** —— 于是 Nano 自己导出对话时，导出的内容和屏幕对不上。

"要不是我回去看了一眼它导出的东西，就真出事了。"
为此烧掉整整两轮：拿着那份导出记录做的分析全是错的。

📌 判据：**流到屏幕上的文字和进入记忆的文字是两条独立通道。**
   只往其中一条写 = 给用户和模型看两份不同的对话，
   之后所有"实测复现"都不可信。

═══ 修法 ═══

缓冲。写手的开场白先扣着，等 `tool_input_delta` 真的来了
（= 这次确实在产出代码）再放行；一直没来就整段丢弃，交给重试。

⚠️ 进重试之前必须 **clear 缓冲 + 复位 `_ws_had_preface`**：
   只清缓冲不复位标志的话，重试成功时会因为"上次说过话"而既不放缓冲
   （已空）也不发默认开场白 —— 变成一句话都没有。

用法：
  py -3.10 tests\cases\t_skillwriter_preface.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


SRC = module_text("core.orchestrator")
TREE = ast.parse(SRC)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise LookupError(f"def {name} not found")


FN = _fn("_generate_skill_with_writer")
SEG = ast.get_source_segment(SRC, FN) if FN else ""
CODE = "\n".join(ln for ln in (SEG or "").splitlines() if not ln.strip().startswith("#"))


def t_buffered() -> None:
    print("\n[1] ⭐⭐ 写手的开场白先缓冲，不直接转发")
    check(FN is not None, "前置条件：找得到 _generate_skill_with_writer")
    if FN is None:
        return
    check("_ws_preface_buf" in CODE, "⭐ 存在开场白缓冲区")

    # 两条流（首次 + 重试）都要 append 而不是直接 yield
    appends = CODE.count("_ws_preface_buf.append(")
    check(appends >= 2,
          "⭐ 首次与重试**两条流**都把 final_text 扣进缓冲",
          f"{appends} 处 append（应 ≥2：主路径 + 重试）")

    # 扣下之后必须紧跟 continue，否则等于既缓冲又转发。
    # ⚠️ 用 AST 而不是文本：两条流的缩进不同，硬编码空格会漏判（第一版就漏了一处）。
    def _append_then_continue(node) -> int:
        n = 0
        for blk in ast.walk(node):
            body = getattr(blk, "body", None)
            if not isinstance(body, list):
                continue
            for a, b in zip(body, body[1:]):
                if not isinstance(b, ast.Continue):
                    continue
                if (isinstance(a, ast.Expr) and isinstance(a.value, ast.Call)
                        and isinstance(a.value.func, ast.Attribute)
                        and a.value.func.attr == "append"
                        and isinstance(a.value.func.value, ast.Name)
                        and a.value.func.value.id == "_ws_preface_buf"):
                    n += 1
        return n

    conts = _append_then_continue(FN)
    check(conts >= 2,
          "⭐ 扣进缓冲之后紧跟 `continue` —— 否则既缓冲又转发，等于没修",
          f"{conts} 处（应 ≥2）")


def t_released_only_on_real_code() -> None:
    print("\n[2] 只有确认在产出代码时才放行")
    i_flag = CODE.find("_ws_dialog_started = True")
    i_release = CODE.find("for _p in _ws_preface_buf:")
    check(i_flag > 0 and i_release > i_flag,
          "⭐ 放行发生在 `_ws_dialog_started = True` 之后 —— "
          "那一刻才知道 tool_input 真的来了")
    check(CODE.count("for _p in _ws_preface_buf:") >= 2,
          "首次与重试两条流都会放行缓冲",
          f"{CODE.count('for _p in _ws_preface_buf:')} 处")
    check(CODE.count("_ws_preface_buf.clear()") >= 3,
          "⭐ 三处清空：主路径放行后 / 进重试前丢弃 / 重试放行后",
          f"{CODE.count('_ws_preface_buf.clear()')} 处")


def t_retry_resets_flag() -> None:
    """⚠️ 这条是我写的时候差点漏掉的那个坑，单独钉住。"""
    print("\n[3] ⚠️ 进重试之前，缓冲和标志【一起】复位")
    i_log = CODE.find("模型未调 WriteSkill（首次）")
    check(i_log > 0, "前置条件：找得到进重试那一处")
    if i_log <= 0:
        return
    tail = CODE[i_log:i_log + 600]
    check("_ws_preface_buf.clear()" in tail,
          "⭐ 丢掉首次那段失败产物")
    check("_ws_had_preface = False" in tail,
          "⭐⭐ `_ws_had_preface` 也复位 —— 只清缓冲不复位标志的话，"
          "重试成功时会一句开场白都没有")


def t_no_unconditional_forward() -> None:
    """反向：证明「无条件转发」那个写法真的不在了。"""
    print("\n[4] 反向：不再有「收到就转发」的裸路径")
    # 旧写法的特征：final_text 分支里紧跟一个无条件 yield，中间没有缓冲判断
    bad = "_ws_had_preface = True\n                    yield _ev\n"
    bad2 = "_ws_had_preface = True\n                    yield _ev2\n"
    check(bad not in CODE and bad2 not in CODE,
          "⭐ 旧的「标记一下就原样转发」写法已消失")
    # 前置条件：yield 本身还在（别把整条通路删了还全绿）
    check("yield _ev" in CODE and "yield _ev2" in CODE,
          "前置条件：非文字事件仍然照常转发（没有把整条流掐掉）")


def t_spec_retry_before_downgrade() -> None:
    """⭐⭐⭐ SkillSpec 校验失败 → 先带着报错重试一次，再谈降级（2026-08-13）。

    ═══ 为什么加这一次重试 ═══

    实测（拆探索那天）：

        SkillSpec hard_validate 失败: Permission mismatch: side_effects contains
        'shell', which requires permission_level='dangerous', but current is
        'external_action'

    一个**字段值不匹配**，报错本身已经把正确答案写出来了 —— 而当时一次重试都没给，
    直接降级成"不带 SkillSpec 的直接代码生成"。
    📌 **这不是安全问题，是每次都在白白掉一档质量**：
       通过校验的 SkillSpec 会被注入代码生成阶段，比让模型凭需求原文猜强得多。

    ═══ ⚠️ 为什么【不】把它改成 fail-closed ═══

    📌 **fail-closed 的适用条件是「缺的那件事只有对方能给」。**
       `handoff_summary` / `open_questions` 缺的是用户与主模型该给的 → 停下来问是对的；
       而「permission_level 应该是 dangerous」是写代码这一方**自己该算对的内部细节**，
       抛给用户是纯噪音，用户根本无从回答。
       **缺自己该算对的东西，正确出口是重试，不是提问。**
    """
    print("\n[5] ⭐⭐⭐ SkillSpec 校验失败先重试一次，且旧报错不许丢")
    # ⚠️ 用 `CODE`（**已剥掉注释**那份），不是 `SEG`。
    # 📌 这次改动的大段说明注释里，逐字包含下面每一个要查的名字 ——
    #    直接在源码文本里查，等于让注释把断言喂绿，删掉代码也照样全过。
    seg = CODE
    check(bool(seg), "前置条件：取到 _generate_skill_with_writer 的源码（已剥注释）")

    check("_first_errs" in seg and "_retry_query" in seg,
          "⭐ 校验失败时带着报错重试一次")
    check("if _first_errs:" in seg,
          "⚠️ **只在有具体报错时才重试** —— 另外两条 return None"
          "（provider 报错 / SkillSpec 构建异常）没有新信息可给。"
          "📌 没有新信息可给的重试，只是把成本翻倍")

    # 🔴 整组里最容易被后人顺手改掉、而且改掉就复发 的那一条
    check("_retry_errs or _first_errs" in seg,
          "⭐⭐⭐ **重试也失败时把旧报错接住**（`_retry_errs or _first_errs`）—— "
          "🔴 `_generate_skill_spec` 进门就清空 `_last_spec_errors`，不接住的话"
          "下游降级分支会拿到空列表、`spec_injection` 变空串 —— "
          "**正是那次「双重伤害」的复发路径**（代码生成阶段既没有 spec、"
          "也不知道刚才错在哪，于是带着同一个违规继续往下走，最后真的部署上去了）。"
          "📌 **一个「进门先清空」的字段，任何重试都必须先把旧值接住。**")

    # 反向：这次改的是"降级之前先试一次"，不是把降级删掉
    check("降级为直接代码生成" in seg and "spec_injection" in seg,
          "⚠️ 反向：降级兜底一行没动（重试仍失败时照旧走它）")

    # 重试要有可见状态行，不是静默重跑一次
    _pre = seg.split("_retry_query")[0][-900:]
    check("yield" in _pre and "SkillSpecGen" in _pre,
          "⚠️ 重试前给用户一个可见的状态行（不静默重跑）")


def main() -> int:
    t_buffered()
    t_released_only_on_real_code()
    t_retry_resets_flag()
    t_no_unconditional_forward()
    t_spec_retry_before_downgrade()
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
