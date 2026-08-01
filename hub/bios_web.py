"""Flask HTTP surface for BIOS/firmware inventory and writes (roadmap #9) -- a thin layer
over bios.py, registered as a Blueprint from app.py.

**Three gates, and the splits are all deliberate:**

  * **Reading** the inventory is `view` + machine scope. What firmware a PC is set to is
    inventory data, exactly like its model or its disk layout, and an operator who can see a
    machine can see it. This is also why it is not behind `manage_settings`: needing an
    admin to answer "is Wake-on-LAN on?" would make the question cost more than walking to
    the desk.
  * **Forcing a re-read** is `issue_commands` + machine scope, because it queues a real
    command on a real machine. Cheap and read-only on the far end, but the perimeter for
    "make that PC do something" is the command channel's, not the viewer's.
  * **Changing a setting** is `manage_firmware` + machine scope -- its own capability, not a
    reuse of `deploy_packages`. This is the one action in the product with no restore path,
    and folding it into "can push an installer" would have granted it silently, on the day it
    shipped, to everyone who already had that.

**The BIOS setup password never travels in command params.** `fleet.create_command` audits
params verbatim, so a password there would sit in the audit log inside the database that is
itself backed up. The command carries a change id; the agent fetches the attribute list and
the password together from `/api/agent/bios/change/<id>`, authenticated as itself and
authorised only for its own change. That is the restore-plan precedent, for the same reason.

Handing an agent the password is a real widening of blast radius, and it is the only design
that works: the write happens in the machine's own firmware interface, so the machine must
hold the credential at the moment it writes. It is minted at FETCH time rather than dispatch,
so a command picked up after a weekend offline does not carry a stale one, and a change that
has already been fetched once will not hand it over twice (`bios.start_change` is conditional).

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request, session

import backups
import bios
import fleet
import permissions
import settings


def _bearer_agent(db_path):
    """Same header contract as fleet_web._bearer_agent: 'Bearer <agent_id>:<token>'."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    raw = header[len("Bearer "):].strip()
    agent_id, _, token = raw.partition(":")
    if not agent_id or not token:
        return None, None
    machine = fleet.authenticate_agent(db_path, agent_id, token)
    if machine is None:
        return None, None
    return agent_id, machine


