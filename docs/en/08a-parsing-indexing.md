# 08a · Parsing & indexing

**What this page covers**: how a file becomes retrievable chunks — the parsing strategy for ten formats (including the PDF three-tier fallback and enhanced mode), chunking rules (tables stay whole, preventing hallucinated cross-table mixing), the `[FileSchema]` structural-summary chunk, incremental indexing, and model loading.  
**After reading it you can**: add a file format, tune chunking, or change incremental indexing without breaking the hash-based decisions.  
**Prerequisites**: [08 overview](08-knowledge-base.md).  

> Language: [中文](../zh/08a-parsing-indexing.md) · English  

---

## Supported formats and parsers

`SUPPORTED_EXTENSIONS` (constants block of `core/rag.py`): txt / md / pdf /
docx / pptx / xlsx / xls / csv + images (jpg/jpeg/png/webp/bmp/gif). One parser
`_parse_<ext>` per format, dispatched by `_parse_file`.

Key parsing strategies:

- **PDF three-tier fallback** (`_parse_pdf`): pdfplumber (text PDFs, good
  layout) → pymupdf (the ones pdfplumber cannot) → PaddleOCR (scanned fallback,
  slow). Tier-change criterion: if the tier's extracted characters < total
  pages × 30 (about one line of Chinese), it "basically extracted nothing" —
  next tier.
- **Enhanced mode** (`enhanced_mode`, from the UI's `_enhanced_mode` setting,
  passed via `index_config`): docx/pptx/pdf extract embedded images and
  generate multimodal descriptions; `max_ocr_pages` limits OCR pages (default 50).
- **xlsx is read cell-by-cell** (openpyxl), keeping position info
  (`[Sheet: xxx]` + `coordinate=A1`), counting charts/images.
- **Image files** go through `_describe_image_multimodal` into text descriptions.
- **Single-file hard cap** `MAX_FILE_SIZE_MB = 50`: parse peak memory can be
  ten times the file size; change with care.

## Chunking: tables stay whole (preventing hallucinated mixing)

Core rules of `_chunk_text` (default 500 chars, overlap 80):

- **Tables do not participate in hard splitting**. Tables parsed by
  pdfplumber/docx start with a `[Table]` marker; before chunking, the text is
  segmented on `[Table]` boundaries and each table becomes one chunk.
- Why: several same-structure tables ("this month's retention rate" vs
  "in-network rate") split across chunks make the model mix numbers from
  different tables — **hallucinated data mismatch**.
- Oversized tables (over `chunk_size × 4`, default 2000 chars) split by rows,
  each sub-chunk keeping a `[Table continued]` prefix.
- `overlap >= chunk_size` forcibly falls back to a quarter (config-error guard).

## The `[FileSchema]` structural-summary chunk

`_generate_schema_chunk` extracts the **file skeleton** (no data): XLSX sheet
names + columns, PDF/DOCX section titles + table titles, CSV columns, TXT/MD
heading lines. Stored with `metadata.chunk_type=schema`.

Effect: at retrieval, the schema chunk comes out naturally with the data
chunks — the model sees both "table 2.4 = cognitive axis" and the actual
numbers, **able to locate fully instead of guessing from orphan numbers**.

## Incremental indexing

The decision chain of `index_documents`:

1. Scan `data/knowledge/`, filtering Office lock files (`~$xxx`) and hidden files.
2. Clean parse_reports entries whose source file no longer exists.
3. **Decide by file hash** (`_file_hash` → `data/indexed_hashes.json`):
   unchanged → skip; changed → delete old chunks by source
   (`_collection_delete_by_source`), then re-chunk and re-embed.
4. Each file produces a parse_report (status, chars, image count, etc.) →
   `data/parse_reports.json`, feeding the health panel.
5. After indexing, **rebuild the BM25 index** (triggered by both incremental
   and skipped paths, keeping it fresh).

Single-file ingestion uses `index_single_file` (used right after uploads).

## Model loading

- **safetensors snapshots**: `_resolve_hf_snapshot` uses `snapshot_download`
  with explicit allow_patterns taking only safetensors and configs, **so .bin
  never enters the cache** — background: transformers 5.x refuses .bin weights
  under torch < 2.6 (CVE-2025-32434), and mirrors may resolve to a .bin-only
  snapshot, causing WEIGHTS_FORMAT_REJECTED with no self-healing (the v1.97
  fix; full story in Changelog v1.97).
- **Lazy load + crash breadcrumb**: `_load_embedder` is the most dangerous call
  in the project (torch/transformers and chroma-hnswlib in one process caused
  random segfaults, 3 crashes in 5 reproductions) — write the breadcrumb to
  disk **before** acting, clear it on success; segfaults bypass every except,
  so next start the uncleared trace says where it died.
- **Error classification**: `_classify_model_load_error` classifies by root
  cause (missing / format-rejected / network) so UI and model see it, instead
  of an upstream except swallowing it.
- Empty KB → **no preload** (let the app start normally).

## Hands-on recipes

**Case A: add a file format**
1. Add the suffix to `SUPPORTED_EXTENSIONS` (images also to `IMAGE_EXTENSIONS`).
2. Write `_parse_<ext>`: returns plain text (tables use `[Table]` markers so
   chunking recognizes them); generates/updates the report (status, stats).
3. Add a dispatch line in `_parse_file`.
4. If the format has "structure" (sheets/sections), extend
   `_generate_schema_chunk`.
5. Tests: beyond `tests/cases/t_f4_catalog.py`, run `index_single_file` with a real
   file and verify retrieval.

**Case B: tune chunking**
`_chunk_text`'s chunk_size/overlap. The tables-stay-whole rule **takes priority
over** the sliding window; larger chunks must consider the embedding model's
input limit (8192 tokens for bge-m3).

**Case C: change incremental decisions**
Touch only `_file_hash` / the `indexed_hashes.json` reads. **Do not** include
parse_report in the hash — that report is for humans; including it makes a
"parser upgrade" look like "content changed".

## How to verify you got it right

1. `bash run_tests.sh`.
2. Real machine: drop an xlsx with tables into `data/knowledge/`, restart, and
   search "what is the cognitive axis in the table" — it should hit both the
   schema chunk and the data chunk.
3. Modify the same file and re-ingest: old chunks must be replaced (vector
   chunk count does not pile up).
4. A scanned PDF (pure images) should reach the OCR tier with its parse_report
   status visible.

---

← Back to [08 overview](08-knowledge-base.md)
