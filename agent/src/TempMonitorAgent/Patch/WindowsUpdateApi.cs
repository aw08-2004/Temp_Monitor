using System.Reflection;
using System.Runtime.InteropServices;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Late-bound access to the Windows Update Agent COM API (WUApiLib).
///
/// <para><b>Late binding rather than an interop assembly, deliberately.</b> A generated
/// WUApiLib interop assembly is a build-time dependency on a type library that ships with
/// Windows, and this project publishes self-contained single-file. Going through
/// <c>Type.GetTypeFromProgID</c> plus <c>InvokeMember</c> costs some ceremony here and
/// removes a packaging problem everywhere else. It also degrades honestly: a machine with no
/// Windows Update Agent (a stripped container image) returns null from the ProgID lookup
/// instead of failing to load an assembly.</para>
///
/// <para><b>Every call is wrapped, because this API throws HRESULTs for ordinary conditions.</b>
/// A machine pointed at a WSUS server that is down, one mid-way through a servicing-stack
/// update, and one where the service is simply disabled all raise COMException from Search().
/// None of those is an agent fault, and none should cost the heartbeat — so the reader returns
/// an empty result with a reason rather than propagating.</para>
///
/// <para><b>This is used for READING only.</b> Installing goes through the same API in
/// <c>PatchInstaller</c>, but reading is separated so a scan can run on the inventory loop
/// with no possibility of changing the machine.</para>
/// </summary>
public static class WindowsUpdateApi
{
    /// <summary>The search this feature is about: what is applicable and not yet installed.
    ///
    /// `IsHidden=0` matters — an administrator who hid an update on a machine has made a
    /// decision, and offering it to the hub for approval would quietly override it.
    /// `IsInstalled=0` is the definition of "available", and it is also what makes an update
    /// DISAPPEAR from this list once it applies, which is the entire completion signal for a
    /// patch run (see hub/patches.py confirm_from_inventory).</summary>
    private const string SearchCriteria = "IsInstalled=0 AND IsHidden=0";

    public sealed record ScanResult(
        IReadOnlyList<AvailableUpdate> Updates, bool Supported, string Error);

    public static ScanResult Search()
    {
        object? session = null;
        try
        {
            var type = Type.GetTypeFromProgID("Microsoft.Update.Session");
            if (type is null)
            {
                return new ScanResult([], false,
                    "this machine has no Windows Update Agent");
            }
            session = Activator.CreateInstance(type);
            if (session is null)
            {
                return new ScanResult([], false,
                    "the Windows Update Agent could not be started");
            }

            var searcher = Invoke(session, "CreateUpdateSearcher");
            if (searcher is null)
            {
                return new ScanResult([], false,
                    "the Windows Update Agent returned no searcher");
            }

            var result = Invoke(searcher, "Search", SearchCriteria);
            var collection = result is null ? null : Get(result, "Updates");
            if (collection is null) return new ScanResult([], true, "");

            var count = ToInt(Get(collection, "Count"));
            var updates = new List<AvailableUpdate>(count);
            for (var i = 0; i < count; i++)
            {
                var item = Index(collection, i);
                if (item is null) continue;
                var mapped = Map(item);
                if (mapped is not null) updates.Add(mapped);
            }
            return new ScanResult(updates, true, "");
        }
        catch (COMException e)
        {
            // Ordinary on a fleet: WSUS unreachable, the service disabled by policy, the
            // servicing stack mid-update. Reported, never thrown.
            return new ScanResult([], true, $"Windows Update refused the search: 0x{e.HResult:X8}");
        }
        catch (Exception e) when (e is TargetInvocationException or MissingMethodException
                                       or InvalidCastException or UnauthorizedAccessException)
        {
            return new ScanResult([], true, $"Windows Update could not be read: {e.Message}");
        }
        finally
        {
            if (session is not null && Marshal.IsComObject(session)) Marshal.ReleaseComObject(session);
        }
    }

    /// <summary>One COM update object as a wire row, or null if it carries no usable identity.
    ///
    /// Public so a test can exercise the mapping against a stand-in object without a Windows
    /// Update Agent anywhere near it — the late binding means any object with the right member
    /// names will do, which is the one upside of not having an interop assembly.</summary>
    public static AvailableUpdate? Map(object update)
    {
        var identity = Get(update, "Identity");
        var guid = identity is null ? "" : ToText(Get(identity, "UpdateID"));
        var kb = FirstKb(Get(update, "KBArticleIDs"));
        // KB first: an approval is a statement about a patch, and the same KB carries a
        // different UpdateID per Windows build. See PatchModels.
        var native = guid;
        var uid = kb.Length > 0
            ? $"{PatchSources.WindowsUpdate}:kb{kb}"
            : $"{PatchSources.WindowsUpdate}:{guid.ToLowerInvariant()}";
        if (guid.Length == 0 && kb.Length == 0) return null;

        return new AvailableUpdate(
            Uid: uid,
            NativeId: native,
            Source: PatchSources.WindowsUpdate,
            Kb: kb.Length > 0 ? $"KB{kb}" : "",
            Title: ToText(Get(update, "Title")),
            Classification: Classify(Get(update, "Categories")),
            RebootRequired: RebootBehaviour(Get(update, "InstallationBehavior")),
            SizeBytes: ToLong(Get(update, "MaxDownloadSize")));
    }

