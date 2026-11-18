<#
.SYNOPSIS
  Creates (or refreshes) a Windows test environment for the GaggiMate HA integration.
.DESCRIPTION
  Idempotent. Needs no admin rights and installs nothing system-wide.
  1. Finds a recent uv (PATH, else pip-installs uv into <Root>\uvboot).
  2. Runs 'uv python install 3.14 --no-bin --no-registry' and creates <Root>\venv
     (Python >= 3.14.2). Nothing is written outside <Root> except uv's own
     managed-Python cache (%APPDATA%\uv\python); no shims on PATH, no registry keys.
  3. Installs requirements_test.txt.
  4. Writes fcntl.py / resource.py stubs into the venv (Windows-only test shims).
  5. Writes <Root>\.gaggimate-ha-tests (marker + requirements hash).
  Safety: refuses to use a Root that is empty, a drive root, the user profile, or
  the repository / one of its parents, and only recreates an existing venv when the
  marker file proves a previous run of this script made it.
  WSL/Linux remains the official/CI route; see README.
.PARAMETER Root
  Target directory. Default: $env:LOCALAPPDATA\gaggimate-ha-tests
#>
[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'gaggimate-ha-tests')
)

$ErrorActionPreference = 'Stop'
$MinUv = [version]'0.9.0'      # first uv line shipping CPython 3.14 builds
$MinPy = [version]'3.14.2'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Req = Join-Path $RepoRoot 'requirements_test.txt'

# Run native commands without PS 5.1 turning stderr output into terminating errors.
function Invoke-Native([scriptblock]$cmd) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $cmd } finally { $ErrorActionPreference = $old }
}

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    Invoke-Native $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

function Get-UvVersion([string]$exe) {
    try {
        $out = Invoke-Native { & $exe --version 2>$null }
        if ($out -match '(\d+\.\d+\.\d+)') { return [version]$Matches[1] }
    } catch { }
    return $null
}

