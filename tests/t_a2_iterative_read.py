# -*- coding: utf-8 -*-
"""迭代阅读 + scratchpad（2026-08-26）。

═══ 盘点：原本记着的四个坑，回代码之后**三个自己消失了** ═══

    坑一 只能改写不能删 tool_result   ✅ 真的存在 → 做了（唯一真正要做的）
    坑二 scratchpad 载体没定          ❌ `AgentDecision.discarded_text` 已在（顺手做的）
    坑三  MAX_FILE_TOOL_CHAIN 卡住迭代  ❌ 常量还在，但**全仓零使用点**，拦不住任何东西
    坑四  meta 先解析全文所以贵         ❌ 它不是贵，是**零调用方**

📌 而三个「消失」的方式各不相同，值得分开记：
    坑二 别的项目顺手解决了它 —— **留痕记的是「当时讨论到哪」，不是「现在缺什么」**
    坑三 代码演进把它变成了死常量 —— 没人注意到它已经不生效
    坑四 它从一开始就没有调用方 —— **那个坑描述的是一个假问题**
⇒ 这正是索引第 3 步（**回代码确认状态**）存在的理由：照着留痕落地，
  会去解三个已经不存在的问题，而真正挡路的那个（见下）留痕里根本没写。

═══ 🔴🔴 而真正挡路的那个，留痕里一个字都没有 ═══

`core/rag.py` 的 `LOAD_FULL_MAX_CHARS = 300_000`：文件超过它就降级成
「章节标题 + 前 5000 字 + 建议改用 RAG」。
⇒ 早先的设计 526K 字符，`load_full_file` 实际只返回 **7,196 字符（1.4%）** ——
  在上层做迭代阅读，切的是一份**已经被砍剩 1.4%** 的东西。
📌 它护错了位置：贵的是**解析**（峰值内存 10×），它砍的是**解析之后**的文本，
  那一刻峰值已经过去了。护内存的是 `MAX_FILE_SIZE_MB`（解析前），保留。
⭐⭐ 顺带一个值得单独记的巧合：的验收案例是「一份 **20 万字**的文档」，
  中文 20 万字 ≈ 20 万字符，**刚好卡在 300,000 以下** ——
  📌 **一个「刚好够用」的阈值，会让验收通过而能力不成立。**

用法：
  py -3.10 tests\\t_a2_iterative_read.py
"""
from __future__ import annotations

import ast
import asyncio
import atexit
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

# ══════════════════════════════════════════════════════════════════════════
# 大文件夹具 —— **测试自己造，不指向仓库里任何真实文件**
# ══════════════════════════════════════════════════════════════════════════
# 🔴 它曾经指向一份内部文档 —— 那份文档
#    **不随仓库发行**。于是克隆下来的人一跑就是红的，而无从判断这红
#    是不是自己弄出来的。📌 一个必然失败的测试，等于没有测试。
# 🔴 改指 `app.py` / `core/orchestrator.py` 这类大文件同样不行：
#    它们在计划中要被拆。拆完之后名字不一定还在，就算在也一定不再够大 ——
#    那等于**给重构埋一条没人写下来的约束**（「这个文件必须保持 40 万字符以上」），
#    而且它会在拆分进行到一半、最需要测试套件当信号的时候变红。
# 📌 所以夹具的唯一职责就是满足这条测试的形状。它不进仓库，跑完即消失，
#    没有任何人会因为改别的代码而需要担心动到它。
_FIXTURE_DIR = tempfile.TemporaryDirectory(prefix="nano_t_a2_", ignore_cleanup_errors=True)
atexit.register(_FIXTURE_DIR.cleanup)

# 🔴 **300,000 不是随便挑的数**：它是 `LOAD_FULL_MAX_CHARS` 的原值，
#    正是那道闸把 526K 的文档砍成 7,196 字符（1.4%）。
#    ⇒ 夹具必须**显著大于**它，这条测试才证明得了「闸已经拆了」。
_FORMER_CAP = 300_000            # `core/rag.py` 里那道已停用的闸的原值
_SECTIONS   = 9_000              # ×2 行/节 = 18,000 行，约 55 万字符 ≈ 门槛的 1.8 倍


