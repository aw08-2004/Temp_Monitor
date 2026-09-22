using System.Management;
using System.Runtime.InteropServices;

namespace TempMonitorAgent.Security;

/// <summary>One key protector on a volume, in the shape the hub stores (roadmap #19).</summary>
/// <param name="Id">The <c>VolumeKeyProtectorID</c> GUID string. This is what an escrowed
/// password is filed under on the hub, so it is the one field with no tolerable fallback.</param>
/// <param name="Kind">`recovery_password` or `other` -- see <see cref="BitLockerReader"/> for
/// why the hub is told only that much.</param>
/// <param name="Label">The protector's friendly name, when it has one. Display only.</param>
public sealed record KeyProtectorInfo(string Id, string Kind, string Label);

/// <summary>One encryptable volume's state (roadmap #19).</summary>
public sealed record VolumeInfo(
    string Mount,
    string DeviceId,
    string Protection,
    string Conversion,
    int? Percentage,
    string Method,
    IReadOnlyList<KeyProtectorInfo> Protectors);

/// <summary>This machine's encryption posture, or why there is none.</summary>
public sealed record BitLockerReport(string Support, string Error,
                                     IReadOnlyList<VolumeInfo> Volumes);

/// <summary>Raised when this machine has no BitLocker provider at all -- Home edition, or a
/// Windows install without the feature. Not an error; see the class docstring.</summary>
public sealed class BitLockerUnsupportedException(string message) : Exception(message);

/// <summary>
/// Reads this machine's BitLocker state, and its recovery passwords when the hub asks
/// (roadmap #19).
///
/// **Two entry points, and the split is the security design.** <see cref="Read"/> collects
/// posture -- which volumes exist, whether they are protected, and the IDs of their key
/// protectors -- and touches no secret at all; it runs on the inventory loop and rides the
/// heartbeat. <see cref="ReadRecoveryPasswords"/> pulls actual 48-digit passwords, and runs
/// only for the specific protector IDs the hub has said it is missing. A machine that is
/// already escrowed therefore never reads a recovery password off its own disk, let alone
/// sends one.
///
/// **`Win32_EncryptableVolume` needs a packet-privacy connection.** The BitLocker provider
/// refuses a scope that is not encrypted, which is a refusal that looks exactly like "the
/// namespace is not there" if you do not know to expect it -- so the authentication level is
/// set explicitly on the scope rather than left at the default. That one line is the
/// difference between reading a fleet's encryption state and reporting every machine in it as
/// unsupported.
///
/// **Unsupported is a first-class outcome**, following <c>BiosReader</c> exactly. Windows Home
/// has no provider and never will; that machine reports the fact once and then goes quiet,
/// rather than showing a red error forever for hardware that was never going to answer.
///
/// **Per-volume failures are soft.** A locked volume answers <c>FVE_E_LOCKED_VOLUME</c> to
/// nearly every call -- and a locked volume is precisely the machine somebody is escrowing
/// keys for -- so one volume that cannot be read must never cost the report the volumes that
/// can. The unreadable one is reported with what was legible and `protection: unknown`.
///
/// **The kind of a protector is flattened to two values.** The hub is told
/// `recovery_password` or `other`, not Microsoft's eleven-value enum, because the only
/// decision it makes with the answer is whether there is a transferable secret to ask for.
/// Shipping the full enum would invite the hub to grow opinions about TPM-and-PIN that belong
/// on the machine, which is the same argument that keeps vendor dispatch out of the hub in #9.
/// </summary>
public static class BitLockerReader
{
    /// <summary>The provider namespace. Present on Pro/Enterprise/Education; absent on Home,
    /// which is what <see cref="BitLockerUnsupportedException"/> reports.</summary>
    public const string NamespacePath = @"root\CIMV2\Security\MicrosoftVolumeEncryption";

    public const string SupportSupported = "supported";
    public const string SupportUnsupported = "unsupported";
    public const string SupportError = "error";

    public const string ProtectionOn = "on";
    public const string ProtectionOff = "off";
    public const string ProtectionUnknown = "unknown";

    public const string KindRecoveryPassword = "recovery_password";
    public const string KindOther = "other";

    /// <summary>`GetKeyProtectors`' filter value for a numerical password (the 48-digit
    /// recovery key). 3 is the only one of the eleven protector types this agent ever asks for
    /// a secret from.</summary>
    private const uint ProtectorTypeNumericalPassword = 3;

