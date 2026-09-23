# core/os_layer/executor_action.py
"""
操作执行器 ActionExecutor —— 鼠标键盘控制 + 急停双保险

执行真正的鼠标键盘动作：click / double_click / right_click / drag / move /
type_text / hotkey / scroll。

决策1（A方案）：click 类接受语义 target，内部调 VisionLocator 定位后再点击。
              模型永远不传像素坐标。

决策3（双保险急停）：
  主：keyboard 库全局监听 ESC（每个动作前检查中断标志）
  兜底：pyautogui failsafe（鼠标甩到屏幕左上角 (0,0) 触发 FailSafeException）
  ——failsafe 不依赖键盘监听，全屏/管理员程序也拦不住鼠标，是最后防线。

急停只在 OS 动作执行期间挂监听（start_listening/stop_listening），
普通对话不挂，避免 ESC 与其他程序冲突。
"""
from __future__ import annotations
import asyncio
import ctypes
import threading
from typing import Any, Dict, Optional
from loguru import logger


class EmergencyStop:
    """急停双保险管理器。"""

    def __init__(self):
        self._stopped = threading.Event()
        self._listener_active = False
        self._kb_hook = None

    def start_listening(self):
        """开始监听急停热键 Ctrl+`（OS 动作序列开始时调用）。

        决策3：用 Ctrl+` 组合键（不是 ESC——ESC 很多程序自己占用，易误触；
        Ctrl+` 几乎无主流软件占用，一只手可按）。失败时 failsafe 仍兜底。
        """
        self._stopped.clear()
        self._listener_active = True
        try:
            import keyboard
            # add_hotkey 监听组合键（on_press_key 只能单键）
            self._kb_hook = keyboard.add_hotkey("ctrl+`", lambda: self.trigger("Ctrl+` hotkey"))
            logger.info("[OS-Estop] Ctrl+` 全局急停监听已启动")
        except ImportError:
            logger.warning("[OS-Estop] keyboard 库未安装，Ctrl+` 监听不可用（failsafe 仍生效）")
        except Exception as e:
            logger.warning(f"[OS-Estop] Ctrl+` 监听启动失败: {e}（failsafe 仍生效）")

    def stop_listening(self):
        """停止监听（OS 动作序列结束时调用）。"""
        self._listener_active = False
        if self._kb_hook is not None:
            try:
                import keyboard
                keyboard.remove_hotkey(self._kb_hook)
            except Exception:
                pass
            self._kb_hook = None
        logger.info("[OS-Estop] Ctrl+` 急停监听已停止")

    def trigger(self, source: str = ""):
        self._stopped.set()
        logger.warning(f"[OS-Estop] 急停触发（来源: {source}）")

    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def reset(self):
        self._stopped.clear()


class _SendInputClick:
    """Windows SendInput 原子点击：move+down+up 一次性提交进同一个INPUT数组。

    决策（验收后改进）：pyautogui.click(x,y) 内部是"先平滑移动到目标点（有
    duration，非瞬移），再按下"——这个移动窗口期如果用户自己也在手动移动鼠标，
    两路输入会冲突，最终点击位置不可预测（已实测复现过一次离谱偏移）。
    SendInput 把整个动作序列一次性提交给系统，Windows 保证同一次 SendInput
    调用内的事件序列不会被其他键鼠输入或别的 SendInput 调用插队，从根上避免
    这个并发问题，比"检测用户是否在动鼠标再决定要不要中止"更彻底。
    """
    _INPUT_MOUSE = 0
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010

    @classmethod
    def _build_input_type(cls):
        # ctypes Structure 不支持类体内自引用，运行时动态拼装一次并缓存
        if hasattr(cls, "_INPUT_READY"):
            return
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]
        cls.MOUSEINPUT = MOUSEINPUT
        cls.INPUT = INPUT
        cls._INPUT_READY = True

    @classmethod
    def _to_absolute(cls, x: int, y: int):
        """物理像素坐标 → SendInput 要求的 0~65535 虚拟桌面归一化坐标。"""
        user32 = ctypes.windll.user32
        vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        nx = int((x - vx) * 65535 / max(vw - 1, 1))
        ny = int((y - vy) * 65535 / max(vh - 1, 1))
        return nx, ny

    @classmethod
    def click(cls, x: int, y: int, button: str = "left", double: bool = False):
        """在 (x,y) 处一次性提交 move+down+up（可选再追加一组做双击）。"""
        cls._build_input_type()
        nx, ny = cls._to_absolute(x, y)
        flags_move = cls._MOUSEEVENTF_MOVE | cls._MOUSEEVENTF_ABSOLUTE | cls._MOUSEEVENTF_VIRTUALDESK
        down_flag = cls._MOUSEEVENTF_RIGHTDOWN if button == "right" else cls._MOUSEEVENTF_LEFTDOWN
        up_flag = cls._MOUSEEVENTF_RIGHTUP if button == "right" else cls._MOUSEEVENTF_LEFTUP

        def _mk(flags):
            mi = cls.MOUSEINPUT(nx, ny, 0, flags, 0, None)
            return cls.INPUT(cls._INPUT_MOUSE, mi)

        seq = [_mk(flags_move), _mk(down_flag), _mk(up_flag)]
        if double:
            seq += [_mk(down_flag), _mk(up_flag)]
        arr = (cls.INPUT * len(seq))(*seq)
        user32 = ctypes.windll.user32
        sent = user32.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(cls.INPUT))
        if sent != len(seq):
            raise OSError(f"SendInput 只提交了 {sent}/{len(seq)} 个事件（GetLastError={ctypes.get_last_error()}）")


