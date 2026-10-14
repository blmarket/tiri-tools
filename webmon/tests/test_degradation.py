"""Against a server without the agent plugin, the viewer degrades rather than fails."""

import asyncio

from conftest import run_async

from webmon.monitor import Monitor


async def until(predicate, timeout: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


class Sink:
    def __init__(self) -> None:
        self.resets: list[dict] = []
        self.screens: list[bytes] = []

    def deliver_model(self, message: dict) -> None:
        pass

    def deliver_screen(self, pane: str, data: bytes) -> None:
        self.screens.append(data)

    def deliver_reset(self, pane: str, payload: dict) -> None:
        self.resets.append(payload)


def test_a_stock_server_is_reported_as_the_lesser_tier(stock_server):
    async def scenario():
        monitor = Monitor(stock_server.tmux, stock_server.socket)
        await monitor.start()
        try:
            assert await until(lambda: bool(monitor.model.panes))
            # The tier probe runs with the first poll.
            await until(lambda: monitor.model.tier != "unknown")
            return monitor.model.tier, dict(monitor.model.panes)
        finally:
            await monitor.stop()

    tier, panes = run_async(scenario())
    assert tier == "stock"
    pane = next(iter(panes.values()))
    assert pane["pane_agent"] == "" and pane["pane_agent_state"] == ""
    assert pane["pane_state_emoji"] == "", "stock tmux has no glyph to give"
    # What a stock server does answer still has to be there.
    assert pane["session_name"] == "work"
    assert pane["pane_current_command"]
    assert pane["pane_current_path"]
    assert pane["pane_width"] == 80


def test_the_screen_plane_works_the_same_on_a_stock_server(stock_server):
    async def scenario():
        monitor = Monitor(stock_server.tmux, stock_server.socket)
        await monitor.start()
        sink = Sink()
        monitor.add_watcher(sink)
        try:
            assert await until(lambda: bool(monitor.model.panes))
            stock_server.command(
                "new-window", "-t", "work", "-n", "tick",
                "sh -c 'while :; do printf TICK; sleep 0.2; done'",
            )
            assert await until(
                lambda: any(
                    values["window_name"] == "tick"
                    for values in monitor.model.panes.values()
                )
            )
            pane = next(
                name
                for name, values in monitor.model.panes.items()
                if values["window_name"] == "tick"
            )
            await monitor.watch_pane(pane, sink)
            streamed = await until(lambda: b"TICK" in b"".join(sink.screens))
            return sink.resets, streamed
        finally:
            await monitor.stop()

    resets, streamed = run_async(scenario())
    assert resets and resets[0]["data"].startswith("\x1bc")
    assert streamed
