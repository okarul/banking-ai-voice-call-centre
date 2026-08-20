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


def test_the_dialplan_forks_audio_with_a_core_application():
    """`unicast` is mod_dptools. No third-party module has to be built in."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert 'application="unicast"' in text
    assert "transport=udp" in text
    assert "flags=native" in text
    for third_party in ("mod_audio_stream", "mod_audio_fork", "audio_fork"):
        assert third_party not in text, third_party


def test_the_fork_target_is_inside_the_gateway_media_range():
    """A fork aimed outside the range would hit a port nothing is listening on."""
    text = DIALPLAN.read_text(encoding="utf-8")
    gateway = GatewaySettings()

    assert "remote-port=16384" in text
    assert gateway.media_port_low <= 16384 <= gateway.media_port_high


def test_the_media_port_range_is_bounded_and_small():
    """Every port in an RTP range is a firewall rule."""
    gateway = GatewaySettings()

    assert gateway.media_ports >= 5, "must exceed the five-call target"
    assert gateway.media_ports <= 64, "a range far larger than the ceiling"


def test_the_fixed_fork_port_is_documented_as_single_call():
    """Two calls forking to one port would mix two callers into one stream."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert "FIXED PORT" in text
    assert "single call only" in text.lower()


# === the call must answer, and media must not bypass us =====================


def test_the_call_is_answered_before_the_fork():
    """`unicast` needs established media."""
    text = DIALPLAN.read_text(encoding="utf-8")

    assert text.index('application="answer"') < text.index('application="unicast"')


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
    assert 'expression="^(6531252836|1000)$"' in text


def test_inbound_calls_are_restricted_by_acl():
    settings = params(PROFILE)

    assert settings["apply-inbound-acl"] == "loopback.auto"
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
