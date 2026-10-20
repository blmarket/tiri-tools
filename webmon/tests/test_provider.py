import base64
import json
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from conftest import run_async
from webmon.monitor import Monitor
from webmon.provider import (
    MAX_QUEUE_BYTES, MAX_QUEUE_MESSAGES, ProviderConfig,
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
