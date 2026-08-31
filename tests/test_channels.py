"""Unit tests for channels.py -- release-channel resolution, with no Flask involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis is on this feature's specific silent failures, which are all variations on
"a machine is on a different train than the console says":

  * **Precedence read the wrong way.** An override that loses to the fleet default means
    the pilot ring quietly does not exist, and nothing anywhere reports that.

  * **An unrecognised name resolving to something other than stable.** A typo, a rolled-back
    migration, or a hub too old to know a channel would otherwise promote machines onto a
    pre-release train -- or resolve to an empty url and stop updating them forever.

  * **The stable filenames drifting.** Both are pinned `-text` in .gitattributes and baked
    into every installed agent and client; renaming one breaks the whole fleet's updates in
    a way that looks like a network problem.

  * **Beta published on the wrong branch.** The manifest is only ever read from `main`, and
    a release pushed anywhere else reaches nothing while looking like it shipped.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import channels

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def make_db(path):
    """The one table this module reads, in the shape app.py's init_db leaves it."""
    with channels.get_conn(path) as conn:
        conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, "
                     "update_channel TEXT, updated_at TEXT)")


def main():
    workdir = tempfile.mkdtemp(prefix="channel-tests-")
    db_path = os.path.join(workdir, "channels.db")
    try:
        make_db(db_path)

        # ------------------------------------------------------------ normalisation
        print("\n== Normalisation ==")
        check("stable round-trips", channels.normalize("stable") == channels.STABLE)
        check("beta round-trips", channels.normalize("beta") == channels.BETA)
        check("casing and space do not make a new channel",
              channels.normalize("  BETA ") == channels.BETA)
        # The failure this catches: an unknown name propagating as itself, which downstream
        # either resolves to no url at all or is shown to an operator as a real channel.
        check("an unknown name reads as stable",
              channels.normalize("nightly") == channels.STABLE)
        check("None reads as stable", channels.normalize(None) == channels.STABLE)
        check("empty reads as stable", channels.normalize("") == channels.STABLE)

        # ------------------------------------------------------------ precedence
        print("\n== Precedence: override beats default, absent inherits ==")
        check("no override follows the fleet default",
              channels.resolve(None, channels.BETA) == channels.BETA)
        check("an empty override follows the fleet default too",
              channels.resolve("", channels.BETA) == channels.BETA)
        # The failure this catches: the pilot ring silently not existing.
        check("an override beats the default",
              channels.resolve(channels.STABLE, channels.BETA) == channels.STABLE)
        check("...in both directions",
              channels.resolve(channels.BETA, channels.STABLE) == channels.BETA)
        check("an unusable override still resolves, to stable",
              channels.resolve("nightly", channels.BETA) == channels.STABLE)
        check("an unusable DEFAULT resolves to stable rather than propagating",
              channels.resolve(None, "nightly") == channels.STABLE)

        check("a pinned machine is reported as pinned",
              channels.is_override(channels.BETA))
        # "on stable because somebody chose it" and "on stable because everything is" behave
        # differently the day the fleet default moves, so the console has to tell them apart.
        check("an inheriting machine is not",
              not channels.is_override(None) and not channels.is_override(""))

        # ------------------------------------------------------------ the override column
        print("\n== The per-machine override ==")
        check("a machine with no row inherits",
              channels.for_machine(db_path, "PC1", channels.STABLE) == channels.STABLE)
        check("...whatever the fleet default is",
              channels.for_machine(db_path, "PC1", channels.BETA) == channels.BETA)

        channels.set_override(db_path, "PC1", channels.BETA, "2026-08-31T10:00:00")
        check("a pinned machine takes its channel",
              channels.for_machine(db_path, "PC1", channels.STABLE) == channels.BETA)
        check("the pin survives a fleet default that disagrees",
              channels.for_machine(db_path, "PC1", channels.STABLE) == channels.BETA)
        check("the override reads back", channels.override_for(db_path, "PC1")
              == channels.BETA)

        # A machine can be pinned before it has ever reported anything -- that is exactly the
        # case when somebody builds a ring out of PCs they are about to enrol.
        channels.set_override(db_path, "NEVER-SEEN", channels.BETA, "2026-08-31T10:00:00")
        check("a machine with no prior row can be pinned",
              channels.for_machine(db_path, "NEVER-SEEN", channels.STABLE) == channels.BETA)

        check("the ring lists the pinned machines",
              channels.machines_on(db_path, channels.BETA) == ["NEVER-SEEN", "PC1"])

        channels.set_override(db_path, "PC1", None, "2026-08-31T11:00:00")
        check("clearing an override returns the machine to the fleet",
              channels.override_for(db_path, "PC1") is None)
        check("...and it inherits again",
              channels.for_machine(db_path, "PC1", channels.BETA) == channels.BETA)
        # Leaving beta does not roll anything back -- that is the agreed behaviour, and it is
        # the console's job to say so. All this asserts is that the CHOICE is cleared.
        check("the ring shrinks", channels.machines_on(db_path, channels.BETA)
              == ["NEVER-SEEN"])

        check("an unusable channel is stored normalised, not verbatim",
              channels.set_override(db_path, "PC2", "nightly", "2026-08-31T11:00:00")
              == channels.STABLE)
        check("a blank machine name is a no-op, not a crash",
              channels.set_override(db_path, "  ", channels.BETA, "x") is None
              and channels.override_for(db_path, "") is None)

        # A hub whose DB predates this column must answer on the heartbeat path rather than
        # raising -- the read is belt-and-braces, but the heartbeat is not a place to find out.
        legacy = os.path.join(workdir, "legacy.db")
        with channels.get_conn(legacy) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY)")
        check("a DB without the column answers None rather than raising",
              channels.override_for(legacy, "PC1") is None)

        # ------------------------------------------------------------ urls and filenames
        print("\n== Urls and filenames ==")
        # Both are pinned `-text` in .gitattributes and baked into every installed agent.
        check("the stable agent manifest keeps its name",
              channels.agent_manifest_filename(channels.STABLE) == "agent.manifest.json")
        check("the stable client manifest keeps its name",
              channels.client_manifest_filename(channels.STABLE) == "client.manifest.json")
        check("beta is a different file",
              channels.agent_manifest_filename(channels.BETA) == "agent.manifest.beta.json")
        check("...for the client too",
              channels.client_manifest_filename(channels.BETA)
              == "client.manifest.beta.json")

        # The manifest is only ever READ from main. A beta published on another branch
        # reaches nothing while looking like it shipped.
        check("both agent manifests are read from main",
              all("/main/agent/" in channels.agent_manifest_url(c)
                  for c in channels.CHANNELS))
        check("the two agent urls differ",
              channels.agent_manifest_url(channels.STABLE)
              != channels.agent_manifest_url(channels.BETA))

        # The HUB is the one that switches branch, because it has no manifest to switch.
        check("a stable hub tracks main", channels.hub_ref(channels.STABLE) == "main")
        check("a beta hub tracks beta", channels.hub_ref(channels.BETA) == "beta")
        check("the hub source url follows the ref",
              "/beta/hub/app.py" in channels.hub_source_url(channels.BETA))
        check("the archive url follows the ref",
              channels.hub_archive_url(channels.BETA).endswith("/refs/heads/beta"))
        check("an unknown hub channel still tracks main",
              channels.hub_ref("nightly") == "main")

        check("every channel has a place in the vocabulary",
              set(channels.CHANNELS) == {channels.STABLE, channels.BETA}
              and channels.DEFAULT in channels.CHANNELS)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
