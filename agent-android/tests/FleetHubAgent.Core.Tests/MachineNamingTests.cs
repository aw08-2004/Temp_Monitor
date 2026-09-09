namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Catches the failure that would silently merge a fleet of identical devices into one machine
/// in the console.
///
/// The hub keys a machine on the name it reports. Android has no hostname, so if the derived
/// name were the model alone, thirty identical tablets would all report "Pixel Tablet" and the
/// hub would treat them as one machine being written to thirty times a second -- one healthy
/// row in the console, twenty-nine devices invisible, and no error anywhere. These tests hold
/// the properties that stop that: the suffix is always present, it comes from the END of the
/// stable id, and two devices of the same model never derive the same name.
/// </summary>
public class MachineNamingTests
{
    [Fact]
    public void Two_devices_of_the_same_model_get_different_names()
    {
        var a = MachineNaming.Derive("Pixel Tablet", "Google", "9f8e7d6c5b4a3210");
        var b = MachineNaming.Derive("Pixel Tablet", "Google", "0123456789abcdef");
        Assert.NotEqual(a, b);
    }

    [Fact]
    public void Derived_name_carries_the_model_and_a_suffix()
    {
        Assert.Equal("Pixel-Tablet-5b4a3210",
            MachineNaming.Derive("Pixel Tablet", "Google", "9f8e7d6c5b4a3210"));
    }

    [Fact]
    public void Suffix_comes_from_the_end_of_the_stable_id()
    {
        // A purchase batch of MDM-provisioned devices can share a long fixed PREFIX and differ
        // only in the tail. Taking the head would collapse the batch into one machine -- the
        // exact failure this file exists for.
        var a = MachineNaming.Derive("Tab", "Vendor", "ACME-FLEET-2026-0001");
        var b = MachineNaming.Derive("Tab", "Vendor", "ACME-FLEET-2026-0002");
        Assert.NotEqual(a, b);
    }

    [Fact]
    public void Missing_stable_id_produces_an_obviously_wrong_name_rather_than_a_colliding_one()
    {
        // Deliberately ugly. A name that looks right and merges two devices is far worse than
        // one that looks broken in the console -- see MachineNaming.Derive.
        var name = MachineNaming.Derive("Pixel Tablet", "Google", null);
        Assert.Contains("unknown-device", name);
    }

    [Fact]
    public void Blank_model_falls_back_to_manufacturer_then_to_a_literal()
    {
        Assert.StartsWith("Google-", MachineNaming.Derive("", "Google", "abcdef1234567890"));
        Assert.StartsWith("android-device-", MachineNaming.Derive(null, null, "abcdef1234567890"));
    }

    [Theory]
    [InlineData("Pixel 9 Pro", "Pixel-9-Pro")]
    [InlineData("  SM-A546B/DS  ", "SM-A546B-DS")]
    [InlineData("Galaxy   Tab", "Galaxy-Tab")]
    [InlineData("---", "")]
    public void Sanitize_collapses_punctuation_to_single_hyphens(string input, string expected)
    {
        Assert.Equal(expected, MachineNaming.Sanitize(input));
    }

    [Fact]
    public void Sanitize_drops_non_ascii_rather_than_transliterating_it()
    {
        // A Latin-1 fold would turn some vendor names into a different word and leave others
        // alone, and this string becomes a machine's identity.
        Assert.Equal("Xiaomi", MachineNaming.Sanitize("Xiaomi小米"));
    }

    [Fact]
    public void Derived_name_never_exceeds_the_hubs_ceiling()
    {
        // Over MACHINE_NAME_MAX_CHARS the hub answers /api/report with a 400 and the device
        // reports nothing at all, forever, with no local symptom.
        var name = MachineNaming.Derive(new string('M', 400), "Vendor", "abcdef1234567890");
        Assert.True(name.Length <= MachineNaming.MaxChars, $"derived {name.Length} chars");
        Assert.True(MachineNaming.IsValid(name));
    }

    [Theory]
    [InlineData("workshop-tablet-3")]
    [InlineData("A")]
    public void IsValid_accepts_ordinary_names(string name) => Assert.True(MachineNaming.IsValid(name));

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    [InlineData("tab<script>")]
    [InlineData("say \"hello\"")]
    [InlineData("it's")]
    [InlineData("a&b")]
    [InlineData("bel\u0007l")]  // a control character the hub's own regex forbids too
    [InlineData("café")]      // non-ASCII: two names that read alike must not both exist
    public void IsValid_rejects_what_the_hub_or_an_operator_could_not_live_with(string? name) =>
        Assert.False(MachineNaming.IsValid(name));

    [Fact]
    public void IsValid_rejects_a_name_over_the_hubs_ceiling() =>
        Assert.False(MachineNaming.IsValid(new string('x', MachineNaming.MaxChars + 1)));
}