def _build_fixture() -> tuple[str, str]:
    """造一大一小两份文档，返回 (大文件路径, 小文件路径)。内容确定性，无随机。"""
    d = pathlib.Path(_FIXTURE_DIR.name)
    big = d / "big_document.md"
    rows = []
    for i in range(_SECTIONS):
        rows.append(f"## 第 {i} 节  锚点-{i:05d}")
        rows.append(f"这一行是第 {i} 段正文，用来把文件撑到超过 {_FORMER_CAP:,} 字符的规模。")
    big.write_text("\n".join(rows), encoding="utf-8")
    assert len(big.read_text(encoding="utf-8")) > _FORMER_CAP, "夹具没到门槛，这条测试会假绿"
    small = d / "small_document.md"
    small.write_text("# 小文档\n\n它的唯一作用是「小到不该触发试读闸」。\n", encoding="utf-8")
    return str(big), str(small)


BIG, SMALL = _build_fixture()
# 夹具里一定存在、且只在夹具里出现的串，供内容检索用
_ANCHOR = "锚点-04242"

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Q:
    def __init__(self):
        self.items = []

    def put_nowait(self, x):
        self.items.append(x)


def _mk():
    from core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._peeked = {}
    o._rag_parallel_sem = asyncio.Semaphore(2)
    o._full_file_hit_this_turn = False
    o._wm_add = lambda *a, **k: None
    return o


def _text(x):
    return getattr(x, "text", x)


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
def t_units() -> None:
    print("\n[1] 🔴 寻址按【行】，配额按【字符】")
    from core import reading as R
    t = pathlib.Path(BIG).read_text(encoding="utf-8")

    # 🔴 行不能当配额：实测跨 11 份真实文档，字符/行的跨度是 **16 倍**
    #    （docx 英文论文 297 vs pdf 中文公文 18）。同样读 200 行，
    #    一个 4,000 字符（几乎没读到），一个 59,400 字符（一次撑爆）。
    s = R.slice_lines(t)
    check(len(s["body"]) <= R.READ_STEP_CHARS,
          "⭐ 不给 limit → 按**字符预算**切，不是按固定行数",
          f"{len(s['body']):,} ≤ {R.READ_STEP_CHARS:,}")
    check(s["capped_by"] == "budget", "而且它知道自己是被预算截的", s["capped_by"])

    s2 = R.slice_lines(t, offset=100, limit=50)
    check(s2["end"] - s2["start"] + 1 == 50,
          "⭐ 给了 limit → 按**行**（「模型拥有阅读主权」）")

    s3 = R.slice_lines(t, offset=1, limit=99999)
    check(len(s3["body"]) <= R.MAX_READ_CHARS and s3["capped_by"] == "hard_cap",
          "⭐⭐ 但一律不许超过硬上限（安全兜底）",
          f"{len(s3['body']):,}")

    # ⭐ 全局信息每次都给 —— 没有它，分片阅读退化成「读一段猜一下」
    r = R.render(s, filename="x.md")
    for token in ("of ", "lines", "chars", "offset="):
        check(token in r, f"⭐ 渲染里带着「{token}」—— 全局信息是阅读的前提")


def t_peek_gate() -> None:
    print("\n[2] ⭐⭐ 大文件必须先试读，而**拒绝要算未执行**")
    from core.orchestrator import ToolOutcome
    from core.orchestrator import Orchestrator
    o, q = _mk(), _Q()
    r = _run(Orchestrator._handle_load_full_file(o, {"filename": BIG}, "a", event_queue=q))
    check(isinstance(r, ToolOutcome) and r.failed,
          "🔴🔴 拒绝标成 **failed=True** —— 实测 2026-08-26 抓到：返回裸字符串时"
          "工具卡显示「加载文件 ✓」，而它**什么都没加载**。"
          "📌 一次没做成的事显示成成功，比不显示更糟",
          f"failed={getattr(r, 'failed', None)}")
    check("peek_file" in _text(r) and "search_files" in _text(r),
          "⭐ 拒绝信息给出**两条**出路（先试读 / 已经定位过就带 offset 来）—— "
          "失败信息要足够选出下一步")

    # 小文件不受闸约束
    o2 = _mk()
    r2 = _text(_run(Orchestrator._handle_load_full_file(o2, {"filename": SMALL}, "a", event_queue=q)))
    check(r2.startswith("[Lines"),
          "⭐ 小文件**不弹试读**，一轮读完 —— 试读它纯属多花一轮")


