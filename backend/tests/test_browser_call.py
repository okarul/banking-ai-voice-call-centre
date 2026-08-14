"""Phase 10 deterministic tests: the browser's telephone API.

Everything here runs offline. The one thing that would cost money — minting a
client secret with OpenAI — is replaced, so these tests exercise the whole
`/api/call/*` surface with no network, no key and no browser.

What they defend, in order of how much it would hurt to get wrong:

1. The permanent API key never leaves the backend.
2. The browser cannot name a customer; identity stays the backend session's.
3. Hanging up really does clean up, however many times it is done.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.agents.registry import FORBIDDEN_ARGUMENTS
from app.auth import authentication
from app.config import settings
from app.main import app
from app.realtime.browser_calls import (
    BrowserCall,
    browser_call_manager,
    sweep_idle_calls,
)
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.realtime.webrtc import (
    browser_session_config,
    execute_tool,
    mint_client_secret,
    tool_schemas,
)
from app.sessions import session_manager

# Synthetic demo credentials from the Phase 2 seed. Not real.
PINS = {"DEMO001": "4821", "DEMO002": "7315"}

FAKE_SECRET = {"value": "ek_test_not_a_real_secret", "expires_at": 1893456000}


@pytest.fixture(autouse=True)
def clean_slate():
    """No call or session may outlive the test that made it."""
    yield
    asyncio.run(browser_call_manager.close_all())
    session_manager.clear()


@pytest.fixture
def client(monkeypatch):
    """A test client whose calls never reach OpenAI."""

    async def fake_mint(**_kwargs):
        return dict(FAKE_SECRET)

    monkeypatch.setattr("app.routers.call.mint_client_secret", fake_mint)
    return TestClient(app)


def start_call(client) -> dict:
    response = client.post("/api/call/start")
    assert response.status_code == 201, response.text
    return response.json()


def authenticate(session_id: str, customer_id: str = "DEMO001") -> None:
    """Take a session through the real deterministic checks."""
    assert authentication.verify_customer(session_id, customer_id)["success"]
    assert authentication.verify_pin(session_id, PINS[customer_id])["success"]


# === 1-2: starting a call ===================================================


def test_starting_a_call_creates_a_banking_session(client):
    call = start_call(client)

    assert call["session_id"].startswith("SESSION-")
    assert call["realtime_session_id"].startswith("REALTIME-")

    session = session_manager.get_session(call["session_id"])
    assert session is not None
    # Nobody is verified yet: who is calling is settled by voice.
    assert session.authenticated is False
    assert session.customer_id is None


def test_starting_a_call_registers_exactly_one_live_call(client):
    call = start_call(client)

    assert browser_call_manager.is_active(call["session_id"])
    assert browser_call_manager.active_count() == 1

    session = session_manager.get_session(call["session_id"])
    assert session.realtime_session_id == call["realtime_session_id"]


def test_two_browsers_get_two_separate_calls(client):
    """Two windows, two customers, nothing shared."""
    first = start_call(client)
    second = start_call(client)

    assert first["session_id"] != second["session_id"]
    assert first["realtime_session_id"] != second["realtime_session_id"]
    assert browser_call_manager.active_count() == 2

    authenticate(first["session_id"], "DEMO001")
    authenticate(second["session_id"], "DEMO002")

    assert session_manager.get_session(first["session_id"]).customer_id == "DEMO001"
    assert session_manager.get_session(second["session_id"]).customer_id == "DEMO002"


def test_the_browser_cannot_ask_to_be_a_particular_customer(client):
    """There is no way to name a customer when starting a call."""
    response = client.post("/api/call/start", json={"customer_id": "DEMO003"})

    assert response.status_code == 201
    session = session_manager.get_session(response.json()["session_id"])
    assert session.customer_id is None
    assert session.authenticated is False


# === 3-4: the permanent key stays here ======================================


def test_the_start_response_carries_only_a_short_lived_credential(client):
    call = start_call(client)

    assert call["client_secret"]["value"] == FAKE_SECRET["value"]
    assert call["client_secret"]["expires_at"] == FAKE_SECRET["expires_at"]
    # A client secret, not an API key.
    assert not call["client_secret"]["value"].startswith("sk-")


def test_no_response_from_the_call_api_contains_the_permanent_key(client):
    call = start_call(client)
    session_id = call["session_id"]
    authenticate(session_id)

    bodies = [
        client.post("/api/call/start").text,
        client.get(f"/api/call/state/{session_id}").text,
        client.post(
            "/api/call/tool",
            json={
                "session_id": session_id,
                "name": "get_account_balance",
                "arguments": {"account_type": "Savings"},
            },
        ).text,
        client.post("/api/call/end", json={"session_id": session_id}).text,
    ]

    for body in bodies:
        assert "sk-" not in body
        assert "OPENAI_API_KEY" not in body
        if settings.openai_api_key:
            assert settings.openai_api_key not in body


def test_the_session_configuration_sent_to_openai_holds_no_key():
    body = json.dumps(browser_session_config())

    assert "api_key" not in body
    assert "sk-" not in body
    if settings.openai_api_key:
        assert settings.openai_api_key not in body


def test_minting_a_credential_without_a_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)

    with pytest.raises(RealtimeSessionError) as raised:
        asyncio.run(mint_client_secret())

    assert raised.value.reason == Reason.REALTIME_NOT_CONFIGURED


def test_a_failed_mint_leaves_no_session_behind(monkeypatch):
    """A call that could not be opened must not leak a banking session."""

    async def refuse(**_kwargs):
        raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED)

    monkeypatch.setattr("app.routers.call.mint_client_secret", refuse)
    before = session_manager.active_session_count()

    response = TestClient(app).post("/api/call/start")

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Unable to connect to voice banking. Please try again."
    )
    assert session_manager.active_session_count() == before
    assert browser_call_manager.active_count() == 0


# === the tool surface the model is shown ====================================


def test_the_model_is_never_offered_an_identity_parameter():
    for schema in tool_schemas():
        properties = set(schema["parameters"].get("properties", {}))
        assert not properties & FORBIDDEN_ARGUMENTS, schema["name"]


def test_the_browser_session_offers_exactly_the_backend_tools():
    from app.realtime.tools import BANKING_TOOLS

    offered = {schema["name"] for schema in browser_session_config()["tools"]}

    assert offered == {tool.name for tool in BANKING_TOOLS}


def test_the_caller_is_transcribed_so_the_scope_gate_can_rule():
    """Transcription is on, deliberately, and the PIN is protected elsewhere.

    It was off originally, to keep the spoken PIN out of the browser. It is on
    now because the scope gate has to see the caller's words to decide in
    Python whether the bank may answer at all — and a control that depends on
    the model's goodwill is not a control.

    The PIN is no more exposed for it: the transcript reaches only the caller's
    own browser and this backend, which already receives the spoken PIN through
    submit_pin. What matters is that it is never shown, stored or logged, which
    the tests below and in test_banking_scope.py pin down.
    """
    audio_input = browser_session_config()["audio"]["input"]

    assert audio_input["transcription"]["model"]
    assert audio_input["turn_detection"]["interrupt_response"] is True


def test_the_page_never_displays_what_the_caller_said():
    """Only the assistant's own words may appear, so a PIN cannot be shown."""
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    page = (frontend / "app.js").read_text(encoding="utf-8")

    # The developer view appends assistant transcripts only; the caller's
    # transcript is used for the scope check and then dropped.
    assert "input_audio_transcription" not in page
    for store in ("localStorage", "sessionStorage", "document.cookie"):
        assert store not in page


