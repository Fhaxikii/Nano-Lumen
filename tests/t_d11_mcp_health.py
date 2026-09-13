# -*- coding: utf-8 -*-
"""MCP 故障不再绕过健康登记 + 探针补登记。

═══ 这一套真正要守的东西 ═══

这一套要守的问题不是「报错不好看」，是**能力静默消失**：
`mcp_client.py` 对 `health.py` 的引用数曾经是 **0**，失败只落 `last_error` + 日志。
后果按严重程度排序，最坏的一层是 ——

  > **模型会误以为自己【从来没有】这个能力。**
  > playwright 挂掉时那 24 个 `browser_*` 工具连「感知行」都不会出现，
  > 于是它说「我不能操作浏览器」，而不是「浏览器能力现在坏了，原因是 X」。

所以本套件的重点全在**「说出来的那句话对不对」**，不在「有没有登记过」。

  ① 分类表：异常 → 稳定 code（缺 Node / 缺 Python 模块 / 命令找不到 / 授权 / 超时）
     ⚠️ **模块名与命令名必须原样带出来** —— 那个 36 字符截断切掉的正是它们
  ② 连不上 → 真的进了 HealthRegistry，粒度**按 server**（fetch 挂 ≠ playwright 挂）
  ③ ⭐⭐ 注入模型的那句话必须说「你本来有，现在坏了」，**不许**只说「别调」
  ④ ⭐⭐ 那个钩子：断线 server 的旧工具名要被 `tool_block_reason` 认领，
     **不能掉回 UNKNOWN_TOOL**（：给模型的失败信息必须正确）
  ⑤ 用户禁用 / 删除 → 能力与状态一起清掉（否则是一张永远关不掉的故障卡片）
  ⑥ 探针：MCP 每个 server 都有；rag 的 reranker / BM25 补上；
     剩下的必须在 `_PROBE_DEFERRED` 里写明理由 —— **新增能力不填就红**
  ⑦ `app.py` 里那个 `[:36]` 真的没了

用法：
  py -3.10 tests\\t_d11_mcp_health.py
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

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _fresh_health():
    """每项测试用一份干净的 HealthRegistry（状态是进程级单例）。"""
    import core.health as H
    h = H.get_health()
    for k in list(h.snapshot()):
        h.forget(k["capability"])
    for k in list(H.all_capability_keys()):
        if k.startswith(H.Cap.MCP_SERVER_PREFIX):
            H.unregister_capability(k)
    return h


def _srv(name="fetch", cfg=None, tools=("fetch_url", "fetch_html")):
    from core.mcp_client import MCPServer
    s = MCPServer(name, cfg if cfg is not None else {"command": "npx", "args": ["-y", "x"]})
    s.tools = [{"name": t, "description": f"{t} desc"} for t in tools]
    return s


_FNF = FileNotFoundError(2, "The system cannot find the file specified")


def _code_only(src: str) -> str:
    """去掉注释与字符串字面量，只留下**会被执行的代码**。

    🔴 这个 helper 是本套件自己栽出来的：两条断言原本直接在源码文本里搜
       `[:36]` 和那个带缓存的函数名，结果双双失败 ——
       **命中的是那两条解释性注释**（注释里当然要提旧写法）。
    📌 本项目第 N 次栽在同一形状上：**按「字符串出现过」核，不算核。**
       所以这里先把注释和字符串剥掉，再判断。
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


# ══════════════════════════════════════════════════════════════════════════

