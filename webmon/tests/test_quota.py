import asyncio
from datetime import datetime, timezone

from agentmon.quota import QuotaReport, QuotaWindow
from conftest import run_async
from test_monitor import until
from webmon.monitor import Monitor
from webmon.provider import ProviderConnection, TerminalStream
from webmon.quota import QuotaSubscriptions


class FakeQuota:
    def __init__(self):
        self.calls = []
        self.fail = False

    def report(self, *, service):
        self.calls.append(service)
        if self.fail:
            return QuotaReport(datetime.now(timezone.utc), (), ("unavailable",))
        return QuotaReport(datetime.now(timezone.utc), (
            QuotaWindow(service, "5h", 25, datetime.fromtimestamp(20000, timezone.utc), 18000),
            QuotaWindow(service, "Unknown window", 10),
        ))


def test_only_attached_services_poll_and_shared_viewers_reuse_feed():
    async def scenario():
        source = FakeQuota()
        quotas = QuotaSubscriptions(source, interval=0.02)
        first, second = [], []
        assert source.calls == []
        quotas.subscribe(1, "codex", first.append)
        assert await until(lambda: first[-1]["status"] == "ready")
        assert first[-1]["windows"] == [
            {"label": "5h", "remaining_percent": 75, "resets_at": 20000, "window_seconds": 18000},
            {"label": "Unknown window", "remaining_percent": 90, "resets_at": None, "window_seconds": None},
        ]
        count = len(source.calls)
        quotas.subscribe(2, "codex", second.append)
        assert second[-1] == first[-1]
        assert len(quotas.tasks) == 1
        assert len(source.calls) == count
        quotas.unsubscribe(1)
        assert await until(lambda: len(source.calls) > count)
        quotas.unsubscribe(2)
        count = len(source.calls)
        await asyncio.sleep(0.06)
        assert len(source.calls) == count
        assert not quotas.tasks
        await quotas.close()
    run_async(scenario())


def test_switch_agent_and_preserve_stale_readings_on_failure():
    async def scenario():
        source = FakeQuota()
        quotas = QuotaSubscriptions(source, interval=0.02)
        messages = []
        quotas.subscribe(1, "codex", messages.append)
        assert await until(lambda: messages[-1]["status"] == "ready")
        windows = messages[-1]["windows"]
        source.fail = True
        assert await until(lambda: messages[-1]["status"] == "stale")
        assert messages[-1]["windows"][0]["remaining_percent"] == 75
        assert messages[-1]["windows"] == windows
        source.fail = False
        quotas.subscribe(1, "agy", messages.append)
        assert messages[-1]["status"] == "loading"
        assert await until(lambda: messages[-1]["status"] == "ready")
        assert messages[-1]["service"] == "antigravity"
        assert set(quotas.tasks) == {"antigravity"}
        quotas.subscribe(1, "unknown", messages.append)
        assert messages[-1]["status"] == "unsupported"
        assert not quotas.tasks
        await quotas.close()
    run_async(scenario())


def test_provider_model_mapping_is_viewer_only_and_updates_live():
    async def scenario():
        source = FakeQuota()
        monitor = Monitor("tmux", "/tmp/fixture")
        connection = ProviderConnection(monitor, None)
        connection.quotas = QuotaSubscriptions(source, interval=0.02)
        monitor.model.panes["%1"] = {"pane_agent": "codex"}
        connection.deliver_model({"t": "model"})
        assert not connection.quotas.tasks
        viewer = TerminalStream(connection, 3, "%1")
        connection.streams[3] = viewer
        connection.subscribe_quota(viewer)
        assert await until(lambda: bool(source.calls))
        monitor.model.panes["%1"]["pane_agent"] = "claude"
        connection.deliver_model({"t": "panes"})
        assert await until(lambda: "claude" in source.calls)
        monitor.model.panes.clear()
        connection.deliver_model({"t": "panes"})
        assert not connection.quotas.tasks
        await connection.quotas.close()
    run_async(scenario())


def test_terminal_unview_releases_the_quota_subscription():
    from unittest.mock import AsyncMock

    async def scenario():
        source = FakeQuota()
        monitor = Monitor("tmux", "/tmp/fixture")
        monitor.model.panes["%1"] = {"pane_agent": "codex"}
        monitor.watch_pane = AsyncMock()
        monitor.unwatch_pane = AsyncMock()
        connection = ProviderConnection(monitor, None)
        connection.quotas = QuotaSubscriptions(source, interval=0.02)
        await connection.handle({"t": "view", "pane": "%1", "stream": 3})
        assert await until(lambda: bool(source.calls))
        await connection.handle_terminal({
            "stream": 3, "pane": "%1", "message": {"t": "unview"},
        })
        assert not connection.quotas.tasks
        assert not connection.quotas.subscribers
        assert not connection.streams
        count = len(source.calls)
        await asyncio.sleep(0.06)
        assert len(source.calls) == count
        await connection.quotas.close()
    run_async(scenario())
