# -*- coding: utf-8 -*-
"""用户发过的图：**上下文可以忘，历史不可以。**

═══ 这个套件在验什么═══

> 「用户端的 UI 上不应该消失吧……**就算 cc 自己的图片也不会因为 cc 重启丢失啊**，
  哪怕模型早就不记得一百轮对话之前发过的图，用户这边也要能看到、甚至点开。
> 「**模型是模型，用户 UI 归用户 UI。** UI 更多的语义是【我曾经发过什么】」

⭐ 核实下来比预想的更简单也更糟：**像素从来没进过权威账本。**
   上传只把 bytes 塞进当轮 context 副本 + `memory.storage`（内存投影），
   落盘那条 user 消息**只有纯文本**。所以这不是"别删它"，是"本来就没存过"。

🔴 而且顺带查出一个**还没发作的泄露**：`compress_image_blocks` 当时会
   `update_message()` 把一段英文 `[System note — inserted by Nano's context
   manager…]` 写进**用户那条消息的落盘正文** —— 重放会把它画进 用户自己的气泡。
   那正是修过的泄露换了一扇门：上次是整条系统消息（`visible_to_user` 挡得住），
   这次是**粘在真实用户消息尾巴上的系统注记**（挡不住，那条消息确实是用户发的）。
   幸好落地后还没人发过图，落盘里 0 条中招。

📌 **判据：省 token 是模型侧的事，它不该有权改写"用户说过什么"。**

用法：
  py -3.10 tests\t_d10_user_images.py
"""
from __future__ import annotations

import ast
import base64
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# 一个最小的合法 PNG（1x1，透明）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def t_blob_store() -> None:
    print("\n[1] 图库：内容寻址 + 拒绝畸形引用")
    from core.runtime import blobs

    ref = blobs.put_image(_PNG, "image/png")
    check(bool(ref) and ref.endswith(".png"), "存得进去，引用带正确后缀", ref)
    check(blobs.put_image(_PNG, "image/png") == ref,
          "⭐ 内容寻址：同一张图第二次存返回同一个引用（不重复占盘）")
    check(blobs.image_path(ref) is not None, "引用能找回文件")
    uri = blobs.image_data_uri(ref)
    check(uri.startswith("data:image/png;base64,"), "读得回 data URI")

    # ⚠️ ref 来自落盘记录 —— 直接拼路径是路径穿越的标准入口。
    for bad in ("../../etc/passwd", "..\\..\\x.png", "zz.png", "", "a" * 64 + ".exe",
                "a" * 63 + ".png"):
        if blobs.image_path(bad) is not None or blobs.image_data_uri(bad):
            check(False, "⭐⭐ 畸形引用一律当作没有", f"放过了 {bad!r}")
            break
    else:
        check(True, "⭐⭐ 畸形引用一律当作没有（含 `../`、错长度、非白名单后缀）")

    check(blobs.put_image(b"", "image/png") == "", "空字节返回空串，不建垃圾文件")

    # ⚠️ 用户的图是**永久历史**，不许放系统临时目录（会被磁盘清理工具扫掉）。
    #    测试运行在临时数据目录（tests/_sandbox.py）里，所以检查的是：图库位于数据目录之下，
    #    且产品默认的数据目录（仓库 data/）不在系统临时目录里。
    import tempfile
    from core import paths as _paths
    _d = blobs.images_dir()
    check(_d.parent == _paths.data_dir()
          and str(pathlib.Path(tempfile.gettempdir())).lower() not in str(_paths.REPO_DATA_DIR).lower(),
          "⭐⭐ 图库【不在】系统临时目录里 —— "
          "📌 照抄先例（附件走 %TEMP%/nano_temp_uploads）之前先问它当初为什么放那儿："
          "附件是当轮素材，用户的图是永久历史",
          f"{_d} | default={_paths.REPO_DATA_DIR}")


