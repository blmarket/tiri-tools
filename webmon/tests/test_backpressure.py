"""What happens when a browser or a pane outruns the other end."""

import json
import struct


from webmon.server import SCREEN_BUFFER_CAP, Connection, pane_number


class FakeSocket:
    """A WebSocket whose send buffer the test controls."""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.binaries: list[bytes] = []
        self.buffered = 0
        self.closed = False

    def send_text(self, text: str) -> None:
        self.texts.append(json.loads(text))

    def send_binary(self, payload: bytes) -> None:
        self.binaries.append(payload)


def connection() -> tuple[Connection, FakeSocket]:
    socket = FakeSocket()
    return Connection(socket, None), socket  # type: ignore[arg-type]


def test_screen_bytes_are_framed_with_the_pane_number():
    conn, socket = connection()
    conn.deliver_screen("%7", b"hello")
    assert socket.binaries == [struct.pack("!I", 7) + b"hello"]
    assert pane_number("%7") == 7


def test_a_browser_that_falls_behind_is_told_and_then_dropped():
    conn, socket = connection()
    socket.buffered = SCREEN_BUFFER_CAP + 1
    conn.deliver_screen("%1", b"first")
    conn.deliver_screen("%1", b"second")
    assert socket.binaries == [], "nothing is queued for a browser already behind"
    assert socket.texts == [{"t": "screen-lagged", "pane": "%1"}], (
        "the browser is told once, not once per chunk"
    )


def test_other_panes_keep_streaming_while_one_is_dropped():
    conn, socket = connection()
    socket.buffered = SCREEN_BUFFER_CAP + 1
    conn.deliver_screen("%1", b"dropped")
    socket.buffered = 0
    conn.deliver_screen("%2", b"kept")
    assert socket.binaries == [struct.pack("!I", 2) + b"kept"]


def test_a_repaint_puts_a_dropped_pane_back_on_the_stream():
    conn, socket = connection()
    socket.buffered = SCREEN_BUFFER_CAP + 1
    conn.deliver_screen("%1", b"dropped")
    socket.buffered = 0
    conn.deliver_reset("%1", {"t": "screen", "pane": "%1", "data": "\x1bc"})
    conn.deliver_screen("%1", b"live")
    assert socket.binaries == [struct.pack("!I", 1) + b"live"]
