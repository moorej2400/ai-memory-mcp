<#
  .SYNOPSIS
    Windows entry point for provisioning AI Memory MCP.

  .DESCRIPTION
    The provisioning logic lives in setup.py so Windows, macOS, and Linux share
    one implementation. This wrapper only translates the PowerShell parameter
    style onto that script's command line.
#>
param(
  [Parameter(Mandatory = $true)][string]$MemoryRoot,
  [switch]$InstallCodex,
  [switch]$InstallClients,
  [switch]$SkipGraphifyRuntime
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$arguments = @('--memory-root', $MemoryRoot)
if ($InstallCodex) { $arguments += '--install-codex' }
if ($InstallClients) { $arguments += '--install-clients' }
if ($SkipGraphifyRuntime) { $arguments += '--skip-graphify-runtime' }

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'setup.py') -Arguments $arguments
