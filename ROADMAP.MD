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

Temperature alerts (hub 1.34.0): a high temperature is evaluated hub-side from a rolling
**average** over `hub.high_temp_avg_window_seconds` (default 300 s) against
`hub.high_temp_threshold`, so a momentary spike no longer flags a machine. It surfaces as a
real alert in the Alerts tab (`alerts.KIND_HIGH_TEMP`, raised by `app.evaluate_high_temp_once`
on a 30 s thread), one card per hot **episode**, held open until an operator dismisses it —
not on the Dashboard, which is now a plain live temp/status view. See below.

Remaining gaps: no per-metric retention (one global window for all readings), no
sampling/reporting-cadence setting, no per-metric or **per-machine** alert thresholds (the
high-temperature threshold and window are still global), no sensor *preference* list for
GPU/disk/network (only CPU temp has one — the others are on/off only). Fold these in
opportunistically; no dedicated feature push planned.

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

## ✅ 1. Backups via HTTPS   ·   1a hub 1.28.0, 1b hub 1.30.0 + agent 3.9.0, manual runs + offline catch-up hub 1.32.0, cancel hub 1.33.0

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
- **Cancel, per PC and fleet-wide** (hub 1.33.0). The honest part of the design is that
  cancel means three different things depending on how far the backup got, and the API
  says which happened rather than pretending it is one thing: a *queued request* is
  dropped; a *dispatched-but-unclaimed* command is expired via
  `fleet.cancel_command_if_pending` — a conditional `UPDATE ... WHERE status='pending'`
  that closes the race with `claim_commands` — so the PC never starts; a run the agent
  has **already claimed cannot be recalled** (claiming is at-most-once, there is no back-
  channel), so the run is marked `RUN_CANCELLED` to free its throttle slot and discard
  its result, but the PC finishes the pass it is in. A cancelled run stays terminal, so
  it stops counting against the throttle and anchors the due clock the same way a
  finished one does — a cancelled machine waits for its next cycle instead of
  re-dispatching on the next tick. The eventual late result is dropped by
  `ingest_file_result`, which also **best-effort deletes any object the cancelled run
  uploaded** — nothing in `backup_file_sets` would ever reference it, so rotation (which
  reads the DB) would never reap it, and a cancel that leaked one orphan per use is not a
  cancel anyone should trust.

**Fixed here** — `files_dispatch_once` could start a **second concurrent backup** on a
machine already running one. A manual request deliberately bypasses the interval check
(that is what makes "Back up now" work an hour after the last run), but "already running"
is not a due question, so a request landing mid-run slipped straight through and
dispatched again — two VSS snapshots and two archives racing into the same chain. Dispatch
now skips any machine with a `RUN_RUNNING` file run and leaves the request pending, so it
is served on the next tick after the current run ends. Found by the cancel tests, which
accumulated the duplicate runs the old path allowed.

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

## ✅ 4. Active Directory integration   ·   login 1.44.0, group mapping 1.49.0, LDAP sync 1.50.0

**Hybrid directory**: login via **Entra ID (OIDC)**, OU/computer data via **on-prem LDAP**
(Entra has no classic OUs, which is why OU targeting has to come from the on-prem side).

**✅ Login — built (hub 1.44.0).** Delivered more generally than specified: rather than a
second *Entra* registration, sign-in is **pluggable OIDC** (`OIDC_ISSUER` + client
id/secret, discovery via `/.well-known/openid-configuration`), so Entra, Okta, Authentik
and Keycloak are all a `.env` edit rather than a release. Google keeps its own
registration; the hub refuses to boot with no provider at all. The session's user shape is
unchanged, and `_complete_login` is shared by every provider so the authorization
decision, the audit identity and the users-directory upsert cannot differ by which button
was pressed. `permissions.email_from_claims` falls back to `preferred_username`/`upn`
(Entra omits `email` in many tenants) but only when the value looks like an address — a
bare `administrator` would otherwise collide with a granted mailbox name.

**✅ Authorization: directory group → Permission Group mapping — built (hub 1.49.0).**
The `ad_group_dn` column that shipped unused in 1.26.0 is now live: a group names
directory groups alongside emails, and membership in one grants that group exactly as
email membership does (same union semantics, same audit trail). Resolved parameters:

- **Tokens are opaque, matched casefolded-exact.** Entra sends group object GUIDs, ADFS
  and on-prem issuers send DNs, others send names — one comparison rule covers all three
  without a mode flag, which is what the column was left opaque for. Exact and never
  prefix/substring: a loose match here silently hands a stranger an operator's
  capabilities, and nothing in the UI would look wrong. Claims read are `groups`, `roles`
  and `wids`, unioned, because which claim carries membership is per-issuer and not
  something an admin should have to know.
- **The session carries the INTERSECTION of claimed and mapped, not the full claim.**
  Load-bearing rather than an optimisation: Flask sessions are signed cookies with a ~4 KB
  budget and a user in 200 Entra groups is ~7 KB of GUIDs — i.e. it would break sign-in
  for precisely the most-privileged accounts. A claimed group nothing maps can never
  affect authorization, so dropping it at the door loses nothing and bounds the session by
  the admin's own config. The cost is that a *newly added mapping* needs a fresh sign-in,
  which a new directory *membership* would have needed anyway.
- **Group-claim overage is reported, never silently refused.** Entra stops emitting
  `groups` past ~200 and sends a Graph pointer this hub does not follow. That is
  indistinguishable downstream from "in no mapped group", and only one of the two is a
  misconfiguration — so the refusal names the actual cause instead of leaving an admin
  debugging a correct mapping that appears to do nothing.
- **Discovery is solved in the UI, not the docs.** The group editor lists the tokens the
  admin's *own* sign-in carried (display-only, gated on `manage_permission_groups`,
  truncated), click-to-add. Otherwise configuring this means a trip to the Entra portal to
  copy a GUID, with no way to tell which claim your tenant actually emits.

**Deviation from the spec below, deliberate:** the spec said an identity matching no
mapping should be *admitted* with zero groups. It is still **refused**, as before. The two
outcomes are functionally identical — neither can read or do anything — so the refusal was
kept for being the one that tells the operator and their admin what is wrong, and because
softening it would weaken an existing control for no gain. Revisit if an empty dashboard
turns out to be the better support experience.

`members_of_machine()` is knowingly incomplete and cannot be otherwise: it answers "who
has access **by name**", and cannot enumerate a mapped directory group's members, because
the hub never queries a directory for *users* — it only ever learns that one person is in
one group, at their sign-in.

