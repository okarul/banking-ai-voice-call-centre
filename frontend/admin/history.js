/**
 * One agent session's conversation.
 *
 * The session id in the query string is the *only* thing that selects what is
 * shown, and it is an opaque operational handle (AGT-000001) rather than
 * anything about the customer. Two calls by the same synthetic customer are two
 * separate conversations, and this page will only ever load one of them.
 */

import { escapeHtml, formatDuration, formatSgt, renderTurns } from "./render.js";

const API = "http://127.0.0.1:8001";

const el = (id) => document.getElementById(id);

function sessionParameter() {
  const asked = new URLSearchParams(window.location.search).get("session");
  // A conservative shape check before it is put in a URL: the handle is
  // machine-generated and looks like AGT-000001.
  return asked && /^[A-Z0-9-]{3,32}$/i.test(asked) ? asked : null;
}

function summary(session) {
  const rows = [
    ["Agent Session ID", session.agent_session_id],
    ["Client ID", session.client_id ?? "—"],
    ["Authentication", session.auth_status ?? "—"],
    ["Start Time", formatSgt(session.started_at)],
    ["End Time", session.ended_at ? formatSgt(session.ended_at) : "—"],
    ["Duration", formatDuration(session.duration_seconds)],
    ["Final Status", session.status],
    ["Disconnect Reason", session.disconnect_reason ?? "—"],
  ];
  return rows
    .map(
      ([label, value]) =>
        `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`
    )
    .join("");
}

/** Slower than the dashboard: a transcript grows a line at a time. */
const LIVE_REFRESH_MS = 5000;

let loadedOnce = false;
let inFlight = false;
let pollTimer = null;

async function load() {
  const id = sessionParameter();
  if (!id) {
    el("turns").innerHTML =
      '<li class="notice error">No agent session was selected.</li>';
    return;
  }
  if (inFlight) return;
  inFlight = true;

  try {
    const response = await fetch(
      `${API}/api/admin/agents/${encodeURIComponent(id)}/history`
    );
    if (response.status === 404) {
      el("turns").innerHTML =
        '<li class="notice error">That agent session was not found.</li>';
      return;
    }
    if (!response.ok) throw new Error("unavailable");

    const data = await response.json();
    el("session-summary").innerHTML = summary(data.session);
    el("turns").innerHTML = renderTurns(data.messages);
    document.title = `${data.session.agent_session_id} — Conversation History`;
    loadedOnce = true;

    // A call that has finished cannot gain new lines, so stop asking.
    if (data.session.ended_at && pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  } catch (error) {
    // A failed poll leaves whatever was already on screen alone. Only a page
    // that has never loaded shows the failure in place of the conversation.
    if (!loadedOnce) {
      el("turns").innerHTML =
        '<li class="notice error">Unable to load this conversation. ' +
        "The banking service may be unavailable.</li>";
    }
  } finally {
    inFlight = false;
  }
}

load();
// New safe lines appear while the call is still running. The session id in the
// URL is the only thing that selects them, so this can never widen into
// another call's conversation.
pollTimer = setInterval(load, LIVE_REFRESH_MS);
