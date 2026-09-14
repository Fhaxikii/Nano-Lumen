# Nano-Lumen User Agreement

**Last updated: 2026-09-14**

The core principle of this agreement can be summarized in one sentence: **Nano is software that genuinely operates your computer, and installing or using the software means that you understand and accept the foregoing.**

## 1. Acceptance of terms

Installing, copying, downloading, accessing, or otherwise using Nano-Lumen (the "software") means you have read, understood, and agreed to this agreement. If you disagree with any term, stop using and delete the software.

## 2. Nature of the software

The software is an open-source agent that resides on the Windows desktop with the following capabilities:

- Executing OS commands and desktop automation (including creating, modifying, moving, and deleting files)
- Reading and editing local files
- Calling third-party model APIs you configure yourself (which cost money)
- Loading and running local and third-party extensions (skills, MCP servers)

The software ships with layered safety mechanisms (risk grading, permission switches, a command classifier, sensitive-path policies), **but no safety mechanism can guarantee that mistakes, data loss, or unintended behavior never happen**. Safety mechanisms reduce risk; they do not eliminate it.

## 3. License

The software is released under the [Apache-2.0](../../LICENSE) license. This agreement supplements that license; where they conflict regarding software licensing, the license governs.

## 4. User responsibilities

By using the software you understand and agree that:

- **Back up data you cannot afford to lose.** The software can operate on files; despite confirmation, recycling, and audit mechanisms, you remain responsible for keeping backups of important data.
- **Model API costs are yours.** API keys are configured by you; the billing relationship exists between you and the model vendor. Charges arising from vendor-side billing anomalies, cache-miss behavior, or price changes are disputes between you and the vendor.
- **Read what you confirm.** High-risk operations show you what will be executed; clicking confirm means you authorize that action.
- **Use it only on devices and accounts you have the right to control.**
- Comply with applicable laws and regulations.

## 5. Prohibited uses

You may not use the software to:

- Violate any applicable law
- Access or operate other people's computers, accounts, or data without authorization
- Generate or spread illegal or harmful content
- Infringe intellectual property or impersonate others
- Circumvent the terms of any third-party service

## 6. Third-party services

Model services (e.g. Anthropic, DeepSeek) and connectable MCP servers are provided by third parties and governed by their own terms. The software is not affiliated with, nor endorsed by, these vendors.

## 7. Disclaimer of warranties

The software is provided "as is", without warranty of any kind, express or implied, including merchantability, fitness for a particular purpose, and non-infringement. **Models can generate wrong content, and automated actions can produce unintended results.**

## 8. Limitation of liability

To the maximum extent permitted by applicable law, the project maintainer is not liable for any direct or indirect loss arising from use of or inability to use the software — including but not limited to **data loss or file damage, model API charges (including charges caused by vendor-side billing anomalies), business interruption, or lost profits**. You assume all risk of using the software.

## 9. Privacy and data

The software collects and uploads no usage data. The only outbound traffic consists of model API requests you configure yourself. See the "Privacy & Trust" section of the repository README for details.

## 10. Modifications

This agreement may be revised with version updates; the latest text in the repository governs. Continued use after an update constitutes acceptance of the revised agreement.

## 11. Contact

Questions about this agreement: open a GitHub Issue in the repository, or email **ziyihukala@gmail.com**. Security issues: follow [SECURITY.md](../../SECURITY.md).

---

← Back to [README](README.md)
