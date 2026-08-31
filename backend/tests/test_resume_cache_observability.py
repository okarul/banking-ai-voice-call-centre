"""Phase 7.3: what an operator sees when a held enquiry is answered once.

B1 answers the enquiry a caller made before they were verified, deterministically,
the moment the PIN checks out. The model then usually calls the same banking tool
itself - and that call is served from the answer already read, because asking the
bank twice for one enquiry is the defect this phase exists to remove.

That left a hole. `_dispatch` returned the cached answer *before*
`record_tool_outcome`, so the model emitted a banking tool call for which no
event existed anywhere. An invocation with no event against it is precisely the
shape that made the original live failure slow to place, so it is now recorded -
but recorded as what it is.

The design is a split between two surfaces:

* **counters** - `agent_tool_events` and `agent_sessions.tool_call_count` -
  count what the bank was actually asked to do. A cache-served follow-up asked
  it nothing and is absent from both. `test_the_trace_does_not_inflate_the_
  existing_counters` has always held one invocation to one row and one count,
  and an enquiry answered once must never reach an operations board as two.

* **the trace** - `call_trace_events` - tells the story, and carries the
  follow-up as a further event whose `tool_status` is `CACHE_SERVED`.
  `trace._summarise` keeps it out of `answered` and `operations`, which are how
  many times the bank was read and which reads they were, and counts it in its
  own `cache_served`.

So the counters stay conservative, the story stays complete, and neither has to
lie to keep the other honest.

All customers and PINs here are synthetic.
"""

import pytest
from sqlalchemy import select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent
from app.observability import business, trace
from app.realtime import tools as realtime_tools

from tests.test_call_trace import (  # noqa: F401  - fixtures are used by name
    ASK_SAVINGS,
    CALLER,
    HEARD_ID,
    HEARD_PIN,
    OTHER,
    PINS,
    Call,
    clean_tables,
    stored_blob,
    traced,
)

BALANCE = "get_account_balance"


@pytest.fixture
def executions(monkeypatch):
    """Counts what actually reached the banking tools, per session.

    Measured at `_run_tool`, the one door every registered tool goes through,
    so a cache hit - which never gets there - cannot be miscounted as a read of
    the bank. The real tool still runs; this only watches.
    """
    original = realtime_tools._run_tool
    seen: list[tuple[str, str]] = []

    def counting(tool_name, session_id, arguments, manager):
        seen.append((session_id, tool_name))
        return original(tool_name, session_id, arguments, manager)

    monkeypatch.setattr(realtime_tools, "_run_tool", counting)

    class Counter:
        def of(self, session_id, tool_name):
            return sum(1 for s, t in seen if s == session_id and t == tool_name)

    return Counter()


def tool_rows(call, tool_name=BALANCE):
    """The counter surface's rows for one call."""
    with session_scope() as db:
        row = db.scalars(
            select(AgentSession).where(AgentSession.provider_call_id == call.call_id)
        ).one()
        events = list(
            db.scalars(
                select(AgentToolEvent).where(AgentToolEvent.session_pk == row.id)
            )
        )
    return row, [e for e in events if e.tool_name == tool_name]


def held_then_verified(call):
    """The live shape: enquiry first, identity second, PIN third."""
    call.says(ASK_SAVINGS)
    call.verify()


# === OBS-001 ================================================================


@pytest.mark.trace
def test_the_resume_is_exactly_one_real_business_execution(traced, executions):
    """OBS-001. One enquiry, one read of the bank, one execution event."""
    call = Call()
    try:
        held_then_verified(call)

        assert executions.of(call.session_id, BALANCE) == 1

        traced_balance = call.tools_named(BALANCE)
        assert len(traced_balance) == 1
        assert traced_balance[0]["tool_status"] == "OK"

        _row, rows = tool_rows(call)
        assert len(rows) == 1
        assert rows[0].status == "OK"
    finally:
        call.close()


# === OBS-002 ================================================================


