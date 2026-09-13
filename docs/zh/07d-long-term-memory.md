# 07d · 长期记忆

**这篇讲什么**：跨会话记忆的三个载体——语义记忆（学到了什么）、工作记忆（做过什么）、L3 索引条目（哪次对话讲过什么）——各自的写入触发、召回路径与红线。  
**读完你能做什么**：新增一种记忆类型、调整脱敏规则、或修改召回路径而不污染现有机制。  
**前置**：[07 总览](07-memory-and-context.md)、[07a 数据与投影](07a-memory-data.md)、[07b 衰减阶梯](07b-decay-ladder.md)。  

> 语言：中文 · [English](../en/07d-long-term-memory.md)  

---

## 三种记忆，三种生命周期

| 载体 | 存哪 | 记什么 | 谁写入 |
|---|---|---|---|
| 语义记忆 `semantic_memories` | SQLite（权威）+ 向量库（联想） | 长期知识：任务模式、纠错、对话结论 | 触发面代码 + bridge（L3） |
| 工作记忆 `working_memory` | `data/nano_memory.db` | 操作事件账本："做过什么" | 代码埋点 + 模型伪工具 |
| L3 索引条目 | system 动态段 | "哪次对话讲过什么"的路标 | 五箭头流水线（L2→L3） |

三者**互不替代**：删掉向量库只是联想变弱；删掉语义记忆才是真的失忆；
工作记忆是事件流水，不是知识库。

## 语义记忆：写什么、什么时候写

`core/semantic_memory.py`。**触发面是刻意收紧过的**，比设计时设想的窄：

- **`task_pattern`**：只在 OS Skill 部署成功时触发。最初的动机就是
  "OS 任务复现"；数据处理类 Skill 天然永久，已被现有机制服务，边际价值低。
- **`correction`**：只在"Skill 报错后修复成功"路径触发。不接"用户在普通对话里
  随口纠正"这种模糊触发面——那需要在顶层路由各处加判断点，范围太散。
- **`exchange`**：L2→L3 时由 `bridge.py` 写入（见 [07b](07b-decay-ladder.md)），
  与上面两种互不复用。

两条通用纪律：

- **去重后才写**：先 `find_semantic_candidates`（SQLite 按类型取候选，:472）
  再向量 search，命中已有条目则**更新置信度而非新建**（:151-177）。
  否则同类任务模式会随使用次数无限增殖。
- **脱敏是硬代码规则，不能交给模型**：`redact_sensitive`（:30）用正则硬拦
  密码/token/长随机串（`_SENSITIVE_PATTERNS`，:19）。宁可误杀（替换成
  `[REDACTED_*]`），因为写进长期记忆的东西会活很久。

## 工作记忆：`core/memory_store.py`

与 `memory/manager.py`（对话历史，重置时清空）刻意区分：这里存**跨会话的操作
事件**，永久保存。

- **双向写入**：代码埋点写（`add`，:176）+ 模型通过伪工具
  `recall_working_memory` 主动读（`search` → `format_for_model`，:219/:291）。
- ⚠️ **一条已修的教训**（:252 附近）：删除的记忆曾照样被 recall 出来——
  因为 `search` 不筛 status。`user_note` 的 pending 状态只在 UI 显示、
  不参与模型 recall，而"已删除"必须同样从召回里排除。
  **写进模型视野的东西，删除也要同步出模型视野。**

## 召回的两条腿

与 [07b](07b-decay-ladder.md) 的 bridge 同源：**SQLite 是权威，向量库是联想**。

| 腿 | 函数 | 失败时 |
|---|---|---|
| SQLite 按类型 | `find_semantic_candidates`（memory_store 侧） | 整体失败 |
| 向量联想 | `memory_index.search`（:88，chroma + 共享 `_load_embedder` 嵌入） | 降级 + 响亮告警 |

三个读取入口：`retrieve_task_pattern_hint`（:51）、
`retrieve_correction_hints`（:79）、`bridge.recall`（:207）——各自绑定自己的
`memory_type`，**类型错了召回腿就开始返回不相干的东西，且不报错**。

## 改动手把手

**场景 A：新增一种 memory_type**
1. `memory_store.add_semantic_memory` 的 schema/字段（:432）。
2. 写入触发点：模仿 `maybe_write_task_pattern` 的"窄触发 + 去重 + 脱敏"三件套。
3. 召回腿：模仿 `retrieve_*_hint`，**绑定新类型**，绝不复用旧类型的检索。
4. 若由阶梯交接产生：在 `bridge.py` 加显式映射（参考 `MEMORY_TYPE`）。
5. 测试：仿 `t_f5_bridge_l3.py`。

**场景 B：改脱敏规则**
`semantic_memory.py` 的 `_SENSITIVE_PATTERNS`。原则：**硬正则、宁可误杀**；
不要引入"让模型判断是否敏感"的方案——被脱敏的内容会活很久，误放行的代价不可逆。

**场景 C：改召回质量**
`memory_index.py` 的 `search`（:88，top_k 默认 5）与嵌入（共享
`_load_embedder`，见 [07c](07c-meter-budget-guard.md)）。改嵌入模型 = 全库
重嵌，属于大窗口工程，不要顺手做。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_f5_bridge_l3.py`）。
2. 真机：部署一个 OS Skill → 查 `semantic_memories` 表出现 `task_pattern`
   且字段已脱敏；再次部署同类 → 是更新而非新增行。
3. 记忆删除后，`recall_working_memory` 不应再返回它。
4. 阶梯跑到 L3 后：向量库能以自然语言查回那条结论，且索引条目进了
   system 动态段。

---

← 返回 [07 总览](07-memory-and-context.md)
