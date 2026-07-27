---
type: memory
memory_id: mem-alpha-retry
title: ALPHA-142 transient authentication retry
root_scope: work
primary_scope:
  kind: ticket
  id: ALPHA-142
status: active
created: 2026-06-01
updated: 2026-07-20
related_repos: [alpha]
related:
  - "[[Workflows/Graph Refresh|Graph Refresh]]"
---

# ALPHA-142 transient authentication retry

## Failure handling

The service reports `NX-401` when a cached bearer token has expired. Refresh the
token once, then retry with exponential backoff capped at eight seconds.

This bounded delay prevents temporary authentication failures from hammering
the upstream service.

