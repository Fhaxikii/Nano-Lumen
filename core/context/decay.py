"""衰减执行 —— **L0→L1**（纯改写）与 **L1→L2**（过提炼器）。

═══ L0→L1 是四个箭头里最"便宜"的一个 ═══

    不过 LLM · 不动 UI · 不落盘正文 · 换掉的东西在硬盘上还能重跑

⭐ 所以它是**验证整套机制（触发点 / 批 / 高低水位 / 不抖）的最佳载体**：
   机制验错了也不丢任何不可再生的东西。
📌 **把不可测的部分排在最后，先用可测的那一档把骨架钉死。**

═══ ⚠️ L1 是「替换」，不是「第二次截断」 ═══

项目里已经有一个**无条件**的安全阀：单条 `tool_result` 超字符上限就地截断
（`_compress_tool_results_inplace`，⚠️ 回代码核实：它跑在 `_append` **之前**）。
两者**正交**：

    安全阀  = admission —— 一条结果当场大到能把整个 request 撑爆
    L1      = aging     —— 这条结果已经是历史，不值得每轮继续背着正文

→ 不管它当初是 3K 还是「原始 50K → 安全阀截成 12K」，到了 L1 都**整个换成占位符**。
   不是 `12K → 4K → 1K` 那样一路截。**这样两者永远不打架。**

🔴 由此推出一条必须改口的旧说法：
**`conversation_messages` 存的不是「工具返回的每一个原始字节」，
而是过完安全阀之后的规范化历史。**

═══ 🔴 三条会静默出错的红线 ═══

① **不许破坏 `tool_calls ↔ tool_results` 配对**（ID 与顺序必须完整对应）——
   只换 `content`，保住 `tool_use_id`。**删掉其中一半 = 省 token 最后变成 provider 400。**
② **`is_error` 必须保留** —— 它是「尝试过」和「做过」的唯一分界。
   丢了它，一次失败的工具调用会在历史里读起来像成功。
③ **只降 closed exchange** —— 当前这一轮永远 L0。
   否则可能在 `tool_result` 还没回来时就把它换成占位符。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from core.context.decay_store import L0, L1, L2, L3, L4

# 降到该层配额的多少才停。⚠️ **不是"降到刚好不越线"**：
# 📌 一个「越线就处理」的机制，如果处理到刚好不越线，它必然在边界上抖动
#    —— 下一条消息又越线、又降一次，UI 的分界线就会刷屏。
# ⭐ 本项目同一形状的第五次（`USER_HOLD_SEC` 的 TTL 续期 / `_COALESCE_SEC` /
#    接管状态条的 `_TAKEOVER_SETTLE_SEC` / 触发-执行分离）。
LOW_WATER = 0.70

# 一轮最多降多少次交换。⚠️ 不是性能考虑，是**防失控**：
# 📌 一个"降到达标为止"的循环，一旦达标条件因为别的原因永远不满足，
#    就会把整段历史一次降完。宁可这一轮没降够，下一轮接着降。
MAX_PER_RUN = 40

# 🔴 **昂贵那一级（L1→L2）单独的上限** —— 每一次都是一个 LLM 调用。
#
# 为什么不能跟着 40 走：`MAX_PER_RUN` 防的是"一次把整段
# 历史降完"，那对 L0/L3/L4 只是 CPU；但 L1→L2 是 **40 次串行 API 调用**，
# 全发生在一条用户消息的收尾里。
#
# ⚠️ 这个数**不是为「第一次切换」拍的临时值**（原本想那么干，但被
#    当场否掉，对的）：首次 backlog 清一次记录就没了，零代码。真正需要这个
#    上限的是**清不掉的那种 backlog** —— 预算打满整天不提炼，次日一开口
#    就排着几十个交换。那个场景会一直存在，所以这个数也必须是长期的。
# 📌 **一个只为「第一次」存在的数，会在第一次过去之后永远留下来。**
#
# ⚠️ 追不上时**必须响亮**（见 `run_l1_to_l2` 结尾）——
#    📌 一个安静地追不上的队列，会以「怎么上下文老是超」的形式活很久。
MAX_DISTILL_PER_RUN = 6

# 提炼时给多少条低分辨率上文。⚠️ 只能是**比它早**的（见 `_prior_for`）。
MAX_PRIOR_LINES = 8

_PLACEHOLDER = (
    "[Tool output aged out of context to save room. "
    "The call itself and whether it succeeded are still above; only the body was dropped. "
    "It was about {n:,} characters. If you need the actual content, run the tool again — "
    "do not tell the user the result is lost.]"
)


def estimate_tokens(messages) -> int:
    """一段消息的粗略 token 量。

    ⚠️ **复用 `meter` 的估算器，不另造一个** ——
       📌 两个估算器迟早会在某个量级上给出不同答案，
          而那时"到底哪个说了算"是个不该存在的问题。
    """
    try:
        from core.context.meter import estimate_text
    except Exception:
        return 0
    n = 0
    for m in messages:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            n += estimate_text(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    n += estimate_text(b["text"])
        for tr in (getattr(m, "tool_results", None) or []):
            if isinstance(getattr(tr, "content", None), str):
                n += estimate_text(tr.content)
        for tc in (getattr(m, "tool_calls", None) or []):
            n += estimate_text(str(getattr(tc, "args", "") or ""))
    return n


def _demotable_results(ex) -> list:
    """这次交换里**还没被换过**的工具结果。"""
    out = []
    for m in ex.messages:
        for tr in (getattr(m, "tool_results", None) or []):
            c = getattr(tr, "content", "")
            if isinstance(c, str) and c and not c.startswith("[Tool output aged out"):
                out.append(tr)
    return out


def apply_l1(ex) -> int:
    """把这次交换的历史工具结果换成占位符。**只改内存投影。**

    返回换掉的条数。
    ⚠️ 只动 `content` —— `tool_use_id` / `is_error` **原样保留**（见模块头红线 ①②）。
    ⚠️ `raw_result` / `tool_data` **不用动**：回代码核实，`ChatMessage.to_dict()`
       只把 `tool_use_id` / `content` / `is_error` 发给 provider，那两个字段
       根本不占上下文。📌 **L1 的职责是省上下文，不是省内存** ——
       顺手清掉不该由它负责的东西，只会让"到底是谁清的"变难查。
    """
    n = 0
    for tr in _demotable_results(ex):
        _len = len(tr.content)
        tr.content = _PLACEHOLDER.format(n=_len)
        n += 1
    return n


def _digest_message(digest: dict):
    """L2 形态里代替「Nano 那半」的那一条消息。"""
    from core.context import digest as D
    from core.schema import ChatMessage
    return ChatMessage(role="assistant", content=D.render_line(digest))


def project_exchange(ex, entry: dict | None, stale: bool = False) -> list:
    """一次交换按它的档位【应该】投影成什么。**live 与 hydrate 共用这一个。**

    🔴🔴 这个函数补的是一个大洞：
       原来 `decay.record(level=L2)` 之后**没有任何地方**把 `e.messages`
       换成「用户原话 + 结论行」，`run_l2_to_l3` 也**从不碰 `memory.storage`**。
       于是：

           exchange_decay   说：这段已经是 L2 / L3
           storage（发给模型的东西）那边仍然背着 L1 那整段原文

       表在"宣布世界变了"，模型的世界没变。压缩收益是**假的** ——
       而且不报错、不掉内容，只是**日志说省了、其实没省**。
       ⚠️ 更怪的是 L3：重启后 `rebuild_projection` 才真的移出，
          于是语义变成「刚降 L3 时 Nano 还记得，重启后才真忘」。

    📌 **三本账的承诺是「投影随时可从账本+衰减表重建」——
       那就必须真的有一个函数，回答「重建成什么」。**
       分散在四个 `run_*` 里各写一遍，等于四个函数各自理解那句承诺。

    ⚠️ **幂等**：已经投影过的形态再跑一遍结果相同（L2 会重新取 user + 重建
       结论行；L1 的占位符替换本身幂等）。
    """
    if entry is None or stale:
        # ⚠️ 陈旧的派生物一律当没有（原文变过了）—— 与 `level_of` 同一条纪律。
        return list(ex.messages)
    lv = str(entry.get("level") or L0)
    if lv in (L3, L4):
        # 🔴 内容**不进上下文** —— 已交接给语义记忆，只留一行索引，
        #    那行由 `_l3_index_block` 无条件注入，不在这里。
        return []
    if lv == L2:
        import json as _j
        try:
            d = _j.loads(entry.get("digest_json") or "{}")
        except Exception:
            d = None
        _u = ex.user_message
        if d and _u is not None:
            # ⭐ 「用户原话逐字保留，只压另一半」—— 这是定下来的做法。
            # ⚠️ 交换里那些 `visible_to_user=False` 的系统注记**跟着被压掉**：
            #    它们是 Nano 自己的脚手架，属于"另一半"。
            return [_u, _digest_message(d)]
        # ⚠️ digest 取不到 → **退回 L1 形态**，不许返回空。
        #    📌 fail-safe 方向永远是"多留一点"，不是"当它已经压好了"。
    apply_l1(ex)
    return list(ex.messages)


def l2_footprint(ex, entry: dict) -> int:
    """L2 形态**实际占用**多少 token。

    🔴 原来这里只算结论行。但 L2 的定义就是
       「用户原话 + 结论行」，于是一个完全合法的交换：

           用户粘贴 20K token 的需求
           Nano 回 500 token

       降到 L2 后账上写「≈150 token」，模型实际背着 20,150。
       **系统性地往低估方向错** —— 而 Nano 的真人内容占比恰恰远高于编码类工具。

    ⭐ 修法不是"再加一项"，是**让计量走投影那一个出口**：
       量的东西和放进 storage 的东西是同一个函数产的，就不可能对不上。
       📌 计量和投影分两处写，它们只在"当初的理解没错"的前提下相等。
    """
    return estimate_tokens(project_exchange(ex, entry))


def run_l0_to_l1(memory, decay, session_id: str, model_id: str) -> dict:
    """把 L0 层降到配额以内。**永不抛** —— 治理层不许把对话搞挂。

    ⭐ 触发是「点」，执行是「批」：
        触发 = L0 层现在超没超它自己的配额（每轮问一次，level-triggered）
        执行 = 一次降一整批，降到 **配额 × LOW_WATER**
    """
    stat = {"ran": False, "demoted": 0, "results": 0,
            "before": 0, "after": 0, "quota": 0}
    try:
        from core.context.exchange import split
        from core.models import quota_of, window_of

        _q = float((quota_of(model_id) or {}).get("L0") or 0.0)
        _w = int(window_of(model_id) or 0)
        if _q <= 0 or _w <= 0:
            return stat
        quota = int(_w * _q)
        stat["quota"] = quota

        exchanges = split(memory.storage)
        if len(exchanges) < 2:
            return stat            # 只有当前这一轮 → 没有 closed exchange

        # ⚠️ 最后一个是 **active**，永远不碰（见模块头红线 ③）。
        closed = exchanges[:-1]
        levels = {}
        l0_list = []
        for e in closed:
            if not e.has_identity:
                continue           # orphan / 还没落盘 —— 没有稳定身份不许记账
            lv = decay.level_of(session_id, e.start_ordinal)
            levels[e.start_ordinal] = lv
            if lv == L0:
                l0_list.append(e)

        thick = sum(estimate_tokens(e.messages) for e in l0_list)
        stat["before"] = thick
        if thick <= quota:
            return stat            # 没越线，什么都不做

        stat["ran"] = True
        target = int(quota * LOW_WATER)
        logger.info(f"[Decay] L0 层 {thick} > 配额 {quota} → 降到 {target}"
                    f"（{len(l0_list)} 次可降交换）")

        # 从最老的开始降。⚠️ `l0_list` 已按 split 顺序 = 时间顺序。
        for e in l0_list:
            if thick <= target or stat["demoted"] >= MAX_PER_RUN:
                break
            _was = estimate_tokens(e.messages)
            # ⚠️ 即使这次交换没有工具结果，也要把它标成 L1 —— 它的 L0 形态和
            #    L1 形态本来就一样，标了才能让阶梯往前走。
            #    📌 否则它会被**永远重新考虑**，而每一轮都不会变小。
            #
            # 🔴🔴 **先落盘，成功了才改投影**。
            #    原来是 `apply_l1 → record` 且**不判返回值** —— 四档里只有这一档
            #    漏了（L2/L3/L4 都写了 `if not decay.record`）。SQLite 恰好失败一次：
            #        storage = 已经 L1（变小了）   exchange_decay = 仍然 L0
            #    下一轮算 L0 厚度时它已经低于配额 → **再也不会重试 record**
            #    → 这个 runtime 里它永远「投影 L1 / 权威 L0」，L1→L2 永远不选它，
            #      重启又恢复成原文。**只有一条 warning，行为继续跑。**
            # 📌 **先改内存再落盘 = 把「失败」变成一种谁也不会再看的状态。**
            #    L1 是纯确定性改写，先落盘完全没有代价。
            if not decay.record(session_id, e.start_ordinal, e.end_ordinal, level=L1):
                stat["failed"] = stat.get("failed", 0) + 1
                continue
            _n = apply_l1(e)
            _now = estimate_tokens(e.messages)
            thick -= max(0, _was - _now)
            stat["demoted"] += 1
            stat["results"] += _n
        stat["after"] = thick
        logger.info(f"[Decay] L0→L1 完成：降了 {stat['demoted']} 次交换 / "
                    f"{stat['results']} 条工具结果，L0 层 {stat['before']} → {stat['after']}")
    except Exception as e:
        logger.warning(f"[Decay] L0→L1 跳过（不影响对话）: {e}")
    return stat


# ══════════════════════════════════════════════════════════════════════════
# L1 → L2 —— **第一次真的调模型删东西**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴🔴 契约写死为 **`derive → validate → persist → activate`**，
#    **每个 Exchange 各自成败**，任何一步失败就**继续留在 L1**。
#
#    ⚠️ 反例：一批 20 个里 17 个成功、3 个 schema 校验失败，
#       **不许**整批标 L2 —— 那 3 个的原文会被当成"已经压好了"而丢掉，
#       换来的却是三条不存在的结论。
#    📌 **一个批次只有一个成功位，等于把最差的那个成员的失败藏进平均值里。**
#
# ⏸ **刻意先不做「多个交换合并成一次调用」**（早先设计里那条成本优化：
#    平均交换 < 2–3K token 时该合并）。理由两条：
#    ① 一次一个时，「每个各自成败」**天然成立**，不需要额外机制去保证；
#    ② 那个 2–3K 阈值本身也是拍的，而计量层的采样正在积累标定它的数据。
#    📌 **先让正确性成立，再优化成本** —— 反过来的话，
#       你会在一个还不知道对不对的机制上做性能调优。

# 提炼调用的输出上限。⚠️ 2026-08-14 之前这个数**根本没传到 provider**
# （`chat_without_tools` 的 `**_` 吃掉了它，底下写死 4096）—— 也就是说
# 它从来没被真正试过。接通之后必须重新算，不能沿用那个随手写的 700：
#
#   schema 的硬上限（`digest.py`）= outcome 400 字 + referents 6×80 + open_items 4×80
#                                 = 1200 字 + JSON 结构约 100 字
#   中文在 Claude 分词下接近 1 token/字 → **最坏约 1400 token**
#
# 📌 700 会让一条**完全合法**的结论行被截断 → JSON 不完整 → `_parse_digest`
#    返回 None → 那次交换白花一次调用继续留在 L1，而日志只说"提炼失败"。
# ⚠️ 2000 同时**刻意压在 thinking 的门槛之下**（`_thinking_arg` 要求
#    `max_tokens - 1024 >= 1024`）：固定 schema 的抽取不需要 extended thinking，
#    开了只是把每次提炼的成本翻倍。
_DISTILL_MAX_TOKENS = 2000


async def _distill_one(provider, ex, prior_lines: list[str], model_id: str):
    """把一次交换提炼成一条结论行。返回 `(digest | None, distiller_model)`。

    ⚠️ **提炼模型必须与主模型同厂**（用户只有那一家的 key）。
       配不到 → **退回主模型自己提炼**，⚠️ **不是「不提炼」**：
       📌 **宁可贵，不许失能** —— 不提炼 = 上下文治理失效 = 迟早撞窗口。
    """
    from core.context import digest as D
    from core.models import distiller_for

    _cfg = distiller_for(model_id)
    _mdl = _cfg or model_id
    # ⚠️ **`_mdl == model_id` 有两种成因，日志不许混成一句**（2026-08-14 当场发现）：
    #      配了提炼器，而它恰好就是主模型   → 正常，不该提示
    #      压根没配（未知厂商）→ 退回主模型 → **这才是那条 fallback**
    #    📌 一句同时描述两种成因的日志，等于把「一切正常」和「走了兜底」
    #       写成一样的 —— 而排查时你正是靠这句话区分它俩。
    if not _cfg:
        logger.info(f"[Decay] 没有为 {model_id} 配提炼器 → 退回主模型自己提炼"
                    f"（宁可贵，不许失能）")
    # ⭐ `kind` 由代码判定（有没有工具调用），**不问模型**；
    #    工具数/报错数也是算出来的事实。见 `digest.kind_of` / `digest.tool_facts`。
    _kind = D.kind_of(ex)
    _n, _err = D.tool_facts(ex)
    _sys, _user = D.build_prompt(D.exchange_text(ex), prior_lines,
                                 kind=_kind, n_tools=_n, n_err=_err)
    try:
        # 🔴🔴 消息形状必须是 `{"role":..., "content":...}`。
        #    这里原来写的是 `{"role":"user","parts":[{"text": ...}]}`（Gemini 那套形状）,
        #    而 `ClaudeProvider._merge_context` 读的是 `msg["content"]` ——
        #    于是**整段提炼输入直接消失**，SDK 收到一个没有 content 的消息。
        #    2026-08-14 实际运行现场：`[TOKEN-USAGE notools-call] fresh=40 output=42`，
        #    三次提炼全败在 `outcome 为空 / status 不在枚举里` —— 模型在对着**空提示词**编。
        # ⚠️ 它为什么没被测出来：`t_f5_decay_l2` 用的 `_FakeProvider` 把参数照单全收，
        #    **测的是"我调了 provider"，不是"provider 认得我给的东西"**。
        #    📌 与 `bridge.get_store` 同一个形状：**跨模块边界那一步，没有一个测试真的走过。**
        content, _used = await provider.chat_without_tools(
            [{"role": "user", "content": _user}], _sys,
            model_override=_mdl, max_tokens=_DISTILL_MAX_TOKENS)
    except Exception as e:
        # ⚠️ 包括预算硬闸抛的 `BudgetExceeded` —— 那时**不该**继续降级。
        logger.warning(f"[Decay] 提炼调用失败（该交换继续留在 L1）: {e}")
        return None, _mdl
    return _parse_digest(content, kind=_kind), _mdl


def _parse_digest(content: str, *, kind: str = ""):
    """从模型输出里抠出那个 JSON 并校验。**校验不过一律 None。**

    ⚠️ 允许 ```json 围栏 —— 模型经常那么写。
       📌 但**不做任何"修补"**（补引号、猜字段）：
          一个被我们猜着修好的 digest，错在哪永远查不出来。
    """
    import json as _j
    from core.context import digest as D
    _t = (content or "").strip()
    if _t.startswith("```"):
        _t = _t.split("```", 2)[1] if "```" in _t[3:] else _t.strip("`")
        _t = _t[4:].strip() if _t.lower().startswith("json") else _t.strip()
    _i, _k = _t.find("{"), _t.rfind("}")
    if _i < 0 or _k <= _i:
        logger.warning("[Decay] 提炼输出里找不到 JSON → 该交换继续留在 L1")
        return None
    try:
        raw = _j.loads(_t[_i:_k + 1])
    except Exception as e:
        logger.warning(f"[Decay] 提炼输出不是合法 JSON（继续留在 L1）: {e}")
        return None
    d, errs = D.validate(raw, kind=kind)
    if d is None:
        logger.warning(f"[Decay] 结论行校验不过（继续留在 L1）: {errs}")
    return d


