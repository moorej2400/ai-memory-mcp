# Retrieval Quality Upgrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `memory_recall` return best-effort evidence on low-confidence queries, replace the hashed pseudo-semantic vectors with real local embeddings (Model2Vec) behind a provider seam, and add a bounded freshness/age-decay signal to result fusion.

**Architecture:** Three independent upgrades to the existing hybrid retrieval pipeline (SQLite FTS5 + semantic vectors + Graphify graph, fused with RRF). Fix 1 changes only what `search()` returns (the `answered` gate becomes a status flag instead of a result filter). Fix 2 introduces `src/ai_memory_mcp/embedding.py` with an `EmbeddingProvider` protocol — the current hashed vectors become one provider, Model2Vec becomes the preferred provider, and the index records which provider built it so query-time embedding always matches. Fix 3 adds a bounded freshness bonus and review-overdue penalty inside `_fuse`, using the `updated`/`review_after` frontmatter already stored in the index.

**Tech Stack:** Python 3.11+, SQLite FTS5, pydantic v2, pytest, model2vec (optional dependency, `minishlab/potion-base-8M` static embedding model, ~30 MB, no torch).

## Global Constraints

- Read `AGENTS.md` at the repository root before making any change.
- The MCP tool surface must not change: `memory_recall`, `memory_sync`, `memory_status` only, and `RecallResponse.status` stays `Literal["answered", "no_answer"]`.
- No external API calls at query time; embeddings must run fully local.
- The frozen benchmark contract (`benchmarks/cases.json`, `benchmarks/fixtures/**`, `benchmarks/benchmark-lock.json`) must NOT be edited. All 20 frozen cases must still pass after every task. Tune implementation constants, never the cases.
- Tests must be deterministic without network access: the shared pytest/benchmark fixtures pin `embedding_provider="hashed"`; Model2Vec tests skip when the model is unavailable.
- Windows, macOS, and Linux all remain supported; no platform-specific code outside existing patterns.
- All test commands run from the repository root with the project venv: `python -m pytest` (or `.venv/bin/python -m pytest`). `pytest.ini_options` already sets `-q` and `testpaths=["tests"]`.
- Commit after each task with a conventional-commit message.

---

### Task 1: Best-effort evidence on low-confidence recall

The `answered` gate in `RetrievalEngine.search()` currently discards all results when confidence thresholds fail (`results=hits if answered else []`). The agent receives zero candidates and cannot pivot. Keep the gate as a *status signal* but always return the ranked hits, and add a warning that tells the agent how to treat them.

**Files:**
- Modify: `src/ai_memory_mcp/retrieval.py` (the `return EvidencePacket(...)` at the end of `search()`, around line 342)
- Modify: `src/ai_memory_mcp/service.py` (inside `recall()`, after `warnings = self._graph_warnings()`, around line 129)
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Consumes: existing `RetrievalEngine.search()` and `MemoryService.recall()`.
- Produces: `EvidencePacket.results` is now always the ranked hit list regardless of `answer_status`. `RecallResponse` gains no new fields; a `no_answer` response may now carry non-empty `evidence`/`citations` plus a warning string containing `"best-effort"`. Later tasks rely on this invariant: status is advisory, results are always best-effort.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_retrieval.py`:

```python
def test_no_answer_returns_best_effort_leads(
    benchmark_settings: Settings,
) -> None:
    service = MemoryService(benchmark_settings)
    packet = service.recall("What is the launch date of Project Zephyr?")
    assert packet.status == "no_answer"
    assert packet.evidence, "low-confidence recall must still return leads"
    assert packet.citations
    assert any("best-effort" in warning for warning in packet.warnings)
```

This query is the frozen `missing-product` benchmark case: it stays `no_answer` (nothing in the fixture vault answers it), but tokens like "project" and "launch" produce weak lexical candidates, so leads must now appear.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_retrieval.py::test_no_answer_returns_best_effort_leads -v`
Expected: FAIL — `assert packet.evidence` is empty because `search()` discards results on `no_answer`.

- [ ] **Step 3: Return hits unconditionally in the engine**

In `src/ai_memory_mcp/retrieval.py`, in the `EvidencePacket` constructed at the end of `search()`, change:

```python
            results=hits if answered else [],
```

to:

```python
            results=hits,
```

- [ ] **Step 4: Add the best-effort warning in the service**

In `src/ai_memory_mcp/service.py`, inside `recall()`, directly after the line `warnings = self._graph_warnings()`, add:

