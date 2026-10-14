"""The control client: frame alignment against a live server, and by hand."""

import asyncio

from conftest import run_async

from webmon.control import ControlClient
from webmon.protocol import subscription_command


def client_for(server) -> ControlClient:
    return ControlClient(server.tmux, server.socket, "$0")


def test_attach_flags_match_what_the_state_plane_needs(server):
    client = client_for(server)
    assert client.argv[:4] == [server.tmux, "-S", server.socket, "-C"]
    assert "-f" in client.argv
    assert client.argv[client.argv.index("-f") + 1] == "no-output,ignore-size"


def test_a_command_reply_comes_back_to_its_caller(server):
    async def scenario():
        client = client_for(server)
        await client.start()
        try:
            reply = await client.execute("display-message -p '#{session_name}'")
            return reply.text, reply.error
        finally:
            await client.stop()

    text, error = run_async(scenario())
    assert text == "work"
    assert error is False


def test_replies_stay_aligned_when_several_commands_are_in_flight(server):
    async def scenario():
        client = client_for(server)
        await client.start()
        try:
            pending = [
                client.execute(f"display-message -p '{index}'") for index in range(5)
            ]
            return [reply.text for reply in await asyncio.gather(*pending)]
        finally:
            await client.stop()

    assert run_async(scenario()) == ["0", "1", "2", "3", "4"]


def test_a_failing_command_is_reported_as_an_error_frame(server):
    async def scenario():
        client = client_for(server)
        await client.start()
        try:
            return await client.execute("capture-pane -p -t %999")
        finally:
            await client.stop()

    reply = run_async(scenario())
    assert reply.error is True


def test_capture_pane_keeps_the_escape_bytes_it_was_asked_for(server):
    server.command("send-keys", "-t", "%0", "-l", "printf '\\033[31mRED\\033[0m\\n'")
    server.command("send-keys", "-t", "%0", "Enter")
    # The echoed command line contains "RED" too; only the rendered output
    # carries a real ESC byte, so that is what says the printf actually ran.
    server.wait_for(
        lambda: "\x1b[31m" in server.command("capture-pane", "-e", "-p", "-t", "%0")
    )

    async def scenario():
        client = client_for(server)
        await client.start()
        try:
            return await client.execute("capture-pane -e -p -t %0")
        finally:
            await client.stop()

    reply = run_async(scenario())
    assert b"\x1b[31mRED" in reply.raw


def test_a_subscription_reports_the_panes_of_the_attached_session(server):
    async def scenario():
        seen = []
        client = ControlClient(
            server.tmux,
            server.socket,
            "$0",
            on_subscription=seen.append,
        )
        await client.start()
        try:
            client.send(subscription_command())
            deadline = asyncio.get_running_loop().time() + 8
            while not seen and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
            return seen
        finally:
            await client.stop()

    seen = run_async(scenario())
    assert seen, "no %subscription-changed arrived"
    assert seen[0].name == "webmon"
    assert seen[0].pane.startswith("%")


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


def test_failed_group_does_not_consume_a_later_command_reply(server):
    async def scenario():
        client = ControlClient(server.tmux, server.socket, "$0")
        await client.start()
        try:
            replies = await client.execute_group(["capture-pane -p -t '%4294967294'", "display-message -p skipped"], lambda replies: None)
            assert replies[0].error
            reply = await client.execute("display-message -p aligned")
            assert reply.text == "aligned" and not reply.error
        finally:
            await client.stop()

    run_async(scenario())
