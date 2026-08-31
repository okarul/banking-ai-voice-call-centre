"""Phase 7.3: the enquiry a caller made before we knew who they were.

Two live calls at capacity 2 authenticated correctly and then went quiet:

    21fbca8a-1f16-1240-4790-eaa5afddeeef  DEMO002, get_loan_details held
    5f9c0453-1f16-1240-4790-eaa5afddeeef  DEMO001, get_account_balance held

Both ran `get_authentication_status`, `submit_customer_id` and `submit_pin`
successfully, reached `auth_status = VERIFIED` with the right `customer_ref` -
and then never called the banking tool. The held enquiry was still sitting in
the trace afterwards, and the caller sat in silence until `CALLER_SILENT`.

The control call `3dac35b9-1ed5-1240-4790-eaa5afddeeef` did the same three
tools and called `get_account_balance` 0.61 s after `submit_pin`.

**The difference is not in this codebase.** Nothing here answers a held
enquiry. `submit_pin` reads the pending request and attaches it to its own
result; `turn_gate.resumes_held_enquiry` *authorises* the follow-up tool;
`pending_request.clear` runs only once that tool has answered. The instruction
to actually make the call lives in the model's prompt:

    If it carries a `pending_request`, that is the enquiry they made before you
    knew who they were: call the tool it names.

So the answer a verified caller gets depends on the model choosing to follow an
instruction. On the control call it did. On these two it did not, and nothing
in the system noticed or recovered.

That is what these tests pin, and it is deliberately stated as a property of
*this* system rather than of the model: **once an enquiry has been classified,
authorised and stored, answering it after verification must not be optional.**
"""

import asyncio

import pytest

from app.auth import authentication
from app.realtime.webrtc import execute_tool
from app.realtime.turn_gate import record_turn
from app.sessions import SessionManager, session_manager
from app import pending_request

PINS = {"DEMO001": "4821", "DEMO002": "7315"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    session_manager.clear()


@pytest.fixture
def manager():
    return SessionManager()


def tool(name, session_id, arguments=None, *, manager):
    return run(execute_tool(name, session_id, arguments or {}, manager=manager))


def caller_asks_then_verifies(manager, customer_id, question):
    """The live shape: enquiry first, identity second, PIN third."""
    session = manager.create_session()

    # The enquiry arrives before anybody is verified. The gate holds it.
    record_turn(session, question)
    held = pending_request.recall(session)
    assert held is not None, "the gate did not hold the enquiry"

    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": customer_id}, manager=manager)
    result = tool("submit_pin", session.session_id,
                  {"spoken_pin": PINS[customer_id]}, manager=manager)
    assert result["success"] is True
    return session, held, result


# === HR-001: the enquiry must be answered, not merely authorised ============


def test_a_held_enquiry_is_answered_once_the_caller_is_verified(manager):
    """HR-001. The live failure, stated as a requirement on this system.

    After `submit_pin` succeeds carrying a `pending_request`, the caller has
    been verified and the enquiry has already been classified, authorised and
    stored. Whether they are answered must not depend on the model choosing to
    make one more tool call.
    """
    session, held, result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )

    assert held.tool == "get_account_balance"
    assert result.get("pending_request") is not None, (
        "submit_pin did not hand the held enquiry back"
    )

    live = manager.get_session(session.session_id)
    assert live.authenticated is True

    # Answered by the backend, deterministically, and the answer came back with
    # the verification rather than waiting for the model to ask for it.
    assert result.get("pending_result") is not None, (
        "the caller was verified and nothing in this system answered their "
        "enquiry"
    )
    assert result["pending_result"]["success"] is True

    # The hold itself stays. It is what authorises the model's own follow-up on
    # a later turn without waiting for a ruling - Phase 6.11.1's D-5 - and it is
    # consumed by that follow-up, which is served from the cached answer.
    assert pending_request.recall(live) is not None
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is not None


# === HR-002 to HR-012: the resume, under everything the live calls did ======


