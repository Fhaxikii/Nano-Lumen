# tests/

**纯标准库，不引 pytest。** 每个文件是一个可直接执行的脚本，退出码 0 = 全过。

```bat
py -3.10 tests\t_f1_stage1_runtime.py
```

## 为什么不用 pytest

- **零新依赖**。`requirements_cpu.txt` 已经有 40+ 个包，为跑测试再加一个测试框架
  （及其插件链）不值得——这些用例本身不需要 fixture / 参数化 / 收集器。
- **崩溃恢复测试要 spawn 子进程并在真实转移点 `os._exit`**。这类用例在 pytest 里
  反而更别扭（要绕开它的输出捕获与 conftest 加载），裸脚本更直接。
- 输出格式自己控制，可以直接贴给 koala 当验收记录。

## 命名约定

`t_<项>_<阶段>_<领域>.py`。当前：

| 文件 | 验的什么 | 状态 |
|---|---|---|
| `t_f1_stage1_runtime.py` | F1 阶段 1：Runtime Kernel + Thin Task Spine | ✅ 65/65 |
| `t_f1_stage2a_toolbatch.py` | F1 阶段 2A：ToolBatchSpan 四态 + shadow 对答案 + 接线检查 | ✅ 67/67 |
| `t_f1_stage2a_inject.py` | F1 阶段 2A：覆盖表第 3~8 行的故障注入（驱动真实 ReAct 循环） | ✅ 34/34 |
| `t_f1_stage3_interaction.py` | F1 阶段 3 ①：Interaction 表 + revision 绑定 + 崩溃后答案存活 | ✅ 82/82 |
| `t_f1_stage3_routing.py` | F1 阶段 3 ②a/③：拆澄清路由劫持 + answer_open_interaction 全链路 | ✅ 55/55 |
| `t_d12_tool_failure_info.py` | [D12] 工具失败信息的正确性与充分性（真实分发路径，不 patch） | ✅ 40/40 |
| `t_d13_explorer_scope.py` | [D13] Explorer 作用域污染 + **manifest ⊆ dispatcher 的 AST 不变量** | ✅ 40/40 |
| `_console.py` | 不是用例。GBK 控制台保护，**每个用例文件第一行 import** | — |

⚠️ **改完 `core/runtime/` 或 `core/orchestrator.py` 的接线后，七个都要跑**——
每加一个阶段都会动 schema（v1→v2→v3），前面阶段的用例就是回归网。

后续阶段（2A ToolBatchSpan / 3 Approval / 4 WaitCondition / 5 OS lease）各自新增一个文件，
不要往已有文件里堆——每个阶段的崩溃点表和不变量集合不同，混在一起会让"哪个阶段回归了"看不出来。

## 写用例时的四条纪律（来自总纲 §11 [L3] [L5] [L8] [L9]）

1. **故障注入要"确定性"优先于"真实性"。** 目的是验管道通不通，不是验错误分类准不准。
2. **"期望某件事没发生"的用例，必须同时断言"前置条件确实发生过"。**
   否则注入失效会伪装成通过——阶段 1 首跑就撞过：子进程死在建库、压根没到注入点，
   而两个用例因为期望恰好是"什么都没持久化"而假通过。
   本目录的做法是把子进程退出码纳入断言（`exit == 9` 才算真死在注入点），
   并在不符时打印它的 stderr 尾部。
3. **断言名里不许出现 GBK 编不出的字符**（`⭐ ⚠ ↳` emoji…），第一行必须
   `import tests._console`。否则 `print()` 会抛 `UnicodeEncodeError` 中止用例，
   **那些断言从此不进计数**，而现场长得像"跑了一半崩了"——前面的 PASS 已经打出来了。
   这是 [L5] 的同族：假的不是断言结果，是断言**存在**本身。
   注释和 docstring 里随便用，它们不会被 print。
4. **断言"某个标识符已经删干净了"必须用 `ast.walk`，不许 `in src`。**
   文本匹配会被注释和 docstring 打中，而且**留档写得越认真越会中**——
   阶段 3 那次三条里错了两条，错的恰好是源码里认真解释过"为什么删它"的那两个。
   同时按第 2 条补一个前置条件断言：让 AST 在同一棵树里找一个确定还存在的名字，
   证明分析器真的在工作。
