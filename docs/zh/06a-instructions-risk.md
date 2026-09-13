# 06a · 指令与风险

**这篇讲什么**：一条 OS 指令的静态契约——`ActionDef` 注册表、六个能力开关如何真正生效、风险三来源取 max 的计算、动态升级规则的写法、档位与状态转移表。  
**读完你能做什么**：新增一个 action、增补动态升级规则、或开放新的执行档位，而不破坏"模型只能抬高风险"的不变量。  
**前置**：[06 总览](06-os-automation.md)。  

> 语言：中文 · [English](../en/06a-instructions-risk.md)  

---

## ActionDef：一条指令的静态档案

每个 action 在 `core/os_layer/dsl.py` 的 `_ACTIONS` 注册表（里登记：

```
ActionDef
├── name       指令名
├── floor      风险地板（1-3）
├── readonly   是否只读
├── stage      从哪一档起可执行：1=只读 / 2=写操作 / 3=鼠标键盘
├── perms      需要用户在【设置 → OS 权限】里开着哪些能力开关
└── tool       归属工具（OS / COMPUTER_USE）
```

两处容易被误读的设计：

- **`readonly` 与 `stage` 不等价**。`read_screen_region` 是只读的，但
  `stage=3`——它依赖 VisionLocator，而低档位的 dispatch 路由表没挂载它；
  让它在校验层就被拒绝，好过拖到执行层报"未挂载"。
- **未知 action 当最高危**。`action_floor`（对不认识的 action 返回 3——
  fail-closed：字典里没有的东西按最危险处理，不按最乐观处理。

## 六个能力开关为什么真的有效

`perms` 字段补的是 2026-08-20 才发现的一个洞：那 6 个开关里**只有
`allow_mouse_keyboard` 有执行点**，另外 5 个（工作区写入 / 窗口控制 /
系统设置 / 注册表写入 / 高危操作总闸）在全项目**零读取**。用户关掉
「高危操作总闸」，Nano 照样 `run_command`、`file_delete`——开关存进了
配置、UI 也变灰，**看起来完全生效了**。

📌 **一个失效的开关比没有这个开关更危险**——用户会据此放松警惕。
⚠️ 而它坏的方向是**放行**。修法：每个 ActionDef 声明自己需要的开关，
dispatch 在校验阶段逐一核对。

## 风险三来源取 max

`compute_effective_risk`（dsl.py:365）：

```
risk = max( declared_risk, action_floor, 命中规则的 upgrade_to )
reasons = [每次抬升的原因]   ← 全部进审计日志
```

模型声明的风险只是**申报**；地板与升级规则是**客观事实**。取 max 的语义
保证模型只能抬高、不能压低——这是"模型不可自创风险等级"不变量的实现。

## 动态升级规则怎么写

内置规则在 `_DEFAULT_UPGRADE_RULES`（，用户可在
`config/os_config.json` 的 `dynamic_upgrade_rules` 里增补（不需要改代码）。
规则形状：

```json
{
  "action": "launch_app",
  "condition": {"target_in": ["cmd", "powershell", "regedit", "diskpart"]},
  "upgrade_to": 3,
  "reason": "launching a system command-line tool"
}
```

`condition` 支持三种判定：`target_in`（目标落在名单里）、
`foreground_window_in` / `foreground_window_matches`（前台窗口是终端或 IDE
可执行上下文）。内置规则里最典型的一条：**向终端输入文本 → 升 3**——
往 cmd 里打字等价于执行任意命令。

## 档位与 M1/M2/M3

- `stage`：1=只读 / 2=写操作 / 3=鼠标键盘。
- `M1/M2/M3_ALLOWED_ACTIONS`（:226-228）**从 `_ACTIONS` 派生**，不许手抄——
  教训：曾手抄 29 个而实际 39 个，还专挑高频项漏。
- `m1_mode` → `max_stage=1`（铁律：纯只读，click 等必被拒）。

## 状态转移表

`_STATE_TRANSITIONS`（硬编码：一次 OS 任务的合法状态推进只有表里那几条，
模型不能自创路径；`request_replan` 与它是同一个状态机的两个分支。

## 改动手把手

**场景 A：新增一个 action**
1. `_ACTIONS` 登记：name / floor / readonly / stage / **perms**（想清楚需要哪几个开关）/ tool。
2. 对应执行器挂载路由（`executor_low` / `executor_write` / `executor_action` / `executor_vision` 之一）。
3. 只读集合用 `readonly_actions()` 派生，**不许在别处手抄 action 名单**。
4. 测试：`t_os_layer_primitives.py` 补一条；若涉及能力开关，`t_os_capability_gate.py` 同步。

**场景 B：增补动态升级规则**
优先写进 `config/os_config.json`（用户可增补，不需要改代码）；只有"无论谁用
都必须升级"的规则才进 `_DEFAULT_UPGRADE_RULES`。写完用一条会命中的真实指令
验证审计日志里出现升级原因。

**场景 C：开放新执行档位**
抬 `max_stage` 即可——这正是"枚举一次定全"的回报。放开前确认该档位涉及的
perms 开关都有执行点（见上文"失效开关"教训）。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_os_layer_primitives.py`、
   `t_os_capability_gate.py`）。
2. 关掉一个 perms 开关，确认依赖它的 action 被拒且**拒绝理由可解释**。
3. 声明 risk=1 发一条命中升级规则的指令，审计日志应显示被抬到 3 及原因。
4. 未知 action（拼错名字）应被当 risk=3 处理，而不是报"不存在"。

---

← 返回 [06 总览](06-os-automation.md)
