"""L2 结论行的 **schema 与提炼契约**。

⚠️ **本模块不调模型。** 它只回答两件事：
    「一条结论行长什么样」（schema + 校验）与「问什么」（提示词）。
真正的提炼调用是第 4 步。
📌 **先把"摘要什么"钉死，再去摘** —— 反过来的话，第一批 digest 就是按
   一个还没想清楚的形状生成的，而它们**会一直留在库里**。

═══ 为什么必须是固定 schema，而不是"写一段摘要" ═══

实测：**固定 schema 的输出体积由 schema 决定，不随输入长度增长。**
这掐死了早先观察到的症状 —— 摘要的地板越垫越高（400k→600k→800k→失忆），
那是**拿摘要再压摘要**、代际损耗逐轮叠加。

═══ 🔴 为什么【不】套用 `semantic_memories` 的 schema ═══

那张表（`wrong_assumption` / `correct_behavior` / `condition_text` / `confidence`…）
是**长期知识生命周期**的形状 —— 「Nano 学到了什么」；
而这里要的是「**这次对话发生了什么**」。

硬套的后果不是多几个空字段，而是**模型会为了满足 schema
把普通对话硬解释成「教训」「纠错」「行为规则」**。
📌 **schema 会反过来污染事实。**
→ L2 独立；`bridge.py`（第 5 步）做一次**显式映射**。

═══ ⚠️ L2 压的是"一半"，不是整次交换 ═══

    L2 Exchange
    ├─ 用户原话                ← **原样保留**
    └─ Nano / 工具 / 中间过程
           ↓
       ExchangeDigest（本模块）  ← 只压这一半

🔴 这一条修的是早先设计里的一处**自相矛盾**：本项目同时定过
「L2 = 一次交换压成一条结论行」和「用户原话逐字保留」——
**两句按字面执行是冲突的**（5000 字需求塞不进一条定长 digest）。
⭐ 拆成两半之后，「不可再生的用户原话保留」才真正成立
（实测：一整段会话里真人打的字只占 0.32%，保它的边际成本极低）。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

# ── schema ────────────────────────────────────────────────────────────────
#
# ⚠️ **只有四个字段。** 字段越多，固定开销越大、模型越容易写废话。
#
# 🔴 明确**反对**放进来的（每条都有理由，别再加回去）：
#   `reasoning`   会膨胀，直接毁掉"体积由 schema 决定"这条性质
#   `confidence`  没有标定来源的**伪精度**
#   `next_step`   把「当时的建议」冻结成「现在仍有效的事实」
#   `tags`        另一套会漂移的分类体系
#   `memory_type` / `wrong_assumption` / `correct_behavior` / `condition_text`
#                 属于语义记忆的 ontology（见模块头）

# ⭐ `chat` 是后加的一档，**不是可有可无**：
#    原始四档是 `done/partial/blocked/failed`，
#    ⚠️ 但**大量交换根本不是任务**（闲聊、提问、"你好"）——
#    硬套会逼模型给闲聊贴一个 `done`，
#    📌 **那正是本模块头警告的「schema 反过来污染事实」。**
# 🔴🔴 **2026-08-14 拆轴**：原来是 `("chat","done","partial","blocked","failed")`，
#    实际跑下来**几乎所有交换都被判成 `chat`** —— 连「读完整个 docx 并总结」
#    也标 `chat`。
#
# 诊断（不是提示词写得不好，是 schema 错了）：**这两组值不在同一个轴上。**
#     done/partial/blocked/failed  = 一次尝试的**结果**
#     chat                          = 这次交换的**类型**
#    一个枚举混两个轴，模型就得先隐式回答"这算不算一次尝试"；
#    而提示词里最响的两句恰好是 "most exchanges are not tasks" 和
#    "Do NOT force conversation into done" —— 于是它一律答 `chat`。
# 📌 **一张表里混进一个不同种类的键，消费方就会把它当同类**
#    （同 `ladder_enabled` 混进厂商表被 `vendor_of` 当成厂商那次）。
#
# ⭐ 拆法的关键不是"分成两个字段"，是**第一个字段根本不问模型**：
#    这次交换有没有工具调用，是 `ex.messages` 里摆着的**事实**，不是判断。
#    问模型 = 把一个已知事实交给它猜，然后再花钱验证它猜得对不对。
# ⭐ 而且这条判据跟 status 的**目的**是对齐的：`partial/blocked/failed`
#    存在的唯一理由是「一次尝试不许被记成一次成就」——
#    那个风险只在**改变了外部状态**的交换上存在，也就是有工具调用的那些。
KIND = ("talk", "work")

STATUS = ("done", "partial", "blocked", "failed")

# 老数据里的 `status="chat"` → `kind="talk"` + `status=None`。
# ⚠️ **不升 `DIGEST_SCHEMA_VERSION`**：升了老 digest 全变陈旧 → 全部重新提炼
#    → 白花钱，而这个映射是**无损**的。📌 迁移能就地做，就别让它变成一次重算。
LEGACY_CHAT = "chat"

# ⚠️⚠️ **数组必须有上限，否则「固定 schema → 输出体积固定」是假的。**
#    `referents: 83 个` 照样能让摘要地板长高。
MAX_REFERENTS = 6
MAX_OPEN_ITEMS = 4
MAX_ITEM_CHARS = 80
MAX_OUTCOME_CHARS = 400

FIELDS = ("kind", "status", "outcome", "referents", "open_items")


def empty() -> dict:
    return {"kind": "talk", "status": None, "outcome": "",
            "referents": [], "open_items": []}


def kind_of(ex) -> str:
    """这次交换是**做事**还是**说话** —— 由代码判定，**不问模型**。

    判据：交换里有没有工具调用 / 工具结果。
    ⚠️ 边界情况说清楚：纯建议类交换（"帮我想个方案"，没调工具）判成 `talk`。
       这是**刻意的** —— 那种交换没有「做成了没有」这个问题可回答，
       硬给它一个 `done` 正是本模块头警告的「schema 反过来污染事实」。
    """
    for m in getattr(ex, "messages", []) or []:
        if getattr(m, "tool_calls", None) or getattr(m, "tool_results", None):
            return "work"
    return "talk"


def tool_facts(ex) -> tuple[int, int]:
    """`(工具调用数, 报错数)` —— 喂给提炼器的**客观事实**，不是判断。

    📌 现在 `[tool ERROR]` 标记散在正文里要模型自己数，`blocked/failed`
       判错很正常。**能算出来的事实就别让它数。**
    """
    n = err = 0
    for m in getattr(ex, "messages", []) or []:
        for tr in (getattr(m, "tool_results", None) or []):
            n += 1
            if getattr(tr, "is_error", False):
                err += 1
    return n, err


def validate(raw: Any, *, kind: str = "") -> tuple[dict | None, list[str]]:
    """校验并归一化一条结论行。返回 `(digest | None, errors)`。

    ⚠️⚠️ **fail-closed**：校验不过就返回 `None` —— 调用方必须让那次交换
       **继续留在 L1**，而不是"反正大部分字段有值，凑合用"。
       📌 一条半残的结论行会被后面的结论行当成上文继续引用，
          错误会**沿着结论链传播**，而且每一步看起来都很正常。

    ⭐ 超上限时**截断 + 响亮留痕**，不算校验失败：
       📌 模型已经在提示词里被告知了上限（见 `build_prompt`），
          **与其在事后猜哪个更重要，不如在事前告诉它有多少位置** ——
          所以这里的截断是兜底，正常不该触发；触发了就该被看见。
    """
    errs: list[str] = []
    if not isinstance(raw, dict):
        return None, ["digest 不是一个对象"]

    # ⚠️ `kind` 由**调用方**（代码）给，不接受模型给的 —— 模型给了也丢掉。
    #    📌 一个已经知道答案的字段，如果还从模型那里"接受"一份，
    #       就等于给同一件事造了第二个权威。
    _kind = str(kind or "").strip().lower()
    if _kind not in KIND:
        # 老数据回读（`kind` 是 2026-08-14 才有的）：按 status 反推。
        _kind = "talk" if str(raw.get("status") or "") == LEGACY_CHAT else "work"

    _st = str(raw.get("status") or "").strip().lower()
    if _kind == "talk":
        # 闲聊没有"做成了没有"可答 —— 模型给了也不要。
        _st = None
    elif _st == LEGACY_CHAT:
        # 老数据：work 却标着 chat（拆轴之前的产物）→ 当成没写
        _st = None
    elif _st not in STATUS:
        errs.append(f"status={raw.get('status')!r} 不在 {STATUS}")

    _out = str(raw.get("outcome") or "").strip()
    if not _out:
        errs.append("outcome 为空")
    elif len(_out) > MAX_OUTCOME_CHARS:
        logger.info(f"[Digest] outcome 超长 {len(_out)} → 截到 {MAX_OUTCOME_CHARS}")
        _out = _out[:MAX_OUTCOME_CHARS]

    def _arr(key, cap):
        v = raw.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            errs.append(f"{key} 不是数组")
            return []
        out = []
        for x in v:
            s = str(x).strip()
            if not s:
                continue
            if len(s) > MAX_ITEM_CHARS:
                s = s[:MAX_ITEM_CHARS]
            out.append(s)
        if len(out) > cap:
            # ⚠️ 兜底截断要**响亮** —— 📌 它意味着提示词里那句上限没被听进去，
            #    而那是个可以改提示词解决的问题，不该被静静吞掉。
            logger.warning(f"[Digest] {key} 超上限 {len(out)}>{cap} → 截断（"
                           f"提示词里已声明上限，超了说明模型没照做）")
            out = out[:cap]
        return out

    _refs = _arr("referents", MAX_REFERENTS)
    _open = _arr("open_items", MAX_OPEN_ITEMS)

    if errs:
        return None, errs
    return {"kind": _kind, "status": _st, "outcome": _out,
            "referents": _refs, "open_items": _open}, []


# ── 提示词 ────────────────────────────────────────────────────────────────

_SYS = (
    "You compress one exchange of a conversation into a small fixed record. "
    "You are not talking to the user and you are not continuing the conversation. "
    "Output JSON only."
)

_STATUS_WORK = f"""  status      one of: {", ".join(STATUS)}
              This exchange DID use tools, so it attempted something.
              "partial"/"blocked"/"failed" exist so that an ATTEMPT is never
              recorded as an ACHIEVEMENT. If any tool errored, or the goal was
              not actually reached, do NOT write "done".

