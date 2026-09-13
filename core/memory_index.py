# core/memory_index.py
"""
持久语义记忆 — 独立向量索引层。

职责边界：
- core/memory_store.py：只管 SQLite（semantic_memories 表），零外部依赖，
  是唯一真值来源（source of truth）。
- core/memory_index.py（本文件）：只管向量召回，独立 collection，
  **不和知识库共用 collection**（粒度/生命周期/检索目标不同，混用会
  互相污染检索质量）。

复用 RAG 已经装好的 embedding 模型（BAAI/bge-m3），但不复用 RAG 的
chromadb collection（`nano_knowledge`）——这里用独立的 collection
（`nano_semantic_memory`），存在独立目录下，避免任何检索串扰。

一致性原则：向量库只做召回，不做最终
真值。本文件的 search() 已经内置"召回后回 SQLite 校验状态"这一步，调用方
拿到的结果保证是当前有效的（is_enabled=1 且未删除），不需要重复校验。
"""
from __future__ import annotations
import pathlib
import threading
from typing import Any, Optional
from loguru import logger

_MEMORY_CHROMA_DIR = str(pathlib.Path(__file__).parent.parent / "data" / "chroma_memory_db")
_COLLECTION_NAME = "nano_semantic_memory"

_client = None
_collection = None
_init_lock = threading.Lock()


def _get_collection():
    """懒加载独立的 chromadb collection。和 RAG 的 nano_knowledge 完全
    隔离（不同目录、不同 collection 名）。"""
    global _client, _collection
    if _collection is not None:
        return _collection
    with _init_lock:
        if _collection is not None:
            return _collection
        import chromadb
        pathlib.Path(_MEMORY_CHROMA_DIR).mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=_MEMORY_CHROMA_DIR)
        _collection = _client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"✔ [MemoryIndex] 独立语义记忆向量集合就绪，当前块数: {_collection.count()}")
        return _collection


def _embed(texts: list[str]):
    """复用 RAG 已加载的 embedding 模型，懒导入避免在 memory_index 模块
    加载时就拉起整个 RAG 模块的初始化开销。"""
    from core.rag import _load_embedder
    embedder = _load_embedder()
    return embedder.encode(texts, normalize_embeddings=True).tolist()


def upsert(memory_id: str, canonical_text: str, memory_type: str) -> None:
    """写入/更新一条记忆的向量。metadata 只存最轻量的过滤字段
    （memory_type），其余真值一律去 SQLite 查，不在向量库metadata里
    冗余存状态字段（状态字段本身就容易和 SQLite 失步）。
    """
    try:
        collection = _get_collection()
        vec = _embed([canonical_text])[0]
        collection.upsert(
            ids=[memory_id],
            embeddings=[vec],
            documents=[canonical_text],
            metadatas=[{"memory_type": memory_type}],
        )
    except Exception as e:
        logger.warning(f"[MemoryIndex] upsert失败（不影响SQLite主流程）: {e}")


def delete(memory_id: str) -> None:
    try:
        collection = _get_collection()
        collection.delete(ids=[memory_id])
    except Exception as e:
        logger.warning(f"[MemoryIndex] delete失败（不影响SQLite主流程，软删除已经在SQLite生效）: {e}")


def search(query_text: str, memory_type: str = "", top_k: int = 5,
          min_similarity: float = 0.0) -> list[dict[str, Any]]:
    """语义检索 + 回SQLite状态校验，返回的都是当前有效记录。

    Returns:
        [{"id":..., "similarity": 0~1, ...SQLite里的完整字段...}, ...]
        按相似度降序。如果向量库/embedding 出问题，安全降级为空列表，
        不抛异常影响调用方主流程。
    """
    try:
        collection = _get_collection()
        if collection.count() == 0:
            return []
        vec = _embed([query_text])[0]
        where = {"memory_type": memory_type} if memory_type else None
        raw = collection.query(
            query_embeddings=[vec], n_results=min(top_k * 3, 50),  # 多召回一些，校验后可能被过滤掉一部分
            where=where, include=["distances"],
        )
    except Exception as e:
        logger.warning(f"[MemoryIndex] search失败，降级为空结果: {e}")
        return []

    ids = (raw.get("ids") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]
    if not ids:
        return []

    # cosine distance -> similarity（chromadb hnsw:space=cosine 时，
    # distance = 1 - cosine_similarity）
    from core.memory_store import get_memory_store
    store = get_memory_store()

    results = []
    for mid, dist in zip(ids, distances):
        similarity = 1.0 - float(dist)
        if similarity < min_similarity:
            continue
        record = store.get_semantic_memory(mid)
        if not record:
            continue  # 向量库有、SQLite没有：脏数据，跳过（不报错，静默过滤）
        if not record.get("is_enabled") or record.get("deleted_at"):
            continue  # 一致性校验：用户删除/禁用的，绝不返回（核心要求）
        if record.get("memory_status") == "superseded":
            continue  # 已被取代的旧记录不参与默认召回
        if record.get("promotion_status") == "promoted":
            continue  # 已转正进显式偏好抽屉的，
                      # 隐性记忆退出默认注入，避免和显式偏好重复生效
        record["similarity"] = similarity
        results.append(record)
        if len(results) >= top_k:
            break

    return results
