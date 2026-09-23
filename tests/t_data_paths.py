# -*- coding: utf-8 -*-
"""数据目录的唯一出口（core/paths.py）与测试沙盒（tests/_sandbox.py）。

- `data_dir()` 遵循 `NANO_DATA_DIR`，未设置时是仓库 `data/`。
- 测试进程运行在临时数据目录里，随仓库分发的数据文件已复制进去。
- 产品代码不许再自己拼 `data/` 路径（除 core/paths.py 外）：新写的硬编码路径会绕过隔离，
  测试又会开始写真实用户数据。
- 每个测试文件都必须在导入任何产品模块之前导入 `tests._console`（它先导入沙盒），
  否则模块级路径常量会在沙盒生效前按真实目录算好。

用法：
  py -3.10 tests\\t_data_paths.py
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_paths() -> None:
    print("\n▶ core.paths")
    code = ("import os, sys; sys.path.insert(0, r'%s'); os.environ.pop('NANO_DATA_DIR', None); "
            "from core import paths; print(paths.data_dir())" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    check(pathlib.Path(r.stdout.strip()) == ROOT / "data", "未设置 NANO_DATA_DIR：仓库 data/", r.stdout.strip())
    code2 = ("import os, sys; sys.path.insert(0, r'%s'); os.environ['NANO_DATA_DIR'] = r'C:\\x\\y'; "
             "from core import paths; print(paths.data_path('a', 'b.json'))" % ROOT)
    r = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True)
    check(r.stdout.strip() == r"C:\x\y\a\b.json", "设置了 NANO_DATA_DIR：以它为根", r.stdout.strip())


def t_sandbox() -> None:
    print("\n▶ 测试沙盒")
    from core import paths
    d = paths.data_dir()
    check(d.resolve() != paths.REPO_DATA_DIR.resolve(), "测试进程的数据目录不是仓库 data/", str(d))
    check(os.environ.get("NANO_DATA_DIR") == str(d), "子进程通过环境变量继承同一个目录")
    for rel in paths.SHIPPED_DATA_FILES:
        check((d / rel).exists(), f"随仓库分发的 {rel} 已复制进沙盒")
    from core.runtime import store
    check(str(d) in str(store.RuntimeStore()._db_path), "运行时库落在沙盒里")


_HARDCODED = re.compile(r'''/\s*["']data["']|["']data/|Path\(\s*["']data''')


def t_no_hardcoded_data_paths() -> None:
    print("\n▶ 产品代码不自己拼 data/ 路径")
    files = [ROOT / "app.py", ROOT / "nano_koala.py"] + sorted((ROOT / "core").rglob("*.py")) \
        + sorted((ROOT / "memory").rglob("*.py")) + sorted((ROOT / "skills").rglob("*.py"))
    hits = []
    for f in files:
        if f.name == "paths.py" and f.parent.name == "core" or not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if _HARDCODED.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}")
    check(not hits, "没有硬编码的 data/ 路径", ", ".join(hits[:8]))
    check(len(files) > 50, "确实扫描到了产品代码（扫描器没有失效）", f"n={len(files)}")


_PRODUCT_IMPORT = re.compile(r'^(?:from|import)\s+(core|app|memory|skills|nano_koala)\b', re.M)
_CONSOLE_IMPORT = re.compile(r'^import\s+(?:tests\.)?_console\b', re.M)


def t_every_test_boots_sandbox_first() -> None:
    print("\n▶ 每个测试都先导入 _console（沙盒）")
    bad = []
    files = sorted((ROOT / "tests").glob("t_*.py"))
    for f in files:
        t = f.read_text(encoding="utf-8", errors="replace")
        c = _CONSOLE_IMPORT.search(t)
        p = _PRODUCT_IMPORT.search(t)
        if c is None or (p is not None and p.start() < c.start()):
            bad.append(f.name)
    check(not bad, "所有测试文件在导入产品模块前导入 _console", ", ".join(bad))
    check(len(files) > 50, "确实扫描到了测试文件", f"n={len(files)}")


if __name__ == "__main__":
    print("=" * 74)
    print("数据目录出口与测试沙盒")
    print("=" * 74)
    t_paths()
    t_sandbox()
    t_no_hardcoded_data_paths()
    t_every_test_boots_sandbox_first()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
