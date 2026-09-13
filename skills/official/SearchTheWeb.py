# skills_official/SearchTheWeb.py
"""官方基础 Skill：网络搜索（Marginalia）。协议 v3.2"""

from __future__ import annotations

import asyncio
import html as _html

import httpx
from bs4 import BeautifulSoup

from core.schema import (
    BaseSkill,
    SkillResult,
    SkillSpec,
    InputDef,
    ContextLevel,
    SideEffect,
    PermissionLevel,
    Lifecycle,
)

_ENDPOINT = "https://old-search.marginalia.nu/search"
_PARAM = "query"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Nano-Lumen/1.0")

_TIMEOUT = 25.0
_RETRY_DELAY = 1.5
_DEFAULT_RESULTS = 8
_MAX_RESULTS = 25
_SNIPPET_CAP = 300

_CONTAINER_SEL = "section#results"
_CARD_SEL = "section.search-result"

_TITLE_MARK = "Marginalia"
_RAW_CARD_MARK = "search-result"

PAGE_OK = "ok"
PAGE_EMPTY = "empty"
PAGE_DEGRADED = "degraded"
PAGE_CHANGED = "changed"
PAGE_ALIEN = "alien"

_NO_RESULT_TEXT = (
    "No results came back for '{q}'. Note: this search engine intermittently "
    "returns an empty page even when it does have matches (already retried once), "
    "so this does NOT prove nothing exists. Either try different keywords, or say "
    "plainly that the search came back empty - do not tell the user the topic has "
    "no information."
)

