"""processes.py -- the machine Processes card's snapshot store, watch and command params.

The three things that can genuinely hurt here, and what each is tested against:

  * **PID reuse.** Every snapshot is seconds old by the time somebody clicks End task, and
    Windows recycles process ids within minutes. The (name, pid) pairing is the guard, so the
    validators are tested for refusing a bare pid at all -- an endpoint that accepted one
    would be an endpoint for killing a process at random.

  * **Ingest arrives on the HEARTBEAT.** A heartbeat that 500s takes the machine offline
    fleet-wide, so nothing a machine can send may raise, and a bad report must not be allowed
    to overwrite a good one -- a blanked card on one malformed scan is a card an operator
    stops trusting.

  * **The watch is what stops the fleet doing this work.** Sampling costs the managed machine
    real CPU and the payload is ~60 KB; both are affordable only because they happen while
    somebody is looking and not otherwise. So the TTL is tested as a real expiry, not as a
    flag somebody remembers to clear.

The field names asserted here are the other half of the contract the agent's
ProcessReader/ProcessReporter assert in C#. Drift between them is not a crash -- it is a
Processes card that quietly shows nothing.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import processes

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


def proc(pid, name, cpu=1.0, mem=100.0, **overrides):
    payload = {"pid": pid, "name": name, "cpu_pct": cpu, "mem_mb": mem,
               "user": "CORP\\alice", "session": 1,
               "path": f"C:\\Program Files\\{name}", "started_at": 1754000000}
    payload.update(overrides)
    return payload


def report(*entries, **extra):
    payload = {"processes": list(entries), "cpu_cores": 8, "mem_total_mb": 16384.0,
               "sample_ms": 5000, "truncated": 0, "captured_at": 1754000100}
    payload.update(extra)
    return payload


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        processes.init_processes_db(db_path)
        processes.init_processes_db(db_path)   # idempotent, like every other init_*_db

        # ================================================================ name normalization
        print("\n== Names are compared one way, or a guard can be dodged by spelling ==")
        check("the .exe is stripped", processes.normalize_name("LSASS.EXE") == "lsass")
        check("case is folded", processes.normalize_name("  CsrSS  ") == "csrss")
        check("None is not a process", processes.normalize_name(None) == "")
        check("lsass.exe is protected", processes.is_protected("lsass.exe"))
        check("...and so is plain lsass", processes.is_protected("lsass"))
        check("svchost is protected -- killing one takes every service in it down",
              processes.is_protected("svchost.exe"))
        check("the agent will not offer to end itself",
              processes.is_protected("TempMonitorAgent.exe"))
        check("an ordinary program is not", not processes.is_protected("chrome.exe"))
        # explorer.exe is deliberately NOT protected: ending it is a real helpdesk action and
        # Windows brings it straight back.
        check("explorer is killable on purpose", not processes.is_protected("explorer.exe"))

        # ================================================================ ingest
        print("\n== A report from a machine is trimmed, never trusted ==")
        check("a good report is stored",
              processes.record_snapshot(db_path, "PC-1",
                                        report(proc(4812, "chrome"), proc(900, "svchost"))))
        snap = processes.get_snapshot(db_path, "PC-1")
        check("both processes came back", len(snap["processes"]) == 2)
        check("the interpretive facts survive with them",
              snap["cpu_cores"] == 8 and snap["sample_ms"] == 5000)
        by_name = {p["name"]: p for p in snap["processes"]}
        check("the protected flag is the HUB's answer, not the agent's",
              by_name["svchost"]["protected"] is True
              and by_name["chrome"]["protected"] is False)

        long_name = "a" * 400
        processes.record_snapshot(db_path, "PC-1", report(
            proc(1, long_name, path="C:\\" + "b" * 900, user="d" * 400,
                 services=["svc" + str(i) for i in range(50)])))
        entry = processes.get_snapshot(db_path, "PC-1")["processes"][0]
        check("an over-long name is cut, not stored whole",
              len(entry["name"]) == processes.MAX_NAME_CHARS)
        check("...and so is the path", len(entry["path"]) == processes.MAX_PATH_CHARS)
        check("...and the user", len(entry["user"]) == processes.MAX_USER_CHARS)
        check("...and the service list",
              len(entry["services"]) == processes.MAX_SERVICES_PER_PROCESS)

        print("\n== Nothing a machine can send may raise, or a heartbeat 500s ==")
        for junk in ("not a dict", 7, None, {}, {"processes": "nope"}, {"processes": {}},
                     {"processes": [1, 2, 3]}, {"processes": [{"pid": "x"}]},
                     {"processes": [{"name": "no pid"}]},
                     {"processes": [proc(1, "ok")], "truncated": "lots"},
                     {"processes": [proc(1, "ok", cpu=float("nan"))]},
                     {"processes": [proc(1, "ok", cpu="hot", mem=None)]}):
            try:
                processes.record_snapshot(db_path, "PC-2", junk)
                ok = True
            except Exception as e:
                ok = False
                print(f"       raised {e!r}")
            check(f"record_snapshot({str(junk)[:40]!r}) does not raise", ok)

        # NaN survives float() and json.dumps writes it as a bare NaN, which no browser's
        # JSON.parse will accept -- one odd reading would blank the whole card.
        processes.record_snapshot(db_path, "PC-NAN",
                                  report(proc(5, "weird", cpu=float("nan"))))
        raw = processes.get_snapshot(db_path, "PC-NAN")
        check("a NaN reading is stored as null, so the payload stays parseable JSON",
              raw["processes"][0]["cpu_pct"] is None)
        check("...and the whole snapshot round-trips through json",
              json.loads(json.dumps(raw))["processes"][0]["name"] == "weird")

        print("\n== A bad report must not overwrite a good one ==")
        processes.record_snapshot(db_path, "PC-3", report(proc(11, "outlook")))
        check("an empty process list is refused rather than stored",
              processes.record_snapshot(db_path, "PC-3", report()) is False)
        check("...so the last good list is still there",
              processes.get_snapshot(db_path, "PC-3")["processes"][0]["name"] == "outlook")

        print("\n== The cap is counted, not silently applied ==")
        many = [proc(i + 1, f"proc{i}") for i in range(processes.MAX_PROCESSES + 25)]
        processes.record_snapshot(db_path, "PC-BIG", report(*many, truncated=7))
        big = processes.get_snapshot(db_path, "PC-BIG")
        check("no more than the cap is stored",
              len(big["processes"]) == processes.MAX_PROCESSES)
        check("what the machine dropped and what the hub dropped are ONE number to a reader",
              big["truncated"] == 7 + 25)

        # ================================================================ staleness
        print("\n== A machine that never reported is a different state from a stale one ==")
        never = processes.get_snapshot(db_path, "PC-NEVER")
        check("an unknown machine still answers", never["processes"] == [])
        check("...saying it has never reported, rather than erroring",
              never["reported_at"] is None and never["age_seconds"] is None)
        check("...and that counts as stale, so nothing renders it as live",
              never["stale"] is True)

        processes.record_snapshot(db_path, "PC-4", report(proc(1, "notepad")), now=1000)
        fresh = processes.get_snapshot(db_path, "PC-4", now=1005)
        check("a recent report is not stale", fresh["stale"] is False and fresh["age_seconds"] == 5)
        old = processes.get_snapshot(db_path, "PC-4",
                                     now=1000 + processes.STALE_AFTER_SECONDS + 1)
        check("...and one past the horizon is", old["stale"] is True)
        # Age is measured on the hub's receipt, never on the agent's captured_at: a machine
        # with a skewed clock would otherwise present a snapshot from next Tuesday.
        processes.record_snapshot(db_path, "PC-SKEW",
                                  report(proc(1, "notepad"), captured_at=99999999999), now=1000)
        skewed = processes.get_snapshot(db_path, "PC-SKEW", now=1010)
        check("a machine with a wrong clock is still aged against the hub's own receipt",
              skewed["age_seconds"] == 10)

        # ================================================================ the watch
        print("\n== The watch is what keeps the fleet from doing this work ==")
        check("nobody is watching by default",
              processes.is_watched(db_path, "PC-1", now=1000) is False)
        processes.note_watch(db_path, "PC-1", watcher="tech@x.com", now=1000)
        check("a console read turns it on", processes.is_watched(db_path, "PC-1", now=1000))
        check("...and it survives a couple of missed polls",
              processes.is_watched(db_path, "PC-1",
                                   now=1000 + processes.WATCH_TTL_SECONDS - 1))
        check("...but LAPSES on its own -- there is no goodbye message to depend on",
              processes.is_watched(db_path, "PC-1",
                                   now=1000 + processes.WATCH_TTL_SECONDS + 1) is False)
        processes.note_watch(db_path, "PC-1", now=2000)
        check("polling again renews it",
              processes.is_watched(db_path, "PC-1", now=2000 + 10))
        processes.clear_watch(db_path, "PC-1")
        check("and it can be dropped outright",
              processes.is_watched(db_path, "PC-1", now=2000 + 10) is False)

        processes.note_watch(db_path, "PC-OLD", now=100)
        processes.note_watch(db_path, "PC-NEW", now=100000)
        check("pruning drops the lapsed row",
              processes.prune_watches(db_path, now=100000) == 1)
        check("...and leaves the live one alone",
              processes.is_watched(db_path, "PC-NEW", now=100000))

        # ================================================================ command params
        print("\n== A pid alone is never enough to name what should die ==")
        params = processes.validate_kill("chrome.exe", [4812, 4907])
        check("a well-formed kill keeps both halves",
              params == {"name": "chrome.exe", "pids": [4812, 4907], "tree": False})
        check("one pid may be passed bare",
              processes.validate_kill("chrome", 4812)["pids"] == [4812])
        check("duplicates collapse",
              processes.validate_kill("chrome", [7, 7, 7])["pids"] == [7])
        check("tree is carried through",
              processes.validate_kill("chrome", [7], tree=True)["tree"] is True)

        def refuses(fn, label):
            try:
                fn()
                check(label, False)
            except ValueError:
                check(label, True)

        refuses(lambda: processes.validate_kill("", [1]), "a kill with no name is refused")
        refuses(lambda: processes.validate_kill("chrome", []), "...and one with no pid")
        refuses(lambda: processes.validate_kill("chrome", "all"),
                "...and one whose pids are not pids")
        refuses(lambda: processes.validate_kill("chrome", [0]), "...and pid 0")
        # bool is an int in Python; without a guard `{"pids": true}` is pid 1.
        refuses(lambda: processes.validate_kill("chrome", True),
                "...and a body whose pids are `true`")
        refuses(lambda: processes.validate_restart("chrome", True),
                "...and a restart whose pid is `true`")
        refuses(lambda: processes.validate_kill("chrome", [-3]), "...and a negative pid")
        refuses(lambda: processes.validate_kill("chrome", list(range(1, 200))),
                "...and a request to end two hundred processes at once")
        refuses(lambda: processes.validate_kill("lsass.exe", [700]),
                "ending lsass is refused before it can ever become a command")
        refuses(lambda: processes.validate_kill("svchost", [900]),
                "...and so is ending a service host")

        print("\n== Restart names ONE process, because that is all it can mean ==")
        check("a well-formed restart keeps the pairing",
              processes.validate_restart("spoolsv.exe", 1234)
              == {"name": "spoolsv.exe", "pid": 1234})
        refuses(lambda: processes.validate_restart("chrome", 0), "pid 0 is refused")
        refuses(lambda: processes.validate_restart("", 12), "a nameless restart is refused")
        refuses(lambda: processes.validate_restart("chrome", "4812x"),
                "...and a pid that is not a number")
        # Deliberately allowed: restarting a service host is the ONE useful thing to do with
        # one, and the agent turns it into a proper service stop/start rather than a kill.
        check("restarting a service host is allowed where ending it is not",
              processes.validate_restart("svchost.exe", 900)["pid"] == 900)

        # ================================================================ deletion
        print("\n== A deleted machine leaves nothing behind ==")
        processes.record_snapshot(db_path, "PC-GONE", report(proc(1, "chrome")))
        processes.note_watch(db_path, "PC-GONE")
        processes.forget_machine(db_path, "PC-GONE")
        check("its snapshot is gone",
              processes.get_snapshot(db_path, "PC-GONE")["reported_at"] is None)
        check("...and so is its watch", not processes.is_watched(db_path, "PC-GONE"))

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
