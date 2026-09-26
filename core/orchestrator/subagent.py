# core/orchestrator/subagent.py
"""子代理：派出、并发限制、运行循环与记录。（`Orchestrator` 的 mixin）"""

import asyncio

from loguru import logger

from core.orchestrator._runtime import _agent_scope_ctx
from core.orchestrator._types import ToolOutcome
from core.schema import ChatMessage, ToolResultBlock


class SubagentMixin:
    """子代理：派出、并发限制、运行循环与记录。"""

    # ══════════════════════════════════════════════════════════════════
    # Subagent
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐⭐ **上下文隔离天然正确，不需要为它写代码**（早先的设计 2026-08-13 预先核实）：
    #    Subagent有自己的 `ctx`，回到主模型的**只有最终报告**，而报告就是一条
    #    `tool_result`。要做的不是"实现隔离"，是**别破坏它** ——
    #    📌 任何"顺手把 Subagent 的中间步骤也塞给 main agent"的好意，都会直接毁掉这个机制
    #       的全部价值。
    #
    # ⭐⭐ **计价也天然并入 main agent**：`usage_tracker` 是进程级单例，`_record_usage`
    #    挂在 provider 层，所以Subagent的 token 自动进同一本账。
    #    ⚠️ 所以**别在 agent 那边另起一个计数器** —— 那会变成两份账。
    #    ⭐ 而归属对不对，靠的是第 0 步那个 `ContextVar`（见 `core/usage.py`）。
    #
    # ⚠️ **Subagent 进后台任务抽屉**（早先的设计 2026-08-12 定）：
    #    agent 是「**无法被回看的后台任务**」—— 进 Running/Finished、有终止 `■`、
    #    也计入 `x running task(s)` pill，与普通后台任务唯一差别是
    #    **主体模型不回看它的过程**（一看就丧失隔离的全部价值）。
    #    📌 「用户能不能看过程」和「主体模型能不能看过程」是两件事。

    # Subagent最多走几步。⚠️ 防失控，不是性能考虑 ——
    # 📌 一个没有步数上限的子循环，坏起来是"一直在跑、一直在花钱、没人看得见"。
    _AGENT_MAX_STEPS = 8

    # ⭐⭐ **同时最多几个Subagent**（2026-08-15 并行）。
    #
    # ⚠️ 这个数**不是性能上限，是成本与可观测性的上限**：
    #    每个Subagent都在独立烧 token，而用户在抽屉里一次能看明白的行数有限。
    # 📌 **一个"想开几个开几个"的并发，第一次失控时的表现是账单，
    #    而账单要到第二天才看得见。**
    # ⭐ 而「并发安全」这件事本身已经在第 0 步解决了（`usage_tracker` 的
    #    `RLock` + `ContextVar` 归属）—— 那两条正是为这一刻修的。
    _AGENT_MAX_PARALLEL = 3

    # ⭐⭐⭐ **Subagent干等多久就交还控制权**（2026-08-20，/）。
    #
    # 🔴 改造前 `spawn_agent` 是**同步 await 到底**的，docstring 还写着
    #    「「后台」在这里指的是上下文隔离，不是"不等它"」—— 而早先的设计同一页写的是
    #    agent 是「**无法被回看的后台任务**」。两句话打架，而实现选了前一句。
    #    后果 实测撞到了：**Subagent一跑，整条前台通道就堵死** ——
    #    插话只能进队列，那一段回应期的元信息行冻在写死的 `thinking · 0s`，
    #    等Subagent跑完才一次跳到 84s。
    # 📌 **一个东西如果在抽屉里叫「running task」，它就不该同时霸占前台。**
    #
    # ⭐ 5s 与 `_MCP_BG_THRESHOLD`(8s) / `_LONG_TASK_HANDBACK_SEC`(**5s**) 同族：
    #    它防的是**干等**，不是「异步」本身。所以短Subagent**一个字都不变** ——
    #    5 秒内回来的照旧同步返回 tool_result，用户完全无感。
    # ⚠️ 取更短是因为Subagent几乎不可能在 5 秒内有意义地跑完（它至少两次模型调用），
    #    而它堵住的东西比一条命令贵得多：整个前台。
    _AGENT_HANDBACK_SEC = 5.0

    def _a4_rec(self, job_id: str) -> dict:
        """取（或建）那个Subagent的记录。"""
        _tr = getattr(self, "_a4_transcripts", None)
        if _tr is None:
            _tr = self._a4_transcripts = {}
        return _tr.setdefault(job_id, {
            "label": "", "instruction": "", "started": 0.0, "ended": 0.0,
            "steps": [], "report": "", "ok": None,
        })

    def _agent_sem(self):
        """并发闸。⚠️ 懒建：`asyncio.Semaphore` 必须绑在**运行中的** loop 上。"""
        _s = getattr(self, "_a4_sem", None)
        if _s is None:
            import asyncio as _a
            _s = self._a4_sem = _a.Semaphore(self._AGENT_MAX_PARALLEL)
        return _s

    def agent_run(self, task_id: str) -> dict:
        """某个Subagent这一次运行的**完整记录**（监控抽屉照着它画）。

        ```
        {"label", "instruction", "started", "ended", "steps": [...], "report", "ok"}
          steps: [(工具名, 参数, 结果文本, 是否失败), ...]
        ```

        ⭐⭐ 它要能**在跑的过程中**被读到，不是跑完才有 ——
           📌 用户要的是**监控**，不是结果：花了多少、走到哪，得在它还在跑时看得见。
        """
        return dict((getattr(self, "_a4_transcripts", None) or {}).get(task_id) or {})

    def agent_transcript(self, task_id: str) -> list:
        """某个Subagent走过的每一步 `(工具名, 参数, 结果文本, 是否失败)`。

        ⭐⭐ **只活在本次运行的内存里，刻意不落盘。**
           📌 这不是省事，是**与抽屉的语义对齐**：已定的 Finished 保留规则
              就是「**本次运行产生的**」（关掉程序就清空）。
              transcript 的寿命如果比抽屉长，那多出来的部分**没有任何入口能看到它** ——
              而一份看不到的记录，只是磁盘上的垃圾。
        ⚠️ 与「主体模型不回看Subagent过程」不冲突：那条掐的是**模型**那一路，
           这里是**用户**那一路（早先的设计 2026-08-12 把这两件事拆开过）。
        """
        return list(self.agent_run(task_id).get("steps") or [])

    _AGENT_SYS = (
        # ⚠️ 开头这句也**不许只说 investigate** —— 它是整段里最先被读到的一句，
        #    只说"调查"会把「机械性批量修改」那一类悄悄框掉。
        #    📌 一段说明的第一句就是它的默认值。
        "You are a sub-agent dispatched by Nano to carry out one specific job "
        "- either finding something out, or making a set of precise, "
        "fully-specified edits - and report back once.\n"
        "You start cold: you cannot see the conversation that sent you, and you "
        "cannot ask questions. Work only from the instruction given.\n"
        # 🔴🔴 **实测 2026-08-20：这三句话把 整个作废了。**
        #    原文是「You can only read. You cannot change anything on this
        #    computer…」—— Subagent照着它回了一份「我的角色被限制为只读权限，
        #    无法完成第 3 步的文件改写」，而它手里**明明有 `edit_file`**。
        #    📌 那一族的最坏形态：能力在、模型看不见 —— 而这次更糟，
        #       **明确告诉了它相反的事**。工具表改了、它读的那份说明没改。
        #    ⚠️ 而它不会报错：一个被告知"我不能"的执行者，会安静地不去做。
        "You can read and search this computer, and you can make precise edits "
        "to existing files with `edit_file`.\n"
        "Every edit you make is shown to the user for approval first, exactly "
        "like Nano's own edits are. That is normal - do not treat it as a "
        "restriction, and do not ask permission in your report instead of just "
        "making the edit.\n"
        "If an edit is denied, or a capability is switched off, say so plainly "
        "in your report and move on with the rest of the task - do NOT retry it, "
        "do NOT look for a way around it, and do NOT abandon the whole job over "
        "one refused step.\n"
        "You cannot delete or move files, run commands, touch the screen or the "
        "user interface, write Skills, or dispatch further sub-agents. If the "
        "task truly needs one of those, report that back and let Nano do it.\n"
        "Investigate with the tools you have, then answer with your findings. "
        "Your answer goes back to Nano, not to the user, so write it as a report: "
        "state what you found, name the concrete files/terms involved, and say "
        "plainly what you could NOT determine. Do not pad it, and do not invent "
        "anything you did not actually read."
    )

    async def _run_agent_loop(self, instruction: str, aid: str, event_queue,
                              job_id: str = "") -> tuple[str, int]:
        """跑一次隔离的 ReAct。返回 `(报告, 用掉的步数)`。**不碰 main agent 的上下文。**"""
        from core.tools.catalog import ToolScope as _TS

        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        # ⭐ 工具集来自 **AGENT 作用域的白名单**（`builtin.D(..., agent=True)`）——
        #    📌 新加的内置工具只声明 MAIN，**天然不在这里**，不需要有人记得排除它。
        _defs = _cat.advertised(_TS.AGENT, _rtv)
        _manifest = [d.manifest for d in _defs]
        if not _manifest:
            return "（Subagent没有任何可用工具，无法调查）", 0

        # ⚠️⚠️ 上下文用 **`ChatMessage.to_dict()`**，不许手搓字典。
        #    🔴 第一版手写了 `{"role": "tool_results", ...}` —— 实测第一次派 Subagent
        #       就 400：`Invalid value for 'messages[2].role': 'tool_results'`。
        #       那个 role 是**项目内部的形状**；`to_dict()` 才负责把它翻成
        #       Anthropic 认的 `user + tool_result blocks`。
        #    📌 **一个已经有规范转换的形状，手搓第二份的代价不是"多写几行"，
        #       是「它在哪一步变形」这件事从此有两个答案。**
        #    ⭐ 走同一条转换，往后 provider 改格式这里自动跟；
        #       而且这正是 `t_f5_decay_l2` 那个假 provider 的教训的反面 ——
        #       那次是假 provider 和调用方**一致地错**，两边出自同一处。
        _msgs: list = [ChatMessage(role="user", content=instruction)]
        _steps = 0
        for _steps in range(1, self._AGENT_MAX_STEPS + 1):
            # ⚠️⚠️ **这里刻意不传 `stable_tool_count`。**
            #    Subagent用的是它自己的 `_manifest`，而 `_core_stable_n` 数的是
            #    主循环那份 `_core_manifest` 的前缀长度 ——
            #    📌 **一个「这份名单的前 N 个」的数字，配错名单就会把断点
            #       打在错的位置上，而且不报错**（缓存照样"工作"，只是命中率变差）。
            #    ⭐ 不传 → 退回旧行为（断点打在最后一个上），对Subagent是正确的：
            #       它的工具集在一次Subagent任务内本来就是固定的。
            _dec, _model = await self.provider.chat_with_tools(
                [m.to_dict() for m in _msgs], _manifest, self._AGENT_SYS)
            _calls = list(getattr(_dec, "tool_calls", None) or [])
            if not _calls:
                return (getattr(_dec, "content", "") or "").strip(), _steps

            _m = ChatMessage(role="tool_calls", content="")
            _m.tool_calls = list(_calls)
            # ⚠️ thinking blocks 必须原样带回（签名不能改写），否则下一轮 400。
            _m.thinking_blocks = list(getattr(_dec, "thinking_blocks", None) or [])
            _msgs.append(_m)
            _results = []
            for c in _calls:
                # ⚠️ **用 AGENT 作用域 resolve** —— 这一行就是隔离的执行侧闸门：
                #    模型哪怕幻觉出 `os_execute`，这里也拿不到 handler。
                #    📌 的教训：给出去的一定要执行得了；反过来
                #       「执行得了却没给出去」是正常的，而这里是第三种：
                #       **既没给出去、也执行不了** —— 那才是真正的隔离。
                _ref = _cat.resolve(c.name, _TS.AGENT, _rtv)
                if _ref is None:
                    _txt, _err = (f"[Tool not available to sub-agents: {c.name}]", True)
                else:
                    try:
                        _out = await getattr(self, _ref)(
                            c.args or {}, aid, event_queue=event_queue,
                            call=c, used_model=_model, gui_waited=False)
                        _oc = ToolOutcome.of(_out)
                        _txt, _err = _oc.text, _oc.failed
                    except Exception as _e:
                        logger.warning(f"[A4] Subagent工具 {c.name} 失败: {_e}")
                        _txt, _err = f"[Tool error] {_e}", True
                _results.append(ToolResultBlock(
                    name=c.name, tool_use_id=c.tool_use_id,
                    content=_txt, is_error=_err))
                # ⭐ 记一步，供抽屉里的 View transcript 用。
                if job_id:
                    _rec = self._a4_rec(job_id)
                    _rec["steps"].append((c.name, dict(c.args or {}), _txt, _err))
            _rm = ChatMessage(role="tool_results", content="")
            _rm.tool_results = _results
            _msgs.append(_rm)

        # ⚠️ 撞上限也要**如实说**，不许假装这是完整结论。
        #    📌 一份没说自己没跑完的报告，会被 main agent 当成定论继续往下推。
        _last = ""
        for _m2 in reversed(_msgs):
            if getattr(_m2, "tool_results", None):
                _last = str(_m2.tool_results[-1].content or "")[:1500]
                break
        return (f"（⚠️ Subagent走满了 {self._AGENT_MAX_STEPS} 步仍未收敛，以下是"
                f"它最后掌握的情况，**不是完整结论**）\n" + _last, _steps)

    async def _handle_spawn_agent(self, args: dict, aid: str, *,
                                  event_queue, used_model: str = "", **_ctx) -> str:
        """派一个 Subagent。**超过 `_AGENT_HANDBACK_SEC` 就把控制权交回 main agent。**

        ⭐⭐ Subagent是「**无法被回看的后台任务**」。三件事同时成立：
          · 进抽屉 / 计入 pill / 有 `■`        —— 它是一件真的在跑的东西
          · main agent **不干等它**（超 5s 就交还）—— 否则一件"后台"的事堵死前台
          · **永远不回看**（`recheck=False`）   —— 回看 = 把它走过的步骤灌回主
            上下文，那正好抵消掉上下文隔离的全部价值

        ⚠️ **快路径（5 秒内跑完）与改造前逐字一致**：报告作为 tool_result 直接回
           main agent。📌 一个阈值防的是「干等」，不该让不干等的那些也改变行为。

        ⚠️ **权威记录的收尾始终在 `_agent_runner` 里**，不交给载体那一层 ——
           理由见下面 `owns_record=False` 那一行。
        """
        _ins = (args.get("instruction") or "").strip()
        _label = (args.get("label") or "").strip() or (_ins[:28] or "子任务调查")
        if not _ins:
            return "[spawn_agent] instruction 是空的，没有可调查的东西。"

        from core.runtime.task import create_background_job, finish_background_job
        _job = None
        try:
            _job = create_background_job(f"Agent · {_label}")
        except Exception as _e:
            # ⚠️ 建不了记录**不阻止Subagent跑** —— 那只是抽屉里少一行。
            #    📌 治理/展示层的故障，不许把能力本身搞掉。
            logger.warning(f"[A4] Subagent的后台任务记录建不了（不影响执行）: {_e}")
        _jid = str(getattr(_job, "task_id", "") or _job or "")
        if _jid:
            import time as _t4
            _rec0 = self._a4_rec(_jid)
            _rec0.update({"label": _label, "instruction": _ins,
                          "started": _t4.time()})

        def _wrap(_report: str, _steps: int) -> str:
            # ⚠️ 报告**原样回给 main agent**，由它转述给用户——
            #    📌 main agent 是协调者；两个声音同时说话会让用户分不清谁在负责。
            #    ⭐ 用户想看原文时看得到：抽屉里那条的 View transcript
            #       与聊天区工具卡展开（那个出口）。
            return (f"[Sub-agent report · {_label}]\n"
                    f"（investigated in {_steps} step(s); this text is the agent's own "
                    f"words — relay it to the user in your own voice, and say so if it "
                    f"reports something it could not determine）\n\n{_report}")

        async def _agent_runner() -> str:
            # ⭐⭐ `begin_agent()` 搬进**这条协程里**（改造前在 handler 体内）。
            #    `create_task` 只在建任务那一刻复制上下文，之后两边各自独立 ——
            #    于是这个归属 ContextVar 只作用在Subagent自己这一支。
            #    🔴 留在 handler 里的话，交还之后 main agent 继续跑的模型调用会**接着**
            #       落在同一个 context 上，被记进这个Subagent的账。
            #    📌 一个用来「归属」的 ContextVar，必须在它所归属的那条执行链里 set。
            _ok, _report, _steps = True, "", 0
            # ⭐ 这一支从此带着「我是Subagent」这个事实往下走 ——
            #    授权弹窗据它画来源标识，确认闸据它决定「用户说话算不算取消」。
            #    ⚠️ 与 `begin_agent()` 放在一起是刻意的：两者都是**归属**，
            #       都必须在它们所归属的那条执行链里 set。
            _agent_scope_ctx.set(_label)
            if _jid:
                try:
                    from core.usage import usage_tracker as _ut4
                    _ut4.begin_agent(_jid)
                except Exception:
                    pass
            _outcome = "completed"
            try:
                # ⭐ 并发闸：多个Subagent可以同时在跑，但不超过 `_AGENT_MAX_PARALLEL`。
                #    ⚠️ 排队时那条**已经在抽屉的 Running 段里**
                #       （`create_background_job` 在闸之前）——
                #       📌 用户该看到「它在排队」，而不是「它不存在」。
                async with self._agent_sem():
                    # ⭐⭐ **拿到 slot 才算「在跑」** —— 在此之前它是 ACTIVE+IDLE，
                    #    抽屉如实显示「排队中」。
                    # 🔴 实测 2026-08-20：抽屉里一个**正在跑**的Subagent一直显示「排队中」。
                    #    根因是 `mark_background_running()` 的唯一调用方是
                    #    `_run_bg_task`，而它的唯一调用方是 `_start_bg_task` ——
                    #    后者在 2026-08-10「系统交还不再写后台 Task 权威记录」之后
                    #    就**零生产调用方**了。于是这行状态**再也没有人写过**，
                    #    每一个真的后台任务都永久停在 IDLE。
                    #    📌 **一条状态如果只有一个写入者，那个写入者一死，
                    #       它就变成一个永远不会改变的谎** —— 而读它的人不会报错。
                    #    ⚠️ 这是同一条死路的第三个受害者（前两个：抽屉那颗 `■`、
                    #       `_bg_tasks` 恒空）。
                    try:
                        from core.runtime.task import mark_background_running as _mbr
                        _mbr(_jid)
                    except Exception as _e_mr:
                        logger.debug(f"[A4] Subagent转 RUNNING 失败（照旧跑）: {_e_mr}")
                    _report, _steps = await self._run_agent_loop(
                        _ins, aid, event_queue, job_id=_jid)
                if not _report:
                    _ok, _report = False, "（Subagent没有给出任何结论）"
                _outcome = "completed" if _ok else "failed"
            except asyncio.CancelledError:
                # ⭐⭐ 用户点了抽屉里那颗 `■`。**`cancelled` 不许并进 `failed`** ——
                #    早先那条实测结论：用户主动停掉不是失败，归进 failed
                #    会让模型和用户都去排查一个不存在的问题。
                _ok, _outcome = False, "cancelled"
                _report = ("（这个Subagent被用户手动终止了，没有给出结论。"
                           "不要自行把它重新派一遍。）")
                if _jid:
                    try:
                        finish_background_job(_job, _outcome, "用户手动终止")
                    except Exception:
                        pass
                    import time as _t6
                    self._a4_rec(_jid).update(
                        {"report": _report, "ok": False, "ended": _t6.time()})
                logger.info(f"[A4] Subagent「{_label}」被用户终止")
                # ⚠️ **必须原样抛上去**：载体那一层靠它区分「被终止」与「失败」，
                #    吞掉就变成一次假的失败（app 侧 `_handback_await` 同款纪律）。
                raise
            except Exception as _e:
                _ok, _outcome = False, "failed"
                _report = f"（Subagent执行失败：{_e}）"
                logger.warning(f"[A4] Subagent失败: {_e}")
            if _job:
                try:
                    finish_background_job(_job, _outcome)
                except Exception:
                    pass
            if _jid:
                import time as _t5
                self._a4_rec(_jid).update(
                    {"report": _report, "ok": _ok, "ended": _t5.time()})
            logger.info(f"[A4] Subagent完成「{_label}」：{_steps} 步 / "
                        f"{'成功' if _ok else '失败'}")
            return _wrap(_report, _steps)

        _task = asyncio.ensure_future(_agent_runner())
        # ⭐ 同两条长任务：用户一开口就立刻交还，不等满 5 秒。
        if await self._wait_or_user_speaks(_task, self._AGENT_HANDBACK_SEC):
            # 快路径：它已经回来了 —— 形状与改造前完全一致，调用方不用改。
            return _task.result()

        # ── 慢路径：交还控制权，Subagent继续跑 ──────────────────────────────────
        # ⚠️ `recheck=False` —— Subagent是唯一走这一档的：**它永远不回看**。
        # ⚠️ `detachable=False` —— 它**已经**不在手头了，再给模型一个
        #    `dont_wait` 是让它对一件已经做完的决定再决定一次。
        #    📌 一个工具只该出现在「它还能改变什么」的时刻。
        # 🔴 **实测第一次就炸在这里**：上一版写的是 `f"agent_{_jid or id(_task):x}"`,
        #    而 `:x` 作用在**整个** `_jid or id(_task)` 上 —— `_jid` 非空时它是 str，
        #    于是 `Unknown format code 'x' for object of type 'str'`。
        #    📌 **一个格式说明符作用的是整个表达式，不是它的某一个分支** ——
        #       而这条路只在「记录建成功了」时走，正好是最常见的那条。
        _agent_ref = "agent_" + (_jid or format(id(_task), "x"))
        _txt = await self._hand_back_long_task(
            display=f"Agent · {_label}", bg_ref=_agent_ref,
            action_id=aid, event_queue=event_queue,
            recheck=False, detachable=False)
        # ⭐ Subagent**生来**就有权威记录（`create_background_job` 在最上面），
        #    所以载体这一层只拿它当「那颗 `■` 要终止谁」的钥匙，
        #    **不负责收尾**（`owns_record=False`）。
        #    📌 一条记录只能有一个收尾人 —— 两个都收的表现是
        #       「后收的那个把先收的结论覆盖掉」，而它不会报错。
        from core.runtime import carriers as _carriers
        _carriers.start(f"Agent · {_label}", _task, _agent_ref,
                        rt_task_id=_jid or None, owns_record=False)
        await event_queue.put({
            "event": "long_task_handback",
            "bg_task_ref": _agent_ref,
            "display": f"Agent · {_label}",
        })
        return _txt
