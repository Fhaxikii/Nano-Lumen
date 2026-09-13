"""硬窗口守卫 —— **启发式底下那条确定性的兜底**。

═══ 它和阶梯回答的是两个不同的问题 ═══

    衰减阶梯     = 正常治理策略 = 「什么时候该开始逐渐遗忘」 = **启发式**
    Window Guard = 请求合法性不变量 = 「这次请求到底能不能发」 = **硬边界**

📌 **阶梯不能承担窗口合法性这一硬不变量。**
   同 12K 单工具安全阀那个思想：**正常机制负责质量，最后一道硬闸负责
   「再差也别直接撞墙」。**
⚠️ 配额是拍脑袋定的（还没标定），而 400 是确定性的 ——
   📌 **一个启发式的机制，底下必须垫一个确定性的兜底。**

═══ 🔴 Guard 只能强制「安全降级」，不能强制「语义删除」 ═══

反例：

    用户：「按刚才说的那个方案，把生产配置改掉。」
    当前消息很短，真正的关键约束在 30 轮前。
    Guard：「最老，删。」
    → API 成功了，**但 Nano 会自信地按错误约束去操作真实电脑。**

📌 **窗口溢出允许导致本次请求失败，不允许导致不受控的语义删除。**
⚠️ 尤其 Nano 是能操作真实电脑的 Agent —— 这不是理论风险。

═══ 🔴 必须区分两种超限 ═══

    可通过回收旧上下文解决        → emergency decay
    **当前这一轮本身就装不下**    → **直接告诉用户**

例：用户一次粘 300K 而模型只有 200K —— 历史删光也没用。
⚠️ **绝不能为了满足窗口去截用户刚发来的正文尾巴**：
   「上传合同 → 后 20% 被静默截掉 → Nano 给出『完整审查结果』」
   是最差的失败形状。

═══ ⚠️ 「确定性」还要再分一层 ═══

`meter.predict()` **不是精确 count**，它是「上次真值锚 + 两次本地估算之差」。
所以不能写 `if predicted > window` 然后管它叫确定性。
→ **便宜预测做快速筛查，红区才值得付一次精确 count**（本层留出了那个钩子）。
"""
from __future__ import annotations

from loguru import logger

# 判定为「红区」的比例：越过它才值得付一次精确计量 / 才启动紧急降级。
# ⚠️ 比 CRITICAL(0.85) 更高 —— 📌 Guard 是最后一道闸，正常情况下阶梯早就动过了；
#    它频繁触发本身就是「配额失准」的信号，不该被当成常态路径。
RED_ZONE = 0.92

# 给本轮输出留的空间（占窗口比例）。
# ⚠️ 上限**不是** `input < context_window` —— 即使接口愿意收，
#    Nano 自己也得有地方生成。📌 一个刚好塞满输入的请求，等于没有回答。
OUTPUT_RESERVE = 0.06


def admissible_input(window: int) -> int:
    """本次请求真正允许的 prompt 上限。"""
    return max(1, int(window * (1.0 - OUTPUT_RESERVE)))


def preflight(model_id: str, predicted: int | None) -> dict:
    """发请求前的快速筛查。**永不抛。**

    返回 `{"ok", "reason", "predicted", "limit", "ratio"}`。
    ⚠️ `predicted is None`（本次运行还没量到）→ **放行** ——
       📌 一个「我不知道」不该被当成「超了」：那会在每次重启后第一句话就拦人，
          而那时上下文通常恰恰是最短的。
    """
    out = {"ok": True, "reason": "", "predicted": predicted, "limit": 0, "ratio": 0.0}
    try:
        from core.models import window_of
        _w = int(window_of(model_id) or 0)
        if _w <= 0 or predicted is None:
            return out
        limit = admissible_input(_w)
        out["limit"] = limit
        out["ratio"] = float(predicted) / _w
        if predicted <= limit:
            return out
        out["ok"] = False
        out["reason"] = "over_limit"
        logger.error(
            f"[Guard] 🔴 预测输入 {predicted} > 允许上限 {limit}"
            f"（窗口 {_w}，已为输出预留 {int(OUTPUT_RESERVE*100)}%）"
        )
    except Exception as e:
        logger.debug(f"[Guard] preflight 跳过: {e}")
    return out


def in_red_zone(model_id: str, predicted: int | None) -> bool:
    """够不够格启动紧急降级 / 精确计量。"""
    try:
        from core.models import window_of
        _w = int(window_of(model_id) or 0)
        return bool(_w > 0 and predicted is not None and predicted / _w >= RED_ZONE)
    except Exception:
        return False


# 用户可见文案。⚠️ **说清是哪一种超限**，因为两种的处置完全不同。
# 📌 一条只说「太长了」的错误，会让用户去删历史（没用）而不是拆分本次输入。
MSG_ACTIVE_TOO_BIG = (
    "这一次的输入本身就超过了当前模型能安全处理的上下文容量 —— "
    "把更早的历史全部回收也装不下。\n"
    "请把这次的内容拆小一点，或者切换到上下文窗口更大的模型。"
)
MSG_STILL_TOO_BIG = (
    "上下文已经接近当前模型的硬上限。Nano 已经尽量收缩了较早的历史，"
    "但仍然装不下这次请求。\n"
    "请缩短本次输入，或者切换到上下文窗口更大的模型。"
)


class ContextWindowExceeded(RuntimeError):
    """**这一次装不下，拒绝发送。**

    ⚠️ 它带着 `kind` 与 `user_message` —— 因为两种超限的**处置完全不同**：
        `active_too_big`  这一轮本身就装不下 → 回收多少历史都没用
        `still_too_big`   已经紧急回收过，仍然装不下
    📌 一条只说「太长了」的错误，会让用户去删历史（没用）
       而不是拆分本次输入。

    🔴 **为什么是抛异常而不是"尽力发出去"**：
       provider 这一层是**唯一**知道最终 request 形状的地方，
       而它没有权力去删用户的历史。能做的只有两件：发，或者说发不了。
       📌 **一个没有拒绝权的守卫，本质上只是一行日志。**
    """

    def __init__(self, kind: str, user_message: str,
                 predicted: int = 0, limit: int = 0):
        super().__init__(f"{kind}: predicted={predicted} > limit={limit}")
        self.kind = kind
        self.user_message = user_message
        self.predicted = predicted
        self.limit = limit


def classify(model_id: str, predicted: int | None, history_tokens: int) -> str:
    """区分两种超限。返回 `"ok" / "recoverable" / "active_too_big"`。

    🔴 **这个区分是 Guard 最重要的产出**：
       前者可以靠回收旧上下文解决，后者**只能告诉用户** ——
       而把后者当成前者，就会一路删历史、删到没得删，最后仍然失败，
       ⚠️ 却已经把用户的历史毁了。
    """
    try:
        from core.models import window_of
        _w = int(window_of(model_id) or 0)
        if _w <= 0 or predicted is None:
            return "ok"
        limit = admissible_input(_w)
        if predicted <= limit:
            return "ok"
        # 把**全部**可回收的历史都算成 0，仍然超 → 这一轮本身装不下
        if predicted - max(0, history_tokens) > limit:
            return "active_too_big"
        return "recoverable"
    except Exception:
        return "ok"
