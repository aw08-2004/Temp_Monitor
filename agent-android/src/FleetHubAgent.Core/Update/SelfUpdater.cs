using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using FleetHubAgent.State;

namespace FleetHubAgent.Update;

/// <summary>What actually installs a package. Deliberately tiny and deliberately in Core, like
/// ILocationSource and IDeviceSecurity: everything about WHETHER to install, WHAT to trust and
/// WHEN to give up is <see cref="SelfUpdater"/>'s business, so the platform half cannot
/// accidentally decide any of it.</summary>
public interface IPackageInstaller
{
    /// <summary>Whether this device can install a package without anybody tapping anything.
    /// False on a device that is not a Device Owner, which is the ordinary state of a
    /// sideloaded build -- and the reason the updater reports rather than retries.</summary>
    bool CanInstallSilently { get; }

    /// <summary>Install the APK at <paramref name="path"/>, replacing this app.
    ///
    /// **This does not return on success.** The platform kills the process as part of
    /// replacing it, which is why nothing in the updater runs afterwards and why the agent has
    /// to be restarted by a broadcast rather than by returning here (see BootReceiver's
    /// ACTION_MY_PACKAGE_REPLACED). Returns a reason on failure; must not throw.</summary>
    string? Install(string path);
}

/// <summary>
/// Signed self-update for the Android agent (roadmap #22).
///
/// **Everything down to the install is identical to the other two agents, and the install is
/// nothing like them.** A Windows or Linux agent replaces a file and restarts; an Android app
/// cannot replace its own APK by writing to disk. What it can do -- and only as a Device Owner
/// -- is hand the bytes to the platform's package installer, which replaces the app and kills
/// the process. So the shared half lives here and the install is behind
/// <see cref="IPackageInstaller"/>.
///
/// Three consequences worth stating, because each is a difference somebody will otherwise trip
/// over while reading this next to SelfUpdater on the other agents:
///
///   1. **There is no `.old` to keep.** The platform holds the previous package during the
///      replace and rolls back itself if the new one will not install. Nothing here can
///      preserve an artifact, so nothing here pretends to -- the confirmation dance the other
///      two do (hold the old binary until the new one reaches the hub) has no equivalent, and
///      inventing one would be theatre.
///   2. **The restart is a broadcast.** ACTION_MY_PACKAGE_REPLACED arrives at this app's own
///      receiver after the install, which starts the service again. Without it a self-updated
///      agent is installed, stopped, and waiting for somebody to open it -- on a device with
///      nobody in front of it.
///   3. **The APK must be signed with the same key.** Android refuses an update signed by
///      anything else, which is a second, platform-enforced check on top of the manifest
///      signature this class verifies. They are not redundant: Android's says "same author as
///      what is installed", and ours says "the fleet's release key approved this exact build".
///
/// **The restart guard is still here and still matters.** A build that installs, starts, and
/// immediately dies would otherwise be downloaded and installed again on the next tick,
/// forever, on every device that took the release.
/// </summary>
public sealed class SelfUpdater
{
    private readonly ILogger<SelfUpdater> _log;
    private readonly AgentState _state;
    private readonly IPackageInstaller _installer;
    private readonly HttpClient _http;

    /// <summary>The key every manifest is checked against.
    ///
    /// **INTERNAL, and settable only through the internal constructor below.** The trust root
    /// is a compile-time constant precisely because it is the one control that survives a
    /// compromised hub, so it must not become configurable to make anything easier -- not a
    /// setting, not managed configuration, not an environment variable. What the tests need is
    /// a manifest this class will accept, and an internal seam gives them that without adding
    /// a public surface an app could reach.</summary>
    private readonly string _publicKeyHex;

    public SelfUpdater(ILogger<SelfUpdater> log, AgentState state, IPackageInstaller installer,
                       HttpClient? http = null)
        : this(log, state, installer, http, AgentConfig.UpdatePublicKeyHex) { }

    internal SelfUpdater(ILogger<SelfUpdater> log, AgentState state, IPackageInstaller installer,
                         HttpClient? http, string publicKeyHex)
    {
        _log = log;
        _state = state;
        _installer = installer;
        _http = http ?? new HttpClient { Timeout = TimeSpan.FromMinutes(5) };
        _publicKeyHex = publicKeyHex;
    }

