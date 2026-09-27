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
    t_settings()
    t_usage()
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
