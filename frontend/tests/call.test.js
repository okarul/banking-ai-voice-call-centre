/**
 * Frontend tests: the handset, without a browser.
 *
 * Every browser API the call touches is injected, so these run on plain Node
 * with no microphone, no network, no WebRTC and no paid usage:
 *
 *     node --test tests/
 *
 * What they defend is the behaviour a customer would notice — one call per
 * page, mute really stops the microphone, hanging up really releases it — plus
 * the one thing they could not notice and would most regret: a credential in
 * the page source.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import { BankingCall, State, formatDuration } from "../call.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const source = (name) => readFileSync(join(HERE, "..", name), "utf8");

const HTML = source("index.html");
const APP_JS = source("app.js");
const CALL_JS = source("call.js");

// --- the page itself --------------------------------------------------------

test("the page offers a Start Call button", () => {
  assert.match(HTML, /id="start-call"[^>]*>[\s\S]*?Start Call/);
});

test("the page offers an End Call button", () => {
  assert.match(HTML, /id="end-call"[^>]*>[\s\S]*?End Call/);
});

test("the page offers a Mute button", () => {
  assert.match(HTML, /id="mute"[^>]*>[\s\S]*?Mute/);
});

test("the page gives the customer nowhere to type", () => {
  // A voice-only line. Any input, textarea or editable region would be a way
  // to bank by typing, which this interface deliberately does not offer.
  assert.doesNotMatch(HTML, /<input\b/i);
  assert.doesNotMatch(HTML, /<textarea\b/i);
  assert.doesNotMatch(HTML, /contenteditable/i);
  assert.doesNotMatch(HTML, /<form\b/i);
});

test("no credential appears anywhere in the page source", () => {
  for (const [name, text] of [
    ["index.html", HTML],
    ["app.js", APP_JS],
    ["call.js", CALL_JS],
  ]) {
    assert.doesNotMatch(text, /sk-[A-Za-z0-9_-]{16,}/, `${name} carries a key`);
    assert.doesNotMatch(text, /OPENAI_API_KEY/, `${name} names the key`);
    assert.doesNotMatch(text, /\bek_[A-Za-z0-9]{16,}/, `${name} carries a secret`);
  }
});

test("the page never stores anything in browser storage", () => {
  // Nothing about a call outlives it: no session id, and above all no PIN.
  for (const text of [APP_JS, CALL_JS]) {
    assert.doesNotMatch(text, /localStorage/);
    assert.doesNotMatch(text, /sessionStorage/);
    assert.doesNotMatch(text, /document\.cookie/);
  }
});

// --- a call, with everything mocked ----------------------------------------

function harness(options = {}) {
  const { micFails = false, startStatus = 201 } = options;

  const track = { enabled: true, stopped: false, stop() { this.stopped = true; } };
  const stream = { getTracks: () => [track] };

  const channel = {
    readyState: "open",
    sent: [],
    onmessage: null,
    onopen: null,
    send(payload) { this.sent.push(JSON.parse(payload)); },
    close() { this.readyState = "closed"; },
  };

  const peer = {
    closed: false,
    added: [],
    local: null,
    remote: null,
    connectionState: "connecting",
    ontrack: null,
    onconnectionstatechange: null,
    addTrack(t) { this.added.push(t); },
    createDataChannel() { return channel; },
    async createOffer() { return { type: "offer", sdp: "v=0 offer" }; },
    async setLocalDescription(d) { this.local = d; },
    async setRemoteDescription(d) { this.remote = d; },
    close() { this.closed = true; },
  };

  const requests = [];
  let micRequests = 0;

  const fetchImpl = async (url, init = {}) => {
    requests.push({ url, init });
    if (url.endsWith("/api/call/start")) {
      if (startStatus !== 201) {
        return { ok: false, status: startStatus, async json() { return {}; } };
      }
      return {
        ok: true,
        async json() {
          return {
            session_id: "SESSION-test-0001",
            realtime_session_id: "REALTIME-test-0001",
            client_secret: { value: "ek_mock_secret", expires_at: 99 },
          };
        },
      };
    }
    if (url.startsWith("https://api.openai.com/")) {
      return { ok: true, async text() { return "v=0 answer"; } };
    }
    if (url.endsWith("/api/call/tool")) {
      return {
        ok: true,
        async json() {
          return { result: { success: true, available_balance: "12450.75" } };
        },
      };
    }
    if (url.includes("/api/call/end")) {
      return { ok: true, async json() { return { success: true }; } };
    }
    throw new Error(`unexpected request to ${url}`);
  };

  const states = [];
  const errors = [];
  const clock = { at: 100_000 };
  const timers = new Map();

  const call = new BankingCall({
    apiBase: "http://127.0.0.1:8001",
    deps: {
      fetch: fetchImpl,
      getUserMedia: async () => {
        micRequests += 1;
        if (micFails) throw new Error("NotAllowedError");
        return stream;
      },
      createPeerConnection: () => peer,
      createAudioSink: () => ({ srcObject: null }),
      now: () => clock.at,
      // Injected so a live call's maximum-duration timer does not sit in the
      // Node event loop for fifteen minutes and hold the test run open.
      setTimer: (fn, ms) => {
        const id = Symbol("timer");
        timers.set(id, { fn, ms });
        return id;
      },
      clearTimer: (id) => timers.delete(id),
      onState: (state) => states.push(state),
      onError: (text) => errors.push(text),
    },
  });

  return {
    call, peer, channel, track, stream, states, errors, requests, clock, timers,
    micCount: () => micRequests,
  };
}

/** Bring a harnessed call all the way up, as the data channel opening would. */
async function connected(options) {
  const kit = harness(options);
  await kit.call.start();
  kit.channel.onopen();
  return kit;
}

