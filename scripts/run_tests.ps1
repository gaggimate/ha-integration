# Runs the test suite on Windows using the venv made by setup_test_env.ps1.
#
# Usage: scripts\run_tests.ps1 [-Root <dir>] [pytest args...]
#   -Root, if given, must be the FIRST argument; everything else is passed to
#   pytest verbatim (no PowerShell parameter binding, so -v, -k, -W, -p etc. work).
#   Default Root: %LOCALAPPDATA%\gaggimate-ha-tests
# Sets the environment up first when it is missing or requirements_test.txt changed.
# Exit code is pytest's.

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Req = Join-Path $RepoRoot 'requirements_test.txt'

$Root = Join-Path $env:LOCALAPPDATA 'gaggimate-ha-tests'
$PytestArgs = @($args)
if ($PytestArgs.Count -ge 1 -and $PytestArgs[0] -ieq '-Root') {
    if ($PytestArgs.Count -lt 2) { Write-Error '-Root needs a value'; exit 2 }
    $Root = $PytestArgs[1]
    if ($PytestArgs.Count -gt 2) { $PytestArgs = @($PytestArgs[2..($PytestArgs.Count - 1)]) } else { $PytestArgs = @() }
}

# Resolve against the original working directory, before any Push-Location.
if (-not [string]::IsNullOrWhiteSpace($Root)) {
    if (-not [System.IO.Path]::IsPathRooted($Root)) { $Root = Join-Path (Get-Location).Path $Root }
    $Root = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
}
$VenvPy = Join-Path $Root 'venv\Scripts\python.exe'
$Stub = Join-Path $Root 'venv\Lib\site-packages\resource.py'
$Marker = Join-Path $Root '.gaggimate-ha-tests'

$needSetup = -not ((Test-Path $VenvPy) -and (Test-Path $Stub) -and (Test-Path $Marker))
if (-not $needSetup) {
    $want = 'requirements_sha256=' + (Get-FileHash -Algorithm SHA256 -Path $Req).Hash
    if (-not (@(Get-Content $Marker) -contains $want)) {
        Write-Host 'requirements_test.txt changed since the environment was built; refreshing.'
        $needSetup = $true
    }
}
if ($needSetup) {
    try {
        & (Join-Path $PSScriptRoot 'setup_test_env.ps1') -Root $Root
    } catch {
        Write-Host "Setup failed: $($_.Exception.Message)"
        exit 1
    }
}

Push-Location $RepoRoot
try {
    $ErrorActionPreference = 'Continue'
    & $VenvPy -m pytest -p no:cacheprovider @PytestArgs
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
