# Phase 4 — live activation

**Status: not live.** `TELEPHONY_ENABLED` is `false`, DIDWW is unchanged, no
telephone call has been placed, and no paid Realtime session has been opened.

This document is the procedure for the first live call. Nothing in it has been
executed.

---

## 1. Architecture

```
caller ──PSTN──► DIDWW ──SIP/RTP──► [ SIP termination ] ──► [ gateway ] ──► [ bank ]
                                        NOT BUILT             built          built
                                                                │
                                                    HTTPS: signed call events
                                                    WSS:   µ-law audio + token
```

Three processes, two of which exist.

| Component | State | Where |
|---|---|---|
| Banking backend | complete | `backend/app` |
| **Reference media gateway** | **complete** | `backend/gateway` |
| SIP termination | **not built** | `gateway/sources.py::SipSource` |

### What the gateway does

`backend/gateway` implements the whole of the bank's Phase 3 media contract on
the far side:

1. Signs and posts the `incoming` event, waits for the answer.
2. Holds the short-lived credential it is issued.
3. Attaches the media socket with that credential.
4. Relays 20 ms µ-law frames both ways for the life of the call.
5. Reports the `ended` event, exactly once, however the call finished.

**The boundary is real.** Nothing in `backend/gateway` imports `app.*` — a test
asserts it. The gateway knows the bank as a URL, a shared secret and a wire
format, which is exactly what a FreeSWITCH deployment would know. In production
it runs as a separate process on the host exposed to the telephone network, so
the bank's database URL and OpenAI key are never in that address space.

The signing is a **second, independent implementation** of the scheme the bank
verifies. That is deliberate: a test that signs with the same function the
backend verifies with can pass while both are wrong together. A test asserts the
two produce identical bytes.

### What is not built, and why

`SipSource` is a named seam that raises `NotImplementedError`, not a stub
pretending to be a SIP stack. Terminating SIP means answering INVITE,
negotiating SDP, de-jittering RTP and handling BYE — writing that convincingly
without a provider to test against produces code that looks finished and fails
on the first real call.

Two ways to fill it:

| | Option A — media server in front | Option B — SIP stack in-process |
|---|---|---|
| What | FreeSWITCH `mod_audio_stream` / Asterisk AudioSocket | `pjsua2` inside the gateway |
| Gateway changes | a client of that server | a new transport layer |
| New dependency | none in Python | native SIP stack, built on Windows |
| Testable offline | the server is separately testable | very hard |

**Recommended: Option A.** Protocol detail stays outside the process that talks
to a bank, and the media server is separately testable.

## 2. Exact local run command

From `backend/`:

```powershell
# 1. the bank
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001

# 2. the gateway — configuration check, places no call
$env:GATEWAY_BACKEND_URL  = "http://127.0.0.1:8001"
$env:GATEWAY_WEBHOOK_SECRET = "<same value as the backend's TELEPHONY_WEBHOOK_SECRET>"
.\.venv\Scripts\python.exe -m gateway check

# 3. synthetic calls end to end — no telephone, no provider, no cost
.\.venv\Scripts\python.exe -m gateway selftest --calls 5 --seconds 2

# 4. the real thing — reports that SIP termination is missing
.\.venv\Scripts\python.exe -m gateway serve
```

`check` prints configuration (never the secret), confirms the bank answers, and
confirms `/api/telephony/incoming` is registered. `selftest` drives synthetic
calls through the entire path — signing, credential, media socket, relay,
teardown — and reports frames heard per call.

Both were run during Phase 4. `serve` exits 2 with an explanation, by design.

## 3. Ports and protocols

| Host | Port | Protocol | From |
|---|---|---|---|
| SIP termination | `5060/udp+tcp` or `5061/tls` | SIP | DIDWW signalling ranges only |
| SIP termination | `~10000–20000/udp` | RTP | DIDWW media ranges only |
| Gateway → bank | `443/tcp` | HTTPS + WSS | gateway only |
| Bank | `8001` | HTTP | **loopback only** |
| PostgreSQL | `5435` | TCP | **loopback only** |

The bank needs no inbound access from DIDWW, and should not be exposed to the
internet at all.

## 4. Environment variables

**Bank** (`backend/.env`) — you must add:

| Variable | Value | Note |
|---|---|---|
| `TELEPHONY_ENABLED` | `true` | **not yet** |
| `TELEPHONY_WEBHOOK_SECRET` | generated | see below |
| `REALTIME_MAX_ACTIVE_SESSIONS` | `1` first, `5` later | currently `3` |
| `OPENAI_API_KEY` | already set | needs Realtime quota |

