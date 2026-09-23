# core/proactive/referent.py
"""窗口 → **可操作的句柄**（referent）。

═══════════════════════════════════════════════════════════════════════════
它要解决的那件事
═══════════════════════════════════════════════════════════════════════════
Ambient 一直只产出**叙述文本**（给人看的一句话），不是**句柄**（给工具用的参数）。
2026-08-03：

> "nano 虽然能知道我改过文件这个动作，但要无缝接上似乎还缺一环 —— 它压根拿不到路径。"

🔴 而 2026-08-26 实际运行坐实了这个缺口**不是「少一个功能」，是会产出自信的错答案**：
   用户说「帮我补全我刚才编辑的那个 txt」，Ambient 只能说「他在 notepad」，
   「哪个文件」那个空 Nano 拿**对话历史**填了 —— 填成上一轮聊过的 `西游记.txt`。
   📌 **给它一个没有句柄的叙述，它就会拿别处的东西补那个空。**

═══════════════════════════════════════════════════════════════════════════
⭐⭐ 分割点：**「是不是用户的东西」**，不是「文件类型」
═══════════════════════════════════════════════════════════════════════════
追问过「哪类东西带路径是有意义的，这个判断分割点在哪」。答案是本项目已经定过的
那条（截图回收那次）：**「这到底是不是用户的东西」**。

    notepad.exe 的路径   C:\\Windows\\System32\\notepad.exe    ← 厂商的，零价值
    它打开的那个 txt      C:\\Users\\...\\新建文本文档 (2).txt   ← 用户的，就是他指的那个

⭐ 而这个分割点**不需要新定义，它已经存在**了 —— 就是 `parse_title()` 抠不抠得出名字：

    editor / office              抠出文件名    ⇒ 带路径
    file(explorer)               窗口标题=文件夹名 ⇒ 带目录路径
    browser                      抠出页面标题  ⇒ **只到这里**，不抓网址
                                 （2026-08-27 定：跨机器不可靠，
                                   而且网址跟路径本来就是两码事）
    im / meeting / media / other 抠不出       ⇒ 不带（带了也是噪音）

⚠️ `terminal` 落在缝里：它的 referent 是**命令+输出**而不是路径，而窗口标题拿不到
   内容。**这是技术限制，不是分类问题** —— 所以这里不为它硬凑一个路径。

═══════════════════════════════════════════════════════════════════════════
🔬 四个信号源的实测矩阵（2026-08-26 当场跑的，全部推翻/修正了早先设想的技术路线）
═══════════════════════════════════════════════════════════════════════════
                      双击文件打开          先开应用再打开文件
    cmdline           ✅ 完整绝对路径        ❌ 只有 'notepad.exe'
    cwd               ⚠️ **启动者**的工作目录 —— 不可信
                         实测：从脚本起 = 那个脚本的 cwd
                               explorer.exe = C:\\Windows\\system32
    open_files        ❌ 只有 .mui/.dat/.mun ❌ 同左
    窗口标题           ✅ 文件名              ✅ 文件名

🔴 **早先设想的技术路线是 `psutil.Process(pid).open_files()`** —— 实测在记事本上
   **一个用户文档都拿不到**（现代应用读进内存就关句柄）。真正给出路径的是
   `cmdline()`，早先的设想里一个字没提。
🔴 **`cwd` 是陷阱**，绝不能用来拼路径。

⭐ 所以这里用**交叉验证**，而不是二选一：
       窗口标题   当前正在编辑的文件【名】      权威在"当前"，但没有路径
       cmdline    启动时打开的文件【完整路径】  权威在"路径"，但可能过时
                                                （开着记事本换一个文件，命令行不会变）
       两边对得上 ⇒ confirmed；对不上/没有 ⇒ 只给名字，**明说没确认**
📌 而「明说没确认」正是这次实际运行暴露的问题的解法：它猜桌面猜中了，
   **而没有任何人知道那是猜的**。同「说清结果可不可信」那条判据。

⚠️ **explorer 必须用 hwnd，不能用 pid**：实测两个资源管理器窗口 pid 可以不同
   （4872 / 691932），但默认设置下它们**共用一个 explorer 进程** ——
   📌 一个「有时候能区分」的标识，等于不能区分。
"""
from __future__ import annotations

