using System.Text;

namespace FleetHubAgent;

/// <summary>
/// Derives the name a device reports to the hub as "machine".
///
/// **This file exists because of a failure that has no counterpart on Windows or Linux, and
/// that is silent in the worst direction.** Those agents send a hostname, which an admin has
/// already made unique. Android has no hostname an app can read -- there is no
/// Environment.MachineName worth sending, and Build.MODEL is a MARKETING name shared by every
/// unit of that model. A fleet of thirty identical tablets reporting "Pixel Tablet" would not
/// appear as thirty machines with a confusing name. They would appear as ONE machine, because
/// the hub keys a machine on the name it reports: thirty devices would overwrite each other's
/// temperature, identity and last_seen several times a second, and the console would show one
/// healthy tablet. Nothing anywhere would report an error.
///
/// So the default name is the model plus a short suffix of the device's stable id, and the
/// suffix is not optional -- see <see cref="Derive"/>.
///
/// The hub's own rule (app.py's is_valid_machine_name) is a REJECTION of characters that
/// cannot appear in a hostname, not an allow-list, and it accepts spaces. This file is
/// stricter on purpose: a name is not only stored, it is read aloud on the phone, typed into
/// change tickets and matched by eye against the console, and "Pixel_9_Pro-a1b2c3d4" survives
/// all three where "Pixel 9 Pro (Wi-Fi) -- a1b2c3d4" does not.
/// </summary>
public static class MachineNaming
{
    /// <summary>The hub's own ceiling (app.py MACHINE_NAME_MAX_CHARS). Restated rather than
    /// imported, because the hub is Python -- a name over this is rejected by /api/report with
    /// a 400 and the device would report nothing at all, forever, with no local symptom.</summary>
    public const int MaxChars = 128;

    /// <summary>How much of the stable id goes on the end. Eight hex characters of a value
    /// that is already random gives a collision chance across a fleet of a few hundred devices
    /// far below the chance of any other failure here, and stays short enough to read out over
    /// a phone.</summary>
    private const int SuffixLength = 8;

    /// <summary>
    /// The default name for a device: a sanitised model, a hyphen, and a short suffix of
    /// <paramref name="stableId"/>.
    ///
    /// **A blank or unusable stableId is not silently dropped.** It would leave every device
    /// of one model reporting the same name, which is precisely the collapse described on this
    /// class, so the caller gets "unknown-device" plus nothing, and AndroidSystemInfo logs it
    /// loudly. That name is obviously wrong in the console, which is the point: a name that
    /// looks right and merges two devices is far worse than one that looks broken.
    /// </summary>
    public static string Derive(string? model, string? manufacturer, string? stableId)
    {
        var baseName = Sanitize(model);
        if (baseName.Length == 0) baseName = Sanitize(manufacturer);
        if (baseName.Length == 0) baseName = "android-device";

        var suffix = Suffix(stableId);
        if (suffix.Length == 0) return Clamp(baseName + "-unknown-device");

        return Clamp(baseName + "-" + suffix);
    }

    /// <summary>Reduce a vendor string to something that reads the same on a phone, in a
    /// ticket and in the console: ASCII letters, digits and single hyphens.
    ///
    /// Non-ASCII is dropped rather than transliterated. A Latin-1 fold would turn some
    /// manufacturer names into a different word and leave others untouched, and this string
    /// becomes a machine's identity -- an approximation is worse than a shorter true name.</summary>
    internal static string Sanitize(string? value)
    {
        var text = (value ?? "").Trim();
        if (text.Length == 0) return "";

        var sb = new StringBuilder(text.Length);
        var lastWasHyphen = false;
        foreach (var c in text)
        {
            if (char.IsAsciiLetterOrDigit(c))
            {
                sb.Append(c);
                lastWasHyphen = false;
            }
            else if (!lastWasHyphen && sb.Length > 0)
            {
                // Spaces, underscores, dots and punctuation all collapse to one hyphen.
                sb.Append('-');
                lastWasHyphen = true;
            }
        }
        return sb.ToString().Trim('-');
    }

    /// <summary>The trailing identity fragment: the last <see cref="SuffixLength"/> usable
    /// characters of the stable id.
    ///
    /// The LAST rather than the first, and that is deliberate. Android's SSAID is 16 hex
    /// characters and is random throughout, so either end would do -- but some MDM-provisioned
    /// ids and vendor serials share a long fixed PREFIX across a purchase batch (a model code,
    /// a factory code) and differ only at the end. Taking the tail is right for both shapes;
    /// taking the head is right for one of them and collapses a batch of devices into one
    /// machine for the other.</summary>
    internal static string Suffix(string? stableId)
    {
        var cleaned = new string((stableId ?? "").Where(char.IsAsciiLetterOrDigit).ToArray());
        if (cleaned.Length == 0) return "";
        return (cleaned.Length <= SuffixLength ? cleaned : cleaned[^SuffixLength..]).ToLowerInvariant();
    }

    /// <summary>Is a name an operator typed acceptable to send as "machine"?
    ///
    /// Checked here rather than left to the hub because rename's failure is read hours later:
    /// the hub answers a bad name with a 400 on the NEXT report, by which time the operator
    /// has moved on and the device has silently stopped reporting.</summary>
    public static bool IsValid(string? name)
    {
        var text = (name ?? "").Trim();
        if (text.Length == 0 || text.Length > MaxChars) return false;
        foreach (var c in text)
        {
            // The hub's forbidden set (markup characters and control codes), plus a refusal of
            // anything non-ASCII: the console renders with textContent so this is defence in
            // depth rather than the only layer, but a machine name is also a dictionary key in
            // several places and a look-alike Unicode character makes two machines that read
            // identically and are not.
            if (c < 0x20 || c > 0x7e) return false;
            if (c is '<' or '>' or '"' or '\'' or '&') return false;
        }
        return true;
    }

    private static string Clamp(string name) =>
        name.Length <= MaxChars ? name : name[..MaxChars].TrimEnd('-');
}
