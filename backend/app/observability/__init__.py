"""Operational records for the agent dashboard.

Everything here watches calls; nothing here affects them. Two rules shape the
whole package, and both are absolute:

* **Observability never breaks a call.** Every entry point swallows its own
  failures and logs a category. If PostgreSQL is unreachable, or a column is
  missing, or the estimate maths divides by zero, the customer still gets their
  balance. `app.observability.recorder` is the only module the application
  calls, and none of its functions can raise.
* **Nothing sensitive is written down.** The spoken PIN, the API key and the
  model's private reasoning never reach a row. Transcripts pass through
  `redact_transcript` first, tool arguments are not stored at all, and the
  balances a caller was told stay out of the dashboard table.

Read `recorder` for what is recorded, `estimates` for how cost and carbon are
derived, and `redaction` for what is removed before anything is stored.
"""

from app.observability.estimates import estimate_carbon_grams, estimate_cost_usd
from app.observability.redaction import redact_transcript

__all__ = ["estimate_carbon_grams", "estimate_cost_usd", "redact_transcript"]
