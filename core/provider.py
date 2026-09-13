# core/provider.py  —  Claude (Anthropic) backend
from typing import List, Any, Optional, Dict
import os
import re
import asyncio
import json
import time
import uuid
import threading
from loguru import logger
from dotenv import load_dotenv
import anthropic
from core.schema import AgentDecision, ToolCall
from core.usage import usage_tracker

load_dotenv()


def _input_tokens_total(usage) -> int:
    """真实输入 token = 未命中缓存 input + 命中缓存读取 + 缓存写入。
    Anthropic prompt caching 下，usage.input_tokens 只含【未命中缓存】那部分，
    命中的稳定前缀落在 cache_read_input_tokens、首次写入落在 cache_creation_input_tokens。
    只取 input_tokens 会系统性少算（表现为单条 token 显示畸小，如"45 tok"），
    也让 token 优化时的记账失真。这里把三者相加，得到真实 prompt 规模。"""
    return (
        (getattr(usage, "input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    )


# ══════════════════════════════════════════════════════════════════════════
# [CACHE-DIAG] 缓存前缀失效归因 —— **常驻，不是临时诊断**
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 为什么需要它：Anthropic 的缓存是**前缀匹配**，顺序固定
#       tools ──→ system ──→ messages
#    三个断点分别挂在「最后一个 tool」「system 的 stable 段」「最后一条消息」上。
#    📌 **前缀里任何一处变了，它后面的全部缓存作废** ——
#       而 `[TOKEN-USAGE]` 只告诉你「这次 read=0」，**不告诉你是谁打断的**。
#
# 🔴 立这行日志的由头（2026-08-23 对账单）：24 小时
#    `Cache Creation 1M : Cache Read 675K` —— **写进去 1.48 个 token 才读到 1 个**。
#    缓存要回本得被读两次以上，现在连一次都没读满。
#    📌 而 `Cache Hit Rate 99.7%` 是个**误导性指标**：它算的是
#       读取/(读取+未缓存输入)，而未缓存输入只有 12 token/请求 —— 怎么算都是 99%。
#       **真正该看的是 Create/Read 的比值。**
#
# ⚠️ 它记的是**归因**不是用量：用量在 `[TOKEN-USAGE]` 那一行，两行不合并 ——
#    📌 一行日志同时回答两个问题，就会在只想看其中一个时被当成噪音关掉。
_CACHE_SIG_PREV: dict = {}


def _cache_sig(system_guide, tools, messages, stable_n: int = -1) -> dict:
    """算出这一次请求的**前缀签名**：三段各自的指纹与体量。

    ⚠️ 用 hash 不用原文：只要回答「变没变」，不需要回答「变成什么」——
       📌 一个诊断记录点，存的东西越少越不容易变成新的泄露面。
    """
    import json as _j
    _blob = lambda o: (_j.dumps(o, ensure_ascii=False, sort_keys=True)
                       if o is not None else "")
    _sg = system_guide or ""
    # system 用哨兵切成 stable / dynamic 两段（与 `_cached_system` 同一条界线）
    if CACHE_BREAK_MARKER in _sg:
        _stable, _dyn = _sg.split(CACHE_BREAK_MARKER, 1)
    else:
        _stable, _dyn = _sg, ""
    _tb = _blob(tools)
    # ⭐⭐⭐ [2026-08-23] **稳定块单独记一个指纹。**
    #
    # 🔴 立这一段的由头：连着两个假设被数据否掉（先怪 `system.dynamic`，
    #    再怪 2048 门槛），第三次才发现**有一个从没验证过的前提** ——
    #    一直假设「`load_tools` 追加第 11 个 ⇒ 前 10 个不变」，
    #    可上面那个 `tools_h` 算的是**整个数组**的 hash，
    #    它只能说「tools 变了」，**根本没验证前 N 个有没有变**。
    # 📌 **一个诊断如果只报「变了」，它就没法区分「哪一段变了」** ——
    #    而那正好是这里唯一要回答的问题。
    #
    # ⭐ 加上它之后，read=0 的那一发能被分成两种完全不同的结论：
    #      稳定块指纹**没变** + 仍 read=0  → 是 API/网关行为，我们改不了
    #      稳定块指纹**变了**              → 是我们自己的 bug，而且能修
    # ⚠️ 这一条不花 API 钱：它只是把已经在手的数据多算一次。
    _stable_tools = (tools or [])[:stable_n] if (stable_n and stable_n > 0) else []
    _stb = _blob(_stable_tools)
    # ⚠️⚠️ messages **不能用整体 hash 去比「相等」** —— 缓存是**前缀匹配**，
    #    ReAct 每一步都往后追加，旧历史仍然是新历史的**前缀**，缓存照旧命中。
    #    🔴 第一版就是拿整体 hash 比相等，于是每追加一条都报「断开」——
    #       那种日志三天之内就会被人当噪音关掉。
    #    📌 **用「相等」去判一个该用「是不是前缀」的问题，会把正常增长报成故障** ——
    #       而一个总在误报的诊断，比没有诊断更坏：它会训练人忽略它。
    # ⭐ 改成**逐条指纹的列表**，比对时问的是「上一次那串还是不是这一次的前缀」。
    #    ⚠️ 仍然去掉最后一条：那条本来就每次都新（新用户消息 / 新 tool_result）。
    _mh = tuple(hash(_blob(m)) for m in (messages[:-1] if messages else []))
    return {
        "tools_h": hash(_tb), "tools_n": len(tools or []), "tools_c": len(_tb),
        "stbl_h": hash(_stb), "stbl_n": len(_stable_tools), "stbl_c": len(_stb),
        "stable_h": hash(_stable), "stable_c": len(_stable),
        "dyn_h": hash(_dyn), "dyn_c": len(_dyn),
        "msgs_hs": _mh, "msgs_n": len(_mh),
    }


def _cache_diag(sig: dict, tag: str = "") -> None:
    """与上一次请求比一比，把**第一个变掉的前缀段**报出来。

    ⭐ 只报**第一个** —— 前缀是链式的：前面那段变了，后面全作废，
       后面再报也只是噪音。📌 归因要指向根，不是列出全部症状。
    """
    global _CACHE_SIG_PREV
    prev = _CACHE_SIG_PREV
    _CACHE_SIG_PREV = sig
    if not prev:
        logger.warning(f"[CACHE-DIAG{(' ' + tag) if tag else ''}] 首次请求（无可比对象）"
                       f" | tools={sig['tools_n']}个/{sig['tools_c']}字符"
                       f" stable={sig['stable_c']}字符 dyn={sig['dyn_c']}字符"
                       f" msgs={sig['msgs_n']}条")
        return
    # ⚠️ 顺序**必须**是 tools → stable → dyn → msgs：那就是缓存前缀的生成顺序。
    #    📌 判据的顺序本身就是结论的一部分 —— 换个顺序就会归错因。
    # ⚠️ **顺序：稳定块排在 tools 之前** —— 缓存断点就打在稳定块末尾，
    #    它是这条链上最靠前、也是唯一我们能控制的那一段。
    #    📌 归因要指向根，而根总是在最前面那一段。
    for _key, _label, _extra in (
            ("stbl_h", "tools·稳定块(缓存断点在此)",
             f"{prev.get('stbl_n')}个/{prev.get('stbl_c')}字符"
             f"→{sig.get('stbl_n')}个/{sig.get('stbl_c')}字符"),
            ("tools_h", "tools·尾部(断点之后，不该伤缓存)",
             f"{prev['tools_n']}→{sig['tools_n']}个"),
            ("stable_h", "system.stable", f"{prev['stable_c']}→{sig['stable_c']}字符"),
            ("dyn_h", "system.dynamic", f"{prev['dyn_c']}→{sig['dyn_c']}字符"),
    ):
        if prev.get(_key) != sig.get(_key):
            logger.warning(
                f"[CACHE-DIAG{(' ' + tag) if tag else ''}] 🔴 前缀在 **{_label}** 处断开"
                f"（{_extra}）→ 它**后面的缓存全部作废**"
                f" | 稳定块={sig.get('stbl_n')}个/{sig.get('stbl_c')}字符"
                f" tools共={sig['tools_n']}个 sys.stable={sig['stable_c']}"
                f" dyn={sig['dyn_c']} msgs={sig['msgs_n']}条")
            return
    # ⭐ messages 单独判：**上一次那串还是不是这一次的前缀**（不是相等）。
    #    只有历史被**改写**（压缩 / 提炼 / 移出上下文）才算真断开。
    _pm, _sm = prev.get("msgs_hs") or (), sig.get("msgs_hs") or ()
    if _sm[:len(_pm)] != _pm:
        # 找出第一条对不上的，那才是被改写的位置
        _at = next((i for i in range(min(len(_pm), len(_sm))) if _pm[i] != _sm[i]),
                   min(len(_pm), len(_sm)))
        logger.warning(
            f"[CACHE-DIAG{(' ' + tag) if tag else ''}] 🔴 前缀在 **messages 第 {_at} 条**"
            f"处断开（历史被改写：{prev['msgs_n']}→{sig['msgs_n']}条）"
            f"→ 它**后面的缓存全部作废** | tools={sig['tools_n']}个 "
            f"stable={sig['stable_c']} dyn={sig['dyn_c']}")
        return
    _grew = sig["msgs_n"] - prev["msgs_n"]
    logger.warning(
        f"[CACHE-DIAG{(' ' + tag) if tag else ''}] ✅ 前缀完好"
        f"（历史{'追加 +%d 条' % _grew if _grew else '未变'}，仍是有效前缀）"
        f" | 稳定块={sig.get('stbl_n')}个/{sig.get('stbl_c')}字符"
        f" tools共={sig['tools_n']}个 sys.stable={sig['stable_c']} "
        f"dyn={sig['dyn_c']} msgs={sig['msgs_n']}条")


def _record_usage(usage, model: str, tag: str = "") -> None:
    """记账 + 打一行真实用量分解日志（做 token 压缩时的量尺）。
    fresh=未命中缓存全价 | cache_read=命中缓存(0.1x) | cache_creation=写缓存(1.25x) | output。
    优化的核心指标是 fresh：压 fresh 才是压钱。"""
    fresh = getattr(usage, "input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    # 成本按缓存权重算准；session/UI 单条显示只累计 fresh+output（本轮真花的量）。
    usage_tracker.record_detailed(fresh, cr, cc, out, model)
    logger.warning(
        f"[TOKEN-USAGE{(' ' + tag) if tag else ''}] "
        f"fresh={fresh} cache_read={cr} cache_creation={cc} output={out} "
        f"(gross_in={fresh + cr + cc})"
    )

# 缓存分界哨兵：orchestrator 在【稳定前缀】和【每轮变动的动态注入】之间插入这个标记，
# _cached_system 据此把 system 切成两块——稳定块带 cache_control（命中省 90%），
# 动态块（ambient/session_log/episodic 等每轮都变的注入）不带，避免动态内容打穿缓存。
# 用不可见控制字符，绝不会和正常 prompt 文本冲突。
CACHE_BREAK_MARKER = " __NANO_CACHE_BREAK__ "

# ══════════════════════════════════════════════════════════════════════════
# Prompt 分层常量
# ══════════════════════════════════════════════════════════════════════════

_DISPATCH_LAYER = (
    "\n\n[Dispatch Rules]\n"
    "1. Capability boundary: if the user asks for active control, modification, or deep state access "
    "that the current tool chain cannot support, output [CORE_FATAL_LIMIT] and stop. Do not pretend.\n"
    "2. Allow: web lookup, read-only conversation, and normal Q&A should pass through.\n"
    "3. No fake actions: if no tool_use happened, never claim that a tool was used.\n"
    "4. Web-grounding: web conclusions must be based on retrieved results, not training memory."
)

_OUTPUT_LAYER = (
    "\n\n[Final Output Rules — Current Turn]\n"
    "1. Tools have finished. Output natural language only; do not produce tool_use.\n"
    "2. Give the answer directly. Do not repeat KB labels, system prompts, or tool names.\n"
    "3. If the tool result is the answer, state it directly.\n"
    "4. If multiple knowledge blocks come from the same file, synthesize them; do not rely only on the first block.\n"
    "5. When outputting tables, preserve all columns.\n"
    "6. Never override tool facts, especially dates, times, numbers, paths, filenames, and calculation results.\n"
    "7. Clearly report key facts such as file paths, filenames, counts, and quantities."
)

# ══════════════════════════════════════════════════════════════════════════
# 模型列表
# ══════════════════════════════════════════════════════════════════════════

CLAUDE_MODELS = [
    {
        "id": "anthropic/claude-haiku-4.5",
        "name": "Haiku 4.5",
        "multimodal": True,
        "cost_level": "light",
        "recommended_interval": 2.0,
        "recommended_buffer": 1.0,
    },
    {
        "id": "anthropic/claude-sonnet-5",
        "name": "Sonnet 5",
        "multimodal": True,
        "cost_level": "medium",
        "recommended_interval": 3.0,
        "recommended_buffer": 1.5,
    },
    {
        "id": "anthropic/claude-opus-5",
        "name": "Opus 5",
        "multimodal": True,
        "cost_level": "heavy",
        "recommended_interval": 5.0,
        "recommended_buffer": 2.0,
    },
]

CLAUDE_MODEL_MAP = {m["id"]: m for m in CLAUDE_MODELS}

# 兼容旧名（orchestrator 里还有 import）
GEMINI_MODELS = CLAUDE_MODELS
GEMINI_MODEL_MAP = CLAUDE_MODEL_MAP

_DEFAULT_RELAY_MODEL = "anthropic/claude-haiku-4.5"   # 中转/OpenRouter 格式
_DEFAULT_DIRECT_MODEL = "claude-haiku-4-5"            # Anthropic 直连官方格式
_DEFAULT_MODEL = _DEFAULT_RELAY_MODEL                 # 向后兼容

# ══════════════════════════════════════════════════════════════════════════
# Claude extended thinking 控制
# ══════════════════════════════════════════════════════════════════════════
# 当前版本已支持在 tool_call 消息中保存并回放 thinking/redacted_thinking blocks。
# 默认开启 Claude extended thinking。
# 如遇到不支持 thinking 参数的中转服务，可设置环境变量：NANO_ENABLE_CLAUDE_THINKING=0
ENABLE_CLAUDE_EXTENDED_THINKING = (
    (os.getenv("NANO_ENABLE_CLAUDE_THINKING") or "1").strip() != "0"
)
THINKING_BUDGET = int((os.getenv("NANO_THINKING_BUDGET") or "2048").strip())


# 官方在这些模型上**移除**了 `budget_tokens`（传了按契约应当 400），改用自适应思考。
# ⚠️ 判据是【官方文档】，不是某家中转的实际行为。
#    一律按官方契约实现，不为任何一家中转的行为做适配 ——
#    用户用的可能是任意一家，我们不可能逐一适配；
#    若某家中转因此不支持某个功能，那是该中转与官方契约的差距。
_ADAPTIVE_THINKING_MARKERS = ("opus-5", "sonnet-5", "fable-5", "mythos-5",
                              "opus-4.7", "opus-4-7", "opus-4.8", "opus-4-8")


def _uses_adaptive_thinking(model: str) -> bool:
    """这个模型是否走 `{"type": "adaptive"}`。

    ⚠️ 兜底方向是 **adaptive**（命中列表外的未知模型按新的算）——
       📌 老模型在消失、新模型都是自适应，兜底要朝"未来更可能对"的那一侧倒。
       同 `FALLBACK_WINDOW` 那条判据的同款应用。
    """
    m = (model or "").lower()
    if any(k in m for k in _ADAPTIVE_THINKING_MARKERS):
        return True
    # 明确仍需 budget_tokens 的老模型
    return not any(k in m for k in ("haiku-4.5", "haiku-4-5", "sonnet-4.5", "sonnet-4-5",
                                    "sonnet-4.6", "sonnet-4-6", "opus-4.5", "opus-4-5",
                                    "opus-4.6", "opus-4-6"))


def _thinking_arg(max_tokens: int, model: str = ""):
    """统一生成 Anthropic thinking 参数。**必须按模型分支** —— 三代模型三套规则。

    ```
    Opus 5 / Sonnet 5 / …   {"type": "adaptive"}       budget_tokens 已移除
    Haiku 4.5 等            {"type": "enabled", ...}   budget_tokens 仍是必需的
    ```
    🔴 关闭思考时也要分支：**Opus 5 不传 thinking 就是开着的**（Opus 4.8 是不传就不开）。
       ⇒ 对自适应模型必须显式 `{"type": "disabled"}`，返回 NOT_GIVEN 等于没关 ——
       而 `NANO_ENABLE_CLAUDE_THINKING=0` 存在的理由正是「遇到不支持 thinking 的
       中转可以关掉」，静默失效等于把这个开关废了。
    """
    adaptive = _uses_adaptive_thinking(model)
    if not ENABLE_CLAUDE_EXTENDED_THINKING:
        return {"type": "disabled"} if adaptive else anthropic.NOT_GIVEN
    if adaptive:
        return {"type": "adaptive"}
    safe_budget = min(THINKING_BUDGET, max_tokens - 1024)
    if safe_budget < 1024:
        return anthropic.NOT_GIVEN
    return {"type": "enabled", "budget_tokens": safe_budget}



# 旧模型 id → 新 id。用户配置里存着的可能是已下线的型号；
# ⚠️ 读取时【就地映射】而不是保留旧条目 —— 保留等于让下拉框越来越长，
#    而这个映射表是可以随下一次升级整条删掉的。
_MODEL_MIGRATIONS = {
    "anthropic/claude-sonnet-4.6": "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4.8": "anthropic/claude-opus-5",
    "claude-sonnet-4-6": "claude-sonnet-5",
    "claude-opus-4-8": "claude-opus-5",
}


def migrate_model_id(model_id: str) -> str:
    """把已下线的模型 id 映射到当前 id。认不出来的原样返回，**不抛**。"""
    new = _MODEL_MIGRATIONS.get((model_id or "").strip())
    if new:
        logger.info(f"[Model] 配置里的 {model_id} 已下线，映射到 {new}")
        return new
    return model_id


_MODELS_CACHE_FILE = "models_cache.json"


def fetch_endpoint_models(base_url: str, api_key: str, timeout: float = 8.0) -> list[str]:
    """问端点自己有哪些模型（`GET {base}/v1/models`）。失败返回空列表，**绝不抛**。

    ⭐ 这是**官方规范里定义的** Models API（Anthropic / DeepSeek 官方文档都有），
       不是某家中转的私有接口。
    🔴 为什么必须问而不是猜：同一个模型在不同端点的 id 不一样 ——
       `claude-haiku-4-5`（官方）vs `anthropic/claude-haiku-4.5`（某中转），
       互相都会 404。⇒ id 是**端点相关**的，猜不出来。
    ⚠️ 只返回 id 清单；**能力**（谁能看图、谁能当判定器）不在这个接口里，
       那部分照旧靠厂商表。📌 两件事不要混。
    """
    import json as _json
    import urllib.request
    import urllib.error
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    # ⚠️ base_url 指向的是【消息端点】；Models API 未必在同一路径下。
    #    实测：深度求索的消息端点是 `…/anthropic`，模型清单却在 `…/v1/models`（根上）。
    #    ⇒ 同路径试不到就退到"去掉协议后缀的根"再试一次。**只多一次请求。**
    _roots = [base]
    for _suffix in ("/anthropic", "/v1", "/openai"):
        if base.endswith(_suffix):
            _root = base[: -len(_suffix)].rstrip("/")
            if _root and _root not in _roots:
                _roots.append(_root)
    for base, path in [(b, p) for b in _roots for p in ("/v1/models", "/models")]:
        try:
            req = urllib.request.Request(
                base + path,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = _json.loads(r.read())
            rows = d.get("data") or d.get("models") or []
            ids = [str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id")]
            if ids:
                logger.info(f"[Models] {base}{path} → {len(ids)} 个模型")
                return ids
        except Exception as e:
            logger.debug(f"[Models] {base}{path} 失败: {type(e).__name__}")
    return []


_ENDPOINT_MODELS_MEM: dict = {}


def endpoint_models(base_url: str, api_key: str, vendor: str = "") -> list[str]:
    """端点支持的模型（带本地缓存 + 回落厂商表）。

    ```
    ① 缓存里有这个端点的清单 → 用缓存（避免每次启动都发一次网络请求）
    ② 拉一次；成功 → 落盘缓存
    ③ 拉不到 → 回落厂商表里的内置清单
    ```
    ⚠️ ③ 这条回落很重要：**断网时 Nano 仍然要能启动、仍然要能选模型**。
       📌 一个"更准的数据源"不该有能力让应用失能 —— 同 `load()` 那条
       「读不到就返回空表，绝不因为配置缺失而崩」。
    """
    import json as _json
    import pathlib as _pl
    # 🔴 官方模式下 base_url 是空的 —— 不能让下游默认到 Anthropic：
    #    选了深度求索也会去问 api.anthropic.com，401 之后回落内置 Claude 清单，
    #    主模型被刷成 Haiku、角色池跟着变 Anthropic（2026-08-31 实测连撞三次）。
    #    📌 **让知道厂商的这一层解析地址**，别把默认值留在只认字符串的下游。
    if not base_url and vendor:
        try:
            from core.models import vendor_meta as _vm
            base_url = _vm(vendor).get("official_base") or ""
        except Exception:
            pass
    key_id = (base_url or "official") + "|" + (vendor or "")
    # ⚠️ 进程内缓存**含失败**：只缓存成功的话，探不到的端点会让每次 UI 构建
    #    都重发一轮请求（一次构建调两次 ⇒ 4 个 HTTP）。
    #    📌 "探不到"这件事本身不该变成持续的网络压力。
    if key_id in _ENDPOINT_MODELS_MEM:
        return list(_ENDPOINT_MODELS_MEM[key_id])
    cache_p = _pl.Path(__file__).parent.parent / "data" / _MODELS_CACHE_FILE
    try:
        if cache_p.exists():
            cached = _json.loads(cache_p.read_text(encoding="utf-8"))
            hit = cached.get(key_id)
            if hit:
                return list(hit)
    except Exception:
        pass
    ids = fetch_endpoint_models(base_url, api_key)
    if not ids:
        # 回落厂商表 —— 并把结果（含这次探测失败）记进进程内缓存
        try:
            from core.models import load as _load
            ids = list(((_load().get(vendor) or {}).get("models") or {}).keys())
        except Exception:
            ids = []
        _ENDPOINT_MODELS_MEM[key_id] = list(ids)
        return list(ids)
    _ENDPOINT_MODELS_MEM[key_id] = list(ids)
    if ids:
        try:
            cached = {}
            if cache_p.exists():
                cached = _json.loads(cache_p.read_text(encoding="utf-8"))
            cached[key_id] = ids
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            cache_p.write_text(_json.dumps(cached, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        except Exception as e:
            logger.debug(f"[Models] 缓存写入失败（不影响使用）: {e}")
        return ids
    return list(ids)


def default_model_for(vendor: str, relay: bool = False) -> str:
    """该厂商的**默认主模型**。

    ⚠️ 老代码用两个常量硬编码（`_DEFAULT_RELAY_MODEL` / `_DEFAULT_DIRECT_MODEL`），
       那等于假设"用户用的是 Claude"。多厂商之后这个假设不成立。
    ⇒ 从厂商表里取 `models` 的**第一条**（表里顺序 = 价格升序，手排）。
    📌 与角色池同一条判据：**顺序即优先级，不在代码里算**。

    ⚠️ 环境变量 `NANO_RELAY_DEFAULT_MODEL` 仍然优先（给"我就要用某个型号"的人留口子），
       但它**不再是默认值的来源** —— 默认值来自厂商表。
    """
    forced = (os.getenv("NANO_RELAY_DEFAULT_MODEL") or "").strip()
    if forced:
        return forced
    try:
        from core.models import load as _load
        models = ((_load().get(vendor) or {}).get("models") or {})
        if models:
            return next(iter(models))
    except Exception:
        pass
    return _DEFAULT_RELAY_MODEL if relay else _DEFAULT_DIRECT_MODEL


def get_relay_default_model() -> str:
    m = (os.getenv("NANO_RELAY_DEFAULT_MODEL") or "").strip()
    if m and m in CLAUDE_MODEL_MAP:
        return m
    return _DEFAULT_RELAY_MODEL


# ══════════════════════════════════════════════════════════════════════════
# ClaudeProvider
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# 成本硬上限的收口层
# ══════════════════════════════════════════════════════════════════════════
# 闸【不】放在 ClaudeProvider 的几个对外方法上，而是包住底层客户端。
#
# 原因是实测出来的：真正发出请求的地方有 8 处，其中 3 处在 rag.py 里
# 直接伸手拿 provider._client.messages.create（多模态图片描述 ×2、
# 扫描件逐页 OCR ×1），完全绕过 provider 的所有对外方法。
#
# 其中最危险的是 rag.py 里逐页 OCR 那个 `for page_idx in range(actual_pages)`
# 循环：load_full_file 给的 max_ocr_pages=100，也就是【一次全文加载最多
# 打 100 次 API】。只闸对外方法的话，全项目最贵的那条路恰好还在闸外。
#
# 包住客户端之后，现在和以后的所有调用点自动被覆盖，无需逐处加 if——
# 这才算真的"在 provider 收口"，只是收口点比原计划低一层。
# ══════════════════════════════════════════════════════════════════════════

class _BudgetGuardedMessages:
    """给 client.messages 加一道硬上限闸。

    只拦 create / stream 两个真正发请求的入口，其余属性透传。
    刻意不改返回值形态：create 返回协程、stream 返回异步上下文管理器，
    检查是同步的，检查完把原对象原样交出去，调用方的 await / async with 不受影响。
    """

    def __init__(self, inner):
        self._inner = inner

    def create(self, *args, **kwargs):
        from core.usage import assert_budget_ok
        assert_budget_ok()
        return self._inner.create(*args, **kwargs)

    def stream(self, *args, **kwargs):
        from core.usage import assert_budget_ok
        assert_budget_ok()
        return self._inner.stream(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _BudgetGuardedClient:
    """只为了把 .messages 换成带闸的版本，其余一律透传给真实客户端。"""

    def __init__(self, inner):
        self._inner = inner
        self.messages = _BudgetGuardedMessages(inner.messages)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_guarded_client(**kwargs):
    """构造 AsyncAnthropic 并套上预算闸。所有建客户端的地方都必须走这里，
    否则 reconfigure 之后闸就没了（有两处 reconfigure 也在建客户端）。"""
    return _BudgetGuardedClient(anthropic.AsyncAnthropic(**kwargs))


# ══════════════════════════════════════════════════════════════════════════
# 未配置态：没有 key 时也要能把界面打开
# ══════════════════════════════════════════════════════════════════════════
# Nano 早期只能在 `.env` 里写 key。做成脱离浏览器的桌面应用之后，目标是
# **用户不碰任何文件、一律在 UI 里配**——设置里「通用 → 环境配置」那个弹窗写 .env +
# 刷新 os.environ + 调 provider.reconfigure()，整条链早就是通的。
#
# 但它一直用不上：`ClaudeProvider.__init__` 在没有任何 key 时直接 raise，
# 于是 `WebUI()` 构造失败 → __main__ 的兜底 except 打印"内核启动异常"退出。
# **而填 key 的入口就在那个打不开的界面里**，形成死锁：
# 要填 key 得先启动，要启动得先有 key。
#
# 修法：构造永不失败。没有 key 就停在"未配置态"——客户端换成下面这个哨兵，
# 真正发请求时才抛 ProviderNotConfigured（可捕获、可给用户可读提示），
# 而不是在启动阶段把整个应用带走。

class ProviderNotConfigured(RuntimeError):
    """尚未配置任何可用的 API key。

    刻意做成"调用时才抛"而不是"构造时就抛"：用户必须能先把界面打开，
    才有地方填 key。上层（orchestrator.handle_query）捕获它并给出可读提示。
    """

    def __init__(self, msg: str = "尚未配置 API Key。点右上角三个点打开设置，在「通用 → 环境配置」里填写，保存后即时生效，无需重启。"):
        super().__init__(msg)


class _UnconfiguredMessages:
    def create(self, *args, **kwargs):
        raise ProviderNotConfigured()

    def stream(self, *args, **kwargs):
        raise ProviderNotConfigured()

    def __getattr__(self, name):
        raise ProviderNotConfigured()


class _UnconfiguredClient:
    """未配置态的客户端哨兵。任何真实调用都会抛出可读错误，而不是
    `AttributeError: 'NoneType' object has no attribute 'messages'`。
    rag.py 那三处直接拿 `_client.messages.create` 的调用点同样被覆盖。"""

    def __init__(self):
        self.messages = _UnconfiguredMessages()

    def __getattr__(self, name):
        raise ProviderNotConfigured()


class ClaudeProvider:
    def __init__(self):
        """构造【永不失败】。没有 key 就停在未配置态，理由见上方 ProviderNotConfigured。

        实现上直接复用 reconfigure()——两者读的是同一批环境变量、做的是同一件事，
        原来是两份几乎一样的代码，改一处漏一处（给客户端加预算闸时就差点只改了一处）。
        """
        self._client = _UnconfiguredClient()
        self.target_model = _DEFAULT_RELAY_MODEL
        self._relay_mode = False
        self.is_relay = False
        self.relay_label = ""
        if not self.reconfigure():
            logger.warning(
                "[Provider] 未配置任何 API key —— 以未配置态启动，界面可正常打开。"
                "点右上角三个点打开设置，在「通用 → 环境配置」里填写 Key，保存后即时生效，无需重启。"
            )

    @property
    def is_configured(self) -> bool:
        """当前是否有可用客户端。UI 据此决定要不要首启就把环境配置弹窗顶到用户面前。"""
        return not isinstance(self._client, _UnconfiguredClient)

    def reconfigure(self) -> bool:
        """UI「环境配置」保存后调用：重读 env、原地重建客户端 + is_relay/relay_label/target_model。
        原地改而非新建对象——orchestrator 持有的是同一个 provider 引用，这样它也一起更新。
        返回 True=配好了，False=没有任何可用 key。"""
        # 🔴 **只有 API Key 是必填的。** 中转地址和代理都是可选 ——
        #    2026-08-31 实测：填了 key、留空中转地址，却被强制弹出配置窗。
        #    根因是老代码写成 `if relay_base and relay_key`，把 base_url 当成了
        #    "这把 key 算不算数"的开关；而它真实的语义是"发到哪个地址"。
        #    📌 **key 有没有** 和 **地址填不填** 是两件事，不该绑在一个条件里。
        key = ((os.getenv("NANO_API_RELAY_API_KEY") or "").strip()
               or (os.getenv("ANTHROPIC_API_KEY") or "").strip())
        if not key:
            return False

        # ── 厂商 → 官方地址；用户填的中转地址优先 ──────────────────────
        # ⚠️ 两个正交的维度，不要合并：
        #      厂商  决定【官方地址是什么】—— 我们从官方文档抄来的事实（厂商表里）
        #      端点  决定【用不用用户填的地址】—— 用户的事（env / UI）
        #    老代码只有"Anthropic 默认"和"用户填的"，没有"某厂商的官方地址"，
        #    于是深度求索官方（https://api.deepseek.com/anthropic）无处安放。
        vendor = (os.getenv("NANO_API_VENDOR") or "anthropic").strip().lower()
        relay_base = (os.getenv("NANO_API_RELAY_BASE_URL") or "").strip()
        try:
            from core.models import vendor_meta as _vm
            official = _vm(vendor).get("official_base") or ""
        except Exception:
            official = ""
        base = relay_base or official

        try:
            self._client = (_make_guarded_client(api_key=key, base_url=base)
                            if base else _make_guarded_client(api_key=key))
            self.vendor = vendor
            self._relay_mode = self.is_relay = bool(relay_base)
            self.relay_label = (relay_base.split("//")[-1].split("/")[0]
                                if relay_base else "")
            self.target_model = default_model_for(vendor, relay=bool(relay_base))
            logger.info(
                f"{'🔀 [Relay]' if relay_base else '🌐 [Direct]'} "
                f"{vendor} · {base or 'SDK 默认地址'} · 模型 {self.target_model}")
            return True
        except Exception as e:
            logger.warning(f"[Provider] reconfigure 失败: {e}")
        return False

    # ── Prompt Cache helper ─────────────────────────────────────────────────

    @staticmethod
    def _cached_system(system_guide: str):
        """把 system prompt 包成带 cache_control 的格式，命中缓存后 input 只收 10%。

        若 system_guide 含 CACHE_BREAK_MARKER，按它切成两块：
          - 标记前的【稳定前缀】→ 带 cache_control（每轮一致，常年命中）
          - 标记后的【动态注入】→ 不带 cache_control（每轮都变，本就不该进缓存）
        这样动态内容（ambient/session_log/episodic）不再打穿稳定前缀的缓存。
        无标记则退回老行为（整体一个缓存块）。
        """
        if not system_guide:
            return system_guide
        if CACHE_BREAK_MARKER in system_guide:
            stable, _, dynamic = system_guide.partition(CACHE_BREAK_MARKER)
            blocks = []
            if stable:
                blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
            if dynamic:
                blocks.append({"type": "text", "text": dynamic})
            return blocks or system_guide
        return [{"type": "text", "text": system_guide, "cache_control": {"type": "ephemeral"}}]

    # ── 工具格式转换 ────────────────────────────────────────────────────────

    @staticmethod
    def _to_anthropic_tools(tools_manifest: list, stable_n: int = -1) -> list:
        """将 Nano 内部 manifest 格式转换为 Anthropic tools 格式。

        `stable_n`：前多少个是**每轮都一样**的。断点打在第 `stable_n-1` 个上，
        它后面的工具（条件工具 / `load_tools` 临时加载的）随便变都不伤前缀。
        传 -1 表示不知道 → 退回旧行为（打在最后一个上）。
        """

        def _norm(schema):
            """递归把 JSON schema 里的 type 值归一化为小写（STRING→string 等）。"""
            if not isinstance(schema, dict):
                return schema
            out = {}
            for k, v in schema.items():
                if k == "type" and isinstance(v, str):
                    out[k] = v.lower()
                elif isinstance(v, dict):
                    out[k] = _norm(v)
                elif isinstance(v, list):
                    out[k] = [_norm(i) if isinstance(i, dict) else i for i in v]
                else:
                    out[k] = v
            return out

        result = []
        for t in tools_manifest:
            raw = t.get("parameters") or t.get("input_schema") or {}
            props = raw.get("properties", {})
            props = {k: _norm(v) if isinstance(v, dict) else v for k, v in props.items()}
            schema = {"type": "object", "properties": props}
            if "required" in raw:
                schema["required"] = raw["required"]
            result.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": schema,
            })
        # ⭐⭐⭐ [2026-08-23 实测账单] **断点打在「最后一个稳定 tool」上，不是最后一个。**
        #
        # 缓存按 `tools → system → messages` 顺序生成前缀，
        # 📌 **tools 在最前面，所以它是最脆弱的位置** —— 它一变，
        #    system 和整条历史的缓存**全部作废**。
        #
        # 🔴 旧写法把断点打在**最后一个** tool 上，于是「缓存整个 tools 数组」——
        #    而这个数组每轮都在变：`load_tools` 临时加载、`availability` 条件工具
        #    进出、回看轮把 deferred 全放出来（实测见过 7 个 → 61 个）。
        #    旧注释写的「下一轮重新稳定，可接受」**与实测不符**：
        #    实测 `tools 7→8` 那一次直接 `cache_read=0 / creation=8064`，
        #    而前缀完好的那次是 `read=6691 / creation=909` —— **差 5.6 倍**。
        #    24 小时账单：`Cache Creation 1M : Cache Read 675K`
        #    （写进去 1.48 个 token 才读到 1 个，缓存要回本得读两次以上）。
        #
        # ⭐ 修法**不是新发明**：`_cached_system` 早就用这个手法把 system 切成
        #    `stable ⟂ dynamic`，断点打在中间 —— 实测证明它有效（dynamic 每轮都变，
        #    代价只有 ~900 create）。📌 **一个已经存在的形状，第二次出现时该复用它。**
        #    这里只是把同一个手法用在 tools 上：
        #        [ 无条件核心 ] ⟂ [ 条件核心 ] + [ load_tools 加载的 ]
        #                    ↑ 断点
        #
        # ⚠️ 顺序前提：`load_tools` 是 **append** 到末尾的（`tools_manifest.append`），
        #    而 `_core_manifest` 已按「无条件在前」排好 —— 两者共同保证
        #    前 `stable_n` 个每轮一致。📌 这个修法的正确性**依赖顺序**，
        #    所以那两处都留了断言。
        # ⚠️ `stable_n` 落在 [1, len] 之外时退回旧行为，**不抛** ——
        #    📌 一个缓存优化不该有能力让请求发不出去。
        if result:
            _i = len(result) - 1
            if 1 <= stable_n <= len(result):
                _i = stable_n - 1
            result[_i] = {**result[_i], "cache_control": {"type": "ephemeral"}}
        return result

    # ── Extended thinking helpers ───────────────────────────────────────────

    @staticmethod
    def _block_to_plain_dict(block) -> Dict[str, Any]:
        """把 Anthropic SDK content block 转成普通 dict，保留 signature 等所有字段。

        用 mode='json' 确保 bytes 类型（如 signature/data）序列化为 base64 字符串，
        而不是 Python bytes 对象（bytes 对象在后续 API 调用时会被错误编码）。
        """
        if isinstance(block, dict):
            return {k: v for k, v in block.items() if v is not None}
        if hasattr(block, "model_dump"):
            try:
                return block.model_dump(mode="json", exclude_none=True)
            except TypeError:
                return block.model_dump(exclude_none=True)
        if hasattr(block, "dict"):
            return block.dict(exclude_none=True)
        out = {}
        for k in ("type", "thinking", "signature", "data", "text", "id", "name", "input"):
            if hasattr(block, k):
                v = getattr(block, k)
                if v is not None:
                    out[k] = v
        return out

    @classmethod
    def _extract_thinking_blocks(cls, content_blocks) -> List[Dict[str, Any]]:
        """从 final_msg.content 提取 thinking/redacted_thinking blocks，原样保留。"""
        out = []
        for b in content_blocks or []:
            d = cls._block_to_plain_dict(b)
            if d.get("type") in ("thinking", "redacted_thinking"):
                out.append(d)
        return out

    # ── 上下文预处理 ────────────────────────────────────────────────────────

    @staticmethod
    def _has_preserved_blocks(content) -> bool:
        """content（list）里是否含必须原样保留的块：thinking/redacted_thinking/tool_use。"""
        if not isinstance(content, list):
            return False
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking", "tool_use"):
                return True
        return False

    @staticmethod
    def _merge_context(context: list) -> list:
        """合并连续相同 role 的消息（Anthropic 要求严格交替）。"""
        if not context:
            return []
        merged = [dict(context[0])]
        for msg in context[1:]:
            last = merged[-1]
            if msg["role"] == last["role"]:
                # 合并 content
                lc = last["content"]
                mc = msg["content"]
                # ── 崩溃修复：上一条 assistant 含 thinking/tool_use（必须原样保留），
                # 而新来的是纯文本（典型=主动开口插进来的一句）→ 合并会改动 thinking 块，
                # 触发 Anthropic 400「thinking blocks cannot be modified」。直接丢弃这句，
                # 既保住 thinking 原样、又不破坏 user/assistant 严格交替。
                if (last["role"] == "assistant"
                        and ClaudeProvider._has_preserved_blocks(lc)
                        and not ClaudeProvider._has_preserved_blocks(mc)):
                    logger.debug("[Provider] 跳过会破坏 thinking 块的同角色合并（丢弃插入的纯文本 assistant 消息）")
                    continue
                if isinstance(lc, str) and isinstance(mc, str):
                    last["content"] = lc + "\n" + mc
                elif isinstance(lc, list) and isinstance(mc, list):
                    last["content"] = lc + mc
                elif isinstance(lc, str):
                    last["content"] = [{"type": "text", "text": lc}] + (mc if isinstance(mc, list) else [{"type": "text", "text": mc}])
                else:
                    last["content"] = (lc if isinstance(lc, list) else [{"type": "text", "text": lc}]) + [{"type": "text", "text": mc}]
            else:
                merged.append(dict(msg))
        return merged

    @staticmethod
    def _mark_last_message_cacheable(messages: list) -> list:
        """给最后一条消息的最后一个 content block 打 cache_control。

        效果：让"system + tools + 全部对话历史"作为一个连续前缀一起进缓存
        （system+tools 本身已 >4096 门槛，历史附在后面一起被缓存）。多轮工具任务里
        历史随轮次增长、原本每轮全价重发——打上后，历史变 cache_read(0.1x)，只有最新
        一轮的增量是 fresh。对齐 Claude Code 的对话缓存。

        安全：只标最后一条（react 循环里那必是 user / tool_result），绝不碰 assistant
        的 thinking / tool_use 块（改动它们会触发 Anthropic 400）。历史太短时 Anthropic
        自动不缓存，无副作用。不改原对象（浅拷贝末条）。"""
        if not messages:
            return messages
        last = messages[-1]
        if last.get("role") != "user":
            return messages
        content = last.get("content")
        if isinstance(content, str):
            new_last = dict(last)
            new_last["content"] = [{"type": "text", "text": content,
                                     "cache_control": {"type": "ephemeral"}}]
            return messages[:-1] + [new_last]
        if isinstance(content, list) and content:
            blocks = list(content)
            tail = dict(blocks[-1]) if isinstance(blocks[-1], dict) else \
                {"type": "text", "text": str(blocks[-1])}
            tail["cache_control"] = {"type": "ephemeral"}
            blocks[-1] = tail
            new_last = dict(last)
            new_last["content"] = blocks
            return messages[:-1] + [new_last]
        return messages

    # ── 核心流式方法 ────────────────────────────────────────────────────────

    async def chat_with_tools_stream(
        self,
        context: list,
        tools_manifest: list,
        system_guide: str,
        model_override: str | None = None,
        stable_tool_count: int = -1,
        **_,
    ):
        """主流式决策方法。yield 事件格式与 Gemini 版本完全兼容：
          {"type": "notice_delta", "text": ...}   — thinking 增量
          {"type": "answer_delta", "text": ...}   — 最终答案增量
          {"type": "answer_discard"}              — 有 tool_use，撤销已发文本
          {"type": "done", "decision": AgentDecision, "model": ..., "notice": ...}
        """
        model = model_override or self.target_model
        messages = self._merge_context(context)
        # ⭐ `stable_tool_count`：`tools_manifest` 的前多少个是**每轮都一样**的。
        #    缓存断点打在那一个上 —— 它后面（条件工具 / `load_tools` 追加的）
        #    随便变都不伤前缀。
        #    📌 缓存顺序是 `tools → system → messages`，tools 在**最前面**，
        #       是最脆弱的位置：它一变，system 和整条历史全废。
        #    ⚠️ 不传 / 越界时退回旧行为（打在最后一个上），**不抛** ——
        #       一个缓存优化不该有能力让请求发不出去。
        tools = self._to_anthropic_tools(tools_manifest, stable_tool_count)
        think_text = ""
        answer_text = ""
        answer_started = False

        try:
            # 8192 → 16384（2026-08-05 实测）。
            # 一个 7 项功能的 Windows 网络配置 Skill 正好把 8192 用光被截断
            # （`output=8192`，一字不差等于上限），拿到空 `code`。
            # Skill 代码动辄 200+ 行 + docstring + get_spec，8192 是真的不够。
            #
            # ⚠️ `max_tokens` 是**上限不是预留**：调高不会让便宜的轮次变贵，
            # 只是不再把长回复砍断。Haiku 4.5 的输出上限远高于这个数。
            # 真正的成本控制在预算闸上，不该靠"把回复砍短"来省。
            max_tokens = 16384
            # ── TOKEN 诊断（临时）：打印 system/tools/messages 各自体量，定位 45K 来源 ──
            try:
                import json as _dbgjson
                _sys_c = len(system_guide or "")
                _tools_c = len(_dbgjson.dumps(tools, ensure_ascii=False)) if tools else 0
                _msgs_c = len(_dbgjson.dumps(messages, ensure_ascii=False))
                _ntools = len(tools) if tools else 0
                logger.warning(
                    f"[TOKEN-DIAG] system={_sys_c}字符 | tools={_tools_c}字符({_ntools}个) "
                    f"| messages={_msgs_c}字符({len(messages)}条) | 合计≈{_sys_c+_tools_c+_msgs_c}字符"
                )
                # ⭐ 归因：这一次的前缀相对上一次，**第一个**变掉的是哪一段。
                #    ⚠️ 放在这里而不是 `_record_usage` 里：那边拿不到
                #       system/tools/messages 三样原料，只能看结果。
                #       📌 **归因要在有原料的地方做**，只有结果的地方只能猜。
                try:
                    _cache_diag(_cache_sig(system_guide, tools, messages,
                                           stable_tool_count), "react")
                except Exception as _e_cd:
                    logger.debug(f"[CACHE-DIAG] 归因跳过: {_e_cd}")
                if tools:
                    _tool_sizes = []
                    for _t in tools:
                        try:
                            _tool_sizes.append((len(_dbgjson.dumps(_t, ensure_ascii=False)), _t.get("name", "")))
                        except Exception:
                            pass
                    _tool_sizes.sort(reverse=True)
                    logger.warning(
                        "[TOKEN-DIAG-TOOLS] "
                        + " | ".join(f"{_name}:{_size}" for _size, _name in _tool_sizes[:10])
                    )
                if CACHE_BREAK_MARKER in (system_guide or ""):
                    _stable, _, _dynamic = (system_guide or "").partition(CACHE_BREAK_MARKER)
                    logger.warning(f"[TOKEN-DIAG-SYSTEM] stable={len(_stable)}字符 | dynamic={len(_dynamic)}字符")
            except Exception:
                pass
            # Debug: log messages with thinking blocks to catch "cannot be modified" errors
            for _mi, _m in enumerate(messages):
                _mc = _m.get("content", "")
                if isinstance(_mc, list):
                    for _ci, _cb in enumerate(_mc):
                        if isinstance(_cb, dict) and _cb.get("type") in ("thinking", "redacted_thinking"):
                            logger.debug(f"[Provider] msg[{_mi}].content[{_ci}] type={_cb.get('type')} sig_len={len(_cb.get('signature','') or _cb.get('data',''))}")
            # ⭐⭐ **上下文计量的收口点就在这里** —— request 形状已定、还没发出去。
            #
            # ⚠️ 不能放 `MemoryManager`：provider 在这之后还会再变形三次
            #    （`_merge_context` 防 400 丢消息 / `_cached_system` 切 stable-dynamic /
            #      tools 转 `input_schema`）。
            #    📌 **Memory 里的变化量 ≠ 真正发出去的变化量。**
            #    与成本闸下沉到 provider 是同一条经验：别在十个上游各补一次。
            # ⚠️ 全程吞异常：计量是观测层，**绝不许影响发请求**。
            _f5_est = 0
            try:
                from core.context import get_meter as _f5_get, MAIN_REACT as _F5_LANE
                _f5_pred, _f5_est = _f5_get().predict(
                    vendor="anthropic", model=model, lane=_F5_LANE,
                    system=self._cached_system(system_guide), messages=messages, tools=tools)
                _f5_get().note_prediction(_f5_pred)
                # ⭐ 顺手记一次「底噪」= system + 工具表（**不含对话历史**）。
                #    重置对话之后的上下文就是这么厚 —— 它**不是 0**，而 UI 需要它
                #    才能在"刚重置、还没说话"时给出一个诚实的数字。
                from core.context.meter import estimate_request as _f5_er
                _f5_get()._note_floor(model, _f5_er(
                    self._cached_system(system_guide), [], tools))
            except Exception as _f5_e:
                logger.debug(f"[ContextMeter] 预测跳过: {_f5_e}")

            # ⭐⭐⭐ 硬窗口守卫 —— **在 request 形状已定、真正发出去之前**。
            #
            # ⚠️ 它与阶梯回答的是两个问题：阶梯是「什么时候该遗忘」（启发式，
            #    配额还没标定），Guard 是「这次到底能不能发」（硬边界）。
            #    📌 **一个启发式的机制，底下必须垫一个确定性的兜底。**
            # ⭐⭐ **2026-08-16：从「只告警」升级成【真的拦截】。**
            #    在此之前它叫 Window Guard *Shadow* —— 发现超限、记一条 ERROR、
            #    然后照常发出去，等 API 回 400。
            #    📌 **一个没有拒绝权的守卫，本质上只是一行日志。**
            #    ⚠️ 而阶梯已经正式打开、开始真的管长上下文了 ——
            #       📌 一个已经在管长上下文的系统，如果它唯一的合法性兜底
            #          只会事后报 400，那它管的是「通常情况」。
            #
            # 四步：
            #   ① 发前检查最终 request     ← 这里是唯一知道最终形状的地方
            #   ② 超限 → 只紧急降级**旧的 closed exchange**
            #   ③ **当前用户输入绝不静默删**（`emergency_reclaim` 不碰 active）
            #   ④ 仍装不下 → **拒绝发送并说明**（抛 `ContextWindowExceeded`）
            #
            # ⚠️ `predicted is None` 时**放行**（算不出就不拦）——
            #    ⭐ 但那恰恰是重启第一次 / 切模型 / 残差作废锚的时刻，
            #       所以这里**留一条 info**：📌 一个「算不出所以没管」的时刻，
            #       如果连痕迹都不留，事后无法与「管了且没问题」区分。
            # 历史厚度（供 `classify` 区分两种超限）。算不出就当 0 ——
            # ⚠️ 那会让判定偏向 `active_too_big`（更保守的那一种：
            #    告诉用户「拆小这次输入」而不是「再删点历史」）。
            #    📌 分不清时，宁可给一句**做了也不会更糟**的建议。
            _f5_hist = locals().get('_f5_hist_tokens') or 0
            from core.context.guard import ContextWindowExceeded as _CWE
            try:
                from core.context import guard as _G
                _gp = _G.preflight(model, _f5_pred)
                _need_block = not _gp["ok"]
            except _CWE:
                raise
            except Exception as _ge:
                logger.debug(f"[Guard] 预检跳过: {_ge}")
                _gp, _need_block = {"ok": True}, False

            if _f5_pred is None:
                logger.info("[Guard] 这一次没有预测值（重启首轮/刚切模型/锚作废）"
                            "→ 不拦截。下一次真实计量后恢复。")

            if _need_block:
                # ── ④ 拒发。⚠️ 两种超限说的话不一样 ──
                #
                # 🔴 **这里【不】做紧急回收** —— provider 不该碰 memory。
                #    📌 provider 是**唯一**知道最终 request 形状的地方，
                #       但它没有权力去改用户的历史；能做的只有两件：
                #       发，或者说发不了。
                #    ⭐ 回收与重试归 orchestrator（它拥有 memory）——
                #       见 `_send_with_window_guard`。
                #    ⚠️ 第一版把回收写在了这里，还要给 provider 挂一个
                #       `_f5_memory` 句柄 ——
                #       📌 **一个为了少写一层而拉进来的依赖，
                #          会让"谁有权改什么"这件事从此说不清。**
                _kind, _msg = "still_too_big", _G.MSG_STILL_TOO_BIG
                try:
                    if _G.classify(model, _gp["predicted"],
                                   int(_f5_hist or 0)) == "active_too_big":
                        _kind, _msg = "active_too_big", _G.MSG_ACTIVE_TOO_BIG
                except Exception:
                    pass
                logger.error(
                    f"[Guard] 🔴 拒绝发送：预计 {_gp['predicted']:,} > 上限 "
                    f"{_gp['limit']:,}（{_kind}）")
                raise _CWE(_kind, _msg,
                           predicted=int(_gp["predicted"]),
                           limit=int(_gp["limit"]))

            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                thinking=_thinking_arg(max_tokens, model),
                system=self._cached_system(system_guide),
                tools=tools if tools else anthropic.NOT_GIVEN,
                messages=self._mark_last_message_cacheable(messages),
            ) as stream:
                async for event in stream:
                    etype = type(event).__name__
                    if etype == "ThinkingEvent":
                        chunk = event.thinking or ""
                        if chunk:
                            think_text += chunk
                            yield {"type": "notice_delta", "text": chunk}
                    elif etype == "TextEvent":
                        chunk = event.text or ""
                        if chunk:
                            answer_text += chunk
                            answer_started = True
                            yield {"type": "answer_delta", "text": chunk}
                    elif etype == "InputJsonEvent":
                        if event.partial_json:
                            yield {"type": "tool_input_delta", "partial_json": event.partial_json}

                final_msg = await stream.get_final_message()
            _record_usage(final_msg.usage, model, "react")
            # ⭐ 用真值刷新锚 + 残差自检。
            # ⚠️ **只有走到这里才刷新** —— 用户中途停 stream 时压根到不了
            #    `get_final_message()`，那时锚只是变旧，不是变错。
            try:
                from core.context import get_meter as _f5_get2, MAIN_REACT as _F5_LANE2
                _f5_get2().observe(vendor="anthropic", model=model, lane=_F5_LANE2,
                                   usage=final_msg.usage, local_estimate=_f5_est)
            except Exception as _f5_e2:
                logger.debug(f"[ContextMeter] 观测跳过: {_f5_e2}")

            # 提取 thinking blocks（必须来自 final_msg.content，不能从 delta 拼）
            thinking_blocks = self._extract_thinking_blocks(final_msg.content)

            # 提取 tool_use blocks（支持多工具 ReAct）
            tool_blocks = [b for b in final_msg.content if getattr(b, "type", "") == "tool_use"]

            if tool_blocks:
                # ── 撞 max_tokens 被截断的 tool_use 是**不可用的** ────────────
                # 实测：生成一个 7 项功能的 Windows 网络配置 Skill，
                # `output=8192`（正好等于上限）→ 响应中途被切断 →
                # `WriteSkill` 的 `code` 参数是空的 → 审计窗口报出 7 条
                # "缺少 import / 缺少类定义 / 缺少 get_spec()…"，**全都是空代码的必然产物**。
                #
                # 于是用户看到的是"模型不懂协议"，真相是"模型话没说完"。
                # 这条诊断路径原来只在**没有文本块**那个分支里才检查 stop_reason，
                # 有 tool_use 时直接跳过 —— 截断因此完全静默。
                _stop = getattr(final_msg, "stop_reason", "") or ""
                if _stop == "max_tokens":
                    logger.error(
                        f"[Provider] ⚠️ 响应撞 max_tokens={max_tokens} 被截断，"
                        f"tool_use 参数不完整: "
                        f"{[getattr(b, 'name', '?') for b in tool_blocks]} "
                        f"—— 下游拿到的参数会是残缺或空的"
                    )
                if answer_started:
                    yield {"type": "answer_discard"}
                # ⚠️ 诊断：**空参数的 tool_use 有两种完全不同的成因**，
                #    而它们在下游长得一模一样（都是 `Missing required parameter`）：
                #      ① 模型真的没传        —— `input` 是 {}，stop_reason 正常
                #      ② 响应被截断/中继吃掉 —— stop_reason=max_tokens，或 input 残缺
                #    📌 分不清这两者，就只能在「改提示词」和「查网络」之间瞎猜。
                for _tb in tool_blocks:
                    if not getattr(_tb, "input", None):
                        logger.warning(
                            f"[Provider-Diag] tool_use【{_tb.name}】参数为空 | "
                            f"stop_reason={_stop or '(none)'} | "
                            f"input={getattr(_tb, 'input', None)!r} | "
                            f"同批工具={[getattr(b,'name','?') for b in tool_blocks]}"
                        )
                tool_calls = [
                    ToolCall(
                        name=tb.name,
                        args=dict(tb.input) if tb.input else {},
                        tool_use_id=tb.id,
                        index=idx,
                    )
                    for idx, tb in enumerate(tool_blocks)
                ]
                decision = AgentDecision(
                    "call_many" if len(tool_calls) > 1 else "call",
                    tool_calls=tool_calls,
                    thinking_blocks=thinking_blocks,
                    # 被 answer_discard 撤销掉的正文原样带出。绝大多数调用方不看它
                    # （主循环调工具前的碎话本就该丢），但 Skill 探索的 conclude 出口需要它——
                    # 那段正文就是探索结论本身，丢了会让用户看到"长结论被一句短摘要整段覆盖"。
                    discarded_text=answer_text or "",
                    # 撞上限被截断 → 参数残缺。调用方必须能区分
                    # "模型写了个空的" 和 "模型话没说完"。
                    truncated=(_stop == "max_tokens"),
                )
            else:
                # ── 空文本的兜底与诊断 ──────────────────────────────
                # `answer_text` 是从流式 `text_delta` 事件累积来的，**不是**从
                # `final_msg.content` 提取的。于是只要 delta 没被捕获到
                # （网络抖动丢事件、中转不发 delta 只发最终消息、block 类型意外等），
                # 这里就会拿到空串，而调用方只能看到"模型什么都没说"。
                #
                # 实测两次撞上同一个签名（`output>0` 但文本为空）：
                #   · `chat_without_tools_or_call` → output=8，探索链尾兜底
                #   · 本函数 tools=[] 那一路      → output=8，inspect 后总结失败，
                #     用户看到"我查看了已有 Skill…但这次没能整理出完整的文字说明"
                #
                # 两条修法都做：
                #   ① **从 final_msg 里再捞一次**——如果文本其实在最终消息里，
                #      这就是真修复，而不是把失败包装得好看一点；
                #   ② 捞不到就把 stop_reason 与 block 类型如实打出来，
                #      让"到底是模型没说、还是我们没接住"下一次能一眼分清。
                if not (answer_text or "").strip():
                    _recovered = "".join(
                        getattr(b, "text", "") or ""
                        for b in (final_msg.content or [])
                        if getattr(b, "type", "") == "text"
                    )
                    _types = [getattr(b, "type", "?") for b in (final_msg.content or [])]
                    if _recovered.strip():
                        logger.warning(
                            f"[Provider] 流式 text_delta 没接到文本，已从 final_msg 补回 "
                            f"{len(_recovered)} 字符 · blocks={_types}"
                        )
                        answer_text = _recovered
                    else:
                        logger.warning(
                            f"[Provider] 模型确实没产出任何文本 · "
                            f"stop_reason={getattr(final_msg, 'stop_reason', '?')} · "
                            f"blocks={_types or '[]'} · "
                            f"output_tokens={getattr(final_msg.usage, 'output_tokens', '?')}"
                        )
                decision = AgentDecision("text", content=answer_text)

            yield {
                "type": "done",
                "decision": decision,
                "model": model,
                "notice": think_text,
            }

        except anthropic.APIStatusError as e:
            logger.error(f"[Provider] API 错误 {e.status_code}: {e.message}")
            if e.status_code == 400:
                logger.error(f"[Provider] 400错误时的messages摘要 (共{len(messages)}条):")
                for _mi, _m in enumerate(messages):
                    _mc = _m.get("content", "")
                    if isinstance(_mc, list):
                        _types = [(_cb.get("type","?") if isinstance(_cb,dict) else type(_cb).__name__) for _cb in _mc]
                        logger.error(f"  messages[{_mi}] role={_m.get('role')} content_types={_types}")
                    else:
                        logger.error(f"  messages[{_mi}] role={_m.get('role')} content={str(_mc)[:80]!r}")
            # 400 用 provider_error 类型，让 orchestrator 清理 memory 而不是当普通回答显示
            _err_decision = AgentDecision("provider_error", content=f"[API Error {e.status_code}] {e.message}")
            _err_decision.error_status_code = e.status_code
            yield {"type": "done", "decision": _err_decision, "model": model, "notice": think_text}
        except Exception as e:
            logger.error(f"[Provider] chat_with_tools_stream 异常: {e}")
            _err_decision = AgentDecision("provider_error", content=f"[Request Error] {e}")
            _err_decision.error_status_code = 0
            yield {"type": "done", "decision": _err_decision, "model": model, "notice": think_text}

    # ── 无工具流式（最终回答用）──────────────────────────────────────────

    async def chat_without_tools_stream(
        self,
        context: list,
        system_guide: str,
        model_override: str | None = None,
        **_,
    ):
        """无工具流式，yield {"type": "text_delta", "text": ...} + done（含 text 字段）。"""
        model = model_override or self.target_model
        messages = self._merge_context(context)
        answer_text = ""

        try:
            max_tokens = 4096
            try:
                import json as _dbgjson
                _sys_c = len(system_guide or "")
                _msgs_c = len(_dbgjson.dumps(messages, ensure_ascii=False))
                logger.warning(
                    f"[TOKEN-DIAG-NOTOOLS] system={_sys_c}字符 | tools=0字符(0个) "
                    f"| messages={_msgs_c}字符({len(messages)}条) | 合计≈{_sys_c+_msgs_c}字符"
                )
                if CACHE_BREAK_MARKER in (system_guide or ""):
                    _stable, _, _dynamic = (system_guide or "").partition(CACHE_BREAK_MARKER)
                    logger.warning(f"[TOKEN-DIAG-SYSTEM] stable={len(_stable)}字符 | dynamic={len(_dynamic)}字符")
            except Exception:
                pass
            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                thinking=_thinking_arg(max_tokens, model),
                system=self._cached_system(system_guide),
                messages=messages,
            ) as stream:
                async for event in stream:
                    if type(event).__name__ == "TextEvent":
                        chunk = event.text or ""
                        if chunk:
                            answer_text += chunk
                            yield {"type": "text_delta", "text": chunk}
                final_msg = await stream.get_final_message()
            _record_usage(final_msg.usage, model, "fastpath")

            yield {"type": "done", "text": answer_text,
                   "decision": AgentDecision("text", content=answer_text),
                   "model": model, "notice": ""}
        except Exception as e:
            logger.error(f"[Provider] chat_without_tools_stream 异常: {e}")
            yield {"type": "done", "text": f"[Request Error] {e}",
                   "decision": AgentDecision("text", content=f"[Request Error] {e}"),
                   "model": model, "notice": ""}

    # ── 非流式（联网搜索等同步路径）────────────────────────────────────────

    async def chat_with_tools(
        self,
        context: list,
        tools_manifest: list,
        system_guide: str,
        status_callback: Any = None,
        model_override: str | None = None,
        stable_tool_count: int = -1,
        **_,
    ) -> tuple[AgentDecision, str]:
        model = model_override or self.target_model
        messages = self._merge_context(context)
        # ⭐ `stable_tool_count`：`tools_manifest` 的前多少个是**每轮都一样**的。
        #    缓存断点打在那一个上 —— 它后面（条件工具 / `load_tools` 追加的）
        #    随便变都不伤前缀。
        #    📌 缓存顺序是 `tools → system → messages`，tools 在**最前面**，
        #       是最脆弱的位置：它一变，system 和整条历史全废。
        #    ⚠️ 不传 / 越界时退回旧行为（打在最后一个上），**不抛** ——
        #       一个缓存优化不该有能力让请求发不出去。
        tools = self._to_anthropic_tools(tools_manifest, stable_tool_count)

        try:
            max_tokens = 4096
            try:
                import json as _dbgjson
                _sys_c = len(system_guide or "")
                _msgs_c = len(_dbgjson.dumps(messages, ensure_ascii=False))
                logger.warning(
                    f"[TOKEN-DIAG-NOTOOLS] system={_sys_c}字符 | tools=0字符(0个) "
                    f"| messages={_msgs_c}字符({len(messages)}条) | 合计≈{_sys_c+_msgs_c}字符"
                )
                if CACHE_BREAK_MARKER in (system_guide or ""):
                    _stable, _, _dynamic = (system_guide or "").partition(CACHE_BREAK_MARKER)
                    logger.warning(f"[TOKEN-DIAG-SYSTEM] stable={len(_stable)}字符 | dynamic={len(_dynamic)}字符")
            except Exception:
                pass
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                thinking=_thinking_arg(max_tokens, model),
                system=self._cached_system(system_guide),
                tools=tools if tools else anthropic.NOT_GIVEN,
                messages=messages,
            )
            _record_usage(resp.usage, model, "with-tools")
            thinking_blocks = self._extract_thinking_blocks(resp.content)
            tool_blocks = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if tool_blocks:
                tool_calls = [
                    ToolCall(name=tb.name, args=dict(tb.input) if tb.input else {},
                             tool_use_id=tb.id, index=i)
                    for i, tb in enumerate(tool_blocks)
                ]
                decision = AgentDecision(
                    "call_many" if len(tool_calls) > 1 else "call",
                    tool_calls=tool_calls,
                    thinking_blocks=thinking_blocks,
                    # 被 answer_discard 撤销掉的正文原样带出。绝大多数调用方不看它
                    # （主循环调工具前的碎话本就该丢），但 Skill 探索的 conclude 出口需要它——
                    # 那段正文就是探索结论本身，丢了会让用户看到"长结论被一句短摘要整段覆盖"。
                    discarded_text="".join(getattr(b, "text", "") or "" for b in resp.content if getattr(b, "type", "") == "text"),
                )
            else:
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                decision = AgentDecision("text", content=text)
            return decision, model
        except Exception as e:
            logger.error(f"[Provider] chat_with_tools 异常: {e}")
            d = AgentDecision("provider_error", content=str(e))
            if isinstance(e, anthropic.APIStatusError):
                d.error_status_code = e.status_code
            return d, model

    # ── 纯文本非流式（摘要/总结路径）────────────────────────────────────────

    async def chat_without_tools_or_call(
        self,
        context: list,
        system_guide: str,
        status_callback: Any = None,
        model_override: str | None = None,
        max_tokens: int | None = None,
        **_,
    ) -> tuple[str, str, AgentDecision | None]:
        """返回 (text, model, None)，兼容旧签名。

        ⚠️ `max_tokens` 是 2026-08-14 补的：调用方（`decay._distill_one`）一直在传
           `_DISTILL_MAX_TOKENS=700`，而它先被 `chat_without_tools` 的 `**_` 吃掉，
           再被这里写死的 4096 顶掉。📌 **一个被 `**_` 吞掉的参数，比没有这个参数
           更糟** —— 没有的话调用方会去查，被吞掉的话调用方以为它生效了。
        """
        model = model_override or self.target_model
        messages = self._merge_context(context)

        try:
            # ⚠️ 调用方给了就用调用方的（见签名注释）；没给才用这个默认值。
            max_tokens = int(max_tokens) if max_tokens else 4096
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                thinking=_thinking_arg(max_tokens, model),
                system=self._cached_system(system_guide),
                messages=messages,
            )
            _record_usage(resp.usage, model, "notools-call")
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

            # ── 诊断缺口 ────────────────────────────────────────────
            # 实测事故里这里返回 `output=8` 个 token 却拿到空文本，而调用方只能看到
            # "summary 是空的"，无从判断到底发生了什么：模型只吐了 thinking？
            # 吐了 tool_use？吐了纯空白？中转返回了别的 block 类型？
            # 四种可能对应完全不同的修法，而当时的日志一个都区分不了。
            #
            # ⚠️ 只在**没提取到文本**时才打这条日志——正常路径不加噪音。
            if not text.strip():
                _types = [getattr(b, "type", "?") for b in (resp.content or [])]
                logger.warning(
                    f"[Provider] chat_without_tools_or_call 未提取到文本 · "
                    f"stop_reason={getattr(resp, 'stop_reason', '?')} · "
                    f"blocks={_types or '[]'} · "
                    f"text_len={len(text)} · raw={text!r}"
                )

            # ⚠️ 第三个返回值原来**永远是 None**（成功路径写死），
            # 于是调用方的 `_stray_call_notice(stray)` 在成功路径上是死代码：
            # 它拿不到"模型刚才想调什么"，也就没法告诉用户/模型。
            # 现在如果模型返回的是 tool_use，如实把它包成 decision 交出去。
            _tool_block = next(
                (b for b in (resp.content or []) if getattr(b, "type", "") == "tool_use"), None
            )
            if _tool_block is not None:
                _stray = AgentDecision(
                    "call",
                    name=getattr(_tool_block, "name", "") or "",
                    args=dict(getattr(_tool_block, "input", None) or {}),
                    tool_use_id=getattr(_tool_block, "id", "") or "",
                )
                logger.warning(
                    f"[Provider] 无工具请求里模型仍返回了 tool_use: {_stray.name!r}"
                )
                return text, model, _stray
            return text, model, None
        except Exception as e:
            logger.error(f"[Provider] chat_without_tools_or_call 异常: {e}")
            d = AgentDecision("provider_error", content=str(e))
            if isinstance(e, anthropic.APIStatusError):
                d.error_status_code = e.status_code
            return "", model, d

    async def chat_without_tools(
        self,
        context: list,
        system_guide: str,
        status_callback: Any = None,
        model_override: str | None = None,
        max_tokens: int | None = None,
        **_,
    ) -> tuple[str, str]:
        """⚠️ `max_tokens` 原来被 `**_` **吃掉了**。

        调用方（`decay._distill_one`）写着 `_DISTILL_MAX_TOKENS = 700`，
        底下 `chat_without_tools_or_call` 却写死 4096。固定 schema 最后会把
        文本裁回来，所以不是信息正确性问题 —— 但**成本模型、thinking 预算、
        的预算消耗全都按一个不存在的数在算**。
        📌 **一个被 `**_` 吞掉的参数，比没有这个参数更糟** ——
           没有的话调用方会去查；被吞掉的话调用方以为它生效了。
        """
        text, model, _ = await self.chat_without_tools_or_call(
            context, system_guide, status_callback, model_override,
            max_tokens=max_tokens,
        )
        return text, model

    # ── 分类器（JSON 路径）───────────────────────────────────────────────

    async def _classify_json(
        self,
        prompt: str,
        model_override: str | None = None,
        status_callback: Any = None,
    ) -> dict:
        """通用 JSON 分类器——prompt 里已包含完整 system + user 内容。"""
        model = model_override or self.target_model
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            _record_usage(resp.usage, model, "classify")
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            # 提取 JSON
            m = re.search(r'\{.*\}', text, re.S)
            if m:
                return json.loads(m.group())
        except Exception as e:
            logger.warning(f"[Classify] 异常: {e}")
        return {}

    # ══ 这里曾经有三个「前置廉价分类器」，全部删除 ══════════════════════
    #
    # 📌 删它们的是**同一条原则**：**判断交给有完整上下文的主决策模型，
    #    不要在它前面加一个只看片段的廉价模型去替它分类。**
    #
    #   ① `classify_primary_intent`        顶层语义路由（~770 fresh token/条消息）
    #   ② `classify_pending_skill_intent`  Skill 审计路由劫持（8 标签，每条一次往返）
    #   ③ `classify_pending_action_intent` Skill 管理/修改确认（6 标签，每条一次往返）
    #
    # ⚠️ ① 早就停用了（调用方连同 fast-path 一起删掉），但**函数本体被留了下来**，
    #    此后零调用方。删它的理由不只是"没人用"：留着等于给后来人留一个
    #    "重新接上就能省事"的诱惑，而那条路已经被实测证伪过。
    # ⚠️ ② 的 8 个标签里有一半（EXPLAIN / RISK / COMPLAINT / EXECUTE_BLOCKED）
    #    本质就是"正常回答用户"，主模型天然会做，本来就不需要先分类。
    #
    # ⭐ `generate_skill_spec` **保留** —— 它是 SkillWriter 的第一步，
    #    不是路由分类器，不在这条原则的射程内。

    async def generate_skill_spec(
        self,
        query: str,
        status_callback: Any = None,
    ) -> dict:
        prompt = (
            f'The user wants to create a new local Python Skill: "{query}"\n\n'
            "Generate a SkillSpec JSON object that matches the schema below. "
            "Return JSON only. Do not include markdown, comments, or any extra text.\n\n"
            "{\n"
            '  "name": "PascalCaseName",\n'
            '  "purpose": "One-sentence purpose of this Skill. Required and non-empty.",\n'
            '  "required_inputs": [{"name": "parameter_name", "type": "string", "description": "parameter description"}],\n'
            '  "optional_inputs": [],\n'
            '  "data_output_keys": ["success"],\n'
            '  "side_effects": ["none"],\n'
            '  "permission_level": "readonly",\n'
            '  "lifecycle": "permanent"\n'
            "}\n\n"
            "[User-Facing Language]\n"
            "The purpose field is user-facing. Write it in the same language as the user's Skill request. "
            "Do not force it into English. Keep code identifiers, filenames, field names, and enum values unchanged.\n\n"
            "[Enum Constraints — Strict]\n"
            "side_effects must be a list. Allowed values: "
            "none, file_read, file_write, file_delete, shell, send_message, external_api, network, os_control.\n"
            "permission_level must be one value: "
            "readonly, workspace_write, network_allowed, external_action, dangerous.\n"
            "none, fragment_ok, generated_ok, full_required, file_path_required, web_required.\n"
            "Use file_path_required for programmatic Excel/CSV/PDF processing; "
            "full_required for extracting rules from a complete document; "
            "fragment_ok when KB fragments are enough; none when no context is needed.\n"
            "lifecycle must be: permanent.\n"
            "data_output_keys must not be empty. Even a mostly side-effect Skill must include a key such as 'success'.\n\n"
            "[OS Skill Rule]\n"
            "If the requested Skill controls the computer, screen, mouse, keyboard, windows, system settings, "
            "or real desktop/disk files through the OS layer, set side_effects to [\"os_control\"], "
            "data_output_keys to [\"dsl_plan\"], and include optional_inputs with "
            "{\"name\":\"os_context\",\"type\":\"string\",\"description\":\"Optional JSON string describing current OS execution/replan context.\"}. "
            "Use permission_level=\"external_action\" unless the requested operation is clearly dangerous."
        )
        result = await self._classify_json(prompt, status_callback=status_callback)
        if result and "name" in result:
            return result
        return {"error": "Failed to generate SkillSpec", "raw": str(result)}

    # ── 图片处理 ────────────────────────────────────────────────────────────

    @staticmethod
    def build_image_part(data: bytes, mime_type: str = "image/jpeg") -> dict:
        """构建 Anthropic 图片消息 content block。"""
        import base64
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64.b64encode(data).decode(),
            },
        }


# ── 全局单例（懒加载）──────────────────────────────────────────────────────
# 2026-08-04：原来这里是 `provider = ClaudeProvider()`，在【模块顶层】构造。
# 两个真实症状：
#
# 1. **没配 key 时 import 直接崩**。`ClaudeProvider.__init__` 在没有任何 key 时
#    `raise RuntimeError`，于是 `import core.provider` 崩 → `_bootstrap_core_modules()`
#    崩 → app 启动打 traceback。**用户连"环境配置"弹窗都进不去，没法在 UI 里填 key 自救。**
#    这是个死锁：要填 key 得先启动，要启动得先有 key。
#
# 2. **全进程有两个实例，只有一个会被 reconfigure**。`WebUI.__init__` 另外自己建了一个，
#    而 `app.py` 的 `self.provider.reconfigure()` 只重建它那个的 `_client`。
#    `core/rag.py` 三处多模态调用（扫描件 OCR 兜底、文档内嵌图片描述、图片入库）
#    import 的是模块级这个——于是用户在 UI 改完中转地址/API key 后，聊天正常，
#    **RAG 多模态还在用旧凭据，直到重启**。
#
# PEP 562 的模块级 `__getattr__` 让 `from core.provider import provider` 这个写法
# 保持可用（rag.py 三处在用），但把真正的构造推迟到第一次访问。
_provider_singleton: Optional["ClaudeProvider"] = None
_provider_lock = threading.Lock()


def get_provider() -> "ClaudeProvider":
    """全进程唯一的 provider。第一次访问时才真正构造。

    双重检查加锁——与 `rag.py` 三个懒加载单例同一范式。那三个在 v1.12 被实测抓出过
    并发初始化竞态（UI 线程与 RAG 后台线程同时进），这里同样会被两类线程访问，
    不能再写成裸的 `if x is None: 构造`。
    """
    global _provider_singleton
    if _provider_singleton is not None:      # 快路径：已建好，不进锁
        return _provider_singleton
    with _provider_lock:
        if _provider_singleton is None:      # 双重检查：等锁期间别人已经建好了
            _provider_singleton = ClaudeProvider()
    return _provider_singleton


def __getattr__(name: str):
    """PEP 562 模块级钩子：让 `from core.provider import provider` 继续可用。

    只在模块 globals 里找不到该名字时才会被调用，所以不影响其他属性。
    """
    if name == "provider":
        return get_provider()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


GeminiProvider = ClaudeProvider   # 兼容旧 import（Gemini 迁移残留，app.py 在 import 它）

