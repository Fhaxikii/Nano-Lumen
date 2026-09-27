# -*- coding: utf-8 -*-
"""界面访问后端的唯一入口 `core/ui_api/`（S6-6b 裁决 75）。

每一类一节：接口的行为（返回可序列化的数据、用户动作带来的后端连锁在接口里一次做完），
以及 app.py 这一类不再直接碰后端。

测试不碰真实 data/：后端对象全部用替身（`core.registry` 用替身模块顶替）。

用法：
  py -3.10 tests\\cases\\t_ui_api.py
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core.ui_api import _state  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _serializable(x) -> bool:
    try:
        return json.loads(json.dumps(x)) == x
    except Exception:
        return False


class _Memory:
    def __init__(self):
        self.notes = []

    def add_system_note(self, role, text):
        self.notes.append((role, text))


class _Agent:
    def __init__(self):
        self.memory = _Memory()
        self.pending = {"a.py": {"filename": "a.py", "code": "x=1", "description": "d",
                                 "valid": True, "errors": [], "spec": object()}}
        self.applied, self.cancelled = [], []

    def _get_pending_skill(self, fn=None):
        return self.pending.get(fn or "a.py")

    def apply_pending_skill(self, fn=None):
        self.applied.append((fn, self.pending[fn]["code"]))
        return {"ok": True, "msg": "已部署 a\n\n细节"}

    def cancel_pending_skill(self, fn=None):
        self.cancelled.append(fn)
        return {"ok": True, "msg": "已丢弃 a"}


def _code_only(fn_name: str) -> str:
    """app.py 里某个方法的代码（去掉 docstring 与注释）。"""
    node = ast.parse(S.def_text("app", fn_name, owner="WebUI").strip())
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and body
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            n.body = body[1:] or [ast.Pass()]
    return ast.unparse(node)


def t_skills() -> None:
    print("\n▶ skills")
    calls = []

    class _Reg:
        skills = {"Alpha": types.SimpleNamespace(
            get_manifest=lambda: {"description": "does alpha"},
            get_spec=lambda: types.SimpleNamespace(purpose="p", not_responsible_for=("x", "y")))}

        def is_official_skill(self, n):
            return n == "Alpha"

        def is_os_skill(self, n):
            return False

        def list_disabled_skills(self):
            return ["Beta"]

        def get_skill_source(self, n, include_disabled=False):
            return {"code": "print(1)"} if n in ("Alpha", "Beta") else None

        def disable_skill(self, n):
            calls.append(("disable", n))
            return {"ok": n == "Alpha", "msg": "禁用了" if n == "Alpha" else "没有这个"}

        def enable_skill(self, n):
            return {"ok": True, "msg": "启用了"}

        def delete_skill_file(self, n):
            return {"ok": True, "msg": ""}

    fake_mod = types.ModuleType("core.registry")
    fake_mod.registry = _Reg()
    saved = sys.modules.get("core.registry")
    sys.modules["core.registry"] = fake_mod
    import core.skill_watch as SW
    sup = []
    orig_sup = SW.suppress
    SW.suppress = lambda sec: sup.append(sec)
    agent = _Agent()
    _state.bind(agent_obj=agent)
    try:
        from core.ui_api import skills as K
        ls = K.list_skills()
        check(ls == {"enabled": [{"name": "Alpha", "official": True, "os": False}], "disabled": ["Beta"]}
              and _serializable(ls), "列表：启用的带标记、禁用的另列，可序列化", str(ls))
        info = K.skill_info("Alpha")
        check(info == {"description": "does alpha", "purpose": "p", "not_responsible_for": ["x", "y"]},
              "详情：描述 / 用途 / 不负责的范围", str(info))
        check(K.skill_info("Nope") is None and K.skill_source("Beta") == "print(1)"
              and K.skill_source("Nope") is None, "没有的 Skill 返回 None；源码含已禁用的")

        r = K.disable("Alpha")
        check(r == {"ok": True, "msg": "禁用了"}, "禁用成功", str(r))
        check(len(agent.memory.notes) == 1 and agent.memory.notes[0][0] == "assistant"
              and "the user disabled Skill \"Alpha\" from the UI sidebar" in agent.memory.notes[0][1],
              "禁用成功 → 对话里记一条系统记录，写明是界面上用户做的", str(agent.memory.notes))
        r2 = K.disable("Ghost")
        check(not r2["ok"] and len(agent.memory.notes) == 1, "禁用失败不记")
        K.enable("Beta")
        K.delete("Alpha")
        check(len(agent.memory.notes) == 3 and "enabled Skill \"Beta\"" in agent.memory.notes[1][1]
              and "deleted Skill \"Alpha\"" in agent.memory.notes[2][1]
              and "has been deleted" in agent.memory.notes[2][1],
              "启用 / 删除同样记录（删除没有消息时用默认说法）")

        d = K.pending_draft("a.py")
        check(d == {"filename": "a.py", "code": "x=1", "description": "d", "valid": True, "errors": []}
              and _serializable(d), "待审草稿：只给显示要用的字段、可序列化", str(d))
        d["code"] = "mutated"
        check(agent.pending["a.py"]["code"] == "x=1", "改返回值不影响后端那份")
        check(K.update_draft_code("a.py", "x=2") and agent.pending["a.py"]["code"] == "x=2"
              and not K.update_draft_code("zz.py", "x"), "写回编辑器里的代码只写这一份")
        v = K.validate_code("def broken(:")
        check(set(v) == {"ok", "errors"} and v["ok"] is False and v["errors"], "校验结果形状", str(v)[:80])
        r3 = K.apply_draft("a.py", "x=3")
        check(r3["ok"] and agent.applied == [("a.py", "x=3")] and sup == [2.5],
              "部署：先写回代码、暂停目录监听，再部署", str(agent.applied))
        check("[System record: a pending Skill was deployed from the UI.] 已部署 a" == agent.memory.notes[-1][1],
              "部署成功记一条（只取第一段）", agent.memory.notes[-1][1])
        K.discard_draft("a.py")
        check(agent.cancelled == ["a.py"] and "discarded the pending Skill" in agent.memory.notes[-1][1],
              "丢弃：取消并记一条")
    finally:
        SW.suppress = orig_sup
        if saved is not None:
            sys.modules["core.registry"] = saved
        else:
            sys.modules.pop("core.registry", None)
        _state.bind()

    for fn in ("refresh_skill_list", "_show_skill_info_dialog", "_show_skill_source_dialog",
               "_disable_skill", "_delete_skill", "_enable_skill", "_reopen_pending_audit",
               "_validate_skill_code", "_on_discard_skill", "_on_apply_skill"):
        code = _code_only(fn)
        hit = [b for b in ("registry.", "_get_pending_skill", "apply_pending_skill",
                           "cancel_pending_skill", "add_system_note", "skill_check", "skill_watch")
               if b in code]
        check(not hit, f"app.{fn} 不再直接碰后端", ", ".join(hit))


def main() -> int:
    t_skills()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
