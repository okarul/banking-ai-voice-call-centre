# FreeSWITCH — SIP/RTP termination for Channel 2

**Status: VALIDATED locally.** FreeSWITCH 1.10.12 carried one, two and three
concurrent SIP calls end to end into the bank, with real INVITE/200/ACK, real
SDP negotiation and real RTP. 29/29 loopback checks passed.

Nothing external was touched: **no DIDWW change, no real telephone call, no
paid model session** (the model is mocked; the rest is real).

---

## What FreeSWITCH is for, and what it is not

| FreeSWITCH does | FreeSWITCH must never do |
|---|---|
| SIP signalling (INVITE, BYE) | customer authentication |
| RTP media termination | PIN validation |
| codec handling (PCMU) | banking logic or tool calls |
| bridging the call to the gateway | database access |
| propagating call start/end | Realtime prompts or business rules |

All of the right-hand column stays in the banking application, reached only
through the authenticated gateway. FreeSWITCH holds no banking state, needs no
banking credentials, and never sees the media token. A test parses the XML and
asserts no banking term appears in any directive.

## Architecture

```
SIP client ──SIP/RTP──► FreeSWITCH ──SIP/RTP──► gateway UAS ──WSS+token──► bank
                         (container)   bridge    (host)
```

### Corrected during validation

The original design forked audio with the `unicast` dialplan application. **It
does not exist.** The running 1.10.12 build lists 178 `mod_dptools`
applications and none of them is `unicast`; the `rtp` endpoint rejects a plain
address as a dial string; and the modules that *do* stream audio out
(`mod_audio_stream`, `mod_audio_fork`) are third-party and absent from
community images.

FreeSWITCH now **bridges the call over SIP** to the gateway, which needs only
`mod_sofia` — present in every build. That turned out to be the better design:

- **SDP negotiates a media port per call**, the way RTP has always allocated
  them, so there is no fixed port to collide on and concurrent calls are safe
  by construction. The three-call loopback proves it.
- **Call control arrives with the signalling.** An INVITE is a call starting
  and a BYE is a call ending, so nothing is inferred from silence — and the
  `mod_event_socket` shim the fork design would have needed is not needed at
  all.

## Ports (as validated)

| | Value |
|---|---|
| FreeSWITCH SIP | `5070/udp`, published to `127.0.0.1` only |
| FreeSWITCH RTP | `16500–16509/udp`, published to `127.0.0.1` only |
| Gateway SIP UAS | `5080/udp` on the host |
| Gateway media (RTP from FreeSWITCH) | `16384–16407/udp`, 24 ports |
| Gateway → bank | HTTPS + WSS |

Ranges are deliberately small: an RTP range is a firewall rule, and every port
in it has to be open. 24 media ports leaves room above the five-call target
without opening more than the service needs.

**The SIP leg never left Docker.** Caller and FreeSWITCH share a user-defined
network, so no SIP or RTP crossed to Windows except the gateway leg. Published
ports are bound to `127.0.0.1`, never `0.0.0.0`.

## Codec

**PCMU (G.711 µ-law), 8000 Hz, mono, 20 ms = 160 bytes.** One codec,
`disable-transcoding=true`, `absolute_codec_string=PCMU`, so FreeSWITCH never
converts and the bank's single µ-law ↔ PCM16-24k conversion stays the only one
in the path. PCMA is a fallback offer only.

⚠️ **Confirm against the DIDWW trunk before live use.** If it offers G.729 or
Opus, FreeSWITCH would transcode and would need that module built in.

## Running it

```powershell
docker network create channel2-net

docker run -d --name channel2-freeswitch `
  --network channel2-net --add-host=host.docker.internal:host-gateway `
  -p 127.0.0.1:5070:5070/udp -p 127.0.0.1:16500-16509:16500-16509/udp `
  -e SOUND_TYPES=none -e SOUND_RATES=8000 `
  safarov/freeswitch:latest

# Config is copied in after first start (the entrypoint populates vanilla).
docker cp deploy/freeswitch/conf/dialplan/banking.xml `
  channel2-freeswitch:/etc/freeswitch/dialplan/public/00_channel2.xml
docker cp deploy/freeswitch/conf/sip_profiles/channel2.xml `
  channel2-freeswitch:/etc/freeswitch/sip_profiles/channel2.xml
docker exec channel2-freeswitch fs_cli -x "reloadxml"
docker exec channel2-freeswitch fs_cli -x "sofia profile channel2 restart"
```

**Shutdown:** `docker stop channel2-freeswitch; docker rm channel2-freeswitch;
docker network rm channel2-net`. Removing the container removes the whole SIP
surface; the bank and the gateway are unaffected.

### Variables the config needs

Set in `vars.xml`. Names are ours; the values are deployment-specific.

| Variable | Loopback value | Meaning |
|---|---|---|
| `channel2_sip_port` | `5070` | where this profile listens (`5060` in production) |
| `channel2_gateway_uri` | `192.168.65.254:5080` | where the gateway answers SIP |
| `channel2_inbound_acl` | `channel2_test` | who may offer a call (`loopback.auto` by default) |
| `local_ip_v4` | container IP | **must be set explicitly** — see below |
| `external_sip_ip` / `external_rtp_ip` | container IP | what SDP advertises |

## Four things that cost time, so they are written down

1. **`local_ip_v4` auto-detects wrongly under Docker Desktop.** FreeSWITCH
   picked the *host* address (`192.168.65.254`) as its own, so bridging to the
   gateway at that address looked like bridging to itself and returned 480. Set
   it explicitly to the container IP.

2. **Files in `dialplan/public/` must not declare a `<context>`.** They are
   included from *inside* `<context name="public">`. A nested context is
   silently ignored: the call arrives, matches nothing, and comes back 480 with
   nothing in the log naming the file. Asserted by test.

3. **A `Contact` header without a port means 5060.** The gateway answered on
   5080, FreeSWITCH addressed its ACK to 5060, and the dialog opened and then
   died with no error anywhere. Asserted by test.

4. **Stock FreeSWITCH owns 1000–1019.** Those extensions answer with voicemail
   before any custom dialplan is reached. The loopback range is 90xx.

## NAT and firewall — for live use, not opened now

| | Requirement |
|---|---|
| SIP | `5060/udp+tcp` (or `5061/tls`) inbound, **restricted to DIDWW signalling ranges** |
| RTP | the configured range inbound, restricted to DIDWW media ranges |
| Public address | static public IP or stable FQDN for the SIP host |
| NAT | set `ext-sip-ip` / `ext-rtp-ip` to the public address and forward the RTP range, or SDP advertises an unreachable address and audio is one-way |
| Gateway SIP UAS | reachable from FreeSWITCH **only**, never public; use `allowed_peers` |
| Bank | reachable from the gateway only |

## Known limitations

- **`auth-calls=false`.** Safe only while the port is restricted by ACL. Before
  a carrier can reach it, use an IP allow-list of DIDWW's signalling ranges or
  SIP digest credentials.
- **No TLS on the SIP leg.** Loopback-only today. Prefer SIP-TLS + SRTP, or a
  private link, before live use. The gateway's leg to the bank is TLS-verified
  regardless (`GATEWAY_VERIFY_TLS=true`).
- **Container IP is pinned in `vars.xml`** for the loopback run and changes if
  the container is recreated. A deployment sets a stable address.
