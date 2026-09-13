# core/code_scan.py
"""代码副作用扫描 —— **全项目唯一一份**。

═══ 为什么它在这里，而不是留在 orchestrator 里 ═══

它原本是 `Orchestrator._detect_ast_side_effects`（给 Skill 审计用）。
临时执行通道要问的是**同一个问题**：「这段代码会不会碰外界」。

📌 **判据只能有一处。** 两份扫描器迟早会分叉 —— 而分叉的表现不是报错，
   是「Skill 审计拦得住的东西，临时通道放过去了」（或者反过来），
   **而且没有任何东西会告诉我们它们已经不一致了**。
⇒ 所以搬到这个不依赖任何东西的底层模块，两条路都调它。
   `Orchestrator._detect_ast_side_effects` 现在是一行转发，保持调用方不变。

═══ ⚠️ 它是什么，不是什么 ═══

**它是分类器，不是沙箱。**
`__import__("os").system(...)` / `getattr(os, "remove")(...)` 这类间接调用它抓不到。
📌 所以**扫描结果不能当安全边界** —— 它决定的是「要不要打断用户」。
   真正的边界是**用户看得见代码**：
     · 扫出副作用 → 弹确认，弹窗里带**只读**代码预览（事前：你在授权什么）
     · 无论扫出没扫出 → 工具卡里都能展开看那段代码（事后：刚才发生了什么）

⭐ 而这个风险水平**与正式 Skill 完全相同**（Skill 也是 AST 扫不出来就不弹），
   已经跑了很久。临时代码在可见性上**只多不少**：Skill 的代码用户只在创建时
   看过一次，之后每次调用都不再看；临时代码**每一次**的工具卡里都能展开。
"""
from __future__ import annotations

import ast
from typing import Any

# ⭐ 与 `Orchestrator._check_skill_side_effects` 的 `_CONFIRM_NEEDED` **同一套词表**。
#    📌 用户在 Skill 那边已经认识这些话术了，临时通道再造一套新词，
#       等于让用户为同一件事学两遍。
SE_FILE_WRITE = "file_write"
SE_FILE_DELETE = "file_delete"
SE_SHELL = "shell"
SE_NETWORK = "network"
SE_OS_CONTROL = "os_control"

# ⭐⭐ 类别 → **给用户看的那句话**。原本长在
#    `Orchestrator._check_skill_side_effects` 里（`_CONFIRM_NEEDED`），
#    2026-08-26 搬来这里，两条路共用。
# 📌 不共用的后果很具体：Skill 那条路弹窗写「写入文件到磁盘」，
#    临时代码那条写 `file_write` —— **同一件事，用户看到两种说法**。
CONFIRM_LABELS: dict[str, str] = {
    "file_write":    "写入文件到磁盘",
    "file_delete":   "删除文件(不可恢复)",
    "shell":         "执行系统命令",
    "send_message":  "发送消息/邮件",
    "external_api":  "调用外部 API",
    "network":       "发起网络请求",
    "os_control":    "操作屏幕/鼠标/键盘/系统窗口",
}


def labels_for(categories) -> list[str]:
    """类别 → 用户可读的描述（未知类别原样保留，不吞掉）。

    ⚠️ 未知类别**不许静默丢弃** —— 📌 一个「显示不了就不显示」的映射，
       会在加了新类别却忘了加文案时，**悄悄少弹一条副作用**。
    """
    return [CONFIRM_LABELS.get(c, c) for c in (categories or [])]


# ⚠️ 只列**需要确认**的。`readonly` / `file_read` 不在这里 —— 与 Skill 一致：
#    读文件不弹窗（Nano 本来就在读用户的文件，那是它的日常）。


