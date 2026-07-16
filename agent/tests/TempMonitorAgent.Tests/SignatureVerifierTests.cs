using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>Cross-checks the C# Ed25519 verifier against a signature produced by the
/// Python signer (fleet.canonical_command_bytes + Ed25519). Also proves the
/// fail-closed behaviour on tampering and missing inputs.</summary>
public class SignatureVerifierTests
{
    // Vector generated with the hub's Python (Ed25519 over canonical bytes).
    private const string PubHex = "9f942bb7c999bae3587c03776b6bbeff6e87f18846703f572094446f7ed8670e";
    private const string SigHex =
        "3151ec7eb27e0d6d53ac1daa9a8f17c76541e3493eaf5d0389ae97101c5237e8" +
        "1235082f7800c99c10d9516ed5a8926258a2baf59519c9f2666a55794b6b3406";
    private const string Type = "run_script";
    private const string Machine = "PC-01";
    private static JsonNode Params => JsonNode.Parse("{\"script\": \"echo hi\"}")!;

    [Fact]
    public void ValidSignature_Verifies()
    {
        Assert.True(SignatureVerifier.VerifyCommand(PubHex, Type, Machine, Params, SigHex));
    }

    [Fact]
    public void TamperedParams_Fails()
    {
        var tampered = JsonNode.Parse("{\"script\": \"echo pwned\"}");
        Assert.False(SignatureVerifier.VerifyCommand(PubHex, Type, Machine, tampered, SigHex));
    }

    [Fact]
    public void TamperedSignature_Fails()
    {
        // Flip the last hex nibble.
        var bad = SigHex[..^1] + (SigHex[^1] == '6' ? '7' : '6');
        Assert.False(SignatureVerifier.VerifyCommand(PubHex, Type, Machine, Params, bad));
    }

    [Fact]
    public void WrongMachine_Fails()
    {
        Assert.False(SignatureVerifier.VerifyCommand(PubHex, Type, "OTHER-PC", Params, SigHex));
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    public void EmptyKey_FailsClosed(string? key)
    {
        Assert.False(SignatureVerifier.VerifyCommand(key, Type, Machine, Params, SigHex));
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    public void EmptySignature_FailsClosed(string? sig)
    {
        Assert.False(SignatureVerifier.VerifyCommand(PubHex, Type, Machine, Params, sig));
    }

    [Fact]
    public void MalformedHex_FailsClosed()
    {
        Assert.False(SignatureVerifier.VerifyCommand("nothex", Type, Machine, Params, SigHex));
        Assert.False(SignatureVerifier.VerifyCommand(PubHex, Type, Machine, Params, "zzzz"));
    }
}
