# Channel 2 — live acceptance matrix

Twenty cases to run against the live deployment before Channel 2 is declared
production-ready. **Not yet run.** Each one names what must already be true,
what to do, what should happen, the single criterion that decides pass or fail,
and where to look to confirm it.

Every case is billable — each accepted call opens a paid realtime session.
Run them deliberately, in order, and stop at the first failure rather than
working through the list.

## Before starting

| Requirement | Check |
|---|---|
| OpenAI account in credit | `GET /readiness` → `checks.realtime.ready` is `true`, **and** a first test call connects. Readiness reports configuration, not balance — an empty account looks ready right up until a call is attempted. |
| Backend and gateway from the same release | `GET /readiness` → `checks.telephony.media_protocol_version` matches the gateway's `PROTOCOL_VERSION`. A mismatch refuses calls with `PROTOCOL_MISMATCH`. |
| Capacity | `REALTIME_MAX_ACTIVE_SESSIONS=1` for cases 1–20. Phase 7 raises it separately. |
| Log access | Backend application log, gateway log, and `agent_sessions` rows by `provider_call_id`. |

Throughout: **no PIN, NRIC, balance, transaction detail, transcript or key may
appear in any log**. Treat a sighting as an immediate stop, whatever else passed.

---

## 1. Thirty-second normal call

**Precondition** Service ready, no call in progress.
**Action** Call the DIDWW number. Wait for the greeting. Ask one balance question after authenticating. Say "goodbye".
**Expected** Greeting heard once; answer heard in full; line drops on its own within ~2s of the closing line finishing.
**Pass** `disconnect_reason = CALLER_GOODBYE`, and the caller never pressed end.
**Verify** Backend: `playback boundary ... issued` then `playback acknowledged`. Gateway: `ended call ... (application finished)`. Database: one row, `status COMPLETED`.

## 2. Three-minute multi-turn call

**Precondition** As 1.
**Action** Authenticate, then ask six or more banking questions across ~3 minutes.
**Expected** Every question answered; context retained; no re-authentication; no drop.
**Pass** Call survives to an explicit goodbye, `CALLER_GOODBYE`, no `IDLE_TIMEOUT` and no `CALLER_SILENT`.
**Verify** One `authentication` transition only. One greeting.

## 3. Ten-minute multi-turn call

**Precondition** As 1.
**Action** Keep a natural conversation going past ten minutes, with pauses under 10s.
**Expected** No disconnect at any duration.
**Pass** Call ends only when the caller says goodbye. **Any duration-based drop is a failure**, and the first thing to check is that no new timeout was introduced.
**Verify** No `IDLE_TIMEOUT`. Elapsed time in the row exceeds ten minutes.

## 4. Thirty-second agent response

**Precondition** As 1.
**Action** Ask something that produces a long answer (e.g. a full transaction list).
**Expected** The whole answer is heard. The line does not drop mid-sentence.
**Pass** No disconnect during playback; `playback acknowledged` appears **after** the caller stops hearing audio, not before.
**Verify** This is the Phase 6.7 regression. A drop here means playback completion has gone back to meaning "the backend's queue emptied".

## 5. Silence under ten seconds

**Precondition** Mid-call, after an answer has finished playing.
**Action** Say nothing for ~7 seconds, then ask another question.
**Expected** No closing line. The call continues normally.
**Pass** Call survives; the next answer arrives.

## 6. Silence over ten seconds

**Precondition** Mid-call, after an answer has finished playing.
**Action** Say nothing.
**Expected** After ~10s: "I do not hear anything from you. Thank you." Then the line drops once that line has played in full.
**Pass** `disconnect_reason = CALLER_SILENT`. The closing line is **not** truncated.

## 7. Thank you does not end the call

**Precondition** Mid-call, authenticated.
**Action** Say "thank you very much".
**Expected** "You're most welcome. Is there anything else I can help you with today?" and the line stays open.
**Pass** No disconnect. Ask a further question and get an answer.