"""

_STATUS_TALK = """  status      Set it to null. This exchange used no tools — it was conversation,
              a question, or advice. There is no "did it succeed" to answer here,
              and inventing one would record something that never happened.

"""


def _rules(kind: str, facts: str, lang_line: str) -> str:
    return f"""{facts}Produce exactly these five fields:

  kind        exactly "{kind}". Do not change it — it was determined from the
              exchange itself, not from your judgement.

{_STATUS_WORK if kind == "work" else _STATUS_TALK}  outcome     What this exchange actually established or produced. <= {MAX_OUTCOME_CHARS} chars.
              ⚠️ MUST STAND ALONE. Never write "that plan", "it", "the above approach".
              Name the thing. Someone reading only this line, months later, with no
              other context, must know what it refers to.
                BAD : changed X per that plan
                GOOD: changed X per the RAG archival formula (multiplicative version)
                      agreed on 08-03

  referents   Concrete things this exchange may later be pointed back at:
              file paths, Skill names, URLs, task ids, image handles.
              At most {MAX_REFERENTS} items, each <= {MAX_ITEM_CHARS} chars.
              Pick the ones that would actually be needed to find things again.

  open_items  Anything left unresolved, waiting, or explicitly deferred.
              At most {MAX_OPEN_ITEMS} items, each <= {MAX_ITEM_CHARS} chars.
              Empty list if nothing is pending.

