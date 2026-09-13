# 08c · 健康度与临时附件

**这篇讲什么**：横切面的两块——健康度探针与 parse_report（让"能力坏了"看得见）、临时附件的注册制 lazy build（上传只存盘，搜索时才建索引）、以及 chroma 损坏的自愈链路。  
**读完你能做什么**：新增探针、扩展 parse_report、或改动临时附件生命周期而不引入"意外索引历史文件"的问题。  
**前置**：[08 总览](08-knowledge-base.md)、[08a 解析与索引](08a-parsing-indexing.md)。  

> 语言：中文 · [English](../en/08c-health-temp.md)  

---

## 健康度探针：五个，一个都不能少

`_register_health_probes` 向 HealthRegistry 登记（读动作查询，不影响性能）：

| 探针 | 盯什么 |
|---|---|
| `KB_STORE` | 向量库可打开 |
| `KB_VECTOR_SEARCH` | 嵌入模型可加载 |
| `KB_RERANKER` | 重排模型可加载 |
| `KB_KEYWORD_SEARCH` | BM25 可构建 |
| `OCR_TESSERACT` | OCR 可用 |

⚠️ **reranker 与 BM25 这两条是补齐的**：它们都会**静默降级**（reranker 挂了
维持原序，BM25 挂了只剩向量），不注册探针就永远看不见——"能力坏了但没人
上报"这一类的直接解法。探针的产出在监控抽屉（见 09c 的可用性优先显示）。

## parse_report：每个文件的解析报告

`data/parse_reports.json`，每个入库文件一条：状态、提取字符数、图片数、
走了哪条解析路径（如 PDF 命中 pdfplumber+tables 还是 OCR）。

- 喂给**健康度面板**（UI），让"这份文件解析不完整"看得见。
- `_build_parse_warning_block` 把报告转成给模型的提示——检索命中一份解析
  不完整的文件时，模型会知道"结果可能不全"。
- **不参与增量判定**：哈希判定只看文件内容（见 08a）；解析器升级不应被
  误判为"内容变了"。

## 临时附件：注册制 lazy build

上传文件的生命周期（Phase 3 设计）：

```
用户上传 → 存盘 + register_temp_file 登记到当前会话
（此时只存盘，不建索引）
→ 用户搜索且 source 含 temp → _ensure_all_temp_files_indexed
   才对注册过的文件 lazy build（OCR/解析/嵌入，可能很慢）
→ 会话结束后 cleanup_stale_temp_files 清理历史遗留
```

**注册制的原因**（头部注释原文逻辑）：扫目录会把历史遗留文件（上次会话
未清理的大文件）全部意外索引；只有"本轮会话明确上传的文件"才参与 lazy
build。`progress_callback` 让 UI 在 OCR/建索引时显示进度文本。

临时文件入库进独立的 temp collection，文件名带 `[临时]` 前缀标记（与
`search` 的 source 过滤一致，保证 RRF 融合时两路正确合并去重，见 08b）。

## chroma 损坏的自愈链路

向量库（HNSW）在特定环境下可能结构损坏。自愈链路（`_backup_and_reset_chroma`）：

```
检测到结构性失败（_is_structural_chroma_failure）
→ 备份损坏库 → 重置进程内 chroma 状态 → 删除孤儿 segment
→ _schedule_reindex_after_heal 后台触发一次全量索引
```

⭐ **为什么要单独调度自愈重建**：自愈可能发生在**任何**打开向量库的时刻。
若发生在启动索引里，调用方拿到空库后会自己重建，不必再做；但若发生在一次
普通查询里（用户搜东西时才第一次打开库），**没有任何人会去重建索引**——
用户会得到一个永远搜不到东西的空库。所以无条件补一次，靠
`_reindex_after_heal_started` 保证每进程只补一次；重复了也无害
（`index_documents` 幂等，哈希增量）。

## 改动手把手

**场景 A：新增一个探针**
模仿 `_probe_embedder` 的形状（只读、返回 bool、永不抛），在
`_register_health_probes` 登记。自检问题：这个能力坏掉时是**静默降级**吗？
是 → 必须有探针。

**场景 B：扩展 parse_report**
`_new_report` / `_set_status` / 各 `_parse_<ext>` 的字段。新增字段要同步
`_build_parse_warning_block`（让模型看到）与健康度面板（让人看到）。

**场景 C：改临时附件生命周期**
注册/清理/collection 三处联动。改动时保持两条不变量：历史遗留文件不被
意外索引（注册制）；临时文件标记与 source 过滤一致（RRF 合并去重）。

## 怎么验证你改对了

1. `bash run_tests.sh`（本篇对应 `t_shot_prune.py`、`t_d10_user_images.py`）。
2. 真机：上传一个大 PDF，立即搜索——UI 应显示 lazy build 进度，完成后
   命中临时库。
3. 让 reranker 不可用，监控抽屉应显示降级档（不是无显示）。
4. 自愈路径：备份向量库目录制造损坏，启动后应看到"自愈后重建索引"日志，
   检索恢复正常。

---

← 返回 [08 总览](08-knowledge-base.md)