@pytest.fixture
def runs(monkeypatch):
    """Counts what actually reached the banking tools, per session.

    Measured at `_run_tool` - the one door every registered tool goes through -
    rather than from `agent_tool_events`, because these tests drive the tool
    layer directly and never claim a phone call, so no agent session row
    exists to count. The real tool still runs; this only watches.
    """
    import app.realtime.tools as tools_module

    original = tools_module._run_tool
    seen: list[tuple[str, str]] = []

    def counting(tool_name, session_id, arguments, manager):
        seen.append((session_id, tool_name))
        return original(tool_name, session_id, arguments, manager)

    monkeypatch.setattr(tools_module, "_run_tool", counting)

    class Counter:
        def of(self, session_id, tool_name):
            return sum(
                1 for sid, name in seen
                if sid == session_id and name == tool_name
            )

    return Counter()


def test_repeated_pin_turns_before_auth_still_resume_exactly_once(manager, runs):
    """HR-002. Both failed live calls had repeated PIN transcription turns."""
    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    # The caller says the PIN more than once; only the last one is right.
    record_turn(manager.get_session(session.session_id), "Four eight two one.")
    tool("submit_pin", session.session_id, {"spoken_pin": "0000"}, manager=manager)
    record_turn(manager.get_session(session.session_id), "Four eight two one.")
    result = tool("submit_pin", session.session_id,
                  {"spoken_pin": "4821"}, manager=manager)

    assert result["success"] is True
    assert runs.of(session.session_id, "get_account_balance") == 1


def test_the_answer_rides_back_with_the_verification(manager):
    """HR-003. No second response generator, so none can overlap.

    The banking answer is inside `submit_pin`'s own tool result. The model is
    already going to speak about that result, so nothing here issues a
    `response.create` that could collide with an active response, cut into
    barge-in, or disturb the goodbye lifecycle.
    """
    _session, _held, result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )

    assert "pending_result" in result, "the answer did not travel with the result"
    assert result["pending_result"]["success"] is True
    assert result["pending_request"]["tool"] == "get_account_balance"

    # Scanned as code, not as prose: the docstring of `_resume_held_enquiry`
    # explains at length why it does *not* create a response, and a plain
    # substring search would trip over its own explanation.
    import ast
    import inspect

    import app.realtime.tools as tools_module

    tree = ast.parse(inspect.getsource(tools_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)

    assert "response.create" not in code, "tools.py creates a response"
    assert "create_response" not in code, "tools.py creates a response"


def test_a_stale_pin_event_after_verification_resumes_nothing(manager, runs):
    """HR-004. A repeat of a submit_pin that already succeeded."""
    session, _held, _first = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    assert runs.of(session.session_id, "get_account_balance") == 1

    tool("submit_pin", session.session_id, {"spoken_pin": "4821"}, manager=manager)

    assert runs.of(session.session_id, "get_account_balance") == 1, (
        "a stale PIN event ran the held enquiry a second time"
    )


def test_two_sessions_resume_only_their_own_enquiry(manager, runs):
    """HR-005. The live pair, side by side."""
    a = manager.create_session()
    b = manager.create_session()
    record_turn(a, "What is my savings balance?")
    record_turn(b, "Tell me about my personal loan")

    for session, customer in ((a, "DEMO001"), (b, "DEMO002")):
        tool("submit_customer_id", session.session_id,
             {"spoken_customer_id": customer}, manager=manager)

    result_a = tool("submit_pin", a.session_id, {"spoken_pin": "4821"},
                    manager=manager)
    result_b = tool("submit_pin", b.session_id, {"spoken_pin": "7315"},
                    manager=manager)

    assert result_a["pending_request"]["tool"] == "get_account_balance"
    assert result_b["pending_request"]["tool"] in {
        "get_loan_details", "get_loan_balance"
    }
    assert runs.of(a.session_id, "get_account_balance") == 1
    assert runs.of(b.session_id, "get_account_balance") == 0

    # Each caller's answer is cached against their own session and nobody
    # else's.
    a_live = manager.get_session(a.session_id)
    b_live = manager.get_session(b.session_id)
    assert pending_request.answer_for(
        a_live, "get_account_balance", {"account_type": "Savings"}
    ) is not None
    assert pending_request.answer_for(
        b_live, "get_account_balance", {"account_type": "Savings"}
    ) is None


def test_noisy_turns_do_not_destroy_a_held_enquiry(manager, runs):
    """HR-006. Unintelligible turns are exactly what these calls were full of."""
    session = manager.create_session()
    record_turn(session, "What is my savings balance?")

    for noise in ("mm", "sorry what", "...", "erm"):
        record_turn(manager.get_session(session.session_id), noise)

    held = pending_request.recall(manager.get_session(session.session_id))
    assert held is not None and held.tool == "get_account_balance"

    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)
    tool("submit_pin", session.session_id, {"spoken_pin": "4821"}, manager=manager)

    assert runs.of(session.session_id, "get_account_balance") == 1


