# -*- coding: utf-8 -*-
"""命令输出全量落盘 —— 「到底输出了什么」（2026-08-26）。

═══ 它修的是什么 ═══

`run_command` 跑完给模型的是 `out[-2000:]`，**而且是静默的**。

🔴 **2000 这个数不是防撑爆的保护**：2000 字符 ≈ 500 token，而外面
   `MemoryManager.MAX_SINGLE_TOOL_RESULT_CHARS = 12000`、上下文是 20 万
   token 级 —— **差两个数量级**。用户的两问：
     「cmd 输出 > 2000 ⇒ 怀疑输出异常？」   —— 不对等。pip install / git log /
        npm test / 任何脚本打一张表，全都轻松过 2000，全都完全正常。
     「cmd 输出 > 2000 ⇒ 有撑爆模型的风险？」—— 没有。
   ⇒ 它筛掉的不是异常，是**「正常但稍长」**，而那恰好是信息量最大的一类。

⭐ 它真正的语义是 用户找出来的：**「命令输出最有价值的部分通常在末尾」**
   —— 那一半是对的，所以内联**仍然取末尾**。
   ⚠️ 错的是它把剩下那 5% **直接堵死**，而且**不告诉任何人**：模型拿到
      2000 字符，不知道后面还有，会当成完整结果去推理。
      📌 与本仓 inbox `delivery_count` / `ActionAttempt` INTERRUPTED 同一条判据：
         **截断可以，不说截断了不行。**

═══ 为什么是「落盘 + 给路径」而不是「向前翻页 API」═══

形状抄的是 **Claude Code 自己**（2026-08-26 实测它的行为）：
    Output too large (152.3KB). Full output saved to: <path>
    Preview (first 2KB): ...
它**不截断**，它落盘 + 给路径。拿到路径之后用 grep / 读某一段 / 只要末尾，
**全由模型自己按实际情况决定**。
📌 而「向前翻页」等于**我们替它决定了检索方式**（只能从尾往前顺序找）——
   用户那个极端例子（10 万字里只要末尾 2100）用 grep 一步就到，
   顺序翻页要翻几十次。
⭐ 顺带它还省掉三样东西：新 action、保留策略、环形缓冲那个天花板。

═══ 🔴🔴 为什么必须「边读边写」═══

数据是在 `self._buf.append()` 那一行丢掉的（`deque(maxlen=300)` 满了自动
丢最老的）。**等命令跑完再想从缓冲写文件，手上只剩最后 300 行** ——
要救的那几千行早就不存在了。
⇒ 落盘必须发生在**行到达的那一刻**，与入缓冲是同一个动作的两个去向：

    读线程收到一行
          ├──→ deque(maxlen=300)   内存，只留尾巴  →「现在怎么样了」要快、要新
          └──→ 追加写文件 磁盘，全量不丢  →「到底输出了什么」要全

📌 而 `out[-2000:]` 之所以像个 bug，根子就在这：**它在一个只服务
   「现在怎么样了」的数据结构上，去取「到底输出了什么」的答案。**

用法：
  py -3.10 tests\cases\t_cmd_output_spill.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

MB = 1024 * 1024
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _run(cmd: str, wait: float = 60.0):
    import core.os_layer.longcmd as LC
    lc = LC.start(cmd, shell=True)
    t0 = time.time()
    while lc.running and time.time() - t0 < wait:
        time.sleep(0.15)
    return lc


def _py(code: str) -> str:
    return f'python -c "{code}"'


# ══════════════════════════════════════════════════════════════════════════
def t_small_output_unchanged() -> None:
    print("\n[1] ⭐ 小输出：行为一个字不变（95% 的情况）")
    r = _run(_py("print('hello'); print('world')")).final_result()
    d = r["data"]
    check(d["output"] == "hello\nworld",
          "⭐⭐ 输出原样给出，**没有任何多余的提示**", repr(d["output"]))
    check(d["truncated"] is False, "truncated=False")
    check(not d["output_file"],
          "⭐ 不留文件 —— 模型已经全看到了，那个文件没有存在的理由", d["output_file"])
    check(r["ok"] is True and d["returncode"] == 0, "成功/返回码照旧")


def t_no_tiny_file_flood() -> None:
    print("\n[2] 🔴 小输出不许在磁盘上堆小文件")
    # 🔴 第一版是「有输出就建文件、跑完就留着」→ `print('hello')` 也留一个
    #    12 字节的文件。而按**字节**的预算**永远不会触发**清理（它们太小）
    #    ⇒ **文件数无限增长**。
    # 📌 这是「按字节 vs 按数量」的反面：字节预算不约束文件个数，
    #    而几万个小文件本身就是问题。
    import core.os_layer.longcmd as LC
    before = len(list(LC._SPILL_DIR.glob("*.txt"))) if LC._SPILL_DIR.is_dir() else 0
    for i in range(5):
        _run(_py(f"print('tiny{i}')"))
    after = len(list(LC._SPILL_DIR.glob("*.txt"))) if LC._SPILL_DIR.is_dir() else 0
    check(after == before,
          "⭐⭐⭐ 连跑 5 条小命令，磁盘上**一个文件都没多**", f"{before} → {after}")


def t_big_output_is_complete_on_disk() -> None:
    print("\n[3] ⭐⭐⭐ 大输出：环形缓冲只有 300 行，文件里要有全部 3000 行")
    import core.os_layer.longcmd as LC
    lc = _run(_py("[print(f'L{i:05d} '+'x'*40) for i in range(1,3001)]"))
    d = lc.final_result()["data"]

    check(d["total_lines"] == 3000,
          "⭐ 总行数如实计数（`len(_buf)` 封顶 300，它答不了这个）", str(d["total_lines"]))
    check(len(lc._buf) == LC._MAX_LINES,
          f"⚠️ 而缓冲里确实只有 {LC._MAX_LINES} 行 —— 落盘不是从它来的", str(len(lc._buf)))
    check(d["truncated"] is True, "truncated=True")

    f = pathlib.Path(d["output_file"])
    check(f.is_file(), "落盘文件在", d["output_file"])
    txt = f.read_text(encoding="utf-8")
    check("L00001 " in txt,
          "🔴🔴 **第一行在文件里** —— 它在缓冲里【早就被挤掉了】，"
          "这就是「必须边读边写」的证据")
    check("L03000 " in txt, "最后一行也在")
    check(txt.count("\n") == 3000, "⭐ 3000 行一行不少", str(txt.count("\n")))


def t_truncation_is_never_silent() -> None:
    print("\n[4] 🔴🔴 截断可以，**静默不行**")
    d = _run(_py("[print(f'L{i:05d} '+'x'*40) for i in range(1,3001)]")).final_result()["data"]
    out = d["output"]
    check("[output truncated" in out, "⭐⭐ 明说被截断了")
    check("3000 line" in out, "⭐ 说清一共多少行（不是含糊的「还有更多」）")
    check("LAST" in out, "⭐ 说清给的是**尾巴**（不是开头，也不是随机一段）")
    check(d["output_file"] and d["output_file"] in out,
          "⭐⭐⭐ 给出完整输出的**路径** —— 只说「你少了东西」不说「怎么拿」，"
          "只会让模型原地重试")
    check("do NOT re-run the command" in out,
          "⚠️ 明确挡住「再跑一次看看」——那会重复副作用")
    # ⭐ 内联的仍然是尾巴：命令的答案通常在末尾（成没成、报什么错）
    head = out.split("[output truncated")[0]
    check("L03000 " in head and "L00001 " not in head,
          "⭐⭐ 内联给的是**末尾** —— `out[-2000:]` 唯一正确的那一半保留了")


def t_spill_failure_says_so() -> None:
    print("\n[5] ⚠️ 落盘失败时必须说没有，不能给一个不存在的路径")
    src = module_text("core.os_layer.longcmd")
    # 📌 「失败信息必须正确」—— 指向一个不存在的文件比不指路更糟：
    #    模型会去读，然后拿到第二个错误。
    check("could NOT be saved to disk" in src,
          "⭐⭐ 有「存不下来」这一支的文案")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "final_result"), None)
    check(fn is not None and "if _p:" in ast.unparse(fn).replace("if _p:", "if _p:"),
          "路径为空时走另一支（不硬拼一个路径出来）")
    check("_spill = False" in src,
          "⭐ 落盘失败标记成 False（试过且失败，别每行都重试一次）")
    # ⚠️ 落盘是附加能力，不是主功能
    fn2 = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_spill_open"), None)
    check(fn2 is not None and any(isinstance(x, ast.Try) for x in ast.walk(fn2)),
          "⭐ 建文件包在 try 里 —— **落盘失败不许影响命令本身**")


def t_recycle_by_bytes() -> None:
    print("\n[6] ⭐ 输出目录按体积回收，且不许删还在跑的")
    import core.os_layer.longcmd as LC
    src = module_text("core.os_layer.longcmd")
    check(LC._SPILL_BUDGET_BYTES == 50 * MB,
          "⭐ 预算 50MB —— 比截图的 200MB 小，因为**有效寿命差一个数量级**："
          "截图是审计证据（价值在以后），命令输出读完就没用了",
          f"{LC._SPILL_BUDGET_BYTES // MB}MB")
    check("live_refs()" in src,
          "🔴 回收时跳过**还在跑**的命令的文件（它正被写着）")
    check("_prune_spill_dir" in src and "lc._spill_close()" in src,
          "⭐ 回收挂在「命令结束」这条心跳上（拥有目录的人负责回收）")
    # ⚠️ 与截图的实质差别：这里**没有地板**
    check("没有地板" in src or "**没有地板**" in src,
          "⚠️ 留痕写清了为什么这里没有地板（命令输出没有事后取证价值）")

    # 真跑一次回收
    LC._SPILL_DIR.mkdir(parents=True, exist_ok=True)
    olds = []
    for i in range(3):
        f = LC._SPILL_DIR / f"cmdout_fake{i}.txt"
        f.write_bytes(b"x" * (30 * MB))
        os.utime(f, (1000 + i, 1000 + i))
        olds.append(f)
    LC._prune_spill_dir()
    left = sum(f.stat().st_size for f in LC._SPILL_DIR.glob("*.txt"))
    check(left <= LC._SPILL_BUDGET_BYTES,
          "⭐⭐ 90MB → 收进 50MB 预算内", f"{left // MB}MB")
    check(not olds[0].exists(), "删的是最旧的")
    for f in olds:
        try:
            f.unlink()
        except OSError:
            pass


def t_buffer_and_file_serve_different_questions() -> None:
    print("\n[7] ⭐⭐ 缓冲和文件各司其职（这是整个设计的支点）")
    src = module_text("core.os_layer.longcmd")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_append"), None)
    check(fn is not None, "找得到 _append")
    if fn is not None:
        u = ast.unparse(fn)
        # 🔴 两个去向必须在同一个函数里 —— 数据是在 append 那一行丢掉的
        check("_buf.append" in u and "_spill_write" in u,
              "🔴🔴🔴 **同一行输出的两个去向在同一处** —— "
              "等跑完再写文件的话，手上只剩最后 300 行")
        check("_total_lines" in u, "⭐ 总行数在这里累加（缓冲数不出来）")

    # ⚠️ 进度那一眼仍然读缓冲，不读文件
    pn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "progress_note"), None)
    check(pn is not None and "_spill" not in ast.unparse(pn),
          "⭐⭐ 回看进度**只读缓冲** —— 每 3 秒去磁盘读一次文件只为拿 20 行，"
          "是把「要快」的问题交给了「要全」的工具")


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 74)
    print("命令输出全量落盘（找回被 out[-2000:] 吃掉的那 90%）")
    print("=" * 74)
    t_small_output_unchanged()
    t_no_tiny_file_flood()
    t_big_output_is_complete_on_disk()
    t_truncation_is_never_silent()
    t_spill_failure_says_so()
    t_recycle_by_bytes()
    t_buffer_and_file_serve_different_questions()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
