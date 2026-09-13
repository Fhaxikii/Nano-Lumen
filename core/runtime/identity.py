"""`runtime_id` —— 这一次进程的身份。

═══ 它回答的唯一问题 ═══

    「模型历史里那句『已加载 xxx / 已授权 xxx / 句柄还开着』，
      是不是**这一次进程**说的？」

📌 根本判据：**持久记忆能证明"过去发生过什么"，证明不了"现在仍然成立"。**
而"现在"的边界之一就是进程 —— 一堆运行态（已加载的工具 schema、临时授权、
文件/浏览器句柄、MCP 连接、内存回调）**天然活不过重启**，但它们在记忆里留下的
那句话会活下来，并且看起来仍然成立。

═══ ⚠️ 为什么不叫 `runtime_epoch`（原方案的名字）═══

`epoch` 在本项目里**已经有主人**：`app.py` 的「**response epoch**」是一段连续的
回应期（`_handoff_response_epoch` / `pending_epoch` / `_resp_state`）。
再引入 `runtime_epoch`，"哪个 epoch"就会变成一句必须追问的话。
📌 **一个词在项目里已经有主人时，别给它第二个语义 —— 改名的成本永远比消歧的成本低。**

═══ 存法：当前值内存权威 + 历史写库 ═══

    当前 runtime_id   进程内权威，每次启动【永远新生成】
    runtime_runs 表   历史记录，与 reconcile_on_startup 的恢复报告挂在一起

⚠️⚠️ **绝不能启动时从库里捞"最后一条 runtime_id"继续当 current。**
库里那些全是**历史身份**。真正的当前身份永远来自本进程启动时刚生成的那一个。
📌 只要守住这一条，落盘就不会变成"持久保存的失效引用" ——
   `runtime_id` 不是句柄 / socket / HWND，它只是一个**身份标签**，本身不指向任何资源。

═══ ⚠️ 刻意【不】记 `previous_shutdown_clean` ═══

原方案的字段清单里有它。**砍掉了，因为现在根本造不出这个值**：
全项目搜 `clean_shutdown` / `shutdown_clean` / `graceful` —— 零命中，
没有任何地方在正常退出时留过标记。而

    启动时没发现 interrupted task  ⇏  上次是干净退出的

（可能只是上次崩的时候正好没有活任务。）
📌 **不要用「没看到尸体」推导「寿终正寝」。**
以后真加了 shutdown marker 再补这个字段，不要先写一个看起来很精确的假事实。
"""
from __future__ import annotations

import threading
import time
import uuid

from loguru import logger


_lock = threading.Lock()
_current: str | None = None
_started_at: float = 0.0


def _new_runtime_id() -> str:
    """`rt_20260813_091801_a3f2` —— 可读的时间前缀 + 4 字节随机。

    ⚠️ 时间只是给人看的，**不参与任何判等**：同一秒内起两个进程也必须是两个身份，
       所以后缀是随机而不是序号（序号需要一个谁来维护的计数器）。
    """
    return f"rt_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def current_runtime_id() -> str:
    """本进程的身份。第一次调用时生成，之后恒定。

    ⚠️ 懒生成而不是 import 时生成：import 顺序不该决定"这次运行什么时候开始"。
    """
    global _current, _started_at
    if _current is None:
        with _lock:
            if _current is None:
                _current = _new_runtime_id()
                _started_at = time.time()
                logger.info(f"[Runtime] 本次运行身份 runtime_id={_current}")
    return _current


def started_at() -> float:
    """本进程 runtime_id 的生成时刻。没生成过就先生成。"""
    current_runtime_id()
    return _started_at


