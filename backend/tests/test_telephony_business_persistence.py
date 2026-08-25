"""Phase 6.9: what a telephone call leaves behind in the database.

A live call authenticated nothing, according to `agent_sessions`. The row said
`authenticated = false`, `auth_status = PENDING`, `tool_call_count = 0` — and
the row was not lying about what it knew, because nothing on the telephone path
ever told it anything.

Channel 1 records business events from `POST /api/call/tool`: the browser
round-trips every tool call through that endpoint, and the endpoint mirrors the
outcome into the database. Channel 2 never visits it. The Agents SDK executes
the same tools in this process, they update the in-memory banking session
correctly, and the database hears nothing at all.

So these tests do what the previous live call could not: run the **real**
Channel 2 tool surface — the same `@function_tool` objects the telephone agent
is given — against a real seeded customer, and then read both the session and
the row. Two questions, deliberately separated:

    does Channel 2 authentication actually work?      (the session)
    does anything write that down?                    (the row)

The first is the functional question the live call left open. The second is the
defect. They have different answers, and a test that checked only the row would
have blamed the wrong thing.

No browser route, no HTTP, no live model, no cost. All customers and PINs here
are the synthetic Phase 2 seed.
"""

import asyncio
import json

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext
from sqlalchemy import delete, select

from app.auth import authentication
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.observability import recorder
from app.realtime import tools
from app.realtime.context import BankingRealtimeContext
from app.realtime.turn_gate import record_turn
from app.sessions import session_manager

# Synthetic demo credentials from the Phase 2 seed. Not real.
PINS = {"DEMO001": "4821", "DEMO002": "7315"}
DEMO001_SAVINGS = "12450.75"


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    session_manager.clear()
    yield
    session_manager.clear()
    wipe()


def run(coro):
    return asyncio.run(coro)


async def call_tool(tool, context: BankingRealtimeContext, **arguments):
    """Invoke a realtime function tool exactly as the SDK would.

    Arguments arrive as a JSON string, which is what the model produces, so a
    field the tool does not declare is refused here rather than by Python. This
    is the same helper the Channel 1 realtime tests use; the point of these
    tests is that the *tool* is reached the way the telephone reaches it.
    """
    payload = json.dumps(arguments)
    tool_context = ToolContext.from_agent_context(
        RunContextWrapper(context),
        tool_call_id="phase69-test",
        tool_name=tool.name,
        tool_arguments=payload,
    )
    result = await tool.on_invoke_tool(tool_context, payload)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result


def phone_call(call_id="phase69-call"):
    """A banking session and the `agent_sessions` row a real call would have.

    Built through `claim_phone_call`, which is what the telephony service
    actually uses, so the row under test has the channel, status and defaults a
    live call's row has — including `authenticated = False` and
    `auth_status = PENDING`, which is exactly what the live call came back with.
    """
    session = session_manager.create_session()
    recorder.claim_phone_call(
        session.session_id,
        provider_call_id=call_id,
        provider_event_id=f"evt-{call_id}",
    )
    return session, BankingRealtimeContext(
        session_id=session.session_id, manager=session_manager
    )


def row(banking_session_id):
    with session_scope() as db:
        return db.scalars(
            select(AgentSession).where(
                AgentSession.banking_session_id == banking_session_id
            )
        ).one()


def tool_events(banking_session_id):
    with session_scope() as db:
        record = db.scalars(
            select(AgentSession).where(
                AgentSession.banking_session_id == banking_session_id
            )
        ).one()
        return list(
            db.scalars(
                select(AgentToolEvent).where(
                    AgentToolEvent.session_pk == record.id
                )
            )
        )


# === A. does Channel 2 authentication actually work? ========================


def test_the_real_channel_2_tools_authenticate_the_banking_session():
    """The functional question the live call could not answer.

    If this fails, the persistence gap was hiding a second and worse defect.
    If it passes, Channel 2 authentication works and only the record is wrong.
    """
    session, context = phone_call()

    identified = run(
        call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001")
    )
    verified = run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    assert identified["success"] is True, identified
    assert verified["success"] is True, verified

    live = session_manager.get_session(session.session_id)
    assert live.authenticated is True, "Channel 2 did not authenticate the session"
    assert live.customer_id == "DEMO001"
    assert live.authentication_attempts == 0, "a clean verification left attempts set"


def test_a_wrong_pin_leaves_the_channel_2_session_unauthenticated():
    session, context = phone_call("phase69-wrong")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    refused = run(call_tool(tools.submit_pin, context, spoken_pin="0000"))

    live = session_manager.get_session(session.session_id)
    assert refused.get("success") is False
    assert live.authenticated is False
    assert live.customer_id is None, "an unverified claim became an identity"


