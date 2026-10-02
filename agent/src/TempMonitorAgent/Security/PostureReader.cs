using System.Management;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32;

namespace TempMonitorAgent.Security;

/// <summary>One product registered with Windows Security Center (roadmap #25 D).</summary>
/// <param name="State">The raw <c>productState</c> bit field, kept beside the decoded flags
/// so a value the decoding gets wrong can be read off the hub later instead of re-collected.</param>
public sealed record AntivirusProduct(string Name, uint State, bool Enabled, bool UpToDate);

/// <summary>What Security Center says is protecting this PC.</summary>
/// <param name="Supported">False when <c>root\SecurityCenter2</c> does not exist, which is
/// every Windows Server: the hub then judges antivirus from <see cref="DefenderArea"/> alone.</param>
public sealed record AntivirusArea(bool Supported, IReadOnlyList<AntivirusProduct> Products,
                                   string Error);

/// <summary>Microsoft Defender's own status, which exists on Server where Security Center
/// does not.</summary>
public sealed record DefenderArea(bool Present, bool? AntivirusEnabled, bool? RealTime,
                                  int? SignatureAgeDays, long? SignatureUpdated, string Mode,
                                  string Error);

public sealed record FirewallProfile(string Name, bool? Enabled);

public sealed record FirewallArea(IReadOnlyList<FirewallProfile> Profiles, string Error);

/// <summary>The two AutoRun policy values, raw. Null means "not configured", which is a
/// finding in its own right -- Windows' default leaves AutoPlay on.</summary>
public sealed record AutorunArea(int? NoDriveTypeAutoRun, int? NoAutorun, string Error);

/// <summary>One signed-in user's effective screen-saver lock: policy where a GPO set it, the
/// user's own preference otherwise.</summary>
public sealed record UserLockPolicy(string Sid, bool? Active, bool? Secure, int? TimeoutSeconds);

public sealed record SessionLockArea(int? MachineInactivitySeconds,
                                     IReadOnlyList<UserLockPolicy> Users, string Error);

public sealed record LocalAccount(string Name, string Sid, bool Enabled);

/// <summary>One member of the local Administrators group.</summary>
/// <param name="Kind">`user`, `group`, `deleted` or `unknown`, from the SID_NAME_USE the
/// group reports. A deleted account still holding admin rights is a SID nobody can name.</param>
/// <param name="Local">True for an account that lives on this PC rather than in a domain.</param>
public sealed record AdminMember(string Name, string Sid, string Kind, bool Local);

public sealed record AccountsArea(IReadOnlyList<LocalAccount> Local,
                                  IReadOnlyList<AdminMember> Administrators, string Error);

public sealed record SecureBootArea(string State, string Error);

public sealed record TpmArea(bool? Present, bool? Enabled, bool? Activated, string SpecVersion,
                             string Error);

/// <summary>Everything <see cref="PostureReader.Read"/> collects, one area per field.</summary>
public sealed record PostureReport(
    AntivirusArea Antivirus,
    DefenderArea Defender,
    FirewallArea Firewall,
    AutorunArea Autorun,
    SessionLockArea SessionLock,
    AccountsArea Accounts,
    SecureBootArea SecureBoot,
    TpmArea Tpm);

/// <summary>
/// Reads the endpoint settings the CIS Controls IG1 safeguards ask about (roadmap #25 D):
/// antivirus and its signatures (10.1, 10.2), AutoRun (10.3), the firewall (4.4, 4.5),
/// session locking (4.3), default and administrator accounts (4.7, 5.4), plus Secure Boot and
/// the TPM, which the device sheet has promised since #25 A.
///
/// **The agent reports facts; the hub decides what passes.** Nothing here compares a value
/// against a threshold. A signature age of nine days is sent as nine, not as "stale", because
/// what counts as stale is an operator setting on the hub, and a threshold baked into the
/// agent would take a fleet release to change. The one exception is decoding Security
/// Center's <c>productState</c> bit field, which is Microsoft's encoding rather than anybody's
/// policy -- and the raw value travels beside it.
///
/// **Every area fails on its own.** Security Center is absent on Server, Defender is absent
/// where a third-party product removed it, the TPM provider wants a packet-privacy connection
/// the way BitLocker's does, and a domain-joined PC can take seconds to enumerate a group that
/// names domain accounts. One of those failing must never cost the report the others, so each
/// area carries its own `error` and the hub marks only that check unknown.
///
/// **Read-only, deliberately.** Turning a firewall on or AutoRun off is a script an operator
/// runs through Rules or Packages, on purpose; a reporter that also enforced would change a
/// machine on an hourly timer nobody approved.
///
/// Rejected: <c>MSFT_NetFirewallProfile</c> over WMI for the firewall. It reads the persistent
/// store, which is not what is in force when a GPO sets the profile -- a domain PC whose local
/// setting says "on" and whose policy says "off" would read as protected. <c>HNetCfg.FwPolicy2</c>
/// answers with the effective state. Also rejected: <c>Win32_GroupUser</c> for the
/// Administrators group, which walks every group-user association on the machine and, on a
/// domain member, can resolve the whole domain.
/// </summary>
public static class PostureReader
{
    public const string SecureBootOn = "on";
    public const string SecureBootOff = "off";
    public const string SecureBootUnsupported = "unsupported";
    public const string SecureBootUnknown = "unknown";

