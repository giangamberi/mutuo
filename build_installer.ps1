# Builds the Mutuo Windows installer.
#
#   1. PyInstaller  -> dist\Mutuo\  (onedir bundle)
#   2. Inno Setup   -> installer\output\Mutuo-Setup-<ver>.exe
#
# Usage:  .\build_installer.ps1
#
# Prereqs: pyinstaller in the venv, Inno Setup 6 installed (winget install
# JRSoftware.InnoSetup).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found at $py" }

# PyInstaller and ISCC write progress to stderr; PowerShell 5.1 with
# ErrorActionPreference=Stop treats native stderr as a fatal error, so drop
# to Continue around the native calls and rely on $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

# --- 1. PyInstaller ---------------------------------------------------------
Write-Host "==> PyInstaller build" -ForegroundColor Cyan
& $py -m PyInstaller Mutuo.spec --noconfirm --clean 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$bundle = Join-Path $root "dist\Mutuo"
if (-not (Test-Path (Join-Path $bundle "Mutuo.exe"))) {
    throw "bundle exe missing: $bundle"
}

# --- 2. Sync version into the installer script ------------------------------
# version.py is the single source of truth; keep the .iss in lockstep so the
# installer filename + AppVersion always match the running app.
Write-Host "==> Syncing version" -ForegroundColor Cyan
$ver = & $py -c "import version; print(version.__version__)" 2>&1
if ($LASTEXITCODE -ne 0 -or -not $ver) { throw "could not read version from version.py" }
$ver = "$ver".Trim()
$iss = Join-Path $root "installer\Mutuo.iss"
(Get-Content $iss -Raw) -replace '(?m)^#define MyAppVersion ".*"$', "#define MyAppVersion `"$ver`"" |
    Set-Content $iss -NoNewline
Write-Host "    version $ver -> $iss"

# --- 3. Inno Setup ----------------------------------------------------------
Write-Host "==> Inno Setup build" -ForegroundColor Cyan
$iscc = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found - install Inno Setup 6 first" }

& $iscc (Join-Path $root "installer\Mutuo.iss") 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$setup = Get-ChildItem (Join-Path $root "installer\output") -Filter "Mutuo-Setup-*.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ""
Write-Host "Installer ready: $($setup.FullName)" -ForegroundColor Green
