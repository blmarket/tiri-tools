"""Start, stop and harvest coding agents in tiri panes.

The dashboard only watches agents; starting one is `looper`'s job. What looper
needs to do it lives here, on top of the shared `AgentmonService`, so the
monitoring side never grows a way to spawn anything.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from .services import AgentmonContext, AgentmonService, CommandError, _run


# Entering a nix dev shell is deferred into the agent pane: a cold flake can
# take minutes to build, and there the wait and any error are visible instead
# of stalling whoever asked for it.
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


class AgentLauncher(AgentmonService):
    """An `AgentmonService` that can also put an agent in a pane and take it out."""

    @classmethod
    def from_context(cls, context: AgentmonContext) -> AgentLauncher:
        return cls(
            context.repository,
            socket=context.socket,
            tmux=context.tmux,
            session=context.session,
        )

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
