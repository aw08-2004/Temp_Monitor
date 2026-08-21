"""Wake-on-LAN -- roadmap #10.

Power a sleeping machine on from the console, so an out-of-hours patch window or a remote
session does not depend on somebody being at the desk to press a button.

**Delivery is agent peer-relay, and that is the whole design.** The hub picks an ONLINE
machine whose reported IPv4/prefix puts it on the same subnet as the target, and issues
*it* a `wake_machine` command carrying the target's MACs and the subnet broadcast address;
that agent sends the magic packet to UDP 9. A hub-sent broadcast reaches the hub's own L2
segment and nothing else, which for a multi-site helpdesk is the wrong default -- the
machines that most need waking are the ones at the branch office the hub has never had a
broadcast domain in common with. Peer-relay is the only mechanism that crosses sites and
VLANs without anybody touching router configuration.

The hub's own broadcast survives as a FALLBACK ONLY (`_hub_delivery`), taken when no awake
peer exists and the hub can prove it shares the target's subnet. That covers the real hole
in peer-relay -- 3am, every PC on the subnet asleep -- for the single-site case, without
becoming the mechanism the design rejected.

**Subnet grouping is computed here, from reported NICs, and never from the source address
of a heartbeat.** That address is the NAT'd site edge, so grouping by it would put an
entire office into one fictional subnet and pick a relay in a different building.

**"No relay available" is a first-class outcome, not a failure.** Every machine on a subnet
being asleep is the EXPECTED state at 3am. "No awake machine on 10.4.7.0/24 to relay
through" is a diagnosis somebody can act on; "wake failed" is not, and the two would send
an operator looking in completely different places.

**A wake is an ATTEMPT, and every state name here says so.** WoL is fire-and-forget --
nothing acknowledges a magic packet, and a machine that was already awake looks identical
to one that just woke. So the packet going out is `SENT`, never success; the target's own
next check-in is what makes it `AWAKE`; and a target that never checks in is `NO_ANSWER`,
which is a report of silence rather than a claim that the wake failed. That is the "Back up
now" indirection again: recording a request and reporting it truthfully beats pretending to
talk to a machine that cannot answer.

**Confirmation compares against `sent_at`, not against online-ness.** A machine can read
online on a `last_seen` from eighty seconds BEFORE the packet was sent, and calling that a
successful wake would report a machine we never woke -- forever, for any machine that was
merely flapping. Only a check-in newer than the packet counts.

**Most of a WoL rollout is preconditions, not code**, so the preconditions are inventory
here rather than folklore in a runbook: `diagnose()` reads the reported NICs and names
every reason this machine cannot be woken -- no wired adapter, Wi-Fi only (this mechanism
cannot wake a laptop over Wi-Fi at all), the NIC's own "allow this device to wake the
computer" turned off, and Windows Fast Startup, which turns shutdown into a hybrid state
that defeats wake-from-S5 on many machines. A machine that can never be woken is shown as
such instead of being offered a button that silently does nothing. The firmware-side enable
is roadmap #9's job, which is why the two arrived together.

Authorization lives entirely upstream at `issue_commands` plus machine scope (wake_web.py).
Waking a PC is strictly less dangerous than shutting one down, which the same capability
already covers, so there is no new capability here. Nothing in this module checks a session,
exactly like fleet.py, packages.py and firmware.py.

Kept free of Flask so it can be unit-tested in isolation.
"""
import ipaddress
import socket
import sqlite3
import time
import uuid

import fleet

# ================================
# VOCABULARY
# ================================
#: Issued to the RELAY, not to the target. See the module docstring.
COMMAND_TYPE = "wake_machine"

#: The port the magic packet goes to. 9 (discard) is the conventional one; 7 (echo) and 0
#: are also seen in the wild. Nothing listens either way -- the NIC matches the frame in
#: hardware, below the IP stack -- so this only has to get past intervening filtering.
DEFAULT_WAKE_PORT = 9

# ---------------------------------------------------------------- request lifecycle
#: Recorded, and looking for a relay. A request SITS here across ticks rather than failing
#: on the first pass: a target whose subnet is entirely asleep is woken by the first peer to
#: come online, which is the case the scheduled pairing exists for.
STATUS_PENDING = "pending"
#: A `wake_machine` command is queued at a relay. The relay has not answered yet.
STATUS_RELAYING = "relaying"
#: A relay (or the hub) reported the packet went out. NOT success -- nothing acknowledges a
#: magic packet. See the module docstring.
STATUS_SENT = "sent"
#: The target checked in AFTER the packet went out. The only success this feature has.
STATUS_AWAKE = "awake"
#: The target was already online when the wake was asked for. Terminal, and deliberately not
#: an error: "it is already on" is the answer, and sending a packet to prove it would produce
#: a `sent` row that confirms itself a second later and teaches an operator to trust a
#: confirmation that means nothing.
STATUS_ALREADY_AWAKE = "already_awake"
#: The deadline passed with no awake machine on the target's subnet. First-class, named, and
#: not a failure -- see the module docstring.
STATUS_NO_RELAY = "no_relay"
#: The packet went out and the target never checked in within the confirmation window. A
#: report of silence, not a claim about the packet.
STATUS_NO_ANSWER = "no_answer"
#: There is nothing to send to: no wired adapter with a MAC and a subnet on record. Refused
#: before anything is dispatched, with the reason attached, on the same principle as
#: firmware's REFUSED -- a fact about the machine, not something going wrong.
STATUS_UNWAKEABLE = "unwakeable"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_PENDING, STATUS_RELAYING, STATUS_SENT, STATUS_AWAKE,
            STATUS_ALREADY_AWAKE, STATUS_NO_RELAY, STATUS_NO_ANSWER, STATUS_UNWAKEABLE,
            STATUS_CANCELLED)