def test_the_model_asking_again_does_not_replay_the_held_enquiry(manager):
    """HR-007. The resume happens once; a later request is a new question.

    A verified caller who asks again must still be answered - refusing them
    would be a worse defect than the one being fixed - so what is pinned here
    is that the *held* enquiry is resumed exactly once and is not owed again.
    """
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    assert pending_request.recall(manager.get_session(session.session_id)) is not None

    again = tool("get_account_balance", session.session_id,
                 {"account_type": "Savings"}, manager=manager)

    assert again["success"] is True
    # Served from what was already read, and the hold is consumed by it.
    assert pending_request.recall(manager.get_session(session.session_id)) is None


def test_a_pending_request_naming_an_unresumable_tool_is_refused(manager, runs):
    """HR-008. The value that decides which tool runs is checked twice."""
    session = manager.create_session()
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    # Written straight into the session, bypassing `remember`'s own guard.
    live = manager.get_session(session.session_id)
    context = dict(live.conversation_context)
    context[pending_request.PENDING_REQUEST_KEY] = {"tool": "submit_pin"}
    manager.update_session(live.session_id, conversation_context=context)

    result = tool("submit_pin", session.session_id, {"spoken_pin": "4821"},
                  manager=manager)

    assert result["success"] is True
    assert "pending_result" not in result, "an unresumable tool was executed"
    assert runs.of(session.session_id, "get_account_balance") == 0


def test_a_held_enquiry_carries_no_customer_and_cannot_name_one(manager):
    """HR-009. Identity comes from the verified session, never from the hold."""
    from dataclasses import fields

    held_fields = {f.name for f in fields(pending_request.PendingRequest)}
    assert held_fields == {"tool", "account_type", "loan_type"}, held_fields

    session, held, result = caller_asks_then_verifies(
        manager, "DEMO002", "What is my savings balance?"
    )
    assert "customer" not in str(held.to_dict()).lower()
    # The answer is DEMO002's, decided by the session and nothing else.
    assert result["pending_result"]["success"] is True
    assert manager.get_session(session.session_id).customer_id == "DEMO002"
    # And the cache key names no customer either.
    key = pending_request._answer_key("get_account_balance",
                                      {"account_type": "Savings"})
    assert "DEMO" not in str(key)


def test_a_failed_banking_tool_keeps_the_enquiry_owed(manager, monkeypatch):
    """HR-010. A failure must not be recorded as an answer."""
    import app.realtime.tools as tools_module

    def failing(tool_name, session_id, arguments, manager_):
        return {"success": False, "reason": "DATABASE_UNAVAILABLE"}

    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    monkeypatch.setattr(tools_module, "_run_tool", failing)
    result = tool("submit_pin", session.session_id, {"spoken_pin": "4821"},
                  manager=manager)

    assert result["pending_result"]["success"] is False
    held = pending_request.recall(manager.get_session(session.session_id))
    assert held is not None, "a failed lookup cleared the enquiry as if answered"
    assert held.tool == "get_account_balance"


