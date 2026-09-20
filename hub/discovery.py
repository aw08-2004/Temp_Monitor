"""Network discovery and shadow IT -- roadmap #18.

The half of FR-01 that #4 does not cover. Directory sync tells the hub about machines
somebody already enrolled; this tells it about the ones nobody did -- the switch in the
comms cupboard, the personal laptop on the staff VLAN, the printer a department bought
without asking.

**The sweep runs on an agent, not on the hub, and that is the same peer-relay decision
roadmap #10 made for Wake-on-LAN.** The hub is almost never on the network it is being
asked about: for a multi-site helpdesk it sits in one office and the subnet in question is
behind a router in another. So the hub picks a machine that is already on that segment and
asks *it* to look. `machine_nics` -- populated by #10's `NetworkInventoryReporter` -- is
what makes that possible without collecting anything new, and `wake.subnet_key` is what
groups it, from the adapter the machine reported and never from the source address of its
last heartbeat, which is the NAT'd site edge.

**The probe is ARP, deliberately, and not ping.** A Windows PC with the firewall at its
default settings does not answer ICMP from an unknown host, so a ping sweep of an office
subnet finds the printers and misses the PCs -- the exact inverse of what this feature is
for. A host cannot decline to answer ARP and stay reachable on the segment, so ARP is the
one probe whose silence really does mean "nothing is there". What it costs is the bound
below, and the bound turns out to be the feature's best safety property.

**A sweep can only look at a subnet the relay is itself on.** ARP does not cross a router,
so this is first a statement of fact -- an off-segment range would return nothing and look
like an empty network. But it is also the answer to the worry the roadmap entry raises
about this feature in particular, that "a discovery sweep is a scanner pointed at a
colleague's network": there is no way to aim one here. The target is not a parameter an
operator types, it is one of the subnets the chosen machine has already told the hub it
lives on, and `request_scan` refuses anything else. A range cap (`MAX_SWEEP_ADDRESSES`)
bounds the rest: a /24 is 254 probes, and a /8 is not a sweep anybody meant to ask for.

**A discovered host is an observation and becomes nothing.** That is the roadmap's second
open parameter -- "what a discovered-but-not-managed device is allowed to become without
someone approving it" -- answered as narrowly as it can be answered. Nothing here enrolls,
names, groups, alerts on or issues a command to an address. `unmanaged` is a row in a table
and a count on a card. The reason is not caution for its own sake: the classification is a
MAC match, and a MAC is a claim a device makes about itself, so acting on one automatically
would let anything on the segment decide what the fleet does about it.

**Classification is done here, at read time, against `machine_nics`, not written into the
row at ingest.** A machine enrolled an hour after a sweep should turn that scan's "unknown
device" into itself, because it IS itself and the alternative is a stale shadow-IT list
that a helpdesk stops trusting after the first false alarm. Storing the verdict would have
frozen the fleet as it was when the packet went out.

Kept free of Flask so it can be unit-tested in isolation, exactly like wake.py.
"""
import ipaddress
import sqlite3
import time
import uuid

import fleet
import wake

# ================================
# VOCABULARY
# ================================
#: Issued to the machine doing the looking. Like `wake_machine`, this command is about
#: something other than the machine that runs it -- but unlike `wake_machine` its subject is
#: a subnet rather than another managed PC, so there is no second machine to scope against.
COMMAND_TYPE = "network_sweep"

# ---------------------------------------------------------------- scan lifecycle
#: Recorded, and the sweep command is queued at the relay. Unlike a wake request this does
#: NOT sit across ticks waiting for a peer: the operator chose the machine, so if that
#: machine never answers the honest outcome is that this scan failed, not that the hub
#: should quietly go and ask somebody else about a subnet nobody asked about.
STATUS_SCANNING = "scanning"
#: The relay reported a host list. Includes a sweep that found nothing, which is a real and
#: common answer on a small VLAN.
STATUS_DONE = "done"
#: The relay could not sweep, or never picked the command up. `error` carries the agent's
#: own sentence -- including "unknown command type: network_sweep" from an agent too old to
#: have this executor, which is why there is no version gate in the console. See below.
STATUS_FAILED = "failed"

OPEN_STATUSES = (STATUS_SCANNING,)