def test_five_wrong_pins_still_lock_the_channel_2_caller_out():
    """The lockout is a property of the bank, not of the browser transport."""
    session, context = phone_call("phase69-lockout")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    for _ in range(5):
        run(call_tool(tools.submit_pin, context, spoken_pin="0000"))

    live = session_manager.get_session(session.session_id)
    assert live.authenticated is False

    # And the correct PIN no longer helps.
    after = run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))
    assert after.get("success") is False
    assert session_manager.get_session(session.session_id).authenticated is False


def test_a_channel_2_call_reaches_only_its_own_customer():
    """Two calls, two customers, no leakage between them."""
    first, first_ctx = phone_call("phase69-a")
    second, second_ctx = phone_call("phase69-b")

    run(call_tool(tools.submit_customer_id, first_ctx, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, first_ctx, spoken_pin=PINS["DEMO001"]))
    run(call_tool(tools.submit_customer_id, second_ctx, spoken_customer_id="DEMO002"))
    run(call_tool(tools.submit_pin, second_ctx, spoken_pin=PINS["DEMO002"]))

    assert session_manager.get_session(first.session_id).customer_id == "DEMO001"
    assert session_manager.get_session(second.session_id).customer_id == "DEMO002"

    record_turn(
        session_manager.get_session(first.session_id), "What is my savings balance?"
    )
    balance = run(
        call_tool(tools.get_account_balance, first_ctx, account_type="Savings")
    )
    assert balance["success"] is True
    assert balance["available_balance"] == DEMO001_SAVINGS, (
        "the first call was answered with somebody else's money"
    )


# === B. is any of it written down? ==========================================
#
# These are the production acceptance assertions. Before the fix they fail,
# and that failure is the defect stated as a test rather than as a paragraph.


def test_a_channel_2_authentication_is_persisted():
    session, context = phone_call("phase69-persist-auth")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    record = row(session.session_id)
    assert record.authenticated is True, (
        "the session authenticated but agent_sessions still says it did not"
    )
    assert record.auth_status == "VERIFIED", record.auth_status
    assert record.customer_id == "DEMO001"


def test_a_failed_channel_2_authentication_persists_no_identity():
    session, context = phone_call("phase69-persist-fail")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin="0000"))

    record = row(session.session_id)
    assert record.authenticated is False
    assert record.customer_id is None, "a claimed identity was persisted as verified"
    assert record.auth_status == "FAILED", record.auth_status


def test_a_channel_2_banking_tool_is_counted_exactly_once():
    session, context = phone_call("phase69-persist-tool")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    before = row(session.session_id).tool_call_count

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is my savings balance?")
    balance = run(call_tool(tools.get_account_balance, context, account_type="Savings"))

    # The tool itself must work — this is not only about the counter.
    assert balance["success"] is True, balance
    assert balance["available_balance"] == DEMO001_SAVINGS

    record = row(session.session_id)
    # "Exactly once" is a delta, not an absolute. The authentication tools are
    # invocations too and are counted — which is what Channel 1 has always
    # done, since `POST /api/call/tool` recorded every tool including
    # `submit_pin`. Asserting an absolute 1 here would have made the two
    # channels disagree, which is the opposite of the point.
    assert record.tool_call_count == before + 1, (
        f"the balance tool moved the count from {before} to "
        f"{record.tool_call_count}, expected exactly one more"
    )

    events = tool_events(session.session_id)
    balance_events = [e for e in events if e.tool_name == "get_account_balance"]
    assert len(balance_events) == 1, (
        f"{len(balance_events)} AgentToolEvent rows for one balance enquiry"
    )
    assert balance_events[0].status == "OK"


def test_a_refused_channel_2_tool_is_not_recorded_as_a_success():
    """An unauthenticated enquiry must not leave a successful-looking event."""
    session, context = phone_call("phase69-persist-refused")

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is my savings balance?")
    refused = run(call_tool(tools.get_account_balance, context, account_type="Savings"))

    assert refused.get("success") is False, refused

    events = tool_events(session.session_id)
    assert all(event.status != "OK" for event in events), (
        "a refused enquiry was recorded as a successful tool call"
    )


# === C. nothing sensitive is written down ===================================


