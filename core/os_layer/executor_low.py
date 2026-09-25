# core/os_layer/executor_low.py
"""
低级执行器 —— 只读动作。

只实现【只读】原子能力，零写操作、零鼠标控制：
  - screenshot          截屏
  - get_sysinfo         CPU/内存/磁盘/网络/电源
  - read_registry       读注册表
  - read_window_tree    读前台窗口 UIA 控件树
  - list_windows        列顶层窗口
  - get_cursor_pos      鼠标坐标
  - wait                等待

设计要点（实现约束 1 / 2）：
- 本执行器【只报状态，不决策】。每个方法返回结构化结果 dict，不决定"下一步做什么"。
- 平台依赖（win32/psutil/PIL/uiautomation）全部 lazy import + 优雅降级：
  缺库时返回 {"ok": False, "error": "dependency_missing: xxx"}，不抛异常崩溃。
  这样在没装全依赖的机器上也能先跑通链路骨架，再逐个补依赖。

返回协议（统一）：
  {"ok": bool, "data": {...}, "summary": str, "error": str}
"""
from __future__ import annotations
import asyncio
import pathlib
import ctypes
import platform
from typing import Any, Dict, Optional
from loguru import logger

from core.self_identity import is_self_window


def _missing(dep: str) -> Dict[str, Any]:
    return {"ok": False, "data": {}, "summary": "", "error": f"dependency_missing: {dep}"}


def _H(hwnd):
    """把 Python int 转成 ctypes 能安全接的句柄。

    ⚠️ **不转就会在大 hwnd 上抛 `OverflowError`**：ctypes 对未声明原型的
    Win32 函数默认按 `c_int` 处理，而 hwnd 可以超过 2^31。
    实测这台机器上就有 `hwnd=30803936`、`22480158` 这类值，更大的也存在。
    ⭐ 这个错法是**静默**的：调用点普遍包着 `except Exception` →
    那个窗口就悄悄从枚举结果里消失了。同一个坑在 `takeover_hooks` 已经踩过一次。
    """
    return ctypes.c_void_p(int(hwnd))


