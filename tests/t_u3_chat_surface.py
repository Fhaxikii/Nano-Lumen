# -*- coding: utf-8 -*-
"""聊天区拿得到的东西 —— 选中/引用、拖拽上传、文件链接、代码框。

═══ 这一套真正要守的东西 ═══

这一组的主线是「窗口里的文字拿不出来」。根因那一半（native 不能选中）已在
v1.59 修掉 —— `pywebview` 的 `text_select` 默认 False，页面加载后注入
`body{user-select:none}`。**这一套守的是建在它上面的四件事**：

  ① **选中文字 → 右键 replay**
     已定：「机制和 UI 反馈跟待审 skill 相同即可，只是入口不同」。
     ⚠️ 所以它必须**复用** `_reply_target` 那一整套，而不是另起一份状态；
        但注入给模型的话**必须分开**（一个是「去答这条待办」，一个是
        「我在指这段话」）—— 📌 一个字段不许表达两个现实。
  ② **拖文件进窗口 → 走 composer 那条上传通道**
     ⚠️ 必须喂给**同一个** `_handle_chat_upload`：图片回看（步 2 的自存）、
        临时文件注册、附件角标全挂在它上面。
        📌 要的是走同一条路，不是再实现一遍。
  ③ **`nano-file:` 链接**：左键打开 / 右键在文件夹中显示
     🔴 左键走 `os.startfile` = **系统默认关联**，而「默认关联」对 `.pdf` 是
        「打开」、对 `.bat` / `.py` 是**执行**。而这些链接是**模型生成的**。
        → 白名单 + 降级成「在文件夹中显示」，不是拒绝。
  ④ **代码块的复制按钮**

用法：
  py -3.10 tests\\t_u3_chat_surface.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


# ⚠️ 2026-08-29：色值收进了 CSS 变量（--nano-amber），断言改成认变量。
#    📌 这条守的是「**这里有琥珀色高亮**」，不是「色值必须写成六位十六进制」——
#       一条断言如果连**表达方式**都锁死，那它拦的是重构，不是回归。
def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP = module_text("app")
ORCH = module_text("core.orchestrator")


def _code_only(src: str) -> str:
    """只留**会被执行的代码**，剥掉注释与字符串字面量。

    🔴 本套件在这上面栽了**四次**：断言直接在源码文本里搜关键词，
       命中的却是**修复处那段解释性注释**（注释里当然要提旧写法）。
       最近一次：`_reveal_in_explorer` 的 docstring 里逐字写着旧写法
       `Popen(["explorer", ...])` 和 `shell=True`，于是「确认已经不用了」
       这两条断言永远红。
    📌 **按「字符串出现过」核，不算核。**
    ⚠️ 而它的危险在于表现形式：**假红看起来和「功能没做」一模一样** ——
       顺手放宽断言的话，真正的缺口会跟着一起放掉。
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        return src
    return " ".join(out)


def esc(txt: str) -> str:
    """把中文拼成源码里那串 unicode 转义（JS 塞在 Python 源码里用的就是这个形式）。

    📌 不手写反斜杠：手写转义的断言，错一个字符就变成「永远搜不到」，
       而它看起来像**功能没做**。本套件为此假红过三条。
    """
    return "".join(chr(92) + "u%04x" % ord(c) for c in txt)


def has_zh(src: str, txt: str) -> bool:
    """源码里的中文**两种形式都算**：unicode 转义 或 真字符。

    ⚠️ 同一个文件里两种都存在（取决于当时是怎么写进去的），而它们
       **在浏览器里完全等价**。📌 断言该守的是「这个菜单项在不在」，
       不是「它是用哪种写法写进去的」—— 后者一改就假红。
    """
    return esc(txt) in src or txt in src


def _fn(src: str, name: str):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


class _FakeAgent:
    _reply_target = None


class _FakeUI:
    """只借 WebUI 的方法，不建 NiceGUI 页面。"""

    def __init__(self):
        from app import WebUI
        self._cls = WebUI
        self.agent = _FakeAgent()
        self._pinned_snapshot = ""
        self.QUOTE_INTERACTION = WebUI.QUOTE_INTERACTION
        self.QUOTE_SELECTION = WebUI.QUOTE_SELECTION
        self._OPENABLE_EXTS = WebUI._OPENABLE_EXTS

    @property
    def _reply_target(self):
        return self.agent._reply_target

    def _redraw_pinned_now(self):
        pass

    def _refresh_reply_prompt(self):
        pass

    def set_target(self, iid, q="", kind=None):
        from app import WebUI
        WebUI._set_reply_target(self, iid, q, kind or self.QUOTE_INTERACTION)

    def resolve(self, raw):
        from app import WebUI
        return WebUI._resolve_local_path(self, raw)


