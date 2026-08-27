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
| Q-121a | Channel 2 stores no spoken word **by default** (Phase 6.12: conditional on `TELEPHONY_TRACE_UTTERANCES`, off unless an operator opts in) | PROTECTED | PENDING | `test_by_default_no_spoken_word_is_stored_at_all` | privacy contract | HIGH |
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

## Q-500 - timing is not a banking input (Phase 6.11.1)

`ac2343f` removed the *ordering* dependency. Live testing then showed a second,
independent dependency underneath it: **timing**. Same customer, same PIN, same
database, same tool order - and two different answers, decided by whether a
microphone burst happened to transcribe.

**Live history for `get_account_balance`, DEMO001 Savings.** Recorded as
observed. Nothing here is rewritten to look better than it was.

| Baseline | Live outcome | Evidence |
|---|---|---|
| `818343d` | **PROVEN** - balance answered correctly on a real call | Phase 6.9 UAT |
| `67c3c71` | **FAILED** - `OUT_OF_SCOPE`, `duration_ms = 0`, no database lookup | calls `26faddf0-...`, `730747e1-...` |
| `ac2343f` | **LIVE INTERMITTENT** - one PASS `0a39cc5f-1bc4-1240-4790-eaa5afddeeef`; one FAIL `3860a81f-1bc5-1240-4790-eaa5afddeeef` (`get_account_balance` FAILED twice, `TURN_NOT_CLASSIFIED`, ~4 s each); one subsequent PASS `7d221518-1bc5-1240-4790-eaa5afddeeef` (`duration_ms = 5`) | live call records |
| this fix | **PENDING** live re-proof | `test_balance_determinism.py` |

`ac2343f` is **not** marked PROVEN. It is `LIVE INTERMITTENT`: it fixed the
ordering defect and left the timing defect standing.

### D-5 - a turn that never transcribed refused the caller's own question

`app/realtime/tools.py::_check_scope` awaited
`turn_gate.wait_for_ruling(session)` with `timeout = TRANSCRIPT_WAIT_SECONDS =
4.0`, polling `TurnGate.pending` every 50 ms. The two ends of that flag were not
symmetric:

* **opened** by `open_turn()`, driven from `RealtimeManager._feed_gate` on the
  raw provider event `input_audio_buffer.speech_started` - a VAD onset, emitted
  for *any* speech-shaped sound, and forwarded by the SDK for every WebSocket
  message (`openai_realtime.py:1315`);
* **closed** only by `record_decision()`, reachable only through
  `record_turn()`, which was called from two places both conditional on a
  non-empty transcript, and which itself returned early on an empty one. The
  only typed transcript event the SDK emits is
  `input_audio_transcription_completed`, mapped solely from
  `conversation.item.input_audio_transcription.completed`
  (`openai_realtime.py:1499-1513`). There is no typed event for
  `...transcription.failed` at all.

A breath, a cough, a burst of line noise, a barge-in the provider discards, or a
transcription that fails therefore opened a turn that **nothing could ever
close** - no other writer, no timer, no fallback. Every later banking tool spent
the full 4.0 s and `refusal_for()` returned `TURN_NOT_CLASSIFIED` at its *first*
branch, which sat above the held-enquiry exemption. The server knew the caller
was DEMO001, knew a Savings balance was owed to them, and refused it anyway
because a sound had not turned into words.

That is `duration_ms = 4023` and `4015` in the failing live call, against
`duration_ms = 5` in the successful one.

**Fixed** by making the authority independent of the ruling rather than by
waiting longer:

* `turn_gate.resumes_held_enquiry()` - two server-side facts (verified caller +
  a held enquiry naming *this* tool) authorise the lookup on their own. Checked
  **before** the gate in `refusal_for`, and used in `_check_scope` to skip a
  wait whose answer could not change the outcome.
* `turn_gate.record_unintelligible_turn()` - a turn the provider reports as
  producing nothing now *resolves* as not-allowed instead of staying open, on
  both an empty transcript and a raw `...transcription.failed`.

No timeout was raised, no retry added, no sleep introduced, no prompt or model
changed.

### Every path that can still return `TURN_NOT_CLASSIFIED`

One, at `turn_gate.py::refusal_for`, reached only when the gate is pending
**and** no held enquiry is owed to a verified caller.