test("Start Call asks for the microphone", async () => {
  const kit = await connected();
  assert.equal(kit.micCount(), 1);
  assert.deepEqual(kit.peer.added, [kit.track]);
});

test("Start Call moves the status from Connecting to Connected", async () => {
  const kit = harness();

  await kit.call.start();
  assert.deepEqual(kit.states, [State.CONNECTING]);

  kit.channel.onopen();
  assert.deepEqual(kit.states, [State.CONNECTING, State.CONNECTED]);
  assert.equal(kit.call.state, State.CONNECTED);
});

test("Start Call binds the page to the banking session the backend created", async () => {
  const kit = await connected();
  assert.equal(kit.call.sessionId, "SESSION-test-0001");
  assert.equal(kit.call.realtimeSessionId, "REALTIME-test-0001");
});

test("the offer goes to the documented calls endpoint with the short-lived secret", async () => {
  const kit = await connected();
  const sdp = kit.requests.find((r) => r.url.startsWith("https://api.openai.com/"));

  assert.equal(sdp.url, "https://api.openai.com/v1/realtime/calls");
  assert.equal(sdp.init.headers["Content-Type"], "application/sdp");
  assert.equal(sdp.init.headers.Authorization, "Bearer ek_mock_secret");
  assert.equal(kit.peer.remote.sdp, "v=0 answer");
});

test("a second Start Call cannot open a second line", async () => {
  const kit = await connected();
  const before = kit.requests.length;

  assert.equal(await kit.call.start(), false);
  assert.equal(await kit.call.start(), false);

  assert.equal(kit.micCount(), 1);
  assert.equal(kit.requests.length, before);
});

test("the timer runs from the moment the line opens", async () => {
  const kit = harness();
  await kit.call.start();
  assert.equal(kit.call.durationSeconds(), 0);

  kit.channel.onopen();
  kit.clock.at += 42_000;

  assert.equal(kit.call.durationSeconds(), 42);
  assert.equal(formatDuration(kit.call.durationSeconds()), "00:42");
});

test("the bank speaks first", async () => {
  const kit = await connected();
  assert.deepEqual(kit.channel.sent, [{ type: "response.create" }]);
});

