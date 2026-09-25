"""
core/mcp_client.py — MCP（Model Context Protocol）客户端核心。

定位：MCP 工具属于「Nano 自身能力」，不是 Skill。用户不区分内置/MCP，统一感知为
"Nano 本来就能做的事"。本模块只负责"连上 server、列工具、调工具、维护连接状态"，
不碰 UI、不碰 orchestrator 路由——那些在上层。

架构（做全做优，不是最小落地）：
- **每个 server 一个常驻 worker task**。MCP SDK 的 stdio_client/streamablehttp_client
  和 ClientSession 都是 async context manager，且 anyio 的 cancel scope 绑定 task——
  context 的 enter/use/exit 必须在同一个 task 内完成，不能跨 task。所以每个 server 起
  一个专属 worker task：在 task 内打开 streams+session、initialize、list_tools，然后
  守在请求队列上服务工具调用。外部（orchestrator）通过 队列+future 把调用投进去。
- 这样既规避了 task-scoping 陷阱，又让连接常驻（stdio 不必每次调用重启子进程——
  npx 启动要好几秒，per-call 重启是糟糕体验，而本项目的取舍是**体验 > 性能**）。
- 自动重连：指数退避（1/2/4/8/16s）最多 5 次→failed；401/403→needs_auth 不再重试
  （对齐 Claude Code 的连接失败语义）。

配置：config/mcp_servers.json，沿用生态标准 mcpServers shape（用户粘贴的就是这个）。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import io
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_SDK_OK = True
except Exception as _e:  # pragma: no cover - SDK 未装时优雅降级
    _MCP_SDK_OK = False
    logger.warning(f"[MCP] 官方 mcp SDK 不可用，MCP 能力关闭：{_e}")

# 项目根 / 配置路径
from core.paths import ROOT as _ROOT  # noqa: E402
CONFIG_PATH = _ROOT / "config" / "mcp_servers.json"

# 工具名前缀：mcp__<server>__<tool>（对齐生态/Claude Code 命名，保证全局唯一、可反解）
TOOL_PREFIX = "mcp__"

# ⭐ 官方内置 server 的说明 —— **三种来源里的第 ① 种：我们自己写。**
#    另外两种：Nano 自主接入 / 用户粘贴 JSON —— 都靠连上之后读 `tools/list`
#    再让便宜模型压一句（见 `orchestrator._mcp_tooltip`）。
# ⚠️ 官方这几个**不走那条路**：它们的用途是我们定的，让模型去猜自己写的东西
#    是多此一举，而且每次重装都会得到一句不一样的话。
#    📌 说明文字的语言跟随界面语言那条规矩，只管**生成出来的**那些；
#       这几句是常量，属于界面文案那一摊。
_BUILTIN_DESC = {
    "fetch": "抓取网页并转成 Markdown 给模型阅读。",
    "playwright": "用真实浏览器打开页面、点击和填表，能处理需要登录或渲染的站点。",
    "playwright-headless": "同 playwright，但不显示浏览器窗口 —— 供内部的网页读取能力使用。",
    "microsoft-learn": "查询微软官方文档与代码示例。",
    "context7": "查询开源库的最新文档和 API 用法。",
}

# 连接状态常量
ST_DISCONNECTED = "disconnected"
ST_CONNECTING = "connecting"
ST_CONNECTED = "connected"
ST_FAILED = "failed"
ST_NEEDS_AUTH = "needs_auth"
ST_DISABLED = "disabled"

_MAX_RECONNECT = 5
_CALL_TIMEOUT_DEFAULT = 120.0   # 单次工具调用超时（秒）；长任务应走后台唤醒而非拉长这个
_READY_TIMEOUT = 25.0           # 等待首次连接就绪的超时（秒）
# 连接握手（打开传输 + initialize + list_tools）中每个请求的读取超时（秒）。
# 没有它时，远端接受连接却不回应会让 worker 永远停在 connecting：既不失败也不重连，
# 界面上也没有「重试」可点。本地 stdio 给得更宽：npx 首次启动可能要先下载包。
_HANDSHAKE_TIMEOUT_HTTP = 30.0
_HANDSHAKE_TIMEOUT_STDIO = 120.0


# ── ${VAR} / ${VAR:-default} 环境变量展开（生态约定，Claude Code 同款）──────────
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """递归展开字符串/列表/字典里的 ${VAR} 与 ${VAR:-default}。"""
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _looks_like_auth_error(err: Exception) -> bool:
    """粗判是否是 401/403 授权类错误（→ needs_auth，不再 hammer 重连）。"""
    s = f"{type(err).__name__}: {err}".lower()
    return any(t in s for t in ("401", "403", "unauthorized", "forbidden", "auth"))


class MCPServer:
    """一个 MCP server 的常驻连接（跑在专属 worker task 内）。"""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg or {}
        self.status: str = ST_DISCONNECTED
        self.tools: list[dict] = []      # [{name, description, input_schema}]
        self.last_error: str = ""
        # 分类后的 (code, 人话, 恢复建议)。原始 last_error 留着做技术详情/日志，
        # 但**给人看的一律用这三个** —— 那个 36 字符截断的根因就是"拿技术串当人话用"。
        self.last_fault: tuple[str, str, str] = ()
        # 子进程 stderr 的留存（stdio 才有）。**每次连接尝试重置** ——
        # 上一次的死因留到下一次会把人指向错误的方向。
        self._stderr_tap: "_StderrTap | None" = None
        self._req_q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._reconnect_attempts = 0

    # ── 配置派生属性 ──────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def transport(self) -> str:
        t = (self.cfg.get("type") or "").strip().lower()
        if t in ("stdio", "http"):
            return t
        if self.cfg.get("command"):
            return "stdio"
        if self.cfg.get("url"):
            return "http"
        return "stdio"

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    # ── 健康登记 ────────────────────────────────────────────────────
    # ⚠️ 这一段是本模块**唯一**碰 health 的地方，且全部 import 都在函数内。
    #    理由与 `register_probe` 一样：依赖方向必须是 mcp_client → health 单向，
    #    health 是最底层的事实表，它不该认识任何领域模块。

    @property
    def cap_key(self) -> str:
        from core.health import Cap
        return Cap.mcp_server(self.name)

    # ── 这个 server 自己声明提供哪些能力 ──────────────────────────
    # 🔴 **这里曾是关键词猜测**（`_is_web_like`：拿 name+command+args+url+
    #    所有工具名和描述拼成一个 blob，去撞 11 个关键词 fetch/search/browse/
    #    http/url/web/…）。2026-08-25 换掉，因为它有个明显的问题：
    #    **关键词太宽等于常绿** —— playwright 那 24 个工具里必然有含 `browse`
    #    或 `url` 的，所以只要 playwright 连上，「互联网检索」就永远是绿的，
    #    而那 ≠ 能搜索。
    # 📌 **一个永远为真的判断，和没有这个判断，信息量完全相同。**
    #    它比没有更坏的地方在于：它看起来在检查。
    # ⭐ 改成**声明式**：谁提供什么能力，写在那个 server 自己的配置里。
    #    配置是事实的来源，不用去它的名字里找线索。
    # ⚠️ 第三方 server 没声明 `provides` → 不认领任何能力，只归 environment 卡。
    #    这是**安全的默认**：宁可少认领一个，也不要让一张卡在能力其实没了的
    #    时候还亮着绿灯（那正是我们刚修掉的问题）。
    def declared_caps(self) -> frozenset[str]:
        prov = self.cfg.get("provides") or []
        if isinstance(prov, str):
            prov = [prov]
        return frozenset(str(x) for x in prov if isinstance(x, str))

    def _is_web_like(self) -> bool:
        """归不归「互联网检索」卡 —— 现在只看它自己声明的 `web.*`。"""
        return any(c.startswith("web.") for c in self.declared_caps())

    # ── 懒起 + 隐藏 ────────────────────────────────────────────────────
    # ⭐ 这两个开关是**一对**，少一个就漏。
    #    `lazy` 让它开机不启动（闲置零开销）；但只有 lazy **不够** ——
    #    🔴 server 一旦连上，`list_tool_manifests()` 就会把它的工具全部
    #       聚合进目录。也就是说：**用完一次之后，模型会突然多出 24 个工具。**
    #       那比一开始就多 24 个更糟，因为它是「用着用着突然变了」。
    # 📌 而多出来的那 24 个还跟现有 playwright **同名同义** ——
    #    正是本仓反复栽的那个形状：两个通道都通向同一件事，模型选它更熟的那个。
    @property
    def lazy(self) -> bool:
        """开机不拉起，第一次被调用时才连。`call_tool` 本来就自带懒启动。"""
        return bool(self.cfg.get("lazy"))

    @property
    def owned_by(self) -> str:
        """这个 server 是**哪个 Skill 的内部零件**；空 = 它是用户的外接能力。

        ⭐⭐ 起因：`playwright-headless` 每次开机都显示「未连接」，
           用户以为是 bug 去点重连，Nano 也以为是故障去 reconnect ——
           而它 `lazy=True`，开机不拉、按需才起，**那是设计**。

        📌 真实结构是：
        ```
        Skill「OpenPageWithBrowser」   ← 用户看得见、可禁用、可删除 = **能力**
          └─ playwright-headless        ← 实现零件
        ```
        而改造前这两者**平级摆在两个列表里，都让用户管理** ⇒ 用户可以禁用零件，
        让能力静默失效（Skill 仍显示 READY，一用就失败）。
        📌 **用户管理的是能力，不是能力的零件。**

        ⚠️ 为什么**不复用 `lazy`**，也**不按名字硬编码**：
        ```
        lazy      答的是「什么时候启动」
        owned_by  答的是「这是谁的东西」        ← 两个问题，别挤一个字段
        名字硬编码 答的是「这一个」              ← 而"唯一特殊"在这个项目里
                                                 几乎从没真的只有一个
        ```
        📌 同 `ToolOrigin` 那条：**一个字段兼答两个现实，就是在制造下一个 bug。**
        ⭐ 而它顺带解决了「坏了怎么办」：错误可以说「网页读取
           （OpenPageWithBrowser）的浏览器组件起不来」，而不是甩一个用户
           没见过的名字。下一个官方 Skill 带自己的零件时自动正确，不用再改代码。

        ⚠️ 用户粘贴的配置走生态标准 `mcpServers` shape，**不含这个字段** ⇒ 永远为空
           ⇒ 用户自己加的东西永远看得见、管得着。
        """
        return str(self.cfg.get("owned_by") or "").strip()

    @property
    def expose_tools(self) -> bool:
        """它的工具进不进模型的工具目录。默认进（不写就是普通 server）。"""
        return self.cfg.get("expose_tools", True) is not False

    def _full_tool_names(self) -> tuple[str, ...]:
        return tuple(MCPManager._make_tool_name(self.name, t["name"]) for t in self.tools)

    def _register_capability(self) -> None:
        """把这个 server 登记成一个能力（幂等；每次连上都刷新一次）。

        ⭐⭐ `tools=` 填的是**上一次已知的工具全名**。
           server 掉线后工具名会直接从 Catalog 消失，执行层因此分不清
           「这名字从来不存在」和「上一轮真实存在、只是现在断了」。
           登记进来之后，`tool_block_reason(name)` 会先一步认领它，
           模型收到的是「currently unavailable + 原因 + 恢复建议」，
           **而不是 UNKNOWN_TOOL 那句假话** —— 📌 失败信息必须是正确的。
        """
        try:
            from core.health import CapabilitySpec, register_capability
        except Exception:
            return
        # 🔴 隐藏 server 登记成**零工具**。它的工具名从来不进模型的工具目录，
        #    把它们登进 `tools=` 会有两个后果：`blocked_tools()` 认领一批模型
        #    根本看不见的名字；故障时的能力通知对模型说「你有这 24 个工具，
        #    它们现在坏了」—— 📌 **那是一句它无法核对的话**，
        #    因为它手上从来没有过这些名字。
        # ⚠️ 能力本身照常登记（掉了要有人知道），只是不认领工具名。
        names = () if not self.expose_tools else self._full_tool_names()
        label = f"外部能力：{self.name}"
        if names:
            label += f"（{len(names)} 个工具）"
        # 注入模型的那句"所以你该怎么办"。⚠️ 通用措辞在这里是**错的** ——
        # 它会让模型"别调相关工具"就完事，而这一项的要害恰恰是
        # 【别说自己从来没有这个能力】—— 那是最坏的一种失败信息。
        sample = ", ".join(n.split("__")[-1] for n in names[:6])
        hint = (
            f"This capability comes from the MCP server '{self.name}'"
            + (f" ({len(names)} tool{'s' if len(names) != 1 else ''}, e.g. {sample})." if names else ".")
            + " You DO have this capability normally - it is broken right now, not absent."
            " If the user asks for something these tools would do, say plainly that the"
            " capability is temporarily unavailable and give the reason above."
            " Never say you never had it."
        )
        register_capability(CapabilitySpec(
            key=self.cap_key,
            label=label,
            tools=names,
            # 联网类 server 归「互联网检索」卡 —— 那张卡现在靠关键词猜，
            # 会把它改成读健康登记表（两条本来就是同一件事的两半）。
            monitor_card="net" if self._is_web_like() else "environment",
            notice_hint=hint,
        ))

    def _health_ok(self) -> None:
        try:
            from core.health import report_ok
            self._register_capability()
            report_ok(self.cap_key, note=f"{self.tool_count} tools")
        except Exception as e:
            logger.debug(f"[MCP] '{self.name}' 健康登记(ok)跳过: {e}")

    def _health_fault(self, err: Exception | None, *, code: str = "",
                      msg: str = "", hint: str = "", hint_en: str = "") -> None:
        try:
            from core.health import report_fault, Severity
            self._register_capability()
            _errtext = self._stderr_tap.tail() if self._stderr_tap else ""
            if err is not None:
                code, msg, hint, hint_en = _classify_mcp_error(
                    err, self.name, str(self.cfg.get("command", "")), _errtext)
            self.last_fault = (code, msg, hint)
            report_fault(
                self.cap_key, code, msg, hint=hint, hint_en=hint_en,
                # 技术详情带上 stderr 尾巴：设置页与日志要能看到原始那几行
                detail=" | ".join(x for x in (
                    self.last_error or (f"{type(err).__name__}: {err}" if err else ""),
                    _stderr_tail(_errtext, 400),
                ) if x),
                # ⚠️ 用户从没配过的可选 server 挂掉 ≠ 知识库挂掉。
                #    严重度按"它带没带来过工具"分：没连上过 → WARNING，
                #    曾经在用 → ERROR。（Status 与 Severity 分开正是为了这个。）
                severity=Severity.ERROR if self.tools else Severity.WARNING,
            )
        except Exception as e:
            logger.debug(f"[MCP] '{self.name}' 健康登记(fault)跳过: {e}")

    def _health_forget(self) -> None:
        """用户主动禁用/删除 → 这不是故障，别留一张故障卡片。"""
        try:
            from core.health import unregister_capability
            unregister_capability(self.cap_key)
        except Exception:
            pass

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        """启动 worker（幂等）。"""
        if not _MCP_SDK_OK:
            self.status = ST_FAILED
            self.last_error = "mcp SDK unavailable"
            self._health_fault(None, code=MCP_SDK_MISSING,
                               msg=f"外部能力「{self.name}」不可用：本机没有可用的 mcp SDK。",
                               hint="运行 install.bat 重装依赖（pip install mcp）。",
                               hint_en="Reinstall dependencies by running install.bat "
                                       "(`pip install mcp`).")
            return
        if not self.enabled:
            self.status = ST_DISABLED
            self._health_forget()   # 用户关的，不是故障
            return
        if self._worker and not self._worker.done():
            return
        self._stop.clear()
        self._ready.clear()
        self._worker = asyncio.create_task(self._run(), name=f"mcp-{self.name}")

    async def stop(self) -> None:
        """优雅停止 worker。"""
        self._stop.set()
        try:
            self._req_q.put_nowait(None)  # 唤醒可能阻塞在 get() 的 _serve
        except Exception:
            pass
        if self._worker:
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except Exception:
                self._worker.cancel()
        self.status = ST_DISABLED if not self.enabled else ST_DISCONNECTED
        self._ready.clear()

    # ── worker 主循环：连接 + 重连退避 ────────────────────────────────────
    async def _run(self) -> None:
        while not self._stop.is_set():
            self.status = ST_CONNECTING
            stack = AsyncExitStack()
            try:
                session = await self._open_session(stack)
                await session.initialize()
                await self._refresh_tools(session)
                self.status = ST_CONNECTED
                self._reconnect_attempts = 0
                self.last_error = ""
                self.last_fault = ()
                self._ready.set()
                self._health_ok()
                logger.debug(f"[MCP] '{self.name}' 已连接 · {self.tool_count} 个工具 · {self.transport}")
                # 进入服务循环；正常情况下一直守在这里直到 stop 或连接断开
                await self._serve(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                if _looks_like_auth_error(e):
                    self.status = ST_NEEDS_AUTH
                    logger.warning(f"[MCP] '{self.name}' 需要授权（不再自动重连）：{self.last_error}")
                    self._health_fault(e)
                    self._ready.set()  # 解除等待者，让调用方拿到 needs_auth
                    break
                self.status = ST_FAILED
                logger.warning(f"[MCP] '{self.name}' 连接/服务异常：{self.last_error}")
                # ⚠️ 第一次失败就登记，**不等重连耗尽** —— 能力此刻就是没有的。
                #    等 5 次退避跑完（最长约 31 秒）才说，用户在这段时间里
                #    问 Nano 会得到一句"我没有这个能力"，那正是本项要修的问题。
                #    重复失败靠 health 自己的去重（同 code 只累加，不刷屏）。
                self._health_fault(e)
            finally:
                try:
                    await stack.aclose()
                except Exception:
                    pass
                self._ready.clear()

            if self._stop.is_set():
                break
            # 指数退避重连
            self._reconnect_attempts += 1
            if self._reconnect_attempts > _MAX_RECONNECT:
                self.status = ST_FAILED
                logger.warning(f"[MCP] '{self.name}' 重连 {_MAX_RECONNECT} 次仍失败，停止自动重连（可手动重试）")
                break
            delay = min(2 ** (self._reconnect_attempts - 1), 16)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

        if not self.enabled:
            self.status = ST_DISABLED
        elif self.status not in (ST_FAILED, ST_NEEDS_AUTH):
            self.status = ST_DISCONNECTED

    async def _open_session(self, stack: AsyncExitStack) -> "ClientSession":
        """在 worker task 内打开 transport streams + ClientSession。"""
        # 每次尝试重置，别拿上次的死因误导这次。⚠️ 旧的要关掉 —— 一个连不上的
        # server 会一直退避重连，不关就是一次一个临时文件句柄。
        if self._stderr_tap is not None:
            self._stderr_tap.close()
        self._stderr_tap = None
        if self.transport == "stdio":
            command = _expand(self.cfg.get("command", ""))
            args = [str(a) for a in _expand(self.cfg.get("args", []) or [])]
            # 合并 os.environ + 配置覆盖：Windows 下 npx 等需要 PATH，必须继承父进程 env
            env = {**os.environ, **{k: str(v) for k, v in _expand(self.cfg.get("env", {}) or {}).items()}}
            # Runtime-bundle-ready：若项目内置了 portable 运行时（runtime/ 下），把它们放进
            # PATH 最前面 → npx/node(Node 生态)、uvx/uv(Python 生态) 优先用内置的，不依赖
            # 用户系统装。这让端用户"纯粘贴 server 配置"对 stdio 生态 server 也成立
            # （npx/uvx 用到时自动下载包）。打包窗口只需把 portable node/uv 解压到
            # runtime/node、runtime/uv（或 runtime/bin），零 MCP 代码改动、零 MCP 知识。
            for _sub in ("node", "uv", "bin"):
                _rt = _ROOT / "runtime" / _sub
                if _rt.exists():
                    env["PATH"] = str(_rt) + os.pathsep + env.get("PATH", "")
            cwd = _expand(self.cfg.get("cwd")) or None
            params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
            # ⭐ `errlog` 是 SDK 给的唯一出口：子进程 stderr 会被写进这里。
            #    不接它的话，缺依赖这类失败在本进程只剩一句 `Connection closed`。
            self._stderr_tap = _StderrTap()
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=self._stderr_tap))
        else:
            url = _expand(self.cfg.get("url", ""))
            headers = {k: str(v) for k, v in _expand(self.cfg.get("headers", {}) or {}).items()}
            # streamablehttp_client 返回 (read, write, get_session_id)；只用前两个
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers or None)
            )
        # 会话默认读取超时只用于握手与工具列表刷新；工具调用在 _serve 里按调用方的超时单独给。
        _hs = _HANDSHAKE_TIMEOUT_STDIO if self.transport == "stdio" else _HANDSHAKE_TIMEOUT_HTTP
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=_hs)))
        return session

    async def _refresh_tools(self, session: "ClientSession") -> None:
        result = await session.list_tools()
        tools = []
        for t in result.tools:
            ann = getattr(t, "annotations", None)
            tools.append({
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                # MCP 工具注解：用于决定是否需要"在场确认"。透明度由界面上的工具卡片保证，
                # 确认卡只为明确标 destructive（不可逆/破坏性）的操作保留，不给只读/普通写 nag。
                "read_only": bool(getattr(ann, "readOnlyHint", False)) if ann else False,
                "destructive": bool(getattr(ann, "destructiveHint", False)) if ann else False,
            })
        self.tools = tools

    async def _serve(self, session: "ClientSession") -> None:
        """守在请求队列上服务工具调用。连接级错误会抛出 → 上层 _run 触发重连。"""
        from core.runtime import progress as _prog_bus
        while not self._stop.is_set():
            req = await self._req_q.get()
            if req is None:  # stop 哨兵
                return
            # 上限回收：一个话痨 server 在长连接里能把 stderr 写很久
            if self._stderr_tap is not None:
                self._stderr_tap.reset_if_huge()
            tool, args, fut, _prog_ref, _timeout = req
            if fut.done():  # 调用方已超时取消
                continue
            try:
                # ⭐⭐⭐ [2026-08-09] **MCP 协议原生就有进度通知，我们一直没接。**
                #
                # 🔴 上一版这里是 `session.call_tool(tool, args or {})` ——
                #    `progress_callback` 一直是 `None`，于是「长任务回看」那一眼
                #    对所有 MCP 永远是空的。这件事一度被记成「MCP 没有进度
                #    通道」，那是**把「我们没接」当成了「它没有」**。
                #    📌 **一个能力没被调用，不等于它不存在** ——
                #       判断「有没有」必须去看协议/SDK，不是去看我们的调用点。
                #
                # ⭐ SDK 侧只需要传这个回调：`BaseSession.send_request` 看到它
                #    非空就自动往 `params._meta.progressToken` 塞 request_id
                #    （已核实 `mcp/shared/session.py`），服务器据此才会推送。
                #    ⚠️ 所以**不传回调 = 服务器根本不会发** —— 这不是「服务器不支持」。
                #
                # ⚠️ 回调在事件循环里被调用，必须**极短且不抛**：
                #    `progress.report` 自己保证了永不抛异常。
                #    📌 一个观测通道的故障，不许变成被观测那件事的故障。
                _cb = None
                if _prog_ref:
                    async def _cb(progress: float, total: float | None,
                                  message: str | None, _r=_prog_ref, _t=tool):
                        _prog_bus.report(_r, message or _t,
                                         progress=progress, total=total)
                result = await session.call_tool(
                    tool, args or {}, progress_callback=_cb,
                    read_timeout_seconds=timedelta(seconds=_timeout + 5))
                if not fut.done():
                    fut.set_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
                # 连接级错误（流断裂/关闭）→ 抛出让 _run 重连；其它（如工具内部
                # 报错）已经回给 future，继续服务下一个请求。
                if _is_connection_error(e):
                    raise

    # ── 外部调用入口 ──────────────────────────────────────────────────────
    async def call_tool(self, tool: str, args: dict, timeout: float = _CALL_TIMEOUT_DEFAULT,
                        progress_ref: str = ""):
        """投递一次工具调用，等待结果。返回 mcp 的 CallToolResult。

        `progress_ref`：非空时订阅协议的进度通知，写进 `core.runtime.progress`
        那条总线 —— 长任务回看时读的就是它。空则完全不订阅（不发
        `progressToken`，服务器也就不会推）。
        """
        if not self.enabled:
            raise MCPError(f"server '{self.name}' is disabled")
        if self.status in (ST_DISCONNECTED, ST_DISABLED) or not (self._worker and not self._worker.done()):
            await self.start()
        if self.status != ST_CONNECTED:
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT)
            except asyncio.TimeoutError:
                raise MCPError(f"server '{self.name}' connection timed out ({self.status}): {self.last_error}")
        if self.status == ST_NEEDS_AUTH:
            raise MCPAuthError(f"server '{self.name}' requires authorization before use")
        if self.status != ST_CONNECTED:
            raise MCPError(f"server '{self.name}' is unavailable ({self.status}): {self.last_error}")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        await self._req_q.put((tool, args, fut, progress_ref, float(timeout)))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"tool '{tool}' call timed out ({timeout:.0f}s)")

    def snapshot(self) -> dict:
        """给设置卡 UI 用的状态快照。"""
        return {
            "name": self.name,
            "status": self.status if self.enabled else ST_DISABLED,
            "transport": self.transport,
            "tool_count": self.tool_count,
            "enabled": self.enabled,
            "last_error": self.last_error,
            # 给 UI 的**人话**：分类过的原因 + 恢复建议，一个字都不截断。
            "fault_code": self.last_fault[0] if self.last_fault else "",
            "fault_message": self.last_fault[1] if self.last_fault else "",
            "fault_hint": self.last_fault[2] if self.last_fault else "",
            "tools": [t["name"] for t in self.tools],
            # ⭐ 展开详情要用的三样。
            # ⚠️ `description` 三种来源同一个字段：官方写死 / 生成的存在 cfg 里。
            #    📌 一个字段三种来源，比三个字段各管一处好 —— 读的人不用问「该看哪个」。
            "description": (self.cfg.get("description")
                            or _BUILTIN_DESC.get(self.name, "")),
            # 🔴 command/args 原样给 —— 它是唯一能回答「这东西在我机器上跑什么」的信息。
            #    ⚠️ **env 一个字都不给**：里面可能有 token。
            # ⭐ 非空 = 它是某个 Skill 的内部零件：管理页不显示、模型清单不列、
            #    manage_mcp 拒绝对它动手。见 `owned_by` 的注释。
            "owned_by": self.owned_by,
            "command": str(self.cfg.get("command") or ""),
            "args": list(self.cfg.get("args") or []),
            "url": str(self.cfg.get("url") or ""),
            # 官方自带能力（如 fetch）：可禁用、不可删除（对齐官方 Skill 受保护模型）
            "builtin": bool(self.cfg.get("builtin", False)),
        }


class _StderrTap:
    """把 stdio 子进程的 stderr 留一份下来。

    🔴🔴 **这一层是实测推翻了纸面推断才发现要做的。**
       纸面上的判断是：「`mcp_server_fetch` 没装 → `ModuleNotFoundError: No module
       named 'mcp_server_fetch'` …… 这些原样落在 `last_error` 里。**缺的不是信息，
       是分类与恢复建议。**」
       **实测不是。** 子进程死掉时本进程拿到的只有 `McpError: Connection closed` ——
       模块名在**子进程的 stderr** 里，被 SDK 原样转发到控制台，没有任何人留存。

    ⚠️ 后果的严重性：缺依赖恰恰是这类故障最主要的形态。
       不做这一层，整套故障分类对它是**无效的** ——
       故障卡片上只会写「连接失败：Connection closed」，比原来那个截断好不了多少。
    📌 判据：**「信息已经有了，只差分类」这句话必须回代码验，不能照抄推断。**
       那个推断是对着 `last_error` 字段做的，而真正的信息**不在这个进程里**。

    ⚠️⚠️ 必须是**真文件**，不能是 `io.TextIOBase` 的子类：
       SDK 的 `errlog` 会被直接交给子进程当 stderr，需要真的 `fileno()`。
       第一版写成内存对象，实测抛 `UnsupportedOperation: fileno`。
    """

    _TAIL_BYTES = 8000      # 只读尾部：报错的结论在最后
    _MAX_BYTES = 1 << 20    # 1 MB 上限 —— 常驻进程里一个话痨 server 能写很久
                            # 📌 上限必须配回收：这里的回收就是就地截断

    def __init__(self) -> None:
        import tempfile
        self._f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    def fileno(self) -> int:
        return self._f.fileno()

    def tail(self) -> str:
        """读尾部内容。⚠️ 只读不动指针语义，读完把位置还回去。"""
        try:
            self._f.flush()
        except Exception:
            pass
        try:
            import os as _os
            fd = self._f.fileno()
            size = _os.fstat(fd).st_size
            start = max(0, size - self._TAIL_BYTES)
            with _os.fdopen(_os.dup(fd), "rb", closefd=True) as r:
                r.seek(start)
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    def reset_if_huge(self) -> None:
        try:
            import os as _os
            if _os.fstat(self._f.fileno()).st_size > self._MAX_BYTES:
                self._f.truncate(0)
                self._f.seek(0)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# 异常分类 —— 异常 → 稳定 code → 用户可读一句话 + 可执行的恢复建议
# ══════════════════════════════════════════════════════════════════════════
# 📌 **抄的是 `rag.py` 的 `_model_load_code()` + `_classify_model_load_error()`**
#    那一对现成的范式，只换映射表 —— 不发明新形状。
#
# ⭐ 这里要修的问题不是"信息不够"，是"信息没被分类"：
#    `npx` 不存在 → `FileNotFoundError/WinError 2`；
#    `mcp_server_fetch` 没装 → `ModuleNotFoundError: No module named 'mcp_server_fetch'`。
#    这些**原样就落在 `last_error` 里**。缺的从来是分类与恢复建议。
#
# ⚠️ code 是**稳定指纹的一部分**（health 用它去重 / 判断"根因变了"），
#    所以它必须只依赖异常形状，不能把 server 名或路径拼进去。

MCP_NODE_MISSING = "MCP_NODE_MISSING"
MCP_PYTHON_MODULE_MISSING = "MCP_PYTHON_MODULE_MISSING"
MCP_COMMAND_NOT_FOUND = "MCP_COMMAND_NOT_FOUND"
MCP_NEEDS_AUTH = "MCP_NEEDS_AUTH"
MCP_HANDSHAKE_TIMEOUT = "MCP_HANDSHAKE_TIMEOUT"
MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"
MCP_SDK_MISSING = "MCP_SDK_MISSING"
# ⚠️ npx 在、包不在 —— 与「命令找不到」是两件事：一个要装 Node，一个是包名/网络。
MCP_NPM_PACKAGE_MISSING = "MCP_NPM_PACKAGE_MISSING"

# Node 生态的入口命令。命中这些 → 缺的是 Node.js 本身，不是"某个命令拼错了"，
# 恢复建议完全不同（装 Node vs 检查配置）。
_NODE_LAUNCHERS = ("npx", "npm", "node", "npx.cmd", "npm.cmd", "node.exe")
_PY_LAUNCHERS = ("uvx", "uv", "python", "py", "python.exe", "uvx.exe")


# ⭐ **子进程 stderr 的信号优先于本进程的异常** —— 本进程那一侧几乎总是
#    `McpError: Connection closed`（子进程死了，管道断了），它说不出任何原因。
#    真正的死因写在子进程自己的 stderr 里。
_STDERR_SIGNALS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"No module named ['\"]?([A-Za-z0-9_.\-]+)"), MCP_PYTHON_MODULE_MISSING),
    (re.compile(r"could not determine executable to run"), MCP_NPM_PACKAGE_MISSING),
    (re.compile(r"npm (?:ERR!|error) (?:code )?E?404"), MCP_NPM_PACKAGE_MISSING),
    (re.compile(r"404 Not Found.*?(@[\w./-]+)"), MCP_NPM_PACKAGE_MISSING),
    (re.compile(r"is not recognized as an internal or external command"), MCP_COMMAND_NOT_FOUND),
    (re.compile(r"不是内部或外部命令"), MCP_COMMAND_NOT_FOUND),
    (re.compile(r"(?:401|403)|[Uu]nauthorized|[Ff]orbidden"), MCP_NEEDS_AUTH),
)


def _stderr_signal(stderr: str) -> tuple[str, str] | None:
    """从子进程 stderr 里认出死因 → (code, 关键细节)。认不出返回 None。"""
    if not stderr:
        return None
    for pat, code in _STDERR_SIGNALS:
        m = pat.search(stderr)
        if m:
            detail = (m.group(1) if m.groups() else "") or ""
            return code, detail
    return None


def _stderr_tail(stderr: str, limit: int = 300) -> str:
    """给「认不出来」时用的兜底：stderr 的最后几行非空内容。

    ⚠️ 取**尾部**不是头部 —— traceback 的结论在最后一行。
       📌 与那个 36 字符截断同一条教训：截断永远先切掉信息量最大的那一段。
    """
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return ""
    out = []
    for l in reversed(lines):
        out.insert(0, l)
        if sum(len(x) + 1 for x in out) > limit:
            break
    return " / ".join(out)[:limit]


def _mcp_error_code(err: Exception, command: str = "", stderr: str = "") -> str:
    """异常 → 稳定 code。**只看异常形状**，不看 server 名。"""
    sig = _stderr_signal(stderr)
    if sig:
        return sig[0]
    name = type(err).__name__
    text = f"{name}: {err}"
    low = text.lower()
    cmd = (command or "").strip().lower()
    base = cmd.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    if name == "ModuleNotFoundError" or "no module named" in low:
        return MCP_PYTHON_MODULE_MISSING
    if name in ("FileNotFoundError", "NotADirectoryError") or "winerror 2" in low             or "cannot find the file" in low or "系统找不到指定的文件" in text:
        # ⚠️ 分两类的理由：缺 Node 是**环境没装**（跑 install.bat 能修），
        #    缺别的命令多半是**配置写错了**（改 mcp_servers.json）。
        #    这两句给用户的下一步完全不同，混成一句等于没分类。
        return MCP_NODE_MISSING if base in _NODE_LAUNCHERS else MCP_COMMAND_NOT_FOUND
    if _looks_like_auth_error(err):
        return MCP_NEEDS_AUTH
    if name in ("TimeoutError", "asyncio.TimeoutError") or isinstance(err, asyncio.TimeoutError)             or "timed out" in low or "timeout" in low:
        return MCP_HANDSHAKE_TIMEOUT
    return MCP_CONNECT_FAILED


def _classify_mcp_error(err: Exception, server: str, command: str = "",
                        stderr: str = "") -> tuple[str, str, str, str]:
    """→ (code, 给用户/模型看的一句话, 恢复建议·中文, 恢复建议·英文)。

    ⚠️ 一句话里**必须带上 server 名**：粒度是按 server 登记的，
       用户同时挂三个 server 时「MCP 连接失败」这句话没有任何用处。
    ⚠️ 恢复建议两份：中文进界面的故障卡片（跟随 UI 语言），英文注入模型。
       📌 「一个字段不许表达两个现实」——这里的两个现实是**两个受众**。
    ⚠️ 措辞用**系统提示的语气**（祈使 + 可执行项），不要写成聊天口吻 ——
       故障卡不是在跟用户对话，它在陈述事实并给出下一步。
    """
    detail = f"{type(err).__name__}: {err}"
    # ⚠️ 同一套模式**两处都扫**：子进程 stderr 优先（真实死因在那），
    #    扫不到再扫本进程异常文本 —— 有些失败确实是本进程抛的
    #    （http transport、或 SDK 自己把子进程报错包进了异常）。
    #    📌 只扫一处的话，另一处的那一半会安静地退回「Connection closed」。
    sig = _stderr_signal(stderr) or _stderr_signal(detail)
    code = sig[0] if sig else _mcp_error_code(err, command)
    extra = sig[1] if sig else ""
    # ⚠️ 模块名/命令名要**原样带出来**，不许截断 —— 之前那个 36 字符的截断
    #    正是栽在"把最关键的那几个字切掉了"。
    table = {
        MCP_NODE_MISSING: (
            f"外部能力「{server}」不可用：启动它需要 {command or 'npx'}，当前系统未安装 Node.js。",
            "运行 install.bat 安装 Node.js LTS，或自行安装后重启 Nano。",
            "Install Node.js LTS (run install.bat in the project folder, or install it "
            "manually) and restart Nano.",
        ),
        MCP_PYTHON_MODULE_MISSING: (
            f"外部能力「{server}」不可用：缺少 Python 模块 {extra or '(见技术详情)'}。",
            (f"安装缺失模块：pip install {extra}，然后在设置的「MCP 连接」里点重试。" if extra
             else "在 Nano 使用的 Python 环境中安装缺失模块，然后在设置的「MCP 连接」里点重试。"),
            (f"Install the missing module: `pip install {extra}`, then hit retry in the "
             f"MCP settings panel." if extra else
             "Install the missing Python module in the environment Nano runs in, then retry."),
        ),
        MCP_NPM_PACKAGE_MISSING: (
            f"外部能力「{server}」不可用：{command or 'npx'} 可用，但无法获取所需的包"
            + (f"（{extra}）。" if extra else "。"),
            "核对包名是否正确、确认可访问 npm registry；首次下载中断也会导致此结果。",
            "Check the package name is correct and that the npm registry is reachable; "
            "an interrupted first-time download causes this too.",
        ),
        MCP_COMMAND_NOT_FOUND: (
            f"外部能力「{server}」不可用：找不到启动命令 {command or '(未配置)'}。",
            "核对 config/mcp_servers.json 中该 server 的 command 是否正确、程序是否已安装。",
            "Check the `command` field for this server in config/mcp_servers.json, and "
            "that the program is installed.",
        ),
        MCP_NEEDS_AUTH: (
            f"外部能力「{server}」需要授权：服务端返回未授权。",
            "在设置的「MCP 连接」里补充该 server 的凭据/令牌，然后点重试。",
            "Add the server's credentials or token in the MCP settings panel, then retry.",
        ),
        MCP_HANDSHAKE_TIMEOUT: (
            f"外部能力「{server}」连接超时：握手阶段无响应。",
            "确认网络与该 server 进程状态，然后在设置的「MCP 连接」里手动重试。",
            "Check the network and whether that server process is running, then retry from "
            "the MCP settings panel.",
        ),
        MCP_SDK_MISSING: (
            f"外部能力「{server}」不可用：本机没有可用的 mcp SDK。",
            "运行 install.bat 重装依赖（pip install mcp）。",
            "Reinstall dependencies by running install.bat in the project folder "
            "(`pip install mcp`).",
        ),
    }
    # ⚠️ 兜底也**不许只说 "Connection closed"** —— 那是本进程看到的表象，
    #    不是死因。子进程留下的最后几行才是。
    _tail = _stderr_tail(stderr)
    msg, hint, hint_en = table.get(code, (
        f"外部能力「{server}」连接失败：{_tail or detail}",
        "在设置的「MCP 连接」里展开这一行看完整报错，再手动重试。",
        "Open the MCP settings panel for the full error, then retry.",
    ))
    return code, msg, hint, hint_en


def _is_connection_error(err: Exception) -> bool:
    name = type(err).__name__
    return name in (
        "BrokenResourceError", "ClosedResourceError", "EndOfStream",
        "ConnectionError", "ConnectionResetError", "ConnectionClosed",
    )


class MCPError(RuntimeError):
    pass


class MCPAuthError(MCPError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# MCPManager —— 单例，统管所有 server
# ══════════════════════════════════════════════════════════════════════════
class MCPManager:
    _instance: "MCPManager | None" = None

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self._loaded = False
        self._config_path: Path = CONFIG_PATH   # 可被测试/部署覆盖
        # full_tool_name(mcp__server__tool) -> (server_name, raw_tool_name)
        self._tool_index: dict[str, tuple[str, str]] = {}

    @classmethod
    def instance(cls) -> "MCPManager":
        if cls._instance is None:
            cls._instance = MCPManager()
        return cls._instance

    @property
    def available(self) -> bool:
        return _MCP_SDK_OK

    # ── 配置 ──────────────────────────────────────────────────────────────
    def load_config(self, path: Path | str | None = None) -> None:
        path = Path(path) if path else self._config_path
        self._config_path = path
        raw = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"[MCP] 配置解析失败 {path}: {e}")
                raw = {}
        servers_cfg = (raw or {}).get("mcpServers", {}) or {}
        # 仅保留新配置里的 server；已存在的复用对象（保活连接）
        new_servers: dict[str, MCPServer] = {}
        for name, cfg in servers_cfg.items():
            if not isinstance(cfg, dict):
                continue
            if name in self.servers:
                self.servers[name].cfg = cfg
                new_servers[name] = self.servers[name]
            else:
                new_servers[name] = MCPServer(name, cfg)
        # 配置里被移除的 server：连能力登记一起撤掉（同 remove_server 那条理由）
        for _gone in set(self.servers) - set(new_servers):
            try:
                self.servers[_gone]._health_forget()
            except Exception:
                pass
        self.servers = new_servers
        self._loaded = True
        logger.debug(f"[MCP] 配置已加载：{len(self.servers)} 个 server（{', '.join(self.servers) or '空'}）")

    def refresh_config_from_disk(self) -> bool:
        """重新读配置文件，**就地更新已有 server 的 `cfg`**；新增的 server 也建出来。

        🔴 **它修的是一个实测抓到的真 bug**（2026-08-21）：
           `MCPServer.cfg` 是启动时读进内存的，之后**再也没人读过磁盘**。
           于是用户把 `mcp_servers.json` 改回正确值以后：
             · 探针每 30 秒去重连一次 —— **拿着旧参数**，永远失败
             · UI 上点「重试」—— 同样拿旧参数，看起来「按了没反应」
           日志里的表现是 `重复上报 code=… count=53`：**重试在跑，只是永远不可能成功。**
           📌 **一个只在启动时读一次的配置，等于要求用户重启才能改任何东西** ——
              而界面上同时摆着一个「重试」按钮，那个按钮承诺了别的事。

        ⚠️ **刻意不复用 `load_config()`**，两点区别：
          1. `load_config` 会用新字典整个替换 `self.servers`（含删除）。
             删除是**用户动作**，不该由一个后台探针顺手做掉。
          2. 解析失败时 `load_config` 会把 `servers` 置空。
             📌 用户正在编辑那个 JSON 的中途，文件必然有一瞬间是坏的 ——
                那一瞬间**不该让所有外接能力消失**。这里解析失败就原地返回 False，
                什么都不动。

        返回：是否成功读到并应用了配置。
        """
        path = self._config_path
        if not path or not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            # 半截的 JSON 是常态（用户正在打字），降到 debug，别刷屏
            logger.debug(f"[MCP] 配置暂时读不了（保持现状）: {e}")
            return False
        cfgs = (raw or {}).get("mcpServers", {}) or {}
        if not isinstance(cfgs, dict):
            return False
        changed = []
        for name, cfg in cfgs.items():
            if not isinstance(cfg, dict):
                continue
            s = self.servers.get(name)
            if s is None:
                self.servers[name] = MCPServer(name, cfg)
                changed.append(name)
            elif s.cfg != cfg:
                s.cfg = cfg
                changed.append(name)
        if changed:
            logger.info(f"[MCP] 配置已刷新：{', '.join(changed)}")
        return True

    # ── 连接 ──────────────────────────────────────────────────────────────
    async def connect_enabled(self) -> None:
        """启动所有 enabled 的 server（非阻塞，连接在各自 worker task 里异步进行）。"""
        if not self._loaded:
            self.load_config()
        if not _MCP_SDK_OK:
            # ⚠️ SDK 缺失时**也要登记**，不能直接 return —— 否则用户配了 5 个
            #    server，而 Nano 一句"我没有这些能力"就完了。
            #    📌 这正是本项要修的"最坏的一层"：它连"我曾经有过"都不知道。
            for _s in self.servers.values():
                if _s.enabled:
                    _s._health_fault(
                        None, code=MCP_SDK_MISSING,
                        msg=f"外部能力「{_s.name}」不可用：本机没有可用的 mcp SDK。",
                        hint="运行 install.bat 重装依赖（pip install mcp）。",
                        hint_en="Reinstall dependencies by running install.bat (`pip install mcp`).")
            return
        self._register_probes()
        for s in self.servers.values():
            # ⚠️ `lazy` 的跳过**只在这里**。别在 `start()` 里挡 ——
            #    那样连按需调用都起不来，而按需正是它存在的理由。
            # 📌 「开机不拉」和「永远不拉」是两件事，挡的位置决定了是哪一件。
            if s.enabled and not s.lazy:
                await s.start()
        asyncio.create_task(self._log_startup_summary())

    async def _log_startup_summary(self, timeout: float = 60.0) -> None:
        """等开机自动连接的 server 各自出第一次结果（或超时），打一行汇总。

        连接失败的原因由各 server 自己的 WARNING 给出，这里只报数量。
        """
        started = [s for s in self.servers.values() if s.enabled and not s.lazy]
        if not started:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if all(s.status in (ST_CONNECTED, ST_FAILED, ST_NEEDS_AUTH) for s in started):
                break
            await asyncio.sleep(0.5)
        ok = sum(1 for s in started if s.status == ST_CONNECTED)
        (logger.info if ok == len(started) else logger.warning)(
            f"[MCP] 已连接 {ok}/{len(started)} 个服务")

    # ── 恢复探针 ─────────────────────────────────────────────────────────
    def _register_probes(self) -> None:
        """给每个 server 登记存活探针。

        ⚠️ 没有它，MCP 会变成第四个「坏了就再也不会好」的能力：
           退避重连最多 5 次（约 31 秒）就永久放弃，而用户装好 Node.js
           往往是几分钟之后的事 —— 那时没有任何东西会再去试一次。
        ⭐ 探针**必须真的去重连**，不能只读 `status`：只读状态的探针
           永远返回 False（没人再去连它了），那是一个**看起来有、实际不解锁**的探针。
           📌 同「写好但零调用方，比没写更坏」。
        """
        try:
            from core.health import get_health
            h = get_health()
        except Exception:
            return
        for name, s in self.servers.items():
            h.register_probe(s.cap_key, self._make_probe(name))

    def _make_probe(self, name: str):
        def _probe() -> bool:
            s = self.servers.get(name)
            if s is None or not s.enabled:
                return False
            if s.status == ST_CONNECTED:
                return True
            # needs_auth 不自动重试（对齐连接语义：凭据是用户要给的，重试没用）
            if s.status == ST_NEEDS_AUTH:
                return False
            # 廉价：只投一个 task 就返回，不在探针里等连接结果。
            # 连上之后 `_health_ok()` 会自己上报恢复，不靠这次返回值。
            # ⭐ 每次探之前刷一遍配置：用户的修复动作**有一半是改配置**
            #    （改回包名/换 command），不刷的话探针永远拿旧参数重试。
            try:
                self.refresh_config_from_disk()
                s = self.servers.get(name) or s
                s._reconnect_attempts = 0
                asyncio.get_running_loop().create_task(s.start())
            except Exception:
                pass
            return False
        return _probe

    async def shutdown(self) -> None:
        for s in list(self.servers.values()):
            try:
                await s.stop()
            except Exception:
                pass

    # ── 工具聚合（给 orchestrator 注入用）────────────────────────────────
    def list_tool_manifests(self) -> list[dict]:
        """聚合所有【已连接】server 的工具，返回 manifest 列表（{name, description, parameters}）。
        name 形如 mcp__<server>__<tool>。同时刷新反解索引。"""
        manifests: list[dict] = []
        self._tool_index = {}
        for s in self.servers.values():
            if s.status != ST_CONNECTED:
                continue
            # 🔴 隐藏 server 的工具**永远不进目录**，连 `_tool_index` 都不进 ——
            #    索引进了就等于模型能调它（反解得到就能路由）。
            #    它只能被我们自己那个包装 Skill 通过 `call_hidden()` 走。
            if not s.expose_tools:
                continue
            for t in s.tools:
                full = self._make_tool_name(s.name, t["name"])
                self._tool_index[full] = (s.name, t["name"])
                desc = t["description"] or f"Capability provided by {s.name}"
                manifests.append({
                    "name": full,
                    "description": f"[{s.name}] {desc}",
                    "parameters": t["input_schema"] or {"type": "object", "properties": {}},
                })
        return manifests

    # ⚠️ 这里曾有 `awareness_lines()`（给感知块产 MCP 一行简述），2026-08-12 删除：
    #    **零调用方**。MCP 的按需感知实际走 orchestrator 的 `_build_deferred_awareness`
    #    （已压缩 browser 工具），这个是**重复的、从没被接上**的版本。
    #    📌 一个写好但零调用方、docstring 还写着「给 xxx 用」的函数，比没写更坏——
    #       缺口看起来补上了，实际没有。

    @staticmethod
    def _make_tool_name(server: str, tool: str) -> str:
        safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server)
        safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", tool)
        return f"{TOOL_PREFIX}{safe_server}__{safe_tool}"

    def is_mcp_tool(self, name: str) -> bool:
        return isinstance(name, str) and name.startswith(TOOL_PREFIX)

    def server_of(self, full_name: str) -> str:
        """工具全名 → 它属于哪个 server。查不到返回 ""。

        ⚠️ **不能切字符串再查字典** —— `_make_tool_name` 把 server 名里的
           非法字符转义过（`pdf-extract` → `pdf_extract`），切出来的那段
           在 `self.servers` 里根本不存在。
        📌 **能查到声明就别去猜字符串** ——
           所以这里遍历真实的 server 列表、用**同一个转义规则**算出前缀再比。

        ⚠️ 取**最长匹配**：server "a" 与 "a__b" 同时存在时，
           前缀 `mcp__a__` 对两者都成立，短的那个会把长的抢走。
        """
        if not self.is_mcp_tool(full_name):
            return ""
        best = ""
        for name in self.servers:
            pre = f"{TOOL_PREFIX}{re.sub(r'[^A-Za-z0-9_]', '_', name)}__"
            if full_name.startswith(pre) and len(pre) > len(best):
                best, hit = pre, name
        return hit if best else ""

    def disabled_tool_server(self, full_name: str) -> str:
        """这个工具名属于一个**被禁用**的 server 吗？是就返回 server 名。

        🔴 **为什么需要它**（2026-08-28，对齐 Skill 那套时发现的）：
           `list_tool_manifests()` 只收 `ST_CONNECTED` 的 server ——
           一个被禁用的 MCP，它的工具**从目录里彻底消失**。
           于是模型调它时会被判成 `UNKNOWN_TOOL`（"这个名字不存在"），
           而 Skill 那边专门写过这是**撒谎**：它明明存在，只是被关了。

        ⚠️ MCP 比 Skill 更棘手：Skill 的 manifest 是本地文件，禁用了照样读得到；
           **MCP 的工具清单要连上才知道**，从没连过的 disabled server，
           我们不可能知道它有哪些工具。
        ⭐ 但 MCP 的工具名**自带 server 名**（Skill 没有这个）——
           ⇒ 判据下移一层：从「这个**工具**被禁用了」下移到
             「它所属的**服务**被禁用了」，而后者永远查得到。
           📌 答不出精确的那一问时，答一个**同样有用且答得准**的问题，
              比猜一个精确答案强。
        """
        name = self.server_of(full_name)
        if not name:
            return ""
        s = self.servers.get(name)
        return name if (s is not None and not s.enabled) else ""

    # ══════════════════════════════════════════════════════════════════════
    # 「互联网检索」监控卡的语义 —— 三级，不是二值
    # ══════════════════════════════════════════════════════════════════════
    # 🔴 **这里曾是 `has_web_capability()`**：扫【已连接】server 的工具名+描述，
    #    撞上 11 个关键词里任意一个就返回 True。两个问题：
    #      ① 只扫 MCP，看不到 Skill —— 而「找」这一层是官方 Skill，做好了也不变绿
    #      ② 关键词太宽等于常绿 —— playwright 一连上就绿，但那 ≠ 能搜索
    #    📌 **一个永远为真的判断，比没有这个判断更坏** —— 它看起来在检查。
    #
    # ⭐ **为什么是三级而不是「三层都没坏」**：三层是**递进降级**关系，不是
    #    全都必需。缺「找」和缺「读」的后果完全不同，一个二值绿灯把它们抹平了。
    #
    #      ONLINE   找 ✅ + 读 ✅        正常（兜底可选）
    #      LIMITED  只剩其中一层        🔴 缺「找」= 只能读给定 URL，搜不了
    #                                   🔴 缺「读」= 只剩摘要，点不进去
    #      OFFLINE  连读都没有
    #
    # 🔴🔴 **context7 / microsoft-learn 故意不算进「找」**。
    #    它们确实能查到东西，但只能查库和框架的官方文档，回答不了
    #    「这个报错怎么解」。把它们算进来 = **让这张卡在真的搜不了的时候仍然
    #    显示 ONLINE**，那就退回今天刚修掉的那个问题了。
    #    📌 判据不是「它有没有用」，是「它顶不顶得掉那一层的职责」。

    WEB_ONLINE = "ONLINE"
    WEB_LIMITED = "LIMITED"
    WEB_OFFLINE = "OFFLINE"

    def web_providers(self, cap: str) -> list[str]:
        """哪些**已连接**的 server 声明了自己提供 `cap`。"""
        return [n for n, s in self.servers.items()
                if s.status == ST_CONNECTED and cap in s.declared_caps()]

    def can_fetch(self) -> bool:
        """「读」这一层 —— 有没有人能抓一个给定 URL 的正文。

        ⚠️ 当场算，不落健康登记表。理由见 `core/health.py` 里 WEB_FETCH 的墓碑：
           它没有任何属于自己的失败态，`mcp.server.<name>` 已经在报了。
        """
        return bool(self.web_providers("web.fetch"))

    def can_search(self) -> bool:
        """「找」这一层 —— 由 `SearchTheWeb` 这个官方 Skill 提供，不是 MCP。

        ⭐ 所以它**必须**走健康登记表：Skill 不在 `self.servers` 里，
           这个 manager 没有任何别的办法知道它的死活。
           📌 这正是原来那个实现的根因 ①「只扫 MCP，看不到 Skill」。
        ⚠️ 从没跑过（`get` 返回 None）当作**可用** —— 它是随包装的官方 Skill，
           默认在。只有真失败过才变红。
           📌 反过来（默认不可用）会让一张卡在全新安装上一开始就是红的，
              而那时什么都没坏。
        """
        try:
            from core.health import get_health, Cap
            st = get_health().get(Cap.WEB_SEARCH)
        except Exception:
            return True
        return True if st is None else st.ok

    def web_status(self) -> str:
        """给「互联网检索」卡的三级状态。"""
        found, read = self.can_search(), self.can_fetch()
        if found and read:
            return self.WEB_ONLINE
        if found or read:
            return self.WEB_LIMITED
        return self.WEB_OFFLINE

    async def call_hidden(self, server: str, tool: str, args: dict,
                          timeout: float = _CALL_TIMEOUT_DEFAULT) -> str:
        """调一个**隐藏 server** 的工具，返回拼好的文本。

        ⭐ 为什么不走 `call()`：`call()` 靠 `_tool_index` 路由，而隐藏 server
           压根不进那张索引（进了就等于模型能调它）。这里按名字直取。
        ⭐ 懒启动**不用我们操心** —— `MCPServer.call_tool` 自己会在断开时
           `await self.start()` 再等 ready（见该方法开头）。
        ⚠️ 只对 `expose_tools=False` 的 server 开放。这不是洁癖：如果它能调
           普通 server，就等于开了一条**绕过工具目录**的旁路，而
           「调用必须受本次 request 的工具合同约束」这条刚刚才立起来。
           📌 一个新加的通道，不该顺手把刚立起来的约束捅个洞。
        """
        s = self.servers.get(server)
        if s is None:
            raise MCPError(f"server '{server}' is not configured")
        if s.expose_tools:
            raise MCPError(f"server '{server}' is a normal server; call it through the tool catalog")
        res = await s.call_tool(tool, args or {}, timeout=timeout)
        out = []
        for c in (getattr(res, "content", None) or []):
            t = getattr(c, "text", "") or ""
            if t:
                out.append(t)
        return "\n".join(out)

    def web_status_detail(self) -> dict:
        """给 tooltip / 设置页：三级 + 到底缺了哪一层。"""
        found, read = self.can_search(), self.can_fetch()
        return {
            "status": self.web_status(),
            "can_search": found,
            "can_fetch": read,
            "fetch_providers": self.web_providers("web.fetch"),
        }

    def confirm_summary(self, full_name: str) -> str | None:
        """若该 MCP 工具被 server 明确标为 destructive（不可逆/破坏性），返回一句
        确认摘要（→ 上层弹"在场确认"卡）；否则返回 None（直接执行，透明度由界面上的工具卡片保证）。
        对齐"范围感知：可逆操作直接做、不可逆先确认"。"""
        entry = self._tool_index.get(full_name)
        if not entry:
            self.list_tool_manifests()
            entry = self._tool_index.get(full_name)
        if not entry:
            return None
        server_name, raw = entry
        srv = self.servers.get(server_name)
        if not srv:
            return None
        for t in srv.tools:
            if t["name"] == raw and t.get("destructive"):
                return f"{server_name} · {raw}（外部操作，可能不可逆）"
        return None

    # ── 调用路由 ──────────────────────────────────────────────────────────
    async def call(self, full_name: str, args: dict, timeout: float = _CALL_TIMEOUT_DEFAULT,
                   progress_ref: str = ""):
        """按 mcp__server__tool 路由到对应 server 执行。返回 (text, is_error, needs_auth, server)。

        `progress_ref` 透传给 server —— 见 `MCPServer.call_tool`。
        """
        if full_name not in self._tool_index:
            # 索引可能过期（manifest 在另一轮构建）；重建一次再找
            self.list_tool_manifests()
        entry = self._tool_index.get(full_name)
        if not entry:
            return (f"Unknown MCP tool: {full_name}", True, False, "")
        server_name, raw_tool = entry
        server = self.servers.get(server_name)
        if not server:
            return (f"MCP server '{server_name}' does not exist", True, False, server_name)
        try:
            result = await server.call_tool(raw_tool, args or {}, timeout=timeout,
                                            progress_ref=progress_ref)
            return (self._stringify_result(result), bool(getattr(result, "isError", False)), False, server_name)
        except MCPAuthError as e:
            return (str(e), True, True, server_name)
        except Exception as e:
            return (f"MCP tool execution failed: {type(e).__name__}: {e}", True, False, server_name)

    @staticmethod
    def _stringify_result(result: Any) -> str:
        """把 mcp CallToolResult 的 content 拍平成文本（给模型看）。"""
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text" or hasattr(block, "text"):
                parts.append(getattr(block, "text", "") or "")
            elif btype == "image":
                parts.append("[Image content]")
            elif btype == "resource":
                res = getattr(block, "resource", None)
                parts.append(getattr(res, "text", None) or f"[Resource {getattr(res, 'uri', '')}]")
            else:
                parts.append(str(block))
        text = "\n".join(p for p in parts if p).strip()
        # 结构化输出（部分 server 走 structuredContent）兜底
        if not text:
            sc = getattr(result, "structuredContent", None)
            if sc:
                try:
                    text = json.dumps(sc, ensure_ascii=False)
                except Exception:
                    text = str(sc)
        return text or "(tool returned no text output)"

    # ── 状态快照（设置卡 UI）────────────────────────────────────────────
    def status_snapshot(self) -> list[dict]:
        if not self._loaded:
            self.load_config()
        return [s.snapshot() for s in self.servers.values()]

    # ── 配置变更（设置卡 UI 的后端；用户永远不碰原始 json）────────────────
    def save_config(self, path: Path | str | None = None) -> None:
        """把当前 servers 的 cfg 写回 config 文件，保留文件里的 _comment 等附加字段。"""
        path = Path(path) if path else self._config_path
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["mcpServers"] = {name: s.cfg for name, s in self.servers.items()}
        try:
            path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"[MCP] 配置写回失败 {path}: {e}")

    async def set_enabled(self, name: str, enabled: bool) -> None:
        s = self.servers.get(name)
        if not s:
            return
        s.cfg["enabled"] = bool(enabled)
        self.save_config()
        if enabled:
            await s.start()
        else:
            await s.stop()
            s._health_forget()   # 禁用是用户的决定，不该留一张用户关不掉的故障卡片

    async def remove_server(self, name: str) -> bool:
        """删除 server。官方自带（builtin）受保护，拒绝删除（只能禁用）。返回是否删除。"""
        s = self.servers.get(name)
        if s and s.cfg.get("builtin"):
            logger.info(f"[MCP] '{name}' 是官方自带能力，受保护不可删除（可禁用）")
            return False
        s = self.servers.pop(name, None)
        if s:
            await s.stop()
            # ⚠️ 能力表与健康状态一起清 —— 否则删掉的 server 会在能力边界里
            #    永久挂着一条 UNAVAILABLE，用户**没有任何办法把它关掉**。
            #    📌 登记入口配注销入口，同「上限必须配回收」。
            s._health_forget()
        self.save_config()
        return True

    async def retry_server(self, name: str) -> None:
        """手动重试：重置重连计数并重新连接（失败/needs_auth 后用）。"""
        # ⭐ 先从磁盘刷一次配置 —— 「重试」这个按钮对用户的承诺是
        #    「我刚改完，再试一次」，而不是「拿启动时那份再撞一次墙」。
        self.refresh_config_from_disk()
        s = self.servers.get(name)
        if not s:
            return
        await s.stop()
        s._reconnect_attempts = 0
        s.status = ST_DISCONNECTED
        s.last_error = ""
        if s.enabled:
            await s.start()

    def preview_server_json(self, text: str) -> tuple[bool, dict | str]:
        """解析一段 server 配置，**只解析、不写盘、不连接**。

        返回 `(ok, info | 错误信息)`；`info` 是给授权弹窗看的：
        ```
        {name, transport, command, args, url, env_keys, raw_cfg}
        ```

        🔴🔴 **为什么必须有这个「只看不动」的函数**：
           对 stdio 来说，「连上」本身就是**在本机执行第三方代码**
           （`npx unknown-mcp-server` 已经跑起来了）。
           ⇒ 不能为了看看它有哪些工具先执行它、再问用户要不要允许执行它。
           📌 **因果倒置的授权等于没有授权。**
        ⇒ 所以授权前这一步只做三件事：解析 JSON、取出身份、取出**它会运行什么**。
           一行第三方代码都不执行，配置文件也一个字都不写。

        ⚠️ `env` 只报 **key 名**不报值 —— 用户粘来的配置里可能带 token。
           📌 弹窗要让人看清「它要什么」，而不是把密钥又抄一遍到屏幕上。
        """
        text = (text or "").strip()
        if not text:
            return (False, "请提供 server 配置")
        try:
            data = json.loads(text)
        except Exception as e:
            return (False, f"不是合法 JSON：{e}")
        if not isinstance(data, dict):
            return (False, "配置必须是 JSON 对象")
        servers_map = data.get("mcpServers") if "mcpServers" in data else data
        if not isinstance(servers_map, dict) or not servers_map:
            return (False, "没找到 server 配置")
        if len(servers_map) != 1:
            # ⚠️ 一次只批一个 —— 📌 一次弹窗批两个 server，用户很难说清
            #    自己同意的是哪个；而「部分同意」这件事这个弹窗表达不了。
            return (False, f"一次只能接入一个 MCP，这段配置里有 {len(servers_map)} 个")
        name, cfg = next(iter(servers_map.items()))
        if not isinstance(cfg, dict):
            return (False, f"「{name}」的配置不是 JSON 对象")
        if name in self.servers:
            return (False, f"「{name}」已经在配置里了")
        _cmd = str(cfg.get("command") or "")
        _url = str(cfg.get("url") or "")
        if not _cmd and not _url:
            return (False, f"「{name}」既没有 command 也没有 url，无法判断它怎么启动")
        return (True, {
            "name": name,
            "transport": str(cfg.get("type") or ("http" if _url else "stdio")),
            "command": _cmd,
            "args": list(cfg.get("args") or []),
            "url": _url,
            # ⚠️ 只给 key 名，不给值
            "env_keys": sorted((cfg.get("env") or {}).keys())
                        if isinstance(cfg.get("env"), dict) else [],
            "raw_cfg": cfg,
        })

    def set_description(self, name: str, text: str) -> bool:
        """给一个 server 存一句人话说明（tooltip），随配置持久化。

        ⭐ 它同时是三处的同一份文案：
           管理页的说明、模型侧的 awareness 补充、以及删除确认时能答上的那句。
           📌 写一次用三处 —— 而不是每处各编一句。
        """
        s = self.servers.get(name)
        if s is None:
            return False
        s.cfg["description"] = (text or "").strip()[:200]
        try:
            self.save_config()
        except Exception as e:
            logger.warning(f"[MCP] 保存 description 失败: {e}")
            return False
        return True

    def add_server_from_json(self, text: str) -> tuple[bool, str]:
        """解析用户粘贴的 server 配置（生态标准 snippet），加进配置并持久化（不自动连）。
        接受：{"mcpServers": {name: cfg}} / 裸 {name: cfg} 映射。返回 (ok, 已添加名字 或 错误信息)。"""
        text = (text or "").strip()
        if not text:
            return (False, "请粘贴 server 配置")
        try:
            data = json.loads(text)
        except Exception as e:
            return (False, f"不是合法 JSON：{e}")
        if not isinstance(data, dict):
            return (False, "配置必须是 JSON 对象")
        if "mcpServers" in data:
            servers_map = data["mcpServers"]
        elif "command" in data or "url" in data:
            return (False, '请用 {"server名字": {配置}} 形式包一层，给它起个名字')
        else:
            servers_map = data
        if not isinstance(servers_map, dict) or not servers_map:
            return (False, "没找到 server 配置")
        added = []
        for name, cfg in servers_map.items():
            if not isinstance(cfg, dict):
                return (False, f"server '{name}' 配置格式不对")
            if not cfg.get("command") and not cfg.get("url"):
                return (False, f"server '{name}' 缺 command 或 url")
            cfg.setdefault("enabled", True)
            if name in self.servers:
                self.servers[name].cfg = cfg
            else:
                self.servers[name] = MCPServer(name, cfg)
            added.append(name)
        self.save_config()
        return (True, ", ".join(added))


def get_mcp_manager() -> MCPManager:
    return MCPManager.instance()
