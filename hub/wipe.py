"""Locking a device's screen, and erasing it -- roadmap #23 phase H.

**The largest blast radius in this product, and the only action in it with no undo.** A backup
can be restored, a policy can be edited back, a suspended app can be un-suspended; a factory
reset cannot be any of those. That single fact is what shapes every decision in this file, and
each of them is a decision rather than a convention:

  * **Two actions, deliberately unequal.** `lock_device` locks the screen now and is undone by
    the person's own PIN a moment later -- it is the ordinary first move for a phone left on a
    train, and it is built to be reached in one click. `wipe_device` erases the device, and it
    is built to be reached only on purpose. They share the `wipe_device` capability because
    anybody trusted with the second is trusted with the first, and a capability nobody would
    ever grant alone is a row in the permissions UI that only adds noise.
  * **A wipe requires the machine's name typed out**, checked here rather than in JavaScript.
    A confirmation that lives only in the console is a confirmation the API does not have, and
    this is precisely the endpoint where "somebody scripted it" must not be a way around the
    pause. The comparison is exact and case-sensitive: a machine called `PHONE-12` and one
    called `phone-1` are two devices, and a lenient match here is how the wrong one goes.
  * **The audit row is written BEFORE the command exists**, not after. A wipe that is issued
    and then fails to be recorded is the one ordering that cannot be reconstructed afterwards --
    the device is gone and the trail never mentions it. Recording an intent that then failed to
    queue is the harmless direction of the same mistake.
  * **A request row of its own, unlike a locate.** location.py explains why a locate needs no
    lifecycle: it is one command with one answer and the command queue already models every
    state it has. A wipe is the opposite, and for a reason peculiar to it -- *the answer never
    comes back*. A wiped device does not report a result, does not heartbeat again, and does
    not exist to be asked. Its command row would sit at "sent" forever, and the console would
    show a machine that simply went quiet: indistinguishable from a flat battery. This table is
    what lets a machine page say "wiped on the 9th by X" about a device that will never speak
    again.

**`reset_protection` is a per-device decision and is stored per request.** Leaving Android's
factory-reset protection in place means the wiped device is unusable without the Google account
that was on it, which is theft protection when the device was stolen and a self-inflicted brick
when it was a company handset with an ex-employee's account on it. Defaulting to CLEARING it is
the right default for company-owned hardware and is the wrong one for a stolen personal device,
so the console says which it is doing and the answer rides the command.

Kept free of Flask so it can be unit-tested in isolation.
"""
import sqlite3
import time
import uuid

# ================================
# VOCABULARY
# ================================
#: Lock the screen now. Reversible by the person holding the device.
ACTION_LOCK = "lock"
#: Erase the device. Not reversible by anybody.
ACTION_WIPE = "wipe"
ACTIONS = (ACTION_LOCK, ACTION_WIPE)

#: The command types the agent answers. Their executors are Core-side so the protocol half
#: stays testable without a device.
COMMAND_FOR = {ACTION_LOCK: "lock_device", ACTION_WIPE: "wipe_device"}

MAX_MACHINE_CHARS = 200
MAX_REASON_CHARS = 500


class WipeRefused(ValueError):
    """A lock or wipe that will not be issued, with a sentence saying why.

    A ValueError subclass so refusals.refuse renders it like every other authored refusal,
    and its own type so a caller can tell "the operator typed the wrong name" apart from a
    programming mistake.
    """


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_wipe_db(db_path):
    """Idempotent, like every other init_*_db.

    One table. There is no result to store, which is the whole point: a wiped device never
    reports back, so what is recorded is the REQUEST -- who, when, which action, and whether
    factory-reset protection was cleared.
    """
    with get_conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_security_actions (
                id TEXT PRIMARY KEY,
                machine TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_by TEXT,
                requested_at INTEGER NOT NULL,
                command_id TEXT,
                reset_protection INTEGER NOT NULL DEFAULT 0,
                reason TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_device_security_machine "
                     "ON device_security_actions(machine, requested_at DESC)")


def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


# ================================
# VALIDATION
# ================================
def confirm_wipe(machine, typed):
    """The typed-name confirmation. Raises WipeRefused when it does not match exactly.

    **Exact and case-sensitive, on purpose.** A lenient match is how the wrong device gets
    erased: `PHONE-1` and `phone-12` are two machines, and the whole value of this step is that
    it cannot be passed by somebody who is not looking at the name in front of them.

    Enforced HERE rather than in the console. A confirmation dialog stops an operator; it does
    not stop a script, a copied curl command, or a second console written later -- and this is
    the one endpoint where those must not be a way around the pause.
    """
    machine = _clean(machine, MAX_MACHINE_CHARS)
    if not machine:
        raise WipeRefused("A wipe names one machine.")
    if _clean(typed, MAX_MACHINE_CHARS) != machine:
        raise WipeRefused(f"Type the machine's name exactly -- {machine} -- to confirm the "
                          f"wipe. Nothing has been sent.")
    return machine


def validate_action(action):
    action = _clean(action, 16).lower()
    if action not in ACTIONS:
        raise WipeRefused(f"Unknown action: {action or '(missing)'}.")
    return action


# ================================
# RECORDING
# ================================
def record_request(db_path, *, machine, action, actor, command_id="", reset_protection=False,
                   reason="", now=None):
    """Store the intent. Returns the row id.

    Called BEFORE the command is created -- see the module docstring on why that ordering is
    the safe one.
    """
    action = validate_action(action)
    machine = _clean(machine, MAX_MACHINE_CHARS)
    if not machine:
        raise WipeRefused("A lock or wipe names one machine.")
    row_id = uuid.uuid4().hex
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO device_security_actions(id, machine, action, requested_by, "
            "                                    requested_at, command_id, reset_protection, "
            "                                    reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, machine, action, _clean(actor, 200), now, _clean(command_id, 64),
             1 if reset_protection else 0, _clean(reason, MAX_REASON_CHARS)))
    return row_id


def attach_command(db_path, row_id, command_id):
    """Fill in the command this request became, once it exists."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE device_security_actions SET command_id = ? WHERE id = ?",
                     (_clean(command_id, 64), row_id))


def history(db_path, machine, limit=20):
    """What has been asked of this device, newest first.

    Kept short by default: this is a card on a machine page answering "has anybody done
    something drastic to this device", not an audit trail. The audit trail is the audit trail.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, machine, action, requested_by, requested_at, command_id, "
            "       reset_protection, reason FROM device_security_actions "
            "WHERE machine = ? ORDER BY requested_at DESC, id DESC LIMIT ?",
            (_clean(machine, MAX_MACHINE_CHARS), max(1, min(200, int(limit or 20))))).fetchall()
    return [{**dict(r), "reset_protection": bool(r["reset_protection"])} for r in rows]


def last_wipe(db_path, machine):
    """The most recent wipe of this machine, or None.

    The one fact the machine page needs above the others: a device that was wiped is not a
    device that has gone offline, and the console is the only place that can tell them apart --
    the device itself will never say so.
    """
    for row in history(db_path, machine, limit=200):
        if row["action"] == ACTION_WIPE:
            return row
    return None


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's requests.

    Unlike location history, there is an argument for keeping these -- "who erased this
    device" outlives the machine row. It is answered by the AUDIT TRAIL, which is not pruned
    and which carries the same facts; this table is a per-machine view, and a view of a
    machine that no longer exists is only a way for a reused hostname to inherit somebody
    else's wipe.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM device_security_actions WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Follow a machine through a duplicate-serial merge. The history MOVES: the survivor is
    the same physical device, and "this device was wiped in March" is true of it under either
    name."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE device_security_actions SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
