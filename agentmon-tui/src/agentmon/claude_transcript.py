"""Read human-entered messages from a local Claude Code session transcript.

Claude Code stores each session as a JSONL file at
``<config>/projects/<project-slug>/<session-id>.jsonl``. That layout and the
per-record schema are implementation details, so this module depends on only
the small event shape needed for a visible transcript: it keeps ``user``
records that carry genuine human text and ignores tool results, meta records,
sidechain (subagent) turns, and the command/hook wrappers Claude Code injects.
Unknown records are ignored and malformed JSONL lines are reported on the
returned transcript instead of making an otherwise readable session unusable.

The public shape mirrors :mod:`agentmon.transcript` (the Codex reader) so both
back ends produce the same :class:`~agentmon.transcript.Transcript` value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from .transcript import (
    InvalidSessionId,
    SessionNotFound,
    Transcript,
    TranscriptError,
    TranscriptMessage,
    normalize_session_id,
)

__all__ = [
    "claude_home",
    "extract_transcript",
    "load_transcript",
    "resolve_transcript_path",
]

# Messages whose text is a single pseudo-XML wrapper (``<command-name>...``,
# ``<task-notification>...``, ``<bash-input>...``, and similar) are injected by
# the harness rather than typed by the person, so they are not part of a
# human-entered transcript.
_WRAPPER_TAG = re.compile(r"\A<[A-Za-z][\w-]*(?:\s|>|/>)")


def claude_home(path: str | os.PathLike[str] | None = None) -> Path:
    """Return an explicit Claude Code config dir or the conventional default."""

    if path is not None:
        return Path(path).expanduser()
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def resolve_transcript_path(
    session_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a Claude Code session id to its local transcript JSONL path.

    The session id is the transcript filename, so a direct lookup across the
    project directories is the fast path. A content scan keeps sessions usable
    when a transcript has been relocated under a different project slug.
    """

    canonical_id = normalize_session_id(session_id)
    projects = claude_home(home) / "projects"

    if projects.is_dir():
        matching = [
            path
            for path in projects.glob(f"*/{canonical_id}.jsonl")
            if path.is_file()
        ]
        if not matching:
            matching = [
                path
                for path in projects.glob("*/*.jsonl")
                if path.is_file() and _path_belongs_to_session(path, canonical_id)
            ]
        if matching:
            return max(matching, key=lambda path: path.stat().st_mtime_ns)

    raise SessionNotFound(
        f"Claude Code session {canonical_id} was not found under {projects}"
    )


def extract_transcript(
    transcript_path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
) -> Transcript:
    """Extract human-entered messages from a Claude Code transcript JSONL file."""

    path = Path(transcript_path)
    expected_id = normalize_session_id(session_id) if session_id is not None else None
    discovered_id: str | None = None
    messages: list[TranscriptMessage] = []
    skipped_lines: list[int] = []

    try:
        lines = path.open("rb")
    except OSError as error:
        raise SessionNotFound(
            f"could not read Claude Code transcript {path}: {error}"
        ) from error

    with lines:
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                skipped_lines.append(line_number)
                continue
            if not isinstance(record, dict):
                continue

            if discovered_id is None:
                candidate = record.get("sessionId")
                if isinstance(candidate, str):
                    try:
                        discovered_id = normalize_session_id(candidate)
                    except InvalidSessionId:
                        pass

            if record.get("type") != "user":
                continue
            if record.get("isSidechain") or record.get("isMeta"):
                continue

            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "user":
                continue

            text = _human_text(message.get("content"))
            if text is None:
                continue
            timestamp = record.get("timestamp")
            messages.append(
                TranscriptMessage(
                    role="user",
                    text=text,
                    timestamp=timestamp if isinstance(timestamp, str) else None,
                )
            )

    resolved_id = expected_id or discovered_id or _session_id_from_filename(path)
    if resolved_id is None:
        raise TranscriptError(f"could not determine the session id for {path}")
    if discovered_id is not None and discovered_id != resolved_id:
        raise TranscriptError(
            f"transcript {path} belongs to session {discovered_id}, not {resolved_id}"
        )

    return Transcript(
        session_id=resolved_id,
        rollout_path=path,
        messages=tuple(messages),
        skipped_lines=tuple(skipped_lines),
    )


def load_transcript(
    session_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Transcript:
    """Resolve a local Claude Code session and extract its human-entered messages."""

    canonical_id = normalize_session_id(session_id)
    transcript = resolve_transcript_path(canonical_id, home=home)
    return extract_transcript(transcript, session_id=canonical_id)


def _human_text(content: Any) -> str | None:
    """Return the human-typed text of a user message, or ``None`` to skip it.

    A plain string is the text a person entered. A content list is only
    human-entered when it is made of ``text`` blocks; a list carrying a
    ``tool_result`` block is Claude Code replaying tool output as a user turn.
    Harness wrappers (slash commands, hook and task notifications, captured
    bash) are dropped in either form.
    """

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                return None
            part = block.get("text")
            if isinstance(part, str):
                parts.append(part)
        text = "".join(parts)
    else:
        return None

    if not text.strip():
        return None
    if _WRAPPER_TAG.match(text.lstrip()):
        return None
    return text


def _path_belongs_to_session(path: Path, session_id: str) -> bool:
    if _session_id_from_filename(path) == session_id:
        return True
    try:
        with path.open("rb") as transcript:
            for _ in range(8):
                line = transcript.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                candidate = record.get("sessionId")
                if not isinstance(candidate, str):
                    continue
                try:
                    return normalize_session_id(candidate) == session_id
                except InvalidSessionId:
                    return False
    except OSError:
        return False
    return False


def _session_id_from_filename(path: Path) -> str | None:
    if path.suffix != ".jsonl":
        return None
    try:
        return normalize_session_id(path.stem)
    except InvalidSessionId:
        return None
