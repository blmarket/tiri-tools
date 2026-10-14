import asyncio
import fcntl
import os
from pathlib import Path
import pty
import signal
import struct
import sys
import termios

import pytest

from conftest import run_async
from test_monitor import Recorder, started, until
from webmon.sizing import StreamSizes, dimensions, is_sizing_session


def size(server, pane):
    return server.command("display-message", "-p", "-t", pane, "#{window_width}x#{window_height}").strip()


def policy(server, pane):
    return server.command("show-options", "-Awv", "-t", pane, "window-size").strip()


def clean(server, pane):
    window = server.command("display-message", "-p", "-t", pane, "#{window_id}").strip()
    return not any(
        is_sizing_session(name) and linked == window
        for line in server.command("list-windows", "-a", "-F", "#{session_name}|#{window_id}").splitlines()
        for name, linked in [line.split("|", 1)]
    )


def select_desktop_window(server, pane):
    window = server.command("display-message", "-p", "-t", pane, "#{window_id}").strip()
    server.command("select-window", "-t", f"work:{window}")


class Desktop:
    async def start(self, server, cols=122, rows=33, session="work"):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows + 1, cols, 0, 0))
        self.process = await asyncio.create_subprocess_exec(
            server.tmux, "-S", server.socket, "attach-session", "-t", session,
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        asyncio.get_running_loop().add_reader(self.master, self.drain)
        assert await until(lambda: "0" in server.command("list-clients", "-F", "#{client_control_mode}").splitlines())
        return self

    def drain(self):
        try:
            os.read(self.master, 65536)
        except OSError:
            asyncio.get_running_loop().remove_reader(self.master)

    def resize(self, cols, rows):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows + 1, cols, 0, 0))
        self.process.send_signal(signal.SIGWINCH)

    async def stop(self):
        if self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)


@pytest.mark.parametrize("value", [None, 0, -1, 1001, True, "40", 1.5])
def test_reject_invalid_dimensions(value):
    assert dimensions({"cols": 100, "rows": value}) is None
    assert dimensions({"cols": value, "rows": 40}) is None


@pytest.mark.parametrize("limit", [100, 80, None])
def test_browser_dimensions_and_opt_out(server, limit):
    async def scenario():
        pane = server.pane_ids()[0]
        original_size = size(server, pane)
        monitor = await started(server, stream_columns=limit)
        watcher = Recorder()
        try:
            await monitor.watch_pane(pane, watcher, {"cols": 120, "rows": 69})
            assert size(server, pane) == (f"{limit}x69" if limit else original_size)
            assert watcher.resets[-1][1]["sizing_columns"] == (limit or 0)
            await monitor.unwatch_pane(pane, watcher)
            assert clean(server, pane)
            assert not monitor.stream_sizes.clients
        finally:
            await monitor.stop()
    run_async(scenario())


