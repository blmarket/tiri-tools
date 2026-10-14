"""Read user interactions from a local Codex session rollout.

Codex's rollout JSONL is an implementation detail, so this module deliberately
depends on only the small event shapes needed for a visible transcript. Unknown
records are ignored and malformed JSONL lines are reported on the returned
transcript instead of making an otherwise readable session unusable.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Literal
from uuid import UUID


Role = Literal["user", "goal"]

__all__ = [
    "InvalidSessionId",
    "SessionNotFound",
    "Transcript",
    "TranscriptError",
    "TranscriptMessage",
    "codex_home",
    "extract_transcript",
    "load_transcript",
    "normalize_session_id",
    "resolve_rollout_path",
]

_STATE_DATABASE = re.compile(r"state_(\d+)\.sqlite\Z")


class TranscriptError(Exception):
    """Base class for transcript lookup and parsing errors."""


class InvalidSessionId(TranscriptError, ValueError):
    """The supplied value is not a UUID-shaped Codex session id."""


class SessionNotFound(TranscriptError, FileNotFoundError):
    """No local Codex rollout could be found for a session id."""


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    """One user-visible interaction from a Codex rollout."""

    role: Role
    text: str
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class Transcript:
    """The user-visible interactions extracted from one Codex session."""

    session_id: str
    rollout_path: Path
    messages: tuple[TranscriptMessage, ...]
    skipped_lines: tuple[int, ...] = ()


def codex_home(path: str | os.PathLike[str] | None = None) -> Path:
    """Return an explicit Codex home or the conventional local default."""

    if path is not None:
        return Path(path).expanduser()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def normalize_session_id(session_id: str) -> str:
    """Validate and return the canonical spelling of a Codex session UUID."""

    try:
        return str(UUID(session_id))
    except (ValueError, AttributeError) as error:
        raise InvalidSessionId(f"invalid Codex session id: {session_id!r}") from error


def resolve_rollout_path(
    session_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a Codex session id to its local rollout JSONL path.

    The current Codex state database is the fast path. A filesystem fallback
    keeps older installations and partially rebuilt databases usable.
    """

    canonical_id = normalize_session_id(session_id)
    root = codex_home(home)

    for database in _state_databases(root):
        rollout = _rollout_from_database(database, canonical_id, root)
        if rollout is not None and _path_belongs_to_session(rollout, canonical_id):
            return rollout

    candidates: list[Path] = []
    filename = f"rollout-*-{canonical_id}.jsonl"
    for directory in (root / "sessions", root / "archived_sessions"):
        if directory.is_dir():
            candidates.extend(directory.glob(f"**/{filename}"))

    matching = [
        path
        for path in candidates
        if path.is_file() and _path_belongs_to_session(path, canonical_id)
    ]
    if matching:
        return max(matching, key=lambda path: path.stat().st_mtime_ns)

    raise SessionNotFound(
        f"Codex session {canonical_id} was not found under {root}"
    )


def extract_transcript(
    rollout_path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
) -> Transcript:
    """Extract user messages and goal objectives from a rollout JSONL file."""

    path = Path(rollout_path)
    expected_id = normalize_session_id(session_id) if session_id is not None else None
    discovered_id: str | None = None
    messages: list[TranscriptMessage] = []
    skipped_lines: list[int] = []
    seen_goal_objectives: set[str] = set()

    try:
        lines = path.open("rb")
    except OSError as error:
        raise SessionNotFound(
            f"could not read Codex rollout {path}: {error}"
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

            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if record.get("type") == "session_meta" and discovered_id is None:
                candidate = payload.get("id")
                if isinstance(candidate, str):
                    try:
                        discovered_id = normalize_session_id(candidate)
                    except InvalidSessionId:
                        pass

            if record.get("type") == "event_msg":
                event_type = payload.get("type")
                if event_type == "user_message":
                    role: Role = "user"
                    text = payload.get("message")
                elif event_type == "thread_goal_updated":
                    role = "goal"
                    goal = payload.get("goal")
                    text = goal.get("objective") if isinstance(goal, dict) else None
                    if (
                        isinstance(text, str)
                        and text
                        and text not in seen_goal_objectives
                    ):
                        seen_goal_objectives.add(text)
                        text = f"/goal {text}"
                    else:
                        continue
                else:
                    continue
            elif record.get("type") == "response_item":
                role = "user"
                text = _response_item_user_text(payload)
            else:
                continue
            if not isinstance(text, str) or not text:
                continue
            timestamp = record.get("timestamp")
            messages.append(
                TranscriptMessage(
                    role=role,
                    text=text,
                    timestamp=timestamp if isinstance(timestamp, str) else None,
                )
            )

    resolved_id = expected_id or discovered_id or _session_id_from_filename(path)
    if resolved_id is None:
        raise TranscriptError(f"could not determine the session id for {path}")
    if discovered_id is not None and discovered_id != resolved_id:
        raise TranscriptError(
            f"rollout {path} belongs to session {discovered_id}, not {resolved_id}"
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
    """Resolve a local Codex session and extract its user interactions."""

    canonical_id = normalize_session_id(session_id)
    rollout = resolve_rollout_path(canonical_id, home=home)
    return extract_transcript(rollout, session_id=canonical_id)


def _state_databases(root: Path) -> Iterator[Path]:
    databases: list[tuple[int, Path]] = []
    for path in root.glob("state_*.sqlite"):
        match = _STATE_DATABASE.fullmatch(path.name)
        if match and path.is_file():
            databases.append((int(match.group(1)), path))
    for _, path in sorted(databases, reverse=True):
        yield path


def _rollout_from_database(
    database: Path, session_id: str, root: Path
) -> Path | None:
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None

    if row is None or not isinstance(row[0], str):
        return None
    path = Path(row[0]).expanduser()
    if not path.is_absolute():
        path = root / path
    return path if path.is_file() else None


def _path_belongs_to_session(path: Path, session_id: str) -> bool:
    filename_id = _session_id_from_filename(path)
    if filename_id is not None:
        return filename_id == session_id

    try:
        with path.open("rb") as rollout:
            for _ in range(8):
                line = rollout.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("id"), str
                ):
                    return False
                try:
                    return normalize_session_id(payload["id"]) == session_id
                except InvalidSessionId:
                    return False
    except OSError:
        return False
    return False


def _session_id_from_filename(path: Path) -> str | None:
    name = path.name
    if not (name.startswith("rollout-") and name.endswith(".jsonl")):
        return None
    candidate = name[-42:-6]
    try:
        return normalize_session_id(candidate)
    except InvalidSessionId:
        return None


def _response_item_user_text(payload: dict[str, object]) -> str | None:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None

    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        return None
    content_item_kinds = metadata.get("content_item_kinds")
    if (
        not isinstance(content_item_kinds, list)
        or "user.text" not in content_item_kinds
    ):
        return None

    content = payload.get("content")
    if not isinstance(content, list):
        return None
    text = "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "input_text"
        and isinstance(item.get("text"), str)
    )
    return text or None
