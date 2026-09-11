# FleetHub Security Audit Report

**Date:** 2026-09-11  
**Auditor:** Agent Zero (Automated Security Assessment)  
**Scope:** Full hub/ web application, agent API, frontend JavaScript  
**Methodology:** Static code analysis, pattern matching, manual code review  

---

## Executive Summary

The FleetHub codebase demonstrates **mature security practices** in many areas — hardened session cookies, a documented CSRF model, parameterized SQL, constant-time token comparisons, no `shell=True`, and a custom sandboxed expression parser instead of `eval()`. The development team clearly understands security principles.

However, several vulnerabilities and weaknesses were identified, ranging from **Medium** to **Low** severity. No Critical or High-severity remotely exploitable vulnerabilities were found. The most significant finding is the unauthenticated `/api/report` endpoint which allows telemetry spoofing and database pollution.

### Summary of Findings

| # | Severity | Title |
|---|----------|-------|
| 1 | Medium | Unauthenticated Telemetry Spoofing via /api/report |
| 2 | Medium | CSRF Protection Relies on Content-Type Check (No Token) |
| 3 | Low | XSS-Safe innerHTML Pattern in setStatusPill |
| 4 | Low | Machine Name Validation Uses Blocklist Instead of Allowlist |
| 5 | Low | Backup URL Validation Allows HTTP to Loopback (SSRF Surface) |
| 6 | Low | Unauthenticated Provisioning APK Download |
| 7 | Info | No Rate Limiting on Enrollment or API Endpoints |
| 8 | Info | Five Duplicate _bearer_agent Implementations |

---

## Detailed Findings

### Finding 1: Unauthenticated Telemetry Spoofing via /api/report

**Severity:** Medium  
**File:** `hub/app.py`, line 4332  
**CWE:** CWE-306 (Missing Authentication for Critical Function)

**Description:**  
The `/api/report` endpoint accepts POST requests from any source without authentication. Any network-accessible client can submit fake temperature readings, spoofed machine identities, and fabricated hardware information. The endpoint stores machine names, asset tags, serial numbers, OS information, and other metadata directly into the database.

While the codebase documents this as intentional ("unauthenticated by design" for agent telemetry reporting), the endpoint accepts arbitrary machine names and stores all submitted metadata. An attacker who can reach the hub can:
- Inject fake machines into the fleet inventory
- Spoof telemetry data for existing machines
- Pollute the audit trail with false machine information
- Potentially influence alert rules and dashboard metrics

**Proof of Concept:**  
```bash
curl -X POST https://hub.example.com/api/report \
  -H "Content-Type: application/json" \
  -d '{"machine":"FAKE-PC","temp":45.0,"serial_number":"SPOOFED","asset_tag":"STOLEN"}'
```

**Recommendation:**  
- Add a shared secret or API key for telemetry reporting (similar to `AGENT_ENROLLMENT_SECRET`)
- Alternatively, rate-limit the endpoint and validate that the machine name matches an enrolled agent
- At minimum, require that machine names match an existing enrolled machine before storing metadata

---

### Finding 2: CSRF Protection Relies on Content-Type Check (No Token)

**Severity:** Medium  
**File:** `hub/app.py`, lines 1955–2060  
**CWE:** CWE-352 (Cross-Site Request Forgery)

**Description:**  
The application's CSRF protection is based on requiring `Content-Type: application/json` for POST requests, combined with `SameSite=Lax` cookies. The reasoning is that cross-origin HTML forms cannot produce this content type, and cross-origin fetch requests preflight and fail.

While this is a valid defense, it is **fragile** and depends on multiple assumptions:
1. No endpoint ever adds `force=True` to `request.get_json()`
2. No endpoint accepts form-encoded fallback
3. No permissive CORS headers are ever added
4. Browser SameSite behavior remains consistent
5. The multipart upload exemptions (4 endpoints) are correctly scoped

The codebase explicitly acknowledges this fragility in comments. A single future change adding `force=True` or a form-encoded fallback to any state-changing endpoint would silently undermine the entire CSRF protection.

**Proof of Concept:**  
If any endpoint were modified to use `request.get_json(force=True)` or accept form data, a cross-site POST could ride the operator's session cookie:
```html
<form action="https://hub.example.com/api/fleet/commands" method="POST">
  <input name="type" value="shell">
  <input name="machine" value="VICTIM-PC">
</form>
```

**Recommendation:**  
- Implement a proper CSRF token mechanism (e.g., double-submit cookie or synchronizer token pattern)
- The content-type check can remain as defense-in-depth, but should not be the sole control
- Consider using Flask-WTF or a similar library for CSRF token management

---

### Finding 3: XSS-Safe innerHTML Pattern in setStatusPill

**Severity:** Low  
**File:** `hub/static/js/common.js`, line 25  
**CWE:** CWE-79 (Cross-Site Scripting)

**Description:**  
The `setStatusPill()` function writes to `innerHTML`:
```javascript
el.innerHTML = `<span class="status-pill__dot"></span>${label}`;
```