# ══════════════════════════════════════════════════════════════════════════

def t_quote_state_is_shared_not_forked() -> None:
    print("\n[1] ⭐⭐ 选中引用复用待审卡那套状态 —— 但两种引用显式分开")
    u = _FakeUI()

    u.set_target("int_abc", "部署这个吧")
    rt = u._reply_target
    check(rt and rt["iid"] == "int_abc" and rt["kind"] == u.QUOTE_INTERACTION,
          "引用待审卡：iid + kind=interaction")

    u.set_target(None, "这个能力坏了", kind=u.QUOTE_SELECTION)
    rt = u._reply_target
    check(bool(rt), "⭐ 选中文字也能建立引用态（iid 为空也算数）")
    check(rt["iid"] == "" and rt["kind"] == u.QUOTE_SELECTION,
          "⭐⭐ kind 是**显式字段**，不靠 iid 空不空去猜", str(rt))

    # ⚠️ 没有文字的选中引用 = 没有引用（否则会出现一个空的引用横幅）
    u.set_target(None, "   ", kind=u.QUOTE_SELECTION)
    check(u._reply_target is None, "空选区不建立引用态")

    u.set_target(None)
    check(u._reply_target is None, "清除仍然有效")

    # 截断：引用是指路标，不是重新贴一遍
    u.set_target(None, "x" * 500, kind=u.QUOTE_SELECTION)
    check(len(u._reply_target["q"]) <= 200, "长选区被截断", str(len(u._reply_target["q"])))


def t_two_injections_stay_separate() -> None:
    print("\n[2] ⭐⭐⭐ 注入给模型的两句话必须分开")
    from core.orchestrator import Orchestrator

    class F(Orchestrator):
        def __init__(self, rt):
            self._reply_target = rt

    out = Orchestrator._build_quoted_selection_injection(
        F({"iid": "", "q": "这个能力坏了", "kind": "selection"}))
    check("[Quoted by the user]" in out, "选中引用有自己的注入块")
    check("这个能力坏了" in out, "⭐ 原文**逐字**给模型（不做摘要）")
    check("this, that, it, here" in out,
          "⭐⭐ 明说要拿引用去解「这个/那个」这类指代")
    check(out.isascii() is False and "The user selected" in out,
          "指令是英文（原则 8.5），只有引文保持原文")

    # ⚠️ 引用待审卡时**不能**触发这一段 —— 那是另一件事
    out2 = Orchestrator._build_quoted_selection_injection(
        F({"iid": "int_1", "q": "部署这个吧", "kind": "interaction"}))
    check(out2 == "", "⭐⭐ 引用待审卡不走这一段（两件事不共用一个出口）")
    check(Orchestrator._build_quoted_selection_injection(F(None)) == "",
          "没有引用时一个字符都不注入")

    # 📌 它必须独立于「未决交互」那段被调用：没有待办时那段直接返回空串，
    #    合在一起的话「没有待办」会顺手把「用户指了一句话」也吃掉。
    i_q = ORCH.find("_quote_injection = self._build_quoted_selection_injection()")
    i_i = ORCH.find("_interaction_injection = self._build_open_interactions_injection()")
    check(i_q > 0 and i_i > 0, "两段都在 system_guide 的组装处被调用")
    seg = ORCH[i_i:i_q]
    check("if _interaction_injection:" in seg and seg.count("return") == 0,
          "⭐ 选中引用不在未决交互那段的 if 里面（不会被它的空串短路掉）")


def t_drop_reuses_the_upload_handler() -> None:
    print("\n[3] ⭐⭐ 拖拽走的是**同一个** upload handler，不是另起一条")
    check(".nano-chat-upload" in APP, "composer 的隐藏 upload 有可寻址的类")
    check(".nano-chat-upload input[type=file]" in APP,
          "⭐⭐ 脚本把文件塞进那个 input，再 dispatch change")
    check("new DataTransfer()" in APP and "new Event('change'" in APP,
          "⭐ 用 DataTransfer 构造 FileList（这是唯一能写 input.files 的方式）")
    # 📌 要的是走同一条路：另发一份到后端就会漏掉图片回看/临时文件注册/角标
    check("_handle_chat_upload" in APP, "原 handler 还在")
    check("emit('nano_upload'" not in APP,
          "⭐⭐ 没有第二条上传通道（另起一条必然漏掉挂在原 handler 上的东西）")

    # 知识库那块保持原行为
    check("closest('.kb-upload')" in APP,
          "⭐ 知识库添加文档区不被劫持（明确划过的边界）")
    # 遮罩不能吃掉 drop 事件
    seg = APP[APP.find("#nano-drop-overlay {{"):][:600]
    check("pointer-events: none" in seg,
          "⭐⭐ 拖拽遮罩 `pointer-events:none` —— 否则它自己会把 drop 吃掉")
    # 四个字就够。📌 遮罩是状态提示不是说明书。
    check("".join(chr(92) + "u%04x" % ord(c) for c in "添加附件") in APP,
          "⭐ 遮罩文案是「添加附件」")


