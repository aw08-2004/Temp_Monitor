"""The device sheet -- roadmap #25 phase A.

Everything the hub knows about one PC, on one page: what a Kaspersky export, a LOGINventory
sheet or a PDQ Inventory report hands a helpdesk when somebody asks "what is this machine, and
what is on it". **It collects nothing.** Nine tables already carried per-machine facts, each
behind the page that needed it, and no route joined them; this module is that join.

**The silent failure this exists to prevent is an empty section that reads as a fact.** A
sheet is printed and stapled to a ticket, and a Software section with no rows on it says "this
PC has nothing installed" to everybody who reads it later. It does not say that. It says one of
three different things, and every section here carries which:

  * `ok` -- the machine reported, and here is what it said (which may be an empty list: a PC
    with no pending patches is a real and good answer);
  * `waiting` -- no agent has told us yet. `waiting_for` names what would: an agent new enough
    to collect it (`agent_update`), or simply the machine's next check-in (`agent_report`);
  * `not_collected` -- nothing in this product collects it yet, and `waiting_for` names the
    roadmap phase that would (#25 C and D).

An agent release reaches the fleet over about fifteen minutes, the Linux and Android agents
answer none of the Windows questions, and a PC that has been offline for a week reports nothing
when it returns -- so `waiting` is the ordinary state of a lot of sections for a while, and the
wording is the difference between a rollout in progress and a machine that was wiped.

**The report renders stored facts. It never collects on demand.** A sheet for an offline PC is
exactly the case a helpdesk needs it for, and a fleet report that collected live would be a
command issued to every machine at once. Each section carries the time its facts were reported,
so a stale section is visibly stale rather than quietly wrong.

**No new capability, and no per-section gates.** Every section here is one an operator holding
`view` can already open page by page -- the Firmware and Network tabs, the encryption card, the
location card, the Apps card are all `view` + scope -- so the sheet is gated exactly that way
by reports_web and adds nothing. Two things are deliberately LEFT OUT because their own pages
gate them higher: BitLocker's escrow bookkeeping (who read a key, how often --
`read_recovery_keys`) and backup history (`manage_backups`). Showing either here would make the
sheet the way round a gate.

Live telemetry (the CPU and GPU names, memory size, per-volume usage) lives in app.py's
sensor cache, which is Flask-side. It is passed in as `hardware_probe` rather than imported,
so this module stays Flask-free and unit-testable standalone like every other model half.
"""
import sqlite3
import time

import apps
import bios
import bitlocker
import capabilities
import device_groups
import location
import patches
import remote
import rules
import software
import wake

# ================================
# SECTION VOCABULARY
# ================================
STATUS_OK = "ok"
STATUS_WAITING = "waiting"
STATUS_NOT_COLLECTED = "not_collected"

#: No agent has sent this block yet; one new enough already exists.
WAITING_AGENT_REPORT = "agent_report"
#: No agent in the field collects this yet -- it needs the agent release that ships #25 B.
WAITING_AGENT_UPDATE = "agent_update"
#: Nothing collects this at all yet; see ROADMAP.MD #25 phase C.
WAITING_PHASE_C = "phase_c"
#: See ROADMAP.MD #25 phase D.
WAITING_PHASE_D = "phase_d"
#: Not an agent fact at all: no directory sync has matched this machine (or none has run).
WAITING_DIRECTORY = "directory"

#: Section ids in the order the sheet prints them. The console renders their titles through
#: i18n by literal key, and tests/test_reports.py pins that every id here has one.
SECTIONS = (
    "identity", "os", "directory", "fields", "groups", "hardware", "firmware", "volumes",
    "network", "security", "software", "apps", "patches", "sessions", "capabilities",
    "location",
)

#: machine_info columns, grouped by the section that shows them. Named rather than
#: `SELECT *`: machine_info also carries internal state (primary_sensor_name, ad_ldap) that is
#: configuration, not a fact about the PC, and a sheet that grew a column every time somebody
#: added one for bookkeeping would end up printing it.
IDENTITY_COLUMNS = ("manufacturer", "model", "serial_number", "service_tag", "asset_tag",
                    "companion_version", "update_channel", "updated_at")
OS_COLUMNS = ("os_caption", "os_version", "os_build", "os_arch", "boot_epoch",
              "last_uptime_seconds")
DIRECTORY_COLUMNS = ("ad_dn", "ad_ou", "ad_owner", "ad_os", "ad_last_logon", "ad_disabled",
                     "ad_synced_at")

