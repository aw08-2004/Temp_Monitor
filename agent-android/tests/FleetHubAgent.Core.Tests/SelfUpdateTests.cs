using System.Net;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using Org.BouncyCastle.Crypto.Generators;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Security;
using FleetHubAgent.State;
using FleetHubAgent.Update;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The signed self-update (roadmap #22): what it refuses, and in what order it does things.
///
/// **The silent failure this file exists to catch is an install that happens on the wrong
/// evidence.** Every other failure in this agent degrades to a device that reports nothing for
/// a while; this one degrades to a phone silently installing somebody else's package as its
/// own device owner, with no prompt and nobody watching. And it degrades quietly, because the
/// happy path still works: a verifier that returned true on malformed input, or a hash check
/// that compared the wrong bytes, would pass every functional test there is.
///
/// So most of what is asserted here is REFUSAL -- a bad signature, a signature by another key,
/// a manifest that is not newer, a payload whose hash does not match, and a device that is not
/// a Device Owner. The keypair is generated per run, so "signed by the wrong key" is a genuinely
/// different key rather than a string edit.
///
/// **The ordering assertions are the ones peculiar to Android.** The platform kills this
/// process to replace the app, so anything the updater does after handing over does not happen:
/// the attempt counter has to be written BEFORE the install, or a build that dies on startup is
/// installed again on every tick forever with nothing recording that it was tried.
/// </summary>
public class SelfUpdateTests
{
    private static (string PublicHex, Ed25519PrivateKeyParameters Private) NewKey()
    {
        var gen = new Ed25519KeyPairGenerator();
        gen.Init(new Ed25519KeyGenerationParameters(new SecureRandom()));
        var pair = gen.GenerateKeyPair();
        return (Convert.ToHexString(((Ed25519PublicKeyParameters)pair.Public).GetEncoded())
                    .ToLowerInvariant(),
                (Ed25519PrivateKeyParameters)pair.Private);
    }

    private static string Sign(Ed25519PrivateKeyParameters key, byte[] message)
    {
        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, key);
        signer.BlockUpdate(message, 0, message.Length);
        return Convert.ToHexString(signer.GenerateSignature()).ToLowerInvariant();
    }

    private sealed class FakeInstaller(bool canInstall = true) : IPackageInstaller
    {
        public bool CanInstallSilently { get; set; } = canInstall;
        public string? Failure { get; set; }
        public string? InstalledPath { get; private set; }
        /// <summary>What the attempt counter said at the moment Install was called. The
        /// platform kills the process here on a real device, so anything written after this
        /// point is never written -- which is what this captures.</summary>
        public RestartState? StateAtInstall { get; set; }
        public Func<RestartState?>? ReadState { get; set; }

        public string? Install(string path)
        {
            InstalledPath = path;
            StateAtInstall = ReadState?.Invoke();
            return Failure;
        }
    }

    /// <summary>An HttpClient answering from a dictionary of url to bytes. 404 for anything
    /// else, so a URL the updater builds wrongly fails rather than silently matching.</summary>
    private sealed class StubHandler(Dictionary<string, byte[]> routes) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken ct)
        {
            var url = request.RequestUri!.ToString();
            if (!routes.TryGetValue(url, out var body))
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound));
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new ByteArrayContent(body),
            });
        }
    }

    private sealed class Fixture : IDisposable
    {
        public FakeStateStore Store { get; } = new();
        public AgentState State { get; }
        public FakeInstaller Installer { get; } = new();
        public string Directory { get; }

        public Fixture()
        {
            State = new AgentState(Store);
            Directory = Path.Combine(Path.GetTempPath(), "fh-update-" + Guid.NewGuid().ToString("N"));
            AgentConfig.ConfigureStaging(Directory);
            Installer.ReadState = () => State.LoadRestartState();
        }

        public void Dispose()
        {
            try { if (System.IO.Directory.Exists(Directory)) System.IO.Directory.Delete(Directory, true); }
            catch { /* a temp directory left behind is not a test failure */ }
        }
    }

    private static byte[] ManifestBytes(string version, byte[] payload, string url)
    {
        var sha = Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
        // sort_keys + compact separators, exactly as sign_release.py writes it -- the signature
        // covers these bytes, so the shape is part of the contract rather than a detail.
        return Encoding.UTF8.GetBytes(
            $"{{\"sha256\":\"{sha}\",\"url\":\"{url}\",\"version\":\"{version}\"}}");
    }

    /// <summary>An updater wired to a stub hub and, through the internal seam, to the test's
    /// own release key. The seam exists only here -- see SelfUpdater on why the real key stays
    /// a compile-time constant.</summary>
    private static SelfUpdater Build(Fixture fixture, byte[] manifest, string signature,
                                     byte[] payload, string payloadUrl, string publicKeyHex)
    {
        var routes = new Dictionary<string, byte[]>(StringComparer.Ordinal)
        {
            [AgentConfig.UpdateManifestUrl] = manifest,
            [AgentConfig.UpdateManifestSigUrl] = Encoding.UTF8.GetBytes(signature),
            [payloadUrl] = payload,
        };
        return new SelfUpdater(NullLogger<SelfUpdater>.Instance, fixture.State, fixture.Installer,
                               new HttpClient(new StubHandler(routes)), publicKeyHex);
    }

    private const string PayloadUrl = "https://example.invalid/fleethub-agent.apk";

    // ---------------------------------------------------------------- the trust root

    [Fact]
    public async Task A_manifest_signed_by_another_key_installs_nothing()
    {
        // The one that matters. A compromised hub can move a device between channels; it has
        // never held this key, so it cannot make a manifest this agent will install.
        using var fixture = new Fixture();
        var (pub, _) = NewKey();
        var (_, otherPriv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", payload, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(otherPriv, manifest), payload, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Null(fixture.Installer.InstalledPath);
    }

    [Fact]
    public async Task A_manifest_whose_signature_covers_other_bytes_installs_nothing()
    {
        using var fixture = new Fixture();
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var real = ManifestBytes("9.9.9", payload, PayloadUrl);
        var tampered = ManifestBytes("9.9.9", "different payload"u8.ToArray(), PayloadUrl);

        var updater = Build(fixture, tampered, Sign(priv, real), payload, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Null(fixture.Installer.InstalledPath);
    }

    [Fact]
    public void The_release_key_is_the_one_that_signs_every_other_artifact_here()
    {
        // agent/, agent-linux/ and hub/clientrelease.py hold the same value. Restated rather
        // than read, because none is importable from here -- and pinned because a key differing
        // by one character would refuse every real release while looking entirely plausible.
        Assert.Equal("9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c",
                     AgentConfig.UpdatePublicKeyHex);
    }

    [Fact]
    public void The_manifest_url_is_this_agents_own_and_not_another_platforms()
    {
        // The concrete hazard the per-platform change exists to remove: the Windows manifest
        // describes a win-x64 executable, which a phone cannot execute in any sense.
        Assert.Contains("agent-android/agent-android.manifest.json", AgentConfig.StableManifestUrl);
        Assert.DoesNotContain("/agent/agent.manifest.json", AgentConfig.StableManifestUrl);
        Assert.DoesNotContain("agent-linux", AgentConfig.StableManifestUrl);
        Assert.StartsWith("https://", AgentConfig.StableManifestUrl);
        Assert.Equal(AgentConfig.UpdateManifestUrl + ".sig", AgentConfig.UpdateManifestSigUrl);
    }

    // ---------------------------------------------------------------- what it downloads

    [Fact]
    public async Task A_payload_that_does_not_match_the_signed_digest_installs_nothing()
    {
        // The check that makes the download itself untrusted: the APK can come from anywhere,
        // as long as it hashes to the value inside a manifest the release key signed.
        using var fixture = new Fixture();
        var (pub, priv) = NewKey();
        var promised = "the apk the manifest describes"u8.ToArray();
        var served = "something else entirely"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", promised, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(priv, manifest), served, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Null(fixture.Installer.InstalledPath);
    }

    [Fact]
    public async Task A_manifest_that_is_not_newer_installs_nothing()
    {
        // Strictly newer, which is what makes a republished manifest a no-op rather than an
        // install loop -- every device would otherwise re-download and reinstall the build it
        // is already running, on every tick.
        using var fixture = new Fixture();
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes(AgentConfig.Version, payload, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(priv, manifest), payload, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Null(fixture.Installer.InstalledPath);
    }

    [Fact]
    public async Task A_device_that_is_not_fully_managed_does_not_even_look()
    {
        // Silent install is a Device Owner power. Without it the platform raises its own
        // installer UI, which on a phone in a drawer is an update waiting forever behind a
        // dialog nobody will tap -- so an unmanaged build does not try.
        using var fixture = new Fixture();
        fixture.Installer.CanInstallSilently = false;
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", payload, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(priv, manifest), payload, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Null(fixture.Installer.InstalledPath);
    }

    // ---------------------------------------------------------------- ordering and the guard

    [Fact]
    public async Task The_attempt_is_recorded_BEFORE_the_install_is_handed_over()
    {
        // The assertion peculiar to this platform. The install kills this process, so anything
        // written after it is never written -- and the counter is the only thing standing
        // between a build that dies on startup and an install loop.
        using var fixture = new Fixture();
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", payload, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(priv, manifest), payload, PayloadUrl, pub);
        Assert.True(await updater.CheckAndApplyAsync(default));

        Assert.NotNull(fixture.Installer.StateAtInstall);
        Assert.Equal("9.9.9", fixture.Installer.StateAtInstall!.Target);
        Assert.Equal(1, fixture.Installer.StateAtInstall.Count);
        Assert.NotNull(fixture.Installer.InstalledPath);
    }

    [Fact]
    public async Task The_same_target_is_given_up_on_after_the_attempt_cap()
    {
        using var fixture = new Fixture();
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", payload, PayloadUrl);
        var updater = Build(fixture, manifest, Sign(priv, manifest), payload, PayloadUrl, pub);

        for (var attempt = 0; attempt < AgentConfig.MaxChainRestarts; attempt++)
        {
            Assert.True(await updater.CheckAndApplyAsync(default));
        }
        // The device would have restarted between each of those. On the next one it stops.
        Assert.False(await updater.CheckAndApplyAsync(default));
    }

    [Fact]
    public async Task A_refused_install_leaves_no_staged_file_behind()
    {
        using var fixture = new Fixture();
        fixture.Installer.Failure = "INSTALL_FAILED_VERSION_DOWNGRADE";
        var (pub, priv) = NewKey();
        var payload = "a pretend apk"u8.ToArray();
        var manifest = ManifestBytes("9.9.9", payload, PayloadUrl);

        var updater = Build(fixture, manifest, Sign(priv, manifest), payload, PayloadUrl, pub);
        Assert.False(await updater.CheckAndApplyAsync(default));
        Assert.Empty(Directory.Exists(fixture.Directory)
            ? Directory.GetFiles(fixture.Directory) : []);
    }

    [Fact]
    public void Confirming_a_build_clears_the_counter_so_the_NEXT_update_starts_at_zero()
    {
        // There is no previous artifact to retire here, unlike the other two agents -- the
        // platform owns that. What this prevents is a device that has legitimately updated
        // three times being refused a fourth because the counter never reset.
        using var fixture = new Fixture();
        fixture.State.SaveRestartState(new RestartState { Target = AgentConfig.Version, Count = 3 });
        var updater = new SelfUpdater(NullLogger<SelfUpdater>.Instance, fixture.State,
                                      fixture.Installer);

        updater.ConfirmRunningBuild();
        Assert.Null(fixture.State.LoadRestartState());
    }

    [Fact]
    public void Confirming_does_NOT_clear_a_counter_for_a_build_that_never_arrived()
    {
        // The device came back on the OLD version, which means the install did not take. The
        // counter is what stops that target being tried forever, so clearing it here would
        // defeat the guard at exactly the moment it is doing its job.
        using var fixture = new Fixture();
        fixture.State.SaveRestartState(new RestartState { Target = "99.0.0", Count = 2 });
        var updater = new SelfUpdater(NullLogger<SelfUpdater>.Instance, fixture.State,
                                      fixture.Installer);

        updater.ConfirmRunningBuild();
        Assert.Equal(2, fixture.State.LoadRestartState()?.Count);
    }

}
