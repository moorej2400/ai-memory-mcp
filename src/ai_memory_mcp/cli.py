from __future__ import annotations

import argparse
import json
import sys

from .config import Settings
from .index import build_index


def index_main() -> None:
    parser = argparse.ArgumentParser(description="Build the derived AI-memory index")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_index(Settings.from_env(), force=args.force), indent=2))


def sync_main() -> None:
    """Publish one retrieval generation, which includes the note graph."""
    argparse.ArgumentParser(
        description=(
            "Build and publish the Markdown index, the artifact index, and the "
            "note graph as one retrieval generation."
        )
    ).parse_args()
    from .service import MemoryService

    result = MemoryService(Settings.from_env()).sync()
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    if not result.ok:
        # Setup and schedulers rely on the exit status; the previous
        # generation stays active after a failed publication.
        sys.exit(1)


if __name__ == "__main__":
    index_main()
