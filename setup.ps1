# One-shot Windows setup. Run it through setup.cmd rather than directly:
#
#     setup.cmd
#
# setup.cmd passes -ExecutionPolicy Bypass, which is what stops Windows from
# refusing to run this file at all. Safe to re-run; it never overwrites an
# existing config.

# -Clean rebuilds the virtual environment from scratch. PowerShell requires
# param() to be the first statement, so it leads.
param([switch]$Clean)

# Deliberately NOT 'Stop'. Windows PowerShell treats anything a native command
# writes to stderr as an error, and pip writes ordinary progress and warnings
# there -- so 'Stop' aborts the install over messages that are not failures.
# Every external command below is checked through $LASTEXITCODE instead, which
# is the only reliable success signal.
$ErrorActionPreference = 'Continue'

# Rich draws box-drawing characters. Windows PowerShell 5.1 defaults to a
# codepage that renders them as garbage, so ask for UTF-8 up front.
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $root

function Write-Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }

function Fail($text) {
    Write-Host ""
    Write-Host "SETUP FAILED" -ForegroundColor Red
    Write-Host $text -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Run an external command and stop the script if it reports failure.
function Invoke-Checked($exe, $arguments, $whatFailed) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail $whatFailed }
}

# ---------------------------------------------------------------- find python

# Every candidate tried, with the reason it was rejected. Printed on failure,
# because "Python not found" on a machine that has Python is useless on its own.
$script:Attempts = @()

# Probe one interpreter. Returns its real path if it is 3.11+, else $null.
function Test-PythonExe($exe, $versionFlag, $label) {
    # This payload deliberately contains NO quote characters. Windows PowerShell
    # mangles embedded double quotes when building the command line for a native
    # executable, which turns the probe into a syntax error and makes a perfectly
    # good interpreter look broken.
    $code = 'import sys; print(sys.executable); print(sys.version_info[0]); print(sys.version_info[1])'

    $probe = @()
    if ($versionFlag) { $probe += $versionFlag }
    $probe += @('-c', $code)

    $output = $null
    try {
        $output = & $exe @probe 2>$null
    } catch {
        $script:Attempts += "  $label -- not installed / not on PATH"
        return $null
    }
    if ($LASTEXITCODE -ne 0) {
        $script:Attempts += "  $label -- exited with code $LASTEXITCODE"
        return $null
    }

    $lines = @($output | Where-Object { "$_".Trim() -ne '' })
    if ($lines.Count -lt 3) {
        $script:Attempts += "  $label -- gave no version (likely the Microsoft Store stub)"
        return $null
    }

    $exePath = "$($lines[0])".Trim()
    $major = 0
    $minor = 0
    [void][int]::TryParse("$($lines[1])".Trim(), [ref]$major)
    [void][int]::TryParse("$($lines[2])".Trim(), [ref]$minor)

    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
        $script:Attempts += "  $label -- Python $major.$minor, too old (need 3.11+)"
        return $null
    }
    # The Microsoft Store ships a stub python.exe that cannot build a real venv.
    if ($exePath -like '*WindowsApps*') {
        $script:Attempts += "  $label -- Microsoft Store version, cannot create a virtual environment"
        return $null
    }
    if (-not (Test-Path $exePath)) {
        $script:Attempts += "  $label -- reported $exePath, which does not exist"
        return $null
    }

    $script:Attempts += "  $label -- OK, Python $major.$minor at $exePath"
    return $exePath
}

# Python installed but never added to PATH is the single most common Windows
# case, so look where the installer actually puts it before giving up.
function Find-PythonOnDisk {
    $found = @()
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    if ($env:ProgramFiles)  { $roots += $env:ProgramFiles }
    $x86 = (Get-Item 'Env:ProgramFiles(x86)' -ErrorAction SilentlyContinue).Value
    if ($x86) { $roots += $x86 }
    $roots += 'C:\'

    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        try {
            $dirs = Get-ChildItem -Path $root -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue
            foreach ($dir in $dirs) {
                $candidate = Join-Path $dir.FullName 'python.exe'
                if (Test-Path $candidate) { $found += $candidate }
            }
        } catch { }
    }
    # Newest first: Python313 sorts above Python311.
    return @($found | Sort-Object -Descending -Unique)
}