| Path | Classification |
|---|---|
| verified caller, held enquiry naming this tool | **impossible by construction** - the invariant is encoded, and the wait is skipped |
| turn genuinely unclassified, nothing owed | **legitimate safety refusal** - fail-closed, unchanged policy |
| turn left open by a wordless onset the provider reported | **race, now closed** - resolved by `record_unintelligible_turn` |
| timeout fallback | retained, but can no longer decide a server-known SELF request |

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| TN-001 | A turn that never transcribes no longer refuses the held enquiry (fail-before-fix) | PROTECTED | - | `test_a_turn_that_never_transcribes_no_longer_refuses_the_held_enquiry` | HIGH |
| TN-002 | An empty transcript resolves the turn instead of hanging | PROTECTED | PENDING | `test_a_wordless_turn_resolves_instead_of_hanging[empty_transcript]` | HIGH |
| TN-003 | A failed transcription resolves the turn instead of hanging | PROTECTED | PENDING | `test_a_wordless_turn_resolves_instead_of_hanging[transcription_failed]` | HIGH |
| TN-004 | Ruling already available before the tool | PROTECTED | PENDING | `test_1_ruling_already_available_before_the_tool` | MED |
| TN-005 | Tool starts before the ruling | PROTECTED | PENDING | `test_2_tool_starts_before_the_ruling` | HIGH |
| TN-006 | A slightly delayed ruling changes nothing | PROTECTED | PENDING | `test_3_a_slightly_delayed_ruling_changes_nothing` | HIGH |
| TN-007 | A ruling delayed past the old timeout is not waited for | PROTECTED | PENDING | `test_4_a_ruling_delayed_past_the_old_timeout_is_not_waited_for` | HIGH |
| TN-008 | Status check before authentication | PROTECTED | PENDING | `test_5_status_check_before_authentication` | MED |
| TN-009 | Id, PIN, then an immediate balance | PROTECTED | PENDING | `test_6_id_then_pin_then_immediate_balance` | HIGH |
| TN-010 | A balance asked before authentication survives it | PROTECTED | PENDING | `test_7_a_balance_asked_before_authentication_survives_it` | HIGH |
| TN-011 | Authenticate first, then ask | PROTECTED | PENDING | `test_8_authenticate_first_then_ask` | MED |
| TN-012 | A repeated own balance request is answered again | PROTECTED | PENDING | `test_9_a_repeated_own_balance_request_is_answered_again` | MED |
| TN-013 | A typed turn is as good as a spoken one | PROTECTED | PENDING | `test_10_a_typed_turn_is_as_good_as_a_spoken_one` | MED |
| TN-014 | A cross-customer ask is refused whatever the timing | PROTECTED | PENDING | `test_a_cross_customer_ask_is_refused_whatever_the_timing` | HIGH |
| TN-015 | An unclassified turn cannot revive a forfeited enquiry | PROTECTED | PENDING | `test_an_unclassified_turn_cannot_revive_a_forfeited_enquiry` | HIGH |
| TN-016 | An unverified caller is never admitted by a held enquiry | PROTECTED | PENDING | `test_an_unverified_caller_is_never_admitted_by_a_held_enquiry` | HIGH |
| TN-017 | 200 randomised schedulings, one business result | PROTECTED | PENDING | `test_two_hundred_schedulings_give_one_business_result` | HIGH |

Every Q-500 row is `PENDING` live. Each becomes `PROVEN` at the next UAT and not
before.

---

## Q-600 - the conversation trace (Phase 6.12)

Phase 6.11.1 was diagnosed from production log lines pieced together by hand.
`agent_sessions` could say a call ended `PROVIDER_ENDED` with six tool calls;
`agent_tool_events` could say `get_account_balance` failed twice taking four
seconds each. Neither could say *why*, and the four seconds was the whole
answer. This is that missing context, kept on purpose.

### The decision that had to be made first

Channel 2 has never stored a spoken word - **Q-121**, `PROTECTED` and `PROVEN`,
guarded by `test_channel_2_stores_no_transcript` and written as *"Whatever else
changes, spoken words must not start being kept."* Phase 6.12 asks for a
per-turn "redacted customer utterance". Those cannot both be unconditionally
true, and checklist Rule 6 requires a deliberate behaviour change to be
reviewed and recorded rather than absorbed.

**Decided: config-gated, default off.** Requested by the operator, 2026-08-26.

* The trace always records what the backend **decided** - scope ruling, held
  enquiry, authentication state, tool, reason, duration, ordering, ending. None
  of that is speech - but it is **off by default** all the same, for cost
  rather than privacy: each traced event is a database write of roughly 35 ms,
  and a call makes one per turn, per tool and per authentication change. That
  was enough to delay assistant audio and fail a gateway isolation test. A
  diagnostic may not tax the calls it exists to diagnose, so `TRACE_ENABLED`
  defaults to false and a UAT switches it on.
* Utterances are recorded **only** when `TELEPHONY_TRACE_UTTERANCES` is set,
  which is off by default. Q-121 therefore remains literally and materially
  true on an unconfigured deployment, and a UAT box opts in deliberately.
* When it is on, an utterance is stored only in the redacted form the browser
  transcript already uses: a PIN turn is `[PIN REDACTED]`, an id turn is
  `[Customer ID provided]`, never digits.

**Q-121 is therefore not superseded.** It is now conditional, and the condition
defaults to its favour. `test_by_default_no_spoken_word_is_stored_at_all` is
the standing proof.

### Architecture

One new append-only table, `call_trace_events`, and **no new event sources**.
Every trace event is hung off a funnel that already existed, so nothing is
counted twice and `tool_call_count` is untouched:

| What | Existing funnel reused | Event |
|---|---|---|
| caller turn + scope ruling | `business.record_turn_decision` | `TURN` / CUSTOMER |
| tool invocation + outcome | `business.record_tool_outcome` | `TOOL` |
| authentication transition | `business.record_identity` | `AUTH` |
| the agent's own words | `bridge._on_history_item` | `TURN` / AGENT |
| the ending | `service._release_everything` | `LIFECYCLE` |

Ordering is by `sequence`, not by clock: two events can share a timestamp, and
a replay that puts a tool result before its own call is worse than none.
Duplicate suppression is a unique index on `(session_pk, idempotency_key)`, so
a retried provider event or a second representation of one utterance is a
no-op rather than a second row.

### Privacy

* Tool arguments are sanitised by **allowlist** (`account_type`, `loan_type`,
  `limit`). `spoken_pin` and `spoken_customer_id` are not on it and cannot be,
  so a credential cannot reach the table even from a future call site nobody
  reviewed. Non-allowlisted names keep the name and lose the value.
* Utterances pass through `redact_transcript` before storage, never after.
* `customer_ref` is only ever the identity the PIN check established - a claim
  is not an identity, and a trace that recorded claims would lie about who was
  on the call.
