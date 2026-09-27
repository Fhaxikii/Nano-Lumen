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


class _FakeModule:
    """把 `sys.modules[name]` 换成替身模块，退出时还原。"""

    def __init__(self, name, **attrs):
        self.name = name
        self.mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(self.mod, k, v)

    def __enter__(self):
        self.saved = sys.modules.get(self.name)
        sys.modules[self.name] = self.mod
        return self.mod

    def __exit__(self, *a):
        if self.saved is not None:
            sys.modules[self.name] = self.saved
        else:
            sys.modules.pop(self.name, None)


def t_knowledge() -> None:
    print("\n▶ knowledge")
    import tempfile
    from core.ui_api import knowledge as KB
    calls = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        kb = pathlib.Path(td) / "kb"
        tmp_up = pathlib.Path(td) / "up"

        class _Coll:
            def get(self, where=None, include=None):
                return {"metadatas": [{"chunk_index": 1}, {"chunk_type": "schema"}, {"chunk_index": 0}],
                        "documents": ["second", "SCHEMA", "first"]}

        rag_attrs = dict(
            get_health_report=lambda: {"summary": {"total": 1},
                                       "files": [{"filename": "a.txt", "path": str(kb / "a.txt")},
                                                 {"filename": "gone.txt", "path": str(kb / "gone.txt")}]},
            index_single_file=lambda path, cfg: calls.append(("index", pathlib.Path(path).name, cfg))
            or {"indexed": 0, "skipped": 0, "errors": [{"error": "bad pdf"}]},
            delete_file=lambda fn: calls.append(("delete", fn)),
            _get_collection=lambda: _Coll(),
            _temp_uploads_dir=lambda: tmp_up,
            register_temp_file=lambda fn, p: calls.append(("register", fn, pathlib.Path(p).read_bytes())),
            remove_temp_file=lambda fn: True,
            clear_temp_knowledge=lambda: calls.append(("clear",)),
        )
        orig_dir = KB._kb_dir
        KB._kb_dir = lambda: kb
        agent = _Agent()
        _state.bind(agent_obj=agent)
        import core  # noqa: F401
        saved_rag_attr = getattr(core, "rag", None)
        try:
            with _FakeModule("core.rag", **rag_attrs) as fake:
                core.rag = fake
                r1 = KB.store_file("a.txt", b"hello")
                r2 = KB.store_file("a.txt", b"again")
                r3 = KB.store_file("x.exe", b"MZ")
                r4 = KB.store_file("e.md", b"")
                check(r1 == {"ok": True} and (kb / "a.txt").read_bytes() == b"hello",
                      "写进知识库目录", str(r1))
                check(r2["reason"] == "exists" and (kb / "a.txt").read_bytes() == b"hello",
                      "同名不覆盖（原文件不动）")
                check(r3 == {"ok": False, "reason": "unsupported", "suffix": ".exe"}
                      and r4["reason"] == "empty", "不支持的格式 / 空文件不写，给原因码")
                lst = KB.list_files()
                check(lst["files"][0]["mtime"] > 0 and lst["files"][1]["mtime"] == 0.0
                      and _serializable(lst), "列表带文件修改时间（读不到为 0）", str(lst["files"])[:120])
                st = KB.index_file("a.txt", True, 50)
                check(st == {"indexed": 0, "skipped": 0, "error": "bad pdf"}
                      and calls[-1] == ("index", "a.txt", {"enhanced_mode": True, "max_ocr_pages": 50}),
                      "建索引：结果只给计数与第一条错误", str(st))
                check(KB.file_text("a.txt") == "first\n\nsecond", "看内容：按块顺序、去掉结构摘要块")
                KB.delete_file("a.txt")
                check(("delete", "a.txt") in calls and not (kb / "a.txt").exists()
                      and "deleted knowledge-base file \"a.txt\" from the UI sidebar"
                      in agent.memory.notes[-1][1], "删除：索引、磁盘、对话里的系统记录三件一起")
                KB.add_temp_file("t.png", b"PNG")
                check(calls[-1] == ("register", "t.png", b"PNG"), "临时附件：存盘并登记当前会话")
                KB.clear_temp_files()
                check(calls[-1] == ("clear",), "清空临时附件")
        finally:
            KB._kb_dir = orig_dir
            if saved_rag_attr is not None:
                core.rag = saved_rag_attr
            _state.bind()
    for fn in ("_refresh_kb_file_list", "_render_kb_file_card", "_handle_kb_upload",
               "_show_kb_file_content_dialog", "_delete_kb_file", "_remove_temp_file"):
        code = _code_only(fn)
        hit = [b for b in ("rag_engine", "KNOWLEDGE_DIR", "getmtime", "with open(", "add_system_note")
               if b in code]
        check(not hit, f"app.{fn} 不再直接碰后端 / 知识库目录", ", ".join(hit))


def t_notes() -> None:
    print("\n▶ notes")
    log = []

    class _Store:
        def get_pending_notes(self):
            return [{"id": 1, "detail": "d", "ts": "t"}]

        def get_all_confirmed_notes(self):
            return [{"id": 2, "summary_user": "s"}]

        def confirm(self, i):
            log.append(("confirm", i))
            return True

        def confirm_all_pending(self):
            log.append(("all",))

        def delete_by_id(self, i):
            log.append(("delete", i))

        def soft_delete_by_id(self, i):
            log.append(("soft", i))

    with _FakeModule("core.memory_store", get_memory_store=lambda: _Store()):
        from core.ui_api import notes as N
        check(N.pending() == [{"id": 1, "detail": "d", "ts": "t"}] and N.confirmed()[0]["id"] == 2,
              "待确认 / 已记住的列表")
        N.confirm(1)
        N.confirm_all()
        N.delete_pending(3)
        N.forget(4)
        check(log == [("confirm", 1), ("all",), ("delete", 3), ("soft", 4)],
              "确认 / 全部确认 / 删待确认 / 删已记住（软删除）", str(log))
    code = S.module_text("app")
    check("get_memory_store" not in code, "app.py 不再直接用 memory_store")


def t_backend_bound() -> None:
    print("\n▶ 接口背后的后端对象在界面启动时登记")
    init = S.def_text("app", "__init__", owner="WebUI")
    check("_api_state.bind(agent_obj=self.agent, provider_obj=self.provider, memory_obj=self.memory)"
          in init, "WebUI 建好 orchestrator 后立刻登记给 ui_api（否则接口在生产里全部报「后端还没启动」）")


def main() -> int:
    t_backend_bound()
    t_skills()
    t_knowledge()
    t_notes()
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
