# core/tools/catalog.py
"""Unified Tool Catalog —— 「一个工具到底是什么」的唯一权威。

═══ 这一层要解决什么 ═══

改造前，新增一个工具要在 **11 个地方**分别登记，而且**没有一处会报错**：
manifest 池 / 感知块 / 延迟感知行 / 工具卡文案 / 「模型打算做什么」文案 /
load_tools 关键词表 / 串行表 / 并行表 / exit 表 / 合法工具名 / 探索作用域清单 /
os_execute action 清单。漏一处的表现各不相同（模型看不见它 / 卡片显示裸名 /
load_tools 搜不到 / 并发语义错 / 「看得见但执行不了」）。

⭐ 两条**当时就已经坏了**的实证（不是"将来会漏"）：
  1. 五个工具同时在 `_REACT_SERIAL_TOOLS` 与 `_REACT_EXIT_TOOLS` 里，
     而分类函数先判 EXIT 就 return —— **它们声明的 serial 永远不生效，且不报错**。
  2. `_OS_ACTIONS` 手抄 29 个 action，而 `dsl.py` 实际有 39 个 —— **漏 10 个**
     （含 `file_delete` / `file_move` / `write_registry` 这种高频项）。
     📌 **一份手抄的清单，过期是静默的，而且专挑高频项漏。**

═══ 问题定义（本模块的验收标准）═══

  任何工具能力只能有一个身份与执行契约权威；模型可见性、按需加载、检索、
  UI 呈现、并发调度、控制流与执行作用域必须全部从该权威派生。
  任何绕过该权威建立第二份工具名事实的代码，都由结构性断言拒绝。

═══ 核心不变量 ═══

    eligible(scope, runtime) ⊆ resolvable(scope)
    active_schema      ⊆ eligible
    deferred_awareness ⊆ eligible

⚠️ **不写成 `visible == executable`**：`load_tools` 只给 schema、**不决定能不能执行**，
   所以 deferred 工具「schema 当前没附带但仍可执行」是**正常状态**，
   旧措辞会把这种正常状态判成违规。

⚠️⚠️ **也不写成 `eligible == resolvable`（切换时更正）。** 那个写法要求
   「执行得了的必须给出去」，而**反向差集是正常且必要的**，两类实例：
     · `Preload.HIDDEN` —— 处理得了，但刻意不告诉模型（`WriteSkill`）；
     · `availability` 为假的条件注入工具 —— 现在不该出现在清单里，
       但模型凭历史里的旧 schema 再调一次时，**仍然要执行**，
       由 handler 回一句正确的「你要回答的那条待办已经没了」。
   🔴 让 `resolve` 跟着 `availability` 走，那句话会变成「没有叫这个名字的工具」
      —— **假话**，违反「给模型的失败信息必须正确」这条。
   ⭐ 真正要守的方向只有一个：**给出去的一定执行得了**（`eligible ⊆ resolvable`），
      那正是「模型看得见但执行不了」那类事故的反面。

⭐ 而 `eligible` 天然由 `bindings` + `availability` 派生 ——
   **只有一张名单**，所以「模型看得见但执行不了」在结构上不可能。
   AST 断言只负责**防后人绕开本层自建旁路**，不负责比对两张名单。

═══ 依据 ═══

早先设计里关于统一注册表与工具描述截断的那两条；
经两轮外部评审；
实现以本文件为准。
"""
from __future__ import annotations

from loguru import logger

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence


# ── 正交维度 ────────────────────────────────────────────────────────────────

class ToolOrigin(str, Enum):
    """这个工具的身份权威在哪里。"""
    BUILTIN = "builtin"   # 权威 = BUILTIN_TOOLS 声明（我们自己的代码）
    SKILL = "skill"       # 权威 = SkillRegistry（Skill 自己的 get_manifest/get_spec）
    MCP = "mcp"           # 权威 = 远端 tools/list


class Scheduling(str, Enum):
    """这一次调用与**同一批**其他调用能否共存、如何排程。

    ⚠️ 与 `Flow` 正交：`flow` 答「调用之后去哪」，`scheduling` 答「能不能和别人同批」。
    """
    PARALLEL = "parallel"
    SERIAL = "serial"
    # ⭐ EXCLUSIVE 不是「串行」——它的真实语义是：
    #    **与任何其他工具同轮出现 → 整批一个都不执行、全部写 error、模型下一轮重选。**
    #    改造前这类工具被写成 SERIAL，而那是一句**假声明**（真实行为是整批拒绝）。
    #    📌 一个字段填了、但描述的不是真实语义，等于又造一个死声明。
    EXCLUSIVE = "exclusive"


class Flow(str, Enum):
    """调用之后，控制流去哪。"""
    CONTINUE = "continue"      # 回到主 ReAct 循环
    EXIT_REACT = "exit_react"  # 退出主循环，进入专属状态机（Explorer / 审计流 等）


