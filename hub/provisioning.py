"""The Android Device Owner provisioning payload -- roadmap #23 phase A.

**This module builds one JSON object, and getting it wrong costs a factory reset.**

A fully managed Android device is created by scanning a QR at the setup wizard of a device
that has *already been factory reset*. The wizard reads this payload, downloads the APK from
the URL in it, checks the APK's signature against the checksum in it, installs it, and hands
device ownership to the component named in it. If any of the three disagree with reality the
device cannot finish provisioning -- and the only way out of a half-provisioned setup wizard
is another factory reset. So the failure is never "the QR did not work"; it is "the QR did not
work and everything on that device is gone, twice".

That is why this file validates rather than trusts, and why `build_payload` refuses to produce
a partial payload at all. A QR that is missing a field still SCANS: the wizard accepts it,
starts, and fails several minutes later on a wiped device. Refusing in the console, before
anything is printed, is the only place the mistake is still cheap.

**The hub hosts the APK and derives the checksum itself.** It used to do neither: two fields
were typed in by hand, and producing the second meant running `apksigner verify --print-certs`
and converting the hex digest it printed. That put three manual steps in front of the one action
in this product whose mistakes are only discovered on an already-wiped device, and the middle
one -- a 43-character checksum -- produces a string that looks entirely plausible when it is
wrong. `signing_certificate_checksum` reads the certificate out of the APK's own signing block
instead, so what goes in the QR is derived from the file the device will actually download.

**Device Owner is a one-way door in the other direction too.** It can only be taken before any
account exists on the device, there is no API to grant it afterwards, and there is no supported
way to move it to a different app. Nothing here can start that flow -- the hub renders a code,
a person points a camera at it, and the platform does the rest.

**The component name is a CONSTANT here, not a setting**, and that is the one deliberate
departure from "make it configurable". The agent pins its own admin class name
(FleetDeviceAdminReceiver.ComponentClassName) precisely so that both sides can hold the same
literal: .NET Android would otherwise generate a mangled name whose hash can change between
builds, and a printed QR naming a component that no longer exists is a wipe with no way back.
A setting would let the two drift, and the drift is only discovered on a device that has
already been reset.

**No Wi-Fi credentials, in this phase, on purpose.** The provisioning extras can carry an SSID
and PSK so a freshly reset device joins a network before downloading the APK, and the plan for
this phase said they would. They are left out because the hub has nowhere honest to keep the
PSK: `settings` is rendered into a form and partly shipped to agents, and the only secret store
here is the master-key-wrapped file the BIOS setup password uses. Putting a corporate PSK into
a QR that is displayed on a console screen -- and often printed and taped to a bench -- also
turns a photograph of that screen into a network credential. The setup wizard asks for Wi-Fi
itself, once, in about fifteen seconds. Recorded as an open parameter in ROADMAP.MD, with the
shape it would take if the trade changes.

Kept free of Flask so it can be unit-tested in isolation.
"""
import base64
import binascii
import hashlib
import json
import re
from urllib.parse import urlsplit

import settings

# ================================
# THE PAYLOAD
# ================================
#: This app's id, as declared by agent-android's csproj `ApplicationId`.
PACKAGE_NAME = "net.arkeanos.fleethub.agent"

#: The admin receiver's class name, pinned on the agent side as
#: FleetDeviceAdminReceiver.ComponentClassName. **These two literals must agree character for
#: character**, and there is no test that can check it from here -- the C# side is not
#: importable. What holds them together is that the agent pins the name explicitly rather than
#: letting .NET Android generate one, and that this comment names the other end.
ADMIN_RECEIVER_CLASS = f"{PACKAGE_NAME}.FleetDeviceAdminReceiver"

#: What the QR carries as PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME: the flattened
#: `package/class` form the platform's ComponentName.unflattenFromString expects.
ADMIN_COMPONENT = f"{PACKAGE_NAME}/{ADMIN_RECEIVER_CLASS}"

# ---------------------------------------------------------------- extra keys
# Frozen platform constants (android.app.admin.DevicePolicyManager). Written out rather than
# derived, because they are a wire format read by the setup wizard of a device this hub has
# never spoken to -- there is nothing to import them from, and they do not change.
EXTRA_COMPONENT = "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME"
EXTRA_DOWNLOAD_LOCATION = "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION"
EXTRA_SIGNATURE_CHECKSUM = "android.app.extra.PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM"
EXTRA_LEAVE_SYSTEM_APPS = "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED"
EXTRA_ADMIN_EXTRAS = "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE"