async def run_l1_to_l2(memory, decay, provider, session_id: str, model_id: str) -> dict:
    """把 L1 层降到配额以内。**永不抛。**

    ⚠️ 与 L0→L1 的区别：这一档**有固定成本**（每次一个 LLM 调用），
       所以它天然是「批」——但**批的是调用次数，不是成败判定**（见上面那条）。
    """
    stat = {"ran": False, "tried": 0, "ok": 0, "failed": 0,
            "before": 0, "after": 0, "quota": 0}
    try:
        from core.context import digest as D
        from core.context.exchange import split
        from core.models import quota_of, window_of

        _q = float((quota_of(model_id) or {}).get("L1") or 0.0)
        _w = int(window_of(model_id) or 0)
        if _q <= 0 or _w <= 0:
            return stat
        quota = int(_w * _q)
        stat["quota"] = quota

        exchanges = split(memory.storage)
        if len(exchanges) < 2:
            return stat
        closed = exchanges[:-1]          # ⚠️ active 永远不碰

        _rows = decay.active_entries(session_id)
        l1_list, _lines = [], []
        for e in closed:
            if not e.has_identity:
                continue
            _entry = _rows.get(e.start_ordinal)
            _lv = str(_entry.get("level")) if _entry else L0
            if _lv == L1:
                l1_list.append(e)
            elif _lv == L2 and _entry.get("digest_json"):
                # ⭐ 已经压过的结论行 = 提炼下一条时的**低分辨率上文**
                try:
                    import json as _j
                    _lines.append((e.start_ordinal,
                                   D.render_line(_j.loads(_entry["digest_json"]))))
                except Exception:
                    pass
            elif _lv in (L3, L4) and _entry.get("index_entry"):
                # ⭐ 更老的那些已经只剩索引了。**也给** —— 否则长距离指代会断：
                #    提炼器看不到"三十轮前那个方案"，只好编一个名字。
                #    ⚠️ 仍然是低分辨率，不回原文。
                _lines.append((e.start_ordinal, str(_entry["index_entry"])))

        def _prior_for(_ord: int) -> list[str]:
            """给某条交换的低分辨率上文 —— 🔴 **只有比它早的**。

            原来所有 L1 共用同一个 `prior`（整段历史里的全部 L2）。
            于是重试一条早期失败的交换时，上文里塞的是**它后面**发生的结论，
            而提示词还写着 "Earlier in this same conversation…"。
            📌 **一个叫「上文」的变量，必须真的只装上文** ——
               否则提炼器会拿未来的结论去解释过去那句话，而且它读起来很合理。
            """
            return [t for o, t in _lines if o < _ord][-MAX_PRIOR_LINES:]

        thick = sum(estimate_tokens(e.messages) for e in l1_list)
        stat["before"] = thick
        if thick <= quota:
            return stat

        stat["ran"] = True
        target = int(quota * LOW_WATER)
        logger.info(f"[Decay] L1 层 {thick} > 配额 {quota} → 降到 {target}"
                    f"（{len(l1_list)} 次可降交换）")

        for e in l1_list:
            # ⚠️ 昂贵那一级用**自己的**上限（见 `MAX_DISTILL_PER_RUN`）。
            if thick <= target or stat["tried"] >= MAX_DISTILL_PER_RUN:
                break
            stat["tried"] += 1
            # ── derive ──
            d, _mdl = await _distill_one(provider, e, _prior_for(e.start_ordinal),
                                         model_id)
            if d is None:
                # ── 任何一步失败 → 保持原级别 ──
                stat["failed"] += 1
                continue
            # ── validate 已在 _parse_digest 里做完 ── persist + activate ──
            if not decay.record(session_id, e.start_ordinal, e.end_ordinal,
                                level=L2, digest=d, distiller_model=_mdl):
                stat["failed"] += 1
                continue
            stat["ok"] += 1
            _lines.append((e.start_ordinal, D.render_line(d)))
            _lines.sort(key=lambda t: t[0])
            thick -= estimate_tokens(e.messages)
        stat["after"] = thick
        logger.info(f"[Decay] L1→L2 完成：{stat['ok']} 成 / {stat['failed']} 败"
                    f"（失败的继续留在 L1），L1 层 {stat['before']} → {stat['after']}")
        # ⚠️ **追不上要响亮** —— 见 `MAX_DISTILL_PER_RUN` 的注释。
        if stat["after"] > target and stat["tried"] >= MAX_DISTILL_PER_RUN:
            logger.warning(
                f"[Decay] ⚠️ L1 层这一轮**没追上**（{stat['after']} > 目标 {target}）："
                f"本轮已用满 {MAX_DISTILL_PER_RUN} 次提炼配额，还剩 "
                f"{max(0, len(l1_list) - stat['tried'])} 个交换排队。下一轮接着降。"
                f"⚠️ 如果这条连续出现很多轮，说明积压清不掉（常见成因："
                f"预算打满导致提炼长时间停摆）。")
    except Exception as e:
        logger.warning(f"[Decay] L1→L2 跳过（不影响对话）: {e}")
    return stat