// --- mute -------------------------------------------------------------------

test("Mute stops the microphone being transmitted", async () => {
  const kit = await connected();

  assert.equal(kit.call.toggleMute(), true);

  assert.equal(kit.track.enabled, false);
  assert.equal(kit.call.state, State.MUTED);
  // Muting is not hanging up: the line and the session are still there.
  assert.equal(kit.peer.closed, false);
  assert.equal(kit.call.sessionId, "SESSION-test-0001");
});

test("Unmute puts the microphone back", async () => {
  const kit = await connected();

  kit.call.toggleMute();
  kit.call.toggleMute();

  assert.equal(kit.track.enabled, true);
  assert.equal(kit.call.state, State.CONNECTED);
});

test("mute does nothing when there is no call", () => {
  const kit = harness();
  assert.equal(kit.call.toggleMute(), false);
});

// --- banking over the data channel -----------------------------------------

test("a tool call is answered from the bank, bound to this call's session", async () => {
  const kit = await connected();

  await kit.call.receive({
    data: JSON.stringify({
      type: "response.done",
      response: {
        output: [
          {
            type: "function_call",
            name: "get_account_balance",
            call_id: "call_abc123",
            arguments: JSON.stringify({ account_type: "Savings" }),
          },
        ],
      },
    }),
  });

  const toolCall = kit.requests.find((r) => r.url.endsWith("/api/call/tool"));
  const body = JSON.parse(toolCall.init.body);

  // The page supplies the identity, from its own session. The model supplied
  // only which account type it wanted.
  assert.equal(body.session_id, "SESSION-test-0001");
  assert.equal(body.name, "get_account_balance");
  assert.deepEqual(body.arguments, { account_type: "Savings" });

  const output = kit.channel.sent.find((e) => e.type === "conversation.item.create");
  assert.equal(output.item.type, "function_call_output");
  assert.equal(output.item.call_id, "call_abc123");
  assert.match(output.item.output, /12450\.75/);

  // And the model is asked to speak the answer.
  assert.equal(kit.channel.sent.at(-1).type, "response.create");
});

/** Build a response.done event carrying one function call. */
function toolCallEvent(name, callId, args = {}, status = "completed") {
  return {
    data: JSON.stringify({
      type: "response.done",
      response: {
        status,
        output: [
          {
            type: "function_call",
            name,
            call_id: callId,
            arguments: JSON.stringify(args),
          },
        ],
      },
    }),
  };
}

const toolNames = (kit) =>
  kit.requests
    .filter((r) => r.url.endsWith("/api/call/tool"))
    .map((r) => JSON.parse(r.init.body).name);

test("a cancelled response never reaches the bank", async () => {
  // Barge-in cancels the response the model was building. Running its tool
  // anyway is how the previous question's answer ends up being spoken over
  // the new one.
  const kit = await connected();

  await kit.call.receive(
    toolCallEvent("get_recent_transactions", "call_old", {}, "cancelled")
  );

  assert.deepEqual(toolNames(kit), []);
  assert.equal(
    kit.channel.sent.filter((e) => e.type === "conversation.item.create").length,
    0
  );
});

test("the same tool call is never run twice", async () => {
  const kit = await connected();
  const event = toolCallEvent("get_account_balance", "call_same");

  await kit.call.receive(event);
  await kit.call.receive(event);

  assert.deepEqual(toolNames(kit), ["get_account_balance"]);
});