The `label` parameter currently comes from the `t()` translation function, which returns catalog strings — not user input. However, this pattern is fragile: any future caller that passes a user-controlled value through `label` would create an XSS vulnerability. The codebase generally uses `textContent` everywhere else, making this an exception.

The code comments acknowledge this risk ("Literal keys, never an interpolated one") but the function itself does not enforce it.

**Recommendation:**  
- Refactor `setStatusPill` to use DOM manipulation instead of `innerHTML`:
```javascript
function setStatusPill(el, state, label) {
    if (!el) return;
    el.classList.remove('status-pill--ok', 'status-pill--warn', 'status-pill--danger', 'status-pill--muted');
    el.classList.add(`status-pill--${state}`);
    el.textContent = '';
    const dot = document.createElement('span');
    dot.className = 'status-pill__dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(label));
}
```

---

### Finding 4: Machine Name Validation Uses Blocklist Instead of Allowlist

**Severity:** Low  
**File:** `hub/app.py`, lines 3040–3047  
**CWE:** CWE-184 (Incomplete List of Disallowed Inputs)

**Description:**  
Machine name validation rejects characters `[<>"'&--]` but allows everything else, including backticks, semicolons, parentheses, and other potentially dangerous characters. The code explicitly comments that this is a defense-in-depth layer, not the primary XSS control.

```python
_MACHINE_NAME_FORBIDDEN = re.compile(r'[<>"\'&--]')

def is_valid_machine_name(machine):
    name = str(machine or "").strip()
    if not name or len(name) > MACHINE_NAME_MAX_CHARS:
        return False
    return _MACHINE_NAME_FORBIDDEN.search(name) is None
```

Machine names flow from the unauthenticated `/api/report` endpoint into every console view, database storage, audit logs, and the Socket.IO live feed. While the frontend uses `textContent` (not `innerHTML`) for rendering, a blocklist approach may miss novel XSS payloads or characters that interfere with other systems.

**Proof of Concept:**  
The following machine name passes validation but contains potentially problematic characters:
```
machine="test`(document.location='https://evil.com')"
```

**Recommendation:**  
- Use an allowlist of valid hostname characters (letters, digits, hyphens) instead of a blocklist
- Valid hostnames per RFC 1123: `[a-zA-Z0-9-]` with a maximum of 63 characters per label
- The blocklist can remain as a secondary check, but the allowlist should be the primary control

---

### Finding 5: Backup URL Validation Allows HTTP to Loopback (SSRF Surface)

**Severity:** Low  
**File:** `hub/backups.py`, lines 1270–1280  
**CWE:** CWE-918 (Server-Side Request Forgery)

**Description:**  
The backup destination URL validation allows plain HTTP to `localhost`, `127.0.0.1`, and `::1`. While this is documented as intentional for local MinIO testing, it creates an SSRF surface if an attacker gains access to the backup configuration interface.

```python
if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
    raise ValueError(f"{field} must use https://")
```

An operator with `manage_backups` could configure a backup destination pointing to `http://localhost:<port>`, potentially reaching internal services that are not exposed externally. The credentials sent to this destination would travel in cleartext.