import pathlib
import re
import urllib.parse
from typing import Optional

from loguru import logger

from core.proactive import app_catalog as _app_catalog

KIND_FILE = "file"
KIND_DIR = "dir"
#: 🪦 `KIND_URL` 已删（2026-08-27）：浏览器停留在互动感层，不产出句柄。

#: 浏览器进程名（与 hooks 共用同一份，见下方 `browser_procs()`）。
_BROWSER_PROCS = {"chrome", "msedge", "firefox", "brave", "opera", "iexplore"}


def browser_procs() -> set:
    """⚠️ **单一出处** —— hooks 原来自己抄了一份同样的集合。"""
    return set(_BROWSER_PROCS)


def cat(app: str) -> str:
    return _app_catalog.categorize(app) or "other"


def parse_title(app: str, title: str):
    """窗口标题 → (文件名, 项目名, 页面标题)，启发式。

    ⭐⭐ 2026-08-26：**`office` 与 `editor` 走同一条**（实际运行中抓到的）。

    🔴 问题：`data/ambient_trail.jsonl` 里真实躺着的是光秃秃一行 **`"in notepad"`** ——
       而记事本的窗口标题**本来就是** `新建文本文档 (2).txt - 记事本`，
       **文件名一直在标题里，只是没人去抠。**
    ⚠️ 拦住它的是**分类**，不是「路径没做」：`notepad`/`winword`/`wps`/`excel`
       全在 `OFFICE`，而这里原来只认 `editor`/`browser`，office 直接空手返回。
    🔴 而验收场景正好撞在这一类上（「你继续把这个 word 搞完吧」）——
       **原本以为只差路径，实际连「是哪个文件」都过不了。**

    ⭐ 两类的标题格式**本来就是同一种**，所以不需要第二套解析：
         editor  `foo.py - myproject - Visual Studio Code`
         office  `报告.docx - Word` / `新建文本文档 (2).txt - 记事本`
                 `笔记 - 我的库 - Obsidian`（三段，proj 那一支同样适用）
       ⚠️ 未保存时标题前缀是 `*` / `●`，由下面的 `strip(" ●*•◦")` 吃掉。

    ⚠️ 本函数原先长在 `orchestrator.py`。搬来是因为 `proactive` 层要用它，
       而 `proactive → orchestrator` 会成环。orchestrator 那边留转发。
       📌 删一段代码时，长在它身上的「为什么」要跟着搬到新家。
    """
    t = (title or "").strip()
    c = cat(app)
    if c in ("editor", "office") and " - " in t:
        # 标题以完整路径开头（Notepad++ 的默认格式）时，名字取文件名；
        # 路径由 title_path() 给出。整段按最后一个 " - " 切，路径里本身含 " - " 也不会断开。
        _tp = title_path(app, t)
        if _tp:
            return (pathlib.PureWindowsPath(_tp).name, "", "")
        segs = [s.strip(" ●*•◦") for s in t.split(" - ")]
        f = segs[0] if segs else ""
        proj = segs[1] if len(segs) >= 3 else ""
        return (f, proj, "")
    if c == "browser":
        for suf in (" - Google Chrome", " - Microsoft Edge", " - Mozilla Firefox",
                    " — Mozilla Firefox", " - Brave"):
            if t.endswith(suf):
                t = t[: -len(suf)]
        return ("", "", t.strip())
    return ("", "", "")


def title_path(app: str, title: str) -> str:
    """窗口标题以绝对路径开头时返回该路径（不检查是否存在），否则返回 ""。

    只对 editor / office 类生效。标题反映的是当前显示的文档，
    因此这个路径比进程命令行（启动时的快照）更可信。
    """
    if cat(app) not in ("editor", "office"):
        return ""
    t = (title or "").strip()
    if " - " not in t:
        return ""
    head = t.rpartition(" - ")[0].strip(" ●*•◦")
    try:
        p = pathlib.PureWindowsPath(head)
    except Exception:
        return ""
    if p.drive and p.is_absolute() and p.name:
        return head
    return ""


