# Phase 4C — deployment plan for a public SIP/RTP host

**Status: plan only. Nothing provisioned, nothing changed at DIDWW, no call
placed.** This is the design to review before anything is bought or booted.

The one blocker from Phase 4C is that DIDWW has nowhere to send a call. This
plan puts FreeSWITCH and the media gateway on a small public VM and leaves the
bank where it is.

---

## Topology

```
handset ──PSTN──► DIDWW ──SIP/RTP──► ┌─────────── VM (public IP) ───────────┐
                                     │  FreeSWITCH  ──SIP/RTP (loopback)──► │
                                     │  gateway                             │
                                     └──────────────┬───────────────────────┘
                                                    │ HTTPS + WSS
                                            private link (WireGuard)
                                                    │
                                     ┌──────────────▼───────────────────────┐
                                     │  bank + PostgreSQL (dev machine)     │
                                     └──────────────────────────────────────┘
```

Only the VM is exposed. The bank, its database and the OpenAI key never sit on
an internet-facing host.

---

## 1. Minimum VM size

**1 vCPU, 1 GB RAM, 20 GB disk.**

Three concurrent G.711 calls is a trivial load: 64 kbit/s each way per call, and
FreeSWITCH does no transcoding in this design. The gateway's µ-law conversion is
pure Python at roughly 1.5 % of one core for five calls, measured in Phase 3.

Take 2 vCPU / 2 GB if the provider's 1 GB tier has no swap — FreeSWITCH's
default configuration is memory-hungry at startup even when idle. Do not size
for the *application* target of 5 calls; size for the *live* ceiling of 3.

## 2. Distribution

**Debian 12 (bookworm)** or **Ubuntu 22.04/24.04 LTS**.

Either is fine because FreeSWITCH runs in a container, so the host only needs
Docker and a firewall. Debian 12 is the lighter of the two. Avoid anything with
an unpredictable kernel or an SELinux policy you would have to reason about
while debugging RTP.

## 3. Public IP

**A static public IPv4 address is required.**

- Static, not ephemeral: DIDWW routing points at it, and a changed address is a
  silently dead trunk.
- IPv4 specifically. Do not plan on IPv6-only; carrier SIP interop over IPv6 is
  uneven.
- **Not behind carrier-grade NAT.** Confirm the VM's own interface holds the
  public address (`ip addr`), not a private one with a NAT in front. If it is
  NATed, `ext-sip-ip` / `ext-rtp-ip` must be set to the public address or media
  arrives one-way.
- A DNS A record is optional for SIP but **required if SIP-TLS is used**, since
  the certificate is validated against a name.

## 4. SIP signalling port

| | |
|---|---|
| Plain UDP/TCP | **5060** |
| TLS | **5061** — preferred if the trunk supports it (§8) |

`5060` is the default in the committed profile via `channel2_sip_port`. Keep it:
a non-standard port is not security, and it complicates DIDWW configuration for
no benefit once the ACL is in place.

## 5. RTP UDP range

**`16500–16530/udp` — 31 ports.**

That is the range already in the committed `switch.conf`/profile plan and is
deliberately small. Sizing: RTP conventionally uses two ports per call, so 31
ports covers 15 concurrent calls — five times the live ceiling of 3, with room
for ports still in TIME_WAIT.

Do **not** open the FreeSWITCH default of `16384–32768`. Every port in an RTP
range is a firewall rule, and a range 500× larger than the call ceiling is a
larger opening than the service needs.

Separately, `16384–16407/udp` is the gateway's own media range
(`GATEWAY_MEDIA_PORT_LOW`/`HIGH`). It stays **loopback-only** — see §10.

## 6. Firewall / security-group rules

Default deny inbound. Then exactly:

| Port | Proto | Source | Purpose |
|---|---|---|---|
| `5060` (or `5061`) | UDP + TCP | **DIDWW signalling ranges only** | SIP |
| `16500–16530` | UDP | **DIDWW media ranges only** | RTP from carrier |
| `51820` | UDP | your dev machine's egress IP, or any | WireGuard to the bank |
| `22` | TCP | **your IP only** | SSH administration |

Never opened: `5080` (gateway SIP), `16384–16407` (gateway media), `8001`
(bank), `5435` (PostgreSQL), the dashboard, or any admin surface. The first two
are loopback-only on the VM; the last three are not on the VM at all.

Apply the rules in **both** the provider's security group and the host firewall
(`ufw`/`nftables`). A provider security group is not a substitute for a host
firewall — one misconfigured console click should not expose a SIP port.