class Preload(str, Enum):
    """这个工具**怎么被告知给模型**。

    ⚠️ DEFERRED **不等于**「不能执行」—— 它只表示 schema 不随每轮附带，
       模型需要时先 `load_tools`。执行资格由 `bindings` + `availability` 决定。

    ⭐⭐ HIDDEN 是切换时补上的第三档，它描述的是一个**改造前就存在、
       但当时没有名字**的状态：**这个作用域处理得了它，却刻意不告诉模型它存在。**

       实例是 `WriteSkill`：主循环里有它的处理分支（模型偶尔会幻觉出这个名字，
       那一支带 `logger.warning` 把它转去 `_emit_skill_preview_from_decision`），
       但三个 `_build_skills_info` 调用点**全部**传 `include_write=False` ——
       它的 schema 只在 SkillWriter 那一步被**直接**递给 provider
       （`orchestrator.py` 的两处 `[_WRITE_SKILL_MANIFEST]`），从不进主决策工具池。

       🔴 **为什么必须补这一档，而不是让它落进 DEFERRED**：DEFERRED 会进感知块、
          也能被 `load_tools` 拉出来 —— 于是模型可以**故意**在主循环调 `WriteSkill`，
          绕开「探索 → 确认 → SkillSpec → 生成」整条链直接出草稿。
          那不是等价切换，那是**放开一条它今天没有的路**。
       📌 判据同 `_INTENT_IS_CONCLUDE_NAMES` 那张表的注释原话：
          「**它们刻意不在 manifest 里：模型不该看见它们。登记只是为了『万一幻觉出来了，
          认得出它的意图』，而不是给它一条正式通路。**」
          —— 那句话描述的就是本档，改造前它是一张手写 frozenset，现在它是一个字段。
    """
    CORE = "core"
    DEFERRED = "deferred"
    HIDDEN = "hidden"


class ToolScope(str, Enum):
    """执行作用域。不同作用域**真的有不同的 handler 集合**。

    出过一次事故就在这：主决策的工具清单泄漏进探索子循环，
    而那个子循环根本没有对应 handler → 模型调了就掉进「未处理的 call」死路。
    """
    MAIN = "main"                # 主决策 ReAct 循环
    EXPLORATION = "exploration"  # Skill 探索子循环
    SKILL_WRITER = "skill_writer"
    OS_LOOP = "os_loop"
    # ⭐⭐ Subagent。**默认零工具，要一个一个显式授予。**
    #
    # 🔴 早先的设计写的是排除法：「把 `create_task_list` / `update_task_step`
    #    / `ask_user_choice` / `set_window_mode` / `render_visual` 从Subagent工具集里
    #    **摘掉**即可」。那句话写在统一注册表落地之前，**现在必须改口**：
    #    按排除法做，每加一个内置工具，Subagent的名单就欠一笔，
    #    **而且欠的时候不会报错** —— Subagent会安安静静拿到一个它不该有的工具。
    #
    # ⭐ 而 `is_eligible` 第一条就是 `if scope not in self.bindings: return False`，
    #    所以新工具只声明 `MAIN` 就**天然不在Subagent里**。
    # 📌 **排除法要求你记得每一个新东西；白名单只要求你记得你想要的那几个。
    #    前者的欠账随时间增长，后者不会。**
    #    （2026-08-14 曾担心后续新增工具会给Subagent留欠账 —— 担心是对的，
    #      但统一注册表已经把它解掉了。）
    AGENT = "agent"


# ── 运行时可用条件 ──────────────────────────────────────────────────────────

class ToolRuntimeView(Protocol):
    """`availability` 能看到的运行时事实（只读）。

    ⚠️ 刻意做成窄接口而不是直接传 orchestrator：
       一个 predicate 能拿到整个 orchestrator，就迟早有人在里面写副作用。
    """
    def has_live_work(self) -> bool: ...
    def has_open_interaction(self) -> bool: ...
    def is_recheck_round(self) -> bool: ...
    def has_detachable_carrier(self) -> bool: ...
    def has_unsummarized_image(self) -> bool: ...
    def has_evicted_history(self) -> bool: ...


Availability = Callable[[ToolRuntimeView], bool]


def ALWAYS(_rt: ToolRuntimeView) -> bool:
    """默认可用条件。绝大多数工具用它。"""
    return True


# ── 用户可见文案 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DetailBlock:
    """工具卡展开后给用户看的**一块**内容。

    ⚠️ 它是**给人看的**，不是给模型看的 —— 与上下文治理层正好相反：
       上下文治理层管「给模型留多少」（把工具结果换成占位符省上下文），
       本模块管「给用户留多少」（要的正是那份原文）。
    📌 两者不冲突，**因为它们读的不是同一本账**：
       上下文治理层改 `MemoryManager.storage`（投影），本模块的消费方读
       `conversation_messages`（事实）。
       🔴 **谁把展开接到 storage 上，谁就会在 L1 降级后看到满屏
          「[Tool output aged out...]」—— 而透明度就白做了。**
    """
    label: str                 # 「命令」「输出」「路径」——给人看的小标题
    body: str                  # 正文
    kind: str = "text"         # text | code | error
    lang: str = ""             # kind=code 时的高亮语言（""=纯文本）


# 单块正文的显示上限。⚠️ **这是显示策略，不是数据策略** ——
# 📌 原文一个字都没少，还在 `conversation_messages` 里，导出拿得到全份。
#    这里截断只是不让一次 40 万字符的 stdout 把聊天区拖垮。
MAX_DETAIL_CHARS = 4000
MAX_DETAIL_LINES = 60


