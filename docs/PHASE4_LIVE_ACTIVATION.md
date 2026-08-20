# Phase 4 — live activation

**Status: not live.** `TELEPHONY_ENABLED` is `false`, DIDWW is unchanged, no
telephone call has been placed, and no paid Realtime session has been opened.

Part A covers the reference media gateway. **Part B (below) covers FreeSWITCH
SIP/RTP termination** — configuration built and validated, FreeSWITCH itself not
installed and not run.

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

---

# Part B — FreeSWITCH SIP/RTP termination

**Status: configuration built and validated; FreeSWITCH not installed, not run.**
The media leg it forks into is complete and proven at one, two, three and five
concurrent calls over real loopback UDP with real RTP. What is not proven is
FreeSWITCH itself — the SIP handshake, SDP negotiation, and RTP from a carrier.

## B1. Architecture

```
SIP client ──SIP/RTP──► FreeSWITCH ──unicast UDP──► gateway ──WSS+token──► bank
   (local)              (container)   µ-law 20ms    (Windows host)
```

FreeSWITCH terminates the telephone network and nothing else:

| It does | It must never do |
|---|---|
| SIP signalling (INVITE, BYE) | customer authentication |
| RTP media termination | PIN validation |
| codec handling (PCMU) | banking logic or tool calls |
| forking audio to the gateway | database access |
| propagating call start/end | Realtime prompts or business rules |

It holds no banking state and needs no banking credentials. A test parses the
XML and asserts no banking term appears in any directive.

`unicast` is **`mod_dptools` — core FreeSWITCH**. No third-party module has to
be built in, which matters because `mod_audio_stream` is absent from community
builds and the official image now needs authentication.

## B2. Deployment method — decided, deferred

FreeSWITCH is **not installed on this machine**, and running it locally has
consequences this project would not choose silently:

| Option | Blocker |
|---|---|
| Official `signalwire/freeswitch` | **requires a SignalWire token** — packages moved behind auth |
| Community image (`safarov`, `dheaps`) | third-party binary; §3 says do not install untrusted binaries |
| Docker Desktop networking | Linux containers run in a VM; `--network host` never reaches Windows, so RTP needs explicitly published UDP ranges |
| WSL2 mirrored networking | cleanest RTP, but enabling it **restarts WSL and stops every other container on the machine** |

**Decision: deferred to Phase 4C**, when a real trunk is being configured
anyway. Nothing was installed and nothing on this machine was changed.

## B3. Setup, when the time comes

```powershell
# From the repo root. Publishes SIP and the two bounded UDP ranges.
docker run -d --name channel2-freeswitch `
  -p 127.0.0.1:5060:5060/udp -p 127.0.0.1:5060:5060/tcp `
  -p 127.0.0.1:16500-16530:16500-16530/udp `
  -v "${PWD}\deploy\freeswitch\conf\sip_profiles\channel2.xml:/etc/freeswitch/sip_profiles/channel2.xml:ro" `
  -v "${PWD}\deploy\freeswitch\conf\dialplan\banking.xml:/etc/freeswitch/dialplan/public/banking.xml:ro" `
  -e gateway_media_ip=host.docker.internal `
  <image>

# Confirm it is listening
docker exec channel2-freeswitch fs_cli -x "sofia status profile channel2"
```

`<image>` is deliberately unfilled — see B2. Bind to `127.0.0.1:` explicitly:
without it Docker publishes on `0.0.0.0` and an unauthenticated SIP port is
found by scanners within hours.

**Shutdown:** `docker stop channel2-freeswitch; docker rm channel2-freeswitch`.
Removing the container removes the whole SIP surface; the bank and the gateway
are unaffected.

## B4. Ports, codecs, and where audio goes

| | Value |
|---|---|
| SIP listen | `127.0.0.1` (host) / `0.0.0.0` (container) |
| SIP port | `5060/udp+tcp` |
| RTP range (FreeSWITCH ↔ SIP client) | `16500–16530/udp` — 31 ports |
| Media fork range (FreeSWITCH → gateway) | `16384–16407/udp` — 24 ports |
| Gateway media bind | `127.0.0.1` (`GATEWAY_MEDIA_BIND_HOST`) |
| Gateway → bank | `443/tcp`, HTTPS + WSS |
| Codec | **PCMU (G.711 µ-law), 8 kHz, mono, 20 ms = 160 bytes** |

Both ranges are small on purpose: an RTP range is a firewall rule, and every
port in it has to be open. 24 fork ports leaves room above the five-call target
without opening more than the service needs.

**One codec, no transcoding in FreeSWITCH.** `disable-transcoding=true` and
`absolute_codec_string=PCMU`, so the bank's single µ-law ↔ PCM16-24k conversion
stays the only one in the path. PCMA is a fallback offer only.

⚠️ **Confirm against the DIDWW trunk before live use.** If it offers G.729 or
Opus, FreeSWITCH would transcode and would need the relevant module built in.

## B5. Media token flow, unchanged

FreeSWITCH does **not** hold the media token and never sees it. The credential
belongs to the gateway↔bank leg:

