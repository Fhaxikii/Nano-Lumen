# core/memory_store.py
"""
Nano Working Memory

跨会话持久化的结构化记忆层。与 memory/manager.py (对话历史) 不同：
- memory/manager.py: 本次对话的消息列表，重置时清空
- WorkingMemoryStore:  跨会话的操作记录，永久保存，模型可主动查询

设计原则：
1. 零外部依赖 — 只用 Python 标准库 sqlite3
2. 写入廉价   — 每次操作一行 INSERT，不做事务合并
3. 查询精确   — SQL 关键词过滤 + 时间排序，秒级响应
4. 双向写入   — 代码埋点写 + 模型通过 recall_working_memory 读
5. 持久化路径 — data/nano_memory.db（跟 data/knowledge/ 并列）
"""

import sqlite3
import json
import pathlib
import threading
from datetime import datetime
from typing import Any
from loguru import logger

# ── 入口类型常量 ─────────────────────────────────────────────────────────
class EntryType:
    SKILL_CALL    = "skill_call"      # 调用了某个 Skill
    SKILL_DEPLOY  = "skill_deploy"    # 部署/更新了某个 Skill
    FILE_WRITE    = "file_write"      # Skill 写入了文件
    FILE_READ     = "file_read"       # 加载了全文/RAG 命中
    RAG_QUERY     = "rag_query"       # RAG 知识库查询
    USER_NOTE     = "user_note"       # 模型主动写入的推断/备注
    SESSION_END   = "session_end"     # 每轮对话结束时的摘要（episodic 层）


