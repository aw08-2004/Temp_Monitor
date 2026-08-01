using System.Management;

namespace TempMonitorAgent.Bios;

/// <summary>
/// Changes this machine's firmware settings, whoever made it (roadmap #9) -- the write half of
/// <see cref="BiosReader"/>, and dispatched exactly the same way: on the manufacturer, then on
/// the namespace being there.
///
/// **There is no cross-vendor write API either, and the three disagree more than they do on
/// reads.** Dell takes parallel name/value ARRAYS in one call; HP takes one name/value pair per
/// call and wants the setup password in a peculiar <c>&lt;utf-16/&gt;</c>-prefixed form; Lenovo
/// takes a single <c>"Name,Value"</c> string per call and then needs a SEPARATE
/// <c>SaveBiosSettings</c> to commit anything at all. That last one is the trap: a Lenovo write
/// that skips the save returns Success and changes nothing.
///
/// **Success here is not the answer.** Every method below reports whether the vendor accepted
/// the write, which on firmware is a different claim from "the setting is now that" -- some
/// attributes apply live, some sit pending until POST, and where the line falls is per vendor
/// AND per setting. The executor therefore re-reads and the hub compares (see bios.py's
/// classify_result). Nothing in this file tries to guess whether a reboot is needed, because
/// nothing in this file can know.
/// </summary>
public static class BiosWriter
{
    /// <summary>Invoke a WMI method on the FIRST instance of a class, and hand back its output
    /// properties. Injected like <see cref="WmiQuery"/>, so all three vendors' call shapes are
    /// testable on a machine that is none of them.
    ///
    /// First instance rather than a named one deliberately: all three of these classes are
    /// singleton service objects, and none of them exposes a key worth selecting on.</summary>
    public delegate IReadOnlyDictionary<string, object?> WmiInvoke(
        string namespacePath, string className, string methodName,
        IReadOnlyDictionary<string, object?> args);

    /// <summary>What happened to one attribute. <see cref="Error"/> is empty on success --
    /// and success means only that the vendor accepted the write.</summary>
    public sealed record WriteOutcome(string Name, string Value, string Error)
    {
        public bool Ok => Error.Length == 0;
    }

    /// <summary>
    /// Apply every requested change, then report per attribute.
    ///
    /// **One failure does not abort the rest.** An operator who ticked six settings and hit a
    /// vendor refusal on the third is better served by the other five landing and a named
    /// failure than by a half-applied change whose boundary depends on dictionary order. The
    /// hub rolls these into `partial` precisely so that state has somewhere to go.
    /// </summary>
    public static IReadOnlyList<WriteOutcome> Write(
        string manufacturer, IReadOnlyList<(string Name, string Value)> changes,
        string? password, WmiInvoke invoke)
    {
        manufacturer = (manufacturer ?? "").Trim();
        var source = Writers.FirstOrDefault(w => w.Matches(manufacturer));
        if (source is null)
        {
            // Reported per attribute rather than thrown, so the hub records the same shape
            // whether the interface was missing or the write was refused -- an operator does
            // not have to learn two failure vocabularies.
            var why = manufacturer.Length == 0
                ? "no manufacturer reported, so there is no firmware interface to write through"
                : $"no firmware write interface for {manufacturer}";
            return changes.Select(c => new WriteOutcome(c.Name, c.Value, why)).ToList();
        }

        var results = new List<WriteOutcome>();
        foreach (var (name, value) in changes)
        {
            string error;
            try
            {
                error = source.WriteOne(name, value, password, invoke) ?? "";
            }
            catch (BiosInterfaceMissingException e)
            {
                error = e.Message;
            }
            catch (Exception e)
            {
                error = BiosReader.Describe(e);
            }
            results.Add(new WriteOutcome(name, value, error));
        }

        // Lenovo commits nothing until this runs, and a skipped save is the single most
        // plausible way this feature reports six green rows over a machine that changed
        // nothing. Only attempted if something was actually written -- a commit after six
        // failures would report a second, confusing error.
        if (results.Any(r => r.Ok))
        {
            var commitError = source.Commit(password, invoke);
            if (commitError is not null)
            {
                // Attributed to every attribute that thought it succeeded: the commit is what
                // makes any of them real, so none of them did.
                results = results
                    .Select(r => r.Ok ? r with { Error = commitError } : r)
                    .ToList();
            }
        }
        return results;
    }

    /// <summary>The write sources, in dispatch order -- deliberately parallel to
    /// <see cref="BiosReader.Sources"/>, so a vendor that can be read and not written is a
    /// visible gap rather than a silent one.</summary>
    public static readonly IReadOnlyList<IBiosVendorWriter> Writers = new IBiosVendorWriter[]
    {
        new DellBiosWriter(),
        new HpBiosWriter(),
        new LenovoBiosWriter(),
    };

