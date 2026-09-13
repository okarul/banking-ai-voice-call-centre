# Runbook — ABC Demo Bank voice call centre

Everything here is a synthetic demonstration: fictional customers, fictional
accounts, fictional PINs. No real banking data is involved at any point.

All commands run from `backend/` unless stated otherwise.

---

## 1. Start the services

**Database** (isolated container on port 5435 — nothing else on this machine
uses it):

```powershell
docker start banking-ai-postgres
```

**Backend** — port 8001. Not 8000: that port belongs to a different application
on this machine and will answer with something unrelated instead of failing.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001 --reload
```

Always launch through `.\.venv\Scripts\python.exe`. Starting it with the system
Python picks up a different interpreter and serves stale code that looks
mysteriously wrong.

**Frontend** — port 5173:

```powershell
cd ..\frontend
& "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend\.venv\Scripts\python.exe" serve.py
```

## 1a. Supported process topology — read before scaling or deploying

This backend runs as **one process with one worker**, and that is a correctness
requirement rather than a sizing preference.

```powershell
# Correct: one process, one worker.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001

# NOT supported. Do not do this.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001 --workers 4
```

Three pieces of state live in process memory and have no shared-state
equivalent:

| State | Where | Consequence of a second worker |
|---|---|---|
| capacity ceiling | `voice_call_manager` | each worker enforces its own ceiling, so the bank holds N times the configured number of provider sessions |
| live calls | `phone_call_registry` | a worker cannot see, drain or reclaim another's calls |
| call ownership | `app.process_ownership` | startup repair cannot tell another worker's live call from a crash orphan |

**Restarts and deploys must not overlap.** Stop the old process, let it drain,
then start the new one. `app.main.lifespan` repairs rows left open by a previous
generation during startup, *before* it begins serving — and under one
non-overlapping process everything still open at that moment genuinely does
belong to a generation that is gone.

Start a new process while the old one is still carrying calls and that
assumption is false. The new process will stamp the old one's live calls
`FORCED_CLEANUP`, which is an operations-board inaccuracy: it reads as "abandoned
by a crashed process" for a call that is going perfectly well. It is **not** a
resource leak — since Phase 7.4A.1 a process releases its own bridge, model
session and capacity slot from its own memory and no longer asks the database
for permission — but the recorded reason will be wrong.

### What is not solved

**True multi-process rolling-restart liveness remains out of scope and is not
solved. Supported topology is one backend process / one worker /
non-overlapping restart.**

Specifically:

* `PROCESS_OWNER_ID` and the pending-terminal set in `app.process_ownership` are
  **process-local memory**, not a distributed lease. They establish *same-process*
  ownership and nothing more.
* Nothing here leases, heartbeats, or takes an advisory lock, so no process can
  establish whether another process is alive. That was a deliberate decision, not
  an omission.
* Multiple workers, or overlapping old/new application processes, require a
  future ownership/lease architecture. Until that exists, the topology above is
  the supported one.

Nothing in the repository can enforce this: `--workers 4` is a command line, not
a code path. It is an operational requirement, and this is where it is written
down. See the module docstring of `backend/app/process_ownership.py` for the
reasoning, and `docs/TELEPHONY_MEDIA.md`, which already recorded that the
capacity ceiling is in-memory and single-process.

---

## 2. Health checks

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/api/call/active
```

Expect `{"status":"ok"}` and both active counts at `0` before a class starts.

## 3. Check the OpenAI account

Billing and rate limits are the two things that fail on demo day, and both fail
silently until a call is attempted.

- **Credit** — OpenAI dashboard → Billing. An exhausted balance still mints
  client secrets successfully, so a green `/api/call/start` proves nothing.
- **Limits and tier** — dashboard → Settings → Limits, for the organisation
  *and* the project the key belongs to.
- **Realtime model access** — confirm the tier can use `gpt-realtime-2.1`.

