# -*- coding: utf-8 -*-
"""测试沙盒：每个测试进程使用自己的临时数据目录，不读写真实用户数据。

由 `tests/_console.py` 第一行导入，所以所有测试在导入任何 core 模块之前就已经生效
（不少 core 模块在导入时把数据路径算成模块级常量）。

- 总是新建临时目录，**忽略**外部已有的 `NANO_DATA_DIR`：测试不能写进任何已有的数据目录。
- 只复制随仓库分发的数据文件（`core.paths.SHIPPED_DATA_FILES`）。
- 测试启动的子进程继承同一个 `NANO_DATA_DIR`。
- 进程退出时删除临时目录。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import paths as _paths  # noqa: E402  只含路径计算，没有副作用

DATA_DIR = pathlib.Path(tempfile.mkdtemp(prefix="nano_test_data_"))
for _rel in _paths.SHIPPED_DATA_FILES:
    _src = _paths.REPO_DATA_DIR / _rel
    if _src.exists():
        _dst = DATA_DIR / _rel
        _dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_src, _dst)
os.environ["NANO_DATA_DIR"] = str(DATA_DIR)

if _paths.data_dir().resolve() == _paths.REPO_DATA_DIR.resolve():
    raise RuntimeError("test sandbox failed: data_dir() still points at the repository data/")

atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)