# ---------------------------------------------------------------- classification
#: The address belongs to the machine that ran the sweep. Split out from `managed` because
#: "I can see myself" is not a finding, and letting it sit in the managed count makes a
#: sweep of an empty VLAN read as having found a PC.
CLASS_RELAY = "relay"
#: A MAC the hub already knows, from some machine's reported adapters.
CLASS_MANAGED = "managed"
#: Everything else on the wire. The point of the feature, and deliberately not called
#: "rogue": most of what lands here is a printer, an access point or a phone, and a console
#: that calls the accounting department's label printer rogue trains people to ignore it.
CLASS_UNMANAGED = "unmanaged"
CLASSIFICATIONS = (CLASS_RELAY, CLASS_MANAGED, CLASS_UNMANAGED)

# ================================
# BOUNDS
# ================================
#: The widest subnet this will sweep, in addresses. A /24 (254 hosts) is the common office
#: segment and a /22 (1022) covers the large flat ones; past that the probe run stops being
#: something an operator waits for and the result stops being something anybody reads. The
#: agent applies the same cap, so the machine never builds the answer the hub would refuse.
MAX_SWEEP_ADDRESSES = 1024
#: Hosts kept from one report. Above the address cap on purpose: they cannot both be hit,
#: and a relay sending more rows than the subnet has addresses is a relay to distrust.
MAX_HOSTS = 1024
#: Scans kept per machine. A sweep is a snapshot, and the value is in comparing the last few
#: rather than in keeping every one somebody ever clicked.
MAX_SCANS_PER_MACHINE = 20
MAX_TEXT_CHARS = 200
MAX_ERROR_CHARS = 500

#: How long a queued sweep may sit before `reconcile_once` gives up on it. Short compared
#: with a wake's TTL, and for the opposite reason: a wake is waiting on a machine that is
#: switched off, while a sweep is waiting on one the operator just saw online.
DEFAULT_SCAN_TTL_SECONDS = 10 * 60


class DiscoveryRejected(ValueError):
    """A sweep that will not be recorded at all, with an operator-facing reason."""


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_discovery_db(db_path):
    """Create the scan and host tables. Idempotent -- safe on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_scans (
                id           TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                subnet       TEXT NOT NULL,
                status       TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT '',
                requested_at INTEGER NOT NULL,
                finished_at  INTEGER,
                deadline_at  INTEGER,
                command_id   TEXT NOT NULL DEFAULT '',
                probed       INTEGER NOT NULL DEFAULT 0,
                truncated    INTEGER NOT NULL DEFAULT 0,
                error        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_scans_machine "
                     "ON discovery_scans(machine, requested_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_scans_status "
                     "ON discovery_scans(status)")
        # No classification column. The verdict is computed on read against machine_nics as
        # it stands NOW -- see the module docstring. What is stored is only what the relay
        # actually observed on the wire.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_hosts (
                scan_id  TEXT NOT NULL,
                ip       TEXT NOT NULL,
                mac      TEXT NOT NULL DEFAULT '',
                hostname TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scan_id, ip)
            )
            """
        )
        # The shadow-IT question is "has anything ever answered from this MAC", so the index
        # that matters is over the MAC rather than over the scan.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_hosts_mac "
                     "ON discovery_hosts(mac)")


# ================================
# ADDRESSING
# ================================
def sweepable_subnets(db_path, machine):
    """Every subnet this machine could sweep, from its own reported adapters.

    Wireless adapters count here and do NOT count for a wake, which is the one place these
    two features disagree about the same table. A magic packet cannot wake a laptop over
    Wi-Fi at all, so `wake.wakeable_nics` drops wireless; ARP over Wi-Fi is ordinary and a
    laptop on the office WLAN is often the only machine awake on that segment. Reusing the
    wake filter here would have quietly made half the fleet unable to look at the network it
    is sitting on.
    """
    network = wake.get_network(db_path, machine)
    subnets = set()
    for nic in network["nics"]:
        key = wake.subnet_key(nic.get("ipv4"), nic.get("prefix"))
        if key and _addresses_in(key) <= MAX_SWEEP_ADDRESSES:
            subnets.add(key)
    return sorted(subnets)


def _addresses_in(subnet):
    """How many addresses `subnet` spans, or 0 if it is not one."""
    try:
        return int(ipaddress.ip_network(subnet).num_addresses)
    except (ValueError, TypeError):
        return 0


# ================================
# READS
# ================================
def _clean(value, limit=MAX_TEXT_CHARS):
    return str(value if value is not None else "").strip()[:limit]


