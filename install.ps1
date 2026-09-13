# Nano-Lumen one-shot full installer (invoked by install.bat)
# Goal: SUCCESS at the end means truly complete. Python package versions are
# pinned one-by-one to the dev environment (requirements_cpu.txt comes from
# `pip freeze`), and everything pip cannot cover (Node / WebView2 / Tesseract /
# RAG models) is installed here too. A final verification gate refuses to
# print SUCCESS if any single item is missing.
$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Hr($t){ Write-Host ""; Write-Host ("[" + $t + "]") -ForegroundColor Yellow }
function Refresh-Path {
    $m = [System.Environment]::GetEnvironmentVariable("Path","Machine")
    $u = [System.Environment]::GetEnvironmentVariable("Path","User")
    $env:Path = "$m;$u"
}
$HasWinget = $false
try { winget --version *> $null; if ($LASTEXITCODE -eq 0) { $HasWinget = $true } } catch {}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Nano-Lumen full installation" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# -- 1. Python 3.10 -------------------------------------------------------
Hr "1/6 Python 3.10"
$pythonOk = $false
try { $ver = & python --version 2>&1; if ($ver -match "Python 3\.10\.") { Write-Host " OK: $ver" -ForegroundColor Green; $pythonOk = $true } } catch {}
if (-not $pythonOk) {
    # The dependency list was frozen on Python 3.10 (hard pins like torch+cpu /
    # numpy are sensitive to the minor version). Only 3.10 is accepted:
    # on 3.11/3.12 some cp310-only wheels fail to install, which produces a
    # half-working environment.
    Write-Host " Python 3.10 not found; downloading 3.10.11..." -ForegroundColor Yellow
    $pyUrl = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"
    $pyExe = "$env:TEMP\python-3.10.11-amd64.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyExe -UseBasicParsing
        Start-Process -FilePath $pyExe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
        Remove-Item $pyExe -Force -ErrorAction SilentlyContinue
        Refresh-Path
        Write-Host " Python installed." -ForegroundColor Green
    } catch {
        Write-Host " Python download failed. Install Python 3.10 manually (check 'Add to PATH') and re-run." -ForegroundColor Red
        Read-Host "Press Enter to exit"; exit 1
    }
}


# -- 2. pip dependencies (exact versions, CPU wheel source for torch) -----
Hr "2/6 pip dependencies (exact versions, 10-20 min)"
& python -m pip install --upgrade pip *> $null
$mirrors = @(
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    ""
)
$pipOk = $false
foreach ($m in $mirrors) {
    if ($m -eq "") {
        Write-Host " Trying official PyPI..." -ForegroundColor Yellow
        & pip install -r requirements_cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu
    } else {
        Write-Host " Trying mirror: $m" -ForegroundColor Yellow
        $host_ = ($m -replace "https://(.+?)/.*", '$1')
        & pip install -r requirements_cpu.txt -i $m --trusted-host $host_ --extra-index-url https://download.pytorch.org/whl/cpu
    }
    if ($LASTEXITCODE -eq 0) { $pipOk = $true; break }
    Write-Host " This source failed; trying the next one..." -ForegroundColor Yellow
}
if ($pipOk) { Write-Host " pip dependencies installed." -ForegroundColor Green }
else { Write-Host " pip dependency installation failed (all sources)." -ForegroundColor Red }

# -- 3. RAG models (BGE-M3 + reranker, offline cache) ----------------------
Hr "3/6 RAG models (~3GB)"
$ragPy = @'
import os, shutil
def dl(repo, pats):
    os.environ['MODELSCOPE_ENDPOINT'] = 'https://mirrors.aliyun.com/modelscope/'
    os.environ['MODELSCOPE_DOWNLOAD_PARALLELS'] = '16'
    from modelscope.hub.snapshot_download import snapshot_download
    md = snapshot_download(repo, allow_patterns=pats)
    hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    cdir = os.path.join(hub, "models--" + repo.replace("/", "--"))
    snap = os.path.join(cdir, "snapshots", "main"); refs = os.path.join(cdir, "refs")
    if os.path.exists(cdir): shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(snap, exist_ok=True); os.makedirs(refs, exist_ok=True)
    for it in os.listdir(md):
        s = os.path.join(md, it); d = os.path.join(snap, it)
        (shutil.copy2 if os.path.isfile(s) else shutil.copytree)(s, d)
    open(os.path.join(refs, "main"), "w").write("main")
    print("[OK]", repo)
