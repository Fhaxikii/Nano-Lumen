# -*- coding: utf-8 -*-
"""第三本账 `exchange_decay` —— 身份 + 指纹 + 陈旧判定。

═══ 这个套件在验什么 ═══

⚠️ **建表这一步是 shadow：只记录，不删任何东西。**
（第 2 步 L0→L1 已把它接上；2026-08-14 整片实测验证通过后阶梯已打开。）
它存在的全部意义是：**让「某次交换现在是第几档」这件事有个持久的家** ——
否则第一次重启就会 `hydrate 原始历史 → 全部恢复 L0 → 又衰减一遍`，
**从第一天起就在验证一套「重启不成立」的状态机**。

四条最有价值的不变量：

  ① 🔴 **`source_hash` 算在【落盘账本】上，不算在内存投影上。**
     内存 `storage` 与账本**故意不一样**（图片占位符 / 重启后算回来的注记）——
     拿投影算哈希会让每次重启、每次压缩都产生新哈希，
     于是所有 Digest 被判成"陈旧"、无限重新提炼（无限花钱），
     📌 **而它看起来完全像是「内容真的变了」**。

  ② **「我没验过」和「我验过了没问题」必须分开** ——
     哈希是空串（当时没算出来）**必须**算陈旧，不能当成没问题。

  ③ **fail-safe 方向**：没记录 / 已陈旧 → 一律当 L0。
     📌 猜"它降过级了"会少发内容给模型（信息丢失）；
        猜"它还是原文"最多多花点 token。**往损失小的那一侧倒。**

  ④ **没有稳定身份的交换不许记账**（orphan / 还没落盘的当前轮）。

用法：
  py -3.10 tests\cases\t_f5_decay_store.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _fresh():
    """一套干净的 store + repo + decay。⚠️ 用临时库 ——：测试不许读生产库。"""
    from core.runtime.store import RuntimeStore
    from core.runtime.conversation import ConversationRepository
    from core.context.decay_store import DecayStore
    st = RuntimeStore(pathlib.Path(tempfile.mkdtemp(prefix="nanodecay_")) / "t.db")
    return st, ConversationRepository(st), DecayStore(st)


def M(role, content="x", **kw):
    from core.schema import ChatMessage
    return ChatMessage(role=role, content=content, **kw)


def t_identity_is_derived() -> None:
    print("\n[1] ⭐⭐ 身份是**推导**出来的，不是存的")
    from core.context.exchange import split
    st, repo, _ = _fresh()
    sid = repo.current_session().session_id
    for m in (M("user", "问1"), M("assistant", "答1"), M("user", "问2"), M("assistant", "答2")):
        repo.append_message(sid, m)
    msgs = repo.load_messages(sid)
    ex = split(msgs)
    check(len(ex) == 2, "前置：切出 2 次交换", str(len(ex)))
    check(ex[0].start_ordinal == 1 and ex[0].end_ordinal == 2,
          "⭐ 第一次交换 ordinal 1..2", f"{ex[0].start_ordinal}..{ex[0].end_ordinal}")
    check(ex[1].start_ordinal == 3 and ex[1].end_ordinal == 4,
          "⭐ 第二次交换 ordinal 3..4", f"{ex[1].start_ordinal}..{ex[1].end_ordinal}")
    check(all(e.has_identity for e in ex), "两次交换都有稳定身份")

    # ⚠️ 没落盘的（纯构造对象）→ 没有身份，不许记账
    _fake = split([M("user", "还没落盘"), M("assistant", "答")])[0]
    check(_fake.start_ordinal is None and not _fake.has_identity,
          "⭐⭐ 还没落盘的交换**没有身份** —— "
          "📌 没有稳定身份的东西不该被记账")

    # ⚠️ orphan 段也不许（没有真正的用户开口，写不出 L2/L3）
    _orph = split([M("assistant", "上一轮的尾巴")])[0]
    check(not _orph.has_identity, "⭐ orphan 段也不许记账")
    st.close_thread_conn()


def t_hash_is_on_the_ledger_not_the_projection() -> None:
    """🔴 本套件最重要的一条。"""
    print("\n[2] ⭐⭐⭐ `source_hash` 算在【落盘账本】上，不算在内存投影上")
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    m1 = M("user", "看这张图")
    repo.append_message(sid, m1)
    repo.append_message(sid, M("assistant", "看到了"))

    h1 = dec.source_hash(sid, 1, 2)
    check(len(h1) == 64, "算得出指纹", h1[:12] + "…")

    # ⭐ 内存投影被改（模拟 compress_image_blocks / _restore_image_notes）——
    #    落盘没动 → 指纹**必须不变**
    m1.content = "看这张图\n\n[System note — inserted by Nano's context manager]…"
    h2 = dec.source_hash(sid, 1, 2)
    check(h1 == h2,
          "⭐⭐⭐ 只改内存投影 → 指纹**不变** —— "
          "🔴 若算在投影上，每次重启/每次压缩都会产生新哈希，"
          "所有 Digest 被判陈旧 → 无限重新提炼（无限花钱），"
          "**而它看起来完全像是「内容真的变了」**")

    # ⚠️ 落盘真的变了 → 指纹必须变（反向，否则上面那条可能是"哈希恒定"蒙的）
    repo.append_message(sid, M("user", "又说了一句"))
    check(dec.source_hash(sid, 1, 3) != h1,
          "⚠️ 反向：落盘范围真变了 → 指纹跟着变")

    # ⚠️ 指纹不该跟着 created_at 走
    src = module_text("core.context.decay_store")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "_hash_rows"), None)
    _body = "\n".join((ast.get_source_segment(src, x) or "") for x in (fn.body if fn else [])
                      if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)))
    check(fn is not None and "created_at" not in _body,
          "⚠️ 指纹不含 `created_at` —— 📌 它不是内容，跟着它变会让哈希对「什么都没改」也报警")
    st.close_thread_conn()


def t_record_and_roundtrip() -> None:
    print("\n[3] ⭐ 登记 / 读回 / 状态迁移")
    from core.context.decay_store import L0, L1, L2, DIGEST_SCHEMA_VERSION
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    for m in (M("user", "问"), M("assistant", "答"), M("tool_calls"), M("tool_results")):
        repo.append_message(sid, m)

    check(dec.record(sid, 1, 4, level=L1), "登记 L1")
    e = dec.get(sid, 1)
    check(e is not None and e["level"] == L1 and e["end_ordinal"] == 4, "读得回来")
    check(bool(e["source_hash"]), "⭐ 指纹由本模块自己算（调用方没传）")

    # 同一次交换反复降级 = 同一条记录的状态迁移，不是多条
    dec.record(sid, 1, 4, level=L2, digest={"status": "done", "outcome": "x",
                                            "referents": [], "open_items": []},
               distiller_model="anthropic/claude-haiku-4.5")
    all_rows = dec.load_session(sid)
    check(len(all_rows) == 1,
          "⭐⭐ 反复降级只有**一条**记录 —— "
          "📌 一个「状态」如果每次变化都新增一行，读的时候就得回答「哪一行是现在」",
          f"{len(all_rows)} 条")
    e2 = dec.get(sid, 1)
    check(e2["level"] == L2 and e2["digest_schema_version"] == DIGEST_SCHEMA_VERSION,
          "⭐ 带 digest 时自动打上 schema 版本", str(e2["digest_schema_version"]))
    check(e2["distiller_model"] == "anthropic/claude-haiku-4.5",
          "⭐ 记下是谁提炼的 —— 📌 半年后最难查的是「为什么旧记忆少一个字段」")

    check(not dec.record(sid, 1, 4, level="L9"), "⚠️ 未知档位被拒绝")
    check(dec.level_of(sid, 1) == L2, "level_of 读得对")
    check(dec.level_of(sid, 999) == L0,
          "⭐⭐ 没记录 → 当 L0（fail-safe）—— "
          "📌 猜「降过级了」会少发内容给模型；猜「还是原文」最多多花点 token")
    st.close_thread_conn()


def t_staleness_three_kinds() -> None:
    print("\n[4] ⭐⭐⭐ 三种「陈旧」都要认出来")
    from core.context.decay_store import L0, L2, DIGEST_SCHEMA_VERSION
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    for m in (M("user", "问"), M("assistant", "答")):
        repo.append_message(sid, m)
    dec.record(sid, 1, 2, level=L2, digest={"status": "done", "outcome": "x",
                                            "referents": [], "open_items": []})
    e = dec.get(sid, 1)
    check(not dec.is_stale(e), "前置：刚写的不陈旧")

    # ① 内容变了
    _bad = dict(e); _bad["source_hash"] = "0" * 64
    check(dec.is_stale(_bad), "⭐ ① 哈希对不上 → 陈旧")

    # ② schema 升级了
    _old = dict(e); _old["digest_schema_version"] = DIGEST_SCHEMA_VERSION - 1
    check(dec.is_stale(_old),
          "⭐⭐ ② schema 版本旧 → 陈旧（旧 digest 会少字段）")

    # ③ 当时没算出哈希 —— 最容易被当成"没问题"的那种
    _nohash = dict(e); _nohash["source_hash"] = ""
    check(dec.is_stale(_nohash),
          "⭐⭐⭐ ③ 哈希是空串 → **也算陈旧** —— "
          "📌 **「我没验过」和「我验过了没问题」必须分开**，"
          "把前者当后者正是这一层最怕的静默失真")

    check(dec.is_stale({}) and dec.is_stale(None or {}), "空记录 → 陈旧")
    check(dec.level_of(sid, 1) == L2, "⚠️ [L5] 前置：没被上面几条污染，真记录仍是 L2")
    st.close_thread_conn()


def t_shadow_only() -> None:
    """⚠️ **这条断言在第 2 步被改过一次，改动本身值得记。**

    第 1 步（建表）时它钉的是「生产代码里还没有人调用 `DecayStore`」——
    那是**当时**正确的不变量（shadow：只建家，不接线）。
    第 2 步（L0→L1）刻意把它接上了，于是这条断言当场变红。

    📌 **一条测中间态的断言，在迁移完成后会反过来阻止终态；
       它的正确处置是【改成钉终态】，不是删掉。**
       （同 `t_f4_catalog` 里 `conclude_exploration` 那条的处置。）
    ⭐ 删掉的话，「衰减账本不许碰 conversation_messages」这条就没人守了 ——
       而那才是这一层真正要守的东西。
    """
    print("\n[5] decay ledger never touches conversation_messages")
    src = module_text("core.context.decay_store")
    tree = ast.parse(src)
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and ("DELETE FROM conversation_messages" in n.value
                or "UPDATE conversation_messages" in n.value)]
    check(not bad,
          "⭐⭐⭐ 衰减账本**不碰** `conversation_messages` —— "
          "📌 三本账权威顺序：历史事实永远最高", str(bad))

    # ⭐ 改成钉终态：它现在**确实**被接上了，但**整个包在开关里**
    from core.models import ladder_enabled
    app_src = module_text("core.orchestrator")
    check("DecayStore" in app_src,
          "⚠️ 前置：生产代码里确实接上了（第 2 步做的）")
    # ⚠️ 见 `t_f5_decay_l1.t_off_by_default` 里那段说明：
    #    「默认关闭」是切换期的安全声明，实测验证过之后就过期了。
    #    这里留下的是长期有效的那半：**接线确实存在**（上一条），
    #    以及它**整个包在开关里**（那条 AST 检查在 `t_f5_decay_l1`）。
    check(isinstance(ladder_enabled(), bool),
          "⚠️ 前置：总开关读得出来且是 bool", str(ladder_enabled()))


def main() -> int:
    t_identity_is_derived()
    t_hash_is_on_the_ledger_not_the_projection()
    t_record_and_roundtrip()
    t_staleness_three_kinds()
    t_shadow_only()
    passed = sum(1 for r in _results if r[0])
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
