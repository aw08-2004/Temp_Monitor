"""Registered Users directory -- the hub's user profile store.

Until now identity was a bare, lowercased email string threaded through the app:
`permission_group_members.email`, `fleet_favorites.owner_email`, `audit_log.actor`.
There was no place to hang a real name, a username, or contact details, and nothing
persisted from a Google login beyond the session. This module is that directory.

  * A **user** is one row keyed by normalized email -- `full_name`, `username`, and a
    handful of optional contact fields. Real columns, not a JSON blob, matching the
    rest of the schema's style.
  * **Populated two ways**: (a) `upsert_from_login()` runs on every successful OAuth
    sign-in, so anyone who has ever logged in appears automatically; (b) an admin adds
    a row by hand via the Users API/page, mirroring how `permission_group_members`
    already lets you grant access to an email before its first login.
  * This module does NOT gate anything. `permission_group_members` stays the
    authorization table, keyed on email regardless of whether a `users` row exists --
    deleting a directory entry here has no effect on anyone's access. This is a
    profile store, not a second permission layer.

Flask-free, like permissions.py/fleet.py/settings.py -- unit-testable without a
running app. The HTTP surface (the admin API behind the Users page) lives in
users_web.py.
"""
import sqlite3
import time

import fleet
import permissions

MAX_FIELD_CHARS = 200
MAX_NOTES_CHARS = 2000

SOURCE_LOGIN = "login"
SOURCE_MANUAL = "manual"

# Columns an admin (or the login upsert) may set. Order here is the order create_user/
# update_user accept them and the order list_users returns them in.
_EDITABLE_FIELDS = ("full_name", "username", "phone", "title", "department", "notes")


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_users_db(db_path):
    """Create the users table if absent. Idempotent -- safe to call next to the other
    init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                email         TEXT PRIMARY KEY,
                full_name     TEXT,
                username      TEXT,
                phone         TEXT,
                title         TEXT,
                department    TEXT,
                notes         TEXT,
                source        TEXT NOT NULL DEFAULT 'manual',
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                last_login_at INTEGER,
                created_by    TEXT
            )
            """
        )


# ================================
# NORMALISATION & VALIDATION
# ================================
def normalize_email(email):
    """Identity here is the same normalized email as everywhere else in the hub --
    delegated to permissions.py rather than reimplemented, so the two tables can never
    disagree about what 'Ann@X.com' means."""
    return permissions.normalize_email(email)


def _validate_email(email):
    cleaned = normalize_email(email)
    if not cleaned or "@" not in cleaned:
        raise ValueError(f"{email!r} is not an email address.")
    return cleaned


def _validate_field(name, value, max_chars=MAX_FIELD_CHARS):
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters.")
    return cleaned


def _validate_fields(data):
    """Validate the editable-field subset of `data`, returning a clean dict with every
    key in _EDITABLE_FIELDS present (None where absent/blank)."""
    cleaned = {}
    for field in _EDITABLE_FIELDS:
        max_chars = MAX_NOTES_CHARS if field == "notes" else MAX_FIELD_CHARS
        cleaned[field] = _validate_field(field, data.get(field), max_chars)
    return cleaned


