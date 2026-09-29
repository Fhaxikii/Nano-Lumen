# -*- coding: utf-8 -*-
"""知识库模型文件管理（core/rag_models.py）。

所有缓存状态都在临时目录里构造（HF_HUB_CACHE / MODELSCOPE_CACHE 指向临时目录），
下载函数替换为调用即失败的桩，用来证明本地路径不发起网络请求。

内存不足的用例制造真实的提交失败：子进程把自己放进限制单进程提交内存的 Job 对象，
再加载一个大于限额的权重文件（safetensors 以写时复制方式映射整个文件，映射大小计入提交）。

用法：
  py -3.10 tests\\cases\\t_rag_models.py
"""
from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 子进程模式：`t_rag_models.py --oom-child <缓存目录>`，沿用父进程构造好的缓存。
_OOM_CHILD = len(sys.argv) > 2 and sys.argv[1] == "--oom-child"
_TMP = (pathlib.Path(sys.argv[2]) if _OOM_CHILD
        else pathlib.Path(tempfile.mkdtemp(prefix="nano_rag_models_")))
# 必须在导入 huggingface_hub 之前设置：它在导入时读取缓存目录。
os.environ["HF_HUB_CACHE"] = str(_TMP / "hf")
os.environ["MODELSCOPE_CACHE"] = str(_TMP / "ms")
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

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


def t_memory_error_signals() -> None:
    print("\n[7] 内存不足的判定")
    from core import rag_models as RM
    from core.rag import _model_load_code
    zh = OSError("页面文件太小，无法完成操作。 (os error 1455)")
    en = OSError("The paging file is too small for this operation to complete. (os error 1455)")
    other_lang = OSError("Le fichier d'échange est insuffisant. (os error 1455)")
    with_winerror = OSError(None, "commit failed", None, 1455)
    positives = {
        "safe_open 映射失败（中文系统）": zh,
        "safe_open 映射失败（英文系统）": en,
        "只靠错误码（其他语言的系统）": other_lang,
        "带 winerror 属性的 OSError": with_winerror,
        "os error 1450": OSError("Insufficient system resources exist. (os error 1450)"),
        "MemoryError": MemoryError(),
        "torch 分配失败": RuntimeError("DefaultCPUAllocator: not enough memory: you tried to allocate 1 bytes."),
    }
    for name, e in positives.items():
        check(RM.is_memory_error(e), f"{name} → 内存不足", repr(e))
        check(_model_load_code(e) == "MODEL_LOAD_OOM", f"{name} → rag 归类为 MODEL_LOAD_OOM",
              _model_load_code(e))
    negatives = {
        "文件不存在": FileNotFoundError(2, "No such file or directory"),
        "os error 2": OSError("系统找不到指定的文件。 (os error 2)"),
        "os error 14550": OSError("unrelated (os error 14550)"),
        "文件头损坏": Exception("Error while deserializing header: HeaderTooLarge"),
    }
    for name, e in negatives.items():
        check(not RM.is_memory_error(e), f"{name} → 不是内存不足", repr(e))


def t_cleanup_skipped_when_memory_short() -> None:
    print("\n[8] 内存不足无法确认快照时，清理跳过而不是报错")
    from core import rag_models as RM
    snap = _make_snapshot("test/cleanup-oom", safetensors=True, bin_=True)
    original = RM.snapshot_ready

    def short_of_memory(_snapshot):
        raise OSError("页面文件太小，无法完成操作。 (os error 1455)")

    RM.snapshot_ready = short_of_memory
    try:
        freed = RM.cleanup_redundant("test/cleanup-oom", snap)
    finally:
        RM.snapshot_ready = original
    check(freed == 0, "返回 0", str(freed))
    check((snap / RM.LEGACY_WEIGHTS).exists() and (snap / RM.WEIGHTS).exists(), "没有删除任何文件")


# ── 真实内存不足（子进程） ─────────────────────────────────────────────────

_OOM_REPO = "test/oom"
_OOM_WEIGHTS_MB = 400
_OOM_HEADROOM_MB = 128


