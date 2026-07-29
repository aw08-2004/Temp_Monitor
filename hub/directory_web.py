"""Flask surface for Active Directory sync (roadmap #4).

Three things the console needs and directory.py deliberately does not provide, because it
stays Flask-free:

  * **status** -- is it on, is ldap3 installed, when did it last succeed, what did it
    find. The last-success time is the one that matters: a sync that quietly stopped
    working looks exactly like an AD nobody has changed.
  * **sync now** -- run a pass immediately instead of waiting up to an hour. This is what
    makes the settings form usable at all; otherwise "did I get the bind DN right?" takes
    an hour to answer, per attempt.
  * **the OU list** -- what the permission-group scope picker offers, so an admin picks an
    OU their machines are actually in rather than pasting a DN and hoping.

Gating: status and the OU list are `manage_permission_groups` OR `manage_settings` -- both
of those administer this, one to scope a group by OU and one to configure the sync, and
requiring the wrong one would make a page unusable for the person who owns it. Running a
sync is `manage_settings` alone: it binds to a domain controller with the service
account, which is a configuration action, not a group-editing one.

The CSRF note in fleet_web.py's docstring applies: bodies are read with
request.get_json(silent=True), so a form post cannot reach these.
"""
from flask import Blueprint, jsonify

import directory
import permissions
import settings


def create_directory_blueprint(db_path, login_required, access):
    bp = Blueprint("directory", __name__)

    def _may_read():
        return (access.can(permissions.MANAGE_PERMISSION_GROUPS)
                or access.can(permissions.MANAGE_SETTINGS))

    @bp.route("/api/directory/status", methods=["GET"])
    @login_required
    def directory_status():
        if not _may_read():
            return jsonify({"error": "You do not have permission to view this."}), 403
        last = directory.last_run(db_path)
        success = directory.last_success(db_path)
        return jsonify({
            "enabled": settings.get_bool(db_path, "directory.enabled"),
            # Reported rather than assumed, so "I turned it on and nothing happened" has
            # an answer on screen instead of only in the service log.
            "library_installed": directory.ldap3_installed(),
            "library_hint": directory.LDAP_IMPORT_HINT,
            "server": settings.get(db_path, "directory.server"),
            "base_dn": settings.get(db_path, "directory.base_dn"),
            # Whether the credential exists, never the credential. The console must be
            # able to say "you haven't set the password" without being able to read it.
            "bind_password_set": bool(directory.bind_password()),
            "bind_password_env": directory.BIND_PASSWORD_ENV,
            "last_run": last,
            "last_success": success,
            "ou_count": len(directory.known_ous(db_path)),
        }), 200

    @bp.route("/api/directory/ous", methods=["GET"])
    @login_required
    def directory_ous():
        if not _may_read():
            return jsonify({"error": "You do not have permission to view this."}), 403
        # The OUs the fleet is actually in, not the whole directory tree. An admin
        # scoping a group cares about where their managed machines sit; offering every OU
        # in the domain would bury those in containers with nothing to grant.
        return jsonify({"ous": directory.known_ous(db_path)}), 200

    @bp.route("/api/directory/sync", methods=["POST"])
    @login_required
    @access.require(permissions.MANAGE_SETTINGS)
    def directory_sync_now():
        """Run a pass now, synchronously, and return what it found.

        Synchronous on purpose: the caller pressed this to find out whether their
        configuration works, and a 202 "started" would hand them back exactly the silence
        they pressed the button to escape. The LDAP timeout bounds how long it can take.
        """
        if not settings.get_bool(db_path, "directory.enabled"):
            return jsonify({"error": "Active Directory sync is turned off. Enable it in "
                                     "Settings first."}), 400
        try:
            result = directory.sync_once(
                db_path, directory.config_from_settings(db_path),
                on_change=permissions.invalidate)
        except directory.DirectoryError as e:
            # 400, not 500: every one of these is a configuration problem the operator can
            # act on, and the message is written to be read by them.
            return jsonify({"error": str(e)}), 400
        return jsonify(result), 200

    return bp