#: The flat, one-row-per-machine CSV. Field names rather than translated headers, for the
#: reason the Devices export gives: an export is read by scripts and by people in other
#: languages alike.
SUMMARY_FIELDS = (
    "machine", "manufacturer", "model", "serial_number", "service_tag", "asset_tag",
    "os_caption", "os_version", "os_build", "os_arch", "cpu", "gpu", "memory_gb",
    "bios_vendor", "bios_version", "ad_ou", "ad_owner", "groups", "software_count",
    "software_reported_at", "pending_patches", "encrypted_volumes", "unprotected_volumes",
    "agent_version", "last_seen",
)

#: The repeating sections, one CSV file each -- a sheet is nested, and flattening software
#: into the summary row would wreck it for the spreadsheet it feeds.
CSV_SECTIONS = {
    "summary": SUMMARY_FIELDS,
    "software": ("machine", "name", "version", "publisher", "install_date", "scope",
                 "user_sid", "arch", "install_location"),
    "patches": ("machine", "source", "kb", "title", "classification", "reboot_required",
                "first_seen"),
    "network": ("machine", "name", "description", "mac", "ipv4", "prefix", "kind", "link_up",
                "wake_enabled"),
    "volumes": ("machine", "name", "used_gb", "total_gb", "used_pct"),
}


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ================================
# HELPERS
# ================================
def _section(status, data=None, reported_at=None, waiting_for=None):
    out = {"status": status, "reported_at": reported_at, "data": data}
    if waiting_for:
        out["waiting_for"] = waiting_for
    return out


def _waiting(waiting_for=WAITING_AGENT_REPORT):
    return _section(STATUS_WAITING, waiting_for=waiting_for)


def _machine_info(db_path, machine):
    """The machine_info row as a dict, with only the columns that exist on this database.

    Every column after the first four was added by ALTER on some hub start, and a test DB (or
    a hub mid-upgrade) may not have run all of them -- so the wanted list is intersected with
    PRAGMA table_info rather than assumed.
    """
    with get_conn(db_path) as conn:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(machine_info)")}
        if not have:
            return None
        row = conn.execute("SELECT * FROM machine_info WHERE machine = ?",
                           (machine,)).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _pick(info, columns):
    return {c: info.get(c) for c in columns}


def _has_any(values):
    return any(v not in (None, "") for v in values.values())


def _groups_for(db_path, machine):
    """Names of the device groups this machine is in right now, alphabetically.

    Resolved live rather than stored, because a group is a target spec (an OU, a field value, a
    name pattern) and not a membership list: which groups a PC is in changes when somebody
    edits its custom field, with nothing on the PC changing at all.
    """
    groups = device_groups.list_groups(db_path)
    if not groups:
        return []
    members = device_groups.member_sets(db_path, [g["id"] for g in groups])
    key = machine.lower()
    return sorted((g["name"] for g in groups if key in members.get(g["id"], set())),
                  key=str.casefold)