def clip(text: str) -> str:
    """按行/字符截断，并**说清楚省了多少** —— 不许静默截断。

    📌 一个没说自己被截断过的显示，会让用户以为那就是全部
       （同 `_settle_tool_pill` 不许显示 0.0s 那条纪律：
        比"不好看"更糟的是"读起来像另一回事"）。
    """
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    _clipped_lines = len(lines) > MAX_DETAIL_LINES
    if _clipped_lines:
        head = lines[:MAX_DETAIL_LINES - 15]
        tail = lines[-15:]
        text = "\n".join(head) + f"\n\n… 中间省略 {len(lines) - MAX_DETAIL_LINES + 15} 行 …\n\n" + "\n".join(tail)
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + f"\n\n… 还有 {len(text) - MAX_DETAIL_CHARS:,} 个字符（完整内容在「设置 → 导出数据」里）"
    return text


def default_detail(args: dict, result: Any) -> list["DetailBlock"]:
    """**通用兜底**：任何没写专属渲染器的工具，也有基本透明度。

    ⭐⭐ 这个兜底是本次改造的**主要产出**，不是配角：
       改造前 `Presentation` 只有 `card` / `intent`，**压根没有「详情」这个概念**，
       于是"能不能看到做了什么"取决于每个工具作者的自觉 —— 27 个里只有
       Skill 审计卡那一个有。
    📌 **让「有详情」成为默认，而不是每个新工具的自觉** ——
       这正是「新工具必须完整接入」这条原则在这一层的兑现。
    """
    import json as _json
    out: list[DetailBlock] = []
    if args:
        try:
            _a = _json.dumps(args, ensure_ascii=False, indent=2, default=str)
        except Exception:
            _a = str(args)
        out.append(DetailBlock("参数", clip(_a), kind="code", lang="json"))
    _c = getattr(result, "content", None) if result is not None else None
    if isinstance(_c, str) and _c.strip():
        _err = bool(getattr(result, "is_error", False))
        out.append(DetailBlock("错误" if _err else "结果", clip(_c),
                               kind="error" if _err else "text"))
    return out


@dataclass(frozen=True)
class Presentation:
    """用户可见文案的**唯一**来源，**三个**渲染上下文。

    🔴 改造前这是**两份独立的表**：一份给工具卡片、一份给「模型打算做什么」，
       同一个工具在两处各写一遍中文，而且会漂移。
    ⭐ 2026-08-14 加第三个：`detail` —— 工具卡展开后看到的内容。
       缺省走 `default_detail`（参数 + 结果），所以**没写专属渲染器的工具
       也不再是一片空白**。
    """
    card: Callable[[dict], str]
    # 缺省时由 card 派生 —— 📌 一个能从已有数据推出来的东西，不该要求再填一遍。
    intent: Optional[Callable[[dict], str]] = None
    # 缺省走 `default_detail` —— 同上，但方向相反：
    # 📌 `intent` 缺省是"少写一遍"，`detail` 缺省是**"不许什么都没有"**。
    detail: Optional[Callable[[dict, Any], list[DetailBlock]]] = None

    def render_card(self, args: dict) -> str:
        return self.card(args or {})

    def render_intent(self, args: dict) -> str:
        fn = self.intent or self.card
        return fn(args or {})

    def render_detail(self, args: dict, result: Any = None) -> list[DetailBlock]:
        """展开后该看到什么。**永不抛** —— 一个渲染不出来的详情，
        不许把整条历史消息的重放搞挂。
        """
        try:
            if self.detail is not None:
                _out = self.detail(args or {}, result)
                if _out:
                    return list(_out)
            return default_detail(args or {}, result)
        except Exception as e:
            logger.debug(f"详情渲染失败，退回通用兜底: {e}")
            try:
                return default_detail(args or {}, result)
            except Exception:
                return []


# ── 执行绑定 ────────────────────────────────────────────────────────────────

# Handler 用**方法名字符串**而不是函数对象，有两个理由：
#   ① 这些 handler 是 Orchestrator 的方法，而声明表在模块级 —— 类还没定义完时拿不到函数对象；
#   ② 字符串可以被**构造期断言**核实（"这个方法在 Orchestrator 上真的存在吗"），
#      于是「绑了一个不存在的 handler」会在启动时响亮失败，而不是等模型调用时才炸。
# ⚠️ 它**不是**第二份事实：binding 本身就是这份声明要表达的东西。
HandlerRef = str


class ToolDefinitionError(ValueError):
    """构造期失败。**故意让它在启动时抛出**，不许静默降级。"""


