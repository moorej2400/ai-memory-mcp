# Retrieval reliability implementation plan

## 1. Status, scope, and operating rules

This document records implementation work, exit evidence, and remaining evaluation tasks.
A checked exit condition has automated test or benchmark evidence.
It implements the revised [retrieval reliability design](retrieval-reliability-design.md).
The [architecture guide](architecture.md) describes current behavior until implementation changes land.

The goal is reliable retrieval from messages, meeting transcripts, and durable memories as the corpus grows.
The system must return useful source material without losing short replies, old messages, long-record endings, or matching passages.
It must distinguish incomplete execution from a completed search with no results.

### Explicit scope boundary

Sentence-derived criterion filtering and semantic verification of people, dates, decisions, and exclusions are outside this plan.
This plan adds no LLM query planner, natural-language date parser, preference resolver, or semantic-proof engine.
It adds no release gate that requires perfect interpretation of a complex sentence.
Search uses sentences for relevance. The calling AI assesses whether returned material answers the question.

Existing explicit scope arguments, source permissions, and redaction rules remain in force.
Existing date serialization and meeting-descendant search defects remain in scope because they break existing API behavior.
This plan does not add new filter types or expand the cross-source filter language.

### Source and repository safety

1. Preserve canonical Markdown, raw artifacts, revisions, and source identities.
2. Treat embeddings, passages, context representations, and graph data as derived data.
3. Keep all fixtures synthetic and independent from private runtime data.
4. Keep configuration, reports, logs, and private measurements in ignored local storage.
5. Preserve unrelated working-tree changes throughout implementation.
6. Do not delete user files or source records during migration, cleanup, or rollback.
7. Obtain separate authorization before any file deletion or public push.
8. Keep source and context access checks on every retrieval path.
9. Preserve the existing frozen benchmark contract.
10. Document non-obvious revision, cancellation, and source-integrity invariants beside the code that enforces them.

### Recorded baseline

The earlier review used a published fix branch with a supervised recall worker.
The local `main` source inspected during planning did not contain that worker module.
The baseline also contained unfinished local benchmark changes.
T00 reconciled these differences before the worker and benchmark implementation changed.
The implementation preserved unrelated local changes.

## 2. Delivery sequence

Each task includes implementation steps, tests, and an exit condition.
Task numbers define the normal sequence, not permission to skip failed gates.
Small correctness fixes can ship before model selection or the largest capacity run.

| Task | Deliverable | Dependencies |
| --- | --- | --- |
| T00 | Reconciled baseline and isolated test harness | None |
| T01 | Date serialization and honest retrieval outcomes | T00 |
| T02 | Canonical raw search during derived-index lag | T01 |
| T03 | Source-order metadata and coverage accounting | T02 |
| T04 | Complete segmentation and short-message inclusion | T03 |
| T05 | Reply-aware context and meeting hierarchy | T04 |
| T06 | Winning-passage preservation and stable citations | T04; T05 for contextual hits |
| T07 | Full-sentence lexical and semantic input handling | T01 |
| T08 | Comparable fusion and historical ranking | T05, T06, T07 |
| T09 | Bounded vector retrieval and measured ANN quality | T04, T08 |
| T10 | Warm supervised workers and query-path cost removal | T01, T02, T09 |
| T11 | Incremental representation updates and segmented publication | T02, T03, T05 |
| T12 | Meeting discovery and durable-memory source links | T05, T06, T08 |
| T13 | Local model and reranker comparison | T08, T09; T10 for final latency |
| T14 | End-to-end quality, growth, and failure evaluation | All enabled implementation tasks |
| T15 | Client migration, documentation, deployment, and rollback proof | T14 |

## 3. Internal contracts

These structures describe required semantics. Proposed names can change if existing types already supply the same contract.
An implementation must not add duplicate state when an existing table or type is authoritative.

### 3.1 Retrieval response

The new response separates execution from the presence of source results.

| Field | Meaning |
| --- | --- |
| `response_version` | Explicit version of the response schema. |
| `execution` | `complete`, `partial`, or `failed`. |
| `result_kind` | `exact`, `ranked`, or `empty`. |
| `evidence`, `citations` | Source excerpts and exact source references. |
| `reason_codes` | Typed causes, not warnings that callers must parse. |
| `coverage` | Layer availability, index lag, and known source-history coverage. |
| `warnings` | Short explanations for human readers. |

`exact` means that a requested source identity resolved. It does not prove additional descriptive claims in a sentence.
`ranked` means that retrieval returned candidates. It is not an answer-confidence certificate.
`empty` means that the executed retrieval paths returned no candidates.
An empty partial or failed response never establishes that a memory is absent.

Reason codes include `deadline_exceeded`, `cancelled`, `queue_full`, `semantic_unavailable`, `semantic_lag`, and `search_budget_exhausted`.
Source-history gaps belong in coverage data even when execution completes.
Execution completeness describes the requested procedure, not an exhaustive proof across all possible meanings.

### 3.2 Source segment

Each derived segment contains these fields:

- Stable segment identity and representation version.
- Source artifact or Markdown identity and source revision.
- Parent conversation, transcript, and meeting identities when available.
- Start and end character offsets in the source text.
- Deterministic source-order key, independent from update order.
- Optional absolute time and optional relative start/end offsets.
- Raw text reference and compact embedding representation reference.
- Source dependency identities and their revision digests.

