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

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import ValidationError

from app.config import settings
from app.observability.events import AuditEvent, safe_event
from app.telephony import service
from app.telephony.channels import Channel
from app.telephony.schemas import InboundCallEvent, InboundEventAccepted
from app.telephony.signature import (
    MAX_BODY_BYTES,
    ProviderVerificationError,
    verify_provider_request,
)

logger = logging.getLogger("app.telephony")

router = APIRouter(prefix="/api/telephony", tags=["telephony"])

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
