"""Flask HTTP surface for webfetch.py -- research, winget lookups and staged installer
downloads, the routes the console assistant (roadmap #26) builds packages with.

**Routes rather than special tools inside the assistant**, on purpose. assistant_tools.py's
whole guarantee is that every tool is an HTTP route replayed as the operator, so the
capability gate, the audit row and the tier table apply to these exactly as to everything
else. A web-fetch branch inside assistant_web.execute() would be the first tool with a code
path of its own, and the first one somebody forgets to gate.

Two gates, both on every route:

  * **`deploy_packages`.** The point of the feature is making packages, and "may make the
    hub fetch things from the internet" should not be wider than "may define what gets
    installed". Not machine-scoped: nothing here names a machine. A staged file reaches one
    only through POST /api/deployments, which does the per-target scope check.
  * **`ai.assistant_web_enabled`, off by default.** A hub that turned the assistant on agreed
    to a model reading the fleet; it did not thereby agree to the hub making outbound requests
    to wherever a model asks. Checked per request, so switching it off is obeyed at once.

Every body is read with `get_json(silent=True)` -- the CSRF argument in fleet_web.py's
docstring applies verbatim. Reading a page is a POST, not a GET, for ai_web.fleet_summary's
reason: a GET is something a third-party page can make the operator's browser send.
"""
import functools
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request

import fleet
import packages
import permissions
import permissions_web
import refusals
import settings
import webfetch

MAX_WAIT_SECONDS = 60
STORE_FAILED = ("The hub could not write the file to its disk; the hub log says why. Check free "
                "space and permissions on the log directory.")
OFF_MESSAGE = ("Web access for the assistant is switched off. An admin can turn it on in "
               "Settings -> AI (Let the assistant use the internet).")


def create_webfetch_blueprint(db_path, log_dir, login_required, access, *, workers=2):
    """Build the webfetch Blueprint. `log_dir` holds the staging folder and, through
    packages.blob_root, the package store a staged file is promoted into."""
    bp = Blueprint("webfetch", __name__)
    can_deploy = access.require(permissions.DEPLOY_PACKAGES)
    staging = webfetch.staging_root(log_dir)
    blob_dir = packages.blob_root(log_dir)
    # Two at a time. A download is a long-lived outbound connection; more in parallel buys an
    # operator nothing and lets one runaway conversation saturate the hub's uplink.
    pool = ThreadPoolExecutor(max_workers=max(1, int(workers)),
                              thread_name_prefix="webfetch")

    def _actor():
        return permissions_web.current_actor()

    def _off():
        return not settings.get(db_path, "ai.assistant_web_enabled")

    def _max_bytes():
        # The package store's own cap, not a second one: a staged file only exists to become
        # a package, and one over that limit would be refused at promote anyway -- after the
        # whole thing had been downloaded.
        return settings.get_int(db_path, "deploy.max_upload_mb") * 1024 * 1024

    def _body():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    def _audit(action, target, detail):
        fleet.audit(db_path, actor=_actor(), action=action, target=str(target or "")[:300],
                    detail=detail)

    def _gate(view):
        """The settings switch, after the capability gate has run."""
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if _off():
                return jsonify({"error": OFF_MESSAGE}), 403
            return view(*args, **kwargs)
        return wrapped

    @bp.route("/api/webfetch/read", methods=["POST"])
    @login_required
    @can_deploy
    @_gate
    def read_page():
        """Read a public https page as text, with its links."""
        url = str(_body().get("url") or "").strip()
        try:
            page = webfetch.fetch_text(url)
        except webfetch.FetchError as e:
            return refusals.refuse(e)
        _audit("webfetch.read", page["url"], {"url": url})
        return jsonify(page), 200

    @bp.route("/api/webfetch/winget", methods=["POST"])
    @login_required
    @can_deploy
    @_gate
    def winget_lookup():
        """Look a package up in the winget community repository: versions and manifests."""
        body = _body()
        try:
            found = webfetch.winget_manifest(body.get("package_id"), body.get("version"))
        except webfetch.FetchError as e:
            return refusals.refuse(e)
        _audit("webfetch.read", found.get("package_id"), {"winget": True,
                                                          "version": found.get("version")})
        return jsonify(found), 200

    @bp.route("/api/webfetch/downloads", methods=["GET"])
    @login_required
    @can_deploy
    @_gate
    def list_downloads():
        """Staged downloads, newest first: status, size, sha256, file name."""
        return jsonify({"downloads": webfetch.list_staged(staging)}), 200

    @bp.route("/api/webfetch/downloads", methods=["POST"])
    @login_required
    @can_deploy
    @_gate
    def start_download():
        """Start downloading a file from a public https URL into the hub's staging folder."""
        url = str(_body().get("url") or "").strip()
        try:
            meta = webfetch.begin_download(staging, url, _actor())
        except webfetch.FetchError as e:
            return refusals.refuse(e)
        except OSError as e:
            # The OS's text goes to the log, not the response -- see refusals.py on
            # py/stack-trace-exposure; a hand-built response is the case it rightly flags.
            print(f"[webfetch] could not create a staging folder: {e}")
            return jsonify({"error": STORE_FAILED}), 500
        pool.submit(webfetch.run_download, staging, meta["id"], _max_bytes())
        _audit("webfetch.download", url, {"staging_id": meta["id"]})
        return jsonify(dict(meta, note="Downloading in the background; read it back with "
                                       "wait_seconds to follow it.")), 202

    @bp.route("/api/webfetch/downloads/<staging_id>", methods=["GET"])
    @login_required
    @can_deploy
    @_gate
    def get_download(staging_id):
        """One staged download. `wait_seconds` (up to 60) waits while it is still running."""
        try:
            wait = max(0, min(int(request.args.get("wait_seconds") or 0), MAX_WAIT_SECONDS))
        except ValueError:
            wait = 0
        deadline = time.monotonic() + wait
        while True:
            meta = webfetch.get_staged(staging, staging_id)
            if meta is None:
                return jsonify({"error": "no such download"}), 404
            running = meta["status"] in (webfetch.STATUS_QUEUED, webfetch.STATUS_DOWNLOADING)
            if not running or time.monotonic() >= deadline:
                return jsonify(meta), 200
            time.sleep(1)

    @bp.route("/api/webfetch/downloads/<staging_id>/promote", methods=["POST"])
    @login_required
    @can_deploy
    @_gate
    def promote_download(staging_id):
        """Move a finished download into the package store; answers the package `source`."""
        try:
            source = webfetch.promote(staging, staging_id, blob_dir, _max_bytes())
        except KeyError:
            return jsonify({"error": "no such download"}), 404
        except ValueError as e:
            return refusals.refuse(e)
        except OSError as e:
            print(f"[webfetch] could not promote {staging_id}: {e}")
            return jsonify({"error": STORE_FAILED}), 500
        _audit("webfetch.promote", source.get("file_name"),
               {"staging_id": staging_id, "sha256": source["sha256"],
                "bytes": source["file_size"]})
        return jsonify({"source": source,
                        "note": "Use this as one entry of create_package's `sources`."}), 200

    @bp.route("/api/webfetch/downloads/<staging_id>", methods=["DELETE"])
    @login_required
    @can_deploy
    @_gate
    def discard_download(staging_id):
        """Delete a staged download."""
        try:
            webfetch.discard(staging, staging_id)
        except KeyError:
            return jsonify({"error": "no such download"}), 404
        _audit("webfetch.discard", staging_id, {})
        return jsonify({"status": "deleted"}), 200

    bp.staging_dir = staging
    bp.pool = pool
    return bp