@pytest.mark.trace
def test_the_follow_up_is_visible_and_says_it_was_served_from_the_answer(traced):
    """OBS-002. The invocation that had no event now has one, and it is honest."""
    call = Call()
    try:
        held_then_verified(call)
        call.tool(BALANCE, account_type="Savings")

        balance = call.tools_named(BALANCE)
        assert [e["tool_status"] for e in balance] == ["OK", trace.CACHE_SERVED]

        served = balance[1]
        assert served["tool_name"] == BALANCE
        assert served["auth_status"] == "VERIFIED"
        assert served["customer_ref"] == CALLER
        assert served["duration_ms"] == 0, "nothing was waited for"
        assert served.get("failure_reason") is None
    finally:
        call.close()


# === OBS-003 ================================================================


@pytest.mark.trace
def test_the_follow_up_never_reaches_the_bank(traced, executions):
    """OBS-003. Observability must not have reintroduced the second execution."""
    call = Call()
    try:
        held_then_verified(call)
        assert executions.of(call.session_id, BALANCE) == 1

        call.tool(BALANCE, account_type="Savings")

        assert executions.of(call.session_id, BALANCE) == 1, (
            "the bank was asked the same question twice"
        )
    finally:
        call.close()


# === OBS-004 ================================================================


@pytest.mark.trace
def test_the_persisted_record_cannot_read_as_two_executions(traced):
    """OBS-004. The counter surface counts reads of the bank, and there was one."""
    call = Call()
    try:
        held_then_verified(call)
        call.tool(BALANCE, account_type="Savings")
        call.ends()

        row, rows = tool_rows(call)

        # One read of the bank, one row, and nothing labelled cache-served here.
        assert len(rows) == 1
        assert rows[0].status == "OK"
        assert not any(e.status == trace.CACHE_SERVED for e in rows)

        # submit_customer_id, submit_pin, get_account_balance - and not the
        # follow-up, which asked the bank nothing.
        assert row.tool_call_count == 3

        # The trace carries both, and tells them apart.
        assert len(call.tools_named(BALANCE)) == 2
        summary = call.replay()["summary"]
        assert summary["answered"] == 1, "one read of the bank showed as two"
        assert summary["operations"] == [BALANCE]
    finally:
        call.close()


# === OBS-005 ================================================================


@pytest.mark.trace
def test_a_different_account_is_never_served_from_the_answer(traced, executions):
    """OBS-005. A different question is a different question."""
    call = Call()
    try:
        held_then_verified(call)
        assert executions.of(call.session_id, BALANCE) == 1

        call.tool(BALANCE, account_type="Current")

        # It really went to the bank, and nothing about it claims otherwise.
        assert executions.of(call.session_id, BALANCE) == 2
        statuses = [e["tool_status"] for e in call.tools_named(BALANCE)]
        assert trace.CACHE_SERVED not in statuses[1:], (
            "a different account was served the Savings answer"
        )
    finally:
        call.close()


# === OBS-006 ================================================================


@pytest.mark.trace
def test_one_call_never_sees_another_calls_cache_or_events(traced, executions):
    """OBS-006. The answer and its event belong to one session and no other."""
    a = Call()
    b = Call()
    try:
        held_then_verified(a)
        a.tool(BALANCE, account_type="Savings")

        # A second caller, a different customer, asking the same thing.
        b.says(ASK_SAVINGS)
        b.says("My customer ID is DEMO zero zero two.")
        b.tool("submit_customer_id", spoken_customer_id="DEMO zero zero two")
        b.says("Seven three one five.")
        b.tool("submit_pin", spoken_pin="seven three one five")

        assert executions.of(b.session_id, BALANCE) == 1, (
            "the second caller was served the first caller's answer"
        )
        assert [e["tool_status"] for e in b.tools_named(BALANCE)] == ["OK"]

        # Each call's trace carries only its own customer.
        assert {e["customer_ref"] for e in a.tools_named(BALANCE)} == {CALLER}
        assert {e["customer_ref"] for e in b.tools_named(BALANCE)} == {OTHER}
    finally:
        a.close()
        b.close()


# === OBS-007 ================================================================


