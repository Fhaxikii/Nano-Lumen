# core/os_layer/dsl.py
"""
OS 层 DSL 核心定义。

本文件是 OS 执行层唯一的指令入口契约。三件事：
1. ACTION 枚举（封闭，模型不可扩展）
2. compute_effective_risk —— 风险地板 + 动态升级（取 max，可证明一致）
3. 状态转移表 —— 硬编码，模型不能自创转移路径

两条硬约束：
- action 枚举**封闭**，配合动态升级配置表加固
- 状态转移表**硬编码**在本文件，不留给模型自由发挥

⭐ 枚举**一次定全**：写操作与鼠标键盘的 action 从一开始就登记在表里，
只是用 `stage` 标记「从第几档起可执行」。放开一档只需要抬 `max_stage`，
**不用改契约** —— 这是当初分批放开权限时定下的形状。
"""
from __future__ import annotations
import re
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from loguru import logger


# ══════════════════════════════════════════════════════════════════════════
# 1. ACTION 枚举（封闭集合）
# ══════════════════════════════════════════════════════════════════════════
# 模型只能从这个集合里选 action，不能自创。新增能力必须先在这里登记。
# 每个 action 标注：风险地板 + 是否只读 + 起始档位 stage。
#
# 字段含义：
#   floor    : 风险地板（1/2/3），declared_risk 只能抬高不能压低
#   readonly : 是否只读（不改变系统/文件/屏幕任何状态）
#   stage    : 从哪一档起可执行（1=只读 / 2=写操作 / 3=鼠标键盘）
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ActionDef:
    name: str
    floor: int
    readonly: bool
    stage: int          # 该 action 从哪一档起可执行：1=只读 / 2=写操作 / 3=鼠标键盘
    desc: str = ""
    # ⭐⭐⭐ 这个 action 需要用户在【设置 → OS 权限】里开着哪些**能力**。
    #
    # 🔴🔴 它补的是一个 2026-08-20 才被发现的洞：那 6 个开关里**只有
    #    `allow_mouse_keyboard` 有执行点**，另外 5 个（工作区写入 / 窗口控制 /
    #    系统设置 / 注册表写入 / **高危操作总闸**）在全项目里**零读取**。
    #    用户把「高危操作总闸」关掉，Nano 照样能 `run_command`、`file_delete` ——
    #    开关会被存进 os_config.json、UI 上也会变灰，**看起来完全生效了**。
    #    📌 **一个失效的开关比没有这个开关更危险** —— 用户会据此放松警惕。
    #    ⚠️ 而它坏的方向是**放行**。
    #
    # ⭐ 能力开关与授权闸是**两层，能力在上游**（2026-08-20 定的语义）：
    #       能力未开放 → 连"要不要授权"这个问题都不该被问到（**auto 也救不了**）
    #       能力开放了 → 才轮到 floor 决定弹不弹窗；auto 管的是这一层
    #
    # ⚠️ **keyword-only 且【没有默认值】** —— 这是这一层唯一真正的防漂措施：
    #    📌 加一个新 action 时**必须回答"它属于哪一类能力"**，
    #       否则连构造都通不过。有默认值的话，下一个 action 会静默落进"谁都不管"。
    perms: frozenset = field(kw_only=True)
    # ⭐ 它归哪个工具（`TOOL_OS` / `TOOL_COMPUTER_USE`）。
    #    ⚠️ **同样 keyword-only 且无默认值** —— 与 `perms` 一个理由：
    #       📌 加新 action 时不回答「它属于哪个工具」，**连构造都通不过**。
    #          有默认值的话，下一个 action 会静默落进错的那个工具，而且不报错。
    tool: str = field(kw_only=True)

# ── stage 语义（修复历史字段坑）──────────────────────────────────────────
# 🔴 旧版用一个 `m1_enabled: bool` 同时表达"只读档是否启用"和"是否已解禁"。
# 放开鼠标键盘那次把这些 action 的 `m1_enabled` 改成 True，于是污染了只读白名单与门禁，
# 导致 `m1_mode=True` 下 click 也能放行 —— **"纯只读"这条铁律出现破口**。
# 📌 一个字段被借去表达第二件事，两件事就会在某次改动里互相污染。
# 改用 stage: int 明确分层，门禁按"当前档位允许的最高 stage"判定，字段不再被借用。
#   stage=1：只读 + 控制流信号（地板恒为 1）
#   stage=2：低危写操作（不含鼠标键盘）
#   stage=3：鼠标键盘控制 + read_screen_region（需 VisionLocator）
# 注意：高危 action（write_registry/file_delete 等地板3）的 stage 仍是 2——它们属于
# “低危写操作的高危变体”：在 stage=2 就登记，靠风险地板=3 强制高危确认，不必等到 stage=3。


# ══════════════════════════════════════════════════════════════════════════
# 能力开关（= 设置 → OS 权限 里那 6 个）—— 键名与 os_config.json 逐字一致
# ══════════════════════════════════════════════════════════════════════════
PERM_WORKSPACE_WRITE = "allow_workspace_write"
PERM_WINDOW_CONTROL = "allow_window_control"
PERM_MOUSE_KEYBOARD = "allow_mouse_keyboard"
PERM_SYSTEM_SETTINGS = "allow_system_settings"
PERM_REGISTRY_WRITE = "allow_registry_write"
PERM_DANGEROUS = "allow_dangerous"

