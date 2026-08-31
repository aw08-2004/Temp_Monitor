"""Release channels -- which train of builds a machine, this hub, or the client follows.

Roadmap #21. Everything in this product updates from `main` and reaches everything at once:
a signed agent manifest landing on main starts upgrading the whole fleet within about
fifteen minutes. That is the right default and the wrong only option, because it means a
release is tested for the first time on every endpoint simultaneously. A **beta** channel
gives a handful of machines the new build first.

Four things carry the design, and the first two are constraints rather than choices:

  * **The hub sends a channel NAME, never a URL.** `State/RuntimeConfig.cs` on the agent
    states the rule and enforces it with an allow-list: nothing the hub pushes may redirect
    where the agent gets its code or which key verifies it. So the agent holds BOTH manifest
    URLs compiled in and the hub merely picks between them. A compromised hub can move a
    machine onto the beta train; it cannot point that machine at a binary of its choosing,
    because both destinations are still signed by the same offline release key. That is the
    whole reason this module deals in names and filenames rather than in URLs the agent
    would trust. The urls below exist for the HUB's own use -- reading version hints, and
    updating itself -- and are never shipped anywhere.

  * **One increasing number sequence, shared by both channels.** `VERSIONING.md` forbids
    version suffixes (four separate comparators break on `3.36.0-beta1`), and every updater
    here installs only what is strictly newer. So beta is not a parallel numbering; it is
    simply where a number appears FIRST. Two consequences worth stating plainly, because
    they are what make this design small:

      - Promoting a beta to stable is copying `agent.manifest.beta.json` over
        `agent.manifest.json`. Every pilot machine is already at that version and does
        nothing; every stable machine updates to it. Nothing is renumbered.
      - Leaving beta does NOT roll a machine back. It keeps its build and stops updating
        until stable catches up, which for a promoted beta is immediately. This is deliberate
        -- a downgrade path would mean defeating the strictly-newer guard that is currently
        the thing stopping a replayed or rolled-back manifest from being installed.

  * **The hub's own channel selects a git REF, not a manifest.** The agent and the client
    each read a signed manifest; the hub has none -- `hub_update_available()` reads
    HUB_VERSION straight out of `hub/app.py` on main, and the updater pulls either
    `origin/main` or a codeload zip of main. So `hub_ref()` is the odd one out here by
    necessity, not by oversight.

  * **Unknown reads as stable, everywhere.** A channel name arriving from a database column,
    a settings value, or an older peer is normalised through `normalize()`, and anything
    unrecognised becomes stable. The failure this prevents is a typo or a rolled-back
    migration quietly promoting machines onto a pre-release train.

Kept free of Flask. The precedence rule -- override beats default, absent inherits -- is a
PURE function (`resolve`), so it is testable with nothing set up at all; the small DB helpers
below read the `machine_info.update_channel` column the same way firmware.py, wake.py and
directory.py read that table. Keeping them here rather than in app.py is what lets
fleet_web.py answer a heartbeat with a machine's channel without importing app.
"""
import sqlite3

#: The repository every channel resolves against. One constant rather than four copies of
#: the same slug -- the pre-rename path is deliberate everywhere it appears (see
#: AgentConfig.UpdateManifestUrl on the agent for why moving it early 404s the fleet).
REPO = "aw08-2004/Temp_Monitor"

STABLE = "stable"
BETA = "beta"
#: Order is least- to most-adventurous, which is the order a picker should render them in.
CHANNELS = (STABLE, BETA)

#: The default for everything: a machine with no override, a fleet with no setting, a hub
#: that has never been told. Named rather than inlined so "what happens when nobody chose"
#: has exactly one answer.
DEFAULT = STABLE

#: Labels and descriptions live in the translation catalogs under
#: `channels.channel.<name>.label` / `.description`, the same self-describing discipline as
#: packages.DETECTION_TEXT_KEY and permissions.CAPABILITY_TEXT_KEY. A channel added without
#: catalog entries fails tests/test_i18n.py rather than captioning a picker with its own key.
CHANNEL_TEXT_KEY = "channels.channel"

#: The git ref each channel tracks. Only the hub uses these -- see the module docstring.
_REFS = {STABLE: "main", BETA: "beta"}

#: Manifest filenames. The stable names are the existing files and must not change: they are
#: pinned `-text` in .gitattributes and baked into every agent already in the field.
_AGENT_MANIFEST = {STABLE: "agent.manifest.json", BETA: "agent.manifest.beta.json"}
_CLIENT_MANIFEST = {STABLE: "client.manifest.json", BETA: "client.manifest.beta.json"}


