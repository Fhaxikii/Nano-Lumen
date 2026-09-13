# 06 · 桌面自动化（总览）

**这篇讲什么**：OS 执行层的地图——一条指令从模型到真实屏幕的完整路径、风险模型的核心、以及三个子篇的分工。  
**读完你能做什么**：知道"我要改的东西在哪个子篇"，并理解这一层为什么长成这样。  
**前置**：[02-architecture.md](02-architecture.md)。  

> 语言：中文 · [English](../en/06-os-automation.md)  

---

## 这一层管什么

桌面自动化是 Nano 风险最高的能力：它操作的**不是沙箱，是真实电脑**。因此
这一层的形态由两条硬约束钉死（`core/os_layer/dsl.py`）：

1. **ACTION 枚举封闭**——模型只能从登记过的动作里选，不能自创指令；
   配合动态升级配置表加固。
2. **状态转移表硬编码**——不留给模型自由发挥。

⭐ 枚举一次定全：写操作与鼠标键盘的 action 从第一天起就登记在表里，
只是用 `stage` 标记"从第几档起可执行"——放开新档位只需抬 `max_stage`，
**不用改契约**。

## 执行流水线（六步）

一条 DSL 指令进来，`core/os_layer/dispatch.py` 按固定顺序走：

```
1. validate_instruction   校验 + 算有效风险 + 档位门禁
2. 控制流信号直接返回上层
3. 定位前置   click/type_text 等带语义 target 的动作先由 VisionLocator
              定位拿坐标 + 生成标注截图，再弹确认窗（让用户看到"要点哪"）
4. 授权门禁   risk=1 直接放行；risk=2 查预授权或挂起等确认；
              risk=3 永远挂起等确认
5. 执行       路由到对应执行器，期间挂急停监听
6. audit.record  写审计日志
```

## 风险模型的核心

```
risk = max( 声明风险, 静态地板, 动态升级规则命中 )
```

模型**只能把风险抬高、不能压低**；每个升级原因都进审计日志。
为什么取 max、地板怎么定、动态规则怎么写——见 [06a](06a-instructions-risk.md)。

## 安全网

- **急停双保险**：甩鼠标 failsafe + Ctrl+` 热键，二者在 `executor_action.py`
  的 `EmergencyStop` 里实现。曾有的第三种"软急停"（关键词判定）已删除——
  它的 `_aborted` 标志进程级且无复位，触发一次进程就永久残废（故事在 06c）。
- **Auto 模式兜底**：开启自动模式后，破坏性命令仍会被命令分类器拦下
  （[06c](06c-permissions-audit.md)）。
- **可解释拒绝**：权限拒绝时模型会告诉你需要打开哪个开关。

## 模块地图

| 模块 | 一句话 | 详读 |
|---|---|---|
| `dsl.py` | 指令契约：枚举、风险计算、状态转移表 | [06a](06a-instructions-risk.md) |
| `dispatch.py` | 六步流水线调度入口 | [06b](06b-execution-pipeline.md) |
| `executor_vision.py` | 视觉定位：UIA → 多模态两级降级 | [06b](06b-execution-pipeline.md) |
| `executor_action.py` | 鼠标键盘 + 急停双保险 | [06b](06b-execution-pipeline.md) |
| `executor_write.py` / `executor_low.py` | 系统 API 写操作 / 只读原子能力 | [06b](06b-execution-pipeline.md) |
| `longcmd.py` | 长命令载体：活过工具调用、看得见进度 | [06b](06b-execution-pipeline.md) |
| `window_binding.py` / `filesearch.py` / `fileedit.py` | 窗口身份、文件搜索、文件编辑 | [06b](06b-execution-pipeline.md) |
| `safety.py` | 授权作用域、步数计数 | [06c](06c-permissions-audit.md) |
| `cmd_classifier.py` | Auto 模式意图一致性分类器 | [06c](06c-permissions-audit.md) |
| `audit.py` / `pathpolicy.py` | append-only 审计、敏感路径黑名单 | [06c](06c-permissions-audit.md) |
| `canary.py` | 空闲时视觉定位链路自检 | [06c](06c-permissions-audit.md) |

## 三条铁律

1. **模型不可自创指令或转移路径**——枚举封闭、转移表硬编码。
2. **风险只抬不压**——max 语义，声明再低也压不过地板和升级规则。
3. **只报状态不决策**——dispatch 只抛执行结果，"要不要 replan"由上层
   `_handle_os_task` 决定。

---

## 怎么验证你改对了

本页是地图。验证方式见各子篇末尾；全量回归 `bash run_tests.sh`
（本层专属：`t_os_layer_primitives.py`、`t_cmd_classifier.py`、
`t_os_capability_gate.py`、`t_window_binding.py`、`t_audit_semantics.py` 等）。

---

← 返回 [README](README.md)
