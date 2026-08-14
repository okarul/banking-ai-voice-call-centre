/**
 * The handset: everything that happens between picking up and putting down.
 *
 * The page is a telephone, so this module is written as one. It knows how to
 * take a line (`start`), cover the mouthpiece (`setMuted`) and hang up (`end`),
 * and it knows nothing about how any of that is drawn on screen — app.js owns
 * the DOM, this owns the call.
 *
 * Audio never passes through the bank's server. The microphone goes straight to
 * OpenAI over WebRTC and the spoken reply comes straight back, which is what
 * makes it sound like a phone call rather than a series of uploads. What does
 * pass through the bank is every question about money: when the model wants a
 * balance it says so on the data channel, and this module relays that to
 * /api/call/tool, where the existing guards decide what may be answered.
 *
 * The permanent API key is not here and cannot be. The only credential this
 * code ever sees is the short-lived client secret minted per call by the
 * backend, which authorises one realtime call and expires by itself.
 *
 * Every browser API is injectable so the whole flow can be tested without a
 * microphone, a network or a browser.
 */

/** Where the browser posts its SDP offer. */
export const CALL_URL = "https://api.openai.com/v1/realtime/calls";

/** The channel name the Realtime API expects for events. */
export const EVENT_CHANNEL = "oai-events";

export const State = {
  DISCONNECTED: "Disconnected",
  CONNECTING: "Connecting",
  CONNECTED: "Connected",
  MUTED: "Muted",
  ENDING: "Ending",
  ERROR: "Error",
};

/** What the customer is told when something goes wrong. Never a raw error. */
export const Messages = {
  MIC_DENIED: "Microphone access is required for voice banking.",
  REALTIME_FAILED: "Unable to connect to voice banking. Please try again.",
  BACKEND_DOWN: "Banking service is temporarily unavailable.",
};

/** Returned to the model when a tool could not be reached at all. */
const TOOL_UNAVAILABLE = { success: false, reason: "TOOL_UNAVAILABLE" };

/**
 * How long the line may stay quiet, and how long a call may run.
 *
 * The quiet timer only ever runs while the bank is genuinely waiting for the
 * caller — never while the assistant is speaking, a banking tool is running,
 * or a response is still being generated. Otherwise a slow balance lookup would
 * look exactly like an absent customer.
 */
export const Timing = {
  // Both customer waits are ten seconds: ten to answer before the bank asks
  // whether anyone is there, and ten more before it closes the line.
  SILENCE_CHECK_MS: 10000,
  SILENCE_CLOSE_MS: 10000,
  MAX_CALL_MS: 15 * 60 * 1000,
};

/** Lines the page makes the assistant say. Kept identical to the backend's. */
export const Speech = {
  SILENCE_CHECK: "I do not hear anything from you. Do you want to continue?",
  SILENCE_CLOSING: "Thank you. Please try again.",
};

/**
 * The assistant's closing line, which is the page's cue to hang up.
 *
 * Matched on "goodbye" as a whole word. The greeting says "Welcome to ABC Demo
 * Bank. Thank you for calling." and never says goodbye, so this cannot fire on
 * the way in — and only the assistant's own transcript is ever tested, so a
 * caller saying goodbye does not end the call until the bank has said its line.
 */
const CLOSING_LINE = /\bgoodbye\b/i;

export class BankingCall {
  constructor({ apiBase, deps = {} } = {}) {
    this.apiBase = apiBase ?? "http://127.0.0.1:8001";

    this.fetch = deps.fetch ?? ((...args) => globalThis.fetch(...args));
    this.getUserMedia =
      deps.getUserMedia ??
      ((constraints) => navigator.mediaDevices.getUserMedia(constraints));
    this.createPeerConnection =
      deps.createPeerConnection ?? (() => new RTCPeerConnection());
    this.createAudioSink = deps.createAudioSink ?? defaultAudioSink;
    this.now = deps.now ?? (() => Date.now());

    // Reporting hooks. app.js draws; this module never touches the document.
    this.onState = deps.onState ?? (() => {});
    this.onError = deps.onError ?? (() => {});
    this.onDebug = deps.onDebug ?? (() => {});
    this.onSpeaking = deps.onSpeaking ?? (() => {});

    this.state = State.DISCONNECTED;
    this.sessionId = null;
    this.realtimeSessionId = null;
    this.muted = false;
    this.startedAt = null;

    this.peer = null;
    this.stream = null;
    this.track = null;
    this.channel = null;
    this.audio = null;

    this.busy = false;

    // Turn correlation. Every banking answer belongs to the question that
    // caused it, and these two are what keep that true — see `handleToolCalls`.
    this.turn = 0;
    this.answeredCalls = new Set();

    // Timers are injected so the quiet-line behaviour can be tested without
    // waiting twenty seconds for it.
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = deps.clearTimer ?? ((id) => clearTimeout(id));
    this.timing = { ...Timing, ...(deps.timing ?? {}) };

    // The quiet line, and how far along it we are.
    this.silenceTimer = null;
    this.maxCallTimer = null;
    this.awaitingSilenceReply = false;

    // Why the timer must not be running: the assistant is talking, a banking
    // tool is out, or a response is still being composed.
    this.assistantSpeaking = false;
    this.responseActive = false;
    this.toolsRunning = 0;

    // Set when the assistant has begun its closing line. The call ends when
    // that audio finishes, not the moment the words are chosen.
    this.endAfterSpeech = false;
    this.onEnded = deps.onEnded ?? (() => {});
  }