* A backend failure reaches the trace as a **reason code**, never a statement:
  no SQL, no host, no parameters.
* No model reasoning, no chain of thought. Observable decisions and state only.

### Retention

`TRACE_RETENTION_DAYS`, default **14**, enforced by `trace.purge_expired()` on
the same sweep that reclaims idle calls - no timer to supervise. There is no
unlimited setting: `_positive_int` refuses zero.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| TR-001 | A happy-path balance call reads end to end | PROTECTED | PENDING | `test_a_happy_path_balance_call_reads_end_to_end` | HIGH |
| TR-002 | The Phase 6.11.1 path is visible in one line | PROTECTED | PENDING | `test_the_turn_not_classified_path_is_visible` | HIGH |
| TR-003 | A cross-customer refusal names itself, and leaks no other id | PROTECTED | PENDING | `test_a_cross_customer_refusal_names_itself` | HIGH |
| TR-004 | An authentication retry shows both attempts | PROTECTED | PENDING | `test_an_authentication_retry_shows_both_attempts` | MED |
| TR-005 | Session exhaustion and lockout are distinguishable | PROTECTED | PENDING | `test_session_exhaustion_and_lockout_are_distinguishable` | HIGH |
| TR-006 | An unsupported request is recorded as refused | PROTECTED | PENDING | `test_an_unsupported_banking_request_is_recorded_as_refused` | MED |
| TR-007 | Every ending reaches the trace, exactly once | PROTECTED | PENDING | `test_every_ending_reaches_the_trace` | HIGH |
| TR-008 | A wordless turn does not invent an utterance | PROTECTED | PENDING | `test_a_wordless_turn_does_not_invent_an_utterance` | HIGH |
| TR-009 | A tool failure records its reason | PROTECTED | PENDING | `test_a_tool_failure_records_its_reason` | MED |
| TR-010 | The trace does not inflate the existing counters | PROTECTED | PENDING | `test_the_trace_does_not_inflate_the_existing_counters` | HIGH |
| TR-011 | A repeated event representation is traced once | PROTECTED | PENDING | `test_a_repeated_event_representation_is_traced_once` | HIGH |
| TR-012 | The ending cannot be written twice | PROTECTED | PENDING | `test_the_ending_cannot_be_written_twice` | HIGH |
| TR-013 | A trace outage does not break the call | PROTECTED | PENDING | `test_a_trace_outage_does_not_break_the_call` | HIGH |
| TR-014 | Tracing can be turned off entirely | PROTECTED | PENDING | `test_tracing_can_be_turned_off_entirely` | MED |
| TR-015 | Six sensitive shapes never reach rows, API or logs | PROTECTED | PENDING | `test_a_sensitive_utterance_never_reaches_the_trace` | HIGH |
| TR-016 | A spoken PIN is never stored even as a tool argument | PROTECTED | PENDING | `test_a_spoken_pin_is_never_stored_even_as_a_tool_argument` | HIGH |
| TR-017 | A database failure is a reason, not a statement | PROTECTED | PENDING | `test_a_database_failure_reaches_the_trace_as_a_reason_not_a_statement` | HIGH |
| TR-018 | By default no spoken word is stored at all (Q-121) | PROTECTED | PENDING | `test_by_default_no_spoken_word_is_stored_at_all` | HIGH |
| TR-019 | The balance itself is never stored | PROTECTED | PENDING | `test_the_balance_itself_is_never_stored` | HIGH |
| TR-020 | The endpoint replays one call in order | PROTECTED | PENDING | `test_the_endpoint_replays_one_call_in_order` | MED |
| TR-021 | Expired traces are purged | PROTECTED | PENDING | `test_expired_traces_are_purged` | MED |
| TR-022 | Retention is bounded by configuration | PROTECTED | PENDING | `test_retention_is_bounded_by_configuration` | MED |

Every Q-600 row is `PENDING` live, and the first live attempt does not change
that: the call made against `6a1d1af` could not have proved trace replay,
because `call_trace_events` did not exist on that deployment (see D-6). The
trace has still never been read during a UAT.

---

## Q-700 - the schema a deployment ships with (Phase 6.12.1)

### D-6 - production startup never created the schema it declared

`6a1d1af` was deployed. The first request for a call trace answered **HTTP
500**:

```
GET /api/telephony/calls/<provider_call_id>/trace
psycopg.errors.UndefinedTable: relation "call_trace_events" does not exist
```

Nothing was wrong with the trace. `create_tables()` - the canonical additive
initialiser, which knows perfectly well how to create that table - was
reachable from exactly one place, `seed_database()`, and a deployment runs that
at most once, long before the table was declared. The FastAPI lifespan did no
schema work at all: it reconciled stale sessions and yielded.

So the defect is not about tracing. **Any release that declares a new table
ships code that queries a database which has never been told the table exists.**
Phase 6.12 was simply the first release to do it.

**Two things it exposed, both fixed generally and neither naming a table:**

* `app.database.seed.ensure_schema()` - the production entry point, awaited in
  the lifespan before startup completes and before any request can be served.
  It calls `create_tables()` and nothing else, so no DDL is written twice, and
  it runs once per process rather than once per application.
* `_add_declared_indexes()` - `create_all` builds a table's indexes only when
  it builds the table, so an index declared on a table that already exists was
  never created either. The same defect, one level down. The index objects are
  asked to create themselves; nothing repeats a `CREATE INDEX` the model has
  already written.

