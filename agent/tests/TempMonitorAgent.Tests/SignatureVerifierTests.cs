using System.Text;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Cross-checks the C# Ed25519 verifier against a signature produced by the Python
/// signer, over a real self-update manifest — the exact bytes sign_release.py
/// --sign-agent emits (json.dumps(sort_keys=True, separators=(',',':'))). Also proves
/// the fail-closed behaviour on tampering and malformed inputs.
///
/// This covers the agent's UPDATE trust root: VerifyRaw is what stands between a
/// compromised hub and arbitrary code running as SYSTEM on the fleet (SelfUpdater
/// verifies the manifest through it, then hash-checks the binary). It used to be
/// tested only transitively, via the since-removed VerifyCommand.
/// </summary>
public class SignatureVerifierTests
{
    // Vector generated with the hub's Python Ed25519 over the manifest bytes below.
    // Regenerate with private key 0x4f repeated 32 times if the manifest shape changes.
    private const string PubHex = "00e3c56b91ab0a017174b96645eaf928366cdbae1e87fd21bf86661d86f3e7ef";
    private const string SigHex =
        "1ab3ce227d22863a1de996484f97d151a6bc1d34e558f7304f34d1a85e6d33b5" +
        "8e3952f45ed5c4f00401eaafa9ed9ae6cc38aa1eac7b15d0b082abbd42566709";
    private const string ManifestJson =
        "{\"sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"," +
        "\"url\":\"https://github.com/aw08-2004/Temp_Monitor/releases/download/agent-v3.1.0/TempMonitorAgent.exe\"," +
        "\"version\":\"3.1.0\"}";

    private static byte[] Manifest => Encoding.UTF8.GetBytes(ManifestJson);

    [Fact]
    public void ValidSignature_Verifies()
    {
        Assert.True(SignatureVerifier.VerifyRaw(PubHex, Manifest, SigHex));
    }

    [Fact]
    public void TamperedManifest_Fails()
    {
        // Swap the download URL — the attack the signature exists to stop.
        var tampered = Encoding.UTF8.GetBytes(
            ManifestJson.Replace("github.com/aw08-2004", "evil.example"));
        Assert.False(SignatureVerifier.VerifyRaw(PubHex, tampered, SigHex));
    }

    [Fact]
    public void TamperedSignature_Fails()
    {
        // Flip the last hex nibble.
        var bad = SigHex[..^1] + (SigHex[^1] == '9' ? '8' : '9');
        Assert.False(SignatureVerifier.VerifyRaw(PubHex, Manifest, bad));
    }

    [Fact]
    public void WrongKey_Fails()
    {
        var otherKey = new string('a', 64);
        Assert.False(SignatureVerifier.VerifyRaw(otherKey, Manifest, SigHex));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    public void EmptyKey_FailsClosed(string? key)
    {
        Assert.False(SignatureVerifier.VerifyRaw(key, Manifest, SigHex));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    public void EmptySignature_FailsClosed(string? sig)
    {
        Assert.False(SignatureVerifier.VerifyRaw(PubHex, Manifest, sig));
    }

    [Fact]
    public void MalformedHex_FailsClosed()
    {
        Assert.False(SignatureVerifier.VerifyRaw("nothex", Manifest, SigHex));
        Assert.False(SignatureVerifier.VerifyRaw(PubHex, Manifest, "zzzz"));
    }

    [Fact]
    public void WrongLengthKeyOrSignature_FailsClosed()
    {
        // Well-formed hex, wrong Ed25519 lengths (31-byte key, 63-byte sig): the
        // explicit length guard, which BouncyCastle would otherwise throw over.
        Assert.False(SignatureVerifier.VerifyRaw(new string('a', 62), Manifest, SigHex));
        Assert.False(SignatureVerifier.VerifyRaw(PubHex, Manifest, new string('a', 126)));
    }

    [Fact]
    public void SignatureIsAcceptedWithSurroundingWhitespace()
    {
        // The .sig files are read straight off disk; a trailing newline must not
        // invalidate an otherwise good update.
        Assert.True(SignatureVerifier.VerifyRaw($"  {PubHex}\n", Manifest, $"  {SigHex}\n"));
    }
}
