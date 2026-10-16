from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import threading
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from agentmon.app import (
    AgentmonApp,
    CleanupScreen,
    DashboardScreen,
    DemoQuotaService,
    DemoService,
    QuotaScreen,
    agent_badge,
    main,
)
from agentmon.quota import QuotaReport, QuotaWindow
from agentmon.model import AgentRun, Repository
from agentmon.services import AgentmonContext, AgentmonService
from agentmon.transcript import Transcript, TranscriptMessage


def test_transcript_renders_goal_updates_distinctly() -> None:
    timestamp = "2026-07-16T20:23:27Z"
    rendered = DashboardScreen._render_transcript(
        (
            TranscriptMessage(
                role="goal",
                text="/goal Reach 80% test coverage.",
                timestamp=timestamp,
            ),
        ),
        (),
    )
    local_timestamp = (
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        .astimezone()
        .strftime("%Y-%m-%d %H:%M %Z")
    )

    assert rendered.plain == (
        f"GOAL · {local_timestamp}\n/goal Reach 80% test coverage."
    )


def test_transcript_preview_keeps_first_and_most_recent_messages() -> None:
    messages = tuple(
        TranscriptMessage("user", f"message {number}", f"timestamp-{number}")
        for number in range(1, 6)
    )
    console = Console(width=80, color_system=None)

    rendered = DashboardScreen._render_transcript_preview(
        messages,
        (),
        console=console,
        width=42,
        max_lines=8,
    )

    assert "message 1" in rendered.plain
    assert "message 5" in rendered.plain
    assert "message 2" not in rendered.plain
    assert "message 4" not in rendered.plain
    assert "3 hidden, press t: transcript" in rendered.plain
    assert len(rendered.wrap(console, 42)) <= 8


def test_transcript_collapse_marker_is_strictly_one_line() -> None:
    console = Console(width=80, color_system=None)
    marker = DashboardScreen._compact_preview_marker(9, ())

    marker.truncate(20, overflow="ellipsis")

    assert len(marker.wrap(console, 20)) == 1
    assert marker.plain.endswith("…")


def test_transcript_preview_prioritizes_last_first_then_second_last() -> None:
    messages = tuple(
        TranscriptMessage("user", f"message {number}", f"timestamp-{number}")
        for number in range(1, 6)
    )
    console = Console(width=80, color_system=None)

    rendered = DashboardScreen._render_transcript_preview(
        messages,
        (),
        console=console,
        width=42,
        max_lines=10,
    )

    assert "message 1" in rendered.plain
    assert "message 4" in rendered.plain
    assert "message 5" in rendered.plain
    assert "message 2" not in rendered.plain
    assert "message 3" not in rendered.plain
    assert rendered.plain.index("message 1") < rendered.plain.index("message 4")
    assert rendered.plain.index("message 4") < rendered.plain.index("message 5")
    assert len(rendered.wrap(console, 42)) <= 10


def test_dashboard_preview_does_not_clip_the_latest_message() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        first = "first message " * 100
        latest = (
            "Latest message has the highest priority and its final words "
            "must remain visible: latest message complete."
        )

        def transcript(run: AgentRun) -> Transcript:
            assert run.session_id is not None
            messages = (
                TranscriptMessage("user", first, "timestamp-1"),
                TranscriptMessage("user", "middle message 2", "timestamp-2"),
                TranscriptMessage("user", "middle message 3", "timestamp-3"),
                TranscriptMessage("user", "middle message 4", "timestamp-4"),
                TranscriptMessage("user", latest, "timestamp-5"),
            )
            return Transcript(
                run.session_id,
                Path("/demo/transcripts/priority.jsonl"),
                messages,
            )

        service.run_transcript = transcript  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(119, 34)) as pilot:
            await pilot.pause()
            content = app.screen.query_one("#transcript-content", Static)
            meta = app.screen.query_one("#transcript-meta", Static)
            preview = app.screen.query_one("#transcript-scroll", Vertical)

            assert "latest message complete" in str(content.render())
            assert "…" in str(content.render())
            assert not meta.display
            assert content.region.height <= preview.content_region.height

    asyncio.run(exercise())


