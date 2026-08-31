# FleetHub — Feature Roadmap

Status: ✅ done · 🚧 partially done · 📋 planned (spec below)

> **Note on where things live (hub 1.87.0).** The console was reorganised around what you
> are doing rather than which PC you are doing it to. **Terminal, Backup, Firmware and
> Network left the machine page** for a **Tools** page, one tool per tab with the machine
> picked from a column beside them; the standalone **Firmware** and **Backups** pages folded
> into the matching tabs as their fleet halves, and both old urls redirect. The machine page
> keeps **Overview only** (every section of which now folds and remembers it) plus deep links
> into Tools. The **Dashboard is fleet-wide** — counts, OS mix, live aggregates, activity and
> a few ranked attention lists, from one `/api/fleet/summary` — and machines are reached
> through Asset Inventory. Entries below describe the features themselves and still hold;
> where one says "the Firmware tab on the machine page" or "the Backups page", read it as
> the corresponding Tools tab.

> **Note on `companion.py`.** The Python companion agent has been removed from the repo;
> the C#/.NET agent in `agent/` is the only telemetry client. Entries below that credit a
> companion release (e.g. "agent 3.18.0 / companion 2.12.0") are kept as accurate release
> history — they record what shipped at the time. Nothing new goes into that column.

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
sending them (an agent below 3.10.0) falls back to LHM's per-device
percentage and shows the bar without the GB line.

## 🚧 3. Settings: which sensors to read, data retention, etc.

Mostly done:
- `computer.primary_sensor_preference` — ordered CPU-temp sensor preference (+ per-machine
  override).
- `metrics.collect_cpu_load` / `collect_memory` / `collect_gpu` / `collect_disk` /
  `collect_disk_io` / `collect_network` — per-metric collection on/off toggles.
- `data.retention_days`, `data.prune_interval_seconds`, `data.ingest_max_backdate_days`,
  `data.command_output_retention_seconds` — retention knobs.

Temperature alerts (hub 1.34.0, **retired in hub 1.80.0**): the hub used to evaluate a high
temperature itself, from a rolling **average** over `hub.high_temp_avg_window_seconds` against
`hub.high_temp_threshold`, on a 30 s thread (`app.evaluate_high_temp_once`), surfacing as
`alerts.KIND_HIGH_TEMP`. That was a second alerting engine sitting beside the rules engine,
expressing a condition the rules engine could already express — and, being fleet-wide, it was
the reason **per-machine alert thresholds** were listed here as a gap.

**It is now an ordinary rule.** `metric.cpu_temp >= N` held for the rule's `for_seconds` says
the same thing, can be targeted at some machines and not others, and can raise an alert, run a
command, ask the user, or notify — the whole action list, rather than the one hardcoded alert.
`for_seconds` replaces the rolling average and is *stricter*: it requires the condition to hold
continuously, so one dip below the threshold restarts the clock, where an average would have
absorbed it.

Upgrading hubs get a **disabled** rule named "High temperature", seeded by
`app.seed_high_temp_rule` from whatever the two retired settings held (falling back to 85 °C /
300 s), so nobody silently loses the alert they had — but somebody has to review and enable it.
Both settings are gone from the registry. `alerts.KIND_HIGH_TEMP` survives as a read-only kind
so alerts raised before the upgrade still render and can still be dismissed.

Remaining gaps: no per-metric retention (one global window for all readings), no
sampling/reporting-cadence setting, no sensor *preference* list for GPU/disk/network (only CPU
temp has one — the others are on/off only). Fold these in opportunistically; no dedicated
feature push planned.

---

## ✅ Rules: real command parameters + a Scripts library   ·   hub 1.81.0 + agent 3.32.0

Three things the rules editor could not express, all of which the engine behind it already
could.

**Commands now declare their parameters.** `fleet.COMMAND_PARAMS` describes what each command
type takes (name, kind, bounds, default), `fleet.validate_command_params` enforces it at the
rules boundary, and `GET /api/rules/variables` serves the catalog so the console renders the
fields instead of carrying a hand-kept list. Before this, `actionCard` offered seven of the
twelve permitted commands and **no parameter editor at all** — so choosing `run_script` queued a
command the agent rejected for having no script, and a restart's countdown was unreachable.
`install_driver` is served marked unavailable (its agent executor is a stub that always fails)
rather than dropped; `update_bios` moved into `RULE_FORBIDDEN_COMMANDS`, where it always
belonged — its `update_id` is minted per target per maintenance window, so a saved rule could
only ever replay a stale one.

**Scripts are a store.** `hub/scripts.py` + `/api/rules/scripts`, edited on a Scripts tab beside
Rules. A script has a reference name, a shell, a body, a timeout and declared **inputs** that the
referencing rule supplies. A rule points at one with the new `script` action and the body is
read at **fire time**, so editing a script changes what every rule using it runs. Deleting one a
rule references is refused with the rule names (409).

Deliberately NOT `fleet_favorites`, which is the same shape: favorites are writable with `view`,
which is safe only because *running* one needs a human holding `issue_commands`. A rule removes
the human, so writing a script needs `issue_commands` and reading a script's **body** does too —
listing is `view`, because the rules editor must offer the names.

**A message's answer can now be any action, with real parameters** — the four synthesised
choices are gone. `render_template` is applied to string command params at dispatch, and a
placeholder that cannot be resolved **skips the action** instead of running code containing the
literal word `unknown`; message text keeps the old behaviour, where "unknown" reads as missing
data rather than as an instruction.

**A rule can now ask once instead of on a timer.** `fire_once_per_match` (default off, so
nothing existing changes) fires on the TRANSITION rather than every `cooldown_seconds`. The
cooldown is a timer, which is the wrong shape for a condition that stays true: `sys.uptime_days
> 7` holds until somebody reboots, so the dialog returned every hour and clicking No bought the
person at the desk nothing. The latch is `last_fired_at >= matched_since` — note it is NOT
`last_fired_at IS NOT NULL`, because `_write_state` deliberately leaves that timestamp alone
when passed None, so it survives the condition going false. `matched_since` is what gets
cleared. New message rules also prefill a one-day snooze on the No answer.

**Agent 3.32.0** gives a rule-issued script a fresh shell per firing. `RunScriptExecutor` keys
its persistent session on `issued_by`, so every fire of one rule was reusing a single PowerShell
process complete with the last fire's working directory and variables.

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

## ✅ 5. Deploy packages (PDQ-style)   ·   hub 1.27.0; multi-step hub 1.64.0 + agent 3.22.0

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

### Multi-step packages · hub 1.64.0 + agent 3.22.0

The original recipe was one command line, which covers an MSI and nothing else. A driver
pack is three programs: download a zip, unpack it, hand the folder to `pnputil`. Expressed
as one chained `cmd /c a && b && c` that is a single exit code for three programs — and
pnputil's dialect (259 is a clean run that simply ran out of INFs) does not survive being
merged with someone else's. So a package now carries an ordered **step list**, and payloads
are **named slots** the steps refer to.

- **Five step kinds**, held to that number for the reason the detection grammar is held to
  three: each is code the agent must implement and keep working. `run`, `powershell`,
  `winget`, `extract` (.zip), `pnputil`. `powershell` is the deliberate escape hatch —
  copy a file, poke the registry, restart a service — so the other four stay narrow instead
  of growing switches to cover every adjacent case. `packages.STEP_KINDS` is served to the
  editor with its catalog text, so adding one is a Python edit plus three catalog entries.
- **Payloads gained names** (`package_sources.name`, `UNIQUE(package_id, name)` replacing
  `UNIQUE(package_id)`) — the seam the original docstring said extra payloads would land
  on. Steps say `{drivers}`; an `extract` step binds its output folder for the steps after
  it. `{work}` is the attempt's own directory, and `{file}` still means the payload when
  there is exactly one, so every package written before this keeps its command line.
- **Unknown variables are refused at save time**, not at 2am on 200 machines. The grammar
  is `{lowercase_words}` only, so a literal MSI product code
  `{90160000-008C-0000-1000-0000000FF1CE}` is left alone rather than rejected — and a step
  that names a folder a LATER step produces fails, because bindings are checked in order.
- **A payload no step opens is refused**, the same rule as the old "`{file}` must appear
  somewhere": downloading a file to every machine and never opening it is always a mistake.
- **Per-step timeout and exit codes**, both defaulting to the package's. `pnputil` defaults
  to `{0, 259, 3010}`, resolved by the HUB so the editor can show an operator the set their
  driver step will be judged by — the 3010 lesson applied one layer down.
- **The first failed step stops the deploy** unless it is marked "carry on". Detection runs
  once at the end, because it is a claim about the package, not about a step.
- **The agent works in one directory per attempt**, under `%ProgramData%` (SYSTEM-owned,
  not `%TEMP%`), deleted whole on every path. `extract` is done with `System.IO.Compression`
  rather than by shelling out to `Expand-Archive`, so the zip-slip check on every entry is
  ours to make rather than someone else's to have made.

**Old agents fail closed, they do not half-run.** An agent that predates steps reads
`source` and `install_command`, so `build_command_params` emits a **legacy projection**: a
package with no steps goes out in exactly the old shape and runs correctly on every agent
in the field, and a package WITH steps deliberately offers no command to run
(`install_command: ""`, `source.kind: "multi"`). Such an agent fails the deploy with "no
install command" and the target retries until the update reaches it. The alternative —
running step one and reporting success — is the one outcome that would lie.

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

**Fixed in 1.63.0 — the Active Directory tab never rendered its own fields.** `directory`
was in `settings.SECTIONS` and had a tab in `settings.html`, but was missing from
`settings.js`'s `panels` map, and `applySchema` skips a section whose panel it cannot find.
So the tab existed, showed `settings-directory.js`'s status card, and rendered none of the
`directory.*` fields — the server, the bind DN, the search base, the cadence, TLS
verification — meaning AD could not actually be configured from the console at all. This is
the identical failure the `remote` section had before 1.40.0, which is why the fix comes
with a test: `test_settings.test_every_section_has_somewhere_to_render` now asserts both
halves for every section, with `deploy`/`backup`/`firmware` listed as deliberate exceptions
(their knobs are edited on their own pages). "No tab" now has to be a decision somebody
made rather than something that happened.

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
before the fleet gains it. Until then, machines show a blank Service Tag, which is exactly
the older-agent fallback above. (companion.py 2.11.0 carried its half too; the script has
since been removed from the repo entirely.)

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

## ✅ 9. BIOS settings + firmware management   ·   `manufacturer` hub 1.57.0 + agent 3.18.0 / companion 2.12.0; settings inventory hub 1.59.0 + agent 3.19.0; settings WRITE hub 1.60.0; **firmware UPDATE hub 1.61.0 + agent 3.20.0**