#: Managed-configuration keys the agent already reads (see the Android agent's ManagedConfig
#: and Resources/xml/app_restrictions.xml). Carried inside ADMIN_EXTRAS_BUNDLE so a device is
#: pointed at its hub and enrolled by the same scan that provisions it -- otherwise somebody
#: has to type a URL and a secret into every phone by hand, which is exactly the step a fleet
#: cannot afford to repeat forty times.
BUNDLE_HUB_URL = "hub_url"
BUNDLE_ENROLLMENT_SECRET = "enrollment_secret"

#: A signature checksum is the URL-safe base64 of a SHA-256, so 32 bytes become 43 characters
#: with the padding stripped. Checked by shape AND by decoding, because the common mistake is
#: pasting the hex digest `apksigner` prints instead of converting it -- which is 64 characters
#: of valid base64url alphabet and would sail through a regex alone.
_CHECKSUM_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHA256_BYTES = 32


class ProvisioningIncomplete(ValueError):
    """The payload cannot be built yet, with a sentence saying which field is missing and how
    to produce it.

    Its own type, and a ValueError so `refusals.refuse` answers 400, for the same reason
    wake.WakeRejected has one: this is a configuration state an operator can fix, not a bug,
    and the console has to be able to render it as instructions rather than as a stack trace.
    """


def checksum_from_hex(digest):
    """Turn `apksigner verify --print-certs`'s hex SHA-256 into the QR's base64url form.

    Not used by the payload builder -- it exists because this conversion is the single most
    likely thing for an operator to get wrong. `apksigner` prints
    `Signer #1 certificate SHA-256 digest: b39a16...` in hex; the provisioning extra wants the
    same 32 bytes as URL-safe base64 with the padding stripped. Pasting the hex produces a
    string that looks plausible, scans fine, and fails on a device that has already been wiped.
    So the conversion is offered by the console next to the field rather than left in a runbook.
    """
    text = str(digest or "").strip().replace(":", "").replace(" ", "").lower()
    try:
        raw = binascii.unhexlify(text)
    except (binascii.Error, ValueError) as e:
        raise ProvisioningIncomplete(
            "That is not a hex SHA-256 digest. Copy the value apksigner prints after "
            "'Signer #1 certificate SHA-256 digest:'.") from e
    if len(raw) != _SHA256_BYTES:
        raise ProvisioningIncomplete(
            f"A SHA-256 digest is {_SHA256_BYTES} bytes ({_SHA256_BYTES * 2} hex characters); "
            f"that one is {len(raw)}.")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def validate_checksum(value):
    """Return the checksum, or raise with the reason. See `_CHECKSUM_RE` for why shape alone
    is not enough."""
    text = str(value or "").strip()
    if not text:
        raise ProvisioningIncomplete(
            "No APK signature checksum is set. Run `apksigner verify --print-certs` against "
            "the signed APK and convert the SHA-256 digest it prints to URL-safe base64.")
    if len(text) == _SHA256_BYTES * 2 and re.fullmatch(r"[0-9a-fA-F]+", text):
        # The specific mistake, caught by name. A generic "invalid checksum" here would send
        # somebody looking at their keystore rather than at the two lines of conversion.
        raise ProvisioningIncomplete(
            "That looks like the hex digest apksigner prints. The QR needs the same 32 bytes "
            "as URL-safe base64 without padding -- 43 characters, not 64.")
    if not _CHECKSUM_RE.match(text):
        raise ProvisioningIncomplete(
            "An APK signature checksum is 43 characters of URL-safe base64 (A-Z a-z 0-9 - _), "
            "with no padding.")
    return text


# ================================
# READING THE SIGNING CERTIFICATE OUT OF AN APK
# ================================
# **This is what lets the hub host the APK instead of asking somebody to run apksigner.**
# The checksum the wizard checks is the SHA-256 of the signing CERTIFICATE, not of the APK
# file, so it cannot be taken from the bytes the upload route already hashes. It has to be
# read out of the archive.
#
# **No ASN.1 parsing, and no new dependency.** The certificate is stored in the APK Signing
# Block as a length-prefixed DER blob, and the checksum is a hash of those exact bytes -- so
# the parser walks lengths and never decodes a certificate. That is the whole reason this is
# eighty lines of struct unpacking rather than a cryptography library in a repo that vendors
# nothing.
#
# **Every read is bounds-checked, and no input may reach struct.error.** The lengths come from
# a file an operator uploaded, and a crafted uint64 turns a slice into a multi-gigabyte
# allocation. `_take` is the only way any of this code touches the buffer, and everything it
# refuses becomes a sentence rather than a 500.

