---
name: graphify
description: Use only for software-repository indexing and questions about code architecture, symbols, calls, dependencies, or file relationships. Do not use for memory recall, memory storage, user preferences, session history, personal knowledge, or durable agent learning.
---

# Graphify Codebase

Use Graphify only to index and query software repositories.
Treat `graphify-out` as derived codebase data.

This skill is independent from AI Memory MCP.
Use the `ai-memory` skill for every durable-memory request.

## Boundary

- Index source code and repository documentation that explains the code.
- Query architecture, symbols, calls, dependencies, and file relationships.
- Do not store user questions or agent answers.
- Do not store preferences, corrections, lessons, outcomes, or session history.
- Do not call `graphify save-result`.
- Do not call `graphify reflect`.
- Do not use Graphify as a memory source.
- Do not index a configured AI Memory vault with this skill.

If a request uses words such as remember, recall, memory, preference, lesson, or session history, use `ai-memory`.

## Find the graph

Resolve the target repository from the user request.
Use the current repository when the user gives no path.

If `graphify-out/graph.json` exists, query that graph.
Do not rebuild the graph unless the user requests a build or update.

## Build

Use the repository wrapper when it is available:

```powershell
.\graphify-codebase\scripts\invoke-graphify-codebase.ps1 -Mode Build -Path 'C:\path\to\repository'
```

```bash
./graphify-codebase/scripts/invoke-graphify-codebase.sh --mode build --path /path/to/repository
```

Otherwise, run:

```text
graphify extract <repository-path>
```

Do not include directories outside the target repository.
Do not add AI Memory vaults to the input.

## Update

Use this command for changed repository files:

```powershell
.\graphify-codebase\scripts\invoke-graphify-codebase.ps1 -Mode Update -Path 'C:\path\to\repository'
```

```bash
./graphify-codebase/scripts/invoke-graphify-codebase.sh --mode update --path /path/to/repository
```

## Query

Use this command for an existing graph:

```powershell
.\graphify-codebase\scripts\invoke-graphify-codebase.ps1 -Mode Query -Path 'C:\path\to\repository' -Question '<question>'
```

```bash
./graphify-codebase/scripts/invoke-graphify-codebase.sh --mode query --path /path/to/repository --question '<question>'
```

Answer from graph evidence and the applicable source files.
State when the graph does not contain sufficient evidence.

Do not save the answer back into Graphify.

## Verify

- Confirm that the target is a software repository.
- Confirm that Graphify wrote only derived output.
- Confirm that no memory vault entered the scan.
- Confirm that no answer, lesson, or correction was saved.
