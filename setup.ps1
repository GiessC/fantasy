# One-shot Windows setup. Run it through setup.cmd rather than directly:
#
#     setup.cmd
#
# setup.cmd passes -ExecutionPolicy Bypass, which is what stops Windows from
# refusing to run this file at all. Safe to re-run; it never overwrites an
# existing config.

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

# Path to a real python.exe of at least 3.11, or $null.
function Resolve-Python($exe, $versionFlag) {
    try {
        $probe = @()
        if ($versionFlag) { $probe += $versionFlag }
        $probe += @('-c', 'import sys; print(sys.executable); print("%d.%d" % sys.version_info[:2])')

        $output = & $exe @probe 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }

        $lines = @($output)
        if ($lines.Count -lt 2) { return $null }

        $exePath = "$($lines[0])".Trim()
        $version = "$($lines[1])".Trim().Split('.')
        if ($version.Count -lt 2) { return $null }
        if ([int]$version[0] -lt 3) { return $null }
        if ([int]$version[0] -eq 3 -and [int]$version[1] -lt 11) { return $null }

        # The Microsoft Store ships a stub python.exe that only opens the Store
        # page. It can report a version yet cannot produce a working venv.
        if ($exePath -like '*WindowsApps*') { return $null }
        if (-not (Test-Path $exePath)) { return $null }

        return $exePath
    } catch {
        return $null
    }
}

Write-Step "Looking for Python 3.11 or newer"

$python = $null
# 'py' is the Windows Python launcher: the most reliable way to reach a real
# interpreter rather than whatever 'python' happens to resolve to.
foreach ($flag in @('-3.13', '-3.12', '-3.11', '-3')) {
    $python = Resolve-Python 'py' $flag
    if ($python) { break }
}
if (-not $python) { $python = Resolve-Python 'python' $null }
if (-not $python) { $python = Resolve-Python 'python3' $null }

if (-not $python) {
    Fail @"
No usable Python 3.11 or newer was found.

Install it from https://www.python.org/downloads/
Do NOT use the Microsoft Store version -- its python.exe is a stub that cannot
create a working virtual environment.

In the installer, tick "Add python.exe to PATH" on the first screen.
Then close this window, open a new one, and run setup.cmd again.
"@
}
Write-Ok "Using $python"

# ------------------------------------------------------------------ the venv

$venvPy  = Join-Path $root '.venv\Scripts\python.exe'
# The console script, rather than 'python -m fantasy_ai.cli.main': running the
# package as a module emits a RuntimeWarning about import order on every call.
$fantasy = Join-Path $root '.venv\Scripts\fantasy-ai.exe'

if (Test-Path $venvPy) {
    Write-Step "Virtual environment already exists"
    Write-Ok ".venv"
} else {
    Write-Step "Creating the virtual environment (.venv)"
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Fail "Could not create the virtual environment. If this Python came from the Microsoft Store, install it from python.org instead."
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
Write-Host "  4. .\fantasy-ai sync all --verbose   (replaces demo data with real data)"
Write-Host ""
