# 08a · 解析与索引

**这篇讲什么**：文件如何变成可检索的 chunk——十种格式的解析策略（含 PDF 三层 fallback 与增强模式）、切块规则（表格整体成 chunk 防幻觉错配）、`[FileSchema]` 结构摘要 chunk、增量索引与模型加载。  
**读完你能做什么**：新增一种文件格式、调整切块策略、或改动增量索引逻辑而不破坏哈希判定。  
**前置**：[08 总览](08-knowledge-base.md)。  

> 语言：中文 · [English](../en/08a-parsing-indexing.md)  

---

## 支持的格式与解析器

`SUPPORTED_EXTENSIONS`（`core/rag.py` 常量区）：txt / md / pdf / docx / pptx /
xlsx / xls / csv + 图片（jpg/jpeg/png/webp/bmp/gif）。每个格式一个解析函数
`_parse_<ext>`，由 `_parse_file` 分发。

关键解析策略：

- **PDF 三层 fallback**（`_parse_pdf`）：pdfplumber（文本型，版面好）→
  pymupdf（pdfplumber 搞不定的）→ PaddleOCR（扫描型兜底，慢）。降级判据：
  当前层提取的总字符 < 总页数 × 30（约一行中文）→ 认为基本没提到东西，进下一层。
- **增强模式**（`enhanced_mode`，来自 UI 的 `_enhanced_mode` 设置，经
  `index_config` 传入）：docx/pptx/pdf 提取内嵌图片并生成多模态描述；
  `max_ocr_pages` 限制 OCR 页数（默认 50）。
- **xlsx 按 cell 读取**（openpyxl），保留位置信息（`[Sheet: xxx]` +
  `coordinate=A1`），统计 chart/image。
- **图片文件**走 `_describe_image_multimodal` 生成文字描述入库。
- **单文件大小硬上限** `MAX_FILE_SIZE_MB = 50`：超大文件解析峰值内存可能
  十倍于文件本身，改此值需谨慎。

## 切块：表格整体成 chunk（防幻觉错配）

`_chunk_text`（默认 500 字符、重叠 80）的核心规则：

- **表格不参与硬切**。pdfplumber/docx 解析出的表格以 `[Table]` 标记开头，
  切块前先按 `[Table]` 边界分段，表格段整体作为一个 chunk。
- 原因：多张同结构表格（如"当月持续在营率"与"营内三晋率"）如果跨 chunk
  被切，模型会把不同表格的数字混搭，产生**幻觉性数据错配**。
- 超大表格（超过 `chunk_size × 4`，默认 2000 字符）按行切分，每个子 chunk
  保留 `[Table continued]` 前缀避免丢失上下文。
- overlap ≥ chunk_size 时强制回退到四分之一（防配置错误）。

## `[FileSchema]` 结构摘要 chunk

`_generate_schema_chunk` 从解析后的文本提取**文件骨架**（不含具体数据）：
XLSX 的 Sheet 名+列名、PDF/DOCX 的章节标题+表格标题、CSV 列名、TXT/MD 的
标题行。存入 chroma 时 `metadata.chunk_type=schema`。

作用：RAG 召回时结构摘要与数据 chunk 自然一起被拉出，模型同时看到
"表 2.4 = 认知轴"和具体数字——**能完整定位，而不是拿着一段孤立的数字猜**。

## 增量索引

`index_documents` 的判定链：

1. 扫描 `data/knowledge/`，过滤 Office 临时锁文件（`~$xxx`）与隐藏文件。
2. 清理 parse_reports 里源文件已不存在的记录。
3. **按文件哈希判定**（`_file_hash` → `data/indexed_hashes.json`）：哈希未变
   → skip；变了 → 先按 source 删旧 chunk（`_collection_delete_by_source`）
   再重切重嵌。
4. 每个文件生成 parse_report（状态、字符数、图片数等）→
   `data/parse_reports.json`，喂给健康度面板。
5. 入库完成后**重建 BM25 索引**（增量与跳过都触发，确保索引最新）。

单文件入库走 `index_single_file`（上传后即时索引用）。

## 模型加载

- **safetensors 快照**：`_resolve_hf_snapshot` 用 `snapshot_download` 显式
  allow_patterns 只取 safetensors 与配置，**让 .bin 根本不进缓存**——
  背景：transformers 5.x 因 CVE-2025-32434 拒载 .bin（要求 torch≥2.6），
  而镜像源可能解析出 .bin-only 快照，导致 WEIGHTS_FORMAT_REJECTED 且
  重启无法自愈（v1.97 修复的完整故事见 [11-testing.md](11-testing.md) 同族
  与 Changelog v1.97）。
- **懒加载 + 崩溃 breadcrumb**：`_load_embedder` 是全项目最危险的一次调用
  （torch/transformers 与 chroma-hnswlib 同进程曾致随机 segfault，5 次复现
  3 崩）——动手**之前**写 breadcrumb 到盘，成功后清掉；segfault 走不到
  except，下次启动看到未清的痕迹就知道死在哪。
- **错误分类上报**：`_classify_model_load_error` 按根因分类（模型缺失/
  格式拒绝/网络），让 UI 与模型看得见，而不是被上游 except 吞掉。
- 空库时**不预加载**嵌入模型（让应用先正常启动）。

## 改动手把手

**场景 A：新增一种文件格式**
1. `SUPPORTED_EXTENSIONS` 加后缀（图片类同时加 `IMAGE_EXTENSIONS`）。
2. 写 `_parse_<ext>`：返回纯文本（表格用 `[Table]` 标记，供切块识别）；
   生成/更新 report（状态、统计）。
3. `_parse_file` 分发表加一行。
4. 若格式有"结构"（Sheet/章节），在 `_generate_schema_chunk` 补提取规则。
5. 测试：`tests/cases/t_f4_catalog.py` 之外，用真实文件跑 `index_single_file`
   并检索验证。

**场景 B：调切块参数**
`_chunk_text` 的 chunk_size/overlap。注意表格整体成 chunk 的规则**优先于**
滑窗参数；调大 chunk 要同时想嵌入模型的输入上限（bge-m3 为 8192 token）。

**场景 C：改增量判定**
只动 `_file_hash` / `indexed_hashes.json` 读写。**不要**把 parse_report 纳入
哈希——那是给人看的报告，纳入会让"解析器升级"误判为"内容变了"。

## 怎么验证你改对了

1. `bash run_tests.sh`。
2. 真机：放一份含表格的 xlsx 进 `data/knowledge/`，重启后检索
   "表里认知轴是什么"——应命中 schema chunk + 数据 chunk。
3. 修改同一文件再入库：旧 chunk 应被替换（向量库 chunk 数不堆积）。
4. 扫描型 PDF（纯图）应走到 OCR 层，parse_report 状态可见。

---

← 返回 [08 总览](08-knowledge-base.md)
