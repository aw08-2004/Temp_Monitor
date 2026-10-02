#!/usr/bin/env bash
#
# FleetHub Linux agent -- web installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/install/install.sh \
#     | sudo bash -s -- --secret 'THE-SECRET'
#
# The Linux counterpart of `irm .../install.ps1 | iex`. It finds the binary the way the agent's
# own SelfUpdater does: the Ed25519-signed agent-linux.manifest.json, verified against the fleet
# key compiled into both agents, then the download checked against the SIGNED sha256. See
# "signed manifest" below for why, and for why install.ps1 does not (yet) do the same.
#
# WHY EVERYTHING IS INSIDE main(). This script is meant to be piped into bash, and a pipe is not
# a file: bash reads it in chunks and executes what it has. If the connection drops halfway, a
# top-level script runs the half that arrived -- which here could mean stopping the running
# agent and never installing the replacement. Wrapping the body in a function means a truncated
# download defines an incomplete function and never reaches the call at the bottom, so a partial
# transfer does nothing at all. That last line is load-bearing; do not "tidy" it away.
#
# WHY IT FETCHES THE UNIT FILE RATHER THAN EMBEDDING IT. agent-linux/packaging/fleethub-agent.service
# is the single source of truth, and a copy pasted in here would drift from it silently -- the
# kind of drift nobody notices until a machine installed last month behaves differently from one
# installed today. install.ps1 fetches agent/install/agent-install.ps1 from raw for the same
# reason. Use --unit to install from a local checkout instead.

set -euo pipefail

REPO="aw08-2004/Temp_Monitor"
BRANCH="main"
ASSET_NAME="fleethub-agent"

INSTALL_DIR=/opt/fleethub/agent
CONF_DIR=/etc/fleethub
UNIT_PATH=/etc/systemd/system/fleethub-agent.service
SERVICE=fleethub-agent

UNIT_URL="https://raw.githubusercontent.com/$REPO/$BRANCH/agent-linux/packaging/fleethub-agent.service"

# The manifest the agent's SelfUpdater reads (AgentConfig.StableManifestUrl) -- the same file, so
# a machine is installed from exactly what it would later update from.
MANIFEST_URL="https://raw.githubusercontent.com/$REPO/$BRANCH/agent-linux/agent-linux.manifest.json"

# The fleet's release-signing public key. **Must equal AgentConfig.UpdatePublicKeyHex** in both
# agents; tests/test_linux_installer.py fails if it drifts. Deliberately NOT overridable by flag
# or environment, for the reason the agents give: --manifest-url may point anywhere, because
# whatever it serves still has to be signed by a key only the offline signing machine holds.
UPDATE_PUBLIC_KEY_HEX="9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c"

BINARY=""
AGENT_URL=""
MANIFEST_URL_OVERRIDE=""
UNIT_SRC=""
SECRET=""
SECRET_FILE=""
HUB=""
UNINSTALL=0
SECRET_FROM_ARGV=0

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
FleetHub Linux agent installer

  curl -fsSL https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/install/install.sh \
    | sudo bash -s -- --secret 'THE-SECRET'

  # Debian minimal, OpenMediaVault and other images without curl:
  wget -qO- https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/install/install.sh \
    | sudo bash -s -- --secret 'THE-SECRET'

  # If your account is not in sudoers (common on appliance distros), become root first:
  su -
  wget -qO- <same url> | bash -s -- --secret 'THE-SECRET'

