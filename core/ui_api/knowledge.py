# -*- coding: utf-8 -*-
"""知识库文件与本轮临时附件。

知识库目录（`data/knowledge/`）与临时附件目录的读写都在这里；界面只传文件名和字节、
拿结果。和 Skill 一样，界面上直接删除知识库文件绕过了对话，成功后往对话里记一条
写明来源的系统记录。
"""
from __future__ import annotations

import os
import pathlib

from core.ui_api import _state

# 可以放进知识库的格式
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv",
                                ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})


def _rag():
    from core import rag
    return rag


def _kb_dir() -> pathlib.Path:
    from core.paths import data_path
    return data_path("knowledge")


def _note(text: str) -> None:
    try:
        _state.require_agent().memory.add_system_note("assistant", text)
    except Exception:
        pass


# ── 知识库 ────────────────────────────────────────────────────────────────
def ensure_dir() -> None:
    _kb_dir().mkdir(parents=True, exist_ok=True)


def list_files() -> dict:
    """健康度报告：`{"summary": {...}, "files": [...]}`。每个文件另带 `mtime`（入库时间的近似，
    取文件本身的修改时间；读不到为 0），列表按它倒序排最新的在前由界面决定。"""
    rep = dict(_rag().get_health_report() or {})
    files = []
    for r in rep.get("files") or []:
        d = dict(r)
        p = d.get("path") or ""
        try:
            d["mtime"] = float(os.path.getmtime(p)) if p else 0.0
        except Exception:
            d["mtime"] = 0.0
        files.append(d)
    rep["files"] = files
    return rep


def store_file(filename: str, raw: bytes) -> dict:
    """把上传的文件写进知识库目录（还没入库）。

    返回 `{"ok": True}`，或 `{"ok": False, "reason": "empty" | "unsupported" | "exists", "suffix"}`。
    同名文件不覆盖：用户要先删旧的或改名。
    """
    suffix = pathlib.Path(filename).suffix.lower()
    if not raw:
        return {"ok": False, "reason": "empty", "suffix": suffix}
    if suffix not in SUPPORTED_SUFFIXES:
        return {"ok": False, "reason": "unsupported", "suffix": suffix}
    ensure_dir()
    dest = _kb_dir() / filename
    if dest.exists():
        return {"ok": False, "reason": "exists", "suffix": suffix}
    with open(dest, "wb") as f:
        f.write(raw)
    return {"ok": True}


def index_file(filename: str, enhanced_mode: bool, max_ocr_pages: int) -> dict:
    """给知识库目录里的这个文件建索引（耗时，调用方放到线程里）。
    返回 `{"indexed", "skipped", "error"}`（`error` 为第一条错误信息或空串）。"""
    stats = _rag().index_single_file(str(_kb_dir() / filename),
                                     {"enhanced_mode": bool(enhanced_mode),
                                      "max_ocr_pages": int(max_ocr_pages)}) or {}
    errs = stats.get("errors") or []
    return {"indexed": int(stats.get("indexed") or 0), "skipped": int(stats.get("skipped") or 0),
            "error": str((errs[0] or {}).get("error") or "") if errs else ""}


def file_text(filename: str) -> str:
    """入库时提取到的文本（按块顺序拼接；给检索用的结构摘要块不算正文，去掉）。"""
    result = _rag()._get_collection().get(where={"filename": filename},
                                          include=["documents", "metadatas"])
    rows = list(zip(result.get("metadatas", []), result.get("documents", [])))
    rows = [(m, d) for m, d in rows if (m or {}).get("chunk_type") != "schema"]
    rows.sort(key=lambda x: (x[0] or {}).get("chunk_index", 0))
    return "\n\n".join(d for _, d in rows if d)


def delete_file(filename: str) -> None:
    """从索引与磁盘删除，并在对话里记一条（写明是界面上用户删的）。"""
    _rag().delete_file(filename)
    p = _kb_dir() / filename
    if p.exists():
        p.unlink()
    _note(f'[System record: the user deleted knowledge-base file "{filename}" from the UI sidebar; '
          f'this was not executed in the current chat.]')


# ── 本轮临时附件 ──────────────────────────────────────────────────────────
def add_temp_file(filename: str, raw: bytes) -> None:
    """存盘并登记到当前会话（不建索引，模型真要检索时再建）。"""
    rag = _rag()
    d = rag._temp_uploads_dir()
    d.mkdir(exist_ok=True)
    dest = d / filename
    with open(dest, "wb") as f:
        f.write(raw)
    rag.register_temp_file(filename, str(dest))


def remove_temp_file(filename: str) -> bool:
    return bool(_rag().remove_temp_file(filename))


def clear_temp_files() -> None:
    _rag().clear_temp_knowledge()

