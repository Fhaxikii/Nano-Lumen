# core/rag_models.py
"""知识库模型（嵌入 / 重排）的本地文件管理：定位、补齐、格式转换、清理。

运行时（``core/rag.py``）与安装器（``install.ps1``）共用本模块，下载逻辑只有这一份。
模块本身只依赖标准库与 loguru；huggingface_hub / modelscope / torch / safetensors
在需要时才导入，安装器可以直接调用。

``ensure_model(repo_id)`` 的处理顺序：

1. **本地优先**：用 ``huggingface_hub.snapshot_download(local_files_only=True)``
   定位 HF 缓存中的快照，不发起任何网络请求。
2. 快照中有可读取的 ``model.safetensors`` 且必需文件齐全 → 直接使用。
3. 快照中只有 ``pytorch_model.bin`` → 就地转换为 safetensors（transformers 5.x
   出于 CVE-2025-32434 拒绝加载 .bin，torch 需 ≥ 2.6）。
4. 本地没有可用快照 → 联网下载：先 HuggingFace（只取 safetensors 与配置），
   得到的快照不完整时改用 ModelScope，并按 HF 缓存结构摆放（``snapshots/main``，
   ``refs/main`` 内容为 ``main``），再按需转换。

任何路径都不会删除含有可用权重的缓存。确认快照可用之后，清理不再需要的副本：
同一快照里的 ``pytorch_model.bin``，以及 ModelScope 缓存中该模型的目录。
清理只记 DEBUG 日志。

每个进程对同一个模型只做一次完整检查，结果缓存在内存中。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import threading
from typing import Iterable, Optional

from loguru import logger

EMBEDDER_REPO = "BAAI/bge-m3"
RERANKER_REPO = "BAAI/bge-reranker-v2-m3"
ALL_REPOS: tuple[str, ...] = (EMBEDDER_REPO, RERANKER_REPO)

WEIGHTS = "model.safetensors"
LEGACY_WEIGHTS = "pytorch_model.bin"
# 加载所需的最小文件集合（权重另行检查）。
REQUIRED_FILES: tuple[str, ...] = ("config.json", "tokenizer.json")

# 从 HuggingFace 下载时只取这些文件：不下载 .bin。
HF_ALLOW_PATTERNS = ["*.json", "*.txt", "*.model", WEIGHTS, "1_Pooling/*", "2_Dense/*"]
# ModelScope 上部分模型只提供 .bin，需要一并下载后再转换。
MS_ALLOW_PATTERNS = ["*.json", "*.txt", "*.bin", "*.safetensors", "*.model",
                     "tokenizer*", "sentence*"]
MS_ENDPOINT = "https://mirrors.aliyun.com/modelscope/"


class ModelUnavailable(RuntimeError):
    """本地没有可用快照，且下载失败。"""


_lock = threading.Lock()
_ready: dict[str, str] = {}


# ── 路径 ──────────────────────────────────────────────────────────────────

def hf_hub_dir() -> pathlib.Path:
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return pathlib.Path(env)
    home = os.environ.get("HF_HOME")
    if home:
        return pathlib.Path(home) / "hub"
    return pathlib.Path.home() / ".cache" / "huggingface" / "hub"


def hf_repo_dir(repo_id: str) -> pathlib.Path:
    return hf_hub_dir() / ("models--" + repo_id.replace("/", "--"))


def modelscope_repo_dirs(repo_id: str) -> list[pathlib.Path]:
    """ModelScope 缓存中该模型可能所在的目录（新旧两种布局）。"""
    base = os.environ.get("MODELSCOPE_CACHE")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".cache" / "modelscope" / "hub"
    org, _, name = repo_id.partition("/")
    return [root / "models" / org / name, root / org / name]


# ── 快照检查 ──────────────────────────────────────────────────────────────

def _weights_readable(path: pathlib.Path) -> bool:
    """safetensors 文件头可以解析即视为完整（文件长度与头部声明不符时解析会失败）。"""
    if not path.is_file():
        return False
    try:
        from safetensors import safe_open
        with safe_open(str(path), framework="pt") as f:
            f.keys()
        return True
    except Exception as e:
        logger.debug(f"[RAG-Models] {path} 无法作为 safetensors 读取: {e}")
        return False


def missing_files(snapshot: pathlib.Path) -> list[str]:
    return [n for n in REQUIRED_FILES if not (snapshot / n).is_file()]


def snapshot_ready(snapshot: pathlib.Path) -> bool:
    return not missing_files(snapshot) and _weights_readable(snapshot / WEIGHTS)


def local_snapshot(repo_id: str) -> Optional[pathlib.Path]:
    """只查本地，不联网。找不到返回 None。"""
    try:
        from huggingface_hub import snapshot_download
        return pathlib.Path(snapshot_download(repo_id, local_files_only=True))
    except Exception as e:
        logger.debug(f"[RAG-Models] {repo_id} 本地无快照: {type(e).__name__}")
        return None


# ── 转换与清理 ────────────────────────────────────────────────────────────

def convert_bin_to_safetensors(snapshot: pathlib.Path) -> None:
    """把 ``pytorch_model.bin`` 转成 ``model.safetensors``。

    先写临时文件，再用 ``os.replace`` 原子替换，转换中断不会留下半个权重文件。
    共享内存的张量先 clone，否则 safetensors 拒绝保存。
    """
    src = snapshot / LEGACY_WEIGHTS
    dst = snapshot / WEIGHTS
    tmp = snapshot / (WEIGHTS + ".tmp")
    import torch
    from safetensors.torch import save_file
    state = torch.load(str(src), map_location="cpu", weights_only=True)
    cleaned: dict = {}
    seen: set = set()
    for k, v in state.items():
        if not isinstance(v, torch.Tensor):
            continue
        ptr = v.data_ptr()
        if ptr in seen:
            v = v.clone()
        else:
            seen.add(ptr)
        cleaned[k] = v.contiguous()
    save_file(cleaned, str(tmp), metadata={"format": "pt"})
    os.replace(tmp, dst)
    logger.debug(f"[RAG-Models] 已转换 {src} → {dst}")


def _size_of(path: pathlib.Path) -> int:
    try:
        if path.is_symlink() or path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def _remove(path: pathlib.Path) -> int:
    """删除文件或目录，返回释放的字节数。失败只记 DEBUG。"""
    size = _size_of(path)
    try:
        if path.is_symlink():
            target = path.resolve()
            path.unlink()
            if target.is_file() and hf_hub_dir() in target.parents:
                target.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            return 0
        return size
    except Exception as e:
        logger.debug(f"[RAG-Models] 清理 {path} 失败: {e}")
        return 0


def cleanup_redundant(repo_id: str, snapshot: pathlib.Path) -> int:
    """快照已确认可用时，删除不再需要的副本。返回释放的字节数。"""
    if not snapshot_ready(snapshot):
        return 0
    freed = 0
    legacy = snapshot / LEGACY_WEIGHTS
    if legacy.exists() or legacy.is_symlink():
        freed += _remove(legacy)
    for d in modelscope_repo_dirs(repo_id):
        if d.exists():
            freed += _remove(d)
    if freed:
        logger.debug(f"[RAG-Models] {repo_id} 清理多余副本，释放 {freed / 1024 ** 3:.2f} GB")
    return freed


# ── 下载 ──────────────────────────────────────────────────────────────────

def _download_hf(repo_id: str) -> Optional[pathlib.Path]:
    try:
        from huggingface_hub import snapshot_download
        return pathlib.Path(snapshot_download(repo_id, allow_patterns=HF_ALLOW_PATTERNS))
    except Exception as e:
        logger.info(f"[RAG-Models] {repo_id} 从 HuggingFace 下载失败（{type(e).__name__}），改用 ModelScope")
        return None


def _download_modelscope(repo_id: str) -> pathlib.Path:
    """从 ModelScope 下载，并按 HF 缓存结构摆放到 ``snapshots/main``。"""
    os.environ.setdefault("MODELSCOPE_ENDPOINT", MS_ENDPOINT)
    os.environ.setdefault("MODELSCOPE_DOWNLOAD_PARALLELS", "16")
    from modelscope.hub.snapshot_download import snapshot_download as ms_download
    src = pathlib.Path(ms_download(repo_id, allow_patterns=MS_ALLOW_PATTERNS))
    repo_dir = hf_repo_dir(repo_id)
    snap = repo_dir / "snapshots" / "main"
    refs = repo_dir / "refs"
    # 走到这里说明 HF 缓存中没有可用快照，repo_dir 里即使有内容也不含可用权重。
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    snap.mkdir(parents=True, exist_ok=True)
    refs.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dst = snap / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    (refs / "main").write_text("main", encoding="utf-8")
    return snap


# ── 入口 ──────────────────────────────────────────────────────────────────

def _prepare(repo_id: str, snapshot: pathlib.Path) -> Optional[pathlib.Path]:
    """快照可用则返回；只缺 safetensors 且有 .bin 则转换后返回；否则 None。"""
    if missing_files(snapshot):
        return None
    if _weights_readable(snapshot / WEIGHTS):
        return snapshot
    if (snapshot / LEGACY_WEIGHTS).is_file():
        try:
            convert_bin_to_safetensors(snapshot)
        except Exception as e:
            logger.warning(f"[RAG-Models] {repo_id} 权重格式转换失败: {type(e).__name__}: {e}")
            return None
        if _weights_readable(snapshot / WEIGHTS):
            return snapshot
    return None


def ensure_model(repo_id: str, *, recheck: bool = False) -> str:
    """保证模型在本地可加载，返回快照目录。失败抛 ``ModelUnavailable``。

    ``recheck=True`` 忽略本进程内的缓存结果，重新检查一次（加载失败后的修复路径使用）。
    """
    with _lock:
        if not recheck and repo_id in _ready:
            return _ready[repo_id]

        snap = local_snapshot(repo_id)
        ready = _prepare(repo_id, snap) if snap is not None else None

        if ready is None:
            logger.info(f"[RAG-Models] {repo_id} 本地没有可用的模型文件，开始下载（约 2.3 GB）")
            downloaded = _download_hf(repo_id)
            if downloaded is not None:
                ready = _prepare(repo_id, downloaded)
            if ready is None:
                try:
                    ready = _prepare(repo_id, _download_modelscope(repo_id))
                except Exception as e:
                    raise ModelUnavailable(
                        f"{repo_id}: 本地没有可用快照，下载失败（{type(e).__name__}: {e}）") from e
            if ready is None:
                raise ModelUnavailable(f"{repo_id}: 下载完成但快照仍不完整")
            logger.info(f"[RAG-Models] {repo_id} 下载完成")

        cleanup_redundant(repo_id, ready)
        _ready[repo_id] = str(ready)
        return _ready[repo_id]


def ensure_all(repos: Iterable[str] = ALL_REPOS) -> dict[str, str]:
    """安装器入口：逐个保证模型可用。"""
    return {r: ensure_model(r) for r in repos}


def all_ready(repos: Iterable[str] = ALL_REPOS) -> bool:
    """只检查、不下载、不转换、不清理。安装器最终校验使用。"""
    for r in repos:
        snap = local_snapshot(r)
        if snap is None or not snapshot_ready(snap):
            return False
    return True


def reset_for_tests() -> None:
    with _lock:
        _ready.clear()