P = ["*.json","*.txt","*.bin","*.safetensors","*.model","tokenizer*","sentence*"]
dl("BAAI/bge-m3", P); dl("BAAI/bge-reranker-v2-m3", P)
'@
$ragPy | Out-File "$env:TEMP\nano_dl_rag.py" -Encoding utf8
& python "$env:TEMP\nano_dl_rag.py"
$ragOk = ($LASTEXITCODE -eq 0)
Remove-Item "$env:TEMP\nano_dl_rag.py" -Force -ErrorAction SilentlyContinue
if ($ragOk) {
    [System.Environment]::SetEnvironmentVariable("HF_ENDPOINT","https://hf-mirror.com","User")
    Write-Host " RAG models ready." -ForegroundColor Green
} else { Write-Host " RAG model download failed (knowledge-base retrieval would be dead; this must be fixed -- no half-working installs)." -ForegroundColor Red }

# -- 4. Node.js LTS (Playwright MCP needs npx) -----------------------------
Hr "4/6 Node.js (LTS)"
Refresh-Path
$nodeOk = $false
try { & node --version *> $null; if ($LASTEXITCODE -eq 0) { $nodeOk = $true } } catch {}
if ($nodeOk) { Write-Host " OK: $(& node --version)" -ForegroundColor Green }
else {
    if ($HasWinget) {
        Write-Host " Installing Node LTS via winget..." -ForegroundColor Yellow
        winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
    }
    Refresh-Path
    try { & node --version *> $null; $nodeOk = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $nodeOk) {
        Write-Host " winget unavailable or failed; downloading the Node MSI directly..." -ForegroundColor Yellow
        $nodeMsi = "$env:TEMP\node-lts.msi"
        try {
            Invoke-WebRequest -Uri "https://nodejs.org/dist/v20.18.1/node-v20.18.1-x64.msi" -OutFile $nodeMsi -UseBasicParsing
            Start-Process msiexec.exe -ArgumentList "/i `"$nodeMsi`" /quiet /norestart" -Wait
            Remove-Item $nodeMsi -Force -ErrorAction SilentlyContinue
            Refresh-Path
            try { & node --version *> $null; $nodeOk = ($LASTEXITCODE -eq 0) } catch {}
        } catch {}
    }
    if ($nodeOk) { Write-Host " Node installed: $(& node --version)" -ForegroundColor Green }
    else { Write-Host " Node installation failed (Playwright MCP will not connect)." -ForegroundColor Red }
}

# -- 4.1 npm global dir + Playwright MCP server ----------------------------
# The npm global directory (%APPDATA%\npm) is not always created automatically
# by the Node installer on some Windows setups; without it npx / npm -g break
# outright (verified: npx only worked after a manual mkdir). Create it first.
# No separate browser download needed: the playwright entry in
# mcp_servers.json is configured with --browser msedge, which uses the Edge
# that ships with every Windows install -- no dependency on Google Chrome and
# no ~150MB Chromium download.
$pwMcpOk = $false
if ($nodeOk) {
    $npmDir = Join-Path $env:APPDATA "npm"
    if (-not (Test-Path $npmDir)) {
        New-Item -ItemType Directory -Force -Path $npmDir | Out-Null
        Write-Host " Created npm global directory: $npmDir" -ForegroundColor Green
    }
    # Install Playwright MCP globally so it persists, instead of a throwaway
    # npx download on every run (slow + flaky behind networks).
    Write-Host " Installing Playwright MCP Server (@playwright/mcp)..." -ForegroundColor Yellow
    & npm install -g "@playwright/mcp@latest" 2>&1 | Out-Null
    & npm list -g "@playwright/mcp" *> $null
    if ($LASTEXITCODE -eq 0) { $pwMcpOk = $true; Write-Host " Playwright MCP Server installed (uses the system Edge as its browser)." -ForegroundColor Green }
    else { Write-Host " Playwright MCP Server installation failed (web automation will be unavailable)." -ForegroundColor Red }
}

# -- 5. Tesseract OCR (+ Chinese/English language packs) -------------------
Hr "5/6 Tesseract OCR"
$tessDir = "C:\Program Files\Tesseract-OCR"
$tessOk = $false
if (Test-Path "$tessDir\tesseract.exe") { $tessOk = $true }
if (-not $tessOk) {
    if ($HasWinget) {
        Write-Host " Installing Tesseract via winget..." -ForegroundColor Yellow
        winget install -e --id UB-Mannheim.TesseractOCR --silent --accept-package-agreements --accept-source-agreements
    }
    if (-not (Test-Path "$tessDir\tesseract.exe")) {
        Write-Host " winget did not install it; falling back to direct download..." -ForegroundColor Yellow
        # Multiple sources, tried in order. There used to be a single
        # university mirror that was unreachable from some networks.
        # Anything an installer downloads from the web is a potential failure
        # point; the download itself cannot be removed here, but "only one
        # path" can. First source is the official GitHub release (upstream),
        # second is the original mirror.
        $tessExe = "$env:TEMP\tesseract-setup.exe"
        $tessUrls = @(
            "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe",
            "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.3.20231005.exe"
        )
        foreach ($u in $tessUrls) {
            if (Test-Path "$tessDir\tesseract.exe") { break }
            try {
                Write-Host "  Trying: $u" -ForegroundColor DarkGray
                Remove-Item $tessExe -Force -ErrorAction SilentlyContinue
                Invoke-WebRequest -Uri $u -OutFile $tessExe -UseBasicParsing
                # The size must be verified: a soft 404 returns a few-KB HTML
                # page, and silently running that through Start-Process installs
                # nothing -- it only surfaces later at the verification gate as
                # "Tesseract missing", with the cause nowhere to be seen.
                $sz = (Get-Item $tessExe -ErrorAction SilentlyContinue).Length
                if ($sz -lt 10MB) {
                    Write-Host "  That was not the installer ($([math]::Round($sz/1MB,2)) MB); trying the next source." -ForegroundColor Red
                    continue
                }
                Start-Process $tessExe -ArgumentList "/S" -Wait
            } catch {
                Write-Host "  This source failed; trying the next one." -ForegroundColor Red
            }
        }
        Remove-Item $tessExe -Force -ErrorAction SilentlyContinue
    }
    $tessOk = (Test-Path "$tessDir\tesseract.exe")
}
if ($tessOk) {
    # Add to the user PATH (pytesseract needs to find the binary)
    $uPath = [System.Environment]::GetEnvironmentVariable("Path","User")
    if ($uPath -notlike "*$tessDir*") {
        [System.Environment]::SetEnvironmentVariable("Path", "$uPath;$tessDir", "User")
    }
    $env:Path = "$env:Path;$tessDir"
    # The chi_sim language pack (winget's default may ship eng only; without
    # chi_sim Chinese text cannot be recognized)
    $tessdata = "$tessDir\tessdata"
    foreach ($lng in @("chi_sim","eng")) {
        if (-not (Test-Path "$tessdata\$lng.traineddata")) {
            try {
                Invoke-WebRequest -Uri "https://github.com/tesseract-ocr/tessdata/raw/main/$lng.traineddata" `
                    -OutFile "$tessdata\$lng.traineddata" -UseBasicParsing
            } catch { Write-Host "  (failed to download the $lng language pack)" -ForegroundColor Red }
        }
    }
    Write-Host " Tesseract ready (chi_sim/eng included)." -ForegroundColor Green
} else { Write-Host " Tesseract installation failed (the OCR-based screen-reading layer will not work)." -ForegroundColor Red }