#: States a request can still move out of. `sent` is open because the target has not
#: answered yet -- the packet is gone, but the outcome is not known.
OPEN_STATUSES = frozenset({STATUS_PENDING, STATUS_RELAYING, STATUS_SENT})
#: States in which no packet has left yet, so a cancel is honest. Once a relay has been
#: handed the command the packet may already be on the wire, and a row saying "cancelled"
#: over a machine that is now booting is worse than no cancel at all -- the same line
#: bios.cancel_change and the backup cancel both draw.
RECALLABLE_STATUSES = frozenset({STATUS_PENDING})

# ---------------------------------------------------------------- delivery methods
DELIVERY_RELAY = "relay"   # an awake agent on the target's subnet sent it
DELIVERY_HUB = "hub"       # the hub sent it itself, sharing the subnet, no peer available

# ---------------------------------------------------------------- adapter kinds
KIND_WIRED = "wired"
KIND_WIRELESS = "wireless"
KIND_OTHER = "other"
NIC_KINDS = frozenset({KIND_WIRED, KIND_WIRELESS, KIND_OTHER})

# ---------------------------------------------------------------- ingest caps
#: A machine has a handful of real adapters plus whatever Hyper-V, VPN clients and docks
#: have left behind. The cap bounds a misbehaving agent, not a real machine.
MAX_NICS = 32
MAX_TEXT_CHARS = 200
MAX_ERROR_CHARS = 500
#: How many requests to keep per machine. Audit-adjacent history an operator reads after the
#: fact ("did last night's window actually wake them"), not a log -- the audit trail is that.
MAX_REQUESTS_PER_MACHINE = 30
MAX_MACHINES_PER_REQUEST = 500

# ---------------------------------------------------------------- timings
#: How long a request keeps looking for a relay before it is called NO_RELAY. Bounded on
#: purpose: a wake asked for on Friday evening must not fire on Monday morning when the
#: first person to arrive gives it the peer it was waiting for. Same order as the command
#: TTL, and for the same reason.
DEFAULT_REQUEST_TTL_SECONDS = 15 * 60
#: How long after the packet to keep waiting for the target's own check-in. A wake from S3
#: is seconds; a cold boot to a running agent service is closer to a minute, and a machine
#: with a spinning disk and a slow domain logon is longer still. Five minutes is generous
#: enough that a slow boot is never called a failure, and short enough that an operator
#: watching the console gets an answer while they are still watching.
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 5 * 60


class WakeRejected(ValueError):
    """A wake the hub refuses to record. Its own type so the web layer answers 400 while a
    genuine bug still becomes a 500. Mirrors bios.ChangeRejected and firmware.PayloadRejected.
    """


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_wake_db(db_path):
    """Create the network-inventory and wake-request tables. Idempotent -- safe to call on
    every hub start next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per adapter, not columns on machine_info: a machine has several adapters
        # and which one is the wake target changes with a dock. The MAC is the identity
        # because it is the thing the magic packet names -- an adapter that gets a new IP
        # from DHCP is the same adapter, and re-keying on the name would fork a row every
        # time Windows renamed "Ethernet" to "Ethernet 2".
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_nics (
                machine      TEXT NOT NULL,
                mac          TEXT NOT NULL,
                name         TEXT NOT NULL DEFAULT '',
                description  TEXT NOT NULL DEFAULT '',
                ipv4         TEXT NOT NULL DEFAULT '',
                prefix       INTEGER,
                kind         TEXT NOT NULL DEFAULT 'other',
                link_up      INTEGER NOT NULL DEFAULT 0,
                wake_enabled INTEGER,
                reported_at  INTEGER NOT NULL,
                PRIMARY KEY (machine, mac)
            )
            """
        )
        # Relay lookup is "who else is on this subnet", so the index that matters is the one
        # over the address, not over the machine.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_nics_ipv4 "
                     "ON machine_nics(ipv4)")
        # Machine-level network facts that are not per-adapter. Its own table rather than an
        # ALTER on machine_info: this module owns the whole of its storage, so forget_machine
        # can drop all of it, and app.py's table stays app.py's.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_network (
                machine      TEXT PRIMARY KEY,
                fast_startup INTEGER,
                reported_at  INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wake_requests (
                id           TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                status       TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT '',
                reason       TEXT NOT NULL DEFAULT '',
                requested_at INTEGER NOT NULL,
                deadline_at  INTEGER,
                sent_at      INTEGER,
                finished_at  INTEGER,
                relay        TEXT NOT NULL DEFAULT '',
                delivery     TEXT NOT NULL DEFAULT '',
                subnet       TEXT NOT NULL DEFAULT '',
                command_id   TEXT NOT NULL DEFAULT '',
                attempts     INTEGER NOT NULL DEFAULT 0,
                error        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wake_requests_machine "
                     "ON wake_requests(machine, requested_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wake_requests_status "
                     "ON wake_requests(status)")


# ================================
# ADDRESSING
# ================================
def normalize_mac(raw):
    """Canonical `AA:BB:CC:DD:EE:FF`, or "" if it is not a MAC.

    Agents, vendors and registry values disagree about separators and case, and the MAC is
    both the primary key here and the six bytes that go into the magic packet -- so it is
    normalised once, on the way in, rather than compared loosely in six places later.
    """
    text = "".join(c for c in str(raw or "") if c.isalnum()).upper()
    if len(text) != 12:
        return ""
    try:
        bytes.fromhex(text)
    except ValueError:
        return ""
    if text == "000000000000":
        # A NIC reporting an all-zero MAC is a driver that has not initialised (Hyper-V's
        # placeholder adapters do this). Stored, it would look like a wakeable target and
        # every packet aimed at it would go nowhere.
        return ""
    return ":".join(text[i:i + 2] for i in range(0, 12, 2))


def mac_bytes(mac):
    """The six bytes of a normalised MAC. Raises ValueError on anything else."""
    normalized = normalize_mac(mac)
    if not normalized:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes.fromhex(normalized.replace(":", ""))


