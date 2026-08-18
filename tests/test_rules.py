"""Tests the rules engine's foundation: the variable resolver, custom fields, the condition
AST/parser/formatter, and the three-valued evaluator.

Unlike most modules here this one does NOT import app: rules.py is deliberately Flask-free
and app-free (it takes the diagnostics dict in rather than importing extract_diagnostics),
so its tests need nothing but a temp SQLite file. That is the whole point of the seam -- if
this file ever needs `import app`, the seam has leaked.

The tables the resolver reads are created here by hand rather than by calling app.init_db().
That keeps the module fast and isolated, at the cost of having to keep these CREATEs in step
with app's -- so they carry only the columns the resolver actually reads, and a drift shows
up as an UNKNOWN in the resolver tests rather than as a silent pass.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import rules
from rules import UNKNOWN

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


_TMPDIR = tempfile.mkdtemp(prefix="hub-rules-test-")
DB = os.path.join(_TMPDIR, "rules-test.db")
NOW = 1_700_000_000


def seed_schema():
    conn = sqlite3.connect(DB)
    conn.executescript(
        """
        CREATE TABLE machine_info (
            machine TEXT PRIMARY KEY, asset_tag TEXT, serial_number TEXT, model TEXT,
            updated_at TEXT, companion_version TEXT, last_temp REAL,
            last_uptime_seconds INTEGER, primary_sensor_name TEXT, service_tag TEXT,
            manufacturer TEXT, boot_epoch INTEGER,
            ad_dn TEXT, ad_ou TEXT, ad_object_guid TEXT, ad_owner TEXT, ad_os TEXT,
            ad_disabled INTEGER, ad_last_logon TEXT, ad_synced_at INTEGER);
        CREATE TABLE remote_inventory (
            machine TEXT PRIMARY KEY, sessions_json TEXT NOT NULL,
            displays_json TEXT NOT NULL, reported_at INTEGER NOT NULL);
        CREATE TABLE machine_nics (
            machine TEXT NOT NULL, mac TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', ipv4 TEXT NOT NULL DEFAULT '',
            prefix INTEGER, kind TEXT NOT NULL DEFAULT 'other',
            link_up INTEGER NOT NULL DEFAULT 0, wake_enabled INTEGER,
            reported_at INTEGER NOT NULL, PRIMARY KEY (machine, mac));
        CREATE TABLE machine_network (
            machine TEXT PRIMARY KEY, fast_startup INTEGER, reported_at INTEGER NOT NULL);
        CREATE TABLE machine_bios (
            machine TEXT PRIMARY KEY, support TEXT NOT NULL, vendor TEXT NOT NULL DEFAULT '',
            interface TEXT NOT NULL DEFAULT '', bios_version TEXT NOT NULL DEFAULT '',
            password_set INTEGER, error TEXT NOT NULL DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '[]', reported_at INTEGER NOT NULL);
        CREATE TABLE machine_processes (
            machine TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
            captured_at INTEGER, reported_at INTEGER NOT NULL);
        """
    )
    conn.commit()
    conn.close()
    rules.init_rules_db(DB)


def ts(epoch):
    """machine_info.updated_at as app writes it: naive LOCAL time, not epoch, not UTC."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def seed_machine(machine, *, updated_at=NOW, boot_epoch=None, temp=55.0, sessions=None,
                 bios=True, nics=True, procs=False, uptime=None):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR REPLACE INTO machine_info (machine, updated_at, companion_version, "
        "last_temp, last_uptime_seconds, boot_epoch, model, manufacturer, serial_number, "
        "ad_ou, ad_disabled, ad_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (machine, ts(updated_at), "3.28.0", temp, uptime, boot_epoch, "OptiPlex 7090",
         "Dell Inc.", "5CG1234", "OU=Sales,DC=corp", 0, NOW - 3600),
    )
    if sessions is not None:
        conn.execute(
            "INSERT OR REPLACE INTO remote_inventory VALUES (?,?,?,?)",
            (machine, __import__("json").dumps(sessions), "{}", updated_at),
        )
    if nics:
        conn.execute(
            "INSERT OR REPLACE INTO machine_nics (machine, mac, ipv4, kind, link_up, "
            "wake_enabled, reported_at) VALUES (?,?,?,?,?,?,?)",
            (machine, "AA:BB:CC:00:11:22", "10.0.0.5", "ethernet", 1, 1, updated_at),
        )
        conn.execute("INSERT OR REPLACE INTO machine_network VALUES (?,?,?)",
                     (machine, 0, updated_at))
    if bios:
        conn.execute(
            "INSERT OR REPLACE INTO machine_bios (machine, support, vendor, bios_version, "
            "password_set, reported_at) VALUES (?,?,?,?,?,?)",
            (machine, "supported", "Dell Inc.", "1.21.0", None, updated_at),
        )
    if procs:
        conn.execute(
            "INSERT OR REPLACE INTO machine_processes VALUES (?,?,?,?)",
            (machine, '{"processes":[{"pid":1},{"pid":2}],"cpu_cores":8,"mem_total_mb":16384}',
             updated_at, updated_at),
        )
    conn.commit()
    conn.close()


DIAGNOSTICS = {
    "has_sensors": True, "cpu_load_pct": 12.5, "memory_load_pct": 61.0,
    "mem_used_gb": 9.8, "mem_total_gb": 16.0, "gpu_temp": None, "gpu_load_pct": None,
    "disk_load_pct": 3.0, "fan_rpm": 1200.0, "net_rx_bps": 1000, "net_tx_bps": 500,
    "cpu_power_w": 35.0, "gpu_power_w": None,
    "disks": [
        {"name": "C: (Windows)", "used_gb": 400.0, "total_gb": 500.0, "used_pct": 80.0},
        {"name": "D: (Data)", "used_gb": 100.0, "total_gb": 1000.0, "used_pct": 10.0},
    ],
}


# =======================================================================================
print("\n-- custom fields --")
seed_schema()

err, field = rules.save_field(DB, "location", "Location", rules.KIND_TEXT, actor="t", now=NOW)
check("create a text field", err is None and field["name"] == "location")

err, _ = rules.save_field(DB, "Bad Name", "x", rules.KIND_TEXT)
check("rejects an invalid name", err is not None)

err, _ = rules.save_field(DB, "headcount", "Headcount", rules.KIND_NUMBER, now=NOW)
check("create a number field", err is None)

err, _ = rules.save_field(DB, "headcount", "Headcount", rules.KIND_TEXT, now=NOW)
check("refuses to change an existing field's kind", err is not None)

err, _ = rules.save_field(DB, "site", "Site", rules.KIND_CHOICE,
                          choices=["Branch 1", "Branch 2"], default_value="Branch 1", now=NOW)
check("create a choice field with a default", err is None)

err, _ = rules.save_field(DB, "site2", "Site2", rules.KIND_CHOICE, choices=[], now=NOW)
check("a choice field needs choices", err is not None)

err, _ = rules.save_field(DB, "site3", "Site3", rules.KIND_CHOICE,
                          choices=["a"], default_value="zzz", now=NOW)
check("default must be one of the choices", err is not None)

err, _ = rules.set_machine_field(DB, "PC1", "location", "Reception", now=NOW)
check("set a field value", err is None)

err, _ = rules.set_machine_field(DB, "PC1", "headcount", "not-a-number", now=NOW)
check("rejects a non-numeric value on a number field", err is not None)

err, _ = rules.set_machine_field(DB, "PC1", "nosuch", "x", now=NOW)
check("rejects an unknown field", err is not None)

err, count = rules.set_machine_field_bulk(DB, ["PC1", "PC2", "PC3"], "site", "Branch 2", now=NOW)
check("bulk set across a selection", err is None and count == 3)

err, count = rules.set_machine_field_bulk(DB, ["PC1", "PC2"], "site", "Nope", now=NOW)
check("bulk set validates once, before writing", err is not None and count == 0)
check("...and wrote nothing", rules.get_machine_fields(DB, "PC1")["site"] == "Branch 2")

rules.set_machine_field(DB, "PC1", "location", "", now=NOW)
check("clearing a value deletes the row", "location" not in rules.get_machine_fields(DB, "PC1"))

_, value = rules.coerce_field_value(rules.KIND_BOOL, "yes")
check("bool coercion accepts yes", value is True)
_, value = rules.coerce_field_value(rules.KIND_NUMBER, "12")
check("number coercion yields an int for a whole number", value == 12 and isinstance(value, int))
_, value = rules.coerce_field_value(rules.KIND_TEXT, "   ")
check("whitespace-only is 'no value', not empty string", value is None)


# =======================================================================================
print("\n-- resolver --")
seed_machine("PC1", boot_epoch=NOW - (9 * 86400),
             sessions=[{"user": "alice", "account": "CORP\\alice", "domain": "CORP",
                        "is_console": True, "is_logon_screen": False}])
vars1 = rules.resolve_machine_vars(DB, "PC1", now=NOW, diagnostics=DIAGNOSTICS,
                                   online_window=120, enrolled=True)

