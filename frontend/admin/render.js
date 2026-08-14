/**
 * Turning dashboard data into HTML.
 *
 * Kept as pure functions with no DOM access so they can be tested directly on
 * Node. Every one of them has the same job: show what the backend said, and
 * show "N/A" where it said nothing.
 *
 * Nothing here computes a cost, a duration for a finished call, or an
 * emissions figure. Those come from the backend or they are not shown.
 */

/**
 * How long a call has been running, worked out locally.
 *
 * The backend's `started_at` is canonical and the frontend does the counting,
 * so the dashboard can tick every second without a request every second.
 *
 * A finished call is frozen: once `ended_at` exists the number stops moving,
 * which is the difference between "this call is still going" and "this call
 * lasted three minutes". Preferring the backend's own `duration_seconds` for
 * finished calls also means a skewed operator clock cannot rewrite history.
 */
export function durationFor(row, nowMs = Date.now()) {
  if (!row) return null;

  if (row.ended_at) {
    if (row.duration_seconds !== null && row.duration_seconds !== undefined) {
      return row.duration_seconds;
    }
    const started = Date.parse(row.started_at);
    const ended = Date.parse(row.ended_at);
    if (Number.isNaN(started) || Number.isNaN(ended)) return null;
    return Math.max(0, Math.floor((ended - started) / 1000));
  }

  const started = Date.parse(row.started_at);
  if (Number.isNaN(started)) {
    return row.duration_seconds ?? null;
  }
  return Math.max(0, Math.floor((nowMs - started) / 1000));
}

/** Fields whose change is worth redrawing a row for. */
export const WATCHED_FIELDS = [
  "status",
  "auth_status",
  "client_id",
  "current_domain",
  "last_intent",
  "tool_calls",
  "total_tokens",
  "input_tokens",
  "output_tokens",
  "estimated_cost_usd",
  "estimated_carbon_grams",
  "ended_at",
  "disconnect_reason",
];

/**
 * What changed between two polls, keyed by agent session id.
 *
 * Used to highlight genuinely new rows and to decide whether the table needs
 * redrawing at all. Redrawing unconditionally every two seconds would fight
 * the operator for their text selection and scroll position.
 */
export function diffSessions(previous, next) {
  const before = new Map(
    (previous ?? []).map((row) => [row.agent_session_id, row])
  );
  const added = [];
  const changed = [];
  const closed = [];

  for (const row of next ?? []) {
    const old = before.get(row.agent_session_id);
    if (!old) {
      added.push(row.agent_session_id);
      continue;
    }
    if (!old.ended_at && row.ended_at) {
      closed.push(row.agent_session_id);
    }
    if (WATCHED_FIELDS.some((field) => old[field] !== row[field])) {
      changed.push(row.agent_session_id);
    }
  }

  return { added, changed, closed };
}

/** Whether anything on screen would actually look different. */
export function needsRedraw(previous, next) {
  if ((previous ?? []).length !== (next ?? []).length) return true;
  const order = (rows) => (rows ?? []).map((row) => row.agent_session_id).join("|");
  if (order(previous) !== order(next)) return true;
  const { added, changed, closed } = diffSessions(previous, next);
  return added.length > 0 || changed.length > 0 || closed.length > 0;
}