def _network(ipv4, prefix):
    """The IPv4Network an address sits in, or None if the pair is unusable.

    Loopback and link-local are rejected rather than stored as subnets: a machine reporting
    169.254.x (DHCP failed) would otherwise be grouped with every OTHER machine whose DHCP
    failed, and the hub would pick a relay in a different building that happens to share an
    APIPA range with the target. That is the NAT'd-heartbeat mistake in another costume.
    """
    try:
        address = ipaddress.IPv4Address(str(ipv4 or "").strip())
        length = int(prefix)
    except (ValueError, TypeError):
        return None
    if not 1 <= length <= 31:
        # /32 has no broadcast address and no peers; /0 is not a subnet anyone is on.
        return None
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return None
    try:
        return ipaddress.ip_network(f"{address}/{length}", strict=False)
    except ValueError:
        return None


def subnet_key(ipv4, prefix):
    """`"10.4.7.0/24"` -- the group key relay selection joins on, or "" if unusable.

    Computed hub-side from what the machine reported about its own adapter, never inferred
    from the source address of its last heartbeat: that is the NAT'd site edge, and grouping
    by it would fold a whole office into one fictional subnet.
    """
    network = _network(ipv4, prefix)
    return "" if network is None else str(network)


def broadcast_for(ipv4, prefix):
    """The directed broadcast address for that adapter's subnet, or ""."""
    network = _network(ipv4, prefix)
    return "" if network is None else str(network.broadcast_address)


def _in_subnet(ipv4, subnet):
    try:
        return ipaddress.IPv4Address(str(ipv4)) in ipaddress.ip_network(subnet)
    except (ValueError, TypeError):
        return False


# ================================
# INGEST
# ================================
def _clean(value, limit=MAX_TEXT_CHARS):
    return str(value if value is not None else "").strip()[:limit]


def _tri_state(value):
    """None stays None; everything else becomes 0/1.

    The three-way distinction is load-bearing for `wake_enabled`: "this adapter cannot wake
    the machine" and "the driver does not expose the setting" lead an operator to different
    places, and collapsing the second into False would tell them to fix something that is
    not broken.
    """
    return None if value is None else (1 if value else 0)


def _clean_nic(raw):
    """Normalise one reported adapter, or None if it is unusable.

    An adapter with no valid MAC is dropped rather than stored: the MAC is the primary key
    AND the payload of the magic packet, so an adapter nobody can name is one nobody can
    wake.
    """
    if not isinstance(raw, dict):
        return None
    mac = normalize_mac(raw.get("mac"))
    if not mac:
        return None

    kind = _clean(raw.get("kind")).lower()
    if kind not in NIC_KINDS:
        kind = KIND_OTHER

    ipv4 = _clean(raw.get("ipv4"), 45)
    prefix = raw.get("prefix")
    try:
        prefix = int(prefix)
    except (TypeError, ValueError):
        prefix = None
    if subnet_key(ipv4, prefix) == "":
        # Keep the adapter -- it is still inventory, and the console shows it -- but do not
        # keep an address that cannot be grouped. Storing a half-valid pair would make
        # `10.4.7.31/None` look like a subnet in every later query.
        ipv4, prefix = "", None

    return {
        "mac": mac,
        "name": _clean(raw.get("name")),
        "description": _clean(raw.get("description")),
        "ipv4": ipv4,
        "prefix": prefix,
        "kind": kind,
        "link_up": 1 if raw.get("link_up") else 0,
        "wake_enabled": _tri_state(raw.get("wake_enabled")),
    }


def record_network(db_path, machine, payload):
    """Store a machine's reported adapters. Returns how many were stored.

    Written from the heartbeat, so like bios.record_inventory the whole path is trimmed,
    type-checked and non-raising: a malformed report costs a stale NIC list, never a
    heartbeat. A heartbeat that 500s takes the machine offline fleet-wide, which is a far
    worse outcome than an out-of-date adapter.

    The adapter set is REPLACED, not merged. A NIC that has gone (a dock unplugged, a VPN
    client uninstalled) must disappear, because a stale adapter is a stale SUBNET -- and
    this module would go on offering that machine as a relay for a segment it is no longer
    on, which is a wake that silently goes nowhere.

    **But a MALFORMED report replaces nothing.** Replace-semantics plus a lenient parse is
    how a payload whose `nics` is not a list at all -- a truncated body, a future agent
    sending a different shape -- silently empties a good inventory and leaves the machine
    unwakeable with no error anywhere. So the adapter table is only touched when `nics` is
    genuinely a list; an empty LIST is a real report of "no adapters" and is stored, while
    an absent or non-list `nics` leaves the last good reading alone. Same instinct as
    bios.record_inventory refusing to read an unrecognised support state as `unsupported`.
    """
    if not isinstance(payload, dict) or not machine:
        return 0

    raw_nics = payload.get("nics")
    if not isinstance(raw_nics, list):
        # Not a network report at all. Nothing is written -- not even `reported_at`, which
        # is what diagnose() reads to tell "we have not been told" from "told, and has no
        # wired adapter". Stamping it here would turn a machine we have never heard from
        # into one we had accused of having no adapters.
        return 0

    nics = []
    seen = set()
    for raw in raw_nics[:MAX_NICS]:
        nic = _clean_nic(raw)
        if nic is not None and nic["mac"] not in seen:
            seen.add(nic["mac"])
            nics.append(nic)

    if raw_nics and not nics:
        # A non-empty list in which NOTHING was usable is a malformed report, not a machine
        # that has lost every adapter. The distinction matters because of replace-semantics:
        # believing it would empty a good inventory and leave the machine unwakeable, with
        # nothing on screen to say why. An EMPTY list is a different claim and is honoured --
        # that really is "this machine has no adapters worth reporting".
        return 0

    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_nics WHERE machine = ?", (machine,))
        conn.executemany(
            "INSERT INTO machine_nics(machine, mac, name, description, ipv4, prefix, kind, "
            "                         link_up, wake_enabled, reported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(machine, n["mac"], n["name"], n["description"], n["ipv4"], n["prefix"],
              n["kind"], n["link_up"], n["wake_enabled"], now) for n in nics],
        )
        conn.execute(
            "INSERT INTO machine_network(machine, fast_startup, reported_at) "
            "VALUES (?, ?, ?) ON CONFLICT(machine) DO UPDATE SET "
            "fast_startup = excluded.fast_startup, reported_at = excluded.reported_at",
            (machine, _tri_state(payload.get("fast_startup")), now),
        )
    return len(nics)


