# Retrieval reliability design

## Status and purpose

This document defines the design for complex questions, historical messages, meetings, and large memory collections.
The [architecture guide](architecture.md) describes the complete system.
The [implementation plan](retrieval-reliability-implementation-plan.md) records tasks, algorithms, tests, and release evidence.

The design combines two reviews into one implementation sequence.
It preserves the source storage architecture and replaces unsafe retrieval assumptions.
All examples and capacity profiles in this document are synthetic.

The current scope excludes sentence-derived criterion filtering and automated verification of people, dates, decisions, or exclusions.
Those features are not implementation tasks or release requirements in this plan.
Existing caller-supplied scope arguments, source permissions, and redaction checks remain mandatory.

## Decisions that remain unchanged

- Markdown remains the authority for distilled durable memories.
- The artifact SQLite database remains the authority for source records and revisions.
- Search indexes, context representations, summaries, and graph data remain derived data.
- Source identity, permissions, redactions, and provenance apply to every retrieval path.
- Local retrieval remains the default. No external model or link-fetching service becomes a requirement.
- Graphify remains an internal retrieval provider, not the source of truth.
- A model change requires a compatible derived index and measured improvement.

## Implemented reliability changes

The current generation rule requires an artifact revision to match the published semantic generation.
The implementation permits newer canonical raw search with an older derived index, revision checks, and explicit coverage limits.
It does not permit stale index text to become current source evidence.

The existing two-state response cannot distinguish a missing answer from incomplete retrieval.
The implementation introduces a versioned response contract.
Older clients require a compatibility adapter, not a silent schema change.

Short text, missing absolute timestamps, and speaker changes no longer exclude useful content from semantic retrieval.
Historical discovery no longer receives a default preference for recent events.
Search returns ranked source material, not a guarantee that a record answers every part of a sentence.

## Consolidated work packages

| Package | Findings addressed | Required result |
| --- | --- | --- |
| A. Availability and outcomes | Revision mismatch, timeout masking, raw exact-match gate | Search remains useful during refresh; failures differ from missing answers. |
| B. Coverage and time | Short messages, missing cue timestamps, truncated long records, missing history | Every eligible source segment has a searchable representation or an explicit exclusion reason. |
| C. Context and hierarchy | Speaker splits, meeting-child filters, discarded matching passages | Evidence retains the exact message or passage and its relevant context. |
| D. Query handling | First-24-word cutoff and date serialization | Search uses the full sentence for relevance and accepts valid existing arguments. |
| E. Ranking and results | Old-message penalties, fixed score rules, misleading answer status | Relevant candidates rank well without a claim of semantic proof. |
| F. Scale and operation | Full scans, weak ANN shortlist, cold workers, large refresh copies | Query work, worker memory, and refresh work have measured bounds. |
| G. Evaluation and rollout | Small tests, missing protocol tests, no realistic distractors | Release gates cover correctness, coverage, scale, and real MCP behavior. |

## Target query procedure

1. Validate the request and caller scope.
2. Preserve the full sentence as retrieval input.
3. Apply explicit caller arguments and read source coverage.
4. Pin a canonical read revision and compatible derived snapshots.
5. Retrieve exact, lexical, and semantic candidates within the permitted scope.
6. Retrieve changed raw records through the current canonical search path.
7. Merge duplicate candidates by source identity and revision.
8. Rank a bounded candidate pool against the complete query.
9. Expand source context around each winning passage.
10. Check citation identity, source revision, and access state.
11. Return ranked source material, coverage, and execution state.

Candidate search can use related words. The calling AI assesses whether the returned material answers its question.
The response records any retrieval limit that prevents complete execution of the defined search procedure.
Completing that procedure does not prove that an approximate search examined every possible answer.

## A. Availability and outcome contract

### Canonical search with versioned derived candidates

Raw ingestion updates canonical records, FTS data, and a monotonic change sequence in one transaction.
Each recall pins a canonical read revision, `R`, and a derived generation with watermark `G`.
The query uses only a compatible generation with `G <= R`.
Current exact and lexical search operate on the pinned canonical snapshot.

Derived results carry source IDs, source revisions, representation versions, and dependency digests.
Recall resolves each candidate to canonical records in revision `R` before it returns source text.
A changed, deleted, redacted, or inaccessible dependency invalidates the old contextual representation.
The system must not relabel old semantic text as the current record.

The change journal identifies records after watermark `G`.
A bounded delta index can supply newer semantic candidates when available.
Until then, raw search remains available and the response reports incomplete semantic coverage.
A large backlog produces an explicit coverage gap, not global raw-search failure or an unbounded emergency rebuild.

