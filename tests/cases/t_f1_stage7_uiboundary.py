# -*- coding: utf-8 -*-
"""的验收标准 —— UI 状态不许爬进 Kernel。

═══ 这个套件盯什么 ═══

早先的设计 1845-1848 行，零成本采纳）：

> 统一 Command 入口**只约束 Canonical Runtime State**，
> **不能扩张到 `_resp_state.current_text` / spinner 帧 / UI 展开折叠 /
> scroll position / 临时 Markdown 引用**，否则 Kernel 会变成 UI 状态垃圾桶。

⚠️ 那条当时只是**写进了验收标准**，没有任何东西在检查它。

约束的出处就是上面这段引文（测试不读 docs 等外部文件）。原文要点：纯展示状态（流式回复当前文本 `ViewSession.current_text`、spinner 帧、展开折叠、滚动位置、临时 Markdown 引用）不许进运行期内核；判据是「这个状态丢了会怎样」——只影响这一次看到的画面的是展示状态。
📌 **一条「写进验收标准」的边界，如果没有人检查，它就只是一句愿望** ——
   而这一条特别容易破：`_resp_state` 是个裸 dict，谁都能往命令 payload 里
   顺手塞一个 `content_md`，而且**塞进去不会报错**（handler 只 `p.get()`
   自己要的键，多的静默忽略 —— 那个坑 2026-08-08 已经踩过一次）。

═══ 为什么这条边界值得一条独立断言 ═══

Kernel 的全部价值是「权威状态可重放、可校验、跨重启自洽」。
而 UI 状态**天生不可重放**（DOM 随窗口消失）。把它放进去有两个后果：
  ① 权威库里出现一批**重启后必然失效**的字段，而它们看起来和别的字段一样可信
  ② 不变量再也没法「只看库就判断对不对」—— 它得知道当时那个窗口长什么样

📌 **一个「跨重启可信」的存储里，混进一个「重启就失效」的字段，
   代价不是多占一列，是【整张表的可信度都降级了】** ——
   读的人从此必须逐字段判断「这个能信吗」。

⏸ **本套件刻意【不】做 `_resp_state` → ViewSession 的结构重构**，理由两条：
  ① **（重启后 UI 重放对话）会改变段落是怎么被构造出来的** ——
     现在只能从实时流构造，之后要能从落盘历史重建；
     而「可从权威状态完整重建」正是 ViewSession 的真实形状（早先的设计 1850 行
     对 Projection 的要求逐字如此）。现在切一次、落地再切一次
     = **做两遍同一个迁移**（早先的设计用这条判据推迟过 execution slots
     与 waitcond 切读、切写两次）。
  ② 它是唯一**测试无法覆盖**的一层（DOM 不能在断言里驱动）。
     这一轮每个 UI 改动都要一次实测往返才能确认。

用法：
  py -3.10 tests\cases\t_f1_stage7_uiboundary.py
"""
from __future__ import annotations

import ast
import re
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ⚠️ 这份名单直接来自列举的五类，逐条落成真实的标识符。
#    新增 UI 状态时**应该往这里加**（加进来的成本是零，漏掉的成本是
#    某天 Kernel 里多了一个重启就失效的字段）。
_UI_ONLY_NAMES = (
    "content_md",        # 临时 Markdown 引用
    "spin_lbl",          # spinner 帧
    "svg_el",
    "loading_col",
    "loading_container",
    "status_lbl",
    "tool_pill_lbl",
    "tool_pill_arrow",
    "tool_details_col",
    "scroll_area",       # scroll position
    "inner_col",
    "text_checkpoint",
    "_resp_state",       # 那个 dict 自身
)

# ⚠️⚠️ **`current_text` 被从这份名单里【拿掉了】** —— 它太泛：
#    `interaction.verify_artifact(rec, current_revision, current_text)` 里那个
#    `current_text` 是 **Skill 产物的当前文本**（用来比指纹），与 UI 毫无关系。
# 📌 **一份「禁止出现的标识符」名单，里面每个名字都必须足够【特异】** ——
#    否则它会抓到同名但无关的东西，而那种误报会让人干脆把断言关掉，
#    于是真正的违规也一起被放过了。
# ⭐ 而真正要禁的 `_resp_state.current_text`，由名单里的 `_resp_state` 覆盖。

_RUNTIME_DIR = ROOT / "core" / "runtime"


