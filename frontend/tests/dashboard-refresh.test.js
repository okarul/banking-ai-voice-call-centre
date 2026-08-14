/**
 * The dashboard's live refresh: what it notices, and what it refuses to do.
 *
 * The interesting behaviour here is negative. Polling every two seconds is
 * easy; polling without stacking requests, without blanking the table when one
 * fails, without resetting the operator's filters, and without a finished
 * call's duration creeping upwards is the part that needs defending.
 *
 * The diff, duration and redraw decisions are pure functions, so they are
 * tested directly. The wiring around them is checked by reading the module.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import {
  diffSessions,
  durationFor,
  formatDuration,
  needsRedraw,
  renderRow,
  renderRows,
} from "../admin/render.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const admin = (name) => readFileSync(join(HERE, "..", "admin", name), "utf8");

const DASHBOARD_JS = admin("dashboard.js");
const HISTORY_JS = admin("history.js");
const AGENTS_HTML = admin("agents.html");
const DASHBOARD_CSS = admin("dashboard.css");

const START = "2026-08-13T14:00:00+00:00";
const startMs = Date.parse(START);

const live = {
  agent_session_id: "AGT-000001",
  status: "ACTIVE",
  client_id: null,
  auth_status: "PENDING",
  started_at: START,
  ended_at: null,
  duration_seconds: 5,
  active: true,
  current_domain: null,
  last_intent: null,
  tool_calls: 0,
  input_tokens: null,
  output_tokens: null,
  total_tokens: null,
  estimated_cost_usd: null,
  estimated_carbon_grams: null,
  disconnect_reason: null,
};

// --- polling shape ----------------------------------------------------------

test("the dashboard polls every two seconds", () => {
  assert.match(DASHBOARD_JS, /REFRESH_MS\s*=\s*2000/);
});

test("normal updates never reload the page", () => {
  assert.doesNotMatch(DASHBOARD_JS, /location\.reload/);
  assert.doesNotMatch(DASHBOARD_JS, /location\.href\s*=/);
});

test("only the two dashboard endpoints and the event ticker are polled", () => {
  const calls = DASHBOARD_JS.match(/getJson\(\s*[`"']([^`"']+)/g) ?? [];
  assert.equal(calls.length, 3);
  assert.match(DASHBOARD_JS, /\/api\/admin\/dashboard\/summary/);
  assert.match(DASHBOARD_JS, /\/api\/admin\/agents\?/);
  assert.match(DASHBOARD_JS, /\/api\/admin\/events\?limit=/);
});

test("a refresh cannot overlap the previous one", () => {
  assert.match(DASHBOARD_JS, /if \(state\.inFlight\) return false;/);
  assert.match(DASHBOARD_JS, /state\.inFlight = true;/);
  assert.match(DASHBOARD_JS, /finally \{\s*state\.inFlight = false;/);
});

test("a hidden tab polls slowly and refreshes on return", () => {
  assert.match(DASHBOARD_JS, /HIDDEN_REFRESH_MS/);
  assert.match(DASHBOARD_JS, /visibilitychange/);
  assert.match(DASHBOARD_JS, /if \(!document\.hidden\) refresh\(\);/);
});

test("manual Refresh calls the same function as the timer", () => {
  assert.match(DASHBOARD_JS, /el\("refresh"\)\.addEventListener\("click", \(\) => refresh\(\)\)/);
  assert.match(DASHBOARD_JS, /setInterval\(refresh,/);
});

// --- detecting change -------------------------------------------------------

test("a brand new session is detected", () => {
  const { added, changed, closed } = diffSessions([live], [
    live,
    { ...live, agent_session_id: "AGT-000002" },
  ]);

  assert.deepEqual(added, ["AGT-000002"]);
  assert.deepEqual(changed, []);
  assert.deepEqual(closed, []);
});

test("an authentication change is detected", () => {
  const after = { ...live, client_id: "DEMO001", auth_status: "VERIFIED" };
  const { changed } = diffSessions([live], [after]);

  assert.deepEqual(changed, ["AGT-000001"]);
});

test("a domain and intent change is detected", () => {
  const after = {
    ...live,
    current_domain: "ACCOUNT",
    last_intent: "ACCOUNT_BALANCE",
  };

  assert.deepEqual(diffSessions([live], [after]).changed, ["AGT-000001"]);
});

test("a token change is detected", () => {
  const after = { ...live, total_tokens: 1540, input_tokens: 1200 };

  assert.deepEqual(diffSessions([live], [after]).changed, ["AGT-000001"]);
});

test("a closed session is detected as closed", () => {
  const after = {
    ...live,
    status: "COMPLETED",
    ended_at: "2026-08-13T14:05:00+00:00",
    duration_seconds: 300,
    active: false,
    disconnect_reason: "VOICE_END_CALL",
  };

  const { closed, changed } = diffSessions([live], [after]);
  assert.deepEqual(closed, ["AGT-000001"]);
  assert.deepEqual(changed, ["AGT-000001"]);
});

test("an unchanged poll reports nothing at all", () => {
  const { added, changed, closed } = diffSessions([live], [{ ...live }]);

  assert.deepEqual([added, changed, closed], [[], [], []]);
});

// --- redrawing --------------------------------------------------------------

test("an identical poll does not redraw the table", () => {
  assert.equal(needsRedraw([live], [{ ...live }]), false);
});

test("a changed row redraws", () => {
  assert.equal(needsRedraw([live], [{ ...live, status: "COMPLETED" }]), true);
});

test("a new row redraws", () => {
  assert.equal(
    needsRedraw([live], [live, { ...live, agent_session_id: "AGT-000002" }]),
    true
  );
});

test("a reordered table redraws", () => {
  const second = { ...live, agent_session_id: "AGT-000002" };
  assert.equal(needsRedraw([live, second], [second, live]), true);
});

test("the ticking duration alone does not force a redraw", () => {
  // duration_seconds is not a watched field: it changes every second and is
  // updated in place, so it must not trigger a whole-table rewrite.
  assert.equal(
    needsRedraw([live], [{ ...live, duration_seconds: live.duration_seconds + 1 }]),
    false
  );
});

// --- durations --------------------------------------------------------------

test("an active call's duration is computed from the backend start time", () => {
  assert.equal(durationFor(live, startMs + 207_000), 207);
  assert.equal(formatDuration(durationFor(live, startMs + 207_000)), "00:03:27");
});

test("an active duration advances with the clock", () => {
  const first = durationFor(live, startMs + 10_000);
  const later = durationFor(live, startMs + 11_000);

  assert.equal(later - first, 1);
});

test("a completed call's duration is frozen", () => {
  const done = {
    ...live,
    ended_at: "2026-08-13T14:05:00+00:00",
    duration_seconds: 300,
  };

  assert.equal(durationFor(done, startMs + 60 * 60_000), 300);
  assert.equal(durationFor(done, startMs + 99 * 60_000), 300);
});

test("a completed call without a stored duration is measured end to start", () => {
  const done = {
    ...live,
    ended_at: "2026-08-13T14:05:00+00:00",
    duration_seconds: null,
  };

  assert.equal(durationFor(done, Date.now()), 300);
});

test("a missing start time does not produce a nonsense duration", () => {
  assert.equal(durationFor({ ...live, started_at: null, duration_seconds: null }), null);
});

test("the local tick only touches rows that are still running", () => {
  assert.match(DASHBOARD_JS, /if \(row\.ended_at\) continue;/);
  assert.match(DASHBOARD_JS, /setInterval\(tick, 1000\)/);
});

test("duration ticking does not call the backend", () => {
  const tick = DASHBOARD_JS.slice(DASHBOARD_JS.indexOf("function tick()"));
  const body = tick.slice(0, tick.indexOf("\n}"));

  assert.doesNotMatch(body, /fetch|getJson/);
});

// --- failure ----------------------------------------------------------------

test("a failed refresh keeps the last good data", () => {
  const failure = DASHBOARD_JS.slice(DASHBOARD_JS.indexOf("} catch (error) {"));
  const body = failure.slice(0, failure.indexOf("} finally"));

  // The only write to the table body on the failure path is guarded by
  // "never loaded anything yet".
  assert.match(body, /if \(!state\.loadedOnce\)/);
  assert.match(body, /Unable to refresh dashboard\. Retrying/);
});

test("the retry message carries no diagnostic detail", () => {
  assert.doesNotMatch(DASHBOARD_JS, /error\.message/);
  assert.doesNotMatch(DASHBOARD_JS, /error\.stack/);
  assert.doesNotMatch(DASHBOARD_JS, /console\.(error|log)/);
  // The operator sees one sentence, and it names nothing.
  assert.match(DASHBOARD_JS, /"Unable to refresh dashboard\. Retrying…"/);
});

test("the page has somewhere to show the retry notice", () => {
  assert.match(AGENTS_HTML, /id="refresh-status"/);
  assert.match(AGENTS_HTML, /role="status"/);
});

test("retrying continues on a timer rather than stopping at the first failure", () => {
  // The poll is an interval, so a thrown refresh does not end the schedule.
  assert.match(DASHBOARD_JS, /pollTimer = setInterval\(refresh, interval\)/);
});

// --- filters and operator state --------------------------------------------

test("a refresh reads the filters and never writes to them", () => {
  // Assignment to a control's value would be what resets an operator's choice.
  assert.doesNotMatch(DASHBOARD_JS, /el\("filter-status"\)\.value\s*=/);
  assert.doesNotMatch(DASHBOARD_JS, /el\("filter-domain"\)\.value\s*=/);
  assert.doesNotMatch(DASHBOARD_JS, /el\("filter-period"\)\.value\s*=/);
  assert.doesNotMatch(DASHBOARD_JS, /el\("search"\)\.value\s*=/);
  assert.match(DASHBOARD_JS, /status: el\("filter-status"\)\.value/);
  assert.match(DASHBOARD_JS, /search/);
});

test("the search box is sent with every poll so results stay filtered", () => {
  assert.match(DASHBOARD_JS, /parameters\.set\("search", search\)/);
});

test("View History opens in a new tab so the dashboard keeps polling", () => {
  assert.match(DASHBOARD_JS, /window\.open\([\s\S]{0,120}"_blank"/);
});

// --- new session highlight --------------------------------------------------

test("a newly seen session is marked, and the mark is temporary", () => {
  const html = renderRows([live], { newIds: ["AGT-000001"] });

  assert.match(html, /just-added/);
  assert.match(DASHBOARD_JS, /HIGHLIGHT_MS/);
  assert.match(DASHBOARD_JS, /state\.newIds\.delete\(id\)/);
});

test("an ordinary row is not marked as new", () => {
  assert.doesNotMatch(renderRow(live), /just-added/);
});

test("the highlight fades and respects reduced motion", () => {
  assert.match(DASHBOARD_CSS, /tbody tr\.just-added/);
  assert.match(DASHBOARD_CSS, /prefers-reduced-motion/);
});

// --- conversation history ---------------------------------------------------

test("history is never opened automatically", () => {
  assert.doesNotMatch(HISTORY_JS, /window\.open/);
  assert.match(DASHBOARD_JS, /data-history/);
});

test("an open history refreshes while its call is live and stops when it ends", () => {
  assert.match(HISTORY_JS, /LIVE_REFRESH_MS/);
  assert.match(HISTORY_JS, /if \(data\.session\.ended_at && pollTimer !== null\)/);
  assert.match(HISTORY_JS, /clearInterval\(pollTimer\)/);
});

test("history polling stays bound to one session id", () => {
  assert.match(HISTORY_JS, /\/api\/admin\/agents\/\$\{encodeURIComponent\(id\)\}\/history/);
  assert.doesNotMatch(HISTORY_JS, /customer_id/);
});

test("a failed history poll does not wipe the conversation", () => {
  assert.match(HISTORY_JS, /if \(!loadedOnce\)/);
});

test("history polls do not overlap either", () => {
  assert.match(HISTORY_JS, /if \(inFlight\) return;/);
});
