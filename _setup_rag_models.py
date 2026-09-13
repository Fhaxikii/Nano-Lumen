"""下载 RAG 模型（BGE-M3 + bge-reranker-v2-m3）并整理成 HuggingFace 缓存结构。

逻辑复刻自 install.ps1 第 4 步，两处不同：
  1. 由 `py -3.10` 直接跑，绕开 install.ps1 用裸 `python` 打到 Microsoft Store
     占位 exe 的问题（这正是这次模型没装上的直接原因）。
  2. 下载完做一次真实校验（体积 + 关键文件），不只看退出码。

走 modelscope 阿里镜像，支持断点续传——中断后重跑会接着下，已下载的不会白费。
装完这个脚本可以删。

用法：  py -3.10 _setup_rag_models.py
"""
import os
import shutil
import sys
import time

os.environ['MODELSCOPE_ENDPOINT'] = 'https://mirrors.aliyun.com/modelscope/'
os.environ['MODELSCOPE_DOWNLOAD_PARALLELS'] = '16'

PATTERNS = ["*.json", "*.txt", "*.bin", "*.safetensors", "*.model", "tokenizer*", "sentence*"]
HUB = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
REPOS = ("BAAI/bge-m3", "BAAI/bge-reranker-v2-m3")


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


def ensure_safetensors(snap: str, repo: str) -> bool:
    """把 pytorch_model.bin 转成 model.safetensors。

    为什么必须做：transformers 5.x 因 CVE-2025-32434 拒绝对 .bin 调用 torch.load，
    除非 torch >= 2.6（本项目锁 2.5.1，且 RAG 那几个原生库非常玄学，不动依赖）。
    转成 safetensors 后 transformers 会优先走 safetensors 分支，那条版本限制根本不触发。
    modelscope 镜像的 BAAI 仓库只提供 .bin，所以这一步是必需的，不是可选优化。
    """
    st = os.path.join(snap, "model.safetensors")
    binf = os.path.join(snap, "pytorch_model.bin")
    if os.path.exists(st):
        print(f"[skip] {repo} 已有 model.safetensors", flush=True)
        return True
    if not os.path.exists(binf):
        print(f"[FAIL] {repo} 既没有 safetensors 也没有 pytorch_model.bin", flush=True)
        return False

    print(f"[convert] {repo}: pytorch_model.bin -> model.safetensors ...", flush=True)
    t0 = time.time()
    try:
        import torch
        from safetensors.torch import save_file
        sd = torch.load(binf, map_location="cpu", weights_only=True)

        # safetensors 不允许多个 key 共享同一块底层存储（权重绑定的模型会有），
        # 命中就 clone 一份；顺便 contiguous 一下，避免非连续张量保存失败。
        cleaned, seen = {}, {}
        for k, v in sd.items():
            if not isinstance(v, torch.Tensor):
                continue
            ptr = v.data_ptr()
            if ptr in seen:
                v = v.clone()
            else:
                seen[ptr] = k
            cleaned[k] = v.contiguous()

        save_file(cleaned, st, metadata={"format": "pt"})
        print(f"[convert] 完成 {human(os.path.getsize(st))}，用时 {time.time() - t0:.0f}s",
              flush=True)
        return True
    except Exception as e:
        print(f"[FAIL] {repo} 转换失败: {type(e).__name__}: {e}", flush=True)
        return False


def dl(repo: str) -> bool:
    t0 = time.time()
    print(f"\n{'=' * 60}\n下载 {repo}\n{'=' * 60}", flush=True)
    from modelscope.hub.snapshot_download import snapshot_download
    md = snapshot_download(repo, allow_patterns=PATTERNS)
    print(f"\n[modelscope] 落盘于 {md}", flush=True)

    # 整理成 HF 缓存结构：~/.cache/huggingface/hub/models--<org>--<name>/snapshots/main
    cdir = os.path.join(HUB, "models--" + repo.replace("/", "--"))
    snap = os.path.join(cdir, "snapshots", "main")
    refs = os.path.join(cdir, "refs")
    if os.path.exists(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(snap, exist_ok=True)
    os.makedirs(refs, exist_ok=True)
    for item in os.listdir(md):
        src = os.path.join(md, item)
        dst = os.path.join(snap, item)
        (shutil.copy2 if os.path.isfile(src) else shutil.copytree)(src, dst)
    with open(os.path.join(refs, "main"), "w") as f:
        f.write("main")

    # 关键一步：确保有 safetensors（见 ensure_safetensors 的说明）
    if not ensure_safetensors(snap, repo):
        return False

    # 校验：必须有 safetensors 权重，且总体积达标（防止"目录建好了但其实是空的"）
    st = os.path.join(snap, "model.safetensors")
    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(snap) for f in fs)
    ok = os.path.exists(st) and os.path.getsize(st) > 200 * 1024 * 1024
    status = "OK" if ok else "校验未通过（safetensors 缺失或体积异常）"
    print(f"[{status}] {repo}  目录合计 {human(total)}  用时 {time.time() - t0:.0f}s",
          flush=True)
    return ok


