"""How many calls are actually open at once in a classroom.

Thirty students is not thirty simultaneous voice calls, and the difference is
the whole capacity question. If students start a minute apart and each call
lasts two minutes, then at any moment roughly two calls are live — the first
student has hung up long before the last one dials.

The relationship is Little's law: for arrival rate L and average call duration
W, the average number in the system is L x W. With one arrival per minute and a
two-minute call that is 2. The peak runs a little above the average because
arrivals and durations are not perfectly even, so this model reports both, and
the recommended capacity is taken from the peak with headroom on top.

    python -m loadtest.classroom
    python -m loadtest.classroom --students 30 --interval 60 --duration 180

No provider is contacted and nothing is billed. This is arithmetic.
"""

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    students: int
    interval: float          # seconds between arrivals
    duration: float          # average call length, seconds

    @property
    def offered_load(self) -> float:
        """Little's law: average calls in progress once the class is flowing."""
        return self.duration / self.interval if self.interval else float(self.students)

    def peak(self) -> int:
        """Highest number of calls open at once, from an exact walk of the timeline.

        Deterministic arrivals and a fixed duration, which is the classroom as
        actually run: the lecturer says "next student, go". Every arrival is an
        event, and the count in progress at that instant is how many earlier
        callers have not yet finished.
        """
        highest = 0
        for index in range(self.students):
            start = index * self.interval
            # Callers still on the line when this one arrives, plus this one.
            in_progress = sum(
                1
                for earlier in range(index + 1)
                if earlier * self.interval + self.duration > start
            )
            highest = max(highest, in_progress)
        return highest

    @property
    def total_minutes(self) -> float:
        """Wall-clock length of the whole session, in minutes."""
        last_start = (self.students - 1) * self.interval
        return (last_start + self.duration) / 60.0


DEFAULT_SCENARIOS = (
    Scenario(30, 60, 120),
    Scenario(30, 60, 180),
    Scenario(30, 60, 300),
    Scenario(20, 60, 120),
    Scenario(20, 60, 180),
    Scenario(20, 60, 300),
    # The pathological case, for contrast: everyone dials at once.
    Scenario(30, 0, 180),
)

HEADER = (
    "Students | Arrival | Call len | Offered load | Peak concurrent | Session length"
)
RULE = "-" * len(HEADER)


def row(scenario: Scenario) -> str:
    arrival = f"{scenario.interval:g}s" if scenario.interval else "all at once"
    return (
        f"{scenario.students:>8} | {arrival:>7} | {scenario.duration:>7.0f}s | "
        f"{scenario.offered_load:>12.1f} | {scenario.peak():>15} | "
        f"{scenario.total_minutes:>11.1f} min"
    )


def recommend(peak: int, *, headroom: float = 2.0, floor: int = 5) -> int:
    """Capacity to ask for, given a peak.

    Headroom is doubled rather than shaved: a classroom demo has no operator
    watching a dashboard, a stalled call is indistinguishable from a broken one
    to a student, and the cost of asking for a slightly higher limit is nothing.
    Not an SLA — a demo-day planning number.
    """
    return max(floor, int(peak * headroom + 0.5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Classroom concurrency model")
    parser.add_argument("--students", type=int)
    parser.add_argument("--interval", type=float, help="seconds between arrivals")
    parser.add_argument("--duration", type=float, help="average call length, seconds")
    args = parser.parse_args()

    if args.students and args.interval is not None and args.duration:
        scenarios = (Scenario(args.students, args.interval, args.duration),)
    else:
        scenarios = DEFAULT_SCENARIOS

    print(HEADER)
    print(RULE)
    for scenario in scenarios:
        print(row(scenario))

    staggered = [s for s in scenarios if s.interval]
    if staggered:
        worst = max(s.peak() for s in staggered)
        print(
            f"\nWorst staggered peak: {worst} simultaneous calls "
            f"-> recommend capacity for {recommend(worst)} "
            f"(peak x2, minimum 5)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