def test_t_opens_the_complete_transcript_in_less(monkeypatch) -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(args: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("agentmon.app.subprocess.run", run)
        app.suspend = nullcontext  # type: ignore[method-assign]

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ["less"]
        assert kwargs["text"] is True
        document = str(kwargs["input"])
        assert "Fix authentication cleanup behavior" in document
        assert "Keep the existing CLI behavior unchanged" in document
        assert "/demo/transcripts/" in document

    asyncio.run(exercise())


def test_agent_badges_are_compact_and_have_a_future_agent_fallback() -> None:
    assert agent_badge("codex") == "CODX"
    assert agent_badge("claude-code") == "CLDE"
    # agy needs no entry of its own: the fallback already pads it to the
    # column width without mangling the name.
    assert agent_badge("agy") == "AGY "
    assert agent_badge("gemini-cli") == "GEMI"
    assert agent_badge("aider") == "AIDE"
    assert agent_badge("window") == "----"
    assert agent_badge("app") == "????"


def test_enter_on_run_switches_tiri_window() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        selected = []
        service.select_window = selected.append  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        assert selected == [app.runs[0]]

    asyncio.run(exercise())


def test_enter_switch_does_not_depend_on_data_table_selection_message() -> None:
    """The dashboard binding must win even while DataTable cannot post a row event."""

    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        selected = []
        service.select_window = selected.append  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            table.action_select_cursor = lambda: None  # type: ignore[method-assign]
            await pilot.press("enter")
            await pilot.pause()

        assert selected == [app.runs[0]]

    asyncio.run(exercise())


def test_j_and_k_navigate_dashboard_runs() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert table.cursor_row == 1

            await pilot.press("j", "j")
            assert table.cursor_row == 3

            await pilot.press("k")
            assert table.cursor_row == 2

            await pilot.press("G")
            assert table.cursor_row == table.row_count - 1

            await pilot.press("g")
            assert table.cursor_row == 1

    asyncio.run(exercise())


def test_dashboard_shows_compact_worktree_tree_and_transcript_side_by_side() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            first_session = "019f6c99-2762-7dc2-9d93-a8a4b48a3a5e"
            repository_row = " ".join(str(value) for value in table.get_row_at(0))
            first_row = " ".join(str(value) for value in table.get_row_at(1))
            assert "├─ 🔄 CODX auth-cleanup" in first_row
            assert first_session not in first_row
            assert [
                str(table.get_row_at(row)[0]).split()[1:3]
                for row in range(1, 5)
            ] == [
                ["🔄", "CODX"],
                ["✋", "CLDE"],
                ["💤", "CODX"],
                ["🏁", "----"],
            ]
            assert not table.show_header
            assert table.virtual_size.width <= table.content_region.width
            assert app.screen.query_one("#notice", Static).region.height == 0

            assert "▾ project  /demo/project" in repository_row
            assert "base:main" in repository_row

            runs_pane = app.screen.query_one("#runs-pane")
            transcript_pane = app.screen.query_one("#transcript-pane")
            assert runs_pane.region.right == transcript_pane.region.x
            # The panes share the width evenly (5fr / 5fr); the transcript pane
            # carries a 1-column right margin, so allow a one-column difference.
            assert abs(runs_pane.region.width - transcript_pane.region.width) <= 1

            meta = app.screen.query_one("#transcript-meta", Static)
            content = app.screen.query_one("#transcript-content", Static)
            assert not meta.display
            assert "Fix authentication cleanup behavior" in str(content.render())
            assert "Keep the existing CLI behavior unchanged" in str(content.render())

            await pilot.press("j")
            await pilot.pause()

            assert not meta.display
            assert "test_reconnect_after_heartbeat" in str(content.render())

    asyncio.run(exercise())


def test_dashboard_uses_main_worktree_as_repository_tree_root() -> None:
    async def exercise() -> None:
        root = Path("/srv/ses")
        repo = Repository(
            root=root,
            common_dir=Path("/srv/tiri/.git"),
            branch="ses",
        )
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            repository_row = " ".join(str(value) for value in table.get_row_at(0))
            assert "▾ tiri  /srv/tiri" in repository_row
            assert "base:ses" in repository_row

    asyncio.run(exercise())


def test_dashboard_groups_runs_from_multiple_repositories() -> None:
    async def exercise() -> None:
        primary = Repository(
            root=Path("/srv/ses"),
            common_dir=Path("/srv/tiri/.git"),
            branch="ses",
        )
        foreign = Repository(
            root=Path("/work/other"),
            common_dir=Path("/work/other/.git"),
            branch="develop",
        )
        service = DemoService(primary, socket="/tmp/demo")
        service.runs = lambda: [  # type: ignore[method-assign]
            AgentRun(
                "%1",
                "dev:1.0",
                "tiri-task",
                "working",
                "codex",
                Path("/srv/tiri-task"),
                worktree_state="dirty",
                session_id="tiri-session",
                repository=primary,
            ),
            AgentRun(
                "%2",
                "dev:2.0",
                "other-task",
                "blocked",
                "claude",
                Path("/work/other-task"),
                worktree_state="unmerged",
                session_id="other-session",
                repository=foreign,
            ),
        ]
        service.recent_finished_all = lambda _active: []  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert table.row_count == 4
            assert "▾ tiri  /srv/tiri" in str(table.get_row_at(0)[0])
            assert "└─ 🔄 CODX tiri-task" in str(table.get_row_at(1)[0])
            assert "▾ other  /work/other" in str(table.get_row_at(2)[0])
            assert "└─ ✋ CLDE other-task" in str(table.get_row_at(3)[0])
            assert table.cursor_row == 1

            await pilot.press("j")
            assert table.cursor_row == 3

    asyncio.run(exercise())


def test_dashboard_sorts_live_repositories_first_and_agent_windows_first() -> None:
    async def exercise() -> None:
        idle_repo = Repository(
            root=Path("/work/quiet"),
            common_dir=Path("/work/quiet/.git"),
            branch="main",
        )
        busy_repo = Repository(
            root=Path("/work/busy"),
            common_dir=Path("/work/busy/.git"),
            branch="main",
        )
        service = DemoService(None, socket="/tmp/demo")
        service.runs = lambda: [  # type: ignore[method-assign]
            # The quiet repository occupies the lower window ordinals, so only
            # the live agent in the busy one can float it to the top.
            AgentRun(
                "window:dev:1",
                "dev:1",
                "quiet",
                "none",
                "window",
                Path("/work/quiet"),
                repository=idle_repo,
                window_name="quiet",
            ),
            AgentRun(
                "window:dev:2",
                "dev:2",
                "shell",
                "none",
                "window",
                Path("/work/busy"),
                repository=busy_repo,
                window_name="shell",
            ),
            AgentRun(
                "window:dev:4",
                "dev:4",
                "feature",
                "idle",
                "codex",
                Path("/work/busy-feature"),
                repository=busy_repo,
                window_name="feature",
            ),
            AgentRun(
                "window:dev:3",
                "dev:3",
                "fix",
                "working",
                "claude",
                Path("/work/busy-fix"),
                repository=busy_repo,
                window_name="fix",
            ),
        ]
        service.recent_finished_all = lambda _active: [  # type: ignore[method-assign]
            AgentRun(
                "finished:/work/busy-done",
                "0:",
                "done",
                "exited",
                "finished",
                Path("/work/busy-done"),
                repository=busy_repo,
            )
        ]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            labels = [str(table.get_row_at(row)[0]) for row in range(table.row_count)]
            assert "▾ busy  /work/busy" in labels[0]
            # Agent windows lead by window ordinal, then plain windows, then
            # runs whose window is gone.
            assert "🔄 CLDE fix" in labels[1]
            assert "💤 CODX feature" in labels[2]
            assert "🪟 ---- shell" in labels[3]
            assert "🏁 ---- done" in labels[4]
            assert "▾ quiet  /work/quiet" in labels[5]
            assert "🪟 ---- quiet" in labels[6]

    asyncio.run(exercise())


def test_dashboard_shows_startup_repository_when_there_are_no_runs() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        service.runs = lambda: []  # type: ignore[method-assign]
        service.recent_finished_all = lambda _active: []  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert table.row_count == 1
            assert "▾ project  /demo/project" in str(table.get_row_at(0)[0])
            assert "No agent runs found" in str(
                app.screen.query_one("#notice", Static).render()
            )

    asyncio.run(exercise())


def test_dashboard_groups_windows_without_a_git_repository() -> None:
    async def exercise() -> None:
        repo = Repository(
            root=Path("/srv/tiri"),
            common_dir=Path("/srv/tiri/.git"),
            branch="main",
        )
        service = DemoService(repo, socket="/tmp/demo")
        service.runs = lambda: [  # type: ignore[method-assign]
            AgentRun(
                "window:dev:7",
                "dev:7",
                "ses",
                "working",
                "codex",
                Path("/srv/ses"),
                worktree_state="not-git",
                session_id="deleted-session",
                repository=None,
                window_name="deleted-worktree",
            )
        ]
        service.recent_finished_all = lambda _active: []  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert table.row_count == 3
            # The agent is running outside any repository, so its group leads
            # and the startup repository — which holds no windows — trails.
            assert "▾ non-Git windows" in str(table.get_row_at(0)[0])
            assert "directories" in str(table.get_row_at(0)[1])
            assert "└─ 🔄 CODX ses" in str(table.get_row_at(1)[0])
            assert str(table.get_row_at(1)[1]) == "—"
            assert "▾ tiri  /srv/tiri" in str(table.get_row_at(2)[0])
            assert table.cursor_row == 1

    asyncio.run(exercise())


def test_dashboard_without_a_startup_repository_can_monitor_non_git_windows() -> None:
    async def exercise() -> None:
        service = AgentmonService(None, socket="/tmp/demo")
        service.runs = lambda: [  # type: ignore[method-assign]
            AgentRun(
                "window:dev:7",
                "dev:7",
                "scratch",
                "none",
                "window",
                Path("/tmp/scratch"),
                worktree_state="not-git",
                repository=None,
                window_name="scratch",
            )
        ]
        service.recent_finished_all = lambda _active: []  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert table.row_count == 2
            assert "▾ non-Git windows" in str(table.get_row_at(0)[0])

    asyncio.run(exercise())


def test_main_preserves_session_without_a_git_repository(
    monkeypatch,
) -> None:
    from agentmon import app as app_module

    captured: list[AgentmonService] = []

    class FakeApp:
        def __init__(self, service: AgentmonService, **_kwargs: object) -> None:
            captured.append(service)

        def run(self) -> None:
            return None

    monkeypatch.setattr(
        app_module,
        "discover_context",
        lambda **_kwargs: AgentmonContext(
            repository=None, socket="/tmp/tiri.sock", session="$3"
        ),
    )
    monkeypatch.setattr(app_module, "AgentmonApp", FakeApp)

    assert main([]) == 0
    assert len(captured) == 1
    assert captured[0].repo is None
    assert captured[0].socket == "/tmp/tiri.sock"
    assert captured[0].session == "$3"


def test_dashboard_shows_git_window_without_running_agent() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        service.runs = lambda: [  # type: ignore[method-assign]
            AgentRun(
                "window:dev:7",
                "dev:7",
                "topic",
                "none",
                "window",
                root.parent / "topic",
                worktree_state="dirty",
                repository=repo,
                window_name="review-shell",
            )
        ]
        service.recent_finished_all = lambda _active: []  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            assert "🪟 ---- review-shell · topic" in str(table.get_row_at(1)[0])
            assert "1 window" in str(
                app.screen.query_one("#title", Static).render()
            )
            assert "Window · review-shell" in str(
                app.screen.query_one("#transcript-title", Static).render()
            )
            assert "No coding agent is running" in str(
                app.screen.query_one("#transcript-content", Static).render()
            )

    asyncio.run(exercise())


def test_w_opens_shell_at_selected_agent_cwd() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        opened = []
        service.open_shell_window = (  # type: ignore[method-assign]
            lambda run: opened.append(run) or "0:6"
        )
        app = AgentmonApp(service)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()

        assert opened == [app.runs[0]]

    asyncio.run(exercise())


def test_finished_worktree_shows_state_instead_of_dead_end_note() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()

            title = app.screen.query_one("#transcript-title", Static)
            content = app.screen.query_one("#transcript-content", Static)
            rendered = str(content.render())
            assert "Worktree · old-fix" in str(title.render())
            assert "no longer has an active agent session" not in rendered
            assert "Repository state" in rendered
            # The merged indicator mirrors the left pane's WORKTREE mark.
            assert "merged into main" in rendered
            # The full instruction text is shown verbatim, not collapsed.
            assert "Fix the parser edge case found during review." in rendered
            # Repository state precedes the instruction body.
            assert rendered.index("Repository state") < rendered.index(
                "Fix the parser edge case"
            )

    asyncio.run(exercise())


def test_render_worktree_overview_shows_repository_state_then_instruction() -> None:
    from agentmon.services import WorktreeOverview

    # Unmerged with committed + uncommitted changes shows every dimension.
    active = DashboardScreen._render_worktree_overview(
        WorktreeOverview(
            worktree=Path("/demo/done"),
            branch="done",
            base_branch="main",
            merged=False,
            has_instruction=True,
            instruction_text="Line one.\nLine two.\n",
            instruction_commit="abc1234",
            committed_changes=True,
            dirty=True,
        )
    ).plain
    assert "not yet merged into main" in active
    assert "committed changes on top of the instruction" in active
    assert "uncommitted changes present" in active
    assert "committed (abc1234)" in active
    # Instruction newlines are preserved rather than collapsed to one line.
    assert "Line one.\nLine two." in active
    assert active.index("Repository state") < active.index("Line one.")

    clean = DashboardScreen._render_worktree_overview(
        WorktreeOverview(
            worktree=Path("/demo/prepared"),
            branch="prepared",
            base_branch="main",
            merged=True,
            has_instruction=True,
            instruction_text="Do the thing.",
            instruction_commit="abc1234",
            committed_changes=False,
            dirty=False,
        )
    ).plain
    assert "merged into main" in clean
    assert "clean working tree" in clean

    missing = DashboardScreen._render_worktree_overview(
        WorktreeOverview(
            worktree=Path("/demo/blank"),
            branch="blank",
            base_branch="main",
            merged=False,
            has_instruction=False,
            instruction_text="",
            instruction_commit=None,
            committed_changes=False,
            dirty=False,
        )
    ).plain
    assert "no instruction.md prepared yet" in missing


def test_w_opens_shell_at_recent_finished_worktree() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        opened = []
        service.open_shell_window = lambda run: opened.append(run) or "0:7"  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            table.move_cursor(row=table.row_count - 1)
            await pilot.press("w")
            await pilot.pause()

        assert opened[0].state == "exited"
        assert opened[0].worktree == Path("/demo/old-fix")

    asyncio.run(exercise())


def test_x_confirms_cleanup_for_finished_merged_worktree() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        service = DemoService(repo, socket="/tmp/demo")
        cleaned = []
        service.cleanup_worktree = cleaned.append  # type: ignore[method-assign]
        app = AgentmonApp(service)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs", DataTable)
            table.move_cursor(row=table.row_count - 1)
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, CleanupScreen)
            await pilot.press("enter")
            await pilot.pause()

        assert cleaned[0].branch == "old-fix"

    asyncio.run(exercise())


