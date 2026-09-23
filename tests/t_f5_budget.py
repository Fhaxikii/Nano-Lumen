# -*- coding: utf-8 -*-
"""两层配置表 + 上下文预算 + 压力注入 + 监控卡。

═══ 这个套件在验什么 ═══

观测层的最后四项：`context_window` / 配置表两层合并 / 压力对模型可见 / 监控卡。
⚠️ **全部是观测层，零行为改变** —— 阶梯（真的去衰减）是后一步。

三条最有价值的不变量：
  ① **价格只有一个权威**。搬表最容易留下"两处都在算"的半截状态，
     而那种 bug 的表现是「用户改了价，账单没变」—— 查不出原因。
  ② **量不到时不许显示 0%**。📌 一个"我不知道"被渲染成 0%，比不显示更糟：
     它看起来像一条真数据。
  ③ **水位单位是窗口百分比**，不是绝对 token 数。Haiku 200K / Sonnet·Opus 1M，
     同一个绝对阈值在两端意义完全不同，而**默认档恰好是那个 200K 的**。

用法：
  py -3.10 tests\t_f5_budget.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_two_layer_table() -> None:
    print("\n[1] ⭐⭐ 两层配置表：厂商→提炼器 ／ 模型→价格+窗口+配额")
    from core import models as M

    tbl = M.load(force=True)
    check(bool(tbl), "读得到 data/model_config.json", f"厂商 {sorted(tbl)}")
    check("anthropic" in tbl, "⚠️ [L5] 前置：至少有 anthropic 这一层")

    for mid in M.known_models():
        w, p, q = M.window_of(mid), M.price_of(mid), M.quota_of(mid)
        if not (w > 0 and p["input_per_1m"] > 0 and q):
            check(False, "每个模型都有窗口/价格/配额三样", f"{mid} 缺项")
            break
    else:
        check(True, "⭐ 每个模型都有窗口/价格/配额三样", f"{len(M.known_models())} 个")

    check(M.window_of("anthropic/claude-haiku-4.5") == 200_000,
          "⭐ Haiku 4.5 = 200K —— **默认档，也是最需要早压的那个**")
    check(M.window_of("anthropic/claude-sonnet-5") == 1_000_000,
          "Sonnet 5 = 1M")
    check(M.window_of("anthropic/claude-opus-5") == 1_000_000,
          "Opus 5 = 1M")

    # 📌 「主模型与提炼模型同厂」是硬约束，不是建议 —— 跨厂 = 用户没有那把 key
    for mid in M.known_models():
        d = M.distiller_for(mid)
        if d and M.vendor_of(d) != M.vendor_of(mid):
            check(False, "⭐⭐ 提炼器与主模型同厂", f"{mid} → {d} 跨厂了")
            break
    else:
        check(True, "⭐⭐ 每个模型的提炼器都与它同厂 —— "
                    "📌 硬约束不是建议：跨厂就意味着用户没有那把 key")

    # 未知模型：保守取小 + 价格 0 + 提炼器空串
    check(M.window_of("nobody/xx") == M.FALLBACK_WINDOW,
          "⭐ 未知模型窗口取兜底 —— "
          "📌 **保守取小**：猜大了=该压时以为还早=撞墙；猜小了只是压早一点")
    check(M.price_of("nobody/xx")["input_per_1m"] == 0.0,
          "⭐ 未知模型价格是 0 —— 📌 计费宁可少算，不许凭空捏一个价格")
    check(M.distiller_for("nobody/xx") == "",
          "⚠️ 未知厂商没有提炼器（含义是「退回主模型自己提炼」，**不是不提炼**）")


def t_price_has_exactly_one_authority() -> None:
    """🔴 搬表最容易留下的半截状态：两处都在算价。"""
    print("\n[2] ⭐⭐⭐ 价格只有一个权威")
    src = module_text("core.usage")
    tree = ast.parse(src)

    # 计费函数里不许再从 config 取 model_prices
    bad = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in ("record", "record_detailed")):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and n.value == "model_prices"):
                bad.append(f"{fn.name}@L{n.lineno}")
    check(not bad,
          "⭐⭐⭐ `record` / `record_detailed` 都不再读 `model_prices` —— "
          "🔴 留一处就是「用户改了价、账单没变」，而查不出原因", str(bad))

    # 默认配置里也不该再带价格（否则它会变成一个静默的第二权威）
    _dc = next((n for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "_DEFAULT_CONFIG"), None)
    check(_dc is not None, "⚠️ [L5] 前置：找得到 `_DEFAULT_CONFIG`")
    if _dc is not None:
        _keys = [k.value for k in _dc.value.keys if isinstance(k, ast.Constant)]
        check("model_prices" not in _keys,
              "⭐⭐ `_DEFAULT_CONFIG` 里已经没有 `model_prices`", str(_keys))
        check("hard_cap_usd" in _keys,
              "⚠️ 反向：预算设置还在（不是把整个配置删空了才绿的）")

    # 老文件里残留的 model_prices 必须**响亮地**被忽略
    _lc = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "load_config"), None)
    _seg = (ast.get_source_segment(src, _lc) or "") if _lc else ""
    check("warning" in _seg and "model_prices" in _seg,
          "⭐⭐ 老 `model_prices` 残留时**警告并忽略** —— "
          "📌 两个权威打架时，「安静地挑一个」是最坏的处置：用户会以为改生效了")

    # ⚠️ 真实值对拍：搬家不许把价格搬错
    from core.usage import _price_for
    from core.models import price_of
    # ⚠️ 值随官方标价走（2026-08-30 升级到 5 系时校正：
    #    haiku 表里原写 0.8 偏低；opus 原写 15.0 是官方价的 3 倍）。
    for mid, want_in in (("anthropic/claude-haiku-4.5", 1.0),
                         ("anthropic/claude-opus-5", 5.0)):
        check(_price_for(mid)["input_per_1m"] == want_in == price_of(mid)["input_per_1m"],
              f"⭐ {mid} 的价格搬家后逐位一致", f"{_price_for(mid)}")


def t_window_not_duplicated() -> None:
    """📌 一个能从别处推导出来的字段不该单独存。"""
    print("\n[3] ⭐⭐ 窗口没有被复制进 `CLAUDE_MODELS`")
    from core.provider import CLAUDE_MODELS
    dup = [m["id"] for m in CLAUDE_MODELS if "context_window" in m or "window" in m]
    check(not dup,
          "⭐⭐ `CLAUDE_MODELS` 里**没有**窗口字段 —— "
          "📌 原清单写的是「补 context_window」，这里**明知故犯地没照做**，"
          "依据是同一份清单里的另一条：**一个能从别处推导出来的字段不该单独存，"
          "存了就会和真值不一致**。窗口只住在 model_config.json，问 `window_of()`",
          str(dup))
    # ⚠️ 反向：证明 CLAUDE_MODELS 还在、还带着它该带的展示元信息
    # ⚠️ 原来查的是 `tooltip` —— 那个字段 2026-08-31 已删（它靠**下标**跟
    #    CLAUDE_MODELS 对应，多厂商之后必然指错；而且我们不代替厂商介绍模型强度）。
    #    这条断言要守的是「表还在、还带着展示元信息」，不是特指某个字段。
    check(bool(CLAUDE_MODELS) and all("name" in m and "id" in m for m in CLAUDE_MODELS),
          "⚠️ 前置：`CLAUDE_MODELS` 仍是 UI 展示元信息（上一条不是靠删空蒙的）")


def t_watermarks_and_snapshot() -> None:
    print("\n[4] ⭐ 水位与快照")
    from core.context import budget as B
    check(B.level_for(0.0) == "ok" and B.level_for(0.55) == "notice"
          and B.level_for(0.75) == "high" and B.level_for(0.90) == "critical",
          "⭐ 四档水位分界正确", "50/70/85")
    check(0 < B.WATERMARK_HIGH < B.WATERMARK_CRITICAL < 1.0,
          "⚠️ 高水位严格低于临界水位，且都在 0~1 之间（**比例不是绝对量**）")

    # 没量到时：known=False，且压力段是空串
    from core.context.meter import ContextMeter, MAIN_REACT
    import core.context.meter as _mm
    _saved, _lk = _mm._meter, _mm._read_last_known()
    try:
        # ⚠️ **必须连落盘的 `last_known` 一起清掉。**（2026-08-14 改）
        #    这条原本只清 meter，那时"没量到"就等于"什么都没有"。
        #    现在多了一条腿（跨重启的 `last_known`），于是同一个断言开始为
        #    **另一个原因**变绿/变红。📌 一条断言的前提被扩了，它就得跟着扩 ——
        #    否则它测的还是那句话，但那句话已经不是原来那件事了。
        _mm._write_last_known({})
        _mm._meter = ContextMeter()
        s = B.snapshot("anthropic/claude-haiku-4.5")
        check(s["known"] is False and s["ratio"] is None,
              "⭐⭐ **真的什么都没有时**（无锚 + 无落盘值）`known=False` —— "
              "📌 一个「我不知道」渲染成 0% 会被当成真数据")
        check(B.pressure_block("anthropic/claude-haiku-4.5") == "",
              "⚠️ 没量到时不注入任何提示")

        class _U:
            input_tokens = 150_000
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
        _mm._meter.observe(vendor="anthropic", model="anthropic/claude-haiku-4.5",
                           lane=MAIN_REACT, usage=_U(), local_estimate=1000)
        s = B.snapshot("anthropic/claude-haiku-4.5")
        check(s["known"] and abs(s["ratio"] - 0.75) < 1e-9 and s["level"] == "high",
              "⭐ 150K/200K → 75% → high", f"{s['ratio']}")

        # 🔴 切模型：锚属于**上一个**模型，不许拿来除以新窗口
        s2 = B.snapshot("anthropic/claude-sonnet-5")
        check(s2["known"] is False,
              "⭐⭐⭐ 换个模型问 → `known=False` —— "
              "🔴 拿 Haiku 的锚除以 Sonnet 的 1M 窗口会得到一个"
              "**看起来很正常的错误比例**（15%），而没有任何东西会报错。"
              "📌 与 [L21] 同源：一个共享的数字必须带上「我是谁」")
    finally:
        _mm._meter = _saved
        _mm._write_last_known(_lk)


def t_pressure_block_discipline() -> None:
    print("\n[5] ⭐⭐ 压力注入的三条纪律")
    from core.context import budget as B
    from core.context.meter import ContextMeter, MAIN_REACT
    import core.context.meter as _mm
    _saved, _lk = _mm._meter, _mm._read_last_known()
    try:
        _mm._write_last_known({})
        _mm._meter = ContextMeter()

        def _set(tok):
            _mm._meter._anchor = None
            class _U:
                input_tokens = tok
                cache_read_input_tokens = 0
                cache_creation_input_tokens = 0
            _mm._meter.observe(vendor="anthropic", model="anthropic/claude-haiku-4.5",
                               lane=MAIN_REACT, usage=_U(), local_estimate=1000)
            return B.pressure_block("anthropic/claude-haiku-4.5")

        check(_set(20_000) == "" and _set(120_000) == "",
              "⭐⭐ 低于**高水位**一个字都不说（10% / 60% 都静默）—— "
              "📌 一段每轮都在、又暂时没有意义的文字，会被模型学成背景噪音，"
              "等它真的变重要时反而没人读")
        _high = _set(150_000)
        _crit = _set(180_000)
        check("75%" in _high and "Context pressure" in _high, "⭐ 高水位注入，带百分比")
        check("critical" in _crit and len(_crit) > len(_high),
              "⭐ 临界水位措辞更硬、更具体")
        for blk in (_high, _crit):
            check("150000" not in blk and "180000" not in blk,
                  "⭐⭐ **不报具体 token 数** —— 那是估算值，报出来会被当成精确事实引用"
                  "（「你说我还有 47231 token」）。📌 不确定的数字要以不确定的形式说出口")
            break
    finally:
        _mm._meter = _saved
        _mm._write_last_known(_lk)

    # 注入点确实接上了
    src = module_text("core.orchestrator")
    check("pressure_block" in src,
          "⭐ orchestrator 里确实调了 `pressure_block` —— 📌 写了没人调，和没写一模一样")


def t_monitor_card() -> None:
    print("\n[6] ⭐ 监控卡：量不到显示 `--`，失准明说失准")
    src = module_text("app")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_refresh_context_card"), None)
    check(fn is not None, "存在 `_refresh_context_card`")
    if fn is None:
        return
    _body = [s for s in fn.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]      # ⚠️ 先剥 docstring
    seg = "\n".join((ast.get_source_segment(src, s) or "") for s in _body)
    check('"--"' in seg or "'--'" in seg,
          "⭐⭐ 量不到时显示 `--`，**不是 0%**")
    check("degraded" in seg,
          "⭐⭐ 残差看门狗报 DEGRADED 时**明说失准**，而不是继续给一个数 —— "
          "📌 一个不可信的数字，比没有数字更有害")
    # 必须真的被调到
    check("_refresh_context_card()" in src,
          "⭐ 它确实被调用（写了没人调 = 没写）")
    check("ctx_lbl" in src, "⚠️ 前置：卡片本身存在")


def t_relay_field_probe() -> None:
    """🔴 中转透传探针 —— **少一个字段**比全都没有更危险。

    用户可能经任意第三方中转访问。官方接口是三项相加，中转若吞掉
    `cache_creation_input_tokens`，第一版代码仍会返回一个**偏低的部分和** ——
    而它长得和一个正常数字一模一样，没有任何东西会报错。

    ⭐ 探针的正确形态**不是「跑一次看看透没透传」**（那只证明了那一次），
       而是**让「字段消失」这件事永远响亮**。
    📌 **一次性的验证证明不了一个持续的属性。**
    """
    print("\n[7] ⭐⭐⭐ 中转透传探针：少一个字段 → None + 报警，不返回部分和")
    from core.context.meter import normalize_prompt_input as n
    import core.context.meter as _m

    class _Full:
        input_tokens = 9
        cache_read_input_tokens = 5185
        cache_creation_input_tokens = 2415

    class _Partial:      # 中转吞掉了 cache_creation
        input_tokens = 9
        cache_read_input_tokens = 5185

    class _ZeroCache:    # 缓存没命中：字段在、值是 0 —— **正常**
        input_tokens = 8303
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    check(n(_Full(), "anthropic") == 7609,
          "⚠️ 前置：全字段时正常相加（9+5185+2415）")
    check(n(_ZeroCache(), "anthropic") == 8303,
          "⭐⭐ 缓存未命中（字段在、值为 0）**照常算** —— "
          "📌 `0` 与 `缺失` 必须分开：判据是 `is None`，不是 `not v`")

    _m._warned_missing.clear()
    check(n(_Partial(), "anthropic") is None,
          "⭐⭐⭐ 少一个字段 → **返回 None**，🔴 绝不返回 5194 这个"
          "「看起来完全正常」的部分和")
    check(len(_m._warned_missing) == 1, "⭐ 报了一次警")
    n(_Partial(), "anthropic")
    check(len(_m._warned_missing) == 1,
          "⭐ warn-once：同一组合不重复刷 —— "
          "📌 每次都刷的告警会变成噪音，恰好毁掉「响亮」这件事本身")
    _m._warned_missing.clear()


def t_distribution_sampler() -> None:
    """per-model 配额标定的原料 —— **今天定不了配额，但今天可以让数据开始积累。**"""
    print("\n[8] ⭐ 分布采样：后面定阈值时才有真实数据可依")
    import json
    from core.context.meter import ContextMeter, MAIN_REACT, samples_path
    import core.context.meter as _mm

    p = samples_path()
    before = p.stat().st_size if p.exists() else 0
    m = ContextMeter()

    class _U:
        input_tokens = 12345
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
    m.observe(vendor="anthropic", model="anthropic/claude-haiku-4.5",
              lane=MAIN_REACT, usage=_U(), local_estimate=999)
    check(p.exists() and p.stat().st_size > before, "⭐ 观测写出了一条采样", str(p.name))
    last = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
    check(last.get("a") == 12345 and last.get("m") == "anthropic/claude-haiku-4.5",
          "⭐ 采样里有模型和真实厚度（标定要按模型分开看）", str(last))

    # ⚠️ 写盘必须在锁外：Subagent会并发调 provider
    import ast, pathlib
    src = module_text("core.context.meter")
    fn = next((x for x in ast.walk(ast.parse(src))
               if isinstance(x, ast.FunctionDef) and x.name == "observe"), None)
    check(fn is not None, "前置条件：找得到 observe")
    if fn is not None:
        # `_sample(...)` 不该在 `with self._lock:` 的子树里
        in_lock = any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_sample"
            for w in ast.walk(fn) if isinstance(w, ast.With)
            for c in ast.walk(w))
        check(not in_lock,
              "⭐⭐ 采样写盘在**锁外** —— 📌 写盘不该拿着计量的锁（[A4] Subagent会并发进来）")


def t_continuity_across_restart() -> None:
    """⭐⭐⭐ 「Nano 是连续的」—— 重启不该让上下文卡变回 `--`。

    2026-08-14，两条理由，**第二条是决定性的**：
      ① Nano 的定位是数字生命，**它是连续的**；每次打开显示 `--`
         给人一种"每次打开是新 nano"的感觉。
      ② **唯一的手动清理入口是「重置对话」按钮** —— 所以打开→关闭→再打开，
         这个数值**没有理由变**。

    ⚠️⚠️ 但**不是**让锚跨重启（那会破坏：重启后 memory 会 hydrate + 截断，
       上一个 runtime 的数字不该当精确锚）。而是落盘一个**只给 UI 看**的
       `last_known`，并明确标成估算。
    📌 **同一个数字服务于两个目的时，就该有两条命。**
    """
    print("\n[9] ⭐⭐⭐ 连续性：重启后不显示 `--`，而是「上次量到的值」")
    import core.context.meter as M
    from core.context.meter import ContextMeter, MAIN_REACT, last_known, forget_conversation_size
    from core.context import budget as B
    MODEL = "anthropic/claude-haiku-4.5"
    # 🔴🔴 **把落盘路径指到临时文件** —— 那条纪律的同族。
    #
    # 原来它 `_read_last_known()` 读的是**生产的** `data/context_last_known.json`，
    # 末尾再 `_write_last_known(_lk)` 还原。看起来很礼貌，但**起始状态是生产文件** ——
    # 2026-08-15 实测现场：一整轮反复重启 Nano，真实运行把 `floor` 写成了 5981
    # （新加了两个 CORE 工具，底噪确实变了），这一项当场红，
    # **而它红的原因和它要证的那件事毫无关系**。
    # 📌 **一条测试只要读了生产状态，它就会在某天因为「程序被正常使用过」而变红**
    #    —— 而那种红最难查：代码没错、断言没错、只是世界动了。
    import tempfile as _tf
    _tmpdir = pathlib.Path(_tf.mkdtemp(prefix="nanolk_"))
    _orig_path_fn = M._last_known_path
    M._last_known_path = lambda: _tmpdir / "context_last_known.json"

    _saved, _lk = M._meter, M._read_last_known()
    try:
        M._meter = ContextMeter()
        M._meter._note_floor(MODEL, 6000)

        class _U:
            input_tokens = 40000
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
        M._meter.observe(vendor="anthropic", model=MODEL, lane=MAIN_REACT,
                         usage=_U(), local_estimate=1)
        s = B.snapshot(MODEL)
        check(s["known"] and s["used"] == 40000 and s["estimated"] is False,
              "⚠️ 前置：量到之后是真值（estimated=False）")
        check(last_known(MODEL)["actual"] == 40000, "⭐ 真值落盘了")

        # ── 模拟重启：新 meter（锚没了），落盘还在 ──
        M._meter = ContextMeter()
        s = B.snapshot(MODEL)
        check(s["known"] and s["used"] == 40000 and s["estimated"] is True,
              "⭐⭐⭐ 重启后**照样有数**（20%），只是标成估算 —— "
              "🔴 改造前这里是 `--`，而判据是"
              "「打开→关闭→再打开，这个数值没有理由变」", str(s["ratio"]))
        check(M._meter.snapshot()["has_anchor"] is False,
              "⭐⭐ 而**锚确实没有跨重启** —— [F6] 那条没被破坏："
              "预测用的锚绑 runtime，显示用的值落盘，两条命")

        check(B.pressure_block(MODEL) == "",
              "⭐⭐ **估算值不注入给模型** —— "
              "📌 让用户看一个估算值、和让模型据此改变行为，是两个门槛；"
              "第一次真实计量就在下一个请求，等一轮不亏")

        # ── 模拟「重置对话」：忘掉历史，底噪留着 ──
        forget_conversation_size()
        M._meter = ContextMeter()
        s = B.snapshot(MODEL)
        check(s["known"] and s["used"] == 6000,
              "⭐⭐⭐ 重置对话后显示**底噪**（system+工具表），"
              "🔴 **不是 0** —— 📌 显示 0 是一句谎话：system prompt 和工具 schema "
              "一直在那儿", str(s["ratio"]))
        check(last_known(MODEL)["actual"] is None and last_known(MODEL)["floor"] == 6000,
              "⭐ 重置只清 `actual`，`floor` 留着")
    finally:
        M._meter = _saved
        M._write_last_known(_lk)
        M._last_known_path = _orig_path_fn      # ⚠️ 路径也要还原

    # 重置路径确实接上了
    app_src = module_text("app")
    check("forget_conversation_size" in app_src,
          "⭐ 「重置对话」按钮里确实调了它 —— 📌 写了没人调，和没写一模一样")
    check("_refresh_context_card()" in app_src.split("self.ctx_lbl = ui.label")[1][:400],
          "⭐ 卡片建完立刻填一次（否则启动后会一直是 `--` 直到用户先说话）")


def main() -> int:
    t_two_layer_table()
    t_price_has_exactly_one_authority()
    t_window_not_duplicated()
    t_watermarks_and_snapshot()
    t_pressure_block_discipline()
    t_monitor_card()
    t_relay_field_probe()
    t_distribution_sampler()
    t_continuity_across_restart()
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