def t_first_peek_is_forced() -> None:
    print("\n[3] 🔴 第一次试读强制从头 + 固定长度")
    from core import reading as R
    from core.orchestrator import Orchestrator
    o, q = _mk(), _Q()
    # 📌 「你不先看一部分、没拿到这个文件的任何上下文信息，根本无法决策
    #    接下来要看多少、要看哪里。所以试读就算给模型决策，它也是纯猜。」
    #    ⇒ **试读是决策的前提，所以它自己不能是决策的产物。**
    r = _run(Orchestrator._handle_peek_file(
        o, {"filename": BIG, "offset": 5000, "limit": 999}, "a", event_queue=q))
    check(r.startswith("[PEEK 1-"), "⭐⭐ 无视模型给的 offset，从第 1 行开始", r.splitlines()[0][:48])
    check("ignored" in r,
          "⚠️ 而且**明说被忽略了**，不静默吞掉 —— 静默的话模型会以为自己选中了")
    check("map, not the content" in r,
          "⭐ 第一次试读明说「这是地图不是内容」")

    # 之后自由
    r2 = _run(Orchestrator._handle_peek_file(
        o, {"filename": BIG, "offset": 5000, "limit": 6}, "a", event_queue=q))
    check(r2.startswith("[PEEK 5,000-5,005"),
          "⭐⭐ 第二次起**自选位置和长度** —— 它已经有线索了，悖论不再成立",
          r2.splitlines()[0][:48])
    check("ignored" not in r2, "⭐ 强制提示正确消失")


def t_grep_counts_as_peek() -> None:
    print("\n[4] ⭐⭐ grep 命中过的文件，视同已试读")
    from core.orchestrator import Orchestrator
    o, q = _mk(), _Q()
    # 🔴 实测抓到的浪费：模型撞上闸 → grep 拿到**精确行号** → 又调 load_full_file
    #    → **又被拒** → 下一轮才 peek。闸白吃了两次工具调用。
    # 📌 试读的目的是「在没有线索时提供线索」，而它此刻已经有了 ——
    #    而且 grep 给的比试读更精准。
    _run(Orchestrator._handle_search_files(o, {
        "path": _FIXTURE_DIR.name,
        "name_pattern": "big_document*.md", "content": _ANCHOR}, "a"))
    check(any(v == "via_search" for v in o._peeked.values()),
          "⭐⭐⭐ 内容命中会记进试读账", str(list(o._peeked.values())))
    r = _text(_run(Orchestrator._handle_load_full_file(
        o, {"filename": BIG, "offset": 8500}, "a", event_queue=q)))
    check(r.startswith("[Lines 8,500"), "⇒ 精读**直接放行**，不再浪费一轮", r.splitlines()[0][:44])

    # ⚠️ 只按文件名找到的不算
    o2 = _mk()
    _run(Orchestrator._handle_search_files(o2, {
        "path": _FIXTURE_DIR.name,
        "name_pattern": "big_document*.md"}, "a"))
    check(not o2._peeked,
          "⚠️ 只按**文件名**找到的（Glob）**不算** —— "
          "📌「知道这个文件存在」和「知道它里面有什么」是两回事，而闸拦的是后者")

    # ⭐ 而它消除的只是那次强制，peek 本身照常可用
    r3 = _run(Orchestrator._handle_peek_file(
        o, {"filename": BIG, "offset": 8500, "limit": 6}, "a", event_queue=q))
    check("ignored" not in r3 and r3.startswith("[PEEK 8,500"),
          "⭐ 消除的**只是那次强制**：之后 peek / 精读由模型按情况自己选")


