using System.Text.RegularExpressions;

namespace FleetHubAgent.Update;

/// <summary>Tolerant dotted-numeric version compare, matching the hub's version_tuple and the
/// Windows agent's VersionUtil: reads the leading numeric prefix, ignores any suffix
/// ("0.2.0-rc1" becomes 0.2.0), and pads so "0.2" equals "0.2.0".
///
/// **Identical to the Windows implementation on purpose.** Both agents compare against
/// manifests signed by the same key and produced by the same script, so two different notions
/// of "newer" would mean a release that upgrades one fleet and not the other -- and the fleet
/// it skipped would look up to date rather than stuck.</summary>
public static partial class VersionUtil
{
    [GeneratedRegex(@"^\s*(\d+(?:\.\d+)*)")]
    private static partial Regex LeadingNumeric();

    public static int[] Parse(string? v)
    {
        var m = LeadingNumeric().Match(v ?? "");
        if (!m.Success) return new[] { 0 };
        return m.Groups[1].Value.Split('.').Select(int.Parse).ToArray();
    }

    /// <summary>1 if a &gt; b, -1 if a &lt; b, 0 if equal.</summary>
    public static int Compare(string? a, string? b)
    {
        var ta = Parse(a);
        var tb = Parse(b);
        int n = Math.Max(ta.Length, tb.Length);
        for (int i = 0; i < n; i++)
        {
            int va = i < ta.Length ? ta[i] : 0;
            int vb = i < tb.Length ? tb[i] : 0;
            if (va != vb) return va > vb ? 1 : -1;
        }
        return 0;
    }
}