test("a tool result from the previous question does not answer the new one", async () => {
  const kit = await connected();

  // The model asks for transactions. While the bank is answering, the caller
  // starts asking something else.
  let release;
  const held = new Promise((resolve) => (release = resolve));
  const realFetch = kit.call.fetch;
  kit.call.fetch = async (url, init) => {
    if (url.endsWith("/api/call/tool")) {
      await held;
    }
    return realFetch(url, init);
  };

  const running = kit.call.receive(toolCallEvent("get_recent_transactions", "c1"));
  kit.call.receive({
    data: JSON.stringify({ type: "input_audio_buffer.speech_started" }),
  });
  release();
  await running;

  // The result is still handed back, so the conversation stays consistent...
  const output = kit.channel.sent.find((e) => e.type === "conversation.item.create");
  assert.equal(output.item.call_id, "c1");
  // ...but the old turn does not get to speak over the new question. The
  // caller's new turn drives its own response.
  const spoken = kit.channel.sent.filter((e) => e.type === "response.create");
  assert.equal(spoken.length, 1, "only the opening greeting should have asked to speak");
});

test("a normal tool call still asks the model to speak the answer", async () => {
  const kit = await connected();

  await kit.call.receive(toolCallEvent("get_account_balance", "c2"));

  // The greeting, plus the one that speaks this answer.
  assert.equal(
    kit.channel.sent.filter((e) => e.type === "response.create").length,
    2
  );
});

test("the page never sends an identity of its own choosing", () => {
  // The only identity on the wire is the session id the backend issued. If a
  // customer id ever appeared in this file, the page would be choosing whose
  // money to read.
  assert.doesNotMatch(CALL_JS, /customer_id/);
});

test("a tool the bank cannot run is reported as a failure, not invented", async () => {
  const kit = await connected();
  kit.call.fetch = async () => {
    throw new Error("offline");
  };

  await kit.call.receive({
    data: JSON.stringify({
      type: "response.done",
      response: {
        output: [
          {
            type: "function_call",
            name: "get_account_balance",
            call_id: "c1",
            arguments: "{}",
          },
        ],
      },
    }),
  });

  // The model is told the tool failed, so it says it cannot retrieve the
  // information. It is never handed a made-up balance.
  const output = kit.channel.sent.find((e) => e.type === "conversation.item.create");
  assert.match(output.item.output, /TOOL_UNAVAILABLE/);
});

// --- hanging up -------------------------------------------------------------

test("End Call stops the microphone, closes the line and clears the session", async () => {
  const kit = await connected();

  assert.equal(await kit.call.end(), true);

  assert.equal(kit.track.stopped, true);
  assert.equal(kit.peer.closed, true);
  assert.equal(kit.channel.readyState, "closed");
  assert.equal(kit.call.sessionId, null);
  assert.equal(kit.call.state, State.DISCONNECTED);
});

test("End Call tells the backend which session to end", async () => {
  const kit = await connected();
  await kit.call.end();

  const ending = kit.requests.find((r) => r.url.includes("/api/call/end"));
  assert.deepEqual(JSON.parse(ending.init.body), { session_id: "SESSION-test-0001" });
});

test("End Call resets the timer", async () => {
  const kit = await connected();
  kit.clock.at += 30_000;
  assert.equal(kit.call.durationSeconds(), 30);

  await kit.call.end();

  assert.equal(kit.call.durationSeconds(), 0);
  assert.equal(formatDuration(kit.call.durationSeconds()), "00:00");
});

test("hanging up twice is harmless", async () => {
  const kit = await connected();

  assert.equal(await kit.call.end(), true);
  assert.equal(await kit.call.end(), false);
  assert.equal(await kit.call.end(), false);

  assert.equal(kit.call.state, State.DISCONNECTED);
  const endings = kit.requests.filter((r) => r.url.includes("/api/call/end"));
  assert.equal(endings.length, 1);
});

test("a closing tab still reports the hang-up", async () => {
  const kit = await connected();
  const beacons = [];

  const sent = kit.call.hangUpBeacon((url, body) => {
    beacons.push({ url, body });
    return true;
  });

  assert.equal(sent, true);
  assert.equal(beacons[0].url, "http://127.0.0.1:8001/api/call/end");
});

// --- the scope gate ---------------------------------------------------------

/** Build the transcription event the page gates on. */
function heardEvent(transcript) {
  return {
    data: JSON.stringify({
      type: "conversation.item.input_audio_transcription.completed",
      transcript,
    }),
  };
}