#: The end-of-central-directory signature that anchors the whole walk.
_EOCD_MAGIC = b"PK\x05\x06"
#: A ZIP comment can be 65535 bytes, so the EOCD is somewhere in the last 22 + 65535.
_EOCD_SEARCH = 22 + 65535
#: What sits immediately before the central directory when an APK is signed v2 or later.
_BLOCK_MAGIC = b"APK Sig Block 42"
#: APK Signature Scheme v2, v3 and v3.1 block ids. All three carry the certificate in the same
#: shape, which is why one walker reads any of them.
_BLOCK_V2 = 0x7109871A
_BLOCK_V3 = 0xF05368C0
_BLOCK_V31 = 0x1B93AD61
#: Nothing inside a signing block is legitimately larger than this. A length above it is a
#: crafted file, and refusing early is what keeps a slice from becoming an allocation.
_MAX_FIELD = 64 * 1024 * 1024


def _take(buf, offset, length):
    """`buf[offset:offset + length]`, refusing anything that is not actually in the buffer."""
    if length < 0 or length > _MAX_FIELD or offset < 0 or offset + length > len(buf):
        raise ProvisioningIncomplete(
            "This APK's signing block is malformed -- a length in it points outside the file. "
            "Re-export the APK, or check that the upload was not truncated.")
    return buf[offset:offset + length]


def _uint(buf, offset, size):
    """A little-endian unsigned integer of `size` bytes, bounds-checked."""
    raw = _take(buf, offset, size)
    return int.from_bytes(raw, "little")


def _sequence(buf):
    """Yield the uint32 length-prefixed elements of `buf`.

    The one shape the whole signing block is built from: signers, signed data, digests,
    certificates and attributes are all sequences of length-prefixed elements, nested.
    """
    offset = 0
    while offset < len(buf):
        length = _uint(buf, offset, 4)
        yield _take(buf, offset + 4, length)
        offset += 4 + length


def _first_certificate(block):
    """The first signer's first X.509 DER certificate, and how many signers there were.

    Both, because the count is an assertion rather than trivia: the wizard checks a download
    against ONE certificate, so an APK with two signers is one this hub must refuse rather than
    guess about.

    **The walk stops at the certificate list and does not enumerate the rest of the signed
    data.** That is not an optimisation, it is required: a v3 block's signed data continues past
    the certificates with a bare minSdkVersion and maxSdkVersion, which are plain uint32 values
    rather than length-prefixed elements. Reading on treats `maxSdkVersion` (0x7FFFFFFF on every
    real APK) as the length of the next element, and the walk runs off the end of the buffer.
    An unchecked reader would slice past the end, get whatever was left, and carry on -- which
    is how this reads correctly right up until the day it does not.
    """
    signers = list(_sequence(next(_sequence(block), b"")))
    if not signers:
        raise ProvisioningIncomplete(
            "This APK's signing block names no signer. Re-sign it with apksigner.")

    fields = _sequence(next(_sequence(signers[0]), b""))
    next(fields, None)                       # digests, which nothing here needs
    certificates = next(fields, None)
    if not certificates:
        raise ProvisioningIncomplete(
            "This APK's signing block has no certificate list where one is required.")

    certificate = next(_sequence(certificates), b"")
    if not certificate:
        raise ProvisioningIncomplete(
            "This APK's signer carries no certificate.")
    return certificate, len(signers)