/** mm:ss, or hh:mm:ss once a call passes an hour. */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(rest)}`;
}

/**
 * Timestamps in Singapore time, which is where the demo is run.
 *
 * Stored in UTC, displayed here — the operator should never have to do the
 * arithmetic while a class is waiting.
 */
export function formatSgt(iso) {
  if (!iso) return "—";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "—";
  const text = when.toLocaleString("en-GB", {
    timeZone: "Asia/Singapore",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
  return `${text} SGT`;
}

/** HH:MM:SS in Singapore time, for the ticker and the refresh stamp. */
export function formatClock(when) {
  if (!when || Number.isNaN(when.getTime?.())) return "—";
  return when.toLocaleTimeString("en-GB", {
    timeZone: "Asia/Singapore",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
      character
    ]
  );
}

/** A value the backend did not have. Never a zero, never a guess. */
function orNa(value, render = (v) => String(v)) {
  if (value === null || value === undefined) {
    return '<span class="na">N/A</span>';
  }
  return escapeHtml(render(value));
}

function badge(value, fallback = "—") {
  if (!value) return escapeHtml(fallback);
  const kind = String(value).toLowerCase().replace(/[^a-z]/g, "");
  return `<span class="badge ${kind}">${escapeHtml(value)}</span>`;
}

export function money(value) {
  return `US$${Number(value).toFixed(4)}`;
}

export function grams(value) {
  return `${Number(value).toFixed(2)} gCO2e`;
}

/** One table row per agent session. */
export function renderRow(row, options = {}) {
  const live = row.active;
  const classes = [live ? "live" : "", options.isNew ? "just-added" : ""]
    .filter(Boolean)
    .join(" ");
  return `<tr class="${classes}">
    <td class="mono">${escapeHtml(row.agent_session_id)}</td>
    <td>${badge(row.status)}</td>
    <td>${row.client_id ? escapeHtml(row.client_id) : "—"}</td>
    <td>${badge(row.auth_status, "—")}</td>
    <td>${escapeHtml(formatSgt(row.started_at))}</td>
    <td>${row.ended_at ? escapeHtml(formatSgt(row.ended_at)) : "—"}</td>
    <td class="num" data-duration="${escapeHtml(row.agent_session_id)}">${escapeHtml(
      formatDuration(durationFor(row, options.now))
    )}</td>
    <td>${row.current_domain ? escapeHtml(row.current_domain) : "—"}</td>
    <td>${row.last_intent ? escapeHtml(row.last_intent) : "—"}</td>
    <td class="num">${row.tool_calls ?? 0}</td>
    <td class="num">${orNa(row.input_tokens)}</td>
    <td class="num">${orNa(row.output_tokens)}</td>
    <td class="num">${orNa(row.total_tokens)}</td>
    <td class="num">${orNa(row.estimated_cost_usd, money)}</td>
    <td class="num">${orNa(row.estimated_carbon_grams, grams)}</td>
    <td>${row.disconnect_reason ? escapeHtml(row.disconnect_reason) : "—"}</td>
    <td><button type="button" class="link-button" data-history="${escapeHtml(
      row.agent_session_id
    )}">View History</button></td>
  </tr>`;
}

export function renderRows(rows, options = {}) {
  if (!rows || rows.length === 0) {
    return '<tr><td colspan="17" class="notice">No agent sessions yet. Start a voice call to see one here.</td></tr>';
  }
  const fresh = new Set(options.newIds ?? []);
  return rows
    .map((row) =>
      renderRow(row, {
        isNew: fresh.has(row.agent_session_id),
        now: options.now,
      })
    )
    .join("");
}

/** The cards above the table. */
export function summaryCards(summary) {
  const cards = [
    ["Active Agents", summary.active_agents ?? 0, ""],
    ["Completed Calls", summary.completed_calls ?? 0, "last 24h"],
    ["Busy / Rejected", summary.rejected_calls ?? 0, "last 24h"],
    [
      "Average Call",
      summary.average_duration_seconds === null ||
      summary.average_duration_seconds === undefined
        ? null
        : formatDuration(summary.average_duration_seconds),
      "last 24h",
    ],
    ["Total Tokens", summary.total_tokens ?? null, "last 24h"],
    [
      "Estimated AI Cost",
      summary.estimated_cost_usd === null || summary.estimated_cost_usd === undefined
        ? null
        : money(summary.estimated_cost_usd),
      "estimated",
    ],
    [
      "Estimated Carbon",
      summary.estimated_carbon_grams === null ||
      summary.estimated_carbon_grams === undefined
        ? null
        : grams(summary.estimated_carbon_grams),
      "estimated",
    ],
    ["Errors", summary.errors ?? 0, "last 24h"],
  ];

  return cards
    .map(([label, value, note]) => {
      const shown =
        value === null || value === undefined
          ? '<div class="value na">N/A</div>'
          : `<div class="value">${escapeHtml(value)}</div>`;
      return `<div class="card"><div class="label">${escapeHtml(
        label
      )}</div>${shown}${note ? `<div class="note">${escapeHtml(note)}</div>` : ""}</div>`;
    })
    .join("");
}

/** One turn of a stored conversation. */
export function renderTurn(message) {
  const who =
    message.role === "CUSTOMER"
      ? "Customer"
      : message.role === "AGENT"
        ? "AI Agent"
        : "Event";
  const redacted = /^\[.*\]$/.test(message.content ?? "");
  return `<li class="turn ${String(message.role).toLowerCase()}">
    <div class="who">${escapeHtml(who)}</div>
    <p class="said ${redacted ? "redacted" : ""}">${escapeHtml(message.content)}</p>
  </li>`;
}

export function renderTurns(messages) {
  if (!messages || messages.length === 0) {
    return '<li class="notice">No conversation was recorded for this session.</li>';
  }
  return messages.map(renderTurn).join("");
}