See `docs/REALTIME_CAPACITY.md` for what was measured and why it matters.

## 4. Single live smoke test — run this before every class

One paid session, about 30 seconds:

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.smoke
```

| Output | Meaning | Action |
|---|---|---|
| `PASS` | voice banking works end to end | ready |
| `PROVIDER_CAPACITY_UNAVAILABLE` | rate/concurrency limit, or no reply in time | wait, then re-run; check Limits |
| `QUOTA_UNAVAILABLE` | account is out of credit | add credit |
| `REALTIME_CONNECTION_FAILED` | key, model access, or network | check key and model access |
| `APPLICATION_FAILURE` | our code or the database | check backend logs and DB |

## 5. Concurrency smoke test — opt in only

Each run opens that many **paid** sessions. There is no default and no
automatic ladder.

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.capacity_smoke --concurrency 2
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.capacity_smoke --concurrency 3
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.capacity_smoke --concurrency 5
```

Above 5 it additionally requires `--i-accept-the-cost`.

Leave recovery time between runs. The creation-rate budget is cumulative and
takes tens of minutes to come back — see `docs/REALTIME_CAPACITY.md`.

## 6. Plan classroom concurrency

Free, local, no provider calls:

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.classroom
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.classroom --students 30 --interval 60 --duration 180
```

## 7. Capacity admission control

Optional. Off by default.

```
# backend/.env
REALTIME_MAX_ACTIVE_SESSIONS=0     # 0 = no limit (default)
```

Set it only to a capacity you have measured. A caller beyond the limit gets
HTTP 503 and:

> Voice banking is temporarily busy. Please try again shortly.

Their banking session is destroyed immediately, so nothing half-open is left
behind. The setting is read live — no restart needed.

## 8. Customer-facing failure messages

The customer never sees a provider name, a limit, an error code or a stack
trace.

| Situation | What the customer is told |
|---|---|
| At capacity | "Voice banking is temporarily busy. Please try again shortly." |
| Cannot connect | "Unable to connect to voice banking. Please try again." |
| Session gone | "This banking session is no longer active." |
| Tool failure | "I'm unable to retrieve that information right now." |

## 9. Cleanup verification

After a class:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/api/call/active
```

Both counts must be `0`. Abandoned browser calls are swept automatically after
30 minutes, and every new call start runs the sweep first.

## 10. Test suites

```powershell
# Deterministic backend — free
.\.venv\Scripts\python.exe -m pytest tests -q

# Frontend — free
cd ..\frontend; node --test "tests/**/*.test.js"

# Live realtime — PAID, and it consumes the creation-rate budget
$env:RUN_REALTIME_TESTS="1"; .\.venv\Scripts\python.exe -m pytest tests -m realtime -q

# Deterministic concurrency, up to 30 simultaneous callers — free
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.run_local 2 5 10 20 30 --burst
```

If the live suite fails, run `loadtest.smoke` first. A single passing call means
the code is fine and the account needs to rest.

---

## Classroom readiness checklist

Work down this list on the morning of the class.

- [ ] Database container running; `/health` returns ok
- [ ] Backend on **8001**, started from `.\.venv\Scripts\python.exe`
- [ ] Frontend on **5173**
- [ ] `/api/call/active` shows `0` and `0`
- [ ] OpenAI billing credit confirmed
- [ ] Correct organisation **and project** for the key in `backend/.env`
- [ ] Rate limits / usage tier reviewed against expected peak (see below)
- [ ] Realtime model access confirmed
- [ ] `python -m loadtest.smoke` → `PASS`
- [ ] `python -m loadtest.capacity_smoke --concurrency 3` → acceptable
- [ ] `python -m loadtest.classroom` peak matches the lesson plan
- [ ] `pytest tests -q` green
- [ ] `node --test` green
- [ ] Students briefed to start roughly one minute apart
- [ ] Recovery time left between the smoke tests and the class