# ================================
# READ
# ================================
def _nic_row(row):
    nic = dict(row)
    nic.pop("machine", None)
    nic["link_up"] = bool(nic["link_up"])
    nic["wake_enabled"] = (None if nic["wake_enabled"] is None
                           else bool(nic["wake_enabled"]))
    nic["subnet"] = subnet_key(nic["ipv4"], nic["prefix"])
    nic["broadcast"] = broadcast_for(nic["ipv4"], nic["prefix"])
    return nic


def get_network(db_path, machine):
    """This machine's reported adapters and machine-level network facts.

    A machine that has never reported is `reported_at: None` with an empty adapter list --
    deliberately distinct from "reported, and has no wakeable adapter". One is an agent too
    old for this feature (or one that has not checked in yet) and the other is a fact about
    the hardware; rendering the first as the second would write off the whole fleet the day
    before the agent release lands. Same distinction bios.get_inventory draws with a null
    support state.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM machine_nics WHERE machine = ? ORDER BY kind ASC, name ASC, "
            "mac ASC", (machine,)).fetchall()
        meta = conn.execute(
            "SELECT fast_startup, reported_at FROM machine_network WHERE machine = ?",
            (machine,)).fetchone()
    return {
        "nics": [_nic_row(row) for row in rows],
        "fast_startup": (None if meta is None or meta["fast_startup"] is None
                         else bool(meta["fast_startup"])),
        "reported_at": None if meta is None else meta["reported_at"],
    }


def wakeable_nics(network):
    """The adapters a magic packet could usefully be aimed at.

    Wired, with a MAC and a subnet. Link state is deliberately NOT required: a sleeping
    machine's adapter reports the link it had when it was last awake, and requiring an up
    link would exclude exactly the machines this feature exists for. Wi-Fi is excluded
    outright -- this mechanism cannot wake a laptop over Wi-Fi, and offering it would be the
    button that silently does nothing.
    """
    return [n for n in (network or {}).get("nics") or []
            if n.get("kind") == KIND_WIRED and n.get("mac") and n.get("subnet")]


def diagnose(network):
    """Every reason this machine cannot (or may not) be woken, as a list of codes.

    Codes rather than sentences because the console renders them through i18n; the web layer
    translates. Ordered most-blocking first, so a UI that shows only the first shows the one
    that matters.

    A machine that has never reported gets `no_report` and nothing else: listing "no wired
    adapter" for a machine that has told us nothing would be an accusation about hardware we
    have not seen.
    """
    network = network or {}
    if network.get("reported_at") is None:
        return ["no_report"]

    nics = network.get("nics") or []
    wakeable = wakeable_nics(network)
    problems = []
    if not wakeable:
        if any(n.get("kind") == KIND_WIRELESS for n in nics) and \
                not any(n.get("kind") == KIND_WIRED for n in nics):
            # The honest diagnosis for a laptop on Wi-Fi: not "misconfigured", but "this
            # mechanism does not reach you at all".
            problems.append("wireless_only")
        elif any(n.get("kind") == KIND_WIRED for n in nics):
            # There IS an Ethernet adapter, it just has no usable address -- so we cannot
            # work out which subnet to broadcast on, or who could relay for it.
            problems.append("no_address")
        else:
            problems.append("no_wired_nic")
    elif all(n.get("wake_enabled") is False for n in wakeable):
        # Every candidate adapter has "allow this device to wake the computer" turned off.
        # Not fatal to trying -- the flag is read from the driver and drivers lie -- but it
        # is the first thing to check, and the agent can turn it back on.
        problems.append("wake_disabled")
    if network.get("fast_startup") is True:
        # Hybrid shutdown, so a "shut down" machine is really hibernating from a session
        # that never fully ended. Wake-from-S5 fails on many NICs in that state, and the
        # symptom is a wake that works from Sleep and not from Shut Down -- which reads as
        # an intermittent fault rather than a setting.
        problems.append("fast_startup")
    return problems


# ================================
# RELAY SELECTION
# ================================
def _online_index(machines):
    """`{machine: last_seen}` for the machines the caller says are online.

    The roster carries `last_seen` and not just a flag because confirmation compares against
    the moment the packet went out -- see confirm_once. A boolean cannot answer "did this
    machine check in AFTER we woke it".
    """
    index = {}
    for entry in machines or []:
        if isinstance(entry, dict):
            if entry.get("online"):
                index[entry.get("machine")] = entry.get("last_seen") or 0
        elif entry:
            index[entry] = 0
    index.pop(None, None)
    return index


def find_relay(db_path, machine, online, exclude=()):
    """Pick an awake machine that shares a subnet with `machine`.

    Returns `{"relay", "subnet", "broadcast", "macs"}` or None. `macs` is EVERY wakeable MAC
    the target has on that subnet, not one: a machine can have several wired adapters (a dock
    and an onboard NIC) and the hub has no way to know which one is cabled right now. One
    extra 102-byte frame per adapter is not a cost worth reasoning about, and picking wrong
    is the difference between the feature working and not.

    `exclude` drops relays that have already failed for this request, so a retry reaches for
    a different peer instead of asking the same broken agent again.
    """
    network = get_network(db_path, machine)
    candidates = wakeable_nics(network)
    if not candidates:
        return None

    # A set of names and a `{name: last_seen}` map are both natural things to hand this,
    # and the only difference is whether recency can break a tie. Normalised here so a
    # caller passing a set does not fail on `.get` three lines further down.
    if not isinstance(online, dict):
        online = {name: 0 for name in (online or ())}
    online_names = {n for n in online if n and n != machine}
    if not online_names:
        return None
    excluded = {e for e in exclude if e}

    # Group the target's own MACs by subnet first, so a machine with adapters on two
    # segments prefers the subnet we can actually reach rather than the first one listed.
    by_subnet = {}
    for nic in candidates:
        by_subnet.setdefault(nic["subnet"], []).append(nic)

    with get_conn(db_path) as conn:
        placeholders = ",".join("?" for _ in online_names)
        rows = conn.execute(
            f"SELECT n.machine, n.ipv4, n.prefix FROM machine_nics n "
            f"WHERE n.machine IN ({placeholders}) AND n.ipv4 != ''",
            list(online_names)).fetchall()

    peers_by_subnet = {}
    for row in rows:
        key = subnet_key(row["ipv4"], row["prefix"])
        if key:
            peers_by_subnet.setdefault(key, set()).add(row["machine"])

    for subnet, nics in by_subnet.items():
        peers = sorted((peers_by_subnet.get(subnet) or set()) - excluded)
        if not peers:
            continue
        # Most recently heard from wins: every candidate is "online", but the one that
        # answered eight seconds ago is likelier to still be there than the one at the far
        # edge of the 90-second window.
        relay = max(peers, key=lambda name: (online.get(name, 0), name))
        return {
            "relay": relay,
            "subnet": subnet,
            "broadcast": nics[0]["broadcast"],
            "macs": sorted({n["mac"] for n in nics}),
        }
    return None


def target_subnets(db_path, machine):
    """Every subnet this machine could be woken on. Used by the console to say WHICH subnet
    had no awake peer, which is the whole value of the no_relay outcome."""
    return sorted({n["subnet"] for n in wakeable_nics(get_network(db_path, machine))})


# ================================
# THE HUB'S OWN BROADCAST (fallback only)
# ================================
def magic_packet(mac):
    """The 102-byte frame: six 0xFF bytes then the target MAC sixteen times."""
    return b"\xff" * 6 + mac_bytes(mac) * 16


def hub_shares_subnet(target_ipv4, subnet):
    """Whether this hub is on `subnet`, decided by the routing table rather than by
    enumerating interfaces.

    Connecting a UDP socket sends NOTHING -- it only asks the kernel which local address it
    would source from to reach that host -- so this is a pure lookup with no packet and no
    dependency on a platform-specific interface API. If the address the OS would use to
    reach the target falls inside the target's own subnet, the hub is on that segment.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        # A unicast address, deliberately, not the broadcast: connecting a UDP socket to a
        # broadcast address without SO_BROADCAST is refused on some platforms, and this call
        # must be a routing question and nothing else.
        probe.connect((str(target_ipv4), DEFAULT_WAKE_PORT))
        local = probe.getsockname()[0]
    except OSError:
        return False
    finally:
        probe.close()
    return _in_subnet(local, subnet)