class SearchTheWeb(BaseSkill):
    def get_manifest(self):
        return {
            "name": "SearchTheWeb",
            "description": (
                "Search the public web and get back a list of candidates "
                "(title + url + snippet). Returns ONLY the summaries, never page "
                "bodies - pick 1-2 urls and read them with the fetch tool. "
                "Search in ENGLISH: the index behind this is English-first, so "
                "turn the user's question into English keywords first. "
                "Do NOT use this for the official docs of a library, framework or "
                "Microsoft/Azure product - context7 and microsoft-learn index those "
                "properly. Use this for error messages, "
                "how-did-other-people-solve-it, and anything with no official doc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords, in English.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"1-{_MAX_RESULTS}, default {_DEFAULT_RESULTS}.",
                    },
                },
                "required": ["query"],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="SearchTheWeb",
            purpose="搜索公开网页，返回候选列表（标题+链接+摘要），不返回正文。",
            required_inputs=[
                InputDef("query", "string", "搜索关键词，用英文。"),
            ],
            optional_inputs=[
                InputDef("max_results", "integer", f"返回条数，1-{_MAX_RESULTS}。"),
            ],
            data_output_keys=["query", "results", "count", "engine"],
            side_effects=[SideEffect.NETWORK],
            permission_level=PermissionLevel.NETWORK_ALLOWED,
            not_responsible_for=[
                "不返回网页正文（读正文用 fetch MCP）",
                "不查库/框架的官方文档（那是 context7 / microsoft-learn 的活）",
                "不翻译查询词",
                "不执行 JS，拿不到前端渲染的结果",
            ],
            lifecycle=Lifecycle.PERMANENT,
        )

    @staticmethod
    def _clean(node) -> str:
        if node is None:
            return ""
        return _html.unescape(node.get_text(" ", strip=True)).strip()

    @classmethod
    def _parse(cls, page_html: str, limit: int):
        """返回 (results, page_kind)。"""
        soup = BeautifulSoup(page_html, "html.parser")

        if _TITLE_MARK not in (page_html[:2000] or ""):
            return [], PAGE_ALIEN

        if soup.select_one(_CONTAINER_SEL) is None:
            return [], (PAGE_CHANGED if _RAW_CARD_MARK in page_html else PAGE_DEGRADED)

        out = []
        for card in soup.select(_CARD_SEL):
            a = card.select_one("a.title")
            if a is None:
                continue
            url = (a.get("href") or "").strip()
            if not url.startswith("http"):
                continue
            title = cls._clean(a)
            snippet = cls._clean(card.select_one("p.description"))[:_SNIPPET_CAP]
            out.append({
                "title": title or url,
                "url": url,
                "snippet": snippet or "(no snippet)",
            })
            if len(out) >= limit:
                break
        if out:
            return out, PAGE_OK
        return [], (PAGE_CHANGED if _RAW_CARD_MARK in page_html else PAGE_EMPTY)

    async def _fetch(self, client, q: str) -> httpx.Response:
        return await client.get(
            _ENDPOINT,
            params={_PARAM: q},
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        )

    async def run(self, query: str = "", max_results: int = _DEFAULT_RESULTS) -> SkillResult:
        from core import health as H

        q = (query or "").strip()
        if not q:
            return SkillResult(success=False, text="搜索失败：query 为空。", data={})

        try:
            limit = int(max_results)
        except (TypeError, ValueError):
            limit = _DEFAULT_RESULTS
        limit = max(1, min(_MAX_RESULTS, limit))

        results: list = []
        kind = PAGE_DEGRADED
        attempts = 0

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                for attempts in (1, 2):
                    resp = await self._fetch(client, q)
                    if resp.status_code != 200:
                        H.report_fault(
                            H.Cap.WEB_SEARCH, "search_http_error",
                            f"网络搜索被拒绝（HTTP {resp.status_code}）。",
                            detail=f"HTTP {resp.status_code} from {_ENDPOINT}",
                            hint="稍后会自动重试；持续失败可能是搜索引擎侧的限制。",
                            hint_en=f"The search engine returned HTTP {resp.status_code}. Do not "
                                    "retry in a loop - tell the user, and fall back to reading a "
                                    "URL with fetch if you already have one.",
                        )
                        return SkillResult(
                            success=False,
                            text=f"搜索失败：搜索引擎返回 HTTP {resp.status_code}。",
                            data={},
                        )
                    results, kind = self._parse(resp.text, limit)
                    if kind not in (PAGE_EMPTY, PAGE_DEGRADED):
                        break
                    if attempts == 2:
                        break
                    await asyncio.sleep(_RETRY_DELAY)
        except Exception as e:
            H.report_fault(
                H.Cap.WEB_SEARCH, "search_unreachable",
                "网络搜索暂时用不了，可能是网络断了。",
                detail=f"{type(e).__name__}: {e}",
                hint="检查网络连接；恢复后会自动重试。",
                hint_en="Web search is unreachable right now. You can still read a URL "
                        "directly with fetch if the user gives you one.",
            )
            return SkillResult(
                success=False,
                text=f"搜索失败：无法访问搜索引擎（{type(e).__name__}）。",
                data={},
            )

        if kind == PAGE_CHANGED:
            H.report_fault(
                H.Cap.WEB_SEARCH, "search_markup_changed",
                "网络搜索的结果解析不出来了，搜索引擎页面结构可能变了。",
                detail=f"raw HTML has '{_RAW_CARD_MARK}' but selectors matched 0 at {_ENDPOINT}",
                hint="这是程序问题，不是网络问题，需要更新 SearchTheWeb 这个 Skill。",
                hint_en="Web search returns pages we can no longer parse. That is a bug in "
                        "the skill, not a network problem - say so plainly instead of retrying.",
            )
            return SkillResult(
                success=False,
                text="搜索失败：结果页结构无法解析（搜索引擎可能已改版）。",
                data={},
            )

        if kind == PAGE_ALIEN:
            H.report_fault(
                H.Cap.WEB_SEARCH, "search_unexpected_page",
                "网络搜索拿回了一个不认识的页面，可能被网络环境拦截或重定向了。",
                detail=f"response from {_ENDPOINT} does not look like the search engine's page",
                hint="检查代理/网关设置；恢复后会自动重试。",
                hint_en="The search request came back with someone else's page (proxy or gateway "
                        "interference). Tell the user plainly; reading a URL with fetch may still work.",
            )
            return SkillResult(
                success=False, text="搜索失败：返回的不是搜索引擎的页面（可能被拦截或重定向）。",
                data={},
            )

        if kind == PAGE_DEGRADED:
            H.report_degraded(
                H.Cap.WEB_SEARCH, "search_flaky",
                "网络搜索这次没返回结果页，稍后会自己恢复。",
                detail=f"no '{_CONTAINER_SEL}' after {attempts} attempt(s) at {_ENDPOINT}",
                hint="通常几秒后重试就好，不需要做什么。",
                hint_en="The search engine returned an incomplete page this time. It is flaky, "
                        "not broken - you may simply try the search again.",
            )
            return SkillResult(
                success=False,
                text=("搜索这次没拿到结果页（该引擎偶发，不是坏了）。"
                      "This engine intermittently returns an incomplete page; "
                      "trying the same search again usually works."),
                data={"query": q, "results": [], "count": 0,
                      "engine": "marginalia", "attempts": attempts, "flaky": True},
            )

        H.report_ok(H.Cap.WEB_SEARCH, note="search page parsed")

        if not results:
            return SkillResult(
                success=True,
                text=_NO_RESULT_TEXT.format(q=q),
                data={"query": q, "results": [], "count": 0,
                      "engine": "marginalia", "attempts": attempts},
            )

        lines = [
            f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results, 1)
        ]
        return SkillResult(
            success=True,
            text=(f"Search results for '{q}' ({len(results)} candidates, summaries only - "
                  f"read a url with fetch to see the actual page):\n" + "\n".join(lines)
                  + "\n\nWhen you answer, name the url(s) you actually opened. "
                    "\"According to the docs\" without the link is not a source."),
            data={"query": q, "results": results, "count": len(results),
                  "engine": "marginalia", "attempts": attempts},
        )

_PROBE_HOME = "https://old-search.marginalia.nu/"
_PROBE_TIMEOUT = 6.0

def _web_search_probe() -> bool:
    """搜索能力是否恢复了。"""
    try:
        from core.health import get_health, Cap
        _st = get_health().get(Cap.WEB_SEARCH)
        if _st is not None and _st.code == "search_markup_changed":
            return False
    except Exception:
        pass
    try:
        r = httpx.get(_PROBE_HOME, headers={"User-Agent": _UA},
                      timeout=_PROBE_TIMEOUT, follow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False

try:
    from core.health import get_health as _get_health, Cap as _Cap
    _get_health().register_probe(_Cap.WEB_SEARCH, _web_search_probe)
except Exception:  # pragma: no cover
    pass
