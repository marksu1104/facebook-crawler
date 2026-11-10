$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
$pythonPath = Join-Path $projectPath '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run start.cmd first to create the Python environment.' }
$artifactPath = Join-Path $projectPath 'artifacts'
New-Item -ItemType Directory -Path $artifactPath -Force | Out-Null
$testPath = Join-Path $artifactPath ('pytest-' + [guid]::NewGuid().ToString('N'))
& $pythonPath -m pytest -q --basetemp $testPath
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
