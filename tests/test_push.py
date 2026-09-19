"""push.py -- push to paired devices (roadmap #11 phase 2).

**The silent failure this file exists to catch is a phone that is told the wrong thing,
quietly.** Every failure mode here looks like success from the hub: the outbox row says
sent, the console shows nothing, and the only witness is an operator whose phone either
buzzed forty times at 08:00 or never buzzed at all. None of it raises, none of it logs, and
none of it is visible without asking what the scan decided.

Four rules carry that, and each is tested against the case that would produce a
plausible-looking wrong answer rather than against the happy path:

  * **Priming.** A device registering for push must be told about nothing that already
    existed. Get this wrong and pairing a phone on a Monday delivers the fleet's whole
    open-alert backlog as notifications -- the storm the desktop client's `_priming` exists
    to prevent, arriving at registration instead of at startup.

  * **Scope, intersected with the token's ceiling.** The count is the whole message, so an
    alert about a machine this device may not see must not be in it. A push path that read
    only the OWNER's permissions would be a way to be notified about machines the device's
    own API calls would refuse to show it -- the credential outliving the grant, in the one
    place nobody would think to look for it. The ceiling test therefore demotes the owner
    AFTER pairing, which is the sequence that would otherwise pass.

  * **Delivery is not readership.** The cursor only moves forward when the app says the
    list was looked at. If the scan advanced it on its own, the second alert of the night
    would announce "1" while three sat waiting.

  * **Repeat suppression.** The scan runs every ten seconds and an alert stays open for as
    long as its condition holds. Without the count check, one overheating PC is a
    notification every ten seconds until somebody unplugs it.

Also asserted here, because both are contracts rather than implementation: the FCM message
carries a localization KEY and no machine name (content-free payloads are a security
property, and `*_loc_key` is what lets the phone's own bundled catalog supply the words),
and `apns` is a refusal with a reason rather than a row that sits pending forever.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import alerts
import apitokens
import fleet
import notify
import permissions
import push

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


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
def _db():
    """A fresh database per test, plus the module-level caches reset.

    One database per test rather than one per module, because permissions.py keeps a
    process-level cache of the group table: one test's groups leaking into the next would
    make the scope assertions depend on the order they run in, which is the kind of green
    that turns red the day somebody adds a test in the middle.
    """
    permissions.invalidate()
    db_path = os.path.join(tempfile.mkdtemp(prefix="hub-push-test-"), "temp_v2.db")
    fleet.init_fleet_db(db_path)
    alerts.init_alerts_db(db_path)
    permissions.init_permissions_db(db_path)
    apitokens.init_apitokens_db(db_path)
    push.init_push_db(db_path)
    notify.init_notify_db(db_path)
    return db_path


def pair(db_path, email, capabilities=(permissions.VIEW,), device="Phone", now=1000):
    """Mint a device token the way redeem_grant would, and return its token id."""
    _, row = apitokens.mint_token(db_path, email=email, device_name=device,
                                  platform="android", capabilities=list(capabilities),
                                  now=now)
    return row["token_id"]


def group(db_path, name, email, capabilities, machines=None):
    """A permission group with one member, which is how a scope gets attached to an email."""
    permissions.create_group(
        db_path, name,
        capabilities=list(capabilities),
        machines=list(machines or []),
        members=[email],
        scope_mode=(permissions.SCOPE_ALL if machines is None else permissions.SCOPE_LIST),
        actor="root@example.com")
    permissions.invalidate()


def collector():
    """An `enqueue` that records instead of queueing -- see push.scan's docstring."""
    seen = []

    def enqueue(db_path, kind, push_token, token_id, count, now=None):
        seen.append({"token_id": token_id, "kind": kind, "count": count})

    return seen, enqueue


