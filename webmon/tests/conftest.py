"""Fixtures for the webmon tests.

The integration tests drive a real server, chosen by `SUT`: `c2rs` (default)
is the transpiled tmux binary, `stock` is the pinned tmux, which is what the
degradation tests compare against. webmon is a viewer of tmux-compatible
servers; it does not gate the hmux daemon.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# sun_path holds 108 bytes including the NUL, and a socket that overruns it
# fails in a way that reads like a server bug rather than a path bug.
SOCKET_PATH_LIMIT = 107


def _tmux_client() -> str:
    return os.environ.get("WEBMON_TMUX") or shutil.which("tmux") or "tmux"


def _subject() -> tuple[str, str]:
    """Return (kind, binary) for the server under test."""
    explicit = os.environ.get("WEBMON_SERVER_BIN")
    subject = os.environ.get("SUT", "c2rs")
    if explicit:
        return subject, explicit
    if subject == "stock":
        return "stock", _tmux_client()
    return "c2rs", str(REPO_ROOT / "hmux" / "target" / "debug" / "hmux")


def subject_kind() -> str:
    """Which server the suite is pointed at: c2rs, hmux0, or stock."""
    return _subject()[0]


def run_async(coro):
    """Run one coroutine to completion; the suite needs no asyncio plugin."""
    return asyncio.run(coro)


class Server:
    """A live server on a private socket, plus a client to drive it."""

    def __init__(self, socket_path: str, tmux: str, kind: str) -> None:
        self.socket = socket_path
        self.tmux = tmux
        self.kind = kind
        self.process: subprocess.Popen[bytes] | None = None

    def command(self, *arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            [self.tmux, "-S", self.socket, *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if check and result.returncode:
            raise AssertionError(
                f"{' '.join(arguments)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    def pane_ids(self) -> list[str]:
        return self.command("list-panes", "-a", "-F", "#{pane_id}").split()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("condition not reached in time")

    def kill(self) -> None:
        # A server that is not answering still has to be torn down, so the
        # polite request is best-effort and the process is dealt with either
        # way.
        try:
            subprocess.run(
                [self.tmux, "-S", self.socket, "kill-server"],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _wait_for_socket(path: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
                return
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.05)
    raise AssertionError(f"server did not come up on {path}")


@pytest.fixture
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    kind, binary = _subject()
    if not Path(binary).exists() and shutil.which(binary) is None:
        pytest.skip(f"server binary for SUT={kind} not built: {binary}")
    tmux = _tmux_client()
    if shutil.which(tmux) is None and not Path(tmux).exists():
        pytest.skip("no tmux client available to drive the server")
    version = subprocess.run([tmux, "-V"], capture_output=True, text=True, timeout=10).stdout.strip()
    if version != "tmux 3.8":
        pytest.fail(f"webmon integration requires pinned tmux 3.8, found {version}")

    base = Path(os.environ.get("TMPDIR", "/tmp"))
    directory = Path(str(tmp_path_factory.mktemp("wm", numbered=True)))
    socket_path = str(directory / "s")
    if len(socket_path.encode()) > SOCKET_PATH_LIMIT:
        socket_path = str(base / f"wm{os.getpid()}.s")
    if len(socket_path.encode()) > SOCKET_PATH_LIMIT:
        pytest.skip(f"no socket path under {SOCKET_PATH_LIMIT} bytes available")

    live = Server(socket_path, tmux, kind)
    subprocess.run(
        [
            binary,
            "-S",
            socket_path,
            "new-session",
            "-d",
            "-s",
            "work",
            "-x",
            "80",
            "-y",
            "24",
            "sh",
        ],
        check=True,
        timeout=15,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_socket(socket_path)
    live.wait_for(lambda: bool(live.pane_ids()))
    try:
        yield live
    finally:
        live.kill()


@pytest.fixture
def stock_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A stock tmux server: no agent plugin, so the hmux variables expand empty."""
    tmux = _tmux_client()
    if shutil.which(tmux) is None and not Path(tmux).exists():
        pytest.skip("no tmux binary to run a stock server")
    try:
        version = subprocess.run(
            [tmux, "-V"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("tmux -V did not answer")
    if version.strip() != "tmux 3.8":
        pytest.fail(f"stock comparison requires pinned tmux 3.8, found {version.strip()}")

    directory = Path(str(tmp_path_factory.mktemp("wms", numbered=True)))
    socket_path = str(directory / "s")
    if len(socket_path.encode()) > SOCKET_PATH_LIMIT:
        socket_path = str(Path(os.environ.get("TMPDIR", "/tmp")) / f"wms{os.getpid()}.s")
    live = Server(socket_path, tmux, "stock")
    subprocess.run(
        [tmux, "-S", socket_path, "new-session", "-d", "-s", "work", "-x", "80", "-y", "24", "sh"],
        check=True,
        timeout=15,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_socket(socket_path)
    live.wait_for(lambda: bool(live.pane_ids()))
    try:
        yield live
    finally:
        live.kill()
