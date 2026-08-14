# Classroom runbook — ABC Demo Bank voice call centre

For the instructor running the demo. Everything is synthetic: fictional
customers, fictional accounts, fictional PINs. No real banking data exists in
this system.

**The one thing that decides whether the class works:** students start roughly
**one minute apart** and keep calls **short (2–3 minutes)**. That keeps about
two or three calls open at once, which is measured and working. Ten students
dialling simultaneously is not.

---

## 1. Pre-class checks

One command, about a second, free:

```powershell
cd "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend"
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.readiness
```

Add one real voice call (one paid session, ~30 s):

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.readiness --live
```

Expect `CLASSROOM READY`. Any failure prints a category:

| Category | Fix |
|---|---|
| `DATABASE_UNAVAILABLE` | `docker start banking-ai-postgres` |
| `DATABASE_NOT_SEEDED` | reseed — see §9 |
| `BACKEND_UNAVAILABLE` | start the backend — see §2 |
| `FRONTEND_UNAVAILABLE` | start the frontend — see §2 |
| `SESSIONS_NOT_CLEAN` | restart the backend to clear them |
| `REALTIME_NOT_CONFIGURED` | set `OPENAI_API_KEY` in `backend/.env` |
| `QUOTA_UNAVAILABLE` | add OpenAI credit |
| `PROVIDER_CAPACITY_UNAVAILABLE` | wait a few minutes, re-run |
| `APPLICATION_FAILURE` | check backend logs |

## 2. Startup commands

Database:

```powershell
docker start banking-ai-postgres
```

Backend — **always** the venv interpreter, **always** port 8001. Port 8000
belongs to a different application on this machine and will answer with
something unrelated instead of failing:

```powershell
cd "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend"
& "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Frontend:

```powershell
cd "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\frontend"
& "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend\.venv\Scripts\python.exe" serve.py
```

Student page: **http://127.0.0.1:5173**
Health: **http://127.0.0.1:8001/health**

If the backend was started with the system Python instead of the venv, it will
serve stale code and behave in ways that make no sense. Check with:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8001 |
  ForEach-Object { (Get-Process -Id $_.OwningProcess).Path }
```

## 3. Smoke test

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.smoke
```

`PASS` means a real call connected, authenticated, fetched the right customer's
balance, refused an out-of-scope question, and cleaned up.

## 4. Capacity check

Free — work out what the class actually needs:

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.classroom --students 30 --interval 60 --duration 180
```

| Students | Arrival | Call length | Peak simultaneous calls |
|---|---|---|---|
| 30 | 1/min | 90 s | 2 |
| 30 | 1/min | 2 min | 2 |
| 30 | 1/min | 3 min | 3 |
| 30 | 1/min | 5 min | 5 |

Paid, opt-in, only if you want to re-measure:

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m loadtest.capacity_smoke --concurrency 3
```

**Measured on this account, 13 August 2026:** 3 simultaneous calls answer
correctly in ~11 s each. 5 simultaneous stalls. Keep the peak at 3.

## 5. Student instructions

Display `docs/STUDENT_INSTRUCTIONS.md`. Distribute customer IDs and PINs
yourself — they are not in that file.

Tell the class:

- start **one at a time, about a minute apart** — the instructor calls each name
- keep calls **short**: verify, ask two or three questions, End Call
- if the page says voice banking is busy, **wait 30 seconds and press Start
  Call again**
- always press **End Call** when finished

## 6. Monitoring during class

### Agent operations dashboard

Open on the instructor's screen — **not** shown to students:

**http://127.0.0.1:5173/admin/agents.html**

One row per voice call: status, client ID, authentication, start and end time,
live duration, domain, last intent, tool count, token usage, estimated cost and
carbon, disconnect reason, and a **View History** link to that call's redacted
transcript. Live calls sort to the top and the duration ticks each second; the
data refreshes every four seconds without reloading the page.

The dashboard API is loopback-only. It shows no balances and no account
numbers — it is about how calls went, not about anyone's money — and the
customer page has no link into it.

Cost and carbon read **N/A** until configured in `backend/.env`. Carbon is an
estimate from an assumption you supply, never a provider measurement; leave it
off unless you can defend the coefficient to your class.

### Quick counts

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/api/call/active
```

Returns counts only — no names, no account numbers, no PINs:

```json
{"active_banking_sessions": 2, "active_browser_calls": 2}
```

Keep this at or below **3**. If it climbs, slow the arrival rate.

Per-call state, still safe (no PIN, no credential, no balances):

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/api/call/state/SESSION-xxxx
```

## 7. Common failures

| Symptom | Cause | Response |
|---|---|---|
| "Voice banking is temporarily busy" | at the configured limit | student waits 30 s and retries |
| Call connects, agent never answers | provider saturation | End Call, wait, retry; slow arrivals |
| Several students stall at once | too many simultaneous calls | pause new starts until the count drops |
| "Unable to connect to voice banking" | key, credit, or provider | run `loadtest.readiness --live` |
| Page loads but Start Call does nothing | backend down or CORS | check `/health`; frontend must be on 5173 |
| Agent asks which account | student holds two accounts | say "savings" or "current" |

## 8. Recovery procedures

**A. Backend unavailable** — restart it (§2). In-memory sessions are lost, which
is fine: students press Start Call again.

**B. Frontend unavailable** — restart `serve.py` (§2). No state lives there.

**C. Database unavailable** — `docker start banking-ai-postgres`, then re-run
readiness. Never touch any other container on this machine.

**D. Provider temporarily unavailable** — pause the class for a few minutes.
The allowance recovers on its own. Do not restart anything; nothing local is
broken.

**E. Quota exhausted** — add credit in the OpenAI dashboard. Nothing in the
application can work around an empty balance.

**F. Provider concurrency limit reached** — stop starting new calls until the
active count drops. Optionally set `REALTIME_MAX_ACTIVE_SESSIONS=3` in
`backend/.env` and restart, so extra callers are told the lines are busy instead
of sitting in silence.

**G. One student's call hangs** — they press End Call and start again. Nobody
else is affected; calls are fully isolated.

**H. Orphan session detected** — abandoned calls are swept after 30 minutes, and
every new call start runs the sweep. To clear immediately, restart the backend.

**I. Restart the whole demo** — restart backend and frontend (§2), confirm
`/api/call/active` reads `0` and `0`, re-run readiness.

## 9. Demo data

The synthetic seed should already be loaded — readiness reports
`5 demo customers`. Only if it is missing:

```powershell
cd "C:\Social Eagle AI Course\Call Centre Project\banking-ai-voice-call-centre\backend"
.\.venv\Scripts\python.exe -m app.database.seed
```

Every customer, account, loan and PIN in that file is invented.

## 10. End of class

1. Ask everyone to press **End Call**.
2. Confirm `/api/call/active` shows `0` and `0`. If not, restart the backend.
3. Stop the frontend and backend windows (Ctrl+C) if you are finished.
4. Leave PostgreSQL running, or `docker stop banking-ai-postgres`. Do not stop
   any other container.
5. Skim the backend window for errors. Secrets are redacted automatically, so
   the log is safe to read and to share.

## Security reminders

- Never put real customer data, real NRIC, or real bank details into this demo.
- Distribute demo PINs verbally or on paper — never in a shared document.
- The API key lives only in `backend/.env`. It is never sent to the browser,
  never returned by a route, and is redacted from logs.
- Students never see provider names, rate limits, quotas or error codes.
- `/dev/*` endpoints are development-only. Do not expose this server beyond
  127.0.0.1.