    /// <summary>The real WMI method adapter. Mirrors <see cref="BiosReader.Query"/>, including
    /// the missing-namespace translation: a machine whose vendor stack was never installed must
    /// report "no interface", not a raw COM error.</summary>
    public static IReadOnlyDictionary<string, object?> Invoke(
        string namespacePath, string className, string methodName,
        IReadOnlyDictionary<string, object?> args)
    {
        var scope = new ManagementScope(namespacePath);
        try
        {
            scope.Connect();
        }
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidNamespace)
        {
            throw new BiosInterfaceMissingException($"{namespacePath} is not present");
        }

        using var searcher = new ManagementObjectSearcher(
            scope, new ObjectQuery($"SELECT * FROM {className}"));
        using var collection = searcher.Get();
        foreach (ManagementBaseObject item in collection)
        {
            using var instance = (ManagementObject)item;
            using var parameters = instance.GetMethodParameters(methodName);
            foreach (var (key, value) in args) parameters[key] = value;
            using var output = instance.InvokeMethod(methodName, parameters, null);
            var row = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
            foreach (PropertyData property in output.Properties) row[property.Name] = property.Value;
            return row;
        }
        // The class exists (the namespace connected) but has no instance. On these three
        // vendors that means the management stack is present and not functioning, which is a
        // fault, not an "unsupported" -- same split BiosReader draws.
        throw new InvalidOperationException($"{className} has no instance to invoke {methodName} on");
    }
}

/// <summary>One vendor's way of CHANGING a firmware setting. Separate from
/// <see cref="IBiosVendorSource"/> rather than bolted onto it, because the two split
/// differently: reading is three queries and a parse, writing is a method call, a password
/// convention and -- on exactly one vendor -- a commit.</summary>
public interface IBiosVendorWriter
{
    bool Matches(string manufacturer);

    /// <summary>Write one attribute. Returns null on acceptance, or a reason.</summary>
    string? WriteOne(string name, string value, string? password, BiosWriter.WmiInvoke invoke);

    /// <summary>Commit, for the vendor that needs one. Null when there is nothing to do --
    /// which is two of the three, and they say so by returning null rather than by the caller
    /// knowing which is which.</summary>
    string? Commit(string? password, BiosWriter.WmiInvoke invoke) => null;
}

/// <summary>Shared return-code handling. All three report through an output property whose
/// NAME differs and whose TYPE differs -- a uint on two of them and a status string on
/// Lenovo -- so nothing below reads a result directly.</summary>
internal static class WriteResult
{
    /// <summary>Dell and HP: 0 is success, everything else is a documented failure code. The
    /// codes are reported verbatim rather than mapped to prose: the meanings differ per
    /// vendor, per model and per firmware revision, and a wrong friendly message is worse than
    /// a number an operator can search for.</summary>
    public static string? FromNumeric(IReadOnlyDictionary<string, object?> result,
                                      params string[] keys)
    {
        foreach (var key in keys)
        {
            if (!result.TryGetValue(key, out var value) || value is null) continue;
            if (long.TryParse(value.ToString(), out var code))
                return code == 0 ? null : $"the firmware refused the write (code {code})";
            // A non-numeric value in a numeric slot is not something to interpret.
            return $"the firmware returned an unexpected result: {value}";
        }
        // No return value at all. Deliberately an ERROR rather than an assumed success: the
        // whole point of this half of the feature is not claiming a change we cannot see.
        return "the firmware interface returned no result";
    }

    /// <summary>Lenovo: a status STRING, and "Success" is the only good one. The others
    /// ("Not Supported", "Invalid Parameter", "Access Denied", "System Busy") are already
    /// operator-readable, so they are passed through as they arrive.</summary>
    public static string? FromLenovo(IReadOnlyDictionary<string, object?> result)
    {
        foreach (var key in new[] { "return", "Return" })
        {
            if (!result.TryGetValue(key, out var value) || value is null) continue;
            var text = value.ToString()?.Trim() ?? "";
            if (text.Equals("Success", StringComparison.OrdinalIgnoreCase)) return null;
            return text.Length > 0 ? text : "the firmware refused the write";
        }
        return "the firmware interface returned no result";
    }
}

/// <summary>Dell, via <c>DCIM_BIOSService.SetBIOSAttributes</c> in <c>root\dcim\sysman</c>.
/// Takes PARALLEL arrays, which is why every argument below is a one-element array rather than
/// a scalar -- passing a bare string here fails with a type error, not a nice message. The
/// setup password goes in <c>AuthorizationToken</c> as plain text.</summary>
public sealed class DellBiosWriter : IBiosVendorWriter
{
    private const string Ns = @"root\dcim\sysman";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("dell", StringComparison.OrdinalIgnoreCase);