**Failure policy.** A schema failure does not stop the process. This
application deliberately starts with PostgreSQL offline - importing it opens no
connection, and `/health` answers - and killing startup would trade a database
blip for a crash loop that fixes neither an unreachable database nor a bad
migration. The failure is logged with the exception *type* only (a connection
error renders the connection string, password included) and `/ready` reports
it. Liveness stays up; readiness goes red; traffic stops arriving.

**Readiness was also wrong, and is now right.** It checked `SELECT 1` and
nothing else, so it reported this process ready for the entire life of the
defect: the database was reachable, and the table the application needed was
absent. It now verifies that every declared table exists, and reports a count
rather than names.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| SS-001 | Startup creates a newly declared table (fail-before-fix) | PROTECTED | - | `test_startup_creates_a_newly_declared_table` | HIGH |
| SS-002 | Startup creates every declared table, not just the new one | PROTECTED | PENDING | `test_startup_creates_every_declared_table_not_just_the_new_one` | HIGH |
| SS-003 | Startup does not drop or rewrite existing data | PROTECTED | PENDING | `test_startup_does_not_drop_or_rewrite_existing_data` | HIGH |
| SS-004 | Repeated startups are safe | PROTECTED | PENDING | `test_repeated_startups_are_safe` | HIGH |
| SS-005 | The initialiser is idempotent | PROTECTED | PENDING | `test_the_initialiser_is_idempotent` | MED |
| SS-006 | The schema is verified once per process | PROTECTED | PENDING | `test_the_schema_is_verified_once_per_process` | MED |
| SS-007 | A failed verification is retried, not remembered | PROTECTED | PENDING | `test_a_failed_verification_is_retried_rather_than_remembered` | HIGH |
| SS-008 | Missing declared indexes are restored by startup | PROTECTED | PENDING | `test_missing_indexes_are_restored_by_startup` | HIGH |
| SS-009 | Readiness reports a missing table | PROTECTED | PENDING | `test_readiness_reports_a_missing_table` | HIGH |
| SS-010 | Readiness reports a failed startup initialisation | PROTECTED | PENDING | `test_readiness_reports_a_failed_startup_initialisation` | HIGH |
| SS-011 | A schema failure does not stop the process starting | PROTECTED | PENDING | `test_a_schema_failure_does_not_stop_the_process_starting` | HIGH |
| SS-012 | No connection detail reaches the startup log | PROTECTED | PENDING | `test_no_connection_detail_reaches_the_startup_log` | HIGH |
| SS-013 | A trace can be written and replayed after startup | PROTECTED | PENDING | `test_a_trace_can_be_written_and_replayed_after_startup` | HIGH |

---

## Q-800 - the first live functional pass, and what the trace read like

### LIVE FUNCTIONAL PASS

| Field | Observed |
|---|---|
| `provider_call_id` | `62cdd16f-1c66-1240-4790-eaa5afddeeef` |
| authentication | **VERIFIED**, DEMO001 |
| `get_account_balance` | **OK**, 11 ms |
| spoken balance | **$12,450.75** - confirmed correct against the seeded value |
| ending | **CALLER_GOODBYE** |
| trace replay | **operational** |
| `TURN_NOT_CLASSIFIED` | none |

This is the first live call to exercise the whole chain end to end after
Phase 6.11 (ordering), 6.11.1 (timing), 6.12 (tracing) and 6.12.1 (schema).

**The banking result is a pass and stays a pass.** Everything below is about
how the call *reads afterwards*. None of it touched a banking tool, the
authentication logic, the prompts, the model, the session semantics, or the
media path - and the qualification proves it.

Live status for the balance flow therefore moves:

| Baseline | Live outcome |
|---|---|
| `818343d` | PROVEN |
| `67c3c71` | FAILED - `OUT_OF_SCOPE`, 0 ms |
| `ac2343f` | LIVE INTERMITTENT - `TURN_NOT_CLASSIFIED`, ~4 s |
| `6a1d1af` | trace replay HTTP 500 - table absent (D-6) |
| `741dc17` + this | **LIVE FUNCTIONAL PASS** - balance answered correctly, trace replay operational |

### D-7 - the trace did not read like the call

Three presentation defects, none of them banking:

* **One sentence, three rows.** The model streams a reply in growing pieces,
  and `_on_history_snapshot` is keyed on the *text* so that a transcript filled
  in later counts as new - which it must, or a goodbye would never be
  recognised. The trace inherited that and stored "Let me check", then "Let me
  check that for your", then the finished sentence. Fixed by holding the turn
  being spoken and writing it once, complete, when the model stops generating
  (`bridge._note_agent_text` / `_flush_agent_turn`).
* **Credentials read as refusals.** The four digits a caller reads out when the
  bank asks for a PIN are ruled `NON_BANKING_REQUEST`. That is true - a PIN is
  not a banking enquiry - and it reads like a refusal of something nobody
  asked for.
* **Goodbyes read as banking refusals.** "No, that is all, thank you" is more
  than one courtesy phrase, so `_is_social` misses it and it lands in a
  refusal category.

The last two are fixed by *describing* the turn as well as ruling it
(`trace.describe_turn`). The gate's own answer is still recorded in
`scope_category` and `scope_allowed`, untouched - Phase 6.11 was diagnosed by
reading exactly those two fields, and a trace that hid them would have hidden
the defect. Alongside them the trace now says what the turn actually was:
`auth_input` / `CUSTOMER_ID_INPUT`, `auth_input` / `PIN_INPUT`, `closing` /
`END_CALL`, `social` / `GREETING` or `THANKS`.

