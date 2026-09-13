# skills_official/OpenPageWithBrowser.py
"""官方基础 Skill：用真实浏览器打开一个页面并取回正文。协议 v3.2"""

from __future__ import annotations

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

_SERVER = "playwright-headless"
_NAV = "browser_navigate"
_EVAL = "browser_evaluate"

_EVAL_FN = "() => document.body.innerText"

_TIMEOUT = 120.0
_DEFAULT_CHARS = 8000
_MAX_CHARS = 20000

class OpenPageWithBrowser(BaseSkill):
    def get_manifest(self):
        return {
            "name": "OpenPageWithBrowser",
            "description": (
                "LAST RESORT for reading one web page. Opens the url in a real "
                "browser (runs JavaScript) and returns the rendered text. "
                "Try the normal fetch tool FIRST - this one is much slower "
                "because it has to start a browser. Use this only when fetch "
                "returned nothing useful, or the page clearly needs JavaScript "
                "to show its content. One url per call; it does not search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full url, including https://",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": f"Cap on returned text, default {_DEFAULT_CHARS}.",
                    },
                },
                "required": ["url"],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="OpenPageWithBrowser",
            purpose="用无头浏览器打开一个 URL，返回渲染后的正文文本。",
            required_inputs=[
                InputDef("url", "string", "要打开的完整 URL。"),
            ],
            optional_inputs=[
                InputDef("max_chars", "integer", f"返回文本上限，默认 {_DEFAULT_CHARS}。"),
            ],
            data_output_keys=["url", "text", "chars", "truncated"],
            side_effects=[SideEffect.NETWORK],
            permission_level=PermissionLevel.NETWORK_ALLOWED,
            not_responsible_for=[
                "不搜索（找页面用 SearchTheWeb）",
                "不做浏览器自动化（点击/填表那是 playwright 的活）",
                "不登录、不处理需要凭据的页面",
                "不截图",
            ],
            lifecycle=Lifecycle.PERMANENT,
        )

    @staticmethod
    def _strip_playwright_noise(text: str) -> str:
        """把 @playwright/mcp 包在结果外面的那层壳剥掉。"""
        if "### Result" not in text:
            return text.strip()
        body = text.split("### Result", 1)[1]
        for marker in ("### Ran Playwright code", "### Page", "### Snapshot"):
            body = body.split(marker, 1)[0]
        body = body.strip()
        if body.startswith('"') and body.endswith('"'):
            try:
                import json as _json
                body = _json.loads(body)
            except Exception:
                pass
        return body.strip()

    async def run(self, url: str = "", max_chars: int = _DEFAULT_CHARS) -> SkillResult:
        u = (url or "").strip()
        if not u.startswith(("http://", "https://")):
            return SkillResult(
                success=False,
                text="打开失败：url 必须是完整的 http(s) 地址。",
                data={},
            )
        try:
            cap = int(max_chars)
        except (TypeError, ValueError):
            cap = _DEFAULT_CHARS
        cap = max(500, min(_MAX_CHARS, cap))

        try:
            from core.mcp_client import get_mcp_manager
            mgr = get_mcp_manager()
            await mgr.call_hidden(_SERVER, _NAV, {"url": u}, timeout=_TIMEOUT)
            raw = await mgr.call_hidden(_SERVER, _EVAL, {"function": _EVAL_FN},
                                        timeout=_TIMEOUT)
        except Exception as e:
            return SkillResult(
                success=False,
                text=(f"用浏览器打开失败（{type(e).__name__}）。这是兜底通道，"
                      f"失败不代表联网坏了 —— 普通的 fetch 可能仍然可用。"),
                data={"url": u},
            )

        text = self._strip_playwright_noise(raw)
        truncated = len(text) > cap
        if truncated:
            text = text[:cap]
        if not text:
            return SkillResult(
                success=True,
                text=f"{u} 打开了，但页面没有可读文本（可能是纯图片/视频，或内容在 iframe 里）。",
                data={"url": u, "text": "", "chars": 0, "truncated": False},
            )
        return SkillResult(
            success=True,
            text=(f"{u} 的正文（{len(text)} 字符"
                  f"{'，已截断' if truncated else ''}）：\n{text}"),
            data={"url": u, "text": text, "chars": len(text), "truncated": truncated},
        )
