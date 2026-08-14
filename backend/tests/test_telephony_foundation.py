"""Phase 1: the telephone channel exists as an idea and nothing more.

Two things are being defended here, and the second matters far more than the
first.

The first is that nothing has been switched on. Telephony defaults to off, no
SIP code initialises, no provider is contacted, and the browser channel behaves
exactly as it did — the working system is the thing most at risk from
preparing for a second channel.

The second is the rule that will matter for the rest of this project's life:
**a channel can never say who the customer is.** A telephone call arrives with
a caller number and a SIP `From` header, both of which the caller controls. If
either could select a banking customer, the PIN check would be decoration. So
the tests below try to use channel metadata as an identity and require that
every route is closed.

All customers and PINs here are Phase 2 synthetic seed data.
"""

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import Settings, settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.main import app
from app.observability import recorder
from app.observability.events import (
    ALLOWED_EVENT_FIELDS,
    FORBIDDEN_EVENT_FIELDS,
    AuditEvent,
    safe_event,
)
from app.realtime.browser_calls import browser_call_manager
from app.sessions import session_manager
from app.telephony import (
    Channel,
    VoiceChannelAdapter,
    WebRTCChannelAdapter,
    mask_caller_number,
    normalise_channel,
)
from app.telephony.channels import CallerMetadata

PINS = {"DEMO001": "4821"}


@pytest.fixture(autouse=True)
def clean_operational_tables():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    asyncio.run(browser_call_manager.close_all())
    session_manager.clear()
    wipe()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def call(client, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    return client.post("/api/call/start").json()


def only_row() -> dict:
    with session_scope() as db:
        record = db.scalars(select(AgentSession).order_by(AgentSession.id)).first()
        assert record is not None
        return {
            column.name: getattr(record, column.name)
            for column in AgentSession.__table__.columns
        }


# === 1-2: nothing has been switched on ======================================


def test_telephony_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TELEPHONY_ENABLED", raising=False)

    assert Settings().telephony_enabled is False


def test_telephony_is_not_considered_configured_without_a_destination(monkeypatch):
    """A flag switched on against blank configuration is a misconfiguration."""
    monkeypatch.setenv("TELEPHONY_ENABLED", "true")
    monkeypatch.delenv("SIP_PUBLIC_URI", raising=False)

    fresh = Settings()
    assert fresh.telephony_enabled is True
    assert fresh.telephony_configured is False


def test_a_nonsense_flag_falls_back_to_disabled(monkeypatch):
    for bad in ("maybe", "", "  ", "yes please"):
        monkeypatch.setenv("TELEPHONY_ENABLED", bad)
        assert Settings().telephony_enabled is False


def test_the_application_starts_and_serves_with_telephony_disabled(client):
    assert settings.telephony_enabled is False
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_no_telephony_route_is_registered(client):
    """Phase 1 adds no endpoint a provider could reach."""
    paths = {getattr(route, "path", "") for route in app.routes}

    for reserved in ("/api/telephony", "/api/sip", "/api/didww", "/sip", "/webhook"):
        assert not any(path.startswith(reserved) for path in paths), reserved


def test_no_sip_library_is_imported_anywhere():
    """Phase 1 installs and imports no telephony stack."""
    import sys

    for banned in ("pjsua2", "pjsip", "aiosip", "sipsimple", "twilio", "asterisk"):
        assert banned not in sys.modules, banned


def test_the_telephony_package_reaches_no_provider():
    """Nothing in the package opens a socket or makes a request."""
    import app.telephony.channels as channels

    source = inspect.getsource(channels)
    for reachy in ("requests.", "httpx.", "socket.", "urlopen", "websocket", "connect("):
        assert reachy not in source, reachy


# === 3-5: the channel model =================================================


def test_both_channels_exist_and_are_distinct():
    assert Channel.WEBRTC.value == "WEBRTC"
    assert Channel.PHONE.value == "PHONE"
    assert Channel.WEBRTC is not Channel.PHONE


def test_an_existing_browser_call_is_recorded_as_webrtc(call):
    assert only_row()["channel"] == "WEBRTC"


def test_a_capacity_rejection_records_its_channel(client, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)

    client.post("/api/call/start")
    assert client.post("/api/call/start").status_code == 503

    with session_scope() as db:
        rejected = db.scalars(
            select(AgentSession).where(AgentSession.status == "REJECTED")
        ).one()
    assert rejected.channel == "WEBRTC"
    assert rejected.customer_id is None


def test_an_unreadable_channel_never_breaks_a_row():
    """Operational metadata must not be able to interrupt banking."""
    for rubbish in (None, "", "  ", "TELEPATHY", 42, object()):
        assert normalise_channel(rubbish) is Channel.WEBRTC


def test_the_phone_channel_can_be_recorded_without_being_enabled():
    """The type exists; using it does not turn anything on."""
    assert normalise_channel("PHONE") is Channel.PHONE
    assert settings.telephony_enabled is False


def test_the_webrtc_adapter_satisfies_the_boundary():
    adapter = WebRTCChannelAdapter()

    assert isinstance(adapter, VoiceChannelAdapter)
    assert adapter.channel is Channel.WEBRTC
    assert adapter.is_enabled() is True
    assert "channel" in adapter.describe()


def test_no_adapter_can_reach_banking_data():
    """A channel is transport. It has no route to an account or a PIN."""
    adapter = WebRTCChannelAdapter()
    surface = {name for name in dir(adapter) if not name.startswith("_")}

    for forbidden in (
        "customer_id", "authenticate", "verify_pin", "get_account_balance",
        "get_loan_balance", "session", "lookup", "customer",
    ):
        assert forbidden not in surface, forbidden


# === 6-7: a channel must never define identity ==============================


def test_caller_metadata_declares_that_it_identifies_nobody():
    metadata = CallerMetadata.from_provider(
        Channel.PHONE, provider_call_id="prov-1", caller_number="+6531252836"
    )

    assert metadata.identifies_customer() is False


def test_caller_metadata_has_nowhere_to_put_a_customer_id():
    """The rule is structural: there is no field to abuse."""
    fields = set(CallerMetadata.__dataclass_fields__)

    assert "customer_id" not in fields
    assert "authenticated" not in fields
    assert fields == {"channel", "provider_call_id", "masked_number"}


def test_a_caller_number_is_discarded_by_default():
    """The safest version of a phone number is not having one."""
    metadata = CallerMetadata.from_provider(
        Channel.PHONE, caller_number="+6531252836"
    )

    assert metadata.masked_number is None
    assert "6531252836" not in str(metadata.to_safe_dict())


def test_a_retained_caller_number_is_only_ever_masked():
    metadata = CallerMetadata.from_provider(
        Channel.PHONE, caller_number="+6531252836", retain_masked_number=True
    )

    assert metadata.masked_number == "+65 **** 2836"
    assert "3125" not in str(metadata.to_safe_dict())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+6531252836", "+65 **** 2836"),
        ("6531252836", "**** 2836"),
        ("+44 20 7946 1234", "+44 **** 1234"),
        ("123", "****"),
        (None, None),
    ],
)
def test_masking_keeps_only_the_last_four_digits(raw, expected):
    assert mask_caller_number(raw) == expected


