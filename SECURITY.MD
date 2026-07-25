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
- **Operator Control:** Remote code execution requires an authorized Google sign-in + allow-list
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
2. **Authentication:** Issuing commands requires valid Google Sign-in + email allow-list
3. **Audit Log:** Every action recorded: who did what, when, on which machine
4. **SYSTEM Context:** Commands run as SYSTEM; access to this capability is intentionally powerful and scoped to trusted admins
5. **Machine Scope:** Operators can be limited to specific machines via Permission Groups
6. **Signed Updates:** Binary replacements verified before execution

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