Options:
  --secret VALUE     The hub's AGENT_ENROLLMENT_SECRET: one shared value for the whole fleet,
                     from the hub's .env (install.ps1 prints it once when the hub is set up).
                     It is not shown anywhere in the console. Without it the agent reports
                     telemetry but takes no commands.
                     NOTE: an argument is visible in `ps` while this runs. Prefer
                     --secret-file, or pass it in the environment -- either
                     FLEETHUB_ENROLLMENT_SECRET or AGENT_ENROLLMENT_SECRET is read, so
                     whichever name you already have exported will work.
  --secret-file PATH Read the secret from a file instead (not visible in the process list).
  --hub URL          Override the compiled-in hub base URL.
  --manifest-url URL Read the signed manifest from here (an internal mirror). It is still
                     verified against the fleet key, so a mirror cannot substitute a build.
  --agent-url URL    Download the binary from here WITHOUT checking it against the signed
                     manifest. Prints a warning. For a build nobody has signed yet.
  --binary PATH      Install a binary already on this machine; skips downloading entirely,
                     and like --agent-url is not checked against the fleet key.
  --unit PATH        Use a local copy of the systemd unit rather than fetching it.
  --uninstall        Stop, disable and remove the agent. Keeps /etc/fleethub so a reinstall
                     does not need the secret again; delete it by hand to remove that too.
  -h, --help         This text.
USAGE
}

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- http
#
# curl OR wget, whichever the machine has.
#
# **Do not collapse this back to curl.** Requiring curl was the first thing this installer got
# wrong in the field: Debian's own minimal images -- and appliance distros built on them, like
# OpenMediaVault -- ship wget and not curl. The failure is `curl: not found` from the FIRST half
# of the pipeline, before a single line of this script has run, so the installer cannot even
# report it. The person then sees a sudo password prompt from the second half of the pipe (both
# sides of a pipeline start regardless) and reasonably concludes the install ran.
#
# Bootstrapping curl with apt-get was rejected: an installer whose first act is to install a
# package on a NAS is doing more than it was asked to, and the tool it wants is already there
# under a different name.
#
# --binary together with --unit downloads nothing, so a machine with neither client is still
# installable; the guard fires only when something is actually about to be fetched.

# The guard lives INSIDE the two helpers, not at their call sites.
#
# It was at the call sites first, and --agent-url slipped past it: that path short-circuits
# resolve_agent_url before its need_http, so a machine with neither client died with a generic
# "download failed" instead of the message this whole section exists to print. Asking every
# caller to remember a precondition is how that happens, and the next download path added would
# have had the same coin flip. Here it cannot be missed, and the file's claim that no download
# runs unchecked is true by construction rather than by review.

http_get_stdout() {
    need_http
    local url="$1"
    if have curl; then curl -fsSL -H 'User-Agent: FleetHub-Installer' "$url" 2>/dev/null
    else wget -qO- --header='User-Agent: FleetHub-Installer' "$url" 2>/dev/null
    fi
}

http_get_file() {
    need_http
    local url="$1" dest="$2"
    if have curl; then curl -fsSL --retry 3 -o "$dest" "$url"
    else wget -q --tries=3 -O "$dest" "$url"
    fi
}

need_http() {
    have curl || have wget || die "neither curl nor wget is installed. Install one first:
    apt-get update && apt-get install -y curl        # Debian / Ubuntu / OpenMediaVault
    dnf install -y curl                              # Fedora / RHEL
  ...or build the agent yourself and install it with --binary, which needs neither."
}

# ---------------------------------------------------------------- preflight

