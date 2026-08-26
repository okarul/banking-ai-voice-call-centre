"""Scrubbing credentials out of anything this application writes down.

The API key is backend-only, but "backend-only" is a property of where it is
*stored*, not of where it can end up. It travels inside two structures that are
perfectly ordinary Python values:

    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    model_config = {"api_key": settings.openai_api_key}

Anything that renders one of those — a log line, an exception message, a
traceback frame with locals, a test diagnostic — prints the key. Nothing in the
application does that deliberately, and that is exactly the problem: the leak
would come from a path nobody wrote on purpose.

So redaction is applied at the boundary instead of trusted at every call site. A
logging filter rewrites records as they are emitted, and `redact()` is available
for any diagnostic string built by our own scripts and tests.

Two kinds of rule:

* **Shape rules** catch anything that looks like a credential — `sk-`, `ek_`,
  a Bearer token, an `api_key` field, an `OPENAI_API_KEY=` assignment.
* **The literal rule** catches the configured key itself, whatever its shape.
  Provider key formats change; the value in this process does not.

Third-party package source is never modified. This wraps our own surfaces.
"""

import logging
import re

REDACTED = "<redacted>"

# Credential shapes. Deliberately broad: a false positive costs a redacted log
# line, a false negative costs a key.
_PATTERNS: tuple[re.Pattern, ...] = (
    # Provider keys and browser client secrets, including project-scoped forms.
    # Non-capturing: the whole token is replaced, leaving no prefix behind.
    re.compile(r"\b(?:sk|ek|rk)[-_][A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    # Authorization headers, however they are quoted or spelled.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*)(['\"]?)[^'\",}\s]+"),
    # api_key / apikey / openai_api_key fields in dicts, JSON, env dumps.
    re.compile(
        r"(?i)(['\"]?(?:openai_)?api[_\-]?key['\"]?\s*[:=]\s*)(['\"]?)[^'\",}\s]+"
    ),
    re.compile(r"(?i)(OPENAI_API_KEY\s*=\s*)\S+"),
    # Database URLs carry a password.
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:\s]+:)[^@\s]+(@)"),
)


def _mask(match: re.Match) -> str:
    """Keep the field name, drop the value."""
    groups = match.groups()
    if not groups:
        return REDACTED
    # Rebuild `name: "` prefixes so the line still reads as structured data.
    prefix = "".join(part for part in groups if part is not None)
    if match.re.groups >= 2 and match.group(match.re.groups) == "@":
        # Database URL: keep the trailing '@' so the host is still visible.
        return f"{groups[0]}{REDACTED}@"
    return f"{prefix}{REDACTED}"


def redact(value) -> str:
    """Return `value` as text with every recognised credential removed.

    Safe to call on anything: the value is stringified first, so an exception,
    a dict of headers or a whole traceback can be passed straight in.
    """
    text = value if isinstance(value, str) else str(value)

    # The configured key first, so it is removed even if its shape is unknown.
    for secret in _live_secrets():
        if secret and secret in text:
            text = text.replace(secret, REDACTED)

    for pattern in _PATTERNS:
        text = pattern.sub(_mask, text)
    return text


def _live_secrets() -> tuple[str, ...]:
    """Secrets held by this process, read fresh so tests can set them.

    Imported lazily: `app.config` reads the environment, and this module must
    stay importable by tooling that has none of it configured.
    """
    try:
        from app.config import settings
    except Exception:  # pragma: no cover - configuration is optional
        return ()

    return tuple(
        value
        for value in (
            settings.openai_api_key,
            settings.database_url,
            # The webhook signing secret has no recognisable shape — it is
            # whatever the operator generated — so no pattern above can catch
            # it. Only the literal rule can, which is why it has to be named
            # here: a traceback rendering a verification call's locals would
            # otherwise print it in full.
            settings.telephony_webhook_secret,
        )
        if isinstance(value, str) and len(value) >= 8
    )


# Renders a traceback exactly as the standard library would, so a record
# carrying `exc_info` can be scrubbed before any handler formats it. Held at
# module level because building one per record is pure waste.
_EXCEPTION_RENDERER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs every record before it is emitted.

    Attached to the handler rather than to one logger, so it applies to
    application logs, third-party logs and anything else that reaches the same
    output. It rewrites the formatted message and drops the original arguments,
    because leaving them in place would let a handler re-expand the unredacted
    values.

    **Tracebacks are rendered here rather than left to the handler.** A filter
    runs before formatting, so `record.exc_text` is still empty at this point
    and scrubbing it alone scrubbed nothing: every `exc_info=True` call site in
    this application was emitting an unredacted traceback. That matters because
    the last line of a traceback is `str(exception)`, and the exceptions most
    likely to be logged are the ones that carry the most: a SQLAlchemy
    `OperationalError` renders the failing statement, its bound parameters and
    the database host, and a connection error renders the URL.

    So the traceback is rendered with the standard formatter, scrubbed, and
    cached on `record.exc_text`. `logging.Formatter.format` uses that cache
    verbatim when it is already set, which is what makes the scrubbed version
    the one that reaches the handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # A broken format string must not stop the record being logged.
            message = str(record.msg)

        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = ()

        if not record.exc_text and record.exc_info:
            try:
                record.exc_text = _EXCEPTION_RENDERER.formatException(record.exc_info)
            except Exception:
                # An exception whose own repr fails must not lose the record.
                record.exc_text = None

        if record.exc_text:
            record.exc_text = redact(record.exc_text)

        return True


def install_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach the filter to a logger's handlers, and to the logger itself.

    Returns the filter so a caller can remove it again. Installing twice is
    harmless — the filter is idempotent — but the existing one is reused rather
    than stacked.
    """
    target = logger or logging.getLogger()

    for existing in target.filters:
        if isinstance(existing, RedactingFilter):
            return existing

    scrubber = RedactingFilter()
    target.addFilter(scrubber)
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(scrubber)
    return scrubber
