namespace TempMonitorAgent.Bios;

/// <summary>A WMI query, as the BIOS sources need it: a namespace, a WQL string, and rows of
/// property bags. Injected rather than called directly so every vendor's parsing is testable
/// on a developer machine that has none of these namespaces -- which is all of them, since a
/// build box is never simultaneously a Dell, an HP and a Lenovo.</summary>
public delegate IEnumerable<IReadOnlyDictionary<string, object?>> WmiQuery(
    string namespacePath, string wql);

/// <summary>What one vendor source read.</summary>
/// <param name="Interface">The namespace that actually answered, for the vendor that has more
/// than one. Empty means "the source's own <see cref="IBiosVendorSource.Namespace"/>", which is
/// still the whole truth on two of the three -- so no source is forced to repeat itself.</param>
public sealed record BiosVendorResult(IReadOnlyList<BiosSetting> Settings, bool? PasswordSet,
                                      string Interface = "");

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

/// <summary>
/// Dell -- and the one vendor with TWO firmware interfaces in the field.
///
/// **<c>root\dcim\sysman\biosattributes</c> is tried first, Command | Monitor's DCIM_BIOS*
/// classes in <c>root\dcim\sysman</c> second.** The DCIM_* classes exist only where somebody
/// installed Dell Command | Monitor. The `biosattributes` provider ships with the Dell client
/// firmware driver on a stock business image, which is the deployment story this whole file
/// rests on -- so it is the one that answers on most of a fleet, and it wins where both are
/// present.
///
/// **The namespace shell outlives the provider, and that is what made reading only the DCIM_*
/// classes a SILENT failure.** <c>root\dcim\sysman</c> is present on a Dell that has never had
/// Command | Monitor -- it connects, and only its classes are missing. BiosReader's
/// missing-namespace test therefore read that as "this vendor's stack is installed" while all
/// three queries came back empty, and every such Dell reported
/// `error: the firmware interface returned no settings`: a real interface, a real attribute
/// list, and an error message pointing at neither. Testing a namespace is not testing an
/// interface, which is why both are tried below rather than one being chosen.
///
/// Nothing is aliased between the two. Every property name differs -- `PossibleValue` not
/// `PossibleValues`, `ReadOnly` not `IsReadOnly`, `DisplayName` not `AttributeDisplayName` --
/// and handing both spellings to one `Row.Str` call was rejected: it reads whichever the
/// machine happens to have, so the day one provider gains the other's property under a
/// different meaning, the wrong value is reported with nothing to show it changed.
/// </summary>
public sealed class DellBiosSource : IBiosVendorSource
{
    /// <summary>The BIOS attribute provider on a stock Dell business image.</summary>
    internal const string AttributesNamespace = @"root\dcim\sysman\biosattributes";

    /// <summary>Where that same provider keeps the setup-password state -- a THIRD namespace,
    /// not a class inside the one above.</summary>
    internal const string SecurityNamespace = @"root\dcim\sysman\wmisecurity";

    /// <summary>Dell Command | Monitor's namespace. Still in the field on older managed
    /// fleets, so it is a fallback rather than a deletion.</summary>
    internal const string LegacyNamespace = @"root\dcim\sysman";

    public string Vendor => "Dell";

    /// <summary>Named for the interface a Dell is expected to have, not for the one that
    /// answered: which of the two did is reported per read, in
    /// <see cref="BiosVendorResult.Interface"/>. This value is what an operator sees when
    /// NEITHER answered, and the one they would have to go and install is the useful thing to
    /// put in front of them.</summary>
    public string Namespace => AttributesNamespace;

    public bool Matches(string manufacturer) =>
        manufacturer.Contains("dell", StringComparison.OrdinalIgnoreCase);

    public BiosVendorResult Read(WmiQuery query)
    {
        // A missing namespace is CAUGHT here rather than left to propagate, because a Dell is
        // only unsupported when neither interface exists. Letting the first one's exception
        // out would file every Command | Monitor machine under "no manageable BIOS" -- the
        // permanent, quiet state nobody investigates.
        BiosVendorResult? modern = null, legacy = null;
        BiosInterfaceMissingException? absent = null;

        try { modern = ReadAttributes(query); }
        catch (BiosInterfaceMissingException e) { absent = e; }
        if (modern is { Settings.Count: > 0 }) return modern;

        try { legacy = ReadLegacy(query); }
        catch (BiosInterfaceMissingException e) { absent ??= e; }
        if (legacy is { Settings.Count: > 0 }) return legacy;

        // Neither produced an attribute, and which silence this was decides the outcome -- a
        // distinction BiosReader draws by exception. BOTH namespaces absent means there is no
        // Dell firmware interface here at all (unsupported). One present that enumerated
        // nothing is a fault (error), and an empty result is how this method says so.
        if (modern is null && legacy is null) throw absent!;
        return modern ?? legacy!;
    }

    /// <summary>The stock-image provider: three classes by type, like Command | Monitor's, and
    /// not one property name in common with them.</summary>
    private static BiosVendorResult ReadAttributes(WmiQuery query)
    {
        var settings = new List<BiosSetting>();

        foreach (var row in query(AttributesNamespace, "SELECT * FROM EnumerationAttribute"))
            AddAttribute(settings, row, BiosSettingKind.Enum, Row.List(row, "PossibleValue"));

        foreach (var row in query(AttributesNamespace, "SELECT * FROM StringAttribute"))
            AddAttribute(settings, row, BiosSettingKind.String, Array.Empty<string>());

        foreach (var row in query(AttributesNamespace, "SELECT * FROM IntegerAttribute"))
            AddAttribute(settings, row, BiosSettingKind.Integer, Array.Empty<string>());

        return new BiosVendorResult(settings, ReadPasswordState(query), AttributesNamespace);
    }

    private static void AddAttribute(List<BiosSetting> into,
                                     IReadOnlyDictionary<string, object?> row,
                                     BiosSettingKind kind, IReadOnlyList<string> possible)
    {
        var name = Row.Str(row, "AttributeName");
        if (name.Length == 0) return;
        into.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"), kind, possible,
                                 // A uint with a 0/1 ValueMap here, a bool on Command |
                                 // Monitor. Row.Flag reads both, which is why it exists.
                                 Row.Flag(row, "ReadOnly"),
                                 Row.Str(row, "DisplayName")));
    }

    /// <summary>Is any BIOS password set? Its own try/catch, because the security namespace is
    /// separate from the attribute one and can be absent on its own -- and an attribute list is
    /// worth reporting from a machine that cannot tell us about its passwords. Null is the
    /// honest answer for "we could not ask"; see BiosReport.PasswordSet.</summary>
    private static bool? ReadPasswordState(WmiQuery query)
    {
        try
        {
            bool? password = null;
            // One instance per password (admin, system, HDD) and ANY of them blocks a write,
            // so what the console needs is whether one is set, not which.
            foreach (var row in query(SecurityNamespace, "SELECT * FROM PasswordObject"))
                password = (password ?? false) || Row.Flag(row, "IsPasswordSet");
            return password;
        }
        catch (BiosInterfaceMissingException)
        {
            return null;
        }
    }

    /// <summary>Dell Command | Monitor. Unchanged from when it was the only interface this
    /// reader knew about -- it is still correct on a machine that has DCM, and was only ever
    /// wrong as the sole answer.</summary>
    private static BiosVendorResult ReadLegacy(WmiQuery query)
    {
        var settings = new List<BiosSetting>();

        foreach (var row in query(LegacyNamespace, "SELECT * FROM DCIM_BIOSEnumeration"))
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

        foreach (var row in query(LegacyNamespace, "SELECT * FROM DCIM_BIOSString"))
        {
            var name = Row.Str(row, "AttributeName");
            if (name.Length == 0) continue;
            settings.Add(new BiosSetting(name, Row.Str(row, "CurrentValue"),
                                         BiosSettingKind.String, Array.Empty<string>(),
                                         Row.Flag(row, "IsReadOnly"),
                                         Row.Str(row, "AttributeDisplayName")));
        }

        foreach (var row in query(LegacyNamespace, "SELECT * FROM DCIM_BIOSInteger"))
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
        foreach (var row in query(LegacyNamespace, "SELECT * FROM DCIM_BIOSPassword"))
        {
            password = (password ?? false) || Row.Flag(row, "IsSet");
        }

        return new BiosVendorResult(settings, password, LegacyNamespace);
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
