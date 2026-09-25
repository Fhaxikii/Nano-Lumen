# -*- coding: utf-8 -*-
"""测试里替换产品代码的模块级名字（函数、常量）。

一个名字被包里多个模块 import 时，调用方在**它自己模块**的全局里查找这个名字；
只替换包顶层（`core.orchestrator.X = ...`）在调用方看来什么都没变，测试会悄悄
跑真实实现。`patch_global` 把包内所有持有同一个对象的模块一起替换。
"""
from __future__ import annotations

import sys
from typing import Any, Callable


def patch_global(package: str, name: str, value: Any) -> Callable[[], None]:
    """把 `package` 及其已导入子模块里的 `name` 统一替换为 `value`，返回还原函数。

    只替换与包顶层同一对象的那些模块属性。一个都没有替换时抛 `LookupError`
    （名字写错或已改名时，测试不能悄悄变成什么都没替换）。
    """
    top = sys.modules.get(package)
    if top is None:
        raise LookupError(f"package {package!r} is not imported")
    if not hasattr(top, name):
        raise LookupError(f"{package}.{name} does not exist")
    original = getattr(top, name)
    touched = []
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not (mod_name == package or mod_name.startswith(package + ".")):
            continue
        if getattr(mod, name, None) is original:
            setattr(mod, name, value)
            touched.append(mod)
    if not touched:
        raise LookupError(f"{package}.{name}: nothing replaced")

    def restore() -> None:
        for mod in touched:
            setattr(mod, name, original)

    return restore
