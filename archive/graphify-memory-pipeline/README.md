# Archived Graphify memory pipeline

AI Memory does not use these files.
They are kept so that an operator can recover the earlier pipeline.

## Contents

The `scripts/graphify/` directory contains the earlier Graphify pipeline for AI Memory.

| File | Earlier purpose |
| --- | --- |
| `extract_ai_memory.py` | Run the Graphify semantic extraction on each memory source. |
| `merge-memory-source-graphs.py` | Merge the graph of each memory source. |
| `publish-ai-memory-global.py` | Add the memory graph to a staged Graphify global graph. |
| `refresh_graph.py` | Build, validate, publish, and roll back the Graphify corpus and global graph. |
| `validate-ai-memory-graph.py` | Validate the corpus, the global graph, and the global MCP listener. |
| `start_global_mcp.py`, `stop_global_mcp.py` | Start and stop the Graphify global MCP listener. |
| `install_autostart.py` | Install a login launcher for the Graphify global MCP listener. |

## Replacements

| Earlier item | Current item |
| --- | --- |
| Graph refresh | `ai-memory-sync` or the `memory_sync` MCP tool. One generation holds the Markdown index, the artifact index, and the note graph. |
| Graph validation | Generation publication. It validates the graph structure, the checksum, and the Markdown snapshot before it changes the pointer. |
| Global MCP listener | None. Agents use the `memory_recall` MCP tool. |
| Retrieval check | `scripts/run_retrieval_eval.py` |
| Process helpers | `scripts/_processes.py` |
| Listener and launcher removal | `scripts/retire_graphify_memory.py` |

## Restore an archived script

These scripts need the pinned Graphify runtime.
They import `_common.py` and `_processes.py` from the `scripts/` directory.

1. Install the runtime with `scripts/setup.py --memory-root <path> --with-graphify-codebase`.
2. Copy the script to `scripts/graphify/` in a local working tree.
3. Copy `scripts/_processes.py` to `scripts/graphify/` when the script uses it.
