# Nano 开发者文档

**这篇讲什么**：这套文档的结构，以及你应该从哪一篇开始读。  
**读完你能做什么**：判断出与你的目标相关的是哪几篇，跳过其余部分。  
**前置**：无。  

---

## 这套文档面向谁

面向想要阅读、修改或向 Nano 提交改动的开发者。它不解释产品怎么使用，
只解释代码怎么组织、改动应该落在哪里、以及如何验证改动是否正确。

## 按目标选择入口

| 你想做的事 | 应该读 |
|---|---|
| 安装并启动 | [01-getting-started.md](01-getting-started.md) |
| 了解整体结构再决定改哪里 | [02-architecture.md](02-architecture.md) |
| 修改配置项或增加配置 | [03-configuration.md](03-configuration.md) |
| 新增一个工具（技能） | [04-writing-a-skill.md](04-writing-a-skill.md) |
| 接入一个 MCP server | [05-mcp-servers.md](05-mcp-servers.md) |
| 修改桌面自动化、权限或风险判定 | [06-os-automation.md](06-os-automation.md) |
| 修改上下文管理或记忆 | [07-memory-and-context.md](07-memory-and-context.md) |
| 修改知识库与检索 | [08-knowledge-base.md](08-knowledge-base.md) |
| 修改界面或新增面板 | [09-ui.md](09-ui.md) |
| 运行测试或新增测试 | [10-testing.md](10-testing.md) |
| 提交 PR 之前 | [11-contributing.md](11-contributing.md) |
| 发布一个新版本 | [12-release.md](12-release.md) |
| 了解使用责任与免责条款 | [user-agreement.md](user-agreement.md) |

新增功能的入口通常是 `04` 和 `05`。这两处是"增加一个东西"，不涉及核心改动，
是风险最低的贡献方式。

## 阅读顺序建议

第一次接触本项目，建议按 `01` → `02` → 目标篇的顺序阅读。
[02-architecture.md](02-architecture.md) 建立了后续每一篇都会用到的术语和结构，跳过它会导致
后面每一篇都需要重新解释上下文。

## 这套文档不包含什么

- 产品的使用说明。
- 历史决策记录。开发过程中"当初为什么这样决定"的记录属于内部资料，
  不随发行版分发。本套文档回答的是"现在应该怎么做"，两者不是同一类问题。
- 版本变更记录。见仓库根目录的 [[Changelog.txt](../../Changelog.txt)](../../Changelog.txt)。

## 语言

中文版是原始版本。其他语言版本均为中文版的翻译：

- [English](../en/README.md)
- 术语表（双语词条、英文行文）：[GLOSSARY](../GLOSSARY.md)
