from __future__ import annotations

import argparse
import random
import subprocess
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Label, Static

from . import __version__
from .model import AGENT_LABELS, AgentRun, Repository
from .quota import DEFAULT_TTL_SECONDS, QuotaReport, QuotaService, QuotaWindow
from .services import (
    AgentmonService,
    CommandError,
    WorktreeOverview,
    discover_context,
    discover_repository,
)
from .transcript import Transcript, TranscriptError, TranscriptMessage


STATE_EMOJI = {
    "working": "🔄",
    "blocked": "✋",
    "idle": "💤",
    "exited": "🏁",
    "none": "🪟",
}

AGENT_BADGE = {
    "codex": "CODX",
    "claude": "CLDE",
    "claude-code": "CLDE",
    "finished": "----",
    "window": "----",
    "app": "????",
}

WORKTREE_MARK = {
    "merged": "✓ merged",
    "unmerged": "↑ unmerged",
    "dirty": "! dirty",
    "unknown": "? unknown",
    "not-git": "—",
}

# Relative widths of the dashboard's run list and transcript panes. Adjust
# these to reslice the split (e.g. 3 / 2 for a 60/40 layout); they are woven
# into the dashboard CSS below.
RUNS_PANE_FRACTION = 5
TRANSCRIPT_PANE_FRACTION = 5


# tiri agent states that still belong to a live coding agent. A repository with
# any of these outranks one whose windows are all plain shells or finished runs.
LIVE_AGENT_STATES = frozenset({"working", "blocked", "idle"})

# Placeholder agent identifiers tiri never reports; they stand in for panes that
# run no recognized coding agent (see AGENT_BADGE).
NON_AGENT_IDENTIFIERS = frozenset({"window", "app", "finished"})

# Sorts after every real "session:index" ordinal, so runs with no live window
# (and repositories with no windows at all) trail the ones that have them.
_NO_WINDOW_ORD = ("￿", 1 << 31)


def _window_ord(run: AgentRun) -> tuple[str, int]:
    """Return the tiri window ordinal (session, window index) a run sits in."""
    session, _, window = run.location.partition(":")
    index = window.split(".", 1)[0]
    if not index.isdigit():
        return _NO_WINDOW_ORD
    return (session, int(index))


def _is_agent_run(run: AgentRun) -> bool:
    return run.agent not in NON_AGENT_IDENTIFIERS


def _run_sort_key(run: AgentRun) -> tuple[int, tuple[str, int]]:
    """Order a repository's rows: agent windows, other windows, finished runs."""
    if run.state == "exited":
        tier = 2
    else:
        tier = 0 if _is_agent_run(run) else 1
    return (tier, _window_ord(run))


def _repository_sort_key(runs: list[AgentRun]) -> tuple[int, tuple[str, int]]:
    """Order repository groups: live agents first, then by their first window.

    "First window" is the lowest window ordinal the repository occupies, not the
    ordinal of whichever row the within-group ordering floats to the top, so the
    group order stays independent of which of its windows runs an agent.
    """
    live = any(
        _is_agent_run(run) and run.state in LIVE_AGENT_STATES for run in runs
    )
    ordinals = [
        _window_ord(run) for run in runs if _window_ord(run) != _NO_WINDOW_ORD
    ]
    return (0 if live else 1, min(ordinals, default=_NO_WINDOW_ORD))


def agent_badge(agent: str) -> str:
    """Return a compact, terminal-safe label for an tiri agent identifier."""
    known = AGENT_BADGE.get(agent)
    if known is not None:
        return known
    compact = "".join(character for character in agent if character.isalnum())
    return (compact[:4] or "????").upper().ljust(4)


