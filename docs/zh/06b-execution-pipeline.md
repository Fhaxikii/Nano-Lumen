# 06b · 执行链路

**这篇讲什么**：一条通过校验的指令如何走到真实屏幕——定位前置、授权门禁、四类执行器、急停、长命令与审计写入。  
**读完你能做什么**：新增执行器路由、调整确认弹窗时机、或接入长命令，而不破坏"只报状态不决策"的边界。  
**前置**：[06 总览](06-os-automation.md)、[06a 指令与风险](06a-instructions-risk.md)。  

> 语言：中文 · [English](../en/06b-execution-pipeline.md)  

---

## 六步流水线（`core/os_layer/dispatch.py`）

| 步 | 做什么 | 失败/特殊时 |
|---|---|---|
| 1 校验 | `validate_instruction`：校验 + 算有效风险 + 档位门禁（见 [06a](06a-instructions-risk.md)） | 直接回状态 |
| 2 控制流 | 上层下发的控制信号（如终止） | 直接返回上层 |
| 3 定位前置 | 带 `target` 的动作（click/type_text）先经 VisionLocator 拿坐标 + 标注截图 | 定位失败**直接回状态，不弹窗** |
| 4 授权门禁 | risk=1 放行；risk=2 查预授权或挂起等确认；risk=3 **永远**挂起 | 用户拒绝 → 回状态 |
| 5 执行 | 路由到执行器，执行期间挂急停监听 | 异常回状态，不重试（replan 归上层） |
| 6 审计 | `audit.record` 落盘（含风险与升级原因） | — |

两条边界（dispatch 的实现约束）：

- **只报状态不决策**：只抛执行结果；"要不要 replan"由上层 `_handle_os_task` 决定。
- **授权与执行分离**：`safety.py` 只管"允不允许"，不管"怎么执行"。

## 定位前置：先看见，再确认

`executor_vision.py` 的 VisionLocator 把「点击某个界面元素」的语义描述转成
屏幕坐标，**两级降级，成本从低到高**：

1. **UIA**（uiautomation）：读控件树匹配 Name/ControlType，拿真实坐标——快且准。
2. **多模态视觉**：UIA 拿不到时（Canvas / 图片按钮 / 非标准控件），截图交给
   视觉模型；模型由「设置 → 进阶配置 → 视觉」指定，不在代码里硬编码。

两个细节：

- **首次定位校验一次坐标系一致性**（DPI 感知是否生效），不一致会告警
  "点击坐标可能系统性偏移"——只查一次，不重复打扰。
- 定位成功后**生成标注截图**再弹确认窗：用户看到的是"要点哪"，不是一句抽象描述。

## 授权门禁与急停

- risk=2 的「预授权」来自 `safety.py` 的会话级"始终允许"；**risk=3 不受它
  影响，永远单独弹窗**。
- 执行期间挂**急停双保险**：甩鼠标 failsafe + Ctrl+` 热键（`EmergencyStop`，
  executor_action.py）。曾经的第三种"软急停"（关键词判定置 `_aborted`）已
  删除——标志进程级且无任何复位，触发一次整个进程永久残废。**安全机制如果
  没有复位路径，本身就是风险。**

## 四类执行器 + 长命令

| 执行器 | 管什么 | 代表动作 |
|---|---|---|
| `executor_low.py` | 只读原子能力，零写零鼠标 | screenshot / get_sysinfo / read_registry / read_window_tree |
| `executor_write.py` | 系统 API 写操作（不含鼠标键盘） | win_minimize / win_close / set_volume / launch_app |
| `executor_action.py` | 鼠标键盘 + 急停 | click / drag / type_text / hotkey / scroll |
| `executor_vision.py` | 视觉定位（被 3、5 两步共用） | locate |

配套组件：

- **`longcmd.py`**：让一个命令**活过它那次工具调用**且**看得见进度**
  （`LiveCommand`：running / elapsed / `tail(20)`；输出落盘 spill 防刷屏）。
  起因是一次质疑：长命令若不进入与 MCP 后台化同一套等待/回看，下载、pip
  这类命令就会失控。
- **`window_binding.py`**：窗口身份绑定——确保动作打在"当时那扇窗"上。
- **`filesearch.py` / `fileedit.py`**：文件搜索与编辑的原子实现。

## 改动手把手

**场景 A：新增执行器路由**
在 `dispatch.py` 的路由表挂上新 action → 执行器映射；执行器只抛结果。
挂载缺失的错误形态是执行期"未挂载"——新增 action 时选对 `stage`
（见 [06a](06a-instructions-risk.md) 的 `read_screen_region` 例子）可以
让这类错误在校验层就暴露。

**场景 B：调整确认弹窗时机**
弹窗由 `yield {"event":"os_action_confirm"}` 挂起实现，上层消费。
改动时保持两条不变量：risk=3 必弹；定位失败不弹窗（没有可展示的目标）。

**场景 C：接入长命令**
凡可能超过工具调用时长的命令（下载、pip、安装），一律走 `longcmd`，
与 MCP 后台化共用同一套等待/回看机制；不要为单个命令另起临时方案。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_os_layer_primitives.py`、
   `t_window_binding.py`、`t_recheck_longtask.py`）。
2. 真机：分别触发 risk=1/2/3 各一条指令，确认放行/预授权/必弹窗三种路径。
3. 触发一次急停（甩鼠标或 Ctrl+`），确认执行中的动作终止、且**后续任务可正常继续**。
4. 定位一个故意写错的目标，确认返回状态而非弹窗、也而非挂死。

---

← 返回 [06 总览](06-os-automation.md)
