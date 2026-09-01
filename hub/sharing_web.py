"""Flask surface for cross-hub machine sharing (roadmap #15) -- a thin layer over sharing.py.

Three audiences, three gates, in the shape `packages_web.py` and `apitokens_web.py` already
use. The unusual one is the middle: this is the only file in the hub where the caller is
**another hub**.

  * **A peer hub, using what it was lent** (`/api/peer/...`) -- the peer bearer token, and
    nothing else. No session, no cookie, and every route re-decides the grant per request
    through `sharing.authorize_peer_action`. There is deliberately no endpoint here that
    answers a question without naming a share: a peer holds no capabilities of its own.

  * **A peer hub, redeeming a pairing code** (`/api/peer/pair`) -- deliberately
    UNAUTHENTICATED, because the code is the credential and the far hub has nothing else
    yet. Safe for the three reasons `sharing.py` enforces: the code is single-use (claimed
    by a conditional DELETE, so a race has one winner), it expires, and it only exists
    because a signed-in operator here generated it.

  * **This hub's console**, managing both directions (`/api/sharing/...`) -- the ordinary
    session gate plus a capability.

**Who may do what, on the console side.** Two different questions, and they get two
different answers:

  * *Which hubs exist* -- pairing a peer, unpairing one, adding or removing an outbound
    link -- is `manage_permission_groups`. Deciding that another organisation's console is
    allowed to talk to this one at all is the grant perimeter, which is exactly what that
    capability names.
  * *Which machine is lent, and how much of it* is gated the way `apitokens_web.py` gates a
    device pairing: the operator must hold each ticked capability over that machine
    themselves, checked here AND intersected live on every later request. Sharing is
    delegation, so the ceiling is what the operator could already do. A `view`-only operator
    can share nothing but `view`, and only for machines in their own scope.

**The borrowed side has no machine scope, and cannot have one.** A borrowed machine is not
in this hub's fleet by construction -- no enrollment row, no `machine_info`, excluded from
every count -- so no permission group can name it and `access.in_scope` would refuse every
one of them. The gate on the borrowed routes is therefore the capability alone. That is a
real widening compared with a local machine, and it is stated here rather than buried: every
operator holding `view` sees every borrowed machine. The narrowing that does apply is the
one that matters -- the OWNING hub re-decides each request against a grant this hub cannot
edit, so the worst a local over-permission does is let somebody ask a question hub A will
answer anyway.

**Outbound calls never follow a redirect.** `_default_peer_call` passes
`allow_redirects=False`, because the request carries a bearer token for one specific hub: a
302 from a peer that has been misconfigured -- or taken over -- would otherwise hand that
credential to whatever host the Location header named. Combined with
`sharing.normalize_peer_url` refusing anything but https, the token has exactly one
destination and no way to acquire a second.

**What a peer is shown of a machine is a PROJECTION, not this hub's asset record.**
`_borrowed_view` names the fields that travel, and the exclusions are the point: no serial,
asset tag or service tag, and nothing from Active Directory. Those identify the machine
inside THIS organisation's systems, and a colleague who was lent a screen has no business
with them. A field added to the machine record later does not silently start travelling.
"""
import functools
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, render_template, request

import backups
import fleet
import permissions
import permissions_web
import refusals
import settings
import sharing

#: What a peer holding `issue_commands` may queue. An ALLOW-LIST, matching the module's
#: general shape and tighter than the console's own door (`fleet_web.fleet_issue_command`,
#: which names the types it refuses). The five here are the ones that mean something on
#: their own; everything else in `fleet.ALL_COMMANDS` either carries a hub-side row a peer
#: cannot mint (a deployment id, a pty session, a wake relay, a file transfer), renames a
#: machine inside THIS hub's inventory, or writes firmware -- the one action with no restore
#: path, which is not something to hand across an organisational boundary through a generic
#: command channel.
PEER_COMMANDS = frozenset({
    "restart",
    "shutdown",
    "gpupdate",
    "run_script",
    "install_app",
})

#: How long an outbound call to a peer may take. Short: every one of them happens while an
#: operator is looking at a page, and a peer that has gone away should badge as unreachable
#: within a few seconds rather than hanging the console on a TCP timeout.
PEER_TIMEOUT_SECONDS = 15


def secret_id_for(link_id):
    """Where a peer link's token lives in the master-key-wrapped store.

    Namespaced like `bios.secret_id_for` so the one file can hold backup destination
    credentials, BIOS setup passwords and peer tokens without any of them being able to
    collide with another kind of secret. The id is also the AEAD's associated data, so a
    blob copied from one link's entry to another fails to decrypt rather than quietly
    authenticating against the wrong hub.
    """
    return f"share-link:{link_id}"