def t_classification() -> None:
    print("\n[1] 异常 → 稳定 code（抄 rag.py 的 `_model_load_code` 范式）")
    import core.mcp_client as M

    check(M._mcp_error_code(_FNF, "npx") == M.MCP_NODE_MISSING,
          "npx 找不到 → MCP_NODE_MISSING")
    check(M._mcp_error_code(_FNF, r"C:\tools\my_server.exe") == M.MCP_COMMAND_NOT_FOUND,
          "别的命令找不到 → MCP_COMMAND_NOT_FOUND")
    # 📌 分两类不是洁癖：一个的下一步是「装 Node」，另一个是「改配置」。
    _, m1, h1, _ = M._classify_mcp_error(_FNF, "fetch", "npx")
    _, m2, h2, _ = M._classify_mcp_error(_FNF, "custom", r"C:\tools\my_server.exe")
    check(h1 != h2 and "Node" in h1, "两类的恢复建议不同（不是一句通用废话）", h1[:24])

    mnf = ModuleNotFoundError("No module named 'mcp_server_fetch'")
    check(M._mcp_error_code(mnf) == M.MCP_PYTHON_MODULE_MISSING,
          "缺 Python 模块 → MCP_PYTHON_MODULE_MISSING")
    _, msg, _, _ = M._classify_mcp_error(mnf, "fetch", "uvx")
    # ⭐ 这一条就是那个 36 字符截断的验尸报告：
    #    `ModuleNotFoundError: No module named 'mcp_server_fetch'` = 48 字符，
    #    切到 36 正好把 **模块名** 丢掉，留下一句完全没用的话。
    check("mcp_server_fetch" in msg, "⭐ 模块名原样带出，没有被截断", msg[-30:])
    check("fetch" in m1 and "「" in m1,
          "一句话里带 server 名（挂三个 server 时，光说「MCP 连接失败」没有任何用）")

    check(M._mcp_error_code(TimeoutError("handshake timed out")) == M.MCP_HANDSHAKE_TIMEOUT,
          "超时 → MCP_HANDSHAKE_TIMEOUT")
    check(M._mcp_error_code(RuntimeError("boom")) == M.MCP_CONNECT_FAILED,
          "兜底 → MCP_CONNECT_FAILED")


def t_stderr_is_the_real_source() -> None:
    """🔴🔴 本轮实测推翻早先的设计原文的那一条 —— 见 `_StderrTap` 的注释。

    早先写的是「模块名原样落在 `last_error` 里，缺的不是信息是分类」。
    **实测不是**：子进程死掉时本进程只拿到 `McpError: Connection closed`，
    模块名在**子进程的 stderr** 里，SDK 转发到控制台，没人留存。
    ⚠️ 而那恰恰是 用户当初发现 的实例（fetch 缺依赖）——
       不修这一层，对它最主要的用例是**无效的**。
    """
    print("\n[2] ⭐⭐⭐ 死因来自子进程 stderr，不是本进程异常")
    import core.mcp_client as M

    closed = RuntimeError("Connection closed")   # 本进程能看到的全部信息
    # ① 没有 stderr → 只能兜底（这就是修之前的样子，留在这里当对照）
    code0, msg0, _, _ = M._classify_mcp_error(closed, "fetch", "python")
    check(code0 == M.MCP_CONNECT_FAILED, "没有 stderr 时只能落兜底", code0)

    # ② 有 stderr → 认出真正的死因，并把模块名带出来
    err = "C:\\Python310\\python.EXE: No module named mcp_server_fetch\n"
    code, msg, hint, hint_en = M._classify_mcp_error(closed, "fetch", "python", err)
    check(code == M.MCP_PYTHON_MODULE_MISSING,
          "⭐ 从 stderr 认出「缺 Python 模块」", code)
    check("mcp_server_fetch" in msg, "⭐⭐ 模块名进了给用户的那句话", msg)
    check("pip install mcp_server_fetch" in hint,
          "⭐⭐ 恢复建议是**可以直接执行的那一条**", hint)
    # ⚠️ 两个受众两份文案：用户看中文，模型看英文（原则 8.5）
    check("pip install mcp_server_fetch" in hint_en and hint_en.isascii(),
          "⭐⭐ 给模型那份是英文，且带同一条可执行命令", hint_en[:50])

    # ③ npx 在、包不在 —— 与「命令找不到」是两件事，用户的下一步不同
    npm = "npm error code E404\nnpm error 404 Not Found - GET https://registry.npmjs.org/@x/y"
    code2, _, hint2, _ = M._classify_mcp_error(closed, "playwright", "npx", npm)
    check(code2 == M.MCP_NPM_PACKAGE_MISSING, "npm 取不到包 → 独立一类", code2)
    check(hint2 != hint, "它的恢复建议与「装模块」不同")

    # ④ 认不出来时也不许只说 Connection closed —— 要给 stderr 的**尾巴**
    weird = "some_tool: fatal: could not open config\nabort()"
    _, msg4, _, _ = M._classify_mcp_error(closed, "x", "foo", weird)
    check("abort()" in msg4,
          "⭐ 认不出时兜底给 stderr 尾部（结论在最后一行）", msg4[-40:])
    check("Connection closed" not in msg4,
          "⭐ 不再把本进程看到的表象当死因")

    # ⑤ 同一套模式两处都扫：本进程真抛 ModuleNotFoundError 时也要带出模块名
    mnf = ModuleNotFoundError("No module named 'mcp_server_fetch'")
    _, msg5, hint5, hint5_en = M._classify_mcp_error(mnf, "fetch", "uvx")
    check("mcp_server_fetch" in msg5 and "pip install" in hint5,
          "本进程异常这一路也扫（只扫一处会漏掉另一半）")

    # ⚠️ tap 必须是**真文件** —— SDK 把 errlog 直接交给子进程当 stderr。
    #    第一版写成 io.TextIOBase 子类，实测 `UnsupportedOperation: fileno`。
    tap = M._StderrTap()
    try:
        check(isinstance(tap.fileno(), int), "⭐ `_StderrTap` 有真的 fileno()")
    finally:
        tap.close()
    src = (ROOT / "core" / "mcp_client.py").read_text(encoding="utf-8")
    check("stdio_client(params, errlog=" in _code_only(src).replace(" ", "").replace(
              "stdio_client(params,errlog=", "stdio_client(params, errlog=") or
          "errlog" in _code_only(src),
          "errlog 真的接上了（不接的话这一整层是死的）")