```python
        if packet.answer_status == "no_answer" and evidence:
            warnings.append(
                "No result met the answer threshold. Evidence contains "
                "best-effort leads only. Verify a lead in its canonical "
                "Markdown source, or search outside memory, before "
                "relying on it."
            )
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `python -m pytest tests/test_retrieval.py::test_no_answer_returns_best_effort_leads -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest`
Expected: PASS. Notes on cases that look at risk but are safe:
- The two frozen `no_answer` cases (`missing-product`, `missing-secret`) assert only `status == "no_answer"`, never empty evidence, and have no `forbidden` lists.
- `test_scope_is_applied_before_ranking` asserts `partial.evidence == []` for `repository="alp"`; that scope matches zero documents, so every retriever returns zero candidates and evidence stays empty even with best-effort return. Do not modify that test.

- [ ] **Step 7: Commit**

```bash
git add src/ai_memory_mcp/retrieval.py src/ai_memory_mcp/service.py tests/test_retrieval.py
git commit -m "feat: return best-effort evidence on low-confidence recall"
```

---

### Task 2: Embedding provider seam (pure refactor, hashed default)

Extract the hashed vector logic behind an `EmbeddingProvider` protocol so Task 3 can drop in Model2Vec. Behavior must be byte-identical after this task. The index records which provider built it; the engine resolves its query-time provider from that record, never from ambient settings, so query vectors always match indexed vectors.

**Files:**
- Create: `src/ai_memory_mcp/embedding.py`
- Modify: `src/ai_memory_mcp/config.py` (Settings fields + `from_env`)
- Modify: `src/ai_memory_mcp/text.py` (`chunk_document` signature)
- Modify: `src/ai_memory_mcp/index.py` (`SCHEMA_VERSION`, `build_index`, new metadata helper)
- Modify: `src/ai_memory_mcp/retrieval.py` (engine init + `_semantic`)
- Modify: `tests/conftest.py` and `src/ai_memory_mcp/benchmark.py` (pin `embedding_provider="hashed"`)
- Test: `tests/test_embedding.py`

**Interfaces:**
- Consumes: `semantic_vector(text, dimensions)` from `text.py` (unchanged, still exported).
- Produces:
  - `embedding.EmbeddingProvider` protocol: attributes `name: str`, `model: str`, `dimensions: int`; method `embed(text: str) -> dict[int, float]`.
  - `embedding.HashedProvider(dimensions: int = 1024)` with `name == "hashed"`, `model == ""`.
  - `embedding.EmbeddingUnavailable(RuntimeError)`.
  - `embedding.resolve_provider(name: str, *, model: str = "", dimensions: int = 1024) -> EmbeddingProvider` — accepts `"hashed"`, `"auto"` (returns hashed until Task 3), raises `EmbeddingUnavailable` for unknown names.
  - `Settings.embedding_provider: str = "auto"` and `Settings.embedding_model: str = ""`.
  - `chunk_document(document, provider)` — second parameter is now a provider, not an int.
  - Index metadata keys: `embedding_provider`, `embedding_model`, `embedding_fingerprint` (format `"{name}:{model}:{dimensions}"`).
  - `index.SCHEMA_VERSION == 3`.
  - `RetrievalEngine.provider: EmbeddingProvider | None` and `RetrievalEngine.provider_warning: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embedding.py`:

```python
from __future__ import annotations

from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.embedding import HashedProvider, resolve_provider
from ai_memory_mcp.index import build_index, current_index_path
from ai_memory_mcp.retrieval import RetrievalEngine
from ai_memory_mcp.text import semantic_vector


def _write_note(root: Path, relative: str, memory_id: str, title: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "---",
                f"memory_id: {memory_id}",
                f"title: {title}",
                "status: active",
                "updated: 2026-07-01",
                "---",
                "",
                f"# {title}",
                "",
                text,
                "",
            )
        ),
        encoding="utf-8",
    )


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        **overrides,
    )


def test_hashed_provider_matches_legacy_semantic_vector() -> None:
    provider = resolve_provider("hashed", dimensions=256)
    assert isinstance(provider, HashedProvider)
    assert provider.name == "hashed"
    assert provider.dimensions == 256
    text = "restart the proxy without a terminal window"
    assert provider.embed(text) == semantic_vector(text, 256)


def test_index_records_embedding_fingerprint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    metadata = engine.index.metadata()
    assert metadata["embedding_provider"] == "hashed"
    assert metadata["embedding_fingerprint"] == "hashed::1024"
    assert engine.provider is not None
    assert engine.provider.name == "hashed"
    assert engine.provider_warning == ""


def test_provider_change_triggers_full_reembed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    first = build_index(settings, force=True)
    assert first["added"] == 1
    second = build_index(settings, force=False)
    assert second["unchanged"] == 1
    resized = _settings(tmp_path, semantic_dimensions=128)
    third = build_index(resized, force=False)
    assert third["added"] == 1, "fingerprint change must re-embed every document"
    engine = RetrievalEngine(resized)
    assert engine.index.metadata()["embedding_fingerprint"] == "hashed::128"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_embedding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_memory_mcp.embedding'` (and `Settings` rejecting `embedding_provider`).

- [ ] **Step 3: Create the embedding module**

Create `src/ai_memory_mcp/embedding.py`:

