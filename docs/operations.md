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
The command skips publication when no Markdown file changed.

Concurrent commands wait for the current index publisher.
The default wait limit is 300 seconds.

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
The script builds the provider graph from the current SQLite index.
This procedure does not require an extraction API.

Run this script for Graphify maintenance:

```powershell
.\scripts\graphify\refresh-ai-memory-graph.ps1
```

```bash
./scripts/graphify/refresh-ai-memory-graph.sh
```

Use semantic extraction only for optional maintenance analysis:

```powershell
.\scripts\graphify\refresh-ai-memory-graph.ps1 -SemanticExtraction
```

```bash
./scripts/graphify/refresh-ai-memory-graph.sh --semantic-extraction
```

Only one refresh can publish at a time.
The script uses an advisory file lock for each run.
The script reports an error if another refresh holds the lock.

Every run writes a transcript to
`<graphify-state>/logs/ai-memory-refresh/ai-memory-refresh-<run-id>.log`.
The transcript records each step and its output.
The transcript also records failures and rollback operations.

## Review local logs

The index log records source counts, changes, errors, lock waits, and elapsed time.
The retrieval log records each query, result, citation, diagnostic, and elapsed time.

Read these files under `AI_MEMORY_LOG_DIR`:

- `index.jsonl`
- `retrieval.jsonl`

The default directory is `AI_MEMORY_MCP_STATE_DIR\logs`.
The logger moves a full active log to a timestamped local archive.

Graphify refresh events use a separate local directory.
Read these files under `AI_MEMORY_GRAPHIFY_STATE_DIR\logs\ai-memory-refresh`.

These logs can contain memory text.
Do not copy these logs into the repository.

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
