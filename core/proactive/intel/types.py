# core/proactive/intel/types.py
"""
主动智能 + 情感系统的全部数据结构与枚举。

所有数值常量标 [原型期标定] 的，都是初始先验，最终靠实际使用体验 + 用户滑块 +
Shadow Mode 趋势来标定。不要在纸面上反复改这些数。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════
# 稳定枚举：scene/key/reason_code 必须来自封闭枚举，
# 不允许 LLM 自由命名——否则慢钟 Ledger 越用越脏，且语义化标签本身泄隐私
# ══════════════════════════════════════════════════════════════════════════

# scene 枚举一旦升级，Ledger key 要按 version 做迁移映射，不静默断档。
SCENE_TAXONOMY_VERSION = 1


class Scene(str, Enum):
    WRITING = "writing"
    CODING = "coding"
    BROWSING = "browsing"
    IM = "im"
    MEETING = "meeting"
    MEDIA = "media"
    FILE_MANAGEMENT = "file_management"
    IDLE_RECOVERY = "idle_recovery"
    UNKNOWN = "unknown"


class Channel(str, Enum):
    """呈现渠道，侵入性从高到低。SILENT 不是"展示"，是后台预备（不红点/不浮现）。"""
    POPUP = "popup"        # 抢焦点弹窗，侵入性最高
    CORNER = "corner"      # 角落卡，中
    PANEL = "panel"        # Nano 面板内静默卡，低（用户自己看）
    SILENT = "silent"      # silent prepare：只备动作外壳，不展示


class InterventionType(str, Enum):
    """五类生产力信封。情绪类不在此列——情绪不能单独成为主动原因，
    只能作为这些生产力介入的语气附着层。"""
    PREPARE = "prepare"        # 预备：备好可能要用的物料/入口
    ORGANIZE = "organize"      # 整理：保存/收尾后帮理一版
    RECOVER = "recover"        # 恢复：续上昨天/崩溃前的工作
    RISK_ALERT = "risk_alert"  # 风险提示：保存反复失败等（注意 L0 走 safety_l0）
    NEXT_STEP = "next_step"    # 下一步建议


class RiskLevel(int, Enum):
    READONLY = 0     # 纯建议/只读分析/本地草稿
    SIDE_LIGHT = 1   # 开文件/建新文件/只读命令（L1 一次确认）
    SIDE_HEAVY = 2   # 写/删/移/发/改系统/有副作用命令（L2 强确认）


class AuthLevel(int, Enum):
    """主动链路最多到 L1；可"提出"L2 但绝不"执行"L2。"""
    L0_NONE = 0      # 无需授权：静默生成/展示候选/本地草稿/公开元数据
    L1_CONFIRM = 1   # 一次确认
    L2_STRONG = 2    # 强确认（主动只能提，不能执行）


class SpiceContext(str, Enum):
    """spice_budget 场合分层。决定负向小脾气能出现到什么强度。
    注意：无论哪档，事实层/步骤层/权限层永不带脾气（硬地板）。"""
    SERIOUS = "serious"   # 高压/报错/用户明确下令 → 脾气至多首尾一句轻微
    NORMAL = "normal"     # 普通协作
    CASUAL = "casual"     # 闲聊/低风险 → 可更有性格


# ══════════════════════════════════════════════════════════════════════════
# 快钟 + 中钟：情感状态
#   情感只染语气 + 当主动闸门，绝不进硬功能准入（铁律）。
# ══════════════════════════════════════════════════════════════════════════

# [原型期标定]
_AROUSAL_HALFLIFE_S = 20 * 60      # Arousal 快：~20min 半衰期
_VALENCE_HALFLIFE_S = 3 * 3600     # Valence 慢：~3h
_SHOCK_HALFLIFE_S = 45 * 60        # recent_shock 回升：~45min（≈一次"别烦我"的冷却）
_VALENCE_BASELINE = 0.05           # 略偏正基线
_AROUSAL_BASELINE = 0.30

# UserModeAnchor 档位
USER_MODE_ANCHOR = {"quiet": 0.25, "balanced": 0.50, "active": 0.75}
_LEARNED_DELTA_CLAMP = 0.15        # 学习只能在锚点附近 ±0.15 微调


def _decay_toward(value: float, baseline: float, dt: float, halflife: float) -> float:
    """指数衰减回基线。dt、halflife 同单位（秒）。"""
    if halflife <= 0:
        return baseline
    import math
    k = 0.5 ** (dt / halflife)
    return baseline + (value - baseline) * k


@dataclass
class AffectState:
    """快钟(Mood) + 中钟(Patience) 的可持久化状态。慢钟 Welcome Ledger 在 ledger.py。"""
    # ── 快钟 Mood / VAD（只染语气）──
    valence: float = _VALENCE_BASELINE      # −1..1
    arousal: float = _AROUSAL_BASELINE      # 0..1

    # ── 中钟 Patience 的四个分量 ──
    proactive_enabled: bool = True          # 硬开关；"以后别主动说话"→False（override）
    user_mode: str = "balanced"             # quiet / balanced / active（用户档位锚）
    learned_delta: float = 0.0              # ±0.15 慢学习微调
    recent_shock: float = 0.0              # ≤0，"别烦我"的瞬时冲击，必回升至 0

    last_update_ts: float = field(default_factory=time.time)

    # ── 代谢 ──
    def decay(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        dt = max(0.0, now - self.last_update_ts)
        self.valence = _decay_toward(self.valence, _VALENCE_BASELINE, dt, _VALENCE_HALFLIFE_S)
        self.arousal = _decay_toward(self.arousal, _AROUSAL_BASELINE, dt, _AROUSAL_HALFLIFE_S)
        # recent_shock 必回升到 0（不变量：不可被单一事件永久拉低）
        self.recent_shock = _decay_toward(self.recent_shock, 0.0, dt, _SHOCK_HALFLIFE_S)
        self.last_update_ts = now

    # ── 中钟输出：当前打扰预算 ──
    def patience_current(self, now: Optional[float] = None) -> float:
        """0..1。仅 proactive_enabled 时有意义。
        = clamp(anchor + learned_delta + recent_shock)。"""
        if not self.proactive_enabled:
            return 0.0
        self.decay(now)
        anchor = USER_MODE_ANCHOR.get(self.user_mode, 0.5)
        v = anchor + self.learned_delta + self.recent_shock
        return max(0.0, min(1.0, v))

    # ── 快钟输出：语气提示（只给"味道"，不给原始数值；模型自己渲染）──
    def tone_hint(self, now: Optional[float] = None) -> str:
        """供注入系统提示的一句短语气提示。负向=小脾气(对事不对人/漏不播报)，
        绝不压成中性机器人，也绝不降任务质量（约束在提示词里另述）。"""
        self.decay(now)
        v = self.valence
        if v >= 0.35:
            return "positive; you may be a little lighter and more playful"
        if v <= -0.35:
            return "slightly annoyed or flat; you may be a bit sharper, but never at the user; do the work normally and do not narrate your mood"
        if v <= -0.15:
            return "low energy; keep the wording shorter and calmer"
        return "steady and natural"

    # ── 边界 clamp（锚点防漂移）──
    def clamp(self) -> None:
        self.valence = max(-1.0, min(1.0, self.valence))
        self.arousal = max(0.0, min(1.0, self.arousal))
        self.learned_delta = max(-_LEARNED_DELTA_CLAMP, min(_LEARNED_DELTA_CLAMP, self.learned_delta))
        self.recent_shock = min(0.0, self.recent_shock)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AffectState":
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ══════════════════════════════════════════════════════════════════════════
# 慢钟：Welcome Ledger
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LedgerKey:
    """分层键。冒泡链：(type,scene,channel) → (type,channel) → (type)。不到全局。"""
    type: InterventionType
    scene: Scene
    channel: Channel

    def parents(self) -> list["LedgerKey"]:
        """父级键（用于惩罚冒泡；scene 退化为 UNKNOWN 表示"该层不限定 scene"）。"""
        return [
            LedgerKey(self.type, Scene.UNKNOWN, self.channel),  # type+channel
            LedgerKey(self.type, Scene.UNKNOWN, Channel.SILENT),  # type（channel 也退化）
        ]

    def as_str(self) -> str:
        return f"{self.type.value}|{self.scene.value}|{self.channel.value}|v{SCENE_TAXONOMY_VERSION}"


# [原型期标定]
_FREQ_FLOOR_BY_CHANNEL = {           # 频率地板（最密多久一次），按渠道分
    Channel.POPUP:  7 * 24 * 3600,   # 弹窗：某类最多每周一次
    Channel.CORNER: 24 * 3600,       # 角落卡：每天一次
    Channel.PANEL:  3 * 3600,        # 面板卡：宽松
    Channel.SILENT: 0,
}


@dataclass
class LedgerEntry:
    """某个 LedgerKey 的长期偏好。welcome / frequency / explicit_mute 三条独立路。"""
    welcome_score: float = 0.5        # 0..1，可衰减回中性 0.5
    explicit_mute: bool = False       # 用户明确"以后别提醒这个"；不自动回升（与 welcome 分离）
    min_interval: float = 0.0         # 两次最小间隔（秒），按渠道有地板、会回升
    daily_cap: int = 3
    last_shown_at: float = 0.0
    today_count: int = 0
    today_date: str = ""
    accept_ema: float = 0.0           # 带衰减窗的正向证据
    annoy_ema: float = 0.0            # 带衰减窗的负向证据
    accuracy_prior: float = 0.5       # correctness 独立路（"这个不准"调这个，不动 welcome）
    capability_confidence: float = 0.5  # 被动使用抬这个，不抬 proactive welcome

    def freq_allowance(self, channel: Channel, now: Optional[float] = None) -> float:
        """0..1：现算（不存）。逼近 min_interval / daily_cap 上限时趋 0。"""
        now = now or time.time()
        if self.explicit_mute or self.welcome_score <= 0.0:
            return 0.0  # 明确禁用 / welcome=0 不受地板保护
        floor = max(self.min_interval, _FREQ_FLOOR_BY_CHANNEL.get(channel, 0))
        # 间隔余量
        gap = now - self.last_shown_at
        interval_ok = 1.0 if floor <= 0 else max(0.0, min(1.0, gap / floor))
        # 每日余量
        import datetime
        today = str(datetime.date.today())
        cnt = self.today_count if self.today_date == today else 0
        daily_ok = 1.0 if self.daily_cap <= 0 else max(0.0, 1.0 - cnt / self.daily_cap)
        return min(interval_ok, daily_ok)


# ══════════════════════════════════════════════════════════════════════════
# 唤醒 / 候选 / 安全 / 影子日志
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Episode:
    """一段工作。边界 = 硬锚点 + 软推断；带 boundary_confidence。"""
    scene: Scene
    app_cluster: str
    start_ts: float
    end_ts: Optional[float] = None
    salient_events: list = field(default_factory=list)
    boundary_confidence: float = 0.0  # 高→可出整理/恢复候选；中→只面板卡；低→不学不扰


@dataclass
class InterventionCandidate:
    """一次主动机会。channel 由 scorer 按渠道分别试算后选定。"""
    type: InterventionType
    scene: Scene
    utility: float                      # 显式收益分（减少用户下一步成本，难造假）
    timing_score: float = 0.5          # 此刻是不是这类内容的好时点（非"用户忙不忙"）
    risk_level: RiskLevel = RiskLevel.READONLY
    required_auth: AuthLevel = AuthLevel.L0_NONE
    proposed_action: Optional[str] = None
    fallback_message: Optional[str] = None
    context_key_str: str = ""
    intervention_id: str = ""


class AlertState(str, Enum):
    NEW = "new"
    NOTIFIED = "notified"
    ACKED = "acked"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"


@dataclass
class L0Alert:
    """硬安全警报。alert_id = type+app/resource+fingerprint+severity_band。"""
    alert_id: str
    alert_type: str          # save_failed / disk_low / battery_low / app_crash ...
    severity_band: str       # critical / ...
    state: AlertState = AlertState.NEW
    created_at: float = field(default_factory=time.time)
    last_penetrated_at: float = 0.0


@dataclass
class ShadowLogEntry:
    """Shadow Mode 一条决策记录。只存枚举/分值/reason_code，
    绝不存窗口标题全文/文件名/任何语义化敏感标签。"""
    ts: float
    candidate_type: str
    scene: str
    score_by_channel: dict          # {channel: score}
    chosen_channel: Optional[str]   # None = 决定不发
    reason_code: str                # 产品级枚举，如 "patience_low" / "freq_capped" / "fired_panel"
