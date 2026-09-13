import contextvars
import threading
import json
import pathlib
import datetime

_DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
_USAGE_FILE = _DATA_DIR / "usage.json"
_CONFIG_FILE = _DATA_DIR / "usage_config.json"

# ⚠️⚠️ **价格已搬进 `data/model_config.json`**（两层配置表，2026-08-13）。
#    这里只剩**用户的预算设置** —— 那是用户的意愿，不是模型的事实。
# 📌 分界线：**「这个模型多少钱」是事实，「我愿意花多少」是设置。**
#    事实归一张表（顺带带上窗口和配额），设置留在用户能改的地方。
_DEFAULT_CONFIG = {
    "enabled": True,
    "soft_cap_usd": 5.0,
    "hard_cap_usd": 10.0,
}


_WARNED_LEGACY_PRICES = False


def _price_for(model: str) -> dict:
    """问 `core.models` 要价格 —— **唯一问处**。

    ⚠️ 拿不到就是 0/0（`core.models.price_of` 的纪律）：
       📌 **计费宁可少算，也不许凭空捏一个价格** —— 编出来的价格会让预算闸
          在错误的位置上开火，而用户查不出原因。
    """
    try:
        from core.models import price_of
        return price_of(model)
    except Exception:
        return {"input_per_1m": 0.0, "output_per_1m": 0.0}



def _cur() -> str:
    """当前厂商的货币符号。⚠️ 不写死 `$` —— 深度求索官方标价是人民币，
    折算需要汇率，而汇率不在官方文档里、每天都在动。"""
    try:
        from core.models import currency_symbol
        return currency_symbol()
    except Exception:
        return "$"

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ══════════════════════════════════════════════════════════════════════════
# 「这些 token 算哪一轮的」—— **并发一引入，打点取差值就错**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 旧实现（2026-08-15 之前）：
#       begin_turn():  self._turn_base = 当前 session 总量
#       turn_tokens(): 当前 session 总量 − _turn_base
#    它是「**打点 + 取差值**」，不是「归属到某个 turn」。而按现在的定义，
#    后台任务 / Subagent**不随中断死、会活过发起它的那一轮**，于是：
#
#       turn A：起了后台任务 → begin_turn 打点在 A；任务还在跑…
#       turn B：用户问「现在几点」→ begin_turn **重新打点**
#               后台任务此刻回来，token 落进 session 总量
#               → 差值把它算进了 **turn B**        ← 🔴 记到不相干的一轮头上
#
# 📌 **一个「打点取差值」的计数器，隐含假设是「这段时间里只有它自己在花」**
#    —— 并发一引入这个假设就没了，**而它不会报错**。
#
# ⭐ 正解是 `ContextVar`：`asyncio.create_task` 会**在创建那一刻复制上下文**，
#    所以后台任务/Subagent天然带着**发起它的那一轮**的 id 跑，晚多久回来都算对。
#    ⚠️ 这不是"用个高级特性"，而是这件事的形状本来就是**上下文继承**：
#       📌 归属应该跟着「谁发起的」走，而不是跟着「什么时候回来的」走。
_turn_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "nano_usage_turn", default="")

# 保留多少轮的账。⚠️ 只为**防无界增长**：UI 只看当前轮，历史轮由 usage.json 管。
_TURN_KEEP = 64

# Subagent用量的归属上下文。⭐ 与 `_turn_ctx` 同一个机制、不同的问题。
_agent_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "nano_usage_agent", default="")