def _row_to_dict(row):
    if row is None:
        return None
    return {
        "email": row["email"],
        "full_name": row["full_name"],
        "username": row["username"],
        "phone": row["phone"],
        "title": row["title"],
        "department": row["department"],
        "notes": row["notes"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
        "created_by": row["created_by"],
    }


# ================================
# READS
# ================================
def get_user(db_path, email):
    """One user, or None."""
    email = normalize_email(email)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return _row_to_dict(row)


def list_users(db_path, q=None):
    """Every registered user, sorted by full name (falling back to email for anyone
    with none) -- the reading order a directory listing wants.

    `q` is an optional case-insensitive substring match against email, full_name and
    username, matching how the Backups restore browser's `search=` works -- the one
    existing precedent for search-as-you-type in this app (roadmap #6). It is a plain
    LIKE scan: directory sizes here are "every operator who has ever signed in", not
    telemetry-scale, so an index would be solving a problem that doesn't exist yet.
    """
    query = "SELECT * FROM users"
    params = ()
    text = str(q or "").strip()
    if text:
        like = f"%{text.lower()}%"
        query += (" WHERE lower(email) LIKE ? OR lower(full_name) LIKE ? "
                  "OR lower(username) LIKE ?")
        params = (like, like, like)
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    users = [_row_to_dict(r) for r in rows]
    users.sort(key=lambda u: (u["full_name"] or u["email"]).lower())
    return users


# ================================
# WRITES
# ================================
def create_user(db_path, email, full_name=None, username=None, phone=None, title=None,
                department=None, notes=None, actor="unknown"):
    """Manually register a user ahead of their first login. Raises ValueError on a bad
    email or invalid field, or if the email is already registered."""
    email = _validate_email(email)
    fields = _validate_fields({
        "full_name": full_name, "username": username, "phone": phone,
        "title": title, "department": department, "notes": notes,
    })
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO users(email, full_name, username, phone, title, "
                "department, notes, source, created_at, updated_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email, fields["full_name"], fields["username"], fields["phone"],
                 fields["title"], fields["department"], fields["notes"],
                 SOURCE_MANUAL, now, now, actor),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"{email} is already registered.")
    fleet.audit(db_path, actor, "user.create", email, dict(fields))
    return get_user(db_path, email)


def update_user(db_path, email, full_name=None, username=None, phone=None, title=None,
                department=None, notes=None, actor="unknown"):
    """Overwrite every editable field on an existing user. Raises KeyError if unknown.

    Unlike update_group()'s "None means leave alone" (which needs a matching form that
    only ever sends what changed), this follows the permissions.js editor's discipline
    instead: the caller always sends the whole record on every save, so there is no
    field this could ambiguously "leave alone" -- and no sentinel-value trap like the
    backup Follow-Fleet bug, where None meaning "unchanged" left no way to actually
    clear a field back to empty.
    """
    email = normalize_email(email)
    before = get_user(db_path, email)
    if before is None:
        raise KeyError(email)
    fields = _validate_fields({
        "full_name": full_name, "username": username, "phone": phone,
        "title": title, "department": department, "notes": notes,
    })
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE users SET full_name = ?, username = ?, phone = ?, title = ?, "
            "department = ?, notes = ?, updated_at = ? WHERE email = ?",
            (fields["full_name"], fields["username"], fields["phone"], fields["title"],
             fields["department"], fields["notes"], now, email),
        )
    after = get_user(db_path, email)
    changes = {f: {"from": before.get(f), "to": after.get(f)} for f in _EDITABLE_FIELDS
               if before.get(f) != after.get(f)}
    if changes:
        fleet.audit(db_path, actor, "user.update", email, {"changes": changes})
    return after


def delete_user(db_path, email, actor="unknown"):
    """Remove a directory entry. Raises KeyError if unknown.

    Deliberately does not touch permission_group_members: membership is keyed on
    email independently of this table (see the module docstring), so deleting a
    profile here never revokes access someone already has.
    """
    email = normalize_email(email)
    before = get_user(db_path, email)
    if before is None:
        raise KeyError(email)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
    fleet.audit(db_path, actor, "user.delete", email, None)
    return True


def upsert_from_login(db_path, email, name):
    """Called on every successful OAuth sign-in. Auto-registers a first-time signer-in
    and always stamps `last_login_at`; deliberately does NOT overwrite an existing
    `full_name` on repeat logins, so an admin's (or the user's own) manual edit isn't
    silently stomped by whatever string Google's claim happens to carry that day.

    Not audited: a login happening is not an admin action, and auditing it here would
    duplicate whatever the login flow itself records while adding one row per sign-in
    to a trail meant for configuration changes.
    """
    email = normalize_email(email)
    if not email:
        return
    cleaned_name = _validate_field("full_name", name)
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT full_name FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(email, full_name, source, created_at, updated_at, "
                "last_login_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, cleaned_name, SOURCE_LOGIN, now, now, now, email),
            )
        elif not row["full_name"]:
            conn.execute(
                "UPDATE users SET full_name = ?, updated_at = ?, last_login_at = ? "
                "WHERE email = ?",
                (cleaned_name, now, now, email),
            )
        else:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE email = ?", (now, email)
            )
