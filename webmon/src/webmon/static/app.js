"use strict";

const state = {
  ws: null,
  model: { sessions: [], panes: {}, tier: "unknown", socket: "", error: "" },
  selected: null,
  term: null,
  backoff: 250,
  sizingColumns: 100,
  lastSize: "",
};

const el = {
  tree: document.getElementById("tree"),
  tier: document.getElementById("tier"),
  status: document.getElementById("status"),
  socket: document.getElementById("socket"),
  selected: document.getElementById("selected"),
  screenState: document.getElementById("screen-state"),
  screen: document.getElementById("screen"),
  wrap: document.getElementById("screen-wrap"),
};

function makeTerminal() {
  const term = new Terminal({
    cols: 80,
    rows: 24,
    convertEol: false,
    disableStdin: true,
    cursorBlink: false,
    scrollback: 0,
    fontSize: 13,
    fontFamily: 'ui-monospace, "SF Mono", "DejaVu Sans Mono", Menlo, monospace',
    theme: { background: "#0e1015", foreground: "#d7dce5" },
  });
  term.open(el.screen);
  return term;
}

function updateViewport() {
  const inner = el.screen.querySelector(".xterm-screen");
  if (!inner) return;
  if (state.term && state.selected && state.sizingColumns && state.ws?.readyState === WebSocket.OPEN) {
    const cellHeight = inner.clientHeight / state.term.rows;
    if (cellHeight > 0 && el.wrap.clientHeight > 20) {
      const size = { t: "resize", pane: state.selected,
        cols: Math.min(1000, state.sizingColumns),
        rows: Math.max(1, Math.min(1000, Math.floor((el.wrap.clientHeight - 20) / cellHeight))) };
      const encoded = JSON.stringify(size);
      if (encoded !== state.lastSize) { send(size); state.lastSize = encoded; }
    }
  }
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  state.ws = ws;

  ws.onopen = () => {
    state.backoff = 250;
    state.lastSize = "";
    setStatus("");
    if (state.selected) send({ t: "view", pane: state.selected });
  };
  ws.onclose = () => {
    setStatus("disconnected from webmon — retrying");
    setTimeout(connect, state.backoff);
    state.backoff = Math.min(5000, state.backoff * 2);
  };
  ws.onmessage = (event) => {
    if (typeof event.data === "string") onJson(JSON.parse(event.data));
    else onBinary(new Uint8Array(event.data));
  };
}

function send(message) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(message));
  }
}

function onJson(message) {
  switch (message.t) {
    case "model":
      state.model = message;
      renderAll();
      break;
    case "panes":
      for (const [id, fields] of Object.entries(message.panes || {})) {
        Object.assign((state.model.panes[id] = state.model.panes[id] || {}), fields);
      }
      for (const id of message.removed || []) delete state.model.panes[id];
      renderTree();
      break;
    case "screen":
      applyScreen(message);
      break;
    case "screen-lagged":
      el.screenState.textContent = "catching up…";
      break;
    case "screen-gone":
      el.screenState.textContent = "pane is gone";
      break;
    default:
      break;
  }
}

function onBinary(bytes) {
  if (bytes.length < 4 || !state.term) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset, 4);
  const pane = `%${view.getUint32(0)}`;
  if (pane !== state.selected) return;
  state.term.write(bytes.subarray(4));
}

function applyScreen(message) {
  if (message.pane !== state.selected) return;
  if (!state.term) state.term = makeTerminal();
  const cols = message.cols || 80;
  const rows = message.rows || 24;
  if (Number.isInteger(message.sizing_columns)) state.sizingColumns = message.sizing_columns;
  if (state.term.cols !== cols || state.term.rows !== rows) {
    state.term.resize(cols, rows);
  }
  const data = message.data || "";
  const bytes = new Uint8Array(data.length);
  for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
  state.term.write(bytes);
  el.screenState.textContent = "";
  requestAnimationFrame(updateViewport);
}

function select(pane) {
  if (state.selected === pane) return;
  if (state.selected) send({ t: "unview", pane: state.selected });
  state.selected = pane;
  state.lastSize = "";
  if (!state.term) state.term = makeTerminal();
  state.term.reset();
  el.screenState.textContent = "loading…";
  renderTree();
  renderSelected();
  send({ t: "view", pane });
}

function setStatus(text) {
  el.status.textContent = text;
  el.status.classList.toggle("show", Boolean(text));
}

function renderSelected() {
  const pane = state.model.panes[state.selected];
  if (!pane) {
    el.selected.textContent = state.selected || "no pane selected";
    return;
  }
  const size = `${pane.pane_width}×${pane.pane_height}`;
  const where = `${pane.session_name}:${pane.window_index}.${pane.pane_index}`;
  el.selected.textContent = `${state.selected}  ${where}  ${pane.window_name}  ${size}`;
}

function paneRow(id) {
  const pane = state.model.panes[id];
  const row = document.createElement("div");
  row.className = "pane" + (id === state.selected ? " selected" : "") + (pane.pane_active ? " active" : "");
  row.onclick = () => select(id);

  const emoji = document.createElement("span");
  emoji.className = "emoji";
  emoji.textContent = pane.pane_state_emoji || "";

  const title = document.createElement("span");
  title.className = "title";
  title.textContent = pane.pane_current_command || pane.pane_title || id;

  const meta = document.createElement("span");
  meta.className = "meta";
  if (pane.pane_agent) {
    meta.textContent = pane.pane_agent_model
      ? `${pane.pane_agent} ${pane.pane_agent_state} · ${pane.pane_agent_model}`
      : `${pane.pane_agent} ${pane.pane_agent_state}`;
  } else {
    meta.textContent = id;
  }

  const path = document.createElement("span");
  path.className = "path";
  path.textContent = pane.pane_current_path || "";

  row.append(emoji, title, meta, path);
  return row;
}

function renderTree() {
  const tree = document.createDocumentFragment();
  for (const session of state.model.sessions || []) {
    const group = document.createElement("section");
    group.className = "session-group";
    const head = document.createElement("div");
    head.className = "session";
    head.textContent = `${session.name}`;
    group.append(head);
    const windows = document.createElement("div");
    windows.className = "windows";
    group.append(windows);
    tree.append(group);
    for (const window of session.windows || []) {
      const windowGroup = document.createElement("div");
      windowGroup.className = "window-group";
      windows.append(windowGroup);
      const line = document.createElement("div");
      line.className = "window";
      const index = document.createElement("span");
      index.textContent = String(window.index);
      const name = document.createElement("span");
      name.className = "wname";
      name.textContent = window.name;
      line.append(index, name);
      windowGroup.append(line);
      for (const id of window.panes || []) {
        if (state.model.panes[id]) windowGroup.append(paneRow(id));
      }
    }
  }
  el.tree.replaceChildren(tree);
  renderSelected();
}

function renderAll() {
  const tier = state.model.tier || "unknown";
  el.tier.textContent = tier === "hmux" ? "agent status" : tier;
  el.tier.className = `badge ${tier}`;
  el.socket.textContent = state.model.socket || "";
  setStatus(state.model.error ? `server: ${state.model.error}` : "");
  if (state.selected && !state.model.panes[state.selected]) {
    el.screenState.textContent = "pane is gone";
  }
  renderTree();
}

new ResizeObserver(() => requestAnimationFrame(updateViewport)).observe(el.wrap);
connect();
