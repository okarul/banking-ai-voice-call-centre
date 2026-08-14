/**
 * The operations dashboard.
 *
 * Two clocks, deliberately separate. Operational data is polled from the
 * backend every two seconds, because the backend is the authority on what a
 * call is doing. The Active Time column is computed locally each second from
 * the backend's own `started_at`, because asking the server for a number that
 * only counts upwards would be a request per second per operator for no new
 * information.
 *
 * Three rules keep the polling from being worse than no polling at all:
 *
 * 1. **One request at a time.** A slow response must not stack another poll
 *    behind it. If a refresh is still running, the tick is skipped.
 * 2. **A failed refresh changes nothing.** The last good data stays on screen
 *    and a small status line says it is retrying. Blanking a table an operator
 *    is reading, because one request timed out, is the opposite of useful.
 * 3. **No redraw unless something differs.** Rewriting the table every two
 *    seconds would fight the operator for their scroll position and their text
 *    selection, so the rows are only re-rendered when they actually change.
 *
 * The page renders whatever the API gives it and nothing else: it does not
 * compute costs, and it shows "N/A" wherever the backend said it did not know.
 */

import {
  diffSessions,
  durationFor,
  formatClock,
  formatDuration,
  formatSgt,
  needsRedraw,
  renderRows,
  summaryCards,
} from "./render.js";

const API = "http://127.0.0.1:8001";
const REFRESH_MS = 2000;
/** Backgrounded tabs poll far less: nobody is reading them. */
const HIDDEN_REFRESH_MS = 15000;
const PAGE_SIZE = 25;
const HIGHLIGHT_MS = 4000;

const state = {
  page: 1,
  totalPages: 1,
  sortKey: null,
  sortAscending: true,
  rows: [],
  rendered: [],
  loadedOnce: false,
  inFlight: false,
  newIds: new Set(),
  // Difference between the server's clock and this browser's, so a skewed
  // operator machine does not invent three-hour calls.
  clockSkewMs: 0,
};

const el = (id) => document.getElementById(id);

