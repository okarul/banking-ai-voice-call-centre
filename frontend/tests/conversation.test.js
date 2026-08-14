/**
 * How the call behaves as a conversation: quiet lines, goodbyes, barge-in.
 *
 * Timers are injected, so twenty seconds of silence is tested in about a
 * millisecond and nothing here waits on a real clock.
 *
 * The behaviour under test is mostly about *when not to act*: the quiet timer
 * must not run while the assistant is speaking, while a banking tool is out, or
 * while a response is still being composed. Each of those has a failure it
 * prevents, and each gets its own test.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { BankingCall, Speech, State, Timing } from "../call.js";

/** A timer wheel that fires only what the test asks it to. */
function fakeTimers() {
  let nextId = 1;
  const scheduled = new Map();
  return {
    scheduled,
    set(fn, ms) {
      const id = nextId++;
      scheduled.set(id, { fn, ms });
      return id;
    },
    clear(id) {
      scheduled.delete(id);
    },
    /** Fire every timer waiting on exactly this delay. */
    async fire(ms) {
      const due = [...scheduled.entries()].filter(([, t]) => t.ms === ms);
      for (const [id, timer] of due) {
        scheduled.delete(id);
        await timer.fn();
      }
      return due.length;
    },
    waitingFor(ms) {
      return [...scheduled.values()].some((t) => t.ms === ms);
    },
    count() {
      return scheduled.size;
    },
  };
}

function harness() {
  const sent = [];
  const ended = [];
  const timers = fakeTimers();

  const track = { kind: "audio", enabled: true, stop() {} };
  const stream = { getTracks: () => [track] };
  const channel = {
    readyState: "open",
    send: (payload) => sent.push(JSON.parse(payload)),
    close() {
      channel.readyState = "closed";
    },
  };
  const peer = {
    addTrack() {},
    createDataChannel: () => channel,
    createOffer: async () => ({ sdp: "v=0 offer", type: "offer" }),
    setLocalDescription: async () => {},
    setRemoteDescription: async () => {},
    close() {},
    ontrack: null,
  };

  const fetchImpl = async (url) => {
    if (url.endsWith("/api/call/start")) {
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
    if (url.endsWith("/api/call/scope")) {
      return { ok: true, async json() { return { allowed: true }; } };
    }
    if (url.includes("/api/call/end")) {
      ended.push(url);
      return { ok: true, async json() { return { success: true }; } };
    }
    throw new Error(`unexpected request to ${url}`);
  };

  const call = new BankingCall({
    apiBase: "http://127.0.0.1:8001",
    deps: {
      fetch: fetchImpl,
      getUserMedia: async () => stream,
      createPeerConnection: () => peer,
      createAudioSink: () => ({ srcObject: null }),
      now: () => 100000,
      setTimer: (fn, ms) => timers.set(fn, ms),
      clearTimer: (id) => timers.clear(id),
    },
  });

  return { call, channel, sent, timers, ended, track };
}

async function connected() {
  const kit = harness();
  await kit.call.start();
  kit.channel.onopen();
  return kit;
}

/** Deliver one Realtime event to the page. */
function deliver(kit, event) {
  return kit.call.receive({ data: JSON.stringify(event) });
}

/** The assistant finishes speaking, with no tool calls. */
function assistantFinished(kit, transcript = "Your balance is 12,450.75 SGD.") {
  deliver(kit, {
    type: "response.output_audio_transcript.done",
    transcript,
  });
  return deliver(kit, { type: "response.done", response: { status: "completed" } });
}

const instructionOf = (event) => event?.response?.instructions ?? "";
const spoken = (sent) =>
  sent.filter((e) => e.type === "response.create").map(instructionOf);

// === the quiet line =========================================================

test("the quiet timer starts only once the assistant has finished", async () => {
  const kit = await connected();
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), false);

  await assistantFinished(kit);

  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), true);
});

