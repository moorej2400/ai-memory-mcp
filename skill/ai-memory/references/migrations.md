# Vault Migration Workflow

Use this workflow for a legacy vault or a schema change.
The migration ledger is the authority for migration state.
The AI is the authority for semantic migration decisions.

## Rules

- Keep the source vault in its current backup coverage.
- Use one migration ID for one migration run.
- Read each applicable note before you classify it.
- Snapshot a file before you edit or create it.
- Use the migration command for every move.
- Do not infer note meaning from a legacy folder name.
- Keep unclassified notes in place.
- Do not remove migration backups after verification.

## Procedure

1. Run `ai-memory-migrate --root <vault> inspect`.
2. Review schema issues, duplicate IDs, broken links, and ambiguous links.
3. Read the notes that can change.
4. Design the target architecture from the note contents and user requirements.
5. Run `ai-memory-migrate --root <vault> start --id <migration-id>`.
6. Snapshot each file before an edit or creation.
7. Record each completed edit or creation.
8. Use `record-move` for each selected move.
9. Update affected metadata and links through recorded edits.
10. Run `ai-memory-migrate --root <vault> verify --id <migration-id>`.
11. Run `memory_sync`.
12. Test recall, links, and applicable Obsidian views.

Use `rollback` if verification finds a material error:

```text
ai-memory-migrate --root <vault> rollback --id <migration-id>
```

The migration command keeps file backups under the migration run directory.
The command does not overwrite an existing destination.
The rollback command preserves later content under `rollback-conflicts/`.
The AI updates links after it decides which relationships remain useful.

Use these commands around direct AI edits:

```text
ai-memory-migrate --root <vault> snapshot --id <migration-id> <path>
ai-memory-migrate --root <vault> record-edit --id <migration-id> <path>
ai-memory-migrate --root <vault> record-create --id <migration-id> <path>
ai-memory-migrate --root <vault> record-move --id <migration-id> <source> <destination>
```

## AI Review Decisions

Move a note to `Notes/` when it owns one durable topic.
Move a note to a collection when typed fields or a Base view add value.
Split a large note only after you identify independent update boundaries.
Use the snapshot backup to preserve the original content.

Move reusable agent procedures to the skill workflow.
Keep human procedures as ordinary memory when they do not change agent behavior.

Promote useful session content into topic notes.
Leave a session in place when it still supports active handoff work.
Use `no-durable-memory` for reviewed artifacts with no future value.
