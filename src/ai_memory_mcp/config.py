from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .platform_paths import path_key


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_home() -> Path:
    return Path.home()


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        # Explicit process variables win so launchers can override one setting
        # without editing the repository-local configuration.
        os.environ.setdefault(name, value)


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    return Path(os.path.expandvars(value)).expanduser()


def _configured_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@dataclass(frozen=True, slots=True)
class MemorySource:
    source_id: str
    root: Path
    writable: bool = False


def _source_id(value: str) -> str:
    normalized = value.strip().casefold()
    if not SOURCE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Memory source IDs must start with a letter and contain only "
            "lowercase letters, numbers, or hyphens."
        )
    return normalized


def _retrieval_sources() -> tuple[MemorySource, ...]:
    raw = os.getenv("AI_MEMORY_RETRIEVAL_SOURCES", "").strip()
    configured: dict[str, str] = {}
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "AI_MEMORY_RETRIEVAL_SOURCES must be a JSON object."
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                "AI_MEMORY_RETRIEVAL_SOURCES must map source IDs to directories."
            )
        configured = {str(key): str(value) for key, value in payload.items()}

    personal = os.getenv("AI_MEMORY_PERSONAL_DIR", "").strip()
    if personal:
        configured.setdefault("personal", personal)

    sources: list[MemorySource] = []
    for name, value in configured.items():
        source_id = _source_id(name)
        if not value.strip():
            raise ValueError(
                f"Memory retrieval source '{source_id}' has no directory."
            )
        sources.append(
            MemorySource(
                source_id=source_id,
                root=Path(os.path.expandvars(value)).expanduser(),
            )
        )
    return tuple(sorted(sources, key=lambda source: source.source_id))


@dataclass(frozen=True, slots=True)
class Settings:
    memory_root: Path
    state_dir: Path
    graph_path: Path
    graphify_mcp_url: str
    primary_source_id: str = "core"
    retrieval_sources: tuple[MemorySource, ...] = ()
    host: str = "127.0.0.1"
    port: int = 4334
    result_limit: int = 8
    semantic_dimensions: int = 1024
    rrf_k: int = 60
    graph_depth: int = 2
    log_dir: Path | None = None
    audit_logging_enabled: bool = True
    audit_log_max_bytes: int = 25_000_000
    audit_lock_timeout_seconds: float = 10.0
    index_lock_timeout_seconds: float = 300.0
    embedding_provider: str = "auto"
    embedding_model: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(project_root() / ".env")
        home = _default_home()
        root = _configured_path("AI_MEMORY_WORK_DIR", home / "AI-Memory")
        state = _configured_path(
            "AI_MEMORY_MCP_STATE_DIR", home / ".ai-memory-mcp"
        )
        graph = _configured_path(
            "AI_MEMORY_GRAPH_PATH",
            home
            / ".graphify"
            / "corpora"
            / "ai-memory"
            / "graphify-out"
            / "graph.json",
        )
        return cls(
            memory_root=root,
            state_dir=state,
            graph_path=graph,
            graphify_mcp_url=os.getenv(
                "GRAPHIFY_GLOBAL_MCP_URL", "http://127.0.0.1:4324/mcp"
            ),
            primary_source_id=_source_id(
                os.getenv("AI_MEMORY_PRIMARY_SOURCE_ID", "core")
            ),
            retrieval_sources=_retrieval_sources(),
            host=os.getenv("AI_MEMORY_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("AI_MEMORY_MCP_PORT", "4334")),
            result_limit=int(os.getenv("AI_MEMORY_MCP_RESULT_LIMIT", "8")),
            semantic_dimensions=int(
                os.getenv("AI_MEMORY_MCP_SEMANTIC_DIMENSIONS", "1024")
            ),
            rrf_k=int(os.getenv("AI_MEMORY_MCP_RRF_K", "60")),
            graph_depth=int(os.getenv("AI_MEMORY_MCP_GRAPH_DEPTH", "2")),
            log_dir=_configured_path(
                "AI_MEMORY_LOG_DIR",
                state / "logs",
            ),
            audit_logging_enabled=_configured_bool(
                "AI_MEMORY_AUDIT_LOGGING",
                True,
            ),
            audit_log_max_bytes=int(
                os.getenv("AI_MEMORY_AUDIT_LOG_MAX_BYTES", "25000000")
            ),
            audit_lock_timeout_seconds=float(
                os.getenv("AI_MEMORY_AUDIT_LOCK_TIMEOUT_SECONDS", "10")
            ),
            index_lock_timeout_seconds=float(
                os.getenv("AI_MEMORY_INDEX_LOCK_TIMEOUT_SECONDS", "300")
            ),
            embedding_provider=os.getenv(
                "AI_MEMORY_MCP_EMBEDDING_PROVIDER", "auto"
            ),
            embedding_model=os.getenv("AI_MEMORY_MCP_EMBEDDING_MODEL", ""),
        )

    @property
    def pointer_path(self) -> Path:
        return self.state_dir / "current-index.json"

    @property
    def resolved_log_dir(self) -> Path:
        return self.log_dir or self.state_dir / "logs"

    @property
    def memory_sources(self) -> tuple[MemorySource, ...]:
        primary = MemorySource(
            source_id=self.primary_source_id,
            root=self.memory_root,
            writable=True,
        )
        sources = (primary, *self.retrieval_sources)
        seen_ids: set[str] = set()
        seen_roots: set[str] = set()
        for source in sources:
            # Case folding is correct on Windows and macOS but would wrongly
            # merge two distinct directories on case-sensitive filesystems.
            resolved = path_key(source.root)
            if source.source_id in seen_ids:
                raise ValueError(
                    f"Duplicate memory source ID: {source.source_id}"
                )
            if resolved in seen_roots:
                raise ValueError(
                    f"Duplicate memory source directory: {source.root}"
                )
            seen_ids.add(source.source_id)
            seen_roots.add(resolved)
        return sources
