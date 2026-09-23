"""用户发过的图 —— **Nano 自己存一份**，内容寻址。

═══ 它回答的唯一问题 ═══

    「我很久以前发给你的那张图，**我这边还看得见吗**」

⚠️ 注意问的是"**用户这边**"，不是"模型还记得吗"：

  模型是模型，用户 UI 归用户 UI。**UI 的语义是「我曾经发过什么 / Nano 发过什么」**，
  它不该因为模型重启或上下文被压缩而消失 —— 用户依然要能看到、甚至点开。
  ⇒ **UI 端的图没必要跟模型端强绑定。**

📌 **判据：上下文可以忘，历史不可以。** 模型侧的图随时可以被压成占位符
   （那是省 token 的正当手段），但"用户曾经发过这张图"是**用户的历史**，
   它的存亡不该由模型的预算决定。

═══ 🔴 为什么【不能】存用户的原始路径 ═══

存原始路径的话，用户一清理本地图片，Nano 界面里的那张也跟着没了。
而且浏览器上传通道**根本拿不到完整路径**（`e.name` 只有文件名，
Web 平台硬限制）—— 所以"引用原路径"这条路既不该走也走不通。

⭐ **自存一份严格更好**：用户事后移动/删除/清理原文件，一律不影响；
   而且将来加粘贴通道时，粘贴进来的 bytes 一样能落盘
   （"粘贴的图无法回看"这个限制只存在于引用原路径的方案里，自存一份就没有）。

⚠️⚠️ **也不能放 `tempfile.gettempdir()`。** 文件附件那条分支放在
   `%TEMP%/nano_temp_uploads` 是对的 —— 它本来就是**当轮 RAG 的临时素材**。
   但用户的图是**永久历史**，放进系统临时目录等于把它交给磁盘清理工具。
   📌 **照抄一个先例之前，先问它当初为什么放在那儿。**

═══ 内容寻址：同一张图发一百次也只占一份 ═══

文件名就是 `sha256(bytes)` —— 于是去重、幂等重写、损坏可检测三件事一起白拿。
⚠️ 引用串必须**严格校验形状**再拼路径：它会从落盘记录里读回来，
   而"从数据里读出来的东西直接当路径用"是路径穿越的标准入口。
"""
from __future__ import annotations

import hashlib
import pathlib
import threading

from loguru import logger

_lock = threading.Lock()

# `image/png` → `.png`。⚠️ 白名单而不是从 mime 现切后缀：后者等于让上游决定
#    落盘文件名的一部分。认不出的一律 `.bin`（存得下、认得出、不可执行）。
_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
             "gif": "image/gif", "bmp": "image/bmp", "bin": "application/octet-stream"}


def images_dir() -> pathlib.Path:
    from core.paths import data_path
    d = data_path("chat_images")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_ref(ref: str) -> bool:
    """`<64位十六进制>.<后缀>` —— 形状不对就当没有这个引用。

    ⚠️ 这不是洁癖：`ref` 来自**落盘记录**，而把读回来的字符串直接拼进路径
       是路径穿越的标准入口（`../../` 之类）。📌 **从数据里读出来的东西，
       在变成路径之前必须先被证明是它自己声称的那个形状。**
    """
    if not isinstance(ref, str) or "." not in ref:
        return False
    stem, _, ext = ref.partition(".")
    return (len(stem) == 64 and all(c in "0123456789abcdef" for c in stem)
            and ext in _EXT_MIME)


def put_image(raw: bytes, mime: str) -> str:
    """把图片存进 Nano 自己的图库，返回引用串。存不下就返回空串（**不抛**）。

    ⚠️ 失败**绝不能影响发消息** —— 这一层是"多留一份历史"，
       不是发送链路的必要条件。📌 附加价值的失败不许升级成主流程的失败。
    """
    if not raw:
        return ""
    try:
        ext = _MIME_EXT.get((mime or "").lower().strip(), "bin")
        ref = f"{hashlib.sha256(raw).hexdigest()}.{ext}"
        dest = images_dir() / ref
        with _lock:
            # ⭐ 内容寻址的白拿好处：同一张图重发只是一次 `exists()`，不重复写盘。
            if not dest.exists():
                _tmp = dest.with_suffix(dest.suffix + ".part")
                _tmp.write_bytes(raw)
                _tmp.replace(dest)   # 原子落位：读到的要么没有，要么完整
        return ref
    except Exception as e:
        logger.warning(f"[Blobs] 图片存盘失败（不影响发送）: {e}")
        return ""