# === 9-11: authorization still decides everything ===========================


def test_an_unauthenticated_call_cannot_read_an_account(client):
    call = start_call(client)

    response = client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "success": False,
        "reason": "NOT_AUTHENTICATED",
    }


def test_an_authenticated_call_reads_its_own_account(client):
    call = start_call(client)
    authenticate(call["session_id"], "DEMO001")

    response = client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    )

    result = response.json()["result"]
    assert result["success"] is True
    assert result["masked_account"] == "XXXX1001"
    assert result["available_balance"] == "12450.75"


@pytest.mark.parametrize("field", sorted(FORBIDDEN_ARGUMENTS))
def test_the_browser_cannot_smuggle_an_identity_into_a_tool_call(client, field):
    """The model may choose an account type. It may not choose a customer."""
    call = start_call(client)
    authenticate(call["session_id"], "DEMO001")

    response = client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings", field: "DEMO002"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "IDENTITY_NOT_ACCEPTED"


def test_one_calls_tools_cannot_reach_another_calls_customer(client):
    """DEMO002's session id returns DEMO002's data, never DEMO001's."""
    first = start_call(client)
    second = start_call(client)
    authenticate(first["session_id"], "DEMO001")
    authenticate(second["session_id"], "DEMO002")

    result = client.post(
        "/api/call/tool",
        json={
            "session_id": second["session_id"],
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    ).json()["result"]

    assert result["masked_account"] == "XXXX1002"


def test_an_unknown_tool_is_refused(client):
    call = start_call(client)

    response = client.post(
        "/api/call/tool",
        json={"session_id": call["session_id"], "name": "transfer_money",
              "arguments": {}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "UNKNOWN_TOOL"


def test_a_tool_call_on_an_unknown_session_is_refused(client):
    response = client.post(
        "/api/call/tool",
        json={
            "session_id": "SESSION-does-not-exist",
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    )

    assert response.status_code == 404


def test_a_tool_error_never_exposes_the_database(client):
    call = start_call(client)

    body = client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "get_loan_balance",
            "arguments": {"loan_type": "Home Loan"},
        },
    ).text

    for leak in ("postgresql", "psycopg", "SELECT", "Traceback", "password"):
        assert leak not in body


def test_a_browser_tool_call_runs_the_same_code_as_the_voice_path():
    """No second implementation of banking for the browser."""
    session = session_manager.create_session()
    authenticate(session.session_id, "DEMO001")

    result = asyncio.run(
        execute_tool("get_loan_balance", session.session_id, {"loan_type": "Home Loan"})
    )

    assert result["loan_reference"] == "HL-DEMO001"
    assert result["outstanding_balance"] == "284500.00"


# === 5-8: hanging up ========================================================


def test_ending_a_call_closes_it_and_ends_the_session(client):
    call = start_call(client)
    authenticate(call["session_id"])

    response = client.post("/api/call/end", json={"session_id": call["session_id"]})

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "call_closed": True,
        "session_ended": True,
    }
    assert browser_call_manager.active_count() == 0
    assert session_manager.get_session(call["session_id"]) is None


def test_ending_a_call_twice_is_safe(client):
    call = start_call(client)

    first = client.post("/api/call/end", json={"session_id": call["session_id"]})
    second = client.post("/api/call/end", json={"session_id": call["session_id"]})

    assert first.json()["session_ended"] is True
    assert second.status_code == 200
    assert second.json() == {
        "success": True,
        "call_closed": False,
        "session_ended": False,
    }


def test_ending_a_call_that_never_existed_is_safe(client):
    response = client.post("/api/call/end", json={"session_id": "SESSION-imaginary"})

    assert response.status_code == 200
    assert response.json()["session_ended"] is False


def test_ending_one_call_leaves_the_other_alone(client):
    first = start_call(client)
    second = start_call(client)

    client.post("/api/call/end", json={"session_id": first["session_id"]})

    assert session_manager.get_session(first["session_id"]) is None
    assert session_manager.get_session(second["session_id"]) is not None
    assert browser_call_manager.active_count() == 1
    assert browser_call_manager.is_active(second["session_id"])


def test_an_abandoned_call_is_reclaimed_by_the_server(client):
    """A tab that closes without saying so must not hold a session open."""
    call = start_call(client)
    # The browser vanished; only the call registration is left behind.
    session_manager.destroy_session(call["session_id"])

    reclaimed = asyncio.run(sweep_idle_calls())

    assert reclaimed == 1
    assert browser_call_manager.active_count() == 0


def test_a_live_call_is_not_swept_away(client):
    call = start_call(client)

    assert asyncio.run(sweep_idle_calls()) == 0
    assert browser_call_manager.is_active(call["session_id"])


def test_closing_a_browser_call_releases_nothing_on_the_network():
    """The peer connection belongs to the page; this side has nothing to close."""
    assert asyncio.run(BrowserCall(banking_session_id="SESSION-x").close()) is None


# === the developer view =====================================================


def test_call_state_reports_progress_without_exposing_secrets(client):
    call = start_call(client)
    authenticate(call["session_id"], "DEMO001")

    state = client.get(f"/api/call/state/{call['session_id']}").json()

    assert state["authenticated"] is True
    assert state["customer_id"] == "DEMO001"
    assert state["call_active"] is True
    for forbidden in ("pin", "hash", "password", "secret", "key"):
        assert forbidden not in json.dumps(state).lower()


def test_call_state_of_an_unknown_session_is_a_plain_404(client):
    response = client.get("/api/call/state/SESSION-imaginary")

    assert response.status_code == 404
    assert response.json()["detail"] == "This banking session is no longer active."


# === 12-14: nothing older broke =============================================


def test_health_still_works(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_the_developer_agent_route_still_works(client):
    call = start_call(client)
    authenticate(call["session_id"], "DEMO001")

    response = client.post(
        "/dev/agents/turn",
        json={"session_id": call["session_id"], "text": "What is my savings balance?"},
    )

    assert response.status_code == 200
    assert "12,450.75" in response.json()["speech"]


def test_the_developer_realtime_route_still_works(client):
    status = client.get("/dev/realtime/status").json()

    assert status["model"] == settings.realtime_model
    assert "sk-" not in json.dumps(status)


def test_the_customer_api_is_reachable_only_from_the_configured_origin(client):
    """CORS is a named list, never a wildcard."""
    allowed = settings.frontend_origins[0]

    permitted = client.post("/api/call/start", headers={"Origin": allowed})
    assert permitted.headers["access-control-allow-origin"] == allowed

    refused = client.post(
        "/api/call/start", headers={"Origin": "http://evil.example.com"}
    )
    assert "access-control-allow-origin" not in refused.headers
    assert "*" not in settings.frontend_origins
