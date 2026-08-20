# Telephony threat model

**Status: Phase 1.** The telephone channel is not implemented. This is the
model the implementation must satisfy, written before the code so it constrains
the design rather than describing it.

Scope: a SIP/DIDWW inbound voice channel reaching the existing synthetic
banking demo. The browser (WebRTC) channel is the working baseline; threats
that apply to both are noted as such.

**Residual risk** below means what remains *after* the stated control, assuming
the control is implemented as described. Where a control is not yet built, the
residual risk is stated for today.

---

## A. Provider and transport

### A1. Forged SIP or provider event
**Threat.** An attacker sends the backend a fabricated "incoming call" event to
open a banking session or drive the agent.
**Risk.** High — a public webhook is reachable by anyone who finds it.
**Control.** No public webhook exists in Phase 1. When one is added: verify the
provider's signature or mutual TLS, allow-list source addresses, reject
unauthenticated payloads before any session is created. A forged event still
authenticates nobody — it can at most open an anonymous session that must pass
the PIN check like any other.
**Residual.** Low. Worst case is capacity consumption, covered by D1.

### A2. Replayed event
**Threat.** A valid event is captured and resent to duplicate a call.
**Risk.** Medium.
**Control.** Idempotency on `provider_event_id` / `provider_call_id`: an event
already processed is acknowledged and ignored. Both columns exist on
`agent_sessions` from Phase 1 — `provider_call_id` names the call,
`provider_event_id` names the single notification about it, which is what
distinguishes a retry from a second call. The dedupe check that reads them is
Phase 2, when there is an endpoint to receive an event at all.
**Residual.** Low, once implemented. **Today: not implemented** — no endpoint
exists to replay against.

### A3. Duplicate incoming-call event
**Threat.** The provider legitimately retries, creating two banking sessions
for one call.
**Risk.** Medium — retries are normal, not adversarial.
**Control.** Same idempotency key as A2. One `provider_call_id` maps to at most
one `agent_session_id`.
**Residual.** Low. **Today: not implemented.**

### A4. Caller ID spoofing
**Threat.** The caller sets the `From` header or ANI to impersonate a customer.
**Risk.** **Critical if trusted, nil if not.** Spoofing is trivial.
**Control.** Channel metadata never determines identity. `CallerMetadata` has
no customer field, `identifies_customer()` returns `False`, and the caller
number is discarded by default. Only the deterministic PIN check writes
`session.customer_id`. Enforced by test.
**Residual.** **None for identity.** A spoofed number can still place a call,
which is what D1 addresses.

### A15. Malformed provider payload
**Threat.** A payload with missing fields, wrong types or injected content
crashes the handler or reaches an interpreter.
**Risk.** Medium.
**Control.** Strict schema validation at the boundary (Pydantic, as the
existing routes use). Reject rather than coerce. Provider strings are treated
as untrusted text — never interpolated into SQL, a shell, or a model
instruction.
**Residual.** Low.

### A16. Oversized payload
**Threat.** A very large body exhausts memory or blocks the loop.
**Risk.** Medium.
**Control.** Maximum body size at the boundary; reject early with a small
response. No unbounded reads.
**Residual.** Low.

---

## B. Identity and access

### B1. Authentication brute force
**Threat.** Repeated PIN guesses over a channel with no CAPTCHA.
**Risk.** High — a four-digit PIN is 10 000 possibilities, and telephony
automates cheaply.
**Control.** Attempts counted per session; the session locks after the
configured limit. Locking is per session, so it cannot be used to lock another
customer out. Phase 2 should add per-DID and per-source rate limiting, since a
new session per attempt resets the counter.
**Residual.** **Medium today.** Session-level locking does not stop an attacker
who opens a fresh session per guess; capacity control (D1) limits the rate but
was not designed as a brute-force control. This is the most significant open
item for the telephone channel and must be closed before any non-synthetic use.

### B2. Customer enumeration
**Threat.** Probing which customer ids exist.
**Risk.** High.
**Control.** Unknown id and wrong PIN are answered identically, in wording and
in timing (a decoy hash is verified so an unknown id does not answer faster).
The agent never confirms or denies existence. Covered by the existing
enumeration-resistance suite.
**Residual.** Low.

### B3. Session fixation
**Threat.** An attacker causes a victim's call to attach to a session the
attacker controls.
**Risk.** Medium.
**Control.** Session ids are server-generated UUIDs, never accepted from a
caller or a provider payload. A banking session is created by the backend at
call start; nothing external names it.
**Residual.** Low.

### B4. Session collision
**Threat.** Two calls share a session, mixing two customers.
**Risk.** Critical if it occurred.
**Control.** UUID session ids; uniqueness asserted under concurrency (20
simultaneous creations, 30-caller load test, zero collisions). Reservations and
connections are keyed by banking session id under a lock.
**Residual.** Very low.

### B5. Prompt injection
**Threat.** A caller instructs the model to ignore its rules, act as another
customer, or reveal internals.
**Risk.** High — it is the obvious attack on a voice agent.
**Control.** Authorization is not in the prompt. The Phase 11 scope gate
classifies each turn deterministically and refuses cross-customer and
out-of-scope turns *before* any protected tool runs. Tools resolve identity
from `session.customer_id`, and no tool schema accepts a customer id.
`submit_customer_id` is refused once verified.
**Residual.** Low for data access. A caller may still make the model *say*
something odd; it cannot make it *fetch* anything.

