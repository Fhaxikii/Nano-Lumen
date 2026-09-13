"""上下文计量 —— 锚 + 快照差 + 残差看门狗。

⚠️⚠️ **这一层只回答一个问题：「现在的上下文占窗口多少」，用来决定要不要衰减。**
**绝不用于计费。** 计费永远只用 `usage` 的真值（`core/usage.py`）。
📌 两者混用会产生「账单和预算对不上」——**最难查的那一类，因为两边各自都是对的。**

═══ 为什么不是"写一个 token 估算器" ═══

第一版方案是「跨厂商字符→token 估算器 + EMA 校准」。追问
「**我们需要的不应该是累积量吗？把发出去的加起来不就是累计？**」，一问打出两件事：

**① 不能累加** —— 上下文**不是累加的，每轮都在重新组装**。每次请求发的是
   system + **全部历史** + 工具表，所以第 N 次的 `input_tokens` 不是"这轮新增的"，
   而是**那一轮的全部内容**。累加 = 把同一段历史数很多遍
   （三轮累加 38355，真实只有 13400）。

**② 但真值本来就每轮都在手上** —— `gross = input + cache_read + cache_creation`
   就是那一次请求的全部输入（缓存命中的仍是输入 token，只是便宜）。
   **这是 Anthropic 官方定义，不是我们的推断。**

⭐ 于是正解是 **「锚 + 增量」**，而增量**不是事件账本**，是**两次 request 快照相减**：

    predicted = anchor.actual + (estimate(now) − anchor.estimate)

📌 **它自动吃掉** history 增删 / 工具结果截断 / 图片占位符化 / system 动态段
   出现消失 / `load_tools` 新增 schema / provider 层的 `_merge_context` 合并 ——
   **一个 hook 都不用接。**
⚠️ 复查时抓出来的一点：provider 在真正发出去前**还会再变形三次**
   （防 400 丢弃消息 / 切 stable-dynamic / manifest→input_schema），
   📌 **Memory 里的变化量 ≠ provider 真正发出去的变化量。**

⭐⭐ 而且由此得到一条很强的性质：**估算器不需要准，只需要【一致】** ——
   取差值时系统性偏差会抵消。哪怕它一贯少算 20%，那 20% 只落在增量上。

═══ 🔴 锚必须带"我是谁"这一维 ═══

`provider._record_usage()` **有五个调用入口**：`react` / `fastpath` / `with-tools` /
`notools-call` / `classify`。它们的上下文根本不是同一个东西：

    42,000 ← 主 ReAct 的正确锚
     3,200 ← 一次内部摘要调用把它覆盖了
           → 下一次主 ReAct，以为上下文只有几千        🔴 系统性低估

📌 **它不会是 0、不会异常、不会 traceback，只会非常自然地告诉你「一切健康」——
   直到撞墙。**
→ 所以锚绑 `(runtime_id, vendor, model, lane)`，**只有 `MAIN_REACT` 能更新它**。
其余 usage 照常计费，但绝不许碰这个锚。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

MAIN_REACT = "main_react"

# ══════════════════════════════════════════════════════════════════════════
# 各厂商的 usage 规格 —— **差异只是「加哪几个字段」，所以它是【声明】不是【逻辑】**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 **只有 Anthropic 是三项相加**，其余三家都是"已经含在里面了，再加就是重复计"：
#
#   Anthropic  input + cache_read + cache_creation   ← 官方定义：总输入 = 三者之和
#   OpenAI     prompt_tokens                         ← cached_tokens 是它的**子集**
#   Gemini     promptTokenCount                      ← **已包含** cachedContent
#   DeepSeek   prompt_tokens                         ← = cache_hit + cache_miss
#   GLM/智谱    prompt_tokens                         ← cached 是子集（见下）
#   Kimi/月之暗面 prompt_tokens                        ← cached 是子集（见下）
#
# 📌 **接新厂商时这里加一行就够** —— 差异是声明就能配，是逻辑才要写代码。
# ⚠️ 而万一配错了（比如把 OpenAI 的 cached 也加上去、重复计数），
#    **残差看门狗会当场响** —— 见 `observe()`。这就是"不用回头担心这里"的依据。
#
# ═══ GLM / Kimi 的证据（官方文档复核）═══
#
# ⭐ **Anthropic 才是那个异类** —— 现在六家里五家都是 OpenAI-style
#    「cached 是 prompt 的一个 breakdown」，只有 Anthropic 把三项并列。
#
#   GLM   `usage.prompt_tokens_details.cached_tokens`
#         官方缓存文档直接给出 `cache_ratio = cached_tokens / prompt_tokens * 100`
#         —— **能拿它当分母，就证明它是分子的超集**。已回官方文档核实。
#   Kimi  `usage.cached_tokens`（⚠️ **顶层，不在 `prompt_tokens_details` 里**）
#         官方示例 `prompt=19, completion=21, total=40` —— 19+21=40 而不是 19+10+21=50，
#         **算术本身就证明了 cached=10 已经含在 prompt=19 里**。已回官方文档核实。
#
# 🔴 **千万别写 `prompt_tokens + cached_tokens`**：那样**缓存命中越好、上下文越被高估**
#    —— 一个「越优化越报警」的反向 bug，而且看起来完全正常。
#
# ⚠️ 别名（`moonshot` / `zhipu`）只是同一份规格的另一个 key：vendor 字符串来自配置，
#    写哪个名字都可能。⚠️ 认不出来的 vendor 会**返回 None 并 warning**（不是 0），
#    所以漏一个别名是"响亮地不工作"，不是静默失真 —— 加别名只是省一次排查。
_PROMPT_INPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"),
    "openai": ("prompt_tokens",),
    "deepseek": ("prompt_tokens",),
    "gemini": ("promptTokenCount",),
    "glm": ("prompt_tokens",),
    "zhipu": ("prompt_tokens",),      # 别名：同 glm
    "kimi": ("prompt_tokens",),
    "moonshot": ("prompt_tokens",),   # 别名：同 kimi
}

# ⏸ **刻意没做**：顺手归一出 `cached_input_tokens`（各家的 cached 字段路径
#    不同：OpenAI/GLM 在 `prompt_tokens_details.cached_tokens`，Kimi 在顶层
#    `cached_tokens`，DeepSeek 是 `prompt_cache_hit_tokens`，Gemini 是
#    `cachedContentTokenCount`，Anthropic 是 `cache_read_input_tokens`）。
#    ⚠️ 不做的理由不是"用不上"，是**这一层的边界**：本模块只回答「上下文多大」，
#    而 cached 的唯一用途是**缓存命中率与成本** —— 那是 `core/usage.py` 的事，
#    它已经在读同一批字段。📌 现在加进来，就得当场回答"这两处谁是权威"，
#    而那正是模块头第一句写死的那条边界。真要合并，是把 usage.py 一起收编，
#    不是在计量这边偷偷长出一个计费字段。
#
# ⏸ 另记：GLM 有 `/tokenizer` 端点（**支持 tools**，多模态还能拆 image/video tokens）。
#    它正好补的是这一层最弱的那一格 —— **切模型后锚失效、只能靠估算器顶一次**。
#    等真接 GLM 时再看，别现在为一个还没接的厂商写分支。


def normalize_prompt_input(usage: Any, vendor: str) -> int | None:
    """把某一家的 usage 归一成「这次请求的 prompt 输入 token」。

    ⚠️ **拿不到就返回 None，不返回 0。**
    📌 `0` 会被下游当成"这次很小"，而真相是"我不知道" ——
       而中转如果吃掉了某个字段，`getattr(..., 0)` 恰好会**安静地变成 0**，
       让 `gross` 系统性偏低 → **该压时以为没超**。这正是本层最怕的失败形状。

    ═══ 🔴 中转透传探针（2026-08-13 补，第一版有洞）═══

    第一版只防住了「**全部**字段都没有」。而用户可能经任意第三方中转访问，
    真正的风险是 **少一个**：Anthropic 三项相加，中转若吞掉
    `cache_creation_input_tokens`，`seen` 仍为 True，于是**返回一个偏低的部分和** ——
    而它长得和一个正常数字一模一样。

    ⭐ 探针的正确形态**不是"跑一次看看透没透传"**（那只证明了那一次），
       而是**让"字段消失"这件事永远响亮**：期望的字段里少了任何一个 → 报警 + 返回 None。
    📌 **一次性的验证证明不了一个持续的属性。**
    ⚠️ 注意 `0` 与 `缺失` 必须分开：缓存没命中时 `cache_read=0` 是**正常的**，
       字段确实在。所以判据是 `is None`（键不存在），不是 `not v`。
    """
    fields = _PROMPT_INPUT_FIELDS.get((vendor or "").lower())
    if not fields or usage is None:
        return None
    total = 0
    missing: list[str] = []
    for f in fields:
        v = getattr(usage, f, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(f)
        if v is None:
            missing.append(f)
        else:
            total += int(v)
    if missing:
        _warn_missing_fields(vendor, missing)
        return None
    return total


# ══════════════════════════════════════════════════════════════════════════
# 分布采样 —— **per-model 配额标定的原料**
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️⚠️ 「per-model 配额（L0/L1/L2 各占窗口多少）」**没法今天拍脑袋定**，
#    它要看真实使用中上下文厚度的分布。而这正是项目自己的纪律：
#      「先 shadow 量出那 61 分钟泄漏再动手」／「先量 DOM 节点数再决定要不要虚拟滚动」／
#      **「启发式的阈值必须先量出分布再定」**。
#
# 📌 所以计量层的收尾不是"把配额定下来"，而是**让那份数据真的开始积累** ——
#    否则等到要定阈值时，手上仍然只有直觉。
# ⚠️ 日志里其实每条都有，但 GBK 控制台 + 轮转让它不可查询；一行 JSON 便宜得多。
_SAMPLE_MAX = 5000          # 满了就丢最老的：这是**分布**，不是账本，不需要全量
_sample_lock = threading.Lock()


def samples_path():
    import pathlib
    p = pathlib.Path(__file__).parent.parent.parent / "data" / "context_samples.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _sample(model: str, actual: int, predicted: int | None) -> None:
    """记一条观测。**永不抛** —— 它是原料，不是正确性依赖。"""
    try:
        import json as _json
        line = _json.dumps({"t": round(time.time()), "m": model, "a": int(actual),
                            "p": (int(predicted) if predicted is not None else None)},
                           ensure_ascii=False)
        with _sample_lock:
            p = samples_path()
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            # ⚠️ 便宜的截断：只在明显超量时重写，不每次都数行数。
            if p.stat().st_size > _SAMPLE_MAX * 90:
                keep = p.read_text(encoding="utf-8").splitlines()[-_SAMPLE_MAX:]
                p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# ⭐⭐⭐ 「上次量到多少」—— **跨重启存活，专供 UI**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 2026-08-14 提出的问题：「启动时上下文卡显示 `--`，必须先聊几句才能算出来，
#    这不太合理吧」，并给出两条理由，**第二条是决定性的**：
#      ① Nano 的定位是数字生命，**它是连续的**；每次打开显示 `--`
#         给人一种"每次打开是新 nano"的感觉。
#      ② **唯一的手动清理入口是「重置对话」按钮** —— 所以打开→关闭→再打开，
#         这个数值**没有理由变**。关闭时的值就该等于开启时的值。
#
# ⚠️⚠️ **但这不等于让锚跨重启存活。** 两者是两件事，混起来会破坏重启重建：
#
#     _anchor      给【预测】用 —— 绑 runtime_id，**重启必须失效**
#                  （重启后 memory 会 hydrate + 丢坏工具对 + 截断，
#                    上一个 runtime 的 48K 不该当精确锚）
#     last_known   给【显示】用 —— 落盘，重启后照常显示，直到本次运行量到真值
#
# 📌 **同一个数字，服务于两个目的时，就该有两条命** ——
#    这正是本轮反复在用的那条（模型侧 vs 用户侧、上下文 vs 历史）。
# ⭐ 而且这不是新发明：模块头早写着「估算器降级为**替补**，只在锚失效时顶一次」，
#    **启动恰恰就是一个锚失效的时刻** —— 设计里本来就有这一格，只是 UI 没用它。
_LAST_KNOWN_LOCK = threading.Lock()


def _last_known_path():
    import pathlib
    p = pathlib.Path(__file__).parent.parent.parent / "data" / "context_last_known.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_last_known() -> dict:
    try:
        import json as _j
        return _j.loads(_last_known_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_last_known(d: dict) -> None:
    try:
        import json as _j
        _last_known_path().write_text(_j.dumps(d, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    except Exception:
        pass


def last_known(model: str) -> dict:
    """`{"actual": N|None, "floor": M|None}` —— UI 在没有锚时拿它顶。

    ⚠️ `actual` 是**上一次真正量到的整段上下文**；`floor` 是 system+工具表
       （不含历史）。重置对话之后应当显示 `floor` —— 📌 因为那时上下文
       **并不是 0**：system prompt 和工具 schema 一直在那儿。
    """
    e = _read_last_known().get(model) or {}
    return {"actual": e.get("actual"), "floor": e.get("floor")}


def forget_conversation_size() -> None:
    """「重置对话」时调用：**只忘掉历史那部分，底噪留着。**

    📌 重置之后上下文回到底噪，不是回到 0 —— 显示 0 是一句谎话。
    """
    d = _read_last_known()
    for k in list(d):
        if isinstance(d[k], dict):
            d[k].pop("actual", None)
    _write_last_known(d)
    logger.info("[ContextMeter] 对话已重置 → 忘掉历史厚度，保留底噪")


_warned_missing: set[tuple[str, ...]] = set()


def _warn_missing_fields(vendor: str, missing: list[str]) -> None:
    """字段缺失只报一次（同一组合）。

    ⚠️ **warn-once 而不是每次都报**：这个函数每次请求都跑，
       📌 **每次都刷的告警会变成噪音，恰好毁掉"响亮"这件事本身。**
       （同 `usage.load_config` 那条废弃告警刚踩过的坑。）
    """
    key = (vendor, *sorted(missing))
    if key in _warned_missing:
        return
    _warned_missing.add(key)
    logger.error(
        f"[ContextMeter] 🔴 {vendor} 的 usage **少了字段** {missing} —— "
        f"锚不更新（返回 None，不返回部分和）。"
        f"走中转时这正是「字段被吞掉」的形状：部分和长得和正常数字一模一样，"
        f"会让上下文被系统性低估、该压时以为没超。请核对中转是否透传全部字段。"
    )


# ══════════════════════════════════════════════════════════════════════════
# 本地估算器 —— **只要求前后一致，不要求准**
# ══════════════════════════════════════════════════════════════════════════

def estimate_text(s: str) -> int:
    """字符 → token 的粗估。CJK 约 1.5 字符/token，拉丁约 4 字符/token。

    ⚠️ **它的绝对精度不重要**（见模块头）：我们只用 `estimate(B) − estimate(A)`，
       系统性偏差在相减时抵消。**它唯一的要求是对自己前后一致** ——
       所以这个函数**不许按模型/厂商分叉**，那会让两次快照不可比。
    """
    if not s:
        return 0
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ")
    return int(cjk / 1.5 + (len(s) - cjk) / 4) + 1


def estimate_request(system: Any, messages: Any, tools: Any) -> int:
    """对**最终待发送的 request** 估一个值。

    ⚠️⚠️ **收口点是 provider 里「request 形状已定、真正发出去之前」那一刻**，
       不是 `MemoryManager`。因为 provider 还会再变形三次（见模块头）。
       📌 与成本闸下沉到 provider 是同一条经验：**别在十个上游各补一次。**

    ⚠️ 图片按固定权重记：base64 长度与 token 数关系完全不同于文本，
       按字符估会把结果打歪一个数量级。这里只求**一致**（同一张图两次估同一个值）。
    """
    def _walk(o: Any) -> int:
        if o is None:
            return 0
        if isinstance(o, str):
            return estimate_text(o)
        if isinstance(o, dict):
            # 图片/文档块：不按 base64 长度估
            if o.get("type") in ("image", "document"):
                return _IMAGE_TOKENS
            return sum(_walk(v) for k, v in o.items() if k != "cache_control")
        if isinstance(o, (list, tuple)):
            return sum(_walk(x) for x in o)
        return 0
    return _walk(system) + _walk(messages) + _walk(tools)


_IMAGE_TOKENS = 1600   # 量级对即可（Anthropic 一张中等图约 1~2K）


# ══════════════════════════════════════════════════════════════════════════
# 锚
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class _Anchor:
    runtime_id: str
    vendor: str
    model: str
    lane: str
    actual: int          # API 回报的真值
    estimate: int        # 那次请求的本地估值
    at: float


class ContextMeter:
    """上下文厚度计量。**进程内单例**（见文件末尾 `get_meter()`）。"""

    # 残差双门限：小 prompt 的噪声不该报警，所以**相对与绝对必须同时越线**。
    # 🔬 具体数值随 per-model 配额一起在实际运行中标定，现在别拍死。
    RESIDUAL_ABS = 3000
    RESIDUAL_REL = 0.20

    def __init__(self) -> None:
        # ⚠️ 有锁：Subagent 会并发调用 provider。
        #    📌 `UsageTracker` 就是因为没锁而在Subagent场景下有隐患 ——
        #       这里不重复那个错。
        self._lock = threading.RLock()
        self._anchor: _Anchor | None = None
        self._last_predicted: int | None = None
        self._degraded_reason: str = ""

    # ── 发请求之前 ────────────────────────────────────────────────────────
    def predict(self, *, vendor: str, model: str, lane: str,
                system: Any, messages: Any, tools: Any) -> tuple[int | None, int]:
        """返回 `(预测的输入 token, 本次本地估值)`。

        锚不可用时返回 `(None, est)` —— **调用方必须把 None 当成"我不知道"，
        不许当成 0**。📌 见 `normalize_prompt_input` 那条同源说明。
        """
        est = estimate_request(system, messages, tools)
        with self._lock:
            a = self._anchor
            if a is None or not self._same_lane(a, vendor, model, lane):
                return None, est
            return max(0, a.actual + (est - a.estimate)), est

    @staticmethod
    def _same_lane(a: _Anchor, vendor: str, model: str, lane: str) -> bool:
        """锚只对**同 runtime + 同厂商 + 同模型 + 同 lane** 有效。

        🔴 四个维度缺一不可：
          · `runtime_id` —— 重启后 memory 会 hydrate + 丢坏工具对 + 截断，
            图片可能已占位符化；**上一个 runtime 的 48K 不该当精确锚**
          · `model` —— 切模型不只是 tokenizer 变：**Haiku 会自动剥离历史 thinking，
            而 Sonnet 4.6+ 保留并重新计入 input** → 上下文组成规则都变了
          · `lane` —— 见模块头那条锚污染
        """
        try:
            from core.runtime.identity import current_runtime_id
            rid = current_runtime_id()
        except Exception:
            rid = a.runtime_id
        return (a.runtime_id == rid and a.vendor == vendor
                and a.model == model and a.lane == lane)

    # ── 请求成功之后 ──────────────────────────────────────────────────────
    def observe(self, *, vendor: str, model: str, lane: str,
                usage: Any, local_estimate: int) -> None:
        """用真值刷新锚，并做残差自检。

        ⚠️⚠️ **只有 `lane == MAIN_REACT` 会更新锚**，其余 lane 只做记录。
        ⚠️ **只有成功取得 usage 的请求才走到这里** —— 用户中途停 stream 时
           provider 走不到 `get_final_message()`，压根没有 usage。
           被取消 / 网络中断 / API 异常都不许假设刷新，只是让锚变旧
           （snapshot-delta 结构下这不破坏正确性，只是精度下降）。
        """
        actual = normalize_prompt_input(usage, vendor)
        if actual is None:
            logger.warning(
                f"[ContextMeter] 拿不到 {vendor} 的 prompt input 字段 —— "
                f"锚不更新（⚠️ 中转吃掉字段时正是这个形状，不许当成 0）"
            )
            return
        if lane != MAIN_REACT:
            return

        # ⭐⭐⭐ 残差看门狗 —— 本层最有价值的一条，几乎零成本。
        #
        # 📌 **EMA 会把 bug 学进去；残差看门狗会告诉你「系统里出现了一个
        #    你没建模的成分」。** 这正是这一层唯一目标（不怕不准，怕静默失真）的正解。
        # ⚠️ 双门限：相对与绝对**同时**越线才报，避免小 prompt 的噪声刷屏。
        with self._lock:
            pred = self._last_predicted
            if pred is not None:
                abs_err = abs(actual - pred)
                rel_err = abs_err / max(actual, 1)
                if abs_err > self.RESIDUAL_ABS and rel_err > self.RESIDUAL_REL:
                    self._degraded_reason = (
                        f"predicted={pred} actual={actual} residual={rel_err:.1%}")
                    logger.error(
                        f"[ContextMeter] DEGRADED —— {self._degraded_reason} "
                        f"model={model} lane={lane}。"
                        f"计量模型里出现了它没理解的新成分，锚已作废。"
                    )
                    self._anchor = None
                    self._last_predicted = None
                    return
                self._degraded_reason = ""
                # ⭐ 正常路径也留一行 —— 它是「上下文厚度」监控卡的数据源，
                #    也是标定 per-model 配额时唯一能看的东西。
                logger.info(
                    f"[ContextMeter] predicted={pred} actual={actual} "
                    f"residual={rel_err:.1%} model={model}"
                )
            else:
                logger.info(f"[ContextMeter] 建锚 actual={actual} model={model}（此前无锚）")

            try:
                from core.runtime.identity import current_runtime_id
                rid = current_runtime_id()
            except Exception:
                rid = ""
            self._anchor = _Anchor(rid, vendor, model, lane, actual, local_estimate, time.time())
            self._last_predicted = None
        # ⚠️ 落在锁**外面**：写盘不该拿着计量的锁（Subagent 会并发进来）。
        _sample(model, actual, pred)
        with _LAST_KNOWN_LOCK:
            d = _read_last_known()
            e = d.setdefault(model, {})
            e["actual"] = int(actual)
            e["t"] = round(time.time())
            _write_last_known(d)

    def _note_floor(self, model: str, floor: int) -> None:
        """记下「底噪」= system + 工具表（不含历史）。**永不抛。**

        ⚠️ 只在明显变化时写盘（>5%）：这个值每轮都算，但它几乎不变
           —— 📌 一个几乎不变的值不值得每轮写一次盘。
        """
        try:
            with _LAST_KNOWN_LOCK:
                d = _read_last_known()
                e = d.setdefault(model, {})
                old = e.get("floor")
                if old and abs(int(floor) - int(old)) <= max(1, int(old) * 0.05):
                    return
                e["floor"] = int(floor)
                _write_last_known(d)
        except Exception:
            pass

    def note_prediction(self, predicted: int | None) -> None:
        """记下本次预测值，供下一次 `observe()` 做残差自检。"""
        with self._lock:
            self._last_predicted = predicted

    # ── 只读视图 ─────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            a = self._anchor
            return {
                "has_anchor": a is not None,
                "actual": a.actual if a else None,
                "model": a.model if a else None,
                "degraded": self._degraded_reason or None,
            }


_meter: ContextMeter | None = None
_singleton_lock = threading.Lock()


def get_meter() -> ContextMeter:
    global _meter
    if _meter is None:
        with _singleton_lock:
            if _meter is None:
                _meter = ContextMeter()
    return _meter
