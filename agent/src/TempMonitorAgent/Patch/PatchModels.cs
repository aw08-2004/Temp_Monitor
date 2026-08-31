namespace TempMonitorAgent.Patch;

/// <summary>Where an available update came from. Mirrors hub/patches.py SOURCE_KINDS —
/// the strings are the wire contract, so they are spelled here once and never inlined.</summary>
public static class PatchSources
{
    public const string WindowsUpdate = "windows_update";
    public const string Winget = "winget";
}

/// <summary>One update this machine is currently offered.
///
/// <para><b>Uid is the identity the hub approves against</b>, and it is deliberately not the
/// Windows UpdateID GUID on its own. An approval is a statement about a patch — "KB5060842 is
/// fine to install" — and the same KB carries a different UpdateID on different Windows
/// builds, so keying on the GUID would mean approving the same patch once per SKU. Where a KB
/// exists it is therefore the identity; where one does not (most drivers), the GUID stands in
/// because nothing better exists. See <c>hub/patches.py normalize_uid</c>, which lowercases
/// whatever arrives here.</para>
///
/// <para><b>NativeId is kept alongside</b> because installing needs the GUID even when
/// approving used the KB, and re-deriving one from the other means a second search.</para>
/// </summary>
public sealed record AvailableUpdate(
    string Uid,
    string NativeId,
    string Source,
    string Kb,
    string Title,
    string Classification,
    bool RebootRequired,
    long SizeBytes);
