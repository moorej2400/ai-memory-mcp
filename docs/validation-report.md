# Validation Report

Date: 2026-07-28

Status: satisfactory

## System boundary

- Markdown is the canonical authority.
- AI Memory MCP is the public agent interface.
- SQLite and Graphify are internal providers.
- The Graphify version is 0.9.26.
- The MCP package uses major version 1.

## Automated tests

All 28 automated tests passed.
The tests covered retrieval, MCP tools, client merging, backups, OpenCode schemas, skill links, and repository privacy.

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
| Median latency | 6.75 ms |
| 95th percentile latency | 12.53 ms |

The frozen contract SHA-256 value is:

`e6a13efda90f9a654b702c9c5161f2bbb97aff5e2de145bd060ab537b0753e0c`

## Transport tests

The standard input and output transport passed.
The server supplied three public tools.
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

The repository is a complete source distribution.
Generated state and user memory stay outside Git.
The privacy gate found no private path, email address, common token shape, or configured private term in commit-eligible files.