def main() -> int:
    print(f"目标缓存目录: {HUB}", flush=True)
    failed = []
    for repo in REPOS:
        try:
            if not dl(repo):
                failed.append(repo)
        except Exception as e:
            print(f"[FAIL] {repo}: {type(e).__name__}: {e}", flush=True)
            failed.append(repo)

    print(f"\n{'=' * 60}", flush=True)
    if failed:
        print(f"以下模型未就绪：{', '.join(failed)}", flush=True)
        print("网络中断的话直接重跑本脚本即可，modelscope 会断点续传。", flush=True)
        return 1

    print("两个模型均已就绪（含 safetensors）。", flush=True)
    # ⚠️ 这一步必须由脚本自己做，不能只打印一句提示。
    #    离线开关没设上的后果不是"慢一点"：huggingface 会联网重拉，
    #    中途被打断后改写 refs/main 并在 .no_exist/ 里写下否定缓存 ——
    #    **权重还在磁盘上（2.27 GB），却从此再也加载不了**（见 core/rag.py 顶部留痕）。
    #    📌 一个做错了就会弄坏已下好模型的步骤，不该指望用户照着提示手动做。
    # ⚠️ 写入点必须在【下载成功之后】：还没有缓存时就设为 1，会直接进离线模式，
    #    于是模型永远下不下来。
    _envp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        _lines = []
        if os.path.exists(_envp):
            with open(_envp, "r", encoding="utf-8") as _f:
                _lines = [l for l in _f.read().splitlines()
                          if not l.strip().startswith("HF_HUB_OFFLINE=")]
        _lines.append("HF_HUB_OFFLINE=1")
        with open(_envp, "w", encoding="utf-8") as _f:
            # ⚠️ 用 "\n" 而不是 os.linesep：文件以文本模式打开，Python 会自己
            #    把 \n 翻译成本平台的换行。写 os.linesep 等于翻译两次，
            #    在 Windows 上每行之间会多出一个空行。
            _f.write("\n".join(_lines).rstrip() + "\n")
        print("已写入 .env：HF_HUB_OFFLINE=1（缓存已就绪，之后一律离线加载）", flush=True)
    except Exception as _e:
        print(f"[WARN] 写 .env 失败（{_e}）—— 请手动在 .env 里加一行 HF_HUB_OFFLINE=1，"
              "否则加载模型时可能联网并损坏本地缓存。", flush=True)
    # 转换完成后 .bin 就是纯冗余（transformers 会优先读 safetensors），可回收空间
    freeable = 0
    for repo in REPOS:
        b = os.path.join(HUB, "models--" + repo.replace("/", "--"),
                         "snapshots", "main", "pytorch_model.bin")
        if os.path.exists(b):
            freeable += os.path.getsize(b)
    if freeable:
        print(f"\n可选：转换后 pytorch_model.bin 已成冗余，删掉可回收 {human(freeable)}。"
              f"\n      想删就跑：py -3.10 _setup_rag_models.py --drop-bin", flush=True)
    return 0


def drop_bin() -> int:
    """删除已被 safetensors 取代的 pytorch_model.bin。只在 safetensors 确实存在时才删。"""
    freed = 0
    for repo in REPOS:
        snap = os.path.join(HUB, "models--" + repo.replace("/", "--"), "snapshots", "main")
        st = os.path.join(snap, "model.safetensors")
        binf = os.path.join(snap, "pytorch_model.bin")
        if os.path.exists(st) and os.path.exists(binf):
            n = os.path.getsize(binf)
            os.remove(binf)
            freed += n
            print(f"[删除] {repo}/pytorch_model.bin  {human(n)}", flush=True)
        elif os.path.exists(binf):
            print(f"[跳过] {repo}: 没有 safetensors，不敢删 .bin", flush=True)
    print(f"共回收 {human(freed)}", flush=True)
    return 0


if __name__ == "__main__":
    if "--drop-bin" in sys.argv:
        sys.exit(drop_bin())
    sys.exit(main())
