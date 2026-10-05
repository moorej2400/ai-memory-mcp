<#
  .SYNOPSIS
    Windows entry point for retiring the Graphify services of earlier releases.

  .DESCRIPTION
    Delegates to retire_graphify_memory.py, the shared cross-platform
    implementation. Without -Apply, the script reports and changes nothing.
#>
param(
  [switch]$Apply
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$arguments = @()
if ($Apply) { $arguments += '--apply' }

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'retire_graphify_memory.py') -Arguments $arguments
