# -*- coding: utf-8 -*-
"""`edit_file` 的**纯计算**部分 —— 算出新内容与 diff，**不写盘**。

═══ 为什么需要它（`file_write` 不够）═══

Nano 现有的只有 `os_execute(file_write)`（`path/content/mode`），
适合新建 / 覆盖 / 追加，**不适合「把这 7 行换掉、其他 12000 行一个字别碰」**。
今天做这件事的路径是：**读整份 → 模型重新生成整份 → `file_write` 覆盖**。

📌 **`file_write` 是文件写入原语，`edit_file` 是精确修改原语 —— 语义确实不同。**
🔴 而这对 Nano 有额外意义：**整份覆盖一旦生成错，那份文件就没有中间状态可退回** ——
   而 Nano 改的是**用户的**文件，不能假设用户那边有版本控制。

═══ ⚠️ 范围：只做 Update ═══

同类工具的 patch 接口通常有 Add / Delete / Update / Move 四种。
Nano 这边 **Add/Delete/Move 已经有了**（`file_write` / `file_delete` / `file_move`），
真正缺的只有 **Update（按上下文精确改）**。

📌 **不重复造已有的东西** —— 同「不新加 `read_file`、而是给 `load_full_file`
   补 offset/limit」那条：多一个干同样事的工具，正是工具清单变糊要修的问题。

═══ 🔴 唯一性是安全属性，不是校验细节 ═══

`old_text` 必须**恰好匹配一次**（除非显式 `replace_all`）。
📌 **一个匹配到两处的替换，会安静地改掉你没在看的那一处** ——
   而改的是别人的文件时，那一处可能永远不会被发现。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class EditResult:
    ok: bool
    content: str = ""          # 新的全文（ok 时有效）
    diff: str = ""             # 给人看的差异
    error: str = ""
    applied: int = 0
    added: int = 0
    removed: int = 0


# diff 最多显示多少行 —— 与工具卡截断同一条纪律：显示策略，不是数据策略。
MAX_DIFF_LINES = 200


def apply_edits(original: str, edits: list) -> EditResult:
    """把一组 `{old_text, new_text, replace_all}` 应用到原文上。

    ⚠️ **全有或全无**：任何一条不满足唯一性就整体失败，不写盘。
       📌 部分应用会留下一个「改了一半」的文件 ——
          而调用方拿到的是「失败」，于是它多半会重试，
          重试时前半段已经变了，`old_text` 又对不上了。
          **一次失败的写入不该改变下一次尝试的前提。**
    """
    if not isinstance(edits, list) or not edits:
        return EditResult(False, error="没有给 edits")
    if not isinstance(original, str):
        return EditResult(False, error="原文不是文本（二进制文件不支持精确修改）")

    text = original
    applied = 0
    for _i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return EditResult(False, error=f"第 {_i} 条 edit 不是对象")
        _old = e.get("old_text")
        _new = e.get("new_text")
        if not isinstance(_old, str) or _old == "":
            return EditResult(False, error=f"第 {_i} 条缺 old_text（不能为空）")
        if not isinstance(_new, str):
            return EditResult(False, error=f"第 {_i} 条缺 new_text（可以是空串=删除）")
        if _old == _new:
            return EditResult(False, error=f"第 {_i} 条的 old_text 与 new_text 相同")

        _n = text.count(_old)
        if _n == 0:
            # ⚠️ 找不到时**给出最接近的一行**，而不是只说"没找到"——
            #    📌 最常见的成因是空白/缩进差一点，而那种差别在报错里看不见。
            _hint = _closest_hint(text, _old)
            return EditResult(
                False,
                error=(f"第 {_i} 条的 old_text 在文件里找不到。"
                       f"⚠️ 常见成因是缩进或空白对不上 —— old_text 必须与文件里"
                       f"**逐字一致**（包括行首空格）。{_hint}"))
        if _n > 1 and not e.get("replace_all"):
            # 🔴 见模块头：唯一性是安全属性
            return EditResult(
                False,
                error=(f"第 {_i} 条的 old_text 在文件里出现了 {_n} 次，无法确定改哪一处。"
                       f"请把 old_text 写长一点（多带几行上下文）让它唯一；"
                       f"确实要全改就设 replace_all=true。"))
        text = text.replace(_old, _new) if e.get("replace_all") else text.replace(_old, _new, 1)
        applied += 1

    if text == original:
        return EditResult(False, error="应用之后内容没有任何变化")

    _d, _add, _rm = make_diff(original, text)
    return EditResult(True, content=text, diff=_d, applied=applied,
                      added=_add, removed=_rm)


def _first_diff(a: str, b: str) -> str:
    """两行**第一个不同的字符**：位置 + 两边的码点。

    🔴🔴 2026-08-26 实测抓到的问题：让 Nano 改一个 txt，`old_text` 匹配失败，
       而报错给出的「最接近的一行」**渲染之后和模型刚写的那一行一模一样** ——
       文件里是 ASCII 直引号 `U+0022`，模型抄的时候顺手排版成了中文弯引号
       `U+201C/201D`（同一次里它还把用户的错别字纠正了）。
    📌 **一个「看起来一样」的 diff 提示，等于没提示。** 模型没法从中知道该改什么，
       于是只能改走 `os_execute` 去读原始字符核实 —— **白花两轮，而且不是它笨。**
    ⭐ 所以这里把不可见的差异**摊开成码点**：一眼看得见「你写的是 U+201C、
       文件里是 U+0022」。同一条判据的另一面：说清结果可不可信，
       也要说清**它为什么不可信**。
    """
    _n = min(len(a), len(b))
    _i = 0
    while _i < _n and a[_i] == b[_i]:
        _i += 1
    if _i >= _n and len(a) == len(b):
        return ""
    _ca = a[_i] if _i < len(a) else ""
    _cb = b[_i] if _i < len(b) else ""

    def _show(c):
        return f"U+{ord(c):04X} {c!r}" if c else "（这一行到此为止）"
    return (f"\n第 {_i + 1} 个字符起不同：\n"
            f"  你给的：{_show(_ca)}\n"
            f"  文件里：{_show(_cb)}")


def _closest_hint(text: str, old: str) -> str:
    """找一行最像的，并**指出第一个不同的字符**。"""
    try:
        _first = (old.splitlines() or [""])[0].strip()
        if not _first:
            return ""
        _cands = difflib.get_close_matches(
            _first, [ln.strip() for ln in text.splitlines() if ln.strip()],
            n=1, cutoff=0.6)
        if _cands:
            return (f"文件里最接近的一行是：{_cands[0][:120]!r}"
                    + _first_diff(_first, _cands[0]))
    except Exception:
        pass
    return ""


def make_diff(before: str, after: str) -> tuple[str, int, int]:
    """统一 diff + 增删行数。⚠️ 只给人看，不参与判定。"""
    _b = before.splitlines()
    _a = after.splitlines()
    _lines = list(difflib.unified_diff(_b, _a, lineterm="", n=3,
                                       fromfile="修改前", tofile="修改后"))
    _add = sum(1 for x in _lines if x.startswith("+") and not x.startswith("+++"))
    _rm = sum(1 for x in _lines if x.startswith("-") and not x.startswith("---"))
    if len(_lines) > MAX_DIFF_LINES:
        _lines = _lines[:MAX_DIFF_LINES] + [
            f"… 差异还有 {len(_lines) - MAX_DIFF_LINES} 行没显示"]
    return "\n".join(_lines), _add, _rm
