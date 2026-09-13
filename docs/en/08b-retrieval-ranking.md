# 08b · Retrieval & ranking

**What this page covers**: the two-way retrieval fusion — vector + BM25 fused by RRF on ranks, the cross-encoder reranker, temp-first routing, and the division between retrieval and full-text loading.  
**After reading it you can**: tune RRF/rerank parameters, change retrieval routing, or reshape `query_for_agent`'s output.  
**Prerequisites**: [08 overview](08-knowledge-base.md), [08a parsing & indexing](08a-parsing-indexing.md).  

> Language: [中文](../zh/08b-retrieval-ranking.md) · English  

---

## The retrieval pipeline

`search` (core/rag.py):

```
query (≥ MIN_QUERY_CHARS=4 non-space chars, else empty result)
→ lazy-build temp attachment index (when source includes temp)
→ vector search (chroma) + BM25 keyword search (two parallel paths)
→ _rrf_fuse fuses by rank
→ _rerank with a cross-encoder
→ results (with a source marker: temp / persist)
```

## RRF: fuse by rank, not score

`_rrf_fuse`: each chunk's RRF score = `sum(1 / (k + rank))`, rank from 1,
`RRF_K = 60` (the industry default).

Why rank, not score: vector scores are 0-1, BM25 is 0-10+ — **the scales
cannot be aligned**; ranks are naturally comparable, and chunks appearing in
both rankings get boosted automatically — exactly the value of hybrid search.

**The dedupe key is "first 200 chars of content + filename"** (not the chroma
id): the same chunk carries a `[临时]` (temp) prefix in the BM25 path but not
in the vector path; id-based dedupe would miss it — content+filename merges
the two paths' copies of one chunk correctly.

## Rerank: the cross-encoder solves "modifiers drown the signal"

`_rerank`: a bi-encoder (vector search) encodes query and chunk separately; a
cross-encoder feeds **both together** into the model and can judge "does this
chunk actually answer the query semantically". That is the final answer to
"long query modifiers drown the relevant chunk" in hybrid search.

- **Reranker unavailable → keep the original order (degrade, never fail)**:
  reranking is a quality enhancement, not a requirement.
- The reranker is bge-reranker-v2-m3, lazy-loaded (first use ~30 seconds; see
  the manual's "only the first time is slow").
- Failures **warn loudly** — a quietly degraded reranker makes people believe
  retrieval quality was always this weak.

## temp-first routing

`query_for_agent` (the string entry for the model):

1. Temp attachments exist (files on disk or in the collection) → **search the
   temp store first**;
2. No temp hits (the query is unrelated to uploaded files) → fall to the
   persistent store;
3. Neither → search the persistent store directly (the original behavior).

`progress_callback` passes through to the lazy build so the UI can show
progress during OCR/indexing.

## Retrieval vs full-text loading (separation of duties)

| Entry | Purpose | When |
|---|---|---|
| `query_for_agent` / `search` | find **fragments** by similarity | specific facts, clauses, numbers, keywords |
| `load_full_file` | read the **whole file** (reuses multimodal parsing) | understanding overall structure, mechanisms, cross-section content |

`with_images=True` returns the image's **text description** (multimodal
generated), not inline images — a "image→text" translation loss; detail
questions ("what does the third branch in the image say") may be inaccurate.
Known limitation, recorded in the header notes.

`list_knowledge_files` merges persistent + temp listings, temp files with a
`[临时]` prefix; the `include_system` parameter decides whether system
documents (`_system/`) are included — hidden from the user and hidden from the
model are two different things.

## Hands-on recipes

**Case A: tune RRF and rerank**
`RRF_K` (default 60; higher = less influence for lower ranks) and `min_score`
(search default 0.45, vector-side gate). Before tuning, use the acceptance
query — long modifiers, key info at the end (the criterion in the header
notes); tune parameters only, not structure.

**Case B: change retrieval routing**
The three temp-first paths live in `query_for_agent`. Keep the "just-uploaded
files come first" semantics — the user uploads and immediately asks; hitting
the temp store is the expected behavior.

**Case C: reshape query_for_agent output**
The output shape (parse notes, source markers, chunk concatenation) directly
affects what the model reads. After changing, diff before/after with the same
question; make sure parse notes are not lost
(`_build_parse_warning_block` tells the model "this file parsed incompletely").

## How to verify you got it right

1. `bash run_tests.sh`.
2. Acceptance criterion: a question with long modifiers and key info at the end
   hits the short clause at the end of the text.
3. Upload a file then immediately ask something unrelated: it should fall to
   the persistent store (temp-first must not return empty).
4. With the reranker unavailable (temporarily rename its weights directory):
   retrieval degrades to original order, with a loud warning in the logs.

---

← Back to [08 overview](08-knowledge-base.md)
