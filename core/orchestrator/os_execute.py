# core/orchestrator/os_execute.py
"""os_execute（含子代理只读版）与临时代码执行。（`Orchestrator` 的 mixin）"""

import asyncio

from loguru import logger

from core.orchestrator._runtime import _rt_lease_acquire, _rt_wait_open
from core.orchestrator._types import ToolExecution, ToolOutcome
from core.orchestrator.screen import _vision_model_for_os


class OsExecuteMixin:
    """os_execute（含子代理只读版）与临时代码执行。"""

    async def _handle_run_scratch_code(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        """跑一段用完就扔的 Python。

        ⭐ 三段：**扫 → （必要时）确认 → 跑**，每一段都复用现成的东西：
          扫   `core.code_scan`（与 Skill 审计**同一份**扫描器）
          确认 `execution_confirm` 事件（与运行有副作用的 Skill **同一个弹窗**）
          跑   `core.temp_exec` → `longcmd`（长任务交还 / 落盘 / 回收全白拿）
        📌 全程没有一样是为这个工具新造的 —— 「直接跟正常 skill 的体感一致」。
        """
        import asyncio as _aio
        from core import temp_exec as _te

        code = (args.get("code") or "").strip()
        purpose = (args.get("purpose") or "").strip()
        if not code:
            return "run_scratch_code was not executed: `code` is empty."

        # ── ① 扫 ──────────────────────────────────────────────────────
        from core import code_scan as _cs
        cats, findings, syn_err = _te.prepare(code)
        if syn_err:
            # ⚠️ 语法错误在**执行前**就返回，不起进程。
            # 📌 让模型拿到 `SyntaxError: invalid syntax (line 3)` 这一句，
            #    比让它拿到一个非零退出码 + 一段 traceback 快得多 ——
            #    要的是「失败信息足够选出下一步」。
            return (f"The code was NOT run: it does not parse.\n"
                    f"SyntaxError: {syn_err}\n"
                    f"Fix the syntax and call the tool again.")

        # ── ② 确认（只在扫出副作用时；Auto 下直接通过）─────────────────────
        from core.os_layer import dsl as _dsl_auto
        if cats and not _dsl_auto.auto_skips_confirmation(
                f"临时代码「{purpose or 'scratch code'}」"):
            _confirm_ev = _aio.Event()
            _cancelled = [False]
            _loop = _aio.get_running_loop()

            def _on_confirm():
                _loop.call_soon_threadsafe(_confirm_ev.set)

            def _on_cancel():
                _cancelled[0] = True
                _loop.call_soon_threadsafe(_confirm_ev.set)

            from core.runtime import replies as _replies
            _rid = _replies.register({"confirm": _on_confirm, "cancel": _on_cancel})
            await event_queue.put({
                "event": "execution_confirm",
                # ⚠️ 复用 `skill_name` 这个字段名 —— UI 那边照它渲染标题。
                #    📌 为了一个「其实不是 Skill」而给事件加一个新字段，
                #       会让所有消费方都得处理两种形状。名字略不精确，
                #       换来的是**零改动接进现有弹窗**。
                "skill_name": purpose or "临时代码",
                # ⭐ 传**中文描述**，不传类别键 —— 与 Skill 那条路一字不差。
                "side_effects": _cs.labels_for(cats),
                # ⭐⭐ 代码带进弹窗 —— 让用户知道**自己在授权什么**。
                # 🔴🔴 UI 侧必须以**只读**方式渲染它。
                #    理由不只是保险：**风险类别是 AST 在【这段代码】上扫出来的，
                #    用户一改，授权就和被授权的东西对不上了** —— 他可以把一段
                #    「无副作用」的代码改成写文件的，而结论还挂着旧的。
                #    ⚠️ Skill 那边能改，是因为改完**还会再过一遍审计管线**；
                #       一次性执行**没有第二遍**。
                "preview_code": code,
                "reply_id": _rid, "actions": ["confirm", "cancel"],
            })
            from core.runtime import inbox as _ib
            try:
                _oc = await _ib.wait_confirm_or_user_message(_confirm_ev, 300)
            finally:
                _replies.discard(_rid)
            if _oc != _ib.ConfirmOutcome.CONFIRMED:
                _cancelled[0] = True
            if _cancelled[0]:
                # ⚠️ 同 Skill 那条：**用户改口** vs **干等超时**要分开告诉模型。
                #    📌 两者对「下一步该干什么」的含义完全不同。
                if _oc == _ib.ConfirmOutcome.USER_MESSAGE:
                    return ("The code was NOT run.\n"
                            + _ib.cancelled_by_user_message_note())
                return ("The code was NOT run: the user did not approve it. "
                        "Do not retry the same code - either ask what to change, "
                        "or find another way.")

        # ── ③ 跑 ──────────────────────────────────────────────────────
        try:
            lc = _te.start(code, display=purpose or "scratch code")
        except Exception as e:
            return f"The code could not be started: {type(e).__name__}: {e}"

        from core.os_layer import longcmd as _lc
        _te.cleanup()          # 顺手清过期的临时文件（家务，失败不影响）

        if await _lc.await_briefly(lc, self._LONG_TASK_HANDBACK_SEC,
                                   stop_when=_lc.foreground_interrupt()):
            res = lc.final_result()
            _lc.forget(lc.ref)
            return self._format_scratch_result(res, cats)
        if _lc.turn_stop_requested():
            # 用户按了终止：前台正在跑的代码一起停（裁决 73），不交还、不唤醒。
            _lc.stop(lc.ref, "stopped by user (turn stopped)")
            await _lc.await_briefly(lc, 2.0)          # 等进程真正退出再从注册表移除
            _lc.forget(lc.ref)
            return ("The code was stopped: the user stopped this turn, so it was terminated "
                    "before it finished. Its effects may be partial.")

        # ⭐ 超过前台耐心 → 走**同一条**长任务交还合同（与 MCP / run_command 同）。
        #    📌 「我们要识别的只有长任务，跟任务类型从来没有关系过。」
        return await self._hand_back_long_task(
            display=purpose or "临时代码",
            bg_ref=lc.ref, action_id=aid, event_queue=event_queue)

    @staticmethod
    def _format_scratch_result(res: dict, cats: list) -> str:
        """把 `longcmd` 的结果转成给模型的一段话。

        ⚠️ **非零退出码不是「工具坏了」，是「代码报错了」** —— 这两者对模型
           的含义完全不同：前者该换个工具，后者该改代码。
           📌 失败信息必须**正确**，而且要**足够选出下一步**。
        """
        d = res.get("data") or {}
        out = (d.get("output") or "").strip()
        rc = d.get("returncode")
        if res.get("ok"):
            head = "The code ran successfully."
            if not out:
                # ⚠️ 空输出是个真实的坑：模型算完了但忘了 print。
                #    📌 明说「没有输出」比给一个空串强 —— 空串会被读成
                #       「结果就是空的」，而真相是它压根没打印。
                return (head + " It printed nothing, so there is no result to show. "
                        "If you expected a value, add a print() and run it again.")
            return f"{head} Output:\n{out}"
        # 失败：把退出码和输出都给出去
        return (f"The code ran but exited with code {rc} - this is an error in the "
                f"code itself, not a problem with the tool. Output (stdout+stderr):\n"
                f"{out or '(nothing was printed)'}")

    async def _handle_os_execute_readonly(self, args: dict, aid: str, **_ctx):
        """Subagent作用域里的 `os_execute` —— **只读**。

        ⭐⭐ **为什么是一个独立 handler，而不是给 `_handle_os_execute` 加个标志**：
           目录的 `bindings` 本来就是「**不同作用域真的有不同的 handler**」——
           把只读做成 binding，"在Subagent里只能只读"这件事就由**目录解析**保证，
           而不是由"每个调用点记得传 `readonly_only=True`"保证。
           📌 **一个靠「记得传参」维持的安全边界，等于把它交给了下一个人的记性。**
           ⚠️ 而且默认值那一侧永远是危险的那侧：忘了传 = 全权限。

        🔴 双保险，两层各有理由：
           ① 这里先按 `dsl.is_readonly()` 挡一次 —— 为了给模型**一句有用的话**
              （告诉它这个执行者只能读），而不是让它撞一个通用错误。
           ② 底下 `OSDispatcher(readonly_only=True)` 再挡一次 —— 那才是**闸**。
           📌 上面那层是**说明**，下面那层是**执行** ——
              说明可以漏，执行不许漏，所以两层都要有。
        """
        from core.os_layer import dsl as _dsl
        _act = (args or {}).get("action") or ""
        if _act and not _dsl.is_readonly(_act):
            # 🔴🔴 **这句话原来是错的，而且它把Subagent逼进了死路**（实测 2026-08-20）。
            #
            # 原文：「`{_act}` **会改变这台电脑**」—— 对 `file_read` 来说那是**假话**：
            # 它一个字节都不改，它落在只读名单外是因为 `_ACTIONS` 表里
            # `readonly=False`（那张表把「只读」和「要不要授权」写在了同一格：
            # `file_read` 的 `floor=2`，读任意文件确实该确认）。
            # 于是Subagent收到一句它无法反驳、也无法绕开的话，就**放弃了整件事** ——
            # 而它手上明明有 `load_full_file`（v1.47 已确认绝对路径直接放行）。
            #
            # 📌 那条逐字适用：**给模型的失败信息必须同时【正确】且【充分】** ——
            #    这里两条全犯了：说了假因，也没给出口。
            # 📌 那条同源：**模型需要的是一个出口，不是一个名字。**
            #
            # ⚠️ **闸一个字没动**：判据仍然只有 `dsl.is_readonly()` 这一个出处，
            #    下面 `OSDispatcher(readonly_only=True)` 那道真闸照旧。
            #    改的只是**说明**那一层（handler 里这一层的职责本来就是"给一句有用的话"）。
            # ⚠️ 下面这张是**建议**表，不是安全名单 —— 📌 安全名单不许手抄，
            #    而它是从 `readonly_actions()` 派生的；这张表只回答「那你可以改用什么」，
            #    写错了最坏的后果是一句没用的建议，不会放行任何东西。
            _ALT = {
                "file_read": "`load_full_file`（读整份文件，绝对路径可以直接给）"
                             "或 `search_files`（只想在文件里找某段内容时用它）",
                "clipboard_read": "",
                "run_command": "",
            }
            _alt = _ALT.get(_act, "")
            return (f"[os_execute] 这个执行者（Subagent）只能调只读动作，`{_act}` 不在"
                    f"只读名单里。"
                    + (f"\n⭐ 你要做的这件事**有别的路**：改用 {_alt}。\n"
                       if _alt else
                       f"\n可用的只读动作：{', '.join(sorted(_dsl.readonly_actions()))}。\n")
                    + f"如果这件事**必须**动真实文件、或者确实没有替代品，"
                      f"就把你已经查到的东西如实报回给 main agent，由它来做 —— "
                      f"不要因此宣布任务无法完成。")
        return await self._handle_os_execute(
            args, aid,
            call=_ctx.get("call"), used_model=_ctx.get("used_model", ""),
            gui_waited=_ctx.get("gui_waited", 0.0),
            event_queue=_ctx.get("event_queue"),
            _readonly_only=True,
        )

    async def _handle_os_execute(self, args: dict, aid: str, *, call, used_model: str,
                                 gui_waited: float, event_queue, **_ctx):
        """`os_execute` —— 从分派链机械提取，**行为零变化**。

        ⚠️ 它是唯一会**提前返回 `ToolExecution`** 的 handler（两处）：
           ① 机器被别的持有者占着 ② 长命令被交还后台。
           那两处的语义是「这次调用已经自己完成了全部收尾」，所以调用方
           检测到 `ToolExecution` 就直接返回 —— 这正是 Flow Runner 的雏形：
           📌 **Catalog 决定"谁处理"，Runner 决定"怎么运行这一类 handler"。**
        """
        name = call.name
        _gui_waited = gui_waited
        result_text = ""
        _is_failed = False
        # ⚠️ 下面这一整段判据，cutover 之前长在 `_execute_one_tool_call`
        #    的 `elif name == "os_execute":` 分支里。那条分派链已被目录取代，
        #    而**判据讲的是这个 handler 内部的事**，所以随它搬到这里。
        #    📌 删一段代码时，长在它身上的「为什么」要跟着搬到新家 ——
        #       注释掉队比代码掉队更难发现（下面 finally 里那句「理由见上方那段」
        #       指的就是它，搬之前那句已经指向空气了）。
        # ⭐⭐ **租约覆盖一整段 GUI 操作，不是一个动作。**
        #
        # 已经持有就不再拿（幂等），并且**不在本次调用的 finally 里归还** ——
        # 归还点是每轮开头（见 `_handle_query_impl` 的每轮重置）。
        #
        # ⚠️ 这一条是推演验收预期时才发现的，值得写清楚：
        # `os_execute` 是**单步**工具，一次调用只做一个动作。原先每次调用
        # 都 acquire/release，于是 Nano 真正"持有机器"的窗口只有单个动作那一瞬，
        # 而**两步之间那段模型思考时间（好几秒）里没人持有**。
        # 传感器只在 Nano 持有时武装（判据 ③），所以用户的点击**大概率落在空窗里**，
        # 被动挂起等于形同虚设。
        #
        # 📌 建模错在哪：Nano 在一串 GUI 动作中间思考下一步点哪儿的时候，
        #    **鼠标仍然是它的**。每步都还锁等于宣称"我这会儿没在用电脑" ——
        #    那不是事实。**租约的粒度必须匹配「这台电脑归谁用」这件事本身的粒度，
        #    不是匹配代码的调用结构。**
        # ⭐⭐ **只有真正碰鼠标键盘的动作才需要租约。**（2026-08-07 实测修）
        #
        # 实测：让 Nano 用 GUI 打开一个 txt，它实际走了**命令行**，
        # 而这时 用户点桌面**照样**被判成"已让出控制"。原话：
        # 「覆盖的是 GUI 模拟，不是整个 OS 控制能力。
        #   命令行为什么要收到被动挂起传感器的影响」——**对**。
        #
        # 📌 判据：**被动挂起争的是「谁在用鼠标键盘」这一个资源，
        #    不是「谁在用这台电脑」。** 跑一条命令、读一个文件、截一张图
        #    都不与用户争鼠标 —— 用户点桌面不会让 `dir` 的结果失效。
        #    把它们也挡住，是把"互斥资源"的范围放大到了整个 OS 能力。
        #
        # ⚠️ 所以租约**按需**取：第一个鼠标键盘动作才 acquire，
        #    之后整段 GUI streak 一直持有（粒度理由见下）。
        #    纯命令行的一轮**一次都不 acquire**，也就完全不受被动挂起影响。

        try:
            from core.os_layer import dsl as _dsl_mk
            _needs_machine = _dsl_mk.contends_for_machine(
                (args or {}).get("action") or "")
        except Exception:
            # 判不出来就当需要 —— 宁可多要一次租约，也别让 GUI 动作漏过闸
            _needs_machine = True

        # ⚠️ acquire 不在这里做了 —— 统一挪进下面那段"等待"里，
        #    因为"要不要等"和"要不要拿"必须由**同一个判断**决定
        #    （分成两处写就是上一版那个 bug：句柄在手里 → 跳过等待）。

        # ⭐⭐ 被动挂起的**第一道闸**：机器在用户手里就别开工。
        # ⚠️ 只对需要鼠标键盘的动作生效（见上）。
        #
        # 拿不到租约不是故障，是"这台电脑现在归用户"。
        # 在这里挡比在 dispatch 里挡更好 —— **一步都还没做**，
        # 不会留下"做了一半"的现场让模型去猜环境变成什么样了。
        # ⚠️ 话术必须**说清是谁占着**，不能统一成一句"你先操作"：
        #    触发源不一定是用户（也可能是别的程序抢了前台），
        #    对一个没动过手的用户说"你先操作"比不说更糟。
        # ⭐⭐⭐ **拿不到就在这一轮里等，不是失败。**
        #
        # 这是本项目里最贵的一个形态错误。第一版做成了"闸"：
        # 拿不到 → 动作失败 → 模型只能"别重试" → **结束这一轮**。
        # 于是用户每发一次「继续」都是新的一轮，进来立刻撞墙、再结束 ——
        # 实测表现为"永久锁死"。
        # 📌 **「闸」和「挂起」在代码里长得像，行为相反：
        #    闸的出口是失败，挂起的出口是等待再继续。**
        #    一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。
        #
        # ⚠️ 等待期间**零 LLM 调用、零 token** —— 只是 `asyncio.sleep`。
        #    所以"等 3 分钟"对预算免费，只占一个挂着的 turn。
        # ⚠️ 即时提示**不在这里发**：顶部那条状态条自己轮询租约（1 秒重画），
        #    用户一动手就出现，不依赖这里跑到哪儿。
        # ⚠️⚠️ **判据是「现在还有没有资格」，不是「手里有没有句柄」。**
        #
        # 第一版写的是 `self._rt_os_lease is None` —— 于是"跑到一半被用户抢走"
        # 这种**最常见**的情况整段被跳过：Nano 手里那个句柄还在，
        # 但它指向的租约早已 PREEMPTED。然后 dispatch 那道闸快速失败、
        # 模型结束这一轮 —— 实测 逐条坐实：
        #   20:45:07 用户接管 → 20:45:10 [OS-Dispatch] 让位，不执行 type_text
        #   **全程没有任何「进入等待」的日志。**
        #
        # 📌 **一个"我还持有"的判断，不能拿"我曾经拿到过"来回答。**
        #    与 同形：历史只能证明它曾经成立，不能证明它现在仍然成立。
        # ⚠️ **等待已经在函数最顶上统一做过了**（GUI 模式 → 任何工具前都等），
        #    所以这里只剩"确保手里有租约"。理由见顶部那段：
        #    📌 感知的范围和反应的范围必须一致，而统一在入口做才不会漏掉
        #       非键鼠的那些工具。
        _waited = _gui_waited
        if _needs_machine:
            try:
                from core.runtime import oslease as _ol_w
                from core.runtime.kernel import get_kernel as _gk_w
                # ⚠️ 判据是「现在还有没有资格」，不是「手里有没有句柄」——
                #    句柄可能指向一条已被抢占的租约。
                _may_w, _why_w = _ol_w.nano_may_touch_os(_gk_w())
                if not _may_w:
                    self._rt_os_lease = None
                elif self._rt_os_lease is None:
                    self._rt_os_lease = _rt_lease_acquire(self, "os_execute")
            except Exception as _e_w:
                logger.warning(f"[OSLease] 取租约异常: {_e_w}")
                self._rt_os_lease = None

        # 等到上限还是拿不到 —— 这时候才体面收场
        # ⏸ 调度器落地后，这里应改成「登记一个 continuation」而不是结束。
        # ⚠️⚠️ **2026-08-22 试过一版又拆了**（见 `runtime/scheduler.py` 的墓碑）：
        #    当时建了一类 `DEFERRED_ACTION` Task + 一个 blocker provider + 一个 tick。
        #    已定拆掉 —— **不是因为这个缺口不真**（下面那句
        #    "you will pick this up again when they are done" 至今没人兑现），
        #    而是因为**它不值得一套新机制**。
        #    ⭐ 真要做：用既有的 `set_next_checkin` 在这里排一次回看（几行），
        #       那正是 v1.52 对同类问题给过的答案（「变回手头的活不需要任何新机制」）。
        #    📌 别再造第二套 —— 那条路已经走过了。
        if _needs_machine and self._rt_os_lease is None:
            _who = "the user"
            try:
                from core.runtime import oslease as _ol_r
                from core.runtime.kernel import get_kernel as _gk_r
                _ok_r, _why_r = _ol_r.nano_may_touch_os(_gk_r())
                if not _ok_r:
                    _who = _why_r
            except Exception:
                pass
            logger.info(f"[OSLease] 等满 {_waited:.0f}s 仍拿不到，本轮收场: {_who}")
            # ⭐⭐⭐ 2026-08-22 落地 —— **先排一次回看，再说那句承诺。**
            #
            # 🔴 问题：下面那句 "you will pick this up again when they are done"
            #    是 Nano 对用户的一句**承诺**，而这条路**压根没登记过任何等待** ——
            #    于是没有任何东西会把它叫回来。
            #    v1.63 把【载体】那一半做通了（排空时队列空 → 扫后台 →
            #    重排回看），但它扫的是 `bg_ref` 非空的记录 ——
            #    **租约这条没有载体，扫不到它。**
            # 📌 **一句模型已经在对用户说、而系统兑现不了的承诺，比没有这句话更坏。**
            #
            # ⭐ 不造新机制（墓碑里写死了）：2026-08-22 试过一套 `DEFERRED_ACTION`
            #    Task + blocker provider + tick，已定拆掉 ——
            #    不是因为缺口不真，而是**它不值得一套新机制**。
            #    这里用的就是 `wait_for` 那个**已经存在的形状**：`_rt_wait_open`
            #    + 纯定时唤醒。📌 一个已经存在的形状，第二次出现时该复用它。
            #
            # ⚠️ 间隔直接用 `_FIRST_RECHECK_SEC`，**不另发明一个数字** ——
            #    📌 它们答的是同一个问题（"过多久回头看一眼"），
            #       而三个宽限期各自演化成 45/90/? 那个问题刚修完。
            _lease_rec = None
            try:
                from core.runtime import waitcond as _wc_lease
                _lease_rec = _rt_wait_open(
                    reason=f"the computer is held by {_who}",
                    wake_on=[_wc_lease.WakeSource.TIMER],
                    timer_seconds=float(self._FIRST_RECHECK_SEC),
                    intent="condition_recheck")
            except Exception as _e_lw:
                logger.warning(f"[OSLease] 排回看失败（照旧收场）: {_e_lw}")
            _picked_up = bool(_lease_rec)
            result_text = (
                f"Waited {_waited:.0f}s but the computer is still held by someone else "
                f"({_who}). This is not an error and not something to diagnose.\n"
                "⚠️ Do NOT retry this action — retrying means grabbing the mouse back "
                "while they are still using it.\n"
                "Stop here. Tell the user briefly and naturally what you observed "
                "(only what the reason above actually says — do not assume it was the "
                "user if it does not say so)"
                + (", and that you will pick this up again when they are done — the "
                   f"runtime will bring you back in about {self._FIRST_RECHECK_SEC:.0f} "
                   "seconds to look again."
                   if _picked_up else
                   # ⚠️⚠️ 排不上就**不许说那句承诺** —— 📌 同 `wait_for` 登记失败
                   #    那处的纪律：宁可承认「不知道」，也不许替用户编一个
                   #    用户没做过的动作；一个写了但永远不生效的声明就是要修的问题。
                   ". ⚠️ Nothing is scheduled, so do NOT say you will check back or "
                   "pick this up later — say plainly that they can ask you again.")
                + " Then end the turn."
            )
            await event_queue.put({
                "event": "tool_end", "action_id": aid,
                "result_summary": result_text[:80], "status": "SYS_IDLE",
                "model": used_model, "current_skill": name, "ok": False,
            })
            # ⭐ 只在**真的排上了**时才给 UI 那个等待 pill。
            #    📌 UI 必须是权威状态的忠实投影：没有权威记录，就不该有 UI 投影。
            #    （`wait_for` 那处抗过这个坑：空 `suspension_id` 会画出一个
            #      永远转圈、按钮点了没用的 pill。）
            if _picked_up:
                await event_queue.put({
                    "event": "suspend_waiting",
                    "action_id": aid,
                    "suspension_id": _lease_rec.wait_id,
                    "reason": f"the computer is held by {_who}",
                    "wake_on": list(_lease_rec.wake_on),
                    "timer_at": _lease_rec.fire_at,
                    "timer_seconds": float(self._FIRST_RECHECK_SEC),
                    "waiting_intent": "condition_recheck",
                })
                logger.info(f"[OSLease] 已排一次回看（{_lease_rec.wait_id}，"
                            f"{self._FIRST_RECHECK_SEC:.0f}s 后）—— [B1] 租约那一半")
            return ToolExecution(call=call, result_text=result_text, ok=False,
                                 error="machine held by another holder")
        import json as _json_os
        from core.os_layer.dispatch import OSDispatcher
        from core.os_layer.safety import OSSessionSafety
        if not hasattr(self, "_os_safety"):
            self._os_safety = OSSessionSafety()
        _os_dispatcher = OSDispatcher(
            session_id=getattr(self, "_session_id", ""),
            m1_mode=False, m2_mode=True, m3_mode=True,
            # ⭐ 只读执行者（Subagent）由 `_handle_os_execute_readonly` 传进来。
            #    ⚠️ 这一层才是**闸** —— 上面那层只是为了给模型一句有用的话。
            readonly_only=bool(_ctx.get("_readonly_only")),
            safety=self._os_safety, provider=self.provider,
            vision_model_override=_vision_model_for_os(),
        )
        _os_result = None
        # ⚠️⚠️ 这个 `try/finally` **只包镜像，不碰 `_os_task_busy`**。
        #
        # 第一版没有它，镜像和旧 bool 挂在**完全相同**的位置 —— 结果实测
        # 测出来的是"两边一致"，而事实上那次泄漏真的发生了：
        # 按一下 Escape 之后 **3 分半**旧 bool 还是 True，只是镜像也一起漏，
        # 所以对答案永远 match。**一个照抄了 bug 的 shadow 测不出那个 bug。**
        #
        # 📌 判据：**shadow 要镜像的是「被建模的那个现实」，不是
        #    「旧实现对现实的记录」。**
        #    早先镜像挂起记录是对的 —— 那里记录本身就是现实；
        #    而这个 bool 是一个**关于现实的断言**（"OS 正在忙"），
        #    镜像必须跟着**真实的 OS 工作**走，两边才有可比性。
        #
        # ⚠️ 旧 bool 依然不动（观测期不切权威）—— 修它是切读那一步的事。
        # ⭐⭐ 动作前后各拍一次顶层窗口快照：**新冒出来的窗口就是这次动作开的。**
        #    这是"谁干的"这个问题在窗口层的答案 —— 键鼠层有 `LLKHF_INJECTED`
        #    可以问系统，窗口层没有，**夹在动作前后的差集**是最接近它的东西。
        #    绑定的有效期挂在活动租约上（见 `window_binding` 模块头），
        #    所以**不需要任何人记得重置**。
        try:
            from core.os_layer import window_binding as _wb
            _wb_before = _wb.snapshot()
        except Exception:
            _wb, _wb_before = None, None

        # ⭐⭐ [ActionAttempt] 开一条尝试。**状态推进到 IN_FLIGHT 不在这里，
        #    在 `dispatch` 真正调执行器之前那一行** —— 因为这中间还有
        #    定位、授权确认（可能等用户点很久），那段时间现实一点没变。
        _att_id = None
        try:
            from core.runtime import attempt as _att_m
            _att_id = _att_m.begin(
                action=(args or {}).get("action") or "",
                tool_name="os_execute",
                summary=self._get_tool_catalog().presentation("os_execute", args or {}),
                turn_id=getattr(self, "_rt_turn_id", None),
                detail={"params": (args or {}).get("params") or {}})
        except Exception:
            _att_id = None
        try:
            async for _os_ev in self._execute_dsl_step(args, _os_dispatcher, self._os_safety, used_model):
                if "_step_result" in _os_ev:
                    _os_result = _os_ev["_step_result"]
                else:
                    await self._ui_sink(event_queue).put(_os_ev)
        finally:
            if _wb is not None and _wb_before is not None:
                try:
                    _wb.bind_if_new(_wb_before,
                                    (getattr(self, "_rt_os_lease", None) or ("", 0))[0])
                except Exception:
                    pass
            # ⚠️⚠️ **这里【不】归还活动租约** —— 归还点在每轮开头。
            #    理由见上方那段：租约覆盖一整段 GUI 操作，不是一个动作；
            #    在这里还了，两步之间就没人持有，被动挂起形同虚设。
            #
            # ⚠️ 这里**曾经**有一行 `self._os_task_busy = False` 的止血，
            # 以及一大段"能不能用 finally"的辨析。切写那一步已把那个 bool 删掉，
            # 但那条辨析值得留：
            # 📌 **同样是「标志没复位」，能不能用 finally 取决于它被谁在什么时候读。**
            #    `_os_task_busy` 唯一用途是抑制 canary → 能用 finally；
            #    `_active_tool_batch_open` 的用途是"异常时告诉
            #    `_clean_damaged_memory` 可以回滚" → **刻意不能**用 finally
            #    （会在外层读到之前清零，等于把机制废掉）。
            pass
        if _os_result is None:
            _os_result = {"ok": False, "error": "step returned no result"}

        # ⭐⭐⭐ [统一长任务 2026-08-09] **长命令走的是和 MCP 完全同一条
        #    交还合同**（`_hand_back_long_task`）。
        #
        # 用户的原则：「我们说的是『耗时长的任务』，跟任务类型从来就没有
        # 关系过……用户需要区分任务吗，我们要识别的就是『长任务』。」
        # 📌 **一个机制如果只有一个接入点，那它可能不是机制，
        #    只是那一处的实现细节。** 这是它的第二个接入点。
        #
        # ⭐ 而这条路上回看**比 MCP 那条有价值得多**：命令的 stdout 里
        #    有 pip 的百分比、下载速度、报错 —— 回看那一眼真的看得见东西。
        _lr = (_os_result.get("data") or {}).get("long_running")
        if _lr and _os_result.get("ok"):
            _lc_ref = (_os_result.get("data") or {}).get("ref", "")
            _lc_disp = (_os_result.get("data") or {}).get("display", "command")
            result_text = await self._hand_back_long_task(
                display=_lc_disp, bg_ref=_lc_ref,
                action_id=aid, event_queue=event_queue)
            # 把「等它结束」这件事做成一个 awaitable 交给后台生产端。
            # ⚠️ 用 `to_thread` 包同步的 `join` —— 读线程已经在跑了，
            #    这里只是「等它」，不是「再跑一次」。
            async def _await_longcmd(_r=_lc_ref, _attempt_id=_att_id):
                import asyncio as _aio
                from core.os_layer import longcmd as _lc2
                try:
                    _res = await _aio.to_thread(_lc2.join, _r)
                except _aio.CancelledError:
                    # 用户在抽屉里按了 ■（进程由载体表停掉）：这次尝试也要收尾，
                    # 否则下一条命令开始时它还挂着（「没收尾就被下一条顶掉」）。
                    try:
                        from core.runtime import attempt as _att_c
                        _att_c.finish(_attempt_id, False, reason="stopped by user")
                    except Exception:
                        pass
                    raise
                _lc2.forget(_r)
                _out = (_res.get("data") or {}).get("output", "")
                # ⭐ `join` 已经给出这条**同一个**命令的真实终态；
                # 现在才有资格收 ActionAttempt。交还那一刻收会把
                # 「还在跑」伪造成结论；由 UI 的完成文字反推又会把展示
                # 形状误当成事实。完成载体本身同时知道结果与 attempt id，
                # 所以收口必须在这里。
                #
                # ⚠️ 不因 `forget()` 失败而影响账本收尾：它只清观测缓冲，
                # 不是命令是否结束的依据。
                try:
                    from core.runtime import attempt as _att_m3
                    _att_m3.finish(
                        _attempt_id, bool(_res.get("ok")),
                        reason=str(_res.get("error") or "")[:200])
                except Exception:
                    pass
                if _res.get("ok"):
                    return f"command finished successfully:\n{_out[-1500:]}"
                return (f"command finished with a problem "
                        f"({_res.get('error','')}):\n{_out[-1500:]}")
            from core.runtime import carriers as _carriers
            _carriers.start(_lc_disp, _await_longcmd(), _lc_ref)
            await event_queue.put({
                "event": "long_task_handback",
                "bg_task_ref": _lc_ref,
                "display": _lc_disp,
            })
            # ⚠️⚠️ **刻意【不在这里】收尾这条 ActionAttempt。**
            #
            # 🔴 第一版在这里写了 `_att_m2.finish(_att_id, True, ...)`，
            #    被 `t_f1_stage5_attempt` 判红 —— 而它是对的：
            #    **那次尝试根本没有结束，它还在跑。**
            # 📌 **不许为一件还没结束的事记一个结论** ——
            #    而 `ActionAttempt` 的全部意义就是「恢复后能说清
            #    结果可不可信」，被交还的长命令**恰恰是「结果未知」那一格**。
            # ⭐ 所以留在 `IN_FLIGHT` 才是它的诚实状态：
            #    进程真的死了，启动收尾会把它标成中断（那也是真话）。
            # ⭐ 已兑现：attempt id 作为 `_await_longcmd` 的闭包参数
            #    随同一条载体走到 `join()` 的真实结果处才 finish。
            return ToolExecution(call=call, result_text=result_text, ok=True)

        # 用户中断（Ctrl+` 急停 / 甩角 failsafe / 确认弹窗点取消）：
        # 本次动作判失败并告诉模型别再试，但【不】置任何会话级标志——
        # 取消一个动作不等于"从此不许再操作电脑"（软急停已删除，见 safety.py 模块头）。
        _is_abort = (
            (_os_result.get("is_control_flow") and
             _os_result.get("data", {}).get("reason") == "USER_ABORT")
            or _os_result.get("aborted")
            or _os_result.get("error") in ("用户已取消", "user cancelled")
        )
        # 急停（Ctrl+` / 甩角 failsafe）结束 GUI 任务：收回临时授权，界面随后恢复窗口。
        if _os_result.get("aborted"):
            self._gui_task_end("emergency stop")

        # ⭐⭐ [ActionAttempt] 收尾。**这里有一个关键分叉：**
        #
        # 如果这一步跑完之后**机器已经不归 Nano 了**（用户中途接管），
        # 那它是**被打断**的，不是"失败"——走 `INTERRUPT`，让内核按
        # commit boundary 判 `effect_state`（已 IN_FLIGHT → PARTIAL_OR_UNKNOWN）。
        #
        # 🔴 走 `FINISH(ok=False)` 会把它记成"失败"，而**"失败"暗示"没生效"** ——
        #    可现实里那半截字已经在记事本里了。**那是假事实。**
        # 📌 「中断了」和「失败了」在结果上长得像，在**语义上完全相反**：
        #    失败 = 可以放心重做；中断 = 结果不可信、重做可能重复副作用。
        try:
            from core.runtime import attempt as _att_m2
            from core.runtime import oslease as _ol_a
            from core.runtime.kernel import get_kernel as _gk_a
            _still_mine, _ = _ol_a.nano_may_touch_os(_gk_a())
            if _is_abort or not _still_mine:
                _att_m2.interrupt(_att_id, "用户接管了这台电脑"
                                  if not _still_mine else "用户中断（急停/取消）")
            else:
                _att_m2.finish(_att_id, bool(_os_result.get("ok")),
                               reason=str(_os_result.get("error") or "")[:200])
        except Exception:
            pass

        if _is_abort:
            _is_failed = True
            result_text = ("The user interrupted this screen action. Do not retry it. "
                           "Respond in text and let the user decide what to do next.")
        else:
            _is_failed = not _os_result.get("ok", False)
            result_text = _json_os.dumps(_os_result, ensure_ascii=False, default=str)
            # ⭐ 见 `_file_read_truncation_note` 的完整推导。
            # ⚠️ **必须前置**：截断切的是尾巴（`content[:limit]`），
            #    附在后面的话，正好在需要它的时候被切掉。
            #    📌 形状同下面的 `_win_note` —— 那条也是前置，同一个理由。
            _fr_note = self._file_read_truncation_note(
                (args or {}).get("action") or "", _os_result, result_text)
            if _fr_note:
                result_text = _fr_note + result_text
            # ⭐ 窗口易主也要在**动作结果**里说，不能只在 look_at_screen 里说 ——
            #    模型完全可以不看屏幕就连着动手，那条路上它同样需要知道对象换了。
            #    （2026-08-07 那次数据损坏正是"看了一眼 → 认错对象 → 直接动手"。）
            _win_note = self._window_identity_note()
            # ⚠️ 门槛从"前台变了"放宽到"目标窗口有任何异常" —— 后者才是决定性的。
            #    只看"前台变了"会漏掉最阴的一种：目标窗口被**最小化**了，
            #    前台没变（还是它自己所在的那个进程/桌面），但它已经不在屏幕上。
            if any(k in _win_note for k in ("CHANGED", "NOT in the foreground",
                                            "MINIMIZED", "is GONE", "MOVED")):
                result_text = _win_note + "\n" + result_text
            # ⭐⭐ 等过就必须说 —— **恢复后不许假装什么都没发生。**
            #    ⚠️ 这是 三层防护网第 1 层的兑现："挂起前后的环境不能默认一致"。
            #    ⭐ 完整版的两半后来都落地了：`ActionAttempt`（说清"上一个动作做到哪了、
            #       结果可不可信"，见下一段）与接管日志（"用户这段时间干了什么"，
            #       `core/proactive/takeover_log.py`）。这里保留的是最小版本：
            #       至少让它知道自己被打断过、等了多久、别拿旧的屏幕认知往下走。
            if _waited >= 1.0:
                # ⭐⭐ [ActionAttempt] 恢复提示不再只说"环境可能变了"（笼统），
                #    而是说清**上一个动作是什么、结果可不可信**。
                #    这正是指出的那个缺口：
                #    「恢复后该告诉模型什么」以前答不上来。
                _att_note = ""
                try:
                    from core.runtime import attempt as _att_m3
                    from core.runtime.kernel import get_kernel as _gk_n
                    _att_note = _att_m3.describe_for_model(
                        _att_m3.last_terminal(
                            _gk_n(), getattr(self, "_rt_turn_id", None)))
                except Exception:
                    _att_note = ""
                # ⚠️⚠️ **这段文案原来最后一句是「Verify the current state
                #    before your next action.」——第 2 层落地时必须改掉。**
                #    那句话把"核实"写成了**无条件义务**，等于绕回早先的设计
                #    纠正过的老路：「用户动过屏幕就必须截图一次」。
                #    📌 判据：**第 2 层是主路径、日志必读；
                #       第 3 层是条件兜底 —— 日志能完成行为还原就不需要截图。**
                #    📌 而真正的判据不是「环境变了没有」，是
                #       **「这个变化影不影响我下一步」** —— 用户往 Nano
                #       正要清空的那个记事本里打了个字，环境确实变了，
                #       但结论是什么都不用做。按"变了就核实"做，这里白烧一次截图。
                result_text = (
                    f"[Resumed after {_waited:.0f}s] The user took control of the "
                    f"computer and you waited for them. The screen may have changed "
                    f"while you were paused — do NOT rely on what you saw before the "
                    f"pause. Read the pause log above and decide whether the change "
                    f"actually affects your next step; if it clearly does not, just "
                    f"continue.\n"
                    + (_att_note + "\n" if _att_note else "")
                ) + result_text
        self._last_called_skill = "os_execute"
        return ToolOutcome(result_text, _is_failed)