def record_run(kernel, recovery_summary: str = "") -> None:
    """把本次运行写进历史表。**失败不抛** —— 它是留痕，不是正确性依赖。

    ⚠️ 调用点在 `reconcile_on_startup` 之后：那份恢复报告和"这次是谁在跑"
    天然是一份东西，分开写两处会立刻产生"哪份是准的"这个问题。

    📌 **这张表只往里写，不往外读【当前身份】** —— 读它的只有崩溃诊断
       （"上一次运行是什么时候、恢复了什么"）。见模块头那条 ⚠️⚠️。
    """
    try:
        with kernel.store.write_txn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runtime_runs(runtime_id, started_at, recovery_summary) "
                "VALUES (?,?,?)",
                (current_runtime_id(), started_at(), (recovery_summary or "")[:500]),
            )
    except Exception as e:
        logger.warning(f"[Runtime] runtime_runs 留痕失败（不影响运行）: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 一次性 Runtime Restart Awareness
# ══════════════════════════════════════════════════════════════════════════
#
# ═══ ⚠️⚠️ 为什么【不】塞进 RecentSystemEvents ═══
#
# 第一版写的是「这条天然属于 RecentSystemEvents」。**那会做错。**
# 回代码核实，它 `render()` 的固定表头逐字写着：
#
#     "Things that happened in this process, not things you said or did.
#      Use them only if the user asks what just happened."
#
# 而这条提示的全部意义恰恰是 —— **用户没问也必须影响当前判断**。
# 塞进去等于给这条最不该被忽略的消息挂上一句"除非被问否则忽略我"。
# 而且它是 `maxlen=20 / window=6h / 每轮 render 重复`，**没有一次消费机制**。
#
# 📌 **复用一个通道时，要连它的措辞一起复用 —— 而那句措辞可能正好否定
#    你要传达的东西。**（当时只核了"通道存在且每轮注入"，没核表头说了什么。）
#
# → 所以：**只复用注入位置**（缓存哨兵之后的动态段），独立的一次性块。

_notice_pending: str = ""


def arm_restart_notice(kernel) -> None:
    """进程启动时挂上提示。**只在真有上一次运行时才挂** —— 首次运行不需要说"重启了"。"""
    global _notice_pending
    prev = previous_run(kernel)
    if not prev:
        logger.info("[Runtime] 没有上一次运行记录（首次运行），不挂重启提示")
        return
    _gap = max(0.0, started_at() - prev["started_at"])
    _rec = prev.get("recovery_summary") or ""
    _notice_pending = (
        "\n\n[Runtime Restarted]\n"
        "The Python process was restarted; this is a new runtime "
        f"(runtime_id={current_runtime_id()}). "
        f"The previous run started {_fmt_gap(_gap)} ago"
        + (f" and recovery on this startup reported: {_rec}." if _rec else ".")
        + "\n"
        "You are still the same continuous Nano and your memory is intact — do not act amnesiac.\n"
        "But everything that only lived inside the previous process is gone:\n"
        "  - tool schemas activated by earlier load_tools calls\n"
        "  - temporary authorizations, open file/browser handles, subprocesses\n"
        "  - MCP connections, leases, cached screen state, in-memory pending actions\n"
        "So do not assume any of those still hold. If you need one, establish it again "
        "in this runtime before relying on it.\n"
    )
    logger.info(f"[Runtime] 已挂上重启提示（上次运行 {prev['runtime_id']}）")


def _fmt_gap(sec: float) -> str:
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{int(sec / 60)}min"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def pending_restart_notice() -> str:
    """还没被消费的重启提示（没有就空串）。**只读，不消费。**"""
    return _notice_pending


def consume_restart_notice() -> None:
    """标记已消费。

    ⚠️⚠️ **调用时机是「模型确实收到了」，不是「我们把它拼进了 system_guide」。**
    判定（沿用 provider 现有的流式事件，不新增协议）：

        第一次出现任一真实模型流事件（thinking / text / tool input）  → 消费
        没有任何 delta 但拿到正常的 final done                        → 消费
        provider_error                                                → **不消费**，保留到下一轮

    📌 依据：已经吐过 thinking 就说明模型确实读到了 system prompt；
       而 API 层 400 / 网络失败时它一个字都没看到 —— 那种情况下把提示标成
       "已说过"，等于**这次重启永远不会被告知**。
    """
    global _notice_pending
    if _notice_pending:
        _notice_pending = ""
        logger.info("[Runtime] 重启提示已被模型收到 → 消费")


def previous_run(kernel) -> dict | None:
    """上一次运行（不是这一次）。给「重启提示」用，拿不到就返回 None。

    ⚠️ 按 `started_at` 倒序取**第二条** —— 第一条是本次。
       不按 `runtime_id` 字符串排：那个前缀是给人看的，同一秒内的两条排不出先后。
    """
    try:
        with kernel.store.read() as _c:
            rows = _c.execute(
                "SELECT runtime_id, started_at, recovery_summary FROM runtime_runs "
                "ORDER BY started_at DESC LIMIT 2"
            ).fetchall()
        for r in rows:
            if str(r["runtime_id"]) != current_runtime_id():
                return {"runtime_id": str(r["runtime_id"]),
                        "started_at": float(r["started_at"]),
                        "recovery_summary": str(r["recovery_summary"] or "")}
    except Exception as e:
        logger.debug(f"[Runtime] 读取上一次运行失败: {e}")
    return None
