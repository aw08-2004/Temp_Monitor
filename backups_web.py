"""Flask HTTP surface for backups -- a thin layer over backups.py, in the same shape as
packages_web.py and permissions_web.py.

One audience and one gate: the console, behind the `manage_backups` capability. There is
deliberately no agent-facing endpoint yet -- roadmap #1b (per-PC file backups) adds one,
and it will be the place `backups.S3Destination.presigned_url` is finally called.

Backups are NOT machine-scoped. A hub database backup is the whole hub -- permission
groups, every machine's history, the audit log -- so there is no coherent way to hand it
to an operator who may only see nine machines out of forty. `manage_backups` is therefore
an all-or-nothing capability here, and an Admin granting it should read it as "can read
everything in the hub, eventually, via a restore". When #1b lands, per-machine file
backups DO get the usual `access.in_scope()` treatment; this module's routes are the
hub-wide half.

Two rules inherited from the rest of the codebase, both load-bearing:

  * **Every mutating route reads a JSON body**, which is what makes a cross-site POST
    preflight and fail -- see fleet_web.py's module docstring. Note that this includes
    revealing the master key: it is a POST with a JSON body, not a GET, precisely so a
    stray `<img src>` or a link in an email cannot cause a browser to fetch it.
  * **Secrets travel in one direction.** A destination's credentials go in and are never
    returned, not even masked -- the edit form shows an empty credential field meaning
    "unchanged". The single exception is the master key reveal, which is the whole point
    of that route, and which is audited every time.

Manual backups run on a background thread. A hub database of any size takes longer than a
browser is willing to wait, and holding a worker for the duration would block the very
console the operator is watching for progress. The route returns 202 with a run id; the
page polls the run list, exactly as the packages page polls a deployment.
"""
import threading
import time

from flask import Blueprint, jsonify, render_template, request, session

import backups
import fleet
import permissions
import settings


