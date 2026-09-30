"""Flask HTTP surface for the device sheet (roadmap #25 A) -- a thin layer over reports.py,
registered as a Blueprint from app.py.

**No new capability.** The sheet shows only what its reader could already open page by page,
so one machine's sheet is `access.require_machine(VIEW)` and the fleet view is `view` with the
selection narrowed by `filter_machines()`. Export is the same gate as the page it exports --
a JSON or CSV file is the page in another shape, and a download that could see more than the
page would be the way round the scope. reports.py explains the two things left off the sheet
because their own pages gate them higher.

**A selection names machines; the caller's scope decides which of them it gets.** Names from
the query string (ticked rows on Devices), a device group, or nothing (the whole visible fleet)
are all narrowed the same way, and an out-of-scope name is dropped SILENTLY rather than
refused. Refusing would make this an oracle for which hostnames exist outside a scope, the
thing permissions_web.require_machine is careful not to be.

**CSV is built here, not in the browser**, unlike the Devices export, because the file is
several sheets assembled on the hub and the browser never holds them all. Every cell goes
through reports.neutralise_cell: software names are the most attacker-influenced text this hub
stores. The BOM is there for the reason inventory.js gives -- Excel opens UTF-8 as UTF-8 only
when told, and an umlaut in a hostname is the common case in de/es.
"""
import csv
import io
import json
import re
import time

from flask import Blueprint, Response, jsonify, render_template, request

import device_groups
import permissions
import reports

#: A ceiling on one fleet request. A sheet is a dozen indexed reads; a thousand of them is
#: seconds of hub time, and a selection larger than this is a whole fleet somebody should
#: export in groups. Refused rather than truncated -- a CSV that silently stopped at row N
#: is a licence count that is quietly wrong.
MAX_SELECTION = 1000


def create_reports_blueprint(db_path, login_required, access, hardware_probe=None):
    bp = Blueprint("reports", __name__)
    can_view = access.require(permissions.VIEW)

    def _known_machines():
        with reports.get_conn(db_path) as conn:
            return [r["machine"] for r in conn.execute(
                "SELECT machine FROM machine_info ORDER BY machine COLLATE NOCASE")]

    def _selection():
        """(names, error) for the request's selection, already scoped."""
        known = _known_machines()
        by_key = {m.lower(): m for m in known}
        raw = (request.args.get("machines") or "").strip()
        group = (request.args.get("group") or "").strip()
        if raw:
            wanted, seen = [], set()
            for part in raw.split(","):
                name = by_key.get(part.strip().lower())
                if name and name not in seen:
                    seen.add(name)
                    wanted.append(name)
        elif group:
            if not group.isdigit():
                return None, "group must be a device group id"
            found = device_groups.get_group(db_path, int(group))
            if found is None:
                return None, "unknown device group"
            members = {m.lower() for m in device_groups.resolve(db_path, found)}
            wanted = [m for m in known if m.lower() in members]
        else:
            wanted = known
        wanted = access.filter_machines(wanted)
        if len(wanted) > MAX_SELECTION:
            return None, f"select at most {MAX_SELECTION} machines per report"
        return wanted, None

    def _sheets(names):
        return [s for s in (reports.build_sheet(db_path, n, hardware_probe) for n in names)
                if s is not None]

    def _stamp():
        return time.strftime("%Y-%m-%d", time.localtime())

    # ---------------- Pages ----------------
    @bp.route("/reports", methods=["GET"])
    @login_required
    @can_view
    def reports_page():
        """The fleet view. The selection is read by the page script from the same query
        string the API takes, so a Devices bulk hand-off is a plain link."""
        groups = [{"id": g["id"], "name": g["name"]} for g in device_groups.list_groups(db_path)]
        return render_template("reports.html", device_groups=groups)

    @bp.route("/reports/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def sheet_page(machine):
        """One machine's sheet on a page of its own -- the thing that gets printed. The
        machine page's Report tab draws the same sheet with the same script."""
        return render_template("report_sheet.html", machine=str(machine).strip())

    # ---------------- Read ----------------
    @bp.route("/api/reports/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_sheet(machine):
        sheet = reports.build_sheet(db_path, str(machine).strip(), hardware_probe)
        if sheet is None:
            return jsonify({"error": "unknown machine"}), 404
        if request.args.get("download"):
            # The hostname is whatever the machine reported to the unauthenticated /api/report,
            # so it is reduced to filename-safe characters before it goes in a header.
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", sheet["machine"])[:63] or "machine"
            return _json_download([sheet], f"fleethub-{safe}-{_stamp()}.json")
        return jsonify(sheet), 200

    @bp.route("/api/reports/fleet", methods=["GET"])
    @login_required
    @can_view
    def fleet_summary():
        """The selection as summary rows -- what the fleet view's table draws. Rows, not
        sheets: the page lists machines, and a hundred full sheets is a download, not a
        table."""
        names, error = _selection()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"rows": [reports.summary_row(s) for s in _sheets(names)]}), 200

    # ---------------- Export ----------------
    def _json_download(sheets, filename):
        body = json.dumps({"generated_at": int(time.time()), "sheets": sheets}, indent=2)
        return Response(body, mimetype="application/json", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})

    @bp.route("/api/reports/export.json", methods=["GET"])
    @login_required
    @can_view
    def export_json():
        names, error = _selection()
        if error:
            return jsonify({"error": error}), 400
        return _json_download(_sheets(names), f"fleethub-report-{_stamp()}.json")

    @bp.route("/api/reports/export.csv", methods=["GET"])
    @login_required
    @can_view
    def export_csv():
        """One CSV file per section: `summary` is one row per machine, and each repeating
        section (software, patches, network, volumes) is one row per item."""
        section = (request.args.get("section") or "summary").strip()
        fields = reports.CSV_SECTIONS.get(section)
        if fields is None:
            return jsonify({"error": "unknown section",
                            "sections": sorted(reports.CSV_SECTIONS)}), 400
        names, error = _selection()
        if error:
            return jsonify({"error": error}), 400
        out = io.StringIO()
        writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(fields)
        for sheet in _sheets(names):
            for row in reports.section_rows(sheet, section):
                writer.writerow([reports.neutralise_cell(row.get(f)) for f in fields])
        return Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
            "Content-Disposition":
                f'attachment; filename="fleethub-{section}-{_stamp()}.csv"'})

    return bp
