namespace FleetHubAgent.Patch;

/// <summary>Where an available update came from. Mirrors hub/patches.py SOURCE_KINDS -- the
/// strings are the wire contract, so they are spelled here once and never inlined. A source
/// the hub does not know is dropped by parse_report, silently and per update.</summary>
public static class PatchSources
{
    public const string Apt = "apt";
    public const string Dnf = "dnf";
}

/// <summary>How the hub classifies an update. Mirrors hub/patches.py CLASSIFICATIONS, and only
/// the three a Linux package manager can honestly produce are named here.
///
/// <para><b>`Unknown` is a real answer, not a parse failure.</b> dnf without an updateinfo
/// index genuinely cannot say whether an update is a security fix, and saying so is better
/// than filing it under `Other` as though something had decided. The hub's auto-approval only
/// ever offers `security` and `critical`, so the difference between `Other` and `Unknown` is
/// never the difference between installed and not -- it is what an operator reads.</para>
/// </summary>
public static class PatchClassifications
{
    public const string Security = "security";
    public const string Other = "other";
    public const string Unknown = "unknown";
}

/// <summary>
/// One update this machine is currently offered.
///
/// <para><b>Uid is the PACKAGE NAME, and that is a deliberate difference from Windows.</b>
/// There, an approval is a statement about a KB -- "KB5060842 is fine" -- and the version is
/// implied by the identity. A Linux package has no such identity: `openssl` is upgraded a
/// dozen times a year and each one is the same package. Keying on name+version would mean an
/// approval that expires every Tuesday and a `machine_patches` row that never disappears in
/// the way hub/patches.py's confirm_from_inventory depends on; keying on the name gives the
/// hub exactly the event it reads -- "this machine stopped being offered openssl" -- and an
/// approval that means what an operator meant. The version is carried in the TITLE, where it
/// is visible and is not an identity.</para>
///
/// <para><b>The name is not prefixed with its source.</b> `apt`'s openssl and `dnf`'s openssl
/// are the same software and roll up into one row of the fleet view, which is the answer an
/// operator wants from "who is missing an openssl fix". A collision with a winget id cannot
/// happen: those are `Publisher.Package` and always contain a dot.</para>
/// </summary>
public sealed record AvailableUpdate(
    string Uid,
    string Source,
    string Title,
    string Classification,
    bool RebootRequired);

/// <summary>What one scan found, plus why it found nothing when that is the answer.
///
/// <para><b>`Error` is reported to the hub rather than swallowed</b>, and the empty list is
/// still sent beside it. "This machine has no updates" and "this machine could not be asked"
/// are different facts that look identical in a count, and only one of them is a reason to
/// walk over to a box.</para>
/// </summary>
public readonly record struct PatchScan(IReadOnlyList<AvailableUpdate> Updates, string? Error)
{
    public static PatchScan Failed(string error) =>
        new(Array.Empty<AvailableUpdate>(), error);
}
