# FleetHub TURN server (remote view/control, roadmap #2)

Remote view/control uses WebRTC. Agents sit behind arbitrary customer NATs and the browser is
behind yours, so a direct peer-to-peer path usually can't form — the media has to relay through
a **TURN server**. FleetHub's design is that **the hub is the TURN server**: it runs on (or
beside) the hub host, and the hub app mints the credentials, so there's nothing external to
depend on and one secret to manage.

This directory runs [coturn](https://github.com/coturn/coturn), which implements the standard
**TURN REST credential scheme** the hub already mints against
(`remote.mint_turn_credentials`): `username = "<expiry-unix>:<session-id>"`,
`password = base64(HMAC-SHA1(secret, username))`. The hub and coturn share **one secret** and
never exchange a per-user database.

## What you need

- **`REMOTE_TURN_SECRET`** — a strong random string. Put the **same value** in the hub's `.env`
  (the hub reads it to mint credentials) and here (coturn validates against it). Without it set
  on the hub, TURN is simply omitted from the ICE list and only STUN/direct paths are tried.
- **`TURN_EXTERNAL_IP`** — the **public** IP address clients reach this host on. Required behind
  NAT/cloud, or coturn will hand out unreachable relay candidates.
- Firewall/NAT openings to this host:
  - **3478/udp and 3478/tcp** — the TURN control port.
  - **49160–49200/udp** (default range, tunable) — the relay port range. Media flows here.

## Easiest: let the hub installer do it

`install.ps1` → **Install Hub** asks "Configure this hub as the TURN/STUN server?". Answer
yes and it generates `REMOTE_TURN_SECRET` (into both the hub `.env` and `turn/.env`), asks for
the public IP, optionally runs `docker compose up` here, and seeds the STUN/TURN URLs into
**Settings → Remote Control**. The generated secret is printed once at the end. The manual
steps below are for a hand setup or a TURN host separate from the hub.

> **On a Windows hub the installer gets you a working credential path, but not necessarily a
> working cross-NAT relay** — read [Host-OS notes](#host-os-notes) before relying on it for
> machines outside your own network.

## Run it by hand

```sh
# .env beside this file (or export the vars):
#   REMOTE_TURN_SECRET=<same as the hub .env>
#   TURN_EXTERNAL_IP=<this host's public IP>
docker compose up -d                          # Linux host (host networking; see below)
docker compose -f docker-compose.windows.yml up -d   # Windows host (published ports)
docker compose logs -f turn                   # watch it accept allocations
```

Then in the console: **Settings → Remote Control → TURN servers** =
`turn:<this-host-public-hostname-or-ip>:3478`. (Optionally also set **STUN servers** — the same
coturn answers STUN, so `stun:<this-host>:3478` works with no extra dependency.)

## Rotating the secret from the UI

**Settings → Remote Control** has a TURN status card where an admin can set or rotate
`REMOTE_TURN_SECRET` without shell access on the hub (it writes the hub's `.env` and applies
immediately). coturn validates against **its own** copy, so after a rotate you must set the
same value as `--static-auth-secret` here — update `turn/.env` `REMOTE_TURN_SECRET` and
`docker compose ... up -d` again — or coturn will reject every allocation. The UI shows the new
value once for exactly this reason.

## Host-OS notes

**A TURN relay wants a real network interface.** Everything below follows from that: the relay
hands a peer a candidate address, and the peer must then receive packets *from exactly that
address and port*. Any NAT between coturn and the wire that rewrites the source port breaks
ICE even though allocations succeed. Read the Windows section before deploying there.

- **Linux host (recommended):** keep `network_mode: host` in `docker-compose.yml`. TURN and its
  relay range are reachable on the host's real IP with no port-publishing gymnastics, and the
  relay's source port is preserved end to end.

- **Windows host:** there are two distinct traps here, and field testing hit both.

  1. **`network_mode: host` silently does nothing useful.** Docker Desktop has no Linux host
     networking, so the container binds inside the Linux VM and *nothing listens on the Windows
     host at all*. The tell is stark: `netstat -ano | findstr 3478` is empty and coturn's log
     shows **zero requests, ever**, however long it has been up. Use
     **`docker-compose.windows.yml`** (`docker compose -f docker-compose.windows.yml up -d`),
     which drops `network_mode: host` and publishes the ports instead. This is the file the
     installer uses.

  2. **Published ports fix reachability but not the relay path.** With the Windows compose file
     3478 answers, credentials validate, and both peers allocate successfully — and cross-NAT
     sessions can *still* fail. The container's relay sockets live on the Docker bridge, which
     coturn announces at startup:

     ```
     WARNING: NO EXPLICIT RELAY ADDRESS(ES) ARE CONFIGURED
     Relay address to use: 172.22.0.2          <-- the bridge, not a real interface
     ```

     `--external-ip` makes coturn *advertise* the public address, and inbound DNAT delivers
     peer→relay fine, so allocations look healthy. But relay→peer packets egress through
     Docker's NAT, which is free to rewrite the source port; the peer then never receives from
     the advertised candidate and every connectivity check fails. The observed signature is
     coturn logging `Global turn allocation count incremented` **twice** (both peers) while the
     agent logs `peer connection state: failed` about 16 s later.

  So on Windows, treat `docker-compose.windows.yml` as good for **LAN and for proving the
  credential path**, and not as a cross-NAT production relay. For production, put coturn
  somewhere it can bind a real interface:

  - **A Linux host or small VM next to the hub — the recommended fix.** "The hub is the TURN
    server" only means *the hub app mints the credentials*; the daemon does not have to share
    the Windows box. Nothing about the hub changes — point **Settings → Remote Control → TURN
    servers** at the new host and keep `REMOTE_TURN_SECRET` in sync.
  - **coturn running natively inside a WSL2 distro**, with `networkingMode=mirrored` in
    `%USERPROFILE%\.wslconfig` so WSL shares the host's network namespace. Note the *natively*:
    a coturn **container under Docker Desktop stays behind Docker's own bridge no matter what
    WSL's networking mode is**, so mirrored mode alone does not fix trap 2. Also note that
    changing `.wslconfig` requires `wsl --shutdown`, which restarts Docker Desktop's VM and
    therefore **every container on the box**.

## TLS (optional)

`turn:` (plain) is enough for a working relay — the WebRTC media inside it is already
DTLS-SRTP-encrypted end to end. If a restrictive network only allows 443, add `turns:` on 5349
with a certificate (mount it and drop `--no-tls --no-dtls`); see the coturn docs.

## Verifying credentials

The hub's minted credentials are checked against coturn's REST auth in
`tests/test_turn_interop.py` (run with Docker available): a hub-minted credential authenticates
and allocates, a wrong one is refused.

## Troubleshooting

Work top-down — each row assumes the ones above it pass. The agent-side log referenced here is
`C:\ProgramData\FleetHub\Agent\remote-helper.log` on the target machine; the coturn side is
`docker compose -f docker-compose.windows.yml logs -f turn`.

| Symptom | Almost certainly |
|---|---|
| Agent logs `ice_servers=0` | `REMOTE_TURN_SECRET` unset on the hub, or no TURN URL in **Settings → Remote Control**. The hub omits TURN rather than failing, so sessions still start and only cross-NAT media dies. |
| Nothing listening on 3478 on the host; coturn log has **no requests at all** since boot | On Windows: started from the Linux `docker-compose.yml`. See trap 1 above. |
| coturn logs `check_stun_auth: Cannot find credentials of user <...>` or clients get **401** | The secret differs between the hub's `.env` and coturn's `--static-auth-secret`. A rotation from the UI updates only the hub — coturn must be updated and restarted to match. |
| Allocations **succeed** (`Global turn allocation count incremented`) but the agent still reports `peer connection state: failed` | The relay's media path, not auth. On Windows/Docker see trap 2 above. Otherwise check that the whole **relay UDP range** (not just 3478) is open and forwarded, and that `TURN_EXTERNAL_IP` is the real public IP. |
| Works on the LAN, fails only cross-NAT | TURN is not actually being used or not reachable — the LAN case succeeds on host candidates alone and proves nothing about the relay. Always validate with a machine on a genuinely different network. |
| Session connects, media flows, but the operator sees a **black screen** | Not TURN at all — the agent injected its capture helper into a session with no desktop. See the remote-control notes in the root [README](../README.md#remote-view--control). |

A useful property of the coturn log: `Global turn allocation count incremented` appearing
**twice** within a second or two means *both* peers reached the relay and authenticated. If you
see that and ICE still fails, you have conclusively ruled out reachability of 3478, the shared
secret, and the credential scheme — the problem is downstream in the media path.
