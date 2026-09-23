# core/os_layer/__init__.py
"""Nano· OS 层。

模块：
  dsl.py          —— action 枚举 / 风险地板 / 动态升级 / 状态转移表（契约层）
  executor_low.py —— 低级执行器只读子集（手眼·只读部分）
  audit.py        —— 审计日志（全量流水留痕）
  dispatch.py     —— 执行层调度入口（校验→执行→审计）

另有：executor_vision / executor_action / safety（授权·急停·熔断）

坐标系一致性：本进程在导入时声明 Per-Monitor DPI Awareness，让 pyautogui /
uiautomation / 截图（mss）在同一套真实物理像素坐标系下工作，从根上避免"进程未
声明DPI感知→Windows悄悄做坐标虚拟化缩放→截图算出来的坐标和实际点击坐标对不上"
这类与显示器缩放比例相关、且因人而异的系统性偏移。详见 _ensure_dpi_awareness()。
"""
import ctypes as _ctypes
from loguru import logger as _logger


def _ensure_dpi_awareness() -> None:
    """进程级声明 DPI 感知（仅 Windows，且只需声明一次）。

    必须在任何窗口/GDI相关调用之前执行——这里是 os_layer 包第一次被导入的时刻，
    早于 VisionLocator/ActionExecutor 实例化，时机合适。
    """
    import platform
    if platform.system() != "Windows":
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2，多显示器不同缩放比例下也准确
        _ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _logger.debug("[OS-Coord] 已声明 Per-Monitor DPI 感知")
    except Exception:
        try:
            _ctypes.windll.user32.SetProcessDPIAware()
            _logger.info("[OS-Coord] 已声明基础 DPI 感知（shcore 不可用，降级 user32）")
        except Exception as e:
            _logger.warning(f"[OS-Coord] DPI 感知声明失败，坐标系可能与显示缩放不一致: {e}")


def verify_coord_consistency() -> tuple[bool, str]:
    """运行期校验：pyautogui 认为的屏幕尺寸是否与系统真实物理分辨率一致。

    用于在 DPI 声明仍失效（极少数环境）时兜底报警，而不是默默产生固定偏移。
    返回 (是否一致, 说明文字)。只做检测，不做事后补偿——补偿系数不可靠
    （验收时的结论：偏移可能和显示缩放、模型内部图像处理等多个因素相关，
    不存在一个能跨机器通用的固定换算公式），一致性应该在源头（DPI声明）解决。
    """
    import platform
    if platform.system() != "Windows":
        return True, "非 Windows，跳过"
    try:
        import pyautogui
        pw, ph = pyautogui.size()
        rw = _ctypes.windll.user32.GetSystemMetrics(0)
        rh = _ctypes.windll.user32.GetSystemMetrics(1)
        if (pw, ph) == (rw, rh):
            return True, f"一致 ({pw}x{ph})"
        return False, f"不一致：pyautogui={pw}x{ph}，真实物理分辨率={rw}x{rh}"
    except Exception as e:
        return False, f"校验异常: {e}"


_ensure_dpi_awareness()