# --------------------------------------------------------------------------------------
# Priming
# --------------------------------------------------------------------------------------
def test_registration_primes_and_tells_nobody_about_the_backlog():
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")

    # Three alerts that already existed when the phone was registered.
    for n in range(3):
        alerts.upsert_rule(db_path, f"PC-OLD-{n}", 40 + n, "Disk nearly full", "96%")

    check("registering a paired device lands",
          push.register(db_path, token_id, "fcm", "fcm-token-1") is True)

    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=2000, enqueue=enqueue)
    check("a freshly registered device is told nothing about the backlog", seen == [])

    alerts.upsert_rule(db_path, "PC-NEW", 77, "Disk nearly full", "97%")
    push.scan(db_path, superusers=(), now=2010, enqueue=enqueue)
    check("...but the first alert raised AFTER it registered does reach it",
          [s["count"] for s in seen] == [1])


def test_reregistration_does_not_replay():
    """FCM rotates a registration token on every reinstall, so re-registering is the
    ORDINARY case. If it reset the cursor, every app update would deliver a backlog."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "fcm-token-1")

    alerts.upsert_rule(db_path, "PC-A", 11, "Hot", "91C")
    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=3000, enqueue=enqueue)
    check("the open alert is pushed once", [s["count"] for s in seen] == [1])

    push.acknowledge(db_path, token_id, now=3005)
    push.register(db_path, token_id, "fcm", "fcm-token-2-rotated")
    push.scan(db_path, superusers=(), now=3010, enqueue=enqueue)
    check("re-registering with a rotated token replays nothing",
          [s["count"] for s in seen] == [1])
    check("...and the new address is the one that would be used",
          push.push_devices(db_path, now=3010)[0]["push_token"] == "fcm-token-2-rotated")


def test_unregistering_keeps_the_cursor():
    """Turning notifications off and on again must not deliver what was missed -- that is
    the one experience that teaches somebody to leave them off."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "tok")
    alerts.upsert_rule(db_path, "PC-A", 11, "Hot", "91C")
    push.acknowledge(db_path, token_id, now=4000)

    check("unregistering reports the change", push.unregister(db_path, token_id) is True)
    check("...and the device is no longer pushable",
          push.push_devices(db_path, now=4001) == [])
    alerts.upsert_rule(db_path, "PC-B", 12, "Hot", "92C")

    push.register(db_path, token_id, "fcm", "tok")
    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=4100, enqueue=enqueue)
    check("re-enabling push delivers the one alert raised while it was off, not a backlog",
          [s["count"] for s in seen] == [1])


# --------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------
def test_count_is_scoped_to_what_the_device_may_see():
    db_path = _db()
    group(db_path, "Ward", "ward@x.com", [permissions.VIEW], machines=["PC-WARD"])
    group(db_path, "Everything", "boss@x.com", [permissions.VIEW])
    ward = pair(db_path, "ward@x.com", device="Ward phone")
    boss = pair(db_path, "boss@x.com", device="Boss phone")
    push.register(db_path, ward, "fcm", "tok-ward")
    push.register(db_path, boss, "fcm", "tok-boss")

    alerts.upsert_rule(db_path, "PC-WARD", 21, "Hot", "91C")
    alerts.upsert_rule(db_path, "PC-OTHER", 22, "Hot", "92C")

    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=5000, enqueue=enqueue)
    counts = {s["token_id"]: s["count"] for s in seen}
    check("a scoped operator's phone counts only their own machine",
          counts.get(ward) == 1)
    check("...while an unrestricted operator's counts both", counts.get(boss) == 2)


def test_the_token_ceiling_is_intersected_live():
    """The load-bearing one. A device paired while its owner held `view` must stop being
    pushed the moment they lose it -- the credential must not outlive the grant, in the one
    path that runs with no request to check."""
    db_path = _db()
    group(db_path, "Viewers", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com", capabilities=[permissions.VIEW])
    push.register(db_path, token_id, "fcm", "tok")
    alerts.upsert_rule(db_path, "PC-A", 31, "Hot", "91C")

    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=6000, enqueue=enqueue)
    check("while the owner holds view, the alert is pushed",
          [s["count"] for s in seen] == [1])

    # Demote: remove them from every group. The token still says `view`.
    gid = permissions.list_groups(db_path)[0]["id"]
    permissions.update_group(db_path, gid, members=[], actor="root@example.com")
    permissions.invalidate()
    push.acknowledge(db_path, token_id, now=6010)
    alerts.upsert_rule(db_path, "PC-B", 32, "Hot", "92C")

    push.scan(db_path, superusers=(), now=6020, enqueue=enqueue)
    check("after the owner is demoted, their phone is told nothing -- the token is a "
          "ceiling, not a grant", [s["count"] for s in seen] == [1])


