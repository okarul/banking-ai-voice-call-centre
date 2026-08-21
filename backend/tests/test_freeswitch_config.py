"""The FreeSWITCH configuration, checked as configuration rather than prose.

These files are not run by this suite — FreeSWITCH is not installed. What is
checked is that they are well-formed XML and that the decisions written into
them still hold: one codec, bounded ports, no banking logic, and no accidental
public exposure.

A config file nobody parses is a config file that is wrong the first time
somebody edits it.
"""

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from gateway.config import GatewaySettings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "freeswitch"
PROFILE = DEPLOY / "conf" / "sip_profiles" / "channel2.xml"
DIALPLAN = DEPLOY / "conf" / "dialplan" / "banking.xml"


def params(path: Path) -> dict[str, str]:
    root = ElementTree.parse(path).getroot()
    return {
        element.get("name"): element.get("value")
        for element in root.iter("param")
    }


# === the files parse ========================================================


@pytest.mark.parametrize("path", [PROFILE, DIALPLAN])
def test_the_configuration_is_well_formed_xml(path):
    assert path.exists(), path
    assert ElementTree.parse(path).getroot() is not None


# === codec ==================================================================


def test_only_g711_is_offered():
    """One codec, so FreeSWITCH never transcodes.

    The bank converts µ-law once for the model and back; a second codec here
    would add a second conversion for no benefit.
    """
    settings = params(PROFILE)

    assert settings["inbound-codec-prefs"] == "PCMU,PCMA"
    assert settings["outbound-codec-prefs"] == "PCMU"
    assert settings["disable-transcoding"] == "true"


def test_the_dialplan_pins_the_codec_for_the_call():
    text = DIALPLAN.read_text(encoding="utf-8")

    assert "absolute_codec_string=PCMU" in text


def test_no_wideband_or_compressed_codec_is_configured():
    """G.729 or Opus would need modules that may not be built, and would put a
    transcode in a path that does not need one."""
    text = PROFILE.read_text(encoding="utf-8") + DIALPLAN.read_text(encoding="utf-8")

    for unwanted in ("G729", "OPUS", "G722", "SPEEX", "AMR"):
        assert unwanted not in text.upper(), unwanted


# === media fork =============================================================


def test_the_dialplan_bridges_with_a_core_module_only():
    """mod_sofia ships in every build. The audio-fork modules do not.

    An earlier design used the `unicast` application. It does not exist in
    FreeSWITCH 1.10.12 — verified against a running build, whose mod_dptools
    offers 178 applications and none of them is `unicast` — so the dialplan
    bridges the call over SIP instead, which needs only mod_sofia.
    """
    text = directives(DIALPLAN)

    assert 'application="bridge"' in text
    assert "sofia/channel2/sip:gateway@" in text
    # Checked against directives, not comments: the comment above the dialplan
    # legitimately explains why `unicast` is absent.
    for absent in ("unicast", "mod_audio_stream", "mod_audio_fork", "audio_fork"):
        assert absent not in text, absent


def test_the_bridge_target_is_configuration_not_a_literal_address():
    """A hard-coded address is a gateway somebody else can point this at."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert "channel2_gateway_uri" in text
    assert not re.search(r"sip:gateway@\d+\.\d+\.\d+\.\d+", text)


def test_no_media_port_is_pinned_in_the_dialplan():
    """SDP allocates a port per call; a pinned one would mix two callers.

    Stronger than the check it replaces. The old design forked to a fixed port
    and could only ever be documented as single-call; bridging removes the
    fixed port entirely, so concurrent calls are safe by construction — and the
    three-call loopback proves it.
    """
    text = DIALPLAN.read_text(encoding="utf-8")

    assert "remote-port=" not in text
    assert "local-port=" not in text


def test_the_dialplan_declares_no_context_of_its_own():
    """Files in dialplan/public/ are included *inside* the public context.

    A nested <context> is silently ignored, and the symptom is a call that
    reaches FreeSWITCH, matches no extension and comes back 480 with nothing in
    the log naming this file. Worth a test precisely because it fails silently.
    """
    root = ElementTree.parse(DIALPLAN).getroot()

    assert root.tag == "include"
    assert root.find("context") is None
    assert root.find("extension") is not None


# === the call must answer, and media must not bypass us =====================


def test_the_call_is_not_answered_before_bridging():
    """Answering first would connect the caller to FreeSWITCH and leave them in
    silence while the gateway leg is set up. Bridging passes the far side's
    answer straight through, so the caller hears the bank when it is ready."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert 'application="answer"' not in text
    assert 'data="hangup_after_bridge=true"' in text


def test_media_is_not_allowed_to_bypass_freeswitch():
    """A call whose media bypassed us could not be forked to the gateway."""
    settings = params(PROFILE)

    assert settings["inbound-bypass-media"] == "false"
    assert settings["inbound-proxy-media"] == "false"


# === no banking logic in the telephony layer ================================


def directives(path: Path) -> str:
    """The configuration with comments stripped.

    Comments explain *why* and legitimately discuss authentication, banking and
    what must not happen here. What matters is that none of it appears in what
    FreeSWITCH actually acts on.
    """
    return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)


def test_the_dialplan_contains_no_banking_logic():
    """FreeSWITCH terminates the network. It decides nothing about a customer."""
    text = (directives(DIALPLAN) + directives(PROFILE)).lower()

    for banking in (
        "pin", "customer_id", "demo001", "balance", "loan", "account",
        "authenticate", "openai", "postgres", "database",
    ):
        assert banking not in text, banking


def test_the_configuration_carries_no_credential():
    text = (directives(DIALPLAN) + directives(PROFILE)).lower()

    for secret in ("password", "api_key", "apikey", "secret", "token", "sk-"):
        assert secret not in text, secret


# === exposure ===============================================================


def test_the_dialplan_rejects_unknown_destinations():
    """An open dialplan on a reachable SIP port is a free long-distance service."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert "404 Not Found" in text
    assert 'expression="^(6531252836|90[0-9][0-9])$"' in text


def test_inbound_calls_are_restricted_by_acl():
    settings = params(PROFILE)

    # The ACL is configuration so a deployment can name the carrier's ranges and
    # the loopback test can name the container network. What must never happen
    # is an unrestricted profile, so the parameter has to be present and the
    # file has to state what it defaults to.
    assert settings["apply-inbound-acl"] == "$${channel2_inbound_acl}"
    assert "loopback" in PROFILE.read_text(encoding="utf-8").lower()
    assert settings["accept-blind-reg"] == "false"
    assert settings["accept-blind-auth"] == "false"


def test_the_profile_warns_that_open_auth_is_loopback_only():
    """`auth-calls=false` is safe while loopback-bound and only then."""
    text = PROFILE.read_text(encoding="utf-8")

    assert params(PROFILE)["auth-calls"] == "false"
    assert "before live use" in text.lower()
    assert "loopback" in text.lower()


def test_the_gateway_media_socket_binds_to_loopback_by_default():
    """Nothing outside this machine has business sending audio here yet."""
    assert GatewaySettings().media_bind_host == "127.0.0.1"


def test_tls_verification_to_the_bank_is_still_on_by_default():
    """Phase 4B must not weaken the gateway's own transport security."""
    assert GatewaySettings().verify_tls is True
