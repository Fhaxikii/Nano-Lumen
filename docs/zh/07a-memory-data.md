# 07a · 数据与投影

**这篇讲什么**：阶梯的**单位**（一次交换）、**衰减账本**（exchange_decay 表）、以及**投影重建**（rebuild_projection）——即"账怎么记、身份是什么、投影怎么从权威重建"。  
**读完你能做什么**：修改交换的切分规则、给账本加字段、或改动水合/重建逻辑而不破坏三本账的权威关系。  
**前置**：[07 总览](07-memory-and-context.md)。  

> 语言：中文 · [English](../en/07a-memory-data.md)  

---

## 一次交换：阶梯的单位

定义在 `core/context/exchange.py`。**一次交换 = 用户那句 + 直到下一句用户话之前的全部内容。**

```
Exchange                     # dataclass，纯只读视图（exchange.py）
├── index          第几次交换（从 0 起）
├── start / end    在消息列表里的 [闭, 开) 区间
├── messages       这段的消息原文
├── user_message   开头那句用户话；可能是 None（见"前导残段"）
├── is_orphan      没有用户开头的残段（历史被截断后的残留）
└── start_ordinal  在落盘账本里的起始 ordinal —— 它的稳定身份
```

四条必须知道的性质：

1. **包含系统注记**。`visible_to_user=False` 的消息也是模型上下文的一部分，
   衰减要算它们的体积。「用户看不看得见」和「占不占上下文」是两个问题。
2. **身份是推导出来的，不是存的**。`start_ordinal` 来自消息上挂的
   `_conversation_ordinal`，而它由两条路都会挂上：`append_message()`（live）
   与 `_message_from_payload()`（水合）。视图不需要 id，但挂在视图上的东西需要。
3. **一个概念只能有一个定义**。`user_message` 的判据必须与 `_opens_exchange`
   完全相同——这里出过一次事故：改了切分规则却没改这个属性，
   「以系统注记开头的残段」被误认成有用户开口。改切分规则时，两处必须同改。
4. **它刻意不进 Kernel、没有存储**。`split(messages)` 与 `user_cut_points(messages)`
   是纯函数（exchange.py / :190）。存了就要回答"它和消息表谁是权威"，
   而那个问题不该存在。历史上这个概念被临时算过三遍、每遍形状不同——
   那正是它该成为一个真实体的信号。

## 衰减账本：`exchange_decay` 表

实现在 `core/context/decay_store.py`（`DecayStore` 类）。档位是**字符串**
`L0/L1/L2/L3/L4`（：日志和 SQLite 里一眼能读，将来加一档（L5）也不会
挪已有数据的含义。

| 方法 | 行 | 用途 |
|---|---|---|
| `source_hash(session_id, start, end)` | :76 | 对**落盘原文**算内容哈希 |
| `record(...)` | :95 | 记一次档位迁移（含派生物） |
| `get` / `load_session` / `active_entries` | :126/:137/:149 | 查询单条 / 整会话 / 未过期条目 |
| `is_stale(entry)` | :176 | 哈希比对：原文变了则条目作废 |
| `level_of(session_id, start)` | :202 | 查一段历史当前档位 |

**铁律：`source_hash` 算的是落盘账本那份，不是内存投影**（`_hash_rows`，:52，
直接从 SQLite 读原文）。原因：内存投影被刻意设计成与落盘不同——图片换占位符、
重启后重算注记，都只动投影。拿投影算哈希，每次重启都会产生新哈希，
每条 Digest 都被判"陈旧"，然后**无限重新提炼（无限花钱）**——
而且现场看起来完全像"内容真的变了"。

## 投影重建：`rebuild_projection`

`core/context/decay.py`。三本账那句承诺的唯一兑现点：

```
conversation_messages（事实） + exchange_decay（档位）
        ↓  project_exchange 逐段
MemoryManager.storage（投影）
```

- **调用点只有两个**：① 水合之后（`MemoryManager._hydrate_current_session`，
  重启 / 会话切换，最终走 `app.py` 的 `_replay_durable_conversation`）；
  ② 每轮五箭头跑完之后（live）。前者漏了会导致重启后分层丢失，
  后者缺席曾造成一个大洞：`run_l1_to_l2` / `run_l2_to_l3` 只改权威不改投影，
  于是"表里说 L2/L3，模型还背着原文"，要等重启才真的生效。
- **live 与水合走同一段代码**，是"两边不会漂"的唯一可靠做法——
  各写一遍的话，它们只在"我两次都想对了"的前提下相等。
- **幂等且永不抛**：同一状态连跑 N 次结果相同；治理层故障绝不许把对话搞挂。

## 改动手把手

**场景 A：给账本加一列**（比如想记录"这次提炼花了多少钱"）
1. `DecayStore.record` 的 INSERT 与表结构（`decay_store.py` 附近）加列。
2. 检查 `get` / `load_session` / `active_entries` 三个读取方是否要带出新列。
3. 老库迁移：SQLite 需 `ALTER TABLE ... ADD COLUMN`，放在 `__init__` 的建表逻辑旁。
4. 测试：`tests/t_f5_decay_store.py`。

**场景 B：改交换的切分规则**（比如让某类系统消息也开启一次交换）
1. 只改 `exchange.py` 的 `_opens_exchange`（。
2. **同步检查 `Exchange.user_message`**（判据必须同源，见上文性质 3）。
3. 下游消费方确认：`decay.py` 与 `rebuild_projection` 都按 `split()` 分组，
   规则变了它们自动跟随，但**已入库的 `exchange_decay` 条目的 start_ordinal
   可能对不上新切分**——`is_stale` 会兜住一部分，跑一遍
   `tests/t_f5_exchange.py` 并在真机上重启一次验证水合。

**场景 C：改水合/重建行为**
只改 `rebuild_projection` / `project_exchange` 一处，live 与水合自动同时生效。
不要在 `app.py` 的 `_replay_durable_conversation` 里另写一份投影逻辑。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_f5_exchange.py` 与 `t_f5_decay_store.py`）。
2. 长对话触发衰减后**重启 Nano**：分层状态应原样恢复，没有整体退回 L0 原文。
3. 同一状态连续触发两次重建，结果应完全相同（幂等）。
4. 监控抽屉的上下文占用与日志中"省了多少"应当一致——不一致说明投影没服从权威。

---

← 返回 [07 总览](07-memory-and-context.md)