# ── URL：完整读一次，域名由调用方自己截 ──────────────────────────────────

def domain_of(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"^[a-zA-Z]+://", "", u)        # 去 scheme（omnibox 有时不带）
    u = u.split("/")[0].split("?")[0].strip()
    if u.lower().startswith("www."):
        u = u[4:]
    return u if ("." in u and " " not in u and len(u) <= 80) else ""


def browser_domain(hwnd: int = 0) -> str:
    """best-effort：UIA 读浏览器地址栏 → **只交出域名**。失败返回 ""。

    ═══ 🪦 这里曾经抓过完整 URL，2026-08-27 被否掉 ═══

    做过一版「采全 → 注入只给域名 → 用户指代时才取完整 URL」，实际也真的跑通过
    （日志实证：`url_len=57 domain='bilibili.com'`，回填成功）。**砍掉的理由不是
    它不work，是它不可靠**，而真正的判据比"可靠性"本身更硬：

    > 「就算我这台电脑咱们折腾半天抓出来了，**其他电脑不一定可靠**。」

    实测日志里 8 拍中有 5 拍 `url_len=0` —— UIA 读地址栏本来就随浏览器版本、
    语言、是否全屏而变。📌 **一个"在我机器上好使"的功能，比没有这个功能更坏**：
    它会在别人的机器上静默地什么都不给，而没有人知道为什么。

    ⭐ 而更根本的分界是这条：
    ```
    需要给源头的     本地可修改的文件（editor/office）+ 文件夹（explorer）
                     ⇒ 路径是「能拿去操作」的东西
    不需要给源头的   Claude Code / 游戏 / 浏览器（含网页）
                     ⇒ 网页名字够了，**网址跟路径本来就是两码事**
    ```
    ⇒ 浏览器停留在**互动感**层：只要页面名字（窗口标题里就有），不要网址。

    ⚠️ 保留下来的只有两点：① 按 `hwnd` 读而不是 `GetForegroundControl()`
    （后者实测在这台机器上根本读不到）；② 完整 URL **不出这个函数**。
    """
    try:
        import uiautomation as auto
    except Exception:
        return ""
    try:
        auto.SetGlobalSearchTimeout(0.6)
        # 🔴 **按 hwnd 读，不用 `GetForegroundControl()`**（2026-08-26 实测）。
        #
        #    问题：trail 里 explorer 那条拿到了完整目录路径，chrome 那条 `path` 却是空的。
        #    最初怀疑 COM 线程未初始化（`_resolve_dir` 加了 `CoInitialize` 而这里没加），
        #    **实测证伪** —— 主线程 / worker 无 CoInit / worker 有 CoInit，
        #    用 `ControlFromHandle(hwnd)` 三种都能读出完整 URL。
        #    ⇒ 挂的是 `GetForegroundControl()` 这条路本身。
        # 📌 与其继续查它为什么不行，不如**换到已经验证过能行的那条**。
        #
        # ⭐ 而且按 hwnd 读**本身就更对**：地址栏是 6 秒一采（UIA 贵），
        #    等这一拍到达时前台可能已经切走了 —— 那时 `GetForegroundControl()`
        #    读回来的是**别人的**地址栏，然后被写进浏览器那条记录里。
        #    📌 一个「读当下」的取法，配上一个慢节奏的采样，必然张冠李戴。
        win = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetForegroundControl()
        if not win:
            return ""
        edit = win.EditControl(searchDepth=12)   # Chrome/Edge 地址栏通常是首个 Edit
        if not edit.Exists(0.4, 0.1):
            return ""
        try:
            url = edit.GetValuePattern().Value or ""
        except Exception:
            url = edit.Name or ""
        # ⚠️ **完整 URL 到此为止** —— 只有域名出得去（见函数 docstring 的墓碑）。
        return domain_of((url or "").strip())
    except Exception:
        return ""
    finally:
        try:
            auto.SetGlobalSearchTimeout(10)
        except Exception:
            pass