**`classify_scope` is not changed.** The gate decides what it always decided;
only the description alongside it is new.

### The session-summary decision

`agent_sessions.current_domain` and `last_intent` show `GENERAL/SCOPE` and a
refusal category after a successful call, because they are deliberately the
**last turn** and the last turn is the goodbye. That is intended: the mapping's
own docstring says an operator "should see that a turn was turned away, not
that a loan was discussed", and `test_dashboard.py` pins the exact values.

**Kept, and documented.** Rewriting proven session state so a dashboard reads
tidier would falsify a record. The outcome is instead **derived from the
trace** - `trace._summarise`, returned as `summary` on the replay - which
reports whether the caller was verified, how many banking enquiries were
answered and refused, which operations succeeded, any refusal reasons, the turn
count and how the call ended. It claims nothing the events do not show.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| TN-101 | Streaming chunks collapse to one final utterance | PROTECTED | PENDING | `test_streaming_agent_chunks_collapse_to_one_final_utterance` | HIGH |
| TN-102 | A new turn flushes the previous one | PROTECTED | PENDING | `test_a_new_turn_flushes_the_previous_one` | HIGH |
| TN-103 | An out-of-order snapshot cannot truncate a turn | PROTECTED | PENDING | `test_an_out_of_order_snapshot_cannot_truncate_the_turn` | MED |
| TN-104 | Flushing twice writes one row | PROTECTED | PENDING | `test_flushing_twice_writes_one_row` | HIGH |
| TN-105 | The greeting cue is never traced as speech | PROTECTED | PENDING | `test_the_greeting_cue_is_never_traced_as_speech` | HIGH |
| TN-106 | A customer id turn is an auth input | PROTECTED | PENDING | `test_a_customer_id_turn_is_recorded_as_an_auth_input` | MED |
| TN-107 | A PIN turn is an auth input and stays redacted | PROTECTED | PENDING | `test_a_pin_turn_is_recorded_as_an_auth_input_and_stays_redacted` | HIGH |
| TN-108 | The gate ruling is still recorded beside the description | PROTECTED | PENDING | `test_the_gate_ruling_is_still_recorded_beside_the_description` | HIGH |
| TN-109 | A banking question is not mistaken for an auth input | PROTECTED | PENDING | `test_a_banking_question_is_not_mistaken_for_an_auth_input` | HIGH |
| TN-110 | A closing turn is not recorded as unsupported banking | PROTECTED | PENDING | `test_a_closing_turn_is_not_recorded_as_unsupported_banking` | MED |
| TN-111 | Social turns say which courtesy they were | PROTECTED | PENDING | `test_social_turns_say_which_courtesy_they_were` | LOW |
| TN-112 | Order, tools and counters unchanged | PROTECTED | PENDING | `test_normalisation_leaves_order_tools_and_counters_alone` | HIGH |
| TN-113 | The call summary is derived, not invented | PROTECTED | PENDING | `test_the_call_summary_is_derived_not_invented` | MED |
| TN-114 | The summary reports a refusal without hiding it | PROTECTED | PENDING | `test_the_summary_reports_a_refusal_without_hiding_it` | HIGH |

---

## Q-900 - a credential is what the bank is waiting for (Phase 6.12.3)

### The live call

`7d67837b-1c7f-1240-4790-eaa5afddeeef`

| Aspect | Status |
|---|---|
| **Banking** | **LIVE FUNCTIONAL PASS** - DEMO001 verified, `get_account_balance` OK in 11 ms, SGD 12,450.75 spoken correctly, `CALLER_GOODBYE`, no `TURN_NOT_CLASSIFIED` |
| **Trace normalization** | **LIVE FAILED** |

The banking pass stands. The trace did not.

Trace failures observed:

* the caller's **PIN was persisted in clear**
* the PIN turn was labelled `SOCIAL`
* auth events were ordered backwards - tool, transition, then the words
* assistant partials were still duplicated
* the closing sentence was truncated to "Thank you for calling ABC"

### D-8 - the PIN was stored because redaction read the words

The trace stored:

```
customer utterance: "فور ایٹ ٹو ون"      (four eight two one, Urdu script)
submit_pin        : OK, on the same turn
```

The authentication path understood it perfectly. `looks_like_pin` did not: it
knows Latin digits and English number words, saw neither, and
`redact_transcript` returned the line verbatim. The bank knew what had just
been said; the redaction layer did not.

**No list of number words fixes this.** A PIN can arrive in any language, any
script, mis-transcribed, or as digits. The only thing that reliably identifies
one is that *the bank asked for it and has not had it yet*, so that is what
`trace.expected_credential` asks. `candidate_customer_id` is set by a
successful `submit_customer_id` and the caller is not yet authenticated: that
window is exactly "waiting for a PIN", and every utterance in it is replaced
whole. A caller who says something else there is over-redacted, which is the
right direction to be wrong in - with one exception, a turn the classifier
recognises as a supported banking enquiry, because four digits cannot become a
balance question and a caller who asks one mid-verification should still be
readable.

**The same defect had a second form, and a naive fix reintroduces it.** The
pump rules the turn, the model calls `submit_pin`, the PIN is accepted, and
only *then* does the write reach the database. Asking "is a credential
expected?" at write time answers **no** - the PIN has just been accepted - and
the words go in clear. `trace.reserve_turn` freezes the answer at the moment
the turn is ruled, which is the only moment it is true. That is also what fixes
the ordering: the replay position is reserved there too, so the caller's words
keep their place ahead of the tool they caused.

### D-9 - generation end is not a finished sentence

