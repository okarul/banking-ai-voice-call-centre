"""Credentials must not survive a trip through anything we write down.

The API key is backend-only, but it travels inside ordinary values — an
Authorization header, a `model_config` dict — and anything that renders one of
those prints it. These tests cover the shapes that actually occur in this
codebase, plus the configured key itself, whatever shape a future provider gives
it.

No real credential appears in this file. The values below are invented strings
that merely look like credentials.
"""

import logging

import pytest

from app.redaction import REDACTED, RedactingFilter, install_redaction, redact

# Invented. Not credentials, and not valid anywhere.
FAKE_KEY = "sk-proj-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
FAKE_EPHEMERAL = "ek_c0ffee1234567890abcdef"


@pytest.mark.parametrize(
    "text",
    [
        FAKE_KEY,
        f"Authorization: Bearer {FAKE_KEY}",
        f"{{'Authorization': 'Bearer {FAKE_KEY}'}}",
        f'{{"api_key": "{FAKE_KEY}"}}',
        f"model_config={{'api_key': '{FAKE_KEY}'}}",
        f"OPENAI_API_KEY={FAKE_KEY}",
        f"openai_api_key='{FAKE_KEY}'",
        FAKE_EPHEMERAL,
        f'{{"value": "{FAKE_EPHEMERAL}"}}',
    ],
)
def test_a_credential_never_survives_redaction(text):
    cleaned = redact(text)

    assert FAKE_KEY not in cleaned
    assert FAKE_EPHEMERAL not in cleaned
    assert REDACTED in cleaned


def test_the_key_body_is_gone_even_in_a_long_traceback():
    """The realistic shape: a key inside a rendered stack frame."""
    traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "app/realtime/realtime_manager.py", line 142, in open_openai_session\n'
        "    session = await runner.run(\n"
        f"        model_config={{'api_key': '{FAKE_KEY}'}},\n"
        "    )\n"
        "RuntimeError: connection failed\n"
    )

    cleaned = redact(traceback_text)

    assert FAKE_KEY not in cleaned
    # The useful part of the diagnostic is still there.
    assert "open_openai_session" in cleaned
    assert "RuntimeError: connection failed" in cleaned


def test_a_database_password_is_removed_but_the_host_survives():
    url = "postgresql+psycopg://postgres:hunter2@127.0.0.1:5435/banking_ai_demo"

    cleaned = redact(url)

    assert "hunter2" not in cleaned
    assert "127.0.0.1:5435" in cleaned


def test_the_configured_key_is_redacted_whatever_shape_it_has(monkeypatch):
    """A future key format must not need a new regular expression."""
    from app.config import settings

    odd_shape = "totally-unlike-a-key-9f4c2e78aa"
    monkeypatch.setattr(settings, "openai_api_key", odd_shape)

    cleaned = redact(f"provider rejected request using {odd_shape}")

    assert odd_shape not in cleaned
    assert REDACTED in cleaned


def test_ordinary_text_is_left_alone():
    """Redaction must not mangle normal logs, or people stop reading them."""
    message = "realtime[SESSION-abc] tool_call customer=DEMO001 tool=get_account_balance"

    assert redact(message) == message


# --- the logging filter -----------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def captured_logger():
    logger = logging.getLogger("test.redaction")
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    install_redaction(logger)
    yield logger, handler
    logger.handlers.clear()
    logger.filters.clear()


def test_a_logged_key_is_scrubbed_before_it_is_emitted(captured_logger):
    logger, handler = captured_logger

    logger.error("connection failed with %s", {"api_key": FAKE_KEY})

    assert handler.lines
    assert FAKE_KEY not in " ".join(handler.lines)
    assert REDACTED in " ".join(handler.lines)


def test_lazy_arguments_cannot_re_expand_the_secret(captured_logger):
    """The filter must drop args, not just rewrite the formatted message."""
    logger, _ = captured_logger
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.addHandler(Keep())
    logger.error("using %s", FAKE_KEY)

    assert records
    record = records[-1]
    assert FAKE_KEY not in record.getMessage()
    assert FAKE_KEY not in str(record.msg)
    assert not record.args


def test_installing_redaction_twice_does_not_stack_filters():
    logger = logging.getLogger("test.redaction.idempotent")
    logger.filters.clear()

    first = install_redaction(logger)
    second = install_redaction(logger)

    assert first is second
    assert sum(isinstance(f, RedactingFilter) for f in logger.filters) == 1


def test_a_broken_format_string_still_logs(captured_logger):
    """Redaction must never be the reason a diagnostic is lost."""
    logger, handler = captured_logger

    logger.error("missing placeholder %s %s", "only-one")

    assert handler.lines


def test_the_application_installs_redaction_on_the_root_logger():
    """Importing the app is enough; no caller has to remember to switch it on."""
    import app.main  # noqa: F401

    root = logging.getLogger()
    assert any(isinstance(f, RedactingFilter) for f in root.filters)
