# webmon

In provider mode, attached terminal viewers receive quota updates from Agentmon's
`QuotaService`. The pane's agent maps `codex` to Codex, `claude` to Claude, and
`agy` to Antigravity, using the provider user's configured Agentmon credentials.
The service quota is account-wide; it is not a per-run token budget. Alternate
accounts inside individual panes are not automatically detected.

Only services with attached terminal viewers are polled, with a five-minute
cache and a shared feed for viewers of the same service. Listing and metadata
subscriptions do not fetch quota. Switching agents updates the mapping, and
detaching the last viewer stops polling. Quota travels as a `quota` message in
the existing `terminal-message` envelope; no GraphQL schema change is required.
Fetch failures preserve the current view's last successful quota as stale.
Each quota window includes `remaining_percent`, `resets_at` (Unix seconds), and
`window_seconds` (null when unknown). The viewer uses the duration and reset
time to update pacing as time passes: used percentage minus elapsed percentage,
matching Agentmon. Positive pacing means usage is ahead of an even burn.
The compact indicator shows the highest quota usage and largest pacing delta
across known windows, with each window's details in its tooltip. Missing timing
information leaves pacing unknown, including with older Webmon providers.

Install with `uv sync --group dev` from this directory to include the sibling
Agentmon package, or use the Webmon Nix package, which includes that dependency.

A read-only browser view of an hmux/tmux server: every pane's state as the
glyphs the server already computes, and the live screen of the pane you click.

webmon is a *viewer*, not part of any server. It attaches to a socket as an
ordinary control-mode client and serves HTTP and WebSocket to browsers, so it
works against hmux, and against stock tmux with the agent columns empty.

```sh
webmon                                   # the default tmux socket
webmon --socket /path/to/socket          # a specific server
webmon --provider https://gql2.blmarket.net/gql
```

With no `--socket`, webmon watches the socket a plain `tmux` would use —
`$TMUX_TMPDIR/tmux-<uid>/default`, or `/tmp/tmux-<uid>/default`. If nothing is
answering there it falls back to `HMUX_SOCKET`, then the `TMUX` of the pane it
was started from, then any other socket in that directory.

It needs a `tmux` client binary on `PATH` (or `--tmux`/`WEBMON_TMUX`). The local
HTTP server, WebSocket endpoint, and control-mode client use stdlib asyncio;
outbound provider mode uses `websockets` 15.x/16.x, `cryptography` for JWK signing,
and OpenSSH's `ssh-keygen` for SSH signing.
xterm.js is vendored under `src/webmon/static/vendor` at a pinned version.

Each actively sized browser view uses a dedicated tmux session containing a
link to its watched window. The existing panes and processes are shared; no
terminal content is copied into a new pane. The browser advertises its measured
rows and capped columns as its control client's general size.

Webmon sets `window-size smallest` and `aggressive-resize on` on the watched
window. These window settings remain after the view closes. Clients affect
a window's size only when their session selects it, so browser and desktop
selections are independent:

| Viewers displaying the window | Grid (100×69 browser, 122×33 desktop) |
|---|---|
| Browser only | 100×69 |
| Desktop only | 122×33 |
| Browser and desktop | 100×33 |

There is no manual sizing controller, size polling, per-window size report,
or 1000×1000 fallback. Other windows keep their existing settings. The default
browser column cap is 100; `--stream-columns 0` disables sizing sessions.

Browser sessions are named `__webmon_size_<32 hexadecimal digits>`. They are
visible and attachable in tmux. Webmon reserves that name pattern and excludes
those sessions from its own discovery, including sessions from other webmon
processes, to avoid showing the same pane under multiple sessions.

Closing or switching the view destroys its session. `destroy-unattached` and
a session-local `client-detached` hook remove it if its browser sizing client
or webmon exits. The hook also removes the session if another desktop has
attached to it, detaching that desktop. The original session and its shared
windows remain. An inactive window may keep its last dimensions until selected
again, following tmux's normal deferred resizing.

Restart webmon to try the changed code:

```sh
nix develop -c webmon --provider https://gql2.blmarket.net/gql
```

Simulate the same session arrangement:

```sh
./sim.sh --separate-browser-session --window-size smallest --aggressive-resize on
```
