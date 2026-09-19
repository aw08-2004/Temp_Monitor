"""Flask HTTP surface for event log mining (roadmap #16) -- a thin layer over events.py,
in the same shape as patches_web.py and wake_web.py.

**The read/write split is the whole authorization story, and it follows the line this hub
has drawn six times already.** Reading what a machine's event log said is `view` plus
machine scope, like its disks, its BIOS version and its available updates: knowing that a PC
logged forty failed logons overnight is not a privilege, and putting it behind a management
capability would mean the people most likely to notice a problem are the ones who cannot see
it. Deciding what the fleet *collects* is `manage_settings`.

**Why `manage_settings` rather than a capability of its own.** A subscription is collection
configuration, in exactly the sense `metrics.collect_gpu` is -- it changes what every managed
machine reads and reports, and it acts on no machine. The two capabilities this was weighed
against are recorded in ROADMAP.MD with the reasons: `manage_rules`, rejected because a
subscription issues nothing and changes nothing on a PC, and a new `manage_event_logs`,
rejected because it would ship switched off for everybody and the first symptom would be a
feature that appears to do nothing.

**Scope is applied to reads and NOT to subscriptions, and that asymmetry is deliberate.**
A subscription names no machine -- it is fleet-wide by design (see events.py) -- so there is
no scope to check on it, exactly as patch approvals have none. What it can reach is bounded
where it is spent: on the read, per machine, against the operator's own scope.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json, and that is
what stops a cross-site form POST from reconfiguring fleet-wide collection.

The agent never talks to this module. Matches arrive on the ordinary heartbeat and the
subscription document goes back on its reply (fleet_web.py), so there is no `/api/agent/*`
surface here to get wrong.
"""
from flask import Blueprint, jsonify, render_template, request

import events
import fleet
import i18n
import permissions
import permissions_web
import refusals
import settings


def _lang():
    """The language of the request in flight; English outside one. See i18n.current."""
    return i18n.current()


def _vocabulary():
    """The self-describing half of the API: levels and common channels, with their words.

    Same discipline as /api/permissions/capabilities and patches' classifications -- the
    console builds its filters from this, so a level added to events.LEVELS shows up in the
    UI with its own words and no JS change, and one added without catalog entries fails
    tests/test_i18n.py rather than captioning a filter with its own slug.
    """
    return {
        "levels": [
            {"name": slug,
             "label": i18n.translate(f"{events.LEVEL_TEXT_KEY}.{slug}", _lang())}
            for slug in events.LEVELS
        ],
        # Channel names are Windows' own and are NOT translated -- "Security" is the literal
        # string the log is called on the machine, and a German console showing "Sicherheit"
        # would name a channel that does not exist. Same call `settings.choice_label` makes
        # for a destination id somebody named.
        "common_logs": list(events.COMMON_LOGS),
        "max_event_ids": events.MAX_EVENT_IDS,
        "max_subscriptions": events.MAX_SUBSCRIPTIONS,
        "rollup_window_seconds": events.ROLLUP_WINDOW_SECONDS,
    }