def foreground_identity() -> Optional[Dict[str, Any]]:
    """前台窗口的**身份**：`{hwnd, pid, proc, title}`。取不到返回 None。

    ⚠️⚠️ **为什么要 hwnd，光有标题不够：** 两个未命名的记事本标题**完全相同**
    （都是"无标题 - 记事本"）。靠标题判身份，正好在最需要分清的场景下失效。
    hwnd 是操作系统给的唯一句柄，这才是身份。

    ⭐ 这个函数存在的理由是一次**真实的数据损坏**（2026-08-07 实测）：
    Nano 要操作它自己打开的 `新建文本文档.txt`，用户中途把焦点放到了**自己的**
    另一个记事本上。`get_target_window()`（见下）返回的是"最前面那个非 Nano 窗口"，
    于是 Nano 对着**用户的**记事本 Ctrl+A + 输入，**清掉了用户的内容**。
    而 `look_at_screen` 当时只返回视觉模型的散文描述，**一个字都没提在看哪个窗口** ——
    模型除了在图里认标题之外，没有任何手段发现自己换了对象。
    📌 **别让模型去「记得怀疑」，把变化本身摆到它眼前。**
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        proc = ""
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return {"hwnd": int(hwnd), "pid": int(pid.value),
                "proc": proc, "title": buf.value or ""}
    except Exception as e:
        logger.debug(f"[OS-Low] 取前台窗口身份失败: {e}")
        return None


# read_window_tree 最多返回的节点数（有名字或 AutomationId 的控件）。
_WINDOW_TREE_MAX_NODES = 300


def get_target_window():
    """返回 Z-order 最靠前、且不是 Nano 自己的窗口对象；找不到返回 None。

    pygetwindow.getAllWindows() 在 Windows 后端走 EnumWindows，天然按
    Z-order（从最前到最后）排列，所以"第一个不是 Nano 自己的窗口"就是
    用户当前实际在看的、最可能想让 Nano 操作的窗口。

    ⚠️⚠️ **这是一个「任务开始时」的启发式，不是「每一步都能重新问」的权威。**
    它的语义是"用户此刻在看哪个窗口" —— 开始一个任务时这么猜是对的；
    但**任务中途**"最前面的窗口"等于"谁最后动过"，包括**用户自己的窗口**。
    2026-08-07 实测的数据损坏就是这么发生的（详见 `foreground_identity` 的说明）。

    📌 与运行作用域那条同形：**一个事实只能证明它在被测那一刻成立，不能证明它现在仍然成立。**

    ✅ **已修（2026-08-07）**：**有绑定就用绑定**（`window_binding`，绑定期 = 活动租约寿命）；
       没有绑定才退回下面那个 Z-order 启发式。
       ⭐ 退化路径**刻意保留** —— 一次任务的**第一步**本来就没有绑定可用，
       那时候"操作用户正在看的窗口"是对的语义。
       📌 **同一个启发式，在任务开始时是对的、在任务中途是错的** ——
       所以修法不是把它删掉，是给它加一个更强的前置。
    """
    try:
        from core.os_layer import window_binding as _wb
        from core.runtime.kernel import get_kernel as _gk
        from core.runtime import oslease as _ol
        _cur = _ol.current_activity(_gk())
        if _cur is not None and _cur.holder == _ol.Holder.NANO:
            _b = _wb.bound(_cur.lease_id)
            if _b:
                import pygetwindow as _gw
                _w = _gw.Win32Window(_b["hwnd"])
                logger.info(f"[OS-Low] get_target_window 用【绑定】: "
                            f"hwnd={_b['hwnd']} {_b['title']!r}")
                return _w
    except Exception as e:
        # 绑定层出任何问题都退回启发式 —— 它至少是今天的行为，不会更差
        logger.debug(f"[OS-Low] 绑定查询失败，退回 Z-order 启发式: {e}")
    try:
        import pygetwindow as gw
    except ImportError:
        return None
    try:
        _seen = []
        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title:
                continue
            try:
                width, height = w.width, w.height
            except Exception:
                width = height = 0
            _seen.append(f"{title!r}(min={getattr(w,'isMinimized','?')},{width}x{height})")
            if is_self_window(getattr(w, "_hWnd", 0)):
                continue
            # 过滤系统外壳小部件（任务栏"开始"按钮等），不是真实应用窗口。
            # 实测复现：pygetwindow 枚举会把"开始"排在最前面，宽高只有
            # 48x40 左右，远小于任何真实应用窗口，之前没过滤导致它被
            # 误选成"目标窗口"，UIA 扫它的控件树自然找不到任何东西。
            if width < 200 or height < 150:
                continue
            logger.info(f"[OS-Low] get_target_window 选中: {title!r}({width}x{height}) | 扫描顺序前几个: {_seen[:6]}")
            return w
        logger.warning(f"[OS-Low] get_target_window 未找到合适的目标窗口 | 扫描顺序: {_seen[:10]}")
    except Exception as e:
        logger.warning(f"[OS-Low] get_target_window 异常: {e}")
    return None


class LowLevelExecutor:
    """只读系统能力。Windows 优先；非 Windows 上多数能力返回 unsupported。"""

    def __init__(self, audit_logger=None, screenshot_dir=None):
        self._audit = audit_logger
        self._is_windows = platform.system() == "Windows"
        self._screenshot_dir = screenshot_dir

    # ── 系统信息（psutil）────────────────────────────────────────────────
    async def get_sysinfo(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_sysinfo_sync, params)

    def _get_sysinfo_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import psutil
        except ImportError:
            return _missing("psutil")
        try:
            # ── 字段名归一化（模型可能用更自然的命名，全部映射到内部标准名）──
            _FIELD_ALIASES = {
                "cpu": "cpu", "cpu_usage": "cpu", "cpu_percent": "cpu",
                "cpu_info": "cpu", "processor": "cpu",
                "memory": "memory", "memory_usage": "memory", "mem": "memory",
                "ram": "memory", "memory_info": "memory",
                "disk": "disk", "disk_usage": "disk", "storage": "disk",
                "battery": "battery", "power": "battery", "charge": "battery",
                "network": "network", "net": "network", "bandwidth": "network",
            }
            raw_fields = params.get("fields") or ["cpu", "memory", "disk", "battery"]
            fields = list(dict.fromkeys(                     # 去重保序
                _FIELD_ALIASES.get(f.lower(), f) for f in raw_fields
            ))
            data: Dict[str, Any] = {}
            if "cpu" in fields:
                data["cpu_percent"] = psutil.cpu_percent(interval=0.3)
                data["cpu_count"] = psutil.cpu_count()
            if "memory" in fields:
                vm = psutil.virtual_memory()
                data["memory"] = {
                    "total_gb": round(vm.total / 1e9, 2),
                    "used_gb": round(vm.used / 1e9, 2),
                    "percent": vm.percent,
                }
            if "disk" in fields:
                du = psutil.disk_usage("/")
                data["disk"] = {
                    "total_gb": round(du.total / 1e9, 2),
                    "used_gb": round(du.used / 1e9, 2),
                    "percent": du.percent,
                }
            if "battery" in fields:
                try:
                    bat = psutil.sensors_battery()
                    data["battery"] = ({"percent": bat.percent, "plugged": bat.power_plugged}
                                       if bat else None)
                except Exception:
                    data["battery"] = None
            summary = self._fmt_sysinfo(data)
            return {"ok": True, "data": data, "summary": summary, "error": ""}
        except Exception as e:
            logger.error(f"[OS-Low] get_sysinfo 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    @staticmethod
    def _fmt_sysinfo(d: Dict[str, Any]) -> str:
        parts = []
        if "cpu_percent" in d:
            parts.append(f"CPU {d['cpu_percent']}%")
        if "memory" in d:
            parts.append(f"memory {d['memory']['percent']}% ({d['memory']['used_gb']}/{d['memory']['total_gb']}GB)")
        if "disk" in d:
            parts.append(f"disk {d['disk']['percent']}%")
        if d.get("battery"):
            parts.append(f"battery {d['battery']['percent']}%")
        return ", ".join(parts)

    # ── 截屏（PIL/mss）───────────────────────────────────────────────────
    async def screenshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._screenshot_sync, params)

    def _screenshot_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import mss
            import mss.tools
        except ImportError:
            # 退而求其次试 PIL ImageGrab（Windows/Mac）
            try:
                from PIL import ImageGrab
            except ImportError:
                return _missing("mss or pillow")
            try:
                img = ImageGrab.grab()
                path = self._save_shot(img_pil=img)
                return {"ok": True, "data": {"path": str(path), "size": img.size},
                        "summary": f"screenshot saved to {path}", "error": ""}
            except Exception as e:
                return {"ok": False, "data": {}, "summary": "", "error": str(e)}
        try:
            import datetime as _dt
            with mss.mss() as sct:
                monitor = sct.monitors[params.get("monitor", 1)]
                shot = sct.grab(monitor)
                if self._screenshot_dir is None:
                    return {"ok": False, "data": {}, "summary": "", "error": "no screenshot_dir configured"}
                fname = f"shot_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                path = self._screenshot_dir / fname
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))
                return {"ok": True,
                        "data": {"path": str(path), "size": [shot.width, shot.height]},
                        "summary": f"screenshot saved to {path}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Low] screenshot 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # 🪦 **这里曾是 `_prune_shots()` + `_SHOT_KEEP = 20`**，2026-08-25 移走。
    #    搬去了 `audit.OSAuditLogger.prune_screenshots()`，并且换成按**体积**。
    #
    # 🔴 移走的理由是它在这里**永远跑不全**：它只在本类保存 `shot_*` 时被调用，
    #    而截图目录里 47 张里有 44 张是视觉定位链（`executor_vision` / `dispatch`）
    #    写的 —— 那条链一次都不会触发它。实测 47 张 / 52.4 MB，上限写着 20。
    # 📌 **目录是 audit 的，回收就该是 audit 的责任** ——
    #    挂在某一个写入方身上，等于要求所有写入方都自觉，而漏一个就永远漏。
    # ⚠️ 别在这里加回一个「顺手也清一下」：**判据只能有一处**，
    #    两处清理迟早会有不同的预算。

    def _save_shot(self, img_pil) -> Any:
        import datetime as _dt
        fname = f"shot_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        path = self._screenshot_dir / fname
        img_pil.save(str(path))
        # ⚠️ **两条写盘路径都要清理** —— 这是 PIL 兜底那条。
        #    📌 只在其中一条接回收，表现是「有时候清理有时候不清理」，
        #       而那比完全不清理更难查（它取决于 mss 装没装）。
        return path

    # ── 读注册表（winreg，只读）──────────────────────────────────────────
    async def read_registry(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._read_registry_sync, params)

    def _read_registry_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: registry is Windows-only"}
        try:
            import winreg
        except ImportError:
            return _missing("winreg")
        hive_map = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT, "HKU": winreg.HKEY_USERS,
        }
        try:
            hive = hive_map.get(params.get("hive", "HKCU"))
            if hive is None:
                return {"ok": False, "data": {}, "summary": "", "error": f"unknown hive: {params.get('hive')}"}
            subkey = params.get("subkey", "")
            value_name = params.get("value_name", "")
            with winreg.OpenKey(hive, subkey) as key:
                val, regtype = winreg.QueryValueEx(key, value_name)
            return {"ok": True, "data": {"value": val, "type": regtype},
                    "summary": f"{params.get('hive')}\\{subkey}\\{value_name} = {val}", "error": ""}
        except FileNotFoundError:
            return {"ok": False, "data": {}, "summary": "", "error": "registry key/value not found"}
        except Exception as e:
            logger.error(f"[OS-Low] read_registry 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 列窗口 / 读控件树（pygetwindow / uiautomation）──────────────────
    async def list_windows(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._list_windows_sync, params)

    def _list_windows_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import pygetwindow as gw
        except ImportError:
            return _missing("pygetwindow")
        try:
            wins = []
            for w in gw.getAllWindows():
                title = (w.title or "").strip()
                if not title:
                    continue
                wins.append({"title": title, "active": bool(getattr(w, "isActive", False)),
                             "minimized": bool(getattr(w, "isMinimized", False))})
            return {"ok": True, "data": {"windows": wins, "count": len(wins)},
                    "summary": f"{len(wins)} visible window(s)", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Low] list_windows 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def read_window_tree(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._read_window_tree_sync, params)

    def _read_window_tree_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: UIA is Windows-only"}
        try:
            import uiautomation as auto
        except ImportError:
            return _missing("uiautomation")
        try:
            max_depth = int(params.get("max_depth", 8))
            # 读目标窗口（排除 Nano 自己），不直接读前台：用户刚在 Nano 里发完消息时，
            # 前台几乎总是 Nano 自己。没有目标窗口时退回前台。
            ctrl = None
            win = get_target_window()
            if win is not None:
                ctrl = auto.ControlFromHandle(win._hWnd)
            if not ctrl:
                ctrl = auto.GetForegroundControl()
            if not ctrl:
                return {"ok": False, "data": {}, "summary": "", "error": "no target window"}
            budget = [_WINDOW_TREE_MAX_NODES]

            def walk(c, depth):
                if depth > max_depth or budget[0] <= 0:
                    return None
                name = (c.Name or "").strip()
                aid = (getattr(c, "AutomationId", "") or "").strip()
                kids = []
                try:
                    for child in c.GetChildren():
                        sub = walk(child, depth + 1)
                        if sub:
                            kids.append(sub)
                except Exception:
                    pass
                # 没有名字、没有 AutomationId 的容器不单独占一层：把子节点提上来
                if not name and not aid:
                    return kids[0] if len(kids) == 1 else ({"type": c.ControlTypeName, "children": kids} if kids else None)
                budget[0] -= 1
                node = {"type": c.ControlTypeName}
                if name:
                    node["name"] = name
                if aid:
                    node["id"] = aid
                if kids:
                    node["children"] = kids
                return node

            tree = walk(ctrl, 0)
            title = (getattr(ctrl, "Name", "") or "").strip()
            return {"ok": True, "data": {"window": title, "tree": tree},
                    "summary": (f"read control tree of {title!r} (depth <= {max_depth}"
                                + (", truncated" if budget[0] <= 0 else "") + "). "
                                "Click controls by their exact name or id as target."),
                    "error": ""}
        except Exception as e:
            logger.error(f"[OS-Low] read_window_tree 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 鼠标坐标（只读）──────────────────────────────────────────────────
    async def get_cursor_pos(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import pyautogui
        except ImportError:
            return _missing("pyautogui")
        try:
            x, y = pyautogui.position()
            return {"ok": True, "data": {"x": x, "y": y}, "summary": f"cursor position ({x}, {y})", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── wait（无副作用）──────────────────────────────────────────────────
    async def wait(self, params: Dict[str, Any]) -> Dict[str, Any]:
        secs = float(params.get("seconds", 0.5))
        secs = max(0.0, min(secs, 10.0))  # 上限 10s，防滥用
        await asyncio.sleep(secs)
        return {"ok": True, "data": {"waited": secs}, "summary": f"waited {secs}s", "error": ""}

    # ── list_dir（只读，列目录真实文件名）───────────────────────────────────
    # 背景：Windows 默认隐藏扩展名，"新建 文本文档 (2)"这类文件名模型只能
    # 看到不带后缀的样子，容易凭感觉拼错扩展名导致 file_delete/file_move 等
    # 操作"文件不存在"。在做这类操作前应该先用这个 action 列目录确认真实
    # 文件名（含完整扩展名），不要瞎猜。
    async def list_dir(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._list_dir_sync, params)

    def _list_dir_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        import os as _os
        raw = params.get("path") or "%USERPROFILE%\\Desktop"
        path = _os.path.expandvars(_os.path.expanduser(raw))
        try:
            p = pathlib.Path(path)
            if not p.exists():
                return {"ok": False, "data": {}, "summary": "", "error": f"directory does not exist: {path}"}
            if not p.is_dir():
                return {"ok": False, "data": {}, "summary": "", "error": f"not a directory: {path}"}
            entries = []
            for child in sorted(p.iterdir())[:200]:  # 上限 200 条，防止超大目录刷屏
                entries.append({"name": child.name, "is_dir": child.is_dir()})
            names = ", ".join(e["name"] for e in entries[:50])
            return {"ok": True, "data": {"path": str(p), "entries": entries},
                    "summary": f"{p} ({len(entries)} item(s)): {names}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Low] list_dir 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 当前目标窗口名（供动态升级规则判定，不是 DSL action）───────────────
    # 排除 Nano 自己的窗口（见上方 get_target_window 注释），不再用
    # gw.getActiveWindow()（=raw OS 前台窗口，几乎总是 Nano 自己）。
    def get_foreground_window_title(self) -> str:
        if not self._is_windows:
            return ""
        try:
            w = get_target_window()
            return (w.title if w else "") or ""
        except Exception:
            return ""