@dataclass(frozen=True)
class ToolDefinition:
    """一个工具的完整身份与契约。**这是唯一权威。**"""
    name: str
    origin: ToolOrigin
    manifest: dict                          # Anthropic tool schema
    awareness: str                          # 一行感知：什么时候值得 load
    presentation: Presentation
    scheduling: Scheduling
    flow: Flow
    preload: Preload
    bindings: Mapping[ToolScope, HandlerRef]
    availability: Availability = ALWAYS
    search_terms: tuple[str, ...] = ()

    # 缓存：检索文档（懒构建，见 search_document()）
    _search_doc: list = field(default_factory=list, compare=False, repr=False)

    def __post_init__(self) -> None:
        # ── 构造期不变量：缺任何必填项 → 立刻失败 ──────────────────────────
        # 📌 fail fast 的意义：一个「漏填」如果能撑到运行时，它就会以
        #    「模型行为怪异」的形式出现，而那是最难定位的一类问题。
        if not self.name:
            raise ToolDefinitionError("工具必须有 name")
        mname = (self.manifest or {}).get("name")
        if mname != self.name:
            raise ToolDefinitionError(
                f"{self.name}: manifest['name']={mname!r} 与 definition.name 不一致 —— "
                f"schema 与身份必须同源")
        if not (self.awareness or "").strip():
            raise ToolDefinitionError(
                f"{self.name}: awareness 必填。⚠️ 它**不许**由 description 截断得到 —— "
                f"根因正是拿字符串截断冒充语义摘要")
        if not self.bindings:
            raise ToolDefinitionError(
                f"{self.name}: bindings 不能为空 —— 一个谁都执行不了的工具不该存在")
        # ⚠️ availability 恒 False **不是**错误：那是合法的「当前不该出现」。

    # ── 资格判定 ────────────────────────────────────────────────────────────

    def is_eligible(self, scope: ToolScope, runtime: ToolRuntimeView) -> bool:
        """这个工具此刻在这个作用域里**该不该存在**。

        ⭐ 两个条件缺一不可：
          · `scope in bindings` —— 这个作用域有没有**执行能力**
          · `availability(runtime)` —— **现在**该不该出现
        📌 后者不是 token 优化：没有 live work 时给出 `task_boundary`，
           模型会「找一件不存在的事来结束」。
           而「一个工具和它的事实来源必须由同一个条件控制」这条判据，
           在改造前就已经写在 `_rt_ongoing_work` 的注释里了。
        """
        if scope not in self.bindings:
            return False
        try:
            return bool(self.availability(runtime))
        except Exception:
            # fail-safe 方向：算不出来就**不给** —— 多一个工具的代价是模型乱调，
            # 少一个的代价只是这一轮用不上它。
            return False

    def handler_ref(self, scope: ToolScope) -> Optional[HandlerRef]:
        return self.bindings.get(scope)

    # ── 检索 ────────────────────────────────────────────────────────────────

    def search_document(self) -> str:
        """自动构建的检索文档 —— 替代改造前那张**人工维护的关键词分组表**。

        ⭐ 关键收益：`os_execute` 的 39 个 action enum 会自然进入索引，于是
           `load_tools(query="delete_file")` 能精准命中 `os_execute`，
           **而 awareness 行一个 action 都不用列**。
        """
        if self._search_doc:
            return self._search_doc[0]
        parts: list[str] = [self.name, self.awareness, *self.search_terms]
        m = self.manifest or {}
        parts.append(str(m.get("description", "")))
        params = m.get("parameters") or m.get("input_schema") or {}
        props = params.get("properties") or {}
        for pname, pdef in props.items():
            parts.append(str(pname))
            if isinstance(pdef, dict):
                parts.append(str(pdef.get("description", "")))
                # ⭐ enum 值是这里最有价值的东西（os_execute 的 39 个 action 就在这）
                for ev in (pdef.get("enum") or []):
                    parts.append(str(ev))
        doc = " ".join(p for p in parts if p).lower()
        self._search_doc.append(doc)
        return doc


# ── 名字规范化 ──────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


# ── 文案兜底（改造前散在两个函数的 else 分支里）──────────────────────────────
#
# ⚠️ 这三个兜底**必须与改造前逐字一致**。它们没有被早先那张「20 个工具卡
#    逐字对拍」覆盖到 —— 那张表只对了**内置**工具的 card，于是三条兜底
#    （MCP / 本地 Skill / 完全未知的名字）和**全部 intent** 都在对拍范围之外。
# 📌 **一张对拍表漏了什么，切换就会漂什么。** 这一条是切换时核出来的，
#    修法是「补齐兜底 + 把它们一起纳入对拍」，两件事必须同时做，
#    否则同一个洞下次还会再来一次。

MCP_PREFIX = "mcp__"


def is_mcp_name(name: str) -> bool:
    return isinstance(name, str) and name.startswith(MCP_PREFIX)


def mcp_card_text(name: str) -> str:
    """`mcp__server__tool` → `外部能力：server · tool`。

    ⚠️ 用户**不区分内置能力和外部能力**，统一感知为「Nano 的能力」，
       所以这里把命名空间拆开显示，不把 `mcp__` 这个内部前缀摆给用户看。
    """
    rest = name[len(MCP_PREFIX):]
    srv, _, tl = rest.partition("__")
    return f"外部能力：{srv} · {tl}" if tl else f"外部能力：{rest}"


def _generic_card_text(name: str) -> str:
    """工具卡兜底：MCP 形状的拆前缀，其余显示「执行 X」。"""
    return mcp_card_text(name) if is_mcp_name(name) else f"执行 {name}"


def _generic_intent_text(name: str) -> str:
    """「模型打算做什么」兜底。

    ⚠️ 与 card 兜底**故意不同**：card 答「正在做什么」，intent 答「打算做什么」，
       它们是两个渲染上下文，不是同一句话的两种写法。
    """
    return f"调用工具「{name}」"


def _skill_intent_text(name: str, description: str) -> str:
    """本地 Skill 的 intent：取 manifest 描述的第一句，让用户能看懂。

    ⚠️ 取法必须与改造前逐字一致（先切「。」再切换行），否则用户看到的话会漂。
    """
    desc = (description or "").strip()
    if desc:
        desc = desc.split("。")[0].split("\n")[0].strip()
        if desc:
            return f"调用「{name}」（{desc}）"
    return _generic_intent_text(name)


# ── 冲突处置 ────────────────────────────────────────────────────────────────

class CatalogConflict(ToolDefinitionError):
    """内置工具之间重名 —— 我们自己的代码写错了，必须当场修。"""


