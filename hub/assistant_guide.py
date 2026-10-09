"""The console's map, as the assistant (roadmap #26) is told it.

The assistant links to pages and is asked "where do I do X", so it needs the same map the
sidebar draws: a key it can put in a `[[page:KEY]]` token, the path, the capability that gates
the link, and one line on what the page is for. Kept here rather than derived from the sidebar
template because a template is markup, and a regex over markup is a parser nobody tests.

**Drift is what this file can get wrong**, so tests/test_assistant.py checks every path below
against the real url_map, and every sidebar `data-nav-prefix` against this list. A page added
to the sidebar and not here fails that test rather than being a page the assistant cannot
name.

The capability is a tuple where the sidebar's gate is an OR (Tools shows for backups OR
firmware). `label_key` is the sidebar's own catalog key, so the assistant and the sidebar call a page the
same thing in every language. `about` is English and goes only to the model -- the model is
told to answer in the operator's language, and it translates a one-line description better
than this hub would maintain three copies of one.
"""
import permissions

PAGES = (
    ("dashboard", "/", "nav.dashboard", permissions.VIEW,
     "fleet overview: online/offline counts, hottest machines, recent problems"),
    ("inventory", "/inventory", "nav.inventory", permissions.VIEW,
     "the device list with filters, grouping and bulk actions"),
    ("map", "/map", "nav.map", permissions.VIEW, "where devices are"),
    ("reports", "/reports", "nav.reports", permissions.VIEW,
     "printable inventory sheets for a selection of machines, CSV/JSON export"),
    ("alerts", "/alerts", "nav.alerts", permissions.VIEW,
     "open and recent alerts, correlated bundles and recommended fixes"),
    ("remote", "/remote", "nav.remote", permissions.REMOTE_CONTROL,
     "remote view and control sessions"),
    # The sidebar also shows Recordings to someone who has only been SHARED a recording; that
    # is a per-recording grant this capability list cannot express, so the assistant offers
    # the page to those who can make one (roadmap #19).
    ("recordings", "/recordings", "nav.recordings", permissions.REMOTE_CONTROL,
     "session recordings: your own (share, download, delete) and ones shared with you"),
    ("packages", "/packages", "nav.packages", permissions.DEPLOY_PACKAGES,
     "software packages and deployments"),
    ("patches", "/patches", "nav.patches", permissions.VIEW,
     "Windows updates: approvals, maintenance windows, patch runs"),
    ("events", "/events", "nav.events", permissions.VIEW,
     "event log stream and subscriptions"),
    ("tools", "/tools", "tools.nav", (permissions.MANAGE_BACKUPS, permissions.MANAGE_FIRMWARE),
     "fleet tasks: backups (?tab=backup) and firmware (?tab=firmware)"),
    ("rules", "/rules", "nav.rules", permissions.VIEW,
     "the rules engine: conditions that raise alerts or run actions"),
    ("watchdogs", "/watchdogs", "nav.watchdogs", permissions.VIEW,
     "self-healing watchdogs that restart services on the machine itself"),
    ("device_groups", "/device-groups", "nav.device_groups", permissions.VIEW,
     "saved target filters used by rules, deployments and reports"),
    ("policy", "/policy", "nav.policy", permissions.VIEW,
     "app and screen-time policy for managed devices"),
    ("audit", "/audit", "nav.audit_log", permissions.VIEW_AUDIT_LOG,
     "who did what, when"),
    ("sharing", "/sharing", "nav.sharing", permissions.VIEW,
     "machines shared with or borrowed from other hubs"),
    ("settings", "/settings", "nav.settings", permissions.MANAGE_SETTINGS,
     "hub settings, including the AI provider (/settings#tab-ai)"),
    ("provisioning", "/provisioning", "nav.provisioning", permissions.MANAGE_SETTINGS,
     "enrolling new Android devices by QR code"),
    ("permissions", "/permissions", "nav.permission_groups",
     permissions.MANAGE_PERMISSION_GROUPS, "permission groups: who may do what, on which machines"),
    ("invites", "/invites", "nav.invites", permissions.MANAGE_PERMISSION_GROUPS,
     "invite links for new operators"),
    ("users", "/users", "nav.users", permissions.MANAGE_USERS, "the registered-users directory"),
    ("download", "/download", "nav.download", permissions.VIEW,
     "download the desktop client and pair a device"),
    ("assistant", "/assistant", "nav.assistant", permissions.VIEW, "this assistant"),
)

PAGES_BY_KEY = {key: (path, label_key, cap, about)
                for key, path, label_key, cap, about in PAGES}

# The machine page's tabs, as `?tab=` slugs. `report` is the printable sheet and lives on its
# own route, which is why app.MACHINE_PAGE_TABS does not list it and this does.
MACHINE_TABS = ("overview", "terminal", "backup", "firmware", "network", "files", "report")


def pages_for(capabilities, translate):
    """The pages this operator's sidebar shows, with labels in their language."""
    out = []
    for key, path, label_key, cap, about in PAGES:
        caps = cap if isinstance(cap, tuple) else (cap,)
        if any(c in capabilities for c in caps):
            out.append({"key": key, "path": path, "label": translate(label_key),
                        "about": about})
    return out


def find_pages(topic, capabilities, translate):
    """Pages matching a topic, for the `find_page` tool. Every page if nothing matches,
    because "I could not find it" from the hub's own map is a worse answer than the map."""
    words = [w for w in str(topic or "").lower().split() if len(w) > 2]
    pages = pages_for(capabilities, translate)
    hits = [p for p in pages
            if any(w in f"{p['key']} {p['label']} {p['about']}".lower() for w in words)]
    return [dict(p, link=f"[[page:{p['key']}]]") for p in (hits or pages)]
