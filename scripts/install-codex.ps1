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
$canonicalGraphifySkill = Join-Path $RepositoryRoot 'graphify-codebase\skill\graphify\SKILL.md'
if (!(Test-Path -LiteralPath $python)) {
  throw "Application environment is missing: $python"
}
if (!(Test-Path -LiteralPath $canonicalSkill)) {
  throw "Canonical AI Memory skill is missing: $canonicalSkill"
}
if (!(Test-Path -LiteralPath $canonicalGraphifySkill)) {
  throw "Canonical Graphify skill is missing: $canonicalGraphifySkill"
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

$skillSpecs = @(
  [pscustomobject]@{
    Name = 'ai-memory'
    Source = $canonicalSkill
  },
  [pscustomobject]@{
    Name = 'graphify'
    Source = $canonicalGraphifySkill
  }
)

foreach ($skillSpec in $skillSpecs) {
  $canonicalText = Get-Content -LiteralPath $skillSpec.Source -Raw
  if ($canonicalText -notmatch '(?s)^---\s*\r?\n(?<header>.*?)\r?\n---\s*\r?\n') {
    throw "Canonical skill has no valid YAML header: $($skillSpec.Source)"
  }
  $header = $Matches.header
  $nameMatch = [regex]::Match($header, '(?m)^name:\s*(?<value>.+?)\s*$')
  $descriptionMatch = [regex]::Match($header, '(?m)^description:\s*(?<value>.+?)\s*$')
  if (!$nameMatch.Success -or $nameMatch.Groups['value'].Value -ne $skillSpec.Name -or !$descriptionMatch.Success) {
    throw "Canonical skill metadata is invalid: $($skillSpec.Source)"
  }
  $description = $descriptionMatch.Groups['value'].Value
  $skillDirectory = Join-Path $CodexHome "skills\$($skillSpec.Name)"
  New-Item -ItemType Directory -Force -Path $skillDirectory | Out-Null
  $stubPath = Join-Path $skillDirectory 'SKILL.md'
  $skillPath = $skillSpec.Source.Replace('\', '/')
  $stub = @"
---
name: $($skillSpec.Name)
description: $description
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
    Write-Host "Installed the $($skillSpec.Name) discovery stub at $stubPath" -ForegroundColor Green
  }
  if ($skillSpec.Name -eq 'graphify') {
    $requirements = Get-Content -LiteralPath (Join-Path $RepositoryRoot 'requirements-graphify.txt') -Raw
    if ($requirements -notmatch '(?m)^graphifyy(?:\[[^\]]+\])?==(?<version>[^\s]+)$') {
      throw 'Pinned Graphify version is missing.'
    }
    $versionPath = Join-Path $skillDirectory '.graphify_version'
    [System.IO.File]::WriteAllText(
      $versionPath,
      "$($Matches.version)`n",
      [System.Text.UTF8Encoding]::new($false)
    )
  }
}

Write-Host 'Restart Codex to load the updated MCP command and skill sources.' -ForegroundColor Yellow
