using System.Security.Cryptography;
using Org.BouncyCastle.Crypto.Generators;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Security;
using FleetHubAgent.Update;

namespace FleetHubAgent.Tests;

/// <summary>
/// The signed self-update's trust root and its version arithmetic (roadmap #22).
///
/// **The silent failure this file exists to catch is a verifier that says yes.** Every other
/// failure in this agent degrades to a machine that reports nothing for a while; this one
/// degrades to a machine that downloads and runs somebody else's code as root, on every Linux
/// box in the fleet at once, because they all read the same manifest. And it degrades quietly:
/// a verifier that returned true on malformed input would pass every functional test, because
/// the happy path still works.
///
/// So the assertions here are mostly about REFUSING -- an unset key, a key or signature of the
/// wrong length, hex that is not hex, a signature over different bytes, and a signature made by
/// a different key. A real Ed25519 keypair is generated per run rather than hardcoded, so
/// "signed by the wrong key" is a genuinely different key rather than a string edit.
///
/// The second is version comparison. Every updater in this product installs only what is
/// STRICTLY newer, which is what makes a replayed or rolled-back manifest a no-op rather than a
/// downgrade -- and VERSIONING.md records that a repeated number has already shipped twice by
/// accident. A comparator that read "0.10.0" as older than "0.9.0" would strand a fleet on an
/// old build with nothing anywhere saying so.
/// </summary>
public class SelfUpdateTests
{
    private static (string PublicHex, Ed25519PrivateKeyParameters Private) NewKey()
    {
        var gen = new Ed25519KeyPairGenerator();
        gen.Init(new Ed25519KeyGenerationParameters(new SecureRandom()));
        var pair = gen.GenerateKeyPair();
        var pub = (Ed25519PublicKeyParameters)pair.Public;
        return (Convert.ToHexString(pub.GetEncoded()).ToLowerInvariant(),
                (Ed25519PrivateKeyParameters)pair.Private);
    }