def test_a_successful_resume_caches_the_answer_and_keeps_the_hold(manager):
    """HR-011, under B1.

    The hold is not "the operation has not run yet" any more - that is the
    cache's job. It is "this classified enquiry is still the legitimate
    continuation", which is what authorises the follow-up on a later turn.
    """
    session, _held, result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    live = manager.get_session(session.session_id)
    assert result["pending_result"]["success"] is True
    assert pending_request.recall(live) is not None
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is not None


def test_an_unverified_session_never_resumes(manager, runs):
    """HR-012. Resume is a post-verification act, and only that."""
    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    assert pending_request.recall(session) is not None

    # No customer id, no PIN: the caller is a stranger.
    live = manager.get_session(session.session_id)
    assert live.authenticated is False
    assert runs.of(session.session_id, "get_account_balance") == 0

    # And a wrong PIN leaves them one.
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)
    refused = tool("submit_pin", session.session_id, {"spoken_pin": "0000"},
                   manager=manager)
    assert refused["success"] is False
    assert "pending_result" not in refused
    assert runs.of(session.session_id, "get_account_balance") == 0
    assert pending_request.recall(manager.get_session(session.session_id)) is not None


# === control cases that must not move ======================================


def test_a_wrong_pin_never_executes_the_held_enquiry(manager, runs):
    """Control. The whole point of holding it is that it waits for VERIFIED."""
    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    for _ in range(2):
        result = tool("submit_pin", session.session_id, {"spoken_pin": "1111"},
                      manager=manager)
        assert result["success"] is False

    assert runs.of(session.session_id, "get_account_balance") == 0
    assert manager.get_session(session.session_id).authenticated is False


def test_the_session_attempt_limit_is_unchanged(manager):
    """Control. Resume must not have altered how many tries a caller gets."""
    session = manager.create_session()
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    outcomes = [
        tool("submit_pin", session.session_id, {"spoken_pin": "1111"},
             manager=manager)
        for _ in range(authentication.MAX_AUTHENTICATION_ATTEMPTS)
    ]
    assert all(o["success"] is False for o in outcomes)
    live = manager.get_session(session.session_id)
    assert live.authentication_attempts >= authentication.MAX_AUTHENTICATION_ATTEMPTS


def test_a_resumed_enquiry_cannot_read_another_customer(manager):
    """Control. Cross-customer protection is unchanged by the resume."""
    session, _held, result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    answer = str(result["pending_result"])
    # DEMO002's seeded savings figure must appear nowhere in DEMO001's answer.
    assert "3820.10" not in answer
    assert manager.get_session(session.session_id).customer_id == "DEMO001"


# === HR-013 to HR-024: what B1 has to guarantee =============================


def test_the_hold_remains_and_the_answer_is_cached(manager, runs):
    """HR-013."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    live = manager.get_session(session.session_id)

    assert pending_request.recall(live) is not None
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is not None
    assert runs.of(session.session_id, "get_account_balance") == 1


def test_the_same_question_again_reuses_the_answer(manager, runs):
    """HR-014. One question, one business operation."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    first = runs.of(session.session_id, "get_account_balance")

    again = tool("get_account_balance", session.session_id,
                 {"account_type": "Savings"}, manager=manager)

    assert again["success"] is True
    assert runs.of(session.session_id, "get_account_balance") == first == 1, (
        "the bank was asked the same question twice"
    )


def test_a_different_account_never_gets_the_cached_answer(manager, runs):
    """HR-015. The wrong-data defect, pinned.

    A resumed Savings answer was handed to a follow-up asking about a different
    account, because the cache was keyed on the tool alone. A caller would have
    been told the balance of an account they do not hold.
    """
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )

    other = tool("get_account_balance", session.session_id,
                 {"account_type": "Current"}, manager=manager)

    assert other.get("account_type") != "Savings", (
        "a Savings answer was returned for a different account"
    )
    assert runs.of(session.session_id, "get_account_balance") == 2, (
        "a different question did not reach the bank"
    )