**Gateway** (its own environment, its own host):

| Variable | Value |
|---|---|
| `GATEWAY_BACKEND_URL` | `https://<bank-host>` |
| `GATEWAY_WEBHOOK_SECRET` | **byte-identical** to `TELEPHONY_WEBHOOK_SECRET` |
| `GATEWAY_PROVIDER_NAME` | `DIDWW` (optional) |
| `GATEWAY_VERIFY_TLS` | `true` — only ever `false` against a local test bank |

Generate the shared secret:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add it to `backend/.env` and to the gateway's environment. Never commit either.
`.env.example` carries empty placeholders only.

**Leave at defaults:** `TELEPHONY_MEDIA_TRANSPORT`, all timeouts,
`TELEPHONY_AUDIO_QUEUE_FRAMES`, `TELEPHONY_SIGNATURE_TOLERANCE_SECONDS`,
PIN-lockout, retention, `DEMO_MODE`, `AI_DISCLOSURE_*`.

`SIP_PUBLIC_URI`, `SIP_PROVIDER_DOMAIN` and `DIDWW_DID_NUMBER` are read into
settings but **consumed by no code path** — `SIP_PUBLIC_URI` only flips the
dashboard's `phone_enabled` badge. They are documentation, not wiring.

## 5. Token and authentication flow

Two credentials, doing different jobs.

```
gateway                                   bank
   │                                        │
   │ POST /api/telephony/incoming           │
   │ X-Telephony-Timestamp: <unix>          │
   │ X-Telephony-Signature: v1=<hmac>       │  ① proves WHO is asking
   │───────────────────────────────────────►│
   │                                        │  verify → claim → capacity
   │                                        │  → mint per-call token
   │ 200 {"status":"accepted",              │
   │      "media_token":"<opaque>",         │  ② authorises ONE media socket
   │      "media_url":"/api/telephony/…"}   │
   │◄───────────────────────────────────────│
   │                                        │
   │ WSS  <media_url>                       │
   │ X-Telephony-Media-Token: <opaque>      │
   │───────────────────────────────────────►│  constant-time compare, spend
   │◄──────── µ-law frames both ways ──────►│
```

**① Event signature.** HMAC-SHA256 over `f"{timestamp}.{raw_body}"`, ±300 s,
body ≤ 64 KiB. The body is serialised once and both signed and sent.

**② Media token.** Opaque `secrets.token_urlsafe(32)`, minted per call.

- **Random, not signed.** The bank already holds per-call state, so a stateless
  token buys nothing and costs revocability. A random value carries no claims
  and cannot be forged from a leaked key.
- **Carries no identity.** No customer id, no authentication state, nothing
  derived from either. A media credential carrying identity would make
  attaching a socket a way to assert one. Asserted by test.
- **Single-use.** Spent on first use, so a replay cannot take over a live call.
- **Short-lived.** Expires with the attach window (`15 s` default), so a leaked
  token is useless seconds later.
- **Constant-time comparison** — a plain `==` leaks how much was right.
- **Header, not query string.** Query strings are written to proxy and server
  access logs as a matter of routine; a credential in an access log is a
  credential in a backup.
- **Issued only on acceptance.** A duplicate or a refusal has no call to attach
  to, so it is given no way to try.

A socket presenting no token, a wrong token, another call's token, or a spent
token is closed with **1008** before the handshake completes, and all four are
indistinguishable from outside.

This replaces the Phase 3 position, where knowing a `provider_call_id` was
enough. An identifier is not a credential, and "the network is private" is a
deployment assumption rather than a control — one misconfigured proxy from
being false.

## 6. HTTPS / TLS / public exposure

