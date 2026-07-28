# Operations

This guide gives the common operating procedures.

## Update the local index

Run this command after a normal Markdown change:

```powershell
.\.venv\Scripts\ai-memory-index.exe
```

The command reads the canonical Markdown files.
The command publishes a new SQLite index snapshot.
The command does not change the Markdown files.

## Run the MCP server

Use the standard input and output transport for local agents:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport stdio
```

Use the HTTP transport when a local client needs an endpoint:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport streamable-http
```

The default HTTP endpoint is `http://127.0.0.1:4334/mcp`.
The server rejects non-loopback hosts because this transport has no authentication.

## Update client registrations

Run this command after you move the repository:

```powershell
.\scripts\install-clients.ps1
```

The installer updates each command path.
The installer preserves the previous configuration in a timestamped backup.
Restart each configured client after the command finishes.

## Refresh Graphify

Use `memory_sync` after an ordinary memory update.
The tool updates only the derived SQLite index.

Use the maintenance script when the Graphify graph must change.
The script uses staging and validation.
The script keeps the last satisfactory graph if validation fails.

Run this script for Graphify maintenance:

```powershell
.\scripts\graphify\refresh-ai-memory-graph.ps1
```

## Control the Graphify MCP service

Start the local Graphify service:

```powershell
.\scripts\graphify\start-graphify-global-mcp.ps1
```

Stop the local Graphify service:

```powershell
.\scripts\graphify\stop-graphify-global-mcp.ps1
```

Install the logon launcher:

```powershell
.\scripts\graphify\install-graphify-global-mcp-startup.ps1
```

## Check health

Call `memory_status` from an MCP client.
Check the canonical root, index, graph, package, and MCP version fields.

A saved Markdown file can exist before its derived indexes change.
Report the Markdown and index results as separate results.