def send_magic_packet(macs, broadcast, port=DEFAULT_WAKE_PORT):
    """Send one magic packet per MAC from the hub. Returns how many went out.

    Only ever reached as a fallback (see `_hub_delivery`), and it is not the mechanism this
    feature is built on -- a hub broadcast reaches the hub's own L2 segment and nothing else.
    """
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            try:
                sock.sendto(magic_packet(mac), (str(broadcast), int(port)))
                sent += 1
            except (OSError, ValueError):
                continue
    finally:
        sock.close()
    return sent


def _hub_delivery(db_path, machine):
    """`{"subnet", "broadcast", "macs"}` if the hub itself can reach this machine's subnet.

    Checked only after peer-relay has found nobody. The point is the 3am case on a
    single-site fleet: every PC on the subnet is asleep, so there is no peer to relay
    through, and the hub is sitting on that very segment.
    """
    candidates = wakeable_nics(get_network(db_path, machine))
    for nic in candidates:
        if not nic["ipv4"] or not hub_shares_subnet(nic["ipv4"], nic["subnet"]):
            continue
        # Every MAC on that subnet, matching the relay path exactly: the hub has no more
        # idea than a peer does which of a docked machine's adapters is cabled right now.
        return {"subnet": nic["subnet"], "broadcast": nic["broadcast"],
                "macs": sorted({n["mac"] for n in candidates
                                if n["subnet"] == nic["subnet"]})}
    return None


# ================================
# REQUESTS
# ================================
def _row(row):
    request = dict(row)
    request["open"] = request["status"] in OPEN_STATUSES
    return request


def get_request(db_path, request_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM wake_requests WHERE id = ?",
                           (request_id,)).fetchone()
    return _row(row) if row is not None else None