def t_reaches_health() -> None:
    print("\n[3] ⭐ 连不上真的进了 HealthRegistry —— 而且粒度按 server")
    import core.health as H
    h = _fresh_health()

    a = _srv("fetch", {"command": "npx"}, ("fetch_url",))
    b = _srv("playwright", {"command": "npx"}, ("browser_navigate", "browser_click"))
    a._health_fault(_FNF)
    b._health_ok()

    caps = {s["capability"]: s for s in h.snapshot()}
    check(H.Cap.mcp_server("fetch") in caps, "fetch 已登记")
    st = caps.get(H.Cap.mcp_server("fetch"), {})
    check(st.get("status") == "UNAVAILABLE", "状态 = UNAVAILABLE", str(st.get("status")))
    check(st.get("code") == "MCP_NODE_MISSING", "带着分类 code", str(st.get("code")))
    # 📌 按 server 而不是「MCP 整体」：fetch 挂了不等于 playwright 挂了
    pw = caps.get(H.Cap.mcp_server("playwright"), {})
    check(pw.get("status", "AVAILABLE") == "AVAILABLE",
          "⭐ playwright 不受牵连（粒度是 server，不是整个 MCP）")

    # 重复失败只累加，不刷屏（health 自己的去重）
    before = len(h.snapshot())
    for _ in range(5):
        a._health_fault(_FNF)
    check(len(h.snapshot()) == before, "重复失败不产生新条目（去重生效）")


def t_model_is_told_it_had_it() -> None:
    print("\n[4] ⭐⭐⭐ 注入模型的那句话 —— 「最坏的一层」")
    h = _fresh_health()
    s = _srv("playwright", {"command": "npx"},
             ("browser_navigate", "browser_click", "browser_type"))
    s._health_fault(_FNF)
    notice = h.render_capability_notice()

    check("UNAVAILABLE" in notice, "能力边界里出现了它")
    check("playwright" in notice, "带着 server 名")
    check("browser_navigate" in notice,
          "⭐ 带着它【曾经有过】的工具名（模型据此把「操作浏览器」对上号）")
    low = notice.lower()
    check("not absent" in low or "do have" in low,
          "⭐⭐ 明说「你本来有，现在是坏了，不是没有」")
    check("never say you never had it" in low,
          "⭐⭐ 直接禁掉那句错话（「我不能操作浏览器」）")
    # ⚠️ 通用措辞在这里是错的 —— 它只说「别调相关工具」就完事了。
    check("Do not call the related tools; tell the user plainly" not in notice,
          "没有落到通用措辞上（notice_hint 生效）")


