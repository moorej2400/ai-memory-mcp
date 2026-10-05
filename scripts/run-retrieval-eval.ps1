<#
  .SYNOPSIS
    Windows entry point for the live AI Memory retrieval check.

  .DESCRIPTION
    Delegates to run_retrieval_eval.py, the shared cross-platform
    implementation.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'run_retrieval_eval.py')
