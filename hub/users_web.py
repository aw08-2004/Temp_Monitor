"""Flask surface for the Registered Users directory: the admin API behind the Users
page, gated by MANAGE_USERS.

Split out of app.py the same way settings_web.py / permissions_web.py are, over the
Flask-free users.py model.

The CSRF note in permissions_web.py's module docstring applies here verbatim: bodies
are read with request.get_json(silent=True), which requires Content-Type:
application/json -- not CORS-safelisted, so a cross-origin fetch preflights and fails
and an HTML form cannot produce it. Do not add force=True and do not accept a
form-encoded fallback.

Unlike permissions_web.py, nothing here is machine-scoped: a user directory has no
per-machine dimension, so MANAGE_USERS is the whole gate. That capability is
deliberately separate from MANAGE_PERMISSION_GROUPS so the two can be delegated
independently -- editing someone's phone number is not the same trust as granting
capabilities.
"""
from flask import Blueprint, jsonify, render_template, request, session

import permissions
import users


def create_users_blueprint(db_path, login_required, access):
    """CRUD for the registered-users directory. `login_required` is app.py's session
    gate; `access` is the Access instance each route is additionally gated by."""
    bp = Blueprint("users", __name__)
    manage = access.require(permissions.MANAGE_USERS)

    def _actor():
        return permissions.normalize_email((session.get("user") or {}).get("email"))

    @bp.route("/users")
    @login_required
    @manage
    def users_page():
        return render_template("users.html")

    @bp.route("/api/users", methods=["GET"])
    @login_required
    @manage
    def list_users_route():
        return jsonify(users.list_users(db_path, q=request.args.get("q"))), 200

    @bp.route("/api/users", methods=["POST"])
    @login_required
    @manage
    def create_user_route():
        data = request.get_json(silent=True) or {}
        try:
            user = users.create_user(
                db_path,
                email=data.get("email"),
                full_name=data.get("full_name"),
                username=data.get("username"),
                phone=data.get("phone"),
                title=data.get("title"),
                department=data.get("department"),
                notes=data.get("notes"),
                actor=_actor(),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(user), 201

    @bp.route("/api/users/<email>", methods=["GET"])
    @login_required
    @manage
    def get_user_route(email):
        user = users.get_user(db_path, email)
        if user is None:
            return jsonify({"error": "unknown user"}), 404
        return jsonify(user), 200

    @bp.route("/api/users/<email>", methods=["PUT"])
    @login_required
    @manage
    def update_user_route(email):
        data = request.get_json(silent=True) or {}
        try:
            user = users.update_user(
                db_path, email,
                full_name=data.get("full_name"),
                username=data.get("username"),
                phone=data.get("phone"),
                title=data.get("title"),
                department=data.get("department"),
                notes=data.get("notes"),
                actor=_actor(),
            )
        except KeyError:
            return jsonify({"error": "unknown user"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(user), 200

    @bp.route("/api/users/<email>", methods=["DELETE"])
    @login_required
    @manage
    def delete_user_route(email):
        try:
            users.delete_user(db_path, email, actor=_actor())
        except KeyError:
            return jsonify({"error": "unknown user"}), 404
        return jsonify({"status": "deleted"}), 200

    return bp
