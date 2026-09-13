# -*- coding: utf-8 -*-
"""MCP 发现链的**第一条**：Official MCP Registry。

═══ 这个模块存在的理由 ═══

发现链是**四条各干一件事**，不许互相替代：

| 入口 | 职责 | 为什么不是它替代别人 |
|---|---|---|
| **Official MCP Registry**（本模块） | **结构化发现** —— 名/版本/包/远端/repo/发布者/安装方式 | 它没有 README、源码、issue |
| GitHub | **工程尽调** —— README / 源码 / release / issue / 许可证 | 它不是目录，别拿它当目录 |
| WebSearch | **开放世界兜底** —— 官网 / 价格 / 认证方式 / 托管 URL | 前两者都没有这些 |
| Hugging Face Spaces | **AI/ML 专用能力源**（ASR/TTS/OCR…） | 它不是「第二个 Registry」 |

⚠️ 本模块**只做第一条**。另外三条用现有能力就够（GitHub / 官网用 `fetch` 或
   `OpenPageWithBrowser` 读，开放世界用 `SearchTheWeb`），
   📌 **不给已经能做的事再造一个工具** —— 那只会多一份要维护的东西。

═══ 🔴 契约是实测来的，不是照着文档抄的（2026-08-29）═══

    GET https://registry.modelcontextprotocol.io/v0/servers?search=<kw>&limit=<n>

    { "servers":[ {"server":{...}, "_meta":{...}} ],
      "metadata": {"nextCursor", "count"} }

    server.*          $schema / name / description / title / version / repository
                      + **remotes** 或 **packages**（二选一）
    server.remotes[]  type / url
    server.packages[] registryType / registryBaseUrl / identifier / version /
                      runtimeHint / transport{type} /
                      runtimeArguments[] / environmentVariables[]
                        └ value / type / description / default / name / **isSecret**
    _meta."io.modelcontextprotocol.registry/official"
                      status / statusChangedAt / publishedAt / updatedAt / isLatest

⭐ 这份字段表直接解决了四件原本要靠猜的事：

  ① `packages` vs `remotes` **就是**「在本机跑第三方代码」和「只发网络请求」的分界
     ⇒ 授权弹窗那两套红/黄视觉，判据现成，不用再从 command 字符串里推
  ② `environmentVariables[].isSecret` —— **哪些环境变量是密钥，registry 自己标了**
     ⇒ 前半段那条「env 只报 key 名不报值」从此有权威依据，不靠名字里有没有 KEY 猜
  ③ `runtimeHint` / `transport.type` —— 装它到底会跑什么，**授权前**就能如实展示
  ④ `_meta` 的 `publishedAt` / `updatedAt` / `isLatest` —— **维护状态**

  🔴 而 ④ 推翻了原先设想的一句分工：「Registry 没有维护状态，那要靠 GitHub」。
     现在 registry 自己就带时间戳。⇒ GitHub 那条链的职责缩小到
     「README / 源码 / issue / 许可证」，**不再包括查维护状态**。
     📌 **一份两周前写的外部 API 分工，要拿今天的响应重新核一遍** ——
        preview 阶段的 API 会长东西，而那句分工是照着更早的观察写的。

═══ ⚠️ 解析一律防御式 ═══

这个 API **仍在 preview**，字段随时可能增删。所以：
  · 全程 `.get()` 链，不用下标
  · 缺字段 → 该项留空，**不抛异常**
  · 结构整个变了 → 返回「拿到了但读不懂」，而不是崩
📌 **一个还在 preview 的外部契约，解析它的代码要按「它会变」来写** ——
   按「它不变」写的代码，变的那天是在用户机器上炸的。
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

_BASE = "https://registry.modelcontextprotocol.io/v0/servers"
_TIMEOUT = 20.0
_MAX_LIMIT = 20
_OFFICIAL_META = "io.modelcontextprotocol.registry/official"


def _meta_of(entry: dict) -> dict:
    """registry 的官方 meta。⚠️ 它挂在 entry 和 server 两层都可能有，都试一遍。"""
    for holder in (entry, entry.get("server") or {}):
        m = (holder.get("_meta") or {}).get(_OFFICIAL_META)
        if isinstance(m, dict):
            return m
    return {}


def _install_shape(srv: dict) -> tuple[str, str]:
    """返回 (kind, 一句话说明)。

    🔴 这是**风险分界线**，不是展示细节：
       packages → 在用户机器上跑第三方进程（stdio）
       remotes  → 只发网络请求
    """
    pkgs = srv.get("packages") or []
    if pkgs:
        p = pkgs[0] if isinstance(pkgs[0], dict) else {}
        ident = p.get("identifier") or "?"
        reg = p.get("registryType") or "?"
        hint = p.get("runtimeHint") or ""
        tr = (p.get("transport") or {}).get("type") or "stdio"
        extra = f", runtime={hint}" if hint else ""
        return "local", f"runs on this machine ({reg}: {ident}, transport={tr}{extra})"
    rem = srv.get("remotes") or []
    if rem:
        r = rem[0] if isinstance(rem[0], dict) else {}
        return "remote", f"remote endpoint ({r.get('type') or '?'}: {r.get('url') or '?'})"
    return "unknown", "no install information published"


def _secret_env(srv: dict) -> list[str]:
    """需要用户提供、且 registry 标了 isSecret 的环境变量名。

    ⚠️ 只取**名字**，永远不碰值 —— 同前半段那条铁律。
    """
    out: list[str] = []
    for p in (srv.get("packages") or []):
        if not isinstance(p, dict):
            continue
        for ev in (p.get("environmentVariables") or []):
            if isinstance(ev, dict) and ev.get("isSecret") and ev.get("name"):
                out.append(str(ev["name"]))
    return out


def _one_line(entry: dict) -> str:
    srv = entry.get("server") or {}
    meta = _meta_of(entry)
    name = srv.get("name") or "(unnamed)"
    desc = (srv.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 160:
        desc = desc[:157] + "..."
    kind, how = _install_shape(srv)
    bits = [f"- {name}  [{kind}]",
            f"    what: {desc or '(no description)'}",
            f"    install: {how}"]
    if srv.get("version"):
        bits.append(f"    version: {srv['version']}")
    if srv.get("repository"):
        repo = srv["repository"]
        url = repo.get("url") if isinstance(repo, dict) else repo
        if url:
            bits.append(f"    repo: {url}")
    upd = meta.get("updatedAt") or meta.get("publishedAt")
    if upd:
        latest = " (latest)" if meta.get("isLatest") else ""
        bits.append(f"    last updated: {upd}{latest}")
    if meta.get("status") and meta.get("status") != "active":
        bits.append(f"    ⚠️ registry status: {meta['status']}")
    secrets = _secret_env(srv)
    if secrets:
        bits.append(f"    needs secrets: {', '.join(secrets)}  "
                    f"(the user must supply these; ask before assuming they have them)")
    return "\n".join(bits)


async def search_registry(query: str, limit: int = 8) -> tuple[bool, str]:
    """查 Official MCP Registry。返回 (ok, 给模型看的文本)。

    ⚠️ 返回的是**候选清单，不是结论**：
       `source = OFFICIAL_MCP_REGISTRY` **不等于** `trust = SAFE`。
       官方 registry 是**发布/发现的基础设施**，它本身就是 community server 的
       集中仓库（且仍在 preview），给的是「身份与安装信息更结构化」，
       不是安全认证。
       📌 **「来源更权威」和「内容更可信」是两个现实，压成一个就是一次安全事故的形状。**
    """
    q = (query or "").strip()
    if not q:
        return False, "search_mcp_registry needs a non-empty query."
    n = max(1, min(int(limit or 8), _MAX_LIMIT))
    try:
        import httpx
    except ImportError:
        return False, ("The registry lookup needs the `httpx` package, which is not "
                       "installed here. Fall back to a normal web search.")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            resp = await cli.get(_BASE, params={"search": q, "limit": n})
        if resp.status_code != 200:
            return False, (f"The MCP registry returned HTTP {resp.status_code}. "
                           f"It may be down or the API may have changed. "
                           f"Fall back to a normal web search.")
        data: Any = resp.json()
    except Exception as e:
        logger.warning(f"[MCPDiscovery] registry 查询失败: {type(e).__name__}: {e}")
        return False, (f"Could not reach the MCP registry ({type(e).__name__}). "
                       f"Fall back to a normal web search - do not treat this as "
                       f"'no such server exists'.")

    # ⚠️ 结构整个变了 → 说「拿到了但读不懂」，不要崩，也不要假装没有结果。
    #    📌 「读不懂」和「没有」是两件事，压成一件会让模型报告一个假的空结果。
    if not isinstance(data, dict) or "servers" not in data:
        return False, ("The MCP registry replied, but not in the shape this build "
                       "expects (no `servers` key). The API may have changed. "
                       "Fall back to a normal web search.")
    entries = [e for e in (data.get("servers") or []) if isinstance(e, dict)]
    if not entries:
        return True, (f"The MCP registry has no server matching {q!r}. "
                      f"That does not mean none exists - the registry is opt-in and "
                      f"still in preview. Try a web search, or a different wording.")
    total = (data.get("metadata") or {}).get("count")
    head = (f"MCP registry results for {q!r}"
            + (f" (showing {len(entries)} of {total})" if total else "")
            + ":")
    tail = (
        "\n\n⚠️ These are candidates, not recommendations. The registry is publishing "
        "infrastructure, not a safety review - anyone can publish to it.\n"
        "Before proposing one to the user:\n"
        "- [local] means installing it RUNS THIRD-PARTY CODE ON THEIR MACHINE. "
        "[remote] only makes network requests. Say which one it is.\n"
        "- Check the repo (README, recent commits, issues, licence) before trusting it.\n"
        "- If it needs secrets, the user has to obtain them - check they are willing.\n"
        "- Prefer a server that is actively maintained; `last updated` is above."
    )
    return True, head + "\n" + "\n".join(_one_line(e) for e in entries) + tail
