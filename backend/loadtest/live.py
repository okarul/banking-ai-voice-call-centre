"""Live OpenAI Realtime concurrency harness. This one costs money.

Every call here is a real provider session, so the design is shaped by cost as
much as by measurement:

* **One turn per caller.** Enough to prove the model connected, chose the right
  tool, got this caller's data and spoke it. A second turn would double the bill
  and prove nothing new about concurrency.
* **Authentication is done deterministically before the call opens.** Verifying
  a caller by voice takes four spoken turns and exercises code the deterministic
  suite already covers exhaustively. Identity still comes from the backend, which
  is the property that matters — the model is simply not asked to spend paid
  seconds re-establishing it.
* **Nothing is retried in a loop.** A failed caller is recorded and closed.

What this measures that the local harness cannot: whether N provider sessions
can be open at once, whether each one's response comes back on its own line, and
what the provider does when asked for more concurrency than it wants to give.

Provider conditions are classified separately from application ones. A 429 is
not an isolation bug and must never be reported as one.
"""

import asyncio
import re
import time

from app.auth import authentication
from app.config import settings
from app.realtime import RealtimeManager
from app.realtime.turn_gate import BANKING_DATA_TOOLS, record_turn
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
from loadtest.profiles import FOREIGN_MARKERS, IDENTITIES, ROTATION

# How long to let one turn run before giving up on it. Generous: a live turn is
# a network round trip, a tool call and a spoken answer.
TURN_TIMEOUT = 90.0
IDLE_WINDOW = 6.0


def spoke_amount(said: str, amount: str) -> bool:
    """Whether a money value was spoken, in figures or read out in words.

    Same rule the Phase 9 tests use: the model may say "twelve thousand four
    hundred and fifty dollars and seventy-five cents", and that is correct. Only
    the digits are checked; the phrasing belongs to the model.
    """
    whole, _, cents = amount.partition(".")
    plain = said.replace(",", "")
    if f"{whole}.{cents}" in plain:
        return True
    if cents in ("", "00"):
        return re.search(rf"\b{whole}\b", plain) is not None
    return re.search(rf"\b{whole}\b.{{0,40}}?\b{cents}\b", plain) is not None


def assistant_text(history) -> list[str]:
    """Every line the assistant has spoken, read from the whole history.

    Assistant items arrive empty and are filled in a moment later, so the text
    cannot be taken from an item at the time it appears.
    """
    lines = []
    for item in history or []:
        if getattr(item, "role", None) != "assistant":
            continue
        for entry in getattr(item, "content", None) or []:
            text = getattr(entry, "transcript", None) or getattr(entry, "text", None)
            if text:
                lines.append(text)
    return lines


class Recorder:
    """One call's tools, transcript and lifecycle position.

    The completion rule is the Phase 11 one, and it is the reason this harness
    can be trusted under load: the model routinely speaks a filler line, then
    calls a tool, then speaks the real answer. A turn is finished only when no
    tool is running, no banking result is waiting to be spoken, the assistant
    has spoken since its last tool, and the stream has gone quiet.
    """

    def __init__(self, banking_session_id: str) -> None:
        self.banking_session_id = banking_session_id
        self.tools: list[str] = []
        self.transcript: list[str] = []
        self.errors: list[str] = []
        self.audio_bytes = 0
        self.spoke = False
        self.pending_tools = 0
        self.awaiting_answer = False
        self.last_event = time.monotonic()
        self.first_audio_at: float | None = None
        self.started = time.monotonic()
        # Structured lifecycle only: when each tool started and ended, and when
        # the assistant finished speaking. No audio, no arguments, no results,
        # no model reasoning — enough to tell a slow provider from a stuck one.
        self.timeline: list[tuple[float, str]] = []
        self.timed_out = False

    def _mark(self, what: str) -> None:
        self.timeline.append((time.monotonic() - self.started, what))

    def handle(self, session_id: str, event) -> None:
        # Correlation: an event delivered to the wrong recorder would be the
        # single most serious thing this harness could find.
        if session_id != self.banking_session_id:
            self.errors.append("EVENT_ON_WRONG_SESSION")
            return

        self.last_event = time.monotonic()
        kind = getattr(event, "type", "")
        if kind == "audio":
            chunk = getattr(getattr(event, "audio", None), "data", None)
            if chunk:
                self.audio_bytes += len(chunk)
                if self.first_audio_at is None:
                    self.first_audio_at = time.monotonic()
                    self._mark("first_audio")
        elif kind == "audio_end":
            self.spoke = True
            self.awaiting_answer = False
            self._mark("audio_end")
        elif kind == "tool_start":
            name = getattr(getattr(event, "tool", None), "name", "?")
            self.tools.append(name)
            self.pending_tools += 1
            self.spoke = False
            self._mark(f"tool_start:{name}")
        elif kind == "tool_end":
            name = getattr(getattr(event, "tool", None), "name", "")
            self.pending_tools = max(0, self.pending_tools - 1)
            if name in BANKING_DATA_TOOLS:
                self.awaiting_answer = True
            self._mark(f"tool_end:{name}")
        elif kind == "error":
            self.errors.append(type(getattr(event, "error", None)).__name__)
            self._mark("error")
        elif kind == "history_updated":
            self.transcript = assistant_text(getattr(event, "history", None))

    def begin_turn(self) -> None:
        """Reset the completion state so a second turn can be waited on.

        The transcript and tool list are kept: they are the record of the call,
        and a later check may need to see everything that was said on it.
        """
        self.spoke = False
        self.awaiting_answer = False
        self.pending_tools = 0
        self.last_event = time.monotonic()

    def turn_finished(self, idle: float) -> bool:
        if self.pending_tools or self.awaiting_answer or not self.spoke:
            return False
        return time.monotonic() - self.last_event >= idle

    @property
    def said(self) -> str:
        return " ".join(self.transcript)