def create_events_blueprint(db_path, login_required, access):
    bp = Blueprint("events", __name__)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_SETTINGS)

    def _actor():
        return permissions_web.current_actor()

    def _body():
        return request.get_json(silent=True) or {}

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _read_scope():
        """The machine list a scoped read is narrowed to, or None if unrestricted.

        None and [] mean different things all the way down into events.list_events: None is
        "no scope", [] is "a scope that matched nothing" and must return nothing. Collapsing
        the two is how a fleet-wide list gets shown to somebody entitled to none of it.
        """
        if access.machine_filter() is None:
            return None
        return access.filter_machines(events.known_machines(db_path))

    def _filters():
        """The query string, as events.list_events' keywords. Unknown values are dropped
        rather than refused: a stale bookmark carrying a level that no longer exists should
        show the page, not a 400."""
        args = request.args
        levels = [slug for slug in args.getlist("level") if slug in events.LEVELS]
        return {
            "levels": levels or None,
            "log": (args.get("log") or "").strip() or None,
            "event_id": args.get("event_id") or None,
            "search": (args.get("q") or "").strip() or None,
            "limit": args.get("limit", 200),
        }

    # ---------------- Pages ----------------
    @bp.route("/events")
    @login_required
    @can_view
    def events_page():
        return render_template("events.html")

    # ---------------- Console: what came back (view + scope) ----------------
    @bp.route("/api/events", methods=["GET"])
    @login_required
    @can_view
    def events_overview():
        """The page's whole first paint: rows, counters, subscriptions and vocabulary.

        One endpoint rather than the four the page would otherwise fan out to, for the reason
        the fleet summary gives -- the counters have to agree with the list they sit above,
        and four requests cannot be made to agree on a moment.

        `subscriptions` is included for an operator who cannot edit them, on purpose: "why is
        this page empty" is answered by the subscription list far more often than by the
        event list, and hiding it would leave a viewer with no way to see that nothing is
        being collected.
        """
        scope = _read_scope()
        window = settings.get_int(db_path, "events.summary_window_seconds")
        return jsonify({
            "events": events.list_events(db_path, machines=scope, **_filters()),
            "summary": events.summary(db_path, machines=scope, window_seconds=window),
            "subscriptions": events.list_subscriptions(db_path),
            "vocabulary": _vocabulary(),
            "can_manage": access.can(permissions.MANAGE_SETTINGS),
            "retention_days": settings.get_int(db_path, "data.event_retention_days"),
        }), 200

    @bp.route("/api/events/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_events(machine):
        """One machine's events, plus whether its agent is reporting at all.

        `state` is the half of this answer that is easy to leave out and expensive to miss: a
        machine with no rows is either behaving or not collecting, and only the state row
        tells them apart. See events.machine_state.
        """
        return jsonify({
            "machine": machine,
            "events": events.list_events(db_path, machine=machine, **_filters()),
            "state": events.machine_state(db_path, machine),
        }), 200

    # ---------------- Console: what to collect (manage_settings) ----------------
    @bp.route("/api/events/subscriptions", methods=["GET"])
    @login_required
    @can_view
    def list_subscriptions():
        return jsonify({"subscriptions": events.list_subscriptions(db_path),
                        "can_manage": access.can(permissions.MANAGE_SETTINGS)}), 200

    @bp.route("/api/events/subscriptions", methods=["POST"])
    @login_required
    @can_manage
    def create_subscription():
        """Add a subscription. Audited at NOTICE, like every other fleet-wide change.

        Not LEVEL_SECURITY, though this can be pointed at the Security channel: what is
        recorded here is a decision about what the fleet collects, which is configuration.
        The security-relevant act would be reading somebody's logon history, and that is the
        read above -- gated on machine scope, and where a widening of the audit level belongs
        if one is ever wanted.
        """
        bad = _require_json()
        if bad:
            return bad
        body = _body()
        try:
            subscription = events.create_subscription(
                db_path,
                name=body.get("name"),
                log=body.get("log"),
                event_ids=body.get("event_ids"),
                levels=body.get("levels"),
                provider=body.get("provider", ""),
                enabled=body.get("enabled", True),
                created_by=_actor(),
            )
        except events.SubscriptionRejected as e:
            return refusals.refuse(e)
        fleet.audit(db_path, actor=_actor(), action="event_subscription_create",
                    level=fleet.LEVEL_NOTICE, target=subscription["log"],
                    detail={"id": subscription["id"], "name": subscription["name"],
                            "event_ids": subscription["event_ids"],
                            "levels": subscription["levels"]})
        return jsonify(subscription), 201

    @bp.route("/api/events/subscriptions/<subscription_id>", methods=["PATCH"])
    @login_required
    @can_manage
    def update_subscription(subscription_id):
        bad = _require_json()
        if bad:
            return bad
        try:
            subscription = events.update_subscription(db_path, subscription_id, **_body())
        except events.SubscriptionRejected as e:
            return refusals.refuse(e)
        fleet.audit(db_path, actor=_actor(), action="event_subscription_update",
                    level=fleet.LEVEL_NOTICE, target=subscription["log"],
                    detail={"id": subscription["id"], "name": subscription["name"],
                            "enabled": subscription["enabled"]})
        return jsonify(subscription), 200

    @bp.route("/api/events/subscriptions/<subscription_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_subscription(subscription_id):
        """Remove a subscription. The events it collected stay -- see events.delete_subscription."""
        if not events.delete_subscription(db_path, subscription_id):
            return jsonify({"error": "That subscription no longer exists."}), 404
        fleet.audit(db_path, actor=_actor(), action="event_subscription_delete",
                    level=fleet.LEVEL_NOTICE, target=subscription_id, detail={})
        return jsonify({"status": "deleted"}), 200

    return bp
