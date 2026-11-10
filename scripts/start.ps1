param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
$pythonPath = Join-Path $projectPath '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host 'Setting up Social Watch (first launch)...'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw 'Python 3.10+ is required. Install Python and enable Add Python to PATH.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
$requirementsPath = Join-Path $projectPath 'requirements.txt'
$stampPath = Join-Path $projectPath '.venv/requirements.sha256'
$requiredHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $stampPath) { (Get-Content -LiteralPath $stampPath -Raw).Trim() } else { '' }
if ($requiredHash -ne $installedHash) {
    Write-Host 'Installing app dependencies...'
    & $pythonPath -m pip install --disable-pip-version-check -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check your internet connection and try again.' }
    Set-Content -LiteralPath $stampPath -Value $requiredHash -Encoding ASCII
}
$entryPath = Join-Path $projectPath 'verify.py'
if ($NoBrowser) {
    & $pythonPath $entryPath --no-browser
} else {
    & $pythonPath $entryPath
}
if ($LASTEXITCODE -ne 0) { throw 'Social Watch stopped with an error.' }