def test_a_device_granted_less_than_its_owner_holds_gets_less():
    """The other direction of the same rule: a token minted WITHOUT `view` must not be
    pushed, however much its owner can see in the console."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW, permissions.ISSUE_COMMANDS])
    token_id = pair(db_path, "op@x.com", capabilities=[permissions.ISSUE_COMMANDS])
    push.register(db_path, token_id, "fcm", "tok")
    alerts.upsert_rule(db_path, "PC-A", 41, "Hot", "91C")

    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=7000, enqueue=enqueue)
    check("a device without `view` is never pushed to", seen == [])


def test_duplicate_serial_alerts_are_scoped_by_their_machine_list():
    """`duplicate_serial` leaves `machine` empty and names the collision in `machines`.
    Reading only the singular field would drop every one of them for a scoped operator and
    hand every one of them to an unrestricted one as an alert about "" -- wrong in both
    directions at once, and silently."""
    db_path = _db()
    group(db_path, "Ward", "ward@x.com", [permissions.VIEW], machines=["PC-WARD"])
    group(db_path, "Lab", "lab@x.com", [permissions.VIEW], machines=["PC-LAB"])
    ward = pair(db_path, "ward@x.com")
    lab = pair(db_path, "lab@x.com")
    push.register(db_path, ward, "fcm", "tok-ward")
    push.register(db_path, lab, "fcm", "tok-lab")

    alerts.upsert_duplicate(db_path, "SER-1", ["PC-WARD", "PC-SPARE"])
    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=8000, enqueue=enqueue)
    counts = {s["token_id"]: s["count"] for s in seen}
    check("a duplicate-serial alert reaches the operator who can see one of its machines",
          counts.get(ward) == 1)
    check("...and not the one who can see neither", lab not in counts)


# --------------------------------------------------------------------------------------
# The delta
# --------------------------------------------------------------------------------------
def test_a_standing_alert_notifies_once_not_every_scan():
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "tok")
    alerts.upsert_rule(db_path, "PC-HOT", 51, "Hot", "91C")

    seen, enqueue = collector()
    for tick in range(5):
        push.scan(db_path, superusers=(), now=9000 + tick * 10, enqueue=enqueue)
    check("one alert held open across five scans is one notification", len(seen) == 1)

    alerts.upsert_rule(db_path, "PC-HOT-2", 52, "Hot", "92C")
    push.scan(db_path, superusers=(), now=9100, enqueue=enqueue)
    check("a second alert raises the count and notifies again",
          [s["count"] for s in seen] == [1, 2])


def test_delivery_is_not_readership():
    """The cursor moves forward only when the app says the list was opened. Advancing it on
    delivery is how the fourth alert of the night arrives announcing "1"."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "tok")

    seen, enqueue = collector()
    alerts.upsert_rule(db_path, "PC-A", 61, "Hot", "91C")
    push.scan(db_path, superusers=(), now=10000, enqueue=enqueue)
    alerts.upsert_rule(db_path, "PC-B", 62, "Hot", "92C")
    push.scan(db_path, superusers=(), now=10010, enqueue=enqueue)
    alerts.upsert_rule(db_path, "PC-C", 63, "Hot", "93C")
    push.scan(db_path, superusers=(), now=10020, enqueue=enqueue)
    check("unread alerts accumulate rather than each arriving as 1",
          [s["count"] for s in seen] == [1, 2, 3])

    push.acknowledge(db_path, token_id, now=10030)
    alerts.upsert_rule(db_path, "PC-D", 64, "Hot", "94C")
    push.scan(db_path, superusers=(), now=10040, enqueue=enqueue)
    check("...and the count restarts once the app says the list was opened",
          [s["count"] for s in seen] == [1, 2, 3, 1])


