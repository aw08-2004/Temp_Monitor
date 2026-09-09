using System.Text.RegularExpressions;

namespace FleetHubAgent.Update;

/// <summary>Tolerant dotted-numeric version compare, matching the hub's
/// version_tuple/cmp_versions and the Windows agent's copy: reads the leading numeric prefix,
/// ignores any suffix ("0.4.0-rc1" becomes 0.4.0), and pads so "0.4" == "0.4.0".
///
/// **Four comparators have to agree, and they only agree on well-formed input** -- see
/// VERSIONING.md, which is why the format rule is three numeric components and nothing else.
/// This one truncates at the first non-digit; `release_client.py` scores the same string -1.
/// Stay inside the format and the disagreement never surfaces.</summary>
public static partial class VersionUtil
{
    [GeneratedRegex(@"^\s*(\d+(?:\.\d+)*)")]
    private static partial Regex LeadingNumeric();

    public static int[] Parse(string? v)
    {
        var m = LeadingNumeric().Match(v ?? "");
        if (!m.Success) return [0];
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