Read and change firmware settings across the fleet, and execute the `update_bios` command
type that had been accepted, queued and routed — and then stubbed — since the
command-signing removal. Both halves are now built; the agent half ships in source and
needs a signed release before the fleet gains it.

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
- **✅ Settings inventory — built (hub 1.59.0 + agent 3.19.0).** `bios.py` (model,
  flask-free) + `bios_web.py` (console read + "re-read now"), a `machine_bios` table, a
  **Firmware tab** on the machine page, and `agent/src/TempMonitorAgent/Bios/` — three
  vendor sources behind one dispatcher, a change-only reporter, and a
  `refresh_bios_inventory` executor. A machine reports its readable BIOS settings under a
  `bios` key on `POST /api/agent/heartbeat`, content-hashed so it is sent only when it
  actually changes, never fatal, and scanned on the agent's inventory loop rather than the
  heartbeat's — a BIOS enumeration is seconds and the offline window is 90, so the two must
  not share a thread. Resolved parameters:
  - **Three outcomes, and the split is the feature.** `supported` / `unsupported` /
    `error`, distinct in the model, the payload and the UI. A namespace that is *absent*
    (Dell without Command | Monitor, any VM, any whitebox) is **unsupported** — neutral,
    permanent, and quiet: one payload per machine, ever. A namespace that exists and throws
    is an **error**, and so is one that enumerates *nothing*, because "the interface
    answered with zero attributes" is a permissions or WMI-repository fault wearing the
    costume of a support claim. The hub additionally normalises an unrecognised support
    string to `error` rather than storing it: guessing `unsupported` would file a real fault
    under the one state nobody investigates.
  - **A fourth value exists only at the read boundary: `support: null`.** "No agent has told
    us" is not "this hardware cannot do it" — and until the 3.19.0 release propagates, every
    machine in the fleet is in that state. Rendering it as `unsupported` would write off a
    whole fleet on the strength of no evidence.
  - **`password_set` is nullable on purpose.** Null where the vendor gives no way to ask,
    which is not the same as `false`. The two lead to different advice the moment somebody
    tries to change a setting, and the write half is next.
  - **Vendor dispatch is agent-side; the hub never branches on vendor.** What crosses the
    wire is already normalised to `{name, value, kind, possible_values, read_only,
    display_name}`. Dell (`root\dcim\sysman`) and HP (`root\hp\instrumentedBIOS`) split
    attributes across three classes by type — so the kind comes from the class, not from
    guessing at the value — while Lenovo is the odd one out: every setting is one string,
    `"WakeOnLAN,Enable;[Enable,Disable,ACOnly]"`, so parsing that format *is* the interface,
    and a setting arriving with no option list is reported `unknown` rather than assumed an
    enum. No CLI fallback is used: CCTK/BiosConfigUtility/ThinkBiosConfig would each add a
    deployment prerequisite to an agent meant to work on a stock vendor image.
  - **Read is `view`, re-read is `issue_commands`.** What a PC's firmware is set to is
    inventory, like its model or its disks; needing an admin to answer "is Wake-on-LAN on?"
    would make the question cost more than walking to the desk. Forcing a re-read queues a
    real command on a real machine, so it sits behind the command channel's perimeter, and
    the console says *queued*, not *refreshed* — a machine that is asleep answers on its
    next check-in (same honesty as "Back up now").
  - **Names stay per-vendor, as specified below.** The real attribute name is always shown;
    a vendor's friendlier `display_name` goes beside it, never instead of it, because the
    real name is the identity a write will target.

  **Tested where it can be** — `tests/test_bios.py` (the ingest boundary: nothing a machine
  can send raises, the states never collapse, caps bound the row) + `tests/test_bios_web.py`
  (both capability gates in both directions, and a malformed `bios` block still returning a
  200 heartbeat — a 500 there marks the machine offline fleet-wide) +
  `BiosReaderTests.cs` (each vendor's parsing against fake WMI rows, since a build box is
  never simultaneously a Dell, an HP and a Lenovo). **What none of that covers is the WMI
  adapter itself**: whether the property names above match what real firmware emits, and
  whether a missing vendor namespace really surfaces as `InvalidNamespace`. Expect the same
  shape of first-contact finding the LDAP and TURN work produced.
- **✅ `set_bios_settings` — built (hub 1.60.0 + agent 3.20.0).** A `bios_changes` table and
  the write half of `bios.py`/`bios_web.py`, an editable Firmware tab, a new
  `manage_firmware` capability, and `agent/src/TempMonitorAgent/Bios/BiosWriter.cs` +
  `SetBiosSettingsExecutor.cs`. Resolved parameters:
  - **The gate is a new `manage_firmware` capability**, not a reuse of `deploy_packages`
    (the roadmap left this open). This is the one action in the product with no restore
    path, and folding it into "can push an installer" would have granted it silently, on
    the day it shipped, to everyone who already held that. Reading the inventory stays
    `view` and forcing a re-read stays `issue_commands` — three gates, and the test asserts
    a tech holding `issue_commands` is refused a write.
  - **Verification replaces the reboot guess, exactly as specified.** The agent writes,
    re-reads the affected attributes, and reports what the firmware now says;
    `bios.classify_result` compares. New value → `applied`, and the console says nothing
    about restarting. Old value with no write error → `pending_reboot`, and that is the
    **only** place a restart is ever suggested. A third value → `unknown`, its own outcome
    rather than a guess in either direction, because some firmware normalises what you
    write and some substitutes something else — calling either "applied" would confirm a
    change nobody made. Comparison is casefolded and trimmed: writing `Enable` and reading
    `enable` is the normal case, and being strict would send an operator to reboot a
    machine for nothing.
  - **Change state lives in its own table, not as pending markers on the inventory.** Three
    reasons: a change is something an operator *did*, with an author and a time, while the
    inventory is something the machine *reported*; the next heartbeat overwrites the whole
    inventory blob and would take per-attribute markers with it; and a history answers "who
    turned Secure Boot off in March", which a current-state column cannot. Rolled up
    worst-news-first — one failure among five successes is `partial`, never the majority
    answer, because on firmware the one that did not take is the one that matters.
    `applied + pending_reboot` rolls up to `pending_reboot`: nothing went wrong there.
  - **One change in flight per machine.** Two concurrent writes would race in the firmware,
    and verification — which reads a single current value per attribute — could not say
    which one it was looking at. A second request is a 409. The corollary is a sweep
    (`expire_stale_changes`, hourly on the existing housekeeping heartbeat): without it a
    machine that died mid-write would leave a `running` row forever and one dead agent
    would permanently lock its own Firmware tab. Swept rows close as `partial`, not
    `failed` — the agent had fetched the payload, so some of it may well have been written.
  - **Validation is against the machine's own reported attributes, never a hub-side
    rulebook.** The attribute must exist, not be read-only, and an enum value must be one
    the machine listed — and the machine's spelling of both name and value wins over the
    caller's. A hub-side rulebook here would be the cross-vendor alias map by another name.
    A **no-op is refused** rather than dropped: verification compares the re-read against
    the old value, so a change from X to X has no observable outcome and would sit as
    `pending_reboot` forever.
  - **Found while wiring this up**: `list_changes`/`open_change_for` ordered by
    `requested_at` alone, and several changes land within one second — so the tab could
    show a resolved change over a pending one and hand the agent the wrong payload. Same
    `(timestamp, rowid)` tiebreak `rotate_chains` needed, for the same reason.
  - **Also found**: `/api/bios/password` would have collided with `/api/bios/<machine>`,
    both shadowing a machine actually named `password` and — worse — making
    `GET /api/bios/password` a 200 that *reads* like a password-reveal endpoint. Moved to
    `/api/bios-password`. There is no route that returns a password at all.
- **Setting names stay per-vendor in v1.** A curated cross-vendor alias set —
  `wake_on_lan`, `virtualization`, `secure_boot` — is the obvious design and the one that
  silently writes the wrong attribute on the third vendor, where the mapping was guessed
  from documentation rather than tested on hardware. v1 shows and writes each machine's
  own attribute names; aliases become a later layer with tests behind them, which is also
  the only form in which fleet-wide "turn WoL on everywhere" can be trusted.
- **✅ A BIOS setup password blocks writes on all three vendors**, so a change supplies one
  from the `.env`-master-key-wrapped secret store `backups.py` already uses for destination
  credentials — **fleet-wide with a per-machine override**, because a helpdesk images a
  fleet with one password and then has exceptions. **The password never travels in command
  params**: `fleet.create_command` audits params verbatim, so it would sit in the audit log
  inside the database that is itself backed up. The command carries a *change id* and the
  agent fetches the attribute list and password together from an authenticated endpoint —
  the restore-plan precedent, for the same reason — and that fetch is a conditional
  `UPDATE ... WHERE status='pending'`, so a redelivered command cannot replay writes
  against firmware. A second audit row (`bios_settings_change`) records the attribute names
  and values, since the `issue_command` row would otherwise say somebody changed firmware
  and not one word about what to. **Setting, rotating or clearing the BIOS password *in the
  firmware* from the console remains out of scope** — what ships is storing the existing
  one.

  **Each vendor's write disagrees more than its read does**, and all three fail silently
  when got wrong: Dell takes parallel name/value **arrays** and reads an empty
  `AuthorizationToken` as a supplied-and-wrong password on some models (so it is omitted
  entirely when there is none); HP wants one pair per call and its password
  `<utf-16/>`-prefixed, without which a *correct* password is refused as wrong; and Lenovo
  takes one `"Name,Value"` string and **commits nothing until a separate
  `SaveBiosSettings`** — a write that skips it returns `Success` and changes nothing, which
  is why a failed commit invalidates every write that thought it had succeeded. A comma in
  a Lenovo name or value is refused rather than escaped, because Lenovo defines no escape
  and it would be read as the separator.

  **Tested where it can be** — `tests/test_bios.py` (validation, all four `classify_result`
  shapes, the change lifecycle, the stale sweep) + `tests/test_bios_web.py` (all three
  capability gates in both directions, the one-in-flight 409, the fetch-once 409, and a
  scan of the whole audit trail asserting no stored password appears in it) +
  `BiosWriterTests.cs` (each vendor's call shape, argument types, password convention and
  return-code reading, against a recording fake). **What none of that covers is the WMI
  method call itself**: whether real firmware accepts these arguments, and whether the
  return properties are named what the documentation says. Expect the same shape of
  first-contact finding the LDAP and TURN work produced.
