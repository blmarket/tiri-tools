import asyncio
import base64
import contextlib
import json
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from websockets.asyncio.server import serve

from conftest import run_async
from test_monitor import started, until
from webmon.monitor import Monitor
from webmon.provider import (
    MAX_QUEUE_BYTES, MAX_QUEUE_MESSAGES, Provider, ProviderConfig,
    ProviderConnection, SIGNING_NAMESPACE, ProviderLogin, TerminalStream, load_jwk,
)


def config(endpoint="https://example.test/gql", key="fixture.pub"):
    return ProviderConfig(endpoint, key, "fixture-provider", "Fixture provider")


@pytest.mark.parametrize("endpoint", ["http://example.test/gql", "https://user:secret@example.test/gql", "https://example.test/gql?token=x", "file:///tmp/gql"])
def test_endpoint_requires_verified_tls_or_explicit_loopback(endpoint):
    with pytest.raises(ValueError):
        config(endpoint)


@pytest.mark.parametrize("kind", ["ssh", "jwk"])
def test_login_signs_exact_challenge_and_keeps_cookie_scope(tmp_path, kind):
    key = tmp_path / "fixture"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    allowed = tmp_path / "allowed"
    allowed.write_text("fixture " + key.with_suffix(".pub").read_text())
    challenge = "fixture challenge without a trailing newline 雪"
    calls = []
    if kind == "jwk":
        private = ec.generate_private_key(ec.SECP256R1())
        key = tmp_path / "fixture.jwk"
        key.write_text(json.dumps(private_jwk(private)))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request["query"])
            cookie = None
            if request["query"] == "mutation { challenge }":
                response = {"data": {"challenge": challenge}}
            else:
                if kind == "ssh":
                    signature = tmp_path / "signature"
                    signature.write_text(request["variables"]["sign"])
                    result = subprocess.run(
                        ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "fixture", "-n", SIGNING_NAMESPACE, "-s", str(signature)],
                        input=challenge.encode(), capture_output=True,
                    )
                    assert result.returncode == 0
                else:
                    signature = bytes.fromhex(request["variables"]["sign"])
                    assert len(signature) == 64
                    der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
                    private.public_key().verify(der, challenge.encode(), ec.ECDSA(hashes.SHA256()))
                mutation = "loginSsh" if kind == "ssh" else "loginJwk"
                assert mutation in request["query"]
                assert request["variables"]["challenge"] == challenge
                response = {"data": {mutation: True}}
                cookie = "id=fixture-session; Path=/gql; HttpOnly; SameSite=Lax"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        login = ProviderLogin(config(f"http://127.0.0.1:{http.server_port}/gql", str(key)))
        assert run_async(login.authenticate()) == "id=fixture-session"
        assert run_async(login.authenticate()) == "id=fixture-session"
        assert len(calls) == 4
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_outbound_queue_is_bounded_and_bytes_use_stream_ids():
    connection = ProviderConnection(Monitor("tmux", "/tmp/fixture"), None)
    viewer = TerminalStream(connection, 0x12345678, "%1")
    viewer.deliver_screen("%1", b"too early")
    assert not connection.queue
    viewer.deliver_reset("%1", {"t": "screen", "pane": "%1", "data": "\u00ff\u0000"})
    viewer.deliver_screen("%1", b"\xff\x00\x1b[31m\xe9")
    binary, _ = connection.queue[1]
    assert binary == struct.pack("!I", 0x12345678) + b"\xff\x00\x1b[31m\xe9"
    for _ in range(1000):
        viewer.deliver_screen("%1", b"x" * 65536)
    assert connection.overloaded.is_set()
    assert connection.queued_bytes <= MAX_QUEUE_BYTES
    assert len(connection.queue) <= MAX_QUEUE_MESSAGES


def test_provider_registers_streams_metadata_and_reconnects_with_fresh_state(server):
    async def scenario():
        monitor = await started(server, poll_interval=600)
        server.command("new-window", "-t", "work", "-n", "stream", "sh -c 'while :; do printf \"RELAY雪\\n\"; sleep 0.1; done'")
        assert await until(lambda: any(p["window_name"] == "stream" for p in monitor.model.panes.values()))
        pane = next(p for p, values in monitor.model.panes.items() if values["window_name"] == "stream")
        complete = asyncio.Event()
        failures = []
        attempts = []
        auth_calls = []

        async def handle(ws):
            try:
                assert ws.request.headers["Cookie"] == "id=fixture-session"
                hello = json.loads(await ws.recv())
                assert hello["t"] == "hello" and hello["version"] == 1
                assert hello["id"] == "fixture-provider" and pane in hello["model"]["panes"]
                attempts.append(hello)
                await ws.send(json.dumps({"t": "ready", "version": 1}))
                await ws.send(json.dumps({"t": "metadata-subscribe", "id": "meta", "template": "#{pane_id}:#{@relay}"}))
                await ws.send(json.dumps({"t": "view", "stream": 99, "pane": pane}))
                seen_screen = seen_output = seen_metadata = False
                while not (seen_screen and seen_output and seen_metadata):
                    raw = await asyncio.wait_for(ws.recv(), 10)
                    if isinstance(raw, bytes):
                        assert seen_screen
                        assert struct.unpack("!I", raw[:4])[0] == 99
                        seen_output = seen_output or b"RELAY" in raw[4:]
                    else:
                        message = json.loads(raw)
                        if message["t"] == "screen":
                            assert message["stream"] == 99
                            assert message["cols"] == 80 and message["rows"] == 24
                            assert message["data"].encode("latin-1").startswith(b"\x1bc")
                            seen_screen = True
                        elif message["t"] == "metadata":
                            assert message["error"] is None
                            expected = pane + (":fresh" if len(attempts) > 1 else ":")
                            seen_metadata = message["values"][pane] == expected
                if len(attempts) == 1:
                    server.command("set-option", "-p", "-t", pane, "@relay", "fresh")
                    await ws.close(code=1012)
                else:
                    await ws.send(json.dumps({"t": "unview", "stream": 99, "pane": pane}))
                    await ws.send(json.dumps({"t": "metadata-unsubscribe", "id": "meta"}))
                    assert await until(lambda: not monitor.pane_watchers and not monitor.formats.feeds)
                    assert not monitor._viewers
                    complete.set()
                    await ws.wait_closed()
            except Exception as exc:
                failures.append(exc)
                complete.set()

        async def authenticate():
            auth_calls.append(True)
            return "id=fixture-session"

        try:
            async with serve(handle, "127.0.0.1", 0, compression=None) as relay:
                port = relay.sockets[0].getsockname()[1]
                provider = Provider(monitor, config(f"http://127.0.0.1:{port}/gql"))
                provider.login.authenticate = authenticate
                task = asyncio.create_task(provider.run())
                try:
                    await asyncio.wait_for(complete.wait(), 30)
                    if failures:
                        raise failures[0]
                    assert len(attempts) == len(auth_calls) == 2
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            assert not monitor.formats.feeds and not monitor.pane_watchers
        finally:
            await monitor.stop()

    run_async(scenario())


