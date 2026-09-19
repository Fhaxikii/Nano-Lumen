# Security Policy / 安全政策

## Supported versions / 支持的版本

Security fixes are only provided for the **latest release** and the `main` branch.
/ 本项目仅对**最新发布版本**与 `main` 分支提供安全修复。

| Version / 版本 | Supported / 支持状态 |
|---|---|
| Latest release / 最新 Release | ✅ |
| Older versions / 旧版本 | ❌ (please upgrade first / 请先升级) |

## How to report a vulnerability / 如何报告漏洞

**Please do not report security vulnerabilities through public issues.**
/ **请不要通过公开 Issue 报告安全漏洞。**

Use GitHub's **Private vulnerability reporting** (Security tab → Report a
vulnerability) to report privately.
/ 请使用 GitHub 的 **Private vulnerability reporting**（仓库 Security 标签页 →
Report a vulnerability）私下报告。

When reporting, please include:
/ 报告时请尽量包含：

- Affected version and commit, if determinable / 受影响的版本与提交（如可确定）
- Reproduction steps or a proof of concept / 复现步骤或概念验证
- Your assessment of the impact / 你评估的影响范围

## Areas of special interest / 重点关注范围

Nano is a resident agent that operates the desktop, executes commands, and
manages its own permissions. The following classes of issues are especially
welcome:
/ Nano 是一个可以操作桌面、执行命令、管理自身权限的常驻智能体。
以下类别的问题尤其欢迎报告：

- Bypassing permission switches or risk grading / 权限开关或风险分级被绕过
- Bypassing the sensitive-path policy (credentials, private keys, browser data) / 敏感路径策略（凭据、私钥、浏览器数据）被绕过
- The command classifier missing high-risk commands / 命令分类器漏判高危命令
- Prompt injection leading to unauthorized actions / 模型提示词注入导致越权操作
- Unintended exfiltration of local data (memory, knowledge base, audit logs) / 本地数据（记忆、知识库、审计日志）的非预期外泄

## Response commitment / 响应承诺

The maintainer will review and respond to every private report as promptly as
possible; fix timelines depend on severity and complexity, and no specific
deadlines are promised.
/ 维护者会尽力及时查看并回应每一份私密报告；修复时长取决于问题的严重程度
与复杂度，恕不承诺具体时限。
