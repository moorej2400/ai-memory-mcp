<#
  .SYNOPSIS
    Windows entry point for starting the Graphify global MCP listener.

  .DESCRIPTION
    Delegates to start_global_mcp.py, the shared cross-platform implementation.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path $PSScriptRoot '..') 'common.ps1')

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'start_global_mcp.py')
