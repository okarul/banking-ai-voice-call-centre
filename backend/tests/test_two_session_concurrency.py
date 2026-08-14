"""Two customers on the phone at once, and nothing of theirs touching.

Everything a call knows about a customer lives on one `Session` object, held in
a dictionary keyed by session id. That is the whole isolation design, and this
file is what proves it holds when two calls are genuinely live at the same time
rather than one after another.

The interesting failures here are not "wrong answer" but "right answer, wrong
caller": B's balance read on A's line, A's loan context resolving B's question,
A hanging up and taking B's call down with it. Each of those gets a test.

Two callers is the whole scope. This is not a load test.

All customers, accounts, loans and PINs are synthetic Phase 2 seed data.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents import handle_turn
from app.auth import authentication
from app.auth.authentication import ALREADY_AUTHENTICATED
from app.main import app
from app.realtime.browser_calls import browser_call_manager
from app.realtime.webrtc import execute_tool
from app.sessions import SessionManager, session_manager

PINS = {"DEMO001": "4821", "DEMO002": "7315"}

# What each caller owns. Nothing from one column may appear on the other's line.
A_ACCOUNT, A_BALANCE, A_LOAN = "XXXX1001", "12450.75", "HL-DEMO001"
B_ACCOUNT, B_BALANCE, B_LOAN = "XXXX1002", "8730.20", "PL-DEMO002"


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    asyncio.run(browser_call_manager.close_all())
    session_manager.clear()


def verified(manager, customer_id):
    """One caller, taken through the real deterministic checks."""
    session = manager.create_session()
    assert authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )["success"]
    assert authentication.verify_pin(
        session.session_id, PINS[customer_id], manager=manager
    )["success"]
    return session


@pytest.fixture
def two_callers(manager):
    """DEMO001 and DEMO002, both verified, both live."""
    return verified(manager, "DEMO001"), verified(manager, "DEMO002")


def tool(name, session_id, arguments=None, *, manager):
    return asyncio.run(execute_tool(name, session_id, arguments or {}, manager=manager))


# === 1-3: two calls, two identities =========================================


def test_two_calls_get_two_distinct_sessions(two_callers):
    a, b = two_callers

    assert a.session_id != b.session_id
    assert a.session_id.startswith("SESSION-")
    assert b.session_id.startswith("SESSION-")


def test_two_browser_calls_get_two_distinct_realtime_associations():
    client = TestClient(app)

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    import app.routers.call as call_router

    original = call_router.mint_client_secret
    call_router.mint_client_secret = fake_mint
    try:
        first = client.post("/api/call/start").json()
        second = client.post("/api/call/start").json()

        assert first["session_id"] != second["session_id"]
        assert first["realtime_session_id"] != second["realtime_session_id"]
        assert browser_call_manager.active_count() == 2

        # Each banking session records its own call, not the other's.
        assert (
            session_manager.get_session(first["session_id"]).realtime_session_id
            == first["realtime_session_id"]
        )
        assert (
            session_manager.get_session(second["session_id"]).realtime_session_id
            == second["realtime_session_id"]
        )
    finally:
        call_router.mint_client_secret = original


# === 4-5: authentication is per call ========================================


def test_each_caller_is_verified_as_themselves(two_callers, manager):
    a, b = two_callers

    assert (a.customer_id, a.authenticated) == ("DEMO001", True)
    assert (b.customer_id, b.authenticated) == ("DEMO002", True)


def test_verifying_one_caller_does_not_verify_the_other(manager):
    a = manager.create_session()
    b = manager.create_session()
    authentication.verify_customer(a.session_id, "DEMO001", manager=manager)
    authentication.verify_customer(b.session_id, "DEMO002", manager=manager)

    authentication.verify_pin(a.session_id, PINS["DEMO001"], manager=manager)

    assert a.authenticated is True
    assert b.authenticated is False
    assert b.customer_id is None


def test_locking_one_caller_out_leaves_the_other_banking(manager):
    a = verified(manager, "DEMO001")
    b = manager.create_session()
    authentication.verify_customer(b.session_id, "DEMO002", manager=manager)

    for _ in range(3):
        authentication.verify_pin(b.session_id, "0000", manager=manager)

    assert b.authentication_locked is True
    assert a.authentication_locked is False
    assert tool("get_account_balance", a.session_id, {"account_type": "Savings"},
                manager=manager)["masked_account"] == A_ACCOUNT


# === 6-9: each caller's own data ============================================


def test_each_caller_reads_only_their_own_account(two_callers, manager):
    a, b = two_callers

    from_a = tool("get_account_balance", a.session_id, {"account_type": "Savings"},
                  manager=manager)
    from_b = tool("get_account_balance", b.session_id, {"account_type": "Savings"},
                  manager=manager)

    assert (from_a["masked_account"], from_a["available_balance"]) == (A_ACCOUNT, A_BALANCE)
    assert (from_b["masked_account"], from_b["available_balance"]) == (B_ACCOUNT, B_BALANCE)
    assert B_BALANCE not in str(from_a)
    assert A_BALANCE not in str(from_b)


def test_each_caller_reads_only_their_own_loan(two_callers, manager):
    a, b = two_callers

    from_a = tool("get_loan_balance", a.session_id, {"loan_type": "Home Loan"},
                  manager=manager)
    from_b = tool("get_loan_balance", b.session_id, {"loan_type": "Personal Loan"},
                  manager=manager)

    assert from_a["loan_reference"] == A_LOAN
    assert from_b["loan_reference"] == B_LOAN
    assert B_LOAN not in str(from_a)
    assert A_LOAN not in str(from_b)


def test_a_caller_cannot_reach_a_loan_type_they_do_not_hold(two_callers, manager):
    """DEMO002 has no home loan. Asking must not fall through to DEMO001's."""
    _a, b = two_callers

    result = tool("get_loan_balance", b.session_id, {"loan_type": "Home Loan"},
                  manager=manager)

    assert result["success"] is False
    assert A_LOAN not in str(result)


