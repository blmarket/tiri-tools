"""What happens when a browser or a pane outruns the other end."""

import asyncio
import json
import struct

from conftest import run_async

from webmon.monitor import Monitor
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


def test_a_flooding_pane_keeps_the_monitor_and_its_model_alive(server):
    """A pane spraying output must not wedge the state plane behind it."""

    async def scenario():
        monitor = Monitor(server.tmux, server.socket)
        await monitor.start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while not monitor.model.panes and loop.time() < deadline:
            await asyncio.sleep(0.05)

        class Sink:
            def __init__(self) -> None:
                self.bytes = 0
                self.resets = 0

            def deliver_model(self, message: dict) -> None:
                pass

            def deliver_screen(self, pane: str, data: bytes) -> None:
                self.bytes += len(data)

            def deliver_reset(self, pane: str, payload: dict) -> None:
                self.resets += 1

        sink = Sink()
        monitor.add_watcher(sink)
        try:
            server.command(
                "new-window", "-t", "work", "-n", "flood",
                "sh -c 'while :; do printf FLOODFLOODFLOOD; done'",
            )
            deadline = loop.time() + 15
            flood = None
            while loop.time() < deadline:
                flood = next(
                    (
                        name
                        for name, values in monitor.model.panes.items()
                        if values["window_name"] == "flood"
                    ),
                    None,
                )
                if flood:
                    break
                await asyncio.sleep(0.05)
            assert flood, "the flooding window never reached the model"
            await monitor.watch_pane(flood, sink)
            await asyncio.sleep(5)
            # The model still answers while the pane is still spraying.
            before = dict(monitor.model.panes)
            server.command("new-window", "-t", "work", "-n", "after", "sh")
            appeared = False
            deadline = loop.time() + 15
            while loop.time() < deadline:
                if any(
                    values["window_name"] == "after"
                    for values in monitor.model.panes.values()
                ):
                    appeared = True
                    break
                await asyncio.sleep(0.1)
            return sink.bytes, sink.resets, appeared, len(before)
        finally:
            server.command("kill-window", "-t", "work:flood", check=False)
            await monitor.stop()

    streamed, resets, appeared, _ = run_async(scenario())
    assert streamed > 0, "the viewer saw none of the flood"
    assert resets >= 1, "the pane was never painted"
    assert appeared, "the state plane stopped answering while a pane flooded"
