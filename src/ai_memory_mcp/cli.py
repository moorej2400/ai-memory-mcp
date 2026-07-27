from __future__ import annotations

import argparse
import json

from .config import Settings
from .index import build_index


def index_main() -> None:
    parser = argparse.ArgumentParser(description="Build the derived AI-memory index")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_index(Settings.from_env(), force=args.force), indent=2))


if __name__ == "__main__":
    index_main()

