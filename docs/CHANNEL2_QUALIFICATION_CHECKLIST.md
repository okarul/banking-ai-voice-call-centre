# Channel 2 — permanent qualification checklist

A standing project artifact, not a phase document. Every future Channel 1 or
Channel 2 change is measured against it.

**Protected baseline: `818343d`** — deployed to production and live-tested
successfully. Everything below marked `Live = PROVEN` has been observed on a
real telephone call, not merely asserted deterministically.

## Status vocabulary

| Status | Meaning |
|---|---|
| `PROVEN` | observed working with evidence — a live call, or a deterministic run of the real path |
| `PROTECTED` | an automated regression test exists **and** is in the release gate |
| `PENDING` | intended, not yet evidenced |
| `FAILED` | was proven or protected, and is now failing — **stop and fix** |

A row may be both `PROVEN` and `PROTECTED`; those are the only rows safe to
build on.

## Rules

1. A case becomes `PROVEN` only with evidence. Not by intent, not by review.
2. A case becomes `PROTECTED` only when a named automated test guards it.
3. **Never silently downgrade** a `PROVEN` or `PROTECTED` row.
4. **Never delete a protected test** because a change makes it fail.
5. **Never rewrite an expected result** to make a regression pass.
6. If behaviour changes deliberately, **stop for explicit review** before the
   baseline moves. Record who decided and why.

## Release gate

```
python -m pytest -m balance_determinism -q # order-invariance gate
python -m pytest -m customer_trust -q      # customer-trust gate
python -m pytest tests/test_browser_call.py tests/test_dashboard.py \
                 tests/test_realtime.py tests/test_turn_gate.py -q   # Channel 1
python -m pytest $(ls tests/*.py | grep -E "telephony|gateway|freeswitch") -q  # Channel 2
python -m pytest tests -q                  # complete deterministic suite
```

Required at every stage: **0 failed, 0 errors, 0 regressions**.

---

## Q-100 — protected baseline at `818343d`

The behaviours §S names. These were working before Phase 6.10 and must not move.

