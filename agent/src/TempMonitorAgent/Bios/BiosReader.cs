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
            return BiosReport.Failed(source.Vendor, source.Namespace, e.Message, biosVersion);
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
    /// over ManagementObjectSearcher at the call site.</summary>
    public static IEnumerable<IReadOnlyDictionary<string, object?>> Query(
        string namespacePath, string wql)
    {
        ManagementObjectCollection collection;
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

        using var searcher = new ManagementObjectSearcher(scope, new ObjectQuery(wql));
        try
        {
            collection = searcher.Get();
        }
        catch (ManagementException e) when (e.ErrorCode is ManagementStatus.InvalidClass
                                                        or ManagementStatus.InvalidNamespace)
        {
            // A vendor namespace that exists but lacks one of the three per-type classes is
            // ordinary: HP machines without an integer setting have no HP_BIOSInteger. Empty,
            // not fatal -- the other classes still make a supported report.
            yield break;
        }

        using (collection)
        {
            foreach (ManagementBaseObject item in collection)
            {
                using (item)
                {
                    var row = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
                    foreach (PropertyData property in item.Properties)
                        row[property.Name] = property.Value;
                    yield return row;
                }
            }
        }
    }

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