def test_refresh_runs_gathers_status_off_the_event_loop() -> None:
    """Regression: the dashboard refresh must not run the blocking tiri/git scans
    on the event-loop thread, or keypresses stall for the duration of every
    refresh. Verify service.runs() executes on a worker thread, not the main one."""

    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        call_threads: list[threading.Thread] = []

        class RecordingService(DemoService):
            def runs(self):  # type: ignore[override]
                call_threads.append(threading.current_thread())
                return super().runs()

        app = AgentmonApp(RecordingService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()

        assert call_threads, "service.runs() was never called"
        assert all(
            t is not threading.main_thread() for t in call_threads
        ), "refresh ran on the event-loop thread (blocks input)"

    asyncio.run(exercise())


def test_command_palette_offers_only_monitoring_commands() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(DemoService(repo, socket="/tmp/demo"))

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            names = {
                command.title for command in app.get_system_commands(app.screen)
            }

        for title in (
            "Open window",
            "New shell at CWD",
            "Clean up worktree",
            "Refresh",
            "Full transcript",
            "Subscription quotas",
        ):
            assert title in names
        for title in (
            "Prepare worktree",
            "Setup instruction",
            "Launch agent",
            "Simple run",
            "Populate from run",
        ):
            assert title not in names

    asyncio.run(exercise())


def test_quota_dialog_toggles_and_shows_all_subscription_windows() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        app = AgentmonApp(
            DemoService(repo, socket="/tmp/demo"), quota_service=DemoQuotaService()
        )

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, QuotaScreen)
            content = str(app.screen.query_one("#quota-content", Static).render())
            for label in (
                "Codex weekly",
                "Codex 5h",
                "Claude 5h",
                "Claude weekly",
                "Fable weekly",
            ):
                assert label in content

            app.action_toggle_quotas()
            await pilot.pause()
            assert not isinstance(app.screen, QuotaScreen)

    asyncio.run(exercise())


