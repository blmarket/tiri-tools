from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentmon.looper import (
    DONE_STATES,
    PRESETS,
    Looper,
    LooperConfig,
    format_duration,
    main,
    pacing_decision,
    pacing_ready_at,
)
from agentmon.model import Repository
from agentmon.quota import QuotaReport, QuotaWindow
from agentmon.services import CommandError, PaneStatus


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
WEEK = 7 * 86400.0


def codex_window(used: float, *, elapsed_days: float = 3.5) -> QuotaWindow:
    """A weekly Codex window `elapsed_days` into its span."""
    return QuotaWindow(
        provider="codex",
        label="Codex weekly",
        used_percent=used,
        resets_at=NOW + timedelta(days=7 - elapsed_days),
        window_seconds=WEEK,
    )


def report(*quotas: QuotaWindow, errors: tuple[str, ...] = ()) -> QuotaReport:
    return QuotaReport(fetched_at=NOW, quotas=quotas, errors=errors)


class FakeQuotaService:
    def __init__(self, *reports: QuotaReport) -> None:
        self.reports = list(reports)
        self.calls = 0

    def report(self, *, force: bool = False) -> QuotaReport:
        self.calls += 1
        # The last report stands in for every later poll.
        return self.reports[min(self.calls - 1, len(self.reports) - 1)]


class FakeService:
    """A service whose panes follow a script, one outcome per run."""

    def __init__(
        self,
        *outcomes: str | None,
        exit_clean: bool = True,
        startup: str | None = "working",
    ) -> None:
        self.outcomes = list(outcomes)
        self.exit_clean = exit_clean
        self.startup = startup
        self.splits: list[dict] = []
        self.done_timeouts: list[float | None] = []
        self.exited: list[str] = []
        self.killed: list[str] = []
        self.commits: list[tuple[Path, str]] = []
        self.commit_result: str | None = "abc1234"

    def split_agent_pane(self, **kwargs: object) -> str:
        self.splits.append(kwargs)
        return f"%{len(self.splits)}"

    def wait_for_pane_state(self, pane, states, *, timeout=None, poll=2.0):
        if "working" in states:
            return self.startup
        assert tuple(states) == DONE_STATES
        self.done_timeouts.append(timeout)
        return self.outcomes.pop(0)

    def pane_status(self, pane: str) -> PaneStatus | None:
        return PaneStatus(pane, "codex", "working")

    def exit_agent_pane(self, pane: str, **kwargs: object) -> bool:
        self.exited.append(pane)
        return self.exit_clean

    def kill_agent_pane(self, pane: str) -> None:
        self.killed.append(pane)

    def commit_all_changes(self, worktree: Path, message: str) -> str | None:
        self.commits.append((worktree, message))
        return self.commit_result


def build(
    service: FakeService,
    quota_service: FakeQuotaService,
    tmp_path: Path,
    **overrides: object,
) -> tuple[Looper, list[str], list[float]]:
    lines: list[str] = []
    naps: list[float] = []
    looper = Looper(
        service,
        worktree=tmp_path,
        pane="%0",
        instruction_file=tmp_path / "prompt.md",
        config=LooperConfig(**overrides),
        quota_service=quota_service,
        log=lines.append,
        sleep=naps.append,
        now=lambda: NOW,
    )
    return looper, lines, naps


def test_pacing_ready_at_is_the_reset_for_a_spent_window() -> None:
    window = codex_window(100.0)
    assert pacing_ready_at(window) == window.resets_at


def test_pacing_ready_at_is_now_for_a_window_burning_exactly_on_pace() -> None:
    # Half spent, half elapsed: the catch-up moment is the present.
    assert pacing_ready_at(codex_window(50.0)) == NOW


def test_pacing_ready_at_needs_a_dated_window() -> None:
    assert pacing_ready_at(QuotaWindow("codex", "Codex", 10.0)) is None


