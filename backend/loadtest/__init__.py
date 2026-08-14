"""Phase 12: the controlled concurrency harness.

This package measures how the existing implementation behaves as callers
arrive together. It adds nothing to the running application — no endpoint, no
dependency, no import from `app` into here in the other direction — and it is
deliberately outside `tests/` so it never runs as part of a regression suite.

Two harnesses, reported separately:

* `loadtest.local`  — every layer except the paid provider. Sessions,
  authentication, the scope gate, routing, tools, guards, PostgreSQL, cleanup.
  Free, so it runs at the full 2/5/10/20/30 ladder.
* `loadtest.live`   — real OpenAI Realtime calls. Paid, so it runs a small,
  explicitly-chosen number of calls and stops the moment the provider or the
  cost says stop.

Both use only the synthetic DEMO customers seeded in Phase 2.
"""