def t_scratchpad_and_compression() -> None:
    print("\n[5] ⭐⭐⭐ 恒定上下文厚度 = notes + 丢弃旧切片（缺一个都不成立）")
    import core.orchestrator as O
    from core import reading as R
    from core.schema import ChatMessage, ToolResultBlock
    from memory.manager import MemoryManager

    props = O._LOAD_FULL_FILE_MANIFEST["parameters"]["properties"]
    check("notes" in props,
          "⭐ scratchpad 的载体是**工具参数** —— 原设计要模型「在特定 "
          "XML/Markdown 块中输出」，那依赖模型愿意在调工具时同时写正文，"
          "📌 而一个「靠模型自觉产出」的载体，漏一轮就断一轮，而且不会有人知道")
    check("gone" in props["notes"]["description"],
          "⭐ 描述里明说「没写下来的就没了」—— 不说的话模型没有理由去写")

    # 丢弃旧切片
    m = MemoryManager.__new__(MemoryManager)
    m.storage = []

    def _add(fn, s, e, n):
        head = f"[Lines {s:,}-{e:,} of 11,174 lines / 526,203 chars · {fn}]"
        m.storage.append(ChatMessage(role="tool_results", tool_results=[
            ToolResultBlock(name="load_full_file", tool_use_id=f"t{len(m.storage)}",
                            content=head + "\n" + "x" * n)]))

    _add("a.md", 1, 198, 20000)
    _add("a.md", 199, 400, 20000)
    _add("b.md", 1, 100, 5000)
    _add("a.md", 401, 600, 20000)
    _add("b.md", 101, 200, 5000)
    before = sum(len(t.content) for msg in m.storage for t in msg.tool_results)
    n1 = m.compress_file_reads()
    after = sum(len(t.content) for msg in m.storage for t in msg.tool_results)
    check(n1 == 3 and after < before // 2,
          "⭐⭐⭐ 旧切片被换成占位符 —— **这是「恒定厚度」唯一的落地点**。"
          "没有它，notes 只是多存一份东西，原文照样堆着，"
          "📌 只做一半（写了 notes 却不丢旧切片）= 原文照样堆着再加一份笔记，**比不做还贵**",
          f"压 {n1} 段，{before:,} → {after:,}")

    kept = [t.content for msg in m.storage for t in msg.tool_results
            if "dropped to keep" not in t.content]
    check(len(kept) == 2,
          "⭐ 每份文件**保留最后一段** —— 那是模型当前的工作面，"
          "📌 丢掉它不是压缩，是失忆")
    check(all("Read it again" in t.content for msg in m.storage
              for t in msg.tool_results if "dropped to keep" in t.content),
          "⚠️ 占位符说清**怎么拿回来** —— 只说「已省略」的话模型会以为永远没了，"
          "于是要么放弃、要么凭记忆编")

    check(m.compress_file_reads() == 0,
          "🔴 **幂等** —— 第一版用 `startswith('[dropped')` 判断，而占位符前面还留着"
          "那个头，所以那个判断**永远为假**：功能是对的，但返回值每轮都在撒谎。"
          "📌 一个只在返回值上错的 bug 最难发现")

    _add("a.md", 601, 800, 20000)
    check(m.compress_file_reads() == 1, "⭐ 又读一段之后，原来保留的那段成了旧的")


def t_rag_cap_is_gone() -> None:
    print("\n[6] 🔴🔴 RAG 层那道 300K 截断已取消（否则 [A2] 整个落空）")
    src = module_text("core.rag")
    check("LOAD_FULL_MAX_CHARS = 1 << 62" in src,
          "⭐⭐ 阈值改成哨兵值 = 不再截断")
    tree = ast.parse(src)
    lit = sum(1 for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and n.value == "[File Too Large — Partial Content Only]\n")
    check(lit == 0, "⭐ 那段降级文案已删除（不再有「读不完就去用 RAG」这条路）", str(lit))
    check("🪦" in src and "护错了位置" in src,
          "🪦 删除处留了墓碑：贵的是**解析**，而它砍的是**解析之后**的文本")
    check("MAX_FILE_SIZE_MB = 50" in src,
          "⚠️ 而护内存的那道闸（解析**之前**）**保留** —— 位置是对的")

    # 真读一次：拿到的必须是完整文本
    from core import rag as R
    t = R.load_full_file(BIG)
    # ⭐ 断言的是「**拿全了**」，不是「大于某个魔数」——
    #    后者在夹具换了尺寸之后会悄悄失去意义。
    _on_disk = len(pathlib.Path(BIG).read_text(encoding="utf-8"))
    check(len(t) >= _on_disk and _on_disk > _FORMER_CAP,
          "⭐⭐⭐ 超过原上限的文件被**完整**读回（此前只剩 1.4%）",
          f"{len(t):,} 字符")


