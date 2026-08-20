# Telephony — security and privacy by design

**Status: Phase 2 (inbound event boundary).** `TELEPHONY_ENABLED` still
defaults to `false`. No SIP media code exists, no audio is carried, and no
telephone call can yet be *answered* — Phase 2 implements the control plane
only: a signed provider event can register that a call exists, take a capacity
slot, and open an unauthenticated banking session.

This document was written in Phase 1, before the code, so its decisions are
constraints on the work rather than a review of it afterwards. Sections now
record both the requirement and how it is met.

The browser (WebRTC) channel is the working, classroom-ready baseline and is
unchanged.

---

## 1. The rule everything else rests on

**A channel can never say who the customer is.**

A telephone call arrives with a caller number and a SIP `From` header. Both are
set by whoever places the call, and caller-ID spoofing is trivial. A bank that
reads an inbound number and concludes "this is DEMO001" has authenticated
nobody — it has merely been told something by an anonymous stranger.

So identity comes from where it comes from today: the deterministic customer-ID
and PIN check, writing `session.customer_id`, which is the only value the
authorization guards consult.

This is enforced structurally, not by memory:

- `CallerMetadata` has three fields — channel, provider call id, masked number
  — and no field for a customer id, authentication state, or account.
- `CallerMetadata.identifies_customer()` returns `False`, always.
- No `VoiceChannelAdapter` exposes a method that reaches a session, a customer
  or a banking tool.
- Tests assert all of the above, including that a provider call id shaped
  exactly like `DEMO001` still authenticates nobody.

## 2. Data minimisation

The channel keeps the least it can, not the most the provider offers.

| Provider gives us | We keep | Why |
|---|---|---|
| Full caller number (ANI) | **Nothing, by default** | No operational need in a demo |
| Caller display name | Nothing | Not needed, and attacker-controlled |
| SIP `From` / headers | Nothing | Not needed, and attacker-controlled |
| Provider call id | Yes | Correlating one call with a provider's records |
| Network/codec detail | Nothing | Not needed |

If a caller number ever becomes operationally necessary, `retain_masked_number`
stores only `+65 **** 2836`. The full number is masked at the boundary, in
`CallerMetadata.from_provider`, so it never reaches an attribute that could be
logged or persisted. Default is off.

## 3. Synthetic data only

`DEMO_MODE` defaults to `true`. Every customer, account, balance, loan and PIN
is invented Phase 2 seed data. There is no production banking connector, no KYC
integration and no payment capability, and the telephone channel introduces
none.

## 4. Authentication

Unchanged and non-negotiable:

- Deterministic. The model never decides whether authentication succeeded; it
  reports what the backend tool returned.
- Customer ID **and** PIN required. Neither alone is sufficient.
- Anti-enumeration: an unknown customer id and a wrong PIN are answered
  identically, in wording and in timing.
- Attempts are counted at **two** levels. Three wrong PINs lock the session,
  as before. Failures are *also* counted in the database against the claimed
  customer id, across calls, and enough of them lock that id for
  `PIN_LOCKOUT_MINUTES` — because a per-session lock alone is not a
  brute-force control when hanging up and redialling costs nothing, which on a
  telephone line is a script rather than a chore.
- The persistent count is kept for ids that match **no** customer too.
  Otherwise only real customers would ever lock, and watching which claims lock
  would separate real ids from invented ones — the enumeration channel the
  generic failure message exists to close.
- Locks expire. An unbounded lock on a shared demonstration customer would let
  one mistyped PIN deny DEMO001 to a whole classroom: a denial of service
  dressed as a security control.
- Voice is **not** a biometric. No speaker recognition, no voiceprint, no
  emotion, age, gender or accent inference — see `RESPONSIBLE_AI_TELEPHONY.md`.

## 5. Customer isolation

One call, one banking session, one customer. There is no global "current
customer" or "current call", and Phase 1 adds none. Every identifier stays
per-call:

```
provider_call_id -> agent_session_id -> banking_session_id -> realtime session
```

The Phase 11 scope gate still refuses cross-customer requests before any
protected tool runs, and the authorization guards still resolve ownership from
`session.customer_id` alone.

## 6. Transcript protection and PIN redaction

There is exactly **one** path into stored transcripts:
`recorder.record_message`, which redacts before it writes. Phase 1 deliberately
adds no second path, because a second path is precisely how a PIN would reach
the database unredacted. A test asserts `ConversationMessage(` is constructed in
exactly one place.

Redaction happens on the way in, not on the way out — a PIN written down and
filtered at display time has still been written down. Customer turns that could
be a PIN become `[PIN REDACTED]`; spoken customer ids become
`[Customer ID provided]`.

The PIN must never appear in logs, the dashboard, conversation history,
telemetry, exceptions, or any provider audit payload.

## 7. Secrets management

- SIP and DIDWW credentials are **not** attributes on `settings`. Only public
  values live there: the DID number (published to callers by definition), the
  SIP URI (a destination) and the provider name. A test asserts no
  `sip_password`, `didww_token` or similar attribute exists.
