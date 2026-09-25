# core/os_layer/dispatch.py
"""
OS 执行层调度入口 —— 视觉定位 + 鼠标键盘 + 定位前置 + 急停生命周期

一条 DSL 指令进来，走：
  1. dsl.validate_instruction  —— 校验 + 算有效风险 + 档位门禁
  2. 控制流信号直接返回上层
  3. 定位前置：click/type_text 等带语义 target 的动作，先 VisionLocator
     定位拿坐标 + 生成标注截图，再弹确认窗（让用户看到"要点哪"）。定位失败直接回状态，不弹窗。
  4. 授权检查：risk=1 直接放行；risk=2 检查预授权或挂起等确认；risk=3 永远挂起等确认
     ——通过 yield {"event":"os_action_confirm"} 挂起，上层把弹窗消费掉
  5. 路由到对应执行器执行（执行期间挂急停监听 start/stop_listening）
  6. audit.record 写审计日志

实现约束 2：只报状态不决策。"要不要 replan" 由上层 _handle_os_task 决定。
实现约束 1：本层是"第1层 Executor 单动作执行"，只抛执行结果，不决定重试/replan。
"""
from __future__ import annotations
import asyncio
from typing import Any, AsyncGenerator, Dict, Optional
from loguru import logger

from core.os_layer import dsl
from core.os_layer.executor_low import LowLevelExecutor
from core.os_layer.executor_write import WriteExecutor
from core.os_layer.executor_action import ActionExecutor, EmergencyStop
from core.os_layer.executor_vision import VisionLocator
from core.os_layer.safety import OSSessionSafety
from core.os_layer.audit import get_audit_logger


# 需要"先定位再操作"的动作（定位前置到确认弹窗之前）。
# 这些动作可接受语义 target；若 instr 已带 x/y 坐标则跳过定位（直接用坐标）。
_LOCATE_ACTIONS = {"click", "double_click", "right_click"}


# ⭐⭐⭐ [OS-ENUM 2026-08-24] 路由表提到**模块级**，并且改成「动作 → (执行器属性, 方法名)」。
#
# 🔴 为什么动它：`os_execute` / `computer_use` 的 action enum 原来是**手抄**的
#    （`_OS_ENUM_FROZEN_36`），而手抄件已经过期 —— 漏了 `move`（有执行器、能跑）
#    和 `request_user_choice`。📌 **一份手抄的清单，它的过期是静默的。**
#
# ⭐ 要让 enum 能**自动派生**，就需要一份「哪些动作真的有执行器」的清单，
#    而它**必须在模块级**（orchestrator 建 schema 时没有 dispatcher 实例）。
# ⚠️ 刻意**不**在 `dsl.ActionDef` 上加一个 `implemented=True/False` 字段 ——
#    那会造出**第二份**要人工同步的清单，正是我们在修的那个问题。
#    📌 **单一出处必须是「事实本身」，不是「对事实的声明」** ——
#       这里的事实就是路由表：挂了执行器就是挂了。
_ROUTE_SPEC: Dict[str, tuple] = {
    # ── 只读 ──
    "screenshot":        ("_low",    "screenshot"),
    "get_sysinfo":       ("_low",    "get_sysinfo"),
    "read_registry":     ("_low",    "read_registry"),
    "read_window_tree":  ("_low",    "read_window_tree"),
    "list_windows":      ("_low",    "list_windows"),
    "get_cursor_pos":    ("_low",    "get_cursor_pos"),
    "wait":              ("_low",    "wait"),
    "list_dir":          ("_low",    "list_dir"),
    # ── 写操作（无鼠标键盘）──
    "win_minimize":      ("_write",  "win_minimize"),
    "win_close":         ("_write",  "win_close"),
    "win_switch":        ("_write",  "win_switch"),
    "set_volume":        ("_write",  "set_volume"),
    "launch_app":        ("_write",  "launch_app"),
    "kill_app":          ("_write",  "kill_app"),
    "file_read":         ("_write",  "file_read"),
    "file_write":        ("_write",  "file_write"),
    "clipboard_read":    ("_write",  "clipboard_read"),
    "clipboard_write":   ("_write",  "clipboard_write"),
    "open_url":          ("_write",  "open_url"),
    # ── 高危写操作（补丁A，地板=3）──
    "run_command":       ("_write",  "run_command"),
    "write_registry":    ("_write",  "write_registry"),
    "file_delete":       ("_write",  "file_delete"),
    "file_move":         ("_write",  "file_move"),
    "manage_service":    ("_write",  "manage_service"),
    "set_env_var":       ("_write",  "set_env_var"),
    "schedule_task":     ("_write",  "schedule_task"),
    "modify_startup":    ("_write",  "modify_startup"),
    "network_config":    ("_write",  "network_config"),
    # ── 鼠标键盘 ──
    "click":             ("_action", "click"),
    "double_click":      ("_action", "double_click"),
    "right_click":       ("_action", "right_click"),
    "move":              ("_action", "move"),
    "drag":              ("_action", "drag"),
    "type_text":         ("_action", "type_text"),
    "hotkey":            ("_action", "hotkey"),
    "scroll":            ("_action", "scroll"),
}

