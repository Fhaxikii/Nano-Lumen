# core/runtime/store.py
"""
Runtime Kernel 的持久层 —— SQLite schema 与事务边界。

═══ 为什么是 SQLite 快照而不是 event log ═══

两轮评审的结论：**不做 event sourcing**（不做 append-only
domain event log、不做 per-consumer cursor、不做全系统回放），**但必须有一个很小的
durable action outbox**。所以这里只有四张表，没有事件流：

    tasks                 唯一权威的 Task 状态
    runtime_actions       action outbox（transactional outbox + work lease）
    commands              命令幂等台账
    interactions          需要用户回应的事
    projection_receipts   "UI 已展示过"的回执

═══ 三条写死的边界 ═══

1. **`tasks` 刻意没有 `blockers` 列**。
   Approval 与 Wait 已经通过 `owner_task_id` 指向 Task，Task 再存一份就是双权威，
   会出现"Approval 已取消但 Task.blockers 仍含它"。blockers 一律查询派生（见 task.py）。

2. **`presented_at` 在 `projection_receipts`，不在业务表**。
   这是上一轮对 `health.py` 的批评（`HealthState` 里混了 `presented_at` /
   `user_acknowledged_at`，把 UI 通知生命周期塞进了状态权威层）。这次一开始就分开。
   注意主键含 `subject_revision` —— 同一个对象改了内容就是一次新的"待展示"，
   旧回执不该让新版本被当成"已经展示过了"。

3. **`runtime_actions.idempotency_key` 上的 UNIQUE 索引就是去重的物理保证。**
   绝不允许"先 SELECT 看有没有，再 INSERT"——并发或后台生成时会有两个任务
   同时读到"没有"然后各插一条。靠 `INSERT ... ON CONFLICT DO NOTHING` 让数据库定夺。

═══ 为什么用 BEGIN IMMEDIATE ═══

sqlite3 默认是 deferred 事务：BEGIN 时不拿锁，第一次写的时候才升级成写锁。
于是两个连接可以都进入事务、都读了数据、然后其中一个升级失败拿到 SQLITE_BUSY，
表现为**偶发的写失败**。IMMEDIATE 在 BEGIN 时就拿写锁，冲突提前且确定。
这正是那条教训的预防性应用：宁可确定地失败（可重试），不要偶发地失败（难排查）。
"""
from __future__ import annotations

import pathlib
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from loguru import logger

_SCHEMA_VERSION = 12  # v2: 加 tool_batch_spans + shadow_observations
                      # v3: 加 interactions
                      # v4: 加 wait_conditions
                      # v5: 加 os_leases
                      # v6: 加 action_attempts
                      # v7: 加 inbox_items（durable inbox）
                      # v8: wait_conditions 补列 —— 
                      #     加 legacy_susp_id（**第一次加列**，见 _ADD_COLUMNS）
                      # v9: wait_conditions 加 intent；定时计划与条件复查到点后的
                      #     语义不同，不能把这个事实塞进面向人的 reason。
                      # v10: 加 conversation_sessions / conversation_messages；
                      #      对话原文与当前会话边界进入 Runtime SQLite。
                      # v11: 加 runtime_runs —— 每次进程运行的身份与恢复摘要。
                      #      ⚠️ 这张表**只写不读当前身份**：当前 runtime_id 永远来自
                      #      本进程刚生成的那个（`core/runtime/identity.py`）。
                      #      读它的只有崩溃诊断（上一次是什么时候、恢复了什么）。
                      # v12: 加 exchange_decay —— **第三本账**。

# ══════════════════════════════════════════════════════════════════════════
# 后来新增的【列】—— 加表走 `_DDL`，加列走这里
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ `CREATE TABLE IF NOT EXISTS` 对**已存在的表**是空操作，**不会加列**。
#    所以新增字段必须同时做两件事：
#      ① 写进 `_DDL` 的建表语句（新库直接带上）
#      ② 在这里登记一条（老库靠 `ALTER TABLE ADD COLUMN` 补上）
#    📌 **漏了 ② 的后果是「新库有、老库没有」，而开发机通常是新库** ——
#       于是它在你自己的机器上永远正常，只在用户那儿炸。
# ⚠️ 新列**必须允许 NULL**（不能 NOT NULL 且无默认值）：已有行没有这个值。
#    这既是 SQLite 的硬限制，也是语义上的诚实 ——
#    **老数据本来就没有这个信息，不该被塞一个编出来的值。**
_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (表, 列, 类型声明)
    ("wait_conditions", "legacy_susp_id", "TEXT"),   # v8
    ("wait_conditions", "intent", "TEXT"),           # v9（scheduled_plan / condition_recheck）
)


