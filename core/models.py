"""模型事实的**唯一**问处 —— 两层配置：厂商 / 模型。

═══ 为什么合并 ═══

多模型支持的前置动作：模型的各项事实散落在各处，改一个要找好几个地方。

改造前它们散在三处：`CLAUDE_MODELS`（UI 元信息，**没有窗口**）／
`usage_config.json` 的 `model_prices`（价格）／ 提炼器选择（还没写）。

⚠️⚠️ **但粒度不一样，不能压成一层**：

    提炼器选择        per **厂商** —— 用户配了哪家的 key，就只能用那家的模型
    价格/窗口/配额     per **模型**

📌 **「主模型与提炼模型同厂」是硬约束，不是建议** —— 跨厂就意味着用户没有那把 key。

═══ 🔴 刻意【不】把 `window` 复制进 `CLAUDE_MODELS` ═══

原计划是「`CLAUDE_MODELS` 补 `context_window`」。**没照做，理由是另一条更强的判据**：

    **一个能从别处推导出来的字段不该单独存，存了就会和真值不一致** ——
    与反复在修的那类 bug（标题当身份、裸 bool 当状态）同一个形状。

窗口现在住在这张表里；`CLAUDE_MODELS` 是**给 UI 用的展示元信息**（名字/图标/tooltip）。
两边各存一份，迟早有人只改一边。→ 想知道窗口，问 `window_of()`。
⚠️ 这是一处**明知故犯的偏离**，不是漏做。

═══ ⚠️ 价格从 `usage_config.json` 搬过来了 ═══

`usage_config.json` 现在只留**用户预算设置**（`enabled` / `soft_cap_usd` / `hard_cap_usd`）。
`model_prices` 若仍存在 → **响亮地警告并忽略**，不静默择一。
📌 **两个权威打架时，"安静地挑一个"是最坏的处置** —— 用户会以为改生效了。
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

from loguru import logger

_lock = threading.Lock()
_cache: dict[str, Any] | None = None

# 表里没有这个模型时的兜底。⚠️ **保守取小**：窗口猜大了 = 该压时以为还早 = 撞墙；
# 猜小了只是压得早一点。📌 未知情况下的默认值要往"损失小的那一侧"倒。
FALLBACK_WINDOW = 200_000
_FALLBACK_QUOTA = {"L0": 0.15, "L1": 0.20, "L2": 0.15}


def _path() -> pathlib.Path:
    from core.paths import data_path
    return data_path("model_config.json")


def load(force: bool = False) -> dict[str, Any]:
    """读表。⚠️ 读不到就返回空表 —— 各访问器自己有兜底，**绝不因为配置缺失而崩**。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
            _cache = {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception as e:
            logger.warning(f"[Models] 读取 model_config.json 失败，使用兜底: {e}")
            _cache = {}
        return _cache


def setting(name: str, default=None):
    """读一条**非厂商**的设置（住在 `_settings` 里）。

    🔴 **为什么设置不能直接放顶层**（2026-08-14 当场踩到）：
       `load()` 返回的是「厂商表」，只过滤 `_` 开头的键。
       把 `ladder_enabled: false` 放顶层，`vendor_of()` 就会把这个 bool
       当成一个厂商去 `.get("models")` —— **AttributeError 当场炸**。
    📌 **一张表里混进一个不同种类的键，消费方就会把它当同类。**
       ⭐ 好在这次炸得响亮；如果那个值恰好是个 dict，它会**安静地**变成一个空厂商。
    """
    try:
        import json as _j
        raw = _j.loads(_path().read_text(encoding="utf-8"))
        return (raw.get("_settings") or {}).get(name, default)
    except Exception:
        return default


def ladder_enabled() -> bool:
    """衰减阶梯总开关。**默认 False。**

    ⚠️ 这是第一个**真的从模型上下文里拿走东西**的动作，必须显式打开。
    📌 引入一个机制和启用一个机制是两件事 —— 一起做，出了问题分不清是
       机制的锅还是接线的锅（同「立架子和搬家具是两件事」）。
    """
    return bool(setting("ladder_enabled", False))


def vendor_of(model_id: str) -> str:
    """模型属于哪一家。**先查表，查不到再看前缀。**

    ⚠️ 前缀（`anthropic/…`）只是**兜底**不是判据：中转/OpenRouter 的 id 形式
       未必带厂商前缀，而表里那条是我们自己写下的事实。
       📌 **能查到声明就别去猜字符串。**
    """
    for vendor, spec in load().items():
        if model_id in (spec.get("models") or {}):
            return vendor
    return (model_id.split("/", 1)[0] or "").lower() if "/" in model_id else ""


def spec_of(model_id: str) -> dict[str, Any]:
    for spec in load().values():
        m = (spec.get("models") or {}).get(model_id)
        if m:
            return m
    return {}


def window_of(model_id: str) -> int:
    """这个模型的上下文窗口（token）。查不到 → `FALLBACK_WINDOW`（保守取小）。"""
    try:
        w = int(spec_of(model_id).get("window") or 0)
        return w if w > 0 else FALLBACK_WINDOW
    except Exception:
        return FALLBACK_WINDOW


def price_of(model_id: str) -> dict[str, float]:
    """`{input_per_1m, output_per_1m}`。查不到 → 全 0。

    ⚠️ 查不到时返回 0 **是对的**：计费宁可少算也不能凭空捏一个价格 ——
       📌 一个编出来的价格会让预算闸在错误的位置上开火，而用户查不出原因。
    """
    m = spec_of(model_id)
    return {"input_per_1m": float(m.get("input_per_1m") or 0.0),
            "output_per_1m": float(m.get("output_per_1m") or 0.0)}


_warned_override = False


def quota_of(model_id: str) -> dict[str, float]:
    """L0/L1/L2/L3 各占窗口的比例。

    ⚠️⚠️ **`_settings.quota_override` 只用于人工验证。**

    正式配额是 `L0 = 25% × 200K = 50K token` —— 要聊很久才够，
    **开了阶梯也只会看到「没触发」**。验证时把 override 填成
    `{"L0":0.03,"L1":0.02,"L2":0.01,"L3":0.005}`，十几轮就能把
    L0→L1→L2→L3→L4 全链跑一遍。

    📌 **这不是作弊**：配额本来就是拍的，调小只是把「要聊三天才触发」
       压缩成「十几轮触发」，**机制一模一样**。
    ⚠️ 但它必须**响亮**：生效期间每次读都 warning（只报一次）——
       📌 **一个临时配置必须响亮到不可能忘记关掉**，
          否则它会以「怎么最近老在压缩」的形式活很久。
    ⚠️ 刻意做成**旁路**而不是改那份正式数据：
       📌 改数据的话，改回来时靠的是"记得原来是多少"，而那是人。
    """
    global _warned_override
    _ov = setting("quota_override")
    if isinstance(_ov, dict) and _ov:
        if not _warned_override:
            _warned_override = True
            logger.warning(
                f"[Models] ⚠️⚠️ 正在使用**临时验证配额** {_ov} —— "
                f"正式配额被旁路了。验完请把 data/model_config.json 的 "
                f"`_settings.quota_override` 设回 null。"
            )
        return {k: float(v) for k, v in _ov.items()}
    q = spec_of(model_id).get("quota")
    return dict(q) if isinstance(q, dict) and q else dict(_FALLBACK_QUOTA)


def currency_symbol(model_or_vendor: str = "") -> str:
    """当前（或指定）厂商的货币符号。默认 `$`。

    📌 **币种是厂商事实**，跟价格、窗口同级 —— 深度求索官方标价是人民币，
       折算成美元等于凭空引入一个汇率：它不在任何一家的官方文档里、且每天在动，
       而且会让用户在 UI 上看到的数字跟厂商后台看到的对不上。
    """
    v = model_or_vendor
    if v and v not in load():
        v = vendor_of(v)
    if not v:
        try:
            from core.provider import provider as _p
            v = getattr(_p, "vendor", "") or vendor_of(getattr(_p, "target_model", "") or "")
        except Exception:
            v = ""
    return str((load().get(v) or {}).get("currency_symbol") or "$")


def default_caps(vendor: str) -> dict[str, float]:
    """该厂商币种下的限额默认值。⚠️ 换厂商时要一起换 —— $5 和 ¥5 不是一个量级。"""
    caps = (load().get(vendor) or {}).get("default_caps") or {}
    return {"soft": float(caps.get("soft") or 5.0), "hard": float(caps.get("hard") or 10.0)}


def vendors() -> list[str]:
    """表里声明了哪些厂商。⚠️ 顺序 = 表里的顺序（UI 下拉照这个排）。"""
    return [k for k in load() if not k.startswith("_")]


def vendor_meta(vendor: str) -> dict[str, Any]:
    """厂商的展示/接入元信息：`label` / `icon` / `official_base` / `key_hint`。

    📌 `official_base` 是**厂商事实**（从官方文档抄的），跟价格/窗口同级；
       中转地址是**用户输入**，不在这张表里。
       ⚠️ 两者混在一起的话，换个中转就得改厂商表。
    """
    spec = load().get(vendor) or {}
    return {"label": str(spec.get("label") or vendor),
            "icon": str(spec.get("icon") or ""),
            "official_base": str(spec.get("official_base") or ""),
            "key_hint": str(spec.get("key_hint") or "")}


ROLES = ("distiller", "classifier", "vision")
_ROLE_LABELS = {"distiller": "压缩提炼", "classifier": "命令检查", "vision": "视觉输入"}


def role_label(role: str) -> str:
    """给 UI 用的中文名。⚠️ 只在这里写一次 —— 三处 UI 各写一遍必然漂移。"""
    return _ROLE_LABELS.get(role, role)


def role_pool(model_id: str, role: str) -> list[str]:
    """这个厂商在这个角色上**可用**的模型，**顺序即优先级**（价格升序）。

    ⭐ 排序是手排进 config 的，不在这里算：各厂商的价格差异一目了然，
       按体感从便宜到贵排就行，算出来也是同一个顺序。
       ⇒ 顺带绕开了**分时定价**（高峰/空闲）带来的"顺序会变吗"问题。

    ⚠️ 表里没有该厂 / 没配这个角色 → 空列表。调用方用 `model_for_role()`，别直接用这个。
    """
    v = vendor_of(model_id)
    if not v:
        return []
    return list(((load().get(v) or {}).get("roles") or {}).get(role) or [])


def _user_role_choice(role: str) -> str:
    """用户在「设置 → 进阶配置」里选的模型。没选过 → 空串。

    📌 **用户选择和厂商事实分两张表**：
       `model_config.json`    厂商事实（谁能干这活、价格、窗口）—— 跟版本走
       `throttle_config.json` 用户选择 —— 跟用户走，由 UI 写
       混在一起的话：升级厂商表会冲掉用户选择，用户改坏配置会污染事实表。
    ⚠️ 读不到一律当"没选过"，**绝不因为配置缺失而崩**（同 `load()` 那条）。
    """
    try:
        from core.paths import data_path
        p = data_path("throttle_config.json")
        if not p.exists():
            return ""
        data = json.loads(p.read_text(encoding="utf-8"))
        return str((data.get("_role_models") or {}).get(role) or "")
    except Exception:
        return ""


def model_for_role(model_id: str, role: str) -> str:
    """这个角色**实际该用**哪个模型。这是三个角色的**唯一**问处。

    ```
    ① 用户在进阶配置里选过，且那个选择仍在池子里   → 用它
    ② 否则                                        → 池子第一个（最便宜的）
    ③ 池子是空的（未知厂商 / 没配这个角色）        → 空串
    ```
    🔴 **不回退主模型**：某个厂商在某个角色上可能只配了一个模型，
       那时「回退主模型」并不能救场 —— 主模型此刻多半也是不可用的那个。
       该做的是往这个池子里加候选，而不是无脑退回主模型。
       ⇒ 池子是**按角色**定义的，退回池子里的下一个永远是能干这活的。

    ⚠️ 空串的含义 = 「这一档没有可用模型」，调用方各自决定怎么办
       （视觉/提炼：退回主模型试一把总比什么都不做强；判定：不启用）。
    """
    pool = role_pool(model_id, role)
    if not pool:
        return ""
    choice = _user_role_choice(role)
    # ⚠️ 必须校验用户的旧选择还在不在池子里 —— 换了厂商 / 型号下线之后，
    #    存着的那个 id 会指向一个别家的、或已经不存在的模型。
    if choice and choice in pool:
        return choice
    return pool[0]


def distiller_for(model_id: str) -> str:
    """给这个主模型配的提炼模型。⇒ `model_for_role(..., "distiller")`。"""
    return model_for_role(model_id, "distiller")


def classifier_for(model_id: str) -> str:
    """给这个主模型配的**危险判定**模型。⇒ `model_for_role(..., "classifier")`。

    📌 历史：这里曾有一条与 `distiller_for` **相反**的语义 ——
       空串 = 不启用判定，红字写着「绝不退回主模型判定，判定的全部价值
       在于它是独立的第三方」。
    ⇒ 那条禁令针对的是「退回**主模型**」= 自己判自己。
       现在退回的是**池子里的另一个模型**，仍然是独立第三方，
       **禁令的理由不成立了** ⇒ 2026-08-30 与另外两个统一。
       📌 一条纪律该不该留，看的是它的**理由**还成不成立。
    """
    return model_for_role(model_id, "classifier")


def vision_for(model_id: str) -> str:
    """给这个主模型配的**视觉**模型。⇒ `model_for_role(..., "vision")`。

    ⭐ 这个槽存在的理由不是省钱，是**解耦**：一个厂商只要有一个型号缺视觉，
       整个厂商的接入就会被卡住（DeepSeek 只有 flash-vision-exp 有视觉）。
       拆成独立角色之后，主模型用它最强的、看图那一下路由到有视觉的那个。
    """
    return model_for_role(model_id, "vision")


def known_models() -> list[str]:
    out: list[str] = []
    for spec in load().values():
        out.extend((spec.get("models") or {}).keys())
    return out