def test_quota_rows_pair_the_usage_bar_with_a_pacing_bar() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    report = QuotaReport(
        fetched_at=now,
        quotas=(
            # 20% of the quota spent 80% of the way through the window.
            QuotaWindow("claude", "Claude 5h", 20.0, now + timedelta(hours=1), 18000.0),
            QuotaWindow("codex", "Codex weekly", 60.0, now + timedelta(days=1)),
        ),
    )
    rendered = QuotaScreen._render_report(report, now)
    header, paced, unpaced = str(rendered).splitlines()

    # The column names live in the header, not repeated on every row.
    assert header.split() == ["used", "pace", "left"]

    assert "    20%" in paced and "   -60%" in paced
    assert paced.endswith("1h00m")
    # Both bars share one row of half blocks: foreground is usage, background
    # is pacing, so the 20%/80% split shows up as three styled runs.
    plain = str(rendered)
    bar = [span for span in rendered.spans if "▀" in plain[span.start : span.end]]
    assert len(bar) == 3
    assert bar[0].end - bar[0].start == round(0.2 * QuotaScreen.BAR_WIDTH)
    assert bar[1].end - bar[1].start == round(0.6 * QuotaScreen.BAR_WIDTH)

    # A window of unknown length keeps the plain full-height usage bar.
    assert "    60%" in unpaced
    assert "%" not in unpaced.split("60%", 1)[1]
    assert "█" in unpaced
    # An unknown window length falls back to the remaining time for granularity.
    assert unpaced.endswith("1d0h")


