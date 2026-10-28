# Prove the frozen helper serves the staging extension health contract.
# Run after windows/build.ps1. Stops the helper before it returns.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Onedir = Join-Path $Root "dist\TileArcAutoFillHelper\TileArcAutoFillHelper.exe"
$Setup = Join-Path $Root "dist\TileArc-AutoFill-Helper-Setup-staging.exe"

function Stop-Helper {
    Get-Process -Name "TileArcAutoFillHelper" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

function Show-HelperLogs {
    $dir = Join-Path $env:LOCALAPPDATA "TileArc\AutoFill"
    foreach ($name in @("doctor.json", "helper.log")) {
        $path = Join-Path $dir $name
        if (Test-Path $path) {
            Write-Host "----- $path -----"
            Get-Content -Path $path -Tail 80
        }
    }
}

function Assert-Doctor {
    param([string]$ExePath)
    Write-Host "Doctor: $ExePath"
    & $ExePath --doctor
    if ($LASTEXITCODE -ne 0) {
        Show-HelperLogs
        throw "doctor exited $LASTEXITCODE"
    }
    $doctor = Join-Path $env:LOCALAPPDATA "TileArc\AutoFill\doctor.json"
    if (-not (Test-Path $doctor)) {
        throw "doctor.json was not written"
    }
    $report = Get-Content -Raw -Path $doctor | ConvertFrom-Json
    if ($report.driverOk -ne $true) { throw "Playwright driver missing: $($report.driver)" }
    if ($report.domUiOk -ne $true) { throw "dom_ui.js missing: $($report.domUi)" }
    if ($report.attach -ne "1") { throw "AUTOFILL_LOCAL_ATTACH was not set" }
    if ($report.bind -ne "127.0.0.1") { throw "helper is not loopback-only" }
    if ($report.port -ne 9410) { throw "helper port is $($report.port), expected 9410" }
    if ($report.profileIsDailyChrome -eq $true) { throw "profile is daily Chrome" }
    if ($report.submit -ne "human") { throw "submit is not human" }
    Write-Host "Doctor ok. Profile: $($report.profileDir)"
}

function Wait-Health {
    param([int]$Seconds = 90)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:9410/health" -UseBasicParsing -TimeoutSec 3
            $body = $response.Content | ConvertFrom-Json
            if (
                $body.status -eq "ok" -and
                $body.service -eq "auto-fill-local" -and
                $body.mode -eq "dedicated-profile" -and
                $body.submit -eq "human"
            ) {
                return $body
            }
            Write-Host "Unexpected health body: $($response.Content)"
        } catch {
            Write-Host "Waiting for http://127.0.0.1:9410/health"
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    Show-HelperLogs
    throw "health did not become ready on 127.0.0.1:9410"
}

function Assert-Loopback {
    $lines = netstat -ano -p tcp | Select-String ":9410" | Select-String "LISTENING"
    if (-not $lines) {
        throw "nothing is listening on 9410"
    }
    $text = ($lines | Out-String).Trim()
    Write-Host $text
    if ($text -notmatch "127\.0\.0\.1:9410") {
        throw "9410 is not bound to 127.0.0.1"
    }
    if ($text -match "0\.0\.0\.0:9410" -or $text -match "\[::\]:9410") {
        throw "9410 is exposed beyond loopback"
    }
}

function Assert-Running {
    param([string]$ExePath)
    Stop-Helper
    $proc = Start-Process -FilePath $ExePath -PassThru -WindowStyle Hidden
    try {
        $body = Wait-Health
        Write-Host ($body | ConvertTo-Json -Compress)
        Assert-Loopback
    } finally {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
        Stop-Helper
    }
}

if (-not (Test-Path $Onedir)) { throw "Missing $Onedir" }
if (-not (Test-Path $Setup)) { throw "Missing $Setup" }

Assert-Doctor -ExePath $Onedir
Assert-Running -ExePath $Onedir

$dest = Join-Path $env:TEMP "TileArcHelperSmoke"
if (Test-Path $dest) {
    Remove-Item -Recurse -Force $dest
}
Write-Host "Silent install to $dest"
$install = Start-Process -FilePath $Setup -ArgumentList @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/DIR=$dest"
) -Wait -PassThru
if ($install.ExitCode -ne 0) {
    throw "installer exited $($install.ExitCode)"
}
$installed = Join-Path $dest "TileArcAutoFillHelper.exe"
if (-not (Test-Path $installed)) {
    throw "installer did not write $installed"
}
try {
    Assert-Doctor -ExePath $installed
    Assert-Running -ExePath $installed
} finally {
    Stop-Helper
    $unins = Join-Path $dest "unins000.exe"
    if (Test-Path $unins) {
        $removed = Start-Process -FilePath $unins -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART"
        ) -Wait -PassThru
        Write-Host "Uninstall exit $($removed.ExitCode)"
    }
}

Write-Host "Smoke passed for TileArc-AutoFill-Helper-Setup-staging.exe"
