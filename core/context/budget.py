"""上下文预算 —— 「现在多厚、离水位多远」。

⚠️⚠️ **这一层只观测，不衰减。** 阶梯（L0→L4 转移）是另一部分。
这里回答的全部问题是：**「该不该压」**，而且它的答案暂时**只给人看**
（监控卡）和**给模型看**（压力动态段），不驱动任何动作。

📌 为什么先做观测再做动作 —— 这是项目自己的纪律：
   「先 shadow 量出那 61 分钟泄漏再动手」／「先量 DOM 节点数再决定要不要虚拟滚动」／
   **「启发式的阈值必须先量出分布再定」**。
   ⭐ 而这一层恰好能拿到 per-model 配额标定所需的真实数据。

═══ 水位为什么是**窗口百分比**而不是绝对 token 数 ═══

Haiku 4.5 = 200K，Sonnet 4.6 / Opus 4.8 = 1M。同一个绝对阈值在两端意义完全不同：
对 1M 模型是"刚热身"，对 200K 模型已经该压了。
⚠️ 而**默认档恰好是那个 200K 的**，也就是最需要早压的那个。
📌 **凡是跨模型复用的阈值，单位必须是"占它自己的多少"，不是绝对量。**
"""
from __future__ import annotations

# ⭐ 三档水位。⚠️ 现在**只用于说话**（提示 + 监控卡），不触发任何动作，
#    所以定得保守一点没有代价 —— 📌 而反过来（等到真要压时才第一次定阈值）
#    就没有任何实测分布可参考了。计量层的产出正是那份分布。
WATERMARK_NOTICE = 0.50   # 「开始占地方了」—— 只进监控卡，不打扰模型
WATERMARK_HIGH = 0.70     # 「该开始收敛了」—— 注入给模型
WATERMARK_CRITICAL = 0.85 # 「快撞墙了」—— 注入给模型，措辞更硬

_LEVELS = ("ok", "notice", "high", "critical")


def level_for(ratio: float) -> str:
    if ratio >= WATERMARK_CRITICAL:
        return "critical"
    if ratio >= WATERMARK_HIGH:
        return "high"
    if ratio >= WATERMARK_NOTICE:
        return "notice"
    return "ok"


def snapshot(model_id: str) -> dict:
    """当前上下文预算的只读快照。**永不抛** —— 观测层不许影响主流程。

    返回 `known=False` 表示「还没量到」（本次运行第一次请求之前、
    或残差看门狗刚把锚作废）。
    ⚠️ **那种情况下什么都不要说** —— 📌 一个"我不知道"被渲染成 0%，
       比不显示更糟：它看起来像一条真数据。
    """
    out = {"known": False, "used": None, "window": None, "ratio": None,
           "level": "ok", "model": model_id, "degraded": None,
           "quota": None, "estimated": False}
    try:
        from core.context.meter import get_meter, last_known
        from core.models import quota_of, window_of
        snap = get_meter().snapshot()
        out["degraded"] = snap.get("degraded")
        out["window"] = window_of(model_id)
        out["quota"] = quota_of(model_id)
        w = out["window"] or 0
        if w <= 0:
            return out

        used = snap.get("actual")
        # ⚠️ 锚是 per-model 的：切了模型之后那个数字**不属于**当前模型，
        #    拿来除以新窗口会得到一个看起来很正常的错误比例。
        #    📌 同一条道理 —— 一个共享的数字必须带上"我是谁"。
        if used is not None and snap.get("model") in (None, model_id):
            out.update(known=True, used=int(used), ratio=float(used) / w)
            out["level"] = level_for(out["ratio"])
            return out

        # ⭐⭐⭐ 本次运行还没量到（刚启动 / 刚切模型 / 锚刚被看门狗作废）。
        #
        # 🔴 2026-08-14 提出的问题：「启动时显示 `--`，必须先聊几句才能算出来 ——
        #    **唯一的手动清理入口是「重置对话」按钮，所以打开→关闭→再打开，
        #    这个数值没有理由变**」。对。而且第一条理由同样成立：
        #    Nano 是**连续的**数字生命，每次打开显示 `--` 像换了一个 nano。
        #
        # ⚠️ 但**不是**让锚跨重启（那会破坏重启后的重建），而是用落盘的「上次量到的值」
        #    顶一格，并**明确标成估算**。📌 同一个数字服务两个目的，就该有两条命。
        # ⚠️ `actual` 没有而 `floor` 有 = 刚重置过对话 →
        #    显示底噪（system+工具表），**不是 0** —— 📌 显示 0 是一句谎话。
        lk = last_known(model_id)
        fallback = lk.get("actual") or lk.get("floor")
        if fallback:
            out.update(known=True, used=int(fallback),
                       ratio=float(fallback) / w, estimated=True)
            out["level"] = level_for(out["ratio"])
    except Exception:
        pass
    return out


def pressure_block(model_id: str) -> str:
    """**上下文压力对模型可见** —— 拼进 system 动态段的那一小段。

    ⭐ 这是成本软闸那条经验的同款应用（它自己的 docstring 就写着两者同一个模式）：
       > 「硬闸拦住之后模型根本不会被调用，注入给它的话它永远看不到。
       >   而软闸时调用照常放行，把状态注入动态段，Nano 就能在撞墙之前自己收敛。」
       📌 **给模型注入它自己的约束状态，模型会自主调整行为。**

    ⚠️ **低于高水位一个字都不说。** 一段每轮都在、又暂时没有意义的文字，
       会被模型学成背景噪音 —— 等它真的变重要时反而没人读。
       📌 同摘要那条提醒：**别让一件事"一直在说"。**
    ⚠️ 也**不报具体 token 数**：那是估算值，报出来会被当成精确事实引用
       （"你说我还有 47231 token"）。📌 **不确定的数字要以不确定的形式说出口。**
    """
    s = snapshot(model_id)
    if not s["known"] or s["level"] not in ("high", "critical"):
        return ""
    # ⚠️ **估算值不注入给模型。** UI 上标个 `~` 让用户看个大概是合理的；
    #    但对模型说"你已经用了 88%"，它会据此收敛甚至拒绝做事 ——
    #    📌 **让用户看一个估算值，和让模型据此改变行为，是两个门槛。**
    #       第一次真实计量就在下一个请求，等一轮不亏。
    if s.get("estimated"):
        return ""
    pct = int(s["ratio"] * 100)
    if s["level"] == "critical":
        return (
            "\n\n[Context pressure — critical]\n"
            f"This conversation now fills roughly {pct}% of your context window. "
            "You are close to the point where the oldest parts of it have to be dropped.\n"
            "Keep replies tight, avoid re-reading large files or dumping long tool output, "
            "and finish what is in flight rather than starting something broad. "
            "If the user is asking for something large, say plainly that it may not fit "
            "and suggest doing it in pieces.\n"
        )
    return (
        "\n\n[Context pressure]\n"
        f"This conversation now fills roughly {pct}% of your context window. "
        "Prefer shorter tool output and tighter replies; avoid reloading whole files you "
        "have already read.\n"
    )
