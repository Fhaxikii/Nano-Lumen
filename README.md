# <img src="assets/nano_icon_preview.png" height="34" alt="Nano" align="top"> Nano-Lumen

![Python](https://img.shields.io/badge/Python-3.10-3776AB) ![Platform](https://img.shields.io/badge/Platform-Windows%20Desktop-0078D6) ![License](https://img.shields.io/badge/License-Apache--2.0-brightgreen) ![Release](https://img.shields.io/github/v/release/Fhaxikii/Nano-Lumen)

![Nano-Lumen](assets/nano-banner.png)

**Nano-Lumen v1.96** · Windows 桌面常驻型通用智能体 · [English](README.en.md)

`常驻 AI Agent` · `任务级持久状态` · `本地优先隐私` · `Windows 桌面端`

---

## 如果 AI 真正生活在你的电脑里，会是什么样？

当前普遍的 AI Agent 设计范式，都是临时入驻电脑并完成任务：

> 打开一个会话。
> 选择一个工作区。
> 给它一个目标。
> 它调用几个工具。

Nano 从另一个假设出发：**电脑不应该只是 AI 可以调用的一组工具，而应该成为智能体所存在的环境。**

文件、应用程序、进程、知识、网络、外部服务——它们不应该只是一个个被接入 AI 的功能，而应该成为智能体可以认识、使用和行动于其中的世界。

Nano 想做的，就是尝试构建这样的世界。

于是，问题就不再只是：

**AI 能做什么？**

而变成了：

**一个真正需要长期存在于计算机中的 AI，需要什么？**

Nano 是对这个问题的一次探索。

---

## ⚙️ 核心技术架构：把 Agent 当持久状态系统来设计

围绕持久状态系统的设计目标，Nano 从内核层面构建了完整的状态保障体系：

- **SQLite 命令内核**：全部运行态操作的唯一写路径，带不变量检查、幂等记账、崩溃后精确恢复
- **全链路崩溃恢复**：write-ahead 日志 + 启动时 reconciler，覆盖 `excepthook` 抓不到的原生崩溃
- **持久收件箱**：用户的话写入即持久，永不丢失；崩溃重投时如实告知用户
- **等待机制三重兜底**：到期 / 截止 / 无条件孤儿回收，避免任务无限挂起，保障系统始终处于可响应状态
- **持久记忆**：工作记忆 + 持久语义记忆双栈，支持主动回忆
- **设计视角**：把 Agent 当作有持久状态的分布式系统来构建，而不是单次进程。
- **目标驱动**：放弃传统 Agent 工具驱动的设计范式，给予自进化、发现及自主接入能力，能力上限由整个互联网生态决定，不由自身决定

> 详细架构说明见 [docs/zh/02-architecture.md](docs/zh/02-architecture.md)。

---

## ✨ 核心能力

**💬 对话与模型**

- **多厂商多模型支持**：内置 Anthropic Claude 与 DeepSeek 支持，同步支持中转 API
- **流式回复，过程可视化**：工具调用卡、用量统计等信息实时可见

**🧰 工具与技能**

- **Agent 执行引擎**：支持 ReAct 工具循环、单文件技能体系，兼容标准 MCP（Model Context Protocol）协议，支持本地 stdio 与远程 HTTP 两种传输方式，可接入任意符合标准的第三方 MCP 服务端
- **30+ 内置工具**：文件读写/搜索/编辑、RAG 检索、任务列表、后台任务与等待唤醒、图像/屏幕、OS 自动化、MCP 与技能管理、子智能体等；统一按需感知、动态注入，压低每轮上下文成本
- **自写技能**：让 Nano 按你的需求直接创建、修改、审计单文件 Skill，并支持查看源码、更新备份、禁用/启用、删除归档
- **MCP 自主发现与接入**：自动搜索官方 MCP Registry，一条指令即可接入与管理 MCP 服务；常驻连接、断线自动重连，跨服务统一管理

**🧠 记忆与上下文**

- **对话原文持久化**（SQLite），重启不丢失，长对话自动分层压缩，控制成本
- **上下文治理**：依据上下文预算、内容新鲜度与记忆层级动态组织对话内容，在保持连续性的同时控制上下文规模与模型调用成本
- **语义长期记忆**：长期记住你的偏好、纠错信息等
- **会话数据导出**：支持将全部会话导出为 Markdown、原始 JSON 与图片副本；导出遵循用户可见性边界，历史数据即使不再显示在界面中，仍保留完整的数据出口

**📚 知识库（RAG）**

- 本地嵌入模型（BGE-M3 + 重排模型），离线检索

**🤖 主动智能（实验性，shadow 观测中）**

- 三层主动引擎（硬安全 / 常规触发 / 状态推断）+ 情绪模型已实现，当前处于 **shadow 观测期**：只记录决策、不真正开口，不会在你未要求时突然发消息。情绪仅影响语气与主动闸门，不改变底层功能行为

**🎛️ 系统操作与安全**

- **操作管控**：系统操作按风险分级，六个独立权限开关 + 带分类器的 Auto 模式；Auto 下以「动作是否符合你的意图」判定危险命令，阻断不符合意图一致性判定的高风险动作
- **操作轨迹感知**：仅记录「在哪个应用、在做什么」级别的环境摘要（约 18 小时过期），行为账本使用封闭类别、不含敏感语义，默认无遥测
- **子智能体**：可派生受限作用域子智能体并行探索或执行任务，工具与运行范围经白名单隔离
- **原生窗口**：基于 NiceGUI + WebView2 构建的原生窗口化交互界面
- **可审计操作流水**：OS 动作（含只读动作）逐条写入 append-only 审计日志，相关截图按配置的保留策略自动回收；空闲时自动验证视觉定位（UIA）链路是否正常

---

## 🚀 安装与快速开始

### 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 及以上 |
| Python | **3.10**（版本敏感，更高版本未验证） |
| 磁盘空间 | 约 6 GB（含本地嵌入模型约 2.3 GB） |
| 内存 | 建议 8 GB 以上 |

### 安装

执行 `install.bat` 一键安装。脚本自动检测并静默安装 Python 3.10.11，并装齐 Python 依赖、本地检索模型、Node、OCR、WebView2 等全部组件；仅当下载失败时提示用户手动安装对应项。

```bat
install.bat
```

下载本地嵌入模型（知识库检索用，可选，支持断点续传）：

```bat
py -3.10 _setup_rag_models.py
```

### 启动

```bat
start.bat
```

### 首次对话配置

首次启动若未配置密钥，会自动弹出环境配置页面；也可随时点击窗口左上角的三个彩色圆点进入设置。选择模型厂商并填入 API Key：

- **选择供应商**：Anthropic 或 DeepSeek
- **API Key**：所选供应商的密钥

配置会写入项目根目录的 `.env`。更完整的配置项见 [docs/zh/03-configuration.md](docs/zh/03-configuration.md)，首次使用与常见问题的排错见 [docs/zh/01-getting-started.md](docs/zh/01-getting-started.md)。

---

## 🧩 技能与扩展

技能（Skill）是 Nano 的插件机制，一个技能 = 一个 Python 文件：

```
skills/
├── official/          # 官方内置技能
│   ├── GetSystemTime.py
│   ├── Base64Codec.py
│   ├── HashGenerator.py
│   ├── RegexTester.py
│   ├── SearchTheWeb.py
│   └── ...
├── disabled/          # 被禁用的技能
├── deleted/           # 被删除的技能
└── (skills/ 根目录)    # Nano 编写的技能放在这里
```

把技能文件放进 `skills/` 根目录即被自动加载，无需修改框架代码。你也能直接**让 Nano 根据需求编写并部署、或修改一个已有技能**（`create_new_skill` / `update_existing_skill`），不必手写代码。

- **技能开发指引**见 [docs/zh/04-writing-a-skill.md](docs/zh/04-writing-a-skill.md)
- **MCP**：标准协议，官方 `mcp` SDK 实现，支持本地 stdio 与远程 HTTP 两种传输方式，可接入任何符合标准的第三方 MCP server。接入方法见 [docs/zh/05-mcp-servers.md](docs/zh/05-mcp-servers.md)

---

## 🏗️ 架构概览

```
┌──────────────────────────────────────────┐
│ 窗口层 · pywebview / WebView2             │
└─────────────────────┬────────────────────┘
┌─────────────────────▼────────────────────┐
│ 界面层 · app.py（NiceGUI）                 │
└─────────────────────┬────────────────────┘
┌─────────────────────▼────────────────────┐
│ 编排层 · core/orchestrator.py             │
│ ReAct 主循环                              │
└─────┬──────────────┬───────────────┬─────┘
      │              │               │
┌─────▼────┐   ┌─────▼────┐   ┌──────▼─────┐
│ 模型层    │   │ 工具层    │   │ 存储记忆层   │
│ provider │   │ tools    │   │ memory     │
│ models   │   │ skills   │   │ context    │
│          │   │ os_layer │   │ rag        │
│          │   │ mcp      │   │ runtime    │
└──────────┘   └──────────┘   └────────────┘
```

界面层不直接调用模型或工具，全部经过编排层。完整说明见 [docs/zh/02-architecture.md](docs/zh/02-architecture.md)。

---

## 📁 目录结构

```
Nano-Lumen/
├── app.py                    # 界面层
├── nano_koala.py             # 精灵图动画
├── core/                     # 核心逻辑
│   ├── orchestrator.py       # 编排层 / ReAct 主循环
│   ├── provider.py           # 模型 API 接入
│   ├── models.py             # 厂商 / 模型表
│   ├── rag.py                # 知识库检索
│   ├── mcp_client.py         # MCP 客户端
│   ├── os_layer/             # 自动化
│   ├── context/              # 上下文计量与衰减
│   ├── proactive/            # 主动智能
│   ├── runtime/              # 任务调度 / 会话持久化
│   └── …                     # 其余模块见 docs/zh/02-architecture.md
├── memory/                   # 对话历史存取
├── skills/                   # 技能（插件）
├── config/                   # 行为规则 / 人格 / 系统指令
├── data/                     # 运行期数据
├── docs/                     # 开发者文档
├── tests/                    # 测试套件
├── assets/                   # 图标 / 字体 / 素材
├── static/                   # 前端静态资源
├── requirements_cpu.txt      # Python 依赖
├── install.bat / install.ps1 # 一键安装
└── start.bat                 # 启动入口
```

---

## 🛡️ 安全说明

桌面自动化是风险最高的能力，Nano 为此设计了多层防护：

- **风险取最大值**：每个动作的生效风险 = max(声明风险, 静态地板, 动态升级规则)，模型只能把风险抬高、不能压低
- **高危总闸**：`PERM_DANGEROUS` 刻意不绑定任何具体动作，凡是风险达到最高档的动作都必须经过它
- **路径黑名单**：明确拒绝读取凭据、私钥、浏览器密码等敏感位置
- **权限拒绝时可解释**：被拒时模型会告诉你需要打开哪个开关
- **自动模式兜底**：开启自动模式后，破坏性命令仍会被命令分类器拦下

六个权限开关随时可在设置中关闭。详见 [docs/zh/06-os-automation.md](docs/zh/06-os-automation.md)。

---

## 🔐 隐私与信任

桌面端智能体的核心信任门槛是隐私。Nano 从架构层面贯彻「本地优先、最小采集、行为克制」的信任原则：

**本地优先**

OCR、向量检索、记忆存储、交互界面全部在本机运行；唯一上行的数据是用户自行配置的模型 API 请求。

**最小采集**

- **环境轨迹**只存储「应用 + 在做什么」的一行摘要（如「Chrome 正在浏览网页」），不存原始事件和内容明文，记录 18 小时后自动过期清除
- **行为账本**只用封闭类别标签（如「文件操作」「网络访问」），禁止任何语义化敏感信息入库

**无遥测**

Nano 自身不收集使用统计或用户行为数据。

---

## ⚠️ 已知边界

本章仅陈述明确的当前限制，不承诺未落地功能，不预设发布日期。也欢迎任何人参与对 Nano 的贡献。

**明确未落地 / 当前限制：**

- 仅支持 Windows 10 及以上，无跨平台版本
- 主动行为引擎处于 shadow 观测期，暂不对外提供主动交互能力，**用户不可手动开启**；界面的「主动程度」档位为预留，**目前没有任何实际影响**。待充分验证 Shadow 日志、确认行为可靠性后，将通过修改代码常量正式开放
- 考拉动画为与状态机绑定的精灵图动画，**非真骨骼**，目前稍显过时
- Nano 开发期测试全部使用 Claude API，DeepSeek 工具调用稳定性 / Token 缓存命中**未深度测试**
- I18N 未完成：当前语言切换仅影响各功能下模型生成语言时的偏好，**不影响 UI 语言**
- NiceGUI 无法实现内置浏览器

---

## 🧪 开发与测试

- 测试套件位于 `tests/`，随仓库分发，通过 `run_tests.sh` 入口执行全量回归

单条用例：

```bat
py -3.10 tests\t_d12_tool_failure_info.py
```

一键全量（需 bash）：

```bash
bash run_tests.sh
```

- 开发者文档在 `docs/`（`zh/` 与 `en/`，另附 `docs/GLOSSARY.md` 术语表）
- 运行测试、构建、贡献规范见 [docs/zh/10-testing.md](docs/zh/10-testing.md) 与 [docs/zh/11-contributing.md](docs/zh/11-contributing.md)

---

## 🐛 反馈问题

提交 Issue 前，请先确认：

- 已阅读 [docs/zh/01-getting-started.md](docs/zh/01-getting-started.md) 的「常见启动问题」
- 已看「⚠️ 已知边界」一节——其中列出的当前限制不在反馈范围内

报告 Bug 时请附：

- Nano 版本号
- 操作系统与 Python 版本
- 复现步骤、期望行为、实际行为
- 相关日志（`data/` 目录或控制台输出）

完整的 **Bug 报告模板**与 **功能建议模板**见 [docs/zh/11-contributing.md](docs/zh/11-contributing.md)「报告问题」一节，可直接照模板填写。

---

## 📚 文档

- 入口：[docs/zh/README.md](docs/zh/README.md)
- 版本历史：[Changelog.txt](Changelog.txt)

---

## 📄 开源许可

本项目采用 [**Apache-2.0**](LICENSE) 开源许可证。

---

## 💛 致谢
*Nano 的诞生离不开以下开源项目、服务与贡献者的启发与支撑：*

- **Anthropic**：Nano 大量参考了 Claude Code 的设计范式，详见 [Anthropic 工程文档](https://www.anthropic.com/engineering)
- **BAAI**：bge-m3 嵌入模型与 bge-reranker-v2-m3 重排模型
- **NiceGUI、pywebview（WebView2）、CodeMirror**：桌面界面与交互
- **SQLite、ChromaDB、Tesseract**：存储、检索与 OCR 底座
- **Model Context Protocol（MCP）**：让工具生态得以标准化扩展
- **《Designing Data-Intensive Applications》（Martin Kleppmann 著）**：「把 Agent 当作有持久状态的系统来设计」的理论来源
- **GNU nano**：Nano 的命名由来

**作者与贡献者**

- [Koala](https://github.com/Fhaxikii) — 项目作者与主要开发者
- [lebangjames](https://github.com/lebangjames) — 数据收集 / 测试及早期原型设计思路

**AI 协作贡献者**

- Claude（[Anthropic](https://www.anthropic.com)）— 在部分代码实现与调试中提供了大量协助
- GPT（[OpenAI](https://openai.com)）— 内核、OS 控制层架构设计
- GLM（[智谱](https://www.zhipuai.cn)）— 文档规范化与国际化