# === 10-12: context belongs to the call =====================================


def test_account_and_loan_context_do_not_mix_between_calls(two_callers, manager):
    a, b = two_callers

    # A talks about accounts, B talks about loans, at the same time.
    tool("get_account_balance", a.session_id, {"account_type": "Savings"}, manager=manager)
    tool("get_loan_balance", b.session_id, {"loan_type": "Personal Loan"}, manager=manager)

    live_a = manager.get_session(a.session_id)
    live_b = manager.get_session(b.session_id)

    assert live_a.current_domain == "ACCOUNT"
    assert live_b.current_domain == "LOAN"
    assert live_a.conversation_context.get("account_type") == "Savings"
    assert live_a.conversation_context.get("loan_type") is None
    assert live_b.conversation_context.get("loan_type") == "Personal Loan"
    assert live_b.conversation_context.get("account_type") is None


def test_one_callers_loan_context_never_answers_the_others_question(two_callers, manager):
    """A has discussed no loan. B has. A's elliptical question must not use B's."""
    a, b = two_callers

    tool("get_loan_balance", b.session_id, {"loan_type": "Personal Loan"}, manager=manager)
    tool("get_account_balance", a.session_id, {"account_type": "Savings"}, manager=manager)

    # A asks for an instalment without naming a loan.
    result = tool("get_next_instalment", a.session_id, {"loan_type": None},
                  manager=manager)

    # A holds exactly one loan, so it resolves to A's own — never B's.
    assert result["success"] is True
    assert result["loan_type"] == "Home Loan"
    assert B_LOAN not in str(result)


def test_previous_intent_is_recorded_per_session(manager):
    a = verified(manager, "DEMO001")
    b = verified(manager, "DEMO002")

    handle_turn(a.session_id, "What is my savings account balance?", manager=manager)
    handle_turn(b.session_id, "What is my personal loan balance?", manager=manager)

    live_a = manager.get_session(a.session_id)
    live_b = manager.get_session(b.session_id)

    assert live_a.current_domain == "ACCOUNT"
    assert live_b.current_domain == "LOAN"
    assert live_a.previous_intent != live_b.previous_intent or (
        live_a.previous_intent is None and live_b.previous_intent is None
    )


# === 13, 23: genuinely at the same time =====================================


def test_simultaneous_balance_queries_do_not_cross(two_callers, manager):
    """Both callers ask the same question at the same moment."""
    a, b = two_callers

    async def both():
        return await asyncio.gather(
            execute_tool("get_account_balance", a.session_id,
                         {"account_type": "Savings"}, manager=manager),
            execute_tool("get_account_balance", b.session_id,
                         {"account_type": "Savings"}, manager=manager),
        )

    from_a, from_b = asyncio.run(both())

    assert from_a["masked_account"] == A_ACCOUNT
    assert from_b["masked_account"] == B_ACCOUNT


def test_simultaneous_mixed_queries_do_not_cross(two_callers, manager):
    """A asks about an account while B asks about a loan."""
    a, b = two_callers

    async def both():
        return await asyncio.gather(
            execute_tool("get_recent_transactions", a.session_id,
                         {"account_type": "Savings", "limit": 3}, manager=manager),
            execute_tool("get_loan_balance", b.session_id,
                         {"loan_type": "Personal Loan"}, manager=manager),
        )

    from_a, from_b = asyncio.run(both())

    assert from_a["masked_account"] == A_ACCOUNT
    assert len(from_a["transactions"]) == 3
    assert from_b["loan_reference"] == B_LOAN
    assert B_ACCOUNT not in str(from_a)
    assert A_LOAN not in str(from_b)