# ================================
# THE SHEET
# ================================
def build_sheet(db_path, machine, hardware_probe=None, now=None):
    """Assemble one machine's sheet. Returns None for a machine the hub has never heard of.

    `hardware_probe(machine)` returns {cpu, gpu, memory_gb, volumes, reported_at} from the
    sensor cache, or None when there is no sensor block at all. Injected -- see the module
    docstring.
    """
    machine = str(machine or "").strip()
    info = _machine_info(db_path, machine) if machine else None
    if info is None:
        return None

    sections = {}

    identity = _pick(info, IDENTITY_COLUMNS)
    sections["identity"] = _section(STATUS_OK, identity, reported_at=info.get("updated_at"))

    os_facts = _pick(info, OS_COLUMNS)
    sections["os"] = (_section(STATUS_OK, os_facts) if _has_any(os_facts)
                      else _waiting(WAITING_AGENT_REPORT))

    directory_facts = _pick(info, DIRECTORY_COLUMNS)
    # Filled by directory.py's sync, never by the agent -- so an empty section waits on the
    # sync, and saying "when the machine next checks in" here would send somebody to reboot a
    # PC over a fact the PC does not supply.
    sections["directory"] = (_section(STATUS_OK, directory_facts,
                                      reported_at=info.get("ad_synced_at"))
                             if _has_any(directory_facts) else _waiting(WAITING_DIRECTORY))

    sections["fields"] = _section(STATUS_OK, rules.get_machine_fields(db_path, machine))
    sections["groups"] = _section(STATUS_OK, _groups_for(db_path, machine))

    # Hardware: what the sensor stream names today, and -- explicitly -- what it does not.
    # DIMMs, disk models, monitors by EDID, chassis, TPM and battery are #25 C, and saying so
    # on the sheet is the difference between "we do not collect that yet" and "none".
    probe = hardware_probe(machine) if hardware_probe else None
    if probe:
        sections["hardware"] = _section(
            STATUS_OK,
            {"cpu": probe.get("cpu"), "gpu": probe.get("gpu"),
             "memory_gb": probe.get("memory_gb"), "detail": None,
             "detail_waiting_for": WAITING_PHASE_C},
            reported_at=probe.get("reported_at"))
        volumes = probe.get("volumes") or []
        sections["volumes"] = _section(STATUS_OK, volumes, reported_at=probe.get("reported_at"))
    else:
        sections["hardware"] = _waiting(WAITING_AGENT_REPORT)
        sections["volumes"] = _waiting(WAITING_AGENT_REPORT)

    firmware = bios.get_inventory(db_path, machine)
    if firmware.get("support") is None:
        sections["firmware"] = _waiting(WAITING_AGENT_REPORT)
    else:
        # The attribute list is left on the Firmware tab. Two hundred vendor settings are not
        # what a device sheet is asked for, and the ones that matter to a report (Secure Boot
        # among them) belong to #25 D's posture section, read properly rather than scraped.
        sections["firmware"] = _section(
            STATUS_OK,
            {k: firmware.get(k) for k in ("support", "vendor", "interface", "bios_version",
                                          "password_set", "error")},
            reported_at=firmware.get("reported_at"))

    network = wake.get_network(db_path, machine)
    sections["network"] = (
        _waiting(WAITING_AGENT_REPORT) if network.get("reported_at") is None
        else _section(STATUS_OK, {"nics": network.get("nics") or [],
                                  "fast_startup": network.get("fast_startup")},
                      reported_at=network.get("reported_at")))

    encryption = bitlocker.get_inventory(db_path, machine)
    if encryption.get("support") is None:
        sections["security"] = _waiting(WAITING_AGENT_REPORT)
    else:
        # Posture only. Which protectors are escrowed, and who read them, is the encryption
        # card's and behind read_recovery_keys -- see the module docstring.
        volumes = []
        for v in encryption.get("volumes") or []:
            volumes.append({
                "mount": v.get("mount") or v.get("device_id"),
                "protection": v.get("protection"),
                "conversion": v.get("conversion"),
                "method": v.get("method"),
                "has_recovery_password": any(
                    p.get("kind") == bitlocker.PROTECTOR_RECOVERY_PASSWORD
                    for p in v.get("protectors") or []),
            })
        sections["security"] = _section(
            STATUS_OK,
            {"bitlocker_support": encryption.get("support"),
             "bitlocker_error": encryption.get("error"), "volumes": volumes,
             # Antivirus, firewall, local admins and TPM state are #25 D.
             "posture_waiting_for": WAITING_PHASE_D},
            reported_at=encryption.get("reported_at"))

    installed = software.get_inventory(db_path, machine)
    if installed["reported_at"] is None:
        # The one section that is waiting on an agent RELEASE rather than a check-in: no agent
        # before #25 B's cut sends this at all.
        sections["software"] = _waiting(WAITING_AGENT_UPDATE)
    else:
        sections["software"] = _section(
            STATUS_OK, {"items": installed["software"], "error": installed["error"]},
            reported_at=installed["reported_at"])

    # Android's package inventory (#23 D). Only drawn when the device has one; on a Windows PC
    # its absence is not "waiting", it is "not this kind of machine", and the software section
    # above is what answers.
    phone = apps.get_inventory(db_path, machine)
    sections["apps"] = (_section(STATUS_OK, phone["apps"], reported_at=phone["reported_at"])
                        if phone["reported_at"] is not None else None)

    # patches.scanned_at is what separates "fully patched" from "never scanned" -- both are an
    # empty list in machine_patches, and the first version of this sheet printed the former as
    # the latter.
    scanned = patches.scanned_at(db_path, machine)
    sections["patches"] = (
        _waiting(WAITING_AGENT_REPORT) if scanned is None
        else _section(STATUS_OK, patches.list_machine_patches(db_path, machine),
                      reported_at=scanned))

    seats = remote.get_inventory(db_path, machine)
    sections["sessions"] = (_waiting(WAITING_AGENT_REPORT) if seats.get("reported_at") is None
                            else _section(STATUS_OK, {"sessions": seats.get("sessions") or [],
                                                      "displays": seats.get("displays") or {}},
                                          reported_at=seats.get("reported_at")))

    caps = capabilities.get_capabilities(db_path, machine)
    sections["capabilities"] = (
        # Every Windows agent in the field has never sent one, and that means "everything",
        # not "nothing" -- see capabilities.py. The sheet says "not reported", never "none".
        _waiting(WAITING_AGENT_REPORT) if caps.get("reported_at") is None
        else _section(STATUS_OK, {"platform": caps.get("platform"),
                                  "commands": caps.get("commands"),
                                  "features": caps.get("features")},
                      reported_at=caps.get("reported_at")))

    fix = location.latest_fix(db_path, machine)
    sections["location"] = (None if fix is None else _section(
        STATUS_OK,
        {k: fix.get(k) for k in ("lat", "lon", "accuracy_m", "provider", "stale", "fixed_at")},
        reported_at=fix.get("received_at")))

    present = {k: v for k, v in sections.items() if v is not None}
    return {
        "machine": machine,
        "generated_at": int(time.time()) if now is None else int(now),
        # Absent sections are dropped rather than sent as null, so the console never has to
        # tell "not applicable to this machine" from a section it forgot to draw.
        "sections": present,
        # The print order, sent explicitly: Flask's jsonify sorts object keys, so a sheet drawn
        # in `sections` order would print Capabilities first and Identity somewhere in the
        # middle.
        "order": [s for s in SECTIONS if s in present],
    }


