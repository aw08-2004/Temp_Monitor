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

**Firmware UPDATES (`update_bios`) live here too**, rather than in a blueprint of their
own. They are the same Firmware tab, the same `manage_firmware` gate, and the same
fetch-the-payload-from-an-authenticated-endpoint discipline -- a second file would have to
restate all three and could drift on any of them. The model half is `firmware.py`, which is
separate for the opposite reason: flashing has its own tables, its own scheduler and a
completion signal that arrives minutes later on a heartbeat.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
import os

from flask import Blueprint, jsonify, redirect, request, send_file, url_for

import backups
import bios
import firmware
import fleet
import packages
import permissions
import permissions_web
import refusals
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


def create_bios_blueprint(db_path, log_dir, login_required, access, hub_url=""):
    bp = Blueprint("bios", __name__)
    can_view = access.require(permissions.VIEW)
    can_issue = access.require(permissions.ISSUE_COMMANDS)
    can_manage = access.require(permissions.MANAGE_FIRMWARE)
    image_dir = firmware.blob_root(log_dir)

    def _current_email():
        return permissions_web.current_actor()

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
            return refusals.refuse(e)
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
            return refusals.refuse(e)

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
            return refusals.refuse(e)
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
            return refusals.refuse(e)
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
            # A re-read after a settings write is also the freshest BIOS VERSION we will
            # ever get from this machine, so it is the natural place to confirm a flash
            # that is waiting on one. Cheap on a fleet with none in flight (one indexed
            # query) and it saves a staged update sitting REBOOTING until the next
            # heartbeat carries the same fact.
            firmware.confirm_from_inventory(db_path, machine,
                                            data["bios"].get("bios_version"))
        return jsonify({"status": (updated or {}).get("status", bios.CHANGE_FAILED)}), 200

    # ================================================================
    # FIRMWARE UPDATES (roadmap #9, `update_bios`)
    # ================================================================
    #
    # Every console route here is `manage_firmware`, including the reads. That is a
    # deliberate departure from the inventory above, which is `view`: a firmware IMAGE
    # library is fleet configuration -- what is uploaded, which models it claims to fit,
    # which machines are queued to be flashed tonight -- not a fact about a PC. And unlike
    # the settings inventory, reading it tells you nothing an operator needs in order to
    # answer a support question.

    # ---------------- Page ----------------
    @bp.route("/firmware")
    @login_required
    @can_manage
    def firmware_page():
        """Kept as a redirect to where firmware lives now: Tools, Firmware tab.

        The page itself -- the image library, the fleet's update history, and the flash
        dialog that queues an update from an image -- moved there whole, so that it sits
        beside the per-machine BIOS view instead of a navigation away from it.

        The endpoint stays because bookmarks, links in tickets and url_for() calls do. Note
        the gate stays too, and runs BEFORE the redirect: whether this route exists must not
        become a way to learn something an operator may not see.
        """
        return redirect(url_for("tools_page") + "?tab=firmware")

    # ---------------- Console: images ----------------
    @bp.route("/api/firmware/payloads", methods=["GET"])
    @login_required
    @can_manage
    def list_firmware_payloads():
        return jsonify({"payloads": firmware.list_payloads(db_path)}), 200

    @bp.route("/api/firmware/upload", methods=["POST"])
    @login_required
    @can_manage
    def upload_firmware_image():
        """Store a BIOS image and return its sha256. Creates no payload record.

        Inert on purpose, like the package upload it borrows: the hash is computed from the
        bytes as they are written, so what an agent later verifies is what the hub actually
        holds rather than what an uploader claimed. The image is useless until somebody
        says which vendor and models it belongs to -- which is the next call, and the one
        that carries the refusal this feature depends on.
        """
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "No file was uploaded."}), 400
        max_bytes = settings.get_int(db_path, "firmware.max_upload_mb") * 1024 * 1024
        try:
            sha256, size = packages.store_blob(image_dir, upload.stream, max_bytes)
        except ValueError as e:
            return refusals.refuse(e)
        except OSError as e:
            return jsonify({"error": f"Could not store the image: {e}"}), 500
        fleet.audit(db_path, actor=_current_email(), action="upload_firmware_image",
                    level=fleet.LEVEL_NOTICE,
                    target=os.path.basename(upload.filename),
                    detail={"sha256": sha256, "bytes": size})
        return jsonify({"sha256": sha256, "file_size": size,
                        "file_name": os.path.basename(upload.filename)}), 201

    @bp.route("/api/firmware/payloads", methods=["POST"])
    @login_required
    @can_manage
    def create_firmware_payload():
        data = request.get_json(silent=True) or {}
        try:
            payload_id = firmware.create_payload(
                db_path, name=data.get("name"), vendor=data.get("vendor"),
                models=data.get("models"), to_version=data.get("to_version"),
                sha256=data.get("sha256"), size_bytes=data.get("file_size") or 0,
                filename=data.get("file_name") or "",
                install_args=data.get("install_args") or "",
                notes=data.get("notes") or "", created_by=_current_email())
        except firmware.PayloadRejected as e:
            return refusals.refuse(e)
        return jsonify({"payload": firmware.get_payload(db_path, payload_id)}), 201

    @bp.route("/api/firmware/payloads/<payload_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_firmware_payload(payload_id):
        try:
            deleted = firmware.delete_payload(db_path, payload_id,
                                              actor=_current_email(),
                                              blob_root_dir=image_dir)
        except firmware.PayloadRejected as e:
            return refusals.refuse(e, 409)
        if not deleted:
            return jsonify({"error": "unknown firmware image"}), 404
        return jsonify({"status": "deleted"}), 200

    # ---------------- Console: update jobs ----------------
    @bp.route("/api/firmware/jobs", methods=["GET"])
    @login_required
    @can_manage
    def list_firmware_jobs():
        machine = request.args.get("machine")
        if machine and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        return jsonify({"jobs": firmware.list_jobs(db_path, machine=machine)}), 200

    @bp.route("/api/firmware/jobs", methods=["POST"])
    @login_required
    @can_manage
    def create_firmware_job():
        """Flash one image onto a set of machines, now or inside a maintenance window.

        Scope is all-or-nothing, like a deployment: an out-of-scope machine is a 403 rather
        than a silently dropped target. Preconditions are the opposite -- a machine of the
        wrong model comes back as a REFUSED target with its reason, because that is a fact
        about the hardware the operator needs to read, not a malformed request.

        202, never 200: nothing has touched any firmware yet and will not until the
        scheduler dispatches. The console says *queued*.
        """
        data = request.get_json(silent=True) or {}
        names = [str(m or "").strip() for m in (data.get("machines") or [])]
        names = [n for n in names if n]
        if not names:
            return jsonify({"error": "Select at least one machine."}), 400
        denied = [n for n in names if not access.in_scope(n)]
        if denied:
            return jsonify({"error": f"You do not have access to {denied[0]!r}" +
                                     (f" and {len(denied) - 1} other machine(s)."
                                      if len(denied) > 1 else ".")}), 403
        try:
            job_id, targets = firmware.create_job(
                db_path, payload_id=data.get("payload_id"), machines=names,
                created_by=_current_email(), note=data.get("note"),
                window_start=data.get("window_start"),
                window_end=data.get("window_end"),
                machine_facts=firmware.read_machine_facts(db_path, names))
        except firmware.PayloadRejected as e:
            return refusals.refuse(e)
        return jsonify({
            "job_id": job_id,
            "queued": [t["machine"] for t in targets
                       if t["status"] == firmware.TARGET_PENDING],
            # Named back to the operator rather than dropped -- the plan_restore precedent.
            # "Queued 37 of 40" with no word on the other three is how somebody believes a
            # fleet was updated when it was not.
            "refused": [{"machine": t["machine"], "reason": t["error"]} for t in targets
                        if t["status"] == firmware.TARGET_REFUSED],
        }), 202

    @bp.route("/api/firmware/jobs/<job_id>", methods=["GET"])
    @login_required
    @can_manage
    def get_firmware_job(job_id):
        job = firmware.get_job(db_path, job_id)
        if job is None:
            return jsonify({"error": "unknown firmware update"}), 404
        # Targets are filtered to the caller's scope rather than the job being refused
        # outright: a fleet-wide job legitimately spans machines one operator cannot see,
        # and hiding the job entirely would leave them unable to follow their own machines
        # through it.
        job["targets"] = [t for t in job["targets"] if access.in_scope(t["machine"])]
        return jsonify(job), 200

    @bp.route("/api/firmware/jobs/<job_id>/cancel", methods=["POST"])
    @login_required
    @can_manage
    def cancel_firmware_job(job_id):
        """Stop every machine that has not been handed the image yet.

        The answer names both numbers. A job cancelled while six machines are already
        flashing has stopped nothing for those six, and a console that reported a clean
        cancellation over hardware being written to right now would be lying at the worst
        possible moment.
        """
        if firmware.get_job(db_path, job_id, with_targets=False) is None:
            return jsonify({"error": "unknown firmware update"}), 404
        cancelled, still_flashing = firmware.cancel_job(db_path, job_id,
                                                        actor=_current_email())
        return jsonify({"cancelled": cancelled, "still_flashing": still_flashing}), 200

    @bp.route("/api/firmware/updates/<update_id>/cancel", methods=["POST"])
    @login_required
    @can_manage
    def cancel_firmware_update(update_id):
        """Stop one machine's flash, if it has not been handed the image yet."""
        target = firmware.get_target(db_path, update_id)
        if target is None:
            return jsonify({"error": "unknown firmware update"}), 404
        if not access.in_scope(target["machine"]):
            return jsonify({"error": "You do not have access to that machine."}), 403
        ok, status = firmware.cancel_target(db_path, update_id, actor=_current_email())
        if not ok:
            return jsonify({
                "error": "That machine has already been handed the image and cannot be "
                         "recalled. Its firmware may already have been written.",
                "status": status,
            }), 409
        return jsonify({"status": status}), 200

    # ---------------- Agent ----------------
    @bp.route("/api/agent/firmware/update/<update_id>", methods=["GET"])
    def agent_firmware_update(update_id):
        """Everything the agent needs to flash, fetched once.

        Answers the image URL, its digest, the vendor arguments, the BIOS setup password
        and the power preconditions -- none of which were in the command params, because
        `fleet.create_command` audits params verbatim and two of those are a credential and
        a download link.

        The fetch flips the target to FLASHING, conditionally. That single UPDATE is what
        stops a redelivered command flashing a machine twice, and it is also the moment
        cancelling stops being possible -- which is correct, because from here on the
        firmware may already have been written.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        target = firmware.get_target(db_path, update_id)
        if target is None or target["machine"] != machine:
            return jsonify({"error": "unknown firmware update"}), 404
        if not firmware.start_target(db_path, update_id):
            return jsonify({"error": "that firmware update is no longer pending"}), 409
        base = (hub_url or request.host_url).rstrip("/")
        return jsonify({
            "update_id": update_id,
            "url": f"{base}/api/agent/firmware/image/{target['sha256']}",
            "sha256": target["sha256"],
            "size_bytes": target["size_bytes"],
            "filename": target["filename"],
            "install_args": target["install_args"],
            # Re-checked on the machine against what the hardware says about itself right
            # now. The hub checked the same things against an inventory that may be hours
            # old, and a chassis swap between the two is exactly the case where flashing
            # the wrong image has no undo.
            "vendor": target["payload_vendor"],
            "models": target["models"],
            "to_version": target["to_version"],
            "password": _password_for(machine),
            "require_ac_power": settings.get_bool(db_path, "firmware.require_ac_power"),
            "min_battery_percent": settings.get_int(db_path,
                                                    "firmware.min_battery_percent"),
        }), 200

    @bp.route("/api/agent/firmware/update/<update_id>/result", methods=["POST"])
    def agent_firmware_update_result(update_id):
        """What the vendor tool said. Note what this does NOT decide: success.

        A tool that returns 0 has staged an image the firmware writes during POST, so a
        good report moves the target to REBOOTING and stops there. Only the machine coming
        back reporting a new BIOS version closes it out, on the heartbeat -- see
        firmware.confirm_from_inventory. Treating this response as the answer is exactly
        the guess the whole feature exists to avoid.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        target = firmware.get_target(db_path, update_id)
        if target is None or target["machine"] != machine:
            return jsonify({"error": "unknown firmware update"}), 404
        data = request.get_json(silent=True) or {}
        updated = firmware.ingest_result(db_path, update_id, data)
        return jsonify({"status": (updated or {}).get("status",
                                                      firmware.TARGET_FAILED)}), 200

    @bp.route("/api/agent/firmware/image/<sha256>", methods=["GET"])
    def agent_download_firmware(sha256):
        """Serve a BIOS image to an enrolled agent.

        The digest must belong to a payload row, which is what keeps this from being a
        general-purpose read primitive over the firmware directory. Deliberately NOT gated
        on the machine having an open update: a flash that failed its download and retries
        after the target was swept would otherwise be unable to fetch the file it is
        halfway through, and the bytes are a vendor's public BIOS image either way.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        try:
            digest = packages.normalize_sha256(sha256)
        except ValueError:
            return jsonify({"error": "not found"}), 404
        if not digest or firmware.payload_for_blob(db_path, digest) is None:
            return jsonify({"error": "not found"}), 404
        path = packages.blob_path(image_dir, digest)
        if not os.path.exists(path):
            # The row survives but the file is gone -- a half-restored backup, or a manual
            # tidy-up. Say so rather than 404ing: the fix is on the hub, not the agent.
            return jsonify({"error": "firmware image is missing on the hub"}), 410
        return send_file(path, as_attachment=True, download_name=digest,
                         mimetype="application/octet-stream")

    return bp
