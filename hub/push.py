"""Push notification to paired devices -- what makes a phone build worth having (roadmap #11).

The desktop client sits in the tray and polls `/api/alerts` every ten seconds, so an alert
raised at 02:00 reaches an operator with the window shut. **Android does not allow that.**
A backgrounded app stops polling, so without push an Android build shows alerts only while
it is open on screen -- which removes most of the reason to want the fleet on a phone.
Roadmap #11 phase 2 therefore lists push not as a later refinement of the Android build but
as its **prerequisite**, and this module is that prerequisite.

`api_tokens` has carried nullable `push_kind` / `push_token` columns since the pairing work
landed, deliberately unused, so that push would register against the device registry that
already exists rather than a second one that could disagree about which devices there are.
This module is the first thing to read or write them.

Four decisions shape everything below.

**The delta is computed here, not signalled from the alert raise path.** Nothing calls into
this module when an alert is raised; a scan reads `alerts` on a timer and works out what
each device has not been told about yet. Two reasons, and the second is the load-bearing
one. First, a callback would have to live in `alerts.upsert_rule`, which cannot today tell a
NEW episode from the refresh of a running one -- a rule matched for a week would push on
every evaluation tick, which is precisely the notification storm the client's `_priming`
exists to prevent. Second, a per-device cursor is the same shape as the client's own
alert-delta seen-set, so the phone and the desktop agree about what "new" means instead of
holding two definitions that drift.

**Priming, for the same reason the client primes.** A device registering for push for the
first time has its cursor set to the current high-water mark and is told nothing. Without
that, pairing a phone on a Monday morning delivers every alert the fleet has accumulated,
and an operator whose first experience of FleetHub push is forty notifications learns to
swipe them away unread -- and the one they eventually swipe away unread is the one that
mattered. The same rule covers a hub that has had push switched on after months of running.

**The payload carries a COUNT and no content.** Not the machine, not the rule, not a
temperature. Two independent reasons that happen to agree: a push transits Google's (or
Apple's) servers, and a helpdesk's machine names are the inventory of somebody's estate; and
a notification is a nudge to open the app, where the alert list is already scoped, already
filtered and already correct. What the device is told is "you have N alerts you have not
seen", which is true, useful and worth nothing to anyone who intercepts it.

**The text is localized on the DEVICE, not here.** FCM and APNs both take a localization
KEY plus arguments rather than a rendered string (`title_loc_key` / `body_loc_key` /
`body_loc_args`, and APNs' `title-loc-key` / `loc-key` / `loc-args`), and the operating
system resolves them against the app's own bundled catalog. That is a better fit than
rendering here even setting aside the hub not knowing the device's language: roadmap #11
chose BUNDLED catalogs over fetched ones because this app gets opened in a car park on a
phone with no signal, and a notification body rendered by the hub would be the one string in
the app that still depended on a round trip.

**Delivery rides notify.py's outbox rather than a second queue.** A push is an outbound
message that must survive a hub restart, must not block the thread that noticed it, and must
back off when the far end is down -- all of which notify.py already does for webhooks and
email. `KIND_PUSH` is a third transport there, and this module only decides WHO gets told
and WHAT the count is.

**APNs is deliberately not implemented**, and this is a refusal rather than an omission.
`push_kind` accepts `apns` so the registry is right the day an iOS build exists, but the
transport raises. APNs is HTTP/2 only and `requests` is HTTP/1.1, so building it means a
second HTTP client in `hub/requirements.txt` -- for a platform that needs an Apple Developer
account this project does not have, against a build roadmap #11 lists as a `link` kind that
has never been produced. A dependency taken now would be an untested dependency by the time
anyone could use it. See send_push in notify.py for the refusal's wording.

Kept free of Flask so it can be unit-tested in isolation, exactly like apitokens.py,
alerts.py and wake.py; push_web.py is the HTTP surface over it.
"""
import json
import os
import threading
import time

import alerts
import fleet
import permissions

# ================================
# VOCABULARY
# ================================
#: Google's Firebase Cloud Messaging -- the Android transport, and the only one built.
KIND_FCM = "fcm"
#: Apple Push Notification service. Accepted at registration, refused at send. See the
#: module docstring for why that is the honest state rather than a gap.
KIND_APNS = "apns"
PUSH_KINDS = (KIND_FCM, KIND_APNS)

