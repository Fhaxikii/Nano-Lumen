# -*- coding: utf-8 -*-
"""OS 工具说明中的参数示例必须与执行层实际读取的参数名一致。

os_execute / computer_use 的 `params` 说明里写着 `action={key,...}` 形式的示例，
模型按示例传参。示例里的参数名若执行层不读，调用必然失败
（例：`launch_app={name}`，执行层只读 target / app / path）。

检查方式：解析两份说明里的全部示例，按 dispatch 的路由表找到执行方法，
确认每个参数名都出现在该方法（及其 `_<方法名>_sync`）的源码里。
带定位的动作（click 等）的 `target` 由 dispatch 的定位前置读取，单独放行。

用法：
  py -3.10 tests\\t_os_param_examples.py
"""
from __future__ import annotations

import inspect
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_EXAMPLE = re.compile(r"([a-z_/]+)=\{([^}]*)\}")


def _examples(desc: str) -> list[tuple[str, list[str]]]:
    """`a/b={x,y:'...'}` → [("a", ["x","y"]), ("b", ["x","y"])]"""
    out = []
    for acts, body in _EXAMPLE.findall(desc):
        keys = []
        for part in body.split(","):
            k = part.split(":")[0].strip().strip("'\"")
            if k:
                keys.append(k)
        for a in acts.split("/"):
            if a:
                out.append((a, keys))
    return out


def _method_source(action: str) -> str:
    from core.os_layer import dispatch as D
    from core.os_layer.executor_low import LowLevelExecutor
    from core.os_layer.executor_write import WriteExecutor
    from core.os_layer.executor_action import ActionExecutor
    classes = {"_low": LowLevelExecutor, "_write": WriteExecutor, "_action": ActionExecutor}
    attr, meth = D._ROUTE_SPEC[action]
    cls = classes[attr]
    src = inspect.getsource(getattr(cls, meth))
    # 入口方法委托给的同类方法（只跟一层）：self._xxx(...) 直接调用，或 self._xxx 作为
    # 参数传给 asyncio.to_thread，例如 _launch_app_sync、_run_command_async、_win_op
    for helper in set(re.findall(r"self\.(_[A-Za-z0-9_]+)\b", src)):
        fn = getattr(cls, helper, None)
        if callable(fn):
            try:
                src += inspect.getsource(fn)
            except (OSError, TypeError):
                pass
    return src


def t_examples() -> None:
    from core.tools import manifests as _MF
    from core.os_layer import dispatch as D
    seen = 0
    for mname in ("_OS_MANIFEST", "_COMPUTER_USE_MANIFEST"):
        man = getattr(_MF, mname)
        desc = man["parameters"]["properties"]["params"]["description"]
        ex = _examples(desc)
        print(f"\n▶ {man['name']}：{len(ex)} 条示例")
        for action, keys in ex:
            if action not in D._ROUTE_SPEC:
                check(False, f"{man['name']} 示例 {action} 在路由表里存在", "无执行器")
                continue
            src = _method_source(action)
            for k in keys:
                if k == "target" and action in D._LOCATE_ACTIONS:
                    ok = True
                else:
                    ok = f'"{k}"' in src or f"'{k}'" in src
                check(ok, f"{man['name']} · {action}={{{k}}} 被执行层读取")
                seen += 1
    check(seen >= 10, "解析到了足够多的示例（解析器没有失效）", f"n={seen}")


def t_parser() -> None:
    print("\n▶ 示例解析器")
    check(_examples("launch_app={target}.") == [("launch_app", ["target"])], "单个")
    check(_examples("win_switch/win_close={title}") == [("win_switch", ["title"]), ("win_close", ["title"])],
          "斜杠分隔的多个动作")
    check(_examples("click={target:'x'}") == [("click", ["target"])], "带示例值的参数名")
    check(_examples("screenshot={}") == [("screenshot", [])], "空参数")


def t_scroll_direction() -> None:
    print("\n▶ scroll 按 direction 决定方向")
    import asyncio
    import types
    from core.os_layer.executor_action import ActionExecutor
    calls = []
    fake = types.ModuleType("pyautogui")
    fake.scroll = lambda n: calls.append(n)
    saved = sys.modules.get("pyautogui")
    sys.modules["pyautogui"] = fake
    try:
        ex = ActionExecutor.__new__(ActionExecutor)
        ex._estop = types.SimpleNamespace(is_stopped=lambda: False)

        def run(p):
            calls.clear()
            r = asyncio.run(ex.scroll(p))
            return r, (calls[0] if calls else None)

        r, n = run({"direction": "down", "amount": 3})
        check(r["ok"] and n == -300, "down + 正数 amount → 向下", str(n))
        r, n = run({"direction": "up", "amount": 3})
        check(r["ok"] and n == 300, "up → 向上", str(n))
        r, n = run({"direction": "up", "amount": -3})
        check(r["ok"] and n == 300, "up + 负数 amount → 仍向上（方向以 direction 为准）", str(n))
        r, n = run({"amount": -2})
        check(r["ok"] and n == -200, "不给 direction：沿用 amount 的正负号", str(n))
        r, n = run({"direction": "left", "amount": 3})
        check(not r["ok"] and n is None, "不支持的方向：报错且不滚动", str(r.get("error")))
    finally:
        if saved is not None:
            sys.modules["pyautogui"] = saved
        else:
            sys.modules.pop("pyautogui", None)


if __name__ == "__main__":
    print("=" * 74)
    print("OS 工具说明的参数示例 ↔ 执行层")
    print("=" * 74)
    t_parser()
    t_examples()
    t_scroll_direction()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