Write-Step "Looking for Python 3.11 or newer"

$python = $null

# 1. The Windows Python launcher, newest first. Most reliable when present.
foreach ($flag in @('-3.13', '-3.12', '-3.11', '-3')) {
    $python = Test-PythonExe 'py' $flag "py $flag"
    if ($python) { break }
}

# 2. Whatever 'python' / 'python3' resolve to on PATH.
if (-not $python) { $python = Test-PythonExe 'python'  $null 'python (from PATH)' }
if (-not $python) { $python = Test-PythonExe 'python3' $null 'python3 (from PATH)' }

# 3. Standard install locations, for an install that never made it onto PATH.
if (-not $python) {
    Write-Host "    Not on PATH -- checking the usual install folders..." -ForegroundColor Yellow
    foreach ($candidate in (Find-PythonOnDisk)) {
        $python = Test-PythonExe $candidate $null $candidate
        if ($python) { break }
    }
}

if (-not $python) {
    $detail = ($script:Attempts -join "`r`n")
    if (-not $detail) { $detail = "  (nothing resembling Python was found at all)" }
    Fail @"
No usable Python 3.11 or newer was found.

Here is every candidate that was tried, and why each was rejected:

$detail

Most likely fixes, in order:

1. You installed Python but this window started BEFORE the install.
   PATH is only read when a window opens. Close this window, open a new
   one, and run setup.cmd again. This is the most common cause.

2. You installed from the Microsoft Store. Its python.exe is a stub and
   cannot create a virtual environment. Install from python.org instead:
   https://www.python.org/downloads/

3. You installed without ticking "Add python.exe to PATH". Re-run the
   installer, choose Modify, and enable it -- or just tell me the output
   of this command and I will point the script straight at it:

       where python

"@
}
Write-Ok "Using $python"

# ------------------------------------------------------------------ the venv

$venvDir = Join-Path $root '.venv'
$venvPy   = Join-Path $venvDir 'Scripts\python.exe'
# The console script, rather than 'python -m fantasy_ai.cli.main': running the
# package as a module emits a RuntimeWarning about import order on every call.
$fantasy = Join-Path $venvDir 'Scripts\fantasy-ai.exe'

# The interpreter directory a venv was built against, from its pyvenv.cfg.
# Named $venvHome and never $home, which is a PowerShell automatic variable.
function Get-VenvHome($dir) {
    $cfg = Join-Path $dir 'pyvenv.cfg'
    if (-not (Test-Path $cfg)) { return $null }
    try {
        foreach ($line in (Get-Content $cfg -ErrorAction Stop)) {
            if ($line -match '^\s*home\s*=\s*(.+?)\s*$') { return $matches[1] }
        }
    } catch { }
    return $null
}