    public const string KindUser = "user";
    public const string KindGroup = "group";
    public const string KindDeleted = "deleted";
    public const string KindUnknown = "unknown";

    /// <summary>A bound on what one PC can send, matching the hub's own caps
    /// (hub/posture.py). A machine with more than this many local accounts or admin members
    /// is not a workstation anybody should be reading a posture of row by row.</summary>
    public const int MaxEntries = 200;

    /// <summary>Signed-in profiles read for screen-lock policy. A terminal server can have
    /// dozens; the hub judges the machine by the worst of them, and the first 32 are plenty
    /// to find it.</summary>
    public const int MaxUsers = 32;

    /// <summary>NET_FW_PROFILE_TYPE2: the three profiles a Windows firewall has.</summary>
    private static readonly (string Name, int Code)[] FirewallProfiles =
        [("domain", 1), ("private", 2), ("public", 4)];

    /// <summary>Read this machine's posture. Never throws.</summary>
    public static PostureReport Read() => new(
        Area(ReadAntivirus, e => new AntivirusArea(true, [], e)),
        Area(ReadDefender, e => new DefenderArea(false, null, null, null, null, "", e)),
        Area(ReadFirewall, e => new FirewallArea([], e)),
        Area(ReadAutorun, e => new AutorunArea(null, null, e)),
        Area(ReadSessionLock, e => new SessionLockArea(null, [], e)),
        Area(ReadAccounts, e => new AccountsArea([], [], e)),
        Area(ReadSecureBoot, e => new SecureBootArea(SecureBootUnknown, e)),
        Area(ReadTpm, e => new TpmArea(null, null, null, "", e)));

    /// <summary>Decode Security Center's <c>productState</c>.
    ///
    /// Undocumented by Microsoft and stable since Windows 7: bits 12-15 are the scanner
    /// state (1 on, 0 off, 2 snoozed, 3 expired) and bits 4-7 the signature state (0 up to
    /// date). Snoozed and expired both read as not enabled here, which is the safe direction
    /// -- a product somebody paused is not protecting the machine.</summary>
    public static (bool Enabled, bool UpToDate) DecodeProductState(uint state) =>
        (((state >> 12) & 0xF) == 1, ((state >> 4) & 0xF) == 0);

    /// <summary>The kind of an Administrators member, from its SID_NAME_USE.</summary>
    public static string KindFromSidUsage(int sidUsage) => sidUsage switch
    {
        1 => KindUser,
        // Group, alias and well-known group: all things that grant admin to everybody in them.
        2 or 4 or 5 => KindGroup,
        6 => KindDeleted,
        _ => KindUnknown,
    };

    /// <summary>A screen-saver registry value (REG_SZ "1", "900") as a number, or null.</summary>
    public static int? ParseRegistryInt(object? value) => value switch
    {
        int i => i,
        long l when l is >= int.MinValue and <= int.MaxValue => (int)l,
        string s when int.TryParse(s.Trim(), out var parsed) => parsed,
        _ => null,
    };

    // ------------------------------------------------------------------ the areas

    private static AntivirusArea ReadAntivirus()
    {
        var scope = new ManagementScope(@"root\SecurityCenter2");
        try
        {
            scope.Connect();
        }
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidNamespace)
        {
            // Windows Server. Not an error: Defender's own status answers instead.
            return new AntivirusArea(false, [], "");
        }

