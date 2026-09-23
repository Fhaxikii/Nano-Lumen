# -*- coding: utf-8 -*-
"""用户报的三个边缘 UI 问题。

① Auto 提示把作用范围写窄了（说「屏幕操作」，实际是全部授权弹窗）
② 知识库长文件名盖住 `⋮`（真根因是 column 的 `align-items:flex-start`）
③ 软/硬上限：去掉「互相顶开」，换成禁用保存 + 提示
"""
import ast
import copy
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402

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


_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _fn(name):
    return next(n for n in ast.walk(_TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _code(name):
    return ast.unparse(_fn(name))


# ══════════════════════════════════════════════════════════════════════════
def t_auto_notice_scope() -> None:
    print("\n[1] 🔴 [L25] Auto 提示的作用范围")
    from app import WebUI

    _txt = WebUI._AUTO_ON_NOTICE
    check("屏幕操作" not in _txt,
          "⭐⭐⭐ **不再说「屏幕操作」** —— 用户报的是：它影响的是**全部授权弹窗**"
          "（Skill 审计 / OS 高危确认 / 探索澄清…）。"
          "📌 **一句描述作用范围的提示，如果范围写窄了，它比不写更危险** —— "
          "用户会以为自己只放开了一小块，然后照着这个错的理解去开它。"
          "⚠️ 这一格是**安全相关**的错，不是文案瑕疵", _txt)
    check("授权" in _txt,
          "⭐ 新措辞说的是「授权」这件事本身（参照 Claude Code 的 "
          "「Claude handles permission decisions」，但不写 Claude）", _txt)
    check("Ctrl" in _txt and "急停" in _txt,
          "⚠️ 急停那句保住了 —— 它是用户唯一的退出路径，不许在改文案时丢掉")

    # ⚠️ 两处调用点必须都用这个常量
    _n_const = _SRC.count("self._AUTO_ON_NOTICE")
    check(_n_const == 2 and "已开启 Auto：屏幕操作" not in _SRC,
          "⭐⭐ **两个入口都读同一个常量**，旧字面量一处不剩 —— "
          "📌 **一句出现在两处的文案，必然有一天只改到一处**"
          "（`_toggle_global_auto` 和 `_set_auto` 原来各写了一遍）",
          f"{_n_const} 处引用")


def t_kb_filename_truncation() -> None:
    print("\n[2] 🔴 [L26] 长文件名不许盖住 `⋮`")
    _c = _code("_render_kb_file_card")

    check(_c.count("min-width:0") >= 3,
          "⭐⭐⭐ **链上每一环都写了 `min-width:0`**（左侧组 / column / 内层 row / label）"
          "—— 📌 **一条「不许溢出」的约束，链上每一环都得写**："
          "只写最里面那一层，外层照样把它撑开",
          f"{_c.count('min-width:0')} 处")
    check(_c.count("overflow:hidden") >= 3,
          "⭐ 同一条链上也都写了 `overflow:hidden`")
    check("width:100%" in _c,
          "⭐⭐⭐ **那个 column / 内层 row 被显式压到 `width:100%`** —— "
          "🔴 真根因：`ui.column()` 带 Quasar 的 `items-start`"
          "（`align-items:flex-start`），而在 **column 方向**的 flex 容器里 "
          "`align-items` 管的是**横轴** → 子元素按**内容宽度**撑开，"
          "于是装文件名那个 row 有多长撑多长，"
          "它内部的 `min-width:0` + `text-overflow:ellipsis` **永远不触发**。"
          "📌 **在 `flex-direction:column` 的容器里，`align-items:flex-start` "
          "会让子元素按内容宽度撑开** —— 这是「省略号配置对了却不生效」最常见的成因，"
          "而它长得完全不像一个宽度问题")
    check("flex:1 1 0" in _c,
          "⭐ 文件名 label 的 flex 也写成**显式 CSS** —— "
          "📌 与其判断那个工具类生效没有，不如让这一处**不依赖它**："
          "**一个「可能生效也可能不生效」的依赖，本身就是缺陷**。"
          "⚠️ 而我一开始把根因判成「Tailwind 没生效」，依据是同处的一句注释 —— "
          "核实后不成立（NiceGUI 2.24.2 自带并加载 Tailwind）。"
          "📌 **一处注释里的因果解释，和它旁边那行代码不是同一个证据等级。**")
    check(".tooltip(fname)" in _c,
          "⭐ 截断之后补了 tooltip —— "
          "📌 **一个「把信息藏起来」的显示改动，必须留一条把它看全的路**")
    check("flex-shrink:0" in _c,
          "⚠️ 右侧按钮组仍然 `flex-shrink:0`（它是不该被压缩的那一边）")



def _code_without_docstrings() -> str:
    """app.py 的源码，**剥掉注释和 docstring**。

    🔴 一条「代码里不许出现 X」的断言，必须先剥掉非代码部分 —— 本项目栽过**五次**。
       前四次修的是「剥 # 注释」（ast.unparse 天然会剥）；
       第五次（2026-08-29）才发现 **docstring 不是注释，unparse 会原样保留** ——
       Claude 在 _build_settings_cost_cap 的 docstring 里写了一句
       「不用 .on(update:model-value, …)」，当场把这条断言打中。
    ⚠️ 而且当时**两处**各写了一份 ast.unparse(_TREE)，修一份必漏另一份。
       📌 同一个剥离逻辑写两份，就等着其中一份先漂移 —— 提成一处。

    ⚠️ 注意这个 docstring 里那几个词是**故意不加反引号**的：它自己会被本函数
       剥掉，所以写什么都安全；但同样的话写在 app.py 里就会打中断言。
    """
    t = copy.deepcopy(_TREE)
    for n in ast.walk(t):
        body = getattr(n, 'body', None)
        if (isinstance(body, list) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    ast.fix_missing_locations(t)
    return ast.unparse(t)

def t_cap_guard() -> None:
    print("\n[3] 🔴 [L27] 软/硬上限：步进器 + 顶着走（2026-08-29 换控件）")

    # ══ 这条断言为什么反转了 ═══════════════════════════════════════════
    # 旧断言：「『互相顶开』已经删掉 —— 用户判『这玩意不可靠』」。
    # 🔴 但当初删它**不是因为设计不对，是因为它在滑块上根本没生效**：
    #    滑块的值同步带节流，高速拖动时顶的逻辑读到上一档的值 → 顶不住
    #    → 用户照样能造出 hard < soft。于是加了「非法则保存按钮不可点」
    #    —— **那是止血，不是首选做法**。
    # ⭐ 2026-08-29 换成步进器（一次点击 = 一跳，离散可预见），
    #    **那个失效原因消失了** ⇒ 顶着走回来；止血用的保存按钮也随
    #    「去掉一切保存/取消、改即时生效」一起消失。
    # 📌 **判据没有反转，反转的是「它当时能不能被实现对」。**
    #    一条因为做不到而被删掉的设计，在做得到之后回来，不算出尔反尔。
    _c = _code("_build_settings_cost_cap")

    check("ui.slider" not in _c,
          "⭐⭐⭐ **滑块已经删掉** —— 2026-08-29 定的：「别用滑块了，"
          "改成步进器，复杂度更低更可靠」。"
          "🔴 滑块那套的根本问题是**值同步带节流**，任何「读了值再做决定」的"
          "逻辑在高速拖动时都可能读到陈旧值")
    check("ui.number" in _c and "step=" in _c,
          "⭐ 换成 `ui.number` + 固定步长 —— 一次点击 = 一跳，**离散且可预见**")

    check("_CAP_STEP" in _c and "_CAP_SOFT_MIN" in _c,
          "⭐ 步长和下限走常量，不散落成字面量")

    check("floor" in _c and ("hard_in.value = floor" in _c),
          "⭐⭐⭐ **顶着走回来了**：硬上限被夹到 >= 软 + 一跳。"
          "📌 **与其检测非法状态再提示，不如让它压根出现不了** —— "
          "非法组合无法被造出来，于是「保存按钮变灰 / 数字标红 / 提示该在哪一刻弹」"
          "那一整套全部随之消失，它们解决的是一个本不该存在的问题")
    check(_c.count("_clamp_and_save") >= 3,
          "⭐ 软、硬两个方向共用**同一处**夹逻辑 —— "
          "📌 两个方向各写一份，迟早会有一份先漂移")

    check("_busy" in _c,
          "⚠️ 代码改控件值要有抑制闸 —— "
          "📌 任何「代码改控件值」的地方都要问：**它会不会把自己的回调再触发一遍**")

    check("ui.notify" not in _c or "失败" in _c,
          "⭐ 正常路径不弹提示（即时生效本身就是反馈），只有写盘失败才说话。"
          "📌 **成功是常态，不必每次都说；失败必须说**")

    check("do_save" not in _SRC and "_save_btn" not in _SRC,
          "⭐⭐ 「保存 / 取消」已删 —— 2026-08-29 定的：**最终填写项留下了什么"
          "= 最终生效什么**，不靠显式保存按钮")

    # ── 🔴 实测 2026-08-10 那条教训**不随控件消失** ────────────────────
    # ⚠️ 断言只看**代码**，不看注释 —— 第一版没剥注释，被说明性注释本身打中了。
    #    📌 一条「代码里不许出现 X」的断言，必须先剥掉非代码部分（本项目第四次）。
    _code_only = _code_without_docstrings()
    check("update:model-value" not in _code_only,
          "⭐⭐⭐ **不许用 `.on('update:model-value', …)` 读控件值。**"
          "🔴 实测：高速拖动硬上限 → 滑块在最左（=1）"
          "**但标签显示 $24.0、保存按钮还可点**；随便点一下另一个滑块就「自己好了」。"
          "根因：`ui.slider` 的值同步带节流，而原始事件**不受节流** → "
          "回调跑在「值还没同步完」的那一刻。"
          "📌 **读一个控件的值，要用框架保证「已经同步过」的那个钩子，"
          "不要用底层事件。** "
          "⚠️ 控件已经换成步进器，但**这条判据没换** —— "
          "它约束的是「怎么读控件的值」，跟是什么控件无关")
    check(_c.count("on_value_change") >= 3,
          "⭐ 启用开关 + 软 + 硬，三个都走 `on_value_change`",
          f"{_c.count('on_value_change')} 处")


def t_scope_clean() -> None:
    print("\n[4] ⚠️ 回归：这几处改动没引入未定义名")
    # ⭐ 这一组不是形式主义 —— 修 ① 时把常量写成了类属性、
    #    却在方法里当裸名字读，**就是这个检查器一分钟内抓出来的**。
    #    📌 一个查错工具的价值，在它抓到你自己刚写的错时才真正兑现。
    sys.path.insert(0, str(ROOT / "tests"))
    from t_l23_missing_imports import undefined_names
    _hits = [h for h in undefined_names(ROOT / "app.py") if h[1] != h[0]]
    check(not _hits, "app.py 无未定义名", str(_hits[:3]) if _hits else "")


def t_no_raw_value_events() -> None:
    print("\n[5] 🔴 顺带扫出来的第二处：增强模式开关点了不生效")
    _co = _code_without_docstrings()
    check("update:model-value" not in _co,
          "⭐⭐⭐ 全库（app.py）**没有一处**再用原始 `update:model-value` 读值")
    _enh = _code("on_enhanced_change")
    check("e.value" in _enh,
          "回调仍然读 `e.value` —— 现在它来自 `ValueChangeEventArguments`，"
          "那个类**有**这个字段")
    check("enhanced_switch.on_value_change(on_enhanced_change)" in _co,
          "⭐⭐⭐ 改用 `on_value_change` —— "
          "🔴 原来是 `.on('update:model-value', lambda e: on_enhanced_change(e))`，"
          "而回调读 `e.value`：`GenericEventArguments` 的字段只有 "
          "`(sender, client, args)`，**没有 `.value`** → 每次点开关都 "
          "`AttributeError` 被吞掉 → **开关视觉上动了，但 `_enhanced_mode` 没变、"
          "没存盘、也没有那句提示**。"
          "📌 **两个长得一样的事件，字段不一样时，「用错哪一个」不会立刻暴露** —— "
          "错的那一半被 except 吞了，而 UI 上开关**照样会动**"
          "（那是前端自己的状态，跟后端收没收到无关）。"
          "📌 **一个「控件自己会动」的交互，不能靠「看起来生效了」验收。**")


if __name__ == "__main__":
    print("=" * 74)
    print("三个边缘 UI 问题")
    print("=" * 74)
    for _t in (t_auto_notice_scope, t_kb_filename_truncation, t_cap_guard,
               t_no_raw_value_events, t_scope_clean):
        _t()
    print("\n" + "=" * 74)
    print(f"结果: {_passed} passed, {len(_failed)} failed")
    print("=" * 74)
    if _failed:
        for _f in _failed:
            print("  !!", _f)
        sys.exit(1)
