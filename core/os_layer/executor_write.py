# core/os_layer/executor_write.py
"""
低级写操作执行器 —— 走系统 API，不含鼠标键盘

包含：
  win_minimize / win_close / win_switch  —— 窗口管理（win32gui）
  set_volume                             —— 音量控制（pycaw）
  launch_app                             —— 启动 GUI 程序（subprocess）
  kill_app                               —— 强制结束进程（psutil）
  file_write / file_read                 —— 文件读写
  clipboard_read / clipboard_write       —— 剪贴板（pyperclip）
  open_url                               —— 打开 URL（webbrowser）

鼠标/键盘（click / type_text / drag 等）在 `executor_action.py`，不在本文件。
设计原则（实现约束 2）：只报状态，不决策。
平台依赖全部 lazy import + 优雅降级。
"""
from __future__ import annotations
import asyncio
import subprocess
import webbrowser
from typing import Any, Dict
from loguru import logger


def _missing(dep: str) -> Dict[str, Any]:
    return {"ok": False, "data": {}, "summary": "", "error": f"dependency_missing: {dep}"}


def _resolve_path(raw: str) -> str:
    """展开路径里的环境变量（%USERPROFILE% 等）和 ~，模型被要求用
    '%USERPROFILE%\\Desktop\\x.txt' 这种写法代替自己猜用户名拼绝对路径，
    这里负责把它真正展开成可用的真实路径。"""
    import os as _os
    return _os.path.expandvars(_os.path.expanduser(raw or ""))


