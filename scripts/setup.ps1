param(
  [Parameter(Mandatory = $true)][string]$MemoryRoot,
  [switch]$InstallCodex,
  [switch]$InstallClients,
  [switch]$SkipGraphifyRuntime
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$repositoryRoot = Get-AiMemoryRepositoryRoot
$resolvedMemoryRoot = (Resolve-Path -LiteralPath $MemoryRoot).Path
$minimumPython = [version]'3.11'

$launcher = $null
$launcherArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
  $launcher = 'py'
  $launcherArgs = @('-3')
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $launcher = 'python'
}
else {
  throw 'Python 3.11 or newer was not found on PATH.'
}

$versionText = & $launcher @launcherArgs -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or [version]$versionText -lt $minimumPython) {
  throw "Python $minimumPython or newer is required; found $versionText."
}

$appPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $appPython)) {
  & $launcher @launcherArgs -m venv (Join-Path $repositoryRoot '.venv')
  if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the application virtual environment.'
  }
}

& $appPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
  throw 'Failed to update pip in the application environment.'
}
& $appPython -m pip install -e "$repositoryRoot[dev]"
if ($LASTEXITCODE -ne 0) {
  throw 'Failed to install AI Memory MCP.'
}

if (!$SkipGraphifyRuntime) {
  $graphifyPython = Join-Path $repositoryRoot '.graphify-runtime\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $graphifyPython)) {
    & $launcher @launcherArgs -m venv (Join-Path $repositoryRoot '.graphify-runtime')
    if ($LASTEXITCODE -ne 0) {
      throw 'Failed to create the Graphify virtual environment.'
    }
  }
  & $graphifyPython -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    throw 'Failed to update pip in the Graphify environment.'
  }
  & $graphifyPython -m pip install -r (Join-Path $repositoryRoot 'requirements-graphify.txt')
  if ($LASTEXITCODE -ne 0) {
    throw 'Failed to install the pinned Graphify runtime.'
  }
}

$envPath = Join-Path $repositoryRoot '.env'
if (!(Test-Path -LiteralPath $envPath)) {
  $homePath = $HOME.Replace('\', '/')
  $memoryPath = $resolvedMemoryRoot.Replace('\', '/')
  $configuration = @"
AI_MEMORY_WORK_DIR="$memoryPath"
AI_MEMORY_PRIMARY_SOURCE_ID="core"
AI_MEMORY_RETRIEVAL_SOURCES="{}"
AI_MEMORY_MCP_STATE_DIR="$homePath/.ai-memory-mcp"
AI_MEMORY_GRAPHIFY_STATE_DIR="$homePath/.graphify"
AI_MEMORY_GRAPH_PATH="$homePath/.graphify/corpora/ai-memory/graphify-out/graph.json"
GRAPHIFY_GLOBAL_MCP_URL="http://127.0.0.1:4324/mcp"
GRAPHIFY_OPENAI_BASE_URL=""
GRAPHIFY_OPENAI_API_KEY=""
GRAPHIFY_OPENAI_MODEL=""
GRAPHIFY_OPENAI_TOKEN_BUDGET="30000"
GRAPHIFY_OPENAI_MAX_CONCURRENCY="1"
GRAPHIFY_OPENAI_API_TIMEOUT="300"
GRAPHIFY_MAX_RETRIES="1"
"@
  [System.IO.File]::WriteAllText($envPath, $configuration, [System.Text.UTF8Encoding]::new($false))
  Write-Host "Created local configuration: $envPath" -ForegroundColor Green
}
else {
  Write-Host "Kept existing local configuration: $envPath" -ForegroundColor Yellow
}

& $appPython -m ai_memory_mcp.cli
if ($LASTEXITCODE -ne 0) {
  throw 'The initial AI Memory index build failed.'
}

if ($InstallClients) {
  & (Join-Path $PSScriptRoot 'install-clients.ps1') -RepositoryRoot $repositoryRoot
}
elseif ($InstallCodex) {
  & (Join-Path $PSScriptRoot 'install-codex.ps1') -RepositoryRoot $repositoryRoot
}

Write-Host "AI Memory MCP is ready at $repositoryRoot" -ForegroundColor Green
