"""provisioning.py -- the Android device-owner QR payload (roadmap #23 phase A).

**The silent failure this file exists to catch is a QR that scans.**

Every other kind of mistake in this repo announces itself: a bad command fails, a bad setting
refuses to save, a broken page shows an error. A provisioning payload with the wrong checksum,
a missing field or a component name nothing carries does none of that. It encodes cleanly, it
renders as a perfectly good QR code, the setup wizard reads it happily -- and the device it is
scanned at has ALREADY been factory reset, so the failure arrives several minutes later with
everything on that device gone and no way forward but another wipe.

So the assertions here are about refusing early, and about the three specific ways an operator
gets this wrong:

  * **Pasting apksigner's hex digest** where the base64url form belongs. Sixty-four characters
    of valid base64url alphabet, so a shape check alone waves it through. It has its own error
    message naming the mistake, because a generic "invalid checksum" sends somebody to look at
    their keystore instead of at two lines of conversion.
  * **A half-configured hub.** Refusing with the reason beats building a payload without the
    field, because the payload without the field still scans.
  * **The enrollment secret reaching the audit trail.** The QR must carry it; a copy in
    `audit_log` would put the fleet's shared secret in front of everyone holding
    `view_audit_log`, which is a far wider audience than `manage_settings`.

The `ADMIN_COMPONENT` assertion is the one thing here that cannot be checked against the other
side: the agent pins the same literal in C#, which is not importable from Python. The test
holds the value so that changing one half without the other is at least a failing test rather
than a wipe.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import provisioning
import settings

PASS = 0
FAIL = 0

#: The signing certificate of the fleet's real Android keystore, as `apksigner verify
#: --print-certs` prints it, and its base64url form. A real pair rather than an invented one:
#: the conversion is the thing being tested, and a made-up digest would still pass a conversion
#: that was subtly wrong about padding or the URL-safe alphabet.
REAL_HEX = "b39a161053eadc4090c52b5c5ae7d5cebd68d86e709e012043547b351285f468"
REAL_B64 = "s5oWEFPq3ECQxStcWufVzr1o2G5wngEgQ1R7NRKF9Gg"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        settings.init_settings_db(db_path)
        settings.invalidate()

        print("== The component name both sides must agree on ==")
        check("the package is the one the csproj declares",
              provisioning.PACKAGE_NAME == "net.arkeanos.fleethub.agent")
        check("the admin component is package/class, flattened",
              provisioning.ADMIN_COMPONENT
              == "net.arkeanos.fleethub.agent/net.arkeanos.fleethub.agent."
                 "FleetDeviceAdminReceiver")
        check("...and the class half is the literal the agent pins",
              provisioning.ADMIN_RECEIVER_CLASS.endswith(".FleetDeviceAdminReceiver"))

        print("\n== Converting apksigner's digest ==")
        check("a real hex digest converts to the QR's base64url form",
              provisioning.checksum_from_hex(REAL_HEX) == REAL_B64)
        check("...and the result is 43 characters, unpadded",
              len(REAL_B64) == 43 and "=" not in REAL_B64)
        check("uppercase hex is accepted", provisioning.checksum_from_hex(REAL_HEX.upper())
              == REAL_B64)
        check("...and so is the colon-separated form some tools print",
              provisioning.checksum_from_hex(":".join(
                  REAL_HEX[i:i + 2] for i in range(0, len(REAL_HEX), 2))) == REAL_B64)
        for bad, why in ((REAL_HEX[:-2], "too short"), ("zz" + REAL_HEX[2:], "not hex"),
                         ("", "empty"), (None, "missing")):
            try:
                provisioning.checksum_from_hex(bad)
                check(f"a digest that is {why} is refused", False)
            except provisioning.ProvisioningIncomplete:
                check(f"a digest that is {why} is refused", True)

        print("\n== Validating what an operator typed ==")
        check("the real checksum validates", provisioning.validate_checksum(REAL_B64) == REAL_B64)
        try:
            provisioning.validate_checksum(REAL_HEX)
            check("the hex digest pasted into the checksum field is refused", False)
        except provisioning.ProvisioningIncomplete as e:
            check("the hex digest pasted into the checksum field is refused", True)
            # The whole point of that branch: 64 characters of hex ARE valid base64url
            # characters, so a shape check alone waves it through. Naming the mistake is what
            # stops somebody going to look at their keystore.
            check("...and the message names the mistake rather than saying 'invalid'",
                  "hex digest" in str(e) and "43" in str(e))
        for bad in ("", "   ", "short", REAL_B64 + "=", REAL_B64[:-1] + "+"):
            try:
                provisioning.validate_checksum(bad)
                check(f"a checksum of {bad!r} is refused", False)
            except provisioning.ProvisioningIncomplete:
                check(f"a checksum of {bad!r} is refused", True)

        check("an https APK url validates",
              provisioning.validate_apk_url("https://fleet.example.com/agent.apk")
              == "https://fleet.example.com/agent.apk")
        for bad, why in (("http://fleet.example.com/agent.apk", "plain http"),
                         ("fleet.example.com/agent.apk", "no scheme"),
                         ("https://", "no host"), ("", "empty")):
            try:
                provisioning.validate_apk_url(bad)
                check(f"an APK url that is {why} is refused", False)
            except provisioning.ProvisioningIncomplete:
                check(f"an APK url that is {why} is refused", True)

        print("\n== A half-configured hub refuses, and does not half-build ==")
        # This is the assertion the whole module exists for. A payload missing the checksum
        # encodes, scans, starts provisioning and fails on a wiped device.
        try:
            provisioning.build_payload(db_path)
            check("a hub with nothing configured refuses to build a payload", False)
        except provisioning.ProvisioningIncomplete:
            check("a hub with nothing configured refuses to build a payload", True)

        settings.set_many(db_path, {"provisioning.signature_checksum": REAL_B64},
                          updated_by="test@x.com")
        settings.invalidate()
        try:
            provisioning.build_payload(db_path)
            check("...and still refuses with only the checksum set", False)
        except provisioning.ProvisioningIncomplete as e:
            check("...and still refuses with only the checksum set", True)
            check("...naming the field that is missing, not the one that is set",
                  "APK download URL" in str(e))

        settings.set_many(db_path,
                          {"provisioning.apk_url":
                           "https://fleet.example.com/fleethub-agent.apk"},
                          updated_by="test@x.com")
        settings.invalidate()

        print("\n== The payload ==")
        payload = provisioning.build_payload(
            db_path, hub_url="https://fleet.example.com", enrollment_secret="s3cr3t")
        check("it names the admin component",
              payload[provisioning.EXTRA_COMPONENT] == provisioning.ADMIN_COMPONENT)
        check("it carries the download location",
              payload[provisioning.EXTRA_DOWNLOAD_LOCATION]
              == "https://fleet.example.com/fleethub-agent.apk")
        check("it carries the SIGNATURE checksum, not a package checksum",
              payload[provisioning.EXTRA_SIGNATURE_CHECKSUM] == REAL_B64
              and provisioning.EXTRA_SIGNATURE_CHECKSUM.endswith("SIGNATURE_CHECKSUM"))
        check("system apps are left enabled by default",
              payload[provisioning.EXTRA_LEAVE_SYSTEM_APPS] is True)
        extras = payload[provisioning.EXTRA_ADMIN_EXTRAS]
        check("the hub url rides in the admin extras bundle",
              extras[provisioning.BUNDLE_HUB_URL] == "https://fleet.example.com")
        check("...and so does the enrollment secret, so ONE scan also enrols",
              extras[provisioning.BUNDLE_ENROLLMENT_SECRET] == "s3cr3t")
        check("every extra key is a real android.app.extra name",
              all(k.startswith("android.app.extra.PROVISIONING_") for k in payload))

        # A hub with no enrollment secret set is a real state (app.py fails closed and says
        # so), and it must still produce a usable provisioning code -- the device comes up
        # managed and unenrolled, which the agent's own setup screen makes visible.
        bare = provisioning.build_payload(db_path, hub_url="https://fleet.example.com")
        check("a hub with no enrollment secret still builds a payload",
              provisioning.BUNDLE_ENROLLMENT_SECRET
              not in (bare.get(provisioning.EXTRA_ADMIN_EXTRAS) or {}))
        empty = provisioning.build_payload(db_path)
        check("...and one with nothing to pass sends no extras bundle at all",
              provisioning.EXTRA_ADMIN_EXTRAS not in empty)

        settings.set_many(db_path, {"provisioning.leave_system_apps_enabled": False},
                          updated_by="test@x.com")
        settings.invalidate()
        check("the kiosk setting reaches the payload",
              provisioning.build_payload(db_path)[provisioning.EXTRA_LEAVE_SYSTEM_APPS]
              is False)
        settings.set_many(db_path, {"provisioning.leave_system_apps_enabled": True},
                          updated_by="test@x.com")
        settings.invalidate()

        print("\n== The encoded string ==")
        text = provisioning.payload_json(payload)
        check("it parses back to exactly what was built", json.loads(text) == payload)
        check("it is compact, because every byte is a denser QR",
              ", " not in text and '": ' not in text)
        check("its key order is stable, so two hubs produce the same code",
              provisioning.payload_json(dict(reversed(list(payload.items())))) == text)
        # A real payload is ~565 bytes, which the encoder fits into roughly a version-18 code
        # at error-correction level M. The specification's own ceiling is far higher (2331
        # bytes at version 40), but that is not the limit worth testing against: a code that
        # dense stops being readable from a screen by a phone camera at an angle, which is the
        # only situation this is ever used in. 900 is the point where provisioning.js's
        # rendering starts producing modules too small to scan reliably.
        check("a real payload stays inside a comfortably scannable code", len(text) < 900)

        print("\n== Redaction ==")
        safe = provisioning.redact(payload)
        check("the enrollment secret is not in the redacted copy",
              "s3cr3t" not in json.dumps(safe))
        check("...but the fact that one was set survives",
              safe[provisioning.EXTRA_ADMIN_EXTRAS][provisioning.BUNDLE_ENROLLMENT_SECRET]
              == "(set)")
        check("the hub url is NOT redacted -- it is not a secret and it is the field an "
              "operator checks",
              safe[provisioning.EXTRA_ADMIN_EXTRAS][provisioning.BUNDLE_HUB_URL]
              == "https://fleet.example.com")
        check("redacting does not mutate the original",
              payload[provisioning.EXTRA_ADMIN_EXTRAS][
                  provisioning.BUNDLE_ENROLLMENT_SECRET] == "s3cr3t")
        check("redacting a payload with no extras is survivable",
              provisioning.redact(empty) == empty
              and provisioning.redact(None) == {})

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        settings.invalidate()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
