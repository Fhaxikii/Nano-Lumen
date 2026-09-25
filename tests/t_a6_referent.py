# -*- coding: utf-8 -*-
"""Ambient 引用目标解析 —— 句柄（referent）（2026-08-26）。

═══ 缺口的形状：不是「少一个功能」，是会产出**自信的错答案** ═══

实测：说「帮我补全我刚才编辑的那个 txt」，而 `ambient_trail.jsonl` 里
真实躺着的是一行光秃秃的 **`"in notepad"`** ——「哪个文件」那个空，
Nano 拿**对话历史**填了，填成上一轮聊过的 `西游记.txt`。
📌 **给它一个没有句柄的叙述，它就会拿别处的东西补那个空。**
⇒ 这把 从「锦上添花」挪到了「修正确性」。

═══ 🔬 实测矩阵（当场跑的，推翻了早先的设计写的技术路线）═══

                      双击文件打开 先开应用再打开文件
    cmdline           ✅ 完整绝对路径        ❌ 只有 'notepad.exe'
    cwd               ⚠️ **启动者**的工作目录 —— 不可信（explorer 起的 = system32）
    open_files        ❌ 只有 .mui/.dat/.mun ❌ 同左
    窗口标题           ✅ 文件名              ✅ 文件名

🔴 早先写的是 `open_files` —— 实测在记事本上**一个用户文档都拿不到**。
   真正给出路径的是 `cmdline`，早先的设计里一个字没提。

═══ 已定三层 ═══

    采集 全（完整路径 / 完整 URL）—— 丢了就永远丢了
    注入 只给够识别的（时间 + 名字）—— 每轮无条件付钱，这里省
    拉取  **按【条】**，不按范围 —— 语义匹配发生在已经在上下文里的那一版上

用法：
  py -3.10 tests\\t_a6_referent.py
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ══════════════════════════════════════════════════════════════════════════
def t_parse_title() -> None:
    """标题解析 —— **office 必须和 editor 走同一条**。"""
    print("\n▶ 窗口标题 → 名字")
    from core.proactive import referent as R

    check(R.parse_title("notepad", "新建文本文档 (2).txt - 记事本")[0]
          == "新建文本文档 (2).txt",
          "🔴 记事本抠得出文件名 —— 📌 名字**一直在标题里**，"
          "拦住它的是**分类**（notepad 归 OFFICE，而原来只认 editor/browser）")
    check(R.parse_title("notepad", "*新建文本文档 (2).txt - 记事本")[0]
          == "新建文本文档 (2).txt",
          "⚠️ 未保存的 `*` 前缀被吃掉")
    check(R.parse_title("winword", "报告.docx - Word")[0] == "报告.docx",
          "🔴 Word 也抠得出 —— 📌 那个验收场景（「把这个 word 搞完」）"
          "正好撞在这一类上：**原以为只差路径，实际连「是哪个文件」都过不了**")
    check(R.parse_title("obsidian", "笔记 - 我的库 - Obsidian")[:2] == ("笔记", "我的库"),
          "⭐ 三段标题连库名一起拿到")
    check(R.parse_title("code", "a.py - proj - Visual Studio Code")[:2] == ("a.py", "proj"),
          "⚠️ editor 一字未变（改的是条件，不是解析）")
    check(R.parse_title("chrome", "B站 - Google Chrome")[2] == "B站",
          "⚠️ browser 一字未变")
    check(R.parse_title("notepad", "记事本") == ("", "", ""),
          "⚠️ 没有 ` - ` 就不硬凑 —— 📌 凑出来的名字会变成一个自信的错答案")
    check(R.parse_title("Weixin", "微信") == ("", "", ""),
          "⭐ 抠不出名字的类别 = 不该有句柄的类别。"
          "📌 分割点是**「是不是用户的东西」**（截图回收那次定的判据）："
          "notepad.exe 的路径是厂商的，它打开的那个 txt 才是用户的")


def t_resolve_kinds() -> None:
    """哪些类别出句柄、哪些不出。"""
    print("\n▶ 分割点")
    from core.proactive import referent as R
    for app in ("Weixin", "wechat", "cloudmusic", "WindowsTerminal", "某个游戏"):
        check(R.resolve(app=app, title="x - y") is None,
              f"⚠️ `{app}` 不产出句柄 —— 带上路径只是噪音")
    check(R.resolve(app="", title="x") is None, "⚠️ 空 app 不炸")

    r = R.resolve(app="notepad", pid=-1, title="无标题 - 记事本")
    check(r is not None and r.get("name") == "无标题" and not r.get("confirmed"),
          "⭐⭐ 拿不到路径时**照样给名字，但明标 confirmed=False** —— "
          "📌 这正是实测那个问题的解法：它猜桌面猜中了，"
          "**而没有任何人知道那是猜的**")


def t_names_and_urls() -> None:
    print("\n▶ 名字匹配 / file:// 还原")
    from core.proactive import referent as R
    check(R._names_match("报告.docx", "报告"),
          "⭐ 去扩展名也算 —— Word 未保存时标题是 `文档1`，命令行里却是 `文档1.docx`")
    check(not R._names_match("report.docx", "report_old.docx"),
          "🔴 **只放宽到去扩展名，不做模糊匹配** —— "
          "📌 一个宽松的匹配器会把两个不同文件判成同一个，"
          "而那正是「看起来确认了其实是猜的」")
    check(not R._names_match("", "x"), "⚠️ 空名字不算匹配")
    check(R.file_url_to_path("file:///C:/a/b%20c") == str(pathlib.Path("C:/a/b c")),
          "⭐ file:// → 本地路径，`%20` 还原")
    check(R.file_url_to_path("https://example.com") == "",
          "⚠️ 非 file:// 返回空，不硬解")
    check(R.domain_of("https://www.bilibili.com/video/BV1?t=4") == "bilibili.com",
          "⚠️ 域名截取搬家后行为不变")


def t_buffer() -> None:
    """采集层：hwnd + ref + URL 回填。"""
    print("\n▶ ActivityBuffer")
    from core.proactive.activity import ActivityBuffer
    b = ActivityBuffer()
    b.on_window_focus(1, "notepad.exe", "a.txt - 记事本", hwnd=901,
                      ref={"kind": "file", "name": "a.txt",
                           "path": "C:/a.txt", "confirmed": True})
    b.on_window_focus(2, "chrome.exe", "表格 - Google Chrome", hwnd=903, ref=None)
    snap = b.snapshot()
    check(snap["windows"][0].hwnd == 901 and snap["windows"][0].ref["path"] == "C:/a.txt",
          "⭐ WindowEvent 带上了 hwnd 与句柄")
    check(snap["foreground_hwnd"] == 903,
          "⚠️ **hwnd 是 explorer 唯一能用的标识** —— 实测两个资源管理器窗口 "
          "pid 可以不同，但默认设置下它们共用一个进程。"
          "📌 一个「有时候能区分」的标识，等于不能区分")

    b.set_url_domain("docs.qq.com")
    snap = b.snapshot()
    check(snap["url_domain"] == "docs.qq.com", "⚠️ 域名照旧存（互动感那一层）")
    check("url_full" not in snap,
          "🪦 **完整 URL 不再采**（2026-08-27 砍掉）。"
          "做过一版、实测也跑通过（日志实证 url_len=57 / 回填成功），"
          "砍掉的理由不是它 work 不了，是这一句："
          "**「就算我这台电脑折腾半天抓出来了，其他电脑不一定可靠」** —— "
          "📌 一个「在我机器上好使」的功能，比没有这个功能更坏："
          "它会在别人机器上静默地什么都不给，而没人知道为什么")
    check(snap["windows"][0].ref["path"] == "C:/a.txt",
          "⭐ 文件那条**不受影响** —— 📌 分界是这么划的："
          "路径是「能拿去操作」的东西，网址跟路径本来就是两码事")


def t_trail() -> None:
    """持久层：一行两版 + 老行兼容 + 去重判据。"""
    print("\n▶ ambient_trail")
    from core.proactive import ambient_trail as T
    _old = T._PATH
    _tmp = pathlib.Path(tempfile.mkdtemp()) / "trail.jsonl"
    T._PATH = _tmp
    try:
        T.append("in notepad (a.txt)", {"kind": "file", "name": "a.txt",
                                        "path": "C:/a.txt", "confirmed": True})
        T.append("in Weixin communicating")           # 无 ref
        rows = [json.loads(x) for x in _tmp.read_text(encoding="utf-8").splitlines()]
        check(rows[0].get("ref", {}).get("path") == "C:/a.txt",
              "⭐ 生产力版落盘了")
        check("ref" not in rows[1],
              "⚠️ 没有句柄的条目**不写空字段** —— 📌 一个恒为 null 的键，"
              "读的人分不清「没有」和「忘了填」")

        # 老行（没有 ref）混在里面不能炸
        _tmp.write_text(_tmp.read_text(encoding="utf-8")
                        + "\n" + json.dumps({"ts": time.time(), "line": "in explorer"}),
                        encoding="utf-8")
        got = T.recent(hours=12, limit=10, exclude_last_sec=0)
        check(len(got) == 3 and (got[-1].get("ref") is None),
              "⭐⭐ **老行天然兼容** —— `.get('ref')` 为空即「这条没有更多信息」，"
              "历史文件不需要迁移")

        # ⚠️ 去重只跟**最后一条**比，所以这里必须先把「最后一条」变成要测的那条 ——
        #    上面刚手动追加过一条老行，直接测会测到「跟老行不同」而不是去重逻辑。
        #    📌 一条测的是环境而不是代码的断言，会把「代码是对的」报成 bug。
        T.append("in notepad (a.txt)", {"kind": "file", "name": "a.txt",
                                        "path": "C:/a.txt", "confirmed": True})
        n0 = len(T.recent(hours=12, limit=99, exclude_last_sec=0))
        T.append("in notepad (a.txt)", {"kind": "file", "name": "a.txt",
                                        "path": "C:/a.txt", "confirmed": True})
        check(len(T.recent(hours=12, limit=99, exclude_last_sec=0)) == n0,
              "⚠️ 同 line 同 path 且很近 → 去重（原行为保留）")
        T.append("in notepad (a.txt)", {"kind": "file", "name": "a.txt",
                                        "path": "D:/别处/a.txt", "confirmed": True})
        check(len(T.recent(hours=12, limit=99, exclude_last_sec=0)) == n0 + 1,
              "🔴 **同名不同路径不去重** —— 📌 把第二条吞掉，"
              "等于让 trail 指向错的那个文件")
    finally:
        T._PATH = _old


def t_tool() -> None:
    """拉取层：命中 / 未确认 / 歧义 / 找不到 / 空参数。"""
    print("\n▶ resolve_ambient_referent")
    from core.orchestrator import Orchestrator as O
    from core.proactive.activity import ActivityBuffer
    import core.proactive.activity as _A

    _real = _A._buffer
    b = ActivityBuffer()
    _A._buffer = b
    try:
        t0 = time.time()
        b._windows.append(_A.WindowEvent(
            ts=t0 - 30, process_name="notepad.exe", window_title="a.txt - 记事本",
            pid=1, event="focus", hwnd=901,
            ref={"kind": "file", "name": "a.txt", "path": r"C:\x\a.txt",
                 "confirmed": True, "app": "notepad"}))
        b._windows.append(_A.WindowEvent(
            ts=t0 - 20, process_name="winword.exe", window_title="稿子.docx - Word",
            pid=2, event="focus", hwnd=902,
            ref={"kind": "file", "name": "稿子.docx", "path": "",
                 "confirmed": False, "app": "winword"}))
        # 同一秒两条 —— 歧义
        b._windows.append(_A.WindowEvent(
            ts=t0 - 10, process_name="chrome.exe", window_title="表格 - Chrome",
            pid=3, event="focus", hwnd=903,
            ref={"kind": "dir", "name": "报表", "path": r"C:\Users\me\报表",
                 "confirmed": True, "app": "explorer"}))
        b._windows.append(_A.WindowEvent(
            ts=t0 - 10, process_name="explorer.exe", window_title="下载",
            pid=4, event="focus", hwnd=904,
            ref={"kind": "dir", "name": "下载", "path": r"C:\Users\me\Downloads",
                 "confirmed": True, "app": "explorer"}))

        class P:
            pass
        for n in ("_ambient_entries", "_handle_resolve_ambient_referent"):
            setattr(P, n, getattr(O, n))
        p = P()

        def _at(off):
            return datetime.datetime.fromtimestamp(t0 - off).strftime("%H:%M:%S")

        def run(a):
            return asyncio.run(p._handle_resolve_ambient_referent({"at": a}, "aid"))

        r = run(_at(30))
        check("C:\\x\\a.txt" in r.text and "verified: yes" in r.text and not r.failed,
              "⭐⭐ 已确认的文件 → **直接给完整路径**")
        r = run(_at(20))
        check("verified: no" in r.text and "稿子.docx" in r.text
              and "search_files" in r.text,
              "⭐⭐⭐ 未确认时：给名字、**明说没确认**、并给出下一步 —— "
              "📌 同 [C2] 那条：**模型需要的是一个出口，不是一个名字**")
        r = run(_at(10))
        check("2 different things" in r.text
              and "报表" in r.text and "Downloads" in r.text,
              "🔴🔴 **同一时刻多条 → 全给**。第一版这里 break 掉第一条，"
              "于是「取那个网页」会静默拿回文件夹那条 —— "
              "📌 **一个可能指错而不自知的 id，比没有 id 更糟**")
        r = run("03:03:03")
        check(r.failed and "Resolvable right now" in r.text,
              "⚠️ 找不到时**连「哪些能取」一起说** —— 不只回一句「没找到」")
        r = run("")
        check(r.failed and "Missing 'at'" in r.text, "⚠️ 空参数明确报错")

        rows = p._ambient_entries()
        check(all(isinstance(x[0], float) for x in rows) and
              rows == sorted(rows, key=lambda x: x[0]),
              "🔴 **排序必须给 key** —— `sorted(元组列表)` 在 ts 与 line 都相同时"
              "会去比较第三项那个 dict ⇒ **TypeError 当场崩**。"
              "📌 一个「顺手」的默认排序，会在最罕见的输入上变成崩溃点")
    finally:
        _A._buffer = _real


def t_wiring() -> None:
    """接线：注入标记、稳定前缀、目录常驻、转发。"""
    print("\n▶ 接线")
    from core.orchestrator import Orchestrator as O, _ambient_parse_title
    from core.tools.manifests import _RESOLVE_AMBIENT_REFERENT_MANIFEST as M
    from core.proactive import referent as R

    check(_ambient_parse_title("notepad", "a.txt - 记事本")
          == R.parse_title("notepad", "a.txt - 记事本"),
          "⭐ orchestrator 侧留的是**转发**，判据只有 referent 一处")

    src = module_text("core.orchestrator")
    check("resolve_ambient_referent with the timestamp" in src,
          "⭐⭐ 稳定前缀里**接上了出口** —— ⚠️ 上一步刻意没接："
          "工具还不存在时指向它，就是「schema 说有、运行说没有」那类最难查的失败")
    check("Never invent a path for something you " in src,
          "🔴 前缀明确禁止**凭名字编路径** —— 这正是实测那次的问题")
    check('_mark = " ▸" if (r.get("ref") or {}).get("path") else ""' in src,
          "⭐ 只有真有句柄的条目才带 ▸ —— "
          "📌 不标的话模型迟早去拉一条本来就没东西的，**白烧一轮**")
    check('strftime("%H:%M:%S")' in src,
          "⚠️ 注入时刻精确到秒（它同时是拉取用的 id）")
    check("绝不能用序号" in src,
          "🔴🔴 钉住「不许用序号」：trail 滚动 + 每 4 分钟新增，"
          "第 N 轮看到的 #3 到第 N+1 轮已经不是那条 —— "
          "📌 **一个会在两次调用之间漂的标识，漂了没人知道**")
    check("往往根本不在 trail 里" in src,
          "🔴 钉住「刚才那一条在实时 buffer 里」：`recent()` 刻意排除最近 10 分钟，"
          "而 trail 每 4 分钟才写一次 —— 只做 trail 的话，最常见的场景一条都拉不到")

    check("triangle" in M["description"] and "resolve" not in M["description"][:20],
          "⚠️ 工具描述告诉模型「只有带标记的才有东西」")
    check("do not call this" in M["description"],
          "⭐ 描述里划清了边界：只问「我刚才在干嘛」时**不该**调它")

    import core.orchestrator as _orc
    from core.tools.builtin import build_builtin_definitions
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    _d = {d.name: d for d in build_builtin_definitions(_mans)}
    from core.tools.catalog import Preload
    check("resolve_ambient_referent" in _d, "⭐ 工具已登记")
    check(_d["resolve_ambient_referent"].preload == Preload.CORE,
          "⚠️ **CORE 常驻** —— 理由同 `peek_file`：它存在的意义就是省轮数，"
          "逼它先 `load_tools` 等于自己把收益抵消掉")

    hk = module_text("core.proactive.hooks")
    # ⚠️ 判据用 **运行时属性**，不用文本搜索 —— 第一版搜 `_domain_of` 字符串，
    #    结果搜到了那条**留痕注释**里提到的同名函数。
    #    📌 一条搜注释的断言，测的是「有没有人提过它」，不是「它还在不在」。
    import core.proactive.hooks as _HK
    check(not hasattr(_HK, "_domain_of") and not hasattr(_HK, "_BROWSER_PROCS")
          and not hasattr(_HK, "_get_browser_domain"),
          "⭐ hooks 里那三份重复的浏览器逻辑已删干净 —— 📌 一份清单出现两处，迟早分叉")
    check("buf.on_window_focus(pid, name, title, hwnd=hwnd, ref=_ref)" in hk,
          "⭐ 焦点变化那一刻就解析句柄")


# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
def t_argv_split() -> None:
    """🔴🔴 Windows 把带空格的路径拆成多个 argv —— **100% 触发的那种**。

    注册表里 .txt 的打开命令是 `NOTEPAD.EXE %1` —— **`%1` 没有引号**。
    而 `新建文本文档 (N).txt` 是 Windows 中文版**默认的新建文件名**，天生带空格
    ⇒ 这个 bug 不在边角，它在最常见的那条路上必然踩中。
    """
    print("\n▶ argv 空格拆分（实测 2026-08-26）")
    from core.proactive import referent as R

    args = [r"C:\Users\me\Desktop\新建文本文档", "(7).txt"]
    cands = R._argv_path_candidates(args)
    full = r"C:\Users\me\Desktop\新建文本文档 (7).txt"
    check(full in cands,
          "🔴🔴 拆开的两段能被拼回完整路径 —— "
          "📌 记事本自己没事（它用 `GetCommandLineW()` 读**原始整串**），"
          "而我们逐个参数看：前半截文件名对不上、后半截不是绝对路径，"
          "**两边都跳过，全程没有任何报错**")
    check(cands.index(full) < cands.index(r"C:\Users\me\Desktop\新建文本文档"),
          "⚠️ 长的候选排在前面 —— 带空格的完整路径必须赢过它自己的前半截")
    check(len(R._argv_path_candidates(["a", "b", "c"])) == 6,
          "⚠️ 所有连续片段都是候选（n 很小，真伪由「文件名对得上 + 文件真存在」兜住）")
    check(R._argv_path_candidates([]) == [], "⚠️ 空 argv 不炸")

    # ── 端到端：伪造一个 cmdline 被拆开的进程 ──────────────────────────
    import psutil as _ps
    _dir = pathlib.Path(tempfile.mkdtemp())
    _f = _dir / "新建文本文档 (7).txt"
    _f.write_text("x", encoding="utf-8")

    class _FakeProc:
        def __init__(self, *a, **k):
            pass

        def cmdline(self):
            return [r"C:\Windows\system32\NOTEPAD.EXE",
                    str(_dir / "新建文本文档"), "(7).txt"]

    _real = _ps.Process
    _real_single = R._single_document
    _ps.Process = _FakeProc
    # 伪造的进程没有真实窗口；文档数判定由 t_referent_multi_doc.py 单独测试。
    R._single_document = lambda *a, **k: True
    try:
        r = R.resolve(app="notepad", pid=1, title="新建文本文档 (7).txt - 记事本")
        check(bool(r) and r.get("confirmed") and r.get("path") == str(_f),
              "⭐⭐⭐ 端到端：拆开的 argv 还原成了真实路径，且 confirmed",
              str(r))
        _f.unlink()
        r2 = R.resolve(app="notepad", pid=1, title="新建文本文档 (7).txt - 记事本")
        check(bool(r2) and not r2.get("confirmed"),
              "🔴 **拼出来的路径必须真的存在** —— 这一条同时是「所有连续片段」"
              "那个做法的安全网：拼错的片段既对不上文件名、也不会存在")
    finally:
        _ps.Process = _real
        R._single_document = _real_single

    _src = module_text("core.proactive.referent")
    check("用一个近似场景去验真实场景" in _src,
          "🪦 留痕记下**我第一次为什么没抓到它**：实测用的是 "
          "`subprocess.Popen(['notepad.exe', 路径])` —— 那是直接传 argv 数组，"
          "**不经过 shell 那层拆分**。验过的和实际跑的不是同一条路")



# ══════════════════════════════════════════════════════════════════════════
def t_browser_no_handle() -> None:
    """🪦 浏览器**刻意不产出句柄**。

    做过一版完整的 URL 抓取：采全 → 注入只给域名 → 用户指代时才取完整 URL，
    还配了按 hwnd 精确回填。**实测跑通过**（诊断日志实证：
    `url_len=57 domain='bilibili.com'`，`windows: chrome#1249846:url:PATH`）。

    砍掉的理由不是它 work 不了，是 用户给的那条判据：
    > 「就算我这台电脑咱们折腾半天抓出来了，**其他电脑不一定可靠**。」

    实测日志 8 拍里有 5 拍 `url_len=0` —— UIA 读地址栏随浏览器版本 / 语言 /
    是否全屏而变。📌 **一个「在我机器上好使」的功能，比没有这个功能更坏**：
    它会在别人的机器上静默地什么都不给，而没有人知道为什么。

    ⭐ 而更根本的是 用户划的那条分界：
    ```
    需要给源头的 本地可修改的文件 + 文件夹    ⇒ 路径是「能拿去操作」的东西
    不需要给源头的   Claude Code / 游戏 / 浏览器  ⇒ **网址跟路径本来就是两码事**
    ```
    """
    print("\n▶ 浏览器停在互动感层")
    import inspect
    from core.proactive import referent as R
    from core.proactive.activity import ActivityBuffer

    check(R.resolve(app="chrome", hwnd=1, title="B站 - Google Chrome") is None,
          "🪦 `browser` 不产出句柄 —— 页面名字已经在窗口标题里、已经进了叙述")
    check(R.resolve(app="msedge", hwnd=1, title="某文章 - Microsoft Edge") is None,
          "🪦 Edge 同理")
    check(not hasattr(R, "KIND_URL"), "🪦 `KIND_URL` 已删")
    check(not hasattr(R, "browser_url"), "🪦 交出完整 URL 的那个函数已删")
    check(hasattr(R, "browser_domain"), "⚠️ 只剩交出**域名**的那个")

    _src = inspect.getsource(R.browser_domain)
    check("domain_of((url or" in _src,
          "🔴 **完整 URL 不出这个函数** —— 读进来立刻截成域名")
    check("hwnd" in inspect.signature(R.browser_domain).parameters
          and "ControlFromHandle" in _src,
          "⭐ 保留的唯一改动：**按 hwnd 读**而不是 `GetForegroundControl()` —— "
          "后者实测在这台机器上根本读不到，而那正是「域名一直是空的」的原因")

    b = ActivityBuffer()
    check(not hasattr(b, "foreground_url"), "🪦 存完整 URL 的字段已删")
    b.set_url_domain("bilibili.com")
    check(b.snapshot()["url_domain"] == "bilibili.com", "⚠️ 域名这条路照旧")

    hk = module_text("core.proactive.hooks")
    check("_a6_probe" not in hk,
          "⚠️ 那段临时诊断代码**已经删干净** —— 📌 定位用的脚手架不留在仓库里")


if __name__ == "__main__":
    print("=" * 74)
    print("[A6] Ambient 引用目标解析（referent）")
    print("=" * 74)
    t_parse_title()
    t_resolve_kinds()
    t_names_and_urls()
    t_argv_split()
    t_buffer()
    t_browser_no_handle()
    t_trail()
    t_tool()
    t_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
