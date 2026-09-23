from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_memory_mcp.collections import create_collection


def test_create_collection_is_lazy_and_idempotent(tmp_path: Path) -> None:
    first = create_collection(tmp_path, name="Links", record_type="link")
    second = create_collection(tmp_path, name="Links", record_type="link")

    assert first["created"] is True
    assert second["created"] is False
    assert (tmp_path / "Collections/Links/Records").is_dir()
    payload = yaml.safe_load((tmp_path / "Collections/Links/Links.base").read_text())
    assert payload["filters"]["and"] == [
        'file.inFolder("Collections/Links/Records")'
    ]
    assert payload["views"][0]["filters"]["and"] == ['record_type == "link"']


def test_collection_does_not_replace_a_changed_view(tmp_path: Path) -> None:
    create_collection(tmp_path, name="People", record_type="person")
    base = tmp_path / "Collections/People/People.base"
    base.write_text("views: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="existing collection view differs"):
        create_collection(tmp_path, name="People", record_type="person")


def test_collection_rejects_unsafe_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="collection name"):
        create_collection(tmp_path, name="../Links", record_type="link")
