# Security Policy & RMM Justification

## What This Software Does

FleetHub is an **enterprise-grade Remote Monitoring and Management (RMM) platform** 
for IT administrators. It is designed, documented, and licensed for legitimate 
system administration use cases:

- CPU temperature monitoring
- System telemetry reporting  
- Remote fleet management (restart, rename, package deployment)
- Backup scheduling and recovery
- Remote desktop view/control with user consent
- Audit logging of all administrative actions

## Transparency & Security

- **Source Code:** Fully open-source (GPL-3.0), auditable on GitHub
- **Signed Artifacts:** All agent updates are Ed25519-signed; manifests are verified fail-closed
- **Audit Trail:** Every RMM command is logged with operator email + full params
- **Operator Control:** Remote code execution requires an authorized OAuth/OIDC sign-in, or a
  device token minted by one (see "Device tokens" below)
- **Signed Releases:** Authenticode-signed executables
- **HTTPS-only:** All hub-to-agent communication over TLS

## RMM Legitimate Use

Remote code execution (RCE) is fundamental to RMM — it's how administrators manage fleets. 
Examples:
- Deploy patches across 100 machines
- Run diagnostics on remote hardware
- Execute repair scripts as SYSTEM
- Restart services fleet-wide

This is standard for tools like PDQ Deploy, ConnectWise Manage, Microsoft SCCM, and others.

## Risk Mitigations

1. **Enrollment Secret:** Agents enroll once with a shared secret; only enrolled agents accept commands
2. **Authentication:** Issuing commands requires a valid OAuth/OIDC sign-in (Google, or any
   configured OIDC provider) against the email allow-list or a Permission Group
3. **Audit Log:** Every action recorded: who did what, when, on which machine
4. **SYSTEM Context:** Commands run as SYSTEM; access to this capability is intentionally powerful and scoped to trusted admins
5. **Machine Scope:** Operators can be limited to specific machines via Permission Groups
6. **Signed Updates:** Binary replacements verified before execution
7. **Bounded Invites:** An invite link grants only Permission Groups, never more than
   its creator holds, and is bounded by a seat count, an expiry and optional pinned
   addresses (see "Invite links" below)

## Personal data inventory

Most of what FleetHub stores is about a machine: its temperature, its disks, its BIOS
settings, which updates it is missing. One thing it stores is about a **person**, and it is
inventoried separately here so that nobody has to infer it from a feature list.

| What | From whom | Why | Where | How long | Who can see it |
|---|---|---|---|---|---|
| **Device position** — latitude, longitude, accuracy radius, provider, and when the fix was taken | Managed Android devices, **only when an operator asks**. Never sampled, never periodic, never in the background | Finding a lost or stolen device, and confirming where a company device is before securing it | `machine_locations` in the hub's SQLite database | `data.location_retention_days`, default **30 days**, then deleted by the retention pruner. Deleting a machine erases its history immediately | Reading: `view` **plus** the machine being in the operator's scope. Asking for a new fix: the separate `locate_device` capability |
| **Installed applications** — package name, label, version, and whether each is enabled or suspended | Managed Android devices, on the inventory loop | Writing an app policy at all: blocking an app means naming a package, and nobody types `com.zhiliaoapp.musically` from memory. Also how a partly-applied policy becomes visible | `machine_apps` in the hub's SQLite database, replaced on each report | Held while the device is enrolled, replaced on each report. Deleting a machine erases it immediately | `view` **plus** the machine being in the operator's scope |
| **App usage** — foreground seconds per package, per the device's own local day | Managed Android devices, **only where somebody has granted usage access on the device itself** | Enforcing daily screen-time budgets, and showing an operator why a budget did or did not bite | `machine_usage` in the hub's SQLite database | `data.usage_retention_days`, default **14 days**, then deleted by the retention pruner. Deleting a machine erases it immediately | `view` **plus** the machine being in the operator's scope |

Four properties of that row are deliberate and worth stating plainly:

- **Collection is on demand only.** There is no background location, no periodic sample and no
  travel history. The only history that exists is a history of *when somebody asked*, which is
  a different and much smaller thing.
