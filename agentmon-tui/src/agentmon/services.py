from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import claude_transcript, transcript as codex_transcript
from .model import (
    AgentRun,
    LaunchDraft,
    LaunchStep,
    PromptHistory,
    Repository,
    normalize_launch_agent,
)
from .transcript import Transcript, TranscriptError


class CommandError(RuntimeError):
    pass


# The launch instruction lives in instruction.md; prompt.md is the legacy name
# still honored when reading so older worktrees keep working.
INSTRUCTION_FILENAMES = ("instruction.md", "prompt.md")

# Foreground commands tiri reports for a bare interactive shell. Anything else
# running in an agentless pane is treated as "some app" rather than an idle
# shell, so it earns a distinct badge in the dashboard.
SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "ash"}
)


# Entering a nix dev shell is deferred into the agent pane: a cold flake can
# take minutes to build, and there the wait and any error are visible instead
# of freezing the TUI.
DEVSHELL_PREFIX = "nix develop . --command"

# What a new pane takes from the server rather than from whoever asked for it:
# its identity inside tmux, the terminal it was handed, and the bookkeeping its
# own shell maintains. Everything else is forwarded, so the agent runs with the
# environment of the caller rather than the one the server was started with.
PANE_OWNED_ENVIRONMENT = frozenset(
    {
        "TMUX",
        "TMUX_PANE",
        "TERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "COLORTERM",
        "SHELL",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
    }
)

CLAUDE_RATE_LIMIT_COMMAND = "/rate-limit-options"
CLAUDE_RATE_LIMIT_OPTION_ONE = "Stop and wait for limit to reset"

# claude, codex and agy all quit on `/exit` typed at their prompt.
AGENT_EXIT_COMMAND = "/exit"

# Keystrokes arriving in one burst read as a paste to an agent TUI, where a
# newline is inserted rather than submitting — the same thing shift+Enter
# does. Spacing a typed command from its Enter keeps them two keystrokes.
KEYSTROKE_GAP_SECONDS = 0.3

# How long an unaccompanied Enter is given before it is assumed lost and sent
# again, out of the exit wait as a whole.
EXIT_RETRY_SECONDS = 3.0

# agy asks whether the project is trusted before it will run anything, and
# remembers the answer per exact workspace path in this file.
AGY_SETTINGS = Path(".gemini/antigravity-cli/settings.json")
AGY_TRUSTED_KEY = "trustedWorkspaces"
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def devshell_available(path: Path) -> bool:
    """Return whether `path` looks like it can supply a nix dev shell.

    Deliberately cheap: this gates a launch-form toggle, so it must not
    evaluate the flake. Whether the flake actually exposes a dev shell is
    settled by the probe in the launch command, where a miss costs a silent
    fallback instead of a stalled form.
    """
    return shutil.which("nix") is not None and (path / "flake.nix").is_file()


def trust_agy_workspace(worktree: Path, *, settings: Path | None = None) -> bool:
    """Pre-answer agy's trust dialog for `worktree`; True when it was added.

    agy asks "Do you trust the contents of this project?" for every workspace
    path it has not seen before, and `--dangerously-skip-permissions` does not
    cover it. Every launch here runs in a freshly created worktree — a path agy
    has never seen — so without seeding this the agent would sit on that dialog
    with nobody watching, which a loop runner reads as blocked and stops on.

    Both the literal and the symlink-resolved path are recorded when they
    differ: the trust check is on the exact string agy computes for its working
    directory, and which of the two that is depends on how the pane was started.
    A malformed or missing settings file is rewritten from scratch rather than
    failing the launch — its only other keys are UI preferences.
    """
    path = settings if settings is not None else Path.home() / AGY_SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    trusted = data.get(AGY_TRUSTED_KEY)
    if not isinstance(trusted, list):
        trusted = []

    wanted = [str(worktree)]
    resolved = str(worktree.resolve())
    if resolved not in wanted:
        wanted.append(resolved)
    missing = [candidate for candidate in wanted if candidate not in trusted]
    if not missing:
        return False

    data[AGY_TRUSTED_KEY] = trusted + missing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _is_shell_command(command: str) -> bool:
    """Return whether a pane's foreground command is just an interactive shell."""
    # Older tiri builds do not expose pane_current_command. Preserve their
    # previous agentless-window behavior instead of labelling every pane an app.
    if not command:
        return True
    # Login shells are reported with a leading dash (e.g. "-zsh").
    return command.lstrip("-") in SHELL_COMMANDS


def is_claude_rate_limit_options_dialog(capture: str) -> bool:
    """Return whether a visible pane capture is Claude's rate-limit menu.

    The command text can remain in the prompt after the menu is dismissed, so
    require the menu title and its first option as well. ``capture-pane -p``
    returns the visible alternate screen, which keeps old scrollback from
    triggering this detector.
    """
    plain = _ANSI_ESCAPE.sub("", capture)
    lines = [" ".join(line.split()) for line in plain.splitlines()]
    return (
        any(CLAUDE_RATE_LIMIT_COMMAND in line for line in lines)
        and any("What do you want to do?" in line for line in lines)
        and any(
            re.search(
                rf"(?:^[❯>]\s*)?1\.\s*{re.escape(CLAUDE_RATE_LIMIT_OPTION_ONE)}\s*$",
                line,
            )
            for line in lines
        )
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]

RUN_FORMAT = "\t".join(
    (
        "#{pane_id}",
        "#{session_name}:#{window_index}.#{pane_index}",
        "#{window_name}",
        "#{pane_agent}",
        "#{pane_agent_state}",
        "#{pane_current_path}",
        "#{pane_agent_session_id}",
        "#{pane_active}",
        "#{pane_current_command}",
    )
)

# Single-pane status is read from the same session-wide listing as RUN_FORMAT:
# `list-panes -t` targets a pane's whole window, so the pane is picked out here.
PANE_STATUS_FORMAT = "\t".join(
    ("#{pane_id}", "#{pane_agent}", "#{pane_agent_state}")
)

_SUBSCRIPTION_NAME = "agentmon"
_RUN_CHANGE_NOTIFICATIONS = (
    f"%subscription-changed {_SUBSCRIPTION_NAME} ",
    "%layout-change ",
    "%session-changed ",
    "%session-closed ",
    "%session-renamed ",
    "%sessions-changed",
    "%unlinked-window-",
    "%window-add ",
    "%window-close ",
    "%window-pane-changed ",
    "%window-renamed ",
)