def test_pacing_allows_a_run_while_spending_trails_the_clock() -> None:
    decision = pacing_decision(report(codex_window(40.0)), provider="codex", now=NOW)

    assert decision.ready
    assert decision.wait_until is None
    assert decision.reason == "Codex weekly 40% used / 50% elapsed"


def test_pacing_holds_a_run_back_until_an_even_burn_catches_up() -> None:
    decision = pacing_decision(report(codex_window(60.0)), provider="codex", now=NOW)

    assert not decision.ready
    assert decision.wait_until == NOW + timedelta(hours=16, minutes=48)


def test_pacing_waits_for_the_last_of_several_windows() -> None:
    weekly = codex_window(55.0)
    # Further over pace, yet caught up sooner: a short window recovers fast.
    session = QuotaWindow(
        provider="codex",
        label="Codex 5h",
        used_percent=90.0,
        resets_at=NOW + timedelta(hours=4),
        window_seconds=5 * 3600.0,
    )

    decision = pacing_decision(report(weekly, session), provider="codex", now=NOW)

    assert not decision.ready
    assert pacing_ready_at(session) < pacing_ready_at(weekly)
    assert decision.wait_until == pacing_ready_at(weekly)


def test_pacing_ignores_windows_belonging_to_another_provider() -> None:
    claude = QuotaWindow(
        provider="claude",
        label="Claude weekly",
        used_percent=99.0,
        resets_at=NOW + timedelta(days=6),
        window_seconds=WEEK,
    )

    decision = pacing_decision(
        report(claude, codex_window(10.0)), provider="codex", now=NOW
    )

    assert decision.ready
    assert "Claude" not in decision.reason


def test_pacing_does_not_stall_when_the_provider_reported_nothing() -> None:
    decision = pacing_decision(
        report(errors=("Codex auth unavailable",)), provider="codex", now=NOW
    )

    assert decision.ready
    assert decision.reason == "no codex quota reported"


def test_format_duration_scales_with_the_wait() -> None:
    assert format_duration(45) == "45s"
    assert format_duration(90) == "1m30s"
    assert format_duration(3 * 3600 + 300) == "3h05m"
    assert format_duration(2 * 86400 + 3600) == "2d01h"


def test_loop_runs_the_prompt_and_commits_what_it_produced(tmp_path: Path) -> None:
    service = FakeService("idle", "idle")
    looper, lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path, max_runs=2
    )

    assert looper.run() == 0
    assert len(service.splits) == 2
    assert service.exited == ["%1", "%2"]
    assert [message for _worktree, message in service.commits] == [
        "looper: auto-commit run 1",
        "looper: auto-commit run 2",
    ]
    assert service.splits[0]["agent"] == "codex"
    assert service.splits[0]["model"] == "gpt-5.6-luna"
    assert service.splits[0]["effort"] == "max"
    assert service.splits[0]["target"] == "%0"
    # The status that follows the active pane should describe the run, so the
    # agent pane takes the focus rather than the pane looper logs into.
    assert service.splits[0]["focus"] is True
    # An agent that finished is at its prompt, so it is asked rather than killed.
    assert service.killed == []
    assert any("committed abc1234" in line for line in lines)


def test_loop_stops_and_keeps_the_pane_when_the_agent_wants_a_human(
    tmp_path: Path,
) -> None:
    service = FakeService("blocked")
    looper, lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path, max_runs=5
    )

    assert looper.run() == 0
    assert len(service.splits) == 1
    # The pane stays up for the user, and nothing is committed behind their back.
    assert service.exited == []
    assert service.commits == []
    assert any("waiting on you" in line for line in lines)


def test_loop_harvests_an_agent_that_closed_its_own_pane(tmp_path: Path) -> None:
    service = FakeService("exited")
    looper, _lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path, max_runs=1
    )

    assert looper.run() == 0
    assert service.exited == []
    assert len(service.commits) == 1


def test_a_pane_that_dies_before_the_agent_starts_stops_the_loop(
    tmp_path: Path,
) -> None:
    """A dev shell that will not build fails every run, so stop on the first."""
    service = FakeService(startup="exited")
    looper, lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path
    )

    assert looper.run() == 1
    assert len(service.splits) == 1
    assert service.commits == []
    assert any("before codex started" in line for line in lines)


