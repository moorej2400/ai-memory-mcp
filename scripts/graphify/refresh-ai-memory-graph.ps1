<#
  .SYNOPSIS
    Windows entry point for refreshing the AI Memory graph.

  .DESCRIPTION
    Delegates to refresh_graph.py, the shared cross-platform implementation.
#>
param(
  [string]$SeedCorpusOut = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path $PSScriptRoot '..') 'common.ps1')

$arguments = @()
if (![string]::IsNullOrWhiteSpace($SeedCorpusOut)) {
  $arguments += @('--seed-corpus-out', $SeedCorpusOut)
}

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'refresh_graph.py') -Arguments $arguments
