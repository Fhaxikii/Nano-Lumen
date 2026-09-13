# Nano Developer Documentation

**What this page covers**: the structure of this documentation set and which page to start from.  
**After reading it you can**: tell which pages are relevant to your goal and skip the rest.  
**Prerequisites**: none.

> Language: 中文 → see [zh/README.md](../zh/README.md) · English

---

## Who this documentation is for

Developers who want to read, modify, or submit changes to Nano. It does not
explain how to use the product; it explains how the code is organized, where
a change belongs, and how to verify that a change is correct.

## Pick your entry point by goal

| You want to | Read |
|---|---|
| Get the project running | [01-getting-started.md](01-getting-started.md) |
| Understand the structure before deciding where to change | [02-architecture.md](02-architecture.md) |
| Modify config items or add configuration | [03-configuration.md](03-configuration.md) |
| Add a tool (skill) | [04-writing-a-skill.md](04-writing-a-skill.md) |
| Connect an MCP server | [05-mcp-servers.md](05-mcp-servers.md) |
| Modify desktop automation, permissions, or risk decisions | [06-os-automation.md](06-os-automation.md) |
| Modify context management or memory | [07-memory-and-context.md](07-memory-and-context.md) |
| Modify the knowledge base and retrieval | [08-knowledge-base.md](08-knowledge-base.md) |
| Modify the UI or add a panel | [09-ui.md](09-ui.md) |
| Run or add tests | [10-testing.md](10-testing.md) |
| Before submitting a PR | [11-contributing.md](11-contributing.md) |
| Understand usage responsibilities and disclaimers | [user-agreement.md](user-agreement.md) |

New features usually start from `04` and `05`. Both are "add one thing"
changes that do not touch the core — the lowest-risk way to contribute.

## Suggested reading order

On first contact, read `01` → `02` → the page for your goal.
[02-architecture.md](02-architecture.md) establishes the terms and structure
every later page relies on; skipping it means every later page has to
re-explain its context.

## What this documentation does not contain

- A user manual for the product.
- Historical decision records. "Why it was decided back then" belongs to
  internal material and is not shipped. These pages answer "how things work
  now" — a different kind of question.
- A changelog. See [[Changelog.txt](../../Changelog.txt)](../../Changelog.txt) at the repository root.

## Language

The Chinese version is the original. Other language versions are
translations of it:

- [中文](../zh/README.md)（original）
- The bilingual glossary: [GLOSSARY](../GLOSSARY.md)
