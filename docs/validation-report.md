# Validation Report

Date: 2026-08-13

Status: satisfactory

## System boundary

- Markdown is the canonical authority for durable knowledge.
- The artifact SQLite database is the canonical authority for raw artifacts.
- AI Memory MCP is the public agent interface.
- The Markdown index, artifact burst index, and Graphify data are derived.
- The Graphify version is 0.9.26.
- The MCP package uses major version 1.

## Automated tests

All 359 automated tests passed.
One platform-specific test was skipped.
The tests covered artifact intake, retrieval, distillation, migration, recovery, MCP tools, client merging, and repository privacy.
All eight provider TypeScript test files passed.
The provider shell setup test passed.
The artifact database uses schema version 3.

## Frozen benchmark

The benchmark contains 20 retrieval cases.
All 20 cases passed.

| Metric | Result |
|---|---:|
| Recall at 1 | 1.000 |
| Recall at 5 | 1.000 |
| Mean reciprocal rank | 1.000 |
| No-answer accuracy | 1.000 |
| Scope leakage | 0.000 |
| Citation failures | 0.000 |
| Median latency | 2.60 ms |
| 95th percentile latency | 4.27 ms |

The frozen contract SHA-256 value is:

`e6a13efda90f9a654b702c9c5161f2bbb97aff5e2de145bd060ab537b0753e0c`

## Artifact benchmark

The artifact benchmark used only synthetic data.
The fixture contained 100 conversations, 100,000 messages, 100 meetings, 100 transcripts, and 50,000 transcript cues.

| Metric | Result |
|---|---:|
| Batch validation | 8.531 s |
| SQLite intake | 38.639 s |
| Active SQLite files | 809,499,920 bytes |
| Warm FTS median | 1.275 ms |
| Warm FTS 95th percentile | 1.311 ms |
| Ordered-read median | 3.521 ms |
| Ordered-read 95th percentile | 3.825 ms |
| Burst-index build | 23.252 s |
| Warm fused-recall median | 194.069 ms |
| Warm fused-recall 95th percentile | 258.923 ms |

The burst index contained 18,800 deterministic bursts.
All 18,800 bursts had hashed vectors.

The JSONL fixture SHA-256 value is:

`380e2e23d1a387771377af75e57f073d44e2658b927555b99bb0b207c977b727`

The measurements came from one local validation run.
The measurements do not define performance limits.
The database passed quick-check and foreign-key validation.
A verified backup restored to a new path with the same SHA-256 value.

## Transport tests

The standard input and output transport passed.
The server supplied four public tools.
The `memory_status` call completed without an MCP error.
The read tools did not create a missing index.

## Public response size

A representative five-result recall used 30.3 percent fewer bytes.
The comparison used the previous internal evidence packet.

## Graphify provider

The repository-local Graphify runtime passed the version check.
The runtime package, CLI, MCP server, and skill reported version 0.9.26.
The fixture graph adapter tests passed.
The public validation report does not contain information from a live memory corpus.

## Installation

The setup and client installers passed isolated tests.
The tests covered these clients:

- Codex
- Claude Code
- Claude Desktop
- GitHub Copilot CLI
- OpenCode
- Visual Studio Code

The tests verified configuration merging, repository skill links, and repeatable installation.

## Result

The repository is a complete source distribution for Markdown and raw artifact retrieval.
Generated state and user memory stay outside Git.
The privacy gate found no private path, email address, common token shape, or configured private term in commit-eligible files.
