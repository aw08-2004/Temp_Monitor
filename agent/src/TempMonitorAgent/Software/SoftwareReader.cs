using Microsoft.Win32;

namespace TempMonitorAgent.Software;

/// <summary>One installed program, in the shape the hub stores (roadmap #25 B).</summary>
/// <param name="Id">Where it was registered: <c>HKLM64:&lt;key&gt;</c>, <c>HKLM32:&lt;key&gt;</c> or
/// <c>HKU:&lt;sid&gt;:&lt;key&gt;</c>. Unique per machine, which is all the hub asks of it -- the
/// same product registered in two views is two installs, and hiding one would hide a
/// licence.</param>
/// <param name="Scope"><c>machine</c> (HKLM, installed for everybody) or <c>user</c> (one
/// profile's hive).</param>
/// <param name="UserSid">The profile's SID for a <c>user</c> entry, else empty. Never a
/// resolved account name: resolving is a domain round trip per profile on the inventory
/// loop.</param>
/// <param name="Arch"><c>x64</c> or <c>x86</c> for an HKLM entry (which registry view it came
/// from), empty for a per-user one.</param>
public sealed record InstalledSoftware(
    string Id,
    string Name,
    string Version,
    string Publisher,
    string InstallDate,
    string InstallLocation,
    string UninstallString,
    string Scope,
    string UserSid,
    string Arch);

/// <summary>What one inventory pass found, and anything it could not read.</summary>
public sealed record SoftwareReport(string Error, IReadOnlyList<InstalledSoftware> Items);

/// <summary>
/// Reads what is installed on this PC from the registry's Uninstall keys (roadmap #25 B) --
/// what Programs and Features shows, and what every inventory product the helpdesk has used
/// actually reads.
///
/// <para><b>Not <c>Win32_Product</c>, deliberately.</b> Enumerating that WMI class makes the
/// Windows Installer validate every MSI on the machine, which triggers self-repairs and
/// rewrites the very machine being inventoried. The Uninstall keys are a read.</para>
///
/// <para><b>Three places, and the third is the one people forget.</b> HKLM in the 64-bit view,
/// HKLM in the 32-bit view (WOW6432Node -- a 32-bit Acrobat on 64-bit Windows registers there,
/// and checking only the native view reports it missing, which is exactly the bug
/// <c>DeployPackageExecutor.FindInstalledVersion</c> documents), and every <b>loaded</b>
/// profile hive under HKEY_USERS. The service runs as LocalSystem, so HKCU is SYSTEM's own hive
/// and says nothing about anybody; per-user installs (Teams classic, Zoom, VS Code's user
/// installer) are only visible through HKU.</para>
///
/// <para><b>Loaded hives only.</b> That was an open parameter in ROADMAP.MD #25 and this is the
/// cheap answer: a signed-in user's hive is already mounted, and loading every
/// <c>NTUSER.DAT</c> on disk means mounting hives under a profile somebody may sign into
/// mid-scan -- the one thing <c>HiveMount</c> exists to do carefully and only when a backup
/// needs it. The cost is that a program installed by a user who is signed out is missing
/// until they sign in again; the hub page says nothing about it either way.</para>
///
/// <para><b>The filter is what Programs and Features applies</b>, in <see cref="FromValues"/>:
/// no DisplayName, <c>SystemComponent = 1</c>, a <c>ParentKeyName</c> (an update to another
/// entry) or an update <c>ReleaseType</c> is not a program anybody would list. Without it a
/// Windows 10 machine reports a few hundred KB entries and runtime fragments.</para>
///
/// <para><b>A read that fails outright throws</b> rather than returning an empty list: the hub
/// stores an empty list as "this PC has nothing installed", which is a real report, and one
/// failed registry open must never be able to send it.</para>
/// </summary>
public static class SoftwareReader
{
    /// <summary>The same ceiling as the hub's <c>software.MAX_ENTRIES</c>, so the two agree
    /// about what "too many" means.</summary>
    public const int MaxEntries = 2000;

    public const string ScopeMachine = "machine";
    public const string ScopeUser = "user";

    private const string UninstallPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";

    /// <summary>Release types that mark an update to something else rather than a program.</summary>
    private static readonly HashSet<string> UpdateReleaseTypes =
        new(StringComparer.OrdinalIgnoreCase) { "Security Update", "Update Rollup", "Hotfix", "Update" };

    public static SoftwareReport Read()
    {
        var items = new List<InstalledSoftware>();
        var errors = new List<string>();
        var machineViewsRead = 0;

        foreach (var (view, arch, prefix) in new[]
                 {
                     (RegistryView.Registry64, "x64", "HKLM64"),
                     (RegistryView.Registry32, "x86", "HKLM32"),
                 })
        {
            try
            {
                using var root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, view);
                using var key = root.OpenSubKey(UninstallPath);
                if (key is null) { machineViewsRead++; continue; }
                ReadKey(key, prefix, ScopeMachine, "", arch, items);
                machineViewsRead++;
            }
            catch (Exception e) when (e is System.Security.SecurityException
                                          or UnauthorizedAccessException or IOException)
            {
                errors.Add($"{prefix}: {e.Message}");
            }
        }