```python
from __future__ import annotations

from typing import Protocol

from .text import semantic_vector


class EmbeddingProvider(Protocol):
    """Query-time and index-time text vectors must come from one provider."""

    name: str
    model: str
    dimensions: int

    def embed(self, text: str) -> dict[int, float]: ...


class EmbeddingUnavailable(RuntimeError):
    pass


class HashedProvider:
    """Deterministic hashed features with no model dependency."""

    name = "hashed"
    model = ""

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def embed(self, text: str) -> dict[int, float]:
        return semantic_vector(text, self.dimensions)


def fingerprint(provider: EmbeddingProvider) -> str:
    return f"{provider.name}:{provider.model}:{provider.dimensions}"


def resolve_provider(
    name: str,
    *,
    model: str = "",
    dimensions: int = 1024,
) -> EmbeddingProvider:
    normalized = (name or "auto").strip().casefold()
    if normalized in {"hashed", ""}:
        return HashedProvider(dimensions)
    if normalized == "auto":
        return HashedProvider(dimensions)
    raise EmbeddingUnavailable(f"Unknown embedding provider: {name}")
```

- [ ] **Step 4: Add the settings fields**

In `src/ai_memory_mcp/config.py`, inside the `Settings` dataclass after `graph_depth: int = 2`, add:

```python
    embedding_provider: str = "auto"
    embedding_model: str = ""
```

In `Settings.from_env`, inside the `return cls(...)` call after the `graph_depth=...` argument, add:

```python
            embedding_provider=os.getenv(
                "AI_MEMORY_MCP_EMBEDDING_PROVIDER", "auto"
            ),
            embedding_model=os.getenv("AI_MEMORY_MCP_EMBEDDING_MODEL", ""),
```

- [ ] **Step 5: Route chunking through the provider**

In `src/ai_memory_mcp/text.py`, replace the whole `chunk_document` function with:

```python
def chunk_document(document: MemoryDocument, provider) -> list[MemoryChunk]:
    """`provider` satisfies embedding.EmbeddingProvider (duck-typed to avoid
    an import cycle: embedding.py imports semantic_vector from this module)."""
    chunks: list[MemoryChunk] = []
    for ordinal, (heading, text) in enumerate(split_sections(document.body)):
        contextual = "\n".join(
            part for part in (document.title, heading, text) if part
        )
        chunks.append(
            MemoryChunk(
                chunk_id=f"{document.memory_id}:{ordinal}",
                memory_id=document.memory_id,
                source_id=document.source_id,
                path=document.path,
                title=document.title,
                heading=heading,
                ordinal=ordinal,
                text=text,
                vector=provider.embed(contextual),
            )
        )
    return chunks
```

- [ ] **Step 6: Record and enforce the provider in the index**

In `src/ai_memory_mcp/index.py`:

1. Change `SCHEMA_VERSION = 2` to `SCHEMA_VERSION = 3` (vector semantics may now differ per provider; stale snapshots must not be reused across the upgrade).

2. Add this import near the other local imports:

```python
from .embedding import fingerprint, resolve_provider
```

3. Add this helper below `_schema_matches`:

```python
def _index_metadata_value(path: Path, key: str) -> str | None:
    try:
        with _connect(path, read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None
    except (OSError, sqlite3.DatabaseError):
        return None
```

4. In `build_index`, immediately after `with _index_lock(settings.state_dir):` and the `current = current_index_path(settings)` line, add:

```python
        provider = resolve_provider(
            settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.semantic_dimensions,
        )
        provider_fingerprint = fingerprint(provider)
        if current and _index_metadata_value(
            current, "embedding_fingerprint"
        ) != provider_fingerprint:
            # A provider change invalidates every stored vector. Dropping the
            # baseline forces a clean full re-embed instead of a partial mix.
            current = None
```

5. Change the chunking call inside `build_index` from:

```python
                    _insert_document(
                        connection,
                        document,
                        chunk_document(
                            document,
                            settings.semantic_dimensions,
                        ),
                    )
```

to:

```python
                    _insert_document(
                        connection,
                        document,
                        chunk_document(document, provider),
                    )
```

6. In the `metadata = {...}` dict inside `build_index`, replace the line
`"semantic_dimensions": str(settings.semantic_dimensions),` with:

```python
                "semantic_dimensions": str(provider.dimensions),
                "embedding_provider": provider.name,
                "embedding_model": provider.model,
                "embedding_fingerprint": provider_fingerprint,
```

- [ ] **Step 7: Resolve the query-time provider from index metadata**

In `src/ai_memory_mcp/retrieval.py`:

1. Add to the local imports:

```python
from .embedding import EmbeddingProvider, EmbeddingUnavailable, resolve_provider
```

2. In `RetrievalEngine.__init__`, after `self.index = MemoryIndex(settings)`, add:

