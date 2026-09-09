"""Flask HTTP surface for remote lock and wipe (roadmap #23 phase H) -- a thin layer over
wipe.py, registered as a Blueprint from app.py.

**This is the only door.** Three other paths could otherwise reach these commands, and all
three are closed rather than trusted to be unused: `/api/fleet/commands` refuses them (its gate
is `issue_commands`), `fleet._validate_favorite` refuses to save one, and
`rules.RULE_FORBIDDEN_COMMANDS` refuses to let a rule issue one. A rule is the worst of the
three: it fires with nobody present, which is the last way anybody should learn that a phone
has been erased.

**Two gates, split the way location's are.**

  * **Reading** what has been done to a device is `view` + machine scope. That a device was
    wiped in March is not a privilege to know; it is the only explanation an operator will ever
    get for why the machine stopped reporting, because the device itself cannot tell them.
  * **Doing** it is **`wipe_device`** + machine scope. Not `issue_commands`, on the argument
    that keeps every other command feature under that gate: "less dangerous than the SYSTEM
    shell it already grants" is true of a reboot and false of a factory reset.

**The typed-name confirmation is checked on the server**, in wipe.confirm_wipe. A confirmation
that lives only in the console stops an operator and does not stop a script, a copied curl
command, or a second console written later -- and this is the one endpoint where those must not
be a way around the pause.

**Locking and wiping are separate routes rather than one route with an action field.** They
have different bodies, different confirmations and different consequences, and a single route
whose destructiveness depends on a string in the body is the shape that produces an accident
the day somebody's client sends the wrong one.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request

import fleet
import permissions
import permissions_web
import refusals
import settings
import wipe


def create_wipe_blueprint(db_path, login_required, access):
    """Build the wipe Blueprint. `login_required` (app.py's session gate) and `access` (the
    permission-group layer) are passed in for the usual two reasons: no circular import, and
    one source of truth per gate."""
    bp = Blueprint("wipe", __name__)

    def _payload(machine):
        return {
            "machine": machine,
            "history": wipe.history(db_path, machine),
            "last_wipe": wipe.last_wipe(db_path, machine),
            "can_wipe": access.can(permissions.WIPE_DEVICE),
        }

    def _issue(machine, action, params, actor, reset_protection=False, reason=""):
        """Record the intent, queue the command, attach the two together.

        **In that order.** A wipe that is issued and then fails to be recorded is the one
        sequence that cannot be reconstructed afterwards: the device is gone and the trail
        never mentions it. Recording an intent that then failed to queue is the harmless
        direction of the same mistake, and it is visible -- the row has no command id.
        """
        row_id = wipe.record_request(
            db_path, machine=machine, action=action, actor=actor,
            reset_protection=reset_protection, reason=reason)
        # The audit row comes before the command too, and for the same reason. create_command
        # writes its own `issue_command` entry; this one exists so that "who erased this
        # device" is answerable by searching for one action rather than by reading params.
        fleet.audit(db_path, actor=actor, action=f"{action}_device",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"request_id": row_id, "reset_protection": reset_protection,
                            "reason": reason})
        command_id = fleet.create_command(
            db_path, machine=machine, command_type=wipe.COMMAND_FOR[action], params=params,
            issued_by=actor,
            ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"))
        wipe.attach_command(db_path, row_id, command_id)
        return command_id

    # ---------------- Read ----------------
    @bp.route("/api/wipe/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_history(machine):
        """What has been asked of this device.

        `view`, not `wipe_device`: a machine that stopped reporting because somebody erased it
        looks exactly like one with a flat battery, and this row is the only thing that can
        tell an operator which it was. Withholding it from readers would leave them
        investigating a device that no longer exists.
        """
        return jsonify(_payload(str(machine).strip())), 200

    # ---------------- Lock ----------------
    @bp.route("/api/wipe/machines/<machine>/lock", methods=["POST"])
    @login_required
    @access.require_machine(permissions.WIPE_DEVICE)
    def lock_machine(machine):
        """Lock the screen now.

        **No typed confirmation, deliberately.** A lock is undone by the person holding the
        device with their own PIN a moment later, and the case it exists for -- a phone left in
        a taxi -- is one where the seconds spent typing a machine name are the wrong trade.
        Friction belongs on the action that cannot be undone, and spreading it over both is how
        people learn to type past it.

        Answers **202 with the queued command**, never a confirmation that the screen is
        locked: the device has to be reachable and claim the command, which never happens at
        all for a phone that is switched off.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        machine = str(machine).strip()
        actor = permissions_web.current_actor()
        body = request.get_json(silent=True) or {}
        try:
            command_id = _issue(machine, wipe.ACTION_LOCK, {}, actor,
                                reason=body.get("reason") or "")
        except (wipe.WipeRefused, ValueError) as e:
            # Covers fleet.UnsupportedCommand -- a device that has told us it cannot lock --
            # and a bad machine name. Both are 400 with the reason attached.
            return refusals.refuse(e)
        payload = _payload(machine)
        payload["command_id"] = command_id
        return jsonify(payload), 202

    # ---------------- Wipe ----------------
    @bp.route("/api/wipe/machines/<machine>/wipe", methods=["POST"])
    @login_required
    @access.require_machine(permissions.WIPE_DEVICE)
    def wipe_machine(machine):
        """Erase the device. There is no undo and no dry run.

        The body must carry `confirm` equal to the machine's name, exactly. That check is in
        wipe.confirm_wipe rather than here so it is one function with one test, and it is on
        the server rather than in the console so that a script cannot skip it.

        `reset_protection` defaults to TRUE -- clear Android's factory-reset protection as part
        of the erase. That is the right default for company-owned hardware, where leaving it on
        produces a device nobody can set up again without the account of whoever last used it.
        It is the wrong default for a stolen personal device, where the protection is the
        point, so it is a per-request answer and the console says which one it is sending.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        machine = str(machine).strip()
        actor = permissions_web.current_actor()
        body = request.get_json(silent=True) or {}

        try:
            machine = wipe.confirm_wipe(machine, body.get("confirm"))
        except wipe.WipeRefused as e:
            # 400 with the sentence, and nothing has been recorded or queued. A refused
            # confirmation is somebody being careful, not an incident.
            return refusals.refuse(e)

        reset_protection = bool(body.get("reset_protection", True))
        try:
            command_id = _issue(machine, wipe.ACTION_WIPE,
                                {"reset_protection": reset_protection}, actor,
                                reset_protection=reset_protection,
                                reason=body.get("reason") or "")
        except (wipe.WipeRefused, ValueError) as e:
            return refusals.refuse(e)

        payload = _payload(machine)
        payload["command_id"] = command_id
        return jsonify(payload), 202

    return bp