# ══════════════════════════════════════════════════════════════════════════
# L2 → L3 —— **唯一用户有感的那个箭头**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴🔴 **提交顺序写死，任何一步失败就保持 L2**：
#
#     产生可召回内容 → 持久化 → 验证确实检索得到 → 生成索引条目 → 最后才 commit L3
#
#    绝不能「内容先移出 → 再写语义记忆 → bridge 失败 → 内容没了、召回也没有」。
#    📌 **fail-closed 的方向由「失败时谁受损」决定**：
#       这里失败的代价是"多背一会儿上下文"（便宜、可逆），
#       反方向失败的代价是"内容永久消失且没人知道"（不可逆）。
#
# ⚠️⚠️ **本函数只改数据，不动 UI。** L2→L3 是唯一用户有感的箭头，而
#    「模型侧已经 L3、UI 侧还挂着 L2」正是要避免的 UI 说谎。
#    📌 所以 UI 与模型必须**由同一个 level 权威驱动、同一帧生效** ——
#       那是第 6 步（记忆起点卡）的事，本步只把权威准备好。


def l3_index_lines(decay, session_id: str) -> list[str]:
    """当前会话全部 L3 的索引条目（老 → 新）。

    🔴 **不许再有第二个上限**。原来这里 `limit=60` +
       `out[-60:]`，而 L3 真正的淘汰判据是 **L3 token 配额**（`run_l3_to_l4`）。
       两套规则同时存在时，第 61 条以前的记录会：

           数据库：还是 L3，没到过期条件
           system：因为 limit=60，**不再注入**

       —— 它在行为上已经是 L4，权威却还说它是 L3，**而且没有任何记录**。
       📌 **同一件事有两个淘汰规则时，输的那个会无声地赢。**
       要少注入，就让它真的 transition 到 L4（那条路径有日志、有权威）。

    ⚠️⚠️ 它要**无条件注入 system 动态段**，不是"需要时再检索" ——
       🔴 这一条解掉的是早先那个悖论：
          「大模型真的忘记某个东西之后，它不就把『我记过这个东西、
            我应该去看笔记』这件事也忘了吗？」
       ⭐ 参照 `MEMORY.md`：**索引不是被回忆起来的，是被塞进来的。**
    """
    out = []
    try:
        # ⚠️ 走**唯一的**衰减读出口（陈旧的自动不在里面）—— 见 `active_entries`。
        for _ord, e in sorted(decay.active_entries(session_id).items()):
            if str(e.get("level")) == L3 and e.get("index_entry"):
                out.append(str(e["index_entry"]))
    except Exception as ex:
        logger.debug(f"[Decay] 读 L3 索引失败: {ex}")
    return out