#: The localization keys the device resolves against its own bundled catalog. They are an
#: API between this hub and every installed client, in the same sense the `MIN_*_AGENT`
#: constants in hub/static/js/ are: renaming one here silently turns every notification on
#: every phone into its raw key, because that is what Android and iOS show when a
#: `*_loc_key` does not resolve. Change them only alongside a client release that has the
#: new names, never on their own.
LOC_KEY_TITLE = "push_title"
LOC_KEY_BODY = "push_alerts"

#: How often the scan looks for alerts a device has not been told about. Ten seconds is the
#: desktop client's own poll interval, so a phone is never more stale than a laptop that is
#: awake; the scan is a single indexed read plus one permissions lookup per DISTINCT owner,
#: so the cost of it is set by how many operators have paired a phone, not by fleet size.
SCAN_INTERVAL_SECONDS = 10

#: A registration token from FCM is ~163 characters today and Google has changed that
#: before, so this is a sanity bound against a body that is trying to be a payload, not a
#: format check. A token that is merely wrong fails at send and is cleared there.
MAX_PUSH_TOKEN_CHARS = 512

#: Google's own ceiling on an FCM OAuth access token is an hour. Refreshing a few minutes
#: early costs one extra token mint per hour and removes the case where a token that was
#: valid when the send started has expired by the time it arrives.
_ACCESS_TOKEN_SKEW_SECONDS = 300

FCM_TIMEOUT_SECONDS = 10
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"


class PushError(Exception):
    """A push that cannot be honoured, with a message meant for a human."""


