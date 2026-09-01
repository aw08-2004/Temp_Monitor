"""HTTP-layer tests for cross-hub machine sharing (roadmap #15).

**Two hubs, two databases, one process.** Hub A is the real `app.py`, because the things
under test are its `login_required` gate, its machine record and its command queue. Hub B is
a minimal Flask app carrying a second `sharing_web` blueprint over its own database, with a
`peer_call` that dispatches into hub A's test client instead of over the network. So every
peer request in this file really is one hub asking another -- serialised, authenticated by a
peer token, and decided by hub A against rows hub B cannot see.

What is worth stating about the assertions:

  * **A peer holds nothing except through a share.** Every peer route is exercised with a
    valid token and the WRONG share, and with a share that has been revoked, narrowed, or
    whose creator has been demoted. Each must refuse, and the lapsed one must refuse BY
    NAME -- a share that quietly does less is the failure mode this design rejects.
  * **The agent channel is untouched.** A peer command lands on hub A's own queue, issued by
    a `peer:` identity, and the agent endpoints are asserted to be unreachable with a peer
    token. There is no path from hub B to an agent, which is the whole security argument.
  * **What crosses the boundary is an allow-list.** The machine projection must not carry a
    serial, an asset tag or anything from Active Directory, and a peer must not be able to
    read a command a LOCAL operator issued on the same machine.
  * **A borrowing hub never enrolls anything.** Hub B's fleet tables stay empty through a
    whole borrow-and-use cycle.
  * **Revocation is immediate**, without a cache to invalidate, and takes an open remote
    session with it rather than merely refusing the next request.
"""
import functools
import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="hub-sharing-web-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
# Hub B keeps the peer token in the master-key-wrapped store, exactly as in production.
# Declared before app.py is imported so both hubs see the same configured key.
os.environ.setdefault("BACKUP_MASTER_KEY",
                      "c2hhcmluZy10ZXN0LW1hc3Rlci1rZXktMzJieXRlcyE=")
os.chdir(_TMPDIR)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet             # noqa: E402
import permissions       # noqa: E402
import remote            # noqa: E402
import settings          # noqa: E402
import sharing           # noqa: E402
import sharing_web       # noqa: E402
import app as hub        # noqa: E402
from flask import Flask, jsonify, session  # noqa: E402
from permissions_web import create_access, set_request_identity  # noqa: E402

PASS = 0
FAIL = 0

MACHINE = "WARD-PC-1"

PASSTHROUGH_PREFIX = "https://hub-a.example.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def sign_in(client, email):
    with client.session_transaction() as sess:
        sess["user"] = {"email": email, "name": email, "directory_groups": []}


def sign_out(client):
    with client.session_transaction() as sess:
        sess.clear()


# ================================
# HUB B -- a second, minimal hub
# ================================
def build_borrower(db_path, log_dir, hub_a_client):
    """A Flask app that is a whole second hub, as far as sharing is concerned.

    `peer_call` is the seam: it turns an outbound HTTPS request into a call on hub A's test
    client, so the two hubs really do exchange JSON over their public API and neither can
    see the other's tables.
    """
    fleet.init_fleet_db(db_path)
    permissions.init_permissions_db(db_path)
    sharing.init_sharing_db(db_path)
    settings.init_settings_db(db_path)
    os.makedirs(log_dir, exist_ok=True)

    app = Flask(__name__, template_folder=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "templates"))
    app.secret_key = "borrower-test"

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = session.get("user")
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            set_request_identity(user)
            return view(*args, **kwargs)
        return wrapped

    def peer_call(method, url, token=None, payload=None, timeout=None):
        if not url.startswith(PASSTHROUGH_PREFIX):
            return 502, {"error": f"unroutable in this test: {url}"}
        path = url[len(PASSTHROUGH_PREFIX):]
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = hub_a_client.open(path, method=method, json=payload or {},
                                     headers=headers)
        try:
            body = response.get_json()
        except Exception:
            body = None
        return response.status_code, (body if isinstance(body, dict) else {})

    access = create_access(db_path, {"borrower@x.com"})
    app.register_blueprint(sharing_web.create_sharing_blueprint(
        db_path, log_dir, login_required, access, peer_call=peer_call))
    return app


# ================================
# TESTS
# ================================
#: Both hubs' databases, so `set_setting` can keep them in step. See its docstring.
DBS = []


