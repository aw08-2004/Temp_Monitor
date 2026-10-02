"""Security posture -- roadmap #25 phase D, and the CIS Controls IG1 safeguards it answers.

The device sheet has said since #25 A that "antivirus, firewall, local administrators and TPM
state are not collected by FleetHub yet". This module is what collects them: a Windows agent
reads its own settings on the inventory loop (`Security/PostureReader.cs`), carries them on the
heartbeat, and this stores the report and judges it.

**The silent failure this exists to prevent is a check that reads "passed" because nobody
asked.** A compliance view is read by people who will not open the machine to check, and a
green row is a claim. So every check has three outcomes, never two:

  * `pass` -- the machine reported, and what it reported meets the threshold;
  * `fail` -- the machine reported, and it does not;
  * `unknown` -- the machine did not say, or its provider failed. **Never folded into either
    of the others.** An unknown counted as a pass is a fleet that looks compliant because its
    agents are old; one counted as a fail is a red card on every PC whose WMI hiccupped, and the
    week of false findings is the week a helpdesk learns to ignore the card.

**The agent reports facts; this module decides.** A signature age arrives as a number of days,
a screen-lock timeout as seconds, an Administrators member as a name and a SID. What counts as
stale, too long or too privileged is an operator setting (`security.posture_*`), so a group
that tightens its lock timeout changes one value here rather than cutting an agent release.
`evaluate` is pure for that reason: settings in, verdicts out, nothing stored.

**Evidence, not enforcement.** Nothing here changes a machine. Switching a firewall on is a
script an operator runs deliberately through Rules or Packages; a reporter that also enforced
would be changing PCs on an hourly timer nobody approved.

Each check names the IG1 safeguard it is evidence for (`cis`), as numbered in CIS Controls
v8.1.2. Secure Boot and the TPM name none -- they are not IG1 safeguards in their own right --
and are here because the device sheet promised them.

Rejected, and recorded in ROADMAP.MD #25: one boolean "compliant" per machine (it hides which
safeguard failed, which is the only thing anybody acts on); judging on the agent (a threshold
change would need a fleet release); a table per area (the posture is read, replaced and judged
as one document, exactly like `machine_bios`).

Kept free of Flask so it can be unit-tested standalone; posture_web.py wires HTTP on top.
"""
import json
import sqlite3
import time

import settings

# ================================
# VOCABULARY
# ================================
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

#: The checks, in the order every surface lists them, each with the CIS v8.1.2 IG1 safeguard
#: it is evidence for. tests/test_posture.py pins that the console has a title for each id.
CHECKS = (
    ("antivirus", "10.1"),
    ("signatures", "10.2"),
    ("autorun", "10.3"),
    ("firewall", "4.4, 4.5"),
    ("session_lock", "4.3"),
    ("default_accounts", "4.7"),
    ("admin_accounts", "5.4"),
    ("encryption", "3.6"),
    ("secure_boot", ""),
    ("tpm", ""),
)
CHECK_IDS = tuple(c for c, _ in CHECKS)

AREAS = ("antivirus", "defender", "firewall", "autorun", "session_lock", "accounts",
         "secure_boot", "tpm")

FIREWALL_PROFILES = ("domain", "private", "public")
SECURE_BOOT_STATES = frozenset({"on", "off", "unsupported", "unknown"})
MEMBER_KINDS = frozenset({"user", "group", "deleted", "unknown"})

#: `NoDriveTypeAutoRun = 0xFF` turns AutoPlay off for every drive type and `NoAutorun = 1`
#: stops autorun.inf from executing. Both, because CIS 10.3 names both, and either alone
#: leaves the other way in.
AUTORUN_ALL_DRIVES = 0xFF

#: RIDs that are a default account (CIS 4.7) and a sanctioned administrator (5.4).
RID_ADMINISTRATOR = 500
RID_GUEST = 501
RID_DOMAIN_ADMINS = 512

