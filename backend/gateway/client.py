"""Talking to the bank: signed call-control events, and the media socket.

Everything the gateway knows about the backend is here — a URL, a shared
secret, and a wire format. There is no import of the bank's code, and nothing
in this file needs one.

Two rules govern what is written down:

* **The credential is never logged.** The media token opens a live banking
  call. It is held in a value object, used once, and never rendered into a log
  line, an exception message or a `repr`.
* **Failures are categories.** A backend error body could carry detail that
  does not belong in a gateway log on an internet-facing host.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import websockets

from gateway.config import GatewaySettings
from gateway.signing import MEDIA_TOKEN_HEADER, sign

logger = logging.getLogger("gateway.client")

INCOMING_PATH = "/api/telephony/incoming"


class BackendUnavailable(Exception):
    """The bank could not be reached, or would not answer sensibly."""


class CallRefused(Exception):
    """The bank declined this call. Not an error — an answer."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class AcceptedCall:
    """What the bank returns when it agrees to take a call.

    `media_token` is a live credential for one banking call. It is kept out of
    `repr` so that logging this object — which is the natural thing to do —
    cannot print it.
    """

    provider_event_id: str
    media_url: str
    media_token: str = field(repr=False)

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"AcceptedCall({self.provider_event_id})"


class BackendClient:
    """The bank, as the gateway sees it."""

    def __init__(self, settings: GatewaySettings) -> None:
        if not settings.configured:
            raise BackendUnavailable(
                "gateway is not configured: set GATEWAY_WEBHOOK_SECRET"
            )
        self._settings = settings
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout,
            verify=settings.verify_tls,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- call control --------------------------------------------------------

    def _event_body(self, call_id: str, kind: str, *, caller: str | None) -> bytes:
        """One event, serialised exactly once.

        Serialised once and then both signed and sent, because the signature
        covers the bytes on the wire. Re-serialising between signing and
        sending — even to an identical-looking string — is how key ordering or
        whitespace silently invalidates a correct signature.
        """
        payload = {
            "provider": self._settings.provider_name,
            "provider_event_id": f"{call_id}-{kind}",
            "provider_call_id": call_id,
            "event_type": kind,
            "event_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if caller:
            payload["source"] = caller
        return json.dumps(payload).encode("utf-8")

    async def announce(self, call_id: str, *, caller: str | None = None) -> AcceptedCall:
        """Tell the bank a call has arrived, and collect the credential.

        Raises `CallRefused` when the bank declines — full, duplicate, or
        unable to open a session. A refusal is a normal answer, and the gateway
        responds by playing busy rather than by retrying: retrying will not
        create capacity, and the caller is waiting.
        """
        body = self._event_body(call_id, "incoming", caller=caller)
        headers = sign(self._settings.webhook_secret, body)

        try:
            response = await self._http.post(
                self._settings.backend_url + INCOMING_PATH,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise BackendUnavailable(type(error).__name__) from error

        if response.status_code != 200:
            # Status only. A body from an unhappy backend may carry detail.
            raise BackendUnavailable(f"HTTP {response.status_code}")

        answer = response.json()
        status = answer.get("status")
        if status != "accepted" or not answer.get("media_token"):
            raise CallRefused(status or "unknown")

        return AcceptedCall(
            provider_event_id=answer["provider_event_id"],
            media_url=answer["media_url"],
            media_token=answer["media_token"],
        )

    async def report_ended(self, call_id: str) -> None:
        """Tell the bank the call is over.

        Failures are logged and swallowed. The call has ended either way, and
        the bank's idle sweep reclaims a call whose ending never arrived — so
        raising here would turn a missed notification into a gateway crash
        while other calls are in progress.
        """
        body = self._event_body(call_id, "ended", caller=None)
        headers = sign(self._settings.webhook_secret, body)
        try:
            await self._http.post(
                self._settings.backend_url + INCOMING_PATH,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as error:
            logger.warning(
                "gateway[%s] end event not delivered: %s",
                call_id,
                type(error).__name__,
            )

    # --- media ---------------------------------------------------------------

    async def open_media(self, accepted: AcceptedCall):
        """Attach the audio socket, presenting the per-call credential.

        The credential goes in a header rather than the URL because query
        strings are written to proxy and web-server access logs as a matter of
        routine, and a credential in an access log is a credential in a backup.
        """
        url = self._settings.websocket_base + accepted.media_url
        try:
            return await websockets.connect(
                url,
                additional_headers={MEDIA_TOKEN_HEADER: accepted.media_token},
                open_timeout=self._settings.attach_timeout,
                max_size=None,
            )
        except Exception as error:
            # Never the URL with the token, never the exception body.
            raise BackendUnavailable(
                f"media socket refused ({type(error).__name__})"
            ) from error
