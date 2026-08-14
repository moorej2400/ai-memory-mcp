from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import quote

import pytest

from ai_memory_mcp.artifacts.ingest import read_artifact_batch


def _batch_with_source_payload(source_payload: dict[str, object]) -> BytesIO:
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
    raw = "\n".join(json.dumps(value) for value in (manifest, event)) + "\n"
    return BytesIO(raw.encode())


@pytest.mark.parametrize(
    "source_payload",
    [
        {"accessToken": "secret-value"},
        {"apiKey": "secret-value"},
        {"token": "secret-value"},
        {"x-amz-signature": "secret-value"},
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
            "text": (
                "[open](//files.example.invalid/item?tempauth=secret-value)"
            )
        },
        {
            "text": (
                "[open](//files.example.invalid/item?view=1"
                "&amp;refresh_token=secret-value)"
            )
        },
        {
            "text": (
                "//redirect.example.invalid/open?next="
                "%2F%2Ffiles.example.invalid%2Fitem%3Fsig%3Dsecret-value"
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
        {"url": "https://files.example.invalid/item?refresh_token=secret-value"},
        {"url": "https://files.example.invalid/item?cookie=secret-value"},
        {"url": "https://files.example.invalid/item#cookies=secret-value"},
        {
            "url": (
                "https://files.example.invalid/item?view=1"
                "&amp;credentials=secret-value"
            )
        },
        {"url": "https://files.example.invalid/item?jwt=secret-value"},
        {"url": "https://files.example.invalid/item#api_key=secret-value"},
        {
            "url": (
                "https://redirect.example.invalid/open?next="
                "https%3A%2F%2Ffiles.example.invalid%2Fitem%3Fid_token%3Dsecret-value"
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?next="
                "https%3A%2F%2Ffiles.example.invalid%2Fitem%3Fsecret%3Dsecret-value"
            )
        },
        {
            "url": (
                "https://meet.example.invalid/callback#"
                "access_token=secret-value&state=stable"
            )
        },
        {
            "url": (
                "https://files.example.invalid/item?view=1"
                "&amp;sig=secret-value"
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?next="
                "https%3A%2F%2Ffiles.example.invalid%2Fitem%3Fview%3D1"
                "%26amp%3Brefresh_token%3Dsecret-value"
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?state="
                + quote(json.dumps({"refresh_token": "secret-value"}), safe="")
            )
        },
        {
            "url": (
                "https://files.example.invalid/#"
                + quote(json.dumps({"refresh_token": "secret-value"}), safe="")
            )
        },
        {
            "url": (
                "https://files.example.invalid/#"
                + quote(
                    quote('{"refresh_token":"secret-value"}', safe=""),
                    safe="",
                )
            )
        },
        {
            "url": (
                "https://files.example.invalid/#state:"
                + quote('{"api_key":"secret-value"}', safe="")
            )
        },
        {
            "url": (
                "https://redirect.example.invalid/open?state="
                + quote(
                    quote('{"api-key":"secret-value"}', safe=""),
                    safe="",
                )
            )
        },
        {
            "url": "https://redirect.example.invalid/open?next="
            + quote(
                quote(
                    quote(
                        quote(
                            "https://files.example.invalid/item?"
                            "tempauth=secret-value"
                        ),
                        safe="",
                    ),
                    safe="",
                ),
                safe="",
            )
        },
        {"text": "/download?token=secret-value"},
        {"text": "[open](/download?cookie=secret-value)"},
        {"text": '<a href="/download?credentials=secret-value">open</a>'},
        {"text": "files.example.invalid/download?jwt=secret-value"},
        {"url": r"https:\\files.example.invalid\item?token=secret-value"},
        {"url": r"https:/\files.example.invalid\item?cookie=secret-value"},
        {
            "url": quote(
                r"https:\files.example.invalid\item?jwt=secret-value",
                safe="",
            )
        },
        {
            "url": "https://redirect.example.invalid/open?next="
            + quote(
                r"https:\files.example.invalid\item?secret=secret-value",
                safe="",
            )
        },
        {"text": "Authorization: Bearer private-token-marker"},
        {"text": "Authorization: Digest private-digest-marker"},
        {"text": "Authorization&#58; Bearer private-html-auth-marker"},
        {"text": "Cookie: sessionid=private-cookie-marker"},
        {"text": "Cookie&#58; sessionid=private-html-cookie-marker"},
        {"text": "refresh_token: private-refresh-marker"},
        {"text": "refresh_token&#61;private-html-refresh-marker"},
        {"text": '{"refresh_token":"private-json-marker"}'},
        {"text": 'state: {"refresh_token":"private-state-marker"}'},
        {
            "text": quote(
                '{"refresh_token":"private-encoded-json-marker"}',
                safe="",
            )
        },
    ],
)
def test_jsonl_parser_rejects_nested_capability_material(
    source_payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="secret|authentication|credential"):
        read_artifact_batch(_batch_with_source_payload(source_payload))


def test_jsonl_parser_accepts_benign_embedded_urls() -> None:
    backslash_url = r"https:\docs.example.invalid\guide?section=intake"
    parsed = read_artifact_batch(
        _batch_with_source_payload(
            {
                "text": "Read https://docs.example.invalid/guide?section=intake.",
                "backslash_url": backslash_url,
                "redirect": (
                    "https://redirect.example.invalid/open?next="
                    "https%3A%2F%2Fdocs.example.invalid%2Fguide%3Fsection%3Dintake"
                ),
            }
        )
    )

    assert parsed.manifest.batch_id == "security-batch-1"
    assert parsed.events[0].payload.source_payload["backslash_url"] == backslash_url


def test_jsonl_parser_accepts_plain_discussion_of_security_fields() -> None:
    parsed = read_artifact_batch(
        _batch_with_source_payload(
            {
                "text": (
                    "The refresh_token field is not stored. "
                    "The Authorization header is required.\n"
                    "Auth: review completed.\n"
                    "Rotate the token: use the security page.\n"
                    "Use key: value in this example."
                )
            }
        )
    )

    assert parsed.manifest.batch_id == "security-batch-1"
