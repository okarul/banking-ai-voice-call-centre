"""Browser WebRTC support: session configuration, short-lived credentials, tools.

Phase 9 connected this Python process to OpenAI over a server-side WebSocket.
Phase 10 moves only the *audio path* into the browser:

    browser mic --WebRTC--> OpenAI Realtime --WebRTC--> browser speaker
    browser data channel  -> POST /api/call/tool -> guards -> PostgreSQL

The banking core does not move. When the model wants a balance it emits a
function call on the data channel, the page relays it here, and the tool that
runs is the *same* `app.realtime.tools` object the Phase 9 voice path calls —
same guards, same session-bound identity, same PostgreSQL. Nothing about
banking is reimplemented for the browser.

Two things stay strictly on this side of the wire:

* **The permanent API key.** The browser is given a short-lived client secret
  minted here and nothing else. The key is used once, server-side, to mint it.
* **The agent's configuration.** Instructions and the tool surface are attached
  to the client secret when it is minted, so the page never declares what the
  agent may do. A page could still send its own `session.update` — which is why
  none of the enforcement lives in the prompt.

Caller audio is deliberately *not* transcribed. Transcription would put the
spoken PIN on the data channel as text, inside the browser, for no benefit: the
interface never shows the caller's own words back to them.
"""

import json
import logging
import uuid
from typing import Any

import httpx
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from app.config import settings
from app.realtime.banking_realtime import INSTRUCTIONS
from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.realtime.tools import BANKING_TOOLS
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager

logger = logging.getLogger("app.realtime.webrtc")

# Current documented endpoint for minting a browser credential.
CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# Long enough to set up a call, short enough to be worthless if it leaks. The
# credential authorises starting one realtime call, not using the API at large.
CLIENT_SECRET_TTL_SECONDS = 600

TOOLS_BY_NAME = {tool.name: tool for tool in BANKING_TOOLS}


def tool_schemas() -> list[dict]:
    """The realtime tool declarations, taken from the Phase 9 tools themselves.

    Deriving these rather than writing them by hand is what guarantees the
    browser session offers exactly the tools the backend implements, with
    exactly the parameters the model is allowed to choose. None of them
    declares a session or customer id — see `test_browser_call.py`.
    """
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params_json_schema,
        }
        for tool in BANKING_TOOLS
    ]


def browser_session_config() -> dict:
    """The realtime session a browser call runs under.

    Turn detection matches Phase 9: semantic VAD with `interrupt_response`, so
    barge-in is the server's own behaviour and the page implements none of it.
    """
    return {
        "type": "realtime",
        "model": settings.realtime_model,
        "instructions": INSTRUCTIONS,
        "audio": {
            "input": {
                # Transcription is on because the scope gate needs the caller's
                # words to decide whether the bank may answer at all, and that
                # decision has to be made in Python rather than trusted to the
                # model. The spoken PIN does appear in this transcript — but it
                # only ever travels to the caller's own browser and to this
                # backend, which already receives it through submit_pin. It is
                # never stored, never logged, and never shown on the page.
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {
                    "type": "semantic_vad",
                    "interrupt_response": True,
                },
            },
            "output": {"voice": settings.realtime_voice},
        },
        "tools": tool_schemas(),
        "tool_choice": "auto",
    }


async def mint_client_secret(*, ttl_seconds: int = CLIENT_SECRET_TTL_SECONDS) -> dict:
    """Mint a short-lived credential for one browser call.

    Returns only the client secret and its expiry. The permanent key is used to
    authorise this request and never appears in the return value, the logs or
    the exception text.
    """
    if not settings.realtime_configured:
        raise RealtimeSessionError(Reason.REALTIME_NOT_CONFIGURED)

    request = {
        "expires_after": {"anchor": "created_at", "seconds": ttl_seconds},
        "session": browser_session_config(),
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                CLIENT_SECRETS_URL,
                json=request,
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as error:
        logger.error("client secret request failed: %s", type(error).__name__)
        raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED) from error

    if response.status_code >= 400:
        # The status is useful; the body may echo request detail, so it is not
        # logged and never reaches the caller.
        logger.error("client secret rejected: HTTP %s", response.status_code)
        raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED)

    body = response.json()
    value = body.get("value")
    if not value:
        logger.error("client secret response carried no credential")
        raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED)

    return {"value": value, "expires_at": body.get("expires_at")}


async def execute_tool(
    name: str,
    session_id: str,
    arguments: dict | None = None,
    *,
    manager: SessionManager = default_manager,
) -> Any:
    """Run one banking tool for a browser call.

    The identity the tool works with comes from `session_id` — supplied by the
    page from its own banking session — and never from `arguments`, which is
    what the model produced. The tool invoked is the Phase 9 tool object, so
    the authorization guards run exactly as they do on a server-side call.
    """
    tool = TOOLS_BY_NAME[name]
    payload = json.dumps(arguments or {})

    context = BankingRealtimeContext(session_id=session_id, manager=manager)
    tool_context = ToolContext.from_agent_context(
        RunContextWrapper(context),
        tool_call_id=f"browser-{uuid.uuid4().hex[:12]}",
        tool_name=name,
        tool_arguments=payload,
    )

    # Structured enough to tell two concurrent calls apart in a log, and
    # nothing more: which call, who it was verified as, and what it asked for.
    # No arguments (a PIN travels in one of them), no result, no reasoning.
    session = manager.get_session(session_id)
    logger.info(
        "tool_call session=%s customer=%s tool=%s",
        session_id,
        (session.customer_id if session else None) or "unverified",
        name,
    )

    result = await tool.on_invoke_tool(tool_context, payload)

    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result
