# core/proactive/app_catalog.py
"""
应用分类的【单一事实来源】。

历史上有两张各知道一半的表：`intel/scene.py` 的 `_PROC_DEFAULT`（前台进程→Scene）
和 `orchestrator` 里的 `_AMB_EDITOR/_AMB_BROWSER/_AMB_IM/_AMB_TERMINAL`（前台进程→
ambient 活动类别）。两处重叠、各有缺漏、每次看运行数据都要两边维护、还漂移过
（claude 两边都漏、IM 只有 ambient 那边有）。现在合并成这一张表：进程名 → 类别。

- 主动智能的 scene 分类：类别 → Scene 枚举（见 intel/scene.py 的 _CATEGORY_TO_SCENE）
- ambient 轨迹的活动措辞：类别直接用（见 orchestrator._ambient_cat）

新增/调整应用只改这里一处。类别是稳定小集合，不轻易增删（scene 学习稳定性靠它）。

隐私说明：进程名/类别都是本地信号，Nano 拿不到键盘输入内容，聊天软件与其它应用
一视同仁——识别它只是为了"知道现场在干嘛"，不涉及任何内容窥探。
"""
from __future__ import annotations
from typing import Optional

# ── 类别常量（稳定枚举）──────────────────────────────────────────────────
EDITOR   = "editor"      # 代码编辑器 / IDE / AI 编码助手
TERMINAL = "terminal"    # 终端
OFFICE   = "office"      # 文档/表格/笔记（Word/Excel/PPT/Obsidian…）
BROWSER  = "browser"     # 浏览器
IM       = "im"          # 即时通讯 / 聊天（微信/QQ/飞书/Slack…）
MEETING  = "meeting"     # 视频会议
MEDIA    = "media"       # 影音播放
FILE     = "file"        # 文件管理器

# ── 进程名(小写、去 .exe) → 类别 ────────────────────────────────────────
_APP_CATEGORY = {}

def _reg(cat: str, *procs: str):
    for p in procs:
        _APP_CATEGORY[p] = cat

_reg(EDITOR, "code", "cursor", "windsurf", "trae", "claude",
     "pycharm64", "pycharm", "idea64", "devenv", "sublime_text", "notepad++",
     "clion64", "goland64", "webstorm64", "rider64")
_reg(TERMINAL, "windowsterminal", "wt", "powershell", "pwsh", "cmd", "conhost", "alacritty")
_reg(OFFICE, "winword", "wps", "notepad", "wordpad", "obsidian", "typora",
     "et", "excel", "powerpnt", "wpp")
_reg(BROWSER, "chrome", "msedge", "firefox", "brave", "opera", "iexplore")
# 飞书/钉钉/Teams 是聊天+会议超级 app，日常主用是聊天，归 IM。
_reg(IM, "wechat", "weixin", "qq", "tim", "telegram", "discord",
     "feishu", "lark", "dingtalk", "slack", "whatsapp")
_reg(MEETING, "zoom", "teams", "wemeetapp", "webex")
_reg(MEDIA, "vlc", "potplayer64", "potplayermini64", "mpv",
     "cloudmusic", "qqmusic", "spotify")
_reg(FILE, "explorer")


def _norm(process_name: str) -> str:
    p = (process_name or "").lower()
    if p.endswith(".exe"):
        p = p[:-4]
    return p


def categorize(process_name: str) -> Optional[str]:
    """前台进程名 → 类别；未知返回 None。"""
    return _APP_CATEGORY.get(_norm(process_name))
