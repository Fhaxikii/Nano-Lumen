# 13 · 发布与版本

**这篇讲什么**：发布一个新版本（vX.YZ）的完整流程——版本号怎么定、仓库里哪些位置带版本号、Changelog 与 Release notes 的格式规范。  
**读完你能做什么**：独立完成一次从代码改动到 Release 上线的完整发布。  
**前置**：[12-contributing.md](12-contributing.md)、[11-testing.md](11-testing.md)。  

> 语言：中文 · [English](../en/13-release.md)

---

## 版本号规则

- 采用 `v主.次` 两位递增（v1.96 → v1.97），次位步长 0.01。
- **整数位留给纪元切换**（如主动智能正式开放、开源后第一个稳定大版本时跳 v2.0），不因「数字涨得快」而提前跳。
- 不使用三位补丁号（v1.96.1 这类），与既有的全部历史条目保持同一形状。
- 仓库内出现版本号的位置**只有以下几处**，发布时逐一更新：

| 位置 | 说明 |
|---|---|
| `Changelog.txt` 末尾追加 `## vX.YZ` 段 | 格式见下文 |
| `app.py` UI 欢迎语 `// nano-lumen vX.YZ` | 搜索 `nano-lumen v` 定位 |
| `data/knowledge/_system/nano_manual.md` 头部「手册对应Nano程序版本号」 | |
| `README.md` / `README.zh.md` 标题下 `**Nano-Lumen vX.YZ**` | |
| `nano_manual.md`「截止 X 版本，主动开口……」 | 条件项：shadow 状态变化时必须改，未变也建议随手跟 |

- docs 08/11 与测试注释里出现的版本号是**示例或历史记录**，永不更新。

## Changelog 规范

- 新版本段落**追加在文件末尾**（整个文件按时间升序排列）。
- 段内用 `### 修复` / `### 优化` / `### 新增` 分节。
- 每个版本段落之间用 `---` 分隔，段落与分隔线之间保留空行。
- **只记录面向用户的变更**（行为、修复、功能）。仓库门面类改动（截图、文档结构调整）不进 Changelog——Changelog 是唯一权威账本，Release notes 是它的橱窗，橱窗不放账本上没有的东西。

## Release 流程

1. 发布前 `bash run_tests.sh` 全量通过。
2. 按上表更新所有版本号位置。提交时**逐个点名 `git add <文件>`**。
   🔴 禁用 `git add -A`：运行期目录 `data/` 下随时可能新生成含本机绝对路径的文件（`indexed_hashes.json` / `parse_reports.json` 都这么混进过仓库）。
   `data/` 已改为白名单制：`data/*.json` 默认忽略，仅放行 `china_regions_city.json`、`model_config.json` 与 `_system/` 手册。
3. 提交并推送后创建 Release：

```
gh release create vX.YZ --target main --title "Nano-Lumen vX.YZ" --notes-file <notes 文件>
```

4. Release notes 规范：
   - **只镜像 Changelog 对应版本段落**，中英对照。
   - 标题就是 `Nano-Lumen vX.YZ`，不加「第 N 个修复版本」之类的序数修饰。
   - 不写 Changelog 里没有的内容（新文档、截图等仓库改动不进 release notes）。
   - 末尾固定两条：

```
- 安装方式见 [README](https://github.com/Fhaxikii/Nano-Lumen#安装) · Install: see the [English README](https://github.com/Fhaxikii/Nano-Lumen/blob/main/README.zh.md#installation--quick-start)
- 完整变更历史见 [Changelog.txt](Changelog.txt) · Full changelog: [Changelog.txt](Changelog.txt)
```

## 标签即冻结

tag 指向的提交就是发布瞬间的快照，之后 main 上的修复**不会**自动进入已发布的版本。两个后果：

- 发布后照常在 main 修东西没问题；但浏览标签或下载 Source code 时，看到的永远是发布那一刻的树。
- **发布物被发现有问题时**：刚发布不久、确认无人下载 → 可删除 Release 与标签，把标签重新打到修复后的 main 再重建 Release；一旦可能有用户下载过 → 不动旧标签，用新版本号发布修复。

## 怎么验证你改对了

1. `git ls-files data/` 只列出随版本分发的文件，无任何运行期文件。
2. 全库搜索上一个版本号，只应命中历史记录与示例类文本。
3. Release 页 notes 完整，Source code 压缩包内无 `.env`、无运行期文件。
4. 新克隆的仓库按 [01-getting-started.md](01-getting-started.md) 走一遍首次启动。

---

← 返回 [README](README.md)