/** A harness whose /api/call/scope returns a fixed ruling. */
function withScope(kit, decision) {
  const realFetch = kit.call.fetch;
  kit.call.fetch = async (url, init) => {
    if (url.endsWith("/api/call/scope")) {
      kit.requests.push({ url, init });
      return { ok: true, async json() { return decision; } };
    }
    return realFetch(url, init);
  };
}

test("an out-of-scope turn is cancelled and the bank's sentence is spoken", async () => {
  const kit = await connected();
  withScope(kit, {
    category: "NON_BANKING_REQUEST",
    allowed: false,
    speech: "I'm here to help with your ABC Demo Bank account and loan enquiries.",
  });

  await kit.call.enforceScope("What is the capital of France?");

  const cancelled = kit.channel.sent.find((e) => e.type === "response.cancel");
  assert.ok(cancelled, "the model's reply must be cancelled");

  const forced = kit.channel.sent.filter((e) => e.type === "response.create").at(-1);
  assert.match(forced.response.instructions, /ABC Demo Bank account and loan/);
  // The page never puts the answer into the caller's ears.
  assert.doesNotMatch(JSON.stringify(kit.channel.sent), /Paris/);
});

test("an in-scope turn is left alone", async () => {
  const kit = await connected();
  const before = kit.channel.sent.length;
  withScope(kit, {
    category: "OWN_ACCOUNT_ENQUIRY",
    allowed: true,
    speech: null,
  });

  await kit.call.enforceScope("What is my savings balance?");

  // No cancellation, no forced sentence — the model answers normally.
  assert.equal(kit.channel.sent.length, before);
});

test("the caller's words are sent to the gate and kept nowhere", async () => {
  const kit = await connected();
  withScope(kit, { category: "AUTHENTICATION", allowed: true, speech: null });

  await kit.call.enforceScope("My PIN is four eight two one");

  const gate = kit.requests.find((r) => r.url.endsWith("/api/call/scope"));
  assert.equal(JSON.parse(gate.init.body).session_id, "SESSION-test-0001");
  // Nothing spoken to the caller, and nothing retained on the call object.
  assert.doesNotMatch(JSON.stringify(kit.call.answeredCalls), /four eight/);
  assert.equal(kit.call.transcript, undefined);
});

test("a gate that cannot be reached does not break the call", async () => {
  const kit = await connected();
  kit.call.fetch = async () => {
    throw new Error("offline");
  };

  await kit.call.enforceScope("What is the capital of France?");

  assert.equal(kit.call.state, State.CONNECTED);
});

// --- two windows at once ----------------------------------------------------
//
// Two browser windows are two page loads, so they are two BankingCall objects
// with nothing between them. These tests make that explicit: the isolation is
// structural, not a rule the code remembers to follow.

test("two windows hold two separate calls", async () => {
  const a = await connected();
  const b = await connected();

  assert.notEqual(a.call, b.call);
  assert.notEqual(a.peer, b.peer);
  assert.notEqual(a.track, b.track);
  // Distinct microphones, distinct peer connections, distinct data channels.
  assert.equal(a.peer.added.length, 1);
  assert.equal(b.peer.added.length, 1);
});

test("each window keeps its own session and call state", async () => {
  const a = await connected();
  const b = await connected();

  // Same mocked backend here, so the ids are forced apart to model two
  // genuinely different calls.
  a.call.sessionId = "SESSION-a";
  b.call.sessionId = "SESSION-b";

  a.call.toggleMute();

  assert.equal(a.call.state, State.MUTED);
  assert.equal(b.call.state, State.CONNECTED);
  assert.equal(a.track.enabled, false);
  assert.equal(b.track.enabled, true);
  assert.equal(b.call.sessionId, "SESSION-b");
});

