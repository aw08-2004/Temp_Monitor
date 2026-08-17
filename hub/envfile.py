"""Reading and writing the hub's `.env`, for the settings the console can change but the
settings TABLE must never hold.

Three kinds of value live in `.env` rather than in `settings`:

  * credentials (REMOTE_TURN_SECRET, DIRECTORY_BIND_PASSWORD, the OAuth client secrets) --
    the settings table is readable by anyone holding `manage_settings` and is dumped
    wholesale into the hub-database backup;
  * things read at process start, before a database connection exists (FLASK_SECRET_KEY);
  * the sign-in provider configuration, which is the perimeter itself.

This module exists because that file was previously written from one place
(`remote.set_env_var`, for the TURN secret) and is now written from several. The dotenv
format has exactly one trap in it and it is worth encoding once rather than three times:
**python-dotenv folds a leading BOM into the first key name**, so a file written with a
BOM leaves the hub unable to read its own first setting on the next restart -- and
Windows tooling writes BOMs by default. Everything here writes UTF-8 without a BOM and
with LF newlines, and tolerates reading one somebody else left behind.
"""
import os
import re
import sys

# Well-known SIDs, in string form. NEVER account names: "Administrators" is localised
# ("Administradores", "Administratoren", ...), and a name lookup that misses would write an
# ACL with no administrator ACE at all.
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_SE_DACL_PROTECTED = 0x1000
# ACE_HEADER.AceFlags bit meaning "this ACE arrived by inheritance". Spelled out here
# because pywin32 exposes it on neither win32security nor ntsecuritycon.
_INHERITED_ACE = 0x10


