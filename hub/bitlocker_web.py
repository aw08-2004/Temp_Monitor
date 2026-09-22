"""Flask HTTP surface for BitLocker posture and recovery-key escrow (roadmap #19) -- a thin
layer over bitlocker.py, registered as a Blueprint from app.py.

**Three gates, and the third is the feature:**

  * **Reading the posture** is `view` + machine scope. Whether a PC is encrypted is inventory
    in exactly the sense its model and its disks are, and an operator who can see the machine
    can see it. This is the same split bios_web.py and wake_web.py already draw, for the same
    reason: making "is this laptop encrypted?" cost an admin makes the question cost more than
    walking to the desk.
  * **Reading a recovery password** is `read_recovery_keys` + machine scope -- its own
    capability, and not a reuse of `manage_backups` even though the key sits in that module's
    secret store. Holding the fleet's recovery passwords is the most concentrated secret in
    this product, and folding it into an existing capability would have granted it silently,
    on the day it shipped, to everybody who already had that one. A new capability is granted
    to nobody until an admin decides to.
  * **The agent endpoint** is bearer agent auth, and acts only for the calling machine.

**A reveal is a POST, and it is audited at security level.** Nothing about producing a
recovery password is idempotent in the way a GET promises: it is an event, it is the moment
custody of that key leaves this hub, and the whole argument for a hub holding these keys is
that taking one out is recorded. A GET would also put the key in a URL that a browser history,
a proxy log and a shared screenshot all keep -- so the id goes in the body, not the path.

**The reveal returns ONE protector's password, named explicitly.** There is no "all keys for
this machine" route, and that is not an oversight: a recovery screen asks for one key, so an
operator needs one, and an endpoint that hands over four is an endpoint whose audit entry
cannot say which one mattered.

**Keys arrive only because the hub asked.** The heartbeat reply carries
`bitlocker_escrow_wanted` (see fleet_web.py), the agent posts exactly those, and
`bitlocker.clean_key_submission` filters what arrives against that same list recomputed
server-side. An enrolled agent therefore cannot grow its own secret blob with ids it invented,
and a settled fleet posts nothing at all.

**A hub with no master key does not pretend to escrow.** `BACKUP_MASTER_KEY` is what wraps the
sidecar store; without it the hub stops asking for keys rather than storing them in the clear
or accepting and dropping them. The console says why, because a security feature that is
quietly doing nothing is worse than one that is plainly off.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
import threading
from collections import defaultdict
from flask import Blueprint, jsonify, request

import auth_helpers
import backups
import bitlocker
import fleet
import permissions
import permissions_web
import settings

# Per-machine lock for the escrow read-modify-write + index reconciliation sequence.
_machine_escrow_locks = defaultdict(threading.Lock)


def escrow_enabled(db_path):
    """Is the hub collecting recovery keys at all?

    Both halves are real gates and both are checked everywhere the answer is needed: the
    operator's setting, and whether there is a master key to wrap anything with. They are
    deliberately one function rather than two checks copied to three call sites -- the
    heartbeat, the agent POST and the console all have to agree, and the way they stop agreeing
    is one of them being updated and the others not.
    """
    if backups.load_master_key() is None:
        return False
    return bool(settings.get_bool(db_path, "security.escrow_bitlocker_keys"))


def create_bitlocker_blueprint(db_path, log_dir, login_required, access):
    bp = Blueprint("bitlocker", __name__)
    can_view = access.require_machine(permissions.VIEW)
    can_read_keys = access.require_machine(permissions.READ_RECOVERY_KEYS)

    def _current_email():
        return permissions_web.current_actor()

    # ---------------- Console ----------------
    @bp.route("/api/bitlocker/<machine>", methods=["GET"])
    @login_required
    @can_view
    def machine_bitlocker(machine):
        """The machine's encryption posture as of its last heartbeat, plus which protectors
        the hub holds a key for.

        Never a password. What this answers is "do we have it", which is the question somebody
        asks before they need it -- and the question that has an answer worth acting on while
        the machine is still working.

        `support: null` means no agent has ever told us, which the console must render
        differently from `unsupported`: the first is a Windows agent older than this feature
        (or a machine that has not heartbeated yet), the second is a machine that has said it
        has nothing to encrypt. Collapsing them makes every fleet look unencrypted the day
        before the agent release lands.
        """
        payload = bitlocker.get_inventory(db_path, machine)
        # Whether escrow is on at all, so the card can say "nothing is being collected" rather
        # than showing an empty escrow column that reads as "no keys exist".
        payload["escrow_enabled"] = escrow_enabled(db_path)
        payload["can_read_keys"] = access.can(permissions.READ_RECOVERY_KEYS)
        return jsonify(payload), 200

    @bp.route("/api/bitlocker/<machine>/reveal", methods=["POST"])
    @login_required
    @can_read_keys
    def reveal_key(machine):
        """Hand one escrowed recovery password to an operator who is allowed to have it.

        Audited before the key is produced, not after: the audit write is the price of the
        read, and an ordering where a failure to record still returns the secret is an
        ordering where the record is optional.
        """
        data = request.get_json(silent=True) or {}
        protector_id = str(data.get("protector_id") or "").strip()
        if not protector_id:
            return jsonify({"error": "A protector id is required."}), 400
        if protector_id not in bitlocker.escrowed_ids(db_path, machine):
            # Deliberately the same answer for "we never had it" and "that id is nonsense":
            # an operator who may read this machine's keys learns nothing from the difference,
            # and the console already shows which protectors are escrowed.
            return jsonify({"error": "No recovery key is escrowed for that protector."}), 404

        master = backups.load_master_key()
        if master is None:
            return jsonify({"error": "No BACKUP_MASTER_KEY is configured, so escrowed keys "
                                     "cannot be decrypted. This hub is not the one that "
                                     "stored them, or the key has been lost."}), 400
        try:
            stored = backups.load_secret(log_dir, master, bitlocker.secret_id_for(machine))
        except ValueError:
            # The master key changed under an existing store -- the failure that cost this
            # fleet its backups once already (hub 1.117.0's import path exists because of it).
            # Named rather than reported as "not found", so nobody goes looking for a key that
            # is sitting right there, unreadable.
            return jsonify({"error": "The escrowed keys for that machine cannot be decrypted "
                                     "with this hub's master key."}), 400
        entry = ((stored or {}).get("keys") or {}).get(protector_id)
        if not entry or not entry.get("recovery_password"):
            return jsonify({"error": "No recovery key is escrowed for that protector."}), 404

        fleet.audit(db_path, actor=_current_email(), action="bitlocker_key_read",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"protector_id": protector_id, "volume": entry.get("volume", "")})
        bitlocker.note_read(db_path, machine, protector_id)
        return jsonify({
            "machine": machine,
            "protector_id": protector_id,
            "volume": entry.get("volume", ""),
            "recovery_password": entry["recovery_password"],
        }), 200

    # ---------------- Agent ----------------
    @bp.route("/api/agent/bitlocker/keys", methods=["POST"])
    def agent_bitlocker_keys():
        """Escrow the recovery passwords the hub asked this machine for.

        Scoped to the caller by construction: the machine name comes from the bearer token and
        never from the body, so one enrolled agent cannot file keys against another's name --
        which here would mean writing over the custody record of a PC it has nothing to do
        with.

        Returns what was stored rather than a bare 200, so an agent that posts a key the hub
        did not want (a protector reported and then rotated away inside one heartbeat) can see
        that it was dropped instead of retrying it forever.
        """
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        if not escrow_enabled(db_path):
            # 200, not an error: the agent did nothing wrong and there is nothing for it to
            # fix. It learns to stop offering from `bitlocker_escrow_wanted` going empty on the
            # next heartbeat, which is the same channel that told it to start.
            return jsonify({"stored": 0, "escrow": "disabled"}), 200

        wanted = set(bitlocker.escrow_wanted(db_path, machine))
        offered = (request.get_json(silent=True) or {}).get("keys") or []
        if not isinstance(offered, list):
            return jsonify({"stored": 0}), 200
        submissions = []
        seen = set()
        for raw in offered[:bitlocker.MAX_KEYS_PER_MACHINE]:
            cleaned = bitlocker.clean_key_submission(raw, wanted)
            if cleaned is not None and cleaned["protector_id"] not in seen:
                seen.add(cleaned["protector_id"])
                submissions.append(cleaned)
        if not submissions:
            return jsonify({"stored": 0}), 200

        held = bitlocker.count_keys(db_path, machine)
        if held + len(submissions) > bitlocker.MAX_KEYS_PER_MACHINE:
            # Refuse the new keys rather than evicting old ones -- see MAX_KEYS_PER_MACHINE.
            # Audited because a machine that has produced this many protectors is either
            # something nobody has looked at in years or something going wrong, and either way
            # it is the kind of thing that should not be discovered by its absence.
            fleet.audit(db_path, actor=machine, action="bitlocker_escrow_full",
                        level=fleet.LEVEL_NOTICE, target=machine,
                        detail={"held": held, "offered": len(submissions)})
            return jsonify({"stored": 0, "error": "escrow full"}), 409

        # Per-machine lock covering the complete read-modify-write and index
        # reconciliation sequence, so concurrent submissions for the same machine
        # cannot lose keys or produce inconsistent index rows.
        with _machine_escrow_locks[machine]:
            master = backups.load_master_key()
            if master is None:
                return jsonify({"stored": 0, "escrow": "disabled"}), 200
            secret_id = bitlocker.secret_id_for(machine)
            try:
                stored = backups.load_secret(log_dir, master, secret_id) if \
                    backups.has_secret(log_dir, secret_id) else {}
            except ValueError:
                # An existing blob this hub cannot open. Refused rather than replaced: writing a
                # fresh store over it would destroy every key in it, which is the one outcome this
                # module is built to make impossible. An operator fixes this by restoring the
                # master key (hub 1.117.0's import), and the audit entry is what tells them to.
                fleet.audit(db_path, actor=machine, action="bitlocker_escrow_unreadable",
                            level=fleet.LEVEL_SECURITY, target=machine, detail={})
                return jsonify({"stored": 0, "error": "existing escrow is unreadable"}), 409

            merged, added = bitlocker.merge_keys((stored or {}).get("keys") or {}, submissions)
            if not added:
                return jsonify({"stored": 0}), 200
            try:
                backups.store_secret(log_dir, master, secret_id, {"keys": merged})
            except ValueError as e:
                # A hub without `cryptography` installed. Nothing is indexed, so the heartbeat
                # goes on asking and the key arrives once somebody fixes the install -- which is
                # strictly better than recording an escrow that does not exist.
                #
                # The reason goes to the hub's log, not to the agent: the caller here is a service
                # that cannot act on it, and this is the one route in this module whose body would
                # otherwise carry an internal failure string back out over the wire. Whoever fixes
                # the install is reading the hub's console output anyway.
                print(f"[bitlocker] Could not store escrowed keys for {machine}: {e}")
                return jsonify({"stored": 0, "error": "escrow store unavailable"}), 500
            # Reconcile index rows for ALL validated keys in the stored blob, not only newly
            # added entries, so retries repair missing indexes while preserving concurrent updates.
            bitlocker.reconcile_escrow_index(db_path, machine, merged)
            fleet.audit(db_path, actor=machine, action="bitlocker_key_escrow",
                        level=fleet.LEVEL_SECURITY, target=machine,
                        detail={"protectors": [a["protector_id"] for a in added]})
            return jsonify({"stored": len(added)}), 200

    return bp


def move_escrow(log_dir, old, new):
    """Move a machine's escrowed keys to its new name, alongside bitlocker.rename_machine.

    Lives here rather than in bitlocker.py because opening the store needs the master key, and
    bitlocker.py is deliberately Flask-free and secret-free. Silent when there is nothing to
    move or nothing to move it with: a rename must not fail because a hub has no master key,
    and the index rows the model half moves are what make the loss visible if it ever happens.
    """
    master = backups.load_master_key()
    if master is None:
        return False
    source_id = bitlocker.secret_id_for(old)
    dest_id = bitlocker.secret_id_for(new)
    # Load the source blob (the machine being merged away).
    if not backups.has_secret(log_dir, source_id):
        return False
    try:
        source_stored = backups.load_secret(log_dir, master, source_id)
    except (ValueError, Exception):
        return False
    # Load the destination blob if it already holds keys for the survivor.
    dest_stored = {}
    try:
        if backups.has_secret(log_dir, dest_id):
            dest_stored = backups.load_secret(log_dir, master, dest_id) or {}
    except (ValueError, Exception):
        # Destination unreadable -- refuse to overwrite; source keeps its copy.
        return False
    # Union both machines' key sets; the survivor's keys win on collision
    # (same physical device, the survivor was still reporting).
    merged_keys = dict((dest_stored.get("keys") or {}))
    for protector_id, key_data in (source_stored.get("keys") or {}).items():
        if protector_id not in merged_keys:
            merged_keys[protector_id] = key_data
    merged = dict(source_stored)
    merged["keys"] = merged_keys
    try:
        backups.store_secret(log_dir, master, dest_id, merged)
    except (ValueError, Exception):
        return False
    backups.delete_secret(log_dir, source_id)
    return True