def detect_side_effects(code: str, tree: Any) -> list[str]:
    """扫描 AST 检测真实副作用 API。返回发现的高危操作描述列表（去重保序）。

    只检测最容易被滥用的几类:
    - 文件写入:open(..., 'w'/'a'/'wb') / pathlib.write_text / write_bytes
    - 文件删除:os.remove / os.unlink / shutil.rmtree / Path.unlink
    - 系统命令:subprocess / os.system / os.popen
    - 网络:requests.get/post / urllib / httpx / aiohttp
    - 动态执行:eval() / exec()

    不做完整污点分析(那需要类型推断),只扫最常见的直接调用模式。
    """
    findings = []

    # ⭐⭐ [2026-08-27] **先把 import 别名摊平**。
    #
    # 🔴 问题：`import subprocess as sp` 之后 `sp.run(...)` 的调用名是 `"sp.run"`，
    #    而规则表里写的是 `"subprocess.run"` ⇒ **换个别名就绕过整条规则**。
    #    实测 14 种常见写法里漏 5 种，而临时执行通道**一直在用这个函数**
    #    给用户看「这段代码有什么副作用」。
    # 📌 同 `allow_dangerous` 那 6 个开关零读取点那次：
    #    **一个失效的检测比没有检测更危险** —— 弹窗上写着「无副作用」，
    #    用户据此点了同意，而那段代码正在改用户的 .env。
    _alias: dict[str, str] = {}
    for _n in ast.walk(tree):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _alias[_a.asname or _a.name.split(".")[0]] = _a.name
        elif isinstance(_n, ast.ImportFrom) and _n.module:
            for _a in _n.names:
                _alias[_a.asname or _a.name] = f"{_n.module}.{_a.name}"

    def _canon(name: str) -> str:
        """`sp.run` → `subprocess.run`；认不出就原样返回。"""
        _parts = name.split(".")
        if _parts[0] in _alias:
            _parts[0] = _alias[_parts[0]]
            return ".".join(_parts)
        return name

    # ⚠️ **方法名本身就足够特殊**的那几个：它们挂在任意变量上都只可能是那件事
    #    （`p.write_text()` 的 `p` 是个变量，别名映射帮不了 —— 只能认方法名）。
    # 📌 判据是「这个名字在别的语义下几乎不会出现」：
    #      收 `write_text`/`unlink` —— 除了 pathlib 没人这么起名
    #      **不收** `get`/`remove`/`save` —— `dict.get` / `list.remove` 满地都是，
    #      收了会把整个检测淹在误报里，而误报多到一定程度等于没有检测
    # ⚠️ 值是**类别后缀**，最终文本用 `full_name` 拼 —— 这样 `os.unlink(...)`
    #    经这里和经规则表产生的是**同一个字符串**，末尾 `dict.fromkeys` 自然去重。
    #    📌 第一版两边文本不同，于是同一件事在弹窗里列了两遍。
    # ⚠️ `rmtree` 不收：规则表和别名映射已经全覆盖，而 pathlib 上没有这个方法，
    #    收了只会制造重复。
    _DISTINCTIVE = {
        "write_text": "文件写入",
        "write_bytes": "文件写入",
        "unlink": "文件删除",
    }

    # 扫描所有函数调用
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call,)):
            continue

        func = node.func
        # 提取调用名(支持 a.b() 和 a() 两种形式)
        if isinstance(func, ast.Attribute):
            attr_chain = []
            cur = func
            while isinstance(cur, ast.Attribute):
                attr_chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                attr_chain.append(cur.id)
            attr_chain.reverse()
            full_name = ".".join(attr_chain)
        elif isinstance(func, ast.Name):
            full_name = func.id
        else:
            continue

        # ⭐ 规范化之后再匹配 —— 规则表一个字不用改，却对所有别名生效
        full_name = _canon(full_name)
        _tail = full_name.split(".")[-1]
        if _tail in _DISTINCTIVE:
            findings.append(f"{full_name}() {_DISTINCTIVE[_tail]}")

        # 文件写入
        if full_name == "open":
            # 检查 mode 参数是否含 w/a/x
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if any(m in arg.value for m in ("w", "a", "x")):
                        findings.append("open(file, 'w'/'a') 文件写入")
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    if any(m in str(kw.value.value) for m in ("w", "a", "x")):
                        findings.append("open(file, mode='w') 文件写入")

        # ⚠️ pathlib 写入那条**已挪进 `_DISTINCTIVE`**（按方法名匹配）——
        #    留在这里只会让 `pathlib.Path('a').write_bytes()` 同时命中两处，
        #    在弹窗里把同一件事列两遍。📌 一条被更通用的规则完全覆盖的规则，
        #    留着不是"双保险"，是重复。

        # 文件删除
        if full_name in ("os.remove", "os.unlink", "shutil.rmtree",
                         "Path.unlink", "unlink", "rmtree"):
            findings.append(f"{full_name}() 文件删除")

        # ⚠️ **改名 / 替换也是写** —— 规则表原来完全没有它们，
        #    而 `os.replace` 恰恰是「原子覆盖一个已存在的文件」，
        #    比 `open(...,'w')` 更彻底。📌 一张漏了最狠那一项的清单，
        #    比没有清单更容易让人以为已经覆盖全了。
        # ⚠️ **不收裸 `rename` / `replace`** —— `'abc'.replace(...)` 满地都是，
        #    第一版收了它，当场把纯字符串操作报成「文件改名」。
        #    📌 而 `from os import replace` 那种写法**已经由别名映射覆盖**，
        #       裸名字纯属多余 —— 只剩误报，没有收益。
        if full_name in ("os.rename", "os.replace", "os.renames",
                         "pathlib.Path.rename"):
            findings.append(f"{full_name}() 文件改名/覆盖(file_write 风险)")

        # 系统命令
        if full_name in ("os.system", "os.popen", "subprocess.run",
                         "subprocess.call", "subprocess.Popen",
                         "subprocess.check_output", "check_output"):
            findings.append(f"{full_name}() 系统命令执行")

        # 动态执行
        if full_name in ("eval", "exec"):
            findings.append(f"{full_name}() 动态代码执行(高危)")

        # 网络请求
        if full_name in ("requests.get", "requests.post", "requests.put",
                         "requests.delete", "requests.request",
                         "urllib.request.urlopen", "httpx.get", "httpx.post",
                         "aiohttp.ClientSession"):
            findings.append(f"{full_name}() 网络请求")

        # ⚠️ `s = requests.Session(); s.get(...)` —— 真正发请求的那一句挂在变量上，
        #    认不出来。⇒ 改认**建对象**这一句：代码里出现它，就说明这段要联网。
        #    📌 抓不住「用」的那一刻，就去抓「准备用」的那一刻 ——
        #       两者对「这段代码会不会联网」这个问题是等价的。
        if full_name in ("requests.Session", "httpx.Client",
                         "httpx.AsyncClient", "urllib.request.build_opener"):
            findings.append(f"{full_name}() 网络请求")

        # OS 高危库（绕过 DSL 契约直接操作系统）
        _OS_DANGEROUS = {
            "pyautogui", "win32gui", "win32api", "win32con",
            "keyboard", "mouse", "ctypes.windll", "windll",
            "pynput", "pynput.keyboard", "pynput.mouse",
        }
        _root = full_name.split(".")[0]
        if _root in _OS_DANGEROUS or full_name in _OS_DANGEROUS:
            findings.append(f"{full_name}() 直接 OS 控制(应通过 dsl_plan 返回，禁止在 run() 内直接调用)")

        # 常见数据写文件 API（声明 READONLY 但实际导出文件）
        _DATA_WRITE_METHODS = {
            "to_excel", "to_csv", "to_json", "to_parquet", "to_pickle",
            "savefig", "save", "imsave",
        }
        _COPY_METHODS = {"copy", "copy2", "copyfile", "move"}
        _method = full_name.split(".")[-1]
        if _method in _DATA_WRITE_METHODS:
            findings.append(f"{full_name}() 数据文件写出(file_write 风险)")
        if full_name.startswith("shutil.") and _method in _COPY_METHODS:
            findings.append(f"{full_name}() 文件复制/移动(file_write 风险)")

    return list(dict.fromkeys(findings))  # 去重保序


