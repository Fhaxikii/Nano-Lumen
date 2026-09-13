"""导出全部聊天记录 —— **「数据入口」，不是「搜索」。**

═══ 它为什么存在 ═══

早先定过一条：**「UI 只保留到 L2 这件事落地时，必须同时有『用户侧历史入口』，
否则是能力倒退」**。而设计到一半时发现**混淆了两个概念**：

    「搜索」  是一个**动作** —— 在还看得见的东西里快速定位、跳过去
    「数据入口」是一件**产权** —— 用户说过的话，用户得能拿到

📌 **跳不过去的东西，与其在 UI 里摆着，不如让用户真正拿走。**
   已经滚出 UI 的历史，在 Nano 自己界面里显示也跳转不过去；导出去反而是真能用的。

⭐ 所以「能力倒退」那道门由**这里**关，不由搜索关：
   搜索只管 L0–L2（UI 里真实存在的），出了 UI 的历史靠导出拿回来。

═══ 格式：md 给人看，json 不丢东西，图片跟着走 ═══

    <选定目录>/Nano对话记录_20260814_0312/
        对话记录.md      ← 人读的主格式（会话分段、工具一行摘要）
        原始数据.json    ← 完整落盘原文，一个字段不丢
        images/          ← ⚠️ 图片**复制一份**过来，不是引用 data/chat_images/

⚠️ 图片必须复制：md 里若指向 `data/chat_images/`，用户清理那个目录、
   或者把导出的文件夹拷到别的机器，图就全断了。
   📌 **不能让一份"给用户的东西"依赖一个用户会清理的路径。**
⚠️ md 会丢结构（thinking 签名、tool_use_id、可见性标记），所以 json 必须同时给：
   📌 **一个「数据入口」如果丢东西，它就不是数据入口，只是一份摘要。**
"""
from __future__ import annotations

import json
import pathlib
import shutil
import time
from typing import Any

from loguru import logger

_ROLE_LABEL = {"user": "你", "assistant": "nano", "model": "nano"}


def _text_of(content: Any) -> str:
    """从（可能是多模态列表的）content 里取可读文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                out.append(str(b["text"]))
        return "\n".join(out)
    return str(content or "")


def export_all(dest_dir: str | pathlib.Path, kernel) -> dict:
    """把库里全部会话导出到 `dest_dir` 下的一个新文件夹。返回统计。

    ⚠️ **导出的是全部会话，包括已经「重置」掉的** —— 重置的语义是
       「放弃、不再出现在 UI 里」，**不是「删除」**（重置弹窗的措辞也是这么写的）。
       📌 措辞和行为必须对得上：既然那里说的是"放弃"，这里就不该把它们当成已删。
    """
    root = pathlib.Path(dest_dir) / f"Nano对话记录_{time.strftime('%Y%m%d_%H%M')}"
    root.mkdir(parents=True, exist_ok=True)

    with kernel.store.read() as c:
        sessions = list(c.execute(
            "SELECT session_id, is_current, created_at, closed_at, close_reason "
            "FROM conversation_sessions ORDER BY created_at"))
        rows = list(c.execute(
            "SELECT session_id, ordinal, role, payload_json, created_at "
            "FROM conversation_messages ORDER BY session_id, ordinal"))

    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(str(r["session_id"]), []).append(r)

    lines: list[str] = ["# Nano 对话记录", "",
                        f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                        f"共 {len(sessions)} 段会话、{len(rows)} 条消息。", ""]
    raw: list[dict] = []
    img_dir = root / "images"
    n_img = 0
    n_visible = 0

    for s in sessions:
        sid = str(s["session_id"])
        msgs = by_session.get(sid, [])
        if not msgs:
            continue
        _t = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(s["created_at"] or 0)))
        _cur = "（当前会话）" if s["is_current"] else ""
        lines += ["", "---", "", f"## 会话 {_t} {_cur}".rstrip(), ""]

        for r in msgs:
            try:
                p = json.loads(r["payload_json"])
            except Exception:
                continue
            raw.append({"session_id": sid, "ordinal": r["ordinal"],
                        "role": r["role"], "created_at": r["created_at"], "payload": p})

            # ⚠️⚠️ **系统注记不进 md。** 它们为了让模型读到才以 user/assistant 角色
            #    落盘，用户从没在屏幕上见过。📌 与落盘那条同源：
            #    **一份账本同时服务「模型上下文」和「用户看过的东西」时，
            #    凡是给用户的出口都必须过 `visible_to_user` 这一关。**
            #    🔴 少这一关，导出文件里就会出现一堆署着用户名字的英文系统提示词
            #    —— 那是同一个泄露 bug 的第四扇门。
            if p.get("visible_to_user") is False:
                continue

            role = str(r["role"])
            if role in ("user", "assistant", "model"):
                body = _text_of(p.get("content")).strip()
                if body:
                    lines += [f"**{_ROLE_LABEL.get(role, role)}：**", "", body, ""]
                    n_visible += 1
                for ref in (p.get("ui_images") or []):
                    src = _blob_path(ref)
                    if src is None:
                        lines += [f"> （图片 {ref[:8]} 已不在图库）", ""]
                        continue
                    img_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, img_dir / src.name)
                        lines += [f"![图片](images/{src.name})", ""]
                        n_img += 1
                    except Exception as e:
                        lines += [f"> （图片复制失败：{e}）", ""]
                if p.get("image_summary"):
                    lines += [f"> 〔当时记下的图片描述〕{p['image_summary']}", ""]
            elif role == "tool_calls":
                names = [t.get("name", "?") for t in (p.get("tool_calls") or [])]
                if names:
                    lines += [f"> 🔧 调用工具：{'、'.join(names)}", ""]

    (root / "对话记录.md").write_text("\n".join(lines), encoding="utf-8")
    (root / "原始数据.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    stat = {"dir": str(root), "sessions": len(sessions), "messages": len(rows),
            "visible": n_visible, "images": n_img}
    logger.info(f"[Export] 导出完成 {stat}")
    return stat


def _blob_path(ref: str):
    try:
        from core.runtime.blobs import image_path
        return image_path(ref)
    except Exception:
        return None
