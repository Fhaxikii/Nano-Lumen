# 08 · Knowledge base and retrieval

**What this page covers**: how files are ingested, how retrieval works, and the special handling of system documents.  
**After reading it you can**: modify the ingest flow or retrieval strategy, and locate why something cannot be retrieved.  
**Prerequisites**: [02-architecture.md](02-architecture.md).

> Language: [中文](../zh/08-knowledge-base.md) · English

---

## Composition

The code is concentrated in `core/rag.py`. Retrieval combines three parts:

| Part | Role |
|---|---|
| Vector retrieval | Matches on semantic proximity, no literal overlap required |
| Keyword retrieval | BM25-based, literal matching; Chinese goes through word segmentation |
| Reranking | Re-sorts the merged results of the two channels |

The two retrieval channels are merged via RRF (Reciprocal Rank Fusion);
the parameter `RRF_K` is defined in `core/rag.py`.

When the keyword channel's dependencies are missing, retrieval degrades to
vector-only. The degradation is registered with the health registry but does
not interrupt the conversation.

## Model dependencies

| Purpose | Model |
|---|---|
| Embedding | `BAAI/bge-m3`, local, ~2.3 GB |
| Reranking | Local, may be absent |

Without the embedding model, knowledge-base retrieval is unavailable
entirely. Load-failure attribution lives in `_model_load_code` and
`_classify_model_load_error` in `core/rag.py`.

Attribution must distinguish several causes, because the advice to the user
is completely different: a genuinely missing file means re-download, while
insufficient memory means the file is intact and re-downloading is pointless.
Never use a broad exception base class as the test for one specific cause.

## Supported formats

Text: `.txt` `.md` `.pdf` `.docx` `.pptx` `.xlsx` `.xls` `.csv`
Images: `.jpg` `.jpeg` `.png` `.webp` `.bmp` `.gif`

The exact lists are `SUPPORTED_EXTENSIONS` and `IMAGE_EXTENSIONS` in
`core/rag.py`. The per-file size cap is `MAX_FILE_SIZE_MB`.

Images and scanned PDFs are converted to text via vision or OCR before
ingest. The vision model is set under Settings → Advanced → Vision and
resolved via `model_for_role` in `core/models.py`, never hardcoded at the
call site.

## Ingest

`index_documents()` scans `data/knowledge` and processes incrementally by
file hash: unchanged files are skipped; changed files have their old chunks
deleted, then are re-chunked and re-ingested. Hashes are recorded in
`data/indexed_hashes.json`.

Chunking parameters are in `_chunk_text` in `core/rag.py`: default chunk 500
characters with 80 overlap. Overly long tables are split by row.

## Retrieval

Two public entry points:

| Function | Purpose |
|---|---|
| `search()` | Returns a structured list of chunks |
| `query_for_agent()` | Returns a string for the model |

There is also `load_full_file()` to read an entire file. Retrieval suits
finding specific facts, clauses, numbers, keywords; for understanding
overall structure or a complete mechanism, read the full text.

Queries shorter than `MIN_QUERY_CHARS` do not trigger retrieval.

## System documents

`data/knowledge/_system/` holds Nano's own documents, such as the user
manual. Their visibility rules differ from ordinary files:

| Entry point | Includes system documents? |
|---|---|
| Retrieval | yes |
| The knowledge-base file list in the UI | no |
| The file catalog provided to the model | yes |
| Deletion | refused |

Hidden from the user and hidden from the model are two different things,
distinguished by the `include_system` parameter of `list_knowledge_files()`,
defaulting to excluded.

System documents appear in the catalog with the `_system/` prefix;
`load_full_file()` must be called with the full prefixed name.

### Writing system documents

System documents must support both retrieval and full-text loading, which
imposes two constraints on how they are written:

1. **Easy for keywords to hit**: whatever words a user would ask with must
   appear in the title and body.
2. **Each chunk stands alone**: any chunk recalled on its own must be
   understandable without context. In practice: each section opens by
   restating where it is.

#### About nano_manual.md

`_system/nano_manual.md` is Nano's user manual: where every UI feature
lives, what it is called, and how to use it. When the user asks "where is
X", "how do I turn Y on/off", or "can Nano do Z", the answer should come
from this file. Three maintenance rules:

1. **Changed the UI or a feature? Update the manual in the same commit.**
   This file is the authority Nano answers "where is X" from; if the
   feature changed and the manual did not, Nano will confidently answer
   with stale information — worse than not knowing.
2. **State the corresponding program version in the file header**
   (e.g. "manual matches Nano version 1.96"). The manual ships with the
   release; the version number is the first check for staleness.
3. **One section answers one question; title it the way users ask.**
   Ingest chunks at roughly 500 characters (see "Ingestion" above), so a
   section should fit inside one chunk. A title like "where do I open
   Settings" hits more easily than "about the settings panel". Avoid
   cross-references like "see the previous section" — a chunk recalled on
   its own cannot follow them.

## Common problems

**The file exists but retrieval never finds it**
First confirm it entered the index: check `data/indexed_hashes.json` and the
chunk count in the vector store. Present on disk but absent from the index
usually means an ingest error or an unsupported format.

**System documents cannot be retrieved**
The retrieval path does not exclude system documents. If the model claims
they are not indexed, check whether the wording of the file catalog given to
the model created that impression.

---

## How to verify you got it right

1. Add a new file: after ingest, the UI's file and chunk counts increase.
2. Search with a keyword from the file: it hits.
3. Modify the file and re-ingest: old chunks are replaced, not stacked.
4. Remove the embedding model and start: the report says "model missing" and
   nothing else, and conversation works unaffected.
5. Run `bash run_tests.sh`.

---

← Back to [README](README.md)