def peer_actor(peer, body=None):
    """Who to record as having done this, from the owning hub's point of view.

    Two names, and the format keeps them apart because they are not the same kind of fact.
    The peer id is something this hub verified -- it authenticated that token. The
    operator's address is a CLAIM by the far hub, which this hub cannot check and must not
    launder into an identity: `peer:<id>/<claimed>` reads as borrowed, where a bare address
    would read like a local operator's.

    The peer ID and not its label, even though the label is what a human would rather read.
    A label is a display name an admin can edit; this string is an IDENTITY, it goes into
    `commands.issued_by` and `remote_sessions.issued_by`, and both of those are what scopes
    a peer's read back to its own rows. An identity that changes when somebody renames a row
    is not one. The label rides in the audit detail beside it, which is where a human is
    looking anyway.

    Module-level, like `peer_gate` and for the same reason -- see there.
    """
    claimed = permissions.normalize_email((body or {}).get("operator"))
    stem = f"peer:{peer['peer_id']}"
    return f"{stem}/{claimed}" if claimed else stem


def peer_owns(row, peer):
    """Is this row one the given peer created? Used to scope a peer's reads to its own work.

    Matches the exact stem or the stem plus an operator, never a bare prefix: without the
    separator, a peer id that happened to be another's prefix would match. Peer ids are
    full-length hex so that cannot arise today, and a check that only works because of that
    is one that breaks quietly if ids ever get shorter.
    """
    stem = f"peer:{peer['peer_id']}"
    value = str((row or {}).get("issued_by") or "")
    return value == stem or value.startswith(stem + "/")


def peer_gate(db_path, access, capability=None):
    """A decorator that authenticates a peer token, and optionally resolves one share.

    Module-level rather than closed over `create_sharing_blueprint`, because `remote_web.py`
    needs the same gate: a peer starting a remote session is authorised by sharing.py and
    carried out by remote.py, and the ICE/TURN minting and stream-parameter validation that
    a session start needs all live over there. Splitting the gate out is what lets those
    routes sit beside the console and agent halves of the same feature instead of growing a
    second copy of remote_web's closure here.

    Without `capability`, the wrapped view is called as `view(peer, ...)`. With one, the
    view's `<share_id>` is resolved first and it is called as `view(peer, share_state, ...)`.

    The disabled case answers 403 with a reason while a bad token answers 401 with none.
    That asymmetry is deliberate: "this hub does not do cross-hub sharing" is a
    configuration fact its own operator already knows and the far operator needs to be told,
    whereas which tokens are valid is not something an unauthenticated caller gets to learn
    anything about.
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not settings.get_bool(db_path, "sharing.enabled"):
                return jsonify({"error": "Cross-hub sharing is switched off on this "
                                         "hub."}), 403
            peer = sharing.authenticate_peer(
                db_path, request.headers.get("Authorization"))
            if peer is None:
                return jsonify({"error": "Not authorized."}), 401
            if capability is None:
                return view(peer, *args, **kwargs)
            # Read fresh from the tables every request, so a revocation takes effect on the
            # very next one with no cache to invalidate.
            state, error = sharing.authorize_peer_action(
                db_path, peer, kwargs.pop("share_id", None), capability,
                superusers=access.superusers)
            if error:
                return jsonify({"error": error}), 403
            return view(peer, state, *args, **kwargs)
        return wrapped
    return decorator


def _default_peer_call(method, url, token=None, payload=None,
                       timeout=PEER_TIMEOUT_SECONDS):
    """One HTTP call to a peer hub. Returns (status, body-dict).

    Redirects are NOT followed -- see the module docstring; this is the guard that keeps a
    bearer token pointed at exactly the host the operator typed.

    A transport failure comes back as a 502 with the reason rather than an exception, so the
    caller has one shape to handle and the console can say "the owner hub did not answer"
    instead of rendering a 500.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, url, json=payload, headers=headers,
                                    timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        return 502, {"error": f"The peer hub did not answer: {exc}"}
    if response.is_redirect:
        return 502, {"error": "The peer hub answered with a redirect, which is refused: a "
                              "peer token has exactly one destination."}
    try:
        body = response.json()
    except ValueError:
        body = {"error": "The peer hub answered with something that was not JSON."}
    return response.status_code, (body if isinstance(body, dict) else {"data": body})


