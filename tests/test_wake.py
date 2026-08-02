"""wake.py -- Wake-on-LAN (roadmap #10).

The three things that can genuinely hurt here, and what each is tested against:

  * **Subnet grouping decides who is asked to send the packet.** Get it wrong and the hub
    picks a relay in a different building, broadcasts onto a segment the target is not on,
    and reports it as sent. So the addressing helpers are tested against the cases that
    would produce a plausible-looking wrong answer -- APIPA, /32, a mask with no prefix --
    not just the happy path.

  * **Ingest arrives on the HEARTBEAT.** A heartbeat that 500s takes the machine offline
    fleet-wide, so nothing a machine can send may raise, and a NIC that has gone must
    actually disappear -- a stale adapter is a stale subnet, and this module would go on
    offering that machine as a relay for a segment it left.

  * **The lifecycle must never claim more than it knows.** Nothing acknowledges a magic
    packet, so `sent` is not success, `no_relay` is not a failure, and a check-in that
    PREDATES the packet is not evidence the wake worked. Each of those has a test, because
    each is a state the feature would otherwise quietly get wrong in the direction of
    looking like it worked.

The field names asserted here are the other half of the contract the agent's
NicReader/NetworkInventoryReporter assert in C#. Drift between them is not a crash -- it is
a Network tab that quietly shows nothing, and a fleet that cannot be woken.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import wake

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def nic(mac, ipv4="10.4.7.31", prefix=24, kind="wired", **overrides):
    payload = {"mac": mac, "name": "Ethernet", "description": "Intel I219-LM",
               "ipv4": ipv4, "prefix": prefix, "kind": kind, "link_up": True,
               "wake_enabled": True}
    payload.update(overrides)
    return payload


def roster(*entries):
    """`(machine, online, last_seen)` triples in the shape app.backup_machine_roster yields."""
    return [{"machine": m, "online": online, "last_seen": seen}
            for m, online, seen in entries]


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        wake.init_wake_db(db_path)
        wake.init_wake_db(db_path)   # idempotent, like every other init_*_db
        fleet.init_fleet_db(db_path)

        # ==================================================================== addressing
        print("== MAC normalisation ==")
        check("colons, dashes and bare hex all normalise to one spelling",
              wake.normalize_mac("aa:bb:cc:dd:ee:ff")
              == wake.normalize_mac("AA-BB-CC-DD-EE-FF")
              == wake.normalize_mac("aabbccddeeff")
              == "AA:BB:CC:DD:EE:FF")
        check("a short address is refused", wake.normalize_mac("aa:bb:cc") == "")
        check("non-hex is refused", wake.normalize_mac("zz:bb:cc:dd:ee:ff") == "")
        # Hyper-V's placeholder adapters report this. Stored, it looks exactly like a
        # wakeable target while every packet aimed at it goes nowhere.
        check("an all-zero MAC is refused", wake.normalize_mac("00:00:00:00:00:00") == "")
        check("mac_bytes yields the six bytes the magic packet carries",
              wake.mac_bytes("AA:BB:CC:DD:EE:FF") == b"\xaa\xbb\xcc\xdd\xee\xff")

        print("\n== Subnets and broadcast ==")
        check("a /24 groups on its network address",
              wake.subnet_key("10.4.7.31", 24) == "10.4.7.0/24")
        check("two hosts on one /24 share a key",
              wake.subnet_key("10.4.7.31", 24) == wake.subnet_key("10.4.7.200", 24))
        check("the same address on a /16 is a DIFFERENT group",
              wake.subnet_key("10.4.7.31", 16) != wake.subnet_key("10.4.7.31", 24))
        check("broadcast is the top of the range",
              wake.broadcast_for("10.4.7.31", 24) == "10.4.7.255")
        check("and follows the prefix, not the class",
              wake.broadcast_for("10.4.7.31", 20) == "10.4.15.255")
        # The failures that would look plausible rather than obviously wrong:
        check("APIPA is refused, so two machines whose DHCP failed are not 'on one subnet'",
              wake.subnet_key("169.254.10.1", 16) == "")
        check("loopback is refused", wake.subnet_key("127.0.0.1", 8) == "")
        check("a /32 is refused -- no broadcast address and no peers",
              wake.subnet_key("10.4.7.31", 32) == "")
        check("a missing prefix is refused rather than guessed",
              wake.subnet_key("10.4.7.31", None) == "")

        print("\n== The magic packet ==")
        packet = wake.magic_packet("AA:BB:CC:DD:EE:FF")
        check("is 102 bytes", len(packet) == 102)
        check("opens with six 0xFF", packet[:6] == b"\xff" * 6)
        check("then repeats the MAC sixteen times",
              packet[6:] == b"\xaa\xbb\xcc\xdd\xee\xff" * 16)

        # ==================================================================== ingest
        print("\n== Ingest is non-raising and total ==")
        for junk in (None, "", [], {"nics": "not a list"}, {"nics": [None, 7, "x"]},
                     {"nics": [{}]}, {"nics": [{"mac": "nope"}]}, {"fast_startup": "yes"}):
            try:
                wake.record_network(db_path, "PC-JUNK", junk)
                ok = True
            except Exception:
                ok = False
            check(f"a heartbeat carrying {junk!r} does not raise", ok)

        wake.record_network(db_path, "PC-01", {
            "fast_startup": False,
            "nics": [nic("AA:BB:CC:DD:EE:01"),
                     nic("AA:BB:CC:DD:EE:02", kind="wireless", ipv4="10.9.9.9"),
                     nic("AA:BB:CC:DD:EE:03", ipv4="169.254.1.1", prefix=16)],
        })
        network = wake.get_network(db_path, "PC-01")
        check("every adapter with a usable MAC is stored", len(network["nics"]) == 3)
        check("an unusable address is dropped without dropping the adapter",
              [n for n in network["nics"] if n["mac"].endswith(":03")][0]["ipv4"] == "")
        check("the subnet is derived on read, not stored twice",
              [n for n in network["nics"] if n["mac"].endswith(":01")][0]["subnet"]
              == "10.4.7.0/24")
        check("fast_startup round-trips as a real boolean",
              network["fast_startup"] is False)

        wake.record_network(db_path, "PC-01", {"nics": [nic("AA:BB:CC:DD:EE:01")]})
        check("a re-report REPLACES the adapter set (a dock unplugged must disappear)",
              len(wake.get_network(db_path, "PC-01")["nics"]) == 1)

        check("a machine that never reported is reported_at None, not 'unsupported'",
              wake.get_network(db_path, "NEVER-SEEN")["reported_at"] is None)

        # ==================================================================== diagnosis
        print("\n== Wakeability diagnosis ==")
        check("a machine we have never heard from gets no_report and nothing else",
              wake.diagnose(wake.get_network(db_path, "NEVER-SEEN")) == ["no_report"])

        wake.record_network(db_path, "PC-WIFI", {
            "nics": [nic("AA:BB:CC:DD:FF:01", kind="wireless")]})
        check("Wi-Fi only is named as such, not as 'misconfigured'",
              wake.diagnose(wake.get_network(db_path, "PC-WIFI")) == ["wireless_only"])
        check("...and it is not wakeable at all",
              not wake.wakeable_nics(wake.get_network(db_path, "PC-WIFI")))

        wake.record_network(db_path, "PC-NOADDR", {
            "nics": [nic("AA:BB:CC:DD:FF:02", ipv4="", prefix=None)]})
        check("a wired adapter with no address is no_address, not no_wired_nic",
              wake.diagnose(wake.get_network(db_path, "PC-NOADDR")) == ["no_address"])

        wake.record_network(db_path, "PC-OFF", {
            "fast_startup": True,
            "nics": [nic("AA:BB:CC:DD:FF:03", wake_enabled=False)]})
        problems = wake.diagnose(wake.get_network(db_path, "PC-OFF"))
        check("wake-disabled and fast-startup are BOTH reported, not just the first",
              problems == ["wake_disabled", "fast_startup"])

        wake.record_network(db_path, "PC-UNKNOWN", {
            "nics": [nic("AA:BB:CC:DD:FF:04", wake_enabled=None)]})
        check("an unknown wake flag is not an accusation",
              wake.diagnose(wake.get_network(db_path, "PC-UNKNOWN")) == [])
        check("...and it survives ingest as None rather than collapsing to False",
              wake.get_network(db_path, "PC-UNKNOWN")["nics"][0]["wake_enabled"] is None)

        # ==================================================================== relays
        print("\n== Relay selection ==")
        wake.record_network(db_path, "SLEEPER", {"nics": [nic("AA:AA:AA:00:00:01")]})
        wake.record_network(db_path, "PEER-NEAR", {
            "nics": [nic("AA:AA:AA:00:00:02", ipv4="10.4.7.50")]})
        wake.record_network(db_path, "PEER-FAR", {
            "nics": [nic("AA:AA:AA:00:00:03", ipv4="10.99.0.5")]})

        online = {"PEER-FAR": 100}
        check("a peer on a DIFFERENT subnet is never chosen",
              wake.find_relay(db_path, "SLEEPER", online) is None)

        online = {"PEER-NEAR": 100, "PEER-FAR": 200}
        choice = wake.find_relay(db_path, "SLEEPER", online)
        check("the peer sharing the subnet is chosen", choice["relay"] == "PEER-NEAR")
        check("and it is told the broadcast address, not the host address",
              choice["broadcast"] == "10.4.7.255")
        check("and the subnet it was chosen for", choice["subnet"] == "10.4.7.0/24")

        wake.record_network(db_path, "PEER-ALSO", {
            "nics": [nic("AA:AA:AA:00:00:04", ipv4="10.4.7.51")]})
        recent = wake.find_relay(db_path, "SLEEPER", {"PEER-NEAR": 100, "PEER-ALSO": 900})
        check("the most recently heard-from peer wins", recent["relay"] == "PEER-ALSO")
        excluded = wake.find_relay(db_path, "SLEEPER", {"PEER-NEAR": 100, "PEER-ALSO": 900},
                                   exclude=["PEER-ALSO"])
        check("a peer that already failed is skipped on the retry",
              excluded["relay"] == "PEER-NEAR")

        # Two wired adapters, so the hub cannot know which is cabled. Both get a frame.
        wake.record_network(db_path, "DOCKED", {
            "nics": [nic("BB:BB:BB:00:00:01"), nic("BB:BB:BB:00:00:02")]})
        both = wake.find_relay(db_path, "DOCKED", {"PEER-NEAR": 100})
        check("every wakeable MAC on that subnet is sent to, not just the first",
              both["macs"] == ["BB:BB:BB:00:00:01", "BB:BB:BB:00:00:02"])

        check("the target is never its own relay",
              wake.find_relay(db_path, "PEER-NEAR", {"PEER-NEAR": 100}) is None)

        # ==================================================================== requests
        print("\n== Requests: the three answers that are already known ==")
        already = wake.request_wake(db_path, "SLEEPER", requested_by="op@x.com", online=True)
        check("a machine that is already awake is told so, not sent a packet",
              already["status"] == wake.STATUS_ALREADY_AWAKE)
        check("...and that is terminal, not open", already["open"] is False)

        refused = wake.request_wake(db_path, "PC-WIFI", requested_by="op@x.com")
        check("a machine with no wakeable adapter is refused before dispatch",
              refused["status"] == wake.STATUS_UNWAKEABLE)
        check("...with the diagnosis attached rather than a bare 'no'",
              "wireless_only" in refused["error"])

        first = wake.request_wake(db_path, "SLEEPER", requested_by="op@x.com")
        check("an ordinary wake starts pending", first["status"] == wake.STATUS_PENDING)
        again = wake.request_wake(db_path, "SLEEPER", requested_by="other@x.com")
        check("a second ask returns the SAME request rather than queueing a second packet",
              again["id"] == first["id"])

        print("\n== Dispatch ==")
        entries = roster(("SLEEPER", False, 500), ("PEER-NEAR", True, 1000))
        dispatched = wake.dispatch_once(db_path, entries, now=1000,
                                        allow_hub_broadcast=False)
        check("a pending request is handed to a relay", dispatched == 1)
        current = wake.get_request(db_path, first["id"])
        check("...and the request records which peer was asked",
              current["relay"] == "PEER-NEAR")
        check("...and moves to relaying, which is NOT 'sent'",
              current["status"] == wake.STATUS_RELAYING)
        queued = fleet.list_commands(db_path, machine="PEER-NEAR")
        check("the command is queued at the RELAY, not at the sleeping machine",
              len(queued) == 1 and queued[0]["type"] == "wake_machine")
        # Params are read through get_command because list_commands deliberately omits them.
        # They are asserted at all because they are what the relay acts on -- and because
        # they are audited verbatim here, unlike the restore-plan and BIOS-password cases:
        # a MAC is not a secret, and "who woke what, through which machine" is exactly what
        # an auditor wants to read back.
        params = fleet.get_command(db_path, queued[0]["id"])["params"]
        check("...and its params name the target and the broadcast address",
              params["target"] == "SLEEPER" and params["broadcast"] == "10.4.7.255")
        check("...and the target's MAC, which is what the relay actually sends to",
              params["macs"] == ["AA:AA:AA:00:00:01"])

        print("\n== The relay answers ==")
        agent_id, token = fleet.enroll_agent(db_path, "PEER-NEAR", "s3cret", "s3cret")
        claimed = fleet.claim_commands(db_path, agent_id, "PEER-NEAR")
        fleet.complete_command(db_path, claimed[0]["id"], agent_id, True, "sent to ...")
        wake.reconcile_once(db_path, now=1010)
        current = wake.get_request(db_path, first["id"])
        check("a relay that sent the packet moves the request to SENT",
              current["status"] == wake.STATUS_SENT)
        check("...recording that a peer, not the hub, delivered it",
              current["delivery"] == wake.DELIVERY_RELAY)
        check("...which is still an OPEN state -- nothing acknowledges a magic packet",
              current["open"] is True)

        print("\n== Confirmation compares against the packet, not against online-ness ==")
        # The bug this is here to prevent: SLEEPER reads "online" on a last_seen from
        # BEFORE the packet went out, which is true of any machine flapping in and out of
        # the 90-second window. Treating that as a wake would confirm one nobody performed.
        stale = roster(("SLEEPER", True, 900), ("PEER-NEAR", True, 1100))
        confirmed, gave_up = wake.confirm_once(db_path, stale, now=1020)
        check("a check-in that PREDATES the packet does not confirm the wake",
              confirmed == 0
              and wake.get_request(db_path, first["id"])["status"] == wake.STATUS_SENT)

        fresh = roster(("SLEEPER", True, 1015), ("PEER-NEAR", True, 1100))
        confirmed, gave_up = wake.confirm_once(db_path, fresh, now=1020)
        check("a check-in AFTER the packet does", confirmed == 1)
        check("...and only then is the request AWAKE",
              wake.get_request(db_path, first["id"])["status"] == wake.STATUS_AWAKE)

        print("\n== Silence is reported as silence ==")
        quiet = wake.request_wake(db_path, "DOCKED", requested_by="op@x.com", now=2000)
        wake.dispatch_once(db_path, roster(("DOCKED", False, 0), ("PEER-NEAR", True, 2000)),
                           now=2000, allow_hub_broadcast=False)
        agent_id2, _ = fleet.enroll_agent(db_path, "PEER-NEAR", "s3cret", "s3cret")
        for command in fleet.claim_commands(db_path, agent_id2, "PEER-NEAR"):
            fleet.complete_command(db_path, command["id"], agent_id2, True, "sent")
        wake.reconcile_once(db_path, now=2001)
        _c, gave_up = wake.confirm_once(db_path,
                                        roster(("DOCKED", False, 0)),
                                        now=2001 + 400, confirm_timeout=300)
        check("a machine that never checks in becomes NO_ANSWER", gave_up == 1)
        final = wake.get_request(db_path, quiet["id"])
        check("...which is a report of silence, not a claim the packet failed",
              final["status"] == wake.STATUS_NO_ANSWER
              and "has not checked in" in final["error"])

        print("\n== 'No relay' is a first-class outcome, and the waiting is bounded ==")
        waiting = wake.request_wake(db_path, "SLEEPER", requested_by="op@x.com", now=3000,
                                    ttl_seconds=600)
        # Nobody awake at all: the request must SURVIVE, so the first peer to come online
        # serves it. That persistence is the whole point of the scheduled pairing.
        wake.dispatch_once(db_path, roster(("SLEEPER", False, 0)), now=3000,
                           allow_hub_broadcast=False)
        check("with no awake peer the request stays pending rather than failing",
              wake.get_request(db_path, waiting["id"])["status"] == wake.STATUS_PENDING)
        check("nothing expires while it is still inside its deadline",
              wake.expire_stale(db_path, now=3500) == 0)
        check("the deadline is what ends the wait", wake.expire_stale(db_path, now=3700) == 1)
        expired = wake.get_request(db_path, waiting["id"])
        check("...as NO_RELAY, naming the subnet nobody was awake on",
              expired["status"] == wake.STATUS_NO_RELAY and "10.4.7.0/24" in expired["error"])

        print("\n== Cancel is honest about how far the wake got ==")
        cancellable = wake.request_wake(db_path, "SLEEPER", requested_by="op@x.com", now=4000)
        check("a request no relay has seen can be cancelled",
              wake.cancel_request(db_path, cancellable["id"]))
        check("...and lands in a terminal state",
              wake.get_request(db_path, cancellable["id"])["status"] == wake.STATUS_CANCELLED)

        handed = wake.request_wake(db_path, "SLEEPER", requested_by="op@x.com", now=5000)
        wake.dispatch_once(db_path, roster(("SLEEPER", False, 0), ("PEER-NEAR", True, 5000)),
                           now=5000, allow_hub_broadcast=False)
        check("once a relay holds it the packet may already be on the wire, so cancel is "
              "refused rather than lying",
              not wake.cancel_request(db_path, handed["id"]))

        print("\n== A machine that came up on its own is not claimed as a wake ==")
        selfstart = wake.request_wake(db_path, "DOCKED", requested_by="op@x.com", now=6000)
        wake.dispatch_once(db_path, roster(("DOCKED", True, 6000)), now=6000,
                           allow_hub_broadcast=False)
        check("it resolves as ALREADY_AWAKE, not AWAKE -- we did not wake it",
              wake.get_request(db_path, selfstart["id"])["status"]
              == wake.STATUS_ALREADY_AWAKE)

        print("\n== Fleet-wide ==")
        rows = wake.request_many(db_path, ["SLEEPER", "PC-WIFI", "PEER-NEAR", "SLEEPER"],
                                 requested_by="op@x.com", online={"PEER-NEAR"}, now=7000)
        check("duplicates are collapsed", len(rows) == 3)
        check("a machine that cannot be woken still gets a row with its own outcome",
              any(r["machine"] == "PC-WIFI" and r["status"] == wake.STATUS_UNWAKEABLE
                  for r in rows))
        check("...and so does one that is already awake",
              any(r["machine"] == "PEER-NEAR" and r["status"] == wake.STATUS_ALREADY_AWAKE
                  for r in rows))
        try:
            wake.request_many(db_path, [f"PC-{i}" for i in range(wake.MAX_MACHINES_PER_REQUEST + 1)],
                              requested_by="op@x.com")
            bounded = False
        except wake.WakeRejected:
            bounded = True
        check("an absurdly large fleet request is refused, not attempted", bounded)

        print("\n== Machine lifecycle ==")
        wake.record_network(db_path, "PC-BYE", {"nics": [nic("CC:CC:CC:00:00:01")]})
        gone = wake.request_wake(db_path, "PC-BYE", requested_by="op@x.com", now=8000)
        wake.forget_machine(db_path, "PC-BYE")
        check("a deleted machine's adapters go -- a stale NIC keeps it a candidate RELAY",
              wake.get_network(db_path, "PC-BYE")["nics"] == [])
        check("...and its wake history goes with it",
              wake.get_request(db_path, gone["id"]) is None)

        wake.record_network(db_path, "MERGE-OLD", {"nics": [nic("CC:CC:CC:00:00:02")]})
        moved = wake.request_wake(db_path, "MERGE-OLD", requested_by="op@x.com", now=8100)
        wake.rename_machine(db_path, "MERGE-OLD", "PC-01")
        check("a merge moves the wake history to the survivor",
              wake.get_request(db_path, moved["id"])["machine"] == "PC-01")
        check("...and the survivor's own adapters win, being its own newer reading",
              [n["mac"] for n in wake.get_network(db_path, "PC-01")["nics"]]
              == ["AA:BB:CC:DD:EE:01"])

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