def t_dead_pit_four() -> None:
    print("\n[7] ⭐ 坑四是个假问题：`load_full_file_meta` 零调用方")
    import re
    hits = []
    for f in ROOT.rglob("*.py"):
        if "site-packages" in str(f) or f.name.startswith("t_"):
            continue
        src = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"load_full_file_meta\s*\(", src):
            # 排除定义本身
            # ⚠️ `"def ".strip()` 是 `"def"`，再判 `.startswith("def ")`（带空格）
            #    **永远为假** —— 第一版就是这么把定义行当成调用方算进去的。
            #    📌 先 strip 再判一个带空格的前缀，是个自相矛盾的判断。
            line_start = src.rfind("\n", 0, m.start()) + 1
            if src[line_start:m.start()].lstrip().startswith("def"):
                continue
            hits.append(f.name)
    check(not hits,
          "⭐⭐ 全仓**没有任何代码调它** —— 📌 一个没有调用方的函数"
          "不构成任何人的阻塞；给它做「廉价路径」优化，是在优化一条没人走的路",
          str(hits))
    src = module_text("core.rag")
    check("零调用方" in src, "🪦 函数上留了墓碑，说清那个坑的前提不成立")


# ══════════════════════════════════════════════════════════════════════════
def t_read_tool_prompts() -> None:
    """读文件这件事上，**三个工具的提示词互相指得对不对**。

    起因是 用户的一个顾虑：「nano 会不会把这个阅读工具用在 os 擅长的领域内，
    比如看一个代码文件」。回代码之后**方向正好反过来**：

        load_full_file    offset/limit ✓  试读闸 ✓  notes ✓  跨轮压缩 ✓
        os_execute        ✗ 没有 offset   ✗        ✗        ✗
          file_read       全文进对话，靠 MAX_SINGLE_TOOL_RESULT_CHARS 事后截断
        run_command+type  内联上限 2000 字符/轮

    ⇒ **读代码最好的工具就是 load_full_file**，站不住的是另外两条。
      而它的描述里当时**一个字都不沾代码**（满口 KB / RAG / documents），
      📌 连作者本人都被这段措辞带偏了 —— 模型没有理由不被带偏。
    """
    print("\n▶ 读文件三工具的互指")
    from core.orchestrator import (Orchestrator as _O,
                                   _LOAD_FULL_FILE_MANIFEST as _LFM,
                                   _OS_MANIFEST as _OSM)
    from memory.manager import MemoryManager as _MM

    _lf = _LFM["description"]
    check("search_files" in _lf,
          "🔴 「找一个具体关键词」指向了 `search_files` —— 📌 原来只指向 "
          "`query_local_knowledge`，而**磁盘上的文件根本不在知识库里**："
          "模型会去一条注定查不到的路，白烧一轮")
    check("query_local_knowledge" in _lf,
          "⚠️ 知识库那条没被顺手删掉 —— 两个去处**各管一半**，不是二选一")
    check("source code" in _lf,
          "⭐ 用途里明写了 reading source code —— 📌 一份满口 documents 的描述，"
          "会让模型（和作者）以为它只管文档")

    _os = _OSM["description"]
    check("load_full_file" in _os and "file_read" in _os,
          "⭐⭐ `os_execute` 自己的描述里点出了 file_read 的短板和那条替代路 —— "
          "⚠️ **只能写在这里**：`dsl.ActionDef.desc` 那句写得没错，但它零消费方")
    check("offset/limit" in _os,
          "⚠️ 说的是**能力**（切片读），不是工具名 —— 📌 只报名字等于让它自己猜为什么")
    check(not any(_w in _os.split("file_read hands you")[-1][:400]
                  for _w in (" must ", " should ")),
          "⭐ 那句话里没有 must / should —— 定的是：「不要强制要求它去用，"
          "只是告诉它存在这个办法，具体情况具体判断」")

    # ── `.desc` 零消费方：这条钉子防的是「把劝阻写回一个没人读的地方」 ──
    import re as _re
    _hits = []
    for f in ROOT.rglob("*.py"):
        if "__pycache__" in str(f) or f.parent.name == "tests":
            continue
        for _l in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if _re.search(r"\.desc\b", _l) and not _l.lstrip().startswith("#"):
                _hits.append(f"{f.name}: {_l.strip()[:50]}")
    check(not _hits,
          "🔴 `ActionDef.desc` 全仓**零消费方** —— 📌 它不进任何提示词，"
          "写在那里的劝阻模型一个字也看不到。这条钉住「别再写回去」",
          str(_hits[:3]))

    # ── 事后提示：真被截断了才说，并且说得出下一步 ──────────────────
    class _Mem:
        MAX_SINGLE_TOOL_RESULT_CHARS = 12_000

    class _P:
        memory = _Mem()
    _P._n = _O._file_read_truncation_note
    _probe = _P()

    _big = {"ok": True, "data": {"path": r"C:\x\provider.py", "content": "x" * 32_242}}
    _note = _probe._n("file_read", _big, "y" * 20_000)
    check(bool(_note), "⭐⭐ file_read 撞上截断闸时给出提示")
    check("32,242" in _note and "12,000" in _note,
          "⚠️ 提示里**两个数都在**：总长 和 能到达的量 —— "
          "📌 只说「被截断了」回答不了「还差多少」")
    check("load_full_file" in _note and "offset/limit" in _note,
          "⭐⭐⭐ **说了截断之后还给出下一步** —— 📌 `_compress_tool_results_inplace` "
          "早就附了 `[...truncated]`，所以缺的从来不是「有没有说截断」，"
          "是**说完之后它能干什么**")
    check("your call" in _note.lower(),
          "⭐ 末句把决定权交回去 —— 同「模型拥有阅读主权」")
    check(not any(_w in _note for _w in ("must ", "should ", "You need to")),
          "⚠️ 没有一个命令词")
    check(r"C:\x\provider.py" in _note,
          "⭐ 带上了路径 —— 📌 给出口就要给**能直接用**的出口，"
          "让它回头翻上一条结果找路径，等于出口给了一半")

    check(not _probe._n("file_read", {"ok": True, "data": {"content": "x" * 500}},
                        "y" * 600),
          "⚠️ 小文件**一个字都不多说** —— 📌 一句在不需要时也出现的提示，"
          "会训练模型忽略它")
    check(not _probe._n("file_read", {"ok": False, "error": "no"}, "y" * 20_000),
          "⚠️ 失败的读不提示（没有「还有多少没读」这回事）")
    check(not _probe._n("run_command", _big, "y" * 20_000),
          "🔴 只对 `file_read` 说 —— 📌 `run_command` 的截断有它自己的出口"
          "（落盘+路径），在这里插一句 load_full_file 是**指错路**")
    check(not _probe._n("file_read", None, "y" * 20_000), "⚠️ 结果不是 dict 时不炸")

    # ── 判据同源：阈值只有一处 ──────────────────────────────────────
    _Mem.MAX_SINGLE_TOOL_RESULT_CHARS = 50_000
    check(not _probe._n("file_read", _big, "y" * 20_000),
          "⭐⭐⭐ 阈值调到 50,000 后**同一份输入不再提示** —— "
          "📌 判据直接读 `MemoryManager.MAX_SINGLE_TOOL_RESULT_CHARS`，"
          "不在这里另写一个数：两处各写一个数，一旦分叉就会「说没截其实截了」，"
          "**那比不提示更坏**")
    _Mem.MAX_SINGLE_TOOL_RESULT_CHARS = "坏了"
    check(not _probe._n("file_read", _big, "y" * 20_000),
          "⚠️ 常量取不到时**宁可不提示**，而不是猜一个数顶上")
    check(isinstance(_MM.MAX_SINGLE_TOOL_RESULT_CHARS, int),
          "⭐ 真的那个常量还在且是整数",
          str(_MM.MAX_SINGLE_TOOL_RESULT_CHARS))

    # ── 前置，不是追加 ──────────────────────────────────────────────
    _src = module_text("core.orchestrator")
    check("result_text = _fr_note + result_text" in _src,
          "🔴🔴 提示**前置**到 result_text —— ⚠️ 截断切的是尾巴"
          "（`content[:limit]`），附在后面的话正好在需要它的那一刻被切掉。"
          "📌 形状同旁边的 `_win_note`，同一个理由")


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 74)
    print("迭代阅读 + scratchpad")
    print("=" * 74)
    t_units()
    t_peek_gate()
    t_first_peek_is_forced()
    t_grep_counts_as_peek()
    t_scratchpad_and_compression()
    t_rag_cap_is_gone()
    t_dead_pit_four()
    t_read_tool_prompts()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
