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
| `AI_MEMORY_GRAPH_PATH` | Sets the legacy graph file. AI Memory reads this file only when no generation exists. |
| `AI_MEMORY_GRAPHIFY_STATE_DIR` | Sets the legacy Graphify state directory. The default legacy graph file is in this directory. |
| `AI_MEMORY_LOG_DIR` | Sets the local index and retrieval log directory. |
| `AI_MEMORY_ARTIFACT_DB` | Sets the canonical raw artifact database file. |
| `AI_MEMORY_ARTIFACT_OBJECTS_DIR` | Sets the attachment object directory. |
| `AI_MEMORY_ARTIFACT_BACKUP_DIR` | Sets the artifact backup directory. |
| `AI_MEMORY_ARTIFACT_BATCH_MAX_BYTES` | Sets the maximum uncompressed intake batch size. |
| `AI_MEMORY_GENERATION_RETENTION_COUNT` | Sets the minimum derived generation count. The effective minimum is two. |
| `AI_MEMORY_GENERATION_LEASE_TTL_SECONDS` | Sets the stale recall lease limit. The default is 86400 seconds. |

`AI_MEMORY_WORK_DIR` also sets the default internal data root.
The default internal data root is `AI_MEMORY_WORK_DIR/.ai-memory/`.

| Internal path | Default |
|---|---|
| Raw artifact database | `.ai-memory/raw/artifacts.sqlite3` |
| Attachment objects | `.ai-memory/raw/objects/` |
| Artifact backups | `.ai-memory/backups/` |
| Migration data | `.ai-memory/migration/` |
| Provider state | `.ai-memory/provider-state/` |
| Derived indexes | `.ai-memory/indexes/` |
| Logs | `.ai-memory/logs/` |

The setup command derives these paths from `AI_MEMORY_WORK_DIR`.
Do not set individual path variables for a standard installation.
Use individual path variables only for compatibility or an advanced storage layout.
An existing `.env` keeps each explicit path override.
Do not remove an override until you create and verify the applicable migration backup.

Use a JSON object for `AI_MEMORY_RETRIEVAL_SOURCES`:

```dotenv
AI_MEMORY_RETRIEVAL_SOURCES='{"archive":"C:/memory/archive","reference":"D:/memory/reference"}'
```

Source IDs must start with a letter.
Use only lowercase letters, numbers, and hyphens.

