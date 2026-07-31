# Development

This guide gives the development and test procedures.

## Install development dependencies

Run the setup script without the Codex option:

```powershell
.\scripts\setup.ps1 -MemoryRoot 'C:\path\to\AI-Memory'
```

```bash
./scripts/setup.sh --memory-root ~/AI-Memory
```

## Run tests

Run all automated tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

```bash
./.venv/bin/python -m pytest
```

## Run the frozen benchmark

Run the retrieval benchmark:

```powershell
.\.venv\Scripts\ai-memory-benchmark.exe --label local-validation
```

```bash
./.venv/bin/ai-memory-benchmark --label local-validation
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

## Platform support

The project runs on Windows, macOS, and Linux.

Every platform difference resolves through one module,
`src/ai_memory_mcp/platform_paths.py`, which covers environment layout,
executable suffixes, `site-packages` discovery, per-user application data
directories, and filesystem case sensitivity. The maintenance scripts use the
equivalent standalone helper `scripts/_common.py`, which must import with only
the standard library because it runs before the project is installed.

Each maintenance task has one Python implementation plus a `.ps1` and a `.sh`
wrapper. Add behaviour to the Python implementation only; the wrappers must stay
thin so the platforms cannot drift apart. `tests/test_scripts_portability.py`
asserts that every entry point keeps both wrappers and that no implementation
reintroduces a Windows-only environment path.

`tests/test_powershell_wrappers.py` checks the PowerShell wrappers without
needing a PowerShell interpreter. It enforces Windows PowerShell 5.1
compatibility — most importantly that `Join-Path` is never given three
arguments, which is valid only from PowerShell 6 and is a parse error on the
5.1 that Windows ships by default.

Process control and port checks live in `scripts/graphify/_processes.py` and use
only the standard library plus the process listing tool each platform ships, so
no third-party dependency is required.

When you add a platform-conditional path, add it to the shared module and cover
it in `tests/test_platform_paths.py` for every supported platform rather than
branching at the call site.

## Change requirements

Read `AGENTS.md` before you change the project.
Keep Graphify behind the provider boundary.
Keep Markdown as the write authority.
Preserve the last satisfactory derived state during refresh work.
Add tests when a behavior changes.
