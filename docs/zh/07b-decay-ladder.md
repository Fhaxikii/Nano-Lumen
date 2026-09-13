# 07b · 衰减阶梯

**这篇讲什么**：五个箭头（L0→L1→L2→L3→L4）各自的触发条件、代价、产物与红线，以及结论行（Digest）的提炼契约。  
**读完你能做什么**：修改某一档的降级策略、调整批量与上限、或改动结论行的 schema / 提炼提示词。  
**前置**：[07 总览](07-memory-and-context.md)、[07a 数据与投影](07a-memory-data.md)。  

> 语言：中文 · [English](../en/07b-decay-ladder.md)  

---

## 五个箭头一览

实现在 `core/context/decay.py`；由编排层在每轮 `finally` 中按序调用（见
[07 总览](07-memory-and-context.md)）。

| 箭头 | 函数 | 调模型？ | 驱动条件 | 产物 |
|---|---|---|---|---|
| L0→L1 | `run_l0_to_l1`（ | 否 | L0 占用降到目标以下即停，或本轮降满 `MAX_PER_RUN` | 工具结果正文 → 占位符 |
| L1→L2 | `run_l1_to_l2`（ | **是，每次一个调用** | L1 占用超过该模型配额（`_quota.L1` × 窗口） | 结论行（Digest） |
| L2→L3 | `run_l2_to_l3`（ | 否（结论行已在库里） | 达到 L3 条件的交换 | 语义记忆 + 索引条目 |
| L3→L4 | `run_l3_to_l4`（ | 否 | 索引条目过期 | 索引条目删除（**不删语义记忆**） |

## 三条红线（改任何一档之前先背）

1. **不许破坏 `tool_calls ↔ tool_results` 配对**。降级只换 `content`，
   `tool_use_id` 与顺序必须原样——删掉一半，省下的 token 会变成 provider 400。
2. **`is_error` 必须保留**。它是「尝试过」和「做过」的唯一分界；丢了它，
   一次失败的工具调用在历史里读起来像成功。
3. **只降 closed exchange**。当前这一轮永远 L0——否则可能在 `tool_result`
   还没回来时就把它换成占位符。

## L0→L1：老化（免费档）

把老交换里的工具结果正文整个换成占位符：

```
[Tool output aged out of context to save room. ...约 {n:,} 字符...
如果需要实际内容，重新运行该工具——不要告诉用户结果丢了。]
```

三个要点：

- **L1 是「替换」，不是「第二次截断」**。已有的单条安全阀
  （`_compress_tool_results_inplace`，跑在 `_append` 之前）是 admission——
  当场大到撑爆 request 才截；L1 是 aging——历史不值得每轮背着正文。
  两者正交，永远不打架：不管原始 50K 被安全阀截成 12K 还是没截，
  到 L1 都整个换占位符。
- **`MAX_PER_RUN = 40`（decay.py）不是性能考虑，是防失控**：
  一个"降到达标为止"的循环，一旦达标条件因别的原因永不满足，
  会把整段历史一轮降完。宁可这轮没降够，下轮接着降。
- 由此有一条必须改口的旧说法：`conversation_messages` 存的是**过完安全阀
  之后的规范化历史**，不是"工具返回的每一个原始字节"。

## L1→L2：提炼（付费档）

触发：L1 层交换的占用超过该模型配额（`data/model_config.json` 的 `_quota.L1`
× 模型窗口，`quota_of` / `window_of`）。每次降级 = 一次 LLM 调用，所以它是批，
但**批的是调用次数，不是成败判定**。

关键常量（decay.py 常量区 :54-73）：

| 常量 | 值 | 为什么 |
|---|---|---|
| `MAX_DISTILL_PER_RUN` | 6 | 昂贵档单独的上限。防的是"清不掉的 backlog"——预算打满整天不提炼，次日一开口排着几十个交换。`MAX_PER_RUN` 防失控，这个防账单。追不上时**必须响亮告警**：安静追不上的队列，会以"怎么上下文老是超"的形式活很久 |
| `MAX_PRIOR_LINES` | 8 | 提炼时给多少条**更早**的低分辨率上文（只能比它早，见 `_prior_for`） |
| `_DISTILL_MAX_TOKENS` | 2000 | 提炼调用的输出上限——这个数必须真的传到 provider，否则 schema 上限形同虚设 |

