from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


AGENT_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "agy": "Antigravity",
}


@dataclass(frozen=True)
class Repository:
    root: Path
    common_dir: Path
    branch: str

    @property
    def project_root(self) -> Path:
        """Return the main worktree that owns this repository's common Git data."""
        if self.common_dir.name == ".git":
            return self.common_dir.parent
        return self.root


@dataclass(frozen=True)
class AgentRun:
    pane_id: str
    location: str
    branch: str
    state: str
    agent: str
    worktree: Path
    prompt_preview: str = "—"
    worktree_state: str = "unknown"
    session_id: str | None = None
    repository: Repository | None = None
    window_name: str = ""
    tmux_pane_id: str | None = None
