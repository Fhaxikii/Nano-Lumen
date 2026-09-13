# Nano Developer Documentation

**What this page covers**: the languages this documentation ships in, and
shared assets of the docs tree.
**After reading it you can**: pick your language and find every page in it.
**Prerequisites**: none.

---

## Languages

The Chinese tree is the original. Other languages are translations of it:

- **中文** — [zh/README.md](zh/README.md)（原始版本 / original）
- **English** — [en/README.md](en/README.md)（translated from the Chinese
  original）

Each page carries a language switch line at the top linking to its
counterparts in the other trees.

## Shared assets

| File | Purpose |
|---|---|
| [GLOSSARY.md](GLOSSARY.md) | Bilingual term anchors (zh ↔ en) plus the translation rules. Written in English; adding a third language means adding a column, nothing else |

## Conventions of this tree

- `docs/` root holds only language-neutral assets, written in English.
- Each language lives in its own directory (`zh/`, `en/`) with a complete,
  same-named set of pages.
- Program output quoted in the docs is never translated. See the translation
  rules in [GLOSSARY.md](GLOSSARY.md).
