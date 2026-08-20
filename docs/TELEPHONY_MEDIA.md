# Telephony media — how a call's audio actually moves

**Status: Phase 3.** The media path is implemented and tested offline against
five concurrent calls. **No live telephone call has been placed or answered**,
and one external decision remains open — see *What is not built* below.

---

## 1. The path

```
caller's telephone
      │  PSTN
      ▼
DIDWW  ──SIP/RTP──►  media gateway  ──WebSocket──►  this application
                                                        │
                                                   PhoneCallBridge
                                                   ├─ µ-law → PCM16 24k ─► model session
                                                   └─ PCM16 24k → µ-law ◄─ model session
                                                        │
                                        existing agent, tools, auth, guards
                                                        │
                                                synthetic banking database
```

The application's half of that is complete. The gateway is not.

## 2. Audio formats, and why they need converting

| | Telephone side | Model side |
|---|---|---|
| Codec | G.711 µ-law (PCMU) | PCM16 little-endian |
| Sample rate | 8 000 Hz | 24 000 Hz |
| Channels | mono | mono |
| Frame | 20 ms = 160 bytes | 20 ms = 960 bytes |

Neither half is a choice. µ-law at 8 kHz is what the public telephone network
carries and what a SIP trunk offers by default — the caller's audio has already
been through it before it arrives. PCM16 is what
`app.realtime.banking_realtime.model_settings` asks for, and changing that would
mean the two channels reaching the same agent with different audio settings.

So `app.telephony.audio` converts, in both directions, on every frame:

```
caller  →  µ-law decode  →  PCM16 @ 8k  →  upsample ×3   →  PCM16 @ 24k
model   →  PCM16 @ 24k   →  downsample ÷3 →  PCM16 @ 8k  →  µ-law encode
```

Two details are load-bearing and easy to get wrong:

- **The encoder is checked against the reference, not against the decoder.** A
  codec that decodes its own output perfectly and disagrees with everyone else
  produces a call that connects, reports no error and sounds like static. The
  tests assert byte-for-byte equality with `audioop`'s G.711.
- **Downsampling averages rather than decimating.** Taking every third sample
  folds everything above 4 kHz back into the audible band as aliasing, which
  turns the model's `s` and `f` sounds into tones.

OpenAI Realtime *can* accept `g711_ulaw` directly, which would remove this
module entirely. It is not used because the browser channel runs on `pcm16` and
both channels must reach the same agent with the same settings; a per-channel
audio format is a second configuration to keep in step.

## 3. One call, one of everything

Per call, and shared with nothing:

| | |
|---|---|
| `provider_call_id` | the provider's name for the call |
| `banking_session_id` | the session every existing service already understands |
| `PhoneCallBridge` | the audio path |
| media transport | one socket, or one loopback |
| inbound queue | caller → model |
| outbound queue | model → caller |
| model session | one Realtime conversation |
| two pump tasks | one per direction |

There is no module-level per-call state. `PhoneCallRegistry` holds the bridges,
but it is a keyed container: a lookup returns one call's bridge and offers no
route to another. Assistant audio reaches a caller through a handler bound to
that caller's bridge — a closure that has no name for any other queue.

## 4. Capacity

**One ceiling for the whole bank**, `REALTIME_MAX_ACTIVE_SESSIONS`, counted
across both channels by the single `voice_call_manager`. A telephone call
consumes the same slot a browser call would, and there is deliberately no
separate phone limit.

```
Application concurrent-call target:  5
Provider practical limit (measured): ~3 concurrent
```

These are **separate constraints and must stay separate.** The application is
built and tested for five; the provider's limit belongs in the provider's own
capacity planning. Lowering the application to 3 to match would bake an
external, changeable constraint into the architecture.

Admission is atomic — check and claim happen in one critical section — so six
simultaneous starts admit exactly five. That is asserted by test, as is ten.

## 5. Cleanup

Every ending converges on `service.tear_down`, which is idempotent:

- caller hangs up (socket closes)
- provider sends an `ended` event
- model session fails
- media or model setup times out
- an unexpected exception

It removes the bridge, cancels both pumps, releases the transport, closes the
model session, returns the capacity slot **exactly once**, and destroys the
banking session.

What it deliberately does **not** touch is the persistent PIN lockout. That is
customer security state and outlives the call by design — clearing it on
hang-up would make hanging up the way to reset it, which is the exact loop the
lockout exists to close.

## 6. Bounded audio

Every queue has a ceiling (`TELEPHONY_AUDIO_QUEUE_FRAMES`, default 200 frames ≈
4 seconds) and drops the **oldest** frame when full.

Oldest, not newest, is the right way round for live audio: dropping the newest
preserves stale speech and grows the delay for ever, while dropping the oldest
loses a moment and keeps the conversation in the present. Drops are counted so
a degraded call is visible to an operator.

This is what stops one caller — or one misbehaving gateway — consuming
unbounded memory. A test floods one call with 5 000 frames and asserts both the
ceiling and that the neighbouring call is untouched.

## 7. What is NOT built

**SIP and RTP termination.** DIDWW is a SIP trunking provider: it delivers
calls as SIP signalling with RTP media. It does not offer a hosted WebSocket
media stream. So something must terminate SIP and hand this application audio,
and there are two ways to do it:

| | Option A — media gateway | Option B — SIP stack in-process |
|---|---|---|
| What | FreeSWITCH / Asterisk in front, streaming to our WebSocket | `pjsua2` or similar inside the backend |
| App changes | none — the WebSocket transport is already built | substantial new transport layer |
| New dependency | none in Python | a native SIP/RTP stack, built on Windows |
| Testability | the gateway is separately testable | very hard to test offline |
| Operational cost | one more service to run | none |

**Recommendation: Option A.** The application side already exists, it needs no
new Python dependency, and it keeps provider protocol detail outside the
banking process entirely.

**This decision has not been made, and nothing external has been changed.**

## 8. What a live call would require — awaiting approval

None of the following has been done. Each is an external change:

1. **A media gateway** deployed and reachable, converting DIDWW SIP/RTP to a
   WebSocket carrying 20 ms µ-law frames to
   `wss://<host>/api/telephony/media/{provider_call_id}`.
2. **DIDWW trunk configuration** pointing the DID at that gateway. *This
   changes provider routing and may affect the existing account.*
3. **A public HTTPS endpoint** for the Phase 2 webhook, with
   `TELEPHONY_WEBHOOK_SECRET` set at both ends.
4. **`TELEPHONY_ENABLED=true`** and `REALTIME_MAX_ACTIVE_SESSIONS=5`. Enabling
   telephony requires a **restart** — the route is registered at application
   build time, so that turning it on is a deliberate act.
5. **A funded OpenAI Realtime quota.** Five concurrent calls means five
   concurrent paid sessions; the measured provider ceiling of ~3 applies here.

Cost applies to items 1, 2 and 5.

## 9. Known gaps

- **The media socket is not individually authenticated.** It is protected by
  needing a `provider_call_id` that a signature-verified webhook already
  registered, which is weaker than the webhook's HMAC. Before this faces
  anything but a gateway on a trusted network, it should carry a per-call token
  issued at registration.
- **Capacity is single-process.** Idempotency is database-backed and survives
  multiple workers; the ceiling is in-memory and would need shared state to do
  the same.
- **No live validation.** Everything here is proven offline. Latency, jitter,
  packet loss, echo, DTMF and real barge-in timing are all unmeasured, because
  measuring them requires a real call.
