# tests/cases/t_c1_websearch.py
"""网络搜索能力 —— 找 / 读 / 兜底 / 监控卡三级。

**全部离线**：不发一个真实请求。搜索引擎的可用性是实测验的事，
这里验的是**我们自己的判断逻辑**，而那部分不该依赖网络才能测。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import module_text  # noqa: E402

_passed = 0
_failed = 0


def check(cond, label, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}" + (f"   [{detail}]" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"   [{detail}]" if detail else ""))


def _code_only(src: str) -> str:
    """剥掉**注释** —— 断言不能被留痕本身喂饱。

    ⚠️ `ast.unparse` **保留 docstring**（它是 AST 里的字符串表达式，不是注释）。
       所以对着它数字符串字面量时，docstring 里的那份仍然算数。
    """
    try:
        return ast.unparse(ast.parse(src))
    except SyntaxError:
        return src


SKILL_PATH = ROOT / "skills" / "official" / "SearchTheWeb.py"


# ══════════════════════════════════════════════════════════════════════════
def t_name_is_not_websearch() -> None:
    print("\n[1] 🔴🔴 这个 Skill 不许叫 `WebSearch`（撞名 = 模型发空参数）")
    import skills.official.SearchTheWeb as M

    m = M.SearchTheWeb().get_manifest()
    spec = M.SearchTheWeb().get_spec()
    # 🔴 2026-08-25 实测单变量 A/B：name="WebSearch" 时模型**每次**发 input={}，
    #    只改 name（描述与 schema 一字未动）立刻正常。
    #    成因：撞了 Claude 生态里那个同名 **server 端** 工具（客户端不传参）。
    # 📌 模型对一个它「认识」的名字，会用记忆里的调用方式，而不是你给的 schema。
    for label, got in (("manifest", m.get("name")),
                       ("spec", spec.name),
                       ("类名", M.SearchTheWeb.__name__),
                       ("文件名", SKILL_PATH.stem)):
        check(got == "SearchTheWeb",
              f"⭐⭐⭐ {label} 是 SearchTheWeb，不是 WebSearch", str(got))

    # ⚠️ 本仓的坑：`registry.skills` 按**类名**索引、`tools_manifest` 按 **manifest 名**
    #    索引。两者不一致 → 模型看得见这个工具、调用时却报 UNKNOWN_TOOL。
    check(M.SearchTheWeb.__name__ == m.get("name") == spec.name == SKILL_PATH.stem,
          "⭐⭐ 类名 / manifest 名 / spec 名 / 文件名**四者同名**"
          "（不一致会静默变成 UNKNOWN_TOOL）")

    check("query" in ((m.get("parameters") or {}).get("required") or []),
          "query 是必填（它就是当初暴露撞名的那个参数）")


def t_five_page_states() -> None:
    print("\n[2] ⭐⭐ 五种页面形态各自判对（这是本项最容易错的地方）")
    from skills.official.SearchTheWeb import (
        SearchTheWeb, PAGE_OK, PAGE_EMPTY, PAGE_DEGRADED, PAGE_CHANGED, PAGE_ALIEN)

    CARD = ('<section class="card search-result"><div class="url">'
            '<a href="https://x.test/a">u</a></div><h2><a class="title" '
            'href="https://x.test/a">T</a></h2><p class="description">D</p></section>')
    T = '<title>Marginalia Search</title>'
    cases = [
        ("正常",           f'{T}<section id="results">{CARD}</section>',                  PAGE_OK),
        ("真空",           f'{T}<section id="results"></section>',                        PAGE_EMPTY),
        ("降级·无容器",     f'{T}<body>nothing here</body>',                              PAGE_DEGRADED),
        ("改版·无容器",     f'{T}<div class="search-result">x</div>',                     PAGE_CHANGED),
        ("改版·容器在形状变", f'{T}<section id="results"><div class="search-result">x</div></section>',
                                                                                          PAGE_CHANGED),
        ("外星页",         '<title>403 Forbidden</title><h1>blocked</h1>',                PAGE_ALIEN),
    ]
    for label, html, want in cases:
        _res, kind = SearchTheWeb._parse(html, 8)
        check(kind == want, f"{label} → {want}", kind)

    _res, _ = SearchTheWeb._parse(f'{T}<section id="results">{CARD}</section>', 8)
    check(_res and _res[0]["url"] == "https://x.test/a" and _res[0]["title"] == "T",
          "正常页真的抽出了 title/url/snippet")


def t_absence_is_not_evidence() -> None:
    print("\n[3] 🔴🔴 「容器不在」**不许**单独判成改版（第一版就是这么写的）")
    from skills.official.SearchTheWeb import SearchTheWeb, PAGE_DEGRADED, PAGE_CHANGED

    T = '<title>Marginalia Search</title>'
    _r, k1 = SearchTheWeb._parse(f'{T}<body>hiccup</body>', 8)
    check(k1 == PAGE_DEGRADED,
          "⭐⭐⭐ 没有容器、也没有卡片标记 → DEGRADED（可恢复），不是 CHANGED", k1)
    # 📌 第一版把它判成 CHANGED → report_fault → 而探针**故意**不救 CHANGED
    #    ⇒ 抖动一次 = 这个能力被永久钉死到重启。
    #    这正是本仓记过的「断网 5 秒把网络能力钉死到重启」那个形状。
    _r, k2 = SearchTheWeb._parse(f'{T}<div class="search-result">x</div>', 8)
    check(k2 == PAGE_CHANGED,
          "⭐ 而有卡片标记却抽不出来 → CHANGED（正证据，不是缺席）", k2)

    src = _code_only(SKILL_PATH.read_text(encoding="utf-8"))
    check("report_degraded" in src,
          "⭐ 抖动走 `report_degraded`（只进监控、不弹故障卡、探针能救）")
    # ⚠️ 别用字符串计数：`_code_only` 不剥 docstring（那里有一处反引号写法），
    #    而 `ast.unparse` 又会把引号风格统一掉。两个坑各踩了一次。
    # ⭐ 直接走 AST 数**字符串常量**，跟引号风格和注释都无关。
    _tree = ast.parse(SKILL_PATH.read_text(encoding="utf-8"))
    _lit = sum(1 for n in ast.walk(_tree)
               if isinstance(n, ast.Constant) and n.value == "search_markup_changed")
    check(_lit == 2,
          "`search_markup_changed` 作为常量只有两处：一处上报 + 一处探针拒绝恢复", str(_lit))


def t_probe_refuses_to_fake_recovery() -> None:
    print("\n[4] ⭐⭐ 探针不许在「改版」时宣布恢复")
    import skills.official.SearchTheWeb as M

    src = M.SearchTheWeb  # noqa: F841  （只为确保模块已导入、探针已登记）
    fn = _code_only(pathlib.Path(SKILL_PATH).read_text(encoding="utf-8"))
    check("search_markup_changed" in fn and "_web_search_probe" in fn,
          "探针里点名了 `search_markup_changed`")
    # 🔴 探针探的是首页可达性，而改版之后首页照样 200。
    # 📌 一个能在能力还坏着的时候宣布恢复的探针，比没有探针更坏：
    #    没有探针只是没人来救，误报恢复是**主动把故障卡片关掉**。
    from core.health import get_health, Cap, Status, Severity
    h = get_health()
    # ⚠️ 先断言登记、再造状态。`health.forget()` **会连探针一起 pop 掉**
    #    （它的语义是「这个能力不在这台机器上了」），拿它当「清状态」用
    #    会把探针一起清掉，还会毒到后面的用例。清状态要用 `recover()`。
    check(Cap.WEB_SEARCH in h._probes, "探针确实登记了（导入 Skill 即登记）")
    h.report(Cap.WEB_SEARCH, status=Status.UNAVAILABLE, severity=Severity.ERROR,
             code="search_markup_changed", user_message="x")
    check(M._web_search_probe() is False,
          "⭐⭐⭐ 处在 markup_changed 时，探针返回 False（不许假恢复）")
    h.recover(Cap.WEB_SEARCH)
    check(Cap.WEB_SEARCH in h._probes, "⭐ 清完状态之后探针还在（用的是 recover 不是 forget）")


def t_capability_registry() -> None:
    print("\n[5] ⭐ 能力登记：WEB_SEARCH 销账、WEB_FETCH 整条删掉")
    import core.health as H
    import skills.official.SearchTheWeb  # noqa: F401

    H._init_probe_deferred()
    check("web.search" not in H._PROBE_DEFERRED,
          "⭐ `web.search` 不再挂在缺口台账上（它有真探针了）")
    check(not hasattr(H.Cap, "WEB_FETCH"),
          "⭐ `Cap.WEB_FETCH` 已删 —— 它完全派生自「有没有 server 声明 web.fetch」")
    check("WEB_FETCH" in module_text("core.health"),
          "🪦 删除处留了墓碑，写清为什么别加回来")
    check(H.get_capability_spec(H.Cap.WEB_SEARCH).monitor_card == "net",
          "WEB_SEARCH 归「互联网检索」卡")


def t_provides_replaces_keyword_guessing() -> None:
    print("\n[6] 🔴 联网能力靠**配置声明**，不靠关键词猜")
    from core.mcp_client import MCPServer, MCPManager

    src = _code_only(module_text("core.mcp_client"))
    # 🔴 旧实现：拿 name+command+args+url+所有工具名描述拼 blob 去撞 11 个关键词。
    #    已明确问题：playwright 那 24 个工具里必然有含 browse/url 的，
    #    所以只要它连上，「互联网检索」就永远绿 —— 而那 ≠ 能搜索。
    # 📌 一个永远为真的判断，比没有这个判断更坏：它看起来在检查。
    check("_WEB_KEYWORDS" not in src, "⭐⭐⭐ 关键词表 `_WEB_KEYWORDS` 已全仓删除")
    check("has_web_capability" not in src, "⭐ 二值的 `has_web_capability()` 已删除")

    s = MCPServer("x", {"provides": ["web.fetch"]})
    check(s.declared_caps() == frozenset({"web.fetch"}), "`provides` 读得出来")
    check(s._is_web_like() is True, "声明了 web.* → 归 net 卡")
    check(MCPServer("y", {}).declared_caps() == frozenset(),
          "⭐ 没声明 `provides` 的第三方 server 不认领任何能力（安全的默认）")
    check(MCPServer("y", {})._is_web_like() is False,
          "⭐ 不认领 → 不归 net 卡（宁可少认领，也不要在能力没了时还亮绿灯）")
    check(MCPServer("z", {"provides": "web.fetch"}).declared_caps() == frozenset({"web.fetch"}),
          "写成字符串而不是数组也认（配置是人手写的）")

    cfg = json.loads((ROOT / "config" / "mcp_servers.json").read_text(encoding="utf-8"))
    servers = cfg["mcpServers"]
    check(servers["fetch"].get("provides") == ["web.fetch"], "内置 fetch 声明了 web.fetch")
    check(servers["playwright"].get("provides") == ["web.fetch"],
          "⭐ playwright 也声明 web.fetch —— 它确实读得了网页，fetch 挂了它顶得上")
    # 🔴🔴 判据是「顶不顶得掉那一层的职责」，不是「它有没有用」。
    for _n in ("context7", "microsoft-learn"):
        check(not servers[_n].get("provides"),
              f"🔴 {_n} **故意不声明** web.* —— 它只能查库文档，答不了「这报错怎么解」；"
              f"算进「找」会让卡在真搜不了时仍显示 ONLINE")


def t_web_status_three_tier() -> None:
    print("\n[7] ⭐⭐ 「互联网检索」是三级，不是绿/灰二值")
    from core.mcp_client import MCPManager, MCPServer, ST_CONNECTED, ST_FAILED
    from core.health import get_health, Cap, Status, Severity

    m = MCPManager.__new__(MCPManager)
    m.servers = {}

    def _srv(name, provides, status):
        s = MCPServer(name, {"provides": provides})
        s.status = status
        return s

    h = get_health()
    h.recover(Cap.WEB_SEARCH)

    m.servers = {"fetch": _srv("fetch", ["web.fetch"], ST_CONNECTED)}
    check(m.web_status() == MCPManager.WEB_ONLINE, "找 ✅ + 读 ✅ → ONLINE", m.web_status())

    m.servers = {"fetch": _srv("fetch", ["web.fetch"], ST_FAILED)}
    check(m.web_status() == MCPManager.WEB_LIMITED,
          "⭐ 读挂了（能搜不能读）→ LIMITED，不是 OFFLINE", m.web_status())

    h.report(Cap.WEB_SEARCH, status=Status.UNAVAILABLE, severity=Severity.ERROR,
             code="search_http_error", user_message="x")
    m.servers = {"fetch": _srv("fetch", ["web.fetch"], ST_CONNECTED)}
    check(m.web_status() == MCPManager.WEB_LIMITED,
          "🔴 找挂了（只能读用户给的 URL）→ LIMITED", m.web_status())
    m.servers = {"fetch": _srv("fetch", ["web.fetch"], ST_FAILED)}
    check(m.web_status() == MCPManager.WEB_OFFLINE, "两层全挂 → OFFLINE", m.web_status())
    h.recover(Cap.WEB_SEARCH)

    # ⚠️ 从没跑过 → 当作可用。反过来会让全新安装一开机就报故障，而那时什么都没坏。
    m.servers = {"fetch": _srv("fetch", ["web.fetch"], ST_CONNECTED)}
    check(m.can_search() is True,
          "⭐ WEB_SEARCH 从没上报过时算**可用**（全新安装不该一开机就红）")

    # 🔴🔴 doc-MCP 不许顶「找」那一层
    m.servers = {"context7": _srv("context7", [], ST_CONNECTED),
                 "microsoft-learn": _srv("microsoft-learn", [], ST_CONNECTED)}
    h.report(Cap.WEB_SEARCH, status=Status.UNAVAILABLE, severity=Severity.ERROR,
             code="search_http_error", user_message="x")
    check(m.web_status() == MCPManager.WEB_OFFLINE,
          "🔴🔴 只有 context7 + microsoft-learn 连着 → 仍是 OFFLINE"
          "（它们查得了库文档，但顶不掉「找」和「读」）", m.web_status())
    h.recover(Cap.WEB_SEARCH)


def t_ui_card_and_avatar() -> None:
    print("\n[8] ⭐ UI 两处：三态状态卡（可用 / 降级 / 不可用）+ 考拉的上网动画")
    app_src = module_text("app")
    check("_NET_COLORS" in app_src and app_src.count('"LIMITED"') >= 1,
          "监控卡有三态配色（LIMITED 用琥珀）")
    check("web_status()" in _code_only(app_src),
          "⭐ 卡片读 `web_status()`，**不在 UI 侧重新判一次**（判据只能有一处）")
    # ⚠️ 这条修的是个不会报错的 bug：动画不亮不会抛异常，没人会报。
    koala_src = module_text("nano_koala")
    check("'SearchTheWeb'" in koala_src,
          "⭐ 考拉头像的上网动画名单里有 SearchTheWeb（漏掉 = 上网时 wifi 不亮，"
          "而那是个不会报错的 bug）")
    check('skill_ui_elements["SearchTheWeb"]' in app_src,
          "技能抽屉的虚拟节点挂上了新名字")


def t_good_citizen_and_sourcing() -> None:
    print("\n[9] ⭐ 对搜索引擎当个好公民 + 答案要有出处")
    import skills.official.SearchTheWeb as M

    src = _code_only(SKILL_PATH.read_text(encoding="utf-8"))
    check(M._ENDPOINT.startswith("https://old-search.marginalia.nu"),
          "端点是运营者给非 JS 客户端指定的 old-search", M._ENDPOINT)
    # ⚠️ 一次用户请求最多两次搜索。第三次实测也救不回来，只是白耗对方资源。
    check("for attempts in (1, 2):" in src, "⭐ 最多两次，不做第三次")
    from urllib.parse import urlparse as _up
    _pu = _up(M._PROBE_HOME)
    check(_pu.path in ("", "/") and not _pu.query,
          "⭐ 探针探的是首页（无 path、无 query），不消耗对方一次真实检索", M._PROBE_HOME)
    check("not a source" in SKILL_PATH.read_text(encoding="utf-8"),
          "⭐ 结果里要求点名真正打开过的 url（[C1] 验收原文是「有出处」）")
    check("summaries only" in SKILL_PATH.read_text(encoding="utf-8"),
          "⚠️ 只回摘要不回正文（否则 token 优势立刻消失）")


def t_fallback_layer() -> None:
    print("\n[10] ⭐⭐ 兜底层：一个工具，底下是 playwright，用户看不到浏览器")
    from core.mcp_client import MCPServer, MCPManager, ST_CONNECTED
    from skills.official.OpenPageWithBrowser import OpenPageWithBrowser

    cfg = json.loads((ROOT / "config" / "mcp_servers.json").read_text(encoding="utf-8"))
    hs = cfg["mcpServers"].get("playwright-headless")
    check(bool(hs), "配置里有 playwright-headless")
    check("--headless" in (hs.get("args") or []), "⭐ 起的是无头浏览器（不弹窗）")
    check("--isolated" in (hs.get("args") or []), "独立 profile，不留痕迹")
    # ⚠️ 不指 --output-dir 的话，navigate 产生的 page-*.yml 快照会写进
    #    **当前工作目录** —— 也就是用户的项目里。实测踩到过。
    _args = hs.get("args") or []
    check("--output-dir" in _args, "⭐ 指定了 --output-dir（否则快照文件拉在用户项目里）")
    _od = _args[_args.index("--output-dir") + 1] if "--output-dir" in _args else ""
    check("${TEMP}" in _od or "${TMP}" in _od, "落盘位置在临时目录", _od)

    # 🔴🔴 这两个开关是**一对**，少一个就漏。
    s = MCPServer("playwright-headless", hs)
    check(s.lazy is True, "⭐ lazy：开机不拉起（闲置零开销）")
    check(s.expose_tools is False,
          "⭐⭐⭐ expose_tools=False：它的 24 个工具永远不进目录 —— "
          "只有 lazy 的话，**用完一次模型就会突然多出 24 个工具**")
    check(MCPServer("normal", {}).expose_tools is True,
          "普通 server 默认暴露（不写这个字段就是普通 server）")
    check(MCPServer("normal", {}).lazy is False, "普通 server 默认开机就连")

    # 隐藏 server 的工具连 `_tool_index` 都不进 —— 进了就等于模型能调它
    m = MCPManager.__new__(MCPManager)
    m._tool_index = {}
    hidden = MCPServer("playwright-headless", hs)
    hidden.status = ST_CONNECTED
    hidden.tools = [{"name": "browser_navigate", "description": "d", "input_schema": {}}]
    shown = MCPServer("playwright", {"provides": ["web.fetch"]})
    shown.status = ST_CONNECTED
    shown.tools = [{"name": "browser_navigate", "description": "d", "input_schema": {}}]
    m.servers = {"playwright-headless": hidden, "playwright": shown}
    names = [x["name"] for x in m.list_tool_manifests()]
    check(all("headless" not in n for n in names),
          "⭐⭐⭐ 已连接的隐藏 server，工具**依然**不进目录", str(names))
    check(all("headless" not in k for k in m._tool_index),
          "⭐ 连 `_tool_index` 都不进（进了就能被反解路由到）")
    check(any(n == "mcp__playwright__browser_navigate" for n in names),
          "而同名的可见 playwright 照常进目录（证明过滤的是 server 不是工具名）")

    # 能力登记不认领模型看不见的工具名
    check(hidden.expose_tools is False and shown.expose_tools is True,
          "两个 server 的暴露性确实不同（上面的对比才成立）")
    src = _code_only(module_text("core.mcp_client"))
    check("if not self.expose_tools" in src or "self.expose_tools else" in src,
          "⭐ 隐藏 server 登记成零工具（否则故障通知会说「你有 24 个工具坏了」，"
          "而模型手上从来没有过这些名字）")

    # `call_hidden` 不许被拿去调普通 server —— 那等于开一条绕过工具目录的旁路
    import asyncio as _aio
    from core.mcp_client import MCPError
    m2 = MCPManager.__new__(MCPManager)
    m2.servers = {"playwright": shown}
    try:
        _aio.get_event_loop().run_until_complete(
            m2.call_hidden("playwright", "browser_navigate", {}))
        check(False, "🔴 `call_hidden` 拒绝普通 server")
    except MCPError:
        check(True, "🔴🔴 `call_hidden` 拒绝普通 server —— 不给 [F6] 刚立起来的"
                    "「调用受本次工具合同约束」捅洞")
    except Exception as _e:
        check(False, "🔴 `call_hidden` 拒绝普通 server", type(_e).__name__)

    # 包装层要把 playwright 的壳剥掉
    raw = ('### Result\n"hello\\nworld"\n### Ran Playwright code\n'
           '```js\nawait page.evaluate(...)\n```')
    got = OpenPageWithBrowser._strip_playwright_noise(raw)
    check(got == "hello\nworld",
          "⭐ 剥掉 `### Result` / `### Ran Playwright code` 的壳", repr(got))
    # 📌 一个包装层如果把内层实现细节原样漏出去，那它就没有在包装。
    check("Playwright" not in got, "⭐ 剥完之后不再泄漏底层是 playwright")

    man = OpenPageWithBrowser().get_manifest()
    check(man["name"] == "OpenPageWithBrowser" == OpenPageWithBrowser.__name__,
          "类名与 manifest 名一致（不一致会静默变 UNKNOWN_TOOL）")
    check("playwright" not in man["description"].lower(),
          "⭐ 工具描述里不提 playwright —— 它的语义是「兜底再试一次」，不是浏览器控制")
    check("LAST RESORT" in man["description"] and "fetch" in man["description"],
          "⭐ 描述点明它是兜底、要先试普通 fetch（否则它会挤掉便宜得多的那条路）")


def main() -> int:
    print("=" * 74)
    print("[C1] 网络搜索能力 —— 全离线")
    print("=" * 74)
    t_name_is_not_websearch()
    t_five_page_states()
    t_absence_is_not_evidence()
    t_probe_refuses_to_fake_recovery()
    t_capability_registry()
    t_provides_replaces_keyword_guessing()
    t_web_status_three_tier()
    t_ui_card_and_avatar()
    t_good_citizen_and_sourcing()
    t_fallback_layer()
    print()
    print("=" * 74)
    print(f"结果：{_passed}/{_passed + _failed} 通过")
    print("=" * 74)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