Deletion and redaction checks remain mandatory for candidates and all contextual dependencies.
If the system cannot validate those checks, it withholds affected evidence.
Worker caches use source revision and visibility state in their keys.

`memory_sync` still publishes compatible derived components atomically.
Recall keeps snapshot leases only for the request lifetime.
An older snapshot can remain available during refresh without claiming that it contains new records.

### Separate execution from evidence

Response version 2 uses three independent fields:

| Field | Values | Meaning |
| --- | --- | --- |
| `execution` | `complete`, `partial`, `failed` | Whether the defined retrieval procedure finished. |
| `result_kind` | `exact`, `ranked`, `empty` | Whether retrieval found a requested identity, ranked candidates, or no candidates. |
| `reason_codes` | Typed reasons | Examples include timeout, coverage gaps, provider failure, or an exhausted search budget. |

`result_kind=exact` means identity resolution, not verification of descriptive claims.
Ranked candidates can accompany partial execution without a claim of exhaustive results.
An empty result with partial execution never means that the requested fact is absent.

Coverage includes requested sources, supported date ranges, known intake gaps, and semantic-index progress.
Coverage timestamps distinguish observed oldest records from a connector-confirmed complete history.
The system cannot infer complete intake merely from a minimum stored date.

The compatibility adapter preserves legacy completed-result behavior during migration.
New clients receive retrieval states without the legacy answer-confidence gate.
Incomplete execution returns an explicit tool error for legacy clients that cannot represent partial results safely.
The new contract exposes useful partial evidence without overloading `no_answer`.
CLI, library, MCP schemas, tests, and canonical skill instructions must migrate together.

## B. Search coverage and time representation

### Stable source units

Every active user-authored message is eligible, including short replies and root-URL messages.
Exclusions apply to explicit system noise or source policy, not text length or URL shape.
Coverage reports include exclusion reasons, failed records, pending records, and representation counts by source and entity.

Long messages and transcripts use overlapping, character-bounded segments.
Segmentation covers the entire source text, including its final segment.
Each segment stores its source revision, stable identity, character offsets, and source-order position.
Representation versioning makes rechunking traceable and invalidates incompatible caches.

Full transcripts without cue records receive derived passages with offsets into the original transcript.
When cues exist, transcript-level and cue-level representations must not count as independent corroboration of the same text.

### Timestamp-free transcript passages

A passage retains separate fields for absolute time, relative start/end offsets, source sequence, and parent event time.
An absolute time is derived only when the source time and offset have compatible meanings.
An unknown time remains unknown. The index does not create a midnight timestamp or current-time replacement.

Source sequence supplies deterministic ordering when time is absent.
If both sequence and time are unavailable, the importer reports the ordering limitation and preserves stable source order where available.
Existing explicit meeting-date arguments use parent event dates for descendant passage retrieval.
Unknown times remain visible in metadata and follow documented existing argument behavior.
The system does not derive date filters from ordinary query sentences.

## C. Message context and meeting hierarchy

Each message remains independently searchable and independently citable.
A second derived representation adds bounded conversation context across speakers.
Explicit reply targets take priority over nearby messages.
Thread boundaries, topic boundaries, and source order prevent unrelated nearby text from becoming asserted context.

Context records identify each speaker and each supporting message.
They retain all source dependencies for invalidation after edits or redactions.
The citation always identifies the original target message, not a generated context paragraph.

Link metadata includes the original URL, available display text, and source-provided preview text.
The system does not infer products from an unrelated domain.
External link fetching is a separate, opt-in ingestion feature with permission and network-safety checks.
It is not a required query-time operation.

Meetings have three retrieval levels: summary, topic section, and original passage.
Every derived level links to its source passages and parent meeting.
Search can enter through any level; a missing summary does not block passage retrieval.
A meeting filter constrains eligible parent meetings and searches their descendants.

Context expansion uses winning segment IDs and offsets, never a heading as a unique key.
The matching passage receives output space before neighboring context.
Additional context uses a bounded read cursor when it cannot fit in the initial response.

## D. Query handling without inferred filters

The full query remains available to semantic retrieval and relevance ranking.
Lexical retrieval uses weighted terms from throughout the query instead of only the first 24 unique words.
It preserves phrases and identifiers as retrieval signals without requiring an LLM planner.
Long inputs use bounded overlapping retrieval windows when a provider cannot encode the full input.

Query expansion adds candidate paths without excluding the original candidates.
An identifier inside a sentence must not silently become an additional metadata restriction.
Explicit identity-only requests retain their direct lookup path.

Existing structured arguments retain their documented scope behavior.
Exact dates serialize through the worker boundary in a canonical, timezone-aware format.
Unsupported argument combinations return a clear validation error rather than a silently narrowed search.
This work does not add new filters or a broader cross-source filter language.

