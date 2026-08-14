# Telephony — security and privacy by design

**Status: Phase 1 (foundation only).** `TELEPHONY_ENABLED` is `false`, no SIP
code initialises, no provider is contacted, and no telephone call can be
placed or received. This document records the decisions the telephone channel
will be built to, so they are constraints on the work rather than a review of
it afterwards.

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
- Attempts are counted and the session locks; the lock is per session.
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
- `app.redaction` scrubs keys, bearer tokens, `api_key` fields and database
  passwords from every log record, and is installed on the root logger at
  startup.

## 8. Logging and audit events

`app.observability.events` defines the vocabulary: `CALL_RECEIVED`,
`CALL_STARTED`, `AUTH_STARTED`, `AUTH_SUCCEEDED`, `AUTH_FAILED`,
`INTENT_RECEIVED`, `TOOL_EXECUTED`, `CALL_END_REQUESTED`, `CALL_ENDED`,
`CALL_FAILED`.

An event says *what happened*, never *what was said*. Fields are an allow-list;
anything else is dropped silently rather than raising, because an observability
mistake must never fail a banking call. Categories, not messages — a free-text
message is where an exception string hides.

## 9. Capacity

One ceiling for the whole bank. `REALTIME_MAX_ACTIVE_SESSIONS` counts
established connections plus in-flight reservations, keyed by banking session
id with no notion of channel, so a telephone call will consume the same slot a
browser call would. There is deliberately no separate phone capacity setting,
and a test asserts none exists.

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

## 12. Failure and incident handling

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

## 13. Known limitations

- No human-agent handoff exists. The AI must never claim to transfer a caller.
- No fund transfer, payment, beneficiary, card or lending capability. The
  channel is read-only: authentication, accounts, loans.
- No financial advice. Factual synthetic account and loan information only.
- The telephone channel does not exist yet. Nothing in this document describes
  running code beyond the channel model, configuration and the documented
  boundary.