check("sys.machine", vars1["sys.machine"].value == "PC1")
check("sys.uptime_days is derived from boot_epoch", vars1["sys.uptime_days"].value == 9.0)
check("sys.uptime_seconds matches", vars1["sys.uptime_seconds"].value == 9 * 86400)
check("sys.online", vars1["sys.online"].value is True)
check("sys.enrolled", vars1["sys.enrolled"].value is True)
check("hw.manufacturer", vars1["hw.manufacturer"].value == "Dell Inc.")
check("metric.cpu_temp from last_temp", vars1["metric.cpu_temp"].value == 55.0)
check("metric.memory_load_pct from diagnostics", vars1["metric.memory_load_pct"].value == 61.0)
check("absent gpu reads UNKNOWN", vars1["metric.gpu_temp"].value is UNKNOWN)

check("disk.c.used_pct", vars1["disk.c.used_pct"].value == 80.0)
check("disk.c.free_gb is computed", vars1["disk.c.free_gb"].value == 100.0)
check("disk.d.used_pct", vars1["disk.d.used_pct"].value == 10.0)
check("disk.count", vars1["disk.count"].value == 2)
check("disk.max_used_pct is the worst drive", vars1["disk.max_used_pct"].value == 80.0)
check("disk.min_free_gb is the tightest drive", vars1["disk.min_free_gb"].value == 100.0)

check("session.user", vars1["session.user"].value == "alice")
check("session.count", vars1["session.count"].value == 1)
check("session.console_active", vars1["session.console_active"].value is True)
check("net.ipv4", vars1["net.ipv4"].value == "10.0.0.5")
check("net.fast_startup false is a value, not UNKNOWN", vars1["net.fast_startup"].value is False)
check("bios.version", vars1["bios.version"].value == "1.21.0")
check("bios.password_set NULL reads UNKNOWN", vars1["bios.password_set"].value is UNKNOWN)
check("ad.ou", vars1["ad.ou"].value == "OU=Sales,DC=corp")
check("ad.disabled", vars1["ad.disabled"].value is False)
check("proc.count is UNKNOWN when nobody is watching",
      vars1["proc.count"].value is UNKNOWN)
check("field default applies with no value set", vars1["field.site"].value == "Branch 2")
check("field with no value and no default is UNKNOWN", vars1["field.location"].value is UNKNOWN)
check("field.headcount unset is UNKNOWN", vars1["field.headcount"].value is UNKNOWN)

# A machine with no sensor block at all.
seed_machine("PC2", sessions=None, bios=False, nics=False)
vars2 = rules.resolve_machine_vars(DB, "PC2", now=NOW, diagnostics=None, online_window=120)
check("no sensors -> metric UNKNOWN", vars2["metric.cpu_load_pct"].value is UNKNOWN)
check("no sensors -> disk.count UNKNOWN, not 0", vars2["disk.count"].value is UNKNOWN)
check("no bios row -> UNKNOWN", vars2["bios.version"].value is UNKNOWN)
check("no remote row -> session.count UNKNOWN, not 0",
      vars2["session.count"].value is UNKNOWN)
check("machine name still resolves", vars2["sys.machine"].value == "PC2")