def test_a_run_is_capped_at_two_hours_by_default(tmp_path: Path) -> None:
    service = FakeService("idle")
    looper, _lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path, max_runs=1
    )

    assert looper.run() == 0
    assert service.done_timeouts == [7200.0]


def test_a_run_timeout_of_zero_waits_for_the_agent_forever(tmp_path: Path) -> None:
    service = FakeService("idle")
    looper, _lines, _naps = build(
        service,
        FakeQuotaService(report(codex_window(10.0))),
        tmp_path,
        max_runs=1,
        run_timeout=0.0,
    )

    assert looper.run() == 0
    assert service.done_timeouts == [None]


def test_loop_ends_a_run_that_outstays_the_run_timeout(tmp_path: Path) -> None:
    service = FakeService(None)
    looper, lines, _naps = build(
        service,
        FakeQuotaService(report(codex_window(10.0))),
        tmp_path,
        max_runs=1,
        run_timeout=1800.0,
    )

    assert looper.run() == 0
    # Mid-turn there is no point asking, and what it leaves says as much.
    assert service.exited == []
    assert service.killed == ["%1"]
    assert [message for _worktree, message in service.commits] == [
        "looper: partial work from timed-out run 1"
    ]
    assert any("still going after 30m00s" in line for line in lines)


def test_loop_keeps_changes_when_committing_is_turned_off(tmp_path: Path) -> None:
    service = FakeService("idle")
    looper, _lines, _naps = build(
        service,
        FakeQuotaService(report(codex_window(10.0))),
        tmp_path,
        max_runs=1,
        commit=False,
    )

    assert looper.run() == 0
    assert service.commits == []


def test_loop_reports_a_failed_auto_commit_without_stopping(tmp_path: Path) -> None:
    service = FakeService("idle", "idle")

    def refuse(worktree: Path, message: str) -> str | None:
        raise CommandError("git commit: nothing to do")

    service.commit_all_changes = refuse
    looper, lines, _naps = build(
        service, FakeQuotaService(report(codex_window(10.0))), tmp_path, max_runs=2
    )

    assert looper.run() == 0
    assert len(service.splits) == 2
    assert any("auto-commit failed" in line for line in lines)


def test_loop_sleeps_until_over_pacing_clears_before_starting_a_run(
    tmp_path: Path,
) -> None:
    quota_service = FakeQuotaService(
        report(codex_window(60.0)), report(codex_window(40.0))
    )
    service = FakeService("idle")
    looper, lines, naps = build(
        service, quota_service, tmp_path, max_runs=1, quota_poll_seconds=300.0
    )

    assert looper.run() == 0
    # The wait is served in bounded naps so the next poll sees fresh usage.
    assert naps == [300.0]
    assert len(service.splits) == 1
    assert any("over pace" in line for line in lines)
    assert any("on pace" in line for line in lines)


def test_loop_naps_no_longer_than_the_catch_up_itself(tmp_path: Path) -> None:
    quota_service = FakeQuotaService(
        # 51% spent at the halfway mark: a little over a minute behind.
        report(codex_window(50.01)),
        report(codex_window(40.0)),
    )
    looper, _lines, naps = build(
        FakeService("idle"), quota_service, tmp_path, max_runs=1
    )

    assert looper.run() == 0
    assert naps and naps[0] < 300.0


def test_loop_runs_its_budget_back_to_back_when_pacing_is_ignored(
    tmp_path: Path,
) -> None:
    # A spent window would otherwise hold every run back until its reset.
    quota_service = FakeQuotaService(report(codex_window(100.0)))
    service = FakeService("idle", "idle")
    looper, lines, naps = build(
        service, quota_service, tmp_path, max_runs=2, ignore_pacing=True
    )

    assert looper.run() == 0
    assert len(service.splits) == 2
    assert naps == []
    assert quota_service.calls == 0
    assert any("pacing ignored" in line for line in lines)


