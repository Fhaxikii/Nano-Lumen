# 08c · Health & temp attachments

**What this page covers**: the two cross-cutting blocks — health probes and parse_report (making "a capability is broken" visible), the registration-based lazy build for temp attachments (uploads persist only; indexing happens at search time), and the chroma corruption self-healing chain.  
**After reading it you can**: add a probe, extend parse_report, or change the temp-attachment lifecycle without introducing "accidentally indexing history files".  
**Prerequisites**: [08 overview](08-knowledge-base.md), [08a parsing & indexing](08a-parsing-indexing.md).  

> Language: [中文](../zh/08c-health-temp.md) · English  

---

## Health probes: five, not one fewer

`_register_health_probes` registers with the HealthRegistry (queried on read,
no performance impact):

| Probe | Watches |
|---|---|
| `KB_STORE` | the vector store opens |
| `KB_VECTOR_SEARCH` | the embedder loads |
| `KB_RERANKER` | the reranker loads |
| `KB_KEYWORD_SEARCH` | BM25 builds |
| `OCR_TESSERACT` | OCR is available |

⚠️ **The reranker and BM25 probes were added to fill a gap**: both degrade
**silently** (reranker down = original order; BM25 down = vector only) —
without a probe they are never seen. This is the direct answer to the "a
capability is broken but nobody reported it" class. Probe output lands in the
monitor drawer (see availability-first display in 09c).

## parse_report: a parsing report per file

`data/parse_reports.json`, one entry per indexed file: status, extracted
characters, image count, which parsing path it took (e.g. PDF via
pdfplumber+tables or OCR).

- Feeds the **health panel** (UI), making "this file parsed incompletely"
  visible.
- `_build_parse_warning_block` turns the report into a hint for the model —
  when retrieval hits a file that parsed incompletely, the model knows "results
  may be partial".
- **It takes no part in incremental decisions**: the hash decision looks only at
  file content (see 08a); a parser upgrade must not be misread as "content
  changed".

## Temp attachments: registration-based lazy build

The lifecycle of an uploaded file (Phase 3 design):

```
user uploads → persist to disk + register_temp_file into the session map
(nothing indexed yet)
→ user searches with source including temp → _ensure_all_temp_files_indexed
   lazy-builds registered files only (OCR/parse/embed — slow)
→ session ends → cleanup_stale_temp_files removes leftovers
```

**Why registration** (the header-note logic): scanning the directory would
accidentally index leftover files (large files from previous sessions); only
files explicitly uploaded in this session participate in lazy build.
`progress_callback` lets the UI show progress during OCR/indexing.

Temp files index into a separate temp collection with a `[临时]` filename
prefix marker (consistent with `search`'s source filtering, so RRF fusion
merges and dedupes the two paths correctly — see 08b).

## The chroma self-healing chain

The vector store (HNSW) can suffer structural corruption in some environments.
The chain (`_backup_and_reset_chroma`):

```
structural failure detected (_is_structural_chroma_failure)
→ back up the corrupted store → reset in-process chroma state → delete orphan segments
→ _schedule_reindex_after_heal triggers one full re-index in the background
```

⭐ **Why the re-index is scheduled separately**: self-healing can happen at
**any** moment the store opens. If it happens during startup indexing, the
caller gets an empty store and re-indexes everything itself — no need to do it
again. But if it happens during an ordinary query (the user searches before the
store was ever opened), **nobody would rebuild the index** — the user gets a
permanently empty store. So it re-indexes unconditionally once per process
(`_reindex_after_heal_started`); a duplicate is harmless
(`index_documents` is idempotent, hash-incremental).

## Hands-on recipes

**Case A: add a probe**
Copy `_probe_embedder`'s shape (read-only, returns bool, never raises) and
register in `_register_health_probes`. Self-check: does this capability fail
**silently**? Then it needs a probe.

**Case B: extend parse_report**
`_new_report` / `_set_status` / the fields in each `_parse_<ext>`. New fields
must reach both `_build_parse_warning_block` (for the model) and the health
panel (for the human).

**Case C: change the temp lifecycle**
Registration / cleanup / collection are three linked pieces. Keep two
invariants: leftover files are never accidentally indexed (registration); temp
file markers match source filtering (RRF merge & dedupe).

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_shot_prune.py`, `t_d10_user_images.py`).
2. Real machine: upload a large PDF and immediately search — the UI shows lazy
   build progress, then hits the temp store.
3. Make the reranker unavailable: the monitor drawer shows the degraded tier
   (not nothing).
4. Self-healing path: back up and corrupt the vector store directory; on
   restart the log shows "re-index after heal" and retrieval recovers.

---

← Back to [08 overview](08-knowledge-base.md)