# -- 6. WebView2 Runtime (native window host) ------------------------------
Hr "6/6 WebView2 Runtime"
# pywebview's bundled WebView2 SDK calls ICoreWebView2Environment10 when
# creating the window; runtimes below 1.0.1661.34 lack that interface and
# fail at startup with InvalidCastException (E_NOINTERFACE). The check
# must verify the version, not just existence.
$wv2MinVersion = [version]"1.0.1661.34"
function Test-WebView2([switch]$Quiet) {
    $g = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$g",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$g",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$g"
    )
    foreach ($k in $keys) {
        try {
            $v = (Get-ItemProperty $k -ErrorAction Stop).pv
            if ($v -and $v -ne "0.0.0.0") {
                if ([version]$v -ge $wv2MinVersion) { return $true }
                else { if (-not $Quiet) { Write-Host " WebView2 Runtime $v found, but $wv2MinVersion+ is required (pywebview needs newer interfaces); upgrading..." -ForegroundColor Yellow }; return $false }
            }
        } catch {}
    }
    return $false
}
$wv2Ok = Test-WebView2
if ($wv2Ok) { Write-Host " OK: already installed" -ForegroundColor Green }
else {
    if ($HasWinget) {
        Write-Host " Installing WebView2 Runtime via winget..." -ForegroundColor Yellow
        winget install -e --id Microsoft.EdgeWebView2Runtime --silent --accept-package-agreements --accept-source-agreements
    }
    if (-not (Test-WebView2)) {
        Write-Host " winget failed; downloading the Evergreen Bootstrapper..." -ForegroundColor Yellow
        $wv = "$env:TEMP\MicrosoftEdgeWebview2Setup.exe"
        try {
            Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $wv -UseBasicParsing
            Start-Process $wv -ArgumentList "/silent /install" -Wait
            Remove-Item $wv -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    $wv2Ok = Test-WebView2
    if ($wv2Ok) { Write-Host " WebView2 installed." -ForegroundColor Green }
    else { Write-Host " WebView2 installation failed (the native window will fall back to browser mode)." -ForegroundColor Red }
}

# -- Verification gate: SUCCESS only if everything passed ------------------
Hr "Verification"
Refresh-Path
$env:Path = "$env:Path;$tessDir"
$fail = @()

# Key Python packages (imported one by one; any miss is recorded)
$verifyPy = @'
import importlib, sys
mods = ['nicegui','webview','clr','mcp','torch','chromadb','transformers',
        'sentence_transformers','pytesseract','mss','pyautogui','keyboard',
        'pygetwindow','anthropic','modelscope','PIL','psutil','watchdog','dotenv']
# Note: torch / transformers / clr are not listed one by one -- they are
#     pulled in indirectly by sentence_transformers / uiautomation; if those
#     installs are broken, the packages above fail first.
# 'paddle' has been removed: the three paddle packages were dropped from
#     requirements_cpu.txt. Keeping it here would make a clean machine that
#     installed exactly per requirements fail its own verification gate
#     (and importing paddle emits a ccache warning that triggered that false
#     verdict). The dependency list and the verification list are two copies
#     of the same thing: change only one, and the other starts lying.
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        bad.append(m)
print('MISSING:' + ','.join(bad) if bad else 'PYOK')
sys.exit(1 if bad else 0)
'@
$verifyPy | Out-File "$env:TEMP\nano_verify.py" -Encoding utf8
$pyVerify = & python "$env:TEMP\nano_verify.py" 2>&1
$pyRc = $LASTEXITCODE
Remove-Item "$env:TEMP\nano_verify.py" -Force -ErrorAction SilentlyContinue
# The verdict uses the EXIT CODE (the verify script already does
# `sys.exit(1 if bad else 0)`), not text matching. $pyVerify is an ARRAY
# (multiple lines): PowerShell's -notmatch on an array returns the
# non-matching elements, not a boolean -- so any stray line besides PYOK
# (a single UserWarning from some package is enough) made the old
# text-based check report "incomplete install" on a perfectly fine machine.
# Output text is display-only, joined into one line to avoid flooding.
$pyMsg = ($pyVerify -join ' ').Trim()
if ($pyRc -ne 0 -or $pyMsg -notlike "*PYOK*") {
    $miss = if ($pyMsg -match 'MISSING:([^\s]*)') { $matches[1] } else { $pyMsg }
    $fail += "Python packages (missing: $miss)"
}

try { & node --version *> $null; if ($LASTEXITCODE -ne 0) { $fail += "Node" } } catch { $fail += "Node" }
& npm list -g "@playwright/mcp" *> $null
if ($LASTEXITCODE -ne 0) { $fail += "PlaywrightMCP" }
# Edge may live at a custom location (another drive), so a hardcoded path
# is not enough. Playwright's msedge channel resolves via the App Paths
# registry key; use the same criterion here.
function Test-EdgeBrowser {
    try {
        $ap = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" -ErrorAction Stop).'(default)'
        if ($ap -and (Test-Path $ap)) { return $true }
    } catch {}
    foreach ($p in @(
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
    )) { if (Test-Path $p) { return $true } }
    return $false
}
if (-not (Test-EdgeBrowser)) { $fail += "Edge browser (required by Playwright)" }
if (-not (Test-Path "$tessDir\tesseract.exe")) { $fail += "Tesseract" }
if (-not (Test-Path "$tessDir\tessdata\chi_sim.traineddata")) { $fail += "Tesseract chi_sim language pack" }
if (-not (Test-WebView2 -Quiet)) { $fail += "WebView2" }
$hub = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
if (-not (Test-Path (Join-Path $hub "models--BAAI--bge-m3\snapshots\main"))) { $fail += "RAG: bge-m3" }
if (-not (Test-Path (Join-Path $hub "models--BAAI--bge-reranker-v2-m3\snapshots\main"))) { $fail += "RAG: reranker" }

Write-Host ""
if ($fail.Count -eq 0) {
    Write-Host "========================================" -ForegroundColor Green
    Write-Host " SUCCESS -- all dependencies complete. Start with start.bat" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
} else {
    Write-Host "========================================" -ForegroundColor Red
    Write-Host " Incomplete installation. Missing:" -ForegroundColor Red
    foreach ($f in $fail) { Write-Host "   - $f" -ForegroundColor Red }
    Write-Host " Fix the items above and re-run install.bat (do not start with a broken setup)." -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
}
Read-Host "Press Enter to exit"