def test_loop_names_a_quota_lookup_failure_once(tmp_path: Path) -> None:
    quota_service = FakeQuotaService(
        report(codex_window(60.0), errors=("Codex auth unavailable",)),
        report(codex_window(10.0), errors=("Codex auth unavailable",)),
    )
    looper, lines, _naps = build(
        FakeService("idle"), quota_service, tmp_path, max_runs=1
    )

    assert looper.run() == 0
    assert sum("Codex auth unavailable" in line for line in lines) == 1


def test_preset_decides_which_agent_the_loop_starts(tmp_path: Path) -> None:
    preset = PRESETS["agy"]
    service = FakeService("idle")
    looper, _lines, _naps = build(
        service,
        # Nothing reports antigravity quota, so the loop runs unpaced.
        FakeQuotaService(report(codex_window(90.0, elapsed_days=0.5))),
        tmp_path,
        agent=preset.agent,
        model=preset.model,
        effort=preset.effort,
        provider=preset.provider,
        max_runs=1,
    )

    assert looper.run() == 0
    assert service.splits[0]["agent"] == "agy"
    assert service.splits[0]["model"] == "gemini-3.7-flash"
    assert service.splits[0]["effort"] == "high"


def test_main_plumbs_the_chosen_preset_into_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module,
        "discover_context",
        lambda **_kwargs: _FakeContext(tmp_path),
    )
    configs: list[LooperConfig] = []
    monkeypatch.setattr(
        looper_module, "Looper", lambda *args, **kwargs: _FakeLooper(configs, **kwargs)
    )

    assert main(["--preset", "agy"]) == 0
    assert configs == [
        LooperConfig(agent="agy", model="gemini-3.7-flash", effort="high",
                     provider="antigravity")
    ]


def test_main_takes_the_dev_shell_when_the_worktree_has_a_flake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    (tmp_path / "flake.nix").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(looper_module, "devshell_available", lambda _path: True)
    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module, "discover_context", lambda **_kwargs: _FakeContext(tmp_path)
    )
    configs: list[LooperConfig] = []
    monkeypatch.setattr(
        looper_module, "Looper", lambda *args, **kwargs: _FakeLooper(configs, **kwargs)
    )

    assert main([]) == 0
    assert configs[0].devshell is True

    configs.clear()
    assert main(["--no-devshell"]) == 0
    assert configs[0].devshell is False


def test_main_leaves_the_dev_shell_alone_without_a_flake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    monkeypatch.setattr(looper_module, "devshell_available", lambda _path: False)
    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module, "discover_context", lambda **_kwargs: _FakeContext(tmp_path)
    )
    configs: list[LooperConfig] = []
    monkeypatch.setattr(
        looper_module, "Looper", lambda *args, **kwargs: _FakeLooper(configs, **kwargs)
    )

    assert main([]) == 0
    assert configs[0].devshell is False


def test_claude_preset_runs_opus_and_is_paced_by_claude_quota(
    tmp_path: Path,
) -> None:
    preset = PRESETS["claude"]
    service = FakeService("idle")
    spent_claude = QuotaWindow(
        provider="claude",
        label="Claude weekly",
        used_percent=100.0,
        resets_at=NOW + timedelta(days=3.5),
        window_seconds=WEEK,
    )
    quota_service = FakeQuotaService(
        report(spent_claude), report(codex_window(0.0))
    )
    looper, lines, naps = build(
        service,
        quota_service,
        tmp_path,
        agent=preset.agent,
        model=preset.model,
        effort=preset.effort,
        provider=preset.provider,
        max_runs=1,
    )

    assert looper.run() == 0
    # The spent Claude window holds the first poll back; a report without it
    # leaves the loop unpaced and the run starts.
    assert naps == [300.0]
    assert any("over pace: Claude weekly" in line for line in lines)
    assert service.splits[0]["agent"] == "claude"
    assert service.splits[0]["model"] == "claude-opus-5"
    assert service.splits[0]["effort"] == "default"


