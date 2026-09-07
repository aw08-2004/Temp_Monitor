using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Catches the identity value that is shared across unrelated devices and would therefore
/// merge them into one machine.
///
/// The hub merges two machines reporting the same serial_number. That is a feature -- it is
/// what makes a rename survivable -- and it is the reason a value shared by a thousand devices
/// is so much worse here than a missing one. A null serial costs a device the ability to be
/// renamed safely. A CONSTANT serial costs the fleet every device that reports it, silently,
/// by collapsing them into a single row.
/// </summary>
public class IdentityCleaningTests
{
    [Fact]
    public void The_known_bad_SSAID_is_rejected()
    {
        // A long-standing Android defect returned this as the SSAID on a large number of
        // devices. Sending it would merge every affected device in the fleet into one machine.
        Assert.Null(IdentityCleaning.CleanStableId("9774d56d682e549c"));
    }

    [Theory]
    [InlineData("0000000000000000")]
    [InlineData("ffffffffffffffff")]
    [InlineData("1234567890abcdef")]
    [InlineData("unknown")]
    public void Factory_and_emulator_placeholders_are_rejected(string value) =>
        Assert.Null(IdentityCleaning.CleanStableId(value));

    [Theory]
    [InlineData("aaaaaaaaaaaaaaaa")]  // one repeated character
    [InlineData("0000000")]           // too short to be unique
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    public void Values_that_cannot_identify_a_device_are_rejected(string? value) =>
        Assert.Null(IdentityCleaning.CleanStableId(value));

    [Fact]
    public void A_real_looking_SSAID_survives()
    {
        Assert.Equal("9f8e7d6c5b4a3210", IdentityCleaning.CleanStableId("9f8e7d6c5b4a3210"));
    }

    [Fact]
    public void A_stable_id_is_trimmed_rather_than_rejected_for_whitespace()
    {
        Assert.Equal("9f8e7d6c5b4a3210", IdentityCleaning.CleanStableId("  9f8e7d6c5b4a3210 "));
    }

    [Theory]
    [InlineData("To Be Filled By O.E.M.")]
    [InlineData("Default string")]
    [InlineData("unknown")]
    [InlineData("N/A")]
    [InlineData("00000000")]
    [InlineData("--")]
    [InlineData("")]
    [InlineData(null)]
    public void Vendor_placeholders_are_rejected(string? value) =>
        Assert.Null(IdentityCleaning.Clean(value));

    [Fact]
    public void An_ordinary_vendor_string_survives()
    {
        Assert.Equal("Google", IdentityCleaning.Clean("  Google  "));
    }
}