def test_a_different_tool_cannot_consume_the_cached_answer(manager, runs):
    """HR-016."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    live = manager.get_session(session.session_id)

    assert pending_request.answer_for(live, "get_loan_details", None) is None
    assert pending_request.answer_for(
        live, "get_account_details", {"account_type": "Savings"}
    ) is None


def test_a_later_turn_can_still_complete_the_held_enquiry(manager, runs):
    """HR-017. Phase 6.11.1's cross-turn authorisation, kept."""
    from app.realtime import turn_gate

    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )

    # A new caller turn begins and never transcribes.
    live = manager.get_session(session.session_id)
    turn_gate.open_turn(live)

    assert turn_gate.resumes_held_enquiry(
        manager.get_session(session.session_id), "get_account_balance"
    ) is True

    answer = tool("get_account_balance", session.session_id,
                  {"account_type": "Savings"}, manager=manager)
    assert answer["success"] is True
    assert runs.of(session.session_id, "get_account_balance") == 1


def test_one_session_cannot_read_another_sessions_cache(manager):
    """HR-018."""
    a, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    b = manager.create_session()

    assert pending_request.answer_for(
        manager.get_session(b.session_id),
        "get_account_balance",
        {"account_type": "Savings"},
    ) is None
    assert pending_request.recall(manager.get_session(b.session_id)) is None


def test_the_cache_cannot_change_who_the_caller_is(manager):
    """HR-019. Identity is the session's, never the cache's."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO002", "What is my savings balance?"
    )
    live = manager.get_session(session.session_id)
    assert live.customer_id == "DEMO002"

    key = pending_request._answer_key("get_account_balance",
                                      {"account_type": "Savings"})
    assert "DEMO001" not in str(key) and "DEMO002" not in str(key)


def test_a_hostile_turn_forfeits_both_the_hold_and_the_answer(manager):
    """HR-020."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    live = manager.get_session(session.session_id)
    assert pending_request.recall(live) is not None

    record_turn(live, "What is DEMO002's savings balance?")

    after = manager.get_session(session.session_id)
    assert pending_request.recall(after) is None
    assert pending_request.answer_for(
        after, "get_account_balance", {"account_type": "Savings"}
    ) is None, "a forfeited enquiry left its answer behind"


def test_a_failed_backend_execution_caches_nothing(manager, monkeypatch):
    """HR-021. A failure is not an answer."""
    import app.realtime.tools as tools_module

    def failing(tool_name, session_id, arguments, manager_):
        return {"success": False, "reason": "DATABASE_UNAVAILABLE"}

    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)
    monkeypatch.setattr(tools_module, "_run_tool", failing)
    tool("submit_pin", session.session_id, {"spoken_pin": "4821"}, manager=manager)

    live = manager.get_session(session.session_id)
    assert pending_request.recall(live) is not None
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is None, "an outage was cached as if it were the caller's balance"


