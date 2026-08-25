# Channel 2 — customer trust matrix

Every scenario is a permanent release gate. One command runs them all:

```
python -m pytest -m customer_trust -q
```

**Protected baseline: `818343d`.** Phase 6.10 cases are deterministically
`PROTECTED`; their `Live` column stays `PENDING` until the Phase 6.11 UAT,
because nothing here has been through a real telephone yet.

All tests live in `backend/tests/test_customer_trust.py`.

## The governing rule

> **What the caller is told must match what the bank believes, and what the
> telephone then does.**

Backend state → speech → telephone action. A break anywhere is a customer-trust
failure even when the security outcome was correct — the live wrong-PIN call
refused the caller correctly and still misled them about their call ending.

## Communication categories

Meanings, not sentences, so wording may improve without breaking the gate.

| Category | Must convey | Must never convey |
|---|---|---|
| `AUTH_RETRY` | verification failed; try again | ending, lockout, success |
| `SESSION_AUTH_ATTEMPTS_EXHAUSTED` | this call's attempts are used; the call will end | that the PIN or account is locked |
| `PERSISTENT_AUTH_LOCKOUT` | too many attempts; verification locked; the call will end | that ringing back will help |
| `AUTH_SUCCESS` | identity verified | unable to verify, locked, ending |
| `AUTHORIZATION_REFUSAL` | only the verified customer's information is available | a system outage |
| `SYSTEM_FAILURE` | temporarily unable to retrieve | that the caller is at fault |

`SESSION_AUTH_ATTEMPTS_EXHAUSTED` and `PERSISTENT_AUTH_LOCKOUT` are **never**
interchangeable: one caller may ring back and succeed, the other may not.

## Authentication

| CT | Scenario | Expected state | Category | Lifecycle | Test | Det | Live |
|---|---|---|---|---|---|---|---|
| CT-002 | Valid customer ID | `candidate` set, `V=false` | ask for PIN | active | `test_ct_002_…` | ✅ | PENDING |
| CT-003 | Unknown customer ID | identical to CT-002 | ask for PIN | active | `test_ct_003_…` | ✅ | PENDING |
| CT-004 | Malformed customer ID | `INVALID_CUSTOMER_ID_FORMAT` | retry | active | `test_ct_004_…` | ✅ | PENDING |
| CT-004b | Malformed PIN | attempts **unchanged** | `AUTH_RETRY` | active | `test_ct_004_a_malformed_pin_…` | ✅ | PENDING |
| CT-005 | Customer ID repeated | attempts preserved | ask for PIN | active | `test_ct_005_…` | ✅ | PENDING |
| CT-006 | Correct PIN, first attempt | `V=true`, `A=0` | `AUTH_SUCCESS` | active | `test_ct_006_…` | ✅ | PENDING |
| CT-D01 | Wrong PIN 1, clean slate | `A=1, L=false` | `AUTH_RETRY` | **active** | `test_ct_d01_…` | ✅ | PENDING |
| CT-102 | Wrong PIN 2 | `A=2, L=false`, 1 left | `AUTH_RETRY` | active | `test_ct_102_…` | ✅ | PENDING |
| CT-103 | Wrong PIN 3 | `L=true`, scope `SESSION`, id **not** locked | `SESSION_AUTH_ATTEMPTS_EXHAUSTED` | closing | `test_ct_103_…` | ✅ | PENDING |
| CT-011 | Final wrong PIN locks | `L=true`, 0 remaining | exhausted | closing | `test_ct_011_…` | ✅ | PENDING |
| CT-012 | Correct PIN after 1 wrong | `V=true` | `AUTH_SUCCESS` | active | `test_ct_012_…` | ✅ | PENDING |
| CT-014 | Correct PIN on last attempt | `V=true` | `AUTH_SUCCESS` | active | `test_ct_014_…` | ✅ | PENDING |
| CT-104 | New call after session exhaustion | authenticates | `AUTH_SUCCESS` | active | `test_ct_104_…` | ✅ | PENDING |
| CT-D02 | Persistent 4 + first wrong in new call | `L=true`, scope `PERSISTENT`, `A=1` | `PERSISTENT_AUTH_LOCKOUT` | closing | `test_ct_d02_…` | ✅ | **PROVEN** |
| CT-105 | Persistent threshold reached | scope `PERSISTENT` | persistent | closing | `test_ct_105_…` | ✅ | PENDING |
| CT-106 | New call while already locked | refused on attempt 1 | persistent | closing | `test_ct_106_…` | ✅ | PENDING |
| CT-D03 | Correct PIN while locked | refused, stays `L=true` | persistent | closing | `test_ct_d03_…` | ✅ | PENDING |
| CT-107 | Locked persists as `LOCKED` | `auth_status=LOCKED` | — | — | `test_ct_107_…` | ✅ | PENDING |
| CT-108 | Persistent lock expires | authenticates after window | `AUTH_SUCCESS` | active | `test_ct_108_…` | ✅ | PENDING |
| CT-109 | Retry and lockout never the same answer | scope present only on locks | — | — | `test_ct_109_…` | ✅ | PENDING |
| CT-101 | The approved limits are pinned (3 / 5) | — | — | — | `test_ct_101_…` | ✅ | PENDING |