# ── 文件：cmdline ∩ 窗口标题 ─────────────────────────────────────────────

def _names_match(a: str, b: str) -> bool:
    """两个文件名算不算同一个 —— **去扩展名后也算**。

    📌 Word 未保存时标题是 `文档1`，命令行里却是 `文档1.docx`；
       反过来某些应用标题带扩展名而命令行不带。
    ⚠️ 只放宽到「去扩展名」，不做模糊匹配 —— 一个宽松的匹配器会把
       `report.docx` 和 `report_old.docx` 判成同一个，而那正是我们要避免的
       「看起来确认了其实是猜的」。
    """
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return pathlib.Path(a).stem == pathlib.Path(b).stem


def _argv_path_candidates(args: list) -> list:
    """argv 尾巴 → 所有可能的路径写法。

    🔴🔴 **Windows 会把带空格的路径拆成多个 argv**（2026-08-26 实测抓到，
       100% 触发的那种）：

           注册表里 .txt 的打开命令是   NOTEPAD.EXE %1
                                                    ↑ **没有引号**

       于是双击 `新建文本文档 (7).txt` 之后，`psutil.cmdline()` 给出的是

           ['C:\\Windows\\system32\\NOTEPAD.EXE',
            'C:\\Users\\...\\Desktop\\新建文本文档',   ← 断在这
            '(7).txt']

       记事本自己没事，因为它用 `GetCommandLineW()` 读**原始整串**，
       根本不依赖 argv 拆分。而我们逐个参数看，于是：
         · `...\新建文本文档`  文件名对不上 → 跳过
         · `(7).txt`            不是绝对路径 → 跳过
       ⇒ **路径拿不到，全程没有任何报错。**

    ⚠️ 而 `新建文本文档 (N).txt` 正是 Windows 中文版**默认的新建文件名**，
       天生带空格 —— 📌 这个 bug 不是边角，它在最常见的那条路上必然踩中。
    ⚠️ 第一次实测没抓到它，是因为用了 `subprocess.Popen(['notepad.exe', 路径])`
       —— 那是直接传 argv 数组，**不经过 shell 那层拆分**。
       📌 **用一个近似场景去验真实场景，验过的和跑的可能不是同一条路。**

    ⇒ 所以把 argv 尾巴的**所有连续片段**都当候选（n 很小，≤ 几个）。
      真伪由调用方的「文件名对得上 **且** 文件真存在」双重判据兜住。
    """
    out = []
    for i in range(len(args)):
        for j in range(i + 1, len(args) + 1):
            seg = " ".join(x for x in args[i:j]).strip().strip('"')
            if seg:
                out.append(seg)
    # 长的优先：带空格的完整路径应该赢过它自己的前半截
    out.sort(key=len, reverse=True)
    return out


#: 在同一个窗口里用标签页承载多个文档的应用（进程名，小写，不含 .exe）。
#: 这些应用的命令行只记录启动时的第一个文件，需要先确认窗口里只有一个标签。
_DOC_TAB_APPS = {"notepad", "notepad++", "wps"}


