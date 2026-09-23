# -*- coding: utf-8 -*-
"""控制台输出保护 —— 所有测试脚本第一行 import 它。

═══ 为什么需要这个 ═══

用户的 cmd 默认是 GBK（chcp 936）。GBK 编不出 `⭐`(U+2B50) / `⚠`(U+26A0) /
`↳`(U+21B3) / emoji，`print` 到这种控制台会抛 `UnicodeEncodeError`。

危害不是"显示难看"，而是**纯展示动作变成了崩溃点**：

    check(ok, "⭐ 旧 span 也被一起收掉了")
        → print 抛异常
        → 整个用例被中止
        → 断言结果丢失，但前面的 PASS 已经打出来了

于是它长得像"测试通过了一部分然后崩了"，而实际情况是**断言根本没被计数**。
这已经在 发生过两次（`↳` 一次、`⭐` 一次），是 那条教训的同族：
**期望展示的东西不许成为失败源**。

═══ 为什么是 errors='replace' 而不是改成 UTF-8 ═══

把 stdout 改成 UTF-8 会让 GBK 控制台显示乱码 —— 中文断言名全部不可读，
比丢几个符号糟得多。保留控制台自己的编码、只把编不出的字符换成 `?`，
是唯一"最坏情况仍可读"的选项。
"""
from __future__ import annotations

import sys

import tests._sandbox  # noqa: F401  测试数据目录隔离，必须先于任何 core 模块导入


def install() -> None:
    """幂等。重复调用无副作用。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # Python 3.7+
        except Exception:
            # 被重定向到管道/StringIO 时可能没有 reconfigure。
            # 那些场景本来就不是 GBK 控制台，忽略即可。
            pass


install()
