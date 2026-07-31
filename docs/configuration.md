# Configuration

The repository uses an untracked `.env` file.
Process environment variables have priority over `.env` values.

Copy `.env.example` when you create the configuration manually.

## Memory paths

| Variable | Function |
|---|---|
| `AI_MEMORY_WORK_DIR` | Sets the only writable memory vault. |
| `AI_MEMORY_PRIMARY_SOURCE_ID` | Sets the primary source ID. The default is `core`. |
| `AI_MEMORY_RETRIEVAL_SOURCES` | Maps retrieval-only source IDs to vault directories. |
| `AI_MEMORY_PERSONAL_DIR` | Sets the optional `personal` retrieval-only source. |
| `AI_MEMORY_MCP_STATE_DIR` | Sets the derived AI Memory index directory. |
| `AI_MEMORY_GRAPH_PATH` | Sets the AI Memory Graphify graph file. |
| `AI_MEMORY_GRAPHIFY_STATE_DIR` | Sets the Graphify state directory. |
| `AI_MEMORY_GRAPHIFY_PYTHON` | Overrides the pinned Graphify interpreter. |
| `AI_MEMORY_GRAPHIFY_MCP_EXE` | Overrides the pinned Graphify MCP executable. |

Use a JSON object for `AI_MEMORY_RETRIEVAL_SOURCES`:

```dotenv
AI_MEMORY_RETRIEVAL_SOURCES='{"archive":"C:/memory/archive","reference":"D:/memory/reference"}'
```

Source IDs must start with a letter.
Use only lowercase letters, numbers, and hyphens.

The two Graphify override variables are normally unset. The setup script
provisions `.graphify-runtime` using the layout of the host platform, and the
project resolves the interpreter and executables from it automatically. Set
them only to point at a Graphify installed somewhere else.

The server writes no Markdown files.
The AI Memory skill writes new records only under `AI_MEMORY_WORK_DIR`.
The indexer reads all configured sources without changing them.

## Graphify

| Variable | Function |
|---|---|
| `GRAPHIFY_MEMORY_REFRESH_SCRIPT` | Names the full-refresh script an agent should run. |
| `GRAPHIFY_MEMORY_EXTRACT_SCRIPT` | Names the extraction script an agent should run. |
| `GRAPHIFY_GLOBAL_MCP_URL` | Sets the Graphify MCP endpoint. |
| `GRAPHIFY_OPENAI_BASE_URL` | Sets an optional compatible API endpoint. |
| `GRAPHIFY_OPENAI_API_KEY` | Sets the optional extraction credential. |
| `GRAPHIFY_OPENAI_MODEL` | Sets the optional extraction model. |
| `GRAPHIFY_OPENAI_TOKEN_BUDGET` | Sets the extraction token limit. |
| `GRAPHIFY_OPENAI_MAX_CONCURRENCY` | Sets the extraction concurrency limit. |
| `GRAPHIFY_OPENAI_API_TIMEOUT` | Sets the extraction timeout. |
| `GRAPHIFY_MAX_RETRIES` | Sets the extraction retry limit. |
| `GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES` | Sets optional local retrieval evaluation cases as JSON pairs. |

The normal index refresh does not need an extraction API.
A full Graphify refresh can need the optional API values.

These two script variables are read by the agent that follows the AI Memory
skill, not by the server. Point them at an entry point the host platform can
run: the `.ps1` wrapper on Windows, the `.sh` wrapper on macOS and Linux, or
the `.py` implementation on any platform. Leave them unset to use the
repository-owned scripts.

## MCP server

| Variable | Default | Function |
|---|---:|---|
| `AI_MEMORY_MCP_HOST` | `127.0.0.1` | Sets the HTTP host. |
| `AI_MEMORY_MCP_PORT` | `4334` | Sets the HTTP port. |
| `AI_MEMORY_MCP_RESULT_LIMIT` | `8` | Sets the default result limit. |
| `AI_MEMORY_MCP_SEMANTIC_DIMENSIONS` | `1024` | Sets the semantic vector size. |
| `AI_MEMORY_MCP_RRF_K` | `60` | Sets the RRF constant. |
| `AI_MEMORY_MCP_GRAPH_DEPTH` | `2` | Sets the graph traversal depth. |

The server accepts only a loopback host.
The HTTP transport does not provide authentication.

## Repository privacy

| Variable | Function |
|---|---|
| `AI_MEMORY_PRIVATE_REPOSITORY_TERMS` | Sets local terms that must not occur in commit-eligible files. Separate each term with a vertical bar. |

The portability test reads this value from the ignored `.env` file.
Keep organization names, private domains, ticket prefixes, and user-specific paths in this local value.

## Client configuration

The client installer registers one local stdio server named `ai-memory`.
Each registration runs the repository-owned Python environment.

| Client | Configuration file | Skill locations |
|---|---|---|
| Codex | `~/.codex/config.toml` | `~/.codex/skills/ai-memory/` and `~/.codex/skills/graphify/` |
| Claude Code | `~/.claude.json` | `~/.claude/skills/ai-memory/` and `~/.claude/skills/graphify/` |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` | Uses Claude Code skills |
| Copilot CLI | `~/.copilot/mcp-config.json` | `~/.copilot/skills/ai-memory/` and `~/.copilot/skills/graphify/` |
| OpenCode | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/skills/ai-memory/` and `~/.config/opencode/skills/graphify/` |
| VS Code | `%APPDATA%/Code/User/mcp.json` | Uses Copilot personal skills |
| Shared agents | Not applicable | `~/.agents/skills/ai-memory/` and `~/.agents/skills/graphify/` |

The installer keeps both canonical skills in this repository.
Each installed skill file is a discovery stub.
Each stub points to its canonical source.
The installer enables the VS Code Agent Skills feature.

The installer supports the OpenCode version 1 and version 2 MCP structures.

For format details, read these official guides:

- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Copilot CLI MCP](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
- [OpenCode MCP](https://opencode.ai/docs/mcp-servers)
- [VS Code MCP](https://code.visualstudio.com/docs/agent-customization/mcp-servers)

## Security

Do not commit `.env`.
Do not put user memory in this repository.
Do not put production secrets in `.env.example`.
Do not put organization-specific values in tracked files.
