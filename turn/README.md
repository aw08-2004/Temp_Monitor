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

## Run it

```sh
# .env beside this file (or export the vars):
#   REMOTE_TURN_SECRET=<same as the hub .env>
#   TURN_EXTERNAL_IP=<this host's public IP>
docker compose up -d
docker compose logs -f turn        # watch it accept allocations
```

Then in the console: **Settings → Remote Control → TURN servers** =
`turn:<this-host-public-hostname-or-ip>:3478`. (Optionally also set **STUN servers**, e.g.
`stun:stun.l.google.com:19302`, to help direct paths form when they can.)

## Host-OS notes

- **Linux host (recommended):** keep `network_mode: host` in `docker-compose.yml`. TURN and its
  relay range are reachable on the host's real IP with no port-publishing gymnastics.
- **Windows host:** Docker Desktop has no Linux host networking. **Remove the `network_mode:
  host` line** so the published `ports:` take effect (the whole relay range is published, which
  Docker Desktop supports but is heavyweight). Alternatives that are often nicer on Windows:
  run coturn under WSL2, or put the TURN server on a small Linux VM next to the hub — "the hub
  is the TURN server" only means *the hub app mints the creds*, not that the daemon must share
  the exact Windows box.

## TLS (optional)

`turn:` (plain) is enough for a working relay — the WebRTC media inside it is already
DTLS-SRTP-encrypted end to end. If a restrictive network only allows 443, add `turns:` on 5349
with a certificate (mount it and drop `--no-tls --no-dtls`); see the coturn docs.

## Verifying credentials

The hub's minted credentials are checked against coturn's REST auth in
`tests/test_turn_interop.py` (run with Docker available). If a browser shows
`iceConnectionState: failed` only on cross-NAT machines, TURN is the thing to check: confirm
`REMOTE_TURN_SECRET` matches on both sides, `TURN_EXTERNAL_IP` is the real public IP, and the
relay UDP range is open.
