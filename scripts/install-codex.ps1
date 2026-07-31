<#
  .SYNOPSIS
    Windows entry point for registering AI Memory MCP with Codex.

  .DESCRIPTION
    Delegates to install_codex.py, the shared cross-platform implementation.
#>
param(
  [string]$RepositoryRoot = '',
  [string]$CodexHome = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$arguments = @()
if (![string]::IsNullOrWhiteSpace($RepositoryRoot)) {
  $arguments += @('--repository-root', $RepositoryRoot)
}
if (![string]::IsNullOrWhiteSpace($CodexHome)) {
  $arguments += @('--codex-home', $CodexHome)
}

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'install_codex.py') -Arguments $arguments
