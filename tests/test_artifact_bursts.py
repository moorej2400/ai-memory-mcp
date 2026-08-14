from __future__ import annotations

from datetime import datetime, timezone

from ai_memory_mcp.artifacts.bursts import MAX_BURST_CHARACTERS, group_bursts
from ai_memory_mcp.artifacts.models import ArtifactBurstRecord


def message(
    time: str,
    author: str,
    text: str,
    *,
    parent: str = "art_" + "a" * 32,
    classification: str = "user",
    reactions: tuple[str, ...] = (),
    attachment: bool = False,
    author_name: str | None = None,
) -> ArtifactBurstRecord:
    hour, minute = map(int, time.split(":"))
    artifact_suffix = f"{hour:02d}{minute:02d}".ljust(32, "a")
    return ArtifactBurstRecord(
        artifact_id="art_" + artifact_suffix,
        artifact_uri="artifact://message/art_" + artifact_suffix,
        parent_artifact_id=parent,
        parent_title="Example conversation",
        source="chat-source",
        source_instance="workspace",
        entity="message",
        author_id=author,
        author_name=author.title() if author_name is None else author_name,
        participant_names=("Actor A", "Actor B"),
        occurred_at=datetime(
            2026,
            1,
            2,
            hour,
            minute,
            tzinfo=timezone.utc,
        ),
        text=text,
        classification=classification,
        reactions=reactions,
        attachment_link=attachment,
    )


def test_same_author_messages_form_one_burst() -> None:
    bursts = group_bursts(
        [
            message("10:00", "actor-a", "First useful point."),
            message("10:05", "actor-a", "Second useful point."),
            message("10:06", "actor-b", "A different response."),
        ]
    )
    assert [burst.record_count for burst in bursts] == [2, 1]
    assert bursts[0].first_artifact_uri.endswith("1000" + "a" * 28)
    assert bursts[0].last_artifact_uri.endswith("1005" + "a" * 28)
    assert bursts[0].text.startswith(
        "Example conversation\nParticipants: Actor A, Actor B"
    )


def test_a_fifteen_minute_gap_splits_a_burst() -> None:
    bursts = group_bursts(
        [
            message("10:00", "actor-a", "First point."),
            message("10:16", "actor-a", "Later point."),
        ]
    )
    assert len(bursts) == 2


def test_parent_change_splits_a_burst() -> None:
    bursts = group_bursts(
        [
            message("10:00", "actor-a", "First point."),
            message(
                "10:01",
                "actor-a",
                "Other parent.",
                parent="art_" + "b" * 32,
            ),
        ]
    )
    assert len(bursts) == 2


def test_interleaved_parents_do_not_fragment_same_parent_bursts() -> None:
    other_parent = "art_" + "b" * 32
    bursts = group_bursts(
        [
            message("10:00", "actor-a", "First point."),
            message(
                "10:01",
                "actor-b",
                "Other conversation.",
                parent=other_parent,
            ),
            message("10:02", "actor-a", "Second point."),
        ]
    )
    assert [burst.record_count for burst in bursts] == [2, 1]


def test_display_name_only_authors_form_separate_bursts() -> None:
    bursts = group_bursts(
        [
            message(
                "10:00",
                "",
                "First actor.",
                author_name="Actor One",
            ),
            message(
                "10:01",
                "",
                "Second actor.",
                author_name="Actor Two",
            ),
        ]
    )
    assert [burst.record_count for burst in bursts] == [1, 1]


def test_eight_record_and_character_limits_split_bursts() -> None:
    records = [
        message(f"10:{index:02d}", "actor-a", f"Point {index}.")
        for index in range(9)
    ]
    assert [burst.record_count for burst in group_bursts(records)] == [8, 1]

    long_records = [
        message("11:00", "actor-a", "a" * 1200),
        message("11:01", "actor-a", "b" * 1200),
    ]
    assert len(group_bursts(long_records)) == 2


def test_single_record_burst_is_capped_at_the_character_limit() -> None:
    one_character = group_bursts([message("11:10", "actor-a", "x")])[0]
    render_overhead = len(one_character.text) - 1
    exact_text = "x" * (MAX_BURST_CHARACTERS - render_overhead)
    exact = group_bursts([message("11:11", "actor-a", exact_text)])[0]
    oversized = group_bursts([message("11:12", "actor-a", "x" * 2500)])[0]

    assert len(exact.text) == MAX_BURST_CHARACTERS
    assert exact.text.endswith(exact_text)
    assert len(oversized.text) == MAX_BURST_CHARACTERS


def test_system_and_low_signal_bursts_are_not_embedded() -> None:
    bursts = group_bursts(
        [
            message(
                "10:00",
                "system",
                "A participant joined. " * 30,
                classification="system",
            ),
            message("10:01", "actor-a", "Thanks."),
        ]
    )
    assert all(burst.embed is False for burst in bursts)


def test_identifier_reaction_and_attachment_are_embedding_signals() -> None:
    bursts = group_bursts(
        [
            message("10:00", "actor-a", "See DEMO-123."),
            message("10:20", "actor-a", "Acknowledged.", reactions=("like",)),
            message("10:40", "actor-a", "See file.", attachment=True),
        ]
    )
    assert [burst.embed for burst in bursts] == [True, True, True]