def managed_macs(db_path):
    """`{MAC: machine}` over every adapter the fleet has reported.

    One query, read fresh on each classification pass, because that freshness is the whole
    reason the verdict is not stored. A fleet of a few hundred machines has a few hundred
    adapters -- this is not a table that needs paging.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT machine, mac FROM machine_nics").fetchall()
    index = {}
    for row in rows:
        mac = wake.normalize_mac(row["mac"])
        # First writer wins rather than last: a MAC reported by two machines means a cloned
        # VM or a reused dock, and flip-flopping which one a host is attributed to between
        # reads would look like the network changing when it is the inventory that is
        # ambiguous. Sorted so the choice is at least stable across calls.
        if mac and mac not in index:
            index[mac] = row["machine"]
    return index


def classify(mac, relay, index):
    """The verdict for one observed MAC. Pure, so the table above can be tested directly."""
    normalized = wake.normalize_mac(mac)
    if not normalized:
        # An address that answered with no usable MAC. Counted as unmanaged rather than
        # dropped: something IS at that address, and "we saw a host we cannot identify" is
        # exactly the finding this feature exists to surface.
        return CLASS_UNMANAGED, ""
    owner = index.get(normalized)
    if owner is None:
        return CLASS_UNMANAGED, ""
    if owner == relay:
        return CLASS_RELAY, owner
    return CLASS_MANAGED, owner


def _scan_row(row, counts=None):
    scan = dict(row)
    scan["truncated"] = bool(scan.get("truncated"))
    scan["counts"] = counts if counts is not None else {}
    return scan


def get_scan(db_path, scan_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM discovery_scans WHERE id = ?",
                           (str(scan_id),)).fetchone()
    return None if row is None else _scan_row(row)


def scan_hosts(db_path, scan_id):
    """One scan's hosts, classified as the fleet stands right now.

    Sorted by address numerically rather than as text, so 10.4.7.9 comes before 10.4.7.10 --
    the ordering an operator reading a subnet expects, and the one SQLite will not give for
    a TEXT column.
    """
    scan = get_scan(db_path, scan_id)
    if scan is None:
        return []
    index = managed_macs(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM discovery_hosts WHERE scan_id = ?",
                            (str(scan_id),)).fetchall()
    hosts = []
    for row in rows:
        classification, owner = classify(row["mac"], scan["machine"], index)
        hosts.append({
            "ip": row["ip"],
            "mac": row["mac"],
            "hostname": row["hostname"],
            "classification": classification,
            "machine": owner,
        })
    hosts.sort(key=lambda h: _sort_key(h["ip"]))
    return hosts


def _sort_key(ip):
    try:
        return int(ipaddress.IPv4Address(ip))
    except (ValueError, TypeError):
        return 0


def count_by_class(hosts):
    """`{classification: n}` over the classes that actually occurred.

    Every class is present with a zero rather than omitted, because a card that renders
    "0 unmanaged" and one that renders nothing say different things, and only the first is
    the answer to "is there anything on this VLAN we do not own".
    """
    counts = {name: 0 for name in CLASSIFICATIONS}
    for host in hosts:
        counts[host["classification"]] = counts.get(host["classification"], 0) + 1
    return counts


def list_scans(db_path, machine=None, limit=50, open_only=False):
    clauses, params = [], []
    if machine:
        clauses.append("machine = ?")
        params.append(str(machine))
    if open_only:
        clauses.append(f"status IN ({','.join('?' for _ in OPEN_STATUSES)})")
        params.extend(OPEN_STATUSES)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT s.*, (SELECT COUNT(*) FROM discovery_hosts h WHERE h.scan_id = s.id) "
            f"AS host_count FROM discovery_scans s {where} "
            # rowid breaks the tie, and it has to: an operator who sweeps, reads the result
            # and sweeps again does all three inside one second, and `requested_at DESC`
            # alone would then show them either scan. The card would report the older one as
            # current -- a stale host list presented as the state of the network right now,
            # which is the one thing this feature must not do.
            f"ORDER BY s.requested_at DESC, s.rowid DESC LIMIT ?", params).fetchall()
    return [_scan_row(row) for row in rows]


def latest_scan(db_path, machine, subnet=None):
    """The most recent scan for this machine, with its hosts. None if it has never swept."""
    scans = list_scans(db_path, machine=machine, limit=MAX_SCANS_PER_MACHINE)
    if subnet:
        scans = [s for s in scans if s["subnet"] == subnet]
    if not scans:
        return None
    scan = scans[0]
    scan["hosts"] = scan_hosts(db_path, scan["id"])
    scan["counts"] = count_by_class(scan["hosts"])
    return scan


def open_scan_for(db_path, machine):
    """The sweep currently in flight on this machine, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM discovery_scans WHERE machine = ? AND "
            f"status IN ({','.join('?' for _ in OPEN_STATUSES)}) "
            f"ORDER BY requested_at DESC, rowid DESC LIMIT 1",
            [str(machine), *OPEN_STATUSES]).fetchone()
    return None if row is None else _scan_row(row)