        var products = new List<AntivirusProduct>();
        using var searcher = new ManagementObjectSearcher(
            scope, new ObjectQuery("SELECT displayName, productState FROM AntiVirusProduct"));
        using var collection = searcher.Get();
        foreach (ManagementBaseObject item in collection)
        {
            using (item)
            {
                var name = AsString(item, "displayName");
                var state = AsUInt(item, "productState");
                if (name.Length == 0 || state is null) continue;
                var (enabled, upToDate) = DecodeProductState(state.Value);
                products.Add(new AntivirusProduct(name, state.Value, enabled, upToDate));
                if (products.Count >= MaxEntries) break;
            }
        }
        return new AntivirusArea(true, products, "");
    }

    private static DefenderArea ReadDefender()
    {
        var scope = new ManagementScope(@"root\Microsoft\Windows\Defender");
        try
        {
            scope.Connect();
        }
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidNamespace)
        {
            return new DefenderArea(false, null, null, null, null, "", "");
        }

        using var searcher = new ManagementObjectSearcher(
            scope, new ObjectQuery("SELECT * FROM MSFT_MpComputerStatus"));
        using var collection = searcher.Get();
        foreach (ManagementBaseObject item in collection)
        {
            using (item)
            {
                long? updated = null;
                var stamp = AsString(item, "AntivirusSignatureLastUpdated");
                if (stamp.Length > 0)
                {
                    try
                    {
                        updated = new DateTimeOffset(
                            ManagementDateTimeConverter.ToDateTime(stamp)).ToUnixTimeSeconds();
                    }
                    catch (ArgumentOutOfRangeException) { }
                }
                var age = AsUInt(item, "AntivirusSignatureAge");
                return new DefenderArea(
                    true,
                    AsBool(item, "AntivirusEnabled"),
                    AsBool(item, "RealTimeProtectionEnabled"),
                    // 65535 is what Defender reports when it has never had signatures at all.
                    age is null or >= 65535 ? null : (int)age.Value,
                    updated,
                    // "Normal", "Passive Mode", "EDR Block Mode". Passive is what Defender
                    // drops to when a third-party product registers, and is why a stale
                    // Defender signature on such a PC is not a finding.
                    AsString(item, "AMRunningMode"),
                    "");
            }
        }
        return new DefenderArea(false, null, null, null, null, "", "");
    }

    private static FirewallArea ReadFirewall()
    {
        var type = Type.GetTypeFromProgID("HNetCfg.FwPolicy2");
        if (type is null)
            return new FirewallArea([], "The firewall policy interface is not registered.");

        object? policy = null;
        try
        {
            policy = Activator.CreateInstance(type);
            if (policy is null)
                return new FirewallArea([], "The firewall policy interface did not start.");
            var profiles = new List<FirewallProfile>();
            foreach (var (name, code) in FirewallProfiles)
            {
                bool? enabled = null;
                try
                {
                    // An indexed property: FirewallEnabled(NET_FW_PROFILE_TYPE2).
                    var value = policy.GetType().InvokeMember(
                        "FirewallEnabled", BindingFlags.GetProperty, null, policy, [code]);
                    enabled = value is null ? null : Convert.ToBoolean(value);
                }
                catch (Exception)
                {
                    // One profile the service will not describe is unknown, not off: "off" is
                    // a finding somebody acts on.
                }
                profiles.Add(new FirewallProfile(name, enabled));
            }
            return new FirewallArea(profiles, "");
        }
        finally
        {
            if (policy is not null && Marshal.IsComObject(policy))
                Marshal.FinalReleaseComObject(policy);
        }
    }

    private static AutorunArea ReadAutorun()
    {
        using var root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
        using var key = root.OpenSubKey(
            @"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer");
        if (key is null) return new AutorunArea(null, null, "");
        return new AutorunArea(ParseRegistryInt(key.GetValue("NoDriveTypeAutoRun")),
                               ParseRegistryInt(key.GetValue("NoAutorun")), "");
    }

    private static SessionLockArea ReadSessionLock()
    {
        int? machine = null;
        using (var root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,
                                                  RegistryView.Registry64))
        using (var key = root.OpenSubKey(
                   @"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"))
        {
            machine = ParseRegistryInt(key?.GetValue("InactivityTimeoutSecs"));
        }

        // Signed-in users only, the SoftwareReader rule: their hives are the ones loaded under
        // HKU. A signed-out user has no session to lock.
        var users = new List<UserLockPolicy>();
        using var hives = RegistryKey.OpenBaseKey(RegistryHive.Users, RegistryView.Default);
        foreach (var sid in hives.GetSubKeyNames())
        {
            if (!sid.StartsWith("S-1-5-21-", StringComparison.OrdinalIgnoreCase)
                && !sid.StartsWith("S-1-12-1-", StringComparison.OrdinalIgnoreCase)) continue;
            if (sid.EndsWith("_Classes", StringComparison.OrdinalIgnoreCase)) continue;
            try
            {
                using var policy = hives.OpenSubKey(
                    $@"{sid}\Software\Policies\Microsoft\Windows\Control Panel\Desktop");
                using var own = hives.OpenSubKey($@"{sid}\Control Panel\Desktop");
                users.Add(new UserLockPolicy(
                    sid,
                    Flag(Effective(policy, own, "ScreenSaveActive")),
                    Flag(Effective(policy, own, "ScreenSaverIsSecure")),
                    ParseRegistryInt(Effective(policy, own, "ScreenSaveTimeOut"))));
            }
            catch (Exception e) when (e is System.Security.SecurityException
                                           or UnauthorizedAccessException or IOException)
            {
                // A hive being unloaded under us is a user signing out mid-scan.
            }
            if (users.Count >= MaxUsers) break;
        }
        return new SessionLockArea(machine, users, "");

        // A value set by policy wins over the user's own; a policy key that exists without
        // this particular value does not hide the preference beneath it.
        static object? Effective(RegistryKey? policy, RegistryKey? own, string name) =>
            policy?.GetValue(name) ?? own?.GetValue(name);

        static bool? Flag(object? value) => ParseRegistryInt(value) is { } n ? n != 0 : null;
    }

    private static AccountsArea ReadAccounts()
    {
        var errors = new List<string>();
        var local = new List<LocalAccount>();
        try
        {
            // LocalAccount = TRUE is what keeps this from enumerating the domain.
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name, SID, Disabled FROM Win32_UserAccount WHERE LocalAccount = TRUE");
            using var collection = searcher.Get();
            foreach (ManagementBaseObject item in collection)
            {
                using (item)
                {
                    var sid = AsString(item, "SID");
                    if (sid.Length == 0) continue;
                    local.Add(new LocalAccount(AsString(item, "Name"), sid,
                                               AsBool(item, "Disabled") != true));
                    if (local.Count >= MaxEntries) break;
                }
            }
        }
        catch (Exception e)
        {
            errors.Add(Describe(e));
        }

        var admins = new List<AdminMember>();
        try
        {
            admins.AddRange(AdministratorsMembers());
        }
        catch (Exception e)
        {
            errors.Add(Describe(e));
        }
        return new AccountsArea(local, admins, string.Join("; ", errors));
    }

    private static SecureBootArea ReadSecureBoot()
    {
        using var root = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
        using var key = root.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\SecureBoot\State");
        // No key at all is a legacy-BIOS boot: there is no Secure Boot to be on.
        if (key is null) return new SecureBootArea(SecureBootUnsupported, "");
        return ParseRegistryInt(key.GetValue("UEFISecureBootEnabled")) switch
        {
            1 => new SecureBootArea(SecureBootOn, ""),
            0 => new SecureBootArea(SecureBootOff, ""),
            _ => new SecureBootArea(SecureBootUnknown, ""),
        };
    }

    private static TpmArea ReadTpm()
    {
        var options = new ConnectionOptions
        {
            // The TPM provider refuses an unencrypted scope, exactly as BitLocker's does --
            // see BitLockerReader. Without this the TPM reads as absent on every machine.
            Authentication = AuthenticationLevel.PacketPrivacy,
            Impersonation = ImpersonationLevel.Impersonate,
            EnablePrivileges = true,
        };
        var scope = new ManagementScope(@"root\CIMV2\Security\MicrosoftTpm", options);
        try
        {
            scope.Connect();
        }
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidNamespace)
        {
            return new TpmArea(false, null, null, "", "");
        }

        using var searcher = new ManagementObjectSearcher(
            scope, new ObjectQuery("SELECT * FROM Win32_Tpm"));
        using var collection = searcher.Get();
        foreach (ManagementBaseObject item in collection)
        {
            using (item)
            {
                // "2.0, 0, 1.59": the first field is the spec family, which is all a
                // Windows 11 readiness question needs.
                var spec = AsString(item, "SpecVersion").Split(',')[0].Trim();
                return new TpmArea(true, AsBool(item, "IsEnabled_InitialValue"),
                                   AsBool(item, "IsActivated_InitialValue"), spec, "");
            }
        }
        // The provider exists and names no TPM: the chip is absent or switched off in firmware.
        return new TpmArea(false, null, null, "", "");
    }

    // ------------------------------------------------------------------ Administrators

    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetLocalGroupGetMembers(
        string? serverName, string localGroupName, int level, out IntPtr buffer,
        int preferredMaxLength, out int entriesRead, out int totalEntries, IntPtr resumeHandle);

    [DllImport("netapi32.dll")]
    private static extern int NetApiBufferFree(IntPtr buffer);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct LocalGroupMembersInfo2
    {
        public IntPtr Sid;
        public int SidUsage;
        public string DomainAndName;
    }

    /// <summary>The members of the local Administrators group.
    ///
    /// **The group is found by SID, never by name.** It is "Administratoren" on a German
    /// install and "Administradores" on a Spanish one, and this helpdesk runs all three -- a
    /// lookup by the English name fails on two PCs in three.</summary>
    private static IEnumerable<AdminMember> AdministratorsMembers()
    {
        var account = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null)
            .Translate(typeof(NTAccount)).Value;
        var groupName = account[(account.LastIndexOf('\\') + 1)..];

        var status = NetLocalGroupGetMembers(null, groupName, 2, out var buffer, -1,
                                             out var read, out _, IntPtr.Zero);
        try
        {
            if (status != 0)
                throw new InvalidOperationException(
                    $"NetLocalGroupGetMembers returned {status}.");
            var members = new List<AdminMember>();
            var size = Marshal.SizeOf<LocalGroupMembersInfo2>();
            var machinePrefix = Environment.MachineName + "\\";
            for (var i = 0; i < read && members.Count < MaxEntries; i++)
            {
                var entry = Marshal.PtrToStructure<LocalGroupMembersInfo2>(buffer + i * size);
                var sid = entry.Sid == IntPtr.Zero ? "" : new SecurityIdentifier(entry.Sid).Value;
                var name = entry.DomainAndName ?? "";
                members.Add(new AdminMember(
                    name, sid, KindFromSidUsage(entry.SidUsage),
                    name.StartsWith(machinePrefix, StringComparison.OrdinalIgnoreCase)));
            }
            return members;
        }
        finally
        {
            if (buffer != IntPtr.Zero) NetApiBufferFree(buffer);
        }
    }

    // ------------------------------------------------------------------ helpers

    /// <summary>Run one area's read, turning any failure into that area's error.
    ///
    /// Deliberately broad, at the same layer and for the same reason as BitLockerReader: this
    /// runs on the inventory loop, and nothing one provider throws is worth the others.</summary>
    private static T Area<T>(Func<T> read, Func<string, T> failed)
    {
        try { return read(); }
        catch (Exception e) { return failed(Describe(e)); }
    }

    private static string AsString(ManagementBaseObject item, string name)
    {
        try { return (item[name]?.ToString() ?? "").Trim(); }
        catch (ManagementException) { return ""; }
    }

    private static uint? AsUInt(ManagementBaseObject item, string name)
    {
        try { return item[name] is null ? null : Convert.ToUInt32(item[name]); }
        catch (Exception e) when (e is ManagementException or InvalidCastException
                                       or FormatException or OverflowException)
        {
            return null;
        }
    }

    private static bool? AsBool(ManagementBaseObject item, string name)
    {
        try { return item[name] is null ? null : Convert.ToBoolean(item[name]); }
        catch (Exception e) when (e is ManagementException or InvalidCastException
                                       or FormatException)
        {
            return null;
        }
    }

    private static string Describe(Exception e)
    {
        var text = $"{e.GetType().Name}: {e.Message}".Trim();
        return text.Length > 300 ? text[..300] : text;
    }
}
