# -*- coding: utf-8 -*-
"""待办卡片剩余 5 条 UI 细节（2026-08-06 报的 ①②③④⑦⑨）。

═══ 每条各自的判据 ═══

**① 详情弹窗不能下滑。**
原来是 `max-height:calc(70vh-110px); overflow:auto` 挂在内容列上。
⚠️ 先判成了"flex 子元素 `min-height:auto` 不肯收缩"，但在同一个浏览器里
做新旧结构对比时**两种写法都能滚** —— 那个诊断**没被证实**，真实原因没复现出来。
（Quasar 的 `q-dialog` 自己接管滚动/滚轮，手写 overflow 未必吃得上。）
📌 所以不按"我猜的原因"修，改用 NiceGUI 的 `ui.scroll_area`（内部是 QScrollArea），
   弹窗里的滚动语义由组件自己保证。
   **拿不准原因的时候，用那个"本来就该用"的组件，比修一个假设更稳。**
顺带干掉 `calc(70vh - 110px)`：那是把表头高度写死成 110px，表头一改就错位。

**② `int_xxx` 挪到左下角、压掉那道空隙。**
原来 `int_xxx` 在描述下面、replay 又单独占一行，中间的空隙把卡片撑高。
合成一行：左 `int_xxx`、右 replay，`space-between` 两端对齐。

**③ 文案 `回复这条` → `replay`。**
与将来"选中文字右键 replay"那个入口同名 —— 同一个动作，两个入口，名字必须一致。

**⑦⑨ `...` 与 `<>`。**
待审代码类压根不该有 `...`（它有 `<>`，看的是代码本身）；两者位置要一致。
正解不是"把两个按钮对齐"，而是**让它们永远不会同时出现** —— 右上角只放一个。

⑦ 的另一半「短文字也出现 `...`」：
第一版 `len(text) > 46` 判，中文宽度是 ASCII 两倍 → 短中文误挂。
按东亚宽度折算也只是把误差变小 —— **真正的容量随窗口宽度变**，
同一句话窄窗截断、宽窗不截断，Python 侧根本算不出来。
📌 **只有浏览器知道有没有真截断**（`scrollWidth > clientWidth`）。
   所以默认渲染出来，由注入的 JS 决定藏不藏；**默认可见**是刻意的 ——
   JS 没跑成的后果是多一个没用的按钮，而不是"真截断了却没入口"。

**④ 引用态的视觉反馈。**
composer 左边的 `❯` 换成引用符号；发出去那条消息用户名右边同样换掉，
并在上方挂一条被回答的原文。⚠️ 复位发生在 orchestrator 的 `finally` 里，
那一刻 UI 没有参与 —— 所以 composer 那个符号必须能靠轮询自愈。

用法：
  py -3.10 tests\cases\t_u7_card_layout.py
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


# ⚠️ 2026-08-29：色值收进了 CSS 变量（--nano-amber），断言改成认变量。
#    📌 这条守的是「**这里有琥珀色高亮**」，不是「色值必须写成六位十六进制」——
#       一条断言如果连**表达方式**都锁死，那它拦的是重构，不是回归。
def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP = module_text("app")
TREE = ast.parse(APP)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(APP, n) or ""
    raise LookupError(f"def {name} not found")


def _code_of(name: str) -> str:
    """函数源码，**剥掉 `#` 注释行**。

    ⚠️ 今天在这个坑里栽了三次：注释里写着 bug 的来龙去脉，
    裸文本匹配会命中注释，把真断言变成假通过/假失败。
    """
    seg = _fn(name)
    return chr(10).join(ln for ln in seg.splitlines() if not ln.strip().startswith("#"))


def t_dialog_scrolls() -> None:
    print("\n[1] ① 详情弹窗可滚动（照抄知识库那个已验证能用的写法）")
    seg = _fn("_show_full_todo")
    check(bool(seg), "前置条件：找得到 _show_full_todo")
    code = _code_of("_show_full_todo")

    check("ui.element('div')" in code,
          "⭐ 内容区是裸 div —— 不是 ui.column()（那套 flex 类会和手写 overflow 打架）")
    check("overflow-y:auto" in code, "内容区 overflow-y:auto")
    check("ui.scroll_area" not in code,
          "⚠️ 不用 ui.scroll_area+flex —— 本项目已试过并否掉（见 KB 弹窗里的留痕）")
    check("flex-direction:column" not in code,
          "⚠️ 卡片不加 flex 列，与那个能用的弹窗保持一致")
    check("calc(70vh - 110px)" not in code,
          "⚠️ 不再把表头高度写死成 110px（表头一改就错位）")

    # ⚠️ 前置条件：被抄的那个弹窗确实是这个写法。它哪天变了，这条要重看。
    kb = _code_of("_show_kb_file_content_dialog")
    check("ui.element('div')" in kb and "overflow-y:auto" in kb,
          "⚠️ 前置条件：知识库「查看内容」确实用的就是这个结构（抄的源头还在）")


def t_meta_and_replay_same_row() -> None:
    print("\n[2] ② int_xxx 与 replay 同一行、两端对齐")
    seg = _fn("refresh_pinned_interactions")
    check(bool(seg), "前置条件：找得到 refresh_pinned_interactions")
    i_meta = seg.find("_meta = r.interaction_id")
    i_row = seg.rfind("justify-content:space-between", 0, i_meta) if i_meta > 0 else -1
    check(i_meta > 0 and i_row > 0,
          "⭐ int_xxx 落在一个 space-between 的行里（左 meta / 右 replay）")
    i_replay = seg.find("'回复'", i_meta) if i_meta > 0 else -1
    check(i_replay > i_meta,
          "⭐ 「回复」按钮和 int_xxx 在同一段里（不再各占一行）")


def t_replay_label() -> None:
    print("\n[3] ③ 文案叫 replay")
    seg = _fn("refresh_pinned_interactions")
    # ⚠️ 2026-08-21 文案改成中文「回复」（已定，为 抽 i18n 做准备）。
    #    ⭐ 断言要守的是**两个入口同名**，不是某个具体字符串 ——
    #       📌 同一个动作在两个入口叫不同名字，用户会以为是两件事。
    check("ui.button('回复'" in seg, "⭐ 待审卡按钮文案是「回复」")
    _menu = APP[APP.find("emit('nano_quote_selection'") - 400:
                APP.find("emit('nano_quote_selection'")]
    _esc = "".join(chr(92) + "u%04x" % ord(c) for c in "回复")
    check(_esc in _menu or "回复" in _menu,
          "⭐⭐ 选中文字的右键菜单用**同一个**文案（只改一处就不同名了）")
    check("ui.button('回复这条'" not in seg, "⚠️ 旧文案「回复这条」已不在渲染代码里")


def t_one_button_only() -> None:
    print("\n[4] ⑦⑨ `...` 与 `<>` 互斥，右上角只放一个")
    seg = _fn("refresh_pinned_interactions")
    i_audit = seg.find("_is_audit")
    check(i_audit > 0, "⭐ 先判是不是待审代码类（_is_audit）")
    # 必须是 if/else，不是两个独立 if
    check("if _is_audit:" in seg and "\n                        else:" in seg,
          "⭐ 用 if/else —— 两个按钮结构上不可能同时出现（这才是「对齐」的正解）")
    check("icon='code'" in seg and "icon='more_horiz'" in seg,
          "前置条件：两个按钮都还在（只是分到了两个分支）")


def t_truncation_decided_by_browser() -> None:
    print("\n[5] ⑦ 截断与否由浏览器判，且默认可见")
    seg = _fn("_u7_sync_more_buttons")
    check(bool(seg), "存在 _u7_sync_more_buttons")
    check("scrollWidth" in seg and "clientWidth" in seg,
          "⭐ 判据是 scrollWidth > clientWidth（唯一准确的来源）")
    check("addEventListener('resize'" in seg,
          "⭐ 窗口缩放后重判（窄窗截断、拉宽就不该再有按钮）")
    check("__nanoU7Resize" in seg, "resize 监听只绑一次")

    card = _fn("refresh_pinned_interactions")
    check("u7-more-btn" in card and "u7-todo-text" in card,
          "前置条件：两个 class 都打上了，JS 才找得到")
    # ⚠️ 反向：Python 侧不许再留字数/宽度阈值
    check("_U7_TRUNCATE" not in APP,
          "⚠️ Python 侧不再有任何字数/宽度阈值常量（那是猜，不是判）")
    check("_u7_sync_more_buttons()" in card,
          "⭐ 卡片重画之后会同步一次（DOM 换了就要重判）")


def t_reply_visual_feedback() -> None:
    print("\n[6] ④ 引用态的视觉反馈（composer + 已发出的消息）")
    seg = _fn("_refresh_reply_prompt")
    check(bool(seg), "存在 _refresh_reply_prompt")
    check("_REPLY_PROMPT" in seg and "_NORMAL_PROMPT" in seg,
          "⭐ composer 左边那个符号按引用态切换")
    check("getattr(el, \"text\", None) == _want" in seg or "== _want" in seg,
          "⚠️ 幂等 —— 它被 1.5 秒定时器反复调，没变就别动 DOM")

    # 自愈：复位发生在 orchestrator 的 finally 里，UI 那时没参与
    pin = _fn("refresh_pinned_interactions")
    check("_refresh_reply_prompt()" in pin,
          "⭐ 轮询里也调一次 —— 轮次结束的复位没人通知 UI，只能自愈")

    # 已发出的消息：引用块 + 用户名右边的符号
    src = APP
    check("_rt_iid" in src and ("border-left:2px solid var(--nano-amber)" in src
                                or "border-left:2px solid #e6a94e" in src),
          "⭐ 发出的消息上方挂被回答的原文（引用块）")
    # ⚠️ 之后判据从 `_rt_iid` 变成 `_rt_on`（选中文字的引用也要出这个符号，
    #    而它没有 iid）。断言跟着**语义**走：符号由「当前是不是引用态」决定。
    check("_mark = self._REPLY_PROMPT if _rt_on else self._NORMAL_PROMPT" in src,
          "⭐ 用户名右边的符号也跟着变（与 composer 同一套符号）")
    check("_rt_on = bool(_rt_iid) or (" in src,
          "⭐ 引用态 = 引用待审卡 or 引用选中文字（两个入口，同一套 UI）")


def t_card_only_for_audit() -> None:
    """⭐⭐⭐ [2026-08-13] 待办卡**只为 Skill 代码审计服务**。

    ═══ 实测（删 Skill）═══

        01:21:27.536  [Interaction] Skill 管理确认已登记 int_61bafcb83b

    屏幕上同时出现两样东西，文字**逐字相同**：
      · nano 气泡：「将要删除 Skill「LocalIPExtractor」…请回复确认继续，或回复取消。」
      · 待办卡：  「【待确认操作】将要删除 Skill「LocalIPExtractor」…」
    而且那张卡**一个按钮都没有** —— 确认仍然要回输入框打字。

    📌 **一张卡片如果只是把气泡里的话复述一遍，它就不是 UI，是噪音。**
    📌 **待办卡该存在的唯一理由，是它承载了自然语言承载不了的东西。**

    `skill_audit` 过得了这条判据（代码块 / 折叠 / `<>` / 部署动作）；其余四种过不了。
    ⭐ 这同时回答了「以后还要不要加新卡」：不是禁止，**是它过不了判据**。

    ⚠️⚠️ 纯显示收缩 —— Interaction 记录一个字不动，
       澄清的 checkpoint 与模型侧 `[Open Interactions]` 全部照旧。
    """
    print("\n[6] ⭐⭐⭐ 待办卡只画 skill_audit（其余四种去掉）")
    seg = _fn("refresh_pinned_interactions")
    code = _code_of("refresh_pinned_interactions")
    check(bool(seg), "前置条件：找得到 refresh_pinned_interactions")

    check("_CARD_KINDS" in code, "⭐ 有一张显式的「哪些 kind 上卡」白名单")
    check("Kind.SKILL_AUDIT" in code, "⭐ 白名单里有 SKILL_AUDIT")
    for k in ("SKILL_CLARIFICATION", "SKILL_MANAGE", "SKILL_SIDE_EFFECT", "OS_RISK"):
        check(f"Kind.{k}" not in code,
              f"⚠️ 渲染代码里不再出现 {k}（连 _KIND_LABEL 的死条目也清了）")

    # ⚠️⚠️ 收缩的是**显示**，不是状态：读出来的原始清单必须仍是全量，
    #    而「目标还在不在」这类判断必须回到未过滤的那一份。
    check("_recs_all" in code and "list_live" in code,
          "⭐ 保留了未过滤的原始清单变量（过滤只发生在渲染用的那份上）")
    i_all = code.find("_recs_all = ")
    i_flt = code.find("_CARD_KINDS")
    check(0 <= i_all < i_flt, "⚠️ 先取全量、再过滤（顺序不能反）")

    i_rt = code.find('_rt["iid"] not in')
    check(i_rt > 0 and "_recs_all" in code[i_rt:i_rt + 120],
          "⭐⭐ 「回复这条」的存活判定读的是**未过滤**那份 —— "
          "否则一条仍然 OPEN 的澄清会因为不再上卡而被误判成已关闭",
          code[i_rt:i_rt + 90] if i_rt > 0 else "找不到存活判定")

    # 反向：证明这不是"把整块渲染删了"—— 卡片本身还在画
    check("_KIND_LABEL" in code and "push_pin" in code,
          "⚠️ 反向：卡片渲染本身没被删掉（只是少画了四种 kind）")


def main() -> int:
    t_dialog_scrolls()
    t_meta_and_replay_same_row()
    t_replay_label()
    t_one_button_only()
    t_truncation_decided_by_browser()
    t_reply_visual_feedback()
    t_card_only_for_audit()
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