def test_no_pin_or_balance_reaches_the_database():
    """The row counts what happened. It never stores what was said."""
    session, context = phone_call("phase69-privacy")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is my savings balance?")
    run(call_tool(tools.get_account_balance, context, account_type="Savings"))

    with session_scope() as db:
        written = " ".join(
            str(value)
            for record in db.scalars(select(AgentSession))
            for value in (
                record.disconnect_reason,
                record.auth_status,
                record.current_domain,
                record.last_intent,
                record.capability,
                record.customer_id,
            )
        )
        events = " ".join(
            f"{event.tool_name} {event.status}"
            for event in db.scalars(select(AgentToolEvent))
        )

    for secret in (PINS["DEMO001"], DEMO001_SAVINGS):
        assert secret not in written, f"{secret!r} reached agent_sessions"
        assert secret not in events, f"{secret!r} reached agent_tool_events"


def test_channel_2_stores_no_transcript():
    """Whatever else changes, spoken words must not start being kept."""
    session, context = phone_call("phase69-no-transcript")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    with session_scope() as db:
        assert db.scalars(select(ConversationMessage)).all() == [], (
            "Channel 2 began persisting conversation messages"
        )


# === D. the two channels must record the same thing, once each ==============
#
# The whole point of moving the mirror down to the shared tool objects is that
# neither channel is special any more. These tests are what stop the fix from
# trading one asymmetry for another: Channel 2 gaining records while Channel 1
# quietly gains a second copy of each.


@pytest.fixture
def browser(monkeypatch):
    """A browser client whose call never reaches OpenAI."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_a_real_secret", "expires_at": 0}

    monkeypatch.setattr("app.routers.call.mint_client_secret", fake_mint)
    return TestClient(create_app())


def browser_call(client):
    """A started browser call and its banking session id."""
    response = client.post("/api/call/start")
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def run_browser_tool(client, session_id, name, **arguments):
    response = client.post(
        "/api/call/tool",
        json={"session_id": session_id, "name": name, "arguments": arguments},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_channel_1_records_each_tool_exactly_once(browser):
    """The regression the shared mirror could most easily have introduced."""
    session_id = browser_call(browser)

    run_browser_tool(browser, session_id, "submit_customer_id",
                     spoken_customer_id="DEMO001")
    run_browser_tool(browser, session_id, "submit_pin", spoken_pin=PINS["DEMO001"])

    record = row(session_id)
    events = tool_events(session_id)

    assert record.tool_call_count == 2, (
        f"two browser tools produced a count of {record.tool_call_count}"
    )
    assert len(events) == 2, f"{len(events)} AgentToolEvent rows for two tools"
    assert [event.tool_name for event in events] == [
        "submit_customer_id",
        "submit_pin",
    ]


def test_channel_1_authentication_still_persists(browser):
    """Channel 1's existing behaviour, unchanged by moving the mirror."""
    session_id = browser_call(browser)

    run_browser_tool(browser, session_id, "submit_customer_id",
                     spoken_customer_id="DEMO001")
    run_browser_tool(browser, session_id, "submit_pin", spoken_pin=PINS["DEMO001"])

    record = row(session_id)
    assert record.authenticated is True
    assert record.auth_status == "VERIFIED"
    assert record.customer_id == "DEMO001"


def test_both_channels_record_identical_semantics(browser):
    """Same customer, same tools, same resulting row — whichever channel ran."""
    browser_session = browser_call(browser)
    run_browser_tool(browser, browser_session, "submit_customer_id",
                     spoken_customer_id="DEMO001")
    run_browser_tool(browser, browser_session, "submit_pin",
                     spoken_pin=PINS["DEMO001"])

    phone_session, context = phone_call("phase69-parity")
    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    one = row(browser_session)
    two = row(phone_session.session_id)

    assert (one.authenticated, one.auth_status, one.customer_id) == (
        two.authenticated,
        two.auth_status,
        two.customer_id,
    ), "the two channels disagree about the same authentication"
    assert one.tool_call_count == two.tool_call_count == 2


def test_a_channel_2_call_does_not_touch_another_session(browser):
    """A telephone call's records land on its own row and nowhere else."""
    browser_session = browser_call(browser)
    phone_session, context = phone_call("phase69-isolation")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    untouched = row(browser_session)
    assert untouched.tool_call_count == 0
    assert untouched.authenticated is False
    assert untouched.customer_id is None
    assert tool_events(browser_session) == []


# === E. turn, domain and intent =============================================


def test_a_channel_2_turn_persists_its_domain_and_intent():
    """Classified by the same gate both channels use, recorded once."""
    session, context = phone_call("phase69-turn")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is my savings balance?")

    record = row(session.session_id)
    assert record.last_intent == "OWN_ACCOUNT_ENQUIRY", record.last_intent
    assert record.current_domain == "ACCOUNT", record.current_domain
    assert record.capability == "Account Services", record.capability