def private_jwk(private):
    numbers = private.private_numbers()
    def encode(value):
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()
    return {"kty": "EC", "crv": "P-256", "x": encode(numbers.public_numbers.x),
            "y": encode(numbers.public_numbers.y), "d": encode(numbers.private_value),
            "key_ops": ["sign"], "ext": True}


@pytest.mark.parametrize("change", [
    {"d": None}, {"d": "!" * 43}, {"d": "A" * 43}, {"crv": "P-384"},
    {"kty": "RSA"}, {"alg": "ES384"}, {"use": "enc"}, {"key_ops": ["verify"]},
    {"key_ops": "sign"}, {"x": "A" * 43},
])
def test_jwk_rejects_invalid_or_incompatible_keys(change):
    jwk = private_jwk(ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(ValueError, match="valid P-256 private JWK"):
        load_jwk(json.dumps({**jwk, **change}).encode())


def test_invalid_jwk_fails_before_network(tmp_path):
    key = tmp_path / "bad.jwk"
    key.write_text("[]")
    with pytest.raises(ValueError, match="valid P-256 private JWK"):
        run_async(ProviderLogin(config(key=str(key))).authenticate())


def test_provider_resize_is_scoped_to_active_stream(server):
    async def scenario():
        monitor = await started(server, stream_columns=100)
        pane = server.pane_ids()[0]
        connection = ProviderConnection(monitor, None)
        try:
            await connection.handle({"t": "view", "pane": pane, "stream": 7})
            await connection.handle({"t": "resize", "pane": pane, "stream": 8, "cols": 100, "rows": 40})
            assert not monitor.stream_sizes.clients
            await connection.handle({"t": "resize", "pane": "%999", "stream": 7, "cols": 100, "rows": 40})
            assert not monitor.stream_sizes.clients
            await connection.handle({"t": "resize", "pane": pane, "stream": 7, "cols": 100, "rows": 40})
            assert await until(lambda: server.command("display-message", "-p", "-t", pane, "#{pane_height}").strip() == "40")
            await connection.handle({"t": "unview", "pane": pane, "stream": 7})
            assert not monitor.stream_sizes.clients
        finally:
            await monitor.stop()
    run_async(scenario())


def test_browser_envelopes_cannot_dispatch_provider_commands_or_other_streams(server):
    async def scenario():
        monitor = await started(server, stream_columns=100)
        pane = server.pane_ids()[0]
        connection = ProviderConnection(monitor, None)
        try:
            for stream in (7, 8):
                await connection.handle({"t": "view", "pane": pane, "stream": stream})
            connection.subscriptions["shared"] = object()
            for message in [
                {"t": "metadata-unsubscribe", "id": "shared"},
                {"t": "view", "stream": 8, "pane": "%999"},
                {"t": "terminal-message", "stream": 8, "message": {"t": "unview"}},
                {"t": "future-operation"},
                {"t": "resize", "cols": 100, "rows": False},
                None,
            ]:
                await connection.handle({"t": "terminal-message", "stream": 7, "pane": pane, "message": message})
                reply = json.loads(connection.queue[-1][0])
                assert reply["t"] == "terminal-message" and reply["stream"] == 7
                assert reply["message"]["t"] == "error"
                assert set(connection.streams) == {7, 8}
                assert "shared" in connection.subscriptions
            await connection.handle({"t": "terminal-message", "stream": 7, "pane": pane, "message": {"t": "resize", "stream": 8, "pane": "%999", "cols": 100, "rows": 45}})
            assert (connection.streams[7], pane) in monitor.stream_sizes.clients
            assert (connection.streams[8], pane) not in monitor.stream_sizes.clients
            await connection.handle({"t": "terminal-message", "stream": 8, "pane": pane, "message": {"t": "ping"}})
            assert json.loads(connection.queue[-1][0])["message"] == {"t": "pong"}
            await connection.handle({"t": "terminal-message", "stream": 7, "pane": pane, "message": {"t": "unview", "stream": 8}})
            assert set(connection.streams) == {8}
            assert not monitor.stream_sizes.clients
        finally:
            await monitor.stop()
    run_async(scenario())