def open_request_for(db_path, machine):
    """This machine's in-flight wake, if it has one. A second request while the first is
    unresolved is answered with the first rather than queued: two magic packets aimed at one
    machine is not twice as much waking, and two rows racing to be confirmed by the same
    check-in would resolve arbitrarily."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM wake_requests WHERE machine = ? AND status IN "
            "(?, ?, ?) ORDER BY requested_at DESC, rowid DESC LIMIT 1",
            (machine, STATUS_PENDING, STATUS_RELAYING, STATUS_SENT)).fetchone()
    return _row(row) if row is not None else None



# ---------------------------------------------------------------- Dashboard tallies
#
# Counted in SQL rather than by loading rows and length-ing them. The Dashboard asks all of
# these on one poll, from every open console, so a helper that returned a hundred rows for a
# number would be the most expensive thing on the page by a wide margin.
#
# `machines` is an optional iterable used as the scope filter, applied HERE rather than by
# the caller for the same reason: dropping out-of-scope rows in Python means reading them
# first, and an operator scoped to three machines would still pay for the whole fleet.


def count_open_requests(db_path, machines=None):
    """Wake requests still in flight -- pending, relaying or sent.

    "Sent" counts as open on purpose, and it is the interesting one: nothing acknowledges a
    magic packet, so a request sits in `sent` until the machine itself checks in. A count
    that dropped it would read zero while the fleet was still waking up.
    """
    statuses = sorted(OPEN_STATUSES)
    clauses = [f"status IN ({','.join('?' for _ in statuses)})"]
    params = list(statuses)

    if machines is not None:
        scope = list(machines)
        if not scope:
            return 0
        clauses.append(f"machine IN ({','.join('?' for _ in scope)})")
        params.extend(scope)

    with get_conn(db_path) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM wake_requests WHERE " + " AND ".join(clauses),
            params).fetchone()[0])


def list_requests(db_path, machine=None, limit=50, open_only=False):
    clauses, params = [], []
    if machine:
        clauses.append("machine = ?")
        params.append(machine)
    if open_only:
        clauses.append(f"status IN ({','.join('?' for _ in OPEN_STATUSES)})")
        params.extend(sorted(OPEN_STATUSES))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM wake_requests {where} "
            f"ORDER BY requested_at DESC, rowid DESC LIMIT ?", params).fetchall()
    return [_row(row) for row in rows]


def _insert(conn, *, machine, status, requested_by, reason, now, deadline, error="",
            subnet=""):
    request_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO wake_requests(id, machine, status, requested_by, reason, "
        "requested_at, deadline_at, finished_at, subnet, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, machine, status, str(requested_by or "system"), _clean(reason),
         now, deadline, None if status in OPEN_STATUSES else now, subnet,
         _clean(error, MAX_ERROR_CHARS)))
    # Trim oldest-first, and only terminal rows: an in-flight request is never evicted by
    # history pressure, however much of it there is.
    conn.execute(
        f"DELETE FROM wake_requests WHERE machine = ? AND status NOT IN "
        f"({','.join('?' for _ in OPEN_STATUSES)}) AND id NOT IN "
        f"(SELECT id FROM wake_requests WHERE machine = ? AND status NOT IN "
        f" ({','.join('?' for _ in OPEN_STATUSES)}) "
        f" ORDER BY requested_at DESC, rowid DESC LIMIT ?)",
        [machine, *sorted(OPEN_STATUSES), machine, *sorted(OPEN_STATUSES),
         MAX_REQUESTS_PER_MACHINE])
    return request_id


def request_wake(db_path, machine, *, requested_by, reason="", online=False,
                 ttl_seconds=DEFAULT_REQUEST_TTL_SECONDS, now=None):
    """Ask for a machine to be woken. Returns the resulting request row.

    Nothing is dispatched here -- the tick owns dispatch, so a button press and a scheduled
    wake travel one code path. Two mechanisms for "send it" is how the immediate case grows a
    bug the scheduled case does not have; packages.py's reasoning, unchanged.

    Three answers come back immediately rather than through the scheduler, because all three
    are already known and pretending otherwise would leave an operator watching a spinner for
    a question that was settled before they asked:

      * the machine is already online       -> ALREADY_AWAKE
      * it has no wakeable adapter          -> UNWAKEABLE, with the diagnosis attached
      * it already has a request in flight  -> that request, unchanged
    """
    machine = _clean(machine)
    if not machine:
        raise WakeRejected("A wake needs a machine.")
    now = int(time.time()) if now is None else int(now)

    existing = open_request_for(db_path, machine)
    if existing is not None:
        return existing

    network = get_network(db_path, machine)
    with get_conn(db_path) as conn:
        if online:
            request_id = _insert(conn, machine=machine, status=STATUS_ALREADY_AWAKE,
                                 requested_by=requested_by, reason=reason, now=now,
                                 deadline=None)
        elif not wakeable_nics(network):
            request_id = _insert(conn, machine=machine, status=STATUS_UNWAKEABLE,
                                 requested_by=requested_by, reason=reason, now=now,
                                 deadline=None,
                                 error=";".join(diagnose(network)) or "no_wired_nic")
        else:
            request_id = _insert(conn, machine=machine, status=STATUS_PENDING,
                                 requested_by=requested_by, reason=reason, now=now,
                                 deadline=now + max(60, int(ttl_seconds)))
    return get_request(db_path, request_id)


def request_many(db_path, machines, *, requested_by, reason="", online=(),
                 ttl_seconds=DEFAULT_REQUEST_TTL_SECONDS, now=None):
    """Wake a set of machines. Returns one request row per name, in the order given.

    Used by the fleet-wide button and by the schedulers. A name that is already awake or
    cannot be woken still gets a row, with its own outcome -- silently dropping it is how
    somebody believes forty machines were woken and thirty were not.
    """
    names, seen = [], set()
    for raw in machines or []:
        name = _clean(raw)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    if len(names) > MAX_MACHINES_PER_REQUEST:
        raise WakeRejected(f"At most {MAX_MACHINES_PER_REQUEST} machines can be woken in "
                           f"one request.")
    online_set = set(online or ())
    return [request_wake(db_path, name, requested_by=requested_by, reason=reason,
                         online=name in online_set, ttl_seconds=ttl_seconds, now=now)
            for name in names]


def cancel_request(db_path, request_id):
    """Give up on a wake no relay has been handed yet. Only a PENDING request can be
    cancelled: once a relay holds the command the packet may already be on the wire, and a
    row saying "cancelled" over a machine that is now booting is worse than no cancel."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE wake_requests SET status = ?, finished_at = ?, error = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_CANCELLED, int(time.time()), "cancelled before a relay was found",
             request_id, STATUS_PENDING))
    return (cursor.rowcount or 0) > 0


