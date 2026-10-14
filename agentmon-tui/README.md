# tiri-agentmon

`agentmon` is a reviewable TUI for creating coding-agent runs in sibling git
worktrees and monitoring their real tiri agent status. `looper` is a headless
companion that re-runs one prompt for as long as quota pacing allows.

## HOWTO

From the repository root, run `nix run .#agentmon`, or enter `nix develop`
and run `agentmon`. To work on the Python app directly:

```sh
cd agentmon-tui
uv run agentmon
```

Run the test suite with `uv run --locked pytest` from this directory.
`nix build .#agentmon` from the repository root builds the package and runs its
tests. The companion loop runner is also available as `nix run .#looper --
prompt.md`.

See command palette(Ctrl + P) for list of available commands.

Runs are monitored for the session `agentmon` itself sits in, and new runs are
opened there too. A window linked into more than one session therefore appears
once rather than once per session. Started outside tiri — from a plain terminal
with `--socket` or `TIRI_SOCKET` — there is no such session, so every session on
the server is monitored and new runs go to session `0`.

## looper

Run it from a pane inside tiri, in the worktree you want worked on:

```sh
looper prompt.md          # or: cat prompt.md | looper
```

`looper` splits its own window and gives the bottom three quarters to the
agent, keeping the top for its log. Each cycle waits for pacing, starts the
agent on the prompt, ends the run once the agent is done, and commits whatever
the run changed. `nix develop` puts `looper` on `$PATH`; from this directory
`uv run looper` works too.

A named prompt file is read again at the start of every run, so a prompt the
agent rewrites — a checklist it ticks off, a plan it revises — steers the next
run. It also stays where you put it: inside the worktree, its edits are
committed along with the rest of the run. A piped prompt is spooled outside the
worktree instead and is fixed for the life of the loop. Naming a file wins over
anything on standard input.

`--preset` chooses what runs. `codex` (the default) is codex with
`gpt-5.6-luna` at effort `max`; `sol` is codex with `gpt-5.6-sol` at effort
`high`; `agy` is the Antigravity CLI with
`gemini-3.7-flash` at effort `high`; `claude` is Claude Code on `claude-opus-5`,
which takes no effort flag. Launching `agy` also records the worktree in the
CLI's `trustedWorkspaces`, since it would otherwise stop on its trust dialog in
a worktree it has never seen.

### Pacing

Pacing compares each of the agent provider's quota windows against the clock: a
window is on pace while the share of it spent is no larger than the share of it
elapsed. Over pace, `looper` sleeps until an even burn would have caught up —
for a fully spent window, that is its reset, so an exhausted quota simply means
a long wait rather than a new run. Every window must be on pace before a run
starts, and only the agent's own provider gates it: a spent Claude
subscription has no bearing on a codex loop.

`-f` skips pacing altogether: quota is never consulted and the runs go back to
back, so `looper -f -n 2 prompt.md` gives exactly two runs no matter what
the windows say.

Two deliberate non-blockers: a window the provider left undated cannot be
paced, and a provider that reported nothing at all (expired credentials, say)
leaves nothing to pace against. Both are passed over with a note in the log
rather than stalling the loop forever.

### Quota sources

Windows come from each provider's own usage endpoint, read with the OAuth
token that provider's CLI already stores, and are cached for five minutes in
`$XDG_CACHE_HOME/agentmon/quota.json` so the TUI dialog and any number of
loops share one lookup.

While the quota dialog is open, it refreshes five minutes after each load
finishes, plus a fresh random delay of up to 30 seconds. The previous report
stays visible during refresh. Press `r` to bypass the cache and refresh now.
During an automatic load, repeated presses queue one forced refresh; during
a forced load, they reuse that request. Closing the dialog stops automatic
refreshes.

Antigravity reports what is left rather than what is spent, and splits its
quota into a Gemini group and one for the Claude and GPT models it can proxy.
Only the Gemini windows are surfaced, since those are the ones an `agy` run on
a Gemini model spends. Its token lives about an hour and only `agy` itself
refreshes it, so quota reads out of a long-idle install fail until the next
run; the dialog and the log say so rather than guessing.

### Ending a run

An interactive agent never exits on its own — "finished" means it is sitting
idle at its prompt — so `looper` is what ends the run, with `/exit` and a
`kill-pane` fallback. An agent that goes `blocked` wants a human instead: the
loop stops there and leaves the pane up for you, without committing.

`/exit` is typed as separate keystrokes with a pause before its Enter. Sent as
one burst it reads as a paste, where a newline is inserted rather than
submitted — the command lands in the composer and the agent never quits, which
shows up only as the `kill-pane` fallback firing.

A run is also ended once it overstays `--run-timeout`, two hours by default:
past that a run has usually stopped making progress, so the loop ends it, keeps
whatever it committed, and starts the next one. `--run-timeout 0` waits forever
instead. The clock starts once the pane is seen working, so a run can occupy up
to the startup wait plus this.

A timed-out agent is killed rather than asked. It is still mid-turn, where a
typed command is queued as its next message instead of being read as one, so
there is nothing `/exit` can do until the turn unwinds — which is the thing
that already ran out of time. Whatever it left in the worktree is still
committed, under a message naming it as partial work rather than a finished
run.

`--no-commit` leaves the worktree dirty and `-n` caps the number of runs.
Ctrl-C stops the loop and leaves any running agent alone.

## Opinion

### Multi-pane windows

When a window have multiple panes, then I'm assuming user is more interested in
agent state, other than other panes. Thus "Window state" is collapsed to an
agent state it's running.

We can later revisit this decision if we want more support on long running jobs.

No special rules around "multiple agents in a single window" - I hope you're
okay with having a window per agent for monitoring. Don't want to add subtree
in order to show multiple agents under a window.

Also we can revisit if there's a strong case we need to run multiple agents in
a single window.
