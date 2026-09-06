"""Flask surface for invite links (roadmap #22) -- the admin API behind the Invites page,
plus the one unauthenticated route on this hub.

**The admin routes are gated on MANAGE_PERMISSION_GROUPS, not MANAGE_USERS.** An invite
hands out capabilities, so creating one is permission-group administration; `manage_users`
is documented, in the README and in users.py's own docstring, as a profile directory that
grants nothing. Gating invites on it would have turned the weakest admin capability into
the strongest one.

**`GET /invite/<code>` is deliberately not behind login_required**, and it is the only
route here that is not. It has to be reachable by someone who by definition cannot sign in
yet -- that is the entire feature. Two consequences are load-bearing:

  * It answers with only what invites.preview returns: the label, who sent it, and the
    names of the groups it grants. Not the capabilities, not the machine scope, not the
    seat count, not the other redeemers. An unauthenticated page on a fleet console is a
    reconnaissance surface unless someone decides, in writing, what it may say.
  * It records the code in the SESSION rather than carrying it through the OAuth round
    trip in a query parameter. A `?invite=` on the callback would put a credential in
    every referrer header, proxy log and browser history entry between here and the
    identity provider. app.py's `_complete_login` reads it back out.

**Creating an invite with a custom capability set creates a real permission group.** The
invite itself only ever stores group ids -- see invites.py's docstring for why one
authorization model was worth the extra step. The group is created here, in the route,
rather than in the model, so the model never has to know that `permissions.create_group`
exists and the ceiling check has exactly one shape to enforce.

The CSRF note in permissions_web.py's docstring applies here verbatim and for the same
reason: these endpoints grant capabilities. Bodies are read with request.get_json(silent=
True), which requires Content-Type: application/json. Do not add force=True and do not
accept a form-encoded fallback.
"""
from flask import Blueprint, jsonify, render_template, request, session

import invites
import permissions
import permissions_web
import refusals

#: Where app.py's `_complete_login` looks for a code the invitee arrived with. Named here,
#: beside the route that writes it, because a session key spelled two ways is a feature that
#: works on the page that sets it and nowhere else.
SESSION_KEY = "pending_invite"


def create_invites_blueprint(db_path, login_required, access, hub_url=""):
    """Build the invites Blueprint.

    `hub_url` is the hub's public origin -- the same value the OAuth callback is anchored
    to. The link is assembled here rather than in the browser from `location.origin`
    because an admin reaching the console through an internal hostname would otherwise copy
    a link that only works from inside, and would have no way to tell.
    """
    bp = Blueprint("invites", __name__)
    manage = access.require(permissions.MANAGE_PERMISSION_GROUPS)

    def _link(code):
        return f"{str(hub_url or '').rstrip('/')}/invite/{code}"

    # ------------------------------------------------------------------ admin page
    @bp.route("/invites", methods=["GET"])
    @login_required
    @manage
    def invites_page():
        return render_template("invites.html")

    @bp.route("/api/invites", methods=["GET"])
    @login_required
    @manage
    def list_invites():
        return jsonify({"invites": invites.list_invites(db_path),
                        "groups": [{"id": g["id"], "name": g["name"]}
                                   for g in permissions.list_groups(db_path)]}), 200

    @bp.route("/api/invites", methods=["POST"])
    @login_required
    @manage
    def create_invite():
        """Create an invite and return its link, once.

        `new_group` is the "build a custom one" half of the form: a name, capabilities and
        a machine scope, which become a real permission group whose id the invite then
        names. It is created BEFORE the invite so that a refusal from create_group (a
        duplicate name, an unknown capability) refuses the whole request rather than
        leaving an invite pointing at a group that was never made.
        """
        data = request.get_json(silent=True) or {}
        actor = permissions_web.current_actor()
        group_ids = list(data.get("group_ids") or [])
        new_group = data.get("new_group")

        created_group_id = None
        try:
            if new_group:
                created_group_id = permissions.create_group(
                    db_path,
                    name=new_group.get("name"),
                    description=new_group.get("description"),
                    capabilities=new_group.get("capabilities"),
                    machines=new_group.get("machines") or [],
                    ous=new_group.get("ous") or [],
                    scope_mode=new_group.get("scope_mode") or permissions.SCOPE_LIST,
                    actor=actor,
                )
                group_ids.append(created_group_id)

            invite_id, code = invites.create_invite(
                db_path,
                label=data.get("label"),
                group_ids=group_ids,
                pinned_emails=data.get("pinned_emails") or [],
                max_uses=data.get("max_uses", 1),
                # Absent means the form's default; an explicit null means "never expires",
                # which is why this reads the key rather than using `or`.
                ttl_days=(data.get("ttl_days", invites.DEFAULT_TTL_DAYS)),
                actor=actor,
                # The ceiling. Passed from the live request rather than re-resolved inside
                # the model, so it is the caller's own permissions -- narrowed to their
                # device, if this came from one -- that bound what they can hand out.
                creator_permissions=access.current(),
            )
        except (invites.InviteError, ValueError) as e:
            # An invite that was refused must not leave a permission group behind. The
            # group was created a few lines up specifically for this invite, so nobody
            # else can be relying on it yet.
            if created_group_id is not None:
                try:
                    permissions.delete_group(db_path, created_group_id, actor=actor)
                except KeyError:
                    pass
            return refusals.refuse(e)

        invite = invites.get_invite(db_path, invite_id)
        # The only response that ever carries the code. Every later read of this invite
        # returns the row without it -- the plaintext is not stored and cannot be shown
        # again, and the page says so.
        invite["link"] = _link(code)
        return jsonify(invite), 201

    @bp.route("/api/invites/<invite_id>/revoke", methods=["POST"])
    @login_required
    @manage
    def revoke_invite(invite_id):
        try:
            invites.revoke_invite(db_path, invite_id,
                                  actor=permissions_web.current_actor())
        except KeyError:
            return jsonify({"error": "unknown invite"}), 404
        return jsonify(invites.get_invite(db_path, invite_id)), 200

    @bp.route("/api/invites/<invite_id>", methods=["DELETE"])
    @login_required
    @manage
    def delete_invite(invite_id):
        try:
            invites.delete_invite(db_path, invite_id,
                                  actor=permissions_web.current_actor())
        except KeyError:
            return jsonify({"error": "unknown invite"}), 404
        return jsonify({"status": "deleted"}), 200

    # ------------------------------------------------------------------ public landing
    @bp.route("/invite/<code>", methods=["GET"])
    def invite_landing(code):
        """The page somebody holding an invite link sees. **No login gate** -- see the
        module docstring.

        A bad, spent, expired or revoked code renders the same template with the reason and
        no sign-in button, rather than a 404: the invitee needs to know whether to ask for
        a new link or to have mistyped one, and a 404 tells them neither. Nothing about the
        hub beyond the refusal sentence is rendered in that case.
        """
        try:
            preview = invites.preview(db_path, code)
        except invites.InviteError as e:
            session.pop(SESSION_KEY, None)
            return render_template("invite.html", invite=None, error=str(e)), 404

        # The code travels in the signed session cookie, not in the OAuth redirect -- see
        # the module docstring. It is popped by _complete_login on the way back.
        session[SESSION_KEY] = code
        return render_template("invite.html", invite=preview, error=None)

    return bp
