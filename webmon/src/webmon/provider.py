from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import http.cookiejar
import ipaddress
import json
import logging
import os
import random
import re
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from websockets.asyncio.client import connect

from .monitor import Monitor
from .quota import QuotaSubscriptions
from .sizing import dimensions

logger = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 1 << 20
MAX_QUEUE_BYTES = 2 << 20
MAX_QUEUE_MESSAGES = 512
MAX_STREAMS = 128
MAX_SUBSCRIPTIONS = 128
SIGNING_NAMESPACE = "login@blmarket.net"


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str
    signing_key: str
    provider_id: str
    name: str
    ssh_keygen: str = "ssh-keygen"

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.endpoint)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("provider endpoint must be an HTTPS GraphQL URL")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("provider endpoint cannot contain credentials, query, or fragment")
        if url.scheme == "http":
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = url.hostname == "localhost"
            if not local:
                raise ValueError("HTTP provider endpoints are allowed only on loopback")
        if not self.signing_key:
            raise ValueError("provider mode requires --signing-key")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.provider_id):
            raise ValueError("provider ID must contain 1 to 128 ASCII letters, digits, or -_.:")
        if not self.name or len(self.name.encode()) > 256:
            raise ValueError("provider name must contain 1 to 256 UTF-8 bytes")

    @property
    def websocket_url(self) -> str:
        url = urllib.parse.urlsplit(self.endpoint)
        return urllib.parse.urlunsplit((
            "wss" if url.scheme == "https" else "ws", url.netloc,
            url.path.rstrip("/") + "/webmon/provider", "", "",
        ))


def default_provider_id(socket_path: str) -> str:
    host = re.sub(r"[^A-Za-z0-9_.:-]", "-", socket.gethostname())
    digest = hashlib.sha256(os.path.realpath(socket_path).encode()).hexdigest()[:12]
    return f"{host[:100]}-{digest}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("authentication endpoint must not redirect")


class ProviderLogin:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect(),
        )

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with self.opener.open(request, timeout=15) as response:
            payload = response.read(MAX_MESSAGE_BYTES + 1)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("GraphQL authentication response is too large")
        result = json.loads(payload)
        if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
            raise ValueError("GraphQL authentication failed")
        return result["data"]

    async def authenticate(self) -> str:
        self.cookies.clear()
        key_path = Path(self.config.signing_key).expanduser()
        key_data = await asyncio.to_thread(key_path.read_bytes)
        jwk = None
        if key_data.lstrip().startswith(b"{") or key_path.suffix.lower() in (".jwk", ".json"):
            jwk = load_jwk(key_data)
        mutation = "loginJwk" if jwk is not None else "loginSsh"
        result = await asyncio.to_thread(self._graphql, "mutation { challenge }")
        challenge = result.get("challenge")
        if not isinstance(challenge, str) or not challenge or len(challenge) > 4096:
            raise ValueError("server returned an invalid login challenge")
        if jwk is None:
            signature = await self.sign(challenge)
        else:
            der = jwk.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            signature = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()
        result = await asyncio.to_thread(
            self._graphql,
            f"mutation Login($challenge: String!, $sign: String!) {{ {mutation}(challenge: $challenge, sign: $sign) }}",
            {"challenge": challenge, "sign": signature},
        )
        if result.get(mutation) is not True:
            raise ValueError("signing key was not accepted by the login service")
        url = self.config.websocket_url.replace("wss:", "https:", 1).replace("ws:", "http:", 1)
        request = urllib.request.Request(url)
        self.cookies.add_cookie_header(request)
        cookie = request.get_header("Cookie")
        if not cookie:
            raise ValueError("login did not establish a session cookie for the provider endpoint")
        return cookie

    async def sign(self, challenge: str) -> str:
        process = await asyncio.create_subprocess_exec(
            self.config.ssh_keygen, "-Y", "sign", "-f", os.path.expanduser(self.config.signing_key),
            "-n", SIGNING_NAMESPACE,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"},
            start_new_session=True,
        )
        try:
            signature, _ = await asyncio.wait_for(process.communicate(challenge.encode("utf-8")), 15)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode or not signature.startswith(b"-----BEGIN SSH SIGNATURE-----"):
            raise ValueError("ssh-keygen could not sign; load encrypted keys into ssh-agent and use the public key path")
        return signature.decode("ascii")


def load_jwk(data: bytes) -> ec.EllipticCurvePrivateKey:
    try:
        jwk = json.loads(data)
        if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise ValueError
        if jwk.get("alg", "ES256") != "ES256" or jwk.get("use", "sig") != "sig":
            raise ValueError
        if "key_ops" in jwk and (not isinstance(jwk["key_ops"], list) or "sign" not in jwk["key_ops"]):
            raise ValueError
        values = {}
        for field in ("x", "y", "d"):
            encoded = jwk[field]
            if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded):
                raise ValueError
            raw = base64.urlsafe_b64decode(encoded + "=")
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != encoded:
                raise ValueError
            values[field] = int.from_bytes(raw, "big")
        public = ec.EllipticCurvePublicNumbers(values["x"], values["y"], ec.SECP256R1())
        return ec.EllipticCurvePrivateNumbers(values["d"], public).private_key()
    except (ValueError, KeyError, TypeError):
        raise ValueError("signing key must be a valid P-256 private JWK permitting ES256 signing") from None