A segment ID can derive from source identity, source revision, representation version, and offsets.
Source citations remain stable even if a new representation changes segmentation.
Display headings are labels, not unique chunk identities.

### 3.3 Representation coverage

Each eligible source unit has one explicit processing state.
States include `indexed`, `pending`, `failed`, `excluded`, and `empty_content`.
The system records a reason for every exclusion or failure.
An artifact can have several segments, but it counts once in artifact-level coverage.
Segment-level coverage separately accounts for every span of nonempty source text.

Observed history bounds, connector coverage claims, and semantic coverage remain separate measurements.
No code can infer complete historical intake from the oldest stored timestamp alone.

### 3.4 Revision and dependency contract

Canonical revision `R` identifies the source database snapshot for a recall.
Derived watermark `G` identifies the source revision represented by an index component.
A usable derived generation has `G <= R` and a compatible representation version.
Each returned candidate resolves to the permitted canonical source revision before response construction.

Context representations also depend on their reply targets and surrounding source records.
An edit, redaction, or access change to any dependency invalidates the affected representation.
Source revisions and dependency digests must not disappear during fusion or context expansion.

### 3.5 Diagnostics and resource controls

Each request records queue, generation, lexical, semantic, fusion, reranking, context, citation, and response-construction stages.
Each stage records start time, end time, outcome, input count, output count, and relevant work counters.
Vector counters include vectors scored and blocks read. Context counters include source records read and returned characters.
Refresh counters include changed units, reused embeddings, new embeddings, pending units, and bytes written.

The request ID connects parent, worker, and provider events without recording raw private queries by default.
Diagnostic payloads must not contain source text, credentials, or private URLs unless local diagnostic policy explicitly permits them.
Elapsed time alone cannot establish progress. A heartbeat without increasing work counters remains a liveness signal only.

Configuration must define worker count, queue capacity, cache memory, candidate limits, context size, and vector work budgets.
The implementation validates ranges and rejects configurations that exceed supported resource bounds.
Defaults come from the measured workload profile. They are not arbitrary copies of the earlier deadline value.
The response records which budget ended incomplete work so operators can distinguish capacity limits from a stalled provider.

## 4. T00: reconcile the baseline and create the test harness

### Files and integration points

Existing areas: `tests/conftest.py`, `tests/test_mcp.py`, `tests/test_generations.py`, and `benchmarks/`.
The unfinished `real_world_benchmark.py` and its tests require inspection before integration.
The reviewed worker branch requires a diff against the selected implementation base.

### Procedure

1. Record the selected commit, branch, installed package path, and runtime entry point.
2. Compare the published worker fix with the selected base.
3. Preserve any fix already present under another implementation.
4. Record unrelated changes without modifying them.
5. Run the existing fast tests and record failures before adding new tests.
6. Create fixtures with isolated vault, database, objects, index, graph, and log paths.
7. Fix the test clock, random seeds, source ordering, and source identities.
8. Block unintended network use in deterministic unit tests.
9. Add an MCP harness that starts a real server process and sends serialized tool requests.
10. Capture response bodies, process exit, worker lifecycle, and cleanup state.

The harness must not call the service directly when testing transport behavior.
Direct service tests remain useful for isolating retrieval algorithms.
The benchmark records both routes so their latency measurements cannot be confused.

### Initial regression fixtures

- A short root-URL message and an otherwise identical URL with a path.
- A question and reply from different speakers.
- A long Markdown section with the target passage in a later chunk.
- A long message with the only relevant passage at its end.
- A meeting with matching transcript text but no matching meeting title.
- Transcript cues with relative offsets and no absolute timestamp.
- An indexed record followed by unrelated intake before another sync.
- A long sentence whose useful terms occur after the first 24 unique words.
- An old relevant message among newer unrelated messages.

### Exit condition

- [x] The selected implementation base and worker behavior are unambiguous.
- [x] Fixtures cannot read or modify the configured user vault.
- [x] Existing failures are distinguished from new regression failures.
- [x] The protocol harness reproduces the date or timeout boundary defect when the selected base contains it.

## 5. T01: correct serialization and retrieval outcomes

### Files and integration points

Modify `models.py`, `server.py`, `service.py`, and the worker boundary on the selected base.
Use `tests/test_mcp.py`, `tests/test_answer_gate.py`, and new protocol regression tests.
Any new worker test module is a proposed file, not an existing dependency.

### Request serialization

1. Define one typed request envelope for the parent and worker.
2. Serialize that envelope with JSON-mode model serialization.
3. Normalize existing date arguments to timezone-aware ISO values.
4. Parse the same envelope inside the worker.
5. Reject invalid values before database work starts.
6. Preserve explicit offsets and UTC equivalence through the round trip.
7. Apply the existing argument validation consistently in library and MCP paths.

Plain `json.dumps` must not receive live `datetime` or `Path` objects.
A generic string conversion is not an acceptable substitute because it hides invalid types.
No sentence parsing belongs at this boundary.

### Outcome migration

1. Add the versioned retrieval response from Section 3.1.
2. Keep legacy completed-response behavior behind an explicit compatibility adapter.
3. Return an explicit tool error when a legacy client cannot safely represent incomplete execution.
4. Return ranked candidates to new clients without the raw exact-phrase answer gate.
5. Keep exact source lookup distinct from broad ranked retrieval.
6. Preserve cancellation and failure causes through worker, service, and MCP layers.
7. Include useful partial evidence only when source checks completed safely.