Scope restrictions apply before expansion, context reads, and graph traversal.
These restrictions come from explicit arguments and access controls, not inferred sentence criteria.

## E. Ranking and source results

Candidate fusion uses independent provider ranks, not incomparable accumulated scores from different source types.
Duplicate summaries, passages, and messages contribute no extra independent evidence merely because multiple indexes found them.
A bounded reranker evaluates the complete query against candidate text and source context.

Historical discovery ranks relevance before event age and has no default freshness bonus.
Current-memory retrieval retains existing active and superseded state behavior.
Results expose event time, source provenance, and available memory dates for the calling AI.
No new intent classifier or semantic verifier is required.

The confidence calculation uses the internal candidate pool before display limits apply.
Changing the displayed result count cannot create an artificial semantic margin.
An optional learned reranker can improve relevance, but its score does not prove a source fact.

### Indirect historical link example

The synthetic request asks for a message that linked to apples.
The target message says that the link sells the recipient's preferred fruit.
The URL itself contains no fruit name.

Search uses related concepts to find possible link messages without requiring the word `apples`.
It then reads the reply target and bounded conversation context.
A prior question about apples can supply useful context for ranking and caller assessment.
The evidence packet cites the target message and any context records it returns.
The calling AI determines whether that material supports the intended connection.
The system does not add a preference resolver or a multi-step proof engine.

Coverage metadata exposes known history gaps without a natural-language date parser.

## F. Bounded execution and large-data operation

A bounded pool of supervised worker processes retains model and immutable-index caches.
Each worker handles one active request and releases all request-local database state afterward.
The supervisor replaces a stuck worker after cancellation and verifies process exit and lease release.
Request deadlines include queue time. Admission limits prevent an unbounded queue.

The pool size, cache size, and worker lifetime have explicit memory limits.
Each worker uses request-local scope and revision state to prevent cross-request leakage.
The external deadline remains a containment measure, not a claim about acceptable retrieval speed.
Stage timings and processed-record counters distinguish slow progress from liveness alone.

Generation publication performs full integrity checks once.
Normal recall checks generation identity and indexed change markers without walking every Markdown file or hashing the full graph.
Background reconciliation detects missed filesystem changes.
Freshness state records the last reconciliation and known change backlog.

ANN evaluation compares candidate recall with exact vector search on the same embeddings and scope filters.
Index selection considers recall, latency, memory, update cost, and platform support.
A small final candidate list does not count as proof of bounded SQL work.
Large exact fallbacks require an explicit work budget and cannot load all vectors without a bound.

Small exact searches remain useful as correctness baselines and bounded fallbacks.
If the large-corpus budget expires, the response exposes incomplete semantic retrieval.
Candidate queries fetch compact IDs and scores before they fetch source text.

Incremental refresh tracks changed source units and their context dependencies.
An append to a long conversation recomputes affected boundary windows, not the complete conversation history.
Each publication adds an immutable delta segment and a small manifest.
The manifest binds each segment to its size and SHA-256.
Readers validate each unchanged segment once per process.
Queries consult at most eight segments.
Background compaction starts at four segments and copies at most 512 rows per block.
Compaction has a 120-second work budget and a 20-million-row limit.
The next publication uses the compacted prefix and retains newer deltas.
At eight segments, publication requires successful compaction before another delta.
An indexed source-order table supports bounded predecessor and successor lookups.
Intake updates this derived table in the canonical transaction.

## G. Implementation sequence and release gates

### Phase 0: baseline and contract

1. Record the exact implementation revision and supported clients.
2. Preserve existing changes and establish isolated synthetic fixtures.
3. Convert the in-scope findings into failing regression tests.
4. Measure current cold and warm MCP behavior before tuning.
5. Define the versioned response and compatibility tests.

The baseline separates corpus coverage, candidate quality, ranking quality, and runtime cost.
Private runtime counts remain local operational evidence, not public fixtures.

### Phase 1: availability, dates, and coverage

1. Correct worker date serialization.
2. Keep canonical exact and lexical search available during semantic refresh.
3. Add revision validation, outcome fields, and coverage reporting.
4. Index short messages and timestamp-free passages.
5. Segment complete long records without dropping their endings.

The gate requires unchanged records to remain retrievable after unrelated intake.
It also requires redacted or changed dependencies to invalidate old contextual evidence.
Every active source segment has an indexed, pending, failed, or explicitly excluded state.

### Phase 2: context, hierarchy, and query handling

1. Add stable passage identities and winning-passage context expansion.
2. Add reply-aware context across speakers.
3. Add parent-meeting filters and source-time semantics.
4. Correct full-query lexical handling without adding inferred filters.
5. Return ranked source results without a semantic-proof claim.

