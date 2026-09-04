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

    /// <summary>A WmiQuery over a NAMESPACE -> class -> rows table. A namespace missing from
    /// the table throws BiosInterfaceMissingException, exactly as the real adapter does for one
    /// that is not present on the machine. <see cref="Fake"/> above cannot express that: it
    /// answers every namespace alike, which was fine while each vendor had one -- and is
    /// precisely the assumption that let Dell's second interface go unnoticed.</summary>
    private static WmiQuery FakeNamespaces(
        Dictionary<string, Dictionary<string, List<IReadOnlyDictionary<string, object?>>>> byNamespace)
        => (ns, wql) =>
        {
            if (!byNamespace.TryGetValue(ns, out var classes))
                throw new BiosInterfaceMissingException($"{ns} is not present");
            foreach (var kv in classes)
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

    [Fact]
    public void A_class_this_model_does_not_carry_is_caught_while_the_rows_are_read()
    {
        // Regression guard for a Dell that reported "Could not read: Clase no válida".
        //
        // A WMI query does not execute at searcher.Get() -- that returns a collection which
        // runs on first MoveNext. While Query was an iterator method, a missing class threw
        // during the CALLER's foreach, outside the try/catch, so the tolerance for "this model
        // has no DCIM_BIOSPassword" never applied and a perfectly readable machine came back
        // as an error.
        //
        // Nothing here can call WMI, so what is asserted is the property that makes the bug
        // impossible: Query hands back a materialised list, which means every row was read
        // inside the method that knows which failures are ordinary.
        var returnType = typeof(BiosReader).GetMethod(nameof(BiosReader.Query))!.ReturnType;
        Assert.True(returnType.IsGenericType
                    && returnType.GetGenericTypeDefinition() == typeof(IReadOnlyList<>),
                    "Query must materialise its rows; a lazy IEnumerable puts the enumeration "
                    + "-- and therefore the InvalidClass -- outside its own try/catch.");
    }

    [Fact]
    public void A_wmi_failure_carries_its_locale_independent_error_code()
    {
        // WMI messages come from Windows on the reporting machine, in that machine's language:
        // a Spanish "Clase no válida" arriving in an English console is normal, not a bug. The
        // ErrorCode is the part an operator in another language can still act on and search
        // for, so it is prefixed rather than left to the prose.
        var described = BiosReader.Describe(
            new System.Management.ManagementException("Clase no válida"));
        Assert.Contains("Clase no válida", described);
        Assert.Contains(":", described);
        Assert.DoesNotContain(":", described[..described.IndexOf(':')]);   // a code, not prose

        // Anything that is not a WMI fault keeps its own message unchanged.
        Assert.Equal("access denied", BiosReader.Describe(new InvalidOperationException("access denied")));
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

    [Fact]
    public void Dell_reads_the_stock_image_provider_before_Command_Monitor()
    {
        // THE REGRESSION. root\dcim\sysman is present on a Dell that has never had Command |
        // Monitor -- it connects, and only its DCIM_BIOS* classes are missing. A reader that
        // tested the namespace and stopped therefore called that machine "supported", found
        // nothing in three queries, and reported
        // `error: the firmware interface returned no settings` -- on hardware whose whole
        // attribute list was readable one namespace over. Every un-managed Dell in the fleet
        // showed that, permanently, and the message named the wrong namespace.
        var report = BiosReader.Read("Dell Inc.", "1.39.0", FakeNamespaces(new()
        {
            // The Command | Monitor shell, present and empty -- the trap.
            [@"root\dcim\sysman"] = new(),
            [@"root\dcim\sysman\biosattributes"] = new()
            {
                ["EnumerationAttribute"] = new()
                {
                    // Not one property name in common with DCIM_BIOSEnumeration:
                    // PossibleValue (singular), ReadOnly (a uint), DisplayName.
                    Row(("AttributeName", "WakeOnLan"), ("CurrentValue", "LanWlan"),
                        ("PossibleValue", new string[] { "Disabled", "LanOnly", "LanWlan" }),
                        ("ReadOnly", 0u), ("DisplayName", "Wake on LAN")),
                    Row(("AttributeName", "SecureBoot"), ("CurrentValue", "Enabled"),
                        ("PossibleValue", new string[] { "Enabled", "Disabled" }),
                        ("ReadOnly", 1u)),
                },
                ["StringAttribute"] = new()
                {
                    Row(("AttributeName", "Asset"), ("CurrentValue", "FIN-0042"),
                        ("ReadOnly", 0u)),
                },
                ["IntegerAttribute"] = new()
                {
                    Row(("AttributeName", "AutoOnHr"), ("CurrentValue", 7u), ("ReadOnly", 0u)),
                },
            },
            [@"root\dcim\sysman\wmisecurity"] = new()
            {
                ["PasswordObject"] = new()
                {
                    Row(("NameId", "Admin"), ("IsPasswordSet", 0u)),
                    Row(("NameId", "System"), ("IsPasswordSet", 1u)),
                },
            },
        }));

        Assert.Equal(BiosSupport.Supported, report.Support);
        Assert.Equal(4, report.Items.Count);

        var wol = report.Items.Single(s => s.Name == "WakeOnLan");
        Assert.Equal("LanWlan", wol.Value);
        Assert.Equal(3, wol.PossibleValues.Count);
        Assert.False(wol.ReadOnly);
        Assert.Equal("Wake on LAN", wol.DisplayName);
        // ReadOnly is a uint here where Command | Monitor sends a bool. Both must read.
        Assert.True(report.Items.Single(s => s.Name == "SecureBoot").ReadOnly);
        Assert.Equal(BiosSettingKind.Integer, report.Items.Single(s => s.Name == "AutoOnHr").Kind);

        // The password lives in a THIRD namespace on this provider, and any one being set
        // blocks a write.
        Assert.True(report.PasswordSet);

        // The interface REPORTED is the one that answered, not the vendor's default. An
        // operator chasing a firmware fault is sent to the namespace that was actually read.
        Assert.Equal(@"root\dcim\sysman\biosattributes", report.Interface);
    }

    [Fact]
    public void A_Dell_with_only_Command_Monitor_still_reads_through_the_fallback()
    {
        // The other half of the same fix: preferring the stock provider must not drop the
        // fleets that have DCM and nothing else. The namespace it prefers is absent here.
        var report = BiosReader.Read("Dell Inc.", "", FakeNamespaces(new()
        {
            [@"root\dcim\sysman"] = new()
            {
                ["DCIM_BIOSEnumeration"] = new()
                {
                    Row(("AttributeName", "WakeOnLan"), ("CurrentValue", "LanOnly"),
                        ("PossibleValues", new string[] { "Disabled", "LanOnly" }),
                        ("IsReadOnly", false)),
                },
            },
        }));

        Assert.Equal(BiosSupport.Supported, report.Support);
        Assert.Equal("WakeOnLan", report.Items.Single().Name);
        Assert.Equal(@"root\dcim\sysman", report.Interface);
    }

    [Fact]
    public void A_Dell_with_neither_interface_is_unsupported_but_one_that_is_empty_is_an_error()
    {
        // The distinction the whole feature turns on, now that Dell has two namespaces: it is
        // unsupported only when NEITHER is there. One present and enumerating nothing is still
        // a fault someone should look at.
        var neither = BiosReader.Read("Dell Inc.", "", FakeNamespaces(new()));
        Assert.Equal(BiosSupport.Unsupported, neither.Support);

        var empty = BiosReader.Read("Dell Inc.", "", FakeNamespaces(new()
        {
            [@"root\dcim\sysman\biosattributes"] = new(),
        }));
        Assert.Equal(BiosSupport.Error, empty.Support);
        Assert.Contains("no settings", empty.Error);
        // ...and it names the namespace that failed to answer, not the one never consulted.
        Assert.Equal(@"root\dcim\sysman\biosattributes", empty.Interface);
    }

    [Fact]
    public void A_missing_security_namespace_costs_the_password_state_not_the_settings()
    {
        // The password lives in a namespace of its own on this provider, so it can be absent
        // while the attributes are perfectly readable. Null, not false: "there is no password"
        // and "we could not find out" lead to different advice the moment a write is refused.
        var report = BiosReader.Read("Dell Inc.", "", FakeNamespaces(new()
        {
            [@"root\dcim\sysman\biosattributes"] = new()
            {
                ["EnumerationAttribute"] = new()
                {
                    Row(("AttributeName", "WakeOnLan"), ("CurrentValue", "LanOnly")),
                },
            },
        }));
        Assert.Equal(BiosSupport.Supported, report.Support);
        Assert.Single(report.Items);
        Assert.Null(report.PasswordSet);
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
