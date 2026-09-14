# Nano-Lumen User Agreement

**Last updated: 2026-09-14**

Nano is software that genuinely operates your computer. By installing or using the software, you acknowledge and accept this nature and the risks that come with it.

## 1. Acceptance of Terms

By installing, copying, downloading, accessing, or otherwise using Nano-Lumen (the "software"), you acknowledge that you have read, understood, and agreed to all terms of this agreement. If you do not agree with any term of this agreement, please stop using and delete the software immediately.

## 2. Nature of the Software

The software is an open-source agent that resides on the Windows desktop with the following capabilities:

- Executing OS commands and desktop automation (including creating, modifying, moving, and deleting files)
- Reading and editing local files
- Calling third-party model APIs that you configure yourself (which may incur costs)
- Loading and running local and third-party extensions (skills, MCP servers)

The software ships with layered safety mechanisms (risk grading, permission switches, a command classifier, and sensitive-path policies), but **no safety mechanism can guarantee that mistakes, data loss, or unintended behavior will never occur**. Safety mechanisms reduce risk; they do not eliminate it.

## 3. License

The software is released under the [Apache-2.0](../../LICENSE) open-source license. This agreement supplements that license; in the event of a conflict regarding software licensing, the license governs.

## 4. Intellectual Property

The software code grants you usage rights under the terms of the Apache-2.0 license, and this agreement does not alter that grant. Apart from the rights granted under the license, this agreement grants you no other intellectual property rights. Third-party names, trademarks, and service marks mentioned in the software belong to their respective owners.

## 5. User Responsibilities

By using the software, you acknowledge and agree that:

- **You are responsible for backing up data you cannot afford to lose.** The software can operate on files; despite the confirmation, recycling, and audit mechanisms, you remain responsible for backing up important data.
- **Model API costs are your responsibility.** API keys are configured by you, and the billing relationship exists between you and the model vendor. Charges arising from vendor-side billing anomalies, cache-miss behavior, or price changes are disputes between you and the vendor.
- **Read the confirmation carefully before authorizing.** The software will show you the actions to be performed; clicking confirm constitutes your authorization of that action.
- **Use the software only on devices and accounts over which you have lawful rights.**
- Comply with applicable laws and regulations.

## 6. Prohibited Uses

You may not use the software for any of the following purposes:

- Committing any act that violates applicable law
- Accessing or operating another person's computer, accounts, or data without authorization
- Generating or spreading illegal or harmful content
- Infringing others' intellectual property or impersonating others
- Circumventing the terms of use of any third-party service

## 7. Third-Party Services

The model services (e.g., Anthropic, DeepSeek) and connectable MCP servers are provided by third parties, and their use is subject to their respective terms. The software has no affiliation with, nor endorsement from, the aforementioned vendors.

## 8. Disclaimer of Warranties

The software is provided "as is", without warranty of any kind, express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, and non-infringement. **Models may generate incorrect content, and automated actions may produce unintended results.**

## 9. Limitation of Liability

To the maximum extent permitted by applicable law, the project maintainer shall not be liable for any direct or indirect loss arising from the use of, or inability to use, the software — including but not limited to **data loss or file damage, model API charges (including charges caused by vendor-side billing anomalies), business interruption, or lost profits**. You assume all risk of using the software.

## 10. Privacy and Data

The software collects and uploads no usage data. The only outbound traffic consists of model API requests that you configure yourself. See the "Privacy & Trust" section of the repository README for details.

## 11. Modifications

This agreement may be revised with version updates; the latest text in the repository governs. Continued use of the software after an update constitutes acceptance of the revised agreement.

## 12. Governing Law and Dispute Resolution

The formation, validity, interpretation, and performance of this agreement shall be governed by the laws of the People's Republic of China. Any dispute arising from this agreement shall first be resolved through friendly negotiation; if negotiation fails, either party may bring the dispute to a competent court at the place of the maintainer's domicile.

## 13. Contact

For questions about this agreement, open a GitHub Issue in the repository, or email **ziyihukala@gmail.com**. For security issues, follow [SECURITY.md](../../SECURITY.md).
