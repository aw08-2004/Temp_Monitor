using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet.Executors;
using TempMonitorAgent.Network;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The two halves of Wake-on-LAN that can be tested off a real network (roadmap #10): the
/// magic packet itself, and the wire payload the hub parses.
///
/// **The wire payload is a contract with Python.** `tests/test_wake.py` asserts the same
/// field names from the other side; drift between the two is not a crash but a Network tab
/// that quietly shows nothing and a fleet that cannot be woken. So the shape is asserted
/// here rather than being taken on trust from whatever `NicReader` happens to produce on
/// the build machine, which has no wake-capable NIC and no vendor drivers.
///
/// **What this cannot cover** is `NicReader.Read()` against real hardware: the WMI device
/// power policy, the driver's `*WakeOnMagicPacket` value, and whether a docked laptop
/// reports the dock's adapter at all. Expect first-contact findings there, the same shape
/// the LDAP and TURN work produced.
/// </summary>
public class WakeTests
{
    // ------------------------------------------------------------------ magic packet

    [Fact]
    public void MagicPacketIsSixFfBytesThenTheMacSixteenTimes()
    {
        var packet = WakeMachineExecutor.MagicPacket("AA:BB:CC:DD:EE:FF");

        Assert.Equal(102, packet.Length);
        Assert.Equal(new byte[] { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF }, packet[..6]);
        for (var repeat = 0; repeat < 16; repeat++)
        {
            Assert.Equal(new byte[] { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF },
                         packet[(6 + repeat * 6)..(12 + repeat * 6)]);
        }
    }

    [Theory]
    [InlineData("AA:BB:CC:DD:EE:FF")]
    [InlineData("aa-bb-cc-dd-ee-ff")]
    [InlineData("aabbccddeeff")]
    [InlineData("AA BB CC DD EE FF")]
    public void EverySpellingOfOneMacParsesToTheSameBytes(string spelling)
    {
        Assert.Equal(new byte[] { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF },
                     WakeMachineExecutor.ParseMac(spelling));
    }

    [Theory]
    [InlineData("")]
    [InlineData("AA:BB:CC")]
    [InlineData("AA:BB:CC:DD:EE:FF:00")]
    [InlineData("not a mac at all")]
    public void AnUnparseableMacThrowsRatherThanBecomingAFrameOfZeroes(string bad)
    {
        // The failure mode this prevents: a malformed MAC quietly turning into six zero
        // bytes, which sends perfectly and wakes nothing, and is reported as sent.
        Assert.Throws<FormatException>(() => WakeMachineExecutor.ParseMac(bad));
    }

    // ------------------------------------------------------------------ executor refusals

    private static FleetCommand Command(JsonObject? parameters) =>
        new() { Id = "cmd-1", Type = "wake_machine", Params = parameters };

    [Fact]
    public async Task ARequestWithNoMacIsRefusedBeforeASocketIsOpened()
    {
        var result = await new WakeMachineExecutor().ExecuteAsync(
            Command(new JsonObject { ["broadcast"] = "10.4.7.255" }), null, default);

        Assert.False(result.Success);
        Assert.Contains("no MAC", result.Output);
    }

    [Fact]
    public async Task ABroadcastAddressThatIsNotAnAddressIsRefused()
    {
        var result = await new WakeMachineExecutor().ExecuteAsync(
            Command(new JsonObject
            {
                ["macs"] = new JsonArray { "AA:BB:CC:DD:EE:FF" },
                ["broadcast"] = "not-an-address",
            }), null, default);

        Assert.False(result.Success);
        Assert.Contains("broadcast address", result.Output);
    }

    [Fact]
    public async Task APacketToTheLoopbackBroadcastIsReportedAsSent()
    {
        // 127.255.255.255 is a real, routable-to-nowhere broadcast that every build machine
        // has, so this exercises the whole send path -- socket, SO_BROADCAST, sendto -- with
        // nothing leaving the box. What it deliberately does NOT assert is that anything
        // woke up: nothing acknowledges a magic packet, and "the packet went out" is the
        // strongest claim this executor is ever allowed to make.
        var result = await new WakeMachineExecutor().ExecuteAsync(
            Command(new JsonObject
            {
                ["target"] = "PC-14",
                ["macs"] = new JsonArray { "AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:01" },
                ["broadcast"] = "127.255.255.255",
                ["port"] = 9,
            }), null, default);

        Assert.True(result.Success);
        Assert.Contains("2 magic packet(s) sent", result.Output);
    }

    // ------------------------------------------------------------------ wire payload

    private static NicInfo Wired(string mac, string ipv4 = "10.4.7.31", int? prefix = 24,
                                 bool? wakeEnabled = true) =>
        new(mac, "Ethernet", "Intel I219-LM", ipv4, prefix, NicReader.KindWired, true,
            wakeEnabled);

    [Fact]
    public void ThePayloadCarriesTheFieldNamesTheHubIngestReads()
    {
        var payload = NetworkInventoryReporter.ToPayload(
            new NetworkReport(new[] { Wired("AA:BB:CC:DD:EE:01") }, FastStartup: false));

        Assert.False(payload["fast_startup"]!.GetValue<bool>());
        var nic = payload["nics"]!.AsArray()[0]!.AsObject();
        Assert.Equal("AA:BB:CC:DD:EE:01", nic["mac"]!.GetValue<string>());
        Assert.Equal("Ethernet", nic["name"]!.GetValue<string>());
        Assert.Equal("Intel I219-LM", nic["description"]!.GetValue<string>());
        Assert.Equal("10.4.7.31", nic["ipv4"]!.GetValue<string>());
        Assert.Equal(24, nic["prefix"]!.GetValue<int>());
        Assert.Equal("wired", nic["kind"]!.GetValue<string>());
        Assert.True(nic["link_up"]!.GetValue<bool>());
        Assert.True(nic["wake_enabled"]!.GetValue<bool>());
    }

    [Fact]
    public void UnknownStaysNullRatherThanBecomingAFact()
    {
        // The hub stores three states for wake_enabled, and the middle one is the point: an
        // adapter whose driver does not publish the setting must not be reported as "cannot
        // wake", or the console sends somebody to fix a working machine. Same for a prefix
        // the OS would not give us -- a 0 there would look like a real /0.
        var payload = NetworkInventoryReporter.ToPayload(new NetworkReport(
            new[] { Wired("AA:BB:CC:DD:EE:02", ipv4: "", prefix: null, wakeEnabled: null) },
            FastStartup: null));

        var nic = payload["nics"]!.AsArray()[0]!.AsObject();
        Assert.Null(nic["prefix"]);
        Assert.Null(nic["wake_enabled"]);
        Assert.Null(payload["fast_startup"]);
    }

    [Fact]
    public void AnEmptyMachineStillProducesAWellFormedPayload()
    {
        // The hub distinguishes an empty LIST (a real report of no adapters) from a
        // malformed one (which it refuses, so a good inventory is never wiped). This is the
        // side of that contract the agent owns: never send the malformed shape.
        var payload = NetworkInventoryReporter.ToPayload(
            new NetworkReport(Array.Empty<NicInfo>(), FastStartup: true));

        Assert.NotNull(payload["nics"]);
        Assert.Empty(payload["nics"]!.AsArray());
        Assert.True(payload["fast_startup"]!.GetValue<bool>());
    }

    // ------------------------------------------------------------------ MAC formatting

    [Fact]
    public void FormatMacProducesTheSpellingTheHubKeysOn()
    {
        Assert.Equal("AA:BB:CC:DD:EE:FF",
                     NicReader.FormatMac(new byte[] { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF }));
    }

    [Fact]
    public void AnAllZeroMacIsRejected()
    {
        // Hyper-V's placeholder adapters report one. Stored, it looks exactly like a
        // wakeable target while every packet aimed at it goes nowhere -- so it is rejected
        // here AND again on the hub's ingest, since it is the value the feature is keyed on.
        Assert.Equal("", NicReader.FormatMac(new byte[6]));
    }

    [Fact]
    public void AnAddressThatIsNotSixBytesIsRejected()
    {
        Assert.Equal("", NicReader.FormatMac(null));
        Assert.Equal("", NicReader.FormatMac(Array.Empty<byte>()));
        Assert.Equal("", NicReader.FormatMac(new byte[] { 1, 2, 3 }));
        // Firewire and InfiniBand adapters report longer addresses.
        Assert.Equal("", NicReader.FormatMac(new byte[8]));
    }
}
