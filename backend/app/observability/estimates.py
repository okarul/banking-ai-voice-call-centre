"""Cost and carbon, both estimated, both refusing to guess.

Two figures on the dashboard are not measurements:

* **Cost** is token usage multiplied by a configured price. The price is not in
  this file; provider prices change, and a number baked into source code goes
  stale silently while still being printed to four decimal places.
* **Carbon** is an estimate from a configured coefficient. OpenAI does not
  publish a per-session emissions figure, so anything shown here is arithmetic
  on an assumption the operator supplied, and it is labelled as such
  everywhere it appears.

Both return `None` when the inputs are missing, and `None` renders as "N/A".
That is the whole design: a dashboard that says "I don't know" is useful, and
one that invents an environmental metric is worse than one with a blank column.
"""

from decimal import Decimal, InvalidOperation

from app.config import settings

MILLION = Decimal(1_000_000)
THOUSAND = Decimal(1000)


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def estimate_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
) -> Decimal | None:
    """Estimated USD for one call, or None when it cannot be known.

    Returns None — never zero — when pricing is unconfigured or usage was never
    reported. Zero would read as "this call was free", which is a claim; None
    reads as "not available", which is the truth.
    """
    price_in = (
        price_input_per_mtok
        if price_input_per_mtok is not None
        else settings.price_input_per_mtok
    )
    price_out = (
        price_output_per_mtok
        if price_output_per_mtok is not None
        else settings.price_output_per_mtok
    )
    if price_in is None or price_out is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None

    rate_in = _decimal(price_in)
    rate_out = _decimal(price_out)
    if rate_in is None or rate_out is None:
        return None

    used_in = _decimal(input_tokens or 0) or Decimal(0)
    used_out = _decimal(output_tokens or 0) or Decimal(0)

    cost = (used_in / MILLION) * rate_in + (used_out / MILLION) * rate_out
    return cost.quantize(Decimal("0.000001"))


def estimate_carbon_grams(
    total_tokens: int | None,
    *,
    enabled: bool | None = None,
    grams_per_ktok: float | None = None,
) -> Decimal | None:
    """Estimated gCO2e for one call, or None.

    Off unless an operator has both switched estimation on and supplied a
    coefficient. This is not a provider-measured value and the dashboard says
    so next to every figure it prints.
    """
    switched_on = (
        enabled if enabled is not None else settings.carbon_estimation_enabled
    )
    if not switched_on:
        return None

    coefficient = (
        grams_per_ktok if grams_per_ktok is not None else settings.carbon_grams_per_ktok
    )
    if coefficient is None or total_tokens is None:
        return None

    rate = _decimal(coefficient)
    used = _decimal(total_tokens)
    if rate is None or used is None:
        return None

    return ((used / THOUSAND) * rate).quantize(Decimal("0.0001"))


CARBON_NOTE = (
    "Environmental impact is an estimate based on configured assumptions and "
    "is not a provider-measured value."
)

COST_NOTE = (
    "Cost is an estimate from configured model pricing and reported token "
    "usage. It is not a provider-issued invoice figure."
)
