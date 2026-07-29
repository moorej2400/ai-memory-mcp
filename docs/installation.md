# Installation

This procedure installs AI Memory MCP on Windows.

## Requirements

Install these items before you start:

- Git
- Python 3.11 or later
- PowerShell 5.1 or later
- A Markdown memory directory

## Procedure

1. Clone the repository.
2. Open PowerShell in the repository root.
3. Run the setup command:

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory' -InstallClients
```

4. Wait for the setup procedure to complete.
5. Restart each configured client.
6. Call `memory_status` from an agent session.
7. Make sure that the status result is satisfactory.

## Add retrieval-only vaults

1. Open the untracked `.env` file.
2. Add a JSON object to `AI_MEMORY_RETRIEVAL_SOURCES`.
3. Give each vault a stable source ID.
4. Run `memory_sync`.
5. Run the Graphify maintenance script when graph relationships must change.

The setup keeps `AI_MEMORY_WORK_DIR` as the only writable vault.
The additional vaults remain retrieval-only sources.

## Setup results

The setup script creates `.venv` for the MCP server.
The script creates `.graphify-runtime` for Graphify 0.9.26.
The script installs the project in editable mode.
The script creates `.env` if the file does not exist.
The script builds the first derived index.
The `-InstallClients` option configures all supported clients.
The option installs repository-linked AI Memory and Graphify skill stubs.

The installer supports these clients:

- Codex
- Claude Code
- Claude Desktop
- GitHub Copilot CLI
- OpenCode
- Visual Studio Code
- Agents that use `~/.agents/skills`

The installer preserves a timestamped backup before it changes an existing file.

The setup script does not add `.env` to Git.
The setup script does not add memory data to Git.

## Select clients

Run the client installer after the main setup procedure:

```powershell
.\scripts\install-clients.ps1 -Clients Codex,ClaudeCode,Copilot
```

The `-InstallCodex` setup option remains available for Codex-only installations.

## Setup without a client

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory'
```

The setup procedure creates the environments and indexes without changing a client configuration.

## Related information

- [Configuration](configuration.md)
- [Operations](operations.md)
- [Development](development.md)