**Recommendation:**  
- Restrict HTTP-to-loopback to a configurable allowlist of ports (e.g., MinIO's default 9000)
- Or require an explicit "development mode" flag to allow HTTP to loopback
- Document the risk clearly in the backup configuration UI

---

### Finding 6: Unauthenticated Provisioning APK Download

**Severity:** Low (Informational)  
**File:** `hub/provisioning_web.py`, line 198  
**CWE:** CWE-306 (Missing Authentication)

**Description:**  
The `/provisioning/apk/<token>/fleethub-agent.apk` endpoint has no authentication. This is explicitly documented as intentional — the caller is a factory-reset device with no credentials. The route uses a random token (`secrets.token_urlsafe(32)`) looked up in the database, not joined to a path.

The codebase correctly identifies this as "anti-enumeration and a kill switch, not a security boundary." The APK is a signed release binary with no fleet data.

**Risk:**  
- If the token is leaked (e.g., from a photographed QR code), the APK can be downloaded by anyone
- The APK itself contains no secrets, so the impact is limited to distributing the agent binary

**Recommendation:**  
- No action required — the design is sound for the stated use case
- Consider adding rate limiting to prevent token brute-force attempts (though 43-char tokens make this infeasible)

---

### Finding 7: No Rate Limiting on Enrollment or API Endpoints

**Severity:** Informational  
**Files:** Multiple (`hub/fleet_web.py`, `hub/app.py`)

**Description:**  
The enrollment endpoint (`/api/agent/enroll`) uses `hmac.compare_digest` for constant-time comparison of the enrollment secret, which prevents timing attacks. However, there is no rate limiting on:
- Enrollment attempts (brute-force of `AGENT_ENROLLMENT_SECRET`)
- Login/OAuth callback endpoints
- API token exchange endpoint
- The unauthenticated `/api/report` endpoint

While the enrollment secret is a strong shared secret, unlimited attempts allow sustained brute-force attacks.

**Recommendation:**  
- Add rate limiting (e.g., Flask-Limiter) to sensitive endpoints
- Consider exponential backoff for failed enrollment attempts
- Rate-limit `/api/report` to prevent telemetry flooding

---

### Finding 8: Five Duplicate _bearer_agent Implementations

**Severity:** Informational  
**Files:** `hub/fleet_web.py`, `hub/remote_web.py`, `hub/bios_web.py`, `hub/backups_web.py`, `hub/files_web.py`  
**CWE:** CWE-1041 (Use of Redundant Code)

**Description:**  
The `_bearer_agent()` function is copy-pasted across five modules. While each copy is identical, this creates a maintenance risk: a fix applied to one copy may not be applied to all, potentially leaving some endpoints with a weaker auth check.

**Recommendation:**  
- Extract `_bearer_agent` into a shared module (e.g., `auth_helpers.py`)
- Import from the shared module in all five files

---

## Areas of Strong Security Practice

The following practices were observed and should be maintained:

1. **Session Cookie Hardening** — `SameSite=Lax`, `Secure` (derived from HUB_URL), `HttpOnly`, rolling expiration
2. **Constant-Time Token Comparison** — `hmac.compare_digest` used for enrollment secret and agent token validation
3. **No shell=True** — All `subprocess.run` calls use list arguments, preventing command injection
4. **Parameterized SQL** — The vast majority of SQL queries use `?` placeholders. F-string SQL is limited to `ALTER TABLE` with hardcoded column names
5. **Custom Expression Parser** — The rules engine uses a hand-written tokenizer/recursive descent parser instead of `eval()` or `ast.literal_eval()`
6. **Path Validation** — `validate_path` in `files.py` rejects relative paths and `..` components; `spool_path` re-checks that spool names are bare filenames
7. **Token Hashing** — Agent tokens and API tokens are stored as hashes, never plaintext
8. **Invite System** — Invite codes are hashed at rest, have seat limits, expiry, creator ceiling, and cannot grant break-glass access
9. **API Token Security** — Device tokens hold a subset of capabilities, intersected live with owner permissions; administrative capabilities are refused at mint time
10. **Frontend XSS Defense** — Nearly all DOM manipulation uses `textContent`/`createElement`, never `innerHTML` from data
11. **Secret Management** — Secrets never travel from `.env` into the settings table; the auth config editor never reads secrets back out
12. **Audit Trail** — Security-relevant actions (key reveal, provisioning, enrollment revocation) are audited at `LEVEL_SECURITY`

---

## Remediation Priority

1. **Immediate:** Add authentication or a shared secret to `/api/report` (Finding 1)
2. **Short-term:** Implement CSRF tokens as a primary control, keeping content-type checks as defense-in-depth (Finding 2)
3. **Short-term:** Tighten machine name validation to an allowlist (Finding 4)
4. **Medium-term:** Refactor `setStatusPill` to avoid `innerHTML` (Finding 3)
5. **Medium-term:** Add rate limiting to sensitive endpoints (Finding 7)
6. **Low priority:** Consolidate `_bearer_agent` implementations (Finding 8)
7. **Low priority:** Restrict backup HTTP-to-loopback (Finding 5)

---

## Remediation Applied (2026-09-11)

The following fixes were implemented based on the findings above:

| # | Finding | Fix Applied | Files Changed |
|---|---------|-------------|---------------|
| 1 | Unauthenticated /api/report | Added optional `AGENT_REPORT_SECRET` env var; when set, reports must include matching `report_secret` field. Uses `hmac.compare_digest` for constant-time comparison. Backwards-compatible (endpoint remains open when secret is unset). | `hub/app.py` |
| 3 | innerHTML in setStatusPill | Refactored to use DOM manipulation (`createElement` + `textContent`) instead of `innerHTML`. | `hub/static/js/common.js` |
| 4 | Machine name blocklist | Added allowlist (`^[a-zA-Z0-9 ._-]+$`) as primary control, kept original blocklist as defense-in-depth. Blocks backticks, semicolons, `$()` and other non-hostname characters. | `hub/app.py` |
| 5 | Backup URL loopback SSRF | Restricted HTTP-to-loopback to MinIO ports (9000, 9001) only. Other ports on loopback now require HTTPS. | `hub/backups.py` |
| 7 | No rate limiting | Created lightweight in-memory rate limiter (`rate_limit.py`). Applied `120/minute` to `/api/report`, `10/minute` to `/api/agent/enroll`. No external dependencies. | `hub/rate_limit.py`, `hub/app.py`, `hub/fleet_web.py` |
| 8 | Duplicate _bearer_agent | Extracted to shared `hub/auth_helpers.py`. All 5 blueprint files now import from the shared module. | `hub/auth_helpers.py`, `hub/fleet_web.py`, `hub/remote_web.py`, `hub/bios_web.py`, `hub/backups_web.py`, `hub/files_web.py` |

### Not Fixed (by design or out of scope)

| # | Finding | Reason |
|---|---------|--------|
| 2 | CSRF Content-Type model | This is a deliberate architectural decision documented extensively in the codebase. Implementing CSRF tokens would require touching every state-changing endpoint and the frontend. Recommend as a separate project. |
| 6 | Unauthenticated APK download | Correctly identified in the audit as "design is sound for the stated use case." No action needed. |

---

*End of Report*