- `settings.public_settings()` is an **allow-list**. A denylist would start
  leaking the day somebody added a setting and forgot to update it.
- `.env` is git-ignored; `.env.example` carries placeholders only.
- `TELEPHONY_WEBHOOK_SECRET` is read from the environment, is absent from
  `public_settings()`, is never returned by any route, and is never written to
  a log. `.env.example` carries an empty placeholder and instructions for
  generating one.
- `app.redaction` scrubs keys, bearer tokens, `api_key` fields and database
  passwords from every log record, and is installed on the root logger at
  startup. The webhook secret is added to its **literal** rules: it is whatever
  the operator generated, so it has no recognisable shape and no pattern rule
  could catch it.

## 8. Logging and audit events

`app.observability.events` defines the vocabulary: `CALL_RECEIVED`,
`CALL_VALIDATED`, `CALL_ACCEPTED`, `CALL_REJECTED`, `CALL_STARTED`,
`AUTH_STARTED`, `AUTH_SUCCEEDED`, `AUTH_FAILED`, `INTENT_RECEIVED`,
`TOOL_EXECUTED`, `CALL_END_REQUESTED`, `CALL_ENDED`, `CALL_FAILED`.

The first four are the boundary of §12, kept as separate moments because on the
telephone they are genuinely different: an event arrived, it proved genuine and
well-formed, and only then was a call admitted or refused. Collapsing them
would make "we received a forged event and threw it away" indistinguishable
from "we answered the telephone".

The names carry no channel prefix. A telephone call being received is
`CALL_RECEIVED` with `channel="PHONE"` — the moment is the same moment, and the
channel is a field on it. Two parallel vocabularies would let the channels
drift, so that a control could be enforced on one and quietly not the other
without any name looking wrong. Filtering on `channel` gives the per-channel
view.

An event says *what happened*, never *what was said*. Fields are an allow-list;
anything else is dropped silently rather than raising, because an observability
mistake must never fail a banking call. Categories, not messages — a free-text
message is where an exception string hides.

## 9. Capacity

One ceiling for the whole bank. `REALTIME_MAX_ACTIVE_SESSIONS` counts
established connections plus in-flight reservations, keyed by banking session
id with no notion of channel, so a telephone call consumes the same slot a
browser call would. There is deliberately no separate phone capacity setting,
and a test asserts none exists.

A registered telephone call is a *registered call*, not a held reservation.
That distinction is cleanup: a reservation is invisible to `close_all()` and to
the idle sweep, so a call whose end event never arrived would hold a slot until
the process restarted.

The claim is taken **before** the slot, never after. Reserving first would mean
every duplicate retry briefly consumed capacity before discovering it was a
duplicate, so a provider retrying under load could turn away real callers with
capacity it was never entitled to.

**Application capacity and provider capacity are separate things.** The
configured ceiling is what this application will admit. The external provider
has its own limit — measured at roughly three concurrent sessions — which may
be lower, and which this setting neither knows nor controls.

## 10. Retention

`AUDIT_RETENTION_DAYS=30`, `TRANSCRIPT_RETENTION_DAYS=7`. Configuration only —
**Phase 1 implements no deletion job.** Recording the intent now makes it a
decision rather than an accident.

Production retention must follow organisational and legal policy, not these
demonstration defaults.

## 11. Error handling

A caller hears a short, generic sentence. Never a stack trace, an API error, a
provider name, a database name, an internal session id or a status code. The
existing customer-safe messages are the model:

> "Voice banking is temporarily busy. Please try again shortly."
> "I'm unable to retrieve that information right now."

## 12. The provider boundary and the inbound webhook

**Status: implemented in Phase 2** as `POST /api/telephony/incoming`. The
requirements below were written in Phase 1, before the endpoint existed, so
they constrained the implementation rather than describing it afterwards. What
follows now records both the requirement and how it is met.

The route is registered **only** when `TELEPHONY_ENABLED` is true *and*
`TELEPHONY_WEBHOOK_SECRET` is set — see `app.main.create_app`. With either
missing, the path does not exist, and a test asserts so. Not registering is a
stronger guarantee than registering something that refuses: a route that exists
can be mis-deployed or accidentally exempted by a later change, and the default
configuration is the one where it was never added.

Phase 2 is the control plane only. No audio is carried, no SIP media session is
opened, and no call is answered — a registered call holds a session and a
capacity slot, and waits for Phase 3.

The boundary itself is the important idea. A provider event is a *claim by an
outside party over the public internet*, not an instruction. Everything on the
far side of that line is untrusted: the payload, its headers, the caller
number, the display name, the provider's own identifiers. The handler's job is
to decide whether the claim is well-formed and genuine, and — if it is — to
hand a small, validated, internally-typed object to the application. It is not
to act on it.

**Transport and origin**

- HTTPS only. No plaintext listener, no downgrade, no "just for testing" HTTP
  variant — a webhook that has ever answered on HTTP is a webhook whose
  payloads have travelled in clear text.
