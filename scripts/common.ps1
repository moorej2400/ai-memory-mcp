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

function Get-AiMemorySources {
  $primaryRoot = $env:AI_MEMORY_WORK_DIR
  if ([string]::IsNullOrWhiteSpace($primaryRoot)) {
    $primaryRoot = $env:AI_MEMORY_DIR
  }
  if ([string]::IsNullOrWhiteSpace($primaryRoot)) {
    throw 'AI_MEMORY_WORK_DIR is not set.'
  }

  $primaryId = if ([string]::IsNullOrWhiteSpace($env:AI_MEMORY_PRIMARY_SOURCE_ID)) {
    'core'
  } else {
    $env:AI_MEMORY_PRIMARY_SOURCE_ID.Trim().ToLowerInvariant()
  }
  $configured = [ordered]@{
    $primaryId = $primaryRoot
  }

  if (![string]::IsNullOrWhiteSpace($env:AI_MEMORY_RETRIEVAL_SOURCES)) {
    try {
      $additional = $env:AI_MEMORY_RETRIEVAL_SOURCES | ConvertFrom-Json
    } catch {
      throw 'AI_MEMORY_RETRIEVAL_SOURCES must be a JSON object.'
    }
    foreach ($property in $additional.PSObject.Properties) {
      $sourceId = $property.Name.ToLowerInvariant()
      if ($configured.Contains($sourceId)) {
        throw "Duplicate memory source ID: $sourceId"
      }
      $configured[$sourceId] = [string]$property.Value
    }
  }
  if (![string]::IsNullOrWhiteSpace($env:AI_MEMORY_PERSONAL_DIR) -and !$configured.Contains('personal')) {
    $configured['personal'] = $env:AI_MEMORY_PERSONAL_DIR
  }

  $sources = @()
  $seenRoots = @{}
  foreach ($entry in $configured.GetEnumerator()) {
    if ($entry.Key -notmatch '^[a-z][a-z0-9-]{0,62}$') {
      throw "Invalid memory source ID: $($entry.Key)"
    }
    $resolved = (Resolve-Path -LiteralPath ([Environment]::ExpandEnvironmentVariables($entry.Value))).Path
    $rootKey = $resolved.ToLowerInvariant()
    if ($seenRoots.ContainsKey($rootKey)) {
      throw "Duplicate memory source directory: $resolved"
    }
    $seenRoots[$rootKey] = $true
    $sources += [pscustomobject]@{
      SourceId = $entry.Key
      Root = $resolved
      Writable = $entry.Key -eq $primaryId
    }
  }
  return $sources
}