# --- 0. Root guard (before anything is created) ------------------------
if ([string]::IsNullOrWhiteSpace($Root)) { throw 'Refusing to run: -Root is empty.' }
if (-not [System.IO.Path]::IsPathRooted($Root)) { $Root = Join-Path (Get-Location).Path $Root }
$Root = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
$RepoFull = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
$DriveRoot = [System.IO.Path]::GetPathRoot($Root).TrimEnd('\')
$Profile_ = ''
if ($env:USERPROFILE) { $Profile_ = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') }
if ($Root -eq '' -or $Root -ieq $DriveRoot) { throw "Refusing to run: -Root '$Root' is a drive root." }
if ($Profile_ -and $Root -ieq $Profile_) { throw "Refusing to run: -Root '$Root' is the user profile folder." }
if (($RepoFull + '\').StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to run: -Root '$Root' is the repository or one of its parent folders."
}

$VenvDir = Join-Path $Root 'venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$Marker = Join-Path $Root '.gaggimate-ha-tests'
$BootDir = Join-Path $Root 'uvboot'
$BootUv = Join-Path $BootDir 'Scripts\uv.exe'

if ((Test-Path $VenvDir) -and -not (Test-Path $Marker)) {
    throw "Refusing to touch existing '$VenvDir': it has no marker file '$Marker' (not created by this script). Choose another -Root or remove it yourself."
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null

function Get-ReqHash { (Get-FileHash -Algorithm SHA256 -Path $Req).Hash }

function Write-Marker([string]$hash) {
    $text = "gaggimate-ha-tests environment (created by scripts/setup_test_env.ps1)`r`nrequirements_sha256=$hash`r`n"
    [System.IO.File]::WriteAllText($Marker, $text, (New-Object System.Text.ASCIIEncoding))
}

# --- 1. uv -------------------------------------------------------------
# Returns a private bootstrapped uv (>= MinUv); -Upgrade forces 'pip install -U uv'.
function Get-BootstrappedUv([switch]$Upgrade) {
    $v = $null
    if (Test-Path $BootUv) { $v = Get-UvVersion $BootUv }
    if ($Upgrade -or -not ($v -and $v -ge $MinUv)) {
        $boot = $null
        foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
            if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
                $extra = @(); if ($c.Count -gt 1) { $extra = $c[1..($c.Count - 1)] }
                Invoke-Native { & $c[0] @extra -c "import venv" 2>$null }
                if ($LASTEXITCODE -eq 0) { $boot = $c; break }
            }
        }
        if (-not $boot) { throw 'No usable python or py launcher found to bootstrap uv. Install uv >= 0.9 (user-level) and retry.' }
        $extra = @(); if ($boot.Count -gt 1) { $extra = $boot[1..($boot.Count - 1)] }
        if (-not (Test-Path (Join-Path $BootDir 'Scripts\python.exe'))) {
            Write-Host "Bootstrapping uv into $BootDir using: $($boot -join ' ')"
            Invoke-Checked 'bootstrap venv' { & $boot[0] @extra -m venv $BootDir }
        }
        $bootPy = Join-Path $BootDir 'Scripts\python.exe'
        Invoke-Checked 'pip install uv' { & $bootPy -m pip install --quiet --upgrade uv }
        $v = Get-UvVersion $BootUv
    }
    if (-not ($v -and $v -ge $MinUv)) { throw "Bootstrapped uv is $v; need >= $MinUv." }
    Write-Host "Using private uv $v at $BootUv"
    return $BootUv
}

$uv = $null
$usingBoot = $false
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($found) {
    $v = Get-UvVersion $found.Source
    Write-Host "uv on PATH: $($found.Source) ($v)"
    if ($v -and $v -ge $MinUv) { $uv = $found.Source }
    else { Write-Host "uv on PATH is older than $MinUv; using a private copy (system uv is left untouched)." }
}
if (-not $uv) { $uv = Get-BootstrappedUv; $usingBoot = $true }

# --- 2. Python 3.14 + venv ---------------------------------------------
function Get-VenvPyVersion {
    if (-not (Test-Path $VenvPy)) { return $null }
    try {
        $o = Invoke-Native { & $VenvPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null }
        if ($LASTEXITCODE -eq 0 -and "$o" -match '^\d+\.\d+\.\d+$') { return [version]"$o" }
    } catch { }
    return $null
}

function New-TestVenv([string]$uvExe) {
    $flags = @()
    $help = Invoke-Native { & $uvExe python install --help 2>$null } | Out-String
    if ($help -match '--no-bin') { $flags += '--no-bin' }
    if ($help -match '--no-registry') { $flags += '--no-registry' }
    Invoke-Checked 'uv python install' { & $uvExe python install @flags 3.14 }
    # The venv path is guarded above: it is new, or carries our marker.
    Invoke-Checked 'uv venv' { & $uvExe venv --python 3.14 --clear $VenvDir }
    Write-Marker 'incomplete'
    return (Get-VenvPyVersion)
}

$pv = Get-VenvPyVersion
if ($pv -and $pv -ge $MinPy) {
    Write-Host "Reusing venv at $VenvDir (Python $pv)"
} else {
    $pv = New-TestVenv $uv
    if (-not ($pv -and $pv -ge $MinPy) -and -not $usingBoot) {
        Write-Host "Venv Python is $pv (need >= $MinPy); retrying once with a freshly upgraded private uv."
        $uv = Get-BootstrappedUv -Upgrade
        $pv = New-TestVenv $uv
    } elseif (-not ($pv -and $pv -ge $MinPy)) {
        Write-Host "Venv Python is $pv (need >= $MinPy); retrying once after upgrading the private uv."
        $uv = Get-BootstrappedUv -Upgrade
        $pv = New-TestVenv $uv
    }
    if (-not ($pv -and $pv -ge $MinPy)) {
        throw "Venv Python is $pv; need >= $MinPy even with the newest uv. Install a newer Python 3.14 and retry."
    }
}

# --- 3. requirements ---------------------------------------------------
Invoke-Checked 'uv pip install' { & $uv pip install --python $VenvPy -r $Req }

# --- 4. Windows-only stubs ---------------------------------------------
$site = ("" + (Invoke-Native { & $VenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>$null })).Trim()

$fcntl = @'
"""Windows-only test shim: stub of the POSIX 'fcntl' module.

Written by scripts/setup_test_env.ps1 into this virtualenv ONLY so that Home
Assistant's runner.py can be imported on Windows. Not part of the integration.
WSL/Linux is the official/CI test route.
"""
LOCK_EX = 2; LOCK_NB = 4; LOCK_SH = 1; LOCK_UN = 8
def flock(fd, op): return None
def fcntl(fd, op, arg=0): return 0
def ioctl(fd, op, arg=0, mutate_flag=True): return 0
'@

$resource = @'
"""Windows-only test shim: stub of the POSIX 'resource' module.

Written by scripts/setup_test_env.ps1 into this virtualenv ONLY so that Home
Assistant's runner.py can be imported on Windows. Not part of the integration.
WSL/Linux is the official/CI test route.
"""
RLIMIT_NOFILE = 7
RLIM_INFINITY = -1
def getrlimit(res): return (1048576, 1048576)
def setrlimit(res, lim): return None
'@

$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $site 'fcntl.py'), (($fcntl -replace "`r`n", "`n") + "`n"), $enc)
[System.IO.File]::WriteAllText((Join-Path $site 'resource.py'), (($resource -replace "`r`n", "`n") + "`n"), $enc)

Write-Marker (Get-ReqHash)

Write-Host ''
Write-Host "Test environment ready: $VenvDir (Python $pv)"
Write-Host 'Run the tests with:'
Write-Host "  & `"$VenvPy`" -m pytest -p no:cacheprovider"
Write-Host "or: powershell -File scripts\run_tests.ps1 -Root `"$Root`" [pytest args]"