    /// <summary>Read this machine's encryption posture. Never throws.</summary>
    public static BitLockerReport Read()
    {
        try
        {
            var scope = ConnectedScope();
            var volumes = new List<VolumeInfo>();
            using var searcher = new ManagementObjectSearcher(
                scope, new ObjectQuery("SELECT * FROM Win32_EncryptableVolume"));
            using var collection = searcher.Get();
            foreach (ManagementBaseObject item in collection)
            {
                using (item)
                {
                    var volume = ReadVolume(item);
                    if (volume is not null) volumes.Add(volume);
                }
            }
            if (volumes.Count == 0)
            {
                // The provider answered and named no volumes. A Windows machine always has at
                // least its system volume, so this is an enumeration that failed rather than a
                // PC with nothing to encrypt -- and calling it `supported` would put an empty,
                // reassuring card in front of an operator.
                return new BitLockerReport(SupportError, "No encryptable volumes were returned.",
                                           []);
            }
            return new BitLockerReport(SupportSupported, "", volumes);
        }
        catch (BitLockerUnsupportedException e)
        {
            return new BitLockerReport(SupportUnsupported, e.Message, []);
        }
        catch (Exception e) when (e is ManagementException or UnauthorizedAccessException
                                       or COMException)
        {
            return new BitLockerReport(SupportError, Describe(e), []);
        }
        catch (Exception e)
        {
            // Deliberately broad at the outermost layer, and the reason is the caller: this
            // runs on the inventory loop that feeds a heartbeat, and a heartbeat that 500s
            // takes the machine offline fleet-wide. An unreadable posture is worth a red card;
            // it is not worth the machine.
            return new BitLockerReport(SupportError, Describe(e), []);
        }
    }

    /// <summary>The recovery passwords for the protector IDs the hub asked for, keyed by ID.
    ///
    /// **Only IDs in <paramref name="wanted"/> are ever read**, so the set of secrets that
    /// leaves this machine is bounded by what the hub said it lacked rather than by what the
    /// disk holds. A protector that cannot be read (a locked volume, a protector deleted
    /// between the heartbeat and this call) is simply absent from the result -- the hub asks
    /// again next heartbeat, which is the retry.</summary>
    public static IReadOnlyDictionary<string, (string Volume, string Password)>
        ReadRecoveryPasswords(IReadOnlyCollection<string> wanted)
    {
        var found = new Dictionary<string, (string, string)>(StringComparer.OrdinalIgnoreCase);
        if (wanted.Count == 0) return found;
        var asked = new HashSet<string>(wanted, StringComparer.OrdinalIgnoreCase);

        try
        {
            var scope = ConnectedScope();
            using var searcher = new ManagementObjectSearcher(
                scope, new ObjectQuery("SELECT * FROM Win32_EncryptableVolume"));
            using var collection = searcher.Get();
            foreach (ManagementBaseObject item in collection)
            {
                using (item)
                {
                    if (item is not ManagementObject volume) continue;
                    var mount = AsString(item, "DriveLetter");
                    foreach (var id in ProtectorIds(volume, ProtectorTypeNumericalPassword))
                    {
                        if (!asked.Contains(id) || found.ContainsKey(id)) continue;
                        var password = NumericalPassword(volume, id);
                        if (!string.IsNullOrWhiteSpace(password))
                            found[id] = (mount, password);
                    }
                }
            }
        }
        catch (Exception)
        {
            // Same reasoning as Read: whatever we managed to collect is worth sending, and
            // nothing here is worth costing the agent its loop. An empty result means the hub
            // asks again on the next heartbeat.
        }
        return found;
    }