class NavigableDataTable(DataTable):
    """A data table that also supports Vim-style vertical navigation."""

    BINDINGS = [
        Binding("j", "cursor_down", "Cursor down", show=False),
        Binding("k", "cursor_up", "Cursor up", show=False),
        Binding("g", "cursor_top", "Cursor to top", show=False),
        Binding("G", "cursor_bottom", "Cursor to bottom", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repository_rows: set[int] = set()

    def _move_worktree_cursor(self, offset: int) -> None:
        if not self.row_count:
            return
        row = self.cursor_row
        while True:
            candidate = row + offset
            if candidate < 0 or candidate >= self.row_count:
                return
            row = candidate
            if row not in self.repository_rows:
                self.move_cursor(row=row)
                return

    def action_cursor_down(self) -> None:
        self._move_worktree_cursor(1)

    def action_cursor_up(self) -> None:
        self._move_worktree_cursor(-1)

    def _jump_worktree_cursor(self, rows: Iterable[int]) -> None:
        for row in rows:
            if row not in self.repository_rows:
                self.move_cursor(row=row)
                return

    def action_cursor_top(self) -> None:
        self._jump_worktree_cursor(range(self.row_count))

    def action_cursor_bottom(self) -> None:
        self._jump_worktree_cursor(range(self.row_count - 1, -1, -1))


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("enter", "open_run", "Open window", priority=True),
        ("w", "new_window", "New shell at CWD"),
        ("x", "cleanup", "Clean up worktree"),
        ("r", "reload", "Refresh"),
        ("t", "view_transcript", "Full transcript"),
        ("u", "app.toggle_quotas", "Quotas"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with Horizontal(id="dashboard-main"):
            with Vertical(id="runs-pane"):
                yield NavigableDataTable(
                    id="runs",
                    cursor_type="row",
                    zebra_stripes=True,
                    show_header=False,
                )
                yield Static("", id="notice")
            with Vertical(id="transcript-pane"):
                yield Static("Transcript", id="transcript-title")
                yield Static(
                    "Select a run to inspect its agent session.", id="transcript-meta"
                )
                with Vertical(id="transcript-scroll"):
                    yield Static("", id="transcript-content")
        yield Footer()

    def on_mount(self) -> None:
        self._subscription_stop = threading.Event()
        self._subscription_started = False
        self._transcript_session_id: str | None = None
        self._transcript_signature: tuple[object, ...] | None = None
        self._loaded_transcript: Transcript | None = None
        self._overview_pane_id: str | None = None
        self._row_runs: list[AgentRun | None] = []
        self._row_repositories: list[Repository | None] = []
        table = self.query_one("#runs", DataTable)
        table.add_column("WORKTREE", width=28)
        table.add_column("GIT", width=12)
        table.add_column("TIRI", width=9)
        self.set_interval(30.0, self.refresh_runs)
        self.refresh_runs()

    def on_unmount(self) -> None:
        self._subscription_stop.set()

    def refresh_runs(self) -> None:
        # The tiri/git scans run in a worker thread instead of blocking the event
        # loop, which keeps keypresses responsive during snapshot reconciliation.
        self._load_runs()

    @work(thread=True, exclusive=True, group="runs")
    def _load_runs(self) -> None:
        """Gather run status off the event loop; apply results on the UI thread."""
        service = self.app.service
        try:
            active = service.runs()
            runs = active + service.recent_finished_all(active)
        except CommandError as exc:
            self.app.call_from_thread(self._show_disconnected, str(exc))
        else:
            self.app.call_from_thread(self._apply_runs, runs)

    @work(thread=True, exclusive=True, group="subscription")
    def _watch_run_changes(self) -> None:
        """Refresh snapshots when tmux format subscriptions report changes."""

        def changed() -> None:
            if not self._subscription_stop.is_set():
                self.app.call_from_thread(self.refresh_runs)

        try:
            self.app.service.watch_runs(changed, self._subscription_stop)
        except CommandError:
            # The periodic snapshot remains authoritative if the advisory
            # control-mode channel cannot be established.
            return

    def _apply_runs(self, runs: list[AgentRun]) -> None:
        selected = self._selected_run()
        selected_pane = selected.pane_id if selected is not None else None
        table = self.query_one("#runs", NavigableDataTable)
        selected_repository = (
            self._selected_repository()
            if table.cursor_row in table.repository_rows
            else None
        )
        selected_repository_key = (
            selected_repository.common_dir
            if selected_repository is not None
            else None
        )
        self.app.runs = runs
        table.clear()
        table.repository_rows.clear()
        self._row_runs = []
        self._row_repositories = []

        default_repository = self.app.service.repo
        grouped: dict[Path | None, tuple[Repository | None, list[AgentRun]]] = {}
        if default_repository is not None:
            grouped[default_repository.common_dir] = (default_repository, [])
        for run in runs:
            repository = run.repository
            key = repository.common_dir if repository is not None else None
            grouped.setdefault(key, (repository, []))[1].append(run)

        ordered_groups = sorted(
            grouped.values(), key=lambda group: _repository_sort_key(group[1])
        )

        counts: dict[str, int] = {}
        selected_row: int | None = None
        first_worktree_row: int | None = None
        for group_index, (repository, repository_runs) in enumerate(ordered_groups):
            repository_runs = sorted(repository_runs, key=_run_sort_key)
            repository_row = len(self._row_runs)
            repository_key = (
                repository.common_dir if repository is not None else None
            )
            table.repository_rows.add(repository_row)
            if repository is None:
                group_label = Text("▾ non-Git windows", style="bold #8ec8ff")
                base_label = Text("directories", style="bold #8ec8ff")
            else:
                project_root = repository.project_root
                group_label = Text(
                    f"▾ {project_root.name}  {project_root}", style="bold #8ec8ff"
                )
                base_label = Text(
                    f"base:{repository.branch}", style="bold #8ec8ff"
                )
            table.add_row(
                group_label,
                base_label,
                "",
                key=f"repository:{group_index}",
            )
            self._row_runs.append(None)
            self._row_repositories.append(repository)
            if (
                selected_pane is None
                and repository_key is not None
                and repository_key == selected_repository_key
            ):
                selected_row = repository_row

            for index, run in enumerate(repository_runs):
                counts[run.state] = counts.get(run.state, 0) + 1
                connector = "└─" if index == len(repository_runs) - 1 else "├─"
                status = STATE_EMOJI.get(run.state, "❔")
                badge = agent_badge(run.agent)
                label = run.branch
                if (
                    run.state == "none"
                    and run.window_name
                    and run.window_name != run.branch
                ):
                    label = f"{run.window_name} · {run.branch}"
                table.add_row(
                    f"  {connector} {status} {badge} {label}",
                    WORKTREE_MARK.get(run.worktree_state, run.worktree_state),
                    run.location if run.state != "exited" else "—",
                    key=run.pane_id,
                )
                row = len(self._row_runs)
                self._row_runs.append(run)
                self._row_repositories.append(run.repository)
                if first_worktree_row is None:
                    first_worktree_row = row
                if run.pane_id == selected_pane:
                    selected_row = row

        summary = "  ".join(
            f"{n} {'window' if state == 'none' else state}"
            for state, n in counts.items()
        )
        self.query_one("#title", Static).update(f"agentmon    {summary or 'no active runs'}")
        notice = self.app.startup_warning
        if not runs and not notice:
            notice = "No agent runs found"
        self._set_notice(notice)
        if first_worktree_row is not None:
            table.move_cursor(
                row=selected_row if selected_row is not None else first_worktree_row
            )
        self._refresh_transcript()
        if not self._subscription_started:
            self._subscription_started = True
            self._watch_run_changes()

    def _set_notice(self, message: str) -> None:
        notice = self.query_one("#notice", Static)
        notice.update(message)
        notice.display = bool(message)

    def _show_disconnected(self, message: str) -> None:
        self.app.runs = []
        self.query_one("#runs", DataTable).clear()
        self._row_runs = []
        self._row_repositories = []
        self.query_one("#title", Static).update("agentmon    disconnected")
        self._set_notice(f"Could not read tiri: {message}")
        self._clear_transcript("Agent runs are unavailable while tiri is disconnected.")

    def _selected_run(self) -> AgentRun | None:
        table = self.query_one("#runs", DataTable)
        row = table.cursor_row
        if row >= len(self._row_runs):
            return None
        return self._row_runs[row]

    def _selected_repository(self) -> Repository | None:
        table = self.query_one("#runs", DataTable)
        row = table.cursor_row
        if row >= len(self._row_repositories):
            return None
        return self._row_repositories[row]

    def _set_transcript_meta(self, message: str | None) -> None:
        meta = self.query_one("#transcript-meta", Static)
        meta.update(message or "")
        meta.display = message is not None

    def _clear_transcript(self, message: str) -> None:
        self._transcript_session_id = None
        self._transcript_signature = None
        self._loaded_transcript = None
        self.query_one("#transcript-title", Static).update("Transcript")
        self._set_transcript_meta(None)
        self.query_one("#transcript-content", Static).update(message)

    def _refresh_transcript(self) -> None:
        # Reset the worktree-overview tracker; only the overview branch below
        # re-arms it, so every other path leaves it cleared.
        previous_overview_pane = self._overview_pane_id
        self._overview_pane_id = None
        run = self._selected_run()
        if run is None:
            self._clear_transcript("Select a run to inspect its agent session.")
            return

        agent_key = "claude" if run.agent == "claude-code" else run.agent
        agent_name = AGENT_LABELS.get(agent_key, run.agent)
        if run.state == "none":
            self._transcript_session_id = None
            self._transcript_signature = None
            self._loaded_transcript = None
            self.query_one("#transcript-title", Static).update(
                f"Window · {run.window_name or run.branch}"
            )
            self._set_transcript_meta(
                f"TIRI       {run.location}\nWorktree   {run.worktree}"
            )
            self.query_one("#transcript-content", Static).update(
                "No coding agent is running in this window."
            )
            return
        if not run.session_id:
            self._transcript_session_id = None
            self._transcript_signature = None
            self._loaded_transcript = None
            if run.state == "exited" and run.repository is not None:
                # A finished or freshly prepared worktree with no live agent
                # session: show what the worktree holds instead of a dead-end
                # note about a missing session.
                self._overview_pane_id = run.pane_id
                self.query_one("#transcript-title", Static).update(
                    f"Worktree · {run.branch}"
                )
                self._set_transcript_meta(None)
                if run.pane_id != previous_overview_pane:
                    self.query_one("#transcript-content", Static).update(
                        "Inspecting worktree state…"
                    )
                self._load_worktree_overview(run)
                return
            self.query_one("#transcript-title", Static).update(
                f"Transcript · {run.branch} · {agent_name}"
            )
            self._set_transcript_meta(None)
            self.query_one("#transcript-content", Static).update(
                "This finished run no longer has an active agent session."
                if run.state == "exited"
                else "Waiting for tiri to discover this agent's session id."
            )
            return
        self.query_one("#transcript-title", Static).update(
            f"Transcript · {run.branch} · {agent_name}"
        )
        self._set_transcript_meta(None)

        changed_session = run.session_id != self._transcript_session_id
        self._transcript_session_id = run.session_id
        if changed_session:
            self._transcript_signature = None
            self._loaded_transcript = None
            self.query_one("#transcript-content", Static).update("Loading transcript…")
        self._load_transcript(run)

    @work(thread=True, exclusive=True, group="transcript")
    def _load_transcript(self, run: AgentRun) -> None:
        try:
            loaded = self.app.service.run_transcript(run)
        except (TranscriptError, OSError) as exc:
            self.app.call_from_thread(
                self._show_transcript_error, run.pane_id, run.session_id, str(exc)
            )
        else:
            self.app.call_from_thread(
                self._apply_transcript, run.pane_id, run.session_id, loaded
            )

    def _is_selected_session(self, pane_id: str, session_id: str | None) -> bool:
        selected = self._selected_run()
        return bool(
            selected
            and selected.pane_id == pane_id
            and selected.session_id == session_id
        )

    @work(thread=True, exclusive=True, group="transcript")
    def _load_worktree_overview(self, run: AgentRun) -> None:
        """Gather an agentless worktree's state off the event loop."""
        try:
            overview = self.app.service.for_run(run).worktree_overview(run)
        except (CommandError, OSError) as exc:
            self.app.call_from_thread(
                self._show_worktree_error, run.pane_id, str(exc)
            )
        else:
            self.app.call_from_thread(
                self._apply_worktree_overview, run.pane_id, overview
            )

    def _apply_worktree_overview(
        self, pane_id: str, overview: WorktreeOverview
    ) -> None:
        if self._overview_pane_id != pane_id:
            return
        selected = self._selected_run()
        if selected is None or selected.pane_id != pane_id:
            return
        self.query_one("#transcript-content", Static).update(
            self._render_worktree_overview(overview)
        )

    def _show_worktree_error(self, pane_id: str, message: str) -> None:
        if self._overview_pane_id != pane_id:
            return
        selected = self._selected_run()
        if selected is None or selected.pane_id != pane_id:
            return
        self.query_one("#transcript-content", Static).update(
            Text.assemble(("Worktree state unavailable\n", "bold #ffcb6b"), message)
        )

    @staticmethod
    def _render_worktree_overview(overview: WorktreeOverview) -> Text:
        rendered = Text()
        rendered.append("Worktree   ", style="bold #8ec8ff")
        rendered.append(f"{overview.worktree}\n")
        rendered.append("Branch     ", style="bold #8ec8ff")
        rendered.append(f"{overview.branch}\n")

        # Repository state expands the left pane's single mark: the merged glyph
        # matches the WORKTREE column, and the committed/uncommitted lines are
        # independent, so the combined "committed change + uncommitted change"
        # case shows both. It stays compact so the instruction gets the space.
        rendered.append("\nRepository state\n", style="bold #8ec8ff")
        if overview.merged:
            rendered.append(
                f"  ✓ merged into {overview.base_branch}\n", style="#7ec97e"
            )
        else:
            rendered.append(
                f"  ↑ not yet merged into {overview.base_branch}\n", style="#ffcb6b"
            )
        if overview.committed_changes:
            rendered.append(
                "  ✓ committed changes on top of the instruction\n", style="#7ec97e"
            )
        if overview.dirty:
            rendered.append("  ! uncommitted changes present\n", style="#ffcb6b")
        else:
            rendered.append("  ✓ clean working tree\n", style="#8492a0")

        # The instruction is the essential information for an idle worktree, so
        # it goes last and is shown verbatim rather than as a collapsed preview.
        rendered.append("\nInstruction", style="bold #8ec8ff")
        if not overview.has_instruction:
            rendered.append("\n  ✗ no instruction.md prepared yet", style="#ffcb6b")
            return rendered
        status = (
            f"  ·  committed ({overview.instruction_commit})"
            if overview.instruction_commit is not None
            else "  ·  present, not committed"
        )
        rendered.append(f"{status}\n", style="#8492a0")
        rendered.append(overview.instruction_text.strip("\n") or "(empty instruction.md)")
        return rendered

    def _show_transcript_error(
        self, pane_id: str, session_id: str | None, message: str
    ) -> None:
        if not self._is_selected_session(pane_id, session_id):
            return
        self._transcript_signature = None
        self._loaded_transcript = None
        self.query_one("#transcript-content", Static).update(
            Text.assemble(("Transcript unavailable\n", "bold #ffcb6b"), message)
        )

    def _apply_transcript(
        self, pane_id: str, session_id: str | None, loaded: Transcript
    ) -> None:
        if not self._is_selected_session(pane_id, session_id):
            return
        signature: tuple[object, ...] = (
            loaded.rollout_path,
            loaded.skipped_lines,
            *((message.timestamp, message.text) for message in loaded.messages),
        )
        self._loaded_transcript = loaded
        if signature == self._transcript_signature:
            return
        self._transcript_signature = signature
        self.call_after_refresh(self._update_transcript_preview)

    def _update_transcript_preview(self) -> None:
        loaded = self._loaded_transcript
        if loaded is None:
            return
        preview = self.query_one("#transcript-scroll", Vertical)
        content = self.query_one("#transcript-content", Static)
        padding = content.styles.padding
        width = max(
            1,
            preview.content_region.width - padding.left - padding.right,
        )
        max_lines = max(
            1, preview.content_region.height - padding.top - padding.bottom
        )
        self.query_one("#transcript-content", Static).update(
            self._render_transcript_preview(
                loaded.messages,
                loaded.skipped_lines,
                console=self.app.console,
                width=width,
                max_lines=max_lines,
            )
        )

    @staticmethod
    def _render_transcript(
        messages: tuple[TranscriptMessage, ...], skipped_lines: tuple[int, ...]
    ) -> Text:
        rendered = Text()
        if not messages:
            rendered.append(
                "No user interactions were found.", style="italic #8492a0"
            )
        for index, message in enumerate(messages):
            if index:
                rendered.append("\n\n")
            timestamp = (
                f" · {DashboardScreen._local_timestamp(message.timestamp)}"
                if message.timestamp
                else ""
            )
            rendered.append(
                f"{message.role.upper()}{timestamp}\n", style="bold #8ec8ff"
            )
            rendered.append(message.text)
        if skipped_lines:
            lines = ", ".join(str(line) for line in skipped_lines)
            rendered.append(
                f"\n\nWarning: skipped malformed JSONL line(s): {lines}",
                style="#ffcb6b",
            )
        return rendered

    @staticmethod
    def _local_timestamp(timestamp: str) -> str:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return timestamp
        if parsed.tzinfo is None:
            return timestamp
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")

    @classmethod
    def _render_transcript_preview(
        cls,
        messages: tuple[TranscriptMessage, ...],
        skipped_lines: tuple[int, ...],
        *,
        console: Console,
        width: int,
        max_lines: int,
    ) -> Text:
        """Fill the preview in last, first, second-last priority order."""

        width = max(1, width)
        full = cls._render_transcript(messages, skipped_lines)
        full_lines = list(full.wrap(console, width))
        if len(full_lines) <= max_lines:
            return full
        if not messages:
            return cls._join_visual_lines(full_lines[:max_lines])

        if len(messages) == 1:
            visible = full_lines[:max_lines]
            if len(full_lines) > max_lines and visible:
                visible[-1].append(" …", style="italic #8492a0")
            return cls._join_visual_lines(visible)

        blocks = [
            list(
                cls._render_transcript((message,), ()).wrap(console, width)
            )
            for message in messages
        ]
        priority = [len(messages) - 1, 0, *range(len(messages) - 2, 0, -1)]
        selected: dict[int, list[Text]] = {}

        for message_index in priority:
            block = blocks[message_index]
            trial = {**selected, message_index: block}
            visible = cls._compose_prioritized_preview(
                trial,
                message_count=len(messages),
                skipped_lines=skipped_lines,
                width=width,
            )
            if len(visible) <= max_lines:
                selected = trial
                continue

            partial: list[Text] | None = None
            for line_count in range(1, len(block)):
                shortened_trial = {
                    **selected,
                    message_index: cls._ellipsize_visual_lines(
                        block[:line_count], width
                    ),
                }
                candidate = cls._compose_prioritized_preview(
                    shortened_trial,
                    message_count=len(messages),
                    skipped_lines=skipped_lines,
                    width=width,
                )
                if len(candidate) > max_lines:
                    break
                partial = candidate
            if partial is not None:
                return cls._join_visual_lines(partial)
            if selected:
                return cls._join_visual_lines(
                    cls._compose_prioritized_preview(
                        selected,
                        message_count=len(messages),
                        skipped_lines=skipped_lines,
                        width=width,
                    )[:max_lines]
                )
            return cls._join_visual_lines(block[:max_lines])

        return cls._join_visual_lines(
            cls._compose_prioritized_preview(
                selected,
                message_count=len(messages),
                skipped_lines=skipped_lines,
                width=width,
            )[:max_lines]
        )

    @classmethod
    def _compose_prioritized_preview(
        cls,
        selected: dict[int, list[Text]],
        *,
        message_count: int,
        skipped_lines: tuple[int, ...],
        width: int,
    ) -> list[Text]:
        hidden = message_count - len(selected)
        marker = cls._compact_preview_marker(hidden, skipped_lines)
        marker.truncate(width, overflow="ellipsis")
        visible: list[Text] = []

        first = selected.get(0)
        if first is not None:
            visible.extend(first)
            visible.append(Text())
        visible.append(marker)

        recent = sorted(index for index in selected if index)
        for index in recent:
            visible.append(Text())
            visible.extend(selected[index])
        return visible

    @staticmethod
    def _compact_preview_marker(
        hidden: int,
        skipped_lines: tuple[int, ...],
    ) -> Text:
        details = []
        if hidden:
            details.append(f"{hidden} hidden")
        if skipped_lines:
            details.append(f"{len(skipped_lines)} malformed")
        detail = ", ".join(details)
        message = (
            f"{detail}, press t: transcript"
            if detail
            else "press t: transcript"
        )
        return Text(
            message,
            style="italic #8492a0",
        )

    @staticmethod
    def _ellipsize_visual_lines(lines: list[Text], width: int) -> list[Text]:
        shortened = [line.copy() for line in lines]
        if shortened:
            shortened[-1].append(" …", style="italic #8492a0")
            shortened[-1].truncate(width, overflow="ellipsis")
        return shortened

    @staticmethod
    def _join_visual_lines(lines: list[Text]) -> Text:
        return Text("\n").join(lines)

    def on_resize(self) -> None:
        if getattr(self, "_loaded_transcript", None) is not None:
            self.call_after_refresh(self._update_transcript_preview)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "runs":
            self._refresh_transcript()

    def action_reload(self) -> None:
        self.refresh_runs()

    def action_open_run(self) -> None:
        run = self._selected_run()
        if run is None:
            return
        try:
            self.app.service.select_window(run)
            self._set_notice(f"Switched to {run.location}")
        except CommandError as exc:
            self._set_notice(str(exc))

    def action_new_window(self) -> None:
        run = self._selected_run()
        if run is None:
            return
        try:
            window = self.app.service.open_shell_window(run)
            self._set_notice(
                f"Opened shell {window or 'window'} at {run.worktree}"
            )
        except CommandError as exc:
            self._set_notice(f"Could not open shell: {exc}")

    def action_cleanup(self) -> None:
        run = self._selected_run()
        if run is None:
            return
        if run.state != "exited" or run.worktree_state != "merged":
            self._set_notice(
                "Cleanup is available only for finished, clean, merged worktrees"
            )
            return
        self.app.push_screen(CleanupScreen(run))

    def action_view_transcript(self) -> None:
        loaded = self._loaded_transcript
        if loaded is None:
            self._set_notice("No transcript is loaded for the selected run")
            return
        transcript = self._render_transcript(
            loaded.messages, loaded.skipped_lines
        ).plain
        document = (
            f"Session ID  {loaded.session_id}\n"
            f"Source      {loaded.rollout_path}\n\n"
            f"{transcript}\n"
        )
        try:
            with self.app.suspend():
                result = subprocess.run(
                    ["less"],
                    input=document,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            self._set_notice(f"Could not open transcript in less: {exc}")
            return
        if result.returncode not in (0, 130):
            self._set_notice(f"less exited with status {result.returncode}")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs":
            self.action_open_run()

    def action_quit(self) -> None:
        self.app.exit()


class CleanupScreen(Screen):
    BINDINGS = [
        ("enter", "confirm", "Remove worktree"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, run: AgentRun) -> None:
        super().__init__()
        self.run = run

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(f"Clean up {self.run.branch}?", classes="dialog-title")
            yield Static(
                f"Remove registered worktree\n  {self.run.worktree}\n\n"
                "The branch will be preserved. Git state will be checked again."
            )
            yield Static("", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Remove worktree", id="remove", classes="dialog-action")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#remove", Button).focus()

    def _confirm(self) -> None:
        try:
            self.app.service.for_run(self.run).cleanup_worktree(self.run)
        except CommandError as exc:
            self.query_one("#error", Static).update(str(exc))
            return
        self.app.pop_screen()
        dashboard = self.app.screen
        if isinstance(dashboard, DashboardScreen):
            dashboard.refresh_runs()
            dashboard._set_notice(
                f"Removed worktree {self.run.worktree}; branch {self.run.branch} was preserved"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "remove":
            self._confirm()
        else:
            self.app.pop_screen()

    def action_confirm(self) -> None:
        self._confirm()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class QuotaScreen(ModalScreen):
    """Floating dialog summarizing subscription quota usage."""

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("r", "refresh", "Refresh now"),
    ]

    BAR_WIDTH = 40
    USED_WIDTH = 7
    PACE_WIDTH = 7
    RESET_WIDTH = 8  # 2-column gutter plus the widest countdown, 23h59m
    TRACK_COLOR = "#33414f"
    PACE_COLOR = "#4d7fa8"

    def __init__(self) -> None:
        super().__init__()
        self._refresh_timer: Timer | None = None
        self._loading = False
        self._forcing = False
        self._pending_force = False
        self._quota_closed = True

    def compose(self) -> ComposeResult:
        with Vertical(id="quota-dialog"):
            yield Label("Subscription quotas", classes="dialog-title")
            yield Static("Loading quota usage…", id="quota-content")
            yield Static(self._legend(), id="quota-legend")
            yield Static("", id="quota-meta")
            yield Static("r refresh  ·  Esc close", classes="key-hint")

    @classmethod
    def _legend(cls) -> Text:
        legend = Text()
        legend.append("▀", style=f"#7ec97e on {cls.TRACK_COLOR}")
        legend.append(" used   ", style="#8492a0")
        legend.append("▀", style=f"{cls.TRACK_COLOR} on {cls.PACE_COLOR}")
        legend.append(
            " window elapsed · pace is used minus elapsed, so + runs ahead",
            style="#8492a0",
        )
        return legend

    def on_mount(self) -> None:
        self._quota_closed = False
        self._request_report()

    def on_unmount(self) -> None:
        self._quota_closed = True
        self._pending_force = False
        self._stop_refresh_timer()

    def _stop_refresh_timer(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def _request_report(self, force: bool = False) -> None:
        if self._quota_closed:
            return
        self._stop_refresh_timer()
        if self._loading:
            self._pending_force |= force and not self._forcing
            return
        self._loading = True
        self._forcing = force
        if force:
            self.query_one("#quota-meta", Static).update("Refreshing quota usage…")
        self._load_report(force)

    @work(thread=True, group="quota")
    def _load_report(self, force: bool) -> None:
        app = self.app
        report = app.quota_service.report(force=force)
        app.call_from_thread(self._apply_report, report)

    def _apply_report(self, report: QuotaReport) -> None:
        self._loading = False
        if self._quota_closed or not self.is_mounted:
            return
        self.query_one("#quota-content", Static).update(self._render_report(report))
        fetched = report.fetched_at.astimezone().strftime("%H:%M:%S")
        self.query_one("#quota-meta", Static).update(
            Text(f"Fetched {fetched} · cached between refreshes", style="#8492a0")
        )
        if self._pending_force:
            self._pending_force = False
            self._request_report(True)
        else:
            self._refresh_timer = self.set_timer(
                DEFAULT_TTL_SECONDS + random.uniform(0, 30), self._request_report
            )

    @classmethod
    def _render_report(cls, report: QuotaReport, now: datetime | None = None) -> Text:
        now = now or datetime.now(timezone.utc)
        rendered = Text()
        label_width = max((len(quota.label) for quota in report.quotas), default=0) + 2
        if report.quotas:
            rendered.append_text(cls._render_header(label_width))
        for quota in report.quotas:
            rendered.append("\n")
            rendered.append_text(cls._render_quota(quota, label_width, now))
        if report.errors:
            if report.quotas:
                rendered.append("\n\n")
            for index, error in enumerate(report.errors):
                if index:
                    rendered.append("\n")
                rendered.append(f"! {error}", style="#ffcb6b")
        if not report.quotas and not report.errors:
            rendered.append("No quota information is available.", style="#8492a0")
        return rendered

    @classmethod
    def _render_header(cls, label_width: int) -> Text:
        """Name the value columns once so the rows can stay bare numbers."""
        header = Text(" " * (label_width + cls.BAR_WIDTH), style="#8492a0")
        header.append("used".rjust(cls.USED_WIDTH), style="#8492a0")
        header.append("pace".rjust(cls.PACE_WIDTH), style="#8492a0")
        header.append("left".rjust(cls.RESET_WIDTH), style="#8492a0")
        return header

    @classmethod
    def _render_quota(
        cls, quota: QuotaWindow, label_width: int, now: datetime
    ) -> Text:
        used = max(0.0, min(100.0, quota.used_percent))
        used_cells = round(used / 100 * cls.BAR_WIDTH)
        if used >= 90:
            used_color = "#ff6b6b"
        elif used >= 70:
            used_color = "#ffcb6b"
        else:
            used_color = "#7ec97e"
        row = Text()
        row.append(quota.label.ljust(label_width))
        pace = quota.pace_percent(now)
        if pace is None:
            row.append("█" * used_cells, style=used_color)
            row.append("░" * (cls.BAR_WIDTH - used_cells), style=cls.TRACK_COLOR)
        else:
            pace_cells = round(pace / 100 * cls.BAR_WIDTH)
            row.append_text(cls._render_split_bar(used_cells, used_color, pace_cells))
        row.append(f"{used:.0f}%".rjust(cls.USED_WIDTH))
        if pace is None:
            row.append(" " * cls.PACE_WIDTH)
        else:
            row.append(
                f"{used - pace:+.0f}%".rjust(cls.PACE_WIDTH), style=cls.PACE_COLOR
            )
        countdown = "" if quota.resets_at is None else cls._reset_countdown(quota, now)
        row.append(countdown.rjust(cls.RESET_WIDTH), style="#8492a0")
        return row

    @staticmethod
    def _reset_countdown(quota: QuotaWindow, now: datetime) -> str:
        """Time left in the window, coarse for long windows and fine for short.

        A weekly window only needs hours to be actionable, while the minutes
        matter on a 5h one; the window length picks the granularity so a fresh
        weekly reset does not read as a wall of minutes.
        """
        remaining = int((quota.resets_at - now).total_seconds())
        if remaining <= 0:
            return "now"
        coarse = (
            quota.window_seconds >= 86400
            if quota.window_seconds
            else remaining >= 86400
        )
        if coarse:
            days, hours = divmod(remaining // 3600, 24)
            return f"{days}d{hours}h" if days else f"{hours}h"
        hours, minutes = divmod(remaining // 60, 60)
        return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"

    @classmethod
    def _render_split_bar(
        cls, used_cells: int, used_color: str, pace_cells: int
    ) -> Text:
        """Stack the usage and pacing bars into one row of half-block cells.

        Every cell is an upper half block, so its foreground draws the usage
        bar and its background draws the pacing bar underneath. The two bars
        share a left edge and a scale, making over- and under-pacing readable
        as the overhang of one half past the other.
        """
        both = min(used_cells, pace_cells)
        longer = max(used_cells, pace_cells)
        overhang_color = (
            f"{used_color} on {cls.TRACK_COLOR}"
            if used_cells > pace_cells
            else f"{cls.TRACK_COLOR} on {cls.PACE_COLOR}"
        )
        bar = Text()
        bar.append("▀" * both, style=f"{used_color} on {cls.PACE_COLOR}")
        bar.append("▀" * (longer - both), style=overhang_color)
        empty = f"{cls.TRACK_COLOR} on {cls.TRACK_COLOR}"
        bar.append("▀" * (cls.BAR_WIDTH - longer), style=empty)
        return bar

    def action_close(self) -> None:
        self._quota_closed = True
        self._pending_force = False
        self._stop_refresh_timer()
        self.dismiss()

    def action_refresh(self) -> None:
        self._request_report(True)


class DemoService(AgentmonService):
    def watch_runs(self, on_change, stop) -> None:
        return None

    def runs(self) -> list[AgentRun]:
        parent = self.repo.root.parent
        return [
            AgentRun(
                "%1", "0:3.0", "auth-cleanup", "working", "codex",
                parent / "auth-cleanup",
                "Fix authentication cleanup behavior and add regression tests.",
                "dirty",
                session_id="019f6c99-2762-7dc2-9d93-a8a4b48a3a5e",
                repository=self.repo,
            ),
            AgentRun(
                "%2", "0:4.0", "flaky-test", "blocked", "claude",
                parent / "flaky-test",
                "Investigate the intermittent timeout in the integration suite.",
                "unmerged",
                session_id="874085ab-4a17-4018-ac19-f0381bb7940a",
                repository=self.repo,
            ),
            AgentRun(
                "%3", "0:5.0", "docs-pass", "idle", "codex",
                parent / "docs-pass",
                "Refresh contributor documentation for the new API.",
                "merged",
                session_id="019f6c99-2762-7dc2-9d93-a8a4b48a3a5f",
                repository=self.repo,
            ),
        ]

    def select_window(self, run: AgentRun) -> None:
        return None

    def recent_finished(self, active: list[AgentRun], limit: int = 5) -> list[AgentRun]:
        return [
            AgentRun(
                "finished:/demo/old-fix", "0:", "old-fix", "exited", "finished",
                self.repo.root.parent / "old-fix",
                "Fix the parser edge case found during review.",
                "merged",
                repository=self.repo,
            )
        ]

    def open_shell_window(self, run: AgentRun) -> str:
        return "0:6"

    def run_transcript(self, run: AgentRun) -> Transcript:
        if not run.session_id:
            raise TranscriptError("Demo run has no active agent session")
        messages = {
            "auth-cleanup": (
                TranscriptMessage(
                    "user",
                    "Fix authentication cleanup behavior and add regression tests.",
                    "2026-07-16T19:01:04Z",
                ),
                TranscriptMessage(
                    "user",
                    "Keep the existing CLI behavior unchanged while fixing it.",
                    "2026-07-16T19:18:31Z",
                ),
            ),
            "flaky-test": (
                TranscriptMessage(
                    "user",
                    "Investigate the intermittent timeout in the integration suite.",
                    "2026-07-16T18:42:10Z",
                ),
                TranscriptMessage(
                    "user",
                    "The last failure was in test_reconnect_after_heartbeat.",
                    "2026-07-16T19:06:55Z",
                ),
            ),
            "docs-pass": (
                TranscriptMessage(
                    "user",
                    "Refresh contributor documentation for the new API.",
                    "2026-07-16T17:55:22Z",
                ),
            ),
        }.get(run.branch, ())
        return Transcript(
            session_id=run.session_id,
            rollout_path=Path(f"/demo/transcripts/{run.session_id}.jsonl"),
            messages=messages,
        )

    def cleanup_worktree(self, run: AgentRun) -> None:
        return None

    DEMO_INSTRUCTIONS = {
        "auth-cleanup": "Fix authentication cleanup behavior and add regression tests.\n",
        "flaky-test": "Investigate the intermittent timeout in the integration suite.\n",
        "docs-pass": "Refresh contributor documentation for the new API.\n",
        "old-fix": "Fix the parser edge case found during review.\n",
    }

    def worktree_overview(self, run: AgentRun) -> WorktreeOverview:
        instruction = self.DEMO_INSTRUCTIONS.get(run.branch)
        return WorktreeOverview(
            worktree=run.worktree,
            branch=run.branch,
            base_branch=self.repo.branch,
            merged=run.worktree_state == "merged",
            has_instruction=instruction is not None,
            instruction_text=instruction or "",
            instruction_commit="a1b2c3d" if instruction is not None else None,
            committed_changes=True,
            dirty=run.worktree_state == "dirty",
        )

class DemoQuotaService(QuotaService):
    def report(self, *, force: bool = False) -> QuotaReport:
        now = datetime.now(timezone.utc)
        week = 7 * 86400.0
        session = 5 * 3600.0
        return QuotaReport(
            fetched_at=now,
            quotas=(
                # Reset times are spread across each window so the demo shows
                # both over-pacing and under-pacing rows.
                QuotaWindow(
                    "codex", "Codex weekly", 37.0, now + timedelta(days=5), week
                ),
                QuotaWindow(
                    "codex", "Codex 5h", 12.0, now + timedelta(hours=4), session
                ),
                QuotaWindow(
                    "claude", "Claude 5h", 41.0, now + timedelta(hours=2), session
                ),
                QuotaWindow(
                    "claude", "Claude weekly", 9.0, now + timedelta(days=3.5), week
                ),
                QuotaWindow(
                    "claude", "Fable weekly", 14.0, now + timedelta(days=1), week
                ),
                QuotaWindow(
                    "antigravity", "Gemini weekly", 19.0, now + timedelta(days=6), week
                ),
                QuotaWindow(
                    "antigravity", "Gemini 5h", 0.0, now + timedelta(hours=4.5), session
                ),
            ),
        )


class AgentmonApp(App):
    CSS = """
    Screen { background: #101419; color: #d7dde5; }
    #title { height: 3; padding: 1 2; background: #263442; color: #ffffff; text-style: bold; }
    #dashboard-main { height: 1fr; }
    #runs-pane { width: RUNS_PANE_FRfr; }
    #runs { height: 1fr; margin: 0 1; border: tall #557086; }
    #runs > .datatable--header { background: #263442; color: #b9dcf5; text-style: bold; }
    #runs > .datatable--cursor { background: #315b76; color: #ffffff; }
    #notice { height: auto; max-height: 3; padding: 0 2; color: #ffcb6b; }
    #transcript-pane { width: TRANSCRIPT_PANE_FRfr; margin: 0 1 0 0; border: tall #557086; }
    #transcript-title { height: 3; padding: 1 2; background: #263442;
                        color: #b9dcf5; text-style: bold; }
    #transcript-meta { height: auto; min-height: 3; padding: 1 2; color: #8ec8ff; }
    #transcript-scroll { height: 1fr; overflow: hidden; }
    #transcript-content { height: auto; padding: 1 2 2 2; }
    #dialog { width: 72; height: auto; max-width: 100%; max-height: 100%;
              margin: 1 2; padding: 1 2;
              border: round #6aa9d8; background: #171d24; }
    .dialog-title { height: 2; color: #ffffff; text-style: bold; }
    .buttons { height: 3; margin-top: 0; }
    .buttons Button { margin-right: 1; }
    .buttons .dialog-action { background: #293440; color: #d7dde5; text-style: none; }
    .buttons .dialog-action:hover { background: #354555; color: #ffffff; }
    .buttons .dialog-action:focus { background: #3f8fc4; color: #ffffff; text-style: bold; }
    .buttons .dialog-action:disabled { background: #202832; color: #65717e; text-style: none; }
    #error { min-height: 1; }
    .key-hint { height: 2; padding-top: 1; color: #8492a0; }
    Footer { background: #202832; }
    QuotaScreen { align: center middle; background: transparent; }
    #quota-dialog { width: 88; height: auto; max-width: 100%; max-height: 100%;
                    padding: 1 2; border: round #6aa9d8; background: #171d24; }
    #quota-content { min-height: 5; text-wrap: nowrap; text-overflow: ellipsis; }
    #quota-legend { margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #quota-meta { margin-top: 1; }
    """.replace("RUNS_PANE_FR", str(RUNS_PANE_FRACTION)).replace(
        "TRANSCRIPT_PANE_FR", str(TRANSCRIPT_PANE_FRACTION)
    )

    def __init__(
        self,
        service: AgentmonService,
        *,
        startup_warning: str = "",
        quota_service: QuotaService | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.quota_service = quota_service or QuotaService()
        self.startup_warning = startup_warning
        self.runs: list[AgentRun] = []

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen())

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Subscription quotas",
            "Toggle the Codex/Claude quota usage dialog",
            self.action_toggle_quotas,
        )
        if isinstance(screen, DashboardScreen):
            yield SystemCommand(
                "Open window",
                "Switch to the selected run's tiri window",
                screen.action_open_run,
            )
            yield SystemCommand(
                "New shell at CWD",
                "Open a shell window at the selected run's directory",
                screen.action_new_window,
            )
            yield SystemCommand(
                "Clean up worktree",
                "Remove the selected finished, merged worktree",
                screen.action_cleanup,
            )
            yield SystemCommand(
                "Refresh",
                "Reload runs and worktree status now",
                screen.action_reload,
            )
            yield SystemCommand(
                "Full transcript",
                "Open the selected run's complete transcript in less",
                screen.action_view_transcript,
            )

    def action_toggle_quotas(self) -> None:
        for existing in self.screen_stack:
            if isinstance(existing, QuotaScreen):
                existing.action_close()
                return
        self.push_screen(QuotaScreen())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentmon")
    parser.add_argument("--socket", help="tiri socket path (otherwise auto-detected)")
    parser.add_argument("--tmux", default="tmux", help="tmux-compatible client")
    parser.add_argument("--demo", action="store_true", help="show sample data without changing anything")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.demo:
        try:
            repo = discover_repository()
        except CommandError:
            root = Path.cwd().resolve()
            repo = Repository(root=root, common_dir=root / ".git", branch="demo")
        socket = args.socket or "/tmp/agentmon-demo.sock"
        service = DemoService(repo, socket=socket, tmux=args.tmux)
        warning = ""
    else:
        try:
            context = discover_context(socket=args.socket, tmux=args.tmux)
        except CommandError as exc:
            print(f"agentmon: {exc}")
            return 1
        service = context.service()
        warning = context.warning
    AgentmonApp(
        service,
        startup_warning=warning,
        quota_service=DemoQuotaService() if args.demo else None,
    ).run()
    return 0
