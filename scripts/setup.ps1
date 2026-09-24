$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

py -3.12 --version
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 was not found. Install 64-bit Python 3.12 and retry."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv" }
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install project requirements" }

Write-Output "Environment is ready. Run: .\.venv\Scripts\python.exe main.py demo/requirements --output-dir demo/output --type web --demo"
