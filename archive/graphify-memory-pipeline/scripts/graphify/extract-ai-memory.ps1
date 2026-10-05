<#
  .SYNOPSIS
    Windows entry point for extracting a Graphify corpus from an AI Memory source.

  .DESCRIPTION
    Delegates to extract_ai_memory.py, the shared cross-platform implementation.
#>
param(
  [string]$OutRoot = '',
  [string]$MemoryRoot = '',
  [string]$SourceId = '',
  [switch]$SkipGlobal
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path $PSScriptRoot '..') 'common.ps1')

$arguments = @()
if (![string]::IsNullOrWhiteSpace($OutRoot)) { $arguments += @('--out-root', $OutRoot) }
if (![string]::IsNullOrWhiteSpace($MemoryRoot)) { $arguments += @('--memory-root', $MemoryRoot) }
if (![string]::IsNullOrWhiteSpace($SourceId)) { $arguments += @('--source-id', $SourceId) }
if ($SkipGlobal) { $arguments += '--skip-global' }

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'extract_ai_memory.py') -Arguments $arguments