        // Neither machine-wide view could be read: whatever the per-user hives say, a list
        // built from them alone would report a PC with Office on it as having three programs.
        if (machineViewsRead == 0)
            throw new InvalidOperationException(string.Join("; ", errors));

        try
        {
            using var users = RegistryKey.OpenBaseKey(RegistryHive.Users, RegistryView.Default);
            foreach (var sid in users.GetSubKeyNames())
            {
                if (!IsProfileSid(sid)) continue;
                try
                {
                    using var key = users.OpenSubKey($@"{sid}\{UninstallPath}");
                    if (key is not null) ReadKey(key, $"HKU:{sid}", ScopeUser, sid, "", items);
                }
                catch (Exception e) when (e is System.Security.SecurityException
                                              or UnauthorizedAccessException or IOException)
                {
                    errors.Add($"HKU {sid}: {e.Message}");
                }
            }
        }
        catch (Exception e) when (e is System.Security.SecurityException
                                      or UnauthorizedAccessException or IOException)
        {
            errors.Add($"HKU: {e.Message}");
        }

        items.Sort((a, b) => string.Compare(a.Id, b.Id, StringComparison.Ordinal));
        if (items.Count > MaxEntries)
        {
            // Said in `error`, not just enforced: the sort puts every HKLM entry ahead of every
            // per-user one, so a machine over the cap loses user installs first, and a list
            // that silently stopped short would read as complete on the device sheet.
            errors.Add($"truncated: {items.Count} entries, kept {MaxEntries}");
            items.RemoveRange(MaxEntries, items.Count - MaxEntries);
        }
        return new SoftwareReport(string.Join("; ", errors), items);
    }

    /// <summary>A real signed-in person's hive: <c>S-1-5-21-...</c>, and not its
    /// <c>_Classes</c> twin. SYSTEM, LOCAL SERVICE and NETWORK SERVICE (S-1-5-18/19/20) and
    /// <c>.DEFAULT</c> are service accounts nobody installs anything as.</summary>
    public static bool IsProfileSid(string name) =>
        name.StartsWith("S-1-5-21-", StringComparison.OrdinalIgnoreCase)
        && !name.EndsWith("_Classes", StringComparison.OrdinalIgnoreCase);

    private static void ReadKey(RegistryKey uninstall, string prefix, string scope,
                                string sid, string arch, List<InstalledSoftware> into)
    {
        foreach (var name in uninstall.GetSubKeyNames())
        {
            if (into.Count >= MaxEntries * 2) return; // bounded work; trimmed after sorting
            try
            {
                using var sub = uninstall.OpenSubKey(name);
                if (sub is null) continue;
                var entry = FromValues($"{prefix}:{name}", scope, sid, arch, sub.GetValue);
                if (entry is not null) into.Add(entry);
            }
            catch (Exception e) when (e is System.Security.SecurityException
                                          or UnauthorizedAccessException or IOException)
            {
                // One unreadable entry is not a failed inventory.
            }
        }
    }

    /// <summary>
    /// One Uninstall subkey as a program, or null when Programs and Features would not list
    /// it. Pure over a value lookup so a test can drive it with a dictionary -- the build
    /// machine has no registry worth reading, and this filter is where a wrong answer hides.
    /// </summary>
    public static InstalledSoftware? FromValues(string id, string scope, string sid, string arch,
                                                Func<string, object?> get)
    {
        var name = Text(get("DisplayName"));
        if (name.Length == 0) return null;
        if (get("SystemComponent") is int system && system == 1) return null;
        if (Text(get("ParentKeyName")).Length > 0) return null;
        if (UpdateReleaseTypes.Contains(Text(get("ReleaseType")))) return null;

        return new InstalledSoftware(
            Id: id,
            Name: name,
            Version: Text(get("DisplayVersion")),
            Publisher: Text(get("Publisher")),
            InstallDate: Text(get("InstallDate")),
            InstallLocation: Text(get("InstallLocation")),
            UninstallString: Text(get("UninstallString")),
            Scope: scope,
            UserSid: scope == ScopeUser ? sid : "",
            Arch: scope == ScopeMachine ? arch : "");
    }

    /// <summary>A registry value as trimmed text. REG_DWORD versions and dates do turn up, and
    /// a type we did not expect is an empty field rather than an exception.</summary>
    private static string Text(object? value) => value switch
    {
        string s => s.Trim(),
        int or long => value.ToString() ?? "",
        _ => "",
    };
}
