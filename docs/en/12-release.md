# 12 · Releases and versioning

**What this page covers**: the full flow for releasing a new version (vX.YZ) — how version numbers work, which places in the repository carry the version, and the format rules for the Changelog and Release notes.
**After reading it you can**: run a complete release, from a code change to a published GitHub Release.
**Prerequisites**: [11-contributing.md](11-contributing.md), [10-testing.md](10-testing.md).

> Language: [中文](../zh/12-release.md) · English

---

## Version numbering

- Two-segment increment `vMAJOR.MINOR` (v1.96 → v1.97); the minor step is 0.01.
- **The integer segment is reserved for era changes** (e.g. jumping to v2.0 when proactive intelligence officially ships, or the first stable post-open-source release). Do not jump early just because the number grows fast.
- No three-segment patch numbers (v1.96.1 and the like) — stay consistent with the shape of all existing history entries.
- The version appears in **only the following places**; update each one on release:

| Location | Notes |
|---|---|
| Append a `## vX.YZ` section at the end of `Changelog.txt` | format below |
| The UI greeting `// nano-lumen vX.YZ` in `app.py` | search for `nano-lumen v` |
| The "manual matches Nano version" header of `data/knowledge/_system/nano_manual.md` | |
| `**Nano-Lumen vX.YZ**` under the titles of `README.md` / `README.en.md` | |
| The "as of version X" proactive-mode note in `nano_manual.md` | conditional: must change when the shadow status changes; otherwise bump it in passing |

- Version numbers inside docs 08/11 and test comments are **examples or history**; never update them.

## Changelog rules

- New version sections are **appended at the end of the file** (the whole file runs in chronological order).
- Within a section use `### Fixed` / `### Improved` / `### Added` (in the Chinese source: `### 修复` / `### 优化` / `### 新增`).
- Separate version sections with `---`, with blank lines around it.
- **Only user-facing changes** (behavior, fixes, features) go in. Repository housekeeping (screenshots, docs restructuring) does not — the Changelog is the single authoritative ledger, and Release notes are its storefront; the storefront never shows anything that is not in the ledger.

## Release flow

1. Before releasing, `bash run_tests.sh` passes in full.
2. Update every version location listed above. Stage commits by **naming files explicitly (`git add <file>`)**.
   🔴 Never use `git add -A`: runtime files with local absolute paths can appear in `data/` at any time (`indexed_hashes.json` / `parse_reports.json` both slipped in this way).
   `data/` is whitelist-based: `data/*.json` is ignored by default, with only `china_regions_city.json`, `model_config.json`, and the `_system/` manual allowed.
3. After committing and pushing, create the Release:

```
gh release create vX.YZ --target main --title "Nano-Lumen vX.YZ" --notes-file <notes file>
```

4. Release notes rules:
   - **Mirror only the matching Changelog section**, in Chinese and English.
   - The title is exactly `Nano-Lumen vX.YZ`; no ordinal decorations like "Nth fix release".
   - Nothing that is not in the Changelog (new docs, screenshots, and other repository changes do not go into release notes).
   - The last two lines are always:

```
- 安装方式见 [README](https://github.com/Fhaxikii/Nano-Lumen#安装) · Install: see the [English README](https://github.com/Fhaxikii/Nano-Lumen/blob/main/README.en.md#installation--quick-start)
- 完整变更历史见 [Changelog.txt](Changelog.txt) · Full changelog: [Changelog.txt](Changelog.txt)
```

## A tag is a freeze

The commit a tag points to is the snapshot from the moment of release; later fixes on main **never** flow into a published version. Two consequences:

- Keep fixing on main after a release; but browsing the tag or downloading its Source code always shows the tree as it was at release time.
- **When a published artifact turns out to be broken**: if the release is fresh and certainly has no downloads, you may delete the Release and tag, re-tag the fixed main, and recreate the Release. Once users may have downloaded it → leave the old tag alone and publish the fix under a new version number.

## How to verify you got it right

1. `git ls-files data/` lists only version-distributed files, no runtime files.
2. Searching the whole repo for the previous version number hits only history and example texts.
3. The Release page shows the notes, and the Source code archive contains no `.env` and no runtime files.
4. A fresh clone completes first launch following [01-getting-started.md](01-getting-started.md).

---

← Back to [README](README.md)