class UsageTracker:
    def __init__(self):
        # 🔴 `self._x += n` 是**读-改-写**，Subagent 并发时会丢更新。
        #    ⭐ 对照：`RecentSystemEvents` 和 `HealthRegistry` 都带 `RLock`，
        #       **唯独 UsageTracker 没有** ——
        #       📌 又一次「一个正确做法已经在代码里存在、却没被推广到同类场景」。
        #    ⚠️ 锁**必须也罩住 usage.json 的读-改-写**（`_load → += → _save`）：
        #       那一段比内存计数器更危险，丢的是**落盘的钱**。
        self._lock = threading.RLock()
        self._session_input = 0
        self._session_output = 0
        self._turn_acc: dict[str, int] = {}   # turn_id -> 该轮累计 fresh+output
        self._turn_order: list[str] = []
        # 每轮的 cache hit 分子/分母。⚠️ 跟 `_turn_acc` 一样是**内存态**，
        # 重启即失 —— 它只服务"这条消息花了多少"，不参与计费。
        self._turn_cache: dict[str, list[int]] = {}
        # 🔴 **读侧要的那个"当前轮"** —— 与 `ContextVar` 分工不同，不是冗余：
        #    · `_turn_ctx`（ContextVar）答「**这笔账算谁的**」——
        #      它必须跟着调用链走，后台任务才能把账记回发起它的那一轮。
        #    · `_current_turn`（实例属性）答「**用户现在在第几轮**」——
        #      读它的人（UI）在**另一个协程**里，那里 ContextVar 天然是空的。
        # 📌 **归属需要上下文；"当前是哪一轮"是个全局问题。
        #    用同一个机制回答两个问题，其中一个一定会在某个调用点上失效。**
        # ⚠️ 这正是 2026-08-15 引入的那个回归：改成纯 ContextVar 之后，
        #    UI 那条 `12s · 2.2K tok` 消失了 —— 因为 UI 的事件消费协程
        #    从来没被 `begin_turn()` set 过。
        #    ⚠️⚠️ 而当时"实测验证"看到页面上有个数字就算过了 ——
        #       那个数字是上下文圆环，不是这个计数器。
        #       📌 **验证时找到「一个看起来对的东西」，不等于验证了「那个东西」。**
        self._current_turn: str = ""
        # Subagent 用量：job_id -> 累计（抽屉里的 `27.3k tok`）
        self._agent_acc: dict[str, int] = {}

    def reset_session(self):
        with self._lock:
            self._session_input = 0
            self._session_output = 0
            self._turn_acc.clear()
            self._turn_order.clear()
            self._agent_acc.clear()
            self._current_turn = ""

    def session_tokens(self) -> tuple[int, int]:
        with self._lock:
            return self._session_input, self._session_output

    def begin_turn(self) -> str:
        """在 orchestrator 处理一条用户消息的最开始调用（本轮任何模型调用之前）。

        返回这一轮的 id；同时把它写进 `ContextVar` ——
        ⚠️ **之后在这个协程里 `create_task` 出去的东西会继承它**，
           那正是后台任务/Subagent把账记回正确那一轮的机制。
        """
        import uuid as _uuid
        tid = _uuid.uuid4().hex[:12]
        _turn_ctx.set(tid)
        with self._lock:
            self._current_turn = tid
            self._turn_acc[tid] = 0
            # 每轮的 cache 分子/分母 —— 跟 `_turn_acc` 同生同灭（同一套淘汰规则）。
            self._turn_cache[tid] = [0, 0]      # [cached, gross]
            self._turn_order.append(tid)
            while len(self._turn_order) > _TURN_KEEP:
                _old = self._turn_order.pop(0)
                self._turn_acc.pop(_old, None)
                self._turn_cache.pop(_old, None)
        return tid

    def _attribute(self, n: int) -> None:
        """把 n 记到**当前上下文那一轮**头上。调用方必须已持锁。"""
        _aid = _agent_ctx.get("")
        if _aid and _aid in self._agent_acc:
            self._agent_acc[_aid] += n
        tid = _turn_ctx.get("")
        if not tid:
            # ⚠️ 上下文里没有轮次 = 这次调用不在任何一轮里（启动期探针 / 后台
            #    维护）。**不许硬塞给最近一轮** —— 📌 那会把「不属于任何一轮」
            #    伪装成「属于这一轮」，而 UI 上看起来完全正常。
            return
        if tid in self._turn_acc:
            self._turn_acc[tid] += n

    def _attribute_cache(self, cached: int, gross: int) -> None:
        """把这次调用的 cache 分子/分母记到当前那一轮。调用方必须已持锁。

        ⚠️ 与 `_attribute` 同一条纪律：**上下文里没有轮次就不记**，
           不许硬塞给最近一轮 —— 那会把"不属于任何一轮"伪装成"属于这一轮"。
        """
        tid = _turn_ctx.get("")
        if not tid or tid not in self._turn_cache:
            return
        self._turn_cache[tid][0] += cached
        self._turn_cache[tid][1] += gross

    def turn_cache_hit(self, turn_id: str = "") -> float | None:
        """这一轮的 cache hit 率（0~1）。没量到 → None（**不是 0**）。"""
        tid = turn_id or self._current_turn
        pair = self._turn_cache.get(tid)
        if not pair or pair[1] <= 0:
            return None
        return pair[0] / pair[1]

    def turn_tokens(self, turn_id: str = "") -> int:
        """这一轮花了多少。**不再是差值。**

        ⚠️ 取哪一轮，三级：显式参数 > 当前协程的归属上下文 > **读侧的当前轮**。
           📌 最后那一级是给 UI 的 —— 它在另一个协程里，ContextVar 是空的。
        """
        with self._lock:
            tid = turn_id or _turn_ctx.get("") or self._current_turn
            return max(0, int(self._turn_acc.get(tid, 0)))

    # ── Subagent的用量：抽屉里那个 `27.3k tok` ──────────────────────
    #
    # ⭐ 复用同一个 `ContextVar` 机制：Subagent循环开始时 set 一次 job_id，
    #    它这一支里的所有模型调用**自动**记到它头上。
    # ⚠️ 与 turn 归属**并存不冲突**：同一笔 token 既算它那一轮的（main agent 的账），
    #    也算那个 Subagent 的（监控用）。
    #    📌 定的是「Subagent 的 token 与 main agent 叠在一起显示，别在 agent 页另起计数器」——
    #       这里没有另起：`_agent_acc` 不参与任何总量，它只回答
    #       「**这一个Subagent花了多少**」，那是 turn 总量答不了的问题。

    def begin_agent(self, job_id: str) -> None:
        _agent_ctx.set(job_id or "")
        with self._lock:
            self._agent_acc.setdefault(job_id, 0)

    def agent_tokens(self, job_id: str) -> int:
        with self._lock:
            return max(0, int(self._agent_acc.get(job_id, 0)))

    def turn_tokens_fmt(self) -> str:
        return _fmt_tokens(self.turn_tokens())

    def session_tokens_fmt(self) -> str:
        with self._lock:
            total = self._session_input + self._session_output
        return _fmt_tokens(total)

    def load_config(self) -> dict:
        if _CONFIG_FILE.exists():
            try:
                data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
                cfg = dict(_DEFAULT_CONFIG)
                cfg.update({k: v for k, v in data.items() if k != "model_prices"})
                # ⚠️⚠️ 老文件里残留的 `model_prices` —— **响亮地忽略，不静默择一**。
                # 📌 两个权威打架时，"安静地挑一个"是最坏的处置：
                #    用户会以为自己改的价生效了，而账单按另一份算。
                # ⚠️ **只说一次。** 实测这个函数一轮被调四次（`cap_status`
                #    每次请求都问它），于是同一句告警一轮刷四条。
                #    📌 **一条每次都刷的告警会变成噪音，恰好毁掉"响亮"这件事本身** ——
                #       与上下文压力段"低于高水位一个字都不说"同一条纪律。
                global _WARNED_LEGACY_PRICES
                if "model_prices" in data and not _WARNED_LEGACY_PRICES:
                    _WARNED_LEGACY_PRICES = True
                    from loguru import logger as _lg
                    _lg.warning(
                        "[Usage] usage_config.json 里的 `model_prices` **已废弃且被忽略** —— "
                        "价格现在只认 data/model_config.json（两层配置表）。"
                        "要改价请改那个文件，并把这一段删掉以免下次又困惑。"
                    )
                return cfg
            except Exception:
                pass
        return dict(_DEFAULT_CONFIG)

    def save_config(self, cfg: dict):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _current_vendor() -> str:
        """当前厂商。读不到就返回空串（**绝不因为这个崩** —— 它只是账本的一个维度）。"""
        try:
            from core.provider import provider as _p
            v = getattr(_p, "vendor", "") or ""
            if v:
                return v
            from core.models import vendor_of
            return vendor_of(getattr(_p, "target_model", "") or "")
        except Exception:
            return ""

    def _load_usage(self) -> dict:
        """今日账本。**日期或厂商任一对不上就归零。**

        ⭐ 「跨天归零」这个机制本来就在（比 `date`）——换厂商只是给它加一维，
           而不是新写一条"重置流程"。
           📌 一个已经在跑的失效判据，**加一维比加一条路径安全**。

        🔴 为什么换厂商要清账而不是换算：
           深度求索官方标价是**人民币**，Anthropic 是**美元**。要合并就得有汇率——
           而汇率不在任何一家的官方文档里、且每天在动。
           ⇒ **换算是在制造一个我们无法验证的数字；清账是承认"这是两笔账"。**
        ⚠️ 归零前把旧厂商那笔写进日志：想查还查得到，只是不再参与今日限额。
        """
        today = datetime.date.today().isoformat()
        vendor = self._current_vendor()
        if _USAGE_FILE.exists():
            try:
                data = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
                _same_day = data.get("date") == today
                # ⚠️ 老账本没有 vendor 字段 —— 那是升级前写的，**不算换厂商**
                #    （否则所有老用户第一次启动就被清一次账）。
                _old_v = data.get("vendor")
                _same_vendor = (_old_v is None) or (_old_v == vendor)
                if _same_day and _same_vendor:
                    if _old_v is None:
                        data["vendor"] = vendor      # 补上，之后就能比了
                    return data
                if _same_day and not _same_vendor:
                    logger.info(
                        f"[Usage] 厂商从 {_old_v!r} 切到 {vendor!r} —— 今日账本清零。"
                        f"旧厂商今日已用 {data.get('cost_usd', 0.0):.4f}"
                        f"（{data.get('input_tokens', 0)} in / "
                        f"{data.get('output_tokens', 0)} out）")
            except Exception:
                pass
        return {"date": today, "vendor": vendor,
                "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                # cache hit 率的两个分子分母。⭐ 它是「压 fresh 才是压钱」那条
                # 优化判据的量尺 —— 不是给某家厂商加的指标。
                "cached_tokens": 0, "gross_input_tokens": 0}

    def _save_usage(self, data: dict):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _USAGE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record(self, input_tokens: int, output_tokens: int, model: str) -> float:
        p = _price_for(model)
        cost = (input_tokens * p["input_per_1m"] + output_tokens * p["output_per_1m"]) / 1_000_000

        # ⚠️ 锁罩住**整段读-改-写**，内存与落盘一起 —— 见 `__init__` 的注释。
        with self._lock:
            data = self._load_usage()
            data["input_tokens"] += input_tokens
            data["output_tokens"] += output_tokens
            data["cost_usd"] = round(data["cost_usd"] + cost, 6)
            self._save_usage(data)
            self._session_input += input_tokens
            self._session_output += output_tokens
            self._attribute(input_tokens + output_tokens)
        return cost

    def record_detailed(self, fresh: int, cache_read: int, cache_creation: int,
                        output_tokens: int, model: str) -> float:
        """带 prompt 缓存的准确记账。
        - 成本按真实费率：fresh 全价 / cache_read 0.1x / cache_creation 1.25x / output 出价。
        - session + usage.json 的 input 只累计 **fresh**（本轮真正全价处理的新 token）——
          这样 UI 单条 token 显示的是"本轮真花的量"，不把便宜的缓存读按满价怼给用户看
          （否则一个多轮工具任务会显示 18K 这种吓人的 gross）。
        """
        p = _price_for(model)
        in_rate = p["input_per_1m"]
        cost = (
            fresh * in_rate
            + cache_read * in_rate * 0.1
            + cache_creation * in_rate * 1.25
            + output_tokens * p["output_per_1m"]
        ) / 1_000_000

        with self._lock:
            data = self._load_usage()
            data["input_tokens"] += fresh
            data["output_tokens"] += output_tokens
            data["cost_usd"] = round(data["cost_usd"] + cost, 6)
            # ⚠️ 分母是 gross（fresh + 读缓存 + 写缓存），不是 fresh ——
            #    命中率问的是"送进去的东西里有多少不用重算"。
            data["cached_tokens"] = data.get("cached_tokens", 0) + cache_read
            data["gross_input_tokens"] = (
                data.get("gross_input_tokens", 0) + fresh + cache_read + cache_creation)
            self._save_usage(data)
            self._session_input += fresh
            self._session_output += output_tokens
            self._attribute(fresh + output_tokens)
            self._attribute_cache(cache_read, fresh + cache_read + cache_creation)
        return cost

    def today_cache_hit(self) -> float | None:
        """今日 cache hit 率（0~1）。还没有任何请求 → None（**不是 0**）。

        📌 「我不知道」渲染成 0% 会被当成真数据 —— 同 `budget.snapshot` 那条纪律。
        """
        d = self._load_usage()
        gross = d.get("gross_input_tokens", 0) or 0
        if gross <= 0:
            return None
        return (d.get("cached_tokens", 0) or 0) / gross

    def today_cost(self) -> float:
        return self._load_usage().get("cost_usd", 0.0)

    def today_input_output(self) -> tuple[int, int]:
        data = self._load_usage()
        return data.get("input_tokens", 0), data.get("output_tokens", 0)

    def cap_status(self) -> str:
        """Returns 'ok', 'soft', or 'hard'."""
        cfg = self.load_config()
        if not cfg.get("enabled", True):
            return "ok"
        cost = self.today_cost()
        if cost >= cfg.get("hard_cap_usd", 10.0):
            return "hard"
        if cost >= cfg.get("soft_cap_usd", 5.0):
            return "soft"
        return "ok"


