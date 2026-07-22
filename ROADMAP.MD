# FleetHub — Feature Roadmap

Status: ✅ done · 🚧 partially done · 📋 planned (spec below)

---

## ✅ 6. History page

Done. Per-machine multi-panel dashboard (Komodo-style): CPU load, memory (with total RAM
shown and used/total GB on hover), disk usage, disk read/write, network in/out, GPU
temp/load, temperature. Backed by typed `readings` columns, a multi-metric history
endpoint, and per-metric collection toggles in Settings.

**Throughput panels auto-scale (hub 1.31.0)** — network in/out and disk read/write are
stored in B/s and rendered through `formatRate()`, so each axis tick and tooltip picks its
own KB/MB/GB. A fixed `B/s` axis made a 400 MB/s NVMe an unreadable nine-digit tick and an
idle NIC a flat line on the floor. Disk I/O is **summed across every drive**, unlike
network, which picks the single busiest adapter — LHM reports each disk once, with none of
the NDIS filter mirrors that would double-count a NIC. Its own toggle
(`metrics.collect_disk_io`), separate from `collect_disk`, so dropping the noisy per-second
rates doesn't also stop recording "is C: filling up".

**Storage cards (hub 1.31.0 + agent 3.10.0)** — one live tile per volume on the machine
page: fill bar, % occupied, used/total/free, amber at 80% and red at 90%. Live state rather
than a chart panel, because how many disks a machine has is per-machine and changes over
its life. **The GB has to come from the agent**: LHM reports storage used space only as a
percentage, with the absolute size nowhere in its sensor set, so `VolumeReader.cs` walks
`DriveInfo` and appends synthetic `/volume/<letter>` Data sensors to the ordinary sensor
block — no new `/api/report` field, and an older hub ignores them. A machine that isn't
sending them (companion.py, or an agent below 3.10.0) falls back to LHM's per-device
percentage and shows the bar without the GB line.

## 🚧 3. Settings: which sensors to read, data retention, etc.

Mostly done:
- `computer.primary_sensor_preference` — ordered CPU-temp sensor preference (+ per-machine
  override).
- `metrics.collect_cpu_load` / `collect_memory` / `collect_gpu` / `collect_disk` /
  `collect_disk_io` / `collect_network` — per-metric collection on/off toggles.
- `data.retention_days`, `data.prune_interval_seconds`, `data.ingest_max_backdate_days`,
  `data.command_output_retention_seconds` — retention knobs.

Remaining gaps: no per-metric retention (one global window for all readings), no
sampling/reporting-cadence setting, no per-metric or per-machine alert thresholds beyond a
single global overheat threshold, no sensor *preference* list for GPU/disk/network (only
CPU temp has one — the others are on/off only). Fold these in opportunistically; no
dedicated feature push planned.

---

## ✅ Foundation — Permission Groups

Built (hub 1.26.0). `permissions.py` (model, flask-free) + `permissions_web.py` (the
`Access` enforcement object + the admin API), a Permission Groups page, and the
capability/scope gates applied across every existing console route — machine list,
machine detail, history, daily summary, alerts, fleet status/commands/terminal output,
and settings. Live telemetry is scoped by Socket.IO room. `ALLOWED_EMAILS` is now the
break-glass superuser list, and login additionally accepts any permission-group member.

Delivered as specified below, with two implementation-level additions: a `scope_mode`
column (`list` | `all`) so a group can be fleet-wide without listing every machine —
this is also where roadmap #4's `ad_ou` mode plugs in — and machine lifecycle hooks
(`forget_machine` on delete, `rename_machine` on a duplicate-serial merge) so a merge
never silently drops a machine out of an operator's scope.

**Not yet done**: nothing consumes `remote_control`, `deploy_packages` or
`manage_backups` — they exist in the vocabulary so the features below have a gate
waiting for them. Machine-record administration (delete/merge/pin sensor/dismiss alert)
is gated on `manage_settings` rather than a capability of its own.

<details>
<summary>Original spec</summary>

Every planned feature below needs an access-control model tighter than today's flat
"any `ALLOWED_EMAILS` address is fully privileged." The requirement is org/site scoping —
e.g. the Hospital IT operator manages Hospital PCs, the HR IT operator manages HR PCs —
not just an admin/non-admin tier. This is a full access-control system, not a thin roles
layer, and nothing else on this roadmap can be meaningfully authorized without it.

