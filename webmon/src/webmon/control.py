"""An asyncio control-mode client: one attached tmux client, one process.

The reader task never stops draining the stream. That is not tidiness: the
server pushes pane output at whatever rate the pane produces it, and a client
that stops reading is a client the server has to hold output for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable

from .protocol import (
    OUTPUT_PREFIXES,
    Begin,
    End,
    Notification,
    Reply,
    SubscriptionChanged,
    parse_line,
    parse_output,
)

logger = logging.getLogger(__name__)

# Big enough for a capture-pane reply line of a very wide pane and for the
# 4x expansion escaping can apply to a single %output chunk.
READ_LIMIT = 1 << 22


class ControlClient:
    """A `tmux -C attach-session` client for one session."""

    def __init__(
        self,
        tmux: str,
        socket: str,
        session: str,
        *,
        flags: str = "no-output,ignore-size",
        on_output: Callable[[str, bytes], None] | None = None,
        on_subscription: Callable[[SubscriptionChanged], None] | None = None,
        on_notification: Callable[[str, str], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.tmux = tmux
        self.socket = socket
        self.session = session
        self.flags = flags
        self.on_output = on_output
        self.on_subscription = on_subscription
        self.on_notification = on_notification
        self.on_close = on_close
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: deque[asyncio.Future[Reply]] = deque()
        self._reply_callbacks: dict[asyncio.Future[Reply], Callable[[Reply], None]] = {}
        self._frame: Reply | None = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()

    @property
    def argv(self) -> list[str]:
        return [
            self.tmux,
            "-S",
            self.socket,
            "-C",
            "attach-session",
            "-f",
            self.flags,
            "-t",
            self.session,
        ]

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=READ_LIMIT,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"control-{self.session}"
        )
        # tmux answers the attach itself with an empty reply frame. Consuming
        # it before any command goes out keeps replies aligned with senders.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=5)

    async def stop(self) -> None:
        """Detach this client, dropping our end of its pipes first.

        The server writes control output straight into the fd the client
        passed it, which is the write end of our stdout pipe. Between the
        moment this stops reading and the moment that pipe is closed, a full
        pipe blocks the server's write — and a blocked server answers nobody,
        not just this client. So the signal and the close go out together,
        before anything is awaited.
        """
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        self._close_pipes(process)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        self._fail_pending(ConnectionError("control client stopped"))
        self._closed.set()

    @staticmethod
    def _close_pipes(process: asyncio.subprocess.Process | None) -> None:
        if process is None:
            return
        for stream in (process.stdout, process.stdin):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()

    def send(self, command: str) -> None:
        """Fire a command without waiting for its reply."""
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        # Nobody awaits this one, so swallow the failure a torn-down client
        # would otherwise report as an unretrieved exception.
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._pending.append(future)
        self._write(command)

    async def execute(self, command: str, *, timeout: float = 10.0) -> Reply:
        """Run a command and return its reply frame."""
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._pending.append(future)
        try:
            self._write(command)
        except Exception:
            with contextlib.suppress(ValueError):
                self._pending.remove(future)
            raise
        return await asyncio.wait_for(future, timeout=timeout)

    async def execute_group(
        self, commands: list[str], on_replies: Callable[[list[Reply]], None], *, timeout: float = 10.0
    ) -> list[Reply]:
        """Run adjacent commands, delivering replies at the control-stream boundary."""
        futures = [asyncio.get_running_loop().create_future() for _ in commands]
        replies: list[Reply | None] = [None] * len(commands)

        def resolved(index: int, reply: Reply) -> None:
            replies[index] = reply
            if reply.error:
                for later in range(index + 1, len(futures)):
                    future = futures[later]
                    with contextlib.suppress(ValueError):
                        self._pending.remove(future)
                    self._reply_callbacks.pop(future, None)
                    replies[later] = reply
                    if not future.done():
                        future.set_result(reply)
            if all(item is not None for item in replies):
                on_replies([item for item in replies if item is not None])

        for index, future in enumerate(futures):
            self._pending.append(future)
            self._reply_callbacks[future] = lambda reply, index=index: resolved(index, reply)
        try:
            self._write(" ; ".join(commands))
            return await asyncio.wait_for(asyncio.gather(*futures), timeout=timeout)
        except ConnectionError:
            for future in futures:
                with contextlib.suppress(ValueError):
                    self._pending.remove(future)
                future.cancel()
            raise
        finally:
            for future in futures:
                self._reply_callbacks.pop(future, None)

    def _write(self, command: str) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ConnectionError("control client is not running")
        process.stdin.write((command + "\n").encode("utf-8"))

    def _fail_pending(self, error: Exception) -> None:
        while self._pending:
            future = self._pending.popleft()
            self._reply_callbacks.pop(future, None)
            if not future.done():
                future.set_exception(error)

    def _resolve(self, reply: Reply) -> None:
        while self._pending:
            future = self._pending.popleft()
            callback = self._reply_callbacks.pop(future, None)
            if not future.done():
                if callback is not None:
                    callback(reply)
                future.set_result(reply)
            return

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        stream = process.stdout
        buffer = bytearray()
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while (index := buffer.find(b"\n")) >= 0:
                    line = bytes(buffer[:index])
                    del buffer[: index + 1]
                    self.feed(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("control reader for %s failed", self.session, exc_info=True)
        finally:
            self._closed.set()
            self._fail_pending(ConnectionError("control stream closed"))
            if self.on_close is not None:
                self.on_close()

    def _closes_frame(self, line: bytes) -> bool:
        frame = self._frame
        if frame is None or not line.startswith((b"%end ", b"%error ")):
            return False
        parts = line.rstrip(b"\r").split(b" ")
        return len(parts) == 4 and parts[2].decode("ascii", "replace") == frame.number

    def feed(self, line: bytes) -> None:
        """Dispatch one complete line of the control stream."""
        # Inside a reply frame every line is payload until %end, including one
        # that happens to look like a notification because a pane printed it.
        # Only an %end naming this frame's own command number closes it, so a
        # pane echoing "%end 1 2 3" cannot truncate a capture.
        if self._frame is not None:
            if not self._closes_frame(line):
                self._frame.lines.append(line.rstrip(b"\r"))
                return
            text = line.decode("utf-8", "replace").rstrip("\r")
        elif line.startswith(OUTPUT_PREFIXES):
            # Payload bytes above 0x7f are not escaped, so this never decodes.
            output = parse_output(line)
            if output is not None and self.on_output is not None:
                self.on_output(output.pane, output.data)
            return
        else:
            text = line.decode("utf-8", "replace").rstrip("\r")
        event = parse_line(text)
        if isinstance(event, Begin):
            self._frame = Reply(number=event.number, error=False, lines=[])
            return
        if isinstance(event, End):
            frame = self._frame or Reply(number=event.number, error=event.error)
            self._frame = None
            reply = Reply(number=frame.number, error=event.error, lines=frame.lines)
            if not self._ready.is_set():
                self._ready.set()
                # The attach's own reply belongs to nobody unless a command was
                # already queued behind it.
                if not self._pending:
                    return
            self._resolve(reply)
            return
        if isinstance(event, SubscriptionChanged):
            if self.on_subscription is not None:
                self.on_subscription(event)
            return
        if isinstance(event, Notification):
            if self.on_notification is not None:
                self.on_notification(event.name, event.arguments)
            return
        if self._frame is not None:
            self._frame.lines.append(text)
