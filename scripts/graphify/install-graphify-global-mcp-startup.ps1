$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'start-graphify-global-mcp.ps1'
$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$launcherVbs = Join-Path $startupDir 'graphify-global-mcp-start.vbs'

New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
$vbs = @"
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$scriptPath""", 0, False
"@
$existing = if (Test-Path -LiteralPath $launcherVbs) {
  Get-Content -LiteralPath $launcherVbs -Raw
} else {
  ''
}
if ($existing -ne $vbs) {
  if (Test-Path -LiteralPath $launcherVbs) {
    $archiveRoot = Join-Path $HOME '.graphify\backups\startup'
    New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
    $archivePath = Join-Path $archiveRoot "graphify-global-mcp-start-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff').vbs"
    Copy-Item -LiteralPath $launcherVbs -Destination $archivePath
    Write-Host "Preserved the previous startup launcher at $archivePath"
  }
  [System.IO.File]::WriteAllText($launcherVbs, $vbs, [System.Text.UTF8Encoding]::new($false))
}
Write-Host "Installed the Graphify startup launcher at $launcherVbs" -ForegroundColor Green
