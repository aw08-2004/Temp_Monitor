"""Named, reusable scripts a rule can run on a machine.

WHY THIS IS ITS OWN STORE, and not `fleet_favorites`
----------------------------------------------------
`fleet_favorites` already saves "a name plus a command plus its params", which is very nearly
this. It is deliberately NOT reused, because of who may write one. A favorite is writable by
anyone holding `view` (fleet_web's favorites routes), and that is safe precisely because
RUNNING a favorite goes through the `issue_commands`-gated command endpoint, with a human
clicking it. A rule removes the human: it runs the script unattended, as SYSTEM, on every
machine it targets. Letting a `view`-only account edit the body of something a rule executes
would be a privilege-escalation path, not a trade-off -- so scripts live here, and writing one
requires `issue_commands`, the same capability already required to run arbitrary code.

Reading is split for the same reason. The rules editor needs to LIST scripts to reference one,
which every viewer may do; the BODY is SYSTEM-privileged code and needs `issue_commands`. See
list_scripts (metadata) versus get_script (everything).

NAMES
-----
`name` is the primary key and an identifier (`rules.is_valid_name`'s grammar), because it is
what a rule stores and what appears inside a `{{...}}` placeholder. `label` is the human title.
A consequence worth knowing: renaming is delete-then-create, which the "cannot delete a script
a rule uses" check therefore also covers -- there is no way to silently repoint a rule's
reference at different code.

TEMPLATING
----------
A body may reference two things, both in the ONE `{{...}}` syntax rules.py already owns:

  * `{{input.<name>}}` -- a value the referencing rule supplies, declared in `inputs`.
  * `{{sys.machine}}`, `{{metric.cpu_temp}}`, ... -- the firing machine's variables.

Every `{{input.x}}` must be declared or the save is refused, which is the same discipline
packages.validate_steps applies to its own substitution ("every variable must already be bound
or the save fails"). What must NOT appear is anything an operator can TYPE the value of:
`{{field.*}}` is written by anyone holding `manage_rules` -- a capability deliberately not
sufficient to issue commands -- so interpolating one into a SYSTEM script body would hand that
account the ability to choose text that runs as SYSTEM. See SAFE_TEMPLATE_PREFIXES.

Kept free of Flask, and free of `rules`, so it can be unit-tested in isolation; the endpoints
in rules_web.py wire the two together (a script's variable names are validated through a
callback, so this module never imports the module that owns them).
"""
import json
import re
import sqlite3
import time

# Matches packages.MAX_SCRIPT_CHARS. One ceiling for "how long may an operator-authored script
# be", not two that drift.
MAX_BODY_CHARS = 10000
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 500
MAX_INPUTS = 10
MAX_INPUT_DEFAULT_CHARS = 500

# The executors' own clamp (RunScriptExecutor.cs Math.Clamp(1, 24h)).
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 600

SHELLS = ("powershell", "cmd")
DEFAULT_SHELL = "powershell"

# Identifier grammar, matching rules._NAME_RE and packages._VARIABLE_RE. Anything matching this
# is safe to embed in a dotted `{{...}}` placeholder.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# The prefix a rule-supplied input takes inside a body.
INPUT_PREFIX = "input."

# Variable families a script body may interpolate.
#
# The test is "who can set this value?". `sys.*`, `metric.*`, `disk.*`, `probe.*` and `hw.*`
# are facts reported by the machine or collected from it -- nobody types them into a form. Left
# OUT, deliberately:
#
#   * `field.*` -- custom field values are writable with `manage_rules`, which is deliberately
#     NOT enough to issue commands. Allowing it would let that account choose a string that
#     runs inside a SYSTEM PowerShell body, which is exactly the escalation this module's
#     write gate exists to prevent, reintroduced through the back door.
#   * `var.*` (derived) -- a derived variable is an expression that may be defined over a
#     field, so allowing it would reopen the same hole one level down.
#
# A rule can still pass either of those in explicitly as an INPUT: that is a decision made by
# someone holding `issue_commands`, written into the rule, and visible in it.
SAFE_TEMPLATE_PREFIXES = ("sys.", "metric.", "disk.", "probe.", "hw.", "session.",
                          "net.", "bios.", "remote.", "process.")

