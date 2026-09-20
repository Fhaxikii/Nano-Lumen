# build.ps1 - Nano-Lumen onedir packaging script
# Run from repo root:  powershell -File packaging\build.ps1
# Self-checks source layout before building; refuses to proceed if layout changed.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "=== Nano-Lumen packaging ===" -ForegroundColor Cyan

# ── 1. Self-check: expected source files ──────────────────────────
$expected = @(
    "app.py",
    "nano_koala.py",
    "nano.spec",
    "core\rag.py",
    "core\registry.py",
    "core\mcp_client.py",
    "config\system_instruction.txt",
    "config\persona.txt",
    "config\mcp_servers.json",
    "config\os_config.json",
    "data\model_config.json",
    "data\china_regions_city.json",
    "data\knowledge\_system\nano_manual.md",
    "skills\official\SearchTheWeb.py",
    "assets\nano_icon_preview.png",
    "static",
    "tools\node\node.exe",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_LICENSES.txt",
    "Changelog.txt"
)

$missing = @()
foreach ($f in $expected) {
    if (-not (Test-Path (Join-Path $root $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Host "SELF-CHECK FAILED: expected files missing:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  MISSING: $_" -ForegroundColor Red }
    Write-Host "Update packaging\PACKAGING.md and this script before building." -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] self-check passed: all expected files present" -ForegroundColor Green

# ── 2. Kill running instance ──────────────────────────────────────
Get-Process "Nano-Lumen" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 1

# ── 3. Clean previous build ───────────────────────────────────────
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue

# ── 4. PyInstaller ────────────────────────────────────────────────
Write-Host "Building with PyInstaller..." -ForegroundColor Cyan
& "C:\Program Files\Python310\python.exe" -m PyInstaller --noconfirm --clean nano.spec
if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller failed" -ForegroundColor Red; exit 1 }

# ── 5. Post-build: outer files ────────────────────────────────────
$d = "dist\Nano-Lumen"
Write-Host "Post-build: copying outer files..." -ForegroundColor Cyan

New-Item -ItemType Directory -Force "$d\config", "$d\data\knowledge\_system", "$d\_internal\data\knowledge\_system", "$d\hf_empty" | Out-Null

Copy-Item "config\system_instruction.txt" "$d\config\"
Copy-Item "config\persona.txt" "$d\config\"
Copy-Item "data\china_regions_city.json" "$d\data\"
Copy-Item "data\knowledge\_system\nano_manual.md" "$d\data\knowledge\_system\"
Copy-Item "data\knowledge\_system\nano_manual.md" "$d\_internal\data\knowledge\_system\"
Copy-Item -Recurse -Force "skills\official" "$d\skills\official"
Copy-Item -Recurse -Force "tools\node" "$d\node"
Copy-Item -Recurse -Force "C:\Program Files\Tesseract-OCR" "$d\tesseract"

# Manual files (see PACKAGING.md section "Manual files to copy")
Copy-Item "LICENSE" "$d\"
Copy-Item "NOTICE" "$d\"
Copy-Item "THIRD_PARTY_LICENSES.txt" "$d\"
Copy-Item "Changelog.txt" "$d\"

# First-launch test bat
$bat = @"
@echo off
chcp 65001 >nul
set HF_HOME=%~dp0hf_empty
set HF_ENDPOINT=https://hf-mirror.com
cd /d %~dp0
"%~dp0Nano-Lumen.exe"
"@
[System.IO.File]::WriteAllText("$d\first-launch-test.bat", $bat)

# ── 6. Verify ─────────────────────────────────────────────────────
Write-Host "Verifying..." -ForegroundColor Cyan
$checks = @(
    "$d\Nano-Lumen.exe",
    "$d\config\system_instruction.txt",
    "$d\config\persona.txt",
    "$d\data\china_regions_city.json",
    "$d\data\knowledge\_system\nano_manual.md",
    "$d\_internal\data\knowledge\_system\nano_manual.md",
    "$d\skills\official\SearchTheWeb.py",
    "$d\_internal\config\mcp_servers.json",
    "$d\_internal\config\os_config.json",
    "$d\_internal\data\model_config.json",
    "$d\node\node.exe",
    "$d\tesseract\tesseract.exe",
    "$d\tesseract\tessdata\chi_sim.traineddata",
    "$d\LICENSE",
    "$d\NOTICE",
    "$d\THIRD_PARTY_LICENSES.txt",
    "$d\Changelog.txt"
)
$fail = 0
foreach ($c in $checks) {
    if (-not (Test-Path $c)) { Write-Host "  FAIL: $c" -ForegroundColor Red; $fail++ }
}
if ($fail -gt 0) { Write-Host "VERIFICATION FAILED: $fail items missing" -ForegroundColor Red; exit 1 }

$size = [math]::Round((Get-ChildItem $d -Recurse -File | Measure-Object Length -Sum).Sum / 1GB, 2)
Write-Host "[ok] build complete: $d  ($size GB)" -ForegroundColor Green
Write-Host ""
Write-Host "Next:" -ForegroundColor Yellow
Write-Host "  1. Double-click first-launch-test.bat to verify empty-cache first boot"
Write-Host "  2. Check the build doesn't crash, RAG model downloads via modelscope"
Write-Host "  3. 7z compress dist\Nano-Lumen for release"