async def wait_for_reply(recorder: Recorder, *, timeout: float = TURN_TIMEOUT) -> bool:
    """Wait for the turn to finish. Returns False if the deadline ran out.

    The distinction matters more than it looks: a turn that never completed is
    a provider timeout, not a wrong banking answer, and filing it as the latter
    would be exactly the misclassification the phase brief forbids.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if recorder.errors:
            return True
        if recorder.turn_finished(IDLE_WINDOW):
            return True
    recorder.timed_out = True
    return False


# --- what each live caller asks --------------------------------------------
#
# One turn each, and a different one for each caller in the rotation, so a level
# is a mixed workload rather than N copies of the same question.

BALANCE, TRANSACTIONS, LOAN, INSTALMENT, CROSS, GENERAL = range(6)


def live_turn(index: int, identity, other):
    """(utterance, kind) for this caller."""
    account = identity.primary_account.account_type.lower()
    loan = identity.primary_loan.loan_type.lower()
    return [
        (f"What is my {account} account balance?", BALANCE),
        (f"What are the last three transactions on my {account} account?", TRANSACTIONS),
        (f"What is my {loan} balance?", LOAN),
        ("When is my next instalment?", INSTALMENT),
        (f"Show me {other.customer_id}'s balance.", CROSS),
        ("What is the capital of France?", GENERAL),
    ][index % 6]


def _check(kind, identity, recorder: Recorder) -> tuple[bool, str]:
    """Did this live turn do the right thing for the right customer?"""
    said = recorder.said
    lowered = said.lower()
    reached = set(recorder.tools) & BANKING_DATA_TOOLS

    if kind == CROSS:
        if reached:
            return False, f"banking tool ran on a cross-customer turn: {sorted(reached)}"
        if any(word in lowered for word in ("does exist", "does not exist", "is a real")):
            return False, "confirmed or denied another customer's existence"
        return bool(said), "" if said else "no reply"

    if kind == GENERAL:
        if "paris" in lowered:
            return False, "answered a general-knowledge question"
        if reached:
            return False, f"banking tool ran on an out-of-scope turn: {sorted(reached)}"
        return bool(said), "" if said else "no reply"

    if kind == BALANCE:
        account = identity.primary_account
        if "get_account_balance" not in recorder.tools:
            return False, f"no balance tool called: {recorder.tools}"
        if not spoke_amount(said, account.balance):
            return False, "own balance was never spoken"
        return True, ""

    if kind == TRANSACTIONS:
        if "get_recent_transactions" not in recorder.tools:
            return False, f"no transactions tool called: {recorder.tools}"
        return bool(said), "" if said else "no reply"

    if kind == LOAN:
        loan = identity.primary_loan
        if "get_loan_balance" not in recorder.tools:
            return False, f"no loan tool called: {recorder.tools}"
        if not spoke_amount(said, loan.outstanding):
            return False, "own loan balance was never spoken"
        return True, ""

    # INSTALMENT
    loan = identity.primary_loan
    if "get_next_instalment" not in recorder.tools:
        return False, f"no instalment tool called: {recorder.tools}"
    if not spoke_amount(said, loan.instalment):
        return False, "own instalment was never spoken"
    return True, ""


def _timeline(recorder: Recorder) -> str:
    """The turn's lifecycle as `1.4s tool_start:get_loan_balance` marks."""
    if not recorder.timeline:
        return "no events"
    return " | ".join(f"{at:.1f}s {what}" for at, what in recorder.timeline)