def normalize(value):
    """A usable channel name, defaulting to stable.

    Never raises and never returns None. Every read of a channel -- from the settings table,
    from a machine's override column, from a heartbeat -- goes through here, so an
    unrecognised value degrades to the safe train instead of being propagated.
    """
    text = str(value or "").strip().lower()
    return text if text in CHANNELS else DEFAULT


def resolve(override, fleet_default=DEFAULT):
    """The channel a machine is actually on.

    `override` is that machine's own choice (the machine_info column) and is usually None,
    meaning "follow the fleet". Kept pure and separate from the database so the precedence
    rule -- override beats default, absent inherits -- is one testable line rather than a
    join somebody has to read twice.
    """
    if override is None or str(override).strip() == "":
        return normalize(fleet_default)
    return normalize(override)


def is_override(override):
    """Whether a machine has been pinned rather than inheriting. The console needs to say
    which, because "on stable because somebody chose it" and "on stable because everything
    is" behave differently the day the fleet default moves."""
    return not (override is None or str(override).strip() == "")


# ================================
# AGENT
# ================================
def agent_manifest_filename(channel):
    return _AGENT_MANIFEST[normalize(channel)]


def agent_manifest_url(channel=DEFAULT):
    """Where the HUB reads a channel's agent manifest to learn its version.

    The agent does not use this -- it holds both URLs compiled in. This is the hub's version
    hint, and it deliberately reads the same file the agent's own updater would install
    from, so `/api/report` never advertises a version the agent would then decline.
    """
    return (f"https://raw.githubusercontent.com/{REPO}/main/agent/"
            f"{agent_manifest_filename(channel)}")


# ================================
# CLIENT
# ================================
def client_manifest_filename(channel):
    return _CLIENT_MANIFEST[normalize(channel)]


# ================================
# HUB
# ================================
def hub_ref(channel=DEFAULT):
    """The git ref this hub's channel tracks. `main` for stable, `beta` for beta."""
    return _REFS[normalize(channel)]


def hub_source_url(channel=DEFAULT):
    """Where to read HUB_VERSION for the update notice and the opt-in self-updater."""
    return f"https://raw.githubusercontent.com/{REPO}/{hub_ref(channel)}/hub/app.py"


def hub_archive_url(channel=DEFAULT):
    """The branch zip the files-only self-update path downloads."""
    return f"https://codeload.github.com/{REPO}/zip/refs/heads/{hub_ref(channel)}"


# ================================
# THE PER-MACHINE OVERRIDE
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def override_for(db_path, machine):
    """A machine's pinned channel, or None when it follows the fleet default.

    Tolerant of the column not existing yet: a hub mid-migration, or one whose DB predates
    this feature, answers None rather than raising on the heartbeat path. The column is added
    by app.py's init_db, so this is a belt-and-braces read rather than the normal case.
    """
    name = str(machine or "").strip()
    if not name:
        return None
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT update_channel FROM machine_info WHERE machine = ?",
                (name,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row["update_channel"] or None) if row else None


def for_machine(db_path, machine, fleet_default=DEFAULT):
    """The channel this machine is actually on. The heartbeat's question."""
    return resolve(override_for(db_path, machine), fleet_default)


def set_override(db_path, machine, channel, now_iso):
    """Pin a machine to a channel, or pass None/"" to return it to the fleet default.

    INSERT ... ON CONFLICT rather than UPDATE: a machine can be pinned before it has ever
    reported anything else worth a machine_info row, which is exactly the case when somebody
    builds a pilot ring out of PCs they are about to enrol. `now_iso` is passed in rather
    than computed here so the stamp matches the format app.py writes everywhere else in this
    table.

    Returns the stored value (a channel name, or None for "inherit").
    """
    name = str(machine or "").strip()
    if not name:
        return None
    value = normalize(channel) if is_override(channel) else None
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_info (machine, update_channel, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(machine) DO UPDATE SET update_channel = excluded.update_channel, "
            "updated_at = excluded.updated_at",
            (name, value, now_iso))
    return value


def machines_on(db_path, channel):
    """Every machine pinned to `channel`. The pilot ring, for the console."""
    wanted = normalize(channel)
    with get_conn(db_path) as conn:
        return sorted(r["machine"] for r in conn.execute(
            "SELECT machine FROM machine_info WHERE update_channel = ?", (wanted,)))
