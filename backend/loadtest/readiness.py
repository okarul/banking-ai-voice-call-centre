"""One command, one verdict: is this machine ready to run a class?

    python -m loadtest.readiness              free checks only
    python -m loadtest.readiness --live       adds one paid Realtime call

Everything an instructor can get wrong on the morning of a demo, checked in
order, with a category attached to each failure so the fix is obvious:

    database reachable and seeded
    backend healthy on 8001
    frontend serving on 5173
    OpenAI key configured, and the account answering
    no banking or realtime sessions left over from anything earlier
    (--live) one real voice call, end to end

The free checks take about a second and cost nothing, so they can be run as
often as the instructor likes. `--live` opens exactly one paid session.

Nothing printed here contains a key, a PIN, a PIN hash, a database password or
a customer's banking values. Failures are reported as a category and a short
safe sentence.
"""

import argparse
import asyncio
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.redaction import redact

BACKEND = "http://127.0.0.1:8001"
FRONTEND = "http://127.0.0.1:5173"

READY = "CLASSROOM READY"
NOT_READY = "CLASSROOM NOT READY"

# Failure categories. Each one points at a different fix.
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
DATABASE_NOT_SEEDED = "DATABASE_NOT_SEEDED"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
FRONTEND_UNAVAILABLE = "FRONTEND_UNAVAILABLE"
REALTIME_NOT_CONFIGURED = "REALTIME_NOT_CONFIGURED"
PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
PROVIDER_CAPACITY_UNAVAILABLE = "PROVIDER_CAPACITY_UNAVAILABLE"
SESSIONS_NOT_CLEAN = "SESSIONS_NOT_CLEAN"
APPLICATION_FAILURE = "APPLICATION_FAILURE"

EXPECTED_CUSTOMERS = 5


@dataclass
class Check:
    name: str
    ok: bool
    category: str | None = None
    detail: str = ""

    def line(self) -> str:
        mark = "  ok  " if self.ok else " FAIL "
        tail = f"  {self.category}: {self.detail}" if not self.ok else (
            f"  {self.detail}" if self.detail else ""
        )
        return f"[{mark}] {self.name}{tail}"


def _get(url: str, timeout: float = 5.0):
    """GET a local URL, returning parsed JSON or raising."""
    import json

    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def check_database() -> Check:
    """The synthetic database is reachable and has its demo customers."""
    try:
        from app.database.connection import session_scope
        from app.database.repositories import count_customers

        with session_scope() as db:
            total = count_customers(db)
    except Exception as error:
        return Check(
            "database reachable", False, DATABASE_UNAVAILABLE,
            redact(type(error).__name__),
        )

    if total < EXPECTED_CUSTOMERS:
        return Check(
            "database seeded", False, DATABASE_NOT_SEEDED,
            f"{total} customers, expected {EXPECTED_CUSTOMERS}",
        )
    return Check("database reachable and seeded", True, detail=f"{total} demo customers")


def check_backend() -> Check:
    try:
        health = _get(f"{BACKEND}/health")
    except (urllib.error.URLError, OSError) as error:
        return Check(
            "backend healthy on 8001", False, BACKEND_UNAVAILABLE,
            redact(type(error).__name__),
        )

    if not isinstance(health, dict) or health.get("status") != "ok":
        return Check("backend healthy on 8001", False, BACKEND_UNAVAILABLE, "no ok")
    return Check("backend healthy on 8001", True)


def check_frontend() -> Check:
    try:
        with urllib.request.urlopen(FRONTEND, timeout=5) as response:
            code = response.getcode()
    except (urllib.error.URLError, OSError) as error:
        return Check(
            "frontend serving on 5173", False, FRONTEND_UNAVAILABLE,
            redact(type(error).__name__),
        )

    if code != 200:
        return Check("frontend serving on 5173", False, FRONTEND_UNAVAILABLE, str(code))
    return Check("frontend serving on 5173", True)


def check_sessions_clean() -> Check:
    """No student's call state may survive into the next class."""
    try:
        active = _get(f"{BACKEND}/api/call/active")
    except (urllib.error.URLError, OSError) as error:
        return Check(
            "no leftover sessions", False, BACKEND_UNAVAILABLE,
            redact(type(error).__name__),
        )

    banking = active.get("active_banking_sessions", -1)
    realtime = active.get("active_browser_calls", -1)
    if banking or realtime:
        return Check(
            "no leftover sessions", False, SESSIONS_NOT_CLEAN,
            f"{banking} banking, {realtime} realtime still active",
        )
    return Check("no leftover sessions", True, detail="0 banking, 0 realtime")


def check_key_configured() -> Check:
    from app.config import settings

    if not settings.realtime_configured:
        return Check(
            "OpenAI key configured", False, REALTIME_NOT_CONFIGURED,
            "OPENAI_API_KEY is not set in backend/.env",
        )
    return Check("OpenAI key configured", True)


def check_account() -> Check:
    """The account answers and has credit.

    A cheap REST call rather than a Realtime session: it costs almost nothing
    and separates "no credit" from "no capacity", which are different problems
    with different fixes.
    """
    import httpx

    from app.config import settings

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                },
            )
    except Exception as error:
        return Check(
            "OpenAI account reachable", False, PROVIDER_UNREACHABLE,
            redact(type(error).__name__),
        )

    if response.status_code == 200:
        remaining = response.headers.get("x-ratelimit-remaining-requests", "?")
        return Check(
            "OpenAI account reachable", True, detail=f"{remaining} requests remaining"
        )

    # Status only. The body can echo request detail and is never printed.
    if response.status_code == 429:
        return Check(
            "OpenAI account reachable", False, QUOTA_UNAVAILABLE,
            "HTTP 429 - out of credit, or rate limited",
        )
    return Check(
        "OpenAI account reachable", False, PROVIDER_UNREACHABLE,
        f"HTTP {response.status_code}",
    )


def check_live_call(timeout: float) -> Check:
    """One real voice call, through the shared smoke test."""
    from loadtest import smoke

    verdict, detail = asyncio.run(smoke.run(timeout))
    if verdict == smoke.PASS:
        return Check("live Realtime smoke test", True, detail=detail)

    category = {
        smoke.QUOTA_UNAVAILABLE: QUOTA_UNAVAILABLE,
        smoke.PROVIDER_CAPACITY_UNAVAILABLE: PROVIDER_CAPACITY_UNAVAILABLE,
        smoke.REALTIME_CONNECTION_FAILED: PROVIDER_UNREACHABLE,
    }.get(verdict, APPLICATION_FAILURE)
    return Check("live Realtime smoke test", False, category, detail)


def main() -> int:
    parser = argparse.ArgumentParser(description="Classroom readiness check")
    parser.add_argument(
        "--live", action="store_true", help="also open one paid Realtime call"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    checks = [
        check_database(),
        check_backend(),
        check_frontend(),
        check_sessions_clean(),
        check_key_configured(),
    ]
    # Only worth asking the provider anything if a key is configured at all.
    if checks[-1].ok:
        checks.append(check_account())
        if args.live:
            checks.append(check_live_call(args.timeout))

    for check in checks:
        print(check.line())

    failed = [check for check in checks if not check.ok]
    print()
    if failed:
        print(NOT_READY)
        for check in failed:
            print(f"  {check.category}: {check.name}")
        return 1

    print(READY)
    if not args.live:
        print("  (free checks only - run with --live to include one voice call)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
