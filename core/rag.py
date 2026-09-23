# core/rag.py
"""
Nano RAG 核心模块
支持：txt / md / pdf / docx / xlsx / xls / csv 文档入库，向量相似度检索 + BM25 关键词检索

设计变更：
- Step 2：RAG 改为真工具 query_local_knowledge 由模型自主调用，杜绝硬注入污染。
- Step 5：
  · docx 增加表格 / 页眉 / 页脚提取；统计跳过的图片数量
  · xlsx 改用 openpyxl 按 cell 读取，保留位置信息，统计 chart / image
  · 每个文件生成 parse_report，写入 data/parse_reports.json 供健康度面板使用
- Step 5.5（hybrid search + reranker）：
  · 在向量检索基础上叠加 BM25 关键词检索
  · 用 RRF（Reciprocal Rank Fusion）融合两路排名
  · 末尾加 cross-encoder reranker（bge-reranker-v2-m3）做语义重排
  · 解决"长 query 里修饰词淹没真正相关 chunk"的核心问题
  · 验收判据：一句**修饰词很长、关键信息在末尾**的提问，
    要能命中正文末尾那条短条款，而不是被中间的表格淹掉

═══ 数据访问层 ═══
- load_full_file(filename, with_images=False)：全文加载入口，不走 chunk/embedding
  · 与 RAG 通道职责分离：RAG 查片段，load_full_file 读完整文件
  · 复用 _parse_file 的多模态解析逻辑
- list_knowledge_files()：合并持久库 + 临时库的文件清单，临时文件带 [临时] 前缀
- _parse_file 加 report=None 默认参数，方便 load_full_file 直接调

已知局限（Phase 1 实测前先记录，后续若实测正常则不需修正）：
- with_images=True 返回的是文字描述（_describe_image_multimodal 调用结果），不是 inline base64 图片
  · 优点：实现简单、token 可控、跟现有 RAG 通道复用同一套描述生成
  · 缺点：有"图→文字"翻译损耗，细节型问题（如"图里第三个分支写什么"）可能不准
  · Phase 4 用例 4.6.C 实测后决定是否升级到 inline base64
- list_knowledge_files 中临时文件信息来源是 _temp_collection 的 metadatas
  · 在 Phase 3 临时附件改 lazy build 后，未入索引的临时文件不会出现在 _temp_collection 里
  · Phase 3 会改造此函数，让它能感知未入索引的临时文件（通过 app.py 维护的内存映射）
  · 当前 Phase 1 实现：只列出 _temp_collection 里有的临时文件（即已入索引的）
- 超量保护阈值固定 30 万字符（约 15 万 token），未对齐到具体模型上下文窗口
  · 当前默认对所有模型用同一阈值，保守
  · 若后续需要按模型动态调整，可在 orchestrator 层注入

═══════════════════════════════════════
"""

import os
import pathlib
import hashlib
import json
import datetime
import threading
from typing import List, Dict, Any, Optional, Tuple
from loguru import logger

from core import rag_models as _rag_models

# ── 加载 .env（本模块会被脚本、子进程和测试直接 import，不经过 app.py）──
# 模型文件的定位与下载不依赖任何环境变量，见 core/rag_models.py。
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=pathlib.Path(__file__).parent.parent / ".env")
except Exception:
    pass


# ══════════════════════════════════════════════════════════════════════════
# 懒加载单例的初始化锁（2026-08-03 实测抓到的竞态，见 _get_collection 注释）
# ══════════════════════════════════════════════════════════════════════════
# 这三个全局都是"裸 if is None 就构造"的懒加载单例，而它们至少被两类线程并发访问：
#   · rag-init 后台线程（Orchestrator._init_rag_async → index_documents）
#   · UI 线程（启动时刷新知识库列表 / 用户查询）
# 三者构造的都是重型原生栈（chromadb 的 Rust bindings、torch/sentence-transformers），
# 并发构造会让后进来的线程看到半初始化对象。必须串行化。
_chroma_init_lock = threading.Lock()
_embedder_init_lock = threading.Lock()
_reranker_init_lock = threading.Lock()

_chroma_client = None
_collection    = None
_embedder      = None
# 进程级共享的"启动初始化阶段"日志。
# 供 app.py 的初始化遮罩轮询展示进度。每个真实里程碑只在第一次
# 真正完成时 append 一次（受各自的缓存变量保护，不会重复触发）。
# 用简单字符串编码，格式见各 append 处注释。
_init_stage_log: List[str] = []


def get_init_stage_log() -> List[str]:
    """返回启动初始化阶段日志（只读副本）。供 app.py 轮询用。"""
    return list(_init_stage_log)


# 临时知识库：内存中的 session 级 collection，会话结束自动消失
# 对话窗口上传的文件走这里，图片走image part直接进context
_temp_client     = None
_temp_collection = None

# Phase 3：当前会话注册的临时文件映射 {filename: absolute_path_str}
# 只有通过 register_temp_file() 注册过的文件才会被 lazy build
# 防止历史遗留文件被意外索引导致性能问题
_registered_temp_files: Dict[str, str] = {}

# BM25 索引：内存里的轻量结构。每次启动重建（重建很快）。
# 结构：[{"id": chroma_id, "filename": ..., "tokens": [..词列表..], "content": ...}, ...]
# 🔴🔴 **BM25 的语料和索引必须一起换。**
#
# 改造前它们是两个全局、两次独立赋值（`_bm25_corpus = corpus` 然后
# `_bm25_index = BM25Okapi(tokenized)`），而第二句是**构造，耗时**。
# ⇒ 窗口内的状态是「新语料 + 旧索引」，读侧用旧索引算出的下标去查新语料：
#     下标越界 → IndexError；下标恰好在范围内 → **安静地返回另一段文字**。
# ⚠️ 并发是真的：上传走 `to_thread(index_single_file)` 重建，
#    检索走 `to_thread(query_for_agent)` 读，而那三个初始化锁都不护这一对。
#
# ⭐ 所以改成**一个元组、单次赋值**：
#    📌 **让错误状态在结构上不可能，比用锁保证它不发生更可靠** ——
#       锁要求每个新写的人都记得加，元组不要求任何人记得任何事。
# ⚠️ `_bm25_corpus` / `_bm25_index` 保留，但**只作为读视图**（别处还在用它们
#    判空和做诊断）；它们一律从 `_bm25_pair` 派生，**不再单独赋值**。
_bm25_pair: tuple = ([], None)          # (corpus, index) —— 唯一真相
_bm25_corpus: List[Dict[str, Any]] = []
_bm25_index = None  # BM25Okapi 实例


def _set_bm25(corpus, index) -> None:
    """原子换掉这一对。**任何地方要改 BM25 状态都必须走这里。**"""
    global _bm25_pair, _bm25_corpus, _bm25_index
    _bm25_pair = (corpus, index)        # ← 单次赋值，读侧要么看到全旧、要么全新
    _bm25_corpus, _bm25_index = corpus, index
_bm25_available: Optional[bool] = None  # 缓存依赖是否可用的判断

CHROMA_DIR      = str(pathlib.Path(__file__).parent.parent / "data" / "chroma_db")
HASH_STORE      = str(pathlib.Path(__file__).parent.parent / "data" / "indexed_hashes.json")
PARSE_REPORTS   = str(pathlib.Path(__file__).parent.parent / "data" / "parse_reports.json")
COLLECTION_NAME = "nano_knowledge"
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv",
                        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 图片格式集合（供 _parse_file 判断）
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 图片 MIME 类型映射
IMAGE_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
    ".bmp": "image/bmp", ".gif": "image/gif",
}

# 单文件大小硬上限。修改此值需谨慎：超大文件解析峰值内存可能十倍于文件本身。
MAX_FILE_SIZE_MB = 50

# 极短 query 跳过阈值（按非空字符算）
MIN_QUERY_CHARS = 4

# RRF 融合参数：k 越大，排名靠后的 chunk 影响越小。业界默认 60。
RRF_K = 60

# 🪦 **`LOAD_FULL_MAX_CHARS` 已于 2026-08-26 停用**（原值 300,000）。
#    原注释：「全文加载超量保护阈值（字符数）/ 超过则降级为返回
#    章节标题列表 + 建议用 RAG」。
# 🔴 它护错了位置：贵的是**解析**（峰值内存 10×），而它砍的是**解析之后**的文本 ——
#    那一刻峰值已经过去了。护内存的是 `MAX_FILE_SIZE_MB`（解析前），保留。
# 🔴🔴 而它与迭代阅读**互斥**：它说「读不完就去用 RAG」，
#    说「用迭代阅读替代一次性注入」。留着它，超大文件的正文永远读不到。
# ⚠️ **符号保留，但理由不是第一版写的那个。**
#    第一版写的是「`load_full_file_meta` 等处还在读它做判断」——**核实下来是假的**：
#    全仓除了本行注释，只有两处 docstring 提到那个函数的字段名，
#    **没有任何代码读这个常量**。
#    📌 给一个刚做完的改动写理由时，**顺手编了一个没核实的依赖**。
#       而那种注释最坏：它看起来是一条约束，下一个人会照着它不敢动。
# ⭐ 真正保留符号的理由只有一条：外部/未来的代码可能 import 它，
#    删掉是一次不必要的破坏性变更；改成哨兵值则语义清晰 ——「不再截断」。

LOAD_FULL_MAX_CHARS = 1 << 62


# ══════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════

def _build_model(repo_id: str, factory):
    """准备本地模型文件并构造模型对象。

    权重格式被拒或文件缺失时，忽略本进程的检查缓存、重新检查（必要时转换或下载）后重试一次；
    其他错误直接抛出。
    """
    try:
        # transformers 加载权重时的进度条，关闭。
        from transformers.utils import logging as _hf_logging
        _hf_logging.disable_progress_bar()
    except Exception:
        pass
    try:
        return factory(_rag_models.ensure_model(repo_id))
    except Exception as e:
        code = _model_load_code(e)
        if code not in ("WEIGHTS_FORMAT_REJECTED", "MODEL_MISSING"):
            raise
        logger.warning(f"[RAG] {repo_id} 加载失败（{code}），重新检查本地模型文件后重试一次")
        return factory(_rag_models.ensure_model(repo_id, recheck=True))


def _load_embedder():
    """加载 bge-m3。

    ⚠️ 这是全项目最危险的一次调用 —— 实测结论是
    bge-m3 模型栈(torch/transformers) 与 chroma-hnswlib 在同一进程里会造成原生堆内存
    损坏 → 随机 segfault（5 次复现 3 崩）。segfault 走不到任何 except，所以这里必须
    用 write-ahead breadcrumb：动手【之前】写盘，成功后清掉；下次启动看到没清掉的痕迹
    就知道上次死在这一步。excepthook 抓不到 segfault，指望不上。
    """
    global _embedder
    if _embedder is not None:                # 快路径：已加载，不进锁
        return _embedder

    from core.health import Cap, report_fault, report_ok, get_health, Status, Severity
    from core import crash_journal
    with _embedder_init_lock:                # 见模块头：并发构造重型原生栈会出事
        if _embedder is not None:
            return _embedder
        try:
            with crash_journal.breadcrumb(
                "rag.load_embedder",
                capability=Cap.KB_VECTOR_SEARCH,
                detail="SentenceTransformer('BAAI/bge-m3') —— 原生栈崩溃高发点",
            ):
                from sentence_transformers import SentenceTransformer
                # bge-m3：智源 2024 年发布的多语言嵌入模型，中文 RAG 业界默认选择。
                # 上一代 paraphrase-multilingual-MiniLM-L12-v2 在中文短查询场景下相似度信号过弱
                # （"解释权"对原文 chunk 余弦相似度仅 0.24，低于无关 Excel cell 的 0.47）。
                # 模型约 2.27GB，推理内存约 1-2GB。文件的定位、下载与格式转换见 core/rag_models.py。
                _embedder = _build_model(_rag_models.EMBEDDER_REPO, SentenceTransformer)
        except Exception as e:
            code = _model_load_code(e)
            _msg, _hint, _hint_en = _classify_model_load_error(e, _rag_models.EMBEDDER_REPO)
            report_fault(
                Cap.KB_VECTOR_SEARCH, code,
                user_message=_msg, hint=_hint, hint_en=_hint_en,
                detail=f"{type(e).__name__}: {e}",
            )
            raise
        logger.debug("[RAG] 嵌入模型加载完成 (BAAI/bge-m3)")
        _init_stage_log.append("embedder_ready")
        report_ok(Cap.KB_VECTOR_SEARCH, note="embedder loaded")
    return _embedder


def _model_load_code(e: Exception) -> str:
    """把模型加载异常归成【稳定指纹】。

    不能用完整异常字符串做指纹——路径、内存地址、下载进度都会变，同一根因会被识别成
    多个故障，去重直接失效。所以这里只映射成有限的几个 code。
    """
    if isinstance(e, _rag_models.ModelUnavailable):
        return "MODEL_FETCH_OFFLINE"
    s = f"{type(e).__name__}: {e}".lower()
    # ⚠️ 不要匹配裸的 "safetensors" —— 那是**权重文件的文件名**，
    #    几乎每条权重相关报错都带它（「找不到 model.safetensors」也会命中）。
    #    📌 匹配要抓的是**故障信号**，不是恰好同名的东西。
    if "torch.load" in s or "cve" in s or "upgrade torch" in s             or "requires safetensors" in s or "use_safetensors" in s:
        return "WEIGHTS_FORMAT_REJECTED"
    if "offline" in s or "couldn't connect" in s or "connection" in s:
        return "MODEL_FETCH_OFFLINE"
    # ⚠️ 内存类失败必须排在 OSError 之前 —— **顺序即逻辑**。
    #    Windows 1455 = ERROR_COMMITMENT_LIMIT（提交内存到顶），
    #    1450 = ERROR_NO_SYSTEM_RESOURCES，8 = ERROR_NOT_ENOUGH_MEMORY。
    #    模型有 2.27GB，`safe_open` 做内存映射时提交不下来就是这几个码。
    #    🔴 2026-08-31 实测：这条被归成了「文件缺失」，而文件是完整的。
    if isinstance(e, MemoryError) or getattr(e, "winerror", None) in (8, 1450, 1455)             or "页面文件太小" in f"{e}" or "commitment limit" in s             or "not enough memory" in s or "paging file" in s or "cannot allocate" in s:
        return "MODEL_LOAD_OOM"
    # ⚠️ 收窄到**真的指向"文件不在/不全"**的证据。
    #    🔴 原来这里写 `isinstance(e, OSError)` —— 那是 Windows 上几乎所有 I/O
    #       失败的基类（内存、磁盘满、权限），全被报成"模型没下全"。
    #    📌 **一个笼统的异常类型不能当成一个具体的诊断。**
    if (isinstance(e, FileNotFoundError)
            or getattr(e, "errno", None) == 2
            or "not a local folder" in s or "no such file" in s
            or "incomplete" in s or "corrupt" in s):
        return "MODEL_MISSING"
    # 认不出就如实说不知道 —— 宁可"去看日志"，也不给一个具体但错误的原因。
    return "MODEL_LOAD_FAILED"


def _classify_model_load_error(e: Exception, repo: str) -> tuple[str, str, str]:
    """异常 → (给用户/模型看的一句话, 恢复建议·中文, 恢复建议·英文)。

    ⚠️ 恢复建议**两份不是冗余**：中文那份进界面的故障卡片（跟随 UI 语言），
       英文那份注入模型（注入模型的文本一律英文）。
       📌 「一个字段不许表达两个现实」—— 这里的两个现实是两个受众。
    """
    code = _model_load_code(e)
    return {
        "WEIGHTS_FORMAT_REJECTED": (
            f"知识库检索不可用：{repo} 的权重是 .bin 格式，当前 transformers 因安全公告拒绝加载。",
            "重新启动 Nano，触发自动修复程序。",
            "Restart Nano to trigger the automatic repair.",
        ),
        "MODEL_FETCH_OFFLINE": (
            f"RAG 模型下载失败：{repo}。",
            "检查网络或尝试启动代理后重启 Nano。",
            f"Failed to download RAG model: {repo}. Check your network or enable a proxy and restart Nano.",
        ),
        "MODEL_LOAD_OOM": (
            f"知识库检索不可用：加载 {repo} 时系统内存不足（模型约 2.3 GB）。"
            "文件本身是完好的，不需要重新下载。",
            "关掉占内存的程序后重启 Nano；仍然失败的话，调大 Windows 的虚拟内存"
            "（页面文件）再试。",
            "Close memory-heavy programs and restart Nano. If it still fails, increase "
            "the Windows paging file size. The model files are intact - do NOT re-download.",
        ),
        "MODEL_MISSING": (
            f"知识库检索不可用：{repo} 模型文件缺失或未下载完整。",
            "重新启动 Nano，触发自动修复程序。",
            "Restart Nano to trigger the automatic repair.",
        ),
    }.get(code, (
        f"知识库检索不可用：{repo} 加载失败。",
        "查看运行日志里的 [RAG] 段落获取详细报错。",
        "Check the [RAG] section of the runtime log for the full error.",
    ))



# ══════════════════════════════════════════════
# BM25 关键词检索（Step 5.5）
# ══════════════════════════════════════════════

def _check_bm25_available() -> bool:
    """检测 rank_bm25 + jieba 是否可用。任一缺失则降级为纯向量检索。"""
    global _bm25_available
    if _bm25_available is not None:
        return _bm25_available
    try:
        import rank_bm25  # noqa
        import jieba
        import logging as _logging
        # jieba 首次分词时向 stderr 打印词典加载信息，对用户没有意义。
        jieba.setLogLevel(_logging.WARNING)
        _bm25_available = True
        logger.debug("[RAG] BM25 + jieba 可用，启用 hybrid search")
        _init_stage_log.append("bm25_ready")
    except ImportError as e:
        _bm25_available = False
        logger.warning(f"⚠️ [RAG] BM25 依赖缺失（{e}），降级为纯向量检索。装包：pip install rank_bm25 jieba")
        _init_stage_log.append("bm25_unavailable")
        # 降级不进聊天区（不打扰），但必须让监控面板说真话 ——
        # 否则这种"还能用但质量下降"的情况不翻日志一辈子发现不了。
        from core.health import Cap, report_degraded
        report_degraded(
            Cap.KB_KEYWORD_SEARCH, "BM25_DEPS_MISSING",
            user_message="知识库只剩向量检索，关键词召回这一路缺失，精确词/专有名词的命中率会下降。",
            hint="pip install rank_bm25 jieba",
            hint_en="Install the missing packages: `pip install rank_bm25 jieba`, then restart Nano.",
            detail=f"{type(e).__name__}: {e}",
        )
    return _bm25_available


def _tokenize_zh(text: str) -> List[str]:
    """中文分词。jieba 精确模式 + 简单清洗（去空白、去单字符标点）。"""
    import jieba
    tokens = []
    for t in jieba.cut(text):
        t = t.strip()
        # 跳过空字符串、单字符纯标点
        if not t:
            continue
        if len(t) == 1 and not t.isalnum():
            # 跳过逗号、句号等单字符标点（中英文都覆盖）
            if not ('\u4e00' <= t <= '\u9fff'):  # 但单字符中文保留（中文里"权""归"这类单字也有意义）
                continue
        tokens.append(t.lower())
    return tokens


def _build_bm25_index():
    """从 chroma 全量拉所有 chunks，构建 BM25 内存索引。

    BM25 索引不持久化（每次启动重建）：
    - 重建很快（几千 chunks 几秒钟）
    - 避免索引文件跟 chroma 不同步带来的诡异 bug
    - jieba 分词词典本身就在内存里
    """
    if not _check_bm25_available():
        _set_bm25([], None)
        return

    from rank_bm25 import BM25Okapi

    collection = _get_collection()
    persist_total = collection.count()
    temp_total = _temp_collection.count() if _temp_collection is not None else 0

    # 持久库和临时库都为空才跳过（与 search() 早退逻辑对齐）。
    # 关键：只要临时库有内容，即使持久库为空也要建索引，
    # 否则"只上传临时文件、没有持久知识库"时临时库永远进不了 BM25。
    if persist_total == 0 and temp_total == 0:
        _set_bm25([], None)
        logger.info("[RAG] BM25 索引：持久库和临时库均为空，跳过")
        return

    corpus = []
    tokenized = []

    # 1. 持久库 chunks
    if persist_total > 0:
        raw = collection.get(include=["documents", "metadatas"])
        for cid, doc, meta in zip(raw.get("ids", []), raw.get("documents", []), raw.get("metadatas", [])):
            tokens = _tokenize_zh(doc)
            corpus.append({
                "id": cid,
                "content": doc,
                "filename": meta.get("filename", ""),
                "source": meta.get("source", ""),
                "tokens": tokens,
            })
            tokenized.append(tokens)

    # 2. 临时库 chunks（filename 加 [临时] 前缀，与 search() 向量路径标记一致，
    #    保证 RRF 融合时同一 chunk 的 BM25 路径和向量路径能正确合并去重）
    if temp_total > 0:
        try:
            temp_raw = _temp_collection.get(include=["documents", "metadatas"])
            for cid, doc, meta in zip(temp_raw.get("ids", []), temp_raw.get("documents", []), temp_raw.get("metadatas", [])):
                tokens = _tokenize_zh(doc)
                corpus.append({
                    "id": cid,
                    "content": doc,
                    "filename": f"[临时]{meta.get('filename', '')}",
                    "source": meta.get("source", ""),
                    "tokens": tokens,
                    "is_temp": True,
                })
                tokenized.append(tokens)
        except Exception as e:
            logger.warning(f"[RAG] BM25 纳入临时库 chunks 失败（跳过临时部分）: {e}")

    if not corpus:
        _set_bm25([], None)
        logger.info("[RAG] BM25 索引：拉取后无有效 chunk，跳过")
        if "bm25_index_built" not in _init_stage_log:
            _init_stage_log.append("bm25_index_built")
        return

    # ⭐ 关键的一处：**先把索引构造完，再一次性换掉这一对**。
    #    改造前是 `_bm25_corpus = corpus` 然后 `_bm25_index = BM25Okapi(...)` ——
    #    而第二句耗时，中间那段时间里语料已经是新的、索引还是旧的。
    #    📌 **两个必须一起变的东西，如果分两句写，那两句之间就是一个可观察的错误状态。**
    _new_index = BM25Okapi(tokenized)   # 先构造（慢），此时全局还是旧的一对
    _set_bm25(corpus, _new_index)       # 再原子换（快）
    logger.debug(f"[RAG] BM25 索引构建完成（{len(corpus)} chunks，含临时 {temp_total}）")
    if "bm25_index_built" not in _init_stage_log:
        _init_stage_log.append("bm25_index_built")


def _bm25_search(query: str, top_k: int, source: str = "both") -> List[Dict[str, Any]]:
    """BM25 检索。返回 [{content, filename, source, score, id}, ...]。

    source: "both" | "temp_only" | "persist_only"
      - "temp_only"    : 只返回 is_temp=True 的 chunk（上传文件）
      - "persist_only" : 只返回 is_temp 不为 True 的 chunk（持久知识库）
      - "both"         : 全部返回（原有行为）

    懒加载：如果当前进程的 _bm25_index 为 None（可能是 NiceGUI worker 进程
    没经过 _init_rag_async 路径），自动触发一次构建。
    """
    global _bm25_index, _bm25_corpus
    if _bm25_index is None or not _bm25_corpus:
        try:
            _build_bm25_index()
        except Exception as e:
            logger.warning(f"[RAG] BM25 索引懒加载失败: {e}")
            return []
        if _bm25_index is None or not _bm25_corpus:
            logger.debug("[RAG] BM25 懒加载后仍为空，返回空结果")
            return []

    query_tokens = _tokenize_zh(query)
    if not query_tokens:
        return []

    # ⭐ **一次取出这一对，之后只用局部量。**
    #    改造前读侧也是两次独立读：先 `_bm25_index.get_scores()` 拿下标，
    #    再 `_bm25_corpus[idx]` 取内容 —— 中间如果重建插进来，
    #    下标就落到另一份语料上。
    #    📌 **写侧原子还不够，读侧也得一次取齐** ——
    #       否则原子写只是把窗口从「写的中间」挪到了「读的中间」。
    _corpus, _index = _bm25_pair
    if _index is None or not _corpus:
        return []

    scores = _index.get_scores(query_tokens)
    import numpy as np
    top_idx = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_idx:
        s = float(scores[idx])
        if s <= 0:
            continue
        c = _corpus[idx]      # ⚠️ 用上面取齐的那一份，不再读全局
        is_temp = c.get("is_temp", False)
        # 按 source 过滤
        if source == "temp_only" and not is_temp:
            continue
        if source == "persist_only" and is_temp:
            continue
        results.append({
            "content": c["content"],
            "filename": c["filename"],
            "source": c["source"],
            "score": s,
            "id": c["id"],
        })
    return results


