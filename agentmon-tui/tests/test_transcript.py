from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from agentmon.transcript import (
    InvalidSessionId,
    SessionNotFound,
    TranscriptError,
    extract_transcript,
    load_transcript,
    resolve_rollout_path,
)
from agentmon.transcript_demo import main as demo_main


SESSION_ID = "019f6c99-2762-7dc2-9d93-a8a4b48a3a5e"
OTHER_ID = "019f6c72-d33f-72c0-b313-d7206e943f22"


def write_rollout(home: Path, session_id: str = SESSION_ID) -> Path:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "07"
        / "16"
        / f"rollout-2026-07-16T13-23-24-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-16T20:23:24Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/work"},
        },
        {
            "timestamp": "2026-07-16T20:23:25Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "injected context"}],
            },
        },
        {
            "timestamp": "2026-07-16T20:23:26Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Build it."},
        },
        {
            "timestamp": "2026-07-16T20:23:27Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_goal_updated",
                "threadId": session_id,
                "goal": {
                    "threadId": session_id,
                    "objective": "Reach 80% test coverage.",
                    "status": "active",
                    "tokensUsed": 0,
                    "timeUsedSeconds": 0,
                    "createdAt": 1784233407,
                    "updatedAt": 1784233407,
                },
            },
        },
        {
            "timestamp": "2026-07-16T20:23:28Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_goal_updated",
                "threadId": session_id,
                "goal": {
                    "threadId": session_id,
                    "objective": "Reach 80% test coverage.",
                    "status": "paused",
                    "tokensUsed": 100,
                    "timeUsedSeconds": 10,
                    "createdAt": 1784233407,
                    "updatedAt": 1784233408,
                },
            },
        },
        {
            "timestamp": "2026-07-16T20:23:29Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Working on it."},
        },
        {
            "timestamp": "2026-07-16T20:23:30Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {}},
        },
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    return rollout


def write_state_database(home: Path, session_id: str, rollout: Path) -> None:
    database = sqlite3.connect(home / "state_5.sqlite")
    database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
    database.execute(
        "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
        (session_id, str(rollout)),
    )
    database.commit()
    database.close()


def write_current_rollout(home: Path, session_id: str = SESSION_ID) -> Path:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "31"
        / f"rollout-2026-08-31T10-04-38-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-08-31T17:04:52Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/work"},
        },
        {
            "timestamp": "2026-08-31T17:04:53Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "injected context"}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": [
                        "agents_md.instructions",
                        "environments.environment_context",
                    ]
                },
            },
        },
        {
            "timestamp": "2026-08-31T17:04:53Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Build it."}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text"]
                },
            },
        },
        {
            "timestamp": "2026-08-31T17:04:54Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Working on it."}],
            },
        },
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    return rollout


def test_loads_human_messages_via_state_database(tmp_path: Path) -> None:
    rollout = write_rollout(tmp_path)
    write_state_database(tmp_path, SESSION_ID, rollout)

    transcript = load_transcript(SESSION_ID, home=tmp_path)

    assert transcript.rollout_path == rollout
    assert [(message.role, message.text) for message in transcript.messages] == [
        ("user", "Build it."),
        ("goal", "/goal Reach 80% test coverage."),
    ]
    assert transcript.skipped_lines == ()


def test_loads_current_response_item_user_messages(tmp_path: Path) -> None:
    rollout = write_current_rollout(tmp_path)

    transcript = extract_transcript(rollout)

    assert [(message.role, message.text) for message in transcript.messages] == [
        ("user", "Build it."),
    ]


def test_falls_back_to_session_directory_without_state_database(tmp_path: Path) -> None:
    rollout = write_rollout(tmp_path)

    assert resolve_rollout_path(SESSION_ID, home=tmp_path) == rollout


def test_skips_malformed_jsonl_and_reports_its_line(tmp_path: Path) -> None:
    rollout = write_rollout(tmp_path)
    with rollout.open("a") as stream:
        stream.write('{"unfinished":\n')
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Afterward."},
                }
            )
            + "\n"
        )

    transcript = extract_transcript(rollout)

    assert transcript.skipped_lines == (8,)
    assert transcript.messages[-1].text == "Afterward."


def test_rejects_mismatched_session_metadata(tmp_path: Path) -> None:
    rollout = write_rollout(tmp_path, session_id=OTHER_ID)

    with pytest.raises(TranscriptError, match="belongs to session"):
        extract_transcript(rollout, session_id=SESSION_ID)


def test_rejects_invalid_or_missing_session(tmp_path: Path) -> None:
    with pytest.raises(InvalidSessionId):
        load_transcript("not-a-session", home=tmp_path)
    with pytest.raises(SessionNotFound):
        load_transcript(SESSION_ID, home=tmp_path)


def test_demo_prints_transcript(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rollout = write_rollout(tmp_path)
    write_state_database(tmp_path, SESSION_ID, rollout)

    result = demo_main(["--codex-home", str(tmp_path), SESSION_ID])

    captured = capsys.readouterr()
    assert result == 0
    assert f"Session: {SESSION_ID}" in captured.out
    assert "[USER 2026-07-16T20:23:26Z]" in captured.out
    assert "Build it." in captured.out
    assert "[GOAL 2026-07-16T20:23:27Z]" in captured.out
    assert "/goal Reach 80% test coverage." in captured.out
    assert "Working on it." not in captured.out
    assert "injected context" not in captured.out


def test_demo_reports_lookup_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = demo_main(["--codex-home", str(tmp_path), SESSION_ID])

    captured = capsys.readouterr()
    assert result == 1
    assert "was not found" in captured.err