⚠️ **DIDWW's published signalling and media ranges are required before this is
usable and I do not have them.** Until they are known, the only alternatives are
an unrestricted SIP port (unacceptable) or SIP digest authentication (§7 of the
Phase 4C brief).

## 7. FreeSWITCH installation

**Docker, using the image already validated in Phase 4B:**

```
safarov/freeswitch:latest
sha256:b31c743f4c911a19687c61e3214968f2a24f93f9d3d667cc26284192e158ffc6
FreeSWITCH 1.10.12
```

Reasons: this exact image carried the 1/2/3-call loopback, the official
`signalwire/freeswitch` image now requires a SignalWire token, and containerising
keeps the host to Docker plus a firewall.

Pin the digest, not `:latest`, so a rebuild cannot silently change the SIP stack
under a working trunk.

On the VM, publish narrowly and **do not use `--network host`** — it would put
FreeSWITCH on every interface at once, including the WireGuard link:

```bash
docker run -d --name channel2-freeswitch --restart unless-stopped \
  -p <public-ip>:5060:5060/udp -p <public-ip>:5060:5060/tcp \
  -p <public-ip>:16500-16530:16500-16530/udp \
  -v /opt/channel2/freeswitch/dialplan/banking.xml:/etc/freeswitch/dialplan/public/00_channel2.xml:ro \
  -v /opt/channel2/freeswitch/sip_profiles/channel2.xml:/etc/freeswitch/sip_profiles/channel2.xml:ro \
  -v /opt/channel2/freeswitch/vars.xml:/etc/freeswitch/vars.xml:ro \
  -e SOUND_TYPES=none -e SOUND_RATES=8000 \
  safarov/freeswitch@sha256:b31c743f...
```

Binding published ports to the **public IP specifically** (not `0.0.0.0`) keeps
them off the WireGuard interface.

Config files come from `deploy/freeswitch/` in this repository. Note the four
traps recorded in `deploy/freeswitch/README.md` — in particular `local_ip_v4`
must be set explicitly, and the dialplan file must not declare its own
`<context>`.

## 8. TLS and certificates

Two different legs; do not confuse them.

