# -*- coding: utf-8 -*-
"""CodeMirror 初始化竞态：没就绪的操作必须【全部】排队。

═══ 现场 ═══

用户反复报"代码框里的改动存活不过最小化"。先后归因于 `syncToPython` 硬编码、
以及 `_minimize` 的 UnboundLocalError（后者是真的，日志实锤）。修完之后用浏览器
连上 8080 实测，直接读到了真相：

    NanoCM.editors.get('431') → { readOnly: true, contentEditable: "false",
                                  syncToPython: false }

**代码框根本不可编辑** —— 所以"改动存活不过最小化"的前提压根不成立，
用户能做的只有看。而手动 `NanoCM.setEditable(431, true)` 一调就好，
说明 JS 接口没问题，是那次调用**没生效**。

═══ 根因 ═══

生成结束时的收尾是这两步（app.py `_sk_stream_dialog` 分支）：

    await self._cm_set_value(area, code)       # 内容
    await self._cm_set_editable(area, True)    # 解锁 + 打开回传

JS 侧 `setValue` / `append` 在编辑器还没建好时会写进 `_pending` 队列，
init 完成后补上。**只有 `setEditable` 是 `if (!item) return false`** ——
静默、永久丢失。

于是编辑器 init 慢一点的时候：
  · 内容进队列 → 后来被应用 → **代码是有的**
  · 解锁被扔掉 → **框是只读的，syncToPython 还是 false**

内容在、编辑不了，看起来完全不像一个竞态。
⚠️ 而 CodeMirror 是 `import(CDN.…)` 动态加载的，init 快慢直接取决于网速 ——
   这正是"有时能改有时不能"的来源。

📌 判据：**同一批操作里，只要有一个能排队，其余就都得能排队。**
   异步初始化面前，"没就绪就放弃"和"没就绪就排队"混用 =
   让一部分状态随机丢失，且丢的那部分不会报错。

═══ 为什么用 AST/文本查 JS 而不是跑浏览器 ═══

这段 JS 是嵌在 app.py 里的字符串，没有 JS 测试环境。要钉的是**结构对称性**
（三个操作都有 pending 分支 + init 都会 flush），文本级检查足够且稳定。

用法：
  py -3.10 tests\t_cm_pending_ops.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP = module_text("app")


def _js_method(name: str) -> str:
    """从 app.py 里那段 NanoCM JS 中抠出一个方法体（按大括号配平，不靠缩进）。"""
    # `async ` 前缀要吃掉 —— `init` 是 `async init(...)`，第一版漏了它，
    # 表现成"抠不到 init"，把一条真实断言变成假失败。
    m = re.search(rf"\n\s*(?:async\s+){{0,1}}{re.escape(name)}\(([^)]*)\)\s*\{{", APP)
    if not m:
        return ""
    i = APP.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(APP):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[i:j + 1]
        j += 1
    return ""


def t_all_ops_queue() -> None:
    print("\n[1] ⭐⭐ 三个操作在编辑器未就绪时都必须排队")
    for name, marker in (("setValue", "_getPending"),
                         ("append", "_getPending"),
                         ("setEditable", "_getPending")):
        body = _js_method(name)
        check(bool(body), f"前置条件：抠得到 {name} 的方法体")
        if not body:
            continue
        check(marker in body,
              f"⭐ {name} 在未就绪时走 _getPending 排队（不是静默 return false）",
              "只有 return false，没有排队" if marker not in body else "")


def t_pending_slot_exists() -> None:
    print("\n[2] pending 结构里有 editable 这个槽")
    body = _js_method("_getPending")
    check(bool(body), "前置条件：抠得到 _getPending")
    check("editable" in body,
          "⭐ 初始结构含 editable（否则 setEditable 写进去也没人 flush）",
          body.strip().replace("\n", " ")[:120])


def t_init_flushes_editable() -> None:
    print("\n[3] init 完成后会把排队的 editable 补上，且【在内容之后】")
    body = _js_method("init")
    check(bool(body), "前置条件：抠得到 init")
    if not body:
        return
    check("pend.editable" in body,
          "⭐ init 的 flush 段处理了 pend.editable")

    # 顺序很重要：setEditable(true) 会 emitChange 做一次初始同步，
    # 必须等内容就位，否则同步回 Python 的是空串。
    i_val = body.find("pend.setValue")
    i_edit = body.find("pend.editable")
    check(i_val != -1 and i_edit != -1 and i_val < i_edit,
          "⭐ editable 在 setValue/deltas 【之后】才补 —— "
          "setEditable 会 emitChange 初始同步，内容没到位就同步的是空的",
          f"setValue@{i_val} editable@{i_edit}")

    check("_pending.delete(key)" in body,
          "⚠️ flush 完清掉队列（否则重建同一个 id 会重放旧操作）")


def t_python_side_unchanged() -> None:
    """⚠️ 前置条件：Python 侧那两步收尾还在。

    只验 JS 排队、不验调用方，等于把"根本没人调"这种情况判成通过。
    """
    print("\n[4] ⚠️ 前置条件：Python 侧的收尾（先灌内容、再解锁）仍然存在")
    tree = ast.parse(APP)
    calls = [c for c in ast.walk(tree)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr in ("_cm_set_value", "_cm_set_editable")]
    names = {c.func.attr for c in calls}
    check("_cm_set_value" in names, "还有人调 _cm_set_value")
    check("_cm_set_editable" in names, "还有人调 _cm_set_editable")

    edit_true = [c for c in calls if c.func.attr == "_cm_set_editable"
                 and any(isinstance(a, ast.Constant) and a.value is True for a in c.args)]
    check(bool(edit_true),
          "⭐ 确实有一处 _cm_set_editable(..., True) —— 那就是这个 bug 丢掉的那一步",
          f"{len(edit_true)} 处")


def t_change_bridge_reaches_element() -> None:
    """JS→Python 的 cm_change 桥必须真的到得了那个元素的 handler。

    ═══ 实测═══

    `emitChange` 原来的两条分支：
        if (el && el.$emit)   el.$emit('cm_change', value)      ← 主路
        if (window.emitEvent) window.emitEvent('cm_change', …)  ← 兜底

    浏览器里实测：`window.getElement(421)` 返回的是**原生 HTMLDivElement**
    （`ui.element('div')` 在这个 NiceGUI 版本没有 Vue 包装）→ `$emit` 不存在
    → 主路是**死代码**，每次都走兜底。而兜底发的是**全局**事件，
    对应 `ui.on('cm_change')`；我们注册的却是 `code_area.on('cm_change', …)`（元素级）。

    结果：**`_on_cm_change` 一次都没被调用过**，而且全程零报错。
    连锁后果就是用户报了三轮的「编辑器改动存活不过最小化」
    （改动进不了 `code_holder`，重建时按旧内容恢复），
    以及指纹守卫在打字部署那条路上永远比不出差异。

    📌 判据：**"有 fallback" 不等于"能工作"。**
       主路是死代码时，fallback 就是唯一路径 —— 它必须自己单独成立。
    """
    print("\n[5] ⭐⭐ cm_change 必须发成【元素上的 DOM 事件】，不是全局事件")
    body = _js_method("emitChange")
    check(bool(body), "前置条件：抠得到 emitChange")
    if not body:
        return

    # ⚠️ 先剥掉 `//` 注释行再比位置。这段注释里就写着 `$emit` / `emitEvent`
    # 的来龙去脉，直接在原文里 find 会命中注释，把顺序判反 —— 假失败。
    code_only = "\n".join(ln for ln in body.splitlines()
                          if not ln.strip().startswith("//"))
    i_dom = code_only.find("dispatchEvent")
    i_emit = code_only.find("$emit")
    i_glob = code_only.find("emitEvent")
    body = code_only
    check(i_dom != -1, "⭐ 有 DOM CustomEvent 这条路（元素级 .on() 收得到的那种）")
    check(i_dom != -1 and (i_emit == -1 or i_dom < i_emit) and (i_glob == -1 or i_dom < i_glob),
          "⭐ DOM 事件排在最前 —— $emit 在本版本恒不存在，全局 emitEvent 到不了元素",
          f"dom@{i_dom} $emit@{i_emit} global@{i_glob}")
    check("'cm_change'" in body, "事件名是 cm_change")

    # Python 侧：注册时必须声明要 detail，否则拿不到我们的载荷
    tree = ast.parse(APP)
    ons = [c for c in ast.walk(tree)
           if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
           and c.func.attr == "on" and c.args
           and isinstance(c.args[0], ast.Constant) and c.args[0].value == "cm_change"]
    check(bool(ons), "前置条件：Python 侧确实注册了 cm_change")
    check(any(len(c.args) >= 3 and isinstance(c.args[2], (ast.List, ast.Tuple))
              and any(isinstance(e, ast.Constant) and e.value == "detail"
                      for e in c.args[2].elts)
              for c in ons),
          "⭐ 注册时声明了 ['detail'] —— 不声明的话 NiceGUI 只回传通用属性，"
          "拿不到我们塞在 detail 里的代码")

    # handler 认得 detail 形状
    handler = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_on_cm_change":
            handler = ast.get_source_segment(APP, n) or ""
    check(handler is not None and '"detail"' in handler,
          "⭐ handler 认得 detail 这种载荷形状（少认一种就是静默丢弃）")


def main() -> int:
    t_all_ops_queue()
    t_pending_slot_exists()
    t_init_flushes_editable()
    t_python_side_unchanged()
    t_change_bridge_reaches_element()
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