# ================================
# INGEST BOUNDS
# ================================
#: Matches PostureReader.MaxEntries on the agent, so the two agree about "too many".
MAX_ENTRIES = 200
MAX_USERS = 32
MAX_TEXT_CHARS = 200
MAX_ERROR_CHARS = 300


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_posture_db(db_path):
    """Create the posture table. Idempotent -- safe to call on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One document per machine, replaced wholesale by each report: the areas are read
        # together on the agent and judged together here, and nothing ever updates one alone.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_posture (
                machine      TEXT PRIMARY KEY,
                posture_json TEXT NOT NULL,
                reported_at  INTEGER NOT NULL
            )
            """
        )


# ================================
# INGEST
# ================================
def _text(value, limit=MAX_TEXT_CHARS):
    return str(value if value is not None else "").strip()[:limit]


def _bool(value):
    """True, False, or None for anything that is not a JSON boolean.

    **Strict on purpose.** `"false"` is truthy in Python, and a lenient read here is how a
    firewall the agent reported off would be stored on.
    """
    return value if isinstance(value, bool) else None


def _int(value, low=0, high=10**9):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(low, min(high, value))


def _list(value, limit):
    return list(value)[:limit] if isinstance(value, list) else []


def _area(payload, name):
    raw = payload.get(name)
    return raw if isinstance(raw, dict) else {}


def clean_posture(payload):
    """Normalise a reported posture, or None if it is not one at all.

    Pure, and the one place an agent's report is trusted to have a shape. Every area comes out
    present, with unknown values as None -- the same rule the agent's payload follows, so a
    field a future agent stops sending reads as "not told" rather than as a default.
    """
    if not isinstance(payload, dict) or not any(isinstance(payload.get(a), dict)
                                                for a in AREAS):
        return None

    av = _area(payload, "antivirus")
    products = []
    for p in _list(av.get("products"), MAX_ENTRIES):
        if isinstance(p, dict) and _text(p.get("name")):
            products.append({"name": _text(p.get("name")),
                             "state": _int(p.get("state"), 0, 2**32 - 1),
                             "enabled": _bool(p.get("enabled")),
                             "up_to_date": _bool(p.get("up_to_date"))})

    defender = _area(payload, "defender")
    firewall = _area(payload, "firewall")
    profiles = []
    for p in _list(firewall.get("profiles"), len(FIREWALL_PROFILES)):
        if isinstance(p, dict) and _text(p.get("name")).lower() in FIREWALL_PROFILES:
            profiles.append({"name": _text(p.get("name")).lower(),
                             "enabled": _bool(p.get("enabled"))})

    autorun = _area(payload, "autorun")
    lock = _area(payload, "session_lock")
    users = []
    for u in _list(lock.get("users"), MAX_USERS):
        if isinstance(u, dict) and _text(u.get("sid")):
            users.append({"sid": _text(u.get("sid")), "active": _bool(u.get("active")),
                          "secure": _bool(u.get("secure")),
                          "timeout_seconds": _int(u.get("timeout_seconds"))})

    accounts = _area(payload, "accounts")
    local = []
    for a in _list(accounts.get("local"), MAX_ENTRIES):
        if isinstance(a, dict) and _text(a.get("sid")):
            local.append({"name": _text(a.get("name")), "sid": _text(a.get("sid")),
                          "enabled": _bool(a.get("enabled"))})
    admins = []
    for m in _list(accounts.get("administrators"), MAX_ENTRIES):
        if isinstance(m, dict) and (_text(m.get("sid")) or _text(m.get("name"))):
            kind = _text(m.get("kind")).lower()
            admins.append({"name": _text(m.get("name")), "sid": _text(m.get("sid")),
                           "kind": kind if kind in MEMBER_KINDS else "unknown",
                           "local": _bool(m.get("local"))})

    secure_boot = _area(payload, "secure_boot")
    boot_state = _text(secure_boot.get("state")).lower()
    tpm = _area(payload, "tpm")

    def err(area):
        return _text(area.get("error"), MAX_ERROR_CHARS)

    cleaned = {
        "antivirus": {"supported": _bool(av.get("supported")), "products": products,
                      "error": err(av)},
        "defender": {"present": _bool(defender.get("present")),
                     "antivirus_enabled": _bool(defender.get("antivirus_enabled")),
                     "realtime": _bool(defender.get("realtime")),
                     "signature_age_days": _int(defender.get("signature_age_days")),
                     "signature_updated": _int(defender.get("signature_updated")),
                     "mode": _text(defender.get("mode")), "error": err(defender)},
        "firewall": {"profiles": profiles, "error": err(firewall)},
        "autorun": {"no_drive_type_autorun": _int(autorun.get("no_drive_type_autorun")),
                    "no_autorun": _int(autorun.get("no_autorun")), "error": err(autorun)},
        "session_lock": {"machine_inactivity_seconds":
                             _int(lock.get("machine_inactivity_seconds")),
                         "users": users, "error": err(lock)},
        "accounts": {"local": local, "administrators": admins, "error": err(accounts)},
        "secure_boot": {"state": boot_state if boot_state in SECURE_BOOT_STATES else "unknown",
                        "error": err(secure_boot)},
        "tpm": {"present": _bool(tpm.get("present")), "enabled": _bool(tpm.get("enabled")),
                "activated": _bool(tpm.get("activated")),
                "spec_version": _text(tpm.get("spec_version"), 40), "error": err(tpm)},
    }
    # Whether the agent sent each area at all. **Without this an absent area is
    # indistinguishable from one that was read and found empty** -- both clean to nulls, empty
    # lists and no error -- and two checks turn that emptiness into a verdict: AutoRun's "key
    # read, nothing configured" is a fail, and an Administrators group with nobody in it read
    # as a pass. Found in review of #25 D.
    for name in AREAS:
        cleaned[name]["reported"] = isinstance(payload.get(name), dict)
    return cleaned


