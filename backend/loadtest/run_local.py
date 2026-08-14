"""Run the deterministic concurrency ladder.

    python -m loadtest.run_local              2, 5, 10, 20, 30 staggered
    python -m loadtest.run_local 5 10         only those levels
    python -m loadtest.run_local --burst      arrivals inside the same instant
    python -m loadtest.run_local --stagger 0.5

The ladder stops climbing the moment a level fails a mandatory threshold. That
is the point of a stepwise gate: a leak at ten callers is not made clearer by
also running thirty.
"""

import argparse
import asyncio
import sys

from loadtest.local import run_level
from loadtest.report import blocking_problem, level_detail, table

DEFAULT_LEVELS = (2, 5, 10, 20, 30)


async def ladder(levels, *, stagger: float, burst: bool) -> int:
    results = []
    stopped_at = None

    for concurrency in levels:
        # Two arrival patterns, and they measure different things. Staggered is
        # the classroom reality — callers dial in one after another, so few
        # calls are ever open at once. Simultaneous is the harsher case and the
        # only one that actually holds N sessions concurrently, which is what
        # the SessionManager and the connection pool have to be judged on.
        burst_size = concurrency if burst else 0
        label = (
            f"LOCAL {concurrency} callers, "
            + ("all arriving together" if burst_size else f"{stagger:g}s apart")
        )
        result = await run_level(
            concurrency, stagger=stagger, burst=burst_size, label=label
        )
        results.append(result)
        print(level_detail(result), flush=True)

        problem = blocking_problem(result)
        if problem:
            print(f"\n  STOP: {label} failed a mandatory threshold: {problem}")
            stopped_at = concurrency
            break

    print("\n" + "=" * 70)
    print("LOCAL / DETERMINISTIC CONCURRENCY")
    print("=" * 70)
    print(table(results))
    if stopped_at:
        print(f"\nLadder stopped at {stopped_at} callers.")
        return 1
    print("\nAll local levels passed the mandatory thresholds.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic load ladder")
    parser.add_argument("levels", nargs="*", type=int, default=list(DEFAULT_LEVELS))
    parser.add_argument("--stagger", type=float, default=1.0)
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()

    return asyncio.run(
        ladder(args.levels or list(DEFAULT_LEVELS),
               stagger=args.stagger, burst=args.burst)
    )


if __name__ == "__main__":
    sys.exit(main())