def _process_windows(pid: int) -> list:
    """该进程可见、有标题、无 owner 的顶层窗口（对话框之类的附属窗口不计）。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        if not user32.IsWindowVisible(h) or user32.GetWindow(h, 4):  # GW_OWNER
            return True
        if not user32.GetWindowTextLengthW(h):
            return True
        _p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(_p))
        if _p.value == int(pid):
            out.append(int(h))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def _tab_count(hwnd: int) -> Optional[int]:
    """窗口里文档标签页的数量：没有标签栏返回 0，读取失败返回 None。

    经 UIA 找到第一个 TabItem，数它所在标签栏里 TabItem 的个数。
    """
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(int(hwnd))
        if not win:
            return None

        def _find(c, depth):
            for ch in c.GetChildren():
                if ch.ControlTypeName == "TabItemControl":
                    return c
                if depth < 10:
                    r = _find(ch, depth + 1)
                    if r is not None:
                        return r
            return None

        bar = _find(win, 0)
        if bar is None:
            return 0
        return sum(1 for ch in bar.GetChildren()
                   if ch.ControlTypeName == "TabItemControl")
    except Exception as e:
        logger.debug(f"[Referent] 读取标签页失败 hwnd={hwnd}: {e}")
        return None


def _single_document(app: str, pid: int, hwnd: int) -> bool:
    """进程里是否只开着一个文档。无法确定时返回 False。

    命令行只记录启动时打开的文件。进程之后再打开的文件（另一个窗口、另一个标签）
    与它同名时，按名字比对会把旧路径当成当前文档的路径。
    """
    try:
        wins = _process_windows(pid)
    except Exception:
        return False
    if len(wins) != 1:
        return False
    if (app or "").lower() in _DOC_TAB_APPS:
        n = _tab_count(hwnd or wins[0])
        return n is not None and n <= 1
    return True


def _resolve_file(app: str, pid: int, title: str, hwnd: int = 0) -> Optional[dict]:
    import psutil
    name, _proj, _page = parse_title(app, title)
    if not name:
        return None
    ref = {"kind": KIND_FILE, "name": name, "path": "",
           "confirmed": False, "app": app}
    # 标题里的完整路径属于当前显示的文档，直接采用；不再回退到命令行。
    _tp = title_path(app, title)
    if _tp:
        if pathlib.Path(_tp).exists():
            ref["path"] = _tp
            ref["confirmed"] = True
        return ref
    try:
        cmd = psutil.Process(int(pid)).cmdline()
    except Exception:
        return ref
    _args = [a for a in (cmd[1:] if len(cmd) > 1 else [])
             if a and not a.startswith("-") and not a.startswith("/")]
    for a in _argv_path_candidates(_args):
        try:
            _p = pathlib.Path(a)
            # ⚠️ **只认绝对路径** —— 相对路径要靠 cwd 拼，而 cwd 是启动者的
            #    （实测 explorer 启动的 = C:\Windows\system32）。
            #    📌 拼出来的路径看起来很像真的，而它是错的 —— 那正是最坏的一种。
            if not _p.is_absolute():
                continue
            if not _names_match(_p.name, name):
                continue
            # ⭐ **给出的路径必须真的打得开**，否则就是在骗人。
            #    命令行是启动那一刻的快照：文件可能已经被移走/删掉/改名。
            #    ⭐ 这一条同时是上面那个「所有连续片段」的安全网：
            #      拼错的片段既对不上文件名、也不会存在。
            if not _p.exists():
                continue
            # 名字对上还不够：进程里开着多个文档时，同名文件可能不是这一个。
            if not _single_document(app, pid, hwnd):
                break
            ref["path"] = str(_p)
            ref["confirmed"] = True
            break
        except Exception:
            continue
    return ref


def _resolve_dir(hwnd: int) -> Optional[dict]:
    """explorer 窗口 → 它当前所在的目录。**按 hwnd 匹配。**

    同一 hwnd 下有多个标签页时只取当前选中的那个；确定不了就只给窗口标题，confirmed=False。

    ⚠️ COM 在非主线程里用必须先 `CoInitialize`（轮询跑在独立线程）。
    """
    if not hwnd:
        return None
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return None
    _inited = False
    try:
        try:
            pythoncom.CoInitialize()
            _inited = True
        except Exception:
            pass
        sh = win32com.client.Dispatch("Shell.Application")
        ws = sh.Windows()
        items = []
        for i in range(ws.Count):
            try:
                w = ws.Item(i)
                if int(w.HWND) == int(hwnd):
                    items.append(w)
            except Exception:
                continue
        if not items:
            return None
        # Win11 资源管理器的每个标签页都是一个条目，且共用同一个顶层 HWND。
        w = items[0] if len(items) == 1 else _active_tab_item(hwnd, items)
        if w is None:
            return {"kind": KIND_DIR, "name": _window_text(hwnd), "path": "",
                    "confirmed": False, "app": "explorer"}
        loc = str(w.LocationURL or "")
        nm = str(w.LocationName or "")
        path = file_url_to_path(loc)
        return {"kind": KIND_DIR, "name": nm, "path": path,
                "confirmed": bool(path), "app": "explorer"}
    except Exception as e:
        logger.debug(f"[Referent] explorer 解析失败: {e}")
    finally:
        if _inited:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    return None


#: SID_STopLevelBrowser：从 Shell 窗口条目取得该标签页的 IShellBrowser。
_SID_TOP_LEVEL_BROWSER = "{4C96BE40-915C-11CF-99D3-00AA004AE837}"


def _active_tab_item(hwnd: int, items: list):
    """同一资源管理器窗口的多个标签条目中，找出当前选中的那个；无法确定时返回 None。

    每个标签页有自己的 ShellTabWindowClass 子窗口（经 IShellBrowser.GetWindow 取得）。
    选中的标签页的子窗口在 z 序中排第一，即 FindWindowEx 返回的第一个。
    窗口标题不能作为依据：它不一定随标签切换更新。
    """
    try:
        import pythoncom
        import pywintypes
        import win32gui
        from win32com.shell import shell
        active = win32gui.FindWindowEx(int(hwnd), 0, "ShellTabWindowClass", None)
        if not active:
            return None
        sid = pywintypes.IID(_SID_TOP_LEVEL_BROWSER)
        hits = []
        for w in items:
            try:
                sp = w._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
                sb = sp.QueryService(sid, shell.IID_IShellBrowser)
                if int(sb.GetWindow()) == int(active):
                    hits.append(w)
            except Exception:
                continue
        return hits[0] if len(hits) == 1 else None
    except Exception as e:
        logger.debug(f"[Referent] 无法确定当前标签页 hwnd={hwnd}: {e}")
        return None


def _window_text(hwnd: int) -> str:
    try:
        import win32gui
        return win32gui.GetWindowText(int(hwnd)) or ""
    except Exception:
        return ""


def file_url_to_path(url: str) -> str:
    """`file:///C:/a/b%20c` → `C:\\a\\b c`。不是 file:// 就返回 ""。"""
    u = (url or "").strip()
    if not u.lower().startswith("file:"):
        return ""
    try:
        p = urllib.parse.urlparse(u)
        raw = urllib.parse.unquote(p.path or "")
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
        return str(pathlib.Path(raw))
    except Exception:
        return ""