- **Asking has its own capability.** `locate_device` is not part of `issue_commands`, which
  every other command-issuing feature reuses. Being able to reboot a PC does not imply being
  able to find out where an employee is. The hub refuses a hand-rolled `locate_device` through
  its generic command endpoint, and refuses to save one as a favorite, so the capability cannot
  be routed around.
- **The person holding the device is told.** The agent posts a notification naming the operator
  who asked, every time — including when it has no position to give, so that a failed request
  cannot be used as a quiet way to check whether a phone is switched on.
- **Every request is in the audit trail** with the operator's address, the device, and the time.

The usage row is the most personal thing in this table, and its properties are stated with the
same care:

- **It exists to make a budget enforceable, not to produce a report.** A screen-time budget has
  to be checked against something, and it is checked on the device, every minute, whether or not
  the hub is reachable. What reaches the hub is the same ledger, so that an operator can see why
  an app was suspended at 15:40.
- **There is no fleet-wide view, deliberately.** The app inventory has one, because a policy
  author has to pick a package from somewhere. Usage has none: the question it would answer at
  fleet scale is "who spends the most time on their phone", and this product does not make that
  easy to ask. The only endpoint names one machine and checks scope.
- **The device cannot measure it unless somebody grants it, by hand, on that device.** Usage
  access on Android is an appop; a Device Owner cannot grant it silently and this agent does not
  try. A device where it was never granted reports nothing, enforces blocked hours normally, and
  says so in the console rather than reporting zeroes that would read as "nobody used this".
- **Retention is shorter than location's and is meant to come down.** Fourteen days by default,
  pruned by the DEVICE's own local day rather than by a hub timestamp, so a device in a timezone
  ahead of the hub does not have its current day pruned out from under it.

**FleetHub cannot give your workforce notice on your behalf.** Whether locating a
company-owned device is lawful where you operate, what you must tell the people carrying those
devices, and what basis you rely on are decisions for the deploying organisation. The product
provides the controls — a separate capability, machine scope, short retention, an audit trail
and an on-device disclosure — and no more than that.

