# -*- coding: utf-8 -*-
"""设置：主模型与收藏、角色模型、环境配置（API Key / 中转 / 代理）、OS 权限与 Auto、
个人资料、语言、界面偏好。

`data/throttle_config.json` 的读改写只在这里（`_update_app_config`）：界面偏好（主题、入库增强、
OCR 页数、token 计数器档位）、收藏模型与它所属的厂商、中转模式、角色模型都存在这个文件里。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from core.ui_api import _state

_THEMES = ("terminal", "aurora")
_TOKEN_COUNTER_MODES = ("off", "tokens", "full")
_OS_PERMISSION_KEYS = ("allow_workspace_write", "allow_window_control", "allow_mouse_keyboard",
                       "allow_system_settings", "allow_registry_write", "allow_dangerous")


def _provider():
    if _state.provider is None:
        raise RuntimeError("后端还没启动（ui_api.boot 未执行）")
    return _state.provider


def _app_config_path() -> Path:
    from core.paths import data_path
    return data_path("throttle_config.json")


def _read_app_config() -> dict:
    p = _app_config_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:
        logger.warning(f"[Config] 读取配置失败，使用默认值: {e}")
        return {}


def _update_app_config(change: Callable[[dict], None]) -> bool:
    """读 → 改 → 写。顺带清掉已废弃的键（节流 / 策略引擎留下的非元数据条目）。"""
    p = _app_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        for obsolete in ("_model_policy", "_policy_engine_enabled"):
            data.pop(obsolete, None)
        for k in [k for k in data if not k.startswith("_")]:
            del data[k]
        change(data)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"[Config] 保存配置失败: {e}")
        return False


def _vendor() -> str:
    return (getattr(_provider(), "vendor", "") or os.environ.get("NANO_API_VENDOR")
            or "anthropic").lower()


# ── 主模型 ────────────────────────────────────────────────────────────────
def model_name(model_id: str) -> str:
    """展示名（内置表里没有就用 id 本身）。"""
    from core.provider import CLAUDE_MODEL_MAP
    return (CLAUDE_MODEL_MAP.get(model_id) or {}).get("name") or model_id


def current_model() -> dict:
    """`{"id", "name", "vendor", "relay", "configured"}`。"""
    p = _provider()
    mid = getattr(p, "target_model", "") or ""
    return {"id": mid, "name": model_name(mid), "vendor": _vendor(),
            "relay": bool(getattr(p, "is_relay", False)),
            "configured": bool(getattr(p, "is_configured", False))}


def model_options() -> dict:
    """主模型下拉的选项 `{id: name}`，跟着当前厂商走。

    ① 当前厂商的模型（厂商表的顺序 = 价格升序，手排）
    ② 与端点 Models API 拉到的清单取交集：端点没有的不列。只做过滤，不拿端点的全量当权威
       （某中转返回 71 个模型，多数没有价格 / 窗口 / 角色能力的声明；支持与否由厂商表说了算）
    ③ 两者都拿不到 → 回落内置表（保证永远有得选）
    拉不到端点清单时不过滤（断网不该让下拉变空）。
    """
    from core.provider import CLAUDE_MODELS
    try:
        from core.models import load as _mload
        vendor = _vendor()
        models = list(((_mload().get(vendor) or {}).get("models") or {}).keys())
        if not models:
            raise ValueError("厂商表里没有该厂商的模型")
        try:
            from core.provider import endpoint_models
            live = endpoint_models((os.environ.get("NANO_API_RELAY_BASE_URL") or "").strip(),
                                   (os.environ.get("NANO_API_RELAY_API_KEY") or "").strip(), vendor)
            if live:
                filtered = [m for m in models if m in live]
                if filtered:
                    models = filtered
        except Exception:
            pass
        names = {m["id"]: m["name"] for m in (CLAUDE_MODELS or [])}
        return {m: names.get(m, m) for m in models}
    except Exception as e:
        logger.debug(f"[Model] 按厂商取模型清单失败，回落内置表: {e}")
        return {m["id"]: m["name"] for m in (CLAUDE_MODELS or [])}


def set_model(model_id: str) -> dict:
    """切换主模型（立即生效）。返回 `current_model()`。"""
    _provider().target_model = model_id
    logger.info(f"[Model] 切换至: {model_id}")
    return current_model()


def ensure_model_in_options() -> dict:
    """换厂商后当前模型可能不在新厂商的清单里 → 换成清单第一个。
    返回 `{"options", "current"}`（下拉框是常驻控件，界面据此重设选项与当前值）。"""
    opts = model_options()
    p = _provider()
    cur = getattr(p, "target_model", "") or ""
    if cur not in opts:
        cur = next(iter(opts), "")
        p.target_model = cur
    return {"options": opts, "current": cur}


def default_model() -> str:
    """收藏的启动默认模型（先过下线映射）；没有为空串。"""
    m = str(_read_app_config().get("_default_model") or "")
    if not m:
        return ""
    try:
        from core.provider import migrate_model_id
        return migrate_model_id(m)
    except Exception:
        return m


def set_default_model(model_id: Optional[str]) -> None:
    """设 / 取消（None）启动默认模型。收藏带上所属厂商：换厂商后它就不再适用。"""
    def _c(d: dict) -> None:
        if model_id is None:
            d.pop("_default_model", None)
        else:
            d["_default_model"] = model_id
        _stamp_provider_facts(d)
    _update_app_config(_c)


def _stamp_provider_facts(d: dict) -> None:
    p = _provider()
    d["_last_relay_mode"] = bool(getattr(p, "is_relay", False))
    d["_default_model_vendor"] = _vendor()


def apply_saved_default_model() -> None:
    """启动时：有收藏模型就用它，否则沿用 provider 的默认；首次启动（没有配置文件）
    用 `NANO_MODEL` 或内置表第一个。

    收藏先过下线映射（旧 id 静默落空的话，用户只看到「收藏的模型变回别的了」却查不出原因）；
    收藏属于别的厂商则作废（它会覆盖 provider 刚设好的主模型，于是监控卡、进阶配置、
    上下文窗口全线显示上一个厂商的型号）；最后按当前厂商的清单校验。
    """
    p = _provider()
    if not _app_config_path().exists():
        from core.provider import CLAUDE_MODELS
        p.target_model = (os.getenv("NANO_MODEL") or CLAUDE_MODELS[0]["id"]).strip()
        logger.debug(f"[Model] 首次启动，初始模型: {p.target_model}")
        _update_app_config(_stamp_provider_facts)
        return
    saved = _read_app_config()
    dm = str(saved.get("_default_model") or "")
    if dm:
        try:
            from core.provider import migrate_model_id
            new = migrate_model_id(dm)
            if new != dm:
                dm = new
                set_default_model(new)
        except Exception:
            pass
    saved_vendor = str(saved.get("_default_model_vendor") or "")
    if dm and saved_vendor and saved_vendor != _vendor():
        logger.info(f"[Model] 收藏模型属于 {saved_vendor}，当前是 {_vendor()} —— 作废，"
                    f"沿用 {p.target_model}")
        dm = ""
    if dm and dm in model_options():
        p.target_model = dm
    else:
        logger.debug(f"[Model] 无收藏模型，沿用默认: {p.target_model}")
    _update_app_config(_stamp_provider_facts)


# ── 界面偏好 ──────────────────────────────────────────────────────────────
def ui_prefs() -> dict:
    """`{"theme_mode", "enhanced_mode", "ocr_max_pages", "token_counter"}`；没存过的项不出现。

    主题名与计数器档位按白名单校验（配置是用户可编辑的文本，不认识的值不交给界面）。
    token 计数器默认 `off`：token 数对新用户没有参照系，只会觉得贵。
    """
    s = _read_app_config()
    out: dict[str, Any] = {}
    tm = s.get("_theme_mode")
    if tm in _THEMES:
        out["theme_mode"] = tm
    elif tm:
        logger.warning(f"[Theme] 配置里的主题名不认识，回退默认: {tm!r}")
    if "_enhanced_mode" in s:
        out["enhanced_mode"] = bool(s["_enhanced_mode"])
    if "_ocr_max_pages" in s:
        try:
            out["ocr_max_pages"] = int(s["_ocr_max_pages"])
        except Exception:
            pass
    tc = str(s.get("_token_counter") or "")
    out["token_counter"] = tc if tc in _TOKEN_COUNTER_MODES else "off"
    return out


def save_ui_prefs(*, theme_mode: Optional[str] = None, enhanced_mode: Optional[bool] = None,
                  ocr_max_pages: Optional[int] = None, token_counter: Optional[str] = None) -> bool:
    def _c(d: dict) -> None:
        if theme_mode is not None:
            d["_theme_mode"] = theme_mode
        if enhanced_mode is not None:
            d["_enhanced_mode"] = bool(enhanced_mode)
        if ocr_max_pages is not None:
            d["_ocr_max_pages"] = int(ocr_max_pages)
        if token_counter is not None and token_counter in _TOKEN_COUNTER_MODES:
            d["_token_counter"] = token_counter
        _stamp_provider_facts(d)
    return _update_app_config(_c)


# ── 角色模型 ──────────────────────────────────────────────────────────────
def roles() -> list[dict]:
    """内部三个角色（压缩提炼 / 命令检查 / 视觉输入）各自可选的模型与当前选择。

    `[{"role", "label", "pool": {id: name}, "current"}]`；池子为空 = 还没配 API Key，
    或该厂商在这个角色上没有可用模型。
    """
    from core.models import ROLES, model_for_role, role_label, role_pool
    main = getattr(_provider(), "target_model", "") or ""
    return [{"role": r, "label": role_label(r),
             "pool": {m: model_name(m) for m in role_pool(main, r)},
             "current": model_for_role(main, r)} for r in ROLES]


def set_role_model(role: str, model_id: str) -> str:
    """记下用户为某个角色选的模型（立即生效：各角色每次用时现问 `model_for_role`）。
    用户选择与厂商事实分两张表存，升级厂商表不会冲掉用户的选择。返回角色的中文名。"""
    from core.models import role_label

    def _c(d: dict) -> None:
        d.setdefault("_role_models", {})[role] = model_id or ""
    if not _update_app_config(_c):
        raise RuntimeError("保存失败")
    logger.info(f"[Roles] {role} = {model_id}")
    return role_label(role)


# ── 环境配置（API Key / 中转 / 代理）─────────────────────────────────────
_ENV_PATH = Path(".env")


def env_config() -> dict:
    """环境配置对话框的初始值与厂商清单：
    `{"vendors": [{"id", "label", "icon", "key_hint"}], "vendor", "api_key", "relay_url", "proxy"}`。"""
    from core.models import vendor_meta, vendors
    vl = vendors()
    vs = [{"id": v, "label": vendor_meta(v).get("label", v), "icon": vendor_meta(v).get("icon", ""),
           "key_hint": vendor_meta(v).get("key_hint", "")} for v in vl]
    cur = (os.environ.get("NANO_API_VENDOR") or (vl[0] if vl else "")).strip().lower()
    if cur not in vl and vl:
        cur = vl[0]
    return {"vendors": vs, "vendor": cur,
            "api_key": ((os.environ.get("NANO_API_RELAY_API_KEY") or "").strip()
                        or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
            "relay_url": (os.environ.get("NANO_API_RELAY_BASE_URL") or "").strip(),
            "proxy": (os.environ.get("HTTP_PROXY") or "").strip()}


def _upsert_dotenv(path: Path, updates: dict) -> None:
    """按键更新 `.env`；值为 None 的键整行删除（「清除」是让它不存在，不是注释掉——
    注释掉的密钥仍在盘上，打包、贴日志时会被一起带出去）。"""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        text = ""
    written, lines = set(), []
    for raw in text.splitlines(keepends=True):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", raw)
        if m and m.group(1) in updates:
            key = m.group(1)
            if updates[key] is not None:
                lines.append(f"{key}={updates[key]}\n")
            written.add(key)
        else:
            lines.append(raw if raw.endswith("\n") else raw + "\n")
    for key, val in updates.items():
        if key not in written and val is not None:
            lines.append(f"{key}={val}\n")
    path.write_text("".join(lines), encoding="utf-8")


def save_env(vendor: str, api_key: str, relay_url: str, proxy: str) -> dict:
    """写 `.env` 并原地重建 provider（无需重启）。

    一把 key 就是一把 key：存在 `NANO_API_RELAY_API_KEY`（变量名里的 RELAY 是历史包袱，改名要
    迁移用户已有的 .env），不再按「有没有中转地址」决定存哪个变量。
    返回 `{"ok": True}`，或 `{"ok": False, "stage": "input" | "write" | "provider", "error"}`。
    """
    api_key, relay_url, proxy = api_key.strip(), relay_url.strip(), proxy.strip()
    vendor = (vendor or "anthropic")
    if not api_key:
        return {"ok": False, "stage": "input", "error": "empty key"}
    updates = {"NANO_API_VENDOR": vendor, "NANO_API_RELAY_API_KEY": api_key,
               "NANO_API_RELAY_BASE_URL": relay_url or None, "ANTHROPIC_API_KEY": None,
               "HTTP_PROXY": proxy or None, "HTTPS_PROXY": proxy or None}
    os.environ["NANO_API_VENDOR"] = vendor
    os.environ["NANO_API_RELAY_API_KEY"] = api_key
    os.environ.pop("ANTHROPIC_API_KEY", None)
    for k, v in (("NANO_API_RELAY_BASE_URL", relay_url), ("HTTP_PROXY", proxy), ("HTTPS_PROXY", proxy)):
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    try:
        _upsert_dotenv(_ENV_PATH, updates)
    except Exception as e:
        return {"ok": False, "stage": "write", "error": str(e)}
    if not _provider().reconfigure():
        return {"ok": False, "stage": "provider", "error": ""}
    return {"ok": True}


# ── OS 权限 / Auto ────────────────────────────────────────────────────────
def permissions() -> dict:
    """六个 OS 权限开关的当前值。"""
    from core.os_layer import dsl
    cur = dsl.load_permissions()
    return {k: bool(cur.get(k, False)) for k in _OS_PERMISSION_KEYS}


def set_permissions(perms: dict) -> dict:
    """写回 `os_state.json` 的 permissions（保留文件里不归权限面板管的字段，如 auto_mode）。
    立即生效：OS 层每次构造都会重新读。返回 `{"ok"}` 或 `{"ok": False, "stage": "read"|"write", "error"}`。"""
    from core.os_layer import dsl
    p = dsl.os_state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:
        return {"ok": False, "stage": "read", "error": str(e)}
    cur = raw.setdefault("permissions", {})
    for k, v in (perms or {}).items():
        if k in _OS_PERMISSION_KEYS:
            cur[k] = bool(v)
    try:
        p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "stage": "write", "error": str(e)}
    return {"ok": True}


def auto_mode() -> bool:
    from core.os_layer import dsl
    return bool(dsl.user_auto_mode_on())


def set_auto_mode(on: bool) -> None:
    from core.os_layer import dsl
    dsl.set_user_auto_mode(bool(on))


# ── 个人资料 ──────────────────────────────────────────────────────────────
def _profile_path() -> Path:
    from core.paths import data_path
    return data_path("user_profile.json")


def profile() -> dict:
    p = _profile_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def save_profile(data: dict) -> None:
    p = _profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data or {}, ensure_ascii=False, indent=2), encoding="utf-8")


def nickname() -> str:
    return str(profile().get("nickname") or "").strip()


def regions() -> list:
    """省市表（个人资料里选所在地用）。"""
    from core.paths import data_path
    try:
        return json.loads(data_path("china_regions_city.json").read_text(encoding="utf-8"))
    except Exception:
        return []


# ── 语言（只影响模型侧的生成语言）─────────────────────────────────────────
def languages() -> dict:
    """`{code: label}`。"""
    from core import i18n
    return {k: v["label"] for k, v in i18n.LANGS.items()}


def current_language() -> str:
    from core import i18n
    return i18n.current_lang()


def set_language(code: str) -> bool:
    from core import i18n
    return bool(i18n.set_lang(code))
