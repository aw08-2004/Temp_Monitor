using System.Text.Json.Nodes;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The parts of the virtual-display feature that do not need a driver, a device tree, or
/// Administrator: turning operator input into settings, rendering the driver's settings file,
/// and reading pnputil's output.
///
/// That last one is worth pinning even though it looks trivial. The published <c>oem##.inf</c>
/// name is reported exactly once, by pnputil at install time, and cannot be recovered
/// afterwards -- if the parse is wrong, the failure surfaces much later as an uninstall that
/// silently leaves the driver package in the DriverStore.
/// </summary>
public class VirtualDisplayTests
{
    private static JsonObject Params(string json) => (JsonObject)JsonNode.Parse(json)!;

    [Fact]
    public void Parse_EmptyParams_YieldsTheHeadlessDefault()
    {
        var settings = VirtualDisplaySettingsParser.Parse(Params("{}"));
        Assert.Equal(1, settings.MonitorCount);
        Assert.Equal(new[] { new VddMode(1920, 1080, 60) }, settings.Modes);
        Assert.False(settings.AllowArm64);
    }

    [Fact]
    public void Parse_ReadsMonitorsAndResolutions()
    {
        var settings = VirtualDisplaySettingsParser.Parse(Params("""
            {"monitors": 2, "resolutions": [
                {"width": 2560, "height": 1440, "hz": 60},
                {"width": 1280, "height": 720, "hz": 30}]}
            """));
        Assert.Equal(2, settings.MonitorCount);
        Assert.Equal(new[] { new VddMode(2560, 1440, 60), new VddMode(1280, 720, 30) },
                     settings.Modes);
    }

    [Fact]
    public void Parse_MonitorCountIsClampedToTheDriversRange()
    {
        Assert.Equal(8, VirtualDisplaySettingsParser.Parse(Params("""{"monitors": 99}""")).MonitorCount);
        Assert.Equal(0, VirtualDisplaySettingsParser.Parse(Params("""{"monitors": -3}""")).MonitorCount);
    }

    [Fact]
    public void Parse_ZeroMonitorsIsKept_ItIsTheStandDownPath()
    {
        // A real monitor was plugged in; the driver stays installed but stops adding a phantom
        // display. Coercing this to 1 would make that impossible without an uninstall + reboot.
        Assert.Equal(0, VirtualDisplaySettingsParser.Parse(Params("""{"monitors": 0}""")).MonitorCount);
    }

    [Fact]
    public void Parse_SkipsOutOfRangeModesButKeepsTheGoodOnes()
    {
        var settings = VirtualDisplaySettingsParser.Parse(Params("""
            {"resolutions": [
                {"width": 100, "height": 100},
                {"width": 99999, "height": 4320},
                {"width": 1920, "height": 1080, "hz": 75}]}
            """));
        Assert.Equal(new[] { new VddMode(1920, 1080, 75) }, settings.Modes);
    }

    [Fact]
    public void Parse_FallsBackToTheDefaultWhenEveryModeIsRejected()
    {
        var settings = VirtualDisplaySettingsParser.Parse(Params("""
            {"resolutions": [{"width": 1, "height": 1}]}
            """));
        Assert.Equal(VddSettings.Default.Modes, settings.Modes);
    }

    [Fact]
    public void ToXml_CarriesTheCountAndEveryMode()
    {
        var xml = new VddSettings(2, new[] { new VddMode(2560, 1440, 60), new VddMode(800, 600, 30) })
            .ToXml();
        Assert.Contains("<count>2</count>", xml);
        Assert.Contains("<width>2560</width>", xml);
        Assert.Contains("<height>1440</height>", xml);
        Assert.Contains("<refresh_rate>30</refresh_rate>", xml);
    }

    [Theory]
    [InlineData("Published Name: oem12.inf", "oem12.inf")]
    [InlineData("Driver package added successfully.\r\nPublished Name:    oem3.inf\r\n", "oem3.inf")]
    // Localised Windows renders the label differently; the trailing .inf is the reliable part.
    [InlineData("Veroeffentlichter Name: oem7.inf", "oem7.inf")]
    public void ParsePublishedName_FindsTheDriverStoreName(string output, string expected)
    {
        Assert.Equal(expected, VirtualDisplayInstaller.ParsePublishedName(output));
    }

    [Theory]
    [InlineData("Driver package added successfully.")]
    [InlineData("")]
    [InlineData("Published Name: something-else")]
    public void ParsePublishedName_ReturnsNullWhenPnputilDidNotReportOne(string output)
    {
        Assert.Null(VirtualDisplayInstaller.ParsePublishedName(output));
    }
}