    public string? WriteOne(string name, string value, string? password,
                            BiosWriter.WmiInvoke invoke)
    {
        var args = new Dictionary<string, object?>
        {
            ["AttributeName"] = new[] { name },
            ["AttributeValue"] = new[] { value },
        };
        // Omitted entirely when there is none. Dell treats an empty-string token as a supplied
        // (and wrong) password on some models, so sending "" where null was meant turns "no
        // password needed" into an authentication failure.
        if (!string.IsNullOrEmpty(password)) args["AuthorizationToken"] = password;

        var result = invoke(Ns, "DCIM_BIOSService", "SetBIOSAttributes", args);
        return WriteResult.FromNumeric(result, "SetResult", "ReturnValue");
    }
}

/// <summary>HP, via <c>HP_BIOSSettingInterface.SetBIOSSetting</c> in
/// <c>root\hp\instrumentedBIOS</c>. One pair per call, and the password has to be
/// <c>&lt;utf-16/&gt;</c>-prefixed -- HP's own encoding marker, not a typo and not optional:
/// sent without it, a correct password is rejected as wrong.</summary>
public sealed class HpBiosWriter : IBiosVendorWriter
{
    private const string Ns = @"root\hp\instrumentedBIOS";

    /// <summary>HP's marker saying the password that follows is UTF-16. The alternative
    /// (<c>&lt;kbd/&gt;</c>, a scan-code encoding) is keyboard-layout dependent, so this is the
    /// one to use from a service that has no idea what keyboard the machine has.</summary>
    private const string PasswordEncoding = "<utf-16/>";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("hp", StringComparison.OrdinalIgnoreCase)
        || manufacturer.Contains("hewlett", StringComparison.OrdinalIgnoreCase);

    public string? WriteOne(string name, string value, string? password,
                            BiosWriter.WmiInvoke invoke)
    {
        var result = invoke(Ns, "HP_BIOSSettingInterface", "SetBIOSSetting",
            new Dictionary<string, object?>
            {
                ["Name"] = name,
                ["Value"] = value,
                // Unlike Dell, HP wants the parameter present and empty when unset.
                ["Password"] = string.IsNullOrEmpty(password) ? "" : PasswordEncoding + password,
            });
        return WriteResult.FromNumeric(result, "Return", "ReturnValue");
    }
}

/// <summary>
/// Lenovo, via <c>Lenovo_SetBiosSetting</c> in <c>root\wmi</c> -- and the odd one out on writes
/// as it is on reads. The value is a single <c>"Name,Value"</c> string, mirroring the
/// <c>"Name,Value;[options]"</c> format the reader parses, and NOTHING happens until
/// <c>Lenovo_SaveBiosSettings.SaveBiosSettings</c> runs.
///
/// The save's password argument is a three-part <c>"password,ascii,us"</c> string: the
/// password, its encoding, and a keyboard layout. `ascii` + `us` is the combination that does
/// not depend on the machine's keyboard, which matters from a service with no session.
/// </summary>
public sealed class LenovoBiosWriter : IBiosVendorWriter
{
    private const string Ns = @"root\wmi";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("lenovo", StringComparison.OrdinalIgnoreCase);

    public string? WriteOne(string name, string value, string? password,
                            BiosWriter.WmiInvoke invoke)
    {
        // A comma in either half would be read as the separator and silently write the wrong
        // attribute -- refused rather than escaped, because Lenovo defines no escape for it.
        if (name.Contains(',') || value.Contains(','))
            return "Lenovo's firmware interface cannot express a setting name or value "
                 + "containing a comma";

        var result = invoke(Ns, "Lenovo_SetBiosSetting", "SetBiosSetting",
            new Dictionary<string, object?> { ["parameter"] = $"{name},{value}" });
        return WriteResult.FromLenovo(result);
    }

    public string? Commit(string? password, BiosWriter.WmiInvoke invoke)
    {
        var parameter = string.IsNullOrEmpty(password) ? "" : $"{password},ascii,us";
        try
        {
            var result = invoke(Ns, "Lenovo_SaveBiosSettings", "SaveBiosSettings",
                new Dictionary<string, object?> { ["parameter"] = parameter });
            var error = WriteResult.FromLenovo(result);
            return error is null ? null : $"the settings were not saved: {error}";
        }
        catch (Exception e)
        {
            return $"the settings were not saved: {BiosReader.Describe(e)}";
        }
    }
}