# ⭐ 「真的挂了执行器」的权威答案。`core.tools.manifests._os_actions_for` 派生 enum 时读它。
# ⚠️ `read_screen_region` **不在这里**，而且那是**对的** —— 它没有执行器
#    （只有鼠标键盘档才接 VisionLocator，低档位在校验层就该拒掉它）。
#    📌 派生规则不能是「把 `_ACTIONS` 全抄一遍」，那会把一个**没实现的动作**
#       写进 schema，模型调了才发现 —— 而那种失败最难查：schema 说有，运行说没有。
ROUTED_ACTIONS = frozenset(_ROUTE_SPEC)


def _derive_auth_scope(action: str, params: dict) -> str:
    """从 action 和 params 派生预授权范围键，防止范围过粗授权。

    - 文件类操作: 父目录路径
    - open_url: 域名
    - 其他: "*"（会话级通用授权）
    """
    import os as _os
    import re as _re
    _file_actions = {"file_write", "file_read", "file_delete", "file_move", "file_list"}
    if action in _file_actions:
        raw = str(params.get("path") or params.get("src") or params.get("dest") or "")
        if raw:
            expanded = _os.path.expandvars(_os.path.expanduser(raw))
            return _os.path.dirname(_os.path.abspath(expanded)).lower()
    if action == "open_url":
        url = str(params.get("url") or "")
        m = _re.match(r"https?://([^/]+)", url)
        if m:
            return m.group(1).lower()
    return "*"