```python
        metadata = self.index.metadata()
        self.provider: EmbeddingProvider | None
        try:
            self.provider = resolve_provider(
                metadata.get("embedding_provider", "hashed"),
                model=metadata.get("embedding_model", ""),
                dimensions=int(
                    metadata.get(
                        "semantic_dimensions",
                        settings.semantic_dimensions,
                    )
                ),
            )
            self.provider_warning = ""
        except EmbeddingUnavailable as exc:
            # Lexical and graph retrieval still work; semantic is disabled
            # until the recorded provider is installed or the index rebuilt.
            self.provider = None
            self.provider_warning = (
                f"Semantic retrieval disabled: {exc}. "
                "Install the provider or run memory_sync to rebuild."
            )
```

3. Replace the first line of `_semantic` (`vector = semantic_vector(query, self.settings.semantic_dimensions)`) with:

```python
        if self.provider is None:
            return []
        vector = self.provider.embed(query)
```

4. Remove `semantic_vector` from the `from .text import ...` line (it is no longer used here); keep `cosine_sparse, query_identifiers, tokenize`.

- [ ] **Step 8: Surface the provider warning in recall**

In `src/ai_memory_mcp/service.py`, inside `recall()`, extend the block added in Task 1 so it reads:

```python
        if self.engine.provider_warning:
            warnings.append(self.engine.provider_warning)
        if packet.answer_status == "no_answer" and evidence:
            warnings.append(
                "No result met the answer threshold. Evidence contains "
                "best-effort leads only. Verify a lead in its canonical "
                "Markdown source, or search outside memory, before "
                "relying on it."
            )
```

- [ ] **Step 9: Pin the hashed provider in shared fixtures**

In `tests/conftest.py`, add `embedding_provider="hashed",` to the `Settings(...)` construction in `benchmark_settings`:

```python
    settings = Settings(
        memory_root=benchmark / "fixtures" / "vault",
        state_dir=benchmark / "runs" / f"pytest-state-{stamp}",
        graph_path=benchmark / "fixtures" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
```

In `src/ai_memory_mcp/benchmark.py`, add the same `embedding_provider="hashed",` line to the `Settings(...)` construction in `run_benchmark` (after `graphify_mcp_url=""`). The frozen benchmark measures fusion behavior deterministically; it must not depend on which optional model a machine has cached.

- [ ] **Step 10: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_embedding.py -v`
Expected: PASS (3 tests)

Run: `python -m pytest`
Expected: PASS — behavior is unchanged because `"auto"` still resolves to hashed and fixtures pin hashed explicitly.

- [ ] **Step 11: Commit**

```bash
git add src/ai_memory_mcp/embedding.py src/ai_memory_mcp/config.py src/ai_memory_mcp/text.py src/ai_memory_mcp/index.py src/ai_memory_mcp/retrieval.py src/ai_memory_mcp/service.py src/ai_memory_mcp/benchmark.py tests/conftest.py tests/test_embedding.py
git commit -m "refactor: put semantic vectors behind an embedding provider seam"
```

---

### Task 3: Model2Vec provider with auto resolution and graceful fallback

Add real static embeddings via `model2vec` (`minishlab/potion-base-8M`: 256-dim, ~30 MB, numpy-only, downloads once from Hugging Face then runs offline). `"auto"` prefers it and silently falls back to hashed; an explicit `"model2vec"` setting fails loudly.

**Files:**
- Modify: `src/ai_memory_mcp/embedding.py`
- Modify: `pyproject.toml` (optional extra)
- Modify: `scripts/setup.py` (install extra + warm the model)
- Modify: `src/ai_memory_mcp/models.py` (`IndexStatus` fields)
- Modify: `src/ai_memory_mcp/service.py` (`status()` surfaces provider)
- Test: `tests/test_embedding.py` (fallback tests, no model needed)
- Test: `tests/test_embedding_model2vec.py` (model-dependent, skips when absent)

**Interfaces:**
- Consumes: `resolve_provider`, `EmbeddingUnavailable`, `HashedProvider` from Task 2.
- Produces:
  - `embedding.Model2VecProvider(model: str = "")` with `name == "model2vec"`, `model` defaulting to `embedding.DEFAULT_MODEL2VEC_MODEL == "minishlab/potion-base-8M"`, `dimensions` probed from the loaded model, dense-as-sparse `embed()` returning a normalized `dict[int, float]` (same shape `cosine_sparse` already consumes).
  - `resolve_provider("model2vec", ...)` raises `EmbeddingUnavailable` when the package or model is missing; `resolve_provider("auto", ...)` returns Model2Vec when loadable, else `HashedProvider`.
  - `IndexStatus.embedding_provider: str | None` and `IndexStatus.embedding_model: str | None`.
  - pyproject extra: `semantic = ["model2vec>=0.3,<1"]`.

- [ ] **Step 1: Write the failing fallback tests (no model required)**

Append to `tests/test_embedding.py`:

```python
import sys

import pytest

from ai_memory_mcp.embedding import EmbeddingUnavailable


