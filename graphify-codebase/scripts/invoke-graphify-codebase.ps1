<#
  .SYNOPSIS
    Windows entry point for driving Graphify against a code repository.

  .DESCRIPTION
    Delegates to invoke_graphify_codebase.py, the shared cross-platform
    implementation.
#>
param(
  [ValidateSet('Build', 'Update', 'Query', 'Path', 'Explain')]
  [string]$Mode = 'Build',
  [string]$Path = '.',
  [string]$Question = '',
  [string]$Target = ''
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..' )).Path
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $repositoryRoot '..')).Path

$candidates = @(
  (Join-Path $repositoryRoot '.venv/Scripts/python.exe'),
  (Join-Path $repositoryRoot '.venv/bin/python')
)
$python = $null
foreach ($candidate in $candidates) {
  if (Test-Path -LiteralPath $candidate) { $python = $candidate; break }
}
if ($null -eq $python) {
  foreach ($name in @('py', 'python3', 'python')) {
    if (Get-Command $name -ErrorAction SilentlyContinue) { $python = $name; break }
  }
}
if ($null -eq $python) {
  throw 'Python 3.11 or newer was not found on PATH.'
}

$arguments = @(
  (Join-Path $PSScriptRoot 'invoke_graphify_codebase.py'),
  '--mode', $Mode.ToLowerInvariant(),
  '--path', $Path
)
if (![string]::IsNullOrWhiteSpace($Question)) { $arguments += @('--question', $Question) }
if (![string]::IsNullOrWhiteSpace($Target)) { $arguments += @('--target', $Target) }

& $python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