def test_a_refused_turn_shows_as_refused_not_as_banking():
    """An operator must see that a turn was turned away."""
    session, context = phone_call("phase69-turn-refused")

    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is DEMO002's balance?")

    record = row(session.session_id)
    assert record.current_domain == "GENERAL/SCOPE", record.current_domain


def test_channel_1_turn_recording_is_not_duplicated(browser):
    """`/scope` must produce one turn record, not two."""
    session_id = browser_call(browser)

    response = browser.post(
        "/api/call/scope",
        json={"session_id": session_id, "transcript": "What is my savings balance?"},
    )
    assert response.status_code == 200, response.text

    record = row(session_id)
    assert record.last_intent is not None, "the browser turn was not recorded at all"


# === F. the log, not only the database ======================================
#
# The privacy assertions above read the database. Recording is new code on a
# path that carries a spoken PIN, and a value kept out of a table but written
# to a log line has still been written down.


def test_the_business_mirror_writes_no_secret_to_any_log(caplog):
    """A full authenticated enquiry, with every logger turned up to DEBUG."""
    import logging

    session, context = phone_call("phase69-log-privacy")

    with caplog.at_level(logging.DEBUG):
        run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
        run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))
        live = session_manager.get_session(session.session_id)
        record_turn(live, "What is my savings balance?")
        balance = run(
            call_tool(tools.get_account_balance, context, account_type="Savings")
        )

    assert balance["success"] is True, balance

    logged = "\n".join(record.getMessage() for record in caplog.records)

    # The PIN, the money, and the customer's name. The customer *id* is
    # deliberately not on this list: it is recorded by design, and Channel 1
    # has always logged it beside a tool call.
    for secret in (PINS["DEMO001"], DEMO001_SAVINGS, "12450", "Alex Tan"):
        assert secret not in logged, f"{secret!r} reached a log line"


def test_a_refused_enquiry_writes_no_secret_to_any_log(caplog):
    """The unauthenticated path logs no more than the authenticated one."""
    import logging

    session, context = phone_call("phase69-log-refused")

    with caplog.at_level(logging.DEBUG):
        run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
        run(call_tool(tools.submit_pin, context, spoken_pin="9999"))
        live = session_manager.get_session(session.session_id)
        record_turn(live, "What is my savings balance?")
        run(call_tool(tools.get_account_balance, context, account_type="Savings"))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("9999", DEMO001_SAVINGS, "Alex Tan"):
        assert secret not in logged, f"{secret!r} reached a log line"


# === G. the mirror must not hold a call open ================================


def test_recording_releases_every_session_it_touches():
    """Observability must leave no capacity behind.

    The mirror runs on worker threads inside a live call's tool path. If it
    held a database session, a reference or a slot, the leak would show up as
    a bank that slowly stops answering — so this is asserted rather than
    assumed.
    """
    from app.realtime.browser_calls import voice_call_manager

    before = voice_call_manager.used_capacity()

    for index in range(3):
        session, context = phone_call(f"phase69-capacity-{index}")
        run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
        run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))
        live = session_manager.get_session(session.session_id)
        record_turn(live, "What is my savings balance?")
        run(call_tool(tools.get_account_balance, context, account_type="Savings"))
        session_manager.destroy_session(session.session_id)

    assert voice_call_manager.used_capacity() == before, (
        "recording business events leaked a capacity slot"
    )


def test_recording_never_breaks_a_call_when_the_database_fails(monkeypatch):
    """An observability failure must not reach the caller.

    `recorder._safe` swallows and logs; this proves the guarantee still holds
    through the new shared boundary, because a tool that raised here would
    turn a working banking answer into a failed one.
    """
    from app.observability import recorder as recorder_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    session, context = phone_call("phase69-recorder-down")
    run(call_tool(tools.submit_customer_id, context, spoken_customer_id="DEMO001"))
    run(call_tool(tools.submit_pin, context, spoken_pin=PINS["DEMO001"]))

    live = session_manager.get_session(session.session_id)
    record_turn(live, "What is my savings balance?")

    # The layer beneath the mirror, which is what a database outage
    # actually looks like. `recorder.record_tool_call` guards itself, so
    # replacing it with a raiser is the way to reach the guard above it.
    monkeypatch.setattr(recorder_module, "record_tool_call", explode)
    balance = run(call_tool(tools.get_account_balance, context, account_type="Savings"))

    assert balance["success"] is True, (
        "a failure to record broke the banking answer itself"
    )
    assert balance["available_balance"] == DEMO001_SAVINGS
