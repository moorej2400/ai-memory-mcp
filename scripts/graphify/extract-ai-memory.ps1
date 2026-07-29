param(
  [string]$OutRoot = '',
  [string]$MemoryRoot = '',
  [string]$SourceId = '',
  [switch]$SkipGlobal
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')

$repositoryRoot = Get-AiMemoryRepositoryRoot
Import-AiMemoryEnvironment -RepositoryRoot $repositoryRoot
$GraphifyRoot = Get-AiMemoryGraphifyStateRoot
$CorporaRoot = Join-Path $GraphifyRoot 'corpora'
New-Item -ItemType Directory -Force -Path $CorporaRoot | Out-Null

$graphifyExecutable = Get-AiMemoryGraphifyExecutable -RepositoryRoot $repositoryRoot
if (!(Test-Path -LiteralPath $graphifyExecutable)) {
  throw "Pinned Graphify executable was not found at $graphifyExecutable. Run scripts\setup.ps1."
}

$sources = @(Get-AiMemorySources)
if ([string]::IsNullOrWhiteSpace($SourceId)) {
  $source = $sources | Where-Object Writable | Select-Object -First 1
  $SourceId = $source.SourceId
}
if ([string]::IsNullOrWhiteSpace($MemoryRoot)) {
  $source = $sources | Where-Object SourceId -EQ $SourceId | Select-Object -First 1
  if ($null -eq $source) {
    throw "Unknown memory source ID: $SourceId"
  }
  $MemoryRoot = $source.Root
}
$MemoryRoot = (Resolve-Path -LiteralPath $MemoryRoot).Path
$requiredExtractionSettings = @(
  'GRAPHIFY_OPENAI_BASE_URL',
  'GRAPHIFY_OPENAI_API_KEY',
  'GRAPHIFY_OPENAI_MODEL',
  'GRAPHIFY_OPENAI_TOKEN_BUDGET'
)
foreach ($setting in $requiredExtractionSettings) {
  if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($setting, 'Process'))) {
    throw "$setting is required for AI-Memory extraction."
  }
}

$env:OPENAI_BASE_URL = $env:GRAPHIFY_OPENAI_BASE_URL
$env:OPENAI_API_KEY = $env:GRAPHIFY_OPENAI_API_KEY
$env:OPENAI_MODEL = $env:GRAPHIFY_OPENAI_MODEL
if ([string]::IsNullOrWhiteSpace($env:GRAPHIFY_MAX_RETRIES)) {
  $env:GRAPHIFY_MAX_RETRIES = '1'
}

function Resolve-PositiveInteger {
  param([string]$RawValue, [int]$DefaultValue, [string]$SettingName)

  if ([string]::IsNullOrWhiteSpace($RawValue)) {
    return $DefaultValue
  }
  $parsed = 0
  if (![int]::TryParse($RawValue, [ref]$parsed) -or $parsed -le 0) {
    throw "$SettingName must be a positive integer."
  }
  return $parsed
}

$tokenBudget = Resolve-PositiveInteger $env:GRAPHIFY_OPENAI_TOKEN_BUDGET 16000 'GRAPHIFY_OPENAI_TOKEN_BUDGET'
$maxConcurrency = Resolve-PositiveInteger $env:GRAPHIFY_OPENAI_MAX_CONCURRENCY 2 'GRAPHIFY_OPENAI_MAX_CONCURRENCY'
$apiTimeoutSeconds = Resolve-PositiveInteger $env:GRAPHIFY_OPENAI_API_TIMEOUT 300 'GRAPHIFY_OPENAI_API_TIMEOUT'

$tag = "ai-memory-$SourceId"
if ([string]::IsNullOrWhiteSpace($OutRoot)) {
  $OutRoot = Join-Path $CorporaRoot $tag
}

Write-Host "=== $tag ===" -ForegroundColor Cyan
$graphifyArgs = @(
  'extract',
  $MemoryRoot,
  '--backend', 'openai',
  '--model', $env:GRAPHIFY_OPENAI_MODEL,
  '--out', $OutRoot,
  '--max-concurrency', "$maxConcurrency",
  '--token-budget', "$tokenBudget",
  '--api-timeout', "$apiTimeoutSeconds",
  '--exclude', '*.png',
  '--exclude', '*.jpg',
  '--exclude', '*.jpeg',
  '--exclude', '*.gif',
  '--exclude', '*.svg',
  '--exclude', '*.html',
  '--exclude', '*.json',
  '--exclude', '.obsidian/**',
  # Match the MCP index exclusions for every configured source.
  '--exclude', '**/Restricted/**',
  '--exclude', '**/.trash/**',
  '--exclude', 'References/Restricted/**',
  '--exclude', '*.zip'
)
if (!$SkipGlobal) {
  $graphifyArgs += @('--global', '--as', $tag)
}

& $graphifyExecutable @graphifyArgs
if ($LASTEXITCODE -ne 0) {
  throw 'graphify extract failed for ai-memory'
}

if (!$SkipGlobal) {
  Write-Host ''
  & $graphifyExecutable global list
}