def _signing_blocks(data):
    """The APK Signing Block's id-value pairs, as `{id: value}`.

    The walk: the last EOCD record gives the central directory's offset; the signing block ends
    immediately before it, with its own size written both at its start and just before the
    magic. Checking that those two agree is cheap and is what catches a file that has been cut
    and re-joined.
    """
    start = max(0, len(data) - _EOCD_SEARCH)
    eocd = data.rfind(_EOCD_MAGIC, start)
    if eocd < 0:
        raise ProvisioningIncomplete(
            "That file is not a ZIP archive, so it cannot be an APK.")

    cd_offset = _uint(data, eocd + 16, 4)
    if cd_offset == 0xFFFFFFFF:
        # ZIP64 puts the real offset in a separate record. Refused rather than half-supported:
        # an APK is never four gigabytes, so this is a file that is not what it claims to be.
        raise ProvisioningIncomplete(
            "This archive uses ZIP64, which an APK never needs. Check that the file is really "
            "an APK.")
    if cd_offset < 24 + len(_BLOCK_MAGIC) or cd_offset > len(data):
        raise ProvisioningIncomplete(
            "This APK's central directory offset points outside the file.")

    if _take(data, cd_offset - 16, 16) != _BLOCK_MAGIC:
        # The common real cause, named. .NET Android, Gradle and apksigner all produce v2/v3
        # by default, so an APK without a signing block is almost always one signed the old
        # JAR way or not signed at all -- and saying which saves an operator a search.
        raise ProvisioningIncomplete(
            "This APK has no APK Signing Block. It is signed only with a v1 (JAR) signature, "
            "or not signed at all. Re-sign it with apksigner, which produces v2 and v3 "
            "signatures by default.")

    trailing = _uint(data, cd_offset - 24, 8)
    block_start = cd_offset - 8 - trailing
    if block_start < 0 or trailing > _MAX_FIELD:
        raise ProvisioningIncomplete(
            "This APK's signing block claims a size that does not fit in the file.")
    leading = _uint(data, block_start, 8)
    if leading != trailing:
        raise ProvisioningIncomplete(
            "This APK's signing block is truncated -- the size at its start and the size at "
            "its end disagree.")

    pairs = {}
    offset = block_start + 8
    end = cd_offset - 24
    while offset < end:
        length = _uint(data, offset, 8)
        if length < 4 or offset + 8 + length > end + 8:
            raise ProvisioningIncomplete(
                "This APK's signing block contains an entry that runs past its own end.")
        pairs[_uint(data, offset + 8, 4)] = _take(data, offset + 12, length - 4)
        offset += 8 + length
    return pairs


def signing_certificate_checksum(data):
    """The QR's signature checksum, read out of a signed APK.

    Returns the same 43 characters `checksum_from_hex` produces from what
    `apksigner verify --print-certs` prints -- the URL-safe base64 of the SHA-256 of the
    signing certificate.

    **Every v2/v3 block present must agree.** A file whose v2 and v3 blocks name different
    certificates is one where guessing produces 43 valid-looking characters and a device that
    fails provisioning after a factory reset. So is one with two signers. Both are refused with
    a sentence rather than resolved by picking the first.
    """
    pairs = _signing_blocks(data)
    found = None
    for block_id in (_BLOCK_V2, _BLOCK_V3, _BLOCK_V31):
        if block_id not in pairs:
            continue
        certificate, signers = _first_certificate(pairs[block_id])
        if signers != 1:
            raise ProvisioningIncomplete(
                f"This APK has {signers} signers. A provisioning QR carries one certificate "
                f"checksum, so an APK with more than one signer cannot be hosted here.")
        if found is not None and found != certificate:
            raise ProvisioningIncomplete(
                "This APK's v2 and v3 signature blocks name different certificates, so there "
                "is no single checksum a device could be given. Re-sign it with one key.")
        found = certificate

    if found is None:
        raise ProvisioningIncomplete(
            "This APK's signing block carries no v2 or v3 signature. Re-sign it with "
            "apksigner, which produces both by default.")
    return base64.urlsafe_b64encode(hashlib.sha256(found).digest()).decode("ascii").rstrip("=")


def validate_apk_url(value):
    """Return the APK URL, or raise with the reason.

    **HTTPS is required and is not a style preference.** This URL is fetched by the setup
    wizard of a device that has just been factory reset, over whatever network somebody put it
    on, and what comes back is installed as this fleet's device owner. The signature checksum
    is what makes a tampered download fail rather than take over the device, so plain HTTP is
    not fatal -- but it is a downgrade with no upside, and a hub that allowed it would allow it
    forever. A lab exception is not offered here for the same reason: unlike an agent's hub
    URL, this one is scanned once, by a person, on a device that can be reset if they were
    wrong.
    """
    text = str(value or "").strip()
    if not text:
        raise ProvisioningIncomplete(
            "No APK download URL is set. The setup wizard downloads the agent from this URL "
            "before the device has any network configuration of its own, so it must be "
            "reachable from wherever devices are provisioned.")
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc:
        raise ProvisioningIncomplete(
            "The APK download URL must be an https:// URL. A factory-reset device fetches it "
            "over whatever network it is put on, and installs what comes back as this fleet's "
            "device owner.")
    return text


