# -*- coding: utf-8 -*-
"""文件类工具的**路径策略** —— 哪些地方不许读。

「实现前必须先定、不能事后补的两条」第 1 条：

> `grep_file` 是**读任意路径**的工具，与 `os_execute` 的文件读取权限边界重叠。
> 要定清楚路径策略（只读不改、走 KB 侧宽松策略，但**必须有黑名单** ——
> 不能让它读 `C:\\Windows\\System32\\config\\SAM` 之类）。

═══ 为什么是**黑名单**而不是白名单（与 Subagent 的工具集正好相反）═══

Subagent的工具集刻意做成白名单，理由是「排除法的欠账随时间增长」。
**这里反过来，而且理由不冲突** ——

    工具集   有限、可枚举、我们自己造的       → 白名单（漏一个 = 多给了一件武器）
    文件系统 无限、别人造的、每天都在变       → 白名单不可能（漏一个 = 用户自己的
                                              文档读不了，而那是这台电脑的主人）

📌 **白名单适用于「我们知道全集」的东西；文件系统不是那种东西。**
⚠️ 所以这里的黑名单**不承诺挡住一切**，它只承诺挡住**那几类明确不该读的**。
   真正的安全边界仍然在 OS 安全网（floor / 确认弹窗）那一层 ——
   📌 **本模块是「别顺手踩到」，不是「防住蓄意」。** 把它当后者会高估它。

═══ ⚠️ 只管【读】═══

写/删/移仍然只能走 `os_execute`，那边有 `floor` 与确认弹窗。
📌 **换一个暴露层，不许换掉它底下的安全层**（2026-08-13 定的硬约束）——
   否则"加个方便的工具"就成了绕过确认的后门。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

# 🔴 明确不许读的位置。每一条都写清**为什么**——
#    📌 一条没写理由的黑名单项，几年后没人敢删、也没人知道它还成不成立。
_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    # ① 凭据与安全数据库
    (r"[\\/]Windows[\\/]System32[\\/]config([\\/]|$)", "SAM/SYSTEM 等安全数据库"),
    (r"[\\/]Windows[\\/]System32[\\/]catroot2?([\\/]|$)", "代码签名目录"),
    (r"[\\/]Microsoft[\\/]Credentials([\\/]|$)", "Windows 凭据库"),
    (r"[\\/]Microsoft[\\/]Protect([\\/]|$)", "DPAPI 主密钥"),
    (r"[\\/]\.ssh([\\/]|$)", "SSH 私钥"),
    (r"[\\/]\.gnupg([\\/]|$)", "GPG 私钥"),
    (r"[\\/]\.aws([\\/]|$)", "云凭据"),
    # ② 浏览器保存的密码 / cookie
    (r"[\\/]User Data[\\/].*[\\/](Login Data|Cookies|Web Data)$", "浏览器密码与 cookie"),
    # ③ 系统运行时（读了没意义，还可能很大或阻塞）
    (r"^[A-Za-z]:[\\/]pagefile\.sys$", "分页文件"),
    (r"^[A-Za-z]:[\\/]hiberfil\.sys$", "休眠文件"),
    (r"^[A-Za-z]:[\\/]swapfile\.sys$", "交换文件"),
    (r"^[\\/]{2}[.?][\\/]", "设备命名空间（\\\\.\\PhysicalDrive 之类）"),
)

_DENY = tuple((re.compile(p, re.IGNORECASE), why) for p, why in _DENY_PATTERNS)

# 搜索时**默认跳过**的目录名。⚠️ 与黑名单不同：这些不是"不许",是"不值得" ——
# 📌 把 `node_modules` 翻一遍不会泄露什么，只会让一次搜索变成三分钟。
#    所以它可以被调用方显式覆盖，而 `_DENY` 不行。
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".next", ".nuxt", "target", "vendor", ".tox", ".gradle",
})

# 明显不是文本的扩展名 —— 搜内容时跳过。
BINARY_EXT = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".jar", ".whl",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".svgz",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".flac", ".webm",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".lib", ".pdb", ".iso", ".img",
})


def denied_reason(path: str | os.PathLike) -> str:
    """这个路径**不许读**的理由。允许时返回空串。**永不抛。**

    ⚠️ fail-safe 方向是**允许** —— 📌 这台电脑是用户的，
       一个算不出结论的路径策略如果默认拒绝，坏掉的是用户读自己文件的能力，
       而它保护不了任何真实的东西（真正的边界在 OS 安全网那层）。
    """
    try:
        _p = str(path or "")
        if not _p:
            return ""
        # ⚠️ 先规范化再匹配：`C:\Windows\..\Windows\System32\config` 这种
        #    📌 一个不做规范化的路径黑名单，用一个 `..` 就绕过去了。
        try:
            _norm = str(Path(_p).resolve(strict=False))
        except Exception:
            _norm = os.path.normpath(_p)
        for _re, _why in _DENY:
            if _re.search(_norm) or _re.search(_p):
                return _why
        return ""
    except Exception as e:
        logger.debug(f"[PathPolicy] 判定失败（放行）: {e}")
        return ""


def is_allowed(path: str | os.PathLike) -> bool:
    return not denied_reason(path)


def should_skip_dir(name: str, extra: frozenset | set | None = None) -> bool:
    _n = (name or "").lower()
    if _n in SKIP_DIRS:
        return True
    return bool(extra and _n in {str(x).lower() for x in extra})


def looks_binary(path: str | os.PathLike) -> bool:
    return Path(str(path)).suffix.lower() in BINARY_EXT