def test_main_plumbs_the_claude_preset_into_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module,
        "discover_context",
        lambda **_kwargs: _FakeContext(tmp_path),
    )
    configs: list[LooperConfig] = []
    monkeypatch.setattr(
        looper_module, "Looper", lambda *args, **kwargs: _FakeLooper(configs, **kwargs)
    )

    assert main(["--preset", "claude"]) == 0
    assert configs == [
        LooperConfig(agent="claude", model="claude-opus-5", effort="default",
                     provider="claude")
    ]


def test_main_plumbs_the_sol_preset_into_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module,
        "discover_context",
        lambda **_kwargs: _FakeContext(tmp_path),
    )
    configs: list[LooperConfig] = []
    monkeypatch.setattr(
        looper_module, "Looper", lambda *args, **kwargs: _FakeLooper(configs, **kwargs)
    )

    assert main(["--preset", "sol"]) == 0
    assert configs == [
        LooperConfig(agent="codex", model="gpt-5.6-sol", effort="high",
                     provider="codex")
    ]


def test_sol_preset_runs_codex_and_is_paced_by_codex_quota(tmp_path: Path) -> None:
    preset = PRESETS["sol"]
    service = FakeService("idle")
    quota_service = FakeQuotaService(
        report(codex_window(100.0, elapsed_days=0.5)), report(codex_window(0.0))
    )
    looper, lines, naps = build(
        service,
        quota_service,
        tmp_path,
        agent=preset.agent,
        model=preset.model,
        effort=preset.effort,
        provider=preset.provider,
        max_runs=1,
    )

    assert looper.run() == 0
    assert naps == [300.0]
    assert any("over pace" in line for line in lines)
    assert service.splits[0]["agent"] == "codex"
    assert service.splits[0]["model"] == "gpt-5.6-sol"
    assert service.splits[0]["effort"] == "high"


def test_main_rejects_a_preset_that_does_not_exist() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--preset", "nope"])
    assert caught.value.code == 2


def test_main_refuses_to_start_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin(""))

    assert main([]) == 2
    assert "standard input" in capsys.readouterr().err


def test_main_hands_the_named_prompt_file_to_every_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import looper as looper_module

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the thing.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", _Stdin("piped instead\n"))
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(
        looper_module, "discover_context", lambda **_kwargs: _FakeContext(tmp_path)
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        looper_module,
        "Looper",
        lambda *args, **kwargs: _FakeLooper([], calls, **kwargs),
    )

    # The file itself is what the agent is started on, so a run rewriting it
    # changes what the next run reads; the named file also wins over stdin.
    assert main(["prompt.md"]) == 0
    assert calls[0]["instruction_file"] == prompt.resolve()


def test_main_refuses_a_prompt_file_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))

    assert main([str(tmp_path / "missing.md")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_main_refuses_an_empty_prompt_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("   \n", encoding="utf-8")

    assert main([str(prompt)]) == 2
    assert "is empty" in capsys.readouterr().err


def test_main_refuses_to_start_outside_a_pane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin("Do the thing.\n"))
    monkeypatch.delenv("TMUX_PANE", raising=False)

    assert main([]) == 2
    assert "tiri pane" in capsys.readouterr().err


class _Stdin:
    def __init__(self, text: str) -> None:
        self.text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self.text


class _FakeContext:
    """The bits of `discover_context` main() reaches for."""

    def __init__(self, root: Path) -> None:
        self.warning = ""
        self.repository = Repository(root, root / ".git", "main")

    def service(self) -> object:
        return object()


class _FakeLooper:
    def __init__(
        self,
        configs: list[LooperConfig],
        calls: list[dict[str, object]] | None = None,
        **kwargs: object,
    ) -> None:
        configs.append(kwargs["config"])
        if calls is not None:
            calls.append(kwargs)

    def log(self, message: str) -> None:
        pass

    def run(self) -> int:
        return 0
