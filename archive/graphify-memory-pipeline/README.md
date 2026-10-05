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
| Graph validation | Generation publication validates the graph structure and the Markdown snapshot before it changes the pointer. Recall and status compare the recorded checksum when they load the graph. |
| Global MCP listener | None. Agents use the `memory_recall` MCP tool. |
| Retrieval check | `scripts/run_retrieval_eval.py` |
| Process helpers | `scripts/_processes.py` |
| Listener and launcher removal | `scripts/retire_graphify_memory.py` |

## Restore the earlier pipeline

The files in this directory are for reference only.
They do not run in the current tree.
They import helpers that `scripts/_common.py` no longer contains.
`refresh_graph.py` also calls the removed `ai_memory_mcp.provider_graph` module.

Commit `514fc22` is the last revision that contains the complete pipeline.
Use that revision in a separate worktree:

1. Open a terminal in the repository root.
2. Create a worktree at the earlier revision:

   ```bash
   git worktree add --detach <worktree-path> 514fc22
   ```

3. Run setup in the worktree. Setup at that revision installs the pinned Graphify runtime.
4. If `graphify-mcp` does not start, install `mcp<2` into the `.graphify-runtime` environment.
5. Run the scripts from `<worktree-path>/scripts/graphify/`.

The earlier `graphifyy` release does not pin `mcp`.
A new installation can get `mcp` 2, which `graphify-mcp` cannot use.