def test_auto_falls_back_to_hashed_without_model2vec(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "model2vec", None)
    provider = resolve_provider("auto", dimensions=64)
    assert provider.name == "hashed"
    assert provider.dimensions == 64


def test_explicit_model2vec_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "model2vec", None)
    with pytest.raises(EmbeddingUnavailable):
        resolve_provider("model2vec")


def test_engine_disables_semantic_when_provider_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    build_index(settings, force=True)

    def _raise(*args: object, **kwargs: object):
        raise EmbeddingUnavailable("provider gone")

    monkeypatch.setattr("ai_memory_mcp.retrieval.resolve_provider", _raise)
    engine = RetrievalEngine(settings)
    assert engine.provider is None
    assert "Semantic retrieval disabled" in engine.provider_warning
    packet = engine.search("proxy restart")
    assert packet.diagnostics["candidate_counts"]["semantic"] == 0
```

(Setting `sys.modules["model2vec"] = None` makes `import model2vec` raise `ImportError`, which simulates the missing package even on machines that have it installed.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_embedding.py -v`
Expected: the two new resolver tests FAIL — `resolve_provider("model2vec")` currently raises for the wrong reason ("Unknown embedding provider") and never attempts an import, and `"auto"` never tries model2vec. The engine test may already pass (the Task 2 branch exists); keep it as regression coverage.

- [ ] **Step 3: Implement the Model2Vec provider**

In `src/ai_memory_mcp/embedding.py`, add after `HashedProvider`:

```python
DEFAULT_MODEL2VEC_MODEL = "minishlab/potion-base-8M"


class Model2VecProvider:
    """Static local embeddings. Downloads once, then runs fully offline."""

    name = "model2vec"

    def __init__(self, model: str = ""):
        from model2vec import StaticModel  # deferred: optional dependency

        self.model = model or DEFAULT_MODEL2VEC_MODEL
        self._model = StaticModel.from_pretrained(self.model)
        self.dimensions = int(len(self._model.encode("dimension probe")))

    def embed(self, text: str) -> dict[int, float]:
        values = [float(value) for value in self._model.encode(text or " ")]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return {
            index: value / norm
            for index, value in enumerate(values)
            if value
        }
```

Add `import math` to the module imports.

Replace the `resolve_provider` function body with:

```python
def resolve_provider(
    name: str,
    *,
    model: str = "",
    dimensions: int = 1024,
) -> EmbeddingProvider:
    normalized = (name or "auto").strip().casefold()
    if normalized in {"hashed", ""}:
        return HashedProvider(dimensions)
    if normalized == "model2vec":
        try:
            return Model2VecProvider(model)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # import, download, or model-load failure
            raise EmbeddingUnavailable(
                f"model2vec provider unavailable: {exc}"
            ) from exc
    if normalized == "auto":
        try:
            return Model2VecProvider(model)
        except Exception:
            return HashedProvider(dimensions)
    raise EmbeddingUnavailable(f"Unknown embedding provider: {name}")
```

- [ ] **Step 4: Declare the optional dependency and install it during setup**

In `pyproject.toml`, change:

```toml
[project.optional-dependencies]
dev = ["pytest>=8.3,<9"]
```

to:

```toml
[project.optional-dependencies]
dev = ["pytest>=8.3,<9"]
semantic = ["model2vec>=0.3,<1"]
```

In `scripts/setup.py`, change the application install line from `f"{root}[dev]"` to `f"{root}[dev,semantic]"`, and directly after that `_run(...)` call add a tolerant model warm-up (downloading at setup time keeps first recall fast; failure must not abort setup because `"auto"` degrades to hashed):

```python
    try:
        _run(
            [
                str(application_python),
                "-c",
                (
                    "from ai_memory_mcp.embedding import resolve_provider; "
                    "print('embedding provider:', resolve_provider('auto').name)"
                ),
            ],
            "Failed to prepare the semantic embedding model.",
        )
    except ScriptError:
        info(
            "Semantic embedding model unavailable; recall uses the hashed "
            "fallback until the model can be downloaded."
        )
```

- [ ] **Step 5: Surface the provider in memory_status**

In `src/ai_memory_mcp/models.py`, add to `IndexStatus` after `semantic_dimensions`:

```python
    embedding_provider: str | None = None
    embedding_model: str | None = None
```

In `src/ai_memory_mcp/service.py`, in `status()`, add to the `IndexStatus(...)` construction after `semantic_dimensions=metadata.get("semantic_dimensions"),`:

```python
                embedding_provider=metadata.get("embedding_provider"),
                embedding_model=metadata.get("embedding_model"),
```

- [ ] **Step 6: Write the model-dependent tests (skip when absent)**

Create `tests/test_embedding_model2vec.py`:

