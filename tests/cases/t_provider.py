# -*- coding: utf-8 -*-
"""core/provider.py 的测试。

- endpoint_models：没有 API key 时不探测端点的模型清单。

用法：
  py -3.10 tests\cases\t_provider.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="nano_test_"))

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_endpoint_models_without_key() -> None:
    print("\n[8] 没有 API key 时不探测端点模型清单")
    from core import provider as P
    calls: list = []
    orig = P.fetch_endpoint_models
    P.fetch_endpoint_models = lambda *a, **k: calls.append(a) or []
    try:
        P._ENDPOINT_MODELS_MEM.clear()
        ids = P.endpoint_models("", "", "anthropic")
        check(calls == [], "未发起探测请求", str(calls))
        check(len(ids) > 0, "返回厂商表中的模型清单", f"{len(ids)} 个")
        check(not P._ENDPOINT_MODELS_MEM, "结果未写入进程内缓存（配置 key 后需要重新探测）")
        P.endpoint_models("", "sk-test", "anthropic")
        check(len(calls) == 1, "有 key 时照常探测")
    finally:
        P.fetch_endpoint_models = orig
        P._ENDPOINT_MODELS_MEM.clear()


def t_empty_tool_input_diag() -> None:
    print("\n[9] tool_use 参数为空时只对可疑情形报警")
    from core import provider as P
    from core.tools.manifests import BUILTIN_MANIFESTS
    manifests = list(BUILTIN_MANIFESTS.values())
    no_req = next(m for m in manifests if m["name"] == "end_screen_task")
    with_req = next(m for m in manifests
                    if (m.get("parameters") or m.get("input_schema") or {}).get("required"))
    check(not (no_req.get("parameters") or no_req.get("input_schema") or {}).get("required"),
          "前提：end_screen_task 没有必填参数")
    check(not P.empty_tool_input_suspicious("end_screen_task", "tool_use", manifests),
          "没有必填参数的工具传空参数 → 不报警")
    check(P.empty_tool_input_suspicious(with_req["name"], "tool_use", manifests),
          f"有必填参数的工具（{with_req['name']}）传空参数 → 报警")
    check(P.empty_tool_input_suspicious("end_screen_task", "max_tokens", manifests),
          "响应被 max_tokens 截断 → 报警")
    check(P.empty_tool_input_suspicious("NotInThisRequest", "tool_use", manifests),
          "不在本次工具表里的工具 → 报警")


def main() -> int:
    t_endpoint_models_without_key()
    t_empty_tool_input_diag()

    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
