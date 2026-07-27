$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')

$repositoryRoot = Get-AiMemoryRepositoryRoot
Import-AiMemoryEnvironment -RepositoryRoot $repositoryRoot
$endpoint = if ([string]::IsNullOrWhiteSpace($env:GRAPHIFY_GLOBAL_MCP_URL)) {
  [uri]'http://127.0.0.1:4324/mcp'
} else {
  [uri]$env:GRAPHIFY_GLOBAL_MCP_URL
}
$port = $endpoint.Port

Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*graphify-mcp*' -and $_.CommandLine -like "*--port $port*" } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped PID $($_.ProcessId)"
  }

