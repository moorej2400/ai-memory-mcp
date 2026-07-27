$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')

$repositoryRoot = Get-AiMemoryRepositoryRoot
Import-AiMemoryEnvironment -RepositoryRoot $repositoryRoot
$graphifyRoot = Get-AiMemoryGraphifyStateRoot
$GraphPath = Join-Path $graphifyRoot 'global-graph.json'
$LogDir = Join-Path $graphifyRoot 'logs'
$Exe = Get-AiMemoryGraphifyMcpExecutable -RepositoryRoot $repositoryRoot
if (!(Test-Path -LiteralPath $Exe)) {
  throw "Pinned Graphify MCP executable was not found at $Exe. Run scripts\setup.ps1."
}
$endpoint = if ([string]::IsNullOrWhiteSpace($env:GRAPHIFY_GLOBAL_MCP_URL)) {
  [uri]'http://127.0.0.1:4324/mcp'
} else {
  [uri]$env:GRAPHIFY_GLOBAL_MCP_URL
}
$Port = $endpoint.Port
$MountPath = $endpoint.AbsolutePath
$StdOut = Join-Path $LogDir 'graphify-global-mcp.out.log'
$StdErr = Join-Path $LogDir 'graphify-global-mcp.err.log'
$StartupDeadlineSeconds = 45

if (!(Test-Path -LiteralPath $GraphPath)) {
  throw "Global graph not found at $GraphPath. Run extract-global-memory.ps1 first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Stop-ProcessTree {
  param(
    [Parameter(Mandatory = $true)][int]$ProcessId
  )

  $children = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ParentProcessId -eq $ProcessId }

  foreach ($child in $children) {
    Stop-ProcessTree -ProcessId $child.ProcessId
  }

  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-DescendantProcessIds {
  param(
    [Parameter(Mandatory = $true)][int]$RootProcessId
  )

  $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
  $ids = New-Object System.Collections.Generic.HashSet[int]
  $queue = New-Object System.Collections.Generic.Queue[int]
  $queue.Enqueue($RootProcessId)
  [void]$ids.Add($RootProcessId)

  while ($queue.Count -gt 0) {
    $current = $queue.Dequeue()
    foreach ($child in ($all | Where-Object { $_.ParentProcessId -eq $current })) {
      if ($ids.Add($child.ProcessId)) {
        $queue.Enqueue($child.ProcessId)
      }
    }
  }

  return @($ids)
}

# Stop only previous Graphify MCP instances for this exact shared-memory port so
# restarts cannot leave a stale listener behind and make the readiness check lie.
$existing = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*graphify-mcp*' -and $_.CommandLine -like "*--port $Port*" }

foreach ($process in $existing) {
  # The Windows launcher can leave Python children behind unless the whole tree
  # is torn down before the next bind attempt.
  Stop-ProcessTree -ProcessId $process.ProcessId
}

$proc = Start-Process `
  -FilePath $Exe `
  -ArgumentList @('--graph', $GraphPath, '--transport', 'http', '--host', $endpoint.Host, '--port', "$Port", '--path', $MountPath) `
  -WindowStyle Hidden `
  -RedirectStandardOutput $StdOut `
  -RedirectStandardError $StdErr `
  -PassThru

# graphify-mcp sometimes needs longer than two seconds to bind on Windows,
# especially right after a forced restart when the prior process is still unwinding.
$deadline = (Get-Date).AddSeconds($StartupDeadlineSeconds)
$tcp = $null

do {
  Start-Sleep -Milliseconds 500
  $candidatePids = Get-DescendantProcessIds -RootProcessId $proc.Id
  $tcp = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $candidatePids -contains $_.OwningProcess }
} while (-not $tcp -and (Get-Date) -lt $deadline)

if (-not $tcp) {
  $stderrTail = if (Test-Path -LiteralPath $StdErr) {
    (Get-Content -LiteralPath $StdErr -Tail 40 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
  } else {
    ''
  }

  throw "Graphify MCP did not start listening on port $Port within $StartupDeadlineSeconds seconds. Check $StdErr`n$stderrTail"
}

Write-Host "graphify-global-mcp started (PID $($proc.Id)) on $($endpoint.AbsoluteUri)"

