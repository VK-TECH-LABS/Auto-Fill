# Build TileArc-AutoFill-Helper-Setup-staging.exe on Windows.
# Employees do not run this. GitHub Actions runs it on windows-latest.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "Installing Auto-Fill and PyInstaller"
python -m pip install --upgrade pip
python -m pip install -e ".[dev]" "pyinstaller==6.16.0"

Write-Host "Freezing the helper"
python -m PyInstaller --noconfirm --clean windows/autofill_helper.spec

$bundle = Join-Path $Root "dist\TileArcAutoFillHelper"
$exe = Join-Path $bundle "TileArcAutoFillHelper.exe"
if (-not (Test-Path $exe)) {
    throw "Missing $exe"
}

$driver = Get-ChildItem -Path $bundle -Recurse -Filter "node.exe" |
    Where-Object { $_.FullName -match "playwright" -and $_.FullName -match "driver" } |
    Select-Object -First 1
if (-not $driver) {
    Write-Host "Bundle layout:"
    Get-ChildItem -Path $bundle -Recurse -Depth 3 | Select-Object -ExpandProperty FullName
    throw "Playwright driver node.exe was not frozen into the helper."
}
Write-Host "Playwright driver: $($driver.FullName)"

$cli = Get-ChildItem -Path $bundle -Recurse -Filter "cli.js" |
    Where-Object { $_.FullName -match "playwright" } |
    Select-Object -First 1
if (-not $cli) {
    throw "Playwright driver cli.js was not frozen into the helper."
}

function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host "Installing Inno Setup"
    choco install innosetup --yes --no-progress
    $iscc = Find-Iscc
}
if (-not $iscc) {
    $installer = Join-Path $env:TEMP "innosetup-6.exe"
    Invoke-WebRequest -Uri "https://jrsoftware.org/download.php/is.exe" -OutFile $installer
    Start-Process -FilePath $installer -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
    $iscc = Find-Iscc
}
if (-not $iscc) {
    throw "ISCC.exe was not found after installing Inno Setup."
}

$version = python -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
Write-Host "Compiling installer $version with $iscc"
& $iscc "/DMyAppVersion=$version" (Join-Path $PSScriptRoot "TileArcAutoFillHelper.iss")
if ($LASTEXITCODE -ne 0) {
    throw "ISCC failed with exit $LASTEXITCODE"
}

$setup = Join-Path $Root "dist\TileArc-AutoFill-Helper-Setup-staging.exe"
if (-not (Test-Path $setup)) {
    throw "Missing $setup"
}
Write-Host "Installer: $setup"
