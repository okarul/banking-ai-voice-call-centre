# Channel 2 — permanent qualification checklist

A standing project artifact, not a phase document. Every future Channel 1 or
Channel 2 change is measured against it.

**Protected baseline: `818343d`** — the commit deployed to production before Phase 6.10.

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
| Q-113 | `authenticated=true` persists | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-114 | `auth_status=VERIFIED` persists | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-115 | `customer_id=DEMO001` persists | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-116 | Savings balance executes | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `app/tools` | HIGH |
| Q-117 | `get_account_balance` recorded once | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-118 | `submit_customer_id` recorded once | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-119 | `submit_pin` recorded once | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime/tools.py` | MED |
| Q-120 | `tool_call_count` semantics | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | MED |
| Q-121 | Channel 2 `conversation_messages` stays 0 | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | privacy contract | HIGH |
| Q-122 | PIN absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-123 | Balance absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-124 | Customer name absent from logs | PROTECTED | PROVEN | `test_telephony_business_persistence.py` | `redaction.py` | HIGH |
| Q-125 | DEMO002 balance blocked for DEMO001 | PROTECTED | PENDING | `test_customer_trust.py` (planned CT-040) | `turn_gate.py` | HIGH |
| Q-126 | Cross-customer recorded once FAILED | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime/tools.py` | HIGH |
| Q-127 | No DEMO002 data leaked | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `authorization/guards.py` | HIGH |
| Q-128 | Identity unchanged after refusal | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `turn_gate.py` | HIGH |
| Q-129 | Observability failure cannot break an answer | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `observability/business.py` | HIGH |
| Q-130 | Duplicate turn representations → one row | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `realtime_manager.py` | MED |
| Q-131 | Channel 1 single-write persistence | PROTECTED | PENDING | `test_telephony_business_persistence.py` | `routers/call.py` | HIGH |
| Q-132 | Channel 1 behaviour unchanged | PROTECTED | PROVEN | `test_browser_call.py` | `routers/call.py` | HIGH |
| Q-133 | Readiness healthy | PROTECTED | PROVEN | `test_telephony_production.py` | `observability/readiness.py` | MED |
| Q-134 | Health endpoint healthy | PROTECTED | PROVEN | `test_health.py` | `main.py` | MED |

`Live = PENDING` on Q-113…Q-120 and Q-125…Q-131 is deliberate: those
behaviours ship in `818343d`, whose business-persistence changes have not yet
been exercised by a live call. They are deterministically protected and
awaiting the Phase 6.11 UAT.

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

## Open defects

| ID | Defect | Evidence | Status |
|---|---|---|---|
| **D-1** | Announced termination did not terminate. | fixed by `bridge._close_if_authentication_is_over`; guarded by `test_d1_*` | **CLOSED** |
| **D-2** | A persistently-locked caller had `session.authentication_locked == False`. | fixed in `verify_pin`; guarded by CT-D03, CT-106, CT-107 | **CLOSED** |

## Open decision

**The per-call attempt limit.** `MAX_AUTHENTICATION_ATTEMPTS = 3`; the Phase
6.10 brief assumes 5. Raising it is a security-policy change, not a defect fix.
CT-007–011 are written against the code's actual limit so that no test hard-codes
a decision nobody has made. **Awaiting explicit direction.**
