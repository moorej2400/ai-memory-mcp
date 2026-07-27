# Frozen retrieval benchmark

`cases.json`, `fixtures/vault/**/*.md`, and `fixtures/graph.json` form the
benchmark contract. `benchmark-lock.json` records their combined SHA-256.

After the lock is created, performance work changes the implementation—not
these cases. The runner refuses to execute if any contract file drifts.

The suite covers exact identifiers, paths, error strings, paraphrases,
multi-hop relationships, active versus superseded facts, scope isolation,
citations, missing answers, and Graphify-backed neighbors.

