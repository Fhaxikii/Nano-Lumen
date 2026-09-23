# -*- coding: utf-8 -*-
"""**「当前语言」这件事的唯一出处。**

═══ 为什么先立这一个文件，而不是直接上 i18n ═══

2026-08-14 观察到：记忆起点卡上的线索**全是英文** —— 提炼器的提示词
是英文，于是它读中文原话、写英文结论，再显示给中文用户。

查现状时发现的东西比那更值得先修：**全仓没有任何「当前语言」的事实来源**，
有的是散在各处提示词里、各自为政的语言策略 ——

    app.py  "Reply in Chinese unless the current-turn user text indicates another language."
    app.py  "Use the user's current conversation language if clear; otherwise use Chinese."

📌 **这不是"少了个开关"，是同一件事有 N 份互不知情的私有答案。**
   任何一处改了策略，别处都不会跟着变，而且**不会有人发现**。

⚠️ 所以本模块**刻意只做一件事**：回答「现在是什么语言」。
   它**不是** i18n 框架 —— 界面文案的抽取与翻译（那笔欠账，
   约 34 处硬编码 + 全部中文 UI）是**另一件事**，见下面「怎么接上去」。
📌 立架子和搬家具一起做，出了问题就分不清是架子的锅还是搬的锅
   （同 `_SETTINGS_TABS` 那一轮）。

═══ 将来的 UI i18n 怎么接到这里 ═══

    core/i18n.py        ← 现在有的：**语言是什么**（本模块）
        │
        ├─ 模型侧：`language_clause()` 拼进提示词      ← 现在就在用
        │
        └─ 界面侧：t("key") / 词条表                   ← ⏸ 尚未接入（不在本模块范围）
                   届时**只加读取端**，`current_lang()` 一个字都不用改。

⭐ 这就是先立它的全部理由：**界面 i18n 是一件大工程，而「结论行说中文」
   今天就能做完 —— 后者不该被压在前者后面。**
"""
from __future__ import annotations

import json
import pathlib
from core.paths import data_dir, data_path

from loguru import logger

# ⚠️ 与「个人信息」同一个文件 —— 它已经是**用户可见设置**的家（`data/user_profile.json`）。
#    📌 不为一个新设置开第二个文件：两份用户设置迟早会各自演化出对方没有的键。
_PROFILE = data_path("user_profile.json")
_KEY = "language"

DEFAULT_LANG = "zh"

# ⚠️ `prompt_name` 是**给模型看的**，必须是模型认得的英文语言名，
#    不能拿 `label`（那是给用户看的）去拼提示词。
#    📌 一个字段同时服务"人读"和"机器读"，迟早有一天会为了讨好其中一个而伤到另一个。
LANGS: dict[str, dict[str, str]] = {
    "zh": {"label": "简体中文", "prompt_name": "Simplified Chinese"},
    "en": {"label": "English", "prompt_name": "English"},
}


def _read() -> dict:
    try:
        if _PROFILE.exists():
            d = json.loads(_PROFILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception as e:
        logger.debug(f"[i18n] 读 user_profile 失败: {e}")
    return {}


def current_lang() -> str:
    """当前语言代码。**永不抛**，认不出来一律回 `DEFAULT_LANG`。

    ⚠️ fail-safe 方向：猜错语言只是文案别扭，抛异常会把调用它的提示词整条搞掉。
    """
    v = str(_read().get(_KEY) or "").strip().lower()
    return v if v in LANGS else DEFAULT_LANG


def set_lang(code: str) -> bool:
    """写回设置。⚠️ 只改这一个键，其余原样保留（那文件里还有个人信息）。"""
    code = str(code or "").strip().lower()
    if code not in LANGS:
        logger.warning(f"[i18n] 未知语言 {code!r}，不改")
        return False
    try:
        d = _read()
        d[_KEY] = code
        _PROFILE.parent.mkdir(parents=True, exist_ok=True)
        _PROFILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[i18n] 语言设为 {code}")
        return True
    except Exception as e:
        logger.error(f"[i18n] 写 user_profile 失败: {e}")
        return False


def label(code: str = "") -> str:
    """给用户看的语言名。"""
    return LANGS.get(code or current_lang(), LANGS[DEFAULT_LANG])["label"]


def prompt_name(code: str = "") -> str:
    """给模型看的语言名（英文）。"""
    return LANGS.get(code or current_lang(), LANGS[DEFAULT_LANG])["prompt_name"]


def language_clause(what: str = "your output") -> str:
    """拼进提示词的那一句。**所有需要指定语言的提示词都该用它。**

    ⚠️ 刻意收成一个函数而不是让各处自己写：
       📌 现在散在 app.py 里那两句（"Reply in Chinese unless…" /
          "Use the user's current conversation language if clear…"）
          **策略并不一致** —— 一处跟随用户当轮语言，一处固定中文兜底。
          它们各自都说得通，合在一起就说不清 Nano 到底按什么规则决定语言。

    ⚠️ **不许拿它去翻译枚举值**（`status` 这类字段是 key 不是文案）——
       调用方要自己在提示词里讲清哪些字段翻、哪些不翻。
    """
    return (f"Write {what} in {prompt_name()}. "
            f"This is the language the user has chosen in the interface.")
