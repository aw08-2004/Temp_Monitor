using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers the two places where a bad string becomes a wrong FLEET, not just a wrong field.
///
/// DMI placeholders are the sharp one. A board vendor that leaves product_serial unset ships
/// the literal text "To Be Filled By O.E.M." on every unit, so passing it through gives dozens
/// of machines the same serial number -- and the hub MERGES machines that report a matching
/// serial (resolve_serial_group, the duplicate-hostname case). Unrelated boxes would collapse
/// into one console entry, and the operator's only evidence would be machines disappearing.
/// </summary>
public class DmiCleanTests
{
    [Theory]
    [InlineData("To Be Filled By O.E.M.")]
    [InlineData("to be filled by o.e.m.")]   // vendors differ on case; the hazard does not
    [InlineData("Default string")]
    [InlineData("Not Specified")]
    [InlineData("System Serial Number")]
    [InlineData("None")]
    [InlineData("N/A")]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("0000000")]                  // filled with a repeated character instead
    [InlineData("-")]
    public void Placeholders_become_null(string raw) => Assert.Null(SystemInfo.Clean(raw));

    [Theory]
    [InlineData("5CG1234ABC", "5CG1234ABC")]
    [InlineData("  7XY9Z01  ", "7XY9Z01")]
    [InlineData("VMware-42 0a bc", "VMware-42 0a bc")]
    public void Real_values_survive(string raw, string expected) =>
        Assert.Equal(expected, SystemInfo.Clean(raw));

    [Fact]
    public void A_serial_that_merely_contains_a_placeholder_word_survives()
    {
        // Matched whole, not as a substring: refusing a real serial because it happens to
        // contain "None" would leave a machine unmergeable, which is the opposite failure and
        // just as quiet.
        Assert.Equal("NoneSuch123", SystemInfo.Clean("NoneSuch123"));
    }
}

/// <summary>
/// os-release parsing, which decides the caption the hub buckets a machine's OS by.
///
/// The quoting is the whole job: PRETTY_NAME is always quoted, VERSION_ID sometimes is, and a
/// caption that arrives still wrapped in its quotes is what an operator sees in the console.
/// </summary>
public class OsReleaseTests
{
    [Fact]
    public void Strips_quotes_and_keeps_the_caption_intact()
    {
        var map = SystemInfo.ParseOsRelease(new[]
        {
            "NAME=\"Ubuntu\"",
            "VERSION=\"24.04.2 LTS (Noble Numbat)\"",
            "ID=ubuntu",
            "PRETTY_NAME=\"Ubuntu 24.04.2 LTS\"",
            "VERSION_ID=\"24.04\"",
        });

        Assert.Equal("Ubuntu 24.04.2 LTS", map["PRETTY_NAME"]);
        Assert.Equal("24.04", map["VERSION_ID"]);
        Assert.Equal("ubuntu", map["ID"]);
    }

    [Fact]
    public void Handles_unquoted_values_and_single_quotes()
    {
        var map = SystemInfo.ParseOsRelease(new[]
        {
            "ID=arch",
            "PRETTY_NAME='Arch Linux'",
            "VERSION_ID=20260901.0.0",
        });

        Assert.Equal("arch", map["ID"]);
        Assert.Equal("Arch Linux", map["PRETTY_NAME"]);
        Assert.Equal("20260901.0.0", map["VERSION_ID"]);
    }

    [Fact]
    public void Ignores_comments_blanks_and_malformed_lines()
    {
        var map = SystemInfo.ParseOsRelease(new[]
        {
            "# a comment",
            "",
            "   ",
            "NOTAKEYVALUE",
            "=novalue",
            "ID=debian",
        });

        Assert.Single(map);
        Assert.Equal("debian", map["ID"]);
    }

    [Fact]
    public void Unquotes_an_escaped_quote_inside_a_value()
    {
        var map = SystemInfo.ParseOsRelease(new[] { "PRETTY_NAME=\"He said \\\"hi\\\"\"" });
        Assert.Equal("He said \"hi\"", map["PRETTY_NAME"]);
    }
}

/// <summary>Which CPU thermal label wins.
///
/// It decides the single number the whole fleet is ranked by on the Dashboard's "hottest"
/// list. Two identical machines that picked different sensors -- one the package, one core #3
/// -- would disagree by several degrees for a reason nobody could see from the console.</summary>
public class PackageLabelTests
{
    [Theory]
    [InlineData("Package id 0")]
    [InlineData("Tctl")]
    [InlineData("Tdie")]
    [InlineData("Composite")]      // NVMe's whole-device sensor, same idea
    [InlineData("PACKAGE ID 0")]
    public void Package_wide_labels_win(string label) =>
        Assert.True(ProcSensorReader.IsPackageLabel(label));

    [Theory]
    [InlineData("Core 0")]
    [InlineData("Core 11")]
    [InlineData("temp1")]
    [InlineData("")]
    [InlineData(null)]
    public void Per_core_and_unnamed_labels_do_not(string? label) =>
        Assert.False(ProcSensorReader.IsPackageLabel(label));
}