def run_l2_to_l3(memory, decay, session_id: str, model_id: str) -> dict:
    """把 L2 层降到配额以内。**永不抛。**

    ⚠️ 不过 LLM —— L2 的结论行已经在库里，这一步只是**交接 + 生成索引 + 改档**。
    """
    stat = {"ran": False, "tried": 0, "ok": 0, "failed": 0,
            "before": 0, "after": 0, "quota": 0}
    try:
        import json as _j
        from core.context import bridge as _B
        from core.context.exchange import split
        from core.models import quota_of, window_of

        _q = float((quota_of(model_id) or {}).get("L2") or 0.0)
        _w = int(window_of(model_id) or 0)
        if _q <= 0 or _w <= 0:
            return stat
        quota = int(_w * _q)
        stat["quota"] = quota

        exchanges = split(memory.storage)
        if len(exchanges) < 2:
            return stat
        by_ord = {e.start_ordinal: e for e in exchanges[:-1] if e.has_identity}

        l2 = []
        for _ord, ent in sorted(decay.active_entries(session_id).items()):
            if str(ent.get("level")) != L2:
                continue
            if _ord in by_ord:
                l2.append((_ord, ent, by_ord[_ord]))

        # ⭐ L2 的"厚度" = **用户原话 + 结论行**（见 `l2_footprint`）——
        #    走的是投影那一个出口，所以量的一定就是模型实际背着的东西。
        thick = sum(l2_footprint(_ex, ent) for _ord, ent, _ex in l2)
        stat["before"] = thick
        if thick <= quota:
            return stat

        stat["ran"] = True
        target = int(quota * LOW_WATER)
        logger.info(f"[Decay] L2 层 {thick} > 配额 {quota} → 降到 {target}"
                    f"（{len(l2)} 条可降）")

        for _ord, ent, ex in l2:
            if thick <= target or stat["tried"] >= MAX_PER_RUN:
                break
            stat["tried"] += 1
            try:
                d = _j.loads(ent["digest_json"] or "{}")
            except Exception:
                stat["failed"] += 1
                continue
            _u = getattr(ex.user_message, "content", "") if ex.user_message else ""
            if not isinstance(_u, str):
                _u = ""

            # ── ①②③ 交接 + 持久化 + 验证真能拿回来（都在 bridge 里，失败返回 None）──
            mid = _B.hand_off(d, _u, source_ref=f"{session_id}:{_ord}")
            if not mid:
                stat["failed"] += 1      # ⚠️ 保持 L2，绝不 commit
                continue
            # ── ④ 生成索引条目 ──
            # ⚠️ 从这里开始，语义记忆**已经写下去了** —— 任何一步失败都要
            #    `rollback`，否则每失败一轮就多一条没人引用的孤儿（内容还几乎相同）。
            #    📌 fail-closed 只保证"没提交的不算数"，不会替你收拾"已经写下去的"。
            _line = _B.index_line(d)
            if not _line:
                _B.rollback(mid)
                stat["failed"] += 1
                continue
            # ── ⑤ 最后才改档 ──
            if not decay.record(session_id, _ord, ent["end_ordinal"], level=L3,
                                digest=d, index_entry=_line,
                                distiller_model=str(ent.get("distiller_model") or "")):
                _B.rollback(mid)
                stat["failed"] += 1
                continue
            stat["ok"] += 1
            thick -= l2_footprint(ex, ent)
        stat["after"] = thick
        logger.info(f"[Decay] L2→L3 完成：{stat['ok']} 成 / {stat['failed']} 败"
                    f"（失败的继续留在 L2），L2 层 {stat['before']} → {stat['after']}")
    except Exception as e:
        logger.warning(f"[Decay] L2→L3 跳过（不影响对话）: {e}")
    return stat