def _run_change_notification(line: str) -> bool:
    return line.startswith(_RUN_CHANGE_NOTIFICATIONS)


def _subscription_command() -> str:
    value = f"{_SUBSCRIPTION_NAME}:%*:{RUN_FORMAT}"
    return f"refresh-client -B {shlex.quote(value)}\n"


def _environment_script(environment: Mapping[str, str] | None = None) -> str:
    """This process's environment, as shell a new pane can source.

    tmux spawns a pane from the environment the *server* was started with, and
    takes only `PATH` from the client asking for the spawn. An agent started
    from here would otherwise run with whatever the server happened to inherit,
    however long ago that was, so every variable that is not the pane's own is
    passed along. A dev shell entered inside the pane still layers its own
    toolchain over the result.

    `split-window -e` would say this directly, but a whole environment does not
    fit: tmux caps a command at `MAX_IMSGSIZE` and a nix dev shell's
    environment is several times that.
    """
    source = os.environ if environment is None else environment
    return "".join(
        f"{name}={shlex.quote(value)}\n"
        for name, value in sorted(source.items())
        # A name a shell cannot assign to would break the whole file when it is
        # sourced. Bash exports its functions as `BASH_FUNC_name%%`, so this is
        # not hypothetical.
        if name not in PANE_OWNED_ENVIRONMENT and _SHELL_NAME.fullmatch(name)
    )


def _spool_environment(script: str) -> Path:
    """Write an environment script where only its owner can read it back."""
    handle, path = tempfile.mkstemp(prefix="agentmon-env-", suffix=".sh")
    with os.fdopen(handle, "w", encoding="utf-8") as spool:
        spool.write(script)
    return Path(path)


def _run(
    args: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args, cwd=cwd, text=True, capture_output=True, check=False
        )
    except OSError as exc:
        raise CommandError(str(exc)) from exc
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise CommandError(f"{shlex.join(args)}: {message}")
    return result


def discover_repository(start: Path = Path.cwd()) -> Repository:
    root = Path(_run(["git", "-C", str(start), "rev-parse", "--show-toplevel"]).stdout.strip())
    common_raw = _run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"]
    ).stdout.strip()
    common_dir = Path(common_raw)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    branch = _run(
        ["git", "-C", str(root), "branch", "--show-current"]
    ).stdout.strip() or "(detached)"
    return Repository(root=root.resolve(), common_dir=common_dir.resolve(), branch=branch)


@dataclass(frozen=True)
class SocketSelection:
    path: str
    warning: str = ""


def default_socket() -> str:
    """Return the socket pathname used by a plain tmux client."""
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp")
    return str(base / f"tmux-{os.geteuid()}" / "default")


def _tmux_socket(value: str) -> str | None:
    """Extract the socket path from tmux's `socket,pid,index` value."""
    parts = value.rsplit(",", 2)
    return parts[0] if len(parts) == 3 and parts[0] else None


def _supports_tiri_agent_status(path: str, tmux: str) -> bool:
    """Return whether this server evaluates tiri's agent-status format vars."""
    result = _run(
        [tmux, "-S", path, "list-panes", "-a", "-F", "#{pane_agent_state}"],
        check=False,
    )
    return result.returncode == 0 and any(
        line.strip() for line in result.stdout.splitlines()
    )


