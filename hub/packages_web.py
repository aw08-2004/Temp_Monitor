"""Flask HTTP surface for package deployment -- a thin layer over packages.py, in the
same shape as fleet_web.py and permissions_web.py.

Three audiences, three gates:

  * **Console, package administration** (`/api/packages/...`) -- gated on the
    `deploy_packages` capability. Defining a package is not machine-scoped: a package is
    a recipe, not a machine, and an operator who can define one still can't aim it
    anywhere outside their scope. That check happens at deployment time, per target.

  * **Console, deployments** (`/api/deployments/...`) -- gated on `deploy_packages` AND,
    for every target machine, on that machine being in the operator's scope. The scope
    check runs BEFORE anything is written, and refuses the whole request if any single
    target is out of reach rather than silently dropping it: a deploy that quietly
    installs on nine of the ten machines you asked for is worse than one that fails.
    Reads are filtered the other way -- an operator sees only the target rows they could
    have created, so a fleet-wide deploy doesn't leak the Hospital hostnames to HR.

  * **Agent, payload download** (`/api/agent/packages/<sha256>`) -- bearer agent auth,
    same as the rest of `/api/agent/*`. The digest must belong to a real package source,
    so this is not a general file host that happens to sit behind agent auth.

UPLOADS ARE THE ONE PLACE THIS FILE DEPARTS FROM THE JSON-BODY RULE. Everywhere else,
reading the body with request.get_json(silent=True) is load-bearing CSRF protection --
see fleet_web.py's module docstring, which applies here verbatim. A file upload cannot
be JSON, so `POST /api/packages/upload` accepts multipart/form-data, which IS a
CORS-safelisted content type an HTML form can produce cross-site. That endpoint is
therefore deliberately inert on its own: it stores a blob and returns its hash, creating
no package and touching no machine. Turning a hash into something that runs anywhere
requires the JSON-bodied create/deploy endpoints below, which a cross-site form cannot
reach. Keep it that way -- do not let the upload endpoint create a package as a
convenience.
"""
import os

from flask import Blueprint, jsonify, render_template, request, send_file, session

import fleet
import packages
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