# ================================
# DISPATCH
# ================================
def _claim(db_path, request_id, now, relay, subnet):
    """Move one request from PENDING to RELAYING, atomically.

    Claim before queueing, exactly as packages._claim_target and firmware._claim_target do:
    a crash between the two costs one retry, and the other order leaves a `wake_machine`
    command whose request is still pending, so the next tick would queue a second one.
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE wake_requests SET status = ?, relay = ?, subnet = ?, "
            "attempts = attempts + 1 WHERE id = ? AND status = ?",
            (STATUS_RELAYING, relay, subnet, request_id, STATUS_PENDING))
    return (cursor.rowcount or 0) == 1


def _finish(db_path, request_id, status, *, error="", now=None):
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE wake_requests SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (status, now, _clean(error, MAX_ERROR_CHARS), request_id))


def _mark_sent(db_path, request_id, delivery, now):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE wake_requests SET status = ?, sent_at = ?, delivery = ? WHERE id = ?",
            (STATUS_SENT, now, delivery, request_id))


def dispatch_once(db_path, machines=None, now=None,
                  ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS, allow_hub_broadcast=True):
    """Find a relay for every pending request and queue it a `wake_machine` command.

    A request with no relay is LEFT PENDING rather than failed -- that is the persistence the
    roadmap asked for, and it is what makes the scheduled pairing work: a target whose subnet
    is entirely asleep is woken by the first peer to come online, within one tick of it
    arriving. `expire_stale` is what eventually calls it NO_RELAY, so the waiting is bounded.

    Returns how many commands were queued (hub-sent packets included).
    """
    now = int(time.time()) if now is None else int(now)
    online = _online_index(machines)

    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM wake_requests WHERE status = ? ORDER BY requested_at ASC",
            (STATUS_PENDING,))]

    dispatched = 0
    for request in rows:
        if request["deadline_at"] and request["deadline_at"] <= now:
            continue  # expire_stale owns the outcome; do not race it to a different one
        machine = request["machine"]
        if machine in online:
            # It came up on its own while the request waited -- somebody switched it on, or
            # a previous packet worked after we stopped waiting. Recorded honestly: we did
            # not wake it, so this is ALREADY_AWAKE, not AWAKE.
            _finish(db_path, request["id"], STATUS_ALREADY_AWAKE, now=now)
            continue

        exclude = [request["relay"]] if request["relay"] else []
        choice = find_relay(db_path, machine, online, exclude=exclude)
        if choice is None and exclude:
            # Nobody but the peer that already failed. Better to ask it again than to leave
            # the machine asleep -- a failed command is not proof the agent is broken.
            choice = find_relay(db_path, machine, online)

        if choice is not None:
            if not _claim(db_path, request["id"], now, choice["relay"], choice["subnet"]):
                continue  # another pass took it
            try:
                command_id = fleet.create_command(
                    db_path, machine=choice["relay"], command_type=COMMAND_TYPE,
                    # A MAC and a broadcast address, in the clear, unlike the restore-plan
                    # and BIOS-password cases: create_command audits params verbatim, and
                    # here that is exactly what an auditor wants to read back -- who asked
                    # for what to be woken, through which machine. Neither field is a
                    # secret, so there is nothing to fetch from a second endpoint.
                    params={"request_id": request["id"], "target": machine,
                            "macs": choice["macs"], "broadcast": choice["broadcast"],
                            "port": DEFAULT_WAKE_PORT},
                    issued_by=request["requested_by"] or "system", ttl_seconds=ttl_seconds)
            except ValueError as e:
                # The relay went away between the roster and this call. Back to pending so
                # the next pass picks somebody else.
                with get_conn(db_path) as conn:
                    conn.execute("UPDATE wake_requests SET status = ?, error = ? "
                                 "WHERE id = ?",
                                 (STATUS_PENDING, _clean(str(e), MAX_ERROR_CHARS),
                                  request["id"]))
                continue
            with get_conn(db_path) as conn:
                conn.execute("UPDATE wake_requests SET command_id = ? WHERE id = ?",
                             (command_id, request["id"]))
            dispatched += 1
            continue

        # No awake peer anywhere on the target's subnets. Before leaving it pending, see
        # whether the hub is itself on that segment -- the single-site 3am case.
        if not allow_hub_broadcast:
            continue
        delivery = _hub_delivery(db_path, machine)
        if delivery is None:
            continue
        if not _claim(db_path, request["id"], now, "", delivery["subnet"]):
            continue
        if send_magic_packet(delivery["macs"], delivery["broadcast"]):
            _mark_sent(db_path, request["id"], DELIVERY_HUB, now)
            dispatched += 1
        else:
            with get_conn(db_path) as conn:
                conn.execute("UPDATE wake_requests SET status = ?, error = ? WHERE id = ?",
                             (STATUS_PENDING, "the hub could not send a magic packet",
                              request["id"]))
    return dispatched


def reconcile_once(db_path, now=None):
    """Read finished relay commands back and move their requests on.

    Reading the command row rather than taking a report on a dedicated agent endpoint, which
    `firmware` needed and this does not: the relay's whole job is one `sendto`, its answer is
    a success bit, and the command queue already carries success bits with an audit trail. A
    second endpoint would be a second thing to authorise for no extra information.

    A failed or expired relay command puts the request BACK to pending -- with a different
    relay excluded next pass -- rather than failing it. One agent being unable to send is not
    evidence the target cannot be woken, and there may be nine other machines on that subnet.
    """
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM wake_requests WHERE status = ? AND command_id != ''",
            (STATUS_RELAYING,))]

    moved = 0
    for request in rows:
        command = fleet.get_command(db_path, request["command_id"])
        if command is None:
            _finish(db_path, request["id"], STATUS_NO_RELAY,
                    error="the relay command disappeared", now=now)
            moved += 1
            continue
        status = command.get("status")
        if status == fleet.STATUS_DONE:
            _mark_sent(db_path, request["id"], DELIVERY_RELAY, now)
            moved += 1
        elif status in (fleet.STATUS_FAILED, fleet.STATUS_EXPIRED):
            told = ((command.get("result") or {}).get("output") or "").strip()
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE wake_requests SET status = ?, command_id = '', error = ? "
                    "WHERE id = ?",
                    (STATUS_PENDING,
                     _clean(f"{request['relay'] or 'relay'}: {told or status}",
                            MAX_ERROR_CHARS),
                     request["id"]))
            moved += 1
    return moved


def confirm_once(db_path, machines=None, now=None,
                 confirm_timeout=DEFAULT_CONFIRM_TIMEOUT_SECONDS):
    """Turn SENT rows green when their machine checks in, and give up when it does not.

    **The comparison is against `sent_at`, not against online-ness.** A machine can read
    online on a `last_seen` from before the packet went out, and treating that as a
    successful wake would confirm a wake nobody performed -- every time, for any machine
    that was merely flapping in and out of the 90-second window. Only a check-in NEWER than
    the packet is evidence.

    Returns `(confirmed, given_up)`.
    """
    now = int(time.time()) if now is None else int(now)
    seen = {}
    for entry in machines or []:
        if isinstance(entry, dict) and entry.get("machine"):
            seen[entry["machine"]] = entry.get("last_seen") or 0

    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM wake_requests WHERE status = ?", (STATUS_SENT,))]

    confirmed = given_up = 0
    for request in rows:
        sent_at = request["sent_at"] or request["requested_at"]
        last_seen = seen.get(request["machine"]) or 0
        if last_seen >= sent_at:
            _finish(db_path, request["id"], STATUS_AWAKE, now=now)
            confirmed += 1
        elif now - sent_at >= int(confirm_timeout):
            _finish(db_path, request["id"], STATUS_NO_ANSWER,
                    error="the magic packet was sent and the machine has not checked in",
                    now=now)
            given_up += 1
    return confirmed, given_up


def expire_stale(db_path, now=None):
    """Close out requests whose deadline passed while they were still looking for a relay.

    This is where NO_RELAY is decided, and it is the only place: dispatch leaves a request
    pending precisely so the first peer to come online can serve it, so something else has
    to bound the waiting. Without it a wake asked for on Friday evening fires on Monday
    morning, when the first person to arrive supplies the peer it was waiting for.
    """
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, machine, status, relay, subnet FROM wake_requests "
            "WHERE status IN (?, ?) AND deadline_at IS NOT NULL AND deadline_at <= ?",
            (STATUS_PENDING, STATUS_RELAYING, now))]
    for request in rows:
        if request["status"] == STATUS_RELAYING and request["relay"]:
            # A relay WAS found and simply never answered -- its command outlived the
            # deadline, or the machine went down holding it. Saying "nobody was awake"
            # here would send an operator to look at the wrong thing entirely.
            error = (f"{request['relay']} was asked to send the packet and never "
                     f"reported back")
        else:
            subnets = (request["subnet"]
                       or ", ".join(target_subnets(db_path, request["machine"])))
            error = (f"no awake machine on {subnets} to relay through" if subnets
                     else "no awake machine on this machine's subnet to relay through")
        _finish(db_path, request["id"], STATUS_NO_RELAY, error=error, now=now)
    return len(rows)


def tick(db_path, machines=None, now=None,
         ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
         confirm_timeout=DEFAULT_CONFIRM_TIMEOUT_SECONDS, allow_hub_broadcast=True):
    """One scheduler pass: read relays back, confirm arrivals, retire, then dispatch.

    Order matters. Reconciling first lets a relay that answered this second move to SENT and
    be confirmed in the same pass; expiring before dispatching stops a request being handed
    a relay a millisecond before its deadline retires it. Dispatch goes last so a request
    that just returned to pending after a failed relay gets its retry immediately rather
    than a tick later.

    Returns `(confirmed, expired, dispatched)`.
    """
    now = int(time.time()) if now is None else int(now)
    reconcile_once(db_path, now=now)
    confirmed, gave_up = confirm_once(db_path, machines, now=now,
                                      confirm_timeout=confirm_timeout)
    expired = expire_stale(db_path, now=now) + gave_up
    dispatched = dispatch_once(db_path, machines, now=now, ttl_seconds=ttl_seconds,
                               allow_hub_broadcast=allow_hub_broadcast)
    return confirmed, expired, dispatched


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's adapters and wake history.

    On the same lifecycle discipline as permissions.forget_machine and bios.forget_machine,
    and with an extra reason of its own: a stale NIC row keeps offering a machine that no
    longer exists as a RELAY for its subnet, and every wake routed through it would be
    queued at a hostname nothing answers to.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_nics WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM machine_network WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM wake_requests WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move a machine's network inventory during a duplicate-serial merge.

    The surviving row wins for the ADAPTERS -- same hardware, and the survivor has reported
    for itself more recently -- while the request HISTORY always moves, because a merge folds
    two records of one physical machine together and "was this PC woken on Tuesday" is the
    same question afterwards. Exactly bios.rename_machine's split, for the same reasons.
    """
    with get_conn(db_path) as conn:
        existing = conn.execute("SELECT 1 FROM machine_nics WHERE machine = ? LIMIT 1",
                                (new_machine,)).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM machine_nics WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_nics SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
        meta = conn.execute("SELECT 1 FROM machine_network WHERE machine = ?",
                            (new_machine,)).fetchone()
        if meta is not None:
            conn.execute("DELETE FROM machine_network WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_network SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
        conn.execute("UPDATE wake_requests SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