    /// <summary>Map the update's category names onto the hub's classification vocabulary.
    ///
    /// An update carries several categories (a product, a family, and its type), so this looks
    /// for the type and takes the most consequential match rather than the first: an update
    /// that is both "Security Updates" and "Updates" is a security update, and reporting it as
    /// the latter would exclude it from the one classification an operator auto-approves.</summary>
    public static string Classify(object? categories)
    {
        if (categories is null) return "unknown";
        var best = "unknown";
        try
        {
            var count = ToInt(Get(categories, "Count"));
            for (var i = 0; i < count; i++)
            {
                var item = Index(categories, i);
                if (item is null) continue;
                var name = ToText(Get(item, "Name")).ToLowerInvariant();
                if (name.Contains("security")) return "security";
                if (name.Contains("critical")) best = "critical";
                else if (name.Contains("driver") && best == "unknown") best = "driver";
                else if ((name.Contains("feature pack") || name.Contains("upgrade"))
                         && best == "unknown") best = "feature";
                else if ((name.Contains("update") || name.Contains("service pack")
                          || name.Contains("definition") || name.Contains("tool"))
                         && best == "unknown") best = "other";
            }
        }
        catch (COMException) { return best; }
        return best;
    }

    /// <summary>Does installing this require a restart?
    ///
    /// RebootBehavior is 0 = never, 1 = always, 2 = "can request". `2` is treated as YES on
    /// purpose: the cost of a restart that turned out to be unnecessary is a restart, and the
    /// cost of skipping one that was needed is an update that reports installed and is not.
    /// The second is the failure this whole feature exists to prevent.</summary>
    public static bool RebootBehaviour(object? installationBehavior)
    {
        if (installationBehavior is null) return false;
        try { return ToInt(Get(installationBehavior, "RebootBehavior")) != 0; }
        catch (COMException) { return false; }
    }

    /// <summary>The first KB number on the update, digits only, or "".</summary>
    public static string FirstKb(object? kbArticleIds)
    {
        if (kbArticleIds is null) return "";
        try
        {
            var count = ToInt(Get(kbArticleIds, "Count"));
            for (var i = 0; i < count; i++)
            {
                var raw = ToText(Index(kbArticleIds, i)).Trim();
                if (raw.StartsWith("KB", StringComparison.OrdinalIgnoreCase)) raw = raw[2..];
                if (raw.Length > 0 && raw.All(char.IsAsciiDigit)) return raw;
            }
        }
        catch (COMException) { return ""; }
        return "";
    }

    // ------------------------------------------------------------------ late binding

    internal static object? Get(object? target, string property) =>
        target?.GetType().InvokeMember(property, BindingFlags.GetProperty, null, target, null);

    internal static object? Invoke(object? target, string method, params object?[] args) =>
        target?.GetType().InvokeMember(method, BindingFlags.InvokeMethod, null, target, args);

    /// <summary>Assign a COM property -- IUpdateDownloader.Updates and
    /// IUpdateInstaller.Updates, which are settable rather than passed as arguments.</summary>
    internal static void Set(object? target, string property, object? value) =>
        target?.GetType().InvokeMember(property, BindingFlags.SetProperty, null, target,
                                       [value]);

    /// <summary>Item i of a COM collection. IUpdateCollection exposes this as an indexed
    /// property named `Item`, which reflection reaches as a GetProperty with an argument.</summary>
    internal static object? Index(object? collection, int i) =>
        collection?.GetType().InvokeMember("Item", BindingFlags.GetProperty, null, collection,
                                           [i]);

    internal static string ToText(object? value) => value?.ToString() ?? "";

    internal static int ToInt(object? value)
    {
        if (value is null) return 0;
        try { return Convert.ToInt32(value); }
        catch (Exception e) when (e is FormatException or InvalidCastException or OverflowException)
        {
            return 0;
        }
    }

    internal static long ToLong(object? value)
    {
        if (value is null) return 0;
        try { return Convert.ToInt64(value); }
        catch (Exception e) when (e is FormatException or InvalidCastException or OverflowException)
        {
            return 0;
        }
    }
}