class OSDispatcher:
    def __init__(self,
                 session_id: str = "",
                 m1_mode: bool = False,
                 # ⭐ 2026-08-16：只读执行者（第一个是 Subagent）。
                 #    ⚠️ **不复用 m1_mode** —— 那个按 `stage` 过滤（能力档位），
                 #       这个按 `readonly` 过滤（改不改这台电脑）。
                 #       📌 让「安全边界」跟着「开发进度」走，是把两件会各自
                 #          变化的事绑在一起。
                 readonly_only: bool = False,
                 m2_mode: bool = True,
                 m3_mode: bool = True,
                 safety: Optional[OSSessionSafety] = None,
                 provider=None,
                 estop: Optional[EmergencyStop] = None,
                 vision_model_override: Optional[str] = None):
        self._audit = get_audit_logger()
        self._low = LowLevelExecutor(audit_logger=self._audit,
                                     screenshot_dir=self._audit.screenshot_dir)
        self._write = WriteExecutor()
        self._session_id = session_id
        self._m1_mode = m1_mode
        self._readonly_only = readonly_only
        self._m2_mode = m2_mode
        self._m3_mode = m3_mode
        self._safety = safety or OSSessionSafety()
        self._upgrade_rules = dsl.load_upgrade_rules()
        self._permissions = dsl.load_permissions()

        # 视觉定位器 + 操作执行器 + 急停
        self._estop = estop or EmergencyStop()
        self._vision = VisionLocator(provider=provider,
                                     screenshot_dir=self._audit.screenshot_dir,
                                     model_override=vision_model_override)
        self._action = ActionExecutor(vision_locator=self._vision, estop=self._estop)

        # action → 执行器方法。⭐ 从模块级 `_ROUTE_SPEC` 生成 ——
        # 📌 这份表现在有**两个**读者（这里执行、orchestrator 派生 enum），
        #    所以它必须是**一份**，而且必须在模块级拿得到。
        self._route: Dict[str, Any] = {
            _a: getattr(getattr(self, _ex), _m) for _a, (_ex, _m) in _ROUTE_SPEC.items()
        }

    @property
    def estop(self) -> EmergencyStop:
        return self._estop

    async def execute(self, instr: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        """执行一条 DSL 指令，异步生成器。

        yields:
          {"type": "confirm_request", ...}  —— 需要用户确认时（由 _handle_os_task 消费弹窗）
          {"type": "result", ...}           —— 最终执行结果

        调用方用法：
            async for event in dispatcher.execute(instr):
                if event["type"] == "confirm_request":
                    yield {"event": "os_action_confirm", ...}  # 交给 app.py 弹窗
                    # 等待确认（见 _handle_os_task）
                elif event["type"] == "result":
                    result = event
        """
        fg = self._low.get_foreground_window_title()
        vr = dsl.validate_instruction(
            instr,
            foreground_window=fg,
            m1_mode=self._m1_mode,
            m2_mode=self._m2_mode,
            m3_mode=self._m3_mode,
            upgrade_rules=self._upgrade_rules,
            readonly_only=self._readonly_only,
        )

        if not vr.ok:
            self._audit.record(
                action=vr.action or str(instr.get("action", "?")),
                params=instr.get("params", {}) or {},
                effective_risk=0,
                result_status="blocked",
                session_id=self._session_id,
                risk_reasons=vr.risk_reasons or [],
                error=vr.error,
            )
            yield {"type": "result", "ok": False, "action": vr.action,
                   "effective_risk": 0, "risk_reasons": [],
                   "is_control_flow": False, "data": {},
                   "summary": "", "error": vr.error}
            return

        if vr.is_control_flow:
            self._audit.record(
                action=vr.action, params={}, effective_risk=1,
                result_status="signal", session_id=self._session_id,
                result_summary=f"Control-flow signal: {vr.action}"
            )
            yield {"type": "result", "ok": True, "action": vr.action,
                   "effective_risk": 1, "risk_reasons": [],
                   "is_control_flow": True,
                   "data": instr.get("params", {}) or {},
                   "summary": f"Signal: {vr.action}", "error": ""}
            return

        # 注：这里【曾经】有一道"软急停检查"（safety.is_aborted() → 拒绝一切动作）。
        # 软急停已整套删除，见 core/os_layer/safety.py 模块头。真正的急停是
        # EmergencyStop（Ctrl+` 热键 + 甩角 failsafe），只在鼠标键盘动作执行期间挂监听，
        # 由执行器返回 aborted=True，在下面的执行结果里处理，不需要前置闸。

        # ⚠️ 这里原来有一个 `_is_mk = M3_ALLOWED - M2_ALLOWED`，它唯一的消费者
        #    是下面那道「鼠标键盘权限」检查。2026-08-20 那道闸被**派生自
        #    `dsl` 权威表**的通用能力检查取代之后，它就没有调用方了 ——
        #    📌 一个只剩定义没有读取的变量，是下一个人误以为"这里有个分类"的入口。
        #    （那条"三档是能力分期，不是争不争鼠标"的教训留在下方注释里。）

        # ── 被动挂起的第二道闸：机器还在不在 Nano 手里 ──────────────────────
        #
        # 第一道闸在 `os_execute` 开工前（拿不到租约就不开始）。这一道管的是
        # **多步计划跑到一半用户接管** —— 那时任务已经在跑，只能一步一步碰壁。
        #
        # ⭐ 只拦**会和用户争这台电脑**的动作（`dsl.contends_for_machine`）。
        # ⚠️⚠️ 这里原先写着「不拦截图/读窗口 —— 用户要的是别动鼠标，不是不许看屏幕，
        #    而且恢复后判断环境变没变恰恰需要能看」。**那条论据已失效，删掉。**
        #    · 它是**闸时代**的产物：出口是「失败」时，拦截图 = 永久拿不到那张图；
        #      出口改成「等待」后，拦 = **稍后再做**，那个担心自动消失。
        #      📌 **机制的出口从「失败」变成「等待」时，一大批「不能拦 X」的论据
        #         会自动失效** —— 论据却被留了下来，没跟前提一起改。
        #    · 更直接的一点：**两个时刻压根不可能重合** ——
        #      「为判断环境而截图」发生在挂起**结束之后**，
        #      而拦截发生在用户**正在动**的时候。
        #    ⭐ 现在的真实理由是另一条：`_look_at_screen` **会最小化/还原自己的窗口**
        #      来避开遮挡 —— 那是**抢焦点**，它确实在跟用户争，所以它也该等。
        # ⚠️ 这里**曾经用 `_is_mk`（＝ stage3 − stage2）当代理**，两个方向都错：
        #    `read_screen_region` 只读却被拦，`win_switch/minimize/close` 抢焦点却放过。
        #    📌 那三档是**能力分期**，不是"争不争鼠标" ——
        #       借一个为别的目的定义的分类去回答另一个问题，边界一定不对。
        #
        # ⚠️ 这道闸**只在有别的持有者时才拒绝**：没人持有 → 放行（退化成今天的行为）。
        #    所以"租约系统整个挂了"不会把 OS 能力锁死，最坏是退回没有被动挂起。
        #    真正会锁死的只有"读不出状态"（fail-safe = 不能），那条已经在
        #    `nano_may_touch_os()` 里打了 WARNING，不会一声不响。
        if dsl.contends_for_machine(vr.action):
            try:
                from core.runtime import oslease as _ol
                from core.runtime.kernel import get_kernel as _gk
                _may, _why = _ol.nano_may_touch_os(_gk())
            except Exception as _e:
                logger.debug(f"[OS-Dispatch] 读租约失败，放行本次动作: {_e}")
                _may, _why = True, ""
            if not _may:
                logger.info(f"[OS-Dispatch] 让位，不执行 {vr.action}: {_why}")
                self._audit.record(
                    action=vr.action, params=instr.get("params", {}),
                    effective_risk=vr.effective_risk, result_status="blocked",
                    session_id=self._session_id, risk_reasons=vr.risk_reasons or [],
                    error=f"os_lease_unavailable: {_why}",
                )
                yield {"type": "result", "ok": False, "action": vr.action,
                       "effective_risk": vr.effective_risk,
                       "risk_reasons": vr.risk_reasons or [],
                       "is_control_flow": False, "data": {"lease_reason": _why},
                       "summary": "",
                       # ⚠️ 措辞刻意区别于 permission_denied：那是"你没被授权"，
                       #    这是"机器暂时不归你"。让模型当成权限问题去排查就错了。
                       "error": f"machine_not_available: {_why}. "
                                f"Someone else is using this computer right now. "
                                f"Stop and do not retry — retrying means fighting them "
                                f"for the mouse."}
                return

        # ══ 能力开关检查（设置 → OS 权限 那 6 个）════════════════════════
        #
        # 🔴🔴 **2026-08-20：这里以前只查 `allow_mouse_keyboard` 一个。**
        #    另外 5 个（工作区写入 / 窗口控制 / 系统设置 / 注册表写入 /
        #    **高危操作总闸**）在全项目里**零读取点**（AST 层面核过）——
        #    用户把「高危操作总闸」关掉，Nano 照样能 `run_command`、`file_delete`；
        #    开关照样存进 os_config.json、UI 上照样变灰，**看起来完全生效了**。
        #    📌 **一个失效的开关比没有这个开关更危险** —— 用户会据此放松警惕。
        #       而它坏的方向是**放行**。
        #    ⚠️ 唯一的缓和是 floor 的确认弹窗还在；但 **auto 一开那道也没了**，
        #       此时「高危总闸=关」是完全无效的。
        #
        # ⭐⭐ **位置是这一层的关键**：它在确认弹窗**之前**。
        #    2026-08-20 定的语义：
        #        能力开关答「**这项能力开不开放**」
        #        auto     答「**开放了的，要不要逐个授权**」
        #    ⇒ 能力没开放时，连"要不要授权"这个问题都不该被问到 —— **auto 救不了**。
        #    📌 一个上游的闸如果被放到下游，下游的豁免就会顺手把它一起豁免掉。
        #
        # ⚠️ 名单**从 `dsl` 的权威表派生**，这里一个字都不手抄
        #    （`_OS_ACTIONS` 手抄 29 个而实际 39 个、专挑高频项漏那次的教训）。
        _missing = dsl.missing_permissions(
            vr.action, self._permissions, vr.effective_risk)
        if _missing:
            _labels = "、".join(f"「{dsl.PERMISSION_LABELS.get(k, k)}」" for k in _missing)
            # ⭐ 给模型的话必须**说清是哪一道、以及谁能打开它** ——
            #    📌 失败信息要同时【正确】且【充分】。
            #       旧文案是 `permission_denied: allow_mouse_keyboard`，
            #       一个机器串，模型只能猜"我是不是不该做这件事"。
            _msg = (f"capability_disabled: {','.join(_missing)}. "
                    f"这台电脑上的{_labels}能力被用户关掉了，所以 `{vr.action}` "
                    f"现在不可用。这**不是**授权被拒，也不是你做错了什么 —— "
                    f"是这项能力没有开放。"
                    f"要用它，需要**用户自己**去【设置 → OS 权限】里打开{_labels}；"
                    f"你打不开它，也不要重试这个动作。"
                    f"请如实把这件事告诉用户，并说明打开的位置。")
            self._audit.record(
                action=vr.action, params=instr.get("params", {}),
                effective_risk=vr.effective_risk, result_status="blocked",
                session_id=self._session_id, risk_reasons=vr.risk_reasons or [],
                error=f"capability_disabled: {','.join(_missing)}",
            )
            yield {"type": "result", "ok": False, "action": vr.action,
                   "effective_risk": vr.effective_risk,
                   "risk_reasons": vr.risk_reasons or [],
                   "is_control_flow": False, "data": {},
                   # ⭐ 结构化那一份也给出去：UI/测试不该去解析中文句子
                   "missing_permissions": list(_missing),
                   "authorized_by": "capability_disabled",
                   "summary": "", "error": _msg}
            return

        # ── 定位前置 ───────────────────────────────────────────────────────
        # click 类带语义 target 时，在弹确认窗之前先定位，让用户看到"要点哪"。
        # 定位失败 → 直接回状态（不弹窗，弹了也没意义）；成功 → 改写 instr 为确定坐标 + 标注图。
        annotated_image_path = ""
        if (vr.action in _LOCATE_ACTIONS
                and (instr.get("params") or {}).get("target")
                and (instr.get("params") or {}).get("x") is None):
            target = instr["params"]["target"]
            loc = await self._vision.locate(target)
            if loc["status"] != "SUCCESS":
                # 定位失败：如实回状态，交上层 _handle_os_task 决定（不做 replan，诚实告知）
                self._audit.record(
                    action=vr.action, params=instr.get("params", {}),
                    effective_risk=vr.effective_risk, result_status="failed",
                    session_id=self._session_id, risk_reasons=vr.risk_reasons,
                    locate_status=loc["status"],
                    error=f"Target location for {target!r} returned status: {loc['status']}",
                )
                # ⭐ [2026-08-24] `summary` 原来是**空串** —— 模型只拿到一个
                #    `AMBIGUOUS` 状态名，于是实际运行中它把「有 2 个候选」
                #    读成了「找到了 2 个你要的东西」，而那两个候选的 label
                #    其实是 `Code` 和 `窗口`。
                #    📌 **一个只报状态、不报内容的失败结果，会被当成一个成功结果的变体。**
                #    ⇒ 把 `locate` 那条 summary（哪一级给的 + 候选各自叫什么）抬上来。
                yield {"type": "result", "ok": False, "action": vr.action,
                       "effective_risk": vr.effective_risk,
                       "risk_reasons": vr.risk_reasons or [],
                       "is_control_flow": False,
                       "data": {"locate_status": loc["status"], "locate": loc},
                       "summary": loc.get("summary", ""),
                       "error": f"Target location for {target!r} returned status: {loc['status']}",
                       "locate_status": loc["status"]}
                return
            # 定位成功：把坐标写进 instr（执行时不再重新定位），生成标注图
            cand = loc["candidates"][0]
            instr = dict(instr)
            instr["params"] = dict(instr["params"])
            instr["params"]["x"] = cand["x"]
            instr["params"]["y"] = cand["y"]
            instr["params"]["_located_label"] = cand.get("label", target)
            annotated_image_path = self._annotate_screenshot(
                loc.get("screenshot_ref", ""), cand["x"], cand["y"],
                cand.get("label", target),
            )

        # ── 授权检查 ──────────────────────────────────────────────────────
        _scope_key = _derive_auth_scope(vr.action, instr.get("params") or {})
        if not self._safety.is_pre_authorized(vr.action, vr.effective_risk, _scope_key):
            # 需要弹窗确认，yield confirm_request，由上层挂起等结果
            yield {
                "type": "confirm_request",
                "action": vr.action,
                "effective_risk": vr.effective_risk,
                "risk_reasons": vr.risk_reasons or [],
                "reason": instr.get("reason", ""),
                "params_summary": self._params_summary(vr.action, instr.get("params") or {}),
                "annotated_image_path": annotated_image_path,
                "_resolved_instr": instr,   # 已写入坐标的 instr，供 execute_after_confirm 使用
            }
            # 上层消费完 confirm_request 之后，必须通过 execute_after_confirm() 继续
            return

        # 已预授权（risk=1 或用户选"始终允许"）——直接执行
        # ⭐ **这次执行是怎么获得许可的，要说出来。**
        #    📌 改造前只有「被拒」这一侧对模型可见；成功时它不知道自己刚被批准过。
        #       而对Subagent尤其要紧：它该能在报告里说「这几处**用户逐个批准了**」，
        #       而不是含糊地说「我改了几处」。
        #    ⚠️ 这里两种情况**分得开**：floor==1 是「本来就不需要授权」，
        #       其余是「以前批过、这次沿用」—— 📌 一个字段不许表达两个现实。
        _how = ("not_required" if int(vr.effective_risk or 0) <= 1
                else "pre_authorized")
        async for ev in self._execute_action(vr, instr):
            if isinstance(ev, dict) and ev.get("type") == "result":
                ev.setdefault("authorized_by", _how)
            yield ev

    async def execute_after_confirm(self, instr: Dict[str, Any],
                                    confirmed: bool) -> Dict[str, Any]:
        """用户确认/取消后调用。返回最终结果 dict（不是生成器）。

        instr 应为 confirm_request 里回传的 _resolved_instr（定位前置时已写入坐标），
        这样 click 执行时直接用已确认的坐标，不再重新定位。
        """
        if not confirmed:
            vr = dsl.validate_instruction(
                instr, m1_mode=self._m1_mode, m2_mode=self._m2_mode,
                m3_mode=self._m3_mode, upgrade_rules=self._upgrade_rules,
            )
            self._audit.record(
                action=vr.action or str(instr.get("action", "?")),
                params=instr.get("params", {}),
                effective_risk=vr.effective_risk,
                result_status="cancelled",
                session_id=self._session_id,
            )
            return {"ok": False, "action": vr.action, "effective_risk": vr.effective_risk,
                    "risk_reasons": vr.risk_reasons or [], "is_control_flow": False,
                    "data": {}, "summary": "", "error": "User cancelled the action"}

        fg = self._low.get_foreground_window_title()
        vr = dsl.validate_instruction(
            instr, foreground_window=fg,
            m1_mode=self._m1_mode, m2_mode=self._m2_mode,
            m3_mode=self._m3_mode, upgrade_rules=self._upgrade_rules,
        )
        # 确认后重新校验：环境可能在确认前后发生变化，失败必须阻断
        if not vr.ok:
            self._audit.record(
                action=vr.action or str(instr.get("action", "?")),
                params=instr.get("params", {}),
                effective_risk=vr.effective_risk,
                result_status="validation_failed_after_confirm",
                session_id=self._session_id,
            )
            return {"ok": False, "action": vr.action, "effective_risk": vr.effective_risk,
                    "risk_reasons": vr.risk_reasons or [], "is_control_flow": False,
                    "data": {}, "summary": "", "error": "Validation failed after confirmation; execution was cancelled"}
        results = []
        async for ev in self._execute_action(vr, instr):
            results.append(ev)
        return results[-1] if results else {"ok": False, "error": "no result"}

    async def _execute_action(self, vr: dsl.ValidationResult,
                               instr: Dict[str, Any]):
        """实际执行动作，yield 单个 {"type":"result", ...}。

        实现约束1：本层是第1层（单动作执行），只抛执行结果，不做重试/replan。
        鼠标键盘动作执行期间挂 Ctrl+` 急停监听（start→执行→finally stop）。
        """
        fn = self._route.get(vr.action)
        if fn is None:
            err = f"action {vr.action!r} is valid but no executor is mounted for the current stage"
            self._audit.record(
                action=vr.action, params=instr.get("params", {}),
                effective_risk=vr.effective_risk, result_status="blocked",
                session_id=self._session_id, error=err,
            )
            yield {"type": "result", "ok": False, "action": vr.action,
                   "effective_risk": vr.effective_risk,
                   "risk_reasons": vr.risk_reasons or [],
                   "is_control_flow": False, "data": {}, "summary": "", "error": err}
            return

        params = instr.get("params", {}) or {}
        # 仅鼠标键盘动作挂急停监听（只读/写操作无需，挂了反干扰别的程序热键）
        _needs_estop = vr.action in dsl.M3_ALLOWED_ACTIONS and vr.action not in dsl.M2_ALLOWED_ACTIONS
        try:
            if _needs_estop:
                self._estop.start_listening()
            # ⭐⭐ [ActionAttempt] **这里就是 commit boundary。**
            #
            # 上面所有的校验、定位、授权确认都还没碰过现实 —— 一律算 `PREPARED`。
            # 从下一行 `await fn(params)` 开始，副作用可能真的发生，而且**没有 undo**。
            # 所以在这一行之前把状态推到 `IN_FLIGHT`：之后一旦被打断，
            # 内核会自动判成 `PARTIAL_OR_UNKNOWN`（而不是让人误以为"没发生"）。
            #
            # 📌 那条判据：**「明确 commit boundary，
            #    而不是幻想 GUI 具备数据库 rollback。」** 这一行就是那个 boundary。
            # ⚠️ 刻意**不传 attempt_id** —— 见 `mark_in_flight_current` 的说明：
            #    把 id 一层层穿下来意味着每个中间层都得记得传，那正是本轮反复栽的形状。
            try:
                from core.runtime import attempt as _att
                _att.mark_in_flight_current()
            except Exception:
                pass
            try:
                result = await fn(params)
            except Exception as e:
                logger.error(f"[OS-Dispatch] action '{vr.action}' 执行异常: {e}")
                result = {"ok": False, "data": {}, "summary": "", "error": str(e)}
        finally:
            if _needs_estop:
                self._estop.stop_listening()

        self._audit.record(
            action=vr.action, params=params,
            effective_risk=vr.effective_risk,
            result_status="aborted" if result.get("aborted") else
                          ("success" if result.get("ok") else "failed"),
            session_id=self._session_id,
            risk_reasons=vr.risk_reasons,
            locate_status=(instr.get("params", {}) or {}).get("_located_label", "") and "located",
            result_summary=result.get("summary", ""),
            aborted=bool(result.get("aborted")),
            error=result.get("error", ""),
        )

        yield {
            "type": "result",
            "ok": result.get("ok", False),
            "action": vr.action,
            "effective_risk": vr.effective_risk,
            "risk_reasons": vr.risk_reasons or [],
            "is_control_flow": False,
            "data": result.get("data", {}),
            "summary": result.get("summary", ""),
            "aborted": bool(result.get("aborted")),
            "error": result.get("error", ""),
        }

    def _annotate_screenshot(self, shot_ref: str, x: int, y: int, label: str) -> str:
        """在定位截图上画标记（红圈+十字+标签），让用户确认时看到"要点哪"。

        返回标注后图片的路径；失败返回原图路径或空串（不阻断主流程）。
        """
        if not shot_ref:
            return ""
        try:
            import pathlib
            from PIL import Image, ImageDraw, ImageFont
            src = pathlib.Path(shot_ref)
            if not src.exists():
                return ""
            img = Image.open(str(src)).convert("RGB")
            draw = ImageDraw.Draw(img)
            r = 28  # 圈半径
            # 红色空心圆
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(239, 68, 68), width=4)
            # 十字准星
            draw.line([x - r - 8, y, x + r + 8, y], fill=(239, 68, 68), width=2)
            draw.line([x, y - r - 8, x, y + r + 8], fill=(239, 68, 68), width=2)
            # 标签底框 + 文字
            txt = f"将点击: {label}"[:30]
            try:
                font = ImageFont.truetype("C:\\Windows\\Fonts\\msyh.ttc", 22)
            except Exception:
                font = ImageFont.load_default()
            tx, ty = x + r + 12, y - r
            try:
                bbox = draw.textbbox((tx, ty), txt, font=font)
                draw.rectangle([bbox[0] - 6, bbox[1] - 4, bbox[2] + 6, bbox[3] + 4],
                               fill=(15, 17, 24))
            except Exception:
                pass
            draw.text((tx, ty), txt, fill=(252, 165, 165), font=font)

            out = src.parent / f"annotated_{src.name}"
            img.save(str(out))
            return str(out)
        except Exception as e:
            logger.warning(f"[OS-Dispatch] 截图标注失败（不阻断）: {e}")
            return shot_ref  # 退而求其次用原图

    @staticmethod
    def _params_summary(action: str, params: Dict[str, Any]) -> str:
        """生成弹窗里展示的参数摘要（不含敏感字段）。"""
        _SHOW = {
            "win_minimize": ["title"], "win_close": ["title"], "win_switch": ["title"],
            "set_volume": ["level"], "launch_app": ["target"], "kill_app": ["name", "pid"],
            "file_write": ["path"], "file_read": ["path"],
            "open_url": ["url"], "clipboard_write": [],   # clipboard_write 不展示 text
            "click": ["target", "_located_label"], "double_click": ["target", "_located_label"],
            "right_click": ["target", "_located_label"], "type_text": [],  # type_text 不展示内容
        }
        keys = _SHOW.get(action, list(params.keys())[:3])
        # _located_label 优先展示为友好文案
        if "_located_label" in keys and params.get("_located_label"):
            return f"目标: {params['_located_label']}"
        parts = [f"{k}={params[k]}" for k in keys if k in params and not k.startswith("_")]
        return ", ".join(parts) if parts else ""
