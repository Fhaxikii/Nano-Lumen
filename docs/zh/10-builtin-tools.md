# 10 · 内置工具与工具目录

**这篇讲什么**：内置工具的本体——`ToolDefinition` 统一注册表、感知/调度/流控/预载四个维度、按需加载（核心常驻 vs load_tools）的机制与利弊、两种文案（卡片与意图）、执行作用域，以及"什么时候该用内置工具而不是 MCP/Skill"。  
**读完你能做什么**：新增一个内置工具、调整它的感知/调度/预载属性，或决定一个新能力该走内置、MCP 还是 Skill。  
**前置**：[02-architecture.md](02-architecture.md)、[04-writing-a-skill.md](04-writing-a-skill.md)、[05-mcp-servers.md](05-mcp-servers.md)。  

> 语言：中文 · [English](../en/10-builtin-tools.md)  

---

## 三种能力载体的选择

一个新能力该走哪条路，判据是**它跟 Nano 的核心有多近、需要多少运行时上下文**：

| 载体 | 适合 | 不适合 |
|---|---|---|
| **内置工具** | 需要 Nano 的运行时状态（任务、交互、衰减、窗口、OS）；需要精细的卡片/意图文案；参与调度与流控 | 纯外部能力（联网服务 → MCP）、纯文本处理（可脚本化 → Skill） |
| **MCP server** | 标准协议的外部服务（网页抓取、浏览器、文档检索）；生态里现成的 | 需要运行时状态深度参与；需要精细 UI 卡片 |
| **Skill** | 纯文本处理、可脚本化、用户个性化的小能力 | 需要运行时事实（任务/交互/衰减状态）；有副作用的 OS 操作 |

判据的核心一句话：**内置工具能读 Nano 的运行时视图（`ToolRuntimeView`），
MCP 和 Skill 不能**。凡是"该不该出现取决于 Nano 现在的状态"的能力（回看轮、
召回、等待管理），只能是内置工具。

## 统一注册表：`ToolDefinition`

`core/tools/builtin.py` 是内置工具的**唯一一处声明**（全部 `D(...)` 条目在此声明）。
改造前一个工具的事实散在 **11 处**，漏一处各有各的坏法且都不报错；这份文件
把它们收成一条定义：

```
ToolDefinition（D() 构造，builtin.py）
├── manifest      给模型的 schema（唯一权威）
├── awareness     给模型的一句话感知（必填，构造期拦截缺失）
├── card          工具卡文案 —— 答「Nano 正在做什么」
├── intent        决策文案 —— 答「Nano 打算做什么」（与 card 不是同一句话）
├── detail        工具卡详情块（DetailBlock 列表）
├── scheduling    SERIAL / PARALLEL_SAFE（能否与其他工具同轮）
├── flow          CONTINUE / EXCLUSIVE（是否独占一轮）
├── preload       CORE（常驻）/ DEFERRED（等 load_tools）
├── availability  运行时可用条件（ToolRuntimeView 谓词）
├── handler       MAIN 作用域的执行函数
└── agent / agent_handler  是否进 Subagent 作用域（默认不进）
```

## 按需加载：核心常驻 vs load_tools

**机制**（`core/orchestrator.py` 8 一带）：

- 默认只常驻**极小核心集**；其余工具只在提示词里留一行"感知"（名字+一句话），
  完整 schema 默认不注入，模型需要时调 `load_tools(query=...)` 加载。
- `_tool_pool` 保留全部过门控的 manifest，是 `load_tools` 真正 append 的
  schema 来源——它不是第二份名单，内容整个来自 `advertised`。

**优势**：普通主循环不背全部工具的 schema，每轮 token 成本被压低；
核心集小而稳定，缓存前缀（tools 在最前）命中率高。

**劣势与边界**：模型要先"知道要什么"才能 `load_tools`——感知行写得不好，
模型就想不到去加载；多一轮 load 往返。非动态感知（全量常驻）则反之：
单轮无加载延迟，但每轮背全部 schema，且缓存前缀随轮变动频繁。

### ⭐ 核心集的两段切分（缓存命中的关键）

核心集排成「**无条件在前、带条件在后**」，断点打在中间：

```
[ 无条件核心 ] ⟂ [ 带条件核心 ] + [ load_tools 追加的 ]
```

原因：缓存前缀顺序是 `tools → system → messages`，tools 在最前——它一变，
后面全废。而带条件的核心工具（`dont_wait` / `stop_background` /
`set_next_checkin`）**在轮与轮之间进出**，改的正是最脆弱的位置。
`_core_stable_n`（无条件段的长度）与 `_core_manifest` **必须同源计算**，
不能在别处重新数——否则断点打错位置且不报错。

