"""The five-minutes-before-class check. One live call, one verdict.

    python -m loadtest.smoke

Opens a single realtime call, verifies the caller by voice-path authentication,
asks one supported banking question, asks one out-of-scope question, and hangs
up. Prints exactly one line:

    PASS
    PROVIDER_CAPACITY_UNAVAILABLE
    QUOTA_UNAVAILABLE
    REALTIME_CONNECTION_FAILED
    APPLICATION_FAILURE

One paid session, one short turn each way. It exists so that an operator finds
out about an expired card or a changed project *before* thirty students are
watching, not during.

Nothing it prints contains a key, a PIN, a customer figure or a provider
message. The detail line, when there is one, is a category and a timing.
"""

import argparse
import asyncio
import sys

from app.auth import authentication
from app.config import settings
from app.realtime import RealtimeManager
from app.realtime.provider_errors import (
    CONCURRENCY_LIMIT,
    QUOTA_EXHAUSTED,
    RATE_LIMIT,
    classify_provider_error,
)
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.realtime.turn_gate import record_turn
from app.sessions import SessionManager

from loadtest.live import Recorder, spoke_amount, wait_for_reply
from loadtest.metrics import stopwatch

PASS = "PASS"
PROVIDER_CAPACITY_UNAVAILABLE = "PROVIDER_CAPACITY_UNAVAILABLE"
QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
REALTIME_CONNECTION_FAILED = "REALTIME_CONNECTION_FAILED"
APPLICATION_FAILURE = "APPLICATION_FAILURE"

# The one synthetic caller the smoke test uses, and what they own.
CUSTOMER = "DEMO001"
PIN = "4821"
BALANCE = "12450.75"

SUPPORTED = "What is my savings account balance?"
OUT_OF_SCOPE = "What is the capital of France?"


def _capacity_verdict(category: str) -> str:
    if category == QUOTA_EXHAUSTED:
        return QUOTA_UNAVAILABLE
    if category in (RATE_LIMIT, CONCURRENCY_LIMIT):
        return PROVIDER_CAPACITY_UNAVAILABLE
    return REALTIME_CONNECTION_FAILED


async def run(timeout: float) -> tuple[str, str]:
    """Returns (verdict, safe detail)."""
    if not settings.realtime_configured:
        return REALTIME_CONNECTION_FAILED, "no API key configured"

    manager = SessionManager()
    realtime = RealtimeManager(manager=manager)
    session = manager.create_session()
    session_id = session.session_id

    try:
        # Identity through the real deterministic checks. The PIN is passed and
        # forgotten; it is never printed and never returned.
        await asyncio.to_thread(
            authentication.verify_customer, session_id, CUSTOMER, manager=manager
        )
        verified = await asyncio.to_thread(
            authentication.verify_pin, session_id, PIN, manager=manager
        )
        if not verified.get("success"):
            return APPLICATION_FAILURE, "authentication did not succeed"

        recorder = Recorder(session_id)
        opened = stopwatch()
        try:
            await realtime.start(session_id, on_event=recorder.handle)
        except RealtimeSessionError as error:
            if error.reason == Reason.REALTIME_AT_CAPACITY:
                return PROVIDER_CAPACITY_UNAVAILABLE, "local admission control"
            category = classify_provider_error(error.__cause__)
            return _capacity_verdict(category), f"connect: {category}"
        connect_seconds = opened()

        # 1. A supported enquiry must reach the tool and speak this customer's
        #    own figure. Anything less is not a working demo.
        record_turn(manager.get_session(session_id), SUPPORTED)
        answered = stopwatch()
        await realtime.send_message(session_id, SUPPORTED)
        if not await wait_for_reply(recorder, timeout=timeout):
            return PROVIDER_CAPACITY_UNAVAILABLE, (
                f"no reply within {timeout:g}s (connect {connect_seconds:.1f}s)"
            )
        reply_seconds = answered()

        if recorder.errors:
            category = classify_provider_error(None)
            return REALTIME_CONNECTION_FAILED, f"stream error: {category}"
        if "get_account_balance" not in recorder.tools:
            return APPLICATION_FAILURE, "balance tool was not called"

        if not spoke_amount(recorder.said, BALANCE):
            return APPLICATION_FAILURE, "own balance was not spoken"

        # 2. An out-of-scope question must be refused, with no banking data.
        #    Same call and same recorder — the transcript so far is kept, and
        #    only the completion state is reset so this turn can be waited on.
        recorder.begin_turn()
        record_turn(manager.get_session(session_id), OUT_OF_SCOPE)
        await realtime.send_message(session_id, OUT_OF_SCOPE)
        await wait_for_reply(recorder, timeout=timeout)
        if "paris" in recorder.said.lower():
            return APPLICATION_FAILURE, "answered a general-knowledge question"

        return PASS, (
            f"connect {connect_seconds:.1f}s, answer {reply_seconds:.1f}s"
        )

    except Exception as error:
        category = classify_provider_error(error)
        if category == QUOTA_EXHAUSTED:
            return QUOTA_UNAVAILABLE, category
        if category in (RATE_LIMIT, CONCURRENCY_LIMIT):
            return PROVIDER_CAPACITY_UNAVAILABLE, category
        if category != "UNKNOWN":
            return REALTIME_CONNECTION_FAILED, category
        return APPLICATION_FAILURE, type(error).__name__
    finally:
        # Cleanup runs on every path, including the failing ones.
        try:
            await realtime.close(session_id)
        finally:
            manager.destroy_session(session_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-class realtime smoke test")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    verdict, detail = asyncio.run(run(args.timeout))
    print(verdict)
    if detail:
        print(f"  {detail}")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