def test_dismissing_the_newest_alert_does_not_replay_the_older_ones():
    """The cursor is a high-water mark over the WHOLE alerts table, not over the open ones.
    A cursor that followed max(open id) would go BACKWARDS when the newest alert was
    dismissed, and re-push everything under it."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    first = alerts.upsert_rule(db_path, "PC-A", 71, "Hot", "91C")
    newest = alerts.upsert_rule(db_path, "PC-B", 72, "Hot", "92C")
    push.register(db_path, token_id, "fcm", "tok")

    alerts.dismiss(db_path, newest)
    seen, enqueue = collector()
    push.scan(db_path, superusers=(), now=11000, enqueue=enqueue)
    check("dismissing the newest alert replays nothing older than it", seen == [])


def test_revoking_a_device_takes_its_cursor_with_it():
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "tok")
    apitokens.revoke_token(db_path, token_id, actor="root@example.com")

    seen, enqueue = collector()
    alerts.upsert_rule(db_path, "PC-A", 81, "Hot", "91C")
    push.scan(db_path, superusers=(), now=12000, enqueue=enqueue)
    check("a revoked device is never pushed to", seen == [])
    with fleet.get_conn(db_path) as conn:
        left = conn.execute("SELECT COUNT(*) AS c FROM push_cursors").fetchone()["c"]
    check("...and its cursor does not outlive the credential it describes", left == 0)


def test_an_expired_token_is_not_pushed_to():
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com", now=1000)
    push.register(db_path, token_id, "fcm", "tok", now=1000)
    alerts.upsert_rule(db_path, "PC-A", 91, "Hot", "91C")

    seen, enqueue = collector()
    # 91 days on -- past the 90-day default lifetime, with no use in between to slide it.
    push.scan(db_path, superusers=(), now=1000 + 91 * 86400, enqueue=enqueue)
    check("an expired device token stops being a push address", seen == [])


def test_registering_a_revoked_device_is_refused():
    db_path = _db()
    token_id = pair(db_path, "op@x.com")
    apitokens.revoke_token(db_path, token_id, actor="root@example.com")
    check("a revoked device cannot register for push",
          push.register(db_path, token_id, "fcm", "tok") is False)
    check("an unknown device cannot either",
          push.register(db_path, "no-such-token", "fcm", "tok") is False)


# --------------------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------------------
def test_the_payload_carries_a_count_and_no_content():
    """Content-free is a security property, not a style. A push transits Google's servers,
    and a helpdesk's machine names are the inventory of somebody's estate."""
    db_path = _db()
    group(db_path, "All", "op@x.com", [permissions.VIEW])
    token_id = pair(db_path, "op@x.com")
    push.register(db_path, token_id, "fcm", "tok")
    alerts.upsert_rule(db_path, "PC-SECRET-HOSTNAME", 99, "Overheating", "PC is at 97C")
    push.scan(db_path, superusers=(), now=13000,
              enqueue=lambda *a, **k: None)

    message = push.build_fcm_message("proj", "tok", 3)
    body = json.dumps(message)
    check("the message names no machine", "PC-SECRET-HOSTNAME" not in body)
    check("...no rule name", "Overheating" not in body)
    check("...and no rendered sentence, only a localization key",
          message["message"]["android"]["notification"]["body_loc_key"]
          == push.LOC_KEY_BODY)
    check("the count rides as a STRING, which is what the FCM v1 schema declares",
          message["message"]["android"]["notification"]["body_loc_args"] == ["3"])
    check("high priority, or Doze holds it until morning -- the delay this exists to remove",
          message["message"]["android"]["priority"] == "high")
    check("one replaceable notification rather than a stack saying the same thing",
          message["message"]["android"]["notification"]["tag"] == "fleethub-alerts")


def test_apns_is_a_refusal_with_a_reason():
    """Accepted at registration so the registry is right the day an iOS build exists;
    refused at send, because APNs is HTTP/2 and requests is not. A row that sat pending
    forever would be the worse answer."""
    db_path = _db()
    token_id = pair(db_path, "op@x.com")
    check("apns is a valid registration kind",
          push.register(db_path, token_id, "apns", "apns-token") is True)

    outbox_id = notify.enqueue_push(db_path, kind="apns", push_token="apns-token",
                                    token_id=token_id, count=2, now=14000)
    notify.send_due(db_path, now=14000)
    with fleet.get_conn(db_path) as conn:
        row = conn.execute("SELECT status, last_error FROM notify_outbox WHERE id = ?",
                           (outbox_id,)).fetchone()
    check("an apns push fails rather than sitting pending", row["status"] != "pending")
    check("...and the failure names the reason", "APNs is not implemented" in row["last_error"])


def test_push_is_off_until_it_is_configured():
    saved = os.environ.pop("FCM_SERVICE_ACCOUNT", None)
    try:
        check("an unconfigured hub reports push as unavailable rather than raising",
              push.is_configured() is False)
        os.environ["FCM_SERVICE_ACCOUNT"] = os.path.join(
            tempfile.gettempdir(), "definitely-not-here.json")
        check("a broken FCM_SERVICE_ACCOUNT is still a False, not an exception, because "
              "the console asks this to explain itself", push.is_configured() is False)
    finally:
        os.environ.pop("FCM_SERVICE_ACCOUNT", None)
        if saved is not None:
            os.environ["FCM_SERVICE_ACCOUNT"] = saved


def test_registration_validates_its_input():
    db_path = _db()
    token_id = pair(db_path, "op@x.com")
    for bad_kind in ("", "gcm", "webhook", None):
        try:
            push.register(db_path, token_id, bad_kind, "tok")
            check(f"push kind {bad_kind!r} is refused", False)
        except push.PushError:
            check(f"push kind {bad_kind!r} is refused", True)
    try:
        push.register(db_path, token_id, "fcm", "")
        check("an empty push token is refused", False)
    except push.PushError:
        check("an empty push token is refused", True)
    try:
        push.register(db_path, token_id, "fcm", "x" * (push.MAX_PUSH_TOKEN_CHARS + 1))
        check("an implausibly long push token is refused", False)
    except push.PushError:
        check("an implausibly long push token is refused", True)


def test_the_outbox_carries_what_the_transport_needs():
    db_path = _db()
    token_id = pair(db_path, "op@x.com")
    outbox_id = notify.enqueue_push(db_path, kind="fcm", push_token="tok",
                                    token_id=token_id, count=4, now=15000)
    with fleet.get_conn(db_path) as conn:
        row = conn.execute("SELECT kind, payload_json FROM notify_outbox WHERE id = ?",
                           (outbox_id,)).fetchone()
    payload = json.loads(row["payload_json"])
    check("a push is queued as its own outbox kind", row["kind"] == notify.KIND_PUSH)
    check("...carrying the device it is for, so a dead address can be cleared",
          payload["token_id"] == token_id)
    check("...and the count", payload["count"] == 4)


# --------------------------------------------------------------------------------------
def main():
    tests = [
        test_registration_primes_and_tells_nobody_about_the_backlog,
        test_reregistration_does_not_replay,
        test_unregistering_keeps_the_cursor,
        test_count_is_scoped_to_what_the_device_may_see,
        test_the_token_ceiling_is_intersected_live,
        test_a_device_granted_less_than_its_owner_holds_gets_less,
        test_duplicate_serial_alerts_are_scoped_by_their_machine_list,
        test_a_standing_alert_notifies_once_not_every_scan,
        test_delivery_is_not_readership,
        test_dismissing_the_newest_alert_does_not_replay_the_older_ones,
        test_revoking_a_device_takes_its_cursor_with_it,
        test_an_expired_token_is_not_pushed_to,
        test_registering_a_revoked_device_is_refused,
        test_the_payload_carries_a_count_and_no_content,
        test_apns_is_a_refusal_with_a_reason,
        test_push_is_off_until_it_is_configured,
        test_registration_validates_its_input,
        test_the_outbox_carries_what_the_transport_needs,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