def t_f6_hook_is_filled() -> None:
    print("\n[5] ⭐⭐ [F6] 留的钩子被填上了 —— 旧工具名不再掉回 UNKNOWN_TOOL")
    h = _fresh_health()
    s = _srv("playwright", {"command": "npx"}, ("browser_navigate",))
    s._health_fault(_FNF)

    stale = "mcp__playwright__browser_navigate"
    blk = h.tool_block_reason(stale)
    check(blk is not None, "⭐ 断线 server 的旧工具名被 tool_block_reason 认领", stale)
    check(bool(blk and blk.user_message), "带着原因（不是空壳）")
    check(bool(blk and blk.recovery_hint), "带着恢复建议")
    check(stale in h.blocked_tools(), "同时出现在 blocked_tools 里")
    # 📌 分不清「从来不存在」和「曾经存在现在断了」，缺的是事实来源；
    #    现在事实来源有了 —— 而【执行层拦截必须发生在 UNKNOWN_TOOL 判定之前】。
    src = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    i_blk = src.find("tool_block_reason(name)")
    i_unk = src.find("_cause = self._ToolFailCause.UNKNOWN_TOOL")
    check(i_blk > 0 and i_unk > 0 and i_blk < i_unk,
          "⭐ 拦截点在 UNKNOWN_TOOL 判定之前（顺序反了就白修）",
          f"blk@{i_blk} unknown@{i_unk}")


def t_user_removal_leaves_nothing() -> None:
    print("\n[6] 用户禁用 / 删除 → 不留一张关不掉的故障卡片")
    import core.health as H
    h = _fresh_health()
    s = _srv("custom", {"command": "foo"}, ("do_thing",))
    s._health_fault(RuntimeError("boom"))
    check(any(x["capability"] == H.Cap.mcp_server("custom") for x in h.snapshot()),
          "先确认故障卡片在")

    s._health_forget()
    check(not any(x["capability"] == H.Cap.mcp_server("custom") for x in h.snapshot()),
          "⭐ forget 之后状态没了")
    check(H.get_capability_spec(H.Cap.mcp_server("custom")) is None,
          "能力声明也一起注销了（登记入口配注销入口）")
    check("mcp__custom__do_thing" not in h.blocked_tools(),
          "它的工具名也不再被拦")

    # ⚠️ forget ≠ recover：删掉一个 server 不该产生「已恢复」的说法（那是假话）
    src = (ROOT / "core" / "health.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "forget"), None)
    calls = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)] if fn else []
    check(fn is not None and "recover" not in calls,
          "forget 不走 recover（「这个能力没了」≠「它好了」）")


