"""Turning realtime session events into safe log lines.

Two things must never reach a log, a trace or a response: the spoken PIN and
the API key. The PIN is the harder of the two, because it arrives as an
ordinary tool argument — so arguments are redacted by tool name here, at the
one place every event is described.

Event names are the current SDK's own (`agents.realtime.events`): audio,
audio_end, audio_interrupted, tool_start, tool_end, agent_start, agent_end,
history_added, history_updated, error, raw_model_event.
"""

import logging

logger = logging.getLogger("app.realtime")

# Tools whose arguments carry a credential. Their arguments are never logged.
SECRET_ARGUMENT_TOOLS = frozenset({"submit_pin"})

REDACTED = "<redacted>"

# Events that fire per audio chunk. Logging these would flood the log and say
# nothing useful, so they are counted by the caller instead.
NOISY_EVENT_TYPES = frozenset({"audio", "raw_model_event", "history_updated"})


def safe_arguments(tool_name: str, arguments) -> str:
    """Arguments for a log line, redacted when the tool carries a credential."""
    if tool_name in SECRET_ARGUMENT_TOOLS:
        return REDACTED
    return str(arguments)


def describe_event(event) -> dict:
    """A safe, structured summary of one realtime event.

    Only fields known to be non-sensitive are copied out. Audio bytes and model
    transcripts are deliberately not included.
    """
    event_type = getattr(event, "type", "unknown")
    summary: dict = {"event": event_type}

    if event_type in ("tool_start", "tool_end"):
        tool = getattr(event, "tool", None)
        tool_name = getattr(tool, "name", str(tool))
        summary["tool"] = tool_name
        summary["arguments"] = safe_arguments(
            tool_name, getattr(event, "arguments", None)
        )
        if event_type == "tool_end":
            # The result may carry balances; record only whether it succeeded.
            output = getattr(event, "output", None)
            summary["ok"] = _succeeded(output)

    elif event_type in ("agent_start", "agent_end"):
        summary["agent"] = getattr(getattr(event, "agent", None), "name", None)

    elif event_type == "error":
        # Message only. An SDK exception may carry request details.
        summary["error"] = type(getattr(event, "error", None)).__name__

    return summary


def _succeeded(output) -> bool | None:
    """Whether a tool result reported success, without copying its values."""
    if isinstance(output, dict):
        return bool(output.get("success"))
    if isinstance(output, str):
        # Tool output is serialised before it reaches the event.
        if '"success": true' in output or '"success":true' in output:
            return True
        if '"success": false' in output or '"success":false' in output:
            return False
    return None


def log_event(session_id: str, event) -> dict | None:
    """Log one event for development. Returns the summary, or None if skipped."""
    event_type = getattr(event, "type", "unknown")
    if event_type in NOISY_EVENT_TYPES:
        return None

    summary = describe_event(event)
    logger.info("realtime[%s] %s", session_id, summary)
    return summary
