"""wipe.py -- remote lock and remote wipe (roadmap #23 phase H).

**The silent failure this file exists to catch is a confirmation that only exists in
JavaScript.** The console asks an operator to type the machine's name before it will enable the
Erase button, and that is a courtesy. The control is `confirm_wipe`, here, on the server -- a
dialog stops an operator and does not stop a script, a copied curl command, or a second console
written later. If this function ever became lenient, every test of the page would still pass and
the only symptom would be the wrong device erased.

The comparison is EXACT and case-sensitive on purpose, and that is asserted rather than assumed:
`PHONE-1` and `phone-12` are two machines, and the whole value of the step is that it cannot be
passed by somebody who is not looking at the name in front of them.

The second is the request row itself. A wiped device never reports a result -- it does not
heartbeat again and does not exist to be asked -- so its command row sits at "sent" forever and
the console shows a machine that simply went quiet, indistinguishable from a flat battery. This
table is the only thing that can say "wiped on the 9th, by X" about a device that will never
speak again, which is why it is recorded BEFORE the command is created rather than derived from
one afterwards.

And the third is `reset_protection`, stored per request rather than as a hub setting. Leaving
Android's factory-reset protection on means the wiped device cannot be set up again without the
account that was on it -- theft protection when it was stolen, a self-inflicted brick when it was
a company handset. The answer differs per device, so it travels with the request.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import wipe

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


def refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except wipe.WipeRefused:
        return True
    return False


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        wipe.init_wipe_db(db_path)
        wipe.init_wipe_db(db_path)
        check("init_wipe_db can run twice", True)

        print("== The typed-name confirmation, which is the point of this file ==")
        check("the exact name confirms", wipe.confirm_wipe("PHONE-1", "PHONE-1") == "PHONE-1")
        check("a different machine's name does not",
              refused(wipe.confirm_wipe, "PHONE-1", "PHONE-2"))
        # The two that a lenient match would let through, and that is the whole accident: an
        # operator looking at PHONE-12 while typing PHONE-1.
        check("a PREFIX of the name does not", refused(wipe.confirm_wipe, "PHONE-12", "PHONE-1"))
        check("the name with something after it does not",
              refused(wipe.confirm_wipe, "PHONE-1", "PHONE-12"))
        check("the wrong case does not", refused(wipe.confirm_wipe, "PHONE-1", "phone-1"))
        check("an empty confirmation does not", refused(wipe.confirm_wipe, "PHONE-1", ""))
        check("a missing confirmation does not", refused(wipe.confirm_wipe, "PHONE-1", None))
        # Surrounding whitespace is forgiven, because a name copied out of the page carries it
        # and refusing that teaches nothing -- the name itself still has to be right.
        check("whitespace around a correct name is forgiven",
              wipe.confirm_wipe("PHONE-1", "  PHONE-1  ") == "PHONE-1")
        check("the refusal names the machine, so an operator can see what to type",
              "PHONE-1" in str(_refusal(wipe.confirm_wipe, "PHONE-1", "wrong")))

        print("\n== Actions ==")
        check("lock and wipe are the only two", set(wipe.ACTIONS) == {"lock", "wipe"})
        check("each maps to the command type the agent implements",
              wipe.COMMAND_FOR == {"lock": "lock_device", "wipe": "wipe_device"})
        check("an unknown action is refused", refused(wipe.validate_action, "reboot"))

        print("\n== Recording ==")
        lock_id = wipe.record_request(db_path, machine="PHONE-1", action="lock",
                                      actor="super@x.com", command_id="cmd-1", now=1000)
        check("a lock is recorded", bool(lock_id))
        wipe_id = wipe.record_request(db_path, machine="PHONE-1", action="wipe",
                                      actor="super@x.com", reset_protection=True,
                                      reason="lost in a taxi", now=2000)
        wipe.attach_command(db_path, wipe_id, "cmd-2")

        rows = wipe.history(db_path, "PHONE-1")
        check("history comes back newest first",
              [r["action"] for r in rows] == ["wipe", "lock"])
        check("...carrying who asked", rows[0]["requested_by"] == "super@x.com")
        check("...and whether reset protection was cleared",
              rows[0]["reset_protection"] is True)
        check("...and the command it became, attached after the fact",
              rows[0]["command_id"] == "cmd-2")
        check("...and the reason, if one was given",
              rows[0]["reason"] == "lost in a taxi")
        check("a request with no machine is refused",
              refused(wipe.record_request, db_path, machine="", action="wipe", actor="x"))

        print("\n== The one fact a wiped device can never report ==")
        last = wipe.last_wipe(db_path, "PHONE-1")
        check("the newest wipe is findable on its own", last["id"] == wipe_id)
        check("...and a device that was only ever locked has none",
              wipe.last_wipe(db_path, "NEVER") is None)
        # A later LOCK must not hide an earlier wipe: the machine page's "this device was
        # erased" line is the only explanation an operator will ever get for the silence.
        wipe.record_request(db_path, machine="PHONE-1", action="lock", actor="x", now=3000)
        check("a later lock does not hide the wipe before it",
              wipe.last_wipe(db_path, "PHONE-1")["id"] == wipe_id)

        print("\n== Lifecycle ==")
        wipe.record_request(db_path, machine="GONE", action="wipe", actor="x", now=4000)
        wipe.forget_machine(db_path, "GONE")
        check("forgetting a machine drops its requests",
              wipe.history(db_path, "GONE") == [])

        wipe.record_request(db_path, machine="OLD-NAME", action="wipe", actor="x", now=5000)
        wipe.rename_machine(db_path, "OLD-NAME", "NEW-NAME")
        check("a rename carries the history with it",
              len(wipe.history(db_path, "NEW-NAME")) == 1)
        check("...and leaves nothing under the old name",
              wipe.history(db_path, "OLD-NAME") == [])

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


def _refusal(fn, *a, **k):
    """The exception a refusal raises, so a test can assert on its sentence."""
    try:
        fn(*a, **k)
    except wipe.WipeRefused as e:
        return e
    return None


if __name__ == "__main__":
    sys.exit(main())
