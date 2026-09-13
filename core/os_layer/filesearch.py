# -*- coding: utf-8 -*-
"""`search_files` —— 递归找文件（名字）+ 找内容（grep），**合成一个**。

═══ 为什么是一个工具而不是三个 ═══

有的编码工具把 **Grep（内容）/ Glob（文件名）/ LS** 拆成三个内置工具。
**Nano 刻意不照抄那个拆法**，理由与
「不要往 `os_execute` 里再塞 action」同源：

📌 **Nano 的工具清单本来就容易糊（模型看不清）——
   一次加三个高度相似的工具，是在加深它。**

而 Nano 今天要做"递归找文件"只有两条路，都很差：
  · 不断 `list_dir → file_read` 手动递归（`list_dir` 是单层）
  · 直接上 `run_command`（**风险地板 3，每次弹窗**）

═══ 🔴 只读 ═══

本模块**不写任何东西**。写/删/移仍然只能走 `os_execute`（那边有 floor 与确认弹窗）。
📌 **换一个暴露层，不许换掉它底下的安全层**（2026-08-13 定的硬约束）。
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from core.os_layer import pathpolicy as _pp

# 一次最多返回多少条。⚠️ 防的是"把一个 20 万行的仓库整个倒进上下文"——
# 📌 一个搜索工具如果能返回无限条，它就不是搜索，是另一种全文加载。
MAX_RESULTS = 100
# 单行最多显示多少字符（压缩过的行、minified js 会有几万字符一行）。
MAX_LINE_CHARS = 300
# 最多扫多少个文件 —— 防在 `C:\` 上跑一次就卡住。
MAX_FILES_SCANNED = 20_000
# 超过这个大小的文件不搜内容（按行读，几百 MB 会拖死）。
MAX_CONTENT_BYTES = 8 * 1024 * 1024


@dataclass
class Hit:
    path: str
    line_no: int = 0        # 0 = 只是文件名命中，没有行
    line: str = ""


def _read_lines(p: Path):
    """按行读，编码尽力而为。**永不抛。**"""
    for _enc in ("utf-8", "gbk", "latin-1"):
        try:
            with p.open("r", encoding=_enc, errors="strict") as f:
                return f.read().splitlines()
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception:
            return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except Exception:
        return None


def search(root: str, *, name_pattern: str = "", content: str = "",
           regex: bool = False, recursive: bool = True,
           exclude: str = "", max_results: int = MAX_RESULTS) -> dict:
    """找文件 / 找内容。返回 `{"ok", "hits", "scanned", "truncated", "error"}`。

    ⚠️ **`name_pattern` 与 `content` 可以只给一个，也可以都给**：
       只给名字 → Glob；只给内容 → Grep（全扫）；都给 → 先按名字缩小再搜内容。
       📌 都不给是**没有意义的调用**，直接拒绝 ——
          否则它等于「把这个目录下所有文件列出来」，那是 `list_dir` 的活。
    """
    out: dict = {"ok": False, "hits": [], "scanned": 0,
                 "truncated": False, "error": ""}
    if not root:
        out["error"] = "没有给 path"
        return out
    if not name_pattern and not content:
        # 📌 见 docstring：这不是"宽容"，是这个调用本身没有意义。
        out["error"] = "name_pattern 与 content 至少要给一个"
        return out

    _why = _pp.denied_reason(root)
    if _why:
        out["error"] = f"这个位置不允许读取（{_why}）"
        return out

    base = Path(root)
    if not base.exists():
        out["error"] = f"路径不存在：{root}"
        return out

    _rx = None
    if content:
        try:
            _rx = re.compile(content if regex else re.escape(content), re.IGNORECASE)
        except re.error as e:
            out["error"] = f"正则不合法：{e}"
            return out

    _extra_skip = {x.strip() for x in (exclude or "").split(",") if x.strip()}
    _cap = max(1, min(int(max_results or MAX_RESULTS), MAX_RESULTS))
    hits: list[Hit] = []
    scanned = 0

    def _walk():
        if base.is_file():
            yield base
            return
        if not recursive:
            try:
                for _e in os.scandir(base):
                    if _e.is_file():
                        yield Path(_e.path)
            except Exception:
                pass
            return
        for _dir, _subs, _files in os.walk(base):
            # ⚠️ **就地改 `_subs`** 才能真的不进那些目录（os.walk 的契约）——
            #    📌 只在结果里过滤的话，它照样把 node_modules 走了一遍。
            _subs[:] = [d for d in _subs
                        if not _pp.should_skip_dir(d, _extra_skip)
                        and _pp.is_allowed(os.path.join(_dir, d))]
            for _f in _files:
                yield Path(_dir) / _f

    try:
        for fp in _walk():
            if scanned >= MAX_FILES_SCANNED or len(hits) >= _cap:
                out["truncated"] = True
                break
            scanned += 1
            if name_pattern and not fnmatch.fnmatch(fp.name, name_pattern):
                continue
            if not _pp.is_allowed(fp):
                continue
            if _rx is None:
                hits.append(Hit(str(fp)))
                continue
            if _pp.looks_binary(fp):
                continue
            try:
                if fp.stat().st_size > MAX_CONTENT_BYTES:
                    continue
            except Exception:
                continue
            lines = _read_lines(fp)
            if lines is None:
                continue
            for _i, _ln in enumerate(lines, 1):
                if _rx.search(_ln):
                    _txt = _ln.strip()
                    if len(_txt) > MAX_LINE_CHARS:
                        _txt = _txt[:MAX_LINE_CHARS] + " …"
                    hits.append(Hit(str(fp), _i, _txt))
                    if len(hits) >= _cap:
                        out["truncated"] = True
                        break
        out["ok"] = True
    except Exception as e:
        logger.warning(f"[search_files] 扫描失败: {e}")
        out["error"] = str(e)
    out["hits"] = hits
    out["scanned"] = scanned
    return out


def render(res: dict, *, root: str = "", name_pattern: str = "",
           content: str = "") -> str:
    """渲染成给模型看的文本。⚠️ 形状照 grep：`文件:行号: 内容`。"""
    if res.get("error"):
        return f"[search_files] {res['error']}"
    hits = res.get("hits") or []
    if not hits:
        # ⚠️ **空结果要说清扫了多少** —— 📌 「没找到」和「压根没扫到东西」
        #    对下一步是完全不同的信息（路径写错 vs 关键词写错）。
        return (f"[search_files] 没有命中。已扫描 {res.get('scanned', 0):,} 个文件"
                f"（root={root}"
                + (f", name={name_pattern}" if name_pattern else "")
                + (f", content={content!r}" if content else "") + "）。")
    lines = [f"[search_files] {len(hits)} 条命中"
             f"（扫描 {res.get('scanned', 0):,} 个文件）"
             + ("，**已截断**，缩小范围可看到更多" if res.get("truncated") else "")]
    for h in hits:
        lines.append(f"{h.path}:{h.line_no}: {h.line}" if h.line_no else h.path)
    return "\n".join(lines)