  // --- state ---------------------------------------------------------------

  setState(state) {
    this.state = state;
    this.onState(state);
  }

  /** Seconds since the call connected, for the on-screen timer. */
  durationSeconds() {
    if (this.startedAt === null) return 0;
    return Math.max(0, Math.floor((this.now() - this.startedAt) / 1000));
  }

  isActive() {
    return this.state === State.CONNECTED || this.state === State.MUTED;
  }

  // --- picking up ----------------------------------------------------------

  /**
   * Take the line: microphone, banking session, WebRTC, greeting.
   *
   * Returns true if the call is being set up. A second click while the first
   * is still connecting is ignored rather than queued — one page, one call.
   */
  async start() {
    // A failed attempt leaves the handset down, not broken: Disconnected and
    // Error are both states a caller can dial from. Connecting and Connected
    // are not, which is what stops a second line being opened.
    const idle = this.state === State.DISCONNECTED || this.state === State.ERROR;
    if (this.busy || !idle) return false;
    this.busy = true;
    this.setState(State.CONNECTING);

    try {
      // The microphone first: there is no point creating a banking session
      // for a caller who has no way to speak.
      try {
        this.stream = await this.getUserMedia({ audio: true });
      } catch (error) {
        return this.failed(Messages.MIC_DENIED);
      }
      this.track = this.stream.getTracks()[0];

      let opening;
      try {
        opening = await this.fetch(`${this.apiBase}/api/call/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
      } catch (error) {
        return this.failed(Messages.BACKEND_DOWN);
      }
      if (!opening.ok) return this.failed(Messages.REALTIME_FAILED);

      const call = await opening.json();
      this.sessionId = call.session_id;
      this.realtimeSessionId = call.realtime_session_id;

      await this.connect(call.client_secret.value);
      return true;
    } catch (error) {
      return this.failed(Messages.REALTIME_FAILED);
    } finally {
      this.busy = false;
    }
  }

  /** Negotiate the peer connection and open the event channel. */
  async connect(clientSecret) {
    const peer = this.createPeerConnection();
    this.peer = peer;

    // The assistant's voice. Autoplay is allowed because everything here
    // happens inside the click that started the call.
    this.audio = this.createAudioSink();
    peer.ontrack = (event) => {
      this.audio.srcObject = event.streams[0];
    };

    peer.addTrack(this.track, this.stream);

    const channel = peer.createDataChannel(EVENT_CHANNEL);
    this.channel = channel;
    channel.onmessage = (message) => this.receive(message);
    channel.onopen = () => this.opened();

    peer.onconnectionstatechange = () => {
      if (peer.connectionState === "failed") {
        this.failed(Messages.REALTIME_FAILED);
      }
    };

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);

    const answering = await this.fetch(CALL_URL, {
      method: "POST",
      body: offer.sdp,
      headers: {
        Authorization: `Bearer ${clientSecret}`,
        "Content-Type": "application/sdp",
      },
    });
    if (!answering.ok) return this.failed(Messages.REALTIME_FAILED);

    await peer.setRemoteDescription({
      type: "answer",
      sdp: await answering.text(),
    });
  }

  /**
   * The line is open.
   *
   * The bank speaks first, as a bank does, so the assistant is asked for a
   * response immediately instead of waiting for the caller to say hello.
   */
  opened() {
    this.startedAt = this.now();
    this.setState(State.CONNECTED);
    this.send({ type: "response.create" });
    this.maxCallTimer = this.setTimer(() => {
      // A call nobody ever ended. Close it rather than hold a line open all
      // afternoon — capacity is finite and the caller has long since gone.
      this.endAfterSpeech = false;
      this.end("MAX_DURATION");
    }, this.timing.MAX_CALL_MS);
  }

  // --- the quiet line ------------------------------------------------------

  /**
   * Start counting only if the bank is genuinely waiting for the caller.
   *
   * Every one of these guards has a failure it prevents: counting while the
   * assistant speaks interrupts it mid-sentence, counting while a tool is out
   * treats a slow database as an absent customer, and counting while a response
   * is being composed does both.
   */
  armSilence(delay = this.timing.SILENCE_CHECK_MS) {
    this.disarmSilence();
    if (!this.isActive()) return;
    if (this.assistantSpeaking || this.responseActive || this.toolsRunning > 0) {
      return;
    }
    this.silenceTimer = this.setTimer(() => this.silenceElapsed(), delay);
  }

  disarmSilence() {
    if (this.silenceTimer !== null) {
      this.clearTimer(this.silenceTimer);
      this.silenceTimer = null;
    }
  }

  /** Ask once, then close. */
  silenceElapsed() {
    this.silenceTimer = null;
    if (!this.isActive()) return;

    if (!this.awaitingSilenceReply) {
      this.awaitingSilenceReply = true;
      this.speakExactly(Speech.SILENCE_CHECK);
      return;
    }

    // Asked and still nothing. Say the closing line; the call ends when that
    // audio finishes, in `response.done`.
    this.endAfterSpeech = true;
    this.speakExactly(Speech.SILENCE_CLOSING);
  }

  /**
   * Make the assistant say one exact sentence and stop.
   *
   * The same mechanism the scope gate uses: the wording is the bank's, not the
   * model's, so a quiet line is always closed with the same words.
   */
  speakExactly(sentence) {
    this.responseActive = true;
    this.send({
      type: "response.create",
      response: {
        instructions: `Say exactly this sentence and nothing else, then stop: ${sentence}`,
      },
    });
  }

  /**
   * The caller is back. Cancel the countdown and carry on as normal.
   *
   * Called the moment they start speaking, so a caller who answers the check
   * question keeps their call — and one who instead just asks their banking
   * question gets it answered, with no extra prompt in the way.
   */
  callerActive() {
    this.awaitingSilenceReply = false;
    this.endAfterSpeech = false;
    this.disarmSilence();
  }

  // --- the conversation ----------------------------------------------------

  send(event) {
    if (this.channel && this.channel.readyState !== "closed") {
      this.channel.send(JSON.stringify(event));
    }
  }

  receive(message) {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (error) {
      return;
    }

    this.onDebug(event.type);

    switch (event.type) {
      case "input_audio_buffer.speech_started":
        // A new question begins here. Anything still in flight belongs to the
        // previous one and must not be allowed to answer this one.
        this.turn += 1;
        // Barge-in: stop whatever is being said so the stale answer does not
        // play over the new question. The server's own VAD also interrupts,
        // and asking twice is harmless.
        if (this.assistantSpeaking || this.responseActive) {
          this.send({ type: "response.cancel" });
          this.assistantSpeaking = false;
          this.responseActive = false;
          this.onSpeaking("assistant", false);
        }
        this.callerActive();
        return this.onSpeaking("caller", true);
      case "input_audio_buffer.speech_stopped":
        return this.onSpeaking("caller", false);
      case "response.created":
        this.responseActive = true;
        return this.disarmSilence();
      case "response.output_audio.delta":
      case "response.audio.delta":
        this.assistantSpeaking = true;
        this.disarmSilence();
        return this.onSpeaking("assistant", true);
      case "conversation.item.input_audio_transcription.completed":
        // What the caller actually said, as text. Used once, to ask the bank
        // whether this turn is in scope, and then dropped — on the
        // authentication turns it contains their spoken PIN.
        return this.enforceScope(event.transcript);
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done":
        // The bank has said goodbye, so this call is over once the words are
        // out. Ending here would cut the sentence off mid-air; the hang-up
        // happens when the response completes.
        if (CLOSING_LINE.test(event.transcript ?? "")) {
          this.endAfterSpeech = true;
        }
        // Only the assistant's own words are surfaced. The caller's are not,
        // which is how the spoken PIN stays out of the interface entirely.
        this.reportTranscript(event.transcript);
        return this.onDebug("said", event.transcript);
      case "response.done":
        this.assistantSpeaking = false;
        this.responseActive = false;
        this.onSpeaking("assistant", false);
        return this.responseFinished(event);
      default:
        return undefined;
    }
  }

  /**
   * Ask the bank whether this turn may be answered, and enforce the ruling.
   *
   * The model is perfectly capable of naming the capital of France, and a
   * prompt asking it not to is a request rather than a control. So the
   * decision is made in the backend, in Python, and when it comes back out of
   * scope the model's reply is cancelled and it is made to read the bank's
   * sentence instead. Whatever it was composing never reaches the caller.
   *
   * The transcript is passed through and kept nowhere: on the authentication
   * turns it contains the caller's spoken PIN.
   */
  async enforceScope(transcript) {
    if (!transcript || !this.sessionId) return;

    let decision;
    try {
      const response = await this.fetch(`${this.apiBase}/api/call/scope`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: this.sessionId, transcript }),
      });
      if (!response.ok) return;
      decision = await response.json();
    } catch (error) {
      // The gate is a control, not a crutch: if it cannot be reached, the
      // agent's own instructions still forbid answering out of scope.
      return;
    }

    this.onDebug("scope", decision.category);
    if (decision.allowed || !decision.speech) return;

    this.send({ type: "response.cancel" });
    this.send({
      type: "response.create",
      response: {
        instructions:
          "Say exactly this sentence and nothing else, then stop: " +
          decision.speech,
      },
    });
  }

  /**
   * Run any function calls the model made, then let it speak the answer.
   *
   * Three things keep an answer tied to the question that asked for it, and
   * without them the caller hears the previous turn's answer to their new
   * question:
   *
   * 1. A response that was cancelled — which is what barge-in does — is not an
   *    instruction to go and read someone's balance. Only a completed response
   *    may reach the bank.
   * 2. A `call_id` is answered once. A response redelivered for any reason
   *    must not run the same banking tool twice.
   * 3. If the caller has started speaking again while the tool was running,
   *    their new turn will drive its own response. Asking for one here would
   *    speak the old answer over the new question.
   */
  /**
   * The assistant has finished speaking. Hang up, run tools, or start waiting.
   *
   * The closing line is checked first and unconditionally: a call that has said
   * goodbye is over, whatever else the response contained.
   */
  async responseFinished(event) {
    if (this.endAfterSpeech) {
      // Why the call is finishing, so the operations record says something
      // more useful than "ended": the caller said goodbye, or nobody was there.
      const reason = this.awaitingSilenceReply
        ? "SILENCE_TIMEOUT"
        : "VOICE_END_CALL";
      this.endAfterSpeech = false;
      await this.end(reason);
      this.onEnded(reason);
      return;
    }

    await this.handleToolCalls(event);

    // Nothing else to do, so the bank is now waiting for the caller. This is
    // the only place the quiet timer starts.
    if (this.toolsRunning === 0 && !this.responseActive) {
      this.armSilence(
        this.awaitingSilenceReply
          ? this.timing.SILENCE_CLOSE_MS
          : this.timing.SILENCE_CHECK_MS
      );
    }
  }

  async handleToolCalls(event) {
    const response = event?.response;
    if (response?.status && response.status !== "completed") return;

    const calls = (response?.output ?? []).filter(
      (item) =>
        item.type === "function_call" && !this.answeredCalls.has(item.call_id)
    );
    if (calls.length === 0) return;

    const askedOn = this.turn;

    // A banking lookup is in progress, so the line is not idle even though
    // nobody is speaking. The quiet timer stays down until it comes back.
    this.toolsRunning += 1;
    this.disarmSilence();
    try {
      for (const call of calls) {
        this.answeredCalls.add(call.call_id);
        const result = await this.runTool(call);
        this.send({
          type: "conversation.item.create",
          item: {
            type: "function_call_output",
            call_id: call.call_id,
            output: JSON.stringify(result),
          },
        });
      }
    } finally {
      this.toolsRunning = Math.max(0, this.toolsRunning - 1);
    }

    if (this.turn === askedOn) {
      this.responseActive = true;
      this.send({ type: "response.create" });
    }
  }

  /**
   * Send the assistant's line to the operations record.
   *
   * Only the assistant's side: the caller's words already reach the backend
   * through the scope gate, where they are redacted. Failure is ignored on
   * purpose — a dashboard that cannot be written to must not disturb a call.
   */
  reportTranscript(text) {
    if (!text || !this.sessionId) return;
    this.fetch(`${this.apiBase}/api/call/transcript`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: this.sessionId, text }),
    }).catch(() => {});
  }

  /**
   * Ask the bank to run one tool.
   *
   * The session id comes from this call's own banking session, never from the
   * model's arguments — the model chooses which account type it wants, not
   * whose account it is.
   */
  async runTool(call) {
    let args = {};
    try {
      args = call.arguments ? JSON.parse(call.arguments) : {};
    } catch (error) {
      args = {};
    }

    try {
      const response = await this.fetch(`${this.apiBase}/api/call/tool`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: this.sessionId,
          name: call.name,
          arguments: args,
        }),
      });
      if (!response.ok) return TOOL_UNAVAILABLE;
      const body = await response.json();
      this.onDebug("tool", call.name);
      return body.result;
    } catch (error) {
      return TOOL_UNAVAILABLE;
    }
  }

  // --- covering the mouthpiece ---------------------------------------------

  /**
   * Mute stops the microphone being transmitted but leaves the call standing.
   * Disabling the track is what stops audio reaching OpenAI; the peer
   * connection, the banking session and the conversation all survive it.
   */
  setMuted(muted) {
    if (!this.isActive()) return false;
    this.muted = muted;
    if (this.track) this.track.enabled = !muted;
    this.setState(muted ? State.MUTED : State.CONNECTED);
    return true;
  }

  toggleMute() {
    return this.setMuted(!this.muted);
  }

  // --- putting down --------------------------------------------------------

  /**
   * Hang up and leave nothing running.
   *
   * Safe to call twice, and safe to call on a call that never connected: each
   * step tolerates having already happened, because the one thing worse than a
   * failed hang-up is a hang-up that throws half way through and abandons the
   * microphone.
   */
  async end(reason = "CUSTOMER_ENDED") {
    if (this.state === State.DISCONNECTED && !this.sessionId) return false;

    this.setState(State.ENDING);
    const sessionId = this.sessionId;

    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        try {
          track.stop();
        } catch (error) {
          /* already stopped */
        }
      }
    }
    try {
      this.channel?.close();
    } catch (error) {
      /* already closed */
    }
    try {
      this.peer?.close();
    } catch (error) {
      /* already closed */
    }
    if (this.audio) this.audio.srcObject = null;

    this.reset();

    if (sessionId) {
      try {
        await this.fetch(
          `${this.apiBase}/api/call/end?reason=${encodeURIComponent(reason)}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId }),
          }
        );
      } catch (error) {
        // The server reclaims abandoned calls on its own, so a failed
        // notification is not worth showing the customer an error for.
      }
    }

    return true;
  }

  /**
   * Best-effort hang-up for a tab that is closing.
   *
   * `fetch` is not guaranteed to survive an unloading page; a beacon is. The
   * server does not depend on this arriving — it sweeps abandoned calls — but
   * when it does arrive the session ends immediately instead of on a timeout.
   */
  hangUpBeacon(sendBeacon) {
    if (!this.sessionId || typeof sendBeacon !== "function") return false;
    const body = new Blob([JSON.stringify({ session_id: this.sessionId })], {
      type: "application/json",
    });
    return sendBeacon(`${this.apiBase}/api/call/end`, body);
  }

  /**
   * Stop every timer. Called on every path that ends a call, including the
   * failing ones — a timer that outlives its call would fire into a dead
   * session, or hang up a later one.
   */
  stopTimers() {
    this.disarmSilence();
    if (this.maxCallTimer !== null) {
      this.clearTimer(this.maxCallTimer);
      this.maxCallTimer = null;
    }
  }

  reset() {
    this.stopTimers();
    this.peer = null;
    this.stream = null;
    this.track = null;
    this.channel = null;
    this.sessionId = null;
    this.realtimeSessionId = null;
    this.muted = false;
    this.startedAt = null;
    this.turn = 0;
    this.answeredCalls.clear();
    this.awaitingSilenceReply = false;
    this.endAfterSpeech = false;
    this.assistantSpeaking = false;
    this.responseActive = false;
    this.toolsRunning = 0;
    this.setState(State.DISCONNECTED);
  }

  /** Report a failure in the customer's language and put the handset down. */
  failed(message) {
    this.stopTimers();
    this.onError(message);
    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        try {
          track.stop();
        } catch (error) {
          /* already stopped */
        }
      }
    }
    try {
      this.peer?.close();
    } catch (error) {
      /* nothing open */
    }
    this.peer = null;
    this.stream = null;
    this.track = null;
    this.channel = null;
    this.sessionId = null;
    this.startedAt = null;
    this.muted = false;
    this.setState(State.ERROR);
    return false;
  }
}

function defaultAudioSink() {
  const audio = document.createElement("audio");
  audio.autoplay = true;
  return audio;
}

/** mm:ss for the call timer. */
export function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}
