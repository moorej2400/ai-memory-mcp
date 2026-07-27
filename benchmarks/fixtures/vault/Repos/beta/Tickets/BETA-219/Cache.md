---
type: memory
memory_id: mem-beta-cache
title: BETA-219 stale cache pointer
root_scope: work
primary_scope:
  kind: ticket
  id: BETA-219
status: active
created: 2026-06-10
updated: 2026-07-21
related_repos: [beta]
---

# BETA-219 stale cache pointer

The cache previously served data from a stale snapshot pointer. Beta now
increments a generation counter when a new snapshot is published and readers
invalidate cached rows whenever their generation is older.

