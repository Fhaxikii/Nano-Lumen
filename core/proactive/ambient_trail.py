# core/proactive/ambient_trail.py
"""Ambient Memory Phase 4：把"工作现场"按时段持久化成一条轨迹，跨会话/重启留存。

只存【一行人话摘要】（应用 + 在做啥 + 标题级细节，如"在 Code 写代码(foo.py)"），
不存原始事件、不存内容明文。内存 buffer 只有 10 分钟；这条轨迹让 Nano 能知道
"今天早些时候在干嘛"，并在重启后仍记得。
"""
from __future__ import annotations
import json
import time
import pathlib
from core.paths import data_dir, data_path
import threading
from loguru import logger

_PATH = data_path("ambient_trail.jsonl")
_LOCK = threading.Lock()
_DEDUP_GAP = 150       # 同一条摘要 150s 内不重复写
_KEEP_HOURS = 18       # 只留最近 18 小时
_MAX_ROWS = 300


def _load_raw() -> list:
    if not _PATH.exists():
        return []
    out = []
    try:
        for ln in _PATH.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        return []
    return out


def append(line: str, ref: dict | None = None) -> None:
    """追加一条时段现场摘要（与上一条相同且很近则跳过，避免刷屏）。

    ⭐⭐ 2026-08-26 起**一行两版**（三层结构里的「采集」+「注入」）：

        line  互动感版 —— 每轮无条件注入，只给「够识别」的：时间 + 名字
        ref   生产力版 —— 完整路径 / 完整 URL / 目录，**只在用户指代时才被取出**

    📌 为什么采集必须全：**丢了就永远丢了**。而「按需解析」只在变化慢的对象上
       成立 —— 等用户说「刚才那个页面」再去读地址栏，拿到的是他**现在**看的那个。
    📌 为什么注入只给名字：trail 是**每轮无条件注入**的，这里每多一个字都乘以轮数。

    ⚠️ 老行没有 `ref` 字段 ⇒ `.get("ref")` 为空 ⇒ 自动落到「这条没有更多信息」，
       **历史文件不需要迁移**。
    ⚠️ **完整 URL 会跟着落盘 18 小时**（2026-08-26 明确定下）。
       ⇒ 这个文件是明文 JSON：严禁把 ref 里的内容写进 logger / os_audit ——
         同 `.env` 那次教训：**采集到的东西会顺着别的路漏出去。**
    """
    line = (line or "").strip()
    if not line:
        return
    now = time.time()
    try:
        with _LOCK:
            rows = _load_raw()
            if rows:
                last = rows[-1]
                # ⚠️ 去重判据要**连句柄一起比**：同一个应用换了文件时 `line` 通常
                #    会跟着变（文件名在里面），但同名不同路径的两个文件会撞 ——
                #    📌 那种情况下把第二条吞掉，等于让 trail 指向错的那个。
                _same_ref = ((last.get("ref") or {}).get("path")
                             == ((ref or {}).get("path")))
                if (last.get("line") == line and _same_ref
                        and now - last.get("ts", 0) < _DEDUP_GAP):
                    return
            _row = {"ts": now, "line": line}
            if ref:
                _row["ref"] = ref
            rows.append(_row)
            cutoff = now - _KEEP_HOURS * 3600
            rows = [r for r in rows if r.get("ts", 0) >= cutoff][-_MAX_ROWS:]
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8",
            )
    except Exception as e:
        logger.warning(f"[AmbientTrail] append 失败: {e}")


def recent(hours: float = 12, limit: int = 8, exclude_last_sec: float = 600) -> list:
    """返回最近 hours 内的轨迹（默认排除最近 10 分钟——那段由实时 buffer 覆盖，
    避免和"当前现场"重复）。每项 {ts, line}。"""
    now = time.time()
    cutoff = now - hours * 3600
    keep_before = now - exclude_last_sec
    rows = [r for r in _load_raw()
            if cutoff <= r.get("ts", 0) <= keep_before]
    return rows[-limit:]
