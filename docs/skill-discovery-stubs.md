# Skill Discovery Stubs

This repository contains two canonical skills:

- `skill/ai-memory/SKILL.md`
- `graphify-codebase/skill/graphify/SKILL.md`

AI harnesses must contain discovery stubs instead of canonical skill copies.
A stub carries only the metadata a host needs to discover and trigger the
skill, then redirects to the canonical `SKILL.md`. This keeps one source of
truth and survives repository moves.

Use this stub pattern:

```markdown
[Add any host-specific skill metadata here if the target platform expects a
header before YAML frontmatter]

---
name: <canonical-name>
description: <copy the exact canonical description>
---

Before following any instruction in this stub, first check the canonical skill
header in '<canonical-path>'. If the source skill metadata has changed and this
stub is out of date, update this stub to match the current source skill
metadata before proceeding.

Then read the SKILL.md in full from '<canonical-path>'
```

Rules:

- Keep the stub folder name exactly the same as the canonical skill folder.
- Copy the canonical `description` exactly, so host triggering is unchanged.
- Copy any other metadata the target platform requires for discovery, in the
  header format and location that platform expects.
- The stub must tell the agent to compare its own header against the canonical
  header and update itself whenever the canonical metadata changes. A stale
  description stops a host from triggering the skill at all.
- Never copy the canonical skill body into a stub.

Run `.\scripts\install-clients.ps1` (or `./scripts/install-clients.sh`) after a
clone or repository move.
The installer writes the correct canonical path into each stub.

Read the [Graphify Codebase guide](../graphify-codebase/README.md) for its independent boundary.
