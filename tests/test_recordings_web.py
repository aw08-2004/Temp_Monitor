"""Session recordings over HTTP (recordings_web.py + its hooks in remote_web.py, roadmap #19).

The silent failures this file exists to catch:

  * **Recording a PC you may not remote into.** Making a recording is remote_control + the PC
    in scope; a viewer, or a technician scoped elsewhere, must be refused -- and so must an
    operator who did not open the session, whose browser holds no picture of it.
  * **A badge request the PC never hears.** Starting must put a `record` signal on the
    session's channel, where the helper polls; and only the recording's own agent may answer.
  * **Video accepted before the badge, or after the session.** The hub decides, not the
    browser: a chunk is refused until the PC confirmed its badge and once the session is gone.
  * **A recording that survives the helper that showed its badge.** A new helper's offer is a
    helper with no badge up, so it must end the recording.
  * **Re-sharing through the API.** Only the owner may change shares; anybody else gets the
    same 404 as for a recording that does not exist.
  * **A download nobody can account for.** Downloads are audited; the video is only served to
    the owner or a share.
  * **An end reason the console cannot name.** Every recordings.END_REASONS code must be a
    literal key in both scripts that show one, and in every catalog.
"""
import functools
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
import fleet
import permissions
import recordings
import remote
import settings
from flask import Flask, session as flask_session
from permissions_web import create_access
from recordings_web import create_recordings_blueprint
from remote_web import create_remote_blueprint

PASS = 0
FAIL = 0
CURRENT_USER = "tech@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def as_user(email):
    global CURRENT_USER
    CURRENT_USER = email


def build():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fleet.init_fleet_db(db)
    remote.init_remote_db(db)
    permissions.init_permissions_db(db)
    settings.init_settings_db(db)
    settings.invalidate()
    settings.set_many(db, {"remote.enabled": True})
    settings.invalidate()
    root = tempfile.mkdtemp(prefix="recordings-web-")

    permissions.create_group(db, "Techs", capabilities=[permissions.VIEW,
                                                        permissions.REMOTE_CONTROL],
                             machines=["PC-01"], members=["tech@x.com", "tech2@x.com"])
    permissions.create_group(db, "Elsewhere", capabilities=[permissions.VIEW,
                                                            permissions.REMOTE_CONTROL],
                             machines=["PC-09"], members=["far@x.com"])
    leads = permissions.create_group(db, "Leads", capabilities=[permissions.VIEW],
                                     machines=["PC-01"], members=["lead@x.com"])
    permissions.create_group(db, "Viewers", capabilities=[permissions.VIEW],
                             machines=["PC-01"], members=["viewer@x.com"])

    app = Flask(__name__)
    app.secret_key = "test"
    access = create_access(db, set())
    app.register_blueprint(create_remote_blueprint(db, fake_login_required, access))
    app.register_blueprint(create_recordings_blueprint(db, fake_login_required, access, root))

    @app.before_request
    def _seed_session():
        flask_session["user"] = {"email": CURRENT_USER}

    secret = "enroll"
    agent_id, token = fleet.enroll_agent(db, "PC-01", secret, secret)
    other_id, other_token = fleet.enroll_agent(db, "PC-09", secret, secret)
    return (db, root, app.test_client(), leads,
            {"Authorization": f"Bearer {agent_id}:{token}"},
            {"Authorization": f"Bearer {other_id}:{other_token}"})


def open_session(c, auth):
    sid = c.post("/api/remote/PC-01/start", json={}).get_json()["session_id"]
    c.post(f"/api/agent/remote/{sid}/signal", json={"kind": "offer", "payload": {"sdp": "x"}},
           headers=auth)
    return sid


def start(c, sid, reason="Ticket 12: printer driver", machine="PC-01"):
    return c.post(f"/api/remote/{machine}/recordings",
                  json={"session_id": sid, "reason": reason, "mime": "video/webm;codecs=vp9"})


def ack(c, sid, rid, auth, badge="shown"):
    return c.post(f"/api/agent/remote/{sid}/signal",
                  json={"kind": "recording", "payload": {"recording_id": rid, "badge": badge}},
                  headers=auth)


def video(c, rid, query="", **kwargs):
    """GET a recording's video and close the response -- an unclosed one holds the file open,
    and Windows will not delete an open file."""
    r = c.get(f"/api/recordings/{rid}/video{query}", **kwargs)
    r.get_data()
    r.close()
    return r


