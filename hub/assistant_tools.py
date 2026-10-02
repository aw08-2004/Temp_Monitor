"""What the console assistant (roadmap #26) may call, and how much each call is trusted.

**The assistant never gets a code path of its own into the fleet.** Every tool here resolves
to an HTTP route the console already has, and assistant_web.py runs that route as an internal
request carrying the operator's own session -- so both authorization gates (`access.require`
and `access.require_machine`) and every inline scope check a route makes for itself apply
exactly as they do when the operator clicks the button. This file decides only two things:
which routes are worth describing to a model, and which TIER a route is in.

The silent failure this file exists to prevent is a destructive route reached without a click.
A model is talked into things -- by the operator, by a hostname, by a line of command output it
was asked to read -- so "the model decided to" can never be the whole reason a machine is wiped.
The tiers:

  * `read`   -- runs at once. Every GET, plus the handful of POSTs that change nothing.
  * `write`  -- runs at once and is audited. Reversible, low-impact changes only.
  * `confirm`-- NOT run. Stored as a pending action the operator must confirm in the panel.
  * `denied` -- never offered and never reachable, whatever the model asks for.

**Fail closed.** A route nobody classified is `confirm`, the same rule fleet.ACTION_LEVELS
applies to audit rows: briefly having to click for a harmless new route is a much cheaper
mistake than a new destructive one running unasked. tests/test_assistant.py walks the real
url_map so a new route forces somebody to look at this table.

Rejected while designing this:
  * One hand-written tool per route. There are ~250 operator routes; a hand-written schema for
    each is a second API description that drifts from the first the day a route changes. The
    curated tools below cover the common work, and `call_endpoint` reaches the rest through the
    same tier table.
  * A deny-list as the only gate. A deny-list says what is forbidden and is silent about
    everything nobody thought of; the tier table is an allow-list for `read` and `write`, and
    everything else needs a person.
  * Re-checking capabilities here. The routes already do it, including the inline checks a
    decorator-reading layer would miss (the body scope check on POST /api/fleet/commands, the
    two capabilities a watchdog needs). A second copy of those checks is a copy that can be
    wrong in the permissive direction.

Flask-free, like every model half in this hub.
"""
import json
import re
from urllib.parse import quote

TIER_READ = "read"
TIER_WRITE = "write"
TIER_CONFIRM = "confirm"
TIER_DENIED = "denied"
TIERS = (TIER_READ, TIER_WRITE, TIER_CONFIRM, TIER_DENIED)

# ---------------------------------------------------------------------------------------
# Never reachable
# ---------------------------------------------------------------------------------------
# Rule PREFIXES. Each of these is either a secret coming out of the hub, a credential being
# made, a surface that is not an operator's to drive, or the assistant itself.
DENIED_PREFIXES = (
    # Agents and peer hubs authenticate with their own credentials; an operator session has no
    # business on these, and a model has even less.
    "/api/agent/",
    "/api/peer/",
    # The assistant calling itself is a loop with a bill attached.
    "/api/assistant/",
    # Sign-in provider configuration is superuser-only and changes who can get in at all.
    "/api/auth/",
    # Device tokens are bearer credentials that leave the building.
    "/api/tokens",
    "/app/pair",
    # The interactive terminal is a human at a prompt. run_script covers the scripted case,
    # behind a confirmation, with the command visible before it runs.
    "/api/fleet/pty",
    # Live remote-control signalling is a browser's WebRTC handshake, not an API call.
    "/api/remote/session/",
    # Push registration only works from a device token anyway.
    "/api/push/",
)

# Exact (METHOD, rule) pairs. Secrets that a model would then hold in its context -- and send
# to whatever provider is configured -- are the reason most of these are here.
DENIED_ROUTES = {
    # The agents' ingest. Exact, not a prefix: as a prefix it also swallowed /api/reports/*,
    # which is the inventory sheet -- caught by the test that no curated tool is denied.
    ("POST", "/api/report"),
    # File CONTENTS, not facts about files: a backup, a fetched file, an installer. Bytes from
    # somebody's PC have no business in a prompt, and the model cannot read a binary anyway.
    ("GET", "/api/backups/machines/<machine>/download"),
    ("GET", "/api/machines/<machine>/files/transfers/<transfer_id>/content"),
    ("GET", "/api/provisioning/apk"),
    ("GET", "/api/remote/virtual-display/payload"),
    # The provisioning QR carries the enrollment secret.
    ("GET", "/api/provisioning/qr"),
    ("POST", "/api/bitlocker/<machine>/reveal"),
    ("POST", "/api/backups/key"),
    ("POST", "/api/backups/key/reveal"),
    ("POST", "/api/backups/key/import"),
    ("POST", "/api/backups/key/escrowed"),
    ("POST", "/api/ai/key"),
    ("POST", "/api/remote/turn/secret"),
    ("PUT", "/api/bios-password"),
    ("PUT", "/api/bios-password/<machine>"),
    ("DELETE", "/api/bios-password/<machine>"),
    ("DELETE", "/api/bios-password"),
    ("POST", "/api/hub/update"),
    ("POST", "/api/sharing/pairings"),
    # Uploads are multipart and the assistant only speaks JSON; refusing them here says so
    # instead of failing on a content-type check two layers down.
    ("POST", "/api/packages/upload"),
    ("POST", "/api/firmware/upload"),
    ("POST", "/api/provisioning/apk"),
    ("POST", "/api/machines/<machine>/files/upload"),
}

