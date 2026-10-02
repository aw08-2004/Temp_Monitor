using System.Text.Json.Nodes;
using TempMonitorAgent.Security;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The halves of the security posture reporter (roadmap #25 D) that can be tested without a
/// real machine: Security Center's bit field, the registry value parsing, and the wire payload
/// the hub parses.
///
/// **The silent failure this exists to catch is an unknown that arrives as a "no".** A
/// firewall profile the service would not describe, a Defender with no signature age, a
/// screen saver nobody configured -- each must reach the hub as JSON null, because the hub
/// marks a check failed on `false` and unknown on null. A reporter that defaulted a missing
/// value to false would put a red row on every PC whose provider hiccupped, and the first
/// week of false findings is the week a helpdesk learns to ignore the card.
///
/// **The wire payload is a contract with Python.** `tests/test_posture.py` asserts the same
/// field names from the other side.
///
/// **What this cannot cover** is <c>PostureReader.Read()</c> against a real PC: that
/// <c>HNetCfg.FwPolicy2</c> answers from LocalSystem, that NetLocalGroupGetMembers resolves
/// domain members on a domain PC without stalling, and what Security Center reports on a
/// machine with a third-party product. Expect first-contact findings.
/// </summary>
public class PostureInventoryTests
{
    [Theory]
    // Defender, on and current; on and stale; off; snoozed. The four states a helpdesk sees.
    [InlineData(0x61100u, true, true)]
    [InlineData(0x61110u, true, false)]
    [InlineData(0x60100u, false, true)]
    [InlineData(0x62100u, false, true)]
    public void SecurityCenterProductStateDecodes(uint state, bool enabled, bool upToDate)
    {
        var decoded = PostureReader.DecodeProductState(state);
        Assert.Equal(enabled, decoded.Enabled);
        Assert.Equal(upToDate, decoded.UpToDate);
    }

    [Theory]
    [InlineData(1, PostureReader.KindUser)]
    [InlineData(2, PostureReader.KindGroup)]
    [InlineData(4, PostureReader.KindGroup)]
    [InlineData(5, PostureReader.KindGroup)]
    [InlineData(6, PostureReader.KindDeleted)]
    [InlineData(8, PostureReader.KindUnknown)]
    public void AdministratorsMemberKinds(int sidUsage, string kind) =>
        Assert.Equal(kind, PostureReader.KindFromSidUsage(sidUsage));

    [Fact]
    public void ScreenSaverStringsParseAndGarbageIsNull()
    {
        Assert.Equal(900, PostureReader.ParseRegistryInt(" 900 "));
        Assert.Equal(1, PostureReader.ParseRegistryInt(1));
        Assert.Null(PostureReader.ParseRegistryInt("soon"));
        Assert.Null(PostureReader.ParseRegistryInt(null));
    }

    private static PostureReport Report(bool? firewallPublic = null, int? signatureAge = null) => new(
        new AntivirusArea(true, [new AntivirusProduct("Microsoft Defender Antivirus", 0x61100, true, true)], ""),
        new DefenderArea(true, true, true, signatureAge, null, "Normal", ""),
        new FirewallArea([new FirewallProfile("domain", true), new FirewallProfile("public", firewallPublic)], ""),
        new AutorunArea(null, null, ""),
        new SessionLockArea(null, [new UserLockPolicy("S-1-5-21-1-1001", true, true, 600)], ""),
        new AccountsArea(
            [new LocalAccount("Administrator", "S-1-5-21-1-500", false)],
            [new AdminMember(@"PC-01\Administrator", "S-1-5-21-1-500", PostureReader.KindUser, true)],
            ""),
        new SecureBootArea(PostureReader.SecureBootOn, ""),
        new TpmArea(true, true, true, "2.0", ""));

    [Fact]
    public void EveryAreaIsPresentWithAnErrorString()
    {
        var payload = PostureInventoryReporter.ToPayload(Report());
        foreach (var area in new[] { "antivirus", "defender", "firewall", "autorun",
                                     "session_lock", "accounts", "secure_boot", "tpm" })
        {
            var node = payload[area] as JsonObject;
            Assert.NotNull(node);
            Assert.Equal("", node!["error"]!.GetValue<string>());
        }
    }

    [Fact]
    public void AnUnknownIsNullNeverFalse()
    {
        var payload = PostureInventoryReporter.ToPayload(Report(firewallPublic: null));
        var profiles = payload["firewall"]!["profiles"]!.AsArray();
        Assert.True(profiles[0]!["enabled"]!.GetValue<bool>());
        // Present as a key, null as a value -- the hub's "we could not ask".
        Assert.True(((JsonObject)profiles[1]!).ContainsKey("enabled"));
        Assert.Null(profiles[1]!["enabled"]);
        Assert.Null(payload["autorun"]!["no_drive_type_autorun"]);
        Assert.Null(payload["defender"]!["signature_age_days"]);
    }

    [Fact]
    public void FieldNamesMatchWhatTheHubReads()
    {
        var payload = PostureInventoryReporter.ToPayload(Report(firewallPublic: false, signatureAge: 3));
        Assert.Equal(0x61100u, payload["antivirus"]!["products"]![0]!["state"]!.GetValue<uint>());
        Assert.True(payload["antivirus"]!["products"]![0]!["up_to_date"]!.GetValue<bool>());
        Assert.Equal(3, payload["defender"]!["signature_age_days"]!.GetValue<int>());
        Assert.False(payload["firewall"]!["profiles"]![1]!["enabled"]!.GetValue<bool>());
        Assert.Equal(600, payload["session_lock"]!["users"]![0]!["timeout_seconds"]!.GetValue<int>());
        Assert.Equal("S-1-5-21-1-500", payload["accounts"]!["administrators"]![0]!["sid"]!.GetValue<string>());
        Assert.True(payload["accounts"]!["administrators"]![0]!["local"]!.GetValue<bool>());
        Assert.Equal("on", payload["secure_boot"]!["state"]!.GetValue<string>());
        Assert.Equal("2.0", payload["tpm"]!["spec_version"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("{\"status\":\"ok\",\"posture_rejected\":true}", true)]
    [InlineData("{\"status\":\"ok\"}", false)]
    [InlineData("{\"status\":\"ok\",\"software_rejected\":true}", false)]
    [InlineData("not json", false)]
    public void APostureTheHubCouldNotStoreStaysPending(string reply, bool rejected) =>
        Assert.Equal(rejected, TempMonitorAgent.Fleet.FleetClient.PostureRejected(reply));
}