def _code_only(path: pathlib.Path) -> str:
    """把一个文件**只剩代码**的样子取出来：注释 + docstring 全剥掉。

    ⚠️⚠️ **为什么必须连 docstring 一起剥**：`kernel.py` 与 `runtime/__init__.py`
       的模块头里**逐字引用了的原文**（「绝不扩张到 `_resp_state.current_text`
       / spinner 帧 / …」）—— 那正是这条边界本身。只滤 `#` 注释的话，
       **这条断言会把「把规则写清楚的那段文档」判成违规。**
    🔴 这已经是今天**第二次**撞到同一个形状（第一次是查「这一层不许有 DELETE」，
       被那一层自己解释「为什么不删行」的注释打中）。
    📌 **越是把规则写清楚的代码，越容易被自己的解释判成违规** ——
       所以「代码里不许出现 X」这类断言，剥离非代码部分必须做成**结构化**的（AST），
       不是再叠一层字符串过滤。**第二次撞上同一个形状时，就该修形状而不是修个案。**
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
    return ast.unparse(tree)   # ast.unparse 天然丢掉 # 注释


def t_kernel_layer_never_mentions_ui() -> None:
    print("\n[1] ⭐⭐⭐ `core/runtime/**` 一个 UI 标识符都不许出现")
    files = sorted(_RUNTIME_DIR.glob("*.py"))
    check(len(files) >= 8, "前置：扫到了内核层的文件", f"{len(files)} 个")

    bad: list[str] = []
    for f in files:
        code = _code_only(f)
        for name in _UI_ONLY_NAMES:
            if name in code:
                bad.append(f"{f.name}:{name}")
    check(not bad,
          "⭐⭐⭐ 内核层**没有任何一个** UI 标识符 —— "
          "📌 一个「跨重启可信」的存储里混进一个「重启就失效」的字段，"
          "代价不是多占一列，是**整张表的可信度都降级了**："
          "读的人从此必须逐字段判断「这个能信吗」",
          "; ".join(bad[:5]) if bad else "")


def t_no_ui_key_in_command_payload() -> None:
    print("\n[2] ⭐⭐ 任何 Command 的 payload 里不许出现 UI 键")
    # ⚠️ 为什么这条必须单独查、不能靠上一条覆盖：payload 是在 **app / orchestrator**
    #    那一侧构造的 —— 内核层干干净净，照样可能被外面塞进来。
    # 🔴 而且**塞进去不会报错**：handler 只 `p.get()` 自己要的键，多余的静默忽略
    #    （2026-08-08 那个 `terminal_reason` vs `reason` 就是这个机制咬人的）。
    bad: list[str] = []
    for rel in ("app", "core.orchestrator"):
        src = module_text(rel)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("Command", "_Cmd")):
                continue
            for kw in node.keywords:
                if kw.arg != "payload" or not isinstance(kw.value, ast.Dict):
                    continue
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        if k.value in _UI_ONLY_NAMES:
                            bad.append(f"{rel}:{k.value}")
    check(not bad,
          "⭐⭐ 所有 `Command(payload={...})` 的字面量键里**没有 UI 键** —— "
          "🔴 塞进去不会报错（handler 只取自己要的键，多余的静默忽略），"
          "所以这条只能靠断言守",
          "; ".join(bad[:5]) if bad else "")


def t_resp_state_stays_in_app() -> None:
    print("\n[3] ⭐ `_resp_state` 本身必须只活在 app 层")
    hits = []
    for f in sorted(_RUNTIME_DIR.glob("*.py")):
        if "_resp_state" in _code_only(f):
            hits.append(f.name)
    check(not hits,
          "⭐⭐ `_resp_state` 在内核层**一次都没出现** —— "
          "它是「本回应期」的 UI 共享状态，跨不过进程边界",
          "; ".join(hits))

    # ⏸ 而它**还没有**变成一个具名对象。这不是遗漏，是刻意推迟（见模块头）。
    src = module_text("app")
    check("_resp_state" in src,
          "⏸ 它目前仍是 app 里的一个 dict —— "
          "结构重构与 [F3]（重启后 UI 重放）一起做，"
          "因为会话落盘会改变「段落怎么被构造出来」，"
          "而那正是 ViewSession 的真实形状。"
          "📌 现在切一次、会话落盘之后再切一次 = 做两遍同一个迁移")


def t_no_dangling_method_calls() -> None:
    """⭐⭐⭐ 每一个被调用的私有方法都必须真的存在（2026-08-09 事故后加）。

    🔴 **这条是造成一次真实破坏之后补的。** 改 `_pill_settle_words` 时
       用 `"    def "` 找替换范围的右边界 —— 而它后面紧跟的是 **`    async def`**，
       前缀不匹配，于是那次查找**跳过并静默删掉了四个方法**：
       `_wake_now` / `_cancel_suspension` / `_drive_wake` / `_suspension_poll_tick`
       （整条定时/后台/手动唤醒主路径）。

    ⚠️⚠️ **而 `py_compile` 照样通过** —— 删掉几个方法不影响语法。
       测试也几乎没抓到：只有 `t_f1_stage6_inbox` 那条「两个持锁点都跟着排空」
       变成 `lock=1 drain=1` 红了一格，因为消失的 `_drive_wake` 里有一个持锁点。
    📌 **一次删除的可见后果，可能只有一条与它看起来无关的断言。**

    ⚠️ 而第一次自查用的是「调用了但没定义的 `self.X()`」——
       **那漏掉了 `_suspension_poll_tick`**，因为它是用 `gui._suspension_poll_tick()`
       调的，接收者不是 `self`。最后是**与备份做全集对比**才查干净。
    📌 **验证一次破坏性改动，要比对「改动前后的全集」，
       不是去猜「哪些地方会用到它」。**

    ⭐ 本条断言就是那个自查的常驻版：它同时看 `self.` 和 `gui.` 两种接收者。
    """
    print("\n[5] ⭐⭐⭐ 没有悬空的私有方法调用（事故后加的守护）")
    src = module_text("app")
    tree = ast.parse(src)

    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check(len(defined) > 200, "前置：解析出了 app 的方法表", f"{len(defined)} 个")

    dangling = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        recv = n.func.value
        # ⚠️ 两种接收者都看 —— 只看 `self` 会漏掉 `gui._suspension_poll_tick()`
        #    那一格（那正是这次真的漏掉的那一个）。
        if not (isinstance(recv, ast.Name) and recv.id in ("self", "gui")):
            continue
        attr = n.func.attr
        if not attr.startswith("_") or attr.startswith("__"):
            continue
        if attr not in defined:
            dangling.append(f"{recv.id}.{attr}")
    check(not dangling,
          "⭐⭐⭐ 所有 `self._xxx()` / `gui._xxx()` 调用都有对应定义 —— "
          "🔴 这一格红了说明有方法被删掉了而语法检查抓不到"
          "（2026-08-09 真的发生过一次，删掉了整条唤醒主路径）",
          "; ".join(sorted(set(dangling))[:6]) if dangling else "")

    # ⭐ 那条唤醒主路径上的四个方法单独钉一次 —— 它们是这次事故的受害者，
    #   而且其中三个**只被 lambda / 定时器引用**，最容易被静默删掉还没人发现。
    for name in ("_wake_now", "_cancel_suspension", "_drive_wake",
                 "_suspension_poll_tick"):
        check(f"def {name}" in src, f"⭐ `{name}` 在（唤醒主路径）")


def t_l7_viewsession_declares_the_whole_field_set() -> None:
    """`_resp_state` 裸 dict → `ViewSession`：**字段集必须有唯一答案。**

    ═══ 落地前的三个问题 ═══

    🔴 一：**两个构造点，两份逐字相同的 20 键字面量**
       （`start_pipeline_task` 与 `_drive_wake_inner`，相隔六千行）。
       📌 同一件事有两个实现，它们只在「我两次想法相同」的前提下一致。
    🔴 二：代码实际读写 **28** 个键，字面量里只有 20 个 —— 多出来的 8 个
       只靠「某处赋值过」存在。于是**「一个回应期到底有哪些字段」
       这个问题，全项目没有任何地方能回答**。
    🔴 三（最要命）：裸 dict 上写错键名不报错、读错键名也不报错，
       `.get()` 安静返回 `None` → 那段 UI 逻辑变成「有时才执行」。
       📌 本项目反复栽的就是这个形状：**漏掉的那种不会报错。**

    ═══ 为什么严格性放在这里，而不放在运行时 ═══

    ⚠️ `ViewSession.__setitem__` 遇到未声明的键**只 warning，照旧存下来**。
       📌 一个会在用户面前崩溃的守卫，迟早会被人加上 try/except 绕过 ——
          绕过之后它连 warning 都不剩了。
    ⭐ 所以**真正的闸在这条断言里**：声明的字段集必须覆盖代码里出现过的
       每一个键。运行时宽容，测试严格。

    ⚠️ 这一层是全项目**唯一测试覆盖不到的**（DOM 不能在断言里驱动），
       所以迁移刻意做成 **drop-in**（下标/`get`/`setdefault` 全代理到属性），
       28 个键 × 每一处调用点一个字没改。
       📌 一次「行为零变化」的迁移，才有资格在测不到的地方做。
    """
    print("\n[L7] ViewSession 的字段集是唯一答案")
    src = module_text("app")

    check("class ViewSession:" in src, "前置：`ViewSession` 在")
    check("self._resp_state = {" not in src,
          "🔴 **两处裸 dict 字面量都不许回来**（负向断言）—— "
          "📌 加字段要加进 `_FIELDS`，加在别处等于又回到裸 dict")

    _i = src.index("_FIELDS: dict = {")
    _j = src.index("}", src.index("waiting_for_carrier", _i))
    declared = set(re.findall(r'"([a-z_]+)"\s*:', src[_i:_j]))
    check(len(declared) >= 28, f"声明字段数 {len(declared)}", str(len(declared)))

    # 代码里真正被读写的键（resp_state 的全部别名）
    alias = (r"(?:self\._resp_state|_rs|_live_rs|_owned_rs|_self\._resp_state"
             r"|predecessor|successor)")
    used = (set(re.findall(alias + r'\["([a-z_]+)"\]', src))
            | set(re.findall(alias + r'\.get\("([a-z_]+)"', src))
            | set(re.findall(alias + r'\.setdefault\("([a-z_]+)"', src)))
    missing = sorted(used - declared)
    check(not missing,
          "⭐⭐⭐ **声明集覆盖代码里出现过的每一个键** —— "
          "📌 少一个的表现不是报错，是那段 UI 逻辑安静地变成「有时才执行」",
          ("未声明: " + ", ".join(missing)) if missing else f"{len(used)} 个全覆盖")

    # ── drop-in 的几个致命细节 ────────────────────────────────────
    _k = src.index("class ViewSession:")
    cls = src[_k:src.index(chr(10) + chr(10) + "# ", _k)]
    check("def __bool__" in cls,
          "⭐⭐ **必须实现 `__bool__` 返回 True** —— 全项目大量 "
          "`(self._resp_state or {})`；📌 兼容层最危险的不是缺方法，"
          "是**某个魔术方法的默认行为恰好不同**（空 dict 为假、对象为真）")
    check("__slots__" in cls,
          "⭐ `__slots__` —— 拼错的属性名**写不进去**（这是运行时那一半的防线）")
    check('kw.setdefault("action_refs", {})' in cls
          or "kw.setdefault('action_refs', {})" in cls,
          "⚠️ `action_refs` 每个实例拿自己那份 —— "
          "📌 Python 可变默认值的经典坑：共享之后两段回应期会互相写对方的工具卡")
    for _m in ("__getitem__", "__setitem__", "get", "setdefault", "__contains__"):
        check(f"def {_m}" in cls, f"drop-in 面：`{_m}`")

    # ── 🔴 实测回归逼出来的那条禁令（2026-08-23）──────────────────────
    #
    #  里曾有一处 `isinstance(_rs, dict)`：换成 ViewSession 之后
    # 它当场变 False → 截图被画进整个聊天区（**图巨大 + 位置错**），
    # **而且没有任何异常或日志** —— 静默降级。
    # 📌 **一个 drop-in 兼容层，光有方法不够，它还得能通过类型检查** ——
    #    而 `isinstance(x, dict)` 是唯一改不了的那个（dict 是具体类型，
    #    连虚拟子类注册都救不了）。
    # ⭐ 所以这条断言就是那道防线：**挡不住的东西，就让测试来禁止它。**
    # ⚠️ **必须剥掉注释再核** —— 第一版就当场被自己写的说明打中了。
    #    📌 **按「字符串出现过」核，不算核**（本项目栽过 6 次的那条）。
    _live = chr(10).join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
    _bad = [l.strip() for l in _live.splitlines()
            if re.search(r'isinstance\(\s*(_rs|_live_rs|_owned_rs|predecessor|successor'
                         r'|self\._resp_state|_self\._resp_state)\s*,\s*dict\s*\)', l)]
    check(not _bad,
          "🔴 **不许用 `isinstance(…, dict)` 判断回应期**（负向断言）—— "
          "它在 ViewSession 上恒为 False，而失败方向是**静默降级**："
          "不报错、不记日志，只是画到了别的地方。"
          "📌 该问的是「它有没有我要的那个东西」，不是「它是不是 dict」",
          str(_bad[:2]))


def main() -> int:
    t_kernel_layer_never_mentions_ui()
    t_no_ui_key_in_command_payload()
    t_resp_state_stays_in_app()

    t_no_dangling_method_calls()
    t_l7_viewsession_declares_the_whole_field_set()

    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过" +
          ("" if ok == len(_results) else " —— 失败项："))
    for good, name, note in _results:
        if not good:
            print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