def protect(env_path):
    """Restrict `.env` to SYSTEM and Administrators. Returns a note worth logging, or None
    when it was already correct (the steady state, and therefore silent).

    WHY. `.env` is the hub's secret store -- FLASK_SECRET_KEY, AGENT_ENROLLMENT_SECRET,
    BACKUP_MASTER_KEY, the OAuth client secret, DIRECTORY_BIND_PASSWORD, REMOTE_TURN_SECRET
    -- and it lives under STATE_ROOT, which sits beneath C:\\Program Files. That directory
    hands every file it contains an inherited `BUILTIN\\Users:(OI)(CI)(IO)(GR,GE)`, so
    nothing but this stops every local user on the hub server from reading all of it.

    FLASK_SECRET_KEY is the one that makes this urgent rather than untidy: it signs the
    session cookie, so anyone holding it can mint a session for any address in
    ALLOWED_EMAILS -- and ALLOWED_EMAILS is the entire authorization perimeter, over a
    console that runs arbitrary commands as SYSTEM on every machine in the fleet.

    Applied at every boot, not only by the installer: the installer runs once, and the hubs
    that most need this are the ones already deployed. `set_vars` rewrites the file in place
    (open "w" truncates but keeps the ACL), so a protected .env stays protected.

    Fails soft -- a hub that cannot re-ACL its own config must still start, or this turns an
    exposure into an outage. The caller logs what comes back.
    """
    if sys.platform != "win32" or not env_path or not os.path.exists(env_path):
        return None
    try:
        import win32api
        import win32security
        import ntsecuritycon
    except ImportError:      # pragma: no cover - pywin32 is a hard requirement in practice
        return f"Could not restrict permissions on {env_path}: pywin32 is not installed."

    try:
        wanted = [win32security.ConvertStringSidToSid(s)
                  for s in (_SYSTEM_SID, _ADMINISTRATORS_SID)]

        # ...plus whoever this process is, which is NOT redundant and is load-bearing.
        #
        # In production it is: the hub runs as LocalSystem, so this adds nothing. In a dev
        # checkout it is the developer, and without it protect() locks the person running
        # the hub out of the .env they are editing -- an unprivileged account cannot even
        # read the ACL back to undo it, so recovery needs an elevated shell. The security
        # property being bought here is "not readable by EVERY local user", and keeping the
        # running account's own access costs none of it.
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        try:
            me = win32security.GetTokenInformation(token, ntsecuritycon.TokenUser)[0]
        finally:
            win32api.CloseHandle(token)
        if not any(me == w for w in wanted):
            wanted.append(me)

        # The Named variants throughout, NOT Get/SetFileSecurity: the legacy pair predates
        # auto-inheritance and does not maintain its control bits. SetFileSecurity with
        # PROTECTED_DACL_SECURITY_INFORMATION does strip the inherited ACEs, but leaves
        # SE_DACL_PROTECTED clear -- so the Users read grant returns the next time Windows
        # recomputes inheritance, and the check below can never see a settled state.
        obj = win32security.SE_FILE_OBJECT
        info = win32security.DACL_SECURITY_INFORMATION
        sd = win32security.GetNamedSecurityInfo(env_path, obj, info)
        control, _revision = sd.GetSecurityDescriptorControl()
        dacl = sd.GetSecurityDescriptorDacl()

        # Already exactly right: inheritance broken, and every ACE belongs to one of the two
        # principals allowed to read this. Checked rather than blindly rewritten so the
        # common path neither churns the ACL nor logs on every restart.
        if control & _SE_DACL_PROTECTED and dacl is not None:
            extra = False
            for i in range(dacl.GetAceCount()):
                (_ace_type, ace_flags), _mask, sid = dacl.GetAce(i)
                if ace_flags & _INHERITED_ACE or not any(sid == w for w in wanted):
                    extra = True
                    break
            if not extra:
                return None

        replacement = win32security.ACL()
        for sid in wanted:
            replacement.AddAccessAllowedAce(
                win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
        # PROTECTED_DACL_SECURITY_INFORMATION is what drops the inherited ACEs and keeps them
        # dropped. Without it the Users read grant comes straight back and this achieves
        # nothing.
        win32security.SetNamedSecurityInfo(
            env_path, obj, info | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, replacement, None)
        return (f"Restricted {env_path} to SYSTEM, Administrators and this hub's own "
                f"account (it was readable by every local user on this machine).")
    except Exception as e:
        return (f"Could not restrict permissions on {env_path}: {e}. Every local user on "
                f"this machine can read it. Fix with: icacls \"{env_path}\" /inheritance:r "
                f"/grant *{_SYSTEM_SID}:F *{_ADMINISTRATORS_SID}:F")

# KEY=value, allowing `export KEY=value` and leading whitespace. Comments and blank lines
# fail to match and are therefore preserved untouched by the rewriter below.
_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_all(env_path):
    """Every KEY=value in the file, as a dict.

    Surrounding whitespace is stripped, because python-dotenv strips it and python-dotenv is
    what actually puts these values in os.environ. Everything this module writes is
    `key=value` with no padding, so the two only diverged on a HAND-EDITED file -- and there
    the divergence was the damaging kind: this function is what the console shows as the
    current value and what set_vars diffs against to decide whether anything changed, so
    `KEY = value` was read here as " value" while the running hub held "value", making an
    unchanged setting look changed and a matching secret look mismatched.

    Quotes are still NOT stripped, which is a different case and deliberate: everything this
    module writes is unquoted, so a value somebody quoted by hand is theirs to own.
    """
    values = {}
    if not env_path or not os.path.exists(env_path):
        return values
    with open(env_path, "r", encoding="utf-8-sig") as fh:
        for line in fh.read().splitlines():
            match = _ENV_KEY_RE.match(line)
            if match:
                values[match.group(1)] = line.split("=", 1)[1].strip()
    return values


def set_vars(env_path, updates):
    """Upsert several KEY=value pairs at once, preserving every other line.

    A single rewrite rather than one per key: writing the OAuth client id and secret as two
    separate rewrites leaves a window where the file holds a new id beside the old secret,
    and a hub that restarted in that window would come up unable to sign anybody in.

    A value of None DELETES the key -- which is how a provider gets turned off, and is
    meaningfully different from setting it empty (python-dotenv would then export an empty
    string, and `os.environ.get(...)` would return "" rather than None; both are falsy
    here, but only one leaves the file honest about what is configured).

    Returns the set of keys that were actually changed.
    """
    updates = {str(k): v for k, v in (updates or {}).items()}
    if not updates:
        return set()

    before = read_all(env_path)
    lines, seen = [], set()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8-sig") as fh:
            for line in fh.read().splitlines():
                match = _ENV_KEY_RE.match(line)
                key = match.group(1) if match else None
                if key is not None and key in updates:
                    seen.add(key)
                    if updates[key] is None:
                        continue          # drop the line entirely
                    lines.append(f"{key}={updates[key]}")
                else:
                    lines.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            lines.append(f"{key}={value}")

    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    changed = set()
    for key, value in updates.items():
        was = before.get(key)
        now = None if value is None else str(value)
        if was != now:
            changed.add(key)
    return changed


def set_var(env_path, key, value):
    """Upsert one KEY=value. Returns the value written."""
    set_vars(env_path, {key: value})
    return value


def apply_to_environ(updates):
    """Mirror the same changes into os.environ, so they take effect without a restart.

    Always paired with set_vars: the file is what survives a restart, and os.environ is
    what the running process reads. Writing only one of the two produces the two worst
    outcomes available -- a change that works until the hub restarts, or one that does
    nothing until it does.
    """
    for key, value in (updates or {}).items():
        if value is None:
            os.environ.pop(str(key), None)
        else:
            os.environ[str(key)] = str(value)