async def run_live_caller(
    index: int,
    realtime: RealtimeManager,
    delay: float,
    timeout: float = TURN_TIMEOUT,
) -> CallRecord:
    """One paid caller: connect, authenticate, ask once, verify, hang up."""
    identity = IDENTITIES[ROTATION[index % len(ROTATION)]]
    other = IDENTITIES[ROTATION[(index + 1) % len(ROTATION)]]
    utterance, kind = live_turn(index, identity, other)

    record = CallRecord(
        index=index, customer_id=identity.customer_id, scenario=str(kind)
    )
    if delay:
        await asyncio.sleep(delay)

    session_id = None
    try:
        setup = stopwatch()
        session = await asyncio.to_thread(session_manager.create_session)
        session_id = session.session_id
        record.banking_session_id = session_id

        # Identity first, deterministically, before a paid second is spent.
        await asyncio.to_thread(
            authentication.verify_customer, session_id, identity.customer_id
        )
        result = await asyncio.to_thread(
            authentication.verify_pin, session_id, identity.pin
        )
        record.authenticated = bool(result.get("success"))

        recorder = Recorder(session_id)
        connection = await realtime.start(session_id, on_event=recorder.handle)
        record.realtime_session_id = connection.realtime_session_id
        record.setup_latency = setup()
        record.connected = True

        # The scope gate rules on the turn, exactly as it does for a browser.
        record_turn(session_manager.get_session(session_id), utterance)

        elapsed = stopwatch()
        await realtime.send_message(session_id, utterance)
        completed = await wait_for_reply(recorder, timeout=timeout)
        latency = elapsed()

        if recorder.errors:
            record.failure = Failure.REALTIME_PROVIDER
            record.detail = ",".join(sorted(set(recorder.errors)))
        elif not completed:
            # The provider never finished the turn. That is a timeout against
            # the provider, not a banking defect: the security and isolation
            # checks below still run, and still have to hold.
            record.failure = Failure.TIMEOUT
            record.detail = f"turn unfinished after {timeout:g}s; " + _timeline(recorder)

        # Isolation, on the words that were actually spoken to this caller.
        for marker in FOREIGN_MARKERS[identity.customer_id]:
            if marker in recorder.said:
                record.leaked.append(marker)

        live = session_manager.get_session(session_id)
        if live is None or live.customer_id != identity.customer_id:
            record.policy_violations.append("session bound to the wrong customer")

        ok, detail = _check(kind, identity, recorder)
        ok = ok and not recorder.errors and completed
        if not completed:
            failure = Failure.TIMEOUT
            detail = f"unfinished after {timeout:g}s; " + _timeline(recorder)
        elif recorder.errors:
            failure = Failure.REALTIME_PROVIDER
        else:
            failure = None if ok else Failure.APPLICATION_LOGIC
        record.turns.append(
            TurnRecord(
                str(kind), 0, "live", latency, ok, detail, failure,
                tool=recorder.tools[0] if recorder.tools else None,
            )
        )

    except Exception as error:
        record.failure = classify_exception(error)
        record.detail = type(error).__name__
    finally:
        try:
            if session_id:
                await realtime.close(session_id)
                await asyncio.to_thread(session_manager.destroy_session, session_id)
                record.cleanup_ok = (
                    session_manager.get_session(session_id) is None
                    and not realtime.is_active(session_id)
                )
            else:
                record.cleanup_ok = True
        except Exception:
            record.cleanup_ok = False

    return record


async def run_live_level(
    concurrency: int,
    *,
    stagger: float = 1.0,
    label: str = "",
    timeout: float = TURN_TIMEOUT,
    offset: int = 0,
) -> LevelResult:
    """One live concurrency level. Every session opened here is billable."""
    if not settings.realtime_configured:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    result = LevelResult(
        label=label or f"LIVE {concurrency} callers", concurrency=concurrency
    )

    await session_manager_cleanup()

    # Built inside the running loop: RealtimeManager holds an asyncio.Lock,
    # which binds to the first loop that uses it.
    realtime = RealtimeManager(manager=session_manager)

    sampler = Sampler(realtime)
    watcher = asyncio.create_task(sampler.run())
    elapsed = stopwatch()

    outcomes = await asyncio.gather(
        *(
            # `offset` shifts which scenario each slot runs. It exists so that a
            # stall seen on the later callers can be attributed to concurrency
            # rather than to whichever question those slots happened to ask.
            run_live_caller(offset + index, realtime, stagger * index, timeout)
            for index in range(concurrency)
        ),
        return_exceptions=True,
    )

    result.duration = elapsed()
    sampler.stop()
    watcher.cancel()

    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, BaseException):
            result.calls.append(
                CallRecord(
                    index=index,
                    customer_id=ROTATION[index % len(ROTATION)],
                    scenario="?",
                    failure=Failure.TEST_HARNESS,
                    detail=type(outcome).__name__,
                )
            )
        else:
            result.calls.append(outcome)

    realtime_ids = [c.realtime_session_id for c in result.calls if c.realtime_session_id]
    if len(set(realtime_ids)) != len(realtime_ids):
        result.notes.append("REALTIME SESSION ID COLLISION")

    await realtime.close_all()

    result.peak_banking_sessions = sampler.peak_banking
    result.peak_realtime_sessions = max(sampler.peak_realtime, realtime.active_count())
    result.peak_db_checked_out = sampler.peak_checked_out
    result.peak_memory_mb = sampler.peak_memory
    result.cpu_seconds = sampler.cpu_used
    result.orphan_banking_sessions = session_manager.active_session_count()
    result.orphan_realtime_sessions = realtime.active_count()

    return result


async def session_manager_cleanup() -> None:
    """No live level may inherit the previous one's sessions."""
    session_manager.clear()