## Disconnect-reason taxonomy

| CT | Scenario | Persisted reason | Test |
|---|---|---|---|
| CT-110 | The two endings map to different reasons | `AUTH_ATTEMPTS_EXHAUSTED` ≠ `AUTH_LOCKOUT` | `test_ct_110_…` |
| CT-111 | Three in-call failures | **`AUTH_ATTEMPTS_EXHAUSTED`** | `test_ct_111_…` |
| CT-112 | Persistent lock | **`AUTH_LOCKOUT`** | `test_ct_112_…` |
| CT-113 | End-to-end through the recorder | both, distinctly | `test_ct_113_…` |

## Lifecycle (D-1)

| ID | Scenario | Expected | Test |
|---|---|---|---|
| D1-1 | Lockout actually ends the call (INV-5) | armed → playback → disconnect | `test_d1_a_persistent_lockout_actually_ends_the_call` |
| D1-2 | The closing line is never cut off (INV-6) | no acknowledgement ⇒ call stays open | `test_d1_the_lockout_line_is_never_cut_off` |
| D1-3 | Its own disconnect reason | not `CALLER_GOODBYE` | `test_d1_a_locked_call_records_its_own_disconnect_reason` |
| D1-4 | A retry arms no closure (INV-9) | call stays active | `test_d1_a_retry_does_not_arm_any_closure` |

## Banking journeys

Only capabilities that exist. **CT-023 (card status) is not applicable** —
`BANKING_TOOLS` carries no card tool, and inventing one would test nothing.

| CT | Capability | Tool | Test |
|---|---|---|---|
| CT-020 | Savings balance | `get_account_balance` | `test_ct_020_to_025_…[CT-020]` |
| CT-021 | Current balance | `get_account_balance` | `…[CT-021]` |
| CT-022 | Recent transactions | `get_recent_transactions` | `…[CT-022]` |
| CT-023 | Card status | — | **N/A, no such tool** |
| CT-024 | Payment due date | `get_next_instalment` | `…[CT-024]` |
| CT-025 | Loan details | `get_loan_details` | `…[CT-025]` |

Each asserts: correct verified customer, exactly one invocation, one `OK`
event, correct value, call still active, identity retained.

## Security and authorization

| CT | Scenario | Expected | Test |
|---|---|---|---|
| CT-040 | DEMO001 asks DEMO002 balance | `OUT_OF_SCOPE`, one `FAILED` event | `…[CT-040]` |
| CT-041 | Cross-customer transactions | as above | `…[CT-041]` |
| CT-042 | Cross-customer loan | as above | `…[CT-042]` |
| CT-043 | Banking request before auth | `NOT_AUTHENTICATED`, no value | `test_ct_043_…` |
| CT-044 | Caller claims to be verified | state unchanged | `test_ct_044_…` |
| CT-045 | Verbal bypass attempt | state unchanged, no leak | `test_ct_045_…` |

All assert `AUTHORIZATION_REFUSAL` wording — no outage phrasing — and that no
`DEMO002` value appears anywhere in the result.

## Social and closing

| CT | Scenario | Expected | Test |
|---|---|---|---|
| CT-054 | "Thank you" alone | **call stays open** | `test_ct_054_…` |
| CT-064 | Explicit goodbye | closure armed | `test_ct_064_…` |

CT-054 asserts through `intents.classify`, the same expression
`_read_caller_intent` uses — not a convenience wrapper.

## Concurrency