def create_backups_blueprint(db_path, log_dir, env_path, login_required, access,
                             hub_version=""):
    """Build the backups Blueprint.

    `log_dir` is where the encrypted secret store and the scratch space for snapshots
    live (beside the database, like the package blob store); `env_path` is the `.env` the
    master key is written to. Both are passed in rather than imported from app.py, to
    avoid a circular import and because the test suite re-points them.
    """
    bp = Blueprint("backups", __name__)
    can_manage = access.require(permissions.MANAGE_BACKUPS)

    def _current_email():
        """The signed-in operator. ALWAYS from the session -- never a request body, or
        the audit trail becomes fiction."""
        return (session.get("user") or {}).get("email", "unknown")

    def _master_key_or_error():
        """The master key as bytes, or (None, response) if there isn't a usable one."""
        try:
            key = backups.load_master_key()
        except ValueError as e:
            return None, (jsonify({"error": str(e)}), 400)
        if key is None:
            return None, (jsonify({
                "error": "No backup encryption key exists yet. Create one first."
            }), 409)
        return key, None

    def _schedule_state():
        """The scheduler's configured shape, resolved from settings for the UI header."""
        interval = settings.get_int(db_path, "backup.hub_interval_hours")
        enabled = settings.get_bool(db_path, "backup.hub_enabled")
        destination_id = settings.get(db_path, "backup.hub_destination") or ""
        last_success = backups.get_state(db_path, backups.LAST_SUCCESS_STATE_KEY)
        return {
            "enabled": bool(enabled),
            "destination_id": destination_id,
            "interval_hours": interval,
            "keep_generations": settings.get_int(db_path, "backup.hub_keep_generations"),
            "last_success_at": int(last_success) if last_success else None,
            "next_due_at": (backups.next_due_at(db_path, interval)
                            if enabled and destination_id else None),
            "running": backups.backup_in_progress(),
        }

    def _key_state():
        """Everything the console needs to nag correctly, and nothing that reveals the
        key itself."""
        try:
            key = backups.load_master_key()
        except ValueError as e:
            return {"configured": False, "error": str(e), "escrowed_at": None,
                    "key_id": None}
        escrowed = backups.get_state(db_path, backups.KEY_ESCROW_STATE_KEY)
        return {
            "configured": key is not None,
            "key_id": backups.key_id(key) if key else None,
            "escrowed_at": int(escrowed) if escrowed else None,
            "crypto_available": backups.CRYPTO_AVAILABLE,
            "error": None,
        }

    # ---------------- Pages ----------------
    @bp.route("/backups")
    @login_required
    @can_manage
    def backups_page():
        return render_template("backups.html")

    # ---------------- Console: overview ----------------
    @bp.route("/api/backups", methods=["GET"])
    @login_required
    @can_manage
    def overview():
        return jsonify({
            "destinations": backups.list_destinations(db_path, log_dir),
            "runs": backups.list_runs(db_path, limit=25, kind=backups.BACKUP_HUB_DB),
            "schedule": _schedule_state(),
            "key": _key_state(),
            # The form renders itself from these, so adding a destination kind is one
            # edit in backups.py -- the same self-describing-API discipline as
            # /api/packages and /api/permissions/capabilities.
            "destination_kinds": [
                {"name": kind,
                 "label": backups.DESTINATION_LABELS[kind][0],
                 "description": backups.DESTINATION_LABELS[kind][1]}
                for kind in backups.DESTINATION_KINDS
            ],
        }), 200

    @bp.route("/api/backups/runs", methods=["GET"])
    @login_required
    @can_manage
    def list_runs():
        return jsonify({
            "runs": backups.list_runs(db_path, limit=25, kind=backups.BACKUP_HUB_DB),
            "schedule": _schedule_state(),
        }), 200

    # ---------------- Console: the master key ----------------
    @bp.route("/api/backups/key", methods=["POST"])
    @login_required
    @can_manage
    def create_key():
        """Generate the master key, once, and return it in the clear.

        This is the only moment the key is displayed automatically, and the response is
        the operator's cue to store it somewhere that is not this server. Creating a
        second one is refused rather than silently rotating: every existing artifact was
        encrypted with the first, and a hub that quietly replaced it would turn every
        backup taken so far into noise.
        """
        request.get_json(silent=True)      # CSRF: a JSON body is required, see docstring
        try:
            key_b64, created = backups.ensure_master_key(env_path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not created:
            return jsonify({"error": "A backup encryption key already exists."}), 409
        fleet.audit(db_path, actor=_current_email(), action="backup_key_create",
                    detail={"key_id": backups.key_id(backups.decode_master_key(key_b64))})
        return jsonify({"key": key_b64, "state": _key_state()}), 201

    @bp.route("/api/backups/key/reveal", methods=["POST"])
    @login_required
    @can_manage
    def reveal_key():
        """Show the key again -- for the operator who is finally writing it down.

        A POST, not a GET, so it cannot be triggered by a link or an embedded resource
        (see the module docstring). Audited every single time, because "who has seen the
        key that decrypts every backup" is exactly the question an incident asks.
        """
        request.get_json(silent=True)
        key_b64 = backups.master_key_b64()
        if not key_b64:
            return jsonify({"error": "No backup encryption key exists yet."}), 409
        fleet.audit(db_path, actor=_current_email(), action="backup_key_reveal",
                    detail={"key_id": backups.key_id(
                        backups.decode_master_key(key_b64))})
        return jsonify({"key": key_b64}), 200

    @bp.route("/api/backups/key/escrowed", methods=["POST"])
    @login_required
    @can_manage
    def acknowledge_escrow():
        """Record that a human has stored the key off this machine.

        Only ever an acknowledgement -- the hub cannot verify it, and does not pretend
        to. Its value is that the console stops claiming the backups are safe when nobody
        has ever copied the one thing that decrypts them, and that the audit log names
        who said otherwise.
        """
        request.get_json(silent=True)
        key, error = _master_key_or_error()
        if error:
            return error
        backups.set_state(db_path, backups.KEY_ESCROW_STATE_KEY, int(time.time()))
        fleet.audit(db_path, actor=_current_email(), action="backup_key_escrowed",
                    detail={"key_id": backups.key_id(key)})
        return jsonify({"key": _key_state()}), 200

    # ---------------- Console: destinations ----------------
    @bp.route("/api/backups/destinations", methods=["POST"])
    @login_required
    @can_manage
    def create_destination():
        data = request.get_json(silent=True) or {}
        key, error = _master_key_or_error()
        if error:
            return error
        try:
            destination_id = backups.create_destination(
                db_path, log_dir, key,
                name=data.get("name"),
                kind=data.get("kind"),
                config=data.get("config"),
                secret=data.get("secret"),
                actor=_current_email(),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(backups.get_destination(db_path, destination_id, log_dir)), 201

    @bp.route("/api/backups/destinations/<destination_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update_destination(destination_id):
        data = request.get_json(silent=True) or {}
        key, error = _master_key_or_error()
        if error:
            return error
        try:
            record = backups.update_destination(
                db_path, log_dir, key, destination_id,
                name=data.get("name"),
                config=data.get("config"),
                secret=data.get("secret"),
                actor=_current_email(),
            )
        except KeyError:
            return jsonify({"error": "unknown destination"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(record), 200

    @bp.route("/api/backups/destinations/<destination_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_destination(destination_id):
        try:
            backups.delete_destination(db_path, log_dir, destination_id,
                                       actor=_current_email())
        except KeyError:
            return jsonify({"error": "unknown destination"}), 404
        # Disarm a schedule that pointed at it. Leaving the setting behind would mean a
        # scheduler that wakes up, fails to resolve the destination, and writes a red run
        # row every interval forever -- an alarm about a decision the operator already
        # made deliberately. Done here rather than in backups.py, which is settings-free
        # by design (the scheduler's knobs are passed in, never read).
        if settings.get(db_path, "backup.hub_destination") == destination_id:
            settings.set_many(db_path, {"backup.hub_destination": "",
                                        "backup.hub_enabled": False},
                              updated_by=_current_email())
        return jsonify({"status": "deleted"}), 200

    @bp.route("/api/backups/destinations/<destination_id>/test", methods=["POST"])
    @login_required
    @can_manage
    def test_destination(destination_id):
        """Round-trip a probe object. Synchronous on purpose: it is small, and an
        operator who just typed a credential is waiting for exactly this answer."""
        request.get_json(silent=True)
        try:
            summary = backups.probe_destination(db_path, log_dir, destination_id,
                                                actor=_current_email())
        except (backups.BackupError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "ok", "detail": summary}), 200

    # ---------------- Console: the schedule ----------------
    @bp.route("/api/backups/schedule", methods=["PUT"])
    @login_required
    @can_manage
    def update_schedule():
        """Write the `backup.*` settings from the Backups page.

        These four keys are also on the Settings page, but reaching them there needs
        `manage_settings` -- a much broader grant than "can configure backups". Routing
        them through this capability is what stops `manage_backups` from being a
        capability that cannot actually turn a backup on. The allow-list comes from
        settings.BACKUP_SCHEDULE_KEYS rather than a prefix test, so this can never become
        a general settings-write endpoint by accident.
        """
        data = request.get_json(silent=True) or {}
        updates = {k: v for k, v in data.items()
                   if k in settings.BACKUP_SCHEDULE_KEYS}
        unknown = set(data) - set(updates)
        if unknown:
            return jsonify({"error": f"Not a backup schedule setting: "
                                     f"{sorted(unknown)[0]}"}), 400
        if not updates:
            return jsonify({"error": "Nothing to update."}), 400

        destination_id = updates.get("backup.hub_destination")
        if destination_id and backups.get_destination(db_path, destination_id) is None:
            return jsonify({"error": "unknown destination"}), 404
        # Refusing to arm a schedule with no destination here rather than letting the
        # scheduler quietly skip every tick: "enabled, and doing nothing" is the exact
        # state an operator would never think to check.
        if updates.get("backup.hub_enabled"):
            target = destination_id or settings.get(db_path, "backup.hub_destination")
            if not target:
                return jsonify({
                    "error": "Choose a destination before turning the schedule on."
                }), 400

        try:
            settings.set_many(db_path, updates, updated_by=_current_email())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        fleet.audit(db_path, actor=_current_email(), action="backup_schedule_update",
                    detail=updates)
        return jsonify({"schedule": _schedule_state()}), 200

    # ---------------- Console: run a backup now ----------------
    @bp.route("/api/backups/run", methods=["POST"])
    @login_required
    @can_manage
    def run_now():
        """Start a hub database backup on a background thread; return 202 immediately.

        Settings are read HERE, on the request thread, and passed to the worker as plain
        values -- the same contract backups.tick() has. A worker that reached back into
        settings would be reading them minutes later, potentially mid-edit.
        """
        data = request.get_json(silent=True) or {}
        destination_id = (data.get("destination_id") or
                          settings.get(db_path, "backup.hub_destination") or "")
        if not destination_id:
            return jsonify({"error": "Choose a destination to back up to."}), 400
        if backups.get_destination(db_path, destination_id) is None:
            return jsonify({"error": "unknown destination"}), 404
        key, error = _master_key_or_error()
        if error:
            return error
        if backups.backup_in_progress():
            return jsonify({"error": "A backup is already running."}), 409

        keep = settings.get_int(db_path, "backup.hub_keep_generations")
        actor = _current_email()

        def worker():
            try:
                backups.backup_hub_database(
                    db_path, log_dir, destination_id, keep=keep,
                    trigger=backups.TRIGGER_MANUAL, actor=actor,
                    hub_version=hub_version)
            except Exception as e:      # pragma: no cover - belt and braces
                # backup_hub_database records expected failures itself; anything reaching
                # here is a bug, and a daemon thread dying silently would hide it.
                print(f"[backup] Manual run failed unexpectedly: {e}")

        threading.Thread(target=worker, daemon=True, name="backup_manual").start()
        return jsonify({"status": "started"}), 202

    return bp
