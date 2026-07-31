$ErrorActionPreference = 'Stop'

function Get-AiMemoryRepositoryRoot {
  return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Get-AiMemoryLauncherPython {
  <#
    .SYNOPSIS
      Resolve the interpreter used to run the cross-platform Python scripts.

    .DESCRIPTION
      The provisioned application environment is preferred so the scripts run
      against the same interpreter the MCP server uses. Before setup has run
      that environment does not exist yet, so a system interpreter is used to
      bootstrap it.
  #>
  param(
    [string]$RepositoryRoot = ''
  )

  if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Get-AiMemoryRepositoryRoot
  }

  $candidates = @(
    (Join-Path $RepositoryRoot '.venv/Scripts/python.exe'),
    (Join-Path $RepositoryRoot '.venv/bin/python')
  )
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
      return @($candidate)
    }
  }

  if (Get-Command py -ErrorAction SilentlyContinue) {
    return @('py', '-3')
  }
  foreach ($name in @('python3', 'python')) {
    if (Get-Command $name -ErrorAction SilentlyContinue) {
      return @($name)
    }
  }

  throw 'Python 3.11 or newer was not found on PATH.'
}

function Invoke-AiMemoryPythonScript {
  <#
    .SYNOPSIS
      Run one of the cross-platform Python scripts and propagate its exit code.
  #>
  param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string[]]$Arguments = @()
  )

  $launcher = @(Get-AiMemoryLauncherPython)
  $executable = $launcher[0]
  $prefix = @($launcher | Select-Object -Skip 1)

  & $executable @prefix $Script @Arguments
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}