def set_setting(key, value):
    """Write one setting to BOTH hubs.

    settings.py (like permissions.py) caches its resolved state in a module global that is
    NOT keyed by database path -- correct for production, where one process is one hub, and
    unavoidable here, where one process is two. Writing the same value to both databases
    makes the shared cache harmless instead of load-bearing: whichever hub's read rebuilds
    it, the answer is the same. It is also why hub B's operator is on the break-glass list
    rather than in a permission group -- a superuser resolves before that cache is touched.
    """
    for db_path in DBS:
        settings.set_many(db_path, {key: value}, updated_by="test")


def test_the_page(a_client):
    print("\n== The page, and which third of it each operator gets ==")
    sign_in(a_client, "viewer@x.com")
    page = a_client.get("/sharing")
    body = page.get_data(as_text=True)
    check("a `view` operator gets the page", page.status_code == 200)
    check("...with both machine tabs",
          'id="tab-btn-lending"' in body and 'id="tab-btn-borrowing"' in body)
    # Presentation, not the gate -- every endpoint behind the tab is checked server-side
    # regardless. Asserted anyway, because a tab that 403s on every click is worse than no
    # tab, and the two halves are easy to change apart.
    check("...but NOT the paired-hubs tab, which is the grant perimeter",
          'id="tab-btn-hubs"' not in body)
    sign_out(a_client)

    sign_in(a_client, "super@x.com")
    body = a_client.get("/sharing").get_data(as_text=True)
    check("an admin gets the paired-hubs tab", 'id="tab-btn-hubs"' in body)
    sign_out(a_client)


def test_peer_api_is_not_the_console(a_client, db_path):
    print("\n== The peer API is a different door, with a different key ==")
    r = a_client.get("/api/peer/catalogue")
    check("a hub with sharing switched off refuses peer traffic outright",
          r.status_code == 403)
    check("...and says so, because the far operator cannot see this hub's settings",
          "switched off" in r.get_json()["error"])

    set_setting("sharing.enabled", True)
    r = a_client.get("/api/peer/catalogue")
    check("no credential at all -> 401", r.status_code == 401)
    r = a_client.get("/api/peer/catalogue", headers={"Authorization": "Bearer tmh_x:y"})
    check("a made-up peer token -> 401", r.status_code == 401)

    # A signed-in operator is not a peer. The peer routes read the header and nothing else.
    sign_in(a_client, "super@x.com")
    r = a_client.get("/api/peer/catalogue")
    check("a console session does not authenticate a peer route", r.status_code == 401)
    sign_out(a_client)


def pair_hubs(a_client, b_client, label="Bob's hub"):
    """Drive a whole pairing through both hubs' real endpoints. Returns the link id."""
    sign_in(a_client, "super@x.com")
    made = a_client.post("/api/sharing/pairings", json={"label": label})
    code = made.get_json()["code"]
    sign_out(a_client)

    sign_in(b_client, "borrower@x.com")
    linked = b_client.post("/api/sharing/links", json={
        "base_url": PASSTHROUGH_PREFIX, "label": "Alice's hub", "code": code})
    return made, linked


def test_pairing(a_client, b_client, a_db, b_db):
    print("\n== Pairing: a code from one console, pasted into another ==")
    set_setting("sharing.enabled", False)
    made, linked = pair_hubs(a_client, b_client)
    check("hub A hands out a code", made.status_code == 201 and made.get_json()["code"])
    check("...but a hub with sharing switched off will not pair, either direction",
          linked.status_code == 403)
    check("...and hub B stored no link for a pairing that failed",
          sharing.list_links(b_db) == [])

    set_setting("sharing.enabled", True)
    made, linked = pair_hubs(a_client, b_client)
    check("with sharing on, the pairing completes", linked.status_code == 201)
    link_id = linked.get_json()["link_id"]

    check("hub A now has one peer", len(sharing.list_peers(a_db)) == 1)
    check("hub B now has one link", len(sharing.list_links(b_db)) == 1)
    check("hub B labelled the link", sharing.get_link(b_db, link_id)["label"]
          == "Alice's hub")

    print("\n== ...and the token it minted is not in hub B's link table ==")
    with fleet.get_conn(b_db) as conn:
        row = dict(conn.execute("SELECT * FROM share_links WHERE link_id = ?",
                                (link_id,)).fetchone())
    check("no column holds a peer token",
          not any(str(v).startswith("tmh_") for v in row.values()))
    listing = b_client.get("/api/sharing/links").get_json()["links"]
    check("the console reports that the credential is READABLE, not what it is",
          listing[0]["has_token"] is True and "token" not in listing[0])

    print("\n== A pairing code is single-use, across hubs ==")
    again = b_client.post("/api/sharing/links", json={
        "base_url": PASSTHROUGH_PREFIX + "/other", "label": "Twice", "code":
        made.get_json()["code"]})
    check("re-redeeming the same code is refused", again.status_code == 400)
    return link_id


