"""Pure parsing of the tmux control-mode stream.

Nothing here talks to a server; every function maps bytes or a line to a
value, so the whole control dialect is testable without a running tmux.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUBSCRIPTION_NAME = "webmon"

# Volatile per-pane fields. `refresh-client -B` re-evaluates this for every
# pane of the attached session and reports the ones whose value changed, so it
# carries what a dashboard repaints, not the structure it hangs off.
PANE_STATE_FIELDS = (
    "pane_id",
    "pane_active",
    "pane_width",
    "pane_height",
    "pane_current_command",
    "pane_current_path",
    "pane_title",
    "pane_agent",
    "pane_agent_state",
    "pane_state_emoji",
    "pane_agent_model",
    "pane_agent_session_id",
    "alternate_on",
)

# The structural fields a poll adds: which session and window a pane hangs in.
PANE_STRUCTURE_FIELDS = (
    "session_id",
    "session_name",
    "window_id",
    "window_index",
    "window_name",
    "window_active",
    "pane_index",
)

POLL_FIELDS = PANE_STRUCTURE_FIELDS + PANE_STATE_FIELDS


def _format(fields: tuple[str, ...]) -> str:
    return "\t".join("#{%s}" % name for name in fields)


POLL_FORMAT = _format(POLL_FIELDS)
SUBSCRIPTION_FORMAT = _format(PANE_STATE_FIELDS)


def subscription_command(name: str = SUBSCRIPTION_NAME) -> str:
    """Return the `refresh-client -B` command that opens the state feed."""
    return f"refresh-client -B '{name}:%*:{SUBSCRIPTION_FORMAT}'"


# Notifications that mean the pane/window/session structure may have moved, so
# the model has to be rebuilt from a poll rather than patched.
STRUCTURE_NOTIFICATIONS = (
    "%layout-change",
    "%session-changed",
    "%session-closed",
    "%session-renamed",
    "%session-window-changed",
    "%sessions-changed",
    "%unlinked-window-add",
    "%unlinked-window-close",
    "%unlinked-window-renamed",
    "%window-add",
    "%window-close",
    "%window-pane-changed",
    "%window-renamed",
)

# The subset that also changes which sessions exist, so the set of control
# clients has to be reconciled too.
SESSION_SET_NOTIFICATIONS = ("%sessions-changed", "%session-closed")


@dataclass(frozen=True)
class Output:
    """A `%output` line: raw bytes a pane wrote."""

    pane: str
    data: bytes


@dataclass(frozen=True)
class SubscriptionChanged:
    """A `%subscription-changed` line with its expanded format value."""

    name: str
    session: str
    window: str
    window_index: str
    pane: str
    value: str


@dataclass(frozen=True)
class Begin:
    """`%begin` — the command reply that follows belongs to this frame."""

    timestamp: str
    number: str
    flags: str


@dataclass(frozen=True)
class End:
    """`%end` or `%error` closing a reply frame."""

    timestamp: str
    number: str
    flags: str
    error: bool


@dataclass(frozen=True)
class Notification:
    """Any other `%name ...` line."""

    name: str
    arguments: str


@dataclass(frozen=True)
class Reply:
    """A completed command reply: the lines between `%begin` and `%end`.

    Body lines are kept as bytes because tmux does not escape them: a
    `capture-pane -e` reply carries raw ESC and tab bytes, and decoding them
    early would round-trip a pane's screen through a lossy replacement.
    """

    number: str
    error: bool
    lines: list[bytes] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.decode("utf-8", "replace") for line in self.lines)

    @property
    def raw(self) -> bytes:
        return b"\n".join(self.lines)


_OCTAL = b"01234567"


def unescape_output(payload: bytes) -> bytes:
    r"""Decode the escaping tmux applies to `%output` payloads.

    tmux writes any byte below 0x20 and the backslash itself as `\ooo`; every
    other byte, DEL included, goes out verbatim. A lone backslash followed by
    something that is not three octal digits cannot come from tmux, so it is
    passed through rather than swallowed.
    """
    if b"\\" not in payload:
        return payload
    out = bytearray()
    index = 0
    length = len(payload)
    while index < length:
        byte = payload[index]
        if byte != 0x5C:
            out.append(byte)
            index += 1
            continue
        digits = payload[index + 1 : index + 4]
        if len(digits) == 3 and all(digit in _OCTAL for digit in digits):
            out.append(int(digits, 8) & 0xFF)
            index += 4
            continue
        if payload[index + 1 : index + 2] == b"\\":
            out.append(0x5C)
            index += 2
            continue
        out.append(byte)
        index += 1
    return bytes(out)


OUTPUT_PREFIXES = (b"%output ", b"%extended-output ")


def parse_output(line: bytes) -> Output | None:
    """Parse a pane-output line, returning the pane's decoded bytes.

    A client attached with `pause-after` is sent `%extended-output <pane>
    <age> : <data>` instead of `%output <pane> <data>` — same payload, same
    escaping, one more field. Both spellings land here.
    """
    if line.startswith(b"%output "):
        rest = line[len(b"%output ") :]
        pane, separator, payload = rest.partition(b" ")
        if not separator and not pane.startswith(b"%"):
            return None
        return Output(pane.decode("ascii", "replace"), unescape_output(payload))
    if line.startswith(b"%extended-output "):
        rest = line[len(b"%extended-output ") :]
        pane, separator, remainder = rest.partition(b" ")
        if not separator:
            return None
        _age, separator, payload = remainder.partition(b" : ")
        if not separator:
            return None
        return Output(pane.decode("ascii", "replace"), unescape_output(payload))
    return None


def parse_subscription(line: str) -> SubscriptionChanged | None:
    """Parse `%subscription-changed <name> $s @w i %p : <value>`."""
    if not line.startswith("%subscription-changed "):
        return None
    head, separator, value = line.partition(" : ")
    parts = head.split(" ")
    if not separator or len(parts) != 6:
        return None
    _, name, session, window, window_index, pane = parts
    return SubscriptionChanged(name, session, window, window_index, pane, value)


def parse_line(line: str) -> Begin | End | SubscriptionChanged | Notification | None:
    """Classify a decoded control line. Returns None for reply body lines."""
    if not line.startswith("%"):
        return None
    name, _, arguments = line.partition(" ")
    if name == "%begin":
        parts = arguments.split(" ")
        if len(parts) == 3:
            return Begin(*parts)
        return None
    if name in ("%end", "%error"):
        parts = arguments.split(" ")
        if len(parts) == 3:
            return End(*parts, error=name == "%error")
        return None
    if name == "%subscription-changed":
        return parse_subscription(line)
    return Notification(name, arguments)


def is_structure_notification(name: str) -> bool:
    return name in STRUCTURE_NOTIFICATIONS


def is_session_set_notification(name: str) -> bool:
    return name in SESSION_SET_NOTIFICATIONS


def split_state_row(value: str) -> dict[str, str] | None:
    """Turn a subscription or poll row into a field mapping."""
    parts = value.split("\t")
    if len(parts) == len(PANE_STATE_FIELDS):
        return dict(zip(PANE_STATE_FIELDS, parts))
    if len(parts) == len(POLL_FIELDS):
        return dict(zip(POLL_FIELDS, parts))
    return None
