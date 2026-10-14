from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest
from textual.widgets import Static

from agentmon.app import AgentmonApp, DemoService, QuotaScreen
from agentmon.model import Repository
from agentmon.quota import QuotaReport, QuotaService, QuotaWindow


class ControlledTimer:
    def __init__(self, deadline: float, callback: Callable[[], None]) -> None:
        self.deadline = deadline
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class ControlledTime:
    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        self.timers: list[ControlledTimer] = []
        self.jitters = iter([0.0, 30.0, 12.0, 7.0, 20.0])
        self.samples: list[float] = []

        def sample(low: float, high: float) -> float:
            assert (low, high) == (0, 30)
            value = next(self.jitters)
            self.samples.append(value)
            return value

        def set_timer(screen, delay, callback):
            timer = ControlledTimer(self.now + delay, callback)
            self.timers.append(timer)
            return timer

        monkeypatch.setattr("agentmon.app.random.uniform", sample)
        monkeypatch.setattr(QuotaScreen, "set_timer", set_timer)

    def __call__(self) -> float:
        return self.now

    @property
    def pending(self) -> list[ControlledTimer]:
        return [timer for timer in self.timers if not timer.stopped]

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for timer in self.pending:
            if timer.deadline <= self.now:
                timer.stop()
                timer.callback()


class ControlledFetch:
    def __init__(self, clock: ControlledTime) -> None:
        self.clock = clock
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.releases = [threading.Event() for _ in range(8)]

    def __call__(self) -> QuotaReport:
        index = self.calls
        self.calls += 1
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            if not self.releases[index].wait(timeout=10):
                raise AssertionError("quota fetch was not released")
            return QuotaReport(
                fetched_at=datetime.fromtimestamp(self.clock(), timezone.utc),
                quotas=(QuotaWindow("codex", "Codex weekly", float(index + 1)),),
            )
        finally:
            self.active -= 1

    def release_all(self) -> None:
        for release in self.releases:
            release.set()


def make_app(tmp_path, monkeypatch):
    clock = ControlledTime(monkeypatch)
    fetch = ControlledFetch(clock)
    service = QuotaService(cache_path=tmp_path / "quota.json", clock=clock)
    monkeypatch.setattr(service, "_fetch_report", fetch)
    root = Path("/demo/project")
    repo = Repository(root=root, common_dir=root / ".git", branch="main")
    app = AgentmonApp(DemoService(repo, socket="/tmp/demo"), quota_service=service)
    return app, clock, fetch


async def until(predicate) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=3)


async def open_quotas(app, pilot) -> QuotaScreen:
    app.action_toggle_quotas()
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, QuotaScreen)
    return screen


def test_refresh_timing_starts_after_completion_and_resamples_jitter(
    tmp_path, monkeypatch
) -> None:
    async def exercise() -> None:
        app, clock, fetch = make_app(tmp_path, monkeypatch)
        try:
            async with app.run_test() as pilot:
                screen = await open_quotas(app, pilot)
                await until(lambda: fetch.calls == 1)
                clock.advance(50)
                assert clock.pending == []
                assert clock.samples == []
                fetch.releases[0].set()
                await until(lambda: len(clock.pending) == 1)
                assert clock.pending[0].deadline == clock.now + 300
                previous = str(screen.query_one("#quota-content", Static).render())

                clock.advance(299)
                assert fetch.calls == 1
                clock.advance(1)
                await until(lambda: fetch.calls == 2)
                assert clock.pending == []
                assert str(screen.query_one("#quota-content", Static).render()) == previous
                clock.advance(40)
                assert clock.samples == [0]
                fetch.releases[1].set()
                await until(lambda: len(clock.pending) == 1)
                assert clock.pending[0].deadline == clock.now + 330

                clock.advance(329)
                assert fetch.calls == 2
                clock.advance(1)
                await until(lambda: fetch.calls == 3)
                fetch.releases[2].set()
                await until(lambda: len(clock.pending) == 1)
                assert clock.pending[0].deadline == clock.now + 312
                assert clock.samples == [0, 30, 12]
                assert fetch.max_active == 1
        finally:
            fetch.release_all()

    asyncio.run(exercise())