    /// <summary>Check for and apply an update. Returns true only if an install was STARTED, in
    /// which case the process is about to be killed by the platform and the caller should stop
    /// doing work.</summary>
    public async Task<bool> CheckAndApplyAsync(CancellationToken ct)
    {
        if (AgentConfig.UpdatesDisabled) return false;

        if (!_installer.CanInstallSilently)
        {
            // Reported once per check rather than retried silently. A sideloaded build is the
            // ordinary state of a device that was never provisioned, and "this device is not
            // fully managed" is something an operator can act on -- unlike a stream of
            // install failures, which reads as a broken agent.
            _log.LogDebug("[update] not a device owner; skipping the update check");
            return false;
        }

        string? staged = null;
        try
        {
            // 1. Manifest + detached signature.
            var manifestBytes = await _http.GetByteArrayAsync(AgentConfig.UpdateManifestUrl, ct);
            var sigHex = (await _http.GetStringAsync(AgentConfig.UpdateManifestSigUrl, ct)).Trim();

            // 2. Verify the signature over the EXACT manifest bytes, fail-closed. Everything
            // after this point trusts the contents; nothing before it does.
            if (!SignatureVerifier.VerifyRaw(_publicKeyHex, manifestBytes, sigHex))
            {
                _log.LogWarning("[update] manifest signature invalid -- refusing");
                return false;
            }

            var manifest = JsonSerializer.Deserialize<UpdateManifest>(manifestBytes);
            if (manifest is null || string.IsNullOrEmpty(manifest.Version)
                || string.IsNullOrEmpty(manifest.Sha256) || string.IsNullOrEmpty(manifest.Url))
            {
                _log.LogWarning("[update] manifest incomplete");
                return false;
            }

            // 3. Newer? Strictly, which is what makes a replayed or rolled-back manifest a
            // no-op rather than a downgrade -- and Android would refuse a downgrade anyway.
            if (VersionUtil.Compare(manifest.Version, AgentConfig.Version) <= 0) return false;

            // 4. Restart-loop guard.
            var restart = _state.LoadRestartState();
            if (restart is not null && restart.Target == manifest.Version
                && restart.Count >= AgentConfig.MaxChainRestarts)
            {
                _log.LogWarning("[update] giving up on {Target} after {Count} attempts",
                    restart.Target, restart.Count);
                return false;
            }

            _log.LogInformation("[update] {Cur} -> {New}", AgentConfig.Version, manifest.Version);

            // 5. Download and verify against the SIGNED sha256. The download is untrusted by
            // design: the APK can come from anywhere as long as it hashes to this.
            var apk = await _http.GetByteArrayAsync(manifest.Url, ct);
            var actual = Convert.ToHexString(SHA256.HashData(apk)).ToLowerInvariant();
            if (!string.Equals(actual, manifest.Sha256.Trim().ToLowerInvariant(),
                               StringComparison.Ordinal))
            {
                _log.LogWarning("[update] sha256 mismatch (got {Got}, want {Want}) -- aborting",
                    actual, manifest.Sha256);
                return false;
            }

            // 6. Stage it inside this app's own storage. Not the shared cache and not external
            // storage: the file is about to be installed as this fleet's device owner, and
            // those are readable or writable by things that are not this app.
            Directory.CreateDirectory(AgentConfig.UpdateStagingDir);
            staged = Path.Combine(AgentConfig.UpdateStagingDir,
                                  $"fleethub-agent-{manifest.Version}.apk");
            await File.WriteAllBytesAsync(staged, apk, ct);

            // 6b. Re-verify FROM DISK, because what is handed to the installer is the file
            // rather than the byte array checked above.
            var stagedSha = Convert.ToHexString(
                SHA256.HashData(await File.ReadAllBytesAsync(staged, ct))).ToLowerInvariant();
            if (!string.Equals(stagedSha, actual, StringComparison.Ordinal))
            {
                _log.LogWarning("[update] staged file changed after verification -- aborting");
                TryDelete(staged);
                return false;
            }

            // 7. Record the attempt BEFORE handing over. The install kills this process, so
            // anything written after it is not written at all -- and the counter is the only
            // thing standing between a broken build and an install loop.
            _state.SaveRestartState(new RestartState
            {
                Target = manifest.Version,
                Count = (restart?.Target == manifest.Version ? restart!.Count : 0) + 1,
            });

            var failure = _installer.Install(staged);
            if (failure is null)
            {
                // Rarely reached: the platform usually kills the process inside Install.
                _log.LogInformation("[update] install of {New} started", manifest.Version);
                return true;
            }

            _log.LogWarning("[update] install refused: {Reason}", failure);
            TryDelete(staged);
            return false;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("[update] check failed (offline?): {Msg}", e.Message);
            if (staged is not null) TryDelete(staged);
            return false;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "[update] unexpected error");
            if (staged is not null) TryDelete(staged);
            return false;
        }
    }

    /// <summary>Called once this build has done real work the hub accepted. Clears the attempt
    /// counter so the NEXT update starts from zero.
    ///
    /// There is no previous artifact to retire here, unlike the other two agents -- the
    /// platform owns that. What this prevents is a device that has updated three times
    /// legitimately being refused a fourth because the counter never reset.</summary>
    public void ConfirmRunningBuild()
    {
        var restart = _state.LoadRestartState();
        if (restart is null) return;
        if (VersionUtil.Compare(AgentConfig.Version, restart.Target) < 0) return;
        _state.ClearRestartState();
        _log.LogInformation("Update to {Version} confirmed -- the hub accepted this build",
            AgentConfig.Version);
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch { /* a staged APK left behind costs disk, not correctness */ }
    }
}
