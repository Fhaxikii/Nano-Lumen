# core/crash_journal.py
"""
Nano 崩溃留痕 —— 故障上报体系里"进程已经死了"那个出口

═══ 为什么主力是 write-ahead breadcrumb，而不是 excepthook ═══

HealthRegistry 只能处理"异常被 Python 捕获、进程还活着"这一种情况。它处理不了：
原生库崩溃 / segfault / os._exit / 启动早期 import 直接终止。

而这恰好就是本项目的已知崩溃形态——内部诊断记录 的结论是
"bge-m3 模型栈(torch/transformers)与 chroma-hnswlib 在同一进程里造成间歇性原生堆
内存损坏 → 随机 segfault，5 次复现 3 崩"。

sys.excepthook / threading.excepthook / asyncio exception handler 这三个钩子
**一个都抓不到 segfault**：原生层直接杀进程，Python 栈根本不会展开。os._exit 按
定义就绕过 atexit 和所有钩子（app.py 的托盘退出用的正是 os._exit(0)）。import 期
崩溃时钩子还没注册。

所以主力必须是"事前留痕"：在危险操作【之前】往盘上写一条"我正要做 X"，成功后清掉。
下次启动看到没清掉的 breadcrumb，就知道上次死在这一步。这个方案不依赖进程能否优雅
退出——segfault、os._exit、断电、任务管理器强杀全覆盖。

excepthook 保留为补充：抓得到的 Python 级异常顺手记进同一个 journal，两者共用
展示通道。但它不是主力。

═══ 边界 ═══

磁盘记录的是"上一个进程发生过什么"，是历史，不是当前健康状态的权威来源。
展示完就标记，当前能力状态一律以本次重新探测的结果为准（HealthRegistry）。
"""
from __future__ import annotations

import json
import os
import pathlib
from core.paths import data_dir, data_path
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from loguru import logger

_DATA_DIR = data_dir()
_BREADCRUMB_FILE = _DATA_DIR / "breadcrumbs.json"
_JOURNAL_FILE = _DATA_DIR / "fault_journal.jsonl"

# journal 只保留最近这么多条，防止长期运行后无限增长
_JOURNAL_MAX_LINES = 200

