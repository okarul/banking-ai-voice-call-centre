"""Measure how many live voice calls this OpenAI organisation will actually serve.

    python -m loadtest.capacity_smoke --concurrency 2
    python -m loadtest.capacity_smoke --concurrency 3
    python -m loadtest.capacity_smoke --concurrency 5

`--concurrency` is required and has no default. Nothing here ever walks a ladder
on its own: every paid session is one the operator asked for by name. Above 5 an
explicit `--i-accept-the-cost` is also required, because the difference between
typing 5 and typing 30 is twenty-five paid sessions.

For each attempted call it records safe provider metadata only — when it was
attempted, whether it connected, how long the connection took, how long until
the first response, which failure category applied, and any retry-after the
provider volunteered. No key, no PIN, no customer figures, no provider bodies,
no model reasoning.

Use the result to set REALTIME_MAX_ACTIVE_SESSIONS, or to decide that the
organisation's limits need raising before class.
"""

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field

from app.auth import authentication
from app.config import settings
from app.realtime import RealtimeManager
from app.realtime.provider_errors import (
    TIMEOUT,
    UNKNOWN,
    classify_provider_error,
    retry_after_seconds,
)
from app.realtime.realtime_manager import RealtimeSessionError
from app.realtime.turn_gate import record_turn
from app.sessions import SessionManager

from loadtest.live import Recorder, spoke_amount, wait_for_reply
from loadtest.profiles import IDENTITIES, ROTATION

COST_GATE = 5


@dataclass
class Attempt:
    """One call attempt, described in terms an operator can act on."""

    index: int
    customer_id: str
    attempted_at: float
    connected: bool = False
    connect_seconds: float | None = None
    first_response_seconds: float | None = None
    answered_correctly: bool = False
    category: str | None = None
    retry_after: float | None = None
    close_reason: str = "closed cleanly"
    tools: list[str] = field(default_factory=list)

    def line(self, origin: float) -> str:
        connect = (
            f"{self.connect_seconds:6.2f}s" if self.connect_seconds is not None else "    -  "
        )
        first = (
            f"{self.first_response_seconds:7.2f}s"
            if self.first_response_seconds is not None
            else "     -   "
        )
        return (
            f"  #{self.index:<2} {self.customer_id}  t+{self.attempted_at - origin:5.1f}s  "
            f"connect={connect}  first_response={first}  "
            f"{'OK ' if self.answered_correctly else 'NO '} "
            f"{self.category or '-':<22} "
            f"retry_after={self.retry_after if self.retry_after is not None else '-'}"
        )


async def one_attempt(
    index: int, realtime: RealtimeManager, manager: SessionManager, timeout: float
) -> Attempt:
    identity = IDENTITIES[ROTATION[index % len(ROTATION)]]
    attempt = Attempt(index, identity.customer_id, time.monotonic())

    session = manager.create_session()
    session_id = session.session_id
    try:
        await asyncio.to_thread(
            authentication.verify_customer,
            session_id,
            identity.customer_id,
            manager=manager,
        )
        await asyncio.to_thread(
            authentication.verify_pin, session_id, identity.pin, manager=manager
        )

        recorder = Recorder(session_id)
        started = time.monotonic()
        try:
            await realtime.start(session_id, on_event=recorder.handle)
        except RealtimeSessionError as error:
            attempt.category = classify_provider_error(error.__cause__)
            attempt.retry_after = retry_after_seconds(str(error.__cause__ or ""))
            attempt.close_reason = f"connect refused: {error.reason}"
            return attempt

        attempt.connect_seconds = time.monotonic() - started
        attempt.connected = True

        utterance = f"What is my {identity.primary_account.account_type.lower()} account balance?"
        record_turn(manager.get_session(session_id), utterance)

        asked = time.monotonic()
        await realtime.send_message(session_id, utterance)
        completed = await wait_for_reply(recorder, timeout=timeout)

        if recorder.first_audio_at is not None:
            attempt.first_response_seconds = recorder.first_audio_at - asked
        attempt.tools = list(recorder.tools)

        if not completed:
            # Connected, tools may even have run, but the provider never
            # finished the turn. That is saturation, not a banking fault.
            attempt.category = TIMEOUT
            attempt.close_reason = f"no completed response within {timeout:g}s"
            return attempt

        if recorder.errors:
            attempt.category = classify_provider_error(None)
            attempt.close_reason = "stream error"
            return attempt

        attempt.answered_correctly = spoke_amount(
            recorder.said, identity.primary_account.balance
        )
        attempt.category = None if attempt.answered_correctly else UNKNOWN
        return attempt

    except Exception as error:
        attempt.category = classify_provider_error(error)
        attempt.close_reason = type(error).__name__
        return attempt
    finally:
        try:
            await realtime.close(session_id)
        finally:
            manager.destroy_session(session_id)


async def run(concurrency: int, *, stagger: float, timeout: float) -> int:
    if not settings.realtime_configured:
        print("REALTIME_CONNECTION_FAILED\n  no API key configured")
        return 1

    manager = SessionManager()
    # Admission control is bypassed on purpose: this script measures the
    # provider's ceiling, and cannot do that through a limit derived from an
    # earlier measurement of it.
    realtime = RealtimeManager(manager=manager, max_active=0)

    origin = time.monotonic()

    async def staggered(index: int) -> Attempt:
        await asyncio.sleep(stagger * index)
        return await one_attempt(index, realtime, manager, timeout)

    attempts = await asyncio.gather(
        *(staggered(index) for index in range(concurrency)), return_exceptions=False
    )
    await realtime.close_all()

    served = sum(1 for a in attempts if a.answered_correctly)
    print(f"\nconcurrency {concurrency}: {served}/{concurrency} calls fully served")
    for attempt in attempts:
        print(attempt.line(origin))

    categories = sorted({a.category for a in attempts if a.category})
    if categories:
        print(f"\n  failure categories: {', '.join(categories)}")
    print(f"  leftover sessions: {manager.active_session_count()} banking, "
          f"{realtime.active_count()} realtime")

    if served == concurrency:
        print(f"\nCAPACITY_OK at {concurrency}")
        return 0
    print(f"\nCAPACITY_LIMIT_REACHED at {concurrency} ({served} served)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Live realtime capacity smoke test")
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--stagger", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--i-accept-the-cost",
        action="store_true",
        help=f"required above --concurrency {COST_GATE}",
    )
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.concurrency > COST_GATE and not args.i_accept_the_cost:
        parser.error(
            f"--concurrency {args.concurrency} opens {args.concurrency} paid "
            f"sessions. Re-run with --i-accept-the-cost if that is intended."
        )

    return asyncio.run(
        run(args.concurrency, stagger=args.stagger, timeout=args.timeout)
    )


if __name__ == "__main__":
    sys.exit(main())