```python
from __future__ import annotations

import pytest

pytest.importorskip("model2vec")

from ai_memory_mcp.embedding import resolve_provider


def _provider():
    try:
        return resolve_provider("model2vec")
    except Exception as exc:  # model not cached and no network
        pytest.skip(f"model2vec model unavailable: {exc}")


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def test_model2vec_reports_probed_dimensions() -> None:
    provider = _provider()
    assert provider.name == "model2vec"
    assert provider.model == "minishlab/potion-base-8M"
    assert provider.dimensions == 256


def test_model2vec_connects_paraphrase() -> None:
    provider = _provider()
    query = provider.embed("how do I log in to the log console")
    target = provider.embed("authentication steps for the the log console log server")
    distractor = provider.embed("quarterly marketing budget review meeting")
    assert _cosine(query, target) > _cosine(query, distractor)


def test_auto_prefers_model2vec_when_available() -> None:
    _provider()
    assert resolve_provider("auto").name == "model2vec"
```

- [ ] **Step 7: Install the extra and run everything**

Run: `python -m pip install -e ".[dev,semantic]"`
Expected: `model2vec` installs successfully.

Run: `python -m pytest tests/test_embedding.py tests/test_embedding_model2vec.py -v`
Expected: PASS (model2vec tests may SKIP on a machine with no model cache and no network — SKIP is acceptable, FAIL is not).

Run: `python -m pytest`
Expected: PASS — frozen cases are unaffected because conftest and the benchmark pin `embedding_provider="hashed"`.

- [ ] **Step 8: Commit**

```bash
git add src/ai_memory_mcp/embedding.py pyproject.toml scripts/setup.py src/ai_memory_mcp/models.py src/ai_memory_mcp/service.py tests/test_embedding.py tests/test_embedding_model2vec.py
git commit -m "feat: add Model2Vec local embeddings with hashed fallback"
```

---

### Task 4: Freshness decay and review-overdue penalty in fusion

Add two bounded ranking adjustments inside `_fuse`, using the `updated` and `review_after` dates already stored per document: an exponential freshness bonus (half-life 180 days, cap +0.03) and a flat −0.03 penalty with a "review overdue" reason when `review_after` has passed. Both are tiebreaker-scale — smaller than the exact-identifier bonus (0.12) — so they reorder only near-equal candidates, matching the article's "when relevance is otherwise equal, the newer thread wins."

**Files:**
- Modify: `src/ai_memory_mcp/models.py` (`SearchHit` fields)
- Modify: `src/ai_memory_mcp/retrieval.py` (constants, date parsing, `_row_hit`, `_lexical`, `_graph`, `_fuse`, engine clock)
- Modify: `src/ai_memory_mcp/index.py` (`all_vectors` SELECT)
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `documents.updated` and `documents.review_after` columns (already in schema); `RetrievalEngine` from Task 2.
- Produces:
  - `SearchHit.updated: str = ""` and `SearchHit.review_after: str = ""`.
  - `RetrievalEngine.now: Callable[[], datetime]` — defaults to UTC now; tests inject a fixed clock.
  - Module constants in `retrieval.py`: `FRESHNESS_CAP = 0.03`, `FRESHNESS_HALF_LIFE_DAYS = 180.0`, `REVIEW_OVERDUE_PENALTY = 0.03`.
  - New hit signals/reasons: `signals["freshness"]`, reasons `"recently updated"` and `"review overdue"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_freshness.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.retrieval import RetrievalEngine


def _write_note(
    root: Path,
    relative: str,
    memory_id: str,
    title: str,
    text: str,
    updated: str,
    review_after: str = "",
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"memory_id: {memory_id}",
        f"title: {title}",
        "status: active",
        f"updated: {updated}",
    ]
    if review_after:
        lines.append(f"review_after: {review_after}")
    lines.extend(("---", "", f"# {title}", "", text, ""))
    path.write_text("\n".join(lines), encoding="utf-8")


def _engine(tmp_path: Path) -> RetrievalEngine:
    settings = Settings(
        memory_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    engine.now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)
    return engine


def test_newer_note_outranks_stale_twin(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_note(
        vault,
        "Tools/Log Console Login 2024.md",
        "mem-log-console-old",
        "Log Console Login 2024",
        "Log in to the log console with LDAP at logs.example.internal.",
        "2024-01-05",
    )
    _write_note(
        vault,
        "Tools/Log Console Login.md",
        "mem-log-console-new",
        "Log Console Login",
        "Log in to the log console with SSO at logs.example.internal.",
        "2026-07-20",
    )
    packet = _engine(tmp_path).search("how to log in to the log console")
    ordered = [hit.memory_id for hit in packet.results]
    assert ordered.index("mem-log-console-new") < ordered.index("mem-log-console-old")
    top = packet.results[0]
    assert top.signals["freshness"] > 0.02
    assert "recently updated" in top.reasons


def test_review_overdue_note_is_penalized_and_flagged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # Titles chosen so the deterministic tiebreak (reverse-alphabetical title)
    # would rank the overdue note FIRST if the penalty did not exist.
    _write_note(
        vault,
        "Tools/Zeta Runbook.md",
        "mem-runbook-overdue",
        "Zeta Runbook",
        "Rotate the deploy token in the vault console.",
        "2026-06-01",
        review_after="2026-07-01",
    )
    _write_note(
        vault,
        "Tools/Alpha Runbook.md",
        "mem-runbook-current",
        "Alpha Runbook",
        "Rotate the deploy token in the vault console.",
        "2026-06-01",
    )
    packet = _engine(tmp_path).search("rotate the deploy token")
    ordered = [hit.memory_id for hit in packet.results]
    assert ordered.index("mem-runbook-current") < ordered.index(
        "mem-runbook-overdue"
    )
    overdue = next(
        hit for hit in packet.results if hit.memory_id == "mem-runbook-overdue"
    )
    assert "review overdue" in overdue.reasons


def test_unparseable_dates_are_ignored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_note(
        vault,
        "Tools/No Date.md",
        "mem-no-date",
        "No Date Note",
        "Restart the collector service after config changes.",
        "sometime last spring",
    )
    packet = _engine(tmp_path).search("restart the collector service")
    top = packet.results[0]
    assert top.memory_id == "mem-no-date"
    assert "freshness" not in top.signals
    assert "review overdue" not in top.reasons
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_freshness.py -v`
Expected: FAIL — `engine.now` does not exist yet (`AttributeError` is acceptable at collection of `_engine`) or, once fields exist, ordering assertions fail because no freshness signal is applied.