def create_packages_blueprint(db_path, log_dir, login_required, access, hub_url=""):
    """Build the packages Blueprint.

    `log_dir` is where the blob store lives (beside the database -- see
    packages.blob_root); `hub_url` is the public base the agent's download URL is built
    from. Both are passed in rather than imported from app.py, to avoid a circular import
    and because the test suite re-points them.
    """
    bp = Blueprint("packages", __name__)
    can_deploy = access.require(permissions.DEPLOY_PACKAGES)
    blob_dir = packages.blob_root(log_dir)

    def _current_email():
        """The signed-in operator. ALWAYS from the session -- never a request body, or
        the audit trail becomes fiction."""
        return (session.get("user") or {}).get("email", "unknown")

    def _scoped_targets(machines):
        """Validate every requested target against the caller's scope.

        Returns (targets, error, status) -- all-or-nothing on purpose, see the module
        docstring. An empty selection is a 400 (malformed request); an out-of-scope
        machine is a 403 (well-formed, refused).
        """
        names = [str(m or "").strip() for m in (machines or [])]
        names = [n for n in names if n]
        if not names:
            return None, "Select at least one machine.", 400
        denied = [n for n in names if not access.in_scope(n)]
        if denied:
            return None, (f"You do not have access to {denied[0]!r}"
                          + (f" and {len(denied) - 1} other machine(s)."
                             if len(denied) > 1 else ".")), 403
        return names, None, 200

    # ---------------- Pages ----------------
    @bp.route("/packages")
    @login_required
    @can_deploy
    def packages_page():
        return render_template("packages.html")

    # ---------------- Console: packages ----------------
    @bp.route("/api/packages", methods=["GET"])
    @login_required
    @can_deploy
    def list_packages():
        return jsonify({
            "packages": packages.list_packages(db_path),
            # The form renders itself from these, so a new detection kind or source kind
            # is one edit in packages.py -- the same self-describing-API discipline as
            # /api/permissions/capabilities.
            "detection_kinds": [
                {"name": kind,
                 "label": packages.DETECTION_LABELS[kind][0],
                 "description": packages.DETECTION_LABELS[kind][1]}
                for kind in packages.DETECTION_KINDS
            ],
            "source_kinds": list(packages.SOURCE_KINDS),
            "registry_roots": list(packages.REGISTRY_ROOTS),
            "file_placeholder": packages.FILE_PLACEHOLDER,
            "max_upload_mb": settings.get_int(db_path, "deploy.max_upload_mb"),
            "defaults": {
                "success_exit_codes": list(packages.DEFAULT_SUCCESS_EXIT_CODES),
                "max_attempts": settings.get_int(db_path, "deploy.default_max_attempts"),
                "retry_backoff_seconds": settings.get_int(
                    db_path, "deploy.default_retry_backoff_seconds"),
            },
        }), 200

    @bp.route("/api/packages/upload", methods=["POST"])
    @login_required
    @can_deploy
    def upload_package_file():
        """Store an installer and return its sha256. Creates NOTHING -- see the module
        docstring on why this endpoint is deliberately inert.

        The hash is computed from the bytes as they are written, so what the agent later
        verifies is what the hub actually holds, not what an uploader claimed.
        """
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "No file was uploaded."}), 400
        max_bytes = settings.get_int(db_path, "deploy.max_upload_mb") * 1024 * 1024
        try:
            sha256, size = packages.store_blob(blob_dir, upload.stream, max_bytes)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not store the file: {e}"}), 500
        fleet.audit(db_path, actor=_current_email(), action="upload_package_file",
                    target=os.path.basename(upload.filename),
                    detail={"sha256": sha256, "bytes": size})
        return jsonify({
            "sha256": sha256,
            "file_size": size,
            # basename only: a browser may send a path, and it is echoed straight back
            # into the package form.
            "file_name": os.path.basename(upload.filename),
        }), 201

    @bp.route("/api/packages", methods=["POST"])
    @login_required
    @can_deploy
    def create_package():
        data = request.get_json(silent=True) or {}
        try:
            package_id = packages.create_package(
                db_path,
                name=data.get("name"),
                description=data.get("description"),
                version=data.get("version"),
                source=data.get("source") or {},
                install_command=data.get("install_command"),
                install_args=data.get("install_args"),
                timeout_seconds=data.get("timeout_seconds", 900),
                success_exit_codes=data.get("success_exit_codes"),
                detection=data.get("detection"),
                actor=_current_email(),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(packages.get_package(db_path, package_id)), 201

    @bp.route("/api/packages/<package_id>", methods=["GET"])
    @login_required
    @can_deploy
    def get_package(package_id):
        package = packages.get_package(db_path, package_id)
        if package is None:
            return jsonify({"error": "unknown package"}), 404
        return jsonify(package), 200

    @bp.route("/api/packages/<package_id>", methods=["PUT"])
    @login_required
    @can_deploy
    def update_package(package_id):
        data = request.get_json(silent=True) or {}
        try:
            package = packages.update_package(
                db_path, package_id,
                name=data.get("name"),
                description=data.get("description"),
                version=data.get("version"),
                source=data.get("source"),
                install_command=data.get("install_command"),
                install_args=data.get("install_args"),
                timeout_seconds=data.get("timeout_seconds"),
                success_exit_codes=data.get("success_exit_codes"),
                detection=data.get("detection"),
                actor=_current_email(),
                blob_root_dir=blob_dir,
            )
        except KeyError:
            return jsonify({"error": "unknown package"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(package), 200

    @bp.route("/api/packages/<package_id>", methods=["DELETE"])
    @login_required
    @can_deploy
    def delete_package(package_id):
        try:
            packages.delete_package(db_path, package_id, actor=_current_email(),
                                    blob_root_dir=blob_dir)
        except KeyError:
            return jsonify({"error": "unknown package"}), 404
        return jsonify({"status": "deleted"}), 200

    # ---------------- Console: deployments ----------------
    @bp.route("/api/deployments", methods=["GET"])
    @login_required
    @can_deploy
    def list_deployments():
        machine = (request.args.get("machine") or "").strip() or None
        if machine and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        return jsonify({"deployments": packages.list_deployments(
            db_path, machine=machine)}), 200

    @bp.route("/api/deployments", methods=["POST"])
    @login_required
    @can_deploy
    def create_deployment():
        data = request.get_json(silent=True) or {}
        targets, error, status = _scoped_targets(data.get("machines"))
        if error:
            return jsonify({"error": error}), status
        try:
            deployment_id = packages.create_deployment(
                db_path,
                package_id=data.get("package_id"),
                machines=targets,
                created_by=_current_email(),
                note=data.get("note"),
                window_start=data.get("window_start"),
                window_end=data.get("window_end"),
                max_attempts=data.get("max_attempts") or settings.get_int(
                    db_path, "deploy.default_max_attempts"),
                retry_backoff_seconds=data.get("retry_backoff_seconds") or
                settings.get_int(db_path, "deploy.default_retry_backoff_seconds"),
            )
        except KeyError:
            return jsonify({"error": "unknown package"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(_visible_deployment(deployment_id)), 201

    def _visible_deployment(deployment_id):
        """One deployment with its target rows narrowed to the caller's scope."""
        deployment = packages.get_deployment(db_path, deployment_id)
        if deployment is None:
            return None
        deployment["targets"] = access.filter_rows(deployment["targets"])
        return deployment

    @bp.route("/api/deployments/<deployment_id>", methods=["GET"])
    @login_required
    @can_deploy
    def get_deployment(deployment_id):
        deployment = _visible_deployment(deployment_id)
        if deployment is None:
            return jsonify({"error": "unknown deployment"}), 404
        return jsonify(deployment), 200

    @bp.route("/api/deployments/<deployment_id>/cancel", methods=["POST"])
    @login_required
    @can_deploy
    def cancel_deployment(deployment_id):
        try:
            packages.cancel_deployment(db_path, deployment_id, actor=_current_email())
        except KeyError:
            return jsonify({"error": "unknown deployment"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(_visible_deployment(deployment_id)), 200

    @bp.route("/api/deployments/<deployment_id>/retry", methods=["POST"])
    @login_required
    @can_deploy
    def retry_deployment(deployment_id):
        try:
            requeued = packages.retry_deployment_failures(
                db_path, deployment_id, actor=_current_email())
        except KeyError:
            return jsonify({"error": "unknown deployment"}), 404
        deployment = _visible_deployment(deployment_id)
        deployment["requeued"] = requeued
        return jsonify(deployment), 200

    # ---------------- Agent: payload download ----------------
    @bp.route("/api/agent/packages/<sha256>", methods=["GET"])
    def agent_download_package(sha256):
        """Serve a hub-hosted payload to an enrolled agent.

        Auth is the agent bearer token, the same boundary that already lets this machine
        be handed a `run_script` command -- an agent that can be told to run arbitrary
        code as SYSTEM is not additionally endangered by being able to fetch an installer
        the hub is holding. The digest must belong to a package source, which is what
        keeps this from being a general-purpose read primitive over the blob directory.

        No range support: installers are fetched once by a service with a retry loop, and
        partial-content handling is a whole class of path/offset bugs for no gain here.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        try:
            digest = packages.normalize_sha256(sha256)
        except ValueError:
            return jsonify({"error": "not found"}), 404
        if not digest or packages.package_id_for_blob(db_path, digest) is None:
            return jsonify({"error": "not found"}), 404
        path = packages.blob_path(blob_dir, digest)
        if not os.path.exists(path):
            # The row survives but the file is gone -- a half-restored backup, or a
            # manual tidy-up of the blob directory. Say so rather than 404ing, because
            # the fix is on the hub, not the agent.
            return jsonify({"error": "package payload is missing on the hub"}), 410
        return send_file(path, as_attachment=True, download_name=digest,
                         mimetype="application/octet-stream")

    return bp
