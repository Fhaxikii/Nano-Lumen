# Nano-Lumen Packaging Guide

> **v1.0** · 2026-09-20
> Internal document. Not distributed. Only pushed to https://github.com/Fhaxikii/nano-dev-archive

---

## 1. What this produces

onedir portable package: unzip → double-click exe → runs. No Python required.

- ~1.35 GB (includes Node + Tesseract; no ML models)
- First launch downloads ~4.5 GB RAG models automatically (huggingface → modelscope fallback)
- console=True (beta; black window doubles as log viewer)

## 2. Prerequisites (build machine)

| Item | Requirement |
|---|---|
| OS | Windows 10+ |
| Python | 3.10.x at `C:\Program Files\Python310\` |
| Deps | `pip install -r requirements_cpu.txt` |
| PyInstaller | `pip install pyinstaller` |
| Tesseract | Installed at `C:\Program Files\Tesseract-OCR` (with `tessdata\chi_sim.traineddata`) |
| Node | Already in `tools\node\node.exe` (v20.18.1 portable) |

## 3. Quick build

```powershell
cd <repo root>
powershell -File packaging\build.ps1
```

The script self-checks expected files, builds, post-copies, and verifies.

## 4. Path resolution rules (layout basis)

| Resolution | Lands at | Files |
|---|---|---|
| `Path(__file__).parent.parent` | `_internal/` | data/model_config.json, config/mcp_servers.json, config/os_config.json, assets/, static/, skills/official/ |
| Bare relative (CWD) | next to exe | config/system_instruction.txt, config/persona.txt, data/china_regions_city.json, data/knowledge/, .env, skills/ (watcher) |

## 5. Source file inventory

### Bundled by PyInstaller (nano.spec datas)
- `app.py`, `nano_koala.py`, `core/**`, `memory/manager.py`
- `assets/`, `static/`
- `config/mcp_servers.json`, `config/os_config.json` → `_internal/config/`
- `skills/` → `_internal/skills/`
- `data/model_config.json`, `data/china_regions_city.json` → `_internal/data/`

### Copied by build.ps1 (outer, CWD-relative)
- `config/system_instruction.txt`, `config/persona.txt` → `config/`
- `data/china_regions_city.json` → `data/`
- `data/knowledge/_system/nano_manual.md` → both `data/knowledge/_system/` and `_internal/data/knowledge/_system/`
- `skills/official/` → outer `skills/official/` (for watcher)
- `tools/node/` → outer `node/`
- `C:\Program Files\Tesseract-OCR\` → outer `tesseract/`

### Not packaged
- `docs/`, `skill_template/`, `tests/`, `.github/`
- `_setup_rag_models.py` (deleted; logic merged into `core/rag.py::_resolve_hf_snapshot`)

## 6. Manual files to copy

These are copied automatically by build.ps1:

| File | Source | Destination |
|---|---|---|
| LICENSE | repo root | package root |
| NOTICE | repo root | package root |
| THIRD_PARTY_LICENSES.txt | repo root | package root |
| Changelog.txt | repo root | package root |

If you add new legal/notices files, add them to the copy list in build.ps1 AND this table.

## 7. Model download

- Not bundled. First launch downloads ~4.5 GB via huggingface_hub.
- If huggingface.co fails, `core/rag.py::_resolve_hf_snapshot` automatically falls back to Aliyun ModelScope, converts .bin → safetensors, lays out as HF cache.
- `HF_ENDPOINT` is not hardcoded; users can set their own.

## 8. Known pitfalls

1. **console=False crashes on GBK consoles** — spec must use `console=True`.
2. **chromadb submodules** — spec needs `collect_all("chromadb")` + hiddenimports for posthog/rust.
3. **Tesseract** — rag.py auto-detects `exe_dir/tesseract/tesseract.exe`.
4. **Node** — mcp_client.py auto-adds `exe_dir/node/` to PATH.

## 9. Release steps

1. Run `packaging\build.ps1`
2. Verify: double-click `first-launch-test.bat`, confirm UI opens, RAG model downloads
3. 7z-compress `dist\Nano-Lumen\`
4. Tag and push release