def _rrf_fuse(rankings: List[List[Dict[str, Any]]], k: int = RRF_K) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion：把多个检索结果按排名（不是分数）融合。

    每个 chunk 的 RRF 分数 = sum(1 / (k + rank_in_each_ranking))
    rank 从 1 开始计数。

    这是个非常聪明的融合算法：
    - 不需要对齐不同检索器的分数尺度（向量 0-1，BM25 0-10+）
    - 只看排名，自动给"在多个排名里都出现"的 chunk 加分
    - k 控制衰减速度，60 是业界默认值
    """
    # 用 chunk 内容前 200 字 + filename 作为去重 key（chroma id 在两路里都有但更稳）
    score_map: Dict[str, float] = {}
    chunk_map: Dict[str, Dict[str, Any]] = {}

    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            # 用 (filename, content 前 200 字) 作为唯一 key
            key = f"{item.get('filename', '')}||{item.get('content', '')[:200]}"
            contribution = 1.0 / (k + rank)
            score_map[key] = score_map.get(key, 0.0) + contribution
            # 保留第一次见到的 chunk 数据（不同 ranking 的 score 字段不同，统一记录就行）
            if key not in chunk_map:
                chunk_map[key] = dict(item)

    # 按 RRF 分数降序
    sorted_keys = sorted(score_map.keys(), key=lambda k: score_map[k], reverse=True)
    output = []
    for key in sorted_keys:
        item = chunk_map[key]
        item["rrf_score"] = round(score_map[key], 6)
        output.append(item)
    return output


# ══════════════════════════════════════════════
# Reranker 重排（Step 5.5++）
# ══════════════════════════════════════════════

# Reranker 全局单例
_reranker = None
_reranker_available: Optional[bool] = None


def _check_reranker_available() -> bool:
    """检测 sentence-transformers 的 CrossEncoder 是否可用。

    设计选择：原本用 FlagEmbedding.FlagReranker，但它对 transformers 版本依赖太脆
    （新版 transformers 移除了 XLMRobertaTokenizer.prepare_for_model 这个旧 API），
    会触发 'XLMRobertaTokenizer has no attribute prepare_for_model' 错误。
    sentence-transformers.CrossEncoder 是同一个库内的 API（你装 bge-m3 时已经有了），
    没有版本冲突风险，且 API 同样简洁。
    """
    global _reranker_available
    if _reranker_available is not None:
        return _reranker_available
    try:
        from sentence_transformers import CrossEncoder  # noqa
        _reranker_available = True
        logger.debug("[RAG] CrossEncoder 可用，启用语义重排")
    except ImportError as e:
        _reranker_available = False
        logger.warning(f"⚠️ [RAG] CrossEncoder 依赖缺失({e})，降级为 hybrid 检索")
        from core.health import Cap, report_degraded
        report_degraded(
            Cap.KB_RERANKER, "RERANKER_DEPS_MISSING",
            user_message="知识库重排不可用，检索结果保持 RRF 原始顺序，长问句里的关键片段可能排不上来。",
            hint="pip install sentence-transformers",
            hint_en="Install it: `pip install sentence-transformers`, then restart Nano.",
            detail=f"{type(e).__name__}: {e}",
        )
    return _reranker_available


def _load_reranker():
    """懒加载 bge-reranker-v2-m3 模型（首次约 30 秒）。

    用 sentence-transformers 的 CrossEncoder 加载，跟 bge-m3 在同一个库里，
    不依赖 FlagEmbedding，规避 transformers 版本冲突。
    """
    global _reranker
    if _reranker is not None:                # 快路径：已加载，不进锁
        return _reranker
    if not _check_reranker_available():
        return None

    from core.health import Cap, report_degraded, report_ok
    from core import crash_journal
    with _reranker_init_lock:                # 同 _load_embedder，见模块头
        if _reranker is not None:
            return _reranker
        try:
            with crash_journal.breadcrumb(
                "rag.load_reranker",
                capability=Cap.KB_RERANKER,
                detail="CrossEncoder('BAAI/bge-reranker-v2-m3')",
            ):
                from sentence_transformers import CrossEncoder
                # bge-reranker-v2-m3：智源 2024 年发布的多语言 cross-encoder
                # max_length=512：bge-reranker-v2-m3 的最大输入长度
                _reranker = _build_model(
                    _rag_models.RERANKER_REPO,
                    lambda path: CrossEncoder(path, max_length=512))
        except Exception as e:
            # 重排缺失只是质量下降（保持 RRF 原序），不是失能 → degraded 不是 fault。
            # 但仍要登记，否则用户永远不知道检索质量为什么变差了。
            _msg, _hint, _hint_en = _classify_model_load_error(e, "BAAI/bge-reranker-v2-m3")
            report_degraded(
                Cap.KB_RERANKER, _model_load_code(e),
                user_message=_msg.replace("知识库检索不可用", "知识库重排不可用（检索仍可用，质量下降）"),
                hint=_hint, hint_en=_hint_en, detail=f"{type(e).__name__}: {e}",
            )
            logger.warning(f"⚠️ [RAG] Reranker 加载失败，降级为 hybrid 检索: {e}")
            return None
        logger.debug("[RAG] 重排模型加载完成 (bge-reranker-v2-m3)")
        report_ok(Cap.KB_RERANKER, note="reranker loaded")
    return _reranker


def _rerank(query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """用 CrossEncoder 对候选 chunks 重新打分。

    工作原理：
    - 跟 bi-encoder（向量检索）不同，cross-encoder 把 query 和 chunk 一起送进模型
    - 模型同时看两边，能判断"这个 chunk 在语义上是否真的回答了 query"
    - 解决了 hybrid search 里"修饰词淹没真正相关 chunk"的问题

    输入：candidates 列表，每个 chunk 必须有 'content' 字段
    输出：按 rerank_score 降序的列表，每个 chunk 多了 'rerank_score' 字段
    """
    if not candidates:
        return []
    if not _check_reranker_available():
        return candidates  # 降级：reranker 不可用就维持原顺序

    reranker = _load_reranker()
    if reranker is None:
        return candidates

    try:
        # CrossEncoder.predict 接受 (query, doc) 对列表，返回每对的相关性分数
        pairs = [[query, c.get("content", "")] for c in candidates]
        scores = reranker.predict(pairs)
        # scores 是 numpy array
        scores = [float(s) for s in scores]

        # 把分数挂到候选上
        for c, s in zip(candidates, scores):
            c["rerank_score"] = round(s, 6)

        # 按 rerank_score 降序
        candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        return candidates
    except Exception as e:
        logger.warning(f"[RAG] rerank 执行异常，跳过重排: {e}")
        return candidates


# ══════════════════════════════════════════════════════════════════════════
# 向量库的损坏检测与自愈
# ══════════════════════════════════════════════════════════════════════════
# 自愈之所以安全，是因为**向量库是派生物**：原始文件全在 data/knowledge/，
# 删掉 chroma_db 只损失"已编码"这个中间结果，重新 encode 即可完全还原。
# 这跟"用户文件"性质不同，所以适用自动修复而不是先问用户。
#
# ⚠️ 但只对【结构性不可用】自愈。普通查询失败、单文件解析失败一律不许触发重建——
# 那些是局部问题，重建是核弹。
# ══════════════════════════════════════════════════════════════════════════

# 本进程是否已经自愈过一次。防的是"环境本身就坏"时无限重建：
# 每次打开失败 → 重建 → 又失败 → 又重建，把用户的库反复搬走。
_chroma_healed_once = False
_orphan_cleanup_done = False


def _is_structural_chroma_failure(e: Exception) -> bool:
    """判断这个异常是不是"库结构性不可用"，即值得整库重建。

    收窄而不是放宽：宁可漏判（用户手动删一次），也不要误判（把好库搬走）。
    """
    s = f"{type(e).__name__}: {e}".lower()
    markers = (
        "tenant",                      # 旧版本 schema：Could not connect to tenant default_tenant
        "schema",
        "migration",
        "no such table",               # sqlite 表缺失
        "database disk image is malformed",
        "file is not a database",
        "unable to open database",
        "database is locked",          # 少见，但重建能解
        "unsupported version",
    )
    return any(m in s for m in markers)


def _reset_chromadb_process_state() -> None:
    """尽力清掉 chromadb 的进程级缓存，好让同进程重开有机会成功。

    ⚠️ 实测不稳定（清了也常常还是 `AttributeError: bindings`），所以调用方
    **不能依赖它成功**，只当作"顺手试一下"。真正的保底是下次启动。
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient as _S
        if hasattr(_S, "_identifier_to_system"):
            _S._identifier_to_system.clear()
    except Exception:
        pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def _backup_and_reset_chroma() -> Optional[str]:
    """把损坏的向量库整体搬走，让下次打开时从零重建。返回备份路径（失败返回 None）。

    ⚠️ 必须连带清空 indexed_hashes.json，否则会踩一个**静默**的坑：
    `_index_one_file` 的增量判据是 `hash_store[path] == 当前hash and path in parse_reports`，
    两者都还在的话，重建后的空库会把每个文件都判成 "skipped" ——
    **向量库永远是空的，而且不报任何错**，用户只会觉得"知识库突然搜不到东西了"。
    """
    import shutil, time as _t
    try:
        src = pathlib.Path(CHROMA_DIR)
        if not src.exists():
            return None
        dst = src.with_name(f"{src.name}.corrupt-{_t.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(str(src), str(dst))
        logger.warning(f"[RAG] 向量库已搬走：{src.name} → {dst.name}（原始文件未动，将自动重建）")

        # 清增量哈希——理由见 docstring，这一步漏了，自愈会"成功"但库是空的
        try:
            if os.path.exists(HASH_STORE):
                os.remove(HASH_STORE)
                logger.info("[RAG] 已清空 indexed_hashes.json，重建后会全量重新索引")
        except Exception as _e:
            logger.warning(f"[RAG] 清 indexed_hashes.json 失败（重建后可能索引不全）: {_e}")

        # 只保留最近 3 份损坏备份，避免反复自愈把磁盘吃满
        try:
            backups = sorted(src.parent.glob(f"{src.name}.corrupt-*"))
            for old in backups[:-3]:
                shutil.rmtree(old, ignore_errors=True)
                logger.info(f"[RAG] 清理旧的损坏备份: {old.name}")
        except Exception:
            pass
        return str(dst)
    except Exception as e:
        logger.error(f"[RAG] 搬走损坏向量库失败: {e}")
        return None


def _cleanup_orphan_segments() -> int:
    """删除 chroma_db 下不在 segments 表里的 UUID 目录。返回清理数量。

    每次重建都会留一个孤儿：2026-08-03 实测 data/chroma_db/ 下有两个体积相同的
    segment 目录，查 chroma.sqlite3 的 segments 表确认只有一个挂在活的 collection 上，
    另一个是上一次重建的遗留。判据是确定性的——**不在 segments 表里就是孤儿**，
    不靠时间戳或体积猜。
    """
    import sqlite3, shutil, re as _re
    try:
        base = pathlib.Path(CHROMA_DIR)
        db = base / "chroma.sqlite3"
        if not db.exists():
            return 0
        con = sqlite3.connect(str(db))
        try:
            live = {row[0] for row in con.execute("SELECT id FROM segments").fetchall()}
        finally:
            con.close()
        if not live:
            # 读不到活 segment 时什么都不删——宁可留着垃圾，不冒误删的风险
            return 0
        _uuid = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I)
        removed = 0
        for p in base.iterdir():
            if p.is_dir() and _uuid.match(p.name) and p.name not in live:
                shutil.rmtree(p, ignore_errors=True)
                logger.info(f"[RAG] 清理孤儿 segment 目录: {p.name}")
                removed += 1
        return removed
    except Exception as e:
        logger.warning(f"[RAG] 孤儿 segment 清理跳过: {e}")
        return 0


def _probe_kb_store() -> bool:
    """KB_STORE 的存活探针：能不能打开向量集合并读到计数。

    廉价（chroma 已建好时是内存里的快路径）、只读、且**绕过能力闸**——
    它就是来解闸的。用户手动修好库、或换了个好的 data/ 目录之后，
    不需要重启也不需要发消息，这个探针会自己发现并恢复能力。
    """
    try:
        return _get_collection().count() >= 0
    except Exception:
        return False


def _probe_embedder() -> bool:
    """KB_VECTOR_SEARCH 的探针。

    ⚠️ 刻意【不】在这里加载模型——加载 bge-m3 要 2.27 GB、几十秒，
    放进每 N 秒一跳的探针里是灾难。只检查"模型已经加载好了"这个事实。
    真正的恢复由下一次实际检索触发，探针只负责在那之后把状态改回来。
    """
    return _embedder is not None


def _probe_reranker() -> bool:
    """KB_RERANKER 的探针。同 `_probe_embedder`：**只看事实，不在探针里加载模型。**

    ⚠️ 它比 embedder 那个更容易被误判成"不需要"：重排是 DEGRADED 不是
       UNAVAILABLE，工具没被下架，所以下一次检索**确实**会重试加载。
       但"下一次检索"可能永远不来（用户被降级劝退、或那几天根本没查知识库），
       状态就一直挂着。📌 探针的价值不在"能不能重试"，在**不依赖业务路径**。
    """
    return _reranker is not None


def _probe_bm25() -> bool:
    """KB_KEYWORD_SEARCH 的探针：重新看一眼两个包在不在。

    🔴 **不能调 `_check_bm25_available()`** —— 它把结果缓存在 `_bm25_available`，
       第一次 False 之后永远返回 False。那会是一个**看起来有、永远解不了闸的探针**，
       比没有探针更坏（缺口看起来已经补上了）。
    ⭐ 装上了就顺手把缓存清掉：否则探针报了"恢复"，而实际检索路径仍然
       走纯向量 —— **那句"恢复了"就是假话**。探针必须让它说的事真的成立。
    """
    global _bm25_available
    import importlib.util
    try:
        ok = all(importlib.util.find_spec(m) is not None for m in ("rank_bm25", "jieba"))
    except Exception:
        return False
    if ok and _bm25_available is False:
        _bm25_available = None
    return ok


def _register_health_probes() -> None:
    try:
        from core.health import get_health, Cap
        h = get_health()
        h.register_probe(Cap.KB_STORE, _probe_kb_store)
        h.register_probe(Cap.KB_VECTOR_SEARCH, _probe_embedder)
        h.register_probe(Cap.OCR_TESSERACT, lambda: bool(_resolve_tesseract_cmd()))
        # ⚠️ 这两条补齐的是「能力坏了但没人上报」那一类：
        #    reranker 与 BM25 都会静默降级，不注册探针就永远看不见。
        h.register_probe(Cap.KB_RERANKER, _probe_reranker)
        h.register_probe(Cap.KB_KEYWORD_SEARCH, _probe_bm25)
    except Exception as _e:
        logger.debug(f"[RAG] 健康探针登记跳过: {_e}")


_reindex_after_heal_started = False


def _schedule_reindex_after_heal(backup_path: str) -> None:
    """自愈重建后触发一次全量索引（后台线程，不阻塞调用方）。

    为什么要单独调度：自愈可能发生在【任何】打开向量库的时刻。
    - 若发生在启动索引里（`_init_rag_async` → `index_documents`），调用方拿到空库后
      会自己把所有文件重新索引一遍，这里其实不必再做；
    - 但若发生在一次普通查询里（用户搜东西时才第一次打开库），**没有任何人会去重建索引**，
      用户会得到一个永远搜不到东西的空库。
    所以这里无条件补一次，靠 `_reindex_after_heal_started` 保证每进程只补一次；
    真的重复了也无害——`index_documents` 本身是幂等的（哈希增量）。
    """
    global _reindex_after_heal_started
    if _reindex_after_heal_started:
        return
    _reindex_after_heal_started = True

    def _run():
        try:
            logger.info("[RAG] 自愈后重建索引开始（后台）")
            stats = index_documents("data/knowledge")
            logger.info(
                f"[RAG] 自愈后重建索引完成 → 新增 {stats.get('indexed', 0)}，"
                f"错误 {len(stats.get('errors', []))}"
            )
            try:
                from core.health import get_system_events
                get_system_events().add(
                    f"Knowledge base re-indexed after automatic repair: "
                    f"{stats.get('indexed', 0)} file(s) restored."
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[RAG] 自愈后重建索引失败: {e}")
            try:
                from core.health import report_degraded, Cap
                report_degraded(
                    Cap.KB_VECTOR_SEARCH, "REINDEX_AFTER_HEAL_FAILED",
                    user_message="向量库已重建，但重新索引没跑完，知识库内容可能不全。",
                    hint=f"旧库备份在 {backup_path}；可在知识库面板重新上传文件触发索引。",
                    hint_en=("The old store was backed up; re-uploading a file in the "
                             "knowledge panel triggers a fresh index run."),
                    detail=str(e),
                )
            except Exception:
                pass

    threading.Thread(target=_run, name="nano-rag-heal-reindex", daemon=True).start()


def _get_collection():
    """打开（或复用）向量集合。

    ⚠️ 必须持锁构造 —— 2026-08-03 实测抓到的竞态，日志证据：
        17:59:44.895 INFO  _get_collection ✔ 向量集合就绪，当前块数: 18
        17:59:44.899 ERROR _get_collection   打开向量库失败
    同一个函数 4 毫秒内一次成功一次失败，因为 rag-init 后台线程和 UI 线程同时进入了
    裸的 `if _collection is None:`，各自去 new 一个 PersistentClient。chromadb 有进程级
    共享状态，后进来的线程撞上半初始化的对象：

        AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
          ↓ 被 chromadb 的 _validate_tenant_database 捕获后重新抛出
        ValueError: Could not connect to tenant default_tenant. Are you sure it exists?

    注意第二行那个"tenant"报错是**误导性外壳**，真正的根因是上面那个 AttributeError。
    这个竞态大概率一直存在，只是以前被 _init_rag_async 那句
    `except Exception: logger.warning("后台初始化失败（跳过）")` 吞掉了——赢的线程已经
    把 _collection 设成合法对象，RAG 实际能用，所以没人发现。
    """
    global _chroma_client, _collection
    if _collection is not None:              # 快路径：已建好，不进锁
        return _collection

    from core.health import Cap, report_fault, report_ok
    from core import crash_journal
    with _chroma_init_lock:
        if _collection is not None:          # 双重检查：等锁期间别人已经建好了
            return _collection
        global _chroma_healed_once, _orphan_cleanup_done

        def _open_once():
            """真正打开一次。抽出来是为了自愈之后能原样重试，不复制两份逻辑。"""
            global _chroma_client, _collection
            with crash_journal.breadcrumb(
                "rag.open_chroma",
                capability=Cap.KB_STORE,
                detail=f"chromadb.PersistentClient({CHROMA_DIR})",
            ):
                import chromadb
                from chromadb.config import Settings as _ChromaSettings
                os.makedirs(CHROMA_DIR, exist_ok=True)
                # chromadb 1.x 默认走 Rust bindings，性能最佳。
                # 历史注释：早期版本曾尝试 SegmentAPI 作为 Windows + Python 3.14 的 workaround，
                # 但在 chromadb 1.5+ 里 SegmentAPI 已被移除，且需要 hnswlib 依赖。
                # 现在直接用默认 PersistentClient，前提是装了 VC++ Redistributable。
                # anonymized_telemetry=False：chromadb 默认会向官方发送匿名使用统计，
                # Nano 承诺不上报任何数据，故显式关闭。
                _chroma_client = chromadb.PersistentClient(
                    path=CHROMA_DIR,
                    settings=_ChromaSettings(anonymized_telemetry=False),
                )
                _collection = _chroma_client.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"}
                )

        _healed_from = None
        try:
            _open_once()
        except Exception as e:
            import traceback as _tb
            logger.error(
                f"[RAG] 打开向量库失败 path={CHROMA_DIR}\n"
                f"      异常: {type(e).__name__}: {e}\n"
                f"{_tb.format_exc()}"
            )
            _s = f"{type(e).__name__}: {e}".lower()
            _incompat = "tenant" in _s or "schema" in _s or "migration" in _s

            # ── 自愈：只对【结构性不可用】动手，且每进程只做一次 ──────────
            # ⚠️ 给后人的提醒：那句 tenant 报错【曾经有两个完全不同的成因】。
            # 一个是 schema 不兼容（真故障），另一个是并发构造竞态（假故障，库其实好好的）。
            # 竞态已经用双重检查锁根治（见本函数 docstring），所以现在看到 tenant 报错
            # 基本可以按 schema 不兼容处理——但如果哪天又出现"删库重建也不好、且时好时坏"，
            # 先回来看看是不是又冒出了新的并发入口，**别再急着重建**。
            if _is_structural_chroma_failure(e) and not _chroma_healed_once:
                _chroma_healed_once = True
                logger.warning("[RAG] 判定为结构性损坏，启动自愈：备份旧库 → 重建 → 全量重新索引")
                _healed_from = _backup_and_reset_chroma()
                if _healed_from:
                    try:
                        _reset_chromadb_process_state()
                        _open_once()
                        logger.info("[RAG] 自愈成功：本进程内已重建空库，随后会全量重新索引")
                    except Exception as e2:
                        # ⚠️ 这是常态而非异常，不要当成"自愈失败"报红。
                        # 实测（2026-08-04）：chromadb 一旦在本进程内打开失败过，
                        # **即使把库删干净，同进程再开仍然抛 `AttributeError: bindings`**。
                        # 它内部有跨调用的进程级状态，清 SharedSystemClient 缓存也不稳定管用。
                        # 这正是历史上"删掉 chroma_db 必须重启才好"的机制解释。
                        #
                        # 所以这里不硬撑：**有价值的部分（备份 + 清哈希）已经做完了**，
                        # 下次启动是全新进程，会看到一个不存在的库 → 自动建新的 → 全量索引。
                        # 报 RECOVERING 而不是 UNAVAILABLE：问题已定位、修复已就绪、只差一次重启。
                        logger.warning(
                            f"[RAG] 本进程内无法重开（chromadb 进程级状态所限，属预期）："
                            f"{type(e2).__name__}: {e2} —— 重启后会自动重建"
                        )
                        # ⚠️⚠️ `Status` / `Severity` **本模块没导入** ——
                        #    上面那处 `from core.health import` 只导了 `get_health, Cap`。于是这条上报
                        #    每次都抛 `NameError`，被外层 except 吞掉：
                        #    🔴 **向量库损坏并被自动清理这件事，用户永远看不到**
                        #       （那句 `user_message` 是写给 UI 的），
                        #       而 Nano 也不知道 `KB_STORE` 处于
                        #       「已清理、等重启重建」这一档。
                        #    📌 **一条「出事时才走」的路径上的错误，
                        #       只会在出事的时候暴露 —— 也就是最不该再出错的时候。**
                        # ⚠️⚠️ **`get_health` 本身也不在这个作用域里** ——
                        #    这个函数开头导的是 `Cap, report_fault, report_ok`。
                        # 🔴 上一个补丁只补了 `Status` / `Severity`，那一行**照样**
                        #    NameError。
                        # 📌 **补一行里缺的名字时，要把那一行里的每个名字都过一遍** ——
                        #    只补「报错说缺的那个」，下一次运行会报下一个；
                        #    而这里根本不会有「下一次」，因为整行被 except 吞了。
                        # ⭐ 这一处是**检查器扫出来的，不是人看出来的** —— 正好证明了
                        #    「先让工具在已知错上过关、然后信它的新发现」这个顺序的价值。
                        from core.health import get_health, Status, Severity
                        get_health().report(
                            Cap.KB_STORE,
                            status=Status.RECOVERING, severity=Severity.WARNING,
                            code="CHROMA_HEALED_PENDING_RESTART",
                            user_message=(
                                "知识库向量库损坏，已自动备份并清理。"
                                "重启 Nano 后会自动重建，原始文件一个都没动。"
                            ),
                            recovery_hint=f"损坏的旧库备份在 {_healed_from}，确认没问题后可以删掉。",
                            recovery_hint_en=("Restart Nano to rebuild the store. The corrupt "
                                              "one was backed up and can be deleted afterwards."),
                            technical_detail=f"reopen in-process failed: {type(e2).__name__}: {e2}",
                        )
                        try:
                            from core.health import get_system_events
                            get_system_events().add(
                                "Knowledge base vector store was corrupt; it has been backed up and cleared. "
                                "It will rebuild automatically on the next restart. Source files are untouched. "
                                "Tell the user this plainly if they ask why search is unavailable."
                            )
                        except Exception:
                            pass
                        raise
                else:
                    report_fault(
                        Cap.KB_STORE, "CHROMA_BACKUP_FAILED",
                        user_message="知识库向量库打不开，且备份旧库失败，未做自动重建。",
                        hint="请检查 data/chroma_db/ 的读写权限。",
                        hint_en="Check read/write permissions on the `data/chroma_db/` folder.",
                        detail=f"{type(e).__name__}: {e}",
                    )
                    raise
            else:
                report_fault(
                    Cap.KB_STORE,
                    "CHROMA_SCHEMA_INCOMPATIBLE" if _incompat else "CHROMA_OPEN_FAILED",
                    user_message=(
                        "知识库向量库打不开：文件是旧版本 ChromaDB 建的，与当前版本不兼容。"
                        if _incompat else "知识库向量库打不开。"
                    ),
                    hint=(
                        "向量库是派生物，原始文件还在 data/knowledge/，删掉 data/chroma_db/ "
                        "重启会自动重建，不丢数据。"
                        if _incompat else "查看运行日志里的 [RAG] 段落获取详细报错。"
                    ),
                    hint_en=(
                        "The vector store is derived data - the original files are still in "
                        "`data/knowledge/`. Deleting `data/chroma_db/` and restarting rebuilds "
                        "it with no data loss."
                        if _incompat else
                        "Check the [RAG] section of the runtime log for the full error."
                    ),
                    detail=f"{type(e).__name__}: {e}",
                )
                raise

        logger.debug(f"[RAG] 向量集合就绪，当前块数: {_collection.count()}")
        report_ok(Cap.KB_STORE, note="chroma opened")

        # 每次重建都会留一个孤儿 segment 目录，开库成功后顺手清一次（每进程一次）
        if not _orphan_cleanup_done:
            _orphan_cleanup_done = True
            _n = _cleanup_orphan_segments()
            if _n:
                logger.info(f"[RAG] 启动清理：移除 {_n} 个孤儿 segment 目录")

        # 自愈发生过 → 通过故障上报的出口告诉用户，并触发全量重建索引
        if _healed_from:
            try:
                from core.health import get_system_events
                get_system_events().add(
                    "Knowledge base vector store was corrupt and has been rebuilt automatically. "
                    "Source files were untouched; re-indexing runs in the background."
                )
            except Exception:
                pass
            _schedule_reindex_after_heal(_healed_from)

    return _collection