def test_sharing_a_machine(a_client, b_client, a_db, b_db, link_id):
    print("\n== Lending one machine ==")
    peer_id = sharing.list_peers(a_db)[0]["peer_id"]

    sign_in(a_client, "viewer@x.com")
    over = a_client.post("/api/sharing/shares", json={
        "peer_id": peer_id, "machine": MACHINE,
        "capabilities": [permissions.VIEW, permissions.REMOTE_CONTROL]})
    check("an operator cannot share a capability they do not hold",
          over.status_code == 403)
    check("...naming what was refused", "remote_control" in over.get_json()["error"])
    sign_out(a_client)

    sign_in(a_client, "tech@x.com")
    admin_cap = a_client.post("/api/sharing/shares", json={
        "peer_id": peer_id, "machine": MACHINE,
        "capabilities": [permissions.VIEW, permissions.MANAGE_SETTINGS]})
    check("an administrative capability cannot be shared at all",
          admin_cap.status_code == 400)
    check("...and the refusal explains why",
          "own configuration" in admin_cap.get_json()["error"])

    out_of_scope = a_client.post("/api/sharing/shares", json={
        "peer_id": peer_id, "machine": "SOMEBODY-ELSES-PC",
        "capabilities": [permissions.VIEW]})
    check("a machine out of the operator's scope cannot be shared",
          out_of_scope.status_code == 403)

    created = a_client.post("/api/sharing/shares", json={
        "peer_id": peer_id, "machine": MACHINE,
        "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS]})
    check("a share the operator can back is created", created.status_code == 201)
    share = created.get_json()
    check("...and reports what it is actually worth right now",
          share["granted"] == [permissions.VIEW, permissions.ISSUE_COMMANDS]
          and share["lapsed"] == [] and share["live"] is True)
    sign_out(a_client)
    return share["share_id"]


def test_borrowing(a_client, b_client, a_db, b_db, link_id, share_id):
    print("\n== The borrowing hub sees it, and never enrolls it ==")
    sign_in(b_client, "borrower@x.com")
    refreshed = b_client.post(f"/api/sharing/links/{link_id}/refresh", json={})
    check("hub B can read hub A's catalogue", refreshed.status_code == 200)
    machines = refreshed.get_json()["machines"]
    check("...and sees exactly the one machine", len(machines) == 1)
    check("...under its own hostname", machines[0]["hostname"] == MACHINE)
    check("...badged as borrowed", machines[0]["borrowed"] is True)

    with fleet.get_conn(b_db) as conn:
        enrolled = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
        commands = conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()["n"]
    check("hub B enrolled no agent", enrolled == 0)
    check("...and queued no command of its own", commands == 0)

    listed = b_client.get("/api/sharing/borrowed").get_json()["machines"]
    check("the borrowed list names which hub it came from",
          listed and listed[0]["peer_label"] == "Alice's hub")

    print("\n== Telemetry is PROXIED, and projected on the way out ==")
    detail = b_client.get(f"/api/sharing/borrowed/{link_id}/{share_id}/machine")
    check("hub B can read the machine", detail.status_code == 200)
    body = detail.get_json()
    check("...getting its identity and health", body["machine"] == MACHINE
          and body["model"] == "OptiPlex 7090")
    for leaked in ("serial_number", "asset_tag", "service_tag", "ad_ou", "ad_dn",
                   "ad_owner", "enrolled"):
        check(f"...and NOT {leaked}", leaked not in body)

    print("\n== A command lands on hub A's own queue ==")
    issued = b_client.post(f"/api/sharing/borrowed/{link_id}/{share_id}/commands",
                           json={"type": "gpupdate", "params": {}})
    check("hub B can ask for a command it was lent", issued.status_code == 201)
    command_id = issued.get_json()["command_id"]
    queued = fleet.get_command(a_db, command_id)
    check("...and the command exists on HUB A", queued is not None)
    check("...aimed at hub A's own machine", queued["machine"] == MACHINE)
    check("...recorded as a peer's, not an operator's",
          queued["issued_by"].startswith("peer:"))
    check("...naming the borrowing operator only as a CLAIM",
          queued["issued_by"].endswith("/borrower@x.com"))
    with fleet.get_conn(b_db) as conn:
        check("hub B still has no command rows of its own",
              conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()["n"] == 0)

    status = b_client.get(
        f"/api/sharing/borrowed/{link_id}/{share_id}/commands/{command_id}")
    check("hub B can follow it up", status.status_code == 200
          and status.get_json()["id"] == command_id)

    print("\n== A capability that was not shared does not work ==")
    refused = b_client.post(f"/api/sharing/borrowed/{link_id}/{share_id}/commands",
                            json={"type": "update_bios", "params": {"update_id": "x"}})
    check("a command type outside the peer allow-list is refused",
          refused.status_code == 400)
    check("...naming what the share can run",
          "gpupdate" in refused.get_json()["error"])
    return command_id


def test_peer_cannot_reach_past_its_share(a_client, a_db, b_db, share_id):
    print("\n== A peer token reaches exactly one share, and nothing else ==")
    # Speak to hub A directly with the peer's own token, as a hostile borrower would.
    token = TOKENS["peer"]
    head = {"Authorization": f"Bearer {token}"}

    r = a_client.get("/api/peer/shares/deadbeef/machine", headers=head)
    check("an invented share id -> 403", r.status_code == 403)
    check("...saying nothing about what exists",
          r.get_json()["error"] == "No such share.")

    r = a_client.get("/api/agent/commands", headers=head)
    check("a peer token does not authenticate an AGENT endpoint",
          r.status_code in (401, 403))
    r = a_client.get("/api/machines", headers=head)
    check("...nor a console one", r.status_code == 401)

    print("\n== ...and cannot read a command a LOCAL operator issued ==")
    local_id = fleet.create_command(a_db, machine=MACHINE, command_type="gpupdate",
                                    params={}, issued_by="tech@x.com")
    r = a_client.get(f"/api/peer/shares/{share_id}/commands/{local_id}", headers=head)
    check("a local operator's command on the same machine is a plain miss",
          r.status_code == 404)


def test_remote_across_hubs(a_client, b_client, a_db, b_db, link_id, share_id):
    print("\n== Remote control, signalled through two hubs ==")
    # remote_control was not in the original share -- the point of the feature is that it
    # must be granted deliberately, so grant it deliberately here.
    sign_in(a_client, "tech@x.com")
    widened = a_client.put(f"/api/sharing/shares/{share_id}", json={
        "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS,
                         permissions.REMOTE_CONTROL]})
    check("the share can be widened to carry remote control", widened.status_code == 200)
    sign_out(a_client)
    set_setting("remote.enabled", True)

    head = {"Authorization": f"Bearer {TOKENS['peer']}"}
    sign_in(b_client, "borrower@x.com")
    b_client.post(f"/api/sharing/links/{link_id}/refresh", json={})

    started = b_client.post(f"/api/sharing/borrowed/{link_id}/{share_id}/remote",
                            json={"codec": "h264"})
    check("hub B can start a session on a borrowed machine", started.status_code == 201)
    session_id = started.get_json().get("session_id")
    check("...and is handed ICE servers for it",
          "ice_servers" in (started.get_json() or {}))

    sess = remote.get_session(a_db, session_id)
    check("the session lives on HUB A", sess is not None)
    check("...on hub A's own machine", sess and sess["machine"] == MACHINE)
    check("...owned by the peer, not by a local operator",
          sess and sess["issued_by"].startswith("peer:"))
    queued = [c for c in fleet.list_commands(a_db, machine=MACHINE)
              if c["type"] == "start_remote_session"]
    check("...and the agent was told from hub A's own queue", len(queued) == 1)
    check("...by the same peer identity", queued[0]["issued_by"] == sess["issued_by"])

    print("\n== The relay carries signals both ways ==")
    signalled = b_client.post(
        f"/api/sharing/borrowed/{link_id}/{share_id}/remote/{session_id}/signal",
        json={"kind": "answer", "payload": {"sdp": "v=0"}})
    check("hub B's answer reaches hub A", signalled.status_code == 200)
    agent_side = remote.get_signals(a_db, session_id, remote.SENDER_AGENT, 0)
    check("...and is queued for the AGENT to collect",
          any(s["kind"] == "answer" for s in agent_side.get("signals", [])))

    remote.add_signal(a_db, session_id, remote.SENDER_AGENT, "offer", {"sdp": "v=0"})
    polled = b_client.get(
        f"/api/sharing/borrowed/{link_id}/{share_id}/remote/{session_id}/poll?after_seq=0")
    check("the agent's offer comes back to hub B", polled.status_code == 200)
    check("...through two hops, unchanged",
          any(s["kind"] == "offer" for s in polled.get_json().get("signals", [])))

    print("\n== A peer reaches only the sessions it started ==")
    local_session = remote.create_session(a_db, MACHINE, "tech@x.com", "unattended")
    r = a_client.get(f"/api/peer/shares/{share_id}/remote/{local_session}/poll",
                     headers=head)
    check("a LOCAL operator's session on the same machine is a plain miss",
          r.status_code == 404)
    r = a_client.post(f"/api/peer/shares/{share_id}/remote/{local_session}/stop",
                      headers=head)
    check("...and cannot be stopped by the peer either", r.status_code == 404)
    check("...so it is still running",
          remote.get_session(a_db, local_session)["status"] != remote.STATUS_ENDED)

    print("\n== Virtual display is not on the peer plane at all ==")
    r = a_client.post(f"/api/peer/shares/{share_id}/virtual-display", headers=head,
                      json={"mode": "install"})
    check("there is no peer route for installing a display driver",
          r.status_code == 404)

    stopped = b_client.post(
        f"/api/sharing/borrowed/{link_id}/{share_id}/remote/{session_id}/stop", json={})
    check("hub B can end its own session", stopped.status_code == 200)
    check("...and hub A records it ended",
          remote.get_session(a_db, session_id)["status"] == remote.STATUS_ENDED)
    remote.end_session(a_db, local_session, "test cleanup", actor="test")

    print("\n== Narrowing the share ends what it was carrying ==")
    live_session = remote.create_session(a_db, MACHINE, f"peer:{PEER_IDS['peer']}",
                                         "unattended")
    sign_in(a_client, "tech@x.com")
    narrowed = a_client.put(f"/api/sharing/shares/{share_id}", json={
        "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS]})
    sign_out(a_client)
    check("the share can be narrowed back", narrowed.status_code == 200)
    check("...and the open screen closed with it, not at the next request",
          remote.get_session(a_db, live_session)["status"] == remote.STATUS_ENDED)
    r = a_client.post(f"/api/peer/shares/{share_id}/remote", headers=head, json={})
    check("a new session on the narrowed share is refused", r.status_code == 403)
    check("...naming the capability that is missing",
          "remote_control" in r.get_json()["error"])
    sign_in(b_client, "borrower@x.com")


def test_lapse_and_revocation(a_client, b_client, a_db, b_db, link_id, share_id):
    print("\n== Demoting the operator who lent it suspends the share, out loud ==")
    permissions.update_group(a_db, GROUPS["techs"],
                             capabilities=[permissions.VIEW], actor="test")
    permissions.invalidate()

    head = {"Authorization": f"Bearer {TOKENS['peer']}"}
    r = a_client.post(f"/api/peer/shares/{share_id}/commands", headers=head,
                      json={"type": "gpupdate", "params": {}})
    check("the lapsed capability is refused", r.status_code == 403)
    check("...NAMING it, rather than the share quietly doing less",
          "no longer holds" in r.get_json()["error"]
          and "issue_commands" in r.get_json()["error"])
    r = a_client.get(f"/api/peer/shares/{share_id}/machine", headers=head)
    check("...while the capability that did NOT lapse still works",
          r.status_code == 200)

    sign_in(b_client, "borrower@x.com")
    b_client.post(f"/api/sharing/links/{link_id}/refresh", json={})
    borrowed = sharing.get_borrowed(b_db, link_id, share_id)
    check("hub B's catalogue shows the machine as lapsed rather than losing it",
          borrowed is not None and borrowed["lapsed"] is True)
    check("...carrying only what survived",
          borrowed["capabilities"] == [permissions.VIEW])

    permissions.update_group(
        a_db, GROUPS["techs"],
        capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                      permissions.REMOTE_CONTROL], actor="test")
    permissions.invalidate()
    r = a_client.post(f"/api/peer/shares/{share_id}/commands", headers=head,
                      json={"type": "gpupdate", "params": {}})
    check("restoring the operator restores the share", r.status_code == 201)

    print("\n== Revoking kills a session in flight, not just the next request ==")
    session_id = remote.create_session(a_db, MACHINE, "peer:test", "unattended")
    sign_in(a_client, "tech@x.com")
    revoked = a_client.delete(f"/api/sharing/shares/{share_id}")
    sign_out(a_client)
    check("the share is revoked", revoked.status_code == 200)
    check("...and the open remote session was ended, not left running",
          remote.get_session(a_db, session_id)["status"] == remote.STATUS_ENDED)

    r = a_client.get(f"/api/peer/shares/{share_id}/machine", headers=head)
    check("the very next peer request is refused, with no cache to invalidate",
          r.status_code == 403)

    sign_in(b_client, "borrower@x.com")
    b_client.post(f"/api/sharing/links/{link_id}/refresh", json={})
    check("hub B's borrowed list empties on its next read",
          sharing.list_borrowed(b_db, link_id) == [])
    entries = fleet.list_audit(b_db, limit=100)["entries"]
    check("...and hub B recorded WHY the machine disappeared",
          any(e["action"] == "share.catalogue_change"
              and share_id in (e["detail"] or {}).get("removed", [])
              for e in entries))

    print("\n== Unpairing the hub takes everything with it ==")
    peer_id = sharing.list_peers(a_db)[0]["peer_id"]
    sign_in(a_client, "super@x.com")
    unpaired = a_client.delete(f"/api/sharing/peers/{peer_id}")
    sign_out(a_client)
    check("the peer is unpaired", unpaired.status_code == 200)
    r = a_client.get("/api/peer/catalogue", headers=head)
    check("...and its token is dead", r.status_code == 401)

    sign_in(b_client, "borrower@x.com")
    stale = b_client.post(f"/api/sharing/links/{link_id}/refresh", json={})
    check("hub B's next poll fails, rather than showing stale machines as live",
          stale.status_code == 502)
    link = sharing.get_link(b_db, link_id)
    check("...and the link records the reason for an operator to read",
          bool(link["last_error"]))


GROUPS = {}
TOKENS = {}
PEER_IDS = {}


def main():
    a_db = hub.DB_PATH
    b_db = os.path.join(_TMPDIR, "hub-b.db")
    b_log = os.path.join(_TMPDIR, "hub-b-logs")

    sharing.init_sharing_db(a_db)

    # Hub A's fleet: one machine with a full record, so machine_detail has something to
    # project and the exclusions are actually being excluded rather than merely absent.
    with hub.get_db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO machine_info(machine, asset_tag, serial_number, "
            "service_tag, manufacturer, model, os_caption, os_build, updated_at, ad_ou, "
            "ad_dn, ad_owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (MACHINE, "AT-4412", "SN-99887", "SVC-1234", "Dell", "OptiPlex 7090",
             "Windows 11 Pro", "22631", int(__import__("time").time()),
             "OU=Ward,DC=x", "CN=WARD-PC-1,OU=Ward,DC=x", "ward.nurse@x.com"))

    GROUPS["techs"] = permissions.create_group(
        a_db, "Techs",
        capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                      permissions.REMOTE_CONTROL],
        machines=[MACHINE], members=["tech@x.com"])
    permissions.create_group(
        a_db, "Viewers", capabilities=[permissions.VIEW],
        machines=[MACHINE], members=["viewer@x.com"])
    permissions.invalidate()
    hub.ALLOWED_EMAILS.add("super@x.com")

    a_client = hub.app.test_client()
    b_app = build_borrower(b_db, b_log, a_client)
    DBS.extend([a_db, b_db])
    b_client = b_app.test_client()

    test_the_page(a_client)
    test_peer_api_is_not_the_console(a_client, a_db)
    link_id = test_pairing(a_client, b_client, a_db, b_db)
    share_id = test_sharing_a_machine(a_client, b_client, a_db, b_db, link_id)

    # The peer's own token, read back out of hub B's store, so the tests below can speak to
    # hub A the way a hostile borrower would.
    import backups
    TOKENS["peer"] = backups.load_secret(
        b_log, backups.load_master_key(),
        sharing_web.secret_id_for(link_id))["token"]
    PEER_IDS["peer"] = sharing.list_peers(a_db)[0]["peer_id"]

    test_borrowing(a_client, b_client, a_db, b_db, link_id, share_id)
    test_peer_cannot_reach_past_its_share(a_client, a_db, b_db, share_id)
    test_remote_across_hubs(a_client, b_client, a_db, b_db, link_id, share_id)
    test_lapse_and_revocation(a_client, b_client, a_db, b_db, link_id, share_id)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
