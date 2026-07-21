# FleetHub — Feature Roadmap

Status: ✅ done · 🚧 partially done · 📋 planned (spec below)

---

## ✅ 6. History page

Done. Per-machine multi-panel dashboard (Komodo-style): CPU load, memory (with total RAM
shown and used/total GB on hover), disk usage, network in/out, GPU temp/load, temperature.
Backed by typed `readings` columns, a multi-metric history endpoint, and per-metric
collection toggles in Settings.

## 🚧 3. Settings: which sensors to read, data retention, etc.

Mostly done:
- `computer.primary_sensor_preference` — ordered CPU-temp sensor preference (+ per-machine
  override).
- `metrics.collect_cpu_load` / `collect_memory` / `collect_gpu` / `collect_disk` /
  `collect_network` — per-metric collection on/off toggles.
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

---

## 📋 1. Backups via HTTPS   ·   build next — two subjects

**1a. Hub database** — scheduled, compressed, offsite.
**1b. Per-PC file backups** — configured user folders (e.g. Desktop, Documents), with
defaults in Settings and per-PC extra paths in a new machine-page **Backup tab**.

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

**Still to decide during implementation**: offsite retention/rotation policy (how many
generations to keep); resumable-upload chunking specifics; exact VSS invocation and its
failure fallback (skip-and-report if a snapshot can't be created).

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
3. **#1 Backups** — hub DB first, then per-PC files  ← next
4. **#4 Active Directory** — Entra login + group mapping first, then LDAP sync
5. **#2 Remote view & control** — spike first, then build

Each item above has a decided approach; only small, implementation-level parameters
remain (noted per feature) and are meant to be resolved while writing that feature's full
implementation plan — not before.
