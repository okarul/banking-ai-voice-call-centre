/**
 * Wiring: buttons and lights on one side, the call on the other.
 *
 * Everything that decides anything lives in call.js. This file only reflects
 * what the call is doing onto the page, which is why it is almost all
 * assignments — and why the call logic can be tested without a browser.
 */

import { BankingCall, State, formatDuration } from "./call.js";

// The backend is on its own port, so the page names it explicitly.
const API_BASE = "http://127.0.0.1:8001";

const el = (id) => document.getElementById(id);

const startButton = el("start-call");
const muteButton = el("mute");
const endButton = el("end-call");
const statusText = el("status-text");
const statusDot = el("status-dot");
const timerText = el("timer");
const message = el("message");
const callerLight = el("caller-indicator");
const assistantLight = el("assistant-indicator");
const debugPanel = el("debug");
const debugToggle = el("debug-toggle");

let ticker = null;
const toolsSeen = [];

const call = new BankingCall({
  apiBase: API_BASE,
  deps: {
    onState: render,
    onError: (text) => {
      message.textContent = text;
    },
    onDebug: note,
    onSpeaking: (who, speaking) => {
      const light = who === "caller" ? callerLight : assistantLight;
      light.classList.toggle("active", speaking);
    },
  },
});

function render(state) {
  statusText.textContent = state;
  statusDot.dataset.state = state;

  const connecting = state === State.CONNECTING || state === State.ENDING;
  const live = state === State.CONNECTED || state === State.MUTED;

  // One call per page: Start is unavailable from the moment it is pressed
  // until the line is genuinely down again.
  startButton.disabled = connecting || live;
  muteButton.disabled = !live;
  endButton.disabled = !live && !connecting;

  muteButton.textContent = state === State.MUTED ? "Unmute" : "Mute";
  muteButton.classList.toggle("on", state === State.MUTED);

  if (state === State.CONNECTED || state === State.MUTED) {
    message.textContent = "";
    startTicking();
  }
  if (state === State.DISCONNECTED || state === State.ERROR) {
    stopTicking();
    callerLight.classList.remove("active");
    assistantLight.classList.remove("active");
  }

  el("debug-session").textContent = call.sessionId ?? "—";
  el("debug-realtime").textContent = call.realtimeSessionId ?? "—";
  el("debug-connection").textContent = state;
}

function startTicking() {
  if (ticker !== null) return;
  ticker = setInterval(() => {
    timerText.textContent = formatDuration(call.durationSeconds());
  }, 250);
}

function stopTicking() {
  if (ticker !== null) clearInterval(ticker);
  ticker = null;
  timerText.textContent = "00:00";
}

/** Developer-view reporting. Never the caller's words, never a PIN. */
function note(kind, detail) {
  if (kind === "said" && detail) {
    const line = document.createElement("li");
    line.textContent = detail;
    el("debug-transcript").append(line);
    return;
  }
  if (kind === "tool" && detail) {
    toolsSeen.push(detail);
    el("debug-tools").textContent = toolsSeen.join(", ");
    refreshSessionState();
    return;
  }
  el("debug-event").textContent = kind;
}

/** Pull the authoritative session state for the developer view. */
async function refreshSessionState() {
  if (!call.sessionId) return;
  try {
    const response = await fetch(`${API_BASE}/api/call/state/${call.sessionId}`);
    if (!response.ok) return;
    const state = await response.json();
    el("debug-authenticated").textContent = state.authenticated ? "yes" : "no";
    el("debug-customer").textContent = state.customer_id ?? "—";
    el("debug-domain").textContent = state.current_domain ?? "—";
  } catch (error) {
    /* the developer view is never load-bearing */
  }
}

startButton.addEventListener("click", async () => {
  message.textContent = "";
  el("debug-transcript").replaceChildren();
  toolsSeen.length = 0;
  el("debug-tools").textContent = "—";
  await call.start();
});

muteButton.addEventListener("click", () => call.toggleMute());

endButton.addEventListener("click", () => call.end());

debugToggle.addEventListener("click", () => {
  debugPanel.hidden = !debugPanel.hidden;
  debugToggle.textContent = debugPanel.hidden
    ? "Developer view"
    : "Hide developer view";
});

// A closing tab cannot be relied on to finish a fetch, so the hang-up goes out
// as a beacon. The server sweeps abandoned calls regardless.
window.addEventListener("pagehide", () => {
  call.hangUpBeacon(navigator.sendBeacon?.bind(navigator));
});

render(State.DISCONNECTED);