@dataclass(frozen=True)
class RejectedTool:
    """被隔离的动态工具（Skill / MCP）。

    ⚠️ **失败作用域必须落在肇事来源上**：一条用户 Skill 或一个外部 MCP server 撞名，
       **不许把整个 Nano 启动打死**。
       📌 响亮的是**报警**，不是**自杀**。
    """
    name: str
    origin: ToolOrigin
    reason: str


# ── Catalog ─────────────────────────────────────────────────────────────────

class ToolCatalog:
    """工具目录：所有关于「有哪些工具、它们是什么」的问题都问它。

    ⚠️ 它**不执行**工具 —— 只回答「谁处理」。
       「怎么运行这一类 handler」是 Flow Runner 的事（统一 plumbing 留在那边：
       GUI 租约等待 / health fail-fast / 工具卡 / 长任务交还 / 错误分类 /
       EXIT 路径的同轮拒绝与终端事件收尾）。
       📌 **Catalog 决定"谁处理"，Runner 决定"怎么运行"** —— 混在一起会让
          这次改动同时承担「统一注册」和「重写 2000 行执行逻辑」两件事。
    """

    def __init__(self) -> None:
        self._defs: dict[str, ToolDefinition] = {}
        self._rejected: list[RejectedTool] = []

    # ── 装配 ────────────────────────────────────────────────────────────────

    def add_builtin(self, definition: ToolDefinition) -> None:
        """内置工具。重名 = **我们自己写错了** → fatal。"""
        if definition.name in self._defs:
            raise CatalogConflict(
                f"内置工具重名: {definition.name} —— 两处声明必须合成一处")
        self._defs[definition.name] = definition

    def add_dynamic(self, definition: ToolDefinition) -> bool:
        """动态工具（Skill / MCP）。冲突 → **隔离这一个**，不影响启动。

        返回 True = 收下；False = 被隔离（原因进 `rejected()`）。
        ⚠️ 绝不 silent later-wins —— 改造前 `_build_skills_info` 的 dedup 就是
           `deduped[seen[name]] = item`（后来的静默覆盖前面的，不报错）。
        """
        exist = self._defs.get(definition.name)
        if exist is not None:
            self._rejected.append(RejectedTool(
                definition.name, definition.origin,
                f"与已存在的 {exist.origin.value} 工具重名，已隔离（不覆盖）"))
            return False
        self._defs[definition.name] = definition
        return True

    def rejected(self) -> list[RejectedTool]:
        """被隔离的动态工具。调用方**必须**把它报进健康登记表（响亮但不致命）。"""
        return list(self._rejected)

    def assert_bindings_exist(self, owner: Any) -> None:
        """**每个 binding 指向的方法都必须在 owner 上真实存在** —— 否则启动就失败。

        ⭐ 这是「让漏一处结构上不可能」的最后一环：binding 用**方法名字符串**
           （模块级声明表拿不到还没定义完的类方法），而字符串写错在运行时才炸，
           那时表现为「模型调了某个工具，然后什么都没发生」。
        📌 **一个只能在运行时暴露的错误，要在启动时把它变成响亮失败。**
        ⚠️ **查全部来源，不只内置。** 改造前这里 `continue` 掉了 Skill/MCP，
           理由写的是「它们的 handler 是固定那两个，由内置那一轮顺带保证」——
           而那句话是错的：那两个名字（`_handle_local_skill`/`_handle_mcp_tool`）
           **在 Orchestrator 上压根不存在**，恰恰因为被跳过才没人发现。
           📌 **一条断言给自己开的例外，往往正是错误藏身的地方。**
        """
        missing: list[str] = []
        for d in self._defs.values():
            for scope, ref in d.bindings.items():
                if not hasattr(owner, ref):
                    missing.append(f"{d.name}[{scope.value}] → {owner.__class__.__name__}.{ref}")
        if missing:
            raise ToolDefinitionError(
                "以下 binding 指向不存在的方法（写错名字或方法被删/改名）：\n  "
                + "\n  ".join(missing))

    def clear_dynamic(self) -> None:
        """重建动态部分（Skill 热加载 / MCP 重连）前调用。内置声明不动。"""
        self._defs = {n: d for n, d in self._defs.items()
                      if d.origin is ToolOrigin.BUILTIN}
        self._rejected.clear()

    # ── 查询：所有旧名单都退化成这里的派生视图 ──────────────────────────────

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._defs.get(name)

    def names(self) -> frozenset[str]:
        """全部合法工具名。← 取代 `_known_tool_names()` 那个「四个来源缺一不可」的手工拼接。"""
        return frozenset(self._defs)

    def eligible(self, scope: ToolScope, runtime: ToolRuntimeView) -> list[ToolDefinition]:
        """此刻在这个作用域里**执行得了**的工具。

        ⭐ 这是**唯一**的名单。`active_schema` 与 `deferred_awareness` 都是它的子集，
           所以「看得见但执行不了」结构上不可能。
        ⚠️ 注意方向：它答的是「**能不能执行**」，不是「**要不要告诉模型**」——
           后者由 `preload` 答（HIDDEN 就是能执行但不告知）。
           反过来是不成立的：不能执行却告诉模型，那正是那类事故。
        """
        return [d for d in self._defs.values() if d.is_eligible(scope, runtime)]

    def advertised(self, scope: ToolScope, runtime: ToolRuntimeView) -> list[ToolDefinition]:
        """此刻**该让模型知道存在**的工具 = eligible 去掉 HIDDEN。

        ← 取代 `_build_skills_info()`（那 11 个 include_* 开关的合力结果）。
        """
        return [d for d in self.eligible(scope, runtime)
                if d.preload is not Preload.HIDDEN]

    def core_manifests(self, scope: ToolScope, runtime: ToolRuntimeView) -> list[dict]:
        """每轮**常驻**的 schema。← 取代手写的 `_CORE_TOOL_NAMES` + `_core_manifest`。"""
        return [d.manifest for d in self.eligible(scope, runtime)
                if d.preload is Preload.CORE]

    def deferred(self, scope: ToolScope, runtime: ToolRuntimeView) -> list[ToolDefinition]:
        """只发一行感知、schema 按需取的工具。← 取代 `_tool_pool` / `_build_deferred_awareness`。"""
        return [d for d in self.eligible(scope, runtime)
                if d.preload is Preload.DEFERRED]

    def resolve(self, name: str, scope: ToolScope,
                runtime: ToolRuntimeView) -> Optional[HandlerRef]:
        """谁来处理它。判据是**这个作用域有没有 binding**。

        ⚠️⚠️ **刻意不看 `availability`。** 这一条是切换时被回归测试
        当场抓出来的，值得写清楚：

        `availability` 答的是「**现在该不该出现在工具清单里**」，
        它**不**答「模型真调了的话要不要执行」。三个受它管的工具，
        改造前**都**有处理"叫来了但没有对象"的分支，而且给的是**正确的那句话**：
          · `answer_open_interaction` → 「这条待办不存在」
          · `task_boundary`           → 标失败 +「不许向用户宣布完成」
          · `set_next_checkin`        → 没有回看对象时的兜底

        🔴 一旦让 `resolve` 跟着 `availability` 走，这三个工具在"没有对象"时会
        **解析不到 handler** → 落进 UNKNOWN_TOOL 诊断 → 模型收到
        「**没有叫这个名字的工具**」。**那是假话**，而且违反
        「给模型的失败信息必须正确」：「不存在」和「现在没有对象」是两回事。
        📌 模型完全可能凭历史里的旧 schema 再调一次 —— 那时它需要的是
           「你要回答的那条待办已经没了」，不是「查无此工具」。

        ⭐ 所以核心不变量的精确形式是 **`eligible ⊆ resolvable`**：
           「给出去的一定执行得了」（要的就是这一条），
           而反向不成立是**正常且必要的** —— HIDDEN 与"条件注入但仍可执行"
           都活在这个差集里。
        """
        d = self._defs.get(name)
        if d is None:
            return None
        return d.handler_ref(scope)

    def presentation(self, name: str, args: dict, *, intent: bool = False) -> str:
        """用户可见文案。← 取代 `_tool_action_display` 与 `_describe_decision_for_user` 两张表。

        ⚠️ **名字不在目录里时不许退回裸工具名** —— 改造前两条路各有自己的兜底
           （卡片是 `执行 X` / MCP 拆前缀，intent 是 `调用工具「X」`），
           退回裸名会让用户在工具卡上看到一个光秃秃的英文标识符。
        📌 兜底也是用户可见文案，它一样要逐字保。
        """
        d = self._defs.get(name)
        if d is None:
            return (_generic_intent_text(name) if intent
                    else _generic_card_text(name))
        try:
            return (d.presentation.render_intent(args) if intent
                    else d.presentation.render_card(args))
        except Exception:
            # 文案永远不许把主流程搞崩 —— 退回与「不认识这个名字」同一条兜底。
            return (_generic_intent_text(name) if intent
                    else _generic_card_text(name))

    def detail(self, name: str, args: dict, result: Any = None) -> list[DetailBlock]:
        """工具卡展开后该看到什么。**唯一出口** —— live 与重放都问它。

        ⚠️ 名字不在目录里时**照样给兜底**（`default_detail`）：
           📌 与上面 `presentation` 同一条纪律 —— 兜底也是用户可见内容。
           而这里更重要：一个我们不认识的工具（Skill / MCP），
           恰恰是用户**最需要看到它到底做了什么**的那一类。
        """
        d = self._defs.get(name)
        if d is None:
            return default_detail(args or {}, result)
        return d.presentation.render_detail(args or {}, result)

    # ── 渲染：延迟工具的感知块 ──────────────────────────────────────────────

    # ⭐⭐⭐ 2026-08-23：**补上那句「这一轮」。**
    #
    # 🔴 实际运行中反复出现的形状：
    #      第一轮：load → 执行 ✅
    #      第二轮：**直接执行 → 报错 → 再 load → 再执行** 🔴
    #
    # ⭐ 根因不是「没告诉它要 load」（这段一直写着），而是
    #    **从来没人告诉它「这是新的一轮，上一轮的 load 不算数了」**。
    #    它的上下文里摆着一次**亲身成功过**的调用，而我们给的是一条泛泛的规则 ——
    #    📌 **一条规则打不过一次亲身成功的示范。**
    #
    # ⭐⭐ 修法：不去陈述「此刻加载着什么」
    #    （那个状态在同一轮里会变，写在 system 里第 3 步就成了**假信息**，
    #     📌 一个会过时的状态陈述比不陈述更坏），
    #    而是给一条**永不过时的规则**，并把那条错误推理**明确堵死**：
    #      · 规则：这一轮要用就得这一轮 load
    #      · 事实：每轮开始时手上只有核心工具（藏在规则里，不单独断言）
    #      · 🔴 直接掐掉：「就算你之前 load 过也一样」
    #
    # ⚠️ 这条**是结构性保证的，不是约定**：每轮 `tools_manifest` 从
    #    `_core_manifest` 重建，而 `_pending_loaded_manifests` 在
    #    `_run_react_loop` 开头清空 —— **跨轮存活在结构上不可能**。
    #    📌 所以这句话不会有「说了但其实有时能用」的风险。
    DEFERRED_HEADER = (
        "\n\n[More Capabilities — Load on Demand]\n"
        "Nano has these capabilities, but their full schemas are not loaded by default "
        "to reduce token cost. "
        "To use one, call load_tools first with query or exact names; then call the "
        "loaded tool on the next step.\n"
        "⚠️ Loading lasts for THIS TURN ONLY. Every turn starts with the core tools "
        "and nothing else. If you need one of the capabilities below in this turn, "
        "load it again now — even if you already loaded and used it earlier in this "
        "conversation. Seeing your own earlier successful call is NOT evidence that "
        "it is still attached.\n"
        "Never say Nano cannot do something only because the schema is not currently "
        "active. Load it first.\n"
    )

    def render_deferred_awareness(self, scope: ToolScope,
                                  runtime: ToolRuntimeView) -> str:
        """延迟工具的感知块。← 取代 `_build_deferred_awareness`（含它那个 `[:28]`）。

        🔴 改造前这里拿 **manifest description 做 `[:28]` 字符级截断**，
           `os_execute` 因此被切成 `Operate this computer: inspe`（切在词中间）——
           那就是那个根因。而同一个进程里另有一张**人工写好的**感知表
           （`_BUILTIN_TOOLS_AWARENESS`），**零调用方**，从来没到过模型面前。
        ⭐ 现在只有一处：`definition.awareness`，必填、人工写、不许由截断得到。
        """
        lines = [f"  - {d.name}: {d.awareness}"
                 for d in self.deferred(scope, runtime)]
        if not lines:
            return ""
        return self.DEFERRED_HEADER + "\n".join(lines)

    # ── 检索：取代 `_match_tools_to_load` 的人工关键词分组 ───────────────────

    def search(self, query: str = "", names: Sequence[str] = (), *,
               scope: ToolScope = ToolScope.MAIN,
               runtime: Optional[ToolRuntimeView] = None,
               limit: int = 25) -> list[ToolDefinition]:
        """`load_tools` 的匹配。确定性排序：精确名 > 名字子串 > 检索文档命中。

        ⚠️ 只在 eligible 集合里搜 —— 搜出一个当前不该出现的工具，
           等于把 `eligible` 那条不变量从后门破掉。
        ⚠️ **HIDDEN 的一律搜不到**：`load_tools` 的作用就是「把模型看得见的东西
           变成可调用的 schema」，而 HIDDEN 的定义正是「不告诉模型它存在」。
           能被 `load_tools` 拉出来的 HIDDEN，等于根本不是 HIDDEN。
        """
        pool = [d for d in (self.eligible(scope, runtime) if runtime is not None
                            else list(self._defs.values()))
                if d.preload is not Preload.HIDDEN]
        by_name = {d.name: d for d in pool}
        out: list[ToolDefinition] = []
        seen: set[str] = set()

        def _take(d: ToolDefinition) -> None:
            if d.name not in seen:
                seen.add(d.name)
                out.append(d)

        for n in (names or ()):
            d = by_name.get(n)
            if d is not None:
                _take(d)

        q = (query or "").strip().lower()
        if q:
            qtokens = _tokens(q)
            for d in pool:                                   # ① 精确名
                if d.name.lower() == q:
                    _take(d)
            for d in pool:                                   # ② 名字子串
                if q in d.name.lower():
                    _take(d)
            for d in pool:                                   # ③ 检索文档
                doc = d.search_document()
                if any(t in doc for t in qtokens if len(t) >= 2):
                    _take(d)
        return out[:limit]


