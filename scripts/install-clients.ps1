<#
  .SYNOPSIS
    Windows entry point for registering AI Memory MCP with supported clients.

  .DESCRIPTION
    Delegates to install_clients.py, the shared cross-platform implementation.
#>
param(
  [string]$RepositoryRoot = '',
  [ValidateSet('Codex', 'ClaudeCode', 'ClaudeDesktop', 'Copilot', 'OpenCode', 'VSCode', 'AgentSkills')]
  [string[]]$Clients = @('Codex', 'ClaudeCode', 'ClaudeDesktop', 'Copilot', 'OpenCode', 'VSCode', 'AgentSkills')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$clientMap = @{
  Codex         = 'codex'
  ClaudeCode    = 'claude-code'
  ClaudeDesktop = 'claude-desktop'
  Copilot       = 'copilot'
  OpenCode      = 'opencode'
  VSCode        = 'vscode'
  AgentSkills   = 'agent-skills'
}

$arguments = @()
if (![string]::IsNullOrWhiteSpace($RepositoryRoot)) {
  $arguments += @('--repository-root', $RepositoryRoot)
}
foreach ($client in $Clients) {
  $arguments += @('--client', $clientMap[$client])
}

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'install_clients.py') -Arguments $arguments
