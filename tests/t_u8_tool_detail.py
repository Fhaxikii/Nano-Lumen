# -*- coding: utf-8 -*-
"""工具卡透明度 —— **内容层**。

═══ 这一套守的东西 ═══

  ① 详情**只读落盘账本**，绝不读 `MemoryManager.storage`
     🔴 storage 是 的投影：它一把那段降到 L1，工具结果就变成
        `[Tool output aged out...]` 占位符。读错账本的表现是
        **历史工具卡集体变成占位符，而且不报错** —— 透明度当场白做。
     ⭐ 所以本套件里那一项是**真的把 storage 换成占位符之后再看详情**，
        不是"检查源码里没写 storage"。
        📌 只查源码文本的话，对代码的【解释】会参与判定（本项目第四次）。

  ② 没写专属渲染器的工具**也有内容**（通用兜底）
     改造前 `Presentation` 只有 card/intent，**压根没有「详情」这个概念**，
     27 个内置工具里只有 Skill 审计卡一个能展开。
     📌 让「有详情」成为默认，而不是每个新工具的自觉。

  ③ 截断要**说自己截断了**
     📌 一个没说自己被截断过的显示，会让用户以为那就是全部。

用法：
  py -3.10 tests\\t_u8_tool_detail.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_BODY = "PING 8.8.8.8\n64 bytes: time=12ms\nexit 0"


def _seed():
    """真库 + 真 repo，写一次 tool_call/tool_result。⚠️ 临时库，绝不碰生产库。"""
    from core.runtime.store import RuntimeStore
    from core.runtime.conversation import ConversationRepository
    from core.schema import ChatMessage, ToolCall, ToolResultBlock
    st = RuntimeStore(pathlib.Path(tempfile.mkdtemp(prefix="nanou8_")) / "t.db")
    repo = ConversationRepository(st)
    sid = repo.current_session().session_id

    _u = ChatMessage(role="user", content="ping 一下")
    _call = ChatMessage(role="tool_calls", content="")
    _call.tool_calls = [ToolCall(name="os_execute", tool_use_id="tu_1",
                                 args={"action": "run", "command": "ping 8.8.8.8",
                                       "reason": "测连通"}, index=0)]
    _res = ChatMessage(role="tool_results", content="")
    _res.tool_results = [ToolResultBlock(name="os_execute", tool_use_id="tu_1",
                                         content=_BODY)]
    for m in (_u, _call, _res):
        repo.append_message(sid, m)
    return st, repo, sid, [_u, _call, _res]


class _StubMemory:
    """只提供 `_ledger_tool_record` 真正用到的两样东西。"""

    def __init__(self, repo, sid, storage):
        self.conversation_repository = repo
        self.conversation_session_id = sid
        self.storage = storage


def _bound(memory):
    """把 `WebUI._ledger_tool_record` 绑到 stub 上**真的调用它**。

    ⚠️⚠️ 不构造整个 WebUI（要 NiceGUI 运行时），但**也不重写一份逻辑** ——
       📌 本项目的判据：跨模块边界必须有一项真的走过去；
          自制假对象 + 自己重写的逻辑，只能证明两次想法相同。
    """
    from app import WebUI
    class _Host:
        pass
    h = _Host()
    h.memory = memory
    return WebUI._ledger_tool_record.__get__(h, _Host)


def t_reads_ledger_not_storage() -> None:
    print("\n[1] ⭐⭐⭐ 详情读的是**落盘账本**，不是上下文治理层的投影")
    st, repo, sid, storage = _seed()
    fn = _bound(_StubMemory(repo, sid, storage))

    name, args, res = fn("tu_1")
    check(name == "os_execute", "拿到工具名", name)
    check(args.get("command") == "ping 8.8.8.8",
          "⭐ **参数也从账本取**（不从 live 事件穿过来）—— "
          "📌 两个来源只在「我两次想法相同」时一致", str(args))
    check(getattr(res, "content", "") == _BODY, "拿到完整结果原文")

    # ── 🔴 关键：把 storage 真的降成 L1，再看详情 ──
    from core.context.decay import apply_l1
    from core.context.exchange import split
    _n = 0
    for ex in split(storage):
        _n += apply_l1(ex)
    check(_n >= 1, "⚠️ [L5] 前置：storage 里的工具结果**真的被换成占位符了**",
          f"换了 {_n} 条")
    check(all(not str(getattr(tr, "content", "")).startswith("PING")
              for m in storage for tr in (getattr(m, "tool_results", None) or [])),
          "⚠️ 前置：确认投影里已经没有原文了")

    _n2, _a2, _r2 = fn("tu_1")
    check(getattr(_r2, "content", "") == _BODY,
          "⭐⭐⭐ **L1 降级之后，详情仍然拿得到原文** —— "
          "🔴 若读的是 storage，这里会变成 `[Tool output aged out...]`，"
          "历史工具卡集体变占位符**而且不报错**，透明度当场白做",
          repr(str(getattr(_r2, "content", ""))[:40]))
    check(_a2.get("command") == "ping 8.8.8.8", "⭐ 参数同样不受降级影响")
    st.close_thread_conn()


def t_generic_fallback_covers_everything() -> None:
    print("\n[2] ⭐⭐ 没写专属渲染器的工具**也有内容**（这才是主要产出）")
    from core.tools.catalog import Presentation, default_detail
    from core.schema import ToolResultBlock

    p = Presentation(card=lambda a: "随便什么")
    r = ToolResultBlock(name="x", tool_use_id="u", content="结果正文")
    blocks = p.render_detail({"k": "v"}, r)
    _labels = [b.label for b in blocks]
    check("参数" in _labels and "结果" in _labels,
          "⭐⭐⭐ 缺省走通用兜底 = 参数 + 结果 —— "
          "📌 让「有详情」成为默认，而不是每个新工具的自觉", str(_labels))

    # 错误要标出来，不能跟成功长一样
    re_ = ToolResultBlock(name="x", tool_use_id="u", content="炸了", is_error=True)
    kinds = {b.label: b.kind for b in default_detail({}, re_)}
    check(kinds.get("错误") == "error",
          "⭐⭐ 失败的结果标成 `error` —— "
          "📌 `is_error` 是「尝试过」和「做过」的唯一分界（同 L1 那条红线）",
          str(kinds))

    # 专属渲染器抛异常 → 退回兜底，不是空白
    def _boom(a, r):
        raise RuntimeError("我坏了")
    p2 = Presentation(card=lambda a: "x", detail=_boom)
    check(len(p2.render_detail({"k": "v"}, r)) >= 1,
          "⭐⭐ 专属渲染器抛异常 → **退回通用兜底**，不是一片空白 —— "
          "📌 一个渲染不出来的详情，不许把整条历史的重放搞挂")


def t_all_builtin_tools_have_detail() -> None:
    print("\n[3] ⭐⭐ 27 个内置工具，**没有一个**展开后是空的")
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions
    from core.schema import ToolResultBlock
    mans = {k: v for k, v in
            ((kk, vv) for kk, vv in vars(O).items()
             if kk.endswith("_MANIFEST") and isinstance(vv, dict) and vv.get("name"))}
    mans = {v["name"]: v for v in mans.values()}
    defs = build_builtin_definitions(mans)
    r = ToolResultBlock(name="x", tool_use_id="u", content="有结果")
    empty = [d.name for d in defs
             if not d.presentation.render_detail({"a": 1}, r)]
    check(not empty,
          f"⭐⭐⭐ 全部 {len(defs)} 个都渲染得出内容 —— "
          "🔴 改造前只有 Skill 审计卡一个能展开", str(empty))
    # 专属渲染器：命令必须单独成块，不能埋在 JSON 里
    _os = next((d for d in defs if d.name == "os_execute"), None)
    _b = _os.presentation.render_detail(
        {"action": "run", "command": "ping 8.8.8.8", "risk": "low"}, r) if _os else []
    check(any(x.label == "命令" and "ping 8.8.8.8" in x.body for x in _b),
          "⭐⭐⭐ `os_execute` 的**命令原文单独一块** —— "
          "🔴 这是整份盘点里最缺的一个；兜底会把它和 risk/reason 一起塞进 JSON",
          str([x.label for x in _b]))


def t_clip_announces_itself() -> None:
    print("\n[4] ⭐ 截断必须**说自己截断了**")
    from core.tools.catalog import clip, MAX_DETAIL_CHARS, MAX_DETAIL_LINES
    _long = "x" * (MAX_DETAIL_CHARS + 5000)
    out = clip(_long)
    check("还有" in out and "字符" in out,
          "⭐⭐ 超长正文截断后**明说还有多少** —— "
          "📌 一个没说自己被截断过的显示，会让用户以为那就是全部")
    check("导出" in out,
          "⭐ 并且指出完整内容去哪拿（设置 → 导出数据）—— "
          "📌 截断是**显示**策略，不是数据策略：原文一个字都没少")
    _many = clip("L\n" * (MAX_DETAIL_LINES + 200))
    check("省略" in _many and "行" in _many, "⭐ 多行同理：头尾都留，中间说省了多少")


def t_no_storage_in_u8_path() -> None:
    print("\n[5] ⭐⭐ 结构守卫：这几个方法里**不许出现 storage**")
    src = pathlib.Path("app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in ("_ledger_tool_record", "_fill_tool_detail",
                           "_attach_tool_detail"):
            continue
        # ⚠️ 走 AST 找**属性访问**，不是在源码文本里搜 "storage" ——
        #    那几个函数的注释正在解释"为什么不读 storage"，
        #    📌 只要断言读的是文本，对代码的【解释】就会参与判定。
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and n.attr == "storage":
                bad.append(f"{fn.name} → .storage")
    check(not bad,
          "⭐⭐⭐ 三个方法都不碰 `.storage` —— "
          "🔴 碰了的话 [F5] 一降级，透明度就归零（第 1 项已经真跑过一遍）",
          str(bad))


def main() -> int:
    t_reads_ledger_not_storage()
    t_generic_fallback_covers_everything()
    t_all_builtin_tools_have_detail()
    t_clip_announces_itself()
    t_no_storage_in_u8_path()
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
