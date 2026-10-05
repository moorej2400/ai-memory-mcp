# Native Graph Validation

## Scope

This report records the validation of the Graphify removal on macOS on October 5, 2026.
It contains no memory content, private identifiers, or machine-specific paths.
The Windows test installation remained read-only.
A read-only probe on Windows found a login launcher from the earlier release and no running listener.

## Change summary

AI Memory builds, validates, and reads its note graph without the external Graphify package.
The graph algorithms and the node-link JSON format did not change.
The change replaced these external items:

| Earlier item | Current item |
| --- | --- |
| Graphify global MCP listener and its login launcher | None. Agents use `memory_recall`. |
| Graphify refresh, extraction, merge, and publication scripts | `memory_sync` or `ai-memory-sync` |
| Pinned Graphify runtime in the default setup | Optional runtime for Graphify Codebase only |
| `graphify` status field with a runtime block | `graph` status field with an `error` field |

The [Graphify pipeline archive](../archive/graphify-memory-pipeline/README.md) keeps the earlier scripts.

## Parsing changes

The link parser ignores fenced code, inline code, HTML comments, and Obsidian comments.
The parser reads the table form `[[Note\|Label]]` and the unquoted YAML form `- [[Note]]`.
The frontmatter parser accepts only whole-line fences.

A note that opens frontmatter without a closing fence now has no frontmatter.
The earlier parser used a later `---` as the fence, for example a table separator.
That error removed the start of the note from the index.

The index schema version changed from `10` to `11`.
The first synchronization after an upgrade parses every note again.
Without this change, an incremental synchronization keeps notes from the earlier parser.

## Automated tests

All `605` automated tests passed.
The suite emits an existing Pydantic lifespan annotation warning.

These test files cover the change:

- `tests/test_graph_baseline.py` records the earlier ranking, neighbors, paths, scope isolation, and graph-assisted recall.
- `tests/test_graph_parsing.py` covers link parsing, frontmatter fences, snapshot validation, and corruption status.
- `tests/test_generations.py` covers generations from the earlier release and the `ai-memory-sync` exit status.
- `tests/test_scripts_portability.py` covers setup options and the retirement script.

The baseline tests passed before the change and after the change without edits to the expected values.

The frozen benchmark passed `20` of `20` cases.
Recall at 5 was `1.0`, and the scope leakage rate was `0.0`.
The benchmark contract digest did not change.

## Comparison on a real vault

The comparison used a scratch copy of the derived data from a real macOS vault.
The canonical vaults were not changed.

| Measurement | Before | After |
| --- | --- | --- |
| Graph nodes | `179` | `179` |
| Graph edges | `123` | `124` |
| Changed unscoped ranks | Not applicable | `3` of `60` |
| Changed scoped ranks | Not applicable | `0` of `60` |
| Changed paths | Not applicable | `0` of `20` |
| Recall queries with a different result set | Not applicable | `0` of `30` |
| Recall queries with a different result order | Not applicable | `2` of `30` |

The one new edge came from the frontmatter fence repair.
Each changed rank, neighbor list, and result order contains that edge.

## Installation checks

| Check | Result |
| --- | --- |
| Fresh setup without Graphify | Passed. Setup created no Graphify runtime and wrote no `GRAPHIFY_*` keys. |
| First generation from setup | Passed. Status reported a consistent generation and an available graph. |
| Upgrade from the earlier release | Passed. The earlier generation stayed valid before the first new synchronization. |
| Health migration | Passed. The next synchronization wrote the `graph` layer name. |
| Re-parse after upgrade | Passed. On the real vault copy, the first synchronization parsed all `163` notes again and added the missing edge. |
| Launcher of another checkout | Passed. The retirement script reported the launcher and kept it. |
| Graphify Codebase stub without Graphify | Passed. The client installers did not add the stub. |
| Retirement dry run | Passed. The script reported the listener, the launcher, and the legacy state. It changed nothing. |
| Retirement with the apply option | Passed. The script stopped the listener and moved the launcher and the state to the archive. |
| Graphify Codebase | Passed. The wrapper used the kept runtime and built a code graph. |

The upgrade check used an isolated home directory for the launcher file.
The check did not register a launcher with the operating system.

The earlier release could not complete a fresh Graphify refresh on this date.
The pinned runtime installed an incompatible `mcp` package, so the listener did not start.
The refresh retrieval gate also ran in an interpreter without `PyYAML`.

## Failure checks

| Check | Result |
| --- | --- |
| Corrupt graph snapshot | Status reported `graph.error`. Recall continued with a warning. |
| Synchronization after corruption | A new valid generation replaced the corrupt graph. |
| Failed publication | `ai-memory-sync` exited with status `1`. The previous generation stayed active. |
| Recovery after the failure | The next synchronization published a consistent generation. |
| Retention | Only the active generation and one verified previous generation remained. |