Schema selection uses an explicit response-version argument during client migration.
The implementation must test the declared MCP output schema for both supported versions.
Unsupported versions fail clearly instead of receiving a different shape silently.

### Tests and exit condition

- [x] ISO date arguments survive a real MCP request and worker round trip.
- [x] Naive or malformed dates follow a documented validation rule.
- [x] A completed empty search differs from timeout, cancellation, and provider failure.
- [x] Raw paraphrase candidates remain available without an exact quoted query.
- [x] Legacy clients receive explicit failures instead of false `no_answer` success packets.
- [x] Changing the result count does not change execution state.

## 6. T02: keep canonical search available during index lag

### Files and integration points

Modify `service.py`, `generation.py`, `artifacts/store.py`, `artifacts/schema.py`, and `artifacts/vector_index.py`.
Use existing artifact events, batch receipts, change counters, and current-record identities where possible.
Extend `tests/test_generations.py`, `tests/test_artifact_retrieval.py`, and ingestion-security tests.

### Change journal

The current change counter advances at the end of a changing batch.
A new journal must use that same committed revision and preserve batch idempotence.
It must not increment a second counter independently or depend on inconsistent trigger timing.

1. Allocate the next revision once for a changing transaction.
2. Record each changed artifact and affected parent in that transaction.
3. Include edit, redaction, tombstone, coverage, and ancestor effects.
4. Commit canonical records, FTS updates, journal entries, and the watermark together.
5. Leave unchanged or rejected replays without a new effective revision.
6. Index journal lookups by revision and artifact identity.

A proposed journal key is `(revision, ordinal)` with artifact ID, latest event ID, operation, and affected parent.
Existing event tables can supply this contract if their committed ordering is sufficient.
Migration must use the next available schema version on the selected base.

### Recall algorithm

1. Pin a compatible derived manifest.
2. Open a read snapshot of canonical source revision `R`.
3. Execute exact lookup and raw FTS against that snapshot even when `R > G`.
4. Retrieve available semantic candidates from watermark `G`.
5. Fetch current source revisions for candidate dependencies in one bounded batch.
6. Discard stale representations without discarding unchanged candidates from the same generation.
7. Resolve retained candidates to canonical source text.
8. Report semantic lag and missing semantic coverage explicitly.

The first implementation can leave newer records lexical-only until sync.
A bounded delta semantic index is part of T11, not a prerequisite for restored raw availability.
The system must not embed an entire backlog synchronously inside recall.

Redaction safety requires a final visibility check before response emission.
If visibility changed after the pinned snapshot, the response drops affected candidates or restarts within its budget.
It does not return an old contextual excerpt after a newer redaction invalidates a dependency.

### Tests and exit condition

- [x] An unchanged old record remains retrievable after unrelated intake.
- [x] A newly ingested record appears through raw FTS before semantic refresh.
- [x] A changed message cannot return an old semantic excerpt as current source text.
- [x] Redacting a reply target invalidates the reply's contextual representation.
- [x] Parent redaction invalidates descendant evidence.
- [x] Concurrent intake does not mix source revisions within one evidence packet.
- [x] Failed sync preserves valid prior derived data without disabling current raw search.

## 7. T03: preserve source order and expose coverage

### Files and integration points

Modify `artifacts/models.py`, import adapters, `artifacts/schema.py`, `artifacts/vector_index.py`, and status models.
Inspect existing `CoverageClaim` and `artifact_coverage` behavior before introducing another coverage table.
Keep search-coverage reporting separate from intake completeness claims that can affect source retention.

### Time and order representation

1. Make derived passage absolute timestamps optional.
2. Add normalized relative start/end offsets and source-position fields where the provider supplies them.
3. Preserve original time values and their provenance in raw payloads.
4. Use explicit transcript position first for passage ordering.
5. Use compatible relative offsets when explicit position is absent.
6. Use a stable import ordinal only as a documented fallback.
7. Keep unknown time values null.

An event's update sequence is not necessarily its position in a transcript.
The implementation must not repurpose revision-order `source_sequence` as cue order without checking the provider contract.
Absolute time derivation requires a compatible parent start time and relative offset.
Order-only passages remain eligible for semantic retrieval.

### Coverage accounting

1. Record source-unit processing state and exclusion reasons during indexing.
2. Count unique eligible artifacts separately from derived segments and contextual windows.
3. Track failures and retry state without dropping them from the denominator.
4. Expose observed dates separately from confirmed provider coverage intervals.
5. Report missing absolute timestamps as metadata availability, not automatic search exclusion.
6. Add bounded aggregate queries for `memory_status`.

Empty content, explicit system noise, policy exclusions, missing objects, and parse failures need distinct counts.
Old coverage reports must not claim that a new representation covers content it has not processed.

### Tests and exit condition

- [x] Cues without absolute timestamps enter semantic indexing.
- [x] Source order remains stable across repeated imports and index rebuilds.
- [x] Relative offsets do not overwrite canonical event dates.
- [x] Coverage totals reconcile with source-unit processing states.
- [x] A stored earliest date does not become a complete-history claim.
- [x] Coverage reporting does not trigger source deletion or redaction behavior.

## 8. T04: index short messages and complete long records

### Files and integration points

