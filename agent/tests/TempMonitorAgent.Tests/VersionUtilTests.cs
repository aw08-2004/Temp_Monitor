using TempMonitorAgent.Update;

namespace TempMonitorAgent.Tests;

/// <summary>Mirrors companion.py _cmp_versions / version_tuple behaviour.</summary>
public class VersionUtilTests
{
    [Theory]
    [InlineData("3.0.1", "3.0.0", 1)]
    [InlineData("3.0.0", "3.0.1", -1)]
    [InlineData("3.0.0", "3.0.0", 0)]
    [InlineData("3.0", "3.0.0", 0)]     // padded equal
    [InlineData("3.1", "3.0.9", 1)]
    [InlineData("10.0.0", "9.9.9", 1)]  // numeric, not lexical
    [InlineData("3.0.1-rc1", "3.0.0", 1)] // suffix ignored
    [InlineData("", "0", 0)]
    public void Compare_Works(string a, string b, int expected)
    {
        Assert.Equal(expected, VersionUtil.Compare(a, b));
    }
}
