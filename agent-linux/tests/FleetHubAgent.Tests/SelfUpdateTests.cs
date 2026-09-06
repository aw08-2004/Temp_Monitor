using System.Security.Cryptography;
using FleetHubAgent.Fleet;
using FleetHubAgent.Update;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Security;

namespace FleetHubAgent.Tests;

/// <summary>
/// The update trust root, which is the one thing in this agent whose failure mode is the whole
/// fleet running someone else's code as root.
///
/// The silent failure these guard is a verifier that says YES when it should not -- a
/// truncated key, an empty signature, a manifest edited after signing. None of those throw and
/// none of them look wrong in a log; the agent simply installs the binary. So every rejection
/// path is asserted explicitly rather than trusting that "the library handles it".
///
/// Signatures are generated here with a throwaway keypair rather than pasted as fixtures,
/// because a fixture would be a signature nobody can regenerate and would rot into a test that
/// is skipped rather than fixed.
/// </summary>
public class SignatureVerifierTests
{
    private static (string PubHex, Ed25519Signer Signer) NewKey()
    {
        var gen = new Org.BouncyCastle.Crypto.Generators.Ed25519KeyPairGenerator();
        gen.Init(new Org.BouncyCastle.Crypto.KeyGenerationParameters(new SecureRandom(), 256));
        var pair = gen.GenerateKeyPair();
        var pub = (Ed25519PublicKeyParameters)pair.Public;
        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, pair.Private);
        return (Convert.ToHexString(pub.GetEncoded()).ToLowerInvariant(), signer);
    }

    private static string Sign(Ed25519Signer signer, byte[] message)
    {
        signer.Reset();
        signer.BlockUpdate(message, 0, message.Length);
        return Convert.ToHexString(signer.GenerateSignature()).ToLowerInvariant();
    }

    [Fact]
    public void Accepts_a_real_signature_over_the_exact_bytes()
    {
        var (pubHex, signer) = NewKey();
        var manifest = """{"sha256":"ab","url":"https://x/y","version":"0.2.0"}"""u8.ToArray();
        Assert.True(SignatureVerifier.VerifyRaw(pubHex, manifest, Sign(signer, manifest)));
    }

    [Fact]
    public void Rejects_a_manifest_edited_after_signing()
    {
        // The attack the whole arrangement exists to stop: a valid signature, over different
        // bytes. One byte of the version string is enough.
        var (pubHex, signer) = NewKey();
        var signed = """{"sha256":"ab","url":"https://x/y","version":"0.2.0"}"""u8.ToArray();
        var sig = Sign(signer, signed);
        var tampered = """{"sha256":"ab","url":"https://x/y","version":"9.2.0"}"""u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(pubHex, tampered, sig));
    }

    [Fact]
    public void Rejects_a_signature_from_a_different_key()
    {
        var (_, signerA) = NewKey();
        var (pubHexB, _) = NewKey();
        var manifest = "payload"u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(pubHexB, manifest, Sign(signerA, manifest)));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not-hex-at-all")]
    [InlineData("9a4f")]                        // right shape, wrong length
    [InlineData("9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c00")] // 33 bytes
    public void Fails_closed_on_a_bad_key(string? key)
    {
        var (_, signer) = NewKey();
        var manifest = "payload"u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(key, manifest, Sign(signer, manifest)));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("zzzz")]
    [InlineData("abcd")]                        // valid hex, wrong length
    public void Fails_closed_on_a_bad_signature(string? sig)
    {
        var (pubHex, _) = NewKey();
        Assert.False(SignatureVerifier.VerifyRaw(pubHex, "payload"u8.ToArray(), sig));
    }

    [Fact]
    public void The_compiled_in_key_is_a_valid_ed25519_public_key()
    {
        // Not that it is the RIGHT key -- nothing here can know that -- but that it is 32 bytes
        // of hex. A truncated or mistyped key makes VerifyRaw fail closed on every manifest,
        // which stops the entire fleet updating with no error anywhere except a debug line.
        var bytes = Convert.FromHexString(AgentConfig.UpdatePublicKeyHex);
        Assert.Equal(32, bytes.Length);
    }

    [Fact]
    public void The_agent_and_the_hub_share_one_release_trust_root()
    {
        // The Linux and Windows agents must verify against the SAME offline key: one key, two
        // manifests. A second key would be a second thing to protect and a second way for a
        // fleet to trust something nobody meant it to. Read from the Windows AgentConfig.cs so
        // that changing either one alone fails here.
        var windows = File.ReadAllText(FindRepoFile("agent/src/TempMonitorAgent/AgentConfig.cs"));
        Assert.Contains(AgentConfig.UpdatePublicKeyHex, windows, StringComparison.OrdinalIgnoreCase);
    }

    internal static string FindRepoFile(string relative)
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, relative.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(candidate)) return candidate;
            dir = dir.Parent;
        }
        throw new FileNotFoundException("Could not locate " + relative + " from " + AppContext.BaseDirectory);
    }
}