def create_bios_blueprint(db_path, log_dir, login_required, access):
    bp = Blueprint("bios", __name__)
    can_view = access.require(permissions.VIEW)
    can_issue = access.require(permissions.ISSUE_COMMANDS)
    can_manage = access.require(permissions.MANAGE_FIRMWARE)

    def _current_email():
        return (session.get("user") or {}).get("email", "unknown")

    def _password_for(machine):
        """The BIOS setup password to send with a change, or None.

        Per-machine wins outright over the fleet entry -- there is nothing to merge in a
        single opaque string, and a machine with its own override has one precisely because
        the fleet password is wrong for it.

        A store that cannot be opened (no master key configured, no `cryptography`, a rotated
        key) yields None rather than raising: a machine with no password set does not need
        one, and failing the write for every machine because one secret is unreadable would be
        the wrong trade. The agent reports the vendor's own refusal if a password really was
        required, which is the message an operator can act on anyway.
        """
        master = backups.load_master_key()
        if master is None:
            return None
        for secret_id in (bios.secret_id_for(machine), bios.SECRET_ID_FLEET):
            if not backups.has_secret(log_dir, secret_id):
                continue
            try:
                stored = backups.load_secret(log_dir, master, secret_id)
            except ValueError:
                continue
            password = (stored or {}).get("password")
            if password:
                return password
        return None

    # ---------------- Console: read ----------------
    @bp.route("/api/bios/<machine>", methods=["GET"])
    @login_required
    @can_view
    def machine_bios(machine):
        """The machine's firmware settings as of its last report, plus its change history.

        Answers from storage rather than from the machine: a BIOS enumeration takes seconds
        on some vendors, and a page that blocks on one would be a page nobody opens twice.
        `/refresh` is there for when the stored answer is not good enough.

        `support: null` means no agent has ever told us -- distinct from `unsupported`, which
        is a machine that has told us it has nothing to manage. The console must render those
        two differently or every fleet looks unmanageable the day before the agent release.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        payload = bios.get_inventory(db_path, machine)
        # Bundled with the inventory rather than behind a second request: the tab renders both
        # together, and a pending change is the single most important thing on the page --
        # every value shown beside it may be about to be wrong.
        payload["changes"] = bios.list_changes(db_path, machine)
        # Whether a password is STORED, never what it is. Shown so an operator knows before
        # they try, since on all three vendors a set password blocks writes.
        payload["password_stored"] = (
            backups.has_secret(log_dir, bios.secret_id_for(machine))
            or backups.has_secret(log_dir, bios.SECRET_ID_FLEET))
        payload["can_manage_firmware"] = access.can(permissions.MANAGE_FIRMWARE)
        return jsonify(payload), 200

    @bp.route("/api/bios/<machine>/refresh", methods=["POST"])
    @login_required
    @can_issue
    def refresh_bios(machine):
        """Queue a re-read. The agent's firmware inventory rides the heartbeat on a
        change-detected, self-throttled cadence -- right for the steady state, wrong for the
        operator who has just changed a setting in the BIOS by hand and wants to see it."""
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type="refresh_bios_inventory",
                params={}, issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 202

    # ---------------- Console: write ----------------
    @bp.route("/api/bios/<machine>/settings", methods=["POST"])
    @login_required
    @can_manage
    def set_bios_settings(machine):
        """Change one or more firmware settings on a machine.

        Validation is against the machine's OWN reported attributes (bios.validate_changes),
        never against a hub-side rulebook -- v1 maps no cross-vendor vocabulary, so the only
        thing that knows whether an attribute exists and what it accepts is the machine that
        reported it.

        Answers 202 with a change id. Not 200: nothing has happened to the firmware yet, and
        will not until the agent polls. The console says *queued*, the same honesty "Back up
        now" needs.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403

        data = request.get_json(silent=True) or {}
        inventory = bios.get_inventory(db_path, machine)
        try:
            changes = bios.validate_changes(inventory, data.get("changes"))
        except bios.ChangeRejected as e:
            return jsonify({"error": str(e)}), 400

        existing = bios.open_change_for(db_path, machine)
        if existing is not None:
            # Two concurrent writes would race in the firmware, and verification -- which
            # compares one current value per attribute -- could not say which one it read.
            return jsonify({
                "error": "A firmware change is already in flight for this machine. Wait for "
                         "it to finish, or cancel it.",
                "change_id": existing["id"],
            }), 409

        change_id = bios.create_change(db_path, machine, changes, _current_email())
        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type="set_bios_settings",
                params={"change_id": change_id}, issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            bios.cancel_change(db_path, change_id)
            return jsonify({"error": str(e)}), 400
        bios.attach_command(db_path, change_id, command_id)

        # A SECOND audit row beside the `issue_command` one that create_command wrote. That
        # row audits params verbatim, and the params here are just a change id -- so without
        # this, the audit trail would record that somebody changed firmware on this machine
        # and not one word about what they changed it to.
        fleet.audit(db_path, actor=_current_email(), action="bios_settings_change",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"change_id": change_id, "command_id": command_id,
                             "changes": [{"name": c["name"], "from": c["from"],
                                          "to": c["to"]} for c in changes]})
        return jsonify({"change_id": change_id, "command_id": command_id,
                        "changes": changes}), 202

    @bp.route("/api/bios/<machine>/changes/<change_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def cancel_bios_change(machine, change_id):
        """Cancel a change the machine has not picked up yet.

        Only a *pending* change can be cancelled. Once the agent has fetched the payload it
        may already have written to the firmware, and a console row reading "cancelled" over
        a machine whose Secure Boot is now off is worse than no cancel button at all -- the
        same three-outcomes honesty the backup cancel needed, minus the middle case, because
        firmware writes are seconds rather than hours and there is no run to free.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        change = bios.get_change(db_path, change_id)
        if change is None or change["machine"] != machine:
            return jsonify({"error": "unknown change"}), 404
        if not bios.cancel_change(db_path, change_id):
            return jsonify({"error": "That change has already been sent to the machine and "
                                     "cannot be recalled."}), 409
        if change["command_id"]:
            fleet.cancel_command_if_pending(db_path, change["command_id"])
        fleet.audit(db_path, actor=_current_email(), action="bios_settings_change",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"change_id": change_id, "cancelled": True})
        return jsonify({"status": "cancelled"}), 200

    # ---------------- Console: BIOS setup password ----------------
    #
    # Stored through the backup secret store: .env-master-key-wrapped, in a sidecar file
    # beside the database, keyed by an opaque id. Never in the `settings` table, which is
    # rendered into a form, dumped by as_dict() and partly shipped to agents.
    #
    # There is no GET that returns a password, only whether one is set. A reveal would need
    # its own audited endpoint and a reason to exist, and there is not one: the hub sends it
    # to the machine that needs it and nobody has to read it off the screen.
    #
    # `/api/bios-password`, not `/api/bios/password`: the latter collides with
    # `/api/bios/<machine>`, which would both shadow a machine actually named "password" and
    # -- much worse -- make `GET /api/bios/password` a 200 that LOOKS like a password read.
    # A route that reads like a reveal endpoint is one somebody eventually treats as one.
    @bp.route("/api/bios-password", methods=["PUT", "DELETE"])
    @bp.route("/api/bios-password/<machine>", methods=["PUT", "DELETE"])
    @login_required
    @can_manage
    def bios_password(machine=None):
        """Set or clear the fleet-wide BIOS setup password, or one machine's override."""
        if machine is not None and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        secret_id = bios.SECRET_ID_FLEET if machine is None else bios.secret_id_for(machine)

        if request.method == "DELETE":
            backups.delete_secret(log_dir, secret_id)
            fleet.audit(db_path, actor=_current_email(), action="bios_password_clear",
                        level=fleet.LEVEL_SECURITY, target=machine or "fleet",
                        detail={"scope": "machine" if machine else "fleet"})
            return jsonify({"status": "cleared"}), 200

        data = request.get_json(silent=True) or {}
        password = str(data.get("password") or "")
        if not password:
            # Clearing is the DELETE, explicitly. An empty PUT storing an empty password
            # would look identical in the UI to "no password set" and behave differently.
            return jsonify({"error": "A password is required. Use Clear to remove a stored "
                                     "one."}), 400
        master = backups.load_master_key()
        if master is None:
            # The same key that protects backup destination credentials. Refused rather than
            # stored in the clear -- a BIOS password sitting readable beside the database is
            # exactly the shape of secret this store exists to avoid.
            return jsonify({"error": "No BACKUP_MASTER_KEY is configured, so secrets cannot "
                                     "be encrypted. Generate one on the Backups page "
                                     "first."}), 400
        try:
            backups.store_secret(log_dir, master, secret_id, {"password": password})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        fleet.audit(db_path, actor=_current_email(), action="bios_password_set",
                    level=fleet.LEVEL_SECURITY, target=machine or "fleet",
                    detail={"scope": "machine" if machine else "fleet"})
        return jsonify({"status": "stored"}), 200

    # ---------------- Agent ----------------
    #
    # Bearer agent auth, and every route checks the change belongs to the CALLING machine. An
    # enrolled agent acts for itself and nothing else -- PC-3 must not be able to read PC-4's
    # pending change, which here would mean reading PC-4's BIOS password.
    def _agent_change(change_id, machine):
        change = bios.get_change(db_path, change_id)
        if change is None or change["machine"] != machine:
            return None
        return change

    @bp.route("/api/agent/bios/change/<change_id>", methods=["GET"])
    def agent_bios_change(change_id):
        """The attribute list and the BIOS setup password for one change.

        Fetching flips the change to RUNNING, conditionally -- so two polls delivering the
        same command cannot replay the writes, and cancel stops being possible at exactly the
        moment the machine could have started writing. Both properties come from the one
        UPDATE ... WHERE status='pending'.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        change = _agent_change(change_id, machine)
        if change is None:
            return jsonify({"error": "unknown change"}), 404
        if not bios.start_change(db_path, change_id):
            return jsonify({"error": "that change is no longer pending"}), 409
        return jsonify({
            "change_id": change_id,
            "changes": [{"name": c["name"], "value": c["to"], "kind": c.get("kind", "")}
                        for c in change["changes"]],
            # Null when none is stored. The agent passes it to the vendor interface if the
            # vendor needs one; it is never written anywhere on the machine.
            "password": _password_for(machine),
        }), 200

    @bp.route("/api/agent/bios/change/<change_id>/result", methods=["POST"])
    def agent_bios_change_result(change_id):
        """What the firmware said when the agent read the attributes back.

        The re-read is the point of this endpoint and the reason the write half needs one at
        all: the agent's exit code says a WMI method returned success, which on firmware is
        not the same claim as "the setting is now that". bios.classify_result turns the
        observed values into applied / pending_reboot / failed / unknown per attribute.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        change = _agent_change(change_id, machine)
        if change is None:
            return jsonify({"error": "unknown change"}), 404

        data = request.get_json(silent=True) or {}
        updated = bios.ingest_change_result(db_path, change_id, data)
        # A full fresh inventory usually rides along, because the agent has just re-read the
        # machine anyway. Taking it here means the tab is current the moment the change
        # resolves, instead of showing the pre-change values until the next heartbeat.
        if isinstance(data.get("bios"), dict):
            bios.record_inventory(db_path, machine, data["bios"])
        return jsonify({"status": (updated or {}).get("status", bios.CHANGE_FAILED)}), 200

    return bp