test("ten seconds of silence asks the customer if they are still there", async () => {
  const kit = await connected();
  await assistantFinished(kit);

  await kit.timers.fire(Timing.SILENCE_CHECK_MS);

  assert.ok(
    spoken(kit.sent).some((text) => text.includes(Speech.SILENCE_CHECK)),
    `expected the check question, got ${JSON.stringify(spoken(kit.sent))}`
  );
  assert.equal(kit.call.awaitingSilenceReply, true);
  // The call is still up. Asking is not hanging up.
  assert.equal(kit.call.state, State.CONNECTED);
  assert.equal(kit.ended.length, 0);
});

test("a further ten seconds of silence closes the call", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  await kit.timers.fire(Timing.SILENCE_CHECK_MS);

  // The check question is spoken and finishes with no reply.
  await assistantFinished(kit, Speech.SILENCE_CHECK);
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CLOSE_MS), true);

  await kit.timers.fire(Timing.SILENCE_CLOSE_MS);

  assert.ok(spoken(kit.sent).some((t) => t.includes(Speech.SILENCE_CLOSING)));
  assert.equal(kit.call.endAfterSpeech, true);
  // Not yet hung up: the closing line has not finished playing.
  assert.equal(kit.ended.length, 0);

  await assistantFinished(kit, Speech.SILENCE_CLOSING);

  assert.equal(kit.ended.length, 1);
  assert.equal(kit.call.state, State.DISCONNECTED);
  assert.equal(kit.call.sessionId, null);
});

test("the customer speaking cancels the countdown", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), true);

  deliver(kit, { type: "input_audio_buffer.speech_started" });

  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), false);
});

test("answering the check question rescues the call", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  await kit.timers.fire(Timing.SILENCE_CHECK_MS);
  assert.equal(kit.call.awaitingSilenceReply, true);

  deliver(kit, { type: "input_audio_buffer.speech_started" });

  assert.equal(kit.call.awaitingSilenceReply, false);
  assert.equal(kit.call.endAfterSpeech, false);
  assert.equal(kit.ended.length, 0);

  // And the next quiet spell starts from the full ten seconds again.
  await assistantFinished(kit);
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), true);
});

test("a banking question after the warning is answered with no extra prompt", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  await kit.timers.fire(Timing.SILENCE_CHECK_MS);

  deliver(kit, { type: "input_audio_buffer.speech_started" });
  const before = kit.sent.length;
  deliver(kit, { type: "input_audio_buffer.speech_stopped" });

  // Nothing is injected into their turn: no re-prompt, no repeated question.
  const injected = kit.sent
    .slice(before)
    .filter((e) => e.type === "response.create");
  assert.deepEqual(injected, []);
  assert.equal(kit.call.awaitingSilenceReply, false);
});

// === when the timer must not run ===========================================

test("the timer does not run while the assistant is speaking", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), true);

  deliver(kit, { type: "response.output_audio.delta" });

  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), false);
});

test("the timer does not run while a response is being composed", async () => {
  const kit = await connected();
  await assistantFinished(kit);

  deliver(kit, { type: "response.created" });

  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), false);
});

test("the timer does not run while a banking tool is out", async () => {
  const kit = await connected();

  await deliver(kit, {
    type: "response.done",
    response: {
      status: "completed",
      output: [
        {
          type: "function_call",
          call_id: "call-1",
          name: "get_account_balance",
          arguments: JSON.stringify({ account_type: "Savings" }),
        },
      ],
    },
  });

  // The tool ran and a fresh response was asked for, so the line is busy —
  // a slow database must never look like an absent customer.
  assert.equal(kit.call.toolsRunning, 0);
  assert.equal(kit.call.responseActive, true);
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), false);
});

// === saying goodbye =========================================================

test("the closing line hangs up once it has finished playing", async () => {
  const kit = await connected();

  deliver(kit, {
    type: "response.output_audio_transcript.done",
    transcript: "Thank you for calling ABC Demo Bank. Have a good day. Goodbye.",
  });
  // Still connected: the sentence is only half spoken.
  assert.equal(kit.ended.length, 0);
  assert.equal(kit.call.endAfterSpeech, true);

  await deliver(kit, { type: "response.done", response: { status: "completed" } });

  assert.equal(kit.ended.length, 1);
  assert.equal(kit.call.state, State.DISCONNECTED);
});

