"""Measurement, classification and resource sampling for the load harness.

Three ideas here:

* **Every failure is classified.** A provider rate limit and a customer-isolation
  bug are both "a call that did not succeed", and reporting them as one number
  would be the single most misleading thing this harness could do. `Failure` is
  the taxonomy; nothing is allowed to be counted without one.
* **Latency is reported, not judged.** p50/p95/max are measured and printed. No
  threshold is invented here — Phase 12 is an assessment, and an arbitrary SLA
  would turn an observation into a false verdict.
* **Nothing sensitive is recorded.** Records carry session ids, customer ids,
  tool names and timings. They never carry a PIN, a credential, an utterance
  from an authentication turn, or a provider error body.
"""

import ctypes
import ctypes.wintypes
import os
import sys
import time
from dataclasses import dataclass, field


class Failure:
    """Why one simulated caller did not complete successfully."""

    APPLICATION_LOGIC = "APPLICATION_LOGIC"
    SESSION_ISOLATION = "SESSION_ISOLATION"
    DATABASE = "DATABASE"
    REALTIME_PROVIDER = "REALTIME_PROVIDER"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA = "QUOTA"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    TEST_HARNESS = "TEST_HARNESS"
    CLEANUP = "CLEANUP"
    UNKNOWN = "UNKNOWN"


# Substrings that identify a provider condition rather than an application one.
# Matched against the exception *type name* and a lowercased message, so a
# quota problem is never filed as an isolation bug.
_PROVIDER_SIGNS = (
    (Failure.QUOTA, ("insufficient_quota", "credit_balance", "billing", "quota")),
    (Failure.RATE_LIMIT, ("rate_limit", "429", "too many requests", "concurrent")),
    (Failure.TIMEOUT, ("timeout", "timederror", "timed out")),
    (
        Failure.NETWORK,
        ("connectionclosed", "connectionreset", "connecterror", "websocket",
         "connection refused", "eof", "ssl"),
    ),
)

_DATABASE_SIGNS = (
    "operationalerror", "interfaceerror", "dbapierror", "sqlalchemy",
    "queuepool", "connection pool", "psycopg", "too many clients",
)


def classify_exception(error: BaseException) -> str:
    """Put one exception into the failure taxonomy.

    Deliberately conservative: anything that cannot be recognised becomes
    UNKNOWN rather than being folded into a neighbouring category. An
    over-broad classifier is how a real isolation defect gets reported as a
    network blip.
    """
    name = type(error).__name__.lower()
    text = f"{name} {error}".lower()

    if any(sign in text for sign in _DATABASE_SIGNS):
        return Failure.DATABASE
    for category, signs in _PROVIDER_SIGNS:
        if any(sign in text for sign in signs):
            return category
    if name in {"assertionerror"}:
        return Failure.APPLICATION_LOGIC
    return Failure.UNKNOWN


@dataclass
class TurnRecord:
    """One caller utterance and what came back."""

    scenario: str
    index: int
    kind: str
    latency: float
    ok: bool
    detail: str = ""
    failure: str | None = None
    # Which tool the routing layer actually reached, for the tool-latency figure.
    tool: str | None = None


@dataclass
class CallRecord:
    """One simulated caller, start to cleanup."""

    index: int
    customer_id: str
    scenario: str
    banking_session_id: str | None = None
    realtime_session_id: str | None = None
    setup_latency: float = 0.0
    connected: bool = False
    authenticated: bool = False
    turns: list[TurnRecord] = field(default_factory=list)
    cleanup_ok: bool = False
    failure: str | None = None
    detail: str = ""
    leaked: list[str] = field(default_factory=list)
    policy_violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.connected
            and self.authenticated
            and self.cleanup_ok
            and self.failure is None
            and all(turn.ok for turn in self.turns)
            and not self.leaked
            and not self.policy_violations
        )


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


@dataclass
class LevelResult:
    """Everything measured at one concurrency level."""

    label: str
    concurrency: int
    calls: list[CallRecord] = field(default_factory=list)
    duration: float = 0.0
    peak_banking_sessions: int = 0
    peak_realtime_sessions: int = 0
    peak_db_checked_out: int = 0
    orphan_banking_sessions: int = 0
    orphan_realtime_sessions: int = 0
    peak_memory_mb: float = 0.0
    cpu_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    # --- derived counts ---------------------------------------------------

    @property
    def attempted(self) -> int:
        return len(self.calls)

    @property
    def connected(self) -> int:
        return sum(1 for call in self.calls if call.connected)

    @property
    def connect_failures(self) -> int:
        return self.attempted - self.connected

    @property
    def authenticated(self) -> int:
        return sum(1 for call in self.calls if call.authenticated)

    @property
    def auth_failures(self) -> int:
        return self.connected - self.authenticated

    @property
    def successful(self) -> int:
        return sum(1 for call in self.calls if call.ok)

    @property
    def failed(self) -> int:
        return self.attempted - self.successful

    @property
    def responses_ok(self) -> int:
        return sum(1 for call in self.calls for turn in call.turns if turn.ok)

    @property
    def responses_wrong(self) -> int:
        return sum(1 for call in self.calls for turn in call.turns if not turn.ok)

    @property
    def leakage(self) -> int:
        return sum(len(call.leaked) for call in self.calls)

    @property
    def policy_violations(self) -> int:
        return sum(len(call.policy_violations) for call in self.calls)

    @property
    def cleanup_failures(self) -> int:
        return sum(1 for call in self.calls if not call.cleanup_ok)

    @property
    def latencies(self) -> list[float]:
        return [turn.latency for call in self.calls for turn in call.turns]

    @property
    def setup_latencies(self) -> list[float]:
        return [call.setup_latency for call in self.calls if call.connected]

    @property
    def tool_latencies(self) -> list[float]:
        return [
            turn.latency
            for call in self.calls
            for turn in call.turns
            if turn.tool is not None
        ]

    def failure_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            for label in [call.failure] + [t.failure for t in call.turns]:
                if label:
                    counts[label] = counts.get(label, 0) + 1
        return counts

    @property
    def correctness_rate(self) -> float:
        """Correct banking answers as a share of answers attempted."""
        total = self.responses_ok + self.responses_wrong
        return 100.0 if total == 0 else 100.0 * self.responses_ok / total

    def summary_row(self) -> str:
        latencies = self.latencies
        return (
            f"{self.concurrency:>5} | {self.attempted:>8} | {self.successful:>7} | "
            f"{self.failed:>4} | {percentile(latencies, 0.50) * 1000:>7.1f} | "
            f"{percentile(latencies, 0.95) * 1000:>7.1f} | "
            f"{(max(latencies) if latencies else 0) * 1000:>7.1f} | "
            f"{self.leakage:>7} | "
            f"{'OK' if self.cleanup_failures == 0 else 'FAIL':>7}"
        )


