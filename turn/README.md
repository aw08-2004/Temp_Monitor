# FleetHub TURN server (remote view/control, roadmap #2)

Remote view/control uses WebRTC. Agents sit behind arbitrary customer NATs and the browser is
behind yours, so a direct peer-to-peer path usually can't form — the media has to relay through
a **TURN server**. FleetHub's design is that **the hub is the TURN server**: it runs on (or
beside) the hub host, and the hub app mints the credentials, so there's nothing external to
depend on and one secret to manage.

Either way it's [coturn](https://github.com/coturn/coturn), which implements the standard
**TURN REST credential scheme** the hub already mints against
(`remote.mint_turn_credentials`): `username = "<expiry-unix>:<session-id>"`,
`password = base64(HMAC-SHA1(secret, username))`. The hub and coturn share **one secret** and
never exchange a per-user database.

**Two ways to run it, and the choice is not cosmetic:**

| Host | How | Cross-NAT |
|---|---|---|
| **Linux** | `docker-compose.yml` here, host networking | ✅ works |
| **Windows** | a dedicated **WSL2 distro**, provisioned by `install.ps1` | ✅ works |
| **Windows** | `docker-compose.windows.yml` (container) | ❌ **LAN only** — see [Host-OS notes](#host-os-notes) |

A coturn *container* on Windows relays from the Docker bridge. That is fine on a LAN and fine
for proving the credential path, but it cannot carry cross-NAT media, for reasons worked out the
hard way and written up below. The Windows installer therefore builds a WSL2 distro instead.

## What you need

- **`REMOTE_TURN_SECRET`** — a strong random string. Put the **same value** in the hub's `.env`
  (the hub reads it to mint credentials) and here (coturn validates against it). Without it set
  on the hub, TURN is simply omitted from the ICE list and only STUN/direct paths are tried.
- **`TURN_EXTERNAL_IP`** — the **public** IP address clients reach this host on. Required behind
  NAT/cloud, or coturn will hand out unreachable relay candidates.
- Firewall/NAT openings to this host:
  - **3478/udp and 3478/tcp** — the TURN control port.
  - **49160–49200/udp** (default range, tunable) — the relay port range. Media flows here.

## Easiest: let the installer do it (Windows)

Two ways in, both landing on the same code path:

- **On the hub** — `install.ps1` → **Install Hub** asks "Configure this hub as the TURN/STUN
  server?". Answer yes and it also seeds the STUN/TURN URLs straight into
  **Settings → Remote Control**.
- **On its own** — `install.ps1` → **Install TURN** (`-Component Turn`) installs the relay and
  nothing else, for when it belongs on a different machine than the hub: a box with a better
  public address, or a Windows host you already have ports forwarded to while the hub runs on
  Linux. It prints the `turn:` / `stun:` URLs and the secret for you to paste into
  **Settings → Remote Control** yourself. If a hub happens to be installed on the same machine
  it defaults to that hub's existing `REMOTE_TURN_SECRET`, so the two can't silently diverge.

Either way it generates or reuses `REMOTE_TURN_SECRET`, asks for the public IP, and builds a
complete relay:

- Creates a **dedicated WSL2 distro** (`FleetHubTurn`, Ubuntu 24.04) via
  `wsl --install --name ... --no-launch`. Dedicated so it never touches an Ubuntu you use for
  anything else, and so uninstall is a clean `wsl --unregister`.
- Installs **coturn natively** in it, under **systemd**, with `Restart=always` so a crash
  self-heals. Config is written to `/etc/turnserver.conf` with the hub's secret already in it.
- Sets **`networkingMode=mirrored`** in `%USERPROFILE%\.wslconfig`, merging into any existing
  file rather than overwriting it (a timestamped backup is taken). This is what makes the relay
  work — see below.
- Opens **both firewalls**: normal Windows Firewall *and* the Hyper-V firewall.
- Registers a **boot scheduled task**, because WSL distros do not start at boot.
- **Verifies itself**: mints a credential exactly as the hub does and performs a real STUN
  Binding and TURN Allocate, including a wrong-password negative control.

The generated secret is printed once at the end. Prerequisites: **Windows 11 22H2 (build 22621)
or newer**, the Store WSL package (`wsl --update`), and ~3 GB of free disk (the installer checks
and asks). If any precondition fails it says why and stops — from *Install Hub* it skips TURN and
finishes the hub install normally, so you are never left with a half-configured hub; from
*Install TURN* it fails outright, since the relay was the entire job.

Removing it: **Uninstall → TURN relay** (`-Component Turn -Uninstall`) removes the distro, boot
task and firewall rules while leaving any hub alone. Uninstalling the **Hub** removes the relay
too, if that machine has one.

> **The one disruptive step:** applying mirrored networking needs `wsl --shutdown`, which
> restarts the WSL VM and therefore **every Docker Desktop container on the machine**. The
> installer detects other distros and Docker, names what will stop, and asks for a separate
> confirmation (default *no*) before doing it. It skips that entirely if mirrored mode is
> already on.

Day-to-day:

```powershell
wsl -d FleetHubTurn -u root -- systemctl status coturn     # health
wsl -d FleetHubTurn -u root -- journalctl -u coturn -f      # live log during a session
wsl -d FleetHubTurn -u root -- nano /etc/turnserver.conf    # config; restart coturn after
```

The manual steps below are for a Linux host, or a TURN server that lives somewhere other than
the hub.

## Run it by hand

```sh
# .env beside this file (or export the vars):
#   REMOTE_TURN_SECRET=<same as the hub .env>
#   TURN_EXTERNAL_IP=<this host's public IP>
docker compose up -d                          # Linux host (host networking) -- the good path
docker compose logs -f turn                   # watch it accept allocations

# Windows host, LAN / credential-proving ONLY -- not a cross-NAT relay, see Host-OS notes:
docker compose -f docker-compose.windows.yml up -d
```

Then in the console: **Settings → Remote Control → TURN servers** =
`turn:<this-host-public-hostname-or-ip>:3478`. (Optionally also set **STUN servers** — the same
coturn answers STUN, so `stun:<this-host>:3478` works with no extra dependency.)

## Rotating the secret from the UI

**Settings → Remote Control** has a TURN status card where an admin can set or rotate
`REMOTE_TURN_SECRET` without shell access on the hub (it writes the hub's `.env` and applies
immediately). coturn validates against **its own** copy, so a rotation is only half done until
you sync it — otherwise coturn rejects **every** allocation with 401. The UI shows the new value
once for exactly this reason.

Where the other copy lives depends on how coturn runs:

```powershell
# WSL2 distro (what the Windows installer builds):
wsl -d FleetHubTurn -u root -- sed -i "s/^static-auth-secret=.*/static-auth-secret=<new>/" /etc/turnserver.conf
wsl -d FleetHubTurn -u root -- systemctl restart coturn
```

```sh
# Docker (Linux host): update turn/.env, then
docker compose up -d
```

This desync is not hypothetical — it happened in the field on 2026-07-27 and cost a debugging
session. The Windows installer now checks the two values against each other as part of its
post-install verification, so a re-run will tell you immediately if they have drifted.

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

  So on Windows, `docker-compose.windows.yml` is good for **LAN and for proving the credential
  path**, and is not a cross-NAT production relay. Put coturn somewhere it can bind a real
  interface instead:

  - **coturn natively inside a WSL2 distro — what `install.ps1` now builds for you**, with
    `networkingMode=mirrored` so WSL shares the host's network namespace and relay source ports
    survive. See [the installer section](#easiest-let-the-hub-installer-do-it-windows-hub).

    Note the *natively*. A coturn **container under Docker Desktop stays behind Docker's own
    bridge no matter what WSL's networking mode is** — mirrored mode alone does not fix trap 2,
    because the container sits behind a second NAT that `.wslconfig` has no say over. This is
    worth spelling out because it is an easy and expensive thing to assume.

  - **A Linux host or small VM next to the hub — still the lowest-risk option**, and the right
    one if this Windows box does other jobs. "The hub is the TURN server" only means *the hub
    app mints the credentials*; the daemon does not have to share the Windows box. Nothing about
    the hub changes — point **Settings → Remote Control → TURN servers** at the new host and keep
    `REMOTE_TURN_SECRET` in sync. Unlike the WSL route it needs no `wsl --shutdown`, so it never
    disturbs anything else running on the hub.

## TLS (optional)

`turn:` (plain) is enough for a working relay — the WebRTC media inside it is already
DTLS-SRTP-encrypted end to end. If a restrictive network only allows 443, add `turns:` on 5349
with a certificate (mount it and drop `--no-tls --no-dtls`); see the coturn docs.

## Verifying credentials

The hub's minted credentials are checked against coturn's REST auth in
`tests/test_turn_interop.py` (run with Docker available): a hub-minted credential authenticates
and allocates, a wrong one is refused.

## Troubleshooting

Work top-down — each row assumes the ones above it pass. The agent-side log is
`C:\ProgramData\FleetHub\Agent\remote-helper.log` on the target machine. For the coturn side:

```powershell
wsl -d FleetHubTurn -u root -- journalctl -u coturn -f     # WSL2 (installer-built)
docker compose logs -f turn                                 # Docker (Linux host)
```

| Symptom | Almost certainly |
|---|---|
| Agent logs `ice_servers=0` | `REMOTE_TURN_SECRET` unset on the hub, or no TURN URL in **Settings → Remote Control**. The hub omits TURN rather than failing, so sessions still start and only cross-NAT media dies. |
| Nothing listening on 3478 on the host; coturn log has **no requests at all** since boot | On Windows/Docker: started from the Linux `docker-compose.yml`. See trap 1 above. |
| coturn logs `check_stun_auth: Cannot find credentials of user <...>` or clients get **401** | The secret differs between the hub's `.env` and coturn's copy. A rotation from the UI updates only the hub — see [Rotating the secret](#rotating-the-secret-from-the-ui). |
| Allocations **succeed** (`Global turn allocation count incremented`) but the agent still reports `peer connection state: failed` | The relay's media path, not auth. On Windows/Docker see trap 2 above. Otherwise check that the whole **relay UDP range** (not just 3478) is open and forwarded, and that the external IP is the real public IP. |
| Works on the LAN, fails only cross-NAT | TURN is not actually being used or not reachable — the LAN case succeeds on host candidates alone and proves nothing about the relay. Always validate with a machine on a genuinely different network. |
| **Worked on install day, dead after a reboot** | WSL distros do **not** auto-start. Check the `FleetHub - TURN (WSL)` scheduled task exists and is running. Note it must run as the **installing user** (S4U), not SYSTEM — distros are registered per-user, so a SYSTEM task cannot see it and fails every time. |
| Remote machines can't allocate, but the LAN can | The **Hyper-V firewall**, which is on by default with WSL 2.0.9+ and blocks inbound to WSL even in mirrored mode. This is the single most likely cause of an otherwise-correct WSL setup failing. Check `Get-NetFirewallHyperVRule`. |
| TURN died out of nowhere, nothing was changed | Someone ran `wsl --shutdown` — Docker Desktop's own restart flow does this — and took the distro with it. The boot task's 5-minute repeating trigger recovers it; that gap is why the trigger exists. |
| `wsl -d FleetHubTurn -- hostname -I` shows a `172.x` address | Mirrored networking is configured but **not active**. Either `wsl --shutdown` was never run, or `.wslconfig` was written to a different user profile than the one WSL reads. |
| A **wrong** password is accepted, relay ports land outside `min-port`–`max-port`, and `/var/log/coturn/turn.log` **does not exist** | coturn could not read `/etc/turnserver.conf` and silently fell back to built-in defaults — no authentication at all, and the default 49152–65535 relay range. The unit runs as `User=turnserver`, so a `root:root 0640` config locks it out, and coturn does **not** treat that as fatal. Fix: `wsl -d FleetHubTurn -u root -- chown root:turnserver /etc/turnserver.conf` then `systemctl restart coturn`. The installer sets this group correctly and now checks for it; the missing log file is the fastest tell. |
| coturn stuck in `activating`, journal repeats `bind: Address already in use` | Something else already owns 3478 on the host. Under `networkingMode=mirrored` the distro **shares the host's network namespace**, so a Docker container publishing `3478` collides with it — including a leftover `turn-turn-1` from the old `docker-compose.windows.yml` path. Stop/remove that container; only one of the two can serve. |
| Session connects, media flows, but the operator sees a **black screen** | Not TURN at all — the agent injected its capture helper into a session with no desktop. See the remote-control notes in the root [README](../README.md#remote-view--control). |

A useful property of the coturn log: `Global turn allocation count incremented` appearing
**twice** within a second or two means *both* peers reached the relay and authenticated. If you
see that and ICE still fails, you have conclusively ruled out reachability of 3478, the shared
secret, and the credential scheme — the problem is downstream in the media path.