# ── Source Adapter ──────────────────────────────────────────────────────────
#
# ⭐ 三类工具**各有各的原生权威**，Adapter 只做投影，不要求它们再登记一次：
#     BUILTIN → BUILTIN_TOOLS 声明（本身就是权威）
#     SKILL   → SkillRegistry（Skill 的 get_manifest/get_spec）
#     MCP     → 远端 tools/list
#   所以「45 个 MCP 工具」不是 45 条迁移，是 **1 条 Adapter**；
#   装一个新 Skill、接一个新 MCP server，都**不碰本模块任何代码**。


def _compress(text: str, limit: int = 110) -> str:
    """按**词/句边界**压缩，绝不做字符级截断。

    🔴 改造前两处字符级截断（`[:28]` / `[:50]`）正是那个根因：
       `os_execute` 的感知行被切成 `Operate this computer: inspe` ——
       📌 **拿字符串截断冒充语义摘要**，而模型据此只能靠猜。
    ⚠️ 展示摘要的长度**不影响可发现性** —— 检索永远走完整 search_document。
    """
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in (". ", "。", "; ", "；", ", ", "，", " "):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i].rstrip(" ,;，；") + "…"
    return cut.rstrip() + "…"


def skill_definitions(registry: Any) -> list[ToolDefinition]:
    """把已加载的本地 Skill 投影成 ToolDefinition。**Skill 作者永不填第二张表。**

    ⚠️ `scheduling` 由**代码强制** SERIAL，不由 Skill 自己声明 ——
       已定的规则：Skill 实例是**跨调用复用的单例**，`side_effects` 验的是
       「有无外部副作用」而并发要的是「有无共享可变状态」，两者不是同一个属性。
       📌 让模型自声明并发安全，比不验证更危险（它看起来合规）。
    """
    out: list[ToolDefinition] = []
    try:
        manifests = list(registry.get_all_manifests() or [])
    except Exception:
        return out
    for m in manifests:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        if not name:
            continue
        desc = str(m.get("description", "") or "")
        out.append(ToolDefinition(
            name=name,
            origin=ToolOrigin.SKILL,
            manifest=m,
            awareness=_compress(desc) or f"local Skill: {name}",
            # ⚠️ card 与 intent 都照抄改造前：卡片是 `执行 X`，而「模型打算做什么」
            #    要带上描述第一句（`调用「X」（这个 Skill 干什么）`）——
            #    用户看不懂一个 Skill 的名字，但看得懂那句描述。
            presentation=Presentation(
                card=lambda a, _n=name: f"执行 {_n}",
                intent=lambda a, _n=name, _d=desc: _skill_intent_text(_n, _d)),
            scheduling=Scheduling.SERIAL,
            flow=Flow.CONTINUE,
            preload=Preload.DEFERRED,
            # ⚠️ binding 指向 `_execute_one_tool_call` 本身，因为**"谁处理"现在
            #    确实是这个函数**：本地 Skill 与 MCP 的执行体是它里面的两段内联
            #    分支（它们除了 result_text 还要回填 raw_result / tool_data，
            #    契约比那批 `_handle_*` 宽，所以当初的机械提取没有覆盖它们）。
            # 📌 同探索作用域的 binding 指向 `_run_skill_exploration` 那个大循环 ——
            #    **目录如实描述"谁处理"，即使那个"谁"现在是一个大函数，
            #    也绝不为了好看虚构一层。**
            # 🔴 改造前这里写的是 `_handle_local_skill` / `_handle_mcp_tool`,
            #    **而 Orchestrator 上根本没有这两个方法** —— 目录里存着一句假话。
            #    它没被发现，是因为构造期断言当时只查内置来源。现在断言覆盖全部来源。
            bindings={ToolScope.MAIN: "_execute_one_tool_call"},
        ))
    return out


