from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class Settings:
    memory_root: Path
    state_dir: Path
    graph_path: Path
    refresh_script: Path | None
    graphify_mcp_url: str
    host: str = "127.0.0.1"
    port: int = 4334
    result_limit: int = 8
    semantic_dimensions: int = 1024
    rrf_k: int = 60
    graph_depth: int = 2

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
        refresh_raw = os.getenv("GRAPHIFY_MEMORY_REFRESH_SCRIPT")
        refresh = (
            Path(os.path.expandvars(refresh_raw)).expanduser()
            if refresh_raw
            else project_root()
            / "scripts"
            / "graphify"
            / "refresh-ai-memory-graph.ps1"
        )
        return cls(
            memory_root=root,
            state_dir=state,
            graph_path=graph,
            refresh_script=refresh,
            graphify_mcp_url=os.getenv(
                "GRAPHIFY_GLOBAL_MCP_URL", "http://127.0.0.1:4324/mcp"
            ),
            host=os.getenv("AI_MEMORY_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("AI_MEMORY_MCP_PORT", "4334")),
            result_limit=int(os.getenv("AI_MEMORY_MCP_RESULT_LIMIT", "8")),
            semantic_dimensions=int(
                os.getenv("AI_MEMORY_MCP_SEMANTIC_DIMENSIONS", "1024")
            ),
            rrf_k=int(os.getenv("AI_MEMORY_MCP_RRF_K", "60")),
            graph_depth=int(os.getenv("AI_MEMORY_MCP_GRAPH_DEPTH", "2")),
        )

    @property
    def pointer_path(self) -> Path:
        return self.state_dir / "current-index.json"

    @property
    def feedback_path(self) -> Path:
        return self.state_dir / "feedback.jsonl"