@pytest.mark.trace
def test_the_cache_served_event_carries_no_credential(traced):
    """OBS-007. Being able to prove a cache hit is not a reason to store more."""
    call = Call()
    try:
        held_then_verified(call)
        call.tool(BALANCE, account_type="Savings")
        call.ends()

        served = call.tools_named(BALANCE)[1]

        # The account selection, and nothing else. No PIN, no customer id, and
        # not the banking result that was replayed.
        assert "Savings" in str(served["tool_arguments"])
        assert "12450.75" not in str(served), "a balance was written into the event"

        blob = stored_blob()
        for secret in (PINS[CALLER], HEARD_PIN, HEARD_ID):
            assert secret.lower() not in blob.lower()
    finally:
        call.close()


# === OBS-008 ================================================================


@pytest.mark.trace
def test_the_trace_endpoint_tells_a_real_read_from_a_replayed_one(traced):
    """OBS-008. An operator can answer "did the bank run twice?" from the replay."""
    call = Call()
    try:
        held_then_verified(call)
        call.tool(BALANCE, account_type="Savings")
        call.ends()

        replay = call.replay()
        statuses = [
            e["tool_status"] for e in replay["events"]
            if e.get("tool_name") == BALANCE
        ]
        assert statuses == ["OK", trace.CACHE_SERVED]
        assert replay["summary"]["cache_served"] == 1
        assert replay["summary"]["answered"] == 1
    finally:
        call.close()


# === OBS-009 ================================================================


@pytest.mark.trace
def test_the_summary_counters_still_mean_what_they_meant(traced):
    """OBS-009. `answered` is reads of the bank; `banking_enquiries` is asks."""
    call = Call()
    try:
        held_then_verified(call)
        call.tool(BALANCE, account_type="Savings")
        call.ends("CALLER_GOODBYE")

        summary = call.replay()["summary"]

        assert summary["verified"] is True
        # Two invocations - a refusal has always counted here too, and a cache
        # hit reaches the bank no more than a refusal does.
        assert summary["banking_enquiries"] == 2
        # But one read of the bank, and one operation.
        assert summary["answered"] == 1
        assert summary["refused"] == 0
        assert summary["cache_served"] == 1
        assert summary["operations"] == [BALANCE]
        assert summary["answered"] + summary["refused"] + summary["cache_served"] == (
            summary["banking_enquiries"]
        )
    finally:
        call.close()


# === OBS-010 ================================================================


@pytest.mark.trace
def test_a_failed_execution_is_never_labelled_cache_served(traced, monkeypatch):
    """OBS-010. An outage must not be written down as an answer."""
    from app.tools import accounts

    def unreachable(*args, **kwargs):
        raise RuntimeError("bank unreachable")

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        monkeypatch.setattr(accounts, "get_accounts_for_customer", unreachable)
        call.verify()

        # The resume failed, so nothing was cached and nothing claims success.
        statuses = [e["tool_status"] for e in call.tools_named(BALANCE)]
        assert statuses and all(s == "FAILED" for s in statuses)
        assert trace.CACHE_SERVED not in statuses

        summary = call.replay()["summary"]
        assert summary["answered"] == 0
        assert summary["cache_served"] == 0
    finally:
        call.close()


@pytest.mark.trace
def test_the_recorder_refuses_to_call_a_failure_cache_served(traced):
    """OBS-010, at the seam itself.

    `served_from_cache` is a claim made by the caller of `record_tool_outcome`.
    Only successes are ever cached, so a failure arriving with that flag would
    mean the invariant had broken - and the status must still say FAILED.
    """
    call = Call()
    try:
        call.verify()
        business.record_tool_outcome(
            call.session_id,
            BALANCE,
            {"success": False, "reason": "DATABASE_UNAVAILABLE"},
            duration_ms=0,
            arguments={"account_type": "Savings"},
            session=call.session,
            served_from_cache=True,
        )

        served = call.tools_named(BALANCE)[-1]
        assert served["tool_status"] == "FAILED"
        assert served["failure_reason"] == "DATABASE_UNAVAILABLE"
    finally:
        call.close()