def mcp_definitions(mcp_manager: Any) -> list[ToolDefinition]:
    """把已连接 MCP server 的工具投影成 ToolDefinition。**接新 server 不碰本模块。**

    ⚠️ 同样强制 SERIAL：外部调用、副作用未知，稳妥优先。
    """
    out: list[ToolDefinition] = []
    try:
        if not getattr(mcp_manager, "available", False):
            return out
        manifests = list(mcp_manager.list_tool_manifests() or [])
    except Exception:
        return out
    for m in manifests:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        if not name:
            continue
        desc = str(m.get("description", "") or "")
        out.append(ToolDefinition(
            name=name,
            origin=ToolOrigin.MCP,
            manifest=m,
            awareness=_compress(desc) or f"external capability: {name}",
            # ⚠️ 卡片要**拆掉命名空间**（`mcp__playwright__browser_navigate` →
            #    `外部能力：playwright · browser_navigate`）—— 用户不该看见
            #    `mcp__` 这个内部前缀。intent 走通用兜底，与改造前一致
            #    （旧 `_describe_decision_for_user` 对 MCP 没有专门分支，
            #     registry 里也查不到它，所以落在 `调用工具「X」`）。
            presentation=Presentation(
                card=lambda a, _n=name: mcp_card_text(_n),
                intent=lambda a, _n=name: _generic_intent_text(_n)),
            scheduling=Scheduling.SERIAL,
            flow=Flow.CONTINUE,
            preload=Preload.DEFERRED,
            # ⚠️ binding 指向 `_execute_one_tool_call` 本身，因为**"谁处理"现在
            #    确实是这个函数**：本地 Skill 与 MCP 的执行体是它里面的两段内联
            #    分支（它们除了 result_text 还要回填 raw_result / tool_data，
            #    契约比那批 `_handle_*` 宽，所以当初的机械提取没有覆盖它们）。
            # 📌 同探索作用域的 binding 指向 `_run_skill_exploration` 那个大循环 ——
            #    **目录如实描述"谁处理"，即使那个"谁"现在是一个大函数，
            #    也绝不为了好看虚构一层。**
            # 🔴 改造前这里写的是 `_handle_local_skill` / `_handle_mcp_tool`,
            #    **而 Orchestrator 上根本没有这两个方法** —— 目录里存着一句假话。
            #    它没被发现，是因为构造期断言当时只查内置来源。现在断言覆盖全部来源。
            bindings={ToolScope.MAIN: "_execute_one_tool_call"},
        ))
    return out


def build_catalog(builtin: Sequence[ToolDefinition], *,
                  registry: Any = None, mcp_manager: Any = None) -> ToolCatalog:
    """装配一份完整目录。**每轮重建动态部分即可，内置声明不变。**"""
    cat = ToolCatalog()
    for d in builtin:
        cat.add_builtin(d)
    if registry is not None:
        for d in skill_definitions(registry):
            cat.add_dynamic(d)
    if mcp_manager is not None:
        for d in mcp_definitions(mcp_manager):
            cat.add_dynamic(d)
    return cat