def build_payload(db_path, *, hub_url="", enrollment_secret="", hosted=None):
    """The provisioning QR's JSON object, or raise ProvisioningIncomplete.

    **All or nothing.** A payload missing the checksum still scans, still starts provisioning,
    and still fails minutes later on a device that has already been wiped -- so a partial one
    is worse than none, and there is no "preview with placeholders" mode for that reason.

    `hub_url` and `enrollment_secret` are passed in rather than read here: the first is app.py's
    resolved public address (the same value packages and BIOS images are handed, for the same
    reason -- a Host header is not the hub's address), and the second is the fleet's enrollment
    secret from `.env`, which this module has no business reading from disk. Both ride in the
    admin extras bundle so one scan provisions AND enrolls; omit the secret and the device
    comes up managed, reporting telemetry, and silently accepting no commands, which is the
    exact state the Android agent's setup screen exists to make visible.

    `hosted` is `{"url", "checksum"}` for the APK this hub is serving, built by the web layer
    from apkhost.py. **Passed in rather than read here for the same reason as the other two**:
    this module owns what a payload MEANS and validates every field of it, and it would stop
    being unit-testable in isolation the moment it learned about blob directories. It is
    validated exactly as an operator-typed value would have been, which is what catches the
    misconfiguration this arrangement introduces -- a hub whose own public address is not an
    https:// URL cannot hand a factory-reset device a download location, and the refusal has to
    name that rather than say "no URL is set".
    """
    record = hosted or {}
    if not record.get("url"):
        raise ProvisioningIncomplete(
            "No agent APK has been uploaded to this hub yet. Upload the signed APK above; the "
            "hub derives the signature checksum from it and serves it to devices being "
            "provisioned.")

    checksum = validate_checksum(record.get("checksum"))
    try:
        apk_url = validate_apk_url(record.get("url"))
    except ProvisioningIncomplete as e:
        # The URL was built from this hub's own public address, so a refusal here is never an
        # operator's typing -- it is HUB_URL. Saying so is the difference between a fix and a
        # search.
        raise ProvisioningIncomplete(
            "An APK is hosted on this hub, but the hub's public address is not an https:// "
            "URL, so a factory-reset device cannot be given anywhere to download it from. Set "
            "HUB_URL to the address devices reach this hub on.") from e

    extras = {}
    if str(hub_url or "").strip():
        extras[BUNDLE_HUB_URL] = str(hub_url).strip()
    if str(enrollment_secret or "").strip():
        extras[BUNDLE_ENROLLMENT_SECRET] = str(enrollment_secret).strip()

    payload = {
        EXTRA_COMPONENT: ADMIN_COMPONENT,
        EXTRA_DOWNLOAD_LOCATION: apk_url,
        EXTRA_SIGNATURE_CHECKSUM: checksum,
        # True leaves the device's own apps alone. Provisioning otherwise DISABLES every system
        # app the device owner has not explicitly enabled, which on a phone somebody carries
        # means no camera, no dialer and no settings -- correct for a single-purpose kiosk and
        # wrong for every device this fleet manages. Left as a knob because the kiosk case is
        # real; defaulted to True because the other one is what an operator will be doing.
        EXTRA_LEAVE_SYSTEM_APPS: bool(
            settings.get_bool(db_path, "provisioning.leave_system_apps_enabled")),
    }
    if extras:
        payload[EXTRA_ADMIN_EXTRAS] = extras
    return payload


def payload_json(payload):
    """The exact bytes that go into the QR.

    Compact separators and no ASCII escaping, because every byte counts against the QR's
    capacity and a larger code is a denser one that scans worse on a cheap camera in bad light
    -- which is the situation this is always used in. `sort_keys` so two hubs with the same
    configuration produce the same string, and so a diff of two codes is readable.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def redact(payload):
    """The payload with the enrollment secret replaced, for logs and the audit trail.

    The QR itself has to carry the real secret -- that is the point of it -- but a copy in
    `audit_log` would put the fleet's shared enrollment secret into a table that `view_audit_log`
    can read, which is a much wider audience than `manage_settings`. Same instinct as
    fleet.create_command's params cap and the restore-plan indirection: record that it happened
    and what it was for, never the credential itself.
    """
    copy = dict(payload or {})
    extras = dict(copy.get(EXTRA_ADMIN_EXTRAS) or {})
    if extras.get(BUNDLE_ENROLLMENT_SECRET):
        extras[BUNDLE_ENROLLMENT_SECRET] = "(set)"
        copy[EXTRA_ADMIN_EXTRAS] = extras
    return copy
