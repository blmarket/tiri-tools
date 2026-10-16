from __future__ import annotations

import io
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentmon.model import AgentRun, Repository
from agentmon.services import (
    AgentmonService,
    CommandError,
    PaneStatus,
    SocketSelection,
    discover_repository,
    discover_socket,
)
from agentmon.transcript import Transcript


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


def test_worktree_without_instruction_is_discovered(
    repository: Repository,
) -> None:
    service = AgentmonService(repository, socket="/tmp/tiri.sock")
    worktree = repository.root.parent / "prep-visible"
    git(repository.root, "worktree", "add", "-b", "prep-visible", str(worktree))

    finished = service.recent_finished([])

    assert [run.branch for run in finished] == ["prep-visible"]
    assert finished[0].worktree == worktree.resolve()
    assert finished[0].prompt_preview == "—"


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