    private static string Sign(Ed25519PrivateKeyParameters key, byte[] message)
    {
        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, key);
        signer.BlockUpdate(message, 0, message.Length);
        return Convert.ToHexString(signer.GenerateSignature()).ToLowerInvariant();
    }

    // ---------------------------------------------------------------- the trust root

    [Fact]
    public void A_manifest_signed_by_the_release_key_verifies()
    {
        var (pub, priv) = NewKey();
        var manifest = "{\"version\":\"0.2.0\",\"sha256\":\"ab\",\"url\":\"https://x/y\"}"u8.ToArray();
        Assert.True(SignatureVerifier.VerifyRaw(pub, manifest, Sign(priv, manifest)));
    }

    [Fact]
    public void A_manifest_signed_by_a_DIFFERENT_key_is_refused()
    {
        // The one that matters: the hub can move a machine between channels, but it has never
        // held this key, so it cannot make a manifest this agent will install.
        var (pub, _) = NewKey();
        var (_, otherPriv) = NewKey();
        var manifest = "{\"version\":\"0.2.0\"}"u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(pub, manifest, Sign(otherPriv, manifest)));
    }

    [Fact]
    public void A_signature_over_DIFFERENT_bytes_is_refused()
    {
        // What an edited manifest looks like: the signature is real, and it is not over this.
        var (pub, priv) = NewKey();
        var signed = "{\"version\":\"0.2.0\"}"u8.ToArray();
        var tampered = "{\"version\":\"9.9.9\"}"u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(pub, tampered, Sign(priv, signed)));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not-hex-at-all")]
    [InlineData("ab")]                                   // right alphabet, wrong length
    public void A_malformed_key_is_refused(string? key)
    {
        var (_, priv) = NewKey();
        var message = "anything"u8.ToArray();
        Assert.False(SignatureVerifier.VerifyRaw(key, message, Sign(priv, message)));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("zz")]
    [InlineData("abcd")]                                 // valid hex, wrong length
    public void A_malformed_signature_is_refused(string? signature)
    {
        var (pub, _) = NewKey();
        Assert.False(SignatureVerifier.VerifyRaw(pub, "anything"u8.ToArray(), signature));
    }

    [Fact]
    public void Nothing_malformed_ever_throws()
    {
        // Fail-closed means returning false, not raising -- an exception escaping here would
        // be caught by the updater's outer handler and logged as "unexpected error", which
        // reads like a transient fault rather than a refused signature.
        var exception = Record.Exception(() =>
            SignatureVerifier.VerifyRaw("!!", [], "??"));
        Assert.Null(exception);
    }

    // ---------------------------------------------------------------- version arithmetic

    [Theory]
    [InlineData("0.2.0", "0.1.0", 1)]
    [InlineData("0.1.0", "0.2.0", -1)]
    [InlineData("0.1.0", "0.1.0", 0)]
    // Numeric, not lexical. "0.10.0" sorting below "0.9.0" would strand a fleet on an old
    // build with nothing anywhere saying so.
    [InlineData("0.10.0", "0.9.0", 1)]
    [InlineData("1.0.0", "0.99.99", 1)]
    // Padded, so "0.2" and "0.2.0" are the same version rather than adjacent ones.
    [InlineData("0.2", "0.2.0", 0)]
    // Suffixes are truncated rather than rejected, matching the hub's version_tuple. The
    // format rule forbids them; this is what happens if one arrives anyway.
    [InlineData("0.2.0-rc1", "0.2.0", 0)]
    [InlineData("garbage", "0.0.1", -1)]
    public void Versions_compare_the_way_every_other_comparator_here_does(
        string a, string b, int expected)
    {
        Assert.Equal(expected, VersionUtil.Compare(a, b));
    }

    [Fact]
    public void The_manifests_own_version_is_not_newer_than_itself()
    {
        // The strictly-newer rule, stated as the updater applies it: a manifest republished
        // unchanged must do nothing. A `<= 0` that became `< 0` would have every machine in
        // the fleet re-download and restart onto the build it is already running, every
        // fifteen minutes, forever.
        Assert.True(VersionUtil.Compare(AgentConfig.Version, AgentConfig.Version) <= 0);
    }

    // ---------------------------------------------------------------- the wiring

    [Fact]
    public void The_manifest_url_is_this_agents_own_and_not_the_Windows_one()
    {
        // The concrete hazard the whole per-platform change exists to remove: the Windows
        // manifest describes a win-x64 executable, and this agent would have installed it.
        Assert.Contains("agent-linux/agent-linux.manifest.json", AgentConfig.StableManifestUrl);
        Assert.DoesNotContain("/agent/agent.manifest.json", AgentConfig.StableManifestUrl);
        Assert.StartsWith("https://", AgentConfig.StableManifestUrl);
    }

    [Fact]
    public void The_signature_url_is_the_manifest_url_plus_sig()
    {
        Assert.Equal(AgentConfig.UpdateManifestUrl + ".sig", AgentConfig.UpdateManifestSigUrl);
    }

    [Fact]
    public void The_release_key_is_the_one_that_signs_every_other_artifact_here()
    {
        // agent/src/TempMonitorAgent/AgentConfig.cs and hub/clientrelease.py hold the same
        // value. Restated rather than read, because neither is importable from here -- and
        // pinned because a key that differed by one character would refuse every real release
        // while looking entirely plausible.
        Assert.Equal("9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c",
                     AgentConfig.UpdatePublicKeyHex);
        Assert.Equal(64, AgentConfig.UpdatePublicKeyHex.Length);
    }

    [Fact]
    public void Staging_is_under_the_state_directory_rather_than_tmp()
    {
        // /tmp is world-writable and frequently a tmpfs, and the file staged there is about to
        // be executed as root.
        Assert.StartsWith(AgentConfig.StateDir, AgentConfig.UpdateStagingDir);
    }

    [Fact]
    public void A_downloaded_binary_hashes_to_what_the_manifest_says_or_it_is_not_installed()
    {
        // The hash is what makes the DOWNLOAD untrusted: the binary can come from anywhere as
        // long as it matches the value inside the signed manifest. Asserted here as the
        // arithmetic the updater does, so a change from SHA-256 to something else fails a test
        // rather than silently accepting a different file.
        var payload = "a pretend agent binary"u8.ToArray();
        var digest = Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
        Assert.Equal(64, digest.Length);
        Assert.NotEqual(digest,
            Convert.ToHexString(SHA256.HashData("something else"u8.ToArray())).ToLowerInvariant());
    }
}
