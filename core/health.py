# core/health.py
"""
Nano 能力健康登记 —— 「异步产出与后台故障统一出口」的状态权威层

═══ 为什么是三个东西而不是一张表 ═══

最初的设计是"一张故障登记表喂三个消费者"。推演时发现那张表实际上揉了四件
生命周期完全不同的事：① 当前能力是否可用 ② 某次故障事件是否发生过
③ UI 是否已经展示 ④ Agent 是否应该知道。揉在一起之后，"恢复"、"去重"、
"多消费者"、"要不要进对话历史"这四件事会互相绊住——比如一条故障因为
acknowledged=True 就再也不通知，但它其实已经恢复过又复发了。

所以拆成三层，彼此不共享生命周期：

  1. HealthRegistry     当前能力状态的唯一事实来源（线程安全，可在事件循环存在前写）
  2. 状态转移队列        queue.SimpleQueue，只放"状态真的变了"的事件，供 UI 侧 drain
  3. RecentSystemEvents 有界事件流，每轮注入 system prompt，让模型知道"刚才发生了什么"

═══ 为什么故障事件绝不写进 MemoryManager ═══

ReAct 循环里 tool_calls 与 tool_results 必须严格相邻（memory/manager.py 的
validate_tool_turns 守着这条）。后台线程在这两步之间插一条 assistant 消息，
真实后果是（已逐行走过链路，不是理论推测）：

    storage: [..., tool_calls, assistant(故障), tool_results]
      → validate_tool_turns 失败
      → _rollback_last_tool_batch 只 pop 掉 tool_results（已经跑完的真实工具结果被丢弃），
        再 peek 到 assistant 不是 tool_calls 就退出 → 留下孤儿 tool_calls
      → raise RuntimeError → 本轮硬崩，用户看到"内核故障"
      → _clean_damaged_memory 对 assistant 结尾三个分支都不匹配，直接 return，不修
      → 下一轮必然 400，靠 400 分支的 repair_invalid_tool_turns 才自愈

一次后台故障告警 = 干掉当前任务 + 丢掉已执行的工具结果 + 再赔一轮无效 API 调用。

而且 provider.py 的 _merge_context 里那段"丢弃会破坏 thinking 块的同角色合并"
就是当年被同一类事故（旧 speaker 往历史插 assistant）磨出来的补丁——这条路早撞过。

结论：模型侧的知情一律走 system prompt 注入（RecentSystemEvents + 能力边界），
不碰对话历史。附带好处是不受主历史 10 轮截断影响，也不会在用户几小时不说话时
无限增长（_truncate_safely 只在真实 user 消息超 max_turns 时才切）。

═══ 线程安全 ═══

RAG 初始化线程在 WebUI() 构造时就启动，早于 ui.run()，所以写入端不能依赖事件循环。
这里全用 threading.RLock + queue.SimpleQueue（后者可以在 loop 存在前创建，
asyncio.Queue 不行）。UI 侧在 loop 起来后用定时器 drain。
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

from loguru import logger


# ══════════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════════

class Status:
    """能力当前可用状态。

    与 severity 分开：同样是 UNAVAILABLE，用户从没配过的可选 MCP 和
    用户正在依赖的知识库，严重程度完全不同。
    """
    AVAILABLE   = "AVAILABLE"      # 正常，不提示
    DEGRADED    = "DEGRADED"       # 仍可完成，但质量/范围/速度明显下降 → 只进监控
    UNAVAILABLE = "UNAVAILABLE"    # 该能力没有可用路径 → 首次出现时出故障卡
    RECOVERING  = "RECOVERING"     # 正在自愈，结果未定 → 监控显示，不重复告警

    _ALL = frozenset({AVAILABLE, DEGRADED, UNAVAILABLE, RECOVERING})


class Severity:
    INFO     = "INFO"
    WARNING  = "WARNING"
    ERROR    = "ERROR"
    CRITICAL = "CRITICAL"

    _RANK = {INFO: 0, WARNING: 1, ERROR: 2, CRITICAL: 3}

    @classmethod
    def rank(cls, s: str) -> int:
        return cls._RANK.get(s, 0)


class Transition:
    """状态转移事件类型。只有真的变了才入队，反复发生只累加计数不入队。"""
    OPENED    = "OPENED"      # 从可用 → 不可用/降级
    UPDATED   = "UPDATED"     # 仍然异常，但根因（code）变了
    RECOVERED = "RECOVERED"   # 回到可用


# ══════════════════════════════════════════════════════════════════════════
# 能力清单
# ══════════════════════════════════════════════════════════════════════════
# 按【能力】登记而不是按【组件】。按组件太粗——"RAG 挂了"这一句话盖住了
# 六种影响面完全不同的情况：知识库为空（正常，不是故障）/ 单文件解析失败
# （只影响该文件）/ BM25 构建失败（向量检索仍可用，属降级）/ reranker 不可用
# （保持原候选顺序，质量降级）/ embedder 或 chroma 加载失败（检索能力没了）/
# 而 load_full_file 在向量检索挂掉时仍然可用。
#
# tools 字段是【能力门控】的依据：该能力 UNAVAILABLE 时这些工具从 manifest 下架，
# 且执行层 fail-fast。DEGRADED 不下架工具，只在能力边界里说明限制。
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    label: str                      # 用户/模型可读名
    tools: tuple[str, ...] = ()     # 不可用时应下架的工具名
    monitor_card: str = "environment"   # 对应监控卡；没有专属卡的落 environment
    gate_on_degraded: bool = False  # 降级时是否也下架（默认否：还能用就别摘）
    # 注入模型的那句"所以你该怎么办"。留空则用按状态生成的通用措辞。
    # 有些能力的通用措辞是错的——比如预算耗尽跟"别调相关工具"无关，
    # 它需要的是"收敛输出、别起大工程"。
    notice_hint: str = ""


class Cap:
    """能力 key 常量。字符串裸写会拼错，一律走这里。"""
    KB_VECTOR_SEARCH = "knowledge.vector_search"
    KB_KEYWORD_SEARCH = "knowledge.keyword_search"
    KB_RERANKER = "knowledge.reranker"
    KB_FULL_FILE = "knowledge.full_file_read"
    KB_FILE_LISTING = "knowledge.file_listing"
    KB_STORE = "knowledge.store"          # 向量库本身（chroma 连接/schema）
    OCR_TESSERACT = "os.ocr_fallback"
    # 🪦 **这里曾有 `WEB_FETCH = "web.fetch"`，后来删除。**
    #    它是一条【定义了却零上报】的死条目，而真去接的时候
    #    发现：**它没有任何属于自己的失败态。**「能不能读一个网页」完全等于
    #    「有没有一个声明了 web.fetch 的 MCP server 连着」，而那件事
    #    `mcp.server.<name>` 已经在报了 —— 连故障文案、恢复建议、工具屏蔽、
    #    探针都是现成的。
    #    ⚠️ 再报一次的后果不是"更保险"，是**同一件事对模型说两遍**
    #      （`render_capability_notice` 会把两条都注进去）。
    #    📌 **一个完全派生的能力，不该在事实表里占一行** —— 它只会在两个
    #       声音不一致的时候制造一个无法回答的问题：该信哪个。
    #    → 现在「读层在不在」由 `MCPManager.web_status()` 当场算，不落库。
    #    🔴 **别把它加回来。** 要加之前先回答：它有哪个 `mcp.server.*` 报不了的失败态？
    WEB_SEARCH = "web.search"
    MEMORY_SEMANTIC = "memory.semantic_index"
    MODEL_BUDGET = "model.budget"         # 今日用量闸

    # ── 动态 key（写代码时还不知道有哪些）────────────────────────────────
    # MCP 按 **server** 登记，不按"MCP 整体"——fetch 挂了不等于 playwright 挂了。
    # ⚠️ 这就是 `_CAPABILITIES` 这张模块级字面量表装不下的那一类，
    #    所以下面配了 `register_capability()`（同 `register_probe` 的运行时登记范式）。
    MCP_SERVER_PREFIX = "mcp.server."

    @staticmethod
    def mcp_server(name: str) -> str:
        return f"{Cap.MCP_SERVER_PREFIX}{name}"


_CAPABILITIES: dict[str, CapabilitySpec] = {
    c.key: c for c in (
        # 向量检索挂了 → query_local_knowledge 直接不可用。
        # 注意 list_knowledge_files / get_file_path 不挂在它下面：那两个只读目录和路径，
        # 不碰 embedder，向量库挂了它们照样能用（也是模型此时唯一还能给用户的交代）。
        CapabilitySpec(Cap.KB_VECTOR_SEARCH, "知识库语义检索",
                       tools=("query_local_knowledge",), monitor_card="rag"),
        CapabilitySpec(Cap.KB_STORE, "知识库向量存储",
                       tools=("query_local_knowledge",), monitor_card="rag"),
        # BM25 缺失只是少一路召回，RRF 退化成纯向量 → 降级，不下架。
        CapabilitySpec(Cap.KB_KEYWORD_SEARCH, "知识库关键词检索", monitor_card="rag"),
        # reranker 缺失 → 保持 RRF 原序，质量降级，不下架。
        CapabilitySpec(Cap.KB_RERANKER, "知识库重排", monitor_card="rag"),
        CapabilitySpec(Cap.KB_FULL_FILE, "全文加载",
                       tools=("load_full_file",), monitor_card="full_file"),
        CapabilitySpec(Cap.KB_FILE_LISTING, "知识库文件清单",
                       tools=("list_knowledge_files", "get_file_path"), monitor_card="rag"),
        # Tesseract 缺失 → 视觉定位第二级 OCR 降级失效，会跳到更贵的多模态兜底；
        # 扫描件入库也失效。能力还在，只是更贵更不准 → 降级。
        CapabilitySpec(Cap.OCR_TESSERACT, "OCR 视觉定位回退"),
        # ⚠️ 只剩「搜」这一条 —— 「抓」是派生的，见 Cap.WEB_FETCH 的墓碑。
        # 由 `skills/official/SearchTheWeb.py` 上报（它有自己的失败态：403 被封、
        # 结果页改版、连不上），并在同一个文件里登记探针。
        CapabilitySpec(Cap.WEB_SEARCH, "网络搜索", monitor_card="net"),
        CapabilitySpec(Cap.MEMORY_SEMANTIC, "语义记忆索引"),
        # 预算闸。tools 刻意为空——预算不可用时挡住的是【所有模型调用】，
        # 不是某几个工具，闸在 provider 的客户端包装层。登记在这里是为了复用
        # 这一套要给齐的四样东西：动态段注入、监控卡、聊天出口、恢复通道。
        #
        # ⭐ 注意软硬两态的价值完全不同：
        #   DEGRADED（软闸）→ 调用照常放行，这条注入是有用的，Nano 能自己收敛；
        #   UNAVAILABLE（硬闸）→ 模型压根不会被调用，这条注入它永远看不到，
        #                        此时的价值在监控卡与聊天出口（告诉【用户】）。
        CapabilitySpec(
            Cap.MODEL_BUDGET, "模型调用预算",
            notice_hint=(
                "Budget is a hard constraint on you, not on the user. "
                "Keep answers tighter, avoid unnecessary extra tool rounds, and do not start "
                "expensive multi-step work without first telling the user the budget is running low."
            ),
        ),
    )
}


# ── 运行时登记 ───────────────────────────────────────────────────────────
# `_CAPABILITIES` 是模块级字面量 dict，装不下"名字要等配置读进来才知道"的能力
# （MCP 的每个 server 都是一个独立能力）。这个入口把它变成可增补的。
#
# 📌 **为什么是运行时登记而不是让 health 去读 mcp 配置**：与 `register_probe`
#    同一条理由 —— health 是最底层的事实表，它不该 import 任何领域模块，
#    否则 health ← mcp_client ← health 就绕回来了。**依赖方向保持单向**：
#    谁有那个事实，谁自己来登记。
#
# ⚠️ 这不是"第二张能力表"，它写进的就是同一张 `_CAPABILITIES` ——
#    所以门控 / 监控卡 / 能力边界注入 / 恢复探针**全部白拿**，一处都不用改。
_CAP_LOCK = threading.RLock()


def register_capability(spec: CapabilitySpec) -> None:
    """登记（或就地更新）一个能力声明。幂等，可反复调。

    ⭐ 允许覆盖是**必需的**，不是图省事：MCP server 重连后工具清单可能变了，
       而 `spec.tools` 决定"这个能力挂掉时哪些工具名要被识别为『暂时不可用』"。
       只登记一次的话，重连后新增的工具名会在下次断线时掉回 UNKNOWN_TOOL。
    """
    if not isinstance(spec, CapabilitySpec) or not spec.key:
        return
    with _CAP_LOCK:
        _CAPABILITIES[spec.key] = spec


def unregister_capability(key: str) -> None:
    """注销一个动态能力，并清掉它的健康状态。

    ⚠️ 必须连状态一起清：能力没了而 UNAVAILABLE 状态还留着，
       会变成一张**用户永远关不掉的故障卡片**（那个 server 明明已经被删了）。
       📌 同「上限必须配回收」那条 —— 登记入口配注销入口，否则只增不减。
    """
    with _CAP_LOCK:
        _CAPABILITIES.pop(key, None)
    try:
        get_health().forget(key)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# 探针覆盖的**缺口清单** —— 让「还没登记探针」这件事有形状
# ══════════════════════════════════════════════════════════════════════════
# 🔴 这张表是为了修一个**已经发生过的**问题：`register_probe` 框架 2026-08-04
#    就建好了，之后三个多月里只登记了 3 个能力，而**没有任何东西提醒过谁**。
#    结果是「断网 5 秒把网络能力钉死到重启」这条一直挂在台账上，
#    而 Nano 是常驻应用，一开就是几天。
#
# 📌 判据（本项目已栽过五次）：**一个需要「有人记得去做」才不会错的机制，
#    等于没有这个机制。** 所以缺口不能只写在外部文档里 —— 它得在代码里有个位置，
#    并且**新增能力时不填就会红**（见 `tests/cases/t_d11_mcp_health.py`）。
#
# ⚠️ 这是**白名单形状**，不是排除法：新增的 Cap 默认「缺探针且没解释」→ 红。
#    📌 排除法欠账随时间增长，白名单不会。
_PROBE_DEFERRED: dict[str, str] = {}


def _init_probe_deferred() -> None:
    _PROBE_DEFERRED.update({
        # 🪦 WEB_FETCH / WEB_SEARCH 两条豁免都在 2026-08-25 清掉了：
        #    前者整条能力被删（派生态，见 Cap 里的墓碑）；
        #    后者拿到了**真上报 + 真探针**，两者都在 `skills/official/SearchTheWeb.py`。
        #    ⭐ 这是这张缺口台账第一次真的被销账，而不是换个理由继续挂着 ——
        #       台账的意义就在这里：📌 **它得能变短，否则它只是个更体面的 TODO。**
        Cap.KB_FULL_FILE: (
            "零上报：`load_full_file` 目前不会把失败登记成能力故障（它按文件报错）。"
            "要接的话属于迭代阅读那一批，不是探针问题。"
        ),
        Cap.KB_FILE_LISTING: "零上报：目录列举没有失败态可言（读不到目录时上层已按普通错误处理）。",
        Cap.MEMORY_SEMANTIC: (
            "零上报：语义记忆索引还没接健康登记，属于「已知缺口、尚未安排」的那一类。"
        ),
        Cap.MODEL_BUDGET: (
            "**已有等价物**：`app.py` 的 `_budget_health_tick`（20 秒一跳）就是它的探针，"
            "只是走的是自己的时钟而不是 `register_probe`（预算要重算，不是探存活）。"
        ),
    })


def probe_coverage_gaps(registered: set[str] | None = None) -> list[str]:
    """返回「既没有探针、也没有写明为什么不需要」的能力 key。

    ⭐ 动态能力（MCP 的 per-server key）不进这张表 —— 它们由
       `MCPManager._register_probes()` 在登记能力的同时登记探针，
       **两件事在同一段代码里**，不存在"忘了另一半"的空间。
    """
    _init_probe_deferred()
    if registered is None:
        registered = set(get_health()._probes)
    return sorted(
        k for k in _CAPABILITIES
        if not k.startswith(Cap.MCP_SERVER_PREFIX)
        and k not in registered and k not in _PROBE_DEFERRED
    )


def get_capability_spec(key: str) -> Optional[CapabilitySpec]:
    return _CAPABILITIES.get(key)


def all_capability_keys() -> tuple[str, ...]:
    return tuple(_CAPABILITIES.keys())


# ══════════════════════════════════════════════════════════════════════════
# 状态记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class HealthState:
    capability: str
    status: str = Status.AVAILABLE
    severity: str = Severity.INFO
    code: str = ""                  # 稳定指纹的一部分，如 EMBEDDER_LOAD_FAILED
    user_message: str = ""          # 用户/模型可见的一句话
    recovery_hint: str = ""         # 可选：怎么恢复（**给用户看**，中文）
    # ⭐ 给【模型】看的同一件事，英文。**必须是两个字段，不是一个。**
    #    📌 「一个字段不许表达两个现实」：这里的两个现实是**两个受众**——
    #       用户看的那份跟随 UI 语言，模型看的那份**永远英文**（原则 8.5）。
    #    ⚠️ 这不是 i18n 的欠账：界面切成英文之后，模型这份仍然是英文，
    #       一个字段永远合不拢。所以现在拆是它本来的形状，不是提前优化。
    #    ⚠️ 留空 → 不注入（fail-safe：朝少说一句错，不朝混语言错）。
    recovery_hint_en: str = ""
    technical_detail: str = ""      # 只进日志与"详情"，不直接喂模型（含路径/堆栈，噪音大）
    first_seen: float = 0.0
    last_seen: float = 0.0
    occurrence_count: int = 0
    generation: int = 0             # 恢复后再次发生 → +1，让它能重新通知
    presented_at: float | None = None        # UI 已经展示过（去重用）
    user_acknowledged_at: float | None = None
    resolved_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == Status.AVAILABLE

    @property
    def fingerprint(self) -> str:
        """稳定指纹。不用完整异常字符串——路径、内存地址、下载进度会变，
        同一根因会被识别成多个故障，去重直接失效。"""
        return f"{self.capability}:{self.code}:gen{self.generation}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "label": (get_capability_spec(self.capability).label
                      if get_capability_spec(self.capability) else self.capability),
            "status": self.status,
            "severity": self.severity,
            "code": self.code,
            "user_message": self.user_message,
            "recovery_hint": self.recovery_hint,
            "recovery_hint_en": self.recovery_hint_en,
            "occurrence_count": self.occurrence_count,
            "generation": self.generation,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "presented_at": self.presented_at,
            "fingerprint": self.fingerprint,
        }


@dataclass
class TransitionEvent:
    kind: str                 # Transition.*
    state: HealthState        # 转移【之后】的快照（已 copy，消费者拿到不会被后续写入改掉）
    previous_status: str = Status.AVAILABLE
    ts: float = field(default_factory=time.time)


# ══════════════════════════════════════════════════════════════════════════
# HealthRegistry
# ══════════════════════════════════════════════════════════════════════════

class HealthRegistry:
    """能力健康的唯一事实来源。

    只负责：当前状态 / 状态转移 / 去重 / 恢复。
    不负责：操作 UI、写对话历史、决定要不要弹卡片（那是 NotificationPolicy 的事）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._states: dict[str, HealthState] = {}
        # SimpleQueue：可以在事件循环存在之前创建，专供线程间通信。
        # 不用 asyncio.Queue——它不适合从普通线程直接写，而 RAG 初始化线程
        # 启动时事件循环压根还不存在。
        self._transitions: "queue.SimpleQueue[TransitionEvent]" = queue.SimpleQueue()
        # 能力探针：key -> 无参函数，返回 True 表示已恢复（见 register_probe）
        self._probes: dict[str, Any] = {}
        # 探针退避调度：key -> (下次可探时间, 已失败次数)
        self._probe_sched: dict[str, tuple[float, int]] = {}

    # ── 写入端（任何线程，任何时刻，含事件循环存在之前）────────────────────

    def report(
        self,
        capability: str,
        *,
        status: str = Status.UNAVAILABLE,
        severity: str = Severity.ERROR,
        code: str = "",
        user_message: str = "",
        recovery_hint: str = "",
        recovery_hint_en: str = "",
        technical_detail: str = "",
    ) -> Optional[TransitionEvent]:
        """登记一次异常。返回状态转移事件（真的变了）或 None（重复发生）。

        重复发生只累加 occurrence_count 与 last_seen，不再入队——否则一个
        每次查询都失败的组件会把队列刷爆，UI 连发几十张故障卡片。
        """
        if status not in Status._ALL:
            status = Status.UNAVAILABLE
        now = time.time()
        with self._lock:
            st = self._states.get(capability)
            if st is None:
                st = HealthState(capability=capability, status=Status.AVAILABLE)
                self._states[capability] = st

            prev_status = st.status
            prev_code = st.code

            if st.ok:
                # 可用 → 异常：开一个新 generation。
                # 为什么必须换代：同一个故障恢复后再次发生，如果沿用旧记录，
                # presented_at 还在，就再也不会通知用户了。
                st.generation += 1
                st.first_seen = now
                st.occurrence_count = 0
                st.presented_at = None
                st.user_acknowledged_at = None
                st.resolved_at = None

            st.status = status
            st.severity = severity
            st.code = code or st.code
            st.user_message = user_message or st.user_message
            st.recovery_hint = recovery_hint or st.recovery_hint
            st.recovery_hint_en = recovery_hint_en or st.recovery_hint_en
            st.technical_detail = technical_detail or st.technical_detail
            st.last_seen = now
            st.occurrence_count += 1

            if prev_status == Status.AVAILABLE:
                kind = Transition.OPENED
            elif code and code != prev_code:
                kind = Transition.UPDATED      # 仍然坏，但根因变了 → 值得更新卡片
            elif status != prev_status:
                kind = Transition.UPDATED      # 例如 UNAVAILABLE → RECOVERING
            else:
                # 同一根因反复发生：只计数，不入队
                logger.debug(
                    f"[Health] {capability} 重复上报 code={code} "
                    f"count={st.occurrence_count}（不入队）"
                )
                return None

            ev = TransitionEvent(kind=kind, state=replace(st), previous_status=prev_status)
            self._transitions.put(ev)
            logger.warning(
                f"[Health] {kind} {capability} status={status} severity={severity} "
                f"code={code} gen={st.generation} :: {user_message}"
            )
            # technical_detail 的用途就是"只进日志不喂模型"，所以必须真的进日志。
            # 之前写成 `user_message or technical_detail`，有 user_message 时详情被吞掉，
            # 结果排查时日志里只剩一句没头没尾的异常名。
            if st.technical_detail:
                logger.warning(f"[Health] {capability} 技术详情: {st.technical_detail}")
            return ev

    # ── 能力探针：解开「坏了就再也不会好」的死锁 ─────────────────────────────
    # 死锁长这样：能力坏了 → 它的工具被下架 → 于是【没有任何代码路径会再去用它】
    # → 而 report_ok 只在"被成功使用时"触发 → 永远不会恢复。
    # 用户把问题修好了（重连网络、装上 Tesseract、修好向量库），Nano 也不知道。
    #
    # 破法：不依赖业务路径，用一个外部时钟主动去探。探针必须【廉价、无副作用、
    # 且绕过能力闸】——它就是来解闸的，不能被自己挡住。
    #
    # 退避是必须的：网络类能力可能长时间不可用，每秒探一次既费电又刷日志。
    _PROBE_BACKOFF = (30.0, 60.0, 120.0, 300.0, 600.0)   # 秒，最后一档封顶

    def register_probe(self, capability: str, fn) -> None:
        """登记某个能力的存活探针。fn() -> bool，True 表示能力已恢复。

        刻意做成"运行时登记"而不是写进 CapabilitySpec：探针实现要 import rag /
        mcp_client 这些重模块，写进 spec 字面量会造成 health ← rag 的循环依赖。
        由各模块在自己 import 时调 register_probe 登记，依赖方向保持单向。
        """
        with self._lock:
            self._probes[capability] = fn

    def _probe_due(self, cap: str, now: float) -> bool:
        nxt, _ = self._probe_sched.get(cap, (0.0, 0))
        return now >= nxt

    def _schedule_next_probe(self, cap: str, now: float) -> None:
        _, attempts = self._probe_sched.get(cap, (0.0, 0))
        delay = self._PROBE_BACKOFF[min(attempts, len(self._PROBE_BACKOFF) - 1)]
        self._probe_sched[cap] = (now + delay, attempts + 1)

    def run_due_probes(self) -> list[str]:
        """跑所有到点的探针，返回本次真正恢复了的能力 key 列表。

        由外部时钟驱动（app.py 的定时器）。探针本身在调用线程里同步执行，
        所以【探针实现必须廉价】——不要在里面加载模型、发大请求。
        """
        now = time.time()
        with self._lock:
            candidates = [
                s.capability for s in self._states.values()
                if (not s.ok) and s.capability in self._probes and self._probe_due(s.capability, now)
            ]
            probes = {c: self._probes[c] for c in candidates}
        recovered: list[str] = []
        for cap, fn in probes.items():
            ok = False
            try:
                ok = bool(fn())
            except Exception as e:
                logger.debug(f"[Health] 探针 {cap} 抛异常（视为仍不可用）: {e}")
            if ok:
                with self._lock:
                    self._probe_sched.pop(cap, None)
                # ⚠️ 不能只看 recover() 的返回值。探针往往复用真实调用路径
                # （如 KB_STORE 的探针就是 _get_collection()），而那条路径成功时
                # 自己就会 report_ok —— 转移事件已经被它发出去了，这里再 recover()
                # 会因为"状态已经是 ok"而返回 None，导致调用方误以为没恢复。
                # 所以以【探针跑完后的实际状态】为准。
                ev = self.recover(cap, note="probe succeeded")
                st = self.get(cap)
                if ev is not None or (st is not None and st.ok):
                    recovered.append(cap)
                    logger.info(f"[Health] 探针确认 {cap} 已恢复")
            else:
                with self._lock:
                    self._schedule_next_probe(cap, now)
        return recovered

    def recover(self, capability: str, *, note: str = "") -> Optional[TransitionEvent]:
        """标记能力已恢复。只有原先确实不可用/降级时才产生转移事件。"""
        now = time.time()
        with self._lock:
            st = self._states.get(capability)
            if st is None or st.ok:
                return None
            prev_status = st.status
            st.status = Status.AVAILABLE
            st.severity = Severity.INFO
            st.resolved_at = now
            st.last_seen = now
            ev = TransitionEvent(kind=Transition.RECOVERED,
                                 state=replace(st), previous_status=prev_status)
            self._transitions.put(ev)
            logger.info(f"[Health] RECOVERED {capability}（原 {prev_status}）{note}")
            return ev

    def mark_recovering(self, capability: str, *, user_message: str = "") -> Optional[TransitionEvent]:
        """自愈进行中（如向量库重建）。避免自愈期间被当成"又坏了"重复告警。"""
        return self.report(
            capability,
            status=Status.RECOVERING,
            severity=Severity.WARNING,
            code="RECOVERING",
            user_message=user_message,
        )

    # ── 读取端 ──────────────────────────────────────────────────────────

    def drain_transitions(self, limit: int = 64) -> list[TransitionEvent]:
        """一次取完当前队列（UI 消费者用）。

        一次取完而不是一次取一条：多个组件同时失败时要能归并成
        "启动时检测到 3 项问题" 一张卡，而不是连发三张故障卡片。
        """
        out: list[TransitionEvent] = []
        for _ in range(limit):
            try:
                out.append(self._transitions.get_nowait())
            except queue.Empty:
                break
        return out

    def is_available(self, capability: str) -> bool:
        """执行层 fail-fast 用。未登记过 = 从没报过错 = 视为可用。"""
        with self._lock:
            st = self._states.get(capability)
            return st is None or st.status in (Status.AVAILABLE, Status.DEGRADED)

    def status_of(self, capability: str) -> str:
        with self._lock:
            st = self._states.get(capability)
            return st.status if st else Status.AVAILABLE

    def get(self, capability: str) -> Optional[HealthState]:
        with self._lock:
            st = self._states.get(capability)
            return replace(st) if st else None

    def problems(self) -> list[HealthState]:
        """当前所有非正常能力，按严重度倒序。"""
        with self._lock:
            bad = [replace(s) for s in self._states.values() if not s.ok]
        bad.sort(key=lambda s: (Severity.rank(s.severity), s.last_seen), reverse=True)
        return bad

    def blocked_tools(self) -> set[str]:
        """当前应该从 manifest 下架的工具名集合。

        只有 UNAVAILABLE 才下架；DEGRADED 保留工具（还能用），
        限制写在能力边界文本里由模型自己权衡。
        """
        out: set[str] = set()
        with self._lock:
            for st in self._states.values():
                spec = _CAPABILITIES.get(st.capability)
                if not spec or not spec.tools:
                    continue
                if st.status == Status.UNAVAILABLE or (
                    st.status == Status.DEGRADED and spec.gate_on_degraded
                ):
                    out.update(spec.tools)
        return out

    def tool_block_reason(self, tool_name: str) -> Optional[HealthState]:
        """某个工具被哪条故障挡住了（执行层 fail-fast 时给模型的解释）。"""
        with self._lock:
            for st in self._states.values():
                spec = _CAPABILITIES.get(st.capability)
                if not spec or tool_name not in spec.tools:
                    continue
                if st.status == Status.UNAVAILABLE or (
                    st.status == Status.DEGRADED and spec.gate_on_degraded
                ):
                    return replace(st)
        return None

    def card_status(self, monitor_card: str) -> Optional[HealthState]:
        """某张监控卡对应的最严重问题。可用性优先于活动状态——
        原来那些卡表达的是"本轮有没有查"，于是 IDLE（没查）和"彻底挂了"
        显示完全一样，两次 RAG 崩溃时它显示的都是 IDLE。"""
        with self._lock:
            cands = [
                replace(s) for s in self._states.values()
                if not s.ok and (_CAPABILITIES.get(s.capability) or CapabilitySpec("", "")).monitor_card == monitor_card
            ]
        if not cands:
            return None
        cands.sort(key=lambda s: Severity.rank(s.severity), reverse=True)
        return cands[0]

    def degraded_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._states.values() if s.status == Status.DEGRADED)

    def mark_presented(self, capability: str, generation: int) -> bool:
        """UI 已展示。带 generation 校验——防止把"新一代故障"错标成已展示。"""
        with self._lock:
            st = self._states.get(capability)
            if st is None or st.generation != generation or st.presented_at is not None:
                return False
            st.presented_at = time.time()
            return True

    def forget(self, capability: str) -> None:
        """彻底忘掉一个能力（它不存在了，不是"恢复了"）。

        ⚠️ 与 `recover()` 是两件事，别合并：
           `recover` 说的是"它好了" —— 会入队 RECOVERED、可能弹恢复提示；
           `forget` 说的是"这个能力已经不在这台机器上了"（用户把 MCP server 删了），
           **不该产生任何"已恢复"的说法** —— 那是假话。
        """
        with self._lock:
            self._states.pop(capability, None)
            self._probes.pop(capability, None)
            self._probe_sched.pop(capability, None)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.snapshot() for s in self._states.values()]

    # ── 模型侧注入：能力边界（状态）────────────────────────────────────────

    def render_capability_notice(self) -> str:
        """拼进 [Current Capability Boundary] 的一段。没有问题时返回空串。

        只给状态与用户可读信息，不给 technical_detail（路径/堆栈对模型是噪音，
        而且容易让它去"排查系统错误"而不是完成用户的任务）。
        """
        bad = self.problems()
        if not bad:
            return ""
        lines = ["", "[Capability Health — Current]"]
        for s in bad:
            spec = _CAPABILITIES.get(s.capability)
            label = spec.label if spec else s.capability
            # 能力自带的"所以你该怎么办"优先——通用措辞对某些能力是错的
            # （预算耗尽跟"别调相关工具"无关）。
            hint = spec.notice_hint if spec and spec.notice_hint else ""
            # ⭐ 恢复建议也要注入 —— 实测：Nano 只会说「这个能力坏了」，
            #    **说不出怎么修**，因为我们从来没把 `recovery_hint` 给过它。
            #    📌 「知道坏了」和「能帮用户修」差一整层；后者才是数字生命体。
            # ⚠️ 用英文那份（`recovery_hint_en`）。中文那份是给用户的 UI 文案，
            #    注进 system 里既违反原则 8.5，又会诱导模型用中文回一个英文块。
            #
            # 🔴🔴 **第二轮实测又抓到一条**（2026-08-21）：给了修复建议之后，
            #    Nano 说得出 `pip install …`，却补了一句「这是系统层面的，我无法帮你修复」。
            #    **那是我们的提示词写的，不是模型的性格** —— 上一版这里写的是
            #    "Relay this to the user"，字面意思就是「你的职责是把话传过去」。
            #    ⚠️ 而 Nano **有 `os_execute`**：`pip install` 它自己就能跑。
            #    📌 **一句把模型定位成「传话筒」的措辞，会让它主动放弃它真有的能力。**
            #    → 改成按「这一步我能不能自己做」分流，并把行动那句放在**最后**
            #      （最后一句是模型最容易照做的那句）。
            fix = (
                f"To fix it: {s.recovery_hint_en} "
                "Check whether that fix is something you can carry out yourself with your "
                "own tools - running a shell command, installing a package, editing a config "
                "file all count. If it is, say so, offer to do it, and do it once the user "
                "agrees. Only hand it back to them when it genuinely needs a human: clicking "
                "something in this app's own UI, a credential, or a decision that is theirs. "
                "Do NOT describe a fix you could run as being out of your reach."
            ) if s.recovery_hint_en else ""
            if s.status == Status.UNAVAILABLE:
                lines.append(
                    f"- {label}: UNAVAILABLE. {s.user_message} "
                    # ⚠️ 「related」太宽，会被读成「跟这件事有关的都别碰」——
                    #    包括修它的那条命令。收窄成「依赖它的那些工具」。
                    + (hint or "Do not call the tools that depend on it; tell the user plainly "
                               "if they ask for it.")
                    + " " + fix
                )
            elif s.status == Status.RECOVERING:
                lines.append(f"- {label}: RECOVERING. {s.user_message} Results may be incomplete right now.")
            else:
                lines.append(
                    f"- {label}: DEGRADED. {s.user_message} "
                    + (hint or "Still usable, but quality or coverage is reduced — say so if it matters to the answer.")
                    + " " + fix
                )
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# RecentSystemEvents —— 让模型知道"刚才发生了什么"
# ══════════════════════════════════════════════════════════════════════════