`AI_MEMORY_GRAPH_PATH` and `AI_MEMORY_GRAPHIFY_STATE_DIR` are legacy read fallbacks.
AI Memory builds the note graph in each coordinated generation.
AI Memory reads the legacy graph file only when no coordinated generation exists.
The default legacy graph file is `.ai-memory/provider-state/graphify/corpora/ai-memory/graphify-out/graph.json`.
AI Memory does not write this file.
The [retirement script](operations.md#upgrade-an-installation-that-used-graphify) uses `AI_MEMORY_GRAPHIFY_STATE_DIR` to find the legacy state.

The server writes no Markdown files.
The AI Memory skill writes new records only under `AI_MEMORY_WORK_DIR`.
The indexer reads all configured sources without changing them.
The indexer excludes the hidden `.ai-memory` directory.

The default artifact batch limit is 268435456 bytes.
Set a positive integer when you override this limit.

The system retains the active derived generation and one verified previous generation.
An active recall lease can temporarily retain an additional generation.
Generation retention never removes canonical artifacts or required attachment objects.

## Retrieval evaluation

| Variable | Function |
|---|---|
| `AI_MEMORY_RETRIEVAL_EVAL_CASES` | Sets the local retrieval evaluation cases as a JSON list of pairs. |

Each pair contains one question and one expected evidence marker.
The [retrieval evaluation](operations.md#run-the-retrieval-evaluation) requires at least one pair.

```dotenv
AI_MEMORY_RETRIEVAL_EVAL_CASES='[["<question>","<expected-marker>"]]'
```

The evaluation also reads `GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES` when `AI_MEMORY_RETRIEVAL_EVAL_CASES` has no value.
Earlier releases used this legacy name.

## Removed variables

AI Memory does not read these variables:

- `GRAPHIFY_GLOBAL_MCP_URL`
- `GRAPHIFY_OPENAI_BASE_URL`
- `GRAPHIFY_OPENAI_API_KEY`
- `GRAPHIFY_OPENAI_MODEL`
- `GRAPHIFY_OPENAI_TOKEN_BUDGET`
- `GRAPHIFY_OPENAI_MAX_CONCURRENCY`
- `GRAPHIFY_OPENAI_API_TIMEOUT`
- `GRAPHIFY_MAX_RETRIES`
- `GRAPHIFY_MEMORY_REFRESH_SCRIPT`
- `GRAPHIFY_MEMORY_EXTRACT_SCRIPT`
- `AI_MEMORY_GRAPHIFY_PYTHON`
- `AI_MEMORY_GRAPHIFY_MCP_EXE`

An existing `.env` file can contain these variables from an earlier release.
Remove each variable that no other tool uses.

The setup command installs `.graphify-runtime` only with `--with-graphify-codebase` or `-WithGraphifyCodebase`.
Only the independent Graphify Codebase skill uses this runtime.

## MCP server

| Variable | Default | Function |
|---|---:|---|
| `AI_MEMORY_MCP_HOST` | `127.0.0.1` | Sets the HTTP host. |
| `AI_MEMORY_MCP_PORT` | `4334` | Sets the HTTP port. |
| `AI_MEMORY_MCP_RESULT_LIMIT` | `8` | Sets the default result limit. |
| `AI_MEMORY_MCP_RECALL_TIMEOUT_SECONDS` | `30` | Sets the recall worker deadline. |
| `AI_MEMORY_RECALL_WORKERS` | `2` | Sets the supervised worker count. |
| `AI_MEMORY_RECALL_QUEUE_CAPACITY` | `8` | Sets the bounded waiting request count. |
| `AI_MEMORY_RECALL_WORKER_MAX_REQUESTS` | `500` | Sets the worker recycle interval. |
| `AI_MEMORY_VECTOR_BLOCK_SIZE` | `1024` | Sets the exact-search vector block size. |
| `AI_MEMORY_VECTOR_MAX_VECTORS` | `200000` | Sets the exact-search vector work limit. |
| `AI_MEMORY_VECTOR_MAX_SECONDS` | `8` | Sets the exact-search elapsed work limit. |
| `AI_MEMORY_ANN_CANDIDATE_LIMIT` | `10000` | Sets the maximum ANN candidate count. |
| `AI_MEMORY_CONTEXT_MAX_CHARACTERS` | `5000` | Sets the expanded context limit. |
| `AI_MEMORY_MCP_SEMANTIC_DIMENSIONS` | `1024` | Sets the hashed semantic vector size. |
| `AI_MEMORY_MCP_EMBEDDING_PROVIDER` | `auto` | Selects `model2vec`, `hashed`, or `auto`. |
| `AI_MEMORY_MCP_EMBEDDING_MODEL` | `minishlab/potion-base-8M` | Sets the Model2Vec model name. |
| `AI_MEMORY_MCP_RRF_K` | `60` | Sets the RRF constant. |
| `AI_MEMORY_MCP_GRAPH_DEPTH` | `2` | Sets the graph traversal depth. |
| `AI_MEMORY_AUDIT_LOGGING` | `true` | Enables local index and retrieval logs. |
| `AI_MEMORY_QUERY_LOG_CONTENT` | `false` | Enables private query, response, and stage logs. |
| `AI_MEMORY_AUDIT_LOG_MAX_BYTES` | `25000000` | Sets the active JSONL log size limit. |
| `AI_MEMORY_AUDIT_LOCK_TIMEOUT_SECONDS` | `10` | Sets the audit log lock timeout. |
| `AI_MEMORY_INDEX_LOCK_TIMEOUT_SECONDS` | `300` | Sets the index publisher lock timeout. |

The server accepts only a loopback host.
The HTTP transport does not provide authentication.

The recall deadline contains failed work.
It does not define acceptable retrieval performance.
Response version 2 reports a deadline as failed execution, not an empty search.

The standard retrieval log contains query hashes, evidence digests, and metadata.
The optional private query log contains full query arguments and returned values.
Both flags must be `true` for private query logs.
Keep the log directory outside the repository.
See [Query Logging](query-logging.md) for setup, record fields, and failure limits.

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
