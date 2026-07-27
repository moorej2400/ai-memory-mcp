---
type: memory
memory_id: mem-alpha-generation
title: Alpha deployment generation counter
root_scope: work
primary_scope:
  kind: repository
  id: alpha
status: active
created: 2026-06-02
updated: 2026-06-02
related_repos: [alpha]
---

# Alpha deployment generation counter

Alpha increments its generation counter only after a blue-green deployment is
fully healthy. Workers compare the counter before accepting new jobs.