class WriteExecutor:

    def __init__(self):
        import platform
        self._is_windows = platform.system() == "Windows"

    # ── 窗口管理 ─────────────────────────────────────────────────────────

    async def win_minimize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._win_op, "minimize", params)

    async def win_close(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._win_op, "close", params)

    async def win_switch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._win_op, "switch", params)

    def _win_op(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import pygetwindow as gw
        except ImportError:
            return _missing("pygetwindow")
        try:
            title_kw = params.get("title") or params.get("window_title") or ""
            wins = [w for w in gw.getAllWindows()
                    if title_kw.lower() in (w.title or "").lower()] if title_kw else []
            if not wins:
                # 没有指定或没找到，操作前台窗口
                w = gw.getActiveWindow()
                if not w:
                    return {"ok": False, "data": {}, "summary": "",
                            "error": f"target window not found (title={title_kw!r})"}
                wins = [w]
            w = wins[0]
            if op == "minimize":
                w.minimize()
                return {"ok": True, "data": {"title": w.title}, "summary": f"minimized window: {w.title}", "error": ""}
            elif op == "close":
                w.close()
                return {"ok": True, "data": {"title": w.title}, "summary": f"closed window: {w.title}", "error": ""}
            elif op == "switch":
                w.activate()
                return {"ok": True, "data": {"title": w.title}, "summary": f"switched to window: {w.title}", "error": ""}
            return {"ok": False, "data": {}, "summary": "", "error": f"unknown win_op: {op}"}
        except Exception as e:
            logger.error(f"[OS-Write] win_{op} 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 音量控制 ─────────────────────────────────────────────────────────

    async def set_volume(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._set_volume_sync, params)

    def _set_volume_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: pycaw is Windows-only"}
        level = params.get("level")
        if level is None:
            return {"ok": False, "data": {}, "summary": "", "error": "missing level parameter (0-100)"}
        level = max(0, min(100, int(level)))
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from ctypes import cast, POINTER
            import comtypes
            devices = AudioUtilities.GetSpeakers()
            # 兼容新旧版 pycaw：
            # 旧版 GetSpeakers() 返回原始 COM 对象，直接有 Activate 方法
            # 新版返回 AudioDevice 包装对象，需要通过 ._dev 取底层 COM 对象
            dev_obj = getattr(devices, '_dev', devices)
            interface = dev_obj.Activate(
                IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None
            )
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMasterVolumeLevelScalar(level / 100.0, None)
            return {"ok": True, "data": {"level": level},
                    "summary": f"volume set to {level}%", "error": ""}
        except ImportError:
            return _missing("pycaw")
        except Exception as e:
            logger.error(f"[OS-Write] set_volume 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 启动应用 ─────────────────────────────────────────────────────────

    async def launch_app(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._launch_app_sync, params)

    def _launch_app_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        target = _resolve_path(params.get("target") or params.get("app") or params.get("path") or "")
        if not target:
            return {"ok": False, "data": {}, "summary": "", "error": "missing target parameter"}
        # 安全检查：launch_app 不允许带可执行参数（那应该用 run_command risk=3）
        _DANGEROUS = ["cmd", "powershell", "regedit", "wmic", "mshta", "diskpart", "cscript", "wscript"]
        if any(d in target.lower() for d in _DANGEROUS):
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"launch_app refuses to launch system command-line tools ({target!r}); use run_command for command execution (risk=3)"}
        try:
            if self._is_windows:
                # os.startfile 走 ShellExecute，不是 subprocess.Popen(target,shell=False)。
                # 后者要求 target 必须是真正的 .exe（CreateProcess 直接执行），遇到 .txt/.docx
                # 这类文档会报 WinError 193（不是有效的 Win32 应用程序）——但用户说"打开"
                # 时大概率是指"用默认关联程序打开"，不是"启动一个可执行文件"，os.startfile
                # 才是和"双击打开"语义一致的 API，且同样不经过 cmd shell 解析，不引入命令注入风险。
                import os as _os
                _os.startfile(target)
            else:
                import platform as _plat
                subprocess.Popen(["open", target] if _plat.system() == "Darwin"
                                 else ["xdg-open", target], shell=False)
            return {"ok": True, "data": {"target": target},
                    "summary": f"launched {target}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] launch_app 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 结束进程 ─────────────────────────────────────────────────────────

    async def kill_app(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._kill_app_sync, params)

    def _kill_app_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import psutil
        except ImportError:
            return _missing("psutil")
        name = params.get("name") or params.get("process_name") or ""
        pid = params.get("pid")
        killed = []
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                if pid and proc.pid == int(pid):
                    proc.terminate(); killed.append(str(proc.pid))
                elif name and name.lower() in (proc.info["name"] or "").lower():
                    proc.terminate(); killed.append(proc.info["name"])
            if not killed:
                return {"ok": False, "data": {}, "summary": "",
                        "error": f"process not found (name={name!r}, pid={pid})"}
            return {"ok": True, "data": {"killed": killed},
                    "summary": f"terminated process(es): {', '.join(killed)}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] kill_app 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 文件读写 ─────────────────────────────────────────────────────────

    async def file_write(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._file_write_sync, params)

    def _file_write_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        path = _resolve_path(params.get("path") or "")
        content = params.get("content") or params.get("text") or ""
        mode = params.get("mode", "w")  # "w" 覆写 / "a" 追加
        if not path:
            return {"ok": False, "data": {}, "summary": "", "error": "missing path parameter"}
        try:
            p = pathlib.Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8") if mode == "w" else \
                open(p, "a", encoding="utf-8").write(content)
            return {"ok": True, "data": {"path": str(p), "bytes": len(content.encode())},
                    "summary": f"wrote {p.name} ({len(content)} character(s))", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def file_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._file_read_sync, params)

    def _file_read_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        path = _resolve_path(params.get("path") or "")
        if not path:
            return {"ok": False, "data": {}, "summary": "", "error": "missing path parameter"}
        try:
            p = pathlib.Path(path)
            if not p.exists():
                return {"ok": False, "data": {}, "summary": "", "error": f"file does not exist: {path}"}
            content = p.read_text(encoding="utf-8", errors="replace")
            preview = content[:300] + ("…" if len(content) > 300 else "")
            return {"ok": True, "data": {"path": str(p), "content": content},
                    "summary": f"read {p.name} ({len(content)} character(s)): {preview}", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 剪贴板 ───────────────────────────────────────────────────────────

    async def clipboard_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._clipboard_read_sync)

    def _clipboard_read_sync(self) -> Dict[str, Any]:
        try:
            import pyperclip
        except ImportError:
            return _missing("pyperclip")
        try:
            text = pyperclip.paste() or ""
            return {"ok": True, "data": {"text": text},
                    "summary": f"clipboard content ({len(text)} character(s)): {text[:100]}", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def clipboard_write(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._clipboard_write_sync, params)

    def _clipboard_write_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import pyperclip
        except ImportError:
            return _missing("pyperclip")
        text = params.get("text") or params.get("content") or ""
        try:
            pyperclip.copy(text)
            return {"ok": True, "data": {"length": len(text)},
                    "summary": f"wrote to clipboard ({len(text)} character(s))", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 打开 URL ─────────────────────────────────────────────────────────

    async def open_url(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = params.get("url") or params.get("target") or ""
        if not url:
            return {"ok": False, "data": {}, "summary": "", "error": "missing url parameter"}
        try:
            webbrowser.open(url)
            return {"ok": True, "data": {"url": url},
                    "summary": f"opened URL: {url}", "error": ""}
        except Exception as e:
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    # ── 高危写操作（补丁A新增，地板=3，dispatch层已完成风险确认，这里只管执行）──

    async def run_command(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # ⚠️ **不再走 `to_thread`。** 起进程是瞬时的（`Popen` 不阻塞），
        #    输出由 `longcmd` 自己的读线程收 —— 这里唯一要做的事是「等一会儿」，
        #    而那件事在协程里做既不占线程、又能看见「用户是不是又说话了」。
        #    📌 **一个只是在等的操作，不该占用一个能干活的线程。**
        return await self._run_command_async(params)

    # ⭐ 前台愿意等一个命令多久。**超了不是失败，是交回控制权。**
    #
    # ⚠️ 这个数原来是 `subprocess.run(timeout=30)` 的 `timeout` ——
    #    语义是「30 秒没完就**杀掉并报错**」。
    # 🔴 于是 pip / 下载 / 安装这类最典型的长任务**从来没进过后台体系**：
    #    30 秒后模型拿到的是 `command timed out`，只能重试或放弃。
    # 📌 **闸的出口是失败，队列的出口是稍后处理**（本项目第六次）——
    #    一个跑得久的命令不是错误，它只是还没完。
    # ⚠️⚠️ **模型不许覆盖这个数**（一次实际故障的根因就是它能覆盖）。
    #    模型的 `timeout` 参数被重新解释为「这条命令最多允许跑多久」，
    #    传给 `longcmd.start(hard_deadline_sec=...)`。
    #    📌 「前台愿意等多久」和「这件事最多允许跑多久」是两个问题，
    #       不许由一个数字回答 —— **也不许由同一个来源提供**。
    # ⭐⭐ **快路径宽限期**（2026-08-22 从 45 收到 5，命令/MCP/Subagent 统一）
    #
    # 🔴 它**不是**「我还要不要干等」的决策 —— 旧注释就是那么写的，
    #    而**那个措辞正是一整轮混乱的源头**：它把一个【测量】写成了【决策】。
    #
    # ⭐ 它真正答的是：**「先同步等一下，多半马上就好，省掉异步那一整套开销。」**
    #    如果永远立刻交还，一条 0.1 秒的 `dir` 也要走
    #    起调用 → 交还 → 登记等待 → 模型一轮 → 唤醒 → 模型又一轮，
    #    **一次调用变成两三轮模型调用**。所以宽限期存在。
    #
    # ⚠️ 但它只需要兜住「快调用」，而快调用普遍 < 5 秒 ——
    #    45 秒的宽限期意味着**中间 40 秒是纯失明期**：
    #    用户看不到任何东西（pill 还没出现），模型也说不了话。
    #    📌 **一个为「兜住 99% 快调用」而设的宽限期，不需要覆盖到第 45 秒。**
    #    ⭐ Subagent那个 5 秒早就是这个值了，理由是「Subagent几乎不可能秒回」——
    #       同一个道理反过来用在命令上：命令经常秒回，所以宽限期该**短**
    #       （久的那些反正要走异步，早走早好）。
    #
    # ⚠️⚠️ **模型不许覆盖它**（同上那次故障的根因）。模型能表达的是
    #    「我这次不等」（`wait_for_result=false`，只能**提前**交还），
    #    **不能**把宽限期撑长 —— 📌 一个只能朝安全方向偏离默认的旋钮，
    #    和一个能把保护关掉的旋钮，是两个东西。
    _FOREGROUND_WAIT_SEC = 5

    @staticmethod
    def _new_user_input_arrived():
        """做一个「用户又说话了吗」的判据。**实现已搬到 `longcmd`。**

        🔴 搬走的理由：临时执行通道要用同一个判据，而消费它的
           `await_briefly(stop_when=)` 就在那个模块里。
        📌 **判据只能有一处** —— 两份「用户说话了吗」分叉时不会报错，
           表现是「命令那条路会被插话打断，临时代码那条不会」。
        """
        from core.os_layer import longcmd as _lc
        return _lc.new_user_input_arrived()

    async def _run_command_async(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cmd = params.get("command") or params.get("cmd") or ""
        if not cmd:
            return {"ok": False, "data": {}, "summary": "", "error": "missing command parameter"}
        try:
            from core.os_layer import longcmd as _lcmod
            # cmd 可以是字符串或列表；字符串走 shell=True（用户/模型已声明高危并经过确认），
            # 列表走 shell=False（更安全，优先推荐模型用列表形式）
            shell = isinstance(cmd, str)
            # ⭐⭐⭐ **前台等多久是系统的事，模型不许覆盖。**
            #
            # 🔴 上一版是 `_fg = float(params.get("timeout", 45))`。
            #    实际运行中撞到：一条 `timeout /t 80` 的命令，模型按旧 schema
            #    描述（`timeout` = 「超时就杀掉」）填了一个 ≥80 的值 ——
            #    **于是「前台只等 45 秒」被模型自己关掉了**：
            #      · 日志里 `[LongCmd] … 结束（code=0，80s）`，
            #        全程没有一条 `[LongTask] … → 交回控制权`
            #      · 没有交还 → 没开等待 → **回看无从谈起**
            #      · 没有轮结束 → 用户「算了别做了」只能等到 80 秒后才被处理
            #    也就是说，**一个参数就同时打掉了这次改造的三个成果。**
            #
            # 📌 **一个参数的语义被改了，它的旧调用方会继续按旧语义传值** ——
            #    而当那个调用方是**模型**、读的是没更新的 schema 描述时，
            #    它填的「合理值」恰好是最坏的值（旧语义下填大更安全，
            #    新语义下填大等于关掉保护）。
            # 📌 而这一处最刺眼的地方：`_FOREGROUND_WAIT_SEC` 的注释里就写着
            #    「**「前台愿意等多久」和「这件事最多允许跑多久」是两个问题，
            #    不许由一个数字回答**」，然后下一行就让模型那一个数字回答了
            #    其中一个。**判据写对了，接线接反了。**
            # ⭐⭐ **第 0 秒的出口**（2026-08-22 新增）。
            #
            # 🔴 问题：宽限期是**系统在测量**「这件事久不久」，而模型要等测量
            #    结束才有机会回答「我等不等」。于是用户明说
            #    「你别等它，我还有别的事要问你」时，系统**仍然先死等** ——
            #    那句话在第 0 秒就已经回答了那个问题。
            #    📌 **当决策已经先于测量到达时，测量就是多余的。**
            #
            # ⭐ 分工（v1.52 那条，现在在时间上补齐了）：
            #      系统答「谁在跑」——事实
            #      模型答「我还等不等」——只有它知道
            #    模型本来就有这个权，只是**在第 45 秒之前拿不到话筒**。
            #
            # ⚠️⚠️ **单向**：`wait_for_result=false` 只能让交还来得**更早**，
            #    **没有任何值能把宽限期撑长** —— 那正是那次的坑
            #    （`timeout` 被模型填大，把 45 秒保护整个关掉）。
            #    📌 一个只能朝安全方向偏离默认的旋钮，和一个能把保护关掉的旋钮，
            #       是两个东西。
            # ⚠️ 它**不代替 `dont_wait`**：这一步只是让「还在跑」立刻报上去，
            #    真正转入后台仍然要模型调 `dont_wait(next_step)` ——
            #    📌 那条 `next_step` 必填是防滥用的唯一防线
            #       （填不出「别的事」= 它本来就该在前台上），不许绕过。
            _fg = float(self._FOREGROUND_WAIT_SEC)
            if params.get("wait_for_result") is False:
                logger.info(f"[OS-Write] 模型声明不等这条命令 → 跳过宽限期，立即交还")
                _fg = 0.0
            # ⭐ 模型的 `timeout` 重新解释为**这条命令的硬上限**
            #    （「这件事最多允许跑多久」）—— 那才是它答得出的那个问题。
            #    没给就用 `longcmd` 的默认（30 分钟）。
            _hard = params.get("timeout")
            _kw = {}
            try:
                if _hard is not None and float(_hard) > 0:
                    # ⚠️ 硬上限不许比前台等待还短 —— 那等于回到「30 秒杀掉」。
                    #    📌 一个「最多允许跑多久」的值，如果小于「前台等多久」，
                    #       那么交还这条路就永远走不到，这个机制等于不存在。
                    _kw["hard_deadline_sec"] = max(float(_hard), _fg + 1.0)
            except Exception:
                pass
            lc = _lcmod.start(cmd, shell=shell, **_kw)
            if await _lcmod.await_briefly(lc, _fg,
                                         stop_when=_lcmod.foreground_interrupt()):
                # 快路径：前台等到了 → **形状与旧实现完全一致**，调用方不用改。
                res = lc.final_result()
                _lcmod.forget(lc.ref)
                return res
            if _lcmod.turn_stop_requested():
                # 用户按了终止：这一轮前台正在执行的命令一起停（裁决 73），不交还、不唤醒。
                _lcmod.stop(lc.ref, "stopped by user (turn stopped)")
                await _lcmod.await_briefly(lc, 2.0)      # 等进程真正退出再从注册表移除
                _lcmod.forget(lc.ref)
                logger.info(f"[OS-Write] 用户终止 → 前台命令一起停：{lc.display[:50]}")
                return {"ok": False, "data": {"stopped_by_user": True, "output": lc.tail(20)},
                        "summary": "",
                        "error": ("stopped_by_user: the user stopped this turn, so this command "
                                  "was terminated before it finished. Its effects may be partial.")}
            # ⭐ 慢路径：**命令继续跑**，把「它还在跑」如实报上去。
            #    上层（orchestrator）看到 `long_running` 就走那条公共交还合同
            #    （`_hand_back_long_task`）—— 与 MCP 走的是同一条，不分类型。
            # ⚠️ `ok=True` 是刻意的：**「启动成功且还在跑」不是失败。**
            #    报 False 会让模型以为出错了，然后去重试 —— 那会起第二个进程。
            #    📌 一个「还没完成」的状态被报成失败，最常见的后果是重复执行。
            logger.info(f"[OS-Write] {lc.display[:50]} 前台等了 "
                        f"{lc.elapsed:.0f}s 还没完 → 报 long_running，交给上层交还")
            return {"ok": True,
                    "data": {"long_running": True, "ref": lc.ref,
                             "display": lc.display, "elapsed": lc.elapsed,
                             "output": lc.tail(20)},
                    "summary": (f"command still running after {lc.elapsed:.0f}s "
                                f"(ref={lc.ref}): {lc.display[:60]}"),
                    "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] run_command 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def write_registry(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._write_registry_sync, params)

    def _write_registry_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
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
        type_map = {
            "REG_SZ": winreg.REG_SZ, "REG_DWORD": winreg.REG_DWORD,
            "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ, "REG_MULTI_SZ": winreg.REG_MULTI_SZ,
        }
        try:
            hive = hive_map.get(params.get("hive", "HKCU"))
            if hive is None:
                return {"ok": False, "data": {}, "summary": "", "error": f"unknown hive: {params.get('hive')}"}
            subkey = params.get("subkey", "")
            value_name = params.get("value_name", "")
            value = params.get("value")
            value_type = type_map.get(params.get("type", "REG_SZ"), winreg.REG_SZ)
            with winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, value_name, 0, value_type, value)
            return {"ok": True, "data": {"hive": params.get("hive"), "subkey": subkey, "value": value},
                    "summary": f"wrote registry value {params.get('hive')}\\{subkey}\\{value_name} = {value}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] write_registry 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def file_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._file_delete_sync, params)

    def _file_delete_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        path = _resolve_path(params.get("path") or "")
        if not path:
            return {"ok": False, "data": {}, "summary": "", "error": "missing path parameter"}
        try:
            p = pathlib.Path(path)
            if not p.exists():
                return {"ok": False, "data": {}, "summary": "", "error": f"file does not exist: {path}"}
            if p.is_dir():
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
            return {"ok": True, "data": {"path": str(p)}, "summary": f"deleted {p.name}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] file_delete 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def file_move(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._file_move_sync, params)

    def _file_move_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        import shutil
        src = _resolve_path(params.get("path") or params.get("src") or "")
        dest = _resolve_path(params.get("dest") or params.get("target") or "")
        if not src or not dest:
            return {"ok": False, "data": {}, "summary": "", "error": "missing path/dest parameter"}
        try:
            p_src = pathlib.Path(src)
            p_dest = pathlib.Path(dest)
            if not p_src.exists():
                return {"ok": False, "data": {}, "summary": "", "error": f"source file does not exist: {src}"}
            p_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p_src), str(p_dest))
            return {"ok": True, "data": {"src": str(p_src), "dest": str(p_dest)},
                    "summary": f"moved {p_src.name} -> {p_dest}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] file_move 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def manage_service(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._manage_service_sync, params)

    def _manage_service_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: Windows-only"}
        name = params.get("name") or ""
        op = params.get("op") or params.get("action") or "status"  # start/stop/status/disable
        if not name:
            return {"ok": False, "data": {}, "summary": "", "error": "missing name parameter"}
        _OP_CMD = {
            "start": ["sc", "start", name], "stop": ["sc", "stop", name],
            "disable": ["sc", "config", name, "start=", "disabled"],
            "status": ["sc", "query", name],
        }
        cmd = _OP_CMD.get(op)
        if not cmd:
            return {"ok": False, "data": {}, "summary": "", "error": f"unknown op: {op} (supported: start/stop/disable/status)"}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    encoding="utf-8", errors="replace")
            out = (result.stdout or "") + (result.stderr or "")
            return {"ok": result.returncode == 0, "data": {"output": out[:500]},
                    "summary": f"service {name} {op}: {out[:200]}",
                    "error": "" if result.returncode == 0 else out[:300]}
        except Exception as e:
            logger.error(f"[OS-Write] manage_service 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def set_env_var(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._set_env_var_sync, params)

    def _set_env_var_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: Windows-only"}
        name = params.get("name") or ""
        value = params.get("value", "")
        scope = params.get("scope", "user")  # user/system
        if not name:
            return {"ok": False, "data": {}, "summary": "", "error": "missing name parameter"}
        try:
            import winreg
            if scope == "system":
                key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                hive = winreg.HKEY_LOCAL_MACHINE
            else:
                key_path = "Environment"
                hive = winreg.HKEY_CURRENT_USER
            with winreg.CreateKeyEx(hive, key_path, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
            return {"ok": True, "data": {"name": name, "value": value, "scope": scope},
                    "summary": f"set environment variable {name}={value} ({scope} scope; restart may be required)", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] set_env_var 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def schedule_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._schedule_task_sync, params)

    def _schedule_task_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: Windows-only"}
        name = params.get("name") or ""
        command = params.get("command") or ""
        schedule = params.get("schedule") or "ONCE"  # ONCE/DAILY/WEEKLY 等 schtasks /sc 取值
        start_time = params.get("start_time", "")
        if not name or not command:
            return {"ok": False, "data": {}, "summary": "", "error": "missing name/command parameter"}
        cmd = ["schtasks", "/create", "/tn", name, "/tr", command, "/sc", schedule, "/f"]
        if start_time:
            cmd += ["/st", start_time]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    encoding="utf-8", errors="replace")
            out = (result.stdout or "") + (result.stderr or "")
            return {"ok": result.returncode == 0, "data": {"output": out[:500]},
                    "summary": f"scheduled task {name}: {out[:200]}",
                    "error": "" if result.returncode == 0 else out[:300]}
        except Exception as e:
            logger.error(f"[OS-Write] schedule_task 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def modify_startup(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._modify_startup_sync, params)

    def _modify_startup_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: Windows-only"}
        name = params.get("name") or ""
        command = params.get("command") or ""
        op = params.get("op", "add")  # add/remove
        if not name:
            return {"ok": False, "data": {}, "summary": "", "error": "missing name parameter"}
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
                if op == "remove":
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        return {"ok": False, "data": {}, "summary": "", "error": f"startup item does not exist: {name}"}
                    return {"ok": True, "data": {"name": name},
                            "summary": f"removed startup item {name}", "error": ""}
                if not command:
                    return {"ok": False, "data": {}, "summary": "", "error": "adding a startup item requires the command parameter"}
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
                return {"ok": True, "data": {"name": name, "command": command},
                        "summary": f"added startup item {name} -> {command}", "error": ""}
        except Exception as e:
            logger.error(f"[OS-Write] modify_startup 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}

    async def network_config(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._network_config_sync, params)

    def _network_config_sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_windows:
            return {"ok": False, "data": {}, "summary": "", "error": "unsupported: Windows-only"}
        op = params.get("op") or ""  # set_dns/reset_dns/flush_dns
        interface = params.get("interface", "")
        dns = params.get("dns", "")
        _CMDS = {
            "flush_dns": ["ipconfig", "/flushdns"],
            "set_dns": ["netsh", "interface", "ip", "set", "dns", interface, "static", dns],
            "reset_dns": ["netsh", "interface", "ip", "set", "dns", interface, "dhcp"],
        }
        cmd = _CMDS.get(op)
        if not cmd:
            return {"ok": False, "data": {}, "summary": "", "error": f"unknown op: {op} (supported: flush_dns/set_dns/reset_dns)"}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    encoding="utf-8", errors="replace")
            out = (result.stdout or "") + (result.stderr or "")
            return {"ok": result.returncode == 0, "data": {"output": out[:500]},
                    "summary": f"network config {op}: {out[:200]}",
                    "error": "" if result.returncode == 0 else out[:300]}
        except Exception as e:
            logger.error(f"[OS-Write] network_config 失败: {e}")
            return {"ok": False, "data": {}, "summary": "", "error": str(e)}