# POSTs that change nothing: previews, dry runs and the AI answering routes. A POST only
# because a GET would be fetchable from a third-party page (see ai_web.fleet_summary).
READ_POSTS = {
    ("POST", "/api/rules/preview"),
    ("POST", "/api/rules/targets"),
    ("POST", "/api/device-groups/preview"),
    ("POST", "/api/policy/preview"),
    ("POST", "/api/backups/preview"),
    ("POST", "/api/ai/query"),
    ("POST", "/api/ai/summary"),
    ("POST", "/api/ai/machines/<machine>/ask"),
    ("POST", "/api/machines/<machine>/live/watch"),
    ("POST", "/api/machines/<machine>/files/list"),
}

# Runs at once. Each is reversible, or harmless to repeat, and none runs code on a machine or
# changes who can do what. A rule created here is created DISABLED by the route itself
# (ai_web.commit_draft / ai.rule_payload); enabling one is `confirm`.
WRITE_ROUTES = {
    ("POST", "/api/alerts/<int:alert_id>/dismiss"),
    ("POST", "/api/alerts/<alert_id>/dismiss"),
    ("POST", "/api/fleet/favorites"),
    ("PUT", "/api/fleet/favorites/<favorite_id>"),
    ("DELETE", "/api/fleet/favorites/<favorite_id>"),
    ("POST", "/api/ai/rules/draft"),
    ("POST", "/api/ai/rules/drafts/<draft_id>/refine"),
    ("POST", "/api/ai/rules/drafts/<draft_id>/commit"),
    ("DELETE", "/api/ai/rules/drafts/<draft_id>"),
    ("POST", "/api/wake/machines/<machine>"),
    ("POST", "/api/wake/machines/<machine>/prepare"),
    ("POST", "/api/discovery/machines/<machine>/scan"),
    ("POST", "/api/bios/<machine>/refresh"),
    ("POST", "/api/remote/<machine>/inventory/refresh"),
    ("POST", "/api/sharing/links/<link_id>/refresh"),
    ("POST", "/api/language"),
}

# Confirm-tier routes where the click alone is not enough: the operator also types the
# machine's name, which is what the wipe dialog asks for and what the route checks.
TYPED_CONFIRM_ROUTES = {
    ("POST", "/api/wipe/machines/<machine>/wipe"),
}


def classify(method, rule):
    """The tier of one route. `rule` is the url_map RULE (`/api/x/<machine>`), not a path.

    Classified on the rule rather than the concrete path so that a machine named
    `reveal` cannot change what a route is.
    """
    method = str(method or "").upper()
    rule = str(rule or "")
    if (method, rule) in DENIED_ROUTES:
        return TIER_DENIED
    if any(rule == p.rstrip("/") or rule.startswith(p) for p in DENIED_PREFIXES):
        return TIER_DENIED
    # Only the JSON API. A page route is something to LINK to, not to call.
    if not rule.startswith("/api/"):
        return TIER_DENIED
    if method in ("GET", "HEAD"):
        return TIER_READ
    if (method, rule) in READ_POSTS:
        return TIER_READ
    if (method, rule) in WRITE_ROUTES:
        return TIER_WRITE
    return TIER_CONFIRM


def needs_typed_name(method, rule):
    return (str(method or "").upper(), str(rule or "")) in TYPED_CONFIRM_ROUTES


# ---------------------------------------------------------------------------------------
# Curated tools
# ---------------------------------------------------------------------------------------
# A name, a sentence for the model, and the route it resolves to. `<machine>` and other
# `<name>` segments in `path` are filled from the arguments of the same name; every other
# argument becomes the query string (GET) or the JSON body (anything else). The schema is the
# model's description of the arguments, not a validator -- the route validates, and its
# refusal goes back to the model verbatim.