# ── findings → 副作用类别 ────────────────────────────────────────────────
# ⚠️ 顺序有讲究：**先匹配更具体的**。「文件删除」和「文件写入」都含「文件」，
#    「数据文件写出」里也有「写出」—— 用整串关键词而不是单字，且删除优先。
_RULES: tuple[tuple[str, str], ...] = (
    ("文件删除", SE_FILE_DELETE),
    ("系统命令执行", SE_SHELL),
    # 🔴 `eval`/`exec` 在 `_CONFIRM_NEEDED` 里**没有对应类别**，归到 shell ——
    #    因为它对用户的实际含义就是「它可以跑任意东西」。
    #    📌 与其新造一个用户没见过的类别，不如落到一个用户已经理解的类别上。
    ("动态代码执行", SE_SHELL),
    ("网络请求", SE_NETWORK),
    ("直接 OS 控制", SE_OS_CONTROL),
    ("文件写入", SE_FILE_WRITE),
    ("数据文件写出", SE_FILE_WRITE),
    ("文件复制/移动", SE_FILE_WRITE),
)


def classify(findings: list[str]) -> list[str]:
    """把扫描结果映射成**与 Skill 同一套**的副作用类别（去重保序）。

    ⭐ 输出直接喂给现有的 `execution_confirm` 事件 —— 用户看到的弹窗
       与运行一个有副作用的 Skill **一模一样**，只是多一段只读代码预览。
    """
    out: list[str] = []
    for f in findings:
        for token, cat in _RULES:
            if token in f:
                if cat not in out:
                    out.append(cat)
                break
    return out


def scan(code: str) -> tuple[list[str], list[str], str]:
    """一步到位：源码 → (副作用类别, 原始发现, 语法错误信息)。

    ⚠️ **语法错误单独返回，不当成「没有副作用」** ——
       📌 解析不了的代码，我们对它一无所知，那不等于它是安全的。
          调用方必须把它当失败处理，而不是「扫描通过」。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [], [], f"{e.msg} (line {e.lineno})"
    findings = detect_side_effects(code, tree)
    return classify(findings), findings, ""
