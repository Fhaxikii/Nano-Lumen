# -*- coding: utf-8 -*-
"""可恢复对话的持久化验收。

这些测试刻意只碰真实 RuntimeStore 和真实 ChatMessage；不 mock SQLite，
因为要守的正是跨进程边界的真实序列化语义。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import ast

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from core.runtime import RuntimeStore
from core.runtime.conversation import ConversationRepository, _message_from_payload
from core.schema import ChatMessage, ToolCall, ToolResultBlock
from memory.manager import MemoryManager

_results: list[tuple[bool, str, str]] = []



def _force_legacy_truncation():
    """把 `_truncate_safely` 按**旧的 10 轮硬切**跑一次的上下文管理器。

    ⚠️⚠️ 2026-08-14 阶梯打开后：`ladder_enabled=true` 时旧截断**按设计退位**
       （触发点抬到 `max_turns*10` + 响亮报警），于是所有「灌几轮 → 看它被切掉」
       的测试都不再成立。
    🔴 但本项验的东西没过期：**UI 重放读完整账本，模型投影读被裁过的那份**。
       截断只是它的**布景**。
    📌 一条测试的布景失效时，要换布景，不是删掉那条测试 ——
       删掉的话，等哪天有人把退位改回去，没有任何东西会红。
    """
    import contextlib
    import core.models as _M

    @contextlib.contextmanager
    def _cm():
        _orig = _M.ladder_enabled
        _M.ladder_enabled = lambda: False
        try:
            yield
        finally:
            _M.ladder_enabled = _orig
    return _cm()

def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_current_session_and_order(tmp: pathlib.Path) -> None:
    print("\n[1] 当前会话唯一、消息顺序严格递增")
    store = RuntimeStore(tmp / "runtime.db")
    repo = ConversationRepository(store)
    first = repo.current_session()
    again = repo.current_session()
    check(first.session_id == again.session_id, "重复取得的是同一个 current session")

    repo.append_message(first.session_id, ChatMessage("user", "第一句"))
    repo.append_message(first.session_id, ChatMessage("assistant", "第一答"))
    restored = repo.load_messages(first.session_id)
    check([m.content for m in restored] == ["第一句", "第一答"],
          "按 ordinal 还原原始聊天顺序")
    store.close_thread_conn()


def t_round_trip_preserves_provider_critical_blocks(tmp: pathlib.Path) -> None:
    print("\n[2] thinking 与多工具配对可逆往返")
    store = RuntimeStore(tmp / "roundtrip.db")
    repo = ConversationRepository(store)
    session = repo.current_session()
    thinking = [{"type": "thinking", "thinking": "先核对", "signature": "sig_exact"}]
    calls = ChatMessage(
        "tool_calls", thinking_blocks=thinking,
        tool_calls=[ToolCall("lookup", {"q": "Nano"}, "tool_1", 0),
                    ToolCall("inspect", {"path": "x"}, "tool_2", 1)],
    )
    results = ChatMessage(
        "tool_results",
        tool_results=[ToolResultBlock("lookup", "tool_1", "found"),
                      ToolResultBlock("inspect", "tool_2", "denied", is_error=True)],
    )
    repo.append_message(session.session_id, calls)
    repo.append_message(session.session_id, results)
    restored = repo.load_messages(session.session_id)
    check([m.to_dict() for m in restored] == [calls.to_dict(), results.to_dict()],
          "provider 必需的 thinking、tool_use_id、参数、顺序与 is_error 均未变形")
    store.close_thread_conn()


def t_memory_hydrates_durable_projection(tmp: pathlib.Path) -> None:
    print("\n[3] MemoryManager 的当前投影可由账本重建")
    store = RuntimeStore(tmp / "projection.db")
    repo = ConversationRepository(store)
    first = MemoryManager(max_turns=10, conversation_repository=repo)
    first.add_message("user", "重启后还记得吗")
    first.add_message("assistant", "记得，来自持久账本。")

    restarted = MemoryManager(max_turns=10, conversation_repository=repo)
    check([m.content for m in restarted.storage] == ["重启后还记得吗", "记得，来自持久账本。"],
          "新的 MemoryManager 自动恢复当前会话，而不是创建第二会话")
    check(restarted.validate_tool_turns()[0], "恢复后的 provider 上下文仍合法")
    store.close_thread_conn()


def t_restart_drops_only_incomplete_tool_transaction(tmp: pathlib.Path) -> None:
    print("\n[4] 崩在 tool_calls 与结果之间，不伪造结果也不污染恢复上下文")
    store = RuntimeStore(tmp / "incomplete.db")
    repo = ConversationRepository(store)
    before = MemoryManager(max_turns=10, conversation_repository=repo)
    before.add_message("user", "先完成这一句")
    before.add_message("assistant", "这句已经完成。")
    before.add_tool_calls([ToolCall("slow_tool", {}, "dangling_tool", 0)])

    restarted = MemoryManager(max_turns=10, conversation_repository=repo)
    check([m.content for m in restarted.storage] == ["先完成这一句", "这句已经完成。"],
          "只排除未闭合 tool_calls，之前有效消息完整保留")
    check(restarted.validate_tool_turns()[0], "不会把孤立 tool_use 送给 provider")
    store.close_thread_conn()


def t_explicit_reset_rotates_only_session_boundary(tmp: pathlib.Path) -> None:
    print("\n[5] 只有显式重置才切到新的空会话")
    store = RuntimeStore(tmp / "reset.db")
    repo = ConversationRepository(store)
    memory = MemoryManager(max_turns=10, conversation_repository=repo)
    old_session = memory.conversation_session_id
    memory.add_message("user", "旧会话的内容")

    new_session = memory.reset_conversation()
    check(new_session != old_session and memory.conversation_session_id == new_session,
          "重置创建唯一的新 current session")
    check(memory.storage == [], "新会话的内存投影为空")
    check([m.content for m in repo.load_messages(old_session)] == ["旧会话的内容"],
          "旧会话原文仍留在账本，没有被重置动作物理删除")
    check(repo.load_messages(new_session) == [], "新会话尚未注入旧会话原文")
    store.close_thread_conn()


def t_real_app_wires_durable_memory_and_reset() -> None:
    print("\n[6] 真实 WebUI 与重置入口接到持久会话")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    orch_source = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    app_tree = ast.parse(app_source)
    webui = next(node for node in app_tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "WebUI")
    init = next(node for node in webui.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__")
    init_text = ast.unparse(init)
    check("ConversationRepository" in app_source and "conversation_repository=" in init_text,
          "WebUI 的真实 MemoryManager 绑定 Runtime 对话仓储")

    orch_tree = ast.parse(orch_source)
    orch = next(node for node in orch_tree.body
                if isinstance(node, ast.ClassDef) and node.name == "Orchestrator")
    reset = next(node for node in orch.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "reset_conversation")
    check("self.memory.reset_conversation()" in ast.unparse(reset),
          "唯一的重置入口轮换 durable session，而不是只清内存")


def t_ui_replay_reads_full_session_not_bounded_model_projection(tmp: pathlib.Path) -> None:
    print("\n[7] UI 重放读取完整会话，不受模型 max_turns 截断")
    store = RuntimeStore(tmp / "full-history.db")
    repo = ConversationRepository(store)
    memory = MemoryManager(max_turns=1, conversation_repository=repo)
    # ⚠️ 显式走**旧的 10 轮硬切**（见 `_force_legacy_truncation`）——
    #    本项验的是「UI 读完整账本 / 模型读被裁过的那份」，截断只是布景。
    with _force_legacy_truncation():
        memory.add_message("user", "第一问")
        memory.add_message("assistant", "第一答")
        memory.add_message("user", "第二问")
        memory.add_message("assistant", "第二答")
    check([m.content for m in memory.storage] == ["第二问", "第二答"],
          "模型投影仍按 max_turns 截断")
    check([m.content for m in memory.conversation_messages()] ==
          ["第一问", "第一答", "第二问", "第二答"],
          "UI 可从权威账本取完整当前会话")
    store.close_thread_conn()


def t_ui_replay_is_passive_and_uses_durable_messages() -> None:
    print("\n[8] UI 重放是账本的被动投影，不触发新执行")
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    webui = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "WebUI")
    replay = next((node for node in webui.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == "_replay_durable_conversation"), None)
    replay_text = ast.unparse(replay) if replay else ""
    check(replay is not None and "conversation_messages()" in replay_text,
          "重放器从完整 durable session 读取，而不是从 bounded storage 读取")
    check("handle_query" not in replay_text and "navigate_pipeline" not in replay_text
          and "_run_react_loop" not in replay_text,
          "重放不调用模型、不执行工具、不重启 live pipeline")
    batch_renderer = next((node for node in webui.body
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name == "_render_durable_tool_batch"), None)
    batch_text = ast.unparse(batch_renderer) if batch_renderer else ""
    check("interrupted before result" in batch_text,
          "未闭合工具事务在历史 UI 明示为重启前中断，绝不伪装成仍在执行或成功")
    check(batch_renderer is not None and "set_visibility(False)" in batch_text
          and ".on('click'" in batch_text,
          "历史连续工具往返重放为默认折叠、可点击展开的同一张 used-tools pill")
    check("_render_durable_tool_batch" in replay_text,
          "重放器按工具批次调用折叠卡片，而不是按单条 tool_calls 平铺")
    response_opener = next((node for node in webui.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name == "_open_durable_replay_response"), None)
    check(response_opener is not None and "nano ❯" in ast.unparse(response_opener),
          "历史工具批次先进入同一段 Nano 回应，再显示 pill")
    opener_text = ast.unparse(response_opener) if response_opener else ""
    opener_assignments = [node for node in ast.walk(response_opener) if isinstance(node, ast.Assign)] if response_opener else []
    response_body_assign = any(
        any(isinstance(target, ast.Name) and target.id == "response_body" for target in node.targets)
        and isinstance(node.value, ast.Call)
        for node in opener_assignments
    )
    legacy_body_assign = any(
        any(isinstance(target, ast.Name) and target.id == "body" for target in node.targets)
        for node in opener_assignments
    )
    check(response_body_assign
          and "return response_body" in opener_text
          and not legacy_body_assign,
          "历史回放的工具列必须嵌在 nano 前缀同行，不能另起纵向 body 制造空白")
    check("_open_durable_replay_response" in replay_text
          and replay_text.index("_open_durable_replay_response") < replay_text.index("_render_durable_tool_batch"),
          "重放顺序保持 nano 前缀 → used-tools pill → Nano 正文，不把 pill 挂到回应之前")
    render = next(node for node in webui.body
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "render")
    check("_replay_durable_conversation()" in ast.unparse(render),
          "聊天容器创建后实际调用重放器")


def t_later_memory_compression_updates_durable_record(tmp: pathlib.Path) -> None:
    """🔴 **这条测试原本钉的是一个 bug。**（2026-08-13 改写）

    旧断言：「内存后续压缩也同步到持久账本」—— 它把
    「落盘账本 == 模型上下文」当成了不变量。**方向错了。**

    「模型是模型，用户 UI 归用户 UI。UI 更多的语义是【我曾经发过什么】」

    压缩是**省 token 的模型侧动作**，而它当时会把一段英文
    `[System note — inserted by Nano's context manager…]`
    写进**用户那条消息的落盘正文** —— 重放会把它画进 用户自己的气泡。
    那正是本文件下一条测试（系统注记不上屏）修过的泄露，**换了一扇门进来**：
    上次是整条系统消息（`visible_to_user` 挡得住），
    这次是**粘在真实用户消息尾巴上的**（挡不住，那条消息确实是用户发的）。

    📌 **判据：省 token 是模型侧的事，它不该有权改写"用户说过什么"。**
    ⭐ 所以现在钉的是**相反**的不变量：压缩**只动 storage**，落盘一个字不改。
    """
    print("\n[9] ⭐⭐⭐ 压缩只动模型投影，**不许改写用户历史**（原断言方向相反，已改）")
    store = RuntimeStore(tmp / "compressed-image.db")
    repo = ConversationRepository(store)
    memory = MemoryManager(max_turns=10, conversation_repository=repo)
    memory.add_message("user", content=[
        {"type": "text", "text": "请看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
    ])
    check(memory.compress_image_blocks() == 1, "前置条件：当前投影确实压缩了图片")
    check("[System note" in memory.storage[0].content,
          "前置条件：模型投影里确实出现了那句系统注记（压缩本身还在工作）")

    _durable = repo.load_messages(memory.conversation_session_id)
    check(isinstance(_durable[0].content, list),
          "⭐⭐⭐ 落盘正文**没有**被压成字符串 —— 用户历史原样保留",
          repr(str(_durable[0].content)[:60]))
    check("[System note" not in str(_durable[0].content),
          "⭐⭐⭐ 落盘正文里**没有**系统注记 —— "
          "🔴 有的话，重放会把它画进用户自己的气泡")

    restarted = MemoryManager(max_turns=10, conversation_repository=repo)
    check(restarted.storage[0].content != memory.storage[0].content,
          "⭐ 重启后的模型上下文来自**原始账本**，不是上一次进程压过的那份 —— "
          "📌 投影可以丢，账本不许跟着变形")
    store.close_thread_conn()


def t_user_images_survive_restart_in_ui(tmp: pathlib.Path) -> None:
    """走真实路径时：像素归图库、注记靠推导、历史一个字不改。"""
    print("\n[9b] ⭐⭐ 用户发过的图：UI 侧跨重启还在，模型侧那句注记是【算】出来的")
    import base64 as _b64
    _png = _b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    store = RuntimeStore(tmp / "user-images.db")
    repo = ConversationRepository(store)
    memory = MemoryManager(max_turns=10, conversation_repository=repo)
    memory.add_message("user", "看这张图")
    _part = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": _b64.b64encode(_png).decode()}}
    check(memory.attach_user_images([_part]) == 1, "前置条件：留档成功（1 张）")

    _durable = repo.load_messages(memory.conversation_session_id)
    check(len(_durable[0].ui_images) == 1,
          "⭐⭐ 引用落进了账本 —— 这就是 UI 重启后还画得出图的唯一依据")
    check(_durable[0].content == "看这张图",
          "⭐⭐ 用户正文一个字没被动过（base64 **没有**被写进落盘）",
          repr(_durable[0].content))

    from core.runtime.blobs import image_data_uri
    check(image_data_uri(_durable[0].ui_images[0]).startswith("data:image/png"),
          "⭐⭐⭐ 从 Nano 自己的图库读得回像素 —— "
          "📌 用户删掉自己的原文件也不影响（所以不能存原始路径）")

    restarted = MemoryManager(max_turns=10, conversation_repository=repo)
    check("You DID see" in restarted.storage[0].content,
          "⭐⭐ 重启后模型仍被告知「你当时确实看过」—— "
          "但它是从 `ui_images` **推导**出来的，不是写在历史里的（[D10] 步1 没丢）")
    store.close_thread_conn()


def t_system_notes_reach_model_but_never_the_screen(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ [2026-08-13 实测] 系统注记进上下文，但不上屏。

    🔴 用户重启 Nano，聊天区冒出一个 `Koala ❯` 气泡，正文是
       `[System check-in] You put this in the background a while ago…`
       —— 一整段英文系统提示词，署着用户的名字。

    这类注记**必须**以 user/assistant 角色进上下文（provider 只认这两种），
    原样落盘也是对的（不然 thinking 签名与工具往返重启后不合法）。
    错的是账本里**没有任何一列**区分得开「模型读的」和「用户看过的」。

    📌 **一份账本如果同时被当作「模型上下文」和「用户看过的东西」，
       它就必须记下这两者的差别 —— 否则重放必然泄露。**
    """
    print("\n[10] ⭐⭐⭐ 系统注记：模型读得到，用户看不到")
    store = RuntimeStore(tmp / "sysnote.db")
    repo = ConversationRepository(store)
    memory = MemoryManager(max_turns=10, conversation_repository=repo)

    memory.add_message("user", "调用 slowprogresstest")
    memory.add_system_note("user", "[System check-in] You put this in the background…")
    memory.add_message("assistant", "还在跑，150/170。")
    memory.add_system_note("assistant", "[System record: the user pressed Stop.]")

    restarted = MemoryManager(max_turns=10, conversation_repository=repo)

    # ① 模型侧：一条都不能少 —— 少了它就不知道自己为什么被叫醒
    check([m.content for m in restarted.storage]
          == [m.content for m in memory.storage],
          "⭐ 模型上下文里【四条全在】—— 注记是给它读的，不许因为不上屏就不落盘")

    # ② 可见性跨重启存活（payload_json 往返）
    vis = [m.visible_to_user for m in repo.load_messages(memory.conversation_session_id)]
    check(vis == [True, False, True, False],
          "⭐⭐ 可见性跟着原文一起活过重启（不是内存里的临时标记）", str(vis))

    # ③ 反向：默认必须是"可见"，否则真用户的话会被吃掉
    check(ChatMessage("user", "普通消息").visible_to_user is True,
          "⚠️ 默认可见 —— fail-safe 朝「多显示一句系统注记」错，"
          "不朝「吃掉用户说过的话」错")
    check(_message_from_payload("user", {"content": "老数据"}).visible_to_user is True,
          "⚠️ 老行（payload 里没这个键）照常显示 —— 不需要迁移")

    # ⚠️ 而且**刻意不按正文猜老行**：08-13 之前落盘的注记确实分不出来，
    #    但那批全是测试数据，「重置对话」一下就没了。
    # 📌 **一个只为一次性需求存在的启发式，会永远留在代码里。**
    check(_message_from_payload(
              "user", {"content": "[System check-in] 老行"}).visible_to_user is True,
          "⭐ 老行即使正文长得像注记也照常显示 —— 不留前缀启发式")

    # ④ UI 重放确实**消费**了这个事实
    src = pathlib.Path("app.py").read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_replay_durable_conversation"), None)
    seg = ast.get_source_segment(src, fn) or ""
    code = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("#"))
    check("visible_to_user" in code,
          "⭐⭐ `_replay_durable_conversation` 按 visible_to_user 过滤")
    check("[System" not in code,
          "⚠️ 而且**不是**按 `[System` 前缀过滤 —— 实测里就有一条 "
          "`[Scheduled plan is now due]`，同样是注记却没有那个前缀。"
          "📌 命名约定不是判据。")
    store.close_thread_conn()


