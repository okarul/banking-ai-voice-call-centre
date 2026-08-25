# Channel 2 — authoritative telephone banking state machine

The one description of what a telephone call may be doing, what the backend
believes while it does it, and what the assistant is allowed to say about it.

Protected baseline `818343d` (deployed). **Not yet implemented as a single enum** — see
§5. Today the state is spread across three places that must agree:

| Where | What it holds |
|---|---|
| `Session.authenticated` / `.authentication_attempts` / `.authentication_locked` | who the caller is, per `app/auth/authentication.py` |
| `CallLifecycle.state` (`app/telephony/lifecycle.py`) | `OPENING · ASSISTANT_SPEAKING · WAITING_FOR_CALLER · CALLER_SPEAKING · CLOSING · CLOSED` |
| `agent_sessions` row | what an operator sees afterwards |

The banking states below (`IDENTIFYING`, `VERIFYING`, `SERVING`, `LOCKED`) are
**derived** from the session, not stored. That is the gap §5 records.

---

## 1. The governing rule

> **The assistant's speech may never assert a state the backend does not hold.**

Three corollaries, each an invariant in §4:

1. If the assistant announces termination, the call **must** actually end.
2. If the backend is still accepting attempts, the assistant **must not**
   announce termination or lockout.
3. Authentication and disconnection are **backend decisions**. The model
   reports them; it never makes them.

---

## 2. States

| State | Backend condition | Telephone |
|---|---|---|
| `OPENING` | session created, nothing said | active |
| `IDENTIFYING` | `customer_id is None`, not locked | active |
| `VERIFYING` | `candidate_customer_id` set, `authenticated == false`, `locked == false` | active |
| `SERVING` | `authenticated == true` | active |
| `LOCKED` | `authentication_locked == true` | active until closing line plays |
| `CLOSING` | lifecycle `CLOSING`; final response draining | active until playback acknowledged |
| `ENDED` | lifecycle `CLOSED`; row closed | disconnected |

---

## 3. Transitions

`A` = `authentication_attempts`, `L` = `authentication_locked`,
`V` = `authenticated`.

### 3.1 Identification

| # | Trigger | From → To | A | L | V | Communication | Phone |
|---|---|---|---|---|---|---|---|
| T1 | call answered | — → `OPENING` | 0 | f | f | `GREETING` | active |
| T2 | greeting played | `OPENING` → `IDENTIFYING` | 0 | f | f | ask for customer ID | active |
| T3 | valid ID | `IDENTIFYING` → `VERIFYING` | 0 | f | f | ask for PIN | active |
| T4 | unknown / malformed ID | `IDENTIFYING` → `IDENTIFYING` | 0 | f | f | `RETRY_AUTH_REQUIRED` | active |
| T5 | ID repeated | `VERIFYING` → `VERIFYING` | unchanged | f | f | ask for PIN again | active |

T4 is deliberately indistinguishable between "unknown" and "malformed": the
difference is what an attacker is probing for.

### 3.2 Verification — the area this phase exists for

| # | Trigger | From → To | A | L | V | Communication | Phone |
|---|---|---|---|---|---|---|---|
| T6 | wrong PIN, `A+1 < PER_CALL_MAX`, not persistently locked | `VERIFYING` → `VERIFYING` | `A+1` | **f** | f | **`RETRY_AUTH_REQUIRED`** | **active** |
| T7 | wrong PIN reaching `PER_CALL_MAX` | `VERIFYING` → `LOCKED` | `A+1` | **t** | f | `LOCKOUT_TERMINATION` | active → closing |
| T8 | wrong PIN tripping the **persistent cross-call** lock | `VERIFYING` → `LOCKED` | `A+1` (may be 1) | **t** | f | `LOCKOUT_TERMINATION` | active → closing |
| T9 | correct PIN, not locked | `VERIFYING` → `SERVING` | 0 | f | **t** | `AUTH_SUCCESS` | active |
| T10 | correct PIN while locked | `LOCKED` → `LOCKED` | unchanged | t | **f** | `LOCKOUT_TERMINATION` | closing |
| T11 | malformed PIN | `VERIFYING` → `VERIFYING` | **unchanged** | f | f | `RETRY_AUTH_REQUIRED` | active |

**T6 must never produce termination or lockout wording.** T11 does not consume
an attempt — a value that is not four digits is rejected before any database
work.

**T8 is the transition the live call actually took.** See §6.

### 3.3 Serving

| # | Trigger | From → To | Permitted tools | Communication | Phone |
|---|---|---|---|---|---|
| T12 | own-account enquiry | `SERVING` → `SERVING` | all banking tools | answer with the tool value | active |
| T13 | cross-customer enquiry | `SERVING` → `SERVING` | **none** — refused at the gate | `AUTHORIZATION_REFUSAL` | active |
| T14 | unsupported / non-banking | `SERVING` → `SERVING` | none | scope sentence | active |
| T15 | "thank you" alone | `SERVING` → `SERVING` | none | courtesy | **active** |
| T16 | tool fails technically | `SERVING` → `SERVING` | attempted, failed | `SYSTEM_FAILURE` | active |

T13 and T16 must not share wording. "Unable to retrieve that right now" for an
authorization refusal tells a caller the bank is broken when it is working
exactly as designed.