def image_path(ref: str) -> pathlib.Path | None:
    if not _valid_ref(ref):
        return None
    p = images_dir() / ref
    return p if p.exists() else None


def image_data_uri(ref: str) -> str:
    """读回来拼成 `data:` URI 给 UI 用。拿不到就返回空串。

    ⚠️ 用 data URI 而不是开一条静态路由：路由要额外回答"谁能访问、活多久、
       会不会被拼出越权路径"三个问题，而这里**只是把自己刚存的字节画出来**。
       📌 能不引入新的对外表面就不引入。
    """
    p = image_path(ref)
    if p is None:
        return ""
    try:
        import base64
        mime = _EXT_MIME.get(ref.rpartition(".")[2], "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
    except Exception as e:
        logger.warning(f"[Blobs] 读取图片失败 {ref}: {e}")
        return ""


HANDLE_LEN = 8


def short_handle(ref: str) -> str:
    """`img#a3f2c1d4` —— 给**模型**看的短把手。

    ⚠️ 64 位十六进制指望模型逐字抄回来是不现实的（抄错一位就是"找不到"，
       而那会被读成"图没了"）。8 位足够在一个会话的图里唯一，
       ⭐ 而且**认不出时是响亮失败**（工具明说没找到），不是静默失真。
    """
    return f"img#{ref[:HANDLE_LEN]}" if _valid_ref(ref) else ""


# 📌 从 `_MIME_EXT` 派生而不是手写第二份 —— 两份清单迟早对不上。
_KNOWN_EXTS = set(_MIME_EXT.values()) | {"bin"}


def resolve_handle(handle: str) -> str:
    """短把手 → 完整引用。找不到或**不唯一**都返回空串。

    ⚠️ 不唯一时**不许挑一个** —— 挑错的表现是"它煞有介事地描述了另一张图"，
       📌 而那比"找不到"糟得多：前者用户看不出来，后者用户一眼就知道。
    """
    # ⚠️ 顺序是逻辑的一部分：**先脱引号，再脱前缀**。
    #    反过来时 `` `img#a3f2c1d4` `` 认不出 —— 反引号打头，
    #    `startswith("img#")` 为假，等脱完引号那一步已经过去了。
    #    📌 模型给标识符加反引号是常态，不是异常输入。
    h = (handle or "").strip().lower().strip("`'\"").strip()
    if h.startswith("img#"):
        h = h[4:]
    h = h.strip().strip("`'\"")
    # ⭐ 完整 ref（`<sha256>.png`）和短把手指向**同一个内容寻址名** ——
    #    没有理由只认其中一种。模型手里拿到哪个形状不该由它负责。
    #    ⚠️ 只脱一层已知扩展名，不做通用切分：`.` 之后的东西必须是我们自己写的后缀。
    if "." in h:
        _stem, _, _ext = h.rpartition(".")
        if _stem and _ext in _KNOWN_EXTS:
            h = _stem
    if not h or not all(c in "0123456789abcdef" for c in h):
        return ""
    try:
        hits = [p.name for p in images_dir().glob(f"{h}*") if _valid_ref(p.name)]
    except Exception:
        return ""
    return hits[0] if len(hits) == 1 else ""


def extract_image_blocks(parts) -> list[tuple[bytes, str]]:
    """从 provider 的 image part 里取出 (bytes, mime)。认不出的跳过。

    ⚠️ 只认 Anthropic base64 形状 —— 目前 `build_image_part` 只产出这一种。
       📌 现在就为还不存在的形状写分支，等于写一份没人能验证的代码。
    """
    out: list[tuple[bytes, str]] = []
    import base64
    for b in (parts or []):
        if not isinstance(b, dict) or b.get("type") != "image":
            continue
        src = b.get("source") or {}
        if src.get("type") != "base64" or not src.get("data"):
            continue
        try:
            out.append((base64.b64decode(src["data"]), str(src.get("media_type") or "")))
        except Exception:
            continue
    return out