def _line_tokens(digest: dict) -> int:
    """一条结论行在上下文里占多少。⚠️ 复用 meter 的估算器（别造第二套）。"""
    try:
        from core.context import digest as D
        from core.context.meter import estimate_text
        return estimate_text(D.render_line(digest))
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════
# L3 → L4 —— **「真遗忘」，但不是「删除」**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴🔴 **这一档最容易做错的地方：把 L4 实现成「把那条语义记忆删掉」。**
#
#     L3   索引条目进 system  →  Nano **自动知道**这儿曾经有东西
#     L4   索引条目掉出 system →  Nano **不再自动想起**它
#
#    ⚠️ 但 `semantic_memories` 里那条内容**原样留着**，向量检索照样找得到，
#       导出数据里也照样有。
#    📌 **遗忘 = 不再自动想起，不等于抹掉。**
#       删掉的话，用户问起"我们是不是讨论过 X"时就真的什么都没有了 ——
#       而那不是遗忘，那是销毁。
#
# ⚠️ 过期判据是 **L3 配额**，不是时间窗（早先的设计写的是"按时间窗"，已更正）。
#    📌 **一个会积累的东西，它的清除条件应该来自它消耗的资源，不是来自钟表** ——
#       按时间过期意味着"聊得多"和"聊得少"用同一把尺，而占地方的是前者。
#
# ⚠️ 也**不删 `exchange_decay` 那一行**：它是"这次交换发生过、被交接过"的记录。
#    📌 把字段清空会让「它后来怎么了」变成一个无法回答的问题。


