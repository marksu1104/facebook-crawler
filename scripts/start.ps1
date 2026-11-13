param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
$pythonPath = Join-Path $projectPath '.venv/Scripts/python.exe'
$entryPath = Join-Path $projectPath 'start.py'
$launchArguments = @($entryPath)
if ($NoBrowser) { $launchArguments += '--no-browser' }
if (Test-Path -LiteralPath $pythonPath) {
    & $pythonPath @launchArguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 @launchArguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python @launchArguments
} else {
    throw 'Python 3.10+ is required. Install Python and enable Add Python to PATH.'
}
if ($LASTEXITCODE -ne 0) { throw 'Social Watch stopped with an error.' }