def record_posture(db_path, machine, payload, now=None):
    """Store a machine's reported posture. Returns True if something was stored.

    A payload that is not a posture at all stores nothing and keeps the last good one: a
    truncated body must not replace a real report with a document of unknowns.
    """
    cleaned = clean_posture(payload)
    if cleaned is None or not machine:
        return False
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_posture(machine, posture_json, reported_at) VALUES (?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET posture_json = excluded.posture_json, "
            "reported_at = excluded.reported_at",
            (machine, json.dumps(cleaned), int(now if now is not None else time.time())))
    return True


# ================================
# READ
# ================================
def get_posture(db_path, machine):
    """{posture, reported_at} for `machine`; both None when it has never reported.

    "Never reported" is the absence of a row, exactly as software.py keeps it: every PC in
    the field is on an older agent the day this ships, and its checks must read as waiting,
    not as failed.
    """
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT posture_json, reported_at FROM machine_posture "
                           "WHERE machine = ?", (machine,)).fetchone()
    if row is None:
        return {"posture": None, "reported_at": None}
    try:
        posture = json.loads(row["posture_json"])
    except (TypeError, ValueError):
        posture = None
    return {"posture": posture, "reported_at": row["reported_at"] if posture else None}


def reporting_machines(db_path):
    with get_conn(db_path) as conn:
        return [r["machine"] for r in conn.execute(
            "SELECT machine FROM machine_posture ORDER BY machine")]


# ================================
# JUDGING
# ================================
def thresholds_from_settings(db_path):
    """The operator's thresholds, read once per request and handed to `evaluate`."""
    return {
        "signature_max_age_days": settings.get(db_path, "security.posture_signature_max_age_days"),
        "lock_max_seconds": settings.get(db_path, "security.posture_lock_max_seconds"),
        "admin_allowlist": list(settings.get(db_path, "security.posture_admin_allowlist") or []),
    }


def _rid(sid):
    tail = str(sid or "").rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _verdict(status, detail, **params):
    return {"status": status, "detail": detail, "params": params}


def _sent(area):
    """Whether the agent sent this area. A stored document without the marker reads as not
    sent: the only safe direction, since the marker is what separates "empty" from "absent"."""
    return area.get("reported") is True


