using System.Text.Json.Nodes;
using TempMonitorAgent.Bios;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The firmware reader's decision tree and each vendor's parsing, against fake WMI rows
/// (roadmap #9).
///
/// **This is the only way these can be tested at all.** A build machine is never
/// simultaneously a Dell, an HP and a Lenovo, so `root\dcim\sysman`, `root\hp\instrumentedBIOS`
/// and Lenovo's `root\wmi` classes are all absent from it -- and two of the three would be
/// absent from any real machine too. What is injected here is exactly what the WMI adapter
/// yields: property bags keyed case-insensitively, with the types those classes actually use
/// (a bool here, a uint there, a string[] somewhere else).
///
/// What this cannot cover is the adapter itself: whether `root\hp\instrumentedBIOS` really
/// throws InvalidNamespace on a Dell, and whether the property names below match the ones a
/// real machine emits. Expect first-contact findings there, the same shape the LDAP and TURN
/// work produced.
/// </summary>
public class BiosReaderTests
{
    private static IReadOnlyDictionary<string, object?> Row(params (string, object?)[] pairs)
    {
        var row = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
        foreach (var (key, value) in pairs) row[key] = value;
        return row;
    }

    /// <summary>A WmiQuery answering from a class-name -> rows table; every other class is
    /// empty, exactly as the real adapter yields for a class a machine does not have.</summary>
    private static WmiQuery Fake(Dictionary<string, List<IReadOnlyDictionary<string, object?>>> byClass)
        => (ns, wql) =>
        {
            foreach (var kv in byClass)
                if (wql.Contains(kv.Key, StringComparison.OrdinalIgnoreCase))
                    return kv.Value;
            return Array.Empty<IReadOnlyDictionary<string, object?>>();
        };

    // ------------------------------------------------------------------ dispatch

    [Fact]
    public void A_machine_with_no_manufacturer_is_unsupported_not_an_error()
    {
        // The whitebox case: SystemInfo filters "System manufacturer" to nothing precisely so
        // it cannot be dispatched on. Reporting an error here would light up every self-built
        // PC in the fleet, permanently, with nothing anyone could do about it.
        var report = BiosReader.Read("", "1.2.3", Fake(new()));
        Assert.Equal(BiosSupport.Unsupported, report.Support);
        Assert.Equal("1.2.3", report.BiosVersion);
    }

    [Fact]
    public void An_unknown_vendor_is_unsupported()
    {
        // A VM. This is the steady state for a large part of many fleets, so it must be the
        // quiet outcome, not the loud one.
        var report = BiosReader.Read("VMware, Inc.", "", Fake(new()));
        Assert.Equal(BiosSupport.Unsupported, report.Support);
        Assert.Contains("VMware", report.Error);
    }

    [Fact]
    public void A_missing_namespace_is_unsupported_but_any_other_failure_is_an_error()
    {
        // The distinction the whole feature turns on: "this vendor's management stack was
        // never installed" is not "this machine is broken".
        var missing = BiosReader.Read("Dell Inc.", "", (ns, wql) =>
            throw new BiosInterfaceMissingException($"{ns} is not present"));
        Assert.Equal(BiosSupport.Unsupported, missing.Support);

        var broken = BiosReader.Read("Dell Inc.", "", (ns, wql) =>
            throw new InvalidOperationException("access denied"));
        Assert.Equal(BiosSupport.Error, broken.Support);
        Assert.Equal("Dell", broken.Vendor);
        Assert.Contains("access denied", broken.Error);
    }

    [Fact]
    public void A_namespace_that_returns_nothing_is_an_error_not_unsupported()
    {
        // The interface exists and answered with nothing -- a permissions problem or a broken
        // WMI repository. Calling that "unsupported" files it under the state nobody ever
        // investigates.
        var report = BiosReader.Read("LENOVO", "", Fake(new()));
        Assert.Equal(BiosSupport.Error, report.Support);
    }

    // ------------------------------------------------------------------ Dell