class TerminalStream:
    def __init__(self, connection: ProviderConnection, stream: int, pane: str) -> None:
        self.connection = connection
        self.stream = stream
        self.pane = pane
        self.ready = False

    def deliver_model(self, message: dict) -> None:
        pass

    def deliver_screen(self, pane: str, data: bytes) -> None:
        if self.ready and pane == self.pane:
            for offset in range(0, len(data), 65536):
                self.connection.enqueue(struct.pack("!I", self.stream) + data[offset:offset + 65536])

    def deliver_reset(self, pane: str, payload: dict) -> None:
        if pane == self.pane:
            self.ready = payload.get("t") == "screen"
            self.connection.enqueue({**payload, "stream": self.stream})


class ProviderConnection:
    def __init__(self, monitor: Monitor, websocket) -> None:
        self.monitor = monitor
        self.websocket = websocket
        self.streams: dict[int, TerminalStream] = {}
        self.subscriptions: dict[str, object] = {}
        self.queue: deque[tuple[str | bytes, int]] = deque()
        self.queued_bytes = 0
        self.wake = asyncio.Event()
        self.overloaded = asyncio.Event()
        self.closed = False
        self.quotas = QuotaSubscriptions()

    def subscribe_quota(self, viewer: TerminalStream) -> None:
        pane = self.monitor.model.pane(viewer.pane)
        if pane is None:
            self.quotas.unsubscribe(viewer)
            return
        self.quotas.subscribe(viewer, str(pane.get("pane_agent", "")), lambda payload: self.enqueue({
            "t": "terminal-message", "stream": viewer.stream, "pane": viewer.pane, "message": payload,
        }))

    def enqueue(self, message: dict | bytes) -> None:
        if self.closed or self.overloaded.is_set():
            return
        payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")) if isinstance(message, dict) else message
        size = len(payload)
        if size > MAX_MESSAGE_BYTES or self.queued_bytes + size > MAX_QUEUE_BYTES or len(self.queue) >= MAX_QUEUE_MESSAGES:
            self.overloaded.set()
            return
        self.queue.append((payload, size))
        self.queued_bytes += size
        self.wake.set()

    def deliver_model(self, message: dict) -> None:
        self.enqueue(message)
        for viewer in list(self.streams.values()):
            self.subscribe_quota(viewer)

    def deliver_screen(self, pane: str, data: bytes) -> None:
        pass

    def deliver_reset(self, pane: str, payload: dict) -> None:
        pass

    async def _write(self) -> None:
        while True:
            await self.wake.wait()
            while self.queue:
                message, size = self.queue.popleft()
                self.queued_bytes -= size
                await asyncio.wait_for(self.websocket.send(message), 10)
            self.wake.clear()

    async def _read(self) -> None:
        async for raw in self.websocket:
            if not isinstance(raw, str):
                raise ValueError("provider control messages must be JSON text")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("invalid provider control message")
            await self.handle(message)

    async def handle(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "ping":
            self.enqueue({"t": "pong"})
        elif kind in ("ready", "pong"):
            return
        elif kind == "terminal-message":
            await self.handle_terminal(message)
        elif kind == "metadata-subscribe":
            identity = message.get("id")
            if not isinstance(identity, str) or len(identity) > 256:
                raise ValueError("invalid metadata subscription ID")
            old = self.subscriptions.pop(identity, None)
            if old is not None:
                await self.monitor.formats.unsubscribe(old)
            token = object()
            try:
                if len(self.subscriptions) >= MAX_SUBSCRIPTIONS:
                    raise ValueError("too many metadata subscriptions")
                await self.monitor.formats.subscribe(
                    token, message.get("template"),
                    lambda values, error: self.enqueue({"t": "metadata", "id": identity, "values": values, "error": error}),
                )
                self.subscriptions[identity] = token
            except ValueError as exc:
                self.enqueue({"t": "metadata", "id": identity, "values": {}, "error": str(exc)})
        elif kind == "metadata-unsubscribe":
            token = self.subscriptions.pop(message.get("id"), None)
            if token is not None:
                await self.monitor.formats.unsubscribe(token)
        elif kind in ("view", "unview", "repaint", "resize"):
            stream = message.get("stream")
            pane = message.get("pane")
            if type(stream) is not int or not 0 <= stream <= 0xFFFFFFFF:
                raise ValueError("invalid terminal stream ID")
            if not isinstance(pane, str) or not pane.startswith("%") or not pane[1:].isdigit():
                raise ValueError("invalid pane ID")
            viewer = self.streams.get(stream)
            if kind == "view":
                if viewer is not None:
                    self.quotas.unsubscribe(viewer)
                    await self.monitor.unwatch_pane(viewer.pane, viewer)
                if len(self.streams) >= MAX_STREAMS and viewer is None:
                    self.enqueue({"t": "screen-gone", "pane": pane, "stream": stream})
                    return
                viewer = TerminalStream(self, stream, pane)
                self.streams[stream] = viewer
                await self.monitor.watch_pane(pane, viewer, message)
                self.subscribe_quota(viewer)
            elif viewer is not None and viewer.pane == pane:
                if kind == "unview":
                    self.streams.pop(stream, None)
                    self.quotas.unsubscribe(viewer)
                    viewer.ready = False
                    await self.monitor.unwatch_pane(pane, viewer)
                elif kind == "resize":
                    await self.monitor.resize_pane(pane, viewer, message)
                else:
                    await self.monitor.repaint(pane, viewer)
        else:
            raise ValueError("unsupported provider request")

    async def handle_terminal(self, envelope: dict) -> None:
        """Interpret browser operations within the relay-assigned stream."""
        stream, pane = envelope.get("stream"), envelope.get("pane")
        if type(stream) is not int:
            return
        viewer = self.streams.get(stream)
        if viewer is None or viewer.pane != pane:
            return
        message = envelope.get("message")
        kind = message.get("t") if isinstance(message, dict) else None
        if kind == "ping":
            reply = {"t": "pong"}
        elif kind == "pong":
            return
        elif kind == "resize" and dimensions(message) is not None:
            await self.monitor.resize_pane(pane, viewer, message)
            return
        elif kind == "repaint":
            await self.monitor.repaint(pane, viewer)
            return
        elif kind == "unview":
            self.streams.pop(stream, None)
            self.quotas.unsubscribe(viewer)
            viewer.ready = False
            await self.monitor.unwatch_pane(pane, viewer)
            return
        else:
            reply = {"t": "error", "error": "Invalid or unsupported terminal message"}
        self.enqueue({"t": "terminal-message", "stream": stream, "pane": pane, "message": reply})

    async def run(self) -> None:
        self.monitor.add_watcher(self)
        tasks = [asyncio.create_task(self._write()), asyncio.create_task(self._read()), asyncio.create_task(self.overloaded.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if self.overloaded.is_set():
                raise ConnectionError("provider uplink fell behind; reconnecting for fresh captures")
        finally:
            self.closed = True
            await self.quotas.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.monitor.remove_watcher(self)
            for viewer in list(self.streams.values()):
                viewer.ready = False
                await self.monitor.unwatch_pane(viewer.pane, viewer)
            for token in list(self.subscriptions.values()):
                await self.monitor.formats.unsubscribe(token)
            self.streams.clear()
            self.subscriptions.clear()
            self.queue.clear()
            self.queued_bytes = 0


class Provider:
    def __init__(self, monitor: Monitor, config: ProviderConfig) -> None:
        self.monitor = monitor
        self.config = config
        self.login = ProviderLogin(config)
        self.connected = asyncio.Event()

    async def run(self) -> None:
        backoff = 0.5
        wire_logger = logging.getLogger("webmon.provider.transport")
        wire_logger.setLevel(logging.WARNING)
        while True:
            started = asyncio.get_running_loop().time()
            try:
                cookie = await self.login.authenticate()
                async with connect(
                    self.config.websocket_url, additional_headers={"Cookie": cookie},
                    compression=None, proxy=None, max_size=MAX_MESSAGE_BYTES, max_queue=16,
                    write_limit=32768, open_timeout=15, close_timeout=3,
                    ping_interval=20, ping_timeout=20, logger=wire_logger,
                ) as websocket:
                    hello = json.dumps({
                        "t": "hello", "version": 1, "id": self.config.provider_id,
                        "name": self.config.name, "model": self.monitor.model.snapshot(),
                    })
                    if len(hello) > MAX_MESSAGE_BYTES:
                        raise ValueError("provider model exceeds the 1 MiB message limit")
                    await websocket.send(hello)
                    ready = json.loads(await asyncio.wait_for(websocket.recv(), 10))
                    if ready.get("t") != "ready" or ready.get("version") != 1:
                        raise ValueError("server did not accept provider protocol version 1")
                    self.connected.set()
                    logger.info("provider %s connected", self.config.provider_id)
                    await ProviderConnection(self.monitor, websocket).run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("provider connection ended (%s); retrying", type(exc).__name__)
                if isinstance(exc, ValueError):
                    logger.warning("provider configuration or protocol error: %s", exc)
            finally:
                self.connected.clear()
            if asyncio.get_running_loop().time() - started >= 30:
                backoff = 0.5
            await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
            backoff = min(backoff * 2, 30.0)
