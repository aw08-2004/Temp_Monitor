"""discovery.py -- network discovery and shadow IT (roadmap #18).

Four silent failures this file exists to catch, all of them in the direction of a shadow-IT
list that is quietly wrong and therefore stops being read:

  * **A sweep aimed off-segment answers with nothing, which looks exactly like a clean
    network.** ARP does not cross a router, so a subnet the relay is not on cannot be
    swept -- and the failure is not an error, it is an empty result. `request_scan` has to
    refuse it before it becomes a reassuring screenful of nothing.

  * **Classification is computed at READ time, against the fleet as it stands now.** A
    machine enrolled an hour after a sweep must turn that scan's "unknown device" into
    itself. Storing the verdict would freeze the fleet as it was when the packet went out,
    and a stale unmanaged row is the one thing that teaches a helpdesk to ignore this card.

  * **A relay must only be able to answer for itself.** The endpoint authenticates the
    agent; `record_results` is the half that checks the scan it is answering is its own.
    Without it any enrolled machine could attribute a fabricated network to another site.

  * **A sweep that found nothing and a sweep that never reported are different facts**, and
    only one of them is a finding. The first is `done` with no hosts; the second has to
    become `failed` with a sentence, or it sits at `scanning` for ever and reads as a slow
    network.

The field names asserted here are the other half of the contract
Network/NetworkSweepExecutor.cs writes in C#. Drift between them is not a crash -- it is a
Discovery card that silently shows an empty subnet.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import discovery
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


def nic(mac, ipv4="10.4.7.31", prefix=24, kind="wired"):
    return {"mac": mac, "name": "Ethernet", "description": "Intel I219-LM",
            "ipv4": ipv4, "prefix": prefix, "kind": kind, "link_up": True,
            "wake_enabled": True}


def host(ip, mac="", hostname=""):
    return {"ip": ip, "mac": mac, "hostname": hostname}


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        discovery.init_discovery_db(db_path)
        discovery.init_discovery_db(db_path)   # idempotent, like every other init_*_db
        wake.init_wake_db(db_path)
        fleet.init_fleet_db(db_path)

        # ==================================================================== subnets
        print("\n-- which subnets a machine may sweep --")
        wake.record_network(db_path, "PC-01", {"nics": [nic("AA:BB:CC:DD:EE:01")]})
        check("a machine's own subnet is sweepable",
              discovery.sweepable_subnets(db_path, "PC-01") == ["10.4.7.0/24"])
        check("a machine that never reported has nothing to sweep",
              discovery.sweepable_subnets(db_path, "PC-99") == [])

        # Wireless counts here and does NOT count for a wake -- the one place these two
        # features read the same table differently. A laptop on the office WLAN is often the
        # only machine awake on that segment, and reusing wake's filter would have made half
        # the fleet unable to look at the network it is sitting on.
        wake.record_network(db_path, "LAPTOP-7", {
            "nics": [nic("AA:BB:CC:DD:EE:07", ipv4="10.4.9.14", kind="wireless")]})
        check("a wireless-only machine can still sweep, unlike being woken",
              discovery.sweepable_subnets(db_path, "LAPTOP-7") == ["10.4.9.0/24"])
        check("...and wake still refuses to call it wakeable",
              wake.wakeable_nics(wake.get_network(db_path, "LAPTOP-7")) == [])

        # A /8 is 16 million probes. The cap keeps it off the picker entirely rather than
        # refusing it after somebody chose it.
        wake.record_network(db_path, "BIG-1", {
            "nics": [nic("AA:BB:CC:DD:EE:B1", ipv4="10.0.0.5", prefix=8)]})
        check("a subnet wider than the cap is not offered at all",
              discovery.sweepable_subnets(db_path, "BIG-1") == [])
        # wake.subnet_key already rejects /0 and /32 while accepting reports. Exercise
        # discovery's own acceptance boundary too, so it cannot start offering a range the
        # agent refuses if another network source later bypasses wake's parser.
        real_get_network = discovery.wake.get_network
        real_subnet_key = discovery.wake.subnet_key
        try:
            discovery.wake.get_network = lambda *_: {"nics": [
                {"ipv4": "10.4.10.6", "prefix": 31},
                {"ipv4": "10.4.10.7", "prefix": 32},
                {"ipv4": "10.4.10.7", "prefix": 0},
            ]}
            discovery.wake.subnet_key = lambda ipv4, prefix: f"{ipv4}/{prefix}"
            check("only ranges the agent can sweep are offered",
                  discovery.sweepable_subnets(db_path, "BOUNDARY-1") == ["10.4.10.6/31"])
        finally:
            discovery.wake.get_network = real_get_network
            discovery.wake.subnet_key = real_subnet_key

        # ==================================================================== requesting
        print("\n-- what a sweep will and will not be aimed at --")
        try:
            discovery.request_scan(db_path, "PC-01", "10.4.7.0/24",
                                   requested_by="op@x.com", online=False)
            check("an offline machine is refused", False)
        except discovery.DiscoveryRejected as e:
            check("an offline machine is refused, and the reason says why",
                  "offline" in str(e))

        # The bound that makes this feature un-aimable. The roadmap's own worry is that a
        # discovery sweep is a scanner pointed at a colleague's network; this is the answer.
        try:
            discovery.request_scan(db_path, "PC-01", "192.168.50.0/24",
                                   requested_by="op@x.com", online=True)
            check("a subnet the machine is not on is refused", False)
        except discovery.DiscoveryRejected as e:
            check("a subnet the machine is not on is refused",
                  "not on 192.168.50.0/24" in str(e))

        scan = discovery.request_scan(db_path, "PC-01", "10.4.7.0/24",
                                      requested_by="op@x.com", online=True)
        check("a sweep of the machine's own subnet is recorded",
              scan["status"] == discovery.STATUS_SCANNING)

        try:
            discovery.request_scan(db_path, "PC-01", "10.4.7.0/24",
                                   requested_by="op@x.com", online=True)
            check("a second concurrent sweep is refused", False)
        except discovery.DiscoveryRejected as e:
            check("a second concurrent sweep is refused",
                  "already running" in str(e))

        # ==================================================================== ingest
        print("\n-- taking a relay's answer --")
        check("another machine cannot answer this scan",
              discovery.record_results(db_path, scan["id"], "PC-02",
                                       {"hosts": [host("10.4.7.99")]}) is False)
        check("...and nothing it sent was stored",
              discovery.scan_hosts(db_path, scan["id"]) == [])

        # Duplicated address, malformed address, and an entry that is not a dict at all: a
        # lenient parse here is how a truncated body silently reports an emptier network
        # than the relay found, which is the finding this feature would then get wrong.
        check("the owning machine's report is taken",
              discovery.record_results(db_path, scan["id"], "PC-01", {
                  "probed": 254,
                  "hosts": [host("10.4.7.31", "AA:BB:CC:DD:EE:01", "pc-01.example"),
                            host("10.4.7.50", "11:22:33:44:55:66", "printer"),
                            host("10.4.7.50", "99:99:99:99:99:99"),
                            host("not-an-address", "11:22:33:44:55:77"),
                            "nonsense"]}) is True)
        hosts = discovery.scan_hosts(db_path, scan["id"])
        check("a repeated address is stored once", len(hosts) == 2)
        check("an unusable address is dropped rather than failing the whole report",
              [h["ip"] for h in hosts] == ["10.4.7.31", "10.4.7.50"])
        check("the probe count the relay reported is kept",
              discovery.get_scan(db_path, scan["id"])["probed"] == 254)

        # ==================================================================== classifying
        print("\n-- managed, unmanaged, and the machine doing the looking --")
        by_ip = {h["ip"]: h for h in hosts}
        check("the relay's own address is not counted as a find",
              by_ip["10.4.7.31"]["classification"] == discovery.CLASS_RELAY)
        check("an unrecognised MAC is unmanaged",
              by_ip["10.4.7.50"]["classification"] == discovery.CLASS_UNMANAGED)

        counts = discovery.count_by_class(hosts)
        check("every class is present with a zero rather than omitted",
              set(counts) == set(discovery.CLASSIFICATIONS))
        check("the unmanaged count is what the card reads", counts["unmanaged"] == 1)

        # The reason the verdict is not stored: enrolling the printer's owner an hour later
        # has to change what that scan says about it, because it IS managed now.
        wake.record_network(db_path, "PRINTER-2", {
            "nics": [nic("11:22:33:44:55:66", ipv4="10.4.7.50")]})
        later = {h["ip"]: h for h in discovery.scan_hosts(db_path, scan["id"])}
        check("a host enrolled after the sweep reclassifies itself",
              later["10.4.7.50"]["classification"] == discovery.CLASS_MANAGED)
        check("...and names the machine it turned out to be",
              later["10.4.7.50"]["machine"] == "PRINTER-2")

        # And the other direction: removing a machine has to turn its address back into a
        # finding, which is the one thing a shadow-IT list must not get wrong.
        wake.forget_machine(db_path, "PRINTER-2")
        discovery.forget_machine(db_path, "PRINTER-2")
        back = {h["ip"]: h for h in discovery.scan_hosts(db_path, scan["id"])}
        check("a machine removed from the fleet becomes unmanaged again",
              back["10.4.7.50"]["classification"] == discovery.CLASS_UNMANAGED)
        check("...and PC-01's own scan survives another machine being forgotten",
              discovery.get_scan(db_path, scan["id"]) is not None)

        check("an empty MAC is a finding, not a dropped row",
              discovery.classify("", "PC-01", {})[0] == discovery.CLASS_UNMANAGED)

        # ==================================================================== a quiet VLAN
        print("\n-- nothing found is an answer --")
        empty = discovery.request_scan(db_path, "LAPTOP-7", "10.4.9.0/24",
                                       requested_by="op@x.com", online=True)
        discovery.record_results(db_path, empty["id"], "LAPTOP-7", {"hosts": [], "probed": 254})
        done = discovery.get_scan(db_path, empty["id"])
        check("a sweep that found nothing completes rather than staying open",
              done["status"] == discovery.STATUS_DONE)
        check("...with no hosts and no error",
              discovery.scan_hosts(db_path, empty["id"]) == [] and done["error"] == "")

        # ==================================================================== reconciling
        print("\n-- the two silences --")
        wake.record_network(db_path, "PC-03", {
            "nics": [nic("AA:BB:CC:DD:EE:03", ipv4="10.4.7.33")]})
        old = discovery.request_scan(db_path, "PC-03", "10.4.7.0/24",
                                     requested_by="op@x.com", online=True,
                                     ttl_seconds=600, now=1000)
        command_id = fleet.create_command(db_path, machine="PC-03",
                                          command_type=discovery.COMMAND_TYPE,
                                          params={"scan_id": old["id"],
                                                  "subnet": "10.4.7.0/24"},
                                          issued_by="op@x.com")
        discovery.attach_command(db_path, old["id"], command_id)
        discovery.reconcile_once(db_path, now=1100)
        check("a sweep still in flight is left alone",
              discovery.get_scan(db_path, old["id"])["status"] == discovery.STATUS_SCANNING)

        # An agent too old for the executor fails the command with its own words, and those
        # words are the whole reason the console has no agent-version gate on this card.
        fleet.claim_commands(db_path, "agent-3", "PC-03")
        fleet.complete_command(db_path, command_id, "agent-3", success=False,
                               output="unknown command type: network_sweep")
        discovery.reconcile_once(db_path, now=1200)
        failed = discovery.get_scan(db_path, old["id"])
        check("a failed command fails the scan", failed["status"] == discovery.STATUS_FAILED)
        check("...carrying the agent's own sentence, not one of ours",
              failed["error"] == "unknown command type: network_sweep")

        lost = discovery.request_scan(db_path, "PC-03", "10.4.7.0/24",
                                      requested_by="op@x.com", online=True,
                                      ttl_seconds=600, now=2000)
        discovery.reconcile_once(db_path, now=2700)
        gone = discovery.get_scan(db_path, lost["id"])
        check("a report that never arrived is failed rather than left open",
              gone["status"] == discovery.STATUS_FAILED)
        check("...and says so, instead of blaming the machine for failing",
              "never sent its results back" in gone["error"])

        # ==================================================================== lifecycle
        print("\n-- renames and removals --")
        discovery.rename_machine(db_path, "PC-03", "PC-03-NEW")
        check("a merge moves the sweep history to the survivor",
              discovery.get_scan(db_path, old["id"])["machine"] == "PC-03-NEW")
        discovery.forget_machine(db_path, "PC-03-NEW")
        check("a deleted machine's scans go with it",
              discovery.get_scan(db_path, old["id"]) is None)
        check("...and its hosts go too",
              discovery.scan_hosts(db_path, old["id"]) == [])

        # ==================================================================== favorites
        print("\n-- a sweep is not favoritable --")
        check("network_sweep is a known command type",
              discovery.COMMAND_TYPE in fleet.ALL_COMMANDS)
        try:
            fleet.create_favorite(db_path, email="op@x.com",
                                  name="sweep the office",
                                  command_type=discovery.COMMAND_TYPE,
                                  params={"scan_id": "x", "subnet": "10.4.7.0/24"})
            check("saving a sweep as a favorite is refused", False)
        except ValueError as e:
            check("saving a sweep as a favorite is refused, naming the subnet reason",
                  "subnet" in str(e))

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