# ================================
# REQUESTING A SWEEP
# ================================
def request_scan(db_path, machine, subnet, *, requested_by, online=False,
                 ttl_seconds=DEFAULT_SCAN_TTL_SECONDS, now=None):
    """Queue one sweep of `subnet`, run by `machine`. Returns the scan row.

    Refuses rather than records in four cases, and each refusal is a sentence an operator
    can act on:

      * the machine is offline -- a sweep is a live probe with nothing to persist for,
        unlike a wake, which exists precisely because the target is off;
      * the machine has never reported its adapters, so the hub does not know what it is
        on and cannot check the next condition;
      * **the subnet is not one of this machine's own** -- the bound the module docstring
        explains, and the one that makes this feature un-aimable;
      * a sweep is already running on it, because two concurrent ARP sweeps of one segment
        produce one answer twice and a lot of broadcast traffic.
    """
    machine = _clean(machine, 200)
    subnet = _clean(subnet, 64)
    now = int(time.time()) if now is None else int(now)
    if not machine:
        raise DiscoveryRejected("A machine is required.")
    if not online:
        raise DiscoveryRejected(
            f"{machine} is offline. A network sweep is a live probe from that machine, so "
            f"it has to be running to answer.")

    available = sweepable_subnets(db_path, machine)
    if not available:
        raise DiscoveryRejected(
            f"{machine} has not reported any network adapters yet, so the hub does not know "
            f"which subnets it is on. Its adapters arrive on the next heartbeat after the "
            f"agent has been upgraded.")
    if subnet not in available:
        raise DiscoveryRejected(
            f"{machine} is not on {subnet or '(no subnet)'}. A sweep is ARP, which does not "
            f"cross a router, so it can only look at a subnet this machine is itself on: "
            f"{', '.join(available)}.")
    if open_scan_for(db_path, machine) is not None:
        raise DiscoveryRejected(f"A sweep is already running on {machine}.")

    scan_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO discovery_scans(id, machine, subnet, status, requested_by, "
            "requested_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scan_id, machine, subnet, STATUS_SCANNING, _clean(requested_by), now,
             now + int(ttl_seconds)))
    _prune(db_path, machine)
    return get_scan(db_path, scan_id)


def attach_command(db_path, scan_id, command_id):
    """Record the command the sweep is riding on, so reconcile_once can read it back."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE discovery_scans SET command_id = ? WHERE id = ?",
                     (str(command_id), str(scan_id)))


def _prune(db_path, machine):
    """Drop this machine's oldest scans past the cap, hosts and all."""
    with get_conn(db_path) as conn:
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM discovery_scans WHERE machine = ? "
            "ORDER BY requested_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (str(machine), MAX_SCANS_PER_MACHINE)).fetchall()]
        for scan_id in stale:
            conn.execute("DELETE FROM discovery_hosts WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM discovery_scans WHERE id = ?", (scan_id,))
    return len(stale)


# ================================
# INGEST
# ================================
def _clean_host(raw):
    """One reported host, or None if there is no usable address in it.

    The address is what makes a row worth keeping; the MAC and the hostname are both allowed
    to be missing. A host that answered ARP with a malformed MAC still occupies an address,
    and dropping it would report an emptier network than the relay found -- the failure this
    parse exists to avoid, and the same one wake.record_network's strictness protects the
    NIC inventory from in the other direction.
    """
    if not isinstance(raw, dict):
        return None
    try:
        ip = str(ipaddress.IPv4Address(_clean(raw.get("ip"), 45)))
    except (ValueError, TypeError):
        return None
    return {
        "ip": ip,
        "mac": wake.normalize_mac(raw.get("mac")),
        "hostname": _clean(raw.get("hostname"), 120),
    }


