from __future__ import annotations

import json
from io import StringIO

import pytest

from ai_memory_mcp.artifacts.ingest import read_artifact_batch


def _batch_with_source_payload(source_payload: dict[str, object]) -> StringIO:
    manifest = {
        "schema": "ai-memory/artifact-batch@1",
        "record": "batch",
        "batch_id": "security-batch-1",
        "source": "chat-source",
        "source_instance": "workspace",
        "observed_at": "2026-01-02T12:00:00Z",
        "event_count": 1,
    }
    event = {
        "schema": "ai-memory/artifact-event@1",
        "record": "event",
        "entity": "message",
        "operation": "upsert",
        "external_id": "message-1",
        "payload": {
            "text": "Neutral message text.",
            "content_format": "plain",
            "source_payload": source_payload,
        },
    }
    return StringIO("\n".join(json.dumps(value) for value in (manifest, event)) + "\n")


@pytest.mark.parametrize(
    "source_payload",
    [
        {"accessToken": "secret-value"},
        {"apiKey": "secret-value"},
        {"encryptedToken": "secret-value"},
        {"temporaryDownloadUrl": "https://files.example.invalid/item"},
        {"url": "https://user:secret-value@127.0.0.1/item"},
        {
            "text": (
                "Download https://files.example.invalid/item?"
                "X-Amz-Signature=secret-value now."
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?next="
                "https%3A%2F%2Ffiles.example.invalid%2Fitem%3Fsig%3Dsecret-value"
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?next="
                "%2Fitem%3Fsig%3Dsecret-value"
            )
        },
        {"url": ("https://meet.example.invalid/#/recap?tempauth=secret-value")},
    ],
)
def test_jsonl_parser_rejects_nested_capability_material(
    source_payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="secret|authentication|credential"):
        read_artifact_batch(_batch_with_source_payload(source_payload))


def test_jsonl_parser_accepts_benign_embedded_urls() -> None:
    parsed = read_artifact_batch(
        _batch_with_source_payload(
            {
                "text": "Read https://docs.example.invalid/guide?section=intake.",
                "redirect": (
                    "https://redirect.example.invalid/open?next="
                    "https%3A%2F%2Fdocs.example.invalid%2Fguide%3Fsection%3Dintake"
                ),
            }
        )
    )

    assert parsed.manifest.batch_id == "security-batch-1"
