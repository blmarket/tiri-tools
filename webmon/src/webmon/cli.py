"""Command line entry point: pick a socket, serve the viewer."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import stat
import socket
import subprocess
from pathlib import Path

from .monitor import Monitor
from .server import HttpServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


class SocketError(RuntimeError):
    pass


def default_socket() -> str:
    """The socket pathname a plain tmux client would use."""
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp")
    return str(base / f"tmux-{os.geteuid()}" / "default")


def _from_tmux_env(value: str) -> str | None:
    """Extract the socket path from tmux's `socket,pid,index` value."""
    parts = value.rsplit(",", 2)
    return parts[0] if len(parts) == 3 and parts[0] else None


def _reachable(path: str, tmux: str) -> bool:
    try:
        result = subprocess.run(
            [tmux, "-S", path, "list-sessions"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def discover_socket(requested: str | None = None, *, tmux: str = "tmux") -> str:
    """Select a reachable socket, preferring the one a plain `tmux` would use.

    The default socket comes first so that starting webmon with no arguments
    watches the server the user's own `tmux` talks to. `HMUX_SOCKET` and the
    `TMUX` of the pane webmon happens to be started from are fallbacks behind
    it, then any other socket sitting in the same directory.
    """
    if requested:
        if not _reachable(requested, tmux):
            raise SocketError(f"no server answering on {requested}")
        return requested
    default = Path(default_socket())
    candidates: list[str] = [str(default)]
    for value in (
        os.environ.get("HMUX_SOCKET"),
        _from_tmux_env(os.environ.get("TMUX", "")),
    ):
        if value and value not in candidates:
            candidates.append(value)
    try:
        siblings = sorted(default.parent.iterdir())
    except OSError:
        siblings = []
    for path in siblings:
        try:
            if stat.S_ISSOCK(path.stat().st_mode) and str(path) not in candidates:
                candidates.append(str(path))
        except OSError:
            continue
    for path in candidates:
        if _reachable(path, tmux):
            return path
    raise SocketError(
        "no reachable tmux/hmux socket found; pass --socket PATH or set HMUX_SOCKET"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="webmon",
        description="Watch an hmux/tmux server's panes from a browser.",
    )
    parser.add_argument(
        "--socket",
        "-S",
        help=f"server socket path (default: {default_socket()})",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tmux", default=os.environ.get("WEBMON_TMUX", "tmux"))
    parser.add_argument(
        "--poll-interval", type=float, default=30.0, help="seconds between full polls"
    )
    parser.add_argument("--stream-columns", type=int, default=100, help="requested browser client columns (default: 100; 0 disables browser sizing)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--provider", metavar="URL", default=os.environ.get("WEBMON_PROVIDER"), help="GraphQL endpoint for outbound provider mode")
    parser.add_argument("--signing-key", default=os.environ.get("WEBMON_SIGNING_KEY", "~/.config/webmon/signing-key.jwk"), help="registered P-256 private JWK, SSH private key, or agent public key (default: ~/.config/webmon/signing-key.jwk)")
    parser.add_argument("--provider-id", default=os.environ.get("WEBMON_PROVIDER_ID"))
    parser.add_argument("--provider-name", default=os.environ.get("WEBMON_PROVIDER_NAME", socket.gethostname()))
    parser.add_argument("--ssh-keygen", default=os.environ.get("WEBMON_SSH_KEYGEN", "ssh-keygen"))
    parser.add_argument("--serve-local", action="store_true", help="also serve the standalone UI in provider mode")
    options = parser.parse_args(argv)
    if options.stream_columns != 0 and not 1 <= options.stream_columns <= 10000:
        parser.error("--stream-columns must be 0 or between 1 and 10000")
    if options.provider and not options.signing_key:
        parser.error("--provider requires --signing-key (or WEBMON_SIGNING_KEY)")
    return options


async def serve(options: argparse.Namespace) -> None:
    socket_path = discover_socket(options.socket, tmux=options.tmux)
    monitor = Monitor(
        options.tmux, socket_path, poll_interval=options.poll_interval,
        stream_columns=options.stream_columns or None
    )
    server = None
    tasks = []
    await monitor.start()
    try:
        if not options.provider or options.serve_local:
            server = HttpServer(monitor, options.host, options.port)
            await server.start()
            tasks.append(asyncio.create_task(server.serve_forever()))
            logging.getLogger("webmon").info(
                "webmon serving %s on http://%s:%d", socket_path, options.host, options.port
            )
        if options.provider:
            from .provider import Provider, ProviderConfig, default_provider_id

            config = ProviderConfig(
                options.provider, options.signing_key,
                options.provider_id or default_provider_id(socket_path),
                options.provider_name, options.ssh_keygen,
            )
            while monitor.model.stale and not monitor.model.error:
                await asyncio.sleep(0.05)
            tasks.append(asyncio.create_task(Provider(monitor, config).run()))
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.stop()
        await monitor.stop()


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if options.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve(options))
    except KeyboardInterrupt:
        return 0
    except (SocketError, ValueError) as exc:
        logging.getLogger("webmon").error("%s", exc)
        return 1
    return 0
