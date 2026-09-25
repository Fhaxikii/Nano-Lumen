# -*- coding: utf-8 -*-
"""找得到 / 读得动 —— `search_files` + `load_full_file` 的 offset/limit。

═══ 这一套守的东西 ═══

  ① **路径黑名单真的挡得住**（含 `..` 绕过）
     早先就明写过「实现前必须先定、不能事后补」：
     不能让它读 `C:\\Windows\\System32\\config\\SAM` 之类。
     ⚠️ 这一项**真的调 `denied_reason()`**，不是查源码里有没有那串正则 ——
        📌 一份写在常量里但没人调用的黑名单，跟没有是一样的
           （同 `is_readonly()` 那个零调用方的死字段）。

  ② **切片如实说明自己是一段**（`core.reading.slice_lines` + `render`，
     peek_file / load_full_file 实际走的路径）
     📌 模型只有知道总长，才谈得上「还剩多少没读」；
        没有它，分片阅读退化成「读一段、猜一下、再读一段」。
     「不切片时一个字都不改」不再是约束：`render` 每次都带全局信息头
     （总行数 / 总字数 / 位置），这是 `core/reading.py` 的设计。

  ③ **`search_files` 不进 `os_execute`**（会更严重）

用法：
  py -3.10 tests\\t_p16_search_read.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_path_policy_really_blocks() -> None:
    print("\n[1] ⭐⭐⭐ 路径黑名单**真的挡得住**（真调，不是查源码）")
    from core.os_layer import pathpolicy as P

    for _p, _what in (
        (r"C:\Windows\System32\config\SAM", "SAM 安全数据库"),
        (r"C:\Users\x\.ssh\id_rsa", "SSH 私钥"),
        (r"C:\Users\x\.aws\credentials", "云凭据"),
        (r"C:\pagefile.sys", "分页文件"),
    ):
        check(bool(P.denied_reason(_p)), f"⭐⭐ 挡住 {_what}", P.denied_reason(_p))

    # 🔴 `..` 绕过 —— 不做规范化的黑名单，一个 `..` 就废了
    _sneaky = r"C:\Windows\..\Windows\System32\config\SAM"
    check(bool(P.denied_reason(_sneaky)),
          "⭐⭐⭐ **`..` 绕不过去**（先 resolve 再匹配）—— "
          "📌 一个不做规范化的路径黑名单，用一个 `..` 就绕过去了",
          P.denied_reason(_sneaky))

    # ⚠️ 反向：用户自己的东西必须读得了
    check(not P.denied_reason(str(ROOT / "app.py")),
          "⭐⭐ 反向：**用户自己的文件照读** —— "
          "📌 fail-safe 方向是允许：这台电脑是用户的，"
          "一个默认拒绝的路径策略坏掉的是用户读自己文件的能力")
    check(not P.denied_reason(""), "⚠️ 空路径不炸")


def t_search_actually_searches() -> None:
    print("\n[2] ⭐⭐⭐ `search_files` 真的搜过（造临时目录，不碰生产）")
    from core.os_layer import filesearch as F

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nanop16_"))
    (tmp / "a.py").write_text("import os\nMAGIC_TOKEN = 1\n", encoding="utf-8")
    (tmp / "b.txt").write_text("no magic here\n", encoding="utf-8")
    _sub = tmp / "node_modules"
    _sub.mkdir()
    (_sub / "junk.py").write_text("MAGIC_TOKEN = 999\n", encoding="utf-8")
    _deep = tmp / "pkg"
    _deep.mkdir()
    (_deep / "c.py").write_text("x = 2\nMAGIC_TOKEN = 3\n", encoding="utf-8")

    # 只按名字 = Glob
    r = F.search(str(tmp), name_pattern="*.py")
    _names = {pathlib.Path(h.path).name for h in r["hits"]}
    check(_names == {"a.py", "c.py"},
          "⭐⭐ 按名字递归找到（**`node_modules` 自动跳过**）—— "
          "📌 那不是「不许」，是「不值得」：翻一遍不会泄露什么，"
          "只会让一次搜索变成三分钟", str(sorted(_names)))

    # 只给内容 = Grep
    r2 = F.search(str(tmp), content="MAGIC_TOKEN")
    _lines = {(pathlib.Path(h.path).name, h.line_no) for h in r2["hits"]}
    check(_lines == {("a.py", 2), ("c.py", 2)},
          "⭐⭐⭐ 搜内容给出 **文件:行号** —— 这才接得上 load_full_file 的 offset",
          str(sorted(_lines)))

    # 两个都给 = 先缩小再搜
    r3 = F.search(str(tmp), name_pattern="a.*", content="MAGIC_TOKEN")
    check(len(r3["hits"]) == 1 and pathlib.Path(r3["hits"][0].path).name == "a.py",
          "⭐ 两个都给时先按名字缩小")

    # 都不给 = 没有意义的调用
    check(F.search(str(tmp))["error"],
          "⭐⭐ 两个都不给**直接拒绝** —— "
          "📌 那等于「把这目录下所有文件列出来」，那是 `list_dir` 的活")

    # 空结果要说清扫了多少
    _txt = F.render(F.search(str(tmp), content="绝对不存在的东西"), root=str(tmp))
    check("没有命中" in _txt and "扫描" in _txt,
          "⭐⭐ 空结果**说清扫了多少个文件** —— "
          "📌 「没找到」和「压根没扫到东西」对下一步是完全不同的信息"
          "（路径写错 vs 关键词写错）")

    # 黑名单在搜索里也生效
    check(F.search(r"C:\Windows\System32\config", name_pattern="*")["error"],
          "⭐⭐⭐ 黑名单目录**当场拒绝**，不是扫完再过滤")


def t_slice_tells_the_truth() -> None:
    print("\n[3] ⭐⭐⭐ 切片如实说明「这是一段」")
    from core import reading as R
    _t = "\n".join(f"L{i}" for i in range(1, 201))

    def _read(**kw) -> str:
        return R.render(R.slice_lines(_t, **kw), filename="t.txt")

    _s = _read(offset=5, limit=3)
    _body = _s.split("\n", 2)[2]           # 去掉信息头和分隔线
    check("L5" in _body and "L7" in _body and "L8" not in _body, "⭐ 切到正确的行")
    check("of 200" in _s,
          "⭐⭐⭐ **给出总行数** —— 📌 模型只有知道总长，"
          "才谈得上「还剩多少没读」；没有它，分片阅读退化成"
          "「读一段、猜一下、再读一段」")
    check("offset=8" in _s,
          "⭐⭐ 直接告诉它**下次从哪开始** —— "
          "📌 能算出来的事别让模型去算")

    _tail_sl = R.slice_lines(_t, offset=198)
    _tail = R.render(_tail_sl, filename="t.txt")
    check(not _tail_sl["has_more"] and "offset=" not in _tail,
          "⚠️ 读到结尾时**不提示还剩多少**（因为没剩）—— "
          "📌 一句「还有 0 行」会让人以为还有东西")

    _oob_sl = R.slice_lines(_t, offset=999)
    _oob = R.render(_oob_sl, filename="t.txt")
    check(_oob_sl["capped_by"] == "out_of_range" and "200" in _oob and "offset" in _oob,
          "⭐⭐ 越界**不报错**，说清总长让它重来 —— "
          "📌 offset 超了是因为它不知道文件多长；"
          "为此报错等于用惩罚回答一个它没法预先知道的问题", _oob[:80])


def t_not_in_os_execute() -> None:
    print("\n[4] ⭐⭐ `search_files` 是**独立工具**，没塞进 os_execute")
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    from core.tools.catalog import Preload, ToolScope

    mans = {v["name"]: v for k, v in vars(O).items()
            if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    defs = {d.name: d for d in build_builtin_definitions(mans)}
    _sf = defs.get("search_files")
    check(_sf is not None, "⚠️ [L5] 前置：工具存在")

    from core.os_layer import dsl as _dsl
    _actions = set(getattr(_dsl, "ACTIONS", {}) or {})
    check("search_files" not in _actions and "grep" not in _actions,
          "⭐⭐⭐ **没有变成 os_execute 的一个 action** —— "
          "🔴 那边 schema 已 2248 字符 / 39 个 action，再塞会加深 [D9]；"
          "⭐ 走的是 `set_window_mode`/`look_at_screen` 那个现存范式")
    check(_sf is not None and _sf.preload is Preload.CORE,
          "⭐⭐⭐ **CORE 常驻** —— 📌 一个「我不知道东西在哪」时才用的工具，"
          "如果自己也要先被 load_tools 找出来，那它在最需要它的那一刻不可用")
    check(_sf is not None and ToolScope.AGENT in _sf.bindings,
          "⭐ Subagent也有它（探索型Subagent的主业就是找东西）")


def t_edit_is_all_or_nothing() -> None:
    print("\n[5] ⭐⭐⭐ `edit_file` 的唯一性与「全有或全无」")
    from core.os_layer import fileedit as F
    orig = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"

    r = F.apply_edits(orig, [{"old_text": "    x = 1", "new_text": "    x = 2"}])
    check(not r.ok and "2 次" in r.error,
          "⭐⭐⭐ old_text 命中两处 → **拒绝** —— "
          "🔴 一个匹配到两处的替换，会安静地改掉你没在看的那一处；"
          "而改的是别人的文件时，那一处可能永远不会被发现", r.error[:40])

    r2 = F.apply_edits(orig, [{"old_text": "def a():\n    x = 1",
                               "new_text": "def a():\n    x = 99"}])
    check(r2.ok and "x = 99" in r2.content and r2.content.count("x = 1") == 1,
          "⭐⭐ 多带上下文变唯一之后改对了，**另一处一个字没动**")
    check(r2.added == 1 and r2.removed == 1 and "-    x = 1" in r2.diff,
          "⭐ 给出 diff 与增删行数（工具卡直接展示它）",
          f"+{r2.added}/-{r2.removed}")

    r3 = F.apply_edits(orig, [{"old_text": "def a():", "new_text": "def A():"},
                              {"old_text": "根本不存在的东西", "new_text": "x"}])
    check(not r3.ok and r3.content == "",
          "⭐⭐⭐ **一条失败 → 整体失败，什么都不写** —— "
          "📌 部分应用会留下一个「改了一半」的文件，而调用方拿到的是「失败」→ "
          "它多半会重试 → 那时前半段已经变了、old_text 又对不上了。"
          "**一次失败的写入不该改变下一次尝试的前提**")

    r4 = F.apply_edits(orig, [{"old_text": "def alpah():", "new_text": "x"}])
    check("最接近的一行" in r4.error,
          "⭐⭐ 找不到时**给出最接近的一行** —— "
          "📌 最常见的成因是缩进/拼写差一点，而那种差别在纯报错里看不见",
          r4.error[-40:])

    check(not F.apply_edits(orig, [{"old_text": "def a():", "new_text": "def a():"}]).ok,
          "⚠️ old_text 与 new_text 相同 → 拒绝（那是一次没有意义的写入）")
    check(not F.apply_edits(orig, []).ok, "⚠️ 空 edits 拒绝")


def t_edit_goes_through_the_safety_net() -> None:
    print("\n[6] ⭐⭐⭐ `edit_file` **穿过 OS 安全网**，不自己写盘")
    import ast as _ast
    src = module_text("core.orchestrator")
    tree = _ast.parse(src)
    _h = next((f for f in _ast.walk(tree)
               if isinstance(f, _ast.AsyncFunctionDef) and f.name == "_handle_edit_file"), None)
    check(_h is not None, "⚠️ [L5] 前置：handler 存在")
    seg = (_ast.get_source_segment(src, _h) or "") if _h else ""

    check("_execute_dsl_step" in seg and "file_write" in seg,
          "⭐⭐⭐ 算完新内容后**交给 `_execute_dsl_step` 走 `file_write`** —— "
          "🔴 于是地板(file_write=2)/确认弹窗/审计**全是 os_execute 那一套**，"
          "而且结构上绕不过去。"
          "📌 2026-08-13 定的硬约束：**换一个暴露层，不许换掉它底下的安全层**")

    # 🔴 它自己**不许**有任何写盘调用
    bad = []
    for n in _ast.walk(_h) if _h else []:
        if isinstance(n, _ast.Attribute) and n.attr in (
                "write_text", "write_bytes", "writelines"):
            bad.append(n.attr)
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id == "open":
            bad.append("open()")
    check(not bad,
          "⭐⭐⭐ handler 里**没有任何直接写盘** —— "
          "⚠️ 哪天有人为了「少一次弹窗」把它改成 `Path.write_text`，"
          "这个工具就变成了绕过确认的后门，**而它看起来只是简化了一下**",
          str(bad))

    check("不存在" in seg,
          "⚠️ 文件不存在时**不顺手创建** —— "
          "📌 「改一个不存在的文件」几乎总是路径写错了，"
          "替用户建一个空文件会把一个明显的错误变成一个安静的错误")
    check("denied_reason" in seg, "⭐ 路径黑名单同样生效")

    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    from core.tools.catalog import Preload, ToolScope
    mans = {v["name"]: v for k, v in vars(O).items()
            if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    defs = {d.name: d for d in build_builtin_definitions(mans)}
    _ef = defs.get("edit_file")
    check(_ef is not None and _ef.preload is Preload.CORE,
          "⭐⭐⭐ **CORE 常驻** —— 📌 让它难被看见，模型就会退回"
          "「读整份 → 重新生成 → 覆盖」，而覆盖错了退不回去，"
          "那条路一旦生成错就没有退路。"
          "CORE 在这里不是为了方便，是为了**让危险的那条路不再是默认路**")
    # ⚠️⚠️ **2026-08-20 换口径**：落地了，Subagent拿到了 `edit_file`。
    #    上一版这条钉的是「Subagent没有它」，理由写的是「Subagent是只读探索型」——
    #    那句话在 立项时是对的，落地那天就过期了。
    # ⭐ 而**该守的东西没变、只是换了一面**：Subagent的写口**只有这一个**，
    #    而且它与 main agent 是**同一个 handler**（安全层零改动）。
    #    📌 一条断言的前提被推翻时，正确的做法是换成它的新形态，
    #       而不是删掉 —— 删了的话，哪天有人给Subagent开了第二个写口，
    #       没有任何东西会红。
    #    🔬 完整的写口清单与「刻意不给」的反向断言在
    #       `tests/t_a4_agent_scope.py` [10]（判据只留一个出处）。
    check(_ef is not None and ToolScope.AGENT in _ef.bindings
          and _ef.bindings.get(ToolScope.AGENT) == _ef.bindings.get(ToolScope.MAIN),
          "⭐⭐ **Subagent 有它，且与 main agent 同一个 handler** —— "
          "📌 换一个暴露层，不许换掉它底下的安全层",
          str(_ef.bindings if _ef else None))


def main() -> int:
    t_path_policy_really_blocks()
    t_search_actually_searches()
    t_slice_tells_the_truth()
    t_not_in_os_execute()
    t_edit_is_all_or_nothing()
    t_edit_goes_through_the_safety_net()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