Phase 6.12.2 flushed the assistant turn when the model stopped generating. The
transcript is still being filled in at that point, so partials survived and the
goodbye was cut to "Thank you for calling ABC".

The provider already says when a message is finished: the SDK carries
`RealtimeMessageItem.status`, set to `completed` from
`response.output_item.done`. The trace now writes on that, with generation end
demoted to a backstop for a transport that never reports a status.

The raw gate fields (`scope_category`, `scope_allowed`) are unchanged and still
record the original ruling.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| CR-001 | A PIN is redacted in ten scripts and spellings | PROTECTED | PENDING | `test_a_pin_is_redacted_whatever_language_it_arrives_in` | HIGH |
| CR-002 | The credential rule reads state, not words | PROTECTED | PENDING | `test_the_credential_rule_reads_state_not_words` | HIGH |
| CR-003 | A PIN written down after acceptance is still redacted | PROTECTED | PENDING | `test_a_pin_written_down_after_it_was_accepted_is_still_redacted` | HIGH |
| CR-004 | A banking question mid-verification is not swallowed | PROTECTED | PENDING | `test_a_banking_question_asked_mid_verification_is_not_swallowed` | MED |
| CR-005 | A customer id turn is masked from state too | PROTECTED | PENDING | `test_a_customer_id_turn_is_masked_from_state_too` | MED |
| CR-006 | The turn keeps its place when the tool runs first | PROTECTED | PENDING | `test_the_turn_keeps_its_place_when_the_tool_runs_first` | HIGH |
| CR-007 | Auth turn, then tool, then transition | PROTECTED | PENDING | `test_an_auth_turn_is_recorded_before_the_tool_it_causes` | HIGH |
| CR-008 | Replay order stays strictly increasing | PROTECTED | PENDING | `test_replay_order_is_still_strictly_increasing` | MED |
| CR-009 | A completed message is written when complete | PROTECTED | PENDING | `test_a_completed_message_is_written_as_soon_as_it_is_complete` | HIGH |
| CR-010 | The closing sentence is never truncated | PROTECTED | PENDING | `test_the_closing_sentence_is_never_truncated` | HIGH |
| CR-011 | Generation end never writes an unfinished message | PROTECTED | PENDING | `test_generation_end_does_not_write_an_unfinished_message` | HIGH |
| CR-012 | A message with no status is still written | PROTECTED | PENDING | `test_a_message_that_never_reports_a_status_is_still_written` | MED |
| CR-013 | Two completed responses are two rows | PROTECTED | PENDING | `test_two_completed_responses_are_two_rows_and_no_more` | MED |

---

## Q-1000 - a gate that cannot read a turn is not agreeing to it (Phase 6.13)

### The live call

`0989e07a-1c91-1240-4790-eaa5afddeeef`

| Aspect | Status |
|---|---|
| **Banking** | **LIVE PASS** - DEMO001 verified, savings balance answered, clean goodbye |
| **Credential privacy** | **LIVE PASS** - the PIN turn stored as `[PIN REDACTED]`, D-8 holding |
| **Auth ordering** | **LIVE PASS** - caller turn, then the tool it caused, then the transition; D-8's ordering half holding |
| **Trace finalization** | **LIVE FAILED** - see D-11 |
| **Non-Latin scope** | **LIVE FAILED** - see D-10 |

Three of the five hold live, and the two that Phase 6.12.3 shipped are among
them. The banking result is a pass and stays a pass: neither defect below
touched a banking tool, an authentication decision, or the media path.

### D-10 - four digits in Urdu were ruled a pleasantry

The same PIN turn that was correctly masked, correctly labelled `PIN_INPUT` and
correctly ordered carried these raw gate fields:

```
scope_category = SOCIAL
scope_allowed  = true
```

`scope._normalize` keeps `[a-z0-9]`, so an utterance in any script but Latin
comes back as whitespace. `_is_social` then opened with

```python
tokens = [word for word in padded.split() if word not in _COURTESY_FILLER]
if not tokens:
    return True
```

a branch written for speech that is *entirely* courtesy filler - "well,
please" - which also caught "nothing survived normalisation at all". SOCIAL is
one of the five in-scope categories, so the gate admitted a turn it had not
read. Punctuation-only, whitespace-only and empty input took the same path.

**No customer data was exposed.** Authentication and ownership are enforced in
the tool layer; SOCIAL authorises no lookup. The defect is that the gate said
yes for a reason that was not true.

**Fixed language-independently, in one condition**: `_is_social` returns False
when nothing survived normalisation. Something was said and none of it was
read, so there are no grounds to call it courtesy; it falls through to
`NON_BANKING_REQUEST`, which is already where a PIN read out in English lands.
Filler-only speech is untouched - "well, please" survives `_normalize` and is
emptied a line later by `_COURTESY_FILLER`, which is the case that branch was
written for.

**No script list, no language list, no number words.** A list would have had to
be right about the next language too. `IN_SCOPE` is unchanged and no permission
was widened - the change can only move a turn *out* of scope.

A known and accepted limit: an utterance that is part Latin courtesy and part
unreadable ("Hello" + four digits in another script) still reads as SOCIAL,
because the greeting genuinely survived. SOCIAL authorises nothing, so this
admits no data; ruling on partial loss would mean guessing how much loss is too
much, and there is no non-arbitrary answer. NL-013 pins that it must never
become a banking category.

### D-11 - a finished turn was not finished

The trace stored the goodbye three times:

```
1. "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
2. "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodb"
3. "Welcome to ABC Demo Bank. Thank you for calling. How may I assist ..."
```

