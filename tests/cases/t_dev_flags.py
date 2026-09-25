# -*- coding: utf-8 -*-
"""开发者开关（core/dev_flags.py、config/dev_flags.json）。

两部分：
- 解析规则：只有 enabled 为 JSON true 才算开启，其余情况一律关闭。
- 仓库状态（发布前必查）：config/dev_flags.json 中所有开关的 enabled 必须为 false，
  且每个开关都有 description。开关处于开启状态时提交，本测试失败。

用法：
  py -3.10 tests\cases\t_dev_flags.py
"""
from __future__ import annotations

import json
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


def t_dev_flags() -> None:
    print("\n[7] 开发者开关：只有 enabled 为 JSON true 才算开启")
    from core import dev_flags as DF
    orig = DF.DEV_FLAGS_PATH
    try:
        DF.DEV_FLAGS_PATH = _TMP / "dev_flags.json"

        def entry(v):
            return json.dumps({"_comment": "x", "console_debug": {"enabled": v, "description": "d"}})

        cases = [
            (None, False, "文件不存在"),
            (entry(True), True, "enabled 为 true"),
            (entry("true"), False, 'enabled 为字符串 "true"'),
            (entry(1), False, "enabled 为 1"),
            (entry(False), False, "enabled 为 false"),
            ('{"console_debug": true}', False, "开关不是对象（缺少 enabled）"),
            ('{}', False, "缺少该开关"),
            ('{broken', False, "JSON 格式错误"),
            ('[true]', False, "不是 JSON 对象"),
        ]
        for content, expected, label in cases:
            if content is None:
                if DF.DEV_FLAGS_PATH.exists():
                    DF.DEV_FLAGS_PATH.unlink()
            else:
                DF.DEV_FLAGS_PATH.write_text(content, encoding="utf-8")
            DF.reset_for_tests()
            check(DF.enabled("console_debug") is expected, f"{label} → {expected}")

        DF.DEV_FLAGS_PATH.write_text(entry(True), encoding="utf-8")
        DF.reset_for_tests()
        DF.enabled("console_debug")
        DF.DEV_FLAGS_PATH.write_text(entry(False), encoding="utf-8")
        check(DF.enabled("console_debug") is True, "同一进程内只读取一次，文件修改后需重启生效")
        check("_comment" not in DF.load_file(DF.DEV_FLAGS_PATH), "以 _ 开头的说明键不作为开关返回")
    finally:
        DF.DEV_FLAGS_PATH = orig
        DF.reset_for_tests()


def t_dev_flags_committed_state() -> None:
    print("\n[7b] 仓库中的 config/dev_flags.json：所有开关为关闭，且每个开关都有说明")
    from core import dev_flags as DF
    path = ROOT / "config" / "dev_flags.json"
    check(path.is_file(), "config/dev_flags.json 存在（随仓库分发）")
    flags = DF.load_file(path)
    check(len(flags) > 0, "至少定义了一个开关", str(sorted(flags)))
    on = [n for n, e in flags.items() if not (isinstance(e, dict) and e.get("enabled") is False)]
    check(not on, "所有开关的 enabled 都是 false（发布前必查）", str(on))
    no_desc = [n for n, e in flags.items()
               if not (isinstance(e, dict) and str(e.get("description") or "").strip())]
    check(not no_desc, "每个开关都有 description", str(no_desc))


def main() -> int:
    t_dev_flags()
    t_dev_flags_committed_state()

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