# A virtual environment is a set of pointers to the interpreter that built it.
# Move it, delete that interpreter, or copy the folder from another machine and
# every command inside it fails on a path that does not exist here -- e.g.
# "did not find executable at '/usr/bin\python.exe'" from a venv built on Linux.
# The folder existing proves nothing.
#
# Two independent checks, because neither alone is sufficient: on Windows the
# venv's python.exe is a redirector that reads pyvenv.cfg to find the real
# interpreter, so a 'home' that does not exist here is fatal and worth testing
# directly rather than inferring from an exit code; but a venv can also be
# broken in ways pyvenv.cfg looks fine for, which running it catches.
function Test-VenvUsable($dir, $exe) {
    if (-not (Test-Path $exe)) { return $false }
    $venvHome = Get-VenvHome $dir
    if ($venvHome -and -not (Test-Path $venvHome)) { return $false }
    try {
        & $exe -c pass 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$needsVenv = $true

if (Test-Path $venvDir) {
    if ((-not $Clean) -and (Test-VenvUsable $venvDir $venvPy)) {
        Write-Step "Virtual environment already exists and works"
        Write-Ok ".venv"
        $needsVenv = $false
    } else {
        if ($Clean) {
            Write-Step "Rebuilding the virtual environment (-Clean)"
        } else {
            $stale = Get-VenvHome $venvDir
            Write-Step "The existing .venv is broken -- rebuilding it"
            if ($stale) {
                Write-Host "    It points at an interpreter that is not on this machine:" -ForegroundColor Yellow
                Write-Host "        $stale" -ForegroundColor Yellow
                Write-Host "    (a venv built on another machine, or by a Python since removed)" -ForegroundColor Yellow
            }
        }
        try {
            Remove-Item -Recurse -Force $venvDir -ErrorAction Stop
        } catch {
            Fail @"
Could not delete the broken virtual environment at:
    $venvDir

$_

Close any editor, terminal, or program using that folder, then run setup.cmd
again. If it still will not go, delete the .venv folder in File Explorer.
"@
        }
        Write-Ok "Removed"
    }
}

if ($needsVenv) {
    Write-Step "Creating the virtual environment (.venv)"
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Fail "Could not create the virtual environment with $python"
    }
    if (-not (Test-VenvUsable $venvDir $venvPy)) {
        Fail "Created .venv but it does not run. The interpreter used was $python"
    }
    Write-Ok "Created"
}

Write-Step "Installing dependencies (takes a minute the first time)"
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
# A pip that refuses to upgrade itself is not fatal; the install below is.
Invoke-Checked $venvPy @('-m', 'pip', 'install', '-e', '.[dev]', '--quiet', '--disable-pip-version-check') "Installing dependencies failed. The full error is above."
if (-not (Test-Path $fantasy)) {
    Fail "Install reported success but $fantasy is missing. Delete the .venv folder and run setup.cmd again."
}
Write-Ok "Installed"

# ------------------------------------------------------------------- configs

Write-Step "Setting up your config files"
foreach ($name in @('league', 'sources')) {
    $target  = Join-Path $root "config\$name.yaml"
    $example = Join-Path $root "config\$name.example.yaml"
    if (Test-Path $target) {
        Write-Ok "config\$name.yaml already exists -- left alone"
    } else {
        try {
            Copy-Item -Path $example -Destination $target -ErrorAction Stop
            Write-Ok "Created config\$name.yaml"
        } catch {
            Fail "Could not create config\$name.yaml : $_"
        }
    }
}

# ------------------------------------------------------------- prove it works

Write-Step "Creating the database"
Invoke-Checked $fantasy @('db', 'init') "Database setup failed. The error is above."

Write-Step "Loading demo data (synthetic -- invented players, no network needed)"
Invoke-Checked $fantasy @('sync', 'demo') "Loading demo data failed. The error is above."

Write-Step "Running the analysis to confirm the whole pipeline works"
Invoke-Checked $fantasy @('analyze', 'board', '--limit', '10') "Analysis failed. The error is above."

# ---------------------------------------------------------------- what's next

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Setup complete. The board above is synthetic demo data." -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Run everything through the fantasy-ai wrapper in this folder."
Write-Host "You never need to 'activate' anything."
Write-Host ""
Write-Host "  PowerShell:  .\fantasy-ai analyze board"
Write-Host "  cmd.exe:     fantasy-ai analyze board"
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Edit config\league.yaml -- teams, scoring, roster, your draft slot."
Write-Host "  2. Put your FantasyPros key in config\sources.yaml, under"
Write-Host "     sources.fantasypros.api_key"
Write-Host "  3. .\fantasy-ai validate-config      (confirms it read the key)"
Write-Host "  4. .\fantasy-ai sync all --verbose   (fetch real data)"
Write-Host "  5. .\fantasy-ai data clear --source demo" -ForegroundColor Yellow
Write-Host ""
Write-Host "Step 5 matters: the demo players above are INVENTED. They do not" -ForegroundColor Yellow
Write-Host "disappear when real data arrives -- they sit on the board next to" -ForegroundColor Yellow
Write-Host "real players until you clear them." -ForegroundColor Yellow
Write-Host ""