def run_l3_to_l4(memory, decay, session_id: str, model_id: str) -> dict:
    """把 L3 索引层降到配额以内。**永不抛。** 不过 LLM。"""
    stat = {"ran": False, "expired": 0, "before": 0, "after": 0, "quota": 0}
    try:
        from core.context.meter import estimate_text
        from core.models import quota_of, window_of

        _q = float((quota_of(model_id) or {}).get("L3") or 0.0)
        _w = int(window_of(model_id) or 0)
        if _q <= 0 or _w <= 0:
            return stat
        quota = int(_w * _q)
        stat["quota"] = quota

        rows = [(o, e) for o, e in sorted(decay.active_entries(session_id).items())
                if str(e.get("level")) == L3 and e.get("index_entry")]
        thick = sum(estimate_text(str(e["index_entry"])) for _, e in rows)  # noqa
        stat["before"] = thick
        if thick <= quota:
            return stat

        stat["ran"] = True
        target = int(quota * LOW_WATER)
        logger.info(f"[Decay] L3 索引层 {thick} > 配额 {quota} -> 降到 {target}"
                    f"（{len(rows)} 条索引）")

        for _ord, e in rows:                      # 最老的先过期
            if thick <= target or stat["expired"] >= MAX_PER_RUN:
                break
            # ⚠️ 只改档位 —— index_entry 留着（见上方注释），
            #    `l3_index_lines` 按 level 过滤，L4 自然就不再注入。
            import json as _j
            try:
                _d = _j.loads(e.get("digest_json") or "{}")
            except Exception:
                _d = None
            if not decay.record(session_id, _ord, e["end_ordinal"], level=L4,
                                digest=_d, index_entry=str(e["index_entry"]),
                                distiller_model=str(e.get("distiller_model") or "")):
                continue
            thick -= estimate_text(str(e["index_entry"]))
            stat["expired"] += 1
        stat["after"] = thick
        logger.info(f"[Decay] L3->L4 完成：{stat['expired']} 条索引过期"
                    f"（语义记忆原样留着，只是不再自动注入），"
                    f"L3 层 {stat['before']} -> {stat['after']}")
    except Exception as e:
        logger.warning(f"[Decay] L3->L4 跳过（不影响对话）: {e}")
    return stat