# ================================
# FLATTENING (CSV)
# ================================
def _data(sheet, section):
    entry = (sheet.get("sections") or {}).get(section) or {}
    return entry.get("data") if entry.get("status") == STATUS_OK else None


def summary_row(sheet):
    """One sheet as the flat summary row. Blank rather than "0" for anything not reported: a
    spreadsheet summing `pending_patches` should not count a silent machine as fully patched."""
    identity = _data(sheet, "identity") or {}
    os_facts = _data(sheet, "os") or {}
    directory_facts = _data(sheet, "directory") or {}
    hardware = _data(sheet, "hardware") or {}
    firmware = _data(sheet, "firmware") or {}
    security = _data(sheet, "security")
    installed = _data(sheet, "software")
    pending = _data(sheet, "patches")
    software_section = (sheet.get("sections") or {}).get("software") or {}

    encrypted = unprotected = ""
    if security is not None:
        vols = security.get("volumes") or []
        encrypted = sum(1 for v in vols if v.get("protection") == "on")
        unprotected = sum(1 for v in vols if v.get("protection") == "off")

    return {
        "machine": sheet["machine"],
        **{k: identity.get(k) for k in ("manufacturer", "model", "serial_number",
                                        "service_tag", "asset_tag")},
        **{k: os_facts.get(k) for k in ("os_caption", "os_version", "os_build", "os_arch")},
        "cpu": hardware.get("cpu"),
        "gpu": hardware.get("gpu"),
        "memory_gb": hardware.get("memory_gb"),
        "bios_vendor": firmware.get("vendor"),
        "bios_version": firmware.get("bios_version"),
        "ad_ou": directory_facts.get("ad_ou"),
        "ad_owner": directory_facts.get("ad_owner"),
        "groups": "; ".join(_data(sheet, "groups") or []),
        "software_count": "" if installed is None else len(installed.get("items") or []),
        "software_reported_at": software_section.get("reported_at") or "",
        "pending_patches": "" if pending is None else len(pending),
        "encrypted_volumes": encrypted,
        "unprotected_volumes": unprotected,
        "agent_version": identity.get("companion_version"),
        "last_seen": identity.get("updated_at"),
    }


def section_rows(sheet, section):
    """The rows one sheet contributes to a CSV file, each already carrying `machine`."""
    machine = sheet["machine"]
    if section == "summary":
        return [summary_row(sheet)]
    if section == "software":
        return [{"machine": machine, **item}
                for item in ((_data(sheet, "software") or {}).get("items") or [])]
    if section == "patches":
        return [{"machine": machine, **item} for item in (_data(sheet, "patches") or [])]
    if section == "network":
        return [{"machine": machine, **nic}
                for nic in ((_data(sheet, "network") or {}).get("nics") or [])]
    if section == "volumes":
        return [{"machine": machine, **vol} for vol in (_data(sheet, "volumes") or [])]
    raise ValueError(f"unknown CSV section: {section!r}")


def neutralise_cell(value):
    """One CSV cell's text, safe to open in a spreadsheet.

    The same rule the Devices export applies client-side (inventory.js csvCell), server-side
    here because this file is built on the hub: a leading = + - @ (or a tab/CR a spreadsheet
    strips first) gets an apostrophe. Software names are the most attacker-influenced text in
    the whole hub -- a local user can write their own uninstall key -- so this is not optional.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # A number is ours, not the machine's, and "-3" is a value rather than a formula.
        return str(value)
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text
