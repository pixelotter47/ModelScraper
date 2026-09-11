param(
    [switch]$RuntimeOnly,
    [ValidateSet('3.11', '3.12', '3.13', '3.14')]
    [string]$PythonVersion = '3.14'
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    py "-$PythonVersion" -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15), 'Use Python 3.11 through 3.14'"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($RuntimeOnly) {
    & $Python -m pip install --disable-pip-version-check -e $ProjectRoot
} else {
    & $Python -m pip install --disable-pip-version-check -e "${ProjectRoot}[dev]"
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m pip check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -c "import ctb_core, ctb_api, ctb_verifier, modelscraper_service, gui_app, web_app"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "ModelScraper environment is ready."
