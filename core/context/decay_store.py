"""衰减账本 —— **第三本账**。

═══ 三本账，权威顺序写死 ═══

    conversation_messages   历史事实权威 —— 用户真正说过什么
    exchange_decay (本模块)  衰减权威     —— 这段历史现在按 L 几投影、派生出了什么
    MemoryManager.storage   临时投影     —— 随时可从上面两份重建

🔴 **不一致时 `conversation_messages` 永远最高。**
📌 宁可重新花一次提炼费用，也不要拿一个过期 Digest 当真。

═══ ⚠️⚠️ `source_hash` 算的是【落盘账本】那一份，不是内存投影 ═══

这一条差一点就写错。内存 `storage` 与落盘账本**故意不一样**：

    compress_image_blocks   把图换成占位符（只动 storage）
    _restore_image_notes    重启后按 ui_images 把注记算回来（只动 storage）

如果拿 storage 算哈希，**每次重启、每次压缩都会产生新哈希** ——
于是每一条 Digest 都会被判成"陈旧"，然后无限重新提炼（无限花钱），
📌 **而它看起来完全像是"内容真的变了"**。

⭐ 所以哈希**直接从 SQLite 读原文算**：那才是"内容变没变"这个问题的权威。
📌 **校验和必须算在权威那一份上，不能算在投影上** ——
   投影按定义就是会变的，拿它当基准等于给自己造一个永远对不上的比较。

═══ ⚠️ 本步骤是 shadow：只记录，不删任何东西 ═══

第 1 步的全部目的是「让『某次交换现在是第几档』这件事有个持久的家」。
行为上**一个字节都不变**。真正的降级从第 2 步（L0→L1）开始。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from loguru import logger

# 阶梯档位。⚠️ 字符串而不是数字：日志和 SQLite 里一眼能读，
# 而且加一档（比如将来的 L5）不会把已有数据的含义挪位。
L0, L1, L2, L3, L4 = "L0", "L1", "L2", "L3", "L4"
_LEVELS = (L0, L1, L2, L3, L4)

# L2 结论行的 schema 版本。⚠️ 改字段就必须 +1 ——
# 📌 半年后最难查的不是"为什么报错"，而是
#    **"为什么有些旧记忆总比新记忆少一个关键字段"**。
DIGEST_SCHEMA_VERSION = 1


def _hash_rows(rows) -> str:
    """对一段消息算内容指纹。

    ⚠️ 只取 `(ordinal, role, payload_json)` —— payload 是落盘的**权威原文**。
    📌 不取 `created_at`：它不是内容，跟着它变会让哈希对"什么都没改"也报警。
    """
    h = hashlib.sha256()
    for r in rows:
        h.update(str(r["ordinal"]).encode())
        h.update(b"\x00")
        h.update(str(r["role"]).encode())
        h.update(b"\x00")
        h.update(str(r["payload_json"]).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


class DecayStore:
    """`exchange_decay` 的唯一入口。**永不抛** —— 它是治理层，不是正确性依赖。"""

    def __init__(self, store):
        self._store = store

    # ── 指纹 ──────────────────────────────────────────────────────────
    def source_hash(self, session_id: str, start_ordinal: int, end_ordinal: int) -> str:
        """直接从 SQLite 读原文算指纹。算不出来返回空串。

        ⚠️ **不接受调用方传进来的消息** —— 那样就又变成"算在投影上"了。
           📌 一个校验和如果允许调用方喂数据，它校验的就是调用方的说法，
              而不是事实本身。
        """
        try:
            with self._store.read() as c:
                rows = list(c.execute(
                    "SELECT ordinal, role, payload_json FROM conversation_messages "
                    "WHERE session_id=? AND ordinal>=? AND ordinal<=? ORDER BY ordinal",
                    (session_id, int(start_ordinal), int(end_ordinal))))
            return _hash_rows(rows) if rows else ""
        except Exception as e:
            logger.debug(f"[Decay] 算 source_hash 失败: {e}")
            return ""

    # ── 写 ────────────────────────────────────────────────────────────
    def record(self, session_id: str, start_ordinal: int, end_ordinal: int, *,
               level: str, digest: dict | None = None, index_entry: str = "",
               distiller_model: str = "") -> bool:
        """登记/更新一次交换的衰减状态。**指纹由本模块自己算，调用方不许传。**

        ⚠️ `INSERT OR REPLACE` 而不是 `INSERT`：同一次交换会被反复降级
           （L0→L1→L2→L3），它是**同一条记录的状态迁移**，不是四条记录。
           📌 一个"状态"如果每次变化都新增一行，读的时候就得回答"哪一行是现在"。
        """
        if level not in _LEVELS:
            logger.warning(f"[Decay] 未知档位 {level!r}，拒绝写入")
            return False
        try:
            _h = self.source_hash(session_id, start_ordinal, end_ordinal)
            with self._store.write_txn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO exchange_decay("
                    "session_id,start_ordinal,end_ordinal,source_hash,level,"
                    "digest_json,index_entry,digest_schema_version,distiller_model,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (session_id, int(start_ordinal), int(end_ordinal), _h, level,
                     json.dumps(digest, ensure_ascii=False) if digest else "",
                     index_entry or "",
                     DIGEST_SCHEMA_VERSION if digest else 0,
                     distiller_model or "", time.time()))
            return True
        except Exception as e:
            logger.warning(f"[Decay] 登记失败 ({session_id[:8]},{start_ordinal}): {e}")
            return False

    # ── 读 ────────────────────────────────────────────────────────────
    def get(self, session_id: str, start_ordinal: int) -> dict | None:
        try:
            with self._store.read() as c:
                r = c.execute(
                    "SELECT * FROM exchange_decay WHERE session_id=? AND start_ordinal=?",
                    (session_id, int(start_ordinal))).fetchone()
            return dict(r) if r is not None else None
        except Exception as e:
            logger.debug(f"[Decay] 读取失败: {e}")
            return None

    def load_session(self, session_id: str) -> dict[int, dict]:
        """整个会话的衰减状态，按 `start_ordinal` 索引。"""
        try:
            with self._store.read() as c:
                rows = list(c.execute(
                    "SELECT * FROM exchange_decay WHERE session_id=? ORDER BY start_ordinal",
                    (session_id,)))
            return {int(r["start_ordinal"]): dict(r) for r in rows}
        except Exception as e:
            logger.debug(f"[Decay] 读取会话失败: {e}")
            return {}

    def active_entries(self, session_id: str) -> dict[int, dict]:
        """整个会话**现在还算数**的衰减状态 —— 陈旧的一律不在里面。

        🔴🔴 **这是消费衰减表的唯一出口。** 别再直接用 `load_session()` 过滤。

        为什么必须收成一个出口：
        `level_of()` 早就写对了（没记录 / 陈旧 → 当 L0），但别的消费方
        **各查各的**，而且各自忘了不同的部分 ——

            `l3_index_lines`   忘了判陈旧  → system 里仍注入旧索引
            app `_l3_entries`  忘了判陈旧  → UI 仍把那几轮藏着
            `rebuild_projection` 判了      → 投影按 L0 把原文放回来

        于是同一条记录，**三个消费方三种意见**：权威说 L0、system 说 L3、
        UI 说"已移出"。三本账没漂，是**读账的人**漂了。

        📌 **「每处都有 stale 逻辑」不等于「stale 被统一消费」** ——
           前者是三份各自正确的判断，后者才是一个权威。
        ⚠️ 补第三个 `if` 是这条 bug 的**第四次机会**，不是它的修复。
        """
        out = {}
        for _ord, e in (self.load_session(session_id) or {}).items():
            if not self.is_stale(e):
                out[_ord] = e
        return out

    # ── 校验 ──────────────────────────────────────────────────────────
    def is_stale(self, entry: dict) -> bool:
        """这条派生物还配不配得上现在的原文？

        ⭐ 三种"陈旧"必须都算上，只对第一种最容易想到：
           ① 内容变了（哈希对不上）
           ② schema 升级了（旧 digest 少字段）
           ③ 当时没算出哈希（空串）—— **不能当成"没问题"**
        📌 **「我没验过」和「我验过了没问题」必须分开** ——
           把前者当后者，正是这一层最怕的静默失真。
        """
        try:
            if not entry:
                return True
            if not entry.get("source_hash"):
                return True                       # ③
            _now = self.source_hash(entry["session_id"], entry["start_ordinal"],
                                    entry["end_ordinal"])
            if not _now or _now != entry["source_hash"]:
                return True                       # ①
            if (entry.get("digest_json")
                    and int(entry.get("digest_schema_version") or 0) != DIGEST_SCHEMA_VERSION):
                return True                       # ②
            return False
        except Exception:
            return True                           # 算不出来 = 不敢用

    def level_of(self, session_id: str, start_ordinal: int) -> str:
        """这次交换现在在第几档。**没记录或已陈旧一律当 L0** —— fail-safe 方向。

        📌 猜"它已经降过级了"会让我们少发内容给模型（信息丢失）；
           猜"它还是原文"最多是多花点 token。**往损失小的那一侧倒。**
        """
        e = self.get(session_id, start_ordinal)
        if e is None or self.is_stale(e):
            return L0
        return str(e.get("level") or L0)