_lock = threading.RLock()
# 本进程的 pid，用来区分"上一个进程留下的"和"自己刚写的"
_PID = os.getpid()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """写临时文件再 os.replace 原子替换。

    直接覆写有个真实风险：正好在写到一半时 segfault，留下半截 JSON，
    下次启动解析失败——而这个模块存在的意义就是"崩了也要留下可读的痕迹"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{_PID}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.debug(f"[CrashJournal] 原子写失败 {path.name}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _load_breadcrumbs() -> dict[str, dict[str, Any]]:
    if not _BREADCRUMB_FILE.exists():
        return {}
    try:
        data = json.loads(_BREADCRUMB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # 半截 JSON 本身就是"上次写到一半崩了"的证据，但没法解析出是哪一步。
        # 不静默吞——记一条未知崩溃，总比什么都没有强。
        logger.warning(f"[CrashJournal] breadcrumbs.json 解析失败（可能是崩溃残留）: {e}")
        return {"__corrupt__": {"step": "unknown", "started_at": 0.0,
                                "detail": "breadcrumbs.json 损坏，上次可能在写入时崩溃",
                                "pid": 0}}


def _save_breadcrumbs(crumbs: dict[str, dict[str, Any]]) -> None:
    _atomic_write(_BREADCRUMB_FILE, json.dumps(crumbs, ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════════════════
# 事前留痕
# ══════════════════════════════════════════════════════════════════════════

def enter_step(name: str, *, detail: str = "", capability: str = "") -> None:
    """进入一个危险步骤。必须在真正动手【之前】调用。"""
    with _lock:
        crumbs = _load_breadcrumbs()
        crumbs[name] = {
            "step": name,
            "detail": detail,
            "capability": capability,
            "started_at": time.time(),
            "pid": _PID,
        }
        _save_breadcrumbs(crumbs)


def leave_step(name: str) -> None:
    """步骤安全结束。清掉痕迹。"""
    with _lock:
        crumbs = _load_breadcrumbs()
        if crumbs.pop(name, None) is not None:
            _save_breadcrumbs(crumbs)


@contextmanager
def breadcrumb(name: str, *, detail: str = "", capability: str = "") -> Iterator[None]:
    """包住可能让整个进程原地消失的原生调用。

    用法：
        with breadcrumb("rag.load_embedder", capability=Cap.KB_VECTOR_SEARCH,
                        detail="SentenceTransformer('BAAI/bge-m3')"):
            _embedder = SentenceTransformer("BAAI/bge-m3")

    注意 finally 里清痕迹：Python 级异常会正常走到 finally（那种情况由
    HealthRegistry 负责上报，不该在下次启动再报一次"上次崩了"）；
    真正的 segfault 走不到 finally，痕迹就留下了——这正是我们要的信号。
    """
    enter_step(name, detail=detail, capability=capability)
    try:
        yield
    finally:
        leave_step(name)


# ══════════════════════════════════════════════════════════════════════════
# journal 读写
# ══════════════════════════════════════════════════════════════════════════

def record_fatal(kind: str, summary: str, *, detail: str = "",
                 capability: str = "", step: str = "") -> str:
    """往 journal 追加一条。返回 record_id。"""
    rid = "flt_" + uuid.uuid4().hex[:10]
    rec = {
        "id": rid,
        "kind": kind,                 # crash_breadcrumb / uncaught_exception / thread_exception / asyncio_exception
        "summary": summary,
        "detail": (detail or "")[:4000],
        "capability": capability,
        "step": step,
        "ts": time.time(),
        "pid": _PID,
        "presented": False,
    }
    with _lock:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[CrashJournal] 写 journal 失败: {e}")
    return rid


def _read_journal() -> list[dict[str, Any]]:
    if not _JOURNAL_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in _JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue   # 单行坏了跳过，不让一行毁掉整个文件
    except Exception as e:
        logger.debug(f"[CrashJournal] 读 journal 失败: {e}")
    return out


def _rewrite_journal(records: list[dict[str, Any]]) -> None:
    keep = records[-_JOURNAL_MAX_LINES:]
    _atomic_write(
        _JOURNAL_FILE,
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
    )


def mark_presented(ids: list[str]) -> None:
    """标记若干条已在 UI 展示过，下次启动不再重复提示。"""
    if not ids:
        return
    idset = set(ids)
    with _lock:
        recs = _read_journal()
        changed = False
        for r in recs:
            if r.get("id") in idset and not r.get("presented"):
                r["presented"] = True
                changed = True
        if changed:
            _rewrite_journal(recs)


# ══════════════════════════════════════════════════════════════════════════
# 启动扫描
# ══════════════════════════════════════════════════════════════════════════

def startup_scan() -> list[dict[str, Any]]:
    """启动时调用一次。

    做两件事：
      1. 把上一个进程遗留的 breadcrumb 转成 journal 记录（那就是"死在这一步"的证据），
         并清空 breadcrumb 文件
      2. 返回所有尚未展示过的 fatal 记录，交给 UI 就绪后呈现

    只清 pid != 当前进程 的痕迹——理论上单实例不会撞上，但万一用户开了两个 Nano，
    别把对方正在进行的步骤当成崩溃残留清掉。
    """
    with _lock:
        crumbs = _load_breadcrumbs()
        stale = {k: v for k, v in crumbs.items() if v.get("pid") != _PID}
        for name, info in stale.items():
            step = info.get("step") or name
            detail = info.get("detail") or ""
            started = info.get("started_at") or 0
            when = time.strftime("%m-%d %H:%M", time.localtime(started)) if started else "未知时间"
            record_fatal(
                "crash_breadcrumb",
                summary=f"上次运行在「{step}」这一步异常终止（{when}），进程没有正常退出。",
                detail=detail,
                capability=info.get("capability", ""),
                step=step,
            )
            logger.error(f"[CrashJournal] 检测到上次崩溃残留: step={step} detail={detail}")
        if stale:
            remaining = {k: v for k, v in crumbs.items() if v.get("pid") == _PID}
            _save_breadcrumbs(remaining)

        recs = _read_journal()

    return [r for r in recs if not r.get("presented")]


# ══════════════════════════════════════════════════════════════════════════
# 补充钩子（抓得到的 Python 级异常）
# ══════════════════════════════════════════════════════════════════════════

_hooks_installed = False


def install_hooks() -> None:
    """安装 excepthook 系列。

    这是【补充】不是主力——它抓不到 segfault / os._exit / import 期终止，
    那些靠 breadcrumb。这里只负责把"能抓到但没人处理"的异常也留一份痕，
    让 journal 成为唯一的崩溃事实来源，不用翻 cmd。
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    _prev_excepthook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            record_fatal(
                "uncaught_exception",
                summary=f"未捕获异常：{exc_type.__name__}: {exc}",
                detail="".join(traceback.format_exception(exc_type, exc, tb)),
            )
        except Exception:
            pass
        try:
            _prev_excepthook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook

    def _thread_hook(args):
        try:
            record_fatal(
                "thread_exception",
                summary=f"后台线程 {getattr(args, 'thread', None) and args.thread.name} "
                        f"未捕获异常：{args.exc_type.__name__}: {args.exc_value}",
                detail="".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)),
            )
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass

    logger.debug("[CrashJournal] 异常钩子已安装")


def install_asyncio_handler(loop) -> None:
    """事件循环起来之后再挂。与 install_hooks 分开，因为 loop 出现得晚。"""
    def _handler(_loop, context):
        exc = context.get("exception")
        msg = context.get("message") or ""
        try:
            record_fatal(
                "asyncio_exception",
                summary=f"事件循环未捕获异常：{type(exc).__name__ if exc else 'Error'}: {exc or msg}",
                detail="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                if exc else str(context)[:2000],
            )
        except Exception:
            pass
        _loop.default_exception_handler(context)

    try:
        loop.set_exception_handler(_handler)
    except Exception as e:
        logger.debug(f"[CrashJournal] asyncio handler 安装失败: {e}")