usage_tracker = UsageTracker()


# ══════════════════════════════════════════════════════════════════════════
# 成本硬上限的统一闸门
# ══════════════════════════════════════════════════════════════════════════
# 闸在 provider 的客户端包装层（见 provider._BudgetGuardedMessages），不在这里。
# 这里只提供【判断】与【状态同步】，不负责拦截——判断逻辑必须只有一份。
# ══════════════════════════════════════════════════════════════════════════

class BudgetExceeded(RuntimeError):
    """今日用量已达硬上限。由 provider 的客户端包装层统一抛出。

    为什么是异常而不是"结构化的已达上限返回值"：哨兵值会被调用方当成正常输出
    继续用下去。最具体的例子是语义记忆抽取——它把模型返回的内容直接往
    semantic_memories 表里写，一个"已达上限"的哨兵会被当成一条记忆存进数据库，
    污染的是持久数据。异常至少是响亮的失败。
    """

    def __init__(self, cost: float, cap: float):
        self.cost = cost
        self.cap = cap
        super().__init__(f"今日用量 {_cur()}{cost:.2f} 已达上限 {_cur()}{cap:.2f}")


def sync_budget_health() -> str:
    """把当前预算状态同步进 HealthRegistry，返回 cap_status()。

    为什么预算要登记成一个 Capability：
    "预算耗尽"本质上就是一种能力不可用，而健康登记那一套已经把机制建好了。
    登记进去就白拿四样：动态段注入（Nano 自己知道）、监控卡、聊天出口、恢复通道。

    ⭐ 真正的价值在【软闸】，不在硬闸：
    硬闸拦住之后模型根本不会被调用，注入给它的话它永远看不到。
    而软闸时调用照常放行，把"今天花了多少、上限多少"注入动态段，
    Nano 就能在撞墙之前自己收敛（少绕几轮工具、别起大工程、提前告诉用户）。
    这与"上下文压力对模型可见"是同一个模式：
    给模型注入它自己的约束状态，模型会自主调整行为。

    恢复是【按日重置】的，所以它是能力探针里最简单的一个：重新算一次即可。
    """
    status = usage_tracker.cap_status()
    try:
        from core.health import Cap, report_degraded, report_fault, report_ok
    except Exception:
        # health 层不可用不该拖垮记账本身
        return status

    try:
        cfg = usage_tracker.load_config()
        cost = usage_tracker.today_cost()
        soft = cfg.get("soft_cap_usd", 5.0)
        hard = cfg.get("hard_cap_usd", 10.0)

        if status == "hard":
            report_fault(
                Cap.MODEL_BUDGET, "BUDGET_HARD_CAP",
                f"Today's spend {_cur()}{cost:.2f} has reached the hard cap {_cur()}{hard:.2f}. "
                f"All model calls are blocked until the daily counter resets or the user raises the cap.",
                hint="点右上角三个点打开设置，在「通用」里能调高上限；用量每日 0 点自动重置。",
                hint_en=("The user can raise the cap in Settings (top-right dots) under General; "
                         "the daily counter also resets at midnight."),
            )
        elif status == "soft":
            report_degraded(
                Cap.MODEL_BUDGET, "BUDGET_SOFT_CAP",
                f"Today's spend {_cur()}{cost:.2f} is past the soft cap {_cur()}{soft:.2f} "
                f"(hard cap {_cur()}{hard:.2f}, at which point all model calls stop).",
                hint="点右上角三个点打开设置，在「通用」里能调整上限。",
                hint_en="The user can adjust the cap in Settings (top-right dots) under General.",
            )
        else:
            report_ok(Cap.MODEL_BUDGET, note="budget below soft cap")
    except Exception:
        # 同步失败不影响闸门判断本身
        pass
    return status


def assert_budget_ok() -> None:
    """硬上限闸：超限抛 BudgetExceeded，否则原样返回。

    顺带做一次健康态同步——这样软闸一旦跨过就立刻登记，不用等 UI 定时器；
    HealthRegistry.report() 自带同根因去重，不会把队列刷爆。
    """
    if sync_budget_health() != "hard":
        return
    cfg = usage_tracker.load_config()
    raise BudgetExceeded(usage_tracker.today_cost(), cfg.get("hard_cap_usd", 10.0))
