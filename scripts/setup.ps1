$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$pythonVersion = (Get-Content ".python-version" -Raw).Trim()
if ($pythonVersion -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
    throw "Invalid .python-version: expected MAJOR.MINOR.PATCH, got '$pythonVersion'."
}

$pythonMinorVersion = "$($Matches[1]).$($Matches[2])"
$pythonVersionOutput = & py "-$pythonMinorVersion" --version
if ($LASTEXITCODE -ne 0) {
    throw "Python $pythonVersion was not found. Install 64-bit Python $pythonVersion and retry."
}
if ($pythonVersionOutput -ne "Python $pythonVersion") {
    throw "Expected Python $pythonVersion from .python-version, but the launcher selected '$pythonVersionOutput'."
}
$pythonBits = & py "-$pythonMinorVersion" -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0) { throw "Failed to inspect the selected Python architecture" }
if ($pythonBits -ne "64") {
    throw "Expected 64-bit Python $pythonVersion, but the launcher selected a $pythonBits-bit interpreter."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py "-$pythonMinorVersion" -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv" }
}

$venvVersion = & .\.venv\Scripts\python.exe --version
if ($LASTEXITCODE -ne 0) { throw "Failed to inspect the existing virtual environment" }
if ($venvVersion -ne "Python $pythonVersion") {
    throw "Expected .venv to use Python $pythonVersion, but found '$venvVersion'. Remove .venv and rerun setup."
}
$venvBits = & .\.venv\Scripts\python.exe -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0) { throw "Failed to inspect the virtual environment architecture" }
if ($venvBits -ne "64") {
    throw "Expected a 64-bit .venv, but found a $venvBits-bit interpreter. Remove .venv and rerun setup."
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install project requirements" }

Write-Output "Environment is ready. Run: .\.venv\Scripts\python.exe main.py demo/requirements --output-dir demo/output --type web --demo"