**SIP leg (DIDWW → FreeSWITCH).** Use SIP-TLS on `5061` with SRTP **if the trunk
supports it** — I cannot confirm that without the account. Requires a DNS name
and a real certificate (Let's Encrypt), and `tls` enabled in the profile.

If the trunk does not support TLS, state it plainly: **the signalling leg is
plaintext, and the only protection is the IP ACL.** That is acceptable for a
synthetic-data demonstration and must not be described as secured. Do not
disable certificate validation anywhere to make something work.

**Gateway → bank leg.** `GATEWAY_VERIFY_TLS=true`, always. If the bank is
reached over WireGuard, the link is already encrypted and a self-signed
certificate would still fail verification — so either terminate real TLS at the
bank (Caddy with a DNS-01 certificate) or use plain HTTP **inside** the
WireGuard tunnel and leave `verify_tls` untouched for any future public path.
Never set `GATEWAY_VERIFY_TLS=false` as the deployed answer.

## 9. How FreeSWITCH and the gateway reach the bank

Two hops, secured differently:

**FreeSWITCH → gateway:** both on the VM, over **loopback**. FreeSWITCH bridges
to `sip:gateway@127.0.0.1:5080`. No network exposure at all, so the gateway's
SIP port never leaves the machine. `GATEWAY_SIP_ALLOWED_PEERS=127.0.0.1`.

**Gateway → bank:** over a **WireGuard** point-to-point link to the dev machine.
The gateway holds `GATEWAY_BACKEND_URL` pointing at the bank's WireGuard
address, plus `GATEWAY_WEBHOOK_SECRET`. The webhook HMAC still applies —
transport encryption and message authentication are doing different jobs, and
the signature is what proves *which* gateway is asking.

WireGuard rather than Tailscale if you want no third-party coordination
service; Tailscale if you want it to survive the dev machine changing networks.
Either is a small install on the dev machine.

## 10. Should the gateway run on the VM?

**Yes — on the VM, beside FreeSWITCH. This is not really optional.**

If the gateway stayed on the dev machine, RTP would have to travel from
FreeSWITCH on the VM to the dev machine — inbound UDP to a host with no public
address, which is precisely the blocker this plan exists to remove. Co-locating
them turns that hop into loopback.

The bank is different, and **should stay off the VM**: it holds the OpenAI key
and the banking database, and the VM is the internet-facing host. Keeping the
bank behind WireGuard means a compromised VM yields a media relay and a webhook
secret, not the model credentials or the customer data.

The cost of that choice: the dev machine must be running during calls, and the
tunnel must be up. For a classroom demonstration that is acceptable. If it ever
is not, move the bank to a second private VM rather than onto the public one.

## 11. Environment variables

**Gateway (on the VM)** — every name below is read by `gateway/config.py`:

```bash
GATEWAY_WEBHOOK_SECRET=<shared with the bank; generate with secrets.token_urlsafe(32)>
GATEWAY_BACKEND_URL=http://10.66.0.2:8001      # the bank over WireGuard
GATEWAY_VERIFY_TLS=true
GATEWAY_PROVIDER_NAME=DIDWW

GATEWAY_SIP_HOST=127.0.0.1                     # loopback: FreeSWITCH is local
GATEWAY_SIP_PORT=5080
GATEWAY_SIP_ADVERTISE_HOST=127.0.0.1
GATEWAY_SIP_ALLOWED_PEERS=127.0.0.1

GATEWAY_MEDIA_PORT_LOW=16384
GATEWAY_MEDIA_PORT_HIGH=16407
GATEWAY_REQUEST_TIMEOUT=10
GATEWAY_ATTACH_TIMEOUT=10
```

The gateway **refuses to start** bound to a non-loopback address with
`GATEWAY_SIP_ALLOWED_PEERS` empty. Here it is loopback, so the guard is
satisfied twice over.

**Bank (dev machine, `backend/.env` — never committed):**

```bash
TELEPHONY_ENABLED=true
TELEPHONY_WEBHOOK_SECRET=<the same value>
REALTIME_MAX_ACTIVE_SESSIONS=3      # not 5 until live concurrency is proven
TELEPHONY_MEDIA_TRANSPORT=websocket
OPENAI_API_KEY=<existing>
DATABASE_URL=<existing>
```

Enabling telephony **requires a backend restart** — routes bind at application
build time.

**FreeSWITCH `vars.xml`:**

```xml
<X-PRE-PROCESS cmd="set" data="channel2_sip_port=5060"/>
<X-PRE-PROCESS cmd="set" data="channel2_gateway_uri=127.0.0.1:5080"/>
<X-PRE-PROCESS cmd="set" data="channel2_inbound_acl=didww"/>
<X-PRE-PROCESS cmd="set" data="local_ip_v4=<container IP>"/>
<X-PRE-PROCESS cmd="set" data="external_sip_ip=<public IP>"/>
<X-PRE-PROCESS cmd="set" data="external_rtp_ip=<public IP>"/>
```

Plus a `didww` ACL in `acl.conf.xml` listing DIDWW's ranges.

## 12. DIDWW destination format (for later — do not set yet)

```text
DID number ......... 6531252836
Destination type ... SIP URI / voice-in trunk
Destination ........ sip:6531252836@<public-ip-or-fqdn>:5060
Transport .......... UDP        (or TLS to :5061 if supported)
Codec .............. PCMU (G.711 µ-law), 8 kHz
Authentication ..... IP allow-list of DIDWW ranges  (or SIP digest)
Expected RTP ....... 16500-16530/udp inbound
```

Field names come from the DIDWW console. **Record the current destination value
before changing it** — that string is the rollback.

## 13. PCMU codec configuration

Already in the committed profile and asserted by test:

```xml
<param name="inbound-codec-prefs" value="PCMU,PCMA"/>
<param name="outbound-codec-prefs" value="PCMU"/>
<param name="disable-transcoding" value="true"/>
```
plus `absolute_codec_string=PCMU` in the dialplan.

One codec, no transcoding in FreeSWITCH, so the bank's single µ-law ↔ PCM16-24k
conversion stays the only one in the path.

⚠️ **Confirm the trunk negotiates PCMU before the first call.** If it offers
only G.729 or Opus, that module must be present and a transcoding step enters a
path that currently has none — which changes CPU sizing and adds latency.

## 14. Rollback

In order, fastest first. Each step is independently sufficient to stop calls.

1. **Restore the DIDWW destination** to the recorded previous value. Calls stop
   arriving immediately; nothing else needs touching.
2. **`TELEPHONY_ENABLED=false`** on the bank, then **restart it**. The webhook
   route is no longer registered, so no call can be created.
3. **`docker stop channel2-freeswitch`** on the VM. The SIP surface disappears.
4. **Close the firewall rules** for 5060 and the RTP range.
5. **Stop the gateway** (`Ctrl-C`) and confirm no media ports are bound.
6. **Verify capacity is zero** — `GET /api/call/active` on the bank.
7. **Verify Channel 1 still works** — the browser page is unaffected by every
   step above, and always has been.
8. **Destroy the VM** if abandoning the approach. Nothing in this repository
   depends on it.

No rollback step touches the database schema or the persistent PIN lockout. No
step involves weakening authentication or TLS to make something work.

## 15. Estimated monthly cost

| Item | Estimate |
|---|---|
| VM (1 vCPU / 1 GB, Hetzner CX22 / DO / Vultr) | **$5–7/month** |
| Static IPv4 | usually included; ~$4/month on AWS/GCP if separate |
| DNS + TLS (Let's Encrypt) | **$0** |
| Bandwidth | negligible — 64 kbit/s per call leg |
| **Fixed monthly total** | **≈ $5–10** |

Usage-based, not monthly:

| | |
|---|---|
| DIDWW inbound minutes | per account rate |
| **OpenAI Realtime** | **per minute, per concurrent session — the dominant cost** |

A one-minute test call is a few cents of DIDWW plus a small OpenAI charge.
Sustained multi-call testing is where this accumulates; three concurrent calls
cost three times one.

Hetzner or Vultr over AWS/GCP for this: hyperscalers charge separately for the
static IP and egress, for a workload that needs neither elasticity nor managed
services.

## 16. First-live-call checklist

**Preconditions — all must hold before dialling.**

1. DIDWW's signalling and media IP ranges are known and in the `didww` ACL.
2. Trunk codec confirmed as PCMU.
3. Whether the trunk supports TLS is known, and the answer is recorded honestly.
4. OpenAI organisation's Realtime concurrency is confirmed ≥ 1.
5. `REALTIME_MAX_ACTIVE_SESSIONS=3`, and **only one call will be placed**.

**Bring-up, in order — verify each before the next.**

6. PostgreSQL up; bank up with `TELEPHONY_ENABLED=true` and **restarted**.
7. WireGuard up; the gateway host can reach the bank's `/health`.
8. `python -m gateway check` → **READY** (confirms the route is registered).
9. `python -m gateway selftest --calls 1` → **completed**, frames heard. Free,
   no telephone, no provider.
10. FreeSWITCH container up; `sofia status` shows the `channel2` profile
    **RUNNING** on the public IP.
11. Firewall verified from **outside**: `5060` and the RTP range reachable only
    from DIDWW ranges; `5080`, `16384–16407`, `8001`, `5435` **not reachable at
    all**.

**Synthetic dry run — before touching DIDWW.**

12. Send one signed synthetic `incoming` event to the deployed bank. Expect:
    accepted → one PHONE row → **no customer authenticated** → the 15 s
    media-attach watchdog reclaims it → capacity returns to 0 → no orphan task.

**[APPROVAL POINT]**

13. **Record the current DIDWW destination string**, then point the DID at
    `sip:6531252836@<public-ip>:5060`. Change nothing else — no other trunk, no
    billing, no new DID.

**The call — one, from one known handset.**

14. Dial the DID. Expect in order:
    - FreeSWITCH logs an INVITE from a DIDWW address
    - the gateway logs a call accepted and a media socket attached
    - **the caller hears the greeting**, once
    - say *"I'd like my balance"* → *"DEMO001"* → PIN `4821`
    - the balance returned matches DEMO001's synthetic record
    - the AI's voice is audible on the handset
    - say *"goodbye"* and hang up

**After the call.**

15. Active calls **0**; capacity fully released; Realtime session closed; media
    session closed; FreeSWITCH call terminated; no orphan tasks.
16. One `COMPLETED` row, `customer_id=DEMO001`, `auth_status=VERIFIED`,
    `ended_at` and `duration_seconds` set.
17. **Log review on both hosts:** no PIN, no media token, no OpenAI key, no
    webhook secret, no DIDWW credential, no balance.
18. Caller ID did **not** authenticate — the PIN did.
19. Run the full regression: `python -m pytest -v -ra` → 0 failed, 0 errors.

**Only if every one of those is clean** should a second call be attempted, then
two concurrent, then three. Do not go to 4 or 5 without verifying provider and
OpenAI concurrency first.

---

## Open questions I cannot answer from here

1. DIDWW's published signalling and media IP ranges.
2. Whether this trunk supports SIP-TLS and SRTP.
3. Whether the trunk negotiates PCMU.
4. The account's concurrent channel limit.
5. The OpenAI organisation's Realtime concurrency limit.

All five need the provider consoles. Items 1–3 gate the first call; 4–5 gate
concurrency beyond one.