def test_quota_reset_countdown_granularity_follows_the_window() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    week, session = 604800.0, 18000.0

    def countdown(delta: timedelta, window: float | None) -> str:
        quota = QuotaWindow("claude", "w", 1.0, now + delta, window)
        return QuotaScreen._reset_countdown(quota, now)

    assert countdown(timedelta(days=3, hours=4, minutes=30), week) == "3d4h"
    assert countdown(timedelta(hours=20, minutes=30), week) == "20h"
    assert countdown(timedelta(hours=2, minutes=5), session) == "2h05m"
    assert countdown(timedelta(minutes=9, seconds=59), session) == "9m"
    assert countdown(timedelta(seconds=-1), week) == "now"


def test_quota_dialog_reports_provider_errors() -> None:
    async def exercise() -> None:
        root = Path("/demo/project")
        repo = Repository(root=root, common_dir=root / ".git", branch="main")
        quota_service = DemoQuotaService()
        quota_service.report = lambda force=False: QuotaReport(  # type: ignore[method-assign]
            fetched_at=datetime.now(timezone.utc),
            quotas=(),
            errors=("codex is down",),
        )
        app = AgentmonApp(
            DemoService(repo, socket="/tmp/demo"), quota_service=quota_service
        )

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            app.action_toggle_quotas()
            await pilot.pause()
            await pilot.pause()

            content = str(app.screen.query_one("#quota-content", Static).render())
            assert "codex is down" in content

    asyncio.run(exercise())