The gate rejects cross-scope citations and preserves winning source passages.
Quality tests measure relevant-result rank without requiring perfect interpretation of sentence criteria.

### Phase 3: ranking and operational scale

1. Correct provider fusion and display-limit-dependent confidence.
2. Compare ANN candidate selection with the exact baseline.
3. Add bounded warm workers and remove full-corpus query-time checks.
4. Bound incremental refresh, delta backlog handling, and index compaction.
5. Measure optional local embeddings and rerankers after coverage fixes.

Model or index selection requires an improvement on held-out queries within measured resource limits.
The benchmark selects a provider only after it records quality and resource results.

### Phase 4: controlled rollout

1. Build new derived representations beside the existing generation.
2. Compare old and new retrieval on the same read-only fixtures.
3. Validate real MCP clients on all supported operating systems.
4. Run authorized local evaluation against private source data without exporting it.
5. Switch the default only after correctness and resource gates pass.

Rollback changes the active derived generation and compatible worker configuration.
Rollback does not revert canonical intake or resurrect deleted or redacted evidence.
No phase requires deletion of user files or source records.

## Evaluation contract

### Synthetic capacity profiles

| Profile | Messages | Meetings | Transcript passages | Memory documents |
| --- | --- | --- | --- | --- |
| Regression | 1,000 | 20 | 2,000 | 100 |
| Workload | 50,000 | 1,000 | 100,000 | 2,500 |
| Growth | 500,000 | 5,000 | 1,000,000 | 25,000 |

These are synthetic test sizes, not measured product capacity or actual customer counts.
Large profiles run outside the fast unit suite.
Fixtures include long individual conversations, uneven activity, duplicate material, and dense semantic distractors.

### Required query families

- Old links with indirect wording, root URLs, and unrelated domains.
- Short question-and-reply exchanges across speakers.
- Answers at the beginning, middle, and end of long messages or transcripts.
- Timestamp-free cues, relative offsets, and serialization of explicit timezone-aware date arguments.
- Similar meetings and unrelated conversations as ranking distractors.
- Long descriptive queries with useful terms near the end.
- Current and superseded memories with source links and historical validity.
- Missing answers, missing intake history, permission exclusions, and redacted context.
- Continuous intake during queries, worker crashes, stalled providers, and concurrent callers.
- Changing the displayed result limit without creating artificial ranking confidence.

### Measurements and gates

The suite measures coverage by source unit, not only by burst count.
It measures ANN candidate recall, correct-message recall at 5 and 10, rank quality, and irrelevant-result rate.
It separately measures citation correctness, explicit scope behavior, and missing-history reporting.

The deterministic regression suite requires all safety and evidence-preservation cases to pass.
It permits zero cross-scope citations, redaction leaks, discarded winning passages, or timeout-as-empty results.
Semantic release targets require a held-out labeled query set with realistic near-miss candidates.
Threshold selection uses the baseline and records the accepted tradeoffs before release evaluation.

Performance measurements include end-to-end MCP p50, p95, and p99 latency, queue delay, peak memory, cancellation time, and refresh throughput.
Runs include concurrent clients, active ingestion, cold workers, warm workers, and growth profiles.
Hardware-specific latency and memory budgets must be recorded before rollout.
An arbitrary 30-second cutoff cannot substitute for these measurements.

The benchmark runner must exercise serialized MCP requests as well as direct library calls.
The existing frozen benchmark remains unchanged; new cases use a separate versioned contract.
No performance or retrieval-quality claim is complete until the corresponding run finishes and records its corpus and configuration.

## Implementation areas

- `server.py`, `recall_worker.py`, `models.py`: request serialization, worker lifecycle, response compatibility, and error states.
- `service.py`, generation management: canonical revision pins, derived watermarks, delta coverage, and scope routing.
- `artifacts/models.py`, import adapters: source sequence, event times, relative offsets, identities, and coverage state.
- `artifacts/bursts.py`, `artifacts/vector_index.py`: complete segmentation, contextual representations, dependency invalidation, and incremental updates.
- `artifacts/search.py`, artifact context readers: parent-aware search, matching excerpts, and exact source citations.
- `text.py`, `retrieval.py`, `index.py`, `ann.py`: query handling, candidate selection, fusion, citation integrity, and bounded work.
- `embedding.py`: provider comparisons and representation compatibility.
- `tests/`, `benchmarks/`: regression cases, protocol tests, quality metrics, concurrency tests, and growth profiles.
- `skill/ai-memory/`: retrieval-state interpretation, ranked-source handling, source reading, and freshness guidance.

These areas identify implementation ownership. They are not a requirement to retain every current module boundary.
