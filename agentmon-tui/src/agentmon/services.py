from __future__ import annotations

import os
import selectors
import shlex
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from . import claude_transcript, transcript as codex_transcript
from .model import AgentRun, Repository
from .transcript import Transcript, TranscriptError


class CommandError(RuntimeError):
    pass


# An agent's instruction lives in instruction.md; prompt.md is the legacy name
# still honored when reading so older worktrees keep working.
INSTRUCTION_FILENAMES = ("instruction.md", "prompt.md")

# Foreground commands tiri reports for a bare interactive shell. Anything else
# running in an agentless pane is treated as "some app" rather than an idle
# shell, so it earns a distinct badge in the dashboard.
SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "ash"}
)


def _is_shell_command(command: str) -> bool:
    """Return whether a pane's foreground command is just an interactive shell."""
    # Older tiri builds do not expose pane_current_command. Preserve their
    # previous agentless-window behavior instead of labelling every pane an app.
    if not command:
        return True
    # Login shells are reported with a leading dash (e.g. "-zsh").
    return command.lstrip("-") in SHELL_COMMANDS


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

    def _registered_worktrees(self) -> set[Path]:
        result = _run(
            ["git", "-C", str(self.repo.root), "worktree", "list", "--porcelain"]
        )
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        }

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

        This covers both finished agent runs and worktrees no agent has run in
        yet, so the dashboard still shows them and their instruction state.
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