# Reuses rules._TEMPLATE_RE's grammar rather than importing it -- this module stays free of
# `rules`, and a second copy of one regex is cheaper than the import cycle.
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_.]{0,63})\s*\}\}")


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_scripts_db(db_path):
    """Create the scripts table if absent. Idempotent -- safe beside the other init_*_db
    calls on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scripts (
                name            TEXT PRIMARY KEY,
                label           TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                shell           TEXT NOT NULL DEFAULT 'powershell',
                body            TEXT NOT NULL,
                inputs_json     TEXT NOT NULL DEFAULT '[]',
                timeout_seconds INTEGER NOT NULL DEFAULT 600,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      INTEGER NOT NULL,
                created_by      TEXT NOT NULL DEFAULT '',
                updated_at      INTEGER NOT NULL,
                updated_by      TEXT NOT NULL DEFAULT ''
            )
            """
        )


def template_names(text):
    """Every `{{name}}` a body references."""
    return sorted({m.group(1) for m in _TEMPLATE_RE.finditer(str(text or ""))})


def _decode(row):
    entry = dict(row)
    try:
        entry["inputs"] = json.loads(entry.pop("inputs_json") or "[]")
    except (TypeError, ValueError):
        entry["inputs"] = []
    entry["enabled"] = bool(entry["enabled"])
    return entry


def _metadata(entry):
    """A script without its body -- what a `view`-only caller may see. `body_chars` and
    `variables` are kept because the rules editor needs to show what a script references
    without being handed the code itself."""
    slim = {k: v for k, v in entry.items() if k != "body"}
    slim["body_chars"] = len(entry.get("body") or "")
    slim["variables"] = [n for n in template_names(entry.get("body"))
                         if not n.startswith(INPUT_PREFIX)]
    return slim


def validate_inputs(raw):
    """Check a script's declared inputs. Returns (error, cleaned).

    An input is a value the REFERENCING RULE supplies, so it needs only enough shape for the
    rule editor to render a field and for the save to refuse a missing one.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, list):
        return "inputs must be a list", None
    if len(raw) > MAX_INPUTS:
        return f"a script may declare at most {MAX_INPUTS} inputs", None

    cleaned = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return f"input {index} must be an object", None
        name = str(item.get("name") or "").strip()
        if not _NAME_RE.match(name):
            return (f"input {index}: '{name}' is not a valid name -- lowercase letters, "
                    "digits and underscores, starting with a letter"), None
        if name in seen:
            return f"input '{name}' is declared twice", None
        seen.add(name)
        default = str(item.get("default") or "")
        if len(default) > MAX_INPUT_DEFAULT_CHARS:
            return f"input '{name}': the default is too long", None
        cleaned.append({
            "name": name,
            "label": str(item.get("label") or "").strip()[:MAX_LABEL_CHARS],
            "required": bool(item.get("required", True)),
            "default": default,
        })
    return None, cleaned


def validate_script(name, label, description, shell, body, inputs, timeout_seconds,
                    enabled=True, known_variable=None):
    """Check one script. Returns (error, cleaned).

    `known_variable(name) -> bool` decides whether a non-input `{{name}}` is real. It is a
    callback because resolving one needs rules.lookup_variable plus the per-hub extra
    namespace, and this module deliberately does not import `rules`. Passing None skips that
    check, which is what a unit test wants.
    """
    name = str(name or "").strip().lower()
    if not _NAME_RE.match(name):
        return ("a script needs a name of lowercase letters, digits and underscores, "
                "starting with a letter"), None

    body = str(body or "")
    if not body.strip():
        return "a script needs a body", None
    if len(body) > MAX_BODY_CHARS:
        return f"the script must be {MAX_BODY_CHARS} characters or fewer", None

    shell = str(shell or DEFAULT_SHELL).strip().lower()
    if shell not in SHELLS:
        return f"shell must be one of {', '.join(SHELLS)}", None

    try:
        timeout = int(timeout_seconds if timeout_seconds is not None
                      else DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return "the timeout must be a whole number of seconds", None
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        return (f"the timeout must be between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS} seconds"), None

    err, clean_inputs = validate_inputs(inputs)
    if err:
        return err, None
    declared = {item["name"] for item in clean_inputs}

    # Every referenced name must be bound to something -- an input, or a real machine
    # variable from a family a script is allowed to read.
    for referenced in template_names(body):
        if referenced.startswith(INPUT_PREFIX):
            input_name = referenced[len(INPUT_PREFIX):]
            if input_name not in declared:
                return (f"the script uses {{{{{referenced}}}}} but declares no input "
                        f"named '{input_name}'"), None
            continue
        if not referenced.startswith(SAFE_TEMPLATE_PREFIXES):
            return (f"{{{{{referenced}}}}} cannot be used in a script. A script may read the "
                    "machine's own reported values; anything an operator types the value of "
                    "(a custom field, or a derived variable over one) would let somebody "
                    "without the Issue commands permission choose text that runs as SYSTEM. "
                    "Pass it as a declared input instead."), None
        if known_variable is not None and not known_variable(referenced):
            return f"{{{{{referenced}}}}} is not a variable this hub knows about", None

    return None, {
        "name": name,
        "label": str(label or "").strip()[:MAX_LABEL_CHARS],
        "description": str(description or "").strip()[:MAX_DESCRIPTION_CHARS],
        "shell": shell,
        "body": body,
        "inputs": clean_inputs,
        "timeout_seconds": timeout,
        "enabled": bool(enabled),
    }


def save_script(db_path, name, label, description, shell, body, inputs, timeout_seconds,
                enabled=True, known_variable=None, actor="", now=None):
    """Create or replace a script. Returns (error, script)."""
    err, script = validate_script(name, label, description, shell, body, inputs,
                                 timeout_seconds, enabled, known_variable)
    if err:
        return err, None
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        existing = conn.execute("SELECT created_at, created_by FROM scripts WHERE name=?",
                                (script["name"],)).fetchone()
        created_at = existing["created_at"] if existing else now
        created_by = existing["created_by"] if existing else str(actor or "")
        conn.execute(
            "INSERT OR REPLACE INTO scripts (name, label, description, shell, body, "
            "inputs_json, timeout_seconds, enabled, created_at, created_by, updated_at, "
            "updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (script["name"], script["label"], script["description"], script["shell"],
             script["body"], json.dumps(script["inputs"]), script["timeout_seconds"],
             1 if script["enabled"] else 0, created_at, created_by, now, str(actor or "")),
        )
    return None, get_script(db_path, script["name"])


def get_script(db_path, name):
    """One script, body included. Callers must hold `issue_commands` -- see the module
    docstring on why the body is not `view`-readable."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM scripts WHERE name=?",
                           (str(name or "").strip().lower(),)).fetchone()
    return _decode(row) if row else None