Modify `artifacts/bursts.py`, `artifacts/vector_index.py`, `artifacts/models.py`, and shared text segmentation helpers.
Extend `tests/test_artifact_bursts.py`, `tests/test_artifact_vector_index.py`, and `tests/test_index_and_text.py`.

### Base representations

Every useful message receives an independently searchable base representation.
Length, reactions, and URL path shape no longer determine whether it is eligible.
System-noise classification can still exclude content under an explicit policy with a recorded reason.

1. Replace the 200-character eligibility shortcut with explicit source-content eligibility.
2. Include root URLs and available provider link text consistently.
3. Split oversized messages into ordered segments instead of truncating the first burst.
4. Segment full transcripts when cue records are unavailable.
5. Preserve headings and speaker labels as metadata without consuming all source-text capacity.
6. Store complete source offsets for every segment.
7. Record the segmenter's version in index metadata and each representation identity.

### Segmentation algorithm

The initial implementation uses paragraph or cue boundaries with a provider-aware maximum token budget.
It adds a bounded overlap to preserve explanations that cross a segment boundary.
A long paragraph receives a secondary split at sentence or token boundaries.
The final remainder always becomes a segment, even when short.

When a provider exposes no tokenizer, the adapter uses a documented conservative estimate and checks its input limit.
Silent provider truncation is unacceptable.
Chunk size and overlap remain measured configuration values, not unrelated constants in each indexer.

The correctness invariant is complete offset coverage of nonempty source text.
Overlap is allowed; missing spans are not.
A source digest distinguishes repeated text segments from accidental duplicate ingestion.

### Tests and exit condition

- [x] Short text and root URLs receive base embeddings when the semantic provider is available.
- [x] Source spans at the beginning, middle, and end are retrievable.
- [x] The last short segment never disappears.
- [x] Unicode, long unbroken strings, code blocks, and URLs preserve valid source offsets.
- [x] Repeated rebuilds produce identical segment IDs for unchanged source revisions.
- [x] Provider input limits cannot silently discard the end of a segment.

## 9. T05: add reply-aware context and parent-aware retrieval

### Files and integration points

Modify `artifacts/bursts.py`, `artifacts/vector_index.py`, `artifacts/search.py`, and existing artifact context helpers.
Use `artifact_links` and source parent relationships before adding new relationship storage.
Extend artifact search, retrieval, and end-to-end tests.

### Context representation algorithm

1. Create a contextual representation anchored to each eligible message or segment.
2. Add an explicit reply target before nearby conversation records.
3. Add a bounded number of preceding and following records within the same conversation or thread.
4. Preserve speaker labels and source references across speaker changes.
5. Stop at explicit thread boundaries or configured time and size limits.
6. Record every included source dependency and its revision digest.
7. Keep the base representation searchable when context is missing or invalid.

No topic-classification model is required for the first implementation.
Explicit replies and conservative source-order windows supply the baseline.
Nearby unrelated messages remain labeled neighboring context, not facts attributed to the anchor message.
Tests tune window sizes against recall, noise, and update cost.

Available source preview text can improve a link representation.
This task adds no query-time crawler or generated product description.

### Meeting hierarchy

An explicit meeting-kind argument must search eligible meeting descendants, not only rows whose entity equals `meeting`.
Use parent traversal or a derived ancestor mapping to associate recordings, transcripts, and cues with their meeting.
The same existing scope and visibility rules apply to every ancestor and descendant.

1. Build an indexed meeting-to-descendant lookup from existing parent relationships.
2. Detect malformed cycles and missing parents without unbounded traversal.
3. Search descendant text within the eligible meeting set.
4. Group the final display by meeting while retaining each winning passage citation.
5. Apply existing explicit meeting dates to the parent event where that is the documented search meaning.

No sentence is parsed into a meeting restriction.
A general sentence still searches broadly through relevance ranking.

### Tests and exit condition

- [x] A short cross-speaker answer gains useful context without losing its own citation.
- [x] A reply target outranks unrelated nearby context in representation construction.
- [x] Another conversation cannot supply context merely because its timestamp is close.
- [x] A matching cue is discoverable through an explicit meeting-kind request.
- [x] Missing parent metadata does not erase the base passage from general search.
- [x] Edits and redactions invalidate every affected contextual representation.

## 10. T06: preserve the winning passage in results

### Files and integration points

Modify `models.py`, `retrieval.py`, `index.py`, `artifacts/search.py`, and response conversion in `service.py`.
The existing `SearchHit` does not carry a unique winning chunk identity.
That identity must survive candidate retrieval, fusion, reranking, and response construction.

### Procedure

1. Add winning segment identity and offsets to internal hits.
2. Preserve those values when merging provider results.
3. Fetch the winning segment directly during context expansion.
4. Allocate excerpt space to the winning source span first.
5. Add neighboring segments only within the remaining output budget.
6. Center raw lexical excerpts on matched source spans, including unquoted queries.
7. Center semantic excerpts on the retrieved segment rather than the start of the raw record.
8. Return bounded continuation information for context that does not fit.

FTS offsets or snippet support can locate lexical matches.
When a reliable offset is unavailable, the implementation must retain the already retrieved matching segment.
It must not replace that segment with an arbitrary document prefix.

A result can cite the anchor message and separately cite supporting context.
The response must not present a neighboring speaker's words as the anchor message's text.