- [ ] **Step 3: Add the date fields to SearchHit**

In `src/ai_memory_mcp/models.py`, in `SearchHit`, after `score: float`, add:

```python
    updated: str = ""
    review_after: str = ""
```

(Keep the existing defaulted fields `ranks`, `signals`, `reasons`, `graph_neighbors` after them.)

- [ ] **Step 4: Carry the dates through every retriever**

In `src/ai_memory_mcp/index.py`, in `all_vectors`, change the SELECT to include the dates:

```python
                SELECT c.*, d.status, d.root_scope, d.scope_kind, d.scope_id,
                       d.projects_json, d.repos_json, d.updated, d.review_after
                FROM chunks c JOIN documents d USING(memory_id)
```

In `src/ai_memory_mcp/retrieval.py`:

1. In `_lexical`, change the SELECT line to:

```python
                SELECT c.*, d.identifiers_json, d.updated, d.review_after, bm25(
                    chunks_fts, 0.0, 4.0, 2.0, 1.0, 7.0
                ) AS lexical_score
```

2. In `_graph`, change the inner query's first line to:

```python
                    SELECT c.*, d.updated, d.review_after FROM chunks c
```

3. In `_row_hit`, add the two fields to the constructed `SearchHit`:

```python
def _row_hit(row: sqlite3.Row, score: float, source: str, rank: int) -> SearchHit:
    return SearchHit(
        memory_id=row["memory_id"],
        source_id=row["source_id"],
        path=row["path"],
        title=row["title"],
        heading=row["heading"],
        text=row["text"],
        score=score,
        updated=row["updated"],
        review_after=row["review_after"],
        ranks={source: rank},
        signals={source: score},
    )
```

4. In `_fuse`, where the merged `SearchHit` is first created (`fused = SearchHit(...)` with `score=0.0`), add the same two fields:

```python
                    fused = SearchHit(
                        memory_id=hit.memory_id,
                        source_id=hit.source_id,
                        path=hit.path,
                        title=hit.title,
                        heading=hit.heading,
                        text=hit.text,
                        score=0.0,
                        updated=hit.updated,
                        review_after=hit.review_after,
                    )
```

- [ ] **Step 5: Implement the freshness scoring**

In `src/ai_memory_mcp/retrieval.py`:

1. Add to the stdlib imports:

```python
from datetime import datetime, timezone
```

2. Add module constants and a parser below `STOPWORDS`:

```python
FRESHNESS_CAP = 0.03
FRESHNESS_HALF_LIFE_DAYS = 180.0
REVIEW_OVERDUE_PENALTY = 0.03


def _parse_utc(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
```

3. In `RetrievalEngine.__init__`, add:

```python
        self.now = lambda: datetime.now(timezone.utc)
```

4. In `_fuse`, at the top of the `for hit in by_memory.values():` adjustment loop (before the loop starts), capture the clock once:

```python
        now = self.now()
```

5. Inside that loop, after the existing bounded-bonus lines (`hit.score += min(0.08, intent_title_overlap * 0.04)`), add:

```python
            # Freshness is a tiebreaker, not a relevance signal: the cap sits
            # below every exact-match bonus so it only reorders near-equals.
            updated_at = _parse_utc(hit.updated)
            if updated_at is not None:
                age_days = max(
                    0.0, (now - updated_at).total_seconds() / 86400.0
                )
                freshness = FRESHNESS_CAP * 0.5 ** (
                    age_days / FRESHNESS_HALF_LIFE_DAYS
                )
                hit.score += freshness
                hit.signals["freshness"] = freshness
                if freshness > 0.02:
                    hit.reasons.append("recently updated")
            review_at = _parse_utc(hit.review_after)
            if review_at is not None and review_at < now:
                hit.score -= REVIEW_OVERDUE_PENALTY
                hit.reasons.append("review overdue")
```

