param(
  [ValidateSet('Build', 'Update', 'Query', 'Path', 'Explain')]
  [string]$Mode = 'Build',
  [string]$Path = '.',
  [string]$Question = '',
  [string]$Target = ''
)

$ErrorActionPreference = 'Stop'

$graphify = Get-Command graphify -ErrorAction SilentlyContinue
if ($null -eq $graphify) {
  throw 'Graphify is not installed or is not available on PATH.'
}

$repository = (Resolve-Path -LiteralPath $Path).Path
$insideWorkTree = & git -C $repository rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -ne 0 -or $insideWorkTree -ne 'true') {
  throw "The target is not a Git repository: $repository"
}

Push-Location $repository
try {
  switch ($Mode) {
    'Build' {
      & $graphify.Source extract $repository
    }
    'Update' {
      & $graphify.Source update $repository
    }
    'Query' {
      if ([string]::IsNullOrWhiteSpace($Question)) {
        throw 'Question is required for Query mode.'
      }
      & $graphify.Source query $Question
    }
    'Path' {
      if ([string]::IsNullOrWhiteSpace($Question) -or [string]::IsNullOrWhiteSpace($Target)) {
        throw 'Question and Target are required for Path mode.'
      }
      & $graphify.Source path $Question $Target
    }
    'Explain' {
      if ([string]::IsNullOrWhiteSpace($Question)) {
        throw 'Question is required for Explain mode.'
      }
      & $graphify.Source explain $Question
    }
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Graphify failed in $Mode mode."
  }
}
finally {
  Pop-Location
}