def _unknown(area):
    """Unknown, and why: the provider's own error when it gave one."""
    if area.get("error"):
        return _verdict(STATUS_UNKNOWN, "read_failed", error=area["error"])
    return _verdict(STATUS_UNKNOWN, "not_reported")


def _check_antivirus(p, _t):
    av, defender = p["antivirus"], p["defender"]
    enabled = [x["name"] for x in av["products"] if x["enabled"]]
    if defender["realtime"] is True and not enabled:
        enabled = ["Microsoft Defender"]
    if enabled:
        return _verdict(STATUS_PASS, "av_on", names=", ".join(enabled))
    if av["products"]:
        # Registered and switched off (or snoozed, or expired -- the agent reads all three as
        # not enabled). Named, because "Kaspersky is off" is the sentence somebody acts on.
        return _verdict(STATUS_FAIL, "av_off", names=", ".join(x["name"] for x in av["products"]))
    if av["supported"] is True and not av["error"]:
        # Security Center answered and nothing is registered with it at all -- and Defender,
        # checked above, is not running either.
        return _verdict(STATUS_FAIL, "av_none")
    if av["supported"] is False and defender["present"] is True and defender["realtime"] is False:
        # A Server, where Defender's own word is the only one there is.
        return _verdict(STATUS_FAIL, "av_off", names="Microsoft Defender")
    return _unknown(av if av["error"] else defender)


def _check_signatures(p, t):
    av, defender = p["antivirus"], p["defender"]
    limit = t["signature_max_age_days"]
    enabled = [x for x in av["products"] if x["enabled"]]
    age = defender["signature_age_days"]
    defender_live = defender["realtime"] is True
    # **Judged only on products that are running.** Defender drops to passive when a third
    # party registers, and stops updating -- a stale Defender beside a current Kaspersky is the
    # normal state of such a PC, not a finding.
    if any(x["up_to_date"] is True for x in enabled) or (defender_live and age is not None
                                                         and age <= limit):
        return _verdict(STATUS_PASS, "sig_current")
    if defender_live and age is not None:
        return _verdict(STATUS_FAIL, "sig_stale", days=age, max=limit)
    if enabled and all(x["up_to_date"] is False for x in enabled):
        return _verdict(STATUS_FAIL, "sig_stale_product", names=", ".join(x["name"] for x in enabled))
    return _unknown(av if av["error"] else defender)


def _check_autorun(p, _t):
    a = p["autorun"]
    if a["error"] or not _sent(a):
        return _unknown(a)
    if a["no_drive_type_autorun"] == AUTORUN_ALL_DRIVES and a["no_autorun"] == 1:
        return _verdict(STATUS_PASS, "autorun_off")
    # Not configured at all is a fail, not an unknown: Windows' default is AutoPlay on, and
    # the agent did read the key -- it simply had nothing in it.
    return _verdict(STATUS_FAIL, "autorun_on")


def _check_firewall(p, _t):
    f = p["firewall"]
    off = [x["name"] for x in f["profiles"] if x["enabled"] is False]
    if off:
        return _verdict(STATUS_FAIL, "fw_off", profiles=off)
    if len(f["profiles"]) == len(FIREWALL_PROFILES) and all(x["enabled"] is True
                                                           for x in f["profiles"]):
        return _verdict(STATUS_PASS, "fw_on")
    # One profile the service would not describe and the rest on is not a pass: the
    # undescribed one is usually Public, which is the one that matters on a laptop.
    return _unknown(f)


