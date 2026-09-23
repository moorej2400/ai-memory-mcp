# Vault Migrations

The AI designs and performs each vault migration.
The migration command supplies backups, an operation log, verification, and rollback.

The command does not select folders, collections, record types, or links.
The AI makes these decisions after it reads the applicable notes.

## Migration data

The command stores state in this directory:

```text
<vault>/.ai-memory/migration/<migration-id>/
  manifest.json
  verification.json
  backup/
  rollback-created/
  rollback-conflicts/
```

Keep this directory in normal backup coverage.

## Inspect and start

Inspect the vault:

```text
ai-memory-migrate --root <vault> inspect
```

Read the notes that can change.
Then choose the target architecture from their meaning and the user requirements.

Start the migration:

```text
ai-memory-migrate --root <vault> start --id <migration-id>
```

The command records the initial files and the current quality report.

## Edit a file

Snapshot an existing file before the AI changes its content:

```text
ai-memory-migrate --root <vault> snapshot --id <migration-id> <path>
```

Edit the file directly.
Then record the completed edit:

```text
ai-memory-migrate --root <vault> record-edit --id <migration-id> <path>
```

Use this sequence for frontmatter changes, links, merges, and content updates.

## Create a file

Snapshot the missing target before the AI creates it:

```text
ai-memory-migrate --root <vault> snapshot --id <migration-id> <path>
```

Create the file directly.
Then record the completed creation:

```text
ai-memory-migrate --root <vault> record-create --id <migration-id> <path>
```

Rollback moves a recorded creation into `rollback-created/`.
Rollback does not delete the file.

## Move a file

Choose the destination after the AI reads and classifies the note.
Then use the safe move command:

```text
ai-memory-migrate --root <vault> record-move --id <migration-id> <source> <destination>
```

The command rejects unsafe paths and existing destinations.
The command backs up the source before movement.
The command records progress before it moves the file.

Update affected frontmatter and links as separate recorded edits.

## Verify and finish

Read the current state:

```text
ai-memory-migrate --root <vault> status --id <migration-id>
```

Complete each prepared operation.
Then verify the migration:

```text
ai-memory-migrate --root <vault> verify --id <migration-id>
```

Verification checks recorded hashes, move state, pending operations, and new quality issues.
The command marks the migration `verified` only when these checks succeed.

Run `memory_sync` after verification.
Then test the important recall queries and Obsidian views.

## Roll back

Roll back a migration:

```text
ai-memory-migrate --root <vault> rollback --id <migration-id>
```

Rollback restores recorded edits and reverses recorded moves.
Rollback preserves recorded creations in the migration directory.
Rollback preserves later file content in `rollback-conflicts/` before restoration.

If an edit stopped before completion, rollback preserves its current content in `rollback-conflicts/`.
