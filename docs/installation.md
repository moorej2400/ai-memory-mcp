# Installation

This procedure installs AI Memory MCP on Windows, macOS, or Linux.

## Requirements

Install these items before you start:

- Git
- Python 3.11 or later
- A Markdown memory directory

Windows additionally requires PowerShell 5.1 or later if you use the `.ps1`
entry points. macOS and Linux use the `.sh` entry points and need no extra
shell.

## Procedure

1. Clone the repository.
2. Open a terminal in the repository root.
3. Run the setup command for your platform.

Windows (PowerShell):

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory' -InstallClients
```

macOS and Linux:

```bash
./scripts/setup.sh --memory-root ~/AI-Memory --install-clients
```

Any platform, invoking the implementation directly:

```bash
python3 scripts/setup.py --memory-root ~/AI-Memory --install-clients
```

4. Wait for the setup procedure to complete.
5. Restart each configured client.
6. Call `memory_status` from an agent session.
7. Make sure that the status result is satisfactory.

## Entry points

Every maintenance task has one cross-platform Python implementation and two
thin wrappers, so the same behaviour is available from any shell:

| Task | PowerShell | POSIX shell | Implementation |
| --- | --- | --- | --- |
| Provision | `scripts\setup.ps1` | `scripts/setup.sh` | `scripts/setup.py` |
| Install clients | `scripts\install-clients.ps1` | `scripts/install-clients.sh` | `scripts/install_clients.py` |
| Install Codex | `scripts\install-codex.ps1` | `scripts/install-codex.sh` | `scripts/install_codex.py` |
| Check live retrieval | `scripts\run-retrieval-eval.ps1` | `scripts/run-retrieval-eval.sh` | `scripts/run_retrieval_eval.py` |
| Retire Graphify services | `scripts\retire-graphify-memory.ps1` | `scripts/retire-graphify-memory.sh` | `scripts/retire_graphify_memory.py` |

The `ai-memory-sync` command publishes a new generation.
The generation contains the Markdown index, the artifact index, and the note graph.
Setup installs the command in the `.venv` environment.

The PowerShell wrappers keep their original parameter style, such as
`-MemoryRoot`. The POSIX wrappers and the Python implementations use the
equivalent long options, such as `--memory-root`.

## Add retrieval-only vaults

1. Open the untracked `.env` file.
2. Add a JSON object to `AI_MEMORY_RETRIEVAL_SOURCES`.
3. Give each vault a stable source ID.
4. Run `memory_sync`.

The synchronization also rebuilds the note graph for every vault.

The setup keeps `AI_MEMORY_WORK_DIR` as the only writable vault.
The additional vaults remain retrieval-only sources.

## Setup results

The setup script creates `.venv` for the MCP server.
The script installs the project in editable mode.
The script creates `.env` if the file does not exist.
The script creates internal data under `AI_MEMORY_WORK_DIR/.ai-memory/`.
The script initializes the canonical artifact database.
The script runs `ai-memory-sync` to publish the first generation.
The first generation contains the note graph.
The `--install-clients` option configures all supported clients.
The option installs repository-linked AI Memory and Graphify Codebase skill stubs.

Setup does not install Graphify.
AI Memory does not use Graphify.

The `--with-graphify-codebase` option creates `.graphify-runtime` with the version in `requirements-graphify.txt`.
Only the independent Graphify Codebase skill uses this runtime.
In PowerShell, use the `-WithGraphifyCodebase` switch.
Setup accepts the earlier `--skip-graphify-runtime` option, but the option has no effect.

Each environment uses the layout of the host platform. Windows uses
`Scripts\python.exe`, and macOS and Linux use `bin/python`. The client
configurations record whichever interpreter the platform created.

The installer supports these clients:

- Codex
- Claude Code
- Claude Desktop
- GitHub Copilot CLI
- OpenCode
- Visual Studio Code
- Agents that use `~/.agents/skills`

The installer resolves each client configuration under the per-user application
data directory of the host platform:

| Platform | Application data root |
| --- | --- |
| Windows | `%APPDATA%` |
| macOS | `~/Library/Application Support` |
| Linux | `$XDG_CONFIG_HOME`, or `~/.config` |

The installer preserves a timestamped backup before it changes an existing file.

The setup script does not add `.env` to Git.
The setup script does not add memory data to Git.

## Select clients

Run the client installer after the main setup procedure:

```powershell
.\scripts\install-clients.ps1 -Clients Codex,ClaudeCode,Copilot
```

```bash
./scripts/install-clients.sh --client codex --client claude-code --client copilot
```

The `--install-codex` setup option remains available for Codex-only
installations.

## Setup without a client

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory'
```

```bash
./scripts/setup.sh --memory-root ~/AI-Memory
```

The setup procedure creates the environment and the first generation without changing a client configuration.

## Related information

- [AI agent setup for a new system](agent-new-system-setup.md)
- [Configuration](configuration.md)
- [Operations](operations.md)
- [Development](development.md)
