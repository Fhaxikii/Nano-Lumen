# core/paths.py
"""项目根目录与数据目录的唯一出口。

`ROOT` 是项目根（程序自己的目录，不是工作目录）。代码里需要项目根的地方一律用它，
不各自用 `__file__` 往上数层级：文件一挪位置，往上数的层数就悄悄错了。

所有运行期数据（运行时库、记忆库、知识库、设置、日志类文件）都经由 `data_dir()` /
`data_path()` 定位。环境变量 `NANO_DATA_DIR` 非空时使用它，否则使用仓库下的 `data/`。
测试入口把 `NANO_DATA_DIR` 指向临时目录，测试因此不会读写真实用户数据。

注意：不少模块在导入时就把路径算成模块级常量，所以 `NANO_DATA_DIR` 必须在导入
这些模块之前设置。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_DATA_DIR = ROOT / "data"

#: 随仓库分发、放在 data/ 下的文件（相对 data/ 的路径）。隔离环境需要复制这些文件。
SHIPPED_DATA_FILES = (
    "model_config.json",
    "china_regions_city.json",
    "knowledge/_system/nano_manual.md",
)


def data_dir() -> pathlib.Path:
    v = (os.environ.get("NANO_DATA_DIR") or "").strip()
    return pathlib.Path(v) if v else REPO_DATA_DIR


def data_path(*parts: str) -> pathlib.Path:
    return data_dir().joinpath(*parts)
