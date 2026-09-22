using System.Text.Json.Nodes;
using TempMonitorAgent.Security;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The half of BitLocker escrow that can be tested without an encrypted disk (roadmap #19):
/// the wire payload the hub parses.
///
/// **The wire payload is a contract with Python.** `tests/test_bitlocker.py` asserts the same
/// field names from the other side; drift between the two is not a crash but an encryption
/// card that quietly shows nothing, and a fleet whose recovery keys are never collected. So
/// the shape is asserted here rather than taken on trust from whatever
/// <see cref="BitLockerReader"/> happens to produce on the build machine, which has no
/// BitLocker provider at all.
///
/// **`percentage` is asserted as a null, not as a zero.** The hub stores "we were not told"
/// distinctly from "0% encrypted", and a JSON 0 where a null belonged would render a
/// fully-encrypted disk as an unencrypted one on the day the provider stopped answering that
/// one property.
///
/// **What this cannot cover** is <c>BitLockerReader.Read()</c> against real hardware: the
/// packet-privacy connection the provider insists on, a suspended volume, a locked one
/// answering FVE_E_LOCKED_VOLUME to every call, and whether GetKeyProtectors returns the
/// protector ids in the form the recovery screen shows. Expect first-contact findings there,
/// the same shape the LDAP and TURN work produced.
/// </summary>
public class BitLockerTests
{
    private static BitLockerReport Supported(params VolumeInfo[] volumes) =>
        new(BitLockerReader.SupportSupported, "", volumes);

    private static VolumeInfo Volume(params KeyProtectorInfo[] protectors) =>
        new("C:", @"\\?\Volume{1}", BitLockerReader.ProtectionOn, "fully_encrypted", 100,
            "XTS-AES 128", protectors);

    [Fact]
    public void PayloadCarriesTheFieldNamesTheHubIngestReads()
    {
        var payload = BitLockerInventoryReporter.ToPayload(Supported(Volume(
            new KeyProtectorInfo("{TPM-1}", BitLockerReader.KindOther, "TPM"),
            new KeyProtectorInfo("{REC-1}", BitLockerReader.KindRecoveryPassword,
                                 "Numerical Password"))));

        Assert.Equal("supported", (string?)payload["support"]);
        var volume = payload["volumes"]!.AsArray()[0]!;
        Assert.Equal("C:", (string?)volume["mount"]);
        Assert.Equal("on", (string?)volume["protection"]);
        Assert.Equal("fully_encrypted", (string?)volume["conversion"]);
        Assert.Equal("XTS-AES 128", (string?)volume["method"]);
        Assert.Equal(100, (int?)volume["percentage"]);

        var protectors = volume["protectors"]!.AsArray();
        Assert.Equal(2, protectors.Count);
        Assert.Equal("{TPM-1}", (string?)protectors[0]!["id"]);
        Assert.Equal("other", (string?)protectors[0]!["kind"]);
        // The one value the hub branches on: only this kind is ever asked for a secret.
        Assert.Equal("recovery_password", (string?)protectors[1]!["kind"]);
    }

    [Fact]
    public void AnUnknownEncryptionPercentageIsNullRatherThanZero()
    {
        var payload = BitLockerInventoryReporter.ToPayload(Supported(
            new VolumeInfo("D:", "", BitLockerReader.ProtectionUnknown, "", null, "", [])));

        var volume = payload["volumes"]!.AsArray()[0]!;
        Assert.Null(volume["percentage"]);
    }

    [Fact]
    public void AnUnsupportedMachineSaysSoRatherThanReportingNoVolumes()
    {
        // The distinction the hub stores and the console renders differently: a machine with
        // no BitLocker provider is a permanent, correct state, and reporting it as an empty
        // supported inventory would show a clean encryption card for a PC that has none.
        var payload = BitLockerInventoryReporter.ToPayload(
            new BitLockerReport(BitLockerReader.SupportUnsupported,
                                "This edition of Windows has no BitLocker provider.", []));

        Assert.Equal("unsupported", (string?)payload["support"]);
        Assert.Empty(payload["volumes"]!.AsArray());
    }

    [Fact]
    public void NothingWantedProducesNoEscrowBodyAtAll()
    {
        // The steady state of every machine in the fleet. A body built here would be a request
        // that reads a recovery password off the disk for nobody.
        Assert.Null(BitLockerInventoryReporter.BuildEscrow([]));
    }
}