**Not collected**, and each absence is a decision rather than a gap: no background or periodic
location, no browsing history, no message or call content, no contacts,
no photographs, no keystrokes, and no microphone or camera access of any kind. Screen sharing
(roadmap #2) exists on Windows and requires a system prompt the person at the machine accepts
for every session.

## What a managed Android device grants, and what it does not

A device provisioned by QR after a factory reset makes this agent its **Device Owner**, which is
the strongest position an app can hold on Android. What that position is actually used for is a
short list, and it is written down here so that a later addition is made on purpose rather than
by reflex.

**Taken:**

- **Blocking user control of the agent** (`setUserControlDisabledPackages`), so the person
  holding the phone cannot force-stop it from Settings. Without this the whole feature set is
  optional from the device's side, and a stopped agent is indistinguishable from a flat battery.
  It stops a *person*; it is not a fix for an OEM power manager, which runs inside the system.
- **Enforcing automatic time** (`setAutoTimeEnabled`). Curfews and budgets are evaluated on the
  device against the device's own clock, which is the only way a phone with no signal at 22:00
  still has a bedtime. That is only defensible if the clock is not a setting somebody can move.
  Nothing *sets* a time, and the time zone stays the device's own.
- **Suspending applications** (`setPackagesSuspended`), which greys the icon and shows the
  system's own "paused by your organisation" message rather than making an app vanish. An
  unconditional never-suspend list on the agent — the launcher, dialer, settings, emergency and
  the agent itself — is enforced on the device, not by the hub, so a compromised or simply newer
  hub cannot brick a phone.
- **Reading the full package list**, which a Device Owner gets without the broad
  `QUERY_ALL_PACKAGES` permission that Play policy flags.

**Deliberately not taken**, each available and each declined: no camera or screenshot
restrictions, no factory-reset block, no certificate installation, no always-on VPN, no keyguard
restrictions, no account-management restrictions, and no background location. Usage access is
not taken either — it cannot be, and the agent does not pretend otherwise.

**The provisioning QR is a fleet credential.** It carries the APK's download URL and signature
checksum and the enrollment secret; anyone who can photograph it can enrol a device into this
fleet. Treat it like the keystore: generate it when a device is being provisioned, do not paste
it into a ticket, and rotate the enrollment secret if one escapes.

## Device tokens (the native client)

The FleetHub desktop/mobile client authenticates with a **long-lived bearer token** rather
than a session cookie, because sign-in is OAuth/OIDC only and there is no password for an
app to hold. This is the largest change to the hub's security perimeter since fleet-command
signing was removed, so the controls around it are stated here rather than left implicit:

- **A token is minted only by a real sign-in.** Pairing follows RFC 8252's native-app flow:
  the app opens the *system* browser, the hub authenticates it through the ordinary OAuth
  path, and an operator explicitly confirms the device on a consent page. The app never
  sees credentials. Nothing is minted until a one-time, sixty-second, single-use code is
  exchanged, so an abandoned pairing leaves no credential behind.
- **Only the token's SHA-256 is stored.** There is no "reveal": the hub does not hold the
  value and cannot show it again.
- **A token carries a SUBSET of its owner's capabilities, intersected live on every
  request.** Removing somebody from a Permission Group disables their paired devices
  immediately — the credential can never outlive the grant.
- **Administrative capabilities cannot be granted to a device at all.** `manage_settings`,
  `manage_users`, `manage_permission_groups` and `manage_firmware` are refused at mint
  time, including for break-glass superusers. Console administration stays in the console.
- **Tokens expire and slide on use** (`hub.device_token_lifetime_days`, 90 by default), so
  a device that stops checking in loses access on its own.
- **Every mint, first use and revoke is audited at security level**, and any device can be
  revoked — by its owner, or fleet-wide by a `manage_users` holder — from the Users page.
- **A token can never be upgraded into a session cookie**, so it cannot be used to escape
  its own capability ceiling.
- **Client downloads are signed.** The console's Download Client page renders only a
  manifest whose Ed25519 signature verifies against the same release key that signs the
  agent, and publishes each build's SHA-256.

## Invite links

An operator can hand somebody a **link** that grants the Permission Groups it names, rather
than adding their address by hand (roadmap #22). A link that confers console access is a
bearer credential in the same sense a device token is, so the same rule applies — the
controls are stated here rather than left implicit:

- **An invite grants Permission Groups, never capabilities of its own.** Redeeming one adds
  the address to those groups, exactly as an admin typing it in would. There is no second
  authorization layer, and therefore no special case for revoking somebody who arrived by
  link: they are an ordinary group member.
- **An invite can never grant break-glass.** `ALLOWED_EMAILS` remains env-only; nothing in
  this feature writes to it.
- **An invite can never grant more than its creator holds**, in capabilities or in machine
  scope, unless its creator is a break-glass superuser. A scoped admin cannot mint themselves
  a link into a fleet-wide group.
- **Seats are bounded and claimed atomically.** `max_uses` is how many distinct people the
  link admits (1 = single-use); the claim is one conditional UPDATE, so two people racing the
  last seat cannot both be admitted. Redemption is idempotent per address.
- **Expiry defaults to 7 days.** "Never" exists and must be chosen deliberately.
- **A link may be pinned to specific addresses**, checked before any seat is claimed.
- **Only the link's SHA-256 is stored.** There is no "reveal": the hub cannot show a link
  again after it is created, and the page says so.
- **Creation, redemption, revocation and deletion are audited at security level** — these
  rows are the answer to "how did this person get access".
- **Creating an invite requires `manage_permission_groups`,** not `manage_users`: an invite
  hands out capabilities, so it is group administration.
- **`GET /invite/<code>` is the hub's only unauthenticated route**, and it has to be — it is
  read by somebody who cannot sign in yet. It renders the invite's label, sender and the
  *names* of the groups it grants, and nothing else: no capabilities, no machine scope, no
  seat count, no other redeemers. Previewing it consumes nothing. The code travels the
  sign-in round trip in the signed session cookie, never as a query parameter, so it does not
  reach referrer headers or proxy logs.

## For Antivirus Teams

This software is **not malware** and is flagged by heuristics that detect:
- Windows Service installation
- Remote code execution capability
- System-level privileges

These are features, not bugs. The above controls + source code review + signing will establish trust.

---

# Vulnerability Reporting

## Reporting a Vulnerability

If you discover a security vulnerability in FleetHub, **please report it responsibly** by emailing wiensfriesena@gmail.com with the following details:

- **Vulnerability title and description**
- **Affected component(s)** (e.g., agent, hub, fleet command channel)
- **Steps to reproduce** (if applicable)
- **Potential impact** (severity, what an attacker could do)
- **Suggested fix** (if you have one)

### Important: Do Not Disclose Publicly

- Do **not** open a public GitHub issue
- Do **not** post details on social media or forums
- Do **not** use pull requests to disclose vulnerabilities
- Do **not** publish exploit code without prior coordination

We will treat your report in confidence and work with you to resolve it before public disclosure.

## What to Expect

1. **Acknowledgment:** We will confirm receipt within **3 business days**
2. **Assessment:** We will investigate and determine severity
3. **Fixing:** Depending on severity we will fix it as soon as possible.
4. **Disclosure:** We will coordinate with you on the timing and wording of a public advisory
5. **Credit:** You will be credited by name (or anonymously, if you prefer) in release notes

## Scope

We take security seriously for:

- **Agent (`agent/`)** — The Windows Service running as SYSTEM
- **Hub API (`app.py`, `fleet.py`, `packages.py`, `backups.py`)** — All fleet command and data access endpoints
- **Authentication & Authorization** — Google sign-in, enrollment secrets, token handling, permission groups
- **Update Mechanism** — Ed25519 signature verification, manifest handling
- **Encryption & Backups** — AES-256-GCM, key derivation, credential storage
- **Remote Control (`turn/`)** — WebRTC, TURN credential minting, session management
- **Audit Logging** — Tampering, omission, or integrity issues

### Out of Scope

- Social engineering or phishing
- Physical security (e.g., stolen credentials on a monitor)
- Issues in dependencies (report to the upstream maintainer instead)
- Missing optional security features (e.g., MFA, which we recommend but don't enforce)
- Issues requiring prolonged user interaction to exploit

If you're unsure whether something is in scope, please report it anyway — we'd rather over-include than miss a real issue.

## Severity Guidelines

| Severity | Definition | Examples |
|----------|-----------|----------|
| **Critical** | Attacker can execute code as SYSTEM fleet-wide without authentication | Unauthenticated RCE in `/api/agent/*`, command verification bypass, manifest signature forgery |
| **High** | Attacker gains unauthorized admin access or exfiltrates sensitive data | Authentication bypass, unauthorized command execution, credential theft, audit log deletion |
| **Medium** | Attacker causes service disruption or accesses scoped/limited data | Denial of service, machine-scoped RCE boundary bypass, cross-user data leaks |
| **Low** | Informational or requires user interaction / existing access | Missing rate limiting, poor error messages, hardcoded non-secrets, typos |

## Security-Conscious Design

This project applies the following security principles:

- **Fail closed:** Unsigned updates, failed enrollments, and verification errors all refuse rather than proceed
- **Least privilege:** Agents run as SYSTEM only when needed (sensor access, remote control); users can scope operator access to specific machines
- **Defense in depth:** Multiple layers — signatures, tokens, audit logs, allow-lists — so no single layer is a single point of failure
- **Transparency:** Audit logs record every fleet command with operator info and full parameters
- **Defense-in-breadth:** Communication over TLS, session cookies pinned `SameSite=Lax`, CSRF protection via `Content-Type` enforcement

## Security Contact

- **Email:** wiensfriesena@gmail.com
- **GPG Key:** (Optional — provide if you have one; see [GitHub docs](https://docs.github.com/en/code-security/repository-security-advisories/publishing-a-repository-security-advisory) for guidance)

## Acknowledgments

We thank the following researchers and contributors for responsibly disclosing vulnerabilities:

(This section will be updated as issues are resolved and disclosed publicly.)

---

## Additional Resources

- [OWASP: Vulnerability Disclosure Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html)
- [GitHub: Security Policy Documentation](https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository)
- [CVE: How to Report](https://www.cvedetails.com/)