class ActionExecutor:
    def __init__(self, vision_locator=None, estop: Optional[EmergencyStop] = None):
        import platform
        self._is_windows = platform.system() == "Windows"
        self._vision = vision_locator
        self._estop = estop or EmergencyStop()
        self._init_pyautogui()

    def _init_pyautogui(self):
        try:
            import pyautogui
            pyautogui.FAILSAFE = True   # 鼠标甩左上角 (0,0) 触发 failsafe
            pyautogui.PAUSE = 0.1       # 每个动作后短暂停顿，更像人操作
        except ImportError:
            logger.warning("[OS-Action] pyautogui 未安装")

    @property
    def estop(self) -> EmergencyStop:
        return self._estop

    # ── 操作前把目标窗口提到前台（关键）──────────────────────────────────
    # type_text/hotkey 直接发给"当前前台窗口"，click 也只发坐标——如果目标
    # 应用（微信/浏览器…）不在前台，快捷键/输入全落到别的窗口、焦点切不过去
    # （实测：微信 Ctrl 搜索按了没反应）。操作前强制把目标窗口拉到前台。
    # SetForegroundWindow 从后台进程会被 Windows 拦，用 AttachThreadInput 绕过。
    def _focus_target_window(self):
        if not self._is_windows:
            return
        try:
            from .executor_low import get_target_window
            w = get_target_window()
            hwnd = getattr(w, "_hWnd", None) if w is not None else None
            if not hwnd:
                return
            import ctypes
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            SW_RESTORE = 9
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, SW_RESTORE)
            fg = u.GetForegroundWindow()
            if fg == hwnd:
                return  # 已是前台，无需折腾
            cur_t = k.GetCurrentThreadId()
            fg_t = u.GetWindowThreadProcessId(fg, None)
            tgt_t = u.GetWindowThreadProcessId(hwnd, None)
            # 把本线程 attach 到前台线程+目标线程，临时获得设置前台的权限
            u.AttachThreadInput(cur_t, fg_t, True)
            u.AttachThreadInput(cur_t, tgt_t, True)
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                u.AttachThreadInput(cur_t, fg_t, False)
                u.AttachThreadInput(cur_t, tgt_t, False)
            import time as _t
            _t.sleep(0.18)  # 等焦点切换落地，再发输入
        except Exception as e:
            logger.warning(f"[OS-Action] 激活目标窗口失败（忽略，继续操作）: {e}")

    # ── 定位 + 点击类（决策1：A方案）────────────────────────────────────

    async def click(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._click_variant(params, "click")

    async def double_click(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._click_variant(params, "double")

    async def right_click(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._click_variant(params, "right")

    async def _click_variant(self, params: Dict[str, Any], kind: str) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()

        # 先把目标窗口提到前台：① 点击落到对的窗口上 ② 视觉定位的"候选点在前台
        # 窗口范围内"校验才不会把目标窗口的候选当成认错窗口而拒掉。
        await asyncio.to_thread(self._focus_target_window)

        # 支持两种输入：语义 target（A方案主路径）或直接 x/y（兜底）
        target = params.get("target")
        x, y = params.get("x"), params.get("y")
        locate_info = None

        if target and (x is None or y is None):
            if self._vision is None:
                return {"ok": False, "data": {}, "summary": "", "error": "no vision locator is available to resolve the semantic target"}
            loc = await self._vision.locate(target)
            locate_info = loc
            if loc["status"] != "SUCCESS":
                # 定位失败：把状态原样返回，让上层 _handle_os_task 决定 replan/user_choice
                return {
                    "ok": False, "data": {"locate_status": loc["status"], "locate": loc},
                    "summary": "", "error": f"Target location for {target!r} returned status: {loc['status']}",
                    "locate_status": loc["status"],
                }
            cand = loc["candidates"][0]
            x, y = cand["x"], cand["y"]

        if x is None or y is None:
            return {"ok": False, "data": {}, "summary": "", "error": "missing coordinates or resolvable target"}

        try:
            if self._estop.is_stopped():
                return self._aborted()
            if self._failsafe_triggered():
                self._estop.trigger("failsafe-corner")
                return self._aborted()
            button = "right" if kind == "right" else "left"
            _SendInputClick.click(x, y, button=button, double=(kind == "double"))
            label = (locate_info["candidates"][0]["label"]
                     if locate_info and locate_info.get("candidates") else f"({x},{y})")
            return {"ok": True, "data": {"x": x, "y": y, "locate": locate_info},
                    "summary": f"{self._kind_label(kind)} {label!r}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Action] {kind} 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    @staticmethod
    def _failsafe_triggered() -> bool:
        """SendInput 绕开了 pyautogui.click 自带的 failsafe 检查，手动补一份：
        鼠标在屏幕左上角 (0,0) 视为用户主动甩角求中止（决策3兜底机制）。
        """
        try:
            import pyautogui
            x, y = pyautogui.position()
            return x <= 0 and y <= 0
        except Exception:
            return False

    # ── 移动 ─────────────────────────────────────────────────────────────

    async def move(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()
        x, y = params.get("x"), params.get("y")
        if x is None or y is None:
            return {"ok": False, "data": {}, "summary": "", "error": "missing x/y"}
        try:
            import pyautogui
            pyautogui.moveTo(x, y)
            return {"ok": True, "data": {"x": x, "y": y}, "summary": f"moved mouse to ({x},{y})", "error": ""}
        except Exception as e:
            if "FailSafe" in type(e).__name__:
                self._estop.trigger("failsafe-corner")
                return self._aborted()
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 拖拽 ─────────────────────────────────────────────────────────────

    async def drag(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()
        try:
            import pyautogui
            x1, y1 = params.get("from_x"), params.get("from_y")
            x2, y2 = params.get("to_x"), params.get("to_y")
            if None in (x1, y1, x2, y2):
                return {"ok": False, "data": {}, "summary": "", "error": "missing drag coordinates"}
            pyautogui.moveTo(x1, y1)
            pyautogui.dragTo(x2, y2, duration=0.4)
            return {"ok": True, "data": {"from": [x1, y1], "to": [x2, y2]},
                    "summary": f"dragged ({x1},{y1}) -> ({x2},{y2})", "error": ""}
        except Exception as e:
            if "FailSafe" in type(e).__name__:
                self._estop.trigger("failsafe-corner")
                return self._aborted()
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 键盘输入 ─────────────────────────────────────────────────────────

    async def type_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()
        await asyncio.to_thread(self._focus_target_window)  # 输入前确保目标窗口在前台
        text = params.get("text", "")
        try:
            import pyautogui
            # pyautogui.write 不支持中文，中文用剪贴板粘贴
            if any(ord(c) > 127 for c in text):
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    pyautogui.hotkey("ctrl", "v")
                except ImportError:
                    return {"ok": False, "data": {}, "summary": "",
                            "error": "Chinese text input requires pyperclip"}
            else:
                pyautogui.write(text, interval=0.02)
            return {"ok": True, "data": {"length": len(text)},
                    "summary": f"typed {len(text)} character(s)", "error": ""}
        except Exception as e:
            if "FailSafe" in type(e).__name__:
                self._estop.trigger("failsafe-corner")
                return self._aborted()
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def hotkey(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()
        await asyncio.to_thread(self._focus_target_window)  # 快捷键前确保目标窗口在前台
        keys = params.get("keys", [])
        if isinstance(keys, str):
            keys = keys.replace("+", " ").split()
        if not keys:
            return {"ok": False, "data": {}, "summary": "", "error": "missing keys"}
        try:
            import pyautogui
            pyautogui.hotkey(*keys)
            return {"ok": True, "data": {"keys": keys}, "summary": f"pressed {'+'.join(keys)}", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def scroll(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._estop.is_stopped():
            return self._aborted()
        amount = int(params.get("amount", -3))  # 负=向下
        # direction 给出时决定方向，amount 只取绝对值（工具说明的参数是 {direction, amount}）。
        direction = str(params.get("direction") or "").strip().lower()
        if direction == "down":
            amount = -abs(amount)
        elif direction == "up":
            amount = abs(amount)
        elif direction:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"unsupported scroll direction {direction!r}; use 'up' or 'down'"}
        try:
            import pyautogui
            pyautogui.scroll(amount * 100)
            return {"ok": True, "data": {"amount": amount},
                    "summary": f"scrolled {'down' if amount < 0 else 'up'} {abs(amount)} notch(es)", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 工具 ─────────────────────────────────────────────────────────────

    def _aborted(self) -> Dict[str, Any]:
        return {"ok": False, "data": {}, "summary": "", "error": "emergency stop triggered; operation aborted", "aborted": True}

    @staticmethod
    def _kind_label(kind: str) -> str:
        return {"click": "clicked", "double": "double-clicked", "right": "right-clicked"}.get(kind, "clicked")
