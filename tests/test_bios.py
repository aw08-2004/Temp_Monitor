"""bios.py -- the firmware inventory model (roadmap #9).

Everything here is about the ingest boundary, because that is where this feature can hurt:
the payload arrives on the HEARTBEAT, from a machine, and a heartbeat that 500s takes that
machine offline fleet-wide. So the rules under test are (1) nothing a machine can send
raises, (2) the three support states stay three and never collapse into each other, and
(3) what comes back out is what a console can render without guessing.

The field names asserted below are the other half of the contract the agent's
BiosReaderTests asserts in C#. Drift between them is not a crash -- it is a Firmware tab
that quietly shows nothing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import bios

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


def supported_payload(**overrides):
    payload = {
        "support": "supported",
        "vendor": "Dell",
        "interface": r"root\dcim\sysman",
        "bios_version": "1.29.0",
        "password_set": True,
        "error": "",
        "settings": [
            {"name": "WakeOnLan", "value": "LanOnly", "kind": "enum",
             "possible_values": ["Disabled", "LanOnly"], "read_only": False,
             "display_name": "Wake on LAN"},
            {"name": "AutoOnHr", "value": "7", "kind": "integer",
             "possible_values": [], "read_only": False, "display_name": ""},
        ],
    }
    payload.update(overrides)
    return payload


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        bios.init_bios_db(db_path)
        bios.init_bios_db(db_path)   # idempotent, like every other init_*_db

        print("\n== A machine that has never reported ==")
        empty = bios.get_inventory(db_path, "PC-NEW")
        # Deliberately a FOURTH value at the read boundary. Before the agent release that
        # collects this, every machine in the fleet is here -- rendering that as
        # "unsupported" would write off the whole fleet on the strength of no evidence.
        check("unknown machine -> support is None, not 'unsupported'", empty["support"] is None)
        check("unknown machine -> empty settings", empty["settings"] == [])
        check("unknown machine -> no reported_at", empty["reported_at"] is None)

        print("\n== A supported machine ==")
        check("record_inventory returns True",
              bios.record_inventory(db_path, "PC-01", supported_payload()) is True)
        got = bios.get_inventory(db_path, "PC-01")
        check("support round-trips", got["support"] == bios.SUPPORT_SUPPORTED)
        check("vendor round-trips", got["vendor"] == "Dell")
        check("interface round-trips", got["interface"] == r"root\dcim\sysman")
        check("bios_version round-trips", got["bios_version"] == "1.29.0")
        check("password_set is a real bool", got["password_set"] is True)
        check("reported_at is stamped", isinstance(got["reported_at"], int))
        # Sorted casefolded so the tab is stable between reports -- WMI returns rows in
        # whatever order it likes, and a list that reshuffles every six hours is unreadable.
        check("settings are sorted by name",
              [s["name"] for s in got["settings"]] == ["AutoOnHr", "WakeOnLan"])
        wol = [s for s in got["settings"] if s["name"] == "WakeOnLan"][0]
        check("possible_values survive", wol["possible_values"] == ["Disabled", "LanOnly"])
        check("display_name survives", wol["display_name"] == "Wake on LAN")
        check("read_only survives as a bool", wol["read_only"] is False)

        print("\n== A second report replaces the first ==")
        bios.record_inventory(db_path, "PC-01", supported_payload(settings=[
            {"name": "WakeOnLan", "value": "Disabled", "kind": "enum",
             "possible_values": ["Disabled", "LanOnly"], "read_only": False},
        ]))
        got = bios.get_inventory(db_path, "PC-01")
        check("upsert replaces rather than appends", len(got["settings"]) == 1)
        check("the new value is stored", got["settings"][0]["value"] == "Disabled")

        print("\n== Unsupported is not an error ==")
        bios.record_inventory(db_path, "PC-VM", {
            "support": "unsupported", "error": "no firmware interface for VMware, Inc.",
        })
        vm = bios.get_inventory(db_path, "PC-VM")
        check("unsupported round-trips", vm["support"] == bios.SUPPORT_UNSUPPORTED)
        check("unsupported keeps its reason", "VMware" in vm["error"])
        # password_set is null, not false: nobody asked the firmware anything.
        check("unsupported leaves password_set unknown", vm["password_set"] is None)

        print("\n== Error keeps the machine's own message ==")
        bios.record_inventory(db_path, "PC-BAD", {
            "support": "error", "vendor": "Dell", "interface": r"root\dcim\sysman",
            "error": "access denied",
        })
        bad = bios.get_inventory(db_path, "PC-BAD")
        check("error round-trips", bad["support"] == bios.SUPPORT_ERROR)
        check("error message round-trips", bad["error"] == "access denied")

        print("\n== The ingest boundary: nothing a machine sends may raise ==")
        for junk in (None, "supported", 42, [], {"support": None},
                     {"support": "supported", "settings": "not a list"},
                     {"support": "supported", "settings": [None, 7, "x"]}):
            try:
                bios.record_inventory(db_path, "PC-JUNK", junk)
                ok = True
            except Exception:
                ok = False
            check(f"junk payload {junk!r} does not raise", ok)

        print("\n== Support state is normalised, never stored verbatim ==")
        bios.record_inventory(db_path, "PC-ODD", {"support": "probably?", "settings": []})
        odd = bios.get_inventory(db_path, "PC-ODD")
        # An agent describing a state we do not understand HAS an interface and is failing to
        # describe it. Guessing "unsupported" would file a real fault under the state nobody
        # ever looks at.
        check("an unknown support state becomes 'error'", odd["support"] == bios.SUPPORT_ERROR)

        bios.record_inventory(db_path, "PC-EMPTY", {"support": "supported", "settings": []})
        check("'supported' with zero settings is an error, not a support claim",
              bios.get_inventory(db_path, "PC-EMPTY")["support"] == bios.SUPPORT_ERROR)

        print("\n== Attribute cleaning ==")
        bios.record_inventory(db_path, "PC-CLEAN", {
            "support": "supported",
            "settings": [
                {"name": "", "value": "x"},                       # nameless -> dropped
                {"name": "  Padded  ", "value": "  v  "},         # trimmed
                {"name": "Weird", "value": None, "kind": "wat",
                 "possible_values": ["a", "a", "b", None]},
                {"name": "Long", "value": "y" * 5000},
                "not a dict",
            ],
        })
        clean = {s["name"]: s for s in bios.get_inventory(db_path, "PC-CLEAN")["settings"]}
        check("a nameless attribute is dropped", "" not in clean)
        check("a non-dict attribute is dropped", len(clean) == 3)
        check("names are trimmed", "Padded" in clean)
        check("a null value becomes empty text, not 'None'", clean["Weird"]["value"] == "")
        check("an unknown kind falls back to 'unknown'", clean["Weird"]["kind"] == "unknown")
        check("duplicate/empty possible values are collapsed",
              clean["Weird"]["possible_values"] == ["a", "b"])
        check("an oversized value is truncated, not rejected",
              len(clean["Long"]["value"]) == bios.MAX_VALUE_CHARS)

        print("\n== Caps bound what one machine can store ==")
        bios.record_inventory(db_path, "PC-HUGE", {
            "support": "supported",
            "settings": [{"name": f"Attr{i:05d}", "value": "v"} for i in range(3000)],
        })
        check("attribute count is capped",
              len(bios.get_inventory(db_path, "PC-HUGE")["settings"]) == bios.MAX_ATTRIBUTES)

        print("\n== Machine lifecycle hooks ==")
        bios.forget_machine(db_path, "PC-VM")
        check("forget_machine drops the row",
              bios.get_inventory(db_path, "PC-VM")["support"] is None)

        bios.record_inventory(db_path, "OLD-NAME", supported_payload(vendor="Lenovo"))
        bios.rename_machine(db_path, "OLD-NAME", "PC-FRESH")
        check("rename moves an inventory to a name with none",
              bios.get_inventory(db_path, "PC-FRESH")["vendor"] == "Lenovo")
        check("the old name is gone",
              bios.get_inventory(db_path, "OLD-NAME")["support"] is None)

        bios.record_inventory(db_path, "DUPE", supported_payload(vendor="HP"))
        bios.rename_machine(db_path, "DUPE", "PC-01")
        # The survivor has already reported for itself, on the same hardware; its own reading
        # is newer than the one being folded in.
        check("a merge keeps the survivor's own reading",
              bios.get_inventory(db_path, "PC-01")["vendor"] == "Dell")
        check("the merged-away name is dropped",
              bios.get_inventory(db_path, "DUPE")["support"] is None)

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
