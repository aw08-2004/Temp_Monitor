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