| ID | Scenario | Det. | Live | Test | Protects | Crit. |
|---|---|---|---|---|---|---|
| Q-101 | DIDWW inbound reaches backend | PROTECTED | PROVEN | `test_telephony_webhook.py` | `routers/telephony.py` | HIGH |
| Q-102 | SIP/media connection stable | PROTECTED | PROVEN | `test_gateway_sip.py` | `gateway/sipserver.py` | HIGH |
| Q-103 | PCMU RTP path | PROTECTED | PROVEN | `test_telephony_audio.py` | `gateway/rtp.py` | HIGH |
| Q-104 | 160-byte / 20 ms pacing | PROTECTED | PROVEN | `test_gateway_udp_media.py` | `gateway/sources.py` | HIGH |
| Q-105 | Playback-boundary acknowledgement | PROTECTED | PROVEN | `test_telephony_playback_ack.py` | `telephony/media.py` | HIGH |
| Q-106 | Goodbye recognition | PROTECTED | PROVEN | `test_telephony_goodbye_integration.py` | `telephony/bridge.py` | HIGH |
| Q-107 | Explicit goodbye disconnects | PROTECTED | PROVEN | `test_telephony_teardown.py` | `telephony/service.py` | HIGH |
| Q-108 | `CALLER_GOODBYE` persisted once | PROTECTED | PROVEN | `test_telephony_disconnect_reasons.py` | `telephony/reasons.py` | HIGH |
| Q-109 | Realtime production connector | PROTECTED | PROVEN | `test_telephony_admission.py` | `realtime_manager.py` | HIGH |
| Q-110 | Capacity cleanup | PROTECTED | PROVEN | `test_capacity.py` | `browser_calls.py` | HIGH |
| Q-111 | DEMO001 identification | PROTECTED | PROVEN | `test_customer_trust.py::identified` | `auth/authentication.py` | HIGH |
| Q-112 | PIN 4821 authenticates DEMO001 | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `auth/authentication.py` | HIGH |
| Q-113 | `authenticated=true` persists | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-114 | `auth_status=VERIFIED` persists | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-115 | `customer_id=DEMO001` persists | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-116 | Savings balance executes | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `app/tools` | HIGH |
| Q-117 | `get_account_balance` recorded once | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-118 | `submit_customer_id` recorded once | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-119 | `submit_pin` recorded once | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-120 | `tool_call_count` semantics | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `observability/business.py` | MED |
| Q-121 | Channel 2 `conversation_messages` stays 0 | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | privacy contract | HIGH |
| Q-122 | PIN absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-123 | Balance absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-124 | Customer name absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-125 | DEMO002 balance blocked for DEMO001 | PROTECTED | PROVEN | `test_customer_trust.py::…[CT-040]` | `turn_gate.py` | HIGH |
| Q-126 | Cross-customer recorded once FAILED | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `realtime/tools.py` | HIGH |
| Q-127 | No DEMO002 data leaked | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `authorization/guards.py` | HIGH |
| Q-128 | Identity unchanged after refusal | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `turn_gate.py` | HIGH |
| Q-129 | Observability failure cannot break an answer | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-130 | Duplicate turn representations → one row | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime_manager.py` | MED |
| Q-131 | Channel 1 single-write persistence | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `routers/call.py` | HIGH |
| Q-132 | Channel 1 behaviour unchanged | PROTECTED | PROVEN | `test_browser_call.py` | `routers/call.py` | HIGH |
| Q-133 | Readiness healthy | PROTECTED | PROVEN | `test_telephony_production.py` | `observability/readiness.py` | MED |
| Q-134 | Health endpoint healthy | PROTECTED | PROVEN | `test_health.py` | `main.py` | MED |

**Live evidence at `818343d`.** The Phase 6.9 UAT exercised the business
persistence path on a real call and established: DEMO001 authenticated,
`auth_status = VERIFIED`, `customer_id = DEMO001`, `submit_customer_id`,
`submit_pin` and `get_account_balance` each recorded exactly once,
`tool_call_count` correct, `CALLER_GOODBYE` persisted, Channel 2
`conversation_messages` still zero, no PIN, balance or customer name in any
log, a cross-customer DEMO002 balance request blocked and recorded once as
`FAILED`, and the verified identity unchanged after that refusal. Those rows
are `PROVEN`.

Q-129, Q-130 and Q-131 stay `PENDING` deliberately: an observability outage, a
duplicated turn representation and Channel 1 single-write are protected
deterministically but were not reproduced on a live call, and a row is only
`PROVEN` with evidence.

---

## Q-200 — customer trust (Phase 6.10, in progress)

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| CT-D01 | First wrong PIN, clean slate → retry only | PROTECTED | PENDING | `test_ct_d01_...` | HIGH |
| CT-D02 | Live path was a persistent cross-call lock | PROTECTED | PROVEN | `test_ct_d02_...` | HIGH |
| CT-D03 | Locked session refuses the correct PIN | PROTECTED | PENDING | `test_ct_d03_...` | HIGH |
| CT-004 | Malformed PIN consumes no attempt | PROTECTED | PENDING | `test_ct_004_...` | MED |
| CT-007–010 | Each wrong PIN below the limit retries | PROTECTED | PENDING | `test_ct_007_to_010_...` | HIGH |
| CT-011 | Final wrong PIN locks out | PROTECTED | PENDING | `test_ct_011_...` | HIGH |
| CT-012 | Correct PIN after one wrong | PROTECTED | PENDING | `test_ct_012_...` | HIGH |
| CT-014 | Correct PIN on the last allowed attempt | PROTECTED | PENDING | `test_ct_014_...` | HIGH |
| — | Retry sentence never announces termination | PROTECTED | PENDING | `test_the_retry_sentence_...` | HIGH |
| — | Lockout sentence announces termination | PROTECTED | PENDING | `test_the_lockout_sentence_...` | HIGH |
| — | The two failure sentences are distinct | PROTECTED | PENDING | `test_the_two_authentication_...` | HIGH |

CT-D03 was `FAILED` by design at design time — a fail-before-fix recording
D-2. It now passes because the backend was fixed, not because the test was
weakened.

### Phase 6.10 additions

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| CT-101 | The two approved limits are pinned (3 / 5) | PROTECTED | PENDING | `test_ct_101_...` | HIGH |
| CT-102 | Second wrong PIN still offers a third | PROTECTED | PENDING | `test_ct_102_...` | HIGH |
| CT-103 | Third wrong PIN exhausts the session, does **not** lock the id | PROTECTED | PENDING | `test_ct_103_...` | HIGH |
| CT-104 | A fresh call after session exhaustion may still authenticate | PROTECTED | PENDING | `test_ct_104_...` | HIGH |
| CT-105 | Persistent lockout is reported as persistent | PROTECTED | PENDING | `test_ct_105_...` | HIGH |
| CT-106 | A new call while locked is refused from the first attempt | PROTECTED | PENDING | `test_ct_106_...` | HIGH |
| CT-107 | A locked caller persists as `LOCKED`, not `FAILED` | PROTECTED | PENDING | `test_ct_107_...` | HIGH |
| CT-108 | The persistent lock expires with its window | PROTECTED | PENDING | `test_ct_108_...` | MED |
| CT-109 | Retry and lockout are never the same answer | PROTECTED | PENDING | `test_ct_109_...` | HIGH |
| D1-1 | A persistent lockout actually ends the call (INV-5) | PROTECTED | PENDING | `test_d1_a_persistent_lockout_actually_ends_the_call` | HIGH |
| D1-2 | The lockout line is never cut off (INV-6) | PROTECTED | PENDING | `test_d1_the_lockout_line_is_never_cut_off` | HIGH |
| D1-3 | A locked call records its own disconnect reason | PROTECTED | PENDING | `test_d1_a_locked_call_records_its_own_disconnect_reason` | MED |
| D1-4 | A retry arms no closure (INV-9) | PROTECTED | PENDING | `test_d1_a_retry_does_not_arm_any_closure` | HIGH |

### Phase 6.10 completion

All applicable CT scenarios are now `PROTECTED` and in the release gate. See
`CHANNEL2_CUSTOMER_TRUST_MATRIX.md` for the full row-by-row map, including the
eight scenarios recorded **N/A with evidence** (either no such production
capability, or already protected by a named existing suite — duplicating them
would add a row, not coverage).

Live status stays `PENDING` for every Phase 6.10 row until the Phase 6.11 UAT.
Nothing here has been through a real telephone.

---

## Q-300 - order invariance (Phase 6.11)

**Live history for `get_account_balance`, DEMO001 Savings.** Recorded as
observed. Nothing below is rewritten to look better than it was.

| Baseline | Live outcome | Evidence |
|---|---|---|
| `818343d` | **PROVEN** - DEMO001 savings balance answered correctly on a real call | Phase 6.9 UAT |
| `67c3c71` | **FAILED** - Phase 6.11 live regression, intermittent. Two live calls, `26faddf0-...` and `730747e1-...`, both `authenticated = true`, `auth_status = VERIFIED`, `customer_id = DEMO001`, `status = COMPLETED`, `disconnect_reason = CALLER_GOODBYE`, and both recorded `get_account_balance` FAILED with `duration_ms = 0`. Other calls on the same baseline, same customer, same PIN, same database, were answered correctly. | live call records |
| this fix | **PENDING** live re-proof - deterministic qualification complete, not yet through a real telephone | `test_balance_determinism.py` |

`67c3c71` stays `FAILED`. It was a real regression on a real call, and the row
is history rather than a status to be tidied away once the cause is fixed.

**Cause.** Not Phase 6.10, which changed only the lockout wording and the
lockout state that wording is drawn from - nothing on the balance path. Phase
6.10 *exposed* a latent state-machine defect by changing how the model
conducted the call. The enquiry a caller made before verifying was remembered
only as a side effect of a banking tool being called too early and failing
`NOT_AUTHENTICATED`. When the model instead called `get_authentication_status`
first - a valid ordering, and the one the agent's own instructions ask for - no
banking tool ran, so nothing was remembered. Verification then completed on a
turn whose transcript is a spoken PIN, which classifies as
`NON_BANKING_REQUEST`, and the Phase 11 scope gate refused the very lookup the
caller had rung about: `OUT_OF_SCOPE`, before any database work, hence
`duration_ms = 0`.

**Fix.** The held enquiry is now taken from the caller's own classified turn
(`turn_gate._hold_unverified_enquiry`) instead of from a model tool failure.
The gate is unchanged and no guard was relaxed.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| BD-001 | The live regression, reproduced tool for tool (fail-before-fix) | PROTECTED | - | `test_the_live_regression_is_reproduced_exactly` | HIGH |
| BD-002 | A spoken PIN alone still admits no lookup | PROTECTED | PENDING | `test_the_pin_turn_alone_still_opens_nothing` | HIGH |
| BD-003 | Eight valid orderings, one business result | PROTECTED | PENDING | `test_every_valid_ordering_reaches_the_same_answer` | HIGH |
| BD-004 | The orderings agree with each other, not merely each work | PROTECTED | PENDING | `test_the_orderings_agree_with_each_other` | HIGH |
| BD-005 | The hold comes from the caller, not from a tool failure | PROTECTED | PENDING | `test_the_held_enquiry_comes_from_the_caller_not_from_a_tool_failure` | HIGH |
| BD-006 | Non-enquiry turns hold nothing and clear nothing | PROTECTED | PENDING | `test_an_unverified_caller_holds_nothing_they_did_not_ask_for` | MED |
| BD-007 | A loan enquiry is held as a loan enquiry, and admits only itself | PROTECTED | PENDING | `test_a_loan_enquiry_is_held_as_a_loan_enquiry` | HIGH |
| BD-008 | Resuming without restating the account still answers Savings | PROTECTED | PENDING | `test_every_valid_ordering_reaches_the_same_answer[C' ...]` | MED |
| BD-009 | Every SELF ordering paired with a refused DEMO002 ask | PROTECTED | PENDING | `test_the_same_call_still_refuses_another_customer` | HIGH |
| BD-010 | Naming another id before verifying holds no enquiry | PROTECTED | PENDING | `test_naming_another_customer_before_verifying_holds_no_enquiry` | HIGH |
| BD-011 | A pre-verification hold can only ever be the caller's own | PROTECTED | PENDING | `test_a_hold_made_before_verification_can_only_ever_be_the_callers_own` | HIGH |
| BD-012 | A hostile turn forfeits the hold | PROTECTED | PENDING | `test_a_held_enquiry_is_dropped_when_the_caller_turns_hostile` | HIGH |
| BD-013 | One call's hold never reaches another call | PROTECTED | PENDING | `test_one_callers_held_enquiry_never_reaches_another_call` | HIGH |
| BD-014 | 100 calls, 100 identical outcomes, zero intermittency | PROTECTED | PENDING | `test_one_hundred_calls_give_one_hundred_identical_answers` | HIGH |
| BD-015 | Four refusals keep four distinct reasons | PROTECTED | PENDING | `test_a_refusal_says_which_refusal_it_was` | MED |
| BD-016 | An unreachable database is reported as an outage and still counted | PROTECTED | PENDING | `test_an_unreachable_database_is_reported_as_an_outage` | MED |
| BD-017 | A balance asked without naming an account still gets answered | PROTECTED | PENDING | `test_a_balance_asked_without_naming_an_account_still_gets_answered` | MED |
| BD-018 | The SDK really would hand a tool exception to the model, so the boundary is kept for a checked reason | PROTECTED | PENDING | `test_the_tool_boundary_never_hands_an_exception_to_the_model` | HIGH |
| BD-019 | An unexpected defect is classified `INTERNAL_TOOL_ERROR` and kept loud, with its traceback | PROTECTED | PENDING | `test_an_unexpected_defect_is_classified_and_kept_loud` | MED |
| BD-020 | A call that ended mid-enquiry is a lifecycle fact, not a defect | PROTECTED | PENDING | `test_a_call_that_ended_mid_enquiry_is_not_reported_as_a_defect` | MED |
| BD-021 | A cancelled call still cancels through the boundary | PROTECTED | PENDING | `test_a_cancelled_call_still_cancels` | HIGH |
| BD-022 | A logged traceback carries no connection details | PROTECTED | PENDING | `test_a_logged_traceback_carries_no_connection_details` | HIGH |

Every Q-300 row is `PENDING` live: none of this has been through a real
telephone. Each becomes `PROVEN` at the Phase 6.11 UAT and not before.

---

## Q-400 - release-gate reliability (Phase 6.11 hardening)

A protected gate that fails one test in four runs protects nothing: the next
real regression is indistinguishable from the noise, and the habit of re-running
until it goes green is how a genuine defect gets waved through. Every
intermittent failure observed during Phase 6.11 was therefore chased to a named
cause and fixed at that cause.

Three of them were the *same production defect* seen from three angles, and it
was not a test problem at all.

### D-4 - a hang-up could strand the call it ended

`service.tear_down` is the single place a call's resources are freed. It is
called from a `finally` in the media socket route, and from bridge tasks that a
closing call is itself cancelling - so the event that ends a call is frequently
the same event that cancels the task cleaning up after it. A `CancelledError`
delivered while `tear_down` sat between two of its own `await`s aborted it
part-way: reliably *after* the bridge left `phone_call_registry`, and reliably
*before* `voice_call_manager.close()`.

What that left behind:

* the provider session stayed open, billing and holding a connection;
* its capacity slot was never returned;
* and `sweep_idle_calls` could reclaim neither, because the sweep walks the
  registry the bridge had already left.

On the deployed configuration (`REALTIME_MAX_ACTIVE_SESSIONS = 1`) that is one
dropped socket away from every subsequent caller being told the bank is full,
until the process is restarted.

Proven by instrumenting `tear_down` during a failing run:

```
tear_down IN                      5e7dc4
tear_down RAISED CancelledError   5e7dc4     <- no close, no release
```

**Fixed** by making the release its own task and having the caller merely wait
for it (`service._uninterruptible`): cancelling the waiter stops the waiting,
never the releasing. Each release step is also attempted independently, so one
failing step can no longer strand the provider session behind it. The media
route's two steps - releasing the call and recording that it ended - became one
shielded unit (`service.end_media_call`), because shielding only the first left
a released call whose row stayed `ACTIVE` with no `ended_at`.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| TD-001 | A hang-up that cancels the cleanup still releases the call | PROTECTED | PENDING | `test_a_hang_up_that_cancels_the_cleanup_still_releases_the_call` | HIGH |
| TD-002 | One failing release step does not strand the ones after it | PROTECTED | PENDING | `test_a_step_that_fails_does_not_strand_the_steps_after_it` | HIGH |
| TD-003 | A cancelled cleanup is still exactly one cleanup | PROTECTED | PENDING | `test_a_cancelled_cleanup_is_still_only_one_cleanup` | HIGH |
| TD-004 | The in-flight release is held against collection | PROTECTED | PENDING | `test_the_release_task_is_held_until_it_finishes` | MED |
| TD-005 | A cancelled hang-up still records the ending | PROTECTED | PENDING | `test_a_cancelled_hang_up_still_records_the_ending` | HIGH |

### M-1 - the tool exception boundary, narrowed and justified

`_run_tool` catches at the tool body because the alternative is worse than a
broad catch. Left to propagate, an exception out of a `@function_tool` is caught
by the Agents SDK (`agents.tool._FailureHandlingFunctionToolInvoker.__call__`),
handed to `default_tool_error_function`, and returned **to the model** as
`"...Error: {str(error)}"`. For the exception this path is most likely to see
that string is not generic - a SQLAlchemy `OperationalError` renders the failing
statement, its bound parameters and the database host. On a live call, to a
model instructed to report what its tools return, that is an
information-disclosure path into the audio. Two further things happen on that
route: the invocation never reaches `business.record_tool_outcome`, so the board
shows no tool event at all; and nothing is logged above debug.

The two operational cases are now named - `SessionNotFoundError` ->
`SESSION_NOT_FOUND`, `DatabaseNotConfiguredError` / `SQLAlchemyError` ->
`DATABASE_UNAVAILABLE` - and neither carries a traceback, because neither is a
defect. What remains broad is the last clause, deliberately: the set of ways
Python code can be wrong is not enumerable, and a defect that escaped would take
the SDK route above. It is classified `INTERNAL_TOOL_ERROR` and logged at ERROR
*with* a traceback, so it is louder than before, not quieter.

That traceback is now safe to log, which it was not. `RedactingFilter` scrubbed
`record.exc_text` - but a filter runs *before* formatting, so `exc_text` is
empty at that point and every `exc_info=True` call site in this application was
emitting an unredacted traceback, whose last line is `str(exception)`. The
filter now renders the traceback itself, scrubs it, and caches it. Proven by
`test_a_logged_traceback_carries_no_connection_details`, which fails on the old
filter with the database password printed in full.

### The remaining flakes, and what each really was

| Test | Cause | Production or harness | Fix |
|---|---|---|---|
| `test_telephony_media_socket.py::test_two_calls_each_get_their_own_socket_and_audio` | D-4 | **production** | `service._uninterruptible` |
| `test_telephony_media_socket.py::test_a_caller_hangup_still_works_unchanged` | D-4 | **production** | as above |
| `test_telephony_media_socket.py::test_the_caller_going_away_releases_the_call` | D-4, and then the unshielded second half of the route's cleanup | **production** | `service.end_media_call` |
| `test_gateway_udp_media.py::test_no_relay_or_socket_outlives_its_call` | collateral of D-4: it starts `REALTIME_LIVE_CEILING` calls, and a slot stranded by an earlier test meant the last one was refused | **production** | as above |
| `test_telephony_disconnect_reasons.py::test_a_real_hang_up_is_still_recorded_as_one` | `settle()` waited for the call to be *released* and then read the reason, which is written after | harness | `settle()` now also waits for the ending to be recorded |
| `test_pin_lockout_persistence.py::test_the_lock_applies_to_a_phone_session` | a fixed `provider_call_id` in a file that wipes nothing: the second run of the test was a DUPLICATE of the first, so no phone session was created and the test read the previous run's destroyed one. It passed only when an unrelated file had happened to clear `agent_sessions` first | harness | a per-run call id, and the media-attach budget pinned out of reach of the test's own runtime |

No sleep was lengthened to make any of these pass.

---

## Open defects

| ID | Defect | Evidence | Status |
|---|---|---|---|
| **D-1** | Announced termination did not terminate. | fixed by `bridge._close_if_authentication_is_over`; guarded by `test_d1_*` | **CLOSED** |
| **D-2** | A persistently-locked caller had `session.authentication_locked == False`. | fixed in `verify_pin`; guarded by CT-D03, CT-106, CT-107 | **CLOSED** |
| **D-3** | A verified DEMO001 was refused their own savings balance, intermittently, decided by which tool the model called first. Live calls `26faddf0-...` and `730747e1-...` on `67c3c71`: `get_account_balance` FAILED, `duration_ms = 0`, no database lookup reached. | fixed by `turn_gate._hold_unverified_enquiry`; guarded by BD-001 to BD-016 | **CLOSED deterministically - PENDING live re-proof** |
| **D-4** | A hang-up could cancel its own cleanup: `tear_down` aborted after the bridge left `phone_call_registry` and before `voice_call_manager.close()`, stranding an open provider session and its capacity slot with no path to reclaim either - on `REALTIME_MAX_ACTIVE_SESSIONS = 1`, one dropped socket from refusing every later caller. | traced in a failing run (`tear_down RAISED CancelledError`); fixed by `service._uninterruptible` and `service.end_media_call`; guarded by TD-001 to TD-005 | **CLOSED deterministically - PENDING live re-proof** |

## Open decision

**The per-call attempt limit.** `MAX_AUTHENTICATION_ATTEMPTS = 3`; the Phase
6.10 brief assumes 5. Raising it is a security-policy change, not a defect fix.
CT-007–011 are written against the code's actual limit so that no test hard-codes
a decision nobody has made. **Awaiting explicit direction.**