### Tests and exit condition

- [x] Repeated headings cannot move the excerpt to an earlier chunk.
- [x] Context expansion retains the match that existed before expansion.
- [x] Long raw records return the matching tail for ordinary unquoted queries.
- [x] Returned offsets resolve to the expected source revision and text.
- [x] Context truncation preserves valid Unicode and complete source references.
- [x] A continuation cursor cannot bypass explicit source scope or visibility checks.

## 11. T07: use the full query without inferred restrictions

### Files and integration points

Modify `text.py`, `retrieval.py`, and both lexical search producers.
Inspect `fts_expression` and `RetrievalEngine._plan` on the selected base.
Extend lexical-scoring, retrieval, artifact-search, and MCP tests.

### Lexical candidate generation

1. Tokenize the complete query and retain token positions.
2. Preserve quoted phrases and exact identifiers as high-value retrieval signals.
3. Select informative terms across the complete sentence using indexed term frequency where available.
4. Use bounded overlapping term groups when a query exceeds one FTS expression's work budget.
5. Include terms from the final query window, not only its beginning.
6. Merge candidate IDs from the term groups before final relevance ranking.
7. Keep low-information fallback behavior explicit and measured.

Term groups supply alternative candidate paths. They do not create mandatory semantic conditions.
Phrase searches can supply an additional ranked producer without removing broader lexical or semantic candidates.
FTS syntax escaping remains mandatory for all user input.

### Semantic input

The embedding adapter receives the complete query when it fits its documented input budget.
Oversized queries use overlapping input windows with independent candidate retrieval and rank fusion.
The system reports executed windows and any exhausted work budget in diagnostics.
It does not impose an arbitrary sentence-length cutoff as a substitute for execution control.

### Existing implicit scope inference

An identifier embedded in prose is a search signal, not permission to exclude records with missing ticket metadata.
Keep direct lookup for identity-only requests.
Remove prose-to-ticket scope mutation where it would silently narrow ordinary search.
Explicit caller-supplied ticket and repository arguments remain unchanged.

### Tests and exit condition

- [x] Useful terms after position 24 produce lexical candidates.
- [x] Adding harmless introductory text does not systematically hide the target.
- [x] A ticket-like token in prose does not add an implicit metadata restriction.
- [x] Explicit scope arguments still restrict every producer.
- [x] Quotes, punctuation, Unicode, and FTS control characters cannot alter the query grammar unexpectedly.
- [x] Multi-window queries remain bounded and preserve the original query for final ranking.

## 12. T08: correct fusion and historical ranking

### Files and integration points

Modify `retrieval.py`, relevance configuration, and response adapters.
Extend `tests/test_freshness.py`, `tests/test_lexical_scoring.py`, `tests/test_answer_gate.py`, and artifact retrieval tests.

### Candidate fusion

1. Give each producer its own rank sequence.
2. Compute comparable fusion contributions from those ranks.
3. Avoid mixing previously boosted Markdown scores with unboosted raw scores as if they shared one scale.
4. Deduplicate source identity, revision, and overlapping passage matches.
5. Preserve the strongest winning passage when duplicate producers find the same source.
6. Keep optional reranking after candidate union and before display truncation.

Base and contextual representations of one source can improve retrieval opportunities.
They do not count as independent factual corroboration.
Scores remain relevance estimates, not probabilities that the sentence is true.

### Historical behavior

General message and meeting discovery has no default event-age bonus.
Old relevant messages compete on relevance rather than recency.
Existing active and superseded memory status behavior remains separate from raw event age.
Source dates remain available to the calling AI.
This task adds no automatic latest-event or historical-date interpreter.

Legacy confidence calculations, where retained for compatibility, use the internal candidate pool before display limits.
A one-result display must not invent a zero-scoring runner-up.
New clients receive ranked results without an answer-confidence gate.

### Tests and exit condition

- [x] An old fruit-link message can rank above newer unrelated link messages.
- [x] A duplicate burst does not create multiple independent votes for the same source text.
- [x] Producer concatenation order does not change reciprocal-rank votes.
- [x] Markdown and raw candidates use comparable fusion contributions.
- [x] Displaying one result does not manufacture greater confidence than displaying eight.
- [x] Source type alone does not prevent useful raw paraphrase results from appearing.

## 13. T09: bound vector work and measure ANN quality

### Files and integration points

Modify `ann.py`, `index.py`, `artifacts/vector_index.py`, and vector-storage helpers.
Add candidate-level benchmark output alongside final hybrid retrieval metrics.

### Exact baseline

1. Implement a block-based exact vector search for evaluation and small scoped corpora.
2. Keep a bounded top-k structure rather than sorting all decoded Python objects.
3. Read compact vector blocks without fetching source text for every row.
4. Record blocks read, vectors scored, peak resident memory, and elapsed time.
5. Abort large fallback work explicitly when its budget expires.

The baseline must score the same vectors used by the approximate index.
Comparing different embedding models cannot isolate ANN quality.

### Approximate candidate selection

1. Reproduce the current bucket shortlist on fixed synthetic and labeled semantic datasets.
2. Measure exact-neighbor retention at each corpus size and explicit scope size.
3. Inspect actual database query plans and rows processed before the candidate limit.
4. Separate bucket match volume from final candidate count.
5. Compare revised candidate selection with compatible local ANN alternatives through a provider boundary.
6. Select the implementation using recall, latency, memory, update cost, and platform support.

