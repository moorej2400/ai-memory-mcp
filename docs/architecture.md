# Architecture

## Decision

AI Memory MCP uses Graphify as an internal graph provider.
The system does not expose Graphify as its permanent public interface.

Agents use the stable AI Memory MCP tools.
The MCP server owns scope, ranking, freshness, evidence, health, and refresh control.

## Source authority

Markdown files in all configured vaults are durable-memory source data.
The primary vault is the only Markdown write authority.
Additional vaults are retrieval-only sources.
The canonical artifact database is the write authority for raw external artifacts.

The system derives memory indexes from Markdown.
The system derives artifact search and burst indexes from the canonical artifact database.

Raw artifacts never make a Markdown file authoritative for a transcript or chat log.
Distilled Markdown never makes the artifact database authoritative for an agent summary.

A failed refresh does not change either authority.

## System flow

```mermaid
flowchart LR
    P[Primary writable vault] --> I[Memory indexer]
    A[Retrieval-only vaults] --> I
    C[Provider adapter] --> D[Canonical artifact database]
    D --> X[Raw artifact FTS]
    D --> B[Derived burst index]
    D --> M[Agent distillation]
    M --> P
    I --> L[Exact and lexical index]
    I --> V[Semantic index]
    I --> G[Graphify graph]
    Q[Agent query] --> S[AI Memory MCP]
    S --> X
    S --> B
    S --> L
    S --> V
    S --> G
    L --> F[RRF fusion]
    V --> F
    G --> F
    F --> R[Rerank and context]
    R --> E[Evidence with citations]
```

## Components

### Markdown memory

The configured primary vault receives all new records.
Named additional vaults supply retrieval-only records.
Each durable record has an identity, scope, status, dates, and provenance.

### Memory indexer

The indexer reads changed Markdown files from all configured vaults.
It validates identity and metadata.
It skips unchanged content.
It publishes a versioned SQLite snapshot.
It prefixes each indexed path with its source ID.
It does not modify a configured vault.

### Exact and lexical retrieval

SQLite FTS5 supplies exact and lexical results.
Exact matches get priority for identifiers, paths, filenames, and error text.

### Semantic retrieval

A local embedding provider supplies paraphrase results.
The default provider is Model2Vec with the `minishlab/potion-base-8M` model.
The hashed feature provider is the automatic fallback.
The semantic index stays local and does not need an external API.

The index records the embedding provider that built it.
A query always uses the recorded provider.
A provider change makes the indexer build all vectors again.
If the recorded provider is not available, recall disables the semantic signal and gives a warning.

### Graphify provider

Graphify supplies graph nodes, edges, neighbors, and paths.

The graph build makes an edge from three sources.
A `primary_scope` value makes a `belongs-to` edge.
A frontmatter `related` entry makes a `declared-related` edge.
A body wikilink makes a `body-link` edge.
The build reports unresolved and ambiguous link counts separately.
The repository pins Graphify 0.9.26 in an isolated environment.

The routine refresh builds a Graphify-compatible graph from the current SQLite index.
This build covers all configured memory sources without an extraction API.

The graph contains one node for each indexed document.
The graph also contains declared relationships and shared-scope relationships.

Semantic Graphify extraction remains an optional maintenance operation.
It does not control routine memory availability.

The provider adapter hides Graphify file formats from MCP clients.
The adapter keeps Graphify replaceable.

### MCP facade

The MCP facade gives agents four public tools.
The facade applies scope rules before retrieval.
The facade returns source paths and retrieval evidence.

| Tool | Function |
|---|---|
| `memory_recall` | Returns cited Markdown and artifact evidence. |
| `memory_artifact_read` | Returns ordered raw context for one artifact reference. |
| `memory_sync` | Updates the derived indexes. |
| `memory_status` | Reports source, index, Graphify, and runtime status. |

## Query procedure

1. Resolve the optional source and domain scope.
2. Apply repository, project, ticket, status, and path filters.
3. Select the required retrieval providers.
4. Run the selected providers.
5. Fuse ranked results with reciprocal rank fusion.
6. Remove duplicate memory results.
7. Rerank a bounded result set.
8. Add the necessary context.
9. Return evidence with source paths.

Graph traversal is one retrieval signal.
Graph traversal is not the only retrieval method.

Fusion adds a bounded freshness bonus from the `updated` date.
An expired `review_after` date applies a bounded penalty and adds a `review overdue` reason.

## Refresh procedure

1. Validate all configured memory sources.
2. Update the SQLite index.
3. Build the Graphify provider graph from the index.
4. Validate the staged graph.
5. Publish the staged graph atomically.
6. Reload the Graphify service.
7. Run a retrieval health check.

The update keeps the last satisfactory data after a failure.
An ordinary Markdown change uses `memory_sync`.
The maintenance script records each phase in a local JSONL log.

## Provider boundary

The MCP contract must not depend on Graphify response formats.
The graph provider can change without an MCP tool change.

Replace Graphify only if its adapter cannot meet a required contract.
Examples include unsafe updates, unstable serialization, or insufficient provenance.

## Performance rules

- Apply scope filters before ranking.
- Use exact matches for stable identifiers.
- Use reciprocal rank fusion for provider results.
- Limit reranking to a bounded candidate set.
- Load context for all results in one database query.
- Keep normal recall responses compact.
- Process only changed Markdown files during normal refreshes.
- Keep full graph clustering as a maintenance task.
- Skip snapshot publication when no Markdown file changed.
- Store semantic vectors in a compact binary form.
- Combine adjacent short sections before indexing chat exports.
- Load Graphify candidate documents in one index query.

## Reliability rules

- Pin one Graphify version.
- Keep the CLI, library, MCP server, and health data consistent.
- Validate staged data before publication.
- Preserve the last satisfactory graph.
- Record Markdown and index results separately.
- Return source paths for evidence.
- Do not remove recovery files without authorization.

## Public tools

| Tool | Function |
|---|---|
| `memory_recall` | Returns cited evidence and applicable relationships. |
| `memory_artifact_read` | Returns ordered raw context for one artifact reference. |
| `memory_sync` | Updates the derived indexes after canonical Markdown or artifact data changes. |
| `memory_status` | Reports source, index, Graphify, and runtime status. |

`memory_recall` selects its internal behavior from the query.
An exact identity returns the complete record.
A relationship question returns a graph path when a path exists.
A general question runs lexical, semantic, and graph retrieval.

Recall reports `answered` for an exact match, sufficient lexical coverage, or a clear semantic lead.
A paraphrase answer needs a lexical anchor and a semantic margin above the other results.
The two conditions together keep an absent answer at `no_answer`.

A `no_answer` status still returns ranked best-effort evidence.
A warning marks that evidence as leads that require verification.

The MCP does not expose provider diagnostics in normal recall results.
The MCP does not expose full Graphify rebuilds.
Use the Graphify maintenance script for a full rebuild.

Each recall result contains `status`, `intent`, `evidence`, `citations`, `relationships`, and `warnings`.

## Research sources

- [Cerebras knowledge base design](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base)
- [Anthropic contextual retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- [Microsoft GraphRAG query modes](https://microsoft.github.io/graphrag/query/overview/)
- [Graphify releases](https://github.com/Graphify-Labs/graphify/releases)
