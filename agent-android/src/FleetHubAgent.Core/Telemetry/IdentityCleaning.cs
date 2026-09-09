namespace FleetHubAgent.Telemetry;

/// <summary>
/// Turns the strings a device reports about itself into either a usable value or null.
///
/// **Null is a first-class answer here and the whole point of the file.** The hub merges two
/// machines that report the same serial_number (resolve_serial_group), so a value that many
/// devices share is far worse than no value at all: it does not produce a confusing entry, it
/// silently collapses unrelated devices into one machine. Every rule below exists because some
/// device somewhere reports a constant where a unique value belongs.
///
/// The vendor-placeholder half is the Linux agent's SystemInfo.Clean, unchanged -- the same
/// "To Be Filled By O.E.M." strings turn up in Android's Build fields as they do in DMI. The
/// stable-id half has no counterpart anywhere else in this repo and is documented on
/// <see cref="CleanStableId"/>.
/// </summary>
public static class IdentityCleaning
{
    /// <summary>Drop the placeholder strings vendors ship in identity fields.
    ///
    /// A field left unset by the manufacturer comes back as literal text, and every device
    /// from that vendor reports the SAME one. Passed through, they become a serial number
    /// shared by dozens of devices, which is precisely the input that makes the hub's
    /// duplicate-serial merge collapse unrelated machines. Null is the honest answer.</summary>
    public static string? Clean(string? value)
    {
        var text = (value ?? "").Trim();
        if (text.Length == 0) return null;
        foreach (var placeholder in Placeholders)
            if (text.Equals(placeholder, StringComparison.OrdinalIgnoreCase)) return null;
        // Some vendors fill the field with a single repeated character instead.
        if (text.All(c => c is '0' or '.' or '-')) return null;
        return text;
    }

    private static readonly string[] Placeholders =
    {
        "To Be Filled By O.E.M.", "To be filled by O.E.M.", "Default string", "None",
        "Not Specified", "Not Available", "Unknown", "unknown", "System Serial Number",
        "Chassis Asset Tag", "Asset-1234567890", "N/A", "INVALID", "null",
    };

    /// <summary>
    /// Validate the value the agent will send as serial_number on Android.
    ///
    /// **This is an SSAID, not a hardware serial, and it is the one identity decision on this
    /// platform worth reading in full.** Build.GetSerial() needs READ_PRIVILEGED_PHONE_STATE,
    /// which is signature-level: only an app signed with the platform key, or a system app,
    /// can hold it. A normal build cannot read a hardware serial at any permission level, on
    /// any device, since Android 10. So the field carries Settings.Secure.ANDROID_ID, which is
    /// unique per device, per app-signing-key, per user, and survives reboots and reinstalls
    /// of the same signed APK.
    ///
    /// What that buys is exactly what the hub needs it for: resolve_serial_group() can merge a
    /// renamed device with the machine it used to be. What it does NOT buy, and must not be
    /// assumed to: it is not the number printed on the chassis, it is not what an MDM or an
    /// asset register calls this device, it changes on a factory reset, and it changes if the
    /// APK is ever re-signed with a different key. That last one is a real operational hazard
    /// -- a re-signed build makes every device in the fleet look like a new machine -- and it
    /// is why agent-android/README.md says the signing key is fleet state, not build
    /// configuration.
    ///
    /// The junk values below are why this is a function rather than a null check.
    /// "9774d56d682e549c" is the notorious one: a long-standing Android defect returned it as
    /// the SSAID on a large number of devices, so it is a constant shared across unrelated
    /// hardware, and sending it would merge every affected device in the fleet into one
    /// machine.
    /// </summary>
    public static string? CleanStableId(string? value)
    {
        var text = (value ?? "").Trim();
        if (text.Length == 0) return null;

        // Anything obviously not an id: too short to be unique, or a repeated character.
        if (text.Length < 8) return null;
        if (text.Distinct().Count() == 1) return null;

        foreach (var junk in JunkStableIds)
            if (text.Equals(junk, StringComparison.OrdinalIgnoreCase)) return null;

        return Clean(text);
    }

    /// <summary>Values that are NOT unique to a device despite arriving in a field that is
    /// supposed to be. Each one has been observed shared across unrelated hardware.</summary>
    private static readonly string[] JunkStableIds =
    {
        // The long-standing Android defect: returned as the SSAID by a large number of
        // devices, so it identifies a bug rather than a device.
        "9774d56d682e549c",
        // Emulator and factory images that never generated one.
        "0000000000000000", "ffffffffffffffff", "1234567890abcdef",
        "android_id", "unknown",
    };
}