# ⭐ 给模型/用户看的名字。**放在这里而不是 UI 层**，因为拒绝话术要说出
#    「你需要去打开哪一个」，而那句话是模型看的 —— 📌 同一个开关有两份名字时，
#    改了一处另一处就开始说谎。`app._OS_PERMISSION_META` 的标签由测试钉住与此一致。
PERMISSION_LABELS: Dict[str, str] = {
    PERM_WORKSPACE_WRITE: "工作区写入",
    PERM_WINDOW_CONTROL: "窗口控制",
    PERM_MOUSE_KEYBOARD: "鼠标键盘模拟",
    PERM_SYSTEM_SETTINGS: "系统控制",
    PERM_REGISTRY_WRITE: "注册表写入",
    PERM_DANGEROUS: "高危操作总闸",
}

# ⚠️⚠️ **`allow_dangerous` 刻意【不写进任何一行 action】。**
#    它不是一个"能力类别"，它是**风险档**：由 `effective_risk >= 3` 自动要求。
#    ⭐ 这样才对得上现实：`dynamic_upgrade_rules` 会把
#       `launch_app powershell` / 在终端里 `type_text` 这类**静态 floor=2**
#       的动作升到 3 —— 如果总闸跟着静态表走，那些升级上来的就绕过去了。
#    📌 **一个「总闸」如果只对静态标了高危的那些生效，它就不是总闸。**


# ⭐⭐⭐ [2026-08-23] **每个 action 归哪个工具** —— `os_execute` 拆成两个之后的唯一权威。
#
# ═══ 为什么必须是一个新字段，不能从 `stage` / `perms` 派生 ═══
#
# 🔴 试过，切不出来。要的那条线（「是不是在模拟图形界面操作」）
#    在现有字段上**分裂成三簇**：
#       click/drag/type_text…  → stage=3 + PERM_MOUSE_KEYBOARD
#       win_minimize/close/switch → stage=2 + PERM_WINDOW_CONTROL
#       get_cursor_pos            → stage=1 + 无 perms
#    因为 `stage` 答的是「哪一档能跑」、`perms` 答的是「哪个权限开关管它」——
#    📌 **两个都不答「它属于哪个工具」。用它们派生就是又一次「拿回答 A 的字段去回答 B」。**
#
# ⭐ 判据同 v1.54 那条：`allow_dangerous` 一行都不写，因为**它不是能力类别，是风险档**。
#    这里是它的镜像：工具归属不是能力类别，也不是档位，**它是第三个轴**。
#
# ═══ 归属的语义 ═══
#
#   computer_use  = 「**模拟电脑图形界面操作**」
#     ⭐ 判据：**不管因为什么原因要截图，这个动作本身就宣告了
#        「我现在要开始接触图形操作系统了」** —— 意图先于原因，比按场景枚举稳。
#     · 鼠标键盘九件 + 窗口三件（操作窗口，不是查询窗口）
#     · `screenshot` / `read_screen_region` —— 看屏幕就是图形语义的一部分
#     · `request_replan` —— 「看截图重新规划」，只在 GUI 计划循环里有意义
#
#   os_execute    = 其余：文件 / 命令 / 系统 / 剪贴板 / **只读查询**
#     ⭐ 拆完之后它的语义变干净了：**完全不碰图形界面**。
#
# ⚠️ 与 UI 权限开关**正交**，不要混：`screenshot` 归 computer_use，
#    但它**依旧不受任何能力开关限制**（`perms=()`）——
#    「Nano 随时都有截图能力」是明确保留的。
#
# ⚠️⚠️ **绑定关系的口径**（这两条要一起进提示词，别只写一半）：
#    · `set_window_mode(mini)` ⟂ `computer_use`  —— **强制**。
#      GUI 模拟时 Nano 自己挡着，那是系统事实。
#    · `set_window_mode(mini)` ⟂ 截图            —— **只鼓励，不强制**。
#      📌 「Nano 的窗口此刻碍不碍事」只有模型看得见上下文；写死会让它
#         为了走流程多一次往返、还把自己无谓地缩小了。
#         合我们那条分工：**系统答发生了什么，模型答所以怎么办。**
TOOL_OS = "os_execute"
TOOL_COMPUTER_USE = "computer_use"