def record_results(db_path, scan_id, machine, payload, now=None):
    """Store one relay's host list. Returns True if the scan was open and took it.

    **A report is only accepted from the machine the scan was issued to.** The endpoint that
    calls this authenticates the agent, so this is the second half of that check: without
    it, any enrolled machine could post a host list against another machine's scan and the
    console would attribute a fabricated network to a site it is not on.

    An empty host list is a real answer and is stored as one. A sweep of a VLAN with one PC
    on it finds one PC, and refusing to record that would leave the scan looking unfinished
    forever.
    """
    now = int(time.time()) if now is None else int(now)
    scan = get_scan(db_path, scan_id)
    if scan is None or scan["machine"] != str(machine) or scan["status"] != STATUS_SCANNING:
        return False

    raw_hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(raw_hosts, list):
        raw_hosts = []
    hosts, seen = [], set()
    for raw in raw_hosts[:MAX_HOSTS]:
        host = _clean_host(raw)
        # Keyed on the address, so a relay that reports 10.4.7.5 twice stores it once rather
        # than failing the whole insert on the primary key.
        if host and host["ip"] not in seen:
            seen.add(host["ip"])
            hosts.append(host)
    overflow = max(0, len(raw_hosts) - MAX_HOSTS)

    probed = payload.get("probed") if isinstance(payload, dict) else None
    try:
        probed = max(0, int(probed))
    except (ValueError, TypeError):
        probed = 0

    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM discovery_hosts WHERE scan_id = ?", (scan["id"],))
        conn.executemany(
            "INSERT INTO discovery_hosts(scan_id, ip, mac, hostname) VALUES (?, ?, ?, ?)",
            [(scan["id"], h["ip"], h["mac"], h["hostname"]) for h in hosts])
        conn.execute(
            "UPDATE discovery_scans SET status = ?, finished_at = ?, probed = ?, "
            "truncated = ?, error = '' WHERE id = ?",
            (STATUS_DONE, now, probed, 1 if overflow else 0, scan["id"]))
    return True


def fail_scan(db_path, scan_id, machine, error, now=None):
    """Record that a sweep could not run. Same sender check as record_results."""
    now = int(time.time()) if now is None else int(now)
    scan = get_scan(db_path, scan_id)
    if scan is None or scan["machine"] != str(machine) or scan["status"] != STATUS_SCANNING:
        return False
    _finish_failed(db_path, scan["id"], error, now)
    return True


def _finish_failed(db_path, scan_id, error, now):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE discovery_scans SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (STATUS_FAILED, now, _clean(error, MAX_ERROR_CHARS), str(scan_id)))


# ================================
# RECONCILIATION
# ================================
def reconcile_once(db_path, now=None):
    """Fail the scans whose command died, and the ones nobody ever answered.

    Two silences, and only one of them has a sentence attached. A command that FAILED
    carries the agent's own words -- including `unknown command type: network_sweep` from an
    agent too old to have the executor, which is the whole reason the console does not gate
    this on an agent version the way the Files and Processes cards do. A version constant
    would have to name a release that has not been cut yet, and this way the operator is
    told what is actually wrong by the machine itself.

    The other silence is a scan whose deadline passed with the report never arriving: the
    command may well have succeeded on the machine and the POST been lost. Saying so is the
    point -- a scan stuck at `scanning` forever reads as a slow network rather than as a
    result that is not coming.

    Returns how many scans moved.
    """
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM discovery_scans WHERE status = ?", (STATUS_SCANNING,))]

    moved = 0
    for scan in rows:
        if scan["command_id"]:
            command = fleet.get_command(db_path, scan["command_id"])
            if command is None:
                _finish_failed(db_path, scan["id"], "the sweep command disappeared", now)
                moved += 1
                continue
            status = command.get("status")
            if status in (fleet.STATUS_FAILED, fleet.STATUS_EXPIRED):
                told = ((command.get("result") or {}).get("output") or "").strip()
                if not told:
                    told = ("the machine did not pick the sweep up before it expired"
                            if status == fleet.STATUS_EXPIRED
                            else "the agent reported a failure")
                _finish_failed(db_path, scan["id"], told, now)
                moved += 1
                continue
        deadline = scan.get("deadline_at")
        if deadline and now >= int(deadline):
            _finish_failed(db_path, scan["id"],
                           "the machine never sent its results back", now)
            moved += 1
    return moved


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop everything this module holds for a machine that has been removed.

    Its scans go, hosts and all. What does NOT go is a row in somebody else's scan that
    happens to carry this machine's MAC -- that row is an observation the relay made, and
    deleting it would rewrite what was on the wire. It reclassifies itself on the next read,
    from `managed` to `unmanaged`, which is the truth once the machine is no longer managed.
    """
    machine = str(machine or "").strip()
    if not machine:
        return
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM discovery_hosts WHERE scan_id IN "
                     "(SELECT id FROM discovery_scans WHERE machine = ?)", (machine,))
        conn.execute("DELETE FROM discovery_scans WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Follow a rename, so a machine's sweep history survives it."""
    old_machine = str(old_machine or "").strip()
    new_machine = str(new_machine or "").strip()
    if not old_machine or not new_machine or old_machine == new_machine:
        return
    with get_conn(db_path) as conn:
        conn.execute("UPDATE discovery_scans SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
