[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "login", "start", "stop", "restart", "status", "doctor", "open")]
    [string]$Command = "start",
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Scout 尚未安装。请先运行 .\install.ps1。"
}
$Args = @((Join-Path $PSScriptRoot "tools\scout_local.py"), $Command)
if ($NoOpen -and $Command -in @("start", "restart")) { $Args += "--no-open" }
& $Python @Args
exit $LASTEXITCODE