def _load_hash_store() -> Dict[str, str]:
    if os.path.exists(HASH_STORE):
        with open(HASH_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_hash_store(store: Dict[str, str]):
    os.makedirs(os.path.dirname(HASH_STORE), exist_ok=True)
    with open(HASH_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def _file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_file_size(path: str) -> tuple[bool, str]:
    """超过 MAX_FILE_SIZE_MB 直接拒绝。"""
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError as e:
        return False, f"无法读取文件大小: {e}"
    if size_mb > MAX_FILE_SIZE_MB:
        return False, f"文件 {size_mb:.1f}MB 超过 {MAX_FILE_SIZE_MB}MB 上限"
    return True, ""


def _collection_delete_by_source(collection, path_str: str):
    """兼容新旧版本 Chroma 的 where 语法。"""
    try:
        existing = collection.get(where={"source": {"$eq": path_str}})
    except Exception:
        existing = collection.get(where={"source": path_str})
    if existing and existing.get("ids"):
        try:
            collection.delete(where={"source": {"$eq": path_str}})
        except Exception:
            collection.delete(ids=existing["ids"])


# ══════════════════════════════════════════════
# Parse Report 层（Step 5 新增）
# ══════════════════════════════════════════════

def _load_parse_reports() -> Dict[str, Dict[str, Any]]:
    """加载持久化的 parse_report 集合。key=源文件绝对路径，value=报告。"""
    if os.path.exists(PARSE_REPORTS):
        try:
            with open(PARSE_REPORTS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[RAG] parse_reports 读取失败: {e}")
            return {}
    return {}


def _save_parse_reports(reports: Dict[str, Dict[str, Any]]):
    os.makedirs(os.path.dirname(PARSE_REPORTS), exist_ok=True)
    with open(PARSE_REPORTS, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)


def _new_report(filename: str, path: str) -> Dict[str, Any]:
    """创建一份空 parse_report 结构。"""
    return {
        "filename": filename,
        "path": path,
        "status": "ok",            # ok | warning | error
        "chunks": 0,
        "methods": [],             # 用了哪些提取手段：text / tables / headers_footers / cells / charts ...
        "warnings": [],            # 黄色级别：入库了但可能漏内容（图片、图表）
        "errors": [],              # 红色级别：完全没入库或解析全失败
        "parsed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _set_status(report: Dict[str, Any]):
    """根据 errors / warnings 自动归算 status。"""
    if report["errors"]:
        report["status"] = "error"
    elif report["warnings"]:
        report["status"] = "warning"
    else:
        report["status"] = "ok"


# ══════════════════════════════════════════════
# 文档解析层
# ══════════════════════════════════════════════

def _parse_txt_or_md(path: str, report: Dict[str, Any]) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    report["methods"].append("text")
    return text


def _parse_docx(path: str, report: Dict[str, Any], enhanced_mode: bool = False) -> str:
    """docx 按文档真实顺序提取（段落 + 表格交错保留）。

    背景：旧版本是「所有段落拼一起 → 再拼所有表格」，导致：
    - 文档真实顺序被打破
    - chunk 切块跨越段落/表格边界，语义混乱
    - 出现在文档末尾的短条款（"解释权归..."等）被切到了中间，
      还跟无关表格挤在一起，被向量编码降权，召回不到

    新方案：遍历 doc.element.body 的子节点，按 XML 真实顺序处理 <w:p>（段落）
    和 <w:tbl>（表格），保留原始结构。

    覆盖：
    - 正文段落（按文档顺序）
    - 表格（按文档顺序，含嵌套）
    - 页眉 / 页脚（追加到末尾，因为它们不属于 body 主流）
    - 图片数量统计
    - XML 全文本兜底（处理文本框、shapes 等 doc.paragraphs 看不到的内容）
    """
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
        from docx.table import Table
    except ImportError:
        report["errors"].append("python-docx 未安装")
        logger.warning("⚠️ [RAG] python-docx 未安装: pip install python-docx")
        return ""

    try:
        doc = docx.Document(path)
    except Exception as e:
        report["errors"].append(f"docx 打开失败: {e}")
        return ""

    body = doc.element.body
    ordered_parts: List[str] = []
    has_paragraph_content = False
    has_table_content = False

    def render_table(table) -> str:
        """渲染单个表格为文本，含嵌套表格。"""
        rows_text = []
        for row in table.rows:
            cells_text = []
            for cell in row.cells:
                cell_paragraphs = [p.text.strip() for p in cell.paragraphs if p.text and p.text.strip()]
                if cell_paragraphs:
                    cells_text.append(" ".join(cell_paragraphs))
                if cell.tables:
                    for nested in cell.tables:
                        nested_text = render_table(nested)
                        if nested_text:
                            cells_text.append(nested_text)
            if cells_text:
                rows_text.append(" | ".join(cells_text))
        if rows_text:
            return "[Table]\n" + "\n".join(rows_text)
        return ""

    # 按 XML 真实顺序遍历 body 的直接子节点
    P_TAG = qn("w:p")
    TBL_TAG = qn("w:tbl")
    for child in body.iterchildren():
        if child.tag == P_TAG:
            try:
                p = Paragraph(child, doc)
                if p.text and p.text.strip():
                    ordered_parts.append(p.text)
                    has_paragraph_content = True
            except Exception:
                continue
        elif child.tag == TBL_TAG:
            try:
                t = Table(child, doc)
                rendered = render_table(t)
                if rendered:
                    ordered_parts.append(rendered)
                    has_table_content = True
            except Exception:
                continue

    if has_paragraph_content:
        report["methods"].append("text")
    if has_table_content:
        report["methods"].append("tables")

    # 页眉 / 页脚（不属于 body 主流，追加到末尾）
    headers_footers: List[str] = []
    for section in doc.sections:
        try:
            for p in section.header.paragraphs:
                if p.text and p.text.strip():
                    headers_footers.append(f"[Header] {p.text.strip()}")
            for p in section.footer.paragraphs:
                if p.text and p.text.strip():
                    headers_footers.append(f"[Footer] {p.text.strip()}")
        except Exception:
            continue
    if headers_footers:
        seen = set()
        uniq = []
        for h in headers_footers:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        ordered_parts.append("\n".join(uniq))
        report["methods"].append("headers_footers")

    # 图片数量统计（warning 延迟到增强模式处理后再写，避免误导性提示）
    drawing_count = 0
    try:
        drawings = body.findall(".//" + qn("w:drawing"))
        drawing_count = len(drawings)
    except Exception:
        pass

    # XML 全文本兜底扫描
    try:
        all_text_nodes = []
        for t in body.iter(qn("w:t")):
            if t.text:
                all_text_nodes.append(t.text)
        xml_full_text = "".join(all_text_nodes)
        xml_char_count = len(xml_full_text.strip())

        ordered_text = "\n\n".join(ordered_parts)
        extracted_char_count = len(ordered_text.strip())

        if xml_char_count > 0 and extracted_char_count < xml_char_count * 0.8:
            missing_ratio = 1.0 - (extracted_char_count / xml_char_count)
            logger.warning(
                f"[RAG] {pathlib.Path(path).name} 顺序提取漏内容(缺失 {missing_ratio:.0%})，"
                f"启用 XML 兜底(顺序={extracted_char_count} XML={xml_char_count})"
            )
            report["methods"].append("xml_fallback")
            report["warnings"].append(
                f"顺序提取漏内容 {missing_ratio:.0%}(可能含文本框或特殊排版)，已通过 XML 兜底找回。"
            )
            ordered_parts.append("[XML Fallback]\n" + xml_full_text)
    except Exception as e:
        logger.debug(f"[RAG] {pathlib.Path(path).name} XML 兜底扫描失败(跳过): {e}")

    # 增强模式：提取 inline shapes 图片并生成描述
    enhanced_img_count = 0
    if enhanced_mode:
        try:
            from docx.oxml.ns import qn as _qn
            img_descs = []
            for shape in doc.inline_shapes:
                try:
                    # type 3 = PICTURE
                    if shape.type != 3:
                        continue
                    rid = shape._inline.graphic.graphicData.pic.blipFill.blip.get(
                        _qn("r:embed")
                    )
                    if not rid:
                        continue
                    img_part = doc.part.related_parts.get(rid)
                    if not img_part:
                        continue
                    img_bytes = img_part.blob
                    # 简单尺寸过滤(EMU单位，914400 EMU = 1 inch ≈ 96px，200px ≈ 1905000 EMU)
                    cx = shape.width or 0
                    cy = shape.height or 0
                    if cx < 1905000 or cy < 1905000:
                        continue
                    desc = _describe_image_multimodal(img_bytes, "png")
                    if desc:
                        img_descs.append(f"[Image Description]\n{desc}")
                except Exception:
                    continue
            if img_descs:
                ordered_parts.extend(img_descs)
                enhanced_img_count = len(img_descs)
                report["methods"].append(f"enhanced_images({enhanced_img_count})")
                logger.info(f"[RAG] 增强模式：docx 提取 {enhanced_img_count} 张图片描述")
        except Exception as e:
            logger.debug(f"[RAG] 增强模式 docx 图片提取失败(跳过): {e}")

    # 图片 warning：根据增强模式实际效果决定文案
    if drawing_count > 0:
        if not enhanced_mode:
            report["warnings"].append(f"包含 {drawing_count} 张图片，Nano 暂无法识别图片内容")
        elif enhanced_img_count > 0:
            unrecognized = drawing_count - enhanced_img_count
            if unrecognized > 0:
                report["warnings"].append(
                    f"已通过增强模式识别 {enhanced_img_count} 张图片，"
                    f"另有 {unrecognized} 张浮动图片暂无法识别（文字环绕排版，增强模式暂不支持）"
                )
            # 全部识别成功：不写 warning，methods 里已有 enhanced_images(N) 记录
        else:
            # 增强模式开了但一张都没识别成功
            report["warnings"].append(
                f"包含 {drawing_count} 张图片，识别失败，"
                f"可能包含浮动图片（文字环绕排版），增强模式暂不支持识别此类图片"
            )

    if not ordered_parts:
        report["errors"].append("docx 解析后内容为空")
        return ""

    return "\n\n".join(ordered_parts)

def _parse_pptx(path: str, report: Dict[str, Any], enhanced_mode: bool = False) -> str:
    """pptx 解析：按幻灯片顺序提取标题 + 正文 + 表格 + 图片（增强模式）。

    提取策略：
    - 每张幻灯片以 [Slide N] 标记开头，保留原始顺序
    - 先取 title placeholder，再递归遍历所有 shape（含 group 嵌套）
    - 表格按 [Table] 格式渲染，与 docx/pdf 的 chunk 切块逻辑完全兼容
    - 图片：增强模式下调多模态模型生成描述；未开启时统计数量并给 warning
    - 图片密度预警：图片数 > 文本块数时给更紧迫的 warning，引导开启增强模式
    - 备注（notes）默认不提取（噪音多于信号）

    已知局限：
    - SmartArt / 艺术字 / 嵌入 OLE 对象无法提取文字
    - 图表（Chart）只记数量，数据不提取（与 xlsx 图表处理策略一致）
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError:
        report["errors"].append("python-pptx 未安装: pip install python-pptx")
        logger.warning("⚠️ [RAG] python-pptx 未安装: pip install python-pptx")
        return ""

    try:
        prs = Presentation(path)
    except Exception as e:
        report["errors"].append(f"pptx 打开失败: {e}")
        return ""

    # ── 递归遍历 shape tree（处理 group 嵌套）────────────────
    def _collect_shapes(shape_collection, title_shape=None):
        """递归展开 group，返回所有叶子 shape（排除 title）。"""
        result = []
        for s in shape_collection:
            if s == title_shape:
                continue
            if s.shape_type == MSO_SHAPE_TYPE.GROUP:
                result.extend(_collect_shapes(s.shapes))
            else:
                result.append(s)
        return result

    parts: List[str] = []
    total_images = 0
    total_charts = 0
    text_shape_count = 0   # 用于图片密度判断
    has_text = False
    has_tables = False
    img_blobs: List[tuple] = []   # (blob, ext, slide_idx) 待增强模式处理

    for slide_idx, slide in enumerate(prs.slides, start=1):
        slide_parts: List[str] = []

        # ── 标题 ──────────────────────────────────────────────
        title_text = ""
        title_shape = slide.shapes.title
        if title_shape and title_shape.has_text_frame:
            title_text = title_shape.text_frame.text.strip()

        slide_header = f"[Slide {slide_idx}]"
        if title_text:
            slide_header += f" {title_text}"
        slide_parts.append(slide_header)

        # ── 递归展开所有 shape（含 group）───────────────────
        for shape in _collect_shapes(slide.shapes, title_shape=title_shape):

            # 图片
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                total_images += 1
                if enhanced_mode:
                    try:
                        img_ext = shape.image.ext or "png"
                        img_blobs.append((shape.image.blob, img_ext, slide_idx))
                    except Exception:
                        pass
                continue

            # 图表
            if shape.shape_type == MSO_SHAPE_TYPE.CHART:
                total_charts += 1
                continue

            # 表格
            if shape.has_table:
                rows_text = []
                for row in shape.table.rows:
                    cells = []
                    for cell in row.cells:
                        try:
                            cell_text = cell.text_frame.text.strip() if cell.text_frame else ""
                        except Exception:
                            cell_text = ""
                        if cell_text:
                            cells.append(cell_text)
                    if cells:
                        rows_text.append(" | ".join(cells))
                if rows_text:
                    slide_parts.append("[Table]\n" + "\n".join(rows_text))
                    has_tables = True
                continue

            # 文本框 / 占位符
            if shape.has_text_frame:
                try:
                    text = shape.text_frame.text.strip()
                except Exception:
                    text = ""
                if text and text != title_text:
                    slide_parts.append(text)
                    has_text = True
                    text_shape_count += 1

        if len(slide_parts) > 1:
            parts.append("\n".join(slide_parts))
        elif title_text:
            parts.append(slide_header)

    if has_text:
        report["methods"].append("text")
    if has_tables:
        report["methods"].append("tables")

    # ── 增强模式：调多模态识别图片 ───────────────────────────
    enhanced_img_count = 0
    if enhanced_mode and img_blobs:
        img_descs = []
        for blob, ext, s_idx in img_blobs:
            try:
                desc = _describe_image_multimodal(blob, ext)
                if desc:
                    img_descs.append(f"[Slide {s_idx} Image Description]\n{desc}")
            except Exception:
                continue
        if img_descs:
            parts.extend(img_descs)
            enhanced_img_count = len(img_descs)
            report["methods"].append(f"enhanced_images({enhanced_img_count})")
            logger.info(f"[RAG] 增强模式：pptx 提取 {enhanced_img_count} 张图片描述")

    # ── 图片 warning（根据增强模式实际效果决定文案）──────────
    if total_images > 0:
        if not enhanced_mode:
            total_shapes = total_images + text_shape_count
            img_ratio = total_images / total_shapes if total_shapes > 0 else 1.0
            if img_ratio > 0.6:
                report["warnings"].append(
                    f"此文件以图片内容为主（图片 {total_images} 张 / 文本块 {text_shape_count} 个），"
                    f"大量内容可能未被读取，强烈建议开启增强模式重新入库"
                )
            else:
                report["warnings"].append(
                    f"包含 {total_images} 张图片，Nano 暂无法识别图片内容，"
                    f"可开启增强模式重新入库以读取图片内容"
                )
        elif enhanced_img_count > 0:
            failed = total_images - enhanced_img_count
            if failed > 0:
                report["warnings"].append(
                    f"已通过增强模式识别 {enhanced_img_count} 张图片，"
                    f"另有 {failed} 张图片识别失败（可能是图片格式异常或 API 错误）"
                )
            # 全部识别成功：不写 warning，methods 里已有记录
        else:
            report["warnings"].append(
                f"包含 {total_images} 张图片，识别失败，可能是图片格式异常或 API 错误"
            )

    if total_charts > 0:
        report["warnings"].append(f"包含 {total_charts} 个图表，Nano 已记录但无法提取图表数据")

    if not parts:
        report["errors"].append("pptx 解析后内容为空")
        return ""

    return "\n\n".join(parts)

def _parse_xlsx(path: str, report: Dict[str, Any]) -> str:
    """Step 5+：xlsx 改用 openpyxl 按 cell 读取，保留位置信息。

    关键设计：
    - 使用 read_only=False 而不是 True。
      原因：很多企业报表系统(SAP/Oracle/旧版工具)导出的 .xlsx 缺少 dimension 元数据，
      在 read_only=True 模式下 openpyxl 只读到 A1 就停了。
      read_only=False 强制全量扫描，能正确读出所有 cell。
      内存代价已由 MAX_FILE_SIZE_MB=50 兜底。
    - data_only=True 取公式计算结果而不是公式本身(前提是文件保存时已计算)。
    - 处理合并单元格：把合并区域内所有 cell 都填上左上角的值。
    - 处理空 cell：跳过 None，但保留位置坐标信息以便溯源。
    """
    try:
        import openpyxl
    except ImportError:
        report["errors"].append("openpyxl 未安装")
        logger.warning("⚠️ [RAG] openpyxl 未安装: pip install openpyxl")
        return ""

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception as e:
        report["errors"].append(f"xlsx 打开失败: {e}")
        return ""

    blocks: List[str] = []
    total_charts = 0
    total_images = 0
    total_merged = 0
    formula_only_cells = 0  # 公式但未计算的 cell 数

    for sheet_name in wb.sheetnames:
        try:
            ws = wb[sheet_name]
        except Exception:
            continue

        # 1. 处理合并单元格：把每个合并区域内所有 cell 标记为持有左上角的值
        # 这样在 iter_rows 里就不会丢内容
        merged_value_map: Dict[str, Any] = {}
        try:
            for merged_range in list(ws.merged_cells.ranges):
                min_col, min_row, max_col, max_row = merged_range.bounds
                top_left = ws.cell(row=min_row, column=min_col)
                top_value = top_left.value
                if top_value is None:
                    continue
                for r in range(min_row, max_row + 1):
                    for c in range(min_col, max_col + 1):
                        coord = ws.cell(row=r, column=c).coordinate
                        merged_value_map[coord] = top_value
                total_merged += 1
        except Exception as e:
            logger.debug(f"[RAG] {sheet_name} 合并单元格处理跳过: {e}")

        # 2. 收集所有图表 / 图片
        try:
            total_charts += len(getattr(ws, "_charts", []) or [])
            total_images += len(getattr(ws, "_images", []) or [])
        except Exception:
            pass

        # 3. 遍历所有 cell
        rows_text: List[str] = []
        for row in ws.iter_rows(values_only=False):
            cells_in_row: List[str] = []
            for cell in row:
                val = cell.value
                # 合并区域里非左上角的 cell 用 merged_value_map 兜底
                if val is None and cell.coordinate in merged_value_map:
                    val = merged_value_map[cell.coordinate]
                if val is None:
                    continue

                # 检测：如果 val 是个公式字符串(以 = 开头)，说明 data_only 没拿到计算结果
                if isinstance(val, str) and val.startswith("=") and len(val) > 1:
                    formula_only_cells += 1
                    continue  # 跳过未计算的公式，避免给模型看 raw 公式

                s_val = str(val).strip()
                if not s_val:
                    continue
                cells_in_row.append(f"{cell.coordinate}={s_val}")
            if cells_in_row:
                rows_text.append(" | ".join(cells_in_row))

        if rows_text:
            blocks.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows_text))

    wb.close()

    if blocks:
        report["methods"].append("cells")
    if total_merged:
        report["methods"].append(f"merged_cells({total_merged})")
    if total_charts:
        report["warnings"].append(f"包含 {total_charts} 个图表，Nano 已提取数据但忽略可视化部分")
    if total_images:
        report["warnings"].append(f"包含 {total_images} 张图片，Nano 暂无法识别图片内容")
    if formula_only_cells:
        report["warnings"].append(
            f"{formula_only_cells} 个 cell 是公式但没有缓存计算结果，"
            f"请在 Excel 里打开保存一次后重新入库(这通常发生在文件由其他系统直接生成、没在 Excel 里保存过的情况)"
        )

    if not blocks:
        report["errors"].append("xlsx 解析后内容为空(所有 sheet 都是空的或无法读取)")
        return ""

    return "\n\n".join(blocks)


def _parse_xls(path: str, report: Dict[str, Any]) -> str:
    """旧 .xls 格式 openpyxl 不支持，回退到 pandas + xlrd。

    真实场景注意：
    - xlrd 在 2.0+ 版本里移除了 .xls 支持，需要明确装 xlrd==1.2.0
    - 如果是 .xls 文件但 magic bytes 是 zip，其实是 .xlsx 改了后缀，应让用户改回 .xlsx
    """
    try:
        import pandas as pd
    except ImportError:
        report["errors"].append("pandas 未安装")
        return ""

    # 检测一下是不是 zip header(被改了后缀的 xlsx)
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic.startswith(b"PK"):
            report["errors"].append(
                "文件后缀是 .xls 但实际是 .xlsx 格式。请把文件后缀改为 .xlsx 后重新放入知识库。"
            )
            return ""
    except Exception:
        pass

    try:
        xl = pd.ExcelFile(path)
        all_text = []
        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name)
            all_text.append(f"[Sheet: {sheet_name}]\n{df.to_string(index=False)}")
        report["methods"].append("text")
        return "\n\n".join(all_text)
    except ImportError as e:
        report["errors"].append(
            f"xls requires xlrd 1.2.0 support: pip install 'xlrd==1.2.0' ({e})"
        )
        return ""
    except Exception as e:
        report["errors"].append(f"xls 解析失败: {e}")
        return ""


def _parse_csv(path: str, report: Dict[str, Any]) -> str:
    """CSV 解析。

    真实场景适配：
    - 编码自动探测(utf-8 / gbk / gb2312 / utf-8-sig)
    - 分隔符自动探测(逗号 / 分号 / Tab / 竖线)
      中国办公环境的 CSV 经常用 ; 或 \\t，标准库 csv.Sniffer 不一定靠谱，
      我们直接用首行字符统计来选择最可能的分隔符。
    """
    try:
        import pandas as pd
    except ImportError:
        report["errors"].append("pandas 未安装")
        return ""

    # 第一阶段：找到能正确读出的编码
    raw_text: Optional[str] = None
    encoding_used: Optional[str] = None
    for enc in ["utf-8-sig", "utf-8", "gbk", "gb2312"]:
        try:
            with open(path, "r", encoding=enc) as f:
                raw_text = f.read()
            encoding_used = enc
            break
        except UnicodeDecodeError:
            continue
        except Exception as e:
            report["errors"].append(f"csv 读取失败: {e}")
            return ""

    if raw_text is None:
        report["errors"].append("csv 所有编码尝试都失败(utf-8 / gbk / gb2312)")
        return ""

    # 第二阶段：探测分隔符
    # 用首行非引号区域里出现次数最多的候选符号作为分隔符
    first_line = raw_text.split("\n", 1)[0] if raw_text else ""
    candidates = [",", ";", "\t", "|"]
    sep_counts = {sep: first_line.count(sep) for sep in candidates}
    best_sep = max(sep_counts, key=sep_counts.get)
    if sep_counts[best_sep] == 0:
        best_sep = ","  # 默认逗号

    try:
        df = pd.read_csv(path, encoding=encoding_used, sep=best_sep)
        report["methods"].append(f"text(encoding={encoding_used},sep={'TAB' if best_sep == chr(9) else best_sep})")
        return df.to_string(index=False)
    except Exception as e:
        # 兜底再试默认逗号
        try:
            df = pd.read_csv(path, encoding=encoding_used)
            report["methods"].append(f"text(encoding={encoding_used},sep=auto)")
            return df.to_string(index=False)
        except Exception as e2:
            report["errors"].append(f"csv 解析失败: {e2}")
            return ""


# ⚠️ 这个标志指的是 **pytesseract + pymupdf + PIL** 是否可用。
#    它原名 `_paddle_ocr_available` —— 而 paddle 那条路 2026-08-29 已整条删除。
#    📌 **一个名字说 A、内容是 B 的变量，读代码的人会照名字理解它**，
#       然后基于一个不存在的事实做判断。
_scan_ocr_available: Optional[bool] = None
_tesseract_cmd_cache: Optional[str] = "__unresolved__"


def _resolve_tesseract_cmd() -> Optional[str]:
    """解析 tesseract 可执行文件路径，不写死 Windows 路径。

    优先级：
    1. 环境变量 TESSERACT_CMD（用户/部署环境显式指定，跨平台首选）
    2. PATH 里能找到的 tesseract（pip 装的 pytesseract 不含二进制，
       但很多环境会单独装好 tesseract 并加入 PATH）
    3. Windows 常见默认安装路径（仅作最后兜底，本机能跑但不可移植）

    都找不到返回 None，调用方应跳过 tesseract OCR 并记录日志，
    而不是报错崩溃。结果按进程缓存，避免重复探测文件系统。
    """
    global _tesseract_cmd_cache
    if _tesseract_cmd_cache != "__unresolved__":
        return _tesseract_cmd_cache

    import shutil

    env_path = os.getenv("TESSERACT_CMD")
    if env_path and os.path.exists(env_path):
        _tesseract_cmd_cache = env_path
        return _tesseract_cmd_cache

    # PyInstaller onedir: exe 同级 tesseract/tesseract.exe
    try:
        import sys as _sys
        _exe_dir = os.path.dirname(os.path.abspath(_sys.executable))
        _bundled = os.path.join(_exe_dir, "tesseract", "tesseract.exe")
        if os.path.exists(_bundled):
            _tesseract_cmd_cache = _bundled
            return _tesseract_cmd_cache
    except Exception:
        pass

    which_path = shutil.which("tesseract")
    if which_path:
        _tesseract_cmd_cache = which_path
        return _tesseract_cmd_cache

    _default_win = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(_default_win):
        _tesseract_cmd_cache = _default_win
        return _tesseract_cmd_cache

    _tesseract_cmd_cache = None
    # ⚠️ Tesseract 缺失是典型的"哑弹型降级" —— 扫描件入库悄悄失效，
    # 视觉定位第二级 OCR 也失效、直接跳到更贵的多模态兜底。不报的话
    # 用户只会觉得"最近有点贵/有点不准"，永远查不到原因。
    # 注意：executor_vision.py 也调这个函数，所以一处上报两处受益。
    try:
        from core.health import Cap, report_degraded
        report_degraded(
            Cap.OCR_TESSERACT, "TESSERACT_NOT_FOUND",
            user_message="没找到 Tesseract：扫描件/图片入库的 OCR 不可用，视觉定位也会跳过 OCR 这一级、直接用更贵的多模态兜底。",
            hint="装 Tesseract 并配 chi_sim 中文包，或设环境变量 TESSERACT_CMD 指向 tesseract.exe。",
            hint_en=("Install Tesseract OCR (with the `chi_sim` language pack), or set the "
                     "`TESSERACT_CMD` environment variable to the tesseract.exe path."),
            detail="TESSERACT_CMD / PATH / 默认安装路径 三处都没找到",
        )
    except Exception:
        pass
    return None


def _check_ocr_available() -> bool:
    """检测 pytesseract + pymupdf 是否可用。

    Step 6 决策：paddle 推理引擎在本机 CPU 环境下对图像输入整体失效(Unknown exception)，
    改用 pytesseract 作为扫描件 OCR 兜底。识别率约 70%，入库后健康度面板标黄提示。
    """
    global _scan_ocr_available
    if _scan_ocr_available is not None:
        return _scan_ocr_available
    try:
        import pytesseract  # noqa
        import fitz  # noqa
        from PIL import Image  # noqa
        _scan_ocr_available = True
        logger.info("✔ [RAG] pytesseract + pymupdf 可用，启用扫描件 OCR 兜底")
    except ImportError as e:
        _scan_ocr_available = False
        logger.warning(f"⚠️ [RAG] OCR 依赖缺失({e})，PDF 只走文本提取。装包：pip install pytesseract pymupdf pillow")
    return _scan_ocr_available


# 🪦 [2026-08-29] 删除 PP-Structure 三件套（_load_paddle_ocr / _check / _render，约 15 行）。
#
# 🔴 它不是「暂时没接线」，是**已经被判定不工作并且已经被替换掉了**：
#    同文件 `_check_ocr_available()` 的 docstring 白纸黑字写着 ——
#    「paddle 推理引擎在本机 CPU 环境下对图像输入整体失效(Unknown exception)，
#      改用 pytesseract 作为扫描件 OCR 兜底」。
#    ⇒ 整条链外部零入口，而 requirements 里还压着 paddleocr / paddlepaddle /
#      paddlex 三个包（paddlepaddle 本体数百 MB）。
#    📌 **一个已经被证明不工作、并且已经被替换掉的依赖，留在 requirements 里
#       不是「备用」，是让每个装 Nano 的人白下几百 MB。**
#
# ⚠️ 顺带修了一处误导性命名：`_scan_ocr_available` 这个变量名指的其实是
#    **tesseract + pymupdf + PIL 可用性**，跟 paddle 一点关系都没有 ——
#    📌 一个名字说 A、内容是 B 的变量，读代码的人会照名字理解它。

def _parse_pdf_pdfplumber(path: str, enhanced_mode: bool = False) -> Tuple[str, int, int]:
    """第一层：pdfplumber。返回 (text, total_pages, non_empty_pages)。

    Step 6+：增加 extract_tables() 抽取。对文本型 PDF 里的表格，pdfplumber
    本身的 extract_text() 会把表格行压成纯文本一行行扁平输出，丢失列对齐信息。
    通过 page.extract_tables() 拿到二维结构，重新拼成 markdown row 格式
    "| 列1 | 列2 | 列3 |"，对向量和 BM25 都更友好。

    顺手压制 pdfminer 的"FontBBox / FontFile / CMap"等字体级 warning。
    某些 PDF 字体描述不规范，每个字符渲染时都会触发一次警告，
    一份几千字的 PDF 能爆出几千行同样的日志。
    """
    import logging
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    import pdfplumber
    page_blocks = []  # 每页一段文本(含表格 markdown)
    non_empty = 0
    total = 0

    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages:
            page_parts = []

            # 1. 提正文文本
            t = page.extract_text()
            if t and t.strip():
                page_parts.append(t.strip())

            # 2. 提取页面内所有文字行，用于给表格找标题
            # 原理：表格正上方最近的非空文字行通常就是表格标题
            try:
                words = page.extract_words() or []
                # 按 y 坐标分组成行(y 相差 < 5 视为同一行)
                text_lines: list[tuple[float, str]] = []
                if words:
                    current_y = words[0]["top"]
                    current_line_words = [words[0]["text"]]
                    for w in words[1:]:
                        if abs(w["top"] - current_y) < 5:
                            current_line_words.append(w["text"])
                        else:
                            line_text = " ".join(current_line_words).strip()
                            if line_text:
                                text_lines.append((current_y, line_text))
                            current_y = w["top"]
                            current_line_words = [w["text"]]
                    if current_line_words:
                        line_text = " ".join(current_line_words).strip()
                        if line_text:
                            text_lines.append((current_y, line_text))
            except Exception:
                text_lines = []

            # 3. 提表格，并给每张表格找标题前缀
            try:
                tables_with_bbox = page.find_tables() or []
            except Exception:
                tables_with_bbox = []

            try:
                tables_raw = page.extract_tables() or []
            except Exception:
                tables_raw = []

            for tbl_idx, tbl in enumerate(tables_raw):
                if not tbl:
                    continue

                # 尝试找这张表格的标题：取表格上方最近的文字行
                table_title = ""
                try:
                    if tbl_idx < len(tables_with_bbox):
                        tbl_bbox = tables_with_bbox[tbl_idx].bbox  # (x0, top, x1, bottom)
                        tbl_top = tbl_bbox[1]
                        # 找所有在表格上方的文字行，取最近的那行
                        above_lines = [(y, txt) for y, txt in text_lines if y < tbl_top - 2]
                        if above_lines:
                            # 最近的上方行
                            closest = max(above_lines, key=lambda x: x[0])
                            candidate = closest[1].strip()
                            # 过滤掉太长的行(正文段落不是标题)和纯数字行
                            if 2 <= len(candidate) <= 50 and not candidate.replace(" ", "").replace("%", "").replace(".", "").isdigit():
                                table_title = candidate
                except Exception:
                    table_title = ""

                # 渲染表格行为 markdown
                rendered_rows = []
                for row in tbl:
                    cells = []
                    for c in row:
                        cell_text = "" if c is None else str(c).strip().replace("\n", " ")
                        cells.append(cell_text)
                    if any(cells):
                        rendered_rows.append("| " + " | ".join(cells) + " |")

                if rendered_rows:
                    # 带标题前缀：[Table: 标题] 或 [Table]
                    prefix = f"[Table: {table_title}]" if table_title else "[Table]"
                    page_parts.append(prefix + "\n" + "\n".join(rendered_rows))

            # 增强模式：提取页面嵌入图片并生成描述
            if enhanced_mode and page_parts:
                try:
                    import fitz as _fitz
                    _fitz_doc = _fitz.open(path)
                    _fitz_page = _fitz_doc[page.page_number]
                    page_w = _fitz_page.rect.width
                    page_h = _fitz_page.rect.height
                    images = _fitz_page.get_images(full=True)
                    img_descs = []
                    for img_info in images:
                        xref = img_info[0]
                        try:
                            # 过滤：图片尺寸 > 200px，且不在边缘 10% 区域(避免水印/logo)
                            rects = _fitz_page.get_image_rects(xref)
                            if not rects:
                                continue
                            rect = rects[0]
                            w = rect.width
                            h = rect.height
                            if w < 200 or h < 200:
                                continue
                            # 边缘过滤
                            edge_x = page_w * 0.1
                            edge_y = page_h * 0.1
                            if rect.x0 < edge_x and rect.x1 < edge_x:
                                continue
                            if rect.x0 > page_w - edge_x and rect.x1 > page_w - edge_x:
                                continue
                            if rect.y0 < edge_y and rect.y1 < edge_y:
                                continue
                            # 提取图片并描述
                            base_img = _fitz_doc.extract_image(xref)
                            img_bytes = base_img["image"]
                            img_ext = base_img.get("ext", "png")
                            desc = _describe_image_multimodal(img_bytes, img_ext)
                            if desc:
                                img_descs.append(f"[Image Description]\n{desc}")
                        except Exception:
                            continue
                    _fitz_doc.close()
                    if img_descs:
                        page_parts.extend(img_descs)
                        logger.debug(f"[RAG] 增强模式：第 {page.page_number + 1} 页提取 {len(img_descs)} 张图片描述")
                except Exception as e:
                    logger.debug(f"[RAG] 增强模式图片提取失败(跳过): {e}")

            if page_parts:
                page_blocks.append("\n\n".join(page_parts))
                non_empty += 1

    return "\n\n".join(page_blocks), total, non_empty


def _parse_pdf_pymupdf(path: str) -> Tuple[str, int, int]:
    """第二层：pymupdf (fitz)。某些扫描混排 PDF 它比 pdfplumber 强。"""
    import fitz
    texts = []
    non_empty = 0
    doc = fitz.open(path)
    total = doc.page_count
    for page in doc:
        t = page.get_text()
        if t and t.strip():
            texts.append(t)
            non_empty += 1
    doc.close()
    return "\n".join(texts), total, non_empty










def _html_table_to_markdown(html: str) -> str:
    """把 PP-Structure 返回的 HTML 表格简单转成 markdown。

    PP-Structure 返回的 HTML 通常很干净：<table><tr><td>...</td></tr></table>
    我们不引入 BeautifulSoup 依赖，用正则解析。
    """
    import re
    if not html:
        return ""
    # 提所有 <tr>...</tr>
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    md_rows = []
    for row in rows:
        # 每个 cell 是 <td>...</td> 或 <th>...</th>
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL | re.IGNORECASE)
        clean_cells = []
        for c in cells:
            # 去 HTML 内部标签，去多余空白
            c = re.sub(r"<[^>]+>", "", c)
            c = re.sub(r"\s+", " ", c).strip()
            clean_cells.append(c)
        if any(clean_cells):
            md_rows.append("| " + " | ".join(clean_cells) + " |")
    return "\n".join(md_rows)


def _vision_model() -> str:
    """看图该用哪个模型。

    ⚠️ 这三处 OCR/图片描述以前**写死** `"anthropic/claude-haiku-4.5"` ——
       连 config 都没走。换厂商时会照着一个不存在的模型名发请求，
       报出来是 404 model not found，根本看不出根因是"某个 OCR 路径写死了"。
    📌 视觉是独立角色槽（同 distiller / classifier）：
       `vision_for()` 空串 = **退回主模型**，不是「不看图」——
       不看图 = 用户贴了图 Nano 说看不见 = 失能。
    """
    try:
        from core.models import vision_for
        from core.provider import provider as _p
        main = getattr(_p, "target_model", "") or ""
        return vision_for(main) or main
    except Exception:
        # ⚠️ 观测/解析失败绝不能让 OCR 整条路挂掉 —— 退回主模型
        try:
            from core.provider import provider as _p
            return getattr(_p, "target_model", "") or ""
        except Exception:
            return ""


def _parse_pdf_ocr(path: str, max_pages: int = 50) -> Tuple[str, int, int]:
    """第三层：多模态模型 OCR（替代 tesseract）。

    改用它的理由：tesseract 中文识别率约 70%，且无法理解表格结构。
    改用 Gemini 多模态模型直接看每页图片，质量大幅提升，
    能保留表格结构和图文关系。

    降级策略：多模态调用失败时自动回退到 tesseract，
    确保入库不会因多模态故障而完全失败。

    max_pages：硬上限，避免超长 PDF 消耗过多 API 配额。
    返回 (text, ocr_pages_count, total_pages)
    """
    import fitz
    import io
    import os
    # ⚠️⚠️ 下面有一条 `except concurrent.futures.TimeoutError:`，
    #    而 `concurrent` **在这个作用域里从来没被导入过**（同名 import 在另一个
    #    函数里 —— 那个不算）。
    # 🔴 后果：OCR 真的超时的那一刻，**`except` 子句本身**抛 `NameError`，
    #    于是那次超时不会被当成超时处理，它变成一个面目全非的错误往上冒。
    # 📌 **一个 `except` 子句里的名字如果不存在，它不是「少了一个兜底」，
    #    而是「把一个已知错误换成了未知错误」** —— 比没有那条 except 更糟。
    # 📌 而这一处和 `_get_collection` 那条健康上报同形：
    #    **一条「出事时才走」的路径上的错误，只会在出事的时候暴露 ——
    #    也就是最不该再出错的时候。**
    # ⭐ 这两处都是 `tests/t_l23_missing_imports.py` 那个作用域检查器扫出来的，
    #    不是看出来的。
    import concurrent.futures

    doc = fitz.open(path)
    total = doc.page_count
    actual_pages = min(total, max_pages)
    page_texts = []
    ocr_count = 0
    use_tesseract_fallback = False

    # 优先用多模态模型
    try:
        import base64
        import asyncio as _asyncio
        from core.provider import provider as _nano_provider
        _claude_model = _vision_model()

        for page_idx in range(actual_pages):
            try:
                page = doc[page_idx]
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                img_b64 = base64.b64encode(img_bytes).decode()

                prompt = (
                    "Extract all text from this image completely, including tables, titles, body text, annotations, and any visible text. "
                    "For tables, output markdown table format such as | column 1 | column 2 |. "
                    "Do not add explanations or descriptions. Output only the original extracted text."
                )
                response = _asyncio.run(_nano_provider._client.messages.create(
                    model=_claude_model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": prompt},
                    ]}],
                ))
                text = response.content[0].text if response.content else ""
                if text and text.strip():
                    page_texts.append(text.strip())
                    ocr_count += 1
            except concurrent.futures.TimeoutError:
                logger.warning(f"[RAG] 多模态 OCR 第 {page_idx + 1} 页超时(60s)，已跳过")
                continue
            except Exception as e:
                logger.debug(f"[RAG] 多模态 OCR 第 {page_idx + 1} 页失败: {e}")
                continue

    except Exception as multimodal_err:
        logger.warning(f"[RAG] 多模态 OCR 不可用，降级到 tesseract: {multimodal_err}")
        use_tesseract_fallback = True

    # 降级：tesseract 兜底
    if use_tesseract_fallback or (ocr_count == 0 and actual_pages > 0):
        _tess_cmd = _resolve_tesseract_cmd()
        if not _tess_cmd:
            logger.warning(
                "[RAG] 未找到 tesseract 可执行文件，跳过 OCR 兜底。"
                "可设置环境变量 TESSERACT_CMD 指向 tesseract 可执行文件路径"
                "（如 Windows 上的 'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'），"
                "或将 tesseract 加入 PATH。"
            )
        else:
            try:
                import pytesseract
                from PIL import Image
                pytesseract.pytesseract.tesseract_cmd = _tess_cmd
                page_texts = []
                ocr_count = 0
                for page_idx in range(actual_pages):
                    try:
                        page = doc[page_idx]
                        pix = page.get_pixmap(dpi=200)
                        img_bytes = pix.tobytes("png")
                        img = Image.open(io.BytesIO(img_bytes))
                        text = pytesseract.image_to_string(img, lang='chi_sim+eng')
                        if text and text.strip():
                            page_texts.append(text.strip())
                            ocr_count += 1
                    except Exception as e:
                        logger.debug(f"[RAG] tesseract 第 {page_idx + 1} 页失败: {e}")
                        continue
            except Exception as e:
                logger.warning(f"[RAG] tesseract 降级也失败: {e}")

    doc.close()
    return "\n\n".join(page_texts), ocr_count, total


def _parse_pdf(path: str, report: Dict[str, Any], index_config: dict | None = None) -> str:
    """PDF 解析三层 fallback：

    1. pdfplumber：文本型 PDF 最常见、版面好
    2. pymupdf (fitz)：pdfplumber 解析不动的某些 PDF 它能搞定
    3. PaddleOCR：扫描型/图片型 PDF 的最后兜底，慢但能救

    降级判断：当前层提取的总字符 < 总页数 × 30，认为"基本没提到东西"，进下一层。
    (30 字符约一行中文，每页连一行都没有几乎可以确定是扫描件)
    """
    cfg = index_config or {}
    enhanced_mode = cfg.get("enhanced_mode", False)
    max_ocr_pages = cfg.get("max_ocr_pages", 50)

    # 阶段 1：pdfplumber(同时提正文 + 表格)
    try:
        text, total_pages, non_empty = _parse_pdf_pdfplumber(path, enhanced_mode=enhanced_mode)
        if total_pages > 0 and len(text.strip()) >= total_pages * 30:
            # 标记 pdfplumber+tables：让健康度面板知道这份 PDF 走的是结构化抽取
            tag = "pdfplumber+tables" if "[Table" in text else "pdfplumber"
            report["methods"].append(tag)
            empty_pages = total_pages - non_empty
            if empty_pages > 0:
                report["warnings"].append(
                    f"pdfplumber：{empty_pages}/{total_pages} 页提取为空，但其他页内容充分"
                )
            return text
        # 提取太少 → 尝试下一层
        logger.debug(f"[RAG] pdfplumber 提取 {len(text.strip())} 字 / {total_pages} 页，太稀疏，降级到 pymupdf")
    except ImportError:
        report["errors"].append("pdfplumber 未安装")
    except Exception as e:
        logger.debug(f"[RAG] pdfplumber 失败，降级到 pymupdf: {e}")

    # 阶段 2：pymupdf
    try:
        text, total_pages, non_empty = _parse_pdf_pymupdf(path)
        if total_pages > 0 and len(text.strip()) >= total_pages * 30:
            report["methods"].append("pymupdf")
            empty_pages = total_pages - non_empty
            if empty_pages > 0:
                report["warnings"].append(
                    f"pymupdf：{empty_pages}/{total_pages} 页提取为空(用 pdfplumber 失败后切到这里)"
                )
            return text
        logger.debug(f"[RAG] pymupdf 提取 {len(text.strip())} 字 / {total_pages} 页，太稀疏，降级到 OCR")
    except ImportError:
        logger.debug("[RAG] pymupdf 未装，跳过到 OCR")
    except Exception as e:
        logger.debug(f"[RAG] pymupdf 失败，降级到 OCR: {e}")

    # 阶段 3：PaddleOCR(最后兜底，扫描件专用)
    if not _check_ocr_available():
        report["errors"].append(
            "PDF 文本提取失败且 OCR 不可用(可能是扫描件)。"
            "安装 OCR：pip install pymupdf pytesseract，并安装 Tesseract 本体"
        )
        return ""

    try:
        logger.info(f"[RAG] {pathlib.Path(path).name} 走 OCR 兜底...")
        text, ocr_count, total_pages = _parse_pdf_ocr(path, max_pages=max_ocr_pages)
        if text and ocr_count > 0:
            # 标记走的是多模态还是 tesseract，给健康度面板区分
            is_multimodal_ocr = not any("tesseract" in m for m in report.get("methods", []))
            if is_multimodal_ocr:
                report["methods"].append(f"ocr(multimodal,{ocr_count}页)")
                report["warnings"].append(
                    f"PDF 通过多模态模型识别({ocr_count} 页)，"
                    f"如有复杂排版或手写内容，建议提供文本版 PDF 以获得最佳效果。"
                )
            else:
                report["methods"].append(f"ocr(tesseract,{ocr_count}页)")
                report["warnings"].append(
                    f"PDF 通过传统 OCR 识别({ocr_count} 页，引擎：tesseract)，"
                    f"中文识别率约 70%，表格结构可能丢失，查询结果准确性较低。"
                    f"建议提供文本版 PDF 替换，或确保多模态模型可用后重新入库。"
                )
            # 超页截断警告
            if total_pages > 50:
                report["warnings"].append(
                    f"PDF 共 {total_pages} 页，仅识别前 50 页，第 51~{total_pages} 页未入库。"
                    f"如需完整入库，请在知识库设置中调高 OCR 页数上限。"
                )
            return text
        report["errors"].append("OCR 也未能提取出文字，文件可能是空白扫描件或图片质量太差")
        if _resolve_tesseract_cmd() is None:
            report["warnings"].append(
                "传统 OCR(tesseract) 兜底当前不可用：未找到 tesseract 可执行文件，"
                "多模态识别失败时无法降级。可设置环境变量 TESSERACT_CMD 指向 "
                "tesseract 可执行文件，或将其加入 PATH。"
            )
        return ""
    except Exception as e:
        report["errors"].append(f"OCR 阶段失败: {e}")
        return ""


def _describe_image_multimodal(img_bytes: bytes, img_ext: str = "png") -> str:
    """增强模式辅助：调多模态模型生成图片内容描述。

    供 _parse_pdf_pdfplumber(嵌入图片)和 _parse_docx(inline shapes)使用。
    失败时静默返回空字符串，不影响主流程。
    """
    import os
    import concurrent.futures

    mime_map = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    }
    mime = mime_map.get(img_ext.lower().lstrip("."), "image/png")

    try:
        import base64
        import asyncio as _asyncio
        from core.provider import provider as _nano_provider
        img_b64 = base64.b64encode(img_bytes).decode()
        response = _asyncio.run(_nano_provider._client.messages.create(
            model=_vision_model(),
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
                {"type": "text", "text": (
                    "Describe this image in detail, including all visible text, data, charts, flowcharts, diagrams, and visual elements. "
                    "Output only the description. Do not add explanations or prefaces."
                )},
            ]}],
        ))
        if response.content:
            return response.content[0].text.strip()
    except Exception as e:
        logger.debug(f"[RAG] 图片描述生成失败: {e}")
    return ""


def _parse_image(path: str, report: Dict[str, Any]) -> str:
    """图片文件入库解析。

    用多模态模型生成图片的完整文字描述，走现有 chunk→chroma 流程。
    这样图片入库后可以被 RAG 检索——用户问"那张图里说了什么"，
    模型能通过描述文字召回对应图片的内容。

    支持格式：jpg/jpeg/png/webp/bmp/gif
    """
    import os

    suffix = pathlib.Path(path).suffix.lower()
    mime_type = IMAGE_MIME_MAP.get(suffix, "image/jpeg")

    try:
        import base64
        import asyncio as _asyncio
        from core.provider import provider as _nano_provider

        with open(path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode()

        prompt = (
            "Describe the full content of this image in detail, including:\n"
            "1. All visible text, transcribed completely.\n"
            "2. The main subject and visual content.\n"
            "3. If there are tables, output the full table content in markdown format.\n"
            "4. If there are charts, describe the chart data and meaning.\n"
            "Be detailed and complete so all useful image information is captured as text."
        )
        response = _asyncio.run(_nano_provider._client.messages.create(
            model=_vision_model(),
            max_tokens=2048,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": img_b64}},
                {"type": "text", "text": prompt},
            ]}],
        ))
        text = response.content[0].text if response.content else ""

        if text and text.strip():
            report["methods"].append(f"multimodal_image({mime_type})")
            report["warnings"].append(
                "图片通过多模态模型转换为文字描述入库，内容完整度取决于图片清晰度和模型理解能力。"
            )
            return f"[Image: {pathlib.Path(path).name}]\n{text.strip()}"
        else:
            report["errors"].append("多模态模型未能提取图片内容，图片可能为空或无法识别")
            return ""

    except Exception as e:
        report["errors"].append(f"图片解析失败: {e}")
        logger.warning(f"[RAG] 图片解析失败 {path}: {e}")
        return ""


def _parse_file(path: str, report: Dict[str, Any] | None = None, index_config: dict | None = None) -> str:
    """统一的文件解析入口。会把 method / warnings / errors 写到 report 里。

    Phase 1 修正：report 加默认值 None，方便 load_full_file 等不关心 report 的场景直接调。
    """
    if report is None:
        report = _new_report(pathlib.Path(path).name, path)

    suffix = pathlib.Path(path).suffix.lower()
    cfg = index_config or {}
    enhanced_mode = cfg.get("enhanced_mode", False)

    if suffix in [".txt", ".md"]:
        return _parse_txt_or_md(path, report)
    if suffix == ".pdf":
        return _parse_pdf(path, report, index_config=index_config)
    if suffix == ".docx":
        return _parse_docx(path, report, enhanced_mode=enhanced_mode)
    if suffix == ".pptx":
        return _parse_pptx(path, report, enhanced_mode=enhanced_mode)
    if suffix == ".xlsx":
        return _parse_xlsx(path, report)
    if suffix == ".xls":
        return _parse_xls(path, report)
    if suffix == ".csv":
        return _parse_csv(path, report)
    if suffix in IMAGE_EXTENSIONS:
        return _parse_image(path, report)

    report["errors"].append(f"不支持的文件类型: {suffix}")
    logger.warning(f"⚠️ [RAG] 不支持的文件类型: {suffix}")
    return ""


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 80) -> List[str]:
    """切块。表格整体作为独立 chunk，不参与硬切。

    设计要点：
    - pdfplumber / docx 解析后，表格以 "[Table]\\n..." 标记开头
    - 多张同结构表格(如通报里的"当月持续在营率"、"营内三晋率"等)
      如果跨 chunk 切块，模型会把不同表格的数字混搭，产生幻觉性数据错配
    - 解决方案：先按 [Table] 边界把文本分段，表格段整体作为一个 chunk，
      普通文字段落继续走 500 字符滑窗逻辑

    表格 chunk 超大时的处理：
    - 单张表格超过 chunk_size * 4(默认 2000 字符)时，按行切分
    - 每个子 chunk 保留 "[Table continued]" 前缀，避免模型丢失上下文
    """
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 4)
        logger.warning(f"⚠️ [RAG] overlap >= chunk_size，已强制回退到 {overlap}")

    text = text.strip()
    if not text:
        return []

    # ── 第一步：按 [Table] 标记分段 ────────────────────────────
    # 分割逻辑：遇到 "[Table]" 或 "[Table continued]" 开头的行，视为新表格段开始
    import re
    # 用正则把文本切成"普通段"和"表格段"交替的列表
    # 表格段以 [Table] 开头，到下一个 [Table] 或文本结束为止
    TABLE_MARKER = re.compile(r'(?=\[Table\])', re.MULTILINE)
    raw_segments = TABLE_MARKER.split(text)

    # raw_segments[0] 可能是表格前的普通文字，后续每段以 [Table] 开头
    chunks = []
    step = max(1, chunk_size - overlap)
    TABLE_SIZE_LIMIT = chunk_size * 4  # 超过此长度的表格才做行级切分

    for seg in raw_segments:
        seg = seg.strip()
        if not seg:
            continue

        if seg.startswith("[Table]") or seg.startswith("[Table:"):
            # ── 表格段：整体作为独立 chunk ──────────────────────
            if len(seg) <= TABLE_SIZE_LIMIT:
                # 表格不太大，整体一个 chunk
                chunks.append(seg)
            else:
                # 表格太大，按行切分，每块保留表头(第一行)
                lines = seg.split("\n")
                # 第一行是 "[Table]"，第二行通常是表头行
                header_lines = lines[:2]  # [Table] + 表头
                header_text = "\n".join(header_lines)
                body_lines = lines[2:]

                current_lines = list(header_lines)
                current_len = len(header_text)
                first_sub_chunk = True

                for line in body_lines:
                    line_len = len(line) + 1  # +1 for newline
                    if current_len + line_len > TABLE_SIZE_LIMIT and len(current_lines) > len(header_lines):
                        # 当前子 chunk 已满，输出
                        prefix = "[Table]\n" if first_sub_chunk else "[Table continued]\n"
                        chunk_text = prefix + "\n".join(current_lines[len(header_lines):])
                        chunks.append(chunk_text.strip())
                        first_sub_chunk = False
                        # 新子 chunk 从表头开始
                        current_lines = list(header_lines) + [line]
                        current_len = len(header_text) + line_len
                    else:
                        current_lines.append(line)
                        current_len += line_len

                # 输出最后一个子 chunk
                if len(current_lines) > len(header_lines):
                    prefix = "[Table]\n" if first_sub_chunk else "[Table continued]\n"
                    chunk_text = prefix + "\n".join(current_lines[len(header_lines):])
                    chunks.append(chunk_text.strip())

        else:
            # ── 普通文字段：走原来的滑窗逻辑 ───────────────────
            start = 0
            while start < len(seg):
                end = min(start + chunk_size, len(seg))
                chunk = seg[start:end].strip()
                if chunk:
                    chunks.append(chunk)
                if end >= len(seg):
                    break
                start += step

    return [c for c in chunks if c]


# ══════════════════════════════════════════════
# 公开 API
# ══════════════════════════════════════════════

def _generate_schema_chunk(file_path: pathlib.Path, raw_text: str) -> str:
    """从解析后的文本提取文件结构摘要，生成 [FileSchema] chunk。

    这个 chunk 描述文件的骨架，不包含具体数据：
    - XLSX：Sheet 名 + 每个 Sheet 的列名(第一行)
    - PDF/DOCX：章节标题 + 表格标题([Table: xxx] 标记)
    - CSV：列名
    - TXT/MD：所有 # 标题行

    存入 chroma 时 metadata 带 chunk_type=schema，
    RAG 召回时自然会把结构摘要 chunk 一起拉出来，
    模型同时看到"表2.4 = 认知轴"和具体数据，能完整定位和回答。
    """
    suffix = file_path.suffix.lower()
    filename = file_path.name
    lines = [f"[FileSchema] {filename}"]

    try:
        if suffix in (".xlsx", ".xls"):
            # 从 raw_text 里提取 [Sheet: xxx] 行和每个 Sheet 的列名(第一行 coordinate=A1 的那行)
            import re
            sheet_blocks = re.split(r'\[Sheet: ([^\]]+)\]', raw_text)
            # sheet_blocks: ['', sheet1_name, sheet1_content, sheet2_name, sheet2_content, ...]
            i = 1
            while i < len(sheet_blocks) - 1:
                sheet_name = sheet_blocks[i].strip()
                sheet_content = sheet_blocks[i + 1].strip() if i + 1 < len(sheet_blocks) else ""
                lines.append(f"\nSheet: {sheet_name}")
                # 取前几行，同时提取第1行(大类)和第2行(子类/子列名)。
                # 多行表头在中国业务 Excel 里非常常见：
                #   第1行：姓名 | 基本信息 | 基本信息 | 业绩
                #   第2行：     | 部门     | 职级     | 当月保费
                # 只取第1行会漏掉子列名，导致结构查询回答不完整。
                # 合并单元格被 openpyxl 填充为重复值，去重后保留唯一大类名。
                first_row_cells = []
                second_row_cells = []
                seen_first = set()   # 去重：合并单元格会导致同一大类名重复出现
                for row_line in sheet_content.split("\n")[:6]:
                    cells = row_line.split(" | ")
                    for cell in cells:
                        cell = cell.strip()
                        m1 = re.match(r'^[A-Z]+1=(.+)$', cell)
                        if m1:
                            val = m1.group(1).strip()
                            if val not in seen_first:
                                seen_first.add(val)
                                first_row_cells.append(val)
                        m2 = re.match(r'^[A-Z]+2=(.+)$', cell)
                        if m2:
                            val = m2.group(1).strip()
                            # 跳过与第1行完全相同的值(子行合并单元格填充导致的重复)
                            if val not in seen_first:
                                second_row_cells.append(val)
                if first_row_cells:
                    lines.append(f"Column names (row 1): {' | '.join(first_row_cells)}")
                if second_row_cells:
                    lines.append(f"Sub-column names (row 2): {' | '.join(second_row_cells)}")
                elif first_row_cells and not second_row_cells:
                    # 单行表头：保持与旧版兼容，但改用更清晰的"列名"描述
                    pass  # first_row_cells 已经输出，无需额外操作
                i += 2

        elif suffix in (".pdf", ".docx"):
            import re
            # 提取表格标题：[Table: xxx] 标记
            table_titles = re.findall(r'\[Table: ([^\]]+)\]', raw_text)
            seen_titles = set()
            for t in table_titles:
                t = t.strip()
                if t and t not in seen_titles:
                    seen_titles.add(t)
                    lines.append(f"Table: {t}")

            # 提取章节标题：docx 的标题段落通常是短行(< 60 字符)，不含数字表格符号
            # 从 raw_text 里找疑似标题行(不含 | 的短行)
            candidate_headings = []
            for line in raw_text.split("\n"):
                line = line.strip()
                if 4 <= len(line) <= 60 and "|" not in line and "[" not in line:
                    # 过滤纯数字行、纯标点行
                    if sum(c.isalpha() or '\u4e00' <= c <= '\u9fff' for c in line) >= 3:
                        candidate_headings.append(line)
            # 去重，最多保留20个
            seen_h = set()
            for h in candidate_headings[:40]:
                if h not in seen_h:
                    seen_h.add(h)
                    lines.append(f"Section/heading: {h}")
                if len(seen_h) >= 20:
                    break

        elif suffix == ".pptx":
            import re
            # pptx schema：提取每张幻灯片的标题（[Slide N] xxx 行）
            slide_titles = re.findall(r'\[Slide \d+\]\s*(.+)', raw_text)
            seen_st = set()
            for t in slide_titles:
                t = t.strip()
                if t and t not in seen_st:
                    seen_st.add(t)
                    lines.append(f"Slide title: {t}")
                if len(seen_st) >= 30:
                    break

        elif suffix == ".csv":
            # 第一行就是列名
            first_line = raw_text.strip().split("\n")[0] if raw_text.strip() else ""
            if first_line:
                lines.append(f"Column names: {first_line}")

        elif suffix in (".txt", ".md"):
            import re
            # 提取 markdown 标题行
            headings = re.findall(r'^#{1,4}\s+(.+)$', raw_text, flags=re.MULTILINE)
            for h in headings[:30]:
                lines.append(f"Heading: {h.strip()}")

    except Exception as e:
        logger.debug(f"[RAG] FileSchema 生成失败 {filename}: {e}")
        return ""

    if len(lines) <= 1:
        # 没有提取到任何结构信息，不生成 schema chunk
        return ""

    return "\n".join(lines)


def _index_one_file(file_path: pathlib.Path, collection, embedder, hash_store: Dict[str, str], parse_reports: Dict[str, Dict[str, Any]], index_config: dict | None = None) -> Tuple[str, Dict[str, Any]]:
    """处理单个文件：返回 (action, report)。

    action: "indexed" | "skipped" | "error"
    """
    path_str = str(file_path)
    report = _new_report(file_path.name, path_str)

    # 0. 大小检查
    ok_size, size_msg = _check_file_size(path_str)
    if not ok_size:
        report["errors"].append(size_msg)
        _set_status(report)
        parse_reports[path_str] = report
        logger.warning(f"⚠️ [RAG] 跳过超大文件 {file_path.name}: {size_msg}")
        return "error", report

    # 1. hash 增量检查(命中即跳过)
    try:
        current_hash = _file_hash(path_str)
    except Exception as e:
        report["errors"].append(f"读取文件失败: {e}")
        _set_status(report)
        parse_reports[path_str] = report
        return "error", report

    if hash_store.get(path_str) == current_hash and path_str in parse_reports:
        return "skipped", parse_reports[path_str]

    # 2. 解析
    text = _parse_file(path_str, report, index_config=index_config)
    if not text or not text.strip():
        # 解析层已经填好了 errors
        _set_status(report)
        parse_reports[path_str] = report
        return "error", report

    # 3. 切块
    chunks = _chunk_text(text)
    if not chunks:
        report["errors"].append("切块后为空")
        _set_status(report)
        parse_reports[path_str] = report
        return "error", report

    # 4. 入库(先删旧块，再加新块)
    try:
        _collection_delete_by_source(collection, path_str)

        # 生成结构摘要 chunk，和普通 chunk 一起入库
        schema_chunk = _generate_schema_chunk(file_path, text)
        all_chunks = chunks + ([schema_chunk] if schema_chunk else [])

        embeddings = embedder.encode(all_chunks, show_progress_bar=False).tolist()
        ids = [f"{file_path.stem}_{i}_{current_hash[:6]}" for i in range(len(all_chunks))]
        metadatas = []
        for i in range(len(chunks)):
            metadatas.append({"source": path_str, "filename": file_path.name, "chunk_index": i, "chunk_type": "content"})
        if schema_chunk:
            metadatas.append({"source": path_str, "filename": file_path.name, "chunk_index": len(chunks), "chunk_type": "schema"})

        collection.add(ids=ids, embeddings=embeddings, documents=all_chunks, metadatas=metadatas)
    except Exception as e:
        report["errors"].append(f"入库失败: {e}")
        _set_status(report)
        parse_reports[path_str] = report
        return "error", report

    # 5. 更新 hash + report
    hash_store[path_str] = current_hash
    report["chunks"] = len(chunks)  # 不计 schema chunk，保持统计数字一致
    _set_status(report)
    parse_reports[path_str] = report
    logger.info(f"✔ [RAG] 入库: {file_path.name} ({len(chunks)} 块 + {'1 schema' if schema_chunk else '0 schema'}，状态={report['status']})")
    return "indexed", report


def index_documents(folder: str = "data/knowledge", index_config: dict | None = None) -> Dict[str, Any]:
    """扫描目录，增量索引所有支持的文档。

    返回汇总统计 + 每个文件的 parse_report(保存到 PARSE_REPORTS 文件)。
    """
    root          = pathlib.Path(__file__).parent.parent
    knowledge_dir = root / folder
    os.makedirs(knowledge_dir, exist_ok=True)

    collection    = _get_collection()
    hash_store    = _load_hash_store()
    parse_reports = _load_parse_reports()
    stats         = {"indexed": 0, "skipped": 0, "errors": []}

    # 过滤 Office 临时锁文件(~$xxx.docx / ~$xxx.xlsx 等)以及隐藏文件
    files = [
        f for f in knowledge_dir.rglob("*")
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
        and not f.name.startswith("~$")
        and not f.name.startswith(".")
    ]

    # 清理 parse_reports 里那些源文件已不存在的记录
    valid_paths = {str(f) for f in files}
    obsolete = [k for k in parse_reports.keys() if k not in valid_paths]
    for k in obsolete:
        parse_reports.pop(k, None)

    if not files:
        logger.info(f"[RAG] 知识库目录为空: {knowledge_dir}，跳过嵌入模型预加载")
        _save_parse_reports(parse_reports)
        # 空库时不要为了 BM25/向量预热加载 BAAI/bge-m3；让应用先正常启动。
        if "bm25_index_built" not in _init_stage_log:
            _init_stage_log.append("bm25_index_built")
        return stats

    # 先用 hash 判断有没有文件真的需要重建索引。
    # 旧逻辑在这里无条件 _load_embedder()，即使知识库为空/全部已跳过，也会在启动时
    # 加载 2GB 级 bge-m3，Windows native/WebView2 环境下很容易导致进程直接退出。
    files_to_index = []
    for file_path in files:
        path_str = str(file_path)
        try:
            ok_size, size_msg = _check_file_size(path_str)
            if not ok_size:
                report = _new_report(file_path.name, path_str)
                report["errors"].append(size_msg)
                _set_status(report)
                parse_reports[path_str] = report
                stats["errors"].append({"file": file_path.name, "error": size_msg})
                logger.warning(f"⚠️ [RAG] 跳过超大文件 {file_path.name}: {size_msg}")
                continue
            current_hash = _file_hash(path_str)
            if hash_store.get(path_str) == current_hash and path_str in parse_reports:
                stats["skipped"] += 1
                continue
            files_to_index.append(file_path)
        except Exception as e:
            # 读文件/hash 失败也交给原单文件流程生成标准 report，避免吞错。
            files_to_index.append(file_path)

    if not files_to_index:
        logger.debug(f"[RAG] 知识库无变化，跳过嵌入模型预加载（{stats['skipped']} 个文件）")
        _save_parse_reports(parse_reports)
        try:
            _build_bm25_index()
        except Exception as e:
            logger.warning(f"[RAG] BM25 索引重建失败(跳过): {e}")
        if "bm25_index_built" not in _init_stage_log:
            _init_stage_log.append("bm25_index_built")
        return stats

    embedder = _load_embedder()

    for file_path in files_to_index:
        try:
            action, report = _index_one_file(file_path, collection, embedder, hash_store, parse_reports, index_config=index_config)
            if action == "indexed":
                stats["indexed"] += 1
            elif action == "skipped":
                stats["skipped"] += 1
            else:
                stats["errors"].append({"file": file_path.name, "error": "; ".join(report.get("errors", []))})
        except Exception as e:
            logger.error(f"❌ [RAG] 处理 {file_path.name} 异常: {e}")
            stats["errors"].append({"file": file_path.name, "error": str(e)})

    _save_hash_store(hash_store)
    _save_parse_reports(parse_reports)

    # Step 5.5：入库完成后重建 BM25 索引(增量 / 跳过都会触发，确保索引最新)
    try:
        _build_bm25_index()
    except Exception as e:
        logger.warning(f"[RAG] BM25 索引重建失败(跳过): {e}")

    return stats


def index_single_file(file_path: str, index_config: dict | None = None) -> Dict[str, Any]:
    """索引单个文件，供 UI 上传后直接调用。"""
    path   = pathlib.Path(file_path)
    stats  = {"indexed": 0, "skipped": 0, "errors": []}

    if not path.exists():
        stats["errors"].append({"file": path.name, "error": "文件不存在"})
        return stats
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        stats["errors"].append({"file": path.name, "error": f"不支持的格式: {path.suffix}"})
        return stats
    if path.name.startswith("~$") or path.name.startswith("."):
        stats["errors"].append({"file": path.name, "error": "临时/隐藏文件，已跳过"})
        return stats

    collection    = _get_collection()
    embedder      = _load_embedder()
    hash_store    = _load_hash_store()
    parse_reports = _load_parse_reports()

    try:
        action, report = _index_one_file(path, collection, embedder, hash_store, parse_reports, index_config=index_config)
        if action == "indexed":
            stats["indexed"] += 1
        elif action == "skipped":
            stats["skipped"] += 1
        else:
            stats["errors"].append({"file": path.name, "error": "; ".join(report.get("errors", []))})
        _save_hash_store(hash_store)
        _save_parse_reports(parse_reports)
    except Exception as e:
        logger.error(f"❌ [RAG] 单文件入库失败 {path.name}: {e}")
        stats["errors"].append({"file": path.name, "error": str(e)})

    # Step 5.5：单文件入库完成后重建 BM25 索引
    try:
        _build_bm25_index()
    except Exception as e:
        logger.warning(f"[RAG] BM25 索引重建失败(跳过): {e}")

    return stats


def _is_likely_scanned_pdf(path: str) -> bool:
    """快速检测 PDF 是否是扫描件（用 pypdf 读首页文字长度，毫秒级）。

    判断逻辑：首页提取出的文字 < 50 字符 → 视为扫描件。
    用于 lazy build 前给 UI 提示"接下来要跑 OCR，耗时较长"。
    """
    try:
        import pypdf
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            if len(reader.pages) == 0:
                return False
            first_page_text = reader.pages[0].extract_text() or ""
            return len(first_page_text.strip()) < 50
    except Exception:
        return False


def _ensure_all_temp_files_indexed(progress_callback=None):
    """Phase 3 · Lazy RAG build：对当前会话注册的临时文件按需建索引。

    关键设计（Phase 3 修正）：
    - 只处理通过 register_temp_file() 注册的文件，不扫整个临时目录。
    - 原因：扫目录会把历史遗留文件（上次会话未清理的大文件）全部 lazy build，
      导致每次 query_local_knowledge 都要等待几十秒的 embedding 计算。
    - 注册制保证：只有用户本轮会话明确上传的文件才参与 lazy build。

    幂等：已在 _temp_collection 里有 chunk 的文件直接跳过。

    progress_callback: Optional[Callable[[str], None]]
      在每个文件开始解析前调用，参数是状态描述文本。
      用于让 UI 把"思考中..."更新为更具体的进度文本（特别是扫描件 OCR）。
    """
    if not _registered_temp_files:
        return  # 当前会话没有注册任何临时文件，直接返回

    # 拿到已入索引的文件名集合
    indexed_filenames: set = set()
    if _temp_collection is not None:
        try:
            raw = _temp_collection.get(include=["metadatas"])
            for m in (raw.get("metadatas") or []):
                fname = m.get("filename", "")
                if fname:
                    indexed_filenames.add(fname)
        except Exception:
            pass

    # 只处理注册过但还没入索引的文件
    for filename, path_str in list(_registered_temp_files.items()):
        if filename in indexed_filenames:
            continue  # 已入索引，跳过
        if filename.lower().rsplit(".", 1)[-1] in {
            ext.lstrip(".") for ext in IMAGE_EXTENSIONS
        }:
            continue  # 图片不走 RAG
        p = pathlib.Path(path_str)
        if not p.exists():
            logger.debug(f"[RAG] 注册的临时文件不存在，跳过 lazy build: {filename}")
            continue

        # 进度提示：扫描件特别标注（OCR 耗时长）
        if progress_callback is not None:
            try:
                if filename.lower().endswith(".pdf") and _is_likely_scanned_pdf(path_str):
                    progress_callback(f"扫描件 OCR 解析中: {filename}（首次约 30-60 秒，请耐心等待）")
                else:
                    progress_callback(f"首次解析附件中: {filename}")
            except Exception:
                pass  # callback 异常不影响主流程

        try:
            logger.info(f"[RAG] Lazy build 临时文件: {filename}")
            index_temp_file(path_str)
        except Exception as e:
            logger.warning(f"[RAG] Lazy build 失败 {filename}: {e}")


def search(query: str, top_k: int = 3, min_score: float = 0.45, source: str = "both",
           progress_callback=None) -> List[Dict[str, Any]]:
    """向量检索 + BM25 检索 + RRF 融合 + cross-encoder 重排。

    source: "both" | "temp_only" | "persist_only"
      控制检索范围，供 query_for_agent 的 temp-first 路由使用。
      默认 "both" 保持原有行为。

    Phase 3：在 source 包含 temp 时，调 _ensure_all_temp_files_indexed()
    实现 lazy RAG build——上传只存盘，真正需要搜索时才建索引。
    """
    if not query or not isinstance(query, str):
        return []
    query = query.strip()
    if sum(1 for c in query if not c.isspace()) < MIN_QUERY_CHARS:
        return []

    # Phase 3：按需 lazy build 临时文件索引
    if source in ("both", "temp_only"):
        try:
            _ensure_all_temp_files_indexed(progress_callback=progress_callback)
        except Exception as e:
            logger.warning(f"[RAG] _ensure_all_temp_files_indexed 异常（跳过）: {e}")

    collection = _get_collection()
    persist_count = collection.count()
    temp_count_check = _temp_collection.count() if _temp_collection is not None else 0

    # 按 source 决定早退条件
    if source == "temp_only":
        if temp_count_check == 0:
            return []
    elif source == "persist_only":
        if persist_count == 0:
            return []
    else:  # both
        if persist_count == 0 and temp_count_check == 0:
            return []

    # ── 阶段 1：向量检索 ─────────────────────────────────────────────
    embedder  = _load_embedder()
    query_vec = embedder.encode([query], show_progress_bar=False).tolist()

    # wide_n 按 source 对应的实际 count 计算，避免 NameError
    if source == "temp_only":
        base_count = temp_count_check
    elif source == "persist_only":
        base_count = persist_count
    else:
        base_count = max(persist_count, temp_count_check)
    wide_n = min(max(top_k * 10, 30), base_count) if base_count > 0 else 30

    vec_ranking = []

    # 持久库向量检索
    if persist_count > 0 and source in ("both", "persist_only"):
        vec_results = collection.query(
            query_embeddings=query_vec,
            n_results=min(wide_n, persist_count),
            include=["documents", "metadatas", "distances"]
        )
        for doc, meta, dist in zip(
            vec_results["documents"][0],
            vec_results["metadatas"][0],
            vec_results["distances"][0]
        ):
            score = round(1.0 - dist, 4)
            if score >= min_score:
                vec_ranking.append({
                    "content":  doc,
                    "source":   meta.get("source", ""),
                    "filename": meta.get("filename", ""),
                    "score":    score,
                    "channel":  "vector",
                    "is_temp":  False,
                })

    # 临时库向量检索(临时内容不卡 min_score，见 search 设计说明)
    if _temp_collection is not None and source in ("both", "temp_only"):
        try:
            temp_count = _temp_collection.count()
            if temp_count > 0:
                temp_n = min(max(top_k * 5, 10), temp_count)
                temp_results = _temp_collection.query(
                    query_embeddings=query_vec,
                    n_results=temp_n,
                    include=["documents", "metadatas", "distances"]
                )
                for doc, meta, dist in zip(
                    temp_results["documents"][0],
                    temp_results["metadatas"][0],
                    temp_results["distances"][0]
                ):
                    score = round(1.0 - dist, 4)
                    vec_ranking.append({
                        "content":  doc,
                        "source":   meta.get("source", ""),
                        "filename": f"[临时]{meta.get('filename', '')}",
                        "score":    score,
                        "channel":  "vector",
                        "is_temp":  True,
                    })
        except Exception as e:
            logger.debug(f"[RAG] 临时知识库搜索失败(跳过): {e}")

    # ── 阶段 2：BM25 检索(如果可用，传入 source 过滤) ──────────────
    bm25_ranking = _bm25_search(query, top_k=wide_n, source=source) if _check_bm25_available() else []
    for r in bm25_ranking:
        r["channel"] = "bm25"

    # ── 阶段 3：RRF 融合 ─────────────────────────────────────────
    if bm25_ranking:
        fused = _rrf_fuse([vec_ranking, bm25_ranking])
        logger.debug(f"[RAG] hybrid: 向量 {len(vec_ranking)} 条 + BM25 {len(bm25_ranking)} 条 → 融合 {len(fused)} 条")
    else:
        fused = vec_ranking
        logger.debug(f"[RAG] vector-only: {len(vec_ranking)} 条候选")

    if not fused:
        return []

    # ── 阶段 4：Reranker 重排(如果可用)────────────────────────
    # 只对候选池前 rerank_n 个做重排：reranker 推理代价随候选数线性增长
    # 20 是个平衡：能覆盖 hybrid 的大多数有效召回，又不会让单次 search 慢到 5 秒+
    rerank_n = min(20, len(fused))
    if _check_reranker_available() and rerank_n > 1:
        to_rerank = fused[:rerank_n]
        reranked = _rerank(query, to_rerank)
        # 把重排后的块放回 fused 头部，未参与重排的保持原顺序
        fused = reranked + fused[rerank_n:]
        logger.debug(
            f"[RAG] rerank: 对前 {rerank_n} 个候选重排 | top1 rerank_score={fused[0].get('rerank_score', 0):.4f}"
        )

    # ── 阶段 5：文件多样性 ──────────────────────────────────────
    # 设计：同一文件最多允许 max_per_file 块进入输出。
    # 这样大表格文件可以贡献多块(覆盖跨 chunk 的表格行)，
    # 同时限制单文件霸占所有名额。
    # max_per_file=4：经过实测调整。chunk_size=500 时，大型表格(如10行×多列的市分公司数据)
    # 会被切成 3-4 块，必须允许同文件多块才能给出完整表格视图。
    # reranker 已经过滤掉无关 chunk，多块同文件不会带入噪声。
    max_per_file = 4
    output = [fused[0]]
    used_files: Dict[str, int] = {fused[0].get("filename", ""): 1}

    for c in fused[1:]:
        if len(output) >= top_k:
            break
        fn = c.get("filename", "")
        count = used_files.get(fn, 0)
        if count < max_per_file:
            output.append(c)
            used_files[fn] = count + 1

    # 如果还没满 top_k，放开 max_per_file 限制，用内容去重兜底
    if len(output) < top_k:
        already_in = {(c.get("filename", ""), c.get("content", "")[:50]) for c in output}
        for c in fused[1:]:
            if len(output) >= top_k:
                break
            key = (c.get("filename", ""), c.get("content", "")[:50])
            if key not in already_in:
                output.append(c)
                already_in.add(key)

    return output


def _format_parse_note_for_agent(note: str) -> str:
    """Convert Chinese parse_report notes into English only for model-facing context.

    The parse_report source text stays Chinese so the health/monitoring UI stays Chinese.
    """
    import re as _re

    s = str(note or "").strip()
    if not s:
        return ""
    if not any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return s

    m = _re.search(r"顺序提取漏内容\s*([0-9.]+%)", s)
    if m:
        return f"ordered extraction missed about {m.group(1)} of content, possibly due to text boxes or special layout; XML fallback was used."

    m = _re.search(r"包含\s*(\d+)\s*张图片，Nano 暂无法识别图片内容", s)
    if m:
        return f"contains {m.group(1)} image(s); Nano cannot recognize image content without enhanced mode"

    m = _re.search(r"已通过增强模式识别\s*(\d+)\s*张图片，另有\s*(\d+)\s*张浮动图片暂无法识别", s)
    if m:
        return (
            f"enhanced mode recognized {m.group(1)} image(s); "
            f"{m.group(2)} floating image(s) could not be recognized because text-wrapping layouts are not supported yet"
        )

    m = _re.search(r"包含\s*(\d+)\s*张图片，识别失败", s)
    if m:
        return f"contains {m.group(1)} image(s); recognition failed, possibly due to unsupported image format, API errors, or floating/text-wrapping layout"

    m = _re.search(r"此文件以图片内容为主（图片\s*(\d+)\s*张\s*/\s*文本块\s*(\d+)\s*个）", s)
    if m:
        return (
            f"this file is image-heavy ({m.group(1)} image(s) / {m.group(2)} text block(s)); "
            "much content may not have been read. Enhanced mode is strongly recommended."
        )

    m = _re.search(r"包含\s*(\d+)\s*个图表，Nano 已记录但无法提取图表数据", s)
    if m:
        return f"contains {m.group(1)} chart(s); Nano recorded their presence but could not extract chart data"

    m = _re.search(r"包含\s*(\d+)\s*个图表，Nano 已提取数据但忽略可视化部分", s)
    if m:
        return f"contains {m.group(1)} chart(s); Nano extracted cell data but ignored visual chart rendering"

    m = _re.search(r"(\d+)\s*个 cell 是公式但没有缓存计算结果", s)
    if m:
        return (
            f"{m.group(1)} cell(s) contain formulas without cached calculated values. "
            "Open and save the file in Excel, then re-index it. This often happens when the file was generated by another system and never saved in Excel."
        )

    m = _re.search(r"pdfplumber：(\d+)/(\d+) 页提取为空", s)
    if m:
        return f"pdfplumber extracted empty text from {m.group(1)}/{m.group(2)} page(s), but other pages had enough content"

    m = _re.search(r"pymupdf：(\d+)/(\d+) 页提取为空", s)
    if m:
        return f"pymupdf extracted empty text from {m.group(1)}/{m.group(2)} page(s) after pdfplumber fallback"

    m = _re.search(r"PDF 通过多模态模型识别\((\d+) 页\)", s)
    if m:
        return (
            f"PDF was recognized by multimodal OCR ({m.group(1)} page(s)). "
            "For complex layouts or handwriting, provide a text-based PDF for best results."
        )

    m = _re.search(r"PDF 通过传统 OCR 识别\((\d+) 页", s)
    if m:
        return (
            f"PDF was recognized by traditional OCR ({m.group(1)} page(s), engine: tesseract). "
            "Chinese recognition is about 70%; table structure may be lost and query accuracy may be lower. "
            "Provide a text-based PDF or make the multimodal model available, then re-index."
        )

    m = _re.search(r"PDF 共\s*(\d+)\s*页，仅识别前\s*50\s*页", s)
    if m:
        return (
            f"PDF has {m.group(1)} page(s), but only the first 50 page(s) were indexed; pages 51-{m.group(1)} were not indexed. "
            "Increase the OCR page limit in knowledge-base settings if full indexing is needed."
        )

    m = _re.search(r"文件\s*([0-9.]+)MB\s*超过\s*([0-9.]+)MB\s*上限", s)
    if m:
        return f"file size {m.group(1)}MB exceeds the {m.group(2)}MB limit"

    simple_map = {
        "python-docx 未安装": "python-docx is not installed",
        "python-pptx 未安装: pip install python-pptx": "python-pptx is not installed: pip install python-pptx",
        "openpyxl 未安装": "openpyxl is not installed",
        "pandas 未安装": "pandas is not installed",
        "pdfplumber 未安装": "pdfplumber is not installed",
        "docx 解析后内容为空": "docx parsed to empty content",
        "pptx 解析后内容为空": "pptx parsed to empty content",
        "xlsx 解析后内容为空(所有 sheet 都是空的或无法读取)": "xlsx parsed to empty content; all sheets were empty or unreadable",
        "csv 所有编码尝试都失败(utf-8 / gbk / gb2312)": "csv decoding failed for all attempted encodings: utf-8 / gbk / gb2312",
        "PDF 文本提取失败且 OCR 不可用(可能是扫描件)。安装 OCR：pip install pymupdf pytesseract，并安装 Tesseract 本体": "PDF text extraction failed and OCR is unavailable; this may be a scanned document. Install OCR support: pip install pymupdf pytesseract, and install the Tesseract binary.",
        "OCR 也未能提取出文字，文件可能是空白扫描件或图片质量太差": "OCR could not extract text; the file may be a blank scan or too low quality",
        "图片通过多模态模型转换为文字描述入库，内容完整度取决于图片清晰度和模型理解能力。": "Image content was converted into text by the multimodal model. Completeness depends on image clarity and model understanding.",
        "多模态模型未能提取图片内容，图片可能为空或无法识别": "multimodal model could not extract image content; the image may be empty or unrecognizable",
        "切块后为空": "chunking produced empty content",
    }
    if s in simple_map:
        return simple_map[s]

    prefix_map = {
        "docx 打开失败:": "failed to open docx file:",
        "pptx 打开失败:": "failed to open pptx file:",
        "xlsx 打开失败:": "failed to open xlsx file:",
        "xls 解析失败:": "xls parsing failed:",
        "csv 读取失败:": "csv read failed:",
        "csv 解析失败:": "csv parsing failed:",
        "OCR 阶段失败:": "OCR stage failed:",
        "图片解析失败:": "image parsing failed:",
        "不支持的文件类型:": "unsupported file type:",
        "读取文件失败:": "failed to read file:",
        "入库失败:": "indexing failed:",
        "无法读取文件大小:": "failed to read file size:",
    }
    for zh, en in prefix_map.items():
        if s.startswith(zh):
            return en + s[len(zh):]

    return s


def _build_parse_warning_block(report: Dict[str, Any]) -> str:
    """从 parse_report 的 warnings 列表生成给 Nano 看的局限说明块。

    有 warnings 时返回格式化字符串，无 warnings 时返回空字符串。
    调用方负责决定插入位置（全文加载：header 后；RAG：检索结果前）。
    """
    warnings = report.get("warnings", [])
    if not warnings:
        return ""
    lines_out = ["[File Parse Notes] The following limitations may affect answer accuracy. Be transparent with the user when relevant:"]
    for w in warnings:
        lines_out.append(f"  · {_format_parse_note_for_agent(w)}")
    return "\n".join(lines_out)


def _format_agent_result(results: List[Dict[str, Any]], source_type: str = "persist") -> str:
    """把 search 返回结果格式化为给 Agent 看的文本。

    source_type: "temp" | "persist"
    - temp    : header 标明来自上传文件，让模型不会把持久库内容混入
    - persist : 原有的【本地知识库检索结果】header
    """
    if not results:
        return (
            "[Local KB No Hit] Fragment search found no relevant content.\n"
            "Fragment search may miss content when the file exists but the relevant text was not included in matched chunks.\n"
            "Required next steps:\n"
            "1. Call list_knowledge_files to check whether the file exists in the knowledge base.\n"
            "2. If the file exists, call load_full_file to load the complete file before answering.\n"
            "3. Only if list_knowledge_files confirms the file does not exist may you tell the user the knowledge base did not contain it.\n"
            "Do not directly answer 'not found' before completing these checks."
        )

    if source_type == "temp":
        header = (
            "[Uploaded File Search Results]\n"
            "The content below comes from files uploaded in this conversation, not from the persistent knowledge base.\n\n"
        )
    else:
        header = "[Local KB Search Results]\n\n"

    # 收集本次检索涉及的文件 warnings，注入给 Nano
    _warn_block = ""
    try:
        _pr = _load_parse_reports()
        # parse_reports key 是绝对路径，result 里只有 filename，用 Path(k).name 匹配
        _pr_by_name: Dict[str, Dict[str, Any]] = {}
        for k, v in _pr.items():
            _pr_by_name[pathlib.Path(k).name] = v
        # 去掉 [临时] 前缀再匹配
        seen_files = set()
        file_warns: List[str] = []
        for r in results:
            fname = r.get("filename", "").replace("[临时]", "").strip()
            if fname and fname not in seen_files:
                seen_files.add(fname)
                rpt = _pr_by_name.get(fname)
                if rpt and rpt.get("warnings"):
                    for w in rpt["warnings"]:
                        file_warns.append(f"  · [{fname}] {_format_parse_note_for_agent(w)}")
        if file_warns:
            _warn_block = "[File Parse Notes]\n" + "\n".join(file_warns) + "\n\n"
    except Exception:
        pass  # warning 注入失败不影响主流程

    blocks = []
    for i, r in enumerate(results, start=1):
        tag = " ★ highest similarity" if i == 1 else ""
        blocks.append(
            f"[Knowledge Block {i}]{tag}\nSource: {r['filename']}  Similarity: {r['score']:.2f}\n{r['content']}"
        )

    return (
        header
        + _warn_block
        + "\n\n---\n\n".join(blocks)
        + "\n\n[Answer Rules]\n"
        "1. Prefer [Knowledge Block 1], because it has the highest similarity to the user's query.\n"
        "2. If [Knowledge Block 1] directly contains the key fact that answers the question, answer from that text. "
        "Do not use common knowledge, guess, or invent facts not present in the search results.\n"
        "3. If [Knowledge Block 1] is insufficient, use the supporting blocks. If none of the blocks directly answer the question, "
        "tell the user that the local knowledge base does not contain a clear answer. Do not guess.\n"
        "4. If multiple blocks come from the same file, treat it as a large table or long document and synthesize all blocks from that file. "
        "Do not answer from only the first block, or you may miss other rows or sections.\n"
        "5. If the user explicitly asks for a complete table, complete list, or all data, preserve all rows and columns from the retrieved content. "
        "Do not skip, merge, abbreviate, or replace rows with ellipses.\n"
        "6. If a result contains an [Image: filename] marker, treat it as a multimodal description of the image content. "
        "If it conflicts with other text, tell the user to verify the original file.\n"
        "7. If the retrieved fragments are not enough to answer fully, such as when the task needs whole-file context for summary, comparison, or reasoning, "
        "call load_full_file to load the complete file. If the filename is unclear, call list_knowledge_files first.\n"
        "8. Cross-turn anti-hallucination: these are fragment search results and may miss rows, columns, or sections. "
        "If the user later asks for complete content from this file, such as a full table, full list, whole section, all clauses, or a full quote, "
        "do not reconstruct or extrapolate from fragments. Call load_full_file again before answering."
    )


def query_for_agent(query: str, top_k: int = 6, progress_callback=None) -> str:
    """供 Agent 工具调用使用的入口。返回给模型看的格式化文本。

    temp-first 路由(方向B)：
    1. 临时库有内容（已索引 OR 磁盘上有文件）→ 先只搜临时库
       - Phase 3 关键修复：不再只看 _temp_collection.count()。
         Phase 3 后上传文件不自动建索引，collection.count() 永远是 0，
         旧逻辑会跳过临时库直接搜持久库，导致 lazy build 永远不被触发。
         现在同时检测磁盘临时目录，只要有文件就进临时库路径，
         search(source="temp_only") 内部会触发 _ensure_all_temp_files_indexed()。
       - 临时库有命中 → 只返回上传文件结果，不混入持久库
       - 临时库无命中(query 跟上传文件无关) → 落到持久库
    2. 临时库为空且磁盘无文件 → 直接搜持久库(原有行为)

    progress_callback: 透传给 search → _ensure_all_temp_files_indexed，
      让 UI 能在 lazy build / OCR 时显示进度文本。
    """
    try:
        temp_count = _temp_collection.count() if _temp_collection is not None else 0

        # Phase 3 补充：磁盘临时目录有文件时也走 temp-first 路径（即使 collection 还未建索引）
        has_temp_on_disk = False
        try:
            temp_dir = _temp_uploads_dir()
            if temp_dir.exists():
                has_temp_on_disk = any(
                    f.is_file()
                    and f.suffix.lower() in SUPPORTED_EXTENSIONS
                    and f.suffix.lower() not in IMAGE_EXTENSIONS
                    for f in temp_dir.iterdir()
                )
        except Exception:
            pass

        if temp_count > 0 or has_temp_on_disk:
            # 先只搜临时库（内部触发 lazy build）
            temp_results = search(query, top_k=top_k, source="temp_only", progress_callback=progress_callback)
            if temp_results:
                # 语义置信度检查。
                # temp-first 的初衷是"用户上传的文件优先"——但只有当临时库的结果
                # 真正与查询相关时才应优先。如果 rerank_score 极低(向量相似但语义不符),
                # 说明临时文件里根本没有答案,应该回落到持久库搜索。
                # 阈值 0.05:这个值来自实测——"六维框架"对 Term Paper 的 top1 = 0.0075,
                # 属于噪声命中;真正相关的命中通常 > 0.1。0.05 是保守的中间值。
                _TEMP_CONFIDENCE_THRESHOLD = 0.05
                _top_rerank = temp_results[0].get("rerank_score", None)
                if _top_rerank is not None and _top_rerank < _TEMP_CONFIDENCE_THRESHOLD:
                    # rerank 可用但置信度太低 → 临时库内容与查询无关,回落持久库
                    logger.debug(
                        f"[RAG] temp-first: 临时库命中 {len(temp_results)} 块，"
                        f"但 top1 rerank_score={_top_rerank:.4f} < {_TEMP_CONFIDENCE_THRESHOLD}，"
                        f"语义置信不足，回落持久库"
                    )
                    # 继续向下搜持久库
                elif _top_rerank is None and temp_results:
                    # reranker 不可用,无法判断置信度,沿用原有 temp-first 行为
                    logger.debug(f"[RAG] temp-first: 临时库命中 {len(temp_results)} 块（reranker不可用，沿用temp优先）")
                    return _format_agent_result(temp_results, source_type="temp")
                else:
                    # rerank 可用且置信度足够 → 真正相关,优先返回临时库结果
                    logger.debug(
                        f"[RAG] temp-first: 临时库命中 {len(temp_results)} 块，"
                        f"top1 rerank_score={_top_rerank:.4f} ✓，返回临时库结果"
                    )
                    return _format_agent_result(temp_results, source_type="temp")
            # 临时库无命中 → 落到持久库
            logger.debug("[RAG] temp-first: 临时库无命中，回落到持久库")

        persist_results = search(query, top_k=top_k, source="persist_only", progress_callback=progress_callback)
        return _format_agent_result(persist_results, source_type="persist")

    except Exception as e:
        logger.warning(f"[RAG] query_for_agent 检索异常: {e}")
        return f"[Local KB Search Error] {e}. Immediately use load_full_file to load the relevant file before answering. If the filename is unknown, call list_knowledge_files first. Do not answer from common knowledge."


# ══════════════════════════════════════════════
# 临时知识库（对话窗口上传的文件）
# ══════════════════════════════════════════════

def _get_temp_collection():
    """获取或创建内存临时collection。EphemeralClient不持久化，进程退出自动清空。"""
    global _temp_client, _temp_collection
    if _temp_collection is None:
        import chromadb
        from chromadb.config import Settings as _ChromaSettings
        _temp_client = chromadb.EphemeralClient(
            settings=_ChromaSettings(anonymized_telemetry=False),
        )
        _temp_collection = _temp_client.get_or_create_collection(
            name="nano_temp",
            metadata={"hnsw:space": "cosine"}
        )
        logger.info("✔ [RAG] 临时知识库 collection 就绪(内存)")
    return _temp_collection


def index_temp_file(file_path: str) -> Dict[str, Any]:
    """对话窗口上传文件入库到临时collection(内存)。

    跟知识库入库逻辑完全一致，区别只是存在内存、不持久化。
    会话结束/重置时调 clear_temp_knowledge() 清空。
    """
    path = pathlib.Path(file_path)
    stats = {"indexed": 0, "errors": [], "filename": path.name, "chunks": 0}

    if not path.exists():
        stats["errors"].append({"file": path.name, "error": "文件不存在"})
        return stats
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.suffix.lower() in IMAGE_EXTENSIONS:
        stats["errors"].append({"file": path.name, "error": f"不支持的格式: {path.suffix}"})
        return stats

    report = _new_report(path.name, str(path))
    try:
        text = _parse_file(str(path), report)
        if not text or not text.strip():
            stats["errors"].append({"file": path.name, "error": "; ".join(report.get("errors", ["解析内容为空"]))})
            return stats

        chunks = _chunk_text(text)
        if not chunks:
            stats["errors"].append({"file": path.name, "error": "切块后为空"})
            return stats

        collection = _get_temp_collection()
        embedder = _load_embedder()

        # 先删该文件的旧块(支持重复上传)
        try:
            existing = collection.get(where={"source": {"$eq": str(path)}})
            if existing and existing.get("ids"):
                collection.delete(ids=existing["ids"])
        except Exception:
            pass

        embeddings = embedder.encode(chunks, show_progress_bar=False).tolist()
        ids = [f"temp_{path.stem}_{i}" for i in range(len(chunks))]
        metadatas = [{"source": str(path), "filename": path.name, "chunk_index": i, "is_temp": True}
                     for i in range(len(chunks))]
        collection.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)

        stats["indexed"] = 1
        stats["chunks"] = len(chunks)
        logger.info(f"✔ [RAG] 临时入库: {path.name} ({len(chunks)} 块)")
    except Exception as e:
        logger.error(f"❌ [RAG] 临时入库失败 {path.name}: {e}")
        stats["errors"].append({"file": path.name, "error": str(e)})

    # 方向A：临时入库后重建 BM25，把临时 chunk 纳入关键词检索语料。
    # 否则临时文件只走向量检索，泛/短 query 召回不到(本次 bug 根因)。
    if stats.get("indexed"):
        try:
            _build_bm25_index()
        except Exception as e:
            logger.warning(f"[RAG] 临时入库后 BM25 重建失败(跳过): {e}")

    return stats


def cleanup_stale_temp_files():
    """一次性清理磁盘上残留的临时文件(历史 bug 遗留)。

    调用场景：
    1. 应用启动时自动调一次（orchestrator._init_rag_async 里调用）
    2. 用户手动在 Python 终端调用排查问题

    之所以需要这个函数：旧版 clear_temp_knowledge 只清 chroma collection，
    不清磁盘，导致每次 UI 关闭后 nano_temp_uploads/ 里一直积累文件。
    这个函数做一次性扫清，后续每次 clear_temp_knowledge 都会连带清磁盘。
    """
    try:
        import shutil
        temp_dir = _temp_uploads_dir()
        if not temp_dir.exists():
            logger.debug("[RAG] 临时目录不存在，无需清理")
            return 0
        files = list(temp_dir.iterdir())
        count = sum(1 for f in files if f.is_file())
        if count == 0:
            logger.info("[RAG] cleanup_stale_temp_files: 无残留文件")
            return 0
        shutil.rmtree(str(temp_dir))
        temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[RAG] cleanup_stale_temp_files: 已清理 {count} 个残留临时文件")
        return count
    except Exception as e:
        logger.warning(f"[RAG] cleanup_stale_temp_files 失败: {e}")
        return 0


def register_temp_file(filename: str, path: str):
    """Phase 3：app.py 上传文件后调用，把文件注册到当前会话临时文件映射。

    只有注册过的文件才会被 _ensure_all_temp_files_indexed() lazy build。
    这样历史遗留文件不会被意外索引。
    """
    global _registered_temp_files
    _registered_temp_files[filename] = str(path)
    logger.debug(f"[RAG] 临时文件已注册: {filename} → {path}")


def get_file_path(filename: str) -> Optional[str]:
    """查询文件名对应的真实磁盘路径(绝对路径字符串，找不到返回 None)。

    给模型用的"文件名→物理路径"翻译器。设计目的：让 Skill 能直接 read/open 文件，
    不需要关心是临时附件还是持久知识库——统一从这里拿真实路径。

    支持的输入：
      - "[临时]xxx.docx"  → 查 _registered_temp_files
      - "xxx.docx"        → 先查临时（不带前缀），再查持久知识库
      - "[临时]"前缀不分大小写

    返回：绝对路径字符串，或 None。

    设计原则：
    - 上层（orchestrator）拿到路径后，应直接传给 Skill 的 file_path 参数，
      不要再做任何路径拼接或前缀处理。
    - Skill 开发者完全不感知"临时 vs 持久"的区别，写得跟读普通文件一样。
    """
    if not filename:
        return None

    # 剥前缀 [临时] / [temp]（不区分大小写）
    raw = filename.strip()
    if raw.startswith("[临时]"):
        bare = raw[len("[临时]"):]
        # 临时文件必带前缀，优先查注册表
        path = _registered_temp_files.get(bare)
        if path and pathlib.Path(path).exists():
            return str(pathlib.Path(path).resolve())
        return None  # 带前缀但找不到，不再降级到持久库

    # 不带前缀：先查临时（兼容模型偶尔忘加前缀），再查持久库
    path = _registered_temp_files.get(raw)
    if path and pathlib.Path(path).exists():
        return str(pathlib.Path(path).resolve())

    # 查持久知识库
    persist_candidate = _PERSIST_KNOWLEDGE_DIR / raw
    if persist_candidate.exists():
        return str(persist_candidate.resolve())

    return None


def get_file_path_for_agent(filename: str) -> str:
    """get_file_path 的 Agent 友好版：返回格式化文本，包含路径或失败提示。"""
    path = get_file_path(filename)
    if path:
        return (
            f"[File Path] {filename} -> {path}\n\n"
            f"[Usage Hint] This is a real absolute disk path. Pass it directly to a local Skill's file_path parameter. "
            f"The Skill can read it with standard methods such as open(), pandas.read_excel(), or pypdf.PdfReader()."
        )
    # 失败时给出可用文件清单
    try:
        files_summary = list_knowledge_files_for_agent()
    except Exception:
        files_summary = ""
    return (
        f"[Path Resolution Failed] No disk path was found for file \"{filename}\".\n"
        f"Possible reasons: 1) the filename is misspelled; 2) a temporary attachment is missing the [临时] prefix; "
        f"3) the file has been cleaned up.\n\n"
        + files_summary
    )


def clear_temp_knowledge():
    """清空临时知识库(会话重置时调用)。

    清理三层状态：
    1. chroma EphemeralClient collection(内存)
    2. 磁盘临时目录 nano_temp_uploads/（关键：进程退出不会自动删）
    3. BM25 索引重建

    历史 bug：旧版只清了层 1，磁盘文件跨会话永久残留，
    导致新开 UI / 新 Python 进程仍能在 list_knowledge_files 里看到
    上次会话遗留的临时文件。
    """
    global _temp_client, _temp_collection

    # 层 1：清 chroma collection
    if _temp_collection is not None:
        try:
            _temp_client.delete_collection("nano_temp")
        except Exception:
            pass
        _temp_collection = None
        logger.info("[RAG] 临时知识库 chroma collection 已清空")

    # 层 2：清磁盘临时目录（无论 collection 是否存在都要清）
    # 这是跨会话残留的根本原因——进程退出不会自动删磁盘文件
    try:
        import shutil
        temp_dir = _temp_uploads_dir()
        if temp_dir.exists():
            shutil.rmtree(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[RAG] 临时目录已清空: {temp_dir}")
    except Exception as e:
        logger.warning(f"[RAG] 临时目录清理失败(跳过): {e}")

    # 层 3：清空当前会话注册表
    global _registered_temp_files
    _registered_temp_files = {}
    logger.info("[RAG] 临时文件注册表已清空")

    # 层 3：重建 BM25，把临时 chunk 从关键词语料里移除
    try:
        _build_bm25_index()
    except Exception as e:
        logger.warning(f"[RAG] 临时库清空后 BM25 重建失败(跳过): {e}")


def remove_temp_file(filename: str) -> bool:
    """从临时知识库移除单个文件的所有 chunk，并重建 BM25。

    供 app.py 的 badge「X」删除按钮调用。封装临时 collection 的删除细节，
    让 UI 层不直接触碰 rag 内部状态(_temp_collection)。
    """
    if _temp_collection is None:
        return False
    try:
        existing = _temp_collection.get(where={"filename": {"$eq": filename}})
        if existing and existing.get("ids"):
            _temp_collection.delete(ids=existing["ids"])
            logger.info(f"[RAG] 临时文件已移除: {filename}")
        # 删除后重建 BM25，把该文件的临时 chunk 从关键词语料里清掉
        try:
            _build_bm25_index()
        except Exception as e:
            logger.warning(f"[RAG] 移除临时文件后 BM25 重建失败(跳过): {e}")
        return True
    except Exception as e:
        logger.warning(f"[RAG] 移除临时文件失败 {filename}: {e}")
        return False


def get_temp_stats() -> Dict[str, Any]:
    """返回临时知识库状态(供UI显示当前会话上传的文件)。"""
    if _temp_collection is None:
        return {"total_chunks": 0, "files": []}
    try:
        total = _temp_collection.count()
        raw = _temp_collection.get(include=["metadatas"])
        seen = {}
        for meta in raw.get("metadatas", []):
            fname = meta.get("filename", "")
            if fname and fname not in seen:
                seen[fname] = 0
            if fname:
                seen[fname] += 1
        return {"total_chunks": total, "files": [{"filename": k, "chunks": v} for k, v in seen.items()]}
    except Exception:
        return {"total_chunks": 0, "files": []}



def get_stats() -> Dict[str, Any]:
    """返回知识库基础统计。"""
    try:
        collection = _get_collection()
        count      = collection.count()
        hash_store = _load_hash_store()
        file_names = [pathlib.Path(f).name for f in hash_store.keys()]
        return {
            "total_chunks": count,
            "total_files":  len(hash_store),
            "file_names":   file_names,
            "ready":        count > 0
        }
    except Exception as e:
        return {"total_chunks": 0, "total_files": 0, "file_names": [], "ready": False, "error": str(e)}


def get_health_report() -> Dict[str, Any]:
    """Step 7：返回完整的知识库健康度报告，给 UI 健康度面板用。

    颜色分级(三色)：
    - 🔴 error  ：文件完全没入库，或解析全部失败。用户需要处理才能使用该文件。
    - 🟡 warning：文件已入库但存在风险，如 OCR 识别率低、含图片/图表无法读取等。
                  Nano 能回答但答案可能不完整或有偏差，需告知用户。
    - 🟢 ok     ：文件正常入库，无已知风险。

    输出结构：
    {
        "summary": {
            "ok": N, "warning": N, "error": N, "total": N, "total_chunks": N,
            "status_color": "green" | "yellow" | "red",  # 整体健康度颜色
            "status_text": "知识库健康",                  # 整体健康度文案
        },
        "files": [
            {
                ...原有字段...,
                "risk_level": "ok" | "warning" | "error",
                "risk_color": "#34d399" | "#f59e0b" | "#f43f5e",  # 直接给 UI 用的颜色值
                "risk_tips": ["提示1", "提示2"],  # 给用户看的风险说明，空列表=无风险
            }
        ]
    }
    """
    parse_reports = _load_parse_reports()
    # _system 子目录下的文件（如 Nano 操作手册）不出现在健康度面板——
    # 它们对用户不可见、不可删除，也不需要出现在健康度统计里。
    # parse_reports 的 key 是绝对路径，过滤掉路径里包含 _system 目录的记录。
    _sep = os.sep
    files = [
        r for r in parse_reports.values()
        if "_system" + _sep not in r.get("path", "")
        and "/_system/" not in r.get("path", "")
    ]

    summary = {"ok": 0, "warning": 0, "error": 0, "total": len(files), "total_chunks": 0}
    for r in files:
        status = r.get("status", "error")
        if status in summary:
            summary[status] += 1
        summary["total_chunks"] += int(r.get("chunks") or 0)

    # 整体健康度颜色：有 error 就红，有 warning 就黄，全 ok 就绿
    if summary["error"] > 0:
        summary["status_color"] = "red"
        summary["status_text"] = f"有 {summary['error']} 个文件入库失败，需要处理"
    elif summary["warning"] > 0:
        summary["status_color"] = "yellow"
        summary["status_text"] = f"有 {summary['warning']} 个文件存在风险提示"
    else:
        summary["status_color"] = "green"
        summary["status_text"] = "知识库健康"

    # 给每个文件附加 risk_level / risk_color / risk_tips
    COLOR_MAP = {
        "ok":      "#34d399",  # 绿
        "warning": "#f59e0b",  # 黄
        "error":   "#f43f5e",  # 红
    }

    for r in files:
        status = r.get("status", "error")
        r["risk_level"] = status
        r["risk_color"] = COLOR_MAP.get(status, "#f43f5e")

        # 生成 risk_tips：把 errors + warnings 整合成给用户看的提示
        tips = []
        methods = r.get("methods", [])
        errors = r.get("errors", [])
        warnings = r.get("warnings", [])

        # error 级别提示
        for e in errors:
            tips.append(f"❌ {e}")

        # warning 级别提示(精简版，不全文复述)
        for w in warnings:
            tips.append(f"⚠️ {w}")

        # 特殊标记：OCR 入库的文件，风险提示要更显眼
        is_multimodal_ocr = any("ocr(multimodal" in m.lower() for m in methods)
        is_tesseract_ocr = any("ocr(tesseract" in m.lower() for m in methods)
        if is_multimodal_ocr and status == "warning":
            tips.insert(0, "⚠️ 此文件通过多模态模型识别入库，内容完整性较好，但复杂图表可能有遗漏。建议优先使用文本版文件。")
        elif is_tesseract_ocr and status == "warning":
            tips.insert(0, "⚠️ 此文件通过传统 OCR 识别(tesseract)入库，中文识别率约 70%，表格结构可能丢失，查询结果准确性较低。建议提供文本版 PDF 替换，或重新入库以启用多模态识别。")

        r["risk_tips"] = tips

    # 按状态排序：error 优先，warning 次之，ok 最后；同状态按 filename
    status_order = {"error": 0, "warning": 1, "ok": 2}
    files.sort(key=lambda r: (status_order.get(r.get("status", "error"), 3), r.get("filename", "")))

    return {"summary": summary, "files": files}


def delete_file(filename: str) -> bool:
    """从向量库删除指定文件的所有块，并清理 parse_report。"""
    try:
        collection    = _get_collection()
        hash_store    = _load_hash_store()
        parse_reports = _load_parse_reports()

        target_path = None
        for path_str in list(hash_store.keys()):
            if pathlib.Path(path_str).name == filename:
                target_path = path_str
                break
        if not target_path:
            return False

        # 防御：即使有什么路径绕过了 list_knowledge_files 的过滤
        # (比如模型直接调用 delete_file)，"_system" 子目录下的文件
        # 也拒绝删除——这是随程序分发的文档（操作手册等），不属于用户可管理的
        # 知识库文件。
        if pathlib.Path(target_path).parent.name == "_system":
            logger.warning(f"[RAG] 拒绝删除系统文件: {filename}")
            return False

        _collection_delete_by_source(collection, target_path)
        del hash_store[target_path]
        parse_reports.pop(target_path, None)
        _save_hash_store(hash_store)
        _save_parse_reports(parse_reports)
        logger.info(f"✔ [RAG] 已删除: {filename}")

        # Step 5.5：删文件后重建 BM25 索引
        try:
            _build_bm25_index()
        except Exception as e:
            logger.warning(f"[RAG] BM25 索引重建失败(跳过): {e}")

        return True
    except Exception as e:
        logger.error(f"❌ [RAG] 删除失败 {filename}: {e}")
        return False


# ══════════════════════════════════════════════
# Phase 1：全文加载层(Data Access Layer)
# ══════════════════════════════════════════════
# 设计要点：
# - load_full_file 与 query_local_knowledge 是平级的两条数据访问通道
# - RAG 通道(query_local_knowledge)适合「在大库里找事实片段」
# - 全文加载(load_full_file)适合「完整理解一份文件做推理」
# - 两者并列挂在 orchestrator 的环 3，由模型自主选
# - load_full_file 默认不调多模态(零成本)；模型看到"含 N 张图片未识别"
#   提示后可自主用 with_images=True 重新调用
# ══════════════════════════════════════════════

# 持久库文件目录(供 _resolve_file_path 用)
_PERSIST_KNOWLEDGE_DIR = pathlib.Path(__file__).parent.parent / "data" / "knowledge"

# 临时附件目录(跟 app.py 里 _handle_chat_upload 写入的位置保持一致)
# 临时文件路径在 _temp_collection.metadatas[*].source 里也能拿到，这里给 fallback 用
def _temp_uploads_dir() -> pathlib.Path:
    import tempfile
    return pathlib.Path(tempfile.gettempdir()) / "nano_temp_uploads"


def _resolve_file_path(filename: str) -> Tuple[Optional[pathlib.Path], str]:
    """把模型传进来的 filename 解析成磁盘路径 + 来源标识。

    返回 (path, source) 二元组：
      - path: pathlib.Path 或 None(找不到)
      - source: "persist" | "temp" | "unknown"

    支持的 filename 形式：
      - "xxx.pdf"           → 先找持久库，再找临时库
      - "[临时]xxx.pdf"     → 强制找临时库，去前缀后定位
      - 绝对路径            → 直接用(防御性，不推荐)
    """
    if not filename or not isinstance(filename, str):
        return None, "unknown"

    raw = filename.strip()

    # 绝对路径直接用(模型一般不会这么传，但兜底)
    if os.path.isabs(raw):
        p = pathlib.Path(raw)
        if p.exists():
            return p, "temp" if str(p).startswith(str(_temp_uploads_dir())) else "persist"
        return None, "unknown"

    # [临时] 前缀 → 强制走临时库
    if raw.startswith("[临时]"):
        bare = raw[len("[临时]"):].strip()
        # 先在临时目录里找
        temp_dir = _temp_uploads_dir()
        if temp_dir.exists():
            candidate = temp_dir / bare
            if candidate.exists():
                return candidate, "temp"
        # 退而求其次，从 _temp_collection 的 metadatas 里找(已入索引的文件)
        if _temp_collection is not None:
            try:
                raw_meta = _temp_collection.get(include=["metadatas"])
                for m in raw_meta.get("metadatas", []):
                    if m.get("filename") == bare:
                        src = m.get("source", "")
                        if src and pathlib.Path(src).exists():
                            return pathlib.Path(src), "temp"
            except Exception:
                pass
        return None, "unknown"

    # 无前缀 → 优先持久库，再临时库
    persist_candidate = _PERSIST_KNOWLEDGE_DIR / raw
    if persist_candidate.exists():
        return persist_candidate, "persist"

    # 持久库找不到 → 临时库
    temp_dir = _temp_uploads_dir()
    if temp_dir.exists():
        temp_candidate = temp_dir / raw
        if temp_candidate.exists():
            return temp_candidate, "temp"

    # 从 _temp_collection 的 metadatas 里找
    if _temp_collection is not None:
        try:
            raw_meta = _temp_collection.get(include=["metadatas"])
            for m in raw_meta.get("metadatas", []):
                if m.get("filename") == raw:
                    src = m.get("source", "")
                    if src and pathlib.Path(src).exists():
                        return pathlib.Path(src), "temp"
        except Exception:
            pass

    return None, "unknown"


def _count_images_in_file(path: pathlib.Path) -> int:
    """统计文件里的图片数量(供 load_full_file 提示用)。

    只对 docx / pdf 有意义；其他格式返回 0。
    实现非常轻量：不解析图片内容，只数引用。
    """
    suffix = path.suffix.lower()

    if suffix == ".docx":
        try:
            import docx
            from docx.oxml.ns import qn
            doc = docx.Document(str(path))
            drawings = doc.element.body.findall(".//" + qn("w:drawing"))
            return len(drawings)
        except Exception:
            return 0

    if suffix == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            count = 0
            for page in doc:
                images = page.get_images(full=True) or []
                # 简单尺寸过滤：< 200px 的小图(logo/水印)不算
                for img_info in images:
                    xref = img_info[0]
                    try:
                        rects = page.get_image_rects(xref)
                        if rects and rects[0].width >= 200 and rects[0].height >= 200:
                            count += 1
                    except Exception:
                        continue
            doc.close()
            return count
        except Exception:
            return 0

    if suffix == ".pptx":
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
            prs = Presentation(str(path))
            count = sum(
                1 for slide in prs.slides
                for shape in slide.shapes
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
            )
            return count
        except Exception:
            return 0

    return 0


def load_full_file(filename: str, with_images: bool = False, progress_callback=None) -> str:
    """全文加载入口：读取一份完整文件的内容，不走 RAG 切块。

    适用场景：用户要总结/分析/改写/评估/对照某份文件，或基于文件做推理判断。
    跟 query_local_knowledge 的区别：本接口读完整文件，适合整体理解；
    后者是片段检索，适合查找具体事实。

    参数：
      filename          - 文件名(支持 [临时] 前缀指代对话附件)
      with_images       - 是否调多模态识别文档内嵌图片(默认 False，零成本路径)
                          扫描型 PDF 不依赖此参数(_parse_pdf 三层 fallback 自动 OCR)
                          仅影响 docx/文本型 PDF 里的嵌入图片识别
      progress_callback - 可选回调，在耗时解析(扫描件 OCR)前调一次，
                          UI 用它把"思考中..."改成具体进度文本

    返回：纯文本字符串。失败时返回带建议的错误提示(供模型自纠错)。

    已知局限：
    - with_images=True 走 _describe_image_multimodal 生成文字描述，
      细节型问题(如"图里第三个分支")可能不准。
      未来若实测不够，可升级到 base64 inline 模式。
    - ~~LOAD_FULL_MAX_CHARS=30 万字符的超量保护~~ **已取消**
      （见该常量处的墓碑）。本函数现在**返回完整文本，不再截断**；
      「一次给模型多少」由 `core.reading` 在上层决定。
      📌 那正是这条已知局限自己指出的方向：「未对齐到具体模型上下文窗口」——
         而对齐它的正确做法不是调这个数，是**把决定权交给读取方**。
    """
    # 1. 输入校验
    if not filename or not isinstance(filename, str):
        return "[Load Failed] Invalid filename parameter. Call list_knowledge_files to view available files."

    # 2. 路径解析
    path, source = _resolve_file_path(filename)
    if path is None:
        try:
            # 🐛 这份列表是**给模型看的**（load_full_file 失败时回灌）——
            #    原本排除了 `_system`，于是模型被告知"手册不存在"然后放弃。
            #    📌 同一个受众必须用同一套可见性规则，不能一处开一处关。
            available = list_knowledge_files(include_system=True)
            avail_names = ", ".join(f.get("filename", "") for f in available) if available else "(none)"
        except Exception:
            avail_names = "(failed to list files)"
        return (
            f"[Load Failed] File \"{filename}\" was not found.\n"
            f"Available files: {avail_names}\n"
            f"Call again with the exact filename, or call list_knowledge_files to view the full list. "
            f"Temporary attachment filenames keep the [临时] prefix."
        )

    # 3. 大小检查
    ok_size, size_msg = _check_file_size(str(path))
    if not ok_size:
        return f"[Load Failed] {_format_parse_note_for_agent(size_msg)}. Use query_local_knowledge to search specific content in this file instead."

    # 4. 图片格式直接走多模态描述(复用 _parse_image)
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        report = _new_report(path.name, str(path))
        text = _parse_image(str(path), report)
        if not text:
            errs = "; ".join(_format_parse_note_for_agent(e) for e in report.get("errors", ["image parsing failed"]))
            return f"[Load Failed] Image parsing failed: {errs}"
        return text

    # 5. 文档解析(复用 _parse_file)
    index_config = {
        "enhanced_mode": bool(with_images),
        # max_ocr_pages 给个偏宽松的默认值——全文加载场景比 RAG 入库容忍更多页数
        "max_ocr_pages": 100,
    }

    # 解析前 progress 提示：扫描件单独标注 OCR 较慢
    if progress_callback is not None:
        try:
            if suffix == ".pdf" and _is_likely_scanned_pdf(str(path)):
                progress_callback(f"扫描件 OCR 解析中: {filename}（约 30-60 秒，请耐心等待）")
            else:
                progress_callback(f"全文加载中: {filename}")
        except Exception:
            pass

    # 用真实 report 捕获解析过程中产生的 warnings
    _full_report = _new_report(path.name, str(path))
    try:
        text = _parse_file(str(path), report=_full_report, index_config=index_config)
    except Exception as e:
        logger.warning(f"[RAG] load_full_file 解析异常 {path.name}: {e}")
        return (
            f"[Load Failed] File \"{filename}\" failed to parse: {e}. "
            "Immediately use query_local_knowledge to search this file before answering the user. "
            "Do not directly tell the user that full-file loading failed unless the fallback search also fails."
        )

    if not text or not text.strip():
        return (
            f"[Load Failed] File \"{filename}\" is empty or failed to parse. "
            f"Possible reasons: corrupted file, unsupported format, or poor scan quality. "
            f"Immediately use query_local_knowledge to search this file. "
            f"Do not directly tell the user the file is empty unless the fallback search also fails."
        )

    # 🪦 **这里曾是「超量保护」：文件 > `LOAD_FULL_MAX_CHARS`（300,000 字符）
    #    就降级成「章节标题 + 前 5000 字预览 + 建议改用 RAG」。2026-08-26 删除。**
    #
    # 🔴 删的理由不是「上限太小」，是**它护错了位置**：
    #      真正贵的是【解析】（docx/pdf → 文本，注释原话「峰值内存可能十倍于
    #      文件本身」）；而**已经解析出来的文本**留在内存里是廉价的
    #      （一份 50 万字符的文档 = 0.5 MB）。
    #    ⇒ 这道闸砍的是**解析之后**的文本 —— 📌 **峰值内存那一刻已经过去了，
    #      砍它救不了内存，只是让上层拿不到数据。**
    #    ⭐ 护内存的闸是 `MAX_FILE_SIZE_MB = 50`，它在**解析之前**，位置是对的，保留。
    #
    # 🔴🔴 而它与迭代阅读**互斥**：
    #      它的世界观是「读不完就别读了，去用 RAG」（原文案第 4 条就是这么写的），
    #      而迭代阅读的整个前提是「**用分片读取替代一次性注入**」。
    #      ⇒ 留着它，迭代阅读**永远读不到超大文件的正文** —— 而超大文件
    #        恰恰是迭代阅读唯一的服务对象。
    #    ⚠️ 顺带暴露一件事：当时的验收案例是「一份 **20 万字**的文档」，
    #       中文 20 万字 ≈ 20 万字符，**刚好卡在 300,000 以下** ——
    #       📌 **验收碰巧能过，而比它大一点的文件整个读不到。**
    #          一个「刚好够用」的阈值，会让验收通过而能力不成立。
    #
    # ⚠️ 上层现在的保护是迭代阅读的三道：`READ_STEP_CHARS`（默认给多少）、
    #    `MAX_READ_CHARS`（单次硬上限）、`peek_file`（大文件必须先试读）——
    #    📌 **「上下文吃不下」这个问题被移到了正确的层：给多少由读取方决定，
    #       而不是由「文件有多大」决定。**

    # ⚠️ `char_count` 原来是在上面那段被删掉的降级逻辑里赋的值，
    #    下面的 header 一直在用它 —— 📌 **删一段代码时，要先问「有没有别人
    #    在用它顺手算出来的东西」**。这次是 NameError 当场炸，属于运气好的那种；
    #    如果它算的是个会被静默用错的值，就不会有人发现。
    char_count = len(text)

    # 7. 顺手加个来源标记，让模型知道文件出处
    # 加上页数信息，让模型回答"这文件多少页"时不必再调工具
    try:
        _pages = _count_pages(path)
    except Exception:
        _pages = None
    _page_suffix = f", {_pages} pages" if _pages is not None else ""
    header = f"[Full File Content] {filename} ({source}, {char_count:,} chars{_page_suffix})\n{'─' * 40}\n"

    # 8. 文件解析局限说明：有 warnings 时注入到 header 之后，让 Nano 在读内容前就知道局限
    _warn_block = _build_parse_warning_block(_full_report)
    if _warn_block:
        return header + _warn_block + "\n" + "─" * 40 + "\n" + text
    return header + text


def list_knowledge_files(include_system: bool = False) -> List[Dict[str, Any]]:
    """列出当前可访问的所有文件(持久库 + 临时库)。

    返回格式：
      [
        {"filename": "章程.pdf", "source": "persist", "size_kb": 123, "indexed": True},
        {"filename": "[临时]员工表.xlsx", "source": "temp", "size_kb": 45, "indexed": True},
        ...
      ]

    临时文件强制加 [临时] 前缀，方便模型在调 load_full_file 时正确指代。

    设计：
    - 持久库：直接扫 _PERSIST_KNOWLEDGE_DIR 目录（不依赖 hash_store）
      hash_store 只用来标记文件是否已完成向量索引
    - 临时库：扫 _temp_uploads_dir() 目录 + _temp_collection 补全
    - 两处都做文件名去重（同名只留一条）

    Phase 1 修正历史：
    - 旧版只读 hash_store，导致"文件存在但未入索引时列表为空"
    - load_full_file 直接扫目录能找到，但 list 找不到，两者不一致
    - 现在统一为"以文件系统为准，hash_store 只做状态标注"
    """
    result: List[Dict[str, Any]] = []

    # 1. 持久库：直接扫知识库目录，hash_store 只用来标记是否已索引
    try:
        hash_store = _load_hash_store()
        # hash_store key 是绝对路径，建立 filename → path 的映射用于 indexed 标注
        indexed_filenames: set = set()
        for path_str in hash_store.keys():
            indexed_filenames.add(pathlib.Path(path_str).name)

        persist_dir = _PERSIST_KNOWLEDGE_DIR
        if persist_dir.exists():
            seen_persist: Dict[str, Dict[str, Any]] = {}
            for f in persist_dir.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if f.name.startswith("~$") or f.name.startswith("."):
                    continue
                # "_system" 子目录用于存放随程序分发的文档（Nano 自己的操作手册）：
                # 仍然走正常的 rglob 索引流程(query_local_knowledge 能检索到)，
                # 但不出现在这份"给用户看的文件列表"里——不会被显示在
                # KB 抽屉的文件列表中，也就不会被删除按钮删掉。
                try:
                    _rel_parts = f.relative_to(persist_dir).parts[:-1]
                except ValueError:
                    _rel_parts = ()
                # ⚠️ 两个受众、两套规则：用户抽屉要藏（防误删），
                #    Nano 要看见（那是它自己的手册）。
                #    📌 默认仍是藏 —— 抽屉 / 删除按钮 / 治理豁免一个字不动。
                _is_system = "_system" in _rel_parts
                if _is_system and not include_system:
                    continue
                try:
                    size_kb = int(f.stat().st_size / 1024)
                except Exception:
                    size_kb = 0
                # ⭐ 系统文档返回**带 `_system/` 前缀的名字** ——
                #    `_resolve_file_path` 是平铺拼接（`_PERSIST_KNOWLEDGE_DIR / raw`），
                #    带上前缀它就能直接解析，解析器一行都不用改。
                _disp = ("/".join([*_rel_parts, f.name]) if _is_system else f.name)
                seen_persist[_disp] = {
                    "filename": _disp,
                    "source": "persist",
                    "size_kb": size_kb,
                    "indexed": f.name in indexed_filenames,
                    "system": _is_system,
                }
            result.extend(seen_persist.values())
    except Exception as e:
        logger.warning(f"[RAG] list_knowledge_files 读持久库失败: {e}")

    # 2. 临时库：先扫磁盘目录，再从 _temp_collection 补全（两者合并去重）
    seen_temp: set = set()
    try:
        temp_dir = _temp_uploads_dir()
        if temp_dir.exists():
            for f in temp_dir.iterdir():
                if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if f.name.startswith("~$") or f.name.startswith("."):
                    continue
                if f.name in seen_temp:
                    continue
                seen_temp.add(f.name)
                try:
                    size_kb = int(f.stat().st_size / 1024)
                except Exception:
                    size_kb = 0
                result.append({
                    "filename": f"[临时]{f.name}",
                    "source": "temp",
                    "size_kb": size_kb,
                    "indexed": False,  # 磁盘扫出来的默认未索引，下面从 collection 更新
                })
    except Exception as e:
        logger.debug(f"[RAG] list_knowledge_files 扫临时目录失败: {e}")

    # 从 _temp_collection 补全：标记哪些已入索引，以及补充 collection 里有但磁盘找不到的
    if _temp_collection is not None:
        try:
            raw = _temp_collection.get(include=["metadatas"])
            indexed_temp: set = set()
            for m in raw.get("metadatas", []) or []:
                fname = m.get("filename", "")
                if fname:
                    indexed_temp.add(fname)

            # 更新 indexed 状态
            for r in result:
                if r["source"] == "temp":
                    bare = r["filename"][len("[临时]"):]
                    if bare in indexed_temp:
                        r["indexed"] = True

            # 补充 collection 里有但磁盘目录里没扫到的（极少情况）
            for fname in indexed_temp:
                if fname not in seen_temp:
                    seen_temp.add(fname)
                    result.append({
                        "filename": f"[临时]{fname}",
                        "source": "temp",
                        "size_kb": 0,
                        "indexed": True,
                    })
        except Exception as e:
            logger.debug(f"[RAG] list_knowledge_files 读临时 collection 失败: {e}")

    return result


def list_knowledge_files_for_agent() -> str:
    """list_knowledge_files 的 Agent 友好格式化版本，返回给模型看的字符串。

    供 orchestrator 的 function_call 分支直接拿来回灌给模型。
    """
    try:
        # 🐛 原本直接调 `list_knowledge_files()` ⇒ 把为**用户抽屉**设计的排除规则
        #    原样继承到了**模型侧**，Nano 因此看不见自己的手册。
        files = list_knowledge_files(include_system=True)
    except Exception as e:
        return f"[File List Failed] {e}"

    if not files:
        return "[Available Files] No files are currently available. The user needs to upload files through the knowledge-base UI or attach files in the conversation."

    # ⚠️ 系统文档单独分一段 —— 光"看得见"不够，还要让它知道
    #    **这是它自己的手册，不是用户上传的资料**，
    #    否则它会拿对待用户文件的态度去对待它。
    system = [f for f in files if f["source"] == "persist" and f.get("system")]
    persist = [f for f in files if f["source"] == "persist" and not f.get("system")]
    temp = [f for f in files if f["source"] == "temp"]

    lines = ["[Available Files]"]
    if system:
        lines.append("")
        # ⚠️ 必须同时说清**归属**和**位置** —— 上一版只说了"不是用户上传的"，
        #    模型据此推断"那就不在 RAG 索引里"，然后拒绝检索（实测）。
        #    📌 一句只说了一半的话，模型会把另一半自己补上，而且补错。
        # ⚠️ 两条路都摆出来，**但不替它选** —— 选哪条要看用户这次问什么：
        #    关键词明确（"切换主题在哪"）⇒ 检索片段就够；
        #    问法绕（"能不能把页面改成黑的"）⇒ 检索无果后再全文加载。
        lines.append(f"Your own documentation ({len(system)} file(s)) - Nano's system "
                     "documents rather than user uploads, but they live in the SAME "
                     "knowledge base and ARE indexed: query_local_knowledge searches them "
                     "like any other file, and load_full_file reads one in full. Both work; "
                     "pick whichever fits the question. Consult them whenever the user asks "
                     "how you or your interface work. Use the exact name shown below, "
                     "including the _system/ prefix:")
        for f in system:
            lines.append(f"  - {f['filename']}  ({f['size_kb']} KB)")
    if persist:
        lines.append(f"\nPersistent knowledge base ({len(persist)} file(s)):")
        for f in persist:
            lines.append(f"  - {f['filename']}  ({f['size_kb']} KB)")
    if temp:
        lines.append(f"\nCurrent conversation attachments ({len(temp)} file(s)):")
        for f in temp:
            lines.append(f"  - {f['filename']}  ({f['size_kb']} KB)")

    lines.append(
        "\n\n[Usage Hint] "
        "When calling load_full_file, pass the filename field directly. Temporary files keep the [临时] prefix. "
        "Use load_full_file for whole-file understanding, and query_local_knowledge for specific facts or fragments."
    )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Plan 编排器专用的 meta 接口
# ═══════════════════════════════════════════════════════════════════════════
#
# 设计原则:
#   1. 严格不修改现有 API(query_for_agent / load_full_file / 等)
#   2. 这些 meta 函数返回 dict,结构化字段含 context_type / coverage / 等
#   3. text 字段跟 phase 2 接口的返回保持一致,Plan 内的 chat_without_tools 也能用
#   4. Plan 编排器据此做 required_context_level 检查
#
# 调用方:
#   - Phase 2 路径(_execute_file_tool_chain):继续用原 query_for_agent / load_full_file
#   - Phase 3 路径(_execute_skill_plan):用这里的 _meta 函数
#
# context_type 取值:
#   "none"             - 失败或空,不可用
#   "fragment_context" - RAG 片段
#   "partial_context"  - 全文加载但被截断
#   "full_context"     - 完整全文
#   "file_reference"   - 真实磁盘路径
#   "file_listing"     - 文件清单(只是元信息,本身不算上下文)
# ═══════════════════════════════════════════════════════════════════════════


def _count_pages(path: pathlib.Path) -> Optional[int]:
    """统计 PDF/docx 等文件的页数。失败返回 None。

    实现:
    - PDF: 优先 pymupdf(fitz),兜底 pypdf
    - docx: docx 的"页数"取决于打印机渲染,python-docx 无法准确获取,
            返回 None 让上层知道"该文件无页数概念"
    - 其他格式: 返回 None
    """
    try:
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
    except Exception:
        return None

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        # 优先 pymupdf(fitz):更快、准确
        try:
            import fitz
            doc = fitz.open(str(path))
            n = doc.page_count
            doc.close()
            return n
        except Exception:
            pass
        # 兜底 pypdf
        try:
            import pypdf
            with open(path, "rb") as f:
                return len(pypdf.PdfReader(f).pages)
        except Exception:
            return None

    # pptx：幻灯片数 = 页数，用 python-pptx 直接读，准确可靠
    if suffix == ".pptx":
        try:
            from pptx import Presentation
            prs = Presentation(str(path))
            return len(prs.slides)
        except Exception:
            return None

    # docx 的页数无法准确算(渲染相关),返回 None
    # xlsx/csv/txt/md/图片:没有"页"概念
    return None


def load_full_file_meta(filename: str, with_images: bool = False, progress_callback=None) -> Dict[str, Any]:
    """全文加载 meta 版,返回结构化字典。

    🪦🪦 **这个函数目前【零调用方】**（2026-08-26 核实）。
       全仓搜下来只有两处 docstring 提到它的字段名，没有任何代码调它。
    🔴 它一度被当成「先完整解析文件再返回 meta，不能用于廉价预检」的问题记着 ——
       **那个问题的前提不成立**：它不是「贵」，它是**没人用**。
    📌 **一个没有调用方的函数不构成任何人的阻塞。**
       给它做「廉价路径」优化，是在优化一条没有人走的路。
    ⭐ 而真正需要的那件事（「这个文件要不要先试读」）已经解决了 ——
       `load_full_file` 现在返回完整文本，字符数当场就有；
       判断由 `core.reading.needs_peek()` 做。
    ⚠️ 保留而不删：它的字段形状（`context_type` / `coverage` / `source_file`）
       被两处 docstring 当成对齐基准引用着。要删的话得连那两处一起处理。

    Plan 编排器用本接口而非 load_full_file,以便:
    - context_type / coverage 判断后续 Skill 是否能用此上下文
    - source_file 字段可直接传给 FILE_PATH_REQUIRED 类 Skill 的 file_path 参数
    - total_pages / total_chars / image_count 等元信息暴露给下游

    返回 dict 字段(始终包含):
      success: bool
      text: str                  - 完整内容(成功时);失败提示(失败时)
      context_type: str          - none/full_context/partial_context
      coverage: str              - full/truncated/none
      filename: str              - 原始 filename(含 [临时] 前缀如有)
      source_file: str|None      - 真实磁盘绝对路径
      source: str                - persist/temp/unknown
      total_chars: int
      total_pages: int|None
      truncated: bool
      image_count: int           - 文档内未识别图片数(docx/pdf)
      with_images: bool
      error: str|None
    """
    # 1. 参数校验
    if not filename or not isinstance(filename, str):
        return {
            "success": False, "text": "[Load Failed] Invalid filename parameter",
            "context_type": "none", "coverage": "none",
            "filename": filename or "", "source_file": None, "source": "unknown",
            "total_chars": 0, "total_pages": None, "truncated": False,
            "image_count": 0, "with_images": with_images, "error": "invalid_filename",
        }

    # 2. 路径解析(复用 phase 2 的 _resolve_file_path)
    path, source = _resolve_file_path(filename)
    if path is None:
        return {
            "success": False,
            "text": (
                f"[Load Failed] File \"{filename}\" was not found. "
                f"Call list_knowledge_files to view available filenames."
            ),
            "context_type": "none", "coverage": "none",
            "filename": filename, "source_file": None, "source": "unknown",
            "total_chars": 0, "total_pages": None, "truncated": False,
            "image_count": 0, "with_images": with_images, "error": "file_not_found",
        }

    # 3. 直接调老的 load_full_file 拿完整文本(复用全部解析逻辑)
    try:
        text = load_full_file(filename, with_images=with_images, progress_callback=progress_callback)
    except Exception as e:
        return {
            "success": False,
            "text": f"[Load Error] {filename}: {e}",
            "context_type": "none", "coverage": "none",
            "filename": filename, "source_file": str(path), "source": source,
            "total_chars": 0, "total_pages": None, "truncated": False,
            "image_count": 0, "with_images": with_images, "error": str(e),
        }

    # 4. 检测是否失败路径(老函数失败时返回以"【加载失败"开头的字符串)
    if text.startswith("[Load Failed]"):
        return {
            "success": False, "text": text,
            "context_type": "none", "coverage": "none",
            "filename": filename, "source_file": str(path), "source": source,
            "total_chars": 0, "total_pages": None, "truncated": False,
            "image_count": 0, "with_images": with_images, "error": "load_failed",
        }

    # 5. 算元信息
    char_count = len(text)
    # 🪦 这里曾是 `truncated = text.startswith("[File Too Large")`。
    #    2026-08-26 取消超量降级之后，那个前缀**再也不会出现** ——
    #    ⇒ 这个判断从此恒为 False。
    # 📌 **一个恒为假的判断，比删掉它更坏**：它让读的人以为这里还有一种情况要处理，
    #    而那种情况已经不存在了。留字段是为了不动调用方的形状，但要说清它已经死了。
    # ⚠️ 若将来重新引入某种截断，**改的是这里**，别在别处再造一个标志位。
    truncated = False

    try:
        total_pages = _count_pages(path)
    except Exception:
        total_pages = None

    image_count = 0
    try:
        if path.suffix.lower() in (".docx", ".pdf", ".pptx"):
            image_count = _count_images_in_file(path)
    except Exception:
        pass

    return {
        "success": True,
        "text": text,
        "context_type": "partial_context" if truncated else "full_context",
        "coverage": "truncated" if truncated else "full",
        "filename": filename,
        "source_file": str(path.resolve()),
        "source": source,
        "total_chars": char_count,
        "total_pages": total_pages,
        "truncated": truncated,
        "image_count": image_count,
        "with_images": with_images,
        "error": None,
    }


    # 🪦 `query_for_agent_meta` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
    #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
    #       写了不接时缺口看起来已经补上了。


    # 🪦 `get_file_path_meta` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
    #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
    #       写了不接时缺口看起来已经补上了。


    # 🪦 `list_knowledge_files_meta` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
    #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
    #       写了不接时缺口看起来已经补上了。


# ── 模块加载完成后登记健康探针（必须放在文件末尾：探针引用了上面定义的函数）──
_register_health_probes()
