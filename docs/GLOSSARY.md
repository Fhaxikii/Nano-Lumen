# GLOSSARY

> **What this table governs**: within the bilingual docs, one concept must map
> to exactly one word on each language side. When translating a new page,
> reviewing, or introducing a new term, check here first; if it is missing,
> add it after you translate.
>
> Maintenance rule: **adding rows is free; changing an agreed translation is
> not** — once a term has landed in a page it is frozen, and replacing it must
> be back-propagated across all language trees. This file itself is written in
> English only (the shared, language-neutral layer of `docs/`); the only
> bilingual content is the term pairs, which are the anchors.

---

## Product name and stage codenames

| 中文 | English | Notes |
|---|---|---|
| Nano-Lumen | Nano-Lumen | The product name is permanent, always hyphenated; repo is `nano-lumen`. Never `NanoLumen` / `Nano Lumen` |
| Nano | Nano | The assistant's persona name as used in the UI; invariant |
| 阶段代号（Nitor / Stella / Sidus / Aether） | stage codename | Roadmap narrative only, never concatenated with the product name; v1.x carries no codename |

## Core concepts

| 中文 | English | Notes |
|---|---|---|
| 技能 | skill | A pluggable single-file tool; the code directory is `skills/` |
| 工具卡 | tool card | The collapsible UI block showing one tool call |
| 工具调用 | tool call | — |
| 工具清单 | tool catalog | The capability list the model sees each turn |
| 编排层 | orchestration layer | Name the file (`core/orchestrator.py`) when talking code, the layer when talking responsibility |
| 上下文治理 | context governance | Umbrella term for metering, budgeting, layered decay, and digesting |
| 上下文分层（L0–L4） | context layers (L0–L4) | L0 is verbatim text, higher is more condensed; formal terms, never translated |
| 暂存器 | scratchpad | Industry term preferred over a literal translation |
| 语义记忆 | semantic memory | Persistent cross-session memory |
| 收件箱 | inbox | One of the runtime facilities |
| 授权 | authorization | The permission mechanism for desktop actions |
| 风险档 | risk level | An integer deciding whether an action needs authorization |
| 前台 / 后台 | foreground / background | Maps to `Placement.FOREGROUND/BACKGROUND` |

## RAG and the knowledge base

| 中文 | English | Notes |
|---|---|---|
| 知识库 | knowledge base | — |
| 嵌入模型 | embedding model | When naming the model, write `BAAI/bge-m3`; never translate model ids |
| 重排 / 重排器 | reranking / reranker | — |
| 入库 / 检索 | ingest / retrieval | — |

## Configuration and integrations

| 中文 | English | Notes |
|---|---|---|
| 厂商 | vendor | — |
| 中转 | relay | The legacy `NANO_API_RELAY_*` variable names stay |
| 环境配置 | Environment (settings) | The settings page 通用 → 环境配置 is General → Environment in English |
| MCP 服务器 | MCP server | MCP is never expanded in docs |

## Translation rules

1. **Program output is quoted verbatim, never translated** — e.g.
   `OK 真·全量 0 失败`, `SUCCESS`. You may add a gloss next to it, but the
   string itself is untouchable.
2. **Established English beats a literal rendering.** Use token, agent, LLM,
   GUI, risk floor, subagent as-is; do not coin new literal renderings for
   them.
3. **Code identifiers are never translated.** File names, function names,
   keys, paths stay as-is.
4. **Each language reads natively; the two are not sentence-aligned mirrors.**
   Same meaning, same terms; word order follows each language.
5. **Navigation lines inside a page are written in that page's language only**
   (the `docs/` root is the only place that speaks about all languages).
