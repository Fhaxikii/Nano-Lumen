# core/temp_exec.py
"""临时执行通道 —— 跑一段用完就扔的 Python。

═══ 它补的是哪个出口 ═══

模型遇到「需要跑段代码，但不值得建 Skill」时，此前只剩三条路，**全是死路**：
  · 硬建永久 Skill  —— `create_new_skill` 的提示词**明令禁止**用于一次性任务，
                       而且会污染 Skill 池
  · 走 `os_execute(run_command)` —— 语义是「操作电脑」不是「帮我算个东西」，
                       风险地板写死 3（**每次弹窗**），`shell=True`，无隔离
  · 自己心算然后编   —— persona 明令禁止，但**这是阻力最小的那条路**
📌 而 `create_new_skill` 禁止了一次性用途，**却没有给任何替代出口** ——
   记的那个形状：**模型需要的是一个出口，不是一个名字。**

═══ 五个设计决定 ═══

① **子进程 + `sys.executable`（同一个解释器）**
   ⭐ 这一条同时解决了「执行环境」和「依赖可用性」两个硬问题：
      同解释器 ⇒ pandas / openpyxl / httpx **全部自动同等待遇**，
      不需要跟 Nano 同进程就拿到了共环境。
   📌 「依赖可用」和「能 import core」一度被当成一件事，**其实是两件**：
      前者靠同解释器拿到，后者靠 cwd + 干净 env 挡住。

② **不接上游 `tool_data`** —— 不是推迟，是**不该做**。
   🔴 原本的前提是「模型得把表的内容抄进代码里」——**错的**：
      临时代码跑在同一台机器上，模型传的是**路径**（`pd.read_excel(path)`）。
      而「本地数据靠路径传」在本项目早就是定好的路：
      `InputDef(type="file_path")` + `_validate_file_path_params` + `get_file_path`。
   ⇒ 再造一条 = **两个通道通向同一件事**（本仓反复被咬的形状）。

③ **副作用确认走【与正式 Skill 完全一样】的那条路。**
   直接跟正常 Skill 的体感一致就行；原本设计的
   「risk 1/2/3 + 白名单」是在**发明一套新东西**。
   ⇒ AST 扫出副作用 → 弹**同一个** `execution_confirm`（同一套词表、同样
     300 秒、同样区分「用户改口 / 干等超时」）；扫不出 → 直接跑。
   ⚠️ 扫描器也是**同一份**（`core/code_scan.py`），不是第二套。

④ **代码可见性：风险决定【打断】，但【永远可查】。**
   · 弹窗里带代码 = 让用户知道**自己在授权什么**（事前）
   · 工具卡可展开 = 让用户知道**刚才发生了什么**（事后）
   🔴🔴 **弹窗里的编辑器必须只读。** 理由不只是保险：
      **风险等级是 AST 在【那段代码】上扫出来的，用户一改，授权就和
      被授权的东西对不上了** —— 用户能把一段「无副作用、不弹窗」的代码
      改成写文件的，而弹窗上还挂着旧结论。**可编辑会让授权失去意义。**
      ⚠️ Skill 那边能改，是因为改完**还会再过一遍审计管线**；
         一次性执行**没有第二遍**。

⑤ **输出复用 `longcmd`** —— 内联末尾 + 超了落盘给路径。
   ⭐ 所以这个模块**不自己管输出**：长任务交还、进度回看、全量落盘、
      按体积回收，全部白拿，而且**行为与 `run_command` 完全一致**
      （模型不用学两套）。

═══ ⚠️ 它不是沙箱 ═══

AST 扫描抓不到 `__import__("os").system(...)` 这类间接调用；子进程跑在
用户自己的权限下。📌 **真正的边界是「用户看得见代码」**，不是扫描结果。
⭐ 而这个风险水平**与正式 Skill 相同**（Skill 也是扫不出来就不弹），
   已经跑了很久；临时代码在可见性上**只多不少**。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from core.code_scan import scan

# 临时代码的落脚点。⚠️ **不是项目根目录** —— cwd 设在这里，
# 相对路径的读写都落在这儿，而不是污染用户的项目。
_SCRATCH_DIR = Path(tempfile.gettempdir()) / "nano_scratch"

# ⚠️ 从 env 里摘掉的：让子进程**看不见项目代码**。
#    📌 只挡 `PYTHONPATH` 是不够的 —— `PYTHONHOME` 会换掉整个标准库定位，
#       `PYTHONSTARTUP` 会在启动时执行一个文件。三个都得摘。
_STRIP_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    # ⭐ 强制 UTF-8：Windows 默认 GBK，模型写的代码里但凡有个中文 print
    #    就会 UnicodeEncodeError —— 而那个报错跟代码本身毫无关系，
    #    📌 会把模型引向一个不存在的问题（「我的代码有什么错？」）。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # ⚠️ 不写 .pyc：临时代码跑一次就扔，`__pycache__` 是纯垃圾。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def prepare(code: str) -> tuple[list[str], list[str], str]:
    """执行前的检查。返回 `(副作用类别, 原始发现, 错误信息)`。

    ⚠️ **语法错误在这里就拦住**，不要等跑起来才发现：
       📌 让模型拿到 `SyntaxError: invalid syntax (line 3)` 这一句，
          比让它拿到一个非零退出码 + 一段 traceback 快得多 ——
          而失败信息的要求正是「足够让模型选出下一步」。
    """
    return scan(code)


def start(code: str, *, display: str = "") -> Any:
    """把代码写成临时文件并起一个子进程，返回 `LiveCommand`。

    ⭐ 走 `longcmd` 而不是自己 `Popen`：长任务交还 / 进度回看 / 输出落盘 /
       按体积回收 **全部白拿**，而且行为与 `run_command` 一致。
    """
    from core.os_layer import longcmd as _lc

    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    ref = uuid.uuid4().hex[:10]
    path = _SCRATCH_DIR / f"scratch_{ref}.py"
    path.write_text(code, encoding="utf-8")

    # ⚠️ `-X utf8` 与 env 里的 `PYTHONUTF8` 是**两道保险**：命令行参数在
    #    某些嵌入式启动方式下会被忽略，env 不会。
    # ⚠️ `-I`（isolated）会同时屏蔽 site-packages —— **不能用**，
    #    📌 那会把 pandas / openpyxl 一起挡掉，而「依赖可用」正是①的目的。
    cmd = [sys.executable, "-X", "utf8", "-u", str(path)]
    lc = _lc.start(cmd, shell=False,
                   display=display or f"scratch code ({len(code)} chars)",
                   cwd=str(_SCRATCH_DIR), env=_clean_env())
    logger.info(f"[TempExec] {lc.ref} 起来了：{len(code)} 字符 → {path.name}")
    return lc


def cleanup(older_than_sec: float = 3600.0) -> int:
    """清掉过期的临时代码文件。返回删掉几个。

    ⚠️ 与截图/命令输出那两处**不同**：这里按**时间**不按体积，因为
       📌 临时代码文件是几 KB 级的 —— **体积从来不会是它的问题，
          而文件个数会**。用体积做判据的话，它永远触发不了（同命令输出
          那个「小文件泛滥」的坑）。
    ⭐ 所以判据要来自「它实际会造成的那个问题」——这条是通用的：
       **清除条件应来自它消耗的那个资源**；只是这里消耗的不是磁盘容量，
       是目录里的条目数，而对应的自然判据就是「跑完就没用了」= 时间。
    ⚠️ 不删还在跑的（它的进程正指着那个文件）。
    """
    import time
    from core.os_layer import longcmd as _lc
    n = 0
    try:
        if not _SCRATCH_DIR.is_dir():
            return 0
        live = set(_lc.live_refs())
        cutoff = time.time() - older_than_sec
        for f in _SCRATCH_DIR.glob("scratch_*.py"):
            try:
                if f.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if any(r in f.name for r in live):
                continue
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    except Exception as e:
        # 📌 回收是家务，不该有能力让执行失败。
        logger.debug(f"[TempExec] 临时文件清理跳过: {e}")
    return n
