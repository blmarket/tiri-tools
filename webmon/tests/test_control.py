"""The control client: frame alignment, fed by hand."""

import asyncio

from conftest import run_async

from webmon.control import ControlClient


def test_a_reply_body_line_that_looks_like_end_does_not_close_the_frame():
    # A pane can print anything, including a line shaped like a notification.
    # Only an %end naming this frame's command number ends it.
    async def scenario():
        client = ControlClient("tmux", "/nonexistent", "$0")
        future = asyncio.get_running_loop().create_future()
        client._pending.append(future)
        client.feed(b"%begin 1788 42 1")
        client.feed(b"%end 1788 41 1")
        client.feed(b"%output %0 still-inside-the-frame")
        client.feed(b"%end 1788 42 1")
        return await future

    reply = run_async(scenario())
    assert reply.lines == [b"%end 1788 41 1", b"%output %0 still-inside-the-frame"]
    assert reply.error is False


def test_output_outside_a_frame_reaches_the_output_callback():
    async def scenario():
        got: list[tuple[str, bytes]] = []
        client = ControlClient(
            "tmux", "/nonexistent", "$0", on_output=lambda pane, data: got.append((pane, data))
        )
        client.feed(rb"%output %2 a\015\012")
        client.feed(rb"%extended-output %2 0 : b\015\012")
        return got

    assert run_async(scenario()) == [("%2", b"a\r\n"), ("%2", b"b\r\n")]


def test_group_delivers_at_reply_boundary_before_following_output():
    async def scenario():
        events = []
        client = ControlClient("tmux", "/nonexistent", "$0", on_output=lambda pane, data: events.append(data))
        client._ready.set()
        client._write = lambda command: None
        task = asyncio.create_task(client.execute_group(["capture-pane", "display-message"], lambda replies: events.append([reply.raw for reply in replies])))
        await asyncio.sleep(0)
        for line in [b"%begin 1 10 1", b"screen", b"%end 1 10 1", b"%begin 1 11 1", b"1 2", b"%end 1 11 1", b"%output %0 next"]:
            client.feed(line)
        await task
        assert events == [[b"screen", b"1 2"], b"next"]

    run_async(scenario())