def list_scripts(db_path, include_body=False):
    """Every script, by name. Without `include_body` this returns METADATA ONLY, which is what
    the rules editor needs and what a `view`-only operator may have."""
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM scripts ORDER BY name").fetchall()
    entries = [_decode(r) for r in rows]
    return entries if include_body else [_metadata(e) for e in entries]


def delete_script(db_path, name, in_use=()):
    """Delete a script. Returns (error, deleted).

    `in_use` is the list of rules referencing it, supplied by the caller (this module does not
    import `rules`). It is checked HERE rather than only at the endpoint so the invariant
    belongs to the store, not to the discipline of its callers -- a second caller added later
    cannot forget it, it can only pass the wrong list, and that is a smaller mistake to make.
    """
    if in_use:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in in_use)
        return (f"this script is used by: {names}. Change those rules first."), False
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM scripts WHERE name=?",
                           (str(name or "").strip().lower(),))
        return None, cur.rowcount > 0


def specs(db_path):
    """{name: {inputs, enabled}} -- what rule validation needs to check a reference without
    loading every body. Built once per save, threaded through validate_actions the same way
    the custom-field namespace (`extra`) is."""
    return {s["name"]: {"inputs": s["inputs"], "enabled": s["enabled"]}
            for s in list_scripts(db_path)}


def validate_reference(spec_map, script_name, inputs):
    """Check a rule's reference to a script. Returns (error, cleaned_inputs).

    Called at SAVE time, so an operator finds out about a typo'd input while editing rather
    than from a fire record a week later.
    """
    name = str(script_name or "").strip().lower()
    if not name:
        return "a script action needs a script", None
    spec = (spec_map or {}).get(name)
    if spec is None:
        return f"there is no script named '{name}'", None
    if not spec.get("enabled", True):
        return f"the script '{name}' is switched off", None

    supplied = inputs or {}
    if not isinstance(supplied, dict):
        return "script inputs must be an object", None

    declared = {item["name"]: item for item in spec.get("inputs") or []}
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        return f"the script '{name}' has no input named '{unknown[0]}'", None

    cleaned = {}
    for input_name, item in declared.items():
        raw = supplied.get(input_name)
        text = "" if raw is None else str(raw)
        if not text.strip():
            text = item.get("default") or ""
        if not text.strip() and item.get("required", True):
            return f"the script '{name}' needs a value for '{input_name}'", None
        if len(text) > MAX_INPUT_DEFAULT_CHARS:
            return f"the value for '{input_name}' is too long", None
        if text:
            cleaned[input_name] = text
    return None, cleaned
