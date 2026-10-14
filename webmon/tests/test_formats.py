import asyncio

import pytest

from conftest import run_async
from test_monitor import started, until
from webmon.formats import validate_template


@pytest.mark.parametrize("template", ["#(touch /tmp/no)", "#{E:@job}", "#{q;E:@job}", "#{E;q:@job}", "#{s|x|#{pane_id}|;E:@job}", "#{T:@job}", "bad\nline", "bad\0value", "é" * 2049])
def test_unsafe_or_oversized_templates_are_rejected(template):
    with pytest.raises(ValueError):
        validate_template(template)


def test_templates_keep_opaque_values_and_share_live_subscriptions(server):
    async def scenario():
        monitor = await started(server, poll_interval=600)
        pane = server.pane_ids()[0]
        first, second = [], []
        template = "'\";\\\t💚 %H : #{pane_id}:#{@webmon_value}:#{?pane_active,yes,no}"
        prefix = "'\";\\\t💚 %H : "
        try:
            server.command("set-option", "-p", "-t", pane, "@webmon_value", "one : ' 雪")
            await monitor.formats.subscribe("first", template, lambda values, error: first.append((values, error)))
            await monitor.formats.subscribe("second", template, lambda values, error: second.append((values, error)))
            assert len(monitor.formats.feeds) == 1
            assert first[-1] == ({pane: prefix + pane + ":one : ' 雪:yes"}, None)
            assert second[-1] == first[-1]
            server.command("set-option", "-p", "-t", pane, "@webmon_value", "two\t值")
            assert await until(lambda: first[-1][0].get(pane) == prefix + pane + ":two\t值:yes")
            assert second[-1] == first[-1]
            await monitor.formats.unsubscribe("first")
            assert len(monitor.formats.feeds) == 1
            count = len(first)
            server.command("set-option", "-p", "-t", pane, "@webmon_value", "three")
            assert await until(lambda: second[-1][0].get(pane) == prefix + pane + ":three:yes")
            assert len(first) == count
            await monitor.formats.unsubscribe("second")
            assert not monitor.formats.feeds
        finally:
            await monitor.stop()

    run_async(scenario())


def test_new_sessions_removed_panes_and_replaced_control_clients_refresh_formats(server):
    async def scenario():
        monitor = await started(server, poll_interval=600, reconcile_interval=0.2)
        received = []
        try:
            await monitor.formats.subscribe("watch", "#{pane_id}|#{session_name}|#{@never_set}", lambda values, error: received.append((values, error)))
            server.command("new-session", "-d", "-s", "second", "sh")
            assert await until(lambda: any("|second|" in value for value in received[-1][0].values()))
            second_pane = next(pane for pane, value in received[-1][0].items() if "|second|" in value)
            server.command("kill-session", "-t", "second")
            assert await until(lambda: second_pane not in received[-1][0])
            client = next(iter(monitor._state_clients.values()))
            await client.stop()
            assert await until(lambda: bool(monitor._state_clients) and client not in monitor._state_clients.values())
            pane = server.pane_ids()[0]
            server.command("set-option", "-p", "-t", pane, "@never_set", "retained")
            assert await until(lambda: received[-1] == ({pane: pane + "|work|retained"}, None))
            await monitor.formats.unsubscribe("watch")
            await monitor.formats.subscribe("new", "#{@after_reconnect}", lambda values, error: received.append((values, error)))
            assert received[-1] == ({pane: ""}, None)
            server.command("set-option", "-p", "-t", pane, "@after_reconnect", "restored ✓")
            assert await until(lambda: received[-1] == ({pane: "restored ✓"}, None))
        finally:
            await monitor.stop()

    run_async(scenario())


def test_format_argument_cannot_inject_tmux_commands(server):
    async def scenario():
        monitor = await started(server)
        received = []
        template = "'; set-option -g @injected yes; display-message ' #{pane_id}"
        try:
            await monitor.formats.subscribe("injection", template, lambda values, error: received.append((values, error)))
            pane = server.pane_ids()[0]
            assert received[-1] == ({pane: template.replace("#{pane_id}", pane)}, None)
            assert server.command("show-options", "-gqv", "@injected") == ""
        finally:
            await monitor.stop()

    run_async(scenario())


def test_metadata_refreshes_without_subscription_notifications_or_full_polls(server):
    async def scenario():
        monitor = await started(server, poll_interval=600, reconcile_interval=600)
        received = []
        pane = server.pane_ids()[0]
        try:
            await monitor.formats.subscribe("watch", "#{@webmon_value}", lambda values, error: received.append((values, error)))
            feed = next(iter(monitor.formats.feeds.values()))
            for client in monitor._state_clients.values():
                reply = await client.execute(f"refresh-client -B {feed.name}")
                assert not reply.error
            for value in ("⠋", "⠙", "⠹"):
                server.command("set-option", "-p", "-t", pane, "@webmon_value", value)
                assert await until(lambda: received[-1] == ({pane: value}, None), timeout=3)
            await monitor.formats.unsubscribe("watch")
            count = len(received)
            await asyncio.sleep(1.4)
            assert len(received) == count
        finally:
            await monitor.stop()

    run_async(scenario())
