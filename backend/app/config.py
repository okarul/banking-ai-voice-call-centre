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

        # --- telephony (Phase 1: foundation only) ------------------------
        # Off unless deliberately switched on. While false, no SIP code
        # initialises, no provider is contacted, no telephony route is
        # registered, and the browser channel behaves exactly as before.
        self.telephony_enabled: bool = _flag(os.getenv("TELEPHONY_ENABLED"), False)
        self.telephony_provider: str = os.getenv("TELEPHONY_PROVIDER", "DIDWW")

        # Public identifiers only. A DID number is published to callers by
        # definition, and a SIP URI is a destination — neither is a secret.
        # Credentials are deliberately absent from this object: see
        # `docs/TELEPHONY_SECURITY_PRIVACY_DESIGN.md` for why they will be read
        # at point of use rather than held here.
        self.didww_did_number: str | None = os.getenv("DIDWW_DID_NUMBER") or None
        self.sip_public_uri: str | None = os.getenv("SIP_PUBLIC_URI") or None
        self.sip_provider_domain: str | None = os.getenv("SIP_PROVIDER_DOMAIN") or None

        # The shared secret an inbound provider event is signed with. Backend
        # only: it is never returned by a route, never logged, and deliberately
        # absent from `public_settings()`. Without it the webhook cannot verify
        # anything, so it refuses every request rather than accepting unsigned
        # ones — see `telephony_webhook_ready`.
        self.telephony_webhook_secret: str | None = (
            os.getenv("TELEPHONY_WEBHOOK_SECRET") or None
        )
        # How far a signed timestamp may be from now, in seconds, in either
        # direction. Five minutes is the usual provider convention: long enough
        # to survive ordinary clock drift and a retry, short enough that a
        # captured request stops being useful quickly.
        self.telephony_signature_tolerance_seconds: int = _positive_int(
            os.getenv("TELEPHONY_SIGNATURE_TOLERANCE_SECONDS"), default=300
        )

        # --- persistent PIN lockout --------------------------------------
        # The per-call lockout stops three wrong guesses inside one call. These
        # two settings are what stop the caller who simply redials: failures are
        # counted against the claimed customer id across calls.
        #
        # Higher than the per-call limit on purpose. Three failures in one call
        # is a caller who has forgotten their PIN; this threshold is meant to
        # catch the pattern that only an attacker produces.
        self.pin_lockout_max_attempts: int = _positive_int(
            os.getenv("PIN_LOCKOUT_MAX_ATTEMPTS"), default=5
        )
        # Both how long a lock lasts and how far back failures are counted. A
        # lock that never expired would let one mistyped PIN deny a shared
        # demonstration customer to a whole classroom, which is a denial of
        # service wearing the costume of a security control.
        self.pin_lockout_minutes: int = _positive_int(
            os.getenv("PIN_LOCKOUT_MINUTES"), default=15
        )

        # --- telephony media (Phase 3) -----------------------------------
        # Which transport carries a telephone call's audio.
        #
        #   websocket  a media gateway streams the call to this application
        #   loopback   in-memory, for tests and local development
        #
        # There is deliberately no "sip" value: terminating SIP and RTM in this
        # process would need a SIP stack, and terminating it outside needs a
        # gateway. See docs/TELEPHONY_MEDIA.md.
        self.telephony_media_transport: str = (
            os.getenv("TELEPHONY_MEDIA_TRANSPORT", "websocket").strip().lower()
        )

        # How long the media gateway has to attach its audio socket after the
        # call event arrives. Beyond this the call is torn down rather than
        # left holding a capacity slot for a caller who will never be heard.
        self.telephony_media_connect_timeout: int = _positive_int(
            os.getenv("TELEPHONY_MEDIA_CONNECT_TIMEOUT"), default=15
        )
        # How long opening the model session may take before the call is
        # abandoned. Generous: a realtime handshake is seconds, and a timeout
        # tighter than reality turns normal calls into failures.
        self.telephony_realtime_connect_timeout: int = _positive_int(
            os.getenv("TELEPHONY_REALTIME_CONNECT_TIMEOUT"), default=30
        )
        # How long the gateway has to answer the protocol hello. Short: it is
        # one small frame each way over a socket that has already connected, so
        # a gateway that has not answered in this long is not going to. An
        # infrastructure startup budget, not a limit on call length.
        self.telephony_protocol_timeout: int = _positive_int(
            os.getenv("TELEPHONY_PROTOCOL_TIMEOUT"), default=10
        )
        # A call with no audio in either direction for this long is assumed
        # dead and cleaned up, so a provider that never sends an end event
        # cannot leak a capacity slot for ever.
        self.telephony_idle_call_timeout: int = _positive_int(
            os.getenv("TELEPHONY_IDLE_CALL_TIMEOUT"), default=900
        )

        # Audio queue ceilings, in frames of 20 ms. 200 frames is four seconds
        # of speech — long enough to ride out a slow model turn, short enough
        # that a caller cannot make this process hold unbounded memory. When
        # full the oldest frame is dropped: see app.telephony.media.
        self.telephony_audio_queue_frames: int = _positive_int(
            os.getenv("TELEPHONY_AUDIO_QUEUE_FRAMES"), default=200
        )

        # Synthetic data only. This is a demonstration bank; turning it off
        # would imply a real banking connector, and there is none.
        self.demo_mode: bool = _flag(os.getenv("DEMO_MODE"), True)

        # Tell callers they are speaking to an AI. Configuration exists now;
        # the greeting itself is unchanged in Phase 1.
        self.ai_disclosure_enabled: bool = _flag(
            os.getenv("AI_DISCLOSURE_ENABLED"), True
        )
        self.ai_disclosure_text: str = os.getenv(
            "AI_DISCLOSURE_TEXT",
            "This is an AI-powered demonstration using synthetic banking data.",
        )

        # Retention intent, recorded so it is a decision rather than an
        # accident. Phase 1 implements no deletion job — see the design note.
        self.audit_retention_days: int = _positive_int(
            os.getenv("AUDIT_RETENTION_DAYS"), default=30
        )
        self.transcript_retention_days: int = _positive_int(
            os.getenv("TRANSCRIPT_RETENTION_DAYS"), default=7
        )

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
    def telephony_configured(self) -> bool:
        """Whether a telephone channel could be opened at all.

        Both halves are required: the flag *and* a destination. A flag switched
        on against blank configuration is a misconfiguration, and it should read
        as "not available" rather than as an attempt that fails later.
        """
        return bool(self.telephony_enabled and self.sip_public_uri)

    @property
    def telephony_webhook_ready(self) -> bool:
        """Whether the inbound event endpoint may be served at all.

        The signing secret is required, not optional. A webhook with no secret
        could only either reject everything or accept anything, and the second
        is how a public endpoint becomes a way to open banking sessions from the
        internet. So an unconfigured secret means the route is not registered.
        """
        return bool(self.telephony_enabled and self.telephony_webhook_secret)

    def public_settings(self) -> dict:
        """Non-sensitive settings, safe to log or show an operator.

        An allow-list, not a filter. A denylist of secret names would silently
        start leaking the first time somebody added a setting and forgot to
        update it; this can only ever expose what is named here.
        """
        return {
            "app_name": self.app_name,
            "app_env": self.app_env,
            "demo_mode": self.demo_mode,
            "telephony_enabled": self.telephony_enabled,
            "telephony_provider": self.telephony_provider,
            "telephony_configured": self.telephony_configured,
            "telephony_media_transport": self.telephony_media_transport,
            "ai_disclosure_enabled": self.ai_disclosure_enabled,
            "realtime_model": self.realtime_model,
            "realtime_voice": self.realtime_voice,
            "realtime_max_active_sessions": self.realtime_max_active_sessions,
            "dashboard_timezone": self.dashboard_timezone,
            "audit_retention_days": self.audit_retention_days,
            "transcript_retention_days": self.transcript_retention_days,
        }

    @property
    def realtime_configured(self) -> bool:
        """Whether a realtime voice session could be opened at all.

        Checked before connecting so a missing key is a clear refusal rather
        than a failure deep inside the SDK. The key itself never leaves this
        object.
        """
        return bool(self.openai_api_key)


settings = Settings()