The existing few-candidate fallback does not detect a large but low-quality shortlist.
Candidate quality therefore needs an offline release gate, not only a runtime candidate-count check.
No particular replacement ANN library is selected before measurements.

The runtime must not respond to a poor or unavailable ANN path by loading the entire large corpus into Python.
It returns partial semantic execution with lexical candidates when a safe fallback cannot finish.

### Tests and exit condition

- [x] ANN candidate recall is reported against the exact baseline at every tested scale.
- [x] Candidate-limit ties do not produce unexplained source-ID bias.
- [x] Sparse explicit scopes and zero-candidate queries behave correctly.
- [x] Large fallback requests obey vector, memory, and elapsed-work budgets.
- [x] SQL work is measured before `LIMIT`, not inferred from returned rows.
- [ ] The selected candidate method passes an independent held-out recall gate.

The corrected synthetic regression and workload gates are separate from an independent held-out evaluation.

## 14. T10: retain warm workers and remove corpus-wide query checks

### Files and integration points

Modify the selected worker implementation, `server.py`, `service.py`, `index.py`, and `generation.py`.
Add worker lifecycle and concurrency tests through the MCP harness.

### Worker pool

1. Create a bounded pool of supervised processes.
2. Allow one active recall per worker initially.
3. Load models and immutable generation caches once per worker.
4. Use a framed request protocol with request IDs and bounded response sizes.
5. Include queue delay in the request deadline.
6. Reject excess queued work with an explicit outcome.
7. Send cancellation to the exact assigned worker.
8. Terminate and reap an unresponsive worker before replacing it.
9. Release request leases and SQLite state on every exit path.
10. Recycle workers using measured memory and lifetime bounds.

Worker reuse must not reuse request scope, transactions, candidate lists, or credentials across requests.
Windows process startup uses the supported spawn behavior, not assumptions about Unix fork semantics.
Worker diagnostics must not share the MCP response channel accidentally.

### Query-path cost removal

1. Validate full generation checksums and graph integrity during publication or first generation load.
2. Cache validated immutable generations by identity.
3. Replace per-query Markdown directory walks with indexed change state.
4. Run periodic reconciliation outside recall to detect missed filesystem events.
5. Replace full alias scans with bounded indexed identity lookup where possible.
6. Query only required graph paths and source identities within the existing scope.
7. Expose the last reconciliation time and known backlog in health data.

A cache entry cannot survive a generation or visibility change that invalidates its contents.
Background reconciliation is necessary even if filesystem watchers are available.

### Tests and exit condition

- [x] Repeated warm requests avoid model reloads and full graph hashing.
- [x] Normal recall does not stat every Markdown file.
- [x] Cancellation ends the assigned process or request and releases its lease.
- [x] A failed worker does not retain a database snapshot indefinitely.
- [x] Queue capacity and total worker memory remain bounded under concurrent requests.
- [x] Alternating source scopes across reused workers cannot leak results.
- [x] Cold, warm, queued, cancelled, and replacement-worker requests have separate timing records.

## 15. T11: make refresh proportional to changed content

### Files and integration points

Modify `artifacts/vector_index.py`, `generation.py`, change-journal access, and context-dependency storage.
Keep the initial availability fix from T02 usable throughout this task.

### Incremental dependency updates

1. Read changed artifact IDs from the committed change journal.
2. Find base representations anchored to those IDs.
3. Find contextual representations that depend on those IDs.
4. Recompute only affected segments and bounded neighboring windows.
5. Reuse embeddings only when text, dependency digest, model, and representation version match.
6. Record failures without advancing coverage past unprocessed changes.

An append affects the new source units and nearby context windows.
It must not require loading the complete history of a large conversation.
An edit to a long message can recompute that message's segments without reprocessing unrelated messages.

### Immutable segments and delta publication

The target index uses immutable vector segments plus a small manifest.
Updates write replacement representations and supersession metadata rather than copying every existing vector file.
Queries consult a bounded number of segments and merge by source revision.
Background compaction limits segment fan-out.

1. Stage changed segments without overwriting active files.
2. Validate representation metadata and coverage watermarks.
3. Publish one manifest atomically.
4. Retain active and rollback generations while their leases exist.
5. Build a bounded delta segment for newer source revisions when resources permit.
6. Report backlog when the delta or refresh budget is exhausted.

Compaction must not race publication or erase an index still used by a worker.
This work plan does not authorize file deletion; retention must preserve recoverability and obey repository safety rules.

### Tests and exit condition

- [x] Appending one message does not read every message in its conversation.
- [x] A reply-target edit rebuilds dependent contexts and no unrelated contexts.
- [x] Interrupted publication leaves the previous manifest usable.
- [x] A delta backlog leaves raw search available with explicit semantic coverage state.
- [x] Segment fan-out and compaction work have measured limits.
- [x] Rollback preserves newer canonical intake and current redaction state.

## 16. T12: connect meeting discovery and durable memories to sources

### Files and integration points

Use `artifacts/distillation.py`, existing artifact links, `retrieval.py`, and source metadata where those modules exist on the selected base.
Preserve manual Markdown outside managed distillation regions.
Extend distillation, meeting retrieval, and source-link tests.

### Procedure

