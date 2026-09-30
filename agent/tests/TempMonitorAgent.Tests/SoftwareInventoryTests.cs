using TempMonitorAgent.Software;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The halves of the installed-software inventory (roadmap #25 B) that can be tested without a
/// real registry: the Programs-and-Features filter and the wire payload the hub parses.
///
/// **The wire payload is a contract with Python.** `tests/test_software.py` and
/// `tests/test_reports.py` assert the same field names from the other side; drift between them
/// is not a crash but a device sheet whose Software section never fills in -- which, on paper,
/// reads as a PC with nothing installed.
///
/// **An empty list is asserted as a present, empty array.** The hub stores "reported nothing"
/// distinctly from "never reported", and the object wrapper is what keeps an empty report
/// truthy all the way to <c>software.record_inventory</c>.
///
/// **What this cannot cover** is <c>SoftwareReader.Read()</c> against a real machine: whether
/// HKU enumeration from LocalSystem sees every signed-in profile, what a roaming or
/// FSLogix-mounted profile looks like there, and how long the walk takes on a machine with
/// thousands of MSI patches registered. Expect first-contact findings.
/// </summary>
public class SoftwareInventoryTests
{
    private static Func<string, object?> Values(params (string Key, object Value)[] pairs)
    {
        var map = pairs.ToDictionary(p => p.Key, p => p.Value);
        return key => map.TryGetValue(key, out var value) ? value : null;
    }

    [Fact]
    public void AnOrdinaryProgramIsListedWithEveryField()
    {
        var entry = SoftwareReader.FromValues("HKLM64:{7ZIP}", SoftwareReader.ScopeMachine, "", "x64",
            Values(("DisplayName", " 7-Zip 23.01 "), ("DisplayVersion", "23.01"),
                   ("Publisher", "Igor Pavlov"), ("InstallDate", "20240102"),
                   ("InstallLocation", @"C:\Program Files\7-Zip\"),
                   ("UninstallString", @"C:\Program Files\7-Zip\Uninstall.exe")));

        Assert.NotNull(entry);
        Assert.Equal("7-Zip 23.01", entry!.Name);
        Assert.Equal("23.01", entry.Version);
        Assert.Equal("x64", entry.Arch);
        Assert.Equal("", entry.UserSid);
    }

    [Theory]
    [InlineData("SystemComponent")]
    [InlineData("ParentKeyName")]
    [InlineData("ReleaseType")]
    [InlineData("NoName")]
    public void WhatProgramsAndFeaturesHidesIsHiddenHereToo(string reason)
    {
        var pairs = new List<(string, object)> { ("DisplayName", "Something") };
        switch (reason)
        {
            case "SystemComponent": pairs.Add(("SystemComponent", 1)); break;
            case "ParentKeyName": pairs.Add(("ParentKeyName", "OfficeProfessionalPlus")); break;
            case "ReleaseType": pairs.Add(("ReleaseType", "Security Update")); break;
            case "NoName": pairs[0] = ("DisplayName", "   "); break;
        }
        Assert.Null(SoftwareReader.FromValues("HKLM64:x", SoftwareReader.ScopeMachine, "", "x64",
                                              Values(pairs.ToArray())));
    }

    [Fact]
    public void APerUserEntryCarriesItsSidAndNoArch()
    {
        var entry = SoftwareReader.FromValues("HKU:S-1-5-21-1:Zoom", SoftwareReader.ScopeUser,
            "S-1-5-21-1", "x64", Values(("DisplayName", "Zoom")));
        Assert.Equal("user", entry!.Scope);
        Assert.Equal("S-1-5-21-1", entry.UserSid);
        Assert.Equal("", entry.Arch);
    }

    [Fact]
    public void ADwordVersionIsText()
    {
        var entry = SoftwareReader.FromValues("HKLM32:x", SoftwareReader.ScopeMachine, "", "x86",
            Values(("DisplayName", "Old Tool"), ("DisplayVersion", 3)));
        Assert.Equal("3", entry!.Version);
    }

    [Theory]
    [InlineData("S-1-5-21-111-222-333-1001", true)]
    [InlineData("S-1-5-21-111-222-333-1001_Classes", false)]
    [InlineData("S-1-5-18", false)]
    [InlineData(".DEFAULT", false)]
    public void OnlyRealProfileHivesAreRead(string name, bool expected) =>
        Assert.Equal(expected, SoftwareReader.IsProfileSid(name));

    [Fact]
    public void PayloadCarriesTheFieldNamesTheHubIngestReads()
    {
        var payload = SoftwareInventoryReporter.ToPayload(new SoftwareReport("", [
            new InstalledSoftware("HKLM64:{A}", "App", "1.0", "Acme", "20240101", @"C:\A",
                                  @"C:\A\u.exe", "machine", "", "x64"),
        ]));
        var item = payload["software"]!.AsArray()[0]!;
        foreach (var field in new[] { "id", "name", "version", "publisher", "install_date",
                                      "install_location", "uninstall_string", "scope",
                                      "user_sid", "arch" })
        {
            Assert.NotNull(item[field]);
        }
        Assert.Equal("", (string?)payload["error"]);
    }

    [Fact]
    public void AnEmptyInventoryIsStillAnObjectWithAnEmptyList()
    {
        var payload = SoftwareInventoryReporter.ToPayload(new SoftwareReport("", []));
        Assert.NotNull(payload["software"]);
        Assert.Empty(payload["software"]!.AsArray());
    }
}