### 3.4 Closing

| # | Trigger | From → To | Recorded | Phone |
|---|---|---|---|---|
| T17 | explicit goodbye | any → `CLOSING` | `CALLER_GOODBYE` | drops after playback acknowledged |
| T18 | silence past timeout | any → `CLOSING` | `CALLER_SILENT` | drops after closing line |
| T19 | authentication over (either limit) | `LOCKED` → `CLOSING` | `AUTH_LOCKOUT` | drops after final line is acknowledged |
| T20 | caller hangs up | any → `ENDED` | `CUSTOMER_ENDED` | already down |
| T21 | provider ends | any → `ENDED` | `PROVIDER_ENDED` | already down |
| T22 | idle sweep | any → `ENDED` | `IDLE_TIMEOUT` | released |

---

## 4. Invariants

Machine-checkable; each maps to an assertion helper.

| ID | Invariant |
|---|---|
| INV-1 | `V == false` ⇒ no protected banking tool succeeds |
| INV-2 | `L == false` ⇒ assistant must not claim lockout |
| INV-3 | `A < PER_CALL_MAX` **and** not persistently locked ⇒ assistant must not announce termination |
| INV-4 | `L == true` ⇒ correct PIN cannot authenticate |
| INV-5 | assistant announces termination ⇒ lifecycle reaches `CLOSING` |
| INV-6 | lifecycle `CLOSING` **and** final playback acknowledged ⇒ disconnect occurs |
| INV-7 | `AUTHORIZATION_REFUSAL` ⇒ no other-customer value returned, and no outage wording |
| INV-8 | `SYSTEM_FAILURE` wording ⇒ a real technical failure occurred |
| INV-9 | `RETRY_AUTH_REQUIRED` ⇒ telephone remains active |
| INV-10 | one logical utterance ⇒ one transition, one tool action, one persisted turn, one response |

**INV-5 is the invariant the live call broke.** See §6.

---

## 5. Known gaps at `818343d`

| Gap | Detail |
|---|---|
| ~~G-1 — no lockout → lifecycle wiring~~ | **CLOSED (Phase 6.10).** `PhoneCallBridge._close_if_authentication_is_over` arms closure through the existing `arm_goodbye` path when authentication stops accepting attempts. T19 is implemented; INV-5 holds. |
| **G-2 — no single state enum** | Banking state is derived, not stored, so "the state" cannot be asserted directly. |
| ~~G-3 — termination is prompt-only~~ | **CLOSED (Phase 6.10).** The backend now decides: the lifecycle closes on `authentication_locked` regardless of what the model says. Speech reports the ending; it no longer is the ending. |
| ~~G-4 — attempt-limit ambiguity~~ | **RESOLVED.** Policy approved: 3 per call, 5 persistent. See §7. |

---

## 6. What the live call actually did

Reproduced deterministically:

```
A) clean slate, first wrong PIN 1111
     reason = INVALID_CREDENTIALS   attempts = 1  locked = False  remaining = 2

B) 4 prior failures within the 15-minute window, then a NEW call's first wrong PIN
     reason = AUTHENTICATION_LOCKED attempts = 1  locked = True   remaining = 0
```

The live call took path **B** — transition **T8**, not T6. The persistent
cross-call lock (`PIN_LOCKOUT_MAX_ATTEMPTS = 5`, 15-minute window) had already
accumulated failures from earlier UAT, so that call's *first* wrong PIN was the
fifth within the window.

Therefore:

- The backend was **correct**: the caller genuinely was locked.
- The speech was **consistent** with backend state — `banking_realtime.py:197`
  is the lockout line, and lockout was true.
- The defect is **G-1 / INV-5**: termination was announced and never happened.

The original report — "one wrong PIN ended the session" — is not what occurred.
One wrong PIN *within that call* coincided with a lock earned across previous
calls. Fixing T6 would have changed nothing.

---

## 7. Attempt limits — APPROVED POLICY

| Limit | Value | Scope | Source |
|---|---|---|---|
| `MAX_AUTHENTICATION_ATTEMPTS` | **3** | one call | `app/auth/authentication.py:21` |
| `PIN_LOCKOUT_MAX_ATTEMPTS` | **5** | across calls, 15 min | `app/config.py:151` |

**Approved and pinned.** The per-call limit stays at three; it is *not* raised
to five. The earlier CT-007…CT-011 specification assuming five in-call attempts
is superseded. `test_ct_101_the_two_limits_are_the_approved_values` fails if
either moves, so a change to these becomes a deliberate decision rather than a
quiet edit.

### The distinction that matters to a caller

Both limits set `authentication_locked` and both end the call, but they are
different facts and are spoken about differently. `verify_pin` returns
`lock_scope` to say which:

| `lock_scope` | Meaning | What the caller may do |
|---|---|---|
| `SESSION` | this call used its three attempts | ring back and try again |
| `PERSISTENT` | five failures against this id in the window | wait for the window to expire |

`reason` stays `AUTHENTICATION_LOCKED` for both — the authorization guard, the
dashboard and twenty-two tests read that string, and an attacker must not be
able to tell the two apart from the spoken answer either.

**Saying "your PIN is locked" to a caller who has merely used up one call's
attempts is a false statement about their account**, and it sends a customer to
a branch over a call they could simply repeat.
