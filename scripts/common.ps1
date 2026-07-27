$ErrorActionPreference = 'Stop'

function Get-AiMemoryRepositoryRoot {
  return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Import-AiMemoryEnvironment {
  param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot
  )

  $envPath = Join-Path $RepositoryRoot '.env'
  if (!(Test-Path -LiteralPath $envPath)) {
    return
  }

  foreach ($rawLine in Get-Content -LiteralPath $envPath) {
    $line = $rawLine.Trim()
    if (!$line -or $line.StartsWith('#') -or !$line.Contains('=')) {
      continue
    }
    $name, $value = $line.Split('=', 2)
    $name = $name.Trim()
    $value = $value.Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    # Launch-time overrides are intentional and must win over repository config.
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
      [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
  }
}

function Get-AiMemoryGraphifyStateRoot {
  if (![string]::IsNullOrWhiteSpace($env:AI_MEMORY_GRAPHIFY_STATE_DIR)) {
    return [Environment]::ExpandEnvironmentVariables($env:AI_MEMORY_GRAPHIFY_STATE_DIR)
  }
  return Join-Path $HOME '.graphify'
}

function Get-AiMemoryGraphifyPython {
  param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot
  )

  if (![string]::IsNullOrWhiteSpace($env:AI_MEMORY_GRAPHIFY_PYTHON)) {
    return $env:AI_MEMORY_GRAPHIFY_PYTHON
  }
  return Join-Path $RepositoryRoot '.graphify-runtime\Scripts\python.exe'
}

function Get-AiMemoryGraphifyExecutable {
  param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot
  )

  return Join-Path $RepositoryRoot '.graphify-runtime\Scripts\graphify.exe'
}

function Get-AiMemoryGraphifyMcpExecutable {
  param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot
  )

  if (![string]::IsNullOrWhiteSpace($env:AI_MEMORY_GRAPHIFY_MCP_EXE)) {
    return $env:AI_MEMORY_GRAPHIFY_MCP_EXE
  }
  return Join-Path $RepositoryRoot '.graphify-runtime\Scripts\graphify-mcp.exe'
}