def create_sharing_blueprint(db_path, log_dir, login_required, access,
                             machine_roster=None, machine_detail=None, peer_call=None):
    """Build the sharing Blueprint.

    `machine_roster` and `machine_detail` are injected for the same reason the backup and
    wake blueprints take a roster: this file must not learn how to enumerate the fleet or
    serialise a machine, and app.py already owns both. `peer_call` is injected so the tests
    can stand in for another hub without a network.
    """
    bp = Blueprint("sharing", __name__)
    can_view = access.require(permissions.VIEW)
    can_command = access.require(permissions.ISSUE_COMMANDS)
    can_remote = access.require(permissions.REMOTE_CONTROL)
    manage_peers = access.require(permissions.MANAGE_PERMISSION_GROUPS)
    call_peer = peer_call or _default_peer_call

    def _actor():
        return permissions_web.current_actor()

    def _enabled():
        return settings.get_bool(db_path, "sharing.enabled")

    def _hub_label():
        return settings.get(db_path, "sharing.hub_label") or ""

    def _peer_lifetime_days():
        return settings.get_int(db_path, "sharing.peer_lifetime_days") \
            or sharing.DEFAULT_PEER_LIFETIME_DAYS

    def _roster_map():
        """The fleet roster as {machine: row}. app.py hands back a list because that is
        what its own schedulers want; sharing.catalogue_for_peer wants a lookup."""
        rows = machine_roster() if machine_roster else []
        return {row["machine"]: row for row in rows}

    # ================================
    # PEER-FACING (this hub is the OWNER)
    # ================================
    peer_auth = peer_gate(db_path, access)

    def peer_share(capability):
        return peer_gate(db_path, access, capability)

    def _peer_actor(peer, body=None):
        return peer_actor(peer, body)

    def _peer_audit(peer, body, action, target, detail=None):
        payload = dict(detail or {})
        payload["peer_id"] = peer["peer_id"]
        payload["peer_label"] = peer.get("label") or ""
        # Named `claimed_operator`, never `operator`. See _peer_actor.
        payload["claimed_operator"] = permissions.normalize_email(
            (body or {}).get("operator")) or None
        fleet.audit(db_path, actor=_peer_actor(peer, body), action=action,
                    level=fleet.LEVEL_SECURITY, target=target, detail=payload)

    @bp.route("/api/peer/pair", methods=["POST"])
    def peer_pair():
        """Pairing code -> peer token, once. No session and no capability.

        Gated on `sharing.enabled` like every other peer route: a hub with sharing switched
        off should not be mintable into a peer relationship by somebody holding a code from
        before it was switched off.
        """
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off on this "
                                     "hub."}), 403
        body = request.get_json(silent=True) or {}
        try:
            token, peer = sharing.redeem_pairing(
                db_path, body.get("code"), peer_label=body.get("hub_label"),
                lifetime_days=_peer_lifetime_days())
        except sharing.SharingError as e:
            return refusals.refuse(e)
        return jsonify({
            # The only time this value exists outside the borrowing hub. It is not stored
            # here in plaintext, and there is no endpoint that can show it again.
            "token": token,
            "peer_id": peer["peer_id"],
            "expires_at": peer["expires_at"],
            # So the borrowing hub can label the link with what we call ourselves rather
            # than with a hostname.
            "hub_label": _hub_label(),
        }), 200

    @bp.route("/api/peer/catalogue", methods=["GET"])
    @peer_auth
    def peer_catalogue(peer):
        """Everything this peer is currently lent. The borrowing hub's whole world."""
        return jsonify({
            "hub_label": _hub_label(),
            "machines": sharing.catalogue_for_peer(
                db_path, peer, superusers=access.superusers, roster=_roster_map()),
        }), 200

    @bp.route("/api/peer/shares/<share_id>/machine", methods=["GET"])
    @peer_share(permissions.VIEW)
    def peer_machine(peer, state):
        """One borrowed machine's telemetry, proxied live.

        Nothing is mirrored into the borrowing hub -- see sharing.py's docstring. This is
        the read that makes that possible, and `_borrowed_view` is what decides how much of
        the machine record travels.
        """
        detail = machine_detail(state["machine"]) if machine_detail else None
        if detail is None:
            return jsonify({"error": "That machine has not reported to this hub."}), 404
        return jsonify(_borrowed_view(detail, state)), 200

    @bp.route("/api/peer/shares/<share_id>/commands", methods=["POST"])
    @peer_share(permissions.ISSUE_COMMANDS)
    def peer_command(peer, state):
        """Queue one command on THIS hub's queue, to THIS hub's agent.

        The agent is never told the borrowing hub exists: it claims this from the same
        queue as every locally-issued command, over its own bearer token, from its one home
        hub. That is the whole security argument for the feature and it is why there is no
        code here that talks to an agent.
        """
        body = request.get_json(silent=True) or {}
        kind = str(body.get("type") or "").strip()
        if kind not in PEER_COMMANDS:
            return jsonify({
                "error": f"A shared machine does not accept '{kind}'. This share can run: "
                         f"{', '.join(sorted(PEER_COMMANDS))}."}), 400
        try:
            command_id = fleet.create_command(
                db_path, machine=state["machine"], command_type=kind,
                params=body.get("params") or {},
                issued_by=_peer_actor(peer, body),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return refusals.refuse(e)
        # A second row beside the `issue_command` one fleet.create_command writes, for the
        # same reason bios_settings_change gets one: that row records the command, this one
        # records that it came from another hub and on whose claimed say-so. An auditor
        # asking "what did the borrowed access actually do" should not have to know the
        # shape of an issued_by string to find out.
        _peer_audit(peer, body, "share.action", state["machine"],
                    {"share_id": state["share_id"], "command_id": command_id,
                     "type": kind})
        return jsonify({"command_id": command_id}), 201

    @bp.route("/api/peer/shares/<share_id>/commands/<command_id>", methods=["GET"])
    @peer_share(permissions.ISSUE_COMMANDS)
    def peer_command_status(peer, state, command_id):
        """How a borrowed command went.

        Scoped to the share's machine AND to commands THIS PEER issued. The second half is
        the one that matters: the machine's queue also carries commands a local operator
        issued, and serving those would hand a borrowing hub their address, their script
        bodies and their output. `issued_by` is the key because `_peer_actor` writes a
        stable, peer-scoped identity into it -- see there.

        The answer is a projection rather than the row, for the same reason `_borrowed_view`
        is: a command row grows fields over time, and none of them should start travelling
        because somebody added a column.
        """
        command = fleet.get_command(db_path, command_id)
        stem = _peer_actor(peer)
        issued_by = str((command or {}).get("issued_by") or "")
        if (command is None
                or command.get("machine") != state["machine"]
                or not (issued_by == stem or issued_by.startswith(stem + "/"))):
            return jsonify({"error": "No such command."}), 404
        result = command.get("result") or None
        return jsonify({
            "id": command.get("id"),
            "type": command.get("type"),
            "params": command.get("params"),
            "status": command.get("status"),
            "created_at": command.get("created_at"),
            "result": None if result is None else {
                "success": result.get("success"),
                "output": result.get("output"),
                "completed_at": result.get("completed_at"),
            },
        }), 200

    # ================================
    # CONSOLE -- THE LENDING HALF
    # ================================
    @bp.route("/api/sharing/pairings", methods=["POST"])
    @login_required
    @manage_peers
    def create_pairing():
        """Generate a one-time code for a colleague to paste into their hub.

        Answered once, in plaintext, and stored only as a hash. Nothing is paired yet: an
        abandoned code leaves no credential behind, which is why this is a separate call
        from the redemption at `/api/peer/pair`.
        """
        body = request.get_json(silent=True) or {}
        try:
            code = sharing.create_pairing(db_path, body.get("label"), _actor())
        except sharing.SharingError as e:
            return refusals.refuse(e)
        return jsonify({"code": code,
                        "expires_in": sharing.PAIRING_CODE_TTL_SECONDS}), 201

    @bp.route("/api/sharing/peers", methods=["GET"])
    @login_required
    @manage_peers
    def list_peers():
        """The hubs this one lends to, each with the shares it holds."""
        peers = sharing.list_peers(db_path)
        by_peer = {}
        for share in sharing.list_shares(db_path):
            by_peer.setdefault(share["peer_id"], []).append(
                sharing.share_state(db_path, share, superusers=access.superusers))
        for peer in peers:
            peer["shares"] = by_peer.get(peer["peer_id"], [])
        return jsonify({"peers": peers, "enabled": _enabled()}), 200

    @bp.route("/api/sharing/peers/<peer_id>", methods=["DELETE"])
    @login_required
    @manage_peers
    def revoke_peer(peer_id):
        """Unpair a hub, taking every share it holds with it.

        Sessions in flight die too: `_end_borrowed_sessions` is what makes revocation
        immediate rather than merely refusing the next request, which is the difference
        between ending a remote session and letting it run until somebody closes a tab.
        """
        machines = [s["machine"] for s in sharing.list_shares(db_path, peer_id=peer_id)]
        revoked = sharing.revoke_peer(db_path, peer_id, actor=_actor())
        if revoked is None:
            return jsonify({"error": "No such peer hub."}), 404
        _end_borrowed_sessions(machines, f"peer {peer_id} unpaired")
        return jsonify({"revoked": peer_id, "shares_revoked": revoked}), 200

    def _may_share(machine, capabilities):
        """Refuse an over-grant, naming what is missing.

        The same rule `apitokens_web.pair_confirm` applies to a device, for the same
        reason: the live intersection would strip these at every later request, and a share
        listing capabilities that never work is worse than one refused at creation.
        """
        if not access.in_scope(machine):
            return "You do not have access to that machine."
        mine = set(access.current().get("capabilities") or ())
        missing = [c for c in capabilities if c not in mine]
        if missing:
            return ("You cannot share a capability you do not hold on that machine: "
                    f"{', '.join(missing)}.")
        return None

    @bp.route("/api/sharing/shares", methods=["GET"])
    @login_required
    @can_view
    def list_shares():
        """Every machine this hub is lending, narrowed to the caller's own scope.

        Filtered as a READ, not just as a write -- an operator who cannot see a machine
        should not learn from this page that it exists, which is the same rule the machine
        list and the history endpoints follow.
        """
        rows = [sharing.share_state(db_path, s, superusers=access.superusers)
                for s in access.filter_rows(sharing.list_shares(db_path))]
        labels = {p["peer_id"]: (p["label"] or p["peer_label"])
                  for p in sharing.list_peers(db_path)}
        for row in rows:
            row["peer_label"] = labels.get(row["peer_id"], "")
        return jsonify({"shares": rows,
                        "shareable": list(sharing.SHAREABLE_CAPABILITIES),
                        "defaults": list(sharing.DEFAULT_SHARE_CAPABILITIES)}), 200

    @bp.route("/api/sharing/shares", methods=["POST"])
    @login_required
    @can_view
    def create_share():
        """Lend one machine to one already-paired hub.

        No capability of its own beyond `view` plus holding what is being shared -- see the
        module docstring. Which hubs exist at all is the administrative decision, and it was
        already made by whoever paired this peer.
        """
        body = request.get_json(silent=True) or {}
        try:
            capabilities = sharing.normalize_share_capabilities(
                body.get("capabilities"))
        except sharing.SharingError as e:
            return refusals.refuse(e)

        machine = permissions.normalize_machine(body.get("machine"))
        denied = _may_share(machine, capabilities)
        if denied:
            return jsonify({"error": denied}), 403
        try:
            share = sharing.create_share(
                db_path, body.get("peer_id"), machine, capabilities,
                created_by=access.email(), expires_at=body.get("expires_at"))
        except sharing.SharingError as e:
            return refusals.refuse(e)
        return jsonify(sharing.share_state(db_path, share,
                                           superusers=access.superusers)), 201

    @bp.route("/api/sharing/shares/<share_id>", methods=["PUT"])
    @login_required
    @can_view
    def update_share(share_id):
        """Widen, narrow, or re-date a live share."""
        share = sharing.get_share(db_path, share_id)
        if share is None or share["revoked"]:
            return jsonify({"error": "No such share."}), 404

        body = request.get_json(silent=True) or {}
        capabilities = body.get("capabilities")
        if capabilities is not None:
            try:
                capabilities = sharing.normalize_share_capabilities(capabilities)
            except sharing.SharingError as e:
                return refusals.refuse(e)
        # Checked against what is being asked for, plus what the share already carries: an
        # operator who cannot hold `remote_control` must not be able to keep a share alive
        # that grants it by editing only its expiry.
        denied = _may_share(share["machine"],
                            capabilities if capabilities is not None
                            else share["capabilities"])
        if denied:
            return jsonify({"error": denied}), 403

        expires_at = body["expires_at"] if "expires_at" in body else ...
        try:
            updated = sharing.update_share(db_path, share_id, capabilities=capabilities,
                                           expires_at=expires_at, actor=_actor())
        except sharing.SharingError as e:
            return refusals.refuse(e)
        if updated is None:
            return jsonify({"error": "No such share."}), 404
        # A narrowing must reach a session that is already open, not just the next request.
        if capabilities is not None and permissions.REMOTE_CONTROL not in capabilities:
            _end_borrowed_sessions([share["machine"]], "share narrowed")
        return jsonify(sharing.share_state(db_path, updated,
                                           superusers=access.superusers)), 200

    @bp.route("/api/sharing/shares/<share_id>", methods=["DELETE"])
    @login_required
    @can_view
    def revoke_share(share_id):
        """Stop lending one machine, now.

        Out-of-scope and non-existent answer alike, so this is not a way to discover which
        machines somebody else is lending.
        """
        share = sharing.get_share(db_path, share_id)
        if share is None or share["revoked"] or not access.in_scope(share["machine"]):
            return jsonify({"error": "No such share."}), 404
        sharing.revoke_share(db_path, share_id, actor=_actor())
        _end_borrowed_sessions([share["machine"]], "share revoked")
        return jsonify({"revoked": share_id}), 200

    def _end_borrowed_sessions(machines, reason):
        """Kill remote sessions a revocation has just invalidated.

        Revocation "must kill sessions in flight, not merely refuse the next request" -- the
        row change alone does the second, and this is the first. Imported here rather than
        at module scope so that sharing_web does not drag remote.py into every import of it;
        the coupling is one function deep and only on this path.
        """
        import remote
        for machine in set(machines or ()):
            for session in remote.list_sessions(db_path, machine=machine,
                                                active_only=True):
                remote.end_session(db_path, session["id"], reason, actor="sharing")

    # ================================
    # CONSOLE -- THE BORROWING HALF
    # ================================
    def _link_token(link_id):
        """This link's peer token, or None if the store cannot be opened.

        None rather than an exception for a missing master key: the console must still be
        able to LIST and REMOVE a link whose secret is unreadable, and a page that 500s is
        a page nobody can use to clean up after a key rotation.
        """
        master = backups.load_master_key()
        if master is None or not backups.has_secret(log_dir, secret_id_for(link_id)):
            return None
        try:
            return (backups.load_secret(log_dir, master,
                                        secret_id_for(link_id)) or {}).get("token")
        except ValueError:
            return None

    @bp.route("/api/sharing/links", methods=["GET"])
    @login_required
    @manage_peers
    def list_links():
        links = sharing.list_links(db_path)
        for link in links:
            # Whether the credential is READABLE, never the credential. A link whose secret
            # was lost to a key rotation looks identical to a working one otherwise, and
            # "remove it and pair again" is the only fix -- so the page has to be able to
            # say which it is.
            link["has_token"] = _link_token(link["link_id"]) is not None
            link["machines"] = len(sharing.list_borrowed(db_path, link["link_id"]))
        return jsonify({"links": links, "enabled": _enabled()}), 200

    @bp.route("/api/sharing/links", methods=["POST"])
    @login_required
    @manage_peers
    def create_link():
        """Redeem a colleague's pairing code and keep the token it mints.

        The order here is load-bearing. The master key is checked BEFORE the code is
        redeemed, because redemption is single-use: discovering afterwards that there is
        nowhere to put the token would burn the code and leave the operator asking for
        another one, with a live peer already paired on the far hub and nothing here to
        show for it.
        """
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off. Turn it on in "
                                     "Settings first."}), 403
        body = request.get_json(silent=True) or {}
        try:
            base_url = sharing.normalize_peer_url(body.get("base_url"))
        except sharing.SharingError as e:
            return refusals.refuse(e)
        code = str(body.get("code") or "").strip()
        if not code:
            return jsonify({"error": "Paste the pairing code the other hub gave you."}), 400

        master = backups.load_master_key()
        if master is None:
            return jsonify({
                "error": "No BACKUP_MASTER_KEY is configured, so the peer hub's token "
                         "cannot be stored encrypted. Generate one on the Backups page "
                         "first."}), 400

        status, answer = call_peer("POST", f"{base_url}/api/peer/pair", payload={
            "code": code, "hub_label": _hub_label()})
        if status != 200 or not answer.get("token"):
            return jsonify({"error": answer.get("error")
                            or "The peer hub refused the pairing code."}), \
                (502 if status >= 500 else 400)

        try:
            link = sharing.create_link(db_path, base_url,
                                       body.get("label") or answer.get("hub_label"),
                                       created_by=access.email(),
                                       peer_id=answer.get("peer_id"))
        except sharing.SharingError as e:
            return refusals.refuse(e)
        backups.store_secret(log_dir, master, secret_id_for(link["link_id"]),
                             {"token": answer["token"]})
        _refresh_link(link)
        return jsonify(link), 201

    @bp.route("/api/sharing/links/<link_id>", methods=["DELETE"])
    @login_required
    @manage_peers
    def delete_link(link_id):
        """Forget a peer hub: the link, its cached machines, and its token.

        This does NOT unpair on the far side -- it cannot, and pretending otherwise would be
        worse than saying so. The owning hub's operator revokes their end; this end simply
        stops holding a credential. The console says as much.
        """
        if not sharing.delete_link(db_path, link_id, actor=_actor()):
            return jsonify({"error": "No such peer link."}), 404
        backups.delete_secret(log_dir, secret_id_for(link_id))
        return jsonify({"removed": link_id}), 200

    def _refresh_link(link):
        """Read one peer's catalogue and replace what we cache for it. Returns an error
        string, or None.

        Every outcome is recorded on the link (`record_link_result`), because "the machine
        is gone" and "the owner hub is unreachable" look identical on a page that only shows
        what it has.
        """
        token = _link_token(link["link_id"])
        if token is None:
            error = "This link has no readable token. Remove it and pair again."
            sharing.record_link_result(db_path, link["link_id"], ok=False, error=error)
            return error

        status, answer = call_peer("GET", f"{link['base_url']}/api/peer/catalogue",
                                   token=token)
        if status != 200:
            error = answer.get("error") or f"The peer hub answered {status}."
            sharing.record_link_result(db_path, link["link_id"], ok=False, error=error)
            return error

        added, removed = sharing.replace_borrowed(
            db_path, link["link_id"], answer.get("machines") or [])
        sharing.record_link_result(db_path, link["link_id"], ok=True)
        if added or removed:
            # So a machine disappearing from this console has a reason somebody can find --
            # see fleet.ACTION_LEVELS for why this one is notice rather than security.
            fleet.audit(db_path, actor="system", action="share.catalogue_change",
                        level=fleet.LEVEL_NOTICE, target=link["base_url"],
                        detail={"link_id": link["link_id"], "added": added,
                                "removed": removed})
        return None

    @bp.route("/api/sharing/links/<link_id>/refresh", methods=["POST"])
    @login_required
    @can_view
    def refresh_link(link_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        link = sharing.get_link(db_path, link_id)
        if link is None:
            return jsonify({"error": "No such peer link."}), 404
        error = _refresh_link(link)
        if error:
            return jsonify({"error": error}), 502
        return jsonify({"machines": sharing.list_borrowed(db_path, link_id)}), 200

    @bp.route("/api/sharing/borrowed", methods=["GET"])
    @login_required
    @can_view
    def list_borrowed():
        """Every machine another hub is lending us, with the link each came through.

        No machine-scope filter, and there cannot be one -- see the module docstring.
        """
        labels = {l["link_id"]: (l["label"] or l["base_url"])
                  for l in sharing.list_links(db_path)}
        rows = sharing.list_borrowed(db_path)
        for row in rows:
            row["peer_label"] = labels.get(row["link_id"], "")
        return jsonify({"machines": rows, "enabled": _enabled()}), 200

    def _borrowed_call(link_id, share_id, capability, method, path, payload=None):
        """One proxied request to the hub that owns a borrowed machine.

        The local capability check has already happened at the route. This adds the two
        things a proxy owes the operator: a cached-share check so an obviously-dead share
        does not cost a round trip, and a refusal that NAMES the owner hub -- "hub A did not
        answer" is actionable where "request failed" is not.
        """
        link = sharing.get_link(db_path, link_id)
        borrowed = sharing.get_borrowed(db_path, link_id, share_id)
        if link is None or borrowed is None:
            return 404, {"error": "No such borrowed machine."}
        if not sharing.borrowed_can(borrowed, capability):
            # The cache's answer, which is advisory -- the owner hub decides for real. It is
            # checked first anyway because it is the one that can explain itself: it knows
            # the share is view-only, where hub A would only say no.
            return 403, {"error": f"{link['label'] or link['base_url']} has not shared "
                                  f"'{capability}' on this machine."}
        token = _link_token(link_id)
        if token is None:
            return 502, {"error": "This peer link has no readable token. Remove it and "
                                  "pair again."}
        # Quoted, not interpolated raw. `share_id` and the ids inside `path` arrive
        # from this hub's own URL, where Flask has already percent-DECODED them -- so a
        # request for a share id spelled `abc%3Fx=1` would otherwise reach the peer with
        # a query string this hub never intended to send. The ids are hex in practice;
        # a guard that only holds because of that is one that breaks quietly the day it
        # stops being true.
        url = f"{link['base_url']}/api/peer/shares/{quote(str(share_id), safe='')}{path}"
        status, answer = call_peer(method, url, token=token, payload=payload)
        if status >= 400:
            sharing.record_link_result(db_path, link_id, ok=False,
                                       error=answer.get("error") or f"HTTP {status}")
        return status, answer

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/machine", methods=["GET"])
    @login_required
    @can_view
    def borrowed_machine(link_id, share_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        status, answer = _borrowed_call(link_id, share_id, permissions.VIEW,
                                        "GET", "/machine")
        return jsonify(answer), status

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/commands", methods=["POST"])
    @login_required
    @can_command
    def borrowed_command(link_id, share_id):
        """Ask the owning hub to run something on a machine it lent us.

        `operator` rides in the body so hub A's trail can name who asked. It is a claim --
        hub A cannot verify it and records it as `claimed_operator` -- but this hub's own
        audit row below is the one that stands behind it, which is why both trails exist and
        why hub A's is the authoritative one for what actually happened to the machine.
        """
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        body = request.get_json(silent=True) or {}
        status, answer = _borrowed_call(
            link_id, share_id, permissions.ISSUE_COMMANDS, "POST", "/commands",
            payload={"type": body.get("type"), "params": body.get("params") or {},
                     "operator": access.email()})
        if status < 400:
            fleet.audit(db_path, actor=_actor(), action="share.borrowed_action",
                        level=fleet.LEVEL_SECURITY, target=share_id,
                        detail={"link_id": link_id, "type": body.get("type"),
                                "command_id": answer.get("command_id")})
        return jsonify(answer), status

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/commands/<command_id>",
              methods=["GET"])
    @login_required
    @can_command
    def borrowed_command_status(link_id, share_id, command_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        status, answer = _borrowed_call(link_id, share_id, permissions.ISSUE_COMMANDS,
                                        "GET", f"/commands/{quote(str(command_id), safe='')}")
        return jsonify(answer), status

    # ---------------- Remote view/control on a borrowed machine ----------------
    # Four thin proxies. The viewer in the browser speaks to THIS hub, this hub speaks to the
    # owning hub, and the owning hub relays to its own agent -- so the signaling for a
    # borrowed session takes two hops instead of one. That is affordable because signaling is
    # a small burst at setup and then almost nothing: once ICE completes the media goes
    # peer-to-peer or through a TURN relay and never touches either hub again.
    #
    # There is deliberately no virtual-display proxy: see remote_web.py's peer plane for why
    # installing a display driver is not something a share hands over.
    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/remote", methods=["POST"])
    @login_required
    @can_remote
    def borrowed_remote_start(link_id, share_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        body = request.get_json(silent=True) or {}
        payload = dict(body)
        payload["operator"] = access.email()
        status, answer = _borrowed_call(link_id, share_id, permissions.REMOTE_CONTROL,
                                        "POST", "/remote", payload=payload)
        if status < 400:
            fleet.audit(db_path, actor=_actor(), action="share.borrowed_action",
                        level=fleet.LEVEL_SECURITY, target=share_id,
                        detail={"link_id": link_id, "type": "start_remote_session",
                                "session_id": answer.get("session_id")})
        return jsonify(answer), status

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/remote/<session_id>/signal",
              methods=["POST"])
    @login_required
    @can_remote
    def borrowed_remote_signal(link_id, share_id, session_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        body = request.get_json(silent=True) or {}
        status, answer = _borrowed_call(
            link_id, share_id, permissions.REMOTE_CONTROL, "POST",
            f"/remote/{quote(str(session_id), safe='')}/signal",
            payload={"kind": body.get("kind"), "payload": body.get("payload")})
        return jsonify(answer), status

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/remote/<session_id>/poll",
              methods=["GET"])
    @login_required
    @can_remote
    def borrowed_remote_poll(link_id, share_id, session_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        try:
            after_seq = int(request.args.get("after_seq", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        status, answer = _borrowed_call(
            link_id, share_id, permissions.REMOTE_CONTROL, "GET",
            f"/remote/{quote(str(session_id), safe='')}/poll?after_seq={after_seq}")
        return jsonify(answer), status

    @bp.route("/api/sharing/borrowed/<link_id>/<share_id>/remote/<session_id>/stop",
              methods=["POST"])
    @login_required
    @can_remote
    def borrowed_remote_stop(link_id, share_id, session_id):
        if not _enabled():
            return jsonify({"error": "Cross-hub sharing is switched off."}), 403
        status, answer = _borrowed_call(link_id, share_id, permissions.REMOTE_CONTROL,
                                        "POST", f"/remote/{quote(str(session_id), safe='')}/stop")
        return jsonify(answer), status

    # ================================
    # PAGE
    # ================================
    @bp.route("/sharing")
    @login_required
    @can_view
    def sharing_page():
        """Both halves on one page: what this hub lends, and what it borrows.

        One page rather than two because they are one relationship seen from either end, and
        an operator asking "what does Bob's hub have of ours" is usually about to ask "and
        what do we have of theirs".
        """
        return render_template("sharing.html",
                               can_manage_peers=access.can(
                                   permissions.MANAGE_PERMISSION_GROUPS))

    return bp


#: What travels to a borrowing hub about a machine, and nothing else.
#:
#: The exclusions are the design. `serial_number`, `asset_tag` and `service_tag` identify the
#: machine inside THIS organisation's asset system; `ad_ou`, `ad_dn`, `ad_owner` and the rest
#: name a person and a place in a directory the borrower has no relationship with. A
#: colleague who was lent a screen has no business with any of it, and an allow-list means a
#: field added to the machine record later does not silently start travelling.
BORROWED_FIELDS = (
    "machine", "manufacturer", "model", "os", "os_caption", "os_version", "os_build",
    "os_arch", "status", "updated_at", "uptime_seconds", "temp", "diagnostics",
)


def _borrowed_view(detail, state):
    """One machine as its borrower sees it: the projection above, plus the grant.

    Kept at module scope, and pure, so the projection can be asserted in a test without a
    request context -- this is the function that decides what crosses an organisational
    boundary, and it should be readable on its own.
    """
    view = {field: detail.get(field) for field in BORROWED_FIELDS}
    view["share_id"] = state["share_id"]
    view["capabilities"] = state["granted"]
    view["borrowed"] = True
    return view
