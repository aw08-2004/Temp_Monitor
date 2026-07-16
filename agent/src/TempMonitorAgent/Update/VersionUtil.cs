using System.Text.RegularExpressions;

namespace TempMonitorAgent.Update;

/// <summary>Tolerant dotted-numeric version compare, matching companion.py
/// version_tuple/_cmp_versions: reads the leading numeric prefix, ignores any suffix
/// ("3.0.1-rc1" → 3.0.1), and pads so "3.0" == "3.0.0".</summary>
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