TABLE_HEADER = (
    "Load |  Attempts | Success | Fail |  p50 ms |  p95 ms |  max ms | "
    "Leakage | Cleanup"
)
TABLE_RULE = "-" * len(TABLE_HEADER)


# --- resource sampling ------------------------------------------------------
#
# Windows-native and stdlib only. Adding a measurement dependency for a
# measurement phase would be its own small irony, and `psutil` is not installed.


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _memory_probe():
    """Bind GetProcessMemoryInfo once, from whichever library exports it.

    Modern Windows forwards the psapi entry point into kernel32 as
    `K32GetProcessMemoryInfo`; older ones export it from psapi.dll. Trying both
    is cheaper than guessing, and a missing entry point simply means the memory
    figure is reported as unavailable rather than as zero.
    """
    if sys.platform != "win32":
        return None
    for library, name in (
        (ctypes.windll.kernel32, "K32GetProcessMemoryInfo"),
        (ctypes.windll.psapi, "GetProcessMemoryInfo"),
    ):
        function = getattr(library, name, None)
        if function is None:
            continue
        function.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.wintypes.DWORD,
        ]
        function.restype = ctypes.wintypes.BOOL
        return function
    return None


_GET_PROCESS_MEMORY_INFO = _memory_probe()


def memory_mb() -> tuple[float, float]:
    """(current, peak) working set in MB. Returns (0, 0) when unavailable."""
    if _GET_PROCESS_MEMORY_INFO is None:
        return 0.0, 0.0
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not _GET_PROCESS_MEMORY_INFO(handle, ctypes.byref(counters), counters.cb):
        return 0.0, 0.0
    megabyte = 1024 * 1024
    return counters.WorkingSetSize / megabyte, counters.PeakWorkingSetSize / megabyte


def cpu_seconds() -> float:
    """Total CPU time this process has consumed, user plus kernel."""
    times = os.times()
    return times.user + times.system


def db_checked_out() -> int:
    """Connections currently checked out of the SQLAlchemy pool.

    Returns -1 when the engine has not been created — the harness samples this
    on a timer and must not be the thing that opens the first connection.
    """
    from app.database import connection

    engine = connection._engine
    if engine is None:
        return -1
    try:
        return engine.pool.checkedout()
    except Exception:
        return -1


class Sampler:
    """Polls the counters that only mean something while the run is happening.

    Peak session counts, peak pool checkout and peak memory cannot be read
    afterwards, so a background task samples them a few times a second.
    """

    def __init__(self, realtime=None) -> None:
        # Which call manager holds the realtime side. The browser manager for a
        # local run; the live run builds its own and passes it in, because peak
        # concurrency cannot be read back once the calls have closed.
        self._realtime = realtime
        self.peak_banking = 0
        self.peak_realtime = 0
        self.peak_checked_out = 0
        self.peak_memory = 0.0
        self._start_cpu = cpu_seconds()
        self._running = False

    def sample(self) -> None:
        from app.realtime.browser_calls import browser_call_manager
        from app.sessions import session_manager

        realtime = self._realtime or browser_call_manager
        self.peak_banking = max(
            self.peak_banking, session_manager.active_session_count()
        )
        self.peak_realtime = max(self.peak_realtime, realtime.active_count())
        self.peak_checked_out = max(self.peak_checked_out, db_checked_out())
        self.peak_memory = max(self.peak_memory, memory_mb()[0])

    async def run(self, interval: float = 0.02) -> None:
        import asyncio

        self._running = True
        while self._running:
            self.sample()
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False
        self.sample()

    @property
    def cpu_used(self) -> float:
        return cpu_seconds() - self._start_cpu


def stopwatch():
    """A high-resolution timer that returns elapsed seconds when called.

    `perf_counter`, not `monotonic`: on Windows the monotonic clock ticks about
    every 15.6 ms, which is the same order as the thing being measured. Timing
    a 20 ms banking turn with it produces a column of 15.6 ms multiples and no
    usable percentile.
    """
    started = time.perf_counter()
    return lambda: time.perf_counter() - started