- [ ] **Step 6: Run the freshness tests**

Run: `python -m pytest tests/test_freshness.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Verify the frozen contract still holds**

Run: `python -m pytest`
Expected: PASS. The fixture vault's `updated` dates span 2025-01-01 to 2026-07-26, so freshness bonuses differ across fixture notes; every expected winner has a relevance margin far above 0.03. **Contingency (only if a frozen case fails):** lower `FRESHNESS_CAP` and `REVIEW_OVERDUE_PENALTY` from `0.03` to `0.02` and the `"recently updated"` threshold from `0.02` to `0.015`, rerun; do not edit `benchmarks/`.

- [ ] **Step 8: Commit**

```bash
git add src/ai_memory_mcp/models.py src/ai_memory_mcp/retrieval.py src/ai_memory_mcp/index.py tests/test_freshness.py
git commit -m "feat: rank fresher memory higher and flag review-overdue notes"
```

---

### Task 5: Documentation, skill guidance, and benchmark verification

Update the docs that describe retrieval behavior, teach the agent-facing skill how to treat best-effort leads, and record a benchmark run proving no regression.

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`
- Modify: `README.md`
- Modify: `skill/ai-memory/SKILL.md`

**Interfaces:**
- Consumes: behavior shipped in Tasks 1–4.
- Produces: documentation only; no code changes.

- [ ] **Step 1: Update the architecture guide**

In `docs/architecture.md`:

1. Replace the "Semantic retrieval" section body:

```markdown
### Semantic retrieval

A local embedding provider supplies paraphrase results.
The default provider is Model2Vec with the `minishlab/potion-base-8M` model.
The hashed feature provider is the automatic fallback.
The index records the provider that built it.
Query embedding always uses the recorded provider.
If the recorded provider is unavailable, recall disables the semantic signal and warns.
The semantic index stays local and does not need an external API.
```

2. In the "Query procedure" section, append two lines after "Graph traversal is not the only retrieval method.":

```markdown
Fusion adds a bounded freshness bonus from the `updated` date.
A passed `review_after` date applies a bounded penalty and a `review overdue` reason.
```

3. In the "Public tools" section, after the line "A general question runs lexical, semantic, and graph retrieval.", add:

```markdown
A `no_answer` status still returns ranked best-effort evidence.
A warning marks that evidence as leads that require verification.
```

- [ ] **Step 2: Update the configuration guide**

In `docs/configuration.md`, in the environment-variable table, after the `AI_MEMORY_MCP_SEMANTIC_DIMENSIONS` row (line 72), add:

```markdown
| `AI_MEMORY_MCP_EMBEDDING_PROVIDER` | `auto` | Selects `model2vec`, `hashed`, or `auto`. |
| `AI_MEMORY_MCP_EMBEDDING_MODEL` | `minishlab/potion-base-8M` | Sets the Model2Vec model name. |
```

- [ ] **Step 3: Update the README component table**

In `README.md`, replace the row:

```markdown
| Local semantic index | Supplies paraphrase candidates without an external API. |
```

with:

```markdown
| Local semantic index | Supplies paraphrase candidates with Model2Vec embeddings or a hashed fallback, without an external API. |
```

- [ ] **Step 4: Teach the skill about best-effort leads**

In `skill/ai-memory/SKILL.md`, in the "Retrieve Memory" numbered list, replace item 5:

```markdown
5. Search canonical Markdown directly when the facade is unavailable, stale, ambiguous, or missing expected results.
```

with:

```markdown
5. Treat `no_answer` evidence as unverified leads: read the cited Markdown before using a lead, and say when no lead survived verification.
6. Search canonical Markdown directly when the facade is unavailable, stale, ambiguous, or missing expected results.
```

and renumber the former item 6 ("State when an answer came only from legacy...") to 7.

- [ ] **Step 5: Run the full suite and the benchmark**

Run: `python -m pytest`
Expected: PASS.

Run: `python -m ai_memory_mcp.benchmark --label retrieval-upgrades`
Expected: completes and writes `benchmarks/runs/<stamp>-retrieval-upgrades.json`. Compare `metrics` against the previous run in `benchmarks/runs/20260727T223044357312Z-macos-port-final.json`: `pass_rate` must equal 1.0, and `recall_at_1`, `recall_at_5`, `mrr`, and `no_answer_accuracy` must be greater than or equal to the previous values. If any metric regressed, stop and report — do not edit benchmark files.

- [ ] **Step 6: Commit**

```bash
git add docs/architecture.md docs/configuration.md README.md skill/ai-memory/SKILL.md benchmarks/runs/
git commit -m "docs: document best-effort recall, embedding providers, and freshness"
```
