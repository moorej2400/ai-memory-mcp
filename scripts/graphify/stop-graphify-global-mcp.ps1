<#
  .SYNOPSIS
    Windows entry point for stopping the Graphify global MCP listener.

  .DESCRIPTION
    Delegates to stop_global_mcp.py, the shared cross-platform implementation.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path $PSScriptRoot '..') 'common.ps1')

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'stop_global_mcp.py')