    /// <summary>Connect the provider namespace with packet privacy, or say it is not there.
    ///
    /// The two failures are told apart deliberately: an absent namespace is a Home edition and
    /// a permanent, correct `unsupported`, while anything else is a provider that exists and
    /// is misbehaving, which is the only thing worth showing an operator as an error.</summary>
    private static ManagementScope ConnectedScope()
    {
        var options = new ConnectionOptions
        {
            // Required by the BitLocker provider -- see the class docstring. Without it the
            // connect fails in a way that reads like a missing namespace.
            Authentication = AuthenticationLevel.PacketPrivacy,
            Impersonation = ImpersonationLevel.Impersonate,
            EnablePrivileges = true,
        };
        var scope = new ManagementScope(NamespacePath, options);
        try
        {
            scope.Connect();
        }
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidNamespace)
        {
            throw new BitLockerUnsupportedException(
                "This edition of Windows has no BitLocker provider.");
        }
        return scope;
    }

    /// <summary>One volume's row, or null when it carries no usable identity.</summary>
    private static VolumeInfo? ReadVolume(ManagementBaseObject item)
    {
        var mount = AsString(item, "DriveLetter");
        var deviceId = AsString(item, "DeviceID");
        if (mount.Length == 0 && deviceId.Length == 0) return null;

        var protection = AsUInt(item, "ProtectionStatus") switch
        {
            0 => ProtectionOff,
            1 => ProtectionOn,
            _ => ProtectionUnknown,
        };
        var conversionCode = AsUInt(item, "ConversionStatus");
        var protectors = new List<KeyProtectorInfo>();
        if (item is ManagementObject volume)
        {
            // Every protector, then the numerical-password ones again to decide which are
            // which. Two queries rather than one call to GetKeyProtectorType per protector:
            // the filtered form is one round trip and cannot mis-classify, and a protector
            // wrongly labelled `recovery_password` would have the hub ask for a secret that
            // does not exist on every heartbeat for the life of the machine.
            var recovery = new HashSet<string>(
                ProtectorIds(volume, ProtectorTypeNumericalPassword),
                StringComparer.OrdinalIgnoreCase);
            foreach (var id in ProtectorIds(volume, null))
            {
                protectors.Add(new KeyProtectorInfo(
                    id,
                    recovery.Contains(id) ? KindRecoveryPassword : KindOther,
                    FriendlyName(volume, id)));
            }
        }

        return new VolumeInfo(
            mount, deviceId, protection,
            ConversionName(conversionCode),
            // Null rather than 0 when the provider did not say: the hub stores "unknown"
            // distinctly, and a 0 would be read as "not encrypted at all".
            AsNullableInt(item, "EncryptionPercentage"),
            MethodName(AsUInt(item, "EncryptionMethod")),
            protectors);
    }

    /// <summary>The protector IDs on a volume, optionally of one type. Empty on any failure --
    /// a volume whose protectors cannot be listed still has a protection status worth
    /// reporting.</summary>
    private static IReadOnlyList<string> ProtectorIds(ManagementObject volume, uint? type)
    {
        try
        {
            using var args = volume.GetMethodParameters("GetKeyProtectors");
            // 0 means "all types" to the provider, which is also what omitting it does. Passed
            // explicitly so the two call sites read the same.
            args["KeyProtectorType"] = type ?? 0u;
            using var result = volume.InvokeMethod("GetKeyProtectors", args, null);
            if (result?["VolumeKeyProtectorID"] is not string[] ids) return [];
            return ids.Where(id => !string.IsNullOrWhiteSpace(id)).ToList();
        }
        catch (Exception)
        {
            return [];
        }
    }

    private static string FriendlyName(ManagementObject volume, string id)
    {
        try
        {
            using var args = volume.GetMethodParameters("GetKeyProtectorFriendlyName");
            args["VolumeKeyProtectorID"] = id;
            using var result = volume.InvokeMethod("GetKeyProtectorFriendlyName", args, null);
            return result?["FriendlyName"] as string ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }

    private static string NumericalPassword(ManagementObject volume, string id)
    {
        try
        {
            using var args = volume.GetMethodParameters("GetKeyProtectorNumericalPassword");
            args["VolumeKeyProtectorID"] = id;
            using var result =
                volume.InvokeMethod("GetKeyProtectorNumericalPassword", args, null);
            return result?["NumericalPassword"] as string ?? "";
        }
        catch (Exception)
        {
            // A locked volume answers FVE_E_LOCKED_VOLUME here, and a locked volume is exactly
            // the machine somebody wants a key for -- but the key cannot be read from inside a
            // machine that cannot read the volume, and pretending otherwise would send an
            // empty string to the hub as though it were a password.
            return "";
        }
    }

    /// <summary>Microsoft's ConversionStatus enum, as a name the hub and the console can show
    /// without either of them holding the table. Unknown codes keep their number rather than
    /// being flattened, so a value a future Windows adds is visible rather than invisible.
    /// </summary>
    private static string ConversionName(uint? code) => code switch
    {
        0 => "fully_decrypted",
        1 => "fully_encrypted",
        2 => "encrypting",
        3 => "decrypting",
        4 => "encryption_paused",
        5 => "decryption_paused",
        null => "",
        _ => $"code_{code}",
    };

    private static string MethodName(uint? code) => code switch
    {
        0 => "none",
        1 => "AES 128 with Diffuser",
        2 => "AES 256 with Diffuser",
        3 => "AES 128",
        4 => "AES 256",
        5 => "Hardware encryption",
        6 => "XTS-AES 128",
        7 => "XTS-AES 256",
        null => "",
        _ => $"code_{code}",
    };

    private static string AsString(ManagementBaseObject item, string name)
    {
        try { return (item[name] as string ?? "").Trim(); }
        catch (ManagementException) { return ""; }
    }

    private static uint? AsUInt(ManagementBaseObject item, string name)
    {
        try
        {
            return item[name] is null ? null : Convert.ToUInt32(item[name]);
        }
        catch (Exception e) when (e is ManagementException or InvalidCastException
                                       or FormatException or OverflowException)
        {
            return null;
        }
    }

    private static int? AsNullableInt(ManagementBaseObject item, string name)
    {
        var value = AsUInt(item, name);
        return value is null ? null : (int)Math.Clamp(value.Value, 0u, 100u);
    }

    private static string Describe(Exception e) =>
        $"{e.GetType().Name}: {e.Message}".Trim();
}
