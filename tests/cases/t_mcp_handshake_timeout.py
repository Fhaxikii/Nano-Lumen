# -*- coding: utf-8 -*-
"""MCP 连接握手超时：远端接受连接却不回应时，不会永远停在「连接中」。

没有握手超时时，worker 停在 initialize / list_tools 的 await 上：状态一直是 connecting，
既不算失败也不重连，界面上没有「重试」可点。现在每个握手请求有读取超时，超时按一次
失败处理，走既有的「退避重连 → 超过次数停止 → 手动重试」。

工具调用不受会话默认超时影响：调用方的超时随请求一起进入队列，调用时单独传给 SDK。

用法：
  py -3.10 tests\\cases\\t_mcp_handshake_timeout.py
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import find_def, module_text  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_silent_server_fails() -> None:
    print("\n▶ 不回应的 stdio 服务：超时后变为失败")
    from core import mcp_client as M
    saved = M._HANDSHAKE_TIMEOUT_STDIO
    M._HANDSHAKE_TIMEOUT_STDIO = 2.0

    async def run():
        s = M.MCPServer("silent", {
            "type": "stdio", "command": sys.executable,
            "args": ["-c", "import time; time.sleep(120)"],   # 进程活着，但从不回应
        })
        seen, t0 = set(), time.monotonic()
        await s.start()
        while time.monotonic() - t0 < 12:
            seen.add(s.status)
            if s.status == M.ST_FAILED:
                break
            await asyncio.sleep(0.2)
        elapsed = time.monotonic() - t0
        err = s.last_error
        await s.stop()
        return seen, elapsed, err

    try:
        seen, elapsed, err = asyncio.run(run())
    finally:
        M._HANDSHAKE_TIMEOUT_STDIO = saved
    check(M.ST_CONNECTING in seen, "先进入 connecting", str(seen))
    check(M.ST_FAILED in seen, "握手超时后状态变为 failed（不会一直 connecting）", f"{seen} after {elapsed:.1f}s")
    check(elapsed < 10, "在超时量级内失败，而不是无限等待", f"{elapsed:.1f}s")
    check("timed out" in err.lower() or "timeout" in err.lower(), "last_error 说明是超时", err[:80])


def t_wiring() -> None:
    print("\n▶ 接线")
    src = module_text("core.mcp_client")
    check("_HANDSHAKE_TIMEOUT_HTTP" in src and "_HANDSHAKE_TIMEOUT_STDIO" in src, "两种传输各有握手超时")
    open_s = ast.unparse(find_def("core.mcp_client", "_open_session", owner="MCPServer"))
    check("read_timeout_seconds=" in open_s, "ClientSession 带默认读取超时")
    serve = ast.unparse(find_def("core.mcp_client", "_serve", owner="MCPServer"))
    check("read_timeout_seconds=" in serve and "_timeout" in serve,
          "工具调用按调用方的超时单独传给 SDK（不被握手超时截断）")
    call = ast.unparse(find_def("core.mcp_client", "call_tool", owner="MCPServer"))
    check("float(timeout)" in call, "调用方的超时随请求进入队列")


if __name__ == "__main__":
    print("=" * 74)
    print("MCP 连接握手超时")
    print("=" * 74)
    t_silent_server_fails()
    t_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
