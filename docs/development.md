# Development

This guide gives the development and test procedures.

## Install development dependencies

Run the setup script without the Codex option:

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory'
```

## Run tests

Run all automated tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Run the frozen benchmark

Run the retrieval benchmark:

```powershell
.\.venv\Scripts\ai-memory-benchmark.exe --label local-validation
```

Do not change the frozen cases during retrieval performance work.
Do not change the frozen fixtures during retrieval performance work.
The benchmark lock detects a contract change.

## Project directories

| Directory | Content |
|---|---|
| `src/ai_memory_mcp/` | MCP server and retrieval source |
| `tests/` | Automated tests |
| `benchmarks/` | Frozen retrieval contract |
| `scripts/` | Setup and operations scripts |
| `skill/ai-memory/` | Canonical agent skill |
| `docs/` | User and developer documentation |

## Change requirements

Read `AGENTS.md` before you change the project.
Keep Graphify behind the provider boundary.
Keep Markdown as the write authority.
Preserve the last satisfactory derived state during refresh work.
Add tests when a behavior changes.