**✅ Machine ↔ AD computer sync + `ad_ou` scope mode — built (hub 1.50.0).**
`directory.py` (model, flask-free) + `directory_web.py` (status, OU list, "sync now"), a
Settings → Active Directory tab, `directory.*` settings, an `ad_unmatched` alert kind, and
the third machine-scope mode in the permission-group editor. Resolved parameters:

- **`ldap3`, imported lazily.** The one dependency taken purely for this feature. LDAP is
  a real ASN.1/BER protocol over TLS — not the SigV4 situation, where hand-rolling ~100
  lines of `hmac` beat taking 80 MB of botocore. It is pure Python (~1 MB, no compiler),
  and `directory.py` imports it *inside the connect path*, so a hub that never turns AD on
  neither needs it installed nor pays the import. Missing-with-the-feature-on is reported
  as a configuration error with the install command, not a crash loop.
- **Paging strategy (the roadmap's open question): always paged, no exceptions.** AD's
  default `MaxPageSize` is 1000 and it does **not** error when a search exceeds it — it
  returns the first page and a referral. An unpaged search on a 2000-computer domain would
  therefore sync half the fleet, mark the other half as having no AD object, and raise a
  review alert on a thousand healthy machines, with nothing in any log to say why.
- **`owner`/`last_logon_user` (the other open question): split, honestly.** `owner` comes
  from `managedBy` (stored as the leaf CN, since showing an operator a full DN where a name
  belongs is not an improvement on showing nothing). **`last_logon_user` was NOT
  implemented, because it is not an AD attribute** — `lastLogonTimestamp` is when the
  *computer account* authenticated, which is a different fact, and is stored as
  `ad_last_logon`. A real last-logged-on *user* needs an agent-side signal; noted, not
  faked.
- **The join is hostname-based**, leading DNS label, case-insensitively, `dNSHostName`
  preferring `name` — consistent with the existing hostname-primary-key model. Two AD
  objects claiming one hostname (a re-join that left the old account behind) uses the first
  and reports the collision, so the OU cannot flip between syncs.
- **AD is authoritative for AD's fields and nothing else.** No machine records are ever
  created from the directory — AD computers without the agent are counted (a useful
  number) and otherwise ignored, because inventing machines from AD fills the console with
  rows that never report. Conversely a machine leaving AD has its AD fields **cleared**:
  a stale OU on a deleted computer account would keep granting access through an OU-scoped
  group indefinitely, with nothing on screen to suggest it.
- **`ad_ou` scope resolves at cache-build time, not per request.** Safe for exactly one
  reason, made explicit in the code: only a directory sync can change which machines are
  in an OU, and `sync_once` invalidates the permissions cache when any machine's OU
  actually moved (and only then — firing every pass would dump the cache hourly for
  nothing). **Containment is component-wise**, so a parent OU covers nested children while
  `OU=Clinical` never captures `OU=NotClinical` — a string `endswith` here is an
  access-control bug, not a display one.
- **Cleartext binds refused by default**, and the refusal lives in `validate_config()`
  ahead of the library import — a safety check must not be reachable only on hubs that got
  a dependency right. Found by a test that asserted on the message: with `ldap3` absent,
  four config-validation checks had been passing on the *import* error instead.

**Not covered by tests, and worth saying:** everything above is exercised against literal
LDAP entries through `sync_once(fetcher=...)`, with no directory and no `ldap3` present.
What that cannot cover is the ldap3 call itself — the bind, the TLS handshake and the
paged-search generator are a thin adapter that has never run against a real DC. Expect the
same shape of finding the TURN work produced on first contact with hardware.

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

## ✅ 6. Search & pickers across the console   ·   hub 1.36.0 + agent 3.11.0 / companion 2.11.0

Built. The Asset Inventory page gained a search box and click-to-sort columns; the
Permission Groups editor's two pickers became real search-as-you-type comboboxes over a
single shared widget; and a new **Service Tag** identifier was added end to end. The open
parameters were resolved as:

- **Inventory search & sort are client-side**, over the rows `/api/machines` already
  returns (itself scope-filtered). The roadmap left server-vs-client open and preferred
  client-side until fleet size demands otherwise: filtering in the browser is instant and
  costs no round-trip per keystroke, and the roster is small. Search spans Name, Asset Tag,
  Serial and Service Tag; sort covers every column (name is the stable tiebreak, online
  sorts before offline, missing temps sink). **Sort persists in `localStorage`**
  (`fleethub.inventory.sort`) so it survives the 30 s auto-refresh and a reload.
- **The shared widget is `static\js\autocomplete.js`** — one combobox behind both pickers
  (and available to any future field). It builds options with `textContent`, race-guards
  async sources by request id, and — the load-bearing detail — mounts its listbox inside
  the input's own parent rather than `document.body`, because these pickers live in a
  `<dialog>` whose top layer paints over any page-level `position:fixed` dropdown. Keyboard
  nav (↑/↓/Enter/Esc), and Enter with nothing highlighted still falls through to the
  field's own "add exactly what I typed" so free text is never blocked.
- **The machine picker** filters the loaded fleet across name + all three identifiers,
  showing the matched identifier as the option's sublabel; it replaces the native
  `<datalist>`. **The member picker** queries the Registered Users directory (#8) through a
  new **`GET /api/permissions/directory?q=`**, deliberately gated on
  `manage_permission_groups`, not `manage_users`: choosing who to add to a group is part of
  managing groups, and the endpoint returns only email/name/username — never the
  phone/title/department/notes the Users API guards. Both pickers still accept free text, so
  a machine can be scoped before it enrolls and a member added by an email that has never
  signed in.
- **New `service_tag`** — the chassis/enclosure serial (`Win32_SystemEnclosure.SerialNumber`,
  where Dell's Service Tag lives), distinct from the BIOS serial we already store as
  `serial_number`. Collected by **both** agents (companion.py and the C# agent) with the
  same placeholder filtering as `asset_tag`, stored on a new `machine_info.service_tag`
  column (COALESCE-updated like the others, and backfilled by the duplicate-serial merge),
  surfaced on the machine page's Identity card and as an Inventory column, and searchable.
  An older agent that never reports it simply leaves the column null.

**Agent halves pending a signed release**: the C# agent's `service_tag` reporting ships in
source (3.11.0) but needs the usual build + `sign_release.py --sign-agent` + asset upload
before the fleet gains it; companion.py 2.11.0 is re-signed and ready. Until then, machines
show a blank Service Tag, which is exactly the older-agent fallback above.

<details>
<summary>Original spec</summary>

Search box and sortable columns on the Asset Inventory page, generalized into a shared
search-as-you-type widget reused everywhere else the console currently makes you type a raw
value with no help. Today (`templates\inventory.html` + `static\js\inventory.js`, backed by
`GET /api/machines` in `app.py`) the Inventory table shows Machine, Status, Model, Serial, Asset
Tag, Last Temp, Last Seen with **no search box and no user-facing sort** — the only ordering is a
hardcoded JS `.sort()` (online machines first, then alphabetical by name), not exposed as UI or
toggleable. The Permission Groups page (`templates\permissions.html`, `static\js\permissions.js`)
turns out to be *more* primitive still:

- **Machine picker** (`permissions.html:58-66`, `permissions.js:341-349`) is a text input wired to
  a native HTML `<datalist>` populated from `GET /api/machines` on page load — browser-native
  suggestion of every hostname, no fuzzy match, no query-as-you-type against the server, and free
  text is accepted unvalidated.
- **Member picker** (`permissions.html:70-74`) is a plain `<input type="email">` with **zero**
  suggestions — validation is just "contains `@`" (`permissions.py:239-249`). A function
  `permissions.known_emails()` already exists with a docstring saying it's "for the admin UI's
  picker" (`permissions.py:588-590`) but is never called anywhere — dead code today.
- The one real precedent for a proper search-as-you-type widget already in the app is the Backups
  restore browser (`static\js\backup-tab.js:370-441`, `#restore-search` hitting a `search=` query
  param server-side) — generalize that pattern into a shared component rather than writing a
  fourth one-off implementation.

**Scope:**
- **Inventory search**: by Name, Asset Tag, Serial Number, Service Tag (and similar identifiers).
  Whether this filters server-side (extend `/api/machines` with a `q` param) or client-side over
  already-loaded rows is an open parameter — the fleet sizes seen so far are small enough that
  either works; server-side is the safer default if that changes.
- **Inventory sort**: by Name, Status, Model, Serial, Asset Tag, Service Tag, Last Seen — click a
  column header to sort asc/desc, replacing today's hardcoded order. Persisted (`localStorage` or
  a query param) so it survives a page refresh.
- **New `service_tag` field.** `machine_info` currently has `asset_tag` and `serial_number` but no
  separate Service Tag column. Add `service_tag` alongside `asset_tag`, collected in `companion.py`
  the same way `asset_tag` already is (BIOS/WMI), and surfaced in `inventory.html` next to Asset
  Tag.
- **Permission Groups machine picker** gets the same widget as Inventory search, replacing the
  native `<datalist>` — same field set (Name, Asset Tag, Serial, Service Tag).
- **Permission Groups member picker** gets a search-as-you-type dropdown against the Registered
  Users directory (**#8**, below) instead of the bare email input — typing a name, username, or
  email pops up matching registered users to pick as a member. **Hard dependency on #8**:
  there's nothing to search until the Users directory exists. `permissions.known_emails()` is the
  likely starting point for the backend query, once it's driven by the `users` table instead of
  scanning existing group membership.
- **No permission-model changes for Inventory/machine-picker** — read/display only, already
  covered by existing view scoping. The member-picker change reads from #8's new `manage_users`
  data but doesn't require it to add someone as a member.

</details>

---

## ✅ 8. Registered Users directory   ·   hub 1.35.0

Built. `users.py` (model, flask-free) + `users_web.py` (admin API + page), a `users` table,
a Users page and nav link, and a new `manage_users` capability. Delivered as specified below,
with these decisions worth carrying forward:

- **`upsert_from_login` does NOT overwrite an existing `full_name`.** A repeat login carrying
  a different display name (Google claims drift — "C. Carr (Contractor)" one day) must not
  silently stomp an admin's or the user's own edit. It always re-stamps `last_login_at`, and
  fills in a name only when the row has none (a manually-added user's first sign-in). Not
  audited — a login is not a config change, and one audit row per sign-in would bury the
  admin edits the trail exists for.
- **Whole-record update semantics**, deliberately unlike `update_group`'s "None means leave
  alone". The editor always sends every field, so there is no ambiguous "unchanged" state and
  no sentinel-value trap like the backup Follow-Fleet `None` bug — a field cleared in the form
  is actually cleared.
- **Delete is a profile delete, not an access change.** `delete_user` never touches
  `permission_group_members`; membership stays keyed on email independently, so removing a
  directory entry cannot revoke anyone's access (a test asserts exactly this). This is a
  profile store, not a second authorization layer.
- **`manage_users` is separate from `manage_permission_groups`** so the two delegate
  independently — editing a phone number is not the same trust as granting capabilities. A
  member of a group holding every capability *except* `manage_users` is still refused the whole
  Users API (tested).

The optional field set landed as `phone`, `title`, `department`, `notes` (real columns, not a
JSON blob). The open question about a manually-added email that never matches a real Google
account resolved to "nothing special happens" — membership is keyed on email regardless, so a
never-logged-in row simply shows `last_login_at` = Never.

<details>
<summary>Original spec</summary>

A real user directory — full name, username, email, plus room for more — instead of today's bare
email strings threaded through the app. No `users` table exists anywhere today: identity is a
lowercased email string (`permission_group_members.email`, `fleet_favorites.owner_email`,
`audit_log.actor`), and login is pure Google OAuth (`app.py:1199-1229`) — the session holds
`{"email", "name", "picture"}` straight from Google's claims, nothing persisted.
`permissions.normalize_email()` is the only "profile" concept that exists today (strip/lowercase).

- **New `users` table**: `email` (normalized, unique key — same normalization as
  `permissions.normalize_email()`), `full_name`, `username`, plus fixed optional columns —
  `phone`, `title`, `department`, `notes` (open parameter: confirm the exact optional set when
  this becomes a full implementation plan). Real columns, not a JSON blob, matching the rest of
  the schema's style; more columns can be added later via an ordinary migration.
- **Populated two ways**: (a) auto-upserted from Google OAuth claims (`email`, `name`) on every
  successful login, so anyone who's ever signed in appears automatically; (b) an admin can
  manually add a user via the new Users tab (full name + username + email, optional fields) ahead
  of their first login — mirrors how `permission_group_members` already lets you add someone by
  email before they've ever logged in.
- **New Users tab** in the left nav (`templates\partials\_sidebar.html`, after Permission
  Groups, same capability-gated-`<a>`-with-active-class pattern as the rest of the sidebar) —
  list/search/add/edit registered users.
- **New `manage_users` capability**, added to the existing vocabulary (`view`, `issue_commands`,
  `remote_control`, `deploy_packages`, `manage_backups`, `manage_settings`,
  `manage_permission_groups`) — gates the Users tab, kept separate from
  `manage_permission_groups` so the two can be delegated independently.
- **Feeds #6's Permission Groups member picker**: this directory is what makes "a registered user
  pops up while you're typing" possible at all.

**Still to decide during implementation**: what happens to a Permission Group membership row
when a manually-added user's email doesn't match any real Google account (typo, or they simply
never sign in) — likely nothing, since membership is keyed on email today regardless of whether a
`users` row exists; whether editing a user's `full_name`/`username` after auto-registration should
be admin-only or self-service.

</details>

---

## ✅ 2. Remote view & control   ·   hub 1.40.0 (+ agent, pending a signed release)

Built. Live remote view **and control** of a managed PC over **true H.264/WebRTC** from v1.
The user chose to go straight for the full pipeline (no throwaway spike) and to make **the hub
itself the TURN server**. Delivered as specified below; the decisions and their resolutions:

**Live-test follow-up (hub 1.40.0):** the first on-hardware test connected everything except
media — the agent logged `ice_servers=0` because TURN wasn't configured yet. So TURN is now
**provisioned by the hub installer** (generates `REMOTE_TURN_SECRET`, writes `turn/.env`,
optionally runs `docker compose` via a new `docker-compose.windows.yml`, seeds the STUN/TURN
URLs into Settings, and prints generated secrets once) and **fully manageable in the UI**: a new
**Settings → Remote Control** tab (the section previously had no panel and never rendered) with a
TURN status card — secret set?, URL counts, and the live **ICE-servers-per-session** count — plus
set/rotate of `REMOTE_TURN_SECRET` (`manage_settings`; writes `.env` + applies live; coturn's
`--static-auth-secret` synced by hand).

- **Session injection (agent, `Remote/SessionInjector.cs`)** — the service duplicates its own
  SYSTEM token, retargets `TokenSessionId` to the active interactive session (enabling `SeTcb`),
  and `CreateProcessAsUser`s the **same binary** with `--remote-helper` onto `winsta0\default`
  as **SYSTEM-in-session**. One binary, one signature; the helper reuses the self-update trust
  root and the agent's enrollment identity to authenticate its signaling. The target session is
  chosen by enumerating `WTSEnumerateSessionsW` and preferring `WTSActive` (console wins ties),
  **not** `WTSGetActiveConsoleSessionId` alone — see the field-test findings below.
- **Capture + encode (`ScreenCapture.cs` + `H264Encoder.cs`, via Vortice)** — DXGI Desktop
  Duplication (GDI `BitBlt` fallback) → BGRA→NV12 (`ColorConvert.cs`) → a Media Foundation
  H.264 encoder → Annex-B. The software encoder path is the shipped baseline; hardware/async-MFT
  is the on-hardware tuning follow-up. **The whole capture→encode path was validated on real
  hardware** (a standalone `--remote-capture-test` produced a valid H.264 stream: SPS/PPS/IDR
  with inline parameter sets per keyframe).
- **WebRTC (`RemotePeer.cs`, SIPSorcery)** — pure-managed `RTCPeerConnection` (so the
  single-file agent is unaffected): an H.264 send track fed by the encoder, a `control`
  DataChannel for input, offer/answer + trickle ICE. The agent is the offerer.
- **Signaling relay (`remote.py` + `remote_web.py`)** — HTTP polling, not a WebSocket: the
  agent has no listening port and the hub's Socket.IO is polling-only, so both sides poll with
  an `after_seq` cursor (the fleet-terminal model). Agent side is bearer-auth + machine-owned;
  console side is `remote_control` + machine scope; every start/stop is audited. New tables
  `remote_sessions` / `remote_signals`; session TTL swept on the 30 s housekeeping heartbeat.
- **Hub as TURN (`turn/`)** — coturn on the hub host, credentials **minted by the hub app**
  via the standard TURN REST scheme (HMAC of `REMOTE_TURN_SECRET` in `.env`), so there is one
  secret and no per-user database. On a **Windows** hub the delivery mechanism changed from a
  Docker container to a **dedicated WSL2 distro** after field testing (see the findings below);
  that adds Windows 11 22H2+, the Store WSL package, a Hyper-V firewall rule, a boot task, ~3 GB
  of disk, and globally-mirrored WSL networking to the requirements.
  **The credential interop was validated against real coturn**
  via Docker (`tests/test_turn_interop.py`): a hub-minted credential authenticates and
  allocates; a wrong one is refused.
- **Input control (`InputInjector.cs`)** — browser input over the DataChannel → `SendInput`
  (mouse move/buttons/wheel, keyboard via a `KeyboardEvent.code`→VK map with a Unicode
  fallback), coordinates mapped from normalised capture space to the monitor's virtual-desktop
  rectangle. Ctrl+Alt+Del via `SendSAS`.
- **Consent (`ConsentBanner.cs`)** — `remote.consent_mode`: **unattended** default (connects
  immediately) vs **attended** (a fail-closed, auto-denying prompt to the logged-in user via
  `MessageBoxTimeout`).
- **Viewer** — a **Remote** tab on the machine page (`remote.js`), gated on `remote_control`.

**Field-test findings (2026-07-27), from a hub + TURN + agent all on one Windows box.** Three
faults, none of which the Docker-based interop test could have caught, because each lives in a
layer that test stubs out:

1. **coturn was unreachable on Windows and looked healthy.** It had been started from the Linux
   `docker-compose.yml`, whose `network_mode: host` binds inside Docker Desktop's VM — nothing
   listened on the Windows host. The container reported `Up 2 days` with **zero requests in its
   log**. Fixed by using `docker-compose.windows.yml`; a raw STUN probe plus a real Allocate
   against a hub-minted credential is now the verification of record.
2. **A UI secret rotation had desynced the hub from coturn.** The **Settings → Remote Control**
   card writes the hub's `.env` and applies live, but coturn validates against its own
   `--static-auth-secret` — so rotating from the console silently breaks every allocation until
   `turn/.env` is updated and coturn restarted. The UI showing the value once is the mitigation;
   the failure mode is now in the `turn/` troubleshooting table.
3. **The helper captured the wrong session, and nothing reported an error.**
   `WTSGetActiveConsoleSessionId` names only the *physical console*, which on an RDP-administered
   machine is typically signed out (`WTSConnected`, no user, no composited desktop). The helper
   injected there, DXGI found no output to duplicate, GDI dutifully blitted an empty desktop, and
   the pipeline streamed **a flawless black screen**. Fixed by preferring `WTSActive`. The
   lesson worth keeping: every layer reported success, so the diagnosis had to come from the
   helper's own `session=1` line rather than from any failure.

**Resolved — the installer now builds a WSL2 relay instead of a container.** The remaining
failure after the fixes above was cross-NAT relay from a Windows/Docker coturn: allocations
succeeded for **both** peers and ICE still reached `failed`. The relay sockets sit on the Docker
bridge (`Relay address to use: 172.x.x.x`), so `--external-ip` advertises a public candidate
whose egress source port Docker is free to rewrite; inbound DNAT works, which is exactly why the
allocation counts look healthy while every connectivity check fails.

`install.ps1` therefore provisions coturn **natively in a dedicated `FleetHubTurn` Ubuntu 24.04
WSL2 distro** (`wsl --install --name`, so there is no rootfs URL to maintain and no collision
with an Ubuntu the admin already uses), under systemd with `Restart=always`, with
`networkingMode=mirrored` merged into `.wslconfig` so the distro shares the host's network
namespace and relay ports stay symmetric. It also opens **both** firewalls, registers a boot
task, and verifies itself with a real Allocate. `docker-compose.windows.yml` is kept but demoted
to LAN/credential testing; `docker-compose.yml` remains correct for a Linux hub.

Four things that took real work to get right, all of which fail *silently* if got wrong:
- **`wsl.exe` emits UTF-16LE**, so every `-match` against `wsl -l -q` fails and distro/Docker
  detection reports "nothing found" — meaning the warning about restarting the operator's
  containers would never fire. One `WSL_UTF8=1` wrapper fixes it at source.
- **`WriteAllLines` emits CRLF** on .NET Framework; a trailing `\r` on `static-auth-secret`
  produces 401s indistinguishable from a secret desync.
- **WSL distros are registered per-user**, so the boot task must run S4U as the installing user.
  A SYSTEM task — the pattern every other service here uses — cannot see the distro at all.
- **`Get-Date -UFormat %s` returns local time** on PowerShell 5.1. The installer's verification
  credential was ~3 h stale on a UTC-3 box, so coturn rejected it as expired and verification
  would have condemned a working relay.

Caveats carried forward: mirrored mode is a **global, persistent** change affecting every WSL
distro including `docker-desktop`, and applying it needs one `wsl --shutdown` that restarts every
container (the installer detects this, names what will stop, and asks separately, default *no*).
A Linux host/VM beside the hub remains the lower-risk option. And the SNAT mechanism above is
strongly indicated by the evidence but was never proven from an external vantage point.

**Open, on-hardware follow-ups** (validate/tune together, as agreed): the **secure desktop**
(capturing/injecting on Winlogon during UAC — the SYSTEM-in-session enabler is in place, the
DXGI re-duplication + SendInput-to-Winlogon is the piece to confirm); **hardware H.264** encode
(async MFT + shared D3D11); **per-machine consent override** (global setting works today);
multi-monitor UX; and the software-encode fallback threshold.

<details>
<summary>Original spec</summary>

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

</details>

---

## ✅ 7. Localization / multi-language UI   ·   engine + chrome hub 1.51.0; Inventory/Alerts/Audit/Users 1.54.0; History + Permission Groups 1.55.0; Settings/Packages/Backups/Machine + browser-language fix 1.56.0

Shipped languages **English, Spanish, German**. A language is data — one JSON file in
`hub/locales/` plus a row in `i18n.LANGUAGES` — so a fourth needs no schema or code-shape
change.

**✅ Engine — built (hub 1.51.0).** `hub/i18n.py` (flask-free, like every other model
module), `hub/locales/{en,es,de}.json`, `hub/static/js/i18n.js`, and
`tests/test_i18n.py`. Both open parameters are now resolved:

- **In-house JSON string table, not flask-babel/gettext.** Three reasons, in order of
  weight. (1) *Most of this console's user-facing text is in JavaScript, not Jinja* —
  templates are ~1.5k lines against ~9.4k lines of vanilla DOM building, so gettext would
  have solved only the server half and the client half would have needed a second
  mechanism regardless: two catalogs, two extraction paths, guaranteed drift. One JSON
  catalog feeds both. (2) *`.mo` files are a build step and this hub has no build step* —
  deployment is a whole-directory file mirror with no compile stage, exactly the situation
  in which a binary artifact goes silently stale and the hub serves last week's
  translations with nothing on screen to say so. JSON is both source and runtime format,
  so there is no stale-artifact state to be in. (3) It matches the hand-rolled-SigV4
  precedent: ~250 lines, no new dependency on a 0.3 MB sparse install.
- **Per-user preference over a fleet default, browser last.** `resolve()` is
  user choice → `hub.default_language` → `Accept-Language` → English. The browser ranks
  *below* the fleet setting deliberately: an admin who sets the fleet to Spanish has made
  a statement about this deployment, and a stray `Accept-Language` from someone's phone is
  not a preference they expressed about this console. A personal choice always wins, so
  changing the fleet default never overrides an operator who has already picked. The
  per-user column is on `users` and deliberately **not** in `_EDITABLE_FIELDS` — the Users
  editor does whole-record updates and would otherwise reset a colleague's language when
  an admin corrected their phone number. `POST /api/language` is gated on `@login_required`
  and nothing else: requiring `manage_settings` to change the language of *your own*
  console would mean only admins could read the UI in their own language, which inverts
  who the feature is for.

**Fallback is the load-bearing property.** Catalogs are merged server-side (English
overlaid with the target language), so a key missing from `es.json` renders the English
string, never a raw `nav.dashboard`. Partial catalogs degrade to mixed-language — ugly but
usable — rather than to visible key names. The merge happening once on the server is also
why `i18n.js` has no fallback logic of its own: one implementation, one behaviour.

**Everything this engine can get wrong at runtime is silent**, which is why
`tests/test_i18n.py` exists and what most of it is about: key parity in both directions
(missing *and* stale), placeholder parity (a `{machine}` dropped in translation yields a
grammatical alert that never names the machine), and a regex scan of every template and JS
file asserting each literal `t('…')` key exists in `en.json` (a typo'd key renders its own
name onto the page and raises nowhere). `_SafeFormat` means a translator's typo cannot 500
a page, and the embedded catalog JSON has `<` escaped so a stray `</script>` cannot blank
the console.

**Known limitation:** `plural()` implements the one/other rule only. Correct for all three
shipped languages, and explicitly wrong for languages with more forms (Polish, Russian,
Arabic) — adding one means adding a rule table, not just a catalog.

**Every page is converted (hub 1.56.0).** Chrome (sidebar, topbar, language picker) plus
login, denied and dashboard in 1.51.0; Asset Inventory, Alerts, Audit Log and Users in
1.54.0; History and Permission Groups in 1.55.0; **Settings, Packages, Backups and the
Machine page — with `machine.js`, `remote.js`, `fleet-terminal.js`, `fleet-pty.js` and
`fleet-favorites.js` — in 1.56.0.** Template and JS together throughout, since a page
converted by halves is a page that reads in two languages at once. Three conventions came
out of the first batch and held for the rest:

- **A sentence is a catalog entry; a sentence assembled from clauses is not.** The
  high-temperature alert body was built by `+` from five fragments (`'Windowed'` /
  `windowMins + '-min'`, the threshold clause, an optional peak parenthetical, the "hot
  since" tail). Concatenation reads fine in English and is unfixable in translation, where
  clause order and punctuation differ — so it became four whole sentences
  (`ongoing`/`ongoing_peak`/`ended`/`ended_peak`), each complete, each with named
  placeholders. Only the window itself stays a fragment, with its own key and an authoring
  note saying it appears mid-sentence. Same reason `"machine" + (n === 1 ? '' : 's')`
  became `tPlural`.
- **Converting a string is also the moment to stop building it with `innerHTML`.**
  Three load-failure paths assigned a literal `<tr>…</tr>` string; a catalog value dropped
  into markup is a new injection surface (catalogs are markup-free by test, but the test is
  the only thing standing there). They now build DOM and set `textContent`, matching how
  the rest of each file already renders data.
- **`common` earns entries only on the second page that needs them.** `delete`, `edit`,
  `save`, `close`, `optional`, `never`, `loading` moved there once Users wanted what
  Inventory and Audit already had; anything that reads the same in English but differently
  in context stays page-scoped.

**Server-supplied UI text — resolved in 1.55.0, and no longer just "mechanical".** The
Permission Groups page turned out not to be convertible from the template alone: its
capability list is rendered from `GET /api/permissions/capabilities`, so the labels and
descriptions an admin reads before granting `issue_commands` lived in a Python dict
(`permissions.CAPABILITY_LABELS`). The settings `REGISTRY`'s `label`/`help`/`unit` are the
same shape and the larger instance of it. Three decisions, which the settings conversion
should follow rather than re-litigate:

- **The English text moves into `en.json`; the Python module keeps only the vocabulary.**
  The alternative — keep the dict as the English source and let a catalog entry override
  it — needs the same string in two files, which is exactly how a catalog rots, and it
  fails the key-parity test besides (`es.json` would carry keys `en.json` does not).
  `en.json` is already the schema, so it is where the string belongs. `CAPABILITY_LABELS`
  is gone; `permissions.CAPABILITY_TEXT_KEY` names the catalog prefix instead.
- **Resolved server-side, in the caller's language, not in the browser.** The key is built
  from the capability *name*, and a key assembled in JS is invisible to the literal-key
  scan — so a capability added with no catalog entries would render
  `permissions.capability.foo.label` on screen with nothing to catch it. Doing it in
  `permissions_web.py` turns that same omission into a failing test
  (`test_server_supplied_ui_text_is_in_the_catalog`), which is the whole point: this is
  the one page where the text says what an operator is about to grant.
- **`i18n.set_language_provider()` / `i18n.current()`** is how a JSON blueprint learns the
  language of the request in flight. Templates are handed their language explicitly
  (`template_context`), which is right for something that renders once in one language;
  a blueprint serving user-facing text has no language argument anywhere in its call
  chain, and threading one through all ten blueprint factories to serve the two that need
  it is worse than one hook installed by `app.py`. `i18n` stays Flask-free — it calls a
  callable it was handed, and returns English when nobody installed one (the bare test
  apps, background threads).

**Two more conventions from the 1.55.0 batch:**

- **Markup around a string does not survive translation — restructure rather than split.**
  The break-glass note wrapped `ALLOWED_EMAILS` in `<code>`. Catalog values are plain text
  by test (`t()` is autoescaped, so a tag in a translation renders as visible angle
  brackets), and splitting the sentence around the tag is the clause-assembly mistake in
  another form. It became one sentence with the `.env` key name passed in as a
  placeholder — which also stops the key from being translated by accident.
- **A line carrying two independent counts takes pre-pluralised fragments.** `plural()` is
  one/other over a single count, so "{date} • N machines • M readings" cannot be one entry
  without a four-way plural. Each count is pluralised on its own and dropped into a
  container string (`history.meta`, `permissions.confirm_delete_directory`), with a `_`-
  prefixed authoring note in `en.json` saying so. The precedent is `alerts.high_temp._window`.

**Four more from the 1.56.0 batch:**

- **A wire value shown to an operator needs a display name, and the map is spelled out.**
  Deployment statuses (`in_flight`), backup run statuses and triggers, remote session
  states — each gets one literal `t()` per value in a small lookup object rather than a key
  built by concatenation, because only literal keys are visible to the key scan. An
  unrecognised value falls back to showing itself, which beats showing a key name when a
  newer hub adds one.
- **Panel titles and metric labels are functions, not strings.** `machine.js`'s `METRICS`
  table is built at module load; a string captured there would be whatever language the
  file was written in. `label: () => t(...)` defers it to render time.
- **Inline markup inside a sentence goes, or the sentence splits at the tag.** Three cases:
  `<code>ALLOWED_EMAILS</code>` became a placeholder (above), the `<em>or</em>` in the
  deploy retry note was decorative and was dropped, and the sensor help wrapped around a
  real `<a>` split into two complete clauses plus a link-text key.
- **Product names, config keys and protocol tokens are not prose.** `REMOTE_TURN_SECRET`,
  `winget`, `H.264`, `Winlogon`, a Windows session state, an operator's own name for a
  backup destination — all deliberately left untranslated, and the ones that could be
  mistaken for prose say so in a `_`-prefixed note beside them.

**The browser's language is finally reachable (1.56.0).** `resolve()` always ranked
`Accept-Language` third, but `hub.default_language` shipped as `"en"` — indistinguishable
from an admin *choosing* English — so on every hub where nobody had opened Settings the
header was dead code, and a German operator on a German browser got an English console
with no fix but picking a language by hand on every device. The setting now ships as
`i18n.AUTO`, which is deliberately not a supported language and therefore falls through.
A *chosen* fleet default still outranks the browser; that part was never the bug.

The topbar picker gained an **Automatic** option for the same reason in reverse: a choice
used to be one-way, with nothing meaning "follow my browser again". It selects against the
user's STORED choice (`chosen_language`), not the language that won — pre-selecting the
resolved language would claim a choice they never made. `POST /api/language` maps `auto`
back to `users.set_language(None)`. A real choice is still stored on the profile, so it
follows the operator to the next browser and the next device.

**Remaining work: none for the shipped pages.** What is left is the standing obligation
rather than a backlog — a new string goes in `en.json` first (the schema), and
`tests/test_i18n.py` names it as untranslated in `es`/`de` until someone fills it in. The
one real limitation is unchanged: `plural()` implements one/other only, so a fourth
language with more forms needs a rule table, not just a catalog.

---

## 🚧 9. BIOS settings + firmware management   ·   `manufacturer` hub 1.57.0 + agent 3.18.0 / companion 2.12.0

Read and change firmware settings across the fleet, and finally execute the `update_bios`
command type that has been accepted, queued and routed — and then stubbed — since the
command-signing removal. The console can already issue one today and the agent already
dispatches it to a `StubExecutor` that answers "not implemented yet"; nothing has ever
run. The settings half has no command type at all yet.

The immediate motivator is #10 below: **Wake-on-LAN is a firmware setting before it is a
network feature**, and turning it on by hand on every machine is exactly the errand this
console exists to remove.

**There is no cross-vendor BIOS API, and pretending otherwise is the main way this
feature goes wrong.** Dell, HP and Lenovo each expose their own WMI namespace
(`root\dcim\sysman`, `root\hp\instrumentedbios`, `root\wmi`'s `Lenovo_*` classes) and
each ships its own CLI (Dell CCTK/DCU, HP BiosConfigUtility, Lenovo ThinkBiosConfig).
The recommended shape is **WMI-first with tool fallback**: the WMI classes are present on
a stock vendor image, whereas the CLI is an extra install — and the agent is a
single-file service that should not require a vendor package to have been deployed first.
Hardware that participates in none of this (a whitebox, a VM) must report **unsupported**
as an outcome distinct from **failed**: "this machine has no manageable BIOS" and "we
tried to change it and it broke" are different rows, and collapsing them means every VM
in the fleet shows a red error forever.

**Scope:**

- **✅ `manufacturer` — built (hub 1.57.0 + agent 3.18.0 / companion 2.12.0).** Vendor
  dispatch is the first branch every code path here takes, and `SystemInfo.cs` read
  `Win32_BIOS.SerialNumber` and `Win32_ComputerSystem.Model` but not `Manufacturer`. Now a
  `machine_info.manufacturer` column on the exact `service_tag` precedent from #6 —
  collected by both agents, COALESCE-updated in `save_machine_info`, backfilled by the
  duplicate-serial merge, null on an older agent. Two decisions worth carrying into the
  rest of #9:
  - **A whitebox's placeholder vendor is filtered to null, not stored.**
    `Win32_ComputerSystem.Manufacturer` reads `System manufacturer` or
    `To Be Filled By O.E.M.` on a self-built PC, and `model` has never been filtered at
    all — but manufacturer is what dispatch reads, so a value that looks like a brand and
    matches no vendor is worse than no value. Both agents share the filter list
    (`_PLACEHOLDER_MANUFACTURERS` / `PlaceholderManufacturers`), extending the asset-tag
    one. This is what keeps **null** ("no agent has told us what this is") distinct from
    the **unsupported** outcome ("this machine has no manageable BIOS") that the feature
    needs to be a different row.
  - **Manufacturer counts as an identity field in `save_machine_info`.** The function
    returns early unless *some* identity field is present, so a report carrying only a
    manufacturer would otherwise have been dropped silently. Tested
    (`test_manufacturer_alone_creates_a_row`).

  Surfaced as an Inventory column (sortable, and in `SEARCH_FIELDS` so `dell` narrows the
  list to one vendor — which is how a fleet-wide firmware action will be scoped) and on
  the machine page's Identity card, above Model. Every searchable field has a visible
  column on purpose: a row matching on something not on screen reads as a bug.
- **Settings inventory rides the heartbeat, change-only.** A machine reports its readable
  BIOS settings under a `bios` key on `POST /api/agent/heartbeat`, content-hashed so it
  is sent only when it actually changes, and never fatal — the same discipline
  `backups.record_profiles` and `remote.record_inventory` already follow. A BIOS
  enumeration is slow and the offline window is 90 seconds; that precedent exists
  precisely to keep one out of the other.
- **New `set_bios_settings` command type** in `ALL_COMMANDS`, carrying a snapshot of the
  attribute/value pairs the operator chose (like `install_virtual_display` carries its
  payload pin).
- **Setting names stay per-vendor in v1.** A curated cross-vendor alias set —
  `wake_on_lan`, `virtualization`, `secure_boot` — is the obvious design and the one that
  silently writes the wrong attribute on the third vendor, where the mapping was guessed
  from documentation rather than tested on hardware. v1 shows and writes each machine's
  own attribute names; aliases become a later layer with tests behind them, which is also
  the only form in which fleet-wide "turn WoL on everywhere" can be trusted.
- **A BIOS setup password blocks writes on all three vendors**, so a change can supply an
  existing password from `.env`-backed secret storage (the mechanism `backups.py` already
  uses for destination credentials), per-machine or fleet-wide. **Setting, rotating or
  clearing the BIOS password from the console is deliberately out of scope** — recorded
  here so the gap is a decision rather than a discovery.
- **Firmware update (`update_bios`) is the second half**, sharing the same vendor
  dispatch. Payload either hub-hosted or pulled from the vendor catalogue, **sha256-pinned
  by the hub** exactly as `packages.py` pins installer blobs. The load-bearing difference
  from a package install: **the reboot is part of the operation, not a side effect** — the
  flash completes during POST, so the command's own result cannot be the answer and
  success is only known when the machine comes back reporting a new BIOS version. Reuse
  the deploy scheduler's maintenance windows rather than inventing a second scheduler.
- **Gate**: either reuse `deploy_packages` or add a `manage_firmware` capability —
  open parameter. Flashing firmware is the most bricking-capable action in the product,
  which is the argument for its own toggle. Audited at `LEVEL_SECURITY`, alongside
  `virtual_display_install`.

**Constraint, stated plainly**: a failed flash can brick a machine, and unlike every other
feature on this roadmap there is no restore path — no chain to re-pull, no snapshot to
roll back to. Battery/AC preconditions, model matching and a refusal to flash a machine
whose reported model does not match the payload are part of v1, not hardening for later.

**Still to decide during implementation**: whether `set_bios_settings` is favoritable (a
boot-order policy is genuinely reusable across a fleet, unlike the session and deploy
types that are excluded today); whether firmware payloads reuse the `packages.py` blob
store outright or need their own; and how a settings change that requires a reboot to take
effect is represented, given the reported inventory will disagree with the intended state
until then.

---

## 📋 10. Wake-on-LAN

Power a sleeping machine on from the console, so an out-of-hours patch window or a remote
session does not depend on someone being at the desk to press a button. Small feature,
one real design decision.

**Prerequisite: a NIC inventory the hub does not have.** Nothing in the product collects
a MAC address or an IP address today — `machine_info` has no network columns at all. So
this starts with a new agent-side reporter on the `BackupProfileReporter.cs` change-only
pattern, sending per wired adapter: MAC, IPv4, prefix length and link state, into a new
`machine_nics` table. A table rather than columns because a machine has several adapters
and which one is the wake target changes with a dock. Rides the heartbeat under a
`network` key with the same never-fatal handling as `profiles` and `remote`.

**Delivery is agent peer-relay.** The hub picks an **online** machine whose reported
IPv4/prefix puts it on the same subnet as the target and issues *it* a new `wake_machine`
command carrying the target MAC and broadcast address; that agent sends the magic packet
to UDP 9. This is the only mechanism that works across sites and VLANs without touching
router configuration — a hub-sent broadcast reaches the hub's own L2 segment and nothing
else, which for a multi-site helpdesk is the wrong default.

- **Subnet grouping is the design**, so it is worth saying explicitly: the group key is
  `(ipv4 & mask, prefix)` computed hub-side from the reported NICs — never inferred from
  the source address of the last heartbeat, which is the NAT'd edge address and would
  group an entire site into one fictional subnet.
- **"No relay available" is a first-class, named outcome, not a failure.** Every machine
  on a subnet being asleep is the *expected* state at 3am. "No awake machine on
  10.4.7.0/24 to relay through" is a diagnosis someone can act on; "wake failed" is not.
- **The result is a wake *attempt*, and the console has to say so.** WoL is
  fire-and-forget — nothing acknowledges a magic packet, and a machine that was already
  awake looks identical to one that just woke. The honest shape is
  optimistic-then-confirmed: the command succeeds when the packet is sent, and the
  machine's own next heartbeat is what turns the row green. Same indirection as "Back up
  now", where recording a request and reporting it truthfully beat pretending to talk to
  a machine that cannot answer.
- **Fleet-wide and scheduled waking**, so a maintenance window can wake its targets before
  the deploy scheduler dispatches into a dark office — that pairing, not the manual
  button, is what makes this worth building. Reuse the existing deployment scheduler
  rather than adding a second one.
- **Gate**: `issue_commands` plus machine scope, like `restart` and `shutdown`. No new
  capability — waking a PC is strictly less dangerous than shutting one down, and the
  vocabulary is already large.

**Constraints — most of a WoL rollout is preconditions, not code**: the firmware wake
setting (**that is #9's job**, and the reason these two arrived together); the NIC
driver's own "Allow this device to wake the computer" power setting, which the agent can
set locally; **Windows Fast Startup**, which turns shutdown into a hybrid state that
defeats wake-from-S5 on many machines; and Wi-Fi, over which this mechanism cannot wake a
laptop at all. A machine that can never be woken should be shown as such from its
inventory rather than offered a button that silently does nothing.

**Still to decide during implementation**: whether `wake_machine` is favoritable; whether
the hub should *also* try its own broadcast when it happens to share the target's subnet
(rejected as the primary mechanism, cheap as an addition); how long to wait before
reporting a wake as unsuccessful; and whether a wake request persists across ticks like a
backup request does, so a target on an all-asleep subnet is woken by the first peer that
comes online.

---

## Build order

1. ~~**Permission Groups** (foundation)~~ — done, hub 1.26.0
2. ~~**#5 Deploy packages**~~ — done, hub 1.27.0 (pending an agent release)
3. ~~**#1 Backups**~~ — done: hub DB (1.28.0), per-PC files (1.29.0 / agent 3.8.0),
   console-driven restore (1.30.0 / agent 3.9.0). Both agent halves are pending a signed
   agent release.
4. ~~**#8 Registered Users directory**~~ — done, hub 1.35.0 (prerequisite for #6's member picker)
5. ~~**#6 Search & pickers across the console**~~ — done, hub 1.36.0 (+ agent 3.11.0 /
   companion 2.11.0 for the new Service Tag; agent half pending a signed release)
6. ~~**#2 Remote view & control**~~ — done, hub 1.40.0 (built ahead of #4 at the user's
   request; agent half pending a signed release; TURN server in `turn/`, installer-provisioned
   and managed from Settings → Remote Control)
7. ~~**#4 Active Directory**~~ — done: OIDC/Entra login (1.44.0), directory-group →
   permission-group mapping (1.49.0), on-prem LDAP computer sync + the `ad_ou`
   machine-scope mode (1.50.0). Never run against a real DC — see the note above.
8. ~~**#7 Localization**~~ — done, hub 1.56.0. Engine, catalogs and shared chrome in
   1.51.0; both open parameters resolved (in-house JSON string table; per-user preference
   over a fleet default). Every page converted by 1.56.0, including the settings
   `REGISTRY` and the other **server-supplied UI text** (capability labels, detection
   kinds, destination kinds, path tokens): the English moves into `en.json`, the web layer
   resolves it in the caller's language via `i18n.current()`, and `tests/test_i18n.py`
   asserts catalog coverage so a vocabulary entry added without words fails a test instead
   of captioning a page with its own key. 1.56.0 also fixed the browser-language fallback,
   which had been unreachable since the feature shipped.
9. **#9 BIOS settings + firmware management ← in progress** — fills in the `update_bios`
   stub that has been queued-but-unimplemented since the signing removal, and adds the
   settings half it never had. Its prerequisite is **done**: `manufacturer` is collected,
   stored, merged and shown as of hub 1.57.0 (+ agent 3.18.0 / companion 2.12.0), so
   vendor dispatch now has something to dispatch on. Next is the change-only BIOS
   settings inventory on the heartbeat, then the `set_bios_settings` command, then the
   `update_bios` executor. **The agent half is pending a signed release** (build +
   `sign_release.py --sign-agent` + asset upload); companion.py 2.12.0 is re-signed and
   ready.
10. **#10 Wake-on-LAN** — planned. Depends on #9 for the firmware-side enable, and on a
    NIC inventory (MAC + subnet) the hub does not collect today.

> Note: this file tracked hub releases closely up to 1.40.0 and then fell behind — the
> interactive terminal, pluggable OIDC sign-in, searchable dropdowns and the standalone
> TURN installer all landed in 1.41–1.48 without a roadmap entry, since none of them was
> a numbered feature above. #4's entry has been brought up to date. #9 and #10 are the
> first new items added to this board since #7, and are the only ones still unbuilt.

Each item above has a decided approach; only small, implementation-level parameters
remain (noted per feature) and are meant to be resolved while writing that feature's full
implementation plan — not before.
