# core/orchestrator/file_tools.py
"""Orchestrator 的这一部分：文件与知识库工具：试读、分段读取、搜索、编辑、知识库检索与 ambient 指代解析。"""

import asyncio

from loguru import logger

from core import rag as rag_engine, reading as _READING
from core.memory_store import EntryType
from core.orchestrator._types import ToolOutcome


class FileToolsMixin:
    """文件与知识库工具：试读、分段读取、搜索、编辑、知识库检索与 ambient 指代解析。"""

    async def _handle_list_knowledge_files(self, args: dict, aid: str, *,
                                           event_queue, **_ctx) -> str:
        return await asyncio.to_thread(rag_engine.list_knowledge_files_for_agent)

    async def _handle_edit_file(self, args: dict, aid: str, *,
                                event_queue, used_model: str = "", **_ctx) -> str:
        """精确修改。**自己不写盘** —— 算完新内容后走 `file_write` 那条路。

        🔴🔴 这个 handler 的**全部安全性**来自最后那一步：
             它把结果交给 `_execute_dsl_step`，于是
             **地板 / 确认弹窗 / 审计 / 路径策略全是 `os_execute` 那一套**。
        📌 2026-08-13 的硬约束：**换一个暴露层，不许换掉它底下的安全层。**
           ⚠️ 如果哪天有人为了"少一次弹窗"把这里改成直接 `Path.write_text`，
              那一刻这个工具就变成了绕过确认的后门 —— 而它看起来只是"简化了一下"。
        """
        from pathlib import Path as _P

        from core.os_layer import fileedit as _fe
        from core.os_layer import pathpolicy as _pp

        _path = (args.get("path") or "").strip()
        _edits = args.get("edits") or []
        if not _path:
            return "[edit_file] 没有给 path。"
        _why = _pp.denied_reason(_path)
        if _why:
            return f"[edit_file] 这个位置不允许操作（{_why}）。"

        _f = _P(_path)
        if not _f.exists():
            # ⚠️ 不许顺手创建 —— 📌 「改一个不存在的文件」几乎总是路径写错了，
            #    而替用户建一个空文件会把一个明显的错误变成一个安静的错误。
            return (f"[edit_file] 文件不存在：{_path}。"
                    f"要新建请用 os_execute 的 file_write。")
        try:
            _orig = await asyncio.to_thread(_f.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return f"[edit_file] 这个文件不是 UTF-8 文本，无法精确修改：{_path}"
        except Exception as e:
            return f"[edit_file] 读不了：{e}"

        _res = _fe.apply_edits(_orig, _edits)
        if not _res.ok:
            # ⚠️ 失败**什么都没写** —— 全有或全无（见 `fileedit` 模块头）。
            return f"[edit_file] 没有改动：{_res.error}"

        # ── 🔴 交给 OS 层：地板 / 弹窗 / 审计 全在这一步 ──
        # ⭐⭐ **授权卡要看的是「要做什么」，不是「做完长什么样」**
        #    （2026-08-15 定的，参照 Claude Code）：
        #        授权卡 = 行为（接下来要执行什么）
        #        pill 展开 = 结果（做了什么、变了什么 → diff）
        #    📌 这正是 自己写过的那条判据 ——
        #       **「事前授权」和「事后审计」是两个不同的问题，
        #         一个界面回答不了两个** —— 当时写了，接线时却没用上。
        #
        # 🔴 所以这里**不能**把 `_res.content`（整份新文件）当预览：
        #    那既不是"要做什么"（要做的是「把这几行换掉」），
        #    也不是"做了什么" —— 它只是路由到 `file_write` 之后漏出来的实现产物。
        _preview_lines = []
        for _i2, _e2 in enumerate(_edits, 1):
            if not isinstance(_e2, dict):
                continue
            _preview_lines.append(f"# 第 {_i2} 处")
            _preview_lines.append("- " + str(_e2.get("old_text", "")).replace("\n", "\n- "))
            _nt = str(_e2.get("new_text", ""))
            _preview_lines.append("+ " + _nt.replace("\n", "\n+ ") if _nt else "+ （删除）")
            _preview_lines.append("")
        _instr = {"action": "file_write",
                  "params": {"path": str(_f), "content": _res.content,
                             "mode": "overwrite",
                             # ⚠️ 只给授权卡看，不参与执行（executor 只读 path/content/mode）。
                             "_preview": "\n".join(_preview_lines).rstrip()},
                  "reason": f"edit_file：{_res.applied} 处修改 "
                            f"(+{_res.added}/-{_res.removed})"}
        from core.os_layer.dispatch import OSDispatcher
        from core.os_layer.safety import OSSessionSafety
        if not hasattr(self, "_os_safety"):
            self._os_safety = OSSessionSafety()
        _dsp = OSDispatcher(
            session_id=getattr(self, "_session_id", ""),
            m1_mode=False, m2_mode=True, m3_mode=True,
        )
        _os_result = None
        async for _ev in self._execute_dsl_step(_instr, _dsp, self._os_safety,
                                                used_model):
            if "_step_result" in _ev:
                _os_result = _ev["_step_result"]
            else:
                # ⭐ Subagent跨过自己那一轮之后要走轮外通道 —— 见 `_ui_sink`。
                await self._ui_sink(event_queue).put(_ev)

        if not (_os_result or {}).get("ok"):
            _err = (_os_result or {}).get("error") or "被拒绝或取消"
            return f"[edit_file] 写入没有发生：{_err}（文件未改动）"

        self._wm_add(
            EntryType.FILE_READ, str(_f), "file_edited",
            detail=f"+{_res.added}/-{_res.removed}", tags=["file", str(_f)],
        )
        # ⭐ 回给模型的是**摘要 + diff**，不是整份新内容 ——
        #    📌 它刚刚才给出那些改动，把整个文件还给它是纯粹的上下文浪费。
        return (f"[edit_file] 已修改 {_f.name}：{_res.applied} 处，"
                f"+{_res.added}/-{_res.removed} 行。\n{_res.diff}")

    async def _handle_search_files(self, args: dict, aid: str, **_ctx) -> str:
        """找文件/找内容。**只读** —— 写仍然只能走 `os_execute`。

        ⚠️ 丢进线程跑：`os.walk` + 逐行读是**阻塞**的，
           📌 一次在大目录上的搜索能把整个事件循环卡住几秒，
              而那期间 UI 的转圈、pill、终止按钮全部不响应。
        """
        from core.os_layer import filesearch as _fs
        _root = (args.get("path") or "").strip()
        _name = (args.get("name_pattern") or "").strip()
        _content = (args.get("content") or "").strip()
        try:
            _res = await asyncio.to_thread(
                _fs.search, _root,
                name_pattern=_name, content=_content,
                regex=bool(args.get("regex")),
                recursive=bool(args.get("recursive", True)),
                exclude=(args.get("exclude") or ""),
                max_results=int(args.get("max_results") or _fs.MAX_RESULTS),
            )
        except Exception as e:
            logger.warning(f"[Search] search_files 失败: {e}")
            return f"[search_files] 搜索失败：{e}"

        # ⭐⭐ **grep 命中过的文件，视同已试读。**
        #
        # 🔴 实测 2026-08-26 抓到的浪费：模型先撞上试读闸 →
        #    转去 `search_files` 拿到了**精确行号** → 然后又调 `load_full_file`
        #    → **又被拒** → 下一轮才去 peek。**闸白白吃掉了两次工具调用。**
        # 📌 根因是设计缺陷，不是模型笨：
        #      试读的目的 = **「在没有线索时提供线索」**
        #      而此刻它已经有 grep 给的行号了 —— 那正是试读要给的那种线索，
        #      **而且比试读更精准**。
        #    ⇒ 逼它再去试读，是让它花一轮去获取一份**它已经拥有的东西**。
        # ⚠️ 只有**内容命中**（grep）才算：只按文件名找到的（Glob）不算 ——
        #    📌 「知道这个文件存在」和「知道它里面有什么」是两回事，
        #       而试读闸拦的是后者。
        if _content:
            for _h in (_res.get("hits") or []):
                _p = getattr(_h, "path", None) or (
                    _h.get("path") if isinstance(_h, dict) else None)
                if _p:
                    self._peeked[self._peek_key(str(_p))] = "via_search"

        return _fs.render(_res, root=_root, name_pattern=_name, content=_content)

    # ══════════════════════════════════════════════════════════════════
    # 迭代阅读：试读闸 + scratchpad
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐ **闸绑 Task，不绑 turn**（同早先对 scratchpad 的处置）：
    #    任务会被挂起、丢后台、被插队 —— 绑 turn 的话，任务一挂起，
    #    「这个文件试读过了」这件事就没了，模型回来还要再试读一次。
    # ⚠️ 而它**只是一张便签，不是权威状态**：读不出 Task id 就退化成
    #    「按没试读过处理」（fail-safe 方向 = 多问一句，不是多做一步）。
    _peeked: dict = {}

    def _peek_key(self, filename: str) -> str:
        try:
            from core.runtime import task as _tk
            _tid = _tk.ensure_conversation_task("") or "no-task"
        except Exception:
            _tid = "no-task"
        return f"{_tid}::{filename}"

    async def _read_file_text(self, filename: str, with_images: bool,
                              aid: str, event_queue) -> str:
        """把文件读成文本。**两个工具共用这一条路。**

        📌 判据只能有一处：`peek_file` 和 `load_full_file` 拿到的必须是
           **同一份文本**，否则「第 200 行」在两个工具里指的是不同的东西。
        """
        def _rag_progress(msg: str):
            event_queue.put_nowait({"event": "tool_progress", "action_id": aid,
                                    "message": msg})
        async with self._rag_parallel_sem:
            return await asyncio.to_thread(
                rag_engine.load_full_file, filename, with_images, _rag_progress)

    def _ambient_entries(self) -> list:
        """实时 buffer + trail 里**所有带句柄**的条目，统一成 (ts, line, ref)。

        ⚠️ 两个源都要查：`ambient_trail.recent()` 刻意 `exclude_last_sec=600`，
           而 trail 每 4 分钟才写一次 —— **「刚才」那一条几乎总在实时 buffer 里**。
        """
        # 🔴🔴 **收「有 ref」，不是「有 path」**（第一版写成后者，自己把出路堵死了）。
        #
        #    `▸` 标记的判据确实是 **path 非空** —— 拉一条只有名字的回来，
        #    注入里本来就有那个名字，**白烧一轮**（已明确 ②）。
        #    但**查找**不能用同一个判据：模型拿一个没标 ▸ 的时刻来问时，
        #    该听到的是「这条只有名字，路径没确认，你可以 search_files」，
        #    而不是「没有这个条目」。📌 已明确 ④：
        #    **拉到一条没东西的，工具要直说「这条只有这些」，而不是回一个空。**
        # ⚠️ 第一版的后果很阴：`_handle_...` 里那整个 unconfirmed 分支
        #    **永远不会被执行** —— 代码在、测试不写就永远发现不了。
        out = []
        try:
            from core.proactive.activity import get_buffer
            for w in (get_buffer().snapshot().get("windows") or []):
                if getattr(w, "event", "") != "focus":
                    continue
                _r = getattr(w, "ref", None)
                if isinstance(_r, dict) and (_r.get("path") or _r.get("name")):
                    out.append((float(w.ts), w.window_title or "", _r))
        except Exception:
            pass
        try:
            from core.proactive import ambient_trail
            for r in ambient_trail.recent(hours=12, limit=60, exclude_last_sec=0):
                _r = r.get("ref")
                if isinstance(_r, dict) and (_r.get("path") or _r.get("name")):
                    out.append((float(r.get("ts") or 0), r.get("line") or "", _r))
        except Exception:
            pass
        # ⚠️ 两个源会重叠：trail 这里**不排除**最近 10 分钟（否则「刚才」拉不到），
        #    而那段同样躺在实时 buffer 里。⇒ 按 (整秒, path) 去重。
        _seen, _uniq = set(), []
        for ts, line, ref in out:
            _k = (int(ts), str(ref.get("path") or ""), str(ref.get("name") or ""))
            if _k in _seen:
                continue
            _seen.add(_k)
            _uniq.append((ts, line, ref))
        # 🔴 **排序必须给 key** —— `sorted(list_of_tuples)` 在 ts 与 line 都相同时
        #    会接着去比较第三项，而那是个 dict ⇒ **TypeError，当场崩**。
        #    📌 一个"顺手"的默认排序，会在最罕见的输入上变成崩溃点。
        _uniq.sort(key=lambda x: x[0])
        return _uniq

    async def _handle_resolve_ambient_referent(self, args: dict, aid: str,
                                               **_ctx) -> ToolOutcome:
        import datetime as _dt
        _at = str((args or {}).get("at") or "").strip()
        if not _at:
            return ToolOutcome(
                "Missing 'at'. Pass the timestamp printed on the entry you mean, "
                "for example 09:32:14.", failed=True)
        _rows = self._ambient_entries()
        # ⚠️ **可能命中多条**：窗口轮询 2s 一次，同一秒内连续切换会撞在一个时刻上。
        # 🔴 第一版这里 `break` 掉第一条 —— 于是「取那个网页」会静默拿回文件那条，
        #    **而模型不知道自己拿错了**。📌 一个可能指错而不自知的 id，
        #    比没有 id 更糟（同「说清结果可不可信」）。
        # ⇒ 全返回，让它自己挑；挑不出来就该问用户（早先的设计：歧义走 ask_user_choice）。
        _hits = [(ts, line, ref) for ts, line, ref in _rows
                 if _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") == _at]
        _hit = _hits[0] if _hits else None
        if _hit is None:
            # ⚠️ **给出口，不只给一个「没找到」**（模型需要的是
            #    一个出口，不是一个名字）。所以这里连「哪些是可取的」一起说。
            _avail = ", ".join(
                _dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")
                for t, _, _ in _rows[-8:]) or "(none right now)"
            return ToolOutcome(
                f"No resolvable entry at {_at}. Only entries marked with the small "
                f"triangle in [Ambient] can be resolved, and you must pass the "
                f"timestamp printed on that same entry.\n"
                f"Resolvable right now: {_avail}", failed=True)

        if len(_hits) > 1:
            _blocks = []
            for _t, _l, _rf in _hits:
                _blocks.append(
                    f"- {_rf.get('kind')}: {_rf.get('name')}\n"
                    f"  {'target' if not _rf.get('confirmed') else 'location'}: "
                    f"{_rf.get('path') if _rf.get('confirmed') else 'not verified'}\n"
                    f"  context: {_l}")
            return ToolOutcome(
                f"[Ambient {_at}] {len(_hits)} different things share this timestamp "
                f"(the user switched windows within the same second):\n"
                + "\n".join(_blocks)
                + "\nPick the one the user means from the context lines. If it is "
                  "genuinely ambiguous, ask them instead of guessing.")

        _ts, _line, _ref = _hit
        _kind = str(_ref.get("kind") or "")
        _name = str(_ref.get("name") or "")
        _path = str(_ref.get("path") or "")
        _ok = bool(_ref.get("confirmed"))
        _what = {"file": "file path", "url": "URL", "dir": "folder path"}.get(_kind, "target")
        _lines = [f"[Ambient {_at}] {_line}".rstrip(),
                  f"kind: {_kind}",
                  f"name: {_name}"]
        if _ok:
            _lines.append(f"{_what}: {_path}")
            _lines.append("verified: yes - this came from the running application "
                          "itself, you can use it directly.")
        else:
            # 📌 **这一支正是这次实测暴露的问题的解法**：它猜桌面猜中了，
            #    而没有任何人知道那是猜的。⇒ 说清结果可不可信。
            _lines.append(f"{_what}: not verified")
            _lines.append("verified: no - the window title gave the name but the real "
                          "location could not be confirmed. Do NOT invent a path: "
                          "locate it with search_files, or ask the user where it is.")
        return ToolOutcome("\n".join(_lines))

    async def _handle_peek_file(self, args: dict, aid: str, *,
                                event_queue, **_ctx) -> str:
        """试读 —— 花小钱看一眼。"""
        filename = args.get("filename", "")
        if not filename:
            return "peek_file was not executed: `filename` is empty."
        text = await self._read_file_text(filename, False, aid, event_queue)
        if not isinstance(text, str) or len(text) < 1:
            return text

        _key = self._peek_key(filename)
        _first = _key not in self._peeked

        if _first:
            # 🔴 **第一次强制从头 + 固定长度**（用户的悖论）：
            #    「你不先看一部分、没拿到这个文件的任何上下文信息，根本无法
            #      决策接下来要看多少、要看哪里。所以试读就算给模型决策，
            #      它也是在没有任何线索的情况下纯猜。」
            #    📌 **试读是决策的前提，所以它自己不能是决策的产物。**
            # ⚠️ 模型传了 offset/limit 也**明确告诉它被忽略了**，不静默吞掉。
            _sl = _READING.slice_lines(
                text, offset=1, budget_chars=_READING.PEEK_CHARS,
                hard_cap_chars=_READING.PEEK_CHARS)
            self._peeked[_key] = True
            _note = ""
            if args.get("offset") or args.get("limit"):
                _note = ("\n[Your offset/limit were ignored: the first peek of a file "
                         "always starts at the top with a fixed size, because at that "
                         "point there is nothing to base a choice on. Peek again to "
                         "choose freely.]")
            _hint = ""
            if _READING.needs_peek(len(text)):
                _hint = ("\n[This is a map, not the content. Do not answer the user "
                         "from this first peek alone - read the part you need with "
                         "load_full_file, or peek another spot first.]")
            return _READING.render(_sl, peek=True, filename=filename) + _note + _hint

        # 之后的 peek：模型自选位置和长度 —— 它已经有线索了，悖论不再成立。
        # ⚠️ 仍然受 `MAX_READ_CHARS` 硬闸约束（安全兜底对所有读取都成立）。
        _sl = _READING.slice_lines(
            text, offset=args.get("offset"), limit=args.get("limit"),
            budget_chars=_READING.PEEK_CHARS,
            hard_cap_chars=_READING.MAX_READ_CHARS)
        self._wm_add(EntryType.FILE_READ, filename, "peek",
                     detail=f"lines {_sl['start']}-{_sl['end']}", tags=["file", filename])
        return _READING.render(_sl, peek=True, filename=filename)

    async def _handle_load_full_file(self, args: dict, aid: str, *,
                                     event_queue, **_ctx) -> str:
        filename = args.get("filename", "")
        with_images = bool(args.get("with_images", False))
        result_text = await self._read_file_text(filename, with_images, aid, event_queue)

        # ── 大文件必须先试读 ─────────────────────────────────────
        # 🔴🔴 **这里是「拒绝」，不是「悄悄给你一个试读」**。
        #    后者零浪费，但它会让**模型以为自己在精读，拿到的却是试读** ——
        #    那正是我们刚把试读拆成独立工具要消灭的东西。
        # 📌 多花一轮，换「模型永远知道自己在干什么」。
        # ⚠️ 而这一轮不是白花：拒绝信息里带着文件规模和下一步该干什么，
        #    要的「失败信息足够选出下一步」在这里是满足的。
        if (isinstance(result_text, str)
                and _READING.needs_peek(len(result_text))
                and self._peek_key(filename) not in self._peeked):
            # ⚠️ **必须标 `failed=True`** —— 实测 2026-08-26 抓到：
            #    handler 返回字符串时 `ToolOutcome.of()` 默认 `failed=False`，
            #    于是工具卡显示 **「加载文件 ✓」**，而它其实**什么都没加载**。
            # 📌 一次没做成的事显示成成功，比不显示更糟：
            #    用户以为读到了、模型也少了一个「这次不算数」的信号。
            #    （同「说清结果可不可信」那条判据。）
            return ToolOutcome(
                f"Not read yet - this file is large ({len(result_text):,} characters, "
                f"{len(result_text.splitlines()):,} lines).\n"
                f"Call peek_file(filename=...) first to see what is in it, then come "
                f"back and read the part you actually need.\n"
                f"If you already know the exact line you want (for example from "
                f"search_files), say so by passing that offset - having located it "
                f"already counts as knowing the file.\n"
                f"Reading it blindly from the top would cost many turns for nothing.",
                failed=True,
            )

        # ── scratchpad：笔记搭在【本次调用的参数】上 ──────────────
        # ⭐⭐ **载体就是工具参数，不用解析正文**。
        #    文档 原设计是「要求模型在特定 XML 或 Markdown 块中输出」——
        #    那依赖**模型愿意在调工具时同时写正文**，而那不是 schema 能强制的。
        #    📌 一个「靠模型自觉产出」的载体，漏一轮就断一轮，而我们不会知道。
        # ⭐ 而 `notes` 是参数：它在 assistant 消息里天然留着，
        #    **不需要我们再注入回去**，也不会被 `answer_discard` 影响。
        _notes_raw = args.get("notes") or ""
        _notes, _cut = _READING.clamp_notes(_notes_raw)
        _note_back = ""
        if _cut:
            # ⚠️ 截断了**必须说** —— 📌 静默截断笔记 = 模型以为自己记住了，
            #    下一轮发现线索没了，而它不知道为什么。
            _note_back = (f"\n[Your notes were cut to {_READING.MAX_NOTES_CHARS} "
                          f"characters. Notes are for conclusions and coordinates, "
                          f"not for copying text - the copy would defeat the point.]")

        if not isinstance(result_text, str):
            return result_text

        _sl = _READING.slice_lines(
            result_text, offset=args.get("offset"), limit=args.get("limit"),
            budget_chars=_READING.READ_STEP_CHARS,
            hard_cap_chars=_READING.MAX_READ_CHARS)
        result_text = _READING.render(_sl, filename=filename) + _note_back

        self._full_file_hit_this_turn = True
        self._wm_add(
            EntryType.FILE_READ, filename, "full_file_read",
            detail=f"lines {_sl['start']}-{_sl['end']} / {_sl['total_lines']}",
            tags=["file", filename],
        )
        return result_text

    async def _handle_query_local_knowledge(self, args: dict, aid: str, *,
                                            event_queue, **_ctx) -> str:
        kb_query = args.get("query", "")

        def _rag_progress_q(msg: str):
            event_queue.put_nowait({"event": "tool_progress", "action_id": aid, "message": msg})
        async with self._rag_parallel_sem:
            result_text = await asyncio.to_thread(
                rag_engine.query_for_agent, kb_query, 6, _rag_progress_q
            )
        self._rag_hit_this_turn = True
        self._wm_add(
            EntryType.RAG_QUERY, kb_query, "rag_search",
            detail=f"returned {len(result_text):,} chars", tags=["rag", kb_query],
        )
        return result_text

    async def _handle_get_file_path(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> str:
        filename = args.get("filename", "")
        return await asyncio.to_thread(rag_engine.get_file_path_for_agent, filename)

    def _file_read_truncation_note(self, action: str, os_result: dict,
                                   result_text: str) -> str:
        """`file_read` 撞上截断闸时，**告诉它还有另一条路**。

        ⭐ 先纠正一个曾经写错的前提：**模型是知道自己被截断的** ——
           `MemoryManager._compress_tool_results_inplace` 已经附了
           `[...truncated; original content was N chars]`。
           📌 所以缺的从来不是「有没有说截断」，而是**说了截断之后没给下一步**：
              它知道被切了、知道总长，却不知道 `file_read` 无法接着读，
              也不知道 `load_full_file` 能用 offset/limit 读同一个路径。
           （同 longcmd 那条判据：**截断可以接受，不说截断了不行** ——
             这里再进一格：说了截断，还得说得出接下来能干什么。）

        ⚠️ **判据只有一处**：阈值直接读 `MemoryManager.MAX_SINGLE_TOOL_RESULT_CHARS`，
           不在这里另写一个数。📌 两处各写一个数，一旦分叉就会出现
           「说没截其实截了」—— **那比不提示更坏**。取不到就宁可不提示。

        ⚠️ 判的是 `len(result_text)` 而**不是** `len(content)`：真正被量的是
           json 包装之后的那一份，中间隔着 `json.dumps`（换行会变成两个字符）。
           📌 量哪一个由「谁会被截」决定，不由「哪个更好拿」决定。

        ⚠️ 措辞里没有 must / should，末句明写 "your call" —— 见 `_OS_MANIFEST`
           里那段同源留痕：**模型拥有阅读主权**。
        """
        if action != "file_read" or not isinstance(os_result, dict):
            return ""
        if not os_result.get("ok"):
            return ""
        try:
            _limit = int(self.memory.MAX_SINGLE_TOOL_RESULT_CHARS)
        except Exception:
            return ""
        if _limit <= 0 or len(result_text) <= _limit:
            return ""
        _data = os_result.get("data") or {}
        _total = len(str(_data.get("content") or ""))
        _path = str(_data.get("path") or "")
        return (
            f"[Note] file_read returned {_total:,} characters, but only about "
            f"{_limit:,} of them reach you here - the tail is cut off, and file_read "
            f"cannot resume from where it stopped. If you need more of this file, "
            f"load_full_file reads the same path in slices (offset/limit)"
            + (f": {_path}" if _path else "")
            + ". Whether that is worth a turn is your call.\n"
        )
