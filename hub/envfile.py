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

# KEY=value, allowing `export KEY=value` and leading whitespace. Comments and blank lines
# fail to match and are therefore preserved untouched by the rewriter below.
_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_all(env_path):
    """Every KEY=value in the file, as a dict. Values are returned raw -- no quote
    stripping -- because everything this module writes is unquoted, and a value somebody
    quoted by hand is theirs to own."""
    values = {}
    if not env_path or not os.path.exists(env_path):
        return values
    with open(env_path, "r", encoding="utf-8-sig") as fh:
        for line in fh.read().splitlines():
            match = _ENV_KEY_RE.match(line)
            if match:
                values[match.group(1)] = line.split("=", 1)[1]
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
