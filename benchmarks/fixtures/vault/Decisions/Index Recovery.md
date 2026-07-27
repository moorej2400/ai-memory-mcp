---
type: memory
memory_id: mem-index-recovery
title: Immutable index snapshot recovery
root_scope: work
primary_scope:
  kind: decision
  id: decision:index-recovery
status: active
created: 2026-07-25
updated: 2026-07-25
---

# Immutable index snapshot recovery

Every index refresh publishes a new SQLite snapshot and retains earlier
snapshots. Keeping the old files makes rollback, benchmark comparison, and
post-failure diagnosis possible without reconstructing lost derived state.

