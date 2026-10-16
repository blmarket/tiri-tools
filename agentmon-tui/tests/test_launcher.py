from __future__ import annotations

import json
import re
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from agentmon.launcher import AgentLauncher, trust_agy_workspace
from agentmon.model import Repository
from agentmon.services import CommandError, PaneStatus, discover_repository


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


def test_devshell_has_no_bare_agent_fallback(repository: Repository) -> None:
    """A dev shell that will not build must not start the agent without it."""
    service = AgentLauncher(repository, socket="/tmp/tiri.sock")
    command = service._agent_command("codex", devshell=True)

    # `exec` replaces the shell, so there is nothing after the dev shell to
    # fall back to and no branch that could reach a bare agent.
    assert command == 'exec nix develop . --command codex --yolo "$(cat instruction.md)"'
    assert "||" not in command
    assert "; fi" not in command


def test_launch_without_devshell_keeps_the_plain_command(
    repository: Repository,
) -> None:
    service = AgentLauncher(repository, socket="/tmp/tiri.sock")

    assert service._agent_command("codex") == 'exec codex --yolo "$(cat instruction.md)"'
    assert "nix" not in service._agent_command("claude")


def test_agy_takes_its_model_effort_and_prompt_as_separate_flags(
    repository: Repository,
) -> None:
    service = AgentLauncher(repository, socket="/tmp/tiri.sock")

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
    from agentmon import launcher

    monkeypatch.setattr(launcher.shutil, "which", lambda _name: "/usr/bin/nix")
    assert not launcher.devshell_available(tmp_path)

    (tmp_path / "flake.nix").write_text("{}")
    assert launcher.devshell_available(tmp_path)

    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)
    assert not launcher.devshell_available(tmp_path)


def test_devshell_available_ignores_a_flake_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import launcher

    monkeypatch.setattr(launcher.shutil, "which", lambda _name: "/usr/bin/nix")
    (tmp_path / "flake.nix").mkdir()

    assert not launcher.devshell_available(tmp_path)


def test_split_agent_pane_starts_the_agent_below_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import launcher

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(launcher, "_run", fake_run)
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

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
    from agentmon import launcher

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(launcher, "_run", fake_run)
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

    service.split_agent_pane(
        target="%1", worktree=tmp_path, agent="codex", focus=True
    )

    assert "-d" not in calls[0]
    assert calls[0][:8] == [
        "tmux", "-S", "/tmp/tiri.sock", "split-window", "-v", "-t", "%1", "-l",
    ]


def test_environment_script_skips_what_the_new_pane_owns() -> None:
    from agentmon import launcher

    script = launcher._environment_script(
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
    from agentmon import launcher

    script = launcher._environment_script(
        {"BASH_FUNC_which%%": "() { :; }", "HOME": "/home/x"}
    )

    assert script == "HOME=/home/x\n"


def test_environment_script_survives_a_round_trip_through_sh(
    tmp_path: Path,
) -> None:
    from agentmon import launcher

    awkward = {
        "QUOTED": "it's \"both\"",
        "NEWLINE": "first\nsecond",
        "EMPTY": "",
        "GLOB": "*",
    }
    spool = tmp_path / "env.sh"
    spool.write_text(launcher._environment_script(awkward), encoding="utf-8")
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
    from agentmon import launcher

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "%7\n", "")

    monkeypatch.setattr(launcher, "_run", fake_run)
    monkeypatch.setenv("AGENTMON_TEST_TOKEN", "carried")
    monkeypatch.setenv("TMUX_PANE", "%1")
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

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
    from agentmon import launcher

    spool = launcher._spool_environment("HOME=/home/x\n")

    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    spool.unlink()


def test_split_agent_pane_requires_a_pane_id_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import launcher

    monkeypatch.setattr(
        launcher,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "\n", ""),
    )
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

    with pytest.raises(CommandError):
        service.split_agent_pane(target="%1", worktree=tmp_path, agent="codex")


def test_a_failed_split_takes_the_spooled_environment_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentmon import launcher

    monkeypatch.setattr(
        launcher,
        "_run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "\n", ""),
    )
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

    with pytest.raises(CommandError):
        service.split_agent_pane(target="%1", worktree=tmp_path, agent="codex")

    assert list(tmp_path.glob("agentmon-env-*.sh")) == []


def test_exit_agent_pane_asks_the_agent_to_quit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import launcher

    calls: list[list[str]] = []
    monkeypatch.setattr(
        launcher,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentLauncher(None, socket="/tmp/tiri.sock")
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
    assert naps == [launcher.KEYSTROKE_GAP_SECONDS] * 2


def test_exit_agent_pane_repeats_the_enter_before_killing_a_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentmon import launcher

    calls: list[list[str]] = []
    monkeypatch.setattr(
        launcher,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentLauncher(None, socket="/tmp/tiri.sock")
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
    from agentmon import launcher

    calls: list[list[str]] = []
    monkeypatch.setattr(
        launcher,
        "_run",
        lambda args, **kwargs: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "", "")
        )[1],
    )
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

    # What a run ended mid-turn gets: nothing typed at an agent that would
    # read it as its next message rather than as a command.
    assert service.kill_agent_pane("%7") is None
    assert calls == [
        ["tmux", "-S", "/tmp/tiri.sock", "kill-pane", "-t", "%7"],
    ]


def test_commit_all_changes_harvests_a_dirty_worktree(
    repository: Repository,
) -> None:
    service = AgentLauncher(repository, socket="/tmp/tiri.sock")
    (repository.root / "notes.md").write_text("agent output\n")

    commit = service.commit_all_changes(repository.root, "looper: auto-commit run 1")

    assert commit
    subject = git(repository.root, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "looper: auto-commit run 1"
    assert git(repository.root, "status", "--porcelain").stdout == ""


def test_commit_all_changes_leaves_a_clean_worktree_alone(
    repository: Repository,
) -> None:
    service = AgentLauncher(repository, socket="/tmp/tiri.sock")
    before = git(repository.root, "rev-parse", "HEAD").stdout

    assert service.commit_all_changes(repository.root, "looper: run 1") is None
    assert git(repository.root, "rev-parse", "HEAD").stdout == before


def test_agent_command_keeps_the_worktree_instruction_by_default() -> None:
    service = AgentLauncher(None, socket="/tmp/tiri.sock")

    assert service._agent_command("codex") == 'exec codex --yolo "$(cat instruction.md)"'