# ── 统一入口 ─────────────────────────────────────────────────────────────

def resolve(*, app: str, pid: int = 0, hwnd: int = 0, title: str = "",
            url: str = "") -> Optional[dict]:
    """窗口 → 句柄。**拿不到就返回 None —— 绝不猜。**

    ⚠️ `url` 由调用方传进来（浏览器地址栏是 UIA 读的，贵，由 hooks 按自己的
       节奏采样后传入），本函数不主动去读。
    """
    a = (app or "").replace(".exe", "").strip()
    if not a:
        return None
    c = cat(a)
    try:
        if c in ("editor", "office"):
            return _resolve_file(a, pid, title, hwnd)
        # 🪦 `browser` **刻意不产出句柄**（2026-08-27 定）——
        #    页面名字已经在窗口标题里、已经进了注入的叙述，那就够了。
        #    抓网址那一版见 `browser_domain` 的墓碑。
        if c == "file":
            return _resolve_dir(hwnd)
    except Exception as e:
        logger.debug(f"[Referent] resolve 失败 app={a}: {e}")
    return None


def brief(ref: Optional[dict]) -> str:
    """句柄 → 注入用的**短名**（互动感版）。

    📌 注入层只给「够识别」的部分，完整地址留给按需拉取 ——
       trail 是**每轮无条件注入**的，这里每多一个字都乘以轮数。
    """
    if not ref:
        return ""
    return str(ref.get("name") or "").strip()