- **HTTPS and WSS are mandatory.** A real CA certificate (Let's Encrypt);
  self-signed is rejected by most gateways. The bank serves plain HTTP —
  terminate TLS at nginx/Caddy in front.
- `GATEWAY_VERIFY_TLS=true` in production, always. The gateway carries a
  credential that opens a live banking call.
- Expose **only** the SIP termination host to the telephone network. The bank
  should be reachable from the gateway alone — a private link or WireGuard hop
  is better than a public URL.
- Optionally SIP-TLS + SRTP on the DIDWW leg.

## 7. DIDWW changes — **required later, not made**

1. Voice-in trunk / SIP destination for DID `6531252836` → SIP termination host
2. Transport (UDP/TCP/TLS) matching that host
3. Codec preference including **PCMU (G.711 µ-law)**
4. IP authentication allow-list *or* SIP digest credentials
5. Optionally CLI/caller-ID format

Field names come from the DIDWW console. **Item 1 changes live routing and may
affect the existing account.** Nothing here has been touched.

**Audio format is not negotiable on our side:** G.711 µ-law, 8000 Hz, mono,
20 ms, **160 bytes**, binary WebSocket messages. If the trunk negotiates PCMA,
G.729 or Opus, the SIP termination layer must transcode — the bank will not.

## 8. One-call live activation procedure

Set `REALTIME_MAX_ACTIVE_SESSIONS=1`, **not 5**. Identical architecture, blast
radius of one caller and one paid session.

1. Confirm the OpenAI organisation's Realtime concurrency limit.
2. Start PostgreSQL, the bank, the TLS proxy, the SIP termination, the gateway.
3. `python -m gateway check` → expect **READY**.
4. `python -m gateway selftest --calls 1` → expect `completed=1`, frames heard.
5. **[APPROVAL]** Point the DIDWW trunk at the SIP termination host.
6. Dial the DID from **one** known handset.
7. Expect, in order:
   - gateway logs `gateway[...]` accepted; bank logs `CALL_ACCEPTED`
   - bank logs `telephony media attached`
   - **caller hears the greeting** within ~1–2 s of answer
   - one DB row: `channel=PHONE`, `status=ACTIVE`, `customer_id=NULL`
8. Say *"I'd like my balance"* → *"DEMO001"* → PIN `4821` → hear the balance.
   Row now shows `customer_id=DEMO001`, `auth_status=VERIFIED`.
9. Say *"goodbye"* and hang up.

**Never expect to see** a PIN, the API key, either secret, a caller number, a
balance or a stack trace in any log, on either host.

### Cleanup checks

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/api/call/active
```

- active calls → **0**
- one `COMPLETED` row with `ended_at` and `duration_seconds`
- no duplicate `provider_event_id`
- gateway `completed=1 refused=0 failed=0`
- no growing task or memory count on either process
- transcript, if any, shows `[PIN REDACTED]` and never digits
- a second call is admitted → the slot was genuinely returned

## 9. Rollback

| Symptom | Action |
|---|---|
| Anything wrong | `TELEPHONY_ENABLED=false` → **restart the bank**. Routes bind at build time. Channel 1 is unaffected throughout. |
| Caller hears silence | Attached but no audio — check PCMU and the 160-byte frame size |
| Call refused | Check `disconnect_reason`: `CAPACITY_REJECTED` = full · `UNAVAILABLE` = model session · `MEDIA_ATTACH_TIMEOUT` = gateway never attached |
| Socket refused (1008) | Token wrong, spent, expired, or for another call |
| One-way audio | SIP behind NAT — set `external_ip` and forward the RTP range |
| Full stop | **[APPROVAL]** revert DIDWW routing; `git checkout` the Phase 4 commit |

No rollback step touches the persistent PIN lockout or the database schema.

## 10. Cost-bearing steps

| Step | Cost |
|---|---|
| OpenAI Realtime session | **per minute, per concurrent call** |
| DIDWW inbound minutes | per minute |
| DID / trunk charges | per account terms |
| Gateway + SIP host | hosting |
| TLS-terminating host | hosting |
| Everything offline — tests, `check`, `selftest`, config, restarts | **free** |

## 11. Approval points

1. **Pointing the DIDWW trunk** — changes live routing, may affect the account
2. **The first real call** — paid on both DIDWW and OpenAI
3. **Any DID purchase, trunk, billing or credential change**
4. **Raising `REALTIME_MAX_ACTIVE_SESSIONS` above 1** — multiplies paid sessions
5. **Exposing the bank publicly** if the gateway-only path is skipped
6. **Choosing and deploying SIP termination** — a new component

## 12. Known limitations

- **SIP termination is not built.** The blocker between here and a live call.
- **No live validation.** Latency, jitter, packet loss, echo, DTMF and real
  barge-in timing are unmeasured, because measuring them needs a real call.
- **Provider concurrency (~3) is below the application target (5).** Calls 4 and
  5 may fail at OpenAI even though the bank admits them. The app degrades
  correctly — unwinds, releases the slot, records `UNAVAILABLE` — but the
  caller is dropped.
- **Capacity is single-process.** Idempotency is database-backed and survives
  multiple workers; the ceiling is in-memory and would need shared state.
- **The gateway holds no persistent state.** A gateway restart mid-call leaves
  the bank's side to the idle sweep (15 min default).
