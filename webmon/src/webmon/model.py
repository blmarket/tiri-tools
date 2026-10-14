"""The in-memory picture of a server: sessions, windows, panes, pane state.

Two sources feed it. A `list-panes -a` poll is authoritative and rebuilds the
structure; `%subscription-changed` rows patch the volatile fields of a pane
that is already known. A row for an unknown pane does not invent one — it
marks the model stale so the next poll settles it, which keeps the structure
single-sourced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import PANE_STATE_FIELDS, POLL_FIELDS

_INT_FIELDS = ("pane_width", "pane_height", "window_index", "pane_index")
_BOOL_FIELDS = ("pane_active", "window_active", "alternate_on")


def normalize(row: dict[str, str]) -> dict[str, object]:
    """Coerce a raw format row into the JSON shape browsers receive."""
    out: dict[str, object] = {}
    for key, value in row.items():
        if key in _INT_FIELDS:
            try:
                out[key] = int(value)
            except ValueError:
                out[key] = 0
        elif key in _BOOL_FIELDS:
            out[key] = value == "1"
        else:
            out[key] = value
    return out


@dataclass
class Delta:
    """What changed since the last broadcast."""

    structure: bool = False
    panes: dict[str, dict[str, object]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.structure or self.panes or self.removed)

    def merge(self, other: "Delta") -> None:
        self.structure = self.structure or other.structure
        for pane, fields_ in other.panes.items():
            self.panes.setdefault(pane, {}).update(fields_)
        for pane in other.removed:
            self.panes.pop(pane, None)
            if pane not in self.removed:
                self.removed.append(pane)


class Model:
    """Sessions, windows and panes as last observed."""

    def __init__(self) -> None:
        self.panes: dict[str, dict[str, object]] = {}
        self.order: list[str] = []
        self.tier: str = "unknown"
        self.stale: bool = True
        self.socket: str = ""
        self.error: str = ""

    def apply_poll(self, rows: list[dict[str, str]]) -> Delta:
        """Replace the structure from an authoritative listing."""
        delta = Delta()
        seen: list[str] = []
        for raw in rows:
            pane_id = raw.get("pane_id", "")
            if not pane_id:
                continue
            seen.append(pane_id)
            values = normalize(raw)
            current = self.panes.get(pane_id)
            if current is None:
                self.panes[pane_id] = values
                delta.panes[pane_id] = values
                continue
            changed = {
                key: value for key, value in values.items() if current.get(key) != value
            }
            if changed:
                current.update(changed)
                delta.panes[pane_id] = changed
        for pane_id in list(self.panes):
            if pane_id not in seen:
                del self.panes[pane_id]
                delta.removed.append(pane_id)
        if seen != self.order:
            self.order = seen
            delta.structure = True
        if delta.removed:
            delta.structure = True
        self.stale = False
        return delta

    def apply_state_row(self, row: dict[str, str]) -> Delta:
        """Patch one pane's volatile fields from a subscription row."""
        delta = Delta()
        pane_id = row.get("pane_id", "")
        if not pane_id:
            return delta
        current = self.panes.get(pane_id)
        if current is None:
            self.stale = True
            delta.structure = True
            return delta
        values = normalize({key: row[key] for key in PANE_STATE_FIELDS if key in row})
        changed = {
            key: value for key, value in values.items() if current.get(key) != value
        }
        if changed:
            current.update(changed)
            delta.panes[pane_id] = changed
        return delta

    def pane(self, pane_id: str) -> dict[str, object] | None:
        return self.panes.get(pane_id)

    def session_of(self, pane_id: str) -> str | None:
        """Return the session id a pane lives in.

        Ids, not names: a session id is what a `-t` target can never resolve
        ambiguously, and it is what the notifications carry.
        """
        pane = self.panes.get(pane_id)
        if pane is None:
            return None
        session_id = pane.get("session_id")
        return str(session_id) if session_id else None

    def panes_in_session(self, session_id: str) -> list[str]:
        return [
            pane_id
            for pane_id, pane in self.panes.items()
            if pane.get("session_id") == session_id
        ]

    def sessions(self) -> list[dict[str, object]]:
        """Group the flat pane map into sessions → windows → panes."""
        sessions: dict[str, dict[str, object]] = {}
        for pane_id in self.order:
            pane = self.panes.get(pane_id)
            if pane is None:
                continue
            session_id = str(pane.get("session_id", ""))
            session = sessions.get(session_id)
            if session is None:
                session = {
                    "id": session_id,
                    "name": pane.get("session_name", ""),
                    "windows": [],
                }
                sessions[session_id] = session
            windows = session["windows"]
            assert isinstance(windows, list)
            window_id = str(pane.get("window_id", ""))
            window = next((w for w in windows if w["id"] == window_id), None)
            if window is None:
                window = {
                    "id": window_id,
                    "index": pane.get("window_index", 0),
                    "name": pane.get("window_name", ""),
                    "active": pane.get("window_active", False),
                    "panes": [],
                }
                windows.append(window)
            window["panes"].append(pane_id)
        return list(sessions.values())

    def snapshot(self) -> dict[str, object]:
        return {
            "t": "model",
            "tier": self.tier,
            "socket": self.socket,
            "error": self.error,
            "sessions": self.sessions(),
            "panes": self.panes,
        }

    def delta_message(self, delta: Delta) -> dict[str, object]:
        if delta.structure:
            return self.snapshot()
        return {"t": "panes", "panes": delta.panes, "removed": delta.removed}


def rows_from_poll(text: str) -> list[dict[str, str]]:
    """Parse the tab-separated output of the poll listing."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != len(POLL_FIELDS):
            continue
        rows.append(dict(zip(POLL_FIELDS, parts)))
    return rows
