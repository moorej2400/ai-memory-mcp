param(
  [string]$RepositoryRoot = '',
  [ValidateSet('Codex', 'ClaudeCode', 'ClaudeDesktop', 'Copilot', 'OpenCode', 'VSCode', 'AgentSkills')]
  [string[]]$Clients = @('Codex', 'ClaudeCode', 'ClaudeDesktop', 'Copilot', 'OpenCode', 'VSCode', 'AgentSkills')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
  $RepositoryRoot = Get-AiMemoryRepositoryRoot
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
  throw "Application environment is missing: $python. Run scripts\setup.ps1."
}

if ($Clients -contains 'Codex') {
  & (Join-Path $PSScriptRoot 'install-codex.ps1') -RepositoryRoot $RepositoryRoot
}

$clientMap = @{
  ClaudeCode = 'claude-code'
  ClaudeDesktop = 'claude-desktop'
  Copilot = 'copilot'
  OpenCode = 'opencode'
  VSCode = 'vscode'
  AgentSkills = 'agent-skills'
}
$arguments = @('-m', 'ai_memory_mcp.client_install', '--repository-root', $RepositoryRoot)
foreach ($client in $Clients) {
  if ($clientMap.ContainsKey($client)) {
    $arguments += @('--client', $clientMap[$client])
  }
}

if ($arguments.Count -gt 4) {
  & $python @arguments
  if ($LASTEXITCODE -ne 0) {
    throw 'Failed to install one or more AI Memory client configurations.'
  }
}

Write-Host 'Restart each configured client to load AI Memory MCP and its skill.' -ForegroundColor Yellow
