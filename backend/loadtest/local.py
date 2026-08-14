"""Deterministic concurrency harness: everything except the paid provider.

One simulated caller runs the same lifecycle a real browser caller runs, minus
the audio:

    create banking session      session_manager.create_session()
    register the call           browser_call_manager.start()
    authenticate by voice       verify_customer() + verify_pin()
    for each utterance          record_turn()  -> the Phase 11 scope gate
                                handle_turn()  -> routing, tools, guards, DB
    hang up                     browser_call_manager.close()
    end the session             session_manager.destroy_session()
    confirm cleanup             both stores no longer know this call

Every one of those is the production object, not a stand-in. The only thing
replaced is the model: the utterance-to-tool choice is taken from the scenario
rather than from OpenAI, which is what makes this free to run at thirty callers
and what makes a wrong answer unambiguous when it happens.

Two checks run on every single turn, because they are the ones that must never
fail regardless of load:

* **ownership** — the result carries this caller's account, loan and figures
* **isolation** — the result carries *no* marker belonging to any other
  synthetic customer, and `session.customer_id` is still who it was

A caller that fails is cleaned up and recorded. It never takes the harness or
another caller down with it.
"""

import asyncio

from app.agents import handle_turn
from app.auth import authentication
from app.realtime.browser_calls import browser_call_manager
from app.realtime.turn_gate import record_turn
from app.realtime.webrtc import execute_tool
from app.sessions import session_manager

from loadtest.metrics import (
    CallRecord,
    Failure,
    LevelResult,
    Sampler,
    TurnRecord,
    classify_exception,
    stopwatch,
)
from loadtest.profiles import FOREIGN_MARKERS, REFUSAL, CallerPlan, plan_callers

# The tool a refused turn must not have been allowed to reach. Any protected
# tool would do; this one is the one an attacker would actually want.
PROBE_TOOL = "get_account_balance"

# Which tool answered a turn, named from the intent the router settled on. Used
# only for the tool-latency figure; the ownership checks read the result itself.
TOOL_BY_INTENT = {
    "ACCOUNT_BALANCE": "get_account_balance",
    "ACCOUNT_DETAILS": "get_account_details",
    "RECENT_TRANSACTIONS": "get_recent_transactions",
    "LOAN_BALANCE": "get_loan_balance",
    "LOAN_DETAILS": "get_loan_details",
    "NEXT_INSTALMENT": "get_next_instalment",
}


class LeakDetected(Exception):
    """Another customer's value appeared on this caller's line."""


def _scan_for_leaks(record: CallRecord, markers, *fragments: str) -> None:
    """Record any foreign marker found in what this caller was told."""
    haystack = " ".join(fragment for fragment in fragments if fragment)
    for marker in markers:
        if marker in haystack:
            record.leaked.append(marker)


async def _authenticate(plan: CallerPlan, session_id: str) -> bool:
    """Take one caller through the real deterministic identity checks.

    Both steps hit PostgreSQL, so they run in a worker thread exactly as they
    do behind FastAPI. The PIN is passed and immediately forgotten; it is never
    returned, recorded or printed.
    """
    first = await asyncio.to_thread(
        authentication.verify_customer, session_id, plan.identity.customer_id
    )
    if not first.get("success"):
        return False
    second = await asyncio.to_thread(
        authentication.verify_pin, session_id, plan.identity.pin
    )
    return bool(second.get("success"))


async def _run_turn(plan: CallerPlan, record: CallRecord, index: int, turn) -> TurnRecord:
    """One utterance, gated, answered, and checked both ways."""
    session_id = record.banking_session_id
    markers = FOREIGN_MARKERS[plan.identity.customer_id]
    elapsed = stopwatch()

    # 1. The scope gate, exactly as the browser drives it: the caller's words
    #    are classified before the model is allowed to answer them.
    session = session_manager.get_session(session_id)
    record_turn(session, turn.utterance)

    # 2. The answer, through routing, the tool layer and the guards.
    response = await asyncio.to_thread(handle_turn, session_id, turn.utterance)
    latency = elapsed()

    data = response.data or {}
    speech = response.speech or ""

    # 3. Isolation, on every turn without exception.
    _scan_for_leaks(record, markers, str(data), speech)
    live = session_manager.get_session(session_id)
    if live is None or live.customer_id != plan.identity.customer_id:
        record.policy_violations.append(
            f"session bound to {live.customer_id if live else None}"
        )

    # 4. Correctness for this kind of turn.
    if turn.kind == REFUSAL:
        ok = response.success is False and response.reason == turn.reason
        detail = "" if ok else f"expected {turn.reason}, got {response.reason}"
        if data:
            record.policy_violations.append("refused turn carried banking data")
            ok = False

        # A refusal must also stop the *voice* path, which does not go through
        # the supervisor. The real protected tool is invoked exactly as the
        # model would invoke it, and must come back refused with no balance in
        # it — proving the refusal happened in front of the database, not after.
        probe = await execute_tool(
            PROBE_TOOL,
            session_id,
            {"account_type": plan.identity.primary_account.account_type},
        )
        if probe.get("success") is not False or "available_balance" in probe:
            record.policy_violations.append("protected tool ran on a refused turn")
            ok = False
        _scan_for_leaks(record, markers, str(probe))
        if plan.identity.primary_account.balance in str(probe):
            record.policy_violations.append("refused turn returned a balance")
            ok = False
        return TurnRecord(
            plan.scenario.code, index, turn.kind, latency, ok, detail,
            None if ok else Failure.APPLICATION_LOGIC,
        )

    # A data turn: the right intent, the right tool result, the right figures.
    problems = []
    if not response.success:
        problems.append(f"unsuccessful: {response.reason}")
    if turn.intent and response.intent.value != turn.intent:
        problems.append(f"intent {response.intent.value} != {turn.intent}")
    for field, expected in turn.expect.items():
        actual = str(data.get(field, ""))
        if actual != expected:
            problems.append(f"{field}={actual!r} != {expected!r}")

    ok = not problems
    return TurnRecord(
        plan.scenario.code, index, turn.kind, latency, ok, "; ".join(problems),
        None if ok else Failure.APPLICATION_LOGIC,
        tool=TOOL_BY_INTENT.get(response.intent.value),
    )