def _check_session_lock(p, t):
    s = p["session_lock"]
    if not _sent(s):
        return _unknown(s)
    limit = t["lock_max_seconds"]
    machine = s["machine_inactivity_seconds"]
    if machine is not None and 0 < machine <= limit:
        # The machine-wide inactivity limit applies to every session, so it settles the
        # question whatever any one user's screen saver says.
        return _verdict(STATUS_PASS, "lock_ok", seconds=machine)
    users = s["users"]

    def locks(u):
        return (u["active"] is True and u["secure"] is True and u["timeout_seconds"] is not None
                and 0 < u["timeout_seconds"] <= limit)

    if users and all(locks(u) for u in users):
        return _verdict(STATUS_PASS, "lock_ok", seconds=max(u["timeout_seconds"] for u in users))
    if s["error"] and not users:
        return _unknown(s)
    longest = max([u["timeout_seconds"] for u in users
                   if u["active"] is True and u["secure"] is True
                   and u["timeout_seconds"] is not None] + ([machine] if machine else []),
                  default=None)
    if longest is not None and longest > limit:
        return _verdict(STATUS_FAIL, "lock_too_long", seconds=longest, max=limit)
    if users or machine is not None:
        return _verdict(STATUS_FAIL, "lock_missing", max=limit)
    # Nobody is signed in and no machine-wide limit is set. There is no session to judge,
    # and calling that a fail would flag every PC that was rebooted overnight.
    return _verdict(STATUS_UNKNOWN, "lock_no_users")


def _check_default_accounts(p, _t):
    a = p["accounts"]
    enabled = [x["name"] for x in a["local"]
               if x["enabled"] is True and _rid(x["sid"]) in (RID_ADMINISTRATOR, RID_GUEST)]
    if enabled:
        return _verdict(STATUS_FAIL, "defaults_enabled", names=", ".join(enabled))
    if a["local"]:
        return _verdict(STATUS_PASS, "defaults_disabled")
    return _unknown(a)


def _admin_allowed(member, allowlist):
    """Whether one Administrators member is sanctioned.

    The built-in Administrator (whose being *enabled* is 4.7's question, not this one's) and
    Domain Admins are always allowed. So is an Entra role group (S-1-12-1-..., kind group) --
    that is how a cloud-joined PC grants its Global Administrators local rights, and it is
    not a person. Everything else must be named in `security.posture_admin_allowlist`, by
    SID, by `DOMAIN\\name` or by bare name, compared without regard to case.
    """
    sid = member["sid"]
    rid = _rid(sid)
    if rid == RID_ADMINISTRATOR and member["local"] is not False:
        return True
    if rid == RID_DOMAIN_ADMINS and member["kind"] == "group":
        return True
    if sid.upper().startswith("S-1-12-1-") and member["kind"] == "group":
        return True
    name = member["name"].casefold()
    bare = name.rsplit("\\", 1)[-1]
    allowed = {str(x).casefold() for x in allowlist}
    return bool({name, bare, sid.casefold()} & allowed)


def _check_admin_accounts(p, t):
    a = p["accounts"]
    members = a["administrators"]
    if not members:
        # Administrators always has somebody in it on a working PC, so an empty list is a read
        # that failed or an area that was never sent -- with or without an error -- and never a
        # group that is empty. It used to pass when no error came with it, which put a green
        # 5.4 row on a machine that had not answered. Found in review.
        return _unknown(a)
    extra = [m["name"] or m["sid"] for m in members if not _admin_allowed(m, t["admin_allowlist"])]
    if extra:
        return _verdict(STATUS_FAIL, "admins_extra", names=", ".join(extra))
    return _verdict(STATUS_PASS, "admins_ok", count=len(members))


def _check_encryption(_p, _t, encryption):
    """CIS 3.6, judged from BitLocker's own report (roadmap #19) rather than re-collected."""
    support = (encryption or {}).get("support")
    if support is None:
        return _verdict(STATUS_UNKNOWN, "enc_not_reported")
    if support == "unsupported":
        return _verdict(STATUS_FAIL, "enc_unsupported")
    if support == "error":
        return _verdict(STATUS_UNKNOWN, "read_failed", error=encryption.get("error") or "")
    volumes = encryption.get("volumes") or []
    off = [v.get("mount") or v.get("device_id") for v in volumes if v.get("protection") == "off"]
    if off:
        return _verdict(STATUS_FAIL, "enc_unprotected", volumes=", ".join(off))
    if volumes and all(v.get("protection") == "on" for v in volumes):
        return _verdict(STATUS_PASS, "enc_all", count=len(volumes))
    return _verdict(STATUS_UNKNOWN, "not_reported")


