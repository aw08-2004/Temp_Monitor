using System.Management;

namespace TempMonitorAgent.Bios;

/// <summary>
/// Reads this machine's firmware settings, whoever made it (roadmap #9).
///
/// **Dispatch on the manufacturer, then on the namespace being there.** The manufacturer
/// string picks a vendor source; the source's namespace existing is what decides between
/// "supported" and "unsupported". Both steps are needed: a Dell with Command | Monitor never
/// installed looks like a Dell and answers like a whitebox, and reporting an error for it
/// would be wrong -- there is nothing broken, the interface simply is not there.
///
/// **Unsupported is a first-class outcome**, not a failure. VMs, whiteboxes and consumer
/// boards report it once and then stay quiet; the console renders it as a neutral, permanent
/// state. The only thing that shows as an error is a namespace that exists and misbehaves.
/// </summary>
public static class BiosReader
{
    /// <summary>The vendor sources, in dispatch order. Adding a fourth vendor is adding a
    /// class here -- deliberately the only extension point, so a new vendor cannot quietly
    /// introduce a second way of deciding what "supported" means.</summary>
    public static readonly IReadOnlyList<IBiosVendorSource> Sources = new IBiosVendorSource[]
    {
        new DellBiosSource(),
        new HpBiosSource(),
        new LenovoBiosSource(),
    };

    /// <summary>Read the machine's firmware settings. Pure with respect to WMI: `query` and
    /// the two identity strings are injected, so the whole decision tree is testable without
    /// a Dell, an HP and a Lenovo on the desk.</summary>
    public static BiosReport Read(string manufacturer, string biosVersion, WmiQuery query)
    {
        manufacturer = (manufacturer ?? "").Trim();
        biosVersion = (biosVersion ?? "").Trim();

        if (manufacturer.Length == 0)
        {
            // SystemInfo filters whitebox placeholders ("System manufacturer", "To Be Filled
            // By O.E.M.") to nothing precisely so they cannot be dispatched on. A machine
            // that reaches here has no vendor to ask.
            return BiosReport.Unsupported("no manufacturer reported", biosVersion);
        }

        var source = Sources.FirstOrDefault(s => s.Matches(manufacturer));
        if (source is null)
            return BiosReport.Unsupported($"no firmware interface for {manufacturer}", biosVersion);

        BiosVendorResult result;
        try
        {
            result = source.Read(query);
        }
        catch (BiosInterfaceMissingException e)
        {
            // The namespace is absent: this vendor's management stack was never installed, or
            // this model does not carry it. Unsupported, not broken.
            return BiosReport.Unsupported(e.Message, biosVersion);
        }
        catch (Exception e)
        {
            return BiosReport.Failed(source.Vendor, source.Namespace, Describe(e), biosVersion);
        }

        if (result.Settings.Count == 0)
        {
            // A namespace that exists and enumerates nothing has not told us this machine is
            // unmanageable -- it has failed to answer. Saying "unsupported" here would hide a
            // permission problem or a broken WMI repository behind a state nobody investigates.
            return BiosReport.Failed(source.Vendor, source.Namespace,
                                     "the firmware interface returned no settings", biosVersion);
        }

        return new BiosReport(BiosSupport.Supported, source.Vendor, source.Namespace,
                              biosVersion, result.PasswordSet, "", result.Settings);
    }

    /// <summary>The real WMI adapter. Distinguishes a missing namespace (unsupported) from
    /// every other failure (an error), which is the whole reason this is not just a lambda
    /// over ManagementObjectSearcher at the call site.
    ///
    /// **Materialised, not an iterator, on purpose.** A WMI query does not run at
    /// <c>searcher.Get()</c> -- that hands back a collection which executes on first MoveNext.
    /// A missing class therefore throws while the CALLER enumerates, and in an iterator method
    /// that is outside any try/catch this method could write. The list is small (a few hundred
    /// rows), and reading it eagerly is what makes "this model has no DCIM_BIOSInteger"
    /// catchable at all.</summary>
    public static IReadOnlyList<IReadOnlyDictionary<string, object?>> Query(
        string namespacePath, string wql)
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
        catch (ManagementException e) when (e.ErrorCode == ManagementStatus.InvalidClass)
        {
            throw new BiosInterfaceMissingException($"{namespacePath} has no BIOS classes");
        }

        var rows = new List<IReadOnlyDictionary<string, object?>>();
        try
        {
            using var searcher = new ManagementObjectSearcher(scope, new ObjectQuery(wql));
            using var collection = searcher.Get();
            foreach (ManagementBaseObject item in collection)
            {
                using (item)
                {
                    var row = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
                    foreach (PropertyData property in item.Properties)
                        row[property.Name] = property.Value;
                    rows.Add(row);
                }
            }
        }
        catch (ManagementException e) when (e.ErrorCode is ManagementStatus.InvalidClass
                                                        or ManagementStatus.InvalidNamespace
                                                        or ManagementStatus.NotFound)
        {
            // A vendor namespace that exists but lacks one of the per-type classes is ordinary:
            // an HP with no integer setting has no HP_BIOSInteger, and plenty of Dell models
            // carry no DCIM_BIOSPassword. Empty, not fatal -- the other classes still make a
            // supported report. Getting this wrong is what turned a working Dell into
            // "Could not read: Invalid class".
            return Array.Empty<IReadOnlyDictionary<string, object?>>();
        }
        return rows;
    }

    /// <summary>A failure message an operator elsewhere can act on. WMI messages come from
    /// Windows on the reporting machine and arrive in THAT machine's language -- a Spanish
    /// "Clase no válida" in an English console is the normal case, not a bug. The
    /// locale-independent ErrorCode is prefixed so the fault is still identifiable, and
    /// searchable, whatever language the machine speaks.</summary>
    internal static string Describe(Exception e) =>
        e is ManagementException m ? $"{m.ErrorCode}: {m.Message.Trim()}" : e.Message;

    /// <summary>SMBIOS BIOS version, for the console's header line. Its own try/catch because
    /// a machine with no readable Win32_BIOS still has firmware settings worth reporting.</summary>
    public static string BiosVersion()
    {
        try
        {
            foreach (var row in Query(@"root\cimv2",
                                      "SELECT SMBIOSBIOSVersion FROM Win32_BIOS"))
            {
                var version = Row.Str(row, "SMBIOSBIOSVersion");
                if (version.Length > 0) return version;
            }
        }
        catch (Exception) { /* identity is a nicety here, never the point */ }
        return "";
    }

    /// <summary>Win32_ComputerSystem.Manufacturer, with the same placeholder filtering
    /// SystemInfo applies -- shared through SystemInfo so the value dispatched on here and
    /// the value shown on the machine page's Identity card can never disagree.</summary>
    public static string Manufacturer()
    {
        try
        {
            foreach (var row in Query(@"root\cimv2",
                                      "SELECT Manufacturer FROM Win32_ComputerSystem"))
            {
                var raw = Row.Str(row, "Manufacturer");
                if (Telemetry.SystemInfo.IsRealManufacturer(raw)) return raw;
            }
        }
        catch (Exception) { /* treated as "no manufacturer" -> unsupported */ }
        return "";
    }
}