**Model.**
- **Permission Group** = `{name, capabilities, machine_scope}`.
  - `capabilities`: granular toggles an Admin sets per group — `view`, `issue_commands`,
    `remote_control`, `deploy_packages`, `manage_backups`, `manage_settings`,
    `manage_permission_groups`. "Admin" is simply a group with the last capability, not a
    hardcoded special tier.
  - `machine_scope`: v1 = an explicit machine list per group. Designed to extend to
    "resolved from an AD OU/group" once AD integration (#4) lands, without changing the
    enforcement model.
- **Membership**: a user (by email/UPN) belongs to one or more groups; effective
  capabilities/scope = the union across their groups. `ALLOWED_EMAILS` becomes the
  **break-glass superuser list** — membership grants every capability over every machine,
  bypassing group scoping. This is both the bootstrap path (day one, before any group
  exists) and the safety net if group config is ever broken.
- **Enforcement is two-layered** everywhere a machine is touched: (1) is there a session
  (today's `login_required`), (2) a new `require_capability(cap)` + a per-call
  `machine_in_scope(user, machine)` check. Applies to writes (issuing a command) **and**
  reads (a Hospital tech shouldn't see HR's machines in a list, not just be blocked from
  acting on them).

**New tables** (same DB): `permission_groups (id, name, capabilities_json)`,
`permission_group_machines (group_id, machine)`, `permission_group_members (group_id,
email)` — the member row anticipates an `ad_group_dn` alternate key for AD integration,
without a later schema change.

**New UI**: a Permission Groups admin page (define groups, assign capabilities, assign
machines, assign members) — a first-class page, not a settings tab.

**Ripple effect**: touches nearly every existing `@login_required` route, not just new
ones — the dashboard, machine list, and history endpoints all need the read-scope filter
too. Budget for that explicitly when this becomes an implementation plan.

</details>

---

## ✅ 5. Deploy packages (PDQ-style)

Built (hub 1.27.0). `packages.py` (model + blob store + scheduler, flask-free) +
`packages_web.py` (console API, agent payload download), a Packages page with a
Deployments tab and per-machine progress, and `DeployPackageExecutor` on the agent.
Delivered as specified below; the open parameters were resolved as:

- **Retry defaults**: 3 attempts, 15-minute backoff **doubling** per attempt (1x, 2x,
  4x…) — a machine off for the weekend shouldn't be retried every quarter hour. Both are
  `deploy.*` settings, and each deployment carries its own copy so changing the default
  never rewrites a policy an operator already agreed to.
- **Upload storage**: content-addressed at `<log dir>/packages/<aa>/<sha256>`, beside the
  database rather than in the source tree — the hub's own updater replaces that tree
  wholesale. Refcounted through `package_sources`, so two packages built from the same
  installer share one file and a blob is unlinked only when nothing points at it.
  `deploy.max_upload_mb` (512 default) is the quota.
- **Detection grammar**: exactly three kinds plus an explicit `none` opt-out —
  `file_exists`, `registry_value` (with an optional exact match), `installed_version`
  (against Windows' installed-programs list, both registry views). Unknown keys are
  dropped at validation, and a kind the agent doesn't implement fails closed.

Two implementation-level notes worth carrying forward:

- Command TTL expiry used to be **lazy** — only swept when an agent for that machine
  polled — so a deploy aimed at an offline machine would have waited forever. Expiry now
  has a heartbeat of its own (`fleet.expire_stale_commands`), which also fixes commands
  sitting `pending` in the console forever for a decommissioned box.
- The scheduler **claims a target before queueing its command**, not after. A crash
  between the two then costs one retry instead of installing the package twice.

**Not yet done**: the agent-side executor ships in source but needs an agent release
(version bump + `sign_release.py --sign-agent`) before the fleet actually gains it.

**Fixed in 3.9.0**: `DeployPackageExecutor` passed its line-oriented `Say()` straight to
`ProcessRunner`'s `onLine`, which already hands back newline-terminated text — so every
line of installer output was double-spaced in both the live console and the stored result
log, and when no live sink was attached (`onOutput == null`, so `onLine` was null) the
installer's output was dropped from the result entirely. `Say()` (our own messages, needs
terminating) and `Emit()` (already terminated) are now separate, and the deploy log is
covered by tests rather than eyeballed.

---

## ✅ 1. Backups via HTTPS   ·   1a hub 1.28.0, 1b hub 1.30.0 + agent 3.9.0, manual runs + offline catch-up hub 1.32.0

**1a. Hub database** — ✅ built. `backups.py` (envelope, destinations, snapshot/rotate
scheduler — flask-free) + `backups_web.py` (console API), a Backups page, `backup.*`
settings, and `restore_backup.py`. Delivered as specified below; the open parameters were
resolved as:

- **Retention/rotation**: count-based, `backup.hub_keep_generations` (14 default = two
  weeks at the default daily cadence). Rotation reads the **remote listing**, not a local
  record, and orders by object key — every key is timestamp-prefixed, so "newest N" is a
  lexicographic sort and never depends on S3's and WebDAV's disagreeing mtime formats. A
  generation deleted by hand stays deleted; `keep < 1` is refused rather than treated as
  "empty the bucket".
- **Chunking**: 4 MiB AES-256-GCM chunks, nonce = per-artifact random prefix ‖ counter.
  The AAD binds `sha256(header) ‖ counter ‖ final-flag`, so header edits, reordering and —
  the likely one — **truncated uploads** all fail closed. No resumable upload in 1a: the
  artifact is built to a temp file and PUT in one shot, because a partial object in the
  bucket looks exactly like a good one to the next rotation pass. Resumability is a #1b
  concern, where files are per-machine and links are worse.
- **S3 signing**: SigV4 written out in ~100 lines of stdlib `hmac` rather than taking
  botocore (~80 MB) onto a hub whose whole sparse install is 0.3 MB. Verified in
  `tests/test_backups.py` against AWS's own published vectors. `sigv4_presign()` is the
  same signer in query-string form — unused by 1a, and there precisely so #1b can hand a
  machine a PUT URL scoped to `<prefix>/machines/<machine>/` without it ever holding the
  master credential.

**The key-custody story, designed explicitly** (the roadmap flagged it as the sharpest
edge, so it is not left implicit):

- `BACKUP_MASTER_KEY` lives in `.env` — generated once, never in the `settings` table,
  never in the database it protects. Each artifact carries its own random data key wrapped
  by it, so no two artifacts share a key stream.
- Destination **credentials** are encrypted with that master key into a sidecar file
  (`backup_secrets.json`), keyed by destination id (which is the AAD, so a credential blob
  copied between destinations fails rather than authenticating somewhere unintended).
- **`restore_backup.py` needs the key and the file, and nothing else** — no hub, no
  database, no network. That is the only form of "we can restore" that survives losing the
  server, and it is why the tool ships in `HUB_RUNTIME_FILES`.
- The console **nags until a human confirms the key is stored off the machine**
  (`master_key.escrowed_at`), because a hub happily uploading backups nobody can decrypt
  looks identical to a hub that is working. Every reveal is audited.

Two implementation-level notes worth carrying forward:

- `manage_backups` gets its own narrow write path to the four `backup.*` settings
  (`PUT /api/backups/schedule`, allow-listed from `settings.BACKUP_SCHEDULE_KEYS`).
  Otherwise arming a schedule would have required `manage_settings` — i.e. the whole
  point of a narrow capability, undone.
- Backups are **not machine-scoped**, deliberately: a hub database backup is the entire
  hub, so there is no coherent way to hand it to an operator who sees nine machines out
  of forty. `manage_backups` should be read as "can eventually read everything, via a
  restore". #1b's per-machine file backups *do* get the usual `access.in_scope()`
  treatment.

**Also fixed here** (found while wiring this up): `packages.py` and `packages_web.py` were
never added to `HUB_RUNTIME_FILES`/`$HubRuntimeFiles` in 1.27.x, so a sparse hub install
or self-update fetched neither and the hub died on import at startup. Both lists now carry
every module `app.py` imports, and both say why.

**1b. Per-PC file backups** — ✅ **built, both directions (hub 1.30.0 + agent 3.9.0).** A
machine is configured, previewed, scheduled, walked, snapshotted, encrypted and uploaded;
and an operator browses what landed and pushes any of it back — onto the same machine or a
replacement. `restore_backup.py` remains the hub-independent path and now opens machine
archives too (`--list` / `--extract`).

**Agent side (3.9.0) — needs a signed release before the fleet gains it.**

**Built:**

- **`backup_paths.py` — the include/exclude grammar**, and the reason this feature is
  usable at all. `%Users%` — spelled `%User%` if that reads better, the two are the same
  token — fans out to every real profile (skipping Public/Default/service accounts), so one
  pattern covers everyone including whoever signs in next week, and `%User%\Scripts` covers
  a custom per-user folder. Note that `%User%` deliberately does NOT mean "the logged-on
  user": backups run as SYSTEM on a schedule with nobody signed in, so such a token would
  resolve to the service profile and silently back up one person's folder on a shared PC. Crucially
  `%Desktop%`/`%Documents%`/… resolve through each user's *User Shell Folders* registry, so
  they follow **OneDrive Known Folder Move** — a literal `C:\Users\bob\Desktop` on a
  KFM-enabled machine is an empty stub, and backing that up nightly while reporting success
  is exactly the failure this feature exists to prevent. **An unknown token is refused, not
  treated as a literal**: `%Userss%` would otherwise match nothing, forever, with a green
  run beside it. Excludes take the same tokens plus globs (`**` crosses folders); excluding
  a folder excludes its contents.
- **A `path_list` settings type.** `str_list` was wrong twice here — it rejects an empty
  list (a legitimate exclude list) and *lowercases every entry*, which would hand back
  `c:\users\%users%\desktop` after you typed it.
- **Backup Settings tab** (third tab on the Backups page): the fleet policy, the token
  reference, and a **live preview against a real machine** using the profiles its agent
  reported over the heartbeat. A pattern you cannot see the effect of is one you cannot
  trust, so the preview also names the problems — "carol has no %Documents% folder
  recorded" beats an empty list.
- **Per-machine overrides** (`backup_machine_config`) and a machine-page **Backup tab**.
  Path lists are **additive** to the fleet policy; only enabled/destination override.
  A machine that has merely *reported profiles* is not treated as an exception.
- **Chains, manifest and scheduler.** `backup_file_sets` + `backup_files` record one row
  per file *version*; `plan_next_run` forces a full every `files_full_every` runs;
  `files_tick` dispatches per machine on the same claim-then-queue discipline as deploys.
- **Offline machines catch up instead of losing a night** (hub 1.32.0). The roster now
  carries each machine's online status and `files_dispatch_once` refuses to queue for a
  machine that cannot answer. That is a *fix*, not a refinement: `start_file_run` stamps
  `started_at`, and `files_due_at` anchors on the newest attempt — so dispatching into
  the void reset the machine's clock and pushed the next real attempt out by a whole
  interval, meaning a laptop closed at 03:00 missed that night **and the following one**.
  It also burned the six-hour pre-signed upload URL minted alongside it. Because due-ness
  is persistent state, doing nothing while a machine is unreachable leaves it due, and
  the first pass after it reappears (≤ 1 tick, so a minute) backs it up.
- **"Back up now", per PC and fleet-wide** (hub 1.32.0). The button records a *request*
  (`backup_machine_config.run_requested_at`) and then tries to dispatch it, rather than
  talking to the machine. The indirection is the feature: pressing it on a laptop that is
  shut in a bag returns "queued — it will back up when it comes online" instead of an
  error or a command that expires unseen fifteen minutes later. One flag per machine, so
  the button is idempotent; cleared only once a real command exists, so a failure retries.
  Manual requests are dispatched **before** scheduled ones, and beat the interval — the
  moment someone presses it is usually just before a risky change, which is exactly when
  "backed up an hour ago, nothing to do" would be the wrong answer.
- **`backup.files_max_concurrent` (default 3).** Catch-up creates the herd this guards
  against: forty laptops that were off all weekend return within minutes of each other on
  Monday and would otherwise all push at once. Counted from `running` run rows rather
  than a counter, so a hub restart mid-pass cannot leak capacity, and `files_tick`
  retires abandoned runs *before* dispatching so one dead agent cannot permanently shrink
  the fleet's backup capacity. 0 disables the limit.

**Fixed here** — the machine Backup tab's **"Follow the fleet policy"** option had never
worked. `set_machine_config` reads `None` as "leave this field alone" (so a caller can
toggle one field without resending the path lists), but the console sends exactly that
for "follow the fleet" — so once a machine had been opted out it could never be opted back
in from the UI, and the new "Back up now" refused it forever with "file backups are turned
off for this machine". `False` is a real setting, so there was no natural empty value to
use: `backups.FOLLOW_FLEET` is now the sentinel that clears the override, and the web
layer translates an explicit JSON `null` into it while a *missing* key still means "leave
alone".
- **`rotate_chains` — the sharp edge.** The hub-DB rotation counts objects, which here
  would happily delete the full that four incrementals depend on. Rotation therefore works
  in whole chains, and orders them by `(created_at, rowid)` — the rowid tiebreak is
  load-bearing, since two chains written in the same second would otherwise order
  arbitrarily and rotation could delete the newer one.
- **Upload brokering without giving agents the credential.** S3 → a hub-minted pre-signed
  PUT scoped to `<prefix>/machines/<machine>/…`; WebDAV → the agent PUTs to the hub, which
  streams it onward (minting a scoped WebDAV credential needs provider-specific admin APIs
  that do not generalise). The object key comes from the run row, never the agent's report.
- **Per-machine derived keys** — `HKDF-SHA256(master, "fleethub-backup-machine:<name>")`.
  An agent must hold the key it encrypts with, so handing over the *master* would mean one
  stolen laptop decrypts the hub-database backup and every other machine's files. Restore
  stays one argument because the envelope header names the machine and the master
  re-derives.
- **`BackupEnvelope.cs`** — FHBK1 in C#, verified against Python-sealed fixtures **in both
  directions** (the agent tests read `tests/fixtures/*.fhb`; `test_backups.py` reads the
  `from-agent.fhb` they write). Two crypto implementations of one format drift silently,
  and the way you find out is a backup that will not restore.
- **`PathExpander.cs`** — the grammar against real registry data, driven by the same
  `backup_path_vectors.json` the Python tests use, so the console's preview and the
  machine's actual walk cannot disagree. Reads each user's *User Shell Folders*, and for
  users who are not signed in **temporarily mounts their NTUSER.DAT** (`HiveMount.cs`,
  SeRestorePrivilege as SYSTEM) — without that, every logged-off user's redirected folders
  would be unresolvable, which on a shared PC is most of them. A folder that cannot be
  resolved is reported MISSING, never defaulted to `<profile>\Desktop`, because the guess
  is what produces the empty-stub backup.
- **`VssSnapshot.cs`** — `Win32_ShadowCopy.Create(volume, "ClientAccessible")` via WMI.
  **`vssadmin create shadow` is deliberately not used: it is Server-only.** Snapshot
  failure is skip-and-report — the run continues against the live filesystem and names
  what it could not read, because a machine that backs up 99% and says so beats one that
  backs up nothing and explains why. Snapshots are deleted on dispose; a leaked one eats
  the volume's shadow storage and silently destroys the user's own restore points.
- **`BackupFilesExecutor.cs`** — expand, snapshot, walk, pack, upload, report.
  **Reparse points are never followed**: a profile contains junctions pointing at their own
  ancestors, so following them is an infinite walk that fills the disk — not hypothetical,
  it is the first thing that happens on a real profile. Change detection is size+mtime
  against a local per-chain manifest cache (hashing every file nightly would read the whole
  profile off disk to learn that nothing changed); the hash is computed only for files
  being uploaded. **A missing or mismatched cache forces a full and says so**, because an
  "incremental" against a chain the agent cannot see would be recorded by the hub as
  complete and discovered at restore.
- **tar → gzip → envelope, streamed through a pipe** rather than staged twice on disk.
  Tar specifically so `restore_backup.py` unpacks it with stdlib `tarfile` — a machine's
  backup stays recoverable with no hub and no agent, the same property #1a insisted on.
  Verified end to end: the C# tests write an archive, `test_backups.py` decrypts it with
  the master key and unpacks it with `tarfile`.
- **`BackupProfileReporter.cs`** — profiles ride the heartbeat, but only on change
  (content-hashed, hourly rescan, off the heartbeat's code path) so discovery can never
  delay a heartbeat into the hub's 90-second offline window.

**Restore (hub 1.30.0 + agent 3.9.0):**

- **A manifest browser, a folder at a time.** `manifest_listing` derives folders from the
  paths beneath them and returns one level per request, because a profile is 100k–500k
  files and the shape that works on a demo fleet — fetch everything, filter in the browser
  — is the shape that times out on a real one. What it lists is what is **recoverable**,
  not what was ever backed up: deleted files and rotated-away chains are already gone from
  the answer, so nobody is offered a restore that 404s. `?search=` spans folders for the
  case where you know the filename and not the path.
- **`plan_restore` resolves a selection against the manifest, hub-side.** A ticked folder
  cannot be expanded in the browser (it has only seen the folders someone clicked into)
  nor by the agent (the folder is usually gone — that is why someone is restoring it).
  Matching walks each row's ANCESTORS against the selection set rather than testing every
  selection against every row: with a 400k-row manifest and a few hundred selections the
  latter is tens of billions of comparisons. **A selection matching nothing is named back
  to the operator**, never silently dropped.
- **The plan lives in the database; the command carries an id.** `fleet.create_command`
  audits its params verbatim, so putting a 40,000-file list in them would write a
  multi-megabyte audit row per restore — into the database that is itself backed up — with
  **the decryption key in it**. The agent fetches the plan from an authenticated endpoint
  instead, which also means download URLs are minted at fetch time rather than dispatch,
  so a command picked up after a weekend offline does not wake up holding expired
  pre-signed links.
- **Download brokering mirrors upload exactly**: S3 → a pre-signed GET per archive;
  WebDAV → proxied by the hub. The agent names *"archive 2 of restore X"* and the hub
  resolves it against the stored plan — an agent-supplied object key would let any
  enrolled machine read any archive in the bucket, which here is a data breach rather than
  a corrupted manifest.
- **`RestoreFilesExecutor.cs`** — fetch plan, download one archive, decrypt, unpack only
  the wanted members, delete it, next. One at a time because a chain can be hundreds of
  gigabytes and a restore needing space for all of it at once fails on the machine that
  needed it most. Targets are resolved **before** the download, so an archive whose files
  all already exist is never fetched — on a re-run of a partly-failed restore that is most
  of them.
- **A partial restore is a FAILURE carrying the counts**, not a green row. "Restored 900
  of 1000 files" needs a human to look at which 100, and success means nobody ever does.
- **Cross-machine restore** (replacing dead hardware) is authorised at **both ends**:
  reading PC-3's files and writing them onto PC-9 are separate things to be allowed to do.
  The target agent is handed the *source* machine's derived key — a deliberate, audited
  widening of blast radius, and the entire point of "restore to a different machine".
- **`restore_backup.py --list/--extract`** — the console's restore button minus the
  console, for when the hub is gone or someone wants one file without pushing it onto a
  PC. Refuses to extract a hub-database artifact rather than handing it to `tarfile` and
  reporting a confusing parse error, and refuses any member that would escape the target
  folder (the classic tar traversal, on a file that came back over the network).
- **`archive_member()` is a second shared contract**, alongside the path grammar: the
  agent names tar members, and the hub names them again when planning a restore of an
  archive it never wrote. Drift there is not a crash — it is a restore that downloads the
  right archive, matches nothing, and reports every file missing. Both sides are tested
  against the `"members"` vectors in `tests/backup_path_vectors.json`.

**Fixed here** — `rotate_chains()`'s non-atomic deletes. The old code deleted every object
in a doomed chain and then every DB row, so a `client.delete()` failure in between left
archives gone from storage while the manifest still listed the whole chain as restorable.
The fix is structural rather than transactional: archives are deleted **newest sequence
first, the full LAST**, so a partial failure leaves a *prefix* of the chain — which is
still a valid, restorable chain describing an older moment — and each archive's manifest
rows are dropped immediately after that archive is actually gone. A partly-deleted chain
is simply still over the limit, so the next pass finishes it; no list of pending deletions
has to be kept anywhere.

**Considered and deferred — content-addressed blobs instead of chains.** Store each file
at `blobs/<sha256>` (encrypted) and make a snapshot nothing but a manifest of path → hash,
refcounting blobs exactly as `packages.py` already does for installer payloads. This
removes the full/incremental distinction entirely: every snapshot is independently
restorable because a "full" costs nothing extra when the unchanged blobs already exist, so
chains — and `rotate_chains` with its whole class of bugs — disappear. Static data would be
stored once ever rather than once per chain (in the worked example above, the 200 GB static
column becomes 50 GB), retention stays hub-side (delete a snapshot, GC unreferenced blobs),
and it needs no provider features so WebDAV behaves identically. It does *not* help a large
file that rewrites daily — that needs block-level chunking, a much bigger jump. Deferred
because it is a genuine rearchitecture plus a GC pass, and the chain design is built and
tested; revisit if chain fragility or static-data duplication becomes the real cost driver
in the field.

A related idea was rejected outright: **per-file objects at stable keys, letting S3
versioning be the retention mechanism.** It collapses on object count (100k–500k files per
profile, billed per PUT), per-file envelope overhead (~400 bytes of header on a 2 KB file),
Windows paths not mapping cleanly to S3 keys (case sensitivity, backslashes, the 1024-byte
limit — you end up hashing the path and still needing the manifest, so none of the
complexity is actually saved), and being S3-only, which would mean two backup engines
split by destination kind and retention moving out of the FleetHub console into the
provider's. If the storage model ever changes it should be a migration with a cutover, not
a user-selectable mode — two engines would mean every restore path supports both forever.

**Bucket versioning (S3/MinIO), if you enable it:** every archive gets a unique
timestamped key and is never overwritten, so versioning has no history to track and does
not protect the archives. What it *does* change is rotation — a `DELETE` on a versioned
bucket writes a delete marker instead of freeing bytes, so `files_keep_chains` becomes
"N chains visible, unbounded chains billed". **Pair versioning with a lifecycle rule
expiring noncurrent versions**, or storage grows forever while the console reports steady
state. Versioning is worth having only as a short (2–3 day) undo window against a
hub-side rotation mistake — never as the retention mechanism, since lifecycle rules
delete by count or age and are chain-blind, which is the exact bug `rotate_chains` exists
to prevent.

- **Storage: both S3-compatible and WebDAV, Admin's choice per destination.** An Admin can
  configure more than one destination, each typed `s3` or `webdav`. Credentials live in
  `.env`-backed secret storage (never in the `settings` table), referenced by an opaque
  destination id.
- **Credential scoping so no agent can touch another machine's backup**: for an S3
  destination, the hub holds the master key and mints short-lived **pre-signed PUT URLs**
  scoped to `backups/<machine>/...` on request. WebDAV has no native pre-signed-URL
  concept, so the equivalent there is a **per-machine subfolder + credential** minted/
  rotated by the hub.
- **Encryption: client-side, on the agent, before upload.** The agent encrypts each file
  with key material fetched over the authenticated channel before it ever leaves the
  machine — the storage provider only ever sees ciphertext. **Sharpest edge of this
  design**: the hub becomes load-bearing for restore, since losing its key material means
  losing the ability to decrypt existing backups. That key material needs its own durable-
  backup story (e.g. folded into the hub DB backup itself, or a separate documented
  export) — this must be designed explicitly, not left implicit.
- **Locked/open files: VSS shadow copy.** Before walking configured paths, the agent
  creates a Volume Shadow Copy and reads from the snapshot, so an open Outlook PST or a
  file the user has open is still captured consistently.
- **Hub DB backup**: scheduled `VACUUM INTO` (never a raw file copy — the DB is WAL and
  written live) → gzip → client-side encrypt → HTTPS PUT to a configured destination, with
  rotation.
- **Per-PC file backup**: agent walks configured paths, builds an incremental manifest
  (hash/mtime) to skip unchanged files, encrypts, and uploads straight to the destination.
- **Restore: hub-brokered, to a machine.** An operator browses a machine's backup manifest
  in the UI and picks "restore to `<machine>`" (or a different target, e.g. replacing
  hardware). The hub mints a scoped download (or WebDAV read credential) and supplies the
  decrypt key over the authenticated channel; the agent pulls the files, decrypts, and
  writes them to the original or an operator-chosen path.

*(The three parameters this spec left open were resolved as described above: count-based
retention — generations for the hub DB, whole chains for machine files; no resumable
upload, because a partial object in the bucket is indistinguishable from a good one, so
the artifact is built locally and PUT in one shot; and VSS via `Win32_ShadowCopy.Create`
with skip-and-report on failure.)*

---

## 📋 4. Active Directory integration   ·   build 4th — after Permission Groups exist to plug into

**Hybrid directory**: login via **Entra ID (OIDC)**, OU/computer data via **on-prem LDAP**
(Entra has no classic OUs, which is why OU targeting has to come from the on-prem side).

- **Login**: Entra as a second OIDC provider alongside the existing Google OAuth (same
  Authlib mechanism, just a second registration). The session's user shape is unchanged —
  it must keep a stable email/UPN so audit-log attribution stays coherent.
- **Authorization: Entra group → Permission Group mapping.** An Admin maps Entra security
  groups to existing Permission Groups. When a user's Entra groups match no mapping: check
  `ALLOWED_EMAILS` — if listed, full break-glass admin (today's escape hatch); if not
  listed, login succeeds (identity is valid) but the user has zero permission groups, so
  they see and can do nothing until an Admin assigns one. This reuses the Permission
  Groups foundation's break-glass rule rather than inventing a second one.
- **Machine ↔ AD computer sync, on-prem LDAP.** Entirely opt-in and Admin-configured — if
  no AD is set up, nothing runs. The hub binds to a DC over LDAPS with a read-only service
  account and syncs computer objects, joining to `machine_info` by hostname (consistent
  with the existing hostname-primary-key + serial-based dedup/merge). When enabled,
  default cadence is **hourly**, and a machine with **no AD match raises a review alert**
  (same pattern as today's duplicate-serial alert) rather than silently sitting ungrouped
  — both the cadence and whether the alert fires are Admin-configurable, not hardcoded.
  New `machine_info` columns: `ad_dn`, `ou`, `ad_object_guid`, `owner`, `last_logon_user`.
- **Target actions by OU/group.** Once OU/group data is synced, a Permission Group's
  `machine_scope` gains a second resolution mode — "AD OU/group" alongside the v1 explicit
  list — so deploys and commands can target an OU the same way.

**Constraints**: this changes the security perimeter, so the break-glass rule above is
what prevents a misconfigured mapping from locking everyone out. On-prem LDAP needs
hub→DC network reachability plus a service-account secret in `.env`.

**Still to decide during implementation**: exact LDAP query/paging strategy; whether
`owner`/`last_logon_user` come from LDAP attributes or need a separate signal.

---

## 📋 2. Remote view & control   ·   build last — highest technical risk, spike first

Live remote view of a managed PC's screen and input control from the console, with true
video from v1 and full secure-desktop (UAC/Ctrl+Alt+Del) support.

- **Why the agent can't just capture its own session.** The agent runs as a Windows
  Service in session 0. Session 0 Isolation (since Vista) means that session has **no
  rendered desktop at all** — no compositor, nothing ever drawn there — so screen-capture
  APIs have nothing to read. This is architectural, not a permissions gap: a helper must
  run *in* the interactive session.
- **Helper placement: SYSTEM, session-injected — not user-token.** Duplicate a SYSTEM
  token, retarget its session ID to the active console session, and launch the helper
  there. Running as SYSTEM-in-that-session (rather than as the logged-in user) is what
  additionally lets the helper open the secure desktop that hosts UAC consent prompts and
  Ctrl+Alt+Del — which is walled off from any user-token process by design. This closes a
  gap a simpler user-token helper would leave open, at the cost of careful token/privilege
  handling that needs validating against each supported Windows version.
- **Capture + input**: DXGI Desktop Duplication (GDI fallback) for frames, `SendInput` for
  mouse/keyboard, from the session-injected helper against whichever desktop object is
  currently active.
- **Codec: true video (H.264/WebRTC) from v1** — hardware-accelerated encode where
  available, feeding a WebRTC stream to the browser, rather than a simpler frame-diff/
  screenshot stream. **This is the single largest scope item on the whole roadmap**: a
  real encoder pipeline plus ICE/STUN/WebRTC signaling, not just a byte pipe. A throwaway
  capture spike (one frame, session-injected helper → browser) is strongly recommended
  before committing to the full pipeline, to de-risk the token/session mechanics
  independently of the codec work.
- **Transport (signaling)**: the agent is currently strictly outbound with no listening
  port; it dials a new WebSocket to the hub to exchange WebRTC signaling, and the hub
  relays to the operator's browser.
- **Consent: configurable per machine/group.** Attended (banner + user approval — doesn't
  work with no one logged in) vs. unattended (connects immediately, standard RMM
  behavior) — default unattended, overridable for sensitive scopes. Every session
  start/stop is gated by the `remote_control` capability + machine scope, and audited
  regardless of consent mode.

**Still to decide during implementation**: multi-monitor UX (switch vs. combined view);
STUN/TURN hosting (a self-hosted TURN server is likely needed, since agents sit behind
arbitrary NATs); the software-encode fallback threshold when no hardware encoder is
present.

---

## Build order

1. ~~**Permission Groups** (foundation)~~ — done, hub 1.26.0
2. ~~**#5 Deploy packages**~~ — done, hub 1.27.0 (pending an agent release)
3. ~~**#1 Backups**~~ — done: hub DB (1.28.0), per-PC files (1.29.0 / agent 3.8.0),
   console-driven restore (1.30.0 / agent 3.9.0). Both agent halves are pending a signed
   agent release.
4. **#4 Active Directory ← next** — Entra login + group mapping first, then LDAP sync
5. **#2 Remote view & control** — spike first, then build

Each item above has a decided approach; only small, implementation-level parameters
remain (noted per feature) and are meant to be resolved while writing that feature's full
implementation plan — not before.