check_platform() {
    # `sudo` is named first because that is what most people will reach for, but an account
    # that is not in sudoers is common on appliance distros (OpenMediaVault manages its users
    # and does not make them admins), so `su -` is spelled out rather than assumed.
    [ "$(id -u)" -eq 0 ] || die "run this as root: it writes to /opt, /etc and systemd.
  Either pipe to \`sudo bash\`, or become root first with \`su -\` and drop the sudo."
    need systemctl
    need install

    local arch
    arch="$(uname -m)"
    # Checked before anything is downloaded or stopped. The published binary is linux-x64, and
    # the failure on the wrong architecture is `cannot execute binary file` from systemd long
    # after the installer has claimed success -- so it is caught here instead.
    [ "$arch" = "x86_64" ] || die "this build is linux-x64 only; this machine is $arch. \
Build for it with: dotnet publish -r linux-arm64 (untested), then use --binary."

    # A self-contained .NET binary links against glibc. On musl (Alpine) it fails with a
    # confusing loader error, so say what is actually wrong.
    if [ -f /etc/os-release ] && grep -qiE '^ID=(alpine)' /etc/os-release; then
        die "musl-based distro detected. This build needs glibc; publish with -r linux-musl-x64 \
and install it with --binary."
    fi
}

# ---------------------------------------------------------------- download

# ---------------------------------------------------------------- signed manifest
#
# **The installer applies the check the agent's SelfUpdater applies**, and that is the whole
# point of this section. Until it existed the installer asked the GitHub releases API for the
# newest linux-agent-v* asset and checked only that the download began with the ELF magic --
# so the fleet key, which every self-update verifies fail-closed, protected every step EXCEPT
# the one that first runs the agent as root. Review on PR #91 caught that when the 0.3.0 note
# made this installer the upgrade path for every enrolled box (0.1.0 and 0.2.0 have no updater).
#
# Mirrored step for step from agent-linux/src/FleetHubAgent/Update/SelfUpdater.cs:
#   1. fetch the manifest and its detached .sig;
#   2. verify the Ed25519 signature over the manifest's EXACT bytes with the pinned key;
#   3. only then parse it for version, sha256 and url;
#   4. download url and refuse it unless its sha256 equals the SIGNED value.
# Fail closed at every step: nothing here falls back to an unverified download.
#
# **OpenSSL 3, and no fallback below it.** Ed25519 verification of a raw message needs
# `pkeyutl -rawin`, which OpenSSL 1.1.1 does not have; the machine has no .NET yet to borrow
# the agent's verifier from. Ubuntu 22.04+, Debian 12+ and RHEL 9 ship 3.x. An older box is told
# so and pointed at --binary, rather than silently installed unverified.
#
# Rejected: keeping the releases-API lookup and verifying afterwards. The manifest already names
# the one URL that is allowed, so asking the API as well would be a second source of truth for
# the same fact, and the pagination and 60-requests-per-hour limits the API brought with it
# disappear with it. Rejected too: a flag or variable to supply the key. That would make the
# trust root something whoever runs the command chooses, which is the thing being removed.
#
# install.ps1 still does what this used to do -- no manifest check on a first Windows install.
# Recorded in ROADMAP.MD rather than changed here, because that is the Windows fleet's front
# door and deserves its own change.

hex_to_file() {
    # Hex text on stdin -> raw bytes in $1. printf '%b' rather than xxd, which a minimal server
    # image does not have.
    local hex
    hex="$(tr -d ' \t\r\n')"
    case "$hex" in *[!0-9a-fA-F]*|'') return 1 ;; esac
    [ $(( ${#hex} % 2 )) -eq 0 ] || return 1
    printf '%b' "$(printf '%s' "$hex" | sed 's/../\\x&/g')" > "$1"
}

need_openssl3() {
    have openssl || die "openssl is required to verify the agent against the fleet's signing key.
  Install it (apt-get install -y openssl / dnf install -y openssl), or build the agent yourself
  and install it with --binary."
    local major
    major="$(openssl version 2>/dev/null | sed -n 's/^OpenSSL \([0-9][0-9]*\)\..*/\1/p')"
    [ -n "$major" ] && [ "$major" -ge 3 ] || die "verifying the agent needs OpenSSL 3 or newer; \
this machine has '$(openssl version 2>/dev/null)'. Build the agent yourself and install it with \
--binary, or install from a machine with a newer OpenSSL."
}

# verify_manifest MANIFEST SIGFILE WORKDIR -> exit 0 only if SIGFILE is a valid Ed25519
# signature by UPDATE_PUBLIC_KEY_HEX over MANIFEST's exact bytes.
verify_manifest() {
    local manifest="$1" sigfile="$2" work="$3"
    [ -s "$manifest" ] && [ -s "$sigfile" ] || return 1
    [ "${#UPDATE_PUBLIC_KEY_HEX}" -eq 64 ] || return 1
    # A raw 32-byte Ed25519 key wrapped in its fixed SubjectPublicKeyInfo DER prefix -- the
    # form openssl reads -- rather than shipping a PEM that could drift from the hex the agents
    # carry. The prefix is the same for every Ed25519 key.
    printf '%s' "302a300506032b6570032100$UPDATE_PUBLIC_KEY_HEX" | hex_to_file "$work/pub.der" \
        || return 1
    hex_to_file "$work/sig.bin" < "$sigfile" || return 1
    [ "$(wc -c < "$work/sig.bin")" -eq 64 ] || return 1
    openssl pkeyutl -verify -pubin -keyform DER -inkey "$work/pub.der" -rawin \
        -in "$manifest" -sigfile "$work/sig.bin" >/dev/null 2>&1
}

# manifest_field FILE NAME -> the string value of a top-level field. The manifest is
# sign_release.py's compact one-line JSON, and it is only ever parsed AFTER its signature has
# verified, so a field it does not contain is a signing bug, not an attack.
manifest_field() {
    grep -o "\"$2\" *: *\"[^\"]*\"" "$1" | head -1 | sed 's/.*: *"\(.*\)"/\1/'
}

fetch_verified_binary() {
    local dest="$1" work="$2" url version want got
    local manifest_url="${MANIFEST_URL_OVERRIDE:-$MANIFEST_URL}"
    need_openssl3
    http_get_file "$manifest_url" "$work/manifest.json" 2>/dev/null \
        && http_get_file "$manifest_url.sig" "$work/manifest.json.sig" 2>/dev/null \
        || die "could not fetch the signed Linux agent manifest from $manifest_url (or its .sig).
  If no Linux release has been cut since signed manifests were introduced, there is nothing
  verifiable to install yet: use --binary with a locally built agent, or --agent-url for an
  unverified download."
    verify_manifest "$work/manifest.json" "$work/manifest.json.sig" "$work" \
        || die "the Linux agent manifest at $manifest_url is NOT signed by the fleet's key. \
Refusing to install. Nothing on this machine was changed."
    version="$(manifest_field "$work/manifest.json" version)"
    want="$(manifest_field "$work/manifest.json" sha256 | tr 'A-F' 'a-f')"
    url="$(manifest_field "$work/manifest.json" url)"
    [ -n "$version" ] && [ -n "$url" ] && printf '%s' "$want" | grep -Eq '^[0-9a-f]{64}$' \
        || die "the signed manifest is missing its version, url or sha256 -- a release-signing bug."
    say "manifest verified: version $version, signed by the fleet key"
    say "binary  <- $url"
    http_get_file "$url" "$dest" || die "download failed: $url"
    # Hashed from stdin, never by name: GNU sha256sum prefixes its output with a backslash
    # when the FILE NAME contains a backslash or a newline, and that prefix lands in $got --
    # so a genuine binary under such a path was refused as tampered. Found on Windows, where
    # every path has one; a Linux install directory can have one too.
    got="$(sha256sum < "$dest" | cut -d' ' -f1)"
    [ "$got" = "$want" ] || die "the downloaded binary does not match the signed manifest \
(sha256 $got, expected $want). Refusing to install."
    say "binary  sha256 matches the signed manifest"
}

fetch_binary() {
    local dest="$1" work="$2" url
    if [ -n "$BINARY" ]; then
        [ -f "$BINARY" ] || die "no such file: $BINARY"
        cp "$BINARY" "$dest"
        say "binary  <- $BINARY (local, not checked against the fleet key)"
        return
    fi
    if [ -z "$AGENT_URL" ]; then
        fetch_verified_binary "$dest" "$work"
    else
        url="$AGENT_URL"
        warn "--agent-url: this download is NOT checked against the fleet's signing key"
        say "binary  <- $url"
        http_get_file "$url" "$dest" || die "download failed: $url"
    fi
    # A proxy or a 404 page saved as the binary is the classic silent failure here: the file
    # exists, the installer says success, and systemd reports "Exec format error" much later.
    [ -s "$dest" ] || die "downloaded file is empty: $url"
    head -c 4 "$dest" | grep -q $'\x7fELF' || die "downloaded file is not a Linux executable \
(got $(head -c 64 "$dest" | tr -d '\0' | head -1)). Check --agent-url."
}

fetch_unit() {
    local dest="$1"
    if [ -n "$UNIT_SRC" ]; then
        [ -f "$UNIT_SRC" ] || die "no such file: $UNIT_SRC"
        cp "$UNIT_SRC" "$dest"
        say "unit    <- $UNIT_SRC (local)"
        return
    fi
    http_get_file "$UNIT_URL" "$dest" || die "could not fetch the unit file: $UNIT_URL"
    grep -q '^\[Service\]' "$dest" || die "fetched unit file looks wrong: $UNIT_URL"
    say "unit    <- $UNIT_URL"
}

# ---------------------------------------------------------------- uninstall

do_uninstall() {
    echo "Removing the FleetHub Linux agent..."
    if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1; then
        systemctl disable --now "$SERVICE" 2>/dev/null || true
    fi
    rm -f "$UNIT_PATH"
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    say "removed $INSTALL_DIR and $UNIT_PATH"
    # /var/lib/fleethub/agent holds agent.json -- the identity the hub keys on. Removing it
    # means the next install enrolls as a NEW agent record for the same machine, so it is left
    # behind deliberately, as is the secret in /etc.
    say "kept    $CONF_DIR and /var/lib/fleethub/agent (secret + enrollment identity)"
    echo "Done. Delete those two by hand if you want this machine fully forgotten."
}

# ---------------------------------------------------------------- main

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --secret)      SECRET="${2:-}"; SECRET_FROM_ARGV=1; shift 2 ;;
            --secret-file) SECRET_FILE="${2:-}"; shift 2 ;;
            --hub)         HUB="${2:-}"; shift 2 ;;
            --agent-url)   AGENT_URL="${2:-}"; shift 2 ;;
            --manifest-url) MANIFEST_URL_OVERRIDE="${2:-}"; shift 2 ;;
            --binary)      BINARY="${2:-}"; shift 2 ;;
            --unit)        UNIT_SRC="${2:-}"; shift 2 ;;
            --uninstall)   UNINSTALL=1; shift ;;
            -h|--help)     usage; return 0 ;;
            *)             usage; die "unknown argument: $1" ;;
        esac
    done

    check_platform

    if [ "$UNINSTALL" -eq 1 ]; then do_uninstall; return 0; fi

    # Env var last so an explicit flag still wins, but available so the secret need never
    # appear in argv (and therefore never in `ps` or root's shell history).
    if [ -z "$SECRET" ] && [ -n "$SECRET_FILE" ]; then
        [ -f "$SECRET_FILE" ] || die "no such file: $SECRET_FILE"
        SECRET="$(cat "$SECRET_FILE")"
    fi
    # BOTH names are accepted, and that is the fix for a trap rather than laziness.
    #
    # The agent's own runtime variable is AGENT_ENROLLMENT_SECRET -- same name the hub's .env
    # uses, same name the Windows agent honours -- so somebody who has read the agent docs will
    # reach for that one here. Accepting only FLEETHUB_ENROLLMENT_SECRET meant
    # `sudo AGENT_ENROLLMENT_SECRET=... bash` fell through to "no secret supplied" and produced
    # a machine that installs cleanly, reports telemetry, and silently takes no commands. There
    # is no error to notice, because passing an environment variable this installer does not
    # read is indistinguishable from passing none.
    #
    # Rejected: keeping one name and documenting the difference. A doc note does not help the
    # person who never reads it, and the two variables mean the same thing to the only audience
    # that types either.
    if [ -z "$SECRET" ]; then
        SECRET="${FLEETHUB_ENROLLMENT_SECRET:-${AGENT_ENROLLMENT_SECRET:-}}"
    fi

    echo "Installing the FleetHub Linux agent..."

    local tmp
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT

    # Downloaded BEFORE the running agent is stopped, so a failed or slow download leaves a
    # working agent running rather than a machine with none.
    fetch_binary "$tmp/$ASSET_NAME" "$tmp"
    fetch_unit "$tmp/unit"

    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        say "stopping the running agent"
        systemctl stop "$SERVICE"
    fi

    install -d -m 0755 "$INSTALL_DIR"
    install -m 0755 "$tmp/$ASSET_NAME" "$INSTALL_DIR/fleethub-agent"
    say "binary  -> $INSTALL_DIR/fleethub-agent"

    install -d -m 0700 "$CONF_DIR"
    if [ -n "$SECRET" ]; then
        # install(1) with the mode set, rather than a redirect and a later chmod: a redirect
        # creates the file at the umask's mode FIRST and narrows it a moment afterwards, so the
        # secret is briefly world-readable on a machine that may have other users on it.
        printf '%s' "$SECRET" | install -m 0600 /dev/stdin "$CONF_DIR/agent.secret"
        say "secret  -> $CONF_DIR/agent.secret (0600)"
        [ "$SECRET_FROM_ARGV" -eq 1 ] && warn "the secret was passed as an argument, so it was \
visible in \`ps\` while this ran. Use --secret-file or FLEETHUB_ENROLLMENT_SECRET next time."
    elif [ -f "$CONF_DIR/agent.secret" ]; then
        say "secret  -> keeping the existing $CONF_DIR/agent.secret"
    else
        # Recorded, and repeated at the very end -- see the summary below for why once is not
        # enough.
        TELEMETRY_ONLY=1
        warn "no secret supplied: this machine will take no commands"
    fi

    # The hub override goes in the unit's EnvironmentFile rather than the unit itself, so
    # reinstalling with a fresh unit file cannot silently drop a machine back to the default hub.
    if [ -n "$HUB" ]; then
        printf 'FLEETHUB_HUB=%s\n' "$HUB" | install -m 0600 /dev/stdin "$CONF_DIR/agent.env"
        say "hub     -> $HUB"
    fi

    install -m 0644 "$tmp/unit" "$UNIT_PATH"
    say "unit    -> $UNIT_PATH"

    systemctl daemon-reload
    systemctl enable --now "$SERVICE"

    # `enable --now` returns 0 once systemd has ACCEPTED the job, which is not the same as the
    # agent being up. This is where an installer should notice that it is not.
    sleep 2
    echo
    if systemctl is-active --quiet "$SERVICE"; then
        echo "The agent is running. Follow it with:"
        echo "  journalctl -u $SERVICE -f"
        # Said AGAIN, last, because the earlier warning scrolls past behind the unit install
        # and systemd's own output -- and the state it describes is one an operator can easily
        # not notice for weeks. A telemetry-only machine looks completely healthy in the
        # console: it is online, it charts, it reports its inventory. It simply never runs
        # anything anyone asks it to, and the first sign is a command that sits there.
        if [ "${TELEMETRY_ONLY:-0}" -eq 1 ]; then
            echo
            echo "  !! TELEMETRY ONLY -- this machine will NOT accept commands."
            echo "     It had no enrollment secret, so it never enrolled. It will still appear"
            echo "     online in the console, which is what makes this easy to miss."
            echo "     Fix it with:"
            echo "       printf '%s' 'THE-SECRET' > $CONF_DIR/agent.secret"
            echo "       chmod 600 $CONF_DIR/agent.secret && systemctl restart $SERVICE"
        fi
    else
        echo "The agent was installed but is not running:" >&2
        journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
        exit 1
    fi
}

# Load-bearing: see the note at the top. Nothing above this line has side effects, so a
# truncated download does nothing rather than half an install.
main "$@"