_DDL = """
-- ── 对话会话与原文 ──────────────────────────────────────────────────────
-- Task 是工作归属，不是会话；这两张表只回答「哪段聊天原文属于当前对话」。
-- 旧会话不删除：重置只是切换 current，不是篡改历史。
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id   TEXT    PRIMARY KEY,
    is_current   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL,
    closed_at    REAL,
    close_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_one_current
    ON conversation_sessions(is_current) WHERE is_current = 1;

-- payload_json 存 ChatMessage 的可逆结构，不能存 provider 的 API 半成品：
-- thinking signature、tool_use_id 与工具结果顺序必须能原样重建。
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id   TEXT    PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES conversation_sessions(session_id),
    ordinal      INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    created_at   REAL    NOT NULL,
    UNIQUE(session_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_session
    ON conversation_messages(session_id, ordinal);

-- ── 每次进程运行的身份 ────────────────────────────────────────────────
-- ⚠️⚠️ **这张表不回答「现在是谁」**，只回答「以前跑过哪些」。
--    当前 runtime_id 永远由本进程启动时新生成（见 core/runtime/identity.py）。
--    从这里捞"最后一条"当 current，就等于把一个历史身份冒充成当前身份。
-- ⚠️ 刻意没有 previous_shutdown_clean：现在没有任何地方在正常退出时留标记，
--    而「启动时没发现 interrupted task」推不出「上次干净退出」。
--    📌 不要用「没看到尸体」推导「寿终正寝」。
-- ══════════════════════════════════════════════════════════════════════════
-- exchange_decay —— **第三本账：衰减权威**
-- ══════════════════════════════════════════════════════════════════════════
--
-- 三本账各管一件事，**权威顺序写死**：
--
--     conversation_messages   历史事实权威 —— 用户真正说过什么、当时真正发生过什么
--     exchange_decay          衰减权威     —— 这段历史现在按 L 几投影、派生出了什么
--     MemoryManager.storage   临时投影     —— 随时可以从上面两份重建
--
-- 🔴 **不一致时 `conversation_messages` 永远最高。** `source_hash` 对不上就
--    **响亮告警 → 忽略旧派生物 → 从原始历史重建**。
--    📌 宁可重新花一次提炼费用，也不要拿一个过期 Digest 当真。
--
-- ⚠️ 身份用 `(session_id, start_ordinal)` 而**不是** message_id：
--    回代码核实过 —— `load_messages` 只 SELECT `ordinal/role/payload_json/created_at`，
--    **没有 message_id**；而 `_conversation_ordinal` 现在 live 与 hydrate 两条路都会
--    带回内存、`update_message` 不改它、reset 后 session 天然换代。
--    用 message_id 反而要新增一条身份 plumbing。
--
-- ⚠️ **没有 L1 列，这是刻意的**：L1（历史工具 payload → placeholder）是
--    deterministic 的，重启重算几乎免费。
--    📌 **能推导出来的东西别再存第二份**（同「窗口不复制进 CLAUDE_MODELS」那条）。
--
-- ⚠️ `digest_schema_version` / `distiller_model` 不是装饰：
--    📌 半年后最难查的不是"为什么报错"，而是
--       **"为什么有些旧记忆总比新记忆少一个关键字段"**。
CREATE TABLE IF NOT EXISTS exchange_decay (
    session_id            TEXT    NOT NULL,
    start_ordinal         INTEGER NOT NULL,   -- 身份：那次交换第一条消息的 ordinal
    end_ordinal           INTEGER NOT NULL,   -- 当时覆盖到哪（闭区间末端）
    source_hash           TEXT    NOT NULL,   -- 摘的是不是现在这份内容
    level                 TEXT    NOT NULL,   -- L0 / L1 / L2 / L3 / L4
    digest_json           TEXT    NOT NULL DEFAULT '',  -- L2 结论行（4 字段）
    index_entry           TEXT    NOT NULL DEFAULT '',  -- L3 索引条目（一行字）
    digest_schema_version INTEGER NOT NULL DEFAULT 0,
    distiller_model       TEXT    NOT NULL DEFAULT '',
    updated_at            REAL    NOT NULL,
    PRIMARY KEY (session_id, start_ordinal)
);
CREATE INDEX IF NOT EXISTS ix_exchange_decay_level
    ON exchange_decay(session_id, level, start_ordinal);

CREATE TABLE IF NOT EXISTS runtime_runs (
    runtime_id       TEXT    PRIMARY KEY,
    started_at       REAL    NOT NULL,
    recovery_summary TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_runtime_runs_started
    ON runtime_runs(started_at);

-- ── Task：唯一权威 ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT    PRIMARY KEY,
    parent_task_id  TEXT,
    kind            TEXT    NOT NULL,
    goal_summary    TEXT    NOT NULL DEFAULT '',
    lifecycle       TEXT    NOT NULL,              -- ACTIVE | TERMINAL
    placement       TEXT    NOT NULL,              -- FOREGROUND | BACKGROUND
    execution       TEXT    NOT NULL,              -- IDLE | RUNNING | PAUSED
    current_turn_id TEXT,
    terminal_reason TEXT,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
-- ⚠️ 没有 blockers 列，这是有意的。见模块头第 1 条。
CREATE INDEX IF NOT EXISTS ix_tasks_lifecycle ON tasks(lifecycle);
CREATE INDEX IF NOT EXISTS ix_tasks_parent    ON tasks(parent_task_id);

-- ── action outbox ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runtime_actions (
    action_id        TEXT    PRIMARY KEY,
    idempotency_key  TEXT    NOT NULL,
    kind             TEXT    NOT NULL,
    target_task_id   TEXT,
    status           TEXT    NOT NULL,             -- PENDING | CLAIMED | DONE | FAILED
    claimed_by       TEXT,
    lease_until      REAL,
    fence            INTEGER NOT NULL DEFAULT 0,   -- 递增 fencing token，见 outbox.py
    attempt_no       INTEGER NOT NULL DEFAULT 0,
    payload          TEXT    NOT NULL DEFAULT '{}',
    last_error       TEXT,
    created_at       REAL    NOT NULL,
    completed_at     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_idem   ON runtime_actions(idempotency_key);
CREATE INDEX        IF NOT EXISTS ix_actions_status ON runtime_actions(status);
CREATE INDEX        IF NOT EXISTS ix_actions_lease  ON runtime_actions(lease_until);

-- ── 命令幂等台账 ─────────────────────────────────────────────────────────
-- 必须与业务变更在【同一个事务】里写，否则"命令已记但状态没改"或反之。
CREATE TABLE IF NOT EXISTS commands (
    command_id    TEXT    PRIMARY KEY,
    kind          TEXT    NOT NULL,
    applied_at    REAL    NOT NULL,
    result_json   TEXT    NOT NULL DEFAULT '{}'
);

-- ── ToolBatchSpan ──────────────────────────────────────────────────────
-- 四态而不是两态：显式 Span 只解决"身份与生命周期"，**不自动创造跨存储原子性**
-- （`MemoryManager.storage` 是纯内存 list，与本库不是同一事务资源）。
-- PREPARED = 正要往 memory 写 tool_calls；OPEN = 已经写进去了。
-- 这一档的存在就是为了区分"崩在写之前"和"崩在写之后"。
CREATE TABLE IF NOT EXISTS tool_batch_spans (
    span_id              TEXT    PRIMARY KEY,
    owner_task_id        TEXT,                  -- 归属的 Task；没有归属时为 NULL
    turn_id              TEXT,
    round_idx            INTEGER NOT NULL DEFAULT 0,
    status               TEXT    NOT NULL,       -- PREPARED | OPEN | COMMITTED | ABORTED
    intended_names       TEXT    NOT NULL DEFAULT '[]',  -- PREPARED 时只知道名字，还没有 id
    call_ids             TEXT    NOT NULL DEFAULT '[]',  -- OPEN 时写（memory 归一化后才有）
    result_ids           TEXT,                  -- COMMITTED 时写
    abort_reason         TEXT,
    path_tag             TEXT,                  -- 对应 shadow 覆盖表的行
    -- shadow 期专用：记录旧字段在两个时刻的值，用来对答案
    legacy_flag_at_open  INTEGER,
    legacy_flag_at_close INTEGER,
    created_at           REAL    NOT NULL,
    opened_at            REAL,
    closed_at            REAL
);
CREATE INDEX IF NOT EXISTS ix_spans_status ON tool_batch_spans(status);
CREATE INDEX IF NOT EXISTS ix_spans_turn   ON tool_batch_spans(turn_id);

-- ── shadow 观测账本（迁移期专用，收工后连表一起删）──────────────────────
-- ⭐ 为什么要落盘而不是内存计数：截止信号是【覆盖率】不是时长，
-- 而用户会跨多次会话使用 Nano。内存计数一重启就归零，覆盖表永远填不满。
CREATE TABLE IF NOT EXISTS shadow_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage        TEXT    NOT NULL,
    path_tag     TEXT    NOT NULL,
    diverged     INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    observed_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shadow_path ON shadow_observations(stage, path_tag);

-- ── Interaction ────────────────────────────────────────────────────────
-- 统一"需要用户回应的事"。原来这件事有三条各写各的路由劫持
-- （_pending_skill / _pending_action / _pending_skill_clarification），
-- 它们的共同 bug 是**先清 pending 再去干活**：干活失败时用户的回答已经不存在了。
--
-- 两个正交维度，不要压成一个 kind：
--     mode       INLINE   本轮内必须回应，不回应就卡住   （风险确认）
--                DEFERRED 可以不理，Nano 继续做别的      （Skill 审计 / 澄清）
--     durability EPHEMERAL  不跨重启（重启即作废）
--                PERSISTED  跨重启存活
--
-- ⚠️ EPHEMERAL 的也进这张表，靠启动时 purge 实现"不跨重启"（见 interaction.py）。
--    统一存储换来的是：模型的动态段只有一个来源，不需要"内存里还有几个、库里还有几个"。
CREATE TABLE IF NOT EXISTS interactions (
    interaction_id    TEXT    PRIMARY KEY,
    owner_task_id     TEXT,                    -- 归属的 Task；没有归属时为 NULL
    owner_turn_id     TEXT,
    kind              TEXT    NOT NULL,        -- skill_clarification | skill_audit | os_risk | ...
    mode              TEXT    NOT NULL,        -- INLINE | DEFERRED
    durability        TEXT    NOT NULL,        -- EPHEMERAL | PERSISTED
    status            TEXT    NOT NULL,
    slot              TEXT,                    -- deferred | foreground | NULL（只有可视卡片占槽）
    prompt_text       TEXT    NOT NULL DEFAULT '',   -- 给用户看的那句话
    payload           TEXT    NOT NULL DEFAULT '{}', -- 领域私有；Skill 澄清只有四个字段
    -- ── artifact 绑定 ────────────────────────────────────────────────────
    -- 审批必须钉在**当时那一版**上。只存 id 的话，用户点"同意"时批的可能是
    -- 一个已经被改过的 Skill —— 这是审批语义上最严重的一类错误。
    artifact_kind     TEXT,
    artifact_id       TEXT,
    artifact_revision INTEGER,
    artifact_hash     TEXT,
    -- ── 回答 ────────────────────────────────────────────────────────────
    answer_verbatim   TEXT,                    -- ⭐ 用户原话，不做任何加工
    relation          TEXT,                    -- ANSWER | ANSWER_AND_AMENDMENT | CANCEL
    resolution        TEXT,                    -- 终态原因
    superseded_by     TEXT,                    -- SUPERSEDED 时指向新的 interaction
    revision          INTEGER NOT NULL DEFAULT 1,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL,
    answered_at       REAL,
    closed_at         REAL,
    deadline_at       REAL
);
CREATE INDEX IF NOT EXISTS ix_inter_status   ON interactions(status);
CREATE INDEX IF NOT EXISTS ix_inter_owner    ON interactions(owner_task_id);
CREATE INDEX IF NOT EXISTS ix_inter_turn     ON interactions(owner_turn_id);
CREATE INDEX IF NOT EXISTS ix_inter_artifact ON interactions(artifact_id);
-- ⭐ 前台槽位唯一性交给数据库，不交给"先查再插"（同 runtime_actions.idempotency_key 的理由）。
-- 前台可视卡片最多 1 个。DEFERRED 上限 5 做不成唯一索引，走不变量。
CREATE UNIQUE INDEX IF NOT EXISTS ux_inter_foreground ON interactions(slot)
    WHERE slot='foreground' AND status IN ('OPEN','ANSWERED');

-- ── WaitCondition：Nano 在等"世界"─────────────────────────────────────────
-- ⚠️ 与 interactions 分工：那边等**用户**，这边等**世界**（到点 / 后台回来）。
-- 详见 `core/runtime/waitcond.py` 头部注释。
CREATE TABLE IF NOT EXISTS wait_conditions (
    wait_id        TEXT    PRIMARY KEY,
    owner_task_id  TEXT,                    -- 归属的 Task；没有归属时为 NULL
    owner_turn_id  TEXT,
    kind           TEXT    NOT NULL,        -- timer | background | external
    status         TEXT    NOT NULL,        -- WAITING/SATISFIED/CONSUMED/EXPIRED/ORPHANED/CANCELLED
    wake_on        TEXT    NOT NULL DEFAULT '[]',
    reason         TEXT    NOT NULL DEFAULT '',
    intent         TEXT,
    bg_ref         TEXT,
    -- ⭐⭐ 旧 suspension 记录的 id。
    -- ⚠️ 观测期靠**内存**里一张 `suspension_id → wait_id` 的映射表对答案，
    --    注释还写明了理由（「只比对本进程内发生的事」）—— 那对观测期是对的。
    -- 🔴 但切成权威之后不能靠它：**权威必须跨重启自洽**，而内存映射一重启就没了。
    -- 📌 **「用来对答案的账本」和「用来做决策的账本」对持久性的要求不同** ——
    --    前者可以在内存，后者必须落盘。
    -- ⚠️ 刻意**不复用 `bg_ref`**（那是后台任务引用，串味）也不塞进 `reason`
    --    （那是给人看的话）—— 📌 一个字段不许表达两个现实。
    legacy_susp_id TEXT,
    -- ⚠️ 三个时间字段语义**互不相同**，不许合并（旧 suspension 表把前两个
    --    挤进一个 `timer_at`，直接造成了"不死挂起"）：
    fire_at        REAL,                    -- 到点 = 条件满足（好事）
    expire_at      REAL,                    -- 到点 = 没等到（坏事）
    orphan_at      REAL,                    -- 无条件兜底回收，活记录必须有
    result_json    TEXT,                    -- ⭐ 满足时与状态同事务落盘
    satisfied_at   REAL,
    satisfied_by   TEXT,
    consumed_at    REAL,
    resolution     TEXT,
    revision       INTEGER NOT NULL DEFAULT 1,
    created_at     REAL    NOT NULL,
    updated_at     REAL    NOT NULL,
    closed_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_wait_status ON wait_conditions(status);
-- 要能从旧 id 反查（UI 的 pill 仍以旧 id 为键）
CREATE INDEX IF NOT EXISTS idx_wait_legacy ON wait_conditions(legacy_susp_id);
CREATE INDEX IF NOT EXISTS idx_wait_fire   ON wait_conditions(fire_at);
CREATE INDEX IF NOT EXISTS idx_wait_orphan ON wait_conditions(orphan_at);
CREATE INDEX IF NOT EXISTS idx_wait_owner  ON wait_conditions(owner_task_id);

-- ── OSActivityLease / AuthorizationLease ────────────────────────────────
-- 「谁正在操作这台电脑」+「谁被允许做什么」。取代 `_os_task_busy`（裸 bool）
-- 与 `_temp_auto`（失效条件写在 UI 里的裸 bool）。
--
-- ⚠️ 两种 kind 的规则**相反**，不要合并处理：
--     activity      互斥（同时只能一个）、**必须有 held_until**、重启即释放
--     authorization 可并存、允许无期限、**重启不动**（用户给的许可不随进程生死）
-- 详见 `core/runtime/oslease.py` 头部。
CREATE TABLE IF NOT EXISTS os_leases (
    lease_id       TEXT    PRIMARY KEY,
    kind           TEXT    NOT NULL,        -- activity | authorization
    status         TEXT    NOT NULL,        -- HELD/RELEASED/PREEMPTED/EXPIRED/REVOKED
    holder         TEXT    NOT NULL,        -- nano | user（⭐ 用户也是合法持有者）
    fence          INTEGER NOT NULL DEFAULT 1,   -- 同 outbox：接管即 +1，旧持有者作废
    scope          TEXT    NOT NULL DEFAULT '',  -- authorization 用；空 = 全局
    reason         TEXT    NOT NULL DEFAULT '',
    owner_task_id  TEXT,                    -- 归属的 Task；没有归属时为 NULL
    owner_turn_id  TEXT,
    held_until     REAL,                    -- activity 必须有；authorization 可为空
    payload        TEXT    NOT NULL DEFAULT '{}',
    resolution     TEXT,
    revision       INTEGER NOT NULL DEFAULT 1,
    created_at     REAL    NOT NULL,
    updated_at     REAL    NOT NULL,
    closed_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_lease_kind_status ON os_leases(kind, status);
CREATE INDEX IF NOT EXISTS idx_lease_until       ON os_leases(held_until);
-- ⭐ 互斥的物理保证：同一时刻最多一个 HELD 的活动租约。
--    不变量会再查一次，但让数据库先兜住 —— 竞态下"两个都以为自己在开车"是最危险的。
CREATE UNIQUE INDEX IF NOT EXISTS ux_lease_one_activity ON os_leases(kind)
    WHERE kind='activity' AND status='HELD';

-- ── ActionAttempt：「当前这一个动作做到哪了」────────────────────────────
-- ⭐⭐ 两个**正交**维度，绝对不许压成一个枚举：
--    status       PREPARED → IN_FLIGHT → SUCCEEDED / FAILED / INTERRUPTED
--    effect_state NONE / CONFIRMED / PARTIAL_OR_UNKNOWN
-- 理由：**GUI 没有 rollback。** 输了一半 `abc` 之后被打断，`abc` 真的在记事本里了；
-- 把它记成"中断了"就等于宣称"没发生" —— 那是假事实，比不记更糟。
-- 📌 「明确 commit boundary，而不是幻想 GUI 具备数据库 rollback」。
CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id    TEXT    PRIMARY KEY,
    status        TEXT    NOT NULL,
    effect_state  TEXT    NOT NULL,
    action        TEXT,                    -- DSL action 名（如 type_text）
    tool_name     TEXT,                    -- 工具名（如 os_execute）
    summary       TEXT,                    -- 人话：这一步想干什么
    detail        TEXT,                    -- JSON
    owner_task_id TEXT,                    -- 归属的 Task；没有归属时为 NULL
    owner_turn_id TEXT,
    reason        TEXT,                    -- 终态原因（谁打断的 / 怎么失败的）
    revision      INTEGER NOT NULL DEFAULT 1,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    closed_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_attempt_open  ON action_attempts(status);
CREATE INDEX IF NOT EXISTS ix_attempt_turn  ON action_attempts(owner_turn_id);
-- ⭐ 同一时刻最多一条未结束的尝试。让数据库先兜住 ——
--    两条同时"在动手"意味着我们根本不知道现实被谁改了。
CREATE UNIQUE INDEX IF NOT EXISTS ux_attempt_one_open ON action_attempts(status)
    WHERE status IN ('PREPARED','IN_FLIGHT');

-- ── Inbox：用户的话永不丢 ───────────────────────────────────────────────
-- 🔴 它取代的是 `app.py` 里那句
--      if self.pipeline_lock.locked(): ui.notify('内核正在处理中，请稍候'); return
--    —— **用户打的字直接被丢掉**，得自己记着重发一遍。
-- 📌 与被动挂起那个「闸 vs 挂起」完全同形：
--    **闸的出口是失败，队列的出口是稍后处理；一个只有失败出口的机制，
--      最终一定把成本转嫁给用户去手动重试。**
--    上一次是让 Nano 撞墙，这次是让用户重新打一遍字。
CREATE TABLE IF NOT EXISTS inbox_items (
    item_id        TEXT    PRIMARY KEY,
    status         TEXT    NOT NULL,   -- PENDING / CLAIMED / CONSUMED / DISCARDED
    kind           TEXT    NOT NULL,   -- user_message / wake_intent
    body           TEXT,               -- 用户原话（wake_intent 为空）
    detail         TEXT,               -- JSON：附件 / suspension_id / trigger 等
    -- ⭐ 投递次数：进程崩在「已交给模型、还没标 CONSUMED」之间时，
    --    重启会把它退回 PENDING 再投一次。这个计数让模型能被告知
    --    「这条你可能已经看过」——**同 ActionAttempt：说清结果可不可信，
    --    而不是假装没发生过或悄悄丢掉。**
    delivery_count INTEGER NOT NULL DEFAULT 0,
    owner_task_id  TEXT,               -- 归属的 Task；没有归属时为 NULL
    owner_turn_id  TEXT,               -- 消费它的那一轮
    reason         TEXT,               -- 终态原因
    revision       INTEGER NOT NULL DEFAULT 1,
    created_at     REAL    NOT NULL,
    updated_at     REAL    NOT NULL,
    closed_at      REAL
);
-- 消费侧只关心「还没处理的，按到达顺序」
-- ⚠️ 排序用 `rowid`（真实插入次序），**不用 `created_at`** ——
--    同一时刻提交的两条时间戳相等，会退化成按随机 id 排。所以这个索引
--    只用来筛 status；顺序由 rowid 天然给出。
CREATE INDEX IF NOT EXISTS ix_inbox_pending ON inbox_items(status);
CREATE INDEX IF NOT EXISTS ix_inbox_turn    ON inbox_items(owner_turn_id);
-- ⭐ 同一时刻最多一条被认领。**不是为了并发安全**（asyncio 单线程），
--    而是为了让「谁正在被处理」有唯一答案 —— 崩溃恢复时才知道该退回哪一条。
CREATE UNIQUE INDEX IF NOT EXISTS ux_inbox_one_claimed ON inbox_items(status)
    WHERE status = 'CLAIMED';

-- ── Projection 回执 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projection_receipts (
    subject_kind     TEXT    NOT NULL,
    subject_id       TEXT    NOT NULL,
    subject_revision INTEGER NOT NULL,
    channel          TEXT    NOT NULL,
    presented_at     REAL    NOT NULL,
    PRIMARY KEY (subject_kind, subject_id, subject_revision, channel)
);
"""


