param(
  [string]$SeedCorpusOut = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')

$repositoryRoot = Get-AiMemoryRepositoryRoot
Import-AiMemoryEnvironment -RepositoryRoot $repositoryRoot
$graphifyRoot = Get-AiMemoryGraphifyStateRoot
$servicesRoot = $PSScriptRoot
$runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stageRoot = Join-Path $graphifyRoot "staging\ai-memory\$runId"
$backupRoot = Join-Path $graphifyRoot "backups\ai-memory\$runId"
$failedRoot = Join-Path $backupRoot 'failed-publication'
$logRoot = Join-Path $graphifyRoot 'logs\ai-memory-refresh'
$statePath = Join-Path $backupRoot 'refresh-state.json'
$liveCorpusOut = Join-Path $graphifyRoot 'corpora\ai-memory\graphify-out'
$liveGlobalGraph = Join-Path $graphifyRoot 'global-graph.json'
$liveGlobalManifest = Join-Path $graphifyRoot 'global-manifest.json'
$corpusBackup = Join-Path $backupRoot 'corpus-graphify-out'
$globalGraphBackup = Join-Path $backupRoot 'global-graph.json'
$globalManifestBackup = Join-Path $backupRoot 'global-manifest.json'
$stageCorpusRoot = Join-Path $stageRoot 'corpus'
$stageCorpusOut = Join-Path $stageCorpusRoot 'graphify-out'
$stageSourcesRoot = Join-Path $stageCorpusOut 'sources'
$stageGlobalRoot = Join-Path $stageRoot 'global'
$toolPython = Get-AiMemoryGraphifyPython -RepositoryRoot $repositoryRoot
if (!(Test-Path -LiteralPath $toolPython)) {
  throw "Pinned Graphify Python was not found at $toolPython. Run scripts\setup.ps1."
}
$mcpUrl = if ([string]::IsNullOrWhiteSpace($env:GRAPHIFY_GLOBAL_MCP_URL)) {
  'http://127.0.0.1:4324/mcp'
} else {
  $env:GRAPHIFY_GLOBAL_MCP_URL
}
$mutex = [System.Threading.Mutex]::new($false, 'Global\GraphifyAiMemoryRefresh')
$hasMutex = $false
$transcribing = $false

function Write-State {
  $script:state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Move-Recoverably {
  param([string]$Source, [string]$Destination)

  if (!(Test-Path -LiteralPath $Source)) {
    return
  }
  if (Test-Path -LiteralPath $Destination) {
    throw "Recovery destination already exists: $Destination"
  }
  $parent = Split-Path -Parent $Destination
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  Move-Item -LiteralPath $Source -Destination $Destination
}

function Restore-LastKnownGood {
  New-Item -ItemType Directory -Force -Path $failedRoot | Out-Null

  if (Test-Path -LiteralPath $globalGraphBackup) {
    Move-Recoverably $liveGlobalGraph (Join-Path $failedRoot 'global-graph.failed.json')
    Move-Recoverably $globalGraphBackup $liveGlobalGraph
  }
  if (Test-Path -LiteralPath $globalManifestBackup) {
    Move-Recoverably $liveGlobalManifest (Join-Path $failedRoot 'global-manifest.failed.json')
    Move-Recoverably $globalManifestBackup $liveGlobalManifest
  }
  if (Test-Path -LiteralPath $corpusBackup) {
    Move-Recoverably $liveCorpusOut (Join-Path $failedRoot 'corpus-graphify-out')
    Move-Recoverably $corpusBackup $liveCorpusOut
  }

  $script:state.phase = 'rolled-back'
  $script:state.rolledBackAt = (Get-Date).ToString('o')
  Write-State
}

try {
  try {
    $hasMutex = $mutex.WaitOne(0)
  } catch [System.Threading.AbandonedMutexException] {
    $hasMutex = $true
  }
  if (!$hasMutex) {
    throw 'An AI-Memory Graphify refresh is already running.'
  }

  New-Item -ItemType Directory -Force -Path $stageSourcesRoot, $stageGlobalRoot, $backupRoot, $logRoot | Out-Null
  $logPath = Join-Path $logRoot "ai-memory-refresh-$runId.log"
  Start-Transcript -LiteralPath $logPath | Out-Null
  $transcribing = $true

  $priorCorpusNodes = 0
  if (Test-Path -LiteralPath (Join-Path $liveCorpusOut 'graph.json')) {
    $priorCorpus = Get-Content -LiteralPath (Join-Path $liveCorpusOut 'graph.json') -Raw | ConvertFrom-Json
    $priorCorpusNodes = @($priorCorpus.nodes).Count
  }

  $script:state = [ordered]@{
    runId = $runId
    phase = 'staging'
    startedAt = (Get-Date).ToString('o')
    priorCorpusNodes = $priorCorpusNodes
    liveCorpusOut = $liveCorpusOut
    liveGlobalGraph = $liveGlobalGraph
    liveGlobalManifest = $liveGlobalManifest
    corpusBackup = $corpusBackup
    globalGraphBackup = $globalGraphBackup
    globalManifestBackup = $globalManifestBackup
    logPath = $logPath
  }
  Write-State

  $corpusSeed = $liveCorpusOut
  if (![string]::IsNullOrWhiteSpace($SeedCorpusOut)) {
    $corpusSeed = (Resolve-Path -LiteralPath $SeedCorpusOut).Path
    foreach ($requiredSeedFile in @('graph.json', 'manifest.json')) {
      if (!(Test-Path -LiteralPath (Join-Path $corpusSeed $requiredSeedFile))) {
        throw "Seed corpus is missing $requiredSeedFile at $corpusSeed"
      }
    }
  }
  $sources = @(Get-AiMemorySources)
  $seedSources = Join-Path $corpusSeed 'sources'
  if (Test-Path -LiteralPath $seedSources) {
    foreach ($source in $sources) {
      $seed = Join-Path $seedSources "$($source.SourceId)\graphify-out"
      if (Test-Path -LiteralPath $seed) {
        $destination = Join-Path $stageSourcesRoot "$($source.SourceId)\graphify-out"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $seed -Destination $destination -Recurse
      }
    }
  } elseif ($sources.Count -eq 1 -and (Test-Path -LiteralPath $corpusSeed)) {
    # A one-source legacy corpus can seed the first named-source refresh.
    $destination = Join-Path $stageSourcesRoot "$($sources[0].SourceId)\graphify-out"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $corpusSeed -Destination $destination -Recurse
  }

  $sourceGraphs = @()
  foreach ($source in $sources) {
    $sourceRoot = Join-Path $stageSourcesRoot $source.SourceId
    & (Join-Path $servicesRoot 'extract-ai-memory.ps1') `
      -OutRoot $sourceRoot `
      -MemoryRoot $source.Root `
      -SourceId $source.SourceId `
      -SkipGlobal
    if ($LASTEXITCODE -ne 0) {
      throw "AI-Memory extraction failed for source '$($source.SourceId)'."
    }
    $sourceGraphs += [pscustomobject]@{
      SourceId = $source.SourceId
      Graph = Join-Path $sourceRoot 'graphify-out\graph.json'
    }
  }

  $mergeArguments = @(
    (Join-Path $servicesRoot 'merge-memory-source-graphs.py'),
    '--output-dir',
    $stageCorpusOut
  )
  foreach ($sourceGraph in $sourceGraphs) {
    $mergeArguments += @(
      '--source',
      "$($sourceGraph.SourceId)=$($sourceGraph.Graph)"
    )
  }
  & $toolPython @mergeArguments
  if ($LASTEXITCODE -ne 0) {
    throw 'AI-Memory source graph merge failed.'
  }

  & $toolPython (Join-Path $servicesRoot 'validate-ai-memory-graph.py') `
    --corpus (Join-Path $stageCorpusOut 'graph.json') `
    --manifest (Join-Path $stageCorpusOut 'manifest.json') `
    --prior-corpus-nodes "$priorCorpusNodes"
  if ($LASTEXITCODE -ne 0) {
    throw 'Staged corpus validation failed.'
  }

  $script:state.phase = 'publishing-corpus'
  Write-State
  Move-Recoverably $liveCorpusOut $corpusBackup
  Move-Recoverably $stageCorpusOut $liveCorpusOut

  if (Test-Path -LiteralPath $liveGlobalGraph) {
    Copy-Item -LiteralPath $liveGlobalGraph -Destination (Join-Path $stageGlobalRoot 'global-graph.json')
  }
  if (Test-Path -LiteralPath $liveGlobalManifest) {
    Copy-Item -LiteralPath $liveGlobalManifest -Destination (Join-Path $stageGlobalRoot 'global-manifest.json')
  }

  & $toolPython (Join-Path $servicesRoot 'publish-ai-memory-global.py') `
    --source (Join-Path $liveCorpusOut 'graph.json') `
    --stage-dir $stageGlobalRoot
  if ($LASTEXITCODE -ne 0) {
    throw 'Global graph staging failed.'
  }

  & $toolPython (Join-Path $servicesRoot 'validate-ai-memory-graph.py') `
    --corpus (Join-Path $liveCorpusOut 'graph.json') `
    --manifest (Join-Path $liveCorpusOut 'manifest.json') `
    --global-graph (Join-Path $stageGlobalRoot 'global-graph.json') `
    --prior-corpus-nodes "$priorCorpusNodes"
  if ($LASTEXITCODE -ne 0) {
    throw 'Staged global graph validation failed.'
  }

  $script:state.phase = 'publishing-global'
  Write-State
  Move-Recoverably $liveGlobalGraph $globalGraphBackup
  Move-Recoverably $liveGlobalManifest $globalManifestBackup
  Move-Recoverably (Join-Path $stageGlobalRoot 'global-graph.json') $liveGlobalGraph
  Move-Recoverably (Join-Path $stageGlobalRoot 'global-manifest.json') $liveGlobalManifest

  $script:state.phase = 'health-check'
  Write-State
  & (Join-Path $servicesRoot 'start-graphify-global-mcp.ps1')
  if ($LASTEXITCODE -ne 0) {
    throw 'Graphify global MCP restart failed after publication.'
  }

  $healthPath = Join-Path $backupRoot 'health-result.json'
  & $toolPython (Join-Path $servicesRoot 'validate-ai-memory-graph.py') `
    --corpus (Join-Path $liveCorpusOut 'graph.json') `
    --manifest (Join-Path $liveCorpusOut 'manifest.json') `
    --global-graph $liveGlobalGraph `
    --prior-corpus-nodes "$priorCorpusNodes" `
    --mcp-url $mcpUrl `
    --output $healthPath
  if ($LASTEXITCODE -ne 0) {
    throw 'Post-refresh health gate failed.'
  }

  & $toolPython (Join-Path $servicesRoot 'run-ai-memory-retrieval-eval.py')
  if ($LASTEXITCODE -ne 0) {
    throw 'AI-Memory retrieval regression suite failed.'
  }

  $script:state.phase = 'complete'
  $script:state.completedAt = (Get-Date).ToString('o')
  $script:state.healthPath = $healthPath
  Write-State
  Write-Host "AI-Memory graph refresh complete. Recovery snapshot: $backupRoot" -ForegroundColor Green
} catch {
  $failure = $_
  if ($null -ne $script:state) {
    $script:state.failure = $failure.Exception.Message
    $script:state.failedAt = (Get-Date).ToString('o')
    Write-State
    Restore-LastKnownGood
    try {
      & (Join-Path $servicesRoot 'start-graphify-global-mcp.ps1')
    } catch {
      Write-Warning "Rollback completed, but MCP restart failed: $($_.Exception.Message)"
    }
  }
  throw $failure
} finally {
  if ($transcribing) {
    Stop-Transcript | Out-Null
  }
  if ($hasMutex) {
    $mutex.ReleaseMutex()
  }
  $mutex.Dispose()
}