def _check_secure_boot(p, _t):
    b = p["secure_boot"]
    if b["state"] == "on":
        return _verdict(STATUS_PASS, "secure_boot_on")
    if b["state"] == "off":
        return _verdict(STATUS_FAIL, "secure_boot_off")
    if b["state"] == "unsupported":
        return _verdict(STATUS_FAIL, "secure_boot_legacy")
    return _unknown(b)


def _check_tpm(p, _t):
    tpm = p["tpm"]
    if tpm["present"] is True and tpm["enabled"] is True and tpm["activated"] is True:
        return _verdict(STATUS_PASS, "tpm_ready", version=tpm["spec_version"])
    if tpm["present"] is False and not tpm["error"]:
        return _verdict(STATUS_FAIL, "tpm_missing")
    if tpm["present"] is True and (tpm["enabled"] is False or tpm["activated"] is False):
        return _verdict(STATUS_FAIL, "tpm_not_ready")
    return _unknown(tpm)


_EVALUATORS = {
    "antivirus": _check_antivirus,
    "signatures": _check_signatures,
    "autorun": _check_autorun,
    "firewall": _check_firewall,
    "session_lock": _check_session_lock,
    "default_accounts": _check_default_accounts,
    "admin_accounts": _check_admin_accounts,
    "secure_boot": _check_secure_boot,
    "tpm": _check_tpm,
}


def evaluate(posture, thresholds, encryption=None):
    """Every check's verdict for one machine, in CHECKS order. Pure.

    `posture` is a cleaned document (as stored); `encryption` is bitlocker.get_inventory's
    answer, passed in so this module never reaches into another's tables. A check whose
    evaluator raises is reported unknown rather than taking the rest with it -- a stored
    document from an older hub is the realistic way to get here.
    """
    out = []
    for check_id, cis in CHECKS:
        try:
            if check_id == "encryption":
                verdict = _check_encryption(posture, thresholds, encryption)
            else:
                verdict = _EVALUATORS[check_id](posture, thresholds)
        except (KeyError, TypeError, ValueError):
            verdict = _verdict(STATUS_UNKNOWN, "not_reported")
        out.append({"id": check_id, "cis": cis, **verdict})
    return out


def counts(checks):
    tally = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for c in checks:
        tally[c["status"]] += 1
    return tally


def fleet_summary(db_path, machines, thresholds, encryption_for):
    """Per-check pass/fail/unknown counts over the machines that have reported.

    `machines` is the caller's visible set, already scoped by the web layer -- always a list,
    so an empty scope is an empty summary and never the fleet's. `encryption_for(machine)` is
    bitlocker's reader, injected for the reason `evaluate` takes it. A machine that has never
    reported is counted in `not_reported`, never as unknown on every check: "this PC's agent
    is too old" is one fact, not ten.
    """
    wanted = set(machines)
    visible = [m for m in reporting_machines(db_path) if m in wanted]
    rows = {cid: {"id": cid, "cis": cis, STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0,
                  "failing": []}
            for cid, cis in CHECKS}
    for machine in visible:
        stored = get_posture(db_path, machine)
        if stored["posture"] is None:
            continue
        for check in evaluate(stored["posture"], thresholds, encryption_for(machine)):
            row = rows[check["id"]]
            row[check["status"]] += 1
            if check["status"] == STATUS_FAIL:
                row["failing"].append(machine)
    return {"checks": [rows[c] for c in CHECK_IDS], "reporting": len(visible),
            "not_reported": max(0, len(wanted) - len(visible))}


# ================================
# LIFECYCLE
# ================================
def forget_machine(db_path, machine):
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_posture WHERE machine = ?", (machine,))


def rename_machine(db_path, old, new):
    """Follow a merge or rename. The survivor's own posture wins a collision: both rows
    describe one physical PC, and the survivor's is the one its agent is still sending."""
    with get_conn(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO machine_posture(machine, posture_json, reported_at) "
                     "SELECT ?, posture_json, reported_at FROM machine_posture WHERE machine = ?",
                     (new, old))
        conn.execute("DELETE FROM machine_posture WHERE machine = ?", (old,))
