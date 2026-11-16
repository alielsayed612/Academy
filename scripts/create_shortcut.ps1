param(
  [Parameter(Mandatory = $true)][string]$Target,
  [Parameter(Mandatory = $true)][string]$WorkDir,
  [string]$Icon = ""
)
# Creates Desktop shortcuts (English + Arabic names). Never fails the build.
$ErrorActionPreference = "SilentlyContinue"
$arName = (-join ([char[]](0x623,0x643,0x627,0x62F,0x64A,0x645,0x64A,0x629,0x20,0x64A,0x648,0x645,0x20,0x62C,0x62F,0x64A,0x62F))) + ".lnk"
$wsh = New-Object -ComObject WScript.Shell
$desk = [Environment]::GetFolderPath("Desktop")
if (-not $desk -or -not (Test-Path $desk)) { $desk = Join-Path $env:USERPROFILE "Desktop" }
foreach ($name in @("YawmJadidAcademy.lnk", $arName)) {
  $path = Join-Path $desk $name
  $s = $wsh.CreateShortcut($path)
  $s.TargetPath = $Target
  $s.WorkingDirectory = $WorkDir
  if ($Icon -and (Test-Path $Icon)) { $s.IconLocation = "$Icon,0" }
  $s.Description = "Yawm Jadid Academy Suite"
  $s.Save()
  Write-Host ("       Desktop Shortcut: " + $path)
}
exit 0