One root cause behind all three: **completion was not terminal.**
`_flush_agent_turn` emptied the buffer and remembered nothing, so a later
snapshot of an item that had already been written simply re-created it - and
the "longest text wins" guard only ever applied *within* one held entry, so a
shorter, later snapshot won by default. Snapshots carry the whole conversation,
which is why row 3 is the greeting from the top of the call arriving after the
goodbye.

The bridge now records which item ids it has written and ignores them
afterwards. `incomplete` joins `completed` as a finishing status: a response the
caller talked over stopped mid-sentence, and a turn that stopped is as finished
as one that ended.

**A third defect surfaced while reproducing this one.** `close()` called
`_flush_agent_turn`, which schedules onto `self._transitions`, and then stopped
exactly that list three lines later - so the last sentence of a turn the
provider never finished was written to a task that was immediately cancelled.
Not a duplicate: a silent loss, and invisible on `0989e07a-...` because that
call's goodbye did complete. Teardown now awaits its own record.

### On the fail-before-fix evidence

The first two lifecycle tests written for D-11 passed against `038cb9a`, which
would have made them worthless as proof. The reason was the cancellation above:
they drove the duplicate through `close()`, whose write never landed, so the
duplicate could not appear. Rewritten to drive it the way the live call did -
a status-less snapshot followed by generation end - they failed as they should,
7 of 13 rather than 3. The bug they now describe is the bug that happened.

| ID | Scenario | Det. | Live | Test | Crit. |
|---|---|---|---|---|---|
| NL-001 | Ten unreadable scripts are not courtesy, verified and unverified | PROTECTED | PENDING | `test_speech_that_does_not_survive_normalisation_is_not_courtesy` | HIGH |
| NL-002 | Unreadable speech lands in the existing refusal category | PROTECTED | PENDING | `test_unreadable_speech_lands_in_the_existing_refusal_category` | HIGH |
| NL-003 | The set of in-scope categories is unchanged | PROTECTED | PENDING | `test_the_set_of_in_scope_categories_is_unchanged` | HIGH |
| NL-004 | Real courtesy is still courtesy | PROTECTED | PENDING | `test_real_courtesy_is_still_courtesy` | HIGH |
| NL-005 | Supported banking is still supported | PROTECTED | PENDING | `test_supported_banking_is_still_supported` | HIGH |
| NL-006 | An identified caller is still an authentication turn | PROTECTED | PENDING | `test_an_identified_caller_is_still_an_authentication_turn` | HIGH |
| NL-007 | Another customer is still refused | PROTECTED | PENDING | `test_another_customer_is_still_refused` | HIGH |
| NL-008 | An attack is still an attack | PROTECTED | PENDING | `test_an_attack_is_still_an_attack` | HIGH |
| NL-009 | Genuinely empty input is still refused | PROTECTED | PENDING | `test_genuinely_empty_input_is_still_refused` | MED |
| NL-010 | Latin mixed with unreadable text still reads the Latin | PROTECTED | PENDING | `test_latin_text_mixed_with_unreadable_text_still_reads_the_latin` | HIGH |
| NL-013 | A greeting beside unreadable text is not admitted blindly | PROTECTED | PENDING | `test_a_greeting_with_unreadable_text_beside_it_is_not_admitted_blindly` | MED |
| AF-001 | One message streamed in pieces is one row | PROTECTED | PENDING | `test_one_message_streamed_in_pieces_is_one_row` | HIGH |
| AF-002 | Two messages in sequence are two rows | PROTECTED | PENDING | `test_two_messages_in_sequence_are_two_rows` | HIGH |
| AF-003 | A completed item is not rewritten by generation end | PROTECTED | PENDING | `test_a_completed_item_is_not_written_again_by_generation_end` | HIGH |
| AF-004 | A completed item is not rewritten at teardown | PROTECTED | PENDING | `test_a_completed_item_is_not_written_again_by_teardown` | HIGH |
| AF-005 | A completed goodbye survives generation end intact | PROTECTED | PENDING | `test_a_completed_goodbye_survives_generation_end_intact` | HIGH |
| AF-006 | Live row 2: a truncated snapshot after completion is ignored | PROTECTED | PENDING | `test_a_truncated_snapshot_after_completion_is_ignored` | HIGH |
| AF-007 | Live row 3: a stale greeting is never replayed | PROTECTED | PENDING | `test_a_stale_greeting_is_never_replayed` | HIGH |
| AF-008 | Only the unfinished item is owed at the end | PROTECTED | PENDING | `test_only_the_current_item_is_flushed_at_teardown` | HIGH |
| AF-009 | A transport that never reports completion still writes once | PROTECTED | PENDING | `test_a_transport_that_never_reports_completion_still_writes_once` | MED |
| AF-010 | A completion after the fallback wrote it is ignored | PROTECTED | PENDING | `test_a_completion_arriving_after_the_fallback_wrote_it_is_ignored` | HIGH |
| AF-011 | Many responses are each stored once | PROTECTED | PENDING | `test_many_responses_with_distinct_ids_are_each_stored_once` | MED |
| AF-012 | An interrupted response is written once and not replayed | PROTECTED | PENDING | `test_an_interrupted_response_is_written_once_and_not_replayed` | HIGH |
| AF-013 | The longest text still wins before the turn is written | PROTECTED | PENDING | `test_the_longest_text_still_wins_before_the_turn_is_written` | MED |

---

## Open defects

