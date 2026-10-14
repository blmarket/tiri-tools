from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmon.claude_transcript import (
    extract_transcript,
    load_transcript,
    resolve_transcript_path,
)
from agentmon.transcript import InvalidSessionId, SessionNotFound, TranscriptError
from agentmon.transcript_demo import main as demo_main


SESSION_ID = "874085ab-4a17-4018-ac19-f0381bb7940a"
OTHER_ID = "019f6c72-d33f-72c0-b313-d7206e943f22"
PROJECT_SLUG = "-home-hun-srv-tiri-agentmon-tui"


def write_transcript(
    home: Path, session_id: str = SESSION_ID, slug: str = PROJECT_SLUG
) -> Path:
    path = home / "projects" / slug / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": "/work",
            "timestamp": "2026-07-17T00:33:01.521Z",
            "message": {"role": "user", "content": "Build it."},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "Working on it."},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "ok", "tool_use_id": "t1"}
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "isMeta": True,
            "message": {"role": "user", "content": "session-scoped hook is active"},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "message": {
                "role": "user",
                "content": "<command-name>/clear</command-name>",
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "isSidechain": True,
            "message": {"role": "user", "content": "subagent instructions"},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-07-17T00:40:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Ship it."}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_loads_only_human_messages(tmp_path: Path) -> None:
    path = write_transcript(tmp_path)

    result = load_transcript(SESSION_ID, home=tmp_path)

    assert result.rollout_path == path
    assert [(m.role, m.text) for m in result.messages] == [
        ("user", "Build it."),
        ("user", "Ship it."),
    ]
    assert result.skipped_lines == ()


def test_resolves_across_project_slugs_by_filename(tmp_path: Path) -> None:
    path = write_transcript(tmp_path, slug="-some-other-project")

    assert resolve_transcript_path(SESSION_ID, home=tmp_path) == path


def test_falls_back_to_content_scan_when_filename_differs(tmp_path: Path) -> None:
    path = write_transcript(tmp_path)
    renamed = path.with_name("resumed-session.jsonl")
    path.rename(renamed)

    assert resolve_transcript_path(SESSION_ID, home=tmp_path) == renamed


def test_skips_malformed_jsonl_and_reports_its_line(tmp_path: Path) -> None:
    path = write_transcript(tmp_path)
    with path.open("a") as stream:
        stream.write('{"unfinished":\n')
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": SESSION_ID,
                    "message": {"role": "user", "content": "Afterward."},
                }
            )
            + "\n"
        )

    result = extract_transcript(path)

    assert result.skipped_lines == (8,)
    assert result.messages[-1].text == "Afterward."


def test_rejects_mismatched_session_metadata(tmp_path: Path) -> None:
    path = write_transcript(tmp_path, session_id=OTHER_ID)

    with pytest.raises(TranscriptError, match="belongs to session"):
        extract_transcript(path, session_id=SESSION_ID)


def test_rejects_invalid_or_missing_session(tmp_path: Path) -> None:
    with pytest.raises(InvalidSessionId):
        load_transcript("not-a-session", home=tmp_path)
    with pytest.raises(SessionNotFound):
        load_transcript(SESSION_ID, home=tmp_path)


def test_demo_prints_claude_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_transcript(tmp_path)

    result = demo_main(
        ["--agent", "claude", "--claude-home", str(tmp_path), SESSION_ID]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert f"Session: {SESSION_ID}" in captured.out
    assert "Build it." in captured.out
    assert "Ship it." in captured.out
    assert "Working on it." not in captured.out
    assert "/clear" not in captured.out