def test_recording_a_phone_channel_does_not_authenticate_anyone():
    """The decisive test: a PHONE session with provider metadata is anonymous."""
    session = session_manager.create_session()
    recorder.start_session(
        session.session_id, channel=Channel.PHONE, provider_call_id="prov-abc"
    )

    record = only_row()
    assert record["channel"] == "PHONE"
    assert record["provider_call_id"] == "prov-abc"
    # No identity anywhere, because none has been established.
    assert record["customer_id"] is None
    assert record["authenticated"] is False
    assert record["auth_status"] == "PENDING"
    assert session_manager.get_session(session.session_id).customer_id is None


def test_a_provider_call_id_is_not_a_customer_id():
    """Even an id shaped exactly like a customer id confers nothing."""
    session = session_manager.create_session()
    recorder.start_session(
        session.session_id, channel=Channel.PHONE, provider_call_id="DEMO001"
    )

    record = only_row()
    assert record["provider_call_id"] == "DEMO001"
    assert record["customer_id"] is None
    assert record["authenticated"] is False


def test_only_the_pin_check_writes_a_customer_id(client, call):
    """Unchanged from the baseline, and restated here as a channel rule."""
    session_id = call["session_id"]

    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    assert only_row()["customer_id"] is None

    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_pin",
            "arguments": {"spoken_pin": PINS["DEMO001"]},
        },
    )
    assert only_row()["customer_id"] == "DEMO001"


# === 8: configuration must not leak secrets =================================


def test_public_settings_carry_no_credentials():
    public = settings.public_settings()

    for secret in (
        "openai_api_key", "database_url", "api_key", "password",
        "sip_password", "didww_password", "didww_api_token", "authorization",
    ):
        assert secret not in public, secret


def test_public_settings_values_contain_no_live_secret():
    """Not just the field names — the values too."""
    rendered = str(settings.public_settings())

    for value in (settings.openai_api_key, settings.database_url):
        if value:
            assert value not in rendered


def test_settings_holds_no_sip_credential_attribute():
    """Credentials are read at point of use, never parked on the settings object."""
    names = {name.lower() for name in vars(settings)}

    for forbidden in ("sip_password", "sip_secret", "didww_password",
                      "didww_api_key", "didww_token", "sip_auth"):
        assert forbidden not in names, forbidden


def test_the_did_number_is_public_but_no_credential_accompanies_it(monkeypatch):
    monkeypatch.setenv("DIDWW_DID_NUMBER", "6531252836")
    fresh = Settings()

    assert fresh.didww_did_number == "6531252836"
    assert not hasattr(fresh, "didww_password")


# === 9: synthetic data only =================================================


def test_demo_mode_defaults_on(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)

    assert Settings().demo_mode is True


def test_ai_disclosure_configuration_exists_and_defaults_on(monkeypatch):
    monkeypatch.delenv("AI_DISCLOSURE_ENABLED", raising=False)
    fresh = Settings()

    assert fresh.ai_disclosure_enabled is True
    assert "synthetic banking data" in fresh.ai_disclosure_text
    assert "AI" in fresh.ai_disclosure_text


