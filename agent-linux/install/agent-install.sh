#!/usr/bin/env bash
#
# Install (or upgrade) the FleetHub Linux agent on this machine.
#
# The counterpart of agent/install/agent-install.ps1, and deliberately much smaller. That
# script has to install a kernel driver for sensor access, register a Windows service with
# recovery actions, and write a registry key; this one copies a binary, drops a unit file and
# writes one secret, because everything else it would do systemd already does.
#
# WHAT IT DOES NOT DO: fetch anything from the internet. The Windows agent self-updates from a
# signed manifest, which is what makes its installer a bootstrap rather than the whole story.
# This agent has no updater yet -- see agent-linux/README.md for why that is deliberate at
# version 0.x -- so an upgrade is this script, run again, with a newer binary beside it.
#
#   sudo ./agent-install.sh --binary ./fleethub-agent --secret 'xxxx' [--hub https://host]
#
set -euo pipefail

INSTALL_DIR=/opt/fleethub/agent
CONF_DIR=/etc/fleethub
UNIT=/etc/systemd/system/fleethub-agent.service
SERVICE=fleethub-agent

BINARY=""
SECRET=""
HUB=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

usage() {
    cat <<'USAGE'
Usage: sudo ./agent-install.sh --binary PATH [--secret SECRET] [--hub URL]

  --binary PATH   The published fleethub-agent executable. Build it with:
                    dotnet publish src/FleetHubAgent/FleetHubAgent.csproj -c Release -o dist
  --secret VALUE  One-time enrollment secret from the hub (Settings -> Fleet). Without it the
                  agent still reports telemetry but cannot receive commands.
  --hub URL       Override the compiled-in hub base URL. For testing against a local hub.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --binary) BINARY="${2:-}"; shift 2 ;;
        --secret) SECRET="${2:-}"; shift 2 ;;
        --hub)    HUB="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "unknown argument: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run this with sudo: it writes to /opt, /etc and systemd"
[ -n "$BINARY" ] || { usage; die "--binary is required"; }
[ -f "$BINARY" ] || die "no such file: $BINARY"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this installer targets systemd"

# The unit lives beside this script in the repo. Resolved relative to the script rather than to
# the caller's cwd, so `sudo /path/to/agent-install.sh` works from anywhere.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$HERE/../packaging/fleethub-agent.service"
[ -f "$UNIT_SRC" ] || die "unit file not found at $UNIT_SRC"

echo "Installing the FleetHub Linux agent..."

# Stop before overwriting. Replacing a running executable in place succeeds on Linux (the
# kernel keeps the old inode open) and leaves the OLD agent running against the NEW state
# directory until something restarts it -- an upgrade that appears to have worked and has not.
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    note "stopping $SERVICE"
    systemctl stop "$SERVICE"
fi

install -d -m 0755 "$INSTALL_DIR"
install -m 0755 "$BINARY" "$INSTALL_DIR/fleethub-agent"
note "binary  -> $INSTALL_DIR/fleethub-agent"

# 0700: the directory holds the enrollment secret below.
install -d -m 0700 "$CONF_DIR"

if [ -n "$SECRET" ]; then
    # install(1) with the mode set, rather than a redirect followed by chmod. A redirect creates
    # the file with the umask's mode FIRST and narrows it a moment later, so the secret is
    # briefly world-readable on a machine that may have other users on it.
    printf '%s' "$SECRET" | install -m 0600 /dev/stdin "$CONF_DIR/agent.secret"
    note "secret  -> $CONF_DIR/agent.secret (0600)"
elif [ -f "$CONF_DIR/agent.secret" ]; then
    note "secret  -> keeping the existing $CONF_DIR/agent.secret"
else
    note "secret  -> none supplied; the agent will report telemetry but take no commands"
fi

# The hub override goes in the unit's EnvironmentFile rather than the unit itself, so a
# reinstall that ships a new unit file does not silently drop a machine back to the default hub.
if [ -n "$HUB" ]; then
    printf 'FLEETHUB_HUB=%s\n' "$HUB" | install -m 0600 /dev/stdin "$CONF_DIR/agent.env"
    note "hub     -> $HUB (via $CONF_DIR/agent.env)"
fi

install -m 0644 "$UNIT_SRC" "$UNIT"
note "unit    -> $UNIT"

systemctl daemon-reload
systemctl enable --now "$SERVICE"

echo
# `is-active` rather than a bare success message: enable --now returns 0 once systemd has
# ACCEPTED the job, which is not the same as the agent being up. A binary built for the wrong
# architecture fails here, and this is where an installer should say so.
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    echo "The agent is running. Follow it with:"
    echo "  journalctl -u $SERVICE -f"
else
    echo "The agent was installed but is not running. Its log:" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi
