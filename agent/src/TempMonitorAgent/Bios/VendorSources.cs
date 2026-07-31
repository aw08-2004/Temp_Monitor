namespace TempMonitorAgent.Bios;

/// <summary>A WMI query, as the BIOS sources need it: a namespace, a WQL string, and rows of
/// property bags. Injected rather than called directly so every vendor's parsing is testable
/// on a developer machine that has none of these namespaces -- which is all of them, since a
/// build box is never simultaneously a Dell, an HP and a Lenovo.</summary>
public delegate IEnumerable<IReadOnlyDictionary<string, object?>> WmiQuery(
    string namespacePath, string wql);

/// <summary>What one vendor source read.</summary>
public sealed record BiosVendorResult(IReadOnlyList<BiosSetting> Settings, bool? PasswordSet);

/// <summary>
/// One vendor's way of exposing firmware settings. There is no cross-vendor BIOS API, and
/// pretending otherwise is the main way this feature goes wrong -- so the shared shape is
/// deliberately thin: recognise a manufacturer string, name a namespace, produce settings.
///
/// **WMI first, vendor CLI never (here).** Dell's CCTK, HP's BiosConfigUtility and Lenovo's
/// ThinkBiosConfig would each add a deployment prerequisite to a single-file agent that is
/// meant to work on a stock vendor image. The WMI classes below ship with that image.
/// </summary>
public interface IBiosVendorSource
{
    /// <summary>Display name for the console. The machine's own manufacturer string is
    /// reported separately -- this is the vendor family we dispatched on.</summary>
    string Vendor { get; }

    /// <summary>The WMI namespace this vendor answers on, shown to the operator as the
    /// interface that produced the answer.</summary>
    string Namespace { get; }

    /// <summary>Does this source handle that Win32_ComputerSystem.Manufacturer?</summary>
    bool Matches(string manufacturer);

    BiosVendorResult Read(WmiQuery query);
}

/// <summary>Shared property-bag helpers. Every vendor's classes disagree about types --
/// IsReadOnly is a bool on one and a uint on another, CurrentValue is a string here and a
/// string[] there -- so nothing below indexes a row without going through these.</summary>
internal static class Row
{
    public static string Str(IReadOnlyDictionary<string, object?> row, params string[] keys)
    {
        foreach (var key in keys)
        {
            if (!row.TryGetValue(key, out var value) || value is null) continue;
            if (value is string[] many) return string.Join(", ", many.Where(s => s is not null));
            if (value is object[] objects)
                return string.Join(", ", objects.Where(o => o is not null).Select(o => o!.ToString()));
            var text = value.ToString();
            if (!string.IsNullOrWhiteSpace(text)) return text!.Trim();
        }
        return "";
    }

    public static IReadOnlyList<string> List(IReadOnlyDictionary<string, object?> row,
                                             params string[] keys)
    {
        foreach (var key in keys)
        {
            if (!row.TryGetValue(key, out var value) || value is null) continue;
            if (value is string[] many)
                return many.Where(s => !string.IsNullOrWhiteSpace(s)).Select(s => s.Trim()).ToList();
            if (value is object[] objects)
                return objects.Where(o => o is not null).Select(o => o!.ToString()!.Trim())
                              .Where(s => s.Length > 0).ToList();
            var text = value.ToString();
            if (!string.IsNullOrWhiteSpace(text)) return new List<string> { text!.Trim() };
        }
        return Array.Empty<string>();
    }

    /// <summary>Truthiness across the shapes these classes actually use: a real bool, a
    /// numeric 0/1, or the strings "true"/"1"/"yes". Anything else is false -- and false is
    /// the safe default here, since a setting wrongly marked writable merely fails at the
    /// write, while one wrongly marked read-only is invisible forever.</summary>
    public static bool Flag(IReadOnlyDictionary<string, object?> row, params string[] keys)
    {
        foreach (var key in keys)
        {
            if (!row.TryGetValue(key, out var value) || value is null) continue;
            if (value is bool b) return b;
            var text = value.ToString()?.Trim().ToLowerInvariant() ?? "";
            if (text is "true" or "yes") return true;
            if (long.TryParse(text, out var n)) return n != 0;
        }
        return false;
    }
}

/// <summary>Dell, via <c>root\dcim\sysman</c>: the namespace Dell Command | Monitor installs
/// and that ships on a stock Dell business image. Attributes are split across three classes by
/// type, which is why the kinds below are assigned per class rather than guessed from the
/// value.</summary>
public sealed class DellBiosSource : IBiosVendorSource
{
    public string Vendor => "Dell";
    public string Namespace => @"root\dcim\sysman";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("dell", StringComparison.OrdinalIgnoreCase);

    public BiosVendorResult Read(WmiQuery query)
    {
        var settings = new List<BiosSetting>();

        foreach (var row in query(Namespace, "SELECT * FROM DCIM_BIOSEnumeration"))
        {
            var name = Row.Str(row, "AttributeName");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(
                name,
                Row.Str(row, "CurrentValue"),
                BiosSettingKind.Enum,
                Row.List(row, "PossibleValuesDescription", "PossibleValues"),
                Row.Flag(row, "IsReadOnly"),
                Row.Str(row, "AttributeDisplayName")));
        }

        foreach (var row in query(Namespace, "SELECT * FROM DCIM_BIOSString"))
        {
            var name = Row.Str(row, "AttributeName");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.String, Array.Empty<string>(),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "AttributeDisplayName")));
        }

        foreach (var row in query(Namespace, "SELECT * FROM DCIM_BIOSInteger"))
        {
            var name = Row.Str(row, "AttributeName");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.Integer, Array.Empty<string>(),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "AttributeDisplayName")));
        }

        // Any set password blocks writes, so the interesting fact is "is one set at all",
        // not which of them. A missing class leaves it null rather than false -- see
        // BiosReport.PasswordSet.
        bool? password = null;
        foreach (var row in query(Namespace, "SELECT * FROM DCIM_BIOSPassword"))
        {
            password = (password ?? false) || Row.Flag(row, "IsSet");
        }

        return new BiosVendorResult(settings, password);
    }
}

/// <summary>HP, via <c>root\hp\instrumentedBIOS</c>. Same three-classes-by-type shape as Dell
/// with different names, and the attribute key is <c>Name</c> rather than
/// <c>AttributeName</c> -- which is exactly the kind of difference an alias layer would
/// paper over and then get wrong.</summary>
public sealed class HpBiosSource : IBiosVendorSource
{
    public string Vendor => "HP";
    public string Namespace => @"root\hp\instrumentedBIOS";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("hp", StringComparison.OrdinalIgnoreCase)
        || manufacturer.Contains("hewlett", StringComparison.OrdinalIgnoreCase);

    public BiosVendorResult Read(WmiQuery query)
    {
        var settings = new List<BiosSetting>();

        foreach (var row in query(Namespace, "SELECT * FROM HP_BIOSEnumeration"))
        {
            var name = Row.Str(row, "Name");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.Enum,
                                         Row.List(row, "PossibleValues"),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "DisplayName")));
        }

        foreach (var row in query(Namespace, "SELECT * FROM HP_BIOSString"))
        {
            var name = Row.Str(row, "Name");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.String, Array.Empty<string>(),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "DisplayName")));
        }

        foreach (var row in query(Namespace, "SELECT * FROM HP_BIOSInteger"))
        {
            var name = Row.Str(row, "Name");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.Integer, Array.Empty<string>(),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "DisplayName")));
        }

        bool? password = null;
        foreach (var row in query(Namespace, "SELECT * FROM HP_BIOSPassword"))
        {
            password = (password ?? false) || Row.Flag(row, "IsSet");
        }

        return new BiosVendorResult(settings, password);
    }
}

/// <summary>
/// Lenovo, via <c>root\wmi</c>'s <c>Lenovo_BiosSetting</c> -- and the odd one out. There is
/// no per-attribute property set: every setting arrives as ONE string,
/// <c>"WakeOnLAN,Enable;[Enable,Disable,ACOnly]"</c>, so the parsing below is the interface.
/// A machine whose firmware omits the option list still reports its value, which is why the
/// kind falls back to Unknown rather than being assumed an enum.
/// </summary>
public sealed class LenovoBiosSource : IBiosVendorSource
{
    public string Vendor => "Lenovo";
    public string Namespace => @"root\wmi";

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("lenovo", StringComparison.OrdinalIgnoreCase);

    public BiosVendorResult Read(WmiQuery query)
    {
        var settings = new List<BiosSetting>();
        foreach (var row in query(Namespace, "SELECT * FROM Lenovo_BiosSetting"))
        {
            var parsed = ParseCurrentSetting(Row.Str(row, "CurrentSetting"));
            if (parsed is not null) settings.Add(parsed);
        }

        // PasswordState is a bitmask of which passwords exist; 0 means none. Anything
        // non-zero blocks a write, which is all the console needs to know.
        bool? password = null;
        foreach (var row in query(Namespace, "SELECT * FROM Lenovo_BiosPasswordSettings"))
        {
            password = (password ?? false) || Row.Flag(row, "PasswordState");
        }

        return new BiosVendorResult(settings, password);
    }

    /// <summary>Parse one <c>CurrentSetting</c> string. Internal-visible and pure, because
    /// this format IS Lenovo's whole API and it is the only part of it a test can reach.</summary>
    internal static BiosSetting? ParseCurrentSetting(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;

        // Options, when present, follow a ';'. Some firmware writes "[A,B]", some writes
        // "A,B", and some appends a second ';'-delimited section we do not use.
        var head = raw;
        var options = new List<string>();
        var semi = raw.IndexOf(';');
        if (semi >= 0)
        {
            head = raw[..semi];
            var tail = raw[(semi + 1)..].Trim();
            var close = tail.IndexOf(']');
            if (tail.StartsWith('[') && close > 0) tail = tail[1..close];
            else if (tail.Contains(';')) tail = tail[..tail.IndexOf(';')];
            foreach (var option in tail.Split(',', StringSplitOptions.TrimEntries))
                if (option.Length > 0 && !options.Contains(option)) options.Add(option);
        }

        var comma = head.IndexOf(',');
        // No comma at all means no value -- a name on its own tells an operator nothing and
        // gives a future write nothing to target, so it is dropped rather than shown empty.
        if (comma <= 0) return null;
        var name = head[..comma].Trim();
        var value = head[(comma + 1)..].Trim();
        if (name.Length == 0) return null;

        return new BiosSetting(
            name, value,
            options.Count > 0 ? BiosSettingKind.Enum : BiosSettingKind.Unknown,
            options,
            // Lenovo does not report read-only-ness here. Reporting `false` is the honest
            // answer for "we were not told" only because a write attempt is what finds out
            // anyway; nothing in the console promises a write will succeed.
            ReadOnly: false);
    }
}