def t_file_link_is_not_an_exec_hole() -> None:
    print("\n[4] 🔴🔴 文件链接不能变成执行入口")
    from app import WebUI
    exts = WebUI._OPENABLE_EXTS
    for bad in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".scr", ".reg",
                ".lnk", ".py", ".sh", ".jar", ".com"):
        if bad in exts:
            check(False, f"⚠️ 可执行/脚本类型 {bad} 混进了白名单")
            break
    else:
        check(True, "⭐⭐⭐ 白名单里没有任何可执行/脚本类型")
    for good in (".txt", ".md", ".pdf", ".png", ".xlsx", ".json"):
        check(good in exts, f"文档类 {good} 可直接打开")

    # ⚠️ 是白名单不是黑名单 —— 📌 排除法欠账随时间增长，白名单不会
    src = ast.get_source_segment(APP, _fn(APP, "_on_open_local_file")) or ""
    check("not in self._OPENABLE_EXTS" in src,
          "⭐⭐ 判据是「不在白名单里」，不是「在黑名单里」")
    check("_reveal_in_explorer" in src,
          "⭐ 不在白名单的**降级**成在文件夹中显示，不是拒绝（用户仍然到得了）")

    # 路径注入：explorer 必须传参数组，不能拼命令行
    rv = ast.get_source_segment(APP, _fn(APP, "_reveal_in_explorer")) or ""
    # ⚠️ 这个函数的 docstring 里**逐字写着旧写法**（那是留痕，不是问题本身）——
    #    所以判据必须走剥过注释与字符串的版本。见 `_code_only` 的注释。
    # 🔴🔴 实测：「点这个按钮永远跳转到我的文档」。
    #    问题出在**看起来最规范的那个写法**：`Popen(["explorer", f"/select,{p}"])`。
    #    Windows 上 subprocess 会把含空格的参数自动加引号 →
    #        explorer "/select,C:\...\Nano-Lumen V1.11\app.py"
    #    而 explorer.exe **不接受被整体引起来的 /select,**，解析失败后
    #    不报错、直接开默认位置。⚠️ 症状是「跳错地方」不是「报错」。
    # 📌 「参数数组更安全」的前提是被调方按标准解析命令行 —— explorer 不。
    #
    # ⚠️⚠️ 这三条**必须走 AST 判实参类型**，两种文本判法都不行：
    #    · 直接搜源码 → 命中的是 docstring 里逐字写着的旧写法（假红，本套件第四次）
    #    · 搜剥过注释的版本 → 字符串字面量也被剥掉了，两种写法长得一模一样（假绿）
    #    📌 **一个既会假红又会假绿的判据，说明判错了层** —— 要判的是
    #       「传给 Popen 的第一个实参是字符串还是列表」，那是结构，不是文本。
    _rv_node = _fn(APP, "_reveal_in_explorer")
    _popens = [n for n in ast.walk(_rv_node)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "Popen"]
    check(len(_popens) == 2, "两条路径（目录/文件）各一次 Popen", str(len(_popens)))
    check(all(isinstance(c.args[0], ast.JoinedStr) for c in _popens if c.args),
          "⭐⭐⭐ 传的是命令行**字符串**，不是参数数组"
          "（数组会把 /select, 整个引起来 → 跳到我的文档）")
    check(not any(isinstance(c.args[0], (ast.List, ast.Tuple)) for c in _popens if c.args),
          "⭐⭐ 确认没有任何一处还在传数组")
    check(not any(k.arg == "shell" for c in _popens for k in c.keywords),
          "⭐ 仍然不用 shell=True（路径里的引号就是注入点）")

    # 引号位置：只包路径，不包 /select,
    _cmds = [v.value for c in _popens for v in c.args[0].values
             if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    check(any('/select,"' in v for v in _cmds),
          "⭐⭐ 引号只包路径，不包 /select,", "explorer 要的就是这个形状")
    # exe 走绝对路径，不靠 PATH（PATH 上能放一个同名的 explorer.exe）
    _envs = [n for n in ast.walk(_rv_node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "get"
             and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "environ"]
    check(bool(_envs), "⭐ explorer.exe 走 %SystemRoot% 绝对路径，不靠 PATH")
    check("SystemRoot" in rv and "explorer.exe" in rv, "拼的就是 SystemRoot + explorer.exe")

    # 复现一次：证明旧写法确实会把 /select, 一起引起来
    import subprocess as _sp
    _old = _sp.list2cmdline(["explorer", "/select,C:" + chr(92) + "a b" + chr(92) + "x.py"])
    check(_old.split(" ", 1)[1].startswith('"/select,'),
          "⭐ 复现：旧写法确实把 /select, 一起引起来了", _old[-26:])

    # 真的按 Windows 的规则拼一次，确认两种写法的差别就是那对引号
    import subprocess as _sp
    _old = _sp.list2cmdline(["explorer", "/select,C:\\a b\\x.py"])
    check(_old.split(" ", 1)[1].startswith('"/select,'),
          "⭐ 复现：旧写法确实把 /select, 一起引起来了", _old[-28:])

    # 解析：相对路径按项目根，不按进程 cwd
    rp = ast.get_source_segment(APP, _fn(APP, "_resolve_local_path")) or ""
    check("__file__" in rp,
          "⭐⭐ 相对路径按**项目根**解析（cwd 会被别的代码改，同一个链接会指向不同文件）")
    check("pth.exists()" in rp, "不存在就返回 None（不假装打开了）")

    u = _FakeUI()
    check(u.resolve("config/system_instruction.txt") is not None, "相对路径解析得到")
    check(u.resolve("no/such/file.txt") is None, "不存在的路径 → None")
    check(u.resolve("") is None, "空路径 → None")


def t_file_link_is_inline_not_a_button() -> None:
    """🔴 「它要放在一个段落里面的啊，你怎么做成按钮了啊」。

    📌 **行内元素的样式预算比块级小得多** —— 一个句子中间的东西只能改颜色/字重；
       一旦加上边框和内边距，它就把整行的行高和节奏撑歪了，
       而那一行的主角是句子，不是它。
    """
    print("\n[5] ⭐⭐ 文件链接是段落里的一段文字，不是按钮")
    # ⚠️ 只截**那两条规则**，不要按固定字符数切 ——
    #    📌 一个「大概够长」的切片，会把隔壁规则的属性也算进来
    #       （下面几条里有 background / padding），断言就永远为假。
    i = APP.find("a.nano-file-link {{")
    j = APP.find("a.nano-file-link:hover {{", i)
    j = APP.find("}}", j) + 2
    css = APP[i:j]
    check("border: 1px solid" not in css, "⭐⭐ 没有外边框")
    check("background:" not in css, "⭐⭐ 没有底色")
    check("padding:" not in css, "⭐ 没有内边距（内边距是按钮的形状）")
    check("display: inline-flex" not in css, "⭐ 不是 inline-flex（那会脱离文字基线）")
    check("var(--nano-amber)" in css or "#e6a94e" in css, "保留黄色高亮")
    check("nano-file-ico" not in APP, "⭐⭐ 那个 ▣ 图标已经整个删掉了")
    # 仍然可点、仍然能认出来
    check("cursor: pointer" in css, "鼠标形状仍然是手")


def t_markdown_convention_survives() -> None:
    print("\n[6] `nano-file:` 这个约定在 markdown 里活得下来")
    import markdown2
    md = markdown2.Markdown(extras=["fenced-code-blocks", "tables"])
    for path in ("config/mcp_servers.json", "C:/Users/x/notes.md", r"C:\Users\x\notes.md"):
        html = md.convert(f"见 [notes]({'nano-file:' + path})")
        check('href="nano-file:' in html, f"自定义 scheme 没被吃掉：{path[:18]}", "")
    # 围栏代码块 → <pre>，脚本据此挂复制按钮
    html = md.convert("```json\n{\"a\": 1}\n```")
    check("<pre>" in html, "围栏代码块渲染成 <pre>（复制按钮挂在它上面）")


def t_browser_side_is_delegated() -> None:
    print("\n[7] 浏览器侧：事件委托 + 观察者（内容是流式插进来的）")
    check("MutationObserver" in APP,
          "⭐⭐ 用 MutationObserver —— 📌 一次性绑事件只覆盖「绑的那一刻已存在」的节点")
    check("window.__nanoU3T" in APP and "setTimeout(sweep" in APP,
          "⭐ 扫描去抖（流式期间别每个 token 扫一遍）")
    for ev in ("nano_quote_selection", "nano_open_file", "nano_reveal_file"):
        check(f"ui.on('{ev}'" in APP, f"`{ev}` 注册成**全局** ui.on", "")
        check(f"emit('{ev}'" in APP or f"'{ev}'" in APP, f"`{ev}` 在脚本侧被 emit")
    # 📌 本项目栽过：`cm_change` 注册成元素级 → handler 一次都没被调用过
    check("ui.on('nano_quote_selection', self._on_quote_selection)" in APP,
          "⭐⭐ 是全局 ui.on 不是元素级（本项目栽过一次同形状的）")
    check("pre:not([data-nano-copy])" in APP, "复制按钮只挂一次（幂等）")
    check("closest('.nano-cm-host')" in APP,
          "⭐ CodeMirror 自己那些 pre 不动（审计弹窗有自己的一套）")
    check("legacyCopy" in APP,
          "⭐ 剪贴板有 execCommand 兜底（WebView2 下 clipboard API 不一定可用）")


def t_prompt_has_bounds() -> None:
    print("\n[8] ⭐ 提示词给的是能力**和边界**（不然会被滥用）")
    ins = (ROOT / "config" / "system_instruction.txt").read_text(encoding="utf-8")
    check("nano-file:" in ins, "教了它文件链接怎么写")
    check("Use it sparingly" in ins, "⭐⭐ 明说要克制")
    check("do NOT turn a sentence into a row of links" in ins.replace("Do NOT", "do NOT"),
          "⭐⭐ 直接禁掉「一句话十个链接」那种用法（点名过的滥用形态）")
    check("fenced code block" in ins, "教了它代码框怎么出")
    check("Do not fence a single identifier" in ins,
          "⭐ 也划了代码框的下界（行内标识符不该成框）")
    # 📌 钉的是**位置**（Safety 段必须最后），不是某句话的字面结尾 ——
    #    旧写法 `endswith("core identity rules.")` 是拿字面当位置的代理，
    #    往 Safety 段里补一行（哪怕补的正是安全规则）就会误报。v1.96 就这么红过。
    _heads = [ln.strip() for ln in ins.splitlines() if ln.startswith("# ")]
    check(bool(_heads) and _heads[-1] == "# Safety",
          "⚠️ Safety 仍然是最后一段（它自称覆盖一切）")


def t_copy_in_context_menu() -> None:
    """实测①：右键菜单里没有「复制」。"""
    print("\n[9] ⭐⭐ 右键菜单里的「复制」")
    # ⭐ 一份实现两处用 —— 📌 两份复制逻辑迟早会一份有兜底一份没有
    check(APP.count("function copyText(") == 1, "复制只有一份实现")
    check(APP.count("function legacyCopy(") == 1, "兜底也只有一份")
    check("copyText(text, done);" in APP, "⭐ 代码框按钮改调公用那份")
    # ⚠️ 脚本里的中文是 \uXXXX 转义（JS 塞在 Python 源码里，直接写中文会踩编码）。
    #    这里**按码位拼出那串转义**再搜，不手写反斜杠 ——
    #    📌 手写转义的断言，错一个字符就变成「永远搜不到」，
    #       而它看起来像**功能没做**。本轮就是这么假红了三条。
    def _esc(txt):
        return "".join(chr(92) + "u%04x" % ord(c) for c in txt)

    # 🔴 2026-08-22：这里原来是 `[:1600]` —— 一个**拍出来的长度**。
    #    白名单那一批注释加进去之后，「复制」被挤出了这个窗口 → **假红**，
    #    而它看起来完全像「功能被删了」。
    # 📌 **一个用固定长度圈出来的范围，会在范围内的内容变长时静默失效** ——
    #    而它失效的方式是「报告一件没发生的事」，比不检查更坏。
    # ⭐ 改成锚到这段 handler 的**真实边界**：从 `const inChat` 到
    #    `openMenu(e.clientX, e.clientY, items);` 那一句结束。
    _i0 = APP.find("const inChat = e.target.closest")
    _i1 = APP.find("openMenu(e.clientX, e.clientY, items);", _i0)
    check(_i0 > 0 and _i1 > _i0, "能锚到选中菜单那段（边界还在）")
    seg = APP[_i0:_i1]
    # ⚠️ 走 `has_zh`（本文件第 87 行就有）—— 2026-08-22 这一块被重写时
    #    中文从 `\uXXXX` 变成了真字符，而**浏览器里完全等价**。
    #    📌 这正是 `has_zh` 存在的理由；上一版这三条断言恰好绕过了它，
    #       于是「写法变了」被报告成「功能没了」。
    check(has_zh(seg, "复制"), "⭐⭐ 选中文字的右键菜单里有「复制」")
    # 📌 菜单顺序按「用户多半想干什么」排，不按「我们新做了什么」排
    # 🔴🔴 **第七次栽在同一个形状上**：`seg.find("回复")` 命中的是
    #    这段上面的**解释性注释**（「「回复」是白名单…」），
    #    于是 replay@249 排在 copy@629 前面 —— **假红**。
    # 📌 **判「菜单项的顺序」要判菜单项，不能判「这两个字出现在哪」** ——
    #    自由文本搜索会把注释、文档、变量名一起算进去，
    #    而这三样恰恰是最容易变的。
    # ⭐ 改成只看 `label: \'…\'` 这个**结构**出现的顺序。
    import re as _re

    _labels = [(m.start(), m.group(1))
               for m in _re.finditer(r"label:\s*\'([^\']*)\'", seg)]

    def _pos(txt):
        """这个**菜单项**在 seg 里的位置（两种写法都算）。找不到返回 -1。"""
        for _i, _v in _labels:
            if _v == txt or _v == esc(txt):
                return _i
        return -1

    i_copy = _pos("复制")
    # ⚠️ 文案 2026-08-21 改成中文「回复」；断言跟着**语义**走。
    i_replay = _pos("回复")
    check(0 < i_copy < i_replay, "⭐ 复制排在 replay 前面", f"copy@{i_copy} replay@{i_replay}")
    # 「你怎么用的『复制』那俩字啊，改成右键复制用的那个图标」
    # ⚠️ 用**内联 SVG** 而不是 Unicode 字符：📌 字形宽度和基线由字体决定，
    #    换个字体回退就会歪，而这个按钮只有一个图标，歪一点就很明显。
    check("const ICON_COPY" in APP and "<rect x=" in APP,
          "⭐⭐ 复制图标是内联 SVG（两个叠起来的圆角方块）")
    check("u29C9" not in APP, "⭐ 那个 Unicode 字符 ⧉ 已经不用了")

    # ── ⭐⭐ [2026-08-22] 「回复」是白名单 ────────────────────────
    # 「如果这句话是模型自己说的 = 可以回复」。
    # 🔴 此前条件是「在聊天区里」→ 系统报错 / 工具卡片 / 用户自己的话都能回复。
    # 📌 白名单**不是**排除法：排除法的欠账随时间增长，而漏掉的那种不会报错。
    check("saidByNano" in seg and ".nano-said" in seg,
          "⭐⭐ 回复项挂在 `.nano-said` 白名单上")
    check("if (saidByNano)" in seg,
          "⭐ 「回复」是**条件加入**菜单，不是加进去再禁用 —— "
          "📌 一个灰着的按钮仍然在说「这里本来能干这件事」")
    # ⚠️ 复制**不受**这条限制（两个动作适用范围本来就不同）
    _i_copy_push = _pos("复制")
    _i_gate = seg.find("if (saidByNano)")
    check(0 < _i_copy_push < _i_gate,
          "⭐ 复制在白名单闸**之前**就进了菜单（对任何文字都有意义）",
          f"copy@{_i_copy_push} gate@{_i_gate}")
    # ⚠️ 判的是**整段选中**都在里面，不是起点在里面
    check("commonAncestorContainer" in seg,
          "⭐⭐ 用 commonAncestorContainer —— "
          "📌 跨界的选中「回复的是哪一条」答不上来，答不上来就不给")

    # ── 标记本身：一个函数，不是 9 处各贴一次 ──────────────────────
    check("def nano_md(" in APP, "⭐ 模型正文收成一个 `nano_md()`")
    check(APP.count("nano_md(") >= 9,
          "⭐⭐ 9 处模型正文全部走它", f"实际 {APP.count('nano_md(')} 处")
    # 🔴 反向：系统报错那条**不许**走 nano_md（它不是模型说的）
    #
    # ⚠️ 第一版是 `APP[_err_i:_err_i + 900]` —— 靠**字符距离**框范围。
    #    2026-08-22 把卡片抽成 `render_sys_error_card()` 之后，那个窗口越过
    #    函数边界撞上了紧随其后的 `def nano_md(` → **假阳性**（性质并没有破）。
    # 📌 **一条靠「字符距离」定位的断言，会在代码挪位置时变成假阳性** ——
    #    同 `t_f1_stage6_inbox` 里那条抓 `"container": loading_container`
    #    文本形状的断言。⭐ 改成按 **AST 取函数体**：它问的才是那个性质本身。
    _card_fn = next(
        (n for n in ast.walk(ast.parse(APP))
         if isinstance(n, ast.FunctionDef) and n.name == "render_sys_error_card"),
        None)
    check(_card_fn is not None, "前置：错误卡片收成了一个函数（live 与重放共用）")
    _card_src = ("\n".join(APP.splitlines()[_card_fn.lineno - 1:_card_fn.end_lineno])
                 if _card_fn else "")
    check(bool(_card_src) and "nano_md(" not in _card_src
          and "ui.markdown(" in _card_src,
          "🔴 System Error 卡片**不**带可回复标记（它不是 Nano 说的）—— "
          "用裸 `ui.markdown`，不走 `nano_md()`")
    check("{icon: ICON_COPY" in APP, "右键菜单用同一个图标")
    check("btn.innerHTML = ICON_COPY" in APP, "⭐⭐ 代码框按钮是图标，不是「复制」两个字")
    check("btn.innerHTML = ICON_OK" in APP, "⭐ 成功时换成对勾，不是「已复制」三个字")
    check("btn.title = " in APP, "⚠️ 图标按钮必须有 title（否则不认识的人只能猜）")
    check(_esc("复制路径") in APP, "文件链接右键有「复制路径」")
    check("nano-mini-toast" in APP,
          "⭐ 复制反馈走浏览器侧小 toast（Python 侧 notify 会慢半拍）")


def t_quote_has_an_exit() -> None:
    """🔴 实测②：「reply 只有入口没出口，你只要 reply 了就取消不了」。

    📌 **一个进得去出不来的状态，是个陷阱不是功能。**
    """
    print("\n[10] 🔴 引用必须有出口（进得去出不来 = 陷阱）")
    check("self._quote_bar = ui.row()" in APP, "⭐⭐ composer 上方有一条引用条")
    seg = APP[APP.find("self._quote_bar = ui.row()"):][:2200]
    check("on_click=lambda: self._set_reply_target(None)" in seg,
          "⭐⭐⭐ 条上有 ✕，点它就取消引用（这就是缺的那个出口）")
    check("_quote_bar_text" in seg, "条上显示被引用的原文")
    # 「重复了两个，去掉引用文字左边那个」
    # 📌 同一件事在相邻两处各说一遍，读者会以为它们是两件事。
    check("ui.label(self._REPLY_PROMPT)" not in seg,
          "⭐⭐ 引用条上**没有**第二个 ↳（输入框左边已经有一个了）")
    check(("border-left:2px solid var(--nano-amber)" in seg
           or "border-left:2px solid #e6a94e" in seg),
          "⭐ 靠左边那条橙色竖线表示「这是引用」，不靠再放一个符号")

    # ⚠️ 超长不许飞出去：三件事缺一不可
    check("truncate" in seg, "⭐ 单行省略号（truncate）")
    check("min-w-0" in seg, "⭐ min-w-0（否则 flex 子项按内容撑开、不肯缩）")
    check("overflow:hidden" in seg, "⭐ 父级 overflow:hidden")
    check("no-wrap" in seg, "不换行（最多一行）")

    # 只服务 selection —— 待审卡那条已经有出口了
    fn = _fn(APP, "_refresh_quote_bar")
    src = ast.get_source_segment(APP, fn) or ""
    check("QUOTE_SELECTION" in src,
          "⭐⭐ 只对选中引用生效（待审卡有自己的出口，两处表达同一件事必然不同步）")
    check('lbl, "text", None) != q' in src, "⚠️ 幂等（它被 1.5 秒定时器反复调）")

    # 🔴 顺序：引用条必须刷在提示符那段的**提前 return 之前**
    # ⚠️ 走 AST 比在源码文本里搜 "return" 可靠 —— 第一版命中的是
    #    **写在那儿解释这件事的注释**。
    #    📌 「按字符串出现过核，不算核」，本项目第 N 次。
    fnode = _fn(APP, "_refresh_reply_prompt")
    _bar_line = min((n.lineno for n in ast.walk(fnode)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "_refresh_quote_bar"), default=10 ** 9)
    _ret_line = min((n.lineno for n in ast.walk(fnode)
                     if isinstance(n, ast.Return)), default=10 ** 9)
    check(_bar_line < _ret_line,
          "⭐⭐ 引用条刷新在第一个 return 之前（否则第二次调用会被短路跳过）",
          f"bar@L{_bar_line} return@L{_ret_line}")
    # 行为：设/清都能被 _refresh_quote_bar 读到正确的值
    u = _FakeUI()
    from app import WebUI
    u._quote_bar = u._quote_bar_text = None       # 没建 UI 时不许崩
    WebUI._refresh_quote_bar(u, {"kind": "selection", "q": "x"})
    check(True, "composer 没建好时静默返回（启动早期也会被调到）")


def t_composer_context_menu() -> None:
    """🔴 实测：输入框没法右键粘贴，只能 Ctrl+V。

    📌 **一个「只有快捷键能做」的操作，对不知道快捷键的人等于不存在。**
    根因：native 窗口（frameless + WebView2）**没有原生右键菜单**。
    """
    print("\n[11] 🔴 输入框的右键菜单（native 没有原生菜单）")
    seg = APP[APP.find("function editableTarget(el)"):][:2600]
    check(bool(seg), "有 composer 的右键菜单实现")
    check("textarea, input[type=text]" in seg,
          "⭐ 认得出可编辑目标（textarea / input / contenteditable）")
    for label in ("粘贴", "全选", "复制", "剪切"):
        check(has_zh(seg, label), f"菜单里有「{label}」")
    check("if (sel) {" in seg,
          "⭐ 有选区时才给复制/剪切（没选区给了也没用）")
    check("navigator.clipboard.readText" in seg, "⭐⭐ 粘贴走 clipboard API")
    check("Ctrl+V" in APP, "⚠️ 读不到剪贴板时明说「请用 Ctrl+V」，不是静默失败")

    # 🔴🔴 这条是本项目栽过的那个形状：界面变了不等于状态变了
    check("execCommand('insertText'" in seg,
          "⭐⭐⭐ 粘贴走 execCommand('insertText')，不是直接改 .value")
    check("new Event('input'" in seg,
          "⭐⭐ 兜底路径也派发 input 事件（Quasar 的 v-model 只认它）")
    ins = APP[APP.find("function insertAtCursor"):][:900]
    check("Vue" in APP[APP.find("function editableTarget"):][:1200] or
          "v-model" in APP[APP.find("function editableTarget"):][:1200],
          "⚠️ 留痕写清了为什么不能直接赋值")
    # 剪切同理
    _i = APP.find(esc("剪切"))
    if _i < 0:
        _i = APP.find("剪切")
    cut = APP[_i:][:500]
    check("execCommand('delete')" in cut,
          "⭐ 剪切也走 execCommand（手改 .value 只改显示）")


def t_link_is_plain_highlight() -> None:
    """把链接的下划线也去掉，纯黄色高亮文字就行。"""
    print("\n[12] ⭐ 文件链接是纯高亮文字")
    i = APP.find("a.nano-file-link {{")
    j = APP.find("a.nano-file-link:hover {{", i)
    j = APP.find("}}", j) + 2
    css = APP[i:j]
    check("border-bottom" not in css, "⭐⭐ 连下划线都没有了")
    check("text-decoration: none" in css, "也没有 markdown 默认的下划线")
    check("var(--nano-amber)" in css or "#e6a94e" in css, "只剩黄色高亮")
    check("cursor: pointer" in css, "⭐ 可点这件事由颜色 + 鼠标形状表达")

    # ⭐ 我们**一个字符都不往链接里插** —— 用户看到的那个空格是模型自己打的
    dec = APP[APP.find("function decorateLinks(root)"):]
    dec = dec[:dec.find("function decorateCode")]
    check("prepend" not in dec and "createElement" not in dec,
          "⭐⭐ decorateLinks 不插入任何节点（那个空格不是我们加的）")
    check("nano-file-ico" not in APP, "那个图标 span 已经彻底没了")


def t_prompt_encourages_inline() -> None:
    """Nano 好像不喜欢把链接插进句子里 —— 先确认不是我们的提示词在劝退。"""
    print("\n[13] ⭐⭐ 提示词鼓励内联，且不劝退")
    ins = (ROOT / "config" / "system_instruction.txt").read_text(encoding="utf-8")
    check("as a word inside your normal sentence" in ins,
          "⭐⭐ 明说要写进句子里，而不是单独一行")
    check("not as a bare link on its own line" in ins,
          "⭐ 直接点名那个不想要的形态")
    # ⚠️ 「Use it sparingly」很容易被读成「尽量别用」——必须澄清它管的是数量
    check("That is about HOW MANY, not about whether to use it at all" in ins,
          "⭐⭐⭐ 澄清 sparingly 指的是**数量**，不是「要不要用」")
    check("when a file is\nworth pointing at, link it" in ins.replace("\r", ""),
          "⭐ 给了正面许可（值得指就指）")
    # 边界仍在
    check("do NOT turn a sentence into a row of links" in ins, "⚠️ 上界还在")


def main() -> int:
    print("=" * 74)
    print("[U3] 聊天区：选中引用 / 拖拽上传 / 文件链接 / 代码框")
    print("=" * 74)
    t_quote_state_is_shared_not_forked()
    t_two_injections_stay_separate()
    t_drop_reuses_the_upload_handler()
    t_file_link_is_not_an_exec_hole()
    t_file_link_is_inline_not_a_button()
    t_markdown_convention_survives()
    t_browser_side_is_delegated()
    t_prompt_has_bounds()
    t_copy_in_context_menu()
    t_quote_has_an_exit()
    t_composer_context_menu()
    t_link_is_plain_highlight()
    t_prompt_encourages_inline()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