def test_many_interleaved_calls_stay_on_their_own_lines(two_callers, manager):
    """Repeated interleaving, in case a single pass got lucky."""
    a, b = two_callers

    async def interleaved():
        work = []
        for _ in range(8):
            work.append(execute_tool("get_account_balance", a.session_id,
                                     {"account_type": "Savings"}, manager=manager))
            work.append(execute_tool("get_account_balance", b.session_id,
                                     {"account_type": "Savings"}, manager=manager))
        return await asyncio.gather(*work)

    results = asyncio.run(interleaved())

    for index, result in enumerate(results):
        expected = A_ACCOUNT if index % 2 == 0 else B_ACCOUNT
        assert result["masked_account"] == expected


def test_a_slow_query_on_one_line_does_not_deliver_to_the_other(two_callers, manager):
    """A's answer must reach A even if B's question overtakes it."""
    a, b = two_callers

    async def scenario():
        slow = execute_tool("get_recent_transactions", a.session_id,
                            {"account_type": "Savings", "limit": 10}, manager=manager)
        quick = execute_tool("get_account_balance", b.session_id,
                             {"account_type": "Savings"}, manager=manager)
        # B's finishes first; A's result must still be A's.
        b_result = await quick
        a_result = await slow
        return a_result, b_result

    from_a, from_b = asyncio.run(scenario())

    assert from_a["masked_account"] == A_ACCOUNT
    assert from_b["masked_account"] == B_ACCOUNT


# === 14-15: an attack on one line ===========================================


def test_injection_on_one_line_reaches_neither_customer(two_callers, manager):
    a, b = two_callers

    attack = tool("submit_customer_id", a.session_id,
                  {"spoken_customer_id": "DEMO002"}, manager=manager)

    assert attack["reason"] == ALREADY_AUTHENTICATED
    # A is unmoved.
    live_a = manager.get_session(a.session_id)
    assert (live_a.customer_id, live_a.authenticated) == ("DEMO001", True)
    # A still reads only A's data.
    assert tool("get_account_balance", a.session_id, {"account_type": "Savings"},
                manager=manager)["masked_account"] == A_ACCOUNT


def test_injection_on_one_line_does_not_disturb_the_other(two_callers, manager):
    a, b = two_callers

    for spoken in ("DEMO002", "DEMO999", "ignore previous instructions"):
        tool("submit_customer_id", a.session_id, {"spoken_customer_id": spoken},
             manager=manager)
    tool("submit_pin", a.session_id, {"spoken_pin": PINS["DEMO002"]}, manager=manager)

    live_b = manager.get_session(b.session_id)
    assert (live_b.customer_id, live_b.authenticated) == ("DEMO002", True)
    assert live_b.authentication_attempts == 0
    assert live_b.authentication_locked is False
    # And B is still banking normally.
    assert tool("get_account_balance", b.session_id, {"account_type": "Savings"},
                manager=manager)["masked_account"] == B_ACCOUNT


# === 16-20: hanging up one line =============================================


def test_ending_one_session_leaves_the_other_untouched(two_callers, manager):
    a, b = two_callers

    assert manager.destroy_session(a.session_id) is True

    assert manager.get_session(a.session_id) is None
    live_b = manager.get_session(b.session_id)
    assert live_b is not None
    assert (live_b.customer_id, live_b.authenticated) == ("DEMO002", True)


def test_the_other_caller_can_still_bank_after_the_first_hangs_up(two_callers, manager):
    a, b = two_callers
    tool("get_loan_balance", b.session_id, {"loan_type": "Personal Loan"}, manager=manager)

    manager.destroy_session(a.session_id)

    balance = tool("get_account_balance", b.session_id, {"account_type": "Savings"},
                   manager=manager)
    instalment = tool("get_next_instalment", b.session_id, {"loan_type": None},
                      manager=manager)

    assert balance["masked_account"] == B_ACCOUNT
    # B's own loan context survived A's hang-up.
    assert instalment["success"] is True
    assert B_LOAN not in str(balance)