class RecentSystemEvents:
    """有界系统事件流，每轮注入 system prompt 的动态段。

    刻意不写进 MemoryManager（原因见模块头）。另外用 assistant 角色写历史还有个
    角色错位问题：模型会把系统事件误当成"这是我自己说的/我自己做的"——项目里
    已经吃过这个亏（UI 侧删除 Skill 时不得不额外写一长串"这不是在当前对话里执行的"
    来源说明）。系统事件不该继续复制这种错位。
    """

    def __init__(self, maxlen: int = 20, window_seconds: float = 6 * 3600):
        self._lock = threading.RLock()
        self._events: deque[tuple[float, str]] = deque(maxlen=maxlen)
        self._window = window_seconds

    def add(self, text: str) -> None:
        if not text or not text.strip():
            return
        with self._lock:
            self._events.append((time.time(), text.strip()))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def render(self) -> str:
        """拼进 system prompt 的一段。没有事件时返回空串。"""
        now = time.time()
        with self._lock:
            items = [(ts, t) for ts, t in self._events if now - ts <= self._window]
        if not items:
            return ""
        lines = ["", "[Recent System Events]",
                 "Things that happened in this process, not things you said or did. "
                 "Use them only if the user asks what just happened."]
        for ts, text in items:
            lines.append(f"- {time.strftime('%H:%M', time.localtime(ts))} {text}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# 进程级单例
# ══════════════════════════════════════════════════════════════════════════

_registry: HealthRegistry | None = None
_events: RecentSystemEvents | None = None
_singleton_lock = threading.Lock()


def get_health() -> HealthRegistry:
    global _registry
    if _registry is None:
        with _singleton_lock:
            if _registry is None:
                _registry = HealthRegistry()
    return _registry


def get_system_events() -> RecentSystemEvents:
    global _events
    if _events is None:
        with _singleton_lock:
            if _events is None:
                _events = RecentSystemEvents()
    return _events


# ── 便捷入口（给 rag.py / mcp_client.py 这类"出错的地方"用，一行就能上报）────

def report_fault(capability: str, code: str, user_message: str, *,
                 detail: str = "", hint: str = "", hint_en: str = "",
                 severity: str = Severity.ERROR) -> None:
    """⚠️ 给了 `hint` 就必须给 `hint_en` —— 用户看中文那份，模型看英文那份。
    `tests/cases/t_d11_mcp_health.py` 会 AST 扫全仓，漏一个就红。"""
    get_health().report(capability, status=Status.UNAVAILABLE, severity=severity,
                        code=code, user_message=user_message,
                        recovery_hint=hint, recovery_hint_en=hint_en,
                        technical_detail=detail)


def report_degraded(capability: str, code: str, user_message: str, *,
                    detail: str = "", hint: str = "", hint_en: str = "") -> None:
    get_health().report(capability, status=Status.DEGRADED, severity=Severity.WARNING,
                        code=code, user_message=user_message,
                        recovery_hint=hint, recovery_hint_en=hint_en,
                        technical_detail=detail)


def report_ok(capability: str, *, note: str = "") -> None:
    get_health().recover(capability, note=note)
