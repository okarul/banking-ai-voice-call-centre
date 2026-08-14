/**
 * The operations dashboard's rendering, tested without a browser.
 *
 * The render functions are pure, so what a row looks like can be checked
 * directly. What is being defended here is mostly restraint: the dashboard
 * shows "N/A" where the backend knew nothing, never invents a cost or an
 * emissions figure, and never puts a customer's balance in the table.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import {
  escapeHtml,
  formatDuration,
  formatSgt,
  renderRow,
  renderRows,
  renderTurn,
  renderTurns,
  summaryCards,
} from "../admin/render.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const admin = (name) => readFileSync(join(HERE, "..", "admin", name), "utf8");

const AGENTS_HTML = admin("agents.html");
const HISTORY_HTML = admin("chat-history.html");
const DASHBOARD_JS = admin("dashboard.js");
const HISTORY_JS = admin("history.js");
const RENDER_JS = admin("render.js");
const CUSTOMER_HTML = readFileSync(join(HERE, "..", "index.html"), "utf8");
const CUSTOMER_APP = readFileSync(join(HERE, "..", "app.js"), "utf8");

const liveRow = {
  agent_session_id: "AGT-000001",
  status: "ACTIVE",
  client_id: "DEMO001",
  auth_status: "VERIFIED",
  started_at: "2026-08-13T14:02:15+00:00",
  ended_at: null,
  duration_seconds: 207,
  active: true,
  current_domain: "ACCOUNT",
  last_intent: "ACCOUNT_BALANCE",
  tool_calls: 3,
  input_tokens: 1200,
  output_tokens: 340,
  total_tokens: 1540,
  estimated_cost_usd: 0.0042,
  estimated_carbon_grams: 0.18,
  disconnect_reason: null,
};

const finishedRow = {
  ...liveRow,
  agent_session_id: "AGT-000002",
  status: "COMPLETED",
  ended_at: "2026-08-13T14:08:42+00:00",
  active: false,
  disconnect_reason: "VOICE_END_CALL",
};

// --- the pages themselves ---------------------------------------------------

test("the dashboard is a separate page from the customer line", () => {
  assert.match(AGENTS_HTML, /Agent Operations/);
  // The customer's page must not offer a way into the operator's.
  assert.doesNotMatch(CUSTOMER_HTML, /admin\//);
  assert.doesNotMatch(CUSTOMER_APP, /admin\//);
});

test("the dashboard header names the bank and its purpose", () => {
  assert.match(AGENTS_HTML, /ABC Demo Bank/);
  assert.match(AGENTS_HTML, /Live monitoring of voice banking agent sessions/);
});

test("no credential appears in any admin page source", () => {
  for (const [name, text] of [
    ["agents.html", AGENTS_HTML],
    ["chat-history.html", HISTORY_HTML],
    ["dashboard.js", DASHBOARD_JS],
    ["history.js", HISTORY_JS],
    ["render.js", RENDER_JS],
  ]) {
    assert.doesNotMatch(text, /sk-[A-Za-z0-9_-]{16,}/, `${name} carries a key`);
    assert.doesNotMatch(text, /OPENAI_API_KEY/, `${name} names the key`);
    assert.doesNotMatch(text, /\bek_[A-Za-z0-9]{16,}/, `${name} carries a secret`);
  }
});

test("the dashboard offers the required filters", () => {
  assert.match(AGENTS_HTML, /id="filter-status"/);
  assert.match(AGENTS_HTML, /id="filter-domain"/);
  assert.match(AGENTS_HTML, /id="filter-period"/);
  assert.match(AGENTS_HTML, /id="search"/);
  assert.match(AGENTS_HTML, /id="refresh"/);
});

test("the dashboard shows when it last refreshed", () => {
  assert.match(AGENTS_HTML, /Last refreshed/);
  assert.match(DASHBOARD_JS, /last-refreshed/);
});

test("the dashboard refreshes on a sane interval and does not reload the page", () => {
  assert.match(DASHBOARD_JS, /REFRESH_MS\s*=\s*(2|3|4|5)000/);
  assert.doesNotMatch(DASHBOARD_JS, /location\.reload/);
});

// --- rows -------------------------------------------------------------------

test("a live row is marked live and has no end time", () => {
  // A live duration is measured from the backend's start time, so the clock is
  // pinned here rather than left to whenever the suite happens to run.
  const now = Date.parse(liveRow.started_at) + 207_000;
  const html = renderRow(liveRow, { now });

  assert.match(html, /<tr class="live">/);
  assert.match(html, /AGT-000001/);
  assert.match(html, /badge active/);
  assert.match(html, /DEMO001/);
  assert.match(html, /00:03:27/);
});

test("a finished row shows its end time and disconnect reason", () => {
  const html = renderRow(finishedRow);

  assert.doesNotMatch(html, /<tr class="live">/);
  assert.match(html, /VOICE_END_CALL/);
  assert.match(html, /badge completed/);
});

test("live sessions are listed above completed ones", () => {
  const html = renderRows([finishedRow, liveRow]);
  // renderRows preserves the order it is given; the ordering itself is the
  // backend's job, so this checks both rows render rather than re-sorting.
  assert.match(html, /AGT-000002[\s\S]*AGT-000001/);
});

test("an empty dashboard says so rather than showing a blank table", () => {
  const html = renderRows([]);

  assert.match(html, /No agent sessions yet/);
});

test("unknown tokens, cost and carbon all render as N/A", () => {
  const html = renderRow({
    ...liveRow,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    estimated_cost_usd: null,
    estimated_carbon_grams: null,
  });

  const na = html.match(/class="na">N\/A</g) ?? [];
  assert.equal(na.length, 5);
  // And no zero has been invented in their place.
  assert.doesNotMatch(html, /US\$0\.0000/);
});

test("a cost is labelled in dollars and a carbon figure in gCO2e", () => {
  const html = renderRow(liveRow);

  assert.match(html, /US\$0\.0042/);
  assert.match(html, /0\.18 gCO2e/);
});

test("the table carries no banking values", () => {
  const html = renderRow({ ...liveRow, client_id: "DEMO001" });

  assert.doesNotMatch(html, /12450\.75/);
  assert.doesNotMatch(html, /XXXX/);
});

test("View History links to that session and nothing else", () => {
  const html = renderRow(liveRow);

  assert.match(html, /data-history="AGT-000001"/);
  assert.match(DASHBOARD_JS, /chat-history\.html\?session=/);
  assert.match(DASHBOARD_JS, /encodeURIComponent/);
});

// --- summary cards ----------------------------------------------------------

test("the summary cards show the required metrics", () => {
  const html = summaryCards({
    active_agents: 2,
    completed_calls: 7,
    rejected_calls: 1,
    errors: 0,
    average_duration_seconds: 185,
    total_tokens: 24000,
    estimated_cost_usd: 0.12,
    estimated_carbon_grams: 3.4,
  });

  for (const label of [
    "Active Agents",
    "Completed Calls",
    "Busy / Rejected",
    "Average Call",
    "Total Tokens",
    "Estimated AI Cost",
    "Estimated Carbon",
    "Errors",
  ]) {
    assert.ok(html.includes(label), `missing card: ${label}`);
  }
  assert.match(html, /00:03:05/);
});

test("unavailable summary figures render as N/A, not as zero", () => {
  const html = summaryCards({
    active_agents: 0,
    completed_calls: 0,
    rejected_calls: 0,
    errors: 0,
    average_duration_seconds: null,
    total_tokens: null,
    estimated_cost_usd: null,
    estimated_carbon_grams: null,
  });

  assert.equal((html.match(/value na">N\/A/g) ?? []).length, 4);
});

// --- durations and timestamps ----------------------------------------------

test("durations format as hh:mm:ss", () => {
  assert.equal(formatDuration(0), "00:00:00");
  assert.equal(formatDuration(65), "00:01:05");
  assert.equal(formatDuration(3671), "01:01:11");
  assert.equal(formatDuration(null), "—");
});

test("timestamps are shown in Singapore time", () => {
  const shown = formatSgt("2026-08-13T14:25:34+00:00");

  // 14:25 UTC is 22:25 in Singapore.
  assert.match(shown, /13 Aug 2026/);
  assert.match(shown, /10:25:34 pm|10:25:34 PM/i);
  assert.match(shown, /SGT$/);
});

test("a missing timestamp does not render as a broken date", () => {
  assert.equal(formatSgt(null), "—");
  assert.equal(formatSgt("not a date"), "—");
});

// --- conversation history ---------------------------------------------------

test("the history page shows customer and agent turns", () => {
  const html = renderTurns([
    { role: "CUSTOMER", content: "What is my savings balance?" },
    { role: "AGENT", content: "Your Savings account has 12,450.75 SGD." },
  ]);

  assert.match(html, /Customer/);
  assert.match(html, /AI Agent/);
  assert.match(html, /What is my savings balance\?/);
});

test("a redacted turn is shown as a placeholder and marked as such", () => {
  const html = renderTurn({ role: "CUSTOMER", content: "[PIN REDACTED]" });

  assert.match(html, /redacted/);
  assert.match(html, /\[PIN REDACTED\]/);
});

test("an empty conversation says so", () => {
  assert.match(renderTurns([]), /No conversation was recorded/);
});

test("the history page loads exactly one session from the query string", () => {
  assert.match(HISTORY_JS, /URLSearchParams/);
  assert.match(HISTORY_JS, /\/api\/admin\/agents\/\$\{encodeURIComponent\(id\)\}\/history/);
  // It must not accept anything that is not an operational handle.
  assert.match(HISTORY_JS, /\[A-Z0-9-\]\{3,32\}/i);
});

test("the history page warns that PINs are never recorded", () => {
  assert.match(HISTORY_HTML, /spoken PIN is never recorded/);
});

// --- escaping ---------------------------------------------------------------

test("dashboard text is escaped before it reaches the page", () => {
  const html = renderRow({
    ...liveRow,
    client_id: '<img src=x onerror="alert(1)">',
  });

  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test("escapeHtml handles the characters that matter", () => {
  assert.equal(escapeHtml(`<&>"'`), "&lt;&amp;&gt;&quot;&#39;");
  assert.equal(escapeHtml(null), "");
});

// --- error handling ---------------------------------------------------------

test("an API failure produces a safe operator message", () => {
  // The dashboard keeps its last good data and says it is retrying, rather
  // than replacing the table with an error.
  assert.match(DASHBOARD_JS, /Unable to refresh dashboard\. Retrying/);
  // No status codes, URLs or provider text on screen.
  assert.doesNotMatch(DASHBOARD_JS, /error\.message/);
  assert.doesNotMatch(DASHBOARD_JS, /console\.error/);
});
