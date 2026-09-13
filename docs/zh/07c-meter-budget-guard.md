# 07c · 计量、预算与守卫

**这篇讲什么**：上下文的"表"——现在占多少（meter）、离水位多远（budget）、这次请求到底能不能发（guard），以及配额怎么配置与调参。  
**读完你能做什么**：调整水位、更换/新增模型窗口、修改输出预留与护栏行为，而不破坏三本账。  
**前置**：[07 总览](07-memory-and-context.md)、[07a 数据与投影](07a-memory-data.md)。  

> 语言：中文 · [English](../en/07c-meter-budget-guard.md)  

---

## 三个模块，三种角色

| 模块 | 只回答一个问题 | 绝不做什么 |
|---|---|---|
| `core/context/meter.py` | 现在的上下文占窗口多少 | **绝不用于计费**（计费只用 `core/usage.py` 的真值） |
| `core/context/budget.py` | 该不该开始收敛 | **不触发任何动作**——只给人看（监控卡）和给模型看（压力动态段） |
| `core/context/guard.py` | 这次请求能不能发 | **不做语义删除**——只允许安全降级或直接失败 |

meter 与计费混用会产生"账单和预算对不上"——两边各自都对，最难查的一类。

## 计量：锚 + 快照差

**为什么不是"写一个 token 估算器"**：上下文不是累加的，每轮都在重新组装——
第 N 次请求的 `input_tokens` 是那一轮的**全部**输入，累加等于把同一段历史数
很多遍（实测三轮累加 38355，真实只有 13400）。而真值本来就每轮都在手上：
`gross = input + cache_read + cache_creation` 是 Anthropic 官方定义的那一次
请求的全部输入（缓存命中的仍是输入，只是便宜）。

⭐ 正解是**锚 + 增量**，增量不是事件账本，而是两次快照相减：

```
predicted = anchor.actual + (estimate(now) − anchor.estimate)
```

它**自动吃掉**历史的增删、工具结果截断、图片占位符化、system 动态段的出现
消失、`load_tools` 新增 schema——一个 hook 都不用接。

关键位置（`core/context/meter.py`）：

| 位置 | 作用 |
|---|---|
| `ContextMeter`（ / `_Anchor`（ | 计量器与锚 |
| `estimate_request`（ / `estimate_text`（ | 发出前的估算 |
| `normalize_prompt_input`（ / `_PROMPT_INPUT_FIELDS`（ | 各厂商 usage 字段归一 |
| `_sample`（:184，`_SAMPLE_MAX=5000` 满了丢最老） | 预测 vs 实际的分布采样（`data/context_samples.jsonl`）——这是**分布**，不是账本 |
| `last_known`（/ `forget_conversation_size`（ | 重启后第一轮的"上次已知值"及其失效 |

⚠️ provider 在真正发出前还会再变形三次（防 400 丢弃消息 / 切 stable-dynamic /
manifest→input_schema）——**Memory 里的变化量 ≠ provider 真正发出去的变化量**，
所以锚要在请求回包时校准，而不是在组装时。

## 预算：三档水位

`core/context/budget.py-26`：

| 水位 | 值 | 给谁看 |
|---|---|---|
| NOTICE | 0.50 | 只进监控卡，不打扰模型 |
| HIGH | 0.70 | 注入给模型（压力动态段） |
| CRITICAL | 0.85 | 注入给模型，措辞更硬 |

- **单位是"占自己窗口的百分比"，不是绝对 token 数**：200K 与 1M 模型共用同一个
  绝对阈值毫无意义。凡是跨模型复用的阈值，单位必须是相对量。
- **这一层只观察，不衰减**。三档暂时不触发动作是刻意的：先量出真实分布再定
  阈值（ meter 的采样就是那份分布）。`level_for`（定档、`snapshot`（
  出快照、`pressure_block`（生成给模型的压力段。

## 守卫：确定性兜底

`core/context/guard.py`。阶梯是启发式（"什么时候该开始遗忘"），guard 是请求
合法性不变量（"这次到底能不能发"）——**启发式底下必须垫一个确定性的兜底**。

- `admissible_input`（：`窗口 × (1 − OUTPUT_RESERVE)`，为输出留余量。
- `preflight`（：发请求前筛查，**永不抛**。`predicted is None` → 放行——
  "我不知道"不该被当成"超了"，否则每次重启后的第一句话就会被拦。
- `in_red_zone`（：够不够格启动紧急降级 / 精确计量。
- **两种超限必须区分**（`classify`）：可通过回收旧上下文解决的 →
  emergency decay；**当前这一轮本身就装不下的** → 直接告诉用户
  （`ContextWindowExceeded`）。反例：用户说"按刚才那个方案改生产配置"，
  关键约束在 30 轮前——guard 说"最老，删"，API 成功了，但 Nano 会**自信地按
  错误约束操作真实电脑**。窗口溢出允许导致本次请求失败，不允许导致不受控的
  语义删除。

## 配额与调参

- 配额 = 各档占**该模型自己窗口**的比例，配置在 `data/model_config.json` 的
  `_quota`（合计约 50%，其余留给 system/工具表底噪、当前轮、突发、输出）。
  读取走 `core/models.py` 的 `quota_of`（；无配置时用
  `_FALLBACK_QUOTA`（L0 0.15 / L1 0.20 / L2 0.15）。
- ⚠️ 这四个数**未标定**——meter 的采样正在积累标定所需分布。
- **实测加速**：`_settings.quota_override` 填 `{"L0":0.03,"L1":0.02,...}`
  可在十几轮内跑完全链；生效期间每次读配额都响亮 warning，验完设回 `null`。

## 改动手把手

**场景 A：调水位** —— `budget.py-26` 三个常量。动之前先看
`data/context_samples.jsonl` 的分布；没有分布数据支撑的阈值是拍脑袋。

**场景 B：换/新增模型窗口** —— 只改 `data/model_config.json` 厂商表的
`window` 字段。guard/budget/decay 全部按比例自动跟随，无代码改动。

**场景 C：改输出预留** —— `guard.py` 的 `OUTPUT_RESERVE`。调小 = 更大可用
输入，但撞模型硬上限 400 的风险变大；这是确定性风险，慎重。

**场景 D：新增厂商的 usage 字段** —— `meter.py` 的 `_PROMPT_INPUT_FIELDS`
加映射 + `normalize_prompt_input` 补分支；否则该厂商的用量归一失败，
锚校准退化（日志有 `_warn_missing_fields`）。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_f5_budget.py`、`t_f5_guard.py`、`t_f5_live.py`）。
2. 真机长对话：监控卡水位应随对话增长按 0.50/0.70/0.85 分档变色。
3. **重启后第一轮**：不应被 guard 拦截（"上次已知"与放行逻辑）。
4. 计费核对：`meter` 的数字永远只做决策参照，真实扣费以厂商后台为准。

---

← 返回 [07 总览](07-memory-and-context.md)
