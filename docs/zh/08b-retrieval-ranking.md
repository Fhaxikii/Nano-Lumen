# 08b · 检索与排序

**这篇讲什么**：检索的两路融合——向量 + BM25 由 RRF 按排名融合、cross-encoder 重排、temp-first 路由，以及检索与全文加载的职责分工。  
**读完你能做什么**：调整 RRF/重排参数、修改检索路由、或改 `query_for_agent` 的输出形状。  
**前置**：[08 总览](08-knowledge-base.md)、[08a 解析与索引](08a-parsing-indexing.md)。  

> 语言：中文 · [English](../en/08b-retrieval-ranking.md)  

---

## 检索流水线

`search`（core/rag.py）：

```
query（≥ MIN_QUERY_CHARS=4 个非空字符，否则直接空结果）
→ lazy build 临时附件索引（source 含 temp 时）
→ 向量检索（chroma） + BM25 关键词检索（并行两路）
→ _rrf_fuse 按排名融合
→ _rerank 用 cross-encoder 重排
→ 结果（含 source 标记：temp / persist）
```

## RRF：按排名融合，不按分数

`_rrf_fuse`：每个 chunk 的 RRF 分数 = `sum(1 / (k + rank))`，rank 从 1 起，
`RRF_K = 60`（业界默认）。

为什么按排名不按分数：向量分数是 0-1，BM25 是 0-10+，**尺度不可对齐**；
排名天然可比，且自动给"在两路排名里都出现"的 chunk 加分——这正是混合
检索的价值所在。

**去重 key 是"内容前 200 字 + filename"**（不是 chroma id）：同一个 chunk
在 BM25 路径带 `[临时]` 前缀、向量路径不带，按 id 去重会漏；按内容+文件名
去重才能把两路的同一 chunk 正确合并。

## 重排：cross-encoder 解决"修饰词淹没"

`_rerank`：bi-encoder（向量检索）分开编码 query 和 chunk；cross-encoder 把
两者**一起**送进模型，能判断"这个 chunk 在语义上是否真的回答了 query"。
这是混合检索里"长 query 修饰词淹没真正相关 chunk"的最终解法。

- **reranker 不可用 → 维持原序降级**（不失败）：重排是质量增强，不是必需品。
- 重排模型 bge-reranker-v2-m3，懒加载（首次约 30 秒，见手册"第一次会慢"）。
- 失败**响亮告警**——一个安静降级的重排，会让人以为检索质量天生就这样。

## temp-first 路由

`query_for_agent`（给模型的字符串入口）：

1. 有临时附件（磁盘有文件或 collection 有内容）→ **先搜临时库**；
2. 临时库无命中（query 跟上传文件无关）→ 落到持久库；
3. 都没有 → 直接搜持久库（原有行为）。

`progress_callback` 透传给 lazy build，让 UI 能在 OCR/建索引时显示进度文本。

## 检索 vs 全文加载（职责分离）

| 入口 | 用途 | 什么时候用 |
|---|---|---|
| `query_for_agent` / `search` | 按相似度找**片段** | 找具体事实、条款、数字、关键词 |
| `load_full_file` | 读**完整文件**（复用多模态解析） | 理解整体结构、机制、跨章节内容 |

`with_images=True` 返回的是图片的**文字描述**（多模态生成），不是内联图片
——有"图→文"翻译损耗，细节型问题（"图里第三个分支写什么"）可能不准，
这是已知局限（头部注释有记录）。

`list_knowledge_files` 合并持久库 + 临时库清单，临时文件带 `[临时]` 前缀；
`include_system` 参数区分系统文档（`_system/`）是否包含——对用户隐藏与对
模型隐藏是两件事。

## 改动手把手

**场景 A：调 RRF 与重排**
`RRF_K`（默认 60，越大排名靠后的影响越小）与 `min_score`（search 默认 0.45，
向量侧门槛）。调整前先用"修饰词长、关键信息在末尾"的问句做验收（头部注释
的判据）；只调参数不动结构。

**场景 B：改检索路由**
temp-first 的三条路径在 `query_for_agent`。改路由时保持"上传的文件优先"
语义——用户刚传了文件就问，命中临时库是预期行为。

**场景 C：改 query_for_agent 输出**
输出形状（解析提示、来源标记、chunk 拼接）直接影响模型阅读。改完用同一
个问题对比前后输出，确认没有丢失 parse 提示（`_build_parse_warning_block`
负责把"这份文件解析不完整"告诉模型）。

## 怎么验证你改对了

1. `bash run_tests.sh`。
2. 验收判据：修饰词很长、关键信息在末尾的提问，命中正文末尾的短条款。
3. 上传一个文件后立刻问与它无关的问题：应落到持久库（temp-first 不该
   返回空）。
4. 重排模型不可用（可临时改名权重目录）时：检索应降级为原序，且日志有
   响亮告警。

---

← 返回 [08 总览](08-knowledge-base.md)
