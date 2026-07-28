param(
  [string]$RepositoryRoot = '',
  [string]$CodexHome = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
  $RepositoryRoot = Get-AiMemoryRepositoryRoot
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
  $CodexHome = Join-Path $HOME '.codex'
}

$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
$canonicalSkill = Join-Path $RepositoryRoot 'skill\ai-memory\SKILL.md'
if (!(Test-Path -LiteralPath $python)) {
  throw "Application environment is missing: $python"
}
if (!(Test-Path -LiteralPath $canonicalSkill)) {
  throw "Canonical AI Memory skill is missing: $canonicalSkill"
}

New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
$configPath = Join-Path $CodexHome 'config.toml'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$pythonToml = $python.Replace('\', '/')
$mainBlock = @"
[mcp_servers.ai-memory]
command = '$pythonToml'
args = ["-m", "ai_memory_mcp.server", "--transport", "stdio"]
startup_timeout_sec = 120
tool_timeout_sec = 1800

"@

$config = if (Test-Path -LiteralPath $configPath) {
  Get-Content -LiteralPath $configPath -Raw
}
else {
  ''
}
$serverPattern = '(?ms)^\[mcp_servers(?:\.ai-memory|\."ai-memory")\]\r?\n.*?(?=^\[|\z)'
$toolPattern = '(?ms)^\[mcp_servers(?:\.ai-memory|\."ai-memory")\.tools\.[^\]]+\]\r?\n.*?(?=^\[|\z)'
$withoutOldToolBlocks = [regex]::Replace($config, $toolPattern, '')
if ([regex]::IsMatch($withoutOldToolBlocks, $serverPattern)) {
  $updatedConfig = [regex]::Replace(
    $withoutOldToolBlocks,
    $serverPattern,
    $mainBlock,
    1
  )
}
else {
  $updatedConfig = $withoutOldToolBlocks.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $mainBlock
}
$syncApprovalBlock = @"
[mcp_servers.ai-memory.tools.memory_sync]
approval_mode = "approve"

"@
$updatedConfig = $updatedConfig.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $syncApprovalBlock

if ($updatedConfig -ne $config) {
  if (Test-Path -LiteralPath $configPath) {
    $backup = "$configPath.backup-$timestamp-ai-memory"
    Copy-Item -LiteralPath $configPath -Destination $backup
    Write-Host "Preserved Codex config backup: $backup"
  }
  [System.IO.File]::WriteAllText($configPath, $updatedConfig, [System.Text.UTF8Encoding]::new($false))
  Write-Host "Registered the repository-owned MCP in $configPath" -ForegroundColor Green
}

$skillDirectory = Join-Path $CodexHome 'skills\ai-memory'
New-Item -ItemType Directory -Force -Path $skillDirectory | Out-Null
$stubPath = Join-Path $skillDirectory 'SKILL.md'
$skillPath = $canonicalSkill.Replace('\', '/')
$stub = @"
---
name: ai-memory
description: Use when meaningful work produces durable knowledge that future agents should retain, when the user asks to remember or recall something, or when Graphify-backed Markdown memory needs retrieval, organization, consolidation, conflict handling, session or handoff capture, or refresh. Invoke automatically during substantive work and before completion; do not wait for the user to ask.
---

Before following any instruction in this stub, first check the canonical skill header in '$skillPath'. If the source skill metadata has changed and this stub is out of date, update this stub to match the current source skill metadata before proceeding.

Then read the SKILL.md in full from '$skillPath'
"@

$existingStub = if (Test-Path -LiteralPath $stubPath) {
  Get-Content -LiteralPath $stubPath -Raw
}
else {
  ''
}
if ($existingStub -ne $stub) {
  if (Test-Path -LiteralPath $stubPath) {
    $stubBackup = Join-Path $skillDirectory "SKILL.md.backup-$timestamp"
    Copy-Item -LiteralPath $stubPath -Destination $stubBackup
    Write-Host "Preserved skill stub backup: $stubBackup"
  }
  [System.IO.File]::WriteAllText($stubPath, $stub, [System.Text.UTF8Encoding]::new($false))
  Write-Host "Installed the AI Memory discovery stub at $stubPath" -ForegroundColor Green
}

Write-Host 'Restart Codex to load the updated MCP command and skill source.' -ForegroundColor Yellow
