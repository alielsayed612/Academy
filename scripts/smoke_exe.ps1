<#
  Headless smoke test for the PyInstaller build (Windows). Added in session 9 - WRITTEN, NOT YET RUN on Windows.
  Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_exe.ps1 [-ExePath dist\YawmJadidAcademy\YawmJadidAcademy.exe] [-Port 8899]
  What it proves: the frozen EXE starts, serves the bundled UI, answers /api/health, creates the DB + log in an ISOLATED
  temp data folder (never your real %LOCALAPPDATA%\AlAhdAcademySuite), and exits cleanly. It does NOT replace docs/RELEASE_CHECKLIST.md.
#>
param(
  [string]$ExePath = "dist\YawmJadidAcademy\YawmJadidAcademy.exe",
  [int]$Port = 8899,
  [int]$TimeoutSec = 45
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $ExePath)) { Write-Error "EXE not found: $ExePath"; exit 1 }

$home_ = Join-Path ([IO.Path]::GetTempPath()) ("yj_smoke_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $home_ | Out-Null
$secret = [guid]::NewGuid().ToString("N")
$env:ALAHD_ERP_HOME = $home_
$env:ALAHD_ERP_PORT = "$Port"
$env:ALAHD_ERP_NO_UI = "1"
$env:ALAHD_WINDOW_SECRET = $secret

$proc = Start-Process -FilePath $ExePath -PassThru -WindowStyle Hidden
$ok = $false; $why = ""
try {
  $hdr = @{ "X-Window-Secret" = $secret }
  $base = "http://127.0.0.1:$Port"
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) { $why = "process exited early with code $($proc.ExitCode)"; break }
    try {
      $h = Invoke-WebRequest -Uri "$base/api/health" -Headers $hdr -UseBasicParsing -TimeoutSec 3
      if ($h.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  if ($ok) {
    $ui = Invoke-WebRequest -Uri "$base/" -Headers $hdr -UseBasicParsing -TimeoutSec 5
    if ($ui.StatusCode -ne 200 -or $ui.Content -notmatch "<html") { $ok = $false; $why = "UI index.html not served (bundled UI missing?)" }
  } elseif (-not $why) { $why = "no healthy /api/health within $TimeoutSec s" }
  if ($ok) {
    $db = Get-ChildItem -Path $home_ -Filter "alahd_erp_v1.sqlite3" -ErrorAction SilentlyContinue
    if (-not $db) { $ok = $false; $why = "database file was not created in $home_" }
  }
} finally {
  if (-not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
  Start-Sleep -Seconds 1
}

if ($ok) {
  Write-Host "SMOKE OK - EXE started, served UI + /api/health, created DB in isolated folder."
  Remove-Item -Recurse -Force $home_ -ErrorAction SilentlyContinue
  exit 0
}
Write-Host "SMOKE FAILED: $why"
$log = Join-Path $home_ "logs\alahd_erp.log"
if (Test-Path $log) { Write-Host "--- last log lines ($log) ---"; Get-Content $log -Tail 40 }
exit 1