- **✅ Firmware update (`update_bios`) — built (hub 1.61.0 + agent 3.20.0).** `firmware.py`
  (model, flask-free) + the firmware half of `bios_web.py`, a **Firmware page** (image
  library + fleet update history), a read-only BIOS-updates panel on the machine page,
  six `firmware.*` settings, and `agent/src/TempMonitorAgent/Bios/FirmwareFlasher.cs` +
  `UpdateBiosExecutor.cs`. Resolved parameters:
  - **The remaining open question — do firmware payloads reuse `packages.py`'s blob store
    — resolved as: the primitives yes, the ROOT no.** `packages.store_blob` /
    `blob_path` are called as-is (one tested content-addressed writer, and the hub still
    computes the digest from the bytes it stored rather than accepting a client's), but
    images live under `<log dir>/firmware`. Sharing one root would mean two features
    refcounting a file neither owns: `packages.delete_blob_if_orphaned` asks
    `package_sources` whether anything still points at a digest, and it would answer "no"
    about a BIOS image it has never heard of — i.e. a package delete unlinking firmware.
  - **A good agent report is NOT success**, and the state machine says so: it reaches
    `rebooting`, and only the machine coming back reporting the payload's version applies
    it (`confirm_from_inventory`, called from the heartbeat's BIOS ingest). A machine that
    comes back on a *third* version is `unknown` — the same fourth-outcome reasoning as
    `classify_result`, because a vendor whose image reports a version string the operator
    did not type is indistinguishable from a flash that did something else, and confirming
    either would confirm a change nobody verified.
  - **No retry, ever.** `packages.py` retries a target three times with a doubling backoff,
    which is right for an installer and is the worst possible behaviour for a flash whose
    outcome is unknown. A failed target is re-issued by a human who has looked at the
    machine. This is also why dispatch only targets machines that are **online**: with no
    retry, dispatching into the dark would record a switched-off PC as a failure nobody
    would dare re-run. Left pending, it goes out the minute it reappears.
  - **Preconditions are refusals, and a refusal is a first-class target row.** A machine
    whose manufacturer or model is not on the image's list — or that is already on the
    target version, whose flash could never be confirmed — is recorded as `refused` with
    its reason rather than dropped, so a mixed fleet proceeds where the image fits and
    *names* where it does not. Same reasoning as `plan_restore` naming a selection that
    matched nothing. The model list is **required** at upload and never defaults to "any":
    an image that matches everything is one refusal away from the failure with no undo.
    Both checks run again **on the agent** against what the hardware says about itself at
    flash time, because the hub's inventory can be hours old and a chassis can be swapped.
  - **The agent never reboots the machine.** A vendor tool exits after *staging*; the
    firmware writes during POST. Restarting somebody's PC as a side effect of an update
    they were not told about is not the agent's call, and the hub is built for the
    alternative — the target waits in `rebooting` with a **day** of grace by default, so a
    PC flashed on a Friday evening is confirmed on Monday morning.
  - **The BIOS password stays off the command line where a vendor allows it.** HP and
    Lenovo read it from a file, so it is written to a SYSTEM-owned staging file and deleted
    on every path; Dell has no file form, and that cost is named in the code rather than
    buried — for the seconds its updater runs, the password is readable in the process
    list. Neither it nor the image URL is ever in command params (`create_command` audits
    those verbatim); the command carries an opaque update id, and the fetch that hands over
    the rest is a conditional `UPDATE`, so a redelivered command cannot flash twice.
  - **"Reboot required" is a success exit code**, per vendor (Dell's 2 and 6, the usual
    3010 elsewhere) — the 3010 trap in another costume, and here it is not an edge case but
    the normal outcome. Dell's codes are deliberately not generalised to the other two: a 2
    from an HP updater means something else, and reading it as success would report an
    unknown failure as a staged flash.
  - **Found while wiring this up**: a target could sit `in_flight` forever if its command
    expired. `fleet.expire_stale_commands` noticed, the target never did, and neither
    timeout catches it because both count from a fetch that never happened — so the job
    stayed at 39/40 with no way to reach 40. `reconcile_once` retires those as failed,
    saying which of the three it was and, for an expiry, that nothing was flashed.

  **Tested where it can be** — `tests/test_firmware.py` (the refusals, the state machine,
  the exactly-once handover, cancel's three outcomes, both sweeps) +
  `tests/test_bios_web.py` (the `manage_firmware` gate on every route including the reads,
  the 409 on a second fetch, the digest computed hub-side, and the audit trail scanned for
  the password) + `FirmwareFlasherTests.cs` (hardware and power refusals, the password
  never reaching a command line where a vendor offers a file, and reboot-required read as
  success). **What none of it covers is a real flash**: no vendor tool runs, and whether a
  Dell Update Package accepts `/s /p=` as documented is a first-contact question, like LDAP
  and TURN before it.

  **Maintenance windows are the deploy scheduler's, reused as semantics rather than as
  code** — same `window_start`/`window_end` fields, same claim-then-queue discipline, in
  `firmware.dispatch_once`. Literally reusing `packages.reconcile_once` would have meant
  teaching one scheduler two completion models, and the one it has (exit code plus
  detection) is precisely the guess this feature must not make.
- **Gate**: ✅ resolved — a new **`manage_firmware`** capability, live as of 1.60.0 and
  gating both halves. `update_bios` reuses it rather than adding a second one, and on the
  Firmware page it gates the **reads** too: an image library is fleet configuration, not a
  fact about a PC, so unlike the settings inventory there is nothing in it an operator
  needs in order to answer a support question. Audited at `LEVEL_SECURITY`, alongside
  `virtual_display_install`.

**Constraint, stated plainly**: a failed flash can brick a machine, and unlike every other
feature on this roadmap there is no restore path — no chain to re-pull, no snapshot to
roll back to. Battery/AC preconditions, model matching and a refusal to flash a machine
whose reported model does not match the payload are part of v1, not hardening for later.
✅ All three shipped in 1.61.0/3.20.0, and the power check refuses on an **unknown** state
rather than assuming mains: a laptop whose battery class is missing or throwing is exactly
the machine the check exists for, and this is the one operation where an unknown must never
be spent as a yes.

**A settings change does NOT imply a reboot, and the design must not assume one.** Whether
a write takes effect immediately is **per vendor *and* per setting**: Dell's interface
(CCTK and the `root\dcim\sysman` classes behind it) applies many attributes live, others
sit pending until POST, and HP and Lenovo each draw that line somewhere else again. So
"changed a BIOS setting → tell the operator to reboot" is wrong most of the time, and a
per-vendor "requires reboot" table would be the alias-map mistake in another costume —
guessed from documentation, wrong on the third vendor, and wrong per-attribute even where
the vendor is right.

**The inventory is the source of truth, so verification replaces the guess.** After a write
the agent re-reads the affected attributes and reports what the firmware now says. If the
new value is there, the change is applied — say so, and say nothing about rebooting. If the
old value is still there and the write itself did not fail, the change is **pending**, and
*that* is when the console asks for a restart. This needs no vendor knowledge, is correct
per-attribute for free, and degrades honestly on hardware nobody has tested: the worst case
is "pending" on a machine that actually applied it live, which the next scheduled read
corrects. It also gives the write half a real completion signal rather than an exit code,
the same way `update_bios` gets its answer from the BIOS version after the flash rather
than from the command result.

**Two of the three open parameters are now resolved.**

- **`set_bios_settings` is NOT favoritable.** The tempting argument was that a boot-order
  policy is genuinely reusable across a fleet — but the command carries a change id
  belonging to one machine and one already-issued set of writes, so a saved copy would
  replay a resolved change against a machine whose attribute names may not even match (v1
  maps no vendor vocabulary, so a Dell's `WakeOnLan` means nothing on a Lenovo). The
  reusable thing is a *named set of attribute values*, which is a different object from a
  command and belongs with the alias layer if that is ever built. `refresh_bios_inventory`
  stays favoritable.
- **A pending change lives in its own `bios_changes` table**, not per attribute on
  `machine_bios` and not on the command result — see the reasoning in the built entry
  above. The deciding factor was that the next heartbeat overwrites the inventory blob
  wholesale and would take per-attribute markers with it.

**Nothing left to decide.** The last open parameter — whether firmware payloads reuse
`packages.py`'s blob store — resolved to "its primitives, not its root"; see the built
entry above for why sharing a root would eventually have one feature deleting the other's
bytes. What remains is a **signed agent release** (build + `sign_release.py --sign-agent`
+ asset upload) before the fleet gains any of #9, and the first-contact WMI findings that
only real Dell/HP/Lenovo hardware can produce.

---

## ✅ 10. Wake-on-LAN   ·   hub 1.63.0 + agent 3.21.0

Built. `wake.py` (model, flask-free) + `wake_web.py` (console API), a `machine_nics` /
`machine_network` / `wake_requests` schema, a **Network tab** on the machine page, a
fleet-wide wake on the Asset Inventory page, `wake.*` settings, and two new command types —
`wake_machine` (issued to a *peer*) and `prepare_wake` (the precondition remedy). Agent
half: `Network/NicReader.cs`, `NetworkInventoryReporter.cs`, `WakeMachineExecutor.cs`,
`PrepareWakeExecutor.cs`. **The agent half needs a signed release** (build +
`sign_release.py --sign-agent` + asset upload) before the fleet gains it.

Delivered as specified below. **All four open parameters resolved:**

- **`wake_machine` is NOT favoritable.** It is the only command type in the product whose
  target machine is not the machine it is about: the params carry somebody *else's* MAC and
  broadcast address, resolved when the scheduler picked that relay. A saved copy replayed
  against a different machine would broadcast a MAC onto a subnet it is not on — a packet
  that goes nowhere, reported as sent. **`prepare_wake` IS favoritable**, and is the one
  that wanted to be: it carries no machine-specific payload at all, and "make these twelve
  PCs wakeable" is exactly the shape a favorite is for. Most of a WoL rollout is
  preconditions, so the fleet-wide verb is the one that matters.
- **The hub's own broadcast: yes, as a fallback only** (`wake.hub_broadcast`, on by
  default). Taken only when no awake peer exists AND the hub can prove it shares the
  target's subnet — which it decides from the **routing table**, not by enumerating
  interfaces: connecting a UDP socket sends nothing and answers "which local address would
  I source from to reach that host", and if that address is inside the target's subnet the
  hub is on that segment. Pure stdlib, no platform-specific interface API. This closes the
  real hole in peer-relay — 3am, every PC on the subnet asleep — for a single-site fleet,
  without becoming the mechanism the design rejected.
- **How long before a wake is reported unsuccessful: two different clocks, because there
  are two different silences.** `wake.request_ttl_seconds` (15 min) bounds how long to keep
  *looking for a relay* before the outcome is `no_relay`; `wake.confirm_timeout_seconds`
  (5 min) bounds how long to wait for the *target's own check-in* after the packet went out
  before the outcome is `no_answer`. Collapsing them into one number would have made a
  machine on an all-asleep subnet indistinguishable from one that was sent a packet and
  never came back, which are the two situations an operator most needs told apart.
- **A wake request DOES persist across ticks**, exactly like a backup request — that is
  what makes a target on an all-asleep subnet get woken by the first peer to come online,
  within one tick of it arriving. Bounded by the TTL above, because a wake asked for on
  Friday evening must not fire on Monday morning when the first person to arrive supplies
  the peer it was waiting for.

**Three implementation-level decisions worth carrying forward:**

- **Confirmation compares against `sent_at`, not against online-ness.** A machine can read
  online on a `last_seen` from eighty seconds BEFORE the packet went out, so a boolean
  "is it up now" would confirm a wake nobody performed — every time, for any machine merely
  flapping in and out of the 90-second offline window. `backup_machine_roster()` therefore
  grew a `last_seen` field, and only a check-in newer than the packet counts. This is the
  single sharpest edge in the feature: it fails in the direction of looking like it worked.
- **A malformed `network` report replaces nothing.** The adapter set is REPLACED on each
  report (a dock unplugged must disappear, because a stale adapter is a stale *subnet* and
  the hub would go on offering that machine as a relay for a segment it left) — and
  replace-semantics plus a lenient parse is how a truncated body silently empties a good
  inventory and leaves a machine unwakeable with nothing on screen to say why. So an absent
  or non-list `nics`, and a non-empty list in which nothing parsed, are both refused; an
  **empty list** is a real claim and is honoured. Found by the tests, not by reasoning.
- **The relay's answer comes from the command row, not a new agent endpoint.** `firmware`
  needed one; this does not. The relay's whole job is one `sendto`, its answer is a success
  bit, and the command queue already carries success bits with an audit trail — a second
  endpoint would be a second thing to authorise for no extra information. A failed or
  expired relay command puts the request back to `pending` with that peer excluded, rather
  than failing it: one agent being unable to send is not evidence the target cannot be
  woken, and there may be nine other machines on that subnet.

**The scheduled pairing is wired through the schedulers that already exist**, which is the
reuse this entry asked for. `packages.pending_target_machines()` and
`firmware.pending_target_machines()` are READS that answer "which machines is this window
waiting on"; `app.wake_pending_targets()` wakes the offline ones, opt-in via
`wake.auto_wake_targets` (off by default — waking a fleet at 3am is a decision, not a side
effect of scheduling a deploy). No third window/target/status machine was built: a wake is
a *precondition* of a window, not a job kind with windows of its own.

**The preconditions are inventory, and they have a remedy.** `wake.diagnose()` names every
reason a machine cannot be woken — `no_report`, `no_wired_nic`, `wireless_only`,
`no_address`, `wake_disabled`, `fast_startup` — as codes the console translates, so a
machine that can never be woken says so instead of being offered a button that silently
does nothing. `prepare_wake` is the other half: it ticks the device's own "allow this
device to wake the computer" (via `powercfg /deviceenablewake`, the supported path), sets
the driver's `*WakeOnMagicPacket` advanced property, and turns Fast Startup off. **Both NIC
settings have to be on**: an adapter allowed to wake the machine but with magic-packet wake
turned off in its driver looks perfectly configured in Device Manager and ignores every
packet. Wireless adapters are never touched — this mechanism cannot wake a laptop over
Wi-Fi, so enabling wake there would change a machine's power behaviour and battery life for
no benefit at all.

**Not covered by tests, and worth saying:** everything above is exercised against literal
payloads and a fake roster, with no network. What that cannot cover is `NicReader.Read()`
against real hardware — the WMI device power policy, the `*WakeOnMagicPacket` value a given
driver actually publishes, whether a docked laptop reports the dock's adapter, and whether
`powercfg /deviceenablewake` matches on the description this reader reports. Expect the same
shape of finding the LDAP and TURN work produced on first contact.

<details>
<summary>Original spec</summary>

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

*(All four were resolved as described above.)*

</details>

---

## 🚧 11. Desktop & mobile app   ·   hub 1.69.0   ·   client 1.1.0

**The hub half is built (1.69.0), 1.0.0 is signed and published, and 1.1.0 is in the tree
awaiting a release.**
It analyzes clean and its 48 tests pass. The Windows binary now exists, and the app has
been **launched**: the window opens, every plugin DLL resolves, and `local_notifier`
registers the Start Menu shortcut that gives an unsigned non-MSIX build the application
identity Windows demands before it will show a toast at all.

**What remains unverified is the client against a live hub.** Launching it proves the
packaging; it exercises neither pairing, nor the ten-second poll, nor a single hub
endpoint, because those need a reachable hub and an OAuth round trip. The "first contact
with hardware" finding this entry predicts is therefore still ahead of it, not behind.

Running the tests immediately earned its keep. It found a **real race in the pairing
flow**: the loopback listener was attached AFTER the browser was opened, so a browser
already running and signed in could complete the round trip before anything was listening,
and the pairing would hang until its five-minute timeout with nothing to say why. The
listener is now attached first.

A second bug surfaced the same way, and is worth recording because **a passing test was
hiding it**. `AlertNotifier.reset()` — documented as "used by the tests, and by sign out" —
was called by the tests and by nothing else. The notifier is built by a Provider with no
dependencies, so one instance serves the whole process and outlives every pairing: after a
sign-out and re-pair, the next operator started against the previous one's seen-set with
priming already spent, and their first poll toasted every alert that operator had not seen.
Two people sharing a helpdesk PC rarely share a machine scope, so that is not a short list
— it is precisely the launch-notification storm `_priming` exists to prevent, arriving at
sign-in instead of at startup. `reset()` now happens in `SessionController.forget()`, the
one seam both the sign-out button and a poll killed by a 401 pass through.

**The transferable part is why the test passed.** `notify_test.dart` asserted that
`reset()` returns the notifier to priming, and it did — the unit was correct and the seam
to it was missing, which is not something a test that constructs the class directly can
see. Two changes close that: `notifyNew` now returns the alerts it raised, because a toast
is invisible under a test binding and `isPriming` was the only observable, and a flag
cannot distinguish "raised nothing" from "raised nine"; and `test/session_test.dart` drives
the real providers, so the WIRING is pinned rather than the unit. Both new tests fail
against the old code, one of them reporting the two toasts an operator would have seen.

The console is a good web app, but it is a *web* app: watching a fleet means keeping a tab
open, and an alert raised at 02:00 by a rule reaches nobody until someone looks at the
Alerts tab. This adds a **native client** — Flutter, one
codebase, **Windows desktop in v1**, Android/iOS as an addition rather than a rewrite.

v1 scope is the fleet list, machine detail, alerts with OS notifications, Wake-on-LAN and
saved commands as quick actions. The open parameters were resolved as:

- **Flutter, not React Native + Electron.** RN's desktop story would have meant Electron as
  a *second* shell — two runtimes, two build systems, and a desktop half that is Chromium
  anyway, at which point a PWA was cheaper. Flutter covers Windows, Android and iOS from
  one codebase with `flutter_webrtc`/`xterm.dart` waiting for the tiers after v1. The cost
  is honest and paid once: Dart is new to this project, and `app/` is its first real build
  toolchain.
- **The hub gained authentication and a download, and no new data endpoints.** Every v1
  screen is composed from routes that already existed, are already scope-filtered and are
  already tested — `/api/machines`, `/api/alerts`, `/api/wake/*`, `/api/fleet/favorites`,
  `/api/fleet/commands`. A screen needing data the console does not serve is a screen
  outside v1, not a reason for a route.
- **No Socket.IO in the app.** It is `transports=["polling"]`, CORS-pinned to `HUB_URL`,
  and costs a waitress thread per open connection. A native client would have forced that
  origin list open — the exact thing app.py's own comments warn against with cookie auth.
  The app polls `/api/machines` every 10 s, which is what the Inventory page already does
  at 30 s. Live temperature is not worth reopening CORS for.
- **Push is phase 2, and v1 needs none.** A running desktop app holds its own connection
  and raises its own toasts from the alert delta; an app that is not running has nothing to
  notify. Building an FCM sender now would mean shipping untested plumbing against no
  client. The `api_tokens` table carries nullable `push_kind`/`push_token` columns so the
  phone build lands on the device registry that already exists rather than a second one
  that could disagree about which devices there are.
- **English only in v1, catalogs bundled and runtime-switchable in v2.** No string is
  written inline (`app/lib/strings.dart`), which is the whole v1 cost and what makes v2 an
  asset drop instead of a sweep through every screen. v2 bundles rather than fetches: this
  app gets opened in a car park on a phone with no signal, and labels that depend on a
  round-trip are labels that are sometimes blank.

### Built (hub side) — device tokens

`apitokens.py` (model, flask-free) + `apitokens_web.py` (pairing, device administration,
the download page), `clientrelease.py` (the signed client catalogue), a Devices tab on the
Users page, a Download Client page, and `--sign-client` in `sign_release.py`.

**Authentication is a second way through the one existing gate.** `login_required` is the
single seam every console route passes through and is injected into every blueprint, so
one change covers the whole surface: a `Bearer tmu_<token_id>:<secret>` header resolves to
a device identity, and the session path is untouched. Three properties carry the security
of adding a months-long credential on somebody's laptop to a hub whose entire perimeter has
been `ALLOWED_EMAILS` + a session since command signing was removed:

- **A token is a CEILING, intersected LIVE.** The capability subset is chosen at pairing;
  the effective set is `token ∩ owner's real capabilities`, recomputed per request in
  `permissions_web._narrow_to_device`. Without the intersection a token minted while
  someone was an admin keeps admin after they are demoted — the credential outliving the
  grant is exactly the failure a bearer token introduces and a session does not.
  `login_allowed` is re-run per request too, so removing somebody from every group kills
  their paired devices immediately rather than at expiry.
- **`manage_settings`, `manage_users`, `manage_permission_groups` and `manage_firmware`
  are refused at MINT time.** Console administration stays in the console, and the cheapest
  way to guarantee a laptop in a bag cannot rewrite the permission model is for the
  credential to have never been able to. Even a break-glass superuser is refused.
- **Tokens expire and the expiry slides on use**, against each row's OWN `lifetime_days`
  rather than the current fleet default — the same rule `packages.py` applies to retry
  policy, so raising `hub.device_token_lifetime_days` never silently extends a device
  somebody agreed to a shorter lifetime for.

**Pairing: RFC 8252's native-app flow, because there is no password and there must not be
one.** The app binds a loopback listener, opens the SYSTEM browser at `/app/pair`, the hub
authenticates it through the existing OAuth path (`_complete_login` untouched), an operator
confirms the device and its capabilities on a consent page, and a one-time code comes back
to loopback and is exchanged once.

- **Nothing is minted until the code is exchanged.** The consent page records a GRANT; the
  token does not exist until the device collects it, so an abandoned pairing leaves no
  credential and no plaintext token is ever at rest, not even for the sixty seconds the
  code lives. Single-use is a conditional DELETE, so a race has exactly one winner, and an
  expired code is CONSUMED rather than left for a second attempt.
- **The redirect is validated as literal loopback only** — `127.0.0.0/8` or `::1`, an
  unprivileged port, no fragment, no credentials, no `localhost`. This is the sharpest edge
  in the feature: the redirect carries a code that becomes a fleet credential and arrives
  in a query string, so an open redirect here mails somebody's console access to whoever
  crafted the link. `localhost` is refused deliberately even though it usually resolves
  correctly — it is a NAME, resolved through a hosts file the hub cannot vouch for — and the
  refusal names the fix rather than being silent about it.
- **A copy-paste code is the fallback**, built now rather than retrofitted, because it is
  also the path a phone takes through a custom URL scheme in phase 2.

**Two implementation-level notes worth carrying forward:**

- **A device token must never become a session.** The identity is stashed on `flask.g`, not
  in `session`, because writing it into the session would make Flask return a signed
  session cookie — and that cookie would carry no capability ceiling. A token that can be
  laundered into something stronger than itself is not a ceiling at all.
- **The CSRF content-type rule does not apply to bearer callers, and that is the rule
  rather than an exception to it.** app.py's own note already says CSRF rides an AMBIENT
  credential; a bearer header is not ambient, which is why `/api/agent/*` is correctly
  outside the check. `tests/test_csrf.py` pins both halves together, so a change that
  relaxed the cookie path while "keeping" the token path fails.
- **`tmu_` is an authentication boundary, not a naming convenience.** Agent auth is
  `Bearer <agent_id>:<token>`, parsed by five copies of `_bearer_agent`. The prefix makes
  "agent token at a console endpoint" and "user token at an agent endpoint" parse failures
  at the door rather than lookups that happen to miss. Both directions are tested.

**Also cleaned up here** (found while wiring this up): ten blueprints each spelled out
`(session.get("user") or {}).get("email", "unknown")` for the audit actor. They now all
call `permissions_web.current_actor()`, so an action taken from the app is attributed to
the operator who paired it instead of being recorded as `unknown` — one implementation of
"who is this caller", for the same reason `login_required` and `access` are passed around
rather than re-declared.

### Built (hub side) — the Download Client page

A first-class console page that renders **whatever the signed manifest lists**, never a
hand-maintained list of links: a hardcoded page is one that offers an Android build three
weeks before the APK exists and keeps offering one that was pulled. Adding a platform is a
release action — build, re-sign, push — with no hub change and no console change.

- **The manifest holds a LIST of builds.** The agent's is `{version, sha256, url}` because
  it has one artifact; a client has several and will have more, so this is
  `{version, builds: [{platform, arch, kind, filename, size, sha256, url}, …]}`, signed as
  one document with the **same Ed25519 key that signs the agent**. One trust root for every
  artifact this project ships, because a second signing key is a second private key to keep
  safe.
- **The signature is verified before the page renders**, not before the download. The
  console is somewhere operators already trust; an unverified page there would let anyone
  who could write a file into the hub's code directory hand the whole helpdesk a binary.
  Failure is a 503 carrying the reason — "no client has been published yet" and "this
  manifest is not signed by your release key" are different situations, and an empty list
  would render them identically.
- **iOS is a `link` kind, not a file.** Apple does not permit installing an app from a
  link to a file, so the distinction exists from the start rather than arriving later as a
  special case in the template.
- **The manifest ships under `hub/`, not under `app/`** — load-bearing, and the reason it
  is called out. The hub's self-updater mirrors the `hub/` directory and nothing else, so a
  manifest beside the Flutter sources would never arrive on an installed hub: the identical
  shape of the 1.27.x bug where `packages.py` was left out of the runtime file list and the
  hub died on import. `/download/manifest.json` and its `.sig` are served unauthenticated
  as the EXACT bytes on disk, because an installed client needs them to check for its own
  update before it has been paired with anything, and re-serialising them would break the
  detached signature.

### Built (client side) — the update check and the release script

**The client verifies the same signed manifest before offering an update**, with the same
embedded public key the agent's `SelfUpdater` uses. Checked once per launch, after the
device is paired. Both `/download/manifest.json` and its `.sig` are unauthenticated on the
hub deliberately, so this works before a device has been paired and keeps working after its
token is revoked — an app that could not tell you it was out of date until you signed in
would be least useful exactly when it was most stale.

- **It asks; it never installs.** The agent self-updates because it runs unattended as
  SYSTEM. This app has a person in front of it, so the dialog names the version, shows the
  sha256 the download page publishes, and opens the signed URL in a browser — where the
  operator can see what they are downloading and their own AV will look at it. That
  resolves the open parameter this entry left ("self-update, or just report?").
- **"Skip this version" is per VERSION, not forever.** Without it an operator who cannot
  install software today sees the same dialog every launch and learns to dismiss it unread
  — and the one they eventually dismiss unread is the one carrying a security fix.
- **A release with no build for this machine is not an update.** An Android-only release
  must not send a Windows operator to a page with nothing on it for them.
- **Every refusal is tested against REAL Ed25519**, with an ephemeral keypair rather than a
  stubbed verifier: unsigned, signed-by-another-key, and edited-after-signing all have to
  produce "not an update", and a verifier tested against a fake verifier tests nothing.

**`release_client.py` is the client's equivalent of `--sign-agent`**, and it refuses rather
than warns. It runs `pub get` / `analyze` / `test` / `build` / package / sign, stopping on
a failing analyzer or a red test: a release script that ships a red build is worse than
none, because it looks like a process.

**It ASKS which version to publish**, rather than inferring it from whatever was left in
the tree. That is the decision most easily got wrong by muscle memory, and its failure is
silent: publishing a version that is not strictly newer than the last one means no
installed client ever compares it as an update, and nobody finds out. So the prompt names
what is currently published, offers the patch/minor/major bumps, warns when the answer is
not newer, and makes the *bump* the blank-Enter default rather than "keep". Choosing a new
version rewrites `app/lib/version.dart` and `pubspec.yaml` before the build, because that
constant is compiled into the binary and is what the update check compares against.
`--version` and `--keep-version` are the non-interactive answers.

**`sys.stdin.isatty()` is checked but not trusted**, and that is worth recording: under
MSYS/Git Bash it returns True even with stdin redirected from `/dev/null`, so a tty check
alone would hang a CI runner on a prompt nobody can see. `EOFError` is the real guard, and
the safe answer to it is "publish what is in the tree" rather than a guessed bump.

### Built (client side) — one instance per session (1.1.0)

**Launching the exe twice used to give the operator two of everything.** The window's X
hides to the tray rather than closing, so "running but invisible" is this app's ordinary
resting state, and double-clicking the exe again is the ordinary way back to it — not an
edge case. Two windows would have been cosmetic. **The cost that mattered was a second
alert poller**: each instance keeps its own delta seen-set, so every new alert toasted
twice, which is exactly the "operator learns to dismiss FleetHub notifications unread"
failure the priming and reset rules above were built to prevent.

A second launch now takes a named mutex, finds it held, wakes the running instance and
exits 0; the running instance un-hides, un-minimises and takes the foreground. Three
decisions inside that:

- **The guard is in the C++ runner (`windows/runner/single_instance.cpp`), not in Dart.**
  A Dart check runs after the engine has booted and the window exists, so the losing
  instance would flash a window before vanishing. In `wWinMain` it is invisible.
- **The mutex is session-scoped, not machine-wide.** Two operators on one box over RDP each
  get a client, each signed in as themselves with their own device token. A machine-wide
  mutex would have handed the first one an app and the second one a process that exits
  without explanation — the wrong answer for a helpdesk group.
- **The wake-up is a broadcast registered message.** It reaches hidden top-level windows,
  which is the state the running instance is usually in, and it avoids identifying the
  other process by window class — the runner's is the stock `FLUTTER_RUNNER_WIN32_WINDOW`
  that every Flutter app on the machine shares. What a broadcast cannot do is carry data,
  so the phase-2 `fleethub://` URL scheme will need a window lookup to forward the second
  launch's command line.

**This forced a change to how the generated runner is stored.** `app/windows/` was ignored
wholesale so a Flutter SDK upgrade would not arrive as a thousand-line diff. The guard
cannot live in `lib/`, and neither can the exe's `VERSIONINFO` block — which was still
Flutter's `com.example` boilerplate, and now names a real publisher and the GPL. Five files
under `windows/runner/` are therefore tracked, and everything else there stays generated.
`flutter create` overwrites those five with boilerplate, which is a **silent** failure — the
build stays green and the client just quietly opens second copies again — so
`release_client.py` checks all five before packaging and **refuses the release** rather than
warning, the same shape as its existing version-agreement check.

### Not yet done

- **The client has never talked to a hub.** It builds and it launches, but pairing, the
  poll loop and every endpoint behind them have only ever run against tests and fakes.
  This is now the one thing gating everything else here, including the Android port below.
- **Packaging is a zip, not an installer.** An unsigned installer is a SmartScreen warning;
  an unsigned zip is a folder. Revisit as MSIX once there is a code-signing certificate,
  which is also what would let toasts stop depending on `local_notifier`'s Start Menu
  shortcut for an application identity.
- **Phase 2**: the Android/iOS builds, FCM/APNs against the `push_*` columns already in
  place, and the bundled localization described above. Scoped in detail below.

### Phase 2 — what an Android build actually costs

Scoped rather than assumed, because "it's Flutter, so it's the same code" is true enough to
be misleading. Most of it IS the same code; the part that is not is precisely the part that
makes the current build a *desktop* app.

**Shared verbatim** — `models.dart`, `state.dart`, `api/`, `auth/pairing.dart`,
`auth/token_store.dart`, `update/updater.dart`, `strings.dart`, and every screen (fleet,
alerts, wake, commands, settings, the update prompt). That is the bulk of the client, and
it is the whole reason Flutter was chosen over React Native + Electron.

**Needs an Android counterpart** — checked against the resolved packages, not assumed:

| Plugin | Ships native code for |
|---|---|
| `window_manager` | windows, macos, linux — **no android** |
| `tray_manager` | windows, macos, linux — **no android** |
| `local_notifier` | windows, macos, linux — **no android** |
| `flutter_secure_storage` | android, ios ✅ |
| `shared_preferences`, `url_launcher` | federated, android ✅ |

1. **`main.dart` needs a platform branch.** Window sizing, the tray icon and close-to-tray
   are three desktop-only plugins, and a phone has no tray. The whole startup path splits.
2. **`notify.dart` needs a second implementation.** `flutter_local_notifications`, a
   notification channel, and the `POST_NOTIFICATIONS` runtime permission on API 33+. The
   alert-delta logic above it is unchanged and already tested.
3. **`NavigationRail` is a desktop shape.** A phone wants a bottom `NavigationBar`.
4. **Pairing works as-is but is not idiomatic.** The loopback listener functions on Android
   (the device's own browser can reach `127.0.0.1`), and the copy-paste code path is
   already built. An app link or custom URL scheme is the shape a phone should use, and the
   hub's pairing flow needs no change for either — which is why the fallback was built now
   rather than retrofitted.
5. **Release: `flutter build apk`, plus an Android signing keystore** (separate from, and
   unrelated to, the Ed25519 release key). The manifest's `builds[]` list already accepts a
   second entry with no hub change and no console change — exactly what it was made a list
   for — so `release_client.py` gains an android branch and nothing else does.

**The gap that decides whether this is worth doing: background alerts.** On Windows the app
sits in the tray and polls, so an alert raised at 02:00 reaches an operator with the window
shut. **Android kills background polling.** Without FCM an Android build shows alerts only
while it is open on screen, which removes most of the reason to want it on a phone — so the
push work is not a later refinement of the Android build, it is a prerequisite for it being
useful. That work is: a Firebase project, a hub-side sender against the `push_kind` /
`push_token` columns already on `api_tokens`, device registration over the existing device
token, and **content-free payloads** ("3 alerts", tap to open) so no machine name or
temperature transits Google. iOS is the same shape with APNs, plus an Apple Developer
account, and would appear on the Download page as a `link`-kind build rather than a file.

**Build prerequisites**, none of which are on the current build machine: the Android SDK
and a JDK (`flutter doctor` currently reports "Unable to locate Android SDK"), plus the
keystore above for a release APK.

**Recommended order: not before the Windows client has actually run.** Porting a client
whose desktop half has never been launched against a real hub risks finding the same bug
twice, in two places.

---

## ✅ 12. Processes on the machine Overview   ·   hub 1.71.0 (+ agent, pending a signed release)

A task manager for a machine you are not sitting at. The Overview tab grows a **Processes**
card: every process grouped by name with its CPU, memory, owner and PID, expandable to the
individual instances, and **End task** / **Restart** on the right-click menu. It answers the
call that starts "this PC has been at 100% since lunch" without opening a SYSTEM shell to run
`Get-Process` and read the output back to somebody.

**Actions are on a context menu, not in a column.** Task Manager's shape, and for Task
Manager's reason: a table of a few hundred rows is scanned, and two buttons on every one of
them widen the row, pull the eye off the numbers the card exists to show, and are no easier
to hit than a right-click on the one row that matters. The menu is also where the two actions
can differ per row — a critical process cannot be ended, a group of twelve cannot be
restarted as one — without the table growing a column of greyed-out buttons. The rows are one
roving tab stop with arrow-key movement, so Enter and the Menu key open the same menu; with
the buttons gone that is the only keyboard path to an action, which makes it load-bearing
rather than polish.

**Reading the list is `view`; ending or restarting is `issue_commands`.** No new capability
— what is running on a PC is inventory in the same sense its disks and its sensor tree are,
and an operator who can open the machine page already reads both in full. Ending a process
is strictly less dangerous than the `shutdown` that gate already covers.

**The list rides the HEARTBEAT, and only while somebody is watching.** Sampling enumerates
every process, opens each one's token to resolve its owner, and asks WMI which of them host
services; the payload is ~60 KB. Neither is affordable on every PC in the fleet forever, and
neither is needed — the question is being asked about one machine, by one operator, for a
couple of minutes. So the console's poll IS the subscription: a read sets a watch
(`processes.note_watch`), and the agent samples every 5 s until the watch lapses ~45 s after
the operator leaves. A machine nobody has open does no process work and sends nothing.

**...but the machine ASKS whether it is watched, rather than waiting to be told.** The first
build learned it from the heartbeat, which is a 10-second tick — so opening the card cost a
heartbeat, then a CPU window, then a console poll, and the first list landed 10-15 seconds
after the click. Long enough that "waiting" had to be a rendered state explaining itself. An
agent has no inbound port, so nothing on the hub can push at it and the only lever is how
often it asks: `GET /api/agent/processes/wanted` every 2 s while idle, which is bearer auth,
one indexed lookup on a one-row-per-machine table and a ~30-byte answer, with no writes on
either side. That is now the *only* thing an unwatched machine does for this feature — the
expensive half is still strictly demand-driven — and the first list arrives in a few seconds.
The heartbeat's `processes_wanted` stays: it is how a machine learns to **stop**, and the
only path for an agent that predates the poll. The console matches it by polling once a
second until the first list lands, then settling to the hub's cadence.

**Listing is deliberately NOT a command.** The obvious design — a `list_processes` command
per refresh — would have written a security-level row into `audit_log` every few seconds for
the crime of reading a list, and that trail is never pruned by design. Commands are for the
two things that change the machine.

**PID reuse is the hazard the whole feature is shaped around.** A snapshot is seconds old by
the time somebody clicks, and Windows recycles process ids within minutes, so a pid alone is
an instruction to kill whatever now holds that number. Every action carries the NAME the
operator saw alongside the pid; the agent re-reads the live process and refuses a mismatch
rather than resolving it helpfully (`ProcessGuard.NameMatches`). Both ends validate, and the
generic `/api/fleet/commands` endpoint refuses these types outright so there is one door.

**Critical processes are refused, and the refusal is shown rather than hidden.** Ending
csrss, wininit, winlogon, services, lsass or smss does not close a program — it bugchecks
the machine. `svchost` is refused for a different reason: it is killable, and killing one
takes every service in it down (for the RPC host, that reboots the box in a minute), so the
refusal points at `restart_process` instead. The agent's copy of the list is the authority
and is checked against the live process; the hub's copy exists so a refusal is immediate in
the console and so a hand-rolled API call never becomes a queued command. `explorer.exe` is
deliberately killable — that is a real helpdesk action and Windows brings it straight back.

**"Restart" means three different things, and the machine decides which.** A process hosting
exactly one Windows service is restarted AS that service (stop, wait, start, wait, and its
running dependents brought back explicitly, which Windows does not do) — killing it instead
leaves the SCM believing it crashed, and a service with no recovery action then simply stays
down. A process hosting several is refused with them named, because there is no honest way
to guess which one was meant. Anything else is relaunched from its own image in the Windows
session it was running in, **as the user who was running it** (`UserSessionLauncher`, via
WTSQueryUserToken) — not as SYSTEM, which would produce a program with no profile, no mapped
drives and no access to the documents it exists to open. This is the mirror image of
`SessionInjector`, which retargets the service's SYSTEM token into a session precisely
because the remote-control helper must be SYSTEM.

**CPU is a rate, so it cannot come from one look.** `ProcessReader` keeps a baseline and
reports the delta over the window divided by (elapsed × logical processors) — the share of
the whole machine, the way Task Manager's Processes tab counts it. The one-look alternative
(cumulative CPU time over uptime) would show a browser that has been open since breakfast at
a permanent 4% while it pins a core right now.

**Remaining gaps.** No per-process disk or network I/O (Task Manager has both; they need
`GetProcessIoCounters` and a second delta, and CPU + memory is what the card was asked for).
No "open file location" or "go to service", no column chooser, and no fleet-wide view — this
is per-machine by construction. The 400-process cap is counted and admitted in the UI rather
than silently applied.

---

## ✅ 13. Remote file explorer (Tools → Files)   ·   hub 1.88.0 (+ agent, pending a signed release)

Browsing a managed machine's disk, and moving files both ways. The Tools page grows a
**Files** tab: the machine's volumes, then folders, with **Download**, **Upload**, **Copy**,
**Cut/Paste**, **Rename**, **New folder** and **Delete**. It answers the two calls the
console previously could not — "the report is on my desktop and I can't open it" and "put
this driver in C:\Temp for me" — which until now meant a SYSTEM shell, because a terminal
moves text and not bytes.

**Browsing is `issue_commands`, and that is the one departure from #12.** The Processes card
reads behind `view` on the argument that a process list is inventory. A folder listing is
not: those are the names of somebody's documents, and the same door reads the bytes inside
them. The capability adds nothing to anyone who already holds it — they can open a SYSTEM
shell on the same machine from the tab next door — while putting it at `view` would have
handed a read of every user's files to everyone who can see a temperature graph.

**A listing IS a command, unlike a process list — for the same reason, read the other way.**
#12 keeps enumeration off the command queue because the console polls it every five seconds
and a security-level audit row per read would drown a trail that nothing prunes. Browsing is
the opposite on both counts: it is human-paced, one click and one listing with no poll behind
it, and "who opened the payroll folder, and when" is exactly what the trail should be able to
answer. So each navigation is one audited command — and the listing itself is POSTed to a
dedicated endpoint rather than returned as command output, because two thousand entries is
~200 KB and the command channel's output is a terminal transcript truncated at 16,000
characters.

**Transfers are two hops through a hub spool, in both directions.** The machine and the
operator's browser are never awake on the same network at the same instant — the assumption
this whole product exists to avoid — so a download is the agent PUTting bytes to the hub and
the browser collecting them from `logs/filespool` when the transfer goes ready, and an upload
is the reverse. Capped at 2 GB either way, expired after an hour with the spooled bytes
deleted, and a folder download is **zipped on the machine** (the hub never sees the folder,
and 200 MB of source becomes 30 MB over the link that is actually slow). Downloads are served
`as_attachment` with an octet-stream type: those bytes are entirely under the control of
whoever put them on that machine, and inline an uploaded `.html` would run as script on the
hub's own origin against the operator's session.

**The upload is two requests, and the split is a CSRF control rather than an ergonomic one.**
Multipart is the one state-changing shape a cross-site HTML form can still produce, so the
endpoint that receives the bytes is inert by construction — it spools them, returns an id,
and touches no machine — and is exempted by name in `app.CSRF_UPLOAD_ENDPOINTS`. The JSON
call that aims them at a folder and queues the command is covered by the ordinary rule. Same
arrangement `packages.upload_package_file` already uses.

**Paths are validated at both ends, and the agent's copy is the authority.** No relative
paths, no `..` component (not to stop an escape — there is nothing to escape — but so the
breadcrumb, the audit row and the command all name the same folder), no device namespaces, no
deleting a drive, and no moving a folder inside itself. There is deliberately **no blocklist
of folders**: the same operator can already read any file on the machine from the Terminal
tab, so a list of forbidden directories would be a map rather than a control.

**Delete is permanent and says so.** The agent runs as SYSTEM with no interactive profile, so
a deletion cannot go to the person's Recycle Bin — it would land in SYSTEM's, where they
cannot reach it. The confirmation names every item and states there is no undo, which is a
better answer than an undo only a disk editor could use.

**Remaining gaps.** No drag-and-drop, no multi-file upload (one file per push), no folder
upload, no in-console preview or edit, no search within a folder, and no progress bar for a
transfer in flight — the panel says which stage it is at, not how far through. The
2,000-entry listing cap is counted and admitted in the UI rather than silently applied.

---

## PRD v2.0 triage   ·   2026-08-31

Where items #14 and up came from. A *Product Requirements Document* v2.0 ("Advanced RMM
Platform", dated 2026-08-28) was written against the RMM market rather than against this
codebase, and the two disagree about what this product is. Its §3 specifies Kubernetes
microservices, a multi-tenant control plane, a C++/Rust cross-platform daemon, gRPC, and
Prometheus/Cortex over PostgreSQL; its §5 asks for 100,000 endpoints per cluster, a 99.99%
SLA and SOC2 Type II at launch. FleetHub is one Flask app and a SQLite file behind a WinSW
service, talking to a C# Windows service over an HTTPS heartbeat and a command queue. That
is not a v2 of this; it is a different product.

So the document was treated as an **idea source, not a specification**. What follows is
what survived, and why the rest did not. The two decisions that framed the triage:

- **Internal now, MSP later.** The audience is one helpdesk group, not managed-service
  clients. Multi-tenancy, white-label reporting, per-client billing and the plugin
  marketplace therefore drop out entirely. What is kept is the discipline of not
  *foreclosing* tenancy: new tables carry their own owning key rather than assuming one
  fleet, and no new code reads "the machine list" as a global.
- **Statistics first, AI on top, and not one vendor's AI.** Every AI requirement in the PRD
  is downstream of data this hub does not yet collect, which is why none of them is near
  the front. When they arrive, correlation and scoring are ordinary statistics over stored
  outcomes — explainable and testable — and any language layer above them sits behind a
  **pluggable provider**, configured the way `authconfig.py` already makes OIDC pluggable
  (validate / merge / to_env / redacted, plus a settings page). No provider is privileged
  by the schema.

| PRD item | Verdict |
|---|---|
| FR-01 Asset discovery | **Partial.** AD/LDAP computer sync is #4. IP-range scan, Intune sync and shadow-IT detection are not built → **#18**. |
| FR-02 Monitoring | **Mostly done.** CPU, RAM, disk, network and thermal all ship. GPU and disk-I/O counters are a small addition to an existing reporter. Event-log mining → **#16**; anomaly baselining → part of **#17**. |
| FR-03 Alert brain | **Substrate exists** — `alerts.py` already keeps episodes and `rules.py` keeps `rule_fires`. Grouping, correlation and recommended-fix → **#17**, deliberately *after* #14 and #16, which are what give it something to correlate. |
| FR-04 Automation | **Largely done.** Rules, scripts and multi-step packages cover the scripting engine, the conditional logic and the script library. Self-healing watchdogs → **#20**. The drag-and-drop workflow builder and the RPA "new user creation" flow are **rejected**: a visual builder over a rules engine one team uses is UI for its own sake, and AD/Exchange/Teams provisioning is a different product. |
| FR-05 Patching | **Missing entirely** → **#14**, the first thing built. |
| FR-06 Remote access | **Done** — remote view/control, an interactive terminal and hub-proxied file transfer all ship. Session recording and malware-scanning on upload → **#19**. |
| FR-07 Security | Encryption-key escrow → **#19**. Shadow-AI detection is cheap and lands there too: it is an inventory query, not a new channel. EDR integration is **deferred** — there is no EDR here to integrate with, and the requirement is an API shape rather than a feature. |
| §5 NFRs | **Rejected as written.** 100k endpoints, 99.99% and SOC2 describe a hosted platform with a team behind it. The NFRs this product can actually be held to are the ones already implicit in the code: an agent that stays invisible, a heartbeat that degrades gracefully, and a hub that survives its own restart. |
| §6 UI | Dark/light ships; mobile is **#11**, in flight. Keyboard-first is a **UX principle**, recorded here rather than tracked as a feature. |
| §7 Integrations | **Rejected.** The outbound webhooks in `notify.py` already cover the real need. PSA sync, ITSM connectors and a plugin marketplace are all MSP-shaped. |
| §9 KPIs | **Rejected.** "500+ community scripts on day one" and an eNPS target do not describe this. |

One item below — **#15** — is not from the PRD at all.

---

## ✅ 14. Patch management (OS + third party)   ·   hub 1.92.0 + agent 3.35.0

The largest real gap the triage found, and the one the console is most obviously missing:
nothing here answers "is this fleet patched?". The insight that sizes it is that **patching
is the package engine with a vendor-supplied catalog**. `packages.py` already has sources,
sha256-pinned blobs, detection rules, multi-step recipes, a dispatch/reconcile loop,
per-target state, cancel and retry-failures. None of that gets rebuilt; a patch run is a
deployment whose contents came from Windows Update instead of from an operator.

**Inventory is change-only on the heartbeat**, the way `BiosInventoryReporter` and
`NicReader` already report. Two sources on the agent: the `WUApiLib` COM search interface
for OS and driver updates, and `winget upgrade` for third-party. COM is preferred over
`UsoClient` or the `PSWindowsUpdate` module because it returns structured results and needs
nothing installed that is not already on the machine.

**An install is confirmed by re-reading inventory after the machine comes back, never by an
exit code.** This is the discipline `FirmwareFlasher` established and it is here for the
same reason: a patch that reports success and did not apply is a failure that looks like a
success, which is the direction that hurts. A run therefore stays in `rebooting` — for a
day if need be — rather than being closed by the command result.

**Approvals are fleet-wide per update; windows are per group.** Approving a KB is a
statement about the patch, and it does not become a different patch on a different desk.
*When* it may run is the thing that legitimately varies, so the maintenance window carries
the scope. Auto-approval of security and critical classifications is a setting, not a
default. **Emergency push** is a run that ignores every window, gated on a new
`manage_patches` capability — its own toggle rather than a reuse of `deploy_packages`,
because forcing a reboot outside an agreed window is a different thing to hand someone than
pushing an installer inside one.

Hub side is `patches.py` (`machine_patches`, `patch_approvals`, `maintenance_windows`,
`patch_runs`, `patch_run_targets`, and a `tick()` registered beside `packages.tick`) plus
`patches_web.py` and a Patches page. Agent side is `Patch/PatchInventoryReporter.cs` and
`Patch/InstallPatchesExecutor.cs`.

**Built so the AI half needs no schema change later.** `patch_run_targets` records machine
model, OS build, outcome and time-to-recover from the start, so the PRD's "stability
scoring" becomes a query over history rather than a migration. That is the whole of the AI
preparation, and it is free.

**Open parameters.** Retention for `machine_patches`, and whether a superseded update is
deleted or tombstoned. Whether the reboot is agent-initiated inside the window or a separate
verb.

---

## 📋 15. Cross-hub machine sharing

Not from the PRD. A colleague running his own hub wants a second pair of eyes on his PC. He
shares **that one machine** with another hub, granting only remote view; it appears in the
borrower's machine list and does exactly that and nothing else. Widen the grant and it does
more. The requirement that shapes the whole design is the negative one: the borrowing hub
must never be able to repoint the agent — not its `HUB_URL`, not its
`AGENT_ENROLLMENT_SECRET`.

**That guarantee comes from removing the channel, not from adding a check.** An agent has
exactly one home hub, and the borrowing hub never speaks to it: hub B calls an API on hub A,
hub A validates the grant and queues the command on **its own** queue to **its own** agent.
The agent is never told hub B exists. So there is no permission to get wrong and no request
to filter — a fully compromised borrowing hub can still only ask hub A for actions inside
the grant, and neither the hub URL nor the enrollment secret is an agent command verb in the
first place. Both are install-time config on the machine, which is why `settings.py` refuses
to hold secrets at all and `fleet.py` already says an agent must ignore anything that would
redirect it.

**A shared machine is never enrolled on the borrowing hub.** No row in its fleet or
enrollment tables, excluded from its fleet-wide operations and its counts, and badged as
borrowed everywhere it appears. It is somebody else's machine seen through a window, and the
console should never let that blur.

**`apitokens.py` is the pattern, one level up.** It already implements the hard part — *a
token holds a SUBSET of its owner's capabilities, intersected LIVE* — along with a
single-use expiring grant flow (`create_grant` → `redeem_grant` → `mint_token`),
`normalize_capabilities` and revocation. A peer hub takes the device's place; the one
addition is that a share is also scoped to a single machine. Pairing reuses that trio
directly: hub A generates a one-time code and its URL, the operator pastes it into hub B,
which redeems it for a long-lived peer token. Administrative capabilities are refused at
share time, mirroring `DEVICE_FORBIDDEN_CAPABILITIES`.

**Nothing rides along implicitly — the owner picks each capability.** In particular `view`
does not travel with `remote_control`. A remote-view-only share shows a stub: hostname,
online state, a Connect button, and no telemetry at all. `view` is its own toggle everywhere
else in this product, and quietly bundling it here would make the smallest possible share
bigger than "look at my screen", which is the exact case the feature exists for.

**Expiry is optional per share**, because both shapes are real: a few hours for "come look at
this", none for two hubs that habitually cooperate. Revocation is immediate and must **kill
sessions in flight**, not merely refuse the next request. Every borrowed action is audited on
both hubs; hub A's trail is the authoritative one and names the peer hub and the remote
operator beside the action.

**Open parameters.** Whether granted telemetry is mirrored into the borrowing hub or proxied
live on each read. Whether one grant may cover several machines or strictly one. How a
borrowed machine is keyed when its hostname collides with a local one. What the borrowing hub
shows while hub A is unreachable.

---

## ✅ 21. Release channels — stable / beta   ·   hub 1.93.0 + agent 3.36.0

Not from the PRD. Everything in this product updates from `main` and reaches everything at
once: a signed agent manifest landing on main starts upgrading the whole fleet within about
fifteen minutes. That is the right default and the wrong only option, because it means a
release is tested for the first time on every endpoint simultaneously. A **beta** channel
gives a handful of machines the build first — a fleet default plus a per-machine pin, so the
pilot ring is a few rows rather than a second fleet.

**The hub sends a channel NAME, never a URL, and that is the whole security argument.**
`State/RuntimeConfig.cs` already forbade the hub from redirecting where an agent gets its
code, and enforces it with an allow-list rather than a rule to remember. A name does not
break that: the agent holds BOTH manifest URLs compiled in and the hub picks between them, so
the worst a compromised hub can do is move a machine onto the other train — where the manifest
is still signed by the same offline key and the binary's sha256 is still checked against that
signed value. It cannot introduce a destination the agent did not already trust. A URL from
the hub would have made the hub the trust root; this does not.

**One increasing number sequence, shared by both channels**, because `VERSIONING.md` forbids
suffixes and four comparators enforce it. Beta is not parallel numbering; it is where a number
appears *first*. Two things follow, and they are what keep this feature small:

- **Promotion is a file copy.** `agent.manifest.beta.json` over `agent.manifest.json`. Every
  pilot machine is already at that version and does nothing; every stable machine updates to
  it. Nothing is renumbered and no machine is asked to move backwards.
- **Leaving beta does not roll anything back.** Every updater installs only what is strictly
  newer, so a machine that took a beta keeps it and simply stops updating until stable catches
  up — which, for a promoted beta, is immediately. Building a downgrade would have meant
  defeating the guard that currently stops a replayed or rolled-back manifest being installed.
  The console says "ahead of stable" instead, because a PC that quietly stops updating looks
  broken and this is the one place an operator will be surprised.

**The channel does NOT travel the `agent_config` channel**, and that is a deliberate
departure. That payload is fleet-wide and content-hashed, so a per-machine value would give
every machine its own hash and destroy the caching that keeps the 10-second heartbeat a
two-field response. The channel rides as its own per-machine field in the heartbeat reply
beside `processes_wanted`, with its own parser on the agent — the two must not be able to lose
each other, for the reason the watch flags already document.

**The hub's own channel selects a git REF rather than a manifest**, because the hub has no
manifest: it reads `HUB_VERSION` out of `app.py`. Beta hubs track a `beta` branch. The client
follows the agent's shape — a second signed manifest in `hub/`, which the hub's self-updater
already mirrors wholesale.

**`sign_release.py` needed no change.** It already took `--manifest`, so beta is the same
signer, the same key and the same bytes-exact discipline writing one file over. `release.ps1`
grew `-Channel`, which also tags the release `agent-v<version>-beta` and marks it a
prerelease — for humans reading the releases list, since the filename is what actually decides
who installs it. Both new manifest pairs are pinned `-text` in `.gitattributes` **before** the
first beta is published, because a line-ending rewrite between signing and serving is
indistinguishable from tampering.

**Open parameters.** Whether an abandoned beta should expire — a machine left on one is the
failure this design cannot see by itself. Whether the ring should become a rule rather than a
column once it outgrows a handful. Whether `install.ps1` should take a channel, so a machine
can be born into the ring.

---

## 📋 16. Event log mining

Windows Event Log (Security, Application, System) ingested on the agent and correlated into
alerts — the PRD's example, 4625 failed logons, is the right one. This is the first genuinely
**high-volume** channel this product would carry, and that is the whole design problem: a
SQLite hub cannot hold raw logs for a fleet, so filtering happens **on the machine**, against
an operator-defined set of event IDs and levels, and only matches travel. Retention is decided
before the first row is written, not after.

Its real value is as an input to #17, which is why it comes first.

**Open parameters.** Push on the heartbeat or a dedicated channel. Whether a rule can be
written against an event directly, or only against a counter derived from one.

---

## 📋 17. Alert correlation and recommended fixes

The PRD's "Alert Brain". One machine with high CPU, low memory and a dead service should be
**one** bundle, not three alerts. The substrate is already here — `alerts.py` keeps episodes,
`rules.py` keeps `rule_fires` — so this is grouping over data that exists, plus the two new
inputs from #14 and #16 that make the grouping worth doing.

**Correlation is deterministic before it is anything else**: same machine, overlapping episode
windows, known causal pairs (a full disk, and the service that then failed to write). That is
a rule set and a time window, it is testable, and it never invents a cause. Only the *wording*
on top — a recommended fix, a suggested script — goes to a language model, behind a
**pluggable provider** with no vendor privileged in the schema, off by default, and with the
PRD's own mitigation kept: a suggested script is drafted into the existing script library for
a human to run, never executed from the suggestion.

Anomaly baselining (FR-02) lands here too, and for the same reason: a per-machine baseline is
a statistic over the history table, not a model.

**Open parameters.** How much reduction is worth claiming (the PRD's 75% is a marketing
number, not a target). Whether a bundle is a first-class row or a view over alerts.

---

## 📋 18. Network discovery and shadow IT

The half of FR-01 that #4 does not cover: an IP-range sweep from an agent already on that
subnet — the same peer-relay trick #10 uses for Wake-on-LAN, since the hub is usually not on
the network it is being asked about — plus an Intune/Azure device cross-reference, and a flag
for anything on the wire that no hub knows about.

Lower priority than it looks. On a fleet where enrollment is driven by MDM the answer is
usually "nothing", and a discovery sweep is a scanner pointed at a colleague's network.

**Open parameters.** Whether discovery is scheduled or on demand. What a discovered-but-not-
managed device is allowed to become without someone approving it.

---

## 📋 19. Endpoint security odds and ends

Three small things from FR-06 and FR-07 that share no code but are each about a day:

- **BitLocker key escrow.** Auto-enable, and escrow the recovery key into the
  master-key-wrapped secret store `backups.py` already provides and the BIOS setup password
  already uses. The custody warning that applies to `BACKUP_MASTER_KEY` applies double here.
- **Shadow-AI detection.** Flag unsanctioned GenAI apps. A query over installed software the
  inventory already collects, plus a policy list — no new channel, and worth being honest in
  the UI that a policy list is not an enforcement.
- **Remote session recording.** Video and keystroke capture for the audit trail, stored
  outside the hub DB. The one with real cost, and the one with a real question attached:
  recording a colleague's screen is a decision to take deliberately, not a default to leave
  on.

---

## 📋 20. Self-healing watchdogs

FR-04's watchdogs: a service stops, the agent restarts it inside two minutes, and only an
escalation reaches a human. A thin layer over the rules engine rather than a new one — a rule
already has a condition and an action, and `validate_actions` already refuses to let someone
who may raise an alert also issue a command.

What it adds is **local evaluation**: today a rule is evaluated hub-side on reported data,
which is the wrong latency for "restart this within two minutes" and useless if the machine is
briefly offline. A watchdog is a small rule the agent holds and runs itself.

**Open parameters.** Flap protection — how many restarts in what window before it stops and
escalates instead. Whether a locally held watchdog is authored separately, or is an ordinary
rule marked for local evaluation.

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
9. ~~**#9 BIOS settings + firmware management**~~ — done. Fills in the `update_bios`
   stub that has been queued-but-unimplemented since the signing removal, and adds the
   settings half it never had. Its prerequisite is **done**: `manufacturer` is collected,
   stored, merged and shown as of hub 1.57.0 (+ agent 3.18.0 / companion 2.12.0), so
   vendor dispatch now has something to dispatch on. The **change-only settings inventory
   is done too** (hub 1.59.0 + agent 3.19.0): three vendor sources, a `machine_bios` table,
   a Firmware tab, and `unsupported` kept firmly distinct from `error`. **The settings
   WRITE half is done as well** (hub 1.60.0 + agent 3.20.0): `set_bios_settings` behind a
   new `manage_firmware` capability, a `bios_changes` table, an editable Firmware tab, the
   BIOS setup password in the master-key-wrapped secret store (fleet-wide with a
   per-machine override, and never in command params), and a change confirmed by
   **re-reading the attribute** rather than by assuming a reboot — so `pending_reboot` is
   the only state that ever asks for a restart. **The `update_bios` executor is done too**
   (hub 1.61.0 + agent 3.20.0): `firmware.py`, a Firmware page holding the sha256-pinned
   image library and the fleet's update history, hardware/power preconditions that refuse
   by name at both ends, one attempt per machine with no retry, and a flash confirmed by
   the **BIOS version the machine reports after its restart** rather than by the command
   result — so a staged image waits in `rebooting` for a day rather than being called
   success by an exit code. **The agent half is pending a signed release** (build +
   `sign_release.py --sign-agent` + asset upload).
10. ~~**#10 Wake-on-LAN**~~ — done, hub 1.63.0 + agent 3.21.0. Both prerequisites were in
    place: #9's settings write turns the firmware-side enable on across a fleet, and the
    NIC inventory the hub did not collect is now a `machine_nics` table fed by a
    change-only agent reporter on the heartbeat. Delivery is **agent peer-relay** — the hub
    picks an awake machine on the sleeping one's subnet and asks *it* to broadcast — with
    the hub's own broadcast kept only as a same-subnet fallback. All four open parameters
    resolved (see the entry above); the sharpest was confirming a wake against the moment
    the packet was sent rather than against online-ness, which fails in the direction of
    looking like it worked. **The agent half is pending a signed release** (build +
    `sign_release.py --sign-agent` + asset upload).
11. **#11 Desktop & mobile app** — hub half done, hub 1.69.0: device tokens at the one
    `login_required` seam, RFC 8252 loopback pairing, a Devices tab, and a Download Client
    page driven by a signed manifest. The Flutter client in `app/` analyzes clean and its
    48 tests pass — including a signature-verifying update check and the pairing rejections
    — and **the Windows binary is built, signed and published as client 1.0.0**, which it
    was not when this line was first written. It launches; it has still never talked to a
    hub, which is now the outstanding item rather than the build.
    `release_client.py` automates that release the way `--sign-agent` does the agent's. Its prerequisite was #Foundation: a device holds a SUBSET of its owner's
    capabilities, intersected live, which is only meaningful because permission groups
    exist.
12. ~~**#12 Processes on the machine Overview**~~ — done, hub 1.70.0; refreshed at 1.71.0.
    Added after the numbered board was cleared, because "what is eating this PC?" turned out
    to be the one helpdesk question the console could not answer without opening a SYSTEM
    shell. It reuses two existing seams rather than adding a channel: the heartbeat carries
    the list (gated on a watch, so an unwatched fleet pays nothing) and the ordinary command
    queue carries the two verbs that change the machine. 1.71.0 added the one thing that
    could not be reused — a dedicated 2-second watch poll, because an agent has no inbound
    port and 10-second heartbeat notification was most of what made the first list feel slow
    — and moved the actions onto a right-click menu. **The agent half is pending a signed
    release** (build + `sign_release.py --sign-agent` + asset upload); until then the card
    names the version it needs instead of waiting forever on a machine that will never
    report.
13. ~~**#13 Remote file explorer**~~ — done, hub 1.88.0. Added for the same reason as #12
    and immediately after it: the other two helpdesk calls the console could not answer
    without a SYSTEM shell were "get that file off my desktop" and "put this one on it", and
    a shell cannot do the second at all. It reuses the command queue for browsing and the
    file verbs, and the backup feature's existing hub-proxied transfer helpers (renamed
    `UploadFileAsync` / `DownloadFileAsync`, since they now have a second caller) for the
    bytes. The one new piece is the spool directory the two hops meet in, pruned by the
    retention thread that already prunes the process and live watches. **The agent half is
    pending a signed release**; until then the tab names the version it needs (3.33.0)
    instead of failing every click on a fleet that has not updated.
14. ~~**#14 Patch management**~~ — done, hub 1.92.0 + agent 3.35.0. The first item off the
    PRD v2.0 triage, and the largest real gap it found. Ordered first among the new items
    because two of the others depend on it: #17 has little to correlate without patch
    outcomes, and the PRD's stability scoring is a query over `patch_run_items` that only
    exists once patch runs do. It had no prerequisite of its own — `packages.py` was already
    the engine, and this is that engine pointed at a vendor catalogue. The one thing that
    could not be reused is the completion model: an install is confirmed by the machine
    coming back and no longer offering the update, which is `firmware.py`'s discipline rather
    than the package scheduler's exit code. **The agent half needs a signed release** before
    any of it reaches the fleet, and the hub must be deployed FIRST — a 3.35.0 agent talking
    to a 1.91.0 hub simply has its patch inventory ignored, which is harmless, but the
    reverse order leaves the console offering a feature no machine answers.
15. **#15 Cross-hub machine sharing** — not from the PRD; asked for directly. Independent of
    everything above, so its position is a priority call rather than a dependency. Its
    prerequisite is #Foundation and #11's device tokens: a peer hub holds a SUBSET of a
    capability set intersected live, which is only meaningful because permission groups
    exist and `apitokens.py` already built exactly that mechanism for devices.
16. **#16 Event log mining** — before #17, because it is one of the two inputs that make
    correlation worth doing. Its own risk is volume rather than mechanism, and that risk is
    settled by filtering on the machine instead of shipping raw logs to a SQLite hub.
17. **#17 Alert correlation and recommended fixes** — after #14 and #16 by dependency, not by
    preference. The deterministic half can be built the day both land; the language layer
    above it is gated on a pluggable AI provider that does not exist yet, and that provider
    is the one piece of new plumbing this item introduces.
18. **#18 Network discovery and shadow IT** — no prerequisite, low expected yield on an
    MDM-driven fleet. Sequenced late for that reason rather than for difficulty.
19. **#19 Endpoint security odds and ends** — three unrelated small items; each can be pulled
    forward on its own if a need appears. BitLocker escrow reuses the `backups.py` secret
    store and is the smallest genuinely useful thing on this list.
20. **#20 Self-healing watchdogs** — last, because it is the one item that needs the rules
    engine to grow a second evaluation site (on the agent), and that is a bigger change to a
    working feature than any of the above make.

> Note: this file tracked hub releases closely up to 1.40.0 and then fell behind — the
> interactive terminal, pluggable OIDC sign-in, searchable dropdowns and the standalone
> TURN installer all landed in 1.41–1.48 without a roadmap entry, since none of them was
> a numbered feature above. #4's entry has been brought up to date. #9 and #10 were the
> first new items added to this board since #7; both are now built (#9 at 1.61.0, #10 at
> 1.63.0). #5 has since grown multi-step packages (hub 1.64.0 + agent 3.22.0), which is an
> extension of a done feature rather than a new board item.
>
> **#14 through #20 came from the PRD v2.0 triage above; #14 is built and the rest are not.**
> That
> section is new as of 2026-08-31 and is the first time this board has grown by more than
> one item at once; read it before the entries, because it records which PRD requirements
> were rejected and why, and several of the loudest ones were. #15 is the exception on that
> block — it is not from the PRD at all.
>
> **Of the items predating the triage, #11 is the one that is not finished.** Its hub half
> is built and tested (1.69.0), its client passes its own tests, and the Windows binary has
> since been built, signed and published as client 1.0.0 — what remains is that it has
> never talked to a hub. Everything else predating the triage is done except the
> opportunistic remainder of #3.
>
> What is otherwise outstanding is not a feature but a release — two of them now: the agent
> halves of #1, #2, #5, #6, #9, #10 and #13 all ship in source and need a signed **agent**
> release before the fleet gains any of them, and #11 needs a signed **client** release
> before there is anything on the Download page.

Each item above has a decided approach; only small, implementation-level parameters
remain (noted per feature) and are meant to be resolved while writing that feature's full
implementation plan — not before.
