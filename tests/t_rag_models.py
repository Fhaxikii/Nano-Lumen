# -*- coding: utf-8 -*-
"""知识库模型文件管理（core/rag_models.py）。

所有缓存状态都在临时目录里构造（HF_HUB_CACHE / MODELSCOPE_CACHE 指向临时目录），
下载函数替换为调用即失败的桩，用来证明本地路径不发起网络请求。

用法：
  py -3.10 tests\\t_rag_models.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="nano_rag_models_"))
# 必须在导入 huggingface_hub 之前设置：它在导入时读取缓存目录。
os.environ["HF_HUB_CACHE"] = str(_TMP / "hf")
os.environ["MODELSCOPE_CACHE"] = str(_TMP / "ms")
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ── 构造工具 ──────────────────────────────────────────────────────────────

def _state():
    import torch
    return {"a.weight": torch.ones(4, 4), "b.bias": torch.zeros(4)}


def _make_snapshot(repo_id: str, *, safetensors: bool = False, bin_: bool = False,
                   required: bool = True, truncate_safetensors: bool = False) -> pathlib.Path:
    from core import rag_models as RM
    repo_dir = RM.hf_repo_dir(repo_id)
    snap = repo_dir / "snapshots" / "main"
    snap.mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text("main", encoding="utf-8")
    if required:
        (snap / "config.json").write_text("{}", encoding="utf-8")
        (snap / "tokenizer.json").write_text("{}", encoding="utf-8")
    if safetensors:
        from safetensors.torch import save_file
        save_file(_state(), str(snap / RM.WEIGHTS))
        if truncate_safetensors:
            data = (snap / RM.WEIGHTS).read_bytes()
            (snap / RM.WEIGHTS).write_bytes(data[: len(data) // 2])
    if bin_:
        import torch
        torch.save(_state(), str(snap / RM.LEGACY_WEIGHTS))
    return snap


def _make_modelscope_copy(repo_id: str) -> pathlib.Path:
    from core import rag_models as RM
    d = RM.modelscope_repo_dirs(repo_id)[0]
    d.mkdir(parents=True, exist_ok=True)
    (d / "pytorch_model.bin").write_bytes(b"x" * 1024)
    return d


class _NoNetwork:
    """替换下载函数：被调用时记录并失败。"""

    def __init__(self):
        self.calls: list[str] = []

    def hf(self, repo_id):
        self.calls.append("hf:" + repo_id)
        return None

    def ms(self, repo_id):
        self.calls.append("ms:" + repo_id)
        raise RuntimeError("network disabled in test")


def _install_no_network():
    from core import rag_models as RM
    nn = _NoNetwork()
    RM._download_hf = nn.hf
    RM._download_modelscope = nn.ms
    RM.reset_for_tests()
    return nn


# ── 用例 ──────────────────────────────────────────────────────────────────

def t_local_complete_no_network() -> None:
    print("\n[1] 本地快照完整：不联网，并清理多余副本")
    from core import rag_models as RM
    repo = "test/complete"
    snap = _make_snapshot(repo, safetensors=True, bin_=True)
    ms_dir = _make_modelscope_copy(repo)
    nn = _install_no_network()

    path = RM.ensure_model(repo)
    check(pathlib.Path(path) == snap, "返回本地快照目录", path)
    check(nn.calls == [], "没有调用任何下载函数", str(nn.calls))
    check((snap / RM.WEIGHTS).is_file(), "model.safetensors 保留")
    check(not (snap / RM.LEGACY_WEIGHTS).exists(), "同一快照中的 pytorch_model.bin 已删除")
    check(not ms_dir.exists(), "ModelScope 缓存中该模型的目录已删除")

    calls_before = list(nn.calls)
    RM.local_snapshot = _counting(RM.local_snapshot)
    RM.ensure_model(repo)
    check(RM.local_snapshot.count == 0, "同一进程第二次调用直接使用内存结果，不再检查磁盘")
    check(nn.calls == calls_before, "第二次调用同样不联网")
    RM.local_snapshot = RM.local_snapshot.inner


def _counting(fn):
    def wrapper(*a, **k):
        wrapper.count += 1
        return fn(*a, **k)
    wrapper.count = 0
    wrapper.inner = fn
    return wrapper


def t_bin_only_converted_in_place() -> None:
    print("\n[2] 本地只有 .bin：就地转换，不联网")
    from core import rag_models as RM
    repo = "test/binonly"
    snap = _make_snapshot(repo, bin_=True)
    nn = _install_no_network()

    path = RM.ensure_model(repo)
    check(pathlib.Path(path) == snap, "返回原快照目录（没有重建缓存）")
    check(nn.calls == [], "没有调用任何下载函数", str(nn.calls))
    check(RM._weights_readable(snap / RM.WEIGHTS), "生成的 model.safetensors 可以读取")
    check(not (snap / RM.LEGACY_WEIGHTS).exists(), "转换成功后 pytorch_model.bin 已删除")
    check(not (snap / (RM.WEIGHTS + ".tmp")).exists(), "没有残留临时文件")

    from safetensors import safe_open
    with safe_open(str(snap / RM.WEIGHTS), framework="pt") as f:
        keys = sorted(f.keys())
    check(keys == ["a.weight", "b.bias"], "转换后的张量名与原权重一致", str(keys))


def t_missing_downloads_via_modelscope() -> None:
    print("\n[3] 本地没有：先试 HuggingFace，再用 ModelScope")
    from core import rag_models as RM
    repo = "test/missing"
    calls: list[str] = []

    def fake_hf(repo_id):
        calls.append("hf")
        return None

    def fake_ms(repo_id):
        calls.append("ms")
        return _make_snapshot(repo_id, bin_=True)

    RM._download_hf = fake_hf
    RM._download_modelscope = fake_ms
    RM.reset_for_tests()

    path = RM.ensure_model(repo)
    check(calls == ["hf", "ms"], "下载顺序为 HuggingFace → ModelScope", str(calls))
    check(RM.snapshot_ready(pathlib.Path(path)), "下载并转换后快照可用")


def t_download_failure_raises() -> None:
    print("\n[4] 本地不可用且下载失败：抛 ModelUnavailable")
    from core import rag_models as RM
    repo = "test/broken"
    snap = _make_snapshot(repo, safetensors=True, truncate_safetensors=True)
    _install_no_network()

    check(not RM._weights_readable(snap / RM.WEIGHTS), "被截断的 safetensors 判定为不可读")
    raised = None
    try:
        RM.ensure_model(repo)
    except RM.ModelUnavailable as e:
        raised = e
    check(raised is not None, "抛出 ModelUnavailable", repr(raised))

    from core.rag import _model_load_code
    check(raised is not None and _model_load_code(raised) == "MODEL_FETCH_OFFLINE",
          "rag 将 ModelUnavailable 归类为 MODEL_FETCH_OFFLINE")


def t_missing_required_file_not_ready() -> None:
    print("\n[5] 缺少必需文件时快照不算可用")
    from core import rag_models as RM
    snap = _make_snapshot("test/noconfig", safetensors=True, required=False)
    check(not RM.snapshot_ready(snap), "缺 config.json / tokenizer.json 时 snapshot_ready 为 False",
          str(RM.missing_files(snap)))


def t_all_ready_is_read_only() -> None:
    print("\n[6] all_ready 只检查，不修改文件")
    from core import rag_models as RM
    _install_no_network()
    a = _make_snapshot("test/ro-a", safetensors=True, bin_=True)
    ms_dir = _make_modelscope_copy("test/ro-a")
    check(RM.all_ready(["test/ro-a"]) is True, "完整快照 → True")
    check((a / RM.LEGACY_WEIGHTS).exists() and ms_dir.exists(), "all_ready 没有删除任何文件")
    _make_snapshot("test/ro-b", bin_=True)
    check(RM.all_ready(["test/ro-b"]) is False, "只有 .bin → False（没有被转换）")
    check(not (RM.hf_repo_dir("test/ro-b") / "snapshots" / "main" / RM.WEIGHTS).exists(),
          "all_ready 没有生成 safetensors")
    check(RM.all_ready(["test/does-not-exist"]) is False, "不存在的模型 → False")


def t_single_download_implementation() -> None:
    print("\n[9] 安装器与运行时共用同一份下载代码")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    rag = (ROOT / "core" / "rag.py").read_text(encoding="utf-8")
    check("snapshot_download" not in ps1, "install.ps1 中不再有独立的下载脚本")
    check("core.rag_models import ensure_all" in ps1, "install.ps1 调用 core.rag_models.ensure_all")
    check("core.rag_models import all_ready" in ps1, "install.ps1 的最终校验调用 core.rag_models.all_ready")
    check("snapshot_download" not in rag and "models--" not in rag
          and "modelscope" not in rag.lower(),
          "core/rag.py 不再自行下载模型或直接操作模型缓存路径")


def main() -> int:
    t_local_complete_no_network()
    t_bin_only_converted_in_place()
    t_missing_downloads_via_modelscope()
    t_download_failure_raises()
    t_missing_required_file_not_ready()
    t_all_ready_is_read_only()
    t_single_download_implementation()

    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
