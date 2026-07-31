# Operations

This guide gives the common operating procedures.

Each procedure shows the PowerShell form first and the POSIX shell form second.
Both wrappers call the same cross-platform Python implementation, so either form
produces the same result on any supported platform.

The console scripts live in `.venv\Scripts` on Windows and `.venv/bin` on macOS
and Linux.

## Update the local index

Run this command after a normal Markdown change:

```powershell
.\.venv\Scripts\ai-memory-index.exe
```

```bash
./.venv/bin/ai-memory-index
```

The command reads Markdown from all configured memory sources.
The command publishes a new SQLite index snapshot.
The command does not change the Markdown files.

## Run the MCP server

Use the standard input and output transport for local agents:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport stdio
```

```bash
./.venv/bin/ai-memory-mcp --transport stdio
```

Use the HTTP transport when a local client needs an endpoint:

```powershell
.\.venv\Scripts\ai-memory-mcp.exe --transport streamable-http
```

```bash
./.venv/bin/ai-memory-mcp --transport streamable-http
```

The default HTTP endpoint is `http://127.0.0.1:4334/mcp`.
The server rejects non-loopback hosts because this transport has no authentication.

## Update client registrations

Run this command after you move the repository:

```powershell
.\scripts\install-clients.ps1
```

```bash
./scripts/install-clients.sh
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
The script extracts each configured memory source separately.
The script merges the source graphs into the AI Memory provider graph.

Run this script for Graphify maintenance:

```powershell
.\scripts\graphify\refresh-ai-memory-graph.ps1
```

```bash
./scripts/graphify/refresh-ai-memory-graph.sh
```

Only one refresh can publish at a time. The script takes an advisory file lock
for the run and reports a clear error if another refresh already holds it.

Every run writes a transcript to
`<graphify-state>/logs/ai-memory-refresh/ai-memory-refresh-<run-id>.log`. The
transcript records each step, its output, and — when a run fails — the failure
and the rollback, so it is the first place to look after an unsuccessful
refresh.

## Control the Graphify MCP service

Start the local Graphify service:

```powershell
.\scripts\graphify\start-graphify-global-mcp.ps1
```

```bash
./scripts/graphify/start-graphify-global-mcp.sh
```

The service is started detached, so it keeps running after the launcher exits.
The launcher waits for the port to accept connections before it reports success.

The launcher first stops any previous Graphify MCP bound to the same port. If
something else still holds that port it refuses to start, rather than reporting
success against a listener it does not own. Stop the other program, or point
`GRAPHIFY_GLOBAL_MCP_URL` at a free port.

Stop the local Graphify service:

```powershell
.\scripts\graphify\stop-graphify-global-mcp.ps1
```

```bash
./scripts/graphify/stop-graphify-global-mcp.sh
```

The stop procedure signals the process tree, then escalates only if a process
does not exit. It never targets this script or the shell that launched it.

## Install the login launcher

```powershell
.\scripts\graphify\install-graphify-global-mcp-startup.ps1
```

```bash
./scripts/graphify/install-graphify-global-mcp-startup.sh
```

The installer selects the startup mechanism of the host platform:

| Platform | Mechanism | Location |
| --- | --- | --- |
| Windows | Startup folder script | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.graphify-global-mcp.plist` |
| Linux with a systemd user manager | systemd user unit | `~/.config/systemd/user/graphify-global-mcp.service` |
| Linux otherwise | XDG autostart entry | `~/.config/autostart/graphify-global-mcp.desktop` |

The Linux installer queries the systemd user manager rather than only checking
that `systemctl` exists, because containers and some sessions ship the binary
without a working user manager. It falls back to the XDG entry in that case.

The installer preserves the previous launcher in a timestamped backup under
`~/.graphify/backups/startup`.

## Check health

Call `memory_status` from an MCP client.
Check the primary source, retrieval sources, index, graph, package, and MCP fields.

A saved Markdown file can exist before its derived indexes change.
Report the Markdown and index results as separate results.
