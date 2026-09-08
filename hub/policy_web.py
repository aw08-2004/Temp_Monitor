"""Flask HTTP surface for app policy (roadmap #23 phase D) -- a thin layer over policy.py,
registered as a Blueprint from app.py.

**Two gates, and the split is the one the rest of the console draws.** Reading a policy, and
reading how a device is complying with it, is `view` (plus machine scope for anything naming a
machine): knowing that an app is blocked on a device you administer is not a privilege. WRITING
one is **`manage_device_policy`** -- a new capability rather than a reuse of `manage_settings`,
because a policy is not a setting. It is a standing instruction that changes what a person's
device will do, applied without anybody present, which is precisely the argument `manage_rules`
already makes for itself.

**The preview is not a convenience.** `POST /api/policy/preview` answers "which packages would
this suspend, on which machines, and how many are actually installed" before anything is
stored. An app policy is the first thing in this product that can make a device less useful to
the person holding it, and the gap between "block TikTok" and "block fourteen apps on ninety
phones" is exactly where somebody notices they picked the wrong list.

**Nothing here pushes.** A saved policy reaches devices on their own next heartbeat, through the
`device_policy` block, because that channel already exists and is already per-machine. An
immediate-push endpoint would be a second delivery path with its own failure modes for a feature
whose latency budget is ten seconds.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, render_template, request

import apps
import fleet
import permissions
import permissions_web
import policy
import refusals


def create_policy_blueprint(db_path, login_required, access):
    """Build the policy Blueprint. `login_required` (app.py's session gate) and `access` (the
    permission-group layer) are passed in for the usual two reasons."""
    bp = Blueprint("policy", __name__)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_DEVICE_POLICY)

    def _body():
        return request.get_json(silent=True) or {}

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _visible(machines):
        return [m for m in machines if access.in_scope(m)]

    def _policy_view(row):
        """One policy, with its machine list narrowed to what the caller can see.

        The machine list is scoped and the packages are not, deliberately: a package name is
        not information about a machine, while the list of machines a policy covers is. An
        operator scoped to three phones sees the policy, sees what it blocks, and sees their
        three -- which is enough to understand why an app on their device will not open.
        """
        view = dict(row)
        view["machines"] = _visible(row["machines"])
        view["machines_hidden"] = len(row["machines"]) - len(view["machines"])
        return view

    # ---------------- Pages ----------------
    @bp.route("/policy", methods=["GET"])
    @login_required
    @can_view
    def policy_page():
        """The policy editor. Gated on `view` like the data behind it, not on the write
        capability: a reader who cannot author one can still see which policies exist and why
        an app on a device they administer will not open. The page hides its own write
        controls from them, and every write endpoint re-decides."""
        return render_template("policy.html")

    # ---------------- Read ----------------
    @bp.route("/api/policy/apps", methods=["GET"])
    @login_required
    @can_view
    def list_policies():
        return jsonify({
            "policies": [_policy_view(p) for p in policy.list_policies(db_path)],
            "can_manage": access.can(permissions.MANAGE_DEVICE_POLICY),
            # Served rather than hardcoded in the console: the console must be able to grey out
            # a package an operator cannot block, and a second copy of this list in JavaScript
            # would drift from the one that actually decides.
            "protected_prefixes": list(policy.NEVER_SUSPEND_PREFIXES),
        }), 200

    @bp.route("/api/policy/apps/<policy_id>", methods=["GET"])
    @login_required
    @can_view
    def read_policy(policy_id):
        found = policy.get_policy(db_path, policy_id)
        if found is None:
            return jsonify({"error": "unknown policy"}), 404
        return jsonify(_policy_view(found)), 200

    @bp.route("/api/policy/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_policy(machine):
        """What this device is meant to enforce, what it says it did, and what its inventory
        shows -- the three views compliance is the difference between.

        `require_machine`, because this names one: the answer includes which of somebody's apps
        have been switched off, which is about the person holding the device.
        """
        name = str(machine).strip()
        installed = apps.list_apps(db_path, name)
        payload = policy.compliance(db_path, name, installed)
        payload["machine"] = name
        payload["can_manage"] = access.can(permissions.MANAGE_DEVICE_POLICY)
        return jsonify(payload), 200

    # ---------------- Preview ----------------
    @bp.route("/api/policy/preview", methods=["POST"])
    @login_required
    @can_manage
    def preview():
        """What a draft policy WOULD do, before it is stored.

        Gated on the write capability rather than on `view`: it takes an arbitrary package list
        and answers with which machines have those packages, which is a fleet-wide query
        somebody with read access has no reason to be able to phrase.

        Answers with the resolved effect PER MACHINE rather than a fleet total, because the
        total is the number that reassures and the per-machine list is the one that stops
        somebody. "14 apps" reads as small; "14 apps on this phone, including the camera" does
        not.
        """
        bad = _require_json()
        if bad:
            return bad
        body = _body()
        try:
            parts = policy.validate(body.get("name") or "draft", body.get("mode"),
                                    body.get("packages"), body.get("machines"),
                                    body.get("fleet_wide"))
        except policy.PolicyRejected as e:
            return refusals.refuse(e)

        # Which machines this would land on. A fleet-wide draft is previewed against every
        # machine that has reported an inventory and is in scope -- there is nothing else to
        # preview against, and a preview over machines the caller cannot see would be a fleet
        # listing behind a different door.
        if parts["fleet_wide"]:
            targets = _visible(_machines_with_inventory())
        else:
            targets = _visible(parts["machines"])

        effects = []
        for machine in targets:
            installed = apps.list_apps(db_path, machine)
            names = {a["package"]: a for a in installed}
            if parts["mode"] == policy.MODE_BLOCK:
                would = [p for p in parts["packages"] if p in names]
            else:
                would = [p for p in names if p not in set(parts["packages"])
                         and not policy.is_protected(p)]
            effects.append({
                "machine": machine,
                # Only what is INSTALLED. A policy naming an app a device does not have is the
                # ordinary case for a fleet-wide list, not a failure, and counting it would
                # inflate every number on this screen.
                "would_suspend": sorted(would),
                "labels": {p: names[p]["label"] for p in would if p in names},
                "inventory_known": bool(installed),
            })

        return jsonify({
            "mode": parts["mode"],
            "packages": parts["packages"],
            # Named rather than counted: an operator who ticked the launcher has to be told,
            # and a silent drop is how somebody believes a policy covers something it does not.
            "dropped_protected": parts["dropped"],
            "machines": effects,
            "machines_hidden": (len(parts["machines"]) - len(targets)
                                if not parts["fleet_wide"] else 0),
        }), 200

    # ---------------- Write ----------------
    @bp.route("/api/policy/apps", methods=["POST"])
    @login_required
    @can_manage
    def create():
        bad = _require_json()
        if bad:
            return bad
        body = _body()
        actor = permissions_web.current_actor()
        try:
            policy_id = policy.create_policy(
                db_path, name=body.get("name"), mode=body.get("mode"),
                packages=body.get("packages"), machines=body.get("machines"),
                fleet_wide=body.get("fleet_wide"), enabled=body.get("enabled", True),
                actor=actor)
        except policy.PolicyRejected as e:
            return refusals.refuse(e)
        _audit(actor, "app_policy_create", policy_id)
        return jsonify(_policy_view(policy.get_policy(db_path, policy_id))), 201

    @bp.route("/api/policy/apps/<policy_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update(policy_id):
        bad = _require_json()
        if bad:
            return bad
        body = _body()
        try:
            found = policy.update_policy(
                db_path, policy_id, name=body.get("name"), mode=body.get("mode"),
                packages=body.get("packages"), machines=body.get("machines"),
                fleet_wide=body.get("fleet_wide"), enabled=body.get("enabled", True))
        except policy.PolicyRejected as e:
            return refusals.refuse(e)
        if not found:
            return jsonify({"error": "unknown policy"}), 404
        _audit(permissions_web.current_actor(), "app_policy_update", policy_id)
        return jsonify(_policy_view(policy.get_policy(db_path, policy_id))), 200

    @bp.route("/api/policy/apps/<policy_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove(policy_id):
        if not policy.delete_policy(db_path, policy_id):
            return jsonify({"error": "unknown policy"}), 404
        _audit(permissions_web.current_actor(), "app_policy_delete", policy_id)
        return jsonify({"status": "deleted"}), 200

    def _audit(actor, action, policy_id):
        """Every write, at security level.

        Not because a policy runs code -- it does not -- but because it changes what somebody's
        device will do without them being asked, and "who blocked this, and when" is the
        question that follows. The policy's contents ride along, so the trail answers it
        without the row that was edited still having to exist.
        """
        found = policy.get_policy(db_path, policy_id)
        fleet.audit(db_path, actor=actor, action=action, level=fleet.LEVEL_SECURITY,
                    target=policy_id,
                    detail={"name": (found or {}).get("name", ""),
                            "mode": (found or {}).get("mode", ""),
                            "packages": (found or {}).get("packages", []),
                            "fleet_wide": (found or {}).get("fleet_wide", False),
                            "machines": (found or {}).get("machines", [])})

    def _machines_with_inventory():
        with apps.get_conn(db_path) as conn:
            rows = conn.execute("SELECT DISTINCT machine FROM machine_apps").fetchall()
        return [r["machine"] for r in rows]

    return bp