# ================================
# SCHEMA
# ================================
def init_push_db(db_path):
    with fleet.get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per device that has ever been scanned. `last_alert_id` is a high-water
        # mark over the WHOLE alerts table rather than over the open ones: dismissing the
        # newest alert lowers max(open id), and a cursor that followed that would re-push
        # every alert below it. Monotonic is the only safe shape for a "have I told them
        # about this" mark.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_cursors (
                token_id      TEXT PRIMARY KEY,
                last_alert_id INTEGER NOT NULL DEFAULT 0,
                last_count    INTEGER NOT NULL DEFAULT 0,
                updated_at    INTEGER NOT NULL
            )
        """)


# ================================
# CONFIGURATION
# ================================
def fcm_config():
    """FCM credentials from the environment. Returns None when push is not configured.

    Not configured is the normal state and never an error -- a helpdesk with no phones
    should see nothing about push anywhere, which is the same rule notify.smtp_config()
    applies to a hub that only wants webhooks.

    **In .env rather than the settings table, and that is a security decision rather than a
    filing one.** A service-account key is a credential, and the settings database is inside
    the nightly backup -- the identical reasoning that keeps SMTP credentials (notify.py)
    and the BIOS setup password (fleet.py) out of it. `FCM_SERVICE_ACCOUNT` is a PATH for
    the same reason: a 2 kB JSON blob pasted into a dotenv line is a blob that gets
    truncated by an editor exactly once, and the file it names can carry its own ACL.
    """
    path = (os.environ.get("FCM_SERVICE_ACCOUNT") or "").strip()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            account = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PushError(f"FCM_SERVICE_ACCOUNT ({path}) cannot be read: {exc}")

    project = (os.environ.get("FCM_PROJECT_ID") or account.get("project_id") or "").strip()
    client_email = str(account.get("client_email") or "").strip()
    private_key = str(account.get("private_key") or "")
    if not (project and client_email and private_key):
        raise PushError(
            f"FCM_SERVICE_ACCOUNT ({path}) is missing project_id, client_email or "
            "private_key -- it should be the JSON key file Firebase hands you for a "
            "service account, unedited.")
    return {"project": project, "client_email": client_email, "private_key": private_key}


def is_configured():
    """Whether this hub can send a push at all. Never raises -- the console and the app
    both ask this to explain themselves, and a question about configuration that throws
    when the configuration is broken tells the operator nothing."""
    try:
        return fcm_config() is not None
    except PushError:
        return False


# ================================
# REGISTRATION
# ================================
def normalize_kind(kind):
    value = str(kind or "").strip().lower()
    if value not in PUSH_KINDS:
        raise PushError(f"Push kind must be one of {', '.join(PUSH_KINDS)}.")
    return value


def check_registration(kind, push_token):
    """Validate a registration request. Returns an error string for the caller, or None.

    Deliberately a returned STRING rather than a raised exception, and the shape is
    notify.check_webhook_url's on purpose. The reason is not style: push_web.py answers a
    bad request with this text, and handing an exception's `str()` to an HTTP response is
    how a message written for a log ends up on the wire. `fcm_config` raises PushError
    carrying a filesystem path and an OS error -- correct for a hub operator reading a
    console, wrong for a device that supplied a typo -- and nothing in a web layer should
    have to know which PushError it caught. CodeQL flagged the original for exactly this
    (`py/stack-trace-exposure`), and it was right to: the messages are safe TODAY, and the
    next one added is one nobody re-checks.

    Every string here is written for the app author who sent the request.
    """
    value = str(kind or "").strip().lower()
    if value not in PUSH_KINDS:
        return f"Push kind must be one of {', '.join(PUSH_KINDS)}."
    token = str(push_token or "").strip()
    if not token:
        return "A push registration needs a token."
    if len(token) > MAX_PUSH_TOKEN_CHARS:
        return "That push token is implausibly long."
    return None


def register(db_path, token_id, kind, push_token, now=None):
    """Record where to push this device, and prime its cursor. Returns True if it landed.

    A device registers ITSELF, over its own bearer token -- there is no path by which one
    operator registers a push token for another's device, because the only caller is
    push_web.py's route and the token id comes from the authenticated identity rather than
    from the body.

    **Priming happens here, on the first registration, not at the first scan.** A device
    that registers on Monday must not be told about Friday's alerts; see the module
    docstring. A RE-registration (the ordinary case -- FCM rotates a registration token
    whenever the app is reinstalled or its data cleared) deliberately leaves the cursor
    alone, because it is the same device and the same operator and they have not stopped
    having seen what they had seen.
    """
    kind = normalize_kind(kind)
    push_token = str(push_token or "").strip()
    if not push_token:
        raise PushError("A push registration needs a token.")
    if len(push_token) > MAX_PUSH_TOKEN_CHARS:
        raise PushError("That push token is implausibly long.")
    now = int(now if now is not None else time.time())

    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE api_tokens SET push_kind = ?, push_token = ? "
            "WHERE token_id = ? AND revoked = 0",
            (kind, push_token, str(token_id))).rowcount
        if not changed:
            return False
        # INSERT OR IGNORE, so a re-registration keeps the cursor it already had.
        conn.execute(
            "INSERT OR IGNORE INTO push_cursors(token_id, last_alert_id, updated_at) "
            "VALUES (?, ?, ?)",
            (str(token_id), _max_alert_id(conn), now))
    return True


def unregister(db_path, token_id):
    """Stop pushing to this device. Returns True if a row changed.

    The cursor stays. An operator who turns notifications off in the app and back on an hour
    later is the same person who has seen the same alerts, and clearing the cursor would
    make re-enabling push deliver a backlog -- which is the one thing that teaches somebody
    to leave it off.
    """
    with fleet.get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE api_tokens SET push_kind = NULL, push_token = NULL WHERE token_id = ?",
            (str(token_id),)).rowcount > 0


def registration(db_path, token_id):
    """What this device is registered for, or None. The token is reported only as a
    presence flag: it is a credential addressed to this device, and no console screen or
    API response has any use for its value."""
    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT push_kind, push_token FROM api_tokens WHERE token_id = ?",
            (str(token_id),)).fetchone()
    if row is None or not row["push_token"]:
        return None
    return {"kind": row["push_kind"], "registered": True}


def push_devices(db_path, now=None):
    """Every device that can be pushed to right now: registered, not revoked, not expired.

    Expiry is checked here rather than left to the send, because an expired token is a
    device whose operator stopped using it -- pushing to it would be delivering a fleet
    notification to a phone that can no longer open the fleet.
    """
    now = int(now if now is not None else time.time())
    with fleet.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT token_id, email, device_name, platform, capabilities_json, "
            "       directory_groups_json, push_kind, push_token "
            "FROM api_tokens "
            "WHERE revoked = 0 AND expires_at > ? "
            "      AND push_token IS NOT NULL AND push_token != '' "
            "ORDER BY token_id",
            (now,)).fetchall()
    return [dict(r) for r in rows]


# ================================
# THE SCAN
# ================================
def _max_alert_id(conn):
    row = conn.execute("SELECT MAX(id) AS top FROM alerts").fetchone()
    return int((row["top"] if row else 0) or 0)


def _json_list(raw):
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []


def _device_permissions(db_path, device, superusers, cache):
    """The effective permissions of one DEVICE: its owner's live set, intersected with the
    ceiling stored on the token.

    This is permissions_web._narrow_to_device's rule, applied without a request. It has to
    be, and stating why is the point of this function existing: a token is a CEILING
    intersected LIVE, so a device paired while its owner was an admin must stop seeing an
    admin's fleet the moment they are demoted. A push path that read only the owner's
    permissions would be a way to have alerts about machines the device's own API calls
    would refuse to show it.

    `cache` is keyed on the owner rather than the device because two phones belonging to one
    operator share one permissions build, and that build is the expensive half.
    """
    email = permissions.normalize_email(device.get("email"))
    groups = tuple(_json_list(device.get("directory_groups_json")))
    key = (email, groups)
    if key not in cache:
        cache[key] = permissions.effective_permissions(
            db_path, email, superusers=superusers, directory_groups=groups)
    current = cache[key]

    ceiling = set(_json_list(device.get("capabilities_json")))
    narrowed = set(current.get("capabilities") or ()) & ceiling
    out = dict(current)
    out["capabilities"] = narrowed
    out["superuser"] = bool(current.get("superuser")) and narrowed == set(
        permissions.CAPABILITIES)
    return out


def _alert_in_scope(perms, alert):
    """Whether one alert is about a machine this device may see.

    Two shapes, because alerts.py has two: a `rule` or `ad_unmatched` alert names ONE
    machine, while `duplicate_serial` names the colliding set in `machines` and leaves
    `machine` empty. Checking only the singular field would silently drop every duplicate
    alert for a scoped operator and -- worse -- pass every one of them to an unrestricted
    one as an alert about the empty-string machine. Any machine in scope makes the alert in
    scope, which is the same answer the console's own alert list gives.
    """
    names = [n for n in (alert.get("machines") or []) if n]
    if alert.get("machine"):
        names.append(alert["machine"])
    return any(permissions.machine_in_scope(perms, n) for n in names)


def scan(db_path, superusers=(), now=None, enqueue=None):
    """Work out what each registered device has not been told about, and enqueue it.

    Returns the list of enqueued pushes, which is what makes this testable: the delta, the
    scoping and the priming are all observable without a transport. `enqueue` defaults to
    notify.enqueue_push and is a parameter for the same reason -- a test that needed a real
    FCM project would test nothing about the rule this function exists to implement.

    **One push per device per scan, carrying a count.** Not one per alert. A rule that trips
    on forty machines at once is one notification saying forty, because forty notifications
    for one event is the storm, and the app's own list is where the detail belongs.

    A device whose count has not changed since its last push is left alone. Without that,
    a machine that stays in alert would re-notify every ten seconds forever.
    """
    now = int(now if now is not None else time.time())
    if enqueue is None:
        import notify
        enqueue = notify.enqueue_push

    prune_cursors(db_path)
    devices = push_devices(db_path, now=now)
    if not devices:
        return []

    open_alerts = alerts.list_open(db_path)
    with fleet.get_conn(db_path) as conn:
        high_water = _max_alert_id(conn)
        cursors = {r["token_id"]: r for r in conn.execute(
            "SELECT token_id, last_alert_id, last_count FROM push_cursors").fetchall()}

    cache = {}
    sent = []
    for device in devices:
        token_id = device["token_id"]
        cursor = cursors.get(token_id)
        if cursor is None:
            # A device the scan has never seen -- push nothing, start it at the high-water
            # mark. Registration primes too; this covers a row that predates this table.
            _write_cursor(db_path, token_id, high_water, 0, now)
            continue

        perms = _device_permissions(db_path, device, superusers, cache)
        if not permissions.has_capability(perms, permissions.VIEW):
            # The owner lost `view`, or the device was never granted it. Advance the cursor
            # anyway: they are not owed a backlog of what they could not see if their
            # permissions come back.
            _write_cursor(db_path, token_id, high_water, 0, now)
            continue

        unseen = [a for a in open_alerts
                  if int(a.get("id") or 0) > int(cursor["last_alert_id"] or 0)
                  and _alert_in_scope(perms, a)]
        count = len(unseen)
        if count and count != int(cursor["last_count"] or 0):
            sent.append({
                "token_id": token_id,
                "kind": device.get("push_kind") or KIND_FCM,
                "push_token": device.get("push_token"),
                "count": count,
            })
            enqueue(db_path, kind=device.get("push_kind") or KIND_FCM,
                    push_token=device.get("push_token"), token_id=token_id,
                    count=count, now=now)
        # The cursor does NOT advance to the high-water mark here: an operator who has been
        # told about three alerts and opens none of them has still not seen them, and the
        # next new alert should say four rather than one. It advances when the app tells us
        # it has been read (see acknowledge), or when the alerts are resolved out from
        # under it. `last_count` is what stops the repeat.
        _write_cursor(db_path, token_id, cursor["last_alert_id"], count, now)
    return sent


def acknowledge(db_path, token_id, now=None):
    """The device opened its alert list: everything up to now has been seen.

    Called from push_web.py when the app foregrounds. This is the cursor's only forward
    move outside priming, and it belongs to the DEVICE rather than to a timer -- a
    notification that was delivered is not a notification that was read, and pretending
    otherwise is how the fourth alert of the night arrives saying "1".
    """
    now = int(now if now is not None else time.time())
    with fleet.get_conn(db_path) as conn:
        high_water = _max_alert_id(conn)
        conn.execute(
            "INSERT INTO push_cursors(token_id, last_alert_id, last_count, updated_at) "
            "VALUES (?, ?, 0, ?) "
            "ON CONFLICT(token_id) DO UPDATE SET last_alert_id = ?, last_count = 0, "
            "                                    updated_at = ?",
            (str(token_id), high_water, now, high_water, now))
    return high_water


def _write_cursor(db_path, token_id, last_alert_id, last_count, now):
    with fleet.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO push_cursors(token_id, last_alert_id, last_count, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token_id) DO UPDATE SET last_alert_id = ?, last_count = ?, "
            "                                    updated_at = ?",
            (str(token_id), int(last_alert_id), int(last_count), now,
             int(last_alert_id), int(last_count), now))


def prune_cursors(db_path):
    """Drop cursors for devices that have been revoked or deleted. Returns how many went.

    Housekeeping on the way past the scan rather than a sweeper thread or a hook in the
    revoke path -- the same call apitokens.create_grant makes for expired grants, and for
    the same reason: the table is small, the work is one indexed DELETE, and a hook in
    apitokens.py would put push's business in the file that authenticates the fleet.

    **Revoked, not unregistered.** A device whose operator turned notifications off in the
    app keeps its cursor deliberately: they are the same person who has seen the same
    alerts, and turning push back on must not deliver a backlog. A revoked device is not
    coming back -- re-pairing mints a new token id.
    """
    with fleet.get_conn(db_path) as conn:
        return conn.execute(
            "DELETE FROM push_cursors WHERE token_id NOT IN "
            "(SELECT token_id FROM api_tokens WHERE revoked = 0)").rowcount


# ================================
# THE MESSAGE
# ================================
def build_fcm_message(project, push_token, count):
    """The FCM HTTP v1 body for one device.

    **`*_loc_key` rather than a rendered string**, so the phone's own bundled catalog
    supplies the words -- see the module docstring. `body_loc_args` carries the count as a
    string because that is what the v1 schema declares, and a JSON number there is rejected
    by the API rather than coerced.

    `priority: high` is required and not decoration: a normal-priority Android message is
    held by Doze until the device next wakes, which for a phone in a pocket at 02:00 is the
    morning -- the exact delay this whole feature exists to remove.
    """
    return {
        "message": {
            "token": push_token,
            # No top-level `notification` block. It takes a literal title and body, which is
            # the one thing this message must not carry -- every word is under `android`,
            # where FCM accepts localization keys instead.
            "android": {
                "priority": "high",
                "notification": {
                    "title_loc_key": LOC_KEY_TITLE,
                    "body_loc_key": LOC_KEY_BODY,
                    "body_loc_args": [str(int(count))],
                    # One notification per device, replaced rather than stacked: the count
                    # already says how many, so a second one saying a bigger number is two
                    # notifications describing one situation.
                    "tag": "fleethub-alerts",
                },
            },
            # No machine, no rule, no temperature -- see the module docstring. The count is
            # duplicated here so the app can update a badge without parsing the body.
            "data": {"alerts": str(int(count))},
        }
    }


# ================================
# THE TRANSPORT
# ================================
_access_token = {"value": None, "expires_at": 0}
_access_lock = threading.Lock()


def _sign_jwt(config, now):
    """A service-account assertion for Google's token endpoint, signed RS256.

    Hand-rolled against `cryptography`, which is already a hub dependency (backups.py needs
    it for AES-256-GCM), rather than taking `google-auth`. That library brings a transport
    stack, a credentials-discovery chain and an async half for what is thirty lines of
    signing -- and the hub's dependency list is read by whoever has to approve an install on
    a helpdesk's server.
    """
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def segment(payload):
        return base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=")

    header = segment({"alg": "RS256", "typ": "JWT"})
    claims = segment({
        "iss": config["client_email"],
        "scope": FCM_SCOPE,
        "aud": FCM_TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    })
    signing_input = header + b"." + claims
    key = serialization.load_pem_private_key(
        config["private_key"].encode("utf-8"), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def access_token(config=None, now=None, force=False):
    """A cached OAuth access token for FCM. Minted on demand, reused for its lifetime.

    Cached process-wide because a fleet-wide alert enqueues one outbox row per phone, and
    minting a token per row would put an RSA signature and a round trip to Google in front
    of every single send.
    """
    import requests

    now = int(now if now is not None else time.time())
    config = config or fcm_config()
    if config is None:
        raise PushError("Push is not configured (set FCM_SERVICE_ACCOUNT in .env).")

    with _access_lock:
        if not force and _access_token["value"] and _access_token["expires_at"] > now:
            return _access_token["value"]
        response = requests.post(
            FCM_TOKEN_URL, timeout=FCM_TIMEOUT_SECONDS, allow_redirects=False,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": _sign_jwt(config, now)})
        if response.status_code >= 400:
            raise PushError(
                f"FCM rejected this hub's service account (HTTP {response.status_code}). "
                "Check FCM_SERVICE_ACCOUNT names the key file for the right project.")
        body = response.json()
        token = str(body.get("access_token") or "")
        if not token:
            raise PushError("FCM returned no access token.")
        _access_token["value"] = token
        _access_token["expires_at"] = now + max(
            60, int(body.get("expires_in") or 3600) - _ACCESS_TOKEN_SKEW_SECONDS)
        return token


def send_fcm(push_token, count, config=None, now=None):
    """Deliver one push. Raises on failure; notify.py's outbox owns the retry.

    **A 404 or a 403 on a registration token is PERMANENT and says so**, because FCM uses
    them to mean "this token is dead" -- the app was uninstalled, or its data was cleared.
    Retrying that four times over twenty minutes, for every alert, for a phone that no
    longer exists, is how an outbox fills with noise that hides the failures worth reading.
    The caller clears the registration on a PushError carrying `permanent`.
    """
    import requests

    config = config or fcm_config()
    if config is None:
        raise PushError("Push is not configured (set FCM_SERVICE_ACCOUNT in .env).")

    response = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{config['project']}/messages:send",
        json=build_fcm_message(config["project"], push_token, count),
        timeout=FCM_TIMEOUT_SECONDS, allow_redirects=False,
        headers={"Authorization": f"Bearer {access_token(config, now=now)}",
                 "Content-Type": "application/json"})
    if response.status_code in (403, 404):
        error = PushError(f"FCM says this device is gone (HTTP {response.status_code}).")
        error.permanent = True
        raise error
    if response.status_code >= 400:
        raise PushError(f"FCM returned HTTP {response.status_code}.")
    return True


# ================================
# THE SCAN THREAD
# ================================
_db_path = None
_superusers = ()
_worker_started = False
_worker_lock = threading.Lock()


def configure(db_path, superusers=()):
    """Point the module at the database and start the scan thread. Idempotent.

    Separate from init_push_db for the reason notify.configure is separate from the init_*
    calls: it owns a thread. The thread starts even when FCM is unconfigured, and costs one
    indexed read per interval to find no registered devices -- so switching push on is
    editing .env and restarting, not editing .env and wondering why nothing happens.
    """
    global _db_path, _superusers, _worker_started
    _db_path = db_path
    _superusers = superusers
    init_push_db(db_path)
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker, daemon=True, name="push_scan").start()


def _worker():
    while True:
        time.sleep(SCAN_INTERVAL_SECONDS)
        try:
            scan(_db_path, superusers=_superusers)
        except Exception as e:                        # noqa: BLE001
            print(f"[push] Scan failed: {e}")