def t_no_system_note_still_uses_add_message() -> None:
    """结构守卫：别再有人把系统注记写成 `add_message`。

    ⚠️ **这条守卫的判据有已知边界，写在这里免得后人以为它包全了**：
    它认的是「正文以 `[` 开头的字面量」—— 那是本项目写机器可读注记的实际约定
    （现存 10 条全部如此）。**一条不以 `[` 开头的系统注记它抓不到。**
    📌 真正的判据是「这句话用户在屏幕上见过吗」，而那件事只有写的人知道 ——
       所以它必须在写的时候被**记下来**（选哪个入口），不能靠事后从正文里猜。
       这条断言只是给最常见的那种漏法加一道网。
    """
    print("\n[11] ⭐⭐ 结构守卫：系统注记不许再走 add_message")

    def _heads(node, out):
        a = node
        while isinstance(a, ast.BinOp):
            a = a.left
        if isinstance(a, ast.IfExp):
            _heads(a.body, out); _heads(a.orelse, out); return
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(a.value)
        elif isinstance(a, ast.JoinedStr) and a.values and isinstance(a.values[0], ast.Constant):
            out.append(a.values[0].value)

    bad, good = [], 0
    for f in ("app.py", "core/orchestrator.py"):
        src = pathlib.Path(f).read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("add_message", "add_system_note")
                    and len(n.args) >= 2):
                continue
            hs = []
            _heads(n.args[1], hs)
            if not any(h.lstrip().startswith("[") for h in hs):
                continue
            if n.func.attr == "add_system_note":
                good += 1
            else:
                bad.append(f"{f}:{n.lineno}")

    # ⚠️ 前置：先证明这条扫描真的找得到东西（否则 `not bad` 恒真）
    check(good >= 10,
          f"⚠️ [L5] 前置：扫到了 {good} 条走 add_system_note 的注记（扫描没空跑）")
    check(not bad,
          "⭐⭐ 没有任何「正文以 `[` 开头」的注记还在走 add_message",
          " / ".join(bad) if bad else "")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_current_session_and_order(tmp)
        t_round_trip_preserves_provider_critical_blocks(tmp)
        t_memory_hydrates_durable_projection(tmp)
        t_restart_drops_only_incomplete_tool_transaction(tmp)
        t_explicit_reset_rotates_only_session_boundary(tmp)
        t_ui_replay_reads_full_session_not_bounded_model_projection(tmp)
        t_later_memory_compression_updates_durable_record(tmp)
        t_user_images_survive_restart_in_ui(tmp)
        t_system_notes_reach_model_but_never_the_screen(tmp)
    t_real_app_wires_durable_memory_and_reset()
    t_ui_replay_is_passive_and_uses_durable_messages()
    t_no_system_note_still_uses_add_message()
    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