# Staleness: an offline machine's last reading must not read as current.
seed_machine("PC3", updated_at=NOW - 7200, boot_epoch=NOW - (30 * 86400))
vars3 = rules.resolve_machine_vars(DB, "PC3", now=NOW, diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("stale machine is offline", vars3["sys.online"].value is False)
check("stale temp reads UNKNOWN", vars3["metric.cpu_temp"].value is UNKNOWN)
check("stale uptime reads UNKNOWN", vars3["sys.uptime_days"].value is UNKNOWN)
check("stale value still reports its age", vars3["metric.cpu_temp"].age_seconds == 7200)
check("identity survives staleness", vars3["hw.manufacturer"].value == "Dell Inc.")
check("bios has no expiry, so it survives too", vars3["bios.version"].value == "1.21.0")

# Logon screen is not a signed-in user.
seed_machine("PC4", sessions=[{"user": "", "is_logon_screen": True, "is_console": True}])
vars4 = rules.resolve_machine_vars(DB, "PC4", now=NOW, diagnostics=None, online_window=120)
check("logon screen is not a session", vars4["session.count"].value == 0)
check("logon screen has no user", vars4["session.user"].value is UNKNOWN)
check("at_logon_screen is true", vars4["session.at_logon_screen"].value is True)

# A machine whose timestamp is unparseable must not read as fresh.
conn = sqlite3.connect(DB)
conn.execute("UPDATE machine_info SET updated_at = 'not a date' WHERE machine = 'PC4'")
conn.commit()
conn.close()
vars4b = rules.resolve_machine_vars(DB, "PC4", now=NOW, diagnostics=DIAGNOSTICS,
                                    online_window=120)
check("unparseable timestamp -> live values UNKNOWN",
      vars4b["metric.cpu_load_pct"].value is UNKNOWN)


# =======================================================================================
print("\n-- expression parser --")
EXTRA = rules.field_variables(DB)


def parse(text):
    return rules.parse_expression(text, EXTRA)


err, ast = parse("sys.uptime_days > 7")
check("simple comparison parses", err is None and ast == {
    "var": "sys.uptime_days", "cmp": ">", "value": 7})

err, ast = parse("sys.uptime_days > 7d")
check("duration literal against a days variable means 7 days",
      err is None and ast["value"] == 7)

err, ast = parse("sys.uptime_seconds > 7d")
check("...and against a seconds variable means 604800",
      err is None and ast["value"] == 604800)

err, ast = parse("sys.uptime_seconds > 12h")
check("hours duration", err is None and ast["value"] == 43200)

err, ast = parse("metric.cpu_temp > 7d")
check("duration against a non-duration variable is refused", err is not None)

err, ast = parse("sys.uptime_days > 7 and session.count > 0")
check("and parses", err is None and ast["op"] == "and" and len(ast["nodes"]) == 2)

err, ast = parse("sys.uptime_days > 7 or (metric.cpu_temp > 90 and session.count > 0)")
check("parens and precedence", err is None and ast["op"] == "or"
      and ast["nodes"][1]["op"] == "and")

err, ast = parse("not sys.online")
check("bare bool with no operator is refused", err is not None)

err, ast = parse("not (sys.online == true)")
check("not parses", err is None and ast["op"] == "not")

err, ast = parse('ad.os contains "Windows 11"')
check("contains with a quoted string", err is None and ast["value"] == "Windows 11")

err, ast = parse('ad.os not contains "Server"')
check("not contains", err is None and ast["cmp"] == "not_contains")

err, ast = parse('hw.serial_number starts with "5CG"')
check("two-word 'starts with'", err is None and ast["cmp"] == "starts_with")

err, ast = parse('hw.serial_number starts_with "5CG"')
check("underscored 'starts_with'", err is None and ast["cmp"] == "starts_with")

err, ast = parse("bios.password_set is unknown")
check("is unknown", err is None and ast["cmp"] == "is_unknown")

err, ast = parse("bios.password_set is not unknown")
check("is not unknown == is known", err is None and ast["cmp"] == "is_known")

err, ast = parse('field.site in ["Branch 1", "Branch 2"]')
check("in with a list", err is None and ast["cmp"] == "in" and len(ast["value"]) == 2)

err, ast = parse('field.site not in ["Branch 1"]')
check("not in", err is None and ast["cmp"] == "not_in")

err, ast = parse("sys.status == online")
check("bare word as a value", err is None and ast["value"] == "online")

err, ast = parse("disk.c.free_gb < 10")
check("per-volume variable parses", err is None)

err, ast = parse("disk.q.free_gb < 10")
check("any drive letter is valid, even one nobody has reported yet", err is None)

err, ast = parse("nosuch.variable > 1")
check("unknown variable is refused", err is not None and "unknown variable" in err)

err, ast = parse("sys.online > 5")
check("ordering operator on a bool is refused", err is not None)

err, ast = parse("metric.cpu_temp contains 'x'")
check("contains on a number is refused", err is not None)

err, ast = parse("sys.uptime_days >")
check("truncated expression is refused", err is not None)

err, ast = parse("sys.uptime_days > 7 and")
check("dangling 'and' is refused", err is not None)

err, ast = parse("")
check("empty expression is refused", err is not None)

err, ast = parse("sys.uptime_days > 7)")
check("unbalanced paren is refused", err is not None)

err, ast = parse('ad.os matches "Windows 1*"')
check("a wildcard pattern is accepted", err is None)

err, ast = parse('ad.os matches "%s"' % ("a" * 250))
check("an over-long pattern is refused", err is not None)

err, ast = parse('field.headcount > 5')
check("custom number field parses", err is None and ast["value"] == 5)

err, ast = parse('field.location == "Reception"')
check("custom text field parses", err is None)


print("\n-- formatter round-trip --")
ROUND_TRIP = [
    "sys.uptime_days > 7",
    "sys.uptime_days > 7 and session.count > 0",
    "sys.uptime_days > 7 or (metric.cpu_temp > 90 and session.count > 0)",
    'ad.os contains "Windows 11"',
    'ad.os not contains "Server"',
    'hw.serial_number starts with "5CG"',
    "bios.password_set is unknown",
    'field.site in ["Branch 1", "Branch 2"]',
    "not (sys.online == true)",
    "disk.max_used_pct >= 90",
]
for text in ROUND_TRIP:
    err1, ast1 = parse(text)
    if err1:
        check(f"round-trip parses: {text}", False)
        continue
    rendered = rules.format_expression(ast1)
    err2, ast2 = parse(rendered)
    check(f"round-trip: {text!r} -> {rendered!r}", err2 is None and ast1 == ast2)

err, ast = parse("sys.uptime_days > 7d")
check("duration normalises on the way out",
      rules.format_expression(ast) == "sys.uptime_days > 7")


# =======================================================================================
print("\n-- evaluator (three-valued) --")
V = rules.Value


def vars_of(**kwargs):
    out = {}
    for name, value in kwargs.items():
        name = name.replace("__", ".")
        var = rules.lookup_variable(name, EXTRA)
        out[name] = V(value, var.kind if var else rules.KIND_TEXT, 0)
    return out


def ev(text, variables):
    err, ast = parse(text)
    assert err is None, err
    return rules.evaluate(ast, variables)


check("true", ev("sys.uptime_days > 7", vars_of(sys__uptime_days=9)) is True)
check("false", ev("sys.uptime_days > 7", vars_of(sys__uptime_days=3)) is False)
check("boundary is exclusive for >", ev("sys.uptime_days > 7", vars_of(sys__uptime_days=7)) is False)
check(">= includes the boundary", ev("sys.uptime_days >= 7", vars_of(sys__uptime_days=7)) is True)
check("missing variable is UNKNOWN", ev("sys.uptime_days > 7", {}) is UNKNOWN)
check("UNKNOWN value is UNKNOWN",
      ev("sys.uptime_days > 7", vars_of(sys__uptime_days=UNKNOWN)) is UNKNOWN)

# The property the whole design turns on.
check("UNKNOWN does not read as zero",
      ev("disk.c.free_gb < 10", vars_of(**{"disk__c__free_gb": UNKNOWN})) is UNKNOWN)

t = vars_of(sys__uptime_days=9, session__count=1)
u = vars_of(sys__uptime_days=9, session__count=UNKNOWN)
f = vars_of(sys__uptime_days=1, session__count=UNKNOWN)
check("and: true and true -> true", ev("sys.uptime_days > 7 and session.count > 0", t) is True)
check("and: true and unknown -> unknown",
      ev("sys.uptime_days > 7 and session.count > 0", u) is UNKNOWN)
check("and: false and unknown -> FALSE (a false conjunct settles it)",
      ev("sys.uptime_days > 7 and session.count > 0", f) is False)
check("or: true or unknown -> TRUE",
      ev("sys.uptime_days > 7 or session.count > 0", u) is True)
check("or: false or unknown -> unknown",
      ev("sys.uptime_days > 7 or session.count > 0", f) is UNKNOWN)
check("not unknown -> unknown",
      ev("not (session.count > 0)", u) is UNKNOWN)
check("not true -> false", ev("not (sys.uptime_days > 7)", t) is False)

check("is_unknown answers definitely about an absent value",
      ev("bios.password_set is unknown", vars_of(bios__password_set=UNKNOWN)) is True)
check("is_known answers definitely too",
      ev("bios.password_set is known", vars_of(bios__password_set=UNKNOWN)) is False)
check("is_known on a present value",
      ev("bios.password_set is known", vars_of(bios__password_set=True)) is True)

check("text equality is case-insensitive",
      ev('ad.ou == "ou=sales,dc=corp"', vars_of(ad__ou="OU=Sales,DC=corp")) is True)
check("contains is case-insensitive",
      ev('ad.os contains "windows"', vars_of(ad__os="Windows 11 Pro")) is True)
check("starts_with", ev('hw.serial_number starts with "5cg"',
                        vars_of(hw__serial_number="5CG1234")) is True)
check("matches (wildcard)", ev('ad.os matches "Windows 1*"',
                              vars_of(ad__os="Windows 11 Pro")) is True)
check("matches is anchored, not a substring search",
      ev('ad.os matches "Windows"', vars_of(ad__os="Windows 11 Pro")) is False)
check("...so a leading star makes it one",
      ev('ad.os matches "*11*"', vars_of(ad__os="Windows 11 Pro")) is True)
check("? matches exactly one character",
      ev('ad.os matches "Windows 1? Pro"', vars_of(ad__os="Windows 11 Pro")) is True)
check("a regex is now a literal, not a pattern",
      ev('ad.os matches "Windows (10|11)"', vars_of(ad__os="Windows 11 Pro")) is False)
# A literal `*` in the SUBJECT used to satisfy the matcher's literal branch before its
# wildcard branch, so the pattern's star was spent matching that one character instead of
# the run it should have swallowed. Paths, command lines and probe output all contain
# asterisks, so this was reachable without anyone writing a strange pattern.
check("a literal * in the text does not eat the pattern's star",
      rules.wildcard_match("*x", "*abx") is True)
check("...nor when the star is mid-pattern",
      rules.wildcard_match("a*b", "a*zb") is True)
check("a literal * still matches a literal * with no wildcard in play",
      rules.wildcard_match("a*b", "ab") is True)
# The shape that used to be able to hang the evaluator. It must now be fast and simply not
# match -- the matcher cannot backtrack exponentially, so there is no pattern that spins it.
# The shape that used to be exploitable: as a regex, `(a*)`-style repetition against a long
# non-matching subject is the classic exponential blow-up. As wildcards it is answered
# immediately, and correctly -- the trailing `*` absorbs the final 'b', so it DOES match.
_evil_start = time.time()
_evil = ev('ad.os matches "%s"' % ("a*" * 40), vars_of(ad__os="a" * 200 + "b"))
check("a catastrophic-looking pattern is answered promptly",
      _evil is True and (time.time() - _evil_start) < 1.0)
# ...and the same shape with no trailing wildcard fails just as fast, which is the case a
# backtracking engine would have spun on.
_evil_start = time.time()
_evil2 = ev('ad.os matches "%sc"' % ("a*" * 40), vars_of(ad__os="a" * 200 + "b"))
check("...and so is the non-matching variant",
      _evil2 is False and (time.time() - _evil_start) < 1.0)
check("in", ev('field.site in ["Branch 1", "Branch 2"]',
               vars_of(field__site="Branch 2")) is True)
check("not_in", ev('field.site not in ["Branch 1"]',
                   vars_of(field__site="Branch 2")) is True)
check("bool equality", ev("sys.online == true", vars_of(sys__online=True)) is True)
check("bool equality false", ev("sys.online == true", vars_of(sys__online=False)) is False)

# A real end-to-end evaluation against the resolver's own output.
check("end-to-end: PC1 is up 9 days with a user logged in",
      ev("sys.uptime_days > 7 and session.count > 0", vars1) is True)
check("end-to-end: PC3 is stale so the same rule is UNKNOWN, not true",
      ev("sys.uptime_days > 7 and session.count > 0", vars3) is UNKNOWN)
check("end-to-end: disk aggregate", ev("disk.max_used_pct >= 80", vars1) is True)
check("end-to-end: custom field", ev('field.site == "Branch 2"', vars1) is True)


print("\n-- explain --")
err, ast = parse("sys.uptime_days > 7 and session.count > 0")
detail = rules.explain(ast, vars1)
check("explain reports the overall result", detail["result"] == "true")
check("explain reports each leaf's actual value",
      detail["nodes"][0]["actual"] == 9.0 and detail["nodes"][0]["result"] == "true")
detail3 = rules.explain(ast, vars3)
check("explain marks an unknown leaf",
      detail3["nodes"][0]["known"] is False and detail3["nodes"][0]["result"] == "unknown")


print("\n-- templating --")
check("substitutes a variable",
      rules.render_template("Up for {{sys.uptime_days}} days", vars1) == "Up for 9 days")
check("drops the trailing .0 on whole numbers",
      rules.render_template("{{metric.mem_total_gb}}", vars1) == "16")
check("one decimal on a fractional number",
      rules.render_template("{{metric.cpu_temp}}", vars1) == "55")
check("bool renders as yes/no",
      rules.render_template("{{sys.online}}", vars1) == "yes")
check("unknown renders as 'unknown', not as literal braces",
      rules.render_template("{{metric.gpu_temp}}", vars1) == "unknown")
check("an unresolvable name renders as 'unknown' too",
      rules.render_template("{{nosuch.thing}}", vars1) == "unknown")
check("text with no placeholders is untouched",
      rules.render_template("Please restart.", vars1) == "Please restart.")
check("template_names finds referenced variables",
      rules.template_names("{{sys.machine}} / {{sys.uptime_days}}")
      == ["sys.machine", "sys.uptime_days"])


print("\n-- limits --")
deep = {"var": "sys.uptime_days", "cmp": ">", "value": 1}
for _ in range(rules.MAX_CONDITION_DEPTH + 2):
    deep = {"op": "not", "nodes": [deep]}
err, _ = rules.validate_condition(deep, EXTRA)
check("over-deep condition is refused", err is not None)

wide = {"op": "and", "nodes": [{"var": "sys.uptime_days", "cmp": ">", "value": 1}
                               for _ in range(rules.MAX_CONDITION_NODES + 5)]}
err, _ = rules.validate_condition(wide, EXTRA)
check("over-wide condition is refused", err is not None)

err, _ = parse("sys.uptime_days > 7 and " * 400 + "sys.uptime_days > 7")
check("an over-long expression is refused", err is not None)

err, _ = rules.validate_condition({"op": "and", "nodes": []}, EXTRA)
check("an empty group is refused at save time", err is not None)

# Error text reaches a browser, so it must never carry what a stdlib exception would put in
# it. Parser messages ARE ours and stay verbatim (they are the point of the text editor);
# anything built from a foreign exception must not echo the input back.
err = rules.validate_pattern("a" * (rules.MAX_PATTERN_CHARS + 1))
check("an over-long pattern is refused", err is not None)
check("...without echoing the pattern back", "aaaa" not in err)

err, _ = parse("sys.uptime_days > 7)")
check("a parse error still names the offending token and position",
      err and "character" in err)
check("...and carries no traceback", err and "Traceback" not in err and "File \"" not in err)
check("...and reads UNKNOWN if one somehow reaches the evaluator",
      rules.evaluate({"op": "and", "nodes": []}, vars1) is UNKNOWN)


# =======================================================================================
print("\n-- targeting --")
import fleet
import alerts
import scripts

fleet.init_fleet_db(DB)
alerts.init_alerts_db(DB)

seed_machine("SALES-1")
seed_machine("SALES-2")
conn = sqlite3.connect(DB)
conn.execute("UPDATE machine_info SET ad_ou='OU=Lab,OU=IT,DC=corp', "
             "ad_dn='CN=LAB-1,OU=Lab,OU=IT,DC=corp' WHERE machine='PC2'")
conn.execute("UPDATE machine_info SET ad_ou='OU=IT,DC=corp' WHERE machine='PC3'")
conn.commit()
conn.close()


def targets(spec):
    err, clean = rules.validate_target(spec, EXTRA)
    assert err is None, err
    return rules.resolve_targets(DB, clean)


everyone = targets({"include": [{"kind": "all"}]})
check("target all", "PC1" in everyone and "SALES-1" in everyone)

subset = targets({"include": [{"kind": "all"}],
                  "exclude": [{"kind": "machines", "machines": ["PC1"]}]})
check("all except one", "PC1" not in subset and "PC2" in subset)

explicit = targets({"include": [{"kind": "machines", "machines": ["PC1", "SALES-2"]}]})
check("explicit machine list", explicit == ["PC1", "SALES-2"])

ou = targets({"include": [{"kind": "ad_ou", "ou": "OU=Sales,DC=corp"}]})
check("OU selector", "PC1" in ou and "PC2" not in ou)

nested = targets({"include": [{"kind": "ad_ou", "ou": "OU=IT,DC=corp",
                               "include_children": True}]})
check("OU includes child OUs", "PC2" in nested and "PC3" in nested)

exact = targets({"include": [{"kind": "ad_ou", "ou": "OU=IT,DC=corp",
                              "include_children": False}]})
check("OU without children excludes the child OU", "PC3" in exact and "PC2" not in exact)

ou_minus = targets({"include": [{"kind": "ad_ou", "ou": "OU=Sales,DC=corp"}],
                    "exclude": [{"kind": "machines", "machines": ["SALES-1"]}]})
check("this OU except that one PC", "SALES-1" not in ou_minus and "PC1" in ou_minus)

by_field = targets({"include": [{"kind": "field", "field": "site", "value": "Branch 2"}]})
check("field selector matches explicit values", "PC1" in by_field)

err, _ = rules.validate_target({"include": []}, EXTRA)
check("a target with no include is refused, not defaulted to 'all'", err is not None)

err, _ = rules.validate_target({"include": [{"kind": "nope"}]}, EXTRA)
check("unknown selector kind is refused", err is not None)

err, _ = rules.validate_target({"include": [{"kind": "field", "field": "nosuch",
                                             "value": "x"}]}, EXTRA)
check("field selector on an unknown field is refused", err is not None)


# =======================================================================================
print("\n-- action validation --")


def val_actions(actions, allow_command=True):
    return rules.validate_actions(actions, EXTRA, allow_command)


err, _ = val_actions([{"type": "alert", "params": {"text": "hot"}}])
check("alert action validates", err is None)

err, _ = val_actions([{"type": "alert", "params": {}}])
check("alert needs text", err is not None)

err, _ = val_actions([{"type": "command",
                       "params": {"command_type": "restart", "params": {"delay_seconds": 60}}}])
check("command action validates", err is None)

err, _ = val_actions([{"type": "command", "params": {"command_type": "restart"}}],
                     allow_command=False)
check("command action refused without permission", err is not None)

err, _ = val_actions([{"type": "command", "params": {"command_type": "shell_open"}}])
check("a rule cannot issue a session-bound command", err is not None)

err, _ = val_actions([{"type": "command", "params": {"command_type": "kill_process"}}])
check("a rule cannot issue a pid-bound command", err is not None)

err, _ = val_actions([{"type": "command", "params": {"command_type": "deploy_package"}}])
check("a rule cannot issue a deployment snapshot command", err is not None)

err, _ = val_actions([{"type": "command", "params": {"command_type": "nonsense"}}])
check("unknown command refused", err is not None)

err, _ = val_actions([{"type": "snooze", "params": {"seconds": 3600}}])
check("snooze validates", err is None)

err, _ = val_actions([{"type": "snooze", "params": {"seconds": 5}}])
check("snooze below the floor is refused", err is not None)

MESSAGE = {
    "type": "show_message",
    "params": {"title": "Restart required",
               "body": "Up for {{sys.uptime_days}} days. Restart now?",
               "preset": "yes_no_later", "default_button": "later",
               "timeout_seconds": 900},
    "on_response": {
        "yes": [{"type": "command", "params": {"command_type": "restart",
                                               "params": {"delay_seconds": 60}}}],
        "later": [{"type": "snooze", "params": {"seconds": 14400}}],
        "no": [{"type": "alert", "params": {"text": "Restart declined on {{sys.machine}}"}}],
        "timeout": [{"type": "snooze", "params": {"seconds": 3600}}],
        "no_session": [{"type": "snooze", "params": {"seconds": 1800}}],
    },
}
err, clean = val_actions([MESSAGE])
check("interactive message validates", err is None)
check("preset expands to three buttons",
      err is None and [b["id"] for b in clean[0]["params"]["buttons"]] == ["yes", "no", "later"])

bad = json.loads(json.dumps(MESSAGE)) if False else None
import copy
bad = copy.deepcopy(MESSAGE)
bad["on_response"]["Yes"] = bad["on_response"].pop("yes")
err, _ = val_actions([bad])
check("an outcome that cannot happen is refused (typo catcher)", err is not None)

bad = copy.deepcopy(MESSAGE)
bad["params"]["default_button"] = "cancel"
err, _ = val_actions([bad])
check("default button must be one of the buttons", err is not None)

bad = copy.deepcopy(MESSAGE)
err, _ = val_actions([bad], allow_command=False)
check("a message whose 'yes' issues a command needs the command permission", err is not None)

bad = copy.deepcopy(MESSAGE)
bad["on_response"]["no"] = [copy.deepcopy(MESSAGE)]
err, _ = val_actions([bad])
check("a message cannot answer a message", err is not None)

err, _ = val_actions([{"type": "webhook", "params": {"url": "http://example.com/hook"}}])
check("plain-http webhook is refused", err is not None)

err, _ = val_actions([{"type": "webhook", "params": {"url": "https://example.com/hook"}}])
check("https webhook validates", err is None)

err, _ = val_actions([{"type": "email", "params": {"to": ["a@b.com"], "subject": "s",
                                                   "body": "b"}}])
check("email validates", err is None)

err, _ = val_actions([{"type": "email", "params": {"to": ["a@b.com"],
                                                   "subject": "s\nBcc: x@y.com", "body": "b"}}])
check("header injection in the subject is refused", err is not None)

check("actions_include_command sees a nested follow-up",
      rules.actions_include_command([MESSAGE]) is True)
check("...and is false for a message with no command anywhere",
      rules.actions_include_command([{"type": "alert", "params": {"text": "x"}}]) is False)


# =======================================================================================
print("\n-- rule store --")
BASE_RULE = {
    "name": "Uptime nag",
    "target": {"include": [{"kind": "machines", "machines": ["PC1"]}]},
    "condition_text": "sys.uptime_days > 7",
    "actions": [{"type": "alert", "params": {"text": "{{sys.machine}} up {{sys.uptime_days}}d"}}],
    "for_seconds": 0,
    "cooldown_seconds": 0,
}

err, rule = rules.save_rule(DB, BASE_RULE, actor="tester", now=NOW)
check("save a rule", err is None and rule["id"])
check("condition text round-trips into the stored AST",
      rule["condition"] == {"var": "sys.uptime_days", "cmp": ">", "value": 7})
check("condition_text is stored back", rule["condition_text"] == "sys.uptime_days > 7")
RULE_ID = rule["id"]

cmd_rule = dict(BASE_RULE, name="Reboot",
                actions=[{"type": "command", "params": {"command_type": "restart"}}],
                cooldown_seconds=60)
err, r2 = rules.save_rule(DB, cmd_rule, actor="tester", now=NOW,
                          command_cooldown_floor=3600)
check("a command rule's cooldown is raised to the floor",
      err is None and r2["cooldown_seconds"] == 3600)

wide_rule = dict(BASE_RULE, name="Wide", max_targets_per_tick=9999)
err, r3 = rules.save_rule(DB, wide_rule, actor="tester", now=NOW, max_targets_cap=25)
check("max_targets_per_tick is clamped to the fleet-wide cap",
      err is None and r3["max_targets_per_tick"] == 25)

err, _ = rules.save_rule(DB, dict(BASE_RULE, name=""), now=NOW)
check("a rule needs a name", err is not None)

err, _ = rules.save_rule(DB, dict(BASE_RULE, condition_text="nosuch.var > 1"), now=NOW)
check("a rule with an unknown variable is refused", err is not None)

err, _ = rules.save_rule(DB, dict(BASE_RULE, actions=[]), now=NOW)
check("a rule needs an action", err is not None)

rules.delete_rule(DB, r3["id"])
check("delete a rule", rules.get_rule(DB, r3["id"]) is None)


# =======================================================================================
print("\n-- evaluator --")
COMMANDS = []
_real_create = fleet.create_command


def fake_create_command(db_path, machine, command_type, params, issued_by=None,
                        ttl_seconds=None):
    COMMANDS.append({"machine": machine, "type": command_type, "params": params,
                     "issued_by": issued_by, "ttl": ttl_seconds})
    return f"cmd-{len(COMMANDS)}"


fleet.create_command = fake_create_command
rules.fleet.create_command = fake_create_command

RESOLVED = {"PC1": vars1, "PC2": vars2, "PC3": vars3}


def resolve(machine):
    return RESOLVED.get(machine) or rules.resolve_machine_vars(DB, machine, now=NOW)


# Clear the decks: only the uptime rule, targeting PC1 (up 9 days) and PC3 (stale).
rules.delete_rule(DB, r2["id"])
err, rule = rules.save_rule(DB, dict(BASE_RULE, target={
    "include": [{"kind": "machines", "machines": ["PC1", "PC3"]}]}),
    rule_id=RULE_ID, actor="tester", now=NOW)
check("rule updated to target two machines", err is None)

summary = rules.evaluate_once(DB, resolve, now=NOW, config={"actions_enabled": True})
check("fires on the machine that matches", summary["fired"] == 1)
check("does not fire on the stale machine", summary["matched"] == 1)
open_alerts = alerts.list_open(DB)
check("an alert was raised", any(a["kind"] == alerts.KIND_RULE for a in open_alerts))
rule_alert = next(a for a in open_alerts if a["kind"] == alerts.KIND_RULE)
check("alert text was templated",
      rule_alert["detail"]["text"] == "PC1 up 9d")
check("alert names the rule", rule_alert["detail"]["rule_name"] == "Uptime nag")

fires = rules.list_fires(DB, RULE_ID)
check("the fire was recorded", len(fires) == 1 and fires[0]["machine"] == "PC1")

# Cooldown.
err, rule = rules.save_rule(DB, dict(BASE_RULE, cooldown_seconds=3600, target={
    "include": [{"kind": "machines", "machines": ["PC1"]}]}),
    rule_id=RULE_ID, actor="tester", now=NOW)
summary = rules.evaluate_once(DB, resolve, now=NOW + 10)
check("cooldown blocks an immediate re-fire", summary["fired"] == 0)
summary = rules.evaluate_once(DB, resolve, now=NOW + 4000)
check("...and lets it fire once the cooldown lapses", summary["fired"] == 1)

# Debounce.
err, rule = rules.save_rule(DB, dict(BASE_RULE, for_seconds=600, cooldown_seconds=0, target={
    "include": [{"kind": "machines", "machines": ["PC1"]}]}),
    rule_id=RULE_ID, actor="tester", now=NOW)
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM rule_state WHERE rule_id=?", (RULE_ID,))
conn.commit()
conn.close()
summary = rules.evaluate_once(DB, resolve, now=NOW)
check("debounce: does not fire on first match", summary["fired"] == 0)
summary = rules.evaluate_once(DB, resolve, now=NOW + 300)
check("debounce: still waiting halfway through", summary["fired"] == 0)
summary = rules.evaluate_once(DB, resolve, now=NOW + 700)
check("debounce: fires once it has held long enough", summary["fired"] == 1)

# The condition going UNKNOWN must reset the clock, not hold it.
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM rule_state WHERE rule_id=?", (RULE_ID,))
conn.commit()
conn.close()
rules.evaluate_once(DB, resolve, now=NOW)
RESOLVED["PC1"] = vars3          # PC1 "goes stale" -> condition UNKNOWN
rules.evaluate_once(DB, resolve, now=NOW + 300)
RESOLVED["PC1"] = vars1          # comes back
summary = rules.evaluate_once(DB, resolve, now=NOW + 700)
check("losing contact resets the debounce clock rather than counting toward it",
      summary["fired"] == 0)

# Command kill switch.
COMMANDS.clear()
err, rule = rules.save_rule(DB, dict(BASE_RULE, name="Reboot", for_seconds=0,
                                     cooldown_seconds=0,
                                     actions=[{"type": "command",
                                               "params": {"command_type": "restart"}}],
                                     target={"include": [{"kind": "machines",
                                                          "machines": ["PC1"]}]}),
                            rule_id=RULE_ID, actor="tester", now=NOW,
                            command_cooldown_floor=0)
summary = rules.evaluate_once(DB, resolve, now=NOW + 10000,
                              config={"command_actions_enabled": False})
check("command action is suppressed while the kill switch is off", not COMMANDS)
fires = rules.list_fires(DB, RULE_ID, limit=1)
check("...and the suppression is recorded, not silent",
      fires[0]["actions"][0].get("skipped"))

summary = rules.evaluate_once(DB, resolve, now=NOW + 20000,
                              config={"command_actions_enabled": True})
check("command action runs once armed", len(COMMANDS) == 1)
check("the command is attributed to the rule",
      COMMANDS[0]["issued_by"] == f"rule:{RULE_ID}")

# Per-tick cap.
COMMANDS.clear()
many = [f"CAP-{i}" for i in range(10)]
conn = sqlite3.connect(DB)
for name in many:
    conn.execute("INSERT OR REPLACE INTO machine_info (machine, updated_at, boot_epoch) "
                 "VALUES (?,?,?)", (name, ts(NOW), NOW - 9 * 86400))
conn.commit()
conn.close()
for name in many:
    RESOLVED[name] = rules.resolve_machine_vars(DB, name, now=NOW, online_window=120)
err, rule = rules.save_rule(DB, dict(BASE_RULE, name="Capped", for_seconds=0,
                                     cooldown_seconds=0, max_targets_per_tick=3,
                                     actions=[{"type": "alert", "params": {"text": "x"}}],
                                     target={"include": [{"kind": "machines",
                                                          "machines": many}]}),
                            rule_id=RULE_ID, actor="tester", now=NOW, max_targets_cap=25)
summary = rules.evaluate_once(DB, resolve, now=NOW + 30000)
check("per-tick cap holds the rest back", summary["fired"] == 3)
check("...and reports how many were held back",
      summary["capped"] and summary["capped"][0]["held_back"] == 7)


# =======================================================================================
print("\n-- message round trip --")
COMMANDS.clear()
err, rule = rules.save_rule(DB, dict(BASE_RULE, name="Restart nag", for_seconds=0,
                                     cooldown_seconds=0, actions=[MESSAGE],
                                     target={"include": [{"kind": "machines",
                                                          "machines": ["PC1"]}]}),
                            rule_id=RULE_ID, actor="tester", now=NOW,
                            command_cooldown_floor=0)
check("a message rule saves", err is None)
summary = rules.evaluate_once(DB, resolve, now=NOW + 40000,
                              config={"command_actions_enabled": True})
check("a show_message command is queued", len(COMMANDS) == 1
      and COMMANDS[0]["type"] == "show_message")
check("the body was templated",
      COMMANDS[0]["params"]["body"] == "Up for 9 days. Restart now?")
check("TTL is stretched past the dialog timeout", COMMANDS[0]["ttl"] > 900)
MSG_CMD_ID = "cmd-1"

# The user clicks Yes -> the restart is issued.
out = rules.handle_message_result(DB, MSG_CMD_ID, status=fleet.STATUS_DONE,
                                  result={"outcome": "yes"}, now=NOW + 40100,
                                  config={"command_actions_enabled": True})
check("Yes routes to the restart", out and out["outcome"] == "yes")
check("...and the restart command was issued",
      any(c["type"] == "restart" for c in COMMANDS))

# A repeated result must not fire it twice.
before = len(COMMANDS)
again = rules.handle_message_result(DB, MSG_CMD_ID, status=fleet.STATUS_DONE,
                                    result={"outcome": "yes"}, now=NOW + 40200,
                                    config={"command_actions_enabled": True})
check("a retried result is ignored", again is None and len(COMMANDS) == before)

# Later -> snooze, and the rule must not re-ask.
COMMANDS.clear()
summary = rules.evaluate_once(DB, resolve, now=NOW + 50000,
                              config={"command_actions_enabled": True})
check("the rule asks again later", len(COMMANDS) == 1)
out = rules.handle_message_result(DB, f"cmd-{len(COMMANDS)}", status=fleet.STATUS_DONE,
                                  result={"outcome": "later"}, now=NOW + 50100,
                                  config={"command_actions_enabled": True})
check("Later routes to snooze", out and out["actions"][0]["snoozed_until"] > NOW + 50100)
COMMANDS.clear()
summary = rules.evaluate_once(DB, resolve, now=NOW + 51000,
                              config={"command_actions_enabled": True})
check("a snoozed machine is not asked again", not COMMANDS)
summary = rules.evaluate_once(DB, resolve, now=NOW + 70000,
                              config={"command_actions_enabled": True})
check("...until the snooze lapses", len(COMMANDS) == 1)

# No user logged in is a routable outcome, not a failure.
out = rules.handle_message_result(DB, f"cmd-{len(COMMANDS)}", status=fleet.STATUS_DONE,
                                  result={"outcome": "no_session"}, now=NOW + 70100,
                                  config={"command_actions_enabled": True})
check("no_session routes to its own snooze", out and out["outcome"] == "no_session")

# An outcome with no mapping is recorded and does nothing.
COMMANDS.clear()
rules.evaluate_once(DB, resolve, now=NOW + 90000, config={"command_actions_enabled": True})
out = rules.handle_message_result(DB, f"cmd-{len(COMMANDS)}", status=fleet.STATUS_DONE,
                                  result={"outcome": "dismissed"}, now=NOW + 90100)
check("an unmapped outcome is a recorded no-op", out and out["actions"] == []
      and out["outcome"] == "dismissed")
fires = rules.list_fires(DB, RULE_ID, limit=1)
check("...and the outcome is visible in the history", fires[0]["outcome"] == "dismissed")

# A command that was not issued by a rule is ignored entirely.
check("a non-rule command result is ignored",
      rules.handle_message_result(DB, "cmd-does-not-exist") is None)

# ---------------------------------------------------------------------------------------
# REGRESSION: a rule whose action list puts a command BEFORE its show_message.
#
# rule_fires.command_id is what handle_message_result looks an incoming answer up by, and
# _fire used to store the FIRST dispatched command id. With the actions in this order that
# was the gpupdate, not the dialog -- so the answer matched no fire row, handle_message_result
# returned at its early guard, and "yes -> restart" silently never ran. The order is legal and
# nothing warned about it.
COMMANDS.clear()
err, _ = rules.save_rule(DB, dict(BASE_RULE, name="Command first", for_seconds=0,
                                  cooldown_seconds=0,
                                  actions=[{"type": "command",
                                            "params": {"command_type": "gpupdate", "params": {}}},
                                           MESSAGE],
                                  target={"include": [{"kind": "machines",
                                                       "machines": ["PC1"]}]}),
                         rule_id=RULE_ID, actor="tester", now=NOW, command_cooldown_floor=0)
check("a command-then-message rule saves", err is None)
rules.evaluate_once(DB, resolve, now=NOW + 120000, config={"command_actions_enabled": True})
check("both commands were issued", len(COMMANDS) == 2
      and {c["type"] for c in COMMANDS} == {"gpupdate", "show_message"})
# The fire row must point at the DIALOG, not at the command that happened to go first.
message_index = next(i for i, c in enumerate(COMMANDS, start=1) if c["type"] == "show_message")
out = rules.handle_message_result(DB, f"cmd-{message_index}", status=fleet.STATUS_DONE,
                                  result={"outcome": "yes"}, now=NOW + 120100,
                                  config={"command_actions_enabled": True})
check("the answer still routes when a command action came first",
      out is not None and out["outcome"] == "yes")
check("...and the restart was actually issued",
      any(c["type"] == "restart" for c in COMMANDS))

# ---------------------------------------------------------------------------------------
# The kill switch must be LOUD. With command actions off fleet-wide the follow-up cannot run
# -- that is the switch working -- but the operator has to be able to find out why, because
# from the user's chair this is "I pressed Yes and my PC did not restart".
COMMANDS.clear()
AUDIT = []
rules.evaluate_once(DB, resolve, now=NOW + 140000, config={"command_actions_enabled": True})
message_index = next(i for i, c in enumerate(COMMANDS, start=1) if c["type"] == "show_message")
out = rules.handle_message_result(
    DB, f"cmd-{message_index}", status=fleet.STATUS_DONE, result={"outcome": "yes"},
    now=NOW + 140100, config={"command_actions_enabled": False},
    audit=lambda actor, action, target, detail, level=None:
        AUDIT.append((action, detail, level)))
check("the answer is still recorded", out is not None and out["outcome"] == "yes")
check("the follow-up is skipped, not run",
      out["actions"] and out["actions"][0].get("skipped")
      and not any(c["type"] == "restart" for c in COMMANDS))
check("...and the skip is audited on its own",
      any(a == "rule_followup_skipped" for a, _detail, _level in AUDIT))
check("...naming the reason, so the setting is findable",
      any("command_actions_enabled" in str(detail)
          for action, detail, _level in AUDIT if action == "rule_followup_skipped"))

# ---------------------------------------------------------------------------------------
# `force` on a restart follow-up survives validate -> save -> dispatch. It reaches the agent
# as a command param (RestartExecutor adds /f), so anything that drops it silently restores
# the polite restart an app with unsaved work can veto.
FORCING = copy.deepcopy(MESSAGE)
FORCING["on_response"]["yes"] = [{"type": "command",
                                  "params": {"command_type": "restart",
                                             "params": {"delay_seconds": 60, "force": True}}}]
err, clean = val_actions([FORCING])
check("a forcing follow-up validates", err is None)
check("...and force survives validation",
      err is None
      and clean[0]["on_response"]["yes"][0]["params"]["params"]["force"] is True)

COMMANDS.clear()
err, _ = rules.save_rule(DB, dict(BASE_RULE, name="Forcing nag", for_seconds=0,
                                  cooldown_seconds=0, actions=[FORCING],
                                  target={"include": [{"kind": "machines",
                                                       "machines": ["PC1"]}]}),
                         rule_id=RULE_ID, actor="tester", now=NOW, command_cooldown_floor=0)
check("a forcing rule saves", err is None)
rules.evaluate_once(DB, resolve, now=NOW + 160000, config={"command_actions_enabled": True})
message_index = next(i for i, c in enumerate(COMMANDS, start=1) if c["type"] == "show_message")
rules.handle_message_result(DB, f"cmd-{message_index}", status=fleet.STATUS_DONE,
                            result={"outcome": "yes"}, now=NOW + 160100,
                            config={"command_actions_enabled": True})
restart = next((c for c in COMMANDS if c["type"] == "restart"), None)
check("force reaches the agent on the restart command",
      restart is not None and restart["params"].get("force") is True)

# =======================================================================================
# COMMAND PARAMETERS -- the schema fleet.py declares, enforced at the rules boundary.
print("\n-- command parameters --")

err, clean = val_actions([{"type": "command", "params": {
    "command_type": "restart", "params": {"delay_seconds": 300, "force": True}}}])
check("a restart with a countdown and force validates", err is None)
check("...and the params are stored as typed values", err is None
      and clean[0]["params"]["params"] == {"delay_seconds": 300, "force": True})

check("an out-of-range countdown is refused",
      val_actions([{"type": "command", "params": {
          "command_type": "restart", "params": {"delay_seconds": 999999}}}])[0] is not None)
check("a parameter the command does not take is refused",
      val_actions([{"type": "command", "params": {
          "command_type": "restart", "params": {"nonsense": 1}}}])[0] is not None)
check("run_script without a script is refused",
      val_actions([{"type": "command", "params": {
          "command_type": "run_script", "params": {}}}])[0] is not None)
check("an unknown shell is refused",
      val_actions([{"type": "command", "params": {"command_type": "run_script",
          "params": {"script": "Get-Date", "shell": "bash"}}}])[0] is not None)
check("install_app with neither an id nor an msi is refused",
      val_actions([{"type": "command", "params": {
          "command_type": "install_app", "params": {}}}])[0] is not None)
# Sloppy JSON types are coerced, exactly as settings does: "300" is the number the operator
# typed, and refusing it would be a transport detail leaking into their face.
err, clean = val_actions([{"type": "command", "params": {
    "command_type": "restart", "params": {"delay_seconds": "300", "force": "yes"}}}])
check("string-typed numbers and booleans are coerced", err is None
      and clean[0]["params"]["params"] == {"delay_seconds": 300, "force": True})
# A default left blank is NOT written into the rule -- the agent's default stays authoritative
# and opening a rule cannot change what was stored.
err, clean = val_actions([{"type": "command", "params": {
    "command_type": "restart", "params": {}}}])
check("an unset parameter is not materialised", err is None
      and clean[0]["params"]["params"] == {})

check("update_bios cannot be issued by a rule (its id is minted per window)",
      val_actions([{"type": "command", "params": {
          "command_type": "update_bios", "params": {"update_id": "x"}}}])[0] is not None)

# THE regression this whole change hangs on: a follow-up is validated by the same funnel as a
# top-level action. There is one _validate_action; a second path would eventually disagree.
check("a nested follow-up gets the SAME parameter validation",
      val_actions([{"type": "show_message",
                    "params": {"title": "T", "body": "B", "preset": "yes_no"},
                    "on_response": {"yes": [{"type": "command", "params": {
                        "command_type": "restart",
                        "params": {"delay_seconds": 999999}}}]}}])[0] is not None)
check("...and accepts a valid one",
      val_actions([{"type": "show_message",
                    "params": {"title": "T", "body": "B", "preset": "yes_no"},
                    "on_response": {"yes": [{"type": "command", "params": {
                        "command_type": "run_script",
                        "params": {"script": "Get-Date"}}}]}}])[0] is None)


# =======================================================================================
# TEMPLATING IN COMMAND PARAMS, and the refusal to run code containing "unknown".
print("\n-- templating command params --")

VARS_OK = {"sys.machine": rules.Value("WKS-1", True, 0)}
rendered, missing = rules.render_params_checked(
    {"script": "Write-Host {{sys.machine}}", "timeout_seconds": 60}, VARS_OK)
check("a string param is templated", rendered["script"] == "Write-Host WKS-1")
check("a non-string param is left alone", rendered["timeout_seconds"] == 60)
check("nothing is reported missing", missing == [])

rendered, missing = rules.render_params_checked({"script": "Stop-Service {{sys.machine}}"}, {})
check("an unresolvable name is REPORTED, not silently substituted", missing == ["sys.machine"])
# The behaviour that must NOT change for messages: a sentence a person reads still says
# "unknown", because there it means "missing data" rather than an instruction.
check("render_template still substitutes 'unknown' for message text",
      rules.render_template("up for {{sys.uptime_days}} days", {}) == "up for unknown days")


# =======================================================================================
# THE SCRIPT ACTION
print("\n-- the script action --")

err, _ = scripts.save_script(
    DB, "restart_svc", "Restart a service", "", "powershell",
    'Write-Host "{{sys.machine}}"\nRestart-Service -Name "{{input.service_name}}"',
    [{"name": "service_name", "required": True}], 900,
    known_variable=lambda n: rules.lookup_variable(n, None) is not None, actor="tester")
check("a script saves for the rule to reference", err is None)

SCRIPT_ACTION = {"type": "script",
                 "params": {"script": "restart_svc", "inputs": {"service_name": "Spooler"}}}
err, rule = rules.save_rule(DB, dict(BASE_RULE, name="Fix the spooler", for_seconds=0,
                                     cooldown_seconds=0, actions=[SCRIPT_ACTION],
                                     target={"include": [{"kind": "machines",
                                                          "machines": ["PC1"]}]}),
                            rule_id=RULE_ID, actor="tester", now=NOW, command_cooldown_floor=0)
check("a rule referencing it saves", err is None)

check("a reference to a missing script is refused at save",
      rules.save_rule(DB, dict(BASE_RULE, name="Ghost", actions=[
          {"type": "script", "params": {"script": "ghost", "inputs": {}}}]),
          actor="tester", now=NOW, command_cooldown_floor=0)[0] is not None)
check("a script action needs ISSUE_COMMANDS like any other command",
      rules.save_rule(DB, dict(BASE_RULE, name="NoPerm", actions=[SCRIPT_ACTION]),
                      actor="tester", now=NOW, allow_command=False,
                      command_cooldown_floor=0)[0] is not None)
check("a script counts as a command for the cooldown floor",
      rules.actions_include_command([SCRIPT_ACTION]) is True)
check("...including when it is only a follow-up",
      rules.actions_include_command([{"type": "show_message", "params": {},
                                      "on_response": {"yes": [SCRIPT_ACTION]}}]) is True)

COMMANDS.clear()
rules.evaluate_once(DB, resolve, now=NOW + 200000, config={"command_actions_enabled": True})
issued = next((c for c in COMMANDS if c["type"] == "run_script"), None)
check("firing it issues a run_script command", issued is not None)
check("...carrying the RENDERED body, not the template",
      issued and "{{" not in issued["params"]["script"]
      and "Restart-Service -Name \"Spooler\"" in issued["params"]["script"])
check("...with the script's own shell and timeout",
      issued and issued["params"]["shell"] == "powershell"
      and issued["params"]["timeout_seconds"] == 900)

# A machine whose variables cannot be resolved must not run code with "unknown" spliced in.
COMMANDS.clear()
out = rules.dispatch_actions(DB, [SCRIPT_ACTION], "PC1", {}, rule=rule, now=NOW,
                             config={**rules.DEFAULT_CONFIG, "command_actions_enabled": True},
                             allowed=True)
check("an unresolvable variable skips the script", bool(out[0].get("skipped")))
check("...and issues nothing at all", not COMMANDS)

# The fleet-wide kill switch reaches scripts, because ACTION_SCRIPT is in MUTATING_ACTIONS.
COMMANDS.clear()
out = rules.dispatch_actions(DB, [SCRIPT_ACTION], "PC1",
                             {"sys.machine": rules.Value("PC1", True, 0)}, rule=rule, now=NOW,
                             config={**rules.DEFAULT_CONFIG, "command_actions_enabled": False},
                             allowed=False, block_reason="command actions are disabled")
check("the command kill switch also stops a script", bool(out[0].get("skipped")))
check("...and issues nothing", not COMMANDS)

check("rules_using_script finds the rule",
      [r["name"] for r in rules.rules_using_script(DB, "restart_svc")] == ["Fix the spooler"])

# =======================================================================================
# "Ask once, then wait until it stops matching."
#
# The bug this exists for: a cooldown is a TIMER, so a condition that stays true re-fires
# forever. `sys.uptime_days > 7` holds until somebody reboots, so the dialog came back every
# hour and clicking No bought the person at the desk nothing.
print("\n-- fire once per match --")

COMMANDS.clear()
ONCE = dict(BASE_RULE, name="Ask once", for_seconds=0, cooldown_seconds=60,
            fire_once_per_match=True,
            actions=[{"type": "alert", "params": {"text": "up too long"}}],
            target={"include": [{"kind": "machines", "machines": ["PC1"]}]})
err, once_rule = rules.save_rule(DB, ONCE, actor="tester", now=NOW, command_cooldown_floor=0)
check("a fire-once rule saves", err is None)
check("...and the flag round-trips",
      rules.get_rule(DB, once_rule["id"])["fire_once_per_match"] is True)

ONCE_ID = once_rule["id"]
T = NOW + 2_000_000


def fires_at(when):
    """How many times the fire-once rule fired on this pass."""
    before = len(rules.list_fires(DB, ONCE_ID, limit=500))
    rules.evaluate_once(DB, resolve, now=when, config={"command_actions_enabled": True})
    return len(rules.list_fires(DB, ONCE_ID, limit=500)) - before


check("it fires the first time the condition holds", fires_at(T) == 1)
# Well past the 60s cooldown: a timer-only rule would fire again here, which is the whole
# complaint.
check("...and NOT again while it keeps matching", fires_at(T + 5000) == 0)
check("...still not, however long it holds", fires_at(T + 200000) == 0)

# The condition clears (the machine rebooted, so uptime reset), then holds again.
RESOLVED["PC1"] = dict(vars1, **{"sys.uptime_days": rules.Value(0, True, 0)})
check("nothing fires while it does not match", fires_at(T + 300000) == 0)
RESOLVED["PC1"] = vars1
check("it arms itself again once the condition has cleared and returned",
      fires_at(T + 400000) == 1)

# And the default is unchanged: without the flag a rule still fires on the cooldown timer.
TIMER = dict(BASE_RULE, name="Ask hourly", for_seconds=0, cooldown_seconds=60,
             actions=[{"type": "alert", "params": {"text": "up too long"}}],
             target={"include": [{"kind": "machines", "machines": ["PC1"]}]})
err, timer_rule = rules.save_rule(DB, TIMER, actor="tester", now=NOW, command_cooldown_floor=0)
check("a rule without the flag defaults to the old behaviour",
      err is None and timer_rule["fire_once_per_match"] is False)
TIMER_ID = timer_rule["id"]
before = len(rules.list_fires(DB, TIMER_ID, limit=500))
rules.evaluate_once(DB, resolve, now=T + 500000, config={"command_actions_enabled": True})
rules.evaluate_once(DB, resolve, now=T + 500100, config={"command_actions_enabled": True})
check("...firing again once its cooldown has elapsed",
      len(rules.list_fires(DB, TIMER_ID, limit=500)) - before == 2)


fleet.create_command = _real_create

# =======================================================================================
print("\n-- derived variables --")

err, _ = rules.save_derived(DB, "free_pct", "(disk.c.total_gb - disk.c.used_gb) / disk.c.total_gb * 100",
                            now=NOW)
check("save a derived variable", err is None)

err, _ = rules.save_derived(DB, "half_free", "var.free_pct / 2", now=NOW)
check("a derived variable may build on another", err is None)

err, _ = rules.save_derived(DB, "loop", "var.loop + 1", now=NOW)
check("a self-reference is refused", err is not None)

err, _ = rules.save_derived(DB, "free_pct", "var.half_free * 2", now=NOW)
check("an indirect cycle is refused", err is not None)

err, _ = rules.save_derived(DB, "bad", "sys.machine * 2", now=NOW)
check("arithmetic on a text variable is refused", err is not None)

err, _ = rules.save_derived(DB, "bad2", "disk.c.total_gb +", now=NOW)
check("a malformed formula is refused", err is not None)

err, _ = rules.save_derived(DB, "bad3", "nosuch.thing * 2", now=NOW)
check("a formula over an unknown variable is refused", err is not None)

EXTRA2 = rules.all_extra_variables(DB)
vars5 = rules.resolve_machine_vars(DB, "PC1", now=NOW, diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("derived value is computed", vars5["var.free_pct"].value == 20)
check("a derived variable over a derived variable resolves",
      vars5["var.half_free"].value == 10)

vars6 = rules.resolve_machine_vars(DB, "PC2", now=NOW, diagnostics=None, online_window=120)
check("a derived value over UNKNOWN inputs is UNKNOWN, not zero",
      vars6["var.free_pct"].value is UNKNOWN)

err, ast = rules.parse_expression("var.free_pct < 25", EXTRA2)
check("a derived variable is usable in a condition", err is None)
check("...and evaluates", rules.evaluate(ast, vars5) is True)

_, ast = rules.parse_arithmetic("1 / 0", EXTRA2)
check("division by zero is UNKNOWN, not an error",
      rules.evaluate_arithmetic(ast, {}) is UNKNOWN)

_, ast = rules.parse_arithmetic("2 + 3 * 4", EXTRA2)
check("multiplication binds tighter than addition",
      rules.evaluate_arithmetic(ast, {}) == 14)
_, ast = rules.parse_arithmetic("(2 + 3) * 4", EXTRA2)
check("parens override precedence", rules.evaluate_arithmetic(ast, {}) == 20)
_, ast = rules.parse_arithmetic("-5 + 8", EXTRA2)
check("unary minus", rules.evaluate_arithmetic(ast, {}) == 3)

err, deleted = rules.delete_derived(DB, "free_pct")
check("cannot delete a calculation another one still uses", err is not None)
err, deleted = rules.delete_derived(DB, "half_free")
check("...but the leaf deletes fine", err is None and deleted)


# =======================================================================================
print("\n-- probes --")

err, probe = rules.save_probe(DB, "chrome", "file_version",
                              {"path": r"%ProgramFiles%\Google\Chrome\chrome.exe"},
                              value_kind=rules.KIND_TEXT, interval_seconds=3600, now=NOW)
check("save a file_version probe", err is None)

err, _ = rules.save_probe(DB, "sketchy", "script", {"script": "Get-Date"}, now=NOW)
check("a script probe is refused while the setting is off", err is not None)

err, _ = rules.save_probe(DB, "sketchy", "script", {"script": "Get-Date"},
                          allow_script=True, now=NOW)
check("...and allowed once armed", err is None)

err, _ = rules.save_probe(DB, "bad_wmi", "wmi", {"query": "DELETE FROM Win32_Process"}, now=NOW)
check("a non-SELECT WMI probe is refused", err is not None)

err, _ = rules.save_probe(DB, "bad_reg", "registry", {"root": "HKXX", "path": "a"}, now=NOW)
check("an unknown registry root is refused", err is not None)

vars7 = rules.resolve_machine_vars(DB, "PC1", now=NOW, diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("a probe with no value yet is UNKNOWN", vars7["probe.chrome"].value is UNKNOWN)

rules.record_probe_value(DB, "PC1", "chrome", "122.0.6261.95", now=NOW)
vars8 = rules.resolve_machine_vars(DB, "PC1", now=NOW, diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("a collected probe resolves", vars8["probe.chrome"].value == "122.0.6261.95")

err, ast = rules.parse_expression('probe.chrome starts with "122"', rules.all_extra_variables(DB))
check("a probe is usable in a condition", err is None)
check("...and evaluates", rules.evaluate(ast, vars8) is True)

stale = rules.resolve_machine_vars(DB, "PC1", now=NOW + (4 * 3600), diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("a probe value older than 3 intervals reads UNKNOWN",
      stale["probe.chrome"].value is UNKNOWN)

rules.record_probe_value(DB, "PC1", "chrome", None, error="access denied", now=NOW + 10)
vars9 = rules.resolve_machine_vars(DB, "PC1", now=NOW + 20, diagnostics=DIAGNOSTICS,
                                   online_window=120)
check("a failed collection keeps the last good value",
      vars9["probe.chrome"].value == "122.0.6261.95")

due = rules.probes_due(DB, ["PC1", "PC2"], now=NOW + 20)
check("PC2 has never collected, so it is due", any(m == "PC2" for m, _ in due))
check("PC1 collected recently, so its chrome probe is not due",
      not any(m == "PC1" and p["name"] == "chrome" for m, p in due))

COMMANDS.clear()
fleet.create_command = fake_create_command
issued = rules.collect_probes_once(DB, ["PC2"], now=NOW + 20)
check("collection issues a command", issued > 0
      and any(c["type"] == "collect_probe" for c in COMMANDS))
check("...and stamps requested_at so the next tick does not re-issue",
      not any(m == "PC2" and p["name"] == "chrome"
              for m, p in rules.probes_due(DB, ["PC2"], now=NOW + 30)))

check("a rule cannot issue collect_probe itself",
      val_actions([{"type": "command",
                    "params": {"command_type": "collect_probe"}}])[0] is not None)


# =======================================================================================
print("\n-- author scope is bound at evaluation, not just at save --")
#
# The reported escalation: a scoped operator saves a DYNAMIC target (`all`) at a moment when
# it resolves only to machines they can see. Every machine enrolled afterwards falls inside
# that same selector, so without a persisted author scope the rule silently starts acting on
# PCs its author could never reach -- and the author need do nothing for it to happen.
err, scoped_rule = rules.save_rule(
    DB, dict(BASE_RULE, name="Scoped", for_seconds=0, cooldown_seconds=0,
             target={"include": [{"kind": "all"}]},
             actions=[{"type": "alert", "params": {"text": "x"}}]),
    actor="scoped@x.com", now=NOW, author_scope=["PC1", "PC3"])
check("a rule records its author's scope", err is None
      and scoped_rule["author_scope"] == ["PC1", "PC3"])

everyone_now = rules.resolve_targets(DB, scoped_rule["target"])
check("the raw target resolves fleet-wide", len(everyone_now) > 2)
check("...but the rule is bound to its author's scope",
      sorted(rules.scoped_targets(scoped_rule, everyone_now)) == ["PC1", "PC3"])

# A machine enrolled AFTER the rule was saved must not be picked up.
seed_machine("LATECOMER", boot_epoch=NOW - (30 * 86400))
after = rules.resolve_targets(DB, scoped_rule["target"])
check("a newly enrolled machine falls inside the raw target", "LATECOMER" in after)
check("...and is still excluded by the author's scope",
      "LATECOMER" not in rules.scoped_targets(scoped_rule, after))

# An unrestricted author keeps a fully dynamic rule -- they can see the whole fleet anyway,
# and pinning would stop legitimate fleet-wide rules covering new PCs.
err, open_rule = rules.save_rule(
    DB, dict(BASE_RULE, name="Unscoped", target={"include": [{"kind": "all"}]},
             actions=[{"type": "alert", "params": {"text": "x"}}]),
    actor="root@x.com", now=NOW, author_scope=None)
check("an unrestricted author stores no scope", open_rule["author_scope"] is None)
check("...and their rule stays dynamic",
      "LATECOMER" in rules.scoped_targets(open_rule, after))

# Re-stamped on edit, so a narrower operator editing a rule narrows it.
err, renarrowed = rules.save_rule(
    DB, dict(BASE_RULE, name="Unscoped", target={"include": [{"kind": "all"}]},
             actions=[{"type": "alert", "params": {"text": "x"}}]),
    rule_id=open_rule["id"], actor="scoped@x.com", now=NOW, author_scope=["PC1"])
check("an edit re-stamps the scope rather than carrying the old one forward",
      renarrowed["author_scope"] == ["PC1"])

# And the evaluator honours it end to end.
RESOLVED["LATECOMER"] = rules.resolve_machine_vars(DB, "LATECOMER", now=NOW,
                                                   online_window=120)
for rid in (r["id"] for r in rules.list_rules(DB)):
    rules.delete_rule(DB, rid)
err, bound = rules.save_rule(
    DB, dict(BASE_RULE, name="Bound", for_seconds=0, cooldown_seconds=0,
             target={"include": [{"kind": "all"}]},
             actions=[{"type": "alert", "params": {"text": "x"}}]),
    actor="scoped@x.com", now=NOW, author_scope=["PC1"])
summary = rules.evaluate_once(DB, resolve, now=NOW + 100000)
fired_machines = {f["machine"] for f in rules.list_fires(DB, bound["id"])}
check("the evaluator fires only inside the author's scope",
      fired_machines <= {"PC1"})
check("...and not on the machine enrolled later", "LATECOMER" not in fired_machines)

fleet.create_command = _real_create

print(f"\n==== rules: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