def chunk(c, rid, seq, data):
    return c.put(f"/api/remote/recordings/{rid}/chunks/{seq}", data=data,
                 content_type="application/octet-stream")


def test_start_gates(c, auth):
    print("\n-- who may start a recording --")
    as_user("tech@x.com")
    sid = open_session(c, auth)
    as_user("viewer@x.com")
    check("a viewer without remote_control is refused", start(c, sid).status_code == 403)
    as_user("far@x.com")
    check("a technician scoped to other PCs is refused", start(c, sid).status_code == 403)
    as_user("tech2@x.com")
    check("another technician cannot record a session they did not open",
          start(c, sid).status_code == 403)
    as_user("tech@x.com")
    check("a reason is required", start(c, sid, reason="").status_code == 400)
    check("the operator who opened the session may record it",
          start(c, sid).status_code == 201)
    return sid


def test_badge_round_trip(db, c, auth, other_auth, sid):
    print("\n-- the PC's badge decides --")
    rec = recordings.live_for_session(db, sid)[0]
    rid = rec["id"]
    polled = c.get(f"/api/agent/remote/{sid}/poll?after_seq=0", headers=auth).get_json()
    record = [s for s in polled["signals"] if s["kind"] == "record"]
    check("starting puts a `record` signal on the session for the helper",
          record and record[0]["payload"] == {"recording_id": rid, "on": True})
    check("a chunk before the badge is refused", chunk(c, rid, 0, b"x").status_code == 409)
    r = c.post(f"/api/agent/remote/{sid}/signal",
               json={"kind": "recording", "payload": {"recording_id": rid, "badge": "shown"}},
               headers=other_auth)
    check("another PC's agent cannot confirm it",
          r.status_code == 404 and recordings.get(db, rid)["status"] == recordings.STATUS_STARTING)
    check("the PC's own agent confirms it", ack(c, sid, rid, auth).status_code == 200
          and recordings.get(db, rid)["status"] == recordings.STATUS_RECORDING)
    status = c.get(f"/api/remote/recordings/{rid}").get_json()
    check("the browser sees `recording`, a deadline and the hub's clock",
          status["status"] == "recording" and status["deadline"] and status["server_time"])
    r = chunk(c, rid, 0, b"WEBM")
    check("a chunk after the badge is stored", r.status_code == 200
          and r.get_json()["next_seq"] == 1)
    as_user("tech2@x.com")
    check("nobody else can add to it", chunk(c, rid, 1, b"x").status_code == 404)
    as_user("tech@x.com")
    return rid


def test_endings(db, root, c, auth):
    print("\n-- what ends a recording --")
    sid = open_session(c, auth)
    rid = start(c, sid).get_json()["id"]
    ack(c, sid, rid, auth)
    c.post(f"/api/agent/remote/{sid}/signal", json={"kind": "offer", "payload": {"sdp": "y"}},
           headers=auth)
    got = recordings.get(db, rid)
    check("a new helper's offer ends the recording (its badge went with the old helper)",
          got["status"] == recordings.STATUS_ENDED
          and got["end_reason"] == recordings.END_HELPER_RESTARTED)
    check("...and a chunk after that is refused with the reason",
          chunk(c, rid, 0, b"x").get_json()["recording"]["end_reason"]
          == recordings.END_HELPER_RESTARTED)

    rid2 = start(c, sid).get_json()["id"]
    ack(c, sid, rid2, auth)
    chunk(c, rid2, 0, b"AB")
    c.post(f"/api/remote/session/{sid}/stop", json={})
    got = recordings.get(db, rid2)
    check("stopping the session ends its recording",
          got["status"] == recordings.STATUS_ENDED
          and got["end_reason"] == recordings.END_SESSION_ENDED)

    sid3 = open_session(c, auth)
    rid3 = start(c, sid3).get_json()["id"]
    ack(c, sid3, rid3, auth)
    chunk(c, rid3, 0, b"VIDEO")
    r = c.post(f"/api/remote/recordings/{rid3}/stop", json={"reason": "made-up"})
    check("the browser stopping it records `stopped` (not a reason it invented)",
          r.status_code == 200 and r.get_json()["end_reason"] == recordings.END_STOPPED)
    signals = c.get(f"/api/agent/remote/{sid3}/poll?after_seq=0", headers=auth).get_json()
    off = [s for s in signals["signals"] if s["kind"] == "record" and not s["payload"]["on"]]
    check("...and asks the PC to take the badge down", len(off) == 1)
    c.post(f"/api/remote/recordings/{rid3}/stop", json={})
    signals = c.get(f"/api/agent/remote/{sid3}/poll?after_seq=0", headers=auth).get_json()
    off = [s for s in signals["signals"] if s["kind"] == "record" and not s["payload"]["on"]]
    check("...once, however often it is stopped", len(off) == 1)
    return rid3


