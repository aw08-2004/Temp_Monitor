"""Outbound notification -- webhooks and email for the rules engine.

Before this, the hub had no outbound channel of any kind: no SMTP, no webhook, nothing. A
high-temperature alert raised at 02:00 reached nobody until somebody opened the Alerts tab.
This module is what closes that gap, and the rules engine's `webhook` and `email` actions are
its first callers.

Two design points that shape everything below.

**Delivery is never inline.** `deliver()` writes to an outbox table and returns immediately.
The evaluator thread is the one that decides whether four hundred PCs need restarting; it
must never be sitting in a TCP connect to a mail server that has stopped answering. A row in
a table also survives a hub restart, which an in-memory queue would not -- and the whole
point of an alert is that it arrives even when things are going badly.

**Configuration lives in .env, not the settings table.** SMTP credentials in the settings
database would be inside the nightly backup, which is exactly the reasoning that keeps a BIOS
setup password out of command params (see fleet.py). Having the host and port follow the
password into .env, rather than splitting one block of config across two homes, is the lesser
of the two evils: one place to look, and no half-configured state where the console shows a
mail server that has no credentials.
"""
import json
import os
import smtplib
import socket
import sqlite3
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from ipaddress import ip_address
from urllib.parse import urlparse

import requests

KIND_WEBHOOK = "webhook"
KIND_EMAIL = "email"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# Backoff between attempts, in seconds. Five attempts over roughly twenty minutes: long
# enough to ride out a mail server restart, short enough that a genuinely dead endpoint is
# marked failed while the operator still remembers configuring it.
RETRY_BACKOFF = (30, 120, 300, 600)
MAX_ATTEMPTS = len(RETRY_BACKOFF) + 1

WEBHOOK_TIMEOUT_SECONDS = 10
SMTP_TIMEOUT_SECONDS = 20
OUTBOX_RETENTION_DAYS = 14


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_notify_db(db_path):
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_outbox (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                kind            TEXT NOT NULL,
                payload_json    TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL,
                created_at      INTEGER NOT NULL,
                sent_at         INTEGER,
                last_error      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notify_outbox_due "
                     "ON notify_outbox(status, next_attempt_at)")


# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------


def smtp_config():
    """SMTP settings from the environment. Returns None when mail is not configured.

    Not configured is the normal state, not an error: a helpdesk that only wants webhooks
    should never see a mail failure in its logs.
    """
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return None
    sender = (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "").strip()
    if not sender:
        return None
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    mode = (os.environ.get("SMTP_SECURITY") or "starttls").strip().lower()
    if mode not in ("starttls", "ssl", "none"):
        mode = "starttls"
    return {
        "host": host, "port": port, "security": mode, "from": sender,
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": os.environ.get("SMTP_PASSWORD") or "",
    }


def webhooks_allow_private():
    """Whether a webhook may target a private/loopback address.

    Off by default. On is legitimate -- plenty of helpdesks run their own ticketing system on
    the same LAN -- but it has to be a decision, because with it on a webhook URL becomes a
    way to make the hub issue requests to anything it can reach, including its own admin
    interfaces.
    """
    return (os.environ.get("NOTIFY_WEBHOOK_ALLOW_PRIVATE") or "").strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------------------

_db_path = None
_worker_started = False
_worker_lock = threading.Lock()
_wake = threading.Event()


def configure(db_path):
    """Point the module at the database and start the delivery worker. Idempotent."""
    global _db_path, _worker_started
    _db_path = db_path
    init_notify_db(db_path)
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker, daemon=True, name="notify_worker").start()


def deliver(action, context):
    """The rules engine's delivery callback: (action, context) -> (ok, detail).

    Renders the templates, enqueues, and returns. `ok` here means "accepted for delivery",
    not "delivered" -- the outbox row is the record of what actually happened, and the rule's
    fire history links to it. Reporting real delivery would mean blocking the evaluator on a
    remote host, which is the one thing this module exists to avoid.
    """
    if _db_path is None:
        return False, "notifications are not configured"
    # Imported here rather than at module scope: rules.py imports nothing from notify, and
    # doing it the other way at import time would make the pair circular.
    import rules

    kind = action.get("type")
    params = action.get("params") or {}
    variables = context.get("variables") or {}
    machine = context.get("machine")
    rule = context.get("rule") or {}

    if kind == KIND_WEBHOOK:
        payload = {
            "url": params.get("url"),
            "body": {
                "machine": machine,
                "rule": {"id": rule.get("id"), "name": rule.get("name")},
                "fired_at": int(context.get("now") or time.time()),
                "message": rules.render_template(params.get("template") or "", variables),
                "variables": {name: value.value for name, value in variables.items()
                              if value.known and not name.startswith("proc.")},
            },
        }
    elif kind == KIND_EMAIL:
        payload = {
            "to": params.get("to") or [],
            "subject": rules.render_template(params.get("subject") or "", variables),
            "body": rules.render_template(params.get("body") or "", variables),
        }
    else:
        return False, f"unknown notification kind: {kind}"

    now = int(context.get("now") or time.time())
    with get_conn(_db_path) as conn:
        cur = conn.execute(
            "INSERT INTO notify_outbox (kind, payload_json, next_attempt_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, json.dumps(payload, default=str), now, now),
        )
        outbox_id = cur.lastrowid
    _wake.set()
    return True, {"queued": outbox_id}


