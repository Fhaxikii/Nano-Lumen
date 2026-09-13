# 安全政策 / Security Policy

## 支持的版本 / Supported versions

本项目仅对**最新发布版本**与 `main` 分支提供安全修复。
/ Security fixes are only provided for the **latest release** and the `main` branch.

| 版本 / Version | 支持状态 / Supported |
|---|---|
| 最新 Release / latest release | ✅ |
| 旧版本 / older versions | ❌（请先升级 / please upgrade first） |

## 如何报告漏洞 / How to report a vulnerability

**请不要通过公开 Issue 报告安全漏洞。**
/ **Please do not report security vulnerabilities through public issues.**

请使用 GitHub 的 **Private vulnerability reporting**（仓库 Security 标签页 →
Report a vulnerability）私下报告。
/ Use GitHub's **Private vulnerability reporting** (Security tab → Report a
vulnerability) to report privately.

报告时请尽量包含 / When reporting, please include:

- 受影响的版本与提交（如可确定）/ affected version and commit, if determinable
- 复现步骤或概念验证 / reproduction steps or a proof of concept
- 你评估的影响范围 / your assessment of the impact

## 重点关注范围 / Areas of special interest

Nano 是一个可以操作桌面、执行命令、管理自身权限的常驻智能体。
以下类别的问题尤其欢迎报告 / Nano is a resident agent that operates the
desktop, executes commands, and manages its own permissions. The following
classes of issues are especially welcome:

- 权限开关或风险分级被绕过 / bypassing permission switches or risk grading
- 敏感路径策略（凭据、私钥、浏览器数据）被绕过 / bypassing the sensitive-path
  policy (credentials, private keys, browser data)
- 命令分类器漏判高危命令 / the command classifier missing high-risk commands
- 模型提示词注入导致越权操作 / prompt injection leading to unauthorized actions
- 本地数据（记忆、知识库、审计日志）的非预期外泄 / unintended exfiltration of
  local data (memory, knowledge base, audit logs)

## 响应承诺 / Response commitment

维护者会尽力及时查看并回应每一份私密报告；修复时长取决于问题的严重程度
与复杂度，恕不承诺具体时限。
/ The maintainer will review and respond to every private report as promptly
as possible; fix timelines depend on severity and complexity, and no specific
deadlines are promised.
