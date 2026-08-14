"""Run the live OpenAI Realtime concurrency ladder. This spends paid credit.

    python -m loadtest.run_live 2
    python -m loadtest.run_live 2 5 --stagger 1

Levels must be named explicitly. There is no default ladder on purpose: the
difference between typing `2` and typing `30` is the difference between two paid
sessions and thirty, and that should never be a default.

The ladder stops climbing on the first mandatory failure, and also on the first
provider condition — a rate limit is the provider saying it does not want this
concurrency, and the right response is to record it and stop rather than to
retry around it.
"""

import argparse
import asyncio
import sys

from loadtest.live import run_live_level
from loadtest.metrics import Failure
from loadtest.report import blocking_problem, level_detail, table

# Provider conditions that mean "stop increasing load", as distinct from a
# defect in this application.
PROVIDER_STOPS = {
    Failure.RATE_LIMIT,
    Failure.QUOTA,
    Failure.REALTIME_PROVIDER,
    # A turn the provider never finished is the soft form of the same signal.
    Failure.TIMEOUT,
}


async def ladder(levels, *, stagger: float, timeout: float, offset: int = 0) -> int:
    results = []
    stopped = None

    for concurrency in levels:
        label = f"LIVE {concurrency} callers, {stagger:g}s apart"
        print(f"\n>>> starting {label} ({concurrency} paid sessions)", flush=True)
        result = await run_live_level(
            concurrency, stagger=stagger, label=label, timeout=timeout, offset=offset
        )
        results.append(result)
        print(level_detail(result), flush=True)

        failures = result.failure_counts()
        provider = {k: v for k, v in failures.items() if k in PROVIDER_STOPS}
        problem = blocking_problem(result)

        if provider:
            print(f"\n  STOP: provider conditions at {concurrency} callers: {provider}")
            stopped = concurrency
            break
        if problem:
            print(f"\n  STOP: {label} failed a mandatory threshold: {problem}")
            stopped = concurrency
            break

    print("\n" + "=" * 70)
    print("LIVE REALTIME CONCURRENCY")
    print("=" * 70)
    print(table(results))
    if stopped:
        print(f"\nLive ladder stopped at {stopped} callers.")
        return 1
    print("\nAll live levels attempted passed the mandatory thresholds.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live realtime load ladder")
    parser.add_argument("levels", nargs="+", type=int)
    parser.add_argument("--stagger", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    return asyncio.run(
        ladder(
            args.levels,
            stagger=args.stagger,
            timeout=args.timeout,
            offset=args.offset,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