def discover_session(socket: str, *, tmux: str = "tmux") -> str | None:
    """Return the id of the session agentmon itself runs in, if any.

    Panes are monitored for that one session, so a window linked into a second
    session — `link-window`, a session group, a screenshot helper borrowing the
    window — is reported once instead of once per session it hangs in. Outside
    tiri, and on a socket other than the one holding this pane, there is no
    such session and the whole server is monitored as before.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = _run(
        [tmux, "-S", socket, "display-message", "-p", "-t", pane, "#{session_id}"],
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip() or None


def discover_socket(
    *, requested: str | None = None, tmux: str = "tmux"
) -> SocketSelection:
    """Select and verify an tiri/tmux-compatible server socket."""
    tiri_socket = os.environ.get("TIRI_SOCKET")
    tmux_socket = _tmux_socket(os.environ.get("TMUX", ""))
    candidates: list[tuple[str, str]] = []
    if requested:
        candidates.append((requested, "explicit"))
    elif tiri_socket:
        candidates.append((tiri_socket, "tiri"))
    elif tmux_socket:
        candidates.append((tmux_socket, "session"))
    else:
        default = Path(default_socket())
        candidates.append((str(default), "discovered"))
        try:
            siblings = sorted(default.parent.iterdir())
        except OSError:
            siblings = []
        for path in siblings:
            try:
                is_socket = stat.S_ISSOCK(path.stat().st_mode)
            except OSError:
                continue
            if is_socket and str(path) != str(default):
                candidates.append((str(path), "discovered"))

    failures: list[str] = []
    for path, _source in candidates:
        result = _run([tmux, "-S", path, "list-sessions"], check=False)
        if result.returncode == 0:
            warning = ""
            if not _supports_tiri_agent_status(path, tmux):
                warning = (
                    "Selected tmux server does not expose tiri agent status; "
                    "agent runs may not be visible."
                )
            return SocketSelection(path, warning)
        failures.append(path)

    detail = ", ".join(failures) if failures else "no candidate sockets"
    raise CommandError(
        "no reachable tiri socket found "
        f"({detail}); use --socket PATH, set TIRI_SOCKET, or start tiri at a "
        "tmux-discoverable socket"
    )


@dataclass(frozen=True)
class AgentmonContext:
    """Everything a client needs to talk to one tiri server about one repo.

    Repository and socket discovery used to live inside the dashboard's
    `main()`, which left scripts with no way to reach it. This bundles both
    lookups so any entry point can go from "a working directory" to a usable
    service in one call.
    """

    repository: Repository | None
    socket: str
    tmux: str = "tmux"
    warning: str = ""
    session: str | None = None

    def service(self) -> AgentmonService:
        return AgentmonService(
            self.repository,
            socket=self.socket,
            tmux=self.tmux,
            session=self.session,
        )


def discover_context(
    *,
    socket: str | None = None,
    tmux: str = "tmux",
    start: Path | None = None,
) -> AgentmonContext:
    """Locate the repository around `start` and a reachable tiri socket.

    A missing repository is reported as None rather than raised: panes outside
    a checkout are still worth monitoring. An unreachable socket raises, since
    nothing works without one.
    """
    try:
        repository = discover_repository(start or Path.cwd())
    except CommandError:
        repository = None
    selection = discover_socket(requested=socket, tmux=tmux)
    return AgentmonContext(
        repository=repository,
        socket=selection.path,
        tmux=tmux,
        warning=selection.warning,
        session=discover_session(selection.path, tmux=tmux),
    )


@dataclass(frozen=True)
class PaneStatus:
    """The agent identity and state tiri reports for a single pane."""

    pane_id: str
    agent: str
    state: str


@dataclass(frozen=True)
class WorktreeOverview:
    """A snapshot of a worktree that has no live agent session.

    It answers the two questions the dashboard cares about for an idle
    worktree: is an instruction prepared, and does anything sit on top of the
    instruction commit (which implies the instruction was already acted on).
    """

    worktree: Path
    branch: str
    base_branch: str
    merged: bool
    has_instruction: bool
    instruction_text: str
    instruction_commit: str | None
    committed_changes: bool
    dirty: bool


class AgentmonService:
    def __init__(
        self,
        repo: Repository | None,
        *,
        socket: str,
        tmux: str = "tmux",
        session: str | None = None,
    ) -> None:
        self.repo = repo
        self.socket = socket
        self.tmux = tmux
        # The session agentmon watches, or None to watch the whole server.
        self.session = session
        self._repositories = {} if repo is None else {repo.common_dir: repo}
        self._worktree_contexts = (
            {}
            if repo is None
            else {repo.root.resolve(): (repo, repo.branch)}
        )

    def for_repository(self, repo: Repository | None) -> AgentmonService:
        """Return a service whose Git operations are scoped to ``repo``."""
        if repo is None or (
            self.repo is not None and repo.common_dir == self.repo.common_dir
        ):
            return self
        return AgentmonService(
            repo, socket=self.socket, tmux=self.tmux, session=self.session
        )

    def for_run(self, run: AgentRun) -> AgentmonService:
        return self.for_repository(run.repository)

    def _pane_listing(self) -> list[str]:
        """Return the `list-panes` arguments for the panes agentmon monitors."""
        if self.session is None:
            return ["list-panes", "-a"]
        return ["list-panes", "-s", "-t", self.session]

    def suggested_worktree(self, branch: str) -> Path:
        # Keep worktrees as siblings while allowing conventional feature/foo refs.
        directory = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
        return self.repo.root.parent / (directory or "new-run")

    def _registered_worktrees(self) -> set[Path]:
        result = _run(
            ["git", "-C", str(self.repo.root), "worktree", "list", "--porcelain"]
        )
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        }

    def _validated_branch(self, branch: str) -> str:
        branch = branch.strip()
        if not branch:
            raise ValueError("Enter a branch name")
        check = _run(["git", "check-ref-format", "--branch", branch], check=False)
        if check.returncode:
            raise ValueError(check.stderr.strip() or "Invalid branch name")
        return branch

    def branch_exists(self, branch: str) -> bool:
        result = _run(
            [
                "git", "-C", str(self.repo.root), "show-ref", "--verify",
                "--quiet", f"refs/heads/{branch}",
            ],
            check=False,
        )
        return result.returncode == 0

    def validate_draft(self, branch: str, prompt: str) -> LaunchDraft:
        branch = self._validated_branch(branch)
        existing_branch = self.branch_exists(branch)
        worktree = self.suggested_worktree(branch)
        overwrite_worktree = False
        if worktree.exists():
            resolved = worktree.resolve()
            registered = self._registered_worktrees()
            if resolved == self.repo.root.resolve() or resolved not in registered:
                raise ValueError(f"ERROR: Cannot overwrite non-worktree path: {worktree}")
            if self._worktree_state(resolved) == "dirty":
                raise ValueError(f"ERROR: Cannot overwrite dirty worktree: {worktree}")
            overwrite_worktree = True
        if not prompt.strip():
            raise ValueError("Write an instruction before continuing")
        return LaunchDraft(
            branch=branch,
            worktree=worktree,
            prompt=prompt,
            overwrite_worktree=overwrite_worktree,
            existing_branch=existing_branch,
            repository=self.repo,
        )

    def prepare_worktree(
        self, branch: str, *, base: str | None = None, reset: bool = False
    ) -> Path:
        """Create a sibling worktree only; instruction and agent come later.

        ``base`` is the start-point a freshly forked branch inherits; when
        omitted the new branch forks from the main worktree's HEAD. Pass the
        cursor's worktree branch to fork from it instead of the project base.
        """
        branch = self._validated_branch(branch)
        worktree = self.suggested_worktree(branch)
        if worktree.exists():
            raise ValueError(f"Worktree path already exists: {worktree}")
        args = ["git", "-C", str(self.repo.root), "worktree", "add"]
        if reset:
            args.extend(["-B", branch, str(worktree)])
            if base is not None:
                args.append(base)
        elif self.branch_exists(branch):
            # Reuse the existing branch untouched; base does not apply.
            args.extend([str(worktree), branch])
        else:
            args.extend(["-b", branch, str(worktree)])
            if base is not None:
                args.append(base)
        _run(args)
        return worktree

    def _git_common_dir(self, cwd: Path) -> Path | None:
        result = _run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], check=False
        )
        if result.returncode:
            return None
        path = Path(result.stdout.strip())
        return (cwd / path).resolve() if not path.is_absolute() else path.resolve()

    def _branch_at(self, cwd: Path) -> str:
        result = _run(
            ["git", "-C", str(cwd), "branch", "--show-current"], check=False
        )
        return result.stdout.strip() or "(detached)"

    def _worktree_root(self, cwd: Path) -> Path:
        result = _run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
        return cwd.resolve()

    def _repository_for_worktree(
        self, cwd: Path, common_dir: Path
    ) -> Repository:
        cached = self._repositories.get(common_dir)
        if cached is not None:
            return cached
        worktree = self._worktree_root(cwd)
        project_root = common_dir.parent if common_dir.name == ".git" else worktree
        repository = Repository(
            root=project_root.resolve(),
            common_dir=common_dir,
            branch=self._branch_at(project_root),
        )
        self._repositories[common_dir] = repository
        return repository

    @staticmethod
    def _pane_directory(raw: str) -> Path:
        """Normalize Linux's marker for a process cwd whose directory was removed."""
        return Path(raw.removesuffix(" (deleted)")).resolve()

    def read_instruction(self, worktree: Path) -> str | None:
        """Return the worktree's instruction, or None when neither file exists."""
        for name in INSTRUCTION_FILENAMES:
            try:
                return (worktree / name).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
        return None

    def save_instruction(
        self, worktree: Path, branch: str, text: str | None = None
    ) -> str | None:
        """Persist instruction.md, retire prompt.md, and commit both.

        With ``text`` the content is written first; with ``None`` whatever is
        already on disk is committed. Returns the short commit hash, or None
        when nothing changed.
        """
        path = worktree / "instruction.md"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        if path.exists():
            _run(["git", "add", "instruction.md"], cwd=worktree)
        legacy = worktree / "prompt.md"
        if legacy.exists():
            _run(
                ["git", "rm", "-f", "--ignore-unmatch", "--", "prompt.md"],
                cwd=worktree,
            )
            legacy.unlink(missing_ok=True)
        status = _run(
            ["git", "status", "--porcelain", "--", *INSTRUCTION_FILENAMES],
            cwd=worktree,
        )
        # Committing by pathspec keeps unrelated staged changes out of the
        # commit; unknown names would make git error, so use what changed.
        changed = [line[3:] for line in status.stdout.splitlines() if len(line) > 3]
        if not changed:
            return None
        _run(
            [
                "git", "commit", "-m",
                f"agentmon: add instruction for {branch}",
                "--", *changed,
            ],
            cwd=worktree,
        )
        return _run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=worktree
        ).stdout.strip()

    def instruction_edit_target(self, run: AgentRun) -> Path:
        """Return the instruction.md to edit, seeded from a legacy prompt.md."""
        worktree = self._worktree_root(run.worktree)
        path = worktree / "instruction.md"
        if not path.exists():
            legacy_text = self.read_instruction(worktree)
            if legacy_text is not None:
                path.write_text(legacy_text, encoding="utf-8")
        return path

    def run_instruction(self, run: AgentRun) -> str | None:
        """Return the instruction text belonging to a dashboard run."""
        return self.read_instruction(self._worktree_root(run.worktree))

    def commit_instruction(self, run: AgentRun) -> str | None:
        """Commit the on-disk instruction.md after an external edit."""
        worktree = self._worktree_root(run.worktree)
        if not (worktree / "instruction.md").exists():
            return None
        return self.save_instruction(worktree, run.branch)

    def worktree_overview(self, run: AgentRun) -> WorktreeOverview:
        """Summarize an agentless worktree's instruction and change state.

        Used when no live agent session backs the selected worktree, so the
        transcript pane can show what the worktree holds instead of a
        dead-end note.
        """
        worktree = self._worktree_root(run.worktree)
        text = self.read_instruction(worktree)
        commit = _run(
            [
                "git", "-C", str(worktree), "log", "-1", "--format=%h", "--",
                *INSTRUCTION_FILENAMES,
            ],
            check=False,
        ).stdout.strip()
        instruction_commit = commit or None
        committed_changes = False
        if instruction_commit:
            counted = _run(
                [
                    "git", "-C", str(worktree), "rev-list", "--count",
                    f"{instruction_commit}..HEAD",
                ],
                check=False,
            ).stdout.strip()
            committed_changes = counted.isdigit() and int(counted) > 0
        status = _run(
            ["git", "-C", str(worktree), "status", "--porcelain"], check=False
        )
        # Compute the merged/unmerged ancestry independently of dirtiness so the
        # detail still explains the left pane's mark even when uncommitted
        # changes would otherwise collapse it to "dirty".
        merged = _run(
            [
                "git", "-C", str(worktree), "merge-base", "--is-ancestor", "HEAD",
                self.repo.branch,
            ],
            check=False,
        ).returncode == 0
        return WorktreeOverview(
            worktree=worktree,
            branch=run.branch or self._branch_at(worktree),
            base_branch=self.repo.branch,
            merged=merged,
            has_instruction=text is not None,
            instruction_text=text or "",
            instruction_commit=instruction_commit,
            committed_changes=committed_changes,
            dirty=bool(status.stdout.strip()),
        )

    def _prompt_preview(self, cwd: Path, limit: int = 120) -> str:
        """Return a single-line glimpse of the instruction belonging to this run."""
        text = self.read_instruction(self._worktree_root(cwd))
        if text is None:
            return "—"
        preview = " ".join(text.split())
        if not preview:
            return "(empty)"
        return preview if len(preview) <= limit else preview[: limit - 1].rstrip() + "…"

    def _worktree_state(self, cwd: Path) -> str:
        """Describe whether a worktree needs attention before removal."""
        status = _run(
            ["git", "-C", str(cwd), "status", "--porcelain"], check=False
        )
        if status.returncode:
            return "unknown"
        if status.stdout:
            return "dirty"
        merged = _run(
            [
                "git", "-C", str(cwd), "merge-base", "--is-ancestor", "HEAD",
                self.repo.branch,
            ],
            check=False,
        )
        return "merged" if merged.returncode == 0 else "unmerged"

    def _session_names(self) -> set[str]:
        result = _run(
            [
                self.tmux,
                "-S",
                self.socket,
                "list-sessions",
                "-F",
                "#{session_name}",
            ]
        )
        return {line for line in result.stdout.splitlines() if line}

    def _session_ids(self) -> set[str]:
        result = _run(
            [
                self.tmux,
                "-S",
                self.socket,
                "list-sessions",
                "-F",
                "#{session_id}",
            ],
            check=False,
        )
        if result.returncode:
            return set()
        return {line for line in result.stdout.splitlines() if line}

    def _watched_sessions(self) -> set[str]:
        """Return the sessions to hold a control client on.

        Intersecting with the live sessions matters for the scoped case: once
        agentmon's own session is gone there is nothing left to attach to, and
        without this the watcher would retry a dead target forever.
        """
        if self.session is None:
            return self._session_names()
        return {self.session} & self._session_ids()

    def watch_runs(
        self, on_change: Callable[[], None], stop: threading.Event
    ) -> None:
        """Notify when tmux reports a dashboard-relevant format change."""
        selector = selectors.DefaultSelector()
        clients: dict[str, subprocess.Popen[bytes]] = {}
        buffers: dict[str, bytearray] = {}
        command = _subscription_command().encode()

        def remove(session: str) -> None:
            process = clients.pop(session, None)
            buffers.pop(session, None)
            if process is None:
                return
            if process.stdout is not None:
                try:
                    selector.unregister(process.stdout)
                except KeyError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()

        def add(session: str) -> None:
            try:
                process = subprocess.Popen(
                    [
                        self.tmux,
                        "-S",
                        self.socket,
                        "-C",
                        "attach-session",
                        "-f",
                        "no-output,ignore-size",
                        "-t",
                        session,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except OSError as exc:
                raise CommandError(str(exc)) from exc
            if process.stdin is None or process.stdout is None:
                process.terminate()
                raise CommandError("tmux control client did not expose pipes")
            try:
                process.stdin.write(command)
                process.stdin.flush()
            except OSError as exc:
                process.terminate()
                process.wait()
                raise CommandError(str(exc)) from exc
            os.set_blocking(process.stdout.fileno(), False)
            clients[session] = process
            buffers[session] = bytearray()
            selector.register(process.stdout, selectors.EVENT_READ, session)

        def reconcile() -> None:
            sessions = self._watched_sessions()
            for session in set(clients) - sessions:
                remove(session)
            for session in sessions - set(clients):
                add(session)

        try:
            reconcile()
            next_reconcile = time.monotonic() + 10
            while not stop.is_set():
                changed = False
                needs_reconcile = False
                for key, _mask in selector.select(timeout=0.5):
                    session = key.data
                    process = clients.get(session)
                    if process is None or process.stdout is None:
                        continue
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        remove(session)
                        needs_reconcile = True
                        changed = True
                        continue
                    buffer = buffers[session]
                    buffer.extend(chunk)
                    while (newline := buffer.find(b"\n")) >= 0:
                        line = bytes(buffer[: newline + 1]).decode(
                            "utf-8", errors="replace"
                        )
                        del buffer[: newline + 1]
                        if _run_change_notification(line):
                            changed = True
                        if line.startswith(("%sessions-changed", "%session-closed ")):
                            needs_reconcile = True

                now = time.monotonic()
                if needs_reconcile or now >= next_reconcile:
                    reconcile()
                    next_reconcile = now + 10
                if changed:
                    on_change()
        finally:
            for session in list(clients):
                remove(session)
            selector.close()

    def runs(self) -> list[AgentRun]:
        result = _run(
            [self.tmux, "-S", self.socket, *self._pane_listing(), "-F", RUN_FORMAT]
        )
        windows: dict[str, list[tuple[tuple[bool, bool], list[str]]]] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t", 8)
            if len(parts) != 9:
                continue
            (
                _pane_id,
                location,
                _name,
                agent,
                _state,
                cwd_raw,
                _session_id,
                active,
                _command,
            ) = parts
            if not cwd_raw:
                continue
            window_location = location.rsplit(".", 1)[0]
            score = (bool(agent), active == "1")
            windows.setdefault(window_location, []).append((score, parts))

        runs: list[AgentRun] = []
        for window_location, candidates in windows.items():
            selected: tuple[list[str], Path, Path] | None = None
            ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
            # An agent pane must represent its window even when its cwd is not
            # a repository, so the repository preference below never reaches
            # past the agent panes to an agentless one.
            if ordered[0][0][0]:
                ordered = [item for item in ordered if item[0][0]]
            for _score, parts in ordered:
                cwd = self._pane_directory(parts[5])
                common_dir = self._git_common_dir(cwd)
                if common_dir is not None:
                    selected = (parts, cwd, common_dir)
                    break
            if selected is None:
                parts = ordered[0][1]
                cwd = self._pane_directory(parts[5])
                common_dir = None
            else:
                parts, cwd, common_dir = selected
            (
                _pane_id,
                _location,
                window_name,
                agent,
                state,
                _cwd_raw,
                session_id,
                _active,
                pane_command,
            ) = parts
            # tiri only reports an agent name for recognized coding agents. When
            # a pane has none, distinguish an idle shell (badge "----") from any
            # other running program (badge "????") by its foreground command.
            effective_agent = agent or (
                "window" if _is_shell_command(pane_command) else "app"
            )
            if common_dir is None:
                cached_context = self._worktree_contexts.get(cwd)
                if cached_context is None:
                    repository = None
                    branch = cwd.name or str(cwd)
                    worktree_state = "not-git"
                else:
                    repository, branch = cached_context
                    worktree_state = "unknown"
                prompt_preview = "—"
            else:
                repository = self._repository_for_worktree(cwd, common_dir)
                scoped_service = self.for_repository(repository)
                branch = self._branch_at(cwd)
                prompt_preview = self._prompt_preview(cwd)
                worktree_state = scoped_service._worktree_state(cwd)
                self._worktree_contexts[cwd] = (repository, branch)
            runs.append(
                AgentRun(
                    pane_id=f"window:{window_location}",
                    location=window_location,
                    branch=branch,
                    state=state or "none",
                    agent=effective_agent,
                    worktree=cwd,
                    prompt_preview=prompt_preview,
                    worktree_state=worktree_state,
                    session_id=session_id or None,
                    repository=repository,
                    window_name=window_name,
                    tmux_pane_id=_pane_id,
                )
            )
        return runs

    def run_transcript(self, run: AgentRun) -> Transcript:
        """Load the local user-interaction transcript for an active agent run."""
        if not run.session_id:
            raise TranscriptError("This run does not expose an agent session id yet")
        if run.agent == "codex":
            return codex_transcript.load_transcript(run.session_id)
        if run.agent in {"claude", "claude-code"}:
            return claude_transcript.load_transcript(run.session_id)
        if run.agent == "agy":
            # agy keeps conversations in sqlite rather than a JSONL transcript;
            # reading it is a task of its own. The run is still monitored, it
            # just has nothing to show here.
            raise TranscriptError(
                "agy stores its conversation in a database agentmon cannot read yet"
            )
        raise TranscriptError(f"Unsupported transcript agent: {run.agent}")

    def recent_finished(self, active: list[AgentRun], limit: int = 5) -> list[AgentRun]:
        """Recover registered worktrees that no tiri window is sitting in.

        This covers both finished agent runs and freshly prepared worktrees
        that have no instruction yet, so the prepare/instruct/launch commands
        can target them.
        """
        active_paths = {run.worktree.resolve() for run in active}
        candidates: list[tuple[int, AgentRun]] = []
        for worktree in self._registered_worktrees():
            if worktree == self.repo.root.resolve() or any(
                worktree == path or worktree in path.parents for path in active_paths
            ):
                continue
            if self._git_common_dir(worktree) != self.repo.common_dir:
                continue
            timestamp = _run(
                [
                    "git", "-C", str(worktree), "log", "-1", "--format=%ct", "--",
                    *INSTRUCTION_FILENAMES,
                ],
                check=False,
            ).stdout.strip()
            if not timestamp.isdigit():
                timestamp = _run(
                    ["git", "-C", str(worktree), "log", "-1", "--format=%ct"],
                    check=False,
                ).stdout.strip()
            if not timestamp.isdigit():
                continue
            candidates.append(
                (
                    int(timestamp),
                    AgentRun(
                        pane_id=f"finished:{worktree}",
                        location="0:",
                        branch=self._branch_at(worktree),
                        state="exited",
                        agent="finished",
                        worktree=worktree,
                        prompt_preview=self._prompt_preview(worktree),
                        worktree_state=self._worktree_state(worktree),
                        repository=self.repo,
                    ),
                )
            )
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [run for _, run in candidates[:limit]]

    def recent_finished_all(
        self, active: list[AgentRun], limit_per_repository: int = 5
    ) -> list[AgentRun]:
        """Recover finished runs for every repository represented by active panes."""
        repositories = (
            {} if self.repo is None else {self.repo.common_dir: self.repo}
        )
        for run in active:
            if run.repository is not None:
                repositories[run.repository.common_dir] = run.repository

        finished: list[AgentRun] = []
        for common_dir, repository in repositories.items():
            scoped = self.for_repository(repository)
            repository_active = [
                run
                for run in active
                if run.repository is not None
                and run.repository.common_dir == common_dir
            ]
            finished.extend(
                scoped.recent_finished(
                    repository_active,
                    limit=limit_per_repository,
                )
            )
        return finished

    def select_window(self, run: AgentRun) -> None:
        if run.state == "exited":
            raise CommandError("The agent window is closed; press w to open a shell at its worktree")
        window = run.location.split(":", 1)[1].split(".", 1)[0]
        session = run.location.split(":", 1)[0]
        _run([self.tmux, "-S", self.socket, "select-window", "-t", f"{session}:{window}"])

    @staticmethod
    def _tmux_pane_target(run: AgentRun) -> str:
        """Use the selected agent pane when a window contains several panes."""
        return run.tmux_pane_id or run.location

    def rate_limit_options_visible(self, run: AgentRun) -> bool:
        """Return whether a Claude run is showing its rate-limit options menu."""
        if run.agent not in {"claude", "claude-code"} or run.state == "exited":
            return False
        result = _run(
            [
                self.tmux,
                "-S",
                self.socket,
                "capture-pane",
                "-p",
                "-t",
                self._tmux_pane_target(run),
            ],
            check=False,
        )
        return result.returncode == 0 and is_claude_rate_limit_options_dialog(
            result.stdout
        )

    def auto_wait_if_rate_limited(self, run: AgentRun) -> bool:
        """Confirm Claude's wait option only if its rate-limit menu is visible."""
        if not self.rate_limit_options_visible(run):
            return False
        result = _run(
            [
                self.tmux,
                "-S",
                self.socket,
                "send-keys",
                "-t",
                self._tmux_pane_target(run),
                "Home",
                "Enter",
            ],
            check=False,
        )
        return result.returncode == 0

    def pane_status(self, pane: str) -> PaneStatus | None:
        """Return one pane's agent status, or None once the pane is gone.

        A dead server is reported the same way as a closed pane: either way the
        pane can no longer reach a state, which is what callers are waiting on.
        """
        result = _run(
            [
                self.tmux, "-S", self.socket, *self._pane_listing(), "-F",
                PANE_STATUS_FORMAT,
            ],
            check=False,
        )
        if result.returncode:
            return None
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0] == pane:
                return PaneStatus(
                    pane_id=parts[0], agent=parts[1], state=parts[2] or "none"
                )
        return None

    def wait_for_pane_state(
        self,
        pane: str,
        states: Collection[str],
        *,
        timeout: float | None = None,
        poll: float = 2.0,
    ) -> str | None:
        """Block until `pane` reaches one of `states`, then return that state.

        A pane that disappears returns "exited" whatever `states` asked for,
        because a closed pane can never reach them. `timeout` of None waits
        indefinitely; when it elapses first the return is None.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.pane_status(pane)
            if status is None:
                return "exited"
            if status.state in states:
                return status.state
            if deadline is not None and time.monotonic() >= deadline:
                return None
            remaining = (
                poll if deadline is None else min(poll, deadline - time.monotonic())
            )
            if remaining > 0:
                time.sleep(remaining)

    def split_agent_pane(
        self,
        *,
        target: str,
        worktree: Path,
        agent: str,
        model: str = "default",
        effort: str = "default",
        instruction_file: Path | None = None,
        size_percent: int = 75,
        devshell: bool = False,
        focus: bool = False,
    ) -> str:
        """Split `target`'s window and start an agent in the new bottom pane.

        The split is detached by default so the caller — typically a loop
        runner printing into `target` — keeps the focus it already had. With
        `focus`, the agent pane becomes the active one instead, which is what
        window-level status such as the agent state follows. Returns the new
        pane id.
        """
        self._prepare_agent_workspace(agent, worktree)
        command = self._agent_command(
            agent,
            model,
            effort,
            with_instruction=instruction_file is not None,
            instruction_file=str(instruction_file) if instruction_file else "",
            devshell=devshell,
        )
        with self._pane_environment(command) as pane_command:
            result = _run(
                [
                    self.tmux, "-S", self.socket, "split-window",
                    *([] if focus else ["-d"]),
                    "-v", "-t", target, "-l", f"{size_percent}%", "-c", str(worktree),
                    "-P", "-F", "#{pane_id}", pane_command,
                ]
            )
            pane = result.stdout.strip()
            if not pane:
                raise CommandError("tiri did not report a pane id for the new split")
        return pane

    def exit_agent_pane(
        self,
        pane: str,
        *,
        timeout: float = 15.0,
        poll: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Ask the agent in `pane` to quit, killing the pane if it will not.

        Interactive agents never exit on their own, so a loop that decides a
        run is done has to end it. Panes started with `exec agent…` close when
        the agent quits, which is the confirmation waited on here. Returns
        whether the agent quit on its own, as opposed to the kill-pane
        fallback.

        Only for an agent already sitting at its prompt. One still working
        reads a typed command as its next message rather than a command, and
        is killed outright instead.
        """
        self._type_exit_command(pane, sleep=sleep)
        retry_after = min(timeout, EXIT_RETRY_SECONDS)
        if self.wait_for_pane_state(pane, ("exited",), timeout=retry_after, poll=poll):
            return True
        # A swallowed Enter leaves the command sitting in the composer, which
        # looks the same from here as an agent taking its time. Another Enter
        # submits it; at an already-clear prompt it costs an empty submit.
        self._send_keys(pane, "Enter")
        if self.wait_for_pane_state(
            pane, ("exited",), timeout=max(0.0, timeout - retry_after), poll=poll
        ):
            return True
        self.kill_agent_pane(pane)
        return False

    def kill_agent_pane(self, pane: str) -> None:
        """Take the pane down without asking the agent."""
        _run([self.tmux, "-S", self.socket, "kill-pane", "-t", pane], check=False)

    def _type_exit_command(
        self, pane: str, *, sleep: Callable[[float], None]
    ) -> None:
        """Type the exit command as three keystrokes an agent TUI can tell apart."""
        # Whatever the agent left in its composer would otherwise turn the
        # command into `…/exit`.
        self._send_keys(pane, "C-u")
        sleep(KEYSTROKE_GAP_SECONDS)
        self._send_keys(pane, "-l", AGENT_EXIT_COMMAND)
        sleep(KEYSTROKE_GAP_SECONDS)
        self._send_keys(pane, "Enter")

    def _send_keys(self, pane: str, *keys: str) -> None:
        _run(
            [self.tmux, "-S", self.socket, "send-keys", "-t", pane, *keys],
            check=False,
        )

    def commit_all_changes(self, worktree: Path, message: str) -> str | None:
        """Stage and commit everything in `worktree`; None when nothing changed.

        Used to harvest an agent run that left work uncommitted. The staged
        check after `add -A` covers dirt that stages to nothing, which would
        otherwise make `git commit` fail.
        """
        status = _run(
            ["git", "-C", str(worktree), "status", "--porcelain"], check=False
        )
        if status.returncode or not status.stdout.strip():
            return None
        _run(["git", "-C", str(worktree), "add", "-A"])
        staged = _run(
            ["git", "-C", str(worktree), "diff", "--cached", "--quiet"], check=False
        )
        if staged.returncode == 0:
            return None
        _run(["git", "-C", str(worktree), "commit", "-m", message])
        return _run(
            ["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"]
        ).stdout.strip()

    def open_shell_window(self, run: AgentRun) -> str:
        """Create and select a shell window in the agent's current directory."""
        session = run.location.split(":", 1)[0]
        result = _run(
            [
                self.tmux,
                "-S",
                self.socket,
                "new-window",
                "-t",
                f"{session}:",
                "-P",
                "-F",
                "#{session_name}:#{window_index}",
                "-c",
                str(run.worktree),
            ]
        )
        return result.stdout.strip()

    def cleanup_worktree(self, run: AgentRun) -> None:
        """Remove a finished worktree after rechecking that cleanup is safe."""
        worktree = run.worktree.resolve()
        if run.state != "exited":
            raise CommandError("Only finished agent worktrees can be cleaned up")
        if worktree == self.repo.root.resolve() or worktree not in self._registered_worktrees():
            raise CommandError(f"Not a removable registered worktree: {worktree}")
        state = self._worktree_state(worktree)
        if state != "merged":
            raise CommandError(
                f"Worktree cleanup refused: state is {state}, not clean and merged"
            )
        _run(["git", "-C", str(self.repo.root), "worktree", "remove", str(worktree)])

    def restart_draft(self, run: AgentRun) -> LaunchDraft:
        """Build a new launch from a finished, safely replaceable run."""
        worktree = run.worktree.resolve()
        if run.state != "exited":
            raise CommandError("Only finished agent runs can be restarted")
        if worktree == self.repo.root.resolve() or worktree not in self._registered_worktrees():
            raise CommandError(f"Not a removable registered worktree: {worktree}")
        state = self._worktree_state(worktree)
        if state != "merged":
            raise CommandError(
                f"Worktree cleanup refused: state is {state}, not clean and merged"
            )
        prompt = self.read_instruction(worktree)
        if prompt is None:
            raise CommandError(f"Could not read {worktree / 'instruction.md'}")
        return LaunchDraft(
            branch=run.branch,
            worktree=worktree,
            prompt=prompt,
            overwrite_worktree=True,
            restart_worktree=True,
            repository=self.repo,
        )

    def populate_draft(self, run: AgentRun) -> LaunchDraft:
        """Build a launch form draft from a selected run's instruction."""
        worktree = self._worktree_root(run.worktree)
        agent = normalize_launch_agent(run.agent)
        prompt = self.read_instruction(worktree)
        if prompt is None:
            raise CommandError(f"Could not read {worktree / 'instruction.md'}")

        registered = self._registered_worktrees()
        if (
            run.state == "exited"
            and worktree != self.repo.root.resolve()
            and worktree in registered
            and self._worktree_state(worktree) == "merged"
        ):
            return LaunchDraft(
                branch=run.branch,
                worktree=worktree,
                prompt=prompt,
                overwrite_worktree=True,
                restart_worktree=True,
                agent=agent,
                repository=self.repo,
            )

        return LaunchDraft(
            branch=run.branch,
            worktree=self.suggested_worktree(run.branch),
            prompt=prompt,
            agent=agent,
            repository=self.repo,
        )

    def prompt_history(self, limit: int = 50) -> list[PromptHistory]:
        result = _run(
            [
                "git", "-C", str(self.repo.root), "log", "--all",
                f"--max-count={limit}", "--format=%H%x09%cs%x09%s", "--",
                *INSTRUCTION_FILENAMES,
            ]
        )
        history: list[PromptHistory] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            commit, date, subject = parts
            for name in INSTRUCTION_FILENAMES:
                shown = _run(
                    ["git", "-C", str(self.repo.root), "show", f"{commit}:{name}"],
                    check=False,
                )
                if shown.returncode == 0:
                    history.append(PromptHistory(commit, date, subject, shown.stdout))
                    break
        return history

    def launch(self, draft: LaunchDraft, progress: Callable[[LaunchStep], None]) -> str:
        if draft.overwrite_worktree:
            state = self._worktree_state(draft.worktree)
            if draft.restart_worktree:
                if state != "merged":
                    raise CommandError(
                        f"Worktree cleanup refused: state is {state}, not clean and merged"
                    )
            elif state == "dirty":
                raise CommandError(
                    f"Worktree overwrite refused: state is {state}, not clean"
                )
            _run(
                [
                    "git", "-C", str(self.repo.root), "worktree", "remove",
                    *([] if draft.restart_worktree else ["--force"]),
                    str(draft.worktree),
                ]
            )
            progress(LaunchStep("Existing worktree removed", str(draft.worktree)))
        worktree_args = ["git", "-C", str(self.repo.root), "worktree", "add"]
        if draft.restart_worktree:
            worktree_args.extend(["-B", draft.branch, str(draft.worktree)])
        elif draft.existing_branch:
            worktree_args.extend([str(draft.worktree), draft.branch])
        else:
            worktree_args.extend(["-b", draft.branch, str(draft.worktree)])
        _run(worktree_args)
        progress(LaunchStep("Branch and worktree created", str(draft.worktree)))

        commit = self.save_instruction(draft.worktree, draft.branch, draft.prompt)
        progress(LaunchStep("instruction.md committed", commit or "unchanged"))

        self._prepare_agent_workspace(draft.agent, draft.worktree)
        command = self._agent_command(
            draft.agent, draft.model, draft.effort, devshell=draft.devshell
        )
        window = self._open_agent_window(draft.worktree, draft.branch, command)
        progress(LaunchStep("tiri window and agent started", window))
        return window

    def launch_agent(
        self,
        run: AgentRun,
        *,
        agent: str,
        model: str = "default",
        effort: str = "default",
        instruction: str = "",
        devshell: bool = False,
    ) -> str:
        """Start an agent in an existing worktree, without touching branches."""
        worktree = self._worktree_root(run.worktree)
        with_instruction = bool(instruction.strip())
        if with_instruction:
            self.save_instruction(worktree, run.branch, instruction)
        self._prepare_agent_workspace(agent, worktree)
        command = self._agent_command(
            agent,
            model,
            effort,
            with_instruction=with_instruction,
            devshell=devshell,
        )
        return self._open_agent_window(worktree, run.branch, command)

    def _open_agent_window(self, worktree: Path, name: str, command: str) -> str:
        # The window has to land in the session agentmon lists, or the run it
        # just started would never show up on the dashboard.
        target = f"{self.session}:" if self.session is not None else "0:"
        with self._pane_environment(command) as pane_command:
            result = _run(
                [
                    self.tmux, "-S", self.socket, "new-window", "-d", "-t", target,
                    "-P", "-F", "#{window_index}", "-n", name,
                    "-c", str(worktree), pane_command,
                ]
            )
        return result.stdout.strip()

    @contextmanager
    def _pane_environment(self, command: str) -> Iterator[str]:
        """Run `command` with the caller's environment, spooled to a file.

        The pane sources the spool and removes it, so the values never reach a
        command line `ps` would show, and nothing is left behind once the agent
        is running. `set -a` is what exports them: the file holds plain
        assignments, so a value needs no escaping beyond shell quoting.

        A spawn that fails leaves a pane that will never read the spool, so the
        file is removed here instead — it holds whatever credentials the
        caller's environment does.
        """
        script = _environment_script()
        if not script:
            yield command
            return
        spool = _spool_environment(script)
        quoted = shlex.quote(str(spool))
        try:
            yield f"set -a; . {quoted}; set +a; rm -f {quoted}; {command}"
        except BaseException:
            spool.unlink(missing_ok=True)
            raise

    def _prepare_agent_workspace(self, agent: str, worktree: Path) -> None:
        """Do whatever `agent` needs before it can be started in `worktree`.

        A failure here is fatal to the launch on purpose: the setup it performs
        is what keeps the agent from stopping on a dialog nobody is watching.
        """
        if agent != "agy":
            return
        try:
            trust_agy_workspace(worktree)
        except OSError as exc:
            raise CommandError(f"Could not record {worktree} as trusted for agy: {exc}")

    def _agent_command(
        self,
        agent: str,
        model: str = "default",
        effort: str = "default",
        *,
        with_instruction: bool = True,
        instruction_file: str = "",
        devshell: bool = False,
    ) -> str:
        # Agents differ in how an initial prompt is handed over: codex and
        # claude take it as a trailing positional argument, agy behind `-i`.
        instruction_flag = ""
        if agent == "codex":
            parts = ["codex", "--yolo"]
            if model != "default":
                parts.extend(["-m", model])
            if effort != "default":
                parts.extend(["-c", f"model_reasoning_effort={effort}"])
        elif agent == "claude":
            parts = ["claude", "--dangerously-skip-permissions"]
            if model != "default":
                parts.extend(["--model", model])
        elif agent == "agy":
            parts = ["agy", "--dangerously-skip-permissions"]
            if model != "default":
                parts.extend(["--model", model])
            if effort != "default":
                parts.extend(["--effort", effort])
            instruction_flag = "-i"
        else:
            raise CommandError(f"Unsupported launch agent: {agent}")
        invocation = shlex.join(parts)
        if with_instruction:
            # The instruction normally travels with the worktree, so the
            # relative default resolves against the pane's start directory. A
            # loop runner's prompt lives outside the worktree instead.
            source = shlex.quote(instruction_file or "instruction.md")
            prefix = f" {instruction_flag}" if instruction_flag else ""
            invocation += f'{prefix} "$(cat {source})"'
        if not devshell:
            return "exec " + invocation
        # The dev shell is what pins the agent's toolchain, so entering it is
        # not optional: a shell that will not build ends the pane carrying
        # nix's own diagnostic, rather than starting the agent with whatever
        # environment the pane happened to inherit. `.` pins the flake to the
        # worktree instead of letting nix search parent directories.
        return f"exec {DEVSHELL_PREFIX} {invocation}"
