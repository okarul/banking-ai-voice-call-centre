# Realtime voice capacity — measured limits and what they mean

## What was measured

| | |
|---|---|
| **Measured on** | 13 August 2026 |
| **Account** | the OpenAI organisation/project whose key is in `backend/.env` (identified there, never here) |
| **Model** | `gpt-realtime-2.1`, server-side WebSocket transport |
| **Method** | `python -m loadtest.capacity_smoke --concurrency N` |

No account identifier, key, or project id is recorded in this file. Read the key
from `backend/.env`; it is git-ignored and must never be pasted into docs.

## The exact provider condition

When too many Realtime sessions are opened in quick succession, the provider
closes the WebSocket during the handshake:

```
close code : 1013  ("try again later")
reason     : requests.rate_limit_exceeded
retry-after: not supplied
category   : RATE_LIMIT
```

This is a **request rate limit on session creation**. It is specifically *not*:

- **not a quota/billing problem** — REST `POST /v1/chat/completions` returned
  HTTP 200 with ~9 999 requests remaining, and `/v1/realtime/client_secrets`
  minted normally, at the same moment Realtime sessions were being refused
- **not a hard cap on concurrent sessions** — the refusal happens at connect
  time based on how fast sessions are being *started*
- **not an application defect** — Phase 12 tested 30 concurrent local sessions
  with zero leakage, zero collisions, 100 % correctness and 100 % cleanup

## Observed behaviour

| Concurrency (≈1 s apart) | Fully served | Notes |
|---|---|---|
| 1 | 1/1 | connect ~10.8 s, first audio ~0.8 s |
| 2 | 2/2 | connect 7.0 s / 1.6 s |
| 3 | 1/3 | all three connected; two never completed the turn within 60 s |
| 4 | 0/4 | three connected then stalled; one refused with 1013 |
| 5 | 0/5 | one connected; **four refused with 1013** |

Two distinct failure modes, and they are worth telling apart:

1. **Refused at connect** — close 1013, immediate, unambiguous.
2. **Connected then silent** — the session opens, first audio arrives in under a
   second, our banking tool returns in milliseconds, and then the provider never
   generates the post-tool response. This is the harder one, because from the
   customer's chair it is indistinguishable from a broken bank.

The second mode is why `REALTIME_MAX_ACTIVE_SESSIONS` exists: a caller told
"temporarily busy" can try again, a caller sitting in silence cannot.

## The budget is cumulative, and recovery takes tens of minutes

Sustained testing exhausts the allowance and the degradation **persists after
the load stops**. During Phase 12 the full Realtime regression suite passed
37/37 from a rested account, then scored 6/37 immediately after a 26-session
load test and 8/37 after a 19-minute pause — with unchanged application code and
a healthy REST API throughout.

Practical consequences:

- Do not run the live suite, the capacity smoke test and a class in the same
  hour without leaving recovery time.
- A failing live suite is not automatically a code regression. Check
  `python -m loadtest.smoke` first: if a *single* call passes, the code is fine
  and the account needs to rest.

## What this means for a classroom

Thirty students is not thirty simultaneous calls. Run
`python -m loadtest.classroom`:

| Students | Arrival | Call length | Peak simultaneous calls |
|---|---|---|---|
| 30 | 1/min | 2 min | **2** |
| 30 | 1/min | 3 min | **3** |
| 30 | 1/min | 5 min | **5** |
| 20 | 1/min | 3 min | **3** |
| 30 | all at once | 3 min | **30** |

Staggered arrivals are the whole game. At one student per minute the *creation
rate* is also one per minute, which is far below the limit that was tripped by
starting sessions back to back.

**Recommended minimum capacity: 10 concurrent sessions** — the worst staggered
peak (5, for five-minute calls) with 2× headroom. This is classroom-demo
planning, not a production SLA.

## Before a class, check your account limits

The limit above belongs to *this* account on *this* date. It is not a universal
OpenAI limit, and it changes with usage tier. Before classroom deployment:

1. Open the OpenAI dashboard → **Settings → Limits** for the organisation *and*
   the project the key belongs to.
2. Check the rate limits that apply to the Realtime model, and the usage tier.
3. If the tier is low, add credit or request an increase — tier advances with
   cumulative spend, so this can take time and should not be left to demo day.
4. Re-measure with `python -m loadtest.capacity_smoke --concurrency 5`.

## Configuration

```
REALTIME_MAX_ACTIVE_SESSIONS=0     # default: no limit
```

`0` means the application admits every call, which is correct once account
capacity comfortably exceeds the class. Set it to a number **you have
measured** — not to the figure in this document — if you would rather turn
callers away cleanly than let them stall. It is read live, so it takes effect
on the next call without a restart.

Do not treat any number here as a product limit. It is one measurement of one
account on one day.