## 8. Explicit goodbye

**Precondition** Mid-call.
**Action** Say "that's all, thank you, goodbye".
**Expected** Closing response, played in full, then the line drops on its own.
**Pass** `CALLER_GOODBYE`; caller did not press end; gateway shows `(application finished)` and a SIP BYE.

## 9. Manual hangup

**Precondition** Mid-call, mid-answer.
**Action** Hang up the handset while the agent is speaking.
**Expected** Immediate teardown.
**Pass** `disconnect_reason = CUSTOMER_ENDED`; capacity back to 0 within seconds; no orphaned session.

## 10. Barge-in

**Precondition** Mid-call, agent speaking a long answer.
**Action** Interrupt with a new question.
**Expected** Agent stops promptly and answers the new question. No stale audio from the interrupted answer.
**Pass** No duplicate or resumed answer; no false silence close afterwards.

## 11. Wrong PIN

**Precondition** Identified, not yet authenticated.
**Action** Give an incorrect PIN once.
**Expected** Polite retry prompt. No banking data offered.
**Pass** No tool call succeeds; call continues.

## 12. Five-attempt lockout

**Precondition** As 11.
**Action** Give incorrect PINs up to the configured limit.
**Expected** Lockout message; no banking data; lockout persists across a new call from the same customer.
**Pass** A fresh call for the same customer is still locked out.

## 13. Balance query · 14. Transaction query · 15. Loan / due-date query

**Precondition** Authenticated.
**Action** Ask each in turn.
**Expected** Values match the database exactly.
**Pass** Spoken figures equal the tool result. **Any invented or rounded figure is a failure**, not a cosmetic issue.
**Verify** Tool name appears in the log; the value does not.

## 16. Realtime unavailable

**Precondition** Deliberately break realtime access (revoke the key on a staging account, or point at an unusable model). **Do not empty a production balance to test this.**
**Action** Place a call.
**Expected** Call refused promptly; service stays up; the next good call works.
**Pass** Gateway logs `rejected_realtime_unavailable` — **not** `rejected_capacity`. Backend logs `connection failed at CONNECTING: ... provider=<code>`. Database `REALTIME_START_FAILURE`. Capacity returns to 0.

## 17. Database unavailable

**Precondition** Stop the database container.
**Action** `GET /readiness`, then place a call.
**Expected** `503` with `checks.database.ready = false`; call refused; process stays alive.
**Pass** `/health` still returns 200 while `/readiness` returns 503. No capacity leak. Service recovers when the database returns, without a restart.

## 18. Gateway unavailable

**Precondition** Stop the gateway.
**Action** Place a call.
**Expected** No media attaches; the call is given up rather than held.
**Pass** `MEDIA_ATTACH_TIMEOUT`; capacity back to 0 within the configured budget.

## 19. Protocol mismatch

**Precondition** Run a gateway built before the negotiation handshake, or with a different `PROTOCOL_VERSION`.
**Action** Place a call.
**Expected** Refused before the caller is greeted. **The caller must not hear a greeting and then silence.**
**Pass** `PROTOCOL_MISMATCH`; backend logs the peer version; capacity back to 0; caller never greeted.

## 20. Service restart recovery

**Precondition** A call in progress.
**Action** Restart the backend.
**Expected** The in-flight call ends; the service comes back; the next call works.
**Pass** No capacity permanently held; `/readiness` returns 200 once up; a fresh call completes case 1.

---

## Recording results

For each case record: date, `provider_call_id`, pass/fail, `disconnect_reason`,
and any log line that was surprising. A case that passes for an unexpected
reason has not passed — note it and re-run.

Cases 1–15 exercise the conversation. Cases 16–20 exercise what happens when a
supplier fails, and are the ones most likely to be skipped under time pressure.
They are also the ones that decide whether the next production incident takes
minutes or days to diagnose.