def test_refresh_reuses_memory_and_shared_disk_cache(tmp_path, monkeypatch) -> None:
    async def exercise() -> None:
        app, clock, fetch = make_app(tmp_path, monkeypatch)
        fetch.release_all()
        async with app.run_test() as pilot:
            screen = await open_quotas(app, pilot)
            await until(lambda: len(clock.pending) == 1)
            screen.action_close()
            await pilot.pause()
            assert clock.pending == []
            screen = await open_quotas(app, pilot)
            await until(lambda: len(clock.pending) == 1)
            assert fetch.calls == 1

            clock.advance(290)
            other = QuotaService(cache_path=app.quota_service.cache_path, clock=clock)
            monkeypatch.setattr(other, "_fetch_report", fetch)
            other.report(force=True)
            assert fetch.calls == 2
            clock.advance(40)
            await until(lambda: len(clock.pending) == 1)
            assert fetch.calls == 2
            content = str(screen.query_one("#quota-content", Static).render())
            assert "2%" in content

            clock.advance(312)
            await until(lambda: fetch.calls == 3 and len(clock.pending) == 1)
            assert fetch.max_active == 1

    asyncio.run(exercise())


def test_manual_refresh_coalesces_requests_and_replaces_timer(
    tmp_path, monkeypatch
) -> None:
    async def exercise() -> None:
        app, clock, fetch = make_app(tmp_path, monkeypatch)
        try:
            async with app.run_test() as pilot:
                screen = await open_quotas(app, pilot)
                await until(lambda: fetch.calls == 1)
                await pilot.press("r", "r")
                assert fetch.calls == 1
                fetch.releases[0].set()
                await until(lambda: fetch.calls == 2)
                assert clock.pending == []
                assert clock.samples == []
                await pilot.press("r", "r")
                fetch.releases[1].set()
                await until(lambda: len(clock.pending) == 1)
                assert fetch.calls == 2
                old_timer = clock.pending[0]

                clock.advance(100)
                await pilot.press("r")
                await until(lambda: fetch.calls == 3)
                assert old_timer.stopped
                assert clock.pending == []
                clock.advance(200)
                assert fetch.calls == 3
                fetch.releases[2].set()
                await until(lambda: len(clock.pending) == 1)
                assert clock.pending[0].deadline == clock.now + 330

                clock.advance(330)
                await until(lambda: fetch.calls == 4)
                await pilot.press("r", "r")
                assert fetch.calls == 4
                fetch.releases[3].set()
                await until(lambda: fetch.calls == 5)
                assert clock.pending == []
                fetch.releases[4].set()
                await until(lambda: len(clock.pending) == 1)
                assert clock.pending[0].deadline == clock.now + 312
                assert fetch.max_active == 1
        finally:
            fetch.release_all()

    asyncio.run(exercise())


@pytest.mark.parametrize("close", ["escape", "q", "toggle", "unmount"])
def test_close_during_load_discards_pending_refresh_and_reopen_reuses_result(
    tmp_path, monkeypatch, close
) -> None:
    async def exercise() -> None:
        app, clock, fetch = make_app(tmp_path, monkeypatch)
        try:
            async with app.run_test() as pilot:
                old = await open_quotas(app, pilot)
                await until(lambda: fetch.calls == 1)
                await pilot.press("r")
                if close == "unmount":
                    app.pop_screen()
                    await pilot.pause()
                elif close == "toggle":
                    app.action_toggle_quotas()
                    await pilot.pause()
                else:
                    await pilot.press(close)
                clock.advance(1000)
                assert clock.pending == []
                new = await open_quotas(app, pilot)
                assert new is not old
                fetch.releases[0].set()
                await until(lambda: len(clock.pending) == 1)
                await pilot.pause()
                assert fetch.calls == 1
                assert fetch.max_active == 1
                assert old._refresh_timer is None
                assert new._refresh_timer is clock.pending[0]
                new.action_close()
                await pilot.pause()
                assert clock.pending == []
                clock.advance(1000)
                await pilot.pause()
                assert fetch.calls == 1
        finally:
            fetch.release_all()

    asyncio.run(exercise())


def test_provider_errors_still_schedule_the_next_refresh(tmp_path, monkeypatch) -> None:
    async def exercise() -> None:
        app, clock, fetch = make_app(tmp_path, monkeypatch)
        report = QuotaReport(
            fetched_at=datetime.fromtimestamp(clock(), timezone.utc),
            quotas=(),
            errors=("provider unavailable",),
        )
        monkeypatch.setattr(app.quota_service, "_fetch_report", lambda: report)
        async with app.run_test() as pilot:
            screen = await open_quotas(app, pilot)
            await until(lambda: len(clock.pending) == 1)
            previous = str(screen.query_one("#quota-content", Static).render())
            assert "provider unavailable" in previous
            monkeypatch.setattr(app.quota_service, "_fetch_report", fetch)
            fetch.release_all()
            clock.advance(300)
            await until(lambda: fetch.calls == 1 and len(clock.pending) == 1)
            content = str(screen.query_one("#quota-content", Static).render())
            assert "Codex weekly" in content
            assert "provider unavailable" not in content

    asyncio.run(exercise())