test("the welcome does not read as a goodbye", async () => {
  const kit = await connected();

  await assistantFinished(
    kit,
    "Welcome to ABC Demo Bank. Thank you for calling. How may I assist you today?"
  );

  assert.equal(kit.call.endAfterSpeech, false);
  assert.equal(kit.ended.length, 0);
  assert.equal(kit.call.state, State.CONNECTED);
});

test("a plain thank you does not end the call", async () => {
  const kit = await connected();

  await assistantFinished(
    kit,
    "You're welcome. Is there anything else I can help you with today?"
  );

  assert.equal(kit.ended.length, 0);
  assert.equal(kit.call.state, State.CONNECTED);
  // And the bank goes back to waiting, rather than hanging up on them.
  assert.equal(kit.timers.waitingFor(Timing.SILENCE_CHECK_MS), true);
});

// === barge-in ===============================================================

test("speaking over the assistant cancels the stale answer", async () => {
  const kit = await connected();
  deliver(kit, { type: "response.output_audio.delta" });
  assert.equal(kit.call.assistantSpeaking, true);

  deliver(kit, { type: "input_audio_buffer.speech_started" });

  assert.ok(kit.sent.some((e) => e.type === "response.cancel"));
  assert.equal(kit.call.assistantSpeaking, false);
});

test("barge-in stops the old turn answering the new question", async () => {
  const kit = await connected();
  const askedOn = kit.call.turn;

  deliver(kit, { type: "input_audio_buffer.speech_started" });

  assert.equal(kit.call.turn, askedOn + 1);
});

// === cleanup and capacity ===================================================

test("hanging up stops every timer", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  assert.ok(kit.timers.count() > 0);

  await kit.call.end();

  assert.equal(kit.timers.count(), 0);
});

test("a call that runs too long is closed", async () => {
  const kit = await connected();
  assert.equal(kit.timers.waitingFor(Timing.MAX_CALL_MS), true);

  await kit.timers.fire(Timing.MAX_CALL_MS);

  assert.equal(kit.ended.length, 1);
  assert.equal(kit.call.state, State.DISCONNECTED);
});

test("ending twice is harmless and releases the line once", async () => {
  const kit = await connected();

  assert.equal(await kit.call.end(), true);
  assert.equal(await kit.call.end(), false);

  assert.equal(kit.ended.length, 1);
  assert.equal(kit.timers.count(), 0);
});

test("a timer cannot fire into a call that has already ended", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  await kit.call.end();

  const fired = await kit.timers.fire(Timing.SILENCE_CHECK_MS);

  assert.equal(fired, 0);
  assert.equal(kit.ended.length, 1);
});

test("the silence closing releases the banking session on the server", async () => {
  const kit = await connected();
  await assistantFinished(kit);
  await kit.timers.fire(Timing.SILENCE_CHECK_MS);
  await assistantFinished(kit, Speech.SILENCE_CHECK);
  await kit.timers.fire(Timing.SILENCE_CLOSE_MS);
  await assistantFinished(kit, Speech.SILENCE_CLOSING);

  // The one thing that frees provider capacity is this call reaching the
  // backend. Every automatic termination path must make it.
  assert.equal(kit.ended.length, 1);
  assert.equal(kit.call.sessionId, null);
  assert.equal(kit.track.enabled, true);
});

// === the wording ============================================================

test("the spoken lines are the agreed ones", () => {
  assert.equal(
    Speech.SILENCE_CHECK,
    "I do not hear anything from you. Do you want to continue?"
  );
  assert.equal(Speech.SILENCE_CLOSING, "Thank you. Please try again.");
});

test("the timings are the agreed ones", () => {
  assert.equal(Timing.SILENCE_CHECK_MS, 10000);
  assert.equal(Timing.SILENCE_CLOSE_MS, 10000);
});