_MACHINE = {"type": "string", "description": "The machine's name exactly as the hub lists it."}


def _params(required=(), **props):
    return {"type": "object", "properties": props, "required": list(required)}


CURATED = (
    ("fleet_summary", "Counts across the fleet: online, offline, alerts, versions.",
     "GET", "/api/fleet/summary", _params()),
    ("list_machines", "Every machine the operator can see, with status and key readings.",
     "GET", "/api/machines", _params()),
    ("get_machine", "Everything the hub knows about one machine right now.",
     "GET", "/api/machines/<machine>", _params(["machine"], machine=_MACHINE)),
    ("machine_history", "Sensor history for one machine. `metrics` is a comma list, "
     "`range` e.g. 24h or 7d.",
     "GET", "/api/machines/<machine>/history",
     _params(["machine"], machine=_MACHINE, metrics={"type": "string"},
             range={"type": "string"})),
    ("machine_report", "The one-page inventory sheet for a machine (hardware, OS, posture).",
     "GET", "/api/reports/machines/<machine>", _params(["machine"], machine=_MACHINE)),
    ("machine_processes", "Running processes on a machine.",
     "GET", "/api/machines/<machine>/processes", _params(["machine"], machine=_MACHINE)),
    ("machine_software", "Installed software on a machine.",
     "GET", "/api/software/machines/<machine>", _params(["machine"], machine=_MACHINE)),
    ("machine_patches", "Pending and installed updates for a machine.",
     "GET", "/api/patches/machine/<machine>", _params(["machine"], machine=_MACHINE)),
    ("machine_posture", "Security posture of a machine (AV, firewall, BitLocker state).",
     "GET", "/api/posture/machines/<machine>", _params(["machine"], machine=_MACHINE)),
    ("list_alerts", "Open alerts the operator can see.",
     "GET", "/api/alerts", _params()),
    ("alert_bundles", "Correlated alert bundles with likely causes.",
     "GET", "/api/alerts/bundles", _params()),
    ("dismiss_alert", "Dismiss one alert by id.",
     "POST", "/api/alerts/<alert_id>/dismiss",
     _params(["alert_id"], alert_id={"type": "integer"})),
    ("list_commands", "Recent fleet commands, optionally for one machine.",
     "GET", "/api/fleet/commands", _params(machine=_MACHINE)),
    ("command_output", "Status, result and output of one command.",
     "GET", "/api/fleet/commands/<command_id>/output",
     _params(["command_id"], command_id={"type": "integer"})),
    ("run_command", "Queue a command on a machine: restart, shutdown, rename, gpupdate, "
     "install_app, run_script, show_message and others. Needs the operator's confirmation.",
     "POST", "/api/fleet/commands",
     _params(["machine", "type"], machine=_MACHINE,
             type={"type": "string", "description": "The command type, e.g. restart."},
             params={"type": "object", "description": "The command's parameters."})),
    ("kill_process", "End a process on a machine by name or pids. Needs confirmation.",
     "POST", "/api/machines/<machine>/processes/kill",
     _params(["machine"], machine=_MACHINE, name={"type": "string"},
             pids={"type": "array", "items": {"type": "integer"}},
             tree={"type": "boolean"})),
    ("wake_machine", "Send Wake-on-LAN to a machine.",
     "POST", "/api/wake/machines/<machine>", _params(["machine"], machine=_MACHINE)),
    ("fleet_query", "Answer a question over the whole fleet in plain English (e.g. "
     "'which machines have less than 10 GB free?'), evaluated by the rules engine.",
     "POST", "/api/ai/query", _params(["text"], text={"type": "string"})),
    ("fleet_report", "Alerts raised, cleared and open over a window of days.",
     "POST", "/api/ai/summary", _params(window_days={"type": "integer"})),
    ("list_rules", "The rules engine's rules.",
     "GET", "/api/rules", _params()),
    ("draft_rule", "Draft a monitoring rule from an English sentence. Returns a draft id and "
     "the staged rules; nothing is saved yet.",
     "POST", "/api/ai/rules/draft", _params(["text"], text={"type": "string"})),
    ("create_rule_from_draft", "Create one staged rule of a draft. It is created DISABLED.",
     "POST", "/api/ai/rules/drafts/<draft_id>/commit",
     _params(["draft_id"], draft_id={"type": "string"}, index={"type": "integer"})),
    ("audit_log", "The audit log, newest first. Filters: q, actor, action, from, to.",
     "GET", "/api/audit", _params(q={"type": "string"}, actor={"type": "string"},
                                  action={"type": "string"})),
    ("my_permissions", "The operator's own capabilities and machine scope.",
     "GET", "/api/permissions/me", _params()),
)