- Verify the provider's signature where DIDWW supports one, or mutual TLS
  where it does not. Failing that, an allow-list of provider source addresses
  is a weaker fallback, not an equivalent: addresses are easier to spoof than
  signatures are to forge.
- Verification happens **before** parsing, before logging, and before any
  session, capacity reservation or database row is created. An unverified
  event has earned none of those.

**Shape of the request**

- Validate `Content-Type` explicitly. Reject anything unexpected rather than
  sniffing the body.
- Enforce a maximum body size at the boundary and reject oversized payloads
  early, with a small response and no unbounded read.
- Validate against a strict schema (Pydantic, as the existing routes use).
  **Reject rather than coerce** — a missing field is a malformed event, not a
  field to default. Unknown fields are dropped, never passed through.
- Treat every provider string as untrusted text. It is never interpolated into
  SQL, a shell command, a file path, or a model instruction.

**Replay and duplication**

- Replay protection where the provider supports it: a signed timestamp outside
  a short tolerance window is rejected.
- Duplicate-event protection regardless of provider support, because duplicates
  are ordinarily benign — a provider retrying after a slow response is normal
  behaviour, not an attack.
- Idempotency is keyed on `provider_event_id` (this exact notification) and
  `provider_call_id` (this call). Both are enforced by **partial unique
  indexes** (`WHERE ... IS NOT NULL`, so browser calls may all sit at NULL),
  created by the schema helper in `app.database.seed`. The claim *is* the
  insert: `if not exists: create()` would let two workers both find nothing and
  both proceed, so the database settles it and the loser takes an
  `IntegrityError` that becomes a deterministic duplicate response.
- Only a provider-id conflict counts as a duplicate. `agent_session_id` is
  derived from `max(id)` and can collide between simultaneous inserts; that is
  retried, because reporting it as a duplicate would strand a real second
  caller whose call was never registered.
- The rule that makes this matter: a retry must never produce a second capacity
  reservation, a second banking session, a second agent session, or a second
  acceptance. Acknowledging a duplicate is cheap; double-booking a slot that a
  student is waiting for is not.

**What the handler may not do**

- **No banking tool is invoked from a raw webhook.** The handler does not read
  an account, look up a customer, check a PIN, or call anything in `app.tools`.
  It validates an event and hands off. `app.telephony.service` imports no
  banking module, and the strict schema has no field that could name a
  customer — `extra="forbid"` means a payload carrying `customer_id` or
  `authenticated: true` is rejected outright rather than accepted and ignored. A webhook that could reach a banking
  tool would be a second route around the authorization guards, which is the
  one thing this architecture exists to prevent.
- No event authenticates anybody. The most a perfectly valid, correctly signed
  provider event can do is open an **anonymous** session, which must then pass
  the same deterministic PIN check as any browser call. Signature verification
  proves the event came from DIDWW; it says nothing whatever about who is
  holding the telephone.

**Logging at the boundary**

- Log by category, never by content: an event name, a decision, an
  `error_category`. Never the raw body, never headers, never the signature,
  never the caller's number.
- A rejected event is logged as a rejection with a reason category. The reason
  is not returned to the sender in detail — an error that explains precisely
  why a signature failed is a tool for the next attempt.
- Boundary failures must never surface to a caller. See §11.

## 13. Failure and incident handling

- **Provider outage** — no call arrives; the browser channel is unaffected.
- **Realtime outage** — the call cannot be answered; the caller is told the
  service is unavailable, and the session is cleaned up.
- **Database outage** — banking answers fail safe (refuse, never guess).
  Observability failures are swallowed and logged by category.
- **Malformed or oversized provider payload** — rejected at the boundary before
  any session is created.
- **Suspected credential exposure** — rotate the affected credential first,
  then investigate. Assume anything written to a log or a repository is
  disclosed.

## 14. Known limitations

- No human-agent handoff exists. The AI must never claim to transfer a caller.
- No fund transfer, payment, beneficiary, card or lending capability. The
  channel is read-only: authentication, accounts, loans.
- No financial advice. Factual synthetic account and loan information only.
- **No audio.** Phase 2 is the control plane only: a call can be registered,
  counted and closed, but not answered. There is no SIP media path, so a caller
  would hear nothing. Phase 3.
- **The signing scheme is an abstraction, not DIDWW compatibility.** No
  provider here has published a scheme this implements. `verify_provider_request`
  is the seam where a real one is added; until then the scheme is ours, and the
  deterministic credentials are for tests and local development.
- **No per-source rate limiting.** The persistent lockout closes the redial
  loop for a single claimed id. An attacker spreading guesses across many ids
  is slowed only by capacity.
- **Replay protection has no nonce store.** A signed request is refused once it
  is older than the tolerance, and a repeat inside that window is caught by
  idempotency — but an event whose row has aged out of retention could in
  principle be replayed later.
- **Single-process capacity.** Admission control is in-memory in one process.
  Idempotency is database-backed and survives multiple workers; the capacity
  ceiling would need shared state to do the same.
