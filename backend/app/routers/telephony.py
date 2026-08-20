"""The one endpoint a telephony provider may reach.

This is the only route in the application that answers a request from outside
the machine, so it is written as a gate rather than as a handler. The order is
fixed and each step refuses before the next one costs anything:

    size -> signature and timestamp -> content type -> schema -> event type
         -> idempotent registration

Verification happens on the **raw bytes**, before parsing. Parsing first would
mean an unauthenticated stranger could hand JSON to the parser, and a signature
computed over a re-serialised body would not match the one the provider signed.

Two rules about what leaves this module:

* **The response never explains the refusal.** Every verification failure —
  missing signature, wrong signature, stale timestamp, body altered in flight —
  returns the same 401 and the same sentence. Saying which check failed is
  precise, helpful, and a tool for whoever is trying the next variation. The
  detail goes to the audit log, where the operator is.

* **Nothing internal escapes.** No exception message, no session id, no
  database detail, no provider string echoed back. The generic 500 exists so
  that an unforeseen error is still a closed door.

The route is registered only when telephony is enabled *and* a signing secret
is configured — see `app.main.create_app`. An endpoint that cannot verify
anything has no safe behaviour available to it, so it does not exist.
"""

import logging

from fastapi import (
    APIRouter,
    Header,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import ValidationError

from app.config import settings
from app.observability import recorder
from app.observability.events import AuditEvent, safe_event
from app.telephony import service
from app.telephony.bridge import phone_call_registry
from app.telephony.channels import Channel
from app.telephony.media import WebSocketMediaTransport
from app.telephony.schemas import InboundCallEvent, InboundEventAccepted
from app.telephony.signature import (
    MAX_BODY_BYTES,
    ProviderVerificationError,
    verify_provider_request,
)

logger = logging.getLogger("app.telephony")

router = APIRouter(prefix="/api/telephony", tags=["telephony"])

# Closed without explanation when a media socket names a call that is not open.
WS_POLICY_VIOLATION = 1008

def _refuse(reason: str, *, provider_call_id: str | None = None) -> None:
    """Record why an event was turned away. Categories only, never the body."""
    logger.warning(
        "telephony event refused: %s",
        safe_event(
            AuditEvent.CALL_REJECTED,
            channel=Channel.PHONE.value,
            provider_call_id=provider_call_id,
            reason=reason,
        ),
    )


@router.post("/incoming", status_code=status.HTTP_200_OK)
async def incoming_event(
    request: Request,
    response: Response,
    content_type: str = Header(default=""),
    x_telephony_timestamp: str | None = Header(default=None),
    x_telephony_signature: str | None = Header(default=None),
) -> InboundEventAccepted:
    """Accept one call-control event from the telephony provider.

    Returns 200 for anything that was genuinely processed, including a
    duplicate and including a call refused for capacity. That is deliberate:
    providers retry non-2xx responses, and retrying will not create capacity or
    un-duplicate an event. A 5xx here would turn one busy moment into a retry
    storm against a bank that is already at its limit.
    """
    # Refuse an oversized body on the declared length, before reading it.
    # Checking after `await request.body()` would be checking a body already
    # held in memory in full, which is the cost the limit exists to avoid.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                _refuse("BODY_TOO_LARGE")
                response.status_code = status.HTTP_413_CONTENT_TOO_LARGE
                return InboundEventAccepted(status="rejected", provider_event_id="")
        except ValueError:
            _refuse("CONTENT_LENGTH_INVALID")
            response.status_code = status.HTTP_400_BAD_REQUEST
            return InboundEventAccepted(status="rejected", provider_event_id="")

    # Read the body once, as bytes, before anything interprets it. `Request`
    # gives no way to reconstruct the exact bytes a model was parsed from, and
    # the signature covers exactly those bytes.
    body = await request.body()

    # A chunked request declares no length, so the limit is enforced again on
    # what actually arrived. Belt and braces, cheaply.
    if len(body) > MAX_BODY_BYTES:
        _refuse("BODY_TOO_LARGE")
        response.status_code = status.HTTP_413_CONTENT_TOO_LARGE
        return InboundEventAccepted(status="rejected", provider_event_id="")

    try:
        verify_provider_request(
            body=body,
            timestamp_header=x_telephony_timestamp,
            signature_header=x_telephony_signature,
            secret=settings.telephony_webhook_secret,
            tolerance_seconds=settings.telephony_signature_tolerance_seconds,
        )
    except ProviderVerificationError as error:
        _refuse(str(error.reason))
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return InboundEventAccepted(status="rejected", provider_event_id="")

    # Only now, with origin established, is the content type worth an opinion.
    # Checked after verification on purpose: an unverified request should learn
    # nothing about our expectations, not even which media types we accept.
    if not content_type.lower().startswith("application/json"):
        _refuse("CONTENT_TYPE_INVALID")
        response.status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        return InboundEventAccepted(status="rejected", provider_event_id="")

    try:
        payload = InboundCallEvent.model_validate_json(body)
    except ValidationError:
        # The validation error itself is not returned. It would name the fields
        # we require and the ones we forbid, which is a specification of how to
        # build an acceptable forgery.
        _refuse("SCHEMA_INVALID")
        response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        return InboundEventAccepted(status="rejected", provider_event_id="")

    try:
        result = await service.handle_event(payload)
    except Exception:
        # Last resort. Logged as a type by the redacting handler, never
        # rendered to the provider.
        logger.exception("telephony event failed")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return InboundEventAccepted(
            status="error", provider_event_id=payload.provider_event_id
        )

    return InboundEventAccepted(
        status=result.outcome.value,
        provider_event_id=result.provider_event_id,
        duplicate=result.duplicate,
    )


@router.websocket("/media/{provider_call_id}")
async def media_socket(websocket: WebSocket, provider_call_id: str) -> None:
    """Carry one call's audio between the media gateway and its bridge.

    One socket, one call. The path names the call, and the only thing that name
    can do is select a bridge that a verified provider event already created —
    so a socket for a call that was never announced, or for one that has
    finished, is closed without creating anything. A media socket can never
    bring a call into existence, because that would be a way to open a banking
    session without passing the signature check.

    Frames are µ-law, 20 ms, binary. Text messages are ignored rather than
    parsed: this socket carries audio, and a control channel here would be a
    second way to affect a call.

    **This socket is not authenticated by itself.** It is protected by
    obscurity of the call id plus the fact that the id must already be
    registered — which is weaker than the webhook's HMAC, and is called out in
    `docs/TELEPHONY_MEDIA.md` as the thing to close before this faces anything
    but a gateway on a trusted network.
    """
    bridge = phone_call_registry.get(provider_call_id)
    transport = getattr(bridge, "transport", None) if bridge else None

    if bridge is None or bridge.closed or not isinstance(
        transport, WebSocketMediaTransport
    ):
        # Refused before the handshake completes. Nothing is told apart: an
        # unknown call, a finished call and a call on another transport all
        # look identical from outside.
        await websocket.close(code=WS_POLICY_VIOLATION)
        _refuse("MEDIA_SOCKET_UNKNOWN_CALL", provider_call_id=provider_call_id)
        return

    await websocket.accept()
    transport.attach(websocket)
    logger.info(
        "telephony media attached: %s",
        safe_event(
            AuditEvent.CALL_STARTED,
            channel=Channel.PHONE.value,
            provider_call_id=provider_call_id,
        ),
    )

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            frame = message.get("bytes")
            if frame:
                # Straight into this call's bounded queue. Never awaited on a
                # full queue: a socket read that blocked would stop this call
                # reading while the gateway kept sending.
                transport.deliver(frame)
    except WebSocketDisconnect:
        pass
    except Exception as error:
        logger.info(
            "telephony media socket ended: %s", type(error).__name__
        )
    finally:
        # The caller has gone. Converge on the one cleanup path, which is
        # idempotent, so an end event arriving at the same moment is harmless.
        await service.tear_down(provider_call_id, bridge.banking_session_id)
        try:
            recorder.close_phone_call(provider_call_id, reason="CUSTOMER_ENDED")
        except Exception as error:
            # This runs in a `finally`. An observability failure here would
            # replace whatever actually ended the call, and the call is already
            # released either way.
            logger.error(
                "telephony call record not closed: %s", type(error).__name__
            )
