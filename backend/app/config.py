"""Application configuration.

Values are read from environment variables (optionally supplied by a local
`.env` file). Every setting has a sensible default so the app runs with no
`.env` file present.
"""

import os

from dotenv import load_dotenv

# Load backend/.env if it exists. Missing file is not an error.
load_dotenv()


def _origins(raw: str) -> list[str]:
    """Split a comma-separated origin list, ignoring blanks."""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _flag(raw: str | None, default: bool) -> bool:
    """Read a boolean setting. Anything unrecognised keeps the default."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _optional_float(raw: str | None) -> float | None:
    """A number, or None when it is absent or unusable.

    None is meaningful here: it is what makes the dashboard print N/A instead
    of a fabricated cost or emission figure.
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _positive_int(raw: str | None, *, default: int) -> int:
    """Read a non-negative integer setting, falling back on anything unusable.

    A misspelt limit must not become an accidental cap of zero, which would
    refuse every call. Anything that is not a non-negative integer is ignored
    in favour of the default.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


class Settings:
    """Simple settings container."""

    def __init__(self) -> None:
        self.app_name: str = os.getenv("APP_NAME", "ABC Demo Bank Voice Banking API")
        self.app_env: str = os.getenv("APP_ENV", "development")
        # Optional: the app still imports and serves /health without it.
        self.database_url: str | None = os.getenv("DATABASE_URL") or None
        # Backend-only. Never returned by a route, logged, or sent to a client.
        self.openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
        self.realtime_model: str = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")
        self.realtime_voice: str = os.getenv("REALTIME_VOICE", "ash")
        # Admission control for live voice calls. 0 means no limit, which is the
        # default on purpose: this must never quietly reduce capacity that
        # already works. Set it only to match a *measured* provider ceiling —
        # a caller turned away with "temporarily busy" is a better outcome than
        # a session that opens and then stalls, but only when the ceiling is
        # real. See docs/REALTIME_CAPACITY.md.
        self.realtime_max_active_sessions: int = _positive_int(
            os.getenv("REALTIME_MAX_ACTIVE_SESSIONS"), default=0
        )

        # --- operations dashboard ---------------------------------------
        # Observability only. None of it touches a banking answer, and all of
        # it is allowed to be absent: an unconfigured price shows as N/A rather
        # than as a guess.
        self.dashboard_enabled: bool = _flag(os.getenv("DASHBOARD_ENABLED"), True)

        # USD per one million tokens, for the realtime model. Unset by default
        # because provider prices change and a stale number printed to two
        # decimal places looks far more authoritative than it is.
        self.price_input_per_mtok: float | None = _optional_float(
            os.getenv("PRICE_INPUT_PER_MTOK")
        )
        self.price_output_per_mtok: float | None = _optional_float(
            os.getenv("PRICE_OUTPUT_PER_MTOK")
        )

        # Carbon is an estimate from configured assumptions, never a provider
        # measurement. Off unless an operator turns it on and supplies both
        # coefficients, so the dashboard cannot invent an environmental figure.
        self.carbon_estimation_enabled: bool = _flag(
            os.getenv("CARBON_ESTIMATION_ENABLED"), False
        )
        self.carbon_grams_per_ktok: float | None = _optional_float(
            os.getenv("CARBON_GRAMS_PER_KTOK")
        )
        # Where the dashboard is read. Times are stored in UTC and displayed in
        # this zone; the demo is run in Singapore.
        self.dashboard_timezone: str = os.getenv("DASHBOARD_TIMEZONE", "Asia/Singapore")
        # Origins allowed to call the customer API. Named explicitly rather
        # than wildcarded: this list is what stops another page on this machine
        # from opening banking calls.
        self.frontend_origins: list[str] = _origins(
            os.getenv(
                "FRONTEND_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            )
        )

    @property
    def realtime_configured(self) -> bool:
        """Whether a realtime voice session could be opened at all.

        Checked before connecting so a missing key is a clear refusal rather
        than a failure deep inside the SDK. The key itself never leaves this
        object.
        """
        return bool(self.openai_api_key)


settings = Settings()