    [Fact]
    public void Dell_reads_all_three_attribute_classes_and_the_password_flag()
    {
        var report = BiosReader.Read("Dell Inc.", "1.29.0", Fake(new()
        {
            ["DCIM_BIOSEnumeration"] = new()
            {
                Row(("AttributeName", "WakeOnLan"), ("CurrentValue", new string[] { "LanOnly" }),
                    ("PossibleValues", new string[] { "Disabled", "LanOnly", "LanWlan" }),
                    ("IsReadOnly", false), ("AttributeDisplayName", "Wake on LAN")),
                // IsReadOnly arrives as a number on some models; Row.Flag must read both.
                Row(("AttributeName", "SecureBoot"), ("CurrentValue", "Enabled"),
                    ("PossibleValues", new string[] { "Enabled", "Disabled" }), ("IsReadOnly", 1u)),
            },
            ["DCIM_BIOSString"] = new()
            {
                Row(("AttributeName", "Asset"), ("CurrentValue", "FIN-0042")),
            },
            ["DCIM_BIOSInteger"] = new()
            {
                Row(("AttributeName", "AutoOnHr"), ("CurrentValue", 7)),
            },
            ["DCIM_BIOSPassword"] = new()
            {
                Row(("AttributeName", "AdminPwd"), ("IsSet", false)),
                Row(("AttributeName", "SysPwd"), ("IsSet", true)),
            },
        }));

        Assert.Equal(BiosSupport.Supported, report.Support);
        Assert.Equal("Dell", report.Vendor);
        Assert.Equal("1.29.0", report.BiosVersion);
        Assert.Equal(4, report.Items.Count);

        var wol = report.Items.Single(s => s.Name == "WakeOnLan");
        Assert.Equal("LanOnly", wol.Value);
        Assert.Equal(BiosSettingKind.Enum, wol.Kind);
        Assert.Equal(3, wol.PossibleValues.Count);
        Assert.False(wol.ReadOnly);
        Assert.Equal("Wake on LAN", wol.DisplayName);

        Assert.True(report.Items.Single(s => s.Name == "SecureBoot").ReadOnly);
        Assert.Equal(BiosSettingKind.String, report.Items.Single(s => s.Name == "Asset").Kind);
        Assert.Equal(BiosSettingKind.Integer, report.Items.Single(s => s.Name == "AutoOnHr").Kind);
        // Any set password blocks a write, so one true among several is "set".
        Assert.True(report.PasswordSet);
    }

    [Fact]
    public void An_attribute_with_no_name_is_dropped()
    {
        // The name is the identity a future set_bios_settings writes against. A nameless row
        // shown in the console is a row nobody could ever act on.
        var report = BiosReader.Read("Dell Inc.", "", Fake(new()
        {
            ["DCIM_BIOSEnumeration"] = new()
            {
                Row(("AttributeName", ""), ("CurrentValue", "x")),
                Row(("AttributeName", "Real"), ("CurrentValue", "y")),
            },
        }));
        Assert.Single(report.Items);
        Assert.Equal("Real", report.Items[0].Name);
    }

    // ------------------------------------------------------------------ HP

    [Fact]
    public void Hp_uses_Name_rather_than_AttributeName()
    {
        // Exactly the difference an alias layer would paper over and then get wrong.
        var report = BiosReader.Read("HP", "", Fake(new()
        {
            ["HP_BIOSEnumeration"] = new()
            {
                Row(("Name", "Wake On LAN"), ("CurrentValue", "Boot to Hard Drive"),
                    ("PossibleValues", new string[] { "Disabled", "Boot to Hard Drive" }),
                    ("IsReadOnly", 0u), ("DisplayName", "Wake On LAN")),
            },
        }));
        Assert.Equal(BiosSupport.Supported, report.Support);
        Assert.Equal("HP", report.Vendor);
        Assert.Equal("Wake On LAN", report.Items[0].Name);
        Assert.False(report.Items[0].ReadOnly);
        // No password class on this machine -> null, NOT false. "There is no password" and
        // "we could not find out" lead to different advice.
        Assert.Null(report.PasswordSet);
    }