### 结论行（Digest）契约 —— `core/context/digest.py`

schema 五字段：`kind / status / outcome / referents / open_items`。
**本模块不调模型**，只定义"结论行长什么样"和"问什么"；调用在 decay.py。

- **固定 schema 的意义**：输出体积由 schema 决定，不随输入长度增长。
  这掐死了"拿摘要再压摘要"的代际损耗（症状：摘要地板 400k→600k→800k→失忆）。
- **`kind` 由代码判定，不问模型**（`kind_of`）：交换里有工具调用/结果就是
  `work`，否则 `talk`。纯建议类交换刻意判 `talk`——它没有"做成了没有"
  可回答，硬给 `done` 正是"schema 反过来污染事实"。
- **`tool_facts` 由代码统计**（调用数、报错数），喂给提炼器的是客观事实——
  能算出来的就别让模型自己数。
- **上限**（validate 强制）：`outcome ≤ 400 字`、`referents ≤ 6×80 字`、
  `open_items ≤ 4×80 字`。
- **用户原话逐字保留**。L2 压的是"一半"：用户原话原样保留，只把
  Nano/工具/中间过程压成结论行。这修的是一个自相矛盾——"L2 = 一次交换压成
  一条结论行"与"用户原话逐字保留"按字面冲突；拆成两半后，不可再生的
  用户原话才真正保留（实测真人打的字只占一段会话的 0.32%）。
- **不套用 `semantic_memories` 的 schema**。那张表是"Nano 学到了什么"
  （长期知识生命周期），这里是"这次对话发生了什么"；硬套会让模型把普通
  对话硬解释成教训。L2 独立，交接处（bridge）做显式映射。

## L2→L3：交接（fail-closed）

`bridge.py` 执行，顺序不可变：**产生可召回内容 → 持久化 → 验证确实检索得到
→ 生成 L3 索引 → 最后才 commit L3**。任何一步失败 → 继续留在 L2。

- **方向由"失败时谁受损"决定**：留在 L2 的代价是多背一会儿上下文（便宜）；
  反方向失败的代价是内容永久消失且没人知道（不可逆）。所以它必须排在
  eviction 之前。
- **两条腿**：SQLite `semantic_memories` 是**权威**，写不进去整个失败；
  向量库 `memory_index` 是**联想召回**，失败只降级但必须响亮告警——
  一个安静降级的召回腿，会让人以为召回率天生就这样。
- **`memory_type = "exchange"`，独立于已有的 `task_pattern` / `correction`**。
  硬塞进旧类型会污染按类型检索的召回腿，且不报错。

## L3→L4：索引过期

只删索引条目，**不删语义记忆**。遗忘 = 不再自动想起，不等于抹掉——
用户仍可主动问起，记忆仍在库里。

## 改动手把手

**场景 A：调批量与上限**
`decay.py` 常量区（:54-73）。`MAX_PER_RUN` 只在防失控时才动；
`MAX_DISTILL_PER_RUN` 动之前先想清楚账单。改完在真机用
`_quota_override`（`data/model_config.json` 的 `_settings`）做加速验证，
验完必须设回 `null`——生效期间每次读配额都会响亮 warning，这是刻意的。

**场景 B：改结论行 schema**
`digest.py`：`FIELDS` + `empty()` + `validate()` + `render_line()` + 提示词常量，
五处同改。注意：**`is_stale` 只校验原文哈希，不校验 digest 形状**——
库里已按旧 schema 生成的结论行不会自动重造；新增字段必须给宽松默认值，
改字段含义则要考虑让旧条目作废重造。

**场景 C：改提炼提示词**
提示词常量在 `digest.py`。改完用 `quota_override` 造一批降级，检查结论行：
`kind` 是否被模型带偏（它应该被代码钉死）、`outcome` 是否被截断、
用户原话是否原样。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_f5_decay_l1.py`、`t_f5_decay_l2.py`、
   `t_f5_digest.py`、`t_f5_bridge_l3.py`）。
2. 真机长对话 + `_quota_override` 加速触发，逐箭头观察日志与监控抽屉。
3. 重启后分层恢复（见 [07a](07a-memory-data.md) 验证节）。
4. 三条红线逐条自检：配对完整、`is_error` 保留、只动 closed exchange。

---

← 返回 [07 总览](07-memory-and-context.md)