class WorkingMemoryStore:
    """SQLite 工作记忆存储。

    数据库路径：<项目根>/data/nano_memory.db
    单表设计，按 entry_type + subject 索引，支持关键词全文搜索。
    """

    _DB_FILENAME = "nano_memory.db"
    _instance: "WorkingMemoryStore | None" = None
    _lock = threading.Lock()

    def __init__(self, db_path: pathlib.Path | None = None):
        if db_path is None:
            root = pathlib.Path(__file__).parent.parent
            db_path = root / "data" / self._DB_FILENAME
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._write_lock = threading.Lock()
        self._init_db()
        logger.info(f"[WorkingMemory] 已初始化: {db_path}")

    # ── 初始化 ────────────────────────────────────────────────────────────

    def _init_db(self):
        with self._connect() as conn:
            # 建表（不含 status，兼容旧 DB）
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS working_memory (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT    NOT NULL DEFAULT '',
                    ts          TEXT    NOT NULL,
                    entry_type  TEXT    NOT NULL,
                    subject     TEXT    NOT NULL DEFAULT '',
                    action      TEXT    NOT NULL DEFAULT '',
                    detail      TEXT    NOT NULL DEFAULT '',
                    tags        TEXT    NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_ts          ON working_memory (ts DESC);
                CREATE INDEX IF NOT EXISTS idx_subject     ON working_memory (subject);
                CREATE INDEX IF NOT EXISTS idx_entry_type  ON working_memory (entry_type);
            """)
            # 兼容迁移：已有 DB 可能没有 status 列，先加列再建索引
            try:
                conn.execute("ALTER TABLE working_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")
                conn.commit()
                logger.debug("[WorkingMemory] 已迁移：添加 status 列")
            except Exception:
                pass  # 列已存在，忽略
            # status 索引放在迁移之后，确保列存在
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON working_memory (status)")
                conn.commit()
            except Exception:
                pass

            # ⭐⭐⭐ [2026-08-25] user_note 从「一句话」变成**有结构**。
            #
            # 🔴 要治的现象：Nano 经常说「我后面会注意这一点」，但它并不会真的记下来。
            #    根因不在「记忆类型不够」，在**注入方式**：
            #    同类工具每轮都无条件注入一段摘要，而这里只是很软地声明
            #    「必要时去查记忆」。
            # 📌 所以这不是「缺一个纠错记忆类型」，
            #    是**现有的记忆没有被无条件送到模型眼前**。
            #    ⇒ 不新建表、不新建工具，**改造 `user_note` 让它也能承担纠错**。
            #
            # 四个新列，每个都得能说出「它在被用到的那一刻回答什么问题」：
            #   applies_when  什么时候用得上 —— ⭐ **唯一的真闸**
            #     📌 说不出「什么时候用得上」的东西，本来就不该被记住。
            #     （「这次用中文」填不出 when ⇒ 它本来就不该进记忆）
            #   why           为什么 —— 让这条记忆**换个场景还站得住**
            #     ⚠️ 按内容分不按类型分：行为型必填、事实型可空。
            #        📌 让规则跟着内容走，不要为规则造一个分类（加 type 字段
            #           就等于把刚砍掉的那个分裂又请回来）。
            #   summary_model 一行，**每轮无条件注入给模型的就是它**
            #   summary_user  一行，**用户抽屉里显示的就是它**
            #     🔴 这两句**必须分开**，而查下来当时正踩着这个坑：
            #        `display_text` 是工具参数、**从来没落库**，弹完确认卡就扔了，
            #        抽屉里一直摆的是给模型看的那句 `detail`。
            #     📌 一句话同时服务两个受众，最后两边都不合身。
            # ⚠️ 沿用 status 那套「先 ALTER 再忽略异常」的兼容迁移 ——
            #    老行这四列为空是**事实**，不是缺陷：它们写下时确实没人问过。
            for _col in ("applies_when", "why", "summary_model", "summary_user"):
                try:
                    conn.execute(
                        f"ALTER TABLE working_memory ADD COLUMN {_col} TEXT NOT NULL DEFAULT ''")
                    conn.commit()
                    logger.debug(f"[WorkingMemory] 已迁移：添加 {_col} 列")
                except Exception:
                    pass  # 列已存在，忽略

            # ── 持久语义记忆：semantic_memories 表 ─────────────────────────
            # 和 working_memory 是同一个 db 文件、**不同表**：可以共用 db，
            # 但不能混进 working_memory / recall_working_memory 那套机制 ——
            # 二者用途不同：前者是模型主动 recall 的事件日志，
            # 后者是特定窄入口才查的语义记忆。
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS semantic_memories (
                    id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,

                    task_intent TEXT,
                    target_entity TEXT,
                    action_outline TEXT,
                    skill_ref TEXT,

                    correction_subtype TEXT,
                    wrong_assumption TEXT,
                    correct_behavior TEXT,
                    condition_text TEXT,

                    canonical_text TEXT NOT NULL,
                    display_summary TEXT,

                    confidence REAL NOT NULL DEFAULT 0.3,
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    memory_status TEXT NOT NULL DEFAULT 'implicit',
                    promotion_status TEXT NOT NULL DEFAULT 'not_suggested',
                    promotion_asked_at TEXT,
                    superseded_by TEXT,

                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT,

                    source_event_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sem_type   ON semantic_memories (memory_type);
                CREATE INDEX IF NOT EXISTS idx_sem_status ON semantic_memories (memory_status);
                CREATE INDEX IF NOT EXISTS idx_sem_enabled ON semantic_memories (is_enabled);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 写入 ──────────────────────────────────────────────────────────────

    def add(self,
            entry_type: str,
            subject: str,
            action: str,
            detail: str = "",
            tags: list[str] | None = None,
            session_id: str = "",
            status: str = "confirmed",
            applies_when: str = "",
            why: str = "",
            summary_model: str = "",
            summary_user: str = "") -> int:
        """写入一条记忆。返回新行的 id。

        Args:
            entry_type: EntryType 常量
            subject:    主体名称（Skill 名、文件名、查询词等）
            action:     动作描述（"已部署"、"调用成功"、"写入文件"等）
            detail:     关键细节（文件路径、结果摘要、错误原因等）
            tags:       可搜索标签列表
            session_id: 本次会话 ID（可选，用于区分会话来源）
            status:     "confirmed"（默认）或 "pending"（user_note 待用户确认）
            applies_when:  什么时候用得上（user_note 必填，其余 entry_type 留空）
            why:           为什么（行为型必填，事实型可空）
            summary_model: 注入给模型的一行摘要
            summary_user:  显示在用户抽屉里的一行摘要
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO working_memory "
                    "(session_id, ts, entry_type, subject, action, detail, tags, status, "
                    " applies_when, why, summary_model, summary_user) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, ts, entry_type, subject, action, detail, tags_json, status,
                     applies_when, why, summary_model, summary_user)
                )
                return cur.lastrowid

    # ── 查询 ──────────────────────────────────────────────────────────────

    def search(self,
               keyword: str = "",
               entry_type: str = "",
               limit: int = 20) -> list[dict]:
        """关键词搜索记忆。

        Args:
            keyword:    在 subject/action/detail/tags 里做 LIKE 匹配
            entry_type: 按类型过滤（可选）
            limit:      最多返回条数

        Returns:
            按时间倒序的记录列表，每条是 dict
        """
        conditions = []
        params: list[Any] = []

        if keyword:
            kw = f"%{keyword}%"
            conditions.append(
                "(subject LIKE ? OR action LIKE ? OR detail LIKE ? OR tags LIKE ?)"
            )
            params.extend([kw, kw, kw, kw])

        if entry_type:
            conditions.append("entry_type = ?")
            params.append(entry_type)

        # 排除待确认的 user_note（pending 状态只在 UI 里显示，不参与模型 recall）
        conditions.append("NOT (entry_type = 'user_note' AND status = 'pending')")
        # 🔴 [2026-08-25] **软删除的也要排除。** 加 `soft_delete_by_id` 时，
        #    它的注释里写着「所有读取点走的都是 status='confirmed'，所以软删除之后
        #    它自动从每一处消失」—— **那句话是错的**：抽屉和无条件注入确实是，
        #    但 `search()`（= `recall_working_memory`）**不筛 status**，
        #    于是删掉的记忆照样能被 recall 出来。
        # 📌 而同一段注释里刚写过「一个要求所有读取方都自觉的删除，
        #    漏一个就永远漏」—— **然后当场就漏了一个。**
        # ⚠️ 写成 `!= 'deleted'` 而不是 `== 'confirmed'`：
        #    📌 白名单会顺手把将来新增的状态一起挡掉，而这里要挡的只有「删了的」。
        conditions.append("status != 'deleted'")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        sql = f"""
            SELECT id, ts, entry_type, subject, action, detail, tags
            FROM working_memory
            {where}
            ORDER BY ts DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [dict(r) for r in rows]

    def get_recent(self, limit: int = 10) -> list[dict]:
        """获取最近 N 条记忆，不过滤。"""
        return self.search(limit=limit)

    def get_recent_session_ends(self, limit: int = 5) -> list[dict]:
        """获取最近 N 条 session_end 记录，供 episodic 注入用。按时间正序返回（旧→新），
        让模型按自然时间顺序叙述。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, subject, action, detail, tags FROM working_memory "
                "WHERE entry_type = 'session_end' "
                "ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
        # 反转为正序（旧→新）
        return list(reversed([dict(r) for r in rows]))

    def format_for_model(self,
                         keyword: str = "",
                         entry_type: str = "",
                         limit: int = 15) -> str:
        """把搜索结果格式化成模型可读的自然语言。

        这是 recall_working_memory 伪工具的核心输出。
        """
        rows = self.search(keyword=keyword, entry_type=entry_type, limit=limit)
        if not rows:
            if keyword:
                return f"[Working Memory] No historical records found for {keyword!r}."
            return "[Working Memory] No historical operation records."

        lines = [f"[Working Memory] Found {len(rows)} relevant record(s), newest first:\\n"]
        for r in rows:
            ts_short = r["ts"][5:]  # MM-DD HH:MM:SS
            detail_str = f" -> {r['detail']}" if r["detail"] else ""
            # ⭐ [2026-08-25] user_note 带上 `#id` —— `forget_user_note` 要它。
            #    ⚠️ **只给 user_note**：只有它删得掉（`soft_delete_by_id` 里
            #       有 `AND entry_type='user_note'`）。给别的类型显示 id
            #       等于邀请模型去调一个注定失败的删除。
            #    📌 一个能看见的 id 就是一个会被使用的 id。
            _id_str = f"#{r['id']} " if r["entry_type"] == "user_note" else ""
            lines.append(f"  [{ts_short}] {_id_str}{r['entry_type']} | {r['subject']} | "
                         f"{r['action']}{detail_str}")

        return "\n".join(lines)

    # ── user_note 确认管理 ───────────────────────────────────────────────

    def get_pending_notes(self) -> list[dict]:
        """获取所有待确认的 user_note（status=pending），按时间倒序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, subject, action, detail, tags, "
                "       applies_when, why, summary_model, summary_user "
                "FROM working_memory "
                "WHERE entry_type = 'user_note' AND status = 'pending' "
                "ORDER BY ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_confirmed_notes(self, limit: int = 20) -> list[dict]:
        """获取最近 N 条已确认的 user_note，供抽屉下半区展示。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, subject, action, detail, "
                "       applies_when, why, summary_model, summary_user "
                "FROM working_memory "
                "WHERE entry_type = 'user_note' AND status = 'confirmed' "
                "ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_confirmed_notes(self) -> list[dict]:
        """获取全部已确认 user_note，供弹出完整表格使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, subject, action, detail, "
                "       applies_when, why, summary_model, summary_user "
                "FROM working_memory "
                "WHERE entry_type = 'user_note' AND status = 'confirmed' "
                "ORDER BY ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def confirm(self, note_id: int) -> bool:
        """确认一条 pending user_note，将 status 改为 confirmed。"""
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE working_memory SET status = 'confirmed' "
                    "WHERE id = ? AND entry_type = 'user_note'",
                    (note_id,)
                )
                return cur.rowcount > 0

    def confirm_all_pending(self) -> int:
        """确认所有 pending user_note，返回更新条数。"""
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE working_memory SET status = 'confirmed' "
                    "WHERE entry_type = 'user_note' AND status = 'pending'"
                )
                return cur.rowcount

    # ⭐⭐ [2026-08-25] **删除改成软删除。**
    #
    # 🔴 起因：`forget_user_note` 让 **Nano 自己**也能删记忆了 ——
    #    从「只有用户在抽屉里删」变成「两个入口」。
    # 📌 **那是用户的东西，而 Nano 代删是不可逆的。** `working_memory` 本来就有
    #    `status` 列，改成 `'deleted'` 几乎零成本 ——
    #    ⇒ 把「删错了就没了」变成「删错了能找回来」，便宜到不做没道理。
    # ⚠️ 两个入口**都走这里**：用户在抽屉点删除，和 Nano 调工具，是同一件事，
    #    📌 一个动作有两个实现，它们只在「我两次想法相同」的前提下一致。
    # ⚠️ 所有读取点（抽屉列表 / 无条件注入 / recall）走的都是
    #    `status = 'confirmed'`，所以软删除之后它**自动从每一处消失**，
    #    不需要任何调用方记得过滤。
    #    📌 一个要求所有读取方都自觉的删除，漏一个就永远漏。
    def soft_delete_by_id(self, note_id: int) -> bool:
        """软删除一条记忆（`status='deleted'`）。返回是否命中。"""
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE working_memory SET status = 'deleted' "
                    "WHERE id = ? AND entry_type = 'user_note' AND status != 'deleted'",
                    (note_id,)
                )
                return cur.rowcount > 0

    def delete_by_id(self, note_id: int) -> bool:
        """删除指定 id 的记忆条目。"""
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM working_memory WHERE id = ?",
                    (note_id,)
                )
                return cur.rowcount > 0

    def count_pending_notes(self) -> int:
        """返回待确认 user_note 数量，供 UI 红点角标使用。"""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM working_memory "
                "WHERE entry_type = 'user_note' AND status = 'pending'"
            ).fetchone()[0]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]

    # ── 持久语义记忆：semantic_memories CRUD ───────────────────────────────
    # 这一层只负责 SQLite 的
    # 增删改查和状态校验，不碰 embedding/向量检索（那是 core/memory_index.py
    # 的职责），不碰模型调用（写入前的字段抽取/敏感信息过滤是 orchestrator
    # 调用方的职责）——保持这个文件"零外部依赖"的原则不被破坏。

    def add_semantic_memory(self, *, memory_id: str, memory_type: str,
                            canonical_text: str, display_summary: str = "",
                            task_intent: str = "", target_entity: str = "",
                            action_outline: str = "", skill_ref: str = "",
                            correction_subtype: str = "", wrong_assumption: str = "",
                            correct_behavior: str = "", condition_text: str = "",
                            confidence: float = 0.3,
                            source_event_ids: list[str] | None = None) -> str:
        """写入一条语义记忆，初始 memory_status='implicit'。返回 memory_id。

        condition_text 不允许为空字符串以外的"默认空"语义——调用方必须显式
        传一个值（哪怕是"当前任务上下文"这种兜底描述），不能让它真的是空，
        这是"condition 不能默认空"那条规则在写入层的落实（不在这里
        强制校验，因为这层不该管业务语义，但调用方必须遵守）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO semantic_memories "
                    "(id, memory_type, task_intent, target_entity, action_outline, skill_ref, "
                    " correction_subtype, wrong_assumption, correct_behavior, condition_text, "
                    " canonical_text, display_summary, confidence, source_event_ids, "
                    " created_at, last_seen_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, memory_type, task_intent, target_entity, action_outline, skill_ref,
                     correction_subtype, wrong_assumption, correct_behavior, condition_text,
                     canonical_text, display_summary, confidence,
                     json.dumps(source_event_ids or [], ensure_ascii=False),
                     now, now, now)
                )
        return memory_id

    def get_semantic_memory(self, memory_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM semantic_memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_semantic_candidates(self, memory_type: str, condition_text: str = "",
                                 correction_subtype: str = "",
                                 only_usable: bool = True, limit: int = 50) -> list[dict]:
        """按 memory_type(+condition/subtype锚点) 取候选集合，供调用方在这个
        小范围内做向量相似度比对——不在这层做语义判断，只做结构化粗筛。

        only_usable=True 时只返回 is_enabled=1 且 deleted_at 为空的记录
        （v3"一致性要求"：向量检索召回的结果使用前必须回 SQLite 校验真实
        状态，这个方法本身已经把校验做掉了，调用方不用重复判断）。
        """
        conditions = ["memory_type = ?"]
        params: list[Any] = [memory_type]
        if condition_text:
            conditions.append("condition_text = ?")
            params.append(condition_text)
        if correction_subtype:
            conditions.append("correction_subtype = ?")
            params.append(correction_subtype)
        if only_usable:
            conditions.append("is_enabled = 1 AND deleted_at IS NULL")
        where = "WHERE " + " AND ".join(conditions)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM semantic_memories {where} ORDER BY last_seen_at DESC LIMIT ?",
                params
            ).fetchall()
        return [dict(r) for r in rows]

    def bump_semantic_memory(self, memory_id: str, *, confidence: float | None = None,
                             memory_status: str | None = None) -> bool:
        """命中已有记忆时调用：hit_count+1、刷新last_seen_at，可选更新
        confidence/memory_status（比如第二次命中从 implicit 升级到 pattern）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets = ["hit_count = hit_count + 1", "last_seen_at = ?", "updated_at = ?"]
        params: list[Any] = [now, now]
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(confidence)
        if memory_status is not None:
            sets.append("memory_status = ?")
            params.append(memory_status)
        params.append(memory_id)
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE semantic_memories SET {', '.join(sets)} WHERE id = ?", params
                )
                return cur.rowcount > 0

    def supersede_semantic_memory(self, old_id: str, new_id: str) -> bool:
        """旧记录被新记录取代：memory_status='superseded'，记 superseded_by，
        不删除（v3采纳的"归档而非置信度归零"，保留历史脉络供审计/溯源）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE semantic_memories SET memory_status='superseded', "
                    "superseded_by=?, updated_at=? WHERE id = ?",
                    (new_id, now, old_id)
                )
                return cur.rowcount > 0

    def set_promotion_status(self, memory_id: str, promotion_status: str,
                             mark_asked: bool = False) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets = ["promotion_status = ?", "updated_at = ?"]
        params: list[Any] = [promotion_status, now]
        if mark_asked:
            sets.append("promotion_asked_at = ?")
            params.append(now)
        params.append(memory_id)
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE semantic_memories SET {', '.join(sets)} WHERE id = ?", params
                )
                return cur.rowcount > 0

    def set_semantic_enabled(self, memory_id: str, enabled: bool) -> bool:
        """用户在"任务经验"列表里手动启用/禁用——和系统自动 archived/
        superseded 是不同语义，必须用独立字段。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE semantic_memories SET is_enabled=?, updated_at=? WHERE id = ?",
                    (1 if enabled else 0, now, memory_id)
                )
                return cur.rowcount > 0

    def soft_delete_semantic_memory(self, memory_id: str) -> bool:
        """用户主动删除——deleted_at有值，任何检索/默认注入永不使用。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE semantic_memories SET deleted_at=?, updated_at=? WHERE id = ?",
                    (now, now, memory_id)
                )
                return cur.rowcount > 0

    def list_semantic_memories(self, memory_type: str = "", include_disabled: bool = True,
                               limit: int = 100) -> list[dict]:
        """供UI"任务经验"列表使用：默认包含禁用的（用户要能看到自己关掉的
        是哪些），但永远排除已删除的（deleted_at有值）。
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if memory_type:
            conditions.append("memory_type = ?")
            params.append(memory_type)
        if not include_disabled:
            conditions.append("is_enabled = 1")
        where = "WHERE " + " AND ".join(conditions)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM semantic_memories {where} ORDER BY last_seen_at DESC LIMIT ?",
                params
            ).fetchall()
        return [dict(r) for r in rows]


# ── 全局单例 ──────────────────────────────────────────────────────────────

_store: WorkingMemoryStore | None = None
_store_lock = threading.Lock()


def get_memory_store() -> WorkingMemoryStore:
    """获取全局单例。线程安全。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = WorkingMemoryStore()
    return _store
