# core/proactive/intel/scene.py
"""
场景分类器：把"前台进程 + 窗口标题（+ 空闲态）"映射到稳定的 Scene 枚举。

立场（2026-06-30 定）：纯本地、不外传，唯一隐私线是"不记录 keystroke 内容"。
窗口标题/页面标题/文件名都是合法且必要的信号——只看进程名太蠢（浏览器里写文档会
被判成 browsing），所以这里【标题感知】。

为什么 scene 仍是封闭枚举：不是隐私，是**慢钟 Ledger 的学习稳定性**——分类标签若
随版本漂移，旧偏好会断档。枚举升级时按 SCENE_TAXONOMY_VERSION 做迁移映射。

精度路线：v0 用"进程 + 标题关键词"启发式；不够了再加桶 / 上轻量 LLM 分类（接口已留）。
"""
from __future__ import annotations

from typing import Optional

from core.proactive import app_catalog as _cat
from core.proactive.intel.types import Scene


# ── 应用类别 → 场景（进程默认兜底；app→类别的表在 app_catalog，两边共用）──────
_CATEGORY_TO_SCENE = {
    _cat.EDITOR: Scene.CODING,
    _cat.TERMINAL: Scene.CODING,
    _cat.OFFICE: Scene.WRITING,
    _cat.BROWSER: Scene.BROWSING,
    _cat.IM: Scene.IM,
    _cat.MEETING: Scene.MEETING,
    _cat.MEDIA: Scene.MEDIA,
    _cat.FILE: Scene.FILE_MANAGEMENT,
}

# ── 标题关键词 → 场景（优先于进程默认；解决"浏览器里其实在写文档/看视频"）──────
# 标题来自窗口标题，本地可见、合法使用。命中越靠前优先级越高。
_TITLE_RULES = [
    # 浏览器里在写作类网页 → writing
    (Scene.WRITING, ["google docs", "腾讯文档", "石墨文档", "语雀", "notion", "飞书文档",
                     "overleaf", "金山文档", "docs.qq", "yuque"]),
    # 浏览器里在看视频/听歌 → media
    (Scene.MEDIA, ["哔哩哔哩", "bilibili", "youtube", "腾讯视频", "爱奇艺", "优酷", "netflix",
                   "- 番剧", "正在播放"]),
    # 浏览器里在写/读代码类 → coding
    (Scene.CODING, ["github", "gitlab", "stack overflow", "stackoverflow", "leetcode",
                    "codepen", "jsfiddle", "- jupyter", "localhost:"]),
    # 会议网页 → meeting
    (Scene.MEETING, ["腾讯会议", "zoom meeting", "google meet", "webex", "会议中"]),
]


def classify_scene(
    process_name: str,
    window_title: str = "",
    idle_seconds: float = 0.0,
    *,
    idle_threshold: float = 15 * 60,
) -> Scene:
    """前台进程 + 标题 → Scene。纯本地信号，标题感知。"""
    # 长空闲后刚回来：idle_recovery（优先级高于一切，用于"续上"类机会）
    if idle_seconds >= idle_threshold:
        return Scene.IDLE_RECOVERY

    title = (window_title or "").lower()

    # 浏览器/通用：先看标题关键词（更准）
    if title:
        for scene, kws in _TITLE_RULES:
            if any(k in title for k in kws):
                return scene

    # 进程默认兜底（app→类别→场景，类别表在 app_catalog）
    cat = _cat.categorize(process_name)
    if cat and cat in _CATEGORY_TO_SCENE:
        return _CATEGORY_TO_SCENE[cat]

    return Scene.UNKNOWN


# ── LLM 分类升级位（v0 暂不接；接口先留好，未来需要更细可插）──────────────────
def classify_scene_llm(process_name: str, window_title: str, provider=None) -> Optional[Scene]:
    """预留：用轻量 LLM 把"进程+标题"归到稳定枚举（仍归枚举，保学习稳定性）。
    v0 不启用，返回 None 表示走规则版。"""
    return None