def t_payload_roundtrip() -> None:
    print("\n[2] 落盘往返：ui_images 存得下、读得回，且【为空时一个字节都不写】")
    from core.schema import ChatMessage
    from core.runtime.conversation import _message_payload, _message_from_payload

    plain = _message_payload(ChatMessage(role="user", content="hi"))
    check("ui_images" not in plain,
          "⭐ 没有图时 payload 里不出现这个键 —— "
          "📌 同 `visible_to_user`：新增一个可选事实，不该改写既有事实的形状")

    m = ChatMessage(role="user", content="看这个", ui_images=["a" * 64 + ".png"])
    back = _message_from_payload("user", _message_payload(m))
    check(back.ui_images == ["a" * 64 + ".png"], "有图时往返一致")
    check(_message_from_payload("user", {"content": "x"}).ui_images == [],
          "老行（没这个键）读回来是空列表，不报错")


def t_compress_never_touches_history() -> None:
    """🔴 本套件的核心：压缩只许动模型投影，不许改写用户历史。"""
    print("\n[3] ⭐⭐⭐ compress_image_blocks 不再写回权威账本")
    src = pathlib.Path("memory/manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "compress_image_blocks":
            fn = n
    check(fn is not None, "前置条件：找得到 compress_image_blocks")
    if fn is None:
        return

    updates = [c.lineno for c in ast.walk(fn)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "update_message"]
    check(not updates,
          "⭐⭐⭐ 函数体内 0 处 update_message —— "
          "🔴 改造前它把一段英文系统注记写进【用户消息的落盘正文】，"
          "重放会原样画进用户自己的气泡（那个泄露换了一扇门进来）",
          f"发现于 L{updates}" if updates else "")

    # ⚠️ 反向前置：证明它**仍然在压**（别把压缩一起删了还全绿）。
    seg = ast.get_source_segment(src, fn) or ""
    check("_image_note_for" in seg and "msg.content" in seg,
          "⚠️ 前置条件：它仍然在改 storage 的 content（压缩本身没被删掉）")


def t_note_is_derived_not_stored() -> None:
    print("\n[4] ⭐⭐ 重启后那句「你当时确实看过」是【算】出来的，不是【存】在历史里的")
    from memory.manager import MemoryManager
    src = pathlib.Path("memory/manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    check("_restore_image_notes" in names, "存在 `_restore_image_notes`")
    check("attach_user_images" in names,
          "⭐ 存在 `attach_user_images` —— 图片进入账本的**唯一收口点**")

    hyd = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_hydrate_current_session":
            hyd = n
    called = hyd is not None and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "_restore_image_notes" for c in ast.walk(hyd))
    check(called, "⭐⭐ hydrate 里确实调了它 —— 📌 写了没人调，和没写一模一样")

    # 行为：带 ui_images 的消息会被补上注记，且幂等
    mm = MemoryManager.__new__(MemoryManager)
    from core.schema import ChatMessage
    msg = ChatMessage(role="user", content="看这个", ui_images=["a" * 64 + ".png"])
    mm.storage = [msg, ChatMessage(role="user", content="没图这条")]
    mm._restore_image_notes()
    check("[System note" in msg.content and "看这个" in msg.content,
          "⭐ 有图的那条被补上注记，用户原文仍在最前面")
    check("You DID see" in msg.content,
          "⚠️ 步1 的保证没丢（模型不许说'我从没见过图片'）")
    before = msg.content
    mm._restore_image_notes()
    check(msg.content == before, "⭐ 幂等：再 hydrate 一次不会贴第二遍")
    check("[System note" not in mm.storage[1].content, "没图的那条一个字没动")


def t_ui_reads_history_not_context() -> None:
    print("\n[5] ⭐⭐ UI 重放读的是 ui_images（历史），不是 content 里的 image block（上下文）")
    app_src = pathlib.Path("app.py").read_text(encoding="utf-8")
    app_tree = ast.parse(app_src)
    fn = None
    for n in ast.walk(app_tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_replay_user_images":
            fn = n
    check(fn is not None, "存在 `_replay_user_images`")
    if fn is not None:
        seg = ast.get_source_segment(app_src, fn) or ""
        check("ui_images" in seg,
              "⭐⭐ 它读 `ui_images` —— 📌 上下文可以忘，历史不可以")
        check("image_data_uri" in seg, "从 Nano 自己的图库读，不碰用户原始路径")

    caller = None
    for n in ast.walk(app_tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_replay_durable_conversation":
            caller = n
    called = caller is not None and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "_replay_user_images" for c in ast.walk(caller))
    check(called, "⭐ 重放主循环里确实调了它")

    # ⚠️ Nano 自己的截图**方向相反**：已明确定为「重启不保留」。
    import app as _app
    check("screenshot_preview" in _app._CHAT_EVENTS_EPHEMERAL,
          "⭐⭐ 反向：nano 自己的截图仍是 EPHEMERAL —— "
          "当初的说法：「重启不保留，刚好是这些信息在 UI 被抛弃的出口」。"
          "📌 一个是「我曾经发过什么」，一个是「它当时看到什么」")


def t_attach_runs_before_content_patch() -> None:
    """🔴 顺序错了 = 把整张图的 base64 塞进权威账本。"""
    print("\n[6] ⭐⭐⭐ 留档排在 content 被换成 base64 list 【之前】")
    src = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    attach_line = patch_line = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "attach_user_images"):
            attach_line = n.lineno
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and n.targets[0].attr == "content"
                and isinstance(n.targets[0].value, ast.Name)
                and n.targets[0].value.id == "_last_user_msg"):
            patch_line = n.lineno
    check(attach_line is not None, "前置条件：找得到 attach_user_images 调用点",
          f"L{attach_line}")
    check(patch_line is not None, "前置条件：找得到 `_last_user_msg.content = _parts`",
          f"L{patch_line}")
    if attach_line and patch_line:
        check(attach_line < patch_line,
              "⭐⭐⭐ 留档在前、改 content 在后 —— 🔴 反过来会让 `update_message` "
              "把整张图的 base64 写进权威账本（payload 暴涨 + 落盘里有两份图）",
              f"attach@L{attach_line} < patch@L{patch_line}")

    # 🔴 第二条顺序约束 —— **实测栽在这一条**（2026-08-13）。
    #
    # `attach_user_images` 原本排在 system_guide 组装**之后**，于是
    # `_image_note_request_block()` 求值那一刻 `ui_images` 还是空的，
    # `has_unsummarized_image()` 恒 False → **那段提示永远不出现，摘要永远不生成**。
    # ⚠️ 而它**一声不响**：图答对了（模型直接看着像素），只有 `image_summary` 是 None。
    # 📌 **一条顺序断言只保护它写下的那个顺序** —— 上面那条钉住了 ①，摔的是 ②。
    # 🔴🔴 这一格连摔三次，每次都是**另一个**顺序（2026-08-13）。
    #   ① attach 必须在 content 换成 base64 list 之前   ← 上面那条，一开始就钉住了
    #   ② 提示段求值时 `ui_images` 得已经在              ← 摔第二次
    #   ③ 🔴 **核心工具清单在用户消息进 memory 之前就算完了** ← 摔第三次
    #      于是 `note_image` 掉进 deferred，模型只能先 load_tools 去捞；
    #      而 ReAct 循环里 `storage[-1]` 又变成 tool_results，判据再次翻假 →
    #      下一轮它从清单里消失 → 模型说「note_image 工具不可用」。
    # ⭐ 正解不是再补一条顺序断言，是**把判据从 memory 上摘下来** ——
    #    改成本轮标志 `_turn_image_pending`，于是三个顺序全部不再相关。
    # 📌 **一条顺序断言只保护它写下的那个顺序；能取消顺序依赖就别去排顺序。**
    print("\n[6b] ⭐⭐⭐ 判据不挂在 memory 上：本轮标志，与三处顺序全部无关")
    import core.orchestrator as _O
    src2 = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    t2 = ast.parse(src2)
    fn = next((n for n in ast.walk(t2)
               if isinstance(n, ast.FunctionDef) and n.name == "has_unsummarized_image"), None)
    check(fn is not None, "前置条件：找得到 `has_unsummarized_image`")
    if fn is not None:
        # ⚠️ **先剥 docstring 再断言。** 这个形状在本项目栽过三次（`_prompt_text`
        #    / `_fn_code` / 这里）：注释里解释"为什么不读 memory"，
        #    那两个字就把断言喂绿/喂红了。
        #    📌 **断言读的是代码，不是对代码的解释。**
        _body = [s for s in fn.body
                 if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                         and isinstance(s.value.value, str))]
        seg = "\n".join((ast.get_source_segment(src2, s) or "") for s in _body)
        check("_turn_image_pending" in seg,
              "⭐⭐⭐ 它读的是**本轮标志**")
        check("storage" not in seg and "memory" not in seg,
              "⭐⭐⭐ 🔴 它**不读 memory/storage** —— "
              "那个位置一轮之内会变好几次（用户消息还没进 / 变成 tool_results），"
              "而「这一轮有没有图」只有一个答案",
              seg[-120:] if "storage" in seg or "memory" in seg else "")

    # 本轮标志必须在**函数最开头**就落定（早于任何工具清单计算）
    impl = next((n for n in ast.walk(t2)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_query_impl"), None)
    check(impl is not None, "前置条件：找得到 `_handle_query_impl`")
    if impl is not None:
        _set = [n.lineno for n in ast.walk(impl)
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and n.targets[0].attr == "_turn_image_pending"]
        _plan = [n.lineno for n in ast.walk(impl)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("debug", "info", "warning")
                 and "TOKEN-PLAN" in (ast.get_source_segment(src2, n) or "")]
        check(bool(_set), "本轮标志确实在 `_handle_query_impl` 里落定", f"L{_set}")
        check(bool(_plan), "⚠️ [L5] 前置：找得到核心工具清单那一步（TOKEN-PLAN）", f"L{_plan}")
        if _set and _plan:
            check(min(_set) < min(_plan),
                  "⭐⭐⭐ 标志落定排在**核心工具清单计算之前** —— "
                  "🔴 反过来时 `note_image` 会被算进 deferred，"
                  "模型只能 load_tools 去捞、下一轮又没了",
                  f"flag@L{min(_set)} < plan@L{min(_plan)}")

    # 🔴 第四次栽在 `storage[-1]` 上（2026-08-13）：`note_image` **真的调了**，
    #    但 `set_image_summary` 读 `storage[-1]` —— ReAct 循环里那已经是 `tool_calls`，
    #    于是它答"当前没有图"，摘要永远落不下去。**日志里工具是成功的，摘要却是 None。**
    # 📌 **别问"最后一条是什么"，直接问"我要的那条在哪"。**
    src3 = pathlib.Path("memory/manager.py").read_text(encoding="utf-8")
    t3 = ast.parse(src3)
    for _name in ("set_image_summary", "attach_user_images"):
        _f = next((n for n in ast.walk(t3)
                   if isinstance(n, ast.FunctionDef) and n.name == _name), None)
        check(_f is not None, f"前置条件：找得到 `{_name}`")
        if _f is None:
            continue
        # ⚠️⚠️ **断言走 AST，不看源码文本。** 这一格连着栽了三次在同一件事上：
        #    先钉源码换行、再钉关键词缺席、再被自己的 docstring 和 `#` 注释喂红。
        #    📌 **只要断言读的是文本，对代码的【解释】就会参与判定。**
        #       —— 而解释里出现 `storage[-1]` 恰恰是因为那句话在说明「为什么不用它」。
        _bad = []
        for _n in ast.walk(_f):
            if not isinstance(_n, ast.Subscript):
                continue
            v = _n.value
            if not (isinstance(v, ast.Attribute) and v.attr == "storage"):
                continue
            sl = _n.slice
            if (isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub)
                    and isinstance(sl.operand, ast.Constant) and sl.operand.value == 1):
                _bad.append(_n.lineno)
        check(not _bad,
              f"⭐⭐⭐ `{_name}` 不读 `storage[-1]` —— "
              "🔴 一轮之内那个位置会变成 user→tool_calls→tool_results→assistant，"
              "读它就等于「工具调成功了、摘要却是 None」",
              f"L{_bad}" if _bad else "")
    _h = next((n for n in ast.walk(t3)
               if isinstance(n, ast.FunctionDef) and n.name == "_last_user_image_message"), None)
    check(_h is not None,
          "⭐ 有一个共用的「最近那条带图的用户消息」定位器（两处别各写一份）")

    # 记完之后标志必须翻假，且**落库失败也要翻**
    h = next((n for n in ast.walk(t2)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_note_image"), None)
    if h is not None:
        _clear = [n for n in ast.walk(h)
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Attribute)
                  and n.targets[0].attr == "_turn_image_pending"]
        check(bool(_clear), "⭐ 记完之后标志翻假（一次性的兑现）")
        # 必须在 `if not _n:` 早退**之前** —— 否则落库失败会导致每轮重复要求
        _early = [n.lineno for n in ast.walk(h) if isinstance(n, ast.Return)]
        check(bool(_clear) and _clear[0].lineno < max(_early or [0]),
              "⚠️ 标志在任何早退之前就落下 —— "
              "📌 否则「记不下来」会变成「每轮再要求它记一次」，"
              "正是点名不许出现的那个形状")


def t_summary_is_one_shot_not_per_turn() -> None:
    """⚠️⚠️ 用户特别点名的那个坑：

    > 「摘要千万不能跟 changelog 修复那个 base64 的 bug 一样，**一直每轮注入**」

    ⭐ 这里钉的不是"我们记得别注入"，是**它结构上没有第二轮可注入** ——
       工具、提示段、事实来源**由同一个判据控制**，摘要一写上三者一起消失。
    """
    print("\n[7] ⭐⭐⭐ 摘要是一次性的：写完之后工具和提示段【一起消失】")
    from core.schema import ChatMessage
    from core.tools.builtin import _when_image_needs_summary

    class _Stub:
        def __init__(self, msg):
            self.msg = msg
        def has_unsummarized_image(self):
            m = self.msg
            return bool(m is not None and m.role == "user"
                        and getattr(m, "ui_images", None)
                        and not getattr(m, "image_summary", ""))

    _no_img = ChatMessage(role="user", content="纯文字")
    _img = ChatMessage(role="user", content="看这个", ui_images=["a" * 64 + ".png"])
    check(not _when_image_needs_summary(_Stub(_no_img)),
          "没有图的一轮：note_image 不出现")
    check(_when_image_needs_summary(_Stub(_img)),
          "⭐ 有图且没记过：note_image 出现（前置，否则下一条恒真）")
    _img.image_summary = "一张三色条图"
    check(not _when_image_needs_summary(_Stub(_img)),
          "⭐⭐⭐ 记完之后立刻为假 —— **没有第二轮可注入**，"
          "这就是「不会每轮注入」的结构性保证（不是靠谁记得）")

    # 提示段与工具**必须由同一个判据控制**（：一个工具和它的事实来源同源）
    src = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    blk = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_image_note_request_block":
            blk = n
    check(blk is not None, "存在一次性提示段 `_image_note_request_block`")
    if blk is not None:
        seg = ast.get_source_segment(src, blk) or ""
        check("has_unsummarized_image" in seg,
              "⭐⭐ 提示段读的是**同一个判据** —— 📌 [F4]：一个工具和它的事实来源，"
              "必须由同一个条件控制；这里连提示词也挂在上面，三者同生共死")


def t_summary_is_not_the_answer() -> None:
    """⚠️ 用户用两个例子说透的坑：摘要 ≠ 答案。它必须写在**模型读得到的地方**。"""
    print("\n[8] ⭐⭐ 「摘要不是答案」写进了工具描述和提示段，不是只留在注释里")
    import core.orchestrator as O
    desc = O._NOTE_IMAGE_MANIFEST["description"]
    check("NOT YOUR ANSWER" in desc.upper(),
          "⭐⭐ 工具描述里明写「摘要不是你的答案」")
    check("1+1" in desc,
          "⭐ 带了那个具体例子（白底黑字『1+1 等于几』→ 回复应当是『2』）——"
          "📌 抽象禁令模型容易绕过，具体反例不容易")
    check("independently" in desc.lower() or "independent" in desc.lower(),
          "⚠️ 明说摘要要独立于用户的问题来写")

    # 回看那条阶梯同样要在模型读得到的地方
    vdesc = O._VIEW_PAST_IMAGE_MANIFEST["description"]
    check("RESTRAINT" in vdesc.upper() and vdesc.upper().index("RESTRAINT") < len(vdesc) // 2,
          "⭐⭐ 回看工具**先说什么时候别用**，再说怎么用 —— "
          "📌 顺序反了那句『少用』没人听 —— 摘要够用就别回看，不是提到图片就回看")
    check("re-upload" in vdesc,
          "⚠️ 明令：图还在盘上就不许让用户重新上传")


def t_ledger_never_stores_pixels() -> None:
    """🔴 我两天里差点犯两次的同一个错 —— 所以收口在唯一出口上。"""
    print("\n[9] ⭐⭐⭐ 权威账本不存像素（收口在 `_message_payload`，不在调用点）")
    from core.schema import ChatMessage
    from core.runtime.conversation import _message_payload

    _blocks = [{"type": "text", "text": "看这个"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": "A" * 5000}}]
    m = ChatMessage(role="user", content=list(_blocks), ui_images=["a" * 64 + ".png"])
    pay = _message_payload(m)
    check("A" * 100 not in str(pay),
          "⭐⭐⭐ 像素已经在图库里时，落盘 payload 里没有 base64 —— "
          "🔴 少这一条，任何一次 update_message 都会把整张图写进 SQLite")
    check(any(isinstance(b, dict) and b.get("type") == "text" for b in pay["content"]),
          "⚠️ 用户的文字块一个没少")

    # ⚠️ fail-safe 方向：**没进图库就不许拿掉**（宁可账本里多一份，不可两边都没有）
    m2 = ChatMessage(role="user", content=list(_blocks))     # 没有 ui_images
    check("A" * 100 in str(_message_payload(m2)),
          "⭐⭐ 没有 ui_images 时**原样保留** —— "
          "📌 fail-safe 方向：宁可账本里多一份 base64，不可两边都没有")

    # 摘要往返
    m3 = ChatMessage(role="user", content="x", ui_images=["b" * 64 + ".png"],
                     image_summary="一张三色条图")
    from core.runtime.conversation import _message_from_payload
    check(_message_from_payload("user", _message_payload(m3)).image_summary == "一张三色条图",
          "摘要往返一致")
    check("image_summary" not in _message_payload(ChatMessage(role="user", content="x")),
          "⚠️ 没摘要时 payload 里不出现这个键（同 ui_images / visible_to_user 的纪律）")


def t_review_tool_fails_loudly() -> None:
    print("\n[10] ⭐⭐ 回看的三种失败**必须彼此可区分**")
    import core.orchestrator as O
    src = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_view_past_image"), None)
    check(fn is not None, "存在 `_handle_view_past_image`")

    # ⚠️ 第一版把这两条钉在**源码文本**上，结果因为那句话被换行拆成两个字面量而假红。
    # 📌 **断言要钉在语义上，不要钉在源码的换行位置上** —— 所以改成真的调它一次。
    import asyncio as _aio
    _stub = O.Orchestrator.__new__(O.Orchestrator)
    _stub.provider = None
    _bad = _aio.get_event_loop().run_until_complete(
        O.Orchestrator._handle_view_past_image(
            _stub, {"handle": "img#zzzzzzzz", "question": "x"}, "aid", event_queue=None))
    check("could not resolve" in _bad and "handle" in _bad,
          "⭐⭐ 把手认不出 → 明说「认不出这个把手」")
    # ⚠️ 第二版又错了一次：断言写成「返回里不许出现 gone」——
    #    可它出现在**禁令**里（"do not tell the user the image is gone"），是对的。
    # 📌 **「某个词不出现」几乎从来不是判据** —— 判据是「那句话说了什么」。
    #    （同一条教训在这一格连栽两次：先钉源码换行，再钉关键词缺席。）
    check("do not tell the user the image is gone" in _bad,
          "⭐⭐⭐ 🔴 认不出时**明令禁止**模型说「图没了」—— "
          "含糊过去会让它转头要求用户重新上传，正是 [D10] 一开始那个 bug",
          _bad[:70])

    seg = (ast.get_source_segment(src, fn) or "") if fn else ""
    check("no longer on disk" in seg and "now it IS correct to" in seg,
          "⭐ 文件真的没了 → 这时**才**轮到请用户重发，并说明原因")

    from core.runtime.blobs import resolve_handle
    check(resolve_handle("img#zzzzzzzz") == "" and resolve_handle("") == "",
          "认不出的把手返回空串（不猜）")


def main() -> int:
    t_blob_store()
    t_payload_roundtrip()
    t_compress_never_touches_history()
    t_note_is_derived_not_stored()
    t_ui_reads_history_not_context()
    t_attach_runs_before_content_patch()
    t_summary_is_one_shot_not_per_turn()
    t_summary_is_not_the_answer()
    t_ledger_never_stores_pixels()
    t_review_tool_fails_loudly()
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