| ID | Defect | Evidence | Status |
|---|---|---|---|
| **D-1** | Announced termination did not terminate. | fixed by `bridge._close_if_authentication_is_over`; guarded by `test_d1_*` | **CLOSED** |
| **D-2** | A persistently-locked caller had `session.authentication_locked == False`. | fixed in `verify_pin`; guarded by CT-D03, CT-106, CT-107 | **CLOSED** |
| **D-3** | A verified DEMO001 was refused their own savings balance, intermittently, decided by which tool the model called first. Live calls `26faddf0-...` and `730747e1-...` on `67c3c71`: `get_account_balance` FAILED, `duration_ms = 0`, no database lookup reached. | fixed by `turn_gate._hold_unverified_enquiry`; guarded by BD-001 to BD-022 | **CLOSED - the ordering defect did not recur live on `ac2343f`; see D-5 for the timing defect underneath it** |
| **D-4** | A hang-up could cancel its own cleanup: `tear_down` aborted after the bridge left `phone_call_registry` and before `voice_call_manager.close()`, stranding an open provider session and its capacity slot with no path to reclaim either - on `REALTIME_MAX_ACTIVE_SESSIONS = 1`, one dropped socket from refusing every later caller. | traced in a failing run (`tear_down RAISED CancelledError`); fixed by `service._uninterruptible` and `service.end_media_call`; guarded by TD-001 to TD-005 | **CLOSED deterministically - PENDING live re-proof** |
| **D-5** | A caller turn that never transcribed left the scope gate open for the rest of the call, so a verified DEMO001 with a held Savings enquiry was refused `TURN_NOT_CLASSIFIED` after the full 4.0 s wait. The gate was opened by a VAD onset and closed only by a transcript with words in it - not the same guarantee. | live call `3860a81f-1bc5-1240-4790-eaa5afddeeef` (FAILED twice, ~4 s each); reproduced offline at 4061 ms; fixed by `turn_gate.resumes_held_enquiry` and `turn_gate.record_unintelligible_turn`; guarded by TN-001 to TN-017 | **CLOSED deterministically - PENDING live re-proof** |
| **D-6** | Production startup never invoked the canonical additive schema initialiser, so a release that declared a new table shipped code querying a database that had never been told it existed. `call_trace_events` was absent after deploying `6a1d1af` and trace replay answered HTTP 500. Readiness reported the process ready throughout. | live: `UndefinedTable: relation "call_trace_events" does not exist`; fixed by `seed.ensure_schema()` in the lifespan plus `_add_declared_indexes`; readiness now verifies declared tables; guarded by SS-001 to SS-013 | **CLOSED deterministically - PENDING live re-proof** |
| **D-7** | The trace did not read like the call: a streamed reply was stored as three growing rows, PIN and customer-id turns were labelled `NON_BANKING_REQUEST`, and multi-phrase goodbyes landed in a banking refusal category. Presentation only - the banking result on `62cdd16f-...` was correct throughout. | fixed by `bridge._note_agent_text` / `_flush_agent_turn` and `trace.describe_turn`; the gate's own ruling is still recorded; guarded by TN-101 to TN-114 | **CLOSED deterministically - PENDING live re-proof** |
| **D-8** | The caller's PIN was persisted in clear. Transcribed into Urdu script, it matched no number-word pattern, so `redact_transcript` returned it verbatim - while `submit_pin` succeeded on the same turn. **HIGH privacy defect.** | live `7d67837b-...`; fixed by `trace.expected_credential` (state, not words) and `trace.reserve_turn` (frozen at ruling time, before the PIN is accepted); guarded by CR-001 to CR-008 | **CLOSED deterministically - PENDING live re-proof** |
| **D-9** | Assistant partials were still duplicated and the closing sentence truncated to "Thank you for calling ABC": generation end is not a finished sentence. | live `7d67837b-...`; fixed by writing on the provider's own `RealtimeMessageItem.status == completed`; guarded by CR-009 to CR-013 | **CLOSED deterministically - PENDING live re-proof** |
| **D-10** | A spoken PIN transcribed into Urdu was ruled `scope_category = SOCIAL`, `scope_allowed = true`. `_normalize` keeps `[a-z0-9]`, so any non-Latin script empties, and `_is_social` returned True for an emptied utterance - a branch meant for filler-only speech. The same held for punctuation-only and empty input. No data was exposed (SOCIAL authorises no lookup), but the gate agreed to a turn it had not read. | live `0989e07a-...`; fixed by one condition in `scope._is_social` - nothing survived normalisation, so it is not courtesy - with no script or language list; `IN_SCOPE` unchanged; guarded by NL-001 to NL-013 | **CLOSED deterministically - PENDING live re-proof** |
| **D-11** | The trace stored the goodbye, a truncated copy of it, and then the greeting from the top of the call. Completion was not terminal: `_flush_agent_turn` emptied the buffer and remembered nothing, so a later snapshot of an already-written item re-created it, and teardown wrote it again. Found alongside it: `close()` scheduled its final record onto the task list it then cancelled, silently losing an unfinished last sentence. | live `0989e07a-...`; fixed by `bridge._agent_written` (an item written is done with), `incomplete` counted as finished, and an awaited teardown record; guarded by AF-001 to AF-013 | **CLOSED deterministically - PENDING live re-proof** |

## Open decision

**The per-call attempt limit.** `MAX_AUTHENTICATION_ATTEMPTS = 3`; the Phase
6.10 brief assumes 5. Raising it is a security-policy change, not a defect fix.
CT-007–011 are written against the code's actual limit so that no test hard-codes
a decision nobody has made. **Awaiting explicit direction.**