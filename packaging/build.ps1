# Builds the offline onedir bundle with PyInstaller.
#
#   pwsh packaging/build.ps1              # build into dist/offline-doc-search/
#   pwsh packaging/build.ps1 -Clean         # remove build/ and dist/ first
#   pwsh packaging/build.ps1 -WithLlm       # also pip install requirements-llm.txt
#
# Run on an online build machine that has the runtime deps installed
# (pip install -r requirements.txt -r requirements-build.txt). The resulting
# dist/offline-doc-search/ folder is self-contained and needs no network.

param(
    [switch]$Clean,
    [switch]$WithLlm
)

$ErrorActionPreference = "Stop"

# Repo root = parent of this script's folder.
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $PSScriptRoot "offline-doc-search.spec"
$DistDir = Join-Path $Root "dist\offline-doc-search"

Push-Location $Root
try {
    if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
        Write-Error "pyinstaller not found. Run: pip install -r requirements-build.txt"
    }

    if ($Clean) {
        Write-Host "Cleaning build/ and dist/ ..."
        Remove-Item -Recurse -Force (Join-Path $Root "build"), (Join-Path $Root "dist") -ErrorAction SilentlyContinue
    }

    if ($WithLlm) {
        Write-Host "Installing optional Ask-mode deps (requirements-llm.txt) ..."
        python -m pip install -r (Join-Path $Root "requirements-llm.txt")
        if ($LASTEXITCODE -ne 0) { Write-Error "pip install requirements-llm.txt failed (exit $LASTEXITCODE)." }
    }

    Write-Host "Building offline bundle (PyInstaller onedir) ..."
    pyinstaller --noconfirm $Spec
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed (exit $LASTEXITCODE)." }

    Write-Host ""
    Write-Host "Build complete:" -ForegroundColor Green
    Write-Host "  $DistDir"
    Write-Host "  Launch:  $(Join-Path $DistDir 'offline-doc-search.exe')"
    Write-Host ""
    Write-Host "Optional beside the .exe:"
    Write-Host '  bin/     - tesseract + ghostscript for OCR without a system install'
    Write-Host '  models/  - a *.gguf instruct model for Ask mode (see PACKAGING.md)'
    if (-not $WithLlm) {
        Write-Host ""
        Write-Host "Ask mode runtime was not bundled. Rebuild with -WithLlm to include"
        Write-Host "llama-cpp-python, then drop a GGUF model into models/."
    }
}
finally {
    Pop-Location
}
