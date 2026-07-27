---
type: memory
memory_id: mem-hidden-startup
title: Proxy hidden startup
root_scope: work
primary_scope:
  kind: reference
  id: workflow:hidden-startup
status: active
created: 2026-07-01
updated: 2026-07-24
related_repos: [proxy]
---

# Proxy hidden startup

The scheduled task launches `scripts/launch-hidden.vbs`. The VBScript starts
PowerShell with window style zero, which keeps both the supervisor and child
proxy process in the background without a visible terminal.

