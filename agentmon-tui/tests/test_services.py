from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentmon.model import AgentRun, LaunchDraft, Repository
from agentmon.services import (
    AgentmonService,
    CommandError,
    PaneStatus,
    SocketSelection,
    discover_repository,
    discover_socket,
    is_claude_rate_limit_options_dialog,
    trust_agy_workspace,
)
from agentmon.transcript import Transcript


_ENVIRONMENT_PRELUDE = re.compile(
    r"^set -a; \. (?P<spool>\S+); set \+a; rm -f (?P=spool); "
)


@pytest.fixture(autouse=True)
def _spool_into_the_test_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep spooled environments out of the developer's own temp directory.

    They are a copy of whatever the environment running the suite holds.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def consume_environment(command: str) -> tuple[str, str]:
    """Do to a pane command what the pane does: read the environment, remove it.

    Returns the spooled environment script and the command left for the agent.
    """
    match = _ENVIRONMENT_PRELUDE.match(command)
    assert match is not None, command
    spool = Path(match.group("spool"))
    script = spool.read_text(encoding="utf-8")
    spool.unlink()
    return script, command[match.end():]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Agentmon Test")
    git(root, "config", "user.email", "agentmon@example.invalid")
    (root / "README.md").write_text("test\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "initial")
    return discover_repository(root)


def test_discovers_repository(repository: Repository) -> None:
    assert repository.root.name == "project"
    assert repository.branch == "main"
    assert repository.common_dir.name == ".git"


def test_service_without_startup_repository_discovers_repositories_from_panes(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    non_git = repository.root.parent / "scratch"
    rows = (
        f"%1\tdev:1.0\tscratch\t\t\t{non_git}\t\t1\tzsh\n"
        f"%2\tdev:2.0\tproject\tcodex\tworking\t"
        f"{repository.root}\tsession-id\t1\tcodex\n"
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(
        service,
        "_git_common_dir",
        lambda cwd: repository.common_dir if cwd == repository.root else None,
    )
    monkeypatch.setattr(
        service,
        "_repository_for_worktree",
        lambda _cwd, _common_dir: repository,
    )
    monkeypatch.setattr(service, "for_repository", lambda _repo: service)
    monkeypatch.setattr(service, "_branch_at", lambda _cwd: "main")
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    runs = service.runs()

    assert service.repo is None
    assert [run.repository for run in runs] == [None, repository]
    assert [run.worktree_state for run in runs] == ["not-git", "dirty"]


def test_service_without_any_repository_can_collect_non_git_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    rows = f"%1\tdev:1.0\tscratch\t\t\t{tmp_path}\t\t1\tzsh\n"
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: None)

    active = service.runs()

    assert len(active) == 1
    assert active[0].repository is None
    assert service.recent_finished_all(active) == []


def test_runs_list_only_the_panes_of_agentmon_own_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A window linked into a second session must not be listed twice."""
    from agentmon import services

    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, f"%1\tdev:1.0\tscratch\t\t\t{tmp_path}\t\t1\tzsh\n", ""
        )

    service = AgentmonService(None, socket="/tmp/tiri.sock", session="$0")
    monkeypatch.setattr(services, "_run", run)
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: None)

    assert len(service.runs()) == 1
    assert calls[0][3:7] == ["list-panes", "-s", "-t", "$0"]

    calls.clear()
    service.pane_status("%1")
    assert calls[0][3:7] == ["list-panes", "-s", "-t", "$0"]


def test_runs_list_the_whole_server_when_agentmon_has_no_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, f"%1\tdev:1.0\tscratch\t\t\t{tmp_path}\t\t1\tzsh\n", ""
        )

    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(services, "_run", run)
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: None)

    service.runs()

    assert calls[0][3:5] == ["list-panes", "-a"]


def test_agent_windows_open_in_the_session_agentmon_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "3\n", "")

    monkeypatch.setattr(services, "_run", run)

    scoped = AgentmonService(None, socket="/tmp/tiri.sock", session="$2")
    assert scoped._open_agent_window(tmp_path, "run", "codex") == "3"
    assert "$2:" in calls[0]

    calls.clear()
    AgentmonService(None, socket="/tmp/tiri.sock")._open_agent_window(
        tmp_path, "run", "codex"
    )
    assert "0:" in calls[0]


def test_watches_only_agentmon_own_session_while_it_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AgentmonService(None, socket="/tmp/tiri.sock", session="$0")
    monkeypatch.setattr(service, "_session_ids", lambda: {"$0", "$1"})
    assert service._watched_sessions() == {"$0"}

    monkeypatch.setattr(service, "_session_ids", lambda: {"$1"})
    assert service._watched_sessions() == set()


def test_discovers_the_session_holding_agentmon_own_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon.services import discover_session

    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        "agentmon.services._run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "$3\n", ""),
    )
    assert discover_session("/tmp/tiri.sock") == "$3"

    # A pane that this server does not know about leaves agentmon unscoped.
    monkeypatch.setattr(
        "agentmon.services._run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "no pane"),
    )
    assert discover_session("/tmp/tiri.sock") is None

    monkeypatch.delenv("TMUX_PANE")
    assert discover_session("/tmp/tiri.sock") is None


def test_discovers_current_tiri_session_socket_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIRI_SOCKET", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/tiri,123,0")

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[3:6] == ["list-panes", "-a", "-F"]:
            return subprocess.CompletedProcess(args, 0, "none\n", "")
        return subprocess.CompletedProcess(args, 0, "0: 1 windows\n", "")

    monkeypatch.setattr("agentmon.services._run", run)

    selection = discover_socket()

    assert selection.path == "/tmp/tmux-1000/tiri"
    assert not selection.warning


def test_warns_when_selected_server_lacks_tiri_agent_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIRI_SOCKET", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[3:6] == ["list-panes", "-a", "-F"]:
            return subprocess.CompletedProcess(args, 0, "\n", "")
        return subprocess.CompletedProcess(args, 0, "0: 1 windows\n", "")

    monkeypatch.setattr("agentmon.services._run", run)

    selection = discover_socket()

    assert selection.path == "/tmp/tmux-1000/default"
    assert "does not expose tiri agent status" in selection.warning


def test_tiri_socket_takes_priority_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIRI_SOCKET", "/tmp/tiri.sock")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(
        "agentmon.services._run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "0: 1 windows\n", ""),
    )

    selection = discover_socket()

    assert selection.path == "/tmp/tiri.sock"
    assert not selection.warning


def test_socket_discovery_fails_with_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIRI_SOCKET", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(
        "agentmon.services._run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "no server"),
    )

    with pytest.raises(CommandError, match="use --socket PATH"):
        discover_socket(requested="/missing/tiri.sock")


def test_subscription_command_tracks_every_dashboard_field() -> None:
    from agentmon import services

    command = services._subscription_command()

    assert command.startswith("refresh-client -B 'agentmon:%*:")
    for field in (
        "pane_id",
        "session_name",
        "window_name",
        "pane_agent",
        "pane_agent_state",
        "pane_current_path",
        "pane_agent_session_id",
        "pane_active",
        "pane_current_command",
    ):
        assert f"#{{{field}}}" in command


@pytest.mark.parametrize(
    "line",
    (
        "%subscription-changed agentmon $0 @0 0 %0 : working\n",
        "%layout-change @0 tiled deadbeef deadbeef\n",
        "%sessions-changed\n",
        "%window-close @0\n",
    ),
)
def test_control_notifications_invalidate_runs(line: str) -> None:
    from agentmon import services

    assert services._run_change_notification(line)
    assert not services._run_change_notification("%output %0 ignored\n")


def test_watcher_subscribes_once_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentmon import services

    stop = threading.Event()
    selector_events: list[tuple[object, int, str]] = []

    class FakeSelector:
        def __init__(self) -> None:
            self.calls = 0

        def register(self, fileobj: object, events: int, data: str) -> None:
            selector_events.append((fileobj, events, data))

        def unregister(self, fileobj: object) -> None:
            selector_events[:] = [item for item in selector_events if item[0] is not fileobj]

        def select(self, timeout: float) -> list[tuple[object, int]]:
            assert timeout == 0.5
            self.calls += 1
            if self.calls == 1:
                key = SimpleNamespace(data="one")
                return [(key, services.selectors.EVENT_READ)]
            stop.set()
            return []

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, session: str, args: list[str]) -> None:
            self.session = session
            self.args = args
            self.stdin = io.BytesIO()
            line = (
                b"%subscription-changed agentmon $0 @0 0 %0 : working\n"
                if session == "one"
                else b""
            )
            read_fd, write_fd = os.pipe()
            os.write(write_fd, line)
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.command = b""

        def terminate(self) -> None:
            self.command = self.stdin.getvalue()
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    processes: list[FakeProcess] = []

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        assert kwargs["bufsize"] == 0
        process = FakeProcess(args[-1], args)
        processes.append(process)
        return process

    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(service, "_session_names", lambda: {"one", "two"})
    monkeypatch.setattr(services.selectors, "DefaultSelector", FakeSelector)
    monkeypatch.setattr(services.subprocess, "Popen", popen)
    changes: list[None] = []

    service.watch_runs(lambda: changes.append(None), stop)

    assert {process.session for process in processes} == {"one", "two"}
    assert all("no-output,ignore-size" in process.args for process in processes)
    assert all(
        process.command == services._subscription_command().encode()
        for process in processes
    )
    assert len(changes) == 1


def test_validates_and_derives_sibling_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    draft = service.validate_draft("feature/auth", "Do the work.\n")
    assert draft.branch == "feature/auth"
    assert draft.worktree == repository.root.parent / "feature-auth"


@pytest.mark.parametrize(
    ("branch", "prompt", "message"),
    [("", "hello", "branch"), ("bad branch", "hello", "branch"), ("valid", "", "instruction")],
)
def test_rejects_invalid_draft(
    repository: Repository, branch: str, prompt: str, message: str
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    with pytest.raises(ValueError, match=message):
        service.validate_draft(branch, prompt)


def test_rejects_overwrite_of_non_worktree_path(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = service.suggested_worktree("occupied")
    target.mkdir()

    with pytest.raises(ValueError, match="ERROR: Cannot overwrite non-worktree"):
        service.validate_draft("occupied", "Do the work.\n")


def test_allows_overwrite_of_registered_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = service.suggested_worktree("replace-me")
    git(repository.root, "worktree", "add", "-b", "old-run", str(target))

    draft = service.validate_draft("replace-me", "Do the work.\n")

    assert draft.worktree == target
    assert draft.overwrite_worktree


def test_rejects_overwrite_of_dirty_registered_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = service.suggested_worktree("dirty-replace")
    git(repository.root, "worktree", "add", "-b", "old-dirty-run", str(target))
    (target / "untracked.txt").write_text("keep me\n")

    with pytest.raises(ValueError, match="ERROR: Cannot overwrite dirty worktree"):
        service.validate_draft("dirty-replace", "Do the work.\n")


def test_allows_new_worktree_for_existing_branch(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    git(repository.root, "branch", "f5")

    draft = service.validate_draft("f5", "Continue the work.\n")

    assert draft.worktree == repository.root.parent / "f5"
    assert draft.existing_branch
    assert not draft.overwrite_worktree


def test_reads_prompt_history(repository: Repository) -> None:
    (repository.root / "prompt.md").write_text("Investigate the race.\n")
    git(repository.root, "add", "prompt.md")
    git(repository.root, "commit", "-m", "agentmon: add prompt for race")
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    history = service.prompt_history()
    assert history[0].subject == "agentmon: add prompt for race"
    assert history[0].prompt == "Investigate the race.\n"


def test_prompt_preview_uses_worktree_prompt(repository: Repository) -> None:
    (repository.root / "prompt.md").write_text("First line.\n\nMore details here.\n")
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    assert service._prompt_preview(repository.root) == "First line. More details here."


def test_runs_include_tiri_agent_session_id(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    session_id = "019f6c99-2762-7dc2-9d93-a8a4b48a3a5e"
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        row = (
            f"%3\tdev:4.0\tagent-session\tcodex\tworking\t"
            f"{repository.root}\t{session_id}\t1\tcodex\n"
        )
        return subprocess.CompletedProcess(args, 0, row, "")

    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(services, "_run", fake_run)
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: repository.common_dir)
    monkeypatch.setattr(service, "_branch_at", lambda _cwd: "agent-session")
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    runs = service.runs()

    assert len(runs) == 1
    assert runs[0].session_id == session_id
    assert "#{pane_agent_session_id}" in calls[0][-1]


def test_detects_claude_rate_limit_options_dialog() -> None:
    capture = """
    ❯ /rate-limit-options

    ──────────────────────────────────────────────────────────────────────────
      What do you want to do?

      ❯ 1. Stop and wait for limit to reset
        2. Upgrade your plan
        3. Upgrade to Team plan

      Enter to confirm · Esc to cancel
    """

    assert is_claude_rate_limit_options_dialog(capture)


@pytest.mark.parametrize(
    "capture",
    (
        "❯ /rate-limit-options\n",
        "What do you want to do?\n1. Upgrade your plan\n",
        "❯ /rate-limit-options\nWhat do you want to do?\n1. Upgrade your plan\n",
    ),
)
def test_rate_limit_detector_requires_the_complete_menu(capture: str) -> None:
    assert not is_claude_rate_limit_options_dialog(capture)


def test_auto_wait_if_rate_limited_rechecks_and_confirms_option_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    capture = (
        "❯ /rate-limit-options\n"
        "What do you want to do?\n"
        "❯ 1. Stop and wait for limit to reset\n"
    )

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        stdout = capture if "capture-pane" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    service = AgentmonService(None, socket="/tmp/tiri.sock")
    run = AgentRun(
        "window:0:2",
        "0:2",
        "claude-run",
        "blocked",
        "claude",
        Path("/tmp/run"),
        tmux_pane_id="%pane",
    )
    monkeypatch.setattr(services, "_run", fake_run)

    assert service.auto_wait_if_rate_limited(run)
    assert calls == [
        [
            "tmux",
            "-S",
            "/tmp/tiri.sock",
            "capture-pane",
            "-p",
            "-t",
            "%pane",
        ],
        [
            "tmux",
            "-S",
            "/tmp/tiri.sock",
            "send-keys",
            "-t",
            "%pane",
            "Home",
            "Enter",
        ]
    ]


def test_auto_wait_if_rate_limited_does_not_send_when_menu_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "❯ /rate-limit-options\n", "")

    service = AgentmonService(None, socket="/tmp/tiri.sock")
    run = AgentRun(
        "window:0:2",
        "0:2",
        "claude-run",
        "blocked",
        "claude",
        Path("/tmp/run"),
        tmux_pane_id="%pane",
    )
    monkeypatch.setattr(services, "_run", fake_run)

    assert not service.auto_wait_if_rate_limited(run)
    assert calls == [
        [
            "tmux",
            "-S",
            "/tmp/tiri.sock",
            "capture-pane",
            "-p",
            "-t",
            "%pane",
        ]
    ]


def test_rate_limit_options_visible_captures_the_run_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    capture = (
        "❯ /rate-limit-options\n"
        "What do you want to do?\n"
        "❯ 1. Stop and wait for limit to reset\n"
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, capture, "")

    service = AgentmonService(None, socket="/tmp/tiri.sock")
    run = AgentRun(
        "window:0:2",
        "0:2",
        "claude-run",
        "blocked",
        "claude",
        Path("/tmp/run"),
        tmux_pane_id="%pane",
    )
    monkeypatch.setattr(services, "_run", fake_run)

    assert service.rate_limit_options_visible(run)
    assert calls == [
        [
            "tmux",
            "-S",
            "/tmp/tiri.sock",
            "capture-pane",
            "-p",
            "-t",
            "%pane",
        ]
    ]


def test_runs_include_agents_from_multiple_git_repositories(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    foreign = Repository(
        root=repository.root.parent / "foreign",
        common_dir=repository.root.parent / "foreign" / ".git",
        branch="develop",
    )
    rows = (
        f"%1\tdev:1.0\tprimary\tcodex\tworking\t"
        f"{repository.root}\tprimary-session\t1\tcodex\n"
        f"%2\tdev:2.0\tforeign\tclaude\tblocked\t"
        f"{foreign.root}\tforeign-session\t1\tclaude\n"
    )
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(
        service,
        "_git_common_dir",
        lambda cwd: (
            repository.common_dir if cwd == repository.root else foreign.common_dir
        ),
    )
    monkeypatch.setattr(
        service,
        "_repository_for_worktree",
        lambda _cwd, common_dir: (
            repository if common_dir == repository.common_dir else foreign
        ),
    )
    monkeypatch.setattr(service, "for_repository", lambda _repo: service)
    monkeypatch.setattr(
        service,
        "_branch_at",
        lambda cwd: "primary-task" if cwd == repository.root else "foreign-task",
    )
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    runs = service.runs()

    assert [run.repository for run in runs] == [repository, foreign]
    assert [run.branch for run in runs] == ["primary-task", "foreign-task"]


def test_runs_track_each_window_and_prefer_its_agent_pane(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    rows = (
        f"%1\tdev:4.0\treview\t\t\t{repository.root}\t\t1\tzsh\n"
        f"%2\tdev:4.1\treview\tcodex\tworking\t"
        f"{repository.root}\tagent-session\t0\tcodex\n"
        f"%3\tdev:5.0\tshell\t\t\t{repository.root}\t\t1\tzsh\n"
        f"%4\tdev:6.0\tfallback\t\t\t/tmp/non-git\t\t1\tzsh\n"
        f"%5\tdev:6.1\tfallback\t\t\t{repository.root}\t\t0\tzsh\n"
        f"%6\tdev:7.0\toffside\tcodex\tworking\t/tmp/non-git\tside-session\t0\tcodex\n"
        f"%7\tdev:7.1\toffside\t\t\t{repository.root}\t\t1\tzsh\n"
    )
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(
        service,
        "_git_common_dir",
        lambda cwd: None if cwd == Path("/tmp/non-git").resolve() else repository.common_dir,
    )
    monkeypatch.setattr(
        service,
        "_repository_for_worktree",
        lambda _cwd, _common_dir: repository,
    )
    monkeypatch.setattr(service, "_branch_at", lambda _cwd: "topic")
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    runs = service.runs()

    assert len(runs) == 4
    assert runs[0].location == "dev:4"
    assert runs[0].agent == "codex"
    assert runs[0].session_id == "agent-session"
    assert runs[0].tmux_pane_id == "%2"
    assert runs[1].location == "dev:5"
    assert runs[1].agent == "window"
    assert runs[1].state == "none"
    assert runs[1].window_name == "shell"
    assert runs[2].location == "dev:6"
    assert runs[2].worktree == repository.root
    assert runs[3].location == "dev:7"
    assert runs[3].agent == "codex"
    assert runs[3].session_id == "side-session"
    assert runs[3].tmux_pane_id == "%6"
    assert runs[3].worktree_state == "not-git"


def test_agentless_panes_distinguish_shell_from_app(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    rows = (
        f"%1\tdev:1.0\tshell\t\t\t{repository.root}\t\t1\t-zsh\n"
        f"%2\tdev:2.0\teditor\t\t\t{repository.root}\t\t1\tvim\n"
    )
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: repository.common_dir)
    monkeypatch.setattr(service, "_repository_for_worktree", lambda *_: repository)
    monkeypatch.setattr(service, "_branch_at", lambda _cwd: "topic")
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    runs = service.runs()

    assert [run.agent for run in runs] == ["window", "app"]
    assert all(run.state == "none" for run in runs)


def test_agentless_pane_from_older_tiri_defaults_to_shell(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    rows = f"%1\tdev:1.0\tshell\t\t\t{repository.root}\t\t1\t\n"
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    monkeypatch.setattr(service, "_git_common_dir", lambda _cwd: repository.common_dir)
    monkeypatch.setattr(service, "_repository_for_worktree", lambda *_: repository)
    monkeypatch.setattr(service, "_branch_at", lambda _cwd: "topic")
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    assert service.runs()[0].agent == "window"


def test_runs_include_non_git_windows_and_retain_deleted_worktree_repository(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    deleted = repository.root.parent / "ses"
    ordinary = repository.root.parent / "scratch"
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    worktree_exists = True
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            (
                f"%1\tdev:4.0\trepository\tcodex\tworking\t"
                f"{repository.root}\trepo-session\t1\tcodex\n"
                f"%2\tdev:5.0\tdeleted\tcodex\tblocked\t"
                f"{deleted}{' (deleted)' if not worktree_exists else ''}"
                "\tdeleted-session\t1\tcodex\n"
                f"%3\tdev:6.0\tscratch\t\t\t{ordinary}\t\t1\tzsh\n"
            ),
            "",
        ),
    )
    monkeypatch.setattr(
        service,
        "_git_common_dir",
        lambda cwd: (
            repository.common_dir
            if cwd == repository.root or (cwd == deleted and worktree_exists)
            else None
        ),
    )
    monkeypatch.setattr(
        service,
        "_repository_for_worktree",
        lambda _cwd, _common_dir: repository,
    )
    monkeypatch.setattr(service, "for_repository", lambda _repo: service)
    monkeypatch.setattr(
        service, "_branch_at", lambda cwd: "ses" if cwd == deleted else "main"
    )
    monkeypatch.setattr(service, "_prompt_preview", lambda _cwd: "Do it.")
    monkeypatch.setattr(service, "_worktree_state", lambda _cwd: "dirty")

    initial = service.runs()
    worktree_exists = False
    runs = service.runs()

    assert initial[1].repository == repository
    assert [run.location for run in runs] == ["dev:4", "dev:5", "dev:6"]
    assert runs[1].repository == repository
    assert runs[1].worktree == deleted
    assert runs[1].branch == "ses"
    assert runs[1].agent == "codex"
    assert runs[1].state == "blocked"
    assert runs[1].session_id == "deleted-session"
    assert runs[1].worktree_state == "unknown"
    assert runs[2].repository is None
    assert runs[2].branch == "scratch"
    assert runs[2].agent == "window"


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_run_transcript_uses_agent_specific_loader(
    monkeypatch: pytest.MonkeyPatch, repository: Repository, agent: str
) -> None:
    from agentmon import services

    session_id = "019f6c99-2762-7dc2-9d93-a8a4b48a3a5e"
    expected = Transcript(session_id, Path("/tmp/transcript.jsonl"), ())
    loaded_ids: list[str] = []
    backend = services.codex_transcript if agent == "codex" else services.claude_transcript
    monkeypatch.setattr(
        backend,
        "load_transcript",
        lambda value: loaded_ids.append(value) or expected,
    )
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    run = AgentRun(
        "%1",
        "dev:3.0",
        "topic",
        "working",
        agent,
        repository.root,
        session_id=session_id,
    )

    assert service.run_transcript(run) is expected
    assert loaded_ids == [session_id]


def test_worktree_state_reports_dirty_unmerged_and_merged(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "state-check"
    git(repository.root, "worktree", "add", "-b", "state-check", str(target))

    assert service._worktree_state(target) == "merged"

    (target / "change.txt").write_text("not committed\n")
    assert service._worktree_state(target) == "dirty"

    git(target, "add", "change.txt")
    git(target, "commit", "-m", "worktree-only change")
    assert service._worktree_state(target) == "unmerged"

    git(repository.root, "merge", "--ff-only", "state-check")
    assert service._worktree_state(target) == "merged"


def test_cleanup_removes_merged_worktree_but_preserves_branch(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "cleanup-run"
    git(repository.root, "worktree", "add", "-b", "cleanup-run", str(target))
    run = AgentRun(
        "finished:cleanup", "0:", "cleanup-run", "exited", "finished", target,
        worktree_state="merged",
    )

    service.cleanup_worktree(run)

    assert not target.exists()
    branches = git(repository.root, "branch", "--list", "cleanup-run").stdout
    assert "cleanup-run" in branches


def test_cleanup_refuses_dirty_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "dirty-cleanup"
    git(repository.root, "worktree", "add", "-b", "dirty-cleanup", str(target))
    (target / "untracked.txt").write_text("keep me\n")
    run = AgentRun(
        "finished:dirty", "0:", "dirty-cleanup", "exited", "finished", target,
        worktree_state="merged",
    )

    with pytest.raises(RuntimeError, match="state is dirty"):
        service.cleanup_worktree(run)

    assert target.exists()


def test_restart_draft_reuses_finished_merged_run(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "restart-run"
    git(repository.root, "worktree", "add", "-b", "restart-run", str(target))
    prompt = "Repeat the complete task.\n\nKeep all of these details.\n"
    (target / "prompt.md").write_text(prompt)
    git(target, "add", "prompt.md")
    git(target, "commit", "-m", "agentmon: add prompt for restart-run")
    git(repository.root, "merge", "--ff-only", "restart-run")
    run = AgentRun(
        "finished:restart", "0:", "restart-run", "exited", "finished", target,
        prompt_preview="Repeat the complete task.", worktree_state="merged",
    )

    draft = service.restart_draft(run)

    assert draft.branch == "restart-run"
    assert draft.worktree == target.resolve()
    assert draft.prompt == prompt
    assert draft.overwrite_worktree
    assert draft.restart_worktree


def test_restart_draft_refuses_dirty_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "dirty-restart"
    git(repository.root, "worktree", "add", "-b", "dirty-restart", str(target))
    (target / "prompt.md").write_text("Try again.\n")
    run = AgentRun(
        "finished:dirty", "0:", "dirty-restart", "exited", "finished", target,
        worktree_state="dirty",
    )

    with pytest.raises(
        RuntimeError,
        match="Worktree cleanup refused: state is dirty, not clean and merged",
    ):
        service.restart_draft(run)


def test_populate_draft_from_dirty_worktree_keeps_form_editable(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "dirty-populate"
    git(repository.root, "worktree", "add", "-b", "dirty-populate", str(target))
    prompt = "Try this prompt on a fresh base.\n"
    (target / "prompt.md").write_text(prompt)
    git(target, "add", "prompt.md")
    git(target, "commit", "-m", "agentmon: add prompt for dirty-populate")
    (target / "untracked.txt").write_text("keep me\n")
    run = AgentRun(
        "finished:dirty", "0:", "dirty-populate", "exited", "finished", target,
        worktree_state="dirty",
    )

    draft = service.populate_draft(run)

    assert draft.branch == "dirty-populate"
    assert draft.prompt == prompt
    assert not draft.overwrite_worktree
    assert not draft.restart_worktree


def test_opens_shell_window_at_agent_cwd(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "dev:8\n", "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    run = AgentRun("%1", "dev:3.0", "topic", "working", "codex", repository.root)

    assert service.open_shell_window(run) == "dev:8"
    assert calls == [
        [
            "tmux",
            "-S",
            "/tmp/tiri.sock",
            "new-window",
            "-t",
            "dev:",
            "-P",
            "-F",
            "#{session_name}:#{window_index}",
            "-c",
            str(repository.root),
        ]
    ]


def test_recovers_recent_finished_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "finished-run"
    git(repository.root, "worktree", "add", "-b", "finished-run", str(target))
    (target / "prompt.md").write_text("Remember where this finished.\n")
    git(target, "add", "prompt.md")
    git(target, "commit", "-m", "agentmon: add prompt for finished-run")

    finished = service.recent_finished([])

    assert len(finished) == 1
    assert finished[0].branch == "finished-run"
    assert finished[0].state == "exited"
    assert finished[0].worktree == target.resolve()
    assert finished[0].prompt_preview == "Remember where this finished."


def test_active_worktree_is_not_shown_as_finished(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "still-active"
    git(repository.root, "worktree", "add", "-b", "still-active", str(target))
    (target / "prompt.md").write_text("Still working.\n")
    git(target, "add", "prompt.md")
    git(target, "commit", "-m", "agentmon: add prompt for still-active")
    subdirectory = target / "src"
    subdirectory.mkdir()
    active = AgentRun("%1", "0:3.0", "still-active", "working", "codex", subdirectory)

    assert service.recent_finished([active]) == []


def test_launch_records_steps(monkeypatch: pytest.MonkeyPatch, repository: Repository) -> None:
    from agentmon import services

    worktree = repository.root.parent / "new-worktree"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        stdout = ""
        if args[-2:] == ["--short", "HEAD"]:
            stdout = "abc123\n"
        elif "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    steps = []
    window = service.launch(LaunchDraft("new-worktree", worktree, "Do it.\n"), steps.append)

    assert window == "7"
    assert (worktree / "instruction.md").read_text() == "Do it.\n"
    assert [step.label for step in steps] == [
        "Branch and worktree created",
        "instruction.md committed",
        "tiri window and agent started",
    ]
    assert any("worktree" in call for call in calls)
    assert any("new-window" in call for call in calls)
    launch_call = next(call for call in calls if "new-window" in call)
    assert consume_environment(launch_call[-1])[1] == (
        'exec codex --yolo "$(cat instruction.md)"'
    )


def test_launch_uses_claude_command_when_selected(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    worktree = repository.root.parent / "claude-worktree"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        stdout = "abc123\n" if args[-2:] == ["--short", "HEAD"] else ""
        if "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    service.launch(
        LaunchDraft("claude-worktree", worktree, "Do it.\n", agent="claude"),
        lambda _step: None,
    )

    launch_call = next(call for call in calls if "new-window" in call)
    assert consume_environment(launch_call[-1])[1] == (
        'exec claude --dangerously-skip-permissions "$(cat instruction.md)"'
    )


def test_launch_with_devshell_enters_the_shell(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    worktree = repository.root.parent / "devshell-worktree"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        stdout = "abc123\n" if args[-2:] == ["--short", "HEAD"] else ""
        if "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    service.launch(
        LaunchDraft("devshell-worktree", worktree, "Do it.\n", devshell=True),
        lambda _step: None,
    )

    launch_call = next(call for call in calls if "new-window" in call)
    # The environment is loaded before nix, so the dev shell layers over it.
    assert consume_environment(launch_call[-1])[1] == (
        'exec nix develop . --command codex --yolo "$(cat instruction.md)"'
    )


def test_devshell_has_no_bare_agent_fallback(repository: Repository) -> None:
    """A dev shell that will not build must not start the agent without it."""
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    command = service._agent_command("codex", devshell=True)

    # `exec` replaces the shell, so there is nothing after the dev shell to
    # fall back to and no branch that could reach a bare agent.
    assert command == 'exec nix develop . --command codex --yolo "$(cat instruction.md)"'
    assert "||" not in command
    assert "; fi" not in command


def test_launch_without_devshell_keeps_the_plain_command(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    assert service._agent_command("codex") == 'exec codex --yolo "$(cat instruction.md)"'
    assert "nix" not in service._agent_command("claude")


def test_agy_takes_its_model_effort_and_prompt_as_separate_flags(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    # agy reads the initial prompt from `-i`, not a trailing argument.
    assert service._agent_command("agy", "gemini-3.6-flash", "high") == (
        "exec agy --dangerously-skip-permissions --model gemini-3.6-flash "
        '--effort high -i "$(cat instruction.md)"'
    )
    assert service._agent_command("agy") == (
        'exec agy --dangerously-skip-permissions -i "$(cat instruction.md)"'
    )
    assert service._agent_command("agy", with_instruction=False) == (
        "exec agy --dangerously-skip-permissions"
    )


def test_agy_trust_seeding_adds_a_worktree_once(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    worktree = tmp_path / "work"
    worktree.mkdir()

    # No settings file yet: one is created with just the trusted list.
    assert trust_agy_workspace(worktree, settings=settings) is True
    assert json.loads(settings.read_text()) == {
        "trustedWorkspaces": [str(worktree)]
    }
    # Already trusted: left exactly as it was.
    assert trust_agy_workspace(worktree, settings=settings) is False

    # An existing file keeps its other keys and entries.
    settings.write_text(
        json.dumps({"colorScheme": "dark", "trustedWorkspaces": ["/elsewhere"]})
    )
    assert trust_agy_workspace(worktree, settings=settings) is True
    assert json.loads(settings.read_text()) == {
        "colorScheme": "dark",
        "trustedWorkspaces": ["/elsewhere", str(worktree)],
    }


def test_agy_trust_seeding_survives_an_unusable_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("not json at all")
    worktree = tmp_path / "work"
    worktree.mkdir()

    assert trust_agy_workspace(worktree, settings=settings) is True
    assert json.loads(settings.read_text()) == {
        "trustedWorkspaces": [str(worktree)]
    }


def test_agy_trust_seeding_records_the_path_the_agent_will_see(
    tmp_path: Path,
) -> None:
    """A worktree reached through a symlink is trusted under both spellings."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    settings = tmp_path / "settings.json"

    assert trust_agy_workspace(link, settings=settings) is True
    assert json.loads(settings.read_text())["trustedWorkspaces"] == [
        str(link),
        str(real),
    ]


def test_devshell_available_needs_both_nix_and_a_flake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    monkeypatch.setattr(services.shutil, "which", lambda _name: "/usr/bin/nix")
    assert not services.devshell_available(tmp_path)

    (tmp_path / "flake.nix").write_text("{}")
    assert services.devshell_available(tmp_path)

    monkeypatch.setattr(services.shutil, "which", lambda _name: None)
    assert not services.devshell_available(tmp_path)


def test_devshell_available_ignores_a_flake_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    monkeypatch.setattr(services.shutil, "which", lambda _name: "/usr/bin/nix")
    (tmp_path / "flake.nix").mkdir()

    assert not services.devshell_available(tmp_path)


def test_launch_existing_branch_puts_worktree_path_before_branch(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    worktree = repository.root.parent / "existing-branch"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        stdout = "abc123\n" if args[-2:] == ["--short", "HEAD"] else ""
        if "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    service.launch(
        LaunchDraft(
            "existing-branch", worktree, "Continue.\n", existing_branch=True
        ),
        lambda _step: None,
    )

    add = next(call for call in calls if "worktree" in call and "add" in call)
    assert add[-3:] == ["add", str(worktree), "existing-branch"]


def test_launch_removes_overwritten_worktree_first(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    worktree = repository.root.parent / "replace-me"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        if "remove" in args:
            (worktree / "prompt.md").unlink(missing_ok=True)
        stdout = "abc123\n" if args[-2:] == ["--short", "HEAD"] else ""
        if "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    steps = []
    service.launch(
        LaunchDraft("replace-me", worktree, "Do it.\n", overwrite_worktree=True),
        steps.append,
    )

    remove_index = next(i for i, call in enumerate(calls) if "remove" in call)
    add_index = next(i for i, call in enumerate(calls) if "add" in call and "worktree" in call)
    assert remove_index < add_index
    assert steps[0].label == "Existing worktree removed"


def test_launch_refuses_dirty_overwritten_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = service.suggested_worktree("dirty-launch")
    git(repository.root, "worktree", "add", "-b", "dirty-launch", str(target))
    (target / "untracked.txt").write_text("keep me\n")

    with pytest.raises(CommandError, match="Worktree overwrite refused: state is dirty"):
        service.launch(
            LaunchDraft(
                "dirty-launch", target, "Do it.\n", overwrite_worktree=True
            ),
            lambda _step: None,
        )

    assert target.exists()


def test_prepare_worktree_creates_new_branch_and_worktree(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")

    worktree = service.prepare_worktree("prep-run")

    assert worktree == repository.root.parent / "prep-run"
    assert worktree.is_dir()
    assert git(worktree, "branch", "--show-current").stdout.strip() == "prep-run"
    assert service.read_instruction(worktree) is None


def test_prepare_worktree_uses_existing_branch(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    git(repository.root, "branch", "prep-existing")

    worktree = service.prepare_worktree("prep-existing")

    assert git(worktree, "branch", "--show-current").stdout.strip() == "prep-existing"


def test_prepare_worktree_reset_moves_existing_branch_to_base(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    git(repository.root, "branch", "prep-stale")
    (repository.root / "later.txt").write_text("newer work\n")
    git(repository.root, "add", "later.txt")
    git(repository.root, "commit", "-m", "advance main past the stale branch")

    worktree = service.prepare_worktree("prep-stale", reset=True)

    stale = git(worktree, "rev-parse", "HEAD").stdout.strip()
    main = git(repository.root, "rev-parse", "main").stdout.strip()
    assert stale == main


def test_prepare_worktree_forks_new_branch_from_base(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    git(repository.root, "branch", "prep-base")
    (repository.root / "base-only.txt").write_text("base work\n")
    git(repository.root, "add", "base-only.txt")
    git(repository.root, "commit", "-m", "commit reachable only from prep-base")
    git(repository.root, "branch", "-f", "prep-base", "HEAD")
    git(repository.root, "reset", "--hard", "HEAD~1")

    worktree = service.prepare_worktree("prep-fork", base="prep-base")

    assert git(worktree, "branch", "--show-current").stdout.strip() == "prep-fork"
    forked = git(worktree, "rev-parse", "HEAD").stdout.strip()
    base = git(repository.root, "rev-parse", "prep-base").stdout.strip()
    assert forked == base


def test_prepare_worktree_rejects_existing_path(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    (repository.root.parent / "prep-occupied").mkdir()

    with pytest.raises(ValueError, match="already exists"):
        service.prepare_worktree("prep-occupied")


def test_save_instruction_commits_and_retires_legacy_prompt(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "migrate-me"
    git(repository.root, "worktree", "add", "-b", "migrate-me", str(target))
    (target / "prompt.md").write_text("Old prompt.\n")
    git(target, "add", "prompt.md")
    git(target, "commit", "-m", "agentmon: add prompt for migrate-me")

    commit = service.save_instruction(target, "migrate-me", "New instruction.\n")

    assert commit
    assert (target / "instruction.md").read_text() == "New instruction.\n"
    assert not (target / "prompt.md").exists()
    subject = git(target, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "agentmon: add instruction for migrate-me"
    assert git(target, "status", "--porcelain").stdout == ""


def test_save_instruction_returns_none_when_unchanged(repository: Repository) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "unchanged"
    git(repository.root, "worktree", "add", "-b", "unchanged", str(target))
    assert service.save_instruction(target, "unchanged", "Same.\n")

    assert service.save_instruction(target, "unchanged", "Same.\n") is None


def test_launch_agent_commits_instruction_and_builds_command(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "launch-me"
    git(repository.root, "worktree", "add", "-b", "launch-me", str(target))
    captured: dict[str, object] = {}

    def open_window(worktree: Path, name: str, command: str) -> str:
        captured.update(worktree=worktree, name=name, command=command)
        return "9"

    monkeypatch.setattr(service, "_open_agent_window", open_window)
    run = AgentRun("finished:launch", "0:", "launch-me", "exited", "finished", target)

    window = service.launch_agent(
        run,
        agent="codex",
        model="gpt-5-codex",
        effort="high",
        instruction="Do it.\n",
    )

    assert window == "9"
    assert (target / "instruction.md").read_text() == "Do it.\n"
    subject = git(target, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "agentmon: add instruction for launch-me"
    assert captured["worktree"] == target.resolve()
    assert captured["command"] == (
        'exec codex --yolo -m gpt-5-codex -c model_reasoning_effort=high'
        ' "$(cat instruction.md)"'
    )


def test_launch_agent_devshell_wraps_the_interactive_command(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "launch-devshell"
    git(repository.root, "worktree", "add", "-b", "launch-devshell", str(target))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service,
        "_open_agent_window",
        lambda worktree, name, command: captured.update(command=command) or "9",
    )
    run = AgentRun(
        "finished:devshell", "0:", "launch-devshell", "exited", "finished", target
    )

    service.launch_agent(run, agent="claude", instruction="  ", devshell=True)

    # No instruction, so the command does not read instruction.md.
    assert captured["command"] == (
        "exec nix develop . --command claude --dangerously-skip-permissions"
    )


def test_launch_agent_without_instruction_starts_interactively(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "launch-empty"
    git(repository.root, "worktree", "add", "-b", "launch-empty", str(target))
    head = git(target, "rev-parse", "HEAD").stdout.strip()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service,
        "_open_agent_window",
        lambda worktree, name, command: captured.update(command=command) or "9",
    )
    run = AgentRun("finished:empty", "0:", "launch-empty", "exited", "finished", target)

    service.launch_agent(run, agent="claude", model="opus", instruction="  ")

    assert captured["command"] == (
        "exec claude --dangerously-skip-permissions --model opus"
    )
    assert not (target / "instruction.md").exists()
    assert git(target, "rev-parse", "HEAD").stdout.strip() == head


def test_prepared_worktree_without_instruction_is_discovered(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    worktree = service.prepare_worktree("prep-visible")

    finished = service.recent_finished([])

    assert [run.branch for run in finished] == ["prep-visible"]
    assert finished[0].worktree == worktree.resolve()
    assert finished[0].prompt_preview == "—"


def test_instruction_edit_target_seeds_from_legacy_prompt(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    target = repository.root.parent / "edit-seed"
    git(repository.root, "worktree", "add", "-b", "edit-seed", str(target))
    (target / "prompt.md").write_text("Legacy prompt.\n")
    run = AgentRun("finished:seed", "0:", "edit-seed", "exited", "finished", target)

    path = service.instruction_edit_target(run)

    assert path == target / "instruction.md"
    assert path.read_text() == "Legacy prompt.\n"

    path.write_text("Edited instruction.\n")
    commit = service.commit_instruction(run)

    assert commit
    assert not (target / "prompt.md").exists()
    assert service.read_instruction(target) == "Edited instruction.\n"


def test_restart_launch_recreates_existing_branch_from_dashboard_branch(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    worktree = repository.root.parent / "restart-me"
    worktree.mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd=None, check=True):
        calls.append(args)
        stdout = "abc123\n" if args[-2:] == ["--short", "HEAD"] else ""
        if "new-window" in args:
            stdout = "7\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    monkeypatch.setattr(service, "_worktree_state", lambda _path: "merged")
    service.launch(
        LaunchDraft(
            "restart-me", worktree, "Do it again.\n",
            overwrite_worktree=True, restart_worktree=True,
        ),
        lambda _step: None,
    )

    remove = next(call for call in calls if "remove" in call)
    add = next(call for call in calls if "worktree" in call and "add" in call)
    assert "--force" not in remove
    assert add[-4:] == ["add", "-B", "restart-me", str(worktree)]


def _pane_rows(*panes: tuple[str, str, str]) -> str:
    return "".join(f"{pane}\t{agent}\t{state}\n" for pane, agent, state in panes)


def test_pane_status_picks_one_pane_out_of_the_server_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    rows = _pane_rows(("%1", "", ""), ("%2", "codex", "working"))
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, rows, ""),
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    assert service.pane_status("%2") == PaneStatus("%2", "codex", "working")
    # An agentless pane keeps a readable state rather than an empty string.
    assert service.pane_status("%1") == PaneStatus("%1", "", "none")
    assert service.pane_status("%9") is None


def test_pane_status_reports_a_dead_server_as_a_gone_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "no server"),
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    assert service.pane_status("%2") is None


def test_wait_for_pane_state_returns_the_state_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    states = iter(["working", "working", "idle"])
    monkeypatch.setattr(
        service, "pane_status", lambda pane: PaneStatus(pane, "codex", next(states))
    )

    assert service.wait_for_pane_state("%2", ("idle", "blocked"), poll=0.001) == "idle"


def test_wait_for_pane_state_ends_when_the_pane_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(service, "pane_status", lambda _pane: None)

    assert service.wait_for_pane_state("%2", ("idle",), timeout=5, poll=0.001) == "exited"


def test_wait_for_pane_state_gives_up_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        service, "pane_status", lambda pane: PaneStatus(pane, "codex", "working")
    )

    assert service.wait_for_pane_state("%2", ("idle",), timeout=0.01, poll=0.005) is None


def test_split_agent_pane_starts_the_agent_below_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    pane = service.split_agent_pane(
        target="%1",
        worktree=tmp_path,
        agent="codex",
        model="gpt-5.6-luna",
        effort="max",
        instruction_file=tmp_path / "prompt.md",
        size_percent=75,
    )

    assert pane == "%7"
    assert calls[0][:9] == [
        "tmux", "-S", "/tmp/tiri.sock", "split-window", "-d", "-v",
        "-t", "%1", "-l",
    ]
    assert calls[0][9] == "75%"
    assert calls[0][-4:-1] == ["-P", "-F", "#{pane_id}"]
    assert consume_environment(calls[0][-1])[1] == (
        "exec codex --yolo -m gpt-5.6-luna -c model_reasoning_effort=max "
        f'"$(cat {tmp_path / "prompt.md"})"'
    )


def test_split_agent_pane_can_hand_the_focus_to_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(services, "_run", fake_run)
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    service.split_agent_pane(
        target="%1", worktree=tmp_path, agent="codex", focus=True
    )

    assert "-d" not in calls[0]
    assert calls[0][:8] == [
        "tmux", "-S", "/tmp/tiri.sock", "split-window", "-v", "-t", "%1", "-l",
    ]


def test_environment_script_skips_what_the_new_pane_owns() -> None:
    from agentmon import services

    script = services._environment_script(
        {
            "HOME": "/home/x",
            "ANTHROPIC_API_KEY": "one two",
            "TMUX_PANE": "%3",
            "TMUX": "/tmp/s,1,0",
            "TERM": "xterm",
            "SHLVL": "2",
        }
    )

    assert script == "ANTHROPIC_API_KEY='one two'\nHOME=/home/x\n"


def test_environment_script_drops_a_name_no_shell_can_assign() -> None:
    """Bash exports its functions under names that would break the source."""
    from agentmon import services

    script = services._environment_script(
        {"BASH_FUNC_which%%": "() { :; }", "HOME": "/home/x"}
    )

    assert script == "HOME=/home/x\n"


def test_environment_script_survives_a_round_trip_through_sh(
    tmp_path: Path,
) -> None:
    from agentmon import services

    awkward = {
        "QUOTED": "it's \"both\"",
        "NEWLINE": "first\nsecond",
        "EMPTY": "",
        "GLOB": "*",
    }
    spool = tmp_path / "env.sh"
    spool.write_text(services._environment_script(awkward), encoding="utf-8")
    # Read back from a child, which sees exported variables and nothing else —
    # the position the agent is in.
    reader = tmp_path / "read.sh"
    reader.write_text(
        "".join(f'printf "%s\\0" "${name}"\n' for name in awkward),
        encoding="utf-8",
    )

    dumped = subprocess.run(
        ["sh", "-c", f"set -a; . {spool}; set +a; sh {reader}"],
        capture_output=True, text=True, check=True,
    ).stdout

    assert dumped.split("\0")[: len(awkward)] == list(awkward.values())


def test_split_agent_pane_carries_the_callers_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tmux spawns a pane from the *server's* environment, so pass ours along."""
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(services, "_run", fake_run)
    monkeypatch.setenv("AGENTMON_TEST_TOKEN", "carried")
    monkeypatch.setenv("TMUX_PANE", "%1")
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    service.split_agent_pane(target="%1", worktree=tmp_path, agent="codex")

    # Sourced and removed by the pane, and only then does the agent take over.
    script, command = consume_environment(calls[0][-1])
    assert "AGENTMON_TEST_TOKEN=carried\n" in script
    # The new pane is not the one this runs in, and tmux names it itself.
    assert "TMUX_PANE" not in script
    assert command.startswith("exec codex ")


def test_the_spooled_environment_is_readable_only_by_its_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It carries whatever credentials the caller's environment holds."""
    from agentmon import services

    spool = services._spool_environment("HOME=/home/x\n")

    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    spool.unlink()


def test_open_agent_window_carries_the_callers_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "7\n", "")

    monkeypatch.setattr(services, "_run", fake_run)
    monkeypatch.setenv("AGENTMON_TEST_TOKEN", "carried")
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    service._open_agent_window(tmp_path, "branch", "exec codex")

    script, command = consume_environment(calls[0][-1])
    assert "AGENTMON_TEST_TOKEN=carried\n" in script
    assert command == "exec codex"


def test_split_agent_pane_requires_a_pane_id_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "\n", ""),
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    with pytest.raises(CommandError):
        service.split_agent_pane(target="%1", worktree=tmp_path, agent="codex")


def test_a_failed_split_takes_the_spooled_environment_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "\n", ""),
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    with pytest.raises(CommandError):
        service.split_agent_pane(target="%1", worktree=tmp_path, agent="codex")

    assert list(tmp_path.glob("agentmon-env-*.sh")) == []


def test_exit_agent_pane_asks_the_agent_to_quit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    calls: list[list[str]] = []
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(service, "pane_status", lambda _pane: None)

    naps: list[float] = []
    assert service.exit_agent_pane("%7", sleep=naps.append) is True
    # Three keystrokes, spaced: sent as one burst the Enter reads as a pasted
    # newline and the command is typed but never submitted.
    assert calls == [
        ["tmux", "-S", "/tmp/tiri.sock", "send-keys", "-t", "%7", "C-u"],
        ["tmux", "-S", "/tmp/tiri.sock", "send-keys", "-t", "%7", "-l", "/exit"],
        ["tmux", "-S", "/tmp/tiri.sock", "send-keys", "-t", "%7", "Enter"],
    ]
    assert naps == [services.KEYSTROKE_GAP_SECONDS] * 2


def test_exit_agent_pane_repeats_the_enter_before_killing_a_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    calls: list[list[str]] = []
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")
    monkeypatch.setattr(
        service, "pane_status", lambda pane: PaneStatus(pane, "codex", "idle")
    )

    assert (
        service.exit_agent_pane(
            "%7", timeout=0.01, poll=0.005, sleep=lambda _seconds: None
        )
        is False
    )
    assert calls[-2:] == [
        ["tmux", "-S", "/tmp/tiri.sock", "send-keys", "-t", "%7", "Enter"],
        ["tmux", "-S", "/tmp/tiri.sock", "kill-pane", "-t", "%7"],
    ]


def test_kill_agent_pane_takes_the_pane_down_without_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import services

    calls: list[list[str]] = []
    monkeypatch.setattr(
        services,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    # What a run ended mid-turn gets: nothing typed at an agent that would
    # read it as its next message rather than as a command.
    assert service.kill_agent_pane("%7") is None
    assert calls == [
        ["tmux", "-S", "/tmp/tiri.sock", "kill-pane", "-t", "%7"],
    ]


def test_commit_all_changes_harvests_a_dirty_worktree(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    (repository.root / "notes.md").write_text("agent output\n")

    commit = service.commit_all_changes(repository.root, "looper: auto-commit run 1")

    assert commit
    subject = git(repository.root, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "looper: auto-commit run 1"
    assert git(repository.root, "status", "--porcelain").stdout == ""


def test_commit_all_changes_leaves_a_clean_worktree_alone(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    before = git(repository.root, "rev-parse", "HEAD").stdout

    assert service.commit_all_changes(repository.root, "looper: run 1") is None
    assert git(repository.root, "rev-parse", "HEAD").stdout == before


def test_agent_command_keeps_the_worktree_instruction_by_default() -> None:
    service = AgentmonService(None, socket="/tmp/tiri.sock")

    assert service._agent_command("codex") == 'exec codex --yolo "$(cat instruction.md)"'


def test_discover_context_pairs_the_repository_with_a_socket(
    monkeypatch: pytest.MonkeyPatch, repository: Repository
) -> None:
    from agentmon import services

    monkeypatch.setattr(
        services, "discover_socket", lambda **_kwargs: SocketSelection("/tmp/tiri.sock", "old server")
    )

    context = services.discover_context(start=repository.root)

    assert context.repository == repository
    assert context.warning == "old server"
    assert context.service().socket == "/tmp/tiri.sock"


def test_discover_context_tolerates_a_directory_outside_any_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import services

    monkeypatch.setattr(
        services, "discover_socket", lambda **_kwargs: SocketSelection("/tmp/tiri.sock")
    )

    context = services.discover_context(start=tmp_path)

    assert context.repository is None
    assert context.service().repo is None