/// <summary>Version comparison, which decides whether an update happens at all.
///
/// The failure worth guarding is not a crash: it is answering "not newer" for something that
/// is, which leaves a fleet on an old build while the release looks shipped. CLAUDE.md records
/// that a repeated or lowered version number has bitten twice.</summary>
public class VersionUtilTests
{
    [Theory]
    [InlineData("0.2.0", "0.1.0", 1)]
    [InlineData("0.1.0", "0.2.0", -1)]
    [InlineData("0.1.0", "0.1.0", 0)]
    [InlineData("0.10.0", "0.9.0", 1)]     // numeric, not lexicographic: "10" beats "9"
    [InlineData("1.0.0", "0.99.99", 1)]
    [InlineData("0.2", "0.2.0", 0)]        // padded
    [InlineData("0.2.0-rc1", "0.2.0", 0)]  // suffix ignored, matching the hub's parser
    [InlineData("3.35.1", "0.1.0", 1)]
    public void Compares_like_the_hub_does(string a, string b, int expected) =>
        Assert.Equal(expected, VersionUtil.Compare(a, b));

    [Fact]
    public void An_equal_version_is_not_newer()
    {
        // The updater applies only on strictly greater. Re-publishing a manifest under a
        // version already in the field must do nothing, everywhere, rather than restart the
        // fleet onto the same build.
        Assert.False(VersionUtil.Compare("0.1.0", AgentConfig.Version) > 0);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("garbage")]
    public void Unparseable_versions_read_as_zero_rather_than_throwing(string? v)
    {
        // A manifest whose version field is nonsense must not take down the update loop; it
        // reads as 0 and therefore as "not newer", which is the safe answer.
        Assert.Equal(0, VersionUtil.Compare(v, "0.0.0"));
    }
}

/// <summary>Where a downloaded binary is staged.
///
/// This is the Linux-specific trap: File.Move is an atomic rename within one filesystem and a
/// non-atomic copy across two. /opt and /var are separate mounts on plenty of real installs, so
/// staging under the state root -- where the Windows agent stages, and the obvious place to
/// "tidy" this to -- would silently turn the swap into a copy that a full disk can interrupt
/// half-written, leaving a machine with no working agent binary.</summary>
public class StagingLocationTests
{
    [Fact]
    public void Staging_sits_beside_the_running_binary_not_under_the_state_root()
    {
        var staging = SelfUpdater.StagingDir();
        Assert.NotNull(staging);

        var exeDir = Path.GetDirectoryName(Environment.ProcessPath);
        Assert.Equal(exeDir, Path.GetDirectoryName(staging));

        // The load-bearing assertion: never under /var/lib, however the state root is
        // configured, because that is a different mount from /opt on real machines.
        Assert.False(staging!.StartsWith(AgentConfig.StateDir, StringComparison.Ordinal),
            $"staging ({staging}) must not live under the state root ({AgentConfig.StateDir}) -- " +
            "a cross-filesystem File.Move is a copy, not an atomic rename");
    }
}

/// <summary>The manifest shape, which is a contract with sign_release.py.
///
/// The agent and the signing script are in different languages and are only ever connected by
/// these three field names. A rename on either side produces a manifest that verifies
/// perfectly and then deserializes to nulls, which the updater treats as "incomplete" and
/// skips -- silently, forever.</summary>
public class ManifestShapeTests
{
    [Fact]
    public void Parses_a_manifest_in_the_exact_shape_sign_release_emits()
    {
        // Copied from the shipped agent/agent.manifest.json: compact, keys sorted, no spaces.
        var json = """{"sha256":"1c1efc10","url":"https://example/fleethub-agent","version":"0.2.0"}""";
        var m = System.Text.Json.JsonSerializer.Deserialize<UpdateManifest>(json);

        Assert.NotNull(m);
        Assert.Equal("0.2.0", m!.Version);
        Assert.Equal("1c1efc10", m.Sha256);
        Assert.Equal("https://example/fleethub-agent", m.Url);
    }

    [Fact]
    public void The_real_windows_manifest_still_has_these_three_fields()
    {
        // Read the manifest actually in the repo. If sign_release.py ever changes its output
        // shape, this fails here rather than in the field, where the symptom is a fleet that
        // quietly stops updating.
        var path = SignatureVerifierTests.FindRepoFile("agent/agent.manifest.json");
        var m = System.Text.Json.JsonSerializer.Deserialize<UpdateManifest>(File.ReadAllText(path));

        Assert.NotNull(m);
        Assert.False(string.IsNullOrEmpty(m!.Version));
        Assert.False(string.IsNullOrEmpty(m.Sha256));
        Assert.False(string.IsNullOrEmpty(m.Url));
    }
}
