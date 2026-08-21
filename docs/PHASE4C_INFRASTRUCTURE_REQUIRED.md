# Phase 4C — infrastructure required before a live DIDWW call

**Status: BLOCKED on infrastructure.** Not on application code, and not on
anything in this repository. The gateway, the SIP leg, the media path and the
bank are built and tested; what does not exist is somewhere for DIDWW to send a
call.

**Nothing external was changed.** No DIDWW setting was touched, no firewall
rule was opened, no telephone call was placed, no paid model session was
opened.

---

## 1. The blocker, precisely

DIDWW delivers an inbound call by sending a SIP INVITE to an address you give
it, then RTP to whatever that address answers with. Both are inbound UDP from
the public internet.

This machine has no address the public internet can reach:

```
192.168.192.1   VMware VMnet1        private
192.168.101.1   VMware VMnet8        private
10.115.129.87   Wi-Fi                private  (the real LAN address)
172.23.96.1     Hyper-V Default      private
172.27.48.1     WSL                  private
```

Every one is RFC1918. The Wi-Fi address is in `10.0.0.0/8`, which usually means
a corporate or ISP NAT sits in front — often carrier-grade NAT, where inbound
connections are impossible regardless of local configuration.

There is no VPS, no cloud account tooling (`aws`, `az`, `gcloud` are all
absent), and no deployment manifest in the repository.

### Tunnels do not solve this

`cloudflared` is installed, and it cannot help. Cloudflare Tunnel carries
HTTP/HTTPS, and TCP through `cloudflared access`. **SIP signalling and RTP media
are UDP**, and arbitrary inbound UDP is not something the free tunnel products
carry. The same is true of ngrok's standard tiers.

A tunnel could expose the *webhook* (HTTPS). It cannot expose the *telephone
call*.

---

## 2. What is actually required

One host with a public address, running three processes.

| | Requirement |
|---|---|
| Host | Any small VPS. 1 vCPU / 1 GB is ample for 3 concurrent G.711 calls. |
| Address | **Static public IPv4**, or a stable FQDN with a static A record |
| OS | Linux (the FreeSWITCH image is a Linux container) |
| Docker | for the FreeSWITCH container |
| Python 3.12 | for the gateway |

### Ports to open — and only these

| Port | Protocol | Source | Purpose |
|---|---|---|---|
| `5060` (or `5061`) | UDP/TCP (TLS) | **DIDWW signalling ranges only** | SIP |
| `16500–16530` | UDP | **DIDWW media ranges only** | RTP from the carrier |
| `5080` | UDP | **FreeSWITCH host only** | gateway SIP leg |
| `16384–16407` | UDP | **FreeSWITCH host only** | gateway media |

Never exposed: PostgreSQL (`5435`), the bank (`8001`), the dashboard, or any
admin surface. The bank does not need inbound access from DIDWW at all — only
the gateway talks to it.

### Where the bank runs

Either on the same host, or reachable from the gateway over a private link
(WireGuard, Tailscale, a cloud private network). It must **not** be published.
If the bank stays on this development machine, the gateway needs a private
tunnel back to it — which is a reasonable arrangement and keeps the bank
unexposed.

---

## 3. Configuration, once the host exists

No code change is needed. These are environment variables, and they already
exist:

```bash
# gateway host
GATEWAY_SIP_HOST=0.0.0.0                  # bind
GATEWAY_SIP_ADVERTISE_HOST=<public IP>    # what SDP advertises
GATEWAY_SIP_ALLOWED_PEERS=<FreeSWITCH host IP>
GATEWAY_MEDIA_PORT_LOW=16384
GATEWAY_MEDIA_PORT_HIGH=16407
GATEWAY_BACKEND_URL=https://<bank>        # private
GATEWAY_WEBHOOK_SECRET=<shared with the bank>
GATEWAY_VERIFY_TLS=true

# bank
TELEPHONY_ENABLED=true
TELEPHONY_WEBHOOK_SECRET=<same value>
REALTIME_MAX_ACTIVE_SESSIONS=3            # not 5, until live concurrency is proven
```

FreeSWITCH `vars.xml`:

```
channel2_sip_port=5060
channel2_gateway_uri=<gateway host>:5080
channel2_inbound_acl=didww          # a list of DIDWW's ranges
local_ip_v4=<the host's own address>
external_sip_ip=<public IP>
external_rtp_ip=<public IP>
```

**The gateway refuses to start** bound to a non-loopback address with
`GATEWAY_SIP_ALLOWED_PEERS` empty. That is deliberate: a SIP port with no
source restriction is found by scanners within hours, and every call they place
is one the gateway would offer to a bank.

---

## 4. What still needs answering from DIDWW

I have no access to the DIDWW account or its current documentation, so these
cannot be confirmed from here. Each is a Phase 4C prerequisite:

1. **Published signalling and media IP ranges**, for the ACL. Without them the
   only options are an unrestricted port (unacceptable) or SIP digest
   authentication.
2. **Whether this trunk supports SIP-TLS and SRTP**, and on which port. If it
   does not, the signalling leg is plaintext and the only protection is the IP
   ACL — which is acceptable for a synthetic-data demonstration and should be
   stated plainly rather than described as secured.
3. **Whether the trunk negotiates PCMU.** The configuration offers PCMU with
   transcoding disabled. If the trunk offers only G.729 or Opus, FreeSWITCH
   needs that codec module present, and a transcoding step enters a path that
   currently has none.
4. **Concurrent channel limit on the account**, which is separate from both the
   application target (5) and the OpenAI Realtime limit.

---

## 5. Cost, before anything is committed to

| | |
|---|---|
| VPS | roughly $5–10/month for this size |
| Static IP / DNS | usually included; TLS via Let's Encrypt is free |
| DIDWW inbound minutes | per the account's rate |
| OpenAI Realtime | **per minute, per concurrent session** — the largest variable |

A single one-minute test call is a few cents of DIDWW and a small OpenAI
charge. Sustained multi-call testing is where cost accumulates.

---

## 6. Summary

| | |
|---|---|
| Application code | **ready** — nothing outstanding |
| Local SIP/RTP validation | **passed** at 1, 2 and 3 concurrent calls |
| Public host | **missing — the blocker** |
| DIDWW answers (ranges, TLS, codec, channels) | **outstanding** |
| DIDWW configuration | **not touched** |

The next action is a decision about hosting, not a change to this repository.