1. Expose meeting summary, section, and passage candidates as separate retrieval entry points.
2. Link every section or summary to its parent meeting and supporting passage references.
3. Keep passage retrieval available when summaries are missing or stale.
4. Use deterministic transcript windows or provider sections before adding any new summarization dependency.
5. Prevent repeated summary and passage text from dominating the result list through duplication.
6. Retain existing active and superseded Markdown semantics.
7. Preserve links from distilled memories to raw source records.
8. Flag stale source links or changed source revisions rather than silently presenting old text as current.

Existing summaries can improve discovery without becoming the authority for raw transcript wording.
This task does not infer causal claims, resolve personal preferences, or create a new fact-verification subsystem.

### Tests and exit condition

- [x] A meeting remains discoverable without a summary.
- [x] A summary hit can expose the original supporting passage.
- [x] Duplicate derived levels do not occupy every displayed result slot.
- [x] Manual Markdown survives an updated source distillation.
- [x] Superseded memories remain reachable through existing explicit status behavior.
- [x] Missing source references produce a visible warning without fabricated citations.

## 17. T13: compare local embeddings and rerankers

### Files and integration points

Extend the provider boundary in `embedding.py` and the benchmark runner.
A new local reranker adapter is optional and must have a bounded batch interface.
No external service is required for this task.

### Experiment procedure

1. Freeze repaired source representations and a held-out query set.
2. Record the current provider as the baseline.
3. Compare candidate embedding providers using the same source units and query set.
4. Compare relevance reranking with no reranker on the same candidate pools.
5. Record model revision, dimensions, tokenizer, license, package versions, and resource use.
6. Measure cold startup, warm latency, throughput, peak memory, and ranking changes.
7. Keep a candidate only when its improvement justifies its resource cost.

A reranker changes order, not eligibility through inferred sentence criteria.
Its score cannot trigger a verified-answer status.
Representation changes and model changes require separate experiments so their effects remain identifiable.

If no candidate meets the resource and quality gates, retain the repaired existing provider.
The hashed fallback must report its limited lexical-feature behavior instead of claiming equivalent semantic quality.
Automatic fallback must not silently mix incompatible query and stored vectors.

### Tests and exit condition

- [x] The query provider matches the recorded index provider and dimensions.
- [x] An unavailable model produces explicit partial semantics or a safe compatible fallback.
- [x] Reranking does not drop the winning source identity or offsets.
- [ ] Experiments report independent held-out quality separately from tuning results.
- [ ] A replacement model or learned reranker has a reproducible comparative evaluation.

The current release retains Model2Vec and the existing relevance rules.
Synthetic contract results do not establish that another model or learned reranker cannot improve retrieval.

## 18. T14: run quality, scale, and failure evaluations

### Test families

The test suite separates retrieval quality from mechanical correctness.
Relevant-result ranking can improve without a claim of perfect sentence interpretation.

| Family | Required cases | Required measurements |
| --- | --- | --- |
| Coverage | Short replies, root URLs, empty text, missing times, long endings | Source-unit states and complete segment spans |
| Context | Cross-speaker replies, missing targets, unrelated neighbors | Target-message rank and cited context correctness |
| Meetings | Descendant hits, missing summaries, repeated headings | Meeting recall and winning-passage retention |
| Query handling | Long introductions, late useful words, paraphrases, punctuation | Candidate recall and robustness to harmless wording changes |
| Historical ranking | Old relevant links and newer distractors | Recall at 5 and 10, rank, and irrelevant-result rate |
| Runtime | Queue overload, crashes, cancellation, stale index, active intake | Execution state, lease release, queue delay, and memory |
| Source safety | Explicit scope, redactions, changed context, duplicate revisions | Zero unauthorized or invalid source citations |
| Compatibility | CLI, library, serialized MCP, old and new responses | Schema behavior and parity of source results |

### Synthetic corpus profiles

| Profile | Messages | Meetings | Transcript passages | Memory documents |
| --- | --- | --- | --- | --- |
| Regression | 1,000 | 20 | 2,000 | 100 |
| Workload | 50,000 | 1,000 | 100,000 | 2,500 |
| Growth | 500,000 | 5,000 | 1,000,000 | 25,000 |

These profiles describe proposed synthetic tests, not measured capacity or private source counts.
Large profiles run separately from the fast unit suite.
They include uneven conversation sizes, long transcripts, sparse activity, many short replies, and dense near-match distractors.

### Historical fruit-link fixture

1. Create an old message whose domain does not contain the requested product name.
2. Use indirect text such as a link to the recipient's preferred fruit.
3. Add one variant with an explicit prior question in the same thread.
4. Add another variant with no explanatory context.
5. Add many newer links about unrelated purchases.
6. Measure the target's candidate rank and final displayed rank.
7. Confirm that returned context cites the original speakers and messages.

The fixture with context tests whether indexing makes the connection discoverable.
The fixture without context tests candidate retrieval, not proof of the intended meaning.
The MCP must not invent the missing connection in either case.

### Mechanical release gates

- [x] All eligible nonempty source spans have processing states and no unexplained gaps.
- [x] No context expansion drops the winning passage.
- [x] No result bypasses explicit scope, current redactions, or source identity checks.
- [x] No timeout, cancellation, or unavailable provider becomes a completed empty search.
- [x] No interrupted refresh disables valid canonical exact or lexical retrieval.
- [x] No large exact fallback or worker queue has unbounded resource use.