class RuntimeStore:
    """SQLite 连接与事务边界。不含任何业务逻辑——业务在 kernel/task/outbox 里。

    连接策略：**每个线程一个连接**（`threading.local`）。
    sqlite3 的连接默认不能跨线程用，而这里的访问者至少有三类：
    UI 线程、RAG 初始化那类普通后台线程、以及将来的执行槽。
    用 `check_same_thread=False` + 共享一个连接会把序列化责任推给我们自己；
    thread-local 让 SQLite 自己用文件锁去序列化，简单且不会写错。
    """

    def __init__(self, db_path: pathlib.Path | str | None = None):
        if db_path is None:
            db_path = pathlib.Path(__file__).parent.parent.parent / "data" / "nano_runtime.db"
        self._db_path = pathlib.Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    @property
    def path(self) -> pathlib.Path:
        return self._db_path

    # ── 连接 ─────────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self._db_path), timeout=10.0, isolation_level=None)
        # isolation_level=None → 关掉 sqlite3 模块的隐式事务管理，
        # 由我们显式 BEGIN IMMEDIATE / COMMIT。否则它会在 INSERT 前偷偷开 deferred 事务。
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")      # 崩溃安全 + 读写不互斥
        conn.execute("PRAGMA synchronous=FULL")      # ⚠️ 见下
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        self._local.conn = conn
        return conn

    # ⚠️ synchronous=FULL 而不是默认的 NORMAL：
    # 本表的全部意义就是"进程被强杀之后还能正确恢复"。WAL + NORMAL 在断电/内核崩溃时
    # 可能丢最后几个已 COMMIT 的事务，而我们的验收表第 3 项写的是
    # "COMMIT 之后立刻杀 → 命令已生效、重放不重复"。NORMAL 下这条会偶发失败。
    # 代价是每次 COMMIT 多一次 fsync；Runtime 的写入频率是"每轮几次"量级，可忽略。

    def close_thread_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ── 事务 ─────────────────────────────────────────────────────────────

    @contextmanager
    def write_txn(self) -> Iterator[sqlite3.Connection]:
        """写事务。BEGIN IMMEDIATE，异常自动 ROLLBACK。

        ⚠️ 不可嵌套。SQLite 没有真正的嵌套事务（SAVEPOINT 是另一套语义），
        嵌套调用会在内层 BEGIN 时抛 "cannot start a transaction within a transaction"。
        Kernel 保证只有 `submit()` 一个地方开写事务，handler 拿到的是已经开好的 conn。
        """
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception as e:      # pragma: no cover - 回滚都失败只能记日志
                logger.error(f"[Runtime] ROLLBACK 失败: {e}")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读访问。不开事务——WAL 下读者看到的是一个一致快照，够用。"""
        yield self.connect()

    # ── schema ───────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn = self.connect()
            try:
                cur_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if cur_ver == 0:
                    self._apply_ddl(conn)
                    logger.info(f"[Runtime] schema 已建立 v{_SCHEMA_VERSION}: {self._db_path.name}")
                elif cur_ver < _SCHEMA_VERSION:
                    # 迁移入口。**不按版本分支**：DDL 全是幂等的增量语句，
                    # 从任何旧版本重跑一遍就到当前版本。以后加表时在这里追加。
                    # ⚠️ 迁移必须是幂等且只增不减的 DDL（CREATE TABLE IF NOT EXISTS /
                    # ADD COLUMN），不许 DROP —— 这个库里存着崩溃恢复的唯一依据。
                    self._apply_ddl(conn)
                    logger.info(f"[Runtime] schema 已迁移 v{cur_ver} → v{_SCHEMA_VERSION}")
                elif cur_ver > _SCHEMA_VERSION:
                    # 用户拿新版本的 data/ 回滚到旧代码。不自愈、不删库——
                    # 这跟 RAG 向量库不同：**Runtime 库不是派生物**，删了就真丢了
                    # （in-flight action、Task 归属都在里面）。只响亮地报警。
                    logger.error(
                        f"[Runtime] ⚠️ 库版本 v{cur_ver} 高于代码期望 v{_SCHEMA_VERSION}。"
                        f"这通常意味着回滚了代码但没回滚 data/。**不会自动重建**"
                        f"（Runtime 库不是派生物，删掉会丢 in-flight 状态）。"
                    )
            except Exception:
                logger.error(f"[Runtime] schema 初始化失败: {self._db_path}")
                raise
            self._initialized = True

    @staticmethod
    def _apply_ddl(conn: sqlite3.Connection) -> None:
        """建表 + 打版本号。

        ⚠️ **不能把 `executescript` 包在显式 `BEGIN IMMEDIATE` 里** ——
        `sqlite3.Connection.executescript()` 会在执行脚本【之前】隐式 COMMIT 掉
        当前事务（CPython 文档明确写了这一点）。于是外层的 `conn.execute("COMMIT")`
        会抛 "cannot commit - no transaction is active"，紧接着 except 分支里的
        ROLLBACK 又抛 "cannot rollback - no transaction is active"，
        **真正的错误被第二个异常盖掉**，表现为一个跟建表毫无关系的报错。

        这个坑第一次跑验收测试就被抓到了（子进程全部 exit=1，而测试 1/2 因为
        "期望什么都没持久化"而假通过）。留档：**凡是 executescript，不许套显式事务。**

        安全性不受影响：DDL 全是 `IF NOT EXISTS`，天然幂等；
        崩在中途最坏结果是下次启动重跑一遍 DDL。
        版本号单独打，且放在 DDL 之后——顺序反了会让"版本已是 1 但表没建完"变成可能。
        """
        # ⚠️⚠️ **补列必须跑在 `_DDL` 之前，顺序反了老库会炸。**
        #
        # 🔴 第一版写成了「先 DDL、再补列」，而 `_DDL` 里有一条
        #    `CREATE INDEX ... ON wait_conditions(legacy_susp_id)` ——
        #    在**老库**上那一列还不存在 → `no such column` → 整个启动失败。
        # ⭐ 而**新库不受影响**：表还没建，`PRAGMA table_info` 返回空 → 补列跳过 →
        #    DDL 一次建好（表自带该列、索引也就能建）。
        #    ⚠️ 于是**只有老库会炸，而开发机通常是新库** ——
        #       这正是上面 `_ADD_COLUMNS` 那段注释刚警告过的形状
        #       「在你自己的机器上永远正常，只在用户那儿炸」，
        #       **那条警告写下之后，紧接着就犯了它的变体**。
        #       抓住它的是「拿真库副本试一遍迁移」这个习惯，不是审读。
        # 📌 **补列步骤必须跑在任何引用那一列的 DDL 之前。**
        RuntimeStore._apply_added_columns(conn)
        conn.executescript(_DDL)
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @staticmethod
    def _apply_added_columns(conn: sqlite3.Connection) -> None:
        """给**已存在的**表补上后来新增的列。

        🔴🔴 **这个机制原来不存在，而上面那段注释声称它存在。**
        原文写着「迁移必须是幂等且只增不减的 DDL（`CREATE TABLE IF NOT EXISTS` /
        **`ADD COLUMN`**）」—— 但 `_apply_ddl` 只跑那一串全是 `IF NOT EXISTS`
        的建表语句，**`CREATE TABLE IF NOT EXISTS` 对已有表是空操作，不会加列**。
        v2→v7 全是**加表**，所以这个缺口从来没被暴露；
        v8 是**第一次加列**，于是它现在必须补上。

        📌 与那个「唤醒源只删了一半」完全同形：
           **注释声称支持某种做法，而实现里没有那条路径** ——
           而且都是因为**那条路径从没被走过**，所以没人发现。
        📌 **一段描述「我们支持 A 和 B」的注释，如果只有 A 被用过，
           那 B 大概率不存在。**

        ⚠️ SQLite 没有 `ADD COLUMN IF NOT EXISTS`，所以靠 `PRAGMA table_info`
           自己判 —— **幂等由构造保证**，不是靠"只跑一次"。
           📌 迁移的幂等性必须是**结构性**的：任何"只能跑一次"的迁移，
              在崩溃重启后就是一颗地雷。
        ⚠️ 新增的列**必须允许 NULL**（不能带 NOT NULL 且无默认值）——
           已有行没有这个值。这条是 SQLite 的硬限制，也是语义上的诚实：
           **老数据本来就没有这个信息，不该被塞一个编出来的值。**
        """
        for _tbl, _col, _decl in _ADD_COLUMNS:
            try:
                cols = {r[1] for r in conn.execute(
                    f"PRAGMA table_info({_tbl})").fetchall()}
                if not cols:
                    continue          # 表还不存在（新库走 DDL 那条路，已带上）
                if _col in cols:
                    continue          # 已经有了
                conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_decl}")
                logger.info(f"[Runtime] schema 补列: {_tbl}.{_col} {_decl}")
            except Exception as e:      # pragma: no cover
                # ⚠️ 一列补不上不该让整个启动失败 —— 但必须**响亮**。
                #    读那一列的代码会拿到异常/None，那比"库开不起来"好。
                logger.error(f"[Runtime] 补列失败 {_tbl}.{_col}: {e}")