def test_library(db, c, leads, rid):
    print("\n-- watching, sharing, downloading --")
    as_user("bob@x.com")
    check("a stranger does not see it in their list",
          c.get("/api/recordings").get_json()["shared"] == [])
    check("...and cannot fetch the video", video(c, rid).status_code == 404)
    check("...or share it (no oracle: 404)",
          c.put(f"/api/recordings/{rid}/shares", json={"users": ["bob@x.com"]}).status_code == 404)
    as_user("tech@x.com")
    mine = c.get("/api/recordings").get_json()
    check("the owner lists it with its size", any(r["id"] == rid and r["size_bytes"] == 5
                                                   for r in mine["owned"]))
    check("...and the usage total", mine["usage"]["bytes"] >= 5)
    check("group names are offered for the share editor",
          any(g["id"] == leads for g in mine["groups"]))
    r = c.put(f"/api/recordings/{rid}/shares", json={"groups": [leads], "users": ["bob@x.com"]})
    check("the owner shares it", r.status_code == 200
          and r.get_json()["shares"] == {"groups": [leads], "users": ["bob@x.com"]})

    as_user("lead@x.com")
    shared = c.get("/api/recordings").get_json()["shared"]
    check("a member of the group sees it as shared", [r["id"] for r in shared] == [rid])
    check("...without the share list (only the owner manages it)", "shares" not in shared[0])
    r = video(c, rid, headers={"Range": "bytes=0-1"})
    check("...can play it, ranged so the player can seek",
          r.status_code == 206 and r.data == b"VI")
    r = c.put(f"/api/recordings/{rid}/shares", json={"users": ["lead@x.com", "eve@x.com"]})
    check("...but cannot re-share it", r.status_code == 404)
    before = len(fleet.list_audit(db, action="recording_download")["entries"])
    r = video(c, rid, "?download=1")
    check("a download is served as an attachment",
          r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", ""))
    after = fleet.list_audit(db, action="recording_download")["entries"]
    check("...and audited with who downloaded whose recording",
          len(after) == before + 1 and after[0]["actor"] == "lead@x.com"
          and after[0]["detail"]["owner"] == "tech@x.com")

    permissions.update_group(db, leads, members=[])
    check("leaving the group takes the access with it",
          video(c, rid).status_code == 404)

    as_user("bob@x.com")
    check("a person it is shared with by name can watch",
          video(c, rid).status_code == 200)
    check("...but not delete it", c.delete(f"/api/recordings/{rid}").status_code == 404)
    as_user("tech@x.com")
    check("the owner deletes it", c.delete(f"/api/recordings/{rid}").status_code == 200
          and recordings.get(db, rid) is None)


def test_end_reasons_are_named():
    print("\n-- every end reason has words in the console --")
    scripts = {name: open(os.path.join(ROOT, "hub", "static", "js", name), encoding="utf-8").read()
               for name in ("remote-recorder.js", "recordings.js")}
    for code in recordings.END_REASONS:
        key = f"recordings.end_reason.{code}"
        check(f"{code} is a literal key in both scripts",
              all(f"t('{key}')" in js for js in scripts.values()))
        for lang in ("en", "de", "es"):
            with open(os.path.join(ROOT, "hub", "locales", f"{lang}.json"), encoding="utf-8") as f:
                catalog = json.load(f)
            check(f"...and in {lang}.json", code in catalog["recordings"]["end_reason"])
    # The keys the console builds from a server code are listed once each; a code the scripts
    # know that the hub does not is a stale one.
    known = set(re.findall(r"recordings\.end_reason\.([a-z_]+)'", scripts["recordings.js"]))
    check("the scripts know no reason the hub cannot send", known == set(recordings.END_REASONS))


def main():
    db, root, c, leads, auth, other_auth = build()
    sid = test_start_gates(c, auth)
    rid = test_badge_round_trip(db, c, auth, other_auth, sid)
    c.post(f"/api/remote/recordings/{rid}/stop", json={})
    rid = test_endings(db, root, c, auth)
    test_library(db, c, leads, rid)
    test_end_reasons_are_named()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
