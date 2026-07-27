---
type: memory
memory_id: mem-graph-refresh
title: Guarded Graphify memory refresh
root_scope: work
primary_scope:
  kind: reference
  id: workflow:graph-refresh
status: active
created: 2026-07-03
updated: 2026-07-25
related:
  - "[[Decisions/Memory Authority|Canonical memory authority]]"
  - "[[Repos/demo/Tickets/DEMO-777/_ticket|DEMO-777]]"
---

# Guarded Graphify memory refresh

## Publication phases

Stage extraction, validate the candidate graph, preserve a recovery snapshot,
publish the validated graph, restart MCP, and run health and retrieval gates.
Rollback restores the prior live graph if a publication gate fails.

## Incremental behavior

For a one-note change, hash the Markdown and rebuild only changed section
chunks. Reuse prompt-versioned semantic artifacts and schedule full community
clustering separately.