# ══════════════════════════════════════════════════════════════════════════
# 🔴🔴 重启后：**把落盘的档位重放到内存投影上**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 不做这一步的后果（2026-08-14 复查时发现的洞）：
#    `_hydrate_current_session` 只从账本重建**原文**，完全不看 `exchange_decay`。
#    于是每次重启：
#
#        L1 的交换  →  工具原文全回来了（省下的又吐回去）
#        L2 的交换  →  原文回来了，digest 白生成（钱白花）
#        L3 的交换  →  **内容回到上下文**，而 decay 表说它已经是 L3
#                      → `run_l2_to_l3` 认为它降过了，**永远不会再移除**
#                      → ⚠️ 而 UI 按 level 过滤把它藏起来了
#                      → **UI 说「已移出记忆」，模型手里还拿着** —— 反向的 UI 说谎
#
# 📌 **建了「衰减权威」这张表，却没有把它重放回投影，等于只做了一半** ——
#    表里记的东西不生效，它就只是一份**关于过去的说明文**，不是权威。
# ⭐ 这正是第 1 步那句话的兑现：**`storage` 随时可从「账本 + 衰减表」重建。**


def emergency_reclaim(memory, decay, session_id: str, model_id: str,
                      need: int) -> int:
    """**马上腾出 `need` 个 token**，返回实际腾出多少。**永不抛。**

    与 `run_l0_to_l1` 的区别，是这里**不看配额** —— 它回答的不是
    「该不该遗忘」，而是「这一次到底发不发得出去」。
    📌 **一个启发式的机制，底下必须垫一个确定性的兜底** ——
       而兜底不能再用启发式的判据（配额）去决定自己出不出手。

    🔴🔴 **只降 closed exchange，当前这一轮一个字都不碰。**
       用户刚打的那句话、以及本轮已经拿到的工具结果，是**不可再生**的输入；
       为了发得出去而删掉它，等于用「我做不到」换成「我假装你没说过」。
       ⚠️ 这条不是礼貌，是正确性：删了它，模型收到的就是一个**残缺的问题**，
          而它会**照样回答** —— 那比拒发糟糕得多。
       📌 **装不下的时候，正确的失败方式是拒绝，不是偷偷少装一点。**

    ⚠️ 只做 **L0→L1**（把工具结果换成占位符）：纯改写、不过 LLM、不花钱、
       毫秒级。📌 紧急路径上不许有网络调用 —— 那是在一条已经失败的路上
       再加一个可能失败的步骤。
    """
    got = 0
    try:
        from core.context.exchange import split
        exchanges = split(memory.storage)
        if len(exchanges) < 2:
            return 0
        _rows = decay.active_entries(session_id)
        # 从**最老的**开始降 —— 越老的越不该占着原文。
        for e in exchanges[:-1]:            # ⚠️ 最后一个是 active，永不碰
            if got >= need:
                break
            if not e.has_identity:
                continue
            _ent = _rows.get(e.start_ordinal)
            if _ent and str(_ent.get("level")) != L0:
                continue                    # 已经降过了，这里榨不出东西
            _was = estimate_tokens(e.messages)
            if not decay.record(session_id, e.start_ordinal, e.end_ordinal, level=L1):
                continue                    # 落盘失败 → 不改投影（同 run_l0_to_l1）
            apply_l1(e)
            got += max(0, _was - estimate_tokens(e.messages))
        if got:
            logger.warning(f"[Guard] 紧急回收：腾出 {got:,} token"
                           f"（目标 {need:,}）—— 只降了已关闭的交换，"
                           f"当前这一轮没动")
    except Exception as e:
        logger.warning(f"[Guard] 紧急回收失败（这一次将拒发）: {e}")
    return got