# The two tools that reach everything else, plus the hub's own map.
GENERIC = (
    ("find_page", "Find console pages for a topic, with their links.",
     _params(["topic"], topic={"type": "string"})),
    ("list_endpoints", "Search the hub's JSON API for routes this operator may call. Use it "
     "before call_endpoint. Returns method, rule, tier and a one-line description.",
     _params(q={"type": "string", "description": "Words to match, e.g. 'backup restore'."})),
    ("call_endpoint", "Call one hub API route listed by list_endpoints. `path` is the "
     "concrete path, e.g. /api/patches/machine/PC-12. GETs take `query`, others take `body`.",
     _params(["method", "path"], method={"type": "string", "enum": ["GET", "POST", "PUT",
                                                                    "PATCH", "DELETE"]},
             path={"type": "string"}, query={"type": "object"}, body={"type": "object"})),
)

CURATED_BY_NAME = {name: (desc, method, path, schema)
                   for name, desc, method, path, schema in CURATED}
GENERIC_NAMES = {name for name, _desc, _schema in GENERIC}

_SEGMENT = re.compile(r"<(?:[a-z_]+:)?([a-z_]+)>")


def tool_specs(curated_names):
    """The OpenAI-shape `tools` list for the names this operator is offered."""
    specs = []
    for name, desc, _method, _path, schema in CURATED:
        if name in curated_names:
            specs.append({"type": "function",
                          "function": {"name": name, "description": desc,
                                       "parameters": schema}})
    for name, desc, schema in GENERIC:
        specs.append({"type": "function",
                      "function": {"name": name, "description": desc, "parameters": schema}})
    return specs


def parse_arguments(raw):
    """The model's argument string as a dict. Returns (error, args)."""
    if isinstance(raw, dict):
        return None, raw
    try:
        args = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return "the arguments were not valid JSON", None
    if not isinstance(args, dict):
        return "the arguments must be a JSON object", None
    return None, args


def build_request(name, args):
    """Turn a curated tool call into (error, method, path, query, body)."""
    entry = CURATED_BY_NAME.get(name)
    if not entry:
        return f"there is no tool called {name}", None, None, None, None
    _desc, method, template, _schema = entry
    args = dict(args or {})
    missing = []

    def fill(match):
        key = match.group(1)
        value = args.pop(key, None)
        if value is None or str(value).strip() == "":
            missing.append(key)
            return ""
        # safe="" so a machine name with a slash cannot become a second path segment.
        return quote(str(value).strip(), safe="")

    path = _SEGMENT.sub(fill, template)
    if missing:
        return f"missing argument: {', '.join(missing)}", None, None, None, None
    if method == "GET":
        return None, method, path, {k: v for k, v in args.items() if v is not None}, None
    return None, method, path, None, args


def check_path(path):
    """Refuse a path that is not a plain hub API path. Returns an error or None.

    The dispatcher resolves the path against the url_map and classifies the RULE it lands on,
    so this is not the gate -- it is the reason the gate only ever sees one kind of input. A
    scheme, a second slash or a dot segment is a model trying to leave the API, and the answer
    to that is no rather than a normalisation.
    """
    path = str(path or "")
    if not path.startswith("/api/"):
        return "only paths under /api/ can be called"
    if "//" in path or "\\" in path or "?" in path or "#" in path:
        return "pass the query in `query`, and a plain path in `path`"
    if any(seg in (".", "..") for seg in path.split("/")):
        return "that path is not allowed"
    if any(ord(ch) < 32 for ch in path):
        return "that path is not allowed"
    return None


def matching_endpoints(catalog, query, allowed, limit=40):
    """Rank the catalog for a free-text query. `allowed(entry)` filters by capability.

    `catalog` entries are {"method", "rule", "summary", "caps"}. Denied routes are dropped
    before ranking so they are never so much as named to the model.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", str(query or "").lower()) if w]
    scored = []
    for entry in catalog:
        tier = classify(entry["method"], entry["rule"])
        if tier == TIER_DENIED or not allowed(entry):
            continue
        hay = f"{entry['rule']} {entry.get('summary', '')}".lower()
        score = sum(1 for w in words if w in hay) if words else 1
        if score:
            scored.append((-score, entry["rule"], entry["method"],
                           dict(entry, tier=tier)))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:limit]]