async function getJson(path) {
  const response = await fetch(`${API}${path}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function query() {
  const parameters = new URLSearchParams({
    page: String(state.page),
    page_size: String(PAGE_SIZE),
    status: el("filter-status").value,
    domain: el("filter-domain").value,
    period: el("filter-period").value,
  });
  const search = el("search").value.trim();
  if (search) parameters.set("search", search);
  return parameters.toString();
}

/** Sort within the page, always keeping live calls above finished ones. */
function sorted(rows) {
  if (!state.sortKey) return rows;
  const key = state.sortKey;
  return [...rows].sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    const left = a[key] ?? "";
    const right = b[key] ?? "";
    if (left === right) return 0;
    const order = left > right ? 1 : -1;
    return state.sortAscending ? order : -order;
  });
}

function serverNow() {
  return Date.now() + state.clockSkewMs;
}

/**
 * One refresh. Never overlaps, and never destroys what is already on screen.
 *
 * The filter and search controls are read from the DOM on the way out and are
 * never written to, so a refresh cannot reset what the operator selected or
 * typed.
 */
async function refresh() {
  if (state.inFlight) return false;
  state.inFlight = true;

  try {
    const [summary, listing, events] = await Promise.all([
      getJson("/api/admin/dashboard/summary"),
      getJson(`/api/admin/agents?${query()}`),
      getJson("/api/admin/events?limit=12"),
    ]);

    if (listing.server_time) {
      const server = Date.parse(listing.server_time);
      if (!Number.isNaN(server)) state.clockSkewMs = server - Date.now();
    }

    const incoming = listing.sessions ?? [];
    const { added } = diffSessions(state.rows, incoming);
    if (state.loadedOnce && added.length) {
      for (const id of added) state.newIds.add(id);
      setTimeout(() => {
        for (const id of added) state.newIds.delete(id);
        draw(true);
      }, HIGHLIGHT_MS);
    }

    state.rows = incoming;
    state.totalPages = Math.max(1, Math.ceil(listing.total / listing.page_size));
    state.loadedOnce = true;

    drawSummary(summary);
    drawEvents(events.events);
    draw();

    el("page-info").textContent =
      `Page ${listing.page} of ${state.totalPages} — ${listing.total} sessions`;
    el("last-refreshed").textContent = formatClock(new Date());
    setStatus("");
    return true;
  } catch (error) {
    // The last good data stays exactly where it is. The operator is told the
    // dashboard cannot reach the backend and that it is still trying — no
    // status code, no URL, no provider text. This is a screen, not a debugger.
    setStatus("Unable to refresh dashboard. Retrying…");
    if (!state.loadedOnce) {
      el("agents-body").innerHTML =
        '<tr><td colspan="18" class="notice">Waiting for agent operations data…</td></tr>';
    }
    return false;
  } finally {
    state.inFlight = false;
  }
}

function setStatus(message) {
  const banner = el("refresh-status");
  if (!banner) return;
  banner.textContent = message;
  banner.hidden = !message;
}

function drawSummary(summary) {
  el("summary-cards").innerHTML = summaryCards(summary);

  const capacity = summary.capacity ?? {};
  el("cap-active").textContent = capacity.active ?? "—";
  el("cap-available").textContent =
    capacity.available === null || capacity.available === undefined
      ? "Unlimited"
      : capacity.available;
  el("cap-configured").textContent = capacity.configured ?? "Unlimited";

  const stateText = capacity.state ?? "NORMAL";
  const cell = el("cap-state");
  cell.textContent = stateText;
  cell.className =
    stateText === "FULL"
      ? "state-full"
      : stateText === "NEAR CAPACITY"
        ? "state-near"
        : "state-normal";

  const notes = [];
  if (!summary.estimates?.cost_configured) {
    notes.push("Cost estimation is not configured, so cost shows as N/A.");
  } else {
    notes.push(summary.estimates.cost_note);
  }
  if (!summary.estimates?.carbon_enabled) {
    notes.push("Carbon estimation is disabled, so emissions show as N/A.");
  } else {
    notes.push(summary.estimates.carbon_note);
  }
  el("estimate-note").textContent = notes.join(" ");
}

function drawEvents(events) {
  if (!events || events.length === 0) {
    el("events").innerHTML = "<li>No events yet.</li>";
    return;
  }
  el("events").innerHTML = events
    .map(
      (event) =>
        `<li><time>${formatClock(new Date(event.at))}</time>${escapeHtml(event.text)}</li>`
    )
    .join("");
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
      character
    ]
  );
}

/** Redraw the table, but only when it would look different. */
function draw(force = false) {
  const rows = sorted(state.rows);
  if (!force && state.loadedOnce && !needsRedraw(state.rendered, rows)) {
    return;
  }
  el("agents-body").innerHTML = renderRows(rows, {
    newIds: [...state.newIds],
    now: serverNow(),
  });
  state.rendered = rows.map((row) => ({ ...row }));
}

/**
 * Tick the live durations without going back to the server.
 *
 * Only rows that are still running move. A finished call's duration comes from
 * the backend and is left alone, which is what stops a completed call counting
 * upwards for the rest of the afternoon.
 */
function tick() {
  const now = serverNow();
  for (const row of state.rows) {
    if (row.ended_at) continue;
    const cell = document.querySelector(
      `[data-duration="${row.agent_session_id}"]`
    );
    if (cell) cell.textContent = formatDuration(durationFor(row, now));
  }
}

function wire() {
  el("refresh").addEventListener("click", () => refresh());
  for (const id of ["filter-status", "filter-domain", "filter-period"]) {
    el(id).addEventListener("change", () => {
      state.page = 1;
      state.rendered = [];
      refresh();
    });
  }

  let typingTimer = null;
  el("search").addEventListener("input", () => {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      state.page = 1;
      state.rendered = [];
      refresh();
    }, 300);
  });

  el("prev-page").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      state.rendered = [];
      refresh();
    }
  });
  el("next-page").addEventListener("click", () => {
    if (state.page < state.totalPages) {
      state.page += 1;
      state.rendered = [];
      refresh();
    }
  });

  for (const header of document.querySelectorAll("th[data-sort]")) {
    header.addEventListener("click", () => {
      const key = header.dataset.sort;
      state.sortAscending = state.sortKey === key ? !state.sortAscending : true;
      state.sortKey = key;
      draw(true);
    });
  }

  el("agents-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-history]");
    if (button) {
      // A new tab, so the dashboard behind it keeps polling and the operator
      // does not lose their place.
      window.open(
        `chat-history.html?session=${encodeURIComponent(button.dataset.history)}`,
        "_blank",
        "noopener"
      );
    }
  });

  // A hidden tab is not being read, so it polls slowly; coming back to it
  // refreshes at once rather than making the operator wait for the next tick.
  document.addEventListener("visibilitychange", () => {
    schedule();
    if (!document.hidden) refresh();
  });
}

let pollTimer = null;

function schedule() {
  if (pollTimer !== null) clearInterval(pollTimer);
  const interval = document.hidden ? HIDDEN_REFRESH_MS : REFRESH_MS;
  pollTimer = setInterval(refresh, interval);
}

wire();
refresh();
schedule();
setInterval(tick, 1000);

export { formatDuration, formatSgt };
