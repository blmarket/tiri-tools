"""Socket discovery: what `webmon` with no arguments decides to watch."""

import os
import asyncio
import pathlib
import shutil
import socket as socketlib
import tempfile

import pytest

from webmon import cli


@pytest.fixture
def reachable(monkeypatch):
    """Let a test declare which socket paths answer."""
    answering: set[str] = set()

    def fake(path: str, tmux: str) -> bool:
        return path in answering

    monkeypatch.setattr(cli, "_reachable", fake)
    return answering


def test_the_default_socket_is_what_a_plain_tmux_would_use(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    assert cli.default_socket() == str(tmp_path / f"tmux-{os.geteuid()}" / "default")
    monkeypatch.delenv("TMUX_TMPDIR")
    assert cli.default_socket().startswith("/tmp/tmux-")


def test_no_arguments_picks_the_default_socket(monkeypatch, tmp_path, reachable):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("HMUX_SOCKET", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    reachable.add(cli.default_socket())
    assert cli.discover_socket() == cli.default_socket()


def test_the_default_socket_wins_over_the_environment(monkeypatch, tmp_path, reachable):
    # Starting webmon from inside a tmux pane on some other socket should not
    # quietly point it at that server instead.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.setenv("HMUX_SOCKET", "/run/hmux.sock")
    monkeypatch.setenv("TMUX", "/run/other.sock,123,0")
    reachable.update({cli.default_socket(), "/run/hmux.sock", "/run/other.sock"})
    assert cli.discover_socket() == cli.default_socket()


def test_hmux_socket_is_the_first_fallback(monkeypatch, tmp_path, reachable):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.setenv("HMUX_SOCKET", "/run/hmux.sock")
    monkeypatch.setenv("TMUX", "/run/other.sock,123,0")
    reachable.update({"/run/hmux.sock", "/run/other.sock"})
    assert cli.discover_socket() == "/run/hmux.sock"


def test_the_surrounding_pane_is_the_last_environment_fallback(
    monkeypatch, tmp_path, reachable
):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("HMUX_SOCKET", raising=False)
    monkeypatch.setenv("TMUX", "/run/other.sock,123,0")
    reachable.add("/run/other.sock")
    assert cli.discover_socket() == "/run/other.sock"


def test_another_socket_in_the_same_directory_is_tried_last(monkeypatch, reachable):
    # Binding a real socket is what makes S_ISSOCK true, and a socket path is
    # capped at 107 bytes, so this cannot use pytest's own temp directory.
    base = tempfile.mkdtemp(prefix="wmc", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", base)
    monkeypatch.delenv("HMUX_SOCKET", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    directory = pathlib.Path(base) / f"tmux-{os.geteuid()}"
    directory.mkdir()
    sibling = directory / "other"
    listener = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    try:
        listener.bind(str(sibling))
        reachable.add(str(sibling))
        assert cli.discover_socket() == str(sibling)
    finally:
        listener.close()
        shutil.rmtree(base, ignore_errors=True)


def test_an_explicit_socket_is_used_or_reported(monkeypatch, reachable):
    reachable.add("/run/wanted.sock")
    assert cli.discover_socket("/run/wanted.sock") == "/run/wanted.sock"
    with pytest.raises(cli.SocketError, match="no server answering"):
        cli.discover_socket("/run/missing.sock")


def test_nothing_answering_names_the_way_out(monkeypatch, tmp_path, reachable):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("HMUX_SOCKET", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(cli.SocketError, match="--socket"):
        cli.discover_socket()


def test_provider_mode_opens_no_local_listener(monkeypatch, reachable):
    from webmon import provider
    from webmon.model import Model

    reachable.add("/tmp/provider-fixture")
    active = []

    class FakeMonitor:
        def __init__(self, *args, **kwargs):
            self.model = Model()
            self.model.stale = False

        async def start(self):
            active.append("monitor-start")

        async def stop(self):
            active.append("monitor-stop")

    class FakeProvider:
        def __init__(self, monitor, config):
            assert config.provider_id

        async def run(self):
            active.append("provider")

    def unexpected_listener(*args):
        raise AssertionError("provider mode opened a local HTTP listener")

    monkeypatch.setattr(cli, "Monitor", FakeMonitor)
    monkeypatch.setattr(cli, "HttpServer", unexpected_listener)
    monkeypatch.setattr(provider, "Provider", FakeProvider)
    options = cli.parse_args(["--socket", "/tmp/provider-fixture", "--provider", "http://127.0.0.1:8000/gql", "--signing-key", "fixture.pub"])
    asyncio.run(cli.serve(options))
    assert active == ["monitor-start", "provider", "monitor-stop"]


def test_provider_default_key_and_overrides(monkeypatch):
    monkeypatch.delenv("WEBMON_SIGNING_KEY", raising=False)
    assert cli.parse_args(["--provider", "https://example.test/gql"]).signing_key == "~/.config/webmon/signing-key.jwk"
    monkeypatch.setenv("WEBMON_SIGNING_KEY", "custom.json")
    assert cli.parse_args([]).signing_key == "custom.json"
    assert cli.parse_args(["--signing-key", "agent.pub"]).signing_key == "agent.pub"


def test_stream_columns_default_opt_out_and_validation():
    assert cli.parse_args([]).stream_columns == 100
    assert cli.parse_args(["--stream-columns", "0"]).stream_columns == 0
    assert cli.parse_args(["--stream-columns", "80"]).stream_columns == 80
    with pytest.raises(SystemExit):
        cli.parse_args(["--stream-columns", "-1"])
