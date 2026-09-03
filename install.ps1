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
    throw "Scout 需要 Python 3.10 或更新版本。请让 Codex 安装 Python 3.12 后重新运行本脚本。"
}

$Python = Find-ScoutPython
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "正在创建 Scout 的独立 Python 环境……"
    & $Python -m venv (Join-Path $RepoRoot ".venv")
}

Write-Host "正在安装 Scout 依赖……"
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot "requirements.txt")
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot "tools\worker-requirements.txt")

if (-not $env:SCOUT_HOME) {
    $env:SCOUT_HOME = Join-Path $env:LOCALAPPDATA "Scout"
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:SCOUT_HOME "playwright"
Write-Host "正在安装 Scout 的网页读取组件……"
& $VenvPython -m playwright install chromium

& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") init
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") login
if ($LASTEXITCODE -ne 0) { throw "Codex 登录没有完成。" }

& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") start --no-open
if ($LASTEXITCODE -ne 0) { throw "Scout 启动失败。" }
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") doctor
if ($LASTEXITCODE -ne 0) { throw "Scout 自检没有通过。" }
& $VenvPython (Join-Path $RepoRoot "tools\scout_local.py") open

Write-Host "Scout 已安装完成。以后可以让 Codex 运行 .\scout.ps1 start、doctor、stop 或 restart。"