test("a tool call on one window is bound to that window's session", async () => {
  const a = await connected();
  const b = await connected();
  a.call.sessionId = "SESSION-a";
  b.call.sessionId = "SESSION-b";

  await a.call.receive(toolCallEvent("get_account_balance", "call_a"));
  await b.call.receive(toolCallEvent("get_account_balance", "call_b"));

  const sessionOf = (kit) =>
    kit.requests
      .filter((r) => r.url.endsWith("/api/call/tool"))
      .map((r) => JSON.parse(r.init.body).session_id);

  assert.deepEqual(sessionOf(a), ["SESSION-a"]);
  assert.deepEqual(sessionOf(b), ["SESSION-b"]);
});

test("hanging up one window leaves the other connected", async () => {
  const a = await connected();
  const b = await connected();

  await a.call.end();

  assert.equal(a.call.state, State.DISCONNECTED);
  assert.equal(a.track.stopped, true);
  assert.equal(a.peer.closed, true);

  assert.equal(b.call.state, State.CONNECTED);
  assert.equal(b.track.stopped, false);
  assert.equal(b.peer.closed, false);
  assert.notEqual(b.call.sessionId, null);
});

test("the surviving window can still bank after the other hangs up", async () => {
  const a = await connected();
  const b = await connected();
  b.call.sessionId = "SESSION-b";

  await a.call.end();
  await b.call.receive(toolCallEvent("get_account_balance", "call_b"));

  const toolCall = b.requests.find((r) => r.url.endsWith("/api/call/tool"));
  assert.equal(JSON.parse(toolCall.init.body).session_id, "SESSION-b");
  assert.equal(b.call.state, State.CONNECTED);
});

test("an error in one window does not disturb the other", async () => {
  const b = await connected();
  const a = harness({ micFails: true });

  assert.equal(await a.call.start(), false);

  assert.equal(a.call.state, State.ERROR);
  assert.equal(b.call.state, State.CONNECTED);
  assert.equal(b.track.stopped, false);
});

test("one window's turn counter does not suppress the other's answer", async () => {
  const a = await connected();
  const b = await connected();

  // A's caller starts speaking again, which supersedes A's pending turn only.
  a.call.receive({
    data: JSON.stringify({ type: "input_audio_buffer.speech_started" }),
  });
  await b.call.receive(toolCallEvent("get_account_balance", "call_b"));

  // B still asks for its answer to be spoken.
  assert.equal(
    b.channel.sent.filter((e) => e.type === "response.create").length,
    2
  );
  assert.equal(a.call.turn, 1);
  assert.equal(b.call.turn, 0);
});

// --- when things go wrong ---------------------------------------------------

test("a refused microphone is explained, and no session is left open", async () => {
  const kit = harness({ micFails: true });

  assert.equal(await kit.call.start(), false);

  assert.deepEqual(kit.errors, ["Microphone access is required for voice banking."]);
  assert.equal(kit.call.state, State.ERROR);
  // The banking session is never created if the caller cannot speak.
  assert.equal(kit.requests.length, 0);
});

test("a backend that cannot open a call says so plainly", async () => {
  const kit = harness({ startStatus: 503 });

  assert.equal(await kit.call.start(), false);

  assert.deepEqual(kit.errors, ["Unable to connect to voice banking. Please try again."]);
  assert.equal(kit.call.state, State.ERROR);
  assert.equal(kit.track.stopped, true);
});

test("after an error the customer can try again", async () => {
  // A failed attempt must not leave the handset dead. The backend recovers
  // between the two attempts, and the second one has to get through.
  let failing = true;
  const kit = harness();
  const realFetch = kit.call.fetch;
  kit.call.fetch = async (url, init) => {
    if (failing && url.endsWith("/api/call/start")) {
      return { ok: false, status: 503, async json() { return {}; } };
    }
    return realFetch(url, init);
  };

  assert.equal(await kit.call.start(), false);
  assert.equal(kit.call.state, State.ERROR);

  failing = false;
  assert.equal(await kit.call.start(), true);
  kit.channel.onopen();
  assert.equal(kit.call.state, State.CONNECTED);
});
