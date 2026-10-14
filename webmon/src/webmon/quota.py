from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from agentmon.quota import DEFAULT_TTL_SECONDS, QuotaService


AGENT_SERVICES = {"codex": "codex", "claude": "claude", "agy": "antigravity"}


class QuotaSubscriptions:
    def __init__(self, service: QuotaService | None = None, interval: float = DEFAULT_TTL_SECONDS) -> None:
        self.service = service or QuotaService()
        self.interval = interval
        self.subscribers: dict[object, tuple[str, Callable[[dict], None]]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.latest: dict[str, dict] = {}
        self.retired: set[asyncio.Task] = set()

    def subscribe(self, token: object, agent: str, callback: Callable[[dict], None]) -> None:
        service = AGENT_SERVICES.get(agent, "")
        if token in self.subscribers and self.subscribers[token][0] == service:
            return
        self.unsubscribe(token)
        self.subscribers[token] = (service, callback)
        callback(self.latest.get(service, {
            "t": "quota", "service": service, "windows": [], "fetched_at": None,
            "status": "loading" if service else "unsupported",
        }))
        if service and service not in self.tasks:
            self.tasks[service] = asyncio.create_task(self._poll(service))

    def unsubscribe(self, token: object) -> None:
        previous = self.subscribers.pop(token, None)
        if previous is None:
            return
        service = previous[0]
        if any(name == service for name, _ in self.subscribers.values()):
            return
        task = self.tasks.pop(service, None)
        if task is not None:
            task.cancel()
            self.retired.add(task)
            task.add_done_callback(self.retired.discard)
        self.latest.pop(service, None)

    async def _poll(self, service: str) -> None:
        while True:
            delay = self.interval
            try:
                report = await asyncio.to_thread(self.service.report, service=service)
                if report.errors or not report.quotas:
                    raise ValueError("Quota unavailable")
                delay = max(0.01, min(self.interval, report.fetched_at.timestamp() + self.interval - time.time()))
                payload = {
                    "t": "quota", "service": service, "status": "ready",
                    "fetched_at": report.fetched_at.timestamp(),
                    "windows": [{
                        "label": window.label,
                        "remaining_percent": min(100.0, window.remaining_percent),
                        "resets_at": window.resets_at.timestamp() if window.resets_at else None,
                        "window_seconds": window.window_seconds,
                    } for window in report.quotas],
                }
            except Exception:
                previous = self.latest.get(service, {})
                payload = {
                    "t": "quota", "service": service,
                    "status": "stale" if previous.get("windows") else "unavailable",
                    "windows": previous.get("windows", []),
                    "fetched_at": previous.get("fetched_at"),
                }
            self.latest[service] = payload
            for name, callback in list(self.subscribers.values()):
                if name == service:
                    callback(payload)
            await asyncio.sleep(delay)

    async def close(self) -> None:
        for token in list(self.subscribers):
            self.unsubscribe(token)
        await asyncio.gather(*self.retired, return_exceptions=True)
