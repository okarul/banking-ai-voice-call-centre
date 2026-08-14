"""Printing the measurements, and deciding what they mean.

Two rules the printer follows:

* **Nothing sensitive is printed.** Session ids, customer ids, tool names,
  counts and timings only. No PIN, no credential, no provider body.
* **A level's verdict is stated, not implied.** A run that leaked, bound a
  session to the wrong customer or left an orphan behind is failing even if
  every latency figure looks healthy, and the gate for going to the next
  concurrency level says so out loud.
"""

from loadtest.metrics import TABLE_HEADER, TABLE_RULE, LevelResult, percentile


def level_detail(result: LevelResult) -> str:
    """The full metric set for one level, as required by the phase brief."""
    latencies = result.latencies
    setup = result.setup_latencies
    tools = result.tool_latencies
    failures = result.failure_counts()

    lines = [
        f"--- {result.label} " + "-" * max(0, 60 - len(result.label)),
        f"  attempted calls              {result.attempted}",
        f"  successful connections       {result.connected}",
        f"  failed connections           {result.connect_failures}",
        f"  successful authentications   {result.authenticated}",
        f"  failed authentications       {result.auth_failures}",
        f"  successful banking responses {result.responses_ok}",
        f"  incorrect responses          {result.responses_wrong}",
        f"  banking correctness          {result.correctness_rate:.1f}%",
        f"  cross-session leakage        {result.leakage}",
        f"  security-policy violations   {result.policy_violations}",
        f"  avg call setup latency       {(sum(setup) / len(setup) * 1000) if setup else 0:.3f} ms",
        f"  p50 response latency         {percentile(latencies, 0.50) * 1000:.1f} ms",
        f"  p95 response latency         {percentile(latencies, 0.95) * 1000:.1f} ms",
        f"  max response latency         {(max(latencies) if latencies else 0) * 1000:.1f} ms",
        f"  avg tool execution latency   {(sum(tools) / len(tools) * 1000) if tools else 0:.1f} ms",
        f"  cleanup failures             {result.cleanup_failures}",
        f"  orphan banking sessions      {result.orphan_banking_sessions}",
        f"  orphan realtime sessions     {result.orphan_realtime_sessions}",
        f"  peak banking sessions        {result.peak_banking_sessions}",
        f"  peak realtime sessions       {result.peak_realtime_sessions}",
        f"  peak DB connections in use   {result.peak_db_checked_out}",
        f"  peak process memory          {result.peak_memory_mb:.1f} MB",
        f"  CPU time consumed            {result.cpu_seconds:.2f} s",
        f"  total duration               {result.duration:.2f} s",
    ]
    if failures:
        lines.append("  failure classification:")
        for label in sorted(failures):
            lines.append(f"      {label:<20} {failures[label]}")
    else:
        lines.append("  failure classification:      none")

    for note in result.notes:
        lines.append(f"  NOTE: {note}")

    for call in result.calls:
        if not call.ok:
            lines.append(
                f"      failing caller #{call.index} ({call.customer_id}, "
                f"scenario {call.scenario}): {call.failure} {call.detail}".rstrip()
            )
            for turn in call.turns:
                if not turn.ok:
                    lines.append(f"          turn {turn.index}: {turn.detail}")
    return "\n".join(lines)


def table(results: list[LevelResult]) -> str:
    lines = [TABLE_HEADER, TABLE_RULE]
    lines.extend(result.summary_row() for result in results)
    return "\n".join(lines)


def blocking_problem(result: LevelResult) -> str | None:
    """Why this level must stop the ladder, or None to carry on.

    The mandatory thresholds are absolute: any data crossover, any wrong
    customer binding, any session collision and any orphan is a stop. Latency
    is not a gate — Phase 12 measures it rather than judging it.
    """
    if result.leakage:
        return "CUSTOMER DATA LEAKAGE"
    if result.policy_violations:
        return "SECURITY POLICY VIOLATION"
    if result.notes:
        return "; ".join(result.notes)
    if result.orphan_banking_sessions or result.orphan_realtime_sessions:
        return "ORPHANED SESSIONS AFTER TEARDOWN"
    if result.cleanup_failures:
        return "CLEANUP FAILURE"

    # A wrong banking answer and a turn the provider never finished are not the
    # same finding, and calling a timeout an incorrect response would be exactly
    # the misclassification this phase is meant to avoid.
    wrong = sum(
        1
        for call in result.calls
        for turn in call.turns
        if not turn.ok and turn.failure == "APPLICATION_LOGIC"
    )
    if wrong:
        return "INCORRECT BANKING RESPONSE"
    if result.connect_failures or result.auth_failures:
        return "CONNECTION OR AUTHENTICATION FAILURE"
    return None
