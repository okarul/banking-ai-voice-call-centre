"""Gateway configuration. Separate from the bank's, deliberately.

In a real deployment this process runs on a different host from the banking
backend — the one exposed to the telephone network — and shares exactly two
things with it: a URL and a signing secret. Reading the bank's settings here
would quietly couple two things that are meant to be deployable apart, and
would put the bank's database URL and OpenAI key in the address space of the
host most exposed to the internet.

So these are `GATEWAY_*` variables, read from this process's own environment.
"""

from __future__ import annotations

import os


def _flag(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _positive_int(raw: str | None, *, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


class GatewaySettings:
    """Everything this gateway needs to reach the bank, and nothing more."""

    def __init__(self) -> None:
        # Where the banking backend is. In production an https:// URL reached
        # over a private link; in development the loopback backend.
        self.backend_url: str = os.getenv(
            "GATEWAY_BACKEND_URL", "http://127.0.0.1:8001"
        ).rstrip("/")

        # The shared secret events are signed with. Must be byte-identical to
        # the backend's TELEPHONY_WEBHOOK_SECRET. Absent means this gateway
        # refuses to run rather than sending unsigned events that the backend
        # would rightly reject one at a time.
        self.webhook_secret: str | None = (
            os.getenv("GATEWAY_WEBHOOK_SECRET") or None
        )

        # The name this gateway reports itself as. Operational labelling only —
        # the backend treats it as untrusted text, as it treats every provider
        # string.
        self.provider_name: str = os.getenv("GATEWAY_PROVIDER_NAME", "DIDWW")

        # Certificate verification. On by default and only ever turned off
        # against a local test backend: a gateway that skips verification is a
        # gateway whose calls can be intercepted, and it carries a credential
        # that opens a live banking call.
        self.verify_tls: bool = _flag(os.getenv("GATEWAY_VERIFY_TLS"), True)

        # How long to wait for the backend to answer a call-control event.
        self.request_timeout: int = _positive_int(
            os.getenv("GATEWAY_REQUEST_TIMEOUT"), default=10
        )

        # The backend gives a gateway a limited window to attach its media
        # socket after a call is accepted; staying under it is this side's job.
        self.attach_timeout: int = _positive_int(
            os.getenv("GATEWAY_ATTACH_TIMEOUT"), default=10
        )

    @property
    def configured(self) -> bool:
        """Whether this gateway may run at all."""
        return bool(self.webhook_secret and self.backend_url)

    @property
    def websocket_base(self) -> str:
        """The backend's URL as a WebSocket scheme."""
        if self.backend_url.startswith("https://"):
            return "wss://" + self.backend_url[len("https://") :]
        return "ws://" + self.backend_url[len("http://") :]

    def safe_summary(self) -> dict:
        """What may be logged or printed. No secret, no credential."""
        return {
            "backend_url": self.backend_url,
            "provider_name": self.provider_name,
            "verify_tls": self.verify_tls,
            "configured": self.configured,
            "request_timeout": self.request_timeout,
            "attach_timeout": self.attach_timeout,
        }


gateway_settings = GatewaySettings()