async def run_caller(plan: CallerPlan) -> CallRecord:
    """One simulated caller, from picking up to hanging up.

    Every failure path still reaches cleanup. A caller that cannot authenticate,
    gets a wrong answer or raises must not leave a banking session or a call
    registration behind, because the next load level starts by requiring that
    both stores are empty.
    """
    record = CallRecord(
        index=plan.index,
        customer_id=plan.identity.customer_id,
        scenario=plan.scenario.code,
    )

    if plan.start_delay:
        await asyncio.sleep(plan.start_delay)

    session_id = None
    try:
        setup = stopwatch()
        session = await asyncio.to_thread(session_manager.create_session)
        session_id = session.session_id
        record.banking_session_id = session_id

        connection = await browser_call_manager.start(session_id)
        record.realtime_session_id = connection.realtime_session_id
        record.setup_latency = setup()
        record.connected = True

        record.authenticated = await _authenticate(plan, session_id)
        if not record.authenticated:
            record.failure = Failure.APPLICATION_LOGIC
            record.detail = "authentication did not succeed"
        else:
            for index, turn in enumerate(plan.scenario.turns):
                record.turns.append(await _run_turn(plan, record, index, turn))
                if record.leaked:
                    raise LeakDetected(", ".join(record.leaked))

    except LeakDetected as error:
        record.failure = Failure.SESSION_ISOLATION
        record.detail = str(error)
    except Exception as error:  # one caller's problem stays one caller's problem
        record.failure = classify_exception(error)
        record.detail = f"{type(error).__name__}"
    finally:
        try:
            if session_id:
                await browser_call_manager.close(session_id)
                await asyncio.to_thread(session_manager.destroy_session, session_id)
                record.cleanup_ok = (
                    session_manager.get_session(session_id) is None
                    and not browser_call_manager.is_active(session_id)
                )
            else:
                # Nothing was ever created, so there is nothing to leave behind.
                record.cleanup_ok = True
        except Exception as error:
            record.failure = record.failure or Failure.CLEANUP
            record.detail = record.detail or type(error).__name__
            record.cleanup_ok = False

    return record


async def run_level(
    concurrency: int,
    *,
    stagger: float = 1.0,
    burst: int = 0,
    label: str = "",
) -> LevelResult:
    """Run one concurrency level end to end and measure it."""
    result = LevelResult(
        label=label or f"{concurrency} callers",
        concurrency=concurrency,
    )

    # No level may inherit the previous level's mess.
    await browser_call_manager.close_all()
    session_manager.clear()

    sampler = Sampler()
    watcher = asyncio.create_task(sampler.run())
    elapsed = stopwatch()

    plans = plan_callers(concurrency, stagger=stagger, burst=burst)
    outcomes = await asyncio.gather(
        *(run_caller(plan) for plan in plans), return_exceptions=True
    )

    result.duration = elapsed()
    sampler.stop()
    watcher.cancel()

    for plan, outcome in zip(plans, outcomes):
        if isinstance(outcome, BaseException):
            # The harness itself failed for this caller, which is its own
            # category — it is not evidence about the application.
            broken = CallRecord(
                index=plan.index,
                customer_id=plan.identity.customer_id,
                scenario=plan.scenario.code,
                failure=Failure.TEST_HARNESS,
                detail=type(outcome).__name__,
            )
            result.calls.append(broken)
        else:
            result.calls.append(outcome)

    # Session ids must be unique across the whole level: two callers sharing one
    # would be a collision, and every isolation guarantee rests on them not.
    ids = [call.banking_session_id for call in result.calls if call.banking_session_id]
    if len(set(ids)) != len(ids):
        result.notes.append("SESSION ID COLLISION")

    realtime_ids = [
        call.realtime_session_id for call in result.calls if call.realtime_session_id
    ]
    if len(set(realtime_ids)) != len(realtime_ids):
        result.notes.append("REALTIME SESSION ID COLLISION")

    result.peak_banking_sessions = sampler.peak_banking
    result.peak_realtime_sessions = sampler.peak_realtime
    result.peak_db_checked_out = sampler.peak_checked_out
    result.peak_memory_mb = sampler.peak_memory
    result.cpu_seconds = sampler.cpu_used
    result.orphan_banking_sessions = session_manager.active_session_count()
    result.orphan_realtime_sessions = browser_call_manager.active_count()

    return result
