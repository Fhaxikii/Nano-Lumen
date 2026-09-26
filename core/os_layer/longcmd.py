# core/os_layer/longcmd.py
"""长命令载体 —— 让一个命令**活过它那次工具调用**，并且**看得见进度**。

═══ 为什么需要它（2026-08-09，一次质疑逼出来的）═══

回看设计落地时，它被接在了 MCP 自动后台化那一个写点上。反例是：

> 如果 os_execute 的长命令完全不进入同一套等待/回看，那么下载、pip、安装
> 这类最典型长任务反而被排除，回看设计的覆盖面就不符合它当初解决
> 「不死长任务」的目的。

回代码核实：**成立，而且比当初估计的更严重**。`run_command` 原来是
`subprocess.run(..., timeout=30)`，超时的出口是 **`{"ok": False, "error": "command timed out"}"`**
—— 也就是说 pip / 下载 / 安装这类东西**从来没进过后台体系**，
30 秒之后直接报错，模型只能重试或放弃。

📌 **又是「闸的出口是失败，队列的出口是稍后处理」**（本项目第六次）——
   一个跑得久的命令不是错误，它只是还没完。

═══ 这个模块只解决两件事，而它们都是「交回控制权」的物理前提 ═══

① **活过那次工具调用。** `subprocess.run` 是阻塞的：函数返回时进程已经结束
   或被杀，没有任何东西可以交给后台。`Popen` + 读取线程才有一个能被
   继续持有的载体。

② **看得见进度。** 这是回看那一眼**真的有东西可看**的唯一来源。
   ⭐ 而这一点让长命令这条路上的回看**比 MCP 那条有价值得多**：
   MCP 的载体是 `await _mcp_task`，里面没有任何进度流，回看时只能看到
   「还没返回」；而一个命令的 stdout 里有 pip 的百分比、下载速度、报错。
   📌 **「回看一眼」的价值完全取决于那一眼能看到什么**（
      「当然要给，不然整个回看设计都是废的」）——
      所以真正该被接进回看的，首先是**有进度可看**的那条路。

⚠️ **刻意不做的事**：不做「暂停/恢复」、不做输出的持久化、不跨重启。
   进程随 Nano 一起死，那正是 `WaitCondition` 启动清理按
   `kind == BACKGROUND` 收掉它们的理由 —— 载体不可重建。
   📌 只在进程内有意义的东西，不该被做成跨重启的样子。
"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from core.paths import data_dir, data_path
from typing import Any, Deque, Dict, Optional

from loguru import logger


# 缓冲多少行输出。⚠️ 满了**丢头不丢尾** —— 回看时人和模型要看的是
# 「现在到哪了」，而最新的几行才回答那个问题。
# 📌 同 `takeover_log` 那条：**一个「给人看现状」的缓冲，满了要丢最旧的。**
_MAX_LINES = 300

# ── 命令输出落盘（2026-08-26）────────────────────────────────────────
# 📌 这个数的语义不是「最多占多少」，是**「一个长期使用 Nano 的用户，
#    要永久让出多少磁盘」** —— README 那行「推荐预留空间」的实际取值。
# ⚠️ 比截图的 200MB 小得多，因为**两者的有效寿命差一个数量级**：
#      截图     是审计证据 —— 价值在**以后**，出了事要回去查
#      命令输出 是当前对话的工作数据 —— 模型读完就没用了
#    📌 用同一个数是错的：它们被需要的时间跨度不同。
# ⭐ 50MB 的推导：超阈值才落盘的输出实际量级是 10KB~1MB（跑一次全量回归
#    的完整输出约 200KB）⇒ 装得下几百次，远超任何一段对话会回头查的量。
_SPILL_BUDGET_BYTES = 50 * 1024 * 1024
_SPILL_DIR = data_path("cmd_output")

# 给模型的内联上限。⭐ **仍然取末尾** —— 命令的答案通常在尾巴上（成没成、
# 报什么错），这是 `out[-2000:]` 唯一正确的那一半。
# 🔴 它错的那一半是**静默**：模型拿到 2000 字符，不知道后面还有，会当成
#    完整结果去推理。📌 截断可以接受，**不说截断了不行** —— 与 inbox 的
#    `delivery_count`、`ActionAttempt` 的 INTERRUPTED 同一条判据：
#    **说清结果可不可信。**
_INLINE_TAIL_CHARS = 2000

# 载体的硬上限：跑到这里还没结束就杀掉。
# ⚠️ 它**不是**「前台愿意等多久」（那个由调用方的 `foreground_seconds` 决定），
#    而是「这件事本身最多允许跑多久」。
#    📌 两个不同的问题，不许由一个数字回答 —— 这条判据今天已经用到第二次
#       （第一次是 MCP 的 120s 硬超时 vs 90s 交还阈值）。
# ⭐ 与 `WaitCondition` 的默认 orphan 兜底同量级（30 分钟）。
# ⭐ **纯防泄漏上限**，不是「跑这么久就不正常」的判断（2026-08-22 改）。
#    旧值 30 分钟且到点 `kill()` —— 那会杀掉一个 31 分钟才装完的安装包。
# 📌 我们没有权力主动杀用户的进程；「这活是不是出事了」由模型判断，
#    它的出口是 `stop_background`。
# ⚠️ 到点也**不杀**，只是不再替它读输出（见 `_pump` 里那段）。
_HARD_DEADLINE_SEC = 8 * 60 * 60


class LiveCommand:
    """一个还在跑（或刚跑完）的命令。**进程内对象，不跨重启。**"""

    __slots__ = ("ref", "display", "cmd", "popen", "_buf", "_lock",
                 "started_at", "finished_at", "returncode", "killed_reason",
                 "_spill", "_spill_path", "_spill_bytes", "_total_lines")

    def __init__(self, ref: str, display: str, cmd: Any, popen: subprocess.Popen):
        self.ref = ref
        self.display = display
        self.cmd = cmd
        self.popen = popen
        self._buf: Deque[str] = deque(maxlen=_MAX_LINES)
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.killed_reason: str = ""
        # 全量落盘（见 `_spill_open` 上方那段）。None=还没建，False=建过且失败
        self._spill: Any = None
        self._spill_path: Optional[Path] = None
        self._spill_bytes = 0
        # ⭐ 总行数**单独计数**：`len(self._buf)` 封顶 300，它答不了
        #    「一共产出了多少行」，而那正是要如实告诉模型的东西。
        self._total_lines = 0

    # ── 读 ────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def tail(self, lines: int = 20) -> str:
        with self._lock:
            buf = list(self._buf)
        return "\n".join(buf[-lines:])

    def _append(self, line: str) -> None:
        _clean = line.rstrip("\r\n")
        with self._lock:
            self._buf.append(_clean)
            self._total_lines += 1
        # ⭐ 同一行的第二个去向。**必须在这里**，不能等跑完再从缓冲写 ——
        #    数据就是在上面那行 `append` 里被 deque 挤掉的。
        self._spill_write(_clean)

    # ══════════════════════════════════════════════════════════════════
    # 全量落盘 —— 「到底输出了什么」
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐⭐ **同一行输出有两个去向，它们服务两个不同的问题：**
    #
    #     deque(maxlen=300)  内存，只留尾巴   →「现在怎么样了」要【快】和【新】
    #     追加写文件          磁盘，全量不丢   →「到底输出了什么」要【全】
    #
    #   📌 而 `out[-2000:]` 之所以像个 bug，根子就在这：
    #      **它在一个只服务「现在怎么样了」的数据结构上，
    #      去取「到底输出了什么」的答案。**
    #
    # 🔴🔴 **必须边读边写，不能等跑完再从缓冲写文件。**
    #    数据是在 `self._buf.append()` 那一行丢掉的（deque 满了自动丢最老的）。
    #    等命令跑完再想落盘，手上只剩最后 300 行 —— 要救的那几千行早就不存在了。
    #    ⇒ 落盘发生在**行到达的那一刻**，和入缓冲是同一个动作的两个去向。
    #
    # ⭐ **形状抄的是 Claude Code 自己**（2026-08-26 实测它的行为）：
    #      Output too large (152.3KB). Full output saved to: <path>
    #      Preview (first 2KB): ...
    #    它**不截断**，它落盘 + 给路径。而拿到路径之后，模型用 grep / 读某一段 /
    #    只要末尾，**全由它自己按实际情况决定** ——
    #    📌 我们不该替它设计检索方式：向前翻页那种 API 等于我们替它决定了
    #       「只能从尾往前顺序找」，而那个极端例子（10 万字里只要末尾 2100）
    #       用 grep 一步就到。
    #
    # ⚠️ 与截图目录的区别（2026-08-26 定的那条判据）：
    #    这些文件是 **Nano 产的**，不是用户的东西 ⇒ 我们按预算自己删，
    #    **不给用户报账**（用户不认得它们，报了是噪音）。

    def _spill_open(self) -> None:
        """第一次真的有输出时才建文件 —— 没输出的命令不该留下空文件。"""
        if self._spill is not None:
            return
        try:
            _SPILL_DIR.mkdir(parents=True, exist_ok=True)
            self._spill_path = _SPILL_DIR / f"cmdout_{self.ref}.txt"
            self._spill = open(self._spill_path, "a", encoding="utf-8",
                               errors="replace", buffering=1)   # 行缓冲
        except Exception as e:
            # ⚠️ 落盘失败**不许影响命令本身** —— 它是附加能力，不是主功能。
            #    📌 同「回收是家务，不该有能力让主功能失败」。
            logger.debug(f"[LongCmd] 输出落盘不可用（不影响命令）: {e}")
            self._spill = False          # False = 试过且失败，别再试

    def _spill_write(self, line: str) -> None:
        if self._spill is False:
            return
        if self._spill is None:
            self._spill_open()
            if self._spill is False:
                return
        try:
            self._spill.write(line + "\n")
            self._spill_bytes += len(line) + 1
        except Exception:
            try:
                self._spill.close()
            except Exception:
                pass
            self._spill = False

    def _spill_close(self) -> None:
        """关文件；**如果模型内联就能全看到，把文件删掉。**

        🔴 第一版是「有输出就建文件、跑完就留着」，实测当场暴露问题：
           `print('hello')` 也会在磁盘上留一个 12 字节的文件。
        📌 而按**字节**的预算**永远不会触发**清理（它们太小）——
           于是**文件数无限增长**。这是「按字节 vs 按数量」的反面：
           字节预算不约束文件个数，而几万个小文件本身就是问题。
        ⭐ 判据很直接：**模型已经全看到了的输出，那个文件没有存在的理由。**
           留着它既不会被读，也不会被回收。
        ⚠️ 保守一点：`_spill_bytes` 是字节、`_INLINE_TAIL_CHARS` 是字符，
           非 ASCII 时字节更多 —— 朝「多留一个文件」错，不朝「删掉还需要的」错。
        """
        if self._spill and self._spill is not False:
            try:
                self._spill.close()
            except Exception:
                pass
        if (self._spill_path is not None
                and self._spill_bytes <= _INLINE_TAIL_CHARS
                and self._total_lines <= _MAX_LINES):
            try:
                self._spill_path.unlink()
            except OSError:
                pass
            self._spill_path = None

    @property
    def spill_path(self) -> Optional[str]:
        """完整输出的文件路径；没落盘过则 None。"""
        if self._spill_path is None or self._spill is False:
            return None
        return str(self._spill_path)


    # ── 进度描述（给模型看的那一眼）────────────────────────────────────
    def progress_note(self, lines: int = 20) -> str:
        """回看时给模型看的一段话。

        ⚠️ **如实说清「有」和「没有」**：有输出就给尾巴，没输出就明说没有 ——
           📌 一个「进度」字段在没有进度时必须说「没有」，
              不许给一个看起来像进度的空值（那会让模型编一个进展出来）。
        """
        _tail = self.tail(lines)
        head = (f"command: {self.display}\n"
                f"elapsed: {self.elapsed:.0f}s\n"
                f"state: {'still running' if self.running else 'finished'}")
        if not self.running:
            head += f" (exit code {self.returncode})"
        if self.killed_reason:
            head += f"\nkilled: {self.killed_reason}"
        if _tail:
            return head + f"\nlast output lines:\n{_tail}"
        return head + ("\nlast output lines: (nothing on stdout/stderr yet — "
                       "that by itself does not mean it is stuck; "
                       "some tools buffer their output)")

    # ── 收尾 ──────────────────────────────────────────────────────────
    def final_result(self) -> Dict[str, Any]:
        """跑完之后的结果，**形状与旧 `run_command` 一致**（调用方不用改）。

        🔴🔴 **旧实现是 `out[-2000:]`，而且是静默的。**
           2000 这个数**不是**防撑爆的保护（2000 字符 ≈ 500 token，而外面
           `MemoryManager.MAX_SINGLE_TOOL_RESULT_CHARS=12000`、上下文是 20 万
           token 级 —— **差两个数量级**）。它真正的语义是
           **「命令输出最有价值的部分通常在末尾」** —— 那一半是对的。
           ⚠️ 错的是它**把剩下那 5% 直接堵死了**，而且**不告诉任何人**：
              模型拿到 2000 字符，不知道后面还有，会当成完整结果去推理。
              📌 与本仓 inbox `delivery_count` / `ActionAttempt` INTERRUPTED
                 同一条判据：**截断可以，不说截断了不行。**

        ⭐ 现在：末尾仍然优先给（那一半对），但**同时如实交代三件事**：
             一共多少行/多少字节  ·  内联的只是尾巴  ·  完整的在哪个文件里
           而**怎么找由模型自己决定** —— grep、只读某一段、只要末尾，
           📌 我们不该替它设计检索方式（向前翻页那种 API 等于替它决定了
              「只能从尾往前顺序找」，而「10 万字里只要末尾 2100」用 grep
              一步就到）。
        """
        out = self.tail(_MAX_LINES)
        ok = self.returncode == 0 and not self.killed_reason
        err = ""
        if self.killed_reason:
            err = f"command was stopped: {self.killed_reason}"
        elif self.returncode != 0:
            err = f"command returned non-zero code: {self.returncode}"

        inline = out[-_INLINE_TAIL_CHARS:]
        truncated = len(out) > len(inline) or self._total_lines > _MAX_LINES
        note = ""
        if truncated:
            _p = self.spill_path
            note = (
                f"\n\n[output truncated - you are seeing the LAST "
                f"{len(inline)} characters of {self._total_lines} line(s)"
                f"{f', {self._spill_bytes} bytes total' if self._spill_bytes else ''}.]"
            )
            if _p:
                # ⚠️ 如实说清**能拿到什么**，而不是只说「被截断了」——
                #    📌 一个只说「你少了东西」却不说「怎么拿」的提示，
                #       只会让模型在原地重试。
                note += (
                    f"\nThe COMPLETE output is saved at:\n{_p}\n"
                    f"Read or search that file if the part you need is not above "
                    f"(grep for an error string, or read a specific range) - "
                    f"do NOT re-run the command just to see more output."
                )
            else:
                # 🔴 落盘失败时**必须说没有**，不能给一个不存在的路径。
                #    📌 「失败信息必须正确」——指向一个不存在的文件
                #       比不指路更糟：模型会去读，然后拿到第二个错误。
                note += ("\nThe full output could NOT be saved to disk this time, "
                         "so the earlier part is gone. If you need it, re-run the "
                         "command with a narrower filter.")

        return {"ok": ok,
                "data": {"returncode": self.returncode,
                         "output": inline + note,
                         "truncated": truncated,
                         "total_lines": self._total_lines,
                         "output_file": self.spill_path or ""},
                "summary": (f"command finished (returncode={self.returncode}) "
                            f"after {self.elapsed:.0f}s: {out[-200:]}"),
                "error": err}

    def kill(self, reason: str) -> None:
        """停掉这条命令。**只由用户/模型主动触发**，系统不会自己调。

        ⚠️⚠️ **必须杀整棵进程树，不能只 `popen.kill()`。**
           🔴 `shell=True` 时直接子进程是 `cmd.exe`，**真正的命令是它的孩子** ——
              只杀 `popen` 会留下一个还在跑的孙子进程，而调用方以为停掉了。
           📌 **一个「停掉」的操作，如果只停掉了它能直接够到的那一层，
              比不提供这个操作更坏** —— 用户会以为已经停了。
        ⚠️ Windows 上用 `taskkill /T /F`（/T = 连同子树）；失败再退回 `popen.kill()`
           —— 退回是为了「至少停掉一层」，但会**如实记进日志**。
        """
        if not self.running:
            return
        self.killed_reason = reason
        _tree_ok = False
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/PID", str(self.popen.pid), "/T", "/F"],
                    capture_output=True, timeout=10)
            _tree_ok = True
        except Exception as e:
            logger.warning(f"[LongCmd] taskkill 进程树失败 {self.ref}: {e}")
        try:
            self.popen.kill()
        except Exception as e:
            if not _tree_ok:
                logger.warning(f"[LongCmd] 杀 {self.ref} 失败: {e}")


_LIVE: Dict[str, LiveCommand] = {}
_LIVE_LOCK = threading.Lock()


def stop(ref: str, reason: str = "stopped by request") -> bool:
    """按 ref 停掉一条还在跑的命令。**给模型/用户的出口。**

    🔴 在此之前 `kill()` 只被两个地方调：30 分钟看门狗、stdout 关闭 ——
       **模型完全够不着**。于是「回看发现这个办法坏了，我换一个」是一句空谈：
       它能停掉的只有**自己的等待**，那条命令还在那儿跑，
       换了新办法之后**两个进程同时在跑**。
    📌 项目自己的判据：**每一个能被创建的状态，都必须有一条用户能主动结束它的路径，
       而且那条路径要在用户能触及的地方**（说一句话，而不是去找一个按钮）。

    返回 True = 找到了并且下了停止指令；False = 那条已经不在了（多半刚跑完）。
    ⚠️ 返回 False **不是错误** —— 调用方要照实说「它已经结束了」，
       而不是说「停止失败」。📌 两者对模型的下一步完全不同。
    """
    with _LIVE_LOCK:
        lc = _LIVE.get(ref)
    if lc is None or not lc.running:
        return False
    lc.kill(reason)
    logger.info(f"[LongCmd] {ref} 已按请求停止：{reason}")
    return True


def start(cmd: Any, *, shell: bool, display: str = "",
          hard_deadline_sec: float = _HARD_DEADLINE_SEC,
          cwd: Any = None, env: Any = None) -> LiveCommand:
    """起一个命令，**立刻返回**，输出由后台线程持续读进缓冲。

    ⭐ `cwd` / `env` 是临时执行通道加的（2026-08-26），**默认 None
       = 原样继承**，所以现有调用方一个字都不用改。
       它们存在的理由是隔离：临时代码要跑在一个**受控目录**里、并且
       **拿不到项目的 `PYTHONPATH`** —— 否则它能 `import core.*`，
       那等于开了一个后门。
       📌 「依赖可用」和「能 import 项目代码」是两件事：前者靠**同一个解释器**
          （`sys.executable`）拿到，后者靠 cwd/env 挡住。
    """
    popen = subprocess.Popen(
        cmd, shell=shell,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        cwd=cwd, env=env)
    lc = LiveCommand("cmd_" + uuid.uuid4().hex[:10],
                     display or (cmd if isinstance(cmd, str) else " ".join(map(str, cmd)))[:80],
                     cmd, popen)
    with _LIVE_LOCK:
        _LIVE[lc.ref] = lc

    def _pump() -> None:
        # ⚠️ 逐行读到进程结束。**读线程同时负责硬上限** ——
        #    把「杀掉」放在读的地方，是因为它是唯一确定在跑的那条线程。
        #    📌 一个「超时要杀掉」的责任，放在离进程最近的那条路径上最可靠。
        try:
            if lc.popen.stdout is not None:
                for line in lc.popen.stdout:
                    lc._append(line)
                    # 🔴🔴 **这里曾经在 30 分钟到点时 `lc.kill()`，2026-08-22 删掉。**
                    #
                    # 定下的铁律：**任何情况下都没有权力主动杀掉用户的进程。**
                    #   想象一下：这玩意 31 分钟装完，而我们 30 分钟把它杀了。
                    #
                    # 📌 它答的是「跑这么久正不正常」——**那是判断，不是事实**，
                    #    和 `_FIRST_RECHECK_SEC` 那条同一个错：
                    #    「常量只该承担系统答得出的那个问题；『这件事还要多久』
                    #      只有模型能答 —— 任何固定数字都覆盖不了真实情况。」
                    #    装一个大安装包跑 40 分钟是正常，`ls` 跑 40 分钟是出事了，
                    #    **同一个数字答不了这两个**。
                    #
                    # ⭐ 正确的分工：
                    #    · 系统答「它跑了多久 / 监护机制还在不在」（事实）
                    #    · 模型答「这么久是不是出事了 / 要不要停」（判断）
                    #      → 模型的出口是 `stop_background`（本轮新建）
                    #
                    # ⚠️ `hard_deadline_sec` 参数**保留**（调用方仍可传），
                    #    但默认值提到「不可能是合理任务」的量级，职责收窄成
                    #    **纯防泄漏**，不再承担「合不合理」。
                    if hard_deadline_sec and lc.elapsed > hard_deadline_sec:
                        logger.warning(
                            f"[LongCmd] {lc.ref} 已跑 {lc.elapsed:.0f}s，"
                            f"超过防泄漏上限 {hard_deadline_sec:.0f}s → 停止读取输出，"
                            f"**不杀进程**（它是用户的进程）")
                        break
                        break
        except Exception as e:
            lc._append(f"[reader error] {e}")
        try:
            lc.returncode = lc.popen.wait(timeout=5)
        except Exception:
            lc.kill("process did not exit after stdout closed")
            try:
                lc.returncode = lc.popen.wait(timeout=5)
            except Exception:
                lc.returncode = -1
        lc.finished_at = time.time()
        # ⚠️ 先关文件再打日志：`final_result()` 可能在这之后立刻被读，
        #    行缓冲虽然基本能保证内容已落盘，但显式 close 才是有保证的。
        lc._spill_close()
        logger.info(f"[LongCmd] {lc.ref} 结束（code={lc.returncode}，"
                    f"{lc.elapsed:.0f}s）：{lc.display[:50]}")
        # ⭐ 回收挂在「命令结束」这条心跳上 —— 同截图那条判据：
        #    **回收该由拥有这个目录的人负责**，而这个目录就是 longcmd 自己的。
        #    📌 而且它天生对齐：产生文件的动作和触发回收的动作是同一个。
        _prune_spill_dir()

    threading.Thread(target=_pump, name=f"longcmd-{lc.ref}", daemon=True).start()
    logger.info(f"[LongCmd] {lc.ref} 起来了：{lc.display[:60]}")
    return lc


def get(ref: str) -> Optional[LiveCommand]:
    with _LIVE_LOCK:
        return _LIVE.get(ref)


def progress(ref: str, lines: int = 20) -> str:
    """给回看用：这条命令现在什么情况。找不到就返回空串。"""
    lc = get(ref)
    return lc.progress_note(lines) if lc is not None else ""


def join(ref: str, poll: float = 0.4) -> Dict[str, Any]:
    """阻塞等它结束，返回旧形状的结果。**给 `asyncio.to_thread` 用。**"""
    lc = get(ref)
    if lc is None:
        return {"ok": False, "data": {}, "summary": "",
                "error": f"no such live command: {ref}"}
    while lc.running:
        time.sleep(poll)
    return lc.final_result()


def wait_briefly(lc: LiveCommand, seconds: float, poll: float = 0.05,
                 stop_when=None) -> bool:
    """前台只等这么久。返回「是不是已经跑完了」。

    ⚠️ 这个 `seconds` 是「**前台愿意等多久**」，不是「这件事的期限」——
       跑完 → 快路径，原样返回；没跑完 → 交回控制权，命令继续跑。
       📌 **闸的出口是失败，队列的出口是稍后处理**（第六次）。

    `stop_when`：一个「别等了」的判据。返回真就**立刻停止等待**（命令继续跑）。

    ⭐⭐⭐ 为什么要有它 —— 实际运行中那次故障的第二个症状：
       用户在命令跑着的时候说「算了别做了」，而 Nano **80 秒都停不下来**。
       📌 **前台等待的耐心，本质上是「现在没有比等它更值得做的事」** ——
          用户一说话，这个前提就不成立了。所以这不是「加一个中断」，
          是把那个耐心的真实条件写出来。
    ⚠️ `stop_when` 抛异常一律当成「没有理由停」（fail-safe 方向是**继续等**）——
       方向由代价决定：错停一次是白交还一次（廉价），
       错不停一次是用户干等（就是那次的体验）。
    """
    deadline = time.time() + max(0.0, seconds)
    while lc.running and time.time() < deadline:
        if stop_when is not None:
            try:
                if stop_when():
                    break
            except Exception:
                pass
        time.sleep(poll)
    return not lc.running


_turn_stop_probe = None


def set_turn_stop_probe(fn) -> None:
    """登记「用户按了终止吗」的判据（orchestrator 的 `_stop_asked`）。前台等待据此提前结束。"""
    global _turn_stop_probe
    _turn_stop_probe = fn


def turn_stop_requested() -> bool:
    """这一轮是不是被用户终止了（读不出来按「没有」）。"""
    try:
        return bool(_turn_stop_probe()) if _turn_stop_probe is not None else False
    except Exception:
        return False


def foreground_interrupt():
    """前台等待的提前结束判据：用户又说话了，或者用户按了终止（裁决 73）。

    调用方在等待结束后用 `turn_stop_requested()` 区分两者：插话 → 交还，命令继续跑；
    终止 → 这一轮前台正在执行的一起停。
    """
    _input = new_user_input_arrived()
    return lambda: bool(turn_stop_requested() or (_input is not None and _input()))


def new_user_input_arrived():
    """做一个「用户又说话了吗」的判据（给前台等待用）。

    ⭐ 读的是 `inbox.submit_seq()` —— 收到过多少条用户消息，单调递增。
       在**等待开始时**取基线，之后变了就说明有新消息进来了。
    ⚠️ 刻意**不**去问「是不是插话」「要不要中断本轮」—— 那些是 orchestrator
       的结论。这一层只需要知道一件事实：**有新的用户输入了，所以
       「没有更值得做的事」这个前提不成立了。**
       📌 **一个层只该读它答得出的那个事实，不该去读别人的结论。**
    ⚠️ 读不出来就返回 None（fail-safe 方向 = 继续等）。

    🔴 **它原来长在 `executor_write` 上**（2026-08-26 搬来）。搬的理由：
       临时执行通道也要用**同一个判据**，而消费它的
       `await_briefly(stop_when=)` 就在本模块。
       📌 **判据只能有一处** —— 两份「用户说话了吗」迟早会分叉，而分叉的
          表现不是报错，是「命令那条路会被插话打断，临时代码那条不会」。
    """
    try:
        from core.runtime import inbox as _ib
        _base = _ib.submit_seq()
        return lambda: _ib.submit_seq() > _base
    except Exception:
        return None


async def await_briefly(lc: LiveCommand, seconds: float, poll: float = 0.05,
                        stop_when=None) -> bool:
    """`wait_briefly` 的异步版。**不占线程池，也不堵事件循环。**

    ⚠️ 原来这条路是 `asyncio.to_thread(_run_command_sync)` —— 一个线程被
       整整占住 45 秒只为了睡觉。而更要紧的是：**线程里没法便宜地看
       「用户是不是又说话了」**，那个信号在 asyncio 侧。
       📌 **一个只是在等的操作，不该占用一个能干活的线程。**
    """
    import asyncio as _aio
    deadline = time.time() + max(0.0, seconds)
    while lc.running and time.time() < deadline:
        if stop_when is not None:
            try:
                if stop_when():
                    break
            except Exception:
                pass
        await _aio.sleep(poll)
    return not lc.running


def _prune_spill_dir() -> None:
    """命令输出目录按**体积**回收。

    ⭐ 与截图那份**故意共用同一套思路**（预算触发 / 删最旧 / 不许影响主功能），
       但**刻意不共用代码**：现在只有两个实例，而它们的资源、寿命、地板语义
       都不同（截图是审计证据要留久，命令输出读完就没用）。
       📌 过早抽象 = 接口长成第一个实例的形状，第二个来时只能削足适履。
       ⇒ 等第三个实例出现时再看要不要抽公共基建。

    ⚠️ **没有地板**，这也是与截图的实质差别：截图是审计证据（「证据 > 磁盘」，
       宁可暂时超预算也不能清空）；命令输出**没有事后取证的价值** ——
       模型读完那一轮就没用了。所以这里删干净是安全的。
    ⚠️ 同样**不给用户报账**：这些文件是 Nano 产的，不是用户的东西
       （2026-08-26 定的判据）—— 报了用户也不认得，是噪音。
    """
    try:
        if not _SPILL_DIR.is_dir():
            return
        items = []
        for f in _SPILL_DIR.glob("cmdout_*.txt"):
            try:
                st = f.stat()
            except OSError:
                continue
            items.append((f, st.st_size, st.st_mtime))
        total = sum(sz for _f, sz, _m in items)
        if total <= _SPILL_BUDGET_BYTES:
            return
        items.sort(key=lambda t: t[2])          # 旧 → 新
        freed, n = 0, 0
        for f, sz, _m in items:
            if total <= _SPILL_BUDGET_BYTES:
                break
            # ⚠️ 还在跑的命令的文件**不能删** —— 它正被写着。
            if f.name[len("cmdout_"):-len(".txt")] in set(live_refs()):
                continue
            try:
                f.unlink()
            except OSError:
                continue
            total -= sz
            freed += sz
            n += 1
        if n:
            logger.info(f"[LongCmd] 输出目录回收：删 {n} 个、释放 "
                        f"{freed / 1024 / 1024:.1f} MB，剩 {total / 1024 / 1024:.1f} MB"
                        f"（预算 {_SPILL_BUDGET_BYTES / 1024 / 1024:.0f} MB）")
    except Exception as e:
        # 📌 回收是家务，不该有能力让命令失败。
        logger.debug(f"[LongCmd] 输出目录回收跳过: {e}")


def forget(ref: str) -> None:
    """结果已经被取走 → 从注册表移除（避免长会话里无限堆积）。

    ⚠️ 只移除**已结束**的：还在跑的移掉就等于把它的进度和结果一起丢了。
       📌 一个「清理」动作不许有任何路径能碰到还活着的东西。
    """
    with _LIVE_LOCK:
        lc = _LIVE.get(ref)
        if lc is not None and not lc.running:
            _LIVE.pop(ref, None)


def live_refs() -> list[str]:
    with _LIVE_LOCK:
        return [r for r, lc in _LIVE.items() if lc.running]


# ══════════════════════════════════════════════════════════════════════════
# 接进进度总线
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 走**拉**（provider）而不是推：命令的输出已经在 `LiveCommand._buf` 里，
#    那份就是权威。往总线再抄一份的话，两份会在「丢头」的时机上分叉。
#    📌 **不许为一个已经存在的权威再存一份副本。**
#
# ⚠️ 注册放在模块底部而不是函数里：它必须在**任何人问进度之前**就完成，
#    而模块导入是唯一能保证这一点的时机。`register_provider` 同名覆盖，
#    所以重复导入是安全的（幂等）。
try:
    from core.runtime import progress as _prog_bus

    _prog_bus.register_provider("longcmd", lambda ref: progress(ref, lines=20) or None)
except Exception:  # pragma: no cover - 总线缺失不该拖垮命令执行
    pass