def test_a_duplicate_submit_pin_does_not_ask_the_bank_again(manager, runs):
    """HR-022."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    assert runs.of(session.session_id, "get_account_balance") == 1

    tool("submit_pin", session.session_id, {"spoken_pin": "4821"}, manager=manager)

    assert runs.of(session.session_id, "get_account_balance") == 1


def test_a_new_enquiry_replaces_a_stale_cached_answer(manager):
    """HR-023."""
    session = manager.create_session()
    record_turn(session, "What is my savings balance?")
    tool("submit_customer_id", session.session_id,
         {"spoken_customer_id": "DEMO001"}, manager=manager)

    # A different enquiry arrives before verification finishes.
    record_turn(manager.get_session(session.session_id),
                "How much is left on my home loan?")

    live = manager.get_session(session.session_id)
    held = pending_request.recall(live)
    assert held is not None and held.tool != "get_account_balance"
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is None


def test_ending_the_session_leaves_no_reusable_answer(manager):
    """HR-024."""
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    session_id = session.session_id
    manager.destroy_session(session_id)

    assert manager.get_session(session_id) is None
    assert pending_request.answer_for(
        manager.get_session(session_id),
        "get_account_balance",
        {"account_type": "Savings"},
    ) is None


# === the cache key, in the tool layer's own terms ===========================


def test_the_same_account_spelled_differently_is_the_same_question(manager, runs):
    """HR-014, at the spelling the model actually sends.

    `_resolve_account` selects on `strip().lower()`, so "savings" and "Savings"
    name one account and must not read it twice. Keyed on the raw string, the
    second spelling missed the cache and asked the bank again - harmless data,
    but a second business operation for one enquiry, which is the thing this
    phase exists to stop.
    """
    session, _held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    assert runs.of(session.session_id, "get_account_balance") == 1

    again = tool("get_account_balance", session.session_id,
                 {"account_type": " savings "}, manager=manager)

    assert again["success"] is True
    assert runs.of(session.session_id, "get_account_balance") == 1, (
        "one spelling of one account was read from the bank twice"
    )


def test_canonicalising_never_merges_two_different_selections(manager):
    """The widening above may only ever join spellings the bank itself joins."""
    key = pending_request._answer_key
    assert key("get_account_balance", {"account_type": "Savings"}) == key(
        "get_account_balance", {"account_type": " savings "}
    )
    assert key("get_account_balance", {"account_type": "Savings"}) != key(
        "get_account_balance", {"account_type": "Current"}
    )
    # An argument nobody gave is not an argument that is null.
    assert key("get_account_balance", {"account_type": None}) != key(
        "get_account_balance", {"account_type": "Savings"}
    )
    assert key("get_account_balance", {}) == key(
        "get_account_balance", {"account_type": None}
    )
    # A non-string argument survives untouched.
    assert key("get_recent_transactions", {"limit": 3}) != key(
        "get_recent_transactions", {"limit": 5}
    )


def test_an_account_the_caller_does_not_hold_is_refused_not_answered(manager, runs):
    """HR-015, at the refusal itself.

    The wrong-data defect's worst form: a resumed Savings balance handed back
    for an account that does not exist, in place of `ACCOUNT_NOT_FOUND`.
    """
    session, _held, result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    savings = result["pending_result"]

    missing = tool("get_account_balance", session.session_id,
                   {"account_type": "Platinum"}, manager=manager)

    assert missing["success"] is False, "a nonexistent account was answered"
    assert missing["reason"] == "ACCOUNT_NOT_FOUND"
    assert missing != savings
    assert runs.of(session.session_id, "get_account_balance") == 2


def test_the_resume_itself_never_consumes_the_hold(manager, runs):
    """The `keep_pending` invariant, at the cache-hit path.

    A second `submit_pin` cannot reach the resume today - an already-verified
    session is answered `ALREADY_AUTHENTICATED` - so this drives the resume
    directly rather than pretending otherwise. The hold must survive being
    served from the cache, or the deterministic resume would consume the very
    thing it is required to leave standing for the model's follow-up.
    """
    import app.realtime.tools as tools_module
    from agents import RunContextWrapper
    from app.realtime.context import BankingRealtimeContext

    session, held, _result = caller_asks_then_verifies(
        manager, "DEMO001", "What is my savings balance?"
    )
    assert runs.of(session.session_id, "get_account_balance") == 1

    context = RunContextWrapper(
        BankingRealtimeContext(session_id=session.session_id, manager=manager)
    )
    answer = run(tools_module._resume_held_enquiry(context, held))

    assert answer["success"] is True
    assert runs.of(session.session_id, "get_account_balance") == 1, (
        "a second resume asked the bank again"
    )
    live = manager.get_session(session.session_id)
    assert pending_request.recall(live) is not None, (
        "the resume consumed the hold it must leave standing"
    )
    assert pending_request.answer_for(
        live, "get_account_balance", {"account_type": "Savings"}
    ) is not None
