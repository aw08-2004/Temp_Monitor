namespace TempMonitorAgent.Bios;

/// <summary>
/// What a firmware read concluded. Three outcomes, and the last two are deliberately
/// separate: "this machine has no manageable BIOS" (a VM, a whitebox) is a permanent,
/// correct answer, while "we found an interface and it failed" is a fault someone should
/// look at. Collapsing them shows every VM in the fleet a red error forever.
/// </summary>
public enum BiosSupport
{
    Supported,
    Unsupported,
    Error,
}

/// <summary>How a setting's value may be written. Sent to the hub so the console can render
/// the right control later without re-deriving it from the value's text.</summary>
public enum BiosSettingKind
{
    Enum,
    String,
    Integer,
    Unknown,
}

/// <summary>
/// One firmware setting, under the machine's OWN name for it.
///
/// No cross-vendor alias layer in v1 (see ROADMAP #9): a curated map from `wake_on_lan` to
/// each vendor's attribute is the design that silently writes the wrong attribute on the
/// third vendor, where the mapping was read out of a PDF rather than tested on hardware.
/// <see cref="DisplayName"/> is the vendor's own friendlier label where one exists -- shown
/// beside the real name, never instead of it, because the real name is the identity a write
/// will target.
/// </summary>
public sealed record BiosSetting(
    string Name,
    string Value,
    BiosSettingKind Kind,
    IReadOnlyList<string> PossibleValues,
    bool ReadOnly,
    string DisplayName = "");

/// <summary>The whole of what one machine reports about its firmware.</summary>
/// <param name="PasswordSet">Null when the vendor gives us no way to ask. Not "false":
/// "there is no password" and "we could not find out" lead to different advice the moment
/// someone tries to change a setting.</param>
public sealed record BiosReport(
    BiosSupport Support,
    string Vendor = "",
    string Interface = "",
    string BiosVersion = "",
    bool? PasswordSet = null,
    string Error = "",
    IReadOnlyList<BiosSetting>? Settings = null)
{
    public IReadOnlyList<BiosSetting> Items => Settings ?? Array.Empty<BiosSetting>();

    public static BiosReport Unsupported(string reason, string biosVersion = "") =>
        new(BiosSupport.Unsupported, Error: reason, BiosVersion: biosVersion);

    public static BiosReport Failed(string vendor, string iface, string error,
                                    string biosVersion = "") =>
        new(BiosSupport.Error, Vendor: vendor, Interface: iface, Error: error,
            BiosVersion: biosVersion);
}

/// <summary>
/// Thrown by the WMI adapter when the vendor namespace itself does not exist -- which is the
/// one failure that means "unsupported" rather than "broken". Every other WMI error is a
/// genuine fault: the namespace is there, so something on this machine is meant to answer.
/// </summary>
public sealed class BiosInterfaceMissingException(string message) : Exception(message);
