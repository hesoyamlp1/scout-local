[CmdletBinding()]
param(
    [string]$ScoutHome = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

if ($ScoutHome) {
    $env:SCOUT_HOME = $ScoutHome
}

function Find-ScoutPython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("3.12", "3.11", "3.10")) {
            $Path = & py "-$Version" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $Path) { return $Path.Trim() }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $Path = & python -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
        if ($LASTEXITCODE -eq 0 -and $Path) { return $Path.Trim() }
    }
    throw "Scout requires Python 3.10 or newer. Ask Codex to install Python 3.12, then retry."
}

$Python = Find-ScoutPython
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Scout's Python environment..."
    & $Python -m venv (Join-Path $RepoRoot ".venv")
}

Write-Host "Installing Scout dependencies..."
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot "requirements.txt")
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot "tools\worker-requirements.txt")

if (-not $env:SCOUT_HOME) {
    $env:SCOUT_HOME = Join-Path $env:LOCALAPPDATA "Scout"
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:SCOUT_HOME "playwright"
Write-Host "Installing Scout's browser component..."
& $VenvPython -m playwright install chromium

& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") init
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") login
if ($LASTEXITCODE -ne 0) { throw "Codex sign-in did not complete." }

& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") start --no-open
if ($LASTEXITCODE -ne 0) { throw "Scout failed to start." }
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") doctor
if ($LASTEXITCODE -ne 0) { throw "Scout doctor did not pass." }
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") open

Write-Host "Scout is installed. Codex can now run .\scout.ps1 start, doctor, stop, or restart."