def _write_large_safetensors(path: pathlib.Path, mb: int) -> None:
    """写一个合法的 safetensors：只写文件头，数据区用 truncate 补零，不占用进程内存。"""
    n = mb * 2 ** 20 // 4
    header = json.dumps({"w": {"dtype": "F32", "shape": [n], "data_offsets": [0, n * 4]}}).encode()
    header += b" " * (-len(header) % 8)
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header)))
        f.write(header)
        f.truncate(8 + len(header) + n * 4)


def _limit_own_commit(extra_mb: int) -> None:
    """把当前进程放进 Job 对象，单进程提交内存上限 = 当前提交量 + extra_mb。"""
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC)] + [
            (n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")] + [
            (n, ctypes.c_size_t) for n in (
                "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.CreateJobObjectW.restype = wt.HANDLE
    me = k32.GetCurrentProcess()
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    if not psapi.GetProcessMemoryInfo(wt.HANDLE(me), ctypes.byref(pmc), pmc.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x100          # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = pmc.PrivateUsage + extra_mb * 2 ** 20
    job = k32.CreateJobObjectW(None, None)
    if not job or not k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info),
                                                 ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    if not k32.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(me)):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")


def _oom_child() -> int:
    """子进程：在提交内存受限的情况下加载嵌入模型，把结果写到 stdout 最后一行。"""
    import sentence_transformers  # noqa: F401  _load_embedder 内部导入；限额之前先导入
    from core import rag, rag_models as RM
    from core.health import Cap, get_health

    downloads: list[str] = []

    def no_hf(repo_id):
        downloads.append("hf:" + repo_id)
        return None

    def no_ms(repo_id):
        downloads.append("ms:" + repo_id)
        raise RuntimeError("network disabled in test")

    RM._download_hf = no_hf
    RM._download_modelscope = no_ms
    RM.EMBEDDER_REPO = _OOM_REPO
    RM.reset_for_tests()

    _limit_own_commit(_OOM_HEADROOM_MB)
    try:
        rag._load_embedder()
        outcome = "loaded"
    except Exception as e:
        outcome = f"{type(e).__name__}: {e}"
    state = get_health().get(Cap.KB_VECTOR_SEARCH)
    print(json.dumps({
        "outcome": outcome,
        "downloads": downloads,
        "code": state.code if state else "",
        "hint_en": state.recovery_hint_en if state else "",
    }, ensure_ascii=True))
    return 0


def t_real_memory_shortage() -> None:
    print("\n[10] 真实内存不足：报内存不足，不下载，不删除模型文件")
    from core import rag_models as RM
    snap = _make_snapshot(_OOM_REPO)
    weights = snap / RM.WEIGHTS
    _write_large_safetensors(weights, _OOM_WEIGHTS_MB)
    size = weights.stat().st_size
    check(RM._weights_readable(weights), "不限内存时这份权重可以读取（前提成立）")

    r = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), "--oom-child", str(_TMP)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    result = json.loads(lines[-1]) if lines else {}
    check(r.returncode == 0 and bool(result), "子进程正常结束并给出结果",
          f"rc={r.returncode} {r.stderr[-400:]}")
    check(result.get("outcome", "loaded") != "loaded", "加载失败（限额生效）", result.get("outcome", ""))
    check(result.get("downloads") == [], "没有调用任何下载函数", str(result.get("downloads")))
    check(result.get("code") == "MODEL_LOAD_OOM", "健康状态记为 MODEL_LOAD_OOM", result.get("code", ""))
    check("do NOT re-download" in result.get("hint_en", ""), "给模型的恢复建议写明不要重新下载")
    check(weights.is_file() and weights.stat().st_size == size, "权重文件原样保留")
    check(RM.hf_repo_dir(_OOM_REPO).is_dir() and not RM.missing_files(snap), "模型缓存目录与必需文件都在")


def t_single_download_implementation() -> None:
    print("\n[9] 安装器与运行时共用同一份下载代码")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    rag = module_text("core.rag")
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
    t_memory_error_signals()
    t_cleanup_skipped_when_memory_short()
    t_single_download_implementation()
    t_real_memory_shortage()

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
    sys.exit(_oom_child() if _OOM_CHILD else main())
