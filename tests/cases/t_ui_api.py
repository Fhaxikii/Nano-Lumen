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


class _Provider:
    def __init__(self, model="m-cheap", vendor="acme", relay=False, configured=True):
        self.target_model, self.vendor, self.is_relay, self.is_configured = model, vendor, relay, configured
        self.reconfigured = 0

    def reconfigure(self):
        self.reconfigured += 1
        return True


def t_settings() -> None:
    print("\n▶ settings")
    import os
    import tempfile
    from core.ui_api import settings as ST
    import core.provider as CP
    env_keys = ("NANO_API_VENDOR", "NANO_API_RELAY_API_KEY", "NANO_API_RELAY_BASE_URL",
                "ANTHROPIC_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "NANO_MODEL")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    prov = _Provider()
    _state.bind(provider_obj=prov)
    models_mod = dict(
        load=lambda: {"acme": {"models": {"m-cheap": {}, "m-big": {}}}, "other": {"models": {"o1": {}}}},
        vendors=lambda: ["acme", "other"],
        vendor_meta=lambda v: {"label": v.upper(), "icon": f"/v/{v}.png", "key_hint": f"{v} key"},
        ROLES=("distiller", "vision"),
        role_label=lambda r: {"distiller": "压缩提炼", "vision": "视觉输入"}[r],
        role_pool=lambda main, r: ["m-cheap", "m-big"] if r == "distiller" else [],
        model_for_role=lambda main, r: "m-cheap" if r == "distiller" else "",
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = pathlib.Path(td)
        cfg = td / "throttle_config.json"
        orig = (ST._app_config_path, ST._ENV_PATH, ST._profile_path, CP.endpoint_models,
                CP.migrate_model_id)
        ST._app_config_path = lambda: cfg
        ST._ENV_PATH = td / ".env"
        ST._profile_path = lambda: td / "user_profile.json"
        CP.endpoint_models = lambda base, key, vendor: None
        CP.migrate_model_id = lambda m: {"m-old": "m-big"}.get(m, m)
        try:
            with _FakeModule("core.models", **models_mod):
                check(ST.model_options() == {"m-cheap": "m-cheap", "m-big": "m-big"},
                      "下拉选项跟着当前厂商")
                CP.endpoint_models = lambda base, key, vendor: {"m-big"}
                check(list(ST.model_options()) == ["m-big"], "端点清单只做过滤")
                CP.endpoint_models = lambda base, key, vendor: None

                # 首次启动：没有配置文件
                os.environ["NANO_MODEL"] = "m-big"
                ST.apply_saved_default_model()
                check(prov.target_model == "m-big" and cfg.exists()
                      and json.loads(cfg.read_text(encoding="utf-8"))["_default_model_vendor"] == "acme",
                      "首次启动用 NANO_MODEL，并记下厂商事实")
                os.environ.pop("NANO_MODEL", None)

                # 收藏：下线映射
                cfg.write_text(json.dumps({"_default_model": "m-old", "_default_model_vendor": "acme",
                                           "legacy_throttle": 1, "_model_policy": "x",
                                           "_role_models": {"vision": "v1"}}), encoding="utf-8")
                prov.target_model = "m-cheap"
                ST.apply_saved_default_model()
                saved = json.loads(cfg.read_text(encoding="utf-8"))
                check(prov.target_model == "m-big" and saved["_default_model"] == "m-big",
                      "收藏的下线型号迁移到新 id 并写回", str(saved))
                check("legacy_throttle" not in saved and "_model_policy" not in saved
                      and saved["_role_models"] == {"vision": "v1"},
                      "写回时清掉废弃键、保留别的元数据")
                # 收藏属于别的厂商 → 作废
                cfg.write_text(json.dumps({"_default_model": "o1", "_default_model_vendor": "other"}),
                               encoding="utf-8")
                prov.target_model = "m-cheap"
                ST.apply_saved_default_model()
                check(prov.target_model == "m-cheap", "收藏属于别的厂商 → 不用它")
                # 收藏不在当前清单里
                cfg.write_text(json.dumps({"_default_model": "ghost", "_default_model_vendor": "acme"}),
                               encoding="utf-8")
                ST.apply_saved_default_model()
                check(prov.target_model == "m-cheap", "收藏不在当前厂商清单里 → 不用它")

                ST.set_default_model("m-big")
                check(ST.default_model() == "m-big", "设为默认")
                ST.set_default_model(None)
                check(ST.default_model() == "", "取消默认")

                cur = ST.set_model("m-big")
                check(cur["id"] == "m-big" and prov.target_model == "m-big" and _serializable(cur),
                      "切换主模型", str(cur))
                prov.target_model = "gone"
                r = ST.ensure_model_in_options()
                check(r["current"] == "m-cheap" and prov.target_model == "m-cheap",
                      "换厂商后当前模型不在清单里 → 换成清单第一个")

                ST.save_ui_prefs(theme_mode="terminal", enhanced_mode=True, ocr_max_pages=80,
                                 token_counter="full")
                p = ST.ui_prefs()
                check(p == {"theme_mode": "terminal", "enhanced_mode": True, "ocr_max_pages": 80,
                            "token_counter": "full"}, "界面偏好存取", str(p))
                d = json.loads(cfg.read_text(encoding="utf-8"))
                d["_theme_mode"], d["_token_counter"] = "neon", "loud"
                cfg.write_text(json.dumps(d), encoding="utf-8")
                p2 = ST.ui_prefs()
                check("theme_mode" not in p2 and p2["token_counter"] == "off",
                      "不认识的主题名 / 档位不交给界面（计数器默认 off）", str(p2))

                roles = ST.roles()
                check(roles[0] == {"role": "distiller", "label": "压缩提炼",
                                   "pool": {"m-cheap": "m-cheap", "m-big": "m-big"}, "current": "m-cheap"}
                      and roles[1]["pool"] == {} and _serializable(roles), "角色模型：池子与当前选择")
                check(ST.set_role_model("vision", "v2") == "视觉输入"
                      and json.loads(cfg.read_text(encoding="utf-8"))["_role_models"]["vision"] == "v2",
                      "记下角色模型的选择")

                ec = ST.env_config()
                check([v["id"] for v in ec["vendors"]] == ["acme", "other"]
                      and ec["vendors"][0]["key_hint"] == "acme key", "环境配置：厂商清单与提示")
                (td / ".env").write_text("KEEP=1\nANTHROPIC_API_KEY=old\nHTTP_PROXY=p\n", encoding="utf-8")
                r = ST.save_env("other", " k-123 ", "", "")
                env_text = (td / ".env").read_text(encoding="utf-8")
                check(r == {"ok": True} and prov.reconfigured == 1, "保存后原地重建 provider", str(r))
                check("KEEP=1" in env_text and "NANO_API_RELAY_API_KEY=k-123" in env_text
                      and "ANTHROPIC_API_KEY" not in env_text and "HTTP_PROXY" not in env_text,
                      "旧密钥 / 清空的代理整行删掉，不是注释掉", env_text)
                check(ST.save_env("x", "  ", "", "")["stage"] == "input", "空 key 不写")
        finally:
            (ST._app_config_path, ST._ENV_PATH, ST._profile_path, CP.endpoint_models,
             CP.migrate_model_id) = orig
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            _state.bind()

        # 权限：只写 permissions，保留文件里别的字段
        from core.os_layer import dsl
        st = td / "os_state.json"
        st.write_text(json.dumps({"auto_mode": True, "permissions": {"allow_dangerous": True}}),
                      encoding="utf-8")
        o_path, o_load = dsl.os_state_path, dsl.load_permissions
        dsl.os_state_path = lambda *a, **k: st
        dsl.load_permissions = lambda *a, **k: json.loads(st.read_text(encoding="utf-8"))["permissions"]
        try:
            check(ST.permissions()["allow_dangerous"] is True and ST.permissions()["allow_window_control"] is False,
                  "权限：六个开关，没写过的为 False")
            r = ST.set_permissions({"allow_window_control": True, "not_a_key": True})
            raw = json.loads(st.read_text(encoding="utf-8"))
            check(r == {"ok": True} and raw["auto_mode"] is True
                  and raw["permissions"] == {"allow_dangerous": True, "allow_window_control": True},
                  "写回时保留 auto_mode，不认识的键不写", str(raw))
        finally:
            dsl.os_state_path, dsl.load_permissions = o_path, o_load

    for fn, bad in (("_load_app_config", ("throttle_config", "migrate_model_id", "target_model")),
                    ("_save_app_config", ("throttle_config", "json.")),
                    ("_toggle_default_model", ("GEMINI_MODEL_MAP", "target_model")),
                    ("_on_model_change", ("target_model", "GEMINI_MODEL_MAP")),
                    ("_build_settings_permissions", ("os_dsl", "cfg_path", "json.")),
                    ("_build_settings_profile", ("user_profile", "data_path", "write_text")),
                    ("_show_env_config_dialog", (".env')", "os.environ", "reconfigure", "core.models")),
                    ("_build_settings_advanced", ("core.models", "CLAUDE_MODEL_MAP", "target_model")),
                    ("_save_role_model", ("throttle_config", "core.models")),
                    ("_token_counter_mode", ("throttle_config",)),
                    ("_build_settings_cost_cap", ("usage_tracker",)),
                    ("start_pipeline_task", ("usage_tracker", "cap_status"))):
        hit = [b for b in bad if b in _code_only(fn)]
        check(not hit, f"app.{fn} 不再直接碰后端 / 配置文件", ", ".join(hit))
    src = S.module_text("app")
    check("def _show_profile_dialog" not in src and "def _show_permissions_dialog" not in src
          and "def _vendor_model_options" not in src, "没人调用的两个旧弹窗与界面侧的模型清单已删")


def t_usage() -> None:
    print("\n▶ usage")
    from core.usage import usage_tracker as UT
    from core.ui_api import usage as U
    names = ("load_config", "save_config", "cap_status", "today_cost", "today_input_output")
    saved = {n: UT.__dict__.get(n) for n in names}
    store = {"enabled": True, "soft_cap_usd": 5.0, "hard_cap_usd": 10.0, "other": 1}
    UT.load_config = lambda: dict(store)
    UT.save_config = lambda c: store.update(c)
    UT.cap_status = lambda: "soft"
    UT.today_cost = lambda: 6.5
    UT.today_input_output = lambda: (1000, 234)
    try:
        b = U.budget()
        check(b == {"enabled": True, "soft_cap": 5.0, "hard_cap": 10.0, "status": "soft", "cost": 6.5},
              "预算：限额设置 + 状态 + 今日花费", str(b))
        U.save_budget(enabled=False, soft_cap=2.0, hard_cap=3.0)
        check(store == {"enabled": False, "soft_cap_usd": 2.0, "hard_cap_usd": 3.0, "other": 1},
              "保存限额（保留别的配置项）", str(store))
        check(U.today_tokens() == 1234 and U.format_tokens(1234) == "1.2K", "今日 token 与格式化",
              U.format_tokens(1234))
    finally:
        for n, v in saved.items():
            if v is None:
                UT.__dict__.pop(n, None)
            else:
                setattr(UT, n, v)
    src = "\n".join(ln for ln in S.module_text("app").splitlines() if not ln.strip().startswith("#"))
    check("usage_tracker." not in src and "_fmt_tokens(" not in src, "app.py 不再直接用 usage_tracker")


def t_mcp() -> None:
    print("\n▶ mcp")
    import asyncio
    log, notes = [], []

    class _Srv:
        def __init__(self, enabled):
            self.enabled = enabled

    class _Mgr:
        servers = {"fetch": _Srv(True), "pw": _Srv(False)}

        def status_snapshot(self):
            return [{"name": "fetch", "status": "connected"},
                    {"name": "part", "status": "idle", "owned_by": "SomeSkill"}]

        async def retry_server(self, n):
            log.append(("retry", n))

        async def remove_server(self, n):
            log.append(("remove", n))
            return True

        def add_server_from_json(self, text):
            return (True, "newsrv") if text.startswith("{") else (False, "bad json")

        async def set_enabled(self, n, on):
            log.append(("set", n, on))

        async def connect_enabled(self):
            log.append(("connect",))

        async def shutdown(self):
            log.append(("shutdown",))

    class _Ag:
        def _note_mcp_change(self, op, server, *, by):
            notes.append((op, server, by))

    _state.bind(agent_obj=_Ag())
    try:
        with _FakeModule("core.mcp_client", get_mcp_manager=lambda: _Mgr()):
            from core.ui_api import mcp as M
            check(M.servers() == [{"name": "fetch", "status": "connected"}], "Skill 自带的零件不列")
            check(M.add_from_json("{...}") == {"ok": True, "msg": "newsrv"}
                  and M.add_from_json("nope")["ok"] is False, "粘 JSON 添加")

            async def run():
                await M.retry("fetch")
                await M.remove("pw")
                return await M.apply_switches({"fetch": False, "pw": False, "ghost": True})
            n = asyncio.run(run())
            check(n == 1 and ("set", "fetch", False) in log and log[-1] == ("connect",),
                  "开关：只改真的变了的，然后连接已启用的", str(log))
            check(notes == [("add", "newsrv", "user"), ("retry", "fetch", "user"),
                            ("delete", "pw", "user"), ("disable", "fetch", "user")],
                  "⭐ 每次用户改动都经 _note_mcp_change(by=user) 记下（对话里的系统记录）", str(notes))
    finally:
        _state.bind()
    code = "\n".join(ln for ln in S.module_text("app").splitlines() if not ln.strip().startswith("#"))
    check("get_mcp_manager" not in code and "mgr." not in code, "app.py 不再直接用 MCP 管理器")

    # 设置页的 MCP 开关推动即生效：每个开关挂 on_value_change → 只提交这一个开关
    page = S.def_text("app", "_build_settings_mcp", owner="WebUI")
    page_code = "\n".join(ln for ln in page.splitlines() if not ln.strip().startswith("#"))
    check("_sw.on_value_change(" in page_code and "_apply_switch(n, bool(e.value))" in page_code,
          "开关推动即调用 _apply_switch（没有保存按钮）")
    check("api_mcp.apply_switches({name: on})" in page_code, "只提交被推动的那一个开关")
    _ap = page_code.split("async def _apply_switch")[1].split("\n        def ")[0]
    check(_ap.index("await api_mcp.apply_switches") < _ap.index("_render_list"),
          "生效完成之后才重画（生效途中重画会把开关画回旧状态）")
    check("with _timer_host:" in _ap, "重画定时器挂在列表外，重画清列表时不会被一起删掉")


def t_proactive() -> None:
    print("\n▶ proactive")
    got = []

    class _Aff:
        mode = "balanced"

        def snapshot(self):
            return {"user_mode": self.mode}

        def set_user_mode(self, m):
            self.mode = m

    aff = _Aff()

    class _Led:
        def explain_readable(self, cap=2):
            return {"items": ["写代码时的下一步"], "total": 3}

        def reset(self):
            got.append("reset")

    class _Eng:
        def feedback(self, sig, iid):
            got.append((sig, iid))

    import core.backend as B
    orig = B.get_intel_engine
    B.get_intel_engine = lambda: _Eng()
    try:
        with _FakeModule("core.proactive.intel.affect", get_affect=lambda: aff), \
                _FakeModule("core.proactive.intel.ledger", get_ledger=lambda: _Led()):
            from core.ui_api import proactive as P
            P.set_effort_mode("proactive")
            check(P.effort_mode() == "proactive", "主动程度存取")
            check(P.learned_preferences() == {"items": ["写代码时的下一步"], "total": 3},
                  "学到的偏好（可解释）")
            P.reset_preferences()
            P.feedback("MUTE_THIS", "iv_1")
            from core.proactive.intel.feedback import Signal
            check(got == ["reset", (Signal.MUTE_THIS, "iv_1")], "恢复默认；反馈按信号名转成信号", str(got))
    finally:
        B.get_intel_engine = orig
    src = "\n".join(ln for ln in S.module_text("app").splitlines() if not ln.strip().startswith("#"))
    check(all(x not in src for x in ("get_affect", "get_ledger", "get_intel_engine", "_ISignal",
                                     "_proactive_push", "_get_activity_buffer")),
          "app.py 不再直接用主动智能的内部模块；没人调用的旧入口已删")


def t_history() -> None:
    print("\n▶ history")
    from memory.manager import ChatMessage, ToolCall, ToolResultBlock
    from core.ui_api import history as H

    u = ChatMessage(role="user", content="看看这个", ui_images=["img_1"], reply_quote="上一句")
    setattr(u, "_conversation_ordinal", 3)
    setattr(u, "_conversation_created_at", 100.0)
    c = ChatMessage(role="tool_calls", content="")
    c.tool_calls = [ToolCall(name="os_execute", tool_use_id="tu_1", args={"command": "dir"}, index=0)]
    r = ChatMessage(role="tool_results", content="")
    r.tool_results = [ToolResultBlock(name="os_execute", tool_use_id="tu_1", content="ok", is_error=False)]
    hidden = ChatMessage(role="assistant", content="[System record] x", visible_to_user=False)

    class _Mem:
        conversation_session_id = "s1"

        def conversation_messages(self):
            return [u, c, r, hidden]
    _state.bind(memory_obj=_Mem())
    try:
        ms = H.messages()
        check(_serializable(ms), "消息可序列化")
        check(ms[0]["role"] == "user" and ms[0]["ordinal"] == 3 and ms[0]["created_at"] == 100.0
              and ms[0]["ui_images"] == ["img_1"] and ms[0]["reply_quote"] == "上一句",
              "带 role / 序号 / 落盘时刻，以及引用与图片", str(ms[0])[:160])
        check(ms[1]["tool_calls"][0]["args"] == {"command": "dir"}
              and ms[2]["tool_results"][0]["content"] == "ok" and ms[3]["visible_to_user"] is False,
              "工具调用与结果、可见性都在")
        import app as A
        m0 = A.WebUI._as_message(ms[0])
        m1 = A.WebUI._as_message(ms[1])
        check(m0.role == "user" and m0._conversation_ordinal == 3 and m0.reply_quote == "上一句"
              and m0.visible_to_user is True and m1.tool_calls[0].tool_use_id == "tu_1"
              and m1.tool_calls[0].args == {"command": "dir"} and m1.render_kind == "",
              "界面把字典包回对象后，重放按属性读得到同样的字段")

        class _DS:
            def __init__(self, store):
                pass

            def active_entries(self, sid):
                return {7: {"level": "L4", "index_entry": "old", "end_ordinal": 9},
                        2: {"level": "L3", "index_entry": "clue", "end_ordinal": 4},
                        5: {"level": "L1", "index_entry": "x"}}
        import core.runtime.kernel as K
        orig = K.get_kernel
        K.get_kernel = lambda: types.SimpleNamespace(store=None)
        try:
            with _FakeModule("core.context.decay_store", DecayStore=_DS, L3="L3", L4="L4"):
                mo = H.moved_out_exchanges()
        finally:
            K.get_kernel = orig
        check(mo == [{"ordinal": 2, "end_ordinal": 4, "index_entry": "clue", "recallable": True},
                     {"ordinal": 7, "end_ordinal": 9, "index_entry": "old", "recallable": False}],
              "移出上下文的交换：老→新，区分还想得起 / 不再自动想起", str(mo))
    finally:
        _state.bind()
    src = "\n".join(ln for ln in S.module_text("app").splitlines() if not ln.strip().startswith("#"))
    check(all(x not in src for x in ("self.memory.", "decay_store", "core.context.budget",
                                     "image_data_uri", "export_all")),
          "app.py 不再直接读对话账本 / 衰减表 / 图库 / 导出 / 上下文预算")


def t_tools() -> None:
    print("\n▶ tools")
    from core.ui_api import tools as T

    class _B:
        def __init__(self, label, body, kind="text"):
            self.label, self.body, self.kind = label, body, kind

    class _Cat:
        def presentation(self, name, args):
            return "检索知识库" if name == "query_local_knowledge" else ""

        def detail(self, name, args, result):
            return [_B("参数", str(args)), _B("结果", getattr(result, "content", ""),
                                               "error" if getattr(result, "is_error", False) else "text")]

    class _Ag:
        def _get_tool_catalog(self):
            return _Cat()

        def agent_run(self, tid):
            return {"instruction": "do", "steps": [("read", {"p": 1}, "txt", False)], "ok": True}

    _state.bind(agent_obj=_Ag())
    try:
        check(T.display_name("query_local_knowledge", {}) == "检索知识库"
              and T.display_name("raw_tool", {}) == "raw_tool", "友好名；算不出用裸名")
        d = T.detail_of("read", {"p": 1}, "boom", True)
        check(d == {"blocks": [{"label": "参数", "body": "{'p': 1}", "kind": "text"},
                               {"label": "结果", "body": "boom", "kind": "error"}], "result_saved": True},
              "直接给出的明细（Subagent 的步骤）", str(d))
        ar = T.agent_run("t1")
        check(ar["run"]["steps"] == [["read", {"p": 1}, "txt", False]] and _serializable(ar),
              "Subagent 监控：步骤可序列化", str(ar)[:120])
        check(T.detail("", "x") == {"blocks": [], "result_saved": False}, "没有 id 时不查账本")
    finally:
        _state.bind()
    src = "\n".join(ln for ln in S.module_text("app").splitlines() if not ln.strip().startswith("#"))
    check(all(x not in src for x in ("_get_tool_catalog", "agent.agent_run(", "_ledger_tool_record",
                                     "_carriers.cancel", "_carriers.running_skill_names")),
          "app.py 不再直接问工具目录 / Subagent 记录 / 载体表")


def t_turn() -> None:
    print("\n▶ turn")
    from core.ui_api import turn as TU
    got = []

    class _Sched:
        parked = {"k": ("user", {})}

        def submit_user_message(self, text, **kw):
            got.append(("submit", text, kw.get("can_continue")))
            got.append(("reply_target", kw.get("reply_target")))
            return "k1", "run"

        def busy(self):
            return self.b

        def discard_parked(self):
            got.append("discard_parked")

        def wake_now(self, sid):
            return "parked"

        def cancel_wait(self, sid):
            return True
    sch = _Sched()
    sch.b = False

    class _Ag:
        _reply_target = None
        memory = types.SimpleNamespace(add_ui_only_record=lambda text, kind: got.append((kind, text)))

        def reset_conversation(self):
            got.append("reset")
            return {"msg": "已重置"}

        def take_reply_target(self):
            return {"iid": "int_1", "q": "x", "kind": "interaction"}

        def request_stop(self, src):
            got.append(("stop", src))

    orig = TU._sched
    TU._sched = lambda: sch
    _state.bind(agent_obj=_Ag())
    try:
        check(TU.submit("你好", can_continue=True) == ("k1", "run") and got[-2] == ("submit", "你好", True),
              "发消息交给调度器")
        TU.submit("引用这条", reply_target={"iid": "int_9", "q": "y", "kind": "interaction"})
        check(got[-1] == ("reply_target", {"iid": "int_9", "q": "y", "kind": "interaction"}),
              "这条消息的引用随 submit 交给调度器", str(got[-1]))
        TU.submit("   ")
        check(got[-2][1].startswith("Please process the uploaded content.") and "reply" in got[-2][1],
              "只有附件没有文字时，后端补一句「请处理上传的内容」（带语言偏好）", got[-2][1])
        check(TU.has_queued() is True and TU.busy() is False, "队列 / 忙的查询")
        TU.set_reply_target({"iid": "int_1", "q": "x", "kind": "interaction"})
        rt = TU.reply_target()
        rt["iid"] = "changed"
        check(TU.reply_target()["iid"] == "int_1", "引用目标返回副本（改了不影响权威）")
        TU.set_reply_target(None)
        check(TU.reply_target() is None
              and (TU.take_reply_target() or {}).get("iid") == "int_1", "清除 / 发送时取走引用")
        TU.request_stop("按钮")
        TU.record_ui_error("402")
        check(("stop", "按钮") in got and ("sys_error", "402") in got, "终止 / 界面错误卡")
        with _FakeModule("core.runtime.inbox", discard_all_pending=lambda why: 2), \
                _FakeModule("core.context.meter", forget_conversation_size=lambda: got.append("forget"),
                            get_meter=lambda: types.SimpleNamespace(_anchor=1)), \
                _FakeModule("core.rag", clear_temp_knowledge=lambda: got.append("clear_temp")) as _fr:
            # `from core import rag` 在真模块已导入时读的是包属性：一起顶替，免得调到真的
            import core
            _real_rag = getattr(core, "rag", None)
            core.rag = _fr
            try:
                sch.b = True
                check(TU.reset_conversation()["busy"] is True and "reset" not in got, "有一轮在跑时不重置")
                sch.b = False
                r = TU.reset_conversation()
            finally:
                if _real_rag is not None:
                    core.rag = _real_rag
        check(r == {"busy": False, "msg": "已重置", "discarded": 2}
              and all(x in got for x in ("discard_parked", "reset", "forget", "clear_temp")),
              "重置：丢排队、开新会话、作废上下文厚度、清临时附件，一次做完", str(r))
        check(TU.wake_now("w") == "parked" and TU.cancel_wait("w") is True, "立即执行 / 取消等待")
    finally:
        TU._sched = orig
        _state.bind()


def t_turn_finalize() -> None:
    print("\n▶ 一轮的 final_result：笔记与跨会话摘要在后端")
    import asyncio
    from core import session as SS
    rec = []

    class _Ag:
        _notes_written_this_turn = [{"note_id": 7, "display_text": "喜欢深色"}]

        def record_session_end(self, q, c):
            rec.append((q, c))

    ag = _Ag()

    async def src():
        yield {"event": "tool_start"}
        yield {"event": "final_result", "content": "好的"}

    async def run():
        return [ev async for ev in SS._with_turn_usage(src(), ag, "记住我喜欢深色")]
    out = asyncio.run(run())
    check(out[1].get("notes_written") == [{"note_id": 7, "display_text": "喜欢深色"}]
          and ag._notes_written_this_turn == [], "本轮笔记附在 final_result 上，并清空后端那张列表")
    check(rec == [("记住我喜欢深色", "好的")], "跨会话摘要用这段回应期最后一条用户消息写", str(rec))
    check("notes_written" not in out[0], "别的事件不动")
    app = S.module_text("app")
    check('step.get("notes_written")' in app and "record_session_end" not in app
          and "_notes_written_this_turn" not in app, "界面只弹气泡、加卡片")


def t_boot() -> None:
    print("\n▶ boot")
    import core.runtime as RT
    from core.runtime import carriers as CAR, events as EV
    from core import session as SS
    from tests._patch import patch_global
    built = {}

    class _Mem:
        def __init__(self, max_turns, conversation_repository):
            built["mem"] = (max_turns, conversation_repository)

    class _Orc:
        def __init__(self, p, reg, mem):
            built["orc"] = (p, reg, mem)
            self._native_window = None

    CAR._reset_for_tests()
    SS.reset_for_tests()
    q = EV.subscribe()
    restore = patch_global("core.runtime", "get_kernel", lambda: types.SimpleNamespace(store="STORE"))
    try:
        with _FakeModule("core.orchestrator", Orchestrator=_Orc), \
                _FakeModule("core.provider", get_provider=lambda: "PROV"), \
                _FakeModule("core.registry", registry="REG"), \
                _FakeModule("core.runtime.conversation", ConversationRepository=lambda store: ("REPO", store)), \
                _FakeModule("memory.manager", MemoryManager=_Mem):
            from core.ui_api import boot as B
            presenter = object()
            win = lambda: "WIN"  # noqa: E731
            B.create(presenter, native_window=win)
        ag = _state.agent
        check(built["mem"] == (10, ("REPO", "STORE")) and built["orc"][0] == "PROV"
              and _state.provider == "PROV" and ag._native_window is win,
              "创建记忆（绑对话账本）、provider、orchestrator，登记给接口；取窗口的办法交给后端")
        check(SS.get_scheduler().presenter is presenter and SS.get_scheduler().agent is ag,
              "会话调度器以界面为呈现方")
        CAR._notify({"event": "carrier_promoted", "skill_name": "S", "rt_task_id": "t1"})
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        check((None, {"event": "carrier_changed", "change": "carrier_promoted", "skill_name": "S",
                      "rt_task_id": "t1"}) in evs, "载体状态变化以轮外事件告诉界面", str(evs)[:160])
    finally:
        restore()
        CAR._reset_for_tests()
        SS.reset_for_tests()
        _state.bind()
        try:
            EV.unsubscribe(q)
        except Exception:
            pass
    t = S.module_text("core.orchestrator")
    check('publish({"event": "decay_applied"}, None)' in t and "_on_decay_applied" not in t,
          "衰减之后发轮外事件 decay_applied（不再回调界面挂上的函数）")
    check('_kind == "decay_applied"' in S.module_text("app"), "界面收到 decay_applied 重画聊天区")


def t_guard() -> None:
    print("\n▶ 守护：app.py 只从 core.ui_api 拿后端能力")
    tree = ast.parse(S.module_text("app"))
    bad_imports, bad_attrs = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in ("core", "memory"):
            # 白名单：`core.paths`（项目根这类纯常量，界面定位 assets / static 用）
            if not (n.module or "").startswith("core.ui_api") and n.module != "core.paths":
                bad_imports.append(f"{n.lineno}:{n.module}")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in ("core", "memory"):
                    bad_imports.append(f"{n.lineno}:{a.name}")
        elif (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
              and n.value.id in ("self", "gui") and n.attr in ("agent", "provider", "memory")):
            bad_attrs.append(f"{n.lineno}:{n.value.id}.{n.attr}")
    check(not bad_imports, "app.py 没有 core.ui_api 以外的 core / memory 导入", ", ".join(bad_imports[:8]))
    check(not bad_attrs, "app.py 不持有 orchestrator / provider / memory", ", ".join(bad_attrs[:8]))
    for mod in ("boot", "turn", "skills", "knowledge", "notes", "settings", "usage", "mcp",
                "proactive", "history", "tools", "state"):
        src = S.module_text(f"core.ui_api.{mod}")
        heavy = [ln for ln in src.splitlines()
                 if ln.startswith(("from core.", "import core.", "from memory", "import memory"))
                 and not ln.startswith(("from core.ui_api", "from core.runtime.events import TURN_END"))]
        check(not heavy, f"ui_api.{mod} 顶层不导入重模块（窗口子进程也会 import 界面）", "; ".join(heavy))


def t_backend_bound() -> None:
    print("\n▶ 接口背后的后端对象在界面启动时登记")
    init = S.def_text("app", "__init__", owner="WebUI")
    check("api_boot.create(self" in init
          and "_state.bind(agent_obj=agent, provider_obj=provider, memory_obj=memory)"
          in S.def_text("core.ui_api.boot", "create"),
          "界面启动时由 ui_api.boot 创建后端对象并登记（否则接口在生产里全部报「后端还没启动」）")


def main() -> int:
    t_backend_bound()
    t_skills()
    t_knowledge()
    t_notes()
    t_settings()
    t_usage()
    t_mcp()
    t_proactive()
    t_history()
    t_tools()
    t_turn()
    t_turn_finalize()
    t_boot()
    t_guard()
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