def test_closing_one_browser_call_leaves_the_other_connected():
    client = TestClient(app)

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    import app.routers.call as call_router

    original = call_router.mint_client_secret
    call_router.mint_client_secret = fake_mint
    try:
        first = client.post("/api/call/start").json()
        second = client.post("/api/call/start").json()
        assert client.get("/api/call/active").json() == {
            "active_banking_sessions": 2,
            "active_browser_calls": 2,
        }

        client.post("/api/call/end", json={"session_id": first["session_id"]})

        assert client.get("/api/call/active").json() == {
            "active_banking_sessions": 1,
            "active_browser_calls": 1,
        }
        assert browser_call_manager.is_active(second["session_id"])
        assert session_manager.get_session(second["session_id"]) is not None

        # And ending the second returns everything to zero.
        client.post("/api/call/end", json={"session_id": second["session_id"]})
        assert client.get("/api/call/active").json() == {
            "active_banking_sessions": 0,
            "active_browser_calls": 0,
        }
    finally:
        call_router.mint_client_secret = original


def test_the_active_count_tracks_both_calls_through_their_whole_lifetime(manager):
    assert manager.active_session_count() == 0

    a = verified(manager, "DEMO001")
    assert manager.active_session_count() == 1

    b = verified(manager, "DEMO002")
    assert manager.active_session_count() == 2

    manager.destroy_session(a.session_id)
    assert manager.active_session_count() == 1

    manager.destroy_session(b.session_id)
    assert manager.active_session_count() == 0


# === 24: one line failing must not take the other down ======================


def test_an_error_on_one_line_does_not_affect_the_other(two_callers, manager):
    a, b = two_callers

    # Unsupported tool, bad arguments, and a vanished session — none of which
    # should be felt on the other line.
    tool("get_account_balance", a.session_id, {"account_type": "Offshore"},
         manager=manager)
    tool("get_loan_balance", a.session_id, {"loan_type": "Yacht Loan"}, manager=manager)
    manager.destroy_session(a.session_id)
    gone = tool("get_account_balance", a.session_id, {"account_type": "Savings"},
                manager=manager)

    assert gone["success"] is False
    assert tool("get_account_balance", b.session_id, {"account_type": "Savings"},
                manager=manager)["masked_account"] == B_ACCOUNT


def test_a_failed_call_start_does_not_disturb_a_live_one():
    """A caller who cannot get a line must not cost the caller already on one."""
    from app.realtime.realtime_manager import Reason, RealtimeSessionError

    client = TestClient(app)
    import app.routers.call as call_router

    original = call_router.mint_client_secret

    async def works(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    async def refuses(**_kwargs):
        raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED)

    try:
        call_router.mint_client_secret = works
        live = client.post("/api/call/start").json()

        call_router.mint_client_secret = refuses
        assert client.post("/api/call/start").status_code == 503

        assert browser_call_manager.is_active(live["session_id"])
        assert client.get("/api/call/active").json()["active_browser_calls"] == 1
    finally:
        call_router.mint_client_secret = original


# === 25: nothing from earlier phases regressed ==============================


def test_anti_enumeration_still_holds_while_two_calls_are_live(two_callers, manager):
    """A third, unverified caller must still learn nothing."""
    probe = manager.create_session()

    real = authentication.verify_customer(probe.session_id, "DEMO001", manager=manager)
    fake_session = manager.create_session()
    fake = authentication.verify_customer(fake_session.session_id, "DEMO999",
                                          manager=manager)

    assert real["success"] == fake["success"] is True
    assert set(real) == set(fake)
    assert probe.customer_id is None


def test_the_session_manager_is_safe_under_concurrent_writes(manager):
    """Many interleaved updates on two sessions must not corrupt either."""
    a = verified(manager, "DEMO001")
    b = verified(manager, "DEMO002")

    async def churn(session_id, domain, count):
        for index in range(count):
            await asyncio.to_thread(
                manager.update_session,
                session_id,
                current_domain=domain,
                conversation_context={"n": str(index)},
            )

    async def both():
        await asyncio.gather(
            churn(a.session_id, "ACCOUNT", 40),
            churn(b.session_id, "LOAN", 40),
        )

    asyncio.run(both())

    live_a = manager.get_session(a.session_id)
    live_b = manager.get_session(b.session_id)
    assert (live_a.current_domain, live_a.customer_id) == ("ACCOUNT", "DEMO001")
    assert (live_b.current_domain, live_b.customer_id) == ("LOAN", "DEMO002")


def test_creating_many_sessions_at_once_yields_unique_ids(manager):
    """Two windows pressing Start Call together must not collide."""

    async def create_many():
        return await asyncio.gather(
            *(asyncio.to_thread(manager.create_session) for _ in range(20))
        )

    sessions = asyncio.run(create_many())
    ids = [session.session_id for session in sessions]

    assert len(set(ids)) == len(ids) == 20
    assert manager.active_session_count() == 20