### availability：工具与事实来源必须同条件

运行时可用条件是 `ToolRuntimeView` 谓词（builtin.py-138），典型几个：

- `_when_has_carrier`：有慢调用在跑（回看轮 ∨ 上一轮交还的）——两个来源
  覆盖不同时刻，缺一不可；只有前者时用户说"把它放后台"，工具不在表里。
- `_when_has_evicted_history`：有交换降到 L3 才给召回——**条件与索引注入
  是同一件事**：有索引 ⇔ 有工具。
- `_when_image_needs_summary`：天然一次性——摘要一写，条件立刻为假。

📌 **铁律：一个工具和它的事实来源，必须由同一个条件控制。** "有工具没事实"
或"有事实没工具"两种半截状态都会让模型开始猜。

## 两种文案：卡片 ≠ 意图

- **card**：答"Nano **正在做什么**"（如「不等它了，先去：查资料」）。
- **intent**：答"Nano **打算做什么**"，写在决策确认场景。
- 两者措辞刻意不同；改造前的逐字对拍表只对了 card，intent 在对拍范围外，
  切换时悄悄退回 card 改掉用户看到的话。
- 卡片要出现**对用户有意义的字段**（如 `next_step`），不是函数名——
  一张工具卡答的是"在做什么"，不是"哪个函数被调用了"。

## 执行作用域（ToolScope，catalog.py）

`MAIN` / `EXPLORATION`（已无工具）/ `SKILL_WRITER` / `OS_LOOP` / `AGENT`。

- **AGENT（Subagent）默认零工具，白名单逐个授予**：新工具只声明 `MAIN` 就
  天然不在 Subagent 里。排除法（"把 X 摘掉即可"）会让每个新工具欠一笔账
  且不报错；白名单只要求记得想要的。
- Subagent 可用**另一个 handler**（目前只有 `os_execute` 只读版）——
  作用域与 handler 的对应关系由目录保证，不由调用点记得传参。

## 五条历史教训（改造前的不一致，本文件按正确侧录入）

1. 5 个工具的 `serial` 声明是死的（同时在 SERIAL 与 EXIT 表里，先判 EXIT
   就 return）→ 录成真实的 `EXCLUSIVE`。
2. 5 个工具没有 awareness → 人工补写（必填项，构造期拦）。
3. `cancel_wait` 不在任何调度表里，靠兜底"碰巧对" → 显式录成 SERIAL。
4. `os_execute` 的 awareness 被 `[]` 截成残句 → 人工写完整句，不列 39 个
   action（它们经 manifest enum 自动进检索文档）。
5. 🔴 **`_BUILTIN_TOOLS_AWARENESS` 是死表**：20 条人工写的好描述从未进过模型
   上下文，模型一直看到的是 `[]` 截出的残句——"写好的正确答案"和"实际
   在用的错误答案"同时存在且互不知道。📌 **一个写好但没人调的东西，比没写
   更坏**：它制造了"已经处理过"的假象。

## 改动手把手

**场景 A：新增一个内置工具**
1. `D(...)` 登记：想清楚六个维度——awareness 写完整句；card 答正在做什么、
   intent 答打算做什么；scheduling 看能否并行；preload 看是否该常驻
   （默认 DEFERRED）；availability 若依赖运行时状态，**与事实来源同条件**。
2. handler 实现 + 路由（MAIN 必给；Subagent 需要则 `agent=True` 并想清权限）。
3. 若它有"事实"（如新的一次性提示），确认注入条件与工具条件同源。
4. 测试：`tests/t_f4_catalog.py`（目录形状）、相关作用域测试。

**场景 B：调整核心集**
改 `preload` 时先想缓存前缀：进核心的工具在轮间是否稳定？带条件的核心工具
进出会打断缓存——这也是它被分成两段的原因。改后验证
`_core_stable_n` 与 `_core_manifest` 同源。

**场景 C：决定一个新能力走哪条路**
按最上面的表判；判不准时问一句——"它的可用性取决于 Nano 的运行时状态吗？"
是 → 内置；否 → 优先 MCP（外部服务）或 Skill（可脚本化）。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_f4_catalog.py`、`t_f1_stage2a_toolbatch.py`）。
2. 新工具在监控抽屉的动态感知里可见性正确：常驻的每轮在、条件的按条件进、
   deferred 只在 load_tools 后出现。
3. 工具卡与意图文案分别核对：卡片答"正在做什么"，确认场景答"打算做什么"。
4. 长对话跑一轮，确认缓存命中未因新工具恶化（provider 日志的 cache_read）。

---

← 返回 [README](README.md)
