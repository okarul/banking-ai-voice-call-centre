# FreeSWITCH — SIP/RTP termination for Channel 2

**Status: configuration only. FreeSWITCH is not installed and has not been
run.** These files are the deployment artifacts, reviewed and committed so the
container's behaviour is version-controlled rather than typed into a shell.

Nothing here has been executed against DIDWW, and nothing has been executed on
this machine — see *Deployment decision* below.

---

## What FreeSWITCH is for, and what it is not

It terminates the telephone network and nothing else:

| FreeSWITCH does | FreeSWITCH must never do |
|---|---|
| SIP signalling (INVITE, BYE) | customer authentication |
| RTP media termination | PIN validation |
| codec handling (PCMU) | banking logic or tool calls |
| forking call audio to the gateway | database access |
| propagating call start/end | Realtime prompts or business rules |

All of the right-hand column stays in the banking application, reached only
through the authenticated gateway. FreeSWITCH holds no banking state and needs
no banking credentials.

## Architecture

```
SIP client ──SIP/RTP──► FreeSWITCH ──unicast UDP──► gateway ──WSS+token──► bank
   (local)              (container)   µ-law 20ms    (Windows host)
```

The `unicast` application (`mod_dptools`, core — no third-party module) forks a
call's audio to a UDP socket and reads the replies back into the call. The
gateway's `UdpMediaSource` is the far end of that socket; it accepts either
bare µ-law payloads or RTP framing, detected per datagram, so the exact framing
a given build produces is a detail rather than a prerequisite.

## Ports

| | Value | Notes |
|---|---|---|
| SIP listen address | `127.0.0.1` (host) / `0.0.0.0` (container) | loopback only until live |
| SIP port | `5060/udp+tcp` | published from the container |
| RTP port range (FreeSWITCH ↔ SIP client) | `16500–16530/udp` | 31 ports |
| Media fork range (FreeSWITCH → gateway) | `16384–16407/udp` | 24 ports, `GATEWAY_MEDIA_PORT_*` |
| Gateway → bank | `443/tcp` (HTTPS + WSS) | |

Both ranges are deliberately small. An RTP range is a firewall rule: every port
in it has to be open, and a range far larger than the call ceiling is a larger
opening than the service needs. 24 fork ports leaves generous room above the
five-call application target.

## Codec

**PCMU (G.711 µ-law), 8000 Hz, mono, 20 ms = 160 bytes.** One codec, offered
and accepted, with no transcoding step inside FreeSWITCH:

```
carrier PCMU ──► FreeSWITCH (no transcode) ──► gateway ──► bank converts once
```

The bank converts µ-law 8 kHz to PCM16 24 kHz for the model and back. That is
the only conversion in the path, and adding a second codec here would add a
second one for no benefit.

`PCMA` (A-law) is configured as a fallback offer only. **This must be confirmed
against the DIDWW trunk before live use** — if the trunk offers G.729 or Opus,
FreeSWITCH would transcode, which needs the relevant module present.

## Deployment decision — not made

FreeSWITCH is **not installed on this machine**, and installing it needs a
choice this project should not make silently:

- The official `signalwire/freeswitch` image **requires authentication**
  (SignalWire moved its packages behind a token). Only community images
  (`safarov/freeswitch`, `dheaps/freeswitch`) pull anonymously.
- Docker Desktop on Windows runs Linux containers in a VM, so `--network host`
  does not reach Windows. RTP therefore needs explicitly published UDP ranges.
- WSL2 *mirrored* networking would make this clean, but enabling it restarts
  WSL — which would stop every other container on this machine.

See `docs/PHASE4_LIVE_ACTIVATION.md` for the options and their consequences.

## Per-call media ports

The dialplan below forks to a **fixed** port, which is correct for a
single-call loopback test and wrong for concurrent calls: two calls forking to
one port would mix two callers into one stream.

Concurrent calls need a port allocated per call, which means FreeSWITCH asking
the gateway for one — `mod_event_socket` outbound (`socket` application), also
core. The gateway's `MediaPortAllocator` already hands out ports one call at a
time; what is missing is the ESL server that answers FreeSWITCH.

**That shim is deliberately not written yet.** It is a protocol implementation
that cannot be validated without FreeSWITCH running, and writing it blind
produces code that looks finished and fails on first contact. The multi-call
isolation it would enable is already proven at the UDP layer by
`tests/test_gateway_udp_media.py`, which drives real RTP over real sockets for
one, two, three and five concurrent calls.