### Quality and performance gates

The baseline establishes ranking quality before tuning.
The held-out gate must improve old-message and context-dependent retrieval without material regressions in exact lookup.
The release report records candidate recall, final recall, rank quality, irrelevant results, and citation correctness separately.
ANN recall is measured against exact search on identical embeddings.

The project records acceptable quality and resource thresholds after baseline measurement and before final evaluation.
Implementation cannot choose thresholds retrospectively to label a failing result successful.
No hard semantic-verification accuracy target belongs to this scope.

Performance output includes full MCP p50, p95, and p99 latency, queue delay, peak memory, and refresh throughput.
It separates cold, warm, concurrent, and active-intake runs.
It records hardware, corpus shape, generation, model, candidate budgets, and worker count.
An aborted or timed-out benchmark produces an incomplete report, not a passing result.

### Existing test commands

These commands select existing tests. New regression modules must join the corresponding groups as implementation proceeds.
Use the repository virtual environment for `python`.

Protocol and source behavior:

```sh
python -m pytest -q tests/test_mcp.py tests/test_generations.py tests/test_artifact_retrieval.py tests/test_artifact_search.py
```

Representation and ranking:

```sh
python -m pytest -q tests/test_artifact_bursts.py tests/test_artifact_vector_index.py tests/test_index_and_text.py tests/test_freshness.py tests/test_answer_gate.py
```

Portability and public-file privacy:

```sh
python -m pytest -q tests/test_portability.py tests/test_scripts_portability.py
```

The [validation report](retrieval-reliability-validation.md) records the current test and benchmark results.
Earlier test counts and measurements do not describe the corrected implementation.

## 19. T15: migrate clients and prove rollback

### Documentation and client changes

1. Update MCP schema documentation and CLI response examples.
2. Update the canonical AI Memory skill for ranked-source results and explicit execution failures.
3. Remove instructions that require an exact quote before raw results are useful.
4. Keep caller guidance to read the cited source before making a factual claim.
5. Document semantic lag, source-history coverage, and normal partial-result behavior.
6. Update the architecture guide only when each described change exists.
7. Test links and technical writing against the repository standard.

The canonical skill must not claim that retrieval verifies sentence criteria.
Discovery stubs change only when their canonical metadata or routing requires it.
No private machine path or runtime count belongs in public documentation.

### Deployment procedure

1. Record the active generation, compatible binary, schema version, and runtime configuration.
2. Back up canonical databases through the supported backup procedure.
3. Build new derived representations beside the existing generation.
4. Run read-only shadow queries against both generations.
5. Check source identities, excerpt correctness, coverage, and resource use.
6. Migrate supported clients to the explicit response contract.
7. Switch the active generation and worker configuration only after the gates pass.
8. Verify real MCP retrieval, ingestion during search, and cancellation on the deployment host.

Private live-data evaluation remains on the authorized host and does not export source text into public reports.
Deployment is a later implementation action, not an action performed while writing this plan.

### Rollback procedure

1. Stop assigning new requests to the failing worker configuration.
2. Drain or cancel active requests within their resource budgets.
3. Select the last compatible derived generation and binary configuration.
4. Preserve canonical intake that occurred after that generation.
5. Reapply current source visibility checks before returning any old-index candidate.
6. Verify exact lookup, raw search, response compatibility, and lease cleanup.

Additive source migrations must preserve compatibility with the documented rollback binary.
An incompatible migration requires a forward-compatible rollback build before deployment.
Rollback must not restore an old canonical database merely to reverse a search-index change.

### Final completion record

All implementation validation ran on macOS.
The Windows checkout contains no implementation or validation changes.
Validation did not deploy the changes to a live host.
The [validation report](retrieval-reliability-validation.md) replaces the earlier measurements and completion claims.
Independent held-out evaluation and replacement-model comparisons remain open in T09 and T13.
The workload follow-up passed quality, latency, throughput, and memory gates with full query logging enabled.
Serialized MCP p95 latency decreased from `7047.10 ms` to `1925.40 ms` without changing the five-second gate.
Both regression and workload profiles have passing complete performance runs.
The growth profile remains outside the supported capacity claim.
The [private query log guide](query-logging.md) describes the requested diagnostic records and their storage limits.

- [ ] Each enabled task has its tests and exit evidence.
- [x] The workload profile passes its five-second MCP p95 latency gate.
- [x] The real MCP path returns useful old-message and meeting-passage results.
- [x] Coverage reports explain all remaining exclusions and backlog.
- [x] Performance reports include completed workload and growth runs or clearly state the supported limit.
- [x] Cancellation and rollback have runtime proof.
- [x] Documentation describes implemented behavior without semantic-verification claims.
- [x] No source file was deleted, no private data was published, and unrelated changes remain intact.

## 20. Completion checklist for each implementation change

1. Add the smallest failing regression before changing behavior.
2. Implement the task without weakening source safety or adding inferred criteria.
3. Run its focused tests and applicable existing tests.
4. Inspect changed branches for missing revision, compatibility, or resource-limit rationale.
5. Add only comments that preserve those non-obvious constraints.
6. Update this plan with actual results and remaining work.
7. Keep benchmark claims separate from unmeasured expectations.
8. Inspect the diff for private data and unrelated changes.

This checklist does not authorize an automatic commit, public push, deployment, or file deletion.
