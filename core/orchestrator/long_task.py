# core/orchestrator/long_task.py
"""长任务：交还、挂起恢复、等待 / 回看 / 停止后台任务、任务边界。（`Orchestrator` 的 mixin）"""

import asyncio
import traceback

from loguru import logger

from core.orchestrator._runtime import _rt_lease_release, _rt_sweep_stale_spans, _rt_wait_open
from core.orchestrator._types import ToolOutcome
from core.orchestrator.react_loop import _OS_CAPABILITY_PROMPT
from core.tools import ToolScope
from core.tools.manifests import _DONT_WAIT_MANIFEST


class LongTaskMixin:
    """长任务：交还、挂起恢复、等待 / 回看 / 停止后台任务、任务边界。"""

    # ⭐⭐⭐ 等待的读写都直接进入 `waitcond`。
    # 切读期的 Authority 门面与观测期的 shadow 映射已经完成使命：继续保留会让后来调用方
    # 误以为仍有两套身份/两套账。历史 `legacy_susp_id` 只是一列反查数据，不是权威。

    # ⭐⭐⭐ **长任务交还控制权的阈值（秒）** —— 与任务类型无关。
    #
    # 历史：它 8s → 90s（2026-08-09 上午，已定），当时还叫「MCP 自动后台化阈值」。
    # 下面那段解释的就是 8s 为什么错；而**它的名字和覆盖面又错了一次**，见更下方。
    #
    # 🔴 **8 秒落在正常值域里**：一次 browser_navigate、一次大页面 fetch
    #    都可能超过它。于是这个机制在**常见路径上**开火，而在常见路径上它是纯开销：
    #    多两次 LLM 往返（一次说「我放后台了」、一次唤醒后继续）+ 一次用户
    #    不需要的绕路，而且**会把一个 ReAct 链从中间切断**
    #    （给模型的话就是「end this turn and do not call more tools」）。
    # 📌 **一个「防异常」的阈值，如果落在正常值域内，它就不再是保护，
    #    而是变成了主路径** —— 而主路径上的每一条绕路都要付 token 和一次用户困惑。
    #
    # ⭐ 它真正要防的是「几分钟不说话」（一个挂住的连接、一个超大下载），
    #    那和 8 秒不是一个量级。90 秒之后，绝大多数正常调用在前台跑完、
    #    ReAct 链保持完整、一次往返。
    # ⚠️ 提高阈值意味着前台阻塞更久 —— 而那个代价**正好是早先帮我们付掉的**：
    #    durable inbox 之后用户随时能说话，被卡住的只剩「Nano 晚一点开口」。
    #    📌 **一个阈值的合理值，会随着「它防的那件事的代价」变化而变化** ——
    #       inbox 落地时就该重新评估它，当时没有。
    # 🔬 待实测标定（与 `MAX_BACKGROUND_RUNNING` 同类：这类数字推不出来，只能量）。
    #
    # ⭐⭐⭐ **2026-08-09 第二次修正：这个阈值与「任务类型」无关，
    #    而且它的产物【不是】「转后台」，是「把控制权交回模型」。**
    #
    # 🔴 上一版把它做成了 MCP 专属（名字就叫 `_LONG_TASK_HANDBACK_SEC`），
    #    而原话是「从来就没把 mcp 和长命令分开过，我们说的是
    #    『耗时长的任务』，跟任务类型从来就没有关系过。」
    #：**确实从来没有按类型区分的设计** —— `_LONG_TASK_HANDBACK_SEC`
    #    只是 MCP 落地时的一个局部实现，是做回看时按「哪里已经有钩子」
    #    找接入点，才把它当成了机制。
    # 📌 **给一个机制接线时，要按「哪些场景需要它」去找接入点，
    #    不是按「哪里已经有现成的钩子」** —— 后者会让机制的覆盖面
    #    等于历史遗留的形状，而不是等于它的目的。
    #
    # ⭐⭐ **而更深的一层是 用户推翻的「强制后台化」本身**：
    #    现实只有两种情况 ——
    #      ① Nano 主动丢后台（它自己定第一次回看间隔）
    #      ② 没丢，就在前台跑，但保留系统这个回看触发时间
    #    而 ② **压根不需要「强制后台」**：**回看本身就回答了
    #    「为什么这么久」**（根本完不成 / 一切正常再等等）。
    # 📌 **一个纯粹为了「让控制权回到模型手上」的机制，
    #    不该产生用户可见的语义。** 旧实现让 Nano 必须说一句
    #    「我放后台了，回头告诉你」—— 那是实现细节泄漏成了对话内容。
    #    用户该看到的只有「还在跑，我看了一下，情况是 X」。
    # ⭐ 见 `executor_write._FOREGROUND_WAIT_SEC` 上方那段：
    #    2026-08-22 那次建模 把三个宽限期（命令 45 / 外部服务 90 / Subagent 5）
    #    **统一成 5 秒** —— 📌 它们本来就在答同一个问题，
    #    只是当初被当成三个问题分别定了值。
    _LONG_TASK_HANDBACK_SEC = 5.0

    # ⭐ 被交回控制权之后，那个**载体**还能活多久的硬上限。
    #
    # ⚠️ 外部评审 2026-08-09 发现的时间矛盾是真的：MCP 客户端默认 120s 硬超时
    #    < 90s 交还 + 60s 首次回看 = 150s → **回看在数学上不可达**。
    # ⭐ 但正确的解释不是「给 MCP 开个特例」，而是一条通用规则：
    #    📌 **一个操作的期限，在控制权被交回模型之后，
    #       应该由「后台合同」决定，而不再由「前台等待的耐心」决定。**
    #    前台阈值（90s）问的是「我还要不要干等」；
    #    这个期限问的是「这件事本身最多允许跑多久」。两个不同的问题。
    # ⚠️ 它是**载体的硬上限**，不代替模型每次回看后的重排判断；
    #    30 分钟与 WaitCondition 的默认 orphan 兜底同量级。
    _HANDED_BACK_DEADLINE_SEC = 30.0 * 60.0

    # ⭐⭐ **第一次回看的间隔** —— 语义是「**开始怀疑这个长任务出问题了**」。
    #
    # ⚠️ 这是系统唯一该承担的那个数字。已定设计：
    #    系统只设**第一次**，之后每次回看都由**模型**根据看到的东西自己决定下一次
    #    （进度 10% 就排远点、95% 就干脆不排、明显坏了就换办法）。
    # 📌 **常量只该承担系统答得出的那个问题**（「多久之后开始怀疑」）；
    #    「这件事还要多久」只有模型能答 —— 任何固定数字都覆盖不了真实情况。
    # ⚠️ 而 Nano **主动**丢后台的那条路**连这一次都不需要** ——
    #    第一次就是它自己填的（具体任务具体判断）。
    _FIRST_RECHECK_SEC = 60.0

    # ⭐ 每次回看唤醒**之前**，系统先把下一次回看推到这个长兜底上，
    #   然后模型再覆盖它（排近一点 / 干脆不排）。
    # 🔴 **没有这一步会每 5 秒唤醒一次**：驱动唤醒的是那个
    #   `status IN ('WAITING','DUE_FOR_REVIEW') AND fire_at <= now` 查询，
    #   而 `_due` 命令的幂等只保证不重复写库、**不保证不重复点火**。
    #   📌 **一个「幂等的状态转换」不等于「幂等的副作用」。**
    # 📌 而兜底取**长**是有意的：**默认必须是安全的，模型只在偏离默认时才说话** ——
    #    模型这一轮没判断（或者崩了），下一次回看是这个长兜底，不是 5 秒。
    _RECHECK_FALLBACK_SEC = 900.0

    async def _hand_back_long_task(self, *, display: str, bg_ref: str,
                                    action_id: str, event_queue,
                                    recheck: bool = True,
                                    detachable: bool = True) -> str:
        """一个耗时长的操作跑过阈值 → **把控制权交回模型，操作继续跑。**

        ⭐⭐⭐ **这是「长任务」的公共合同，与任务类型无关**（MCP / OS 命令 /
        以后的Subagent，全走这里）。2026-08-09 定的原则：
        📌 **我们要识别的只有「长任务」，识别任务类型毫无意义** ——
           用户不需要区分，所以系统也不该区分。

        它做三件事，一件都不多：
          ① 开一条**双源等待**（完成信号 + 到点回看）
          ② 出等待 pill（让用户看得见「这件事还挂着」）
          ③ 返回一段**不结束这一轮**的话给模型

        ⚠️⚠️ **第 ③ 件是这次改动的核心。** 旧实现给模型的话是
        「…has been moved to the background. Tell the user… **Then end this turn
        and do not call more tools.**」—— 那句话**在系统层面把 ReAct 链切断**，
        正是 用户最初质疑的那个后果（「会不会在系统层面强制把 react 拆成
        一大堆 turn」）。
        📌 **控制权交回模型 ≠ 这一轮必须结束。** 前者是「你可以继续做别的」，
           后者是「你不许再做事」—— 把它们压成一个，模型就失去了
           「一边等它一边铺路」的能力，而那正是它本来在做的事。

        ⚠️ 登记不上等待时**不许假装挂起了** —— 那会让这个操作永远没人等它。
           📌 一个「我会回来看」的承诺，登记失败时必须撤回，不许留在嘴上。
        """
        # ⭐⭐ `recheck=False` = **这个载体永远不回看**，只等完成信号。
        #
        # 今天唯一走这一档的是**Subagent**。早先的设计原话：agent 是
        # 「**无法被回看的后台任务**」—— 回看意味着把它走过的步骤读回主上下文，
        # 而上下文隔离正是Subagent存在的全部理由。
        # 📌 **一个为了隔离而存在的东西，不能有一条把它的过程灌回来的通路。**
        # ⚠️ 而「不回看」不是这里新造的一档：`set_next_checkin` 的
        #    `seconds` 省略语义逐字就是它（"do NOT look again; the completion
        #    signal will wake you"）。Subagent只是**永久**停在那一档。
        _wake_on = ["background", "timer"] if recheck else ["background"]
        _rec = _rt_wait_open(
            reason=f"still running: {display}",
            wake_on=_wake_on, bg_ref=bg_ref,
            timer_seconds=self._FIRST_RECHECK_SEC if recheck else None,
            # ⚠️ **意图必须显式写，不许由「有没有 timer」反推。**
            #    📌 早先的设计那条逐字同形：「不许由 `timer_at` 猜 pill 是否可控 ——
            #       等待创建者必须显式给 `waiting_intent`」。
            #       timer 的有无是这个意图的**后果**，不是它的定义。
            intent="system_recheck" if recheck else "detached")
        if _rec is None:
            logger.error(f"[LongTask] 交还控制权时登记等待失败（{display[:40]}）"
                         f"—— 不发等待 pill，也不承诺回看")
            return (f"\"{display}\" is still running, but the runtime could not "
                    f"register a follow-up for it. Tell the user it is running and "
                    f"that you cannot promise to report back automatically.")
        await event_queue.put({
            "event": "suspend_waiting",
            "action_id": action_id,
            "suspension_id": _rec.wait_id,
            "reason": f"still running: {display}",
            "wake_on": list(_wake_on),
            "timer_at": _rec.fire_at,
            "timer_seconds": self._FIRST_RECHECK_SEC if recheck else None,
            "waiting_intent": "system_recheck" if recheck else "detached",
            "bg_ref": bg_ref,
        })
        # ⭐⭐ **这一刻是 `dont_wait` 唯一有意义的时刻** —— 在此之前没有
        #    载体可以交出去，在此之后这一轮就结束了。所以它走
        #    `_pending_loaded_manifests`（本轮中途并入 schema 的既有通道），
        #    **不常驻、也不进 deferred 感知块**。
        #    📌 一个只在某种状态下才有意义的工具，应该只在那种状态下出现 ——
        #       这正是 `set_next_checkin` 那条 `availability` 的同一条判据，
        #       只不过它的「那种状态」出现在**一轮的中途**，而
        #       `availability` 是**每轮开头**算一次的，接不住它。
        # ⚠️ Subagent那条 `detachable=False`：它**已经**是不在手头的了，
        #    再给一个「别等它」的工具是让模型对一件已经做完的事再做一次决定。
        if detachable:
            self._detachable_carrier = {
                "wait_id": _rec.wait_id, "bg_ref": bg_ref, "display": display,
                "action_id": action_id,
            }
            try:
                if not hasattr(self, "_pending_loaded_manifests"):
                    self._pending_loaded_manifests = []
                if not any((m or {}).get("name") == "dont_wait"
                           for m in self._pending_loaded_manifests):
                    self._pending_loaded_manifests.append(_DONT_WAIT_MANIFEST)
            except Exception as _e_dw:
                logger.warning(f"[B1] dont_wait 没能注入本轮工具表: {_e_dw}")
        logger.info(f"[LongTask] {display[:40]} 交回控制权"
                    f"（{_rec.wait_id}，"
                    + (f"首次回看 {self._FIRST_RECHECK_SEC:.0f}s 后" if recheck
                       else "**不回看**，只等完成信号") + "）")
        # ⭐⭐ **这一句原来是无条件的「The runtime knows nothing about its
        #    progress right now.」—— 它现在会说谎。**
        #    接上进度总线之后，长命令有 stdout、MCP 有协议原生进度通知，
        #    交还的那一刻往往**已经能看到东西**了。
        #    📌 **一句「我不知道」在它其实知道的时候说出来，比不说更糟** ——
        #       模型会照着它去做一次多余的探查，或者干脆放弃判断。
        # ⚠️ 反过来也一样：没有进度就必须**明说没有**，不许给一个看起来
        #    像进度的空值。📌 一个「进度」字段在没有进度时必须说「没有」。
        _now = ""
        try:
            from core.runtime import progress as _pb0
            _now = _pb0.tail(bg_ref, lines=10)
        except Exception:
            _now = ""
        _now_seg = (f"Here is what it is doing right now:\n{_now}\n" if _now else
                    "⚠️ The runtime cannot see its progress — this carrier does not "
                    "report any. Do NOT claim you can see progress unless you "
                    "actually go and look.\n")
        return (
            f"\"{display}\" is still running — it has not finished yet, and it "
            f"keeps running on its own. It is the command you already started: do NOT "
            f"launch it, retry it, or ask the user whether to launch it again. Its "
            f"completion signal is already registered. Control is back with you only "
            f"for independent work.\n"
            + _now_seg +
            # 🔴 上一版这里写的是「你可以：…/ 继续做不依赖它的步骤 / …」，
            #    而下一段才提 `dont_wait` —— **这两句在打架**：前者已经
            #    准许它直接去做别的，于是它就直接去做了（实测 2026-08-20）。
            #    📌 一段给模型的指引，如果先给了出口再给条件，它只会读到出口。
            f"Decide now, before you do anything else:\n"
            # ⭐⭐ **把那个出口指出来。** schema 在清单里，但模型不会因为
            #    「有个工具」就想到该用它 —— 它需要被告知这一刻有个选择。
            #    ⚠️ 而且**把条件和它一起说**：说出口不说条件，等于邀请它逢长任务
            #       就调（那正是实测抓到的那次滥用）。
            #    📌 一个只在特定条件下才正确的动作，提示它的时候必须连条件一起提。
            + (("(a) If your next move needs this result — do nothing special. "
                "Say one short line to the user if it is worth saying, and the "
                "runtime will bring you back to it in about a minute.\n"
                "(b) If you have OTHER work that does not need this result — "
                "call `dont_wait` FIRST, naming that work, and only then start it. "
                "That hands this call to the runtime: it stops pulling you back to "
                "look at it, it shows up in the user's task drawer, and you get "
                "woken up once when it is actually done.\n"
                "⚠️ Doing that other work WITHOUT calling `dont_wait` first is the "
                "one wrong answer here: you will be interrupted partway through it.\n") if detachable else
               "Carry on with anything that does not depend on it, or end your turn "
               "if there is nothing else to do.\n")
            + "The runtime already owns follow-up. Do not narrate its cadence to the "
              "user; only speak about this task when you have a useful fact or decision.")

    async def resume_suspension(self, suspension_id: str, trigger: str, note: str = ""):
        """定时/后台唤醒入口——被外部触发器（app 的定时器 / 诈尸回调）调用，
        起一个新 turn 带着挂起上下文重新进 ReAct 循环。

        user 唤醒不走这里——用户下次说话时由 _handle_query_impl 顶部自动注入恢复。
        """
        from core.runtime.kernel import get_kernel
        from core.runtime import waitcond as _wc
        rec = _wc.find_by_id(get_kernel(), suspension_id)
        if rec is None or not rec.is_live:
            logger.info(f"[Suspension] resume 跳过：{suspension_id} 不存在或非 active")
            return
        # 预算检查必须在 resolve 之前——这是本方法里唯一的不可逆状态变更。
        #
        # 下面那句 resolve() 是幂等保护（防止同一条被定时+后台重复唤醒），
        # 它有意放在模型调用之前。代价是：一旦后续的 provider 调用失败，
        # 记录已经被标记为已恢复，而 turn 从未发生 —— 这条挂起就【永久丢了】，
        # 用户永远不知道 Nano 找过自己。
        #
        # 这不是假想：实测过一次同形状的事故——代理挂掉时，失败的请求
        # 照样把挂起记录消费掉了。所以这里在动状态之前先问一次预算，
        # 不够就原样退出，记录保持 active，下一次轮询还能再试。
        #
        # ⚠️ 这与"闸必须在 provider 收口"不冲突：判断逻辑仍然只有一份
        # （usage.cap_status），这里只是在会造成不可逆后果的地方提前止损。
        try:
            from core.usage import sync_budget_health
            if sync_budget_health() == "hard":
                logger.warning(
                    f"[Suspension] 预算已达硬上限，跳过唤醒 {suspension_id}（记录保持 active，稍后重试）"
                )
                return
        except Exception as _e:
            # 预算模块异常不该把挂起卡死——放行，真超限的话 provider 层还有一道闸
            logger.warning(f"[Suspension] 预算预检异常，按放行处理: {_e}")

        try:
            from core.usage import usage_tracker as _ut
            _ut.begin_turn()   # 唤醒 turn 也打点，UI 单条 token 一致
        except Exception:
            pass
        # ⭐⭐⭐ [回看设计 2026-08-09] **一次「回看」不许把这条等待 resolve 掉。**
        #
        # 判定：这条等待挂着 `bg_task_ref`（后台还在跑）而唤醒源是**定时** ——
        # 那就是「到点该去看一眼了」，**不是「那件事成了」**。
        # 🔴 原来这里无条件 `resolve` —— 对回看是**致命的**：
        #    后台任务还在跑，而它的完成信号从此再也唤不醒任何人
        #    （记录已终态 → `notify_background_done` 找不到匹配 → 静默丢弃）。
        #
        # 📌 **「唤醒」和「结束这条等待」是两件事** —— 它们此前被压在同一个动作里
        #    （`resolve` 兼做「防重复唤醒」的去重）。回看正是把它们分开的那个场景。
        #    ⭐ 而去重仍然有：重排会把 `fire_at` 推到长兜底上，
        #      所以下一跳轮询不会再点火。**换了一种去重方式，不是取消去重。**
        _is_recheck = bool(rec.bg_ref) and trigger == "timer"
        if _is_recheck:
            # ⚠️ **先把下一次回看排到长兜底，再唤醒模型**（顺序不能反）：
            #    驱动唤醒的是那个 `fire_at <= now` 的查询，
            #    不推的话这一跳唤醒完、下一跳（5 秒后）又会点火。
            #    📌 一个「幂等的状态转换」不等于「幂等的副作用」。
            try:
                from core.runtime import waitcond as _wc_r
                _wc_r.reschedule_wait(suspension_id, self._RECHECK_FALLBACK_SEC)
            except Exception as _e_rs:
                logger.error(f"[Recheck] 🔴 重排下一次回看失败（{suspension_id}）—— "
                             f"这条等待可能会被反复唤醒: {_e_rs}")
            # ⭐ 记住「这一轮在回看哪一条」—— `set_next_checkin` 不需要模型传 id
            #    （回看轮里只有一个对象，让它传 id 只会多一个可以填错的地方）。
            #    📌 **模型不该被要求提供系统已经知道的东西。**
            self._recheck_sid = suspension_id
            logger.info(f"[Recheck] 回看 {suspension_id}（后台仍在跑）→ 起新 turn"
                        f"，下一次回看已先排到 {self._RECHECK_FALLBACK_SEC:.0f}s 后")
        else:
            # ⚠️ 非回看轮要**清掉**它，否则 `set_next_checkin` 会在下一轮
            #    继续被注入、并指向一条已经结束的等待。
            #    📌 一个「本轮有效」的状态，必须在每一条进入这一轮的路径上都被设定 ——
            #       只在需要它的那条路上设，另一条路会带着上一轮的值跑。
            self._recheck_sid = ""
            # 标记恢复（防止同一条被定时+后台重复唤醒）
            _wc.resolve_wait(suspension_id, resolved_by=trigger)
        # ⚠️ 这里原本还会关闭一次观测期的镜像 ——
        #    它关的是**镜像**，而镜像已经就是权威（上一行的
        #    resolve/cancel 直接走内核）。留着是**对同一件事关两次**，
        #    而且它依赖一个**重启就没的内存映射**。
        #    📌 双写拆掉之后，配对的「双关」也必须一起拆 ——
        #       留一半会让人以为还有另一套账。
        logger.info(f"[Suspension] 唤醒 {suspension_id}（trigger={trigger}）→ 起新 turn")

        used_model = "UNKNOWN"
        event_queue = asyncio.Queue()

        async def realtime_callback(m_name: str):
            nonlocal used_model
            used_model = m_name

        # ① 唤醒 turn 的工具清单同样问目录（双循环合一：唤醒也能操作屏幕）。
        # ⚠️ 这里**只**用来拼 `{skills}` 那句系统提示；本 turn 真正的 manifest
        #    由下游 `_run_react_loop` 自己组装 —— 与改造前一致。
        regular_skills = [
            d.manifest for d in
            self._get_tool_catalog().advertised(ToolScope.MAIN,
                                                self._tool_runtime_view())
        ]
        base_guide = self._system_guide_template.format(skills=', '.join(self._skill_names(regular_skills)))
        try:
            base_guide += self._build_tool_awareness_block()
        except Exception:
            pass
        base_guide += _OS_CAPABILITY_PROMPT  # 唤醒 turn 同样注入 OS 使用说明

        system_guide = base_guide + self._build_suspension_resume_injection([rec])
        _session_injection = self._build_session_log_injection()
        if _session_injection:
            system_guide += _session_injection

        # 写一条 user 角色的唤醒提示进 memory，使本轮有"输入"驱动 ReAct
        _trigger_desc = {
            "timer": "(timer fired; automatic wake-up)",
            "background": "(background task completed; automatic wake-up)",
        }.get(trigger, f"({trigger} wake-up)")
        _note_seg = f" Background output: {note}" if note else ""
        if _is_recheck:
            # ⭐⭐⭐ **回看那一眼的全部价值，取决于这段话。**
            #
            # 🔴 系统能告诉它的只有一件事：**那个后台调用还没返回**。
            #    进度、速度、错误 —— 一样都没有（后台载体是 `await _mcp_task`，
            #    里面没有任何进度流）。
            # ⚠️ 所以如果不告诉它「你可以自己去看」，这一轮的必然结果是
            #    「还在跑，我过一会儿再看」—— 花一次完整的模型调用换一句废话。
            # 📌 **「回看一眼」的价值完全取决于那一眼能看到什么** ——
            #    只能看到「还在跑」的话，那个判断系统自己也能做，不需要模型。
            #    「当然要给，不然整个回看设计都是废的。」
            #
            # ⭐ 所以这段话必须做三件事：
            #    ① 如实说清它**现在只知道什么**（别让它以为自己看到了进度）
            #    ② 告诉它**可以怎么去看**（这才是那一眼的内容）
            #    ③ 告诉它**看完要做什么决定** —— 而且三个出口都点明：
            #       换办法 / 排下一次回看 / 不用再看了
            # ⭐⭐⭐ **如果这个载体能给出真实进度，就直接把进度放进来。**
            #
            # 🔴 上一版无条件说「系统对进度一无所知，你自己去看」。对 MCP 是真的
            #    （载体是 `await _mcp_task`，里面没有进度流），但对**长命令是假的**
            #    —— 它的 stdout 就在缓冲里躺着。
            # 📌 **一句「我不知道」在它其实知道的时候说出来，比不说更糟** ——
            #    模型会照着这句话去做一次多余的探查，或者干脆放弃判断。
            # ⭐ 所以这里先问一次载体：有进度就摆在眼前，没有就如实说没有。
            #    📌 **「回看一眼」的价值取决于那一眼能看到什么** ——
            #       所以能给的时候必须给（不给整个设计都是废的）。
            _prog = ""
            try:
                # ⭐⭐⭐ **问总线，不问某一个载体。**
                # 🔴 这里原来是 `from core.os_layer import longcmd; longcmd.progress(...)`
                #    —— 于是 MCP 和本地 Skill 那两条路**必然拿到空**，
                #    而代码上只看得出「这里读了命令的进度」，
                #    看不出「这里少了两个载体」。
                # 📌 **三个载体要被同一只眼睛看到，那个「看」的接口就该属于
                #    第三方，不属于其中任何一个。**
                from core.runtime import progress as _pb3
                _prog = _pb3.tail(rec.bg_ref or "", lines=20)
            except Exception:
                _prog = ""
            _prog_seg = (f"\nHere is what it is doing right now:\n{_prog}\n"
                         if _prog else
                         "\n⚠️ That is ALL the system knows — there is no progress "
                         "information for this one. Do NOT claim you can see progress "
                         "unless you actually go and look.\n")
            self.memory.add_system_note(
                "user",
                "[System check-in] You put this in the background a while ago and it "
                f"has NOT finished yet: {rec.reason}\n"
                + _prog_seg +
                "\n"
                "If you want to judge whether it is healthy or stuck, go look — for example:\n"
                "  · take a screenshot / read the relevant window if it has a UI\n"
                "  · run a quick command to inspect it (log tail, file size, process state)\n"
                "  · ask the same external service for its status\n"
                "\n"
                "Then choose ONE:\n"
                "  · it looks broken (e.g. 3 KB/s for a 3 GB download) → say so and try another way\n"
                "  · it looks fine but slow → tell the user briefly it is still running, "
                "and set the next check-in yourself (a long gap is fine and cheaper — "
                "if you expect ~10 more minutes, ask for ~10 minutes, not 1)\n"
                "  · it looks nearly done → no need to check again; "
                "the completion signal will wake you\n"
                "\n"
                "Decide first, then give at most one user-facing status conclusion. "
                "If scheduling the next check adds no new fact, do not repeat the same "
                "progress both before and after `set_next_checkin`.\n"
                "\n"
                f"Use `set_next_checkin` to choose when (or whether) to look again. "
                f"If you say nothing, the next check-in defaults to "
                f"{self._RECHECK_FALLBACK_SEC:.0f} seconds from now."
            )
        else:
            # ⭐⭐⭐ [2026-08-25 实测] **「核实」和「继续」必须是同一件事。**
            #
            # 🔴 这句话原来是：`Verify whether the task can continue now, then act accordingly.`
            #    用户的观察：**第一条消息那一轮很完美，唤醒之后就变得异常糟糕。**
            #    逐轮对下来，每个唤醒轮只走 1~2 个工具（看一眼屏幕）就结束了，
            #    从外面看就是「疯狂截图、从来不回复」。
            #
            # ⭐ 而 Nano 自己的复盘字字对得上：
            #      「这不是终止任务，所以按照你的指令我应该正常跟他聊天」
            #      「但是我没有这样做，而是调用了多次 look_at_screen…」
            #    ⇒ 它**知道**该聊天（那是用户指令，在历史里），
            #      但它这一轮**被交代的是「Verify」** —— 而它把核实做完了。
            # 📌 **两个指令打架时，离它最近的那个赢。**
            # 📌 **不是模型退化了，是我们把它这一轮的任务换小了** ——
            #    第一轮的驱动是「做完一整件事」，唤醒轮的驱动是「核实一下」。
            # ⚠️ 而且原文**从头到尾没有重述任务是什么**，只说了「你在等什么」
            #    和「能不能继续」—— 任务本身要它自己回头去历史里翻。
            #
            # ⭐ 修法：把「核实」写成**继续任务的第一步**，而不是一件独立的、
            #    做完就能收工的事；并且明说**这一轮什么时候才允许结束**。
            #    📌 一个不说「什么时候算做完」的指令，模型会在第一个自然停顿处停下。
            # ⚠️ 这句话**不止 GUI 走**：长命令、MCP、定时等待的唤醒都走它。
            #    所以措辞只讲「继续你原来那件事」，不提任何屏幕/工具。
            self.memory.add_system_note(
                "user",
                (f"[Scheduled plan is now due] {_trigger_desc}. The user asked for: {rec.reason}. "
                 "Perform the requested reminder or action now."
                 if rec.intent == "scheduled_plan" else
                 f"[System wake-up] {_trigger_desc}. Previous wait reason: {rec.reason}. {_note_seg}"
                 "The thing you were waiting for may have happened. Check it, and then "
                 "CONTINUE THE ORIGINAL TASK in this same turn - checking is only the "
                 "first step, it is not the job. The job is still the task the user gave "
                 "you earlier; re-read it if you need to. End this turn only when that "
                 "task is finished, or when you genuinely have to wait again.")
            )

        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False
        _rt_lease_release(self)     # 每轮重置时镜像也归还
        # ⚠️ 这里是 `_run_react_loop` 的【第二个】调用方——
        # 定时/后台唤醒**绕过 `_handle_query_impl`**，所以那边做的两件事必须在这里重做一遍，
        # 否则唤醒 turn 会复用上一轮的 `_rt_turn_id`（冷启动时甚至全都落到 "rtturn_unknown"），
        # 一旦有活 Span 残留就会触发假的不变量违反、且 sweep 永远捞不到它。
        # 顺序同样重要：sweep 必须在旧标志被重置之前。
        self._rt_turn_id = "rtturn_" + __import__("uuid").uuid4().hex[:12]
        # 工具失败计数按轮清零：跨轮保留会让"你这轮已经试过"变成假话。
        self._tool_failures_this_turn = {}
        # 旧字段没了 —— sweep 现在自己从 Span 判断，不需要外部喂旧值。
        _rt_sweep_stale_spans(self, None)
        try:
            async for ev in self._run_react_loop(
                tools_manifest=regular_skills,
                system_guide=system_guide,
                base_guide=base_guide,
                realtime_callback=realtime_callback,
                event_queue=event_queue,
            ):
                yield ev
        except Exception as core_err:
            logger.critical(f"[Suspension] 唤醒 turn 异常: {core_err}\n{traceback.format_exc()}")
            self._clean_damaged_memory()
            yield self._get_generic_error_payload(core_err, None)

    @staticmethod
    async def _wait_or_user_speaks(task, timeout: float) -> bool:
        """等它跑完，**或者等到用户又说话了**。返回 True = 它跑完了。

        🔴🔴 **实测：插话之后 `queued` 挂了五十多秒。**
           那不是排队态画错了，是**它真的排了那么久** —— 长任务的前台等待是
           90 秒定长，插话只能在队列里干等它到点。
        📌 **一个「我还要不要干等」的阈值，在用户已经开口之后就失去了前提** ——
           它防的是「没有更值得做的事时白等」，而用户说话恰恰说明有了。
        ⭐ 这不是新机制：`run_command` 早就这么做了
           （`executor_write._new_user_input_arrived()` → `await_briefly(stop_when=)`）。
           📌 **一个正确做法已经在代码里存在、却没被推广到同类场景** ——
              本项目第 N 次（`RLock` / `is_readonly` 都是这个形状）。
        ⚠️ 读的是 `inbox.submit_seq()`（收到过多少条用户消息，单调递增）——
           OS 层那条注释写得最准：**这一层只该读它答得出的那个事实**
           （「有新用户输入了」），不去问「要不要中断本轮」（那是 orchestrator 的判断）。
        ⚠️ 读不出来就退化成纯定时等待（fail-safe 方向 = 照旧等），不许因此不等。
        """
        import asyncio as _a
        _seq0 = None
        try:
            from core.runtime import inbox as _ib
            _seq0 = _ib.submit_seq()
        except Exception:
            _seq0 = None
        if _seq0 is None:
            _done, _ = await _a.wait({task}, timeout=timeout)
            return task in _done
        _deadline = _a.get_event_loop().time() + float(timeout)
        while True:
            _left = _deadline - _a.get_event_loop().time()
            # ⚠️⚠️ **先 await 一次，再判到点** —— 顺序反了会丢掉一个隐含语义：
            #    `asyncio.wait({task}, timeout=0)` 也**至少让出一次事件循环**，
            #    于是那个刚 `create_task` 出来的协程有机会跑到第一个 await。
            #    🔴 第一版把「到点就 return」放在前面，`timeout=0` 时那个任务
            #       **一步都没跑过** —— `t_recheck_longtask` 当场抓到（它正是用
            #       `_LONG_TASK_HANDBACK_SEC = 0.0` 驱动慢路径的）。
            #    📌 **把一个 `wait(timeout=T)` 换成自己的轮询循环时，
            #       要连它在 T=0 时的行为一起接管** —— 那不是边界情况，
            #       那是测试驱动慢路径的标准手法。
            _done, _ = await _a.wait({task},
                                     timeout=(0.25 if _left > 0.25
                                              else max(_left, 0.0)))
            if task in _done:
                return True
            if _left <= 0:
                return task.done()
            try:
                from core.runtime import inbox as _ib2
                if _ib2.submit_seq() > _seq0:
                    logger.info("[LongTask] 用户又说话了 → 立刻交还控制权，不等满阈值")
                    return False
            except Exception:
                pass

    async def _handle_set_next_checkin(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> ToolOutcome:
        # ⭐ 模型看完之后自己决定下一次 —— 系统只设过第一次。
        _sec = args.get("seconds")
        try:
            _sec = int(_sec) if _sec is not None else None
        except (TypeError, ValueError):
            _sec = None
        if _sec is not None and _sec <= 0:
            _sec = None      # <=0 当成「不再回看」
        _rk_sid = getattr(self, "_recheck_sid", "") or ""
        if not _rk_sid:
            # ⚠️ 不在回看轮里调它 → **如实说没有对象**，不假装排上了。
            #    📌 同 `task_boundary`：宁可少说一句，不许多断言一件事。
            return ToolOutcome(
                "There is no background job being checked in right "
                "now, so there is nothing to schedule. Do NOT tell "
                "the user you scheduled anything.", True)
        from core.runtime import waitcond as _wc_sn
        _ok_rk = _wc_sn.reschedule_wait(_rk_sid, _sec)
        if _ok_rk:
            return ToolOutcome(
                f"Next check-in set: {_sec} seconds from now."
                if _sec is not None else
                "No further check-ins; the completion signal will wake you. "
                "There is still a long safety net in case it never returns.")
        # ⚠️ 排不上最常见的原因是那件事**刚好完成了** ——
        #    那时它已经终态，重排本来就不该成功。
        return ToolOutcome(
            "Could not set a check-in — that job may have "
            "just finished. Do not assume it is still running.", True)

    def _park_carrier(self, car: dict, next_step: str, why: str = "") -> dict:
        """把一个还在前台上的载体放转入后台。**`dont_wait` 与系统自动转入后台的唯一实现。**

        ⭐ 两个调用方：
           · 模型自己调 `dont_wait`（一句话交代两件事的场景）
           · **用户一插话，系统自动调**（2026-08-22 已定，见下）
           📌 一个已经存在的形状，第二次出现时该复用它 ——
              同一件事有两个实现，它们只在「我两次想法相同」的前提下一致。

        ⚠️ 撤回看复用 `reschedule_wait(None)`，**不另写一套**。
        ⚠️ 抽屉记录交给 UI 侧建：Task 记录要和 carrier 的**生命周期**绑在一起
           （完成时收），而持有 carrier 的是 app。
           📌 一条记录的收尾必须落在拥有它真实终态的地方（逐字同形）。
        """
        try:
            from core.runtime import waitcond as _wc_pk
            _wc_pk.reschedule_wait(car["wait_id"], None)
        except Exception as _e_pk:
            logger.warning(f"[B1] 转入后台时撤回看失败（照旧继续）: {_e_pk}")
        # ⚠️ **一次性** —— 用掉就清。📌 一个「当前是哪个」的记录点如果不清，
        #    下一次会作用在一个早就结束的载体上，而且不报错。
        self._detachable_carrier = None
        logger.info(f"[B1] {why or 'park'}：{car.get('display','')[:40]} 移出手头 "
                    f"→ 进抽屉，下一步「{next_step[:40]}」")
        # ⚠️ **只造事件，不投递** —— 两个调用方的送法本来就不同
        #    （工具走 `event_queue.put`，查询主流程是 async generator 走 `yield`）。
        #    📌 一个函数不该同时决定「做什么」和「怎么把结果送出去」。
        return {
            "event": "carrier_detached",
            "bg_ref": car.get("bg_ref", ""),
            "display": car.get("display", ""),
            "next_step": next_step,
        }

    async def _handle_dont_wait(self, args: dict, aid: str, *,
                                event_queue, **_ctx) -> ToolOutcome:
        """「这个调用我不等了」—— 把它从**手头**移出去。

        ⭐ 三件事，一件都不多：
          ① 撤掉回看（那条等待只剩完成信号）
          ② 让它**进抽屉 + 计入 pill**（它现在真的是一件独立在跑的东西了）
          ③ 告诉模型「完成时我会叫你」，并堵死「把回看当下一步」那条退路

        ⚠️ **不碰那个载体本身。** 「不等它」改的是 Nano 的注意力，
           不是那条命令的生死 —— 与早先的设计那条同源：
           📌 **搁置的是注意力，不是执行体。**
        """
        _next = (args.get("next_step") or "").strip()
        _car = getattr(self, "_detachable_carrier", None) or {}
        if not _car.get("wait_id"):
            # ⭐ **回看轮也是一个合法的时刻**：「看了一眼，还早得很，我不等了」。
            #    那一轮没有经过 `_hand_back_long_task`，所以指向要从回看对象来。
            #    📌 同 `set_next_checkin` 不要模型传 id 的理由：这一轮只有一个
            #       对象，让它传等于多一个可以填错的地方。
            _rk = getattr(self, "_recheck_sid", "") or ""
            if _rk:
                try:
                    from core.runtime.kernel import get_kernel as _gk_dw
                    from core.runtime import waitcond as _wc_dw0
                    _r0 = _wc_dw0.find_by_id(_gk_dw(), _rk)
                    if _r0 is not None and _r0.is_live:
                        _car = {"wait_id": _r0.wait_id,
                                "bg_ref": _r0.bg_ref or "",
                                "display": (_r0.reason or "").replace(
                                    "still running: ", "") or "that job",
                                "action_id": ""}
                except Exception as _e_rk:
                    logger.warning(f"[B1] dont_wait 读回看对象失败: {_e_rk}")
        # ⚠️⚠️ **指向还活着吗** —— 记录点存在不等于对象还在。
        #    🔴 回看轮不走 `_handle_query_impl`，所以 `_detachable_carrier` 有可能
        #       是上一轮留下的；那条等待此刻多半已经终态。
        #       不验的话：`reschedule_wait` 静默返回 False，而模型收到「办好了」
        #       → 它会去做 `next_step`，并且以为有人会叫它回来。
        #    📌 **一个「稍后使用」的指向，使用前必须问一次它指的东西还在不在** ——
        #       否则失败会以「成功」的形状返回。
        if _car.get("wait_id"):
            try:
                from core.runtime.kernel import get_kernel as _gk_lv
                from core.runtime import waitcond as _wc_lv
                _r_lv = _wc_lv.find_by_id(_gk_lv(), _car["wait_id"])
                if _r_lv is None or not _r_lv.is_live:
                    logger.info(f"[B1] dont_wait 的指向已终态（{_car['wait_id']}）—— 当作没有")
                    _car = {}
                    self._detachable_carrier = None
            except Exception as _e_lv:
                logger.warning(f"[B1] dont_wait 校验指向失败: {_e_lv}")
        if not _car.get("wait_id"):
            # ⚠️ 没有可交出去的载体 → **如实说没有**，不假装办成了。
            #    📌 同 `set_next_checkin` / `task_boundary` 那条 fail-safe：
            #       宁可少说一句，不许多断言一件事。
            return ToolOutcome(
                "There is no slow call handed back to you right now, so there is "
                "nothing to stop waiting for. Do NOT tell the user you moved "
                "anything to the background.", True)
        if not _next:
            # 🔴 **这条闸就是「模型滥用 dont_wait」的修法。**
            #    实测抓到过：模型逢长任务就调它，而它其实无事可做 ——
            #    于是「不等」变成了「干等，只是没人管了」。
            #    📌 **「不等它」只有在「我有别的事要做」时才成立** ——
            #       填不出 `next_step`，恰恰证明这次不该调。
            return ToolOutcome(
                "dont_wait needs `next_step`: the other work you are going to do "
                "while it runs. If your next move is to wait for that call or use "
                "its result, do not call this tool at all - just carry on.", True)
        await event_queue.put(self._park_carrier(_car, _next, why="dont_wait"))
        return ToolOutcome(
            f"\"{_car.get('display','that call')}\" is no longer something you are "
            f"waiting on. It keeps running in the background and now shows up in the "
            f"user's task drawer; the runtime will wake you when it finishes.\n"
            f"Go do this now: {_next}\n"
            f"Do NOT schedule or perform a check on it, and do not tell the user you "
            f"will keep an eye on it - you will be woken up. If you finish the work "
            f"above and its result still has not arrived, that is the moment to look "
            f"at it.")

    async def _handle_wait_for(self, args: dict, aid: str, *,
                               event_queue, **_ctx) -> ToolOutcome:
        # 挂起/等待：登记一条挂起记录 + 触发等待 UI。
        # 不在这里阻塞——挂起的本质是"结束 turn 等触发"，所以这里只做
        # 登记和发事件，随后模型会用一句话告诉用户在等什么并结束本轮。
        _reason = (args.get("reason") or "").strip() or "external state change"
        # ⭐ 这个工具现在**只剩定时**一种，理由见 `_WAIT_FOR_MANIFEST`。
        #
        # ⚠️ 旧代码：`_wake_on = args.get("wake_on") or ["user"]` ——
        #    schema 的默认值和这里的兜底**双双指向 `user`**，
        #    模型只要不显式写 wake_on 就会拿到一条永远等不到东西的挂起。
        #
        # ⚠️ 老模型可能还会按旧 schema 传 `wake_on` / `bg_task_ref`（缓存里的旧描述、
        #    或历史对话里的旧例子）。**不静默忽略**：明确告诉它这些参数没了，
        #    否则它会以为自己挂了个"等用户"的等待、实际拿到的是定时。
        _legacy = [k for k in ("wake_on", "bg_task_ref") if args.get(k)]
        from core.runtime import waitcond as _wc_wait_for
        _wake_on = [_wc_wait_for.WakeSource.TIMER]
        _bg_ref = None
        # 计时器的数据形状不足以说明用户能不能操纵它：系统自己的回看同样
        # 有 timer。这里是模型理解用户委托后的判断，必须显式写出。
        _plan_intent = args.get("intent")
        _waiting_intent = ("scheduled_timer"
                           if _plan_intent == "scheduled_plan"
                           else "condition_recheck")

        _timer_seconds = args.get("timer_seconds")
        try:
            _timer_seconds = int(_timer_seconds) if _timer_seconds is not None else None
        except (TypeError, ValueError):
            _timer_seconds = None
        if not _timer_seconds or _timer_seconds <= 0:
            # 没有时间的定时等待 = 天生等不到的记录。直接退回，别让它进库。
            return ToolOutcome(
                "wait_for needs timer_seconds (how many seconds until Nano should check "
                "again). It no longer supports waiting on the user or on a background job: "
                "if you need the user to do something, just say so and end your turn — "
                "their next message resumes you. If a background job is running, the "
                "runtime resumes you when it finishes.", True)

        # ⭐⭐⭐ 写点搬到内核 —— **旧库从此不再被写**。
        #    切读那一步已经把 13 个读点 + resolve/cancel 切过来了，只剩这个
        #    `add()` 还写旧库，于是那段时间是**双写**（旧库 + 镜像），
        #    而镜像是 best-effort，所以要靠每次读 `_realign` 兜。
        #    📌 **一个「兜底对齐」机制的正确结局不是被加固，
        #       而是被它兜的那个风险消失。**
        # ⚠️ `session_id` 不再传：WaitCondition 没有这个概念。
        #    📌 迁移时**不许把旧模型里已经没有意义的字段一起搬过来**。
        _rec = _rt_wait_open(
            reason=_reason, wake_on=_wake_on,
            timer_seconds=_timer_seconds, bg_ref=_bg_ref,
            intent=_plan_intent)
        if _rec is None:
            # ⭐⭐ **登记失败 → 立刻如实返回，不发 UI 事件。**（2026-08-12 修）
            #
            # 🔴 旧行为有**两个**后果，而且都是"假的"：
            #   ① 这句诚实的话写了，但**下面的 `result_text` 无条件覆盖了它** ——
            #      模型实际收到的是 "Scheduled a re-check in N seconds…"，
            #      **那是一句假陈述**（什么都没登记）。
            #      📌 与 要修的问题同族：**一个写了但永远不生效的声明。**
            #   ② 更糟的一半（修的时候才查出来）：它**仍然会发 `suspend_waiting` 事件**，
            #      而 `suspension_id` 是空串。UI 侧两条路不对称 ——
            #      `_register_hidden_waiting` 有 `if not suspension_id: return` 保护，
            #      但 **`_make_pill_waiting` 没有**：于是 `scheduled_timer`（用户委托的
            #      定时计划）会画出一个「⏸ 等待中」pill + 倒计时 + [立即执行][取消计划]，
            #      而它注册在**空 key** 上 → 收尾时用真 id 永远找不到它 →
            #      **一个永远转圈的 pill，还带着两颗点了没用的按钮。**
            #
            # ⚠️ 它同时违反两条硬判据：
            #   · **宁可承认"不知道"，也不许替用户编一个用户没做过的动作**（模型侧）
            #   · **UI 必须是权威状态的忠实投影**（用户侧）——
            #     没有权威记录，就不该有对应的 UI 投影。
            # ⭐ 所以修法是「立刻返回」而不是「补一个空 id 判断」：
            #   📌 **不产生那个事件，比让下游各自记得防它更可靠**（下游有两条路，
            #      而它们的保护本来就不一致 —— 那正是这个 bug 能活下来的原因）。
            return ToolOutcome(
                "Could not register that wait, so nothing is pending. "
                "Tell the user plainly instead of implying you will check back.", True)
        _failed = False
        # 发等待事件给 UI（pill 位置渲染成"⏸ 等待中 · 计时器 · 等什么"）
        await event_queue.put({
            "event": "suspend_waiting",
            "action_id": aid,
            # UI 协议字段名本轮不迁移；值已经是唯一的 wait_id。
            "suspension_id": _rec.wait_id if _rec else "",
            "reason": _reason,
            "wake_on": list(_rec.wake_on) if _rec else [],
            "timer_at": _rec.fire_at if _rec else None,
            "timer_seconds": _timer_seconds,
            "waiting_intent": _waiting_intent,
        })
        # ⭐ 只剩定时一种，不再需要那张"源 → 描述"的映射表。
        return ToolOutcome(
            (f"Scheduled the user's plan for {_timer_seconds} seconds from now: \"{_reason}\".\n"
             if _waiting_intent == "scheduled_timer" else
             f"Scheduled a re-check in {_timer_seconds} seconds for: \"{_reason}\".\n")
            + (f"⚠️ You passed {_legacy}, which no longer exist. wait_for is now "
               "timer-only. If you were trying to wait on the user, just say what you "
               "need and end the turn — their next message resumes you. If you were "
               "waiting on a background job, the runtime resumes you when it finishes.\n"
               if _legacy else "")
            + ("Now tell the user briefly and naturally that their plan is scheduled. "
               if _waiting_intent == "scheduled_timer" else
               "Now tell the user briefly and naturally what you are waiting for and "
               "when you will look again, such as \"I'll check again in about 5 minutes\". ")
            + "Then end this turn and do not call more tools.\n"
            + ("⚠️ When you wake up, the time being up does NOT mean the thing you were "
               "waiting for has happened. Check first, then decide."
               if _waiting_intent == "condition_recheck" else
               "⚠️ When you wake up, the requested time has arrived: perform the reminder "
               "or action now."),
            _failed)

    async def _handle_task_boundary(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> ToolOutcome:
        # ⭐⭐ 「一件事」的边界 —— **只有模型能划**。
        #    三条各自独立的结论都要求 Task 跨多个 turn（迭代阅读的 scratchpad /
        #    仅本次 MCP / per-task 成本），而它们对「什么时候结束」的要求
        #    是同一句：**那个目标达成或放弃** —— 代码答不了。
        from core.runtime import task as _tkb
        _act = (args.get("action") or "").strip()
        if _act == "finish":
            _oc = (args.get("outcome") or "completed").strip()
            # ⚠️ 「做完了」和「放弃了」**必须分开** —— 早先已定
            #    「用户主动停掉不是失败」，归错会让人去排查一个不存在的问题。
            #    这里同源：**历史要读得出真相**。
            if _oc not in ("completed", "abandoned"):
                _oc = "completed"
            _done = _tkb.finish_conversation_task(_oc, args.get("note") or "")
            if _done:
                return ToolOutcome(
                    f"Marked that piece of work as {_oc}. "
                    f"⚠️ Anything you started under it (background jobs, timed "
                    f"waits) is NOT cancelled by this — cancel those separately "
                    f"if the user wants them stopped.")
            # ⚠️ 没有在进行的事 → **如实说没有**，不假装收了一件。
            #    📌 这正是那条 fail-safe 的兑现：宁可少说一句，
            #       不许多断言一件事（否则模型会向用户宣布一个假的完成）。
            return ToolOutcome(
                "There was no piece of work in progress, so nothing was "
                "closed. Do NOT tell the user you finished something.", True)
        if _act == "start":
            _goal = (args.get("goal") or "").strip()
            if not _goal:
                return ToolOutcome(
                    "task_boundary(start) needs `goal` — one short "
                    "line saying what this new piece of work is for. "
                    "Without it the record is useless later.", True)
            # ⚠️ **默认不结束旧的** —— 早先的设计：「并不要求 x 和 y 一定相关」
            #    ⭐ 2026-08-16 起旧那件事会被**显式搁置**（而不是留在前台），
            #      所以这里的回话也跟着改：要告诉它**旧的去哪了、怎么回去**。
            #      📌 一个工具的回话是模型对世界的唯一观测——
            #         它描述的行为变了，回话不改就是一句假话。
            _prev_before = None
            try:
                from core.runtime.kernel import get_kernel as _gk_tb
                _prev_before = _tkb.current_conversation_task(_gk_tb())
            except Exception:
                pass
            _new = _tkb.start_new_conversation_task(_goal)
            if not _new:
                return ToolOutcome(
                    "Could not start a new piece of work; carry on without it.")
            return ToolOutcome(
                f"Started a new piece of work: {_goal}."
                + (f" The previous one ({_prev_before}) is now PARKED - it is not "
                   f"finished, and anything it started is still running. "
                   f"Use action='resume' with that id to go back to it, or "
                   f"action='finish' after resuming if it is actually done."
                   if _prev_before else ""))
        # ⚠️⚠️ `park` **已退役（2026-08-20，Task 收窄）** —— 见 `task.py` 那段留痕。
        #    模型若照旧写 `park`，撞在这条如实的拒绝上，而不是被静默当成别的动作。
        #    📌 **一个被删掉的枚举值，必须让调用它的人撞到墙** ——
        #       静默忽略会让模型以为自己搁置成功了，然后向用户宣布一件没发生的事。
        if _act == "park":
            return ToolOutcome(
                "There is no 'park' action any more. The current piece of work is "
                "set aside automatically when you start a different one "
                "(action='start'). If you are simply not working on it right now, "
                "do nothing. To stop waiting for a slow CALL, use `dont_wait`.", True)
        if _act == "resume":
            _ok, _msg = _tkb.resume_conversation_task(args.get("task_id") or "")
            return ToolOutcome(_msg, not _ok)
        return ToolOutcome(
            "task_boundary needs action='finish', 'start' or 'resume'. "
            "If you are simply continuing, do not call this tool "
            "at all — continuing is the default.", True)

    async def _handle_stop_background(self, args: dict, aid: str, *,
                                      event_queue=None, **_kw):
        """真的停掉一件正在跑的东西。**不是「不再等它」，是「让它别跑了」。**

        ⚠️ 与另外两个刻意分开 —— 三个不同的意思，不许合并：
            · `dont_wait`   —— 别等了，但**让它继续跑**（转入后台）
            · `cancel_wait` —— 别回看了（等待记录收掉，载体照旧）
            · 本工具        —— **让它停下**
          📌 「答的不是同一个问题，就不合并」。

        🔴 它补的是一个把回看变成空谈的缺口：在此之前模型看到「这个办法坏了」，
           它能做的只有**不再看它** —— 而那件事还在跑。于是它换个新办法，
           **两个进程同时在跑**，而用户以为只有一个。

        ⚠️ **停不掉的要如实说，不许假装成功**：
           · 命令 → 能停（杀整棵进程树）
           · MCP  → 停不了（server 在对端）
           · Skill→ 停不了（`importlib` 进程内执行，Python 没有安全中断手段）
           📌 给模型一个错误的成功信号，比没有这个工具更糟 ——
              它会以为清干净了，然后在一个还在跑的东西上面盖新东西。
        """
        _ref = str(args.get("ref") or "").strip()
        if not _ref:
            return ToolOutcome(
                "stop_background needs `ref` - the id of the thing you want stopped. "
                "It is shown to you when a slow call is handed back to you.", False)
        # 命令这一类：真能停
        if _ref.startswith("cmd_"):
            try:
                from core.os_layer import longcmd as _lc_s
                if _lc_s.stop(_ref, "stopped by the model"):
                    return ToolOutcome(
                        f"Stopped {_ref} (the process and everything it started). "
                        f"It did not finish, so treat its result as unknown - not as failure.",
                        True)
                # ⚠️ 找不到不是错误：它多半刚跑完。
                #    📌 「已经结束了」和「停止失败」对模型的下一步完全不同。
                return ToolOutcome(
                    f"{_ref} was already finished - nothing to stop. "
                    f"Its result should be arriving on its own.", True)
            except Exception as e:
                return ToolOutcome(f"Could not stop {_ref}: {e}", False)
        # 其余：如实说停不掉
        return ToolOutcome(
            f"{_ref} is not something that can be stopped from here. External tool "
            f"calls run on the other side, and local skills run inside this process "
            f"with no safe way to interrupt them. It will keep running until it "
            f"finishes on its own. You can stop waiting for it, but do NOT tell the "
            f"user it has been stopped.", False)

    async def _handle_cancel_wait(self, args: dict, aid: str, *,
                                  event_queue, **_ctx) -> ToolOutcome:
        # 让"别等了"这句话真的能生效。理由见 manifest。
        _match = (args.get("match") or "").strip()
        try:
            from core.runtime.kernel import get_kernel as _get_wait_kernel
            from core.runtime import waitcond as _wc_cancel
            _pending = _wc_cancel.list_live(_get_wait_kernel(), oldest_first=True)
        except Exception as _ce:
            _pending = []
            logger.warning(f"[Suspension] cancel_wait 读取失败: {_ce}")

        if _match:
            # 宽松匹配：模型转述 reason 时常常不是逐字。
            # ⚠️ 匹配不上时**不要静默取消全部** —— 那是把"没听懂"变成"多杀一个"。
            _hit = [r for r in _pending
                    if _match in r.reason or r.reason in _match]
        else:
            _hit = list(_pending)

        if not _pending:
            return ToolOutcome(
                "There is nothing pending right now, so there was nothing to "
                "cancel. Tell the user plainly instead of implying you stopped "
                "something.")
        if not _hit:
            _names = "; ".join((r.reason or "?") for r in _pending)
            return ToolOutcome(
                f"No pending wait matched {_match!r}, so nothing was cancelled. "
                f"Currently pending: {_names}. "
                f"Ask the user which one they meant, or call again with the exact text.", True)
        for _r in _hit:
            _sid = _r.wait_id
            _wc_cancel.cancel_wait(_sid, resolved_by="model-cancel")
            # ⚠️ 这里原本还会关闭一次观测期的镜像 ——
            #    它关的是**镜像**，而镜像已经就是权威（上一行的
            #    resolve/cancel 直接走内核）。留着是**对同一件事关两次**，
            #    而且它依赖一个**重启就没的内存映射**。
            #    📌 双写拆掉之后，配对的「双关」也必须一起拆 ——
            #       留一半会让人以为还有另一套账。
        _done = "; ".join((r.reason or "?") for r in _hit)
        logger.info(f"[Suspension] 模型取消 {len(_hit)} 条等待：{_done}")
        return ToolOutcome(
            f"Cancelled {len(_hit)} pending wait(s): {_done}. "
            f"Nano will not check on those again. Confirm this to the user.")
