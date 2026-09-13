# 08 · Knowledge base & retrieval (Overview)

**What this page covers**: the map of the RAG layer — the four blocks of `core/rag.py` (~4,600 lines), the two-way retrieval fusion, a file's lifecycle (ingest → index → retrieve → temp attachments), and how the three sub-pages divide the work.  
**After reading it you can**: tell which sub-page your change belongs in, and understand why RAG is shaped as hybrid retrieval + multi-level parsing fallback.  
**Prerequisites**: [02-architecture.md](02-architecture.md), [10-builtin-tools.md](10-builtin-tools.md) (RAG retrieval is an availability of a built-in tool).  

> Language: [中文](../zh/08-knowledge-base.md) · English  

---

## The four blocks (a map of `core/rag.py`)

| Block | Function region (lines) | Contents | Read in |
|---|---|---|---|
| Model loading | `_resolve_hf_snapshot` / `_load_embedder` / `_load_reranker` | safetensors snapshots, lazy embedder/reranker, error classification | [08a](08a-parsing-indexing.md) |
| Parsing & indexing | `_parse_file` / `_chunk_text` / `_index_one_file` / `index_documents` | ten formats, chunking, incremental indexing | [08a](08a-parsing-indexing.md) |
| Retrieval & fusion | `_build_bm25_index` / `_bm25_search` / `_rrf_fuse` / `_rerank` / `search` / `query_for_agent` | vector + BM25 fused by RRF + rerank | [08b](08b-retrieval-ranking.md) |
| Temp attachments | `index_temp_file` / `register_temp_file` / `cleanup_stale_temp_files` | registration-based lazy build for uploads | [08c](08c-health-temp.md) |

Cross-cutting: **health probes** (`_register_health_probes` — four probes
watching embedder/reranker/bm25/vector store) and **parse_report** (per-file
parsing report, `data/parse_reports.json`, feeding the health panel) — see
[08c](08c-health-temp.md).

## The retrieval design in one sentence

**Hybrid retrieval (vector + BM25, fused by RRF) + a cross-encoder reranker**,
solving "long queries with modifiers drown the relevant chunk" — vector search
has high semantic similarity but gets pulled off by irrelevant content; BM25 is
precise on keywords but blind to semantics; RRF fuses both rankings, then the
reranker re-sorts. Acceptance criterion: a question with long modifiers and the
key info at the end must hit the short clause at the end of the text. Details
and tuning: [08b](08b-retrieval-ranking.md).

## A file's lifecycle

```
drop into data/knowledge/ → index_documents scans (incremental by file hash)
→ _parse_file multi-level parsing (PDF three-tier fallback: pdfplumber → pymupdf → OCR)
→ _chunk_text chunking (tables stay whole) → embed into chroma
→ BM25 index rebuild → served to query_local_knowledge / load_full_file
```

Hashes live in `data/indexed_hashes.json`, parse reports in
`data/parse_reports.json`, the vector store in `data/chroma_db/` — **all three
are runtime files, not in the repository** (`data/` is whitelist-based, see
[12-contributing.md](12-contributing.md)).

## System documents (`_system/`)

`data/knowledge/_system/` holds Nano's own manual (`nano_manual.md`), with
special visibility: hidden from the user (not in the UI's KB list), visible to
the model (in retrieval and the file catalog), deletion refused. Maintenance
rules (UI changes must update the manual in the same commit; version in the
header; one section, one question) live in
[12-contributing.md](12-contributing.md) and
[the writing requirements](#writing-system-documents).

## The three sub-pages

| Sub-page | Direction of change |
|---|---|
| [08a · Parsing & indexing](08a-parsing-indexing.md) | new formats, chunking strategy, incremental indexing, model loading |
| [08b · Retrieval & ranking](08b-retrieval-ranking.md) | RRF/rerank tuning, retrieval paths, query_for_agent |
| [08c · Health & temp attachments](08c-health-temp.md) | probes, parse_report, temp-attachment registry, chroma self-healing |

## Two iron rules

1. **Incremental decisions are based on file content hashes**. Unchanged files
   are skipped; changed ones have old chunks deleted before re-chunking
   (otherwise chunks pile up); parse_report is a human-facing report and takes
   no part in the decision.
2. **Retrieval and full-text loading are separate**: RAG finds fragments
   (`query_for_agent`); `load_full_file` reads the whole file — "find a specific
   fact" goes to retrieval, "understand the whole structure" goes to full text.

---

## How to verify you got it right

This page is a map. Verification lives at the end of each sub-page; full
regression `bash run_tests.sh`.

---

← Back to [README](README.md)