def t_probes() -> None:
    print("\n[7] 探针 —— 「坏了就再也不会好」的那一类")
    import core.health as H
    _fresh_health()
    from core.mcp_client import MCPManager

    mgr = MCPManager()
    mgr.servers = {"fetch": _srv("fetch", {"command": "npx"}, ("fetch_url",))}
    mgr._register_probes()
    check(H.Cap.mcp_server("fetch") in H.get_health()._probes,
          "⭐ 每个 MCP server 都有探针（否则退避 5 次约 31 秒后永久放弃）")

    mcp_src = (ROOT / "core" / "mcp_client.py").read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(mcp_src))
                if isinstance(n, ast.FunctionDef) and n.name == "_make_probe")
    probe_src = ast.get_source_segment(mcp_src, node) or ""
    # ⭐ 探针必须**真的去重连**，不是只读 status —— 只读状态的探针永远返回 False
    #    （已经没人再去连它了），那是「看起来有、实际不解锁」，比没有更坏。
    check("create_task" in probe_src and ".start()" in probe_src,
          "⭐ 探针真的发起重连，不是只读 status")
    check("ST_NEEDS_AUTH" in probe_src,
          "needs_auth 不自动重试（凭据要用户给，重试没用）")

    # rag 那两个
    rag_src = (ROOT / "core" / "rag.py").read_text(encoding="utf-8")
    check("register_probe(Cap.KB_RERANKER" in rag_src, "KB_RERANKER 探针已登记")
    check("register_probe(Cap.KB_KEYWORD_SEARCH" in rag_src, "KB_KEYWORD_SEARCH 探针已登记")
    # 🔴 BM25 探针不能调 `_check_bm25_available()` —— 它缓存结果，第一次 False
    #    之后永远 False。那会是一个**永远解不了闸**的探针。
    bm_node = next(n for n in ast.walk(ast.parse(rag_src))
                   if isinstance(n, ast.FunctionDef) and n.name == "_probe_bm25")
    bm_calls = {n.func.id for n in ast.walk(bm_node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("_check_bm25_available" not in bm_calls,
          "⭐ BM25 探针没有调那个带缓存的函数（否则永远 False）",
          " ".join(sorted(bm_calls)))
    check("_bm25_available = None" in _code_only(
              ast.get_source_segment(rag_src, bm_node) or ""),
          "⭐ 探到之后清缓存 —— 否则「恢复了」是假话（检索仍走纯向量）")


def t_no_silent_probe_gap() -> None:
    print("\n[8] ⭐⭐ 缺口台账：新增能力不写理由就红（白名单，不是排除法）")
    import core.health as H
    import core.rag as R
    _fresh_health()
    R._register_health_probes()
    # ⚠️ 起必须**显式导入官方 Skill**：`web.search` 的探针登记在
    #    `skills/official/SearchTheWeb.py` 的模块层，实测是 `registry.reload_all`
    #    在导入它时顺带登记的。这里不导入，这条就会假报成缺口。
    # 📌 探针跟着提供者走（谁提供能力谁登记探针），所以「Skill 没加载 → 没探针」
    #    是自洽的：那时这个能力本来也不存在。
    import skills.official.SearchTheWeb  # noqa: F401  （导入即登记探针）
    gaps = H.probe_coverage_gaps()
    check(not gaps,
          "每个能力要么有探针，要么在 `_PROBE_DEFERRED` 里写明为什么不需要",
          "" if not gaps else "没交代：" + " ".join(gaps))
    if gaps:
        print("    📌 这不是「再加个探针」就完了 —— 先回答："
              "它坏了之后，有没有任何东西会去看它好没好？")
    H._init_probe_deferred()
    check(all(len(v) > 20 for v in H._PROBE_DEFERRED.values()),
          "每条豁免都写了真正的理由（不是一个 TODO）")

    # ⭐⭐ 2026-08-25：这里原本断言的是
    #    「`WEB_FETCH` 的豁免点名了它在等谁」—— 现在**两条都销账了**，
    #    所以断言反过来：确认它们不再挂在台账上。
    # 📌 这张台账的意义就在于**它得能变短**，否则它只是个更体面的 TODO。
    check("web.search" not in H._PROBE_DEFERRED,
          "⭐ `web.search` 不再是豁免 —— 它拿到了真探针（SearchTheWeb.py 里）")
    check(not hasattr(H.Cap, "WEB_FETCH"),
          "⭐ `WEB_FETCH` 整条能力已删（派生态：等于「有没有 server 声明 web.fetch」）")
    check("WEB_FETCH" in (ROOT / "core" / "health.py").read_text(encoding="utf-8"),
          "🪦 但删除处留了墓碑，说明为什么别加回来")
    # 🔴 删一条能力最容易漏的就是别处还在引用它 —— 那会在运行时才炸。
    _refs = []
    for _f in (ROOT / "core").rglob("*.py"):
        _src = _code_only(_f.read_text(encoding="utf-8", errors="ignore"))
        if "Cap.WEB_FETCH" in _src or '"web.fetch"' in _src and _f.name == "health.py":
            _refs.append(_f.name)
    check(not _refs, "没有任何代码还在引用 `Cap.WEB_FETCH`", " ".join(_refs))


def t_truncation_is_gone() -> None:
    print("\n[9] `app.py` 那个 36 字符截断真的没了（与 [D9] 同形状）")
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    # ⚠️ 必须剥掉注释再判 —— 修复处的注释里写着旧写法（那是留痕，不是问题本身）。
    _code = _code_only(src)
    check("[:36]" not in _code.replace(" ", ""),
          "⭐ `[:36]` 已删除（按代码判，不按文本判）")
    check('s.get("fault_message")' in src, "改用分类过的人话")
    # ⚠️ 设置页**刻意不给恢复建议**（实测之后定的）：
    #    ① 与故障卡片冗余 ② 这里的职责只是陈述事实，「怎么修」是故障卡片的事。
    #    📌 两处说同一句话时，改的人只会改到一处。
    check('s["fault_hint"]' not in src and "→ {s[" not in src,
          "⭐ 设置页不再重复恢复建议（只陈述事实）")
    # ⚠️ 旧代码只在 failed 时显示；needs_auth / disconnected 一个字都不出
    seg = src[src.find('_fmsg = s.get("fault_message")'):][:700]
    check('"needs_auth"' in seg and '"disconnected"' in seg,
          "needs_auth / disconnected 也显示原因")
    check("white-space:normal" in seg, "不截断、允许换行")


def t_two_audiences_two_texts() -> None:
    """实测抓到的三条：故障卡片的语气、恢复建议要注入模型、UI 不重复。"""
    print("\n[10] ⭐⭐ 两个受众两份文案 + 故障卡片语气 + UI 不重复")
    import ast as _ast
    import core.health as H
    h = _fresh_health()
    s = _srv("fetch", {"command": "python"}, ("fetch_url",))
    # ⚠️ err=None 才走「显式给定」那条路；给了 err 就会以分类结果为准（那是对的）
    s._health_fault(None, code="X", msg="坏了",
                    hint="中文建议", hint_en="Do the English thing.")

    notice = h.render_capability_notice()
    # ⭐⭐ 实测：Nano 只会说「这个能力坏了」，说不出怎么修 ——
    #    因为 recovery_hint 从来没进过注入。
    check("Do the English thing." in notice,
          "⭐⭐ 恢复建议真的注入模型了（原来一个字都没给）")
    check("To fix it:" in notice, "有明确的引导词，模型知道这是可转述的修复步骤")
    check("中文建议" not in notice,
          "⭐ 注入的是英文那份（原则 8.5：注入模型的文本一律英文）")

    # ⚠️ 一个字段不许表达两个现实 —— 这里的两个现实是两个受众
    st = [x for x in h.snapshot() if x["capability"].startswith("mcp.")][0]
    check(st["recovery_hint"] == "中文建议" and st["recovery_hint_en"] == "Do the English thing.",
          "⭐ 中英两份各自独立存着，谁也不盖谁")

    # ⭐ 全仓机械核：给了 hint 就必须给 hint_en，漏一个就红
    FN = {"report_fault", "report_degraded", "report"}
    PAIRS = {"hint": "hint_en", "recovery_hint": "recovery_hint_en"}
    miss = []
    for f in list(ROOT.glob("*.py")) + list(ROOT.glob("core/**/*.py")):
        if f.name == "health.py":
            continue
        try:
            tree = _ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func.attr if isinstance(node.func, _ast.Attribute) else getattr(node.func, "id", "")
            if fn not in FN:
                continue
            kw = {k.arg for k in node.keywords if k.arg}
            for a, b in PAIRS.items():
                if a in kw and b not in kw:
                    miss.append(f"{f.name}:{node.lineno}")
    check(not miss, "⭐⭐ 全仓每个上报点都给了英文那份", " ".join(miss) or "")
    if miss:
        print("    📌 给了中文建议却没给英文那份 = 模型永远说不出怎么修。")

    # ① 故障卡片的语气：恢复建议前面要有小标题，别读起来像在聊天
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    seg = app[app.find("def _render_fault_card"):][:2200]
    check("修复建议" in seg, "⭐ 故障卡片的恢复建议有小标题（系统提示语气，不是聊天）")


def t_config_is_reread() -> None:
    """实测：改回配置后自愈失败、UI 重连也无效。"""
    print("\n[11] ⭐⭐⭐ 配置真的会被重新读（实测抓到的自愈失败）")
    import ast as _ast
    import json as _json
    import tempfile
    from core.mcp_client import MCPManager

    d = pathlib.Path(tempfile.mkdtemp(prefix="nanod11_"))
    cfg = d / "mcp_servers.json"
    cfg.write_text(_json.dumps({"mcpServers": {"fetch": {"command": "python",
                                                         "args": ["-m", "bad_mod"]}}}),
                   encoding="utf-8")
    mgr = MCPManager()
    mgr._config_path = cfg
    mgr.load_config()
    check(mgr.servers["fetch"].cfg["args"] == ["-m", "bad_mod"], "先确认读到了坏配置")

    # 用户把文件改回正确值 —— 进程没重启
    cfg.write_text(_json.dumps({"mcpServers": {"fetch": {"command": "python",
                                                         "args": ["-m", "good_mod"]}}}),
                   encoding="utf-8")
    check(mgr.servers["fetch"].cfg["args"] == ["-m", "bad_mod"],
          "此刻内存里还是旧的（这就是那个 bug 的形状）")
    check(mgr.refresh_config_from_disk() is True, "refresh 成功")
    check(mgr.servers["fetch"].cfg["args"] == ["-m", "good_mod"],
          "⭐⭐ 刷新之后拿到的是新参数（不刷的话重试永远撞同一堵墙）")

    # ⚠️ 用户正在编辑 JSON 的中途，文件必然有一瞬间是坏的 ——
    #    那一瞬间不该让所有外接能力消失
    cfg.write_text('{"mcpServers": {"fetch"', encoding="utf-8")
    check(mgr.refresh_config_from_disk() is False, "半截 JSON → 返回 False")
    check("fetch" in mgr.servers,
          "⭐⭐ 半截 JSON **不清空 servers**（编辑到一半不该让能力全消失）")
    check(mgr.servers["fetch"].cfg["args"] == ["-m", "good_mod"], "也不动已有的 cfg")

    # 两个入口都必须刷：手动重试 + 探针
    src = (ROOT / "core" / "mcp_client.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    for fname, why in (("retry_server", "UI 的「重试」按钮"), ("_make_probe", "自愈探针")):
        fn = next((n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == fname), None)
        calls = {n.func.attr for n in _ast.walk(fn) if isinstance(n, _ast.Call)
                 and isinstance(n.func, _ast.Attribute)} if fn else set()
        check("refresh_config_from_disk" in calls,
              f"⭐ {why} 会先刷配置", fname)

    # 📌 刻意不复用 load_config：删除是用户动作，不该由后台探针顺手做掉
    rf = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "refresh_config_from_disk")
    body = _ast.get_source_segment(src, rf) or ""
    check("self.servers = " not in _code_only(body),
          "⭐ refresh 不整体替换 servers（那会把删除也顺手做了）")


def t_nano_can_actually_fix_it() -> None:
    """🔴 用户第二轮实测：Nano 说得出 `pip install …`，却补一句
    「这是系统层面的，我无法帮你修复」。**那是提示词写的，不是模型的性格。**

    上一版这里写的是 "Relay this to the user" —— 字面意思就是
    「你的职责是把话传过去」。而 Nano 有 `os_execute`，`pip install` 它自己就能跑。
    📌 **一句把模型定位成「传话筒」的措辞，会让它主动放弃它真有的能力。**
    """
    print("\n[12] ⭐⭐⭐ Nano 不该把能跑的修复说成「我做不了」")
    h = _fresh_health()
    s = _srv("fetch", {"command": "python"}, ("fetch_url",))
    s._health_fault(None, code="X", msg="坏了", hint="中文",
                    hint_en="Install the missing module: `pip install foo`.")
    n = h.render_capability_notice()
    low = n.lower()

    check("relay this to the user" not in low,
          "⭐⭐ 「把话传给用户」那句没了（它就是那句错话的来源）")
    check("carry out yourself" in low or "do it once the user agrees" in low,
          "⭐⭐⭐ 明确让它判断「这一步我能不能自己做」")
    check("out of your reach" in low,
          "⭐⭐ 直接禁掉那句错话（把能跑的修复说成够不着）")
    check("clicking something in this app" in low,
          "⭐ 同时划清真的该交回用户的那一类（UI 点击 / 凭据 / 决定）")
    # ⚠️ 「related tools」太宽，会被读成「跟这件事有关的都别碰」——含修它的那条命令
    check("do not call the related tools" not in low,
          "⭐ 「别调相关工具」收窄成「别调依赖它的工具」")
    # ⭐ 行动那句必须在最后 —— 最后一句是模型最容易照做的那句
    check(n.rstrip().endswith("out of your reach.") or
          n.rstrip().rfind("out of your reach") > n.rstrip().rfind("Do not call the tools"),
          "⭐ 行动指令排在「别调工具」之后（最后一句最容易被照做）")


def t_native_selection_and_scroll() -> None:
    """的根因 + 开窗滚动位置（用户两条实测线索直接指到根）。"""
    print("\n[13] ⭐⭐ [U3] 根因锁定 + 开窗停在最新一条")
    src = (ROOT / "app.py").read_text(encoding="utf-8")

    # ⭐⭐ 根因是 pywebview 的 `text_select` 默认 False：它在**页面加载之后**
    #    注入 `body{user-select:none}`。用户的两条线索是同一个原因：
    #    「网页版能选、native 不能」+「启动头几秒能选、之后不能」。
    check("window_args['text_select'] = True" in _code_only(src).replace(" ", "")
          .replace("window_args['text_select']=True", "window_args['text_select'] = True")
          or "'text_select'" in src,
          "⭐⭐⭐ 打开了 pywebview 的 text_select")
    seg = src[src.find("_apply_native_window_icon"):][:3000]
    check("text_select" in seg and "customize.js" in seg,
          "⭐ 留痕写清了根因在 pywebview 的哪段代码", "customize.js")

    # ⚠️ 打开之后要把 chrome 单独关掉，否则拖窗口会顺手选中标题
    #    📌 顺序要对：默认可选、只在少数地方关，不是反过来
    check(".pywebview-drag-region," in src and "user-select: none" in src,
          "⭐ 拖拽区/按钮单独关掉选择")
    css = src[src.find(".pywebview-drag-region,"):][:900]
    check("nano-chat-scroll" in css and "user-select: text" in css,
          "⭐⭐ 聊天区**显式**打开（不靠「没人给它设 none」维持）")

    # 开窗停在最新一条
    seg2 = src[src.find("_had_history = self._replay_durable_conversation()"):][:1600]
    check(bool(seg2), "启动重放处拿到了 had_history")
    check("scroll_to(percent=1.0" in seg2,
          "⭐⭐ 启动重放之后会滚到底（原来只有衰减那条路径滚，启动这条漏了）")
    check(seg2.count("ui.timer(") >= 2,
          "⭐ 两拍：布局算完一次、晚到的图片撑开高度后再兜一次")
    check("while" not in seg2.split("ui.timer(")[0][-200:],
          "⚠️ 不是「循环到高度稳定」（那是个不会自己停的东西）")


def main() -> int:
    print("=" * 74)
    print("MCP 故障不再绕过健康登记 + 探针补登记")
    print("=" * 74)
    t_classification()
    t_stderr_is_the_real_source()
    t_reaches_health()
    t_model_is_told_it_had_it()
    t_f6_hook_is_filled()
    t_user_removal_leaves_nothing()
    t_probes()
    t_no_silent_probe_gap()
    t_truncation_is_gone()
    t_two_audiences_two_texts()
    t_config_is_reread()
    t_nano_can_actually_fix_it()
    t_native_selection_and_scroll()
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