    [Fact]
    public void Hewlett_Packard_matches_HP()
    {
        Assert.True(new HpBiosSource().Matches("Hewlett-Packard"));
        Assert.True(new HpBiosSource().Matches("HP Inc."));
        Assert.False(new HpBiosSource().Matches("Dell Inc."));
    }

    // ------------------------------------------------------------------ Lenovo

    [Theory]
    // The documented shape: name, value, then the selectable list.
    [InlineData("WakeOnLAN,Enable;[Enable,Disable,ACOnly]", "WakeOnLAN", "Enable", 3)]
    // Some firmware omits the brackets.
    [InlineData("SecureBoot,Enabled;Enabled,Disabled", "SecureBoot", "Enabled", 2)]
    // And some reports no options at all -- still a readable setting.
    [InlineData("BootMode,UEFI", "BootMode", "UEFI", 0)]
    // Trailing sections after a second ';' are not ours to interpret.
    [InlineData("USBPort,Enable;[Enable,Disable];rw", "USBPort", "Enable", 2)]
    public void Lenovo_parses_its_one_string_format(string raw, string name, string value,
                                                    int optionCount)
    {
        var parsed = LenovoBiosSource.ParseCurrentSetting(raw);
        Assert.NotNull(parsed);
        Assert.Equal(name, parsed!.Name);
        Assert.Equal(value, parsed.Value);
        Assert.Equal(optionCount, parsed.PossibleValues.Count);
        // No option list means we were not told it is an enumeration -- Unknown rather than a
        // guess, so the console never offers a dropdown built from nothing.
        Assert.Equal(optionCount > 0 ? BiosSettingKind.Enum : BiosSettingKind.Unknown, parsed.Kind);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("NameWithNoValue")]
    [InlineData(",orphanvalue")]
    public void Lenovo_drops_a_setting_it_cannot_name_and_value(string raw)
    {
        Assert.Null(LenovoBiosSource.ParseCurrentSetting(raw));
    }

    // ------------------------------------------------------------------ wire payload

    [Fact]
    public void The_payload_is_the_shape_the_hub_ingests()
    {
        // This object is the entire contract between a C# reader and a Python ingest. The
        // hub's own tests assert the other side of it against these same field names.
        var report = new BiosReport(
            BiosSupport.Supported, "Dell", @"root\dcim\sysman", "1.29.0", PasswordSet: true,
            Settings: new[]
            {
                new BiosSetting("WakeOnLan", "LanOnly", BiosSettingKind.Enum,
                                new[] { "Disabled", "LanOnly" }, false, "Wake on LAN"),
            });

        var payload = BiosInventoryReporter.ToPayload(report);
        Assert.Equal("supported", (string?)payload["support"]);
        Assert.Equal("Dell", (string?)payload["vendor"]);
        Assert.Equal(@"root\dcim\sysman", (string?)payload["interface"]);
        Assert.Equal("1.29.0", (string?)payload["bios_version"]);
        Assert.True((bool?)payload["password_set"]);

        var settings = payload["settings"]!.AsArray();
        var first = settings[0]!.AsObject();
        Assert.Equal("WakeOnLan", (string?)first["name"]);
        Assert.Equal("LanOnly", (string?)first["value"]);
        Assert.Equal("enum", (string?)first["kind"]);
        Assert.Equal("Wake on LAN", (string?)first["display_name"]);
        Assert.False((bool?)first["read_only"]);
        Assert.Equal(2, first["possible_values"]!.AsArray().Count);
    }

    [Fact]
    public void An_unknown_password_state_serialises_as_null_not_false()
    {
        var payload = BiosInventoryReporter.ToPayload(
            new BiosReport(BiosSupport.Unsupported, Error: "no firmware interface"));
        Assert.Equal("unsupported", (string?)payload["support"]);
        Assert.True(payload["password_set"] is null || payload["password_set"] is JsonValue v
                    && v.TryGetValue<bool?>(out var b) && b is null);
        Assert.Empty(payload["settings"]!.AsArray());
    }
}
