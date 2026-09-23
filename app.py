import html
import os
import json
import urllib.parse
# 彻底关闭新版中间件，退回兼容性最好的旧版解析逻辑
os.environ['FLAGS_enable_pir_api'] = '0' 
# 关闭多线程动态图优化（可选，若还是报 kernel error 可开启此项）
os.environ['FLAGS_use_mkldnn'] = '0'
os.environ["PADDLE_PDX_MODEL_SOURCE"] = "huggingface"

import asyncio
import inspect
import pathlib
import re
import time
from contextlib import nullcontext

# 「…」按钮该不该出现，**由浏览器判**（scrollWidth > clientWidth），
# 见 `_u7_sync_more_buttons()`。这里刻意不再留任何字数/宽度常量：
# 第一版 `len(text) > 46` 让短中文误挂按钮（中文宽度是 ASCII 两倍），
# 改成按东亚宽度折算也只是把误差变小 —— 真正的容量随窗口宽度变，
# 同一句话窄窗截断、宽窗不截断，Python 侧根本算不出来。
# ── AuthorizationLease 门面 ───────────────────
# ⚠️ 这三个原来是**观测期的 shadow**（只观测、不影响行为）。现在 `os.temp_auto`
#    已经是权威，`_auto_on()` 读的就是它 —— 所以这里不再是"观测门面"。
# ⚠️ 仍然吞异常，但**吞掉之后的退化方向不同，必须分清**：
#    · `grant` 失败 → 授权没发出去 → `_auto_on()` 为假 → **照常弹确认**（安全方向）
#    · `revoke` 失败 → 授权可能还在 → 不该免确认时免了（危险方向）
#      所以 `oslease.revoke_temp_auto` 里那条是 `warning` 级别留痕，不是安静吞。
def _rt_auto_grant(reason: str = "") -> None:
    try:
        from core.runtime import oslease as _ol
        _ol.grant_temp_auto(reason)
    except Exception:
        pass


def _rt_auto_revoke() -> None:
    try:
        from core.runtime import oslease as _ol
        _ol.revoke_temp_auto()
    except Exception:
        pass


def _rt_auto_authorized() -> bool:
    """⭐ 切读之后的**权威读点**。⚠️ fail-safe 方向是「没授权」→ 照常弹确认。"""
    try:
        from core.runtime import oslease as _ol
        return _ol.temp_auto_authorized()
    except Exception:
        return False


def _rt_auto_compare(legacy: bool) -> None:
    try:
        from core.runtime import oslease as _ol
        _ol.shadow_compare_auto(legacy)
    except Exception:
        pass


# ── durable inbox 门面 ─────────────────────────────────────────
# ⚠️⚠️ **这四个全部吞异常，而且退化方向必须朝「照旧干活」，不是朝「不干活」。**
#    队列的目的是**不丢**，不是**多一道能挡住用户的闸**。
#    📌 如果库挂了就不让用户发消息，那就亲手造出了本项要消灭的那个东西。
def _rt_inbox_submit(body: str, detail: dict | None = None) -> str | None:
    try:
        from core.runtime import inbox as _ib
        return _ib.submit_user_message(body, detail)
    except Exception as e:
        logger.error(f"[Inbox] 落库失败（不阻断，照旧处理这条）: {e}")
        return None


def _rt_inbox_claim(item_id: str | None) -> None:
    """把**这一条**标成「正在处理」。

    ⚠️⚠️ **必须传 `item_id`，不能让内核「认领最早那条」** ——
       UI 侧是从自己的内存队列里取出某一条去跑的，而库里最早那条可能是
       上个进程遗留、被启动收尾退回队列的另一条。
       📌 **「取哪一条去做」和「把哪一条标成在做」必须是同一条**，
          否则跑的是 A、标记的是 B，两条都被记错，而库里看起来完全正常。
    ⚠️ 拿不到 id（落库失败）就跳过，**不影响主流程**。
    """
    if not item_id or item_id.startswith("mem_"):
        return
    try:
        from core.runtime import inbox as _ib
        from core.runtime.kernel import get_kernel, Command
        get_kernel().submit(Command(kind=_ib.CLAIM,
                                    payload={"item_id": item_id}))
    except Exception as e:
        logger.debug(f"[Inbox] 认领失败（忽略）: {e}")


def _rt_inbox_consume(item_id: str | None) -> None:
    if not item_id:
        return
    try:
        from core.runtime import inbox as _ib
        _ib.consume(item_id)
    except Exception as e:
        logger.debug(f"[Inbox] 标记已消费失败（忽略）: {e}")


def _rt_inbox_submit_wake(suspension_id: str, trigger: str) -> str | None:
    """收下一个「继续」意图。⚠️ 与用户消息**分开的 kind**，因为处理路径不同：
    用户消息要起一轮新 turn，唤醒意图要走 `resume_suspension` 恢复一个已有挂起。"""
    try:
        from core.runtime import inbox as _ib
        return _ib.submit_wake_intent(suspension_id, trigger)
    except Exception as e:
        logger.error(f"[Inbox] 唤醒意图落库失败（不阻断）: {e}")
        return None


#: 上个进程留下的、被启动恢复终止掉的那些活。
#: ⚠️ **模块级**，因为写它的那段启动恢复代码本身就在模块级（不在任何函数里）。
#:    📌 第一版写成 `self._startup_interrupted` → `NameError: self`，
#:       而它被那段 `except` 吞成一句「启动恢复失败（不影响启动）」——
#:       **一条被吞掉的 NameError，表现成的是「恢复失败」，不是「有人写错了变量」。**
#: ⚠️ 重启即清（进程级），正好对上「那些活不活过重启」的语义。
_STARTUP_INTERRUPTED: list = []


def _rt_inbox_pending() -> int:
    try:
        from core.runtime import inbox as _ib
        from core.runtime.kernel import get_kernel
        return _ib.pending_count(get_kernel())
    except Exception:
        return 0


from dotenv import load_dotenv
from nicegui import ui, events
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from loguru import logger
load_dotenv()

# ── 延迟导入的核心模块占位 ───────────────────────────────────────────────
# Windows / NiceGUI native / pywebview 会用 multiprocessing spawn 启动窗口子进程。
# 子进程会 import app.py；如果这里顶层 import core.provider / core.registry / RAG 等
# 重模块，可能在窗口子进程里重复初始化 Provider、DPI、MCP、RAG，进而触发
# PermissionError: [WinError 5] DuplicateHandle。
# 所以 core.* 与 nano_koala 全部放到 _bootstrap_core_modules()，只在主进程启动时导入。
Orchestrator = None
_get_activity_buffer = None
ProactiveSpeaker = None
_IntelEngine = None
_ISignal = None
_start_proactive_hooks = None
os_dsl = None
ClaudeProvider = None
get_provider = None
GeminiProvider = None
CLAUDE_MODELS = []
CLAUDE_MODEL_MAP = {}
GEMINI_MODELS = []
GEMINI_MODEL_MAP = {}
usage_tracker = None
_fmt_tokens = None
registry = None
rag_engine = None
MemoryManager = None
render_nano_koala_avatar = None

def _bootstrap_core_modules() -> None:
    """只在主进程里导入会产生副作用的核心模块。

    注意：native 窗口子进程 import 本文件时不会执行 __main__，因此不会走到这里；
    它只需要读取 app.native.start_args/window_args 这类轻量窗口参数。
    """
    global Orchestrator, _get_activity_buffer, ProactiveSpeaker, _IntelEngine, _ISignal
    global _start_proactive_hooks, os_dsl
    global ClaudeProvider, get_provider, GeminiProvider, CLAUDE_MODELS, CLAUDE_MODEL_MAP, GEMINI_MODELS, GEMINI_MODEL_MAP
    global usage_tracker, _fmt_tokens, registry, rag_engine, MemoryManager, render_nano_koala_avatar

    from nano_koala import render_nano_koala_avatar as _render_nano_koala_avatar
    from core.orchestrator import Orchestrator as _Orchestrator
    from core.proactive.activity import get_buffer as __get_activity_buffer
    from core.proactive.speaker import ProactiveSpeaker as _ProactiveSpeaker
    from core.proactive.intel.engine import ProactiveEngine as __IntelEngine
    from core.proactive.intel.feedback import Signal as __ISignal
    from core.proactive.hooks import start_hooks as __start_proactive_hooks
    from core.os_layer import dsl as _os_dsl
    from core.provider import (
        ClaudeProvider as _ClaudeProvider,
        get_provider as _get_provider,
        CLAUDE_MODELS as _CLAUDE_MODELS,
        CLAUDE_MODEL_MAP as _CLAUDE_MODEL_MAP,
        GeminiProvider as _GeminiProvider,
        GEMINI_MODELS as _GEMINI_MODELS,
        GEMINI_MODEL_MAP as _GEMINI_MODEL_MAP,
    )
    from core.usage import usage_tracker as _usage_tracker, _fmt_tokens as __fmt_tokens
    from core.registry import registry as _registry
    from core import rag as _rag_engine
    from memory.manager import MemoryManager as _MemoryManager

    render_nano_koala_avatar = _render_nano_koala_avatar
    Orchestrator = _Orchestrator
    _get_activity_buffer = __get_activity_buffer
    ProactiveSpeaker = _ProactiveSpeaker
    _IntelEngine = __IntelEngine
    _ISignal = __ISignal
    _start_proactive_hooks = __start_proactive_hooks
    os_dsl = _os_dsl
    ClaudeProvider = _ClaudeProvider
    get_provider = _get_provider
    GeminiProvider = _GeminiProvider
    CLAUDE_MODELS = _CLAUDE_MODELS
    CLAUDE_MODEL_MAP = _CLAUDE_MODEL_MAP
    GEMINI_MODELS = _GEMINI_MODELS
    GEMINI_MODEL_MAP = _GEMINI_MODEL_MAP
    usage_tracker = _usage_tracker
    _fmt_tokens = __fmt_tokens
    registry = _registry
    rag_engine = _rag_engine
    MemoryManager = _MemoryManager

# ── WebView2 Runtime 探测（native 窗口的内核依赖）────────────────────
def _webview2_runtime_present() -> bool:
    """检测系统是否装有 WebView2 Runtime（pywebview 在 Windows 的渲染内核）。

    查 EdgeUpdate 注册表里的 WebView2 客户端 GUID（HKLM 32/64 位 + HKCU）。
    任一存在且有版本号即视为在位。内核不在时 native 会失败，提前探测好降级。
    """
    try:
        import winreg
    except Exception:
        return False
    _GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    _candidates = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_GUID}"),
        (winreg.HKEY_CURRENT_USER,  rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_GUID}"),
    ]
    for hive, subkey in _candidates:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _start_system_tray():
    """常驻系统托盘（Nano = "电脑本身"，✕ 隐藏、不退出）。

    在独立 daemon 线程里等 pywebview 窗口就绪，再用 nano_icon.ico 建 pystray 托盘图标：
    左键/「显示 Nano」→ 恢复窗口；「退出」→ 真退出。托盘消息循环阻塞在本线程。
    仅 native 模式调用；缺 pystray 时静默跳过（不影响启动）。
    """
    import threading

    def _run():
        import time as _t
        try:
            import pystray
            from PIL import Image
            from nicegui import app as _app
        except Exception as e:
            print(f"[Tray] 依赖缺失，跳过系统托盘：{e}")
            return
        # 等窗口就绪（最多 ~30s）
        win = None
        for _ in range(300):
            try:
                win = _app.native.main_window
            except Exception:
                win = None
            if win is not None:
                break
            _t.sleep(0.1)
        if win is None:
            print("[Tray] 窗口未就绪，跳过系统托盘")
            return
        try:
            _img = Image.open(str(pathlib.Path(__file__).parent / "assets" / "nano_icon.ico"))
        except Exception as e:
            print(f"[Tray] 图标加载失败，跳过系统托盘：{e}")
            return

        def _show(icon=None, item=None):
            try:
                win.show()
            except Exception as e:
                print(f"[Tray] 显示窗口失败：{e}")

        def _quit(icon=None, item=None):
            try:
                icon.stop()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass
            import os as _os
            # NiceGUI native 把 WebView2 窗口跑在【子进程】里，只 os._exit 主进程会留下孤儿
            # 窗口（进程结束但窗口还在、显示"连接断开"）。先把窗口子进程连同自己整棵进程树杀净。
            try:
                import psutil
                _me = psutil.Process(_os.getpid())
                for _ch in _me.children(recursive=True):
                    try:
                        _ch.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            _os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("显示 Nano", _show, default=True),  # 左键单击=显示
            pystray.MenuItem("退出", _quit),
        )
        try:
            pystray.Icon("nano", _img, "Nano", menu).run()
        except Exception as e:
            print(f"[Tray] 托盘运行异常：{e}")

    threading.Thread(target=_run, name="nano-tray", daemon=True).start()


# ── 全局路径 ─────────────────────────────────────────────────────────────
# 注意：Windows / NiceGUI native / pywebview 会用 multiprocessing spawn 启动子进程。
# 子进程会 import 本模块，所以这里不能放有副作用的全局初始化（例如
# registry.scan_skills()、ClaudeProvider()、mkdir 等），否则会出现 provider/技能库
# 重复初始化，甚至触发 WinError 5。真正初始化放到 __main__ 启动入口里。
KNOWLEDGE_DIR = pathlib.Path(__file__).parent / "data" / "knowledge"
# ── native 窗口标题栏图标（透明 = 无图标，极简风）─────────────────────
# 必须在模块【顶层】设置，不能放进 __main__ 块：NiceGUI native 的窗口跑在
# spawn 出来的独立子进程里，子进程会 import 本模块（执行顶层代码）但【不会】
# 执行 __main__ 块，而 pywebview 的 _open_window 在子进程内读 app.native.start_args
# 来拿 icon。设在 __main__ 里子进程读不到（icon 丢失，退回默认 python 图标）；
# 设在顶层才会被子进程 import 时执行、填进 start_args，窗口创建时才生效。
def _apply_native_window_icon() -> None:
    try:
        from nicegui import app as _app
        _ico = pathlib.Path(__file__).parent / "assets" / "nano_icon.ico"
        if _ico.exists():
            _app.native.start_args['icon'] = str(_ico)
        # 最小窗口尺寸：像正常桌面应用一样，缩到底就停，内容永远装得下，
        # 不会被用户无限缩小到溢出（同 icon，必须顶层设，spawn 子进程才读得到）。
        _app.native.window_args['min_size'] = (780, 760)
        # 无边框窗口：去掉 OS 标题栏，用 app 内自定义终端风标题栏（三灯+nano@office=拖拽区，
        # 右上自绘最小/最大/关闭）。安全网：无边框下 Alt+仍可关窗，按钮失灵也不会被锁死。
        _app.native.window_args['frameless'] = True
        _app.native.window_args['easy_drag'] = False  # 用 pywebview-drag-region 精确控制拖拽区
        # ── 窗口内文字无法复制 —— 根因就是这一个默认值 ────────────────
        # 🔴 `pywebview.create_window(text_select=...)` **默认 False**，它在页面加载
        #    完成后注入一段 CSS：`body { user-select: none; cursor: default }`
        #    （见 pywebview 的 `js/customize.js`）。
        # ⭐⭐ 这解释了 实测看到的两个现象，而且是**同一个原因**：
        #    ① 「网页版能选中，只有 native 不能」——那段 CSS 只在 native 注入
        #    ② 「启动头几秒能选，过一会儿就不能了」——**注入发生在加载之后**，
        #       秒数不固定是因为它跟页面 ready 的时机挂钩
        #    📌 这两条线索价值极高：它们把嫌疑人从「我们的 CSS」直接排除掉了 ——
        #       我们全项目一个 `user-select` 都没写过，找不到才是正常的。
        # ⚠️ 打开之后标题栏那条拖拽区会变得可选（拖窗口时会顺手选中文字），
        #    所以配套在 CSS 里给 chrome 元素单独关掉选择。
        #    📌 顺序对了：**默认可选、只在少数地方关**，而不是反过来。
        _app.native.window_args['text_select'] = True
    except Exception:
        pass  # 拿不到就退回默认，不影响启动
_apply_native_window_icon()


# ══════════════════════════════════════════════════════════════════════════
# 🔴🔴🔴 [2026-08-24 实测 · 第三次栽在同一个地方] mini 窗**缩不下去**。
#
# ═══ 三次都失败，而三次的根因是三个不同的层 ═══
#   v1  `win.min_size = (...)`      → **死写入**：pywebview 只在建窗那一刻读一次
#   v2  `Form.MinimumSize = ...`    → 对象找对了，**进程找错了**（就是这次）
#   v3  （本次）把这件事**送进那个进程去做**
#
# ═══ v2 为什么静默失败 ═══
# NiceGUI 的 native 模式把 pywebview **跑在 spawn 出来的子进程里**
# （本文件上面那条注释早就写着 —— 但当时在找「哪个对象」，
#  没在问「哪个进程」）。于是主进程里的
# `webview.platforms.winforms.BrowserView.instances` **永远是空的**，
# `_relax_min_size` 每次都走到 `_inst is None` 那一支，
# 打一条 `logger.debug` 然后 return。
# ⇒ 表现正是 用户看到的：**位置对了（move 走代理，跨进程 OK），
#    尺寸纹丝不动（resize 被子进程里那个 780×760 的 MinimumSize 钳住）**。
#
# 📌 **「拿不到就静默返回」把一个必然失败伪装成了偶发降级** ——
#    它每一次都失败，而日志级别是 debug，所以一次都没被看见。
# 📌 更该记住的那条：**问题不在「哪个对象能改」，在「谁有资格改它」。**
#    连着两版都在换对象，而两版的代码都跑在一个碰不到那个窗口的进程里。
#
# ═══ 这次怎么做到的 ═══
# NiceGUI 的跨进程执行器（`native_mode._start_window_method_executor`）最后一支是
# `method = getattr(window, method_name)` —— **任何挂在 Window 上的可调用属性
# 都能被调到**。而子进程会 import 本模块（顶层代码会执行，`__main__` 块不会），
# 所以在**顶层**给 `webview.Window` 挂一个方法，子进程那边就有了。
# ⚠️ 必须挂在**类**上而不是实例上：实例在子进程里创建，我们这边碰不到它。
# ⚠️ 执行器是在**普通线程**里调它的，而 WinForms 控件只能在 UI 线程上改 ——
#    所以里面照抄 pywebview 自己的 `InvokeRequired / Invoke` 写法。
# ⚠️ 整段吞异常：这是**观感优化**，📌 降级要降到「窗口小不下去」，不是「窗口没了」。
# ══════════════════════════════════════════════════════════════════════════
def _patch_webview_min_size() -> None:
    try:
        import webview as _wv
    except Exception:
        return
    if getattr(_wv.Window, "nano_set_min_size", None) is not None:
        return

    def nano_set_min_size(self, w: int, h: int) -> None:
        """在 **pywebview 那个进程**里改 `Form.MinimumSize`。"""
        try:
            from webview.platforms import winforms as _wf
            inst = _wf.BrowserView.instances.get(self.uid)
            if inst is None:
                return
            from System.Drawing import Size as _Size      # noqa: N813
            from System import Action as _Action
            # ⚠️ `MinimumSize` 要**物理像素**（同 pywebview 内部：逻辑 × scale），
            #    而 `resize()` 收的是**逻辑像素**（它自己乘）。
            #    📌 同一带里两个 API 单位不同，是这里最容易错的地方。
            _scale = getattr(inst, "_scale", 1.0) or 1.0
            _target = _Size(int(w * _scale), int(h * _scale))

            def _apply():
                inst.MinimumSize = _target

            if inst.InvokeRequired:
                inst.Invoke(_Action(_apply))
            else:
                _apply()
        except Exception:
            pass

    try:
        _wv.Window.nano_set_min_size = nano_set_min_size
    except Exception:
        pass


_patch_webview_min_size()

# ── SVG 头像（内联，用于聊天气泡和 favicon）──────────────────────────────
# "光影与星辰"主题：4方向十字光芒 + 4条斜线副芒 + 3颗闪烁星点，全部用 SVG SMIL 动画
# ⭐⭐⭐ 一段回应期的 UI 状态。**原来是个裸 dict，两处各写一遍。**
#
# ═══ 它解决的是什么 ═══
#
# 🔴 问题一：**两个构造点，两份逐字相同的 20 键字面量**（`start_pipeline_task`
#    与 `_drive_wake_inner`）。📌 同一件事有两个实现，它们只在
#    「我两次想法相同」的前提下一致 —— 而这两处相隔六千行。
# 🔴 问题二：代码实际读写 **28** 个键，字面量里只有 20 个。多出来的 8 个
#    （`is_waiting_pill` / `pill_settled` / `waiting_for_carrier` /
#     `batch_start_time` / `batch_fail_count` / `tool_pill_row` /
#     `tool_pill_dollar` / `tool_pill_fail_lbl` / `_status_timer_task`）
#    **只靠「某处赋值过」存在** —— 于是「这个回应期到底有哪些字段」
#    这个问题，全项目没有任何一个地方能回答。
# 🔴 问题三（最要命）：裸 dict 上**写错一个键名不会报错，读错一个键名也不会** ——
#    `.get()` 安静地返回 `None`，于是那段 UI 逻辑变成「有时才执行」。
#    📌 本项目反复栽的就是这个形状：**漏掉的那种不会报错。**
#
# ═══ 为什么是「兼容层」而不是重写 ═══
#
# ⚠️ 这一层是**唯一测试覆盖不到的**（DOM 不能在断言里驱动），
#    每一处改动都要一次实测往返才能确认。
# ⭐ 所以做成 **drop-in**：`__getitem__` / `__setitem__` / `get` / `setdefault`
#    全部代理到属性上，**28 个键 × 每一处调用点一个字都不用改**。
#    📌 一次「行为零变化」的迁移，才有资格在测不到的地方做。
#
# ═══ 严格性放在测试里，不放在运行时 ═══
#
# ⚠️ 未知键**只记一条 warning，照旧存下来**，不抛异常。
#    📌 一个会在用户面前崩溃的守卫，迟早会被人加上 try/except 绕过 ——
#       而绕过之后它连 warning 都不剩了。
# ⭐ 真正的闸在 `tests/t_f1_stage7_uiboundary.py`：**声明的字段集必须覆盖
#    代码里出现过的每一个键**。运行时宽容，测试严格。
#
# ⚠️⚠️ **的边界在这里仍然成立**：这些字段一个都不许进 Kernel。
#    它们天生不可重放（DOM 随窗口消失），而
#    📌 一个「跨重启可信」的存储里混进一个「重启就失效」的字段，
#       代价不是多占一列，是**整张表的可信度都降级了**。
# ⭐⭐⭐ [2026-08-23] 聊天流里的图片：**缩略图 + 点开看大图**。一处实现，四处共用。
#
# 🔴 问题：Nano 的截图在气泡里画得又大又糊，而且**点不开** ——
#    要看清只能去磁盘翻文件。而 用户随即指出这不止是截图的事：
#    **用户自己上传的图发出去之后，一样点不开。**
#    📌 同一个缺陷散在四处（Nano 截图 / 发送前预览 / 发送后气泡 / 重放），
#       每一处都各写了一段 `<img>`，于是「点开看大图」这件事没有任何一处负责。
#
# ⭐ 判据：**截图是证据，不是内容** ——
#    在气泡里知道「它看过这个」就够了，要细看再点开。
#    所以缩略图可以很小（高 120px），大图才是用来看的。
# ⚠️ 刻意**不做成「在系统里打开本地文件」**：那把用户赶出了 Nano，
#    而且用户上传的图根本没有本地路径可开。
#    📌 一个只对其中一种来源成立的方案，不是这四处的公共解。
#
# ⚠️ 大图用 `ui.dialog` 而不是新窗口：native 窗口里开新窗会脱离 mini/full 那套
#    几何管理，而这只是「看一眼」。
def chat_image(src: str, *, alt: str = "", thumb_h: int = 120):
    """在聊天流里放一张图：默认缩略图，点击弹出大图。

    `src` 可以是 `data:` URI，也可以是任何 `<img>` 认的地址 ——
    📌 让它只认一种来源，就会再次分裂成四份实现。
    """
    _esc = (alt or "").replace('"', "&quot;")

    def _open():
        with ui.dialog() as _dlg, ui.card().classes("p-0").style(
                "background:var(--nano-bg); border:1px solid rgba(var(--nano-amber-rgb), 0.25); "
                "max-width:92vw; max-height:92vh;"):
            ui.html(
                f'<img src="{src}" alt="{_esc}" '
                f'style="display:block; max-width:92vw; max-height:88vh; '
                f'object-fit:contain;">'
            )
            # ⚠️ 关闭走点击遮罩 + 这一行，两条路都留着：
            #    📌 一个只能用某一种手势关掉的浮层，在那种手势失灵时就是个陷阱。
            with ui.row().classes("w-full justify-end").style("padding:6px 10px;"):
                ui.button("关闭", on_click=_dlg.close).props("flat dense").style(
                    "color:var(--nano-fg-soft); font-size:var(--nano-fs-base); font-family:var(--nano-mono);")
        _dlg.open()

    _img = ui.html(
        f'<img src="{src}" alt="{_esc}" '
        f'style="max-height:{thumb_h}px; max-width:100%; border-radius:8px; '
        f'border:1px solid rgba(var(--nano-contrast-rgb), 0.10); margin-top:6px; display:block; '
        f'cursor:zoom-in;">'
    )
    _img.on("click", lambda _: _open())
    _img.tooltip("点击查看大图")
    return _img


# ⚠️⚠️ **继承 `Mapping` 是被一次实测回归逼出来的**（2026-08-23）：
#    `app.py` 里有一处 `isinstance(_rs, dict)`，换成 ViewSession 之后
#    它当场变 False，截图被画到了整个聊天区里（图巨大 + 位置错），
#    **而且没有任何异常或日志** —— 静默降级。
# 📌 **一个 drop-in 兼容层，光有方法不够，它还得能通过类型检查** ——
#    `get` / `__getitem__` 都实现了，唯独 `isinstance` 改不了。
# ⭐ 注册成 `Mapping` 的虚拟子类之后 `isinstance(x, Mapping)` 为真；
#    ⚠️ 但 `isinstance(x, dict)` **仍然是 False**（dict 是具体类型，不能虚拟注册）——
#    所以真正的防线是下面那条断言：**代码里不许再出现 `isinstance(…, dict)`
#    去判断回应期**。📌 挡不住的东西，就让测试来禁止它。
class ViewSession:
    """一段回应期（一个 `nano ❯` 气泡）的 UI 状态。

    ⭐ 「一段回应期」而不是「一轮」：它可以跨多轮（无缝续接、回看续接）。
       判据见 `_sync_task_pill` 与 `_drive_wake_inner` 里那条气泡合并规则。
    """

    # ── 声明式字段集 —— **这里就是「一个回应期有哪些字段」的唯一答案** ──
    #    ⚠️ 加字段就加在这里；加在别处等于又回到裸 dict。
    _FIELDS: dict = {
        # 结构（气泡自身）
        "container": None,          # 整个 nano ❯ 块
        "meta_row": None,           # 底下那行 spinner/头像/token
        "loading_col": None,        # 正文列
        "content_md": None,         # 正文的 markdown 元素
        # 文本流
        "current_text": "",
        "text_checkpoint": "",
        # 生命周期
        "running": True,
        "pending_epoch": False,     # 排队中、还没真正开跑
        "start_time": 0.0,
        "tok_base": 0,
        "_status_timer_task": None,
        # 元信息行部件
        "status_lbl": None,
        "spin_lbl": None,
        "svg_el": None,
        # 工具卡（本批次）
        "tool_count": 0,
        "batch_tool_count": 0,
        "batch_fail_count": 0,
        "batch_start_time": 0.0,
        "had_text_since_tool": True,
        "action_refs": None,        # ⚠️ dict，构造时必须给独立实例
        "tool_pill_row": None,
        "tool_pill_lbl": None,
        "tool_pill_arrow": None,
        "tool_pill_dollar": None,
        "tool_pill_fail_lbl": None,
        "tool_details_col": None,
        "pill_settled": False,
        # 等待 pill
        "is_waiting_pill": False,
        "waiting_for_carrier": False,
    }

    __slots__ = tuple(_FIELDS) + ("_extra",)

    def __init__(self, **kw):
        for _k, _v in self._FIELDS.items():
            object.__setattr__(self, _k, _v)
        object.__setattr__(self, "_extra", {})
        # ⚠️ `action_refs` 默认值是 dict —— **每个实例必须拿到自己那份**，
        #    不能共享类级别的那个（那是 Python 可变默认值的经典坑）。
        if kw.get("action_refs") is None:
            kw.setdefault("action_refs", {})
        for _k, _v in kw.items():
            self[_k] = _v

    # ── dict 兼容面（drop-in 的全部内容）────────────────────────────
    def __getitem__(self, k):
        if k in self._FIELDS:
            return getattr(self, k)
        return self._extra[k]

    def __setitem__(self, k, v):
        if k in self._FIELDS:
            object.__setattr__(self, k, v)
            return
        # ⚠️ 未知键：**记一条再存下来**，不抛。理由见类注释。
        try:
            logger.warning(f"[L7] ViewSession 收到未声明的字段 {k!r} —— "
                           f"要么是拼错了，要么该加进 _FIELDS")
        except Exception:
            pass
        self._extra[k] = v

    def __contains__(self, k) -> bool:
        return k in self._FIELDS or k in self._extra

    def get(self, k, default=None):
        if k in self._FIELDS:
            return getattr(self, k)
        return self._extra.get(k, default)

    def setdefault(self, k, default=None):
        if k in self._FIELDS:
            _cur = getattr(self, k)
            if _cur is None:
                object.__setattr__(self, k, default)
                return default
            return _cur
        return self._extra.setdefault(k, default)

    def __bool__(self) -> bool:
        # ⚠️ 全项目大量 `(self._resp_state or {})` —— 一个回应期**永远是真的**，
        #    否则那些写法会静默退化成空 dict。
        #    📌 兼容层最危险的地方不是缺方法，是**某个魔术方法的默认行为
        #       恰好不同**（空 dict 为假、对象为真）。
        return True

    def __repr__(self) -> str:
        return (f"<ViewSession running={self.running} "
                f"pending={self.pending_epoch} tools={self.tool_count}>")


# ⭐⭐ [2026-08-22] **「这句话是模型自己说的」的唯一标记。**
#
# 🔴 问题：右键「回复」此前挂在**整个聊天区**上（`e.target.closest(CHAT)`），
#    于是系统报错、工具卡、连用户自己刚发的那句，都能「回复」。
#    用户定的是一句话：**「如果这句话是模型自己说的 = 可以回复」。**
#
# 📌 **这是白名单，不是排除法。** 项目自己的判据：
#    排除法的欠账会随时间增长 —— 每加一种新气泡，就多一种「忘了排除」的可能，
#    而漏掉的那种**不会报错**，它会安静地允许一个不该允许的动作。
#    白名单反过来：新加的东西**默认不在名单里**，要进名单得自己举手。
#
# ⚠️ 收成一个函数而不是在 9 处各贴一次 `.classes('nano-said')` ——
#    📌 一个已经存在的形状，第二次出现时该复用它；这里它出现了 **9** 次。
#       更要紧的是：贴 class 那种写法，**第 10 处会忘**，而忘了的表现是
#       「那条 Nano 说的话回复不了」——一个没人会当成 bug 报的静默缺失。
#
# ⚠️ `nano-said` 这个 class **只做标记，不带任何样式** ——
#    📌 一个既管长相又管行为的 class，改长相的人会顺手把行为也改了。
def render_sys_error_card(text: str) -> None:
    """画一张红色 `System Error` 卡。**live 与重放共用这一个实现。**

    ⭐ 抽出来的理由不是省几行：live 那张和重放那张**必须逐像素一样** ——
       📌 同一件事有两个实现，它们只在「我两次想法相同」的前提下一致，
          而这两处相隔四千行。
    ⚠️ 卡里的内容是**错误原文**，不是我们编的文案 ——
       📌 **原文不是文案**：它不需要模型生成，也不归固定文案纪律管。
    """
    with ui.column().classes("w-full px-2 py-1 mb-8"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("error_outline").classes("text-[16px] text-rose-400")
            ui.label("System Error").style(
                "font-size:var(--nano-fs-sm); color:var(--nano-danger); font-weight:600; "
                "letter-spacing:0.03em;")
        with ui.column().classes(
                "w-full theme-card rounded-2xl px-6 py-5").style(
                "background:rgba(var(--nano-danger-rgb),0.05); "
                "border:1px solid rgba(var(--nano-danger-rgb),0.15);"):
            ui.markdown(text or "未知错误").classes(
                "text-[14px] theme-text leading-7")


# ══════════════════════════════════════════════════════════════════════════
# ⭐⭐⭐ 关软件时**还排在队列里没被处理**的那些话。
#
# ═══ 用户定的：呈现，不执行 ═══
#   inbox 的立意：用户的话永不丢
#   关软件那条：关闭软件 = 用户默认放弃这次协同（一律 TERMINAL）
#          ├─ 重新【呈现】→ 两条都满足 ✅  ← 定这个
#          └─ 自动【执行】→ 违反后一条 ❌
#   📌 **「不丢」和「替用户做决定」是两件事** —— 消息还在、看得见，
#      就已经满足「不丢」了（早先的设计用的词本来就是「重新**呈现**」）。
#
# ⚠️ **附件不救**：明说「已失效」，不去 那套图库里捞。
#    📌 一个「看起来还在、点下去才发现没了」的附件，比明说没了更坏。
#
# ⚠️ 卡里那句提示是**固定文案，而且正当**：它是**界面构件**（在说明一件系统事实），
#    不是 Nano 在对你说话 —— 早先定的判据是**文字出现在哪里**。
#    ⭐ 而卡里的**正文是用户自己打的原话**，本来就不该由模型改写。
def render_unsent_user_card(payload: str) -> None:
    """画一张「这条当时没被处理」的卡。**live 与重放共用这一个实现。**

    ⚠️ `payload` 是 JSON（`{"text": ..., "had_image": bool}`），
       解析失败就当纯文本 —— 📌 一张画不出来的卡，比一张信息少一点的卡糟得多。
    """
    import json as _j
    _text, _had_img = payload or "", False
    try:
        _d = _j.loads(payload or "")
        if isinstance(_d, dict):
            _text = str(_d.get("text") or "")
            _had_img = bool(_d.get("had_image"))
    except Exception:
        pass
    with ui.column().classes("w-full px-2 py-1 mb-8"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("schedule_send").classes("text-[16px] text-amber-500/70")
            ui.label("未处理的消息").style(
                "font-size:var(--nano-fs-sm); color:var(--nano-dim); font-weight:600; "
                "letter-spacing:0.03em;")
        with ui.column().classes(
                "w-full theme-card rounded-2xl px-6 py-5 gap-2").style(
                "background:rgba(var(--nano-amber-rgb), 0.04); "
                "border:1px solid rgba(var(--nano-amber-rgb), 0.14);"):
            # ⭐ 原话原样 —— 不 markdown 渲染（它是用户打的字，不是排版内容）
            ui.label(_text or "（空消息）").classes(
                "text-[14px] leading-7 whitespace-pre-wrap min-w-0").style(
                "color:var(--nano-fg); font-family:var(--nano-mono);")
            _note = "上次关闭软件时这条还在排队，没有被处理。"
            if _had_img:
                _note += "随它一起发的附件已失效，需要的话请重新发一次。"
            ui.label(_note).style(
                "font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.6;")



def _cur() -> str:
    """当前厂商的货币符号。⚠️ 不写死 `$` —— 深度求索官方标价是人民币，
    折算需要汇率，而汇率不在官方文档里、每天都在动。"""
    try:
        from core.models import currency_symbol
        return currency_symbol()
    except Exception:
        return "$"

def nano_md(content: str = "", classes: str = "text-[14px] leading-7",
            style: str = "color:var(--nano-fg); min-height:1em;"):
    """建一条**模型正文** markdown，并打上可回复标记。

    ⭐ 判据：这里放的是 **Nano 自己说的话**。
       系统报错、工具卡、状态行、用户消息**都不走这个函数** ——
       它们不是模型说的，所以不该能被「回复」。
    """
    return ui.markdown(content).classes(f"{classes} nano-said").style(style)


NANO_AVATAR_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:100%;height:100%;display:block;">
  <!-- 主十字光芒（竖） -->
  <line x1="12" y1="2.5" x2="12" y2="21.5" stroke="var(--nano-brand-core)" stroke-width="1.2" stroke-linecap="round">
    <animate attributeName="stroke-width" values="0.7;1.7;0.7" dur="2.4s" repeatCount="indefinite"/>
    <animate attributeName="opacity" values="0.4;1;0.4" dur="2.4s" repeatCount="indefinite"/>
  </line>
  <!-- 主十字光芒（横） -->
  <line x1="2.5" y1="12" x2="21.5" y2="12" stroke="var(--nano-brand-core)" stroke-width="1.2" stroke-linecap="round">
    <animate attributeName="stroke-width" values="0.7;1.7;0.7" dur="2.4s" begin="0.6s" repeatCount="indefinite"/>
    <animate attributeName="opacity" values="0.4;1;0.4" dur="2.4s" begin="0.6s" repeatCount="indefinite"/>
  </line>
  <!-- 斜向副芒（较短，较淡） -->
  <line x1="5.8" y1="5.8" x2="18.2" y2="18.2" stroke="var(--nano-brand-glow)" stroke-width="0.7" stroke-linecap="round">
    <animate attributeName="opacity" values="0.12;0.55;0.12" dur="2.4s" begin="0.3s" repeatCount="indefinite"/>
  </line>
  <line x1="18.2" y1="5.8" x2="5.8" y2="18.2" stroke="var(--nano-brand-glow)" stroke-width="0.7" stroke-linecap="round">
    <animate attributeName="opacity" values="0.12;0.55;0.12" dur="2.4s" begin="0.9s" repeatCount="indefinite"/>
  </line>
  <!-- 发光核心 -->
  <circle cx="12" cy="12" r="2.8" fill="var(--nano-brand-core)">
    <animate attributeName="r" values="2.2;3.1;2.2" dur="2.4s" repeatCount="indefinite"/>
    <animate attributeName="opacity" values="0.75;1;0.75" dur="2.4s" repeatCount="indefinite"/>
  </circle>
  <!-- 三颗闪烁星点 -->
  <circle cx="20.5" cy="4" r="1.1" fill="var(--nano-brand-glow)">
    <animate attributeName="opacity" values="0.06;0.95;0.06" dur="2.8s" begin="0.5s" repeatCount="indefinite"/>
  </circle>
  <circle cx="3" cy="7" r="0.85" fill="var(--nano-brand-glow)">
    <animate attributeName="opacity" values="0.06;0.85;0.06" dur="2.2s" begin="1.4s" repeatCount="indefinite"/>
  </circle>
  <circle cx="20" cy="19.5" r="0.85" fill="var(--nano-brand-glow)">
    <animate attributeName="opacity" values="0.06;0.8;0.06" dur="3.1s" begin="0.9s" repeatCount="indefinite"/>
  </circle>
</svg>'''

# 用于 favicon 的更简洁版本（16x16 逻辑）
FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <rect width="32" height="32" rx="8" fill="#1a1d2e"/>
  <polygon points="16,6 22,10 22,18 16,22 10,18 10,10" fill="none" stroke="#e6a94e" stroke-width="1.5"/>
  <circle cx="16" cy="16" r="2.5" fill="#a89f92"/>
</svg>'''


# ── Skill 目录文件变更监听（热重载） ─────────────────────────────────────
class SkillWatcher(FileSystemEventHandler):
    def __init__(self, web_ui):
        self.web_ui = web_ui

    def on_modified(self, event):
        if not event.src_path.endswith(".py"):
            return
        if time.time() < getattr(self.web_ui, "_suppress_skill_watcher_until", 0):
            logger.info(f"✨ 技能代码变更由 SkillWriter 触发，已抑制二次热重载: {event.src_path}")
            return
        logger.info(f"✨ 技能代码变更，标记等待 UI 主上下文刷新: {event.src_path}")
        self.web_ui.request_skill_refresh()

    def on_deleted(self, event):
        # 删除/禁用 Skill 会把 .py 移出 skills/，触发 on_deleted（聊天确认流
        # 删除、disable 移到 skills/disabled 等都走这里）。on_modified 收不到
        # 文件移出事件，边栏不会刷新——补上删除/移动监听。
        if event.is_directory or not str(event.src_path).endswith(".py"):
            return
        logger.info(f"✨ 技能文件删除/移出，标记等待 UI 刷新: {event.src_path}")
        self.web_ui.request_skill_refresh()

    def on_moved(self, event):
        # 文件在监听树内移动会触发 on_moved。
        _src = str(getattr(event, "src_path", ""))
        _dst = str(getattr(event, "dest_path", ""))
        if event.is_directory or not (_src.endswith(".py") or _dst.endswith(".py")):
            return
        logger.info(f"✨ 技能文件移动，标记等待 UI 刷新: {_src} → {_dst}")
        self.web_ui.request_skill_refresh()

    def on_created(self, event):
        # enable（从 skills/disabled 移回 skills/）时文件移入监听目录，
        # 源在监听树外，watchdog 报 on_created 而非 on_moved。
        if event.is_directory or not str(event.src_path).endswith(".py"):
            return
        if time.time() < getattr(self.web_ui, "_suppress_skill_watcher_until", 0):
            return
        logger.info(f"✨ 技能文件新增/启用，标记等待 UI 刷新: {event.src_path}")
        self.web_ui.request_skill_refresh()


# 流式输出末尾的终端闪烁光标（答完由 final_result 的干净 set_content 覆盖清掉）
_STREAM_CURSOR = ' <span class="nano-cursor">▋</span>'


# ══════════════════════════════════════════════════════════════════════════
# ⭐⭐⭐ 聊天流事件的「重启后还在不在」三分类
# ══════════════════════════════════════════════════════════════════════════
#
# ═══ 为什么需要这张表（2026-08-13 实测，用户）═══
#
# 🔴 Nano 画了一张流程图，重启后**图没了，描述那张图的话还在**。
#    根因：`visual_render` 的内容明明逐字落在工具 args 里，
#    但 `_replay_durable_conversation` 是**手写的投影**，它没读那一格。
#
# 用户当场问出了比这个 bug 大得多的那一句：
#   > 「应该不会有其他部分有类似的问题吧？比如输出某些东西然后重启后不存在，
#   >   **或者未来新加某些输出内容（类型）然后重启后不存在** —— 这种 bug 也挺隐蔽的」
#
# ⭐ 这就是问题的真实形状：**直播链是一长串 `if event == X`，重放链是另一段手写代码，
#    两条链之间没有任何东西强迫它们对齐。** 加一个新事件永远只需要改直播那条 ——
#    重放那条不会报错、不会警告，只会安静地少画一样东西，
#    而且**要等到有人重启并且恰好回看那一段才会发现**。
#
# 📌 所以修的不只是那一张图，而是把「每个上屏事件重启后还在不在」变成一个
#    **必须显式回答的问题** —— `tests/t_replay_chat_events.py` 从 `app.py` 的
#    真实分发链里数出所有事件名，凡是没被下面四张表之一认领的，测试当场红。
#    📌 同 `_UI_TERMINAL_EVENTS` 那条判据：
#       **常量表必须能被证明等于真实分发链，而不是靠人记得同步。**
#
# ⚠️ 这张表**不产生任何运行时行为**，它是一份声明。真正画画的是各分支自己。
#    这是刻意的：让它去驱动渲染就变成了第二个权威，而 的根因正是那个。

# 进聊天流，且重启后能从落盘记录**完整重建**。
_CHAT_EVENTS_REPLAYED = frozenset({
    "final_text_start", "final_text_delta", "final_text",
    "text_replace", "final_result",          # → conversation_messages 的 assistant 正文
    "tool_start", "tool_end",                # → tool_calls / tool_results（工具 pill）
    "skill_preview",                         # → SKILL_AUDIT Interaction（`_CARD_KINDS`）
    "visual_render",                         # → 工具 args 里的 html/title（本次修的那一张）
})

# 进聊天流，但重启后**确实不在** —— 已知、刻意、有理由。
_CHAT_EVENTS_EPHEMERAL = frozenset({
    # ⭐ Nano 自己截的屏。**已明确定的（2026-08-13）**，理由比"base64 太大"好得多：
    #   > 「这些截图更多是当 nano 截图时让用户看到**『它看到了什么』的即时信息**，
    #   >   **重启不保留刚好是这些信息在 UI 被抛弃的出口**；
    #   >   保留反而会让上下文 UI 里多出很多对用户的视觉杂音」
    # 📌 **即时信息需要一个退出口，而重启就是那个出口** —— 这条是"该消失"，
    #    不是"没来得及做持久化"。⚠️ 与**用户自己发的图**方向相反：
    #    那个是「我曾经发过什么」，属于用户的历史，**必须留**（见 步 2）。
    "screenshot_preview",
    # 「本轮被打断」的那一行。⚠️ 中断这件事本身由后续消息体现，
    # 而**插话那一支连已说出的文字都要擦掉**（见该分支的长注释）——
    # 它要求的正是"不留下"，所以重放不画它才是忠实的。
    "turn_interrupted",
    # 运行期错误提示行。它描述的是**上一次进程里的故障**，
    # 重启后原样重贴等于把一条已经不成立的告警说成现在的状态。
    "sys_error",
})

# 交互卡：**存活与否由 Interaction 协议（mode × durability）决定，不由这张表决定。**
# ⚠️ 别把它们搬进上面两张表 —— 那会造出第二个权威，而协议才是权威。
_CHAT_EVENTS_INTERACTION = frozenset({
    "user_choice_request", "execution_confirm", "os_action_confirm", "confirm_dismiss",
    "mini_auth_request", "mcp_auth_required",
    # ⭐ 接入一个新 MCP 的授权 —— 与 `mcp_auth_required`（**MCP 要求认证**）
    #    不是一回事：那个是「它让你去登录」，这个是「要不要把它装进来」。
    # ⚠️ 它是**同步等待**的（`inbox.wait_confirm_or_user_message`，300s），
    #    重启后那次等待本来就没了 —— 交互卡的存活由 Interaction 协议决定，
    #    不由这张表决定（见上面那句注释）。
    "mcp_connect_confirm",
})

# 根本不往聊天流写（弹窗 / 右侧面板 / 状态行 / 直接 continue）。
_CHAT_EVENTS_NOT_IN_CHAT = frozenset({
    "thought_block_start", "thought_block_done", "thought_summary", "thought_delta",
    "final_text_discard",                    # 统一文字流里不撤销，纯 continue
    "skill_code_start", "skill_code_delta",  # 流式代码 → 审计弹窗
    "task_list_update", "task_step_update",  # → 右侧计划面板
    "window_mode", "suspend_waiting", "long_task_handback",
    "carrier_detached",                      # → 抽屉 / pill，不写聊天流
    "user_note_pending",                     # 状态行
})

# `render_visual` 的工具名。⚠️ 单独拎出来是为了让测试能证明它**还在工具目录里** ——
# 改了名而重放这边没跟着改，表现正好是这次这个 bug（安静地少画一张图）。
_REPLAYABLE_VISUAL_TOOL = "render_visual"


# ── WebUI ────────────────────────────────────────────────────────────────
class WebUI:
    def __init__(self):
        # 聊天原文的权威在 Runtime SQLite；MemoryManager 只是当前会话、
        # 受 max_turns 约束的上下文投影。重启/刷新继续同一 session，只有显式
        # 「重置对话」才会在 Orchestrator 那边轮换它。
        from core.runtime import get_kernel as _rt_get_kernel
        from core.runtime.conversation import ConversationRepository
        self.memory = MemoryManager(
            max_turns=10,
            conversation_repository=ConversationRepository(_rt_get_kernel().store),
        )
        # Provider 必须在主进程创建 WebUI 时初始化，不能放在模块顶层。
        # Windows spawn 子进程 import app.py 时只需要读取 native 窗口参数，
        # 不应再次创建 ClaudeProvider / 中转连接 / 模型配置。
        # 走 get_provider 拿全进程唯一实例，不再自己 new 一个——
        # 否则 self.provider.reconfigure() 只重建这一个，core/rag.py 三处多模态
        # 用的是模块级那个，用户改完 key 后 RAG 还在用旧凭据直到重启。
        self.provider = get_provider()
        self.agent  = Orchestrator(self.provider, registry, self.memory)
        self.agent._push_callback = self._proactive_push
        # 🔴 衰减发生在**本轮 `final_result` 之后**（orchestrator 的 finally 里），
        #    所以 UI 不能在 `final_result` 那一刻去问"有没有东西被移出去" —— 那时还没有。
        #    2026-08-14 实测：模型侧 3 段已经移出、聊天区**一个字没变**，
        #    正是说的那个 UI 说谎，只是原因比它猜的更靠前。
        # 📌 **一个"事后才发生"的事实，不能用"事前的那个事件"去刷新** ——
        #    要么等它自己说一声，要么就永远慢一拍。这里选前者。
        self.agent._on_decay_applied = self._sync_evicted_after_turn
        self._speaker = ProactiveSpeaker(self.provider, self._proactive_push)
        # 主动智能 v0：默认 SHADOW（只决策记日志、不真说话），与旧 speaker 并存零冲突。
        # 复核 data/proactive_shadow.jsonl 后，把 engine.SHADOW_MODE 改 False 即上线，
        # 同时停掉上面旧 _speaker 的轮询（见 _maybe_speak 注释）。
        self._intel_engine = _IntelEngine(self.provider, self._proactive_push)
        self._activity = _get_activity_buffer()

        self.scroll_area    = None
        self.chat_container = None
        self.input_field    = None
        self.choice_card_slot = None   # 选择卡片挂载点（输入框上方）

        # ── 执行时自缩窗（模型驱动：Nano 调 set_window_mode 工具）──────────
        self._mini_active    = False   # 当前是否处于 mini（缩窗）形态
        self._mini_orig_geom = None    # 缩窗前的 (w,h,x,y)，恢复用
        self._mini_hint      = None    # 输入框下方提示胶囊（缩窗期可见）

        # ── auto 模式（免逐动作确认）──────────────────────────────────────
        # global_auto：持久全局开关（输入框下方 Auto chip 切换，存 data/os_state.json）。
        # temp_auto：本次屏幕任务临时授权，生命周期 = mini（缩窗前弹一次"同意/拒绝"，
        #   mini 关即结束）。任一为真 → 所有 OS 确认弹窗自动通过（含 risk=3），
        #   消除"中途弹窗偷焦点"。急停 Ctrl+` / 甩角 failsafe 仍生效。
        self._global_auto = self._load_global_auto()
        self._temp_auto   = False
        # durable inbox 的内存侧：item_id → pipeline 参数（含 UI 句柄）
        # ⚠️ **库负责「不丢」，这个 dict 负责「接得上」** ——
        #    UI 句柄和图片字节只在本进程有意义，落库也没用。
        self._rt_inbox_parked: dict = {}
        self._rt_inbox_running_id = None
        self._auto_chip   = None
        self._auto_label  = None
        self._auto_menu   = None
        self._auto_menu_box = None
        self._effort_chip = None
        self._effort_label = None
        self._effort_menu = None
        self._effort_menu_box = None
        self._identity_label = None
        self._relay_badge_label = None
        self._mini_bar       = None    # 右上角小胶囊（光芒头像 + 计时器，无按钮）
        self._mini_timer_lbl = None
        self._mini_start_time = 0.0
        self._mini_timer_task = None
        self.status_lbl     = None
        self.model_lbl      = None
        self.net_lbl        = None   # 现专给"互联网检索"卡片（MCP 联网能力），见 _update_net_status
        self.net_dot        = None
        self._net_crashed   = False  # 后端崩溃标志（原先借 net_lbl.text=="CRASH" 表达，已独立）
        self.log_lbl        = None
        self.rag_lbl        = None
        self.full_file_lbl  = None 

        self._cost_warning_bar  = None
        # 接管状态条。⚠️ 必须在 __init__ 里初始化：`render` 之前就有
        # 1 秒 timer 可能跑到（`_refresh_takeover_bar` 有 None 保护，但别依赖属性不存在）。
        self._takeover_bar      = None
        self._takeover_lbl      = None
        self._cost_warning_lbl  = None
        self._bg_tasks: dict        = {}
        # ⭐ 轮外 UI 事件通道。**由 app 建、由 orchestrator 写、由 app 读** ——
        #    📌 建在 app 是因为消费它的是 UI；而它必须在 orchestrator 拿得到的
        #       地方（`agent._ui_oob_events`），否则Subagent发不出去。
        self._oob_events: "asyncio.Queue | None" = None
        self.drawer         = None
        # 之前是 3 个独立的 ui.right_drawer 抢同一个布局槽位——怀疑（也是
        # 目前最合理的解释）NiceGUI/Quasar 的 q-layout 假设一侧只有一个
        # drawer，多个右抽屉在 hide()/toggle() 切换时，布局的
        # padding-right 计算会出现顺序依赖，导致"先开监控再切别的"留白
        # 跟丢。改成只挂一个 right_drawer，里面放三个内容面板切换
        # 可见性，从根上消除"多个 drawer 抢布局槽位"这个前提。
        self.right_drawer   = None
        self.monitor_panel  = None
        self.kb_panel       = None
        self.memory_panel   = None
        self.tasks_panel    = None          # 后台任务抽屉
        self._tasks_body    = None
        self.agent_panel    = None      # Subagent监控抽屉（无并列按钮）
        self._agent_body    = None
        self._agent_watch   = ""        # 当前盯住哪个Subagent
        self._nav_tasks_row = None
        self._tasks_badge_label = None
        self._task_pill     = None          # 聊天区那个 `x running task(s)`
        self._task_pill_lbl = None
        self._bg_finished_hidden_before = 0.0   # Clear 只隐藏，不删记录
        self.plan_panel     = None
        self._nav_kb_row      = None
        self._nav_monitor_row = None
        self._nav_memory_row  = None
        self._nav_plan_row    = None
        # 任务列表状态（当前会话）
        self._task_state: dict | None = None       # {"title": ..., "steps": [...]}
        self._plan_title_label = None              # plan_panel 里的标题 label
        self._plan_steps_container = None          # plan_panel 里的步骤容器

        # user_note 确认 UI
        self._pending_notes: list = []          # [{note_id, display_text, ts}]
        self._memory_badge_label = None         # 右上角红点数字
        self._pending_cards_container = None    # 待确认卡片容器

        self.skill_ui_elements      = {}
        self.skill_list_container   = None
        self.kb_file_list_container = None
        self.kb_stats_lbl           = None
        self._kb_health_lbl         = None
        self._model_select          = None
        self._star_btn              = None
        self._current_loading_label = None
        self._empty_state_greeting  = None
        # 当前回复的实时状态（跨事件共享，pipeline_lock 保证单线程访问）
        self._resp_state: dict = {}

        # 主题：'aurora'（浅色，默认）/ 'terminal'（终端风，暖黑）
        # 🔴 必须赋在 `_load_app_config()` 【之前】—— 它是"没有配置时用什么"，
        #    赋在后面就会无条件覆盖掉刚读出来的用户选择（2026-08-30 踩过）。
        # ⚠️ 默认取浅色：用户「终端风为了模仿终端字体，可读性一般般、更风格化，
        #    不适合当默认」。内部标识保持 'aurora' 不改 —— 它已经写进用户配置了。
        self.theme_mode    = 'aurora'

        # ⚠️ 下面两个和上面的 theme_mode 是**同一组**：凡是 `_load_app_config()`
        #    会读、`_save_app_config()` 会写的属性，默认值都必须赋在这里。
        #    🔴 2026-08-31：它们原本赋在 __init__ 靠后的位置（约 1126 行），
        #       于是干净机器上第一次启动就炸 ——
        #       `_save_app_config()` 读 `self._enhanced_mode`，而它还不存在。
        #       开发机上不炸，是因为 `data/` 里已有配置，`_load_app_config()`
        #       顺手把它赋上了 —— **一个错误的顺序被已有的用户数据遮住**。
        #    📌 加新的这类属性时加进这一组，不要加在 __init__ 别处。
        self._enhanced_mode: bool = False   # 入库时是否识别嵌入图片
        self._ocr_max_pages: int  = 50      # OCR 页数上限

        self._load_app_config()
        self._save_app_config()  # 立即写入当前状态

        self.pipeline_lock = asyncio.Lock()
        self._ui_client    = None
        self._suppress_skill_watcher_until = 0.0
        self._skill_refresh_requested = False

        # ── 统一出口的就绪状态 ──────────────────────────────────────────
        # _ui_ready 在 render() 结尾置 True。在此之前发生的事件【入队不丢弃】——
        # 这是为了消灭 `if not container: return` 这种永久丢事件的路径。
        # 典型受害场景：RAG 初始化线程在 WebUI() 构造时就启动了，比 ui.run() 还早。
        self._ui_ready: bool = False
        self._pending_chat_events: list = []
        self._emitted_chat_keys: set = set()   # 按 dedupe_key 保证一个事件只渲染一次

        # 对话窗口图片上传状态（直接进context）
        self._pending_image_bytes: bytes | None = None
        self._pending_image_mime: str = "image/jpeg"
        self._image_preview_container = None
        # 选中引用的出口条（composer 上方那一行）
        self._quote_bar = None
        self._quote_bar_text = None

        # 对话窗口文件上传状态（临时RAG）
        self._temp_files: list = []          # [{filename, chunks}]
        self._temp_file_badge = None         # 显示已上传文件数量的badge
        self._chat_upload = None             # upload 组件引用，重置时 reset 用

        # 知识库入库进度追踪：正在入库的文件名集合，刷新列表时显示 spinner 行
        self._kb_indexing_files: set = set()

        # 增强模式 / OCR 页数上限的默认值见 __init__ 开头那一组
        self._koala_current_skill = None
        self._current_query: str = ""  # episodic: 本轮用户输入，final_result 时写摘要用
        # 流式 Skill 代码审计对话框状态（skill_code_start/delta/preview 三步协议）
        self._sk_stream_dialog = None
        self._sk_stream_code_area = None
        self._sk_stream_code_holder = None
        self._sk_stream_validation_lbl = None
        self._sk_stream_apply_btn = None
        self._sk_stream_badge_icon_el = None
        self._sk_stream_code_lbl = None
        self._sk_stream_title_lbl = None
        self._sk_stream_desc_lbl = None
        self._sk_stream_cm_state = None
        self._sk_stream_accumulated = ""

    # ── 技能列表刷新 ──────────────────────────────────────────────────────

    def refresh_skill_list(self):
        if not self.skill_list_container:
            return
        self.skill_list_container.clear()
        self.skill_ui_elements.clear()

        with self.skill_list_container:
            if not registry.skills:
                with ui.row().classes('px-3 py-3 items-center gap-2'):
                    ui.icon('inbox').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                    ui.label('暂无已注册工具').classes('text-[12px]').style('color:var(--nano-fg) !important;')

            def _render_skill_row(name: str):
                # 原来是4个并排图标按钮（信息/源码/暂停/删除），行内噪音大；
                # 收成一个"更多操作"(⋯)菜单，跟设置/外观那两个菜单同一套
                # 视觉语言，只在需要时展开。
                is_official = registry.is_official_skill(name)
                is_os = registry.is_os_skill(name)
                with ui.row().classes('w-full items-center justify-between px-2 py-2 rounded-xl hover:bg-white/[0.04] transition-all group no-wrap'):
                    with ui.row().classes('items-center gap-2 flex-1 min-w-0 no-wrap'):
                        # READY 状态目前永远是唯一状态，圆点统一用绿色表示"就绪"。
                        dot_color = 'var(--nano-ok)'
                        sk_icon = ui.element('div').style(
                            f'width:7px; height:7px; border-radius:50%; background:{dot_color}; flex-shrink:0; transition:background 0.3s;'
                        )
                        sk_lbl = ui.label(name).classes('text-[13px] text-slate-300 theme-text flex-1 min-w-0').style(
                            'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
                        )
                        # 官方/OS 标识——纯展示用的小徽标，不参与任何权限判断。
                        if is_official:
                            ui.label('官方').style(
                                'font-size:var(--nano-fs-3xs); font-weight:700; color:var(--nano-amber); '
                                'background:rgba(var(--nano-info-rgb),0.1); padding:1px 5px; '
                                'border-radius:999px; flex-shrink:0; letter-spacing:0.02em;'
                            )
                        if is_os:
                            ui.label('OS').style(
                                'font-size:var(--nano-fs-3xs); font-weight:700; color:var(--nano-ok); '
                                'background:rgba(var(--nano-ok-rgb),0.1); padding:1px 5px; '
                                'border-radius:999px; flex-shrink:0; letter-spacing:0.02em;'
                            )
                    with ui.row().classes('items-center gap-1 flex-shrink-0 no-wrap'):
                        sk_status = ui.label('READY').style(
                            'font-size:var(--nano-fs-xs); line-height:1; color:var(--nano-ok); letter-spacing:0.04em; '
                            'margin-right:2px;'
                        )
                        with ui.element('div'):
                            with ui.button(icon='more_vert').props('flat round dense size=sm') \
                                    .style('color:var(--nano-fg) !important;'):
                                with ui.menu().props('anchor="bottom left" self="top left"').classes('q-pa-none') as skill_menu:
                                    with ui.column().style(
                                        'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                                        'box-shadow:0 4px 16px rgba(var(--nano-shade-rgb), 0.08); '
                                        'border-radius:10px; min-width:136px; padding:5px; gap:1px;'
                                    ):
                                        def _skill_menu_item(icon, label, on_click, accent='var(--nano-fg-soft)'):
                                            def _go():
                                                skill_menu.close()
                                                on_click()
                                            with ui.row().classes('items-center gap-1.5 w-full cursor-pointer hover:bg-white/5 transition-colors') \
                                                    .style('padding:5.5px 8px; border-radius:7px;') \
                                                    .on('click', _go):
                                                ui.icon(icon).style(f'font-size:var(--nano-fs-base); color:{accent}; flex-shrink:0;')
                                                ui.label(label).style('font-size:var(--nano-fs-xs); color:var(--nano-fg);')

                                        _skill_menu_item('info_outline', '查看说明',
                                                          lambda n=name: self._show_skill_info_dialog(n), accent='var(--nano-ok)')
                                        _skill_menu_item('visibility', '查看源码',
                                                          lambda n=name: self._show_skill_source_dialog(n), accent='var(--nano-fg-soft)')
                                        ui.separator().style('background:rgba(var(--nano-contrast-rgb), 0.06); margin:2px 4px;')
                                        _skill_menu_item('pause_circle', '禁用',
                                                          lambda n=name: self._confirm_disable_skill(n), accent='var(--nano-warn)')
                                        _skill_menu_item('delete_outline', '删除',
                                                          lambda n=name: self._confirm_delete_skill(n), accent='var(--nano-danger)')
                self.skill_ui_elements[name] = {"icon": sk_icon, "label": sk_lbl, "status": sk_status}

            def _show_all_skills_dialog(names: list):
                """"查看全部工具"——永久 Skill 列表只在侧边栏内联显示最近
                若干个，全部在这个弹窗里看，复用 _render_skill_row（闭包，
                跟知识库那次"提取成独立方法复用"思路一样，只是这里直接用
                闭包更省事，不用额外传一堆状态）。"""
                with self._ui_scope():
                    with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                         ui.card().style(
                             'width:340px; max-height:80vh; padding:0; overflow:hidden; '
                             'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                             'box-shadow:0 8px 28px rgba(var(--nano-shade-rgb), 0.12); border-radius:16px;'
                         ):
                        with ui.row().style(
                            'width:100%; align-items:center; justify-content:space-between; '
                            'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                            'background:var(--nano-panel);'
                        ):
                            # names 这里只是"溢出"那部分（不含侧边栏内联
                            # 显示的那几个），标题别说"全部"，免得数字对不上。
                            ui.label(f'其余工具（{len(names)} 个）').style(
                                'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);'
                            )
                            ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;')

                        with ui.element('div').style(
                            'width:100%; max-height:calc(80vh - 64px); overflow-y:auto; padding:6px 8px;'
                        ):
                            for n in names:
                                _render_skill_row(n)
                    dialog.open()

            # 侧边栏空间有限，只内联显示最近的若干个，
            # 超出的塞进"查看全部工具"弹窗，跟知识库文件列表同一个思路。
            permanent_names = list(registry.skills)
            SKILL_INLINE_LIMIT = 6
            for name in permanent_names[:SKILL_INLINE_LIMIT]:
                _render_skill_row(name)
            if len(permanent_names) > SKILL_INLINE_LIMIT:
                # 弹窗只放"溢出"的那部分，不包含侧边栏已经内联显示的那几个——
                # 避免同一个 Skill 名字被渲染两遍，导致 self.skill_ui_elements
                # 这个状态追踪字典被弹窗里的元素覆盖，弹窗关掉后侧边栏那几个
                # 的实时状态更新（执行中变 spinner 等）就指向已销毁的元素了。
                ui.button(
                    f'查看全部工具（{len(permanent_names)}）',
                    on_click=lambda ns=permanent_names[SKILL_INLINE_LIMIT:]: _show_all_skills_dialog(ns)
                ).props('flat dense').style(
                    'width:100%; font-size:var(--nano-fs-sm); color:var(--nano-fg-mute) !important; margin-top:2px;'
                )

            disabled_skills = registry.list_disabled_skills() if hasattr(registry, 'list_disabled_skills') else []
            if disabled_skills:
                ui.separator().classes('opacity-10 my-2')
                with ui.row().classes('px-2 items-center gap-2 mb-1'):
                    ui.label('DISABLED').style('font-size:var(--nano-fs-2xs); color:var(--nano-dim); letter-spacing:0.08em; ')
                for name in disabled_skills:
                    with ui.row().classes('w-full items-center justify-between px-2 py-2 rounded-xl hover:bg-white/[0.03] transition-all opacity-50'):
                        with ui.row().classes('items-center gap-2 flex-1 min-w-0'):
                            ui.element('div').style(
                                'width:7px; height:7px; border-radius:50%; background:var(--nano-fg); flex-shrink:0; border:1px solid var(--nano-dim);'
                            )
                            ui.label(name).classes('text-[12px] text-slate-500 truncate')
                        ui.button(icon='play_circle', on_click=lambda n=name: self._enable_skill(n)) \
                            .props('flat round dense size=sm').classes('text-slate-600 hover:text-emerald-400 transition-colors')

            # 虚拟节点
            self.skill_ui_elements["WebSearch"] = {
                "icon": ui.element('div').classes('hidden'),
                "label": ui.label('').classes('hidden'),
                "status": ui.label('').classes('hidden')
            }
            self.skill_ui_elements["google_search"] = self.skill_ui_elements["WebSearch"]
            # ⚠️ 新的搜索 Skill 叫 SearchTheWeb（**不能叫 WebSearch**，
            #    撞名会让模型发空参数，见该 Skill 文件头）。挂同一个虚拟节点。
            self.skill_ui_elements["SearchTheWeb"] = self.skill_ui_elements["WebSearch"]
            self.skill_ui_elements["SkillWriter"] = {
                "icon": ui.element('div').classes('hidden'),
                "label": ui.label('').classes('hidden'),
                "status": ui.label('').classes('hidden')
            }

    def _set_skill_dot_color(self, dot_el, color_hex: str):
        """动态更新技能状态圆点颜色"""
        dot_el.style(f'width:7px; height:7px; border-radius:50%; background:{color_hex}; flex-shrink:0; transition:background 0.3s;')

    def _show_skill_info_dialog(self, skill_name: str):
        """Item6：Skill 名字一般是英文(模型起的)，用户看不出是干什么的。
        这个弹窗展示 description / purpose / 不负责的范围，
        帮用户快速判断"这个工具是做什么的、能不能解决我的问题"，
        不用打开源码也不用先调用试一次。
        """
        skill_obj = registry.skills.get(skill_name)
        if skill_obj is None:
            ui.notify(f'未找到 Skill: {skill_name}', type='warning')
            return
        try:
            manifest = skill_obj.get_manifest() or {}
        except Exception:
            manifest = {}
        description = manifest.get('description', '') or '（未提供描述）'

        purpose = ''
        not_responsible_for: list = []
        try:
            spec = skill_obj.get_spec()
            purpose = getattr(spec, 'purpose', '') or ''
            not_responsible_for = list(getattr(spec, 'not_responsible_for', None) or [])
        except Exception:
            pass

        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:440px; max-width:94vw; background:var(--nano-panel); '
                     'border:1px solid rgba(var(--nano-amber-rgb), 0.2); border-radius:16px; padding:0; overflow:hidden;'
                 ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                    'background:var(--nano-panel-2); border-radius:16px 16px 0 0;'
                ):
                    with ui.row().classes('items-center gap-3 min-w-0'):
                        ui.icon('info_outline').style('font-size:var(--nano-fs-4xl); color:var(--nano-ok); flex-shrink:0;')
                        ui.label(skill_name).classes('truncate').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg); ')
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').classes('text-slate-500 flex-shrink-0')

                with ui.column().style('width:100%; padding:18px 20px; gap:12px;'):
                    # ⭐⭐ [2026-08-28] **「作用」不再显示给用户** —— 它是
                    #    `manifest["description"]`，也就是**注入模型**的那一份
                    #    （→ `ToolDefinition.awareness` / tool schema）。
                    # 📌 两个受众的需求不同，硬凑一份就两头不讨好：
                    #      模型侧要**英文 + 触发条件**（省 token、避免中英混排干扰匹配）
                    #      用户侧要**母语 + 说人话**
                    #    ⇒ 用户侧只留 `purpose`（`SkillSpec.purpose`，本来就是给人看的）。
                    # ⚠️ 于是那句 `if purpose != description` 的去重也一并删掉 ——
                    #    它存在的前提是「两者可能重复」，而现在它们连语言都不同了。
                    if purpose:
                        with ui.column().style('gap:4px;'):
                            ui.label('用途说明').style(
                                'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                            )
                            ui.label(purpose).style('font-size:var(--nano-fs-md); color:var(--nano-fg-soft); line-height:1.6; white-space:normal;')

                    if not_responsible_for:
                        with ui.column().style('gap:4px;'):
                            ui.label('不负责').style(
                                'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                            )
                            for item in not_responsible_for:
                                with ui.row().classes('items-start gap-2'):
                                    ui.label('·').style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft);')
                                    ui.label(str(item)).style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft); line-height:1.6; white-space:normal;')

                    ui.separator().classes('opacity-10')
                    ui.label(f'调用方式：直接用自然语言描述需求，或明确说「调用 {skill_name}」。').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.6;'
                    )

            dialog.open()

    def _show_skill_source_dialog(self, skill_name: str):
        source = registry.get_skill_source(skill_name, include_disabled=True) if hasattr(registry, 'get_skill_source') else None
        if not source:
            ui.notify(f'未找到 Skill: {skill_name}', type='warning')
            return
        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style('width:900px; max-width:96vw; background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.2); border-radius:16px; padding:0;'):
                with ui.row().style('width:100%; align-items:center; justify-content:space-between; padding:16px 24px; border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06); background:var(--nano-panel-2); border-radius:16px 16px 0 0;'):
                    with ui.row().classes('items-center gap-3'):
                        ui.html(NANO_AVATAR_SVG).style('width:28px; height:28px;')
                        ui.label(f'源码查看 — {skill_name}.py').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg-soft);')
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').classes('text-slate-500')
                # 三个"看代码"的地方共用同一个 CodeMirror 宿主：临时代码执行 /
                # Skill 创建流式 / 这里。⚠️ 原来这里是 `ui.textarea(... dark ...)`，
                # dark 写死 ⇒ 浅色主题下一块深色文本框贴在白页面上，
                # 而且三处长得不一样，改配色要改三遍。
                # padding 必须在【外层】—— `.nano-cm-host` 自带 border+background，
                # padding 写在它身上会让代码从自己的边框往里缩出一道空隙。
                # `nano-cm-readonly` 不能漏：它关掉"当前行高亮"，而这里是改不了的
                # 预览 —— 一条跟着点击走的高亮条会让人以为自己能编辑。
                with ui.column().style('width:100%; padding:0 24px 16px; gap:0;'):
                    _src_area = ui.element('div').classes(
                        'nano-cm-host nano-cm-readonly').style('width:100%;')
            dialog.open()

            async def _mount_src_cm():
                await asyncio.sleep(0.15)      # 等 dialog DOM 挂上
                await self._cm_init(_src_area, source.get('code', ''), readonly=True)
            asyncio.create_task(_mount_src_cm())

    def _confirm_disable_skill(self, skill_name: str):
        with self._ui_scope():
            with ui.dialog() as dialog, ui.card().style('background:var(--nano-panel); border:1px solid rgba(var(--nano-warn-rgb),0.2); border-radius:16px; min-width:380px; padding:24px;'):
                with ui.row().classes('items-center gap-3 mb-3'):
                    ui.icon('pause_circle').classes('text-amber-400 text-[20px]')
                    ui.label(f'禁用 Skill「{skill_name}」').classes('text-slate-200 text-[14px] font-medium')
                ui.label('文件将移动到 skills/disabled/，之后可恢复。').classes('text-slate-500 text-[12px] mb-4')
                with ui.row().classes('justify-end w-full gap-2'):
                    ui.button('取消', on_click=dialog.close).props('flat').classes('text-slate-400')
                    ui.button('确认禁用', on_click=lambda: self._disable_skill(skill_name, dialog)).props('unelevated color=warning')
            dialog.open()

    def _confirm_delete_skill(self, skill_name: str):
        with self._ui_scope():
            with ui.dialog() as dialog, ui.card().style('background:var(--nano-panel); border:1px solid rgba(var(--nano-danger-rgb),0.2); border-radius:16px; min-width:380px; padding:24px;'):
                with ui.row().classes('items-center gap-3 mb-3'):
                    ui.icon('delete_outline').classes('text-rose-400 text-[20px]')
                    ui.label(f'删除 Skill「{skill_name}」').classes('text-rose-300 text-[14px] font-medium')
                ui.label('将移动到 skills/deleted/ 备份目录，并从当前工具链移除。').classes('text-slate-500 text-[12px] mb-4')
                with ui.row().classes('justify-end w-full gap-2'):
                    ui.button('取消', on_click=dialog.close).props('flat').classes('text-slate-400')
                    ui.button('确认删除', on_click=lambda: self._delete_skill(skill_name, dialog)).props('unelevated color=negative')
            dialog.open()

    def _disable_skill(self, skill_name: str, dialog):
        result = registry.disable_skill(skill_name) if hasattr(registry, 'disable_skill') else {"ok": False, "msg": "当前 Registry 不支持禁用"}
        dialog.close()
        ui.notify(result.get('msg', '已处理'), type='positive' if result.get('ok') else 'negative')
        self.refresh_skill_list()
        if result.get('ok'):
            _msg = result.get('msg') or f'Skill "{skill_name}" has been disabled'
            self.agent.memory.add_system_note(
                "assistant",
                f'[System record: the user disabled Skill "{skill_name}" from the UI sidebar; this was not executed in the current chat.] {_msg}'
            )

    def _delete_skill(self, skill_name: str, dialog):
        result = registry.delete_skill_file(skill_name) if hasattr(registry, 'delete_skill_file') else {"ok": False, "msg": "当前 Registry 不支持删除"}
        dialog.close()
        ui.notify(result.get('msg', '已处理'), type='positive' if result.get('ok') else 'negative')
        self.refresh_skill_list()
        # 侧边栏直接点删除，绕过了聊天里的 SKILL_DELETE 确认流程，之前完全不写
        # memory——Nano 后续被问起这个 Skill 时毫无所知，不是"忘了"，是从没被
        # 告知过。和聊天内删除一样写一条 assistant 记录，供后续统一的
        # "Skill 没找到"事实生成复用。
        # 注意措辞：role="assistant" 会让模型把这条记录读成"我自己说/做的"，
        # 导致被追问"为什么"时编出"我执行了删除操作"这种第一人称幻觉——
        # 这条记录其实是用户在UI侧边栏点的，不是Nano在对话里做的，必须显式
        # 标注来源，不能让模型误认为是自己的动作。
        if result.get('ok'):
            _msg = result.get('msg') or f'Skill "{skill_name}" has been deleted'
            self.agent.memory.add_system_note(
                "assistant",
                f'[System record: the user deleted Skill "{skill_name}" from the UI sidebar; this was not executed in the current chat.] {_msg}'
            )

    def _enable_skill(self, skill_name: str):
        result = registry.enable_skill(skill_name) if hasattr(registry, 'enable_skill') else {"ok": False, "msg": "当前 Registry 不支持启用"}
        ui.notify(result.get('msg', '已处理'), type='positive' if result.get('ok') else 'negative')
        self.refresh_skill_list()
        if result.get('ok'):
            _msg = result.get('msg') or f'Skill "{skill_name}" has been enabled'
            self.agent.memory.add_system_note(
                "assistant",
                f'[System record: the user enabled Skill "{skill_name}" from the UI sidebar; this was not executed in the current chat.] {_msg}'
            )

    def _ui_scope(self):
        return self._ui_client if self._ui_client is not None else nullcontext()

    def _js_fire(self, js: str) -> None:
        """auto-index 安全的 JS 推送：只发送，不等待浏览器返回。"""
        client = self._ui_client
        if not client:
            return
        try:
            with self._ui_scope():
                try:
                    result = client.run_javascript(js)
                except TypeError:
                    result = client.run_javascript(js, respond=False)
                import inspect
                if inspect.iscoroutine(result):
                    asyncio.create_task(result)
        except Exception as e:
            logger.warning(f"[UI] fire-and-forget JS 推送失败: {e}")

    async def _cm_init(self, mount_el, value: str = "", readonly: bool = True):
        """初始化 CodeMirror。fire-and-forget，auto-index 安全。"""
        if not mount_el:
            return
        js = f"""
        (async () => {{
            const mountId = {mount_el.id};
            for (let i = 0; i < 100; i++) {{
                if (window.NanoCM && window.NanoCM.init) break;
                await new Promise(r => setTimeout(r, 50));
            }}
            if (!window.NanoCM || !window.NanoCM.init) {{
                console.error('[NanoCM] runtime not ready');
                return;
            }}
            try {{
                await window.NanoCM.init(mountId, {{
                    doc: {json.dumps(value or '')},
                    readOnly: {str(readonly).lower()},
                    // ⭐ [2026-08-06 实测] syncToPython 必须跟着"可不可编辑"走。
                    //
                    // 原来这里**硬编码 false**，只有 `_cm_set_editable(True)` 会打开它，
                    // 而那个只在【流式】那条路被调过。后果：
                    //   · 非流式审计弹窗（`_show_skill_preview`）里改代码 → 改动从不回传
                    //   · 最小化再展开（`_restore_cm` 重新 init）→ 之后改也不回传
                    // 于是用户改完一半最小化去问 Nano 问题，回来发现代码被还原成原始版本，
                    // **而且没有任何提示**。实测撞到：这不只影响那次测试，
                    // 它意味着"编辑器里的修改必须一次做完"。
                    //
                    // 而且这还让 artifact 指纹守卫在打字部署那条路上失效 ——
                    // 比对的副本永远是原始代码，自然永远一致。
                    //
                    // 判据很简单：**可编辑就必须同步**。两者本来就是一回事。
                    syncToPython: {str(not readonly).lower()}
                }});
            }} catch (e) {{ console.error('[NanoCM] init failed:', e); }}
        }})();
        """
        self._js_fire(js)
        await asyncio.sleep(0.05)

    async def _cm_append(self, mount_el, delta: str):
        """流式追加 delta。fire-and-forget，auto-index 安全。"""
        if not mount_el or not delta:
            return
        js = f"""
        (() => {{
            if (!window.NanoCM || !window.NanoCM.append) return;
            try {{ window.NanoCM.append({mount_el.id}, {json.dumps(delta)}); }}
            catch (e) {{ console.error('[NanoCM] append failed:', e); }}
        }})();
        """
        self._js_fire(js)
        await asyncio.sleep(0.02)

    async def _cm_set_value(self, mount_el, value: str):
        """最终权威同步完整代码。fire-and-forget，auto-index 安全。"""
        if not mount_el:
            return
        js = f"""
        (() => {{
            if (!window.NanoCM || !window.NanoCM.setValue) return;
            try {{ window.NanoCM.setValue({mount_el.id}, {json.dumps(value or '')}); }}
            catch (e) {{ console.error('[NanoCM] setValue failed:', e); }}
        }})();
        """
        self._js_fire(js)
        await asyncio.sleep(0.02)

    async def _cm_set_editable(self, mount_el, editable: bool):
        """切换可编辑状态，同时开启 syncToPython。fire-and-forget，auto-index 安全。"""
        if not mount_el:
            return
        js = f"""
        (() => {{
            if (!window.NanoCM || !window.NanoCM.setEditable) return;
            try {{ window.NanoCM.setEditable({mount_el.id}, {str(editable).lower()}); }}
            catch (e) {{ console.error('[NanoCM] setEditable failed:', e); }}
        }})();
        """
        self._js_fire(js)
        await asyncio.sleep(0.02)

    async def _cm_get_value(self, mount_el, fallback: str = "", event_args=None) -> str:
        """读取 CodeMirror 当前值。尝试 await JS，失败则用 code_holder fallback。"""
        if not mount_el:
            return fallback or ""
        js = f"""
        (() => {{
            if (!window.NanoCM || !window.NanoCM.getValue) return {json.dumps(fallback or '')};
            return window.NanoCM.getValue({mount_el.id});
        }})()
        """
        client = None
        try:
            client = getattr(event_args, "client", None)
        except Exception:
            pass
        if client is None:
            try:
                client_ref = getattr(mount_el, "_client", None)
                client = client_ref() if callable(client_ref) else None
            except Exception:
                pass
        if client is None:
            client = self._ui_client
        if client is None:
            return fallback or ""
        try:
            with self._ui_scope():
                try:
                    value = await client.run_javascript(js, timeout=5.0)
                except TypeError:
                    value = await client.run_javascript(js, respond=True, timeout=5.0)
            return value if isinstance(value, str) else (fallback or "")
        except RuntimeError as e:
            if "auto-index" in str(e) or "Cannot await" in str(e):
                logger.warning("[UI] auto-index 禁止 await JS，使用 code_holder fallback")
                return fallback or ""
            logger.warning(f"[UI] 读取 CodeMirror 内容失败: {e}")
            return fallback or ""
        except Exception as e:
            logger.warning(f"[UI] 读取 CodeMirror 内容失败: {e}")
            return fallback or ""


    def request_skill_refresh(self):
        self._skill_refresh_requested = True

    def _consume_skill_refresh_request(self):
        if not self._skill_refresh_requested:
            return
        self._skill_refresh_requested = False
        try:
            registry.reload_all()
            self.refresh_skill_list()
            logger.info("🔄 已在 UI 主上下文刷新 Skill 列表")
        except Exception as e:
            logger.error(f"[UI] Skill 刷新失败: {e}")

    # ── Skill 审计弹窗 ────────────────────────────────────────────────────

    def refresh_pinned_interactions(self) -> None:
        """把未决交互刷到输入框上方那张常驻卡片上。

        ═══ 设计约束（都是 已定）═══

        · **位置 C**：composer 上方独立容器，不侵占聊天区、不盖输入框。
          没有未决交互时整块 `display:none`，一个像素都不占。
        · **代码默认折叠**，要看点开 —— 复用现有的 CodeMirror 审计弹窗，
          不在卡片里再造一个编辑器（"复用现有 audit UI"）。
        · **最多 6 条**（1 前台 + 5 队列，），第 7 条在内核层就被拒了，
          所以这里不用自己做上限。

        ⚠️ **只读投影**：这个函数一个字都不写状态。它读 `interaction` 表、渲染，
        完了。所有写入仍然只走 Kernel（唯一写路径）。
        点按钮触发的是既有的 `apply_pending_skill` / `cancel_pending_skill`，
        它们内部会关交互 —— 卡片自己不碰。
        """
        card = getattr(self, "_pinned_card", None)
        if card is None:
            return
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import interaction as _it
            _recs_all = _it.list_live(get_kernel())
        except Exception as e:
            logger.debug(f"[UI] 读取未决交互失败，pinned card 本次不更新: {e}")
            return

        # ⭐⭐⭐ [2026-08-13] **待办卡只为 Skill 代码审计服务。**
        #
        # 原来这里画的是**所有** live 交互，`_KIND_LABEL` 五种 kind 全在。
        # 实测（删 Skill）暴露出它的真面目：卡片文字与 nano 气泡**逐字相同**、
        # 卡上**一个按钮都没有**（确认仍然要回输入框打字）——
        # 也就是说它既没多说什么，也没多做什么。
        #
        # 📌 **一张卡片如果只是把气泡里的话复述一遍，它就不是 UI，是噪音。**
        # 📌 **待办卡该存在的唯一理由，是它承载了自然语言承载不了的东西。**
        #
        # `skill_audit` 过得了这条判据：它承载**代码块 + 折叠 + `<>` 看源码 +
        # 部署/丢弃动作**，这些没法用一句话替代。其余四种过不了：
        #   · `skill_clarification` / `skill_manage` —— 复读气泡，去掉
        #   · `skill_side_effect` / `os_risk` —— INLINE/EPHEMERAL，
        #     本来就该是当轮弹窗，不该在跨重启的待办卡里出现
        #
        # ⭐ 这条同时回答了「以后还要不要加新待办卡」：**不是禁止，是它过不了判据**。
        #    能用一句话说清的东西不配一张卡 —— 卡片加重程序感，而 Nano 要的是活人感。
        #
        # ⚠️⚠️ **这是纯显示收缩，一个字都不动 Interaction 记录。**
        #    澄清的 checkpoint（`original_requirement` / `last_explorer_message` /
        #    `include_os` / `explorer_prompt_version`）仍在 payload 上，
        #    续接照走 —— 否掉的是"把上下文压成字符串重塞"，不是这个。
        #    模型侧 `[Open Interactions]` 也照旧列出全部，它需要知道有什么挂着。
        #    **用户看不见卡，但 nano 已经在对话里把话说了** —— 这就是"纯净的自然语言"。
        _CARD_KINDS = (_it.Kind.SKILL_AUDIT,)
        recs = [r for r in _recs_all if r.kind in _CARD_KINDS]

        # ⭐⭐ [2026-08-06 实测] 目标已经不在清单里 → 立刻复位「回复这条」。
        #
        # ⚠️ 这不是打扫卫生，是**唯一的出口**：「取消引用」那个 ✕ 只长在目标
        # 自己那张卡片上，目标一关闭卡片就消失了 —— 用户再也点不到它，
        # 于是这个指向会一直挂着。里就是这样：
        # 澄清 int_192a0393ab 在 17:46 被 SUPERSEDED，之后每一轮都还在给模型
        # 注入「必须回答 int_192a0393ab、不许改挑别的」，直接把「部署这个吧」
        # 逼成了新建 Skill。
        #
        # 📌 判据：**只有一个入口能撤销的状态，那个入口不许比状态本身先消失。**
        #
        # ⚠️⚠️ 这里必须用 `_recs_all` 而**不是**过滤后的 `recs`：判据是
        #    「那条交互还活着吗」，不是「它现在画不画得出来」。用 `recs` 的话，
        #    一条仍然 OPEN 的澄清会因为**不再上卡**而被判成"已关闭"→ 取消引用，
        #    等于把显示策略偷偷变成了状态变更。
        # 📌 **过滤了显示之后，所有"还在不在"的判断都要回到未过滤的那份。**
        _rt = getattr(self, "_reply_target", None) or {}
        if _rt.get("iid") and _rt["iid"] not in {r.interaction_id for r in _recs_all}:
            logger.info(f"[UI] 「回复这条」目标 {_rt['iid']} 已关闭，自动取消引用")
            self._set_reply_target(None)

        # ⭐ composer 那个提示符靠这里自愈：轮次结束时的复位发生在
        # orchestrator 的 `finally` 里，那一刻 UI 完全没有参与，没人通知它。
        # 幂等，没变就是个空操作。
        self._refresh_reply_prompt()

        # 内容指纹：没变就不重画（避免 2 秒定时器把 DOM 翻来覆去重建，
        # 那会让用户正在展开的详情被折回去）。
        #
        # ⚠️⚠️ **`_reply_target` 必须进指纹。** 它决定按钮显示「回复这条」还是
        # 「取消引用」，也就是说它**是画面的一部分**。
        # 而它的复位发生在轮次结束的 `finally` 里 —— 那一刻 UI 没有任何参与，
        # 指纹如果不含它，定时器就会一直短路，按钮永远停在「取消引用」，
        # 等于复位了个寂寞。
        #
        # 📌 判据：**凡是影响渲染结果的输入，都必须在指纹里。**
        #    指纹漏掉一个输入，表现就是"状态变了但界面不变"——
        #    比不做缓存更难查，因为看起来像是状态没改成功。
        snap = "|".join(f"{r.interaction_id}:{r.status}:{r.revision}" for r in recs)
        snap += f"|reply={(self._reply_target or {}).get('iid') or ''}"
        if snap == getattr(self, "_pinned_snapshot", None):
            return
        self._pinned_snapshot = snap

        with self._ui_scope():
            card.clear()
            if not recs:
                card.style('display:none;')
                return
            card.style('display:flex;')
            with card:
                # ⚠️ 只留还上卡的那一种。另外四条**不是忘了删**：
                #    留着就成了第二张"写好但没人读"的死表（的根因，
                #    项目刚在里为它付过一次代价）。
                # ⭐ 仍然保留 dict + fallback 而不是直接写死两个常量：
                #    万一将来有人往 `_CARD_KINDS` 里加了东西却忘了加标签，
                #    fallback 会显示"待处理"（看得出不对），写死则会给它贴上
                #    "待审代码"（看起来对，实际是错的）。
                #    📌 **兜底要让错误可见，而不是让错误看起来正常。**
                _KIND_LABEL = {
                    _it.Kind.SKILL_AUDIT: ("fact_check", "待审代码"),
                }
                # 页码归位：待办被处理掉之后页码可能越界
                _n = len(recs)
                # getattr 兜底：定时器可能在 render() 走完之前就跳一次
                if getattr(self, "_pinned_page", 0) >= _n:
                    self._pinned_page = max(0, _n - 1)
                # ⭐ [2026-08-06] 不需要"自动翻到新的"那种 hack ——
                # `list_live()` 现在就是**最新在前**（`1 = 最新 / 2 = 次新 / 3 = 最旧`），
                # 新待办进来天然占住第 1 页、旧的往后推。
                # （第一版做的是"停在旧页 + 出现新的就跳到最后一页"，已明确那是错的：
                #   应该是顺序本身倒过来，而不是靠翻页去追。前者是数据的事，后者是补丁。）
                _pg = getattr(self, "_pinned_page", 0)

                def _flip(step: int):
                    self._pinned_page = (self._pinned_page + step) % max(1, len(recs))
                    # 指纹里不含页码，所以要手动作废，否则下一跳会短路掉不重画
                    self._pinned_snapshot = ""
                    self._redraw_pinned_now()   # 别让用户等 1.5 秒

                with ui.row().classes('w-full items-center gap-2').style('min-width:0;'):
                    ui.icon('push_pin').style('font-size:var(--nano-fs-md); color:var(--nano-amber);')
                    ui.label(f'{_n} 件事等你').style(
                        'font-size:var(--nano-fs-sm); font-weight:700; color:var(--nano-amber); '
                        'letter-spacing:0.04em; flex-shrink:0;')
                    # ANSWERED 的单独标出来 —— 那是"答案记下了但没跑完"，
                    # 与"还没回答"是两种状态，混在一起用户会以为自己没答。
                    _n_retry = sum(1 for x in recs if x.needs_retry)
                    if _n_retry:
                        ui.label(f'（{_n_retry} 件已回答·待重试）').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                    # ⭐ 翻页控件只在有多条时出现，一条时不占位置
                    if _n > 1:
                        ui.element('div').style('flex:1 1 auto;')   # 把翻页推到右侧
                        ui.button(icon='chevron_left').props(
                            'flat dense round size=sm').style('color:var(--nano-fg-soft);'
                        ).on('click', lambda: _flip(-1))
                        ui.label(f'{_pg + 1}/{_n}').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft); min-width:26px; text-align:center;')
                        ui.button(icon='chevron_right').props(
                            'flat dense round size=sm').style('color:var(--nano-fg-soft);'
                        ).on('click', lambda: _flip(1))

                # ⭐ 只渲染当前这一条 —— 高度恒定，不随待办数量增长。
                # 原来是 `for r in recs:` 全部堆上去，两条就把 composer 顶起来一截。
                for r in recs[_pg:_pg + 1]:
                    _icon, _tag = _KIND_LABEL.get(r.kind, ("radio_button_unchecked", "待处理"))
                    _q = (r.prompt_text or "").strip().replace("\n", " ") or "（无描述）"
                    with ui.row().classes('w-full items-start gap-2 no-wrap').style(
                        'min-width:0; padding:2px 0;'
                    ):
                        ui.icon(_icon).style(
                            'font-size:var(--nano-fs-lg); color:var(--nano-amber); flex-shrink:0; margin-top:2px;')
                        with ui.column().classes('min-w-0').style('gap:0; flex:1 1 auto;'):
                            # ⭐ `truncate` 只在**父容器能约束宽度**时才生效。
                            # 实测 实测"文字飞出 UI"：这一列是 `flex:1 1 auto`，
                            # 而 flex item 的默认 `min-width:auto` 会让它被内容撑开，
                            # `overflow:hidden` 因此永远没有可裁的边界。
                            # `min-w-0` 加在列上还不够 —— label 自己也要能被压缩。
                            _full = f'[{_tag}] {_q}'
                            ui.label(_full).classes('truncate u7-todo-text').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg); '
                                'min-width:0; max-width:100%; display:block;')

                        # ⭐⭐ 右上角**只放一个**按钮，位置因此天然对齐。
                        #
                        # 用户的两条：⑦ 待审代码类压根不该有 `...`（它已经有 `<>`
                        # 能看到全部内容，而且看的是代码本身，比看一句描述有用）；
                        # ⑨ `...` 和 `<>` 位置要完全一致。
                        # 两条合起来的正解不是"把两个按钮对齐"，而是
                        # **让它们永远不会同时出现** —— 审计类给 `<>`，其余给 `...`。
                        #
                        # 按这张卡片自己的 artifact_id 判，不是判"有没有最近那条"。
                        _is_audit = (r.kind == _it.Kind.SKILL_AUDIT
                                     and bool(self.agent._get_pending_skill(r.artifact_id or "")))
                        if _is_audit:
                            ui.button(icon='code').props('flat dense round size=sm').style(
                                'color:var(--nano-fg-soft); flex-shrink:0;'
                            ).on('click',
                                 lambda _f=(r.artifact_id or ""): self._reopen_pending_audit(_f)) \
                             .tooltip('查看/审计代码')
                        else:
                            # ⚠️⚠️ 「短文字也出现 `...`」修法的关键：
                            # **能不能放得下取决于窗口宽度，Python 侧算不出来。**
                            #
                            # 第一版按字符数（`len > 46`）判 —— 中文宽度是 ASCII 两倍，
                            # 短中文被误判。改成按东亚宽度折算也只是把误差变小，
                            # 因为容量本身会随窗口缩放变化：同一句话，窄窗截断、宽窗不截断。
                            #
                            # 📌 判据：**只有浏览器知道有没有真的截断**
                            #   （`scrollWidth > clientWidth`）。所以这里**默认渲染出来**，
                            #   由 `_u7_sync_more_buttons()` 注入的 JS 去决定藏不藏。
                            #
                            # ⚠️ 默认**可见**而不是默认隐藏：JS 万一没跑成，
                            #    结果是多一个没用的按钮，而不是"真截断了却没有入口"。
                            #    后者才是用户丢信息。
                            ui.button(icon='more_horiz').classes('u7-more-btn').props(
                                'flat dense round size=sm').style(
                                'color:var(--nano-fg-soft); flex-shrink:0;'
                            ).on('click', lambda _t=_full, _r=r: self._show_full_todo(_t, _r)) \
                             .tooltip('查看完整内容')

                    # ⭐ 底行：左边 `int_xxx`，右边 replay —— 两端对称。
                    #
                    # 原来 `int_xxx` 挤在描述文字下面、replay 单独占一行，
                    # 中间那道空隙把卡片撑得很高（「空行太大，卡片上下过高」）。
                    # 合成一行之后既省一行高度，左右也对称了。
                    with ui.row().classes('w-full items-center no-wrap').style(
                        'justify-content:space-between; gap:8px; min-width:0; margin-top:1px;'
                    ):
                        _meta = r.interaction_id
                        if r.artifact_id:
                            _meta += f' · {r.artifact_id}'
                        if r.needs_retry:
                            _meta += ' · 已回答，待重试'
                        ui.label(_meta).classes('truncate').style(
                            'font-size:var(--nano-fs-2xs); color:var(--nano-faint); min-width:0; flex:1 1 auto;')

                        # ⭐ `replay` —— 引用回复（用户的设计，**纯体验优化**）
                        #
                        # ⚠️ 原来的直接回复**仍然完全可用**，这个按钮只是让"我在回答哪个"
                        # 变成显式的。它的价值场景是 用户描述的那个：
                        # 待办挂了很久、中间和 Nano 聊了很多轮无关的事，
                        # 这时点一下比重新描述"我在回答哪个问题"方便得多。
                        #
                        # 📌 它和 `[Open Interactions]` 的歧义消解是**互补**的：
                        # 那条让模型**猜得更准**，这条让用户能**直接指定**，从而不必猜。
                        # ⚠️ ③ 文案就叫 `replay`（已定，与右键菜单那个入口同名，
                        #    两个入口不同、动作同一个，名字必须一致）。
                        _replayed = (getattr(self, "_reply_target", None) or {}).get("iid")
                        if _replayed == r.interaction_id:
                            ui.button('取消引用', icon='close').props(
                                'flat dense size=sm no-caps').style(
                                'color:var(--nano-amber); font-size:var(--nano-fs-xs); flex-shrink:0;'
                            ).on('click', lambda: self._set_reply_target(None))
                        else:
                            # ⚠️ 2026-08-21 已定：文案改中文「回复」。
                            #    ⭐ **两处必须同时改** —— 下面右键菜单那个入口
                            #       与它是同一个动作，早先的设计原话就是「与右键菜单那个
                            #       入口同名」。只改一处，那句话当场就不成立了。
                            #    📌 同一个动作在两个入口叫不同名字，用户会以为是两件事。
                            ui.button('回复', icon='reply').props(
                                'flat dense size=sm no-caps').style(
                                'color:var(--nano-fg-soft); font-size:var(--nano-fs-xs); flex-shrink:0;'
                            ).on('click',
                                 lambda _i=r.interaction_id, _q2=_q:
                                     self._set_reply_target(_i, _q2)
                                 ).tooltip('下一条消息会被明确标记为在回答这个待办')

        # DOM 刚重建完 → 让浏览器重新判一次哪些「…」是真需要的。
        # ⚠️ 必须在 `with self._ui_scope():` 之外、且在渲染之后 ——
        #    JS 查的是已经贴上去的 DOM。
        self._u7_sync_more_buttons()

    def _u7_sync_more_buttons(self) -> None:
        """只在**真的被截断**时保留「…」按钮，否则藏掉。

        判据是浏览器侧的 `scrollWidth > clientWidth` —— 这是唯一准确的来源：
        容量取决于窗口宽度和字体，Python 侧任何字数/宽度阈值都只是猜。

        同时挂一次 resize 监听：窄窗截断、拉宽之后就不该再有那个按钮了。
        幂等，`__nanoU7Resize` 防重复绑定。
        """
        self._js_fire("""
        (() => {
          const sync = () => {
            document.querySelectorAll('.u7-todo-text').forEach(lbl => {
              const row = lbl.closest('.items-start') || lbl.parentElement?.parentElement;
              const btn = row && row.querySelector('.u7-more-btn');
              if (!btn) return;
              // +1 容差：亚像素布局下未截断也可能差出零点几像素
              btn.style.display = (lbl.scrollWidth > lbl.clientWidth + 1) ? '' : 'none';
            });
          };
          sync();
          if (!window.__nanoU7Resize) {
            window.__nanoU7Resize = true;
            window.addEventListener('resize', () => setTimeout(sync, 80));
          }
        })();
        """)

    def _show_full_todo(self, text: str, rec) -> None:
        """三个点点开 → 小弹窗显示待办全文，过长可滚动。"""
        with self._ui_scope():
            # ⚠️⚠️ 实测：「详情弹窗**不能下滑**，文字显示不全」。
            #
            # ═══ 这一条连着判错两次，记下来省得再犯 ═══
            #
            # 第一版：内容区是 `ui.column()` + `max-height:calc(70vh-110px); overflow:auto`
            #         → 滚不动。
            # 我的判断①："flex 子元素 `min-height:auto` 不肯收缩" → 加了 min-height:0。
            #         但在同一个浏览器里做新旧结构对比，**两种写法都能滚** —— 没证实。
            # 我的判断②：改用 `ui.scroll_area()` + `flex:1`。
            #         → **这个项目已经试过并否掉了**，见 `_show_kb_file_content_dialog`
            #           里那句留痕："不用 ui.scroll_area()+flex:1，改用跟'全部记忆'
            #           弹窗一样、已验证能正常显示内容的写法"。差点把旧坑重挖一遍。
            #
            # 📌 真正的差别是**内容区用什么元素**：
            #    能用的那两个弹窗（知识库看内容 / 全部记忆）用的是**裸 `ui.element('div')`**，
            #    用的是 `ui.column()` —— 后者带 `.nicegui-column` 那套 flex 类，
            #    和手写的 overflow 打架。
            #
            # 📌 判据：**同一个项目里已经有能用的实现时，先去抄它，不要自己推。**
            #    留痕注释存在的意义就是这个 —— 而它差点被绕开、又走一遍死路。
            #
            # 下面这段结构与 `_show_kb_file_content_dialog` 保持一致，改动只有尺寸和配色。
            with ui.dialog() as d, ui.card().style(
                'width:680px; max-width:92vw; max-height:70vh; padding:0; overflow:hidden; '
                'background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.25); '
                'border-radius:12px;'
            ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:12px 18px; background:var(--nano-panel-2);'
                ):
                    ui.label('待办完整内容').style(
                        'font-size:var(--nano-fs-base); font-weight:700; color:var(--nano-fg);')
                    ui.button(icon='close').props('flat dense round').style(
                        'color:var(--nano-fg-soft);').on('click', d.close)
                with ui.element('div').style(
                    'width:100%; max-height:calc(70vh - 56px); overflow-y:auto; '
                    'padding:14px 18px;'
                ):
                    # ⚠️ 用 markdown 而不是 label：待办文本里常有 `**粗体**` 和列表
                    #（Explorer 的提问就是这种格式），当纯文本显示会满屏星号。
                    ui.markdown(text).style('font-size:var(--nano-fs-base); color:var(--nano-fg);')
                    _m = rec.interaction_id + (f' · {rec.artifact_id}' if rec.artifact_id else '')
                    ui.label(_m).style(
                        'font-size:var(--nano-fs-2xs); color:var(--nano-faint); display:block; margin-top:8px;')
            d.open()

    @property
    def _reply_target(self) -> dict | None:
        """只读视图。**权威副本在 orchestrator**，这里不存第二份。

        ⚠️ 原来 app 和 orchestrator 各存一份、靠 `_set_reply_target` 同步。
        那撑不住 ⑤（发出后自动复位）：复位发生在**轮次结束的 `finally` 里**，
        那一刻 UI 完全没参与，它那份会一直停在旧值 —— 按钮永远显示「取消引用」。
        双权威在这里不是"可能不同步"，是**注定不同步**。
        """
        return getattr(self.agent, "_reply_target", None)

    # 引用的两种来源。⚠️ **必须显式区分**，不许靠"iid 是不是空"去猜：
    #    📌 「一个字段不许表达两个现实」—— 两种引用注入给模型的话完全不同
    #       （一个是「你去答这条待办」，一个是「用户在指这段话」）。
    QUOTE_INTERACTION = "interaction"   # 引用一张待审卡片
    QUOTE_SELECTION = "selection"       # 选中聊天区的文字 → 右键 replay

    def _set_reply_target(self, iid: str | None, question: str = "",
                          kind: str = "") -> None:
        """设/清"下一条消息是在引用什么"。

        ⚠️ 只记**意图**，不动 Interaction 的状态 —— 真正的落盘仍然走
        `answer_open_interaction`（模型调工具）。这里做的只是把用户的指向
        明确告诉模型，省掉它去猜。

        ⭐ 选中文字那条入口**完全复用这套状态与 UI**（已定：
           「机制和 UI 反馈跟待审 skill 相同即可，只是入口不同」），
           区别只有 `kind` 和随之而来的那句注入。
        """
        # ⚠️ 默认值刻意写成空串而不是 `kind=QUOTE_INTERACTION`：
        #    在默认值里引用类属性虽然合法，但 `t_ui_edge_l25_l27` 的作用域检查器
        #    会把它判成未定义名。📌 **改代码比放宽一个抓过真 bug 的守卫便宜** ——
        #    守卫放宽一次，下次真的漏了就不会红。
        kind = kind or self.QUOTE_INTERACTION
        _sel = (kind == self.QUOTE_SELECTION) and bool((question or "").strip())
        try:
            self.agent._reply_target = (
                {"iid": iid or "", "q": (question or "")[:200], "kind": kind}
                if (iid or _sel) else None)
        except Exception as e:
            logger.warning(f"[UI] 传递引用回复目标失败: {e}")
            return
        # 指纹里不含 _reply_target，所以手动作废，否则重画会被短路掉。
        self._pinned_snapshot = ""
        self._redraw_pinned_now()
        self._refresh_reply_prompt()      # 输入框左边那个符号跟着变

    # 引用态用的符号。`❯` 是常态，`↳` 是"这条在回答上面某个待办"。
    #
    # ⚠️ 第一版用的是 `❮`（把 `❯` 左右翻转）。用户一眼看出来那是敷衍：
    #    反向箭头表达的是"往回/上一个"，不是"回复某条"。
    #    `↳`（U+21B3，先下后右）才是通用的"承接上面那条"的语义，
    #    也正是各家聊天软件引用回复用的那个形状。
    #
    # 仍然用字符而不是 Material 图标：composer 那一行是等宽终端风，
    # 塞图标字体会让基线和宽度都跳一下。
    _REPLY_PROMPT = "↳"
    _NORMAL_PROMPT = "❯"

    def _refresh_reply_prompt(self) -> None:
        """按当前有没有引用目标，切换输入框左边那个提示符。

        吞异常：这只是视觉提示，composer 还没建好（启动早期）或已销毁时
        不该把调用方弄崩 —— `_set_reply_target` 是从按钮回调里调过来的。
        """
        _rt = self._reply_target or {}
        _on = bool(_rt.get("iid")) or (
            _rt.get("kind") == self.QUOTE_SELECTION and bool(_rt.get("q")))
        # ── 引用条（出口）──────────────────────────────────────
        # ⚠️ 放在提示符那段**之前**：提示符那段有幂等短路（没变就 return），
        #    引用条挂在它后面的话，第二次调用会被那个 return 跳过。
        #    📌 一个提前 return 会悄悄把它后面的一切都变成「有时才执行」。
        self._refresh_quote_bar(_rt)
        el = getattr(self, "_composer_prompt", None)
        if el is None:
            return
        # 幂等：这个函数被 1.5 秒的定时器反复调（复位发生在轮次结束的 `finally` 里，
        # UI 那时没有参与，只能靠轮询自愈），没变就别改 DOM。
        _want = self._REPLY_PROMPT if _on else self._NORMAL_PROMPT
        if getattr(el, "text", None) == _want:
            return
        try:
            el.set_text(self._REPLY_PROMPT if _on else self._NORMAL_PROMPT)
            el.style(f'color:{"var(--nano-amber)" if _on else "var(--nano-amber)"}; font-size:var(--nano-fs-xl); '
                     f'flex-shrink:0; line-height:1; font-family:var(--nano-mono); '
                     f'opacity:{1 if _on else 0.85};')
            el.tooltip('这条消息会被标记为在回答上面那个待办' if _on else '')
        except Exception as e:
            logger.debug(f"[UI] 切换 composer 提示符失败（仅视觉）: {e}")

    def _render_quote_banner(self, text: str) -> None:
        """用户消息上面那条**引用横幅**。live 与重放**共用这一份**。

        ⭐⭐ [2026-08-22] 抽出来的直接原因：重启之后引用回复变回了普通消息 ——
           重放路径**根本不知道有引用这回事**。
           而修法如果是「在重放那边照着再写一遍」，就等于承认
           📌 **同一个东西有两份实现，它们只在"我两次想法相同"的前提下一致** ——
              本项目已经因为这个形状栽过的计量 vs 投影、的 live vs 重放。
        ⚠️ 调用方必须自己保证外层已经是 `w-full min-w-0` 的列 ——
           宽度靠那个约束决定，这里不写死任何数（见下面那条判据）。
        """
        if not (text or "").strip():
            return
        with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0').style(
            'margin-bottom:2px;'
        ):
            # 占位，让引用块与下面的正文左边缘对齐
            ui.label('').style('flex-shrink:0; min-width:64px;')
            # ⭐ [2026-08-09 实测] 这一块原来 **`items-center` + 内层
            #    `no-wrap` + 文字 `truncate`** —— 于是引用的原文
            #    **永远只有一行、超出就切掉省略号**。
            # ⭐ 用户的要求：它的单行最大长度应该**和聊天气泡一样**
            #    （那是最美观的宽度），超了就换行。
            # ⚠️ 三处一起改才有效，少一处都还是一行：
            #    · 外层 `items-center` → `items-start`
            #      （换行之后图标要对第一行，不是对整块的垂直中心）
            #    · 内层 `no-wrap` 去掉
            #    · 文字的 `truncate` 去掉，换成 `whitespace-pre-wrap`
            #      + `break-words`（长 URL / 长标识符也要能断）
            # 📌 **一个「不换行」的现象往往由多处叠加造成，
            #    只改看起来最相关的那一处会让人以为改法不对。**
            # ⭐ 宽度不用自己算：外层已经 `w-full min-w-0` 且有
            #    64px 占位，剩下的宽度**天然就是正文那条的宽度**。
            #    📌 要和某个东西一样宽，最好的办法是让它们
            #       由同一个约束决定，而不是各自写一个数。
            with ui.row().classes('items-start gap-1.5 min-w-0').style(
                'padding:3px 10px; border-left:2px solid var(--nano-amber); '
                'background:rgba(var(--nano-amber-rgb), 0.07); border-radius:0 6px 6px 0; '
                'max-width:100%;'
            ):
                ui.icon('reply').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-amber); flex-shrink:0; '
                    'margin-top:2px;')
                ui.label(text).classes(
                    'whitespace-pre-wrap break-words min-w-0').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.6;')

    def _refresh_quote_bar(self, rt: dict | None = None) -> None:
        """选中引用的可见出口：显示原文 + ✕ 取消。

        ⚠️ 只对 `selection` 生效。待审卡那条已经有出口（卡片上的「取消引用」），
           再给它一个就是**两处表达同一件事** —— 两处一定会不同步。
        ⚠️ 幂等：它被 1.5 秒的定时器反复调（轮次结束的复位发生在
           orchestrator 的 `finally` 里，UI 那时没有参与，只能靠轮询自愈）。
        """
        bar = getattr(self, "_quote_bar", None)
        lbl = getattr(self, "_quote_bar_text", None)
        if bar is None or lbl is None:
            return
        rt = rt if rt is not None else (self._reply_target or {})
        q = (rt.get("q") or "") if rt.get("kind") == self.QUOTE_SELECTION else ""
        try:
            if getattr(lbl, "text", None) != q:
                lbl.set_text(q)
                # 全文进 tooltip：条上只显示一行，但用户得有办法看全
                lbl.tooltip(q or "")
            _want = "display:flex;" if q else "display:none;"
            if getattr(self, "_quote_bar_shown", None) != bool(q):
                self._quote_bar_shown = bool(q)
                bar.style(_want)
        except Exception as e:
            logger.debug(f"[UI] 刷新引用条失败（仅视觉）: {e}")

    def _redraw_pinned_now(self) -> None:
        """立刻重画待办卡片，不等那个 1.5 秒的定时器。

        实测：点 replay / 翻页都有 **1–2 秒可见延迟**。
        原因是这两个动作只把指纹置空，真正的重画要等下一次 `ui.timer(1.5, …)`。

        ⚠️ **不能在这里直接调 `refresh_pinned_interactions()`** ——
        我们此刻正在那张卡片里某个按钮的点击回调里，而重画的第一件事是
        `card.clear()`，等于在处理点击的过程中把这个按钮本身删掉。
        推迟一拍（10ms）让当前回调先返回完，视觉上仍然是"立即"。
        """
        try:
            ui.timer(0.01, self.refresh_pinned_interactions, once=True)
        except Exception as e:
            # 立即重画只是体验优化，失败就退回定时器那条路，不能影响功能。
            logger.debug(f"[UI] 立即重画待办卡片失败，交给定时器: {e}")

    def _reopen_pending_audit(self, filename: str | None = None) -> None:
        """从 pinned card 点「看代码」→ 重开审计弹窗。

        ⚠️⚠️ **优先复用已经建好的那个 dialog，不要新建。**
        第一版直接调 `_show_skill_preview`，它每次都 `_build_skill_preview_dialog`
        建一个全新的 —— 实测撞出三个后果：
          ① 同一份代码有多个弹窗实例，点哪个生效说不清；
          ② 旧实例的悬浮条/引用成了孤儿，屏幕上卡着不走；
          ③ 点「验证并应用」时到底用哪个实例的 `code_holder`，取决于哪个后建 —— 不确定。
        所以这里记住上一次建的那个 dialog，能复用就复用。
        """
        # pinned card 现在可能有多条，卡片按 filename 告诉我们点的是哪一个。
        _p = self.agent._get_pending_skill(filename)
        if not _p:
            ui.notify(
                f'「{filename}」那份待审代码已经不在了（超时或已处理）'
                if filename else '那份待审代码已经不在了（重启或超时后会失效）',
                type='warning')
            return
        _fn = _p.get("filename") or filename or ""
        _live = (getattr(self, "_audit_dialog_refs", None) or {}).get(_fn)
        if _live is not None:
            try:
                with self._ui_scope():
                    _live["dialog"].open()
                _r = _live.get("on_restore")
                if _r is not None:
                    _res = _r()
                    if inspect.iscoroutine(_res):
                        asyncio.create_task(_res)
                return
            except Exception as e:
                logger.warning(f"[UI] 复用审计弹窗失败，改为新建: {e}")
        asyncio.create_task(self._show_skill_preview(
            _p.get("filename", "Unknown"), _p.get("code", ""),
            _p.get("description", ""), bool(_p.get("valid", True)),
            "", list(_p.get("errors") or []),
        ))

    def _make_minimizable(self, client, dialog, chip_label: str, chip_icon: str = 'expand_more',
                          on_restore=None, chip: bool = True):
        """一条硬性 UI 要求：弹窗/卡片不能强制遮挡背后的内容（包括正在
        显示的思考块），必须提供收起/最小化控件。这是通用实现，给所有"待确认"类
        弹窗（操作授权/副作用确认/Skill审计）复用，不要各自重新发明一套。

        收起：dialog.close()（只是隐藏，内容和已绑定的按钮回调都还在）+ 右下角弹出
        一个悬浮条，点悬浮条 = dialog.open() 重新展开，悬浮条同时自我清除。
        因为弹窗的确认/取消按钮只在展开状态下才能点到，用户必须先展开才能操作，
        这条路径下"展开"本身就会清掉悬浮条，不会有悬浮条残留的情况。

        ═══ on_restore：给"重开后需要自己重建"的内容用（2026-08-05 实测 bug）═══

        `dialog.close()` 只是隐藏，**但 Quasar 的 `q-dialog` 会把内容从 DOM 里卸载**。
        对纯 NiceGUI 元素无所谓（重开时按 Python 侧状态重新渲染），
        但 **CodeMirror 是 JS 侧挂在那个 div 上的实例** —— DOM 一没，实例就没了；
        重开时拿到的是一个全新的空 div，而 `_cm_init` 是一次性 fire-and-forget，
        没人再调它。表现就是 实测到的：**最小化再展开，代码框变成空白。**
        （只是视觉：`code_holder[0]` 在 Python 侧还在，所以「验证并应用」仍然正常。
          但用户看到一个空框还敢点部署吗 —— 这才是真正的代价。）

        所以需要重建 JS 侧内容的调用方，传一个 `on_restore`。它在 `dialog.open()`
        **之后**被调用，可以是普通函数或协程。
        """
        holder = {"chip": None}
        # ⭐⭐ **在这里登记，不让三个调用方各自记得。**
        #    📌 同「Subagent工具集用白名单」那条：一个要求每个调用方自觉的机制，
        #       它的欠账会随调用方数量增长 —— 而这里已经有三个
        #       （OS 授权 / 副作用确认 / Skill 审计），第四个迟早会来。
        self._register_pending_confirm(dialog, holder)

        def _restore():
            if holder["chip"] is not None:
                try:
                    holder["chip"].delete()
                except Exception:
                    pass
                holder["chip"] = None
            dialog.open()
            if on_restore is None:
                return
            # 重建交给调用方。吞异常：恢复失败最坏是回到原来那个空框，
            # 不能让它把"展开弹窗"这个动作本身弄崩。
            try:
                _r = on_restore()
                if inspect.iscoroutine(_r):
                    asyncio.create_task(_r)
            except Exception as e:
                logger.warning(f"[UI] 弹窗展开后的内容重建失败: {e}")

        def _minimize():
            dialog.close()
            # ⚠️ 下面那个 `with ... as chip_el` **绝对不能再叫 `chip`** —— 叫 `chip`
            #   会让 `chip` 在本函数里变成局部名，这一行读到的就是"未赋值的局部变量"，
            #   直接 UnboundLocalError，整个最小化动作崩掉。
            #   （2026-08-06 实测 真崩过，还连累"改动存活不过最小化"
            #     那条测试被误判成同步问题。）
            if not chip:
                # ⭐ 调用方自己有常驻入口（pinned card），不要再放一个悬浮条 ——
                # 两个入口并存会产生孤儿悬浮条，见审计弹窗那处的说明。
                return
            with client:
                with ui.row().style(
                    'position:fixed; bottom:90px; right:24px; z-index:9999; '
                    'align-items:center; gap:8px; padding:10px 16px; '
                    'background:var(--nano-panel-2); border:1px solid rgba(var(--nano-warn-rgb),0.4); '
                    'border-radius:999px; cursor:pointer; box-shadow:0 4px 16px rgba(var(--nano-ink-rgb), 0.35);'
                ).on('click', _restore) as chip_el:
                    ui.icon(chip_icon).style('font-size:var(--nano-fs-2xl); color:var(--nano-amber);')
                    ui.label(chip_label).style('font-size:var(--nano-fs-base); color:var(--nano-fg); font-weight:600;')
                holder["chip"] = chip_el

        return _minimize

    @staticmethod
    def _make_thought_preview(text: str, max_len: int = 32) -> str:
        """从思考内容里提取一段更有"摘要感"的预览，不是粗暴截最后N个字符。

        第一版（按句末标点切完整个文本再取最后一句）实测复现出新问题：
        模型在思考过程里经常夹带数据列表/表格（比如逐条列出客户信息，
        行与行之间没有句末标点），整段列表会被正则误判成一个超长"句子"，
        截断点落在列表数据中间，读起来更乱（用户原话"句子分割还是乱的"）。

        改成先按换行分行（列表/表格通常是按行排列的，不要提前拍平成
        空格），从最后一行往前找一个"像人话"的句子（用字母占比简单
        判断，过滤掉数据行/表格行），找不到才退回最后一行硬截断兜底
        ——这样数据列表途中产生的预览，会自动跳过列表自身，落在前后的
        陈述句上，比如"输出格式：按照清单要求，完整列出所有信息。"
        而不是"普通客户, 13800000010"这种断章数据片段。
        """
        if not text or not text.strip():
            return ""
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return ""

        def _looks_like_prose(s: str) -> bool:
            if not (4 <= len(s) <= max_len * 3):
                return False
            word_chars = sum(1 for ch in s if ch.isalpha())
            return word_chars / len(s) > 0.4

        for line in reversed(lines):
            sentences = re.split(r'(?<=[。！？.!?;；])\s*', line)
            sentences = [s.strip() for s in sentences if s.strip()]
            candidate = sentences[-1] if sentences else line
            if _looks_like_prose(candidate):
                return candidate if len(candidate) <= max_len else "…" + candidate[-max_len:]

        last_line = lines[-1]
        return last_line if len(last_line) <= max_len else "…" + last_line[-max_len:]

    @staticmethod
    def _extract_latest_sentence(text: str, max_len: int = 22) -> str:
        """从累积 think 文本里提取最后一个完整句子（以句末标点结尾）。
        只在检测到新句末标点时才更新，保证每次展示的都是完整句子，
        不出现乱七八糟的断句片段。找不到完整句子返回空字符串。"""
        matches = list(re.finditer(r'[。！？.!?]', text))
        if not matches:
            return ""
        start = matches[-2].end() if len(matches) >= 2 else 0
        sentence = text[start:matches[-1].end()].strip()
        if not sentence:
            return ""
        return sentence if len(sentence) <= max_len else sentence[:max_len] + '…'


    def _collapse_thought_block(self, _ref: dict):
        """折叠态：隐藏完整内容，标题栏右侧补一行灰字预览，展开箭头指向
        "可展开"方向。已完成用"耗时 · 最后一条内容预览"格式（早先的设计
        11.2节）；还在进行中用实时滚动预览（没有耗时这个概念）——这个函数
        在思考块创建时（默认折叠）和用户主动从展开点回折叠时都会用到，
        两种场景都要能正确选用对应格式。
        """
        _ref["expanded"] = False
        try:
            _ref["content_lbl"].set_visibility(False)
            if _ref.get("done"):
                _preview_text = _ref.get("preview_text", "")
                _ref["preview_lbl"].set_text('· ' + _preview_text if _preview_text else '')
            else:
                _ref["preview_lbl"].set_text('· 思考中…')
            _ref["expand_icon"].set_text('▾')
        except Exception:
            pass

    def _expand_thought_block(self, _ref: dict):
        """展开态：显示完整内容，标题栏不再显示预览，展开箭头指向"可收起"方向。"""
        _ref["expanded"] = True
        try:
            _ref["content_lbl"].set_visibility(True)
            _ref["preview_lbl"].set_text('')
            _ref["expand_icon"].set_text('▴')
        except Exception:
            pass

    def _toggle_thought_block(self, _ref: dict):
        """标题栏点击回调——思考块默认就是折叠的，进行中也能随时点开看当前
        播放到哪里了，不再要求必须等done。"""
        if _ref.get("expanded"):
            self._collapse_thought_block(_ref)
        else:
            self._expand_thought_block(_ref)

    # ══════════════════════════════════════════════════════════════════
    # 🔴 确认弹窗**不许活过它那一轮**
    # ══════════════════════════════════════════════════════════════════
    #
    # 现象：把授权弹窗最小化 → 给 Nano 发一句「你好」→ **pill 变成对钩**，
    #       而那个弹窗还挂着说"待授权"。
    #
    # ⭐ 查下来 **pill 是对的**：`inbox.wait_confirm_or_user_message` 是
    #    **刻意**设计成「等确认**或**等一条新用户消息，谁先来算谁」的，
    #    docstring 原话「这个机制是**为了少堵用户**」。用户改口说话 →
    #    那个 OS 动作按设计被取消 → 这一轮正常收尾。
    #
    # 🔴 **错的是没人去关那个弹窗** —— 它变成一个死壳：
    #    上面写着"待授权"，按钮指向一个已经结束的等待。
    # 📌 **一个只能被自己的按钮关掉的弹窗，一定会在「不是按按钮」的那些
    #    结束路径上活下来** —— 而那些路径恰恰是用户没在看它的时候。
    #
    # ⚠️ 最小化本身**不是 bug**（早先就硬性要求过：弹窗不许强制遮挡
    #    背后的内容）。所以修的是"关不掉"，不是"能收起"。
    #    ⚠️ 但最小化之后弹窗已经 `close()` 了，**右下角那个悬浮条还在** ——
    #       所以外部关闭必须**连悬浮条一起清**，否则壳只是换了个形状。

    def _present_os_confirm(self, step: dict) -> bool:
        """把一次 OS 授权请求呈现出来。返回 True = 已经处理掉（auto 直接放行）。

        ⭐⭐ **抽成方法是因为它现在有两个入口**：
          · 轮内 —— `navigate_pipeline` 的事件流（main agent 自己动手时）
          · 轮外 —— `_drain_oob_events`（Subagent**跨过了它那一轮**之后）
        📌 同一件事有两处各画一遍，只在"我两次想法相同"的前提下一致
           （本项目第 N 次撞这个形状：live/replay 工具卡、pill、渲染…）。
        """
        _on_confirm = step.get("on_confirm")
        # auto 模式（全局或本次临时）→ 自动通过，不弹窗（消除中途偷焦点）
        # ⭐ 走 `on_auto` 而**不是** `on_confirm` —— 📌 「用户亲自点了同意」
        #    和「auto 替用户点了」对模型是两件事：前者是一次真实的人类判断，
        #    后者是"这一轮压根没人被问过"。复用同一个回调，这个区别就消失了。
        # ⭐⭐ [危险判定 2026-08-27] auto 放行的判据从「auto 开着」收紧成
        #    「auto 开着 **且** 这一步被判定为可自动放行」。
        #
        # ⚠️ **判据写成 `is True` 而不是 `not step.get("blocked")`** ——
        #    📌 前者要求上游**明确说可以**，后者是「没人说不行就放行」。
        #       上游漏传这个键时：前者退化成"照常弹窗"（安全），
        #       后者退化成"静默执行"（危险）。**默认值那一侧永远是危险的那侧。**
        # ⚠️ C/D 走到弹窗时**没有「始终允许」按钮** —— 那不是这里做的，
        #    是 `run_command` 的 floor=3 天然走红色分支（见 _show_os_action_confirm_dialog）。
        #    📌 而这正是它该有的语义：用户开的本来就是 auto，
        #       "始终"什么呢？下一个弹窗是**另一次**意图对不上，不是同一件事。
        if self._auto_on() and step.get("auto_ok") is True:
            _cb = step.get("on_auto") or _on_confirm
            if _cb:
                _cb()
            return True
        try:
            if self._current_loading_label:
                self._current_loading_label.set_text('等待你确认操作...')
        except Exception:
            pass
        with self._ui_scope():
            self._show_os_action_confirm_dialog(
                action=step.get("action", ""),
                effective_risk=step.get("effective_risk", 2),
                params_summary=step.get("params_summary", ""),
                reason=step.get("reason", ""),
                params_raw=step.get("params_raw", {}),
                on_confirm=_on_confirm or (lambda: None),
                on_always=step.get("on_always") or (lambda: None),
                on_cancel=step.get("on_cancel") or (lambda: None),
                annotated_image_path=step.get("annotated_image_path", ""),
                agent_label=step.get("agent_label", ""),
            )
        return False

    async def _drain_oob_events(self) -> None:
        """轮外 UI 事件的**唯一消费者**。

        🔴🔴 **它补的是 detach 引入的一个结构性洞**（实测 2026-08-20，Subagent卡死）：
            Subagent 5s 后交还 → 主轮继续 → 主轮结束 → `navigate_pipeline` 的
            `async for` 退出 → **从此没有人 drain `event_queue`**
            → Subagent随后调 `edit_file` → `os_action_confirm` 进了一个没人读的队列
            → 弹窗永远不出现 → Subagent在确认闸上干等 300 秒。
        📌 **一个跨过了自己那一轮的执行者，不能再用那一轮的通道去要 UI** ——
           那条通道的寿命和那一轮绑在一起，而它已经不在那一轮里了。
        ⚠️ 这个洞**只可能出现在需要 UI 往返的事件上**：Subagent的只读工具不需要
           任何往返，所以之前一直没撞到 —— 给它写权限的那一刻才暴露。

        ⚠️ 只认**需要用户回应**的那几种事件，不搬整套事件词汇表 ——
           📌 一个"顺便什么都能收"的通道，会变成第二条谁都往里塞的主路。
        """
        _q = getattr(self, "_oob_events", None)
        if _q is None:
            return
        while not _q.empty():
            try:
                _ev = _q.get_nowait()
            except Exception:
                break
            try:
                _kind = (_ev or {}).get("event", "")
                if _kind == "os_action_confirm":
                    self._present_os_confirm(_ev)
                elif _kind == "confirm_dismiss":
                    self._dismiss_pending_confirms(_ev.get("why", ""))
                else:
                    logger.warning(f"[OOB] 轮外通道收到不认识的事件：{_kind}")
            except Exception as e:
                logger.warning(f"[OOB] 轮外事件处理失败（{_ev.get('event','?')}）: {e}")

    def _dismiss_pending_confirms(self, why: str = "") -> None:
        """把还挂着的确认类弹窗（OS 授权 / 副作用 / Skill 审计）全部收掉。**永不抛。**"""
        _reg = getattr(self, "_pending_confirm_dialogs", None) or []
        if not _reg:
            return
        self._pending_confirm_dialogs = []
        for _d, _holder in _reg:
            try:
                with self._ui_scope():
                    if _d is not None:
                        _d.close()
                    # ⚠️ 悬浮条只在**最小化过**的情况下存在 —— 见 `_make_minimizable`。
                    _chip = (_holder or {}).get("chip") if isinstance(_holder, dict) else None
                    if _chip is not None:
                        _chip.delete()
                        _holder["chip"] = None
            except Exception as e:
                logger.debug(f"[Confirm] 收弹窗失败: {e}")
        if why:
            logger.info(f"[Confirm] 已收掉 {len(_reg)} 个还挂着的确认弹窗（{why}）")

    def _forget_pending_confirm(self, dialog) -> None:
        """用户**自己**点了按钮 → 它不再是"挂着的"。⚠️ 不摘的话，
        下一次收尾会去 close 一个早就关了的弹窗（无害但会掩盖真问题）。"""
        try:
            _reg = getattr(self, "_pending_confirm_dialogs", None) or []
            self._pending_confirm_dialogs = [x for x in _reg if x[0] is not dialog]
        except Exception:
            pass

    def _register_pending_confirm(self, dialog, chip_holder=None) -> None:
        """登记一个"正在等用户"的弹窗，供 `_dismiss_pending_confirms` 收尾。

        ⚠️ `chip_holder` 是**最小化悬浮条的持有者**（一个 list，元素可能后补）——
           📌 悬浮条是最小化那一刻才生出来的，登记时它还不存在；
              传值就只能传一个"以后会有东西"的容器。
        """
        _reg = getattr(self, "_pending_confirm_dialogs", None)
        if _reg is None:
            _reg = self._pending_confirm_dialogs = []
        _reg.append((dialog, chip_holder))

    def _show_os_action_confirm_dialog(self, action: str, effective_risk: int,
                                        params_summary: str, reason: str,
                                        on_confirm, on_always, on_cancel,
                                        annotated_image_path: str = "",
                                        params_raw: dict = None,
                                        agent_label: str = ""):
        """OS 操作授权弹窗。

        risk=2 → 黄色标准卡片（可选"始终允许"）
        risk=3 → 红色 + 5秒倒计时（确认按钮倒计时内禁用，不可"始终允许"）
        annotated_image_path → click 类操作的定位标注截图（让用户看到"要点哪"）
        """
        client = self._ui_client
        if client is None:
            return
        is_high_risk = effective_risk >= 3
        border_color = "rgba(var(--nano-danger-rgb),0.4)" if is_high_risk else "rgba(var(--nano-warn-rgb),0.3)"
        icon_color   = "var(--nano-danger)" if is_high_risk else "var(--nano-warn)"
        icon_name    = "dangerous" if is_high_risk else "security"
        title_color  = "var(--nano-danger)" if is_high_risk else "var(--nano-warn)"
        title_text   = "高危操作确认" if is_high_risk else "操作授权"

        # ⭐ **先算代码预览，宽度才能跟着内容走**（2026-08-15：
        #    「脚本内容框也太窄了，右侧明明有那么多可占用的空间」）。
        # 📌 一个写死宽度的弹窗，是在假设"所有内容一样宽" ——
        #    而这里同一个弹窗既要装一行 `path=...`，也要装一个文件的全文。
        _code_preview = ""
        if params_raw and action in ("run_command", "file_write"):
            if action == "run_command":
                _code_preview = (params_raw.get("script") or
                                 params_raw.get("command") or "")
            else:  # file_write
                _p_ = params_raw.get("path", "")
                # ⭐ `_preview` 是调用方明确指定「授权卡该看什么」时给的
                #    （现在只有 `edit_file` 会给：它要看的是**那几处改动**，
                #     不是改完之后的整份文件）。
                # 📌 授权卡答「要做什么」，pill 展开答「做了什么」——
                #    同一份内容放错界面，两个问题都没答好。
                _pv_ = params_raw.get("_preview")
                if _pv_:
                    _code_preview = f"# {_p_}\n{_pv_}" if _p_ else str(_pv_)
                else:
                    _c_ = params_raw.get("content", "")
                    _code_preview = f"# {_p_}\n{_c_}" if _p_ else _c_
        _w = "820px" if _code_preview else "440px"

        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     f'width:{_w}; max-width:95vw; padding:0; '
                     f'background:var(--nano-panel); '
                     f'border:1px solid {border_color}; '
                     f'border-radius:14px; overflow:visible;'
                 ):

                # 标题栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; gap:10px; '
                    'padding:14px 20px; '
                    'border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                    'background:var(--nano-panel-2); border-radius:14px 14px 0 0;'
                ):
                    with ui.row().style('align-items:center; gap:10px;'):
                        ui.icon(icon_name).style(f'font-size:var(--nano-fs-5xl); color:{icon_color};')
                        with ui.column().style('gap:2px;'):
                            # ⭐⭐ **这个动作是谁发起的。**
                            #    2026-08-20 实测 Claude Code：subagent 改文件时那个弹窗
                            #    与 main agent 的**一字不差**（"Allow Claude to edit …"），
                            #    连 agent 自己都不知道弹过窗。
                            #    📌 **一个后台执行体发起的写操作，如果在弹窗上和你
                            #       自己那一轮长得一模一样，用户就没有办法判断
                            #       「这是我刚让它做的吗」** —— 而那正是用户要判断的事。
                            #    ⚠️ 只加这一行：授权规则（地板 / 预授权 / Auto）
                            #       一个字不改（「本身怎么授权它就怎么授权」）。
                            #    ⚠️ 这行文案不在 Nano 气泡里（系统级弹窗），
                            #       命中固定文案豁免。
                            # ⭐ 来源标记不在标题行 —— 见下面 `action:` 那一行。
                            ui.label(title_text).style(
                                f'font-size:var(--nano-fs-md); font-weight:600; color:{title_color};'
                            )
                            # ⭐⭐ 来源就写在 `action:` **同一行的前面**。
                            #    🔴 上一版做成了一个绿色徽标 —— 「太扎眼了，
                            #       跟其他元素不像一个画风」。
                            #    📌 **一个只用来消歧的标记，不该比它消歧的那件事更响** ——
                            #       它要回答的只是「这次是谁要动手」，
                            #       而不是在弹窗上争第二个视觉焦点。
                            #    ⭐ 所以字号/颜色/字重与 `action:` **完全一致**，
                            #       只靠位置和一个 `·` 分隔——同一行里多一段前缀。
                            ui.label(
                                (f'✦ nano agent · ' if agent_label else '')
                                + f'action: {action}'
                            ).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); ')
                    # ⚠️ 悬浮条也要带来源：收起来之后它是唯一还看得见的东西 ——
                    #    📌 一个标识如果只活在展开态，那它在用户最需要它的时候
                    #       （屏上挂着好几个待确认）恰好不在。
                    _minimize = self._make_minimizable(
                        client, dialog,
                        (f'nano agent · ' if agent_label else '')
                        + f'{title_text}待确认 · {action}')
                    ui.button(icon='remove').props('flat round dense').style(
                        'color:var(--nano-fg-soft);'
                    ).on('click', _minimize)

                # 详情区
                with ui.column().style(
                    # ⚠️ 原来是 `#0b0d14` —— 一块**冷调藏蓝**，而整套终端主题是
                    #    暖褐（卡片 `var(--nano-panel)` / 标题栏 `var(--nano-panel-2)`）。
                    #    2026-08-15：「弹窗的中间是个很奇怪的蓝色」。
                    # 📌 一个跟周围**色温**不同的块，比色号错更显眼 ——
                    #    人先看出"这块不属于这里"，再看出它是什么颜色。
                    'width:100%; padding:16px 20px; gap:8px; background:var(--nano-panel);'
                ):
                    if reason:
                        ui.label(reason).style('font-size:var(--nano-fs-md); color:var(--nano-fg-soft);')

                    # 定位标注截图——让用户看到"Nano 要点这里"再确认
                    if annotated_image_path:
                        try:
                            import base64 as _b64, pathlib as _pl
                            _p = _pl.Path(annotated_image_path)
                            if _p.exists():
                                _raw = _p.read_bytes()
                                _b64str = _b64.b64encode(_raw).decode()
                                # ⚠️ 这张**刻意不缩略**：用户要靠它判断「红圈对不对」，
                                #    缩了就看不清 —— 📌 一张要用来做决定的图，
                                #    和一张只用来证明「我看过」的图，不是一回事。
                                #    但仍然可以点开看原图。
                                chat_image(f"data:image/png;base64,{_b64str}",
                                           alt="即将点击的位置", thumb_h=320)
                                ui.label('红圈处是我即将点击的位置，确认无误再点「执行」').style(
                                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); font-style:italic;'
                                )
                        except Exception:
                            pass

                    if params_summary:
                        with ui.row().style('align-items:center; gap:8px; margin-top:4px;'):
                            ui.icon('tune').style('font-size:var(--nano-fs-lg); color:var(--nano-fg-soft);')
                            ui.label(params_summary).style(
                                'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); '
                            )

                    # 代码预览（内容在上面算过了 —— 宽度要用它）
                    if _code_preview:
                        import html as _html_mod
                        _escaped = _html_mod.escape(_code_preview)
                        with ui.column().classes('w-full').style(
                                'gap:4px; margin-top:8px; min-width:0;'):
                            ui.label('脚本内容').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); font-weight:500;'
                            )
                            # ⚠️ `ui.html` 外面那层 div **不会自己占满** ——
                            #    里面的 `<pre width:100%>` 于是只有那层 div 那么宽。
                            #    📌 给内层写 `width:100%` 而没给外层，
                            #       等于把百分比挂在一个自己也不知道多宽的东西上。
                            ui.html(
                                f'<pre style="'
                                f'width:100%; max-height:320px; overflow:auto; '
                                # ⚠️ 原来是 `#060810` 底 + `#c8d3f5` 字 —— 又一处**冷调蓝**，
                                #    与暖褐主题格格不入（同上面那块 `#0b0d14`）。
                                f'background:var(--nano-bg); border:1px solid rgba(var(--nano-contrast-rgb), 0.08); '
                                f'border-radius:8px; padding:10px 12px; margin:0; '
                                f'font-size:var(--nano-fs-sm); line-height:1.6; color:var(--nano-fg); '
                                f'font-family:var(--nano-mono),monospace; '
                                f'white-space:pre-wrap; word-break:break-word; '
                                f'overflow-wrap:anywhere;'
                                f'">{_escaped}</pre>'
                            ).classes('w-full').style('min-width:0;')

                    risk_label = "高危 · 不可撤销" if is_high_risk else "中危 · 影响系统/文件"
                    risk_color = "var(--nano-danger)" if is_high_risk else "var(--nano-warn)"
                    with ui.row().style('align-items:center; gap:6px; margin-top:8px;'):
                        ui.element('div').style(
                            f'width:6px; height:6px; border-radius:50%; background:{risk_color};'
                        )
                        ui.label(f'风险等级 {effective_risk}：{risk_label}').style(
                            f'font-size:var(--nano-fs-sm); color:{risk_color};'
                        )

                # 操作栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:flex-end; gap:10px; '
                    'padding:12px 20px; flex-wrap:wrap; '
                    'border-top:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                    'background:var(--nano-panel-2); border-radius:0 0 14px 14px;'
                ):
                    # 取消（始终可点）
                    ui.button('取消', icon='close').props('flat').style(
                        'color:var(--nano-fg-soft); font-size:var(--nano-fs-md);'
                    ).on('click', lambda: (self._forget_pending_confirm(dialog),
                                           dialog.close(), on_cancel()))

                    if is_high_risk:
                        # risk=3：5秒倒计时，不可"始终允许"
                        confirm_btn = ui.button('确认执行 (5)', icon='check').props(
                            'unelevated disabled'
                        ).style('background:var(--nano-danger-fill); color:#fff; font-size:var(--nano-fs-md); padding:0 14px; border-radius:8px;')
                        countdown = [5]

                        async def _tick():
                            import asyncio
                            while countdown[0] > 0:
                                await asyncio.sleep(1)
                                countdown[0] -= 1
                                try:
                                    confirm_btn.set_text(
                                        f'确认执行 ({countdown[0]})' if countdown[0] > 0 else '确认执行'
                                    )
                                except Exception:
                                    return
                                if countdown[0] == 0:
                                    try:
                                        confirm_btn.props(remove='disabled')
                                    except Exception:
                                        pass

                        import asyncio as _aio
                        _aio.ensure_future(_tick())
                        confirm_btn.on('click', lambda: (self._forget_pending_confirm(dialog),
                                                         dialog.close(), on_confirm()))
                    else:
                        # risk=2：标准卡片，可"始终允许"
                        ui.button('本次允许', icon='check').props('unelevated').style(
                            'background:var(--nano-warn-fill); color:#fff; font-size:var(--nano-fs-md); padding:0 14px; border-radius:8px;'
                        ).on('click', lambda: (self._forget_pending_confirm(dialog),
                                               dialog.close(), on_confirm()))
                        ui.button('始终允许', icon='done_all').props('unelevated').style(
                            'background:var(--nano-ok-fill); color:#fff; font-size:var(--nano-fs-md); padding:0 14px; border-radius:8px;'
                        ).on('click', lambda: (dialog.close(), on_always()))

            dialog.open()

    def _show_user_choice_card(self, question: str, choices: list, allow_custom: bool,
                               on_choice, on_dismiss, progress=None):
        """选择卡片：挂载在输入框正上方，支持折叠成小条。

        progress：可选 (当前序号, 总数)，>1 时在标题显示 "1/X" 计数（多卡逐张作答）。
        """
        client = self._ui_client
        if client is None or self.choice_card_slot is None:
            return

        card_ref = [None]
        expanded_ref = [None]
        collapsed_ref = [None]
        custom_input_ref = [None]
        is_expanded = [True]

        def _do_choice(val):
            on_choice(val)
            if card_ref[0]:
                card_ref[0].delete()
                card_ref[0] = None

        def _do_dismiss():
            on_dismiss()
            if card_ref[0]:
                card_ref[0].delete()
                card_ref[0] = None

        def _toggle():
            is_expanded[0] = not is_expanded[0]
            if expanded_ref[0]:
                expanded_ref[0].set_visibility(is_expanded[0])
            if collapsed_ref[0]:
                collapsed_ref[0].set_visibility(not is_expanded[0])

        def _make_handler(lbl):
            def _h():
                _do_choice(lbl)
            return _h

        def _skip():
            _do_choice(None)

        def _submit_custom():
            val = (custom_input_ref[0].value or '').strip() if custom_input_ref[0] else ''
            _do_choice(val if val else None)

        with client:
            with self.choice_card_slot:
                with ui.element('div').style(
                    'border: 1.5px solid var(--nano-fg); border-radius: 12px;'
                    'background: var(--nano-panel); box-shadow: 0 4px 16px rgba(var(--nano-ink-rgb), 0.10);'
                    'margin-bottom: 8px; overflow: hidden;'
                ) as _card:
                    card_ref[0] = _card

                    # ── 标题栏（始终可见）──
                    with ui.element('div').style(
                        'display:flex; align-items:center; justify-content:space-between;'
                        'padding: 11px 14px 10px;'
                    ):
                        with ui.element('div').style('display:flex; align-items:center; gap:8px; min-width:0;'):
                            ui.element('div').style(
                                'width:7px; height:7px; border-radius:50%; background:var(--nano-warn); flex-shrink:0;'
                            )
                            if progress and progress[1] > 1:
                                ui.label(f'{progress[0]}/{progress[1]}').style(
                                    'font-size:var(--nano-fs-sm); font-weight:600; color:var(--nano-warn); flex-shrink:0;'
                                    'background:rgba(var(--nano-warn-rgb),0.14); border-radius:6px; padding:1px 7px;'
                                )
                            ui.label(question).style(
                                'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);'
                                'white-space:nowrap; overflow:hidden; text-overflow:ellipsis;'
                            )
                        with ui.element('div').style('display:flex; gap:2px; flex-shrink:0; margin-left:8px;'):
                            ui.button('', icon='expand_more', on_click=_toggle).props('flat dense round size=xs').style('color:var(--nano-fg-soft);')
                            ui.button('', icon='close', on_click=_do_dismiss).props('flat dense round size=xs').style('color:var(--nano-fg-soft);')

                    # ── 展开区：选项列表 + 底部按钮 ──
                    with ui.element('div') as expanded_body:
                        expanded_ref[0] = expanded_body
                        with ui.element('div').style(
                            'border-top: 1px solid var(--nano-line); padding: 6px 10px 4px;'
                            'max-height: 40vh; overflow-y: auto;'
                        ):
                            for i, choice in enumerate(choices[:6]):  # 最多显示6个
                                if isinstance(choice, dict):
                                    lbl = (choice.get('label') or choice.get('text') or str(choice))[:60]
                                    desc = (choice.get('description') or choice.get('desc') or '')[:80]
                                else:
                                    lbl = str(choice)[:60]
                                    desc = ''
                                with ui.element('div').style(
                                    'display:flex; align-items:center; justify-content:space-between;'
                                    'padding: 9px 12px; margin: 3px 0; border-radius: 8px;'
                                    'cursor:pointer; border: 1px solid var(--nano-line);'
                                ).on('click', _make_handler(lbl)):
                                    with ui.element('div').style('min-width:0;'):
                                        ui.label(lbl).style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                                        if desc:
                                            ui.label(desc).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); margin-top:1px;')
                                    ui.label(str(i + 1)).style(
                                        'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); background:var(--nano-panel);'
                                        'border-radius:4px; padding:1px 6px; flex-shrink:0; margin-left:8px;'
                                    )
                            if allow_custom:
                                with ui.element('div').style('padding: 4px 2px 2px;'):
                                    _inp = ui.input(placeholder='或自行描述...').style(
                                        'width:100%; font-size:var(--nano-fs-md);'
                                    ).props('dense outlined')
                                    custom_input_ref[0] = _inp

                        with ui.element('div').style(
                            'display:flex; justify-content:flex-end; gap:6px;'
                            'padding: 8px 14px 10px; border-top: 1px solid var(--nano-line);'
                        ):
                            ui.button('Skip', on_click=_skip).props('flat dense').style(
                                'font-size:var(--nano-fs-base); color:var(--nano-dim);'
                            )
                            if allow_custom:
                                ui.button('Submit', on_click=_submit_custom).props('dense').style(
                                    'font-size:var(--nano-fs-base); background:var(--nano-amber); color:var(--nano-bg);'
                                    'border-radius:6px; padding:0 14px;'
                                )

    def _show_code_viewer_dialog(self, title: str, code: str):
        """只读代码查看窗（2026-08-26）。

        ⭐ 它**只负责给人看**，所以可以大 —— 而授权窗保持小且与 Skill 那个
           一模一样。📌 把「决策」和「取证」拆成两个窗，两边就都能是它该有的
           尺寸；挤在一个窗里的时候，两个需求会互相把对方压坏。

        🔴🔴 **只读**。理由不只是保险：**风险类别是 AST 在
           【这段代码】上扫出来的，用户一改，授权就和被授权的东西对不上了。**
           ⚠️ Skill 创建那边能改，是因为改完**还会再过一遍审计管线**；
              一次性执行**没有第二遍**。
           ⭐ 落地不靠自觉：`_cm_init(readonly=True)` 会让 `syncToPython` 一起
              关掉（「可编辑就必须同步」那条不变量的另一半），改了也传不回来。

        ⚠️ 挂载点必须带 `nano-cm-host` —— CodeMirror 的**全部**样式覆盖都 scope
           在它下面。漏掉它会一次性退回四个已经修好的问题：顶部浅灰条 /
           选中行高亮 / **左侧白条**（修过）/ 挂载时先压扁再撑开。
        ⭐ 额外加 `nano-cm-readonly`：只关掉选中行高亮与光标 —— 那两样在
           改不了的预览里没有意义，反而让人以为自己能编辑。
        """
        client = self._ui_client
        if client is None:
            return
        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dlg, \
                 ui.card().style(
                     'width:860px; max-width:96vw; padding:0; background:var(--nano-panel); '
                     'border:1px solid rgba(var(--nano-contrast-rgb), 0.08); '
                     'border-radius:14px; overflow:hidden;'
                 ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:12px 18px; background:var(--nano-panel-2); '
                    'border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06);'
                ):
                    with ui.row().style('align-items:center; gap:8px; min-width:0;'):
                        ui.icon('code').style('font-size:var(--nano-fs-4xl); color:var(--nano-fg-soft);')
                        with ui.column().style('gap:1px; min-width:0;'):
                            ui.label('将要运行的代码').style(
                                'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg);')
                            # ⚠️ 明说只读 —— 📌 一个长得像编辑器的东西，
                            #    不说清就会有人去改，然后发现改不动而困惑。
                            ui.label('只读 · 无法修改').style(
                                'font-size:var(--nano-fs-xs); color:var(--nano-dim);')
                    ui.button(icon='close').props('flat round dense').style(
                        'color:var(--nano-fg-soft);').on('click', dlg.close)
                _host = ui.element('div').classes(
                    'nano-cm-host nano-cm-readonly').style('width:100%;')
                ui.timer(0.05, lambda el=_host, c=code: (
                    asyncio.create_task(self._cm_init(el, c, readonly=True))
                ), once=True)
        dlg.open()

    def _show_execution_confirm_dialog(self, skill_name: str, side_effects: list,
                                        on_confirm, on_cancel,
                                        preview_code: str = ""):
        """副作用确认弹窗。比对话确认体验好。

        on_confirm / on_cancel 是回调函数,由 navigate_pipeline 传入。

        ⭐ `preview_code`（2026-08-26 新增）：临时执行通道会把**那段代码**
           一起带进来 —— 让用户知道**自己在授权什么**，而不是只看到
           「写入文件到磁盘」这一句抽象描述。
        🔴🔴 **它必须只读**。理由不只是保险：
           **风险类别是 AST 在【这段代码】上扫出来的，用户一改，授权就和
           被授权的东西对不上了** —— 可以把一段「无副作用」的代码改成写文件的，
           而弹窗上还挂着旧结论。**可编辑会让授权失去意义。**
           ⚠️ Skill 创建那边能改，是因为改完**还会再过一遍审计管线**
              （AST 校验 + SkillSpec 交叉验证）；一次性执行**没有第二遍**。
           ⭐ 落地上不靠自觉：`_cm_init(readonly=True)` 会让 `syncToPython`
              一起关掉（「可编辑就必须同步」那条不变量的另一半），改了也传不回来。
        """
        client = self._ui_client
        if client is None:
            return
        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:420px; max-width:94vw; padding:0; '
                     'background:var(--nano-panel); '
                     'border:1px solid rgba(var(--nano-warn-rgb),0.3); '
                     'border-radius:14px; overflow:visible;'
                 ):
                # 标题栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; gap:10px; '
                    'padding:14px 20px; '
                    'border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                    # 🔴 `flex-wrap:nowrap` 是 2026-08-26 补的：NiceGUI 的 `ui.row`
                    #    默认带 **`flex-wrap: wrap`**，所以左边那组一旦装不下，
                    #    最小化按钮就被**换行挤到下一排**（用户看到「按钮跑到了
                    #    奇怪的位置」）。
                    # ⚠️ 光给文字加省略号**不够**：省略号解决「文字太长」，
                    #    `nowrap` 解决「这一排放不下」—— 是两个机制。
                    #    📌 两条都要，缺任一条症状都还在。
                    'flex-wrap:nowrap; '
                    'background:var(--nano-panel-2); border-radius:14px 14px 0 0;'
                ):
                    # 🔴🔴 **这个槽位必须不可能被撑破。**
                    #    2026-08-26 实测翻车：副标题原来只放 Skill 名
                    #    （短，永远单行），而临时执行通道往里塞的是**模型写的
                    #    一句 purpose** ——「计算1到1000中所有能被7整除的数字的和，
                    #    并写入桌面的c2test.txt」直接折成两行 →
                    #    撑破这个 `ui.row` → **最小化按钮被挤到下一行**、
                    #    标题栏变高 → 背景色边界跟着移（用户看到的「奇怪条纹」）。
                    # 📌 **修法不是「让模型写短一点」** —— 那是把一个不变量
                    #    托付给我们控制不了的东西。是让这个槽位**装不下就省略号**。
                    # ⚠️⚠️ `min-width:0` 是关键：flex 子项默认 `min-width:auto`，
                    #    它**拒绝收缩到内容宽度以下**，于是 `text-overflow` 永远
                    #    不触发 —— 这一条不写，下面那三行样式全是摆设。
                    with ui.row().style(
                        'align-items:center; gap:10px; min-width:0; flex:1 1 auto;'
                    ):
                        ui.icon('warning_amber').style(
                            'font-size:var(--nano-fs-5xl); color:var(--nano-warn); flex-shrink:0;')
                        with ui.column().style('gap:2px; min-width:0; flex:1 1 auto;'):
                            ui.label('执行前确认').style(
                                'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-warn);'
                            )
                            # ⚠️ 临时代码不是 Skill，别写「Skill:」——
                            #    📌 一句不准确的标签会让用户去 Skill 抽屉里
                            #       找一个根本不存在的东西。
                            # ⭐ 截断不丢信息：完整那句在正文（副作用 + 代码窗）
                            #    里还看得到，鼠标悬停也有完整 tooltip。
                            ui.label(('即将运行：' + skill_name) if preview_code
                                     else f'Skill: {skill_name}').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); '
                                'white-space:nowrap; overflow:hidden; '
                                'text-overflow:ellipsis; max-width:100%;'
                            ).tooltip(skill_name)
                    _minimize = self._make_minimizable(client, dialog, f'执行确认待处理 · {skill_name}')
                    ui.button(icon='remove').props('flat round dense').style(
                        'color:var(--nano-fg-soft); flex-shrink:0;'
                    ).on('click', _minimize)

                # 副作用列表
                with ui.column().style(
                    # ⚠️ 同 OS 授权弹窗那处：`#0b0d14` 是冷调藏蓝，主题是暖褐。
                    #    📌 **同一个问题出现在两处，只修一处等于把它挪个地方**
                    #       （同「双算 bug 通常有两处症状」那条）。
                    'width:100%; padding:16px 20px; gap:8px; background:var(--nano-panel);'
                ):
                    ui.label('本次执行存在以下副作用:').style(
                        'font-size:var(--nano-fs-base); color:var(--nano-fg-soft);'
                    )
                    for se in side_effects:
                        with ui.row().style('align-items:center; gap:8px;'):
                            ui.element('div').style(
                                'width:5px; height:5px; border-radius:50%; '
                                'background:var(--nano-warn); flex-shrink:0;'
                            )
                            ui.label(se).style('font-size:var(--nano-fs-base); color:var(--nano-amber);')

                    # ⭐ 「查看代码」——**待在同一个 column 里**，
                    #    自然跟「本次执行存在以下副作用:」左对齐。
                    # 🔴 第一版另起了一个 `ui.row` + 自己的 padding，于是它既
                    #    不对齐、跟上面还空出一截。
                    # 📌 **要跟谁对齐，就待在谁的容器里** —— 用另一份 padding
                    #    去凑对齐，容器边距一改就再次错位。
                    # ⚠️ 放在副作用列表**正下方**而不是操作栏：
                    #    📌 它是**做这个决定的依据**，该跟它解释的东西待在一起；
                    #       混进「取消 / 确认执行」里会让人以为它也是一个动作。
                    if preview_code:
                        ui.button('查看将要运行的代码', icon='code').props(
                            'flat dense no-caps'
                        ).style(
                            'color:var(--nano-fg-soft); font-size:var(--nano-fs-sm); padding:0; '
                            'min-height:22px; align-self:flex-start;'
                        ).on('click', lambda c=preview_code, n=skill_name: (
                            self._show_code_viewer_dialog(n, c)))

                # ⭐ 代码**另开一个窗**（`_show_code_viewer_dialog`），
                #    授权窗里只放一个按钮（在上面那个 column 里）。
                # 🔴 第一版把 CodeMirror 内联进这个弹窗，实测一次暴露四个 UI
                #    问题，而更根本的是**它把两种授权窗变成了两个长相**。
                # ⭐⭐ 2026-08-26 定的形状：**两个授权窗长得一模一样**。
                #      · 一致性 —— 用户不用认两种授权窗
                #      · 两个弹窗都**小**（决策窗不该被一段长代码撑开）
                #      · 代码窗反而可以**大**，因为它只负责给人看
                #    📌 而它顺带干掉了「按行数算高度」那个补丁：
                #       弹窗尺寸不再取决于代码长度，那个问题从根上没了。

                # 操作栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:flex-end; gap:10px; '
                    'padding:12px 20px; '
                    'border-top:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                    'background:var(--nano-panel-2); border-radius:0 0 14px 14px;'
                ):
                    cancel_btn = ui.button('取消', icon='close').props('flat').style(
                        'color:var(--nano-fg-soft); font-size:var(--nano-fs-md);'
                    )
                    confirm_btn = ui.button('确认执行', icon='check').props(
                        'unelevated'
                    ).style(
                        'background:var(--nano-warn-fill); color:#fff; font-size:var(--nano-fs-md); '
                        'padding:0 16px; border-radius:8px;'
                    )

                    def _do_cancel():
                        dialog.close()
                        on_cancel()

                    def _do_confirm():
                        dialog.close()
                        on_confirm()

                    cancel_btn.on('click', _do_cancel)
                    confirm_btn.on('click', _do_confirm)

            dialog.open()

    def _build_skill_preview_dialog(self, filename: str, description: str, client):
        """构建 Skill 审计对话框骨架，code_area 为 CodeMirror 挂载 div（非 textarea）。"""
        code_holder = [""]
        # ⭐ [BUG1 · 2026-08-06 实测] **filename 必须是可变的**，不能让按钮闭包
        # 捕获建窗时那个值。
        #
        # 流式那条路是这样的：`skill_writing` 事件到达时就建弹窗，而那一刻
        # `step.get("filename", "...")` 拿到的可能是占位或早期名字；真实文件名要等
        # `skill_preview` 才确定，届时代码只 `set_text()` 改了标题 ——
        # **闭包里的旧名字没人更新**。
        # 多槽之后载荷是按【最终】文件名索引的，于是"验证并应用"
        # 拿旧名字去查，查不到 → 「部署失败：没有待审批的 Skill」。
        # 而最小化再展开之后就好了，因为 pinned card 传的是正确的 artifact_id。
        #
        # 用一个 list 当盒子，重命名时同步它，闭包读盒子而不是读快照。
        fn_box = [filename]
        # 最小化→展开时重建 CodeMirror 需要知道当前是否可编辑。
        # 流式写入阶段 readonly=True，代码写完转审计后变 False；
        # 重建时必须沿用当时的状态，否则展开后要么变成只读（用户改不了）、
        # 要么变成可编辑（写入中途就能改，会和后续 delta 打架）。
        cm_state = {"readonly": True}

        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:920px; max-width:96vw; max-height:92vh; padding:0; overflow:hidden; '
                     'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                     'box-shadow:0 18px 60px rgba(var(--nano-shade-rgb), 0.18); border-radius:16px;'
                 ):

                # ── 标题栏 ──
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:16px 24px; border-bottom:1px solid rgba(var(--nano-ink-rgb), 0.07); '
                    'background:var(--nano-panel-2); border-radius:16px 16px 0 0;'
                ):
                    with ui.row().style('align-items:center; gap:12px; min-width:0;'):
                        ui.html(NANO_AVATAR_SVG).style('width:28px; height:28px; flex-shrink:0;')
                        with ui.column().style('gap:2px; min-width:0;'):
                            title_lbl = ui.label(f'Skill 审计 — {filename}.py').classes('truncate').style(
                                'font-size:var(--nano-fs-md); font-weight:700; color:var(--nano-fg);'
                            )
                            desc_lbl = ui.label(description or '生成中...').classes('truncate').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-dim);'
                            )
                    with ui.row().style('align-items:center; gap:4px;'):
                        badge_icon_el = ui.icon('hourglass_top').style('font-size:var(--nano-fs-5xl); color:var(--nano-fg-soft);')
                        # [2026-08-05] 展开后重建 CodeMirror —— Quasar 关闭弹窗会卸载 DOM，
                        # JS 侧的编辑器实例随之消失，不重建就是一个空代码框。
                        # 内容取 Python 侧的 code_holder（auto-index 下 _cm_get_value
                        # 永远走它的 fallback，所以它本来就是权威）。
                        async def _restore_cm(_area=None):
                            await asyncio.sleep(0.15)   # 等 dialog DOM 重新挂上
                            await self._cm_init(code_area, code_holder[0] or "",
                                                readonly=cm_state["readonly"])

                        # ⭐ [⑤ · 2026-08-06] 审计弹窗**不再创建悬浮条**。
                        #
                        # 实测撞出来的：pinned card 落地后，最小化会同时出现
                        # 两个"待处理"入口（卡片 + 右下角悬浮条），而且点丢弃之后
                        # 悬浮条还卡在屏幕上不走。
                        #
                        # 根因是没想清楚职责：**pinned card 本身就是"最小化后的入口"**，
                        # 它常驻、跨重启、按 SQLite 重建。悬浮条是它的前身，
                        # 一次只管一个弹窗、纯内存、靠 `_restore()` 自删。
                        # 两个并存不只是难看 —— 从卡片点 `<>` 会**新建**一个 dialog，
                        # 于是旧悬浮条变成孤儿，永远没人删它。
                        #
                        # 所以这里传 `chip=False`：收起就是收起，重新打开走 pinned card。
                        # `_make_minimizable` 的悬浮条对**其它**弹窗（OS 授权、副作用确认）
                        # 仍然有效 —— 那些还没有卡片可挂。
                        _minimize = self._make_minimizable(
                            client, dialog, f'Skill审计待处理 · {filename}',
                            on_restore=_restore_cm, chip=False,
                        )
                        # 存引用给 pinned card 复用（见 `_reopen_pending_audit`）
                        # 按 filename 索引 —— 单槽会让第二个弹窗顶掉第一个的引用，
                        # 之后从卡片点开第一个只能走"新建"那条退路。
                        if not hasattr(self, "_audit_dialog_refs"):
                            self._audit_dialog_refs = {}
                        self._audit_dialog_refs[filename] = {
                            "filename": filename, "dialog": dialog,
                            "on_restore": _restore_cm,
                        }
                        ui.button(icon='remove').props('flat round dense').style(
                            'color:var(--nano-fg-soft);'
                        ).on('click', _minimize)

                # ── 内容区 ──
                with ui.column().style(
                    'width:100%; padding:18px 24px 16px 24px; gap:10px; '
                    'background:var(--nano-panel); max-height:calc(92vh - 122px); overflow:auto;'
                ):
                    with ui.row().style(
                        'width:100%; align-items:center; justify-content:space-between;'
                    ):
                        code_lbl = ui.label('生成代码 — 正在写入，暂不可编辑').style(
                            'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                        )
                        ui.label('CodeMirror 6 · Python').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft); letter-spacing:0.04em;'
                        )

                    # CodeMirror 挂载点
                    code_area = ui.element('div').classes('nano-cm-host').style('width:100%;')

                    def _on_cm_change(e):
                        try:
                            # ⚠️ 三种载荷形状都要吃：
                            #   · `detail`  —— DOM CustomEvent（**当前真正走的那条**，
                            #                  见 emitChange 里那段注释）
                            #   · 纯字符串  —— Vue `$emit` 那条（本版本用不到，留着不碍事）
                            #   · `value`   —— 全局 emitEvent 那条
                            # 少认一种，改动就静默丢失且不报错 —— 这个 bug 已经吃过一次亏。
                            if isinstance(e.args, str):
                                code_holder[0] = e.args
                            elif isinstance(e.args, dict) and "detail" in e.args:
                                code_holder[0] = e.args["detail"]
                            elif isinstance(e.args, dict) and "value" in e.args:
                                code_holder[0] = e.args["value"]
                            else:
                                logger.warning(
                                    f"[UI] cm_change 载荷形状不认识，改动被丢弃: "
                                    f"{type(e.args).__name__} {str(e.args)[:80]}"
                                )
                                return
                            # ⭐⭐ [2026-08-06 实测] 同步回**待审载荷**，不只是 code_holder。
                            #
                            # 改造前只写 `code_holder[0]`，而 `_pending_skill["code"]`
                            # **只在「验证并应用」按钮那条路里**才被写回。于是走
                            # "打字说部署"（②b 新加的模型工具路径）时：
                            #   · 用户在编辑器里的修改**被静默丢弃**，装上去的是改之前那版；
                            #   · 而 `verify_artifact` 比对的也是那份过期副本 →
                            #     原始 vs 原始 → 一致 → 放行。
                            # **指纹守卫在那条路上因此等于失效** ——
                            # 它永远比不出差异，因为差异从来没进到它比对的副本里。
                            #
                            # 实测实测：改了一行 `import os`，Nano 说"已部署"，
                            # 装上去的文件里没有那行，用户完全没有信号。
                            #
                            # ⚠️ 只在**可编辑**状态回写。流式写入阶段（readonly=True）
                            # CodeMirror 也会触发 cm_change（模型正往里灌代码），
                            # 那时回写是无意义的，还会和后续 delta 打架。
                            if cm_state.get("readonly"):
                                return
                            _own = self.agent._get_pending_skill(fn_box[0])
                            if _own is not None:
                                _own["code"] = code_holder[0]
                        except Exception as ex:
                            logger.warning(f"[UI] CodeMirror 内容同步失败: {ex}")

                    # ⚠️ `['detail']` 不能省 —— 不声明要哪些字段，NiceGUI 只会把
                    # DOM 事件的通用属性传回来，`detail`（我们真正的载荷）拿不到。
                    code_area.on('cm_change', _on_cm_change, ['detail'], throttle=0.3)

                    validation_lbl = ui.label('⏳ 等待代码生成完成...').style(
                        'font-size:var(--nano-fs-sm); margin-top:8px; color:var(--nano-fg-soft);'
                    )

                # ── 操作栏 ──
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 24px; border-top:1px solid rgba(var(--nano-ink-rgb), 0.07); '
                    'background:var(--nano-panel-2); border-radius:0 0 16px 16px;'
                ):
                    ui.label('编辑后点击「验证并应用」会重新校验').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);'
                    )
                    with ui.row().style('gap:10px; align-items:center;'):
                        discard_btn = ui.button('丢弃', icon='delete_outline').props('flat').style(
                            'color:var(--nano-fg-soft); font-size:var(--nano-fs-md);'
                        )
                        # 闭包捕获**这个弹窗**的 filename，多条待审并存时才不会丢错人
                        discard_btn.on(
                            'click', lambda: self._on_discard_skill(dialog, fn_box[0]))
                        apply_btn = ui.button('验证并应用', icon='check_circle_outline').props(
                            'unelevated color=positive'
                        ).style('font-size:var(--nano-fs-md); padding:0 18px; border-radius:10px;').props('disabled')

                        async def _apply_click(e):
                            await self._on_apply_skill(
                                dialog, code_holder, validation_lbl, code_area, e,
                                filename=fn_box[0])

                        apply_btn.on('click', _apply_click)

        # cm_state 一并返回：调用方切换 readonly / 灌入代码时必须同步它，
        # 否则最小化后展开、重建出来的编辑器状态是错的。
        return (dialog, code_holder, code_area, validation_lbl, apply_btn,
                badge_icon_el, code_lbl, title_lbl, desc_lbl, cm_state, fn_box)

    async def _show_skill_preview(self, filename: str, code: str, description: str,
                                  validation_ok: bool = True,
                                  validation_summary: str = "",
                                  validation_errors: list = None):
        """非流式路径：代码已全部生成，一次性打开完整审计对话框。"""
        validation_errors = validation_errors or []
        client = self._ui_client
        if client is None:
            logger.error("[UI] _show_skill_preview: _ui_client 未初始化")
            return

        (dialog, code_holder, code_area, validation_lbl, apply_btn,
         badge_icon_el, code_lbl, _tl, _dl, cm_state, _fn_box) = \
            self._build_skill_preview_dialog(filename, description, client)

        with self._ui_scope():
            dialog.open()

        await asyncio.sleep(0.05)
        await self._cm_init(code_area, code, readonly=False)
        code_holder[0] = code
        cm_state["readonly"] = False   # 审计阶段可编辑，最小化后重建要沿用

        with self._ui_scope():
            code_lbl.set_text('生成代码 — 可在此直接编辑')
            badge_icon  = 'check_circle' if validation_ok else 'warning'
            badge_color = 'var(--nano-ok)' if validation_ok else 'var(--nano-warn)'
            badge_icon_el.name = badge_icon
            badge_icon_el.style(f'font-size:var(--nano-fs-5xl); color:{badge_color};')
            if not validation_ok:
                validation_lbl.set_text('⚠ ' + '  ·  '.join(validation_errors))
                validation_lbl.style('color:var(--nano-warn)')
            else:
                validation_lbl.set_text('✔ 通过协议 v3.2 全部校验，可以部署')
                validation_lbl.style('color:var(--nano-ok)')
            apply_btn.props(remove='disabled')

    def _validate_skill_code(self, code: str, label_el) -> bool:
        ok, errors = self.agent.validate_skill_code(code)
        if not ok:
            label_el.set_text("⚠ " + "  ·  ".join(errors))
            label_el.style('color:var(--nano-danger)')
        else:
            label_el.set_text("✔ 通过协议 v3.2 全部校验，可以部署")
            label_el.style('color:var(--nano-ok)')
        return ok

    def _on_discard_skill(self, dialog, filename: str | None = None):
        # 带上**这个弹窗自己那份**的 filename。多条待审并存时，
        # 不传就会丢掉"最近那条"—— 而用户点的可能是更早那个弹窗上的按钮。
        result = self.agent.cancel_pending_skill(filename)
        dialog.close()
        ui.notify(result["msg"], type='warning', icon='delete')
        discard_msg = result["msg"] + "（未部署）"
        # 写进 memory，让 Nano 知道这个 Skill 已被丢弃，不要再引用它
        self.agent.memory.add_system_note(
            "assistant",
            f"[System record: the user discarded the pending Skill in the UI; it was not deployed.] {result.get('msg', '')}"
        )
        with self.chat_container:
            with ui.row().classes('items-center gap-2 px-2 mb-6'):
                ui.icon('delete_outline').style('font-size:var(--nano-fs-lg); color:var(--nano-fg-soft);')
                ui.label(discard_msg).style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); font-style:italic;'
                )
        self.scroll_area.scroll_to(percent=1.0, duration=0.1)

    async def _on_apply_skill(self, dialog, code_holder: list, validation_lbl, code_area=None,
                              event_args=None, filename: str | None = None):
        current_code = await self._cm_get_value(code_area, fallback=code_holder[0], event_args=event_args)
        code_holder[0] = current_code
        # 把用户在编辑器里改过的代码写回**这一份**载荷，不是"最近那条"。
        # 写错人的后果很实在：用户改的是 A，改动却落到了 B 上。
        _own = self.agent._get_pending_skill(filename)
        if _own is not None:
            _own["code"] = current_code
        if not self._validate_skill_code(current_code, validation_lbl):
            ui.notify('代码存在问题，请修复后再部署', type='negative')
            return
        self._suppress_skill_watcher_until = time.time() + 2.5
        result = self.agent.apply_pending_skill(filename)
        if result["ok"]:
            dialog.close()
            if code_area is not None:
                self._js_fire(f"""
                (() => {{
                    try {{
                        if (window.NanoCM && window.NanoCM.destroy) {{
                            window.NanoCM.destroy({code_area.id});
                        }}
                    }} catch(e) {{ console.error('[NanoCM] destroy failed:', e); }}
                }})();
                """)
            ui.notify(result["msg"].split("\n\n")[0], type='positive', icon='check_circle')
            self.refresh_skill_list()
            apply_msg = result["msg"].split("\n\n")[0] + "（已部署）"
            self.agent.memory.add_system_note(
                "assistant",
                f"[System record: a pending Skill was deployed from the UI.] {result['msg'].split(chr(10) + chr(10))[0]}"
            )
            with self.chat_container:
                with ui.row().classes('items-center gap-2 px-2 mb-6'):
                    ui.icon('check_circle_outline').style('font-size:var(--nano-fs-lg); color:var(--nano-ok);')
                    ui.label(apply_msg).style(
                        'font-size:var(--nano-fs-base); color:var(--nano-ok); font-style:italic;'
                    )
            self.scroll_area.scroll_to(percent=1.0, duration=0.1)
        else:
            ui.notify(f"部署失败: {result['msg']}", type='negative')
    
    def _build_koala_state(self) -> dict:
        """给考拉头像用的状态读取器。"""
        try:
            return {
            "status":        self.status_lbl.text if self.status_lbl else "SYS_IDLE",
            "current_skill": self._koala_current_skill,
            "rag_hit":       (self.rag_lbl.text == "HIT") if self.rag_lbl else False,
            "full_file_hit": (self.full_file_lbl.text == "HIT") if self.full_file_lbl else False,
            "net_crash":     bool(getattr(self, "_net_crashed", False)),
            "model": self.model_lbl.text if self.model_lbl else "",
           } 
        except Exception:
            return {"status": "SYS_IDLE"}

    # 三级，不是二值。语义与取值都在 `MCPManager.web_status`，这里只画。
    # ⚠️ **别在这里重新判断一次** —— 判据只能有一处，两处迟早分叉。
    _NET_COLORS = {
        "ONLINE":  "var(--nano-ok)",   # 绿：找 + 读都在
        "LIMITED": "var(--nano-warn)",   # 琥珀：只剩一层（能读不能搜，或反过来）
        "OFFLINE": "var(--nano-fg-mute)",   # 灰：连读都没有
    }

    def _update_net_status(self):
        """刷新"互联网检索"卡片。

        🔴 曾经是二值绿灯，判据是**关键词猜测**（扫已连接 MCP 工具的名字+描述
           撞 11 个词）。那个判断在 playwright 连上时永远为真，所以这张卡
           实际上是**常绿**的 —— 它从没告诉过用户任何事。
        ⭐ 现在读 `web_status()`：找（SearchTheWeb Skill）+ 读（声明了 web.fetch
           的已连接 server）分开看，缺一层是琥珀不是灰。
        📌 缺「找」和缺「读」后果完全不同（前者 = 只能读用户给的 URL），
           一个绿/灰二值把这个区别抹平了。
        """
        try:
            from core.mcp_client import get_mcp_manager
            state = get_mcp_manager().web_status()
        except Exception:
            state = "OFFLINE"
        # 只在状态变化时才更新 UI——避免每 3 秒无脑 patch 客户端（会搅扰正在编辑的输入框等）
        if getattr(self, "_net_online_state", None) == state:
            return
        self._net_online_state = state
        _c = self._NET_COLORS.get(state, self._NET_COLORS["OFFLINE"])
        if getattr(self, "net_lbl", None):
            self.net_lbl.set_text(state)
            self.net_lbl.style(f'font-size:var(--nano-fs-sm); color:{_c}; font-weight:500;')
        if getattr(self, "net_dot", None):
            self.net_dot.style(f'width:6px; height:6px; border-radius:50%; background:{_c}; flex-shrink:0;')

    def _clear_pending_image(self):
        """清除待发送的图片。"""
        self._pending_image_bytes = None
        self._pending_image_mime = "image/jpeg"
        if self._image_preview_container:
            self._image_preview_container.style('display:none;')

    def _refresh_temp_file_badge(self):
        """刷新临时文件列表显示。"""
        if not self._temp_file_badge:
            return
        self._temp_file_badge.clear()
        if not self._temp_files:
            self._temp_file_badge.style('display:none;')
            return
        self._temp_file_badge.style('display:flex;')
        with self._temp_file_badge:
            ui.label('附件:').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
            for f in self._temp_files:
                status = f.get("status", "ready")
                if status == "indexing":
                    bg, border, color = 'rgba(var(--nano-warn-rgb),0.1)', 'rgba(var(--nano-warn-rgb),0.25)', 'var(--nano-warn)'
                    icon = 'hourglass_top'
                elif status == "error":
                    bg, border, color = 'rgba(var(--nano-danger-rgb),0.1)', 'rgba(var(--nano-danger-rgb),0.25)', 'var(--nano-danger)'
                    icon = 'error_outline'
                else:
                    bg, border, color = 'rgba(var(--nano-amber-rgb), 0.1)', 'rgba(var(--nano-amber-rgb), 0.2)', 'var(--nano-fg-soft)'
                    icon = 'description'
                with ui.row().classes('items-center gap-1 rounded-lg px-2 py-0.5').style(
                    f'background:{bg}; border:1px solid {border};'
                ):
                    if status == "indexing":
                        ui.spinner(size='xs').style(f'color:{color};')
                    else:
                        ui.icon(icon).style(f'font-size:var(--nano-fs-sm); color:{color};')
                    label_text = f["filename"]
                    if status == "indexing":
                        label_text += " (处理中)"
                    elif status == "error":
                        label_text += " (失败)"
                    ui.label(label_text).style(f'font-size:var(--nano-fs-xs); color:{color}; ')
                    ui.button(
                        icon='close',
                        on_click=lambda fn=f["filename"]: self._remove_temp_file(fn)
                    ).props('flat round dense size=xs').classes('text-slate-600 hover:text-rose-400')

    def _remove_temp_file(self, filename: str):
        """从临时知识库移除单个文件。

        防御性：发消息后 _temp_files 已被 start_pipeline_task 清空，
        此时 X 被触发应直接 return，不能删 collection 里正在被检索的数据。
        ui.notify 包 try/except 防止父元素被删除后的 RuntimeError。
        """
        if not any(tf["filename"] == filename for tf in self._temp_files):
            return
        self._temp_files = [f for f in self._temp_files if f["filename"] != filename]
        try:
            rag_engine.remove_temp_file(filename)
        except Exception:
            pass
        self._refresh_temp_file_badge()
        try:
            ui.notify(f'已移除: {filename}', type='info')
        except Exception:
            pass

    # ── Pipeline ──────────────────────────────────────────────────────────

    async def navigate_pipeline(self, query, loading_container, image_bytes: bytes | None = None, image_mime: str = "image/jpeg", temp_file_hint: str | None = None, thought_blocks_container=None, event_source=None):
        # event_source: 挂起唤醒用——给定时/后台唤醒复用整套事件渲染逻辑。
        # 不传时走常规 self.agent.handle_query(query)；传入时直接消费该异步生成器。
        # ⭐⭐⭐ [无缝对话] 续接的那一段**不重置用量、不新建 nano 块**。
        #
        # ⚠️ `reset_session()` 原来无条件调 —— 续接时会把上一段的 token **清零**，
        #    而 已明确要求「token 计数器不能出现两个，也得出现在末尾」，
        #    那意味着它统计的是**整段回应期**，不是最后一个子轮。
        # ⚠️ 即读即清：这个标志只在「下一次调用的开头」有意义。
        _seam_cont = bool(getattr(self, "_resp_continuation", False))
        self._resp_continuation = False
        if not _seam_cont:
            usage_tracker.reset_session()
        current_session_skill = None
        # 构建图片 parts（如果有）
        _image_parts = None
        if image_bytes:
            _image_parts = [self.provider.build_image_part(image_bytes, image_mime)]
            # ⚠️ 图**原样进 memory**，一个字节都不删 —— `attach_user_images`
            #    是「图片进入账本的唯一入口」（登记 handle + 落盘），
            #    `view_past_image` 的回看能力整根挂在它上面。
            #    🔴 2026-08-31 这里写过 `_image_parts = None` 改走文字描述，
            #       顺手掐了 handle 登记 ⇒ 回看能力没了（实测抓到）。
            #    主模型没视觉时怎么办 → 见 orchestrator 的到达轮视觉兜底，
            #    那里走既有的 `_vision_ask` 轨道，不在这里另起一套。


        # 重置所有技能状态圆点
        for elements in self.skill_ui_elements.values():
            elements["status"].set_text("READY")
            elements["status"].style('font-size:var(--nano-fs-2xs); color:var(--nano-dim); letter-spacing:0.04em; margin-right:4px;')
            try:
                elements["icon"].style(
                    'width:7px; height:7px; border-radius:50%; background:var(--nano-dim); flex-shrink:0; transition:background 0.3s;'
                )
            except Exception:
                pass
        # 回看会起一个新 pipeline，但被交还的载体属于更长的生命周期。
        # 不能因为这轮刚开始就把仍在运行的本地 Skill 擦成 READY。
        self._refresh_handed_back_skill_statuses()

        # 本回应期共享状态（由 start_pipeline_task 在 send_message 里初始化）
        _rs = self._resp_state
        if _seam_cont:
            # ⭐⭐⭐ **续接时是「复用」还是「新开」，由【那个元素现在有没有内容】决定。**
            #
            # 🔴 **这里原来无条件新开一个 markdown —— 那造出了 实测看到的
            #    「nano ❯ 和文字对不齐」**：
            #    插话时上一段被 `set_content("")` **擦空了、但没有被移除**，
            #    而它的 style 带 `min-height:1em` —— **空着也占一行**。
            #    新段落在它下面 → 文字比 `nano ❯` 低一行。
            #    📌 **擦掉内容 ≠ 移除元素。**
            #
            # ⭐ 而「无条件新开」本身是**「执行分段」时代的产物**，那个设计已被推翻：
            #    现在续接总是跟在一个**被撤回**的段后面，旧元素里没东西可保护。
            # ⚠️ **但仍有一格例外**：用户插话落在「最后一个 stream 事件之后、
            #    `final_result` 之前」那个窗口里 → 检查点都没赶上 → 上一段**正常答完了**、
            #    有内容。这时候必须新开，否则 `final_result` 的整体覆盖会**擦掉它**。
            #
            # 📌 所以判据是**看现在的状态**，不是**追踪从哪条路来的** ——
            #    与本项目其它地方同一条纪律：
            #    **别追踪「我是从哪来的」，直接问「现在是什么样」。**
            try:
                _prev_has_text = bool((_rs.get("current_text") or "").strip())
                with self._ui_scope():
                    if _prev_has_text:
                        # 上一段有内容 → 新开一段，别覆盖它
                        with _rs["loading_col"]:
                            _rs["content_md"] = nano_md(
                                style='color:var(--nano-fg); min-height:1em; margin-top:6px;')
                    else:
                        # 上一段是空的（被撤回）→ **复用它**，别再叠一个空占位行
                        try:
                            _rs["content_md"].set_content("")
                        except Exception:
                            pass
                _rs["current_text"] = ""
                _rs["waiting_for_carrier"] = False
                _rs["running"] = True
                # 恢复转圈：上一段可能已经把它藏了
                if _rs.get("spin_lbl"):
                    _rs["spin_lbl"].set_visibility(True)
                if _rs.get("svg_el"):
                    _rs["svg_el"].style('display:none;')
            except Exception as _e_sc:
                logger.warning(f"[Seam] 续接准备失败（退化成覆盖同一元素）: {_e_sc}")
        # One response epoch owns one status timer. A carrier completion may resume this
        # same epoch; replace the old timer before starting the continuation so two tasks
        # never race to render one metadata row.
        _old_timer_task = _rs.get("_status_timer_task")
        if _old_timer_task is not None:
            try:
                _old_timer_task.cancel()
            except Exception:
                pass
        _timer_task = asyncio.create_task(self._resp_status_timer(_rs))
        _rs["_status_timer_task"] = _timer_task

        _stream = event_source if event_source is not None else self.agent.handle_query(query, image_parts=_image_parts, temp_file_hint=temp_file_hint)
        async for step in _stream:

            # ── 统一文字流：思考文字和最终答案同字体同样式直接流入内容区 ───
            # thought_block_start / thought_block_done / thought_summary 不再创建
            # 独立可折叠块——所有文字（思考 + 最终答案）都追加到同一个 markdown
            # 元素里，实现"思考流和回答浑然一体"的体验。
            if step.get("event") in ("thought_block_start", "thought_block_done", "thought_summary"):
                continue

            if step.get("event") == "thought_delta":
                # 内部扩展思考不显示在聊天区，状态栏计时器已给足反馈
                continue

            # final_text_start：保存回滚检查点（以防 discard），不创建新容器
            if step.get("event") == "final_text_start":
                _rs["text_checkpoint"] = _rs["current_text"]
                continue

            if step.get("event") == "final_text_delta":
                # 答案文字开始流出 = 工具阶段真的结束了 → 此刻才把 pill 定型
                self._settle_tool_pill(_rs)
                _rs["current_text"] += step.get("delta", "")
                _rs["had_text_since_tool"] = True
                with self._ui_scope():
                    try:
                        _rs["content_md"].set_content(_rs["current_text"] + _STREAM_CURSOR)
                    except Exception:
                        pass
                # 🔴 不能用 `scroll_to(percent=…, duration=…)`：
                #    set_content 重渲染的一瞬 scrollHeight 会塌，percent 算出来≈顶部；
                #    带时长的动画又会被下一个 delta 打断、互相抢。
                #    两者叠起来就是 2026-09-01 看到的"疯狂上下飞"。
                self._pin_chat_bottom()
                continue

            # final_text_discard：统一流设计里不需要撤销。
            # 原因：思考流和最终答案外观相同，Claude 先流了一段文字再决定调工具，
            # 这段文字对用户来说就是推理过程，应当保留，不要清掉。
            if step.get("event") == "final_text_discard":
                continue

            # final_text（一次性，非流式）：_generate_skill_update 等处发出的前置发言
            if step.get("event") == "final_text":
                _delta = step.get("delta") or step.get("content") or ""
                if _delta:
                    _rs["current_text"] += _delta
                    _rs["had_text_since_tool"] = True
                    with self._ui_scope():
                        try:
                            _rs["content_md"].set_content(_rs["current_text"] + _STREAM_CURSOR)
                        except Exception:
                            pass
                    self._pin_chat_bottom()   # 同上：流式路径不许用带动画的 percent
                continue

            # text_replace：用干净的文本覆盖当前流式内容（例如探索阶段结束后抹掉 [PROCEED]）
            if step.get("event") == "text_replace":
                _new_content = step.get("content", "")
                _rs["current_text"] = _new_content
                with self._ui_scope():
                    try:
                        _rs["content_md"].set_content(_new_content)
                    except Exception:
                        pass
                continue

            # ── 流式代码生成：对话框提前弹出（锁定，空代码区）──
            if step.get("event") == "skill_code_start":
                _sk_fn   = step.get("filename", "...")
                _sk_desc = step.get("description", "生成中...")
                self._sk_stream_accumulated = ""
                self._sk_stream_last_push = 0
                self._sk_cm_ready = False  # CodeMirror 初始化完成前只累积不推送
                client = self._ui_client
                if client:
                    res = self._build_skill_preview_dialog(_sk_fn, _sk_desc, client)
                    (self._sk_stream_dialog, self._sk_stream_code_holder,
                     self._sk_stream_code_area, self._sk_stream_validation_lbl,
                     self._sk_stream_apply_btn, self._sk_stream_badge_icon_el,
                     self._sk_stream_code_lbl, self._sk_stream_title_lbl,
                     self._sk_stream_desc_lbl, self._sk_stream_cm_state,
                     self._sk_stream_fn_box) = res
                    # 先给 preface 文字一个绘制窗口再开弹窗——否则模态框命令
                    # 和 preface 的 markdown diff 背靠背走 websocket，弹窗常抢先
                    # 画出来，造成"Nano 的话还没出现审计 UI 就弹了"。
                    await asyncio.sleep(0.05)
                    with self._ui_scope():
                        self._sk_stream_dialog.open()
                    self.status_lbl.set_text(f"WRITING: {_sk_fn}")
                    # 等 dialog DOM 挂载后初始化 CodeMirror，不阻塞主流。
                    # 初始化期间到达的代码 delta 只累积不推送（见 skill_code_delta
                    # 的 _sk_cm_ready 门控）；初始化完成时把已累积的全量灌进去，
                    # 再标记 ready 开始增量——避免早期代码丢失导致"从一半开始流式"。
                    _cm_area = self._sk_stream_code_area
                    async def _init_cm():
                        await asyncio.sleep(0.15)
                        await self._cm_init(_cm_area, "", readonly=True)
                        _acc_now = self._sk_stream_accumulated
                        if _acc_now:
                            try:
                                await self._cm_append(_cm_area, _acc_now)
                            except Exception as _e:
                                logger.warning(f"[UI] CodeMirror 初始全量灌入失败: {_e}")
                        self._sk_stream_last_push = len(_acc_now)
                        self._sk_cm_ready = True
                    asyncio.ensure_future(_init_cm())
                    self.status_lbl.style('color:var(--nano-amber); font-size:var(--nano-fs-sm);')
                continue

            # ── 流式代码增量填入 ──
            if step.get("event") == "skill_code_delta":
                _delta = step.get("delta", "")
                if not _delta:
                    continue
                self._sk_stream_accumulated += _delta
                # CodeMirror 还没初始化完成：只累积，等 _init_cm 完成后全量灌入。
                # 不在这里推送（也不前移指针），否则早期 delta 会丢。
                if not getattr(self, "_sk_cm_ready", False):
                    continue
                # 节流：每 40 字符推一次，推"还没推过的 delta"，不是完整 accumulated
                _acc_len = len(self._sk_stream_accumulated)
                _last_push = getattr(self, "_sk_stream_last_push", 0)
                if self._sk_stream_code_area and (_acc_len - _last_push >= 40):
                    _delta_to_push = self._sk_stream_accumulated[_last_push:_acc_len]
                    try:
                        await self._cm_append(self._sk_stream_code_area, _delta_to_push)
                        self._sk_stream_last_push = _acc_len  # 仅在 append 成功后前移指针
                    except Exception as _e:
                        logger.warning(f"[UI] CodeMirror 流式追加失败: {_e}")
                continue

            # ── Skill 预览弹窗 ──
            if step.get("event") == "skill_preview":
                _filename    = step["filename"]
                _code        = step["code"]
                _description = step["description"]

                _rs["running"] = False
                try:
                    _timer_task.cancel()
                except Exception:
                    pass
                # 不删 loading_container——探索阶段产出的文字应留在聊天记录里，
                # 用户在审批弹窗里能看到上下文，部署后也有完整的会话记录。
                _elapsed_preview = int(time.time() - _rs["start_time"])
                with self._ui_scope():
                    try:
                        _rs["status_lbl"].set_text(f"Nano · {_elapsed_preview}s")
                        _rs["status_lbl"].style(
                            'font-size:var(--nano-fs-base); color:var(--nano-dim); font-style:normal; letter-spacing:0.01em;'
                        )
                    except Exception:
                        pass

                # 光标残留修复：三个"终端事件"里 final_result 和 user_note_pending
                # 都会用干净的 set_content 把流式光标覆盖掉，唯独 skill_preview 从头到尾
                # 没碰过 content_md 就往下走——而在它之前 final_text（SkillWriter 的开场白）
                # 刚执行过 set_content(current_text + _STREAM_CURSOR) 把光标加上去。
                # 于是审计弹窗一弹，上方那句话末尾就永久留着一个闪烁的 ▋。
                # v1.1 修过同类问题，但那次只覆盖了 tool_start 这条路径。
                with self._ui_scope():
                    try:
                        _rs["content_md"].set_content(_rs.get("current_text", "") or "")
                    except Exception:
                        pass

                self.scroll_area.scroll_to(percent=1.0, duration=0.2)
                self.status_lbl.set_text(f"AWAITING: {_filename}")
                self.status_lbl.style('color:var(--nano-amber); font-size:var(--nano-fs-sm);')
                self.log_lbl.set_text(step.get("log", "等待用户审批..."))

                _validation_ok      = step.get("validation_ok", True)
                _validation_summary = step.get("validation_summary", "")
                _validation_errors  = step.get("validation_errors", [])

                if self._sk_stream_dialog:
                    # 1) 补最后不足 40 字符、尚未 append 的尾巴
                    _last_push = getattr(self, "_sk_stream_last_push", 0)
                    if self._sk_stream_accumulated and len(self._sk_stream_accumulated) > _last_push:
                        _tail = self._sk_stream_accumulated[_last_push:]
                        try:
                            await self._cm_append(self._sk_stream_code_area, _tail)
                        except Exception as _e:
                            logger.warning(f"[UI] CodeMirror 尾巴追加失败: {_e}")
                        self._sk_stream_last_push = len(self._sk_stream_accumulated)

                    # 2) 最终权威同步 + 解锁编辑
                    try:
                        await self._cm_set_value(self._sk_stream_code_area, _code)
                        await self._cm_set_editable(self._sk_stream_code_area, True)
                        self._sk_stream_code_holder[0] = _code
                        # 同步给"最小化→展开"的重建路径，否则展开后会变回只读
                        if getattr(self, "_sk_stream_cm_state", None) is not None:
                            self._sk_stream_cm_state["readonly"] = False
                    except Exception as _e:
                        logger.warning(f"[UI] CodeMirror 最终同步失败: {_e}")

                    # 3) 更新 UI 状态
                    with self._ui_scope():
                        try:
                            self._sk_stream_code_lbl.set_text('生成代码 — 可在此直接编辑')
                            # ⭐ [BUG1] 真实文件名到手 → **同步闭包用的那个盒子**。
                            # 只改标题不改盒子，按钮就会拿建窗时的旧名字去查载荷。
                            if getattr(self, "_sk_stream_fn_box", None) is not None:
                                self._sk_stream_fn_box[0] = _filename
                            if self._sk_stream_title_lbl:
                                self._sk_stream_title_lbl.set_text(f'Skill 审计 — {_filename}.py')
                            if self._sk_stream_desc_lbl:
                                self._sk_stream_desc_lbl.set_text(_description or '')
                            _bi = 'check_circle' if _validation_ok else 'warning'
                            _bc = 'var(--nano-ok)' if _validation_ok else 'var(--nano-warn)'
                            self._sk_stream_badge_icon_el.name = _bi
                            self._sk_stream_badge_icon_el.style(f'font-size:var(--nano-fs-5xl); color:{_bc};')
                            if _validation_ok:
                                self._sk_stream_validation_lbl.set_text('✔ 通过协议 v3.2 全部校验，可以部署')
                                self._sk_stream_validation_lbl.style('color:var(--nano-ok)')
                            else:
                                self._sk_stream_validation_lbl.set_text('⚠ ' + '  ·  '.join(_validation_errors or []))
                                self._sk_stream_validation_lbl.style('color:var(--nano-warn)')
                            self._sk_stream_apply_btn.props(remove='disabled')
                        except Exception as _e:
                            logger.warning(f"[UI] 流式对话框状态更新失败: {_e}")

                    # 4) 清空流式状态；code_area 已被 apply_btn 闭包持有，不影响部署读取
                    self._sk_stream_dialog = None
                    self._sk_stream_code_area = None
                    self._sk_stream_code_holder = None
                    self._sk_stream_validation_lbl = None
                    self._sk_stream_apply_btn = None
                    self._sk_stream_badge_icon_el = None
                    self._sk_stream_code_lbl = None
                    self._sk_stream_title_lbl = None
                    self._sk_stream_desc_lbl = None
                    self._sk_stream_cm_state = None
                    self._sk_stream_accumulated = ""
                    self._sk_stream_last_push = 0
                else:
                    # 非流式路径：也走 CodeMirror 版本
                    await self._show_skill_preview(_filename, _code, _description,
                                                   _validation_ok, _validation_summary, _validation_errors)
                return

            # ── 选择卡片 ─────────────────────────────────────────────
            if step.get("event") == "user_choice_request":
                # 支持一次弹出多张选择卡片（cards 列表）；向后兼容单卡（顶层 question/choices）
                _cards = step.get("cards")
                if not _cards:
                    _cards = [{
                        "question": step.get("question", "请选择"),
                        "choices": step.get("choices", []),
                        "allow_custom": step.get("allow_custom", True),
                        "on_choice": step.get("on_choice"),
                        "on_dismiss": step.get("on_dismiss"),
                    }]
                try:
                    if self._current_loading_label:
                        self._current_loading_label.set_text('等待选择...')
                except Exception:
                    pass
                # 多卡片：一次只显示一张，答完一张自动出下一张（带 1/X 计数），
                # 不一次性全堆叠（学 Claude Code 的逐张作答体验）。
                _total = len(_cards)

                def _show_card_at(_idx):
                    if _idx >= _total:
                        return
                    _c = _cards[_idx]
                    _real_choice = _c.get("on_choice") or (lambda v: None)
                    _real_dismiss = _c.get("on_dismiss") or (lambda: None)

                    def _wrapped_choice(v, _i=_idx):
                        _real_choice(v)
                        _show_card_at(_i + 1)

                    def _wrapped_dismiss(_i=_idx):
                        _real_dismiss()
                        _show_card_at(_i + 1)

                    with self._ui_scope():
                        self._show_user_choice_card(
                            question=_c.get("question", "请选择"),
                            choices=_c.get("choices", []),
                            allow_custom=_c.get("allow_custom", True),
                            on_choice=_wrapped_choice,
                            on_dismiss=_wrapped_dismiss,
                            progress=((_idx + 1, _total) if _total > 1 else None),
                        )

                _show_card_at(0)
                self.status_lbl.set_text("AWAITING_CHOICE")
                self.status_lbl.style('color:var(--nano-warn); font-size:var(--nano-fs-sm);')
                continue

            # ── 副作用确认弹窗 ──────────────────────────────────────
            # ⭐⭐ MCP 接入授权 —— 🔴 **这里刻意【不】查 `self._auto_on`。**
            #
            # 不是「MCP 特殊」或「频率低」那种例外理由（例外会被下一个人问
            # 「那为什么别的不例外」），而是**它不在 auto 管辖的维度上**：
            #     能力开关   答「这项能力开不开放」
            #     auto      答「开放了的，要不要逐个授权」
            #     这个弹窗   答「**这个第三方是什么东西**」   ← 第三个维度
            # 📌 **auto 从没承诺「不给你看东西」，它承诺的是「不用你逐个点同意」。**
            # 🔴 而且对 stdio，「连上」本身就是在本机执行第三方代码 ——
            #    这是执行第三方代码前的**最后一道**，auto 豁免它 = 那道就不存在了。
            if step.get("event") == "mcp_connect_confirm":
                with self._ui_scope():
                    self._show_mcp_connect_dialog(
                        info=step.get("info") or {},
                        purpose_line=step.get("purpose_line", ""),
                        what_it_does=step.get("what_it_does", ""),
                        on_confirm=step.get("on_confirm") or (lambda: None),
                        on_cancel=step.get("on_cancel") or (lambda: None),
                    )
                continue

            if step.get("event") == "execution_confirm":
                _skill_name  = step.get("skill_name", "")
                _side_effects = step.get("side_effects", [])
                _confirm_cb  = step.get("on_confirm")
                _cancel_cb   = step.get("on_cancel")
                # auto 模式（全局或本次临时）→ 自动通过，不弹窗（不偷焦点）
                if self._auto_on():
                    if _confirm_cb:
                        _confirm_cb()
                    continue

                # 修复：不移除 loading_container——确认对话框弹出后，
                # 用户点确认到最终回复之间还有"工具执行+总结"几秒钟，
                # 之前这段时间聊天区完全没有任何提示，像卡住了。
                # 保留这一行 spinner，只更新文案，final_result 时统一移除。
                try:
                    if self._current_loading_label:
                        self._current_loading_label.set_text('等待确认...')
                except Exception:
                    pass

                with self._ui_scope():
                    self._show_execution_confirm_dialog(
                        skill_name=_skill_name,
                        side_effects=_side_effects,
                        on_confirm=_confirm_cb or (lambda: None),
                        on_cancel=_cancel_cb or (lambda: None),
                        # 临时执行通道会带上那段代码；
                        # Skill 那条路没有这个键，取到空串 → 弹窗形状一个字不变。
                        preview_code=str(step.get("preview_code") or ""),
                    )
                self.status_lbl.set_text("AWAITING_CONFIRM")
                self.status_lbl.style('color:var(--nano-warn); font-size:var(--nano-fs-sm);')
                self.log_lbl.set_text(f"等待用户确认执行 {_skill_name}...")
                # 不 return:async for 自然挂起,等待 orchestrator 的 Event 触发后继续消费后续事件
                continue

            # ── OS 层操作授权弹窗──────────────────────────────────
            # 🔴 [2026-08-15] 确认弹窗的**非按钮结束路径**：用户改口说话 / 超时。
            #    见 `_dismiss_pending_confirms` 的那段说明。
            if step.get("event") == "confirm_dismiss":
                self._dismiss_pending_confirms(step.get("why", ""))
                continue

            if step.get("event") == "os_action_confirm":
                _risk = step.get("effective_risk", 2)
                if self._present_os_confirm(step):
                    continue
                risk_color = "var(--nano-danger)" if _risk >= 3 else "var(--nano-warn)"
                self.status_lbl.set_text("AWAITING_OS_CONFIRM")
                self.status_lbl.style(f'color:{risk_color}; font-size:var(--nano-fs-sm);')
                # ⚠️ 2026-08-20 抽 `_present_os_confirm()` 时，这里的 `_action`
                #    随那段一起被搬走了，而这一行还在读它 —— 常驻的 AST 作用域
                #    检查器当场抓到。
                #    📌 **一个查错工具的价值，在它抓到你自己刚写的错时才真正兑现**
                #       （这是它第四次兑现）。
                self.log_lbl.set_text(
                    f"等待用户授权 OS 操作：{step.get('action', '')}（risk={_risk}）...")
                # 不 return：挂起等用户点击，orchestrator 的 asyncio.Event 唤醒后继续
                continue
            
            if "progress" in step and self._current_loading_label:
                try:
                    self._current_loading_label.set_text(step["progress"])
                except Exception:
                    pass
                    
                        
            # ── 状态更新 ──────────────────────────────────────
            current_status = step.get("status", "RUNNING")
            self.status_lbl.set_text(current_status)

            # 可用性优先于活动状态。这两段【曾经】无条件把标签写成 HIT/IDLE，
            # 于是健康消费者刚画上的 FAULT 每一轮都被抹回 IDLE——实测复现过一次
            # "红点配 IDLE 文字"的割裂（圆点归健康消费者管、标签归这里管，两边打架）。
            # 现在活动态写入前先问一句健康状态：坏着就不许降级成 IDLE/HIT。
            if "rag_hit" in step and self.rag_lbl:
                if not self._card_is_faulted("rag"):
                    if step["rag_hit"]:
                        self._paint_card(getattr(self, "rag_dot", None), self.rag_lbl, "HIT")
                    else:
                        self._paint_card(getattr(self, "rag_dot", None), self.rag_lbl, "IDLE")

            if "full_file_hit" in step and self.full_file_lbl:
                if not self._card_is_faulted("full_file"):
                    if step["full_file_hit"]:
                        # 全文加载命中沿用原来的绿色，跟 RAG 的琥珀色区分开
                        self._paint_card(getattr(self, "full_file_dot", None),
                                         self.full_file_lbl, "FULL_HIT")
                    else:
                        self._paint_card(getattr(self, "full_file_dot", None),
                                         self.full_file_lbl, "IDLE")

            if current_status == "TOOL_EXECUTING":
                self.status_lbl.style('color:var(--nano-fg-soft); font-size:var(--nano-fs-sm);')
                active_sk = step.get("current_skill") or current_session_skill
                if active_sk in self.skill_ui_elements:
                    self.skill_ui_elements[active_sk]["status"].set_text("RUNNING")
                    self.skill_ui_elements[active_sk]["status"].style(
                        'font-size:var(--nano-fs-2xs); color:var(--nano-fg-soft); letter-spacing:0.04em; margin-right:4px;'
                    )
                    try:
                        self.skill_ui_elements[active_sk]["icon"].style(
                            'width:7px; height:7px; border-radius:50%; background:var(--nano-fg-soft); '
                            'flex-shrink:0; transition:background 0.3s; box-shadow:0 0 6px var(--nano-fg-soft)88;'
                        )
                    except Exception:
                        pass
            elif current_status in ["CORE_THINKING", "ROUTING..."]:
                self.status_lbl.style('color:var(--nano-warn); font-size:var(--nano-fs-sm);')
            elif current_status in ["ERROR", "CRITICAL_SYSTEM_HALT"]:
                self.status_lbl.style('color:var(--nano-danger); font-size:var(--nano-fs-sm);')
            else:
                self.status_lbl.style('color:var(--nano-ok); font-size:var(--nano-fs-sm);')

            # ── 任务列表事件 ──────────────────────────────────────────────
            if step.get("event") == "task_list_update":
                task_state = step.get("task_state", {})
                self._task_state = task_state
                with self._ui_scope():
                    self._plan_title_label.set_text(task_state.get("title", "任务"))
                    self._plan_steps_container.clear()
                    with self._plan_steps_container:
                        for s in task_state.get("steps", []):
                            self._render_plan_step(s)
                    self._nav_plan_row.set_visibility(True)
                    # 仅在计划面板尚未处于打开状态时才切换，避免重复调用
                    # create_task_list（覆盖列表）时 toggle 语义把抽屉收回去。
                    _plan_already_open = self.right_drawer.value and self.plan_panel.visible
                    if not _plan_already_open:
                        self._show_right_panel('plan')
                continue

            if step.get("event") == "task_step_update":
                step_id = step.get("step_id", "")
                status  = step.get("status", "done")
                note    = step.get("note", "")
                if self._task_state:
                    for s in self._task_state["steps"]:
                        if s["id"] == step_id:
                            s["status"] = status
                            s["note"] = note
                            break
                    with self._ui_scope():
                        self._plan_steps_container.clear()
                        with self._plan_steps_container:
                            for s in self._task_state["steps"]:
                                self._render_plan_step(s)
                    # 所有步骤都完成/失败时隐藏计划按钮
                    all_done = all(
                        s["status"] in ("done", "failed")
                        for s in self._task_state["steps"]
                    )
                    if all_done:
                        await asyncio.sleep(2)  # 让用户看见全部完成状态 2 秒
                        with self._ui_scope():
                            self._nav_plan_row.set_visibility(False)
                            if self.plan_panel.visible:
                                self.right_drawer.hide()
                            self.plan_panel.set_visibility(False)
                        self._task_state = None
                continue

            # ── widget 可视化输出：沙箱 iframe 内联渲染 ───────────────────────
            if step.get("event") == "visual_render":
                with self._ui_scope():
                    self._render_visual_artifact(
                        step.get("html", ""),
                        step.get("title", ""),
                        step.get("token", ""),
                    )
                # 渲染图后，模型还会继续输出文字说明——开新文字段，避免追加到旧段
                _rs["had_text_since_tool"] = True
                continue

            # ── 截图自查：把 Nano 截到的屏幕图推进聊天流 ──────────────────
            if step.get("event") == "screenshot_preview":
                _b64 = step.get("png_b64", "")
                if _b64:
                    # 🔴🔴 [2026-08-23] **这里原来是 `isinstance(_rs, dict)`。**
                    #
                    # 把 `_resp_state` 从裸 dict 换成 `ViewSession` 之后，
                    # 那个判断当场变成 False → `_tgt` 退回 `chat_container` →
                    # 截图被画进**整个聊天区**而不是那一轮的正文列里。
                    # ⇒ 用户看到的是：**图跑到气泡外面（位置错）+ 宽度按整个聊天区
                    #   算（巨大无比）** —— 两个症状同一个根因。
                    #
                    # 📌 **`isinstance(x, dict)` 是 drop-in 兼容层唯一挡不住的东西** ——
                    #    实现了 `get` / `__getitem__` / `__bool__`，但改不了它的类型。
                    #    ⚠️ 而它失败的方向是**静默降级**：没有异常、没有日志，
                    #       只是画到了别的地方。
                    # ⭐ 所以修法有两层：这里改判据（下面），
                    #    以及让 `ViewSession` **真的是** Mapping（见类定义），
                    #    否则下一个人写同样的代码还会中。
                    # 📌 判据本身也换了：问的不该是「它是不是 dict」，
                    #    而是「**它有没有我要的那个东西**」。
                    _tgt = (_rs or {}).get("loading_col") or self.chat_container
                    with self._ui_scope():
                        with _tgt:
                            # ⭐ 截图是**证据**不是内容：缩略图够用，要看清点开。
                            chat_image(f"data:image/png;base64,{_b64}",
                                       alt=step.get("purpose") or "Nano 看到的屏幕",
                                       thumb_h=120)
                    _rs["had_text_since_tool"] = True
                continue

            # ── 缩窗前的临时 auto 授权（缩窗即开始操作屏幕，先要授权）──────
            if step.get("event") == "mini_auth_request":
                _approve = step.get("on_approve")
                _reject  = step.get("on_reject")
                if self._global_auto:
                    # 全局 auto 已开 → 直接缩窗，不弹授权
                    await self._enter_mini()
                    if _approve:
                        _approve()
                else:
                    # 弹"同意/拒绝"——同意则开本次临时 auto + 缩窗
                    self._show_mini_auth_dialog(_approve, _reject)
                continue

            # ── 执行时自缩窗：Nano 调 set_window_mode 切 full（恢复）──────────
            if step.get("event") == "window_mode":
                if step.get("mode") == "mini":
                    await self._enter_mini()
                else:
                    await self._exit_mini()
                continue

            # ── 挂起/等待：把"刚调用 wait_for 工具"那个动态 pill 切成等待态 ───
            # 复用上个窗口做的动态工具 pill：执行时显示工具名，这里切成
            # "⏸ 等待中 · Ns"（计时器跳动 + 流光保活）；只有用户定时计划带
            # [立即执行][取消计划]，系统回看不露出操作按钮。
            # 等什么由 Nano 随后用自然语言说，不塞进 pill。
            if step.get("event") == "suspend_waiting":
                _waiting_intent = step.get("waiting_intent", "condition_recheck")
                # 本轮是挂起结束（不是真完成）→ 标记，turn 结束时不要恢复 mini，
                # 让 mini 跨挂起保留（任务还没做完，唤醒后继续在 mini 里做）。
                with self._ui_scope():
                    if _waiting_intent == "scheduled_timer":
                        self._turn_suspended = True
                        self._make_pill_waiting(
                            _rs,
                            suspension_id=step.get("suspension_id", ""),
                            reason=step.get("reason", "外部状态变化"),
                            wake_on=step.get("wake_on", []),
                            timer_at=step.get("timer_at"),
                            waiting_intent=_waiting_intent,
                            action_ref=_rs.get("action_refs", {}).get(step.get("action_id")),
                        )
                    else:
                        # The carrier is still alive: this response epoch's elapsed clock
                        # must continue through Nano's hand-back result.
                        _rs["waiting_for_carrier"] = True
                        self._register_hidden_waiting(
                            suspension_id=step.get("suspension_id", ""),
                            action_ref=_rs.get("action_refs", {}).get(step.get("action_id")),
                            waiting_intent=_waiting_intent,
                            bg_ref=step.get("bg_ref", ""),
                            # ⭐⭐⭐ [实测] **把「要收的是哪个 pill」在这里就定下来。**
                            #
                            # 🔴 不快照的话，等载体真完成时再去读
                            #    `_rs["tool_pill_lbl"]`，读到的**已经不是这一个了**：
                            #    回看轮会开新批次（`pill_settled` 被重置为 False、
                            #    `tool_pill_lbl` 换指向），于是收尾会收错一个 pill。
                            # 📌 **一个「稍后收尾」的动作，必须记住它要收的那个对象，
                            #    而不是到时候再去读「当前是哪个」** —— 因为在
                            #    「稍后」这段时间里，「当前」的含义会变。
                            pill=self._snapshot_tool_pill(_rs),
                        )
                _rs["had_text_since_tool"] = True  # 等待后模型的文字另起一段
                if _waiting_intent == "scheduled_timer":
                    self.status_lbl.set_text("SUSPENDED")
                    self.status_lbl.style('color:var(--nano-warn); font-size:var(--nano-fs-sm);')
                continue

            # ── 长任务被交还：把还在跑的那个载体交给后台生产端 ────────────────
            # 完成时 _run_bg_task → notify_background_done(ref) → 唤醒等它的 background 挂起，
            # Nano 带结果起新 turn 续做。复用整套背景唤醒桥（无需重新发明）。
            #
            # ⭐⭐⭐ **这里不认识任何一种载体。** 事件里那个 `task` 一律
            #    `await` 出**一个字符串**（给模型看的话），由各自的生产端包好。
            #
            # 🔴 上一版这里写的是 `_txt, _err, _na, _srv = await _t` ——
            #    **写死了 MCP 的四元组**。于是长命令和本地 Skill 接进同一条
            #    合同之后，它们完成的那一刻会被解成
            #    `后台执行失败：too many values to unpack (expected 4)`，
            #    而那正好落在最典型的场景上（`pip install` 跑完）。
            # ⚠️ 事件名也从 `mcp_background_request` 改成了 `long_task_handback`
            #    —— 三个载体共用它之后，旧名字会让人以为只有 MCP 走这里。
            #    📌 **一个「保留旧名字」的决定，必须同时检查那个事件的消费端
            #       还假设着什么。名字兼容 ≠ 形状兼容。**
            if step.get("event") == "long_task_handback":
                _bg_t = step.get("task")
                _bg_ref = step.get("bg_task_ref", "")
                _bg_disp = step.get("display", "后台任务")

                async def _handback_await(_t=_bg_t):
                    try:
                        return await _t
                    except asyncio.CancelledError:
                        # ⚠️ 必须原样抛上去：`_run_bg_task_inner` 靠它区分
                        #    「被用户终止」和「执行失败」，吞掉就变成假的失败。
                        raise
                    except Exception as _e:
                        return f"后台执行失败：{type(_e).__name__}: {_e}"

                # 系统交还只是让一个仍阻塞当前工作结果的载体继续跑；它不是 Nano
                # 主动委派出去的一件独立工作，不能因此出现在 `x running task(s)`。
                self._start_handed_back_carrier(
                    _bg_disp, _handback_await(), _bg_ref,
                    skill_name=step.get("skill_name", ""),
                    rt_task_id=step.get("rt_task_id") or None,
                    owns_record=bool(step.get("owns_record", True)))
                continue

            # ── 模型说「这个调用我不等了」→ 它现在是一件真的后台任务 ────
            #
            # ⭐⭐ **这是抽屉 Running 段的第二个生产者。**
            #    在此之前它只有一个（Subagent）—— 那正是 用户报的
            #    「后台任务只有Subagent能进去」：不是抽屉坏了，是**没有第二条路**。
            #
            # ⚠️ 与系统交还**刻意不同**（见 `_start_handed_back_carrier` 的
            #    docstring）：系统交还时当前工作**仍然依赖它的结果**，所以它
            #    不进 pill；而 `dont_wait` 之后 Nano 真的转去做别的了 ——
            #    📌 **`x running task(s)` 数的是「有东西在动、而你不必等它」**，
            #       两个条件缺一不可。系统交还满足前者不满足后者。
            if step.get("event") == "carrier_detached":
                self._promote_carrier_to_background(
                    step.get("bg_ref", ""), step.get("display", ""))
                continue

            # ── MCP 在场授权提示：某外部服务需登录（OAuth 延后授权）──────────
            # 模型已在回复里口头告知用户；这里再给一条引导到"设置→MCP 连接"登录的提示。
            if step.get("event") == "mcp_auth_required":
                _auth_srv = step.get("server", "")
                with self._ui_scope():
                    ui.notify(f'外部服务「{_auth_srv}」需要登录授权，可在设置（右上角三个点）的「MCP 连接」里登录',
                              type='warning', icon='login', timeout=6000)
                continue

            # ── 工具调用：按"有无文字间隔"分批次，每批次一个可展开 pill ──────
            # had_text_since_tool==True → 开新 pill 批次（思考→行动的节点）
            # had_text_since_tool==False → 同批次连续调用，更新计数即可
            if step.get("event") == "tool_start" and step.get("action_id"):
                _aid = step["action_id"]
                _adisplay = step.get("action_display", step.get("log", "执行中"))
                _rs["tool_count"] = _rs.get("tool_count", 0) + 1
                _start_new_batch = _rs.get("had_text_since_tool", True) or _rs.get("batch_tool_count", 0) == 0
                _rs["had_text_since_tool"] = False

                # 定型上一段流式文字：去掉闪烁光标 ▋。否则模型"先说一段话再调工具"时，
                # 那段文字的 markdown 会一直挂着光标（下面开新批次会把 content_md 指到新元素，
                # 旧元素的光标永远清不掉）——表现为光标卡在第二次工具调用前。
                try:
                    if _rs.get("content_md") is not None:
                        _rs["content_md"].set_content(_rs.get("current_text", "") or "")
                except Exception:
                    pass

                with self._ui_scope():
                    if _start_new_batch:
                        # 开新批次：创建新 pill + 明细列 + 后续文字段
                        _rs["batch_tool_count"] = 1
                        _rs["batch_fail_count"] = 0
                        _rs["pill_settled"] = False
                        try:
                            with _rs["loading_col"]:
                                with ui.row().classes('items-center gap-1.5 cursor-pointer mt-2 mb-0.5') as _pill_row:
                                    _pill_arrow = ui.label('▸').style('font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono);')
                                    _pill_dollar = ui.label('$').style('font-size:var(--nano-fs-base); color:var(--nano-dim); font-family:var(--nano-mono);')
                                    # 执行时顶行动态显示"当前命令名"(琥珀)，批次结束定型成 [✓] N tools
                                    # nano-tool-active：执行中扫光动效，定型时移除。
                                    _pill_lbl = ui.label(_adisplay).classes('nano-tool-active').style(
                                        'font-size:var(--nano-fs-base); color:var(--nano-amber); font-weight:500; font-family:var(--nano-mono);'
                                    )
                                    # 失败计数后缀（红色），全成功时留空
                                    _pill_fail_lbl = ui.label('').style(
                                        'font-size:var(--nano-fs-base); color:var(--nano-danger); font-weight:500; font-family:var(--nano-mono);'
                                    )
                                _details_col = ui.column().classes('w-full pl-3 gap-0.5 mb-0.5')
                                _details_col.set_visibility(False)
                                _new_md = nano_md(
                                    style='color:var(--nano-fg); min-height:0.5em; margin-top:2px;')
                            _rs["tool_pill_lbl"] = _pill_lbl
                            _rs["tool_pill_dollar"] = _pill_dollar   # 定型时藏掉 $
                            _rs["batch_start_time"] = time.time()    # 收尾算用时
                            _rs["tool_pill_fail_lbl"] = _pill_fail_lbl
                            _rs["tool_pill_arrow"] = _pill_arrow
                            _rs["tool_pill_row"] = _pill_row  # 等待态要往这行追加计时器/按钮
                            _rs["tool_details_col"] = _details_col
                            _rs["content_md"] = _new_md
                            _rs["current_text"] = ""

                            def _toggle_tools(_pill_row=_pill_row, _details_col=_details_col,
                                              _pill_arrow=_pill_arrow):
                                _expanded = [False]
                                def _do():
                                    _expanded[0] = not _expanded[0]
                                    with self._ui_scope():
                                        _details_col.set_visibility(_expanded[0])
                                        _rot = "rotate(90deg)" if _expanded[0] else "rotate(0deg)"
                                        _pill_arrow.style(
                                            f'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono);'
                                            f' transform:{_rot}; transition:transform 0.18s;'
                                        )
                                return _do
                            _pill_row.on('click', _toggle_tools())
                        except Exception:
                            pass
                    else:
                        # 同批次追加：计数+1，pill 顶行切到"当前工具名"（动态），
                        # 批次结束才定型成 "Used N tools"。
                        _rs["batch_tool_count"] = _rs.get("batch_tool_count", 1) + 1
                        try:
                            _rs["tool_pill_lbl"].set_text(_adisplay)
                            _rs["tool_pill_lbl"].classes('nano-tool-active')
                        except Exception:
                            pass

                    # 在当前批次明细列追加此工具行（spinner → done）
                    try:
                        with _rs["tool_details_col"]:
                            # ⭐ 与重放侧同一形状：一行 + 它自己的可展开详情。
                            _row_col = ui.column().classes('w-full gap-0')
                            with _row_col:
                                with ui.row().classes('items-center gap-1.5').style('padding:1px 0;') as _trow:
                                    ui.label('$').style('font-size:var(--nano-fs-sm); color:var(--nano-faint); font-family:var(--nano-mono);')
                                    ui.label(_adisplay).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); font-family:var(--nano-mono);')
                                    _d_spin = ui.spinner(size='xs').style('color:var(--nano-dim);')
                                    _d_done = ui.label('').style('font-size:var(--nano-fs-sm); color:var(--nano-ok); font-weight:500; font-family:var(--nano-mono);')
                        _rs["action_refs"][_aid] = {
                            "spin": _d_spin, "done": _d_done, "start_time": time.time(),
                            "row_col": _row_col, "row": _trow,
                            "tool_use_id": step.get("tool_use_id", "") or "",
                            "tool_name": step.get("current_skill",
                                                  step.get("tool_name", "")) or "",
                        }
                    except Exception:
                        pass

            if step.get("event") == "tool_end" and step.get("action_id"):
                if step.get("ok") is False:
                    _rs["batch_fail_count"] = _rs.get("batch_fail_count", 0) + 1
                _ref = _rs["action_refs"].get(step["action_id"])
                if _ref:
                    with self._ui_scope():
                        try:
                            _ref["spin"].set_visibility(False)
                            # 失败画叉，成功画钩
                            if step.get("ok") is False:
                                _ref["done"].set_text("✕")
                                _ref["done"].style('font-size:var(--nano-fs-sm); color:var(--nano-danger); font-weight:500; font-family:var(--nano-mono);')
                            else:
                                _ref["done"].set_text("✓")
                        except Exception:
                            pass
                        # ⭐⭐ 结果出来了才挂详情 —— 挂在 tool_start 的话，
                        #    用户点开时账本里还没有那条结果，只能看到参数。
                        # ⚠️ **挂的只是入口，内容仍然是点开时才去账本取**（懒加载）：
                        #    📌 这一轮工具刚跑完，落盘可能比事件晚一点点，
                        #       而"点开"这个动作天然给了它时间。
                        try:
                            if _ref.get("row_col") is not None and not _ref.get("u8_done"):
                                _ref["u8_done"] = True
                                # ⚠️ 账本索引作废：这一轮刚写进去的它还不知道。
                                self._u8_ledger_index = None
                                self._attach_tool_detail(
                                    _ref.get("row"), _ref["row_col"],
                                    _ref.get("tool_use_id", "") or step.get("tool_use_id", "") or "",
                                    _ref.get("tool_name", "") or step.get("current_skill", "") or "")
                        except Exception as _u8e:
                            logger.debug(f"[U8] live 挂详情失败: {_u8e}")

            if "log" in step:
                # log 只写入监控面板，不再更新聊天区（路由/内部步骤信息不在聊天里显示）
                self.log_lbl.set_text(step["log"])
            if step.get("current_skill"):
                current_session_skill = step["current_skill"]
                self._koala_current_skill = step["current_skill"]
            if "model" in step:
                # ⚠️ **收口守卫**：`UNKNOWN` / 空 一律忽略，保持上一个已知值。
                #    带 `model` 的 yield 点有几十处，源头修一处不等于修完 ——
                #    📌 **防御要放在收口处，不是每个发出点。**
                # 📌 而"保持上一个已知值"比"显示 UNKNOWN"诚实：当前模型确实是它，
                #    只是这一瞬间还没有人回报而已。
                _mname = (step.get("model") or "").strip()
                if _mname and _mname.upper() != "UNKNOWN":
                    self.model_lbl.set_text(_mname.upper())
                # 后端崩溃标志（供考拉头像反应）。互联网检索卡片已独立，不在这里写。
                # ⚠️ 用 `_mname` 而不是 `step["model"]` —— 上面刚归一过一次，
                #    两处各读一次原始值就会在"空/None"上分叉。
                self._net_crashed = ("TOTAL_CRASH" in _mname.upper()
                                     or step.get("skills_active") is False)

            # ── 错误 ──────────────────────────────────────────
            if step.get("event") == "sys_error" or current_status == "CRITICAL_SYSTEM_HALT":
                # 停止计时器
                _rs["running"] = False
                try:
                    _timer_task.cancel()
                except Exception:
                    pass
                # navigate_pipeline 既会从普通UI事件触发，也会从 _safe_execute_
                # pipeline 这个后台 asyncio task 里触发——后台task没有天然的
                # client slot上下文，这整段只要操作UI元素就必须统一包在
                # ui_scope里，不能裸调用（今晚实测复现过"slot stack is empty"，
                # 根因就是类似的裸调用）。
                with self._ui_scope():
                    # 整轮原子性：清除已经流出的内容，整体替换为错误显示
                    try:
                        self.chat_container.remove(loading_container)
                    except Exception:
                        pass
                    _err_text = step.get("content", "未知错误")
                    with self.chat_container:
                        render_sys_error_card(_err_text)
                    # ⭐⭐⭐ [2026-08-22] **把它写进账本。**
                    #
                    # 🔴 问题：这张卡**只画不存**。用户的 API 欠费出了一张 402 卡片，
                    #    重启之后它**消失了** —— 「你好」下面空空如也，
                    #    而当时真的发生过一件事。
                    # 📌 **对话记录在说谎**，而这违反早先的设计那条唯一的持久化原则：
                    #    **UI 必须是真实的反馈。**
                    #
                    # ⚠️ 一度提议过折叠它 / 加「当时」的修饰，理由是「它是当时的
                    #    系统状态，重放会让人以为还成立」。用户否掉了，而且是对的：
                    #    📌 **一条在对话流里的记录不对「现在」做任何断言 ——
                    #       它的位置已经把时间说清楚了。**
                    #       会过期的是**活的状态指示器**（pill / 抽屉），
                    #       不是历史里的一条记录。
                    #    → 所以**原样重画**，不折叠、不加修饰。
                    #
                    # ⚠️ 走 `add_ui_only_record` 而不是 `add_message`：
                    #    那一轮**根本没到达模型**（请求本身失败了）。
                    #    📌 一条从没到达模型的消息，不该在它的历史里
                    #       显示成它说过的话 —— 否则重启后模型会看见自己
                    #       "说"了一串 402 报错，然后为此道歉。
                    # ⚠️ 落盘失败不许影响这张卡已经画出来的事实（展示层优先）。
                    try:
                        self.agent.memory.add_ui_only_record(_err_text, "sys_error")
                    except Exception as _e_se:
                        logger.warning(f"[L14] System Error 落盘失败（卡片已画）: {_e_se}")
                    self.scroll_area.scroll_to(percent=1.0, duration=0.2)
                    self.status_lbl.set_text("ERROR")
                    self.status_lbl.style('color:var(--nano-danger); font-size:var(--nano-fs-sm);')
                    self.log_lbl.set_text(step.get("log", "调用链路故障"))
                    ui.notify(step.get("log", "内核异常"), type='negative')
                self._koala_current_skill = None
                return

            # ── ⭐⭐⭐ [无缝对话] 本轮被用户插话中断 → 擦掉这一段，重画 ────────
            if step.get("event") == "turn_interrupted":
                # 把这件事说得最准：
                #   「语义上更像是『**我听到了新的消息，那我撤了重新说**』的感觉」
                # ⭐ 所以擦掉**不是妥协，它才是忠实的模拟** —— 一个人说话说到一半
                #    被你补一句，他不会先把原话讲完再回应，**会停下、重说**。
                #    📌 (b) 才是「就像真的两个人对话一样」，「留着接在后面」反而是
                #       机器人的做法（而且在第二条撤回第一条时自相矛盾）。
                # 📌 同时与 那条同源：**UI 必须是权威状态的忠实投影** ——
                #    一个已经被推翻的答案，已经不属于权威回答了。
                _is_stop = bool(step.get("stopped"))
                logger.info(
                    (f"[Stop] 本轮被用户终止（{step.get('where')}）→ **保留已说出的部分**，"
                     f"在中断点补一行系统注记")
                    if _is_stop else
                    (f"[Seam] 本轮被用户插话中断（{step.get('where')}）"
                     f"→ 擦掉这一段，等队列里那条带着两条消息重新决策"))
                try:
                    _timer_task.cancel()
                except Exception:
                    pass
                with self._ui_scope():
                    try:
                        # ⚠️⚠️ **擦掉只属于「插话」那一支**（2026-08-09 实测 UI 反馈后分开）。
                        #
                        # 原来这两行在分叉**之前**，于是终止也会把已经说出来的字擦掉 ——
                        # 而那违反 用户给终止定的原则：
                        #   「根据 UI 要显示真实情况的原则，**nano 说到哪里，就被打断到哪里**」
                        #
                        # ⭐ 两支的语义本来就相反，所以擦不擦也相反：
                        #   · 插话 = 「我听到了新消息，那我撤了重新说」→ **有下一段**，
                        #     留着旧的会在第二条推翻第一条时自相矛盾 → **擦**
                        #   · 终止 = 「到此为止」→ **没有下一段**，
                        #     擦掉等于假装它什么都没说过 → **留**
                        # 📌 **「撤回」和「停下」对已经说出的话要求相反** ——
                        #    前者要它消失（它已经不是权威回答了），
                        #    后者要它留着（它就是真实发生过的那部分）。
                        # ⭐ 与同源：**两个机制的出口方向相反，就不能共用一条路径。**
                        #    这已经是同一条判据在这个分叉上的第三次应用
                        #    （收尾元信息行 / 清不清队列 / 擦不擦文字）。
                        if not _is_stop:
                            # 擦掉这一段已经吐出来的文字（可能一个字都没有 —— 那更好）
                            _rs["content_md"].set_content("")
                            _rs["current_text"] = ""
                        # ⭐ 收掉工具 pill 批次是**两支都要做**的：那批工具确实结束了，
                        #    留一个还在转的 pill 无论哪一支都是假的。
                        self._settle_tool_pill(_rs)
                    except Exception:
                        pass
                # 下面九行保留的是早期“同一气泡重画”方案的迁移历史；将它退役。
                # ⚠️⚠️ **修掉一个「擦掉重画」会引入的静默数据丢失**：
                #    `_bg_tasks` 里快照了「最新那条回复」的 UI 引用
                #    （`inner_col` / `status_lbl`），后台任务完成时要往那里追加结果。
                #    这一段被擦掉/重画之后，那些引用可能指向**已被删除**的元素 →
                #    后台任务跑完，结果**追加到不存在的地方，静默丢失**。
                #    ⭐ 与 那个 bug 完全同形（持有已被移除的 DOM 引用）。
                #    📌 **任何「以后要往这里写」的 UI 引用，都必须能承受「这里被重画」。**
                #    修法：把它们改指到**当前**这一段的元素 ——
                #    它们本来就是「最新那条回复」的快照，语义上就该跟着最新走。
                # 旧的“把所有后台任务 DOM 引用改指到当前回复”已经退役：有等待的
                # 异步结果走 future wake，无等待的走独立 ChatEmitter，不再绑定“上一条”。
                # ⭐⭐⭐ **两种原因的出口方向相反，必须分开。**
                if step.get("stopped"):
                    # 终止：**没有下一段**。所以这里要**收尾**（写统计、停转圈），
                    # 而且要让用户看到「它真的停了」。
                    # ⚠️ 与插话相反：插话时不收尾（队列里那条马上接上来）。
                    # 📌 出口方向相反的两件事，不能共用一条收尾路径。
                    logger.info("[Stop] 已在动作边界停下 —— 收尾这一段，不接着跑")
                    with self._ui_scope():
                        try:
                            # ⭐⭐⭐ [2026-08-09 实测 UI 反馈] **在中断点补一行系统注记。**
                            #
                            # 实测：功能没问题，但**中断之后只剩一个空的 `nano ❯` 头
                            # 挂在那里**（thinking 立刻被打断时尤其明显 —— 一个字都没有）。
                            # ⭐ 用户的判断（采纳了，所以没去掉那个头）：
                            #   「**这个头本身也代表信息**，去掉会导致用户两条消息贴在一起」
                            # 📌 **一个空容器不是「没有信息」，它是「一条没说完的信息」** ——
                            #    该补的是「为什么没说完」，不是把容器删掉。
                            #
                            # ⚠️⚠️ **固定文案豁免**：这行字**在气泡内**，所以豁免第 5 条
                            #    （不在气泡里的）**不适用**，走的是另外两条：
                            #    · 第 3 条**系统级通知** —— 它是系统在陈述**系统自己的动作**，
                            #      不是 Nano 在说话（灰色 + 等宽就是这个区分的视觉标记）
                            #    · 第 1/2 条 —— **被停下的正是那个模型本身**。要它自然语言
                            #      说这句话，就得在用户刚要求「停」的那一刻再发一次请求，
                            #      既贵又荒谬。
                            #    📌 **让一个刚被停下的模型解释自己被停下，是自相矛盾的要求。**
                            #
                            # ⚠️ **措辞刻意只说这四个字，不多说一句。**
                            #    想过写「后续未执行」，但那**可能为假** ——
                            #    终止不杀后台任务/Subagent（已明确要求过），
                            #    写终止事实那段也明说「已经起来的后台工作仍在跑」。
                            #    📌 **一行系统注记多说一句，就多一个可能为假的断言。**
                            # ⚠️ 也刻意**不用红色/不带「失败」字样** ——
                            #    早先已定：**用户主动停掉不是失败**。
                            # ⚠️⚠️ **先处理「那个元素空着也占一行」** —— 否则 thinking
                            #    立刻被打断那一格（用户截图里的第一个红框，一个字都没有）
                            #    会变成：`nano ❯` 一行、空 markdown 占一行、注记在第三行。
                            # ⭐ 这与 之后那个「`nano ❯` 和文字对不齐」是**同一个 bug**：
                            #    `content_md` 的 style 带 `min-height:1em`，**空着也占一行**。
                            #    📌 **擦掉内容 ≠ 移除元素**（原判据），
                            #       而这里连擦都没擦 —— 它天生就是空的。
                            # ⭐ 判据照抄那处：**看那个元素现在有没有内容**，
                            #    不追踪「它是从哪条路来的」。
                            # ⚠️ 用 `min-height:0` 而不是 `display:none` —— 元素要留着可用，
                            #    万一之后有人往它写内容，有内容自然就有高度。
                            _note_leads = not (_rs.get("current_text") or "").strip()
                            if _note_leads:
                                try:
                                    _rs["content_md"].style('min-height:0; margin:0;')
                                except Exception:
                                    pass
                            # ⚠️ 必须显式 `with loading_col`：`ui.label` 落在**当前 slot**，
                            #    而这里的当前 slot 不是那一段的内容列
                            #    → 不指定容器它会画到别处去（或压根看不见）。
                            with _rs["loading_col"]:
                                _stop_note = ui.label('⏹ 已中断')
                                # ⚠️⚠️ **对齐靠的是「行盒高度一致」，不是靠调 margin。**
                                #    `nano ❯` 那个 label 是 `line-height:1.75rem`（28px），
                                #    外层 row 是 `items-start` —— 所以内容列的**第一个**
                                #    子元素必须也有 28px 的行盒才会齐。
                                #    正文 `_c_md` 带 `leading-7`（= 1.75rem）**正好是它**，
                                #    这就是平时文字为什么齐。
                                # 🔴 第一版给这行写的是 `font-size:var(--nano-fs-base)` + 默认行高
                                #    + `margin-top:2px` → 行盒只有 ~16px、还多 2px 外边距，
                                #    于是它比 `nano ❯` 低一点（用户截图里那个小 bug）。
                                # 📌 **和一行文字对齐，要复制那行文字的行盒，
                                #    而不是去猜一个偏移量** —— 猜出来的偏移在字号、
                                #    缩放、字体回退变化时会再次错开。
                                # ⭐ 只有它**打头**时才需要这个行盒；跟在文字后面时
                                #    它就是普通的下一行，跟 `nano ❯` 对齐无关。
                                _stop_note.style(
                                    'font-size:var(--nano-fs-base); color:var(--nano-dim); '
                                    'font-family:var(--nano-mono); letter-spacing:0.02em; '
                                    'opacity:0.85; '
                                    + ('line-height:1.75rem; margin-top:0;'
                                       if _note_leads else 'margin-top:2px;'))
                        except Exception:
                            pass
                        try:
                            _tok = usage_tracker.turn_tokens_fmt()
                            _el = int(time.time() - _rs["start_time"])
                            if _rs.get("spin_lbl"):
                                _rs["spin_lbl"].set_visibility(False)
                            if _rs.get("svg_el"):
                                _rs["svg_el"].style('width:16px; height:16px; '
                                                    'flex-shrink:0; display:inline-block;')
                            # ⚠️ 这行字**不在 Nano 气泡里**（是元信息行/系统状态）→
                            #    命中固定文案豁免第 3、5 条，允许写死。
                            _rs["status_lbl"].set_text(f"已终止 · {_el}s · {_tok} tok")
                            _rs["status_lbl"].style(
                                'font-size:var(--nano-fs-base); color:var(--nano-danger); font-style:normal; '
                                'letter-spacing:0.01em; font-family:var(--nano-mono);')
                        except Exception:
                            pass
                    # ⭐⭐ **终止这个事实必须传给模型** —— 早先已定，
                    #    而那条来自实测 Claude Code 时看到的**反面教材**：
                    #    被用户手动终止时，模型**什么都没收到**，任务就是不存在了。
                    #    📌 状态变化必须让需要知道的人知道 —— 这里是模型。
                    #    ⚠️ 走 memory 而不是动态段：它是**已经发生的事实**，
                    #       该留在对话历史里，不是每轮重复注入的提示。
                    try:
                        self.agent.memory.add_system_note(
                            "assistant",
                            "[System record: the user pressed Stop, so I stopped at "
                            "the next safe boundary. Nothing further was executed. "
                            "Any background work I had already started is still "
                            "running unless I cancel it.")
                    except Exception as _e_sr:
                        logger.warning(f"[Stop] 写终止事实失败: {_e_sr}")
                    # ⚠️ 终止**不清队列** —— 用户排着的消息不该被这次终止吃掉。
                    #    📌 「停止当前这一轮」和「丢掉我说过的话」是两件事。
                    self._turn_interrupted = True
                    self._refresh_send_btn()
                    return

                # ⚠️ **插话：不收尾元信息行** —— 这一段被撤回了，但整个回应期还没结束，
                #    队列里那条马上会接上来，token 统计要等**整段**完了才写。
                self._turn_interrupted = True
                return

            # ── 最终结果 ───────────────────────────────────────
            if step.get("event") == "final_result":
                # A hand-back result ends Nano's current wording, not the live carrier.
                # Keep this response epoch's elapsed clock alive until the carrier returns.
                _waiting_for_carrier = bool(_rs.get("waiting_for_carrier"))
                if not _waiting_for_carrier:
                    _rs["running"] = False
                    try:
                        _timer_task.cancel()
                    except Exception:
                        pass
                # 用完整 content 定型（覆盖流式累积文字，或首次填入）
                _content = step.get("content", "")
                _elapsed_done = int(time.time() - _rs["start_time"])
                rag_suffix = " · 📚" if step.get("rag_hit") else ""
                # 兜底定型：若没有答案文字流（直接出 final_result）的路径
                self._settle_tool_pill(_rs)
                with self._ui_scope():
                    try:
                        _rs["content_md"].set_content(_content)
                        # 结构化数据表格追加在内容末尾
                        with _rs["loading_col"]:
                            self._render_tool_data_tables(step.get("tool_data"))
                    except Exception:
                        pass
                    try:
                        # ⭐⭐⭐ [无缝对话] **收尾的条件不是「这一轮结束」，
                        #    而是「这一轮结束 且 队列里没有排着的了」。**
                        #
                        # 队列里还有 → 这一段只是回应期的**中途**：
                        # 不藏转圈、不显 ✦、不写 token —— 因为 已明确
                        # 「token 计数器不能出现两个，也得出现在末尾」，
                        # 那意味着它统计的是**整段回应期**。
                        # 📌 一个「结束时才做的事」，在「一段可以包含多轮」之后，
                        #    判据必须从「这一轮完了吗」换成「整段完了吗」。
                        _seam_more = (bool(getattr(self, "_rt_inbox_parked", None)) or
                                      _waiting_for_carrier)
                        if _seam_more:
                            logger.info("[Seam] 这一段答完了但队列里还有 → "
                                        "保持转圈，不写 token（等整段结束再定型）")
                        else:
                            # 本段用量：orchestrator 在本轮开头 begin_turn() 打点，这里取差值
                            # （fresh+output，非全会话累加）。避免 app 端 tok_base 的时序竞争。
                            _tok_str = usage_tracker.turn_tokens_fmt()
                            # 定型：藏 braille 转圈、显光芒头像 ✦、状态变 "8.2s · 2.2K tok"
                            if _rs.get("spin_lbl"):
                                _rs["spin_lbl"].set_visibility(False)
                            if _rs.get("svg_el"):
                                _rs["svg_el"].style('width:16px; height:16px; flex-shrink:0; display:inline-block;')
                            # ⚠️ 按「设置 → 通用 → Token 计数器」的档位拼：
                            #    这行**每条消息下面都有**，是用户看得最多的数字。
                            #    📌 一个数字出现的频率，跟它的大小一样影响观感。
                            _rs["status_lbl"].set_text(
                                f"{_elapsed_done}s{self._turn_tok_suffix(_tok_str)}{rag_suffix}")
                            _rs["status_lbl"].style(
                                'font-size:var(--nano-fs-base); color:var(--nano-dim); font-style:normal; '
                                'letter-spacing:0.01em; font-family:var(--nano-mono);'
                            )
                    except Exception as _settle_err:
                        # 🔴 **原来这里是裸 `except: pass`** —— 而 2026-08-15
                        #    报的「工具轮次下面没有 token 计数器」正是被它吞掉的：
                        #    这条路径一旦抛异常，屏幕上表现为**什么都没有**，
                        #    而那看起来只是「这里本来就没这一行」。
                        # 📌 **一个静默的 except，会把「坏了」伪装成「设计如此」** ——
                        #    而后者没有人会去查（同 `_ledger_tool_record` 那次）。
                        logger.warning(f"[Settle] 状态行没写成: {_settle_err!r}")
                        pass

                active_sk = step.get("current_skill") or current_session_skill
                if active_sk in self.skill_ui_elements:
                    # 回看那一轮的 final_result 只意味着 Nano 这次看完了，
                    # 不是原来的 Skill 已经结束。真正的完成由 carrier 的 finally
                    # 收口；在那之前该状态必须保持 RUNNING。
                    self._set_skill_ui_status(
                        active_sk,
                        "RUNNING" if self._is_handed_back_skill_running(active_sk) else "OK")

                self.scroll_area.scroll_to(percent=1.0, duration=0.2)
                self.status_lbl.set_text("SYS_IDLE")
                self.status_lbl.style('color:var(--nano-ok); font-size:var(--nano-fs-sm);')
                self.log_lbl.set_text(step.get("log", "处理完毕。"))
                self._koala_current_skill = None
                self._update_cost_warning()
                # 🔴 这里原本直接 set_text(今日总量)，**绕过了「Token 计数器」那条设置** ——
                #    于是选了「不显示」也只在重启后有效，一发消息就被这三行改回去
                #。
                #    📌 同一块 UI 的两个写入口，只要有一个不认规则，那条规则就等于不存在。
                self._refresh_token_card()
                self._refresh_context_card()
                # ⚠️ **不在这里同步"被移出的那几段"** —— 衰减发生在本轮 `final_result`
                #    之后（见 `self.agent._on_decay_applied` 的接线）。
                # 本轮 write_user_note 写入的笔记：ReAct 内不发终端事件（会截断 tool_results），
                # 改在这里统一弹"Nano 似乎记住了什么"气泡 + 加进右侧记忆抽屉的待确认卡片。
                try:
                    _notes = list(getattr(self.agent, '_notes_written_this_turn', []) or [])
                    if _notes:
                        from datetime import datetime as _dt2
                        with self._ui_scope():
                            ui.notify("Nano 似乎记住了什么 🔖", type='info', timeout=4000)
                            for _n in _notes:
                                _nid = _n.get("note_id"); _disp = _n.get("display_text", "")
                                if _nid is not None and _disp:
                                    self._pending_notes.insert(0, {
                                        "note_id": _nid, "display_text": _disp,
                                        "ts": _dt2.now().strftime("%Y-%m-%d %H:%M:%S")})
                            self._render_pending_cards()
                            self._update_memory_badge()
                        self.agent._notes_written_this_turn = []
                except Exception:
                    pass

                # Episodic: 写本轮会话摘要到跨会话记忆
                try:
                    _eq = getattr(self, '_current_query', '')
                    self.agent.record_session_end(_eq, _content)
                except Exception:
                    pass
                return

            # ── user_note 待确认事件 ──────────────────────────────────────
            if step.get("event") == "user_note_pending":
                note_id = step.get("note_id")
                display_text = step.get("display_text", "")
                # 停止计时器，用统一容器显示内容
                _rs["running"] = False
                try:
                    _timer_task.cancel()
                except Exception:
                    pass
                _elapsed_done = int(time.time() - _rs["start_time"])
                with self._ui_scope():
                    try:
                        _rs["content_md"].set_content(step.get("content", ""))
                        if _rs.get("spin_lbl"):
                            _rs["spin_lbl"].set_visibility(False)
                        if _rs.get("svg_el"):
                            _rs["svg_el"].style('width:16px; height:16px; flex-shrink:0; display:inline-block;')
                        _rs["status_lbl"].set_text(f"{_elapsed_done}s")
                        _rs["status_lbl"].style('font-size:var(--nano-fs-base); color:var(--nano-dim); font-style:normal; font-family:var(--nano-mono);')
                    except Exception:
                        pass
                # toast + 卡片 + 红点——必须在 _ui_scope 内操作
                with self._ui_scope():
                    ui.notify("Nano 似乎记住了什么 🔖", type='info', timeout=4000)
                    if note_id is not None and display_text:
                        from datetime import datetime as _dt
                        new_note = {"note_id": note_id, "display_text": display_text,
                                    "ts": _dt.now().strftime("%Y-%m-%d %H:%M:%S")}
                        self._pending_notes.insert(0, new_note)
                        self._render_pending_cards()
                        self._update_memory_badge()
                self.scroll_area.scroll_to(percent=1.0, duration=0.2)
                self.status_lbl.set_text("SYS_IDLE")
                self.status_lbl.style('color:var(--nano-ok); font-size:var(--nano-fs-sm);')
                self.log_lbl.set_text(step.get("log", "记忆已写入。"))
                self._koala_current_skill = None
                return

            await asyncio.sleep(0.01)

    # ── Safe wrapper ──────────────────────────────────────────────────────

    async def _safe_execute_pipeline(self, query, loading_container, image_bytes: bytes | None = None, image_mime: str = "image/jpeg", temp_file_hint: str | None = None, thought_blocks_container=None):
        # 用户插话会创建 successor 并覆盖全局 `self._resp_state`。旧 pipeline
        # 随后若异常，异常兜底仍只能收它自己拥有的 predecessor，不能误伤新回应。
        _owned_rs = None
        async with self.pipeline_lock:
            _owned_rs = self._resp_state
            self._speaker.set_responding(True)
            self._intel_engine.set_responding(True)
            self._activity.on_nano_event("user_message")
            self._turn_suspended = False   # 本轮是否以挂起方式结束（挂起则不恢复 mini）
            try:
                await self.navigate_pipeline(query, loading_container, image_bytes, image_mime, temp_file_hint, thought_blocks_container)
                self._activity.on_nano_event("nano_responded")
            except Exception as e:
                logger.error(f"UI 未捕获异常: {e}")
                # 停止计时器
                try:
                    _rs = _owned_rs or {}
                    _rs["running"] = False
                except Exception:
                    pass
                # 这是后台 asyncio task（_safe_execute_pipeline 由 create_task 启动），
                # 操作UI元素必须显式进入这个连接的 client slot，否则 NiceGUI 找不到
                # slot 上下文，连"显示错误提示"这个兜底动作本身都会静默失败
                with self._ui_scope():
                    try:
                        self.chat_container.remove(loading_container)
                    except Exception:
                        pass
                    self.status_lbl.set_text("ERROR")
                    self.status_lbl.style('color:var(--nano-danger); font-size:var(--nano-fs-sm);')
                    self.log_lbl.set_text(f"核心故障: {str(e)[:80]}")
            finally:
                self._speaker.set_responding(False)
                self._intel_engine.set_responding(False)
                # ⛔ [2026-08-23 已定：整段删除] 这里原来在 **turn 结束时无条件
                #    把窗口恢复成 full**，理由写的是「避免 Nano 没机会调 full 时卡在 mini」。
                #
                # 🔴 那是**用系统兜底去代替提示词**，而它的代价比它防的问题大：
                #    · **GUI 任务天生跨多轮**（缩小 → 看 → 点 → 再看），
                #      每轮结束弹回 full、下一轮又缩回去 —— 用户看到的是窗口来回跳，
                #      而且中间那次截图正好是**全屏挡着**的。
                #    · 它还**夺走了模型的控制权**：`set_window_mode` 的描述里已经
                #      写清了什么时候该恢复，而这条兜底让那句话变成一句空话。
                # 📌 **一个「怕模型忘了」的系统兜底，如果会在模型【没忘】的时候
                #    也生效，那它就不是兜底，是覆盖。**
                # ⭐ 用户定的：**这里不需要任何系统强制机制** ——
                #    该恢复的时候由 `set_window_mode` 的措辞提醒模型自己恢复。
                # ⭐ 这一轮对应的那条 inbox 记录收尾
                _rt_inbox_consume(getattr(self, "_rt_inbox_running_id", None))
                self._rt_inbox_running_id = None

        # ⭐⭐ **必须在 `async with` 之外** —— 锁已经释放了。
        #    写在 finally 里的话，被排空起的那一轮会立刻撞上还没释放的锁，
        #    于是它又被判成「忙」→ 又进队列 → 永远没人处理。
        #    📌 **一个「等锁释放后再做」的动作，不能写在还持有锁的作用域里。**
        #       （形状与早先那个「在 finally 里 release 却又在 finally 里 acquire」同族。）
        try:
            await self._drain_inbox()
        except Exception as e:
            logger.error(f"[Inbox] 排空队列失败: {e}")

    # ── 队列排空 ───────────────────────────────────────────────

    def _rt_inbox_mark_queued(self, loading_container) -> None:
        """把那条消息的 loading 区改成「排队中」。

        ⚠️ 这段文案**不出现在 Nano 的气泡里**，是系统状态提示 ——
           命中固定文案豁免的第 3 条（系统级通知）和第 5 条（不在气泡里）。
        """
        try:
            n = _rt_inbox_pending()
            with self._ui_scope():
                loading_container.clear()
                with loading_container:
                    ui.label(
                        f'排队中 · Nano 正在处理上一条'
                        + (f'（前面还有 {n - 1} 条）' if n > 1 else '')
                    ).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
        except Exception as e:
            logger.debug(f"[Inbox] 标记排队中失败（不影响入队）: {e}")

    async def _drain_inbox(self) -> None:
        """当前轮结束 → 把排队的下一条接上。

        ⭐⭐ **一次只处理一条，然后靠新那一轮的收尾再次调用本函数。**
        ⚠️ 不写 `while` 循环，两个理由：
           ① 新那一轮是 `create_task` 起的，本函数**不等它结束**——
              循环会在它还没跑完时就去取下一条，两轮并跑。
           ② level-triggered 天然更稳：每轮结束都重新看一次「还有没有」，
              中途新来的消息、失败退回的消息都会被自然带上。
              📌 **与 Reconciler 同一条：让每次检查都问「现在的真实状态是什么」，
                 而不是维护一个「还剩几条」的计数。**
        """
        if self.pipeline_lock.locked():
            return                      # 有别的轮在跑，等它结束时会再来
        parked = getattr(self, "_rt_inbox_parked", None)
        if not parked:
            # ⭐⭐⭐ [2026-08-22] **前台空了 → 👁 视线转向后台。**
            #
            # 走到这里的含义很精确：锁没人持有、队列里也没有排队的 ——
            # **手上确实没活了**。这正是建模原图里那条支线的触发点：
            #   「next_step 做完／前台空了／用户否决了前台那件事
            #     → 👁 视线转向后台『那件事好了没』
            #     → 没好 → set_next_checkin」
            #
            # 🔴 此前代码把「转入后台」做成单向门：撤了回看就再也不看，只剩完成唤醒。
            #    于是「这个办法坏了我换一个」永远等到跑完才可能发生。
            # ⚠️ 这**不是**把东西从后台拿回前台 —— pill 不变、抽屉那行不搬。
            #    📌 位置和注意力正交：转过去的只是视线。
            # ⚠️ 排的是 `_FIRST_RECHECK_SEC`（60s）那一档，之后由模型自己
            #    `set_next_checkin` 接管 —— 📌 系统只负责「什么时候看第一眼」，
            #    「下一眼隔多久」始终是模型的判断。
            # ⚠️ 挂在 `_drain_inbox` 而不是新起一个定时器：**每一个持有过
            #    pipeline_lock 的地方结束时都会调它**，天然覆盖全部出口。
            #    📌 与那条既有判据同形：一个「锁释放后要做的动作」，
            #       必须挂在每一个持有那把锁的地方。
            try:
                from core.runtime import waitcond as _wc_lb
                for _wid in _wc_lb.parked_without_recheck():
                    _wc_lb.reschedule_wait(_wid, 60.0)
                    logger.info(f"[B1] 前台空了 → 视线转回后台（{_wid}，60s 后看一眼）")
            except Exception as _e_lb:
                logger.debug(f"[B1] 转视线回后台失败（完成唤醒仍在）: {_e_lb}")
            return
        # 按入队顺序取第一条（dict 在 3.7+ 保序）
        item_id = next(iter(parked))
        args = parked.pop(item_id)
        logger.info(f"[Inbox] 上一轮结束 → 接上排队的那条（{item_id}）"
                    f"，队列里还剩 {len(parked)} 条")
        _rt_inbox_claim(item_id)
        self._rt_inbox_running_id = (
            None if item_id.startswith(("mem_", "memwake_")) else item_id)

        # ⚠️ 两种排队项走**两条不同的恢复路径**，不能混：
        #    · 用户消息 → 起一轮新 turn，把原话喂进去
        #    · 唤醒意图 → 走 `_drive_wake` 恢复一个**已有的**挂起
        #    📌 这正是 `ItemKind` 刻意分两种的原因（答的不是同一个问题，就不合并）。
        if isinstance(args, tuple) and args and args[0] == "wake":
            # ⚠️ [] 现在可能带第四项 `note` —— 后台唤醒必须把**结果**一起带回去，
            #    否则模型醒过来手上没有那个结果，只能再问一遍。
            #    📌 **一个「稍后再处理」的队列，必须把「处理它需要的东西」一起排进去** ——
            #       只排一个 id 等于把上下文丢在了原地。
            # ⭐ 仍然兼容 3 元组（历史入队项 / 手动继续那条路）。
            _susp = args[1]
            _trig = args[2] if len(args) > 2 else "manual"
            _note = args[3] if len(args) > 3 else ""
            # 🔴🔴 [2026-08-22 实测] **唤醒这条路认领了，却从来没人消费它。**
            #
            # 上面 `_rt_inbox_claim(item_id)` 把它标成 CLAIMED，而消费只发生在
            # `_safe_execute_pipeline` 的收尾里 —— **唤醒不走那个函数**。
            # 于是这条记录**永远停在 CLAIMED**，而 `_claim` 的第一道闸是
            # 「已经有一条 CLAIMED → 直接返回 None」：
            #   → 此后**每一次**认领都静默失效
            #   → `delivery_count` 再也不涨
            #   → 之后每一次 consume 都破 `inbox_consumed_was_delivered`
            # 实测 里这条 ERROR 连着出现了 **5 次**，第一次正好在
            # 第一次 wake 走 drain 的 58 秒之后。
            #
            # 📌 **一条卡住的 CLAIMED，会把整条队列的投递记账全废掉** ——
            #    而 `delivery_count` 存在的唯一理由，就是回答
            #    「崩溃时这条给模型看过没有」。它一坏，那个问题就永远答不了了。
            # 📌 更一般的那条：**认领和消费必须在同一个人手里闭合。**
            #    这里认领在 `_drain_inbox`、消费在另一个函数，
            #    于是「新加一条不走那个函数的路径」就必然漏掉 —— 而且不报错。
            # ⭐ 修法不是「再补一个消费点」，是把这一条的收尾**交给它自己那条路**：
            #    谁起的 turn，谁负责收。
            asyncio.create_task(
                self._drive_wake(_susp, trigger=_trig, note=_note,
                                 inbox_item_id=item_id))
            return

        # ⭐⭐⭐ [无缝对话] 续接：喂进**已经存在的那个** nano 气泡，不新建。
        if isinstance(args, tuple) and args and args[0] == "cont":
            _, _q, _ib_, _im_, _th_ = args
            _rs_live = getattr(self, "_resp_state", None) or {}
            _rs_live["pending_epoch"] = False
            _box = _rs_live.get("container")
            if _box is None:
                logger.warning("[Seam] 续接时找不到 nano 块，放弃这一条（已在库里）")
                return
            # ⚠️ 这个标志由 `navigate_pipeline` 开头读一次就清 ——
            #    它的作用范围只有「下一次调用的开头」，不是一个长命状态。
            #    📌 本项目栽过的裸 bool 都是「长命 + 多处读写」；
            #       这个是「即读即清 + 单一读点」，形状不同。
            self._resp_continuation = True
            # ⭐⭐ [无缝 · 措辞] 段号 +1 并交给 orchestrator ——
            #    它据此往动态段里加一句「你这一段会和上一段拼在同一个气泡里」。
            #    ⚠️ 注入的是**事实**（呈现方式），不是「请假装连贯」那种演出指令。
            self._seam_part = int(getattr(self, "_seam_part", 1) or 1) + 1
            try:
                self.agent._seam_continuation_part = self._seam_part
            except Exception:
                pass
            logger.info(f"[Seam] 续接当前回应期（第 {self._seam_part} 段）"
                        f"—— 同一个 nano 气泡，不新建")
            asyncio.create_task(
                self._safe_execute_pipeline(_q, _box, _ib_, _im_, _th_, None))
            return

        try:
            with self._ui_scope():
                args[1].clear()          # 清掉「排队中」那行，让正常的 loading 接上
        except Exception:
            pass
        asyncio.create_task(self._safe_execute_pipeline(*args))

    # ── 回复状态计时器 ────────────────────────────────────────────────────

    # ⭐ 多久之后才承认「我在排队」。**与Subagent那个 5 秒是两个不同的数**
    #    （一个问「要不要 detach」，一个问「要不要告诉用户你在排队」），
    #    碰巧同值，刻意不合并 —— 📌 一个限制有两个含义时，
    #    合并它等于让将来调其中一个的人无声地改掉另一个。
    _QUEUED_NOTICE_SEC = 5.0

    async def _pending_epoch_timer(self, state: dict):
        """successor 回应期**还没轮到它跑**的那段时间，由它来写元信息行。

        ⚠️ 头 `_QUEUED_NOTICE_SEC` 秒**一个字都不改**：插话之后立刻被接上是
           常态，那时把 `thinking` 换成 `queued` 再换回来是纯噪音。
           📌 **一个只在异常时才需要出现的状态，不该在正常路径上闪一下。**
        ⚠️ 措辞不能是 `thinking` —— 它没在想，它在排队。
           📌 假事实的修法是**换成真话**，不是把话删掉（同 `still running` 那次）。
        ⭐ 转圈继续转：那一行的词汇表只有 ⠋（进行中）/ ✦（已结束）两个状态，
           而「有事在跑」此刻是真的 —— 藏起来会落进一个不存在的第三态。
        ⚠️ 它存在 `_status_timer_task` 这个**同一个键**里，所以
           `navigate_pipeline` 开头那段「换班前先取消旧的」天然管住它 ——
           📌 同一回应期只允许一个计时协程写该元信息行。
        """
        _BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        _i = 0
        while state.get("running") and state.get("pending_epoch"):
            await asyncio.sleep(0.12)
            _i += 1
            _lbl = state.get("status_lbl")
            if not _lbl:
                break
            elapsed = int(time.time() - state["start_time"])
            if elapsed < self._QUEUED_NOTICE_SEC:
                continue
            try:
                with self._ui_scope():
                    if state.get("spin_lbl"):
                        state["spin_lbl"].set_text(_BRAILLE[_i % len(_BRAILLE)])
                    # ⚠️ 这行字不在 Nano 气泡里（元信息行/系统状态）→ 命中固定文案
                    #    豁免，与 `thinking` / `still running` 同一套词汇表。
                    _lbl.set_text(f"queued · {elapsed}s")
            except Exception:
                break

    async def _resp_status_timer(self, state: dict):
        """终端风：braille 转圈 + "thinking · Xs"。每 ~0.12s 转一帧，文字随阶段变。"""
        STAGES = [(10, "thinking"), (20, "still thinking"), (35, "thinking more"), (50, "some more thinking")]
        _BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        _i = 0
        while state.get("running"):
            await asyncio.sleep(0.12)
            _i += 1
            elapsed = int(time.time() - state["start_time"])
            stage = "almost done thinking"
            for thr, lbl in STAGES:
                if elapsed < thr:
                    stage = lbl
                    break
            lbl_el = state.get("status_lbl")
            spin_el = state.get("spin_lbl")
            if not lbl_el:
                break
            # ⭐⭐⭐ [实测 2026-08-10] 载体还在跑时，**只换措辞，不动那两个图标**。
            #
            # 🔴 上一版这里 `spin_el.set_visibility(False)` + `set_text(f"{elapsed}s")`
            #    → 实测看到的是**一个裸的计时器 `62s`**：转圈没了、✦ 也没出来。
            #    那一行的词汇表只有两个状态（⠋ 进行中 / ✦ 已结束），
            #    藏了前者又不给后者，就落进了一个**不存在的第三态** ——
            #    读起来像「它坏了」。
            #    📌 **一个状态指示器有 N 个合法状态，任何路径都必须落在这 N 个里** ——
            #       「把它藏起来」通常不是第 N+1 个状态，而是「没有状态」。
            #
            # 🔴 而它的前提也不对。原注释写的是「Nano is not thinking while the
            #    carrier runs」—— 对 Nano 是真的，**但这一行不属于 Nano**，
            #    它属于**这一段回应期**（`_resp_state`），而那一段确实还开着、
            #    确实还有事在跑。
            #    📌 **一个「进行中」指示器属于它所在的那一段，
            #       不属于其中某一个参与者。**
            #
            # ⭐ 所以：转圈继续转（有事在跑，这是真事实），但**措辞要诚实** ——
            #    不能说 `thinking`（它没在想），说它在等那件事跑完。
            #    📌 假事实的修法是**换成真话**，不是**把话删掉**。
            try:
                with self._ui_scope():
                    if state.get("waiting_for_carrier"):
                        # ⚠️ 覆盖阶段词。这一行是系统状态栏（不是 Nano 的发言），
                        #    与既有的 `thinking / still thinking / …` 同一套词汇，
                        #    属于「不出现在 Nano 气泡里」那条豁免。
                        stage = "still running"
                    if spin_el:
                        spin_el.set_text(_BRAILLE[_i % len(_BRAILLE)])
                    lbl_el.set_text(f"{stage} · {elapsed}s")
            except Exception:
                break

    # ── 执行时自缩窗 ────────────────────────────────────────────────────
    def _screen_metrics(self):
        """主屏【逻辑】分辨率 (宽, 高)。

        pywebview 的 move()/resize() 按逻辑像素接收（内部再 ×DPI scale 转物理），
        但本进程是 Per-Monitor DPI 感知，GetSystemMetrics 返回的是物理像素——
        必须除以系统 DPI scale 换算成逻辑像素，否则缩窗位置在高 DPI 下算错（飞偏）。
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            phys_w = u.GetSystemMetrics(0)
            phys_h = u.GetSystemMetrics(1)
            try:
                scale = u.GetDpiForSystem() / 96.0
            except Exception:
                scale = 1.0
            if scale <= 0:
                scale = 1.0
            return int(phys_w / scale), int(phys_h / scale)
        except Exception:
            return 1536, 864

    @staticmethod
    def _relax_min_size(win, w: int, h: int) -> None:
        """改窗口的**最小尺寸下限**。放宽/收回都走它。

        🔴🔴 [2026-08-24] 这里**不能自己动手** —— 见文件顶部
           `_patch_webview_min_size` 那段长注释：pywebview 跑在**另一个进程**里，
           本进程的 `BrowserView.instances` 永远是空的。
        ⭐ 所以这里只做一件事：**往 NiceGUI 的跨进程方法队列里塞一条**，
           让子进程去调 `window.nano_set_min_size(w, h)`。
        ⚠️ 不能写成 `win.nano_set_min_size(...)`：`WindowProxy` 继承自
           `webview.Window`，我们又把方法挂在**类**上 —— 于是那样调等于
           **在主进程里就地执行**，回到原来那个什么都碰不到的处境。
           📌 一个继承来的方法，看起来跟代理的其它方法一模一样，
              但它不走队列 —— **这正是代理最会骗人的地方**（同 `_pick_folder`
              里那条「WindowProxy 把每个方法包成协程」的教训，一个硬币两面）。
        ⚠️ 队列是 fire-and-forget（没有回执），所以调用方要**留出时间**
           再 resize —— 见 `_enter_mini`。
        ⚠️ 整段吞异常：这是**观感优化**，不该有能力让缩窗这件事失败。
        """
        try:
            from nicegui.native import native as _nat
            _q = getattr(_nat, "method_queue", None)
            if _q is None:
                logger.debug("[F8] 没有 native 方法队列，最小尺寸下限没改成")
                return
            _q.put(("nano_set_min_size", (int(w), int(h)), {}))
            logger.info(f"[F8] 最小尺寸下限 → {w}x{h}（已投递给 webview 子进程）")
        except Exception as e:
            logger.debug(f"[F8] 改最小尺寸下限失败（mini 可能缩不下去）: {e}")

    # ⭐⭐ [2026-08-25] **切换窗口形态之后，把聊天区推到底。**
    #
    # 🔴 问题：进 mini 之后滚动条不跟着新消息往下走，用户看不到最新的那条。
    # ⭐ 成因是**视口整个换了**：1200×820 → 390×560，`.nano-mini` 那套 CSS
    #    还把 header 收掉、`main-chat-col` 改成 `height:100vh`。
    #    原来贴着底的位置，在新视口里已经不是底了 —— 而**没有任何人重新算过**。
    # ⚠️ 用 JS 直接推 `scrollTop = scrollHeight`，不用 `scroll_to(percent=1.0)`：
    #    📌 percent 是**按当时的高度**算的，而我们正处在「高度刚变、可能还没
    #       reflow 完」的那一刻 —— 一个依赖旧高度的百分比，正是这个 bug 本身。
    # ⚠️ Quasar 的滚动区真正在滚的是里面那层 `.q-scrollarea__container`，
    #    不是挂 class 的那个根 —— 两个都试一遍。
    # ⚠️ 分两拍推（立刻 + 120ms）：切模式会触发 CSS 过渡与 reflow，
    #    📌 只推一次的话，推的可能是**变形途中**的那个高度。
    # ⚠️ 整段吞异常：这是观感，不该有能力让缩窗/复原失败。
    _SCROLL_BOTTOM_JS = (
        "(function(){try{"
        "var r=document.querySelector('.nano-chat-scroll');if(!r)return;"
        "var b=r.querySelector('.q-scrollarea__container')||r;"
        "var go=function(){b.scrollTop=b.scrollHeight;};"
        "go();setTimeout(go,120);"
        "}catch(e){}})()"
    )

    # 🔴 流式专用：**不用 percent，不用动画**。
    #    `set_content()` 重渲染 markdown 的一瞬间 scrollHeight 会塌，
    #    `percent=1.0 × 塌掉的高度` ≈ 顶部 —— 那就是 用户看到的"飞到上面"；
    #    而 `duration>0` 的动画会被下一个 delta 打断、互相抢 —— 那是"来回抽"。
    #    📌 5181 行那段注释早就写过这个机制（当时是为缩窗写的）：
    #       「一个依赖旧高度的百分比，正是这个 bug 本身」。
    #       ⚠️ 规则写在一个实例旁边，就被读成只管那个实例。
    _STREAM_PIN_JS = (
        "(function(){try{"
        "var r=document.querySelector('.nano-chat-scroll');if(!r)return;"
        "var b=r.querySelector('.q-scrollarea__container')||r;"
        # ⭐ 只在**本来就贴着底**时才跟：用户往上翻看历史时不该被拽回来。
        #    📌 自动滚动的语义是「保持贴底」，不是「强制到底」。
        "if(b.scrollHeight-b.scrollTop-b.clientHeight>80)return;"
        "b.scrollTop=b.scrollHeight;"
        "}catch(e){}})()"
    )

    def _pin_chat_bottom(self) -> None:
        """流式输出期间保持贴底。⚠️ 节流 80ms —— 每个 delta 发一次 JS 是一个来回，
        📌 修一个观感问题不该换来一个性能问题。"""
        import time as _t
        _now = _t.monotonic()
        if _now - getattr(self, "_last_pin_ts", 0.0) < 0.08:
            return
        self._last_pin_ts = _now
        try:
            ui.run_javascript(self._STREAM_PIN_JS)
        except Exception:
            pass

    def _scroll_chat_to_bottom(self) -> None:
        try:
            ui.run_javascript(self._SCROLL_BOTTOM_JS)
        except Exception as e:
            logger.debug(f"[UI] 推到底失败（不影响功能）: {e}")

    async def _enter_mini(self):
        """缩成右上角 mini：缩 native 窗口 + 收侧栏/隐 header + 显提示。不打断任务。

        由 Nano 调 set_window_mode('mini') 触发（模型驱动），不再硬编码自动检测。
        """
        if self._mini_active:
            return
        # ⭐ [2026-08-23 用户用红框标的尺寸] 780×760 → **390×560**。
        #    mini 窗存在的意义是**让开**，越小让得越干净；而它仍要能读一行对话，
        #    所以宽度收得比高度狠（竖长条，贴右上角）。
        MINI_W, MINI_H = 390, 560
        try:
            from nicegui import app as _app
            win = _app.native.main_window
        except Exception:
            win = None
        if win is not None:
            try:
                # 走进程间队列，加超时兜底，别让取几何把整轮卡死
                ow, oh = await asyncio.wait_for(win.get_size(), timeout=2)
                ox, oy = await asyncio.wait_for(win.get_position(), timeout=2)
                self._mini_orig_geom = (int(ow), int(oh), int(ox), int(oy))
            except Exception:
                self._mini_orig_geom = self._mini_orig_geom or (1200, 820, 80, 60)
            sw, sh = self._screen_metrics()
            try:
                # resize 与 move 分两条进程间消息、各自起线程，会抢着调 SetWindowPos
                # （winforms 的 resize 内部也重设坐标），并发竞争导致 move 被覆盖、窗口
                # 停在原位。中间让一下，错开两次调用，move 才稳定生效。
                # 🔴🔴 [2026-08-23 实测根因] `min_size = (780, 760)`（app 启动时设的）
                #    **把 resize 直接挡掉了** —— 请求 507 但窗口纹丝不动，
                #    而下面的坐标又按 507 算 → 右边 780-507=273px 飞出屏幕。
                #    ⭐ 用户的观察正好指到这里：「之前是能对齐的」——
                #       因为之前请求的就是 780，**请求值恰好等于实际值**。
                #
                # 📌 那个 `min_size` 答的是「**用户能把窗口拖多小**」
                #    （注释原话：缩到底就停，内容永远装得下）——
                #    而 mini 是**另一个模式**，有自己的 CSS 布局（`.nano-mini`）。
                #    📌 **一个数字在答两个问题**，于是防用户的那条顺手把我们自己也拦了。
                # ⭐ 修法：进 mini 时**临时放宽**，退出时还原。
                # ⚠️ 放宽失败**不抛**（pywebview 版本差异）—— 那时 resize 仍会被挡，
                #    但下面「读回实际尺寸」那段保证它**照样不会飞出屏幕**，
                #    只是缩不下去。📌 降级要降到「效果差一点」，不是「更坏」。
                # 🔴🔴 [2026-08-23 查证] 上一版写的是 `win.min_size = (...)` ——
                #    **那是个死写入**：pywebview 只在**创建窗口那一刻**读一次
                #    `window.min_size`（`winforms.py:210`，"Set the initial size now
                #    that we have a window handle"），之后再改没有任何人看它。
                #    📌 又写了一次「写好但零读取点」的东西 —— 而且它**不报错**，
                #       表现就是 用户看到的「位置对了，尺寸纹丝不动」。
                # ⭐ 真正的闸是 WinForms `Form.MinimumSize`，它**能运行时改**。
                # ⚠️ 它要**物理像素**（同 pywebview 内部的写法：逻辑 × scale），
                #    而 `resize()` 收的是**逻辑像素**（内部自己乘 scale）——
                #    📌 两个 API 单位不同，是这一带最容易错的地方，各按各的来。
                self._relax_min_size(win, MINI_W, MINI_H)
                # ⚠️ 放宽下限是**投递到另一个进程**的（无回执），
                #    而 resize 走同一条队列 —— 执行器给每条消息**各起一个线程**，
                #    所以两者会抢。📌 先让一下，别让 resize 跑在放宽之前，
                #       否则它照样被旧的 780×760 钳住（那就是这个 bug 本身）。
                await asyncio.sleep(0.15)
                win.resize(MINI_W, MINI_H)
                await asyncio.sleep(0.2)
                # 🔴🔴 [2026-08-23 实测] 「不仅没感觉变小，而且**有一半飞到屏幕
                #    外面去了**」。旧写法用 `MINI_W`（**请求的尺寸**）去算右上角坐标 ——
                #    而 `_screen_metrics()` 返回的是 **DPI 缩放后的逻辑像素**，
                #    `win.resize/move` 未必是同一套单位。两边单位一旦不同，
                #    `sw - MINI_W - 16` 就会把窗口推出屏幕。
                # 📌 **别用「我请求的尺寸」代替「它实际的尺寸」** ——
                #    请求和结果之间隔着一层我们不掌握的换算。
                # ⭐ 改成**读回实际尺寸再定位**，并且**钳死在屏幕内**：
                #    这样不管中间那层怎么换算，窗口都不会跑出去。
                try:
                    _aw, _ah = await asyncio.wait_for(win.get_size(), timeout=2)
                    _aw, _ah = int(_aw), int(_ah)
                except Exception:
                    _aw, _ah = MINI_W, MINI_H
                # ⚠️ **别做「无缝紧贴屏幕边缘」，观感反而不好** → 留 32px。
                _M = 32
                _x = max(0, min(sw - _aw - _M, sw - 1))
                _y = max(0, min(_M, sh - _ah - 1))
                logger.info(f"[F8] mini 定位：请求 {MINI_W}x{MINI_H} → 实际 {_aw}x{_ah}"
                            f" | 屏幕 {sw}x{sh} → 落点 ({_x},{_y})")
                win.move(_x, _y)
            except Exception:
                pass
        with self._ui_scope():
            try:
                if self.drawer: self.drawer.hide()
                if self.right_drawer: self.right_drawer.hide()
            except Exception:
                pass
            ui.run_javascript("document.body.classList.add('nano-mini');")
            self._scroll_chat_to_bottom()
            if self._mini_hint: self._mini_hint.style('display:flex;')
            if self._mini_bar: self._mini_bar.style('display:flex;')
        self._mini_active = True
        # ⭐⭐ mini 窗 = GUI 模式 = 被动挂起全量监控。三者绑定。
        #    ⚠️ `_mini_active` 从此**只是投影**，权威在租约里 ——
        #       否则作用域的判据落在一个 UI 标志上。
        try:
            from core.runtime import oslease as _ol_g
            _ol_g.open_gui_session("mini 窗打开")
        except Exception as _e:
            logger.warning(f"[A3] 开 GUI 模式失败（不影响缩窗本身）: {_e}")
        self._mini_start_time = time.time()
        try:
            self._mini_timer_task = asyncio.create_task(self._mini_timer_loop())
        except Exception:
            self._mini_timer_task = None

    async def _mini_timer_loop(self):
        """缩窗期间每秒刷新小胶囊上的计时器（mm:ss）。"""
        while self._mini_active:
            try:
                el = self._mini_timer_lbl
                if el:
                    s = int(time.time() - self._mini_start_time)
                    with self._ui_scope():
                        el.set_text(f"{s // 60}:{s % 60:02d}")
            except Exception:
                pass
            await asyncio.sleep(1)

    async def _exit_mini(self):
        """恢复全屏几何 + 还原布局 + 收起提示/胶囊。mini 关 = 本次临时 auto 结束。"""
        if not self._mini_active:
            return
        self._mini_active = False
        # ⚠️ 注意这条的失效条件：**一个 UI 事件**（mini 窗关掉）。
        # 一条"授权还有没有效"的判断，答案取决于某个窗口开着没有 —— 这正是
        # `AuthorizationLease` 要换掉的东西（授权该有自己的寿命）。观测期只镜像不改。
        self._temp_auto = False   # 任何 mini 窗关 → 临时 auto 失效
        _rt_auto_revoke()
        # ⭐⭐ mini 窗关 = 退出 GUI 模式 = 被动挂起停止监控。
        try:
            from core.runtime import oslease as _ol_g
            _ol_g.close_gui_session()
        except Exception as _e:
            logger.warning(f"[A3] 关 GUI 模式失败: {_e}")
        if self._mini_timer_task:
            try:
                self._mini_timer_task.cancel()
            except Exception:
                pass
            self._mini_timer_task = None
        try:
            from nicegui import app as _app
            win = _app.native.main_window
        except Exception:
            win = None
        if win is not None and self._mini_orig_geom:
            ow, oh, ox, oy = self._mini_orig_geom
            try:
                # 先 restore() 取消最小化——窗口若被截图最小化过、或用户手动
                # 最小化到了任务栏，光 resize/move 不会把它从任务栏拉出来，会永远
                # 卡在任务栏点不出来。restore 先把它拉回正常态，再设几何。
                # ⭐ 还原下限 —— 进 mini 时临时放宽过（见 `_enter_mini`）。
                #    📌 一个「临时放宽」如果没有配套的还原，它就不是临时的，
                #       而用户从此可以把窗口拖到内容溢出。
                self._relax_min_size(win, 780, 760)
                win.restore()
                await asyncio.sleep(0.15)
                win.resize(ow, oh)
                await asyncio.sleep(0.2)   # 同 _enter_mini：错开 resize/move 竞争
                win.move(ox, oy)
            except Exception:
                pass
        with self._ui_scope():
            try:
                if self.drawer: self.drawer.show()
            except Exception:
                pass
            ui.run_javascript("document.body.classList.remove('nano-mini');")
            self._scroll_chat_to_bottom()
            if self._mini_hint: self._mini_hint.style('display:none;')
            if self._mini_bar: self._mini_bar.style('display:none;')

    async def _exit_mini_if_active(self):
        """turn 结束/报错/急停的兜底：若 Nano 忘了调 full，自动恢复全屏。"""
        if self._mini_active:
            await self._exit_mini()

    def _show_mini_auth_dialog(self, on_approve, on_reject):
        """缩窗前的临时 auto 授权窗：只"同意/拒绝"。同意→开本次临时 auto + 缩窗。"""
        client = self._ui_client
        if client is None:
            if on_reject:
                on_reject()
            return
        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dialog, ui.card().style(
                'width:380px; max-width:94vw; background:var(--nano-panel); '
                'border:1px solid rgba(var(--nano-warn-rgb),0.25); border-radius:16px; padding:22px;'
            ):
                with ui.row().classes('items-center').style('gap:10px; margin-bottom:8px;'):
                    ui.html(NANO_AVATAR_SVG).style('width:22px; height:22px; flex-shrink:0;')
                    ui.label('开始操作你的屏幕').style('font-size:var(--nano-fs-xl); font-weight:600; color:var(--nano-fg);')
                ui.label(
                    '接下来我要点击/输入来操作屏幕，需要本次任务的操作授权。'
                    '授权后我会缩到右上角连续完成，期间不再逐个打扰你；任务结束自动收回授权。'
                ).style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft); line-height:1.6;')
                ui.label('随时按 Ctrl + ` 可立即停止。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); margin-top:6px;')

                async def _do_approve():
                    dialog.close()
                    self._temp_auto = True           # 本次任务临时 auto（mini 关即失效）
                    _rt_auto_grant("用户在 mini 窗批准了本次任务")   # shadow
                    await self._enter_mini()
                    if on_approve:
                        on_approve()

                def _do_reject():
                    dialog.close()
                    if on_reject:
                        on_reject()

                with ui.row().style('width:100%; justify-content:flex-end; gap:8px; margin-top:16px;'):
                    ui.button('拒绝', on_click=_do_reject).props('flat').style('color:var(--nano-fg-soft) !important;')
                    ui.button('同意', icon='check', on_click=_do_approve).props('unelevated').style(
                        'background:var(--nano-warn-fill); color:#fff; border-radius:10px; padding:0 16px;')
            dialog.open()

    # ── auto 模式 ──────────────────────────────────────────────────────────
    def _load_global_auto(self) -> bool:
        # ⚠️ 路径走 `os_dsl.os_state_path`，不在这里另算一遍 ——
        #    这份数据跟着用户走，不能留在会被升级覆盖的 `config/` 里。
        try:
            import json as _j
            p = os_dsl.os_state_path()
            if p.exists():
                return bool(_j.loads(p.read_text(encoding="utf-8")).get("auto_mode", False))
        except Exception:
            pass
        return False

    def _save_global_auto(self, on: bool):
        try:
            import json as _j
            p = os_dsl.os_state_path()
            raw = {}
            if p.exists():
                raw = _j.loads(p.read_text(encoding="utf-8"))
            raw["auto_mode"] = bool(on)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_j.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Auto] 保存 auto_mode 失败: {e}")

    def _auto_on(self) -> bool:
        """当前是否处于 auto（全局开 或 本次任务临时开）→ 所有 OS 确认自动通过。

        ⭐⭐ **权威已从 `self._temp_auto` 换成授权租约。**

        旧写法 `self._global_auto or self._temp_auto` 的问题**不是"这个 bool 会写错"**，
        而是 `_temp_auto` 的**失效条件写在 UI 里**（`任何 mini 窗关 → False`）——
        一条"用户还授权着吗"的判断，答案取决于某个窗口开着没有。
        📌 点名的方向错误：**用 UI 形态当授权作用域的锚点。**

        ⚠️ `_global_auto` **刻意不动**：它是用户在设置里显式拨的模式开关，
           存在 `data/os_state.json` 里、跨重启存活、打开就能明显看到当前是什么模式。
           📌 它没有生命周期问题，所以不在这次迁移范围内。

        ⚠️ `_temp_auto` 仍在维护但**只读不作权威** —— 切写那一步删。
           它是实测验证前的回退路，同 `_os_task_busy` 当时的处理。
        """
        # 切读期的对答案点：拿旧 bool 验证新权威（方向与观测期相反）
        _rt_auto_compare(bool(self._temp_auto))
        # ⭐ 公式搬到 `dsl.auto_authorization_on()`（唯一出处）——
        #    📌 模型侧也要读这个判断，而一个要被两层同时消费的判断，
        #       不该只写在其中一层里：另一层要么读不到，要么照着自己的理解再写一遍。
        #    ⚠️ `_global_A` 仍然参与：它是本进程刚拨过、可能还没落盘的那一半。
        try:
            from core.os_layer import dsl as _dsl_auto
            return bool(self._global_auto or _dsl_auto.auto_authorization_on())
        except Exception:
            return bool(self._global_auto or _rt_auto_authorized())

    def _toggle_global_auto(self):
        self._global_auto = not self._global_auto
        self._save_global_auto(self._global_auto)
        self._refresh_auto_chip()
        # 只在【开启】Auto 时提示（黄色）；切回 ask-permission 不需要提示。
        if self._global_auto:
            ui.notify(self._AUTO_ON_NOTICE, type='warning')

    def _refresh_auto_chip(self):
        """刷新输入框下方 Auto chip 的样式（开=高亮琥珀，关=灰）。"""
        chip = self._auto_chip
        if not chip:
            return
        try:
            _lbl = getattr(self, '_auto_label', None)
            if _lbl:
                _lbl.set_text('Auto' if self._global_auto else 'Ask permission')
            if self._global_auto:
                chip.style('display:flex; align-items:center; gap:4px; cursor:pointer; '
                           'font-size:var(--nano-fs-sm); padding:2px 10px; border-radius:999px; '
                           'background:rgba(var(--nano-warn-rgb),0.12); color:var(--nano-warn); '
                           'border:1px solid rgba(var(--nano-warn-rgb),0.25); font-weight:500;')
            else:
                chip.style('display:flex; align-items:center; gap:4px; cursor:pointer; '
                           'font-size:var(--nano-fs-sm); padding:2px 10px; border-radius:999px; '
                           'background:transparent; color:var(--nano-fg-soft); '
                           'border: 1px solid var(--nano-border);')
        except Exception:
            pass

    def _set_auto(self, on: bool):
        """Auto 下拉选择（Ask permission=关 / Auto=开）。"""
        self._global_auto = bool(on)
        self._save_global_auto(self._global_auto)
        self._refresh_auto_chip()
        if self._global_auto:
            ui.notify(self._AUTO_ON_NOTICE, type='warning')

    def _open_auto_menu(self):
        """打开左下 Auto 下拉：Ask permission / Auto mode，当前项高亮（不用对钩）。"""
        box = getattr(self, '_auto_menu_box', None)
        menu = getattr(self, '_auto_menu', None)
        if box is None or menu is None:
            return
        box.clear()
        with box:
            _it1 = ui.menu_item('Ask permission', on_click=lambda: self._set_auto(False))
            _it2 = ui.menu_item('Auto mode', on_click=lambda: self._set_auto(True))
            (_it2 if self._global_auto else _it1).classes('nano-active')
        menu.open()

    # ── 主动智能 effort（Low/Medium/High = UserModeAnchor）+ 可解释面板 ──────
    _EFFORT_TO_MODE = {"Low": "quiet", "Medium": "balanced", "High": "active"}
    _MODE_TO_EFFORT = {"quiet": "Low", "balanced": "Medium", "active": "High"}

    def _set_effort(self, level: str):
        try:
            from core.proactive.intel.affect import get_affect
            get_affect().set_user_mode(self._EFFORT_TO_MODE.get(level, "balanced"))
        except Exception:
            pass
        self._refresh_effort_chip()

    def _refresh_effort_chip(self):
        lbl = getattr(self, '_effort_label', None)
        if not lbl:
            return
        try:
            from core.proactive.intel.affect import get_affect
            mode = get_affect().snapshot().get("user_mode", "balanced")
            lbl.set_text(self._MODE_TO_EFFORT.get(mode, "Medium"))
        except Exception:
            lbl.set_text("Medium")

    def _open_effort_menu(self):
        """打开右下角 effort 下拉：三档选择 + 折叠可解释面板（最多 5 行）。"""
        box = getattr(self, '_effort_menu_box', None)
        menu = getattr(self, '_effort_menu', None)
        if box is None or menu is None:
            return
        box.clear()
        try:
            from core.proactive.intel.affect import get_affect
            from core.proactive.intel.ledger import get_ledger
            mode = get_affect().snapshot().get("user_mode", "balanced")
            cur = self._MODE_TO_EFFORT.get(mode, "Medium")
            info = get_ledger().explain_readable(cap=2)
        except Exception:
            cur, info = "Medium", {"items": [], "total": 0}
        with box:
            for lv in ("Low", "Medium", "High"):
                _it = ui.menu_item(lv, on_click=lambda l=lv: self._set_effort(l))
                if lv == cur:
                    _it.classes('nano-active')   # 当前项高亮（不用对钩，对齐左侧 Auto）
            ui.separator()
            with ui.column().classes('gap-0').style('padding:6px 14px; max-width:240px;'):
                _cn = {"Low": "偏安静", "Medium": "均衡", "High": "更主动"}
                ui.label(f"当前：{_cn.get(cur, cur)}").style('font-size:var(--nano-fs-sm); color:var(--nano-dim);')
                if info["total"] > 0:
                    _items = "、".join(info["items"])
                    _more = f" 等{info['total']}项" if info["total"] > len(info["items"]) else ""
                    ui.label(f"已降低：{_items}{_more}").style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
                    ui.button("全部恢复默认", on_click=self._reset_proactive_prefs).props('flat dense').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-warn); padding:0;')
                else:
                    ui.label("还没学到你的偏好").style('font-size:var(--nano-fs-sm); color:var(--nano-dim);')
        menu.open()

    def _reset_proactive_prefs(self):
        try:
            from core.proactive.intel.ledger import get_ledger
            get_ledger().reset()
            ui.notify("已恢复主动偏好默认", type='positive')
        except Exception:
            pass
        self._refresh_effort_chip()

    # ── 发送消息 ──────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════
    # 发送 / 终止 —— 同一颗按钮，两个身份
    # ══════════════════════════════════════════════════════════════════════
    # 用户提过两条，上一轮只做了第二条：
    #   > 「一个是输入框为空 我直接点终止按钮
    #   >   一个是输入框不为空 终止按钮检测到输入框不为空 变成发送按钮」
    #
    # 🔴 **早先的设计曾写「终止按钮不单独修，做完自然消失」—— 那条是错的。**
    # 📌 **两个机制的出口方向相反**：无缝中断的出口是「带着新输入**继续**决策」，
    #    终止的出口是「**停下**，别做了」。做完前者不会自动得到后者。
    # ⭐⭐ 决定性的论据：如果想让它停、而唯一办法是**打字告诉它「停」**，
    #    那么**模型可能不停**（理解成别的意思、或觉得该先收个尾）。
    #    📌 **一个「停止」能力如果依赖被停止的那一方理解你的意思，
    #       它就不是停止能力。**

    def _turn_running(self) -> bool:
        """现在有没有一轮在跑。⚠️ 用 `pipeline_lock` —— 它就是那个事实的权威。"""
        try:
            return self.pipeline_lock.locked()
        except Exception:
            return False

    def _refresh_send_btn(self) -> None:
        """按当前真实状态重画那颗按钮。

        ⚠️ **level-triggered** —— 每 0.4 秒照现状重画，不靠"输入时记得改图标"。
           📌 本轮反复栽的都是「靠所有调用点都记得同步」的写法；
              这种只要漏一处就永久错位，而 level-triggered 漏不掉。
        ⚠️ 文案/图标属**系统级控件**，不出现在 Nano 气泡里 → 命中固定文案豁免
           第 3、5 条，允许写死。
        """
        try:
            btn = getattr(self, "_send_btn", None)
            if btn is None:
                return
            _empty = not (self.input_field.value or "").strip()
            _stopish = _empty and self._turn_running()
            if getattr(self, "_send_btn_is_stop", None) is _stopish:
                return                      # 状态没变，不重画（省 socket 流量）
            self._send_btn_is_stop = _stopish
            if _stopish:
                btn.props('icon=stop')
                btn.style('width:34px; min-width:34px; height:30px; min-height:30px; '
                          'padding:0; align-self:center; flex-shrink:0; '
                          'background:var(--nano-stop-fill) !important; color:#fff !important; '
                          'border:none; border-radius:var(--nano-btn-radius); transition:all 0.2s;')
                btn.tooltip('终止当前任务')
            else:
                btn.props('icon=arrow_upward')
                btn.style('width:34px; min-width:34px; height:30px; min-height:30px; '
                          'padding:0; align-self:center; flex-shrink:0; '
                          'background:var(--nano-send-bg) !important; color:var(--nano-send-fg) !important; '
                          'border:none; border-radius:var(--nano-btn-radius); transition:all 0.2s;')
                btn.tooltip('发送')
        except Exception as e:
            logger.debug(f"[Stop] 刷新按钮失败（忽略）: {e}")

    def _on_send_or_stop(self):
        """按钮的唯一入口。**身份由「输入框空不空 + 有没有在跑」现场判定。**

        ⚠️ 刻意**不看 `_send_btn_is_stop`** —— 那是**渲染状态**，
           而这里要的是**此刻的事实**。
           📌 用「界面现在长什么样」去决定「该做什么」，就是让 UI 反过来当权威 ——
              而本项目的判据是**UI 必须是权威状态的忠实投影**，不是权威本身。
              （渲染有 0.4 秒的滞后，照它判会在边界上做错事。）
        """
        _empty = not (self.input_field.value or "").strip()
        _has_attach = bool(self._pending_image_bytes) or bool(self._temp_files)
        if _empty and not _has_attach and self._turn_running():
            self._request_stop()
            return
        self.start_pipeline_task()

    def _request_stop(self):
        """确定性终止：**不过模型**，在最近的动作边界停下。"""
        try:
            self.agent.request_stop("用户点了终止按钮")
        except Exception as e:
            logger.error(f"[Stop] 请求终止失败: {e}")
            return
        # ⚠️ 立刻给一点反馈 —— 真正停下要等到下一个动作边界（可能是几百毫秒，
        #    也可能要等一个不可阻断的动作跑完）。
        #    📌 早先那条判据在这里复现：**「已提出」和「已生效」是两件事**，
        #       接管状态条当初也是因为混了这两件事才让用户觉得它在骗人。
        try:
            _rs = getattr(self, "_resp_state", None) or {}
            if _rs.get("status_lbl"):
                with self._ui_scope():
                    _rs["status_lbl"].set_text("正在停下…")
        except Exception:
            pass
        self._refresh_send_btn()

    def _handoff_response_epoch(self, predecessor: dict, successor: dict) -> None:
        """用户插话时关闭旧 UI 回应，并把仍活着的延迟产出交给新回应。

        两个容器都已经按真实时间顺序在 chat 根节点中，绝不移动/删除。等待 pill
        仍画在 predecessor（它是已经发生的事实），只把 future wake 的写入归属迁移。
        """
        if not predecessor or not successor or predecessor is successor:
            return
        # 当前回应的权威必须保持为新用户消息下面的 successor。旧 pipeline 靠自己
        # 已捕获的局部 state 完成协作式退出，不需要也不允许恢复全局指针。
        self._resp_state = successor
        _moved = 0
        for _entry in (getattr(self, "_waiting_pills", None) or {}).values():
            if not _entry.get("done") and _entry.get("resp_state") is predecessor:
                _entry["resp_state"] = successor
                _moved += 1
        if _moved:
            logger.info(f"[Seam] {_moved} 条活等待的 future wake 已交给 successor")

        predecessor["running"] = False
        # 连续多次插话时，前一个 successor 可能还没真正开跑：它没有任何输出事实，
        # 只是一个占位气泡。后一个用户消息到达后应把这个空占位折叠掉，形成
        # “多条用户更新 → 一个最终 Nano 回应”，不能留下空 `nano ❯`。
        # ⭐⭐ [2026-08-22] 判据从 `pending_epoch` 放宽到**「它到底产出过没有」**。
        #
        # 🔴 实测：一个回看气泡刚开、一个字还没写，就被新消息隔开 ——
        #    留下一个孤立的 `nano ❯`。而这段代码上面那句注释写的正是
        #    「不能留下空 `nano ❯`」：**这个坑已经被认出来过，只是判据没覆盖到。**
        # 📌 `pending_epoch` 是**近似物**（「它是排队没启动的那种」），
        #    而真正要问的是 **「这个容器有没有产出过任何东西」** ——
        #    别用近似物回答一个能精确回答的问题（本项目反复出现的那条）。
        # ⚠️ 三样都算「产出」：正文、工具卡、以及**已经画上去的等待 pill**
        #    （那条 pill 是「已经发生的事实」，上面的迁移逻辑明说它不搬）。
        #    📌 少算一样，就会删掉一个其实有内容的气泡 —— 那比留个空头严重得多，
        #       所以这里的默认必须是**留着**，只有三样全空才折叠。
        _nothing_shown = (
            not (predecessor.get("current_text") or "").strip()
            and not int(predecessor.get("tool_count") or 0)
            and not any(_e.get("resp_state") is predecessor
                        for _e in (getattr(self, "_waiting_pills", None) or {}).values())
        )
        if predecessor.get("pending_epoch") or _nothing_shown:
            _box = predecessor.get("container")
            if _box is not None:
                try:
                    with self._ui_scope():
                        self.chat_container.remove(_box)
                except Exception as e:
                    logger.debug(f"[Seam] 折叠未启动的空回应失败（不影响交接）: {e}")
            return

        _meta = predecessor.get("meta_row")
        if _meta is not None:
            try:
                with self._ui_scope():
                    _meta.delete()
            except Exception as e:
                logger.debug(f"[Seam] 收 predecessor 元信息行失败（不影响交接）: {e}")
            predecessor["meta_row"] = None

    def start_pipeline_task(self):
        query = self.input_field.value.strip()
        # 有附件时允许空文字发送（上传图片/文件后无需另输文字）
        has_attach = bool(self._pending_image_bytes) or bool(self._temp_files)
        if not query and not has_attach:
            return
        self._current_query = query  # episodic: 记录本轮用户输入
        if usage_tracker.cap_status() == "hard":
            cfg = usage_tracker.load_config()
            cost = usage_tracker.today_cost()
            ui.notify(
                f'今日用量 {_cur()}{cost:.2f} 已达上限 {_cur()}{cfg["hard_cap_usd"]:.2f}，请在设置中调整限额',
                type='negative', icon='block', timeout=5000
            )
            return
        # ⭐⭐⭐ 这里原来是：
        #        if self.pipeline_lock.locked():
        #            ui.notify('内核正在处理中，请稍候...'); return
        #    —— **用户打的字直接被丢掉**，得自己记着重发一遍。
        #
        # 📌 与 那个「闸 vs 挂起」完全同形：
        #    **闸的出口是失败，队列的出口是稍后处理。**
        #    **一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。**
        #    上一次是让 Nano 撞墙（拿不到租约→动作失败→结束 turn），
        #    这次是让用户重新打一遍字。
        #
        # ⭐ 现在**无论忙不忙都照常往下走**（渲染气泡、备好附件）——
        #    唯一的差别落在最后那一步：忙就不起 pipeline，交给队列。
        #    ⚠️ 刻意**不在这里 return**：用户按了发送就该立刻看见自己那条消息，
        #       「用户的话被收下了」这件事不该等到内核闲下来才可见。
        _rt_inbox_busy = self.pipeline_lock.locked()
        # ⚠️⚠️⚠️ **必须在这里就把活着的那个回应期快照下来。**
        #
        # 🔴 打回的那个 bug 就是漏了这一步：把「忙不忙」的判断放在了函数开头，
        #    却把「忙」的分支放在函数**末尾** —— 而中间那两百行**无条件**做了两件事：
        #      ① `self._last_meta_row.delete()` → 把**正在转的那个 spinner 元信息行删了**
        #      ② 新建一个 nano 块并**覆盖 `self._resp_state`**
        #    于是分支里读到的是**刚被覆盖的新状态**，`move` / `remove` 全作用在
        #    那个新空块上；而真正活着的那一轮靠 `navigate_pipeline` 开头捕获的
        #    **局部引用**继续跑（所以第一条答案照样出来），续接却拿到一个
        #    **已被移除的容器** → 输出看不见。
        #
        # 📌 **一个「要不要做 X」的判断，和「不做 X」的那个分支之间，
        #    不许有任何会改变 X 前提的代码。**
        #    —— 判断和分支离得越远，中间那段就越可能把前提改掉，而且**不会报错**。
        _seam_live = getattr(self, "_resp_state", None) if _rt_inbox_busy else None

        from nicegui import context as _ctx
        self._ui_client = _ctx.client

        self.input_field.value = ''
        self.model_lbl.style('color:var(--nano-fg-soft); font-size:var(--nano-fs-sm);')

        # 先取出本轮附件信息（发送前快照）。
        # 必须在 _temp_files 被清空前复制，否则气泡里看不到文件标签。
        _img_bytes_preview = self._pending_image_bytes
        _img_mime_preview  = self._pending_image_mime
        _temp_files_preview = list(self._temp_files)   # 浅拷贝，后面清空原列表不影响此处渲染

        self._clear_empty_state_greeting()
        # 用户发话 = 用户唤醒源触发，orchestrator 会恢复所有 active 挂起，
        # 这里同步把还在跳的等待 pill 收尾（去掉计时器/按钮）。
        self._settle_all_waiting_pills(final_text="▶ 你回来了，继续")

        with self.chat_container:
            with ui.column().classes('w-full items-start mb-4'):
                    # ── 附件区：图片缩略图 + 文件名标签，在文字上方 ──────────
                    if _img_bytes_preview or _temp_files_preview:
                        with ui.column().classes('w-full gap-2 mb-3'):
                            if _img_bytes_preview:
                                import base64 as _b64
                                _b64str = _b64.b64encode(_img_bytes_preview).decode()
                                # ⭐ 已明确那一半：**用户自己传的图也点不开**。
                                chat_image(f"data:{_img_mime_preview};base64,{_b64str}",
                                           alt="你发送的图片", thumb_h=160)
                            if _temp_files_preview:
                                with ui.row().classes('items-center gap-1.5 flex-wrap'):
                                    for _tf in _temp_files_preview:
                                        with ui.row().classes('items-center gap-1 rounded-lg px-2 py-1').style(
                                            'background:rgba(var(--nano-amber-rgb), 0.12); border:1px solid rgba(var(--nano-amber-rgb), 0.25);'
                                        ):
                                            ui.icon('description').style('font-size:var(--nano-fs-md); color:var(--nano-fg-soft);')
                                            ui.label(_tf["filename"]).style(
                                                'font-size:var(--nano-fs-sm); color:var(--nano-dim); '
                                            )
                    # ⭐ 这条消息是"引用回复"时，上方挂一条被回答的原文。
                    #
                    # 用户的要求：发出去之后要能看出**这条在回答哪个**，
                    # 不能只在发之前有提示 —— 聊天记录往上翻的时候更需要它。
                    # 形态与右键菜单那个入口**必须一致**（同一个动作，只是入口不同），
                    # 所以这里的样式将来要和选中文字 replay 复用。
                    _rt = self._reply_target or {}
                    _rt_q = _rt.get("q") or ""
                    _rt_iid = _rt.get("iid") or ""
                    # ⭐ 选中文字的引用**长得和引用待审卡完全一样** ——
                    #    同一个动作、同一套视觉，只是入口不同（用户的要求）。
                    _rt_on = bool(_rt_iid) or (
                        _rt.get("kind") == self.QUOTE_SELECTION and bool(_rt_q))
                    if _rt_on:
                        self._render_quote_banner(_rt_q or _rt_iid)

                    # ── 文字（终端 log：you ❯ 前缀，左对齐）───────────────────
                    with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                        # ⭐ 用户名右边那个符号也要跟着变成引用图标 ——
                        # 与 composer 那个同一套符号，一眼能对上是同一件事。
                        _mark = self._REPLY_PROMPT if _rt_on else self._NORMAL_PROMPT
                        ui.label(f'{self._user_label()} {_mark}').style(
                            'color:var(--nano-accent); font-size:var(--nano-fs-lg); line-height:1.75rem; flex-shrink:0; min-width:64px; text-align:right;'
                            'font-family:var(--nano-mono);')
                        if query:
                            ui.label(query).classes('text-[14px] leading-7 whitespace-pre-wrap min-w-0').style(
                                'color:var(--nano-fg);')
                        else:
                            ui.label('（附件已发送）').style('font-size:var(--nano-fs-base); color:var(--nano-dim); font-style:italic;')

        # ⭐⭐⭐ [2026-08-09 实测] **引用状态在这里移交给本轮。**
        #
        # ⚠️⚠️⚠️ **2026-08-13 CMD63 更正：这里原来是「清除」，那是个真 bug。**
        #    `self._set_reply_target(None)` 跑在 orchestrator 建 prompt 之前 4 毫秒
        #    （：`22:33:39.127 引用态复位` / `22:33:39.131 [TOKEN-PLAN]`），
        #    于是 `_build_open_interactions_injection()` 读到的永远是空 ——
        #    「用户明确指了这一条」那句话从来没进过模型上下文。
        #    后果：用户引用审计卡说「部署这个吧」，模型改去 `create_new_skill`。
        #    整条链见 `Orchestrator.hand_off_reply_target()` 的 docstring。
        #
        # 📌 **一个「发出去就该消失」的状态，不该被清掉，该被【移交】。**
        #    清掉会让真正的消费者读空 —— 而这里真正的消费者不是 UI，是模型。
        #
        # 🔴 现象：点了 `replay` 引用一条待审、发出去之后，**composer 仍然停在
        #    引用态**（提示符还是 `↳`），于是**下一条消息也会被当成在回答那一条**。
        #    回查发现：全项目只有三处 `_set_reply_target` —— 卡片关闭时自动取消、
        #    ✕ 按钮、以及设置。**发送路径上一处都没有。**
        #
        # ⚠️⚠️ **为什么必须放在这里、而不是发送函数的开头**：
        #    上面那条引用横幅（`_rt_q` / `_rt_iid`）读的是**实时状态**。
        #    先清再渲染 → 横幅消失，而 已明确要求「发出去之后要能看出
        #    这条在回答哪个」（往上翻聊天记录时更需要它）。
        # ⭐ 而放在这里是安全的：`ui.label(...)` 在构造时就把文字**烤进了 DOM**，
        #    之后清掉变量不会影响已经画出来的那一条。
        # 📌 **一个「读了状态才能画出来」的东西和「必须被清掉」的状态，
        #    顺序只有一种：先画，再清。** 反过来两者只能满足一个。
        #
        # ⚠️ 顺带刷一次 composer 的提示符 —— 那个 `↳` 是另一处显示，
        #    不刷它就会出现「状态清了、符号还留着」的半截样子。
        #    📌 **一个状态有两处显示时，复位必须同时管到两处。**
        # ⚠️ 这里刻意**重新读实时状态**，而不是用上面那个 `_rt_iid` 局部变量 ——
        #    它定义在内层 `with` 里，今天是安全的（`with` 不产生作用域、也必然执行），
        #    但将来谁在外面套一个 `if`，这里就会变成 NameError，而且是在**发送路径上**。
        #    📌 **一个在别处条件里赋值的局部变量，不该被函数末尾的收尾逻辑依赖** ——
        #       收尾要么自己读一次权威，要么在函数开头就有确定的初值。
        # 🔴🔴 [2026-08-22] **这里原来是 `.get("iid")`，而
        #    选中引用（`kind=selection`）根本没有 `iid`** —— 它只有 `q`。
        #    于是整段复位对 那条入口**从来没执行过**：
        #    发出去之后 composer 上方的引用条**一直留着**，
        #    下一条消息还会被当成在回答同一段话。
        #
        # 📌 **「有没有引用」这个判断，全项目只该有一个说法。**
        #    此前有两个：`_refresh_reply_prompt` 用「iid 或 (selection 且有 q)」，
        #    发送路径用「iid」—— 两个说法一定会在某一类上分叉，
        #    而分叉的那一类恰好是后加的那个入口。
        # ⭐ 权威就是 `_reply_target` 本身：`_set_reply_target` 已经保证了
        #    「两种都不满足 → 存 None」。所以这里判它是不是 None，
        #    **而不是再复述一遍它的构造条件**。
        #    📌 复述一个已经被别处保证的条件，等于给它开了个分叉口。
        _rt_obj = self._reply_target or {}
        _rt_live = _rt_obj.get("iid") or _rt_obj.get("q") or ""
        if self._reply_target is not None:
            try:
                # ⚠️ 移交由 orchestrator 自己完成（两个字段都在它身上，UI 不碰）。
                #    它清掉 `_reply_target` → composer 提示符立刻能刷回普通态；
                #    同时把指向存进 `_reply_target_turn` → 模型这一轮读得到。
                _handed = self.agent.hand_off_reply_target()
                self._refresh_reply_prompt()
                logger.info(f"[UI] 引用已发出（{_handed or _rt_live}）→ 移交本轮，UI 侧复位")
            except Exception as _e_rt:
                logger.warning(f"[UI] 引用态移交失败: {_e_rt}")

        self.scroll_area.scroll_to(percent=1.0, duration=0.1)

        # 只保留最后一条 nano 消息的元信息行(头像/秒数/token)：新回复来了删掉上一条
        # ⚠️⚠️ **忙的时候不许删** —— 那个元信息行属于**正在转的**那一段回应期，
        #    删掉就是把 spinner 和 token 统计一起抹了（图 1 里
        #    `nano >` 后面空空的，就是这一行干的）。
        #    📌 「上一条」这个说法在「一段回应期可以包含多轮」之后失效了 ——
        #       此刻那个元信息行不是「上一条」的，是**当前这一段**的。
        if not _rt_inbox_busy:
            try:
                if getattr(self, '_last_meta_row', None):
                    self._last_meta_row.delete()
            except Exception:
                pass
        with self.chat_container:
            # 统一回复容器：标题行（图标+状态）+ 流式内容区（思考/答案同字体）
            loading_container = ui.column().classes('w-full py-1 mb-8')
            with loading_container:
                # 终端 log：nano ❯ 前缀（绿）+ 内容流在右侧
                with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                    ui.label('nano ❯').style(
                        'color:var(--nano-ok); font-size:var(--nano-fs-lg); line-height:1.75rem; flex-shrink:0; min-width:64px; text-align:right;'
                        'font-family:var(--nano-mono);')
                    # 内层：所有动态追加的 md/pill 都放这里
                    _inner_col = ui.column().classes('w-full gap-0 min-w-0')
                    with _inner_col:
                        _c_md = nano_md()
                # 元信息行（缩进对齐内容）：进行中=braille转圈，结束=光芒头像✦+统计
                with ui.row().classes('items-center gap-1.5 mt-1').style('padding-left:72px;') as _meta_row:
                    _spin_lbl = ui.label('⠋').style('font-size:var(--nano-fs-md); color:var(--nano-dim); font-family:var(--nano-mono);')
                    _svg_el = ui.html(NANO_AVATAR_SVG).style('width:16px; height:16px; flex-shrink:0; display:none;')
                    _s_lbl = ui.label('thinking · 0s').style(
                        'font-size:var(--nano-fs-base); color:var(--nano-dim); letter-spacing:0.01em; font-family:var(--nano-mono);'
                    )
                self._last_meta_row = _meta_row
            # 初始化本轮回复状态（供 navigate_pipeline 各事件共享）
            self._resp_state = ViewSession(
                # ⭐⭐ 记住容器自身 —— 续接的那一段要复用它，
                #    而不是新建一个 nano 块（那就是「出现两个」）。
                #    ⚠️ 归属从「本轮」变成「本**回应期**」：一段回应期可以包含多轮。
                #    📌 一个叫「本轮」的状态，在语义变成「一段可含多轮」之后
                #       必须重新划归属 —— 不改归属只改用法，
                #       就会出现「两个东西都以为自己是本轮」。
                container=loading_container,
                meta_row=_meta_row,       # 这段回应自己的元信息行；不能靠全局指针猜归属
                pending_epoch=_rt_inbox_busy,
                status_lbl=_s_lbl,
                spin_lbl=_spin_lbl,
                svg_el=_svg_el,
                start_time=time.time(),
                tok_base=sum(usage_tracker.session_tokens()),  # 本轮 token 基线：结束时取差值=单条用量
                running=True,
                content_md=_c_md,    # 当前活跃的 markdown 元素
                current_text="",     # 当前段落的累积文字
                loading_col=_inner_col,   # 动态追加目标（非 loading_container）
                tool_count=0,
                had_text_since_tool=True,  # True=下次工具调用开新pill批次
                batch_tool_count=0,        # 当前批次内工具数
                tool_pill_lbl=None,        # 当前批次的 pill 标签
                tool_pill_arrow=None,
                tool_details_col=None,     # 当前批次的明细列
                action_refs={},
                text_checkpoint="",)
            self._current_loading_label = _s_lbl  # 兼容 execution_confirm/os_action_confirm
            # ⭐⭐⭐ **这一段回应期还没轮到它跑时，谁来写那一行。**
            #
            # 🔴 实测（Subagent那次）：插话之后秒数**一直是 0**，等前一轮跑完
            #    直接跳到 84s。逐行核出来是三件事叠在一起：
            #      ① `_s_lbl` 的初值是**写死的字面量** `thinking · 0s`
            #      ② `start_time` 在气泡**创建**那一刻就起跑了
            #      ③ 计时协程只在 `navigate_pipeline` 里启动，而 successor 要等
            #         前一轮结束才轮得到 → 整段等待期**零个协程在写这一行**
            #    于是屏幕上唯一活着的指示器（predecessor 的元信息行）刚被交接删掉，
            #    而接班的那个是一句静止的假话。
            # 📌 **一个「稍后才会开始」的东西，它的时钟不该在「稍后」之前就起跑；
            #    要么晚点起跑，要么现在就得有人念它。** 这里选后者 ——
            #    用户等的是「我按下回车之后过了多久」，那个表从按下就该走。
            if _rt_inbox_busy:
                # ⚠️ 包住：这一行是**元信息行的显示**，它失败不该让这条消息发不出去。
                #    📌 展示层的故障，不许把能力本身搞掉（同 建记录那条）。
                try:
                    self._resp_state["_status_timer_task"] = asyncio.create_task(
                        self._pending_epoch_timer(self._resp_state))
                except Exception as _e_pt:
                    logger.warning(f"[Seam] 排队态计时器没起来（不影响发送）: {_e_pt}")
            thought_blocks_container = None  # 不再使用独立思考块容器

        self.scroll_area.scroll_to(percent=1.0, duration=0.1)

        # 图片 bytes 取出供发送；预览已在气泡内渲染，这里不再重复插图
        _img_bytes = self._pending_image_bytes
        _img_mime  = self._pending_image_mime
        if _img_bytes:
            self._clear_pending_image()

        # 构造提示引导模型主动检索临时文件，发送后清空 badge
        _temp_hint = None
        if self._temp_files:
            _names = "、".join(tf["filename"] for tf in self._temp_files)
            _first_name = _names.split("、")[0]
            _temp_hint = (
                f"\n\n[Current-Turn Attachment — Important]\n"
                f"The user uploaded attachment(s) in this current turn: {_names}.\n"
                f"References in the user's message such as this file, this document, this spreadsheet, attachment, "
                f"or just uploaded refer specifically to these new current-turn attachment(s).\n"
                f"Do not treat those references as referring to files discussed in previous turns. "
                f"Even if earlier context contains full content loaded from another file, current-turn references point to the new attachment(s).\n\n"
                f"Access rules:\n"
                f"- For whole-file understanding, summarization, analysis, rewriting, evaluation, comparison, or reasoning: call load_full_file. "
                f"Temporary filenames keep the [临时] prefix, for example [临时]{_first_name}.\n"
                f"- For specific facts or keywords inside the file: call query_local_knowledge.\n"
                f"- To pass the file into a local Skill: call get_file_path first and pass the returned real path to the Skill.\n"
                f"- If the filename is unclear: call list_knowledge_files."
            )
            self._temp_files = []
            self._refresh_temp_file_badge()

        # 空文字时用默认 query（模型需要非空 query 才能正常路由）
        # ⚠️ 语言策略**不在这里自己写** —— 收编到 `core.i18n.language_clause()`。
        #    原文写死了「Reply in Chinese unless…」，跟另外五处各说各的。
        from core.i18n import language_clause as _lc
        effective_query = query if query else (
            "Please process the uploaded content. " + _lc("your reply"))

        # ⭐⭐ 先落库 —— **无论忙不忙都落**。
        #
        # ⚠️ 为什么闲着也要落库：`item_id` 是这句话在系统里的**唯一身份**，
        #    而崩溃可能发生在任何时刻（包括「正在处理第一条」）。
        #    只在忙的时候落库，就等于说「不忙时丢了不算丢」。
        #    📌 **一条「不丢」的保证，不能有「除了这种情况」。**
        _rt_inbox_id = _rt_inbox_submit(effective_query, {
            "had_image": bool(_img_bytes),
            "temp_hint": bool(_temp_hint),
        })
        _rt_inbox_args = (effective_query, loading_container, _img_bytes,
                          _img_mime, _temp_hint, None)

        if _rt_inbox_busy:
            # ── 忙：用户插话切开 predecessor / successor 两段回应期 ─────────
            # ⭐⭐⭐ [2026-08-09] 旧形态把**整个** predecessor 移到新用户消息
            # 后面，再删除 successor。于是插话前已经真实发生的工具调用也被搬到了
            # 插话后，时间线变成了假话。
            #
            # 正确顺序是：旧用户 → predecessor 已发生事实 → 新用户 → successor。
            # predecessor 里被新输入推翻的自然语言稍后由 `turn_interrupted` 撤回；
            # 已执行工具不回滚。仍活着的等待只迁移「未来输出写到哪里」，执行体不死。
            # 📌 **用户插话是回应期边界，不是 DOM 搬家指令。**
            _rs_live = _seam_live or {}
            _live_box = _rs_live.get("container")
            if _live_box is not None and _rs_live.get("running"):
                self._handoff_response_epoch(_rs_live, self._resp_state)
                self._rt_inbox_parked[_rt_inbox_id or f"cont_{id(loading_container)}"] = \
                    ("cont", effective_query, _img_bytes, _img_mime, _temp_hint)
                logger.info(f"[Seam] 内核忙 → 切开回应期（{_rt_inbox_id}）："
                            f"predecessor 留在原位，后续写入 successor")
                return

            # ── 兜底：找不到活着的回应期 → 退回「排队」那套 ──────────────────
            # ⚠️ 这条路存在的意义是**别让无缝变成一条新的失败路径**：
            #    读不到活着的 `_resp_state` 时（比如锁被别的东西持有），
            #    宁可退化成上一版那种「排队 + 稍后起一轮」，也不要把消息卡死。
            #    📌 fail-safe 方向：**退化成旧行为，不退化成不处理。**
            # ⚠️ **附件字节留在内存里，不落库。** 两层分工要写清：
            #    · **库负责「不丢」** —— 这句话本身跨重启存活
            #    · **内存负责「接得上」** —— UI 句柄和图片字节只在本进程有意义
            #      （进程死了 UI 本来就没了，句柄落库也没用）
            #    ⚠️ 于是有个**已知缺口**：重启后队列里那条会保留文字、丢掉附件，
            #       且不会自动接上。**但它在库里，没丢。**
            self._rt_inbox_parked[_rt_inbox_id or f"mem_{id(loading_container)}"] = \
                _rt_inbox_args
            self._rt_inbox_mark_queued(loading_container)
            logger.info(f"[Inbox] 内核忙 → 这条进队列（{_rt_inbox_id}），"
                        f"当前轮结束后自动接上")
            return

        # ── 闲：立刻认领并开跑 ────────────────────────────────────────────
        # ⚠️ 认领失败（库挂了）**不许因此不干活** —— 照旧起 pipeline。
        #    📌 队列是为了「不丢」，不是为了「多一道能挡住用户的闸」。
        #       在这里 return 就等于亲手造出本项要消灭的那个东西。
        _rt_inbox_claim(_rt_inbox_id)
        self._rt_inbox_running_id = _rt_inbox_id
        # ⭐ [无缝 · 措辞] 这是回应期的**第一段** —— 计数归 1，且不注入任何说明。
        #    ⚠️ 首段一个字都不许加：那时候压根没有「上一段」。
        self._seam_part = 1
        try:
            self.agent._seam_continuation_part = 0
        except Exception:
            pass
        asyncio.create_task(self._safe_execute_pipeline(*_rt_inbox_args))


    # ── 记忆管理（user_note）────────────────────────────────────────────────

    def _update_memory_badge(self):
        """更新顶部记忆按钮的红点角标。"""
        if self._memory_badge_label is None:
            return
        count = len(self._pending_notes)
        if count > 0:
            self._memory_badge_label.set_text(str(count))
            self._memory_badge_label.style(
                'display:inline-block; background:var(--nano-danger-fill); color:#fff; '
                'font-size:var(--nano-fs-2xs); font-weight:700; border-radius:999px; '
                'min-width:16px; height:16px; line-height:16px; '
                'text-align:center; padding:0 3px;'
            )
        else:
            self._memory_badge_label.style('display:none;')

    def _render_pending_cards(self):
        """重新渲染待确认卡片区。"""
        if self._pending_cards_container is None:
            return
        self._pending_cards_container.clear()
        with self._pending_cards_container:
            if not self._pending_notes:
                ui.label('暂无新记忆').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-mute);'
                ).classes('w-full text-center py-3')
                return
            for note in self._pending_notes:
                self._render_one_pending_card(note)

    def _render_one_pending_card(self, note: dict):
        """渲染单张待确认卡片（调用方负责在正确容器内）。"""
        note_id = note["note_id"]
        ts_short = note.get("ts", "")[-14:-3] if note.get("ts") else ""

        with ui.column().classes('w-full rounded-xl px-4 py-3 gap-2').style(
            'background:rgba(var(--nano-amber-rgb), 0.07); border:1px solid rgba(var(--nano-amber-rgb), 0.18); '
            'transition:opacity 0.3s ease, transform 0.3s ease;'
        ) as card:
            ui.label(note["display_text"]).style(
                'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); line-height:1.5;'
            )
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(ts_short).style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
            with ui.row().classes('w-full justify-center'):
                ui.button('知道了', icon='check',
                    on_click=lambda n=note, c=card: self._confirm_note(n, c)
                ).props('flat dense').classes('text-[11px] text-emerald-400 hover:text-emerald-300')

    def _confirm_note(self, note: dict, card_el):
        """「知道了」= 保留这条记忆：把 pending 转 confirmed（这样才会出现在「编辑记忆」里
        供查看/编辑）。write_user_note 在 ReAct 里写为 pending，等用户在卡片上选保留或删除。"""
        try:
            from core.memory_store import get_memory_store
            get_memory_store().confirm(note["note_id"])
        except Exception:
            pass
        self._pending_notes = [n for n in self._pending_notes if n["note_id"] != note["note_id"]]
        try:
            self._pending_cards_container.remove(card_el)
        except Exception:
            pass
        if not self._pending_notes:
            with self._pending_cards_container:
                ui.label('暂无新记忆').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-mute);'
                ).classes('w-full text-center py-3')
        self._update_memory_badge()

    def _delete_note(self, note: dict, card_el):
        """删除单条 user_note。"""
        try:
            from core.memory_store import get_memory_store
            get_memory_store().delete_by_id(note["note_id"])
        except Exception:
            pass
        self._pending_notes = [n for n in self._pending_notes if n["note_id"] != note["note_id"]]
        try:
            self._pending_cards_container.remove(card_el)
        except Exception:
            pass
        if not self._pending_notes:
            with self._pending_cards_container:
                ui.label('暂无新记忆').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-mute);'
                ).classes('w-full text-center py-3')
        self._update_memory_badge()

    def _confirm_all_notes(self):
        """「全部知道了」= 保留所有待确认记忆：pending 全转 confirmed，再清空卡片列表。"""
        try:
            from core.memory_store import get_memory_store
            get_memory_store().confirm_all_pending()
        except Exception:
            pass
        self._pending_notes.clear()
        self._render_pending_cards()
        self._update_memory_badge()

    def _show_all_notes_dialog(self):
        """弹出全部已确认记忆的可滚动表格。删除单条时原地刷新列表，不关弹窗
        （否则想连删多条要反复开关，很烦）。"""
        from core.memory_store import get_memory_store
        dlg = ui.dialog().props('maximized=false')
        with dlg:
            with ui.card().style(
                'width:600px; max-height:80vh; padding:0; overflow:hidden; '
                'background:var(--nano-panel); border: 1px solid var(--nano-border); border-radius:16px;'
            ):
                with ui.row().classes('w-full items-center justify-between px-5 py-4').style(
                    'border-bottom: 1px solid var(--nano-border);'
                ):
                    _title_lbl = ui.label('').style(
                        'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);'
                    )
                    ui.button(icon='close', on_click=dlg.close) \
                        .props('flat round dense').style('color:var(--nano-fg) !important;')
                _list_col = ui.element('div').style(
                    'width:100%; max-height:calc(80vh - 64px); overflow-y:auto;'
                )

        def _render_list():
            try:
                rows = get_memory_store().get_all_confirmed_notes()
            except Exception:
                rows = []
            _title_lbl.set_text(f'全部记忆（共 {len(rows)} 条）')
            _list_col.clear()
            with _list_col:
                if not rows:
                    ui.label('暂无已记住内容').style(
                        'font-size:var(--nano-fs-base); color:var(--nano-fg-mute); display:block; text-align:center; padding:24px;'
                    )
                for r in rows:
                    ts_full = r.get("ts", "")[5:] if r.get("ts") else ""
                    # ⭐⭐ **抽屉显示 `summary_user`，不是 `detail`。**
                    #
                    # 🔴 问题：`detail` 是**给模型看的那句原文**。而旧的 `display_text`
                    #    （给用户看的那句）是工具参数、**从来没落库** —— 弹完确认卡就扔了。
                    #    ⇒ 抽屉里一直摆着给模型看的版本。
                    # 📌 「必须分开，这个坑我踩过」—— 而查下来**现在就踩着**。
                    #    一句话同时服务两个受众，最后两边都不合身。
                    # ⚠️ 老行没有这一列（迁移前写的），**回退到 `detail`** ——
                    #    📌 一条读不出摘要的旧记忆，该照旧显示出来，
                    #       而不是在列表里变成一行空白。
                    detail = (r.get("summary_user") or r.get("detail") or "")
                    _when = (r.get("applies_when") or "").strip()
                    with ui.row().style(
                        'width:100%; gap:12px; padding:12px 20px; '
                        'border-bottom: 1px solid var(--nano-border); align-items:flex-start;'
                    ):
                        ui.label(ts_full).style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-fg-mute); '
                            'flex-shrink:0; width:110px; padding-top:2px;'
                        )
                        with ui.column().style('flex:1; gap:2px; min-width:0;'):
                            ui.label(detail).style(
                                'font-size:var(--nano-fs-base); color:var(--nano-fg); line-height:1.6; white-space:pre-wrap;'
                            )
                            # ⚠️ 作用域用**弱一档**的样式：它是给用户一个「什么时候会用上」
                            #    的交代，不该跟正文抢注意力。
                            if _when:
                                ui.label(f'· {_when}').style(
                                    'font-size:var(--nano-fs-xs); color:var(--nano-dim); line-height:1.5;'
                                )
                        # 删除后只重绘列表，弹窗保持打开——可连续删多条
                        ui.button(icon='delete',
                            on_click=lambda rid=r["id"]: (
                                self._delete_confirmed_note(rid),
                                _render_list()
                            )
                        ).props('flat round dense').style('color:var(--nano-fg) !important;').classes('text-[12px] hover:text-rose-400')

        _render_list()
        dlg.open()

    def _delete_confirmed_note(self, note_id: int):
        """从编辑记忆弹窗删除一条已确认记忆。"""
        try:
            from core.memory_store import get_memory_store
            # ⭐ 抽屉里的删除也走**软删除** ——
            #    📌 用户点的删除和 Nano 调工具的删除是同一件事，
            #       一个动作有两个实现，它们只在「我两次想法相同」时一致。
            get_memory_store().soft_delete_by_id(note_id)
        except Exception:
            pass

    def _load_pending_notes_on_start(self):
        """启动时从 DB 加载待确认记忆，恢复红点状态。"""
        try:
            from core.memory_store import get_memory_store
            pending = get_memory_store().get_pending_notes()
            self._pending_notes = [
                {"note_id": r["id"], "display_text": r.get("detail", ""), "ts": r.get("ts", "")}
                for r in pending
            ]
        except Exception:
            self._pending_notes = []

    # ── 知识库管理 ────────────────────────────────────────────────────────

    # ⭐⭐ Auto 模式那句提示。
    #
    # 🔴 原文是「已开启 Auto：**屏幕操作**不再逐个确认」—— 用户报错：
    #    **它影响的是全部授权弹窗**（Skill 审计、OS 高危确认、探索澄清…），
    #    不只是屏幕操作。
    # 📌 **一句描述作用范围的提示，如果范围写窄了，它比不写更危险** ——
    #    用户会以为自己只放开了一小块，然后照着这个错的理解去开它。
    #    ⚠️ 这一格是**安全相关**的错，不是文案瑕疵。
    #
    # ⚠️ 而它原来**出现在两处**（两个入口各写一遍）——
    #    📌 **一句出现在两处的文案，必然有一天只改到一处。** 收成一个常量。
    # ⚠️ 写死是允许的：它同时命中「系统级通知」和「不出现在 Nano 气泡里」
    #    两条豁免 —— 这是 UI chrome，不是 Nano 的发言。
    _AUTO_ON_NOTICE = (
        "已开启 Auto：授权交给 Nano 自己判断，不再逐个询问"
        "（急停 Ctrl+` 仍可用）"
    )

    def _refresh_kb_file_list(self):
        """刷新知识库文件列表，同时渲染健康度状态（三色分级）。"""
        try:
            from nicegui import context as _ctx
            self._ui_client = _ctx.client
        except Exception:
            pass
        if not self.kb_file_list_container:
            return
        self.kb_file_list_container.clear()

        # 用 get_health_report 替代 get_stats，获取完整健康度信息
        health = rag_engine.get_health_report()
        summary = health.get("summary", {})
        files = health.get("files", [])

        with self.kb_file_list_container:
            # ── 正在入库的文件：持久化 spinner 行，不随 toast 消失 ──────────
            if self._kb_indexing_files:
                for fname in sorted(self._kb_indexing_files):
                    with ui.row().classes('w-full items-center justify-between py-1.5 px-2 rounded-xl').style(
                        'background:rgba(var(--nano-amber-rgb), 0.06); border:1px solid rgba(var(--nano-amber-rgb), 0.15); margin-bottom:2px;'
                    ):
                        with ui.row().classes('items-center gap-2 flex-1 min-w-0'):
                            ui.spinner(size='xs').style('color:var(--nano-fg-soft); flex-shrink:0;')
                            ui.label(fname).classes('text-[12px] text-slate-300 flex-1 min-w-0').style(
                                'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
                            )
                        ui.label('入库中...').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft); flex-shrink:0;'
                        )
                if files:
                    ui.separator().classes('opacity-[0.06] my-1')

            if not files and not self._kb_indexing_files:
                with ui.row().classes('items-center gap-2 py-2 px-1'):
                    ui.icon('inbox').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                    ui.label("知识库为空").classes('text-[12px]').style('color:var(--nano-fg) !important;')
            elif files:
                # 按 filename 去重（防止大小写差异导致重复显示）
                seen_filenames = set()
                deduped_files = []
                for r in files:
                    key = r.get("filename", "").lower()
                    if key not in seen_filenames:
                        seen_filenames.add(key)
                        deduped_files.append(r)

                # 按入库时间（mtime 近似）倒序，最新的排前面
                def _mtime_key(r):
                    p = r.get("path", "")
                    try:
                        return os.path.getmtime(p) if p else 0
                    except Exception:
                        return 0
                deduped_files.sort(key=_mtime_key, reverse=True)

                INLINE_LIMIT = 5
                for r in deduped_files[:INLINE_LIMIT]:
                    self._render_kb_file_card(r)

                if len(deduped_files) > INLINE_LIMIT:
                    ui.button(
                        f'查看全部文件（{len(deduped_files)}）',
                        on_click=lambda fs=deduped_files: self._show_all_kb_files_dialog(fs)
                    ).props('flat dense').style(
                        'width:100%; font-size:var(--nano-fs-sm); color:var(--nano-fg-mute) !important; margin-top:4px;'
                    )

        # 更新统计标签，加整体健康度颜色
        if self.kb_stats_lbl:
            status_color = summary.get("status_color", "green")
            color_map = {"green": "var(--nano-ok)", "yellow": "var(--nano-warn)", "red": "var(--nano-danger)"}
            risk_color = color_map.get(status_color, "var(--nano-ok)")
            total_files = summary.get("total", 0)
            total_chunks = summary.get("total_chunks", 0)
            ok_count = summary.get("ok", 0)
            warning_count = summary.get("warning", 0)
            error_count = summary.get("error", 0)
            # 文件数统计始终用绿色（侧重"成功导入"）
            self.kb_stats_lbl.set_text(f"{total_files} 份文件 · {total_chunks} 个知识块")
            # 字号当时是因为这行字跟"知识库管理"大标题并列才放大的，现在
            # 挪到"已索引文件"这个小标题旁边了，改回去；绿色也调暗一点
            # （之前 var(--nano-ok) 太亮，在白色卡片上有点扎眼）。
            self.kb_stats_lbl.style('font-size:var(--nano-fs-sm); color:var(--nano-ok); ')
            # 风险提示独立一行，有warning/error才显示
            if hasattr(self, '_kb_health_lbl') and self._kb_health_lbl:
                health_lines = []
                if error_count > 0:
                    health_lines.append(f'<span style="color:var(--nano-danger)">{error_count} 个文件入库失败</span>')
                if warning_count > 0:
                    health_lines.append(f'<span style="color:var(--nano-warn)">{warning_count} 个文件存在风险提示</span>')
                if health_lines:
                    self._kb_health_lbl.set_content(" · ".join(health_lines))
                    self._kb_health_lbl.visible = True
                else:
                    self._kb_health_lbl.set_content("")
                    self._kb_health_lbl.visible = False

    def _render_kb_file_card(self, r: dict):
        """渲染单个知识库文件的卡片——内联列表（最近5个）和"查看全部文件"
        弹窗共用这一份渲染逻辑，不重复写两遍。"""
        fname = r.get("filename", "")
        fpath = r.get("path", "")
        risk_color = r.get("risk_color", "var(--nano-ok)")
        risk_level = r.get("risk_level", "ok")
        raw_tips = r.get("risk_tips", [])
        icon_url = self._file_type_icon_url(fname)
        try:
            time_str = self._relative_time_str(os.path.getmtime(fpath)) if fpath else ""
        except Exception:
            time_str = ""

        # 去重 + 按重要性排序
        # 黄色优先级关键词顺序
        YELLOW_PRIORITY = ["OCR", "扫描识别", "公式", "漏内容", "图片", "图表", "页提取为空"]
        RED_PRIORITY = ["未能提取出文字", "空白扫描件", "超过", "格式", "未安装", "入库失败", "切块后为空"]

        def tip_priority(tip):
            keywords = RED_PRIORITY if risk_level == "error" else YELLOW_PRIORITY
            for i, kw in enumerate(keywords):
                if kw in tip:
                    return i
            return 999

        seen_keys = set()
        risk_tips = []
        for tip in sorted(raw_tips, key=tip_priority):
            key = tip[:30]
            if key not in seen_keys:
                seen_keys.add(key)
                risk_tips.append(tip)

        tip_color = "var(--nano-warn)" if risk_level == "warning" else "var(--nano-danger)"
        has_tips = bool(risk_tips) and risk_level != "ok"

        # 每个文件独立一张卡片（参考日间设计图的分块感），不再是
        # 一长串贴在一起的列表行。
        with ui.column().classes('w-full gap-0 rounded-xl').style(
            'border: 1px solid var(--nano-border); margin-bottom:6px; overflow:hidden;'
        ):
            # tips_container 用一个可变单元格(列表)做"前向引用"：
            # ⓘ按钮在 header row 里，但 tips_container 这个元素本身
            # 必须创建在 header row 之外（见下方注释），所以这里先占
            # 一个位置，header row 渲染完后再把真正的元素塞进去。
            _tips_ref = [None]
            with ui.row().classes('w-full items-center justify-between py-2 px-2 hover:bg-black/[0.02] transition-all group no-wrap'):
                # ⚠️ 左侧这一组也要 `min-width:0; overflow:hidden` ——
                #    它是 `justify-between` 的第一个子项，不压住它的话
                #    右边那个 `flex-shrink:0` 的按钮组会被挤出容器。
                #    📌 **一条「不许溢出」的约束，链上每一环都得写** ——
                #       只写最里面那一层，外层照样把它撑开。
                with ui.row().classes('items-center gap-2 flex-1 min-w-0 no-wrap').style(
                        'min-width:0; overflow:hidden;'):
                    # 文件类型缩略图（外部评审 生成 + 用户自己抠的透明 PNG，
                    # 按扩展名映射，/icons 静态挂载）。
                    ui.image(icon_url).style(
                        'width:30px; height:30px; flex-shrink:0; object-fit:contain;'
                    )
                    # ⭐⭐⭐ **`width:100%` + `overflow:hidden` 是这一处
                    #    省略号能不能触发的关键，不是下面那条 `text-overflow`。**
                    #
                    # 🔴 `ui.column()` 带 Quasar 的 `items-start`
                    #    （`align-items:flex-start`）。在 **column 方向**的 flex 容器里，
                    #    `align-items` 管的是**横轴** → 子元素按**内容宽度**撑开，
                    #    而不是被压到容器宽度。
                    #    于是装文件名的那个内层 `row` **有多长撑多长**，
                    #    它内部的 `min-width:0` + `text-overflow:ellipsis`
                    #    **永远不会触发** → 长文件名直接盖过右边那个
                    #    `flex-shrink:0` 的 `⋮` 按钮（实测截图）。
                    #
                    # 📌 **在 `flex-direction:column` 的容器里，`align-items:flex-start`
                    #    会让子元素按内容宽度撑开** —— 这一格是「省略号配置对了却不生效」
                    #    最常见的成因，而它长得完全不像一个宽度问题。
                    #
                    # ⚠️ 这里写**显式 CSS**而不是加工具类：
                    #    📌 与其判断那个类名生效没有，不如让这一处**不依赖它** ——
                    #    **一个「可能生效也可能不生效」的依赖，本身就是缺陷。**
                    # ⚠️ 而一开始把根因判成了「Tailwind 没生效」，依据是下面那句注释 ——
                    #    📌 **一处注释里的因果解释，和它旁边那行代码不是同一个证据等级**：
                    #       注释记录的是当时的推测，而推测会被抄进下一个人的判断。
                    #       （核实结果：NiceGUI 2.24.2 确实自带并加载 Tailwind。）
                    with ui.column().classes('gap-0 flex-1 min-w-0').style(
                            'width:100%; min-width:0; overflow:hidden;'):
                        with ui.row().classes('items-center gap-2 no-wrap min-w-0').style(
                                'width:100%; min-width:0; overflow:hidden;'):
                            # 三色状态圆点——之前长文件名会把这个点挤到
                            # 上一行去（跟 Skill 列表那次是同一个
                            # flex-wrap 默认行为的坑），加 no-wrap
                            # 强制保持同一行。
                            ui.element('div').style(
                                f'width:7px; height:7px; border-radius:50%; background:{risk_color}; flex-shrink:0;'
                            )
                            # 之前用 Tailwind 的 truncate 类一直没生效（这个
                            # 环境里这个工具类大概率没被正确加载/生成），
                            # 换成直接写在 style 里的显式 CSS，不依赖它。
                            # flex 子元素默认 min-width:auto，必须显式
                            # min-w-0 才会在空间不够时触发省略号，而不是把
                            # 容器撑宽/换行。
                            ui.label(fname).classes('text-[12px] text-slate-400 flex-1 min-w-0').style(
                                'flex:1 1 0; min-width:0; '
                                'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
                            ).tooltip(fname)
                        # "{chunks} 知识块"用户看不懂是什么意思，
                        # 换成入库时间（mtime 近似值）
                        if time_str:
                            ui.label(time_str).style(
                                'font-size:var(--nano-fs-xs); color:var(--nano-fg-mute); '
                            )
                with ui.row().classes('items-center gap-1 flex-shrink-0'):
                    if has_tips:
                        # 展开/收起按钮
                        def make_toggle(tc_ref):
                            def toggle():
                                tc = tc_ref[0]
                                if tc is not None:
                                    tc.set_visibility(not tc.visible)
                            return toggle
                        ui.button(icon='info_outline', on_click=make_toggle(_tips_ref)) \
                            .props('flat round dense') \
                            .style(f'color:{tip_color}; font-size:var(--nano-fs-lg);')
                    # 删除单独占一个按钮太重——换成"⋮"更多操作菜单，
                    # 跟 Skill 那个操作菜单同一套视觉语言。
                    with ui.element('div'):
                        with ui.button(icon='more_vert').props('flat round dense size=sm') \
                                .style('color:var(--nano-fg) !important;'):
                            with ui.menu().props('anchor="bottom right" self="top right"').classes('q-pa-none') as file_menu:
                                with ui.column().style(
                                    'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                                    'box-shadow:0 4px 16px rgba(var(--nano-shade-rgb), 0.08); '
                                    'border-radius:10px; min-width:150px; padding:5px; gap:1px;'
                                ):
                                    def _file_menu_item(icon, label, on_click, accent='var(--nano-fg)'):
                                        def _go():
                                            file_menu.close()
                                            on_click()
                                        with ui.row().classes('items-center gap-1.5 w-full cursor-pointer hover:bg-black/5 transition-colors') \
                                                .style('padding:5.5px 8px; border-radius:7px;') \
                                                .on('click', _go):
                                            ui.icon(icon).style(f'font-size:var(--nano-fs-base); color:{accent}; flex-shrink:0;')
                                            ui.label(label).style('font-size:var(--nano-fs-xs); color:var(--nano-fg);')

                                    # "打开所在文件夹"撤掉了——所有知识库文件都在
                                    # 同一个目录下，点哪个文件的"打开文件夹"结果都
                                    # 一样，不是真的per-file操作，没意义。
                                    _file_menu_item('visibility', '查看内容',
                                                     lambda f=fname, p=fpath: self._show_kb_file_content_dialog(f, p))
                                    _file_menu_item('delete', '删除',
                                                     lambda f=fname: self._delete_kb_file(f), accent='var(--nano-danger)')
            # 风险提示展开区（默认隐藏）
            # 关键修复：这个元素之前是 header row 里
            # "items-center gap-1 flex-shrink-0" 这个横向 flex 行
            # 的一个子项——一个没有宽度限制的 flex item，展开后
            # word-break:break-all 没有"边界"可断，会按单行最大
            # 内容宽度撑开，把 ⓘ/删除按钮一起推出 320px 抽屉，
            # 还连带把整个页面撑出横向滚动条。
            # 改成 header row 的"下一行"，作为 w-full 列的独立
            # 子元素，宽度跟随抽屉宽度，长文本能正常换行。
            if has_tips:
                tips_container = ui.column().classes('hidden w-full')
                _tips_ref[0] = tips_container
                with tips_container:
                    with ui.column().classes('w-full px-3 pb-2 gap-1').style(
                        f'border-left: 2px solid {tip_color}; margin-left:14px; margin-bottom:4px; '
                        f'max-width:100%; overflow-wrap:break-word;'
                    ):
                        for tip in risk_tips:
                            ui.label(tip).style(
                                f'font-size:var(--nano-fs-xs); color:{tip_color}; line-height:1.5; '
                                f'white-space:normal; overflow-wrap:break-word; word-break:break-all;'
                            )

    def _show_all_kb_files_dialog(self, files: list):
        """"查看全部文件"——内联列表只显示最近 5 个，全部文件在这个可
        滚动弹窗里展示，复用 _render_kb_file_card 同一份卡片渲染逻辑。"""
        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:420px; max-height:80vh; padding:0; overflow:hidden; '
                     'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                     'box-shadow:0 8px 28px rgba(var(--nano-shade-rgb), 0.12); border-radius:16px;'
                 ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                    'background:var(--nano-panel);'
                ):
                    ui.label(f'全部文件（共 {len(files)} 个）').style(
                        'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);'
                    )
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;')

                # 跟"全部记忆"弹窗同一套写法（普通 div + max-height +
                # overflow-y:auto），不用 ui.scroll_area() 配 flex:1——
                # 那个组合在 Quasar 弹窗里父级没有明确高度时会塌缩成 0，
                # 内容其实渲染了，但看起来是空的。
                with ui.element('div').style(
                    'width:100%; max-height:calc(80vh - 64px); overflow-y:auto; padding:12px 16px;'
                ):
                    for r in files:
                        self._render_kb_file_card(r)
            dialog.open()

    async def _handle_kb_upload(self, e: events.UploadEventArguments):
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            # 这个会话装的 NiceGUI 2.24.2 里，UploadEventArguments 这个
            # dataclass 实际只有 content(BinaryIO) / name / type 三个字段，
            # 压根没有 .file——之前这里访问 e.file 必报
            # AttributeError，是个一直存在、从没被实测点过的 bug
            # （没人通过这个上传框真的传过新文件测试这条路径）。
            filename = e.name
            raw_bytes = e.content.read()
            if asyncio.iscoroutine(raw_bytes):
                raw_bytes = await raw_bytes
        except Exception as read_err:
            ui.notify(f"❌ 读取上传内容失败: {read_err}", type='negative')
            return

        if not raw_bytes:
            ui.notify(f"❌ {filename} 内容为空，已忽略", type='negative')
            return

        # 不支持的文件格式提前拦截，给出明确提示
        import pathlib as _pl
        SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv",
                     ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
        suffix = _pl.Path(filename).suffix.lower()
        if suffix not in SUPPORTED:
            ui.notify(
                f"❌ 不支持的文件格式：{suffix or '(无后缀)'}。支持：txt / md / pdf / docx / xlsx / xls / csv / jpg / png / webp",
                type='negative'
            )
            return

        dest = KNOWLEDGE_DIR / filename

        # 同名文件拦截：已存在则拒绝，提示用户先删除或改名
        if dest.exists():
            ui.notify(
                f"⚠️ 知识库中已存在「{filename}」。请先在列表中删除旧文件，或将新文件改名后重新上传。",
                type='warning'
            )
            return

        def _write():
            with open(dest, "wb") as f:
                f.write(raw_bytes)

        await asyncio.to_thread(_write)

        # 立即加入"入库中"集合并刷新列表：KB 面板里出现持久 spinner 行，
        # 用户无需盯着会消失的 toast，可以做其他事，看到 spinner 消失即完成。
        self._kb_indexing_files.add(filename)
        self._refresh_kb_file_list()
        ui.notify(f"已接收「{filename}」（{len(raw_bytes) // 1024} KB），入库中，可在知识库列表查看进度", type='info', timeout=4000)

        _cfg = {"enhanced_mode": self._enhanced_mode, "max_ocr_pages": self._ocr_max_pages}
        stats = await asyncio.to_thread(rag_engine.index_single_file, str(dest), _cfg)

        # 入库完成，从"入库中"集合移除
        self._kb_indexing_files.discard(filename)

        if stats["indexed"] > 0:
            ui.notify(f"✔ 「{filename}」入库完成", type='positive', timeout=6000)
        elif stats["skipped"] > 0:
            ui.notify(f"「{filename}」内容未变化，跳过重复索引", type='warning', timeout=6000)
        else:
            err = stats["errors"][0]["error"] if stats["errors"] else "未知错误"
            ui.notify(f"❌ 「{filename}」入库失败: {err}", type='negative', timeout=0)
            logger.error(f"[KB Upload] 索引失败 {filename}: {err}")

        # 刷新 KB 列表前先更新 _ui_client，确保 DOM 变更推送给当前 websocket 连接。
        # 大文件入库耗时较长，await 期间 context 可能已切换，显式刷新确保正确。
        try:
            from nicegui import context as _ctx
            self._ui_client = _ctx.client
        except Exception:
            pass
        self._refresh_kb_file_list()

    @staticmethod
    def _relative_time_str(mtime: float) -> str:
        """文件修改时间 → 相对时间文案（刚刚/N分钟前/N小时前/昨天/MM-DD）。
        不碰 core/rag.py（健康度报告里没存入库时间戳，且 RAG 代码这次
        会话定了不能动）——直接读文件系统自身的 mtime 做近似值，知识库
        文件上传后一般不会再被编辑，这个近似足够准。"""
        import time as _time
        import datetime as _datetime
        delta = _time.time() - mtime
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{int(delta // 60)} 分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)} 小时前"
        if delta < 172800:
            return "昨天"
        return _datetime.datetime.fromtimestamp(mtime).strftime("%m-%d")

    @staticmethod
    def _file_type_icon_url(filename: str) -> str:
        """按扩展名返回缩略图 URL（/icons 静态挂载，assets/file_icons/
        下的真图，外部评审 生成 + 用户自己抠的透明 PNG）。"""
        ext = pathlib.Path(filename).suffix.lower().lstrip(".")
        icon_map = {
            "docx": "docx", "doc": "docx",
            "xlsx": "xlsx", "xls": "xlsx", "csv": "xlsx",
            "pptx": "pptx", "ppt": "pptx",
            "pdf": "pdf",
            "txt": "txt", "md": "txt",
            "jpg": "image", "jpeg": "image", "png": "image", "webp": "image",
        }
        return f"/icons/{icon_map.get(ext, 'generic')}.png"

    def _show_kb_file_content_dialog(self, filename: str, fpath: str):
        """在 Nano 内部弹窗里看文件内容——不是打开原始文件，是把 RAG
        入库时已经拆好的文本 chunk 重新拼起来展示（图片文件除外，直接
        显示原图）。这部分只读 Chroma 已有数据，不改 core/rag.py 任何
        一行代码。"""
        ext = pathlib.Path(filename).suffix.lower().lstrip(".")
        IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}

        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:560px; max-height:80vh; padding:0; overflow:hidden; '
                     'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                     'box-shadow:0 8px 28px rgba(var(--nano-shade-rgb), 0.12); border-radius:16px;'
                 ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                    'background:var(--nano-panel);'
                ):
                    ui.label(filename).style(
                        'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg); '
                        'overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:440px;'
                    )
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;')

                # 同款修复：不用 ui.scroll_area()+flex:1，改用跟"全部记忆"
                # 弹窗一样、已验证能正常显示内容的写法。
                with ui.element('div').style(
                    'width:100%; max-height:calc(80vh - 64px); overflow-y:auto; padding:20px 24px;'
                ):
                    if ext in IMAGE_EXTS:
                        if fpath and pathlib.Path(fpath).exists():
                            ui.image(fpath).classes('w-full').style('border-radius:8px;')
                        else:
                            ui.label('文件不存在或已被移动').style('color:var(--nano-fg-mute); font-size:var(--nano-fs-base);')
                    else:
                        try:
                            collection = rag_engine._get_collection()
                            result = collection.get(
                                where={"filename": filename},
                                include=["documents", "metadatas"],
                            )
                            rows = list(zip(result.get("metadatas", []), result.get("documents", [])))
                            # schema chunk 是给检索用的结构摘要，不是正文，
                            # 看内容时不需要，过滤掉。
                            rows = [(m, d) for m, d in rows if (m or {}).get("chunk_type") != "schema"]
                            rows.sort(key=lambda x: (x[0] or {}).get("chunk_index", 0))
                            full_text = "\n\n".join(d for _, d in rows if d)
                        except Exception as e:
                            full_text = ""
                            ui.notify(f"读取内容失败：{e}", type='negative')
                        if full_text:
                            ui.label(
                                '以下是 Nano 入库时提取到的文本内容（不是原始文件排版）'
                            ).style('font-size:var(--nano-fs-xs); color:var(--nano-fg-mute); margin-bottom:10px; display:block;')
                            ui.markdown(full_text).style('font-size:var(--nano-fs-md); color:var(--nano-fg); line-height:1.7;')
                        else:
                            ui.label('没有找到可显示的文本内容（可能入库失败或暂不支持预览该格式）').style(
                                'color:var(--nano-fg-mute); font-size:var(--nano-fs-base);'
                            )
            dialog.open()

    def _delete_kb_file(self, filename: str):
        rag_engine.delete_file(filename)
        disk_path = KNOWLEDGE_DIR / filename
        if disk_path.exists():
            disk_path.unlink()
        ui.notify(f"已删除: {filename}", type='positive')
        self._refresh_kb_file_list()
        # 和 _delete_skill 同理：侧边栏直接删除知识库文件，绕过了聊天流程，
        # 之前不写 memory，Nano 答不出"是你删的"这层解释。措辞同样要显式标注
        # 来源是用户的UI操作，不能让模型读成自己说/做的（见 _delete_skill 注释）。
        self.agent.memory.add_system_note(
            "assistant",
            f"[System record: the user deleted knowledge-base file \"{filename}\" from the UI sidebar; this was not executed in the current chat.]"
        )

    def _get_default_model_from_json(self) -> str:
        """读取 throttle_config.json 里的 _default_model 字段。"""
        import json as _json
        import pathlib as _pl
        cfg_path = _pl.Path(__file__).parent / "data" / "throttle_config.json"
        try:
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                _m = data.get("_default_model", "")
                try:
                    from core.provider import migrate_model_id as _mig
                    return _mig(_m) if _m else _m
                except Exception:
                    return _m
        except Exception:
            pass
        return ""

    def _get_star_icon(self) -> str:
        """当前模型是否是默认模型，返回对应图标。"""
        current = self.provider.target_model
        default = self._get_default_model_from_json()
        return "star" if current == default else "star_border"

    # ── Skill 结构化结果 → 原始表格渲染 ──────────────────────────────────

    def _render_tool_data_tables(self, tool_data):
        """把 SkillResult.data 里的 list[dict] 渲染成表格。

        目的：Skill 真实算出来的数据，原样展示给用户，不经过 Nano 的
        自然语言转述（逐行转录大表格容易出现转写错误，比如把"银牌"
        写成"普通"——这个表格是数据真相来源）。

        只渲染 data 里值为 list[dict] 且非空的字段；标量/路径/状态等
        字段不处理（那些适合 Nano 用文字说明）。
        """
        if not tool_data or not isinstance(tool_data, dict):
            return

        for key, value in tool_data.items():
            if not isinstance(value, list) or not value:
                continue
            if not all(isinstance(item, dict) for item in value):
                continue

            # 列：取所有行 key 的并集，保持首行出现顺序
            columns_order: list[str] = []
            for item in value:
                for k in item.keys():
                    if k not in columns_order:
                        columns_order.append(k)

            def _safe(v):
                if v is None:
                    return ""
                if isinstance(v, (str, int, float, bool)):
                    return v
                return str(v)

            rows = [{k: _safe(item.get(k)) for k in columns_order} for item in value]
            columns = [
                {"name": k, "label": k, "field": k, "align": "left", "sortable": True}
                for k in columns_order
            ]

            with ui.column().classes('w-full gap-1'):
                ui.label(f'{key}（共 {len(rows)} 条）').style(
                    'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                )
                ui.table(columns=columns, rows=rows, row_key=columns_order[0] if columns_order else None) \
                    .classes('w-full text-[12px]') \
                    .props('flat dense wrap-cells').style(
                        'background:var(--nano-panel); border: 1px solid var(--nano-border); border-radius:8px; color:var(--nano-fg) !important;'
                    )

    def _toggle_default_model(self):
        """点星星：是默认就取消，不是就设为默认。"""
        current = self.provider.target_model
        default = self._get_default_model_from_json()
        mname = GEMINI_MODEL_MAP.get(current, {}).get("name", current)
        if current == default:
            # 取消默认，删掉 json 里的 _default_model
            self._save_app_config(default_model="__clear__")
            ui.notify(f"已取消「{mname}」的默认设置", type='warning', icon='star_border')
        else:
            self._save_app_config(default_model=current)
            ui.notify(f"已将「{mname}」设为启动默认", type='positive', icon='star')
        # 更新星星图标 + 颜色/发光
        if self._star_btn:
            _icon = self._get_star_icon()
            _is_active = _icon == "star"
            self._star_btn.props(f'icon={_icon}')
            self._star_btn.style(
                ('color:var(--nano-warn) !important; filter:drop-shadow(0 0 4px rgba(var(--nano-warn-rgb),0.45));'
                 if _is_active else 'color:var(--nano-fg) !important; filter:none;') +
                ' transition:color 0.2s, filter 0.2s;'
            )

    def _save_app_config(self, default_model: str = None):
        """把应用配置写到 data/throttle_config.json。"""
        import json as _json
        import pathlib as _pl
        cfg_path = _pl.Path(__file__).parent / "data" / "throttle_config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data: dict = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
            # 清理已废弃的节流和策略引擎字段
            for obsolete in ("_model_policy", "_policy_engine_enabled"):
                data.pop(obsolete, None)
            # 移除非元数据（旧节流条目）
            for k in [k for k in data if not k.startswith("_")]:
                del data[k]
            if default_model == "__clear__":
                data.pop("_default_model", None)
            elif default_model is not None:
                data["_default_model"] = default_model
            data["_last_relay_mode"] = getattr(self.provider, "is_relay", False)
            data["_enhanced_mode"] = self._enhanced_mode
            data["_ocr_max_pages"] = self._ocr_max_pages
            data["_theme_mode"] = getattr(self, "theme_mode", "terminal")
            # ⚠️ 收藏模型必须带上"属于哪家" —— 否则换厂商后它会指向别家的型号。
            data["_default_model_vendor"] = (
                getattr(self.provider, "vendor", "")
                or os.environ.get("NANO_API_VENDOR") or "anthropic").lower()
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Config] 保存配置失败: {e}")

    def _load_app_config(self):
        """从 data/throttle_config.json 加载默认模型、入库设置。"""
        import json as _json
        import pathlib as _pl
        cfg_path = _pl.Path(__file__).parent / "data" / "throttle_config.json"

        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    saved = _json.load(f)

                # 加载收藏模型
                default_model = saved.get("_default_model")
                # ⚠️ 先过一遍下线映射再判断在不在表里 —— 否则旧 id 会静默落进
                #    else 分支「无收藏模型」，用户只看到"我收藏的 Opus 变回 Haiku 了"，
                #    日志里却看不出是型号下线。
                if default_model:
                    try:
                        from core.provider import migrate_model_id as _mig
                        _new = _mig(default_model)
                        if _new != default_model:
                            default_model = _new
                            self._save_app_config(default_model=_new)
                    except Exception:
                        pass
                # 🔴 收藏模型**按厂商作废**（2026-08-31，同计费清账那条）：
                #    `_default_model` 曾是一个跨厂商的全局值 —— 换厂商之后它指向
                #    别家的模型，把 provider 刚设好的主模型覆盖掉，于是监控卡、
                #    进阶配置、上下文窗口全线显示上一个厂商的型号。
                #    ⚠️ 不做"切回去还在"——那要多存一张表，而收藏本来就是随手行为。
                _saved_vendor = str(saved.get("_default_model_vendor") or "")
                _now_vendor = (getattr(self.provider, "vendor", "")
                               or os.environ.get("NANO_API_VENDOR") or "anthropic").lower()
                if default_model and _saved_vendor and _saved_vendor != _now_vendor:
                    logger.info(f"[Model] 收藏模型属于 {_saved_vendor}，当前是 "
                                f"{_now_vendor} —— 作废，沿用 {self.provider.target_model}")
                    default_model = ""
                # ⚠️ 校验用【当前厂商的清单】，不是 GEMINI_MODEL_MAP（那是 Claude 专属表，
                #    深度求索的 id 一个都不在里面 ⇒ 永远校验不过）。
                _valid = self._vendor_model_options()
                if default_model and default_model in _valid:
                    self.provider.target_model = default_model
                else:
                    logger.info(f"[Model] 无收藏模型，沿用默认: {self.provider.target_model}")

                # 入库设置
                if "_enhanced_mode" in saved:
                    self._enhanced_mode = bool(saved["_enhanced_mode"])
                if "_ocr_max_pages" in saved:
                    self._ocr_max_pages = int(saved["_ocr_max_pages"])

                # 主题：跟用户上次的选择。首启（配置文件不存在）才用默认终端风。
                # ⚠️ 白名单校验 —— 配置是用户可编辑的文本，写进一个不认识的主题名
                #    会让 body 挂上没有对应变量块的 class ⇒ 满屏裸样式。
                _tm = saved.get("_theme_mode")
                if _tm in ("terminal", "aurora"):
                    self.theme_mode = _tm
                elif _tm:
                    logger.warning(f"[Theme] 配置里的主题名不认识，回退默认: {_tm!r}")
            except Exception as e:
                logger.warning(f"[Config] 读取配置失败，使用默认值: {e}")
        else:
            # 首次启动：设置默认模型
            self.provider.target_model = (os.getenv("NANO_MODEL") or CLAUDE_MODELS[0]["id"]).strip()
            logger.info(f"[Model] 首次启动，初始模型: {self.provider.target_model}")

    def _persist_relay_mode(self):
        """启动后持久化配置（保留方法名兼容调用点）。"""
        self._save_app_config()

    def _on_model_change(self, e):
        """模型切换：实时生效。"""
        new_id = e.value
        self.provider.target_model = new_id
        model_cfg = GEMINI_MODEL_MAP.get(new_id, {})
        name = model_cfg.get("name", new_id)
        ui.notify(f"已切换至 {name}", type='positive', icon='swap_horiz')
        if self.model_lbl:
            self.model_lbl.set_text(name.upper())
        if self._star_btn:
            _icon = self._get_star_icon()
            _is_active = _icon == "star"
            self._star_btn.props(f'icon={_icon}')
            self._star_btn.style(
                ('color:var(--nano-warn) !important; filter:drop-shadow(0 0 4px rgba(var(--nano-warn-rgb),0.45));'
                 if _is_active else 'color:var(--nano-fg) !important; filter:none;') +
                ' transition:color 0.2s, filter 0.2s;'
            )
        logger.info(f"[Model] 切换至: {new_id}")

    # OS 权限开关元数据：(key, 图标, 标签, 说明, 风险档位)
    # 风险档位仅用于 UI 视觉区分，沿用 OS 层执行确认弹窗已有的语言
    # （risk=2 黄色 / risk=3 红色），不是新发明一套配色。
    _OS_PERMISSION_META = [
        ("allow_workspace_write", "folder", "工作区写入", "在指定工作目录内创建/修改/删除文件", "amber"),
        ("allow_window_control", "web_asset", "窗口控制", "切换、移动、缩放、关闭窗口", "amber"),
        ("allow_mouse_keyboard", "mouse", "鼠标键盘模拟", "代为点击屏幕元素、输入文字", "amber"),
        ("allow_system_settings", "display_settings", "系统设置", "调整音量、亮度等系统级设置", "amber"),
        ("allow_registry_write", "dns", "注册表写入", "修改 Windows 注册表键值", "red"),
        ("allow_dangerous", "warning", "高危操作总闸", "运行命令行、删除文件等不可逆操作", "red"),
    ]

    def _build_settings_permissions(self):
        """OS 权限的内容区。**独立弹窗和设置面板共用这一份。**

        ⚠️ 改成【即时生效】之后，内容对壳**一个依赖都没有了** —— 原来那个
           `on_done`（应用成功后关掉外面那层）随「应用」按钮一起消失。
           📌 一个不需要按钮的内容区，天然也不需要知道自己被装在什么壳里。
        """
        cfg_path = os_dsl.os_state_path()
        current = os_dsl.load_permissions()
        # ⚠️ 不再自带 padding：设置面板那一层已经给了横向留白。
        #    留一点上边距就够 —— 这一格原来的 `padding:18px 24px` 是它
        #    还是独立弹窗时的设定，进面板后跟外层叠成了两倍。
        with ui.column().style('width:100%; padding:4px 0 0; gap:8px;'):
            switch_map: dict = {}

            # ⚠️ 拨回去会再次触发 on_value_change ⇒ 死循环。这个闸只为闸掉那一次。
            #    📌 任何「代码改控件值」的地方都要问：它会不会把自己的回调再触发一遍。
            _suppress = {"v": False}

            def _persist() -> bool:
                """把当前所有开关的值写回配置。失败返回 False。"""
                try:
                    raw = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
                except Exception as e:
                    ui.notify(f"读取 os_state.json 失败：{e}", type='negative', icon='error')
                    return False
                perms = raw.setdefault('permissions', {})
                for _k, _sw in switch_map.items():
                    perms[_k] = bool(_sw.value)
                try:
                    cfg_path.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
                except Exception as e:
                    ui.notify(f"写入 os_state.json 失败：{e}", type='negative', icon='error')
                    return False
                return True

            def _on_toggle(key):
                """即时生效。🔴 写盘失败 → 把开关拨回去，不能让界面和盘上说两套话。"""
                if _suppress["v"]:
                    return
                if _persist():
                    return
                _suppress["v"] = True
                try:
                    switch_map[key].value = not switch_map[key].value
                finally:
                    _suppress["v"] = False

            for key, icon, label, desc, tier in self._OS_PERMISSION_META:
                is_red = tier == "red"
                accent = "var(--nano-danger)" if is_red else "var(--nano-warn)"
                with ui.row().style(
                    f'width:100%; align-items:center; gap:12px; padding:10px 12px; '
                    f'border-radius:10px; background:var(--nano-panel); '
                    f'border:1px solid {"rgba(var(--nano-danger-rgb),0.18)" if is_red else "rgba(var(--nano-shade-rgb), 0.06)"};'
                ):
                    ui.icon(icon).style(f'font-size:var(--nano-fs-3xl); color:{accent}; flex-shrink:0;')
                    with ui.column().style('gap:1px; flex:1; min-width:0;'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label(label).style('font-size:var(--nano-fs-base); color:var(--nano-fg); font-weight:600;')
                            if is_red:
                                ui.label('高危').style(
                                    'font-size:var(--nano-fs-2xs); font-weight:700; color:var(--nano-danger); '
                                    'background:rgba(var(--nano-danger-rgb),0.12); padding:1px 6px; '
                                    'border-radius:999px; letter-spacing:0.03em;'
                                )
                        ui.label(desc).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.4;')
                    switch_map[key] = ui.switch(value=bool(current.get(key, False))) \
                        .props(f'color={"red-5" if is_red else "amber-5"}').style('flex-shrink:0;') \
                        .on_value_change(lambda e, k=key: _on_toggle(k))

        # 🪦 原来这里是「取消 / 应用」两个按钮 + do_apply()。
        #    2026-08-29 定：去掉一切保存/取消 —— **开关最终是什么，就生效什么**。
        #    ⇒ 写盘挪到每个开关的 on_value_change 上（见上面的 _on_toggle）。
        # 🪦 顺带删掉的还有「含高危」徽标的刷新逻辑（已定：徽标不要了）。

    # ── 后台任务 / 诈尸机制 ──────────────────────────────────────────────────

    def _show_permissions_dialog(self):
        """OS 权限面板：6 个布尔开关，整体读改写，保留 `data/os_state.json` 里
        不归这个面板管的字段（目前是 `auto_mode`）。改完立即生效，
        不需要重启——OSDispatcher 每次构造都会重新 dsl.load_permissions()。
        """
        cfg_path = os_dsl.os_state_path()
        current = os_dsl.load_permissions()

        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     'width:460px; background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.2); '
                     'border-radius:16px; padding:0; overflow:hidden;'
                 ):
                # 标题栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                    'background:var(--nano-panel); border-radius:16px 16px 0 0; flex-wrap:nowrap;'
                ):
                    with ui.row().classes('items-center gap-3 flex-1 min-w-0'):
                        ui.icon('admin_panel_settings').style('font-size:var(--nano-fs-4xl); color:var(--nano-fg-soft); flex-shrink:0;')
                        with ui.column().style('gap:2px; min-width:0;'):
                            ui.label('OS 权限').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                            ui.label('控制 Nano 能在系统层面做什么，改完立即生效').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;'
                            )
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;').classes('flex-shrink-0')

                self._build_settings_permissions()

            dialog.open()

    # ── 后台任务 / 诈尸机制 ──────────────────────────────────────────────────

    def _start_bg_task(self, display: str, coro, suspension_ref: str | None = None) -> str:
        """注册并启动后台任务。display 是用户可见的任务描述。返回 task_id。

        suspension_ref 不为空时，本任务被当作"某个挂起在等的后台进程"——
        完成时不走诈尸追加，而是触发 background 唤醒（notify_background_done），
        让 Nano 带着上下文起新 turn 接着做。这是 wait_for(wake_on=['background'])
        的生产者端：任何会产出后台结果的能力（未来 MCP 长任务/OS 长命令）
        spawn 任务时带上 wait_for 给的 bg_task_ref 即可，无需重新发明唤醒。
        """
        import uuid as _uuid
        task_id = _uuid.uuid4().hex[:8]
        # ⭐⭐⭐ **权威记录先落，再起协程。**
        #    顺序是有讲究的：先落记录，万一起协程那一步炸了，历史里至少留着
        #    「有个后台任务试图开始」；反过来则会跑起一个**没有任何记录的**任务。
        #    📌 **先落成事实、再执行**（同那条崩溃窗口的处置）。
        # ⚠️ 归给「那件事」（对话类 Task）—— 用 `owner_label()` 而不是 `ensure_...`：
        #    后台任务**不该让一件事诞生**。它只在某件事进行中才会被起来，
        #    而那件事早就因为别的归属物（等待）存在了；如果确实没有，
        #    留空比无端造一件事诚实。
        _rt_tid = None
        try:
            from core.runtime import task as _rt_task
            _rt_tid = _rt_task.create_background_job(display, _rt_task.owner_label())
        except Exception as _e_bt:
            logger.warning(f"[Task] 后台任务权威记录没落上（任务照旧跑）: {_e_bt}")
        self._bg_tasks[task_id] = {
            "display": display,
            "started_at": time.time(),
            "suspension_ref": suspension_ref,
            "rt_task_id": _rt_tid,       # 权威记录的 id（可能为 None）
        }
        # ⭐⭐⭐ **保住 asyncio.Task 句柄。**
        #    🔴 原来是 `asyncio.create_task(...)` **返回值直接丢掉** —— 两个后果：
        #      ① **没有句柄就没有终止能力**。早先就要求每个后台任务
        #         有自己的手动终止按钮，而在此之前那个按钮**物理上不可能实现**：
        #         没有任何东西可以被 cancel。
        #      ② asyncio 的已知陷阱：**没有强引用的 task 可能在完成前被 GC 回收**，
        #         表现为后台任务偶发「跑了一半没了」，而且不留任何痕迹。
        #    📌 **一个「以后要能停下它」的东西，创建时就得把句柄留住** ——
        #       句柄不是终止功能的一部分，它是终止功能的**前提**。
        _aio = asyncio.create_task(self._run_bg_task(task_id, coro))
        self._bg_tasks[task_id]["aio"] = _aio
        return task_id

    def _set_skill_ui_status(self, name: str, status: str) -> None:
        """更新抽屉中一项本地 Skill 的活动态。"""
        elements = self.skill_ui_elements.get(name)
        if not elements:
            return
        is_running = status == "RUNNING"
        color = "var(--nano-fg-soft)" if is_running else "var(--nano-ok)"
        elements["status"].set_text(status)
        elements["status"].style(
            f'font-size:var(--nano-fs-2xs); color:{color}; letter-spacing:0.04em; margin-right:4px;')
        try:
            glow = ' box-shadow:0 0 6px var(--nano-fg-soft)88;' if is_running else ''
            elements["icon"].style(
                'width:7px; height:7px; border-radius:50%; '
                f'background:{color}; flex-shrink:0; transition:background 0.3s;{glow}')
        except Exception:
            pass

    def _is_handed_back_skill_running(self, name: str) -> bool:
        """该 Skill 是否仍有一个被系统交还、尚未完成的载体。"""
        return any(meta.get("skill_name") == name
                   for meta in getattr(self, "_handed_back_carriers", {}).values())

    def _refresh_handed_back_skill_statuses(self) -> None:
        """新 pipeline 重置抽屉后，恢复跨回看轮仍在跑的本地 Skill。"""
        for name in {meta.get("skill_name")
                     for meta in getattr(self, "_handed_back_carriers", {}).values()}:
            if name:
                self._set_skill_ui_status(name, "RUNNING")

    def _start_handed_back_carrier(self, display: str, coro, suspension_ref: str,
                                    skill_name: str = "",
                                    rt_task_id: str | None = None,
                                    owns_record: bool = True) -> str:
        """保住交还载体的句柄，并在结束时唤醒等待它的当前工作。

        它在执行上当然是独立协程；语义则由 `rt_task_id` 有没有值区分：

          · `None`  —— **系统交还**：当前工作仍依赖它的结果。不进抽屉、不进
            pill、没有权威 Task 记录（2026-08-10 定的：共享执行手段不代表
            共享用户语义）。
          · 有值    —— **它已经不在 Nano 手头了**：要么是 Subagent（生来如此），
            要么是模型调了 `dont_wait`。这时它进抽屉、进 pill、有 `■`。

        ⚠️ **收权威记录的地方必须在这里**，不在创建它的地方 ——
           📌 逐字同形：收口要落在拥有该载体**真实终态**的地方，
              而不是从展示用文本或"我以为它完了"去猜。
        """
        import uuid as _uuid
        carrier_id = _uuid.uuid4().hex[:8]
        carriers = getattr(self, "_handed_back_carriers", None)
        if carriers is None:
            carriers = {}
            self._handed_back_carriers = carriers

        async def _run():
            _outcome, _note = "completed", ""
            try:
                result = await coro
            except asyncio.CancelledError:
                # ⭐ 用户点了 `■`（或进程收尾）。仍要通知等待方，避免活
                #    WaitRecord 因一个已经死亡的载体而永久挂着。
                # ⚠️ `cancelled` **不并进 `failed`** —— 早先那条实测结论：
                #    用户主动停掉不是失败，归进 failed 会让模型和用户都去排查
                #    一个不存在的问题。
                _outcome = "cancelled"
                # 🔴🔴 **实测 2026-08-20：这里把一句准确的话盖成了一句笼统的话。**
                #    Subagent自己在 `_agent_runner` 的取消分支里写的是
                #    「这个Subagent被用户手动终止了」—— 而它 re-raise 之后，
                #    载体这一层用下面这段**通用文案**覆盖了 `result`，
                #    于是 main agent 收到的是「被中止了，没有返回结果」，
                #    它只好去猜：「可能是那个目录太大…被系统停了，或者其他原因」。
                #    📌 **两个人都写这条结论时，后写的那个会盖掉先写的** ——
                #       而先写的那个才是知道真相的（同 `owns_record` 那条，
                #       刚为它写过这句判据，转头在【结论文本】上又犯了一次）。
                #    ⭐ 所以这里要分清**是谁按的停**：UI 那颗 `■` 会先落一个标记。
                _by_user = bool((carriers.get(carrier_id) or {}).get("cancelled_by_user"))
                _note = "用户手动终止"
                if _by_user:
                    result = ("[System record: the user manually stopped this from the "
                              "task drawer. It did not fail and it did not finish - "
                              "the user decided to stop it. Do NOT restart it on your "
                              "own; tell them plainly that it was stopped and let them "
                              "decide what happens next.]")
                else:
                    _note = "载体在返回结果前终止"
                    result = ("[System record: this stopped before it returned a "
                              "result, and nobody asked for that - it was not the "
                              "user. Do NOT assume it succeeded, and say plainly that "
                              "you do not know why it stopped.]")
            except Exception as e:
                _outcome, _note = "failed", f"{type(e).__name__}: {e}"[:160]
                result = f"后台执行失败：{type(e).__name__}: {e}"
            finally:
                _meta = carriers.pop(carrier_id, None) or {}
                # ⚠️ 从**表里**取 id，不用闭包里那个 —— `dont_wait` 是在载体
                #    起跑之后才补上它的。📌 一个「稍后可能被补上」的字段，
                #    必须在用它的那一刻现读，不能在创建时快照。
                _rt = _meta.get("rt_task_id") or rt_task_id
                # ⚠️ **`owns_record=False` 的不收** —— Subagent自己的协程
                #    已经在它的 `finally` 里收过了（它才知道真实 outcome：
                #    completed / failed / cancelled 三分）。这里再收一次会用
                #    一个更粗的判断**覆盖**那个结论。
                #    📌 **一条记录只能有一个收尾人** —— 两个都收的表现是
                #       「后收的把先收的盖掉」，而且不会报错。
                if _rt and _meta.get("owns_record", owns_record):
                    try:
                        from core.runtime import task as _rt_task_c
                        _rt_task_c.finish_background_job(_rt, _outcome, _note)
                    except Exception as _e_fc:
                        logger.warning(f"[B1] 载体 {carrier_id} 收权威记录失败: {_e_fc}")
                    try:
                        with self._ui_scope():
                            self._refresh_tasks_panel()
                    except Exception:
                        pass
                # 只有载体真实结束才允许本地 Skill 离开 RUNNING；若同一 Skill
                # 还有另一条交还载体，仍保持 RUNNING。
                if skill_name:
                    try:
                        with self._ui_scope():
                            self._set_skill_ui_status(
                                skill_name,
                                "RUNNING" if self._is_handed_back_skill_running(skill_name)
                                else "OK")
                    except Exception:
                        pass
            # ⚠️⚠️ **这一段必须包住 `CancelledError`。**
            #    🔴 改造前这里是裸的，注释写着「没有 UI 取消入口；这里只可能是
            #       进程收尾」—— 而 2026-08-20 那颗 `■` 就是 UI 取消入口，
            #       那句话当场过期了。
            #    `cancel()` 可能在协程不在 await 点时到达，于是 `_must_cancel`
            #    置位、**下一个 await 再抛一次** —— 正好打在这个通知上，把它吃掉，
            #    而等着它的那条 WaitRecord 就再也醒不了。
            #    📌 **一句「这里不可能发生 X」的注释，会在有人给 X 修了一条路之后
            #       原地过期，而它不会报错** —— 它只是从此开始说谎。
            #    ⭐ 权威记录已经在上面**同步**落好了，所以最坏情况只丢一次通知，
            #       而挂起那边有 orphan 兜底会收（同 `_run_bg_task` 那条处置）。
            try:
                await self.notify_background_done(suspension_ref, result_hint=result)
            except asyncio.CancelledError:
                logger.warning(
                    f"[B1] 载体 {carrier_id} 的收尾通知被第二次取消打断 —— "
                    f"权威记录已落（{_outcome}），等待那边靠 orphan 兜底回收")

        aio = asyncio.create_task(_run())
        carriers[carrier_id] = {"display": display, "aio": aio,
                                "suspension_ref": suspension_ref,
                                "skill_name": skill_name,
                                "rt_task_id": rt_task_id,
                                "owns_record": owns_record}
        return carrier_id

    def _promote_carrier_to_background(self, bg_ref: str, display: str) -> bool:
        """模型说「这个调用我不等了」→ 给它一条权威记录，让它进抽屉。

        ⚠️ **不碰那个载体本身** —— 它照旧在跑。变的只是「它算不算一件
           用户该看得见、也该能终止的独立工作」。
           📌 与早先的设计那条同源：**改的是注意力的归属，不是执行体。**
        """
        carriers = getattr(self, "_handed_back_carriers", None) or {}
        _hit = None
        for _cid, _meta in carriers.items():
            if (_meta or {}).get("suspension_ref") == bg_ref:
                _hit = (_cid, _meta)
                break
        if _hit is None:
            # ⚠️ 最常见的原因是**它刚好跑完了** —— 那时不该再建一条 Running。
            #    📌 如实记一条日志即可：模型那边已经收到「我会叫你」，而
            #       完成唤醒本来就会到。
            logger.info(f"[B1] dont_wait 找不到载体 {bg_ref}（多半刚完成）—— 不建记录")
            return False
        _cid, _meta = _hit
        if _meta.get("rt_task_id"):
            return True                      # 幂等：同一条别建两次
        try:
            from core.runtime import task as _rt_task_p
            _tid = _rt_task_p.create_background_job(
                display or _meta.get("display") or "后台任务",
                _rt_task_p.owner_label())
        except Exception as e:
            # 📌 治理/展示层的故障不许把能力本身搞掉（同 那条）：
            #    记录建不上，那个调用照旧在后台跑、照旧会唤醒 Nano。
            logger.warning(f"[B1] dont_wait 建后台任务记录失败（载体照旧跑）: {e}")
            return False
        _meta["rt_task_id"] = str(getattr(_tid, "task_id", "") or _tid or "")
        # ⭐ 它**此刻就在跑**（`dont_wait` 是对一个已经启动的载体说的）——
        #    不 mark 的话抽屉会把一个正在跑的东西显示成「排队中」。
        #    📌 同Subagent那处：一条状态的唯一写入者一死，它就变成一个不会改变的谎。
        try:
            _rt_task_p.mark_background_running(_meta["rt_task_id"])
        except Exception as _e_mr2:
            logger.debug(f"[B1] 载体转 RUNNING 失败（照旧跑）: {_e_mr2}")
        logger.info(f"[B1] {(display or '')[:40]} 进抽屉 "
                    f"（carrier={_cid} task={_meta['rt_task_id']}）")
        try:
            with self._ui_scope():
                self._refresh_tasks_panel()
        except Exception:
            pass
        return True

    def _cancel_carrier(self, rt_task_id: str) -> bool:
        """终止一条**已经不在手头**的载体（Subagent / 被 `dont_wait` 的调用）。

        ⭐ 收尾**不在这里做** —— `cancel()` 之后 `_run()` 的 `finally` 会拿到
           `CancelledError`，在那里记 `cancelled` 并通知等待方。
           📌 一个动作的结果只该被记一次，而记它的地方是**知道真实终态**的
              那一个；这里只负责扣扳机。
        """
        for _cid, _meta in list((getattr(self, "_handed_back_carriers", None) or {}).items()):
            if (_meta or {}).get("rt_task_id") != rt_task_id:
                continue
            _aio = (_meta or {}).get("aio")
            if _aio is None or _aio.done():
                return False
            # ⭐ **先落标记，再扣扳机** —— 📌 顺序反了的话，`_run()` 的取消分支
            #    可能在同一轮事件循环里先跑到，读到的还是"没人按过停"。
            #    （同 「先落成事实、再执行」那条。）
            _meta["cancelled_by_user"] = True
            _aio.cancel()
            logger.info(f"[B1] 用户终止载体 {_cid}（task={rt_task_id}）")
            return True
        return False

    def _bg_slot_sem(self):
        """后台 slot 信号量。**懒建** —— 它必须在事件循环里第一次被用到时才存在。

        ⚠️ 上限取自 `task.MAX_BACKGROUND_RUNNING`，**不在这里另写一个数** ——
           📌 一个限制有两个数字来源，迟早会变成两个不同的限制。
        """
        sem = getattr(self, "_bg_sem", None)
        if sem is None:
            try:
                from core.runtime.task import MAX_BACKGROUND_RUNNING as _n
            except Exception:
                _n = 3
            sem = asyncio.Semaphore(_n)
            self._bg_sem = sem
            logger.info(f"[Task] 后台并发上限 = {_n}（超出的排队，**不失败**）")
        return sem

    def cancel_bg_task(self, task_id: str, by: str = "user") -> bool:
        """用户手动终止一个后台任务。返回是否真的停掉了一个还在跑的。

        ⚠️⚠️ **这个方法的存在本身就是实测 Claude Code 得出的那三条要求的兑现**
。那边的反面教材：被用户手动终止时，模型
        **什么都没收到**，任务就是不存在了，output 文件 0 字节 ——
        「跑完了但没输出」/「崩了」/「被停了」三种情况表象完全一样。

        所以这里三件事都做，一件都不许省：
          ① **产出一条事实记录**（Task 终态 CANCELLED），不只是「进程没了」
          ② **告诉模型**（写进 memory，它下一轮读得到）
          ③ **CANCELLED 不是 FAILED** —— 用户主动停掉不是失败
        """
        meta = self._bg_tasks.get(task_id)
        if not meta:
            return False
        _aio = meta.get("aio")
        _disp = meta.get("display") or task_id
        # ① 先取消协程。⚠️ `cancel()` 只是**请求**，真正的收尾在 `_run_bg_task`
        #    的 CancelledError 分支里 —— 那里才知道该记成 cancelled。
        try:
            if _aio is not None and not _aio.done():
                _aio.cancel()
            else:
                return False
        except Exception as e:
            logger.warning(f"[Task] 终止后台任务 {task_id} 失败: {e}")
            return False
        meta["cancelled_by"] = by

        # ⭐⭐ ② **再停掉载体本身**（2026-08-22 那次建模 新增）。
        #
        # 🔴 在此之前这里**只取消协程**，而协程只是「我在等它」这件事 ——
        #    那条命令**还在跑**。函数自己的结果文案都写着
        #    「its result is unknown」，那句话诚实地承认了：
        #    **停掉的是自己的等待，不是那件事。**
        # ⚠️ 于是用户点了 ■、看着它从抽屉里消失，而 pip 还在后台装。
        #    📌 **一个「停掉」的按钮，如果只停掉了我们自己的等待，
        #       比没有这个按钮更坏** —— 用户会以为已经停了。
        # ⚠️ 只对**命令**有效（`cmd_` 前缀）。MCP 停不了（server 在对端）、
        #    Skill 停不了（`importlib` 进程内执行，Python 没有安全中断手段）——
        #    那两类**如实留日志，不假装停掉了**。
        _ref = str(meta.get("suspension_ref") or "")
        if _ref.startswith("cmd_"):
            try:
                from core.os_layer import longcmd as _lc_stop
                if _lc_stop.stop(_ref, f"stopped by {by}"):
                    logger.info(f"[Task] 载体 {_ref} 已真正停止（进程树）")
                else:
                    logger.info(f"[Task] 载体 {_ref} 已经不在了（多半刚跑完）")
            except Exception as e:
                logger.warning(f"[Task] 停止载体 {_ref} 失败（等待已取消）: {e}")
        elif _ref:
            logger.info(f"[Task] 载体 {_ref} 属于停不掉的一类（MCP/Skill）——"
                        f"只取消了等待，它可能仍在运行")

        logger.info(f"[Task] 用户手动终止后台任务 {task_id}（{_disp[:40]}）")
        return True

    async def _run_bg_task(self, task_id: str, coro):
        """包装协程：完成后——若关联了挂起则触发 background 唤醒，否则走诈尸追加。

        ⭐⭐⭐ **三种结局必须分开。**
        🔴 原来只有两行：`try: result = await coro` / `except: result = f"执行失败：{e}"`
           —— 于是异常被压成一个字符串，然后走**和成功完全一样**的路径。
           「跑完了但没输出」/「崩了」/「被用户停了」在下游**表象完全一致**。
        ⚠️ 而这正是实测 Claude Code 时观察到、并写成三条要求的那个问题
           。原话：「**要不是在对话里说了一句，模型永远不会知道。**」
        📌 **一个能分辨三种结局的系统，和一个能描述其中一种的系统，
           差的不是细节，是「历史能不能读出真相」。**
        """
        _outcome, _note = "completed", ""
        # ⭐⭐⭐ **等一个后台 slot。**
        #
        # 🔴 在此之前后台并发**完全没有上限** —— `asyncio.create_task` 想起多少起多少。
        #    而后台任务**每完成一个就唤醒一次模型**，所以那不只是句柄上限，
        #    **它是一个成本乘数**：同时二十个下载，完成时就是二十次模型调用。
        #
        # ⚠️⚠️ **满了的出口是「等」，不是「失败」。**
        #    📌 **闸的出口是失败，队列的出口是稍后处理** —— 这条判据这是第四次用到
        #       （前三次：`pipeline_lock` 丢消息 / 早先的闸-vs-挂起 / inbox）。
        #    ⭐ 而「在排队」有它自己的诚实状态：Task 停在 **ACTIVE + IDLE**
        #       （`CREATE` 恒定写 IDLE，本来就是「还没跑」），拿到 slot 才转 RUNNING。
        #       📌 **「在排队」和「在跑」必须是两个状态** —— 压成一个之后上限就数不清了。
        _sem = self._bg_slot_sem()
        async with _sem:
            try:
                from core.runtime import task as _rt_task
                _rt_task.mark_background_running(
                    (self._bg_tasks.get(task_id) or {}).get("rt_task_id"))
            except Exception as _e_mr:
                logger.debug(f"[Task] 后台任务 {task_id} 转 RUNNING 失败（照旧跑）: {_e_mr}")
            return await self._run_bg_task_inner(task_id, coro)

    async def _run_bg_task_inner(self, task_id: str, coro):
        """真正跑那个协程 + 分辨三种结局。**已经持有 slot 才会进来。**"""
        _outcome, _note = "completed", ""
        try:
            result = await coro
        except asyncio.CancelledError:
            # ⭐⭐ 用户手动终止走这条。**必须最先接**（CancelledError 在 3.8+
            #    继承 BaseException，不被下面那个 `except Exception` 捕获 ——
            #    所以原实现里它会**直接穿透上去**，连那句「执行失败」都不会有）。
            _outcome = "cancelled"
            _by = (self._bg_tasks.get(task_id) or {}).get("cancelled_by") or "user"
            _note = f"被 {_by} 手动终止"
            # ⚠️ 结果文案要说清「停在哪」而不是假装完成。
            result = ("[System record: this background job was stopped manually by the "
                      "user before it finished. Its result is unknown — do NOT assume "
                      "it succeeded or failed.]")
        except Exception as e:
            _outcome = "failed"
            _note = f"{type(e).__name__}: {e}"[:160]
            result = f"执行失败：{e}"
        meta = self._bg_tasks.pop(task_id, {})
        # ⭐ 收掉权威记录。⚠️ 放在 `pop` 之后、通知之前：
        #    先把「这件事结束了」落成事实，再去叫醒别人 ——
        #    📌 顺序反了的话，被叫醒的那一方可能读到一条还自称 ACTIVE 的记录。
        try:
            from core.runtime import task as _rt_task
            _rt_task.finish_background_job(meta.get("rt_task_id"), _outcome, _note)
        except Exception as _e_ft:
            logger.warning(f"[Task] 后台任务 {task_id} 收权威记录失败: {_e_ft}")
        _ref = meta.get("suspension_ref")
        # ⚠️⚠️ **被终止时也必须走通知** —— 否则等它的那条挂起就再也醒不了，
        #    而那正是 2026-08-04 那条不死挂起的成因（background 源一去不回）。
        #    📌 **「这件事没成」和「这件事没消息」对等着它的那一方完全不同** ——
        #       前者能让它继续，后者让它永远等。
        # ⚠️ 而这一段之所以要包一层 CancelledError：`cancel()` 有可能在协程不在
        #    await 点时被调用，那时 `_must_cancel` 会置位、**下一个 await 再抛一次**
        #    —— 于是它会打在下面这个 await 上，把通知吃掉。
        #    ⭐ 权威记录已经在上面**同步**落好了（`finish_background_job` 不是协程），
        #       所以最坏情况只丢一次通知，而挂起那边有 orphan 兜底会收 ——
        #       📌 **把不可靠的那一步排在可靠的那一步之后。**
        try:
            if _ref:
                # 后台进程是某个挂起在等的——唤醒接上，不另起诈尸气泡
                await self.notify_background_done(_ref, result_hint=result)
                return
            with self._ui_scope():
                await self._append_zombie_bubble(meta, result)
        except asyncio.CancelledError:
            logger.warning(
                f"[Task] 后台任务 {task_id} 的收尾通知被第二次取消打断 —— "
                f"权威记录已落（{_outcome}），挂起那边靠 orphan 兜底回收")

    async def notify_background_done(self, ref: str, result_hint: str | None = None):
        """background 唤醒入口：某个后台进程（ref）完成 → 唤醒等它的挂起。

        生产者（未来的 MCP 长任务/OS 长命令）拿到 wait_for 返回的 bg_task_ref 后，
        完成时调用本方法即可。消费者端（resume_suspension）已就绪。
        """
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            _active = _wc.list_live(get_kernel(), oldest_first=True)
        except Exception:
            _active = []
        _hit = [r for r in _active
                if _wc.WakeSource.BACKGROUND in r.wake_on and r.bg_ref == ref]
        if not _hit:
            _settled = self._settle_cancelled_handback_actions(ref)
            if _settled:
                logger.info(f"[Suspension] background 完成 ref={ref}；等待已取消，"
                            f"仅收 {_settled} 条原始动作 UI，不唤醒 Nano")
                return
            logger.info(f"[Suspension] background 完成 ref={ref}，但无匹配 active 挂起（可能已被其它源唤醒）")
            return
        for r in _hit:
            sid = r.wait_id
            # ⚠️⚠️ [] **这里原来先把 pill 定型成「▶ 后台完成，继续」再去唤醒。**
            #    而 `_drive_wake` 内部早就把「定型」挪到了**拿到锁之后**，
            #    注释写得很清楚：「真正拿到锁、即将起唤醒 turn 时才把活 pill 定型
            #    （避免内核忙时提前定型）」。
            # 🔴 **但那次修改只改了它自己那条路径，这一处仍在提前定型** ——
            #    于是内核忙时用户看到的是绿色的「后台完成，继续」，
            #    **而后面什么都不会发生**。截图里就是这个。
            # 📌 **一个「等拿到锁再定型」的修法，如果只改了其中一条调用路径，
            #    另一条路径上的 UI 仍然在说谎** —— 而它说的还是「完成了，继续」，
            #    比什么都不说更糟。
            # ⭐ 现在统一交给 `_drive_wake`：它要么拿到锁并定型，
            #    要么把唤醒意图排进队列（pill 保持转圈，那是**真实状态**）。
            await self._drive_wake(sid, trigger="background", note=(result_hint or ""))

    # ══════════════════════════════════════════════════════════════════════
    # 异步产出与后台故障的统一出口（ChatEmitter）
    # ══════════════════════════════════════════════════════════════════════
    #
    # 它取代了原来的 _proactive_push。原实现有四层问题，逐层修掉：
    #
    # 第 1 层｜旧实现往“上一条回复”的内容容器追加；冷启动时引用为空，开头直接
    #   吞掉。已确证受害者：ui.timer(2, _restore_suspensions, once=True) 在
    #   启动 2 秒后跑，那一刻必定为空 —— "我重启前还挂着在等 XX" 这句话从上线至今
    #   从未真正显示过。修法：永远在 chat_container 根容器新建独立块。
    #
    # 第 2 层｜client 上下文也为空：_ui_scope() 依赖的 _ui_client 只在用户主动交互时
    #   才赋值。修法见 render() 结尾的提前捕获（比 _on_browser_connect 更早，那里要等
    #   WebView2 子进程 + socket 握手好几秒，而 RAG 初始化线程在 WebUI() 构造时就在跑了）。
    #
    # 第 3 层｜语义错误 + 副作用：把"新说的一句话"追加进"上一条回复"，语义就不对；
    #   更糟的是它还 status_lbl.set_text("Nano")，把那一轮的 "8.2s · 2.2K tok" 抹掉了。
    #
    # 第 4 层｜绝不写对话历史：后台线程在 tool_calls 与 tool_results 之间插一条
    #   assistant，会让 validate_tool_turns 失败 → _rollback_last_tool_batch 把已经跑完的
    #   tool_results 吃掉、留下孤儿 tool_calls → 当轮 RuntimeError 硬崩 → 下一轮 400。
    #   模型侧的知情改走 system prompt（RecentSystemEvents + 能力边界）。

    def _mark_ui_ready(self):
        """render() 结尾调用。在此之前发生的事件只入队不渲染，之后由唯一消费者渲染。"""
        try:
            from nicegui import context as _ctx
            self._ui_client = _ctx.client
        except Exception as e:
            logger.warning(f"[UI] 提前捕获 auto-index client 失败: {e}")
        self._ui_ready = True
        logger.info("[UI] ui_ready=True（chat_container 与 client 均已就绪）")
        # 冲掉 render 之前攒下的事件
        _pending, self._pending_chat_events = list(self._pending_chat_events), []
        for _ev in _pending:
            try:
                self._render_chat_event(_ev)
            except Exception as e:
                logger.warning(f"[UI] 冲刷待渲染事件失败: {e}")

    def _render_chat_event(self, ev: dict):
        """唯一的聊天区渲染入口。永远在根容器新建独立块，绝不碰上一条回复。"""
        if not self.chat_container:
            return
        _key = ev.get("dedupe_key")
        if _key and _key in self._emitted_chat_keys:
            return          # 一个事件只渲染一次
        if _key:
            self._emitted_chat_keys.add(_key)

        category = ev.get("category", "speech")
        with self._ui_scope():
            # 空状态问候语是 chat_container 的【兄弟节点】且排在它后面，所以聊天区
            # 一有内容它就会被顶到下面去。原来清它的入口只有 start_pipeline_task
            # （用户发消息）和两处重置——统一出口是第四条入口，不经过那里，于是
            # 冷启动推送/故障卡出现后问候语还挂在下面。这里补上。
            self._clear_empty_state_greeting()
            with self.chat_container:
                _blk = ui.column().classes('w-full py-1 mb-8')
                with _blk:
                    with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                        ui.label('nano ❯').style(
                            'color:var(--nano-ok); font-size:var(--nano-fs-lg); line-height:1.75rem; flex-shrink:0; '
                            'min-width:64px; text-align:right; font-family:var(--nano-mono);')
                        _inner = ui.column().classes('w-full gap-0 min-w-0')
                        with _inner:
                            if category == "fault":
                                self._render_fault_card(ev)
                            else:
                                self._render_speech(ev, _blk)
        try:
            self.scroll_area.scroll_to(percent=1.0, duration=0.2)
        except Exception:
            pass

    def _render_fault_card(self, ev: dict):
        """醒目错误卡。宗旨：尽可能让致命报错摆脱 cmd —— Nano 是原生桌面
        应用，真实用户不会去翻控制台。"""
        with ui.column().classes('w-full gap-1').style(
            'border:1px solid rgba(var(--nano-danger-rgb),0.35); border-left:3px solid var(--nano-danger); '
            'border-radius:10px; background:rgba(var(--nano-danger-rgb),0.07); padding:10px 12px;'
        ):
            ui.label(ev.get("title") or '有个能力出问题了').style(
                'font-size:var(--nano-fs-md); font-weight:700; color:var(--nano-danger); letter-spacing:0.01em;')
            for _line in (ev.get("lines") or []):
                ui.label(_line).classes('text-[13px] leading-6').style('color:var(--nano-fg);')
            _hints = [h for h in (ev.get("hints") or []) if h]
            if _hints:
                # ⚠️ 加一行小标题，把「事实」和「你能做什么」分开。
                #    📌 没有标题时那几行读起来像**卡片在跟你聊天**；
                #       故障卡的语气应该是系统提示 —— 陈述 + 可执行项，不是对话。
                #    ⭐ 落在**通用渲染处**，所以所有 fault 卡一起变（不只 MCP）。
                with ui.row().classes('items-center gap-1').style('margin-top:4px;'):
                    ui.label('修复建议').style(
                        'font-size:var(--nano-fs-xs); font-weight:700; color:var(--nano-amber); '
                        'letter-spacing:0.08em;')
                    ui.element('div').style(
                        'flex:1; height:1px; background:rgba(var(--nano-amber-rgb), 0.22);')
                for _h in _hints:
                    ui.label(f'· {_h}').style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft); line-height:1.6;')

    def _render_speech(self, ev: dict, block):
        """标准 Nano 气泡。intervention_id 非空（主动智能 L2）时挂 hover 反馈入口。"""
        content = ev.get("body") or ""
        intervention_id = ev.get("intervention_id")
        if not intervention_id:
            nano_md(content)
            return
        _grp = ui.column().classes('group gap-1')
        with _grp:
            nano_md(content)
            _fb = ui.row().classes(
                'gap-3 opacity-0 group-hover:opacity-100 transition-opacity duration-200')

            def _send(sig, dismiss=False):
                try:
                    self._intel_engine.feedback(sig, intervention_id)
                except Exception:
                    pass
                try:
                    _fb.set_visibility(False)
                    if dismiss:
                        _grp.set_visibility(False)
                except Exception:
                    pass

            with _fb:
                # ✕=打扰(timing 轴)，不准=内容(correctness 轴)，两轴天然分开
                for _txt, _sig, _dis in (
                    ('有用', _ISignal.ACCEPTED, False),
                    ('不准', _ISignal.WRONG, False),
                    ('别再提醒这类', _ISignal.MUTE_THIS, True),
                    ('✕', _ISignal.ANNOYED_INTRUSIVE, True),
                ):
                    ui.label(_txt).classes('text-[11px] cursor-pointer') \
                      .style('color:var(--nano-fg-mute);') \
                      .on('click', lambda e, s=_sig, d=_dis: _send(s, d))

    def emit_chat(self, *, category: str = "speech", body: str = "",
                  title: str = "", lines: list | None = None,
                  hints: list | None = None, intervention_id: str = None,
                  dedupe_key: str = ""):
        """统一出口。UI 未就绪时入队而不是丢弃（绝不再出现 `if not container: return`
        这种永久丢事件的路径）。

        六类消费者都走这条：主动开口 / 后台任务完成 / 被动挂起提示 / 挂起恢复提醒 /
        canary 自检告警 / 后台组件致命故障。新增异步产出一律走这里，不许各写各的。
        """
        ev = {
            "category": category, "body": body, "title": title,
            "lines": lines or [], "hints": hints or [],
            "intervention_id": intervention_id, "dedupe_key": dedupe_key,
        }
        if not getattr(self, "_ui_ready", False):
            self._pending_chat_events.append(ev)
            logger.info(f"[Emit] UI 未就绪，事件入队（当前 {len(self._pending_chat_events)} 条）")
            return
        self._render_chat_event(ev)

    async def _proactive_push(self, content: str, intervention_id: str = None):
        """主动开口的旧入口，保留签名——orchestrator._push_callback / ProactiveSpeaker /
        IntelEngine 都持有它。现在只是 emit_chat 的薄包装。

        ⚠️ 已移除原来的 `self.agent.memory.add_message("assistant", content)`：
        异步写对话历史会撕裂 tool_calls/tool_results 事务（见本节顶部第 4 层）。
        模型侧的知情改走 RecentSystemEvents。
        """
        try:
            from core.health import get_system_events
            get_system_events().add(f"Nano spoke up on its own: {content[:120]}")
        except Exception:
            pass
        self.emit_chat(category="speech", body=content, intervention_id=intervention_id)

    # ── 健康登记表的唯一 UI 消费者 ────────────────────────────────────────
    async def _health_consumer_tick(self):
        """定时 drain 状态转移队列。

        为什么是"登记 + 轮询"而不是 callback：故障可能发生在事件循环存在之前
        （_init_rag_async 跑在普通 threading.Thread 里，且 WebUI() 构造早于 ui.run()），
        也可能发生在 UI 尚未构建完成时。项目里已有同款范式：rag_engine._init_stage_log
        线程写、UI 轮询读。
        """
        try:
            from core.health import get_health, get_system_events, Transition, Status, Severity
        except Exception:
            return
        _h = get_health()
        _events = _h.drain_transitions()
        if not _events:
            # 没有新转移也要重画一次：一轮对话结束时 navigate_pipeline 会写活动态，
            # 万一哪条路径漏了健康判断，1 秒内会被这里纠正回来（自愈，不靠调用方自觉）。
            try:
                self._refresh_monitor_health()
            except Exception:
                pass
            return

        _sys = get_system_events()
        _to_card = []
        for _t in _events:
            _st = _t.state
            _spec_label = _st.snapshot().get("label", _st.capability)
            # 事件流（给模型看）：所有转移都记，含 degraded 与 recovered
            if _t.kind == Transition.RECOVERED:
                _sys.add(f"{_spec_label} recovered and is available again.")
            elif _st.status == Status.DEGRADED:
                _sys.add(f"{_spec_label} degraded: {_st.user_message}")
            else:
                _sys.add(f"{_spec_label} became unavailable: {_st.user_message}")

            # 聊天区（给用户看）：只有【不可用】才出卡，degraded 只进监控面板。
            # 用户的论证："致命报错就算不看 cmd，只看 UI 也一定能感知到不对劲；
            # 但降级如果不做，不靠 cmd 你可能一辈子都发现不了。"——所以降级要做，
            # 但落点是让监控卡说真话，不是往聊天区塞。
            if _t.kind in (Transition.OPENED, Transition.UPDATED) and \
               _st.status == Status.UNAVAILABLE and _st.presented_at is None:
                if _h.mark_presented(_st.capability, _st.generation):
                    _to_card.append(_st)

        # 多个组件同时失败时归并成一张卡，不连发三到十张故障卡片
        if _to_card:
            _to_card.sort(key=lambda s: Severity.rank(s.severity), reverse=True)
            if len(_to_card) == 1:
                _s = _to_card[0]
                self.emit_chat(
                    category="fault",
                    title=f"{_s.snapshot().get('label', _s.capability)} 不可用",
                    lines=[_s.user_message],
                    hints=[_s.recovery_hint],
                    dedupe_key=_s.fingerprint,
                )
            else:
                self.emit_chat(
                    category="fault",
                    title=f"检测到 {len(_to_card)} 项能力不可用",
                    lines=[f"{s.snapshot().get('label', s.capability)}：{s.user_message}"
                           for s in _to_card],
                    hints=[s.recovery_hint for s in _to_card],
                    dedupe_key="|".join(s.fingerprint for s in _to_card),
                )

        try:
            self._refresh_monitor_health()
        except Exception as e:
            logger.debug(f"[Health] 监控卡刷新跳过: {e}")

    # 监控卡的配色档位。前三档是【可用性】，后三档是【活动状态】——
    # 可用性档一旦成立就压过活动状态档，这是的落点。
    _CARD_STYLE = {
        "FAULT":      ("var(--nano-danger)", "FAULT"),
        "DEGRADED":   ("var(--nano-warn)", "DEGRADED"),
        "RECOVERING": ("var(--nano-info)", "REPAIRING"),
        "HIT":        ("var(--nano-amber)", "HIT"),
        "FULL_HIT":   ("var(--nano-ok)", "HIT"),
        "IDLE":       ("var(--nano-fg-mute)", "IDLE"),
    }

    def _card_is_faulted(self, card_key: str) -> bool:
        """这张卡当前有没有可用性问题。活动态写入前必须先问一句，
        否则会把刚画上的 FAULT 抹回 IDLE。"""
        try:
            from core.health import get_health
            return get_health().card_status(card_key) is not None
        except Exception:
            return False

    def _paint_card(self, dot, lbl, level: str, text: str = ""):
        _color, _default = self._CARD_STYLE.get(level, self._CARD_STYLE["IDLE"])
        try:
            if dot:
                dot.style(f'width:6px; height:6px; border-radius:50%; background:{_color}; flex-shrink:0;')
            if lbl:
                lbl.set_text(text or _default)
                lbl.style(f'font-size:var(--nano-fs-sm); color:{_color}; font-weight:500;')
        except Exception:
            pass

    def _refresh_context_card(self):
        """刷新「上下文」显示 —— 监控卡 **和** 输入框右下角那个圆环。

        ⚠️ **两个控件必须在同一处刷新。** 它们显示的是同一个数，
           分开刷新就迟早会出现「抽屉里 62%、圆环上 47%」——
           📌 **两个显示同一件事的控件，一旦各自取数、各自刷新，就会互相打脸。**
        

        ⚠️ 三条纪律，都写在 `core/context/budget.py` 里，这里只是消费：
          · 显示**占窗口的百分比**，不显示绝对 token 数（跨模型不可比）
          · 量不到显示 `--`，**不显示 0%**（📌 一个"我不知道"渲染成 0% 会被当成真数据）
          · 残差看门狗报 DEGRADED 时**明说不可信**，而不是继续给一个数
        """
        lbl = getattr(self, "ctx_lbl", None)
        if lbl is None:
            return
        try:
            from core.context.budget import snapshot as _bs
            s = _bs(self.provider.target_model)
            if s.get("degraded"):
                lbl.set_text("失准")
                lbl.style('font-size:var(--nano-fs-sm); color:var(--nano-danger); font-weight:500;')
                return
            if not s.get("known"):
                lbl.set_text("--")
                lbl.style('font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); font-weight:500;')
                return
            _color = {"ok": "var(--nano-ok)", "notice": "var(--nano-fg-soft)",
                      "high": "var(--nano-amber)", "critical": "var(--nano-danger)"}[s["level"]]
            # ⭐ `~` = 这是上次量到的值（本次运行还没量过）。
            #    📌 「Nano 是连续的，打开→关闭→再打开这个数没有理由变」——
            #    所以启动时**不显示 `--`**，显示上次那个数，只是标明它是估的。
            _pfx = "~" if s.get("estimated") else ""
            lbl.set_text(f"{_pfx}{int(s['ratio'] * 100)}%")
            lbl.style(f'font-size:var(--nano-fs-sm); color:{_color}; font-weight:500;')
        except Exception:
            pass
        finally:
            self._refresh_context_ring()

    # ── 上下文圆环（输入框右下角，形态参考 Claude Code）───────────────
    #
    # ⚠️ 它与监控卡那张「上下文」是**同一个数**（都读 `budget.snapshot`），
    #    只是一个在抽屉里、一个在手边。📌 **不许各算各的** ——
    #    两个显示同一件事的控件一旦各自取数，迟早会在某个时刻互相打脸。
    # ⚠️ Nano **没有 plan limit**，所以这个环只说一件事：这段对话占了窗口多少。
    #    不做成 Claude Code 那种「三条进度条」——那是它的计费结构，不是我们的。
    _RING_COLORS = {"ok": "var(--nano-ok)", "notice": "var(--nano-info)",
                    "high": "var(--nano-info)", "critical": "var(--nano-danger)"}

    def _context_ring_svg(self, s: dict) -> str:
        """画环。⚠️ 量不到 / 失准 → **空心灰环**，不是 0%（同监控卡那条纪律）。"""
        _r, _c = 7.0, 2.0                      # 半径 / 线宽
        _circ = 2 * 3.141592653589793 * _r
        _known = bool(s.get("known")) and not s.get("degraded")
        _ratio = min(1.0, max(0.0, float(s.get("ratio") or 0.0))) if _known else 0.0
        _col = self._RING_COLORS.get(s.get("level", "ok"), "var(--nano-ok)") if _known else "var(--nano-faint)"
        _dash = f"{_circ * _ratio:.2f} {_circ:.2f}"
        return (
            f'<svg width="18" height="18" viewBox="0 0 18 18" style="display:block;">'
            f'<circle cx="9" cy="9" r="{_r}" fill="none" stroke="var(--nano-line)" stroke-width="{_c}"/>'
            + (f'<circle cx="9" cy="9" r="{_r}" fill="none" stroke="{_col}" '
               f'stroke-width="{_c}" stroke-dasharray="{_dash}" stroke-linecap="round" '
               f'transform="rotate(-90 9 9)"/>' if _known and _ratio > 0.004 else "")
            + '</svg>'
        )

    def _refresh_context_ring(self):
        """重画圆环。**永不抛** —— 观测层。"""
        ring = getattr(self, "_ctx_ring", None)
        if ring is None:
            return
        try:
            from core.context.budget import snapshot as _bs
            s = _bs(self.provider.target_model)
            ring.set_content(self._context_ring_svg(s))
            # ⚠️ **不加 tooltip**（2026-08-14：遮挡）——
            #    圆环点开就是那个面板，面板第一行就写着「上下文」。
            #    📌 一个点一下就能看到全部内容的控件，不需要悬浮提示；
            #       悬浮提示反而会挡住它自己要展示的东西。
        except Exception:
            pass

    def _show_context_popover(self):
        """点圆环 → 上拉一个只讲上下文的小面板（已定形态，参考 Claude Code）。

        内容只有三样：**左上「上下文」/ 右上 `已用 / 窗口（百分比）` / 一条填充条**。
        ⚠️ 刻意不写解释句 —— 「保持简洁」。
        📌 一个每天要瞄好几眼的指示器，多一行字就多一次阅读成本。

        ⚠️ 这里**报绝对 token 数是可以的**，而 `pressure_block`（注给模型的那段）
           **不许报** —— 两者不矛盾：
           📌 **让用户看一个数，和让模型据此改变行为，是两个门槛。**
           用户会自己判断这个数有多准；模型会把它当成精确事实引用。
        ⭐ 估算态仍带一个 `~` 前缀（本次运行还没量过）—— 一个字符，不占地方，
           但那句"这不是刚量的"没有被吞掉。
        """
        box = getattr(self, "_ctx_menu_box", None)
        menu = getattr(self, "_ctx_menu", None)
        if box is None or menu is None:
            return
        try:
            from core.context.budget import snapshot as _bs
            s = _bs(self.provider.target_model)
        except Exception:
            return
        _known = bool(s.get("known")) and not s.get("degraded")
        _ratio = min(1.0, max(0.0, float(s.get("ratio") or 0.0))) if _known else 0.0
        _col = self._RING_COLORS.get(s.get("level", "ok"), "var(--nano-ok)") if _known else "var(--nano-faint)"
        _pfx = "~" if s.get("estimated") else ""
        if s.get("degraded"):
            _right = "失准"
        elif _known:
            # ⚠️ 复用既有的 `_fmt_tokens`，不另造一个格式化函数
            #    —— 📌 两个格式化函数迟早会在某个量级上不一致。
            _right = (f"{_pfx}{_fmt_tokens(int(s['used']))} / {_fmt_tokens(int(s['window']))}"
                      f" ({int(_ratio * 100)}%)")
        else:
            _right = "--"

        box.clear()
        with box:
            with ui.column().style('padding:10px 12px; gap:7px; min-width:230px;'):
                with ui.row().classes('w-full items-center justify-between no-wrap'):
                    ui.label('上下文').style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
                    ui.label(_right).style(
                        f'font-size:var(--nano-fs-sm); color:{_col}; font-weight:500; white-space:nowrap;')
                ui.html(
                    # ⚠️ 槽色跟着面板底色一起提 —— 面板从 var(--nano-panel) 提到 var(--nano-panel-2) 之后，
                    #    原来的槽色 var(--nano-border) 和它几乎同亮度，条子会糊住看不清。
                    #    📌 改了容器的底色，容器里那些"靠对比说话"的元素都得跟着重算。
                    f'<div style="width:100%; height:5px; border-radius:999px; '
                    f'background:var(--nano-line); overflow:hidden;">'
                    f'<div style="width:{_ratio * 100:.1f}%; height:100%; background:{_col}; '
                    f'border-radius:999px; transition:width .25s;"></div></div>'
                ).style('width:100%;')
        menu.open()

    def _refresh_monitor_health(self):
        """按 HealthRegistry 重绘监控卡。可用性优先——故障态覆盖活动态显示。

        为什么不新增 UI 区域而是让现有卡片说真话（用户定的）：致命报错就算不看
        cmd、只看 UI 也一定能感知到不对劲；但降级如果不做，不靠 cmd 可能一辈子发现不了。
        所以降级要做，落点就是这几张卡。
        """
        try:
            from core.health import get_health, get_capability_spec, Status
        except Exception:
            return
        _h = get_health()

        def _level_of(card_key: str) -> tuple[str, str]:
            _st = _h.card_status(card_key)
            if _st is None:
                return "", ""
            if _st.status == Status.UNAVAILABLE:
                return "FAULT", ""
            if _st.status == Status.RECOVERING:
                return "RECOVERING", ""
            return "DEGRADED", ""

        # 知识库 / 全文加载：有故障就压过 HIT/IDLE；没故障则保留原有活动态不动
        for _card, _dot, _lbl in (
            ("rag", getattr(self, "rag_dot", None), self.rag_lbl),
            ("full_file", getattr(self, "full_file_dot", None), self.full_file_lbl),
            ("net", getattr(self, "net_dot", None), self.net_lbl),
        ):
            _lv, _txt = _level_of(_card)
            if _lv:
                self._paint_card(_dot, _lbl, _lv, _txt)

        # 环境卡：收容所有没有专属卡片的问题项（Tesseract 缺失这类）
        def _is_orphan(cap: str) -> bool:
            _spec = get_capability_spec(cap)
            return _spec is None or _spec.monitor_card == "environment"

        _orphan = [s for s in _h.problems() if _is_orphan(s.capability)]
        _env_dot = getattr(self, "env_dot", None)
        _env_lbl = getattr(self, "env_lbl", None)
        if not _orphan:
            try:
                if _env_dot:
                    _env_dot.style('width:6px; height:6px; border-radius:50%; background:var(--nano-ok); flex-shrink:0;')
                if _env_lbl:
                    _env_lbl.set_text('OK')
                    _env_lbl.style('font-size:var(--nano-fs-sm); color:var(--nano-ok); font-weight:500;')
            except Exception:
                pass
        else:
            _worst = "FAULT" if any(s.status == Status.UNAVAILABLE for s in _orphan) else "DEGRADED"
            self._paint_card(_env_dot, _env_lbl, _worst, f"{len(_orphan)} 项降级")

    async def _capability_probe_tick(self):
        """跑到点的能力探针，恢复的能力会通过的转移队列通知用户。

        探针本身是同步且廉价的（见 rag.py 里那几个实现），所以直接在这里跑；
        真要出现慢探针，应该改探针，而不是把这里挪到线程池——
        慢探针在任何位置都是错的。
        """
        try:
            from core.health import get_health, get_capability_spec
            recovered = get_health().run_due_probes()
        except Exception as e:
            logger.debug(f"[Health] 探针轮询失败（忽略）: {e}")
            return
        for cap in recovered:
            try:
                spec = get_capability_spec(cap)
                label = spec.label if spec else cap
                from core.health import get_system_events
                get_system_events().add(f"Capability recovered: {label}.")
                logger.info(f"[Health] 探针恢复：{label}")
            except Exception:
                pass

    async def _budget_health_tick(self):
        """把预算状态同步进 HealthRegistry，并让顶部用量条跟着走。

        存在的理由是 留下的恢复检测死锁：report_ok 只在能力【被使用时】
        才有机会触发，而闸的作用恰恰是让它不能被使用。预算是这个死锁最干净的
        样本——硬上限挡住全部模型调用之后，没有任何代码路径会再去重算它。
        跨过 0 点用量归零，但故障卡片会一直挂着，直到用户重启进程。

        所以这里用一个外部时钟去推，不依赖任何业务路径。
        """
        try:
            from core.usage import sync_budget_health
            sync_budget_health()
        except Exception as e:
            logger.debug(f"[Budget] 状态同步失败（忽略）: {e}")
            return
        # 顺手刷新顶部那条用量警示（原来只在发消息/改配置时更新，
        # 跨天或后台消耗导致的变化看不见）
        try:
            self._update_cost_warning()
        except Exception:
            pass

    async def _startup_present_unsent(self):
        """关软件时还排在队列里的用户消息 —— **呈现，不执行**。

        ═══ 2026-08-24 定下来的 ═══
            inbox 的立意：用户的话永不丢
            关软件那条：关闭软件 = 用户默认放弃这次协同（一律 TERMINAL）
                   ├─ 重新【呈现】→ 两条都满足 ✅
                   └─ 自动【执行】→ 违反后一条 ❌
        📌 **「不丢」和「替用户做决定」是两件事。**

        ═══ 三个刻意的选择 ═══
        ⚠️ **呈现完必须丢弃。** 留着 PENDING 的话，下一次 `_drain_inbox` 会把它
           捡起来真的执行 —— 那正是被否掉的那一支。
           📌 **「不执行」不是靠没人去执行它，是靠它不再处于可被执行的状态。**
        ⚠️ 只走 `add_ui_only_record`（用户看得见、模型看不见）——
           📌 一旦进了模型的上下文，「呈现」和「执行」的界限就没了：
              模型看见一条没人处理的用户请求，它会去做。
           ⭐ 复用 前一半刚建的那条通道，不新增轴。
        ⚠️ `WAKE_INTENT` 不呈现（只丢弃）：那不是用户打的字，是系统的唤醒信号；
           「上个进程有没干完的活」由 `_startup_resume_offer` 负责问。
           📌 两条路各答各的问题，别让一件事在两个地方说两遍。

        ⚠️ 整段吞异常：它是补一条历史，不该有能力挡住启动。
        """
        try:
            from core.runtime import inbox as _ib
            items = _ib.list_unfinished(limit=50)
        except Exception as e:
            logger.warning(f"[L14] 读未处理队列失败（跳过）: {e}")
            return
        if not items:
            return
        import json as _j
        _shown = 0
        for it in items:
            try:
                if getattr(it, "kind", "") != _ib.ItemKind.USER_MESSAGE:
                    _ib.discard(it.item_id, "重启后丢弃（非用户消息）")
                    continue
                _d = it.detail or {}
                _payload = _j.dumps({"text": it.body or "",
                                     "had_image": bool(_d.get("had_image"))},
                                    ensure_ascii=False)
                # ⭐ 先落账本（下次重启还能重放），再画到屏幕上。
                #    📌 顺序不能反：先画后存的话，画完崩了这条就真没了 ——
                #       同 那条「先产生可召回内容 → 持久化 → 才 commit」。
                try:
                    self.agent.memory.add_ui_only_record(_payload, "inbox_unsent")
                except Exception as _e:
                    logger.warning(f"[L14] 落账本失败（仍然画出来）: {_e}")
                with self._ui_scope():
                    with self.chat_container:
                        render_unsent_user_card(_payload)
                _ib.discard(it.item_id, "重启后已呈现给用户")
                _shown += 1
            except Exception as e:
                logger.warning(f"[L14] 呈现 {getattr(it, 'item_id', '?')} 失败: {e}")
        if _shown:
            logger.info(f"[L14] 重启后呈现了 {_shown} 条未处理的消息（未执行，已出队）")


    async def _startup_resume_offer(self):
        """重启后：上个进程留下的活，由 Nano **主动开口**问一句。

        🔴 它绕开的是一个**分不开的三岔口**：运行中崩溃（该接）/ 非运行中崩溃 /
           正常关闭（都不用管）。本机上分不清 —— 见 `reconciler` 里那段留痕。
           而第一版「按关闭 = 放弃，一律不提」只是把一个不可靠的推断换成了另一个
           （**误触了关闭按钮呢？**）。
        ⭐ 正解是**不推断**：把事实说出来，让用户答。
           📌 **温和提醒本身是无害的**（只是一段话，不是强制继续）——
              「问错了」代价接近零，「猜错了」会丢掉用户真正想接着做的事。
              **两边代价不对称时，往代价小的那边倒。**

        ⚠️ **新气泡**（走 `_proactive_push`）—— 「这是关闭后说的新一句话，
           合并老气泡会非常奇怪」。与本次运行内那条提醒（挂进下一轮）规则相反，
           因为它们在对话里的位置不同：一个是重新见面的第一句，一个是话说到一半顺带提。

        ⚠️ **气泡里的话由模型生成**（早先第五条：文字出现在哪里，
           决定它是不是「Nano 在说话」）。系统只交出事实。
        ⚠️ **生成失败就什么都不说** —— 同 `proactive/speaker.py` 刚拆掉的那个兜底：
           API 调不通意味着 Nano 此刻恰恰不能思考，这时蹦一句写死的话是在谎报它的状态。
        """
        details = list(_STARTUP_INTERRUPTED or [])
        if not details:
            return                      # 没有可问的 → 一个字都不说
        try:
            from core.runtime.scheduler import startup_resume_notice
            facts = startup_resume_notice(details)
            if not facts:
                return                  # 都说不出「是什么」→ 不提（提了也是噪音）
            from core.i18n import language_clause
            content, _ = await self.agent.provider.chat_without_tools(
                context=[{"role": "user", "content": facts}],
                system_guide=("You are Nano. Speak in your own voice, "
                              "1-2 short sentences.\n"
                              + language_clause("your line") + "\n"),
            )
            content = (content or "").strip()
            if not content:
                return
            await self._proactive_push(content)
            logger.info(f"[B1] 重启后已就 {len(details)} 件未完成的活开口询问")
        except Exception as e:
            # 📌 fail-safe 朝「少说一句」：宁可不问，也不拿写死的话顶上。
            logger.warning(f"[B1] 重启后询问生成失败 → 本次不开口: {e}")

    async def _crash_journal_tick(self):
        """启动后展示上一个进程的崩溃留痕（一次性）。

        进程级登记表只能处理"异常被 Python 捕获、进程还活着"。它处理不了原生库崩溃、
        segfault、os._exit、启动早期 import 终止——而那恰是本项目的已知崩溃形态
        （内部诊断记录：torch+chromadb 原生堆损坏 → 随机 segfault）。
        那些靠 write-ahead breadcrumb 留痕，在这里读出来。
        """
        try:
            from core import crash_journal
            _recs = crash_journal.startup_scan()
        except Exception as e:
            logger.debug(f"[CrashJournal] 启动扫描跳过: {e}")
            return
        if not _recs:
            return
        self.emit_chat(
            category="fault",
            title="上次运行没有正常退出",
            lines=[r.get("summary", "") for r in _recs[:5]],
            hints=["这是上一个进程的记录，当前这次的能力状态以本次重新探测为准。"],
            dedupe_key="crash:" + ",".join(r.get("id", "") for r in _recs[:5]),
        )
        try:
            from core.health import get_system_events
            for r in _recs[:5]:
                get_system_events().add(r.get("summary", ""))
            crash_journal.mark_presented([r.get("id", "") for r in _recs])
        except Exception:
            pass

    async def _append_zombie_bubble(self, meta: dict, result: str):
        """后台任务完成通知（**没有**关联挂起的那一支）。

        两条路径必须保持语义区分（外部评审推演出来、且已回代码核实）：
          suspension_ref 存在 → 只唤醒，不单独发完成通知（否则会同时出现"后台完成"
                                和"唤醒流程生成的新回答"两条，重复）
          suspension_ref 不存在 → 发一条独立的 background_result
        分流在 _run_bg_task 里，这里只负责后者。

        改动：从"追加进上一条回复 + 改写它的状态标签"改成独立块。
        原实现会把上一条的 "8.2s · 2.2K tok" 覆写成 "Nano · N s"，把那一轮的
        用量统计抹掉；而且冷启动时 inner_col 为 None 会直接静默丢弃。
        """
        display = meta.get("display", "后台任务")

        # 用 Claude 把原始结果润色成自然语言，失败则 fallback 原文
        from core.i18n import language_clause as _lc_bg
        _body = result or f"{display}已完成。"
        try:
            _polished, _ = await self.agent.provider.chat_without_tools(
                context=[{"role": "user", "content":
                    f"You just completed a background task ({display}). The execution result is below:\n\n{result}\n\n"
                    f"Tell the user the result in 1-2 natural sentences. "
                    # ⚠️ 同上：语言由 language_clause() 定，这里不自己判。
                    f"{_lc_bg('your reply')} "
                    f"Do not repeat the raw content. Do not say the task is completed. Keep Nano's usual brief style."
                }],
                system_guide="You are Nano, a desktop assistant. State the result directly, briefly, and naturally.",
            )
            if _polished and _polished.strip():
                _body = _polished.strip()
        except Exception:
            pass

        try:
            from core.health import get_system_events
            _elapsed = int(time.time() - meta.get("started_at", time.time()))
            get_system_events().add(
                f"Background task finished after {_elapsed}s: {display}")
        except Exception:
            pass
        self.emit_chat(category="speech", body=_body)

    def _refresh_takeover_bar(self):
        """重画接管状态条。**整体重画，不做增量。**

        📌 走的是 `projection.py` 定下的契约：**收到任何事件（甚至不看内容）
        → 触发一次 rebuild**。绝不能"根据事件里的 detail 增量改 UI" ——
        那样丢一个事件就永久错位。所以这里每次都从**权威状态**（活动租约）重算，
        不依赖"有没有收到通知"。

        ⚠️ 读失败按"没人占着"处理（把条藏起来）：
        观测手段坏了不该在界面上留一条永久的假警报。
        """
        bar = getattr(self, "_takeover_bar", None)
        if bar is None:
            return
        holder = None
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import oslease as _ol
            cur = _ol.current_activity(get_kernel())
            if cur is not None and cur.holder == _ol.Holder.USER:
                holder = cur
        except Exception:
            holder = None
        # ⚠️⚠️ **只在状态翻转时打日志**，1 秒一跳会把 cmd 淹掉。
        #    记的是「接管状态条什么时候真的变了」—— 和 `[Takeover]` 那条的时间差
        #    就是**事件循环被堵住的时长**，也就是"为什么不瞬发"的答案。
        # 📌 判据：**"我以为它 1 秒重画一次"不是事实，timer 只在事件循环空闲时才跑。**
        #    这条时间差是唯一能量出它的东西。
        _shown_now = holder is not None
        if getattr(self, "_takeover_bar_shown", None) != _shown_now:
            self._takeover_bar_shown = _shown_now
            if _shown_now:
                logger.info(f"[TakeoverBar] 接管状态条**出现** —— 持有者={holder.holder} "
                            f"reason={holder.reason!r} 剩余={holder.held_until - __import__('time').time():.1f}s")
            else:
                logger.info("[TakeoverBar] 接管状态条**消失** —— 没有用户持有")

        if holder is None:
            bar.style('display:none;')
            return
        bar.style(
            'display:flex; width:100%; align-items:center; gap:8px; '
            'padding:6px 12px; border-radius:8px; margin-bottom:4px; flex-shrink:0; '
            'background:rgba(var(--nano-info-rgb),0.10); '
            'border-bottom:1px solid rgba(var(--nano-info-rgb),0.30);')
        if getattr(self, "_takeover_lbl", None) is not None:
            self._takeover_lbl.set_text(self._takeover_text(holder))

    #: 用户"停手"到底停够多久才算真停下。⚠️ **这 2 秒刻意不显示** ——
    #: 人打字的自然间隙经常超过 0.2 秒，显示的话接管状态条会在两个文案之间疯狂闪。
    #: 📌 它的作用是**让"停下了"这件事有意义**，不是给用户看的一个计时。
    _TAKEOVER_SETTLE_SEC = 2.0

    def _takeover_text(self, holder) -> str:
        """按「Nano 停没停」×「用户停手多久」选文案。

        ⭐ **固定中文在这里是正当的，不需要改成自然语言。**
        「不许写死用户可见文案」那条原则要防的是**人格分裂**，而人格分裂只发生在
        **用户以为那是 Nano 在说话**的地方 —— 也就是**对话气泡**。
        这条接管状态条在**卡片区**，用户天然把它读成系统在陈述状态；给它套人格模板反而怪。
        📌 **判断一句文案该不该走人格，先问「用户会不会以为这是 Nano 在对我说话」。**
        （这已作为固定文案豁免的**第五条**写进早先的设计。）
        ⚠️ 原先这里留过「临时固定文案、正式要换异步自然语言」的注记 ——
        **那是多余的**，接管状态条本来就该是系统通知。

        ═══ 六格穷举 → 三句话═══

        | # | Nano | 用户 | 文案 |
        |---|---|---|---|
        | 1 | 还在收手 | 刚动过 | 已让出控制 —— 我手上这一步做完就停 |
        | 2 | 已停下   | 刚动过 | 已暂停控制 —— 你停手后我自动继续 |
        | 3 | 已停下   | 停手 ≥2s | N 秒内你不再操作，我会继续行动 |
        | 4 | 还在收手 | 停手 ≥2s | 同 3（**共用**）|
        | 5 | 已停下   | 租约到期 | 接管状态条消失 |
        | 6 | 还在收手 | 租约到期 | 接管状态条消失（= "挂起被取消"，它从没真停过）|

        ⭐⭐ **3 和 4 共用一句**：倒计时文案说的是**条件 + 后果**，
        不声称"Nano 停了"（第 4 格里它确实没停），所以两格都为真；
        而"Nano 到底停没停"在那一刻**对用户既不可见、也不影响他做什么**。

        📌📌 **穷举状态是为了保证每句话都为真，不是为了每个状态都配一句独有的话。
        能共用就该共用 —— 状态数和文案数不必一一对应。**
        ⚠️ Claude 第一版正因为想给每格配一句，把第 4 格硬塞成了"接管状态条消失"，
        被 用户当场指出那一格没覆盖到。

        ⚠️ **倒计时从 `held_until` 推导，绝不自己数。** 用户再动一下，
        `held_until` 会跳到 now+20，于是**自动**弹回"刚动过"那一档 ——
        不需要额外记"上次停手时间"，也不可能与真实状态漂移。
        📌 同 `projection.py` 的契约：**每跳都从权威重算，永远不自己维护计数器。**
        """
        try:
            from core.proactive.takeover import USER_HOLD_SEC as _HOLD
            from core.runtime import oslease as _ol_t
            _parked = _ol_t.is_parked()
        except Exception:
            # ⚠️ 这个兜底值**必须跟 `USER_HOLD_SEC` 一起改**（2026-08-26 差点漏）。
            #    📌 一个「读不出来就用默认值」的兜底，如果不跟着它兜的那个值走，
            #       它就是一份**会静默说谎的备份**：真值 12，接管状态条按 20 倒数。
            _HOLD, _parked = 12.0, True
        _remain = max(0.0, float(holder.held_until or 0) - time.time())
        # 剩余 > (HOLD - 2) ⟺ 距上次操作不足 2 秒 ⟺ 用户还在动
        if _remain > _HOLD - self._TAKEOVER_SETTLE_SEC:
            return ('已暂停控制 —— 你停手后我自动继续' if _parked
                    else '已让出控制 —— 我手上这一步做完就停')
        # 停手 ≥2s：倒计时。⚠️ 向上取整，免得显示 0 秒却还没醒。
        import math as _math
        return f'{max(1, int(_math.ceil(_remain)))} 秒内你不再操作，我会继续行动'

    def _update_cost_warning(self):
        """根据当日用量刷新顶部警告条。"""
        if not self._cost_warning_bar:
            return
        status = usage_tracker.cap_status()
        cfg = usage_tracker.load_config()
        cost = usage_tracker.today_cost()
        if status == "ok":
            self._cost_warning_bar.style('display:none;')
        elif status == "soft":
            self._cost_warning_bar.style(
                'display:flex; background:rgba(var(--nano-warn-rgb),0.12); '
                'border-bottom:1px solid rgba(var(--nano-warn-rgb),0.3);'
            )
            if self._cost_warning_lbl:
                self._cost_warning_lbl.set_text(
                    f'今日用量 {_cur()}{cost:.2f} 已超软上限 {_cur()}{cfg["soft_cap_usd"]:.2f}，接近限额'
                )
        else:
            self._cost_warning_bar.style(
                'display:flex; background:rgba(var(--nano-danger-rgb),0.10); '
                'border-bottom:1px solid rgba(var(--nano-danger-rgb),0.28);'
            )
            if self._cost_warning_lbl:
                self._cost_warning_lbl.set_text(
                    f'今日用量 {_cur()}{cost:.2f} 已达硬上限 {_cur()}{cfg["hard_cap_usd"]:.2f}，已停止发送'
                )

    def _build_settings_profile(self, on_done=None):
        """个人信息的内容区。**独立弹窗和设置面板共用这一份。**

        🔴 它一开始被放进了通用页的「一行 + 编辑」—— 用户一眼看出不对：
           **内容最多的那个，被放进了最小的容器**（261 行 vs 一行）。
        📌 更根本的问题是：通用页当时已经有三个「点开另一个窗」的按钮 ——
           **一页里全是开窗按钮，那这页就是个菜单，不是设置页**，
           等于把下拉菜单原样搬进来、只换了个地方。

        ⚠️ 不含 `with self._ui_scope():` —— 那是**壳**的一部分，
           两个调用方（独立弹窗 / 设置面板）自己都已经在 scope 里了。
        ⚠️ `on_done`：「取消」只在独立弹窗里有意义（面板有自己的关闭按钮），
           所以它只在传了 on_done 时出现；「保存」两边都留 ——
           📌 姓名生日这类**填错了不当场露馅**，验证需要一个「我填完了」的动作。
        """
        import json as _json
        from pathlib import Path as _Path

        _REGION_PATH = _Path("data/china_regions_city.json")
        _PROFILE_PATH = _Path("data/user_profile.json")

        try:
            _regions = _json.loads(_REGION_PATH.read_text(encoding="utf-8"))
        except Exception:
            _regions = []

        _province_map = {p["name"]: p for p in _regions}
        _province_names = list(_province_map.keys())

        try:
            _profile = _json.loads(_PROFILE_PATH.read_text(encoding="utf-8")) if _PROFILE_PATH.exists() else {}
        except Exception:
            _profile = {}

        _IDENTITY_OPTIONS = [
            "学生", "上班族", "自由职业", "创业者 / 管理者",
            "程序员 / 技术人员", "设计 / 内容创作者",
            "销售 / 市场", "研究 / 学术", "其他",
        ]
        _MONTHS = [f"{i:02d} 月" for i in range(1, 13)]
        _DAYS   = [f"{i:02d} 日" for i in range(1, 32)]

        _saved_region = _profile.get("region", {})
        _state = {
            "province_code": _saved_region.get("province_code"),
            "province_name": _saved_region.get("province_name"),
            "city_code":     _saved_region.get("city_code"),
            "city_name":     _saved_region.get("city_name"),
        }

        _bday = _profile.get("birthday", "")
        _init_month = f"{int(_bday.split('-')[0]):02d} 月" if _bday and "-" in _bday else None
        _init_day   = f"{int(_bday.split('-')[1]):02d} 日" if _bday and "-" in _bday else None

        # 终端风：下拉 popup 用深色底 + 浅色字（全局 .q-item__label 已是 var(--nano-fg)），
        # 之前是近白色残留导致浅字浅底看不清。
        _popup_bg = (
            "background:var(--nano-panel-2) !important;"
            "border:1px solid rgba(var(--nano-amber-rgb), 0.18);"
            "border-radius:10px;"
            "box-shadow:0 6px 20px rgba(var(--nano-ink-rgb), 0.45);"
            "color:var(--nano-fg) !important;"
        )
        _sel_props = "outlined dense popup-content-style='" + _popup_bg + "'"

        _label_style = 'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft); margin-bottom:4px;'
        _hint_style  = 'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); margin-top:3px;'
        _clear_style = 'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft); cursor:pointer; padding:0 4px; line-height:1;'

        # ── 称呼 ──────────────────────────────────────────────────────────
        with ui.row().classes('items-center justify-between').style('width:100%;'):
            ui.label('我该怎么称呼你？').style(_label_style)
        _nickname_inp = ui.input(placeholder='输入你的昵称').props('outlined dense maxlength=16').style(
            'width:100%; font-size:var(--nano-fs-md);'
        )
        _nickname_inp.value = _profile.get("nickname", "")
        ui.label('Nano 偶尔会直接叫你的名字。').style(_hint_style)

        ui.separator().style('margin:14px 0;')

        # ── 生日 ──────────────────────────────────────────────────────────
        with ui.row().classes('items-center justify-between').style('width:100%;'):
            ui.label('你的生日').style(_label_style)
            _bday_clear = ui.label('清空').style(_clear_style)
        with ui.row().classes('gap-3').style('width:100%;'):
            _month_sel = ui.select(
                options=_MONTHS, label='月', with_input=False
            ).props(_sel_props).style('flex:1; font-size:var(--nano-fs-md);')
            _day_sel = ui.select(
                options=_DAYS, label='日', with_input=False
            ).props(_sel_props).style('flex:1; font-size:var(--nano-fs-md);')
            _month_sel.value = _init_month
            _day_sel.value   = _init_day

        def _clear_bday():
            _month_sel.value = None
            _day_sel.value   = None
        _bday_clear.on('click', _clear_bday)
        ui.label('有些日子，值得被记住。').style(_hint_style)

        ui.separator().style('margin:14px 0;')

        # ── 地区 ──────────────────────────────────────────────────────────
        with ui.row().classes('items-center justify-between').style('width:100%;'):
            ui.label('你主要在哪个城市？').style(_label_style)
            _region_clear = ui.label('清空').style(_clear_style)
        with ui.row().classes('gap-3').style('width:100%;'):
            _prov_sel = ui.select(
                options=_province_names, label='省 / 自治区 / 直辖市', with_input=True
            ).props(_sel_props).style('flex:1; font-size:var(--nano-fs-md);')
            _city_sel = ui.select(
                options=[], label='城市', with_input=True
            ).props(_sel_props).style('flex:1; font-size:var(--nano-fs-md);')

        if _state["province_name"]:
            _prov_sel.value = _state["province_name"]
            _p = _province_map.get(_state["province_name"])
            if _p:
                _city_sel.options = [c["name"] for c in _p.get("children", [])]
                _city_sel.value = _state["city_name"]

        _region_hint = ui.label(
            f'当前：{_state["province_name"]} · {_state["city_name"]}' if _state["city_name"]
            else ''
        ).style(_hint_style)

        def _on_prov(e):
            pname = e.value
            p = _province_map.get(pname)
            if not p:
                return
            children = p.get("children", [])
            _city_sel.options = [c["name"] for c in children]
            _state["province_code"] = p["code"]
            _state["province_name"] = pname
            _state["city_code"] = None
            _state["city_name"] = None
            if len(children) == 1 and children[0]["name"] == pname:
                _city_sel.value = children[0]["name"]
            else:
                _city_sel.value = None
            _city_sel.update()

        def _on_city(e):
            cname = e.value
            pname = _state.get("province_name")
            if not pname or not cname:
                return
            p = _province_map.get(pname)
            if not p:
                return
            city = next((c for c in p["children"] if c["name"] == cname), None)
            if city:
                _state["city_code"] = city["code"]
                _state["city_name"] = cname
                _region_hint.set_text(f'当前：{pname} · {cname}')

        def _clear_region():
            _prov_sel.value = None
            _city_sel.options = []
            _city_sel.value = None
            _city_sel.update()
            _state.update({"province_code": None, "province_name": None,
                           "city_code": None, "city_name": None})
            _region_hint.set_text('')

        _prov_sel.on_value_change(_on_prov)
        _city_sel.on_value_change(_on_city)
        _region_clear.on('click', _clear_region)

        ui.separator().style('margin:14px 0;')

        # ── 身份 ──────────────────────────────────────────────────────────
        with ui.row().classes('items-center justify-between').style('width:100%;'):
            ui.label('你现在更像是哪种身份？').style(_label_style)
            _id_clear = ui.label('清空').style(_clear_style)
        _id_sel = ui.select(
            options=_IDENTITY_OPTIONS, label='身份', with_input=False
        ).props(_sel_props).style('width:100%; font-size:var(--nano-fs-md);')
        _saved_id = _profile.get("identity", None)
        _id_sel.value = _saved_id if _saved_id in _IDENTITY_OPTIONS else (
            "其他" if _saved_id else None
        )
        # 其他（自填）输入框
        # ⚠️ `maxlength=24`：昵称那个输入框一直有 `maxlength=16`，而这个自填身份
        #    **没有任何上限** —— 2026-08-29 顺手抓到的。
        #    📌 同一类控件里，**有一个加了保护而另一个没有**，那多半不是有意的差别，
        #       只是写第二个的时候忘了。
        _id_custom = ui.input(placeholder='填写你的身份').props('outlined dense maxlength=24').style(
            'width:100%; font-size:var(--nano-fs-md); margin-top:6px;'
        )
        _id_custom.value = _saved_id if (_saved_id and _saved_id not in _IDENTITY_OPTIONS) else ""
        _id_custom.set_visibility(_id_sel.value == "其他")

        def _on_id_change(e):
            _id_custom.set_visibility(e.value == "其他")
        _id_sel.on_value_change(_on_id_change)

        def _clear_id():
            _id_sel.value = None
            _id_custom.value = ""
            _id_custom.set_visibility(False)
        _id_clear.on('click', _clear_id)

        ui.label('Nano 会用它理解你的工作语境。').style(_hint_style)

        ui.separator().style('margin:16px 0 12px;')

        # ── 底部按钮 ─────────────────────────────────────────────────────
        with ui.row().classes('justify-end gap-3').style('width:100%;'):
            # ⚠️ 「取消」只在**独立弹窗**里有意义 —— 设置面板有自己的关闭按钮。
            #    📌 一个能把外层容器关掉的按钮，不该由内容区提供。
            if on_done:
                ui.button('取消', on_click=on_done).props('flat').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-soft);'
                )
            def _save():
                _new = dict(_profile)
                nick = _nickname_inp.value.strip()
                if nick:
                    _new["nickname"] = nick
                else:
                    _new.pop("nickname", None)

                _m = _month_sel.value
                _d = _day_sel.value
                if _m and _d:
                    _new["birthday"] = f"{int(_m.split()[0]):02d}-{int(_d.split()[0]):02d}"
                else:
                    _new.pop("birthday", None)

                if _state["city_code"]:
                    _new["region"] = {
                        "country": "CN",
                        "province_code": _state["province_code"],
                        "province_name": _state["province_name"],
                        "city_code":     _state["city_code"],
                        "city_name":     _state["city_name"],
                        "display":       f'{_state["province_name"]} · {_state["city_name"]}',
                    }
                else:
                    _new.pop("region", None)

                _id = _id_sel.value
                if _id == "其他":
                    _custom = _id_custom.value.strip()
                    if _custom:
                        _new["identity"] = _custom
                    else:
                        _new.pop("identity", None)
                elif _id:
                    _new["identity"] = _id
                else:
                    _new.pop("identity", None)

                _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _PROFILE_PATH.write_text(
                    _json.dumps(_new, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._refresh_identity()   # 昵称变了 → 头栏 nano@昵称 同步

            # 🪦 原来这里有个「保存」按钮 —— 2026-08-29 定：去掉，改即时生效。
            #
            # ⭐ 先前把这一格归进了「填错了不当场露馅 ⇒ 需要一个『我填完了』的动作」，
            #    用户反驳：「这里似乎不存在非法保存，下拉菜单类的不可能非法，
            #    称呼和身份只是两个字符串」。这个判断是对的 ——
            #    📌 **「需要验证」的前提是「错了有后果」。**
            #       API Key 填错 → 整个 Nano 不能用；昵称填错 → 什么也不会发生。
            #       漏了这个前提，把判据套到了一个不适用的地方。
            # ⚠️ 也不再弹「已保存」：即时生效的界面里，每改一下弹一次是噪音。
            #    📌 成功是常态，不必每次都说。
            for _w in (_nickname_inp, _month_sel, _day_sel,
                       _prov_sel, _city_sel, _id_sel, _id_custom):
                _w.on_value_change(lambda e: _save())

    def _show_profile_dialog(self):
        """个人信息填写面板。"""
        import json as _json
        from pathlib import Path as _Path

        _REGION_PATH = _Path("data/china_regions_city.json")
        _PROFILE_PATH = _Path("data/user_profile.json")

        try:
            _regions = _json.loads(_REGION_PATH.read_text(encoding="utf-8"))
        except Exception:
            _regions = []

        _province_map = {p["name"]: p for p in _regions}
        _province_names = list(_province_map.keys())

        try:
            _profile = _json.loads(_PROFILE_PATH.read_text(encoding="utf-8")) if _PROFILE_PATH.exists() else {}
        except Exception:
            _profile = {}

        _IDENTITY_OPTIONS = [
            "学生", "上班族", "自由职业", "创业者 / 管理者",
            "程序员 / 技术人员", "设计 / 内容创作者",
            "销售 / 市场", "研究 / 学术", "其他",
        ]
        _MONTHS = [f"{i:02d} 月" for i in range(1, 13)]
        _DAYS   = [f"{i:02d} 日" for i in range(1, 32)]

        _saved_region = _profile.get("region", {})
        _state = {
            "province_code": _saved_region.get("province_code"),
            "province_name": _saved_region.get("province_name"),
            "city_code":     _saved_region.get("city_code"),
            "city_name":     _saved_region.get("city_name"),
        }

        _bday = _profile.get("birthday", "")
        _init_month = f"{int(_bday.split('-')[0]):02d} 月" if _bday and "-" in _bday else None
        _init_day   = f"{int(_bday.split('-')[1]):02d} 日" if _bday and "-" in _bday else None

        # 终端风：下拉 popup 用深色底 + 浅色字（全局 .q-item__label 已是 var(--nano-fg)），
        # 之前是近白色残留导致浅字浅底看不清。
        _popup_bg = (
            "background:var(--nano-panel-2) !important;"
            "border:1px solid rgba(var(--nano-amber-rgb), 0.18);"
            "border-radius:10px;"
            "box-shadow:0 6px 20px rgba(var(--nano-ink-rgb), 0.45);"
            "color:var(--nano-fg) !important;"
        )
        _sel_props = "outlined dense popup-content-style='" + _popup_bg + "'"

        with self._ui_scope():
            with ui.dialog().props('persistent').classes('q-pa-none') as dlg, \
                 ui.card().style(
                    'width:440px; max-width:95vw; border-radius:16px; '
                    'background:var(--nano-panel); padding:28px 28px 20px;'
                 ):
                ui.label('认识你一下').style(
                    'font-size:var(--nano-fs-3xl); font-weight:700; color:var(--nano-fg); letter-spacing:-0.02em;'
                )
                ui.label('这些信息只在本地保存，让 Nano 说话更贴近你。').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); margin-top:2px; margin-bottom:18px;'
                )

                self._build_settings_profile(on_done=dlg.close)

            dlg.open()

    def _refresh_model_select(self):
        """环境配置保存后刷新顶栏主模型下拉。

        🔴 2026-08-31：「从 Claude 换成 ds，上面那个下拉框永远是 Claude……
           甚至进阶配置都能正确同步变化，但那个主模型下拉框不行（重启后才会变化）。」
        📌 进阶配置能同步，是因为它**每次打开都重建**；下拉框是**建一次就留着**的
           ⇒ **「重建的」和「常驻的」要用不同的刷新方式**，
              不能指望前者的做法自动覆盖后者。
        """
        sel = getattr(self, "_model_select", None)
        if sel is None:
            return
        try:
            opts = self._vendor_model_options()
            cur = self.provider.target_model
            if cur not in opts:
                cur = next(iter(opts), "")
                self.provider.target_model = cur
            sel.set_options(opts, value=cur)
        except Exception as e:
            logger.warning(f"[Model] 刷新主模型下拉失败: {e}")

    def _refresh_relay_badge(self):
        """环境配置保存后刷新顶栏「中转」徽章显隐。"""
        lbl = getattr(self, '_relay_badge_label', None)
        if lbl:
            lbl.set_visibility(getattr(self.provider, 'is_relay', False))

    def _show_env_config_dialog(self):
        """环境配置：API Key / 中转地址 / 代理。保存后原地重建 provider，无需重启。"""
        import re as _re
        from pathlib import Path as _Path

        _ENV_PATH = _Path('.env')

        def _upsert_dotenv(path, updates):
            try:
                text = path.read_text(encoding='utf-8') if path.exists() else ''
            except Exception:
                text = ''
            raw_lines = text.splitlines(keepends=True)
            written = set()
            new_lines = []
            for raw in raw_lines:
                m = _re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', raw)
                if m and m.group(1) in updates:
                    key = m.group(1)
                    val = updates[key]
                    if val is None:
                        # 🔴 这里原来是把整行注释掉（`# KEY=值  # cleared`）——
                        #    于是"清除密钥"的真实结果是**密钥原样留在盘上**：
                        #    界面上那一栏空了，文件里还在。
                        #    ⚠️ 打包、抽纯净版、贴日志时它会被一起带出去。
                        #    📌 「清除」的语义是让它**不存在**，不是让它不生效。
                        pass          # 整行丢弃
                    else:
                        new_lines.append(f'{key}={val}\n')
                    written.add(key)
                else:
                    new_lines.append(raw if raw.endswith('\n') else raw + '\n')
            for key, val in updates.items():
                if key not in written and val is not None:
                    new_lines.append(f'{key}={val}\n')
            path.write_text(''.join(new_lines), encoding='utf-8')

        _cur_relay_url  = (os.environ.get('NANO_API_RELAY_BASE_URL') or '').strip()
        _cur_relay_key  = (os.environ.get('NANO_API_RELAY_API_KEY') or '').strip()
        _cur_direct_key = (os.environ.get('ANTHROPIC_API_KEY') or '').strip()
        _cur_proxy      = (os.environ.get('HTTP_PROXY') or '').strip()
        _cur_api_key    = _cur_relay_key or _cur_direct_key

        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as _dlg, \
                 ui.card().classes('nano-soft-field').style(
                     'width:460px; background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.2); '
                     'border-radius:16px; padding:0; overflow:hidden;'
                 ):
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom:1px solid rgba(var(--nano-line-rgb), 0.8); '
                    'background:var(--nano-panel); border-radius:16px 16px 0 0; flex-wrap:nowrap;'
                ):
                    with ui.row().classes('items-center gap-3 flex-1 min-w-0'):
                        ui.icon('dns').style('font-size:var(--nano-fs-4xl); color:var(--nano-fg-soft); flex-shrink:0;')
                        with ui.column().style('gap:2px; min-width:0;'):
                            ui.label('环境配置').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                            ui.label('修改后自动生效，无需重启').style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
                    ui.button(icon='close', on_click=_dlg.close).props('flat round dense').style(
                        'color:var(--nano-fg) !important;').classes('flex-shrink-0')

                with ui.column().style('width:100%; padding:20px 24px; gap:18px;'):
                    # ── 厂商（放最上面）───────────────────────────────
                    # 📌 它决定下面那把 key 该发到哪个地址 —— **逻辑上先于 key，
                    #    填写顺序也先于 key**。
                    # ⭐ 顺带告诉用户 Nano 目前支持哪几家 —— 省掉「填了个第三家的 key
                    #    然后一直连不上」这条弯路。
                    from core.models import vendors as _vendors, vendor_meta as _vmeta
                    _vlist = _vendors()
                    _vopts = {v: _vmeta(v)['label'] for v in _vlist}
                    _cur_vendor = (os.environ.get('NANO_API_VENDOR') or _vlist[0]).strip().lower()
                    if _cur_vendor not in _vopts:
                        _cur_vendor = _vlist[0]
                    with ui.column().style('gap:6px; width:100%;'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label('厂商').style(
                                'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg);')
                            ui.label('必选').style(
                                'font-size:var(--nano-fs-2xs); color:var(--nano-danger); '
                                'background:rgba(var(--nano-danger-rgb),0.12); '
                                'padding:1px 5px; border-radius:2px;')
                        _vendor_sel = ui.select(_vopts, value=_cur_vendor) \
                            .props('outlined dense options-dense '
                                   'popup-content-class=nano-select-popup') \
                            .classes('lang-select-field').style('width:100%;')
                        # 图标：**左图标 + 右厂商名**。
                        # 图标走 /vendors 静态路由（assets/vendors/*.png）。
                        # ⚠️ 两个 slot 都要给，缺一个就只有一半有图标：
                        #      option   下拉展开后的每一项
                        #      prepend  收起时框内那个（当前选中的）
                        _vicons = {v: _vmeta(v)['icon'] for v in _vlist}
                        _icon_map = ''.join(
                            "<img v-if=\"props.opt.label==='%s'\" src='%s' "
                            "style='width:18px;height:18px;border-radius:3px;flex-shrink:0;'>"
                            % (_vopts[v], _vicons[v])
                            for v in _vlist if _vicons.get(v))
                        # 🔴 比对用 **label** 不是 value —— NiceGUI 送到前端的
                        #    `opt.value` 是【整数下标】（见 choice_element.py:
                        #    `[{'value': index, 'label': option} for index, option in …]`），
                        #    真正的 key 只在 Python 侧维护。
                        #    📌 一个框架把你给的东西换了形状再往下传，只能读源码，猜不出来。
                        _vendor_sel.add_slot('option', f'''
                            <q-item v-bind="props.itemProps">
                              <q-item-section avatar style="min-width:26px;padding-right:8px;">
                                {_icon_map}
                              </q-item-section>
                              <q-item-section>
                                <q-item-label>{{{{ props.opt.label }}}}</q-item-label>
                              </q-item-section>
                            </q-item>
                        ''')
                        # 选中态也走 slot（而不是 prepend + JS 改 src）——
                        # ⚠️ 那样会出现"图标是鲸鱼、标签写 Anthropic"的错位：
                        #    图标是建的时候钉死的，靠 JS 事后同步，一旦错开就对不上。
                        #    📌 能让框架自己重算的，别自己去改 DOM。
                        _vendor_sel.add_slot('selected-item', f'''
                            <div style="display:flex;align-items:center;gap:8px;min-width:0;">
                              {_icon_map}
                              <span>{{{{ props.opt.label }}}}</span>
                            </div>
                        ''')
                        _vendor_hint = ui.label('').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-dim);')

                        def _sync_vendor_hint(v=None):
                            _v = v or _vendor_sel.value or _cur_vendor
                            _m = _vmeta(_v)
                            # 📌 「留空即走官方」这句在下面「中转地址」那一格已经写了 ——
                            #    同一件事说两遍，还容易说得不一样。
                            _vendor_hint.set_text(_m['key_hint'])

                        def _sync_vendor(e):

                            _sync_vendor_hint(e.value)
                            # ⚠️ 框内图标也要跟着换 —— 只换文字的话，
                            #    选了另一家、图标还停在原来那个，比没图标更误导。
                            _src = _vicons.get(e.value, '')
                            if _src:
                                ui.run_javascript(
                                    f"const _i=document.getElementById('nano-vendor-icon');"
                                    f"if(_i) _i.src='{_src}';")

                        _sync_vendor_hint(_cur_vendor)
                        _vendor_sel.on_value_change(_sync_vendor)

                    with ui.column().style('gap:6px; width:100%;'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label('API Key').style('font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);')
                            ui.label('必填').style(
                                'font-size:var(--nano-fs-2xs); color:var(--nano-danger); background:rgba(var(--nano-danger-rgb),0.14); '
                                'padding:1px 5px; border-radius:2px;')
                        _key_inp = ui.input(
                            placeholder='sk-ant-... 或中转 key',
                            value=_cur_api_key,
                            password=True, password_toggle_button=True,
                        ).props('outlined dense').style(
                            'width:100%; font-family:var(--nano-mono); font-size:var(--nano-fs-base);'
                        ).classes('nano-dark-input')
                        # ⚠️ 不再写死 sk-ant- —— 那是**假设了厂商是 Anthropic**。
                        #    具体提示跟着上面选的厂商走（见 _sync_vendor_hint）。
                        ui.label('填入所选厂商的 API Key；使用中转时填中转方发的 key').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-dim);')

                    with ui.column().style('gap:6px; width:100%;'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label('中转地址').style('font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);')
                            ui.label('可选').style(
                                'font-size:var(--nano-fs-2xs); color:var(--nano-dim); background:rgba(var(--nano-dim-rgb), 0.14); '
                                'padding:1px 5px; border-radius:2px;')
                        _relay_inp = ui.input(
                            placeholder='https://example.com/anthropic',
                            value=_cur_relay_url,
                        ).props('outlined dense').style(
                            'width:100%; font-family:var(--nano-mono); font-size:var(--nano-fs-base);'
                        ).classes('nano-dark-input')
                        ui.label('填入则启用中转模式；留空则官方直连').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-dim);')

                    with ui.column().style('gap:6px; width:100%;'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label('HTTP 代理').style('font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);')
                            ui.label('可选').style(
                                'font-size:var(--nano-fs-2xs); color:var(--nano-dim); background:rgba(var(--nano-dim-rgb), 0.14); '
                                'padding:1px 5px; border-radius:2px;')
                        _proxy_inp = ui.input(
                            placeholder='http://127.0.0.1:7890',
                            value=_cur_proxy,
                        ).props('outlined dense').style(
                            'width:100%; font-family:var(--nano-mono); font-size:var(--nano-fs-base);'
                        ).classes('nano-dark-input')
                        ui.label('同时写入 HTTP_PROXY 和 HTTPS_PROXY；留空则清除代理').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-dim);')

                    def _save_env():
                        _key   = _key_inp.value.strip()
                        _relay = _relay_inp.value.strip()
                        _proxy = _proxy_inp.value.strip()
                        if not _key:
                            ui.notify('API Key 不能为空', type='warning', icon='warning')
                            return
                        # 🔴 老代码按「有没有中转地址」决定 key 存哪个变量 ——
                        #    又是把「地址填不填」当成「key 算不算数」的开关，
                        #    而且 ANTHROPIC_API_KEY 这个名字对深度求索的 key 是错的。
                        #    📌 **一把 key 就是一把 key**，存哪儿不该由另一个字段决定。
                        # ⚠️ 变量名里的 RELAY 是历史包袱；改名要迁移用户已有的 .env，
                        #    收益只是名字好看 ⇒ 不改（同 soft_cap_usd 那次的判断）。
                        _vendor = (_vendor_sel.value or 'anthropic')
                        _updates = {
                            'NANO_API_VENDOR': _vendor,
                            'NANO_API_RELAY_API_KEY': _key,
                            'NANO_API_RELAY_BASE_URL': _relay or None,
                            'ANTHROPIC_API_KEY': None,
                        }
                        os.environ['NANO_API_VENDOR'] = _vendor
                        os.environ['NANO_API_RELAY_API_KEY'] = _key
                        os.environ.pop('ANTHROPIC_API_KEY', None)
                        if _relay:
                            os.environ['NANO_API_RELAY_BASE_URL'] = _relay
                        else:
                            os.environ.pop('NANO_API_RELAY_BASE_URL', None)
                        if _proxy:
                            _updates['HTTP_PROXY']  = _proxy
                            _updates['HTTPS_PROXY'] = _proxy
                            os.environ['HTTP_PROXY']  = _proxy
                            os.environ['HTTPS_PROXY'] = _proxy
                        else:
                            _updates['HTTP_PROXY']  = None
                            _updates['HTTPS_PROXY'] = None
                            os.environ.pop('HTTP_PROXY',  None)
                            os.environ.pop('HTTPS_PROXY', None)
                        try:
                            _upsert_dotenv(_ENV_PATH, _updates)
                        except Exception as _e:
                            ui.notify(f'.env 写入失败：{_e}', type='negative')
                            return
                        ok = self.provider.reconfigure()
                        if ok:
                            ui.notify('已保存并生效', type='positive', icon='check_circle')
                            self._refresh_relay_badge()
                            # ⚠️ 下拉框是常驻控件，不会自己重建 —— 必须显式刷。
                            self._refresh_model_select()
                            _dlg.close()
                        else:
                            ui.notify('已写入 .env，但 provider 初始化失败——请检查 Key 是否有效', type='warning')

                    with ui.row().style('justify-content:flex-end; width:100%;'):
                        # ⚠️ 中性色：琥珀留给真正需要被看见的东西
                        ui.button('保存', on_click=_save_env).props('unelevated flat').style(
                            'color:var(--nano-fg) !important; background:rgba(var(--nano-fg-rgb), 0.10); '
                            'border:1px solid rgba(var(--nano-fg-rgb), 0.22); border-radius:8px; '
                            'padding:4px 18px; font-size:var(--nano-fs-base);')

            _dlg.open()

    # 步长 / 下限：2026-08-29 定。软最低 1，硬最低 1.5（= 软的下限 + 一跳）。
    _CAP_STEP = 0.5
    _CAP_SOFT_MIN = 1.0

    def _build_settings_cost_cap(self):
        """用量限额的内容区。**即时生效，没有保存按钮。**

        ⭐ 形态由 已定：步进器（不是滑块），且**硬上限永远被软上限顶着走**
           —— 于是「软 > 硬」这个非法组合根本无法被造出来。
           📌 与其检测非法状态再提示，不如让它压根出现不了。

        ⚠️ 一律用 `on_value_change`，不用 `.on('update:model-value', …)`。
           那是 2026-08-10 实测抓过的坑（滑块节流 → 读到还没同步完的值 →
           标签停在陈旧值而按钮仍可点）。控件换了，**那条判据没换**：
           📌 读控件的值要用框架保证「已同步」的钩子，不要用底层事件。
        """
        cfg = usage_tracker.load_config()
        cost = usage_tracker.today_cost()
        # ⚠️ 同 OS 权限那处：代码改控件值会再次触发回调 ⇒ 用闸挡掉那一次。
        _busy = {"v": False}

        def _persist():
            try:
                c = usage_tracker.load_config()
                c["enabled"] = bool(enabled_sw.value)
                c["soft_cap_usd"] = float(soft_in.value or self._CAP_SOFT_MIN)
                c["hard_cap_usd"] = float(hard_in.value or self._CAP_SOFT_MIN + self._CAP_STEP)
                usage_tracker.save_config(c)
            except Exception as e:
                ui.notify(f"保存限额失败：{e}", type="negative", icon="error")

        def _clamp_and_save(_e=None):
            """把硬上限夹到 >= 软+一跳，然后写盘。**两个方向共用这一处。**"""
            if _busy["v"]:
                return
            try:
                s = float(soft_in.value or self._CAP_SOFT_MIN)
                h = float(hard_in.value or 0)
            except Exception:
                return
            floor = s + self._CAP_STEP
            if h < floor:
                _busy["v"] = True
                try:
                    hard_in.value = floor
                finally:
                    _busy["v"] = False
            _persist()

        # ⭐ 布局：**三样一排**（2026-08-29 定的）
        #
        # 🪦 原来是两段：上面「每日用量限额 + 今日已用 $x；关闭后不再拦截…」加开关，
        #    下面才是两个输入框。
        #      · 「今日已用 $3.785」在标题栏里**已经有一份**了 —— 同一个数字出现两次
        #      · 「关闭后不再拦截，但仍会记账」没啥用 —— **开关本身就是启用/禁用的意思**
        #      · 两个框「全都挤在最左边，右边都是空的」
        #    📌 一个说明如果只是把控件的名字换个说法重讲一遍，它就不是说明。
        # ⇒ 标签一行、控件一行，三样在整宽里分布开。
        with ui.row().classes("w-full items-end").style("gap:18px; padding:2px 0 4px;"):
            for _lab, _sub in (("软上限", "黄色警告"), ("硬上限", "停止发送")):
                with ui.column().classes("gap-1").style("flex:1; min-width:0;"):
                    ui.label(f"{_lab}（{_sub}）").style(
                        "font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg);")
                    # ⚠️ 输入框跟着列宽走（原来写死 118px，$ 右边留一大片空）。
                    _ph = ui.column().classes("w-full")
                    if _lab == "软上限":
                        _soft_slot = _ph
                    else:
                        _hard_slot = _ph
            # 开关自己一列，不给标签 —— 开关的两个状态就是它的标签。
            with ui.column().classes("items-center justify-end").style("flex-shrink:0;"):
                enabled_sw = ui.switch(value=cfg.get("enabled", True))                     .props("color=indigo-4")                     .on_value_change(lambda e: _persist())

        with _soft_slot:
            soft_in = ui.number(
                value=float(cfg.get("soft_cap_usd", 5)),
                min=self._CAP_SOFT_MIN, max=50, step=self._CAP_STEP, format="%.1f",
            ).props("dense outlined suffix=$").classes("w-full")              .on_value_change(_clamp_and_save)
        with _hard_slot:
            hard_in = ui.number(
                value=float(cfg.get("hard_cap_usd", 10)),
                min=self._CAP_SOFT_MIN + self._CAP_STEP, max=100,
                step=self._CAP_STEP, format="%.1f",
            ).props("dense outlined suffix=$").classes("w-full")              .on_value_change(_clamp_and_save)



    def _show_cost_cap_dialog(self):
        """用量限额的弹窗。**两个入口共用**：下拉菜单里那一项，和设置→通用里的「管理」按钮。

        ⚠️ 原 docstring 写的是「软/硬 cap **滑块**」—— 控件 2026-08-29 换成步进器了。
           📌 一个描述实现细节的注释，会在实现变了之后**继续自信地描述旧世界**。
        """
        # ⚠️ 标题栏那句「今日已用 $x」要用它 —— 上一版改 docstring 时把这行
        #    连同 cfg 一起删了，结果**方法还在、点开就 NameError**。
        #    📌 按 AST 校验「方法在不在」抓不到这种：**在，但一跑就炸**。
        cost = usage_tracker.today_cost()

        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().classes('nano-soft-field').style(
                     'width:420px; background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.2); '
                     'border-radius:16px; padding:0; overflow:hidden;'
                 ):
                # 标题栏
                with ui.row().style(
                    'width:100%; align-items:center; justify-content:space-between; '
                    'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                    'background:var(--nano-panel); border-radius:16px 16px 0 0; flex-wrap:nowrap;'
                ):
                    with ui.row().classes('items-center gap-3 flex-1 min-w-0'):
                        ui.icon('payments').style('font-size:var(--nano-fs-4xl); color:var(--nano-fg-soft); flex-shrink:0;')
                        with ui.column().style('gap:2px; min-width:0;'):
                            ui.label('用量限额').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                            ui.label(f'今日已用 {_cur()}{cost:.3f}').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);'
                            )
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;').classes('flex-shrink-0')

                self._build_settings_cost_cap()

            dialog.open()

    def _build_settings_mcp(self):
        """MCP 连接的内容区。**独立弹窗和设置面板共用这一份。**

        ⭐ 它单开一页而个人信息只给一行 —— **不是按代码行数分的**
           （MCP 212 行，个人信息 261 行，行数上还更少）。
           📌 **一页 vs 一行，看用户在里面待多久。**
              这里要反复查看状态、开关、展开详情、粘 JSON 加新的 —— 值得一整页。

        🔴 里面那个「应用」按钮**不能去掉**，理由跟环境配置又不一样：
           环境配置是「填错了看不出来，需要一个我填完了的动作」；
           这里是**异步冲突** —— 开关采用「待定 → 应用」模型，推开关只改本地
           `_pending`，不立即重连。改造前每推一次就异步重连 + 刷新，两边打架，
           表现为**开关自己变回去**。
           📌 「去掉保存按钮」是个好默认，但它有三种各自独立的例外：
              ① 连续输入（打字打到一半不是想要的值）
              ② 填错了不当场露馅（验证需要一个触发点）
              ③ **生效动作本身是异步的**（立即生效会和上一次生效打架）
        """
        # 🔴 这行 import 差点被漏在原方法里：搬内容区时只抓了
        #    `mgr = get_mcp_manager()`，没抓它上面那句局部 import ——
        #    结果**语法全绿、回归全绿，点开 MCP 页当场 NameError**。
        #    📌 搬一段代码时，它依赖的**局部 import** 跟它一样是那段的一部分；
        #       而漏掉 import 不会报语法错，只会在真的走到那一行时才炸。
        from core.mcp_client import get_mcp_manager
        mgr = get_mcp_manager()

        # ⚠️ 状态色/文案表：**定义必须跟着使用走**。
        #    它原来留在 `_show_mcp_dialog` 里，而用它的那段被搬到了这里 ——
        #    「定义在后、使用在前」，点开就 NameError。
        #    📌 搬一段代码，要连它**读到的东西**一起搬，不只是它写的东西。
        _ST_META = {
            "connected":    ("var(--nano-ok)", "已连接"),
            "connecting":   ("var(--nano-warn)", "连接中"),
            "failed":       ("var(--nano-danger)", "连接失败"),
            "needs_auth":   ("var(--nano-warn)", "需要登录"),
            "disabled":     ("var(--nano-fg-soft)", "已禁用"),
            "disconnected": ("var(--nano-fg-soft)", "未连接"),
        }
        # ── 块1：服务器状态列表 ───────────────────────────────────
        # 开关采用「待定 → 应用」模型：推开关只改本地 _pending（开关自由移动、
        # 不再每次异步重连+刷新打架，根除"自己变回去"的 bug）。真正生效在底部
        # 「应用」按钮：兼做"添加粘贴的新 server" + "保存开关变更" + 重连。
        # 开关用「待定 → 应用」模型：推开关只移动它自己（不立即生效、不刷新，
        # 根除"自己变回去"）。应用时直接读开关的 .value（开关本身就是期望状态源，
        # 比监听 update:model-value 事件可靠——之前 e.args 拿不到 bool 导致判"没变化"）。
        _switches: dict = {}   # name -> ui.switch
        # ⭐ 展开状态：**每行独立**，不是手风琴（用户可能想同时对比两个）。
        # ⚠️ 刻意**不持久化** —— 关掉弹窗再打开回到全折叠。
        #    📌 它是「查一眼」的动作，不是一个偏好。
        _expanded: set = set()
        # 🪦 原来这里有 `max-height:300px; overflow-y:auto` —— 那是它还是独立弹窗
        #    时的设定。进了设置面板之后，**外层已经在滚**，于是套了两层：
        #    2026-08-29：「上面这一坨是一个容器，然后整个页面是另一个容器，
        #    感觉非常奇怪」。那条横向滚动条也是这么来的。
        #    📌 **滚动容器套滚动容器，用户分不清自己在滚哪一个** ——
        #       而且内层一旦有横向溢出，就会多出一条谁也不需要的横条。
        # ⚠️ 横向 padding 交给外层；这里只留行与行之间的间距。
        _list_col = ui.column().style('width:100%; padding:4px 0 0; gap:6px;')

        def _render_list():
            _list_col.clear()
            _switches.clear()
            # ⭐ `owned_by` 非空 = 某个 Skill 的内部零件 → **整行不显示**。
            #    2026-08-28：「用户根本不需要"管理"这个」。
            #    📌 用户管理的是**能力**（Skill OpenPageWithBrowser），
            #       不是能力的**零件**（playwright-headless）。
            # 🔴 改造前两者平级摆着，用户可以禁用零件让能力静默失效 ——
            #    Skill 仍显示 READY，一用就失败，而用户不知道为什么。
            # ⚠️ 过滤放在**取快照之后、`if not snap` 之前** ——
            #    否则一个只剩零件的机器会显示成"有能力"却列不出任何一行。
            snap = [x for x in mgr.status_snapshot() if not x.get("owned_by")]
            with _list_col:
                if not snap:
                    ui.label('还没有外接能力。粘贴一段 server 配置，点「应用」即可添加。').style(
                        'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); padding:8px 0;')
                for s in snap:
                    _color, _stlabel = _ST_META.get(s["status"], ("var(--nano-fg-soft)", s["status"]))
                    with ui.row().style('width:100%; align-items:center; gap:10px; '
                                        'padding:8px 10px; border: 1px solid var(--nano-border); '
                                        'border-radius:10px;'):
                        ui.element('div').style(f'width:9px; height:9px; border-radius:999px; '
                                                f'background:{_color}; flex-shrink:0;')
                        with ui.column().style('gap:1px; flex:1; min-width:0;'):
                            ui.label(s["name"]).style('font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg);')
                            _sub = f'{"本地 stdio" if s["transport"]=="stdio" else "远程 HTTP"} · {_stlabel}'
                            if s["status"] == "connected":
                                _sub += f' · {s["tool_count"]} 个能力'
                            ui.label(_sub).style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft); '
                                                 'white-space:nowrap; overflow:hidden; text-overflow:ellipsis;')
                            # ── 出错原因：分类过的人话 + 恢复建议 ──────
                            # 🔴 这里**曾经**是 `s["last_error"][:36]`，与 那个
                            #    28 字符截断是同一形状的错：
                            #    `ModuleNotFoundError: No module named 'mcp_server_fetch'`
                            #    有 48 字符，切完正好把**模块名**丢掉 ——
                            #    📌 截断永远先切掉信息量最大的那一段（它在最后）。
                            # ⚠️ 而且旧代码只在 `failed` 时显示：`needs_auth` /
                            #    `disconnected` 下 `last_error` 一个字都不出现。
                            _fmsg = s.get("fault_message") or s.get("last_error") or ""
                            if s["status"] in ("failed", "needs_auth", "disconnected") and _fmsg:
                                # 不截断、允许换行 —— 这一行的全部价值就是"说清楚为什么"。
                                ui.label(_fmsg).style(
                                    'font-size:var(--nano-fs-xs); color:var(--nano-amber); line-height:1.45; '
                                    'white-space:normal; word-break:break-word;')
                                # ⚠️ 这里**刻意不给恢复建议** —— 那是故障卡片的职责。
                                #    📌 两处说同一句话时，改的人只会改到一处。
                                #    这一行只陈述事实：为什么连不上。
                        if s["status"] in ("needs_auth", "failed", "disconnected") and s["enabled"]:
                            _ic = 'login' if s["status"] == "needs_auth" else 'refresh'
                            ui.button(icon=_ic, on_click=lambda e=None, n=s["name"]: _do_retry(n)) \
                                .props('flat round dense').style('color:var(--nano-fg-soft) !important;')
                        # 开关：只移动自己（不立即生效），应用时读 .value。官方自带也可禁用。
                        _switches[s["name"]] = ui.switch(value=s["enabled"]).props('color=indigo-4 dense')
                        if s.get("builtin"):
                            # 官方自带能力：受保护，可禁用、不可删除（对齐官方 Skill 模型）
                            ui.label('官方').style(
                                'font-size:var(--nano-fs-2xs); font-weight:700; color:var(--nano-fg-soft); '
                                'background:rgba(var(--nano-info-rgb),0.12); padding:2px 7px; '
                                'border-radius:999px; flex-shrink:0;')
                        else:
                            # 用户自己加的：可删除（破坏性动作，立即生效）
                            ui.button(icon='delete_outline', on_click=lambda e=None, n=s["name"]: _do_remove(n)) \
                                .props('flat round dense').style('color:var(--nano-danger) !important;')
                        # ⭐ 展开箭头 —— 跟在徽标/删除键**后面**。
                        # ⚠️ 官方那一支画徽标、非官方那一支画删除键，
                        #    **两边都占一个位子** ⇒ 箭头天然对齐，
                        #    不需要给谁留占位空格（2026-08-28 点名的那条）。
                        _open = s["name"] in _expanded
                        ui.button(icon='expand_less' if _open else 'expand_more',
                                  on_click=lambda e=None, n=s["name"]: _toggle(n)) \
                            .props('flat round dense').style('color:var(--nano-fg-soft) !important;')
                    # ── 展开详情：向下展开，缩在这一行下面 ──────────────
                    if s["name"] in _expanded:
                        with ui.column().style(
                                'width:100%; gap:6px; margin:-4px 0 2px 26px; '
                                'padding:10px 12px; border-radius:10px; '
                                'background:rgba(var(--nano-shade-rgb), 0.04);'):
                            _desc = (s.get("description") or "").strip()
                            if _desc:
                                # ⚠️ 拿不到就**整栏不画**，不写「暂无描述」——
                                #    📌 一句"暂无描述"和没有这一栏信息量相同，
                                #       但前者占了地方。
                                ui.label('作用').style(self._MCP_H)
                                ui.label(_desc).style(self._MCP_B)
                            _tl = s.get("tools") or []
                            if _tl:
                                # ⭐ 它是「作用」那句的**证据** —— 用户可以自己核对
                                ui.label('提供的能力').style(self._MCP_H)
                                ui.label('、'.join(_tl[:24])
                                         + (f' …共 {len(_tl)} 个' if len(_tl) > 24 else '')
                                         ).style(self._MCP_B)
                            _how = (s.get("url") or "").strip() or " ".join(
                                [s.get("command") or ""] + list(s.get("args") or [])).strip()
                            if _how:
                                # 🔴 唯一能回答「这东西到底在我机器上跑什么」的一栏。
                                #    ⇒ 与接入授权弹窗里那一栏**是同一份信息**，
                                #      只是一个在装之前、一个在装之后。
                                #      📌 装之前给你看、装之后藏起来，那是耍流氓。
                                ui.label('启动方式').style(self._MCP_H)
                                ui.label(_how).style(
                                    self._MCP_B + 'font-family:ui-monospace,Consolas,monospace; '
                                             'word-break:break-all;')

        def _toggle(name):
            _expanded.discard(name) if name in _expanded else _expanded.add(name)
            _render_list()

        def _do_retry(name):
            ui.notify(f'正在重连 {name}…', type='info')
            asyncio.create_task(mgr.retry_server(name))
            ui.timer(1.6, _render_list, once=True)

        def _do_remove(name):
            asyncio.create_task(mgr.remove_server(name))
            ui.notify(f'已移除 {name}', type='warning')
            ui.timer(0.3, _render_list, once=True)

        _render_list()

        # ── 块3：添加 server —— **列表的最后一条** ─────────────────────────
        #
        # ⭐ 2026-08-29 的形状：每个 MCP 占一条，**最下面再加一条**，
        #    里面居中写「添加 server」。点它弹出粘 JSON 的二级弹窗。
        #    📌 一个「加一项」的入口，长得像**列表的下一项**最自然 ——
        #       它做的事就是让列表多一项。
        # 🪦 中间试过「右下角按钮」，被否：那是把它当成页面级操作，
        #    而它其实是**列表级**操作。
        # ⚠️ 粘 JSON 那一整块本身**逐行没变**，只是从常驻区挪进了弹窗 ——
        #    它是低频动作（来这页十次有九次是看状态、开关），
        #    📌 **常驻空间该按使用频率分配，不按功能重要性分配。**
        with ui.row().classes('w-full items-center justify-center cursor-pointer') \
                .style('padding:13px 0; gap:7px; border-top:1px solid rgba(var(--nano-fg-rgb), 0.07);') \
                .on('click', lambda: _open_add_dialog()):
            ui.icon('add').style('font-size:var(--nano-fs-xl); color:var(--nano-amber);')
            ui.label('添加 server').style('font-size:var(--nano-fs-base); color:var(--nano-amber);')

        def _open_add_dialog():
            """粘 JSON 加新 server 的二级弹窗。内容与改造前**逐行相同**，
            只是从常驻区挪进了弹窗，并把「应用」挪到右下角。
            """
            with ui.dialog() as _add_dlg, ui.card().style(
                    'width:520px; max-width:94vw; background:var(--nano-panel); border:1px solid rgba(var(--nano-amber-rgb), 0.20); border-radius:14px; padding:18px 20px 14px;'):
                ui.label('从 server 文档复制 JSON 粘进来，会自动校验并自动连接。').style(
                    'font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                _paste = ui.textarea(
                    placeholder='{\n  "mcpServers": {\n    "名字": { "command": "npx", "args": ["..."] }\n  }\n}'
                ).props('outlined').style('width:100%; font-family:monospace; font-size:var(--nano-fs-sm); min-height:96px;')
        
                # 「应用」兼做三件事：①添加粘贴的新 server ②保存开关变更 ③重连。
                # 粘贴框为空也行——只应用开关变更。应用后【不关闭弹窗】，只弹成功气泡，
                # 用户自己点叉子关。开关期望状态直接读 _switches[name].value（可靠）。
                async def _do_apply():
                    _paste_txt = (_paste.value or "").strip()
                    _added = ""
                    if _paste_txt:
                        ok, msg = mgr.add_server_from_json(_paste_txt)
                        if not ok:
                            ui.notify(f'添加失败：{msg}', type='negative')
                            return
                        _paste.set_value('')
                        _added = msg
                    _changed = 0
                    for _n, _sw in list(_switches.items()):
                        _s = mgr.servers.get(_n)
                        if _s is not None and bool(_s.enabled) != bool(_sw.value):
                            await mgr.set_enabled(_n, bool(_sw.value))
                            _changed += 1
                    await mgr.connect_enabled()
                    if _added or _changed:
                        _parts = []
                        if _added:
                            _parts.append(f'已添加 {_added}')
                        if _changed:
                            _parts.append(f'{_changed} 个开关已生效')
                        ui.notify('已应用：' + '，'.join(_parts), type='positive', icon='check_circle')
                    else:
                        ui.notify('没有要应用的改动', type='info')
                    _render_list()
                    ui.timer(1.6, _render_list, once=True)
        
                ui.button('应用', icon='check', on_click=_do_apply) \
                    .props('unelevated color=primary').style('border-radius:10px; align-self:flex-end;')
            _add_dlg.open()


    def _show_mcp_dialog(self):
        """MCP 连接管理（设置下拉里的隐藏页，平时不开）。三块：状态列表 / 每行控制 / 粘贴添加。
        config 文件是 source-of-truth，本面板是它的人性化前门——用户永远不碰原始 json。"""
        from core.mcp_client import get_mcp_manager
        mgr = get_mcp_manager()


        with self._ui_scope():
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style('width:480px; background:var(--nano-panel); '
                                 'border:1px solid rgba(var(--nano-amber-rgb), 0.2); border-radius:16px; '
                                 'padding:0; overflow:hidden;'):
                # 标题栏
                with ui.row().style('width:100%; align-items:center; justify-content:space-between; '
                                    'padding:14px 20px; border-bottom: 1px solid var(--nano-border); '
                                    'background:var(--nano-panel); border-radius:16px 16px 0 0;'):
                    with ui.row().classes('items-center gap-3'):
                        ui.icon('extension').style('font-size:var(--nano-fs-4xl); color:var(--nano-fg-soft);')
                        with ui.column().style('gap:2px;'):
                            ui.label('MCP 连接').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                            ui.label('Nano 的外接能力').style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
                    ui.button(icon='close', on_click=dialog.close).props('flat round dense').style('color:var(--nano-fg) !important;')

                self._build_settings_mcp()

            dialog.open()

    def _show_mcp_connect_dialog(self, info: dict, purpose_line: str,
                                 what_it_does: str, on_confirm, on_cancel):
        """「Nano 请求接入一个 MCP 服务」。

        ⚠️ **风格照抄 `_show_os_action_confirm_dialog`**——
           三段式：标题栏 `var(--nano-panel-2)` / 详情区 `var(--nano-panel)` / 操作栏 `var(--nano-panel-2)`，
           整套暖褐色温。📌 第一版另配了一套色，结果那个红"不像纯红"、
           而且跟周围色温不同 —— **一个跟周围色温不同的块，比色号错更显眼**
           （那条判据本来就写在 OS 弹窗的注释里，当时没去看）。

        ⚠️ **stdio 与 http 必须不一样**：
            stdio  在你机器上起一个第三方进程   → 红（复用 risk=3 那套）
            http   只发网络请求                → 黄（复用 risk=2 那套）
        📌 后果差一个量级，长得一样反而是在撒谎。

        ⚠️ **没有「始终允许」** —— 每次要接的是不同的第三方，
           「始终」在这里没有指称对象。
        """
        client = self._ui_client
        if client is None:
            return
        _stdio = (info.get("transport") or "stdio") != "http"
        border_color = "rgba(var(--nano-danger-rgb),0.4)" if _stdio else "rgba(var(--nano-warn-rgb),0.3)"
        icon_color = "var(--nano-danger)" if _stdio else "var(--nano-warn)"
        title_color = "var(--nano-danger)" if _stdio else "var(--nano-warn)"
        _done = {"v": False}

        def _fire(cb, dlg):
            if _done["v"]:
                return
            _done["v"] = True
            try:
                dlg.close()
            finally:
                try:
                    cb()
                except Exception as e:
                    logger.warning(f"[B3] MCP 授权回调异常: {e}")

        with client:
            with ui.dialog().props('no-backdrop-dismiss') as dialog, \
                 ui.card().style(
                     f'width:460px; max-width:95vw; padding:0; background:var(--nano-panel); '
                     f'border:1px solid {border_color}; border-radius:14px; overflow:visible;'):

                # ── 标题栏 ──────────────────────────────────────────────
                with ui.row().style(
                        'width:100%; align-items:center; gap:10px; padding:14px 20px; '
                        'border-bottom:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                        'background:var(--nano-panel-2); border-radius:14px 14px 0 0;'):
                    ui.icon('extension').style(f'font-size:var(--nano-fs-5xl); color:{icon_color};')
                    with ui.column().style('gap:2px; min-width:0;'):
                        ui.label('请求接入一个 MCP 服务').style(
                            f'font-size:var(--nano-fs-md); font-weight:600; color:{title_color};')
                        # ⚠️ 副行与 OS 弹窗的 `action:` 那行同字号同色 ——
                        #    📌 一个只用来消歧的标记，不该比它消歧的那件事更响。
                        ui.label(f'{"本地进程 stdio" if _stdio else "远程 HTTP"} · '
                                 f'{info.get("name", "")}').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')

                # ── 详情区 ──────────────────────────────────────────────
                with ui.column().style(
                        'width:100%; padding:16px 20px; gap:12px; background:var(--nano-panel);'):

                    def _sec(title, body, mono=False, warn=False):
                        if not (body or "").strip():
                            return          # 拿不到就整栏不画，不写占位句
                        with ui.column().style('gap:3px; width:100%;'):
                            ui.label(title).style(
                                'font-size:var(--nano-fs-xs); font-weight:700; letter-spacing:0.04em; '
                                + (f'color:{icon_color};' if warn else 'color:var(--nano-dim);'))
                            ui.label(body).style(
                                'font-size:var(--nano-fs-base); line-height:1.55; white-space:normal; '
                                + ('font-family:ui-monospace,Consolas,monospace; '
                                   'word-break:break-all; ' if mono else '')
                                + (f'color:{icon_color};' if warn else 'color:var(--nano-fg-soft);'))

                    # ⭐ 判断的锚：先看「这跟我要的事对不对得上」
                    _sec('你刚才要做的', purpose_line)
                    _sec('它是什么', what_it_does)
                    if _stdio:
                        # 🔴 原样显示，一个字不改写
                        _sec('它会在你的电脑上运行',
                             " ".join([info.get("command", "")]
                                      + list(info.get("args") or [])).strip(),
                             mono=True, warn=True)
                        ui.label('这会下载并执行第三方代码。').style(
                            f'font-size:var(--nano-fs-sm); color:{icon_color};')
                    else:
                        _sec('它会请求这个地址', info.get("url", ""), mono=True)
                        ui.label('只发网络请求，不在你的电脑上运行程序。').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft);')
                    _envk = info.get("env_keys") or []
                    if _envk:
                        # ⚠️ 只报 key 名 —— 值可能是 token
                        _sec('需要的环境变量', '、'.join(_envk))

                # ── 操作栏 ──────────────────────────────────────────────
                with ui.row().style(
                        'width:100%; align-items:center; justify-content:flex-end; gap:10px; '
                        'padding:12px 20px; flex-wrap:wrap; '
                        'border-top:1px solid rgba(var(--nano-contrast-rgb), 0.06); '
                        'background:var(--nano-panel-2); border-radius:0 0 14px 14px;'):
                    ui.button('取消', icon='close').props('flat').style(
                        'color:var(--nano-fg-soft); font-size:var(--nano-fs-md);'
                    ).on('click', lambda: _fire(on_cancel, dialog))
                    ui.button('接入', icon='check').props('unelevated').style(
                        f'background:{"var(--nano-danger-fill)" if _stdio else "var(--nano-warn-fill)"}; color:#fff; '
                        f'font-size:var(--nano-fs-md); padding:0 14px; border-radius:8px;'
                    ).on('click', lambda: _fire(on_confirm, dialog))

            dialog.open()

    # MCP 展开详情的两个样式常量（小标题 / 正文）
    _MCP_H = 'font-size:var(--nano-fs-xs); font-weight:700; color:var(--nano-dim); letter-spacing:0.04em;'
    _MCP_B = 'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.5; white-space:normal;'

    # ══════════════════════════════════════════════════════════════════════
    # 聊天搜索（2026-08-14 定的形态）
    # ══════════════════════════════════════════════════════════════════════
    #
    # ⭐⭐⭐ **只搜聊天区里真实存在的东西。** 被 L2→L3 推出 UI 的历史搜不到。
    #
    # 用户的两条理由（都成立，第 2 条把复杂度砍掉一个数量级）：
    #   ① 「这就是最正常的认知里的搜索功能。你比如微信之类的聊天软件，
    #      **从来没见过一个 UI 里历史对话实际上已经不存在但还能搜到的** ——
    #      搜索的语义是快速找到 + 跳转，节省自己拉滚动条的时间」
    #   ② 条目在 UI 里真实存在 → **能真的跳过去** → 完全效仿 Claude Code 即可。
    #
    # ⚠️ 而早先的设计那句「UI 保留到 L2 时必须有用户侧历史入口，否则是能力倒退」
    #    **不由这里关**。已明确我们混淆了两个概念：
    #        「搜索」  是**动作** —— 在还看得见的东西里定位
    #        「数据入口」是**产权** —— 用户说过的话，用户得能拿到
    #    📌 **跳不过去的东西，与其在 UI 里摆着，不如让用户真正拿走** ——
    #       所以由「设置 → 通用 → 导出数据」关。
    #
    # ═══ 🔴 为什么整个浮层住在浏览器里，而不是用 NiceGUI 组件 ═══
    #
    # 第一版用 `ui.dialog` + `await ui.run_javascript(...)` 取扫描结果，实测报：
    #     Cannot await JavaScript responses on the auto-index page.
    #     There could be multiple clients connected and it is not clear which one to wait for.
    # 回 NiceGUI 源码核实（`Client.run_javascript`）：
    #     `if self is self.auto_index_client: raise RuntimeError(...)`
    # —— **写死的，没有绕过去的参数**。而本项目正是 auto-index（模块顶层建 UI，
    # 没有 `@ui.page`）。项目里那个 `_js_fire`（注释写着「auto-index 安全的 JS 推送：
    # 只发送，不等待浏览器返回」）就是当年撞过的同一堵墙。
    #
    # ⭐ 于是改成**整个搜索都在浏览器里跑**：输入即搜、结果列表、点击跳转、关闭、
    #    Ctrl+F，全是 JS。Python 只负责把这段代码注入一次、以及"请显示出来"。
    # 📌 而这不是妥协，是**更对**：搜索的定义就是「UI 里有什么」，那它就该**只看 UI**。
    #    零往返、零 auto-index 限制，而且**不可能搜出一条界面上没有的东西** ——
    #    那正是这个功能最该守住的边界。
    _SEARCH_OVERLAY_JS = r"""
    (() => {
      if (window.__nanoSearchOpen) return;
      const CSS = `
        #nano-search-ov{position:fixed;inset:0;z-index:12000;display:none;
          background:rgba(var(--nano-ink-rgb), 0.45);align-items:flex-start;justify-content:center;}
        #nano-search-ov.on{display:flex;}
        #nano-search-box{margin-top:12vh;width:620px;max-width:92vw;background:var(--nano-panel);
          border:1px solid var(--nano-line);border-radius:14px;overflow:hidden;
          box-shadow:0 18px 60px rgba(var(--nano-ink-rgb), 0.55);}
        #nano-search-head{display:flex;align-items:center;gap:10px;padding:12px 14px;
          border-bottom:1px solid var(--nano-border);}
        #nano-search-inp{flex:1;min-width:0;background:transparent;border:0;outline:none;
          color:var(--nano-fg);font-size:var(--nano-fs-md);font-family:inherit;}
        #nano-search-inp::placeholder{color:var(--nano-faint);}
        #nano-search-hint{font-size:var(--nano-fs-xs);color:var(--nano-faint);padding:8px 14px 0;}
        #nano-search-list{max-height:46vh;overflow-y:auto;padding:6px;}
        .nano-search-row{padding:9px 10px;border-radius:8px;cursor:pointer;
          font-size:var(--nano-fs-base);line-height:1.6;word-break:break-word;color:var(--nano-fg-soft);}
        .nano-search-row:hover{background:rgba(var(--nano-fg-rgb), 0.07);}
        .nano-search-row .pre{color:var(--nano-dim);}
        .nano-search-x{background:transparent;border:0;color:var(--nano-dim);cursor:pointer;
          font-size:var(--nano-fs-xl);line-height:1;padding:2px 4px;}
      `;
      const st = document.createElement('style'); st.textContent = CSS;
      document.head.appendChild(st);

      const ov = document.createElement('div'); ov.id = 'nano-search-ov';
      ov.innerHTML = '<div id="nano-search-box">'
        + '<div id="nano-search-head">'
        + '<span style="color:var(--nano-dim);font-size:var(--nano-fs-xl);">&#128269;</span>'
        + '<input id="nano-search-inp" placeholder="\u641c\u7d22\u8fd9\u6bb5\u5bf9\u8bdd\u2026" autocomplete="off">'
        + '<button class="nano-search-x" id="nano-search-close">&#10005;</button>'
        + '</div><div id="nano-search-hint"></div><div id="nano-search-list"></div></div>';
      document.body.appendChild(ov);

      const inp = ov.querySelector('#nano-search-inp');
      const list = ov.querySelector('#nano-search-list');
      const hint = ov.querySelector('#nano-search-hint');
      const esc = (s) => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

      function scan(q) {
        const box = document.querySelector('.nano-chat-scroll');
        if (!box || q.length < 2) return [];
        const out = [], seen = new Set(); let idx = 0;
        box.querySelectorAll('div.w-full').forEach((b0) => {
          if (b0.querySelector('div.w-full')) return;    // 只取叶子块，避免父子重复命中
          // 🔴 叶子块**未必带说话人前缀**（2026-08-14 实测：nano 那条有「nano ❯」，
          //    用户那条只剩「你好」）。成因是 给用户气泡加的图片容器 ——
          //    它让 `Koala ❯` 那个 label 落在了叶子的**外面**。
          // ⭐ 所以往上爬到第一个带 `❯` 的祖先：那个字符就是每个气泡的说话人标记，
          //    **它本来就在 DOM 里**。
          // 📌 **不写死任何名字**（「不要写死用户俩字，跟气泡一样用个人信息里那个名字」）
          //    —— 名字从 DOM 上原样带出来，气泡改名它自动跟着改，
          //    连"去哪儿取名字"这个问题都不存在。
          let b = b0;
          for (let i = 0; i < 3 && b.parentElement; i++) {
            if ((b.innerText || '').indexOf('❯') >= 0) break;
            b = b.parentElement;
          }
          const t = (b.innerText || '').trim();
          if (!t || seen.has(t)) return;
          const at = t.toLowerCase().indexOf(q.toLowerCase());
          if (at < 0) return;
          seen.add(t);
          if (!b.dataset.nanoSearchId) b.dataset.nanoSearchId = 'nsr_' + (idx++);
          const s0 = Math.max(0, at - 30), e0 = Math.min(t.length, at + q.length + 50);
          out.push({id: b.dataset.nanoSearchId,
                    pre: (s0 > 0 ? '\u2026' : '') + t.slice(s0, at),
                    hit: t.slice(at, at + q.length),
                    post: t.slice(at + q.length, e0) + (e0 < t.length ? '\u2026' : '')});
        });
        return out.slice(0, 60);
      }

      function jump(id) {
        close();
        const el = document.querySelector('[data-nano-search-id="' + id + '"]');
        if (!el) return;
        el.scrollIntoView({behavior: 'smooth', block: 'center'});
        // 「跳过去了」这件事必须看得见，否则用户不知道自己停在哪一段。
        el.classList.remove('nano-jump-target'); void el.offsetWidth;
        el.classList.add('nano-jump-target');
      }

      let rows = [];
      function render() {
        const q = inp.value.trim();
        list.innerHTML = '';
        if (q.length < 2) { hint.textContent = '\u8f93\u5165\u81f3\u5c11 2 \u4e2a\u5b57\u7b26'; rows = []; return; }
        rows = scan(q);
        if (!rows.length) {
          // 措辞必须说清【搜的范围】——否则用户会以为"我没说过这句话"，
          // 而真相可能是那段已经被收走了。
          hint.textContent = '\u8fd9\u6bb5\u5bf9\u8bdd\u91cc\u6ca1\u6709\u5339\u914d\uff08\u66f4\u65e9\u7684\u5386\u53f2\u5df2\u4e0d\u5728\u754c\u9762\u4e0a\uff0c\u53ef\u5728 \u8bbe\u7f6e \u2192 \u901a\u7528 \u2192 \u5bfc\u51fa\u6570\u636e \u91cc\u53d6\uff09';
          return;
        }
        hint.textContent = rows.length + ' \u6761\u5339\u914d';
        rows.forEach(r => {
          const d = document.createElement('div');
          d.className = 'nano-search-row';
          d.innerHTML = '<span class="pre">' + esc(r.pre) + '</span>'
                      + '<span class="nano-hit">' + esc(r.hit) + '</span>'
                      + '<span>' + esc(r.post) + '</span>';
          d.addEventListener('click', () => jump(r.id));
          list.appendChild(d);
        });
      }

      let timer = null;
      inp.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(render, 160); });
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { close(); }
        else if (e.key === 'Enter' && rows.length) { jump(rows[0].id); }
      });
      ov.querySelector('#nano-search-close').addEventListener('click', close);
      ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });

      function open() {
        ov.classList.add('on');
        inp.value = ''; list.innerHTML = '';
        hint.textContent = '\u8f93\u5165\u81f3\u5c11 2 \u4e2a\u5b57\u7b26';
        setTimeout(() => inp.focus(), 30);
      }
      function close() { ov.classList.remove('on'); }
      window.__nanoSearchOpen = open;

      // Ctrl+F 全局捕获：用户在 composer 里打字时按它也该是搜索（那正是肌肉记忆）。
      document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
          e.preventDefault();
          if (!ov.classList.contains('on')) open();
        }
      });
    })()
    """

    # ══════════════════════════════════════════════════════════════════════
    # 聊天区的三个新入口：引用选中文字 / 打开本地文件 / 在文件夹中显示
    # ══════════════════════════════════════════════════════════════════════

    # 左键直接打开的扩展名**白名单**。
    #
    # 🔴🔴 为什么是白名单而不是「挡掉 .exe 就行」：`os.startfile` 走的是
    #    **系统默认关联**，而「默认关联」对不同扩展名意味着完全不同的事 ——
    #    对 `.pdf` 是「用阅读器打开」，对 `.py` / `.bat` / `.ps1` 是**直接执行**。
    # ⚠️ 而这些链接是**模型生成的**：只要它读过的某个文件里带着诱导性内容，
    #    就有可能让它写出一个指向可执行文件的链接，用户一点就中。
    # 📌 排除法欠账随时间增长，白名单不会 —— 新出现的可执行格式默认落在安全侧。
    # ⭐ 不在白名单里的**不是拒绝**，而是降级成「在文件夹中显示」：
    #    用户仍然到得了那个文件，只是**执行这一步由用户自己按下去**。
    _OPENABLE_EXTS = frozenset({
        ".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".conf", ".csv", ".tsv", ".xml", ".html", ".htm",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
        ".mp3", ".wav", ".flac", ".mp4", ".mkv", ".mov", ".avi", ".webm",
    })

    def _resolve_local_path(self, raw: str):
        """把链接里的路径解析成一个**存在的**绝对路径；解析不了返回 None。

        ⚠️ 相对路径按**项目根**解析（模型说 `config/mcp_servers.json` 时指的是这个），
           而不是按进程 cwd —— cwd 会被别的代码改，那会让同一个链接在不同时刻指向不同文件。
        """
        try:
            raw = (raw or "").strip().strip('"').strip("'")
            if not raw:
                return None
            import pathlib as _pl
            pth = _pl.Path(raw).expanduser()
            if not pth.is_absolute():
                pth = (_pl.Path(__file__).resolve().parent / pth)
            pth = pth.resolve()
            return pth if pth.exists() else None
        except Exception:
            return None

    def _reveal_in_explorer(self, pth) -> None:
        """在资源管理器里选中这个文件。

        🔴🔴 **这里踩过一个非常隐蔽的坑（实测：「永远跳转到我的文档」）**：
           第一版写的是 `subprocess.Popen(["explorer", f"/select,{pth}"])`。
           看起来最规范 —— 参数数组、不拼命令行、不 `shell=True`。
           **但它是错的**：Windows 上 `subprocess` 会把含空格的参数**自动加引号**，
           于是真正的命令行变成
               explorer "/select,C:∖...∖Nano-Lumen V1.11∖app.py"   （此处用 ∖ 代替反斜杠：docstring 不是 raw，写真的反斜杠会被当成转义）
           而 `explorer.exe` **不接受被整体引起来的 `/select,`** ——
           它解析失败后不报错，直接打开默认位置（「我的文档」）。
           ⚠️ 症状因此是「跳错地方」而不是「报错」，非常容易被当成路径算错了。

        📌 判据（可推广）：**「参数数组更安全」这条规则的前提是被调方
           按标准方式解析命令行。** `explorer.exe` 是出了名的不按标准来 ——
           它要的是 `/select,"<路径>"`（**路径带引号、开关不带**）。
           规则本身没错，是它的前提在这里不成立。

        → 改成传**命令行字符串**（Windows 的 `CreateProcess` 直接收字符串，
          **不经过 shell**，所以没有 shell 注入面），由我们自己控制引号位置。
        ⚠️ 仍然不用 `shell=True`；且 exe 走 `%SystemRoot%` 绝对路径，
           不靠 PATH（PATH 上可以放一个同名的 explorer.exe）。
        ⚠️ Windows 路径里不可能出现 `"`，所以这里的引号是闭合的。
        """
        import subprocess
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
        try:
            if pth.is_dir():
                subprocess.Popen(f'"{exe}" "{pth}"')
            else:
                # ⚠️ 逗号后面不能有空格；引号只包路径，不包 `/select,`
                subprocess.Popen(f'"{exe}" /select,"{pth}"')
        except Exception as e:
            logger.warning(f"[UI] 打开文件夹失败: {e}")
            ui.notify(f'打不开文件夹：{e}', type='negative')

    def _on_open_local_file(self, e) -> None:
        raw = (getattr(e, "args", None) or {}).get("path") or ""
        pth = self._resolve_local_path(raw)
        if pth is None:
            ui.notify(f'文件不在了：{raw}', type='warning', icon='link_off')
            return
        if pth.is_dir() or pth.suffix.lower() not in self._OPENABLE_EXTS:
            # 📌 降级而不是拒绝 —— 用户仍然到得了，只是「执行」这一步由他自己按。
            self._reveal_in_explorer(pth)
            if not pth.is_dir():
                ui.notify(f'{pth.name} 不是可直接打开的文档类型，已为你定位到它',
                          type='info', icon='folder_open')
            return
        try:
            os.startfile(str(pth))          # noqa: S606 - Windows 默认关联
        except Exception as err:
            logger.warning(f"[UI] 打开文件失败 {pth}: {err}")
            self._reveal_in_explorer(pth)
            ui.notify(f'打不开它，已改为定位到文件夹（{err}）', type='warning')

    def _on_open_external_url(self, e) -> None:
        """把 http(s) 链接交给系统默认浏览器。

        🔴 不这么做的话它会在 Nano 自己的 WebView 里打开 —— 而这个窗口没有
           后退按钮，用户就被困在那个网页里了。
        ⚠️ 只放行 http/https。`os.startfile` 会按协议交给系统处理，
           而系统认得的协议远不止这两个（`file:` 能开本地文件，
           自定义协议能拉起任意已注册的程序）——
           📌 **一个把字符串交给操作系统去解释的调用，白名单是必需的，不是保险。**
           这跟 `_OPENABLE_EXTS` 那个白名单是同一条理由。
        """
        import webbrowser
        url = ((getattr(e, "args", None) or {}).get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            logger.warning(f"[UI] 拒绝打开非 http(s) 链接: {url[:80]}")
            return
        try:
            # ⚠️ 用 webbrowser 而不是 os.startfile：前者只做"打开网址"这一件事，
            #    后者是通用的 shell 执行，能被更多东西利用。
            webbrowser.open(url, new=2)
        except Exception as err:
            logger.warning(f"[UI] 打开链接失败 {url[:80]}: {err}")
            ui.notify('打不开这个链接', type='warning', icon='link_off')

    def _on_reveal_local_file(self, e) -> None:
        raw = (getattr(e, "args", None) or {}).get("path") or ""
        pth = self._resolve_local_path(raw)
        if pth is None:
            ui.notify(f'文件不在了：{raw}', type='warning', icon='link_off')
            return
        self._reveal_in_explorer(pth)

    def _on_quote_selection(self, e) -> None:
        """选中聊天区文字 → 右键 replay。**复用引用待审卡那整套状态与 UI。**"""
        txt = ((getattr(e, "args", None) or {}).get("text") or "").strip()
        if not txt:
            return
        # ⚠️ 折行的选区在引用条里读起来很碎，压成单行；截断交给 _set_reply_target
        txt = " ".join(txt.split())
        self._set_reply_target(None, txt, kind=self.QUOTE_SELECTION)
        # 引用完把光标送回输入框 —— 用户的下一个动作一定是打字。
        # ⚠️ 走 JS 而不是 element.run_method('focus')：composer 是 Quasar 的
        #    q-input，真正的焦点目标是它内部那个 textarea。
        try:
            ui.run_javascript(
                "const e=document.querySelector('.composer-input-row textarea')"
                "||document.querySelector('.composer-input-row input');"
                "if(e) e.focus();")
        except Exception:
            pass

    def _open_chat_search(self):
        """打开搜索浮层。**只是让浏览器把它显示出来**，搜索本身全在前端。"""
        self._js_fire('window.__nanoSearchOpen && window.__nanoSearchOpen()')

    # ══════════════════════════════════════════════════════════════════════
    # 二级设置面板（2026-08-14 立的架子）
    # ══════════════════════════════════════════════════════════════════════
    #
    # ⭐ 用户早就想做这件事：**不想把「个人信息」这类东西平铺在下拉菜单里**，
    #    而是像 Claude Code 那样开一个二级面板，左侧是分类、右侧是内容。
    #    往后人格切换之类的东西就有地方放了。
    #
    # ⚠️ **这一轮只立架子，不搬家具** —— 现有的「个人信息 / 环境配置 / 用量限额 /
    #    OS 权限 / MCP 连接」原样留在下拉菜单里。
    #    📌 立架子和搬家具一起做，出了问题就分不清是架子的锅还是搬的锅。
    # ⚠️ 第三个字段是图标：原来四个 tab 全都画 `tune`（2026-08-29：
    #    「四个长得一毛一样的按钮」）。
    #    📌 **四个一样的图标 = 没有图标**，它不再帮人区分，只占位置。
    _SETTINGS_TABS = (("general", "通用", "tune"),
                      ("profile", "个人信息", "person"),
                      ("permissions", "OS 权限", "admin_panel_settings"),
                      ("mcp", "MCP 连接", "extension"),
                      ("advanced", "进阶配置", "settings_suggest"))

    def _show_settings_panel(self, tab: str = "general"):
        with self._ui_scope():
            with ui.dialog().props('persistent') as dialog, ui.card().style(
                'background:var(--nano-panel); border:1px solid var(--nano-line); border-radius:14px; '
                # 🔴🔴 **高度必须固定**，别再试着让它自适应。
                #
                # 2026-08-29 为了消掉「内容放得下却有滚动条」，把它改成
                # `height:auto; min-height:430px`，结果 用户当场发现：
                # **切 tab 时整个面板忽大忽小**。
                # 📌 两个问题被合成了一个解法，于是制造了第三个 ——
                #    而第三个（面板跳变）比原来那个（多一条滚动条）难受得多。
                # 📌 **一个左侧带导航的面板，它的外框是用户的空间参照物** ——
                #    参照物会动，人就得每次重新找一遍东西在哪。
                'padding:0; width:760px; max-width:92vw; height:520px; max-height:86vh; '
                'overflow:hidden;'
            ):
                with ui.row().classes('no-wrap w-full h-full').style('gap:0;'):
                    # ── 左侧：分类 ──
                    with ui.column().classes('h-full').style(
                        'width:180px; flex-shrink:0; background:var(--nano-panel); '
                        'border-right:1px solid var(--nano-border); padding:14px 8px; gap:2px;'
                    ):
                        ui.label('设置').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-faint); padding:0 10px 8px; '
                            'font-family:var(--nano-mono);')
                        _body = {}
                        _tabs = {}

                        def _pick(key):
                            # ⚠️ 三元组了，不能再 `dict(...)` —— 那会把图标当成 key。
                            _title.set_text(next(
                                (_l for _k2, _l, _ in self._SETTINGS_TABS if _k2 == key), ''))
                            for k, col in _body.items():
                                col.set_visibility(k == key)
                            for k, row in _tabs.items():
                                row.style('background:rgba(var(--nano-fg-rgb), 0.07);' if k == key
                                          else 'background:transparent;')

                        for _k, _label, _icon in self._SETTINGS_TABS:
                            with ui.row().classes('items-center gap-2 w-full cursor-pointer') \
                                    .style('padding:7px 10px; border-radius:7px;') as _row:
                                ui.icon(_icon).style('font-size:var(--nano-fs-xl); color:var(--nano-fg-soft);')
                                ui.label(_label).style('font-size:var(--nano-fs-base); color:var(--nano-fg);')
                            _row.on('click', lambda k=_k: _pick(k))
                            _tabs[_k] = _row

                    # ── 右侧：标题栏（固定）+ 内容（滚动）──
                    #
                    # 🔴 改造前**整块**都是 `overflow-y:auto`，标题栏和右上角的 ✕
                    #    在同一个滚动容器里 —— 2026-08-29：「叉子不应该随着
                    #    滚动条下拉被滚上去看不到」。MCP 那页内容一长就复现。
                    # 📌 **一个「随时可以退出」的控件，不能待在会滚走的容器里。**
                    #    ⇒ 滚动条挂到内容那一层，标题栏留在外层不动。
                    with ui.column().classes('h-full').style(
                        'flex:1; min-width:0; gap:0; overflow:hidden;'
                    ):
                        with ui.row().classes('w-full items-center justify-between').style(
                                'padding:14px 20px 8px; flex-shrink:0;'):
                            # 🔴 原来这里写死「通用」—— 只有一个 tab 的时候看不出来，
                            #    加第二个 tab 的当场就会露馅：切过去了标题还写着通用。
                            #    📌 一个写死的值只在"只有一种情况"时看起来是对的。
                            _title = ui.label('通用').style('font-size:var(--nano-fs-lg); color:var(--nano-fg); font-weight:500;')
                            ui.button(icon='close', on_click=dialog.close) \
                                .props('flat round dense size=sm') \
                                .style('color:var(--nano-dim) !important;')
                        # ⚠️ 滚动只发生在这一层里。
                        with ui.column().classes('w-full gap-0 nano-settings-pane').style(
                                # ⚠️ **横向 padding 只在这一层给** —— 各个内容区
                                #    自己带的横向 padding 已经去掉，否则叠成两层。
                                #    📌 padding 该由容器给一次，不该每层都给。
                                'flex:1; min-height:0; overflow-y:auto; padding:0 20px 10px;'):
                            with ui.column().classes('w-full gap-0') as _c_general:
                                self._build_settings_general()
                            _body["general"] = _c_general
                            # ⭐ 个人信息单开一页：**内容最多的一个，不该被塞进一行**
                            #    （2026-08-29 一眼看出来的）。
                            with ui.column().classes('w-full gap-0') as _c_profile:
                                self._build_settings_profile()
                            _body["profile"] = _c_profile
                            # ⭐ 试搬第一个：OS 权限。
                            #    内容区原样搬 —— 这次只回答"照搬能不能看"，不重新设计。
                            with ui.column().classes('w-full gap-0') as _c_perm:
                                self._build_settings_permissions()
                            _body["permissions"] = _c_perm
                            # ⭐ MCP 连接单开一页：要反复查看状态、开关、展开详情、
                            #    粘 JSON 加新的 —— 用户在这儿待得久。
                            with ui.column().classes('w-full gap-0') as _c_mcp:
                                self._build_settings_mcp()
                            _body["mcp"] = _c_mcp
                            # ⭐ 进阶配置：Nano 内部三个角色（压缩提炼 / 命令检查 / 视觉输入）
                            #    各用哪个模型。⚠️ 单开一页而不是塞进「通用」——
                            #    它是**换厂商时才会碰**的东西，跟日常设置不同频。
                            with ui.column().classes('w-full gap-0') as _c_adv:
                                self._build_settings_advanced()
                            _body["advanced"] = _c_adv
                _pick(tab)
            dialog.open()

    def _vendor_model_options(self) -> dict:
        """头栏主模型下拉的选项：**跟着当前厂商走**。

        ```
        ① 当前厂商的模型（厂商表 models 的顺序 = 价格升序，手排）
        ② 与端点 Models API 拉到的清单取交集 —— 端点没有的就不列
        ③ 两者都拿不到 → 回落内置 CLAUDE_MODELS（老行为，保证永远有得选）
        ```
        ⚠️ ② 只做**过滤**，不拿端点的全量当权威：某中转返回 71 个模型，
           其中绝大多数我们没有价格/窗口/角色能力的声明。
           📌 **「端点有」不等于「我们支持」** —— 支持与否由厂商表说了算。
        ⚠️ 拉不到清单时**不过滤**（而不是过滤成空）——
           断网不该让下拉变空。同 `load()` 那条「读不到就退回默认，绝不失能」。
        """
        try:
            from core.models import load as _mload
            _vendor = (getattr(self.provider, "vendor", "")
                       or os.environ.get("NANO_API_VENDOR") or "anthropic").lower()
            _models = list(((_mload().get(_vendor) or {}).get("models") or {}).keys())
            if not _models:
                raise ValueError("厂商表里没有该厂商的模型")
            try:
                from core.provider import endpoint_models
                _live = endpoint_models(
                    (os.environ.get("NANO_API_RELAY_BASE_URL") or "").strip(),
                    (os.environ.get("NANO_API_RELAY_API_KEY") or "").strip(),
                    _vendor)
                if _live:
                    _filtered = [m for m in _models if m in _live]
                    if _filtered:
                        _models = _filtered
            except Exception:
                pass          # 拉不到就不过滤 —— 断网不该让下拉变空
            _names = {m["id"]: m["name"] for m in (CLAUDE_MODELS or [])}
            return {m: _names.get(m, m) for m in _models}
        except Exception as e:
            logger.debug(f"[Model] 按厂商取模型清单失败，回落内置表: {e}")
            return {m["id"]: m["name"] for m in (CLAUDE_MODELS or [])}

    def _turn_tok_suffix(self, tok_str: str) -> str:
        """每条消息尾部那行的 token 部分，按用户选的档位给。

        ```
        off     ""                          只剩 "3s"
        tokens  " · 4.8K tok"
        full    " · 4.8K tok · cache hit 54%"
        ```
        ⚠️ cache hit 量不到时**不显示那一段**，而不是显示 0% ——
           📌 一个"我不知道"渲染成 0% 会被当成真数据。
        """
        mode = self._token_counter_mode()
        if mode == "off":
            return ""
        out = f" · {tok_str} tok"
        if mode == "full":
            try:
                hit = usage_tracker.turn_cache_hit()
                if hit is not None:
                    out += f" · cache hit {int(hit * 100)}%"
            except Exception:
                pass
        return out

    def _token_counter_mode(self) -> str:
        """Token 计数器的显示档位：`off` / `tokens` / `full`。**默认 off**。

        📌 token 数对新用户没有参照系 —— 他不知道 4.8K 是多是少，只会觉得贵。
        ⚠️ 这也是「那行数字吓人」的根治：不是把数字改小，是默认不摆在用户面前。
        """
        import json as _json
        import pathlib as _pl
        try:
            p = _pl.Path(__file__).parent / "data" / "throttle_config.json"
            if p.exists():
                v = str(_json.loads(p.read_text(encoding="utf-8")).get("_token_counter") or "")
                if v in ("off", "tokens", "full"):
                    return v
        except Exception:
            pass
        return "off"

    def _save_token_counter_mode(self, mode: str) -> None:
        """落盘 + 立刻刷新监控卡（不需要重启）。"""
        import json as _json
        import pathlib as _pl
        try:
            p = _pl.Path(__file__).parent / "data" / "throttle_config.json"
            d = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            d["_token_counter"] = mode
            p.write_text(_json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            self._refresh_token_card()
        except Exception as e:
            logger.warning(f"[Usage] 保存 Token 计数器设置失败: {e}")

    def _refresh_token_card(self) -> None:
        """按当前档位刷新监控卡里的「今日 Token」。

        ⚠️ cache hit 拿不到时（本次运行还没发过请求）**不显示那一段**，
           而不是显示 0% —— 📌 一个"我不知道"渲染成 0% 会被当成真数据。
        """
        lbl = getattr(self, "token_lbl", None)
        if lbl is None:
            return
        try:
            mode = self._token_counter_mode()
            if mode == "off":
                # 2026-08-31：「不显示」不要真空白，显示 `--`。
                # ⭐ 空白读起来像"坏了"，占位符读起来像"关着"。
                lbl.set_text("--")
                return
            # ⚠️ `tokens` 和 `full` 两档在监控卡上**一样** —— 那张卡本来就叫
            #    「今日 Token」，cache hit 是给【每条消息那行】用的参照系。
            _in, _out = usage_tracker.today_input_output()
            txt = _fmt_tokens(_in + _out)
            lbl.set_text(txt)
        except Exception:
            pass

    def _build_settings_advanced(self):
        """「进阶配置」页：三个内部角色各自用哪个模型。

        ⚠️ 这三项**不是**主模型 —— 主模型在头栏那个选择器里。这里管的是
           Nano 内部三个功能各自调用的模型：压缩提炼 / 命令检查 / 视觉输入。
        📌 下拉的选项**只来自 `role_pool()`** —— 用户选不出一个干不了这活的
           模型（DeepSeek 只有一个型号有视觉，那一项就只有一个选项）。
        ⚠️ 池子为空 = 还没配 API Key，或接的是尚未适配的厂商。
           这个状态**真的会出现**：没 key 时 Nano 照常启动、停在未配置态。
           ⇒ 那时候整页禁用并说清为什么，不要给一个点了没反应的空下拉。
        """
        from core.models import ROLES, role_label, role_pool, model_for_role

        _main = getattr(self.provider, "target_model", "") or ""
        _pools = {r: role_pool(_main, r) for r in ROLES}
        _any = any(_pools.values())

        if not _any:
            # ⚠️ 空态要说清**为什么空**和**怎么才能不空** ——
            #    📌 一个什么都不解释的空下拉，用户会当成 bug 来报。
            ui.label("还没有可选的模型 —— 先在「通用 → 环境配置」里填好 API Key。").style(
                "font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.7;")
            return

        _DESC = {
            "distiller": "将历史对话提炼为结论条目，以压缩上下文占用，建议选用低成本模型。",
            "classifier": "在 auto 模式下，判定 Nano 执行的命令是否属于与意图不符的危险命令，并适时拦截，建议选用低成本模型。",
            "vision": "处理多模态输入，如图片和 PDF 扫描件。",
        }
        for _role in ROLES:
            _pool = _pools[_role]
            _cur = model_for_role(_main, _role)
            with ui.row().classes("w-full items-center justify-between no-wrap").style(
                    "padding:14px 0; gap:20px;"):
                with ui.column().classes("gap-1 min-w-0").style("flex:1;"):
                    ui.label(role_label(_role)).style(
                        "font-size:var(--nano-fs-md); color:var(--nano-fg);")
                    ui.label(_DESC.get(_role, "")).style(
                        "font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.6;")
                if not _pool:
                    # 单个角色没池子（比如某厂商没有能看图的型号）
                    ui.label("该厂商暂无可用模型").style(
                        "font-size:var(--nano-fs-sm); color:var(--nano-dim); flex-shrink:0;")
                    continue
                # ⚠️ 只有一个选项时也照样给下拉（禁用态）——
                #    📌 换成纯文字的话，用户不知道"这里本来是可以选的"。
                _opts = {m: (CLAUDE_MODEL_MAP.get(m, {}).get("name") or m) for m in _pool}
                # ⚠️ 写法**照抄语言下拉**，两处细节都不能改：
                #  · `popup-content-class` 要放进 **props** —— 弹层是挂在 body 上的
                #    另一个 DOM 节点，类写在 `.classes()` 上根本到不了它，
                #    结果就是"背景透明、样子很奇怪"。
                #  · 用 `on_change=` 而**不是** `.on('update:model-value', …)` ——
                #    后者是底层事件、不受值同步的节流约束，会读到还没同步完的值
                #    （见 t_ui_edge_l25_l27 里那条红线，滑块上真栽过）。
                ui.select(_opts, value=_cur,
                          on_change=lambda e, role=_role: self._save_role_model(role, e.value))                     .props("outlined dense options-dense "
                           "popup-content-class=nano-select-popup"
                           + (" disable" if len(_pool) == 1 else ""))                     .classes("lang-select-field")                     .style("min-width:170px; flex-shrink:0;")

    def _save_role_model(self, role: str, model_id: str) -> None:
        """把「进阶配置」里的选择写进 throttle_config.json 的 `_role_models`。

        📌 **用户选择和厂商事实分两张表**：厂商能力表（model_config.json）跟版本走，
           用户这一份跟用户走。混在一起的话，升级厂商表会冲掉用户的选择。
        ⚠️ 写完立刻生效 —— 三个角色都是**每次用的时候现问** `model_for_role()`，
           没有缓存，所以不需要重启，也不需要通知谁。
        """
        import json as _json
        import pathlib as _pl
        try:
            cfg = _pl.Path(__file__).parent / "data" / "throttle_config.json"
            data = _json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
            data.setdefault("_role_models", {})[role] = model_id or ""
            cfg.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            from core.models import role_label as _rl
            ui.notify(f"{_rl(role)} → {model_id}", type="positive", icon="tune")
            logger.info(f"[Roles] {role} = {model_id}")
        except Exception as e:
            ui.notify(f"保存失败: {e}", type="negative")
            logger.warning(f"[Roles] 保存 {role} 失败: {e}")

    def _build_settings_general(self):
        """「通用」页：语言 / 用量限额 / 环境配置 / 导出数据。"""
        # ⚠️ **这一页没有任何分隔线**：每一项的标题本身
        #    就是天然分隔符，再画一条线是同一件事说两遍。
        #    📌 **分隔线是给「看不出边界」的东西用的** —— 边界已经清楚时，
        #       它只是多一道横杠。
        # ⭐ 2026-08-29 定：用量限额 + 环境配置**并进通用**，
        #    不各自单开一页。理由不是「懒得分」，是**信息密度**：
        #    它们本身很小，单开一页会让空白区远大于内容区。
        #    📌 一页的粒度该由内容的体量决定，不由它在菜单里曾经是不是一项决定。
        # ══ 语言（i18n 前置，2026-08-14）═════════════════════════════════
        #
        # ⚠️⚠️ **它现在只影响【模型侧】，不影响界面文案。**
        #    界面 i18n（约 34 处硬编码 + 全部中文 UI）是**另一件事**，不在这一步的范围里。
        #    这里立的是**「当前语言」这个事实的唯一出处**
        #    （`core/i18n.py`）—— 见那个模块头的「将来的 UI i18n 怎么接到这里」。
        #
        # ⭐ 为什么这一半值得**先**做完，而不是等 UI i18n 一起上：
        #    2026-08-14 实测发现记忆起点卡上的线索**全是英文** ——
        #    提炼器提示词是英文，于是它读中文原话、写英文结论，再显示给中文用户。
        #    📌 那不只是"不好看"：**提炼器读中文写英文，等于凭空多做一次有损翻译。**
        # 📌 一件今天就能做完的事，不该被压在一件几天的工程后面。
        from core import i18n as _I18N
        # ⚠️ `items-center` 而不是 `items-start`（2026-08-14：「明显偏上」）：
        #    左边是「标题 + 两行说明」，右边是一个单行控件。顶对齐时控件贴着
        #    标题那一行，视觉上就吊在整块的上沿。
        # 📌 **一个控件该对齐的是它旁边那一【块】，不是那一块的第一行。**
        with ui.row().classes('w-full items-center justify-between no-wrap').style(
            'padding:14px 0; gap:20px;'
        ):
            with ui.column().classes('gap-1 min-w-0').style('flex:1;'):
                ui.label('语言').style('font-size:var(--nano-fs-md); color:var(--nano-fg);')
                # ⚠️ 说清它**现在管什么、还不管什么** —— 📌 一个只做了一半的开关，
                #    如果文案说得像做全了，用户会把"界面没变"当成 bug 来报。
                ui.label('调整 Nano 生成语言时的偏好，'
                         '当前界面不受影响，固定为简体中文。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.6;')
            _opts = {k: v["label"] for k, v in _I18N.LANGS.items()}
            # ⚠️⚠️ **`popup-content-class` 不是可选项** —— 全局有一条
            #    `.q-menu { background: transparent }`（见那条注释：清掉 Quasar
            #    自带的深灰容器，让内层卡片自己的背景说了算）。
            #    于是**任何没有显式给弹层一个类的下拉，弹出来就是透明的**。
            #    📌 「这个 bug 之前做 UI 重复无数次了」—— 它会重复，是因为
            #       那条全局规则把「有背景」变成了**每个下拉各自的责任**，
            #       而责任一旦分散到 N 处，就一定会漏掉第 N+1 处。
            #    ⭐ 用 `nano-select-popup` —— **只管样子的那个共享类**。
            # 🔴 上一版图省事直接复用了 `model-select-popup`，结果**连模型下拉的
            #    tooltip 一起抄了过来**：鼠标停在「简体中文」上弹出「均衡旗舰 ·
            #    强推理 · 多模态」（那段 JS hook 的就是 `.model-select-popup .q-item`，
            # 📌 **复用一个类，继承的不只是它的样子，还有所有挂在它身上的行为** ——
            #    而行为不写在样式表里，翻 CSS 是看不见它的。
            ui.select(_opts, value=_I18N.current_lang(),
                      on_change=lambda e: _I18N.set_lang(e.value)) \
                .props('outlined dense options-dense '
                       'popup-content-class=nano-select-popup') \
                .classes('lang-select-field') \
                .style('min-width:150px; flex-shrink:0;')

        # ── 环境配置（2026-08-29：并进通用，「一行 + 配置」）─────────────
        #
        # 🔴 **这一格是「即时生效」的例外，而且例外有理由，不是懒得改。**
        #    即时生效适用于**离散选择**（开关 / 步进器 / 下拉）；
        #    而这里是**连续输入** —— 每敲一个字符就写盘 + 重新初始化 provider，
        #    `sk-ab` 不是一个想要的值，它只是打字打到一半。
        # 📌 更硬的理由：API Key 填完之后要**试着初始化 provider** 才知道对不对。
        #    那个验证什么时候跑？⇒ **一个需要验证才知道对不对的输入，
        #    必须有一个「我填完了」的动作。**
        # ⭐ 2026-08-29 定的：**保存按钮保留**，理由很直接 ——
        #    「限额能去掉是因为用户不可能填进去非法内容，但这里不行：
        #      用户在 key 里面填错了都没人告诉他错了」。
        #    📌 **一个填错了不会当场露馅的输入，必须有一个「我填完了」的动作** ——
        #       否则「告诉他错了」这件事永远等不到该发生的时刻。
        # ⚠️ 弹窗本身**一个像素都不改**（直接复用目前那个样子）。
        # ⭐ 另：`.env` 缺 key 时启动会**直接弹这个配置页**
        #    （`if not self.provider.is_configured: ui.timer(… _show_env_config_dialog …)`），
        #    **不经过设置面板** —— 这条路径本来就对，搬架子没碰它。
        #    📌 一个「第一次开机就撞上」的入口，不该要求用户先学会怎么开设置。
        with ui.row().classes('w-full items-center justify-between no-wrap').style(
            'padding:14px 0; gap:20px;'
        ):
            with ui.column().classes('gap-1 min-w-0').style('flex:1;'):
                ui.label('环境配置').style('font-size:var(--nano-fs-md); color:var(--nano-fg);')
                ui.label('配置 API Key、中转地址与网络代理。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.6;')
            # ⚠️ 这个按钮叫「配置」不叫「管理」——
            #    📌 限额是**已有的东西在调整**（管理），而这里可能是**第一次填**（配置）。
            ui.button('配置', icon='tune', on_click=self._show_env_config_dialog) \
                .props('flat dense no-caps') \
                .style('color:var(--nano-fg) !important; background:rgba(var(--nano-fg-rgb), 0.07); '
                       'border:1px solid rgba(var(--nano-fg-rgb), 0.20); border-radius:8px; '
                       'padding:4px 14px; font-size:var(--nano-fs-base); flex-shrink:0;')

        # ── 用量限额（2026-08-29：只留一行入口，内容进二级小弹窗）──────
        #
        # ⭐ 第一版把整段内容直接摊在通用页里，实测看了一眼：「太丑了，
        #    全挤在这个页面」。⇒ 改成**一行入口 + 「管理」按钮**，形状与上面
        #    「导出数据」那一行完全一致。
        # 📌 判据：**一页里每一项的高度应该差不多。** 一项撑得比别人高好几倍，
        #    读的人会以为那是主角 —— 而它只是恰好控件多。
        # ⚠️ 这**不是**退回「下拉菜单 + 独立弹窗」：入口在设置页里、弹窗是二级、
        #    而且里面即时生效没有保存按钮。层级变了，交互没变回去。
        with ui.row().classes('w-full items-center justify-between no-wrap').style(
            'padding:14px 0; gap:20px;'
        ):
            with ui.column().classes('gap-1 min-w-0').style('flex:1;'):
                with ui.row().classes('items-center gap-1'):
                    # ⚠️ `line-height:1` 是对齐问号的关键 —— 标签默认行高比字号大，
                    #    图标按自己的行盒居中，两者的中线就错开了（增强模式那处同款）。
                    ui.label('每日用量限额').style(
                        'font-size:var(--nano-fs-md); color:var(--nano-fg); line-height:1;')
                    # ⚠️ 说清这是【估算】—— 否则用户拿它跟厂商后台对不上时会以为记账坏了。
                    #    实测同一批请求：Nano ¥0.017 / 厂商后台 ¥0.02。
                    #    差的部分（分时定价、缓存独立单价、阶梯折扣）我们算不了也不该算。
                    with ui.element('div').style(
                            'flex-shrink:0; display:flex; align-items:center;'):
                        ui.icon('help_outline').style(
                            'font-size:var(--nano-fs-lg); color:var(--nano-dim); '
                            'cursor:help; line-height:1;')
                        ui.tooltip('消耗金额为估算，实际金额以官方后台计费为准。').style(
                            'font-size:var(--nano-fs-sm); max-width:280px;')
                ui.label(f'今日已用 {_cur()}{usage_tracker.today_cost():.3f}。'
                         '达到软上限时发出警告，达到硬上限时停止发送请求。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.6;')
            ui.button('管理', icon='tune', on_click=self._show_cost_cap_dialog) \
                .props('flat dense no-caps') \
                .style('color:var(--nano-fg) !important; background:rgba(var(--nano-fg-rgb), 0.07); '
                       'border:1px solid rgba(var(--nano-fg-rgb), 0.20); border-radius:8px; '
                       'padding:4px 14px; font-size:var(--nano-fs-base); flex-shrink:0;')

        # ── Token 计数器─────────────────────
        # 📌 交给用户自己定：有人要看用量、有人嫌它吵。
        #    而 cache hit 那一档存在的理由是**参照系** ——
        #    深度求索上 fresh 天然比 Anthropic 高（两家缓存模型不同），
        #    只给一个大数字会让人以为"换了厂商就变贵了"。
        with ui.row().classes('w-full items-center justify-between no-wrap').style(
                'padding:14px 0; gap:20px;'):
            with ui.column().classes('gap-1 min-w-0').style('flex:1;'):
                ui.label('Token 计数器').style(
                    'font-size:var(--nano-fs-md); color:var(--nano-fg);')
                ui.label('调整 Token 计数器的显示方式。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.6;')
            _tc_opts = {'off': '不显示',
                        'tokens': '仅 Token',
                        'full': 'Token + cache hit'}
            _tc_cur = self._token_counter_mode()
            ui.select(_tc_opts, value=_tc_cur,
                      on_change=lambda e: self._save_token_counter_mode(e.value)) \
                .props('outlined dense options-dense '
                       'popup-content-class=nano-select-popup') \
                .classes('lang-select-field').style('min-width:170px; flex-shrink:0;')

        with ui.row().classes('w-full items-center justify-between no-wrap').style(
            'padding:14px 0; gap:20px;'
        ):
            with ui.column().classes('gap-1 min-w-0').style('flex:1;'):
                ui.label('导出数据').style('font-size:var(--nano-fs-md); color:var(--nano-fg);')
                # ⚠️ 说清**导出的是什么**，尤其是"包括已经重置掉的" ——
                #    📌 重置弹窗写的是"放弃"不是"删除"，这里就必须对得上。
                ui.label('将数据库中保留的全部对话记录导出至指定目录，'
                         '含已重置的历史会话。').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-dim); line-height:1.6;')
            # ⚠️ 就叫「导出」：按钮上写"选择文件夹"是在描述**过程**，
            #    而按钮应该说**结果**。📌 用户点它是为了拿到数据，不是为了选一个文件夹。
            #    样式跟项目里其他主按钮对齐（琥珀描边 + 深底），不用 Quasar 默认那套。
            ui.button('导出', icon='download', on_click=self._do_export_data) \
                .props('flat dense no-caps') \
                .style('color:var(--nano-fg) !important; background:rgba(var(--nano-fg-rgb), 0.07); '
                       'border:1px solid rgba(var(--nano-fg-rgb), 0.20); border-radius:8px; '
                       'padding:4px 14px; font-size:var(--nano-fs-base); flex-shrink:0;')

        # 🪦 这里原来是「个人信息 + 编辑」一行 —— 已改成左侧独立 tab。
        #    📌 内容最多的那个被放进最小的容器，是把「用户待多久」这条判据
        #       用过了头：它确实填一次就不动，但**一次要填十几个字段**。
    async def _pick_folder(self) -> str:
        """弹 Windows 选文件夹对话框。取消/失败返回空串。

        ⚠️ 走 pywebview 的原生对话框（本 app 就是 native 模式跑的），
           不引 tkinter：📌 **为一个选路径的框引入第二套 GUI 栈不值得**，
           而且 tkinter 在这个进程里还得自己开线程躲事件循环。

        🔴 **必须 `await`**（2026-08-14 实测：`'coroutine' object is not subscriptable`）：
           `nicegui.app.native.main_window` **不是** pywebview 的 Window 对象本身，
           而是 NiceGUI 的 `WindowProxy` —— 它把每个方法都包成协程，
           丢进 pywebview 自己那个线程里跑再把结果送回来。
           ⚠️ 不 await 拿到的是协程对象，`res[0]` 当场炸。
           📌 **一个"看起来就是那个对象"的代理，最容易骗过人的地方就是方法签名。**
        """
        try:
            import webview
            from nicegui import app as _napp
            # 🔴🔴 **必须用 `FileDialog.FOLDER`，不能用 `webview.FOLDER_DIALOG`**
            #    （2026-08-14 实测：`PicklingError: Can't pickle <function FOLDER_DIALOG…>`）。
            #
            #    `FOLDER_DIALOG` 是新版 pywebview 留的**弃用垫片**，实际类型是
            #    `proxy_tools.Proxy`（一个惰性代理），而 `FileDialog.FOLDER` 是真枚举（值 20）。
            #    ⚠️ NiceGUI 的 `WindowProxy` 把这次调用**跨进程队列**送去 pywebview 那个进程，
            #    所以**每个参数都必须可 pickle** —— 代理对象过不去。
            #
            # 📌 **一个"为了兼容"而存在的代理对象，在跨进程边界上就是地雷**：
            #    它在同一个进程里和真值表现得一模一样，**一过队列才炸**，
            #    而报错指向 pickle，跟"选文件夹"看起来毫无关系。
            # ⚠️ 兼容老版 pywebview（没有 FileDialog 枚举）时才退回常量。
            try:
                _kind = webview.FileDialog.FOLDER
            except AttributeError:
                _kind = 20      # 老版里 FOLDER_DIALOG 就是这个整数
            res = await _napp.native.main_window.create_file_dialog(_kind)
            return str(res[0]) if res else ""
        except Exception as e:
            logger.warning(f"[Export] 打开文件夹选择框失败: {e}")
            ui.notify(f'打不开文件夹选择框：{e}', type='negative')
            return ""

    async def _do_export_data(self):
        _dir = await self._pick_folder()
        if not _dir:
            return          # 用户取消 —— 不弹任何提示，取消不是错误
        try:
            from core.runtime.export import export_all
            from core.runtime.kernel import get_kernel
            stat = export_all(_dir, get_kernel())
            ui.notify(f"已导出 {stat['sessions']} 段会话 / {stat['messages']} 条消息"
                      + (f" / {stat['images']} 张图片" if stat["images"] else "")
                      + f" → {stat['dir']}", type='positive', icon='download_done')
        except Exception as e:
            logger.exception("[Export] 导出失败")
            ui.notify(f"导出失败：{e}", type='negative')

    def _confirm_reset_conversation(self):
        """重置对话确认弹窗。"""
        with self._ui_scope():
            with ui.dialog() as dialog, ui.card().style(
                'background:var(--nano-panel); border:1px solid rgba(var(--nano-danger-rgb),0.2); border-radius:16px; min-width:360px; padding:24px;'
            ):
                with ui.row().classes('items-center gap-3 mb-3'):
                    ui.icon('restart_alt').classes('text-rose-400 text-[20px]')
                    ui.label('重置当前对话').classes('text-slate-200 text-[14px] font-medium')
                # ⚠️⚠️ 文案是 2026-08-14 定的：**「完全放弃全部之前的会话」，
                #    干脆一点最好。** 理由不是措辞洁癖，是**产品结构**：
                #    > 「你不这么做，你还得加一个『清除历史对话』的按钮，
                #    >   这会让人困惑 —— 重置对话删的是哪些？清理历史又是哪些？」
                #    📌 **一个动作只留一个出口，用户才不用先搞清楚两个出口的分界。**
                #
                # 🔴 旧文案「将清空全部对话历史和待处理状态。Skill 文件和知识库不受影响。」
                #    两处都错：前半句**没说清到底清掉了什么**（记忆？技能？），
                #    后半句是**纯废话**（没人会以为重置对话会删技能）。
                # ⚠️ 措辞用「放弃」而不是「删除/不可恢复」—— 落盘账本里那些会话**还在**，
                #    只是不再有入口。📌 **别承诺一件没做的事，哪怕听起来更利落。**
                ui.label('完全放弃之前的全部会话：聊天记录、排队中的消息、'
                         'Nano 对这段对话的记忆，一并清空，之后从零开始。').classes(
                    'text-slate-500 text-[12px] mb-4'
                )
                with ui.row().classes('justify-end w-full gap-2'):
                    ui.button('取消', on_click=dialog.close).props('flat').classes('text-slate-400')
                    ui.button('确认重置', on_click=lambda: self._do_reset_conversation(dialog)).props(
                        'unelevated color=negative'
                    )
            dialog.open()

    def _do_reset_conversation(self, dialog):
        if self.pipeline_lock.locked():
            dialog.close()
            ui.notify('当前有任务正在执行，请等待完成后再重置', type='warning')
            return
        # ⭐ 重置对话 = **用户显式**要求丢掉排队的消息。
        # ⚠️ 这是**唯一**允许丢弃 inbox 消息的路径。
        #    📌 **用户自己决定丢，和系统悄悄丢，是两件事** —— 账上要能分开，
        #       所以记成 DISCARDED 而不是删掉。
        try:
            from core.runtime import inbox as _ib
            _n = _ib.discard_all_pending("用户重置对话")
            self._rt_inbox_parked.clear()
            if _n:
                logger.info(f"[Inbox] 重置对话 → 丢弃 {_n} 条排队消息（用户显式要求）")
        except Exception as e:
            logger.warning(f"[Inbox] 清队列失败: {e}")
        result = self.agent.reset_conversation()
        # ⭐ 上下文厚度也跟着归位 —— **忘掉历史那部分，底噪留着**。
        #    📌 重置之后上下文回到底噪（system + 工具表），**不是回到 0**；
        #       显示 0 是一句谎话。
        try:
            from core.context.meter import forget_conversation_size, get_meter
            forget_conversation_size()
            get_meter()._anchor = None      # 本次运行的锚也作废（历史真的没了）
            # 🔴 **必须立刻重画卡片**（2026-08-14 实测：用户报「重置按钮不会让
            #    上下文的数字更新，只有重置 + 重启 nano 才会」）。
            #    改数据 ≠ 改界面：`forget_conversation_size()` 只动了落盘和锚，
            #    而那张卡是**事件驱动**的（只在 `final_result` 时重画）——
            #    重置不产生 `final_result`，所以它一直显示旧值直到下一次对话。
            # 📌 **一个由事件驱动刷新的显示，在「没有事件的那条路径」上必须手动补一次。**
            self._refresh_context_card()
        except Exception as e:
            logger.debug(f"[F5] 重置上下文厚度跳过: {e}")
        dialog.close()
        # 清空聊天区
        if self.chat_container:
            self.chat_container.clear()
        # 问候语是 chat_container 的兄弟节点（不受 clear() 影响），
        # 重置后重新渲染一份新的，换上当前时段的问候语
        self._clear_empty_state_greeting()
        with self.scroll_area:
            self._render_empty_state_greeting()
        # 清空临时知识库和文件列表
        try:
            rag_engine.clear_temp_knowledge()
        except Exception:
            pass
        self._temp_files = []
        self._refresh_temp_file_badge()
        self._clear_pending_image()
        # 重置 upload 组件：NiceGUI 的 upload 在完成一次上传后会进入"已完成"状态，
        # 不 reset 的话重置对话后点上传按钮无反应。
        if self._chat_upload is not None:
            try:
                self._chat_upload.reset()
            except Exception:
                pass
        ui.notify(result.get("msg", "已重置"), type='positive', icon='restart_alt')
        if self.status_lbl:
            self.status_lbl.set_text("SYS_IDLE")
            self.status_lbl.style('color:var(--nano-ok); font-size:var(--nano-fs-sm);')
        if self.log_lbl:
            self.log_lbl.set_text("对话已重置。等待新指令...")
        # 重置会重绘问候语等容器，补一次主题防止新挂载容器把背景透明规则盖掉
        self._apply_theme_visuals()

    # ── 主题 ──────────────────────────────────────────────────────────────

    # ── 无边框窗口控制（自绘标题栏的最小/最大/关闭）──────────────────────
    def _win_minimize(self):
        try:
            from nicegui import app as _app
            _app.native.main_window.minimize()
        except Exception as e:
            logger.warning(f"[Win] minimize 失败: {e}")

    def _win_maximize_toggle(self):
        try:
            from nicegui import app as _app
            w = _app.native.main_window
            if getattr(self, '_win_maximized', False):
                w.restore(); self._win_maximized = False
            else:
                w.maximize(); self._win_maximized = True
        except Exception as e:
            logger.warning(f"[Win] maximize 失败: {e}")

    def _win_close(self):
        """✕：隐藏到系统托盘（Nano 是"电脑本身"，常驻后台不退出）。
        托盘图标可恢复/退出。Alt+仍是真退出（force-quit 兜底）。"""
        try:
            from nicegui import app as _app
            _app.native.main_window.hide()
        except Exception as e:
            logger.warning(f"[Win] hide 失败: {e}")

    def _identity_text(self) -> str:
        """头栏身份：nano@昵称；没填昵称就只显示 nano。"""
        try:
            import json as _j, pathlib as _pl
            p = _pl.Path("data/user_profile.json")
            if p.exists():
                nick = (_j.loads(p.read_text(encoding="utf-8")).get("nickname") or "").strip()
                if nick:
                    return f"nano@{nick}"
        except Exception:
            pass
        return "nano"

    def _user_label(self) -> str:
        """聊天里用户提示符：填了昵称就用昵称，没填就 you。"""
        try:
            import json as _j, pathlib as _pl
            p = _pl.Path("data/user_profile.json")
            if p.exists():
                nick = (_j.loads(p.read_text(encoding="utf-8")).get("nickname") or "").strip()
                if nick:
                    return nick
        except Exception:
            pass
        return "you"

    def _refresh_identity(self):
        if getattr(self, "_identity_label", None):
            try:
                self._identity_label.set_text(self._identity_text())
            except Exception:
                pass

    def _apply_theme_visuals(self):
        """套用主题：只设 body 类，配色全部交给 CSS 变量。

        📌 旧的浅/深色模式（大重构前那套 UI）曾在这里用几十行 JS 内联
           `setProperty(..., 'important')` 硬刷每个元素 —— 2026-08-30 删除。
           要加第二套主题时，加的是一个 `body.nano-theme-xxx {{ --var: … }}`
           变量块，不是再写一遍 JS。
        """
        _light = getattr(self, 'theme_mode', 'terminal') == 'aurora'
        ui.run_javascript(f"Quasar.dark.set({'false' if _light else 'true'})")
        ui.run_javascript(
            "document.body.classList.remove('nano-theme-terminal','nano-theme-light');"
            f"document.body.classList.add('{'nano-theme-light' if _light else 'nano-theme-terminal'}');"
            "document.body.style.removeProperty('background');"
        )

    # ── 空状态问候语（参考日间设计图）────────────────────────────────────
    # 问候语是 chat_container 的兄弟节点【且排在它后面】，由 render() 在启动时渲染
    # 一次，聊天区第一次出现内容时移除，点"重置当前对话"时重新渲染一份（换成当前
    # 时段的问候语）。
    # 注意：连接不再重置对话（见 _on_browser_connect），所以"浏览器刷新后
    # 聊天区必然是空的"这个旧前提已经不成立——刷新会保留已有的元素树。
    #
    # ⚠️ 不变量（修正）：显示与否取决于【聊天区有没有内容】，不是"有没有发过消息"。
    # 原来的措辞是"本进程内有没有发过消息"，只在 start_pipeline_task 里清——统一出口
    # （emit_chat）是第四条写入口，冷启动的故障卡/挂起提醒都不经过发消息，于是问候语
    # 会留着并被顶到消息下面。所以 _render_chat_event 里也必须清一次。
    # 结论：**任何往 chat_container 写内容的新入口，都要负责清问候语。**

    def _greeting_text(self) -> str:
        import datetime
        hour = datetime.datetime.now().hour
        if hour < 6:
            return "夜深了，需要我做点什么？"
        elif hour < 12:
            return "早上好，我能帮你处理什么？"
        elif hour < 18:
            return "下午好，我能帮你处理什么？"
        else:
            return "晚上好，我能帮你处理什么？"

    def _render_empty_state_greeting(self):
        # 终端风：左上角 boot 行（整行同一暗色），不再居中大标题、不再带副标题
        with ui.column().classes('w-full items-start') \
                .style('padding-top:10px;') as self._empty_state_greeting:
            ui.label(f'// nano-lumen v1.97 (beta) · {self._greeting_text()}').style(
                'font-size:var(--nano-fs-lg); color:var(--nano-faint); font-family:var(--nano-mono);'
            )

    @staticmethod
    def _replay_text_content(content) -> str:
        """Extract displayable text from a stored multimodal ChatMessage."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts, image_count = [], 0
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("type") in {"image", "image_url"}:
                    image_count += 1
            if image_count:
                parts.append(f"（此前发送过 {image_count} 张图片）")
            return "\n".join(parts)
        return str(content or "")

    def _open_durable_replay_response(self):
        """Open the static counterpart of one live Nano response epoch.

        A tool phase belongs after this prefix, not between the user input and the
        response that owns it.  Its content column intentionally shares the prefix
        row, exactly as the live response path does.
        """
        with self.chat_container:
            with ui.column().classes('w-full py-1 mb-8'):
                with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                    ui.label('nano ❯').style(
                        'color:var(--nano-ok); font-size:var(--nano-fs-lg); line-height:1.75rem; '
                        'flex-shrink:0; min-width:64px; text-align:right; '
                        'font-family:var(--nano-mono);'
                    )
                    response_body = ui.column().classes('w-full gap-0 min-w-0')
                    with response_body:
                        nano_md()
        return response_body

    def _replay_tool_display(self, call) -> str:
        """重放明细的工具名：重算 live 侧同一个友好显示（`检索知识库` 等），
        而不是落盘里的裸工具名。name/args 都在落盘记录里，是 (name,args) 的纯函数，
        所以不用额外持久化字段。任何异常退回裸名（best-effort UI 重建）。"""
        _name = getattr(call, "name", "") or "tool"
        try:
            # 问统一工具目录 —— live 侧那张 `_display_map` 已随 cutover 删除，
            # 两边现在读的是**同一处声明**（这正是重放要的：重算，不是抄一份）。
            return self.agent._get_tool_catalog().presentation(
                _name, getattr(call, "args", {}) or {}) or _name
        except Exception:
            return _name

    # ══════════════════════════════════════════════════════════════════
    # 工具卡的**内容层** —— live 与重放的**唯一**渲染出口
    # ══════════════════════════════════════════════════════════════════
    #
    # 🔴 改造前的缺口（早先的设计，2026-08-13 提出）：
    #    工具卡展开后**只有** `$ <工具摘要> ✓` —— 没有参数、没有结果。
    #    于是「Nano 做过什么」在聊天区**没有可回溯的载体**。
    #    ⚠️ 而 OS 授权弹窗**不能算数**，它回答的是另一个问题：
    #         弹窗 = 事前「要不要授权」，一次性，只覆盖需要确认的那些
    #         工具卡 = 事后「它具体做了什么」，与对话一样长，该覆盖全部
    #    📌 **一个 Agent 的可信度，取决于它做过的事能不能被复查，
    #       而不取决于它做之前问没问。**
    #
    # ⭐⭐ 为什么详情是**展开时才去取**，而不是在 `tool_end` 事件里带过来：
    #    `tool_end` 有 **9 个发射点**，每个都只发 `result_summary[:80]`。
    #    把全文穿过 9 个点 = 9 处各改一遍，而且漏一处不报错（只是那类工具
    #    展开后是空的）。
    #    📌 **懒加载在这里不是性能优化，是「让两条路不可能读到不同的东西」** ——
    #       live 和重放问的是同一个 `tool_use_id`、同一本账。
    #
    # 🔴🔴 **红线：只读 `conversation_messages`，绝不读 `MemoryManager.storage`。**
    #    storage 是 的投影 —— 它一把那段降到 L1，工具结果就变成
    #    `[Tool output aged out...]` 占位符了。读错账本的表现是：
    #    **历史工具卡集体变成占位符，而且不报错**，透明度当场白做。
    #    ⭐ 这不是巧合，是两件事的方向本来就相反：
    #       管「给模型留多少」，管「给用户留多少」。

    def _ledger_tool_record(self, tool_use_id: str):
        """按 `tool_use_id` 从**落盘账本**取 `(工具名, 参数, 结果)`。

        ⭐⭐ **参数也从这里取，不从 live 事件里穿过来。**
           tool_start 事件里有个 `action_input: str(args)[:200]`，看着能用 ——
           但那样参数就有了**两个来源**（live 走事件、重放走账本），
           📌 而两个来源只会在"我两次想法相同"的前提下一致。
           这一轮已经被这个形状咬过两次（的计量 vs 投影、live vs hydrate）。

        ⚠️ 缓存**miss 时重建** —— live 侧刚跑完的那条比缓存新。
           📌 一个只建一次的索引，会让"刚发生的事"永远查不到。
        """
        if not tool_use_id:
            return "", {}, None

        def _pick(idx):
            _c = idx.get(("call", tool_use_id))
            _r = idx.get(("res", tool_use_id))
            _n = getattr(_c, "name", "") or getattr(_r, "name", "") or ""
            return _n, (getattr(_c, "args", None) or {}), _r

        _cache = getattr(self, "_u8_ledger_index", None)
        if _cache is not None and ("res", tool_use_id) in _cache:
            return _pick(_cache)
        try:
            _repo = self.memory.conversation_repository
            _sid = self.memory.conversation_session_id
            if _repo is None or not _sid:
                return "", {}, None
            _new = {}
            for m in _repo.load_messages(_sid):
                for tc in (getattr(m, "tool_calls", None) or []):
                    if getattr(tc, "tool_use_id", ""):
                        _new[("call", tc.tool_use_id)] = tc
                for tr in (getattr(m, "tool_results", None) or []):
                    if getattr(tr, "tool_use_id", ""):
                        _new[("res", tr.tool_use_id)] = tr
                # 旧的单工具形状（role="tool_call" / "tool"）
                if m.role == "tool_call" and getattr(m, "tool_use_id", ""):
                    _new[("call", m.tool_use_id)] = m
                elif m.role == "tool" and getattr(m, "tool_use_id", ""):
                    _new[("res", m.tool_use_id)] = m
            self._u8_ledger_index = _new
            return _pick(_new)
        except Exception as e:
            # ⚠️ **warning 不是 debug** —— 这条走通了才有透明度可言。
            #    📌 第一版写的是 `debug`，于是 `self.memory.conversation_repository`
            #       根本不存在（真名私有）这件事，表现为工具卡展开显示
            #       「没有可展示的参数或结果」—— **一句读起来完全正常的话**。
            #       一个静默失败如果还配了一句得体的兜底文案，它就永远不会被发现。
            logger.warning(f"[U8] 从账本取工具记录失败 {tool_use_id}: {e}")
            return "", {}, None

    def _attach_tool_detail(self, row, container, tool_use_id: str,
                            fallback_name: str = "", *, direct=None) -> None:
        """给一行工具明细挂展开器。**live 与重放共用这一个。**

        · `row`       —— 那一行本身；展开箭头 `›` 挂在它**右侧同一行**
        · `container` —— 那一行所在的列；详情正文落在它**下面**

        ⚠️ 形态是 2026-08-14 参照 Claude Code 定的（`+148 -0 ›` 那个样子）：
           **箭头同行、不占新行、不写「详情」两个字。**
           📌 展开控件本身就有语义，再配一个「详情」标签是**同一个词说两遍**
              （同 header 那个「三灯 = 设置」按钮不再挂悬浮提示的理由）。
           ⚠️ 第一版给它单起了一行 + 写了「详情」——
              一次工具批里有 N 个工具就白占 N 行。
              📌 一个每条记录都出现的装饰，它的成本要乘以记录条数。
        ⚠️ `fallback_name` 只在账本里那条还没落盘时用来选渲染器，
           **不参与内容** —— 📌 兜底可以影响"怎么画"，不许影响"画什么"。
        """
        _tid = tool_use_id or ""
        try:
            with container:
                _det_col = ui.column().classes('w-full gap-0').style(
                    'padding:2px 0 4px 12px; border-left:1px solid var(--nano-border); margin-left:4px;')
                _det_col.set_visibility(False)
            with row:
                # ⭐ `›` 而不是 `▸`：实心三角在这个字号下发糊，尖角雪佛龙更清楚
                #    （「看起来也美观」）。
                # ⚠️⚠️ **展开态靠换字符（`›`→`⌄`），不靠 `transform:rotate(90deg)`。**
                #    2026-08-14 实测测下来那个旋转**根本不生效**，而且不是优先级问题：
                #      · 内联 `transform:rotate(90deg)` 写进了 style 属性（读得到）
                #      · 但 `getComputedStyle` 一直是 identity
                #      · **连 JS 直接 `setProperty(..., 'important')` 也一样**
                #      · 同一元素的 `display:inline-block` 同样吃不进去（计算值仍是 block）
                #    → 不是"被某条 CSS 盖了"（扫过全部 styleSheet，没有命中它的 transform 规则），
                #      像是这棵子树的样式计算被上层掐着（`content-visibility` 一类）。
                #    ⭐ 没继续挖：这是**装饰**，而换字符零 CSS 依赖、效果一样。
                # 📌 把这段观察留下来，是为了**下一个人不用再从零试一遍那条死路**
                #    —— 一个"看起来该生效却没生效"的样式，最贵的成本是重复排查。
                # ⚠️ 对齐：`›`/`⌄` 的字号比同行文字大（14 vs 11），默认基线会
                #    把它压低一截（2026-08-15：「感觉偏下很多」）。
                #    · `line-height` 拉到和邻居一样 → 它的行盒不再更高
                #    · `position:relative; top` 做最后 1px 的微调
                #    🔴 **刻意不用 `transform: translateY()`** —— 这棵子树里
                #       `transform` 计算不出来（同上面那段观察：连 `!important`
                #       的内联 transform 都是 identity）。
                #    📌 一个已知在这里不生效的手段，不该因为"它更标准"就再试一次。
                # ⚠️ `flex-shrink:0` —— 它是这一行里**唯一不能被压缩**的东西：
                #    📌 一个宽度会被挤没的点击目标，等于在窄容器里悄悄消失。
                #       （Subagent面板很窄，实测 2026-08-20 就是在那里被折到第三行的。）
                _arrow = ui.label('›').classes('cursor-pointer').style(
                    'flex-shrink:0; '
                    'font-size:var(--nano-fs-lg); line-height:11px; color:var(--nano-faint); '
                    'font-family:var(--nano-mono); padding:0 3px; '
                    'position:relative; top:-1px;')
            _state = {"open": False, "loaded": False}

            def _toggle():
                _state["open"] = not _state["open"]
                with self._ui_scope():
                    if _state["open"] and not _state["loaded"]:
                        _state["loaded"] = True
                        self._fill_tool_detail(_det_col, _tid, fallback_name,
                                               direct=direct)
                    _det_col.set_visibility(_state["open"])
                    _arrow.set_text('⌄' if _state["open"] else '›')
                    _arrow.style(
                        'font-size:var(--nano-fs-lg); line-height:11px; font-family:var(--nano-mono); '
                        'padding:0 3px; position:relative; top:-1px; '
                        f"color:{'var(--nano-fg-soft)' if _state['open'] else 'var(--nano-faint)'};")

            _arrow.on('click', lambda _: _toggle())
        except Exception as e:
            logger.debug(f"[U8] 挂详情失败 {_tid}: {e}")

    def _fill_tool_detail(self, col, tool_use_id: str, fallback_name: str = "",
                          *, direct=None) -> None:
        """把 `catalog.detail()` 给的块画出来。**永不抛。**

        ⚠️ 两种取数方式，**一个渲染出口**：
          · 聊天区／重放 —— 按 `tool_use_id` 回**落盘账本**取（红线：不读 storage）
          · Subagent监控   —— `direct=(name, args, result)` 直接给，因为Subagent那一步
            **不在对话账本里**（它是隔离的，本来就不该进 main agent 的历史）
        📌 取数可以有两种，**渲染只能有一种** ——
           各画一遍的话，同一个工具在两处会长得不一样，
           而它们只在「我两次想法相同」的前提下一致。
        """
        try:
            col.clear()
            if direct is not None:
                _name, _args, _res = direct
                _name = _name or fallback_name
            else:
                _name, _args, _res = self._ledger_tool_record(tool_use_id)
                _name = _name or fallback_name
            _blocks = self.agent._get_tool_catalog().detail(_name, _args, _res)
            with col:
                if not _blocks:
                    # ⚠️ 说清是"没有内容"还是"没取到" —— 📌 两者对用户是不同的事，
                    #    前者正常，后者是 bug 的线索。
                    ui.label('（这次调用没有可展示的参数或结果）').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-faint); font-family:var(--nano-mono);')
                    return
                if _res is None and tool_use_id and direct is None:
                    ui.label('⚠ 结果尚未落盘（这一步可能还在跑）').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono);')
                for b in _blocks:
                    ui.label(b.label).style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-faint); font-family:var(--nano-mono); '
                        'margin-top:4px;')
                    _color = 'var(--nano-danger)' if b.kind == 'error' else 'var(--nano-fg-soft)'
                    ui.label(b.body).style(
                        f'font-size:var(--nano-fs-sm); color:{_color}; font-family:var(--nano-mono); '
                        'white-space:pre-wrap; word-break:break-word; '
                        'overflow-wrap:anywhere; line-height:1.55; '
                        'background:rgba(var(--nano-contrast-rgb), 0.02); padding:6px 8px; '
                        'max-width:100%;')
        except Exception as e:
            logger.debug(f"[U8] 画详情失败 {tool_use_id}: {e}")

    def _render_durable_tool_batch(self, entries: list[tuple[list, list, bool]], parent=None,
                                   duration: float | None = None) -> None:
        """Render one historical tool phase with the live UI's collapsed-card shape.

        The records are historical facts: this deliberately has no spinner, timer,
        action reference, or click action beyond expanding its own details.
        ``duration`` 是重放侧从落盘时间戳重算出的工具阶段跨度（缺则不显示）。
        """
        all_calls = [call for calls, _, _ in entries for call in calls]
        failures = sum(
            1 for _, results, _ in entries
            for result in results if getattr(result, "is_error", False)
        )
        complete = all(entry_complete for _, _, entry_complete in entries)
        mark = "[✓]" if complete and not failures else ("[✗]" if complete else "[○]")
        color = "var(--nano-ok)" if complete and not failures else "var(--nano-danger)" if complete else "var(--nano-fg-soft)"
        # 耗时：由重放侧从落盘时间戳重算。缺就不显示，绝不显示 0.0s
        # （那读起来像"没跑过"，与 live 侧 _settle_tool_pill 同一条纪律）。
        _dur = ""
        if duration is not None and duration >= 0:
            _dur = " · <0.1s" if duration < 0.05 else f" · {duration:.1f}s"
        _n = len(all_calls)
        if not complete:
            suffix = " · interrupted before result"
        elif failures:
            suffix = f" · {failures} failed"
        else:
            suffix = ""
        # 与 live 定型同一形状：对钩/叉子 + used N tool(s) + M failed + 耗时。
        text = f"{mark} used {_n} tool{'s' if _n != 1 else ''}{suffix}{_dur}"

        container = parent or self.chat_container
        with container:
            with ui.column().classes('w-full gap-0 mb-0.5'):
                with ui.row().classes('items-center gap-1.5 cursor-pointer mt-2 mb-0.5') as pill_row:
                    arrow = ui.label('▸').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono);'
                    )
                    ui.label(text).style(
                        f'font-size:var(--nano-fs-base); font-weight:500; font-family:var(--nano-mono); color:{color};'
                    )
                with ui.column().classes('w-full pl-3 gap-0.5 mb-0.5') as details_col:
                    for calls, results, entry_complete in entries:
                        results_by_id = {
                            getattr(result, "tool_use_id", ""): result for result in results
                        }
                        for call in calls:
                            result = results_by_id.get(getattr(call, "tool_use_id", ""))
                            if not entry_complete or result is None:
                                state, state_color = "○", "var(--nano-fg-soft)"
                            elif getattr(result, "is_error", False):
                                state, state_color = "✕", "var(--nano-danger)"
                            else:
                                state, state_color = "✓", "var(--nano-ok)"
                            # ⭐ 每一行工具明细各自带一个可展开的详情。
                            #    ⚠️ 放在**列**里而不是行里 —— 详情要落在这一行下面。
                            _row_col = ui.column().classes('w-full gap-0')
                            with _row_col:
                                with ui.row().classes('items-center gap-1.5').style('padding:1px 0;') as _trow:
                                    ui.label('$').style(
                                        'font-size:var(--nano-fs-sm); color:var(--nano-faint); font-family:var(--nano-mono);'
                                    )
                                    ui.label(self._replay_tool_display(call)).style(
                                        'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); font-family:var(--nano-mono);'
                                    )
                                    ui.label(state).style(
                                        f'font-size:var(--nano-fs-sm); font-weight:500; font-family:var(--nano-mono); color:{state_color};'
                                    )
                            self._attach_tool_detail(
                                _trow, _row_col, getattr(call, 'tool_use_id', '') or '',
                                getattr(call, 'name', '') or '')
                details_col.set_visibility(False)

                expanded = [False]

                def toggle() -> None:
                    expanded[0] = not expanded[0]
                    with self._ui_scope():
                        details_col.set_visibility(expanded[0])
                        rotation = 'rotate(90deg)' if expanded[0] else 'rotate(0deg)'
                        arrow.style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono); '
                            f'transform:{rotation}; transition:transform 0.18s;'
                        )

                pill_row.on('click', toggle)

    def _replay_durable_conversation(self) -> bool:
        """Passively project current durable history; never rerun model, tools, or work."""
        try:
            messages = self.memory.conversation_messages()
        except Exception as e:
            logger.error(f"[F3] 读取持久对话失败，保留空白 UI: {e}")
            return False
        if not messages:
            return False

        # ⭐⭐⭐ [2026-08-13 实测] **系统注记不上屏。**
        #
        # 🔴 用户重启 Nano，聊天区里冒出一个 `Koala ❯` 气泡，正文是
        #    `[System check-in] You put this in the background a while ago…`
        #    —— 一整段英文系统提示词，署着用户的名字。`[System wake-up]` 同理。
        #
        # 这些注记为了让模型读到，**必须**以 `user` / `assistant` 角色进上下文
        # （provider 只认这两种）。把上下文原样落盘是对的 —— 不然重启后
        # thinking 签名和工具往返都不合法。**错的是这里把整本账本当聊天记录画。**
        #
        # 📌 **一份账本如果同时被当作「模型上下文」和「用户看过的东西」，
        #    它就必须记下这两者的差别 —— 否则重放必然泄露。**
        # ⚠️ 所以修的是账本（`ChatMessage.visible_to_user` +
        #    `MemoryManager.add_system_note`），这里只是**消费**那个事实。
        #    在这里按 `[System` 前缀过滤是行不通的：实测里就有一条
        #    `[Scheduled plan is now due]`，它同样是系统注记却不带那个前缀。
        #
        # ⭐ 顺带把重放对齐了直播：注记消失后，唤醒前后的两句话落进**同一个**
        #    `nano ❯` 气泡 —— 那正是 v1.12 「被唤醒后继续说话，不再另起一个
        #    `nano ❯`」定下的样子。📌 **重放要还原用户当时看到的画面，
        #    不是还原模型当时读到的上下文。**
        messages = [m for m in messages if getattr(m, "visible_to_user", True)]
        if not messages:
            return False

        # ⭐⭐⭐ **UI 只保留到 L2** —— L3/L4 的交换从聊天区移除，
        #    由顶部那张「记忆起点」卡告知边界。
        #
        # 🔴 保留到 L3 会让 **UI 说谎**：用户在屏幕上读到一段 Nano 其实看不见的话，
        #    而「它记得」和「它忘了」在屏幕上长得一模一样。
        #    📌 判据：**UI 必须是权威状态的忠实投影**（UI 里存在 = 真的还活着）。
        # ⚠️ 读的是 `exchange_decay` 的 level —— **和模型读的是同一份权威**，
        #    所以两边不可能各说各话。
        _l3 = self._l3_entries()
        if _l3:
            _hidden = set()
            for _o, _e in _l3:
                try:
                    _hidden.update(range(int(_o), int(_e["end_ordinal"]) + 1))
                except Exception:
                    pass
            messages = [m for m in messages
                        if getattr(m, "_conversation_ordinal", None) not in _hidden]
        self._render_memory_origin_card(_l3)
        if not messages:
            return bool(_l3)

        index = 0
        response_body = None
        while index < len(messages):
            msg = messages[index]
            # ⭐⭐⭐ [2026-08-22] **系统事件原样重画。**
            #
            # 🔴 问题：API 调用本身失败（402 欠费 / 熔断）时画的那张故障卡片
            #    **只画不存**，重启之后凭空消失 —— 用户看到的是
            #    「你好」下面空空如也，而当时真的发生过一件事。
            # 📌 **对话记录在说谎**，违反早先的设计那条唯一的持久化原则：
            #    **UI 必须是真实的反馈。**
            #
            # ⚠️ **原样重画，不折叠、不加「当时」的修饰**——
            #    📌 一条在对话流里的记录不对「现在」做任何断言：
            #       它的位置已经把时间说清楚了。会过期的是**活的状态指示器**
            #       （pill / 抽屉），不是历史里的一条记录。
            # ⚠️ 走**同一个** `render_sys_error_card`，不另写一份 ——
            #    📌 live 那张和重放那张必须逐像素一样，而两处相隔四千行。
            _rk = str(getattr(msg, "render_kind", "") or "")
            if _rk == "sys_error":
                with self.chat_container:
                    render_sys_error_card(msg.content or "")
                index += 1
                continue
            # ⭐ 关软件时还没处理的那些话 —— 同样走**同一个**渲染器。
            if _rk == "inbox_unsent":
                with self.chat_container:
                    render_unsent_user_card(msg.content or "")
                index += 1
                continue
            if msg.role == "user":
                response_body = None
                # ⭐⭐ [2026-08-22] **重放也要认引用。**
                #    🔴 问题：重启之后，引用回复的消息变回普通消息 ——
                #       这里原来**写死 `_NORMAL_PROMPT`**，而引用横幅一行都没有。
                #    📌 判据同 `visible_to_user`：**一份账本同时服务两个受众时，
                #       两边看到的必须是同一件事** —— 而在此之前，
                #       「这句话在回复什么」只有 live 那一侧知道。
                #    ⚠️ 横幅走**同一个** `_render_quote_banner`，不另写一份。
                _rq = str(getattr(msg, "reply_quote", "") or "")
                with self.chat_container:
                    with ui.column().classes('w-full items-start mb-4'):
                        if _rq:
                            self._render_quote_banner(_rq)
                        with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                            ui.label(f'{self._user_label()} '
                                     f'{self._REPLY_PROMPT if _rq else self._NORMAL_PROMPT}').style(
                                'color:var(--nano-accent); font-size:var(--nano-fs-lg); line-height:1.75rem; '
                                'flex-shrink:0; min-width:64px; text-align:right; '
                                'font-family:var(--nano-mono);')
                            with ui.column().classes('w-full gap-0 min-w-0'):
                                ui.label(self._replay_text_content(msg.content)).classes(
                                    'text-[14px] leading-7 whitespace-pre-wrap min-w-0').style(
                                    'color:var(--nano-fg);')
                                self._replay_user_images(msg)
                index += 1
                continue

            if msg.role in ("assistant", "model"):
                if response_body is not None:
                    with response_body:
                        nano_md(self._replay_text_content(msg.content),
                                classes='text-[14px] leading-7 min-w-0',
                                style='color:var(--nano-fg);')
                    index += 1
                    continue
                with self.chat_container:
                    with ui.column().classes('w-full py-1 mb-8'):
                        with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                            ui.label('nano ❯').style(
                                'color:var(--nano-ok); font-size:var(--nano-fs-lg); line-height:1.75rem; '
                                'flex-shrink:0; min-width:64px; text-align:right; '
                                'font-family:var(--nano-mono);')
                            nano_md(self._replay_text_content(msg.content),
                                    classes='text-[14px] leading-7 min-w-0',
                                    style='color:var(--nano-fg);')
                index += 1
                continue

            if msg.role not in ("tool_calls", "tool_call"):
                index += 1
                continue

            if response_body is None:
                response_body = self._open_durable_replay_response()
            entries = []
            _batch_start_ts = None   # 首个工具调用的落盘时刻
            _batch_end_ts = None     # 该批最后一个结果的落盘时刻
            while index < len(messages) and messages[index].role in ("tool_calls", "tool_call"):
                call_msg = messages[index]
                following = messages[index + 1] if index + 1 < len(messages) else None
                _cts = getattr(call_msg, "_conversation_created_at", None)
                if _batch_start_ts is None and _cts is not None:
                    _batch_start_ts = _cts
                if call_msg.role == "tool_calls":
                    calls = list(call_msg.tool_calls)
                    results = (list(following.tool_results)
                               if following is not None and following.role == "tool_results" else [])
                    complete = (len(calls) == len(results)
                                and [c.tool_use_id for c in calls] == [r.tool_use_id for r in results])
                else:
                    calls = [call_msg]
                    results = ([following] if following is not None and following.role == "tool"
                               and following.tool_use_id == call_msg.tool_use_id else [])
                    complete = bool(results)
                _paired = following is not None and following.role in ("tool_results", "tool")
                _ets = getattr(following, "_conversation_created_at", None) if _paired else _cts
                if _ets is not None:
                    _batch_end_ts = _ets
                entries.append((calls, results, complete))
                index += 2 if _paired else 1
            # 该回应期若紧跟一段最终回答文字，用它的落盘时刻当批次终点，
            # 更贴近 live 侧「工具阶段结束＝最终答案开始」的耗时口径；否则用最后一个结果。
            if index < len(messages) and messages[index].role in ("assistant", "model"):
                _tail_ts = getattr(messages[index], "_conversation_created_at", None)
                if _tail_ts is not None:
                    _batch_end_ts = _tail_ts
            _duration = None
            if (_batch_start_ts is not None and _batch_end_ts is not None
                    and _batch_end_ts >= _batch_start_ts):
                _duration = _batch_end_ts - _batch_start_ts
            self._render_durable_tool_batch(entries, parent=response_body, duration=_duration)
            self._replay_visual_artifacts(entries, parent=response_body)
        return True

    # ══════════════════════════════════════════════════════════════════
    # 「记忆起点」卡 —— 滚动区顶部那张
    # ══════════════════════════════════════════════════════════════════
    #
    # ⚠️⚠️ **UI 与模型必须由【同一个 level 权威】驱动、同一帧生效。**
    #    「模型侧已经 L3、UI 侧还挂着 L2」正是要避免的 UI 说谎 ——
    #    📌 判据：**UI 里存在 = 它真的还活着。**
    #    所以重放侧读的是 `exchange_decay` 的 level，和模型读的是同一份。
    #
    # ⭐ 卡片**永远 5 行**（不是"默认 5 行但可以展开"）——
    #    2026-08-14：「如果是展开的话它会不会特别长，而且它本身还在
    #    聊天 UI 里，导致观感不好。建议……一个弹出 UI，太长可滚动」。
    # 📌 **一个「默认收起」的容器，它的高度上界只是【默认】的，不是【结构上的】。**
    #    要让上界真正成立，变长的那部分必须**搬出这个容器所在的空间**。
    _ORIGIN_CARD_ROWS = 5

    def _l3_entries(self):
        """当前会话已经移出上下文的那些交换（老 → 新）。读的是 level 权威。

        ⚠️ **L3 和 L4 都在里面** —— 这个列表的用途是「哪些消息不该出现在聊天区」，
           而这两档**都已经移出上下文**，答案相同。
           📌 但**「还记得几条」不能一起算**（见 `_render_memory_origin_card`）：
              L3 = 索引还在注入，Nano 还想得起来；L4 = 不再自动想起。
        ⚠️ 走 `active_entries` 而不是 `load_session` —— 陈旧的派生物一律不算数，
           📌 否则会出现「投影按 L0 把原文放回上下文，UI 却还照 L3 把它藏着」。
        ⚠️⚠️ 刻意不判 `ladder_enabled`：它是单向迁移开关，见 `model_config.json`
           的 `_ladder` 说明 —— 模型侧、投影侧、UI 侧必须同一个答案。
        """
        try:
            from core.context.decay_store import DecayStore, L3, L4
            from core.runtime.kernel import get_kernel
            _sid = self.memory.conversation_session_id
            if not _sid:
                return []
            rows = DecayStore(get_kernel().store).active_entries(_sid)
            return [(o, e) for o, e in sorted(rows.items())
                    if str(e.get("level")) in (L3, L4) and e.get("index_entry")]
        except Exception:
            return []

    def _sync_evicted_after_turn(self) -> None:
        """一轮结束后，如果这一轮有交换被移出上下文，**立刻重画聊天区**。

        🔴 不做的后果：L3 发生在 orchestrator 那一轮的 `finally` 里，
           而记忆起点卡和「隐藏被移出的消息」只在 `_replay_durable_conversation`
           里做 —— 于是：

               本轮结束 → 某段变 L3 → **模型已经看不到了，UI 还在显示**
               → 要等下一次重启才消失

           这不是体贴层的问题，是 UI 说谎：用户以为 Nano 还看得见那几轮，
           于是接着问「你刚才说的那个」。
        📌 **权威变了而投影没跟着变，模型侧叫 bug，UI 侧叫说谎 —— 同一个洞的两头。**
        ⚠️ 只在**条数真的变了**时重画：整段重放不便宜，而绝大多数轮次没有 L3。
        """
        try:
            _n = len(self._l3_entries())
            if _n == getattr(self, "_evicted_count", 0):
                return
            self._evicted_count = _n
            if self.chat_container is None:
                return
            # ⚠️ 由 orchestrator 在自己的协程里回调过来，**不在 UI 的 client 上下文里** ——
            #    不进 `_ui_scope()` 的话 NiceGUI 找不到该往哪个客户端发，
            #    表现是"什么都没发生、也不报错"。
            with self._ui_scope():
                self.chat_container.clear()
                self._replay_durable_conversation()
                if self.scroll_area is not None:
                    self.scroll_area.scroll_to(percent=1.0, duration=0.2)
        except Exception as e:
            logger.debug(f"[F5] 移出后同步聊天区跳过: {e}")

    def _render_memory_origin_card(self, entries) -> None:
        """滚动区顶部那张卡。**没有内容就不画** —— 空卡片是纯噪音。"""
        if not entries:
            return
        # 🔴 **L4 不算「还记得」**。两档都已移出上下文，
        #    但它们对用户的含义完全不同：
        #        L3 = 索引还在注入 system → **Nano 还想得起这条线索**
        #        L4 = 索引已过期      → 记忆还在库里，但**不再自动想起**
        #    把它们加在一起显示成「还记得 N 条」，是把一件已经不成立的事
        #    继续报给用户。📌 **一个数字必须只回答一个问题。**
        from core.context.decay_store import L3 as _L3
        _l3 = [str(e.get("index_entry")) for _, e in entries
               if str(e.get("level")) == _L3]
        _l4n = len(entries) - len(_l3)
        _lines = _l3
        if not _lines:
            # ⚠️ 全都过期了也要画卡 —— 那几轮确实从聊天区消失了，
            #    没有卡片的话用户只会看到"我的对话少了一段"。
            with self.chat_container:
                ui.label(f'再往前的对话已移出记忆 · {_l4n} 条线索 Nano 不再自动想起').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-faint); font-family:var(--nano-mono); '
                    'padding:8px 12px; margin-bottom:14px;')
            return
        _shown = _lines[-self._ORIGIN_CARD_ROWS:]
        _rest = len(_lines) - len(_shown)
        with self.chat_container:
            with ui.column().classes('w-full gap-1').style(
                'padding:10px 12px; margin-bottom:14px; border-radius:10px; '
                'background:var(--nano-panel); border:1px solid var(--nano-border);'
            ):
                with ui.row().classes('items-center gap-2 no-wrap'):
                    ui.icon('history').style('font-size:var(--nano-fs-lg); color:var(--nano-dim);')
                    # ⚠️ 措辞要说清**作用域** —— 这个数字是「当前这个 Nano 还能想起来的」，
                    #    ⚠️ 与「设置 → 导出数据」那个跨全部会话的数字**不是一回事**。
                    #    📌 两个数字回答的问题不同，永远不该互相印证。
                    ui.label(
                        f'再往前的对话已移出记忆 · Nano 还记得线索 {len(_lines)} 条'
                        + (f'，另有 {_l4n} 条不再自动想起' if _l4n else '')
                    ).style('font-size:var(--nano-fs-sm); color:var(--nano-dim); font-family:var(--nano-mono);')
                if _rest > 0:
                    _more = ui.label(f'▸ 还有 {_rest} 条更早的').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-faint); cursor:pointer; '
                        'font-family:var(--nano-mono);')
                    _more.on('click', lambda: self._show_origin_dialog(_lines))
                for _x in _shown:
                    ui.label(f'· {_x}').classes('w-full').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.7; '
                        'white-space:pre-wrap; word-break:break-word;')

    def _show_origin_dialog(self, lines) -> None:
        """全部线索 —— **弹窗，不是就地展开**（见上方注释）。"""
        with self._ui_scope():
            with ui.dialog() as dlg, ui.card().style(
                'background:var(--nano-panel); border:1px solid var(--nano-line); border-radius:14px; '
                'padding:0; width:640px; max-width:92vw;'
            ):
                with ui.row().classes('w-full items-center justify-between').style(
                    'padding:12px 16px; border-bottom:1px solid var(--nano-border);'
                ):
                    ui.label(f'已移出记忆的线索（{len(lines)} 条）').style(
                        'font-size:var(--nano-fs-base); color:var(--nano-fg);')
                    ui.button(icon='close', on_click=dlg.close) \
                        .props('flat round dense size=sm').style('color:var(--nano-dim) !important;')
                with ui.column().classes('w-full gap-1').style(
                    'padding:10px 16px; max-height:56vh; overflow-y:auto;'
                ):
                    for _x in reversed(lines):
                        ui.label(f'· {_x}').classes('w-full').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.75; '
                            'white-space:pre-wrap; word-break:break-word;')
            dlg.open()

    def _replay_user_images(self, msg) -> None:
        """把用户当时发的图重新画出来。

        ═══ 已定（2026-08-13）═══

        > 「就算 cc 自己的图片也不会因为 cc 重启丢失啊，哪怕你早就不记得一百轮
          之前发过的图，用户这边也要能看到、甚至点开。
        > 「**模型是模型，用户 UI 归用户 UI**」

        📌 **上下文可以忘，历史不可以。** 所以这里读的是 `msg.ui_images`
           （用户历史，谁都不许动），**不是** `msg.content` 里的 image block
           —— 那一份是模型上下文，随时会被 `compress_image_blocks` 压掉。

        ⚠️ 与 Nano 自己的截图**方向相反**：那些是 EPHEMERAL，
           已明确说「重启不保留刚好是这些信息在 UI 被抛弃的出口」。
           **一个是「我曾经发过什么」，一个是「它当时看到什么」。**

        ⚠️ 图不在了（用户手动清了 `data/chat_images/`）就跳过这一张，
           **不画一个碎图标** —— 破图比没有更像故障。
        """
        refs = list(getattr(msg, "ui_images", None) or [])
        if not refs:
            return
        try:
            from core.runtime.blobs import image_data_uri
        except Exception:
            return
        for _ref in refs:
            _uri = image_data_uri(_ref)
            if not _uri:
                logger.debug(f"[D10] 重放：图片 {_ref} 已不在图库，跳过")
                continue
            # ⚠️ 重放这条也要能点开 —— 📌 同一张图在「刚发出」和「重启后」
            #    表现不一样的话，用户会以为它坏了。
            chat_image(_uri, alt="你发送的图片", thumb_h=160)

    def _replay_visual_artifacts(self, entries: list[tuple[list, list, bool]], parent) -> None:
        """把这一批工具里的 `render_visual` 重新画出来。

        ═══ 🔴 2026-08-13 实测：重启后流程图消失 ═══

        用户让 Nano 画了一张流程图，重启，气泡里只剩
        `[✓] used 2 tools` 和那句「就是这样——三个盒子从上到下」——
        **它在描述一张不存在的图。**

        ⭐ 根因不是"没存"，而是**没画**：`render_visual` 的 `html` / `title`
           就在工具调用的 `args` 里，而 args 是**逐字落盘**的
           （`core/runtime/conversation.py` 的 `"args": call.args`）。
           重放侧只画了工具 pill 和正文，从没看过 args 里还有能上屏的东西。

        📌 **落盘完整 ≠ 重放完整。** 这两件事之间隔着一个"谁去读它"，
           而那一步是手写的 —— 手写的东西就会漏。
        📌 与那条同源：**重放要还原用户当时看到的画面**；
           少一张图和多一段系统提示词，是同一个问题的两个方向。

        ⚠️ `is_error` 的那次不画：报错来自**执行前的闸**
           （健康闸 / `TOOL_NOT_ACTIVE`），闸拦下时 handler 根本没跑，
           `visual_render` 事件没发出去过 —— **用户当时就没看见**。
           而 handler 一旦进去，第一件事就是 put 事件，之后没有失败点。
        ⚠️ token 现场新生成：它只是"自适应高度回调找哪个 iframe"的地址，
           **不是内容的一部分**，所以不该也不需要落盘。
        """
        import uuid as _uuid_r
        for calls, results, _complete in entries:
            _errored = {getattr(r, "tool_use_id", "")
                        for r in results if getattr(r, "is_error", False)}
            for call in calls:
                if getattr(call, "name", "") != _REPLAYABLE_VISUAL_TOOL:
                    continue
                if getattr(call, "tool_use_id", "") in _errored:
                    continue
                _args = getattr(call, "args", {}) or {}
                _html = _args.get("html") or ""
                if not _html:
                    continue
                try:
                    self._render_visual_artifact(
                        _html, _args.get("title") or "",
                        _uuid_r.uuid4().hex[:12], parent=parent)
                except Exception as e:
                    logger.warning(f"[F3] 重放可视化失败（跳过这一张）: {e}")

    def _clear_empty_state_greeting(self):
        if getattr(self, '_empty_state_greeting', None) is not None:
            try:
                self._empty_state_greeting.delete()
            except Exception:
                pass
            self._empty_state_greeting = None

    def _show_right_panel(self, name: str):
        """知识库/监控/记忆/计划四个面板共用一个 right_drawer。
        再点一次当前已显示的面板 → 关闭整个抽屉。
        计划面板（plan）仅在 _nav_plan_row 可见时才响应点击，
        但 _show_right_panel('plan') 可从代码直接调用。"""
        panels = {
            'kb': self.kb_panel,
            'monitor': self.monitor_panel,
            'memory': self.memory_panel,
            'plan': self.plan_panel,
            'tasks': self.tasks_panel,
        }
        nav_rows = {
            'kb': self._nav_kb_row,
            'monitor': self._nav_monitor_row,
            'memory': self._nav_memory_row,
            'plan': self._nav_plan_row,
            'tasks': self._nav_tasks_row,
        }
        target = panels[name]
        if self.right_drawer.value and target.visible:
            self.right_drawer.hide()
            for row in nav_rows.values():
                if row:
                    row.classes(remove='nav-glow')
            return
        # ⚠️ Subagent监控不在 `panels` 里（它没有并列按钮），
        #    但切到别的面板时**必须把它藏掉** ——
        #    📌 一个不参与互斥的面板，会在某次切换后和别人叠在一起。
        if getattr(self, "agent_panel", None) is not None:
            self.agent_panel.set_visibility(False)
        for key, panel in panels.items():
            is_target = panel is target
            panel.set_visibility(is_target)
            row = nav_rows[key]
            if row:
                row.classes(add='nav-glow' if is_target else '', remove='' if is_target else 'nav-glow')
        # ⚠️ 打开后台任务面板时**当场刷一次** —— 📌 一个只在创建时渲染过的
        #    列表，会让用户以为"什么都没发生"，而其实是它没去看。
        if name == 'tasks':
            self._refresh_tasks_panel()
        self.right_drawer.show()

    # ══════════════════════════════════════════════════════════════════
    # 后台任务抽屉 —— Running / Finished
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐ 这一项的**认领人**是 `cancel_bg_task`：早先那一行原话
    #    「⚠️⚠️ 它现在【零 UI 调用方】—— 这一行就是它的认领人」。
    # 📌 **一个写好但没人调的函数，比没写更坏** —— 没写时缺口是可见的，
    #    写了不接时缺口**看起来已经补上了**。
    #    （本轮 的 `bridge.recall` 与 `get_store` 又各印证一次。）

    _BG_LABELS = {
        "COMPLETED": ("完成", "var(--nano-ok)"),
        "FAILED": ("失败", "var(--nano-danger)"),
        # 🔴 CANCELLED 独立，**不许并进失败** —— 早先已定：
        #    📌 用户主动停掉不是失败，归错会让人去排查一个不存在的问题。
        "CANCELLED": ("已终止", "var(--nano-fg-soft)"),
        "INTERRUPTED_BY_RESTART": ("被重启打断", "var(--nano-amber)"),
        "EXPIRED": ("过期", "var(--nano-dim)"),
    }

    def _bg_snapshot(self):
        """`(running, finished)`。**永不抛** —— 抽屉挂了不许把界面带走。"""
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime.task import (live_background_jobs,
                                           finished_background_jobs,
                                           queued_background_jobs)
            k = get_kernel()
            _fin = list(finished_background_jobs(k))
            # ⚠️ 「哪些还在排队」**问 `task` 那一层**，不在渲染里另写一次
            #    `execution == IDLE`。📌 一条规则写在两处，它们只在
            #    「我两次想法相同」的前提下一致 —— 而本项目已经因为这个形状
            #    踩过的计量 vs 投影、的 live vs 重放。
            self._bg_queued_ids = {getattr(r, "task_id", "")
                                   for r in queued_background_jobs(k)}
            # ⚠️ Clear 只是**把这一刻之前的藏起来**，不删 Task 记录 ——
            #    📌 「我不想再看见它」和「它没发生过」是两件事。
            _cut = getattr(self, "_bg_finished_hidden_before", 0.0) or 0.0
            if _cut:
                _fin = [r for r in _fin if float(getattr(r, "updated_at", 0)) > _cut]
            return list(live_background_jobs(k)), _fin
        except Exception as e:
            # ⚠️ 读失败时**把排队名单清空**，不许留上一次的 ——
            #    📌 一份读不出来时保留下来的旧名单，会让界面继续断言一件
            #       它已经不知道真假的事；而"不知道"该表现成"不标"，不是"沿用"。
            self._bg_queued_ids = set()
            logger.warning(f"[L5] 读后台任务失败: {e}")
            return [], []

    # ⚠️⚠️ `_parked_snapshot()` 与抽屉里那段「搁置 N」**已删除**
    #    （2026-08-20，Task 收窄）。
    #
    # 🔴 它画的是「被 Nano 显式搁置的一件事」，而那正是被推翻的那个概念
    #    （`task_boundary(park)` 已随之退役）。
    # ⭐ 而**留下来的判据比那段 UI 更有价值**，原样搬到这里：
    #    📌 **抽屉回答的是「什么正在动」** —— 一个被放下的「一件事」
    #       **没有任何执行体**，把它画在这里，用户就会以为有人在推进它，
    #       然后等一个永远不会自己到来的结果。
    #    ⭐ 这也正是 2026-08-20 补的那条的同一面：一个被 `dont_wait`
    #       移出手头的**调用**，之后就算「变回手头的活」也**不搬 UI** ——
    #       抽屉记的是执行体，而执行体从头到尾没变过。
    # ⭐ 模型侧一个字都没少：`conversation_tasks_for_model()` 仍然把它们
    #    连同 `[PARKED - nothing is working on it]` 一起给模型看。

    def _refresh_tasks_panel(self) -> None:
        """重画抽屉内容 + 同步导航角标与聊天区 pill。**永不抛。**"""
        try:
            running, finished = self._bg_snapshot()
            # ⭐⭐ 后台那条的**心跳**（2026-08-22 那次建模）。
            #
            # 前台那条的心跳是**回看**（`_reschedule` 每次都推后 `orphan_at`，
            # 判据：「一次成功的回看恰恰是『有人管』的证据」）。
            # 🔴 但 `dont_wait` 之后 `fire_at=None` —— **后台的东西没有回看**，
            #    于是没有任何东西去推 `orphan_at`：一个装 35 分钟的包会在
            #    第 30 分钟被 `ORPHANED`，35 分钟真装完时通知落到**已终态**的
            #    记录上 → **结果丢失**。
            # ⭐ 所以这里换一个「有人管」的证据：**载体还在跑**。
            #    📌 `orphan_at` 的定义是「等的那个东西再也没回来」，
            #       而载体还活着恰恰证明它还会回来。
            # ⚠️ 挂在这个 2 秒定时器上而不是新起一个：它本来就每 2 秒
            #    问一遍「谁还在跑」——📌 同一个事实不该被问两遍。
            # ⚠️ 心跳的来源必须是 `_bg_tasks`（**内存里那份，谁还活着**），
            #    不是 `running`（那是 runtime 记录，不带 `suspension_ref`）。
            #    📌 「谁还在跑」这个事实有两个副本，而**只有一个副本知道载体的 ref** ——
            #       拿错那个的表现是心跳静默失效（异常被 except 吞掉），不报错。
            try:
                from core.runtime import waitcond as _wc_hb
                for _m in list(self._bg_tasks.values()):
                    _aio = _m.get("aio")
                    if _aio is None or _aio.done():
                        continue          # 已经结束的不推 —— 推后 ≠ 让它不死
                    _ref = str(_m.get("suspension_ref") or "")
                    if _ref:
                        _wc_hb.touch_by_bg_ref(_ref)
            except Exception as _e_hb:
                logger.debug(f"[B1] 载体心跳跳过（不影响抽屉）: {_e_hb}")
            # ⭐⭐ pill 数的是「**它在后台**」—— 就这一个条件。
            #
            # 🔴 旧判据写的是「有东西在动、**而你不必等它**」，两个条件。
            #    2026-08-22 那次建模：**后半句把「注意力」混进了「位置」。**
            #
            # ⭐ **位置**和**注意力**是两个正交的事实，pill 只数前者：
            #      · 在不在后台   = 它占不占「一次只能有一件」的那个执行位（位置）
            #      · 在看哪一件事 = 此刻的注意力（由回看安排）
            #    📌 所以一件后台的事重新占据注意力时，**它并没有离开后台** ——
            #       变的只是看哪儿。pill 不变、抽屉那行也不搬。
            #    📌 依据：一件事无论是跑完还是坏了，结果**都在后台产出** ——
            #       所以不存在「把它挪回前台」这个需求，也就不需要反向搬 UI。
            #
            # ⚠️ 于是「进入后台」是一次性事件，由**进入那一刻**的判断决定；
            #    之后判断翻转了也不搬 UI —— 📌 UI 的抖动比语义的不精确更伤。
            self._sync_task_pill(len(running))
            if self._tasks_badge_label is not None:
                with self._ui_scope():
                    if running:
                        self._tasks_badge_label.set_text(str(len(running)))
                        self._tasks_badge_label.style('display:block;')
                    else:
                        self._tasks_badge_label.style('display:none;')
            if self._tasks_body is None:
                return

            # 🔴🔴 **不变就不重画**（2026-08-15 实测：展开 1-2 秒后被强制收起）
            #
            # 根因：`ui.timer(2.0, self._refresh_tasks_panel)` 每 2 秒
            # `self._tasks_body.clear()` **整棵重建**，而"哪一行是展开的"活在
            # 被重建的那棵子树里 —— 于是用户刚点开，下一个 tick 就没了。
            # ⭐ 这与「`ui.menu` 放进每次 `set_content` 的 `ui.html` 里」是**同一个形状**
            #    （那次把菜单画没了、还留下一层挡住抽屉的 Quasar 遮罩）。
            # 📌 **一个周期性的整体重画，会把所有活在它里面的 UI 状态一起清掉 ——
            #    而那些状态通常是"用户此刻正在做的事"。**
            #
            # ⚠️ 指纹要包含**会影响这一屏的每一样东西**（数量 / id / 终局 / 更新时刻）：
            #    📌 指纹漏一项，那一项的变化就永远不会重画 —— 而它同样不报错，
            #       只是"某个任务一直显示成还在跑"。
            # ⚠️ 指纹里带上 transcript 的步数 —— 否则Subagent跑到第 3 步时
            #    抽屉不会重画（`updated_at` 不变），用户看到的永远是第 1 步。
            #    📌 指纹漏一项，那一项的变化就永远不会重画，**而且不报错**。
            def _tsteps(_r):
                try:
                    return len(self.agent.agent_transcript(
                        getattr(_r, "task_id", "") or ""))
                except Exception:
                    return 0

            _fp = (
                # ⚠️ 带上 `execution`：**「排队 → 开跑」不一定 touch `updated_at`
                #    以外的东西，但它改变这一屏**。
                #    📌 指纹漏一项，那一项的变化就永远不会重画，而且不报错。
                tuple((getattr(r, "task_id", ""), getattr(r, "updated_at", 0),
                       getattr(r, "execution", ""), _tsteps(r))
                      for r in running),
                tuple((getattr(r, "task_id", ""), getattr(r, "terminal_reason", ""),
                       getattr(r, "updated_at", 0)) for r in finished),
            )
            if _fp == getattr(self, "_bg_fp", None):
                return
            self._bg_fp = _fp

            with self._ui_scope():
                self._tasks_body.clear()
                with self._tasks_body:
                    self._render_bg_section(running, finished)
            # ⭐ Subagent监控抽屉开着时跟着重画 —— 📌 用户要的是**监控**：
            #    在跑的时候就看得见走到哪、花了多少，而不是跑完才有的记录。
            if (getattr(self, "agent_panel", None) is not None
                    and self.agent_panel.visible):
                self._render_agent_monitor()
        except Exception as e:
            logger.warning(f"[L5] 刷新后台任务面板失败: {e}")

    def _render_bg_section(self, running, finished) -> None:
        ui.label(f'Running {len(running)}' if running else 'Running').style(
            'font-size:var(--nano-fs-sm); color:var(--nano-dim); font-family:var(--nano-mono);')
        if not running:
            ui.label('当前没有在跑的后台任务').style(
                'font-size:var(--nano-fs-sm); color:var(--nano-faint); padding:2px 0 6px;')
        for r in running:
            self._render_bg_row(r, live=True)

        with ui.row().classes('w-full items-center justify-between').style(
                'margin-top:10px;'):
            ui.label(f'Finished {len(finished)}').style(
                'font-size:var(--nano-fs-sm); color:var(--nano-dim); font-family:var(--nano-mono);')
            if finished:
                # ⚠️ Clear 只清**显示**，不删 Task 记录 —— 那是历史事实。
                #    📌 「我不想再看见它」和「它没发生过」是两件事
                #       （同重置对话写的是"放弃"不是"删除"）。
                ui.button('Clear', on_click=self._clear_finished_bg) \
                    .props('flat dense no-caps') \
                    .style('font-size:var(--nano-fs-xs); color:var(--nano-dim) !important;')
        # ⚠️ Finished 只列**本次运行产生的**（含本次启动认定的
        #    `INTERRUPTED_BY_RESTART`）—— 见 `task._PROCESS_START` 那段。
        if not finished:
            ui.label('本次运行还没有结束的后台任务').style(
                'font-size:var(--nano-fs-sm); color:var(--nano-faint); padding:2px 0;')
        for r in finished:
            self._render_bg_row(r, live=False)

    def _render_bg_row(self, rec, *, live: bool) -> None:
        """一行任务。形态与工具卡一致：**一行 + 行尾 `›` 展开**。

        ⭐ 刻意复用同一个视觉语法：📌 用户已经学会了「行尾那个 `›` 能展开」，
           再发明第二种展开方式等于让用户学两遍。
        """
        # ⚠️ `TaskRecord` 上**没有** `title`/`note` 这两个字段（回代码核实过）——
        #    第一版照着印象写的，那会静默显示成 task_id。
        #    📌 一个 `getattr(x, "不存在的字段", 兜底)` 永远不会报错，
        #       它只会安静地一直给兜底。
        _title = (getattr(rec, "goal_summary", "") or getattr(rec, "task_id", "") or "任务")
        _reason = str(getattr(rec, "terminal_reason", "") or "")
        _label, _color = self._BG_LABELS.get(_reason, (_reason or "", "var(--nano-dim)"))
        # ⭐⭐ **排队中 ≠ 在跑。**
        #
        # 🔴 `live_background_jobs()` 返回的是所有 ACTIVE，含**还在等 slot** 的
        #    （ACTIVE + IDLE）。此前它们和真在跑的画成同一个橙色 `▶` ——
        #    而模型侧的注入**早就把这两者分开**了（`[queued - not started yet]`，
        #    理由写在 `background_jobs_for_model`：对模型是两个不同的事实）。
        # 📌 **同一条区分，只在一个受众那里生效，等于承认它重要、
        #    却只告诉了其中一个人** —— 用户看着一个"在跑"的东西毫无进展，
        #    而它其实连开始都没开始。
        # ⭐ pill **仍然把它算进去**，这是刻意的：📌 排队的会**自己**开始，
        #    不需要任何人再做决定；而搁置的不会（要 Nano 回头接）。
        #    pill 那句「你会被告知它结束」对排队的成立，对搁置的不成立。
        _queued = live and (getattr(rec, "task_id", "")
                            in (getattr(self, "_bg_queued_ids", None) or set()))
        _col = ui.column().classes('w-full gap-0').style(
            'border:1px solid var(--nano-border); padding:6px 8px; margin-bottom:4px;')
        with _col:
            with ui.row().classes('w-full items-center gap-1.5 no-wrap') as _row:
                ui.label('⋯' if _queued else ('▶' if live else '·')).style(
                    f"font-size:var(--nano-fs-xs); "
                    f"color:{'var(--nano-dim)' if _queued else ('var(--nano-amber)' if live else 'var(--nano-faint)')}; "
                    'font-family:var(--nano-mono);')
                ui.label(_title[:48]).classes('min-w-0').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-fg); font-family:var(--nano-mono); '
                    'overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1;')
                if _queued:
                    # ⚠️ 排队的**照样能终止**（它的协程已经存在，正卡在信号量上）——
                    #    📌 一个还没开始的任务，用户想撤销的理由只会更充分。
                    ui.label('排队中').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono);')
                if live:
                    # ⭐⭐ 那颗 `■` —— `cancel_bg_task()` 的**唯一 UI 调用方**。
                    ui.label('■').classes('cursor-pointer').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-danger); padding:0 4px;').on(
                        'click', lambda _, _t=rec: self._cancel_bg_from_ui(_t))
                elif _label:
                    ui.label(_label).style(
                        f'font-size:var(--nano-fs-xs); color:{_color}; font-family:var(--nano-mono);')
                # ⭐⭐ **Subagent那一行没有 `›`** —— 它的"展开"就是 `View transcript`
                #    （2026-08-15 定的，说了三遍才做对）。
                # 🔴 原来把Subagent的过程塞进这一行的 `›` 里做成一个纯文本列表 ——
                #    那既不是独立抽屉，也不是聊天区那套展示形式。
                # 📌 **一个过程该有多大的展示空间，取决于它有多长，
                #    不取决于它此刻挂在哪一行下面。**
                _is_agent = str(getattr(rec, "goal_summary", "") or "").startswith("Agent · ")
                if _is_agent:
                    ui.label('View transcript ›').classes('cursor-pointer').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-amber); font-family:var(--nano-mono); '
                        'padding:0 2px; white-space:nowrap;').on(
                        'click', lambda _, _t=(getattr(rec, "task_id", "") or ""):
                            self._show_agent_monitor(_t))
                    _arrow = None
                else:
                    _arrow = ui.label('›').classes('cursor-pointer').style(
                        'font-size:var(--nano-fs-lg); line-height:11px; color:var(--nano-faint); '
                        'font-family:var(--nano-mono); padding:0 2px; '
                        'position:relative; top:-1px;')
            if _arrow is None:
                # Subagent没有行内详情 —— 它整个过程在自己的抽屉里。
                return
            _det = ui.column().classes('w-full gap-0').style(
                'padding:4px 0 0 10px; border-left:1px solid var(--nano-border); margin:4px 0 0 2px;')
            _det.set_visibility(False)
            with _det:
                import datetime as _dt
                def _ts(v):
                    try:
                        return _dt.datetime.fromtimestamp(float(v)).strftime("%H:%M:%S")
                    except Exception:
                        return ""
                for _k, _v in (("完整描述", getattr(rec, "goal_summary", "")),
                               ("任务 id", getattr(rec, "task_id", "")),
                               ("类型", getattr(rec, "kind", "")),
                               ("执行位置", getattr(rec, "placement", "")),
                               ("开始", _ts(getattr(rec, "created_at", 0))),
                               ("结束" if not live else "最后更新",
                                _ts(getattr(rec, "updated_at", 0))),
                               ("结局", _reason or "（还在跑）")):
                    if not _v:
                        continue
                    ui.label(f'{_k}：{_v}').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-dim); font-family:var(--nano-mono); '
                        'white-space:pre-wrap; word-break:break-word; '
                        'overflow-wrap:anywhere;')

        # ⭐⭐ 展开状态**存在这一行之外**，按 `task_id` 记。
        #
        # 🔴 指纹门控只解决"没变化时别重画"；真有变化时（任务从 Running 变
        #    Finished、`updated_at` 跳了）还是会重建这一行 —— 那一刻**不许**把
        #    用户正在读的东西收起来。
        # 📌 **一个"因为内容更新了所以重画"的界面，如果顺手丢掉了用户的展开/
        #    选择/滚动位置，那它是在用「更新」惩罚「正在看」。**
        # ⚠️ 用 `task_id` 而不是行的序号：序号会随 Running/Finished 迁移而变，
        #    那会导致"展开的行"莫名其妙换成了另一条。
        _tid_key = str(getattr(rec, "task_id", "") or id(rec))
        _open_set = getattr(self, "_bg_expanded", None)
        if _open_set is None:
            _open_set = self._bg_expanded = set()
        _st = {"open": _tid_key in _open_set}
        if _st["open"]:
            _det.set_visibility(True)
            _arrow.set_text('⌄')

        def _tg():
            _st["open"] = not _st["open"]
            if _st["open"]:
                _open_set.add(_tid_key)
            else:
                _open_set.discard(_tid_key)
            with self._ui_scope():
                _det.set_visibility(_st["open"])
                _arrow.set_text('⌄' if _st["open"] else '›')

        _arrow.on('click', lambda _: _tg())

    # ══════════════════════════════════════════════════════════════════
    # Subagent监控抽屉 —— **内容照主聊天区那套画**
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐⭐ 2026-08-15（说了三遍）：Claude Code 的 agent 监控抽屉与它的主聊天 UI
    #    **展示形式一模一样**。所以这里画的是：
    #
    #        nano ❯         main agent 发给 Subagent 的指令
    #        [✓] used N tools · 12.3s     ← 与聊天区同一个工具 pill
    #           $ list_knowledge_files ✓ ›   ← 与聊天区同一个 展开
    #        nano agent ❯   Subagent的报告
    #
    # ⚠️ 头部用 `nano ❯` / `nano agent ❯`（用户给的两个选项里选了这个）：
    #    📌 那条指令是 **main agent** 写的，不是用户写的。没有头部的话，
    #       它读起来会像是用户发的 —— 而 用户最早提的要求正是
    #       「**用户要能看到 main agent 给 Subagent 写了什么指令**」。
    #
    # ⚠️ **不加与 [知识库]/[监控]/[任务] 并列的按钮**——
    #    入口已经有两个：聊天区的 pill、后台任务抽屉那行的 `View transcript`。
    #    📌 一个已经有两条通路的东西，再加第三个入口只是让导航栏更挤。
    #
    # ⭐ **实时**：它在跑的时候就该看得见走到哪、花了多少 ——
    #    📌 用户要的是**监控**，不是结果。跑完才有的东西叫记录。

    def _show_agent_monitor(self, task_id: str) -> None:
        """打开Subagent监控抽屉，盯住某一个Subagent。"""
        try:
            self._agent_watch = task_id or ""
            for _k, _panel in (("kb", self.kb_panel), ("monitor", self.monitor_panel),
                               ("memory", self.memory_panel), ("plan", self.plan_panel),
                               ("tasks", self.tasks_panel)):
                if _panel is not None:
                    _panel.set_visibility(False)
            self.agent_panel.set_visibility(True)
            self._render_agent_monitor()
            self.right_drawer.show()
        except Exception as e:
            logger.warning(f"[A4] 打开Subagent监控失败: {e}")

    def _render_agent_monitor(self) -> None:
        """画一遍。**永不抛。** 在跑的Subagent会被 `_refresh_tasks_panel` 带着重画。"""
        _tid = getattr(self, "_agent_watch", "") or ""
        if self._agent_body is None:
            return
        try:
            run = self.agent.agent_run(_tid)
        except Exception:
            run = {}
        try:
            from core.usage import usage_tracker as _ut
            _tok = _ut.agent_tokens(_tid)
        except Exception:
            _tok = 0
        _steps = list(run.get("steps") or [])
        _live = run.get("ok") is None and bool(run.get("started"))
        _dur = 0.0
        try:
            import time as _t
            _dur = (float(run.get("ended") or _t.time())
                    - float(run.get("started") or 0)) if run.get("started") else 0.0
        except Exception:
            pass

        with self._ui_scope():
            self._agent_body.clear()
            with self._agent_body:
                if not run:
                    ui.label('这个分身没有留下记录（可能是上个进程跑的）').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-faint); font-family:var(--nano-mono);')
                    return

                # ── main agent 发给 Subagent 的指令 ──
                ui.label('nano ❯').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-ok); font-weight:600; '
                    'font-family:var(--nano-mono); margin-top:2px;')
                ui.label(str(run.get("instruction") or "")).style(
                    'font-size:var(--nano-fs-base); color:var(--nano-fg); line-height:1.7; '
                    'white-space:pre-wrap; word-break:break-word; '
                    'overflow-wrap:anywhere; margin:2px 0 10px;')

                # ── Subagent开口 ──
                #
                # 🔴 **头部必须在工具 pill 之前**（2026-08-15 实测指出）：
                #    第一版把 pill 画在了 `nano ❯`（main agent 指令）那一段下面、
                #    `nano agent ❯` 之前 —— 于是 **Subagent 调的工具，看起来像是 main agent 调的**。
                # ⭐ 聊天区的顺序是「**谁在说 → 它调了什么 → 它说了什么**」，
                #    这里工具是**Subagent**调的，所以头部要先出现。
                # 📌 **一段行为归谁，是由它排在谁的名字下面决定的** ——
                #    位置本身就是归属声明，不是排版细节。
                ui.label('nano agent ❯').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-ok); font-weight:600; '
                    'font-family:var(--nano-mono); margin-top:12px;')

                # ── 工具 pill（与聊天区同一形状）──
                _fail = sum(1 for _s in _steps if _s[3])
                _mark = '[✓]' if not _fail else '[✗]'
                _color = 'var(--nano-ok)' if not _fail else 'var(--nano-danger)'
                if _live:
                    _mark, _color = '[⋯]', 'var(--nano-amber)'
                _bits = f"{_mark} used {len(_steps)} tool{'s' if len(_steps) != 1 else ''}"
                if _fail:
                    _bits += f" · {_fail} failed"
                if _dur:
                    _bits += f" · {_dur:.1f}s"
                if _tok:
                    _bits += f" · {_tok:,} tok"
                with ui.column().classes('w-full gap-0'):
                    ui.label(_bits).style(
                        f'font-size:var(--nano-fs-base); font-weight:500; color:{_color}; '
                        'font-family:var(--nano-mono); margin-bottom:2px;')
                    for _name, _args, _txt, _err in _steps:
                        _rowc = ui.column().classes('w-full gap-0')
                        with _rowc:
                            # 🔴 实测 2026-08-20：这一行会被折成**三行**
                            #    （`$` / 简介 / 展开箭头各占一行），整块看起来很乱。
                            #    根因是Subagent面板很窄，而这一行默认允许换行 ——
                            #    于是三个**本来就该在同一行**的东西被拆开了。
                            # ⭐ 修法与 （长文件名盖住 `⋮`）同一条：
                            #    **`no-wrap` + 中间那格 `min-width:0` + 省略号**。
                            #    📌 在窄容器里，"让文字自己换行"和"让这一行保持
                            #       是一行"是两个相反的诉求；要后者就必须
                            #       同时给三样：不许换行 / 中间可压缩 / 溢出用省略号。
                            #    ⚠️ 只写 `no-wrap` 不给 `min-width:0` 是不够的：
                            #       flex 子项默认 `min-width:auto`，它会拒绝被压到
                            #       比内容更窄，于是撑破容器而不是省略。
                            with ui.row().classes(
                                    'items-center gap-1.5 no-wrap w-full min-w-0'
                            ).style('padding:1px 0;') as _r:
                                ui.label('$').style(
                                    'font-size:var(--nano-fs-sm); color:var(--nano-faint); flex-shrink:0; '
                                    'font-family:var(--nano-mono);')
                                ui.label(self._tool_display(_name, _args)).classes(
                                    'min-w-0').style(
                                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); flex:1; '
                                    'font-family:var(--nano-mono); '
                                    'overflow:hidden; text-overflow:ellipsis; '
                                    'white-space:nowrap;')
                                ui.label('✕' if _err else '✓').style(
                                    f"font-size:var(--nano-fs-sm); font-weight:500; flex-shrink:0; "
                                    f"font-family:var(--nano-mono); "
                                    f"color:{'var(--nano-danger)' if _err else 'var(--nano-ok)'};")

                        class _R:
                            content = _txt
                            is_error = _err
                        # ⭐⭐ **同一个展开出口**—— 只是取数方式不同：
                        #    Subagent那一步不在对话账本里，所以 `direct=` 直接给。
                        self._attach_tool_detail(_r, _rowc, "", _name,
                                                 direct=(_name, _args, _R()))

                # ── Subagent的报告 ──
                # ⚠️ 头部已经在工具 pill 之前画过了（见上），这里只接正文 ——
                #    📌 与聊天区一致：一个 `nano ❯` 头下面既有工具也有回答，
                #       不是每段内容各挂一个头。
                if run.get("report"):
                    ui.markdown(str(run.get("report") or "")).classes(
                        'text-[12px] leading-7 min-w-0').style('margin-top:6px;')
                elif _live:
                    # ⭐ 与 main agent 那一行**同一套词汇**——
                    #    📌 用户已经学会 `⠋ still running · Xs` 是"有事在跑"；
                    #       在 Subagent 这里换一句中文，等于让用户学两遍。
                    #    ⚠️ 而它确实是同一件事：一个还没结束的执行体。
                    with ui.row().classes('items-center gap-1.5').style('margin-top:10px;'):
                        _sp = ui.label('⠋').style(
                            'font-size:var(--nano-fs-base); color:var(--nano-dim); '
                            'font-family:var(--nano-mono);')
                        ui.html(NANO_AVATAR_SVG).style(
                            'width:14px; height:14px; flex-shrink:0;')
                        ui.label('still running…').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-dim); '
                            'font-family:var(--nano-mono); letter-spacing:0.01em;')
                        # ⚠️ 转圈用 **`ui.timer`，而且建在这个 row 里面** ——
                        #    抽屉会整棵重建（`_refresh_tasks_panel` 的指纹门控），
                        #    timer 跟着容器一起被销毁，不会留下一个写空元素的孤儿。
                        #    📌 一个指向「可能被重画的 UI」的写入者，
                        #       必须和它指向的东西同生共死。
                        # 🔴 这里**不能用 CSS 动画**：那一格要变的是**文字内容**
                        #    （braille 帧），而 CSS 改不了 textContent。
                        _bf = ['⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏', 0]

                        def _tick(_s=_sp, _b=_bf):
                            _b[1] += 1
                            try:
                                _s.set_text(_b[0][_b[1] % len(_b[0])])
                            except Exception:
                                pass
                        ui.timer(0.12, _tick)

    def _tool_display(self, name: str, args: dict) -> str:
        """工具的友好名。⚠️ 走 目录，**不另写一张表**。"""
        try:
            return self.agent._get_tool_catalog().presentation(name, args or {}) or name
        except Exception:
            return name

    def _cancel_bg_from_ui(self, rec) -> None:
        """那颗 `■`。⚠️ 终止的是**那个后台任务**，不是这场对话。

        🔴🔴 **2026-08-20：它此前对【每一条】Running 都是失效的。**
           `_bg_tasks` 的唯一写入方 `_start_bg_task()` 在 2026-08-10
           「系统交还不再写后台 Task 权威记录」之后就**零生产调用方**了，
           于是这张表恒空 → 这里永远走 `_ui_id is None` → 用户点 `■`
           永远收到「这个任务已经不在跑了」，**而它正在跑**。
           而抽屉里唯一的 Running（Subagent）走的是另一条路，从来不在这张表里。
        📌 **一个只查一张表的查找，会随着「东西改从别的门进来」而静默失效** ——
           它不报错，它只是永远找不到。
        ⚠️ 而 `t_l5_bg_drawer` 第 2 项当时是绿的：它**自己 seed 了 `_bg_tasks`**，
           证明的是「给它一张有货的表，查得对」，不是「有人往表里放货」。
           📌 一条断言如果它声明的前提是自己建立的，它证明不了生产路径 ——
              与早先那条互为镜像（那次是前提**没人**建立）。
        """
        _tid = getattr(rec, "task_id", "") or ""
        try:
            # ⭐ 先问载体那张表 —— 今天所有真的在跑的东西都在它里面
            #    （Subagent / 被 `dont_wait` 的调用）。
            if self._cancel_carrier(_tid):
                ui.notify('已终止', type='positive')
                self._refresh_tasks_panel()
                return
            _ui_id = None
            for k, v in (self._bg_tasks or {}).items():
                if (v or {}).get("rt_task_id") == _tid or k == _tid:
                    _ui_id = k
                    break
            if _ui_id is None:
                ui.notify('这个任务已经不在跑了', type='info')
                self._refresh_tasks_panel()
                return
            ok = self.cancel_bg_task(_ui_id, by="user")
            ui.notify('已终止' if ok else '它已经结束了', type='positive' if ok else 'info')
        except Exception as e:
            logger.warning(f"[L5] UI 终止后台任务失败 {_tid}: {e}")
            ui.notify('终止失败，看日志', type='negative')
        self._refresh_tasks_panel()

    def _clear_finished_bg(self) -> None:
        """只清显示，不删记录（见 `_render_bg_section` 里的说明）。"""
        # ⚠️ 展开状态跟着一起清 —— 📌 否则那个集合只增不减
        #    （：任何列表落地时必须回答「谁来把它降下去」）。
        try:
            getattr(self, "_bg_expanded", set()).clear()
            self._bg_fp = None          # 强制下一次真的重画
        except Exception:
            pass
        self._bg_finished_hidden_before = time.time()
        self._refresh_tasks_panel()

    def _sync_task_pill(self, n: int) -> None:
        """聊天区那个 `x running task(s)`。

        ⭐ 早先定的形态是「**挂在最新一条回复气泡下面，始终跟随**（不是固定在
           页面某处）」，数量为 0 时整个消失，点击展开右侧抽屉。

        ⚠️ 实现是**一个** pill + 每次刷新 `move()` 到聊天容器末尾，
           而不是"每条气泡各挂一个再统一控制显隐"：
           📌 后者会在历史里留下一串隐藏的空壳，重放时还得一个个清 ——
              **一个始终跟随的东西，本身就该只有一个。**

        ⚠️ 与 常驻按钮的分工：
           **pill 管「向未来」**（还有事在跑、跟着气泡、归零消失）、
           **常驻按钮管「向过去」**（翻 Finished 历史）——
           📌 两个不同的钟，不合并。
        """
        try:
            _c = getattr(self, "chat_container", None)
            if _c is None:
                return
            with self._ui_scope():
                _pill = getattr(self, "_task_pill", None)
                # 🔴🔴 [2026-08-22] **pill 只在启动后出现一次，
                #    之后永远不再出现；重启又能再出现一次。抽屉全程正常。**
                #
                # ⭐ 根因不在这个函数的显示逻辑，在**重建条件**：
                #    原来只问 `if _pill is None:`——那问的是「**我建过没有**」，
                #    而真正要问的是「**它还在不在**」。
                #    📌 又一次「别用近似物回答一个能精确回答的问题」。
                #
                # 有两条路会让句柄活得比元素长，而且**都不报错**：
                #   ① `move()` **不是原子的**（NiceGUI 2.24 源码）：
                #        parent_slot.children.remove(self)   ← 先摘下来
                #        parent_slot.parent.update()         ← 这里一抛…
                #        parent_slot.children.insert(...)    ← 就永远挂不回去
                #      而下面那句原来是 `except Exception: pass` ——
                #      📌 **一个「先摘下来再挂上去」的操作被 except 吞掉时，
                #         会停在「摘下来了」那一半。**
                #   ② `chat_container.clear()`（对话重置 / 重放）会
                #      `client.remove_elements(...)` 把元素从注册表里**删掉**，
                #      句柄照旧留在 `self._task_pill` 上。
                #
                # 🔴 两条路的后果一样：`_pill is None` 永远不再成立 → 死句柄上
                #    `set_visibility(True)` **不报错也不显示** → 重启前 pill 回不来。
                # 📌 **一个「只创建一次」的句柄，必须有办法知道它指的东西还在不在** ——
                #    否则一次静默失败就是永久失效。
                # ⭐ 判据用**它还在不在聊天容器的孩子里**（level-triggered，
                #    每 2 秒重新问一次现状），不维护任何"它是否有效"的标志位。
                if _pill is not None:
                    try:
                        _alive = _pill in _c.default_slot.children
                    except Exception:
                        _alive = False
                    if not _alive:
                        logger.info("[L5] pill 已不在聊天容器里 → 重建"
                                    "（多半是对话重置/重放清过容器，或 move 半途失败）")
                        _pill = None
                        self._task_pill = None
                        self._task_pill_lbl = None
                if n <= 0:
                    if _pill is not None:
                        _pill.set_visibility(False)
                    return
                if _pill is None:
                    with _c:
                        # 🔴 实测 2026-08-20：整条 pill 都可点 —— 它是 `w-full`，
                        #    于是右边一大片空白也在点击范围里。
                        #    📌 **一个可点击区域的边界，应该等于它看起来的边界** ——
                        #       看不见的热区会让用户在"没点到东西"的地方触发跳转。
                        # ⭐ 修法：外层行**不再可点**，只有里面那一小段可点，
                        #    宽度由内容决定（`w-max`），`w-full` 留给外层做定位。
                        _pill = ui.row().classes(
                            'items-center w-full').style('padding:2px 0 6px 72px;')
                        with _pill:
                            _hot = ui.row().classes(
                                'items-center gap-1.5 cursor-pointer w-max').on(
                                'click', lambda _: self._show_right_panel('tasks'))
                            with _hot:
                                ui.label('✳').style(
                                    'font-size:var(--nano-fs-sm); color:var(--nano-amber); '
                                    'font-family:var(--nano-mono);')
                                self._task_pill_lbl = ui.label('').style(
                                    'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); '
                                    'font-family:var(--nano-mono);')
                                # ⭐ 「真的在跑」的动态感：三个点逐个出现再循环。
                                #    ⚠️ 单独一个 label —— 📌 与数字分开，
                                #       否则每 0.4 秒重写一次整句话，
                                #       文本长度一变就会把这一行的宽度也带着抖。
                                # ⚠️ 用**中点 `·`** 而不是句点 `.` ——
                                #    📌 句点坐在基线上，看起来吊在文字下面；
                                #       中点本身就在字高中间，与 `task` 齐平。
                                #       这是「换一个字符」而不是「调一个偏移量」：
                                #       偏移量在字号/缩放/字体回退变化时会再次错开。
                                self._task_pill_dots = ui.label('').style(
                                    'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); width:20px; '
                                    'letter-spacing:0.12em; '
                                    'font-family:var(--nano-mono);')
                        self._task_pill_tick = 0

                        def _dots(_self=self):
                            # ⚠️ pill 藏起来时**不要空转写 DOM** ——
                            #    📌 一个隐藏元素上的定时写入，是纯粹的浪费，
                            #       而且它会一直把这条 UI 通道占着。
                            try:
                                if not _self._task_pill.visible:
                                    return
                                _self._task_pill_tick += 1
                                _self._task_pill_dots.set_text(
                                    '·' * (_self._task_pill_tick % 4))
                            except Exception:
                                pass
                        ui.timer(0.4, _dots)
                    self._task_pill = _pill
                else:
                    # ⚠️ 重新挂到末尾 = 「跟着最新那条气泡」。
                    try:
                        _pill.move(_c)
                    except Exception as _e_mv:
                        # 🔴 **不许 `pass`** —— `move()` 先摘后挂，抛在中间时
                        #    元素已经离开了容器。静默吞掉 = 让 pill 永久消失，
                        #    而下一次调用还会因为句柄非空而不重建。
                        # ⭐ 如实记一条，并把句柄清掉 → 下一次（2 秒后）重建。
                        #    📌 失败的正确表现是「回到可恢复的状态」，不是「什么都不做」。
                        logger.warning(f"[L5] pill 重新挂载失败 → 下次重建: {_e_mv}")
                        self._task_pill = None
                        self._task_pill_lbl = None
                        return
                self._task_pill_lbl.set_text(
                    f"{n} running task{'s' if n != 1 else ''}")
                _pill.set_visibility(True)
        except Exception as e:
            logger.debug(f"[L5] 同步 pill 失败: {e}")

    def _render_plan_step(self, step: dict):
        """在 _plan_steps_container 内渲染单个步骤行（调用前已 clear）。"""
        status = step.get("status", "todo")
        desc   = step.get("desc", "")
        note   = step.get("note", "")

        status_cfg = {
            "todo":   ("radio_button_unchecked", "var(--nano-dim)", "var(--nano-panel)", "var(--nano-line)"),
            "doing":  ("pending",                "var(--nano-amber)", "var(--nano-panel-2)", "var(--nano-line)"),
            "done":   ("check_circle",           "var(--nano-ok)", "var(--nano-panel)", "var(--nano-line)"),
            "failed": ("cancel",                 "var(--nano-danger)", "var(--nano-panel-2)", "var(--nano-line)"),
        }
        icon_name, icon_color, bg_color, border_color = status_cfg.get(
            status, status_cfg["todo"]
        )
        text_style = 'text-decoration:line-through; color:var(--nano-fg-mute);' if status == "done" else 'color:var(--nano-fg);'

        with ui.row().classes('w-full items-start gap-2').style(
            f'background:{bg_color}; border:1px solid {border_color}; '
            f'border-radius:8px; padding:7px 10px;'
        ):
            ui.icon(icon_name).style(f'font-size:var(--nano-fs-2xl); color:{icon_color}; margin-top:1px; flex-shrink:0;')
            with ui.column().classes('gap-0').style('min-width:0;'):
                ui.label(desc).style(f'font-size:var(--nano-fs-base); line-height:1.4; {text_style}')
                if note:
                    ui.label(note).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); margin-top:2px;')

    def _settle_tool_pill(self, _rs):
        """工具阶段结束（最终答案开始/收尾）时，把动态 pill 定型成
        'used N tool(s) · M failed · 耗时'（有任一失败→叉子+红字）+ 移除扫光。
        幂等：靠 pill_settled 防重复。不在 tool_batch_end 调（那是每轮触发，
        会让 pill 在多轮工具间反复跳）。⚠️ 重放侧 `_render_durable_tool_batch`
        必须与此保持同一形状。"""
        if _rs.get("pill_settled"):
            return
        # 等待态 pill 不定型成 'Used N tools'——它要一直保持"⏸ 等待中"活着，
        # 直到被唤醒/取消时由 _settle_waiting_pill 收尾。
        if _rs.get("is_waiting_pill"):
            return
        _btc = _rs.get("batch_tool_count", 0)
        _lbl = _rs.get("tool_pill_lbl")
        if _btc <= 0 or not _lbl:
            return
        # ⭐⭐⭐ [实测] **这个批次里还有工具在跑 → 不许定型。**
        #
        # 🔴 实测看到的：一条 90 秒的命令被交还，**回看那一刻** pill
        #    变成了 `[✓] 2 tools · 55.2s` —— 而那条命令还在跑。两处都是假的：
        #    ① `[✓]` 宣称这批工具做完了 ② `55.2s` 不是它的真实用时，
        #       只是「回看碰巧发生在第 55 秒」。
        #
        # 🔴 根因：定型的触发点是「最终答案开始流出」（`final_text_delta`），
        #    那个触发点建立在一条**已经不成立**的等价关系上 ——
        #    📌 **「模型开始说话」曾经等价于「工具都做完了」；长任务被交还之后
        #       不再等价** —— 交还的全部意义就是「让它一边说话一边继续跑」。
        #    ⚠️ 这是「强制后台化」被删之后冒出来的**次生**缺陷：以前模型被要求
        #       结束这一轮，所以没有「一边说一边跑」这个状态。
        #       📌 一个前提被拿掉之后，依赖它的推断不会自己失效，只会变成错的。
        #
        # ⭐ 而正确的完成信号**已经存在**：`_settle_waiting_action` 只挂在
        #    `background`（真完成）上，刻意不挂 `timer`（回看只证明「该看一眼」）。
        #    所以这里只需要**让路**，由那条路去收 —— 顺带 `55.2s` 也自动变成
        #    真实用时，因为那时才算 `time.time() - batch_start_time`。
        #    📌 **两个症状同一个根因时，别分别修**（那会长出两套时间来源）。
        if self._pill_has_running_carrier(_lbl):
            logger.debug("[Pill] 本批次还有交还中的载体 → 暂不定型（等完成信号）")
            return
        with self._ui_scope():
            try:
                _fc = _rs.get("batch_fail_count", 0)
                # ⚠️ 缺 `batch_start_time` 时**不要**默认成"现在" —— 那会安静地
                # 显示成 `0.0s`，看起来像"这个工具瞬间就完了"，是个假事实。
                # 宁可不显示时长，也不要显示一个错的。
                _bst = _rs.get("batch_start_time")
                _el = (time.time() - _bst) if _bst else None
                if _el is None:
                    _dur = ""
                elif _el < 0.05:
                    # ⚠️ 不要显示 `0.0s`。它读起来像"这个工具根本没跑"，
                    # 用户两次都是先盯上这个数字才发现卡片有问题的。
                    # 真的很快就明说很快，别让一个真实的小数字长得像故障。
                    _dur = " · <0.1s"
                else:
                    _dur = f" · {_el:.1f}s"
                _mark = "[✓]" if _fc == 0 else "[✗]"
                _failtxt = f' · {_fc} failed' if _fc > 0 else ''
                # 形状：对钩/叉子（有任一失败→叉子） + used N tool(s) + M failed + 耗时
                _lbl.set_text(f'{_mark} used {_btc} tool{"s" if _btc != 1 else ""}{_failtxt}{_dur}')
                _lbl.classes(remove='nano-tool-active')
                _lbl.style(f'font-size:var(--nano-fs-base); font-weight:500; font-family:var(--nano-mono); '
                           f'color:{"var(--nano-ok)" if _fc == 0 else "var(--nano-danger)"};')
                # 定型后隐藏 $ 提示符（那是"运行中命令"用的）
                if _rs.get("tool_pill_dollar"):
                    _rs["tool_pill_dollar"].set_visibility(False)
                # failed 已并入主标签（顺序在耗时之前），清空这个独立后缀避免重复。
                if _rs.get("tool_pill_fail_lbl"):
                    _rs["tool_pill_fail_lbl"].set_text('')
            except Exception:
                pass
        _rs["pill_settled"] = True

    # ── 挂起/等待 UI ────────────────────────────────────────────────────

    def _snapshot_tool_pill(self, _rs) -> dict:
        """把当前那个工具批次 pill 的元素和起点记下来，供**载体真完成时**收尾。

        ⚠️ 只记**元素引用和数字**，不记 `_rs` —— 📌 快照的意义就是「不再依赖
           那个会变的东西」，顺手把 `_rs` 塞进来就等于没快照。
        """
        return {
            "lbl": _rs.get("tool_pill_lbl"),
            "dollar": _rs.get("tool_pill_dollar"),
            "fail_lbl": _rs.get("tool_pill_fail_lbl"),
            "count": _rs.get("batch_tool_count", 0),
            "fail_count": _rs.get("batch_fail_count", 0),
            "start_time": _rs.get("batch_start_time"),
        }

    def _register_hidden_waiting(self, *, suspension_id: str, action_ref=None,
                                 waiting_intent: str, bg_ref: str = "",
                                 pill: dict | None = None) -> None:
        """Keep completion/UI-epoch linkage for an internal wait without rendering a pill."""
        if not suspension_id:
            return
        self._waiting_pills = getattr(self, "_waiting_pills", {})
        self._waiting_pills[suspension_id] = {
            "hidden": True, "done": False, "waiting_intent": waiting_intent,
            "action_ref": action_ref, "action_done": False,
            "bg_ref": bg_ref,
            "resp_state": getattr(self, "_resp_state", None),
            # ⭐ 这一条等待「押着」哪个工具 pill（交还时快照，见调用点）
            "pill": pill or {},
        }

    def _make_pill_waiting(self, _rs, *, suspension_id: str, reason: str,
                           wake_on: list, timer_at, waiting_intent="condition_recheck",
                           action_ref=None):
        """把当前动态工具 pill（刚执行的 wait_for）切成"等待态"：
        顶行文字 → '⏸ 等待中'（保留 nano-tool-active 流光=活着），
        往 pill 行追加跳动计时器；只有用户委托的定时计划才有
        [立即执行][取消计划]，系统回看不暴露假控制面，并标记 is_waiting_pill
        让 _settle_tool_pill 不要把它定型成 'Used 1 tool'。
        """
        self._waiting_pills = getattr(self, "_waiting_pills", {})
        _lbl = _rs.get("tool_pill_lbl")
        _row = _rs.get("tool_pill_row")
        if _lbl is None or _row is None:
            return
        _rs["is_waiting_pill"] = True
        _start = time.time()
        try:
            _lbl.set_text('⏸ 等待中')
            _lbl.classes('nano-tool-active')  # 保活流光
            _lbl.style('font-size:var(--nano-fs-base); color:var(--nano-warn); font-weight:500;')
        except Exception:
            pass
        with _row:
            _timer_lbl = ui.label('0s').style(
                'font-size:var(--nano-fs-sm); color:var(--nano-warn); font-variant-numeric:tabular-nums; margin-left:2px;'
            )
            _btn_now = _btn_cancel = None
            if waiting_intent == "scheduled_timer":
                _btn_now = ui.button('立即执行', on_click=lambda: self._wake_now(suspension_id)).props(
                    'flat dense size=sm color=amber-9'
                ).style('font-size:var(--nano-fs-sm); min-height:0; padding:0 6px;')
                _btn_cancel = ui.button('取消计划', on_click=lambda: self._cancel_suspension(suspension_id)).props(
                    'flat dense size=sm color=grey-7'
                ).style('font-size:var(--nano-fs-sm); min-height:0; padding:0 6px;')

        def _tick():
            entry = self._waiting_pills.get(suspension_id)
            if not entry or entry.get("done"):
                return
            try:
                with self._ui_scope():
                    if timer_at:
                        _remain = int(timer_at - time.time())
                        _timer_lbl.set_text(f'{max(0, _remain)}s 后' if _remain > 0 else '即将继续')
                    else:
                        _timer_lbl.set_text(f'{int(time.time() - _start)}s')
            except Exception:
                pass

        _utimer = ui.timer(1.0, _tick)
        self._waiting_pills[suspension_id] = {
            "txt": _lbl, "timer_lbl": _timer_lbl,
            "btn_now": _btn_now, "btn_cancel": _btn_cancel,
            "ui_timer": _utimer, "done": False, "reason": reason,
            "waiting_intent": waiting_intent,
            # 工具明细行仍画在原回应里；后台真正返回时要把它的 spinner 收成终态。
            "action_ref": action_ref, "action_done": False,
            # ⭐⭐ [2026-08-09] **记住这个 pill 属于哪一段回应期。**
            #    唤醒时如果还是同一段（用户没在中间说过话），就该**续接进那个气泡**，
            #    而不是新开一个 `nano ❯`。
            # ⭐ 用 `_resp_state` 的**对象身份**而不是计数器：
            #    每来一条新的用户消息，`start_pipeline_task` 都会新建一个
            #    `_resp_state` dict → 身份自然不同。
            #    📌 **能用「是不是同一个对象」判断的事，不要另立一个计数器** ——
            #       计数器需要有人记得维护，而身份是天然的。
            "resp_state": getattr(self, "_resp_state", None),
        }

    def _pill_has_running_carrier(self, lbl) -> bool:
        """这个 pill 上还押着没完成的载体吗。

        ⚠️ **level-triggered：直接数权威（`_waiting_pills`），不维护计数器。**
           📌 一个「还有几个没回来」的数，如果由两处 +1/-1 维护，
              它迟早和事实分叉；而事实本来就在表里，数一遍就有。
        ⚠️ 判据是「**是不是同一个 pill 元素**」，不是「是不是同一段回应期」——
           后者会把回看轮新开的批次也一起锁住（那个批次里没有长任务）。
           📌 能用「是不是同一个对象」判断的事，不要绕道去比别的东西。
        """
        if lbl is None:
            return False
        for entry in (getattr(self, "_waiting_pills", None) or {}).values():
            if entry.get("action_done"):
                continue
            if (entry.get("pill") or {}).get("lbl") is lbl:
                return True
        return False

    def _settle_pill_snapshot(self, pill: dict, ok: bool) -> None:
        """载体真完成时，按**交还那一刻的快照**把那个 pill 定型。

        ⚠️ 时长在这里才算 —— 那才是这批工具的**真实用时**。
        """
        _lbl = (pill or {}).get("lbl")
        if _lbl is None:
            return
        _btc = pill.get("count", 0) or 0
        if _btc <= 0:
            return
        _fc = pill.get("fail_count", 0) or 0
        if not ok:
            _fc = max(1, _fc)
        _bst = pill.get("start_time")
        # ⚠️ 缺起点时**不显示**时长，而不是显示 0.0s（见 `_settle_tool_pill`
        #    里同一条理由：宁可不显示，也不要显示一个错的）。
        _el = (time.time() - _bst) if _bst else None
        _dur = "" if _el is None else (" · <0.1s" if _el < 0.05 else f" · {_el:.1f}s")
        try:
            with self._ui_scope():
                _mark = "[✓]" if _fc == 0 else "[✗]"
                _failtxt = f' · {_fc} failed' if _fc > 0 else ''
                # 与 _settle_tool_pill / 重放侧同一形状：used N tool(s) + M failed + 耗时
                _lbl.set_text(f'{_mark} used {_btc} tool{"s" if _btc != 1 else ""}{_failtxt}{_dur}')
                _lbl.classes(remove='nano-tool-active')
                _lbl.style(f'font-size:var(--nano-fs-base); font-weight:500; '
                           f'font-family:var(--nano-mono); '
                           f'color:{"var(--nano-ok)" if _fc == 0 else "var(--nano-danger)"};')
                if pill.get("dollar") is not None:
                    pill["dollar"].set_visibility(False)
                # failed 已并入主标签，清空独立后缀避免重复。
                if pill.get("fail_lbl") is not None:
                    pill["fail_lbl"].set_text('')
        except Exception as e:
            logger.debug(f"[Pill] 按快照定型失败（不影响唤醒）: {e}")

    def _settle_waiting_action(self, suspension_id: str, ok: bool) -> None:
        """后台载体真正返回时，收掉与这条等待关联的工具明细 spinner。

        定时回看只证明“该看一眼”，不证明载体完成，所以调用点只在 background
        信号上。幂等且容忍旧 DOM 已删除，异步收尾不能因此拖垮唤醒。
        """
        entry = (getattr(self, "_waiting_pills", None) or {}).get(suspension_id)
        if not entry or entry.get("action_done"):
            return
        ref = entry.get("action_ref") or {}
        # ⚠️ **`action_done` 必须无条件标上**，哪怕没有明细行可收。
        #    🔴 原来是 `if not ref: return`（不标就走），而 `action_done`
        #       现在还是「这个 pill 还押着载体吗」的判据 ——
        #       不标就等于那个 pill **永远**定不了型，一直挂着流光。
        #    📌 **一个状态位一旦有了第二个读者，它的每一条设定路径都要重新过一遍**
        #       —— 原来它只防重复收明细行，漏一次无害。
        entry["action_done"] = True
        if ref:
            try:
                with self._ui_scope():
                    if ref.get("spin") is not None:
                        ref["spin"].set_visibility(False)
                    if ref.get("done") is not None:
                        ref["done"].set_text("✓" if ok else "✕")
                        ref["done"].style(
                            "font-size:var(--nano-fs-sm); font-weight:500; font-family:var(--nano-mono); "
                            f"color:{'var(--nano-ok)' if ok else 'var(--nano-danger)'};")
            except Exception as e:
                logger.debug(f"[Suspension] 收工具明细终态失败（不影响唤醒）: {e}")
        # ⭐ 载体真完成了 → 现在才轮到那个工具批次 pill 定型（用真实用时）
        self._settle_pill_snapshot(entry.get("pill") or {}, ok)

    def _settle_cancelled_handback_actions(self, bg_ref: str) -> int:
        """A cancelled recheck must not wake Nano, but its carrier may still finish.

        In that case the wait record intentionally no longer appears in ``list_live``.
        The original tool action nevertheless needs a terminal UI state; otherwise its
        spinner becomes a permanent false claim that the command is still running.
        """
        if not bg_ref:
            return 0
        settled = 0
        for suspension_id, entry in (getattr(self, "_waiting_pills", None) or {}).items():
            if entry.get("bg_ref") != bg_ref or entry.get("action_done"):
                continue
            self._settle_waiting_action(suspension_id, ok=True)
            settled += 1
        return settled

    def _settle_waiting_pill(self, suspension_id: str, final_text: str, color: str = "var(--nano-dim)"):
        """挂起结束（恢复/取消）时，把活 pill 定型成静态状态、停掉计时器、移除按钮。"""
        self._waiting_pills = getattr(self, "_waiting_pills", {})
        entry = self._waiting_pills.get(suspension_id)
        if not entry or entry.get("done"):
            return
        entry["done"] = True
        if entry.get("hidden"):
            return
        try:
            with self._ui_scope():
                try:
                    entry["ui_timer"].cancel()
                except Exception:
                    pass
                entry["txt"].classes(remove='nano-tool-active')
                entry["txt"].set_text(final_text)
                entry["txt"].style(f'font-size:var(--nano-fs-base); color:{color}; font-weight:500;')
                entry["timer_lbl"].set_text('')
                if entry.get("btn_now") is not None:
                    entry["btn_now"].set_visibility(False)
                if entry.get("btn_cancel") is not None:
                    entry["btn_cancel"].set_visibility(False)
        except Exception:
            pass

    def _settle_all_waiting_pills(self, final_text: str = "▶ 继续", color: str = "var(--nano-ok)"):
        """用户发新消息时调用：把**已经真的结束**的等待 pill 收尾。

        ⚠️⚠️ 这个函数原来的注释是：

            「orchestrator 会把**所有** active 挂起按 user 唤醒并恢复，
              所以这里把所有还在跳的等待 pill 一并收尾」

        那句话**曾经为真、后来被改掉了**（background-only 早就有 `continue` 豁免，
        ②b 之后更是一条都不 resolve），而这里的代码一直按那个旧假设无脑收全部。

        实测后果：一条 background-only 挂起活着时，用户问了句「现在几点了」，
        pill 立刻定型成「▶ 你回来了，继续」并打上 **✓** —— 而记录还活着、还在每轮注入。
        **屏幕说完成了，系统还在等。**

        📌 判据（外部评审 交叉评审的不变量④，我们独立撞到同一条）：
           **UI 不能自己推断"完成"。** ✓ 必须来自权威状态，
           而不是"用户发消息了"这种与完成无关的事件。

        📌 更一般的那条：**注释里对别的模块行为的假设，
           会在那个模块改动时悄悄变成谎言。** 这里就是活标本。

        现在：逐条回查权威，只收**真的不在 active 里**的。
        """
        self._waiting_pills = getattr(self, "_waiting_pills", {})
        if not self._waiting_pills:
            return
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            _alive = {r.wait_id for r in _wc.list_live(get_kernel(), oldest_first=True)}
        except Exception as e:
            # ⚠️ 读不到权威时**什么都不收**（而不是全收）。
            #    多留一个转圈的 pill，好过谎报一个 ✓ —— 前者用户看得出不对劲，
            #    后者会让用户以为事情办完了。
            logger.warning(f"[Suspension] 读取活跃挂起失败，本次不收 pill（避免谎报完成）: {e}")
            return
        for sid in list(self._waiting_pills.keys()):
            if sid in _alive:
                continue      # 还活着 —— 它没完，别打勾
            # ⭐⭐⭐ [2026-08-09 实测] **措辞也必须从权威读，不能由调用方给。**
            #    实测现象：Nano 自己调 `cancel_wait` 取消了那条定时（**取消是真的，
            #    不会再触发**），但 pill 照旧数完、然后翻成「等待中 · 即将继续」——
            #    用户强制点「继续」才看到那句「这个等待已经结束了」。
            # ⭐ 用户的判断（采纳了）：「**结束 = UI 跟上 = 那个按钮不该存在**」。
            # ⚠️ 而这函数原来收 pill 时用的是调用方传进来的 `final_text`
            #    （唯一调用点传的是「▶ 你回来了，继续」）—— 对一条**被取消**的等待，
            #    那句话是**假的**。
            # 📌 这正是本函数 docstring 里那条判据的**同族第二例**：
            #    「**UI 不能自己推断「完成」，✓ 必须来自权威状态**」——
            #    上一次修的是「该不该收」，这一次是「**收成什么字**」。
            #    📌 **一个「必须来自权威」的判断，它的措辞也必须来自权威** ——
            #       否则会出现「收得对、说得错」，而那比不收更难发现。
            _txt, _col = self._pill_settle_words(sid, final_text, color)
            self._settle_waiting_pill(sid, _txt, color=_col)

    def _pill_settle_words(self, suspension_id: str, fallback_text: str,
                           fallback_color: str) -> tuple[str, str]:
        """一条已经不在 active 里的等待，pill 该定型成什么字 —— **回权威问**。

        ⚠️⚠️ **第二版（2026-08-09 实测 打回后重写）。**
        🔴 第一版读的是切读期的兼容门面，它把新的六态**折成旧的两态**，
           于是一条 `CANCELLED` 出来是 `"resolved"`，我判 `== "cancelled"` 永远为假，
           结果 pill 落到兜底文案「▶ 继续」（绿色）—— 正是 用户截图里那个。
        📌 **一个兼容层刻意丢掉的信息，不会因为下游需要它而回来。**
        📌 **写一个新的消费者时，要先读它数据来源的「忠实度表」** ——
           不许假设「字段名一样 ⇒ 语义一样」。当时兼容投影已经把
           `status` / `resolved_by` 标成有损，而消费者仍把它们当完整事实。
        ⭐ 讽刺的是这个 fix 本身的判据就是「措辞必须来自权威」——
           而当时读的是**兼容投影**，不是权威。
           📌 **「读权威」不只是「别读缓存」，还包括「别读一个降了分辨率的投影」。**

        ⚠️ **「是谁取消的」刻意不区分**：权威里**压根没记**这件事 ——
           `resolution` 是固定枚举（`USER_CANCELLED` 等），
           取消入口传的 `"model-cancel"` 进的是命令 `reason`，不落库。
           📌 **一行系统注记多说一句，就多一个可能为假的断言**（今天第二次用到）——
              而「是谁取消的」就写在上面那句 Nano 的话里，pill 不必替它说。

        ⚠️ 读不出记录时退回调用方给的兜底，不猜 —— 少说比说错好。
        """
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            rec = _wc.find_by_id(get_kernel(), suspension_id)
        except Exception:
            return fallback_text, fallback_color
        if rec is None:
            # 记录压根没了（历史清理过）→ 只说「结束了」，不声称怎么结束的
            return "✓ 已结束", "var(--nano-dim)"
        _S = _wc.WaitStatus
        if rec.status == _S.CANCELLED:
            return "✕ 已取消等待", "var(--nano-fg-soft)"
        if rec.status == _S.EXPIRED:
            return "✕ 没等到（已过期）", "var(--nano-fg-soft)"
        if rec.status == _S.ORPHANED:
            # ⚠️ 这一档要**看得出来不对劲**：它意味着等的东西再也没回来，
            #    是被兜底回收的。用户有权知道这不是正常收尾。
            return "✕ 等的东西没回来", "var(--nano-danger)"
        if rec.status in (_S.SATISFIED, _S.CONSUMED):
            # ⭐ 具体是哪种唤醒也回权威读（`satisfied_by`）——
            #    否则 tick 抢在唤醒路径前面时会用通用措辞盖掉更具体的那句，
            #    而那种降级**只在偶发时出现**，是最难查的一类。
            return ({"background": ("▶ 后台完成，继续", "var(--nano-ok)"),
                     "timer":      ("▶ 到点了，继续", "var(--nano-ok)"),
                     "manual":     ("▶ 已手动继续", "var(--nano-ok)")}
                    .get((rec.satisfied_by or "").lower(),
                         (fallback_text, fallback_color)))
        return fallback_text, fallback_color

    async def _wake_now(self, suspension_id: str):
        """[立即执行]：用户提前触发自己委托的定时计划。"""
        from core.runtime.kernel import get_kernel
        from core.runtime import waitcond as _wc
        rec = _wc.find_by_id(get_kernel(), suspension_id)
        if rec is None or not rec.is_live:
            ui.notify('这个等待已经结束了', type='info')
            self._settle_waiting_pill(suspension_id, '✓ 已结束')
            return
        # ⭐⭐ 这里原来是 `notify + return` —— **点了「继续」但内核忙 →
        #    那个意图就没了**，用户得自己记着再点一次。
        #    这是 `pipeline_lock` 五个使用点里最后一个还在丢用户意图的。
        #    📌 同 ①②：**闸的出口是失败，队列的出口是稍后处理。**
        # ⚠️ 而 `_drive_wake` 那处（定时/后台唤醒）**本来就是对的** ——
        #    它写着「内核忙：稍后由 poller 再尝试（记录仍 active）」。
        #    📌 一个正确做法已经在代码里存在、却没被推广到同类场景，
        #       缺的不是想法，是一致性。
        if self.pipeline_lock.locked():
            _wid = _rt_inbox_submit_wake(suspension_id, "manual")
            self._rt_inbox_parked[_wid or f"memwake_{suspension_id}"] = \
                ("wake", suspension_id, "manual")
            self._settle_waiting_pill(suspension_id, '▶ 已排队，稍后继续',
                                      color="var(--nano-fg-soft)")
            logger.info(f"[Inbox] 内核忙 → 手动继续进队列（{suspension_id}）")
            return
        self._settle_waiting_pill(suspension_id, '▶ 已手动继续', color="var(--nano-ok)")
        await self._drive_wake(suspension_id, trigger="manual")

    async def _cancel_suspension(self, suspension_id: str):
        """[取消计划]：取消用户委托的定时计划，不再唤醒。"""
        from core.runtime import waitcond as _wc
        ok = _wc.cancel_wait(suspension_id)
        # ⚠️ 这一处**差点漏掉**。前面四个镜像点都在 orchestrator 里，
        # 唯独 UI 的「取消」按钮在这里 —— 漏了它会造成"旧死新活"的**假分歧**，
        # 而那正是对答案里最危险的那个方向（它本该意味着"有条关闭路径没镜像到"）。
        # 📌 判据：**镜像点要按"权威被改动的地方"去找，不是按模块去找。**
        # ⚠️ 这里原本还会关闭一次观测期的镜像。
        #    上一行已经直接把权威那条关掉；镜像关闭是**对同一件事关第二次**，
        #    而且依赖一个重启就丢失的内存映射。
        # ⭐ 而这一处当年是**四个镜像点里差点漏掉的那一个**（早先的判据：
        #    「镜像点要按『权威被改动的地方』去找，不是按模块去找」）——
        #    现在它连同镜像机制一起退役了。
        # 📌 **一条当年靠「别漏掉」才立住的规则，最好的结局是那件要做的事本身消失。**
        self._settle_waiting_pill(suspension_id, '✕ 已取消等待', color="var(--nano-fg-soft)")
        if ok:
            try:
                self.agent.memory.add_system_note(
                    "assistant", "[System record: the user cancelled the pending wait above; Nano will not continue waiting.]"
                )
            except Exception:
                pass

    def _park_wake(self, suspension_id: str, trigger: str, note: str,
                   why: str) -> None:
        """唤醒起不来 → **把它排进 durable inbox**，锁/预算恢复后由排空接上。

        ⚠️⚠️ **两条早退路径共用这一个门面，是被自己的判据逼出来的。**
        `_drive_wake` 有两处会提前返回：① 内核忙 ② 预算到硬上限。
        修的是 ①，而 ② **原样留着静默 `return`** ——
        对 `timer` 无害（poller 会重试，`fire_at` 在过去、记录仍 active），
        但对 `background` 又是**同一个静默丢失**，只是触发原因从「忙」换成「超预算」。
        📌 **刚写下「一个修法只改了其中一条调用路径，另一条仍在说谎」，
           然后在同一个函数里重复了它** ——
           所以这里不是各修一遍，而是**把出口收成一个**。
        📌 **同一种失败要走同一个出口，否则「补齐所有出口」这件事永远做不完。**

        ⚠️ **去重**：预算硬上限不是瞬时状态，可能连续多轮都满。
           每次重新入队都会写一条 durable inbox 行 → 那是个泄漏。
           所以先看这条挂起是不是已经排着了。
           📌 **一个「稍后再试」的队列必须对同一件事去重，
              否则「稍后」的次数会变成行数。**
        """
        parked = getattr(self, "_rt_inbox_parked", None)
        if parked is None:
            logger.error(f"[Suspension] 🔴 {trigger} 唤醒无处可排（{why}）"
                         f"—— 这个完成通知丢了：{suspension_id}")
            return
        for _v in parked.values():
            if (isinstance(_v, tuple) and len(_v) > 1
                    and _v[0] == "wake" and _v[1] == suspension_id):
                logger.debug(f"[Inbox] {suspension_id} 的唤醒已在队列里，不重复排（{why}）")
                return
        try:
            _wid = _rt_inbox_submit_wake(suspension_id, trigger)
            parked[_wid or f"memwake_{suspension_id}"] = (
                "wake", suspension_id, trigger, note)
            logger.info(f"[Inbox] {why} → {trigger} 唤醒进队列（{suspension_id}）")
        except Exception as e:
            # ⚠️ 连队列都进不去 → **响亮报错**。这条路径丢掉的是
            #    「后台任务已经完成」这个事实，而它不会有第二次机会。
            logger.error(f"[Suspension] 🔴 {trigger} 唤醒既起不来也进不了队列 "
                         f"（{suspension_id}，{why}）—— 这个完成通知丢了: {e}")

    async def _drive_wake(self, suspension_id: str, trigger: str, note: str = "",
                          inbox_item_id: str | None = None):
        """唤醒轮的**唯一出口**：不管里面从哪条路返回，那条 inbox 记录都要收掉。

        🔴🔴 [2026-08-22 实测] 问题：`_drain_inbox` 的 wake 分支
           `claim` 了一条记录，而消费只发生在 `_safe_execute_pipeline` 的收尾里
           —— **唤醒不走那个函数**。于是记录永远停在 `CLAIMED`，
           而 `_claim` 的第一道闸是「已经有一条 CLAIMED → 直接返回 None」：
             → 此后每一次认领静默失效 → `delivery_count` 再也不涨
             → 之后每一次 consume 都破 `inbox_consumed_was_delivered`
           实测里这条 ERROR 连着出现 **5 次**，第一次正好在第一次 wake 走 drain 之后。
        📌 **一条卡住的 CLAIMED，会把整条队列的投递记账全废掉** ——
           而 `delivery_count` 存在的唯一理由，是回答「崩溃时这条给模型看过没有」。

        ⚠️⚠️ **为什么是包一层，而不是在每个出口各收一次**：
           里面有三条早退（无处可排 / 内核忙 / 预算满）加正常路径加异常路径。
           第一版就是逐个补，**当场漏了预算满那条** ——
           📌 **逐出口补丁的正确性依赖「我数全了」，而包一层不依赖任何人记得。**
              前者还会随着将来新增一条 `return` 再次失效，且失效时不报错。
        ⚠️ 收账失败不许影响能力：它是**记账**，不是这一轮该干的事。
        """
        try:
            # ⭐ `inbox_item_id` 有值 ⟺ 这条唤醒当初**触发时前台上有东西**
            #    （忙才会走 `_park_wake` 进队列）。见 `_drive_wake_inner` 里
            #    气泡规则那段：**走了哪条路，本身就记录了触发那一刻的状态。**
            await self._drive_wake_inner(suspension_id, trigger, note,
                                         busy_at_trigger=bool(inbox_item_id))
        finally:
            if inbox_item_id:
                try:
                    _rt_inbox_consume(inbox_item_id)
                except Exception as _e_ic:
                    logger.warning(f"[Inbox] 唤醒轮收尾消费失败: {_e_ic}")

    async def _drive_wake_inner(self, suspension_id: str, trigger: str,
                                note: str = "", busy_at_trigger: bool = False):
        """定时/后台/手动唤醒：起一个新回复 turn，复用 navigate_pipeline 整套渲染，
        事件源换成 orchestrator.resume_suspension。"""
        if self.pipeline_lock.locked():
            # ⭐⭐⭐ [2026-08-09 实测] **这里原来是静默 `return`**，注释写着
            #    「内核忙：稍后由 poller 再尝试（记录仍 active）」。
            #
            # 🔴 **那句承诺对 `background` 唤醒是【假的】**：poller
            #    （`_suspension_poll_tick`）走的是 `due_timers()`，SQL 条件是
            #    `fire_at IS NOT NULL AND fire_at <= now` —— 而后台等待的
            #    `fire_at` 是 `None`，**它永远不会被轮到**。
            #
            # ⚠️ 而 MCP 自动后台化**必然**撞上这个：调用是在**一轮进行中**被转后台的，
            #    页面往往几百毫秒后就完成，那时那一轮还握着 `pipeline_lock` →
            #    唤醒被丢掉 → Nano 永远停在「等着看结果」。
            #    实测现象：网页确实打开了，pill 甚至显示「▶ 后台完成，继续」，
            #    **但那一轮再也没有下文**。
            #
            # 📌 **一句「稍后由 X 再试」的注释，必须能指出 X 真的会再试。**
            #    此前把这一处判成「本来就是对的」，依据正是这句注释本身 ——
            #    而没去看 poller 到底轮什么。
            #    📌 **判断一处「本来就是对的」，不能只读它的注释说它交给了谁，
            #       要去看那个「谁」是不是真的会接。**
            #
            # ⭐ 修法不是给 poller 加一条「也轮 background」——那还是 edge 思维，
            #    而是用**已经存在的那条正确做法**：`_wake_now` 在内核忙时把唤醒意图
            #    落进 durable inbox，锁一释放就被排空接上。
            #    📌 **闸的出口是失败，队列的出口是稍后处理**（本项目第五次用到）。
            #    📌 **一个正确做法已经在代码里存在、却没被推广到同类场景 ——
            #       缺的不是想法，是一致性。**（这条判据当初就是从 `_wake_now`
            #       那一处立的，而它当时把 `_drive_wake` 当成了正面例子。）
            self._park_wake(suspension_id, trigger, note, "内核忙")
            return
        # 预算已满就别起唤醒 turn。orchestrator.resume_suspension 里也有一道
        # 同样的检查（那道是保护挂起记录不被白白消费，是正确性防线）；这里这道
        # 纯粹是为了 UI 不说谎——下面 _settle_waiting_pill 会把等待 pill 定型成
        # "▶ 时间到，继续"，要是随后什么都没发生，用户看到的就是一句空承诺。
        try:
            from core.usage import sync_budget_health
            if sync_budget_health() == "hard":
                # ⭐ [2026-08-09] 这里原来也是静默 `return` + 一行 warning。
                #    对 `timer` 无害（poller 会重试），但 `background` 的 `fire_at`
                #    是 `None`，**poller 永远轮不到它** —— 于是同一个静默丢失
                #    换了个触发原因（超预算而不是忙）。
                # 📌 **同一种失败要走同一个出口**，见 `_park_wake` 的说明。
                logger.warning(f"[Suspension] 预算已达硬上限，本次不唤醒 "
                               f"{suspension_id}（记录保持 active，唤醒进队列）")
                self._park_wake(suspension_id, trigger, note, "预算已达硬上限")
                return
        except Exception:
            pass
        async with self.pipeline_lock:
            self._speaker.set_responding(True)
            self._intel_engine.set_responding(True)
            # 真正拿到锁、即将起唤醒 turn 时才把活 pill 定型（避免内核忙时提前定型）
            _settle_txt = {"timer": "▶ 时间到，继续", "manual": "▶ 已手动继续",
                           "background": "▶ 后台完成，继续"}.get(trigger, "▶ 继续")
            if trigger == "background":
                # 只有载体的完成信号能结束动作 spinner；timer 回看绝不冒充完成。
                self._settle_waiting_action(suspension_id, ok=True)
            self._settle_waiting_pill(suspension_id, _settle_txt, color="var(--nano-ok)")
            # ⭐⭐⭐ [2026-08-09] **同一段回应期里的唤醒，续接进同一个气泡。**
            #
            # 用户的原话：「并没有插话进去，所以这里不应该产生后两次 >NANO，
            # 而是在一个气泡当中继续（**回看同理**），只要不出现用户插话。」
            # ⭐ 判据成立：`nano ❯` 那个头代表的是「**Nano 对用户的一次回应**」——
            #    用户没说话，就不该有第二个头。
            # 📌 **一个 nano 气泡 = 一段连续的回应期**（这条判据早先就立了）。
            #
            # ⭐ 这里**不新增代码路径**，直接打开 `navigate_pipeline` 已有的
            #    `_resp_continuation` 那条续接分支 —— 它会复用 markdown 元素、
            #    把 `running` 拨回 True、重新显示转圈、藏掉已经亮出来的 ✦，
            #    并且跳过 `usage_tracker.reset_session()`（token 页脚仍只在
            #    整段结束时算一次）。
            #    📌 **复用一条已经被实测验过的路径，比新写一条等价的更安全。**
            #
            # 🔴🔴 **这一段原来落在了 `_safe_execute_pipeline` 里** —— 那个函数
            #    没有 `suspension_id` 这个名字，于是实测上每一次普通发送都
            #    `NameError`。而消费它的 `if _same_epoch:` 留在了这里。
            #    ⚠️ 全库 1630 条全绿，是因为**没有一条断言验作用域**：那条
            #       结构断言只问了「`_resp_continuation = True` 在不在
            #       `if _same_epoch:` 底下」，而 `_same_epoch` 在哪定义、
            #       和它同不同函数，没人问。
            #    📌 **一条验「消费者在不在」的断言，不等于验了「它读的名字
            #       在同一个作用域里被定义过」** —— 跨函数搬代码时，
            #       前者永远通过。
            # ⭐⭐⭐ [2026-08-22] **合并判据换成一条能直接读的事实。**
            #
            #     合并 ⟺ 最新气泡是 nano 的 ∧ **触发那一刻前台上有东西**
            #
            # 🔴 这里原来比对的是 `_waiting_pills[sid]["resp_state"]` 与当前
            #    `_resp_state` 的**对象身份** —— 而那个指针记的是【交还那一刻】
            #    是哪个回应期。回看轮和用户新消息**都会换掉 `_resp_state`**，
            #    于是它一路过期：实测 里 6 次唤醒只有 2 次续接成功，
            #    里又漏一次。补一条路径就再漏一条 ——
            #    📌 **一个需要多处同步才能保持正确的记录点，
            #       换成一个随时可直接读的事实。**
            #
            # ⭐ 为什么判据是「前台」而不是「有没有转圈的 pill」：
            #    后台的东西没完成时 pill **本来就该转**（UI 如实报事实）——
            #    那条 pill 说的是「它还没好」，不是「Nano 还在忙」。
            #    📌 **用一个回答 A 的信号去回答 B，它再准也是错的。**
            #    而「前台」正好是比喻里**唯一的独占资源**（后台可并行、
            #    前台一次一个），所以「前台空了」精确对应
            #    「Nano 这一口气说完了」—— 正是人类聊天换气泡的那个瞬间。
            #
            # ⚠️⚠️ **必须是「触发那一刻」，不是「渲染那一刻」。**
            #    忙时触发的唤醒会先进 inbox 排队，等排到它时上一轮早已结束、
            #    锁也释放了 —— 那时再问「前台有没有东西」得到的是**另一个时刻**
            #    的答案（空），会把该并入的判成新气泡。
            #    ⭐ 而代码天然已经把两条路分开了：触发时忙 → `_park_wake` 进队列；
            #       触发时闲 → 直接进来。**走了哪条路本身就是那个记录**，
            #       所以不用新加时间戳（`busy_at_trigger` 就是它）。
            #
            # ⚠️ 这条判据刻意**一个字都没提**回看/后台唤醒/主动开口 ——
            #    📌 以后再加多少种触发源，它都不用改：它问的只是
            #       「上一个气泡还是不是 Nano 正在说的那一口气」。
            _live_rs = getattr(self, "_resp_state", None)
            _same_epoch = bool(busy_at_trigger) and _live_rs is not None
            # ⭐⭐ **续接分支还需要一样只在 `else` 里被创建的东西：`loading_container`。**
            #    它下面要传给 `navigate_pipeline`。续接时它必须是**活着那个容器**。
            # 📌 **一个 if/else 里只在 else 分支赋值的局部变量，是 if 分支的
            #    隐藏依赖** —— 只打开开关不补依赖，if 分支必然 NameError。
            #    （这正是上面那个 bug 的第二层，原来的补丁两层都错了。）
            # ⚠️ 拿不到容器就**退回新开一个**，而不是崩掉 ——
            #    唤醒轮的价值在于「Nano 接着说下去」，气泡长相是次要的。
            loading_container = None
            if _same_epoch:
                loading_container = (_live_rs or {}).get("container")
                if loading_container is None:
                    logger.warning(f"[Wake] {suspension_id} 判为同一回应期，但活着的"
                                   f"容器已经不在了 → 退回新开一个 nano ❯")
                    _same_epoch = False
            if _same_epoch:
                logger.info(f"[Wake] {suspension_id} 仍在同一段回应期 → 续接进原气泡"
                            f"（不新开 nano ❯）")
                # ⚠️ 同一段回应期 → **跳过整段「重建 nano 块」**，只打开续接开关。
                #    ⚠️ 也**不许删 `_last_meta_row`** —— 那是这一段自己的元信息行，
                #       续接之后它还要继续用（token 统计等整段结束才写）。
                #       📌 「上一条的元信息行」这个说法在续接场景里不成立：
                #          此刻那个元信息行不是上一条的，是**当前这一段**的。
                self._resp_continuation = True
            else:
                with self._ui_scope():
                  self._clear_empty_state_greeting()
                  try:
                      if getattr(self, '_last_meta_row', None):
                          self._last_meta_row.delete()
                  except Exception:
                      pass
                  with self.chat_container:
                      loading_container = ui.column().classes('w-full py-1 mb-8')
                      with loading_container:
                          with ui.row().classes('items-start gap-2 no-wrap w-full min-w-0'):
                              ui.label('nano ❯').style(
                                  'color:var(--nano-ok); font-size:var(--nano-fs-lg); line-height:1.75rem; flex-shrink:0; min-width:64px; text-align:right;'
                                  'font-family:var(--nano-mono);')
                              _inner_col = ui.column().classes('w-full gap-0 min-w-0')
                              with _inner_col:
                                  _c_md = nano_md()
                          with ui.row().classes('items-center gap-1.5 mt-1').style('padding-left:72px;') as _meta_row:
                              _spin_lbl = ui.label('⠋').style('font-size:var(--nano-fs-md); color:var(--nano-dim); font-family:var(--nano-mono);')
                              _svg_el = ui.html(NANO_AVATAR_SVG).style('width:16px; height:16px; flex-shrink:0; display:none;')
                              _s_lbl = ui.label('thinking · 0s').style(
                                  'font-size:var(--nano-fs-base); color:var(--nano-dim); letter-spacing:0.01em; font-family:var(--nano-mono);'
                              )
                      self._last_meta_row = _meta_row
                      self._resp_state = ViewSession(container=loading_container, meta_row=_meta_row,
                          pending_epoch=False,
                          status_lbl=_s_lbl, spin_lbl=_spin_lbl, svg_el=_svg_el,
                          start_time=time.time(), tok_base=sum(usage_tracker.session_tokens()), running=True,
                          content_md=_c_md, current_text="", loading_col=_inner_col,
                          tool_count=0, had_text_since_tool=True, batch_tool_count=0,
                          tool_pill_lbl=None, tool_pill_arrow=None, tool_details_col=None,
                          action_refs={}, text_checkpoint="",)
                      self._current_loading_label = _s_lbl
                      # 🪦 这里曾经回填 `_pill_entry["resp_state"] = self._resp_state`
                      #    —— 那是给上一版「对象身份比对」续命的补丁。
                      # 2026-08-22 判据换成「触发那一刻前台上有东西」之后，
                      # `_pill_entry` 的那个字段**再没有任何读取点**，所以一并拆掉。
                      # 📌 **留着一个零读取点的写入，是「写好但没人调」的反面镜像** ——
                      #    它同样会让下一个人以为这里有一套还在生效的机制。
                  try:
                      self.scroll_area.scroll_to(percent=1.0, duration=0.1)
                  except Exception:
                      pass
            try:
                await self.navigate_pipeline(
                    None, loading_container,
                    event_source=self.agent.resume_suspension(suspension_id, trigger, note=note),
                )
                self._activity.on_nano_event("nano_responded")
            except Exception as e:
                logger.error(f"[Suspension] 唤醒 turn UI 异常: {e}")
                try:
                    self._resp_state["running"] = False
                except Exception:
                    pass
            finally:
                self._speaker.set_responding(False)
                self._intel_engine.set_responding(False)
        # ⭐⭐ 唤醒轮结束后**也要**排空队列。
        #
        # ⚠️⚠️ 漏了这一处的后果：用户在**唤醒轮**跑的时候发的消息会一直排着，
        #    直到下一次有普通 turn 结束才被处理 —— 而如果没有下一次，就是永远。
        #    📌 **一个「锁释放后要做的动作」，必须挂在每一个持有那把锁的地方** ——
        #       只挂一处等于只在一半情况下生效。
        #    ⭐ 这和「`pipeline_lock` 五个使用点被处理成四种语义」是同一个问题：
        #       **缺的不是想法，是一致性。**
        # ⚠️ 同样必须在 `async with` **之外**（锁已释放）—— 见 `_safe_execute_pipeline`。
        try:
            await self._drain_inbox()
        except Exception as e:
            logger.error(f"[Inbox] 唤醒轮后排空队列失败: {e}")

    async def _suspension_poll_tick(self):
        """周期轮询 store：到点的定时挂起 → 驱动唤醒。基于 store 状态，
        已被用户唤醒/取消的记录不再 active，不会重复触发（无孤儿定时器）。"""
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            # 这里是副作用驱动查询，不是状态推进查询：DUE_FOR_REVIEW 也必须继续
            # 被捞到，否则一次唤醒尝试失败就会永久失联。
            due = _wc.list_due_wakeups(get_kernel())
        except Exception as e:
            logger.warning(f"[Suspension] 轮询失败: {e}")
            return
        for rec in due:
            sid = rec.wait_id
            if not sid:
                continue
            # pill 定型移到 _drive_wake 内部（拿到锁后才定型，内核忙时不会提前定型）
            await self._drive_wake(sid, trigger="timer")


    def _copy_visual(self, code: str) -> None:
        """把可视化的源码复制到剪贴板。

        🔴 [2026-08-23] 第一版自己写了一句裸 `navigator.clipboard.writeText`，
           并且**无论成败都弹「已复制」**。两个问题叠在一起最坏：
           WebView2 下 clipboard API 不一定可用（那份实现旁边的注释里
           早就写了这条，并配了 `execCommand` 兜底），
           于是可能**根本没复制，却告诉用户复制好了**。
           📌 **一个总是报成功的提示等于没有提示** —— 它把失败也伪装成了成功。
        ⭐ 改成复用那份 `copyText`（带兜底），且**提示写在它的回调里**：
           真复制成功才吐司。
        """
        try:
            import json as _j
            ui.run_javascript(
                "(function(){var t=" + _j.dumps(code) + ";"
                "if(!window.__nanoCopy){return;}"
                "window.__nanoCopy(t,function(){"
                "if(window.__nanoToast){window.__nanoToast('已复制代码');}"
                "});})()"
            )
        except Exception as e:
            logger.warning(f"[Visual] 复制失败: {e}")
            ui.notify("复制失败，可以手动选中代码", position="bottom", type="warning")

    async def _download_visual(self, code: str, title: str) -> None:
        """把可视化导出成文件 —— **用户自己选存到哪**。

        🔴 [2026-08-23 实测] 第一版用 `ui.download(...)` —— **点了没有任何反应**。
           它靠浏览器发起一次下载，而 Nano 跑在 **frameless WebView2** 里：
           没有下载栏、没有「另存为」，那次下载**无处落地**，也不报错。
           📌 **一个在浏览器里成立的做法，不一定在嵌入式 WebView 里成立** ——
              而它失败的方向是**静默**：按钮点了，什么都没发生。
        🔴 第二版改成我们自己写文件，但**写死落在「桌面/Nano导出」**。
           「这个不能改成选导出位置那种吗，类似于设置菜单导出数据那里」。
           📌 **替用户决定文件落在哪，是把「省一次点击」当成了体验** ——
              而导出这件事，用户心里本来就有一个目的地。
        ⭐ 复用「设置 → 通用 → 导出数据」那条 `_pick_folder()`：
           同一个原生对话框、同一套取消语义（取消不弹提示，取消不是错误），
           📌 一个已经存在的形状，第二次出现时该复用它。

        ⚠️ 后缀按**内容**判，不按标题猜 ——
           📌 一个名字叫 `.svg` 而内容是 HTML 的文件，打开时才发现，那时已经晚了。

        ⭐⭐ [2026-08-24] **SVG 的同时再导一份 `.html`。**
           🔴 双击导出的 `.svg`，浏览器给的是一棵 XML 树 ——
              「This XML file does not appear to have any style information」，
              **改个后缀成 .html 就正常了**。
           ⇒ 根因是 `xmlns`（见 `_svg_export_pair`）。两件事一起做：
             ① 导出时**补上 xmlns**，让 `.svg` 自己就能打开；
             ② **同时给一份 `.html`**。「能打开 svg 的电脑不多吧」。
           📌 ① 修的是正确性，② 修的是**可用性** —— 而 ② 不因为 ① 做了就不需要：
              「双击就能看」和「它合规、但要指望系统关联对了」，对用户是两件事。
        """
        _picked = await self._pick_folder()
        if not _picked:
            return          # 用户取消 —— 不弹任何提示（同 _do_export_data）
        try:
            import datetime as _dt
            _name = (title or "nano_visual").strip() or "nano_visual"
            for _c in '\/:*?"<>|':
                _name = _name.replace(_c, "_")
            # ⚠️ 仍然带时间戳 —— 📌 导出两张同名的图不该悄悄覆盖前一张。
            _stamp = _dt.datetime.now().strftime("%m%d_%H%M%S")
            _stem = pathlib.Path(_picked) / f"{_name}_{_stamp}"
            _written = []
            for _ext, _text in self._svg_export_pair(code, title):
                _f = _stem.with_suffix("." + _ext)
                _f.write_text(_text, encoding="utf-8")
                _written.append(_f)
            logger.info(f"[Visual] 已导出 {[str(f) for f in _written]}")
            # ⚠️ 报**全路径**：用户刚亲手选了目录，但吐司只写文件名的话，
            #    📌 用户还是得自己回想「刚才选的是哪个来着」。
            _kinds = " + ".join(f.suffix.lstrip(".") for f in _written)
            ui.notify(f"已导出（{_kinds}） → {_written[0].parent}",
                      type="positive", icon="download_done")
        except Exception as e:
            logger.warning(f"[Visual] 保存失败: {e}")
            ui.notify(f"保存失败：{e}", position="bottom", type="warning")

    @staticmethod
    def _svg_export_pair(code: str, title: str) -> list:
        """把一份可视化源码摊成要落盘的文件：`[(后缀, 内容), ...]`。

        ═══ 为什么 用户那份 .svg 打不开 ═══
        🔴 模型产出的根标签是 `<svg width="100%" viewBox="0 0 600 400">` ——
           **没有 `xmlns`**。
           · 嵌在 HTML 里时：HTML 解析器**自动补上** SVG 命名空间 → 正常渲染，
             所以它在聊天里一直好好的；
           · 单独存成 `.svg` 时：那是一份 **XML 文档**，没有命名空间就只是
             一棵普通 XML 树 → 浏览器给出「没有样式信息」那句话。
        📌 **同一段字节，在两个解析器里是两个东西** ——
           而它在我们眼皮底下（聊天里）恰好是好的那个，所以一直没露。
        🔴 而 `xmlns` 是**规格自己漏了**：`render_visual` 描述里那行示例写的
           就是 `<svg width="100%" viewBox="0 0 680 H">`，模型一字不差照做了。
           📌 **模型照着做了，错的是给它的样板。**

        ⭐ 两手都补：规格里加（orchestrator 那侧），这里也加 ——
           📌 提示词里的约束是**建议**，落盘前这一道才是**保证**。
        """
        import re as _re
        _out = []
        _is_svg = code.lstrip().lower().startswith("<svg")

        if _is_svg:
            _svg = code
            # ⚠️ 只在**确实没有**时才插，别把已经写对的那份改坏。
            if not _re.search(r"<svg[^>]*\sxmlns\s*=", _svg, _re.I):
                _svg = _re.sub(
                    r"<svg\b",
                    '<svg xmlns="http://www.w3.org/2000/svg"'
                    ' xmlns:xlink="http://www.w3.org/1999/xlink"',
                    _svg, count=1, flags=_re.I)
            _out.append(("svg", _svg))

        # ⚠️ HTML 那份要**自带深色底**：图是照着深色聊天设计的（浅色字），
        #    落在浏览器默认白底上会看不见。
        #    📌 一个换个背景就读不了的导出，等于没导出。
        _body = _out[0][1] if _is_svg else code
        _t = (title or "Nano 可视化").replace("<", "&lt;").replace(">", "&gt;")
        _out.append(("html",
                     "<!doctype html>\n"
                     '<html lang="zh-CN"><head><meta charset="utf-8">\n'
                     f"<title>{_t}</title>\n"
                     "<style>\n"
                     "  html,body{margin:0;padding:0;background:var(--nano-bg);}\n"
                     "  body{color:var(--nano-fg);font-family:ui-sans-serif,system-ui,"
                     "'Segoe UI','Microsoft YaHei',sans-serif;"
                     "display:flex;flex-direction:column;align-items:center;"
                     "gap:18px;padding:32px 20px;box-sizing:border-box;}\n"
                     "  h1{font-size:var(--nano-fs-lg);font-weight:500;color:var(--nano-dim);margin:0;}\n"
                     "  #wrap{width:100%;max-width:900px;}\n"
                     "  #wrap svg{width:100%;height:auto;}\n"
                     "</style></head><body>\n"
                     + (f"<h1>{_t}</h1>\n" if title else "")
                     + '<div id="wrap">\n' + _body + "\n</div>\n"
                     "</body></html>\n"))
        return _out

    def _render_visual_artifact(self, html_code: str, title: str, token: str, parent=None):
        """widget：把模型生成的 HTML/SVG 渲染进聊天流。

        ⚠️ `parent` 是重放通道加的：live 侧靠 `self._resp_state["loading_col"]`
        找当前回应体，而**重放时那个 state 要么不存在、要么是上一次运行的残留**
        —— 让重放显式传目标容器，比让它去猜一个 live 专用的隐式状态可靠。

        安全：用 <iframe sandbox="allow-scripts"> 隔离——模型代码在独立的 null
        origin 沙箱里跑，拿不到 Nano 自己的 DOM/cookie/storage，防 XSS 污染主界面。
        自适应高度：往 srcdoc 末尾注入一段上报脚本，iframe 加载后把内容高度
        postMessage 给父页面，父页面的全局监听器（render() 里注入）据 token 设高。
        不依赖任何外部 CDN，也兼容 native 壳（WebView2 同样支持 iframe/sandbox）。
        """
        import html as _htmllib
        if not html_code:
            return
        # 自适应高度：测量 height:auto 的内容包裹层 __nanowrap，而不是 documentElement。
        # documentElement 在 WebView2 里会拉伸填满 iframe（= 我们刚设的高度），测它会
        # 造成 "测高→设高→再测到更大→再设" 的 +4 死循环（Chrome 的 html 高度是 auto
        # 不复现，WebView2 复现）。包裹层高度只反映真实内容，跨引擎都稳，循环被斩断。
        _reporter = (
            "<script>(function(){"
            "function h(){var w=document.getElementById('__nanowrap');"
            "return w?Math.ceil(w.getBoundingClientRect().height):"
            "Math.ceil(document.documentElement.scrollHeight);}"
            "function r(){try{parent.postMessage("
            "{__nanoVisualHeight:true,token:'" + token + "',height:h()},'*');"
            "}catch(e){}}window.addEventListener('load',r);setTimeout(r,250);setTimeout(r,800);"
            "var w=document.getElementById('__nanowrap');"
            "if(window.ResizeObserver&&w){try{new ResizeObserver(r).observe(w);}catch(e){}}"
            "})();</script>"
        )
        # ⭐⭐ [2026-08-23] **兜底 CSS：模型不守规格时也别毁掉版式。**
        #
        # 规格已经写进 `render_visual` 的描述（宽度 100% / 不许固定尺寸 /
        # 不许 overflow），但 📌 **一条只写在提示词里的约束，等于一条建议** ——
        # 实测就是模型给了固定 `width` 的 SVG，于是画成一小块 + 出滚动条。
        # ⭐ 所以这里再兜一层，三条各解决一个已经见过的症状：
        #   · `svg{width:100%!important;height:auto!important}` —— 解决「画成一小块」
        #   · `#__nanowrap{overflow:visible}` + 关掉 body 滚动 —— 解决「出滚动条」
        #     （高度本来就由 ResizeObserver 上报撑开，**滚动条只会挡住真实高度**）
        #   · 透明底 + 浅色字 —— 解决「深色聊天里一块白斑」
        # ⚠️ 用 `!important`：这是**兜底**，就是要压过模型写的内联样式。
        #    📌 一个兜底如果能被它要兜的那个东西覆盖，它就不是兜底。
        # 🔴🔴 [2026-08-23 第二次实测] 第一版这里写的是 `#__nanowrap>svg{width:100%}`
        #    —— **只匹配直接子元素**。而模型把 SVG 包在了一个 `<div>` 里，
        #    于是它是**孙子**，那条规则一次都没命中；只有
        #    `#__nanowrap svg{max-width:100%}` 生效，而 `max-width` 对一张
        #    `width="400"` 的图毫无作用 → 图照样只有 400px 宽。
        # 📌 **`>` 和空格是两个完全不同的选择器，而写错的表现是「样式看起来没写」** ——
        #    不报错、不警告，只是安静地不生效。
        # ⚠️ 而那个白底也来自同一个包裹 `<div>`：压了 `body` 的背景，
        #    却没压模型自己那层。
        _fallback_css = (
            "<style>"
            "html,body{margin:0;padding:0;background:transparent!important;overflow:hidden;}"
            "body{color:var(--nano-fg);font-family:ui-sans-serif,system-ui,'Segoe UI',sans-serif;}"
            "#__nanowrap{overflow:visible;width:100%;}"
            # ⭐ 后代选择器（不是 `>`）+ `!important`：不管模型包了几层都撑满。
            "#__nanowrap svg{width:100%!important;height:auto!important;max-width:100%;}"
            # ⭐ 只压**外层包裹**的背景，不碰图形内部 ——
            #    📌 一刀切压掉所有背景会把节点填色也抹掉，那是把兜底做成了破坏。
            "#__nanowrap>div,#__nanowrap>section,#__nanowrap>main,#__nanowrap>figure"
            "{background:transparent!important;width:100%!important;max-width:100%!important;"
            "margin:0!important;padding:0!important;}"
            "#__nanowrap *{max-width:100%;}"
            "</style>"
        )
        _wrapped = (_fallback_css
                    + '<div id="__nanowrap" style="display:block;">' + html_code + '</div>')
        _srcdoc = _htmllib.escape(_wrapped + _reporter, quote=True)
        _iframe = (
            f'<iframe data-nano-token="{token}" sandbox="allow-scripts" '
            f'srcdoc="{_srcdoc}" loading="lazy" '
            'style="width:100%; height:160px; border:0.5px solid rgba(var(--nano-shade-rgb), 0.12); '
            # ⚠️ 底色改透明：iframe 窄的时候这块写死的底会露成一条色带，
            #    而它本来就该融进气泡。📌 一个容器的底色，应该由它所在的地方决定。
            'border-radius:12px; background:transparent; transition:height 0.15s ease; '
            'margin-top:8px; display:block;"></iframe>'
        )
        target = parent
        if target is None:
            target = self._resp_state.get("loading_col") if hasattr(self, "_resp_state") else None
        if target is None:
            target = getattr(self, "chat_container", None)
        if target is None:
            return
        with target:
            # ⭐ [2026-08-23] 标题行 + 右上角 `⋯`：把「拿走这张图」的出口给用户。
            #    📌 一个画在聊天里的东西，如果用户只能截屏才能带走它，
            #       那它就只是一张贴图，不是产物。
            # ⚠️ 标题行必须和 iframe **在同一个容器里**，否则 `⋯` 会按聊天区宽度
            #    右对齐，而图按自己的宽度 —— 两者右边界对不齐（实测就是这样）。
            #    📌 两个要对齐的东西，必须由**同一个盒子**决定它们的边界。
            _vis_box = ui.column().classes("w-full").style("gap:0; min-width:0;")
        with _vis_box:
            with ui.row().classes("items-center w-full no-wrap").style(
                    "margin-top:8px; gap:6px;"):
                ui.label(title or "").style(
                    "font-size:var(--nano-fs-base); color:var(--nano-dim); font-weight:500; "
                    "font-family:var(--nano-mono); flex:1; min-width:0;")
                # ⚠️ 平时半透明 —— 📌 同聊天区搜索按钮那条：
                #    一个「平时看不见」的按钮等于没有入口，半透明保留发现性、不抢戏。
                _more = ui.button(icon="more_horiz").props("flat dense round").style(
                    "color:var(--nano-dim); opacity:0.45; transition:opacity .2s;")
                _more.on("mouseenter", lambda _e, b=_more: b.style("opacity:1;"))
                _more.on("mouseleave", lambda _e, b=_more: b.style("opacity:0.45;"))
                with _more:
                    # 🔴 [2026-08-23 实测] 第一版用 `ui.menu.style(background=...)`
                    #    —— **底色没生效，菜单是透明的**。
                    #    Quasar 的 `q-menu` 把内容渲染在 **portal**（body 层）里，
                    #    `.style()` 落在外壳上，**到不了那张浮起来的卡**。
                    # ⭐ 项目里已经有两个能用的菜单（`skill_menu` / `file_menu`），
                    #    它们的写法都是 `.classes('q-pa-none')` + 里面套一个
                    #    `ui.column()` **自己画底** —— 📌 一个已经存在的形状，
                    #    第二次出现时该复用它，而不是重新试一遍。
                    with ui.menu().props('anchor="bottom right" self="top right"') \
                            .classes('q-pa-none') as _vis_menu:
                        with ui.column().style(
                                'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                                'box-shadow:0 4px 16px rgba(var(--nano-shade-rgb), 0.08); '
                                'border-radius:10px; min-width:150px; padding:5px; gap:1px;'):
                            # ⚠️ `fn` 可能是协程（导出要 await 原生选框）——
                            #    📌 一个不 await 的协程调用**不报错也不执行**，
                            #       表现就是「点了没反应」，正是我们刚修完的那个问题。
                            def _vis_item(icon, label, fn):
                                async def _go():
                                    _vis_menu.close()
                                    _r = fn()
                                    if inspect.isawaitable(_r):
                                        await _r
                                with ui.row().classes(
                                        'items-center gap-1.5 w-full cursor-pointer '
                                        'hover:bg-white/5 transition-colors') \
                                        .style('padding:5.5px 8px; border-radius:7px;') \
                                        .on('click', _go):
                                    ui.icon(icon).style(
                                        'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); flex-shrink:0;')
                                    ui.label(label).style('font-size:var(--nano-fs-xs); color:var(--nano-fg);')

                            _vis_item('content_copy', '复制代码',
                                      lambda c=html_code: self._copy_visual(c))
                            # ⚠️ 就叫「导出」，跟设置里那个按钮一个词 —— 用户当时定过：
                            #    按钮上写「选择文件夹」是在描述**过程**，不是结果。
                            #    ⚠️ 也不叫「下载 SVG」：内容可能是 HTML，
                            #       📌 一个名字承诺了 `.svg`，而它有时给的是别的，那就是在骗人。
                            _vis_item('download', '导出',
                                      lambda c=html_code, t=title:
                                          self._download_visual(c, t))
            # 🔴🔴 [2026-08-23 第三次实测 · 这次是看代码看出来的] **必须 `w-full`。**
            #
            # `ui.html()` 生成的是一个普通 `<div>`，在 flex 列里它**收缩到内容宽度**。
            # 于是 iframe 那句 `width:100%` 算的是「**那个收缩后的 div** 的 100%」——
            # 📌 **`width:100%` 从来不是「撑满」，是「撑满我爹」**，而它爹自己就是窄的。
            #
            # ⚠️ 这解释了**从一开始**它就窄：跟模型的产出无关（实测那份 SVG
            #    `width="100%" viewBox="0 0 680 400"` 完全合规、也没有包裹 div），
            #    也跟加的那两轮兜底 CSS 无关。
            # 🔴 对着症状连推了两个成因（「模型包在 div 里」「`>` 选择器写错」），
            #    **两个都是编的**。
            # 📌 教训很具体：**症状在 A，不代表原因在 A 附近。**
            #    这次是倒着问出来的 —— 「这个 100% 的基准是谁定的」，
            #    而不是继续盯着 SVG 本身看。
            ui.html(_iframe).classes("w-full").style("min-width:0;")

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def render(self):
        ui.query('body').style(
            'background:var(--nano-bg); overflow:hidden; height:100vh; margin:0;'
        )
        # 找到真正的"终端字符"bug根因：FAVICON_SVG 是带双引号属性的原始
        # SVG（xmlns="..." viewBox="..." 等），之前只手动转义了 # 号，
        # 双引号原样拼进了 href="..." 这个属性值里——浏览器解析到 SVG
        # 内部第一个 " 就提前判定 href 属性结束，href="data:image/svg+xml,
        # <svg xmlns=" 后面那一大截（包括 viewBox="0 0 32 32" 这种带 "
        # 的内容）全部被当成 <link> 标签上一堆乱七八糟的属性/裸文本去解析，
        # 浏览器进入错误恢复模式后，把这些解析失败的文本片段当成页面
        # 内容渲染了出来——这正是之前一直没查清楚的"终端一样的字符"。
        # 改用 urllib.parse.quote() 做完整的 URL 编码（双引号也编码成
        # %22），不再手动挑着转义个别字符。
        _favicon_data_uri = urllib.parse.quote(FAVICON_SVG)
        # widget：可视化 iframe 自适应高度——监听沙箱内 postMessage 上报的高度，
        # 据 data-nano-token 给对应 iframe 设高。沙箱是 null origin，event.origin
        # 是 'null'，靠 token 匹配而不是 origin。
        ui.add_head_html('''<script>
        window.addEventListener('message', function(e){
          try{
            var d = e.data;
            if(d && d.__nanoVisualHeight && d.token){
              var f = document.querySelector('iframe[data-nano-token="' + d.token + '"]');
              if(f){
                var nh = Math.max(40, d.height) + 4;
                // 防抖兜底：仅当目标高与当前高差 >2px 才设，杜绝 ±1px 抖动引发的回环增高
                var cur = parseFloat(f.style.height) || 0;
                if(Math.abs(cur - nh) > 2){ f.style.height = nh + 'px'; }
              }
            }
          }catch(err){}
        });
        </script>''')
        # 工具 pill"扫光"动效：工具执行中，pill 顶行文字有一道高光从左向右
        # 循环流过（background-clip:text + 动 background-position），看得出"正在行动"。
        # 批次结束移除 nano-tool-active 类 → 回到静态色。
        # ⭐ 聊天区搜索按钮：平时半透明（不在聊天 UI 里扎眼），指针放上去恢复。
        #    ⚠️ 只动 opacity 不动显隐 —— 📌 一个"平时看不见、需要时才出现"的按钮
        #       等于没有入口；半透明保留了发现性，只是不抢戏。
        ui.add_head_html('''<style>
        .nano-chat-search-btn {
          opacity: 0.26;
          transition: opacity 0.18s ease;
        }
        .nano-chat-search-btn:hover { opacity: 1; }
        /* 搜索结果里的命中片段高亮 */
        .nano-hit { color:var(--nano-amber); font-weight:600; }
        /* 跳转后目标气泡短暂发光 —— 让"跳过去了"这件事看得见 */
        @keyframes nano-jump-flash {
          0%   { background: rgba(var(--nano-amber-rgb), 0.18); }
          100% { background: transparent; }
        }
        .nano-jump-target { animation: nano-jump-flash 1.6s ease-out 1; border-radius:8px; }
        </style>''')
        ui.add_head_html('''<style>
        @keyframes nano-tool-sweep {
          0%   { background-position: 100% center; }
          100% { background-position: -100% center; }
        }
        .nano-tool-active {
          display: inline-block !important;
          background: linear-gradient(90deg,var(--nano-amber) 0%,var(--nano-amber) 35%,#ffe89a 50%,var(--nano-amber) 65%,var(--nano-amber) 100%) !important;
          background-size: 200% auto !important;
          -webkit-background-clip: text !important;
          background-clip: text !important;
          -webkit-text-fill-color: transparent !important;
          color: transparent !important;
          animation: nano-tool-sweep 1.3s linear infinite !important;
        }
        </style>''')
        # 执行时自缩窗：mini 形态隐藏 header（页容器顶距清零），浮控贴到窗口顶部。
        ui.add_head_html('''<style>
        .nano-mini .q-header { display:none !important; }
        /* 关键：Quasar 左抽屉是用 q-page-container 的 padding-left 撑开的（不是 margin！），
           缩窗时必须把 padding 全清零，否则内容被那 256px 左 padding 推出窗口右侧。 */
        .nano-mini .q-page-container { padding:0 !important; margin:0 !important; }
        .nano-mini .q-drawer--left, .nano-mini .q-drawer--right { display:none !important; }
        /* header 隐藏后内容区高度补回那 60px，不留底部空隙 */
        .nano-mini .main-chat-col { height:100vh !important; padding-left:14px !important; padding-right:14px !important; }
        /* mini 小胶囊（头像+计时器）贴窗口右上角 */
        .nano-mini .f8-mini-bar { top:10px !important; right:10px !important; }
        /* ── native 窗口横向溢出护栏（所有模式）──────────────────────────
           左抽屉给 q-page-container 加了 padding-left:256，但窗口模式下容器宽度
           算成整窗宽（content-box），内容总宽=窗宽+256 → 右侧飞出一个抽屉宽。
           最大化时窗够宽看不出，窗口模式就露馅（输入框/选择卡片飞出右边）。
           强制 border-box + 限宽到视口 + 兜底裁剪横向溢出。 */
        .q-layout, .q-page-container, .q-page {
            box-sizing:border-box !important;
            max-width:100vw !important;
            overflow-x:hidden !important;
        }
        </style>''')
        ui.add_head_html(f'''
        <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{_favicon_data_uri}">
        <style>

            /* 终端风字体：JetBrains Mono（OFL，打包在 assets/fonts，离线、零安装）。
               覆盖 Latin/数字/符号；中文走 fallback（系统 YaHei），属正常终端混排。 */
            @font-face {{
                font-family: 'JetBrains Mono';
                src: url('/fonts/JetBrainsMono-400.woff2') format('woff2');
                font-weight: 400; font-display: swap;
            }}
            @font-face {{
                font-family: 'JetBrains Mono';
                src: url('/fonts/JetBrainsMono-500.woff2') format('woff2');
                font-weight: 500; font-display: swap;
            }}
            /* Inter（OFL 1.1，打包在 assets/fonts，离线、零安装）——
               浅色主题的正文字体。只取 latin 子集（72KB）：中文本来就走 fallback。 */
            @font-face {{
                font-family: 'Inter';
                src: url('/fonts/Inter-400.woff2') format('woff2');
                font-weight: 400; font-display: swap;
            }}
            @font-face {{
                font-family: 'Inter';
                src: url('/fonts/Inter-500.woff2') format('woff2');
                font-weight: 500; font-display: swap;
            }}
            @font-face {{
                font-family: 'Inter';
                src: url('/fonts/Inter-600.woff2') format('woff2');
                font-weight: 600; font-display: swap;
            }}
            /* 终端风等宽字体栈（中文 fallback）。主皮用 时启用。 */
            /* 中文等宽很关键：英文走 JetBrains Mono，中文按顺序找【等宽】CJK——
               Sarasa Mono SC（若装/打包）→ NSimSun 新宋体（Windows 自带、真等宽、retro 终端味）。
               故意不放 YaHei（它不是等宽，会破坏终端对齐）。 */
            :root {{ --nano-mono: 'JetBrains Mono', Consolas, 'Sarasa Mono SC', 'NSimSun', monospace; }}
            /* 浅色主题的比例字体。⚠️ 中文 fallback 到 YaHei —— 终端风【故意不用】它
               （不等宽、破坏对齐），而这里正因为不需要等宽，它才是最合适的那个。 */
            :root {{ --nano-sans: 'Inter', 'Microsoft YaHei', system-ui, -apple-system, sans-serif; }}
            /* ── 字号阶梯 ──────────────────────────────────────────────
               2026-08-30 从 462 处硬编码收上来。**收的时候一个都没放大**：
               整档原样，只有 4 个半档（9.5/10.5/11.5/12.5）向下并 —— 向上并
               就是放大，会让「收口的锅」和「放大的锅」分不清（这个顺序是定死的）。
               📌 名字按【档位序】不按 px 值：叫 --nano-fs-11 的变量一旦被调成
                  13px，名字就开始骗人（同 --nano-hi-rgb 装深色值那个缺陷）。 */
            :root {{
                --nano-fs-3xs:8px;
                --nano-fs-2xs:9px;
                --nano-fs-xs:11px;
                --nano-fs-sm:12px;
                --nano-fs-base:12px;
                --nano-fs-md:13px;
                --nano-fs-lg:14px;
                --nano-fs-xl:15px;
                --nano-fs-2xl:16px;
                --nano-fs-3xl:17px;
                --nano-fs-4xl:18px;
                --nano-fs-5xl:20px;
            }}
            /* Tailwind 的 text-[Npx] 有 42 处写在 .classes() 里，混着别的类；
               搬进 .style() 属于结构改动 ⇒ 用规则把类名接进阶梯，零结构改动。 */
            .text-\[11px\] {{ font-size: var(--nano-fs-sm) !important; }}
            .text-\[12px\] {{ font-size: var(--nano-fs-base) !important; }}
            .text-\[13px\] {{ font-size: var(--nano-fs-md) !important; }}
            .text-\[14px\] {{ font-size: var(--nano-fs-lg) !important; }}
            .text-\[16px\] {{ font-size: var(--nano-fs-2xl) !important; }}
            .text-\[20px\] {{ font-size: var(--nano-fs-5xl) !important; }}
            /* ⚠️ 主题【无关】的变量必须放这里，不能塞进某一套主题块 ——
               CSS 变量跟着选择器走，写在 body.nano-theme-terminal 上的，
               换成 nano-theme-light 之后就不存在了（头像没色 / 终止键消失都是这么来的）。 */
            :root {{
                /* 品牌：是「Nano 这个东西」的一部分，任何主题下同值 */
                --nano-brand-core:#9b6406;
                --nano-brand-glow:#b28103;
                /* 实心按钮底色：深底配白字，两种主题下都成立，所以也不随主题变 */
                --nano-danger-fill:#dc2626;
                --nano-warn-fill:#7c4a12;
                --nano-ok-fill:#0f766e;
                /* 终止键单独一档：它是【常驻】控件（一轮跑着就一直挂着），
                   不同于弹窗里按一下就走的确认键，用砖红而非高饱和红。白字 4.48。 */
                --nano-stop-fill:#b06060;
            }}

            body {{ font-family: var(--nano-sans); }}

            /* ── 稳定背景系统：背景图不挂 body.background，改用 ::before 伪层 ── */
            /* 目的：不同电脑/显示器/GPU合成差异下表现一致，不再依赖 JS 时序 */
            html, body, #q-app {{ width:100%; height:100%; min-height:100%; margin:0; }}
            /* 默认就是终端炭黑：终端类生效前也不会闪出旧的天空背景，初始/加载页也是终端感 */
            body {{ position:relative; overflow:hidden !important; background:var(--nano-bg) !important; color-scheme:dark; }}

            body::before {{
                content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
                background:var(--nano-bg);
            }}
            body::after {{
                content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
                background: transparent;
            }}

            /* ═══════════ 浅色主题（暖白 + 黑，语义色保色相） ═══════════ */
            body.nano-theme-light {{
                background:#faf9f7 !important; color:#241f1a !important; color-scheme:light;
                font-family: var(--nano-sans) !important;

                /* ── 表面：浅色下【两个方向】—— 框架凹陷、输入框抬升 ── */
                --nano-bg:#faf9f7;        /* 暖白，不是纯白（纯白亮度 1.000 → 这里 0.948） */
                --nano-chrome:#f2f0ec;    /* 头栏 + 抽屉：比底色【灰】= 凹陷 */
                --nano-input:#ffffff;     /* 输入框：比底色【白】= 抬升 */
                --nano-panel:#fdfcfa;     /* 卡片 / 弹窗。⚠️ 不用纯白：#ffffff 亮度 1.000
                   贴在 0.948 的暖白底上会"发光"，而且跟整套暖调不搭（实际观感很差）。
                   #fdfcfa 亮度 ~0.975，仍比底色亮、层次还在，但不刺眼。 */
                --nano-panel-2:#f6f4f0;   /* 次级面板 / 下拉：浅色下靠【更灰】区分（白之上没空间了） */
                --nano-border:#e6e2da;
                --nano-line:#d6d0c4;
                --nano-panel-rgb:255,255,255;  --nano-panel-2-rgb:246,244,240;
                --nano-line-rgb:214,208,196;   --nano-dim-rgb:125,117,101;

                /* ── 文字五档：对比度阶梯与深色主题对齐 ── */
                --nano-fg:#241f1a;        /* 15.52 */
                --nano-fg-soft:#57503f;   /*  7.60 */
                --nano-fg-mute:#6b6a6e;   /*  5.10 */
                --nano-dim:#7d7565;       /*  4.33 */
                --nano-faint:#a8a094;     /*  2.46 */
                --nano-fg-rgb:36,31,26;

                /* ── 品牌琥珀：浅色下【压深】，不是变黑 ──
                   ⚠️ 曾按「白主题走黑白」写成近黑 #241f1a，实测上：
                      ① 跟正文同色 ⇒ 导航选中态没有任何反馈
                      ② 反馈：「目前看起来太丑了」
                   ⇒ 改回琥珀。取值 = 头像的 brand-core，那是唯一一个
                      已验证「深底 3.90 / 浅底 4.73」两边都可见的琥珀。 */
                --nano-amber:#9b6406;
                --nano-amber-rgb:155,100,6;
                /* 控件强调色走蓝（实测：滑块/开关本来就是这个蓝，保留） */
                --nano-accent:#1976d2;   --nano-accent-rgb:25,118,210;
                /* 发送键：与终端风同构（实心琥珀方块），实测认可后保留。
                   ⚠️ 曾试过「去框只留图标」的极简版，看过之后取消了。 */
                --nano-send-bg:var(--nano-amber);
                --nano-send-fg:var(--nano-bg);
                --nano-btn-radius:2px;
                /* ── 代码浏览器语法色（三处共用：临时代码 / Skill 创建 / 查看源码）── */
                --nano-cm-comment:#7d8c76;  --nano-cm-keyword:#e0855f;
                --nano-cm-string:#87d29a;   --nano-cm-number:#d9a55f;
                --nano-cm-func:#8ab4d8;     --nano-cm-def:#9ecbe8;
                --nano-cm-type:#d7c07a;     --nano-cm-punct:#9a9384;
                --nano-cm-self:#c98fb0;     --nano-cm-invalid:#f87171;
                --nano-cm-caret:#4f46e5;

                /* ── 语义色：色相不变，明度换边 ── */
                --nano-ok:#477923;      --nano-ok-rgb:71,121,35;       /* 5.21，与深色同色相 98° */
                --nano-danger:#dc2626;  --nano-danger-rgb:220,38,38;   /* 4.59，色相差 10° */
                --nano-warn:#7c4a12;    --nano-warn-rgb:124,74,18;     /* 7.01，色相差  6° */
                --nano-info:#0369a1;    --nano-info-rgb:3,105,161;     /* 5.64，色相差  3° */

                /* ⚠️ --nano-*-fill 不在这里覆盖：实心按钮是【深底配白字】，两种主题下都成立 */
                /* ⚠️ --nano-brand-* 不在这里覆盖：品牌色一个固定值，两种主题下都可见 */

                /* ── 半透明基色 ── */
                --nano-shade-rgb:15,13,11;      /* 压暗：两种主题都朝暗走，不翻 */
                --nano-ink-rgb:0,0,0;
                --nano-contrast-rgb:36,31,26;   /* 🔴 翻了：浅底上「白色 6%」等于没有 */
            }}
            body.nano-theme-light::before {{ background:var(--nano-bg) !important; filter:none !important; }}
            body.nano-theme-light::after {{ background:none !important; }}
            body.nano-theme-light .q-header {{ background:var(--nano-chrome) !important; color:var(--nano-fg) !important; }}
            body.nano-theme-light .q-drawer {{ background:var(--nano-chrome) !important; color:var(--nano-fg) !important; }}
            /* 浅色主题：正文走比例字体；代码/终端提示符那些仍走 --nano-mono，
               因为它们【显式】写了 var(--nano-mono)，不受这条继承影响。 */
            body.nano-theme-light *:not(i):not(.material-icons):not(.q-icon):not(.ti):not([class*="cm-"]) {{
                font-family: var(--nano-sans);
            }}
            body.nano-theme-light .composer-shell {{ background:var(--nano-input) !important; border:1px solid var(--nano-line) !important; }}

            /* ═══════════ 终端风（默认主题，琥珀 on 炭黑 + CRT） ═══════════ */
            body.nano-theme-terminal {{
                background:#0f0d0b !important; color:#cdc9bd !important; color-scheme:dark;
                font-family: var(--nano-mono) !important;
                --nano-bg:#0f0d0b; --nano-panel:#1a1714; --nano-border:#2b2621;
                --nano-fg:#cdc9bd; --nano-dim:#857c6e; --nano-faint:#5c554a;
                --nano-amber:#e6a94e;
                /* 控件强调色：开关/滑块/选中态/发送键。深色下与品牌琥珀同值，
                   浅色下分开（浅色主题这四个走蓝） */
                --nano-accent:#e6a94e;   --nano-accent-rgb:230,169,78;
                --nano-send-bg:var(--nano-amber);   /* 终端风：琥珀实心方块 */
                --nano-send-fg:var(--nano-bg);
                --nano-btn-radius:2px;
                /* ── 代码浏览器语法色 ──
                   色相保留、**饱和度提高**、明度换边。
                   ⚠️ 第一版只压明度（低饱和土色系→白底=浑浊），实测可读性差。
                   ⚠️ 强弱层次与深色那套一一对应（注释最弱 4.6 … 定义最强 9.0），
                      不能全部齐平 —— 函数名(208°)和定义(204°)本来就靠明度区分。 */
                --nano-cm-comment:#597f48;  --nano-cm-punct:#786a4d;
                --nano-cm-invalid:#ca1414;  --nano-cm-keyword:#ab4418;
                --nano-cm-self:#b12976;     --nano-cm-number:#7c4d0e;
                --nano-cm-func:#1b5a90;     --nano-cm-type:#5c490f;
                --nano-cm-string:#0f5721;   --nano-cm-def:#0d4d76;
                --nano-cm-caret:#4f46e5;
                --nano-fg-soft:#a89f92;   /* 次要文字（暖灰，跟终端调子一致） */
                --nano-fg-mute:#928F91;   /* 中性灰文字（OFFLINE / 不活动这类状态） */
                --nano-panel-2:#211d18;   /* 面板第二层（比 --nano-panel 略亮） */
                --nano-line:#3d382f;
                /* 表面分三个角色。深色主题下三者同值，浅色主题下【方向不同】：
                   框架要凹陷（更灰）、输入框要抬升（更白）、卡片居中。 */
                --nano-chrome:#171512;   /* 窗口框架：头栏 + 抽屉。
                   ⚠️ 比 --nano-panel(#1a1714) 略暗，是【有意】的：抽屉跟主底色
                      #0f0d0b 大面积相邻，台阶从 (8,8,7) 变成 (11,10,9) 就会显突兀。
                      📌 ΔE 量的是颜色变了多少，不是它跟邻居的落差变了多少。 */
                --nano-input:#1a1714;    /* 输入框外壳 */
                --nano-panel-rgb:26,23,20;      /* 半透明面板用，值同 --nano-panel */
                --nano-panel-2-rgb:33,29,24;    /* 同 --nano-panel-2 */
                --nano-dim-rgb:133,124,110;     /* 同 --nano-dim */
                --nano-line-rgb:61,56,47;       /* 同 --nano-line */
                /* ── 语义色：颜色本身带含义，【不随主题翻转】 ── */
                /* 正向：完成/在线/健康。⚠️ 值取自 `终端风UI待办.md` 的调色板
                   「绿 #84b06a」—— 曾按「取多数」并成 #34d399(Tailwind emerald)，
                   但那 27 次是**没迁完的默认色**，1 次的 #84b06a 才是设计值。
                   📌 多数派也可能是「还没改过来的那批」。 */
                --nano-ok:#84b06a;      --nano-ok-rgb:132,176,106;
                --nano-danger:#f43f5e;  --nano-danger-rgb:244,63,94;   /* 错误/危险 */
                --nano-warn:#f59e0b;    --nano-warn-rgb:245,158,11;    /* 警告 */
                --nano-info:#38bdf8;    --nano-info-rgb:56,189,248;    /* 提示/notice */
                /* 实心按钮底色：深色，配【浅色】文字。与上面的语义色是两个角色 ——
                   前景色要在深底上亮，填充底色要让浅字读得出，方向相反，不能共用。 */
                /* 闲置/OFFLINE 沿用 --nano-fg-mute，不另起变量 */
                /* ── 品牌色：是「Nano 这个东西」的一部分，不是界面的一部分 ── */
                /* ⚠️ favicon 的底色【不】建变量：它走 data: URI，var() 在里面不解析 */
                --nano-shade-rgb:15,13,11;   /* 压暗用（阴影/深描边）。原来这里混着 Tailwind slate-900(15,23,42) 的冷蓝黑，2026-08-29 统一到暖色 */
                --nano-ink-rgb:0,0,0;   /* 纯黑阴影（中性，不随主题色温走） */
                --nano-contrast-rgb:255,255,255;   /* 与当前表面【相反】的一侧：深底上是白，浅底上要翻成黑。
                                      15/26 处是低透明度边框 —— 浅底上白色 6% 等于没有 */
                --nano-fg-rgb:205,201,189;   /* 前景色的半透明用法，值同 --nano-fg */
                --nano-amber-rgb:230,169,78;   /* 品牌琥珀的半透明用法，值同 --nano-amber */   /* 较亮的分隔线 / 边框 */
            }}
            body.nano-theme-terminal::before {{ background:var(--nano-bg) !important; filter:none !important; }}
            body.nano-theme-terminal::after {{
                background: transparent !important;
                pointer-events:none;
            }}
            body.nano-theme-terminal *:not(i):not(.material-icons):not(.q-icon):not(.ti) {{
                font-family: var(--nano-mono) !important; letter-spacing:0 !important;
            }}
            .q-header {{
                background:var(--nano-chrome) !important; color:var(--nano-fg) !important;
                border-bottom:1px solid var(--nano-border) !important; backdrop-filter:none !important; box-shadow:none !important;
            }}
            .q-drawer {{
                background:var(--nano-chrome) !important; backdrop-filter:none !important; color:var(--nano-fg) !important;
            }}
            .q-drawer--left {{ border-right:1px solid var(--nano-border) !important; box-shadow:none !important; }}
            .q-drawer--right {{ border-left:1px solid var(--nano-border) !important; box-shadow:none !important; }}
            .theme-card {{
                background:var(--nano-panel) !important; border:1px solid var(--nano-border) !important; color:var(--nano-fg) !important; border-radius:6px !important;
            }}
            .theme-text {{ color:var(--nano-fg) !important; }}
            .theme-user-card {{ background:var(--nano-panel-2) !important; border:1px solid var(--nano-border) !important; }}
            .theme-user-text {{ color:var(--nano-fg) !important; }}
            .composer-shell {{
                background:var(--nano-input) !important; border:1px solid var(--nano-line) !important; color:var(--nano-fg) !important; border-radius:6px !important;
            }}
            textarea, input, .main-input {{
                color:var(--nano-fg) !important; font-family:var(--nano-mono) !important; caret-color:var(--nano-amber) !important;
            }}
            input::placeholder, textarea::placeholder {{ color:var(--nano-faint) !important; }}
            /* ⭐ 所有弹出菜单：**比周围亮一档**才浮得起来（2026-08-14 定，
               先在上下文面板上验过，确认之后统一提上来 —— 保持一致）。

               🔴 原来的底色 `var(--nano-panel)` 与 composer 外壳、设置卡片**完全相同**，
                  边框也几乎相同，于是弹出层和它盖住的东西读成了同一个面。
                  原话：「感觉跟背景粘在一起了，怀疑是因为输入框的
                  边框线和这个框完全一样」—— 诊断准确。

               📌 **深色 UI 上「浮起来」不能靠阴影** —— 黑底上的黑阴影约等于看不见；
                  能读出层次的只有**亮度差**。所以底色 / 边框各提一档，
                  再套一圈极淡的白色描边（`0 0 0 1px rgba(var(--nano-contrast-rgb), .035)`）
                  当作"边缘高光"。 */
            body.nano-theme-terminal .q-menu.nano-menu-popup,
            /* ══ 下拉弹层的**样子**（共享）与**行为**（各自）必须分开 ══════
               🔴 2026-08-14 踩的：语言下拉复用了 `model-select-popup`，
                  结果**把模型下拉的 tooltip 一起抄来了** —— 鼠标停在
                  「简体中文」上弹出「均衡旗舰 · 强推理 · 多模态」。
                  真凶是一段 body 级 JS：它 hook 的正是
                  `.model-select-popup .q-item`，再**按下标**取 TIPS[i]）。
                  ⚠️ 那套 tooltip 已于 2026-08-31 删除（多厂商之后按下标必然指错，
                     而且我们不代替厂商介绍模型强度）—— 但**这条教训仍然成立**：
               📌 **复用一个类，继承的不只是它的样子，还有所有挂在它身上的行为**
                  —— 而行为不写在样式表里，你翻 CSS 是看不见它的。
               ⭐ 所以拆成两层：
                    `.nano-select-popup`   只管样子，谁都能用
                    `.model-select-popup`  = 样子 + 模型专属行为（不高亮；tooltip 已删）
                  这样"新下拉忘了给背景就是透明的"那个老问题也一并解决了：
                  有一个**默认该用的**类，而不是每次去挑一个别人的类。 */
            .nano-select-popup,
            .model-select-popup {{
                background:var(--nano-panel-2) !important; border:1px solid var(--nano-line) !important; color:var(--nano-fg) !important;
                border-radius:12px !important;
                box-shadow:0 10px 34px rgba(var(--nano-ink-rgb), 0.62),
                           0 0 0 1px rgba(var(--nano-contrast-rgb), 0.035) !important;
            }}
            .nano-menu-popup .q-item,
            .nano-select-popup .q-item,
            .model-select-popup .q-item {{ color:var(--nano-fg) !important; }}
            /* ⚠️ 悬停高亮跟「设置」那个菜单同一套观感。
               📌 一个可点的列表项，如果鼠标停上去毫无反应，用户会先怀疑它能不能点。
               ⚠️ 模型下拉**刻意不要**这个（下面 9798 那条把它压掉了）——
                  它用 tooltip 表达"我在这一项上"，两套反馈叠加会打架。 */
            .nano-select-popup .q-item {{
                padding:8px 14px !important; min-height:34px !important;
                font-size:var(--nano-fs-base) !important; transition:background 0.12s !important;
                font-family:var(--nano-mono) !important;
            }}
            .nano-select-popup .q-item:hover {{
                background:rgba(var(--nano-contrast-rgb), 0.06) !important;
            }}
            .nano-select-popup .q-item.q-item--active {{
                color:var(--nano-amber) !important; background:rgba(var(--nano-amber-rgb), 0.08) !important;
            }}
            .nano-menu-popup .q-item.nano-active {{
                background:transparent !important; color:var(--nano-accent) !important;
                border-left-color:transparent !important; font-weight:500 !important;
            }}
            .nano-menu-popup .q-item:hover,
            .model-select-popup .q-item:hover {{ background:rgba(var(--nano-contrast-rgb), 0.05) !important; }}
            ::-webkit-scrollbar {{ width:9px; height:9px; }}
            ::-webkit-scrollbar-thumb {{ background:var(--nano-border); border-radius:0; }}
            ::-webkit-scrollbar-thumb:hover {{ background:var(--nano-line); }}
            ::-webkit-scrollbar-track {{ background:transparent; }}
            /* 二级 UI 一刀切终端化：所有对话框/卡片 → 深色面板（覆盖 skill 副作用/os 授权/
               选择卡片/各 dialog 的白底残留）。文字若是浅色类(text-slate-xxx)在深底自然可读。 */
            .q-card {{
                background:var(--nano-panel) !important; border:1px solid var(--nano-border) !important;
                color:var(--nano-fg) !important; border-radius:6px !important;
            }}
            .q-dialog__backdrop {{ background:rgba(var(--nano-ink-rgb), 0.55) !important; }}
            /* 抽屉里旧深色模式残留的深色硬编码文字(var(--nano-fg) 等)在深底下看不见 → 救回。
               放最后、加 !important，盖过非 important 的内联色；语义色(绿/琥珀)单独再上。 */
            .q-drawer .q-item__label,
            .q-drawer label {{ color:var(--nano-fg) !important; }}
            /* tailwind slate 文字类残留 → 终端灰(去蓝) */
            .text-slate-200, .text-slate-300 {{ color:var(--nano-fg) !important; }}
            .text-slate-400, .text-slate-500,
            .text-slate-600 {{ color:var(--nano-fg-soft) !important; }}
            /* 模型选择器：只改文字色/字体/字号，不强改内部 flex 布局(那会把 q-select 挤乱) */
            .model-select-field .q-field__native,
            .model-select-field .q-field__native > span {{
                color:var(--nano-fg) !important; font-family:var(--nano-mono) !important;
            }}
            /* 图标只改色，绝不改字体(改了 ligature 会变成 arrow_drop_down 字面乱码) */
            .model-select-field .q-icon {{ color:var(--nano-fg) !important; }}
            .model-select-field .q-field__native {{ font-size:var(--nano-fs-base) !important; }}
            .model-select-field .q-field__control {{ min-height:26px !important; }}
            /* 模型下拉列表：跟 Auto/effort 一致——选中只字亮，去橘黄边框、去hover灰块、去圆环动画 */
            .model-select-popup .q-item.q-item--active {{
                background:transparent !important; border-left:none !important;
                color:var(--nano-amber) !important; font-weight:500 !important;
            }}
            .model-select-popup .q-item:hover {{ background:transparent !important; }}
            .model-select-popup .q-item:hover::before {{ display:none !important; content:none !important; }}
            .model-select-popup .q-item {{ border-bottom:none !important; }}
            /* broad：所有 select/输入框文字/值/下拉项 → 终端浅色(修个人信息等对话框深底深字看不清) */
            .q-field__native,
            .q-field__native > span,
            .q-field__input,
            .q-item__label,
            .q-select__dropdown-icon {{ color:var(--nano-fg) !important; }}

            /* ══ 设置里的语言下拉：**描边是暗白，不是琥珀** ══════════════
               ⚠️ Quasar 的 `outlined` 用 `--q-primary` 画聚焦态边框
               （`.q-field__control:after`），而本项目的 primary 是主题琥珀
               —— 于是一个**下拉框**长得像一个**主按钮**（导出那种）。
               📌 「这又不是按钮」—— 琥珀在这套界面里是**动作**的颜色，
                  用在一个"选一下"的控件上，等于把强调级别说错了。
               ⭐ `:before`（静态）和 `:after`（聚焦）**必须一起压**：
                  只压静态的话，点开的那一刻它又变回琥珀。 */
            .lang-select-field .q-field__control:before,
            .lang-select-field .q-field__control:after {{
                border: 1px solid rgba(var(--nano-contrast-rgb), 0.16) !important;
            }}
            .lang-select-field .q-field__native,
            .lang-select-field .q-field__native > span {{
                color:var(--nano-fg) !important; font-family:var(--nano-mono) !important;
                font-size:var(--nano-fs-base) !important;
            }}
            .lang-select-field .q-icon {{ color:var(--nano-dim) !important; }}
            .q-field__label {{ color:var(--nano-dim) !important; }}
            .q-field__control::before {{ border-color:var(--nano-line) !important; }}
            .q-field__control::after {{ border-color:var(--nano-amber) !important; }}
            /* ⚠️ 设置面板里把上面那条**琥珀描边**降成中性色（2026-08-29：
               「这一大堆黄色框线太扎眼」）。个人信息页一屏五六个输入框，
               全是高饱和琥珀边时，**强调色因为到处都是而不再是强调**。
               📌 只在 `.nano-settings-pane` 内覆盖，不动全局 —— 聊天输入框那条
                  琥珀边是有意的（那里只有一个，它确实该被看见）。 */
            /* ⚠️ 作用域要**跟着 DOM 走**，不能跟着「我以为它在哪」走：
               用量限额 / 环境配置那两个弹窗是从设置面板里点开的，但它们在 DOM 上是
               **独立的 dialog**，不在 .nano-settings-pane 内 —— 所以要单独挂
               .nano-soft-field，靠嵌套是够不着的。 */
            .nano-settings-pane .q-field__control::after,
            .nano-soft-field .q-field__control::after {{
                border-color:rgba(var(--nano-fg-rgb), 0.26) !important; }}
            .nano-settings-pane .q-field--focused .q-field__control::after,
            .nano-soft-field .q-field--focused .q-field__control::after {{
                border-color:rgba(var(--nano-fg-rgb), 0.55) !important; }}
            /* 中转徽章与模型名残留空格：把 select 往左拉紧 */
            .model-select-field {{ margin-left:0 !important; }}
            .model-select-field .q-field__control-container {{ padding-left:0 !important; }}
            /* 根因：span 上有 padding-left:30px(抵消下拉箭头做"视觉居中")，清掉它，中转才能贴紧模型名 */
            .model-select-field .q-field__native > span:not(.q-select__focus-target) {{
                padding-left:0 !important; justify-content:flex-start !important;
            }}
            /* 根治"灰色/黑色反应块"：它是 Quasar 的 .q-focus-helper 浮层(hover/focus 时
               currentColor 半透明铺满)，不是元素背景——之前设 background 都没用。直接干掉。 */
            .q-focus-helper {{ opacity:0 !important; display:none !important; }}
            /* 模型框：紧凑 + 文字垂直居中(对照设计图小尺寸) */
            .model-select-field .q-field__control {{
                min-height:28px !important; height:28px !important;
            }}
            .model-select-field .q-field__control-container {{
                padding:0 !important; min-height:28px !important;
            }}
            .model-select-field .q-field__native {{
                min-height:28px !important; padding:0 !important; align-items:center !important;
            }}
            /* 开关/滑块：Quasar 默认蓝 → 终端琥珀 */
            .q-toggle__inner--truthy {{ color:var(--nano-accent) !important; }}
            .q-slider__track--active,
            .q-slider__selection {{ background:var(--nano-accent) !important; color:var(--nano-accent) !important; }}
            .q-slider__thumb {{ color:var(--nano-accent) !important; }}
            .q-slider__track {{ background:var(--nano-line) !important; }}
            /* 蓝色主按钮(color=primary/blue/info) → 终端琥珀；文字类蓝 → 琥珀 */
            .q-btn.bg-primary, .q-btn.bg-blue,
            .q-btn.bg-info {{ background:var(--nano-amber) !important; color:var(--nano-bg) !important; }}
            .text-primary, .text-blue,
            .text-info {{ color:var(--nano-amber) !important; }}
            /* 模型下拉三角图标垂直居中(对齐文字) */
            .model-select-field .q-field__append {{
                height:28px !important; align-items:center !important; padding:0 !important;
            }}
            /* 问候语顶对齐（外部评审 方案）：scroll_area 真正吃掉剩余高度，内容从顶部开始 */
            .main-chat-col > .q-scrollarea {{
                flex: 1 1 0% !important; min-height: 0 !important; width: 100% !important;
            }}
            .main-chat-col .q-scrollarea__container {{ min-height: 100% !important; }}
            .main-chat-col .q-scrollarea__content {{ min-height: 100% !important; display: block !important; }}
            /* 全局扁平化：终端是直角、描边、无柔光。圆角统一压到 2px，去模糊。
               头像/图片/考拉保留原样（不强制方块）。 */
            body.nano-theme-terminal *:not(.q-avatar):not(img):not(.nano-koala-wrap):not(.q-avatar *) {{
                border-radius:2px !important;
            }}
            body.nano-theme-terminal * {{ backdrop-filter:none !important; -webkit-backdrop-filter:none !important; }}
            /* 圆形发送按钮 → 方块；模型选择/各种胶囊 → 直角描边 */
            body.nano-theme-terminal .q-btn {{ box-shadow:none !important; }}
            /* 导航选中态：去掉浮夸黄色荧光框，改成文字本身变主题琥珀（像 ★ 那种语义高亮） */
            body.nano-theme-terminal .nav-glow {{ box-shadow:none !important; background:transparent !important; }}
            /* ⚠️ 终端风【没有】外圈胶囊（.nav-glow 的 box-shadow 被上一行清掉了），
               所以它靠标签变色表示选中；浅色主题保留胶囊，胶囊本身就是反馈，
               再叠一层蓝字反而多余（实测结论：「让外圈胶囊表示选中语义」）。 */
            body.nano-theme-terminal .nav-glow .nav-label {{ color:var(--nano-accent) !important; }}
            .nav-label {{ color:var(--nano-fg-soft); font-family:var(--nano-mono); }}
            /* 选中态叠第二个【不依赖颜色】的信号：字重。终端风靠标签变琥珀，
               浅色靠外圈胶囊 —— 两边再加个加粗，低对比场景下也认得出。 */
            .nav-glow .nav-label {{ font-weight:600 !important; }}
            /* 终端下去掉鼠标悬停的灰色反应块（hover:bg-white/5），文字本身够清楚 */
            .hover\\:bg-white\\/5:hover {{ background-color:transparent !important; }}
            #q-app, .q-layout, .q-page-container, .q-page, .nicegui-content, main {{ background:transparent !important; }}
            #q-app {{ position:relative; z-index:1; isolation:isolate; }}
            .q-layout {{ min-height:100vh !important; }}
            .q-dialog__backdrop {{ backdrop-filter:blur(1.5px); background:rgba(var(--nano-ink-rgb), 0.34) !important; }}

            /* 终端流式光标：琥珀方块闪烁，跟在正在吐字的内容末尾 */
            @keyframes nano-blink {{ 0%,49% {{ opacity:1; }} 50%,100% {{ opacity:0; }} }}
            .nano-cursor {{ color:var(--nano-amber); font-family:var(--nano-mono); animation:nano-blink 1s step-end infinite; }}
            /* 回复首行与 nano ❯ 提示符对齐：markdown 首段去掉顶边距 */
            .nicegui-markdown > :first-child {{ margin-top:0 !important; }}

            /* ══ 聊天区**永远不许**出现横向滚动条（2026-08-14 实测发现）══
               🔴 现象：一句巨长的话不换行，整个主聊天区多出一条横向滚动条。
                  真凶是 markdown 的 ``` 代码块 —— `<pre><code>` 默认
                  `white-space: pre`，一行有多长就撑多宽（实测撑到 4015px），
                  于是**把整个 q-scrollarea 顶宽了**。
               ⚠️ 注意这不是"某个气泡变宽"，是**页面级**的溢出：一条消息里的
                  一个代码块，会让上面所有消息一起跟着横向滚。
                  📌 一个能撑宽祖先的子元素，症状会出现在离它很远的地方。

               ⭐ 选择「换行」而不是「块内独立横向滚动」（2026-08-14 定）：
                  Nano 是聊天，不是代码浏览器；而且实际撞上这条的绝大多数是
                  **模型把中文正文包进了 ``` 围栏**，那本来就该换行。
               ⚠️ 代价说清楚：真代码（Skill 源码）的长行也会跟着折行，缩进会错位。
                  接受它 —— 在一个 600px 宽的聊天栏里横向滚代码更难读。

               ⚠️ `overflow-wrap:anywhere` 是**必须的第二道**：只加 pre-wrap 的话，
                  一个超长不可断 token（URL / base64 / 一长串英文）照样撑宽。
                  📌 修"这句话太长"和修"这个词太长"是两件事，只修前者会复发。 */
            .nicegui-markdown pre,
            .nicegui-markdown code {{
                white-space: pre-wrap !important;
                word-break: break-word !important;
                overflow-wrap: anywhere !important;
                max-width: 100% !important;
            }}
            /* 兜底：任何别的东西再想撑宽滚动区，也只到这里为止 */
            .main-chat-col .q-scrollarea__container {{
                overflow-x: hidden !important;
            }}
            .main-chat-col .q-scrollarea__content {{
                width: 100% !important; max-width: 100% !important;
            }}
            /* ⚠️ 表格是**例外**：折行会让它彻底不可读，所以让它在**自己的框里**
               横向滚，而不是把整页顶宽。📌 宽内容要么换行，要么自带滚动条，
               唯独不许推着别人走。 */
            .nicegui-markdown table {{ display:block; overflow-x:auto; max-width:100%; }}

            /* 思考块折叠标题从左到右扫光——代表"还在思考中" */
            @keyframes thought-sweep {{
                0% {{ background-position: -200% center; }}
                100% {{ background-position: 200% center; }}
            }}
            .thought-pulsing {{
                background: linear-gradient(90deg, var(--nano-fg-soft) 30%, #e0d0c0 50%, var(--nano-fg-soft) 70%);
                background-size: 200% auto;
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                animation: thought-sweep 1.8s linear infinite;
            }}

            /* ── 抽屉 ── */
            /* 📌 颜色写在 CSS 里，不靠 JS 内联样式刷。
               早期是靠 JS 给每个抽屉 setProperty 翻色，但 breakpoint 折叠/重展
               时 Quasar 会重建 DOM、内联样式随之丢失，抽屉就露出底下的旧色。
               ⇒ 状态由 CSS 承载，JS 只负责换 body 上那个类名。 */
            .q-drawer {{
                background: var(--nano-chrome) !important;
                backdrop-filter: blur(20px) !important;
            }}
            .q-drawer--left {{
                /* ⚠️ 用 --nano-border，不用 rgba(shade,0.08)：后者压在深底上不可见，
                   抽屉和主区就成了没有分界线的生硬色块交界。 */
                border-right: 1px solid var(--nano-border) !important;
            }}
            .q-drawer--right {{
                border-left: 1px solid var(--nano-border) !important;
            }}

            /* ── Header 磨砂玻璃 ── */
            /* ══════════════════════════════════════════════════════════
               🔴🔴 聊天区顶部那一大片黑色空白 —— **修了两次没修掉的那个**
               ══════════════════════════════════════════════════════════
               实测（2026-08-14，量 DOM 量出来的，不是猜的）：

                 header             position:relative  top=0  height=60  ← 真实占位
                 q-page-container   style="padding-top:60px"             ← 🔴 又补一次
                 nicegui-content    padding-top:16px
                 main-chat-col      padding-top:32px
                 q-scrollarea__content padding-top:16px
                 ───────────────────────────────────────
                 第一行字 top = 184，而标题栏只有 60。**124px 是纯空白。**

               ⭐ 成因：Quasar 的 QHeader 默认 `position:fixed`（浮在内容之上），
                  所以 QPageContainer 必须补一个**等于 header 高度**的 padding-top
                  把内容顶下来。而本项目为了无边框窗口把 header 改成了在流内 ——
                  **于是两边都在让位，让了两次。**

               📌 **为什么两次都没修掉**：那 60px 不是任何人写的 CSS，是 Quasar
                  按 header 高度**自动注入的行内样式**（`style="padding-top:60px"`）。
                  在源码里 grep `padding-top` 永远找不到它。
                  📌 **一个查不到出处的样式，先去 DOM 上量，别在源码里找。**

               ⭐ 项目其实**摸到过这个 padding 一次**：`.nano-mini .q-page-container`
                  把它整个清零了，但注释只解释了左边那 256px（抽屉让位），
                  **没人回头问上边那 60px 是不是也多余**。
                  📌 **解决了一个症状之后，值得回头问一句「同一个东西还有别的症状吗」。**

               ⚠️ 只清 padding-top：`padding-left:256px` 是给左抽屉让位的，**必须留着**。
               ⚠️ 假设：header 始终在流内（本项目里它要么 relative、要么在 mini 下
                  `display:none`）。哪天它真变成 fixed，表现是正文钻到半透明标题栏底下 ——
                  **看得见的失败**，不是静默的，所以不额外加守卫。 */
            .q-page-container {{ padding-top: 0 !important; }}

            /* 🔴🔴 **同一个 bug 的另一半** —— 修完上面那条之后，那 60px 从顶部
               跑到了底部（实测：`q-page-container` bottom=950 > 视口 918）。

               成因是**同一个双算**：Quasar 给 `.q-page` 算的
                   min-height = 视口(918) − header(60) = 855
               是留给「内容从 0 开始」的；而 header 真实占位、page 已经从 60 开始，
               **再要 855 就多了 60**，再加上 `.nicegui-content` 上下各 16px 的默认
               padding，正好顶穿视口。

               📌 **一个「双算」bug 通常有两处症状，修掉一处只是把它挪个地方** ——
                  顶部那片空白和底部这片溢出，是同一件事的两个面孔。
                  （这正好是上面那条判据「解决了一个症状之后，回头问一句同一个东西
                    还有别的症状吗」的第二次兑现 —— 而这次是用户发现的。）

               ⚠️ 只清**上下** padding：左右那份是页面内容的呼吸，留着。
               ⭐ 顺带把顶部又收紧 16px：第一行字 124 → 108。 */
            .nicegui-content {{ padding-top: 0 !important; padding-bottom: 0 !important; }}

            .q-header {{
                background: rgba(var(--nano-shade-rgb), 0.85) !important;
                backdrop-filter: blur(20px) !important;
                -webkit-backdrop-filter: blur(20px) !important;
                border-bottom: 1px solid rgba(var(--nano-contrast-rgb), 0.05) !important;
            }}

            /* ── 输入框 ── */
            /* Item3 修复：原来 height/min/max-height 都锁在 52px，
               导致输入框只能单行、文字超出后只能横向溢出(看不到之前打的字)。
               改成 min-height(单行外观不变) + max-height(上限，超出后内部
               滚动)，配合下面 .q-field__native 的 textarea + autogrow，
               实现"随内容增高，到上限后内部滚动"。 */
            /* 输入框自身不再单独带边框/背景——边框和背景挪到外层
               .composer-shell 上，让附件按钮+输入框+发送按钮在视觉上
               读作一个整体的圆角胶囊，而不是三个各自独立的元素拼在一起。 */
            /* min-width:0 + width:100%：textarea 默认带 cols 固有宽度，不肯随窗口
               收缩，窄窗会把整条 composer 顶大、飞出窗口右侧。强制可收缩。 */
            /* 输入行单行中线对齐（外部评审 方案）：提示符/textarea/按钮统一 30px 中线 */
            .composer-input-row {{ align-items: center !important; }}
            .composer-input-row .composer-prompt {{
                height: 30px !important; line-height: 30px !important;
                display: flex !important; align-items: center !important;
            }}
            .composer-input-row .q-btn {{ align-self: center !important; padding: 0 !important; }}
            .composer-input-row .q-btn .q-icon {{ line-height: 1 !important; }}
            .main-input, .main-input .q-field__inner, .main-input .q-field__control {{
                min-width: 0 !important; width: 100% !important;
            }}
            .main-input {{ align-self: center !important; }}
            .main-input .q-field__inner {{ padding: 0 !important; }}
            .main-input .q-field__control {{
                background: transparent !important; border: none !important;
                min-height: 30px !important; height: auto !important; max-height: 140px !important;
                padding: 0 !important; display: flex !important; align-items: center !important;
                overflow: visible !important;
            }}
            .main-input .q-field__control-container {{
                padding: 0 !important; min-height: 30px !important;
                display: flex !important; align-items: center !important;
            }}
            .main-input textarea.q-field__native, .main-input .q-field__native {{
                color: var(--nano-fg) !important; box-sizing: border-box !important; display: block !important;
                padding: 4px 0 !important; font-size:var(--nano-fs-lg) !important; line-height: 22px !important;
                min-height: 30px !important; max-height: 140px !important;
                resize: none !important; overflow-y: auto !important; overflow-x: hidden !important;
                min-width: 0 !important; width: 100% !important;
            }}
            /* 📌 主题联动要用【class 切换】，别用 JS inline style 写颜色：
               inline style 的优先级会盖过下面 :hover/:focus-within 这些伪类，
               切一次主题后悬停/聚焦动效就失效；class 切换是普通 CSS 规则，
               没有这个问题。
               ⚠️ 也别依赖 body--dark —— 实测这个环境里 Quasar.dark.set()
                  不会可靠地翻转 body 上那个 class。 */
            .composer-shell {{
                border-radius: 24px !important;
                background: rgba(var(--nano-shade-rgb), 0.9) !important;
                border: 1px solid rgba(var(--nano-contrast-rgb), 0.06) !important;
                transition: border-color 0.2s, box-shadow 0.2s, background 0.3s !important;
            }}
            .composer-shell:hover {{
                border: 1px solid rgba(var(--nano-amber-rgb), 0.35) !important;
            }}
            .composer-shell:focus-within {{
                border: 1px solid rgba(var(--nano-amber-rgb), 0.6) !important;
                box-shadow: 0 0 0 4px rgba(var(--nano-amber-rgb), 0.12) !important;
            }}

            /* ── 滚动条 ── */
            ::-webkit-scrollbar {{ width: 4px; }}
            ::-webkit-scrollbar-track {{ background: transparent; }}
            ::-webkit-scrollbar-thumb {{
                background: rgba(var(--nano-contrast-rgb), 0.06);
                border-radius: 999px;
            }}
            ::-webkit-scrollbar-thumb:hover {{
                background: rgba(var(--nano-contrast-rgb), 0.12);
            }}

            /* ── 过渡动画 ── */
            .theme-card, .theme-user-card, .theme-text, .theme-user-text {{
                transition: all 0.3s ease !important;
            }}
            /* 卡片投影：之前所有卡片都是纯色块平贴在背景上，没有层次感。
               用一套轻量、深浅两色背景下都能看的阴影，不依赖 body--dark
               这个在这个环境里不可靠的 class（实测 Quasar.dark.set() 不会
               稳定翻转它，composer-shell 那次踩过同样的坑）。 */
            .theme-card {{
                box-shadow: 0 1px 3px rgba(var(--nano-ink-rgb), 0.18), 0 1px 2px rgba(var(--nano-ink-rgb), 0.12) !important;
            }}
            .composer-shell {{
                box-shadow: 0 2px 10px rgba(var(--nano-ink-rgb), 0.16) !important;
            }}

            /* ── 初始化阶段文案 渐隐动画 ── */
            .init-stage-text {{
                transition: opacity 0.35s ease;
                opacity: 1;
            }}
            .init-stage-text.fade-out {{
                opacity: 0;
            }}

            /* ── 下拉菜单（设置/外观/Skill操作 用的都是这套模式）── */
            /* Quasar 的 q-menu 容器自己带一层默认深灰背景(约 rgb(29,29,29))，
               跟我们手写的卡片背景(#0e1018 等)不是同一个颜色，而且容器本身是
               直角、内层卡片是圆角——圆角处会露出容器自己的直角深灰边，
               看起来像"颜色不对"或"没对齐"。根因是容器背景没清掉，不是
               定位计算错，这里统一清掉，让内层卡片自己的背景/圆角说了算。 */
            .q-menu {{
                background: transparent !important;
                box-shadow: none !important;
            }}
            /* 模型下拉的原生选项列表 */
            /* ⚠️ 基础规则也要给共享类一份 —— 📌 只在终端主题里给背景，
               切浅色时"透明弹层"那个问题会原样回来（同 `.nano-menu-popup`
               当年那条注释）。 */
            .nano-select-popup,
            .model-select-popup {{
                background: var(--nano-panel) !important;
                border: 1px solid var(--nano-border) !important;
                box-shadow: 0 8px 32px rgba(var(--nano-shade-rgb), 0.18) !important;
                border-radius: 12px !important;
                overflow: visible !important;
                padding: 4px 0 !important;
            }}
            .model-select-popup .q-item {{
                color: var(--nano-fg) !important;
                border-bottom: 1px solid var(--nano-border) !important;
                padding: 8px 14px !important;
                min-height: 36px !important;
                font-size:var(--nano-fs-md) !important;
                transition: background 0.12s !important;
                position: relative !important;
                overflow: visible !important;
            }}
            .model-select-popup .q-item:last-child {{
                border-bottom: none !important;
            }}
            .model-select-popup .q-item.q-item--active {{
                background: rgba(var(--nano-amber-rgb), 0.10) !important;
                color: var(--nano-amber) !important;
                font-weight: 600 !important;
                border-left: 3px solid rgba(var(--nano-amber-rgb), 0.55) !important;
            }}
            .model-select-popup .q-item:not(.q-item--active):hover {{
                background: rgba(var(--nano-shade-rgb), 0.05) !important;
            }}
            /* 圆圈读条：hover 时右上角出现一个从0填满的圆弧 */
            @property --model-ring-pct {{
                syntax: '<percentage>';
                inherits: false;
                initial-value: 0%;
            }}
            .model-select-popup .q-item:hover::before {{
                content: '';
                position: absolute;
                top: 8px;
                right: 10px;
                width: 11px;
                height: 11px;
                border-radius: 50%;
                background: conic-gradient(var(--nano-amber) var(--model-ring-pct), rgba(var(--nano-amber-rgb), 0.18) 0%);
                -webkit-mask: radial-gradient(circle, transparent 52%, black 53%);
                mask: radial-gradient(circle, transparent 52%, black 53%);
                animation: model-ring-fill 0.6s linear forwards;
                pointer-events: none;
                z-index: 1;
            }}
            @keyframes model-ring-fill {{
                to {{ --model-ring-pct: 100%; }}
            }}
            /* 主动智能：Auto / effort 下拉的卡片（.q-menu 被全局清成透明，这里给回白底）。
               用 .q-menu.nano-menu-popup 提高特异性，盖过上面的透明规则。 */
            /* ⚠️ 基础规则（终端主题会整块覆盖它）。这里也提一档亮度 ——
               📌 只改终端主题会让切浅色时"粘背景"那个问题原样回来。 */
            .q-menu.nano-menu-popup {{
                background: var(--nano-panel-2) !important;
                border: 1px solid var(--nano-line) !important;
                box-shadow: 0 10px 34px rgba(var(--nano-ink-rgb), 0.55),
                            0 0 0 1px rgba(var(--nano-contrast-rgb), 0.035) !important;
                border-radius: 12px !important;
                padding: 4px 0 !important;
            }}
            .nano-menu-popup .q-item {{
                color: var(--nano-fg) !important;
                padding: 7px 16px !important;
                min-height: 32px !important;
                font-size:var(--nano-fs-base) !important;
                transition: background 0.12s !important;
            }}
            .nano-menu-popup .q-item:hover {{
                background: rgba(var(--nano-shade-rgb), 0.05) !important;
            }}
            /* 当前选中项：用高亮（左色条 + 浅底），不用对钩——对齐模型下拉的选中效果 */
            .nano-menu-popup .q-item.nano-active {{
                background: rgba(var(--nano-warn-rgb),0.10) !important;
                color: var(--nano-warn) !important;
                font-weight: 600 !important;
                border-left: 3px solid rgba(var(--nano-warn-rgb),0.55) !important;
            }}
            /* body-level tooltip，由 JS 创建 fixed div，不属于 q-menu 绘制树 */
            .nano-model-tip {{
                position: fixed;
                z-index: 100000;
                background: var(--nano-panel);
                border: 1px solid var(--nano-line);
                color: var(--nano-fg);
                padding: 10px 14px;
                border-radius: 6px;
                font-family: var(--nano-mono);
                font-size:var(--nano-fs-base);
                line-height: 1.65;
                white-space: pre-line;
                max-width: 230px;
                pointer-events: none;
                opacity: 0;
                transform: translateY(-50%) translateX(-4px);
                transition: opacity 0.18s ease, transform 0.18s ease;
                box-shadow: 0 8px 28px rgba(var(--nano-shade-rgb), 0.14);
            }}
            .nano-model-tip.show {{
                opacity: 1;
                transform: translateY(-50%) translateX(0);
            }}
            /* 模型下拉字段：只留结构（flex 撑满 + border-box）。
               🔴 这里曾有 `justify-content:center` + `padding-left:30px`（为抵消
                  右侧 30px 下拉箭头做"视觉居中"），但它与上面那条
                  「中转徽章与模型名残留空格」的修复**直接矛盾**，
                  一直靠 body.nano-theme-terminal 前缀的高特异性压着。
                  2026-08-30 去前缀后特异性打平 ⇒ 老规则靠源码顺序翻盘 ⇒ 空格回归。
               📌 两条互相矛盾、靠特异性决胜负的规则是定时炸弹 —— 删掉冲突那两条，
                  而不是把前缀加回去。 */
            .model-select-field .q-field__native > span:not(.q-select__focus-target) {{
                flex: 1 !important;
                display: flex !important;
                box-sizing: border-box !important;
            }}
            /* 模型下拉列表项：文字居中 */
            .model-select-popup .q-item__label {{
                text-align: center !important;
            }}
            /* 策略引擎 / 模型说明 select field 样式——outlined 在白底上不够显眼 */
            .strategy-select .q-field__control {{
                background: var(--nano-panel-2) !important;
                border-radius: 6px !important;
            }}
            .strategy-select .q-field__control:hover {{
                background: var(--nano-border) !important;
            }}

            /* ── 右侧导航激活态：胶囊发光边框 ── */
            /* 三个抽屉合并成一个之后没有滑入动画了，用这个表示"当前
               显示的是哪个面板"。原来用 text-shadow 文字发光，但字号太小
               几乎看不出效果，改成胶囊形状的发光边框包住整个按钮（图标+
               文字），颜色沿用原来发光色，不用纯色块/纯颜色变化（按用户
               要求）。 */
            .nav-glow {{
                border-radius: 999px !important;
                box-shadow: 0 0 0 1.5px rgba(var(--nano-amber-rgb), 0.85), 0 0 10px rgba(var(--nano-amber-rgb), 0.55) !important;
                transition: box-shadow 0.3s ease !important;
            }}

            /* ── 知识库上传区 ── */
            /* .kb-upload 这个 class 是直接加在 .q-uploader 这个元素本身上的，
               不是它的祖先——选择器必须是 .q-uploader.kb-upload（无空格，
               同一元素），写成 ".kb-upload .q-uploader"（后代选择器）永远
               匹配不到，是这次发现的一个真实 bug，不是新引入的。 */
            .q-uploader.kb-upload {{ background: transparent !important; box-shadow: none !important; }}
            .kb-upload .q-uploader__header {{ display: none !important; }}
            .kb-upload .q-uploader__list,
            .kb-upload .q-placeholder {{ background: transparent !important; }}

            /* ── 审计弹窗 CodeMirror host ── */
            .nano-cm-host {{
                width: 100%;
                min-height: 430px;
                border: 1px solid var(--nano-line);
                border-radius: 10px;
                background: var(--nano-panel);
                overflow: hidden;
            }}
            .nano-cm-host .cm-editor {{
                height: 430px;
                background: var(--nano-panel);
                font-family: "Fira Code", "JetBrains Mono", "Consolas", monospace;
                font-size:var(--nano-fs-base);
                line-height: 1.6;
            }}
            .nano-cm-host .cm-scroller {{
                font-family: "Fira Code", "JetBrains Mono", "Consolas", monospace !important;
            }}
            /* ── 选择策略：默认可选，只在 chrome 上关掉 ──────────────
               pywebview 的 `text_select=True` 已经把全局那条 `user-select:none`
               去掉了。这里只补少数**不该被选中**的地方：
                 · 拖拽区 —— 拖窗口时顺手选中标题文字，观感很差
                 · 按钮 / 图标 —— 双击会选中标签文字
               ⚠️ 聊天区、代码框、工具卡**一律不许出现在这张表里**，
                  那正是 要修的东西。 */
            .pywebview-drag-region,
            .nano-no-select,
            .q-btn, .q-tab, .q-item__label, .material-icons {{
                -webkit-user-select: none; user-select: none;
            }}
            /* 聊天区显式打开（防止将来某个父容器的 none 继承下来 ——
               📌 一个靠"没人给它设 none"维持的可选状态，迟早会被继承掉） */
            .nano-chat-scroll, .nano-chat-scroll * {{
                -webkit-user-select: text; user-select: text;
            }}
            /* ══ 右键菜单 / 文件链接 / 代码框 / 拖拽遮罩 ══════════════ */
            .nano-ctx-menu {{
                position: fixed; z-index: 12000; min-width: 132px;
                background: var(--nano-panel-2); border: 1px solid rgba(var(--nano-fg-rgb), 0.14);
                border-radius: 8px; padding: 4px;
                box-shadow: 0 8px 26px rgba(var(--nano-ink-rgb), 0.42);
                font-family: var(--nano-mono); user-select: none;
            }}
            .nano-ctx-item {{
                display: flex; align-items: center; gap: 8px;
                padding: 6px 10px; border-radius: 6px; cursor: pointer;
                font-size:var(--nano-fs-base); color: var(--nano-fg); white-space: nowrap;
            }}
            .nano-ctx-item:hover {{ background: rgba(var(--nano-amber-rgb), 0.14); color: var(--nano-amber); }}
            .nano-ctx-ico {{
                font-size:var(--nano-fs-base); width: 14px; height: 14px; opacity: 0.85;
                display: inline-flex; align-items: center; justify-content: center;
                flex-shrink: 0;
            }}
            .nano-ctx-ico svg {{ width: 13px; height: 13px; }}

            /* 文件链接：**纯高亮文字**，不是按钮。
               🔴 第一版做成了带边框和图标的芯片，一眼就被指出问题：
                  「它要放在一个段落里面的啊，你怎么做成按钮了啊」。
               📌 **行内元素的样式预算比块级小得多** —— 一个句子中间的东西
                  只能改颜色/字重；一旦加上边框和内边距，它就把整行的行高
                  和节奏撑歪了，而那一行的主角是句子，不是它。
               ⚠️ 所以这里只留：颜色 + 等宽 + 悬停下划线。 */
            a.nano-file-link {{
                color: var(--nano-amber); cursor: pointer;
                text-decoration: none;
                font-family: var(--nano-mono); font-size: 0.94em;
                transition: color 0.12s ease;
                word-break: break-all;
            }}
            /* 悬停只提亮颜色。⚠️ 连下划线都不留（纯黄色高亮文字就行）——
               📌 这一行的主角是句子；可点这件事由**颜色 + 鼠标形状**表达就够了，
                  再加任何占位置的装饰都会把行距顶开。 */
            a.nano-file-link:hover {{ color: var(--nano-amber); }}

            /* 代码框 + 右上角复制。⚠️ `position:relative` 是按钮定位的前提 */
            .nano-chat-scroll pre.nano-codeblock {{
                position: relative;
                background: var(--nano-panel); border: 1px solid rgba(var(--nano-fg-rgb), 0.11);
                border-radius: 10px; padding: 12px 14px; margin: 8px 0;
                overflow-x: auto;
            }}
            .nano-chat-scroll pre.nano-codeblock code {{
                background: transparent; padding: 0;
                font-family: var(--nano-mono); font-size:var(--nano-fs-base); line-height: 1.65;
                color: var(--nano-fg);
            }}
            .nano-copy-btn {{
                position: absolute; top: 6px; right: 6px;
                width: 24px; height: 24px; padding: 0; border-radius: 6px;
                display: inline-flex; align-items: center; justify-content: center;
                cursor: pointer;
                color: var(--nano-dim); background: rgba(var(--nano-panel-2-rgb), 0.9);
                border: 1px solid rgba(var(--nano-fg-rgb), 0.14);
                opacity: 0; transition: opacity 0.14s ease, color 0.14s ease;
            }}
            .nano-copy-btn svg {{ width: 13px; height: 13px; }}
            /* 📌 常驻会挡住第一行代码；只在悬停这块时出现，键盘聚焦也要出现 */
            .nano-chat-scroll pre.nano-codeblock:hover .nano-copy-btn,
            .nano-copy-btn:focus {{ opacity: 1; }}
            .nano-copy-btn:hover {{ color: var(--nano-amber); border-color: rgba(var(--nano-amber-rgb), 0.4); }}
            .nano-copy-btn.ok {{ opacity: 1; color: var(--nano-ok); border-color: rgba(var(--nano-ok-rgb),0.4); }}

            /* markdown2 会把围栏代码包一层 `.codehilite`。让它彻底透明 ——
               📌 可见的那个框必须只有一个，两层背景叠出来的边框永远对不齐。 */
            .nano-chat-scroll .codehilite {{
                background: transparent !important; border: none; padding: 0; margin: 0;
            }}
            .nano-chat-scroll .codehilite pre {{ margin: 8px 0; }}
            /* 行内 code 不是代码框，别让它蹭到上面那套样式 */
            .nano-chat-scroll :not(pre) > code {{
                font-family: var(--nano-mono); font-size: 0.92em;
                padding: 1px 5px; border-radius: 4px;
                background: rgba(var(--nano-fg-rgb), 0.09); color: var(--nano-fg-soft);
            }}

            /* 复制之类的一次性小反馈。⚠️ 不用 ui.notify —— 那是从 Python 侧发的，
               而复制发生在浏览器里，绕一圈回来会慢半拍。 */
            #nano-mini-toast {{
                position: fixed; left: 50%; bottom: 84px; transform: translateX(-50%);
                z-index: 12500; pointer-events: none;
                padding: 6px 14px; border-radius: 999px;
                background: rgba(var(--nano-panel-2-rgb), 0.95); color: var(--nano-fg);
                border: 1px solid rgba(var(--nano-fg-rgb), 0.16);
                font-family: var(--nano-mono); font-size:var(--nano-fs-sm);
                opacity: 0; transition: opacity 0.16s ease;
            }}
            #nano-mini-toast.on {{ opacity: 1; }}

            /* 拖拽遮罩 */
            #nano-drop-overlay {{
                position: fixed; inset: 0; z-index: 11500; display: none;
                align-items: center; justify-content: center;
                background: rgba(var(--nano-panel-rgb), 0.62); backdrop-filter: blur(2px);
                pointer-events: none;   /* 📌 绝不能吃掉 drop 事件本身 */
            }}
            #nano-drop-overlay.on {{ display: flex; }}
            .nano-drop-box {{
                padding: 18px 28px; border-radius: 14px;
                border: 2px dashed rgba(var(--nano-amber-rgb), 0.55);
                background: rgba(var(--nano-panel-2-rgb), 0.94);
                color: var(--nano-amber); font-size:var(--nano-fs-md); font-family: var(--nano-mono);
            }}

            /* min-width:100% —— CM6 的 `.cm-content` 默认宽是 min-content，比
               `.cm-scroller` 窄 ⇒ 当前行高亮拉不满、右侧留出一条空带。 */
            .nano-cm-host .cm-content {{ padding: 12px 0; min-width: 100%; }}
            /* ── 左侧白条的根因就在这里 ─────────────────────────────
               原来写的是 background:#f8f9fa + border-right:#e5e7eb，
               **两个都是浅色主题的遗留值**，压在 var(--nano-panel) 的深色编辑器旁边，
               看上去就是一条突兀的白条。行号区跟主题脱节，不是 CM6 的问题。 */
            .nano-cm-host .cm-gutters {{
                background: var(--nano-panel);
                color: var(--nano-faint);
                border-right: 1px solid var(--nano-border);
            }}
            .nano-cm-host .cm-lineNumbers .cm-gutterElement {{
                color: var(--nano-faint);
                padding: 0 10px 0 8px;
            }}
            .nano-cm-host .cm-activeLine,
            .nano-cm-host .cm-activeLineGutter {{
                background: rgba(var(--nano-amber-rgb), 0.06);
            }}
            .nano-cm-host .cm-activeLineGutter {{ color: var(--nano-amber); }}
            .nano-cm-host .cm-focused {{ outline: none !important; }}

            /* ── 只读预览：关掉选中行高亮与光标 ──────────────────────
               ⚠️ 上面那条 `.cm-activeLine` 是给**可编辑**的 Skill 代码框用的：
                  那里光标在哪一行是有意义的信息。
               📌 而在一个改不了的预览里，「当前行」这个概念根本不存在 ——
                  一条跟着鼠标走的高亮条只会让人以为自己能编辑。
               ⚠️ 只关这两样，其余（背景/行号/字体/高度）全部继承上面的 —— **别复制**。 */
            .nano-cm-readonly .cm-activeLine,
            .nano-cm-readonly .cm-activeLineGutter {{
                background: transparent !important;
            }}
            .nano-cm-readonly .cm-activeLineGutter {{ color: var(--nano-faint) !important; }}
            .nano-cm-readonly .cm-cursor {{ display: none !important; }}
            /* ⚠️ 高度**沿用** `.nano-cm-host .cm-editor` 的 430px，不另设。
               🔴 早先这里有一条「按行数算高度」的规则，是为了不让代码把
                  **授权窗**撑开 —— 而现在代码另开了一个窗，
                  📌 **那个问题从根上不存在了，规则也就该跟着消失**
                     （一个补丁的前提没了，补丁本身就是债）。 */

            /* ⚠️ 的**语法高亮**部分刻意不在 CSS 层修。
               CM6 的 token 类名（`ͼb` 这种）是构建时自动生成的，字母后缀不稳定，
               靠 CSS 覆盖等于押注一个随版本变化的实现细节。
               根因在 JS 层：init 用了 `defaultHighlightStyle` —— **CM6 内置的
               浅色配色表**，压在 var(--nano-panel) 深底上必然有深字压深底。
               正确修法是换一张深色配色表，见下方 `<script type="module">` 里的
               `nanoDarkHighlight`。 */
            .nano-cm-host .cm-editor .cm-line {{ color: var(--nano-fg); }}

            /* ── 状态标签统一字体 ── */
            .mono-status {{ font-family: "Fira Code", "JetBrains Mono", monospace; font-size:var(--nano-fs-sm); }}

            /* ── 监控卡片 ── */
            .monitor-metric {{
                background: rgba(var(--nano-contrast-rgb), 0.025);
                border-radius: 12px;
                padding: 12px 14px;
                border: 1px solid rgba(var(--nano-contrast-rgb), 0.04);
            }}
        </style>
        ''')

        # ⭐ 把 vendored 的 CodeMirror 挂成静态目录（2026-08-26）。
        # ⚠️ 必须在注入那段 `<script type="module">` **之前**挂 ——
        #    模块里的 import 路径指向 `/cm/...`，路由不存在就是 404。
        try:
            from nicegui import app as _ngapp
            _cm_dir = pathlib.Path(__file__).resolve().parent / "static" / "cm"
            if _cm_dir.is_dir():
                _ngapp.add_static_files("/cm", str(_cm_dir))
            else:
                # 🔴 响亮失败：见下面那段「不静默回退」的理由。
                logger.error(
                    "[UI] static/cm 不存在 —— **代码高亮会整个不可用**。"
                    "它是 vendored 的 CodeMirror（41 个文件），"
                    "打包时漏了目录就会这样。"
                )
        except Exception as _e_cm:
            logger.error(f"[UI] 挂载 static/cm 失败，代码高亮不可用: {_e_cm}")

        ui.add_head_html(r'''
<script type="module">
(() => {
    if (window.NanoCM) return;
    // ══ CodeMirror 从**本地**加载（2026-08-26 改）══════════════════
    //
    // 🔴🔴 **原来这六个是 esm.sh 的 CDN 地址**，两个后果：
    //    ① 每次开代码窗要等网络 —— 实测单个包 2.5~3 秒，而 esm.sh 返回的
    //       只是个 157 字节的**转发模块**，真正的代码在它 import 的下一层，
    //       浏览器还要**递归解析**。实测：点「查看代码」空白 5~6 秒。
    //    ② ⭐⭐ **离线就没有代码高亮** —— 而 Nano 是个**本地桌面应用**。
    //       📌 一个本地应用的核心 UI，不该依赖一个境外 CDN 活着。
    // ⇒ 整棵依赖树（41 个文件 / 520KB）已经抓进 `static/cm/`，运行时**零联网**。
    // ⚠️ 重新 vendor 的脚本见 Changelog v1.74；升级 CodeMirror 版本时要重跑它。
    const CDN = {
        state:    "/cm/state-46e0e8033a.mjs",
        view:     "/cm/view-c494148df2.mjs",
        commands: "/cm/commands-ea1ee7e33b.mjs",
        language: "/cm/language-4b046cb4d6.mjs",
        // `tags` 来自 @lezer/highlight，**不在 @codemirror/language 里**。
        // 少了这一行，nanoDarkHighlight 拿不到 tags 就会静默回退默认（浅色）高亮，
        // 表面上"修了"其实没生效 —— 这类静默回退最难发现，所以守卫里加了 console.warn。
        python:   "/cm/lang-python-79cf1685db.mjs",
        lezerhl:  "/cm/highlight-9ee4b7cf0c.mjs",
    };
    // ⚠️ **本地文件缺失时不静默回退到 CDN。**
    //    📌 静默回退会让「vendor 没打包进去」这种问题永远发现不了 ——
    //       开发机上有网，看着一切正常；用户装完离线一开，代码框是空的。
    //    ⭐ 同本文件上面那条：**这类静默回退最难发现**，所以要响亮。
    window.__NANO_CM_LOCAL__ = true;
    window.NanoCM = {
        modules: null,
        editors: new Map(),
        _pending: new Map(),   // elementId -> {deltas: [], setValue: null}
        _getPending(key) {
            // ⚠️ `editable` 也必须排队，理由见 setEditable 里那段注释。
            if (!this._pending.has(key)) this._pending.set(key, {deltas: [], setValue: null, editable: null});
            return this._pending.get(key);
        },
        async ensure() {
            if (this.modules) return this.modules;
            const [state, view, commands, language, py, lezerhl] = await Promise.all([
                import(CDN.state), import(CDN.view), import(CDN.commands),
                import(CDN.language), import(CDN.python), import(CDN.lezerhl),
            ]);
            this.modules = {
                EditorState: state.EditorState, Compartment: state.Compartment,
                Transaction: state.Transaction, EditorView: view.EditorView,
                lineNumbers: view.lineNumbers, keymap: view.keymap,
                highlightActiveLine: view.highlightActiveLine,
                highlightActiveLineGutter: view.highlightActiveLineGutter,
                drawSelection: view.drawSelection, history: commands.history,
                defaultKeymap: commands.defaultKeymap, historyKeymap: commands.historyKeymap,
                indentWithTab: commands.indentWithTab, indentOnInput: language.indentOnInput,
                bracketMatching: language.bracketMatching,
                syntaxHighlighting: language.syntaxHighlighting,
                defaultHighlightStyle: language.defaultHighlightStyle,
                // 深色配色表需要的两样东西
                HighlightStyle: language.HighlightStyle,
                tags: lezerhl.tags,
                python: py.python,
            };
            return this.modules;
        },
        emitChange(elementId, value) {
            try {
                // ⭐⭐ [2026-08-06 实测] 这三条分支的顺序**曾经全是错的**。
                //
                // `window.getElement(id)` 在当前 NiceGUI 版本返回的是**原生
                // HTMLDivElement**（`ui.element('div')` 没有 Vue 包装），
                // 所以 `el.$emit` 永远不存在 —— 第一条是死代码。
                // 于是每次都落到 `emitEvent`，而那是**全局**事件，
                // 对应 Python 侧的 `ui.on('cm_change')`；
                // 我们注册的却是 `code_area.on('cm_change', …)`（元素级）。
                // 两者不通 → **`_on_cm_change` 一次都没被调用过**。
                //
                // 后果链：编辑器里的改动进不了 `code_holder` → 最小化再展开
                // 用 `code_holder[0]` 重建 → 改动消失（用户报了三轮的
                //「改动存活不过最小化」）；同时 `_pending_skill["code"]` 也不更新，
                // 指纹守卫在打字部署那条路上永远比不出差异。
                //
                // 📌 判据：**"有 fallback" 不等于"能工作"。**
                //    主路是死代码时，fallback 就是唯一路径 —— 它必须自己单独成立，
                //    而不是"反正还有一条"。这条链上没有任何一处报错。
                //
                // DOM CustomEvent 才是元素级 `.on()` 真正收得到的东西。
                const el = window.getElement ? window.getElement(elementId) : null;
                if (el && el.dispatchEvent) {
                    el.dispatchEvent(new CustomEvent('cm_change', { detail: value }));
                    return true;
                }
                if (el && el.$emit) { el.$emit('cm_change', value); return true; }
                if (window.emitEvent) { window.emitEvent('cm_change', {id: elementId, value}); return true; }
                console.warn('[NanoCM] no event bridge for cm_change');
                return false;
            } catch(e) { console.error('[NanoCM] emitChange failed:', e); return false; }
        },
        async init(elementId, opts = {}) {
            const m = await this.ensure();
            const key = String(elementId);
            const mount =
                (window.getHtmlElement ? window.getHtmlElement(elementId) : null)
                || document.getElementById(String(elementId));
            if (!mount) { console.error('[NanoCM] mount not found:', elementId); return false; }
            const old = this.editors.get(key);
            if (old && old.view) old.view.destroy();
            mount.innerHTML = "";
            const readOnly = !!opts.readOnly;
            const syncToPython = !!opts.syncToPython;
            const editableCompartment = new m.Compartment();
            const readOnlyCompartment = new m.Compartment();
            const theme = m.EditorView.theme({
                "&": { height: "430px", backgroundColor: "var(--nano-panel)", color: "var(--nano-fg)",
                       fontFamily: '"Fira Code","JetBrains Mono","Consolas",monospace', fontSize: "12px" },
                ".cm-scroller": { fontFamily: '"Fira Code","JetBrains Mono","Consolas",monospace' },
                ".cm-content": { caretColor: "var(--nano-cm-caret)" },
                ".cm-cursor": { borderLeftColor: "var(--nano-cm-caret)" },
                ".cm-selectionBackground, &.cm-focused .cm-selectionBackground":
                    { backgroundColor: "rgba(var(--nano-amber-rgb), 0.20)" },
            });
            const editorEntry = { view: null, editableCompartment, readOnlyCompartment,
                                  syncToPython, syncTimer: null };
            const syncChange = () => {
                if (!editorEntry.syncToPython) return;
                clearTimeout(editorEntry.syncTimer);
                editorEntry.syncTimer = setTimeout(() => {
                    try {
                        const value = editorEntry.view.state.doc.toString();
                        window.NanoCM.emitChange(elementId, value);
                    } catch(e) { console.error('[NanoCM] sync failed:', e); }
                }, 250);
            };
            const syncExtension = m.EditorView.updateListener.of(update => {
                if (update.docChanged) syncChange();
            });
            // ── 深色语法配色表 ──────────────────────────────────────
            // 原来用的是 CM6 内置 `defaultHighlightStyle`，那是**为浅色底设计的**：
            // 注释是深灰、多个 token 亮度偏低，压在 var(--nano-panel) 上几乎看不见
            // （2026-08-03 实测："功能白费"）。
            //
            // ⚠️ 不在 CSS 层用 `.ͼb` 之类的类名去覆盖 —— 那些类名是 CM6 构建时
            // 自动生成的，字母后缀随版本变化，等于押注一个实现细节。
            // 换配色表才是这个问题的正确层次。
            //
            // 拿不到 tags/HighlightStyle 时返回 null，调用方回退到默认高亮 ——
            // 宁可颜色不理想，不要变成完全没有高亮。
            const nanoDarkHighlight = (m) => {
                if (!m.HighlightStyle || !m.tags) {
                    console.warn('[NanoCM] tags/HighlightStyle 不可用，回退默认高亮');
                    return null;
                }
                const t = m.tags;
                try {
                    return m.HighlightStyle.define([
                        // 注释是原来最惨的一个，单独调到明显能读的绿灰
                        {tag: [t.comment, t.lineComment, t.blockComment, t.docComment],
                         color: 'var(--nano-cm-comment)', fontStyle: 'italic'},
                        {tag: [t.keyword, t.controlKeyword, t.moduleKeyword],
                         color: 'var(--nano-cm-keyword)', fontWeight: '600'},
                        {tag: [t.string, t.special(t.string)], color: 'var(--nano-cm-string)'},
                        {tag: [t.number, t.bool, t.null, t.atom], color: 'var(--nano-cm-number)'},
                        {tag: [t.function(t.variableName), t.function(t.propertyName)],
                         color: 'var(--nano-cm-func)'},
                        {tag: [t.definition(t.variableName), t.definition(t.propertyName)],
                         color: 'var(--nano-cm-def)'},
                        {tag: [t.className, t.typeName, t.namespace], color: 'var(--nano-cm-type)'},
                        {tag: [t.operator, t.punctuation, t.bracket], color: 'var(--nano-cm-punct)'},
                        {tag: [t.variableName, t.propertyName], color: 'var(--nano-fg)'},
                        {tag: [t.self, t.constant(t.variableName)], color: 'var(--nano-cm-self)'},
                        {tag: [t.meta, t.annotation], color: 'var(--nano-fg-soft)'},
                        {tag: t.invalid, color: 'var(--nano-cm-invalid)'},
                    ]);
                } catch (e) {
                    console.error('[NanoCM] 深色配色表构建失败，回退默认:', e);
                    return null;
                }
            };
            const state = m.EditorState.create({
                doc: opts.doc || "",
                extensions: [
                    m.lineNumbers(), m.highlightActiveLineGutter(), m.highlightActiveLine(),
                    m.drawSelection(), m.history(), m.indentOnInput(), m.bracketMatching(),
                    // 用深色配色表，不用 CM6 内置的 defaultHighlightStyle。
                    // 后者是为浅色底设计的，压在 var(--nano-panel) 上时注释和多个 token
                    // 是深字压深底、几乎看不见。
                    // fallback:true 保留：万一 tags 拿不到，至少还有默认高亮，
                    // 不会退化成完全没颜色。
                    m.python(), m.syntaxHighlighting(nanoDarkHighlight(m) || m.defaultHighlightStyle,
                                                     {fallback: true}),
                    m.keymap.of([m.indentWithTab, ...m.defaultKeymap, ...m.historyKeymap]),
                    m.EditorView.lineWrapping, theme, syncExtension,
                    readOnlyCompartment.of(m.EditorState.readOnly.of(readOnly)),
                    editableCompartment.of(m.EditorView.editable.of(!readOnly)),
                ],
            });
            editorEntry.view = new m.EditorView({ state, parent: mount });
            this.editors.set(key, editorEntry);
            // flush pending ops
            const pend = this._pending.get(key);
            if (pend) {
                if (pend.setValue !== null) {
                    this.setValue(elementId, pend.setValue);
                } else if (pend.deltas.length > 0) {
                    for (const d of pend.deltas) this.append(elementId, d);
                }
                // ⭐ 内容之后再补可编辑性 —— setEditable 在 editable 时会
                // emitChange 一次做初始同步，放在内容就位之后才同步得对。
                if (pend.editable !== null) this.setEditable(elementId, pend.editable);
                this._pending.delete(key);
            }
            return true;
        },
        append(elementId, delta) {
            const key = String(elementId);
            const item = this.editors.get(key);
            if (!item || !item.view) {
                this._getPending(key).deltas.push(delta);
                return false;
            }
            if (!delta) return false;
            const m = this.modules;
            const view = item.view;
            const end = view.state.doc.length;
            view.dispatch({ changes: {from: end, insert: delta},
                            selection: {anchor: end + delta.length}, scrollIntoView: true,
                            annotations: m.Transaction.addToHistory.of(false) });
            return true;
        },
        setValue(elementId, value) {
            const key = String(elementId);
            const item = this.editors.get(key);
            if (!item || !item.view) {
                const pend = this._getPending(key);
                pend.setValue = value || "";
                pend.deltas = [];  // discard partial deltas, setValue wins
                return false;
            }
            const m = this.modules;
            const view = item.view;
            const text = value || "";
            view.dispatch({ changes: {from: 0, to: view.state.doc.length, insert: text},
                            selection: {anchor: text.length}, scrollIntoView: true,
                            annotations: m.Transaction.addToHistory.of(false) });
            return true;
        },
        setEditable(elementId, editable) {
            const item = this.editors.get(String(elementId));
            if (!item || !item.view) {
                // ⭐⭐ [2026-08-06 实测] 编辑器还没建好 → **排队**，不能丢。
                //
                // `setValue` 和 `append` 早就有这个兜底，只有 `setEditable`
                // 直接 `return false` —— 一个静默的永久丢失。
                //
                // 后果就是用户报的「代码框改不了 / 改动不回传」：
                // 生成结束时的收尾是 `_cm_set_value(...)` + `_cm_set_editable(True)`，
                // 如果此刻 `ensure()` 还在 await CDN 上的 CodeMirror 模块，
                // 前者进队列、稍后被应用（**所以代码是有的**），
                // 后者被扔掉（**所以框是只读的、syncToPython 还是 false**）。
                // 内容在、编辑不了 —— 看起来完全不像一个竞态。
                //
                // ⚠️ 而 CodeMirror 是 `import(CDN.…)` 动态加载的，
                //    所以"init 有没有赶在收尾之前完成"直接取决于网速 ——
                //    这正是那种"有时能改有时不能"的来源。
                //
                // 📌 判据：**同一批操作里，只要有一个能排队，其余就都得能排队。**
                //    异步初始化面前，"没就绪就放弃"和"没就绪就排队"混用，
                //    等于让一部分状态随机丢失。
                this._getPending(String(elementId)).editable = !!editable;
                return false;
            }
            const m = this.modules;
            item.view.dispatch({ effects: [
                item.readOnlyCompartment.reconfigure(m.EditorState.readOnly.of(!editable)),
                item.editableCompartment.reconfigure(m.EditorView.editable.of(!!editable)),
            ]});
            item.syncToPython = !!editable;
            if (editable) {
                try {
                    const value = item.view.state.doc.toString();
                    window.NanoCM.emitChange(elementId, value);
                } catch(e) { console.error('[NanoCM] initial sync failed:', e); }
            }
            return true;
        },
        getValue(elementId) {
            const item = this.editors.get(String(elementId));
            if (!item || !item.view) return "";
            return item.view.state.doc.toString();
        },
        destroy(elementId) {
            const key = String(elementId);
            const item = this.editors.get(key);
            if (item && item.view) item.view.destroy();
            this.editors.delete(key);
            return true;
        },
    };
})();
</script>
''')

        # body-level model tooltip（不能用 ::after 伪元素：会被 Quasar
        # QMenu/QVirtualScroll 的 paint context 困住，无法画到菜单右侧外部）
        # ══════════════════════════════════════════════════════════════════
        # 聊天区四件事的浏览器侧：
        #   ① 选中文字 → 右键 replay（引用）
        #   ② 把文件拖进窗口 → 走 composer 那条上传通道
        #   ③ `nano-file:` 链接：左键打开 / 右键在文件夹中显示
        #   ④ 代码块右上角的复制按钮
        # ⚠️ 全部走**事件委托 + MutationObserver**，因为聊天内容是流式插进来的：
        #    📌 一次性给现有节点绑事件，只能覆盖「绑的那一刻已经存在」的那些。
        # ⚠️ 这一整块的前提是 native 窗口已经能选中文字（v1.59 的 `text_select`）——
        #    ① 在那之前**根本不可能存在**。
        # ══════════════════════════════════════════════════════════════════
        ui.add_body_html(r'''<script>
(function() {
  if (window.__nanoU3Installed) return;
  window.__nanoU3Installed = true;

  const CHAT = '.nano-chat-scroll';
  function emit(name, payload) {
    if (window.emitEvent) window.emitEvent(name, payload);
  }

  // ── 复制：一份实现，代码框按钮和右键菜单共用 ─────────────────────
  // ⚠️ WebView2 下 clipboard API 不一定可用（非安全上下文/权限）→ execCommand 兜底。
  //    📌 同 `cm_change` 那条教训：**有 fallback 不等于能工作**，两条都得自己成立。
  function legacyCopy(text, done) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:-1000px;opacity:0;';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      if (done) done();
    } catch (err) { console.warn('[NanoU3] copy failed', err); }
  }
  function copyText(text, done) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function() { if (done) done(); },
        function() { legacyCopy(text, done); });
    } else { legacyCopy(text, done); }
  }
  // ⭐ [2026-08-23] 把这两个暴露出去，给可视化块的「复制代码」用。
  //    📌 这份实现带 `execCommand` 兜底，而 WebView2 下裸 clipboard API
  //       **会静默失败** —— 上面那条注释早就写了。
  //       与其在 Python 侧再写一遍半成品，不如把这一份共用出去。
  window.__nanoCopy = copyText;
  window.__nanoToast = toast;   // 函数声明会提升，toast 定义在后面也没关系
  // 复制图标：两个叠起来的圆角方块（各家右键菜单的通用形状）。
  // ⚠️ 用**内联 SVG** 而不是字体图标/Unicode 字符：
  //    📌 字形宽度和基线由字体决定 —— 换个字体回退它就会歪，
  //       而这个按钮只有一个图标，歪一点就很明显（同 pill 那个点的教训：
  //       换字符，别调偏移量；这里更进一步 —— 干脆别用字符）。
  const ICON_COPY =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="11" height="11" rx="2"></rect>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
  const ICON_OK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M20 6 9 17l-5-5"></path></svg>';

  function toast(msg) {
    let el = document.getElementById('nano-mini-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'nano-mini-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(window.__nanoToastT);
    window.__nanoToastT = setTimeout(function() { el.classList.remove('on'); }, 1100);
  }

  // ── 通用小菜单（选中引用 / 文件链接右键 共用一个）─────────────────
  let menuEl = null;
  function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
  function openMenu(x, y, items) {
    closeMenu();
    menuEl = document.createElement('div');
    menuEl.className = 'nano-ctx-menu';
    items.forEach(function(it) {
      const b = document.createElement('div');
      b.className = 'nano-ctx-item';
      // ⚠️ 图标位允许是 SVG 也允许是字符：复制用 SVG（要和代码框那个一致），
      //    其余用字符就够。**标签保留** —— 这是菜单，没有标签的菜单项没法用。
      b.innerHTML = '<span class="nano-ctx-ico">' + it.icon + '</span>' +
                    '<span>' + it.label + '</span>';
      b.addEventListener('mousedown', function(ev) {
        ev.preventDefault(); ev.stopPropagation();
        closeMenu(); it.run();
      });
      menuEl.appendChild(b);
    });
    document.body.appendChild(menuEl);
    // 贴边翻转：菜单不能被窗口边缘切掉
    const r = menuEl.getBoundingClientRect();
    const px = (x + r.width > window.innerWidth - 6) ? x - r.width : x;
    const py = (y + r.height > window.innerHeight - 6) ? y - r.height : y;
    menuEl.style.left = Math.max(6, px) + 'px';
    menuEl.style.top = Math.max(6, py) + 'px';
  }
  document.addEventListener('mousedown', function(e) {
    if (menuEl && !menuEl.contains(e.target)) closeMenu();
  }, true);
  document.addEventListener('scroll', closeMenu, true);
  window.addEventListener('blur', closeMenu);

  // ── ① 选中文字 → 右键 replay ────────────────────────────────────
  //    ③ 的文件链接右键也在这里分流。
  document.addEventListener('contextmenu', function(e) {
    const link = e.target.closest && e.target.closest('a.nano-file-link');
    if (link) {
      e.preventDefault();
      const p = link.dataset.path || '';
      openMenu(e.clientX, e.clientY, [
        {icon: '\u2197', label: '\u6253\u5f00',
         run: function() { emit('nano_open_file', {path: p}); }},
        {icon: '\u{1F4C1}', label: '\u5728\u6587\u4ef6\u5939\u4e2d\u663e\u793a',
         run: function() { emit('nano_reveal_file', {path: p}); }},
        {icon: ICON_COPY, label: '\u590d\u5236\u8def\u5f84',
         run: function() { copyText(p, function() { toast('\u5df2\u590d\u5236\u8def\u5f84'); }); }}
      ]);
      return;
    }
    const inChat = e.target.closest && e.target.closest(CHAT);
    if (!inChat) return;
    const sel = window.getSelection();
    const txt = sel ? String(sel).trim() : '';
    if (!txt) return;
    e.preventDefault();

    // ⭐⭐ [2026-08-22] **「回复」是白名单：只有 Nano 自己说的话能回复。**
    //    「如果这句话是模型自己说的 = 可以回复」。
    //    🔴 此前的条件是「在聊天区里」—— 于是系统报错、工具卡、
    //       连用户自己刚发的那句都能「回复」，而「回复一条系统报错」
    //       对模型是一句没有意义的话。
    //
    // ⚠️ 判的是**整段选中都在里面**（用 `commonAncestorContainer`），
    //    不是「起点在里面」：一段从 Nano 的话拖到下面系统报错里的选中，
    //    「回复这段」指的到底是哪一条？📌 **一个答不上来的引用，
    //    不如不给** —— 而起点判法会让它安静地变成「引用了一半」。
    // ⚠️ `复制` **不受这条限制**：复制对任何文字都有意义，
    //    📌 两个动作的适用范围本来就不同，别因为它们在同一个菜单里就一起收紧。
    var _node = null;
    try {
      var _r = sel.rangeCount ? sel.getRangeAt(0) : null;
      _node = _r ? _r.commonAncestorContainer : null;
      if (_node && _node.nodeType === 3) _node = _node.parentElement;
    } catch (err) { _node = null; }
    const saidByNano = !!(_node && _node.closest && _node.closest('.nano-said'));

    const items = [
      // ⚠️ 复制排第一：选中文字之后最常做的就是复制，而回复是这个项目
      //    特有的动作。📌 菜单顺序该按「用户多半想干什么」排，不按「我们新做了什么」排。
      {icon: ICON_COPY, label: '复制', run: function() {
        copyText(txt, function() { toast('已复制'); });
        if (sel && sel.removeAllRanges) sel.removeAllRanges();
      }}
    ];
    if (saidByNano) {
      items.push({icon: '↳', label: '回复', run: function() {
        emit('nano_quote_selection', {text: txt});
        if (sel && sel.removeAllRanges) sel.removeAllRanges();
      }});
    }
    openMenu(e.clientX, e.clientY, items);
  });

  // ── ①b 输入框的右键菜单 ────────────────────────────────────────
  // 🔴 native 窗口**没有原生右键菜单**（frameless + WebView2 不给），
  //    于是 composer 里只能 Ctrl+V —— 实测报的就是这个。
  // 📌 一个「只有快捷键能做」的操作，对不知道快捷键的人等于不存在。
  // ⚠️ 粘贴必须走 `document.execCommand('insertText')` 而不是直接改 `.value`：
  //    Quasar 的 q-input 绑的是 Vue 的 v-model，**只认 input 事件**；
  //    直接赋值能看见字，但按发送时后端拿到的还是旧值。
  //    📌 本项目栽过同形状的（`cm_change` 那次）：**界面变了不等于状态变了。**
  function editableTarget(el) {
    if (!el) return null;
    const t = el.closest && el.closest('textarea, input[type=text], [contenteditable="true"]');
    return t || null;
  }
  function insertAtCursor(el, text) {
    if (!text) return;
    el.focus();
    let ok = false;
    try { ok = document.execCommand('insertText', false, text); } catch (err) {}
    if (!ok) {
      // 兜底：手动拼 + 派发 input，让 Vue 知道值变了
      const s = el.selectionStart || 0, e = el.selectionEnd || 0;
      const v = el.value || '';
      el.value = v.slice(0, s) + text + v.slice(e);
      const pos = s + text.length;
      try { el.setSelectionRange(pos, pos); } catch (err2) {}
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }
  }
  document.addEventListener('contextmenu', function(e) {
    const el = editableTarget(e.target);
    if (!el) return;
    e.preventDefault();
    const sel = (el.value || '').slice(el.selectionStart || 0, el.selectionEnd || 0);
    const items = [];
    if (sel) {
      items.push({icon: ICON_COPY, label: '\u590d\u5236',
                  run: function() { copyText(sel, function() { toast('\u5df2\u590d\u5236'); }); }});
      items.push({icon: '✂', label: '剪切', run: function() {
        // ⚠️ 删选区必须走 execCommand('delete')：它会像用户真按了退格一样
        //    派发 input 事件，Vue 的 v-model 才跟得上。手改 .value 只改显示。
        copyText(sel, null);
        el.focus();
        try { document.execCommand('delete'); } catch (err) {}
      }});
    }
    items.push({icon: '\u{1F4CB}', label: '\u7c98\u8d34', run: function() {
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function(txt) { insertAtCursor(el, txt); },
          function() { toast('\u8bfb\u4e0d\u5230\u526a\u8d34\u677f\uff0c\u8bf7\u7528\u0020\u0043\u0074\u0072\u006c\u002b\u0056'); });
      } else {
        toast('\u8bfb\u4e0d\u5230\u526a\u8d34\u677f\uff0c\u8bf7\u7528\u0020\u0043\u0074\u0072\u006c\u002b\u0056');
      }
    }});
    items.push({icon: '\u2261', label: '\u5168\u9009', run: function() {
      el.focus(); try { el.select(); } catch (err) {}
    }});
    openMenu(e.clientX, e.clientY, items);
  }, true);

  // ── ③ 文件链接左键 ─────────────────────────────────────────────
  document.addEventListener('click', function(e) {
    const link = e.target.closest && e.target.closest('a.nano-file-link');
    if (!link) return;
    e.preventDefault();
    emit('nano_open_file', {path: link.dataset.path || ''});
  });

  // 外部链接：交给系统浏览器。
  // 🔴 Nano 自己就是个 WebView —— markdown 渲染出的 <a href="https://…">
  //    会直接让它导航走，而这个窗口没有后退按钮 ⇒ 用户被困在网页里出不来
  //。
  // ⚠️ 用捕获阶段（第三参数 true）：要赶在别的处理器之前拿到这次点击。
  // ⚠️ 只认 http/https —— 站内锚点、nano-file: 等一概不管，
  //    📌 一个拦截器管的范围越窄，越不会误伤。
  document.addEventListener('click', function(e) {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    if (!/^https?:\/\//i.test(href)) return;
    e.preventDefault();
    emit('nano_open_url', {url: href});
  }, true);

  const FPREFIX = 'nano-file:';
  function decorateLinks(root) {
    root.querySelectorAll('a[href^="nano-file:"]:not(.nano-file-link)').forEach(function(a) {
      let raw = a.getAttribute('href').slice(FPREFIX.length);
      try { raw = decodeURIComponent(raw); } catch (err) {}
      a.classList.add('nano-file-link');
      a.dataset.path = raw;
      a.setAttribute('title',
        raw + '\n\u5de6\u952e\u6253\u5f00 \u00b7 \u53f3\u952e\u5728\u6587\u4ef6\u5939\u4e2d\u663e\u793a');
      // ⚠️ 这里**刻意不加图标、不加边框** —— 「它要放在一个段落里面的啊，
      //    你怎么做成按钮了啊」。
      //    📌 行内元素的样式预算比块级小得多：一个句子中间的东西只能改**颜色**，
      //       一旦加上边框和内边距，它就会把整行的行高和节奏都撑歪。
    });
  }

  // ── ④ 代码块右上角的复制按钮 ───────────────────────────────────
  function decorateCode(root) {
    root.querySelectorAll('pre:not([data-nano-copy])').forEach(function(pre) {
      if (pre.closest('.nano-cm-host')) return;   // CodeMirror 自己那些 pre 不动
      pre.setAttribute('data-nano-copy', '1');
      pre.classList.add('nano-codeblock');
      const btn = document.createElement('button');
      btn.className = 'nano-copy-btn';
      btn.type = 'button';
      // 「你怎么用的『复制』那俩字啊」——📌 一个只做一件事、
      //    位置固定在角上的按钮，图标已经说清楚了；两个字反而在抢代码的注意力。
      btn.innerHTML = ICON_COPY;
      btn.title = '\u590d\u5236';
      btn.addEventListener('click', function(ev) {
        ev.preventDefault(); ev.stopPropagation();
        const code = pre.querySelector('code');
        const text = (code ? code.innerText : pre.innerText) || '';
        function done() {
          btn.innerHTML = ICON_OK;
          btn.classList.add('ok');
          setTimeout(function() {
            btn.innerHTML = ICON_COPY; btn.classList.remove('ok');
          }, 1400);
        }
        copyText(text, done);   // 与右键菜单共用同一份实现
      });
      pre.appendChild(btn);
    });
  }

  function sweep() {
    const root = document.querySelector(CHAT) || document.body;
    decorateLinks(root);
    decorateCode(root);
  }
  const mo = new MutationObserver(function() {
    clearTimeout(window.__nanoU3T);
    window.__nanoU3T = setTimeout(sweep, 60);   // 流式期间别每个 token 扫一遍
  });
  mo.observe(document.body, {childList: true, subtree: true});
  sweep();

  // ── ② 拖文件进窗口 → 喂给 composer 那个隐藏 upload ────────────────
  // 刻意走「把 File 塞进那个 <input> 再 dispatch change」，而不是自己发一份到后端：
  // 要的是走同一条路，不是再实现一遍。图片的回看、临时文件注册、附件角标
  // 全都挂在那个 handler 上。
  let dragDepth = 0;
  function overlay() {
    let el = document.getElementById('nano-drop-overlay');
    if (!el) {
      el = document.createElement('div');
      el.id = 'nano-drop-overlay';
      // 这里不需要一句话，四个字就够 ——
      // 📌 遮罩是个**状态提示**，不是说明书；用户手上正拖着文件，知道要干嘛。
      el.innerHTML = '<div class="nano-drop-box">\u6dfb\u52a0\u9644\u4ef6</div>';
      document.body.appendChild(el);
    }
    return el;
  }
  function isKb(target) {
    return !!(target && target.closest && target.closest('.kb-upload'));
  }
  function hasFiles(e) {
    const dt = e.dataTransfer;
    if (!dt || !dt.types) return false;
    for (const ty of dt.types) { if (ty === 'Files') return true; }
    return false;
  }
  document.addEventListener('dragenter', function(e) {
    if (!hasFiles(e) || isKb(e.target)) return;
    dragDepth++;
    overlay().classList.add('on');
  });
  document.addEventListener('dragover', function(e) {
    if (!hasFiles(e) || isKb(e.target)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('dragleave', function(e) {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay().classList.remove('on');
  });
  document.addEventListener('drop', function(e) {
    dragDepth = 0;
    overlay().classList.remove('on');
    if (!hasFiles(e) || isKb(e.target)) return;   // 知识库那块保持原行为
    e.preventDefault();
    const files = e.dataTransfer.files;
    if (!files || !files.length) return;
    const input = document.querySelector('.nano-chat-upload input[type=file]');
    if (!input) { console.warn('[NanoU3] chat upload input not found'); return; }
    try {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      input.files = dt.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    } catch (err) { console.warn('[NanoU3] drop failed', err); }
  });

  // ── 粘贴通道（Ctrl+V 贴图片/文件）─────────────────────────
  // ⭐ 走的是**和拖拽同一条路**：塞进同一个 input + dispatch change
  //    ⇒ 同一个 `_handle_chat_upload`，图片回看 / 临时文件注册 / 附件角标全都白送。
  //    📌 拖拽那段的注释已经写明：另起一条必然漏掉其中几样。
  // ⚠️ `paste` 是标准 DOM 事件，**不需要 clipboard API 权限**
  //    （那是 navigator.clipboard.read() 才要的，WebView2 下不一定可用）。
  document.addEventListener('paste', function(e) {
    const cd = e.clipboardData;
    if (!cd) return;
    // ⚠️ 只在剪贴板里**真有文件**时接管。纯文字粘贴一个字都不能碰 ——
    //    拦错了就是把最常用的操作弄坏了。
    const files = [];
    for (const it of (cd.items || [])) {
      if (it.kind === 'file') { const f = it.getAsFile(); if (f) files.push(f); }
    }
    if (!files.length) return;
    if (isKb(e.target)) return;                 // 知识库那块保持原行为（同拖拽）
    const input = document.querySelector('.nano-chat-upload input[type=file]');
    if (!input) { console.warn('[NanoU3] chat upload input not found'); return; }
    try {
      const dt = new DataTransfer();
      for (const f of files) {
        // ⚠️ 截图粘贴出来的 File 名字是空的、或都叫 image.png ——
        //    不改名的话连续贴两张会在临时目录里互相覆盖（dest = tmp_dir / filename）。
        let name = f.name || '';
        if (!name || /^image\.(png|jpe?g|webp)$/i.test(name)) {
          const ext = (f.type && f.type.split('/')[1]) || 'png';
          const t = new Date(), pad = n => String(n).padStart(2, '0');
          name = 'paste_' + t.getFullYear() + pad(t.getMonth() + 1) + pad(t.getDate())
               + '_' + pad(t.getHours()) + pad(t.getMinutes()) + pad(t.getSeconds())
               + '_' + Math.floor(Math.random() * 1000) + '.' + ext;
        }
        dt.items.add(new File([f], name, {type: f.type || 'application/octet-stream'}));
      }
      input.files = dt.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
      // ⚠️ 只在**没有文字**时才拦默认行为：截图工具常常同时放图片和一段文本，
      //    那时接管文件、让文字照常贴进输入框才是对的。
      if (!(cd.getData && cd.getData('text/plain'))) e.preventDefault();
    } catch (err) { console.warn('[NanoU3] paste failed', err); }
  });
})();
</script>''')


        ui.timer(0.5, self._consume_skill_refresh_request)

        # ── 未决交互卡片的唯一驱动 ────────────────────────
        # **level-triggered**：每次重新读 `interaction` 表算一遍，
        # 不依赖任何"状态变了要记得通知 UI"的回调 —— 那正是要避免的
        # （SQLite 是权威，事件队列只是 refresh hint，丢光也不影响正确性）。
        # 函数内部有内容指纹短路，没变化时不碰 DOM。
        # 1.5 秒：比健康卡慢一点就够，待办不是毫秒级的东西。
        ui.timer(1.5, self.refresh_pinned_interactions)
        # 后台任务：pill / 角标 / 抽屉内容。
        # ⚠️ 2s 而不是 1.5s 是刻意错开的：📌 两个同周期的定时器会永远在同一帧
        #    里一起跑，把偶发的卡顿叠成必然的卡顿。
        ui.timer(2.0, self._refresh_tasks_panel)
        # ⭐⭐ **轮外 UI 事件**（Subagent跨过它那一轮之后发的授权请求）。
        #    0.2s：它是一条要**人来回应**的通道，延迟直接变成用户等待。
        #    ⚠️ 轮询本身极廉价（正常情况下队列是空的）——
        #       📌 而它是Subagent唯一能要到弹窗的路：没有它，Subagent会在确认闸上
        #          干等 300 秒，而屏幕上什么都不会发生（实测 2026-08-20）。
        # ⚠️ **在这里建**（UI 构建期，事件循环已经在了）——
        #    📌 `asyncio.Queue()` 在 3.10 里不再绑定循环，但消费它的 timer 在这里，
        #       建在同一处才不会出现「队列有了、没人读」的窗口。
        try:
            self._oob_events = asyncio.Queue()
            self.agent._ui_oob_events = self._oob_events
        except Exception as _e_oob:
            logger.error(f"[OOB] 轮外 UI 通道没建起来 —— "
                         f"Subagent的授权弹窗将无法呈现: {_e_oob}")
        ui.timer(0.2, self._drain_oob_events)

        # ── 健康登记表的唯一 UI 消费者 ────────────────────────────────
        # 1 秒一跳。轮询本身极廉价（drain 一个 SimpleQueue，正常情况下空转），
        # 但它是"事件循环存在之前发生的故障"唯一能被看见的通道——RAG 初始化线程
        # 在 WebUI() 构造时就启动了，比 ui.run() 还早，callback 那条路走不通。
        ui.timer(1.0, self._health_consumer_tick)
        # 上一个进程的崩溃留痕，启动后展示一次（segfault / os._exit 靠 breadcrumb 留痕）
        ui.timer(2.5, self._crash_journal_tick, once=True)
        # ⭐ 重启后问一句「那几件没做完的要不要重做」。
        #    ⚠️ 排在崩溃留痕之后：上次真崩了的话，用户该**先**看到那条系统提示，
        #       再听 Nano 说话。📌 系统陈述事实在前、Nano 开口在后 ——
        #       顺序反了会让人以为 Nano 在替系统解释。
        # ⭐ **排在 `_startup_resume_offer` 前面** ——
        #    📌 「你上次还有话没说完」应该出现在「要不要接着做那件活」**之前**：
        #       前者是事实回放，后者是基于事实的提问。顺序反了，Nano 会在
        #       用户还没看到自己那条消息时就先问要不要继续。
        ui.timer(2.5, self._startup_present_unsent, once=True)
        ui.timer(4.0, self._startup_resume_offer, once=True)
        # ── 预算状态的定期同步 ────────────────────────────────────────
        # 这是能力恢复探针最简单的一个实例：预算按日重置，重算一次即可。
        # 没有它的话，跨过 0 点后 HealthRegistry 里的故障卡片不会消失——
        # 因为 assert_budget_ok 只在【有人真的发起模型调用时】才跑，
        # 而硬上限恰恰把所有模型调用都挡住了（这就是那个恢复死锁）。
        # 20 秒一跳：只读两个小 JSON，比 1 秒跳廉价得多，而预算不是毫秒级的东西。
        ui.timer(20.0, self._budget_health_tick)
        # ── 能力探针（解开遗留的恢复检测死锁）──────────────────────
        # 死锁：能力坏了 → 工具被下架 → 没人再用它 → report_ok 永远不触发 → 永不恢复。
        # 用户把问题修好了（重连网络、装上 Tesseract、换了好的 data/ 目录），Nano 也不知道。
        # 这个外部时钟不依赖任何业务路径，是唯一能打破闭环的东西。
        # 15 秒一跳只是"看看有没有到点的"，真正的探针带指数退避（30s→10min 封顶），
        # 所以长期不可用的能力不会被反复打扰。
        ui.timer(15.0, self._capability_probe_tick)

        # asyncio 的异常处理器要等事件循环真的起来才能挂（和 install_hooks 分开的原因）
        def _install_loop_handler():
            try:
                import asyncio as _a
                from core import crash_journal as _cj
                _cj.install_asyncio_handler(_a.get_running_loop())
            except Exception as e:
                logger.debug(f"[CrashJournal] asyncio handler 未安装: {e}")
        ui.timer(0.05, _install_loop_handler, once=True)

        # canary 自检定时器——每5分钟探一次"现在要不要跑"，真正要不要跑
        # （距上次够久 + 当前没有OS任务在执行）由 maybe_run_canary 内部判断，
        # 这里只是个轻量的"有空就喊一声"触发源，不用纠结这个5分钟间隔本身。
        async def _maybe_run_canary():
            try:
                await self.agent.maybe_run_canary()
            except Exception as e:
                logger.warning(f"[Canary] 定时触发异常（不影响主流程）: {e}")
        ui.timer(300, _maybe_run_canary)

        # 主动开口：感知钩子照常启动；旧 speaker 已停用（新主动智能引擎接管 L0/L1/L2）。
        # 旧 speaker 会往对话历史插 assistant 消息，曾导致 thinking 块被合并改动而 400 崩溃，
        # 且它发的是"宠物式"陪伴话——正是这次要替换掉的。下面两个旧 timer 已注释停用。
        _start_proactive_hooks()
        # async def _startup_holiday():
        #     await gui._speaker.check_holiday_on_startup()
        # ui.timer(1, _startup_holiday, once=True)
        # async def _maybe_speak():
        #     try:
        #         await gui._speaker.maybe_speak()
        #     except Exception as e:
        #         logger.warning(f"[Proactive] 轮询异常: {e}")
        # ui.timer(300, _maybe_speak)
        # 主动智能 v0：每 60s tick 一次（默认 SHADOW，只记日志不打扰）。
        # 上线时：把 core/proactive/intel/engine.py 的 SHADOW_MODE 改 False，
        # 并把上面旧 _speaker 的 check_holiday/_maybe_speak 两个 timer 停掉（新引擎已含 L1 日历）。
        async def _intel_tick():
            try:
                await gui._intel_engine.tick()
            except Exception as e:
                logger.warning(f"[Intel] tick 异常: {e}")
        # shadow 期 20s 一跳，多采样决策面、加速攒数据；上线后可调回 60s。
        ui.timer(20, _intel_tick)
        # CPU 采样：每60秒取一次 psutil 均值
        import psutil as _psutil
        def _cpu_sample():
            try:
                pct = _psutil.cpu_percent(interval=None)
                _get_activity_buffer().on_cpu_sample(pct)
            except Exception:
                pass
        ui.timer(60, _cpu_sample)

        # Ambient Memory Phase 4：每 ~4 分钟把当前现场追加进持久轨迹（跨会话/重启留存）
        def _ambient_trail_tick():
            try:
                self.agent.record_ambient_trail()
            except Exception:
                pass
        ui.timer(240, _ambient_trail_tick)

        # 挂起/等待：定时唤醒轮询。轮询本身只是本地 SQLite 查询（廉价），
        # 真正的 LLM 成本只在某条定时挂起到点、驱动唤醒 turn 时才发生——
        # 到点与否由模型当初设的 timer_seconds 决定（已在 wait_for 描述里按
        # "缓存窗口/隔多久值得回看一次"引导）。5 秒一轮给足响应度。
        async def _suspension_tick():
            try:
                await gui._suspension_poll_tick()
            except Exception as e:
                logger.warning(f"[Suspension] tick 异常: {e}")
            # ⭐⭐⭐ [2026-08-09 实测] **等待 pill 的收尾挂进 tick。**
            #
            # 🔴 实测问题：Nano 自己调 `cancel_wait` 之后 pill 不收 ——
            #    因为收 pill 这件事原来**只挂在两条路径上**（UI 的取消按钮 /
            #    用户发新消息），而模型那条 `cancel_wait` 在 orchestrator 里，
            #    它**改了权威却没有 UI 通道**。
            #
            # ⚠️⚠️ 而 `app.py:7443` 那段注释写的正是这个判据 ——
            #    「这一处**差点漏掉**……📌 **镜像点要按「权威被改动的地方」去找，
            #      不是按模块去找**」。当时把**镜像**点数全了，
            #    却没对**pill** 问同一个问题。
            #    📌 **一条判据只被用在它诞生的那个问题上，等于没立。**
            #
            # ⭐ 所以修法**不是**给 `cancel_wait` 补发一个事件（那只修这一条路径），
            #    而是把这个**本来就是 level-triggered** 的收尾接到时钟上：
            #    它逐条回查权威、只收真的不在 active 里的。
            #    📌 **level-triggered 的收尾对「以后又多一条取消路径」免疫，
            #       edge-triggered 的补发只修当前这一条。**
            #    ⭐ 与 `reconcile_tick` 同一个形状（那条也曾经「写好了没人调」）。
            try:
                gui._settle_all_waiting_pills()
            except Exception as e:
                logger.debug(f"[Suspension] pill 收尾 tick 异常（忽略）: {e}")
        ui.timer(5, _suspension_tick)

        # ⭐⭐ Runtime Reconciler 的周期 tick（2026-08-07 补接）。
        #
        # ⚠️⚠️ **这条以前根本没有人调。** 全项目只调过 `reconcile_on_startup`，
        # 而 `reconcile_tick` 从早先落地起就没有任何生产调用方 ——
        # 也就是说**整个 level-triggered 层在真实运行中一直是死的**：
        #   · 早先给澄清设的 2 小时 TTL（`interaction.expire_tick`）从没生效过；
        #     真库里能查到好几条早已过期的记录，它们全是被**别的路径**
        #     （草稿消化 → SUPERSEDED）顺手收掉的 —— 只是运气好掩盖了这个洞。
        #   · action lease 过期回收（`_reclaim`）同样从没跑过。
        #   · 早先的兜底回收（不死挂起的最后一道防线）如果不接这条，
        #     写得再对也永远不会执行。
        #
        # 📌 判据：**"level-triggered" 不是一种写法，是一份契约 ——
        #    它要求有人真的在按周期推它。** 只写收敛逻辑、不接时钟，
        #    等于把一堆"迟早会自愈"的承诺变成永不兑现。
        #    ⚠️ 新增任何 `register_tick_step()` 时，先确认这条时钟还在。
        #
        # 频率跟挂起轮询对齐（5 秒）：它自称"廉价：几条索引扫描，不加载任何模型"，
        # 实测每跳只读几张小表。
        def _runtime_reconcile_tick():
            try:
                from core.runtime import get_kernel as _gk
                from core.runtime import reconcile_tick as _rt_tick
                rep = _rt_tick(_gk())
                # 只在真做了事的时候出声，否则 5 秒一条日志会把 cmd 淹掉
                if rep.did_anything or rep.extra:
                    logger.info(f"[Runtime] tick：{rep.summary()} extra={rep.extra}")
            except Exception as e:
                # 收敛失败不能影响 UI —— 它是修脏状态的，不是必需路径
                logger.warning(f"[Runtime] reconcile tick 异常（忽略本跳）: {e}")
        ui.timer(5, _runtime_reconcile_tick)

        # ⭐⭐ 接管状态条的驱动源。**1 秒，不复用上面那个 5 秒的 tick。**
        #
        # ⚠️ 5 秒对"瞬发"来说太慢了：用户动手到界面出现提示最坏要等 5 秒，
        #    那正是 用户说的"过一会不知道啥时候突然触发"。
        # 📌 判据：**收敛的节奏和展示的节奏是两件事，不该共用一个 timer。**
        #    收敛（修脏状态）慢一点没关系；展示（让人知道现在什么情况）不行。
        #
        # ⚠️ 这里刻意用**轮询重画**而不是"接管时推一次事件"：
        #    推事件要求"每个改状态的地方都记得推" —— 那正是本轮反复栽的形状
        #    （`_os_task_busy` 靠所有调用点记得配对、`reconcile_tick` 压根没人调）。
        #    轮询重画丢一跳只是晚 1 秒，丢一个事件是永久错位。
        # ⚠️ 代价核过：每跳一次 SQLite 只读查询（`current_activity`），
        #    与 UI 的其他 1 秒级 timer 同量级。
        def _takeover_bar_tick():
            try:
                gui._refresh_takeover_bar()
            except Exception as e:
                logger.debug(f"[A3] 接管状态条重画失败（忽略本跳）: {e}")
        ui.timer(1, _takeover_bar_tick)

        # 重启恢复：把上次会话遗留的 active 挂起在聊天区补一条提示，
        # 让用户知道 Nano 重启后仍记得在等什么（定时源由上面的 poller 接管，
        # 用户源在用户下次说话时由 orchestrator 自动恢复）。
        async def _restore_suspensions():
            try:
                from core.runtime.kernel import get_kernel
                from core.runtime import waitcond as _wc
                _left = _wc.list_live(get_kernel(), oldest_first=True)
            except Exception:
                _left = []
            if _left:
                _txt = "、".join(r.reason for r in _left)
                # 这句话从产品上线至今【从未真正显示过】：它在启动 2 秒后跑，那一刻
                # _last_reply_inner_col 必定为 None，旧 _proactive_push 开头就 return 了。
                # 走统一出口后，容器和 client 上下文都不再是前提。
                await gui._proactive_push(f"（我重启前还挂着在等：{_txt}。需要的话直接跟我说一声就能接着来。）")
        ui.timer(2, _restore_suspensions, once=True)

        # MCP：启动时加载 config + 连接 enabled server。连接在各自 worker task 里
        # 异步进行，不阻塞启动；连不上的走重连/needs_auth，不影响其它能力。MCP 工具属于
        # "Nano 自身能力"，连上后自动进主决策 manifest（_build_skills_info 全量注入）。
        async def _mcp_startup():
            try:
                from core.mcp_client import get_mcp_manager
                _mgr = get_mcp_manager()
                if _mgr.available:
                    _mgr.load_config()
                    await _mgr.connect_enabled()
            except Exception as e:
                logger.warning(f"[MCP] 启动初始化失败（跳过）: {e}")
        ui.timer(1.5, _mcp_startup, once=True)
        # 互联网检索卡片：周期反映当前联网类 MCP 能力（fetch 等连上→ONLINE，关掉→OFFLINE）
        ui.timer(3, self._update_net_status)

        # ── 初始化中遮罩 ──────────────────────────────────────────────
        # 后台 RAG 索引(嵌入模型加载、BM25构建)在 _init_rag_async 里跑，
        # 不阻塞页面渲染，但用户此时看到的UI其实还不能正常工作。
        # 用 self.agent._rag_ready(threading.Event) 作为最终就绪信号，
        # 遮罩盖住整个页面，就绪后自动隐藏。
        #
        # 文案分两类：
        # - "瞬时事实"：render() 时已经能读到的真实数据（技能数、代理、
        #   知识库块数等），按固定节奏依次渐隐展示，制造"在动"的感觉，
        #   即使这些事实其实早就为真。
        # - "真实异步阶段"：嵌入模型加载/BM25索引等，通过
        #   rag_engine.get_init_stage_log() 轮询，真正完成才推进；
        #   耗时不定的那一条（嵌入模型）用呼吸动画占位，不装样子分段。
        with ui.element('div').classes(
            'fixed inset-0 z-[9999] flex flex-col items-center justify-center gap-3'
        ).style('background:var(--nano-panel-2);') as loading_overlay:
            ui.spinner('dots', size='3em', color='indigo')
            ui.label('正在初始化…').style(
                'font-size:var(--nano-fs-lg); color:var(--nano-fg-soft); '
                'letter-spacing:0.05em; margin-top:4px;'
            )
            init_stage_label = ui.html('').classes('init-stage-text').style(
                'font-size:var(--nano-fs-base); color:var(--nano-dim); '
                'letter-spacing:0.04em; text-align:center; min-height:18px;'
            )

        async def _run_init_sequence():
            STAGE_MIN_MS = 500

            async def _show(text: str, min_ms: int = STAGE_MIN_MS):
                init_stage_label.set_content(text)
                init_stage_label.classes(remove='fade-out')
                await asyncio.sleep(min_ms / 1000)
                init_stage_label.classes(add='fade-out')
                await asyncio.sleep(0.35)  # 等渐隐动画播完，再切下一条文案

            # ── 1. 瞬时事实：render() 时已能读到的真实数据 ──────────────
            try:
                _skill_count = len(self.agent.registry.get_all_manifests())
            except Exception:
                _skill_count = 0

            _proxy = os.getenv("AI_PROXY") or os.getenv("HTTPS_PROXY")

            try:
                _kb_stats = rag_engine.get_stats()
                _chunk_count = _kb_stats.get("total_chunks", 0)
            except Exception:
                _chunk_count = 0

            # temp_cleaned 由后台线程写入 _init_stage_log，是近乎瞬时的操作，
            # 但严格来说仍是异步的——短等一下，等不到就用不带数字的通用文案。
            _temp_cleaned_text = "🧹 临时文件检查完成 ✓"
            for _ in range(5):  # 最多等 ~0.5s
                _stages = rag_engine.get_init_stage_log()
                _hit = next((s for s in _stages if s.startswith("temp_cleaned:")), None)
                if _hit:
                    _n = int(_hit.split(":")[1])
                    _temp_cleaned_text = (
                        f"🧹 已清理 {_n} 个历史临时文件 ✓" if _n > 0
                        else "🧹 临时文件目录无需清理 ✓"
                    )
                    break
                await asyncio.sleep(0.1)

            instant_items = [
                f"📦 注册技能 {_skill_count} 个 ✓",
                ("🌐 网络代理已配置 ✓" if _proxy else "🌐 未配置代理，直连 ✓"),
                "🧠 工作记忆模块已就绪 ✓",
                _temp_cleaned_text,
                "🖥️ 界面渲染完成 ✓",
                f"📚 向量数据库已就绪，共 {_chunk_count} 个知识片段 ✓",
            ]

            for _text in instant_items:
                if self.agent._rag_ready.is_set():
                    break  # 极端情况：索引早已就绪，不必继续播放
                await _show(_text)

            # ── 2. 真实异步阶段：轮询 rag_engine 的初始化阶段日志 ────────
            async def _wait_stage(prefix_or_name: str, ready_text: str,
                                   loading_text: str | None = None):
                """等待某个真实阶段标记出现，再展示对应文案。"""
                if self.agent._rag_ready.is_set():
                    return
                if loading_text:
                    init_stage_label.set_content(loading_text)
                    init_stage_label.classes(remove='fade-out')
                while True:
                    _stages = rag_engine.get_init_stage_log()
                    if any(s.startswith(prefix_or_name) for s in _stages):
                        break
                    if self.agent._rag_ready.is_set():
                        return  # 整体已就绪(比如极快完成)，不再单独展示这条
                    await asyncio.sleep(0.15)
                await _show(ready_text)

            await _wait_stage(
                "embedder_ready", "🧠 知识库嵌入模型加载完成 ✓",
                loading_text="🧠 加载知识库嵌入模型中…",
            )

            # bm25_ready / bm25_unavailable 二选一（互斥，只会出现其中一个）
            if not self.agent._rag_ready.is_set():
                init_stage_label.set_content("🔍 准备检索引擎中")
                init_stage_label.classes(remove='fade-out')
                _bm25_ok = False
                while True:
                    _stages = rag_engine.get_init_stage_log()
                    if "bm25_ready" in _stages:
                        _bm25_ok = True
                        break
                    if "bm25_unavailable" in _stages:
                        _bm25_ok = False
                        break
                    if self.agent._rag_ready.is_set():
                        break
                    await asyncio.sleep(0.15)
                if not self.agent._rag_ready.is_set():
                    _text = "🔍 混合检索引擎已启用 ✓" if _bm25_ok else "🔍 向量检索已启用 ✓"
                    await _show(_text)

            await _wait_stage(
                "bm25_index_built", "⚙️ 关键词索引构建完成 ✓",
            )

            # ── 3. 最终完成 ───────────────────────────────────────────
            while not self.agent._rag_ready.is_set():
                _stages = rag_engine.get_init_stage_log()
                _done = next((s for s in _stages if s.startswith("done:")), None)
                if _done:
                    break
                await asyncio.sleep(0.15)

            _stages = rag_engine.get_init_stage_log()
            _done = next((s for s in _stages if s.startswith("done:")), None)
            if _done:
                _, _indexed, _skipped, _errors = _done.split(":")
                init_stage_label.set_content(
                    f"✨ 初始化完成，新增 {_indexed} 个文件、跳过 {_skipped} 个、"
                    f"错误 {_errors} 个 ✓"
                )
            else:
                init_stage_label.set_content("✨ 初始化完成 ✓")
            init_stage_label.classes(remove='fade-out')
            await asyncio.sleep(STAGE_MIN_MS / 1000)

            try:
                loading_overlay.delete()
            except Exception:
                pass

        # render() 在 ui.run() 之前同步执行，此时还没有运行中的 event loop，
        # asyncio.create_task 会报 "no running event loop"。
        # 用 ui.timer(once=True) 交给 NiceGUI 在事件循环启动后调度。
        ui.timer(0.01, _run_init_sequence, once=True)
        # 默认启动是浅色模式，但很多基础 CSS 规则（.q-header/.q-drawer 等）
        # 的默认值是深色，需要主动套用浅色 JS 才会生效——这个调用挪到了
        # main 里的 _on_browser_connect 钩子，每次新连接（含刷新页面）都
        # 会重新跑一遍，不再放在这里（这里只对第一次连接生效一次，见
        # _on_browser_connect 旁边的注释）。

        # ── Header ────────────────────────────────────────────────────────
        # 模型选择器跟左右两组图标分开，用绝对定位摆在 header 正中间
        # （参考日间设计图），不再随便挤在右侧那组里——header 本身要
        # position:relative 才能让下面那个 absolute 居中块生效。
        with ui.header().classes('px-5 h-[60px] items-center').style('position:relative;'):
            with ui.row().classes('w-full items-center justify-between pywebview-drag-region'):
                with ui.row().classes('items-center gap-3'):
                    # Quasar 的 flat 按钮在没显式指定 color 时，文字/图标颜色
                    # 会被 q-btn 自己的主题色（蓝）用 !important 盖掉，光靠
                    # classes('text-slate-500') 这种 Tailwind 工具类赢不过，
                    # 跟之前发送按钮撞的是同一个坑——颜色必须写 inline
                    # !important 才稳。下面几处同款按钮都是这个原因。
                    # 三灯 = 设置菜单触发按钮（抽屉常开、原齿轮按钮已删）。点三灯弹设置。
                    # ⭐⭐ 三个点**直接进设置面板**，下拉菜单已删
                    #     （2026-08-29：「搬完架子里面就剩下一个设置…没必要留个下拉」）。
                    #
                    # 🪦 原来这里是一个 6 项的下拉：设置 / 个人信息 / 环境配置 / 用量限额 /
                    #    OS 权限 / MCP 连接。前一轮把后五项全搬进了设置面板，于是这个下拉
                    #    只剩「设置」一项 —— **一个只有一项的菜单，就是一次多余的点击**。
                    # 🪦 一起删的还有 `_settings_dirty`（给 OS 权限那项点「含高危」红标）——
                    #    2026-08-29 定：徽标不要了。
                    #
                    # ⚠️ 那五个 `_show_*_dialog` **一个都没删**：它们是弹窗形态，还有别的入口在用
                    #    —— 聊天里的「管理限额」按钮、以及 .env 缺 key 时启动直接弹的配置页。
                    #    📌 **删掉一个入口，不等于删掉它通向的东西。**
                    with ui.element('div').classes('cursor-pointer pywebview-no-drag').style('line-height:1;').on('click', self._show_settings_panel):
                        ui.html(
                            '<span style="color:var(--nano-danger);">●</span>'
                            '<span style="color:var(--nano-amber); margin:0 8px;">●</span>'
                            '<span style="color:var(--nano-ok);">●</span>'
                        ).style('font-size:var(--nano-fs-4xl); line-height:1;')
                    self._identity_label = ui.label(self._identity_text()).style(
                        'font-size:var(--nano-fs-md); color:var(--nano-dim); font-family:var(--nano-mono); letter-spacing:0; margin-left:8px;'
                    )
                with ui.row().classes('items-center gap-2 pywebview-no-drag'):
                    # 模型选择器（挪到头栏右侧、导航左边；原绝对居中版已删）。
                    # 中转徽章精简到模型名左侧：中转=只"中转"两字(琥珀)，官方=不显示。
                    current_model_id = self.provider.target_model
                    # ⚠️ 换厂商之后，旧的 model id 不在新厂商的清单里 ——
                    #    必须按【当前厂商的选项】兜底，不能按 GEMINI_MODEL_MAP
                    #    （那是 Claude 专属的表，深度求索的 id 一个都不在里面）。
                    _opts_now = self._vendor_model_options()
                    if current_model_id not in _opts_now:
                        current_model_id = next(iter(_opts_now), "")
                    with ui.row().style(
                        'align-items:center; gap:2px; padding:0 6px; height:30px; '
                        'background:var(--nano-panel); border:1px solid var(--nano-line); border-radius:6px;'
                    ):
                        self._relay_badge_label = ui.label('中转').style(
                            'font-size:var(--nano-fs-xs); color:var(--nano-warn); background:rgba(var(--nano-warn-rgb),0.14); '
                            'border-radius:2px; padding:1px 5px; white-space:nowrap;'
                        ).tooltip('当前经过第三方 API 中转，非官方直连。可在设置（右上角三个点）的「环境配置」里切换。')
                        self._relay_badge_label.set_visibility(getattr(self.provider, 'is_relay', False))
                        # ⚠️ 跟着厂商走 —— 写死 GEMINI_MODELS 的话，切到深度求索之后
                        #    下拉里还是三个 Claude，选谁都发不出去。
                        _model_options = self._vendor_model_options()
                        self._model_select = ui.select(
                            options=_model_options, value=current_model_id, on_change=self._on_model_change,
                        ).props('borderless dense popup-content-class=model-select-popup') \
                            .classes('model-select-field').style('font-size:var(--nano-fs-md); min-width:78px;')
                        _star_is_active = self._get_star_icon() == "star"
                        self._star_btn = ui.button(
                            icon=self._get_star_icon(), on_click=self._toggle_default_model
                        ).props('flat round dense size=sm').style(
                            ('color:var(--nano-warn) !important;' if _star_is_active else 'color:var(--nano-fg) !important;')
                            + ' transition:color 0.2s;')
                        self._star_btn.tooltip('设为启动默认 / 取消默认')

                    # KB 按钮——抽屉合并成一个之后没有滑入动画了，用文字
                    # "发光"代表当前激活面板，不用纯色块（用户原话："变
                    # 颜色有点突兀"）。三个按钮存引用，_show_right_panel()
                    # 切换时统一加/去 nav-glow 这个 class。
                    self._nav_kb_row = ui.row().classes('items-center gap-1 cursor-pointer px-3 py-1.5 rounded-xl hover:bg-white/5 transition-all') \
                            .on('click', lambda: self._show_right_panel('kb'))
                    with self._nav_kb_row:
                        ui.label('[知识库]').classes('nav-label').style('font-size:var(--nano-fs-base);')
                    # Monitor 按钮
                    self._nav_monitor_row = ui.row().classes('items-center gap-1 cursor-pointer px-2 py-1.5 rounded-xl hover:bg-white/5 transition-all') \
                            .on('click', lambda: self._show_right_panel('monitor'))
                    with self._nav_monitor_row:
                        ui.label('[监控]').classes('nav-label').style('font-size:var(--nano-fs-base);')
                    # 后台任务按钮（agent 也走这一张，见 tasks_panel）
                    self._nav_tasks_row = ui.row().classes('items-center gap-1 cursor-pointer px-2 py-1.5 rounded-xl hover:bg-white/5 transition-all relative') \
                            .on('click', lambda: self._show_right_panel('tasks'))
                    with self._nav_tasks_row:
                        ui.label('[任务]').classes('nav-label').style('font-size:var(--nano-fs-base);')
                        with ui.element('div').classes('absolute -top-1 -right-1'):
                            self._tasks_badge_label = ui.label('').style(
                                'display:none; background:var(--nano-amber); color:var(--nano-panel); '
                                'font-size:var(--nano-fs-2xs); font-weight:700; border-radius:999px; '
                                'min-width:16px; height:16px; line-height:16px; '
                                'text-align:center; padding:0 3px;')
                    # Memory 按钮（user_note）
                    self._nav_memory_row = ui.row().classes('items-center gap-1 cursor-pointer px-2 py-1.5 rounded-xl hover:bg-white/5 transition-all relative') \
                            .on('click', lambda: self._show_right_panel('memory'))
                    with self._nav_memory_row:
                        ui.label('[记忆]').classes('nav-label').style('font-size:var(--nano-fs-base);')
                        with ui.element('div').classes('absolute -top-1 -right-1'):
                            self._memory_badge_label = ui.label('').style(
                                'display:none; background:var(--nano-danger-fill); color:#fff; '
                                'font-size:var(--nano-fs-2xs); font-weight:700; border-radius:999px; '
                                'min-width:16px; height:16px; line-height:16px; '
                                'text-align:center; padding:0 3px;'
                            )
                    # 计划按钮（初始隐藏，任务开始时出现）
                    self._nav_plan_row = ui.row().classes('items-center gap-1 cursor-pointer px-3 py-1.5 rounded-xl hover:bg-white/5 transition-all') \
                            .on('click', lambda: self._show_right_panel('plan'))
                    with self._nav_plan_row:
                        ui.label('[计划]').classes('nav-label').style('font-size:var(--nano-fs-base);')
                    self._nav_plan_row.set_visibility(False)

                    # 无边框窗口控制（终端风：细字符按钮，最小/最大/关闭）
                    with ui.row().classes('items-center pywebview-no-drag').style('gap:2px; margin-left:6px;'):
                        ui.button('—', on_click=self._win_minimize).props('flat dense').style(
                            'min-width:26px; width:26px; height:26px; padding:0; color:var(--nano-fg-soft) !important; '
                            'font-size:var(--nano-fs-md); border-radius:2px;').tooltip('最小化')
                        ui.button('▢', on_click=self._win_maximize_toggle).props('flat dense').style(
                            'min-width:26px; width:26px; height:26px; padding:0; color:var(--nano-fg-soft) !important; '
                            'font-size:var(--nano-fs-sm); border-radius:2px;').tooltip('最大化/还原')
                        ui.button('✕', on_click=self._win_close).props('flat dense').style(
                            'min-width:26px; width:26px; height:26px; padding:0; color:var(--nano-fg-soft) !important; '
                            'font-size:var(--nano-fs-md); border-radius:2px;').tooltip('关闭')

        # ── 左侧：工具栏 ──────────────────────────────────────────────────
        with ui.left_drawer(value=True, fixed=True).props('width=256 breakpoint=0').classes('p-0') as self.drawer:
            with ui.column().classes('w-full h-full pb-4 justify-between'):
                # ── ① 考拉卡片：参考日间设计图，头像区独立成一张圆角卡片，
                # 跟下面的工具列表区分开，而不是直接摆在抽屉里。
                with ui.column().classes('w-full px-3 pt-3'):
                    with ui.column().classes('w-full items-center theme-card').style(
                        'border-radius:16px; padding:16px 8px 12px;'
                    ):
                        render_nano_koala_avatar(state_getter=self._build_koala_state, height=220)

                with ui.column().classes('w-full gap-0 flex-1 overflow-hidden'):
                    # 标题
                    with ui.row().classes('px-4 mb-3 items-center gap-2'):
                        ui.label('~/tools').style(
                            'font-size:var(--nano-fs-base); color:var(--nano-dim); font-family:var(--nano-mono);'
                        )
                    # 技能列表
                    self.skill_list_container = ui.column().classes('gap-0 w-full px-2')
                    self.refresh_skill_list()

                # 底部：外观切换（参考日间设计图，"图标+文字+›"的行，点开
                # 是一个真实的二选一菜单（浅色/深色），不是纯装饰箭头——
                # 加主题模式只需要加菜单项，不用改交互形态。
                # 主题切换：默认（浅色，暖白）/ 终端（终端风，暖黑）。
                # ⚠️ 内部标识仍是 'aurora'/'terminal'（已写进用户配置，不能改）；
                #    这里改的只是【显示名】和【顺序】。
                with ui.column().classes('w-full px-4'):
                    ui.separator().classes('opacity-[0.06] mb-3')
                    with ui.row().classes('w-full items-center justify-between cursor-pointer hover:bg-white/5 transition-colors theme-row') \
                            .style('padding:6px 4px; border-radius:8px; margin:0 -4px;'):
                        with ui.row().classes('items-center gap-2'):
                            # 恢复上次选择：图标/文字必须跟着走，否则会出现
                            # 「界面是极光、这一行却写着默认」
                            _tm0 = getattr(self, 'theme_mode', 'terminal')
                            _ic0, _lb0 = (('auto_awesome', '默认') if _tm0 == 'aurora'
                                          else ('terminal', '终端'))
                            self._theme_row_icon = ui.icon(_ic0).style('font-size:var(--nano-fs-xl); color:var(--nano-dim) !important;')
                            self._theme_row_label = ui.label(_lb0).style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft) !important;')
                        ui.icon('chevron_right').style('font-size:var(--nano-fs-xl); color:var(--nano-fg);')

                        def _set_theme(mode: str, label: str, icon: str):
                            theme_menu.close()
                            self.theme_mode = mode
                            self._apply_theme_visuals()
                            # 不落盘 = 只在内存里生效，关掉就没了。
                            # 「开启后永远是深色」的根因就是缺这一行。
                            self._save_app_config()
                            # 收起菜单后要能看出当前是哪套，所以行首图标/文字跟着走
                            if getattr(self, '_theme_row_icon', None):
                                self._theme_row_icon.props(f'name={icon}')
                            if getattr(self, '_theme_row_label', None):
                                self._theme_row_label.set_text(label)
                            # ⚠️ 菜单两项的高亮必须在【切换时】重算 —— 建菜单时算一次
                            #    就固定了，之后再怎么切都不会变（edge- vs level-triggered）。
                            for _r, _m in ((_row, 'aurora'), (_row2, 'terminal')):
                                _col = ('var(--nano-accent)' if _m == mode
                                        else 'var(--nano-fg-soft)')
                                for _ch in list(getattr(_r, 'default_slot').children):
                                    try:
                                        _ch.style(f'color:{_col};')
                                    except Exception:
                                        pass

                        with ui.menu().props('anchor="top left" self="bottom left"').classes('nano-menu-popup') as theme_menu:
                            with ui.column().style('min-width:170px; padding:6px; gap:2px;'):
                                # 选中项：去掉发光边框/底块，只让字本身变亮琥珀
                                _row = ui.row().classes('items-center gap-2 w-full cursor-pointer') \
                                    .style('padding:7px 10px; border-radius:8px;') \
                                    .on('click', lambda: _set_theme('aurora', '默认', 'auto_awesome'))
                                _c1 = 'var(--nano-accent)' if _tm0 == 'aurora' else 'var(--nano-fg-soft)'
                                with _row:
                                    ui.icon('auto_awesome').style(f'font-size:var(--nano-fs-xl); color:{_c1}; flex-shrink:0;')
                                    ui.label('默认').style(f'font-size:var(--nano-fs-base); flex:1; color:{_c1};')
                                _row2 = ui.row().classes('items-center gap-2 w-full cursor-pointer') \
                                    .style('padding:7px 10px; border-radius:8px;') \
                                    .on('click', lambda: _set_theme('terminal', '终端', 'terminal'))
                                _c2 = 'var(--nano-accent)' if _tm0 != 'aurora' else 'var(--nano-fg-soft)'
                                with _row2:
                                    ui.icon('terminal').style(f'font-size:var(--nano-fs-xl); color:{_c2}; flex-shrink:0;')
                                    ui.label('终端').style(f'font-size:var(--nano-fs-base); flex:1; color:{_c2};')

        # ── 右侧：监控面板 ────────────────────────────────────────────────
        # 宽度跟知识库/记忆两个抽屉统一成 320——之前这里是 300，三个
        # 抽屉宽度不一致，切换时主内容区的留白计算容易跟丢（复现条件
        # 跟"先开哪个宽度不同的抽屉"强相关），统一宽度后这个问题不会
        # 再触发。
        with ui.right_drawer(value=False, fixed=True).props('width=320 breakpoint=0').classes('p-0') as self.right_drawer:
            with ui.column().classes('w-full h-full px-4 py-5 gap-3') as self.monitor_panel:
                # 标题
                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('monitor').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                    ui.label('系统监控').style(
                        'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                    )

                # 状态 metric cards (2x2 grid)
                with ui.grid(columns=2).classes('w-full gap-2'):
                    # 系统状态
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('系统状态').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        self.status_lbl = ui.label('SYS_IDLE').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-ok); font-weight:500;'
                        )
                    # 当前模型
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('当前模型').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        _init_model_name = next(
                            (m["name"] for m in CLAUDE_MODELS if m["id"] == self.provider.target_model),
                            self.provider.target_model
                        ).upper()
                        self.model_lbl = ui.label(_init_model_name).style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-ok); font-weight:500;'
                        )
                    # 互联网检索 = 当前是否有联网类 MCP 能力（真实状态，见 _update_net_status）
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('互联网检索').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        with ui.row().classes('items-center gap-1.5'):
                            self.net_dot = ui.element('div').style(
                                'width:6px; height:6px; border-radius:50%; background:var(--nano-fg-mute); flex-shrink:0;'
                            )
                            self.net_lbl = ui.label('OFFLINE').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); font-weight:500;'
                            )
                    # 知识库 RAG
                    # 这些卡【曾经】只表达"本轮活动状态"（IDLE=本轮没查 / HIT=查了），
                    # 于是"组件彻底挂了"和"能用但没查"显示完全相同——2026-08-03 两次 RAG
                    # 崩溃时它显示的都是 IDLE。改为【可用性优先于活动状态】：
                    # FAULT(红) > DEGRADED(橙) > HIT(琥珀) > IDLE(灰)。
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('知识库 RAG').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        with ui.row().classes('items-center gap-1.5'):
                            self.rag_dot = ui.element('div').style(
                                'width:6px; height:6px; border-radius:50%; background:var(--nano-fg-mute); flex-shrink:0;'
                            )
                            self.rag_lbl = ui.label('IDLE').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); font-weight:500;'
                            )
                    # 全文加载
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('全文加载').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        with ui.row().classes('items-center gap-1.5'):
                            self.full_file_dot = ui.element('div').style(
                                'width:6px; height:6px; border-radius:50%; background:var(--nano-fg-mute); flex-shrink:0;'
                            )
                            self.full_file_lbl = ui.label('IDLE').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); font-weight:500;'
                            )
                    # 环境（新增）：没有专属卡片的降级项落这里，
                    # 否则 Tesseract 缺失这类"哑弹型降级"不翻 cmd 一辈子发现不了。
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('环境').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        with ui.row().classes('items-center gap-1.5'):
                            self.env_dot = ui.element('div').style(
                                'width:6px; height:6px; border-radius:50%; background:var(--nano-ok); flex-shrink:0;'
                            )
                            self.env_lbl = ui.label('OK').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-ok); font-weight:500;'
                            )
                    # 今日 Token
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('今日 Token').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        _today_tok = usage_tracker.today_input_output()
                        self.token_lbl = ui.label(
                            _fmt_tokens(_today_tok[0] + _today_tok[1])
                        ).style('font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); font-weight:500;')
                        # 建完立刻按当前档位刷一次（可能要补上 cache hit 那一段）
                        self._refresh_token_card()
                    # ⭐ 上下文厚度。**这是这一层对用户唯一可见的产出。**
                    #
                    # ⚠️ 显示的是「占这个模型自己窗口的百分比」，不是绝对 token 数：
                    #    Haiku 200K / Sonnet·Opus 1M，同一个绝对数在两端意义完全不同。
                    # ⚠️ 没量到时显示 `--` 而**不是 0%** ——
                    #    📌 一个"我不知道"被渲染成 0%，比不显示更糟：它看起来像真数据。
                    with ui.column().classes('monitor-metric theme-card gap-1'):
                        ui.label('上下文').style('font-size:var(--nano-fs-xs); color:var(--nano-fg-soft);')
                        self.ctx_lbl = ui.label('--').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-fg-mute); font-weight:500;'
                        )
                        # ⭐ 建完立刻按「上次量到的值」填一次 —— 📌 
                        #    「Nano 是连续的，打开→关闭→再打开这个数没有理由变」。
                        #    不填的话它会一直是 `--` 直到用户先说一句话。
                        self._refresh_context_card()

                # 执行日志
                ui.separator().classes('opacity-[0.06]')
                with ui.column().classes('w-full gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('terminal').classes('text-[13px]').style('color:var(--nano-fg) !important;')
                        ui.label('执行日志').style(
                            'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                        )
                    with ui.column().classes('w-full rounded-xl p-3 theme-card'):
                        self.log_lbl = ui.label('系统已就绪。等待指令...').style(
                            'font-size:var(--nano-fs-base); color:var(--nano-fg-soft); line-height:1.6; word-break:break-all;'
                        )

                # 重置对话——之前标题字号/颜色比"执行日志"那种标题弱一截，
                # 跟卡片又挤在一起分不清谁是标题。改成同样"图标+12px加粗
                # 标题在卡片外面，卡片只装内容"的结构，跟执行日志对齐。
                ui.separator().classes('opacity-[0.06]')
                with ui.column().classes('w-full gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('restart_alt').classes('text-[13px]').style('color:var(--nano-fg) !important;')
                        ui.label('重置对话').style(
                            'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                        )
                    with ui.row().classes('w-full items-center justify-between rounded-xl p-3 theme-card'):
                        # 与确认弹窗同一句话的短版 —— 📌 两处措辞必须同源，
                        #    否则用户在按钮上读到一件事、在弹窗里读到另一件事。
                        ui.label('放弃之前的全部会话，从零开始').style('font-size:var(--nano-fs-base); color:var(--nano-fg-soft);')
                        ui.button(
                            icon='restart_alt',
                            on_click=self._confirm_reset_conversation
                        ).props('flat round dense').style('color:var(--nano-fg) !important;').classes('hover:text-rose-400 transition-colors')

        # ── 右侧：知识库管理 ──────────────────────────────────────────────
            # overflow-x:hidden 防御：即使某个子元素意外比 320px 宽
            # （比如长警告文本/长路径），也只在抽屉内部触发横向滚动，
            # 不会把整个页面撑出横向滚动条。
            with ui.column().classes('w-full h-full px-4 py-5 gap-3').style('overflow-x:hidden;') as self.kb_panel:
                # 标题行——刷新按钮挪到"已索引文件"卡片右上角了（更贴近
                # "刷新的是文件列表"这个语义），这里只留标题。
                with ui.row().classes('w-full items-center gap-2 mb-1'):
                    ui.icon('library_books').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                    ui.label('知识库管理').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')

                # ── 知识库设置 ──────────────────────────────────────
                with ui.column().classes('w-full rounded-2xl p-3 gap-3 theme-card'):
                    with ui.row().classes('items-center gap-2 mb-1'):
                        ui.icon('tune').classes('text-[13px]').style('color:var(--nano-fg) !important;')
                        ui.label('入库设置').style(
                            'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                        )
                    # 增强模式开关
                    # 之前"?"图标是跟"标题+副标题"两行的整个 column 平级
                    # 摆在一起，items-center 会按这个 column 的总高度（两行）
                    # 居中，导致图标飘到两行中间、没有紧跟在标题那一行右侧。
                    # 改成图标跟标题同一行，副标题单独另起一行在下面。
                    with ui.row().classes('w-full items-center justify-between'):
                        with ui.column().classes('gap-0.5'):
                            with ui.row().classes('items-center gap-1'):
                                ui.label('增强模式').style('font-size:var(--nano-fs-sm); line-height:1; color:var(--nano-fg-soft); ')
                                with ui.element('div').style('flex-shrink:0; display:flex; align-items:center;'):
                                    ui.icon('help_outline').style('font-size:var(--nano-fs-lg); color:var(--nano-dim); cursor:help; line-height:1;')
                                    ui.tooltip(
                                        '对 PDF 和 Word 文档中的嵌入图片（图表、流程图、示意图等）'
                                        '调用多模态模型生成文字描述并入库，让 Nano 能理解图片内容。'
                                        '\n\n适用场景：文档含有大量图表、流程图或截图时开启。'
                                        '\n不适用：纯文字文档、扫描件（扫描件走整页 OCR，不受此影响）。'
                                        '\n\n注意：每张图片独立调用一次 API，图片较多时消耗较高，入库时间增加，建议按需开启。'
                                    ).style('font-size:var(--nano-fs-sm); max-width:280px; white-space:pre-line;')
                        enhanced_switch = ui.switch(value=self._enhanced_mode).style(
                            'color:var(--nano-amber);'
                        )
                        # ⭐⭐⭐ [2026-08-10] **这里原来是 `.on('update:model-value', …)`
                        #    而回调读的是 `e.value` —— 那个属性在原始事件上不存在。**
                        #
                        # 🔴 `GenericEventArguments` 的字段是 `(sender, client, args)`；
                        #    只有 `ValueChangeEventArguments` 才有 `.value`。
                        #    于是每次点这个开关都 `AttributeError`，被 NiceGUI 的
                        #    事件包装吞掉 → **开关视觉上动了，`self._enhanced_mode`
                        #    没变、没存盘、也没有那句提示**。
                        #
                        # ⭐ 这一处是修软/硬上限滑块那个 bug 时**顺带扫出来的**
                        #    （同一形状：拿原始事件当值变化事件用）。
                        # 📌 **两个长得一样的事件，字段不一样时，"用错哪一个"不会
                        #    立刻暴露** —— 因为错的那一半被 except 吞了，
                        #    而 UI 上开关**照样会动**（那是前端自己的状态，
                        #    跟后端有没有收到无关）。
                        # 📌 **一个「控件自己会动」的交互，不能靠"看起来生效了"验收。**
                        def on_enhanced_change(e):
                            _on = bool(e.value)
                            self._enhanced_mode = _on
                            self._save_app_config()
                            ui.notify(
                                f"增强模式已{'开启' if _on else '关闭'}",
                                type='positive' if _on else 'info',
                                icon='auto_awesome' if _on else 'auto_awesome_off'
                            )
                        enhanced_switch.on_value_change(on_enhanced_change)
                    # OCR页数上限滑块
                    with ui.column().classes('w-full gap-1'):
                        with ui.row().classes('w-full items-center justify-between'):
                            with ui.row().classes('items-center gap-1'):
                                ui.label('OCR 页数上限').style('font-size:var(--nano-fs-sm); line-height:1; color:var(--nano-fg-soft); ')
                                with ui.element('div').style('flex-shrink:0; display:flex; align-items:center;'):
                                    ui.icon('help_outline').style('font-size:var(--nano-fs-md); color:var(--nano-dim); cursor:help; line-height:1;')
                                    ui.tooltip(
                                        '扫描型 PDF（图片型）入库时，最多处理的页数上限。\n'
                                        '超出部分不会入库，健康度面板会显示截断警告。\n\n'
                                        '建议值：日常文档 50 页，合同/报告 100 页。\n'
                                        '注意：每页调用一次多模态 API，页数越多消耗越高。'
                                    ).style('font-size:var(--nano-fs-sm); max-width:260px; white-space:pre-line;')
                            ocr_pages_lbl = ui.label(f'{self._ocr_max_pages} 页').style(
                                'font-size:var(--nano-fs-sm); color:var(--nano-amber); '
                            )
                        ocr_slider = ui.slider(min=10, max=200, step=10, value=self._ocr_max_pages).style(
                            'width:100%; color:var(--nano-amber);'
                        )
                        def on_ocr_pages_change(e):
                            self._ocr_max_pages = int(e.args)
                            ocr_pages_lbl.set_text(f'{self._ocr_max_pages} 页')
                            self._save_app_config()
                        ocr_slider.on('change', on_ocr_pages_change)

                # 上传区
                with ui.column().classes('w-full rounded-2xl p-4 gap-3 theme-card'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('upload_file').classes('text-[13px]').style('color:var(--nano-fg) !important;')
                        ui.label('添加文档').style(
                            'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                        )
                    ui.label('支持 txt / md / pdf / docx / pptx / xlsx / csv / jpg / png / webp').style(
                        'font-size:var(--nano-fs-xs); color:var(--nano-dim); '
                    )
                    upload = ui.upload(
                        label='拖拽文件到此处',
                        on_upload=self._handle_kb_upload,
                        multiple=True,
                        auto_upload=True,
                        max_file_size=50_000_000,
                    ).props('flat accept=".txt,.md,.pdf,.docx,.pptx,.xlsx,.xls,.csv,.jpg,.jpeg,.png,.webp,.bmp,.gif"').classes('w-full kb-upload')
                    upload.style(
                        'border: 1px dashed rgba(var(--nano-shade-rgb), 0.15); border-radius: 12px; padding: 6px;'
                    )
                    # 虚线框本身能点选/拖拽，但不够明显——加个真按钮，
                    # 不用每次都从文件夹里拖文件过来。
                    ui.button(
                        '添加文件', icon='add',
                        on_click=lambda: ui.run_javascript(
                            f'document.querySelector("#c{upload.id} input[type=file]").click()'
                        )
                    ).props('flat dense').style(
                        'width:100%; font-size:var(--nano-fs-sm); color:var(--nano-fg) !important; margin-top:4px;'
                    )

                # 已索引文件列表
                ui.separator().classes('opacity-[0.06]')
                with ui.column().classes('w-full gap-1 flex-grow rounded-2xl p-3 theme-card'):
                    with ui.row().classes('w-full items-center justify-between mb-0.5'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('folder_open').classes('text-[13px]').style('color:var(--nano-fg) !important;')
                            ui.label('已索引文件').style(
                                'font-size:var(--nano-fs-base); font-weight:600; color:var(--nano-fg-soft);'
                            )
                        # 刷新按钮从最上面"知识库管理"标题旁边搬过来了，
                        # 缩到 90% 大小，跟这张卡片本身的尺寸更协调。
                        ui.button(icon='refresh', on_click=self._refresh_kb_file_list) \
                            .props('flat round dense').style(
                                'color:var(--nano-fg) !important; transform:scale(0.9); margin:-2px;'
                            )
                    self.kb_stats_lbl = ui.label('加载中...').style(
                        'font-size:var(--nano-fs-sm); color:var(--nano-ok); margin-bottom:2px;'
                    )
                    # 健康度提示（有风险才显示）跟着统计行一起搬过来——
                    # 之前留在最上面"知识库管理"标题旁边，跟统计数字搬家
                    # 之后的位置脱节了，看起来像是给旧位置留的空行。
                    self._kb_health_lbl = ui.html('').style(
                        'font-size:var(--nano-fs-xs); width:100%; margin-bottom:4px; '
                        'white-space:normal; overflow-wrap:break-word; word-break:break-word;'
                    )
                    self.kb_file_list_container = ui.column().classes('w-full gap-1.5')
                self._refresh_kb_file_list()
                self._load_pending_notes_on_start()

        # ── 右侧：记忆管理（user_note）──────────────────────────────────
            with ui.column().classes('w-full h-full px-4 py-5 gap-3') as self.memory_panel:
                # 标题行：左侧图标+标题，右侧"编辑记忆"入口
                with ui.row().classes('w-full items-center justify-between mb-1'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('psychology').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                        ui.label('Nano 的记忆').style('font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                    ui.button('编辑记忆', icon='edit_note',
                        on_click=self._show_all_notes_dialog
                    ).props('flat dense').style('color:var(--nano-fg) !important;').classes('text-[11px]')

                # ── 通知卡片区（纯通知，摞满即可，无需滚动条）────────────────
                self._pending_cards_container = ui.column().classes('w-full gap-2')

                with self._pending_cards_container:
                    ui.label('暂无新记忆').style('font-size:var(--nano-fs-base); color:var(--nano-fg-mute);') \
                        .classes('w-full text-center py-3')

        # ── 右侧：计划面板 ──────────────────────────────────────────────
            with ui.column().classes('w-full h-full px-4 py-5 gap-2') as self.plan_panel:
                with ui.row().classes('w-full items-center gap-2 mb-2'):
                    ui.icon('checklist').style('font-size:var(--nano-fs-lg); color:var(--nano-fg) !important;')
                    self._plan_title_label = ui.label('').style(
                        'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);'
                    )
                self._plan_steps_container = ui.column().classes('w-full gap-2')

        # ── 右侧：后台任务（Subagent 也共用这一张）──────────────
        #
        # ⭐ 早先定的形态：Running / Finished 两段，Finished 可折叠带
        #    Clear，每行有终止 `■` 与可展开详情。agent 是「**无法被回看的
        #    后台任务**」—— 进同一张表，与普通后台任务唯一差别是
        #    **主体模型不回看它的过程**（保护上下文隔离）。
        #    📌 「用户能不能看过程」和「主体模型能不能看过程」是两件事。
        #
        # ⭐ 做成**常驻抽屉按钮**（和知识库/监控/记忆并列）而不是只挂在 pill 上，
        #    是 2026-08-12 拍的，同时解掉「pill 归零后无法翻 Finished」：
        #    📌 **pill 管「向未来」**（还有事在跑、跟着气泡、归零消失）、
        #       **常驻按钮管「向过去」**（翻历史调用）—— 两个不同的钟，不合并。
            with ui.column().classes('w-full h-full px-4 py-5 gap-3') as self.tasks_panel:
                with ui.row().classes('w-full items-center justify-between mb-1'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('bolt').classes('text-[14px]').style('color:var(--nano-fg) !important;')
                        ui.label('后台任务').style(
                            'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                    ui.button(icon='refresh', on_click=self._refresh_tasks_panel) \
                        .props('flat dense round').style('color:var(--nano-dim) !important;')
                self._tasks_body = ui.column().classes('w-full gap-2')

            # ⭐ Subagent监控抽屉。**不进 nav_rows** —— 没有并列按钮，
            #    入口只有 pill 与后台任务里那行的 `View transcript`。
            with ui.column().classes('w-full h-full px-4 py-5 gap-2') as self.agent_panel:
                with ui.row().classes('w-full items-center justify-between mb-1'):
                    with ui.row().classes('items-center gap-2'):
                        ui.label('‹').classes('cursor-pointer').style(
                            'font-size:var(--nano-fs-xl); color:var(--nano-dim); '
                            'font-family:var(--nano-mono);').on(
                            'click', lambda _: self._show_right_panel('tasks'))
                        ui.label('分身监控').style(
                            'font-size:var(--nano-fs-md); font-weight:600; color:var(--nano-fg);')
                    ui.button(icon='refresh', on_click=self._render_agent_monitor) \
                        .props('flat dense round').style('color:var(--nano-dim) !important;')
                self._agent_body = ui.column().classes('w-full gap-1')

        # 五个面板共用一个 right_drawer，默认都不显示。
        self.monitor_panel.set_visibility(False)
        if self.agent_panel is not None:
            self.agent_panel.set_visibility(False)
        self.kb_panel.set_visibility(False)
        self.memory_panel.set_visibility(False)
        self.plan_panel.set_visibility(False)
        self.tasks_panel.set_visibility(False)

        # ── 主聊天区 ──────────────────────────────────────────────────────
        # Item4 修复：max-w-4xl(896px) 在宽屏下两侧留白过多，宽表格在
        # 中间这一小块里就触发横向滚动条。放宽到 max-w-6xl(1152px)——
        # 小屏(w-full < max-width 时 max-width 不生效)不受影响，
        # 宽屏下给表格多 256px 空间。
        # ⚠️ `pb-24`（96px）→ `pb-6`（24px），2026-08-14。
        #    那 96px 一直在，只是**以前被底部溢出吃掉了一截**，所以看起来"刚好"。
        #    溢出修掉之后它整块露了出来，composer 下面凭空空一大片。
        # 📌 **一个被 bug 遮住一半的边距，看起来是「刚好」的** ——
        #    修掉 bug 之后它的真实大小才暴露，那时才发现它从来就没被真正定过。
        # ⭐ 24px 与顶部 `pt-8`(32px) 视觉配重接近；嫌多嫌少直接调这个数。
        with ui.column().classes('main-chat-col w-full min-w-0 max-w-6xl mx-auto h-[calc(100vh-60px)] px-6 pb-6 pt-8 flex flex-col overflow-hidden'):
            # 日用量警告条（默认隐藏，soft/hard cap 时显示）
            self._cost_warning_bar = ui.row().style('display:none; width:100%; align-items:center; justify-content:space-between; padding:6px 12px; border-radius:8px; margin-bottom:4px; flex-shrink:0;')
            with self._cost_warning_bar:
                with ui.row().classes('items-center gap-2'):
                    ui.icon('warning_amber').style('font-size:var(--nano-fs-xl); color:var(--nano-warn);')
                    self._cost_warning_lbl = ui.label('').style('font-size:var(--nano-fs-base); color:var(--nano-fg);')
                ui.button('管理限额', on_click=self._show_cost_cap_dialog).props('flat dense').style(
                    'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft) !important; padding:2px 8px;'
                )
            self._update_cost_warning()

            # ⭐⭐ 被动挂起的**即时投影** —— 这是"瞬发"唯一能被人看见的地方。
            #
            # 实测三轮都说"从来没见过瞬发"，原因不是传感器慢：
            # 租约翻转是毫秒级的，但**唯一的感知途径是等 Nano 下一个动作失败、
            # 再等一次 LLM 往返、模型写一句话**。所以感知到的必然是
            # "过一会不知道啥时候突然触发"。
            # 📌 **瞬发存在于状态里，不存在于任何能被看见的东西里** —— 差的就是这一条。
            #
            # ⚠️ 刻意**不走 `emit_chat`**：那是往聊天区发一条，一个任务里接管 5 次
            #    就是 5 条，吵。即时那一层要的是**状态**（有就显示、没有就消失），
            #    不是**事件**。自然语言那一层才走 emit_chat（异步、一次任务一次）。
            # ⚠️ 刻意**不过 LLM**：过了就不是瞬发。这是 用户原设计
            #    「瞬时挂起 + 自然语言分离」里的"瞬时"那一半。
            #
            # 📌 抄的是上面那条日用量警告条的范式（同一个容器、同款隐藏/显示），
            #    不新造布局 —— 本项目已有能用的实现时先抄它。
            self._takeover_bar = ui.row().style(
                'display:none; width:100%; align-items:center; gap:8px; '
                'padding:6px 12px; border-radius:8px; margin-bottom:4px; flex-shrink:0; '
                'background:rgba(var(--nano-info-rgb),0.10); '
                'border-bottom:1px solid rgba(var(--nano-info-rgb),0.30);')
            with self._takeover_bar:
                ui.icon('pan_tool').style('font-size:var(--nano-fs-xl); color:var(--nano-info);')
                self._takeover_lbl = ui.label('').style('font-size:var(--nano-fs-base); color:var(--nano-fg);')
            self._refresh_takeover_bar()

            # ⭐⭐ 聊天区搜索入口（2026-08-14 定的位置）。
            #
            # ⚠️ 位置排除法：左上角是设置菜单、右侧不再加抽屉、顶栏那三个
            #    （知识库/监控/记忆）都是"打开另一个面板"——搜索不是打开别的东西，
            #    是**在当前这堆内容里找**，混进去会被当成第四个抽屉。
            # 📌 **入口应该长在它作用的那个区域上。** 所以贴在滚动区右上角。
            # ⚠️ 半透明 + hover 才实：它不该在聊天 UI 里扎眼 ——
            #    Nano 没有 session 概念，搜索远不如 Claude Code 里那么常用。
            # 🔴 `flex-direction:column` 一个字都不能少（2026-08-14 实测）：
            #    默认是 `row`，而滚动区靠的是 `flex-1 + height:0` 那一套 ——
            #    那套只在**列方向**上成立。行方向下 `flex-1` 只让它横向长，
            #    高度老老实实取 `height:0` → **整个聊天区高度归零、内容全部看不见**。
            #    ⚠️ 而它不报错、不警告，看起来就像"消息没发出去"。
            with ui.element('div').style(
                'position:relative; width:100%; flex:1; min-height:0; '
                'display:flex; flex-direction:column;'
            ):
                self.scroll_area = ui.scroll_area().classes('w-full flex-1 min-h-0 nano-chat-scroll').style('height:0;')
                with self.scroll_area:
                    self.chat_container = ui.column().classes('w-full gap-2 pb-4')
                    _had_history = self._replay_durable_conversation()
                    if not _had_history:
                        self._render_empty_state_greeting()
                # ── 开窗后停在【最新一条】，不是最上面 ────────────────────────
                # 🔴 重放走的是 `_replay_durable_conversation()`，它只负责把内容画出来，
                #    **从来没有人滚过** —— 于是历史越长，开窗越像"回到了远古"。
                #    （那条移出后同步的路径 `scroll_to(percent=1.0)` 是有的，
                #      唯独启动这一条没有 —— 📌 同一件事的两个入口，补了一个漏了一个。）
                # ⚠️ 为什么是两拍而不是一拍：滚动要在**布局算完之后**才有意义，
                #    而 markdown / 图片 / 工具卡的高度是陆续定下来的。
                #    第一拍让它立刻大致到底（用户不会先看到顶部再被甩下去），
                #    第二拍等晚到的图片撑开高度之后再兜一次。
                #    📌 不做成"循环到高度稳定"：那是个不会自己停的东西，
                #       而这里只需要覆盖已知的两个时间点。
                if _had_history:
                    def _to_latest():
                        try:
                            self.scroll_area.scroll_to(percent=1.0, duration=0)
                        except Exception:
                            pass
                    ui.timer(0.15, _to_latest, once=True)
                    ui.timer(1.0, _to_latest, once=True)
                self._search_btn = ui.button(icon='search', on_click=self._open_chat_search) \
                    .props('flat round dense size=sm').classes('nano-chat-search-btn') \
                    .style('position:absolute; top:2px; right:10px; z-index:20; '
                           'color:var(--nano-fg) !important;')
                # ⚠️ 短。「就搜索（快捷键）就行了，现在的太长了」。
                # 🔴 而且第一版 tooltip 里写了 `Ctrl+F`，**却根本没绑那个键** ——
                #    📌 **UI 上许的诺，代码必须兑现**：一个写着快捷键却按了没反应的提示，
                #       比不写更糟（用户会以为是自己按错了）。下面 `ui.keyboard` 补上了。
                self._search_btn.tooltip('搜索 (Ctrl+F)')

                # ── 浏览器侧那三个动作的落点 ────────────────────────
                # ⚠️ 用**全局** `ui.on`，不是元素级 —— 事件是从 document 上的
                #    委托监听器 `emitEvent` 出来的，没有归属元素。
                #    📌 本项目栽过一次同形状的（`cm_change` 注册成元素级 →
                #       handler 一次都没被调用过），所以这里写死用全局。
                ui.on('nano_quote_selection', self._on_quote_selection)
                ui.on('nano_open_file', self._on_open_local_file)
                ui.on('nano_open_url', self._on_open_external_url)
                ui.on('nano_reveal_file', self._on_reveal_local_file)

            # ── 小胶囊（右上角，仅缩窗期可见）：光芒头像 + 计时器，无按钮 ──
            self._mini_bar = ui.row().classes('f8-mini-bar items-center').style(
                'display:none; position:fixed; top:12px; right:12px; z-index:9000; gap:7px; '
                'padding:5px 12px 5px 10px; border-radius:999px; '
                'background:var(--nano-panel); border: 1px solid var(--nano-border); '
                'box-shadow:0 4px 16px rgba(var(--nano-shade-rgb), 0.14); backdrop-filter:blur(6px);'
            )
            with self._mini_bar:
                ui.html(NANO_AVATAR_SVG).style('width:18px; height:18px; flex-shrink:0;')
                self._mini_timer_lbl = ui.label('0:00').style(
                    'font-size:var(--nano-fs-base); color:var(--nano-dim); min-width:28px; font-variant-numeric:tabular-nums;'
                )

            # ── 输入区 ──
            with ui.column().classes('w-full min-w-0 gap-2 pt-4'):
                # 选择卡片挂载点（卡片动态插入到这里，输入框正上方）
                self.choice_card_slot = ui.element('div').classes('w-full')

                # 附件预览区（图片预览 + 临时文件badge）
                self._image_preview_container = ui.row().classes('items-center gap-2 px-1').style('display:none;')
                with self._image_preview_container:
                    self._image_preview_html = ui.html('').style('max-height:60px; border-radius:8px; overflow:hidden;')
                    ui.button(
                        icon='close',
                        on_click=self._clear_pending_image
                    ).props('flat round dense size=sm').classes('text-slate-500 hover:text-rose-400')

                # ── 选中引用的**出口** ─────────────────────────────
                # 🔴 实测：「reply 只有入口没出口 —— 你只要 reply 了就取消不了，
                #    下一条必须 reply」。
                # 📌 **一个进得去出不来的状态，是个陷阱不是功能。**
                #    引用待审卡那条有出口（卡片上的「取消引用」按钮），
                #    而选中文字这条**根本没有承载它的东西** —— 卡片不存在。
                # → 给它自己的承载物：输入框上方撑开一行，显示被引用的原文 + ✕。
                # ⚠️ 只服务 `selection`：待审卡那条 已明确说不需要动
                #    （它已经有出口了，再加一个就是两处表达同一件事）。
                self._quote_bar = ui.row().classes(
                    'items-center gap-1.5 px-1 no-wrap w-full min-w-0'
                ).style('display:none;')
                with self._quote_bar:
                    with ui.row().classes('items-center gap-1.5 no-wrap min-w-0').style(
                        'padding:3px 8px; border-left:2px solid var(--nano-amber); '
                        'background:rgba(var(--nano-amber-rgb), 0.07); border-radius:0 6px 6px 0; '
                        'flex:1; min-width:0; overflow:hidden;'
                    ):
                        # ⚠️ 这里**刻意不放** `↳` —— 输入框左边已经有一个了。
                        #    「重复了两个，去掉引用文字左边那个」。
                        #    📌 同一件事在相邻两处各说一遍，读者会以为它们是两件事。
                        #    左边那条橙色竖线已经把「这是引用」说清楚了。
                        # ⚠️ 三件事一起才不会飞出去：`truncate`（单行省略号）+
                        #    `min-w-0`（否则 flex 子项按内容撑开、不肯缩）+
                        #    父级 `overflow:hidden`。
                        #    📌 少任何一件都还是会撑破 —— 同 那条引用块的教训。
                        self._quote_bar_text = ui.label('').classes(
                            'truncate min-w-0').style(
                            'font-size:var(--nano-fs-sm); color:var(--nano-fg-soft); line-height:1.7; '
                            'font-family:var(--nano-mono); flex:1;')
                    ui.button(icon='close',
                              on_click=lambda: self._set_reply_target(None)) \
                        .props('flat round dense size=xs') \
                        .style('color:var(--nano-dim) !important; flex-shrink:0;') \
                        .tooltip('取消引用')

                # 临时文件列表（文字型，文件名列表）
                self._temp_file_badge = ui.row().classes('items-center gap-1 px-1 flex-wrap').style('display:none;')

                # ── 未决交互常驻卡片（位置 C：输入框上方）─────────
                #
                # 2026-08-04 从四个候选里拍的板：**pinned card + 禁止创建第 7 个**。
                # 位置也是用户定的（"最好不要占用其他 UI 的地方"）——
                # 所以它是 composer 上方一个独立容器，不侵占聊天区、不盖住输入框，
                # 没有未决交互时 `display:none`，一个像素都不占。
                #
                # ⭐ 它解决的是 ②a/②b/②c 留下的**最后一块体感缺口**：
                # 待办已经跨重启存活、模型也看得见了，但**用户看不见**。
                # 实测 ④ 那次测试里 用户得自己记着 Nano 问过什么才能重开后回答 ——
                # 这张卡就是那个缺口。
                self._pinned_card = ui.column().classes('w-full min-w-0 gap-1').style(
                    'display:none; padding:8px 10px; margin-bottom:6px; '
                    'background:var(--nano-panel-2); border:1px solid rgba(var(--nano-warn-rgb),0.28); '
                    'border-radius:10px;'
                )
                self._pinned_snapshot = ""      # 上次渲染的指纹，内容没变就不重画
                # ⭐ 翻页而不是纵向堆叠（2026-08-06 实测之后定的）
                #
                # 第一版每条待办一行、往上堆。两条就把 composer 顶起来一截，
                # 六条（1 前台 + 5 队列，的上限）会堆得非常高 ——
                # 而 已定位置 C 时的原话是"最好不要占用其他UI的地方"，
                # 堆叠违背了那个前提。
                #
                # 改成**一次显示一条 + 翻页**：高度恒定，与待办数量无关。
                #
                # ⚠️ 别把它和 `ask_user_choice` 选择卡片的问题搞混（第一版注释写错了）：
                # **选择卡片本来就是一次一张、已经在显示 `1/3` 页码了，它不堆高**，
                # 缺的只是「翻页」这一个动作。它那三个控件语义各不相同，都**只作用于当前这张**：
                #     submit = 提交选中项或自己填的内容
                #     叉子   = 拒绝回答，放弃这张卡
                #     skip   = 授权 LLM 自己选
                # 给它补翻页时**不要动这三个语义**，尤其不能让叉子把整批卡片一起关掉。
                # 详见下方那段说明。
                self._pinned_page = 0

                # composer 是个纵向卡片：上方是输入区（留白多，不是细条），
                # 底部一行工具栏（附件在左下角，发送按钮在右下角）——
                # 参考日间设计图的比例，不是"一条细横排"。
                with ui.column().classes('w-full min-w-0 no-wrap composer-shell').style(
                    'padding:8px 10px; gap:4px;'
                ):
                    # ── 统一上传按钮（自动路由：图片→context，文件→临时RAG） ──
                    async def _handle_chat_upload(e: events.UploadEventArguments):
                        try:
                            # 这个会话装的 NiceGUI 2.24.2 里 UploadEventArguments
                            # 只有 content/name/type 三个字段，没有 .file——
                            # 跟知识库上传那处是同一个一直存在、没被实测点过的
                            # bug，一起修了。
                            filename = e.name
                            raw_bytes = e.content.read()
                            if asyncio.iscoroutine(raw_bytes):
                                raw_bytes = await raw_bytes
                            if not raw_bytes:
                                ui.notify('❌ 读取文件失败（文件可能为空或格式异常）', type='negative')
                                return
                            import pathlib as _pl
                            suffix = _pl.Path(filename).suffix.lower()
                            IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
                            MIME_MAP = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                        ".png": "image/png", ".webp": "image/webp",
                                        ".bmp": "image/bmp", ".gif": "image/gif"}
                            if suffix in IMAGE_EXTS:
                                # 图片路径：直接进context（待发送）
                                mime = MIME_MAP.get(suffix, "image/jpeg")
                                self._pending_image_bytes = raw_bytes
                                self._pending_image_mime = mime
                                import base64
                                b64 = base64.b64encode(raw_bytes).decode()
                                self._image_preview_html.set_content(
                                    f'<img src="data:{mime};base64,{b64}" style="max-height:56px; border-radius:6px; border:1px solid rgba(var(--nano-amber-rgb), 0.3);">'
                                )
                                self._image_preview_container.style('display:flex;')
                                ui.notify(f'已选择图片: {filename}', type='positive', icon='image')
                            else:
                                # Phase 3：文件路径只存盘，不建 RAG 索引
                                # query_local_knowledge 真正需要搜索时才 lazy build（见 rag.py _ensure_all_temp_files_indexed）
                                import tempfile
                                tmp_dir = pathlib.Path(tempfile.gettempdir()) / "nano_temp_uploads"
                                tmp_dir.mkdir(exist_ok=True)
                                dest = tmp_dir / filename
                                with open(dest, "wb") as f:
                                    f.write(raw_bytes)
                                # 直接标记 ready，不再有 indexing 中间态
                                rag_engine.register_temp_file(filename, str(dest))  # Phase 3：注册到当前会话
                                self._temp_files.append({"filename": filename, "chunks": 0, "status": "ready"})
                                self._refresh_temp_file_badge()
                                ui.notify(f'已添加附件: {filename}', type='positive', icon='attach_file')
                        except Exception as err:
                            ui.notify(f'❌ 上传处理失败: {err}', type='negative')

                    chat_upload = ui.upload(
                        on_upload=_handle_chat_upload,
                        auto_upload=True,
                        max_file_size=50_000_000,
                    ).props('flat accept=".txt,.md,.pdf,.docx,.pptx,.xlsx,.xls,.csv,.jpg,.jpeg,.png,.webp,.bmp,.gif" color=blue-grey-8') \
                     .classes('nano-chat-upload').style('display:none;')
                    # ⭐ 这个类是**拖拽入口的唯一锚点** —— 拖进来的文件靠
                    #    `.nano-chat-upload input[type=file]` 找到它，塞进去再 dispatch
                    #    change，于是走的是**同一个 `_handle_chat_upload`**。
                    #    📌 要的是走同一条路，不是再实现一遍：图片回看、临时文件注册、
                    #       附件角标全挂在那个 handler 上，另起一条必然漏掉其中几样。
                    #    ⚠️ 改这个类名要同步改上面那段脚本里的选择器。
                    self._chat_upload = chat_upload  # 存引用，供重置时调用

                    # 终端风单行输入：❯ + 输入框 + 上传 + 发送 同一行（默认单行细条，
                    # shift+enter 才向上长高；autogrow 负责增高）。
                    with ui.row().classes('composer-input-row w-full min-w-0 items-center no-wrap').style('gap:6px;'):
                        # ⭐ 选中 replay 时，这个 `❯` 换成引用图标 ——
                        # 「我下一条是在回答某个待办」这件事必须在**输入框这里**看得见，
                        # 光靠卡片上那个按钮变成「取消引用」太远、用户打字时看不到。
                        # 由 `_refresh_reply_prompt()` 在设/清指向时切换。
                        self._composer_prompt = ui.label('❯').classes('composer-prompt').style(
                            'color:var(--nano-amber); font-size:var(--nano-fs-xl); flex-shrink:0; line-height:1; font-family:var(--nano-mono);'
                        )
                        self._refresh_reply_prompt()
                        # Bug3：plain Enter 发送、Shift+Enter 放行换行+autogrow 增高（外部评审 方案）
                        self.input_field = ui.input(placeholder='向 nano 发送消息…').props(
                            'borderless autofocus dense type=textarea autogrow rows=1'
                        ).classes('w-full min-w-0 main-input').on(
                            'keydown', self.start_pipeline_task, args=[],
                            js_handler="(e) => { const p = e.key==='Enter' && !e.shiftKey && !e.ctrlKey "
                                       "&& !e.altKey && !e.metaKey && !e.isComposing; if(!p) return; "
                                       "e.preventDefault(); emit(); }"
                        )
                        # 上传 = 次要（小、无底、灰），发送 = 主操作（琥珀方块、更大）
                        ui.button(
                            icon='upload',
                            on_click=lambda: ui.run_javascript(
                                f'document.querySelector("#c{chat_upload.id} input[type=file]").click()'
                            )
                        ).props('flat dense').style(
                            'width:24px; min-width:24px; height:24px; min-height:24px; padding:0; align-self:center; '
                            'flex-shrink:0; color:var(--nano-dim) !important; border-radius:var(--nano-btn-radius);'
                        ).tooltip('上传文件或图片')
                        # ⭐⭐⭐ 这颗按钮有**两个身份**（用户从 todo 里捞出来的漏项）：
                        #    · 输入框**不为空** → 发送（原样）
                        #    · 输入框**为空** 且 有一轮在跑 → **终止**
                        # ⚠️ 它不是「无缝对话」的一部分，早先的设计那句
                        #    「终止按钮不单独修、做完自然消失」**是错的** ——
                        #    📌 两个机制的**出口方向相反**（一个继续、一个停止），
                        #       做完前者不会自动得到后者。
                        self._send_btn = ui.button(
                            icon='arrow_upward', on_click=self._on_send_or_stop
                        ).props('flat dense').style(
                            'width:34px; min-width:34px; height:30px; min-height:30px; padding:0; align-self:center; '
                            'flex-shrink:0; background:var(--nano-send-bg) !important; color:var(--nano-send-fg) !important; '
                            'border:none; border-radius:var(--nano-btn-radius); transition:all 0.2s;'
                        )
                        # ⚠️ **level-triggered 刷新**，不靠"输入时记得改图标"那种配对写法。
                        #    📌 本轮反复栽的都是「靠所有调用点都记得同步」的东西
                        #       （那个泄漏的忙标志）；这里让它每 0.4 秒照当前真实状态重画。
                        ui.timer(0.4, self._refresh_send_btn)

                # 输入框下方提示（仅 OS 屏幕任务期可见）——做成一颗轻量胶囊，
                # 不直接把字甩在背景上（浅色背景下灰字看不清）。
                self._mini_hint = ui.row().classes('w-full items-center justify-center').style(
                    'display:none; padding-top:2px;'
                )
                with self._mini_hint:
                    with ui.row().classes('items-center').style(
                        'gap:6px; padding:4px 12px; border-radius:999px; '
                        'background:rgba(var(--nano-warn-rgb),0.08); border:1px solid rgba(var(--nano-warn-rgb),0.14);'
                    ):
                        ui.icon('stop_circle').style('font-size:var(--nano-fs-md); color:var(--nano-warn);')
                        ui.html(
                            '按 <b style="font-weight:600;">Ctrl + `</b> 停止 Nano 操作'
                        ).style('font-size:var(--nano-fs-sm); color:var(--nano-warn); letter-spacing:0.02em;')

                # 输入框下方：左=Auto 模式下拉（Ask permission / Auto），
                # 右=主动智能 effort 下拉（Low/Medium/High + 折叠可解释面板）。
                with ui.row().classes('w-full items-center justify-between').style('padding:2px 2px 0;'):
                    # 左下：Auto 模式
                    self._auto_chip = ui.row(wrap=False)
                    with self._auto_chip:
                        ui.icon('bolt').style('font-size:var(--nano-fs-md);')
                        self._auto_label = ui.label('Auto').style('letter-spacing:0.02em;')
                        with ui.menu().props('anchor="top middle" self="bottom middle"').classes('nano-menu-popup') as self._auto_menu:
                            self._auto_menu_box = ui.column().classes('gap-0')
                    self._auto_chip.on('click', self._open_auto_menu)
                    self._refresh_auto_chip()

                    # ⚠️ effort chip 与上下文圆环包成**右侧一组** —— 外层是
                    #    `justify-between`，直接加第三个孩子会把它们摊成三等分。
                    with ui.row(wrap=False).classes('items-center').style('gap:0;'):
                        # 右下：主动智能 effort + 可解释面板（锚右对齐，防止弹出溢出右边缘）
                        # ⚠️ `margin-right` 是给右边那个上下文圆环让位的（已明确
                        #    圆环对齐输入框右下角）—— 不挪的话 effort chip 会把它顶出去。
                        self._effort_chip = ui.row(wrap=False).style(
                            'display:flex; align-items:center; gap:2px; cursor:pointer; '
                            'font-size:var(--nano-fs-sm); padding:2px 8px; border-radius:999px; '
                            'margin-right:8px; '
                            'color:var(--nano-fg-soft); border: 1px solid var(--nano-border);')
                        with self._effort_chip:
                            self._effort_label = ui.label('Medium').style('letter-spacing:0.02em;')
                            ui.icon('expand_less').style('font-size:var(--nano-fs-lg);')
                            with ui.menu().props('anchor="top middle" self="bottom middle"').classes('nano-menu-popup') as self._effort_menu:
                                self._effort_menu_box = ui.column().classes('gap-0')
                        self._effort_chip.on('click', self._open_effort_menu)
                        self._refresh_effort_chip()

                        # ⭐ 上下文圆环 —— 对齐输入框右下角（形态参考 Claude Code）。
                        #
                        # ⚠️ 它**不是**第二个"当前模型/用量"指示器：Nano 没有 plan limit，
                        #    这个环只说一件事 —— **这段对话占了窗口多少**。
                        # 📌 与监控卡那张「上下文」是**同一个数**（都读 `budget.snapshot`），
                        #    只是一个在抽屉里、一个在手边。**不许各算各的。**
                        # ⚠️ effort chip 上面刚加了 `margin-right`，为的就是把这个环
                        #    腾到输入框右下角那条竖线上（用户指定的位置）。
                        # 🔴 **菜单必须挂在【外层容器】上，不能挂进 `ui.html` 里面。**
                        #    第一版把 `ui.menu()` 建在 `self._ctx_ring` 内部，
                        #    而 `_refresh_context_ring()` 每次都 `set_content(svg)` ——
                        #    那会**整体替换 innerHTML，连菜单的 DOM 一起抹掉**。
                        #    实测表现：点圆环没反应，**而且 Quasar 残留的遮罩层把
                        #    知识库/监控/记忆三个抽屉按钮一起挡死了**。
                        # 📌 **别把组件塞进一个"内容会被整体替换"的容器里** ——
                        #    它不会报错，只会在下一次刷新时安静地消失。
                        # ⚠️ 于是 `ui.html` 只负责画 svg（可以随便被替换），
                        #    菜单和点击都长在它外面那层 div 上。
                        _ring_wrap = ui.element('div').style(
                            'display:flex; align-items:center; cursor:pointer; '
                            'line-height:0; flex-shrink:0; position:relative;')
                        with _ring_wrap:
                            self._ctx_ring = ui.html('').style('line-height:0;')
                            # 上拉面板与 Auto / effort 同一套形态
                            # —— 📌 同一排的三个控件，点开的方式不该有三种。
                            with ui.menu().props(
                                'anchor="top right" self="bottom right"'
                            ).classes('nano-menu-popup') as self._ctx_menu:
                                self._ctx_menu_box = ui.column().classes('gap-0')
                        _ring_wrap.on('click', self._show_context_popover)
                        self._ctx_ring_wrap = _ring_wrap
                        self._refresh_context_ring()

        # ── UI 就绪不变量（必须是 render() 的最后一句）────────────────────
        # render 完成后保证：chat_container 存在、auto-index client 已捕获、ui_ready=True。
        # 在此之前发生的事件只登记/入队，此后由唯一消费者渲染。
        #
        # 为什么捕获点在这儿而不是 _on_browser_connect（原方案）：render() 在 ui.run()
        # 之前同步跑完，而 _on_browser_connect 要等 WebView2 子进程起来 + socket 握手，
        # 中间隔好几秒；而 RAG 初始化线程在 WebUI() 构造时就已经在跑了，故障恰好落在
        # 这个窗口里。元素树是进程级单例（auto-index client），此刻写入的 DOM 会在
        # 浏览器连上后自动同步过去，不会丢。
        self._mark_ui_ready()

        # ⭐ 搜索浮层：整段 JS 注入一次，之后开/关/搜/跳都在浏览器里跑（见
        #    `_SEARCH_OVERLAY_JS` 的长注释：auto-index 上 `run_javascript` 不能 await）。
        # ⚠️ 走 `add_body_html` 而不是 `_js_fire`：后者依赖客户端已连上，
        #    而这里是**建界面的时刻**，浏览器还没连。📌 一次性的初始化要跟着页面走，
        #    不要跟着连接走。
        ui.add_body_html('<script>' + self._SEARCH_OVERLAY_JS + '</script>')

        # ── 首启未配置 API Key：把环境配置弹窗顶到用户面前 ──────────────────
        # 这条路径以前根本到不了：没 key 时 ClaudeProvider.__init__ 直接 raise，
        # WebUI() 构造失败、界面压根不显示。现在构造不再失败（未配置态），
        # 于是"用户不碰 .env、一律在 UI 里配"这个目标才真正成立——
        # 但前提是得让用户知道去哪填，不能只留一个什么都不回答的输入框。
        # 延时挂载：等 WebView2 子进程连上再弹，否则弹窗会开在还没渲染的树上。
        try:
            if not self.provider.is_configured:
                ui.timer(1.2, lambda: self._show_env_config_dialog(), once=True)
        except Exception as _e:
            logger.warning(f"[UI] 首启配置检查失败（不影响启动）: {_e}")


# ── 启动 ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    # 控制台日志：默认 INFO；data/dev_flags.json 中 console_debug 为 true 时输出 DEBUG。
    # 必须在其他模块产生日志之前配置。
    try:
        import sys as _sys
        from core import dev_flags as _dev_flags
        logger.remove()
        logger.add(_sys.stderr, level="DEBUG" if _dev_flags.enabled("console_debug") else "INFO")
    except Exception as _log_err:
        print(f"[Log] 控制台日志配置失败，沿用默认设置: {_log_err}")

    try:
        # 异常钩子尽早装（补充手段——抓得到的 Python 级异常也记进同一个 journal，
        # 免得只留在 cmd 里）。真正的主力是 rag.py 里那几处 write-ahead breadcrumb，
        # 因为 segfault / os._exit / import 期终止这三种钩子一个都抓不到。
        try:
            from core import crash_journal as _cj
            _cj.install_hooks()
        except Exception as _e:
            print(f"[CrashJournal] 钩子安装失败（不影响启动）：{_e}")

        _bootstrap_core_modules()

        # ── Runtime Kernel 的启动恢复 ────────────────────────────
        # ⚠️ 必须在 `ui.run()` 之前【同步】跑，不能挂 ui.timer：上一个进程留下的
        # 活 ToolBatchSpan 是脏状态，而这段窗口（WebUI() 构造 → RAG 初始化线程启动）
        # 恰好是最容易再出事的地方。理由同 crash_journal.startup_scan() 与 health 的写入端。
        #
        # 早先时刻意没接线（那时 Kernel 里没有任何跨重启需要恢复的东西）；
        # 早先一旦让 Span 落盘，不接就会让"重启时处于 OPEN"被下一轮的 sweep
        # 误判成"批次中途抛异常"——实测 shadow 第一天就撞到了这个误判。
        try:
            from core.runtime import get_kernel as _rt_get_kernel
            from core.runtime import reconcile_on_startup as _rt_reconcile
            _rt_rep = _rt_reconcile(_rt_get_kernel())
            # ⭐ 留给「重启后问一句」用。⚠️ 存 details 不是 id 列表 ——
            #    只有 id 的话 Nano 说不出「那件事是什么」（见 ReconcileReport 注释）。
            # 🔴 **这里是模块级代码，没有 `self`。** 第一版写成 `self._startup_…`
            #    → 运行时 `name 'self' is not defined`，被那个 `except` 吞成
            #    「启动恢复失败（不影响启动）」—— 📌 一条被吞掉的 NameError，
            #    表现成的是「恢复失败」，而不是「有人写错了变量」。
            # ⚠️ 这里**本来就在模块作用域**，不需要（也不能）写 `global` ——
            #    它上面那条声明带类型标注，`global` 一个带标注的名字是 SyntaxError。
            _STARTUP_INTERRUPTED = list(
                getattr(_rt_rep, "interrupted_details", []) or [])
            # extra 中的计数全为 0 时不算有动作（reconciler 已记录「无需处理」）。
            if _rt_rep.did_anything or any((_rt_rep.extra or {}).values()):
                logger.warning(f"[Runtime] 启动恢复：{_rt_rep.summary()} extra={_rt_rep.extra}")

            # ⭐ 本次运行的身份留痕。**挂在恢复报告之后**——
            #    "这次是谁在跑"和"这次恢复了什么"天然是一份东西，
            #    分开写两处会立刻产生"哪份是准的"这个问题。
            # ⚠️ 失败不抛：它是留痕，不是正确性依赖。
            from core.runtime import identity as _rt_ident
            _rt_ident.record_run(_rt_get_kernel(), _rt_rep.summary())

            # ⭐ 挂上"这一次是新进程"的一次性提示。
            #    ⚠️⚠️ 绑在这里（进程启动）而**不是** NiceGUI `on_connect` ——
            #    后者的语义是"每一次 socket 握手"，**重连也会触发**
            #    （ping_timeout≈2-4s，网络抖一下就重放）。绑它等于把
            #    "网络抖了一下"当成"进程重启了"。理由是 用血换来的。
            _rt_ident.arm_restart_notice(_rt_get_kernel())
        except Exception as _rt_err:
            # 启动恢复失败绝不能阻断启动 —— 它是修脏状态的，不是必需路径。
            logger.error(f"[Runtime] 启动恢复失败（不影响启动）: {_rt_err}")

        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        registry.reload_all()

        gui = WebUI()
        
        observer = Observer()
        observer.schedule(
            SkillWatcher(gui),
            path=str(pathlib.Path("skills").absolute()),
            recursive=False
        )
        observer.daemon = True
        observer.start()

        from nicegui import app as _nicegui_app

        # 静态资源必须在 render() 前注册，否则首次挂载时 CSS 里的 url(...)
        # 可能先于静态路由生效，不同机器缓存状态下表现不一致
        _icons_dir = pathlib.Path(__file__).parent / "assets" / "file_icons"
        if _icons_dir.exists():
            _nicegui_app.add_static_files('/icons', str(_icons_dir))
        # 终端风字体：JetBrains Mono（OFL，已打包进 assets/fonts，离线可用，用户无需安装）
        _vendors_dir = pathlib.Path(__file__).parent / "assets" / "vendors"
        if _vendors_dir.exists():
            _nicegui_app.add_static_files('/vendors', str(_vendors_dir))
        _fonts_dir = pathlib.Path(__file__).parent / "assets" / "fonts"
        if _fonts_dir.exists():
            _nicegui_app.add_static_files('/fonts', str(_fonts_dir))

        # MCP：进程退出时优雅关闭所有 server worker（终止 stdio 子进程，避免残留）。
        async def _mcp_shutdown():
            try:
                from core.mcp_client import get_mcp_manager
                await get_mcp_manager().shutdown()
            except Exception:
                pass
        _nicegui_app.on_shutdown(_mcp_shutdown)

        gui.render()

        # ── 客户端连接：只重贴主题，绝不重置对话 ──────────────────────────
        # 这里【曾经】每次连接都调 reset_conversation()，本意是让"浏览器刷新/
        # 重新打开"表现成新会话。但 NiceGUI 的 on_connect 语义不是"新客户端"，
        # 而是"每一次 socket 握手"——重连也会重放（见 nicegui/client.py 的
        # handle_handshake，其相邻的 handle_disconnect 注释明确写着 disconnect
        # handler 才不在重连时调用）。而心跳阈值很紧（ping_timeout≈2-4s），
        # 事件循环卡 6 秒（加载 torch、大 PDF OCR、机器休眠、渲染进程重启）
        # 就足以触发一次重连 → 整段对话历史 + 所有 pending 状态被静默清空。
        # 最坏场景已复现：Skill 审计弹窗开着时网络打嗝 → _pending_skill 被清 →
        # 用户点"验证并应用"报"没有待审批的 Skill"，代码还在弹窗里却部署不了。
        #
        # 为什么直接删掉而不是"只在首次握手 reset"：
        # Nano 没有 @ui.page，render() 在启动时跑一次，UI 挂在 NiceGUI 的
        # auto-index client 上（nicegui.py 里进程级单例）。所以刷新页面【不会】
        # 重建元素树，只是把已有的树重新序列化给新页面——聊天气泡刷新后还在。
        # 既然刷新和重连都保留 UI，那两种情况都不该清 memory，否则就会出现
        # "屏幕上有对话、Nano 却失忆"的撕裂。要守的不变量是 UI 与 memory 同步。
        #
        # 原注释担心的两件事都另有正经解法，不依赖这次 reset：
        # - 幽灵 Skill：_skill_not_found_reply 查磁盘备份 + 扫最近事件给事实性回答
        # - pending 残留：_expire_stale_pending（30min）+ clarification 自带 600s 超时
        # 显式"重置当前对话"按钮（_do_reset_conversation）是唯一的重置入口。

        @_nicegui_app.on_connect
        def _on_browser_connect(client):
            with client:
                gui._apply_theme_visuals()
                # Quasar 部分容器在 connect 回调后继续挂载，延迟补一次透明补丁
                # 防止后挂载的容器覆盖背景层（新电脑时序可能和旧电脑不同）
                ui.run_javascript("""
                    // ⚠️ 这里**不许**再贴主题类：主题由 `_apply_theme_visuals()` 唯一负责。
                    //    曾经写死 `add('nano-theme-terminal')`，导致切到浅色 350ms 后
                    //    被无条件改回深色 —— 而 Python 侧状态是对的，菜单还显示"默认"。
                    //    📌 表现和状态不一致时，是有人在绕过状态直接改表现。
                    setTimeout(() => {
                        document.querySelectorAll('#q-app,.q-layout,.q-page-container,.q-page,.nicegui-content,main').forEach(el=>{
                            el.style.setProperty('background','transparent','important');
                        });
                    }, 350);
                    setTimeout(() => {
                        document.querySelectorAll('#q-app,.q-layout,.q-page-container,.q-page,.nicegui-content,main').forEach(el=>{
                            el.style.setProperty('background','transparent','important');
                        });
                    }, 900);
                """)
            logger.info("[App] 客户端已连接（重贴主题；对话状态保持不变）。")

        # ── 原生窗口（pywebview + WebView2）─────────────────────────────
        # 浏览器模式下 Nano 没有独立窗口句柄，自缩窗 / 截图排除自身都做不到。
        # native=True 让 NiceGUI 走 pywebview，Windows 上用 WebView2 内核渲染成独立桌面窗口。
        # 接缝：pywebview 缺失或 WebView2 Runtime 不在 → 优雅降级回浏览器模式，
        # 不崩。WebView2 Runtime 的 bundle/检测是打包窗口的事，这里只做"不在就降级"。
        _native_ok = True
        _native_reason = ""
        try:
            import webview  # noqa: F401  pywebview
        except Exception as _e:
            _native_ok, _native_reason = False, f"pywebview 未安装（{_e}）"
        if _native_ok and not _webview2_runtime_present():
            _native_ok, _native_reason = False, "未检测到 WebView2 Runtime"

        _run_kwargs = dict(
            title='Nano OS', port=8080, reload=False, dark=False,
            storage_secret='nano-office-secret-2025',
        )
        if _native_ok:
            # window_size 给一个像样的初始尺寸（NiceGUI native 默认 800x600 偏小）。
            # 自缩窗后续靠 app.native.main_window 动态改尺寸/位置。
            _run_kwargs.update(native=True, window_size=(1200, 820))
            # 标题栏/任务栏：标题 "Nano" + 金黄星芒图标（图标在模块顶层设置，
            # spawn 子进程才读得到，见 _apply_native_window_icon）。Windows 下标题栏
            # 与任务栏共用同一份图标+标题，两处一致显示 Nano 头像 + "Nano"。
            _run_kwargs['title'] = 'Nano'
            logger.info("[App] 启动 native 原生窗口（WebView2）。")
            # 常驻系统托盘：✕ 隐藏到右下角托盘而非退出（等窗口就绪后在 daemon 线程建图标）
            _start_system_tray()
        else:
            logger.warning(f"[App] native 不可用，降级浏览器模式：{_native_reason}")

        ui.run(**_run_kwargs)

    except Exception:
        import traceback
        print("\n🚨🚨🚨 【内核启动异常】 🚨🚨🚨")
        traceback.print_exc()
        input("\n[按下回车退出]...")
