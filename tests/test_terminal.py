"""Interactive terminal (ConPTY) -- terminal.py's rules plus the HTTP surface in fleet_web.

Wires the fleet blueprint into a minimal Flask app, same approach as test_fleet_web /
test_remote_web, so the console and agent endpoints are exercised against each other rather
than mocked apart. The whole feature is one duplex stream with two very different auth
stories on its two ends, and that seam is where the interesting failures live.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import settings
import terminal
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read_static(*parts):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "static", *parts)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        terminal.init_pty_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-01"], members=["tech@x.com", "other@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["viewer@x.com"])
        settings.invalidate()

        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        other_id, other_token = fleet.enroll_agent(db_path, "PC-09", SECRET, SECRET)
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}
        other_auth = {"Authorization": f"Bearer {other_id}:{other_token}"}

        app.register_blueprint(
            create_fleet_blueprint(db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        print("\n== Opening a session queues the shell_open the agent needs ==")
        r = c.post("/api/fleet/pty", json={"machine": "PC-01", "shell": "cmd",
                                           "cols": 100, "rows": 40})
        check("open -> 201", r.status_code == 201)
        body = r.get_json()
        sid = body["session_id"]
        check("open returns a session id and the normalized shell",
              bool(sid) and body["shell"] == "cmd" and body["cols"] == 100)
        claimed = fleet.claim_commands(db_path, agent_id, "PC-01")
        check("a shell_open command was queued carrying the session id",
              any(cmd["type"] == "shell_open" and cmd["params"]["session_id"] == sid
                  for cmd in claimed))
        check("the command carries the size, so the pty starts the right shape",
              any(cmd["params"]["cols"] == 100 and cmd["params"]["rows"] == 40
                  for cmd in claimed if cmd["type"] == "shell_open"))
        check("session id is unguessable, not a sequential handle", len(sid) >= 32)

        print("\n== shell_open cannot be forged through the ordinary command channel ==")
        # It names a session row; a hand-rolled one would point at a session that doesn't
        # exist, or at somebody else's.
        r = c.post("/api/fleet/commands",
                   json={"machine": "PC-01", "type": "shell_open",
                         "params": {"session_id": sid}})
        check("hand-issued shell_open -> 400", r.status_code == 400)

        print("\n== Keystrokes reach the agent BYTE FOR BYTE ==")
        # The entire point of the feature. A bare Enter is a lone '\r' and must survive the
        # round trip; so must Ctrl-C (0x03) and an arrow key's escape sequence. Anything
        # that trims, appends a newline, or normalises control characters breaks answering
        # an interactive prompt -- which is the bug this feature exists to fix.
        for payload in ["\r", "\x03", "\x1b[A", "1", "  spaced  ", "héllo"]:
            r = c.post(f"/api/fleet/pty/{sid}/input", json={"data": payload})
            check(f"console posts {payload!r} -> 200", r.status_code == 200)
        r = c.get(f"/api/agent/pty/{sid}/input?after_seq=-1", headers=auth)
        items = r.get_json()["items"]
        got = [i["data"] for i in items if i["kind"] == "data"]
        check("agent receives every keystroke verbatim, in order",
              got == ["\r", "\x03", "\x1b[A", "1", "  spaced  ", "héllo"])
        check("a bare Enter is delivered as exactly one CR and nothing else",
              got[0] == "\r")

        print("\n== The agent's cursor acks; nothing is replayed ==")
        cursor = r.get_json()["next_seq"] - 1
        r = c.get(f"/api/agent/pty/{sid}/input?after_seq={cursor}", headers=auth)
        check("second poll returns nothing new", r.get_json()["items"] == [])
        check("and is not 'closing'", r.get_json()["closing"] is False)

        print("\n== Input keeps flowing after the queue has drained ==")
        # Regression guard. The agent's ack DELETES the rows it consumed, so a sequence
        # derived from MAX(seq) in pty_input restarts at 0 the moment the queue empties --
        # below the agent's cursor, so every later keystroke is filtered out as "already
        # delivered" and the terminal goes silently deaf a second after you start typing.
        # The counter therefore lives on the session row.
        r = c.post(f"/api/fleet/pty/{sid}/input", json={"data": "after-drain"})
        check("post after a full drain -> 200", r.status_code == 200)
        r = c.get(f"/api/agent/pty/{sid}/input?after_seq={cursor}", headers=auth)
        drained = [i["data"] for i in r.get_json()["items"] if i["kind"] == "data"]
        check("the agent still receives it (seq did not rewind)", drained == ["after-drain"])
        cursor = r.get_json()["next_seq"] - 1
        c.get(f"/api/agent/pty/{sid}/input?after_seq={cursor}", headers=auth)   # ack it

        print("\n== Resize rides the same channel ==")
        r = c.post(f"/api/fleet/pty/{sid}/input", json={"size": {"cols": 137, "rows": 41}})
        check("resize -> 200", r.status_code == 200)
        r = c.get(f"/api/agent/pty/{sid}/input?after_seq={cursor}", headers=auth)
        items = r.get_json()["items"]
        check("agent receives the resize with the new size",
              len(items) == 1 and items[0]["kind"] == "resize"
              and items[0]["size"] == {"cols": 137, "rows": 41})
        check("and the session remembers it",
              terminal.get_session(db_path, sid)["cols"] == 137)
        check("a silly size is clamped rather than passed to CreatePseudoConsole",
              terminal.push_input(db_path, sid, "resize", {"cols": 99999, "rows": 0}) is not None
              and terminal.get_session(db_path, sid)["cols"] == 500
              and terminal.get_session(db_path, sid)["rows"] == 5)

        print("\n== VT output flows back, escape sequences intact ==")
        vt = "\x1b[2J\x1b[HC:\\Windows\\System32>"
        r = c.post(f"/api/agent/pty/{sid}/output", json={"seq": 0, "chunk": vt}, headers=auth)
        check("agent posts output -> 200", r.status_code == 200)
        check("posting output marks the session live",
              terminal.get_session(db_path, sid)["status"] == terminal.STATUS_LIVE)
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq=-1")
        out = r.get_json()
        check("console receives the VT stream unmodified",
              len(out["chunks"]) == 1 and out["chunks"][0]["text"] == vt)
        check("console is told nothing was lost", out["lost"] is False)

        print("\n== A retried POST reuses its seq and is a free no-op ==")
        # Allocating a fresh seq on retry would splice duplicate bytes into the middle of an
        # escape sequence and corrupt the operator's screen.
        c.post(f"/api/agent/pty/{sid}/output", json={"seq": 0, "chunk": vt}, headers=auth)
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq=-1")
        check("the duplicate did not double the stream", len(r.get_json()["chunks"]) == 1)

        print("\n== The rolling window drops old bytes and SAYS so ==")
        # Enough bytes to push the earliest chunks out of the replay budget.
        filler = "x" * 2000
        for seq in range(1, (terminal.PTY_REPLAY_MAX_CHARS // len(filler)) + 40):
            terminal.push_output(db_path, sid, seq, filler)
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq=0")
        out = r.get_json()
        check("a console whose cursor fell behind is told it lost bytes", out["lost"] is True)
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq={out['next_seq'] - 1}")
        check("a caught-up console is not", r.get_json()["lost"] is False)

        print("\n== A session outlives the page: re-attach finds it again ==")
        # The operator navigated to Packages and came back. Nothing was closed, so the
        # lookup must hand them back the SAME shell rather than quietly opening a second
        # SYSTEM console next to the one still running their download.
        r = c.get("/api/fleet/pty?machine=PC-01")
        listed = r.get_json()["sessions"]
        check("the operator's open session is listed", any(s["session_id"] == sid for s in listed))
        check("with the shell it was opened as, so the dropdown can follow",
              next(s for s in listed if s["session_id"] == sid)["shell"] == "cmd")

        print("\n== Re-attaching replays the scrollback ==")
        # after_seq=-1 is the re-attach: it must return the whole retained buffer, because
        # that replay IS the restored history in the operator's fresh terminal.
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq=-1")
        replay = r.get_json()
        check("re-attach returns the retained history, not just what is new",
              len(replay["chunks"]) > 1)
        check("and reports that older output rolled out of the buffer",
              replay["replay_truncated"] is True)
        check("re-attach is not reported as a corrupt stream", replay["lost"] is False)
        # A fresh session has nothing to apologise for.
        fresh = c.post("/api/fleet/pty", json={"machine": "PC-01"}).get_json()["session_id"]
        terminal.push_output(db_path, fresh, 0, "hello")
        check("a session still holding all its output reports no truncation",
              c.get(f"/api/fleet/pty/{fresh}/output?after_seq=-1")
               .get_json()["replay_truncated"] is False)

        print("\n== Clear forgets the scrollback but keeps the shell ==")
        r = c.post(f"/api/fleet/pty/{fresh}/clear")
        check("clear -> 200", r.status_code == 200)
        check("the buffer is empty afterwards",
              c.get(f"/api/fleet/pty/{fresh}/output?after_seq=-1").get_json()["chunks"] == [])
        check("but the session is still live, not closed",
              terminal.get_session(db_path, fresh)["status"] != terminal.STATUS_CLOSED)
        check("and it still appears for re-attach",
              any(s["session_id"] == fresh
                  for s in c.get("/api/fleet/pty?machine=PC-01").get_json()["sessions"]))
        c.post(f"/api/fleet/pty/{fresh}/close")

        print("\n== Isolation: a session belongs to ONE operator ==")
        # 'other@x.com' has issue_commands on PC-01 -- they may open their OWN terminal here.
        # That is deliberately not consent to watch someone else's keystrokes.
        CURRENT_USER = "other@x.com"
        check("another operator cannot read the stream -> 404",
              c.get(f"/api/fleet/pty/{sid}/output").status_code == 404)
        check("another operator cannot type into it -> 404",
              c.post(f"/api/fleet/pty/{sid}/input", json={"data": "x"}).status_code == 404)
        check("another operator cannot close it -> 404",
              c.post(f"/api/fleet/pty/{sid}/close").status_code == 404)
        check("another operator cannot clear its scrollback -> 404",
              c.post(f"/api/fleet/pty/{sid}/clear").status_code == 404)
        check("and the re-attach lookup never shows them somebody else's terminal",
              all(s["session_id"] != sid
                  for s in c.get("/api/fleet/pty?machine=PC-01").get_json()["sessions"]))
        check("but they can open their own", c.post(
            "/api/fleet/pty", json={"machine": "PC-01"}).status_code == 201)

        CURRENT_USER = "viewer@x.com"
        check("view alone cannot open a terminal -> 403",
              c.post("/api/fleet/pty", json={"machine": "PC-01"}).status_code == 403)
        CURRENT_USER = "tech@x.com"
        check("out-of-scope machine -> 403",
              c.post("/api/fleet/pty", json={"machine": "PC-09"}).status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== Isolation: one agent cannot touch another's session ==")
        check("foreign agent reading keystrokes -> 404",
              c.get(f"/api/agent/pty/{sid}/input", headers=other_auth).status_code == 404)
        check("foreign agent injecting output -> 404",
              c.post(f"/api/agent/pty/{sid}/output",
                     json={"seq": 99, "chunk": "x"}, headers=other_auth).status_code == 404)
        check("foreign agent closing it -> 404",
              c.post(f"/api/agent/pty/{sid}/closed",
                     json={"reason": "x"}, headers=other_auth).status_code == 404)
        check("no bearer token at all -> 401",
              c.get(f"/api/agent/pty/{sid}/input").status_code == 401)

        print("\n== Closing: the console asks, the agent confirms ==")
        r = c.post(f"/api/fleet/pty/{sid}/close")
        check("close -> 200", r.status_code == 200)
        check("session is 'closing', not yet closed",
              terminal.get_session(db_path, sid)["status"] == terminal.STATUS_CLOSING)
        r = c.get(f"/api/agent/pty/{sid}/input?after_seq=-1", headers=auth)
        check("the agent's next poll tells it to shut down", r.get_json()["closing"] is True)
        check("typing into a closing session is refused -> 409",
              c.post(f"/api/fleet/pty/{sid}/input", json={"data": "x"}).status_code == 409)
        r = c.post(f"/api/agent/pty/{sid}/closed", json={"reason": "the shell exited"},
                   headers=auth)
        check("agent confirms -> 200", r.status_code == 200)
        check("session is closed with the agent's reason",
              terminal.get_session(db_path, sid)["status"] == terminal.STATUS_CLOSED
              and terminal.get_session(db_path, sid)["close_reason"] == "the shell exited")
        r = c.get(f"/api/fleet/pty/{sid}/output?after_seq=-1")
        check("the console's last poll still reports why it ended",
              r.get_json()["status"] == "closed"
              and r.get_json()["close_reason"] == "the shell exited")

        print("\n== A shell_open that expired means the machine never picked it up ==")
        r = c.post("/api/fleet/pty", json={"machine": "PC-01"})
        dead_sid = r.get_json()["session_id"]
        dead_cmd = r.get_json()["command_id"]
        fleet.cancel_command_if_pending(db_path, dead_cmd)   # what the TTL sweep does
        r = c.get(f"/api/fleet/pty/{dead_sid}/output")
        check("the console is told the agent never picked it up",
              r.get_json()["status"] == "closed"
              and "never picked it up" in (r.get_json()["close_reason"] or ""))

        print("\n== Limits ==")
        check("an over-long keystroke payload is refused -> 400",
              c.post("/api/fleet/pty/%s/input" % c.post(
                  "/api/fleet/pty", json={"machine": "PC-01"}).get_json()["session_id"],
                  json={"data": "x" * (terminal.PTY_MAX_INPUT_CHARS + 1)}).status_code == 400)

        # Sessions are real SYSTEM consoles; opening tabs must not be able to fill a machine
        # with them. Everything opened above still counts against super@x.com's quota.
        opened = len(terminal.list_sessions(db_path, machine="PC-01", operator="super@x.com"))
        while len(terminal.list_sessions(db_path, machine="PC-01",
                                         operator="super@x.com")) < terminal.PTY_MAX_SESSIONS_PER_OPERATOR:
            c.post("/api/fleet/pty", json={"machine": "PC-01"})
        r = c.post("/api/fleet/pty", json={"machine": "PC-01"})
        check("past the per-operator cap -> 400", r.status_code == 400)
        check("and the refusal says what to do", "close one" in (r.get_json()["error"] or ""))

        print("\n== Reaping: the two silences are measured separately ==")
        import time as _time
        now = int(_time.time())
        live = terminal.list_sessions(db_path, machine="PC-01", operator="super@x.com")[0]

        # An agent that keeps polling keeps last_activity fresh forever, which is exactly
        # why abandonment cannot be measured on it. A console that is still polling must
        # survive an agent-silence sweep AND an abandonment sweep.
        terminal.pull_input(db_path, live["id"], -1)          # agent checking in
        terminal.note_console_seen(db_path, live["id"])       # operator still watching
        terminal.reap_sessions(db_path, now=now)
        check("a session both ends are still using survives the sweep",
              terminal.get_session(db_path, live["id"])["status"] != terminal.STATUS_CLOSED)

        # The operator went to Packages for ten minutes. The agent is still polling. This
        # is the case persistence exists for, and it must NOT be reaped.
        terminal.reap_sessions(db_path, now=now + 10 * 60)
        check("leaving the page for ten minutes does not kill the shell",
              terminal.get_session(db_path, live["id"])["status"] != terminal.STATUS_CLOSED)

        # ...but not forever. Nobody came back. agent_silent is disabled for this sweep so
        # the abandonment rule is what closes it -- by the hour mark both conditions are
        # true, and the agent-silence rule (checked first) would otherwise claim the reason.
        terminal.reap_sessions(db_path, agent_silent_seconds=10 ** 9,
                               now=now + terminal.PTY_ABANDONED_SECONDS + 1)
        check("an abandoned session is eventually closed rather than left holding a SYSTEM shell",
              terminal.get_session(db_path, live["id"])["status"] == terminal.STATUS_CLOSED)
        check("and says why", "came back" in
              (terminal.get_session(db_path, live["id"])["close_reason"] or ""))

        # The other silence: the machine went away while the operator was still watching.
        gone = c.post("/api/fleet/pty", json={"machine": "PC-01"}).get_json()["session_id"]
        terminal.note_console_seen(db_path, gone, now=now + terminal.PTY_AGENT_SILENT_SECONDS + 2)
        terminal.reap_sessions(db_path, now=now + terminal.PTY_AGENT_SILENT_SECONDS + 2)
        check("a session whose agent stopped responding is closed even with a live console",
              terminal.get_session(db_path, gone)["status"] == terminal.STATUS_CLOSED)
        check("and says that, not 'abandoned'", "machine" in
              (terminal.get_session(db_path, gone)["close_reason"] or ""))
        check("reaping frees the operator's quota",
              c.post("/api/fleet/pty", json={"machine": "PC-01"}).status_code == 201)

        print("\n== The console's end of the input contract ==")
        # Source assertions, in the style of test_select_search: there is no browser harness
        # here, but these are joins between fleet-pty.js and this module that break silently.
        pty_js = read_static("js", "fleet-pty.js")

        # push_input REFUSES an over-length body rather than truncating it, so the console
        # has to split before it gets there. If these two drift apart, a pasted script or a
        # long favorite is rejected whole and the operator sees "[input not delivered]".
        check("fleet-pty.js splits input at the hub's cap",
              f"MAX_INPUT_CHARS = {terminal.PTY_MAX_INPUT_CHARS}" in pty_js)

        # Multi-line favorites used to be flattened to spaces here, which turned any script
        # longer than one statement into a different (usually invalid) one.
        check("fleet-pty.js no longer flattens newlines into spaces",
              "replace(/\\r?\\n/g, ' ')" not in pty_js)
        check("...it sends them as carriage returns instead",
              "replace(/\\n/g, '\\r')" in pty_js)

        # Returning false from an xterm custom key handler stops the KEY being forwarded but
        # does not preventDefault, so the browser still performs its own paste. Reading the
        # clipboard here as well is what doubled every Ctrl-Shift-V.
        check("fleet-pty.js leaves pasting to the browser",
              "clipboard.readText" not in pty_js)

        # Both terminals share one toolbar, so exactly one of them must answer a click on
        # it. fleet-terminal.js binds the three buttons once and dispatches on which
        # terminal is in front; fleet-pty.js exposes the handlers for its half. It used to
        # steal the buttons by replacing the nodes, which drops every listener on them --
        # that works exactly once, and a page whose machine can change re-runs activate().
        # A second run against a machine too old for a pseudoconsole would have found the
        # clones in place and BOTH sets of handlers gone.
        term_js = read_static("js", "fleet-terminal.js")
        check("the pty console no longer steals the shared buttons by cloning them",
              "replaceWith(fresh)" not in pty_js)
        for handler in ("clearActive", "openFavorites", "saveFavorite"):
            check(f"fleet-pty.js exposes its {handler} for the shared toolbar",
                  f"FleetPty.{handler}()" in term_js)
        check("...and fleet-terminal.js picks between them on who is in front",
              "owner === 'pty'" in term_js)

        print("\n== Terminals are not favoritable ==")
        # Nothing reusable to save: the params name a live session.
        try:
            fleet.create_favorite(db_path, email="super@x.com", name="term",
                                  command_type="shell_open", params={"session_id": "x"})
            check("saving a shell_open favorite is refused", False)
        except ValueError:
            check("saving a shell_open favorite is refused", True)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