### B6. Cross-customer request
**Threat.** "Show me DEMO002's balance", or a comparison between customers.
**Risk.** High.
**Control.** Scope gate classifies as `CROSS_CUSTOMER_REQUEST` and refuses
before retrieval — including refusing to read the caller's *own* balance for
the purpose of comparing. Generic privacy refusal with no existence
confirmation.
**Residual.** Low.

---

## C. Data exposure

### C1. Transcript leakage
**Threat.** One call's conversation appears in another's, or reaches a
customer.
**Risk.** High.
**Control.** History is keyed on `agent_session_id`, never on customer, so two
calls by the same customer remain two conversations. The admin API is
loopback-only and separate from customer routes. Tested.
**Residual.** Low.

### C2. PIN leakage
**Threat.** A spoken PIN reaches a log, the dashboard, history or telemetry.
**Risk.** Critical.
**Control.** One transcript write path, redacting before storage. PIN-shaped
customer turns are replaced wholesale, not edited. Tool arguments are never
stored. Audit events use an allow-list that excludes every PIN field name.
Phase 1 adds no second path — asserted by test.
**Residual.** Low.

### C3. API-key leakage
**Threat.** The OpenAI key or a SIP credential reaches a log, a response or a
traceback.
**Risk.** Critical.
**Control.** `app.redaction` filter on the root logger scrubs key shapes,
bearer tokens, `api_key` fields and database passwords, plus the configured key
by literal value. Credentials are not attributes on `settings`.
`public_settings()` is an allow-list. The browser receives only a short-lived
ephemeral secret, never the permanent key.
**Residual.** Low.

### C4. Dashboard leakage
**Threat.** The operations dashboard exposes banking values or provider
internals.
**Risk.** Medium.
**Control.** The table carries no balances and no account numbers — figures
appear only inside one call's transcript, where an operator has deliberately
navigated. Loopback-only. Provider internals are not returned. Tested.
**Residual.** Low. The dashboard is unauthenticated beyond loopback, which is
acceptable for a single-machine classroom demo and **not** acceptable beyond
it.

---

## D. Availability

### D1. Capacity exhaustion
**Threat.** Calls consume every available slot, denying service to students.
**Risk.** Medium.
**Control.** One ceiling across both channels: `REALTIME_MAX_ACTIVE_SESSIONS`
counts connections plus in-flight reservations, atomically, with no
per-channel pool. A caller beyond it is told the lines are busy and no session
is left half-open.
**Residual.** Medium — the measured provider ceiling (~3 concurrent) is lower
than the configured application ceiling may be, so the provider is the binding
constraint.

### D2. Denial of service
**Threat.** Flooding the webhook or the DID.
**Risk.** Medium.
**Control.** Rate limiting at the boundary, payload size limits, admission
control, and provider-side controls at the DID. Observability failures never
block a call.
**Residual.** Medium. A demo on one laptop has no upstream protection; do not
publish the webhook beyond what is needed.

### D3. Provider outage
**Threat.** DIDWW is unavailable.
**Risk.** Low impact.
**Control.** No call arrives. The browser channel is independent and unaffected.
**Residual.** Low.

### D4. Realtime (model) outage
**Threat.** OpenAI Realtime is unavailable or rate-limited.
**Risk.** Medium — observed in practice during load testing.
**Control.** Provider errors are categorised (`RATE_LIMIT`, `QUOTA_EXHAUSTED`,
`CONCURRENCY_LIMIT`, …), the caller hears a generic busy message, capacity is
released, and the session is cleaned up.
**Residual.** Medium — outside our control; mitigated by admission control and
by pre-class smoke tests.

### D5. Database outage
**Threat.** PostgreSQL is unavailable.
**Risk.** Medium.
**Control.** Banking answers fail closed — a tool that cannot read returns a
failure and the agent says it cannot retrieve the information. It never
invents a figure. Observability failures are swallowed and logged by category
so the dashboard cannot take down a call.
**Residual.** Low for correctness; the demo is unusable while the database is
down, which is acceptable.

### D6. Orphan session
**Threat.** A call ends without the record closing, holding capacity and
showing a phantom active agent.
**Risk.** Medium.
**Control.** Every termination path writes `ended_at` and a disconnect reason.
Abandoned browser calls are swept after 30 minutes and on every new call start.
Sessions left open by a restart are reconciled to `FORCED_CLEANUP` at startup.
The telephone channel must hook the same lifecycle, including provider
`hangup`/`failed` events.
**Residual.** Low for the browser; **to be implemented** for the telephone.

---

## Open items before any non-synthetic use

1. **B1 brute force** — per-DID and per-source rate limiting, not just
   per-session locking.
2. **A1/A2/A3** — webhook authentication and idempotency, before any public
   endpoint exists.
3. **C4** — real operator authentication if the dashboard ever leaves loopback.
4. **D6** — telephone hangup and failure events wired to the same cleanup.