```
gateway ──signed event──► bank          bank mints per-call token
gateway ◄──token────────  bank
gateway ──WSS + X-Telephony-Media-Token──► bank
FreeSWITCH ──unicast UDP──► gateway      (no token; loopback, per-call port)
```

Phase 4A's authentication is untouched: opaque, per-call, single-use,
expiring with the attach window, constant-time compared, header-borne, never
logged, and carrying no customer identity.

The UDP leg is protected differently, because it is a different threat: it is
loopback-bound, one port per call, and **the peer is fixed by the first
datagram**. A socket that re-pointed itself at the most recent sender would let
anyone who found the port redirect a live call's audio to themselves with a
single datagram. Asserted by test.

## B6. TLS

`GATEWAY_VERIFY_TLS` stays **`true`** — asserted by test. Phase 4B did not
weaken it and no localhost exception was added: the loopback tests reach the
bank over plain HTTP on a loopback port, which is a test fixture, not a
configuration change.

The SIP leg has no TLS while it is loopback-only. Before live use, prefer
SIP-TLS on `5061` with SRTP, or a private link to the carrier.

## B7. NAT and firewall — for later, not opened now

Nothing has been opened. For live DIDWW connectivity you would need:

| | Requirement |
|---|---|
| SIP | `5060/udp+tcp` (or `5061/tls`) inbound, **restricted to DIDWW signalling ranges** |
| RTP | `16500–16530/udp` inbound, restricted to DIDWW media ranges |
| Public address | static public IP or stable FQDN for the SIP host |
| NAT | if NATed, set `ext-sip-ip` / `ext-rtp-ip` to the public address and forward the RTP range — otherwise SDP advertises a private IP and you get **one-way or no audio**, the classic SIP-behind-NAT failure |
| Bank | `443/tcp` from the gateway **only**; never public |
| Media fork | `16384–16407/udp` **loopback only** — never opened externally |

## B8. Local loopback procedure

```powershell
# 1. bank + gateway configured as in Part A, then:
docker run ... channel2-freeswitch          # B3
.\.venv\Scripts\python.exe -m gateway check # expect READY

# 2. place one call to extension 1000 from any local SIP client
#    (linphone, zoiper, or `sipp -sn uac 127.0.0.1:5060 -s 1000`)
```

Expect: FreeSWITCH answers → `unicast` forks to `16384` → gateway announces →
bank accepts and mints a token → gateway attaches the media socket → **caller
hears the greeting** → audio flows both ways → hangup releases everything and
capacity returns to 0.

**Single call only.** The dialplan forks to a fixed port; two calls would mix
two callers into one stream. Concurrent calls need the `mod_event_socket` shim
(B9).

## B9. What is not built

The **ESL shim** that lets FreeSWITCH ask the gateway for a port per call.
`MediaPortAllocator` already hands out ports one call at a time; what is
missing is the `mod_event_socket` outbound server that answers FreeSWITCH.

It is not written because it is a protocol implementation that cannot be
validated without FreeSWITCH running — written blind it would look finished and
fail on first contact. The multi-call isolation it enables is already proven at
the UDP layer by `tests/test_gateway_udp_media.py`.

## B10. Future DIDWW routing values — placeholders, not set

```text
DID number ................ 6531252836
Destination type .......... SIP URI / voice-in trunk
Destination host .......... <public FQDN or IP of the SIP host>   # NOT SET
Destination port .......... 5060            (or 5061 for TLS)
Transport ................. UDP             (or TLS)
Codec preference .......... PCMU            (G.711 µ-law) — confirm on the trunk
Authentication ............ IP allow-list of DIDWW signalling ranges,
                            or SIP digest credentials              # NOT SET
CLI / caller-ID format .... optional
```

Field names come from the DIDWW console. **Changing the destination alters live
routing and may affect the existing account.** Nothing has been touched.

## B11. Troubleshooting

| Symptom | Cause |
|---|---|
| Call connects, silence both ways | SDP advertising a private IP — set `ext-sip-ip` / `ext-rtp-ip` |
| One-way audio | RTP range not forwarded, or NAT rewriting only one direction |
| `404 Not Found` | destination number not matched by the dialplan |
| Gateway sees no datagrams | `gateway_media_ip` not resolving to the Windows host, or fork port outside `GATEWAY_MEDIA_PORT_*` |
| Frames counted as dropped | framing or frame-size mismatch — the gateway drops anything that is not exactly 160 bytes rather than feeding it to the codec |
| Media socket closed 1008 | token missing, wrong, spent or expired — a gateway-to-bank problem, not a SIP one |
| Two callers hear each other | the fixed fork port with concurrent calls — B9 |

## B12. Rollback

| Step | Action |
|---|---|
| Remove the SIP surface | `docker stop channel2-freeswitch; docker rm channel2-freeswitch` |
| Disable telephony | `TELEPHONY_ENABLED=false` → **restart the bank** |
| Close the firewall | remove the SIP and RTP rules |
| **[APPROVAL]** revert routing | point the DIDWW DID back to its previous destination |
| Full revert | `git checkout` the Phase 4B commit |

Channel 1 is unaffected by every one of these. No rollback step touches the
persistent PIN lockout or the database schema.
