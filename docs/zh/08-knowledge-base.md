# 08 · 知识库与检索（总览）

**这篇讲什么**：RAG 层的地图——`core/rag.py` 的四大块、检索的两路融合、文件的生命周期（入库→索引→检索→临时附件），以及三个子篇的分工。  
**读完你能做什么**：知道"我要改的东西在哪个子篇"，并理解 RAG 为什么长成混合检索 + 多级解析降级的形状。  
**前置**：[02-architecture.md](02-architecture.md)、[10-builtin-tools.md](10-builtin-tools.md)（RAG 检索是一个内置工具的可用能力）。  

> 语言：中文 · [English](../en/08-knowledge-base.md)  

---

## 四大块（`core/rag.py` 的地图）

| 块 | 函数（按函数名搜索） | 内容 | 详读 |
|---|---|---|---|
| 模型加载 | `_resolve_hf_snapshot` / `_load_embedder` / `_load_reranker` | safetensors 快照、嵌入/重排懒加载、错误分类上报 | [08a](08a-parsing-indexing.md) |
| 解析与入库 | `_parse_file` / `_chunk_text` / `_index_one_file` / `index_documents` | 十种格式解析、切块、增量索引 | [08a](08a-parsing-indexing.md) |
| 检索与融合 | `_build_bm25_index` / `_bm25_search` / `_rrf_fuse` / `_rerank` / `search` / `query_for_agent` | 向量 + BM25 两路 RRF 融合 + 重排 | [08b](08b-retrieval-ranking.md) |
| 临时附件 | `index_temp_file` / `register_temp_file` / `cleanup_stale_temp_files` | 上传文件的注册制 lazy build | [08c](08c-health-temp.md) |

贯穿全层的横切面：**健康度探针**（`_register_health_probes`，四个探针
分别盯 embedder/reranker/bm25/向量库）、**parse_report**（每个文件的解析报告，
`data/parse_reports.json`，喂给健康度面板）——见 [08c](08c-health-temp.md)。

## 检索设计的一句话

**混合检索（向量 + BM25，RRF 融合）+ cross-encoder 重排**，解决的是
"长 query 里修饰词淹没真正相关 chunk"——向量检索语义相似度高但会被无关
内容带偏；BM25 对关键词精确但不理解语义；RRF 融合两路排名再重排。
验收判据：一句修饰词很长、关键信息在末尾的提问，要能命中正文末尾那条
短条款。细节与调参见 [08b](08b-retrieval-ranking.md)。

## 文件的生命周期

```
放入 data/knowledge/ → index_documents 扫描（按文件哈希增量）
→ _parse_file 多级解析（PDF 三层 fallback：pdfplumber → pymupdf → OCR）
→ _chunk_text 切块（表格整体成 chunk）→ 嵌入入库（chroma）
→ BM25 索引重建 → 供 query_local_knowledge 检索 / load_full_file 全文读
```

哈希记录在 `data/indexed_hashes.json`，解析报告在 `data/parse_reports.json`，
向量库在 `data/chroma_db/`——**三个都是运行期文件，不在仓库里**（`data/`
白名单制，规则在仓库根 `.gitignore` 的 `data/*.json` 段）。

## 系统文档（`_system/`）

`data/knowledge/_system/` 存 Nano 自己的手册（`nano_manual.md`），可见性规则
特殊：对用户隐藏（不出现在界面的知识库列表）、对模型可见（进检索与清单）、
拒绝删除。

维护约定（完整版也适用于任何 `_system/` 下的文档）：

1. **改了界面或功能，同一次提交里更新手册。** 手册是 Nano 回答「XX 在哪」的
   权威来源；功能改了手册没改，Nano 会拿着过时信息自信地答错——比"不知道"更糟。
2. **文件头部写明对应的程序版本号**。手册随版本分发，版本号是判断过时的第一依据。
3. **一节只回答一个问题，标题用用户的问法**（如「设置在哪打开」而非「设置面板
   说明」），节内不用「详见上一节」——片段被单独召回时指代会断。
4. 便于关键词命中：用户会用什么词问，标题和正文里就要出现那个词。

## 三个子篇

| 子篇 | 面向的改动方向 |
|---|---|
| [08a · 解析与索引](08a-parsing-indexing.md) | 新增格式、切块策略、增量索引、模型加载 |
| [08b · 检索与排序](08b-retrieval-ranking.md) | RRF/重排调参、检索路径、query_for_agent |
| [08c · 健康度与临时附件](08c-health-temp.md) | 探针、parse_report、临时附件注册制、chroma 自愈 |

## 两条铁律

1. **哈希增量判定基于文件内容哈希**。哈希未变的文件跳过，变了的先删旧
   片段再重切（不然旧 chunk 堆积）；parse_report 是给人看的报告，不参与判定。
2. **检索与全文加载职责分离**：RAG 查片段（`query_for_agent`），
   `load_full_file` 读完整文件——"找具体事实"走检索，"理解整体结构"走全文。

---

## 怎么验证你改对了

本页是地图。验证方式见各子篇末尾；全量回归 `bash run_tests.sh`。

---

← 返回 [README](README.md)
