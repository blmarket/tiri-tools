"""Demo that prints user interactions from a coding-agent transcript."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from . import claude_transcript, transcript
from .transcript import Transcript, TranscriptError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo",
        description="Print user interactions for a local coding-agent session.",
    )
    parser.add_argument("session_id", help="Codex or Claude Code session UUID")
    parser.add_argument(
        "--agent",
        choices=("codex", "claude"),
        default="codex",
        help="which agent's transcript to read (default: codex)",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex data directory (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--claude-home",
        type=Path,
        help="Claude Code config directory (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    return parser


def _load(args: argparse.Namespace) -> Transcript:
    if args.agent == "claude":
        return claude_transcript.load_transcript(args.session_id, home=args.claude_home)
    return transcript.load_transcript(args.session_id, home=args.codex_home)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        loaded = _load(args)
    except TranscriptError as error:
        print(f"demo: {error}", file=sys.stderr)
        return 1

    print(f"Session: {loaded.session_id}")
    print(f"Rollout: {loaded.rollout_path}")
    if loaded.skipped_lines:
        lines = ", ".join(str(line) for line in loaded.skipped_lines)
        print(f"Warning: skipped malformed JSONL line(s): {lines}")

    for message in loaded.messages:
        timestamp = f" {message.timestamp}" if message.timestamp else ""
        print(f"\n[{message.role.upper()}{timestamp}]")
        print(message.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