_ACTIONS: Dict[str, ActionDef] = {
    # ── 只读 / 信号（stage 1，地板 1）─────────────────────────────────────
    "screenshot":         ActionDef("screenshot",         1, True,  1, "截取当前屏幕", perms=(), tool=TOOL_COMPUTER_USE),
    "read_screen_region": ActionDef("read_screen_region", 1, True,  3, "读取屏幕指定区域（OCR/像素）", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    # ↑ 只读，但 stage=3：它要 VisionLocator，低档位的 dispatch 路由表里没挂载它。
    #   stage=3 让它在低档位的校验层就被拒绝，不拖到执行层报"未挂载"。
    "get_sysinfo":        ActionDef("get_sysinfo",        1, True,  1, "查 CPU/内存/磁盘/网络/电源等系统信息", perms=(), tool=TOOL_OS),
    "read_registry":      ActionDef("read_registry",      1, True,  1, "读注册表键值（只读）", perms=(), tool=TOOL_OS),
    "read_window_tree":   ActionDef("read_window_tree",   1, True,  1, "读当前前台窗口的 UIA 控件树", perms=(), tool=TOOL_OS),
    "list_windows":       ActionDef("list_windows",       1, True,  1, "列出当前所有顶层窗口", perms=(), tool=TOOL_OS),
    "get_cursor_pos":     ActionDef("get_cursor_pos",     1, True,  1, "获取当前鼠标坐标", perms=(), tool=TOOL_COMPUTER_USE),
    "wait":               ActionDef("wait",               1, True,  1, "等待 N 秒（无副作用）", perms=(), tool=TOOL_OS),
    "list_dir":           ActionDef("list_dir",           1, True,  1, "列目录文件名（含真实扩展名），操作不确定文件名前先确认用", perms=(), tool=TOOL_OS),
    # 控制流信号（本身不执行实际操作，地板 1；stage=1 任意档位可发，触发上下文可能高危）
    "request_replan":     ActionDef("request_replan",     1, True,  1, "请求模型看截图重新规划剩余步骤", perms=(), tool=TOOL_COMPUTER_USE),
    # 🔴 [OS-ENUM 2026-08-24] 原来写的是 `tool=TOOL_OS` —— **那是错的**。
    #    它和 `request_replan` 是**同一个状态机的两个分支**（`_STATE_TRANSITIONS`）：
    #        AMBIGUOUS → else_request_user_choice
    #        NOT_FOUND / OCCLUDED → then_request_replan
    #    两者都只在**视觉定位**流程里产生，而视觉定位属于 `computer_use`。
    # 📌 它错得很隐蔽：拆分时它**根本不在 enum 里**，于是「归哪个工具」这个问题
    #    从来没有被真正问过 —— 那个字段是**填出来的，不是判出来的**。
    #    ⭐ 这正是解冻 enum 的附带收益：**一个没人调用的条目，它的元数据不会被验证。**
    "request_user_choice":ActionDef("request_user_choice",1, True,  1, "请求用户从多候选中选择", perms=(), tool=TOOL_COMPUTER_USE),

    # ── 鼠标键盘（stage 3）─────────────────────────────────────────────────
    "move":               ActionDef("move",               1, False, 3, "移动鼠标到坐标（不点击）", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "click":              ActionDef("click",              2, False, 3, "鼠标点击（接受语义 target）", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "double_click":       ActionDef("double_click",       2, False, 3, "鼠标双击（接受语义 target）", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "right_click":        ActionDef("right_click",        2, False, 3, "鼠标右键（接受语义 target）", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "drag":               ActionDef("drag",               2, False, 3, "拖拽", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "type_text":          ActionDef("type_text",          2, False, 3, "键盘输入文本", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "hotkey":             ActionDef("hotkey",             2, False, 3, "组合键", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),
    "scroll":             ActionDef("scroll",             2, False, 3, "滚动", perms=(PERM_MOUSE_KEYBOARD,), tool=TOOL_COMPUTER_USE),

    # ── 低危写操作（stage 2，不含鼠标键盘）────────────────────────────────
    "win_minimize":       ActionDef("win_minimize",       2, False, 2, "最小化窗口", perms=(PERM_WINDOW_CONTROL,), tool=TOOL_COMPUTER_USE),
    "win_close":          ActionDef("win_close",          2, False, 2, "关闭窗口", perms=(PERM_WINDOW_CONTROL,), tool=TOOL_COMPUTER_USE),
    "win_switch":         ActionDef("win_switch",         2, False, 2, "切换前台窗口", perms=(PERM_WINDOW_CONTROL,), tool=TOOL_COMPUTER_USE),
    "launch_app":         ActionDef("launch_app",         2, False, 2, "启动 GUI 程序（禁止带可执行参数）", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "set_volume":         ActionDef("set_volume",         2, False, 2, "设置系统音量", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    # 🔴 原描述写的是「读文件内容到 OS 层（**非加载进对话**）」—— **那是假的**
    #    实现返回 `data: {"content": 全文}`，而 orchestrator
    #    那边 `result_text = json.dumps(_os_result)` —— **全文确实进了对话**。
    # 📌 **一句描述如果说的是「它不会做什么」，而它其实会做，
    #    那它比没有描述更危险** —— 读到它的人（包括模型）会据此放松警惕。
    # ⚠️ 而且 `p.read_text()` **没有大小上限**，靠 memory 的
    #    `MAX_SINGLE_TOOL_RESULT_CHARS=12000` 事后截断 →
    #    模型拿到一个**不知道总长、没有偏移量**的 12K 片段。
    #    ⭐ 所以 Nano 当初说 `file_read`「最高频需求、完全缺失」：
    #       **存在性说错了，「不好用」说对了。** 后者由 `load_full_file`
    #       的 `offset`/`limit` 解掉（本项第 2 步）。
    "file_read":          ActionDef("file_read",          2, False, 2, "读文件全文（内容会进入对话；大文件请改用 load_full_file 的 offset/limit）", perms=(), tool=TOOL_OS),
    "file_write":         ActionDef("file_write",         2, False, 2, "写文件", perms=(PERM_WORKSPACE_WRITE,), tool=TOOL_OS),
    "clipboard_read":     ActionDef("clipboard_read",     2, False, 2, "读剪贴板", perms=(), tool=TOOL_OS),
    "clipboard_write":    ActionDef("clipboard_write",    2, False, 2, "写剪贴板", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "open_url":           ActionDef("open_url",           2, False, 2, "用默认浏览器打开 URL", perms=(), tool=TOOL_OS),

    # ── 高危写操作（stage 2 起在枚举里，靠地板=3 强制高危确认）──────────────
    "run_command":        ActionDef("run_command",        3, False, 2, "执行带参命令行", perms=(), tool=TOOL_OS),
    "write_registry":     ActionDef("write_registry",     3, False, 2, "写注册表", perms=(PERM_REGISTRY_WRITE,), tool=TOOL_OS),
    "file_delete":        ActionDef("file_delete",        3, False, 2, "删除文件", perms=(PERM_WORKSPACE_WRITE,), tool=TOOL_OS),
    "file_move":          ActionDef("file_move",          3, False, 2, "移动/重命名文件", perms=(PERM_WORKSPACE_WRITE,), tool=TOOL_OS),
    "kill_app":           ActionDef("kill_app",           3, False, 2, "强制结束进程", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "manage_service":     ActionDef("manage_service",     3, False, 2, "启停/禁用 Windows 服务", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "set_env_var":        ActionDef("set_env_var",        3, False, 2, "修改环境变量", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "schedule_task":      ActionDef("schedule_task",      3, False, 2, "创建计划任务", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "modify_startup":     ActionDef("modify_startup",     3, False, 2, "修改启动项", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
    "network_config":     ActionDef("network_config",     3, False, 2, "修改网络/DNS/代理设置", perms=(PERM_SYSTEM_SETTINGS,), tool=TOOL_OS),
}

# 各档位允许的 action 白名单（按 stage 分层，语义清晰、互不污染）
M1_ALLOWED_ACTIONS = {name for name, a in _ACTIONS.items() if a.stage <= 1}
M2_ALLOWED_ACTIONS = {name for name, a in _ACTIONS.items() if a.stage <= 2}
M3_ALLOWED_ACTIONS = {name for name, a in _ACTIONS.items() if a.stage <= 3}
ALL_ACTION_NAMES = set(_ACTIONS.keys())
# 纯信号 action（不执行实际操作，执行器拿到后交回上层调度，不进真正执行）
CONTROL_FLOW_ACTIONS = {"request_replan", "request_user_choice"}

# ⭐⭐ **会和用户争这台电脑的 action** —— 被动挂起（活动租约）只管这些。
#
# 判据：**这个动作会不会直接操纵输入设备、或者改变用户眼前的窗口状态。**
#
# ⚠️ **不要用「stage 3 减 stage 2」当代理**（第一版就是这么写的，两个方向都错）：
#   · 过严：`read_screen_region` 是只读的，却排在 stage 3
#   · 过松：`win_switch` / `win_minimize` / `win_close` 在 stage 2，
#           可它们**抢焦点、改窗口**，跟用户争的正是同一个东西
#   📌 因为这三档是**能力分期**（当初为分批放开权限而定），不是"争不争鼠标"。
#      **借一个为别的目的定义的分类去回答另一个问题，边界一定不对。**
#
# ⚠️ 实际运行中发现的那条（2026-08-07）：让 Nano 用 GUI 打开一个 txt，
#    它实际走了**命令行**，而这时用户点桌面**照样**被判成"已让出控制"。
#    判据是：覆盖的是 GUI 模拟，不是整个 OS 控制能力。
#    `run_command` / `list_dir` / `file_*` / `screenshot` 都不与用户争鼠标 ——
#    **用户点桌面不会让 `dir` 的结果失效。**
#
# 📌 `launch_app` / `open_url` **刻意不算**：它们确实会弹窗抢一下焦点，
#    但**不依赖任何先前的屏幕状态**，用户动手不会让它们失效；
#    把它们也挡住，就是把那个问题扩大到更大一片。
#    被动挂起要保护的是**多步 GUI 推理被作废**，不是"任何会动窗口的事"。
CONTENDS_FOR_MACHINE = {
    # 直接操纵输入设备
    "click", "double_click", "right_click", "drag", "move",
    "type_text", "hotkey", "scroll",
    # 改变用户眼前的窗口状态（与用户争同一批窗口）
    "win_switch", "win_minimize", "win_close",
}


def contends_for_machine(action: str) -> bool:
    """这个 action 需不需要持有活动租约。**未知 action 按需要处理**（保守）。"""
    if not action:
        return True
    if action in CONTROL_FLOW_ACTIONS:
        return False
    return action in CONTENDS_FOR_MACHINE or action not in ALL_ACTION_NAMES


def is_known_action(action: str) -> bool:
    return action in _ACTIONS


def action_floor(action: str) -> int:
    a = _ACTIONS.get(action)
    return a.floor if a else 3  # 未知 action 当最高危处理


def readonly_actions() -> frozenset:
    """全部只读 action。⚠️ **从 `_ACTIONS` 派生，不许手抄** ——
    📌 记着 `_OS_ACTIONS` 手抄 29 个而实际 39 个、专挑高频项漏。
    """
    return frozenset(n for n, d in _ACTIONS.items() if d.readonly)


def is_readonly(action: str) -> bool:
    a = _ACTIONS.get(action)
    return bool(a and a.readonly)


def action_stage(action: str) -> int:
    """该 action 从哪一档起可执行（1/2/3）。未知 action 返回 99（永不放行）。"""
    a = _ACTIONS.get(action)
    return a.stage if a else 99


def is_stage_allowed(action: str, max_stage: int) -> bool:
    """当前档位（max_stage）是否允许执行该 action。"""
    return action_stage(action) <= max_stage


# ══════════════════════════════════════════════════════════════════════════
# 2. 动态升级规则 + 风险计算
# ══════════════════════════════════════════════════════════════════════════
# effective_risk = max(declared_risk, floor, 命中的所有动态规则的 upgrade_to)
# 取 max → 模型只能把风险抬高，不能压低；多规则命中也只取最严，结果唯一确定。
# （这就是对"规则冲突消解"的回答：max 即消解，可证明一致）
#
# 规则表来自 config/os_config.json，用户可加，不硬编码在代码里。
# 这里提供 load + match 逻辑，以及一份内置默认规则（配置缺失时兜底）。
# ══════════════════════════════════════════════════════════════════════════

_DEFAULT_UPGRADE_RULES: List[Dict[str, Any]] = [
    {"action": "launch_app",
     "condition": {"target_in": ["cmd", "powershell", "regedit", "wmic", "mshta", "diskpart", "cscript", "wscript"]},
     "upgrade_to": 3, "reason": "launching a system command-line tool"},
    {"action": "type_text",
     "condition": {"foreground_window_in": ["cmd", "powershell", "ConsoleHost", "WindowsTerminal"]},
     "upgrade_to": 3, "reason": "typing text in a terminal"},
    {"action": "type_text",
     "condition": {"foreground_window_matches": r"DevTools|Console|.*IDLE.*|PyCharm|VSCode.*[Tt]erminal|Jupyter"},
     "upgrade_to": 3, "reason": "typing in a browser console or IDE interactive window (executable context)"},
    {"action": "file_write",
     "condition": {"path_matches": r"C:\\Windows|C:\\System32|.*Startup.*"},
     "upgrade_to": 3, "reason": "writing to a system directory or startup location"},
    {"action": "file_move",
     "condition": {"path_matches": r"C:\\Windows|C:\\System32"},
     "upgrade_to": 3, "reason": "moving a file under a system directory"},
    {"action": "clipboard_read",
     "condition": {"foreground_window_in": ["1Password", "KeePass", "Bitwarden"]},
     "upgrade_to": 3, "reason": "reading clipboard while a password manager is foreground"},
]


def _rule_matches(rule: Dict[str, Any], action: str, params: Dict[str, Any],
                  foreground_window: str) -> bool:
    if rule.get("action") != action:
        return False
    cond = rule.get("condition", {}) or {}
    fg = (foreground_window or "").lower()

    # target_in：params 里的可执行目标命中关键字列表（用于 launch_app/run_command）
    if "target_in" in cond:
        target = str(params.get("target") or params.get("app") or params.get("path") or "").lower()
        if not any(t.lower() in target for t in cond["target_in"]):
            return False
    # foreground_window_in：前台窗口名命中列表
    if "foreground_window_in" in cond:
        if not any(w.lower() in fg for w in cond["foreground_window_in"]):
            return False
    # foreground_window_matches：前台窗口名正则
    if "foreground_window_matches" in cond:
        if not re.search(cond["foreground_window_matches"], foreground_window or "", re.IGNORECASE):
            return False
    # path_matches：params 路径正则
    if "path_matches" in cond:
        path = str(params.get("path") or params.get("dest") or params.get("target") or "")
        if not re.search(cond["path_matches"], path, re.IGNORECASE):
            return False
    return True


def compute_effective_risk(action: str,
                           params: Optional[Dict[str, Any]] = None,
                           declared_risk: int = 1,
                           foreground_window: str = "",
                           upgrade_rules: Optional[List[Dict[str, Any]]] = None) -> tuple[int, List[str]]:
    """计算有效风险等级。返回 (risk, reasons)。

    risk = max(declared_risk, floor, 命中规则的 upgrade_to ...)
    reasons：升级原因列表，供审计日志记录"为什么是这个等级"。
    """
    params = params or {}
    rules = upgrade_rules if upgrade_rules is not None else _DEFAULT_UPGRADE_RULES

    floor = action_floor(action)
    risk = max(int(declared_risk or 1), floor)
    reasons: List[str] = []
    if floor > (declared_risk or 1):
        reasons.append(f"floor[{action}]={floor}")

    for rule in rules:
        try:
            if _rule_matches(rule, action, params, foreground_window):
                up = int(rule.get("upgrade_to", 1))
                if up > risk:
                    risk = up
                    reasons.append(f"dynamic upgrade -> {up}: {rule.get('reason', '')}")
                elif up == risk:
                    reasons.append(f"dynamic rule matched at same level: {rule.get('reason', '')}")
        except Exception as e:
            logger.warning(f"[OS-DSL] 升级规则匹配异常，已跳过: {e} | rule={rule}")

    return risk, reasons


def load_upgrade_rules(config_path: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    """从 config/os_config.json 读 _DYNAMIC_UPGRADE_RULES，缺失则用内置默认。"""
    if config_path is None:
        from core.paths import ROOT as _ROOT
        config_path = _ROOT / "config" / "os_config.json"
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            rules = data.get("dynamic_upgrade_rules")
            if isinstance(rules, list) and rules:
                return rules
    except Exception as e:
        logger.warning(f"[OS-DSL] 读取 os_config.json 升级规则失败，用内置默认: {e}")
    return list(_DEFAULT_UPGRADE_RULES)


# 默认权限（配置缺失时兜底）——全部默认关，符合"默认只读/默认不暴露"地基第 2 条
_DEFAULT_PERMISSIONS: Dict[str, bool] = {
    "allow_workspace_write": False,
    "allow_window_control": False,
    "allow_mouse_keyboard": False,
    "allow_system_settings": False,
    "allow_registry_write": False,
    "allow_dangerous": False,
}


def os_state_path() -> pathlib.Path:
    """用户侧 OS 状态（六个权限开关 + auto 模式）的落盘位置。**唯一出处。**

    ⚠️ 它**不在** `config/os_config.json` 里，判据是「这份数据跟着谁走」：
       升级版本时 `config/` 会被覆盖，而这两项是用户自己拨的，不能跟着被覆盖。
       📌 同 `docs/zh/03-configuration.md` 里 `model_config` 与 `throttle_config`
          必须分开的那一条 —— 混在一张表里，升级时要么丢用户选择，要么更新不了版本事实。
    🔴 而这个文件的用户状态还多一层后果：带着开发机上拨开的开关发布，
       等于新用户一装上所有闸门就是开的。
    ⚠️ 文件不存在是**正常状态**（新装的机器就没有），不是故障 ——
       读取方一律用 `_DEFAULT_PERMISSIONS`（全关）兜底，fail-closed。
    """
    from core.paths import data_path
    return data_path("os_state.json")


def auto_authorization_on(config_path: Optional[pathlib.Path] = None) -> bool:
    """现在是不是 **Auto**（授权交给 Nano 自己判断，不逐个询问）。

    ⭐⭐ **唯一出处。** 它由两半组成，缺一不可：
       · `os_config.json` 的 `auto_mode` —— 用户在设置里拨的**长期**模式
       · `oslease.temp_auto_authorized()` —— **本次临时**授权租约
    🔴 改造前这个公式只活在 `app._auto_on()` 里，于是**模型侧完全看不到 auto**：
       开着 auto 时它和不开时看到的东西一模一样，说不出"我不需要请求授权"。
    📌 一个要被两层同时消费的判断，不该只写在其中一层里 ——
       另一层要么读不到，要么照着自己的理解再写一遍。

    ⚠️⚠️ **它答的不是「这项能力开不开放」**（那是 `missing_permissions`）。
       两者是上下游：能力没开放时，auto 一点用都没有 ——
       📌 auto 豁免的是「要不要逐个问」，不是「能不能做」。
    ⚠️ fail-safe 方向是 **False**（照常弹确认）。
    """
    if user_auto_mode_on(config_path):
        return True
    try:
        from core.runtime import oslease as _ol
        return bool(_ol.temp_auto_authorized())
    except Exception:
        return False


def user_auto_mode_on(config_path: Optional[pathlib.Path] = None) -> bool:
    """用户自己选的是不是 Auto（`os_state.json` 的 `auto_mode`；不含 GUI 任务的临时授权）。"""
    if config_path is None:
        config_path = os_state_path()
    try:
        if config_path.exists():
            return bool(json.loads(config_path.read_text(encoding="utf-8"))
                        .get("auto_mode", False))
    except Exception as e:
        logger.warning(f"[OS-DSL] 读 auto_mode 失败，按未开启处理: {e}")
    return False


def set_user_auto_mode(on: bool, config_path: Optional[pathlib.Path] = None) -> bool:
    """保存用户选的 Ask / Auto（`os_state.json` 的 `auto_mode`，保留其它字段）。返回是否写成功。"""
    if config_path is None:
        config_path = os_state_path()
    try:
        raw = {}
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["auto_mode"] = bool(on)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"[OS-DSL] 保存 auto_mode 失败: {e}")
        return False


def auto_skips_confirmation(what: str) -> bool:
    """执行确认（Skill 副作用、临时代码、MCP 不可逆操作）在 Auto 下直接通过、不发确认事件。

    Auto = 用户选的 Auto，或 GUI 任务期间的临时授权（`auto_authorization_on`）。
    放行时记一条日志，`what` 写进日志。
    """
    if auto_authorization_on():
        logger.info(f"[Auto] 自动放行执行确认：{what}")
        return True
    return False


def required_permissions(action: str, effective_risk: int = 0) -> frozenset:
    """这一次执行需要哪些能力开关同时开着。**权威只有 `_ACTIONS` 这一张表。**

    ⭐ 两个来源，合起来：
       ① `spec.perms` —— 静态的「它属于哪一类能力」
       ② `effective_risk >= 3` → 追加 `allow_dangerous`（**总闸跟着有效风险走**）
    📌 ② 是必需的：`dynamic_upgrade_rules` 会把静态 floor=2 的动作
       （`launch_app powershell` / 终端里 `type_text`）升到 3 ——
       总闸若只看静态表，升级上来的那些正好全部绕过它，
       **而那批恰恰是最该被总闸挡住的**。
    ⚠️ 未知 action 返回**总闸**而不是空集：📌 fail-safe 朝「多要一道」错，
       不朝「谁都不管」错。
    """
    spec = _ACTIONS.get(action)
    if spec is None:
        return frozenset({PERM_DANGEROUS})
    need = set(spec.perms)
    if int(effective_risk or 0) >= 3:
        need.add(PERM_DANGEROUS)
    return frozenset(need)


def missing_permissions(action: str, permissions: Dict[str, bool],
                        effective_risk: int = 0) -> list:
    """哪些该开的没开。空列表 = 放行。**顺序稳定**（给用户看的话不能每次不一样）。"""
    need = required_permissions(action, effective_risk)
    return [k for k in PERMISSION_LABELS if k in need
            and not bool((permissions or {}).get(k, False))]


# ── 启动期不变量：不许出现「谁都不管」的写动作 ────────────────────────────
#
# 📌 `perms` 无默认值只挡住了「忘了填」，挡不住「填了个空集」。
#    这两条把剩下的那半也钉住 —— 而且它们是**派生的**，不是又一张手抄名单。
# ⚠️ floor==2 却**刻意**不归任何能力的那几个。**每一条都要有理由**，
#    而不是"想不出该放哪就放这"。
_NO_CAPABILITY_GATE = frozenset({
    # 不改变任何外部状态；它们落在 floor=2 是因为**隐私**（读任意文件 / 读剪贴板），
    # 而隐私由确认弹窗管，不由「能力开关」管。
    "file_read", "clipboard_read",
    # ⚠️ **归属存疑**：它确实启动了浏览器，但 UI 上那句「调整音量、亮度等系统级
    #    设置」对不上它 —— 📌 硬塞进一个描述对不上的开关，是让那个开关说谎。
    #    留在这里并显式记账，等有更合适的档位再挪。
    "open_url",
})


def _assert_perm_table_sane() -> None:
    _bad = []
    for _n, _s in _ACTIONS.items():
        # ① **真正要防的是「谁都不管」的动作**，而它只可能出现在 floor==2：
        #      floor 1  → 只读，本来就不需要能力开关
        #      floor >=3 → `allow_dangerous` 由有效风险**自动**要求，恒有闸
        #      floor 2  → 确认弹窗**可以被 auto 跳过** ⇒ 没有能力归属 = 零闸
        #    ⚠️ 判据用 `floor` 而不是 `readonly` —— 那个字段本身就不准
        #       （`file_read` 明明不改东西却 readonly=False）：
        #       📌 一条不变量不该建立在一个已知不准的字段上。
        #    🔴 第一版写成了 `floor >= 3`，启动时当场被 `run_command` 打红 ——
        #       而**红得不对**：它 floor=3，总闸恒罩着它。
        #       📌 一条不变量抓到东西时，先问它抓的是不是它声称要抓的那件事。
        if _s.floor == 2 and not _s.perms and _n not in _NO_CAPABILITY_GATE:
            _bad.append(f"{_n}: floor=2 且没有能力归属 —— auto 一开就是零闸")
        # ② 声明的能力必须真的存在于那 6 个开关里（防手滑打错字符串）
        for _k in _s.perms:
            if _k not in PERMISSION_LABELS:
                _bad.append(f"{_n}: 未知能力键 {_k!r}")
        # ③ `allow_dangerous` 不许写进静态表 —— 它由有效风险自动要求
        if PERM_DANGEROUS in _s.perms:
            _bad.append(f"{_n}: 不许把总闸写进静态 perms（它跟着 effective_risk 走）")
    if _bad:
        raise RuntimeError("[OS-DSL] 能力归属表不自洽：" + "; ".join(_bad))


_assert_perm_table_sane()


def load_permissions(config_path: Optional[pathlib.Path] = None) -> Dict[str, bool]:
    """从 `os_state_path()` 读 permissions，缺失项用默认（全关）兜底。"""
    perms = dict(_DEFAULT_PERMISSIONS)
    if config_path is None:
        config_path = os_state_path()
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            cfg = data.get("permissions") or {}
            for k in perms:
                if isinstance(cfg.get(k), bool):
                    perms[k] = cfg[k]
    except Exception as e:
        logger.warning(f"[OS-DSL] 读取 os_state.json 权限失败，用默认(全关): {e}")
    return perms


# ══════════════════════════════════════════════════════════════════════════
# 3. 状态转移表（硬编码，模型不能自创转移路径）—— 实现约束 3
# ══════════════════════════════════════════════════════════════════════════
# 视觉定位返回的状态，下一步只能走这张表允许的路径。
# 关键：NOT_FOUND 不能直接 request_replan，必须先走完内部降级。
# 只读档不涉及视觉定位（无定位需求），此表为鼠标键盘档预置并固定契约。
# ══════════════════════════════════════════════════════════════════════════

LOCATE_STATES = ("SUCCESS", "NOT_FOUND", "AMBIGUOUS", "OCCLUDED")

_STATE_TRANSITIONS: Dict[str, List[str]] = {
    "SUCCESS":   ["proceed_next"],
    "NOT_FOUND": ["try_internal_downgrade", "then_request_replan"],   # 必须先内部降级
    "AMBIGUOUS": ["pick_highest_conf_if_allowed", "else_request_user_choice"],
    "OCCLUDED":  ["try_unocclude", "then_request_replan"],            # 先尝试解除遮挡
}


def allowed_transitions(state: str) -> List[str]:
    return list(_STATE_TRANSITIONS.get(state, []))


def is_valid_transition(state: str, step: str) -> bool:
    return step in _STATE_TRANSITIONS.get(state, [])


# ══════════════════════════════════════════════════════════════════════════
# 4. DSL 指令校验
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    ok: bool
    action: str = ""
    effective_risk: int = 0
    risk_reasons: Optional[List[str]] = None
    is_control_flow: bool = False
    error: str = ""


def validate_instruction(instr: Dict[str, Any],
                         foreground_window: str = "",
                         m1_mode: bool = False,
                         m2_mode: bool = True,
                         m3_mode: bool = False,
                         upgrade_rules: Optional[List[Dict[str, Any]]] = None,
                         readonly_only: bool = False) -> ValidationResult:
    """校验一条 JSON DSL 指令。

    Args:
        instr: {"action": str, "params": dict, "declared_risk": int, "reason": str}
        foreground_window: 当前前台窗口名（动态升级判定用）
        m1_mode: True 时只允许 stage<=1（只读），优先级最高（覆盖 m2/m3）
        m2_mode: True 时允许 stage<=2（+低危写操作，不含鼠标键盘）
        m3_mode: True 时允许 stage<=3（+鼠标键盘控制）

    ⭐⭐ `readonly_only` —— **与 m1_mode 是两个不同的问题，刻意不合并**：

        m1_mode        按 `stage` 过滤 —— 「这个能力放开到哪一档了」
        readonly_only  按 `readonly` 过滤 —— 「它改不改这台电脑」

    🔴 2026-08-16 实测两者的差集：`stage<=1` 里**没有**任何会写的 action（好），
       但 `readonly=True` 里有一个 `read_screen_region` 是 `stage=3`
       （只读，却因为要 VisionLocator 而排在 stage 3）。
       📌 **两个轴今天几乎重合，那是巧合不是设计** —— 合并它们，
          等于让「安全边界」跟着「开发进度」走，而后者随时会变。
    ⚠️ 也正因为问的问题不同，`is_readonly()` 至今零调用方**不是它写错了**，
       而是**至今没有出现需要它回答的问题**（`m1_mode` 问的是 stage）。
       第一个真实消费者是只读 Subagent。

    档位判定（清晰分层，不再依赖被污染的 m1_enabled 字段）：
        m1_mode=True            → max_stage=1（铁律：纯只读，click 等必被拒）
        否则 m3_mode=True       → max_stage=3
        否则 m2_mode=True        → max_stage=2
        否则                     → max_stage=1（最保守兜底）

    Returns:
        ValidationResult
    """
    if not isinstance(instr, dict):
        return ValidationResult(ok=False, error="instruction must be a dict")

    action = instr.get("action")
    if not action or not isinstance(action, str):
        return ValidationResult(ok=False, error="missing action field")

    if not is_known_action(action):
        return ValidationResult(ok=False, action=action,
                                error=f"unknown action {action!r}; actions must come from the closed enum and cannot be invented by the model")

    if action in CONTROL_FLOW_ACTIONS:
        return ValidationResult(ok=True, action=action, effective_risk=1,
                                risk_reasons=[], is_control_flow=True)

    # ── 档位门禁（基于 stage 分层，m1_mode 始终最严）────────────────────────
    if m1_mode:
        max_stage = 1
    elif m3_mode:
        max_stage = 3
    elif m2_mode:
        max_stage = 2
    else:
        max_stage = 1

    if not is_stage_allowed(action, max_stage):
        st = action_stage(action)
        stage_name = {1: "readonly", 2: "write operation", 3: "mouse/keyboard control"}.get(st, f"stage{st}")
        if m1_mode:
            err = f"action {action!r} is disabled in readonly mode, which only allows readonly actions; this action belongs to {stage_name}"
        else:
            err = f"action {action!r} requires {stage_name}; current max stage={max_stage}"
        return ValidationResult(ok=False, action=action, error=err)

    # ⭐⭐ 只读闸 —— **与上面的 stage 闸正交**（见函数头那段）。
    #    ⚠️ 放在 stage 之后：两个都不满足时，先报**更具体**的那一个
    #       （「这个能力还没开放」比「你只能只读」更接近根因）。
    # 🔴 判据取自 `_ACTIONS` 这张**权威表**，不是一份手抄名单 ——
    #    📌 记着 `_OS_ACTIONS` 手抄 29 个而实际 39 个、专挑高频项漏。
    if readonly_only and not is_readonly(action):
        return ValidationResult(
            ok=False, action=action,
            error=(f"action {action!r} changes system state; this executor is "
                   f"restricted to read-only actions "
                   f"(allowed: {', '.join(sorted(readonly_actions()))})"))

    params = instr.get("params") or {}
    if not isinstance(params, dict):
        return ValidationResult(ok=False, action=action, error="params must be a dict")

    # OS-1: 路径展开先于风险规则判断，防止 %USERPROFILE%\Startup 绕过 Startup 规则
    import os as _os
    _path_keys = ["path", "dest", "target", "src"]
    params_normalized = dict(params)
    for _k in _path_keys:
        if _k in params_normalized and isinstance(params_normalized[_k], str):
            params_normalized[_k] = _os.path.expandvars(
                _os.path.expanduser(params_normalized[_k])
            )

    declared = instr.get("declared_risk", 1)
    try:
        declared = int(declared)
    except Exception:
        declared = 1

    risk, reasons = compute_effective_risk(
        action, params_normalized, declared, foreground_window, upgrade_rules
    )
    return ValidationResult(ok=True, action=action, effective_risk=risk,
                            risk_reasons=reasons, is_control_flow=False)
