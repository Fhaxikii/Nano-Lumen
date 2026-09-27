# -*- coding: utf-8 -*-
"""工具在界面上的样子：友好名、展开后的明细块；Subagent 监控；后台任务的终止；本地 Skill 是否还在跑。

明细按 `tool_use_id` 从落盘账本取（参数也从账本取，不从实时事件里穿过来：两个来源只在
「两次想法相同」时才一致）。账本索引按会话缓存，查不到时重建一次（刚跑完的那条比缓存新）。
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from core.ui_api import _state

_ledger: dict = {"sid": None, "index": {}}


def _catalog():
    return _state.require_agent()._get_tool_catalog()


def display_name(name: str, args: Optional[dict] = None) -> str:
    """友好名（`检索知识库` 等），由工具目录按 (名字, 参数) 算；算不出用裸名。"""
    try:
        return _catalog().presentation(name, args or {}) or name
    except Exception:
        return name


def _blocks(name: str, args: dict, result: Any) -> list[dict]:
    return [{"label": b.label, "body": b.body, "kind": b.kind}
            for b in (_catalog().detail(name, args or {}, result) or [])]


def _ledger_record(tool_use_id: str):
    mem = _state.memory
    repo = getattr(mem, "conversation_repository", None)
    sid = getattr(mem, "conversation_session_id", None)
    if repo is None or not sid:
        return "", {}, None

    def _pick(idx):
        c, r = idx.get(("call", tool_use_id)), idx.get(("res", tool_use_id))
        return (getattr(c, "name", "") or getattr(r, "name", "") or ""), (getattr(c, "args", None) or {}), r

    if _ledger["sid"] == sid and ("res", tool_use_id) in _ledger["index"]:
        return _pick(_ledger["index"])
    idx = {}
    for m in repo.load_messages(sid):
        for tc in (getattr(m, "tool_calls", None) or []):
            if getattr(tc, "tool_use_id", ""):
                idx[("call", tc.tool_use_id)] = tc
        for tr in (getattr(m, "tool_results", None) or []):
            if getattr(tr, "tool_use_id", ""):
                idx[("res", tr.tool_use_id)] = tr
        # 旧的单工具形状（role="tool_call" / "tool"）
        if m.role == "tool_call" and getattr(m, "tool_use_id", ""):
            idx[("call", m.tool_use_id)] = m
        elif m.role == "tool" and getattr(m, "tool_use_id", ""):
            idx[("res", m.tool_use_id)] = m
    _ledger.update(sid=sid, index=idx)
    return _pick(idx)


def detail(tool_use_id: str, fallback_name: str = "") -> dict:
    """聊天区 / 重放里一行工具的明细：`{"blocks": [{"label", "body", "kind"}], "result_saved"}`。
    `result_saved` 为假 = 结果还没落盘（这一步可能还在跑）。"""
    if not tool_use_id:
        return {"blocks": [], "result_saved": False}
    try:
        name, args, res = _ledger_record(tool_use_id)
    except Exception as e:
        # warning：这里静默的话，工具卡展开只会显示一句读起来很正常的「没有可展示的内容」
        logger.warning(f"[U8] 从账本取工具记录失败 {tool_use_id}: {e}")
        name, args, res = "", {}, None
    return {"blocks": _blocks(name or fallback_name, args, res), "result_saved": res is not None}


def detail_of(name: str, args: dict, result_text: str, is_error: bool) -> dict:
    """直接给出 (名字, 参数, 结果) 的明细（Subagent 的步骤不在对话账本里）。"""
    class _R:
        content = result_text
    _R.is_error = bool(is_error)
    return {"blocks": _blocks(name, args, _R()), "result_saved": True}


def agent_run(task_id: str) -> dict:
    """Subagent 监控：`{"run": {instruction, steps: [[name, args, text, is_error]], started, ended, ok, report, ...},
    "tokens": 它花掉的 token}`；没有这个任务时 `run` 为空字典。"""
    try:
        run = dict(_state.require_agent().agent_run(task_id) or {})
    except Exception:
        run = {}
    if "steps" in run:
        run["steps"] = [list(s) for s in run.get("steps") or []]
    try:
        from core.usage import usage_tracker
        tok = int(usage_tracker.agent_tokens(task_id))
    except Exception:
        tok = 0
    return {"run": run, "tokens": tok}


def cancel_background(task_id: str) -> bool:
    """抽屉里的 ■：终止那个后台任务（不影响对话）。返回是否真的停了一个在跑的。"""
    from core.runtime import carriers
    return bool(carriers.cancel(task_id))


def running_skill_names() -> list[str]:
    from core.runtime import carriers
    return sorted(n for n in carriers.running_skill_names() if n)


def skill_running(name: str) -> bool:
    from core.runtime import carriers
    return bool(carriers.skill_running(name))