def test_the_greeting_is_unchanged_in_phase_one():
    """Configuration exists; the caller's experience does not change yet."""
    from app.agents import speech

    assert speech.WELCOME_SPEECH == (
        "Welcome to ABC Demo Bank. Thank you for calling. "
        "How may I assist you today?"
    )


def test_retention_defaults_are_recorded(monkeypatch):
    monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("TRANSCRIPT_RETENTION_DAYS", raising=False)
    fresh = Settings()

    assert fresh.audit_retention_days == 30
    assert fresh.transcript_retention_days == 7


# === 10: the dashboard handles the new field ================================


def test_the_dashboard_reports_a_channel_for_every_session(client, call):
    rows = client.get("/api/admin/agents").json()["sessions"]

    assert rows
    assert all(row["channel"] in {"WEBRTC", "PHONE"} for row in rows)


def test_the_summary_says_which_channels_are_answering(client):
    channels = client.get("/api/admin/dashboard/summary").json()["channels"]

    assert channels["webrtc_enabled"] is True
    assert channels["phone_enabled"] is False
    assert channels["telephony_provider"] is None


def test_the_dashboard_exposes_no_provider_internals(client, call):
    body = client.get("/api/admin/agents").text

    for internal in ("sip:", "SIP_PUBLIC_URI", "DIDWW_", "password", "sip_domain"):
        assert internal not in body, internal


# === capacity: phone must not get its own unlimited pool ====================


def test_capacity_is_counted_once_for_all_channels():
    """One ceiling, not one per channel.

    `RealtimeManager` counts connections and reservations by banking session
    id, with no notion of channel, so a telephone call added later consumes the
    same slot a browser call would.
    """
    from app.realtime.realtime_manager import RealtimeManager

    source = inspect.getsource(RealtimeManager.used_capacity)
    assert "channel" not in source.lower()
    assert "_connections" in source and "_reserved" in source


def test_no_separate_phone_capacity_setting_exists():
    names = {name.lower() for name in vars(settings)}

    for forbidden in ("phone_max_active_sessions", "sip_max_active_sessions",
                      "telephony_max_active_sessions"):
        assert forbidden not in names, forbidden


# === audit event taxonomy ===================================================


def test_the_event_taxonomy_covers_the_call_lifecycle():
    names = {event.value for event in AuditEvent}

    for required in (
        "CALL_RECEIVED", "CALL_STARTED", "AUTH_STARTED", "AUTH_SUCCEEDED",
        "AUTH_FAILED", "INTENT_RECEIVED", "TOOL_EXECUTED",
        "CALL_END_REQUESTED", "CALL_ENDED", "CALL_FAILED",
    ):
        assert required in names, required


def test_an_event_drops_anything_not_on_the_allow_list():
    payload = safe_event(
        AuditEvent.AUTH_SUCCEEDED,
        customer_id="DEMO001",
        channel="PHONE",
        spoken_pin="4821",
        caller_number="+6531252836",
        transcript="my pin is four eight two one",
        api_key="sk-proj-should-never-appear",
    )

    assert payload["customer_id"] == "DEMO001"
    assert payload["channel"] == "PHONE"
    assert "spoken_pin" not in payload
    assert "caller_number" not in payload
    assert "transcript" not in payload
    assert "api_key" not in payload
    assert "4821" not in str(payload)
    assert "6531252836" not in str(payload)


def test_the_forbidden_and_allowed_field_lists_do_not_overlap():
    assert not (ALLOWED_EVENT_FIELDS & FORBIDDEN_EVENT_FIELDS)


def test_an_event_never_carries_a_free_text_message():
    """Categories, not messages: a message is where an exception string hides."""
    payload = safe_event(AuditEvent.CALL_FAILED, error_category="PROVIDER_UNAVAILABLE")

    assert payload == {
        "event": "CALL_FAILED",
        "error_category": "PROVIDER_UNAVAILABLE",
    }


# === no second transcript path ==============================================


def test_transcripts_have_exactly_one_way_into_storage():
    """A second path is how a PIN would reach the database unredacted."""
    from app.observability import recorder as recorder_module

    source = inspect.getsource(recorder_module)
    # Every ConversationMessage is constructed in record_message, and that one
    # place redacts before it writes. A second construction site would be a
    # second chance to store a PIN.
    assert source.count("ConversationMessage(") == 1

    record_message_source = source[source.index("def record_message(") :]
    record_message_source = record_message_source[
        : record_message_source.index("\n@_safe")
    ]
    assert "redact_transcript" in record_message_source


def test_no_biometric_or_inference_capability_was_introduced():
    """Voice is not an identity mechanism, and never infers anything personal."""
    import app.telephony.channels as channels

    source = (inspect.getsource(channels) + inspect.getsource(recorder)).lower()
    for banned in (
        "voiceprint", "speaker_recognition", "biometric", "emotion",
        "age_estimate", "gender_", "accent", "ethnicity",
    ):
        assert banned not in source, banned