def rebuild_projection(memory, decay, session_id: str) -> dict:
    """按落盘的档位，把内存投影重建成"它本该是的样子"。**永不抛。**

    ⭐⭐ 这是三本账那句承诺的**唯一兑现点**：

        conversation_messages（事实） + exchange_decay（档位）
            ↓  project_exchange 逐段
        MemoryManager.storage（投影）

    调用点**两个，不多不少**：
      ① `MemoryManager._hydrate_current_session` 之后（重启 / 会话切换）
      ② 每轮四档跑完之后（live）—— 🔴 前面说的那个大洞就是它缺席造成的：
         `run_l1_to_l2` / `run_l2_to_l3` 只改权威、不改投影，于是
         「表说 L2/L3，模型还背着原文」，要等重启才真的生效。

    📌 **live 和重启走同一段代码，是「两边不会漂」的唯一可靠做法** ——
       各写一遍的话，它们只在"我两次都想对了"的前提下相等。
    ⚠️ 幂等：同一状态连跑 N 次结果相同（见 `project_exchange`）。
    """
    stat = {"l1": 0, "l2": 0, "evicted": 0}
    try:
        from core.context.exchange import split
        rows = decay.load_session(session_id) or {}
        if not rows:
            return stat
        keep = []
        for ex in split(memory.storage):
            if not ex.has_identity:
                keep.extend(ex.messages)
                continue
            ent = rows.get(ex.start_ordinal)
            _stale = bool(ent and decay.is_stale(ent))
            _lv = (str(ent.get("level")) if ent and not _stale else L0)
            out = project_exchange(ex, ent, _stale)
            if not out:
                stat["evicted"] += 1
            elif _lv == L2 and len(out) == 2:
                stat["l2"] += 1
            elif _lv in (L1, L2):
                stat["l1"] += 1
            keep.extend(out)
        memory.storage = keep
        if any(stat.values()):
            logger.info(f"[Decay] 重放档位：L1 改写 {stat['l1']} 段、"
                        f"L2 压成结论行 {stat['l2']} 段、移出 {stat['evicted']} 段")
    except Exception as e:
        logger.warning(f"[Decay] 重放档位失败（退回原文，只是上下文厚一点）: {e}")
    return stat