| CT | Scenario | Test |
|---|---|---|
| CT-090 | 2 / 3 / 5 concurrent calls in mixed states | `test_ct_090_…[2,3,5]` |
| CT-091 | One customer's lockout does not block another | `test_ct_091_…` |

Calls are deliberately put in *different* states — verified, one wrong PIN,
still verifying — because identical calls would not detect a crossover.
**Production `REALTIME_MAX_ACTIVE_SESSIONS` is unchanged.**

## Communication contracts — the three authentication outcomes

The defect this phase closed: one generic lockout sentence for two different
facts. Now three distinct sentences, each asserted against the shipped prompt.

| Backend | Sentence | Test |
|---|---|---|
| `INVALID_CREDENTIALS` | "I'm unable to verify those details. Please try again." | `test_the_retry_sentence_…` |
| `lock_scope = SESSION` | "I couldn't complete verification on this call, so I'll end the call here. Please call again if you would like to try once more." | `test_the_session_exhaustion_sentence_…` |
| `lock_scope = PERSISTENT` | "Verification is temporarily locked after too many unsuccessful attempts. This call will now end." | `test_the_persistent_lock_sentence_…` |

The session sentence **must not** say anything is locked; the persistent one
**must not** invite a call back. `test_the_three_authentication_sentences_are_all_distinct`
holds all three apart, and `test_the_prompt_tells_the_two_endings_apart_by_lock_scope`
proves the model branches on backend state rather than its own judgement.

## Lifecycle bound

| CT | Scenario | Test |
|---|---|---|
| CT-121 | A locked call closes even if the model never speaks | `test_ct_121_…` |
| CT-122 | The bound never cuts off a line that does arrive | `test_ct_122_…` |
| CT-123 | A goodbye is never given a deadline (6.7 untouched) | `test_ct_123_…` |

## Multi-turn, social, closing, failure, events

| CT | Scenario | Test |
|---|---|---|
| CT-030 | Second balance question answered again | `test_ct_030_…` |
| CT-031 | Domain change account → loan | `test_ct_031_…` |
| CT-032 | Same question on two turns → two answers | `test_ct_032_…` |
| CT-035 | Correction uses what was said last | `test_ct_035_…` |
| CT-050/051/053 | Unsupported, non-banking, social reach no banking tool | `test_ct_050_to_053_…` |
| CT-052 | Ambiguous request asks which account | `test_ct_052_…` |
| CT-053 | Social greeting is courtesy | `test_ct_053_…` |
| CT-060–063 | Goodbye before / during / after auth / after banking | `test_ct_060_to_063_…` |
| CT-070 | Tool failure: no leak, identity intact | `test_ct_070_…` |
| CT-071 | Observability failure never reaches the caller | `test_ct_071_…` |
| CT-073 | Vanished session refused | `test_ct_073_…` |
| CT-074 | Malformed arguments refused | `test_ct_074_…` |
| CT-080 | Every representation rules the turn; one persists | `test_ct_080_…` |
| CT-081 | One utterance → one tool event | `test_ct_081_…` |
| CT-082 | Speech-started opens a turn without persisting one | `test_ct_082_…` |

## Not applicable, with evidence

| CT | Reason |
|---|---|
| CT-023 card status | No card tool exists in `BANKING_TOOLS`. Inventing one would test nothing. |
| CT-034 clarification | Covered by CT-052 — the production behaviour *is* `ACCOUNT_TYPE_REQUIRED` with the choices returned. A separate row would be the same test twice. |
| CT-036 barge-in | Already protected by `test_telephony_media.py` (`audio_interrupted` clears the queue and releases the response id). Duplicating it here would add a row, not coverage. |
| CT-065 provider hangup | Protected by `test_telephony_disconnect_reasons.py` (`PROVIDER_ENDED`). |
| CT-066 silence timeout | Protected by `test_telephony_call_journey.py` and `test_telephony_lifecycle.py` (`CALLER_SILENT`). |
| CT-001 greeting | Protected by `test_greeting_flow.py` and `test_telephony_media.py` (greeted once, only after media is ready). |
| CT-017/018 silence during auth | The silence timer is state-independent — `test_telephony_lifecycle.py` proves it for any state, so an auth-specific row would assert the same code path. |
| CT-072 Realtime provider failure | Protected by `test_telephony_admission.py` and `test_telephony_production.py` (`REALTIME_START_FAILURE`, `REALTIME_RUNTIME_FAILURE`). |
