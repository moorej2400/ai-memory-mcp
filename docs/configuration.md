# Configuration

The repository uses an untracked `.env` file.
Process environment variables have priority over `.env` values.

Copy `.env.example` when you create the configuration manually.

## Memory paths

| Variable | Function |
|---|---|
| `AI_MEMORY_WORK_DIR` | Sets the canonical work memory directory. |
| `AI_MEMORY_PERSONAL_DIR` | Sets the optional personal memory directory. |
| `AI_MEMORY_MCP_STATE_DIR` | Sets the derived AI Memory index directory. |
| `AI_MEMORY_GRAPH_PATH` | Sets the AI Memory Graphify graph file. |
| `AI_MEMORY_GRAPHIFY_STATE_DIR` | Sets the Graphify state directory. |

Do not use the work directory as a personal memory fallback.

## Graphify

| Variable | Function |
|---|---|
| `GRAPHIFY_MEMORY_REFRESH_SCRIPT` | Sets a custom full-refresh script. |
| `GRAPHIFY_MEMORY_EXTRACT_SCRIPT` | Sets a custom extraction script. |
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

| Client | Configuration file | Skill location |
|---|---|---|
| Codex | `~/.codex/config.toml` | `~/.codex/skills/ai-memory/` |
| Claude Code | `~/.claude.json` | `~/.claude/skills/ai-memory/` |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` | Uses the Claude personal skill |
| Copilot CLI | `~/.copilot/mcp-config.json` | `~/.copilot/skills/ai-memory/` |
| OpenCode | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/skills/ai-memory/` |
| VS Code | `%APPDATA%/Code/User/mcp.json` | Uses the Copilot personal skill |
| Shared agents | Not applicable | `~/.agents/skills/ai-memory/` |

The installer keeps the canonical skill in this repository.
Each installed skill file points to that canonical source.
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