# ---------------------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------------------

WORKER_IDLE_SECONDS = 30


def _worker():
    while True:
        try:
            sent = send_due(_db_path)
        except Exception as e:                        # noqa: BLE001
            print(f"[notify] Delivery pass failed: {e}")
            sent = 0
        # Sleep until woken by a new enqueue, or until the next poll. The event makes a
        # freshly-queued message go out in milliseconds rather than waiting out the poll.
        _wake.wait(timeout=1 if sent else WORKER_IDLE_SECONDS)
        _wake.clear()


def send_due(db_path, now=None):
    """Attempt every message that is due. Returns how many were sent."""
    if not db_path:
        return 0
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM notify_outbox WHERE status = ? AND next_attempt_at <= ? "
            "ORDER BY id LIMIT 50",
            (STATUS_PENDING, now),
        ).fetchall()
    sent = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            _mark(db_path, row["id"], STATUS_FAILED, row["attempts"], "unreadable payload", now)
            continue
        try:
            if row["kind"] == KIND_WEBHOOK:
                _send_webhook(payload)
            else:
                _send_email(payload)
            _mark(db_path, row["id"], STATUS_SENT, row["attempts"] + 1, "", now)
            sent += 1
        except Exception as e:                        # noqa: BLE001
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                _mark(db_path, row["id"], STATUS_FAILED, attempts, str(e)[:500], now)
            else:
                _mark(db_path, row["id"], STATUS_PENDING, attempts, str(e)[:500], now,
                      next_at=now + RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)])
    return sent


def _mark(db_path, outbox_id, status, attempts, error, now, next_at=None):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE notify_outbox SET status=?, attempts=?, last_error=?, "
            "next_attempt_at=?, sent_at=? WHERE id=?",
            (status, attempts, error, next_at if next_at is not None else now,
             now if status == STATUS_SENT else None, outbox_id),
        )


def prune_outbox(db_path, retention_days=OUTBOX_RETENTION_DAYS, now=None):
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM notify_outbox WHERE status != ? AND created_at < ?",
            (STATUS_PENDING, now - int(retention_days) * 86400),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------------------
# The two transports
# ---------------------------------------------------------------------------------------


def check_webhook_url(url, allow_private=None):
    """Validate a webhook target just before connecting. Returns an error string or None.

    The check happens HERE rather than when the rule is saved, because what a hostname
    resolves to is not knowable in advance -- and because the answer can change between the
    two moments.

    Worth being honest about the limit: resolving and then handing the original URL to
    requests leaves a DNS-rebinding window, since requests resolves again itself. Closing it
    properly means connecting to the pinned address and carrying the hostname in SNI plus the
    Host header, which is a meaningful amount of machinery for a URL that only a MANAGE_RULES
    holder can set in the first place. This is defence in depth around an already-trusted
    input, not the perimeter.
    """
    if allow_private is None:
        allow_private = webhooks_allow_private()
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https":
        return "webhook URL must be https"
    if not parsed.hostname:
        return "webhook URL has no host"
    if allow_private:
        return None
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return f"cannot resolve {parsed.hostname}: {exc}"
    for info in infos:
        address = ip_address(info[4][0])
        # link_local covers 169.254.0.0/16, which is where the cloud metadata endpoints live
        # -- the single most valuable target an SSRF has.
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            return (f"{parsed.hostname} resolves to a private address ({address}); "
                    "set NOTIFY_WEBHOOK_ALLOW_PRIVATE=1 if that is intended")
    return None


def _send_webhook(payload):
    url = payload.get("url")
    error = check_webhook_url(url)
    if error:
        raise ValueError(error)
    response = requests.post(url, json=payload.get("body") or {},
                             timeout=WEBHOOK_TIMEOUT_SECONDS,
                             # No redirects: a 302 to http://169.254.169.254 would walk
                             # straight past every check above.
                             allow_redirects=False,
                             headers={"User-Agent": "FleetHub-Rules/1.0"})
    if response.status_code >= 400:
        raise ValueError(f"webhook returned HTTP {response.status_code}")


def _send_email(payload):
    config = smtp_config()
    if not config:
        raise ValueError("SMTP is not configured (set SMTP_HOST and SMTP_FROM in .env)")
    recipients = [str(r) for r in (payload.get("to") or []) if str(r).strip()]
    if not recipients:
        raise ValueError("no recipients")

    message = EmailMessage()
    message["From"] = config["from"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = str(payload.get("subject") or "")
    message["Date"] = formatdate(localtime=True)
    message.set_content(str(payload.get("body") or ""))

    if config["security"] == "ssl":
        server = smtplib.SMTP_SSL(config["host"], config["port"],
                                  timeout=SMTP_TIMEOUT_SECONDS)
    else:
        server = smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS)
    try:
        if config["security"] == "starttls":
            server.starttls()
        if config["user"]:
            server.login(config["user"], config["password"])
        server.send_message(message, to_addrs=recipients)
    finally:
        try:
            server.quit()
        except Exception:                             # noqa: BLE001
            pass