{lang_line}
Field names and the values of `kind` and `status` are fixed keys — always English,
never translated. Only `outcome`, `referents` and `open_items` follow that language.

Do not add any other field. Do not explain your reasoning."""


def build_prompt(exchange_text: str, prior_lines: list[str], *,
                 kind: str = "work", n_tools: int = 0, n_err: int = 0) -> tuple[str, str]:
    """`(system, user)` —— 提炼一次交换所需的全部输入。

    ⭐⭐ **输入范围 = 这次交换 + 已生成的 L2 结论行**。
       **不给它前面的原文** —— 那是 O(n²)，会变回"反复重读"那个形状，
       成本优势全没。

    📌 这个做法的权衡一句话：**用「低分辨率但够定位」的上文，换取线性成本。**
    ⚠️ 代价是「误差沿结论链累积」，但它与被否掉的递归摘要**不是一回事**：

        递归摘要（已否掉）  摘要的摘要的摘要 → **乘性**损失，地板单调抬高
        本做法              每段原文都**直接从原文**压一次（不叠加），
                            只是参考前面的结论来理解指代

    ⭐ 所以本做法里**结论本身的分辨率不退化**，退化的只是「能不能正确解开指代」——
       弱得多的问题，而且被上面那条 `outcome` 硬要求直接堵掉。
    """
    _ctx = ""
    if prior_lines:
        # ⚠️ 只给最近的几条：上文是用来解指代的，不是用来复述历史的。
        #    📌 给多了它会开始"总结总结"，那就退回递归摘要了。
        _recent = [x for x in prior_lines if x][-8:]
        _ctx = ("Earlier in this same conversation (already compressed, "
                "for resolving references only — do NOT summarize these):\n"
                + "\n".join(f"- {x}" for x in _recent) + "\n\n")
    # ⭐ **客观事实先行**：工具调用数 / 报错数是算出来的，不让模型去数。
    #    📌 一个模型要"数一数正文里有几个 ERROR"才能回答的问题，
    #       就是一个本来不该问它的问题。
    _facts = ""
    if kind == "work":
        _facts = (f"Facts about this exchange (counted from the transcript, "
                  f"not your judgement): {n_tools} tool result(s), "
                  f"{n_err} of them returned an error.\n\n")

    # ⭐ 语言走**唯一出处**（`core/i18n.py`）—— 不在这里自己写一句语言策略。
    #    📌 散在各处的私有语言策略，是同一件事的 N 份互不知情的答案。
    try:
        from core.i18n import language_clause
        _lang = language_clause("`outcome`, `referents` and `open_items`")
    except Exception:
        _lang = ""

    return _SYS, (f"{_ctx}The exchange to compress:\n\n{exchange_text}\n\n"
                  f"{_rules(kind, _facts, _lang)}")


def exchange_text(ex, *, max_chars: int = 12000) -> str:
    """把一次交换渲染成提炼器的输入。

    ⚠️ **用户原话原样给**，Nano 那边可以裁 —— 这正是本模块头那条
       「L2 压的是一半」在输入侧的体现。
       📌 实测：一整段会话里真人实际打的字只占 0.32%，**保它的边际成本极低**。
    """
    parts: list[str] = []
    used = 0
    for m in ex.messages:
        role = getattr(m, "role", "")
        c = getattr(m, "content", "")
        body = c if isinstance(c, str) else " ".join(
            str(b.get("text") or "") for b in c if isinstance(b, dict))
        # 🔴 `visible_to_user=False` 的那些 role=user 消息是 **Nano 自己的注记**
        #    （回看提示、图片占位说明、系统 check-in）。它们不会开新交换
        #    （`exchange._opens_exchange` 已经修对了），但这里如果照样标成
        #    `[user]`，提炼器就会读到「用户亲口说：This message originally
        #    carried 1 image…」并把它当成用户的需求写进结论。
        # 📌 **「什么算用户说的话」这条判据，边界那里判对了，正文这里也必须判** ——
        #    两处各判各的，等于系统里有两个"用户"的定义。
        # ⚠️ 不是删掉它 —— 它确实发生过，删了提炼器会看不懂上下文；
        #    只是必须标成注记，并且**跟着 Nano 那半一起裁**。
        if role == "user" and not getattr(m, "visible_to_user", True):
            _s = body[:600]
            parts.append(f"[system note] {_s}")
            used += len(_s)
            continue
        if role == "user":
            # 用户原话不裁
            parts.append(f"[user] {body}")
            used += len(body)
            continue
        for tr in (getattr(m, "tool_results", None) or []):
            _s = str(getattr(tr, "content", ""))[:600]
            parts.append(f"[tool{' ERROR' if getattr(tr, 'is_error', False) else ''}] {_s}")
            used += len(_s)
        for tc in (getattr(m, "tool_calls", None) or []):
            parts.append(f"[calls] {getattr(tc, 'name', '?')}")
        if body:
            _s = body[:1500]
            parts.append(f"[nano] {_s}")
            used += len(_s)
        if used > max_chars:
            parts.append("[…truncated]")
            break
    return "\n".join(parts)


def render_line(digest: dict) -> str:
    """把一条结论行渲染成注入上下文的那一行文字。

    ⚠️ 保持**一行** —— 它要被几十条一起塞进上下文，每条多一行就是几十行。
    """
    if not digest:
        return ""
    _st = digest.get("status") or "chat"
    _tail = ""
    if digest.get("referents"):
        _tail += f" [refs: {', '.join(digest['referents'])}]"
    if digest.get("open_items"):
        _tail += f" [open: {'; '.join(digest['open_items'])}]"
    _mark = "" if _st == "chat" else f"({_st}) "
    return f"{_mark}{digest.get('outcome', '')}{_tail}"