def test_visible_viewers_determine_size_and_desktop_resizes(server):
    async def scenario():
        pane = server.pane_ids()[0]
        other = server.command("new-window", "-d", "-t", "work", "-P", "-F", "#{pane_id}", "sh").strip()
        desktop = await Desktop().start(server)
        select_desktop_window(server, other)
        monitor = await started(server, stream_columns=100)
        watcher = Recorder()
        try:
            await monitor.watch_pane(pane, watcher, {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x69"
            assert size(server, other) == "122x33"
            select_desktop_window(server, pane)
            assert await until(lambda: size(server, pane) == "100x33")
            desktop.resize(90, 40)
            assert await until(lambda: size(server, pane) == "90x40")
            await monitor.resize_pane(pane, watcher, {"cols": 80, "rows": 30})
            assert size(server, pane) == "80x30"
            select_desktop_window(server, other)
            assert await until(lambda: size(server, pane) == "80x30")
            await monitor.resize_pane(pane, watcher, {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x69"
            select_desktop_window(server, pane)
            assert await until(lambda: size(server, pane) == "90x40")
            await monitor.unwatch_pane(pane, watcher)
            assert await until(lambda: size(server, pane) == "90x40")
            assert policy(server, pane) == "smallest"
            assert clean(server, pane)
        finally:
            await monitor.stop()
            await desktop.stop()
    run_async(scenario())


def test_desktop_attach_and_detach_recalculate_watched_window(server):
    async def scenario():
        pane = server.pane_ids()[0]
        monitor = await started(server, stream_columns=100)
        desktop = None
        try:
            await monitor.watch_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x69"
            desktop = await Desktop().start(server)
            assert await until(lambda: size(server, pane) == "100x33")
            await desktop.stop()
            desktop = None
            assert await until(lambda: size(server, pane) == "100x69")
        finally:
            await monitor.stop()
            if desktop:
                await desktop.stop()
    run_async(scenario())


@pytest.mark.parametrize("original", [None, "latest", "smallest", "largest", "manual"])
def test_watched_window_uses_smallest_and_aggressive_resize(server, original):
    async def scenario():
        pane = server.pane_ids()[0]
        if original:
            server.command("set-option", "-w", "-t", pane, "window-size", original)
        hooks = server.command("show-hooks", "-g")
        monitor = await started(server, stream_columns=100)
        watcher = Recorder()
        try:
            await monitor.watch_pane(pane, watcher, {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x69"
            assert policy(server, pane) == "smallest"
            await monitor.unwatch_pane(pane, watcher)
            assert policy(server, pane) == "smallest"
            assert server.command("show-options", "-Awv", "-t", pane, "aggressive-resize").strip() == "on"
            assert clean(server, pane)
            assert server.command("show-hooks", "-g") == hooks
        finally:
            await monitor.stop()
    run_async(scenario())


def test_multiple_monitors_share_sizes_and_restore_after_last_view(server):
    async def scenario():
        pane = server.pane_ids()[0]
        first = await started(server, stream_columns=1000)
        second = await started(server, stream_columns=1000)
        a, b = Recorder(), Recorder()
        try:
            await asyncio.gather(first.watch_pane(pane, a, {"cols": 110, "rows": 40}),
                                 second.watch_pane(pane, b, {"cols": 90, "rows": 50}))
            assert await until(lambda: size(server, pane) == "90x40")
            await first.unwatch_pane(pane, a)
            assert size(server, pane) == "90x50"
            assert policy(server, pane) == "smallest"
            await second.unwatch_pane(pane, b)
            assert policy(server, pane) == "smallest"
            assert clean(server, pane)
        finally:
            await first.stop()
            await second.stop()
    run_async(scenario())


def test_switching_views_releases_previous_window(server):
    async def scenario():
        pane = server.pane_ids()[0]
        other = server.command("new-window", "-d", "-t", "work", "-P", "-F", "#{pane_id}", "sh").strip()
        monitor = await started(server, stream_columns=100)
        watcher = Recorder()
        try:
            await monitor.watch_pane(pane, watcher, {"cols": 100, "rows": 69})
            await monitor.unwatch_pane(pane, watcher)
            await monitor.watch_pane(other, watcher, {"cols": 90, "rows": 50})
            assert size(server, other) == "90x50"
            assert clean(server, pane)
            assert policy(server, pane) == "smallest"
        finally:
            await monitor.stop()
    run_async(scenario())


def test_unauthorized_resize_and_failed_attach(server):
    async def scenario():
        pane = server.pane_ids()[0]
        monitor = await started(server, stream_columns=100)
        try:
            await monitor.resize_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            assert not monitor.stream_sizes.clients
            with pytest.raises(ConnectionError):
                await monitor.stream_sizes.resize(object(), "%999", "$999", "@999", 100, 69)
            assert not monitor.stream_sizes.clients
        finally:
            await monitor.stop()
    run_async(scenario())


def test_deleted_pane_cleanup(server):
    async def scenario():
        pane = server.command("split-window", "-v", "-P", "-F", "#{pane_id}", "sh").strip()
        monitor = await started(server, stream_columns=100)
        try:
            await monitor.watch_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            server.command("kill-pane", "-t", pane)
            assert await until(lambda: not monitor.stream_sizes.clients)
        finally:
            await monitor.stop()
    run_async(scenario())


def test_process_crash_removes_browser_session(server):
    async def scenario():
        pane = server.pane_ids()[0]
        window = server.command("display-message", "-p", "-t", pane, "#{window_id}").strip()
        session = server.command("display-message", "-p", "-t", pane, "#{session_id}").strip()
        hooks = server.command("show-hooks", "-g")
        script = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from webmon.sizing import StreamSizes
async def main():
    sizes = StreamSizes(sys.argv[2], sys.argv[3], 100)
    await sizes.resize(object(), sys.argv[4], sys.argv[5], sys.argv[6], 100, 69)
    print('ready', flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "src"),
            server.tmux, server.socket, pane, session, window,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
            assert size(server, pane) == "100x69"
            process.kill()
            await process.wait()
            assert await until(lambda: policy(server, pane) == "smallest" and clean(server, pane))
            assert server.command("show-hooks", "-g") == hooks
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    run_async(scenario())


def test_status_line_changes_use_desktop_content_height(server):
    async def scenario():
        pane = server.pane_ids()[0]
        desktop = await Desktop().start(server)
        monitor = await started(server, stream_columns=100)
        try:
            await monitor.watch_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x33"
            server.command("set-option", "-t", "work", "status", "2")
            assert await until(lambda: size(server, pane) == "100x32")
            server.command("set-option", "-t", "work", "status", "off")
            assert await until(lambda: size(server, pane) == "100x34")
        finally:
            await monitor.stop()
            await desktop.stop()
    run_async(scenario())


def test_sizing_client_crash_cleans_up_and_can_reconnect(server):
    async def scenario():
        pane = server.pane_ids()[0]
        monitor = await started(server, stream_columns=100)
        watcher = Recorder()
        try:
            await monitor.watch_pane(pane, watcher, {"cols": 100, "rows": 69})
            view = next(iter(monitor.stream_sizes.clients.values()))
            view.client._process.kill()
            await view.client._process.wait()
            assert await until(lambda: policy(server, pane) == "smallest" and clean(server, pane))
            await monitor.resize_pane(pane, watcher, {"cols": 100, "rows": 69})
            assert size(server, pane) == "100x69"
            assert policy(server, pane) == "smallest"
        finally:
            await monitor.stop()
    run_async(scenario())


@pytest.mark.parametrize("base_index", [0, 1])
def test_browser_sessions_hidden_from_all_monitors_and_contain_only_target(server, base_index):
    async def scenario():
        server.command("set-option", "-g", "base-index", str(base_index))
        pane = server.pane_ids()[0]
        window = server.command("display-message", "-p", "-t", pane, "#{window_id}").strip()
        monitor = await started(server, stream_columns=100)
        observer = await started(server)
        original_session = monitor.model.session_of(pane)
        try:
            await monitor.watch_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            view = next(iter(monitor.stream_sizes.clients.values()))
            assert server.command("list-windows", "-t", view.client.session, "-F", "#{window_id}").splitlines() == [window]
            for instance in (monitor, observer):
                await instance._reconcile()
                await instance._poll()
                assert instance.model.order == [pane]
                assert instance.model.session_of(pane) == original_session
                assert set(instance._state_clients) == {original_session}
        finally:
            await monitor.stop()
            await observer.stop()
        assert clean(server, pane)
    run_async(scenario())


def test_browser_exit_removes_its_session_even_with_another_desktop(server):
    async def scenario():
        pane = server.pane_ids()[0]
        monitor = await started(server, stream_columns=100)
        desktop = None
        try:
            await monitor.watch_pane(pane, Recorder(), {"cols": 100, "rows": 69})
            view = next(iter(monitor.stream_sizes.clients.values()))
            desktop = await Desktop().start(server, session=view.client.session)
            view.client._process.kill()
            await view.client._process.wait()
            assert await until(lambda: clean(server, pane))
            await asyncio.wait_for(desktop.process.wait(), 5)
            assert server.command("list-panes", "-t", "work", "-F", "#{pane_id}").strip() == pane
        finally:
            await monitor.stop()
            if desktop:
                await desktop.stop()
    run_async(scenario())
